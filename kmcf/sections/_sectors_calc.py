"""板块轮动：纯计算部分（不联网），便于单元测试。

标准板块表（各数据源统一成这些列）：
    name 板块名  pct 涨跌幅(%)  amount 成交额(元)  net 净流入(元)
    up/down 上涨/下跌家数  lead 领涨股  lead_pct 领涨股涨跌幅(%)  count 成分股数  code 板块代码
"""

import math

import pandas as pd

from ..section import DOWN, FLAT, UP, fmt_amount, fmt_pct, num
from ._names import clean_stock_name, normalize_industry

STD_COLS = ["name", "pct", "amount", "net", "up", "down", "lead", "lead_pct", "count", "code"]
TEXT_COLS = {"name", "lead", "code"}

# 各数据源原始列 -> 标准列；SCALE 为换算到“元”的倍数
EM_MAP = {"name": "f14", "code": "f12", "pct": "f3", "amount": "f6", "net": "f62",
          "up": "f104", "down": "f105", "lead": "f128", "lead_pct": "f136"}
THS_INDUSTRY_MAP = {"name": "板块", "pct": "涨跌幅", "amount": "总成交额", "net": "净流入",
                    "up": "上涨家数", "down": "下跌家数", "lead": "领涨股", "lead_pct": "领涨股-涨跌幅"}
THS_INDUSTRY_SCALE = {"amount": 1e8, "net": 1e8}      # 同花顺行业一览：成交额、净流入单位为亿元
THS_CONCEPT_MAP = {"name": "行业", "pct": "行业-涨跌幅", "net": "净额", "lead": "领涨股",
                   "lead_pct": "领涨股-涨跌幅", "count": "公司家数"}
THS_CONCEPT_SCALE = {"net": 1e8}                      # 同花顺资金流（概念/行业）：净额单位为亿元
THS_FLOW_MAP = THS_CONCEPT_MAP                        # 同花顺行业资金流与概念资金流列名相同
SINA_MAP = {"name": "板块", "code": "label", "pct": "涨跌幅", "amount": "总成交额",
            "lead": "股票名称", "lead_pct": "个股-涨跌幅", "count": "公司家数"}
SINA_INDUSTRY_EXCLUDE = frozenset({"次新股"})        # 新浪行业里的非行业项

# 非题材类概念（交易属性、指数成分、资金持仓、财务预告、昨日涨停等统计标签），排行时剔除
EXCLUDE_CONCEPTS = frozenset({
    "融资融券", "转融券标的", "深股通", "沪股通", "MSCI中国", "MSCI概念", "标准普尔", "标普道琼斯A股",
    "富时罗素", "富时罗素概念", "富时罗素概念股", "转债标的", "预盈预增", "预亏预减", "扭亏为盈",
    "业绩预升", "业绩预降", "昨日涨停", "昨日涨停_含一字", "昨日连板", "昨日连板_含一字", "昨日触板",
    "昨日首板", "QFII重仓", "社保重仓", "基金重仓", "机构重仓", "证金持股", "汇金持股", "AH股", "AB股",
    "GDR", "B股", "含GDR", "含B股", "含H股", "ST股", "ST板块", "准ST股", "次新股", "注册制次新股",
    "新股与次新股", "破净股", "低价股", "百元股", "高送转", "送转潜力", "股权激励", "股权转让", "举牌",
    "壳资源", "本月解禁", "未股改", "茅指数", "宁组合", "HS300_", "上证50_", "上证180_", "上证380",
    "中证500", "深成500", "央视50_", "央企50", "科创50", "三板精选", "创业成份", "创业板综",
    "参股银行", "参股券商", "参股保险", "参股期货", "参股新三板", "参股金融", "金融参股", "外资背景",
    "大盘股", "中盘股", "小盘股", "微盘股", "超大盘", "中字头股票", "高股息精选", "超级品牌",
})
EXCLUDE_KEYWORDS = ("昨日", "融资融券", "股通", "MSCI", "标准普尔", "标普", "富时罗素", "转债标的",
                    "预增", "预减", "预亏", "预盈", "预升", "预降", "次新", "摘帽", "ST股", "ST板块",
                    "参股", "高股息", "重仓", "持股", "同花顺", "_")


# ---------------------------------------------------------------- 标准化

def to_num(s: pd.Series) -> pd.Series:
    """转数值；字符串里的 % 和千分位逗号先去掉。"""
    if pd.api.types.is_numeric_dtype(s):
        return pd.to_numeric(s, errors="coerce")
    t = s.astype(object).where(s.notna(), None)
    t = t.map(lambda v: None if v is None else str(v).replace("%", "").replace(",", "").strip())
    return pd.to_numeric(t, errors="coerce")


def standardize(df: pd.DataFrame | None, mapping: dict, scale: dict | None = None) -> pd.DataFrame:
    """按 {标准列: 原始列} 取列、转数值、换算单位；去掉无名称/重复的行，按涨跌幅降序。"""
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=STD_COLS)
    src = df.reset_index(drop=True)
    out = {}
    for key in STD_COLS:
        raw = mapping.get(key)
        col = src[raw] if raw is not None and raw in src.columns else pd.Series([None] * len(src))
        if key in TEXT_COLS:
            col = col.astype(object).map(lambda v: "" if v is None or (isinstance(v, float) and math.isnan(v))
                                         else str(v).strip())
            col = col.replace({"-": "", "nan": "", "None": ""})
            if key == "lead":   # 领涨股简称：新浪等把三字简称写成“粤 传 媒”
                col = col.map(clean_stock_name)
            out[key] = col.values
        else:
            col = to_num(col)
            if scale and key in scale:
                col = col * scale[key]
            out[key] = col.values
    res = pd.DataFrame(out, columns=STD_COLS)
    res = res[res["name"] != ""].drop_duplicates("name")
    return res.sort_values("pct", ascending=False, na_position="last").reset_index(drop=True)


def is_theme_concept(name) -> bool:
    s = str(name or "").strip()
    if not s or s in EXCLUDE_CONCEPTS:
        return False
    return not any(k in s for k in EXCLUDE_KEYWORDS)


def filter_concepts(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    return df[df["name"].map(is_theme_concept)].reset_index(drop=True)


def has_values(df: pd.DataFrame | None, col: str) -> bool:
    """该列是否有内容（文本列的空字符串不算）。"""
    if df is None or col not in df.columns:
        return False
    vals = df[col].dropna()
    if col in TEXT_COLS:
        vals = vals[vals.astype(str).str.strip() != ""]
    return not vals.empty


def ranked(df: pd.DataFrame, col: str = "pct") -> pd.DataFrame:
    """按 col 降序并加 rank 列（1 起）。"""
    d = df[df[col].notna()].sort_values(col, ascending=False).reset_index(drop=True)
    return d.assign(rank=range(1, len(d) + 1))


def top_bottom(df: pd.DataFrame, col: str, n_top: int, n_bottom: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """按 col 取前 n_top 和后 n_bottom（不重叠）。后者按 col 降序排列（最小的在最后）。"""
    d = df[df[col].notna()].sort_values(col, ascending=False).reset_index(drop=True)
    top = d.head(n_top)
    rest = d.iloc[len(top):]
    bottom = rest.tail(n_bottom) if n_bottom > 0 else rest.iloc[0:0]
    return top, bottom


def flow_split(df: pd.DataFrame, n: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """净流入前 n（>0）与净流出前 n（<0，最小的在最后）。"""
    d = df[df["net"].notna()].sort_values("net", ascending=False)
    inflow = d[d["net"] > 0].head(n)
    outflow = d[d["net"] < 0].tail(n)
    return inflow.reset_index(drop=True), outflow.reset_index(drop=True)


# ---------------------------------------------------------------- 涨停股行业分布

def zt_by_industry(zt: pd.DataFrame | None) -> pd.DataFrame:
    """涨停池按“所属行业”汇总：industry, count, max_board, stocks[(名称, 连板数)]，按家数降序。

    行业名按申万二级全称补全、去掉“Ⅱ”（见 _names.normalize_industry），缺失的归入“其他”；
    股票简称去掉空格（见 _names.clean_stock_name），缺失时用代码。
    """
    cols = ["industry", "count", "max_board", "stocks"]
    if zt is None or zt.empty or "所属行业" not in zt.columns:
        return pd.DataFrame(columns=cols)
    ind = zt["所属行业"].astype(object).map(lambda v: normalize_industry(v) or "其他")
    boards = to_num(zt["连板数"]) if "连板数" in zt.columns else pd.Series([1] * len(zt))
    first = zt["首次封板时间"].astype(str) if "首次封板时间" in zt.columns else pd.Series([""] * len(zt))
    codes = zt["代码"].astype(object) if "代码" in zt.columns else pd.Series([""] * len(zt))
    names = zt["名称"].astype(object) if "名称" in zt.columns else pd.Series([""] * len(zt))
    names = [clean_stock_name(n) or clean_stock_name(c) for n, c in zip(names, codes)]
    d = pd.DataFrame({"industry": ind.values, "boards": boards.fillna(1).astype(int).values,
                      "first": first.values, "name": names})
    rows = []
    for industry, g in d.groupby("industry", sort=False):
        g = g.sort_values(["boards", "first"], ascending=[False, True])
        rows.append({"industry": industry, "count": len(g), "max_board": int(g["boards"].max()),
                     "stocks": list(zip(g["name"], g["boards"]))})
    out = pd.DataFrame(rows, columns=cols)
    return out.sort_values(["count", "max_board"], ascending=False).reset_index(drop=True)


def zt_matrix(pools: dict) -> pd.DataFrame:
    """{日期: 涨停池 DataFrame 或 None} -> 行业 × 日期 的涨停家数（该日数据缺失则整列为 NaN）。

    有“所属行业”列但 0 行的表视为当日无涨停（整列为 0）。
    """
    dates = list(pools)
    counts = {}
    for d, df in pools.items():
        if df is None or "所属行业" not in df.columns:
            continue
        counts[d] = zt_by_industry(df).set_index("industry")["count"].astype(float)
    if not counts:
        return pd.DataFrame(columns=dates, dtype=float)
    m = pd.DataFrame(counts).reindex(columns=dates)
    for d in counts:
        m[d] = m[d].fillna(0)
    return m.astype(float)


def top_ties(df: pd.DataFrame, value_col: str, name_col: str) -> list[str]:
    """与第一名数值相同的所有名称（df 已按 value_col 降序）。"""
    if df is None or df.empty:
        return []
    top = df[value_col].iloc[0]
    return list(df.loc[df[value_col] == top, name_col])


# ---------------------------------------------------------------- 轮动 / 持续性

def streak_info(values, cond=lambda v: v > 0) -> tuple[int, bool]:
    """从最后一个值往前数连续满足 cond 的个数，以及“实际可能更长”标记。

    一直数到窗口起点、或在缺失值处停下时，更早的情况未知，标记为 True（显示为“≥N”）。
    """
    n = 0
    for v in reversed(list(values)):
        f = num(v)
        if f is None:
            return n, n > 0
        if not cond(f):
            return n, False
        n += 1
    return n, n > 0


def streak(values, cond=lambda v: v > 0) -> int:
    """从最后一个值往前数，连续满足 cond 的个数（遇到缺失或不满足即停）。"""
    return streak_info(values, cond)[0]


def fmt_streak(n: int, open_ended: bool) -> str:
    return f"≥{n}" if open_ended and n > 0 else str(n)


def streak_words(n: int, open_ended: bool) -> str:
    """文字描述用：'3日' / '至少5日'。"""
    return f"至少{n}日" if open_ended and n > 0 else f"{n}日"


def compound(pcts) -> float | None:
    """多日涨跌幅（%）复合累计；忽略缺失值，全缺失返回 None。"""
    vals = [num(v) for v in pcts]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    acc = 1.0
    for v in vals:
        acc *= 1 + v / 100
    return (acc - 1) * 100


def rotation_rows(pct_m: pd.DataFrame, rank_m: pd.DataFrame | None, top: int = 10) -> list[dict]:
    """热力图各行的持续性统计。pct_m/rank_m：行=板块，列=日期（升序，最后一列为复盘日）。"""
    out = []
    full_dates = [] if rank_m is None else [c for c in rank_m.columns if rank_m[c].notna().any()]
    for name in pct_m.index:
        row = pct_m.loc[name]
        avail = [v for v in row.tolist() if num(v) is not None]
        n_up, up_open = streak_info(row.tolist())
        item = {
            "name": name,
            "pct": num(row.iloc[-1]) if len(row) else None,
            "cum": compound(avail) if len(avail) == len(row) else None,
            "up_days": f"{sum(1 for v in avail if num(v) > 0)}/{len(avail)}" if avail else "—",
            "streak": n_up, "streak_open": up_open, "streak_txt": fmt_streak(n_up, up_open),
            "n_days": len(avail),
        }
        if full_dates:
            # 只看有完整排行的日期；矩阵列仍按完整窗口取，未记录的日期为缺失，连续天数在此处“开口”
            ranks = (rank_m.loc[name] if name in rank_m.index else pd.Series([None] * len(rank_m.columns))).tolist()
            item["top_hits"] = int(sum(1 for r in ranks if num(r) is not None and num(r) <= top))
            n_top, top_open = streak_info(ranks, lambda r: r <= top)
            item.update(top_streak=n_top, top_streak_open=top_open, top_streak_txt=fmt_streak(n_top, top_open))
            item["rank"] = num(rank_m.at[name, full_dates[-1]]) if name in rank_m.index else None
        out.append(item)
    return out


def presence(rank_m: pd.DataFrame | None, top: int = 10) -> pd.DataFrame:
    """各板块在已记录的完整交易日里进入前 top 的次数与当前连续上榜天数（最后一列为最新日）。

    rank_m 的列是完整观察窗口（未记录的日期整列缺失）；连续上榜一直延续到窗口起点或未记录的日期时，
    streak_open=True（实际天数可能更长）。
    """
    cols = ["name", "hits", "streak", "streak_open", "days"]
    if rank_m is None or rank_m.empty:
        return pd.DataFrame(columns=cols)
    full = [c for c in rank_m.columns if rank_m[c].notna().any()]
    if not full:
        return pd.DataFrame(columns=cols)
    rows = []
    for name in rank_m.index:
        ranks = rank_m.loc[name].tolist()
        hits = sum(1 for r in ranks if num(r) is not None and num(r) <= top)
        if hits:
            n, open_ended = streak_info(ranks, lambda r: r <= top)
            rows.append({"name": name, "hits": hits, "streak": n, "streak_open": open_ended, "days": len(full)})
    res = pd.DataFrame(rows, columns=cols)
    return res.sort_values(["streak", "hits"], ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------- 图表 option（纯 JSON）

def _r(v, digits=2):
    f = num(v)
    return None if f is None else round(f, digits)


def _fixed(f: float, digits: int) -> str:
    """数值写成定点小数字符串：ECharts 标签会原样显示（4.00 而不是 4），坐标和色阶仍按数值计算。"""
    return f"{f:.{digits}f}"


def hbar_option(names: list, values: list, unit: str = "%", digits: int = 2, color: str | None = None,
                name: str = "") -> dict:
    """横向条形图（红涨绿跌），第一项在最上方。values 中 None 表示分隔用的空行。"""
    items, has_neg = [], False
    for v in values:
        f = _r(v, digits)
        if f is None:
            items.append("-")
            continue
        c = color or (UP if f > 0 else DOWN if f < 0 else FLAT)
        item = {"value": _fixed(f, digits), "itemStyle": {"color": c}}
        if f < 0:
            has_neg = True
            item["label"] = {"position": "left"}
        items.append(item)
    return {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "grid": {"left": 8, "right": 12, "top": 6, "bottom": 6, "containLabel": True},
        "xAxis": {"type": "value", "boundaryGap": ["16%" if has_neg else 0, "16%"],
                  "axisLabel": {"formatter": "{value}" + unit}, "minInterval": 1 if digits == 0 else 0},
        "yAxis": {"type": "category", "data": list(names), "inverse": True,
                  "axisLine": {"onZero": False}, "axisTick": {"show": False}},
        "series": [{"type": "bar", "name": name, "data": items, "barMaxWidth": 16,
                    "label": {"show": True, "position": "right", "formatter": "{c}" + unit, "fontSize": 11}}],
    }


def heatmap_option(rows: list, cols: list, col_labels: list, matrix: pd.DataFrame, diverging: bool = True,
                   cap: float | None = None, digits: int = 2) -> dict:
    """热力图。diverging=True：红涨绿跌对称色阶；False：家数类单色（红）色阶。缺失值留空。"""
    data, vals = [], []
    for yi, r in enumerate(rows):
        for xi, c in enumerate(cols):
            v = _r(matrix.at[r, c], digits) if r in matrix.index and c in matrix.columns else None
            if v is None or (not diverging and v == 0):
                continue
            data.append([xi, yi, _fixed(v, digits)])
            vals.append(abs(v))
    vmax = max(vals) if vals else 1
    if cap:
        vmax = min(vmax, cap)
    vmax = max(vmax, 1 if diverging else 2)
    if diverging:
        vmap = {"min": -round(vmax, 1), "max": round(vmax, 1), "inRange": {"color": [DOWN, FLAT, UP]},
                "text": [f"+{vmax:.1f}%", f"-{vmax:.1f}%"]}
    else:
        vmap = {"min": 1, "max": int(math.ceil(vmax)), "inRange": {"color": ["#f2b8ba", UP, "#9f1d23"]},
                "text": [f"{int(math.ceil(vmax))}只", "1只"]}
    vmap.update({"calculable": False, "orient": "horizontal", "left": "center", "bottom": 0,
                 "itemHeight": 100, "itemWidth": 10, "textStyle": {"color": FLAT, "fontSize": 11}})
    return {
        "tooltip": {"position": "top"},
        "grid": {"left": 8, "right": 12, "top": 6, "bottom": 40, "containLabel": True},
        "xAxis": {"type": "category", "data": list(col_labels), "splitArea": {"show": False},
                  "axisTick": {"show": False}},
        "yAxis": {"type": "category", "data": list(rows), "inverse": True, "splitArea": {"show": False},
                  "axisTick": {"show": False}},
        "visualMap": vmap,
        "series": [{"type": "heatmap", "data": data,
                    "label": {"show": True, "formatter": "{@[2]}", "fontSize": 11},  # 文字颜色由 ECharts 按底色自动反差
                    "itemStyle": {"borderColor": "rgba(128,128,128,0.25)", "borderWidth": 1},
                    "emphasis": {"itemStyle": {"borderColor": "#ffffff", "borderWidth": 1}}}],
    }


# ---------------------------------------------------------------- 文字

# 要点文字的写法与“大盘与情绪”模块一致：名称与数字、数字与单位之间都不加空格（如“影视院线+7.23%”“证券+26.91亿”）

def join_pct(df: pd.DataFrame, n: int = 3, col: str = "pct") -> str:
    return "、".join(f"{r['name']}{fmt_pct(r[col])}" for _, r in df.head(n).iterrows())


def fmt_flow(v, coarse: bool = False) -> str:
    """净流入文字。coarse=True（同花顺，只精确到 0.01 亿元）时统一写成“亿”，不显示成“8000.00万”这种假精度。"""
    f = num(v)
    if f is None:
        return "—"
    return f"{f / 1e8:+.2f}亿" if coarse else fmt_amount(f, signed=True)


def join_flow(df: pd.DataFrame, n: int = 3, coarse: bool = False) -> str:
    return "、".join(f"{r['name']}{fmt_flow(r['net'], coarse)}" for _, r in df.head(n).iterrows())


def short_date(iso: str) -> str:
    """'2026-10-09' -> '10-09'"""
    return iso[5:] if len(iso) >= 10 else iso
