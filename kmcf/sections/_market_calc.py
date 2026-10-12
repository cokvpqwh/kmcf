"""大盘与情绪：纯计算、文案与图表配置（不联网，便于单元测试）。"""

import pandas as pd

from ..section import ACCENT, DOWN, FLAT, SERIES, UP, fmt_amount, num
from ._names import clean_stock_name, normalize_industry

# 主要指数：(带市场前缀的代码, 名称)。顺序即 KPI 展示顺序
INDEXES = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("sh000688", "科创50"),
    ("sh000300", "沪深300"),
    ("sh000852", "中证1000"),
    ("bj899050", "北证50"),
]

# 东方财富涨停板专题“涨跌分布”的档位 key -> 标签（来自其页面脚本：-10 为“低于-9%(未跌停)”，10 为“高于9%(未涨停)”）
FENBU_LABELS = {-11: "跌停", -10: "<-9%", 0: "平盘", 10: ">9%", 11: "涨停"}
for _k in range(1, 10):
    FENBU_LABELS[_k] = f"{_k - 1}~{_k}%"
    FENBU_LABELS[-_k] = f"-{_k}~-{_k - 1}%" if _k > 1 else "-1~0%"
FENBU_ORDER = list(range(-11, 12))

# 多指数对比用色：避开红/绿，免得和“红涨绿跌”混淆
LINE_COLORS = ["#3e63dd", "#f59e0b", "#8e4ec6", "#0ea5e9", "#d6409f", "#64748b", "#14b8a6", "#a16207"]


# ---------------------------------------------------------------- 通用小工具

def hms(v) -> str:
    """'92500' / 92500 / '092500' -> '09:25:00'；无效值返回 '—'。"""
    if v is None:
        return "—"
    s = str(v).strip().split(".")[0]
    if not s.isdigit() or int(s) == 0:
        return "—"
    s = s.zfill(6)
    if len(s) != 6:
        return "—"
    return f"{s[:2]}:{s[2:4]}:{s[4:]}"


def zfill_code(series: pd.Series) -> pd.Series:
    return series.astype(str).str.strip().str.split(".").str[0].str.zfill(6)


def fmt_yi(v, signed: bool = False) -> str:
    """成交额差值等：≥1 万亿用“万亿”两位小数，否则用“亿”取整（如 +2181亿，不用千分位）。"""
    f = num(v)
    if f is None:
        return "—"
    sign = "+" if signed and f > 0 else "-" if f < 0 else ""
    a = abs(f)
    return f"{sign}{a / 1e12:.2f}万亿" if a >= 1e12 else f"{sign}{a / 1e8:.0f}亿"


def mmdd(date_iso: str) -> str:
    return date_iso[5:10] if isinstance(date_iso, str) and len(date_iso) >= 10 else str(date_iso)


def _round(v, digits=2):
    f = num(v)
    return None if f is None else round(f, digits)


# ---------------------------------------------------------------- 指数日线

def norm_kline(df: pd.DataFrame | None) -> pd.DataFrame:
    """统一指数日线：date('YYYY-MM-DD' 字符串) open close high low volume [amount]，按日期升序去重。"""
    cols = ["date", "open", "close", "high", "low", "volume", "amount"]
    if df is None or len(df) == 0 or "date" not in df.columns or "close" not in df.columns:
        return pd.DataFrame(columns=cols)
    out = pd.DataFrame({"date": pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")})
    for c in cols[1:]:
        out[c] = pd.to_numeric(df[c], errors="coerce") if c in df.columns else float("nan")
    out = out.dropna(subset=["date", "close"])
    return out.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)


def index_day(df: pd.DataFrame, date_iso: str) -> dict | None:
    """复盘日的收盘、涨跌幅（%）、成交额（元，可能缺失）。复盘日不在数据里返回 None。"""
    if df is None or df.empty:
        return None
    pos = df.index[df["date"] == date_iso]
    if len(pos) == 0:
        return None
    i = int(pos[0])
    close = num(df.at[i, "close"])
    prev = num(df.at[i - 1, "close"]) if i > 0 else None
    pct = (close / prev - 1) * 100 if close is not None and prev else None
    return {"close": close, "pct": pct, "prev_close": prev, "amount": num(df.at[i, "amount"])}


def upto(df: pd.DataFrame, date_iso: str) -> pd.DataFrame:
    """截至复盘日（含）的数据。"""
    if df is None or df.empty:
        return df
    return df[df["date"] <= date_iso].reset_index(drop=True)


def turnover_series(sh: pd.DataFrame, sz: pd.DataFrame) -> pd.DataFrame:
    """上证指数成交额 + 深证成指成交额 = 沪深两市成交额（元）。两边都有数据的日期才计入。"""
    if sh is None or sz is None or sh.empty or sz.empty:
        return pd.DataFrame(columns=["date", "turnover"])
    m = sh[["date", "amount"]].merge(sz[["date", "amount"]], on="date", suffixes=("_sh", "_sz"))
    m = m.dropna(subset=["amount_sh", "amount_sz"])
    m = m[(m["amount_sh"] > 0) & (m["amount_sz"] > 0)]
    return pd.DataFrame({"date": m["date"], "turnover": m["amount_sh"] + m["amount_sz"]}).reset_index(drop=True)


def sse_stock_amount(df: pd.DataFrame | None) -> float | None:
    """上交所每日成交概况（stock_sse_deal_daily）-> 股票成交金额（元）。原表单位：亿元。"""
    if df is None or df.empty or "单日情况" not in df.columns:
        return None
    row = df[df["单日情况"].astype(str).str.strip() == "成交金额"]
    if row.empty:
        return None
    row = row.iloc[0]
    v = num(row.get("股票"))
    if v is None:  # 老格式没有“股票”合计列
        parts = [num(row.get(k)) for k in ("主板A", "主板B", "科创板")]
        parts = [p for p in parts if p is not None]
        v = sum(parts) if parts else None
    return v * 1e8 if v else None


def szse_stock_amount(df: pd.DataFrame | None) -> float | None:
    """深交所证券类别统计（stock_szse_summary）-> 股票成交金额（元）。"""
    if df is None or df.empty or "证券类别" not in df.columns:
        return None
    row = df[df["证券类别"].astype(str).str.strip() == "股票"]
    if row.empty:
        return None
    v = num(row.iloc[0].get("成交金额"))
    return v if v else None


# ---------------------------------------------------------------- 涨跌家数

def parse_fenbu(raw) -> dict[int, int]:
    """getTopicZDFenBu 的 fenbu：[{"-1": 740}, {"0": 103}, ...] 或 dict -> {int档位: 家数}。"""
    out: dict[int, int] = {}
    items = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
    for d in items:
        if not isinstance(d, dict):
            continue
        for k, v in d.items():
            try:
                key = int(k)
            except (TypeError, ValueError):
                continue
            n = num(v)
            if key in FENBU_LABELS and n is not None:
                out[key] = int(n)
    return out


# 涨跌家数的统计口径（basis）：两个来源的范围不同，数值不可混用
#   em   东方财富涨停板专题“涨跌分布”：沪深京A股（含北交所），不含ST
#   legu 乐咕乐股赚钱效应：沪深A股，含ST，不含停牌
BREADTH_SCOPE = {"em": "含北交所·不含ST", "legu": "沪深A股·含ST"}
_BREADTH_KEYS = {"em": ("up_count", "down_count", "flat_count"),
                 "legu": ("up_count_legu", "down_count_legu", "flat_count_legu")}


def breadth_from_fenbu(fenbu: dict[int, int]) -> dict:
    up = sum(v for k, v in fenbu.items() if k > 0)
    down = sum(v for k, v in fenbu.items() if k < 0)
    flat = fenbu.get(0, 0)
    return {"up": up, "down": down, "flat": flat, "total": up + down + flat,
            "zt": fenbu.get(11), "dt": fenbu.get(-11), "basis": "em"}


def parse_legu(df: pd.DataFrame | None) -> dict:
    """乐咕乐股赚钱效应表（item/value 两列）-> {item: value}。数值转 float，“统计日期”保留字符串。"""
    out: dict = {}
    if df is None or df.empty or "item" not in df.columns:
        return out
    for item, value in zip(df["item"], df["value"]):
        key = str(item).strip()
        out[key] = str(value).strip() if key == "统计日期" else num(value)
    return out


def breadth_from_legu(d: dict) -> dict | None:
    up, down, flat = num(d.get("上涨")), num(d.get("下跌")), num(d.get("平盘"))
    if up is None or down is None:
        return None
    flat = flat or 0
    return {"up": int(up), "down": int(down), "flat": int(flat), "total": int(up + down + flat),
            "zt": None, "dt": None, "basis": "legu"}


def breadth_from_history(m: dict) -> dict | None:
    """history 里保存的涨跌家数（优先东方财富口径，其次乐咕口径），带 basis。"""
    for basis, (ku, kd, kf) in _BREADTH_KEYS.items():
        up, down = m.get(ku), m.get(kd)
        if up is not None and down is not None:
            flat = m.get(kf) or 0
            return {"up": up, "down": down, "flat": flat, "total": up + down + flat,
                    "zt": None, "dt": None, "basis": basis}
    return None


# ---------------------------------------------------------------- 涨停板相关股票池

_POOL_NUMERIC = ["涨跌幅", "最新价", "成交额", "流通市值", "总市值", "换手率", "封板资金", "炸板次数", "连板数",
                 "涨停价", "振幅", "涨速", "封单资金", "板上成交额", "连续跌停", "开板次数", "昨日连板数", "动态市盈率"]


# 各股票池必须有的列：非空表缺这些列说明接口/akshare 格式变了，要报错而不是当成空表（否则会静默记成 0 家）
POOL_REQUIRED = {
    "zt": ("代码", "名称", "连板数"),
    "zb": ("代码", "名称"),
    "dt": ("代码", "名称"),
    "prev": ("代码", "名称", "涨跌幅"),
}


class PoolFormatError(ValueError):
    """股票池返回了数据，但缺少必需的列。"""


def norm_pool(df: pd.DataFrame | None, kind: str | None = None) -> pd.DataFrame | None:
    """股票池统一格式：代码补齐 6 位、数值列转 float、名称去空格（粤 传 媒 → 粤传媒）、
    所属行业补全为申万二级全称并去掉“Ⅱ”（见 _names）。None 原样返回（表示未获取到）。

    真空表（0 行）返回带“代码/名称”列的空表；有数据却缺必需列时抛 PoolFormatError。
    """
    if df is None:
        return None
    if len(df) == 0:
        return pd.DataFrame(columns=["代码", "名称"])
    missing = [c for c in POOL_REQUIRED.get(kind, ("代码",)) if c not in df.columns]
    if missing:
        raise PoolFormatError("返回数据缺少" + "、".join(f"“{c}”" for c in missing) + "列")
    out = df.copy()
    out["代码"] = zfill_code(out["代码"])
    if "名称" in out.columns:
        out["名称"] = out["名称"].map(clean_stock_name)
    if "所属行业" in out.columns:
        out["所属行业"] = out["所属行业"].map(normalize_industry)
    for c in _POOL_NUMERIC:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out.reset_index(drop=True)


def pool_metrics(zt: pd.DataFrame | None, zb: pd.DataFrame | None, dt: pd.DataFrame | None,
                 prev: pd.DataFrame | None) -> dict:
    """由涨停/炸板/跌停/昨日涨停池计算情绪指标。缺哪个池，相关指标就是 None。"""
    m: dict = {}
    if zt is not None:
        m["zt_count"] = len(zt)
        if len(zt) and "连板数" in zt.columns:
            lb = pd.to_numeric(zt["连板数"], errors="coerce")
            mx = lb.max()
            m["max_board"] = int(mx) if pd.notna(mx) else None
            has_name = pd.notna(mx) and "名称" in zt.columns
            names = [clean_stock_name(x) for x in zt.loc[lb == mx, "名称"]] if has_name else []
            m["max_board_names"] = [x for x in names if x]
            m["lianban_count"] = int((lb >= 2).sum())
        elif len(zt) == 0:
            m["max_board"], m["max_board_names"], m["lianban_count"] = 0, [], 0
    if zb is not None:
        m["zb_count"] = len(zb)
    if dt is not None:
        m["dt_count"] = len(dt)
        if len(dt) == 0:
            m["dt_lianxu"] = 0
        elif "连续跌停" in dt.columns:
            m["dt_lianxu"] = int((pd.to_numeric(dt["连续跌停"], errors="coerce") >= 2).sum())
    if zt is not None and zb is not None and len(zt) + len(zb) > 0:
        m["zb_rate"] = len(zb) / (len(zt) + len(zb)) * 100
    if prev is not None and len(prev) and "涨跌幅" in prev.columns:
        pct = pd.to_numeric(prev["涨跌幅"], errors="coerce").dropna()
        m["prev_zt_count"] = len(prev)
        if len(pct):
            m["prev_zt_avg_pct"] = float(pct.mean())
            m["prev_zt_up_ratio"] = float((pct > 0).mean() * 100)
        if zt is not None:
            kept = set(prev["代码"]) & set(zt["代码"]) if len(zt) else set()
            m["promote_rate"] = len(kept) / len(prev) * 100
    return m


def hhmmss(v) -> str:
    """'92512' / 92512 / '092512' / 92512.0 -> '092512'；无效值返回 ''。"""
    if v is None:
        return ""
    s = str(v).strip().split(".")[0]
    if not s.isdigit() or int(s) == 0 or len(s) > 6:
        return ""
    return s.zfill(6)


def is_yizi(first_time, last_time, zb_count) -> bool:
    """一字板：首次封板在 09:25 集合竞价（09:25:xx 都算），最后封板也在 09:25（缺失时不核对），且炸板 0 次。"""
    first, last = hhmmss(first_time), hhmmss(last_time)
    return (first.startswith("0925") and (not last or last.startswith("0925"))
            and (num(zb_count, 0) or 0) == 0)


def ladder_levels(zt: pd.DataFrame | None, first_board_limit: int = 30) -> list[dict]:
    """连板梯队：按连板数从高到低分组，组内按首次封板时间先后。首板太多时只列前若干只。"""
    if zt is None or zt.empty or "连板数" not in zt.columns:
        return []
    df = zt.copy()
    df["_lb"] = pd.to_numeric(df["连板数"], errors="coerce").fillna(1).astype(int)
    df["_fbt"] = df.get("首次封板时间", pd.Series("", index=df.index)).map(hhmmss)
    df["_lbt"] = df.get("最后封板时间", pd.Series("", index=df.index)).map(hhmmss)
    levels = []
    for lb in sorted(df["_lb"].unique(), reverse=True):
        g = df[df["_lb"] == lb].sort_values("_fbt")
        items = []
        for _, r in g.iterrows():
            industry = normalize_industry(r.get("所属行业"))
            when = "一字" if is_yizi(r["_fbt"], r["_lbt"], r.get("炸板次数")) else hms(r["_fbt"])[:5]
            sub = " · ".join(x for x in (industry, when) if x and x != "—")
            name = clean_stock_name(r.get("名称")) or clean_stock_name(r.get("代码"))
            items.append({"text": name, "sub": sub, "tone": "up"})
        if lb <= 1 and len(items) > first_board_limit:
            rest = len(items) - first_board_limit
            items = items[:first_board_limit] + [{"text": f"…另有{rest}只", "sub": f"共{len(g)}只", "tone": "neutral"}]
        levels.append({"label": f"{lb}板" if lb >= 2 else "首板", "items": items})
    return levels


def zttj_text(v) -> str:
    """涨停统计 'days/ct'（如 '4/3'）-> '4天3板'。"""
    s = str(v or "").strip()
    parts = s.split("/")
    if len(parts) == 2 and all(p.strip().isdigit() for p in parts):
        return f"{parts[0].strip()}天{parts[1].strip()}板"
    return s or "—"


def _sorted(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    keys = [c for c in by if c in df.columns]
    return df.sort_values(keys, ascending=False, kind="stable") if keys else df


def zt_rows(zt: pd.DataFrame | None) -> list[dict]:
    """涨停池表格行：按连板数、封板资金从高到低。"""
    if zt is None or zt.empty:
        return []
    out = []
    for _, r in _sorted(zt, ["连板数", "封板资金"]).iterrows():
        out.append({"name": clean_stock_name(r.get("名称")), "code": r.get("代码"), "lb": r.get("连板数"),
                    "tj": zttj_text(r.get("涨停统计")), "pct": r.get("涨跌幅"), "amount": r.get("成交额"),
                    "fund": r.get("封板资金"), "fbt": hms(r.get("首次封板时间")), "lbt": hms(r.get("最后封板时间")),
                    "zbc": r.get("炸板次数"), "hs": r.get("换手率"), "ltsz": r.get("流通市值"),
                    "hy": normalize_industry(r.get("所属行业"))})
    return out


def zb_rows(zb: pd.DataFrame | None) -> list[dict]:
    """炸板池表格行：按涨跌幅从高到低。"""
    if zb is None or zb.empty:
        return []
    return [{"name": clean_stock_name(r.get("名称")), "code": r.get("代码"), "pct": r.get("涨跌幅"),
             "amount": r.get("成交额"), "fbt": hms(r.get("首次封板时间")), "zbc": r.get("炸板次数"), "zf": r.get("振幅"),
             "hy": normalize_industry(r.get("所属行业"))}
            for _, r in _sorted(zb, ["涨跌幅"]).iterrows()]


def dt_rows(dt: pd.DataFrame | None) -> list[dict]:
    """跌停池表格行：按连续跌停天数、封单资金从高到低。"""
    if dt is None or dt.empty:
        return []
    return [{"name": clean_stock_name(r.get("名称")), "code": r.get("代码"), "pct": r.get("涨跌幅"),
             "amount": r.get("成交额"), "fund": r.get("封单资金"), "days": r.get("连续跌停"), "oc": r.get("开板次数"),
             "lbt": hms(r.get("最后封板时间")), "hy": normalize_industry(r.get("所属行业"))}
            for _, r in _sorted(dt, ["连续跌停", "封单资金"]).iterrows()]


# ---------------------------------------------------------------- 历史指标（data/history.csv）

SENTIMENT_KEYS = ("zt_count", "zb_count", "dt_count", "max_board")   # 缺任何一个就需要回填
_HIST_INT = {"zt_count", "zb_count", "dt_count", "max_board", "lianban_count", "dt_lianxu",
             "up_count", "down_count", "flat_count", "up_count_legu", "down_count_legu", "flat_count_legu"}
_HIST_FLOAT = {"zb_rate", "promote_rate", "prev_zt_avg_pct", "prev_zt_up_ratio", "turnover"}
NAMES_KEY = "max_board_names"   # 最高连板股名称，用“、”连接后存成文本
_NAMES_SEP = "、"

# 情绪指标的中文名（说明“哪些指标取自本地历史记录”时用）
METRIC_LABELS = {
    "zt_count": "涨停家数", "dt_count": "跌停家数", "zb_count": "炸板家数", "zb_rate": "炸板率",
    "max_board": "最高连板", "lianban_count": "连板股数", "dt_lianxu": "连续跌停家数",
    "promote_rate": "连板晋级率", "prev_zt_avg_pct": "昨日涨停今日均涨幅", "prev_zt_up_ratio": "昨日涨停收涨比例",
}


def to_history(metrics: dict, breadth: dict | None = None, turnover: float | None = None) -> dict:
    """要写入 history 的指标（只含有值的键；比率保留 2 位小数）。

    涨跌家数按口径写到不同的列（东方财富 up_count…；乐咕 up_count_legu…），避免来源切换时趋势跳变。
    """
    out = {}
    for k in _HIST_INT | _HIST_FLOAT:
        v = num(metrics.get(k))
        if v is not None:
            out[k] = int(round(v)) if k in _HIST_INT else round(v, 2)
    names = metrics.get(NAMES_KEY)
    if isinstance(names, (list, tuple)) and names:
        out[NAMES_KEY] = _NAMES_SEP.join(clean_stock_name(x) for x in names)
    if breadth:
        keys = _BREADTH_KEYS.get(breadth.get("basis") or "em", _BREADTH_KEYS["em"])
        for src, dst in zip(("up", "down", "flat"), keys):
            if num(breadth.get(src)) is not None:
                out[dst] = int(breadth[src])
    if num(turnover) is not None:
        out["turnover"] = round(float(turnover), 0)
    return out


def from_history(row: dict | None) -> dict:
    """history 的一行 -> 指标 dict（键名与 pool_metrics 一致，缺失的键不出现）。"""
    out = {}
    for k, v in (row or {}).items():
        if k == NAMES_KEY:
            if isinstance(v, str) and v.strip():
                out[k] = [n for n in (clean_stock_name(x) for x in v.split(_NAMES_SEP)) if n]
            continue
        f = num(v)
        if f is None:
            continue
        if k in _HIST_INT:
            out[k] = int(round(f))
        elif k in _HIST_FLOAT:
            out[k] = f
    return out


def needs_backfill(row: dict | None) -> bool:
    m = from_history(row)
    return any(k not in m for k in SENTIMENT_KEYS)


# ---------------------------------------------------------------- 图表配置（纯 JSON）

def _ma(values: pd.Series, n: int) -> list:
    return [_round(v) for v in values.rolling(n).mean()]


def kline_option(df: pd.DataFrame, name: str, days: int = 60) -> dict:
    """K 线 + 均线 + 下方量能柱（有成交额用成交额，否则用成交量）。df 为 norm_kline 结果（含更早数据以算均线）。"""
    full = df.reset_index(drop=True)
    ma = {n: _ma(full["close"], n) for n in (5, 10, 20)}
    tail = full.tail(days).reset_index(drop=True)
    start = len(full) - len(tail)
    dates = tail["date"].tolist()
    candles = [[_round(r.open), _round(r.close), _round(r.low), _round(r.high)] for r in tail.itertuples()]
    use_amount = tail["amount"].notna().all() and (tail["amount"] > 0).all()
    bar_name = "成交额(亿元)" if use_amount else "成交量(亿股)"
    bars = []
    for r in tail.itertuples():
        v = r.amount / 1e8 if use_amount else (r.volume / 1e8 if pd.notna(r.volume) else None)
        color = UP if r.close >= r.open else DOWN
        bars.append({"value": _round(v, 1), "itemStyle": {"color": color}})
    ma_series = [{"type": "line", "name": f"MA{n}", "data": ma[n][start:], "smooth": True, "showSymbol": False,
                  "lineStyle": {"width": 1}, "color": SERIES[i + 2]} for i, n in enumerate((5, 10, 20))]
    return {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "cross"}},
        "axisPointer": {"link": [{"xAxisIndex": "all"}]},
        "legend": {"data": [name, "MA5", "MA10", "MA20"], "top": 0},
        "grid": [{"left": 60, "right": 20, "top": 30, "height": "58%"},
                 {"left": 60, "right": 20, "top": "76%", "height": "16%"}],
        "xAxis": [{"type": "category", "data": dates, "boundaryGap": True, "axisLabel": {"show": False}},
                  {"type": "category", "gridIndex": 1, "data": dates, "boundaryGap": True}],
        "yAxis": [{"scale": True, "splitNumber": 4},
                  {"gridIndex": 1, "scale": True, "splitNumber": 2, "name": bar_name,
                   "nameTextStyle": {"align": "left"}}],
        # 按住 Ctrl 滚轮缩放，避免普通滚动页面时误缩放图表
        "dataZoom": [{"type": "inside", "xAxisIndex": [0, 1], "start": 0, "end": 100, "zoomOnMouseWheel": "ctrl"}],
        "series": [{"type": "candlestick", "name": name, "data": candles,
                    "itemStyle": {"color": UP, "color0": DOWN, "borderColor": UP, "borderColor0": DOWN}},
                   *ma_series,
                   {"type": "bar", "name": bar_name, "xAxisIndex": 1, "yAxisIndex": 1, "data": bars}],
    }


def normalized_option(frames: dict[str, pd.DataFrame], base_dates: list[str]) -> dict:
    """多指数以区间首日为基准的累计涨跌幅（%）。"""
    series, names = [], []
    for i, (name, df) in enumerate(frames.items()):
        s = df.set_index("date")["close"].reindex(base_dates)
        first = s.dropna()
        if first.empty:
            continue
        base = first.iloc[0]
        data = [None if pd.isna(v) else round((v / base - 1) * 100, 2) for v in s]
        series.append({"type": "line", "name": name, "data": data, "showSymbol": False, "smooth": True,
                       "connectNulls": True, "color": LINE_COLORS[i % len(LINE_COLORS)], "lineStyle": {"width": 1.5}})
        names.append(name)
    return {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": names, "top": 0, "itemWidth": 14, "itemHeight": 8, "itemGap": 8,
                   "textStyle": {"fontSize": 11}},
        "grid": {"left": 50, "right": 20, "top": 52, "bottom": 28},
        "xAxis": {"type": "category", "data": [mmdd(d) for d in base_dates], "boundaryGap": False},
        "yAxis": {"type": "value", "scale": True, "axisLabel": {"formatter": "{value}%"}},
        "series": series,
    }


def turnover_option(dates: list[str], values: list) -> dict:
    """两市成交额柱（亿元）+ 5 日均线。values 单位：元，可含 None。"""
    s = pd.Series([num(v) for v in values], dtype="float64") / 1e8
    bars = [_round(v, 0) for v in s]
    ma5 = [_round(v, 0) for v in s.rolling(5, min_periods=5).mean()]
    return {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["成交额(亿元)", "5日均值"], "top": 0},
        "grid": {"left": 60, "right": 20, "top": 36, "bottom": 28},
        "xAxis": {"type": "category", "data": [mmdd(d) for d in dates]},
        "yAxis": {"type": "value", "min": 0},
        "series": [{"type": "bar", "name": "成交额(亿元)", "data": bars, "color": ACCENT, "barMaxWidth": 14},
                   {"type": "line", "name": "5日均值", "data": ma5, "showSymbol": False, "smooth": True,
                    "color": SERIES[2], "connectNulls": True}],
    }


def fenbu_option(fenbu: dict[int, int]) -> dict:
    cats, data = [], []
    for k in FENBU_ORDER:
        cats.append(FENBU_LABELS[k])
        color = UP if k > 0 else DOWN if k < 0 else FLAT
        data.append({"value": int(fenbu.get(k, 0)), "itemStyle": {"color": color}})
    return {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "grid": {"left": 44, "right": 12, "top": 24, "bottom": 46},
        "xAxis": {"type": "category", "data": cats, "axisLabel": {"interval": 0, "rotate": 40, "fontSize": 11}},
        "yAxis": {"type": "value"},
        "series": [{"type": "bar", "name": "家数", "data": data, "barMaxWidth": 28,
                    "label": {"show": True, "position": "top", "fontSize": 10}}],
    }


def limits_trend_option(dates: list[str], zt: list, dt: list, zb_rate: list) -> dict:
    return {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["涨停", "跌停", "炸板率"], "top": 0},
        "grid": {"left": 44, "right": 48, "top": 36, "bottom": 28},
        "xAxis": {"type": "category", "data": [mmdd(d) for d in dates]},
        "yAxis": [{"type": "value", "name": "家数"},
                  {"type": "value", "name": "炸板率", "axisLabel": {"formatter": "{value}%"},
                   "splitLine": {"show": False}, "min": 0}],
        "series": [{"type": "bar", "name": "涨停", "data": [_int(v) for v in zt], "color": UP, "barMaxWidth": 12},
                   {"type": "bar", "name": "跌停", "data": [_int(v) for v in dt], "color": DOWN, "barMaxWidth": 12},
                   {"type": "line", "name": "炸板率", "yAxisIndex": 1, "data": [_round(v, 1) for v in zb_rate],
                    "color": SERIES[2], "connectNulls": True, "symbolSize": 5}],
    }


def board_trend_option(dates: list[str], max_board: list, prev_avg: list) -> dict:
    return {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["最高连板", "昨日涨停今日均涨幅"], "top": 0},
        "grid": {"left": 44, "right": 48, "top": 36, "bottom": 28},
        "xAxis": {"type": "category", "data": [mmdd(d) for d in dates]},
        "yAxis": [{"type": "value", "name": "板数", "minInterval": 1},
                  {"type": "value", "name": "均涨幅", "axisLabel": {"formatter": "{value}%"},
                   "splitLine": {"show": False}}],
        "series": [{"type": "bar", "name": "最高连板", "data": [_int(v) for v in max_board], "color": UP,
                    "barMaxWidth": 12, "label": {"show": True, "position": "top", "fontSize": 10}},
                   {"type": "line", "name": "昨日涨停今日均涨幅", "yAxisIndex": 1,
                    "data": [_round(v, 2) for v in prev_avg], "color": ACCENT, "connectNulls": True,
                    "symbolSize": 5}],
    }


def trim_leading_empty(dates: list[str], rows: list[dict], key: str) -> tuple[list[str], list[dict]]:
    """去掉开头 key 缺失的日期（回填窗口之前没有数据的部分），中间缺失保留为空。"""
    for i, r in enumerate(rows):
        if r.get(key) is not None:
            return dates[i:], rows[i:]
    return [], []


def _int(v):
    f = num(v)
    return None if f is None else int(round(f))


# ---------------------------------------------------------------- 复盘要点（客观描述，不含任何建议/预测）

def _pct_text(p: float) -> str:
    """带符号百分数（+0.52% / -1.20%）；0 不带符号。"""
    return "0.00%" if round(p, 2) == 0 else f"{p:+.2f}%"


def index_summary(idx: list[dict]) -> str | None:
    """idx: [{"name", "close", "pct"}]，只含复盘日有数据的指数。涨跌按两位小数（与展示一致）判断。"""
    ok = [{**x, "pct": round(x["pct"], 2)} for x in idx if x.get("pct") is not None]
    if not ok:
        return None
    first = ok[0]
    head = f"{first['name']}收于{first['close']:.2f}点（{_pct_text(first['pct'])}）"
    shown = ok[:3]   # 前面已写出涨跌幅的指数，后面比较最大/最小时不再重复数字
    others = "、".join(f"{x['name']}{_pct_text(x['pct'])}" for x in shown[1:])
    text = head + (f"，{others}" if others else "")
    if len(ok) >= 3:
        n_up = sum(1 for x in ok if x["pct"] > 0)
        n_down = sum(1 for x in ok if x["pct"] < 0)
        n_flat = len(ok) - n_up - n_down
        if n_up == len(ok):
            breadth = "全部上涨"
        elif n_down == len(ok):
            breadth = "全部下跌"
        elif n_flat == len(ok):
            breadth = "全部持平"
        else:
            breadth = f"{n_up}涨{n_down}跌" + (f"{n_flat}平" if n_flat else "")
        text += f"。{len(ok)}个主要指数{breadth}"
        best = max(ok, key=lambda x: x["pct"])
        worst = min(ok, key=lambda x: x["pct"])
        names_shown = {x["name"] for x in shown}

        def which(x: dict, word: str) -> str:
            pct = "" if x["name"] in names_shown else f"（{_pct_text(x['pct'])}）"
            return f"{x['name']}{word}{pct}"

        parts = []
        if best["pct"] != worst["pct"]:   # 全部相同就不比较
            if best["pct"] > 0:
                parts.append(which(best, "涨幅最大"))
            elif best["pct"] < 0:
                parts.append(which(best, "跌幅最小"))
            if worst["pct"] < 0:
                parts.append(which(worst, "跌幅最大"))
            elif worst["pct"] > 0:
                parts.append(which(worst, "涨幅最小"))
        if parts:
            text += "，" + "，".join(parts)
    return text + "。"


def turnover_summary(turnover: float | None, prev: float | None, series: list | None = None) -> str | None:
    if turnover is None:
        return None
    text = f"沪深两市成交额{fmt_amount(turnover)}"
    if prev:
        diff = turnover - prev
        word = "放量" if diff > 0 else "缩量" if diff < 0 else "持平"
        text += f"，较上一交易日{word}" + (f"{fmt_yi(abs(diff))}（{diff / prev * 100:+.1f}%）" if diff else "")
    vals = [num(v) for v in (series or []) if num(v) is not None]
    if len(vals) >= 20:
        if turnover >= max(vals):
            text += f"，为近{len(vals)}个交易日最高"
        elif turnover <= min(vals):
            text += f"，为近{len(vals)}个交易日最低"
    return text + "。"


def breadth_summary(b: dict | None) -> str | None:
    if not b or not b.get("total"):
        return None
    ratio = b["up"] / b["total"] * 100
    return f"上涨{b['up']}家、下跌{b['down']}家、平盘{b['flat']}家，上涨家数占比{ratio:.0f}%。"


def limits_summary(m: dict) -> str | None:
    if m.get("zt_count") is None:
        return None
    parts = [f"涨停{m['zt_count']}家"]
    if m.get("dt_count") is not None:
        parts.append(f"跌停{m['dt_count']}家")
    text = "、".join(parts) + "（不含ST）"
    if m.get("zb_count") is not None and m.get("zb_rate") is not None:
        text += f"，炸板{m['zb_count']}家，炸板率{m['zb_rate']:.1f}%"
    return text + "。"


def breadth_limits_summary(b: dict | None, m: dict) -> str | None:
    """涨跌家数与涨跌停合成一句（报告顶部“今日要点”每个模块只取前 3 条，涨跌停要能进前 3）。"""
    parts = [s.rstrip("。") for s in (breadth_summary(b), limits_summary(m)) if s]
    return "；".join(parts) + "。" if parts else None


def board_summary(m: dict) -> str | None:
    mb = m.get("max_board")
    if mb is None:
        return None
    if mb <= 1:
        text = "当日无连板股，涨停股均为首板"
    else:
        names = m.get("max_board_names") or []
        shown = "、".join(names[:3]) + (f"等{len(names)}只" if len(names) > 3 else "")
        text = f"最高连板{mb}板" + (f"：{shown}" if shown else "")   # 名称可能缺失（如只有历史记录的数值）
        if m.get("lianban_count") is not None:
            text += f"；连板股{m['lianban_count']}家"
    if m.get("promote_rate") is not None:
        text += f"，连板晋级率{m['promote_rate']:.1f}%"
    return text + "。"


def money_effect_summary(m: dict) -> str | None:
    if m.get("prev_zt_avg_pct") is None:
        return None
    text = f"昨日涨停股今日平均涨幅{m['prev_zt_avg_pct']:+.2f}%"
    if m.get("prev_zt_up_ratio") is not None:
        text += f"，其中{m['prev_zt_up_ratio']:.0f}%收涨"
    return text + "。"
