"""自选股模块的纯计算（不联网，便于单元测试）。

输入是标准化后的日K（英文列名），输出是可直接放进表格/图表的数值与文字。
只陈述客观数据，不做任何买卖建议。
"""

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

import numpy as np
import pandas as pd

from ..section import ACCENT, DOWN, FLAT, SERIES, UP, fmt_amount, fmt_pct, num
from ._names import clean_stock_name
from ._watchlist_src import limit_ratios

MA_WINDOWS = (5, 10, 20, 60)
CHART_MAS = (5, 10, 20)
CHART_DAYS = 60
VOL_UP = 2.0      # 放量：量比 ≥ 2
VOL_DOWN = 0.5    # 缩量：量比 ≤ 0.5
NEW_LISTING_ROWS = 5  # 窗口内 K 线不超过 5 根，视为新股/长期停牌复牌，不推算涨跌停

KCOLS = ["date", "open", "close", "high", "low", "volume", "amount", "pct", "turnover"]
_COLMAP = {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low",
           "成交量": "volume", "成交额": "amount", "涨跌幅": "pct", "换手率": "turnover"}


# ---------------------------------------------------------------- 日K 标准化

def normalize_kline(raw: pd.DataFrame | None, trade_date: dt.date) -> pd.DataFrame:
    """stock_zh_a_hist / 腾讯备用源 的中文列 → 英文列；只保留复盘日（含）之前的数据。"""
    if raw is None or len(raw) == 0 or "日期" not in raw.columns or "收盘" not in raw.columns:
        return pd.DataFrame(columns=KCOLS)
    df = raw.rename(columns=_COLMAP)
    df = df.assign(**{c: np.nan for c in KCOLS if c not in df.columns})[KCOLS]
    df = df.assign(date=pd.to_datetime(df["date"], errors="coerce").dt.date)
    df = df.assign(**{c: pd.to_numeric(df[c], errors="coerce") for c in KCOLS[1:]})
    df = df.dropna(subset=["date", "close"])
    df = df[df["date"] <= trade_date]
    df = df.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    calc_pct = df["close"].pct_change() * 100
    return df.assign(pct=df["pct"].fillna(calc_pct))


def kline_name(raw: pd.DataFrame | None) -> str:
    """备用源附带的股票简称（没有则返回空串）。"""
    if raw is None or len(raw) == 0 or "名称" not in raw.columns:
        return ""
    s = [n for n in (clean_stock_name(x) for x in raw["名称"]) if n]
    return s[-1] if s else ""


# ---------------------------------------------------------------- 指标

def _r(v, digits: int = 2):
    f = num(v)
    return None if f is None else round(f, digits)


def ma_text(close: float | None, mas: dict) -> str | None:
    """收盘价相对各均线的位置，如“站上全部均线”“站上5/10，低于20/60”。
    K 线不够算长均线（上市时间短）时只描述能算出的均线，并注明“不足N日”。"""
    if close is None:
        return None
    avail = [(n, v) for n, v in sorted(mas.items()) if v is not None]
    if not avail:
        return None
    missing = [n for n, v in sorted(mas.items()) if v is None]
    above = [str(n) for n, v in avail if close >= v]
    below = [str(n) for n, v in avail if close < v]
    if missing:
        parts = ([f"站上{'/'.join(above)}"] if above else []) + ([f"低于{'/'.join(below)}"] if below else [])
        return "，".join(parts) + f"（不足{missing[0]}日）"
    if not below:
        return "站上全部均线"
    if not above:
        return "低于全部均线"
    return f"站上{'/'.join(above)}，低于{'/'.join(below)}"


def compute_metrics(k: pd.DataFrame, trade_date: dt.date) -> dict:
    """单只股票在复盘日的各项指标。k 为 normalize_kline 的结果。"""
    m = {"has_data": len(k) > 0, "traded": False, "close": None, "prev_close": None, "pct": None,
         "amount": None, "turnover": None, "vol_ratio": None, "chg20": None, "pos60": None,
         "new_high60": False, "new_low60": False, "cross_up20": False, "cross_down20": False,
         "last_date": None, "ma": {n: None for n in MA_WINDOWS}, "dist": {n: None for n in MA_WINDOWS},
         "ma_text": None, "high": None, "low": None, "open": None}
    if k.empty:
        return m
    last = k.iloc[-1]
    close = num(last["close"])
    traded = last["date"] == trade_date
    m.update(traded=traded, close=close, last_date=last["date"])
    closes = k["close"]

    for n in MA_WINDOWS:
        if len(k) >= n:
            ma = float(closes.iloc[-n:].mean())
            m["ma"][n] = ma
            m["dist"][n] = (close / ma - 1) * 100 if ma else None
    m["ma_text"] = ma_text(close, m["ma"])
    if len(k) >= 21:
        base = num(closes.iloc[-21])
        m["chg20"] = (close / base - 1) * 100 if base else None
    if len(k) >= CHART_DAYS:   # 不足 60 根（上市时间短）时不算“60日位置”
        w = k.iloc[-CHART_DAYS:]
        hi, lo = num(w["high"].max()), num(w["low"].min())
        if hi is not None and lo is not None and hi > lo:
            m["pos60"] = min(100.0, max(0.0, (close - lo) / (hi - lo) * 100))

    if not traded:  # 停牌/无数据：当日不计涨跌
        m["prev_close"] = close
        return m

    m.update(amount=num(last["amount"]), turnover=num(last["turnover"]),
             high=num(last["high"]), low=num(last["low"]), open=num(last["open"]))
    # 昨收直接取上一根K线的收盘：前复权序列里它就是除权参考价，且不受源数据涨跌幅只保留 2 位小数的影响
    prev = num(closes.iloc[-2]) if len(k) >= 2 else None
    pct = num(last["pct"])
    if prev:
        m.update(prev_close=prev, pct=(close / prev - 1) * 100)
    elif pct is not None and pct > -100:   # 只有一根K线（如上市首日）：用源数据的涨跌幅反推
        m.update(prev_close=close / (1 + pct / 100), pct=pct)

    vol = num(last["volume"])
    prev5 = k["volume"].iloc[-6:-1]
    if vol is not None and len(prev5) == 5 and prev5.notna().all() and prev5.mean() > 0:
        m["vol_ratio"] = vol / float(prev5.mean())

    if len(k) >= CHART_DAYS:
        prev_hi = num(k["high"].iloc[-CHART_DAYS:-1].max())
        prev_lo = num(k["low"].iloc[-CHART_DAYS:-1].min())
        if m["high"] is not None and prev_hi is not None:
            m["new_high60"] = m["high"] >= prev_hi
        if m["low"] is not None and prev_lo is not None:
            m["new_low60"] = m["low"] <= prev_lo
    if len(k) >= 21 and m["ma"][20] is not None:
        prev_close = num(closes.iloc[-2])
        prev_ma20 = float(closes.iloc[-21:-1].mean())
        if prev_close is not None:
            m["cross_up20"] = prev_close < prev_ma20 and close > m["ma"][20]
            m["cross_down20"] = prev_close > prev_ma20 and close < m["ma"][20]
    return m


# ---------------------------------------------------------------- 涨跌停

def limit_price(prev_close: float, ratio: float, up: bool = True) -> float:
    """涨跌停价：昨收 × (1 ± 幅度)，四舍五入到分。"""
    base = Decimal(repr(float(prev_close)))
    r = Decimal(str(ratio))
    v = base * (1 + r if up else 1 - r)
    return float(v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def infer_limit(k: pd.DataFrame, trade_date: dt.date, code: str, name: str = "") -> dict:
    """按日K推算当日是否涨停/炸板/跌停（涨停池等接口不可用时的兜底）。"""
    out = {"zt": False, "zb": False, "dt": False}
    if len(k) < 2 or len(k) <= NEW_LISTING_ROWS or k.iloc[-1]["date"] != trade_date:
        return out
    prev, last = num(k.iloc[-2]["close"]), k.iloc[-1]
    close, high, low = num(last["close"]), num(last["high"]), num(last["low"])
    if not prev or close is None:
        return out
    eps = 0.005
    for ratio in limit_ratios(code, name, trade_date):
        up, dn = limit_price(prev, ratio, True), limit_price(prev, ratio, False)
        if abs(close - up) < eps:
            out["zt"] = True
        elif high is not None and high >= up - eps and close < up - eps:
            out["zb"] = True
        if abs(close - dn) < eps:
            out["dt"] = True
    if out["zt"]:
        out["zb"] = False
    return out


def ex_event(q0: float, q1: float, a0: float, a1: float, tol: float = 0.015) -> bool:
    """前一日/当日的 前复权价(q) 与 实际价(a) 之间的“差”和“比”是否都变了——变了说明当日是除权除息日。
    （前复权对现金分红多为减法调整，差不变；送转股为比例调整，比不变。）"""
    same_diff = abs((a1 - q1) - (a0 - q0)) <= tol
    same_ratio = a0 > 0 and a1 > 0 and q0 > 0 and abs((q1 / a1) / (q0 / a0) - 1) <= 2e-4
    return not (same_diff or same_ratio)


def actual_fix(kq: pd.DataFrame, ka: pd.DataFrame, trade_date: dt.date) -> dict | None:
    """历史日期：用不复权日K(ka)修正复盘日的收盘价/涨跌幅/昨收。前复权价与实际价一致时返回 None。

    exact=True 表示复盘日不是除权除息日，涨跌幅直接按实际收盘价计算（也可用 ka 推算涨跌停）。
    """
    if kq.empty or ka.empty or kq.iloc[-1]["date"] != trade_date or ka.iloc[-1]["date"] != trade_date:
        return None
    cq, ca = num(kq.iloc[-1]["close"]), num(ka.iloc[-1]["close"])
    if cq is None or ca is None or abs(ca - cq) <= 0.005:
        return None
    pq = num(kq.iloc[-2]["close"]) if len(kq) >= 2 else None
    pct = (cq / pq - 1) * 100 if pq else num(kq.iloc[-1]["pct"])   # 前复权口径的涨跌幅
    if len(ka) >= 2 and pq and ka.iloc[-2]["date"] == kq.iloc[-2]["date"]:
        pa = num(ka.iloc[-2]["close"])
        if pa and not ex_event(pq, cq, pa, ca):   # 复盘日不是除权除息日：昨收就是前一日实际收盘
            return {"close": ca, "pct": (ca / pa - 1) * 100, "prev_close": pa, "exact": True}
    prev = ca / (1 + pct / 100) if pct is not None and pct > -100 else None
    return {"close": ca, "pct": pct, "prev_close": prev, "exact": False}


def norm_codes(s: pd.Series) -> pd.Series:
    """代码列统一成 6 位字符串：兼容整数、带 NaN 的浮点（600001.0）、带市场前后缀（sh600001 / 600001.SH）。"""
    return s.astype(str).str.extract(r"(\d{1,6})")[0].str.zfill(6)


def pool_codes(df: pd.DataFrame | None) -> dict | None:
    """涨停/炸板/跌停池 → {代码: 行dict}。df 为 None 或认不出格式（缺“代码”列）返回 None，表示不可用。"""
    if df is None:
        return None
    if len(df) == 0:
        return {}
    if "代码" not in df.columns:
        return None
    d = df.assign(代码=norm_codes(df["代码"])).dropna(subset=["代码"])
    return {r["代码"]: r for r in d.to_dict("records")}


# “龙虎榜与资金”模块整理后的龙虎榜（_funds_calc.merge_lhb，每只股票一行）→ 本模块的列
LHB_COLS = ["code", "name", "reason", "pct", "net", "buy", "sell", "ratio", "basis", "explain"]
_LHB_MAP = {"code": "code", "name": "name", "reasons": "reason", "pct": "pct", "net": "net",
            "buy": "buy", "sell": "sell", "net_ratio": "ratio", "basis": "basis", "explain": "explain"}


def normalize_lhb(df: pd.DataFrame | None) -> pd.DataFrame | None:
    """龙虎榜 → 统一列 LHB_COLS（代码补齐 6 位）。空表返回空表；非空但认不出格式返回 None。"""
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=LHB_COLS)
    if "code" not in df.columns:
        return None
    out = df.rename(columns=_LHB_MAP)
    out = out.assign(**{c: None for c in LHB_COLS if c not in out.columns})[LHB_COLS]
    return out.assign(code=norm_codes(out["code"])).dropna(subset=["code"]).reset_index(drop=True)


def status_labels(m: dict, zt: dict | None, zb: bool, dt_: bool, lhb: bool) -> list[str]:
    """当日状态标签。zt：涨停池里的该股记录（含 连板数），或 {} 表示推算出的涨停，None 表示未涨停。"""
    if not m["has_data"]:
        return ["无数据"]
    if not m["traded"]:
        return ["停牌/无数据"]
    labels = [] if m["pct"] is not None else ["涨跌幅缺失"]
    if zt is not None:
        boards = int(num(zt.get("连板数"), 1) or 1)
        labels.append(f"{boards}连板" if boards >= 2 else "涨停")
    if zb:
        labels.append("炸板")
    if dt_:
        labels.append("跌停")
    if lhb:
        labels.append("上龙虎榜")
    if m["new_high60"]:
        labels.append("创60日新高")
    if m["new_low60"]:
        labels.append("创60日新低")
    if m["cross_up20"]:
        labels.append("上穿20日线")
    if m["cross_down20"]:
        labels.append("跌破20日线")
    vr = m["vol_ratio"]
    if vr is not None and vr >= VOL_UP:
        labels.append("放量")
    elif vr is not None and vr <= VOL_DOWN:
        labels.append("缩量")
    return labels


# ---------------------------------------------------------------- 持仓

def holdings(stocks: list[dict]) -> tuple[list[dict], dict]:
    """持仓明细与汇总。stocks 每项含 code/name/shares/cost/close/prev_close/traded。

    当日盈亏 = 股数 × (收盘 − 昨收)；浮动盈亏 = 股数 × (收盘 − 成本)。停牌按最近收盘价估值、当日盈亏为 0；
    有行情但缺昨收（如上市首日且无涨跌幅）时当日盈亏为 None（显示“—”，不计入合计）。
    """
    rows = []
    for s in stocks:
        shares = num(s.get("shares"), 0) or 0
        if shares <= 0:
            continue
        close, prev = num(s.get("close")), num(s.get("prev_close"))
        cost = num(s.get("cost"), 0) or 0
        row = {"name": _nm(s), "code": s["code"], "shares": shares,
               "cost": cost if cost > 0 else None, "close": close, "mv": None, "day_pnl": None,
               "day_pct": s.get("pct") if s.get("traded") else None,
               "float_pnl": None, "float_pct": None, "weight": None, "cost_amt": None}
        if close is not None:
            row["mv"] = shares * close
            if prev is not None:
                row["day_pnl"] = shares * (close - prev)
            elif not s.get("traded"):
                row["day_pnl"] = 0.0
            if cost > 0:
                row["cost_amt"] = shares * cost
                row["float_pnl"] = row["mv"] - row["cost_amt"]
                row["float_pct"] = (close / cost - 1) * 100
        rows.append(row)

    valued = [r for r in rows if r["mv"] is not None]
    total_mv = sum(r["mv"] for r in valued)
    with_day = [r for r in valued if r["day_pnl"] is not None]
    day_pnl = sum(r["day_pnl"] for r in with_day)
    with_cost = [r for r in valued if r["cost_amt"]]
    cost_amt = sum(r["cost_amt"] for r in with_cost)
    float_pnl = sum(r["float_pnl"] for r in with_cost)
    for r in valued:
        r["weight"] = r["mv"] / total_mv * 100 if total_mv > 0 else None
    prev_mv = sum(r["mv"] for r in with_day) - day_pnl
    tot = {
        "count": len(rows), "valued": len(valued), "mv": total_mv if valued else None,
        "day_pnl": day_pnl if with_day else None,
        "day_pct": day_pnl / prev_mv * 100 if with_day and prev_mv > 0 else None,
        "no_day": [r["name"] for r in valued if r["day_pnl"] is None],
        "float_pnl": float_pnl if with_cost else None,
        "float_pct": float_pnl / cost_amt * 100 if with_cost and cost_amt > 0 else None,
        "no_cost": [r["name"] for r in rows if r["cost"] is None],
        "up": sum(1 for r in valued if (num(r["day_pnl"]) or 0) > 0),
        "down": sum(1 for r in valued if (num(r["day_pnl"]) or 0) < 0),
    }
    rows.sort(key=lambda r: -(r["mv"] or 0))
    return rows, tot


# ---------------------------------------------------------------- 文字要点

def money(v, signed: bool = False) -> str:
    """金额：1 万以上用 万/亿，不足 1 万补“元”（如 +3601元）。"""
    s = fmt_amount(v, signed=signed)
    f = num(v)
    return s + "元" if f is not None and abs(f) < 1e4 else s


def _nm(s: dict) -> str:
    return s.get("label") or s.get("name") or s.get("code", "")


def _join(names: list[str], limit: int = 6) -> str:
    return "、".join(names[:limit]) + (f"等{len(names)}只" if len(names) > limit else "")


def breadth(stocks: list[dict]) -> dict:
    """涨跌家数、等权平均、最强/最弱。tie=True 表示有行情的股票涨跌幅全部相同（此时不分最强最弱）。"""
    traded = [s for s in stocks if s.get("traded") and s.get("pct") is not None]
    up = [s for s in traded if s["pct"] > 0]
    down = [s for s in traded if s["pct"] < 0]
    best = max(traded, key=lambda s: s["pct"]) if traded else None
    worst = min(traded, key=lambda s: s["pct"]) if traded else None
    avg = sum(s["pct"] for s in traded) / len(traded) if traded else None
    return {"total": len(stocks), "traded": len(traded), "up": len(up), "down": len(down),
            "flat": len(traded) - len(up) - len(down),
            "halt": [_nm(s) for s in stocks if not s.get("traded")],
            "nopct": [_nm(s) for s in stocks if s.get("traded") and s.get("pct") is None],
            "best": best, "worst": worst, "avg": avg,
            "tie": bool(traded) and round(best["pct"], 2) == round(worst["pct"], 2)}


LHB_STATE_TEXT = {"empty": "龙虎榜暂未发布", "failed": "龙虎榜获取失败", "format": "龙虎榜数据格式无法识别"}


def summary_lines(stocks: list[dict], tot: dict | None, lhb_state: str = "ok",
                  flow_line: str | None = None) -> list[str]:
    """复盘要点（客观描述）。stocks 每项含 name/pct/traded/labels。
    lhb_state：'ok' 龙虎榜可用；'empty'/'failed'/'format' 时不能断言“未上榜”，改为注明未核对。
    flow_line：自选股进入个股资金排行的说明（见 flow_rank_line），放在当日事件之后。
    写法与“大盘与情绪”模块一致：句末用“。”，名称/文字与数字、数字与单位之间不加空格。"""
    b = breadth(stocks)
    lines = []
    if b["traded"]:
        s = f"自选股{b['total']}只：上涨{b['up']}只、下跌{b['down']}只"
        if b["flat"]:
            s += f"、平盘{b['flat']}只"
        if b["halt"]:
            s += f"、停牌/无数据{len(b['halt'])}只"
        if b["nopct"]:
            s += f"、涨跌幅缺失{len(b['nopct'])}只"
        s += f"，等权平均涨跌幅{fmt_pct(b['avg'])}"
        if b["traded"] >= 2 and b["tie"]:
            s += "；各股涨跌幅相同"
        elif b["traded"] >= 2:
            s += (f"；最强{_nm(b['best'])}（{fmt_pct(b['best']['pct'])}），"
                  f"最弱{_nm(b['worst'])}（{fmt_pct(b['worst']['pct'])}）")
        lines.append(s + "。")
    elif b["nopct"]:
        lines.append(f"自选股{b['total']}只：{_join(b['nopct'])}复盘日有行情但缺少涨跌幅（如上市首日），"
                     f"其余停牌或无数据。")
    elif stocks and not all(s.get("src", "") is None for s in stocks):
        # 全部日K都获取失败（src 为 None）时不写这句：错误框里已有说明，要点只放有数据的内容
        lines.append(f"自选股{b['total']}只在复盘日均无行情数据（停牌或数据获取失败）。")

    if tot and tot.get("mv") is not None:
        s = f"持仓{tot['count']}只，市值{money(tot['mv'])}"
        if tot.get("day_pnl") is not None:
            s += f"，当日盈亏{money(tot['day_pnl'], signed=True)}（{fmt_pct(tot['day_pct'])}）"
        if tot.get("float_pnl") is not None:
            s += f"，累计浮动盈亏{money(tot['float_pnl'], signed=True)}（{fmt_pct(tot['float_pct'])}）"
        lines.append(s + "。")

    events = []
    for label, title in (("涨停", "涨停"), ("炸板", "炸板"), ("跌停", "跌停"), ("上龙虎榜", "上龙虎榜"),
                         ("放量", "放量（量比≥2）"), ("创60日新高", "创60日新高"), ("创60日新低", "创60日新低"),
                         ("上穿20日线", "上穿20日线"), ("跌破20日线", "跌破20日线")):
        hit = [_nm(s) for s in stocks
               if label in s.get("labels", []) or (label == "涨停" and any(x.endswith("连板") for x in s.get("labels", [])))]
        if hit:
            events.append(f"{title}：{_join(hit)}")
    caveat = "" if lhb_state == "ok" else f"（{LHB_STATE_TEXT.get(lhb_state, '龙虎榜不可用')}，未核对上榜情况）"
    if events:   # 没有任何事件时不写（“当日无……情况”这类句子没有信息量）
        lines.append("；".join(events) + caveat + "。")
    if flow_line:
        lines.append(flow_line)
    if b["halt"] and (b["traded"] or b["nopct"]):
        lines.append(f"停牌/无数据：{_join(b['halt'])}。")
    return lines


# ---------------------------------------------------------------- 个股资金排行（来自“龙虎榜与资金”模块）

def _code6(v) -> str:
    digits = "".join(ch for ch in str(v or "") if ch.isdigit())
    return digits[-6:].zfill(6) if digits else ""


def flow_rank_hits(stocks: list[dict], rank: dict | None) -> dict[str, dict]:
    """自选股在个股资金排行（ctx.shared['funds.flow_rank']）里的名次。

    返回 {代码: {"dir": "inflow"|"outflow", "word": "主力净流入"…, "rank": 名次, "net": 金额(元)}}；
    没有排行数据或自选股都不在排行里时返回 {}。
    """
    if not isinstance(rank, dict):
        return {}
    codes = {s.get("code") for s in stocks}
    out: dict[str, dict] = {}
    for direction, word_key, default in (("inflow", "word_in", "主力净流入"), ("outflow", "word_out", "主力净流出")):
        word = rank.get(word_key) or default
        for it in rank.get(direction) or []:
            if not isinstance(it, dict):
                continue
            code, pos = _code6(it.get("code")), num(it.get("rank"))
            if code in codes and code not in out and pos is not None:
                out[code] = {"dir": direction, "word": word, "rank": int(pos), "net": num(it.get("net"))}
    return out


def flow_rank_label(hit: dict) -> str:
    """状态标签，如“主力净流入第1”。"""
    return f"{hit['word']}第{hit['rank']}"


def flow_rank_line(stocks: list[dict], hits: dict, rank: dict | None) -> str | None:
    """要点：'个股资金排行：宁德时代主力净流入第1（+19.58亿）、贵州茅台主力净流出第3（-5.00亿）。'
    数据源与口径写在口径说明里，要点只写名次与金额。"""
    found = [(s, hits[s.get("code")]) for s in stocks if s.get("code") in hits]
    found.sort(key=lambda x: (x[1]["dir"] != "inflow", x[1]["rank"]))   # 净流入在前，各按名次
    items = []
    for s, hit in found:
        amt = f"（{fmt_amount(hit['net'], signed=True)}）" if hit.get("net") is not None else ""
        items.append(f"{_nm(s)}{flow_rank_label(hit)}{amt}")
    if not items or not isinstance(rank, dict):
        return None
    return "个股资金排行：" + "、".join(items) + "。"


# ---------------------------------------------------------------- 图表

def _clean(seq, digits: int = 2) -> list:
    return [_r(v, digits) for v in seq]


def candle_up(o, c, prev_c=None) -> bool:
    """K 线按“涨”着色？与 ECharts candlestick 一致：收>开为涨、收<开为跌；
    开=收（如一字板）时与前一根收盘比较（不低于前收为涨），第一根算涨。"""
    o, c, p = _r(o), _r(c), _r(prev_c)
    if o is None or c is None:
        return False
    if c != o:
        return c > o
    return p is None or p <= c


def kline_option(k: pd.DataFrame, days: int = CHART_DAYS, cost: float | None = None) -> dict:
    """近 days 日 K 线 + MA5/10/20 + 成交量副图（红涨绿跌）。"""
    closes = k["close"]
    mas = {n: closes.rolling(n).mean() for n in CHART_MAS}
    t = k.tail(days)
    idx = t.index
    dates = [d.strftime("%m-%d") for d in t["date"]]
    # 类目轴下 ECharts 会自动在每根K线前补上类目序号，所以 dimensions 第一个名字对应日期，
    # 后四个对应 [开, 收, 低, 高]，提示框据此显示中文名称
    candles = [[_r(o), _r(c), _r(lo), _r(h)] for o, c, lo, h in zip(t["open"], t["close"], t["low"], t["high"])]
    vols = []
    prev_c = None
    for o, c, v in zip(t["open"], t["close"], t["volume"]):
        vols.append({"value": _r(num(v, 0) / 1e4, 2), "itemStyle": {"color": UP if candle_up(o, c, prev_c) else DOWN}})
        prev_c = c


    series = [{
        "name": "K线", "type": "candlestick", "data": candles,
        "dimensions": ["日期", "开盘", "收盘", "最低", "最高"],
        "encode": {"x": 0, "y": [1, 2, 3, 4], "tooltip": [1, 2, 3, 4]},
        "itemStyle": {"color": UP, "color0": DOWN, "borderColor": UP, "borderColor0": DOWN},
    }]
    colors = {5: SERIES[2], 10: SERIES[0], 20: SERIES[4]}
    for n in CHART_MAS:
        series.append({"name": f"MA{n}", "type": "line", "data": _clean(mas[n].loc[idx]), "showSymbol": False,
                       "lineStyle": {"width": 1}, "color": colors[n], "z": 3})
    series.append({"name": "成交量(万手)", "type": "bar", "xAxisIndex": 1, "yAxisIndex": 1,
                   "data": vols, "barMaxWidth": 8})

    lo, hi = num(t["low"].min()), num(t["high"].max())
    if cost and lo is not None and hi is not None and lo * 0.9 <= cost <= hi * 1.1:
        series[0]["markLine"] = {
            "symbol": "none", "silent": True,
            "data": [{"yAxis": round(cost, 2), "name": "成本"}],
            "lineStyle": {"type": "dashed", "color": ACCENT, "width": 1},
            "label": {"formatter": "成本 {c}", "position": "insideStartTop", "color": ACCENT},
        }
    return {
        "animation": False,
        "legend": {"data": [f"MA{n}" for n in CHART_MAS], "top": 0, "right": 4, "itemWidth": 14, "itemHeight": 8,
                   "textStyle": {"fontSize": 11}},
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "cross"}},
        "axisPointer": {"link": [{"xAxisIndex": "all"}]},
        "grid": [{"left": 56, "right": 12, "top": 24, "bottom": 100},
                 {"left": 56, "right": 12, "height": 54, "bottom": 22}],
        "xAxis": [
            {"type": "category", "data": dates, "boundaryGap": True,
             "axisLabel": {"show": False}, "axisTick": {"show": False}},
            {"type": "category", "gridIndex": 1, "data": dates, "boundaryGap": True, "axisTick": {"show": False}},
        ],
        "yAxis": [
            {"scale": True, "splitNumber": 4},
            {"gridIndex": 1, "splitNumber": 2, "axisLabel": {"show": False}, "axisLine": {"show": False},
             "axisTick": {"show": False}, "splitLine": {"show": False}},
        ],
        "series": series,
    }


def pct_bar_option(entries: list[tuple[str, float]]) -> dict:
    """自选股当日涨跌幅横向条形图（最强在上）。entries: [(名称, 涨跌幅%)]。"""
    entries = sorted(entries, key=lambda e: e[1])
    data = []
    for _, p in entries:
        color = UP if p > 0 else DOWN if p < 0 else FLAT
        data.append({"value": round(p, 2), "itemStyle": {"color": color},
                     "label": {"position": "right" if p >= 0 else "left"}})
    # 两端各留出约 30% 空白放数值标签，避免负值标签压住左侧名称
    vals = [e[1] for e in entries] or [0.0]
    span = max(1.0, max(abs(v) for v in vals))
    lo = -float(np.ceil(abs(min(vals)) + span * 0.3)) if min(vals) < 0 else 0
    hi = float(np.ceil(max(vals) + span * 0.3)) if max(vals) > 0 else 0
    if lo == hi:
        hi = 1.0
    return {
        "grid": {"left": 8, "right": 16, "top": 8, "bottom": 8, "containLabel": True},
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "xAxis": {"type": "value", "min": lo, "max": hi, "axisLabel": {"formatter": "{value}%"}},
        "yAxis": {"type": "category", "data": [e[0] for e in entries], "axisTick": {"show": False}},
        "series": [{"name": "涨跌幅(%)", "type": "bar", "data": data, "barMaxWidth": 18,
                    "label": {"show": True, "formatter": "{c}%", "color": "inherit"}}],
    }


def pct_bar_height(n: int) -> int:
    return int(max(140, min(640, 30 * n + 40)))


def weight_pie_option(rows: list[dict]) -> dict:
    """持仓市值占比（环形图）。"""
    data = [{"name": r["name"], "value": round(r["mv"], 2)} for r in rows if r.get("mv")]
    return {
        "color": SERIES,
        "tooltip": {"trigger": "item", "formatter": "{b}：{d}%"},
        "series": [{"name": "持仓占比", "type": "pie", "radius": ["42%", "70%"], "center": ["50%", "52%"],
                    "data": data, "label": {"formatter": "{b}\n{d}%", "color": "inherit"},
                    "itemStyle": {"borderWidth": 1, "borderColor": "rgba(128,128,128,0.25)"}}],
    }


def trend_option(hist: pd.DataFrame) -> dict | None:
    """持仓市值（万元）与当日盈亏走势，来自 history.csv。"""
    if hist is None or hist.empty or "持仓市值" not in hist.columns:
        return None
    pnl_col = hist["持仓当日盈亏"] if "持仓当日盈亏" in hist.columns else pd.Series(np.nan, index=hist.index)
    h = hist.assign(mv=pd.to_numeric(hist["持仓市值"], errors="coerce"),
                    pnl=pd.to_numeric(pnl_col, errors="coerce"))
    h = h.dropna(subset=["mv"])
    if len(h) < 3:
        return None
    pnl = [{"value": _r(v / 1e4), "itemStyle": {"color": UP if v > 0 else DOWN if v < 0 else FLAT}}
           if num(v) is not None else None for v in h["pnl"]]
    return {
        "legend": {"data": ["持仓市值(万元)", "当日盈亏(万元)"], "top": 0},
        "tooltip": {"trigger": "axis"},
        "grid": {"left": 8, "right": 8, "top": 30, "bottom": 8, "containLabel": True},
        "xAxis": {"type": "category", "data": [str(d)[5:] for d in h["date"]]},
        "yAxis": [{"type": "value", "scale": True, "name": "市值"}, {"type": "value", "name": "盈亏", "splitLine": {"show": False}}],
        "series": [
            {"name": "持仓市值(万元)", "type": "line", "data": _clean(h["mv"] / 1e4), "color": ACCENT, "showSymbol": False},
            {"name": "当日盈亏(万元)", "type": "bar", "yAxisIndex": 1, "data": pnl, "barMaxWidth": 14, "color": FLAT},
        ],
    }
