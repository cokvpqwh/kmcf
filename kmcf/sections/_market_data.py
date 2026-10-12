"""大盘与情绪：数据获取（主数据源 + 备用数据源）。

所有网络请求都经过 ctx.fetch（磁盘缓存 / 重试 / 限速）。本文件里的自定义请求函数只做“请求 + 解析 + 校验”，
校验不通过就抛异常，这样错误数据不会进缓存。

数据源（2026-10 实测）：
- 指数日线：东方财富 push2his（stock_zh_index_daily_em，含成交额）→ 备用 新浪财经日线（无成交额）
- 涨跌分布：东方财富涨停板专题 push2ex getTopicZDFenBu（只有最新快照，带数据日期可核对）→ 备用 乐咕乐股（快照）
- 涨停/炸板/跌停/昨日涨停池：东方财富 push2ex，按日期查询，只能查最近约 30 个自然日
- 成交额备用：上交所 + 深交所官网每日成交概况（按日期查询）

站点失败标记（ctx.shared）：某站点出现连接级失败（连不上/被重置/超时/HTTP 错误）后，
后续对该站点的请求只读缓存、没有缓存就直接跳过，避免每次都重试 3 遍。push2his_down 会跨次生成保留 3 小时
（见 report.PERSIST_SITE_FLAGS），其余标记只在同一次生成内有效。
"""

import bisect
import datetime as dt
import functools
import threading
from pathlib import Path

import akshare as ak
import pandas as pd
import requests

from .. import trade_calendar as tc
from ..fetch import FetchError, SnapshotUnavailable, cache_key
from . import _market_calc as calc
from ._site import skip_reason

PUSH2HIS_DOWN = "push2his_down"     # push2his（东方财富日线）已失败（任何失败），后续指数直接用新浪；跨次生成保留 3 小时
PUSH2EX_DOWN = "push2ex_down"       # push2ex（涨停板专题）本次连接失败
SINA_DOWN = "sina_kline_down"       # 新浪财经日线（money.finance.sina.com.cn）本次连接失败
EXCHANGE_DOWN = "exchange_down"     # 上交所/深交所官网本次连接失败
POOL_WINDOW_DAYS = 29               # 涨停板专题按日期只能回查约 30 个自然日（实测 10-11 可查到 09-11），留 1 天余量
KLINE_DAYS = 120                    # 指数日线取最近 120 个交易日（画 60 日 + 算 20 日均线）
AK_TIMEOUT = 25                     # akshare 函数内部没有设超时，这里给整次调用加上限（秒）

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
_EM_UT = "7eea3edcaed734bea9cbfc24409ed989"
# 视为“站点不可用”的错误：连接失败、被重置、超时、HTTP 错误状态。解析/格式类错误不算
_CONN_ERRORS = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError,
                requests.HTTPError, TimeoutError, ConnectionError)


class SiteDown(FetchError):
    """该站点已标记连接失败，且没有缓存，直接跳过。"""

    def __init__(self, message: str, flag: str = ""):
        super().__init__(message)
        self.flag = flag


# ---------------------------------------------------------------- 通用：失败标记 / 超时 / 缓存

def is_conn_error(err: BaseException | None) -> bool:
    """沿 __cause__ 链判断是否为连接级失败（Fetcher 抛出的 FetchError 以原始异常为 cause）。"""
    for _ in range(6):
        if err is None:
            return False
        if isinstance(err, SiteDown) or isinstance(err, _CONN_ERRORS):
            return True
        err = err.__cause__
    return False


def mark_down(ctx, err: Exception, key: str) -> None:
    """连接级失败时在 ctx.shared 记标记，同一次生成里不再请求该站点（缓存照常可用）。"""
    if is_conn_error(err):
        ctx.shared[key] = True


def has_cache(ctx, func, kwargs: dict) -> bool:
    """ctx.fetch(func, **kwargs) 是否会命中磁盘缓存。"""
    f = ctx.fetcher
    raw_dir = getattr(f, "raw_dir", None)
    if raw_dir is None or getattr(f, "force", False):
        return False
    return (Path(raw_dir) / f"{cache_key(func, (), kwargs)}.pkl").exists()


def site_fetch(ctx, key: str, func, *, snapshot: bool = False, **kwargs):
    """ctx.fetch + 站点失败标记：站点已标记失败且没有缓存时抛 SiteDown；连接级失败时打标记。"""
    if ctx.shared.get(key) and not has_cache(ctx, func, kwargs):
        raise SiteDown(f"{getattr(func, '__name__', 'call')}：该站点{skip_reason(key)}", key)
    try:
        return ctx.fetch(func, snapshot=snapshot, **kwargs)
    except SnapshotUnavailable:
        raise
    except FetchError as e:
        mark_down(ctx, e, key)
        raise


def timed(func, seconds: float = AK_TIMEOUT):
    """给没有设超时的 akshare 函数加总超时（在后台线程里执行）。保留函数名，缓存键与直接调用一致。

    同一个包装对象超时一次后，后续调用（Fetcher 的重试）立即失败，不再每次等满超时。
    """
    state = {"timed_out": False}

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if state["timed_out"]:
            raise TimeoutError(f"{func.__name__} 上次请求已超时，不再重试")
        box = {}

        def run():
            try:
                box["v"] = func(*args, **kwargs)
            except BaseException as e:  # noqa: BLE001  原样交给调用方
                box["e"] = e

        th = threading.Thread(target=run, name=f"kmcf-market-{func.__name__}", daemon=True)
        th.start()
        th.join(seconds)
        if th.is_alive():
            state["timed_out"] = True
            raise TimeoutError(f"{func.__name__} 超过 {seconds:.0f} 秒未返回")
        if "e" in box:
            raise box["e"]
        return box["v"]
    return wrapper


# ---------------------------------------------------------------- 自定义请求函数（交给 ctx.fetch）

def em_zdfenbu(date: str) -> dict:
    """东方财富涨停板专题-涨跌分布。接口只返回最新快照，date 仅用于核对返回数据的日期。"""
    r = requests.get("https://push2ex.eastmoney.com/getTopicZDFenBu",
                     params={"ut": _EM_UT, "dpt": "wz.ztzt", "date": date}, headers=_UA, timeout=15)
    r.raise_for_status()
    data = (r.json() or {}).get("data") or {}
    qdate = str(data.get("qdate") or "")
    if qdate != date:
        raise ValueError(f"涨跌分布数据日期为 {qdate or '未知'}，不是 {date}")
    fenbu = calc.parse_fenbu(data.get("fenbu"))
    if not fenbu:
        raise ValueError("涨跌分布为空")
    return {"qdate": qdate, "fenbu": fenbu}


def sina_index_daily(symbol: str, datalen: int = 200) -> pd.DataFrame:
    """新浪财经指数日线（最近 datalen 根，含当天）。只有成交量（股），没有成交额。"""
    r = requests.get("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
                     params={"symbol": symbol, "scale": "240", "ma": "no", "datalen": str(datalen)},
                     headers={**_UA, "Referer": "https://finance.sina.com.cn/"}, timeout=15)
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"新浪 {symbol} 日线为空")
    df = pd.DataFrame(rows).rename(columns={"day": "date"})
    return calc.norm_kline(df)


def legu_market_activity(date_iso: str) -> dict:
    """乐咕乐股赚钱效应（上涨/下跌/平盘/涨停/跌停家数，快照）。核对统计日期必须是复盘日。"""
    d = calc.parse_legu(timed(ak.stock_market_activity_legu)())
    stat = str(d.get("统计日期", ""))
    if not stat.startswith(date_iso):
        raise ValueError(f"乐咕乐股统计日期为 {stat or '未知'}，不是 {date_iso}")
    return d


# ---------------------------------------------------------------- 指数日线

def _sina_datalen(start: dt.date) -> int:
    """从 start 到今天大约有多少根日线（新浪只能取“最近 N 根”）。"""
    days = tc.trade_days()
    n = bisect.bisect_right(days, dt.date.today()) - bisect.bisect_left(days, start)
    return max(n + 5, 30)


def load_index(ctx, symbol: str) -> tuple[pd.DataFrame, str]:
    """指数日线（截至复盘日），返回 (df, 来源 'em'|'sina')。df 列见 calc.norm_kline。"""
    start = ctx.recent_trade_days(KLINE_DAYS)[0]
    partial = None
    em_func = timed(ak.stock_zh_index_daily_em)
    em_kw = {"symbol": symbol, "start_date": start.strftime("%Y%m%d"), "end_date": ctx.date_str}
    if not ctx.shared.get(PUSH2HIS_DOWN) or has_cache(ctx, em_func, em_kw):
        try:
            df = calc.upto(calc.norm_kline(ctx.fetch(em_func, **em_kw)), ctx.date_iso)
            if not df.empty and df["date"].iloc[-1] == ctx.date_iso:
                return df, "em"
            partial = df if not df.empty else None
            ctx.log.warning("[market] 东方财富 %s 日线未包含复盘日，改用新浪", symbol)
        except FetchError as e:
            ctx.shared[PUSH2HIS_DOWN] = True
            ctx.log.warning("[market] push2his 不可用，指数日线改用新浪：%s", e)
    try:
        df = site_fetch(ctx, SINA_DOWN, sina_index_daily, symbol=symbol, datalen=_sina_datalen(start))
        df = calc.upto(df, ctx.date_iso)
        df = df[df["date"] >= start.isoformat()].reset_index(drop=True)
    except FetchError:
        if partial is not None:
            return partial, "em"
        raise
    return df, "sina"


# ---------------------------------------------------------------- 成交额（交易所官网，备用）

def exchange_turnover(ctx, d: dt.date) -> float | None:
    """上交所股票成交金额 + 深交所股票成交金额（元）。任一缺失返回 None。未发布的日期接口会抛错（不进缓存）。"""
    ds = d.strftime("%Y%m%d")
    sh = calc.sse_stock_amount(site_fetch(ctx, EXCHANGE_DOWN, timed(ak.stock_sse_deal_daily), date=ds))
    sz = calc.szse_stock_amount(site_fetch(ctx, EXCHANGE_DOWN, timed(ak.stock_szse_summary), date=ds))
    return sh + sz if sh and sz else None


# ---------------------------------------------------------------- 涨停板专题股票池

POOL_FUNCS = {
    "zt": ak.stock_zt_pool_em,              # 涨停池
    "zb": ak.stock_zt_pool_zbgc_em,         # 炸板池
    "dt": ak.stock_zt_pool_dtgc_em,         # 跌停池
    "prev": ak.stock_zt_pool_previous_em,   # 昨日涨停池（今日表现）
}


def pool_window_ok(d: dt.date, today: dt.date | None = None) -> bool:
    """该日期的股票池是否还能按日期查到。"""
    today = today or dt.date.today()
    return d >= today - dt.timedelta(days=POOL_WINDOW_DAYS)


def fetch_pool(ctx, kind: str, d: dt.date) -> pd.DataFrame | None:
    """取某日某个股票池（统一格式）。涨停池/昨日涨停池为空视为“数据不可用”返回 None。

    失败抛 FetchError（含 SiteDown）；返回了数据但缺必需列抛 calc.PoolFormatError。
    函数名不变（timed 保留 __name__），与其他模块的 ctx.fetch(ak.stock_zt_pool_em, date=...) 共用缓存。
    """
    raw = site_fetch(ctx, PUSH2EX_DOWN, timed(POOL_FUNCS[kind]), date=d.strftime("%Y%m%d"))
    df = calc.norm_pool(raw, kind)
    if kind in ("zt", "prev") and (df is None or df.empty):
        return None
    return df


def pool_error_text(err: Exception) -> str:
    """股票池获取失败时给用户看的说明（原始异常只写日志）。"""
    if isinstance(err, calc.PoolFormatError):
        return f"东方财富涨停板专题{err}（数据格式有变化），已跳过"
    if isinstance(err, SiteDown):
        return f"东方财富涨停板专题{skip_reason(err.flag or PUSH2EX_DOWN)}"
    if is_conn_error(err):
        return "东方财富涨停板专题连接失败或超时，已跳过"
    return "东方财富涨停板专题暂时无法获取（接口返回异常），已跳过"
