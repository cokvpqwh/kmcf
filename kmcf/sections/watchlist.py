"""自选股/持仓：自选股当日表现、均线位置与状态标签，持仓盈亏，近 60 日 K 线。

数据源（主 → 备）：
- 日K：东方财富 stock_zh_a_hist（push2his，前复权，可按日期查询）→ 腾讯证券日K（_watchlist_src.tx_stock_daily）
- 涨停/炸板/跌停：东方财富涨停板行情（push2ex，按日期）→ 按日K收盘价与涨跌停价推算
- 龙虎榜：优先复用“龙虎榜与资金”模块整理好的结果（ctx.shared['funds.lhb']）；
  单独运行时用与该模块完全相同的调用（东方财富数据中心 em_lhb_detail + merge_lhb，共享缓存、口径一致）
- 名称补全：配置 → 行情数据自带 → 涨停池/龙虎榜 → 新浪行情 → 腾讯行情（统一去掉简称里的空格）
- 个股资金排行：读取“龙虎榜与资金”模块的 ctx.shared['funds.flow_rank']，自选股进入净流入/净流出前 N 时客观注明名次；
  没有该数据（如单独运行本模块）时不显示
某个站点已连接失败，就在 ctx.shared 记标记（push2his_down / push2ex_down / tx_down），后续直接跳过；
其中 push2his_down 会跨次生成保留 3 小时（见 report.PERSIST_SITE_FLAGS），其余只在本次生成内有效。
只陈述客观数据，不给出任何操作建议。
"""

import datetime as dt
from pathlib import Path

import akshare as ak
import pandas as pd
import requests

from .. import trade_calendar as tc
from ..config import normalize_item
from ..context import Context
from ..fetch import FetchError, cache_key
from ..section import Section, col, fmt_pct, tone_of
from . import _funds_calc as funds_calc
from . import _funds_data as funds_data
from . import _watchlist_calc as calc
from . import _watchlist_src as src
from ._names import clean_stock_name
from ._site import skip_reason

ID = "watchlist"
TITLE = "自选股/持仓"

EM_DOWN = "push2his_down"   # ctx.shared 标记：东方财富日K接口已失败，后续直接用备用源（跨次生成保留 3 小时）
EX_DOWN = "push2ex_down"    # ctx.shared 标记：涨停板行情接口（push2ex）本次已失败，跳过并改用日K推算
TX_DOWN = "tx_down"         # ctx.shared 标记：腾讯证券日K接口本次已连接失败，后续股票不再请求
EM_TIMEOUT = 15             # 东方财富日K单次请求超时（秒）
POOL_TIMEOUT = 30           # 涨停板行情（akshare 未设超时）的总超时（秒）
HIST_DAYS = 130             # 取日K的交易日数（够算 MA60、60日区间，并画带 MA20 的 60 日 K 线）
ACTUAL_DAYS = 8             # 历史日期时额外取的不复权日K天数（修正收盘价、推算涨跌停）
MAX_KLINE_CHARTS = 8
SRC_NAMES = {"em": "东方财富", "tx": "腾讯证券"}
POOL_NAMES = {"zt": "涨停池", "zb": "炸板池", "dt": "跌停池"}
POOL_WHY = {"too_old": "只提供最近30天的数据", "failed": "暂时无法获取",
            "empty": "该日无数据", "format": "返回格式无法识别"}
FUNDS_LHB = "funds.lhb"     # ctx.shared：“龙虎榜与资金”模块整理好的龙虎榜（每只股票一行）
FUNDS_FLOW_RANK = "funds.flow_rank"   # ctx.shared：“龙虎榜与资金”模块的个股资金排行（净流入/净流出前 N）
LIMIT_RULE = ("按日K收盘价/最高价与涨跌停价推算（主板10%，主板ST股2025-07-07起同为10%、此前为5%；"
              "创业板/科创板20%；北交所30%）")
EMPTY_TIP = "还没有添加自选股，可在软件的『自选股/持仓』里添加股票代码。"   # 操作提示只保留这一句
NO_HOLD_TIP = "未填写持股数和成本价，不计算持仓盈亏。"


def build(ctx: Context) -> Section:
    sec = Section(ID, TITLE)
    stocks = _load_items(ctx.cfg)
    if not stocks:
        sec.note(EMPTY_TIP)
        return sec

    start = ctx.recent_trade_days(HIST_DAYS)[0].strftime("%Y%m%d")
    failed = []
    for st in stocks:
        with sec.guard(f"{st['label']} 日K"):
            try:
                raw, st["src"] = _fetch_kline(ctx, st["code"], start, "qfq")
            except FetchError as e:   # 接口失败：先记下，最后合并成一条错误
                ctx.log.warning("[%s] %s 日K获取失败：%s", ID, st["code"], e)
                failed.append(st["label"])
                continue
            st["k"] = calc.normalize_kline(raw, ctx.trade_date)
            st["kname"] = calc.kline_name(raw)
    if failed:
        sec.error(f"日K获取失败（{_kline_fail_reason(ctx)}）：{'、'.join(failed)}")

    status = _load_status(ctx, sec)
    with sec.guard("名称补全"):
        _fill_names(ctx, stocks, status)
    status["flow_rank"] = ctx.shared.get(FUNDS_FLOW_RANK)
    status["flow_hits"] = {}
    with sec.guard("资金排行"):
        status["flow_hits"] = calc.flow_rank_hits(stocks, status["flow_rank"])

    history_mode = ctx.trade_date < tc.latest_completed_trade_day()   # 复盘的不是最近一个交易日
    check_actual = _need_actual_check(ctx.trade_date, history_mode, dt.date.today())
    for st in stocks:
        with sec.guard(f"{st['label']} 指标"):
            _compute(ctx, st, status, check_actual)

    rows, tot = calc.holdings(stocks)
    has_hold = tot["count"] > 0

    with sec.guard("要点"):
        flow_line = calc.flow_rank_line(stocks, status["flow_hits"], status["flow_rank"])
        for line in calc.summary_lines(stocks, tot if has_hold else None, status["lhb_state"], flow_line):
            sec.add_summary(line)
    with sec.guard("概览指标"):
        _kpis(sec, stocks, tot if has_hold else None)
    with sec.guard("自选股总览"):
        _overview_table(sec, stocks)
    if has_hold:
        with sec.guard("持仓明细"):
            _holding_table(sec, rows)
    with sec.guard("龙虎榜上榜"):
        _lhb_table(sec, stocks, status)
    with sec.guard("当日涨跌幅图"):
        _pct_chart(sec, stocks, rows, has_hold)
    if has_hold and tot["mv"] is not None:
        with sec.guard("持仓走势"):
            _holding_trend(ctx, sec, tot, history_mode)
    with sec.guard("K线图"):
        _kline_charts(sec, stocks, rows)

    _notes(ctx, sec, stocks, status, tot, has_hold, history_mode)
    return sec


# ---------------------------------------------------------------- 配置

def _load_items(cfg: dict) -> list[dict]:
    """读取并规整自选股配置，去重，生成每只股票的工作字典。"""
    out, seen = [], set()
    empty = calc.normalize_kline(None, dt.date.min)
    for raw in (cfg or {}).get("watchlist") or []:
        it = normalize_item(raw)
        if not it or it["code"] in seen:
            continue
        seen.add(it["code"])
        name = clean_stock_name(it["name"])
        out.append({**it, "name": name, "label": name or it["code"], "k": empty, "kname": "", "src": None,
                    "m": calc.compute_metrics(empty, dt.date.min), "labels": [], "close": None,
                    "prev_close": None, "pct": None, "traded": False, "adjusted": False})
    return out


# ---------------------------------------------------------------- 数据获取

def _has_cache(ctx: Context, func, kwargs: dict, volatile: bool) -> bool:
    """ctx.fetch 这次调用会不会直接命中磁盘缓存（用来优先使用已缓存的数据源）。"""
    f = ctx.fetcher
    if getattr(f, "force", False) or (volatile and getattr(f, "same_day", False)):
        return False
    raw_dir = getattr(f, "raw_dir", None)
    return raw_dir is not None and (Path(raw_dir) / f"{cache_key(func, (), kwargs)}.pkl").exists()


def _is_network_error(e: BaseException) -> bool:
    """FetchError 的根因是不是连接/超时类网络错误（而不是某只股票的数据问题）。"""
    cause = e.__cause__ or e
    return isinstance(cause, (requests.RequestException, TimeoutError, ConnectionError))


def _fetch_kline(ctx: Context, code: str, start: str, adjust: str) -> tuple[pd.DataFrame, str]:
    """日K：东方财富优先，失败/无数据时用腾讯证券。复盘日当晚 8 点前不读缓存（盘后数据可能修正）。"""
    em_kw = dict(symbol=code, period="daily", start_date=start, end_date=ctx.date_str, adjust=adjust,
                 timeout=EM_TIMEOUT)
    tx_kw = dict(symbol=code, start_date=start, end_date=ctx.date_str, adjust=adjust)
    use_em = src.em_supported(code)

    # 已缓存的源优先：重复生成时结果一致，也不必再碰已知失败的接口
    if use_em and _has_cache(ctx, ak.stock_zh_a_hist, em_kw, True):
        df = ctx.fetch(ak.stock_zh_a_hist, volatile=True, **em_kw)
        if len(df):
            return df, "em"
    if _has_cache(ctx, src.tx_stock_daily, tx_kw, True):
        return ctx.fetch(src.tx_stock_daily, volatile=True, **tx_kw), "tx"

    if use_em and not ctx.shared.get(EM_DOWN):
        try:
            df = ctx.fetch(ak.stock_zh_a_hist, volatile=True, **em_kw)
            if len(df):
                return df, "em"
        except FetchError as e:
            ctx.shared[EM_DOWN] = True
            ctx.log.warning("[%s] 东方财富日K不可用，切换腾讯证券：%s", ID, e)
    if ctx.shared.get(TX_DOWN):
        raise FetchError(f"腾讯证券日K{skip_reason(TX_DOWN)}")
    try:
        return ctx.fetch(src.tx_stock_daily, volatile=True, **tx_kw), "tx"
    except FetchError as e:
        if _is_network_error(e):
            ctx.shared[TX_DOWN] = True
        raise


def _kline_fail_reason(ctx: Context) -> str:
    em, tx = ctx.shared.get(EM_DOWN), ctx.shared.get(TX_DOWN)
    if em and tx:   # 站点跳过等运行时状态只写日志，报告里只写“暂时无法访问”
        return "东方财富与腾讯证券日K接口暂时无法访问"
    if tx:
        return "腾讯证券日K接口暂时无法访问"
    return "数据接口返回异常"


def _load_status(ctx: Context, sec: Section) -> dict:
    """涨停/炸板/跌停池与龙虎榜。

    池为 None 表示不可用（改用日K推算），why[key] 记原因：too_old / failed / empty / format。
    lhb_state：ok / empty（未发布）/ failed / format。
    """
    out = {"zt": None, "zb": None, "dt": None, "why": {}, "lhb": None, "lhb_codes": set(), "lhb_state": "ok"}
    date = ctx.date_str
    recent = (dt.date.today() - ctx.trade_date).days < 30   # 炸板/跌停池只提供最近 30 天
    for key, func in (("zt", ak.stock_zt_pool_em), ("zb", ak.stock_zt_pool_zbgc_em),
                      ("dt", ak.stock_zt_pool_dtgc_em)):
        kw = {"date": date}
        if not _has_cache(ctx, func, kw, False):
            if key != "zt" and not recent:
                out["why"][key] = "too_old"
                continue
            if ctx.shared.get(EX_DOWN):
                out["why"][key] = "failed"
                continue
        try:
            df = ctx.fetch(src.with_timeout(func, POOL_TIMEOUT), **kw)
        except FetchError as e:
            ctx.shared[EX_DOWN] = True
            out["why"][key] = "failed"
            ctx.log.warning("[%s] %s 获取失败，改用日K推算：%s", ID, POOL_NAMES[key], e)
            continue
        if key == "zt" and len(df) == 0:   # 交易日涨停池为空基本不可能，视为数据不可用
            out["why"][key] = "empty"
            continue
        out[key] = calc.pool_codes(df)
        if out[key] is None:
            out["why"][key] = "format"

    _load_lhb(ctx, sec, out)
    return out


def _load_lhb(ctx: Context, sec: Section, out: dict) -> None:
    shared = ctx.shared.get(FUNDS_LHB)   # “龙虎榜与资金”模块先运行时已取好，直接复用
    if isinstance(shared, pd.DataFrame):
        merged = shared
    else:
        kw = dict(date=ctx.date_iso)     # 与“龙虎榜与资金”模块完全相同的调用（共享缓存）
        try:
            raw = ctx.fetch(funds_data.em_lhb_detail, volatile=True, **kw)
        except FetchError as e:
            ctx.log.warning("[%s] 龙虎榜获取失败：%s", ID, e)
            sec.error("龙虎榜数据获取失败，未标注“上龙虎榜”")
            out["lhb_state"] = "failed"
            return
        if raw is None or not len(raw):   # 还没发布：删掉空结果的缓存，下次生成重新请求
            _drop_cache(ctx, funds_data.em_lhb_detail, kw)
        try:
            merged = funds_calc.merge_lhb(raw)
        except Exception as e:  # noqa: BLE001  接口字段变化
            ctx.log.warning("[%s] 龙虎榜整理失败：%s", ID, e)
            merged = None

    lhb = calc.normalize_lhb(merged) if merged is not None else None
    if lhb is None:
        sec.error("龙虎榜数据格式无法识别，未标注“上龙虎榜”")
        out["lhb_state"] = "format"
        return
    out["lhb"] = lhb
    out["lhb_codes"] = set(lhb["code"])
    if not len(lhb):
        out["lhb_state"] = "empty"


def _drop_cache(ctx: Context, func, kwargs: dict) -> None:
    raw_dir = getattr(ctx.fetcher, "raw_dir", None)
    if raw_dir is not None:
        try:
            (Path(raw_dir) / f"{cache_key(func, (), kwargs)}.pkl").unlink(missing_ok=True)
        except OSError:
            pass


def _fill_names(ctx: Context, stocks: list[dict], status: dict) -> None:
    """名称缺失时补全：行情数据自带 → 涨停池/龙虎榜 → 新浪行情 → 腾讯行情。"""
    known = {}
    for key in ("zt", "zb", "dt"):
        for code, r in (status.get(key) or {}).items():
            name = clean_stock_name(r.get("名称"))
            if name:
                known.setdefault(code, name)
    lhb = status.get("lhb")
    if lhb is not None and len(lhb):
        for code, name in zip(lhb["code"], lhb["name"]):
            name = clean_stock_name(name)
            if name:
                known.setdefault(code, name)
    for st in stocks:
        if not st["name"]:
            st["name"] = clean_stock_name(st["kname"]) or known.get(st["code"], "")

    missing = [st["code"] for st in stocks if not st["name"]]
    for fn in (src.sina_names, src.tx_names):
        if not missing:
            break
        if fn is src.tx_names and ctx.shared.get(TX_DOWN):
            continue
        try:
            df = ctx.fetch(fn, codes=",".join(missing))
            if not isinstance(df, pd.DataFrame) or not {"code", "name"} <= set(df.columns):
                raise ValueError(f"返回列 {list(getattr(df, 'columns', []))} 无法识别")
        except Exception as e:  # noqa: BLE001  一个源失败或格式变化就换下一个源
            ctx.log.warning("[%s] 名称补全（%s）失败：%s", ID, fn.__name__, e)
            continue
        names = dict(zip(calc.norm_codes(df["code"]), df["name"].map(clean_stock_name)))
        for st in stocks:
            if not st["name"] and names.get(st["code"]):
                st["name"] = names[st["code"]]
        missing = [st["code"] for st in stocks if not st["name"]]
    for st in stocks:
        st["label"] = st["name"] or st["code"]


def _actual_kline(ctx: Context, code: str) -> pd.DataFrame:
    """复盘日前后几天的不复权日K（历史日期时，前复权价会因之后的分红送转偏离当时的实际价格）。"""
    start = ctx.recent_trade_days(ACTUAL_DAYS)[0].strftime("%Y%m%d")
    try:
        raw, _ = _fetch_kline(ctx, code, start, "")
    except FetchError as e:
        ctx.log.warning("[%s] %s 不复权日K获取失败：%s", ID, code, e)
        raw = None
    return calc.normalize_kline(raw, ctx.trade_date)


# ---------------------------------------------------------------- 计算

def _need_actual_check(trade_date: dt.date, history_mode: bool, today: dt.date) -> bool:
    """是否需要用不复权价核对复盘日价格。复盘日之后出现过交易日（除权除息只发生在交易日），
    前复权价才可能偏离当时的实际价格；周末/节假日复盘最近一个交易日时不需要，省掉一倍请求。"""
    return history_mode or (trade_date < today and tc.is_trade_day(today))


def _compute(ctx: Context, st: dict, status: dict, check_actual: bool) -> None:
    k, code = st["k"], st["code"]
    m = calc.compute_metrics(k, ctx.trade_date)
    st["m"] = m
    st.update(close=m["close"], prev_close=m["prev_close"], pct=m["pct"], traded=m["traded"])

    k_limit = k   # 推算涨跌停用的日K：复盘日之后有分红送转时改用不复权价
    if check_actual and m["traded"] and m["close"]:
        k_act = _actual_kline(ctx, code)
        fix = calc.actual_fix(k, k_act, ctx.trade_date)
        if fix:
            st.update(close=fix["close"], pct=fix["pct"], prev_close=fix["prev_close"], adjusted=True)
            if fix["exact"]:
                k_limit = k_act

    inferred = calc.infer_limit(k_limit, ctx.trade_date, code, st["name"])
    is_st = "ST" in st["name"].upper()   # 涨停板行情的各个池不含 ST 股，ST 股一律加上日K推算

    def pick(key):
        pool = status.get(key)
        if pool is None:
            return inferred[key], None
        rec = pool.get(code)
        return (rec is not None) or (is_st and inferred[key]), rec

    zt_hit, zt_rec = pick("zt")
    zb_hit, _ = pick("zb")
    dt_hit, _ = pick("dt")
    zt_arg = (zt_rec if zt_rec is not None else {}) if zt_hit else None
    m_view = {**m, "pct": st["pct"]}
    st["labels"] = calc.status_labels(m_view, zt_arg, zb_hit and not zt_hit, dt_hit, code in status["lhb_codes"])
    hit = (status.get("flow_hits") or {}).get(code)
    if hit and m["traded"]:
        st["labels"].append(calc.flow_rank_label(hit))


# ---------------------------------------------------------------- 展示

def _kpis(sec: Section, stocks: list[dict], tot: dict | None) -> None:
    if tot and tot["mv"] is not None:
        sec.kpi("持仓市值", calc.money(tot["mv"]), f"{tot['count']}只持仓")
        sec.kpi("持仓当日盈亏", calc.money(tot["day_pnl"], signed=True), fmt_pct(tot["day_pct"]), tone_of(tot["day_pnl"]))
        if tot["float_pnl"] is not None:
            sec.kpi("累计浮动盈亏", calc.money(tot["float_pnl"], signed=True), "收益率" + fmt_pct(tot["float_pct"]),
                    tone_of(tot["float_pnl"]))
        sec.kpi("持仓数量", f"{tot['count']}只", f"当日盈利{tot['up']} · 亏损{tot['down']}")
    b = calc.breadth(stocks)
    sub = f"共{b['total']}只" + (f" · 平{b['flat']}" if b["flat"] else "") + \
          (f" · 停牌/无数据{len(b['halt'])}" if b["halt"] else "") + \
          (f" · 涨跌幅缺失{len(b['nopct'])}" if b["nopct"] else "")
    sec.kpi("自选股涨 / 跌", f"{b['up']} / {b['down']}", sub)
    if b["avg"] is not None:
        sec.kpi("等权平均涨跌幅", fmt_pct(b["avg"]), f"{b['traded']}只有行情", tone_of(b["avg"]))
    if b["traded"] >= 2 and not b["tie"]:
        sec.kpi("最强", b["best"]["label"], fmt_pct(b["best"]["pct"]), tone_of(b["best"]["pct"]))
        sec.kpi("最弱", b["worst"]["label"], fmt_pct(b["worst"]["pct"]), tone_of(b["worst"]["pct"]))


def _yuan_cells(sec: Section, keys: tuple[str, ...]) -> None:
    """刚加入的表格里，金额不足 1 万时显示的是裸数字（如 +721），补上“元”。"""
    block = sec.blocks[-1]
    idx = [i for i, c in enumerate(block["columns"]) if c["key"] in keys]
    for row in block["rows"]:
        for i in idx:
            text = row[i]["text"]
            if text and text[-1].isdigit():
                row[i]["text"] = text + "元"


def _overview_table(sec: Section, stocks: list[dict]) -> None:
    rows = []
    for st in stocks:
        m = st["m"]
        rows.append({
            "name": st["label"], "code": st["code"], "close": st["close"], "pct": st["pct"],
            "amount": m["amount"], "turnover": m["turnover"], "vol_ratio": m["vol_ratio"],
            "ma_text": m["ma_text"], "dist20": m["dist"][20], "dist60": m["dist"][60], "chg20": m["chg20"],
            "pos60": m["pos60"], "status": "、".join(st["labels"]), "note": st["note"],
        })
    cols = [col("name", "名称"), col("code", "代码", "code"), col("close", "收盘价", "price"),
            col("pct", "涨跌幅", "pct"), col("status", "当日状态"), col("amount", "成交额", "amount"),
            col("turnover", "换手率", "pct_plain"), col("vol_ratio", "量比", "num"),
            col("ma_text", "均线位置(5/10/20/60)"), col("dist20", "距20日线", "pct"), col("dist60", "距60日线", "pct"),
            col("chg20", "近20日", "pct"), col("pos60", "60日位置", "pct_plain")]
    if any(st["note"] for st in stocks):
        cols.append(col("note", "备注"))
    sec.table("自选股总览", cols, rows, note="“距N日线”为收盘价相对N日均线的偏离，正值在均线上方；"
                                         "上市不足60个交易日的股票不计算“60日位置”。")
    _yuan_cells(sec, ("amount",))


def _holding_table(sec: Section, rows: list[dict]) -> None:
    cols = [col("name", "名称"), col("code", "代码", "code"), col("shares", "股数", "int"),
            col("cost", "成本价", "price"), col("close", "现价", "price"), col("mv", "市值", "amount"),
            col("day_pct", "当日涨跌", "pct"), col("day_pnl", "当日盈亏", "flow"), col("float_pnl", "浮动盈亏", "flow"),
            col("float_pct", "盈亏比例", "pct"), col("weight", "仓位占比", "pct_plain")]
    sec.table("持仓明细", cols, rows, note="按市值从大到小排列；仓位占比为占持仓总市值的比例（不含现金）。")
    _yuan_cells(sec, ("mv", "day_pnl", "float_pnl"))


def _lhb_table(sec: Section, stocks: list[dict], status: dict) -> None:
    lhb = status.get("lhb")
    codes = {st["code"]: st["label"] for st in stocks}
    if lhb is None or not len(lhb) or not (set(codes) & status["lhb_codes"]):
        return
    hit = lhb[lhb["code"].isin(list(codes))].drop_duplicates("code")
    rows = [{**r, "name": codes[r["code"]]} for r in hit.to_dict("records")]
    cols = [col("name", "名称"), col("code", "代码", "code"), col("pct", "涨跌幅", "pct"),
            col("net", "龙虎榜净买额", "flow"), col("buy", "买入额", "amount"), col("sell", "卖出额", "amount"),
            col("ratio", "净买额占成交比", "pct"), col("basis", "金额口径"), col("reason", "上榜原因")]
    if any(isinstance(r.get("explain"), str) and r["explain"].strip() for r in rows):
        cols.append(col("explain", "解读"))
    sec.table("自选股龙虎榜", cols, rows,
              note="来源：东方财富数据中心龙虎榜，与“龙虎榜与资金”板块口径一致。一只股票因多个原因上榜时合并为一行，"
                   "金额优先取“当日”口径；只因“连续三个交易日”等多日条件上榜的，金额为多日累计（见“金额口径”列）。")
    _yuan_cells(sec, ("net", "buy", "sell"))


def _pct_chart(sec: Section, stocks: list[dict], rows: list[dict], has_hold: bool) -> None:
    entries = [(st["label"], st["pct"]) for st in stocks if st["traded"] and st["pct"] is not None]
    valued = [r for r in rows if r.get("mv")]
    pie = has_hold and len(valued) >= 2
    if entries:
        sec.chart("当日涨跌幅", calc.pct_bar_option(entries), height=calc.pct_bar_height(len(entries)),
                  span="half" if pie else "full")
    if pie:
        sec.chart("持仓市值占比", calc.weight_pie_option(valued), height=max(260, calc.pct_bar_height(len(entries))),
                  span="half")


def _holding_trend(ctx: Context, sec: Section, tot: dict, history_mode: bool) -> None:
    if not history_mode:   # 只记录最新交易日，避免用当前持仓覆盖历史记录
        rec = {"持仓市值": round(tot["mv"], 2)}
        if tot["day_pnl"] is not None:
            rec["持仓当日盈亏"] = round(tot["day_pnl"], 2)
        if tot["float_pnl"] is not None:
            rec["持仓浮动盈亏"] = round(tot["float_pnl"], 2)
        ctx.history.upsert(ctx.date_iso, rec)
    opt = calc.trend_option(ctx.history.recent(ctx.date_iso, 60))
    if opt:
        sec.chart("持仓市值与当日盈亏走势", opt, height=280,
                  note="来自每日复盘时记录的持仓数据；期间调整过持仓时市值会跳变。")


def _kline_charts(sec: Section, stocks: list[dict], rows: list[dict]) -> None:
    order = {r["code"]: i for i, r in enumerate(rows)}   # 持仓按市值优先，其余按自选顺序
    cands = sorted([st for st in stocks if len(st["k"])],
                   key=lambda st: (0, order[st["code"]]) if st["code"] in order else (1, 0))
    for st in cands[:MAX_KLINE_CHARTS]:
        cost = st["cost"] if st["shares"] > 0 and st["cost"] > 0 and not st["adjusted"] else None
        title = f"{st['label']}（{st['code']}）"
        if not st["traded"]:
            title += "停牌/无数据"
        elif st["pct"] is not None:
            title += fmt_pct(st["pct"])
        sec.chart(title, calc.kline_option(st["k"], cost=cost), height=340, span="half")
    if len(cands) > MAX_KLINE_CHARTS:
        sec.note(f"有行情的自选股共{len(cands)}只，K线图只画前{MAX_KLINE_CHARTS}只（持仓优先，其余按自选顺序）。")


def _limit_note(status: dict) -> str:
    """涨停/炸板/跌停标签的来源说明：按每个池不可用的原因分别写明。"""
    why = status.get("why", {})
    down = [k for k in ("zt", "zb", "dt") if status.get(k) is None]
    groups = {}
    for k in down:
        groups.setdefault(why.get(k, "failed"), []).append(POOL_NAMES[k])
    reasons = "；".join(f"{'、'.join(names)}{POOL_WHY[r]}" for r, names in groups.items())
    if len(down) == 3:
        return f"涨停/炸板/跌停标签{LIMIT_RULE}：东方财富涨停板行情{reasons}。"
    if down:
        return (f"涨停/炸板/跌停标签来自东方财富涨停板行情；其中{reasons}，相应标签{LIMIT_RULE}；"
                f"涨停板行情不含ST股，ST股另按涨跌停价推算。")
    return f"涨停/炸板/跌停标签来自东方财富涨停板行情；涨停板行情不含ST股，ST股{LIMIT_RULE}。"


def _notes(ctx: Context, sec: Section, stocks: list[dict], status: dict, tot: dict, has_hold: bool,
           history_mode: bool) -> None:
    nodata = [st["label"] for st in stocks if not len(st["k"]) and st["src"]]
    if nodata:
        sec.note(f"{'、'.join(nodata)}在复盘日之前约半年内没有行情数据（代码有误、长期停牌或尚未上市）。")
    nopct = [st["label"] for st in stocks if st["traded"] and st["pct"] is None]
    if nopct:
        sec.note(f"{'、'.join(nopct)}复盘日有行情但没有前一交易日收盘价（如上市首日），涨跌幅和当日盈亏不计算。")
    if not has_hold:
        sec.note(NO_HOLD_TIP)
    else:
        sec.note("当日盈亏 = 持股数 × (收盘价 − 昨收)，浮动盈亏 = 持股数 × (收盘价 − 成本价)；"
                 "停牌股按最近收盘价估值；未计手续费、税费和分红。")
        if tot["no_cost"]:
            sec.note(f"{'、'.join(tot['no_cost'])}未填写成本价，浮动盈亏未计入。")
        if history_mode:
            sec.note("复盘的是历史交易日：持仓按当前配置的持股数和成本价计算。")
    if any(st["adjusted"] for st in stocks):
        sec.note("部分股票在复盘日之后有分红送转：收盘价、涨跌幅和盈亏按当时的实际（不复权）价格计算，"
                 "K线图、均线和区间位置为前复权价格。")

    used = {}
    for st in stocks:
        if st["src"] and len(st["k"]):
            used[st["src"]] = used.get(st["src"], 0) + 1
    if used:
        if set(used) == {"em"}:
            text = "日K来自东方财富（前复权）"
        else:
            text = "日K来自" + "、".join(f"{SRC_NAMES[s]}{n}只" for s, n in used.items()) + "（前复权）"
            text += (f"；东方财富日K接口{skip_reason(EM_DOWN)}，已改用腾讯证券" if ctx.shared.get(EM_DOWN)
                     else "；东方财富日K接口不可用时改用腾讯证券")
        sec.note(text + "。均线、量比、区间位置均按前复权日K计算。")

    sec.note(_limit_note(status))
    rank = status.get("flow_rank")
    if status.get("flow_hits") and isinstance(rank, dict):
        sec.note(f"“{rank.get('word_in') or '主力净流入'}第N / {rank.get('word_out') or '主力净流出'}第N”标签：来自“龙虎榜与资金”板块的"
                 f"个股资金排行（{rank.get('source') or '—'}，{rank.get('basis') or '口径见该板块说明'}），"
                 f"只在自选股进入净流入/净流出前{rank.get('top_n') or 'N'}名时标注；排行为收盘后的快照。")
    if status["lhb_state"] == "empty":
        sec.note("复盘日龙虎榜暂无数据（龙虎榜一般在收盘后17:00~18:30陆续发布），未核对上榜情况。")
    sec.note("量比 = 当日成交量 ÷ 前5个交易日平均成交量（与行情软件的分时量比口径不同）；放量指量比≥2，缩量指量比≤0.5。")
    sec.note("60日位置 = (收盘价 − 近60日最低价) ÷ (近60日最高价 − 近60日最低价)；创60日新高/新低指当日最高/最低价"
             "达到此前59个交易日的最高/最低；上穿/跌破20日线指前一日收盘在20日线下方/上方、当日收盘在其上方/下方。")
    if any(src.market_of(st["code"]) == "bj" for st in stocks):
        sec.note("北交所股票涨跌幅限制为30%。")
