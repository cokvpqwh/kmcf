"""「龙虎榜与资金」模块的纯计算逻辑（不联网，便于单元测试）。

约定：输出 DataFrame 的金额一律为“元”，涨跌幅/占比为“百分比数值”（2.35 表示 2.35%）。
接口必需的列缺失时抛 ValueError（“接口字段变化：缺少 X 列”），不静默输出 0。
"""

import datetime as dt
import re

import pandas as pd

from ..section import DOWN, UP, is_missing, num
from ._names import clean_stock_name

YI = 1e8


# ---------------------------------------------------------------- 通用小工具

def to_yi(v, digits: int = 2):
    """元 -> 亿元（图表用），无法转换返回 None。"""
    f = num(v)
    return None if f is None else round(f / YI, digits)


def to_date(v) -> dt.date | None:
    """'2026-10-09 00:00:00' / date / Timestamp -> date。"""
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v).strip()[:10])
    except ValueError:
        return None


def norm_code(v) -> str:
    """股票代码统一成 6 位字符串（去掉 sh/sz/bj 前缀、.SH 后缀，补前导 0）。"""
    s = re.sub(r"\D", "", str(v).split(".")[0]) if v is not None and not is_missing(v) else ""
    return s.zfill(6) if s else ""


# A 股代码（沪深主板/创业板/科创板/北交所）。龙虎榜数据里偶尔混有可转债（11/12 开头）、B 股（200/900 开头），不算“个股”。
_A_SHARE_CODE = re.compile(r"^(00|30|60|68|92|4|8)\d{4}$")


def is_a_share(code) -> bool:
    return bool(_A_SHARE_CODE.match(str(code or "")))


def require_cols(df: pd.DataFrame, cols, what: str) -> None:
    """接口返回缺少必需列时抛 ValueError（接口改版时给出明确提示，而不是输出一列 0）。"""
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"{what}接口字段变化：缺少 {'、'.join(missing)} 列")


def _numcol(df: pd.DataFrame, col: str, scale: float = 1.0) -> pd.Series:
    if col not in df.columns:
        return pd.Series([None] * len(df), index=df.index, dtype="float64")
    return df[col].map(lambda v: num(v)).astype("float64") * scale


def _clean_text(v) -> str:
    """空值 / 'None' / 'nan' -> ''（pandas 3 的 astype(str) 会把 NaN 原样保留，不能靠它判断）。"""
    if v is None or is_missing(v):
        return ""
    s = str(v).strip()
    return "" if s in ("None", "nan", "NaN", "<NA>") else s


def _textcol(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series([""] * len(df), index=df.index, dtype=object)
    return df[col].map(_clean_text).astype(object)


def _names(df: pd.DataFrame, name_col: str, codes: pd.Series) -> pd.Series:
    """股票简称（去掉半角/全角空格，如“粤 传 媒”→“粤传媒”），缺失时用代码代替。"""
    names = _textcol(df, name_col).map(clean_stock_name).astype(object)
    return names.where(names != "", codes)


def streak(values) -> int:
    """末尾连续同号的天数：+3 表示连续 3 日为正，-2 表示连续 2 日为负，0 表示最后一天为 0/缺失。"""
    vals = [num(v) for v in values]
    if not vals or vals[-1] is None or vals[-1] == 0:
        return 0
    sign = 1 if vals[-1] > 0 else -1
    n = 0
    for v in reversed(vals):
        if v is None or v == 0 or (v > 0) != (sign > 0):
            break
        n += 1
    return sign * n


def name_list(names, limit: int = 6, sep: str = "、") -> str:
    """名称列表 -> '甲、乙、丙等8只'。"""
    items = [_clean_text(x) for x in names]
    items = [x for x in items if x]
    if len(items) <= limit:
        return sep.join(items)
    return sep.join(items[:limit]) + f"等{len(items)}只"


# ---------------------------------------------------------------- 龙虎榜

_REASON_RULES = [
    (r"的前[5五]只(证券|股票)$", ""), (r"的(证券|股票)$", ""),
    (r"^有价格涨跌幅限制的", ""), (r"^非S证券", ""), (r"^当日", "日"),
    (r"连续(\d+)个交易日内[，,]?(\d+)次出现同(正|负)?向异常波动", r"\1日内\2次同\3向异常波动"),
    (r"连续三个交易日内[，,]?", "3日"), (r"连续(\d+)个交易日内[，,]?", r"\1日"),
    (r"最近(\d+)个有成交的交易日以内", r"\1日"), (r"收盘价格?", ""),
    (r"偏离值", "偏离"), (r"振幅值", "振幅"), (r"价格振幅", "振幅"),
    (r"累计达到", ""), (r"达到", ""), (r"日日", "日"),
    (r"跌幅(偏离)?-(\d)", r"跌幅\1\2"),                    # “日跌幅-20%”：“跌幅”已表明方向，去掉重复的负号
    (r"[+＋](\d+(?:\.\d+)?%)[(（][-－]\1[)）]", r"±\1"),   # “偏离+40%(-40%)” -> “偏离±40%”
    (r"^非ST、\*ST和S证券", ""),
    (r"^ST、\*ST证券", "ST股"), (r"^(.+)的ST证券、\*ST证券和未完成股改证券$", r"ST股\1"),
    (r"^北交所股票", "北交所"),
]


def short_reason(s) -> str:
    """上榜原因精简：'有价格涨跌幅限制的日收盘价格涨幅偏离值达到7%的前五只证券' -> '日涨幅偏离7%'。"""
    s = _clean_text(s)
    for pat, rep in _REASON_RULES:
        s = re.sub(pat, rep, s)
    return s


def is_multi_day(reason) -> bool:
    """多日累计类上榜原因（金额为多日累计，不是当日）。"""
    s = _clean_text(reason)
    return bool(re.search(r"连续|[三3]个交易日|\d+个交易日|累计", s))


# 合并同一股票多条记录时的优先级（越小越优先），同时决定“金额口径”
RANK_DAY, RANK_3DAY, RANK_MULTI, RANK_NO_SEAT = 0, 1, 2, 3
BASIS = {RANK_DAY: "当日", RANK_3DAY: "3日累计", RANK_MULTI: "多日累计", RANK_NO_SEAT: "多日累计（无席位明细）"}


def _reason_rank(reason) -> int:
    """当日 > 3日累计 > 其他多日（10 日异常波动、严重异常期间等）。"""
    if not is_multi_day(reason):
        return RANK_DAY
    return RANK_3DAY if re.search(r"[三3]个(有成交的)?交易日", _clean_text(reason)) else RANK_MULTI


def no_seat_rows(df: pd.DataFrame) -> pd.Series:
    """“连续10个交易日 / 严重异常期间”类记录：数据中心的席位字段填的是期间累计成交额
    （买入 = 卖出 = 累计成交额、净额 0），没有真实的席位买卖数据。df 需含 net buy sell total 列。"""
    tot = df["total"]
    tol = tot.abs() * 1e-4 + 1
    return ((df["net"].fillna(0).abs() < 1) & (tot > 0)
            & ((df["buy"] - tot).abs() <= tol) & ((df["sell"] - tot).abs() <= tol))


def clean_explain(s) -> str:
    """东方财富“解读”去掉“成功率”统计，只保留席位描述（如“3家机构买入”）。"""
    s = re.sub(r"[，,;；]?\s*成功率\s*[\d.]+%", "", _clean_text(s)).strip(" ，,;；")
    return s


def _ranked(df: pd.DataFrame, amount_cols: list[str]) -> pd.DataFrame:
    """按上榜原因给每条记录定优先级；无席位明细的记录排最后，并把其金额置空（不能当作真实买卖额）。"""
    rank = df["reason_raw"].map(_reason_rank)
    noseat = no_seat_rows(df)
    df = df.assign(_rank=rank.where(~noseat, RANK_NO_SEAT))
    return df.assign(**{c: df[c].mask(noseat) for c in amount_cols})


def _dedupe(df: pd.DataFrame, amount_col: str) -> pd.DataFrame:
    """同一代码多条记录：按优先级（_rank）选一条代表行，并合并所有原因。"""
    df = df.assign(_amt=_numcol(df, amount_col).fillna(0))
    df = df.sort_values(["code", "_rank", "_amt"], ascending=[True, True, False], kind="stable")
    reasons = df.groupby("code", sort=False)["reason_raw"].agg(
        lambda s: "；".join(dict.fromkeys(r for r in (short_reason(x) for x in s) if r)))
    first = df.drop_duplicates("code", keep="first").set_index("code")
    first = first.assign(reasons=reasons, multi_day=first["_rank"] > RANK_DAY, basis=first["_rank"].map(BASIS))
    return first.drop(columns=["_rank", "_amt"]).reset_index()


LHB_COLS = ["code", "name", "close", "pct", "net", "buy", "sell", "deal", "total", "net_ratio",
            "turnover", "float_cap", "reasons", "explain", "multi_day", "basis"]


def merge_lhb(raw: pd.DataFrame) -> pd.DataFrame:
    """龙虎榜明细（每个上榜原因一行）-> 每只股票一行，按净买额降序。

    金额优先取“当日”上榜口径；只有多日累计上榜的股票取累计口径（basis 列注明，multi_day=True）。
    """
    if raw is None or raw.empty:
        return pd.DataFrame(columns=LHB_COLS)
    require_cols(raw, ["SECURITY_CODE", "BILLBOARD_NET_AMT", "BILLBOARD_BUY_AMT", "BILLBOARD_SELL_AMT",
                       "EXPLANATION"], "龙虎榜")
    codes = raw["SECURITY_CODE"].map(norm_code)
    df = pd.DataFrame({
        "code": codes,
        "name": _names(raw, "SECURITY_NAME_ABBR", codes),
        "close": _numcol(raw, "CLOSE_PRICE"),
        "pct": _numcol(raw, "CHANGE_RATE"),
        "net": _numcol(raw, "BILLBOARD_NET_AMT"),
        "buy": _numcol(raw, "BILLBOARD_BUY_AMT"),
        "sell": _numcol(raw, "BILLBOARD_SELL_AMT"),
        "deal": _numcol(raw, "BILLBOARD_DEAL_AMT"),
        "total": _numcol(raw, "ACCUM_AMOUNT"),
        "net_ratio": _numcol(raw, "DEAL_NET_RATIO"),
        "turnover": _numcol(raw, "TURNOVERRATE"),
        "float_cap": _numcol(raw, "FREE_MARKET_CAP"),
        "reason_raw": _textcol(raw, "EXPLANATION"),
        "explain": raw["EXPLAIN"].map(clean_explain) if "EXPLAIN" in raw.columns else "",
    })
    df = df[df["code"].map(is_a_share)]
    df = _ranked(df, ["net", "buy", "sell", "deal", "net_ratio"])
    out = _dedupe(df, "deal")
    out = out.sort_values("net", ascending=False, na_position="last", kind="stable").reset_index(drop=True)
    return out[LHB_COLS]


def _top(df: pd.DataFrame, positive: bool) -> dict | None:
    net = df["net"]
    if positive:
        return df.loc[net.idxmax()].to_dict() if (net > 0).any() else None
    return df.loc[net.idxmin()].to_dict() if (net < 0).any() else None


def flow_stats(df: pd.DataFrame) -> dict:
    """龙虎榜 / 机构的汇总，只用“当日”口径（multi_day=False）的股票；多日累计的单独计数、不混进合计。

    net_sum 为 None 表示有当日记录但金额全部缺失（数据不完整，不应写进历史）。
    """
    if df is None or df.empty:
        return {"count": 0, "day_n": 0, "net_sum": 0.0, "buy_n": 0, "sell_n": 0, "top_buy": None,
                "top_sell": None, "md_n": 0, "md_net": None}
    md_mask = df["multi_day"].astype(bool)
    day, md = df[~md_mask], df[md_mask]
    net = day["net"].dropna()
    md_net = md["net"].dropna()
    if day.empty:
        net_sum = 0.0
    else:
        net_sum = float(net.sum()) if len(net) else None
    return {"count": int(len(df)), "day_n": int(len(day)), "net_sum": net_sum,
            "buy_n": int((net > 0).sum()), "sell_n": int((net < 0).sum()),
            "top_buy": _top(day, True) if len(net) else None, "top_sell": _top(day, False) if len(net) else None,
            "md_n": int(len(md)), "md_net": float(md_net.sum()) if len(md_net) else None}


def lhb_stats(lhb: pd.DataFrame) -> dict:
    """龙虎榜汇总：上榜家数（全部）、当日口径的净买入合计、净买入/净卖出家数、净买入/卖出最多的个股。"""
    return flow_stats(lhb)


def explain_mentions_org(lhb: pd.DataFrame) -> int:
    """龙虎榜“解读”里提到机构席位的股票数。"""
    if lhb is None or lhb.empty or "explain" not in lhb.columns:
        return 0
    return int(lhb["explain"].map(lambda s: "机构" in _clean_text(s)).sum())


ORG_COLS = ["code", "name", "close", "pct", "buy_n", "sell_n", "buy", "sell", "net", "ratio",
            "turnover", "reasons", "multi_day", "basis", "seats"]


def normalize_org(raw: pd.DataFrame) -> pd.DataFrame:
    """机构买卖每日统计 -> 每只股票一行，按机构净买额降序（basis/multi_day 标明金额口径）。"""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=ORG_COLS)
    require_cols(raw, ["SECURITY_CODE", "NET_BUY_AMT", "EXPLANATION"], "机构买卖统计")
    codes = raw["SECURITY_CODE"].map(norm_code)
    df = pd.DataFrame({
        "code": codes,
        "name": _names(raw, "SECURITY_NAME_ABBR", codes),
        "close": _numcol(raw, "CLOSE_PRICE"),
        "pct": _numcol(raw, "CHANGE_RATE"),
        "buy_n": _numcol(raw, "BUY_TIMES"),
        "sell_n": _numcol(raw, "SELL_TIMES"),
        "buy": _numcol(raw, "BUY_AMT"),
        "sell": _numcol(raw, "SELL_AMT"),
        "net": _numcol(raw, "NET_BUY_AMT"),
        "total": _numcol(raw, "ACCUM_AMOUNT"),
        "ratio": _numcol(raw, "RATIO"),
        "turnover": _numcol(raw, "TURNOVERRATE"),
        "reason_raw": _textcol(raw, "EXPLANATION"),
    })
    df = df[df["code"].map(is_a_share)]
    df = _ranked(df, ["buy", "sell", "net", "ratio"])
    df = df.assign(_deal=df["buy"].fillna(0) + df["sell"].fillna(0))
    out = _dedupe(df, "_deal").drop(columns=["_deal"])

    def seats(r):
        b, s = num(r["buy_n"], 0), num(r["sell_n"], 0)
        return f"买{b:.0f} / 卖{s:.0f}"

    out = out.assign(seats=out.apply(seats, axis=1) if len(out) else "")
    out = out.sort_values("net", ascending=False, na_position="last", kind="stable").reset_index(drop=True)
    return out[ORG_COLS]


BRANCH_COLS = ["name", "full_name", "kind", "buy_n", "sell_n", "buy", "sell", "net", "total", "stocks", "stock_n"]


def branch_kind(name) -> str:
    s = _clean_text(name)
    if "股通专用" in s:
        return "北向"
    if "机构专用" in s:
        return "机构"
    return "营业部"


_BRANCH_ENDINGS = ("营业部", "分公司", "总部", "专用")
_CORP_WORDS = re.compile(r"股份有限公司|有限责任公司|有限公司")


def branch_name(short, full) -> str:
    """营业部显示名：简称以“营业部/分公司/总部/专用”结尾时用简称；否则（如简称只有“国泰海通”，看不出是哪个席位）
    用全称并去掉“股份有限公司/有限责任公司/有限公司”（“国泰海通证券股份有限公司总部” -> “国泰海通证券总部”）。"""
    short, full = _clean_text(short), _clean_text(full)
    if short.endswith(_BRANCH_ENDINGS) or not full:
        return short or full
    return _CORP_WORDS.sub("", full) or full


def normalize_branch(raw: pd.DataFrame) -> pd.DataFrame:
    """每日活跃营业部 -> 标准列，按净额降序。stocks 为涉及个股名称（买入或卖出）。"""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=BRANCH_COLS)
    require_cols(raw, ["OPERATEDEPT_NAME", "TOTAL_BUYAMT", "TOTAL_SELLAMT", "TOTAL_NETAMT"], "活跃营业部")
    full = _textcol(raw, "OPERATEDEPT_NAME")
    short = pd.Series([branch_name(s, f) for s, f in zip(_textcol(raw, "ORG_NAME_ABBR"), full)],
                      index=raw.index, dtype=object)
    # 涉及个股：空格分隔的简称列表（数据中心的简称本身不含空格）
    name_lists = _textcol(raw, "SECURITY_NAME_ABBR").map(
        lambda s: [n for n in (clean_stock_name(x) for x in s.split()) if n])
    df = pd.DataFrame({
        "name": short, "full_name": full, "kind": full.map(branch_kind),
        "buy_n": _numcol(raw, "BUYER_APPEAR_NUM"), "sell_n": _numcol(raw, "SELLER_APPEAR_NUM"),
        "buy": _numcol(raw, "TOTAL_BUYAMT"), "sell": _numcol(raw, "TOTAL_SELLAMT"),
        "net": _numcol(raw, "TOTAL_NETAMT"),
        "stocks": name_lists.map(name_list), "stock_n": name_lists.map(len),
    })
    df = df[df["full_name"] != ""]
    df = df.assign(total=df["buy"].fillna(0) + df["sell"].fillna(0))
    return df.sort_values("net", ascending=False, na_position="last", kind="stable").reset_index(drop=True)[BRANCH_COLS]


def top_active_branches(branch: pd.DataFrame, n: int) -> pd.DataFrame:
    """营业部：排除沪深股通、机构专用席位，取买卖总额前 n，再按净额降序展示。"""
    if branch is None or branch.empty:
        return pd.DataFrame(columns=BRANCH_COLS)
    b = branch[branch["kind"] == "营业部"]
    b = b.sort_values("total", ascending=False, kind="stable").head(n)
    return b.sort_values("net", ascending=False, na_position="last", kind="stable").reset_index(drop=True)


# ---------------------------------------------------------------- 大盘资金流向

MFF_MAP = {
    "main": "主力净流入-净额", "main_ratio": "主力净流入-净占比",
    "super": "超大单净流入-净额", "big": "大单净流入-净额",
    "mid": "中单净流入-净额", "small": "小单净流入-净额",
    "sh_close": "上证-收盘价", "sh_pct": "上证-涨跌幅",
}


def market_flow_series(mff: pd.DataFrame, trade_date: dt.date, n: int = 60) -> pd.DataFrame:
    """akshare.stock_market_fund_flow 的全部历史 -> 截至复盘日（含）最近 n 日，列：date + MFF_MAP 的键。"""
    if mff is None or mff.empty:
        return pd.DataFrame(columns=["date", *MFF_MAP])
    require_cols(mff, ["日期", MFF_MAP["main"]], "大盘资金流向")
    df = pd.DataFrame({"date": mff["日期"].map(to_date)})
    for key, col in MFF_MAP.items():
        df[key] = _numcol(mff, col)
    df = df.dropna(subset=["date"])
    df = df[df["date"] <= trade_date].sort_values("date").drop_duplicates("date", keep="last")
    return df.tail(n).reset_index(drop=True)


def market_flow_day(series: pd.DataFrame, trade_date: dt.date) -> dict | None:
    """复盘日当天的大盘资金流（没有该日数据返回 None），附带连续天数与近 5/20 日累计。

    复盘日这一行存在但主力净流入为空时，main 为 None（由调用方判断并改用备用源）。
    """
    if series is None or series.empty or series["date"].iloc[-1] != trade_date:
        return None
    row = series.iloc[-1].to_dict()
    row = {k: (num(v) if k != "date" else v) for k, v in row.items()}
    row["streak"] = streak(series["main"].tolist())
    row["sum5"] = float(series["main"].tail(5).fillna(0).sum())
    row["sum20"] = float(series["main"].tail(20).fillna(0).sum())
    row["days"] = len(series)
    return row


# ---------------------------------------------------------------- 个股资金流排行（三个数据源统一成同一套列）

RANK_COLS = ["code", "name", "price", "pct", "net", "ratio", "amount"]


def normalize_em_rank(df: pd.DataFrame) -> pd.DataFrame:
    """东方财富 push2 排行（em_fund_flow_rank 的输出）-> RANK_COLS。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=RANK_COLS)
    require_cols(df, ["code", "main_net"], "东方财富个股资金流")
    codes = df["code"].map(norm_code)
    out = pd.DataFrame({
        "code": codes, "name": _names(df, "name", codes),
        "price": _numcol(df, "price"), "pct": _numcol(df, "pct"),
        "net": _numcol(df, "main_net"), "ratio": _numcol(df, "main_ratio"),
        "amount": _numcol(df, "amount"),
    })
    return out.dropna(subset=["net"]).reset_index(drop=True)


def em_rank_date(df: pd.DataFrame) -> dt.date | None:
    """push2 快照里最新的行情时间（f124，秒）对应的日期，用来确认快照属于复盘日。"""
    if df is None or df.empty or "ts" not in df.columns:
        return None
    ts = _numcol(df, "ts").dropna()
    ts = ts[ts > 0]
    if ts.empty:
        return None
    return dt.datetime.fromtimestamp(ts.max()).date()


# 沪深京 A 股。北交所只认 92 开头的新代码：新浪仍残留已停用的 4/8 开头旧代码，数据停留在多年前（价格、成交额都过期）。
_A_SHARE_SYMBOL = re.compile(r"^(sh6|sz0|sz3|bj92)")


def normalize_sina_rank(data) -> pd.DataFrame:
    """新浪 MoneyFlow.ssl_bkzj_ssggzj 返回（list[dict] 或 DataFrame）-> RANK_COLS + has_main，只保留当日有成交的 A 股。

    新浪 changeratio / r0_ratio 是小数（0.0384 = 3.84%），金额单位元；r0_net 为主力净流入。
    新浪的资金流统计不含尾盘集合竞价，price/pct/amount 与收盘行情略有出入（展示时应改用收盘行情）。
    has_main=False：当日有成交但新浪没有给出主力数据（r0_in = r0_out = 0），不参与主力家数统计。
    """
    df = pd.DataFrame(data) if not isinstance(data, pd.DataFrame) else data
    if df is None or df.empty:
        return pd.DataFrame(columns=RANK_COLS + ["has_main"])
    require_cols(df, ["symbol", "r0_net", "amount"], "新浪个股资金流")
    df = df[df["symbol"].astype(str).str.match(_A_SHARE_SYMBOL)]
    codes = df["symbol"].map(norm_code)
    net = _numcol(df, "r0_net")
    if "r0_in" in df.columns and "r0_out" in df.columns:
        has_main = (_numcol(df, "r0_in").fillna(0) != 0) | (_numcol(df, "r0_out").fillna(0) != 0)
    else:
        has_main = net.fillna(0) != 0
    out = pd.DataFrame({
        "code": codes, "name": _names(df, "name", codes),
        "price": _numcol(df, "trade"), "pct": _numcol(df, "changeratio", 100.0),
        "net": net, "ratio": _numcol(df, "r0_ratio", 100.0),
        "amount": _numcol(df, "amount"), "has_main": has_main,
    })
    out = out[out["amount"] > 0]  # 停牌、已退市（成交额为 0）的条目不算
    return out.dropna(subset=["net"]).reset_index(drop=True)


def sina_market_total(data) -> dict | None:
    """新浪全市场个股资金流 -> 主力净流入/净流出家数及合计（新浪口径）。数据太少（不像全市场）时返回 None。

    count：当日有成交的 A 股数；covered：其中新浪给出主力数据的股票数；no_main = count - covered。
    """
    r = normalize_sina_rank(data)
    if len(r) < 1000:
        return None
    cov = r[r["has_main"]]
    net = cov["net"]
    amount = float(r["amount"].sum())
    total = float(net.sum())
    return {"main": total, "amount": amount, "ratio": total / amount * 100 if amount > 0 else None,
            "n_in": int((net > 0).sum()), "n_out": int((net < 0).sum()), "count": int(len(r)),
            "covered": int(len(cov)), "no_main": int(len(r) - len(cov))}


def _find_col(df: pd.DataFrame, *keys) -> str | None:
    for c in df.columns:
        if all(k in str(c) for k in keys):
            return c
    return None


def normalize_ths_rank(df: pd.DataFrame) -> pd.DataFrame:
    """同花顺个股资金流“即时”表格 -> RANK_COLS（净额 = 流入 - 流出，同花顺口径；ratio = 净额/成交额）。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=RANK_COLS)
    c_code, c_name = _find_col(df, "代码"), _find_col(df, "简称")
    c_net, c_amt = _find_col(df, "净额"), _find_col(df, "成交额")
    c_price, c_pct = _find_col(df, "最新价"), _find_col(df, "涨跌幅")
    if not (c_code and c_name and c_net):
        raise ValueError("同花顺表格缺少代码/简称/净额列")
    net = _numcol(df, c_net)
    amount = _numcol(df, c_amt) if c_amt else pd.Series([None] * len(df), index=df.index, dtype="float64")
    codes = df[c_code].map(norm_code)
    out = pd.DataFrame({
        "code": codes, "name": _names(df, c_name, codes),
        "price": _numcol(df, c_price) if c_price else None,
        "pct": _numcol(df, c_pct) if c_pct else None,
        "net": net, "ratio": (net / amount.where(amount > 0) * 100).round(2), "amount": amount,
    })
    return out.dropna(subset=["net"]).reset_index(drop=True)


def split_rank(rank: pd.DataFrame, n: int, direction: str) -> pd.DataFrame:
    """取净流入最多（in）或净流出最多（out）的前 n 只。"""
    if rank is None or rank.empty:
        return pd.DataFrame(columns=RANK_COLS)
    r = rank.drop_duplicates("code")
    if direction == "in":
        r = r[r["net"] > 0].sort_values("net", ascending=False, kind="stable")
    else:
        r = r[r["net"] < 0].sort_values("net", ascending=True, kind="stable")
    return r.head(n).reset_index(drop=True)


# ---------------------------------------------------------------- 收盘行情（新浪 hq.sinajs.cn）

QUOTE_COLS = ["code", "name", "prev_close", "price", "amount", "date"]


def sina_symbol(code) -> str | None:
    """6 位代码 -> 新浪行情代码（sh600519 / sz300750 / bj920627），认不出返回 None。"""
    c = norm_code(code)
    if len(c) != 6:
        return None
    if c.startswith(("60", "68")):
        return "sh" + c
    if c.startswith(("00", "30")):
        return "sz" + c
    if c.startswith(("92", "8", "4")):
        return "bj" + c
    return None


def parse_sina_quotes(text: str) -> pd.DataFrame:
    """解析 hq.sinajs.cn 返回：名称,开盘,昨收,现价,最高,最低,买一,卖一,成交量(股),成交额(元),五档…,日期,时间,…"""
    rows = []
    for m in re.finditer(r'hq_str_(?:sh|sz|bj)(\d{6})="([^"]*)"', text or ""):
        f = m.group(2).split(",")
        if len(f) < 32:
            continue
        rows.append({"code": m.group(1), "name": f[0].strip(), "prev_close": num(f[2]), "price": num(f[3]),
                     "amount": num(f[9]), "date": to_date(f[30])})
    return pd.DataFrame(rows, columns=QUOTE_COLS)


def apply_quotes(rank: pd.DataFrame, quotes: pd.DataFrame, trade_date: dt.date) -> pd.DataFrame:
    """用复盘日收盘行情替换排行表的 price / pct / amount。行情日期不是复盘日或缺失的股票留空。"""
    if rank is None or rank.empty:
        return rank
    q = quotes if quotes is not None else pd.DataFrame(columns=QUOTE_COLS)
    q = q[(q["date"] == trade_date) & (q["price"] > 0) & (q["prev_close"] > 0)].drop_duplicates("code")
    q = q.set_index("code")
    price = rank["code"].map(q["price"]).astype("float64")
    prev = rank["code"].map(q["prev_close"]).astype("float64")
    amount = rank["code"].map(q["amount"]).astype("float64")
    return rank.assign(price=price, pct=((price / prev - 1) * 100).round(4), amount=amount)


# ---------------------------------------------------------------- 北向 / 两融

def northbound_series(raw: pd.DataFrame, trade_date: dt.date, n: int = 20) -> pd.DataFrame:
    """RPT_MUTUAL_DEAL_HISTORY（北向）-> date amount(元) deals，截至复盘日最近 n 日，升序。

    DEAL_AMT 单位为百万元（与 akshare 把 BUY_AMT/100 当亿元的处理一致）。
    """
    cols = ["date", "amount", "deals"]
    if raw is None or raw.empty:
        return pd.DataFrame(columns=cols)
    require_cols(raw, ["TRADE_DATE", "DEAL_AMT"], "北向成交")
    df = pd.DataFrame({"date": raw["TRADE_DATE"].map(to_date), "amount": _numcol(raw, "DEAL_AMT", 1e6),
                       "deals": _numcol(raw, "DEAL_NUM")})
    df = df.dropna(subset=["date", "amount"])
    df = df[(df["date"] <= trade_date) & (df["amount"] > 0)]
    return df.sort_values("date").drop_duplicates("date", keep="last").tail(n).reset_index(drop=True)


MARGIN_COLS = ["date", "margin", "fin", "loan", "fin_buy", "trade_ratio"]


def margin_series(raw: pd.DataFrame, source: str, trade_date: dt.date, n: int = 60) -> pd.DataFrame:
    """两融数据 -> 统一列（元），截至复盘日最近 n 日，升序。

    source='daily'：RPTA_WEB_MARGIN_DAILYTRADE，金额单位亿元；
    source='total'：RPTA_RZRQ_LSHJ，金额单位元。
    """
    if raw is None or raw.empty:
        return pd.DataFrame(columns=MARGIN_COLS)
    if source == "daily":
        require_cols(raw, ["STATISTICS_DATE", "MARGIN_BALANCE", "FIN_BALANCE"], "两融账户统计")
        df = pd.DataFrame({
            "date": raw["STATISTICS_DATE"].map(to_date), "margin": _numcol(raw, "MARGIN_BALANCE", YI),
            "fin": _numcol(raw, "FIN_BALANCE", YI), "loan": _numcol(raw, "LOAN_BALANCE", YI),
            "fin_buy": _numcol(raw, "FIN_BUY_AMT", YI), "trade_ratio": _numcol(raw, "TRADE_AMT_RATIO"),
        })
    else:
        require_cols(raw, ["DIM_DATE", "RZRQYE", "RZYE"], "两融合计")
        df = pd.DataFrame({
            "date": raw["DIM_DATE"].map(to_date), "margin": _numcol(raw, "RZRQYE"),
            "fin": _numcol(raw, "RZYE"), "loan": _numcol(raw, "RQYE"),
            "fin_buy": _numcol(raw, "RZMRE"), "trade_ratio": None,
        })
    df = df.dropna(subset=["date", "margin"])
    df = df[(df["date"] <= trade_date) & (df["margin"] > 0)]
    return df.sort_values("date").drop_duplicates("date", keep="last").tail(n).reset_index(drop=True)


# ---------------------------------------------------------------- 图表（纯 JSON 的 ECharts option）

def _signed_items(values) -> list:
    return [{"value": v, "itemStyle": {"color": UP if (v or 0) >= 0 else DOWN}} for v in values]


def hbar_option(names: list, values_yi: list, label_pos: str = "right") -> dict:
    """横向柱状图（亿元），正红负绿。"""
    return {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "grid": {"left": 8, "right": 56, "top": 8, "bottom": 8, "containLabel": True},
        "xAxis": {"type": "value", "axisLabel": {"formatter": "{value}亿"}},
        # onZero=False：全是负值时类目轴仍贴在左侧，不会压在柱子上
        "yAxis": {"type": "category", "inverse": True, "data": names,
                  "axisTick": {"show": False}, "axisLine": {"onZero": False}},
        "series": [{"type": "bar", "name": "净买额(亿元)", "data": _signed_items(values_yi),
                    "barMaxWidth": 18, "itemStyle": {"color": UP},
                    "label": {"show": True, "position": label_pos, "formatter": "{c}亿", "fontSize": 11}}],
    }


def flow_option(series: pd.DataFrame) -> dict:
    """近 N 日主力净流入柱（亿元，正红负绿）+ 上证指数折线。"""
    dates = [d.strftime("%m-%d") for d in series["date"]]
    vals = [to_yi(v) for v in series["main"]]
    closes = [None if num(v) is None else round(num(v), 2) for v in series["sh_close"]]
    return {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["主力净流入(亿元)", "上证指数"], "top": 0},
        "grid": {"left": 8, "right": 8, "top": 36, "bottom": 8, "containLabel": True},
        "xAxis": {"type": "category", "data": dates},
        "yAxis": [{"type": "value", "name": "亿元"},
                  {"type": "value", "name": "上证指数", "scale": True, "splitLine": {"show": False}}],
        "series": [
            {"type": "bar", "name": "主力净流入(亿元)", "data": _signed_items(vals), "itemStyle": {"color": UP},
             "barMaxWidth": 14},
            {"type": "line", "name": "上证指数", "yAxisIndex": 1, "data": closes, "symbol": "none",
             "smooth": True, "lineStyle": {"width": 1.5, "color": "#3e63dd"}, "itemStyle": {"color": "#3e63dd"}},
        ],
    }


def northbound_option(series: pd.DataFrame) -> dict:
    """北向成交总额柱状图（亿元，y 轴从 0 开始，柱高与数值成比例）。不是方向性数据，用中性色。"""
    return {
        "tooltip": {"trigger": "axis"},
        "grid": {"left": 8, "right": 8, "top": 24, "bottom": 8, "containLabel": True},
        "xAxis": {"type": "category", "data": [d.strftime("%m-%d") for d in series["date"]]},
        "yAxis": {"type": "value", "name": "亿元", "min": 0},
        "series": [{"type": "bar", "name": "北向成交总额(亿元)", "data": [to_yi(v, 1) for v in series["amount"]],
                    "itemStyle": {"color": "#3e63dd"}, "barMaxWidth": 16}],
    }


def margin_option(series: pd.DataFrame) -> dict:
    """融资余额折线（亿元）+ 融资余额日变化柱（亿元，正红负绿）。"""
    fin = series["fin"].astype("float64")
    change = fin.diff()
    return {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["融资余额(亿元)", "较前日变化(亿元)"], "top": 0},
        "grid": {"left": 8, "right": 8, "top": 36, "bottom": 8, "containLabel": True},
        "xAxis": {"type": "category", "data": [d.strftime("%m-%d") for d in series["date"]]},
        "yAxis": [{"type": "value", "name": "余额", "scale": True},
                  {"type": "value", "name": "变化", "splitLine": {"show": False}}],
        "series": [
            {"type": "line", "name": "融资余额(亿元)", "data": [to_yi(v, 0) for v in fin], "symbol": "none",
             "smooth": True, "lineStyle": {"width": 1.8, "color": "#3e63dd"}, "itemStyle": {"color": "#3e63dd"}},
            {"type": "bar", "name": "较前日变化(亿元)", "yAxisIndex": 1, "barMaxWidth": 10,
             "data": _signed_items([to_yi(v, 1) for v in change]), "itemStyle": {"color": UP}},
        ],
    }
