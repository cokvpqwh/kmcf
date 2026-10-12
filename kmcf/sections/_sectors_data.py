"""板块轮动：数据源适配。所有请求都经 ctx.fetch（磁盘缓存 / 重试 / 限速）。

板块实时排行（只能取最新快照，snapshot=True）。已有当天缓存的数据源优先（同一天重跑结果一致），否则按顺序尝试：
    1. 东方财富 push2 板块列表（行业 m:90 t:2 / 概念 m:90 t:3，含主力净流入 f62）
    2. 同花顺：行业一览 stock_board_industry_summary_ths（+ 行业资金流 stock_fund_flow_industry 的净额）；
       概念资金流 stock_fund_flow_concept（均为“即时”）
    3. 新浪财经：stock_sector_spot（新浪行业 / 概念，无资金流）
某个站点出现连接级失败（连不上 / 被重置 / 超时 / HTTP 错误），就在 ctx.shared 里记标记
（push2_down / ths_down …），后续同站请求只读缓存、没有缓存就跳过。解析类错误不打标记，不影响同站其他接口。
push2_down、push2his_down 会跨次生成保留 3 小时（见 report.PERSIST_SITE_FLAGS），其余标记只在本次生成内有效。

可按日期查询（不受快照限制）：
    涨停池 stock_zt_pool_em(date=)（push2ex，只能回查约 30 个自然日）
    行业历史：同花顺 stock_board_industry_index_ths / 东方财富 stock_board_industry_hist_em（push2his，需 BK 代码）
"""

import datetime as dt
import functools
import logging
import os
import re
import sys
import threading
import time
from io import StringIO
from pathlib import Path
from typing import NamedTuple

import akshare as ak
import pandas as pd
import requests

from .. import trade_calendar as tc
from ..fetch import FetchError, SnapshotUnavailable, cache_key
from . import _sectors_calc as calc
from ._site import skip_brief, skip_reason

log = logging.getLogger("kmcf")

SRC_LABEL = {"em": "东方财富", "ths": "同花顺", "sina": "新浪财经"}
KIND_LABEL = {"industry": "行业", "concept": "概念"}
# 站点 -> ctx.shared 里的“已连接失败”标记（与其他模块共用同名 key；push2_down / push2his_down 跨次生成保留 3 小时）
FLAGS = {"em": "push2_down", "emh": "push2his_down", "ex": "push2ex_down", "ths": "ths_down", "sina": "sina_down"}
ORDER = ("em", "ths", "sina")
TIMEOUT = 20            # akshare 函数内部没有设超时，给整次调用加上限（秒）；正常 1~3 秒
POOL_WINDOW_DAYS = 29   # 东方财富涨停池按日期只能回查约 30 个自然日（与 market 模块一致），留 1 天余量

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")
# 视为“站点不可用”的错误：连接失败、被重置、超时、HTTP 错误状态。解析/格式类错误不算
_CONN_ERRORS = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError,
                requests.HTTPError, TimeoutError, ConnectionError)


class SiteSkipped(FetchError):
    """该站点已标记连接失败（本次生成中，或 push2 / push2his 近 3 小时内），且没有可用缓存，直接跳过。"""

    def __init__(self, message: str, flag: str = ""):
        super().__init__(message)
        self.flag = flag


# ---------------------------------------------------------------- 通用

def ensure_stdio() -> None:
    """pythonw（定时任务）下 sys.stdout/stderr 为 None，akshare 内部的 tqdm 进度条会抛 AttributeError。"""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except OSError:
                pass


def is_conn_error(err: BaseException | None) -> bool:
    """沿 __cause__ 链判断是否为连接级失败（Fetcher 抛出的 FetchError 以原始异常为 cause）。"""
    for _ in range(6):
        if err is None:
            return False
        if isinstance(err, SiteSkipped) or isinstance(err, _CONN_ERRORS):
            return True
        err = err.__cause__
    return False


def reason(err: BaseException) -> str:
    """给用户看的失败原因（原始异常只写日志）。"""
    if isinstance(err, SiteSkipped):
        return skip_brief(err.flag)
    if is_conn_error(err):
        return "连接失败或超时"
    return "返回数据异常"


def with_timeout(func, seconds: float = TIMEOUT):
    """给没有设超时的 akshare 函数加总超时（在线程里执行）。保留函数名，缓存键与直接调用一致。

    同一个包装对象超时一次后，后续调用（ctx.fetch 的重试）立即失败，错误信息保留超时原因。
    """
    state: dict = {}

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if "timeout" in state:
            raise TimeoutError(state["timeout"])
        box = {}

        def run():
            try:
                box["v"] = func(*args, **kwargs)
            except BaseException as e:  # noqa: BLE001  原样转交给调用方
                box["e"] = e

        th = threading.Thread(target=run, name=f"kmcf-{func.__name__}", daemon=True)
        th.start()
        th.join(seconds)
        if th.is_alive():
            state["timeout"] = f"{func.__name__} 超过 {seconds:.0f} 秒未返回（不再重试）"
            raise TimeoutError(f"{func.__name__} 超过 {seconds:.0f} 秒未返回")
        if "e" in box:
            raise box["e"]
        return box["v"]
    return wrapper


def _cache_path(ctx, func, args, kwargs) -> Path | None:
    raw_dir = getattr(ctx.fetcher, "raw_dir", None)
    return Path(raw_dir) / f"{cache_key(func, tuple(args), dict(kwargs))}.pkl" if raw_dir else None


def _cache_exists(ctx, func, args=(), kwargs=None) -> bool:
    p = _cache_path(ctx, func, args, kwargs or {})
    return bool(p) and p.exists()


def _has_cache(ctx, func, args, kwargs) -> bool:
    """ctx.fetch 这次会不会直接读缓存（--force 时不会）。"""
    return not getattr(ctx.fetcher, "force", False) and _cache_exists(ctx, func, args, kwargs)


def _read_cache(ctx, func, args, kwargs):
    """直接读当天的磁盘缓存（不经 Fetcher）；没有或读不了返回 None。"""
    p = _cache_path(ctx, func, args, kwargs)
    if p is None or not p.exists():
        return None
    try:
        return pd.read_pickle(p)
    except Exception as e:  # noqa: BLE001
        log.warning("[sectors] 缓存读取失败 %s：%s", p.name, e)
        return None


def _force_fallback(ctx, func, args, kwargs):
    """--force 刷新失败时沿用已有的当天缓存（Fetcher 在 force 模式下不会自己回退）。"""
    if not getattr(ctx.fetcher, "force", False):
        return None
    obj = _read_cache(ctx, func, args, kwargs)
    if obj is not None:
        ctx.log.info("[sectors] %s 强制刷新失败，沿用已有缓存", getattr(func, "__name__", "call"))
    return obj


def fetch(ctx, site: str, func, *args, snapshot: bool = False, **kwargs):
    """ctx.fetch + 站点失败标记：该站点已标记连接失败且无缓存时直接跳过，避免每次都重试 3 遍。"""
    ensure_stdio()
    flag = FLAGS[site]
    if ctx.shared.get(flag) and not _has_cache(ctx, func, args, kwargs):
        cached = _force_fallback(ctx, func, args, kwargs)
        if cached is not None:
            return cached
        raise SiteSkipped(f"{func.__name__}：该站点{skip_reason(flag)}", flag)
    try:
        return ctx.fetch(func, *args, snapshot=snapshot, **kwargs)
    except SnapshotUnavailable:
        raise
    except FetchError as e:
        if is_conn_error(e):
            ctx.shared[flag] = True
        cached = _force_fallback(ctx, func, args, kwargs)
        if cached is not None:
            return cached
        raise


# ---------------------------------------------------------------- 东方财富 push2

EM_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
EM_FS = {"industry": "m:90 t:2 f:!50", "concept": "m:90 t:3 f:!50"}
EM_FIELDS = "f2,f3,f4,f6,f8,f12,f14,f20,f62,f104,f105,f128,f136,f184"


def em_board_spot(fs: str) -> pd.DataFrame:
    """东方财富板块列表（行业/概念）原始字段：f3 涨跌幅 f6 成交额 f62 主力净流入 f104/f105 涨跌家数 f128/f136 领涨股。"""
    params = {"pn": 1, "pz": 100, "po": 1, "np": 1, "ut": "bd1d9ddb04089700cf9c27f6f7426281",
              "fltt": 2, "invt": 2, "fid": "f3", "fs": fs, "fields": EM_FIELDS}
    rows, page = [], 1
    while page <= 30:
        r = requests.get(EM_CLIST, params={**params, "pn": page}, headers={"User-Agent": UA}, timeout=(5, 15))
        r.raise_for_status()
        data = (r.json() or {}).get("data") or {}
        diff = data.get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        rows.extend(diff)
        if not diff or len(rows) >= int(data.get("total") or 0):
            break
        page += 1
        time.sleep(0.4)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 同花顺

THS_FLOW_URL = "http://data.10jqka.com.cn/funds/{board}/field/tradezdf/order/desc/page/{page}/ajax/1/free/1/"
THS_FLOW_COLS = ["序号", "行业", "行业指数", "行业-涨跌幅", "流入资金", "流出资金", "净额", "公司家数",
                 "领涨股", "领涨股-涨跌幅", "当前价"]


def _ths_v() -> str:
    """同花顺反爬 token（hexin-v），用 akshare 自带的 ths.js 计算。"""
    import py_mini_racer
    from akshare.datasets import get_ths_js

    js = py_mini_racer.MiniRacer()
    with open(get_ths_js("ths.js"), encoding="utf-8") as f:
        js.eval(f.read())
    return js.call("v")


def _ths_fund_flow(board: str) -> pd.DataFrame:
    """同花顺资金流向（即时）：board = gnzjl（概念）/ hyzjl（行业）。每页有超时，页间隔 0.6 秒。"""
    frames, pages, page = [], 1, 1
    while page <= pages:
        headers = {"User-Agent": UA, "hexin-v": _ths_v(), "X-Requested-With": "XMLHttpRequest",
                   "Referer": f"http://data.10jqka.com.cn/funds/{board}/", "Accept": "text/html, */*; q=0.01"}
        r = requests.get(THS_FLOW_URL.format(board=board, page=page), headers=headers, timeout=(5, 20))
        r.raise_for_status()
        if page == 1:
            m = re.search(r'page_info">\s*\d+\s*/\s*(\d+)', r.text)
            if not m:
                raise ValueError("同花顺资金流页面结构异常（无分页信息）")
            pages = min(int(m.group(1)), 30)
        frames.append(pd.read_html(StringIO(r.text))[0])
        page += 1
        if page <= pages:
            time.sleep(0.6)
    big = pd.concat(frames, ignore_index=True)
    if big.shape[1] != len(THS_FLOW_COLS):
        raise ValueError(f"同花顺资金流列数异常：{list(big.columns)}")
    big.columns = THS_FLOW_COLS
    big["序号"] = range(1, len(big) + 1)
    for c in ("行业-涨跌幅", "领涨股-涨跌幅"):
        big[c] = calc.to_num(big[c])
    return big


def stock_fund_flow_concept(symbol: str = "即时") -> pd.DataFrame:
    """同花顺-概念资金流（即时）。输出与 ak.stock_fund_flow_concept(symbol="即时") 相同（同名同参 -> 共享缓存）。"""
    if symbol != "即时":
        raise ValueError("只支持 symbol='即时'")
    return _ths_fund_flow("gnzjl")


def stock_fund_flow_industry(symbol: str = "即时") -> pd.DataFrame:
    """同花顺-行业资金流（即时）。输出与 ak.stock_fund_flow_industry(symbol="即时") 相同（同名同参 -> 共享缓存）。"""
    if symbol != "即时":
        raise ValueError("只支持 symbol='即时'")
    return _ths_fund_flow("hyzjl")


# ---------------------------------------------------------------- 板块排行（带回退）

class Boards(NamedTuple):
    df: pd.DataFrame        # 标准板块表（见 _sectors_calc.STD_COLS）
    src: str                # 实际数据源 em / ths / sina
    failed: list            # 失败的数据源说明，如 ["东方财富（连接失败或超时）"]
    net_basis: str = ""     # 净流入口径：em_main 主力净流入 / ths_flow 同花顺资金流净额 / ths_summary 同花顺行业一览


def _spec(kind: str, src: str) -> tuple[str, object, dict]:
    """板块排行的原始请求：(站点, 函数, 参数)。"""
    if src == "em":
        return "em", em_board_spot, {"fs": EM_FS[kind]}
    if src == "ths":
        if kind == "industry":
            return "ths", with_timeout(ak.stock_board_industry_summary_ths), {}
        return "ths", stock_fund_flow_concept, {"symbol": "即时"}
    return "sina", with_timeout(ak.stock_sector_spot), {"indicator": "新浪行业" if kind == "industry" else "概念"}


def _raw(ctx, kind: str, src: str) -> pd.DataFrame:
    site, func, kw = _spec(kind, src)
    return fetch(ctx, site, func, snapshot=True, **kw)


def standardize(kind: str, src: str, raw: pd.DataFrame) -> pd.DataFrame:
    if src == "em":
        return calc.standardize(raw, calc.EM_MAP)
    if src == "ths":
        if kind == "industry":
            return calc.standardize(raw, calc.THS_INDUSTRY_MAP, calc.THS_INDUSTRY_SCALE)
        return calc.standardize(raw, calc.THS_CONCEPT_MAP, calc.THS_CONCEPT_SCALE)
    df = calc.standardize(raw, calc.SINA_MAP)
    if kind == "industry":
        df = df[~df["name"].isin(calc.SINA_INDUSTRY_EXCLUDE)].reset_index(drop=True)
    return df


def _order(ctx, kind: str) -> list[str]:
    """东方财富/同花顺里已有当天缓存的排在最前（重跑时数据源和行业分类不变，也不必先等东方财富失败）。"""
    if getattr(ctx.fetcher, "force", False):
        return list(ORDER)
    cached = [s for s in ("em", "ths") if _cache_exists(ctx, _spec(kind, s)[1], (), _spec(kind, s)[2])]
    return cached + [s for s in ORDER if s not in cached]


def _ths_industry_net(ctx, df: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """同花顺行业的净流入改用行业资金流“净额”，与概念资金流同一口径；取不到时保留行业一览的“净流入”。"""
    try:
        flow = calc.standardize(fetch(ctx, "ths", stock_fund_flow_industry, symbol="即时", snapshot=True),
                                calc.THS_FLOW_MAP, calc.THS_CONCEPT_SCALE)
    except Exception as e:  # noqa: BLE001  含快照不可用
        ctx.log.info("[sectors] 同花顺行业资金流获取失败，净流入沿用行业一览：%s", e)
        return df, "ths_summary"
    net = flow.set_index("name")["net"]
    matched = df["name"].isin(net.index).mean() if len(df) else 0
    if matched < 0.8:
        ctx.log.info("[sectors] 同花顺行业资金流与行业一览名称匹配率 %.0f%%，净流入沿用行业一览", matched * 100)
        return df, "ths_summary"
    return df.assign(net=df["name"].map(net).astype(float).values), "ths_flow"


def load_boards(ctx, kind: str) -> Boards:
    """板块排行（带回退）。全部失败时抛 SnapshotUnavailable / FetchError（信息已简化，详细原因写日志）。"""
    failed, snap_missing = [], False
    for src in _order(ctx, kind):
        try:
            df = standardize(kind, src, _raw(ctx, kind, src))
        except SnapshotUnavailable:
            snap_missing = True
            continue
        except Exception as e:  # noqa: BLE001  换下一个源
            ctx.log.warning("[sectors] %s%s排行获取失败：%s", SRC_LABEL[src], KIND_LABEL[kind], e)
            failed.append(f"{SRC_LABEL[src]}（{reason(e)}）")
            continue
        if df.empty or not df["pct"].notna().any():
            failed.append(f"{SRC_LABEL[src]}（返回空数据）")
            continue
        basis = {"em": "em_main", "sina": ""}.get(src, "ths_flow")
        if src == "ths" and kind == "industry":
            df, basis = _ths_industry_net(ctx, df)
        return Boards(df, src, failed, basis)
    if snap_missing and not ctx.snapshot_ok:
        raise SnapshotUnavailable("板块实时排行只有最新快照，当前时刻无法取得复盘日数据，且没有当天的缓存")
    raise FetchError("所有数据源均未取到：" + "、".join(failed) + "（详见日志）")


# ---------------------------------------------------------------- 涨停池（按日期查询）

class ZtPool(NamedTuple):
    df: pd.DataFrame | None
    status: str             # ok / empty / no_data / out_of_window / bad_format / failed
    reason: str = ""


def zt_window_ok(d: dt.date, today: dt.date | None = None) -> bool:
    """该日期的涨停池是否还能按日期查到。"""
    today = today or dt.date.today()
    return d >= today - dt.timedelta(days=POOL_WINDOW_DAYS)


def classify_pool(df) -> ZtPool:
    """akshare 在接口返回 data=null（无数据/超出回查范围）时给出没有任何列的空表，与“当日 0 只涨停”区分开。"""
    if df is None or not isinstance(df, pd.DataFrame) or df.shape[1] == 0:
        return ZtPool(None, "no_data")
    if "所属行业" not in df.columns:
        return ZtPool(None, "bad_format")
    if df.empty:
        return ZtPool(df, "empty")
    return ZtPool(df, "ok")


def zt_pool(ctx, date_str: str) -> ZtPool:
    """东方财富涨停池（与其他模块的 ak.stock_zt_pool_em(date=) 调用同一缓存）。"""
    func, kw = with_timeout(ak.stock_zt_pool_em), {"date": date_str}
    d = dt.datetime.strptime(date_str, "%Y%m%d").date()
    if not zt_window_ok(d):
        # 超出回查范围：不再请求（只会得到空表）。以前在范围内取到并缓存过的照常使用（直接读，避免 --force 时被空表覆盖）
        res = classify_pool(_read_cache(ctx, func, (), kw))
        return res if res.status in ("ok", "empty") else ZtPool(None, "out_of_window")
    try:
        return classify_pool(fetch(ctx, "ex", func, **kw))
    except Exception as e:  # noqa: BLE001
        ctx.log.info("[sectors] %s 涨停池获取失败：%s", date_str, e)
        return ZtPool(None, "failed", reason(e))


# ---------------------------------------------------------------- 行业历史（按日期查询）

def _span(ctx, dates: list[str]) -> tuple[str, str]:
    """历史 K 线的请求区间：从最早日期的上一个交易日再往前几天开始（用来算第一天的涨跌幅，长假也覆盖）。"""
    first = dt.date.fromisoformat(dates[0])
    prev = tc.prev_trade_day(first) or first - dt.timedelta(days=20)
    start = prev - dt.timedelta(days=5)
    return start.strftime("%Y%m%d"), ctx.date_str


def industry_history(ctx, src: str, board: dict, dates: list[str]) -> dict:
    """单个行业在 dates 各日的涨跌幅 {date_iso: pct}。src=ths 用名称，src=em 用 BK 代码。"""
    start, end = _span(ctx, dates)
    if src == "ths":
        df = fetch(ctx, "ths", with_timeout(ak.stock_board_industry_index_ths),
                   symbol=board["name"], start_date=start, end_date=end)
        if df is None or df.empty:
            return {}
        d = df.assign(date=pd.to_datetime(df["日期"]).dt.strftime("%Y-%m-%d"))
        d = d[d["date"] <= dates[-1]].sort_values("date")
        d = d.assign(pct=calc.to_num(d["收盘价"]).pct_change() * 100)
    elif src == "em":
        code = str(board.get("code") or "")
        if not code.startswith("BK"):
            return {}
        df = fetch(ctx, "emh", with_timeout(ak.stock_board_industry_hist_em),
                   symbol=code, start_date=start, end_date=end, period="日k", adjust="")
        if df is None or df.empty:
            return {}
        d = df.assign(date=pd.to_datetime(df["日期"]).dt.strftime("%Y-%m-%d"), pct=calc.to_num(df["涨跌幅"]))
    else:
        return {}
    d = d[d["date"].isin(dates) & d["pct"].notna()]
    return dict(zip(d["date"], d["pct"].astype(float)))


def ths_industry_names(ctx) -> set[str]:
    """同花顺行业名称表（补历史前先确认名称存在，避免无效请求）。"""
    df = fetch(ctx, "ths", with_timeout(ak.stock_board_industry_name_ths))
    return set(df["name"].astype(str)) if df is not None and not df.empty else set()
