"""板块轮动：哪些行业/概念最强最弱、资金流向哪里、涨停集中在哪些行业、热点是否有持续性。

数据源与回退见 _sectors_data.py；纯计算见 _sectors_calc.py；近 5 日数据的本地累积见 _sectors_store.py。
只陈述客观数据，不给出任何投资建议或预测。
"""

import pandas as pd

from ..context import Context
from ..fetch import SnapshotUnavailable
from ..section import UP, Section, col, fmt_pct, tone_of
from . import _sectors_calc as calc
from . import _sectors_data as data
from ._sectors_store import SectorStore

ID = "sectors"
TITLE = "板块轮动"

BAR_TOP, BAR_BOTTOM = 15, 5   # 行业涨跌幅条形图：前 15 / 后 5
CONCEPT_BAR = 15              # 概念涨幅条形图条数
FLOW_N = 10                   # 资金流条形图：净流入 / 净流出各前 10
ZT_BAR = 15                   # 涨停行业条形图条数
ZT_LADDER = 10                # 涨停名单最多列出的行业数
HEAT_ROWS = 12                # 近 5 日热力图行数
ROT_DAYS = 5                  # 轮动观察天数
TOP_K = 10                    # “上榜” = 当日涨幅前 10
HOT_SHOW = 4                  # 要点里最多列出的“近 5 日多次涨停”行业数
STREAK_MIN = 3                # 要点里“连续位列涨幅前 N”：至少 3 个完整排行日、连续至少 3 日才写
BOOTSTRAP_HISTORY = True      # 本地累积不足时，向数据源补今日最强行业的近 5 日涨跌幅（同花顺每个行业 1 次请求）

KEY = {"industry": "ind", "concept": "con"}
LABEL = {"industry": "行业", "concept": "概念"}
ZT_TEXT = {
    "out_of_window": "复盘日距今超过约30个自然日，东方财富涨停池已不能按日期查询，本次省略",
    "no_data": "东方财富涨停池没有返回该日数据（接口无数据），本次省略",
    "bad_format": "涨停池缺少“所属行业”字段（接口格式可能有变化），本次省略",
    "failed": "东方财富涨停池获取失败（{reason}），本次省略",
    "empty": "当日涨停池没有股票",
}


def build(ctx: Context) -> Section:
    sec = Section(ID, TITLE)
    st: dict = {"snap_missing": [], "from_store": []}
    top_n = int(ctx.cfg.get("top_n") or 20)
    store = SectorStore()
    dates = [d.isoformat() for d in ctx.recent_trade_days(ROT_DAYS)]

    with sec.guard("行业板块排行"):
        _load(ctx, st, store, "industry")
        if st.get("ind") is not None:
            _industry_blocks(sec, st)

    with sec.guard("概念板块排行"):
        _load(ctx, st, store, "concept")
        if st.get("con") is not None:
            _concept_blocks(sec, st, top_n)

    with sec.guard("板块资金流向"):
        _flow_blocks(sec, st)

    with sec.guard("涨停股行业分布"):
        _zt_blocks(ctx, sec, st)

    rot = zt_trend = None
    with sec.guard("近5日行业轮动"):
        rot = _rotation(ctx, st, store, dates)
    with sec.guard("近5日涨停行业"):
        zt_trend = _zt_trend(ctx, st, dates)
    with sec.guard("近5日概念持续性"):
        _concept_presence(st, store, dates)
    with sec.guard("轮动图表"):
        _trend_blocks(sec, st, rot, zt_trend)

    if st["snap_missing"]:   # 历史日期 / 盘中运行的预期降级，写进说明（真正的获取失败才进错误框）
        text = (f"{'、'.join(st['snap_missing'])}板块实时排行：数据源只提供最新快照，当前时刻不对应复盘日收盘，"
                "且没有当天的缓存或本地记录，本次省略")
        if st.get("zt_stats") is not None and not st["zt_stats"].empty:
            text += "（涨停行业分布按日期查询，照常显示）"
        sec.note(text + "。")
    _kpis(sec, st)
    _summary(sec, st)
    _notes(sec, st)
    _balance_spans(sec)
    return sec


# ---------------------------------------------------------------- 板块排行

def _load(ctx, st, store, kind):
    key = KEY[kind]
    try:
        b = data.load_boards(ctx, kind)
    except SnapshotUnavailable:
        _load_from_store(ctx, st, store, kind)
        return
    df = b.df
    if kind == "concept":
        raw_n = len(df)
        df = calc.filter_concepts(df)
        st["con_removed"] = raw_n - len(df)
    st[key], st[key + "_src"], st[key + "_failed"], st[key + "_net"] = df, b.src, b.failed, b.net_basis
    try:
        store.upsert_day(ctx.date_iso, kind, b.src, df)
    except Exception as e:  # 累积失败不影响当天报告
        ctx.log.warning("[sectors] 写入 sector_daily.csv 失败：%s", e)


def _load_from_store(ctx, st, store, kind):
    """快照不可用（历史日期 / 盘中）且没有当天缓存时，用本地累积里复盘日当天的完整排行（只有涨跌幅和净流入）。"""
    key = KEY[kind]
    day, src = store.day(kind, ctx.date_iso)
    if src is None:
        st["snap_missing"].append(LABEL[kind])
        return
    df = calc.standardize(day, {"name": "name", "pct": "pct", "net": "net"})
    if kind == "concept":
        raw_n = len(df)
        df = calc.filter_concepts(df)
        st["con_removed"] = raw_n - len(df)
    st[key], st[key + "_src"], st[key + "_failed"], st[key + "_net"] = df, src, [], "store"
    st["from_store"].append(kind)


def _coarse(st, key) -> bool:
    """同花顺的资金数据只精确到 0.01 亿元，显示时统一用“亿”。"""
    return st.get(key + "_src") == "ths"


def _net_col(st, key, df) -> tuple[dict | None, pd.DataFrame]:
    if not calc.has_values(df, "net"):
        return None, df
    if _coarse(st, key):
        return col("net_yi", "净流入（亿元）", "num_signed"), df.assign(net_yi=df["net"] / 1e8)
    return col("net", "净流入", "flow"), df


def _industry_blocks(sec, st):
    ind = st["ind"]
    top, bottom = calc.top_bottom(ind, "pct", BAR_TOP, BAR_BOTTOM)
    names = list(top["name"]) + (["⋯"] + list(bottom["name"]) if len(bottom) else [])
    vals = list(top["pct"]) + ([None] + list(bottom["pct"]) if len(bottom) else [])
    title = f"行业涨跌幅：前{len(top)}" + (f"/后{len(bottom)}" if len(bottom) else "")
    sec.chart(title, calc.hbar_option(names, vals, name="涨跌幅"), height=max(240, 22 * len(names) + 30))

    net_col, rows = _net_col(st, "ind", ind)
    cols = [col("name", "行业"), col("pct", "涨跌幅", "pct"), col("amount", "成交额", "amount"),
            col("up", "上涨家数", "int"), col("down", "下跌家数", "int"), col("lead", "领涨股"),
            col("lead_pct", "领涨股涨幅", "pct")]
    cols = [c for c in cols if c["key"] in ("name", "pct") or calc.has_values(ind, c["key"])]
    if net_col:
        cols.append(net_col)
    src_txt = "，取自本地累积" if "industry" in st["from_store"] else ""
    sec.table(f"行业板块涨跌幅排行（共{len(ind)}个{src_txt}）", cols, rows.to_dict("records"))


def _concept_blocks(sec, st, top_n):
    con = st["con"]
    top = con[con["pct"].notna()].head(CONCEPT_BAR)
    if top.empty:
        return
    sec.chart(f"概念涨幅前{len(top)}", calc.hbar_option(list(top["name"]), list(top["pct"]), name="涨跌幅"),
              height=max(220, 22 * len(top) + 30), span="half")
    net_col, rows = _net_col(st, "con", con)
    cols = [col("name", "概念"), col("pct", "涨跌幅", "pct")] + ([net_col] if net_col else [])
    cols += [c for c in (col("amount", "成交额", "amount"), col("lead", "领涨股"),
                         col("lead_pct", "领涨股涨幅", "pct")) if calc.has_values(con, c["key"])]
    sec.table(f"概念涨幅前{min(top_n, len(con))}", cols, rows.to_dict("records"), span="half", max_rows=top_n,
              note=f"已剔除{st.get('con_removed', 0)}个非题材类概念（融资融券、昨日涨停、指数成分、业绩预告等）")


# ---------------------------------------------------------------- 资金流

def _flow_blocks(sec, st):
    for key, label in (("ind", "行业"), ("con", "概念")):
        df = st.get(key)
        if df is None or not calc.has_values(df, "net"):
            continue
        inflow, outflow = calc.flow_split(df, FLOW_N)
        st[key + "_in"], st[key + "_out"] = inflow, outflow
        if inflow.empty and outflow.empty:
            continue
        sep = len(inflow) and len(outflow)
        names = list(inflow["name"]) + (["⋯"] if sep else []) + list(outflow["name"])
        vals = [v / 1e8 for v in inflow["net"]] + ([None] if sep else []) + [v / 1e8 for v in outflow["net"]]
        parts = ([f"净流入前{len(inflow)}"] if len(inflow) else []) + ([f"净流出前{len(outflow)}"] if len(outflow) else [])
        sec.chart(f"{label}资金：" + "/".join(parts), calc.hbar_option(names, vals, unit="亿", name="净流入"),
                  height=max(240, 22 * len(names) + 30), span="half")


# ---------------------------------------------------------------- 涨停行业分布

def _zt_blocks(ctx, sec, st):
    res = data.zt_pool(ctx, ctx.date_str)
    st["zt_res"] = res
    if res.status != "ok":
        text = "涨停股行业分布：" + ZT_TEXT[res.status].format(reason=res.reason or "原因见日志") + "。"
        (sec.note if res.status in ("out_of_window", "empty") else sec.error)(text)
        return
    stats = calc.zt_by_industry(res.df)
    st["zt_stats"] = stats
    top = stats.head(ZT_BAR)
    sec.chart(f"涨停股行业分布（共{int(stats['count'].sum())}只，前{len(top)}个行业）",
              calc.hbar_option(list(top["industry"]), list(top["count"]), unit="", digits=0,
                               color=UP, name="涨停家数"),
              height=max(220, 22 * len(top) + 30), span="half")
    # 名单只列涨停 ≥2 只的行业（至少列 5 个、最多 ZT_LADDER 个），其余汇总成一句
    n_show = min(ZT_LADDER, max(5, int((stats["count"] >= 2).sum())))
    levels = []
    for _, r in stats.head(n_show).iterrows():
        items = [{"text": n, "sub": f"{b}板" if b > 1 else "首板", "tone": "up"} for n, b in r["stocks"]]
        levels.append({"label": f"{r['industry']}·{r['count']}", "items": items})
    rest = stats.iloc[n_show:]
    note = f"其余{len(rest)}个行业共{int(rest['count'].sum())}只" if len(rest) else None
    sec.ladder("各行业涨停股", levels, span="half", note=note)


# ---------------------------------------------------------------- 近 5 日轮动

def _rotation(ctx, st, store, dates) -> dict | None:
    """今日最强行业近 5 日的涨跌幅矩阵与持续性统计。"""
    base, src = st.get("ind"), st.get("ind_src")
    if base is None or not src:
        return None
    base = calc.ranked(base)
    rows = list(base["name"].head(HEAT_ROWS))
    pct_m = store.matrix("industry", src, dates).reindex(rows)
    pct_m[ctx.date_iso] = base.set_index("name")["pct"].reindex(rows).values
    full_days = store.full_dates("industry", src, dates)

    filled = 0
    missing = [n for n in rows if pct_m.loc[n].isna().any()]
    if BOOTSTRAP_HISTORY and missing and src in ("ths", "em"):
        filled = _bootstrap(ctx, store, src, base, pct_m, missing, dates)

    has_rank = len(full_days) >= 2  # 至少 2 个完整排行日才统计“上榜”
    rank_m = store.matrix("industry", src, dates, value="rank", full_only=True) if has_rank else None
    stats = calc.rotation_rows(pct_m, rank_m, TOP_K)
    days_avail = [d for d in dates if pct_m[d].notna().any()]
    return {"src": src, "rows": rows, "pct_m": pct_m, "stats": stats, "full_days": full_days,
            "filled": filled, "days_avail": days_avail, "dates": dates, "has_rank": has_rank}


def _concept_presence(st, store, dates):
    """概念连续位列涨幅前 TOP_K 的天数（需要本地累积至少 STREAK_MIN 个完整交易日）。"""
    src = st.get("con_src")
    if not src or len(store.full_dates("concept", src, dates)) < STREAK_MIN:
        return
    p = calc.presence(store.matrix("concept", src, dates, value="rank", full_only=True), TOP_K)
    st["con_presence"] = p[p["streak"] >= STREAK_MIN]


def _bootstrap(ctx, store, src, base, pct_m, missing, dates) -> int:
    """向数据源补齐缺失的历史涨跌幅（并存入本地累积，之后不再请求）。返回补到的行业数。"""
    names_ok = None
    if src == "ths":
        try:
            names_ok = data.ths_industry_names(ctx)
        except Exception as e:
            ctx.log.info("[sectors] 同花顺行业名称表获取失败，跳过历史补齐：%s", e)
            return 0
    boards = base.set_index("name")
    n = 0
    for name in missing:
        if names_ok is not None and name not in names_ok:
            continue
        try:
            code = boards["code"].get(name, "") if "code" in boards.columns else ""
            hist = data.industry_history(ctx, src, {"name": name, "code": code}, dates)
        except Exception as e:
            ctx.log.info("[sectors] %s 历史涨跌幅获取失败，停止补齐：%s", name, e)
            break
        if not hist:
            continue
        for d, p in hist.items():
            if pd.isna(pct_m.at[name, d]):
                pct_m.at[name, d] = p
        try:
            store.add_partial("industry", src, name, {d: p for d, p in hist.items() if d != ctx.date_iso})
        except Exception as e:
            ctx.log.warning("[sectors] 写入 sector_daily.csv 失败：%s", e)
        n += 1
    return n


def _zt_trend(ctx, st, dates) -> dict:
    """近 5 日每日涨停股的行业分布（涨停池可按日期查询，超出约 30 个自然日的不再请求）。"""
    pools, out_win, failed = {}, [], []
    for d in dates:
        res = st["zt_res"] if d == ctx.date_iso and "zt_res" in st else data.zt_pool(ctx, d.replace("-", ""))
        pools[d] = res.df if res.status in ("ok", "empty") else None
        if res.status == "out_of_window":
            out_win.append(calc.short_date(d))
        elif pools[d] is None:
            failed.append(calc.short_date(d))
    m = calc.zt_matrix(pools)
    n_days = sum(1 for d in dates if pools.get(d) is not None)
    if m.empty:
        return {"m": m, "rows": [], "out_win": out_win, "failed": failed, "dates": dates, "n_days": n_days,
                "days_with": pd.Series(dtype=int)}
    m = m.assign(_total=m.sum(axis=1), _today=m[dates[-1]].fillna(0))
    m = m.sort_values(["_total", "_today"], ascending=False).drop(columns=["_total", "_today"])
    return {"m": m, "rows": list(m.index[:HEAT_ROWS]), "out_win": out_win, "failed": failed, "dates": dates,
            "days_with": (m > 0).sum(axis=1), "n_days": n_days}


def _zt_missing_text(zt_trend) -> str | None:
    parts = []
    if zt_trend["out_win"]:
        parts.append(f"{'、'.join(zt_trend['out_win'])}已超出东方财富涨停池可按日期查询的范围（约30个自然日）")
    if zt_trend["failed"]:
        parts.append(f"{'、'.join(zt_trend['failed'])}的涨停池未取到")
    return "；".join(parts) or None


def _trend_blocks(sec, st, rot, zt_trend):
    st["rot"], st["zt_trend"] = rot, zt_trend
    if rot:
        labels = [calc.short_date(d) for d in rot["dates"]]
        sec.chart(f"今日涨幅前{len(rot['rows'])}的行业：近{ROT_DAYS}日涨跌幅",
                  calc.heatmap_option(rot["rows"], rot["dates"], labels, rot["pct_m"]),
                  height=26 * len(rot["rows"]) + 70, span="half",
                  note=None if len(rot["days_avail"]) == ROT_DAYS else
                  f"目前只有{len(rot['days_avail'])}个交易日的数据")
    if zt_trend and zt_trend["rows"]:
        labels = [calc.short_date(d) for d in zt_trend["dates"]]
        sec.chart(f"涨停行业近{ROT_DAYS}日热度（每日涨停家数）",
                  calc.heatmap_option(zt_trend["rows"], zt_trend["dates"], labels, zt_trend["m"],
                                      diverging=False, digits=0),
                  height=26 * len(zt_trend["rows"]) + 70, span="half", note=_zt_missing_text(zt_trend))
    elif zt_trend and st.get("zt_res") is not None and st["zt_res"].status != "out_of_window":
        sec.note(f"涨停行业近{ROT_DAYS}日热度：{_zt_missing_text(zt_trend) or '近几日均无涨停数据'}，本次省略。")
    if rot:
        n_full = len(rot["full_days"])
        cols = [col("name", "行业"), col("pct", "今日涨跌幅", "pct"), col("cum", f"近{ROT_DAYS}日累计", "pct"),
                col("up_days", "上涨天数", align="right"), col("streak_txt", "连涨天数", align="right")]
        if rot["has_rank"]:
            cols += [col("top_hits", f"进前{TOP_K}次数（近{n_full}日）", "int"),
                     col("top_streak_txt", f"连续进前{TOP_K}天数", align="right")]
        sec.table(f"今日涨幅前{len(rot['rows'])}的行业：持续性（近{ROT_DAYS}个交易日）", cols, rot["stats"],
                  note=f"“近{ROT_DAYS}日累计”为逐日涨跌幅复合计算，有缺失日时不计算；“上涨天数”= 上涨日数/有数据的日数；"
                       "“≥N”表示连续天数已延续到可统计的最早一天，实际可能更长")


# ---------------------------------------------------------------- KPI / 要点 / 口径

def _kpis(sec, st):
    ind = st.get("ind")
    if ind is not None and ind["pct"].notna().any():
        d = ind[ind["pct"].notna()]
        best, worst = d.iloc[0], d.iloc[-1]
        lead = f" · 领涨 {best['lead']}" if best["lead"] else ""
        sec.kpi("最强行业", best["name"], fmt_pct(best["pct"]) + lead, tone_of(best["pct"]))
        sec.kpi("最弱行业", worst["name"], fmt_pct(worst["pct"]), tone_of(worst["pct"]))
        up, down = int((d["pct"] > 0).sum()), int((d["pct"] < 0).sum())
        flat = len(d) - up - down
        sec.kpi("行业涨 / 跌", f"{up} / {down}", f"平盘{flat} · 共{len(d)}个行业",
                "up" if up > down else "down" if down > up else "flat")
    con = st.get("con")
    if con is not None and con["pct"].notna().any():
        c = con[con["pct"].notna()].iloc[0]
        sec.kpi("最强概念", c["name"], fmt_pct(c["pct"]), tone_of(c["pct"]))
    zt = st.get("zt_stats")
    if zt is not None and not zt.empty:
        tied = calc.top_ties(zt, "count", "industry")
        cnt, total = int(zt["count"].iloc[0]), int(zt["count"].sum())
        value = "、".join(tied[:2]) + (f"等{len(tied)}个" if len(tied) > 2 else "")
        sub = (f"并列 · 各{cnt}只" if len(tied) > 1 else f"{cnt}只") + f" · 涨停池共{total}只"
        sec.kpi("涨停最集中", value, sub, "up")
    inflow = st.get("ind_in")
    if inflow is not None and not inflow.empty:
        f = inflow.iloc[0]
        sec.kpi("行业净流入第一", f["name"], calc.fmt_flow(f["net"], _coarse(st, "ind")), tone_of(f["net"]))


def _summary(sec, st):
    ind = st.get("ind")
    if ind is not None and ind["pct"].notna().any():
        d = ind[ind["pct"].notna()]
        up, down = int((d["pct"] > 0).sum()), int((d["pct"] < 0).sum())
        flat = len(d) - up - down
        text = f"行业板块{up}涨{down}跌{flat}平（共{len(d)}个）"
        if up:
            text += f"；涨幅居前：{calc.join_pct(d[d['pct'] > 0])}"
        if down:
            text += f"；跌幅居前：{calc.join_pct(d[d['pct'] < 0].iloc[::-1])}"
        sec.add_summary(text + "。")
    zt = st.get("zt_stats")
    if zt is not None and not zt.empty:
        parts = "、".join(f"{r['industry']}（{r['count']}只）" for _, r in zt.head(3).iterrows())
        if len(zt) > 3 and zt["count"].iloc[3] == zt["count"].iloc[2]:
            parts += "等"
        sec.add_summary(f"涨停股集中在{parts}，涨停池共{int(zt['count'].sum())}只（不含ST股）。")
    inflow, outflow = st.get("ind_in"), st.get("ind_out")
    if inflow is not None and (len(inflow) or len(outflow)):
        coarse = _coarse(st, "ind")
        text = []
        if len(inflow):
            text.append(f"净流入居前：{calc.join_flow(inflow, coarse=coarse)}")
        if len(outflow):
            text.append(f"净流出居前：{calc.join_flow(outflow.iloc[::-1], coarse=coarse)}")
        sec.add_summary("行业资金" + "；".join(text) + "。")
    con = st.get("con")
    if con is not None and (con["pct"] > 0).any():
        sec.add_summary(f"概念板块涨幅居前：{calc.join_pct(con[con['pct'] > 0])}。")
    _summary_trend(sec, st)


def _summary_trend(sec, st):
    rot = st.get("rot")
    if rot and len(rot["days_avail"]) >= 2:
        sec.add_summary(_trend_text(rot["stats"], len(rot["days_avail"]), len(rot["full_days"])))
    cp = st.get("con_presence")
    if cp is not None and not cp.empty:
        parts = "、".join(f"{r['name']}（{calc.streak_words(int(r['streak']), bool(r['streak_open']))}）"
                         for _, r in cp.head(5).iterrows())
        sec.add_summary(f"概念连续位列涨幅前{TOP_K}：{parts}。")
    text = _zt_hot_text(st.get("zt_trend"))
    if text:
        sec.add_summary(text)


def _zt_hot_text(zt_trend) -> str:
    """近 N 日多数日子都出现涨停的行业（按累计涨停只次排序，最多列 HOT_SHOW 个，其余写“等 N 个行业”）。"""
    if not zt_trend or zt_trend["n_days"] < 3 or zt_trend["m"].empty:
        return ""
    dw = zt_trend["days_with"]
    need = zt_trend["n_days"] if zt_trend["n_days"] < ROT_DAYS else ROT_DAYS - 1
    hot = [n for n in zt_trend["m"].index if dw.get(n, 0) >= need]
    if not hot:
        return ""
    total = zt_trend["m"].loc[hot].sum(axis=1)
    parts = "、".join(f"{n}（{int(total[n])}只次）" for n in hot[:HOT_SHOW])
    if len(hot) > HOT_SHOW:
        parts += f"等{len(hot)}个行业"
    return f"近{zt_trend['n_days']}个交易日中至少{need}天出现涨停的行业：{parts}。"


def _trend_text(stats: list[dict], n_days: int, n_full: int = 0) -> str:
    """持续性描述：今日前 3 的近 N 日累计；连涨 ≥3 日的行业；
    完整排行至少 STREAK_MIN 天时，再写连续上榜 ≥STREAK_MIN 日的行业（天数太少没有意义）。"""
    parts = []
    top3 = stats[:3]
    if any(s["cum"] is not None for s in top3):
        items = [f"{s['name']}{fmt_pct(s['cum'])}" if s["cum"] is not None else f"{s['name']}（缺少完整数据）"
                 for s in top3]
        parts.append(f"今日涨幅前{len(top3)}的行业近{n_days}日累计：" + "、".join(items))
    by_streak: dict[tuple, list[str]] = {}
    for s in stats:
        if s["streak"] >= 3:
            by_streak.setdefault((s["streak"], bool(s.get("streak_open"))), []).append(s["name"])
    for k, open_ended in sorted(by_streak, reverse=True)[:2]:
        parts.append(f"{'、'.join(by_streak[(k, open_ended)][:4])}已连涨{calc.streak_words(k, open_ended)}")
    listed = [s for s in stats if s.get("top_streak", 0) >= STREAK_MIN] if n_full >= STREAK_MIN else []
    if listed:
        parts.append("连续位列涨幅前%d：%s" % (TOP_K, "、".join(
            f"{s['name']}（{calc.streak_words(s['top_streak'], bool(s.get('top_streak_open')))}）" for s in listed[:4])))
    return "持续性：" + "；".join(parts) + "。" if parts else ""


NET_NOTES = {
    "em_main": "东方财富主力净流入（超大单 + 大单）",
    "ths_flow": "同花顺资金流向“净额”（流入资金 − 流出资金，精确到0.01亿元）",
    "ths_summary": "同花顺行业一览“净流入”（统计方法与同花顺资金流向页的“净额”不同，不宜与概念净流入相互比较）",
}


def _src_text(st, key) -> str:
    """排行数据源说明（不含“行业/概念”前缀），行业与概念相同时合并成一条。"""
    text = f"数据源：{data.SRC_LABEL[st[key + '_src']]}"
    if st.get(key + "_failed"):
        text += "（" + "、".join(f.split("（")[0] for f in st[key + "_failed"]) + "暂时无法获取，已改用此源）"
    basis = NET_NOTES.get(st.get(key + "_net", ""))
    if basis:
        text += f"；净流入为{basis}"
    return text


def _notes(sec, st):
    texts = {}
    for kind in ("industry", "concept"):
        key, label = KEY[kind], LABEL[kind]
        src = st.get(key + "_src")
        if not src:
            continue
        if kind in st["from_store"]:
            sec.note(f"{label}排行：复盘日的实时快照已不可用，取自本地逐日记录（{data.SRC_LABEL[src]}，"
                     "只有涨跌幅和净流入）。")
            continue
        texts[kind] = _src_text(st, key)
    if len(texts) == 2 and texts["industry"] == texts["concept"]:
        sec.note(f"行业、概念排行{texts['industry']}。")
    else:
        for kind, text in texts.items():
            sec.note(f"{LABEL[kind]}排行{text}。")
    for kind in texts:
        if st.get(KEY[kind] + "_src") == "sina":
            extra = "新浪财经板块数据不含资金流向与涨跌家数"
            if kind == "industry":
                extra += "；新浪行业分类较旧，合计只覆盖约3000只股票，与同花顺/东方财富的行业不可直接比较"
            sec.note(extra + "。")
    if st.get("con") is not None:
        sec.note("概念排行已剔除非题材类概念：融资融券、沪深股通、MSCI/富时罗素/标普成分、转债标的、业绩预告（预增/预亏等）、"
                 "昨日涨停/连板、次新股、ST/摘帽、机构重仓、指数与风格成分（高股息、中字头等）、参股金融等。")
    if st.get("zt_stats") is not None or (st.get("zt_trend") or {}).get("rows"):
        text = ("涨停股行业分布：东方财富涨停股池（按日期查询；不含ST股，含创业板、科创板、北交所），"
                "按其“所属行业”（申万二级行业）统计")
        if st.get("ind") is not None:   # 页面上有行业排行时才需要提醒两者分类不同
            text += "，与上方行业排行的数据源和分类不同"
        sec.note(text + "。")
    rot = st.get("rot")
    if rot:
        text = f"近{ROT_DAYS}日行业涨跌幅：本地逐日记录，其中{len(rot['full_days'])}个交易日有完整排行"
        partial = [calc.short_date(d) for d in rot["days_avail"] if d not in rot["full_days"]]
        if partial:
            text += f"；{'、'.join(partial)}为按行业补齐的{data.SRC_LABEL[rot['src']]}行业指数日线数据"
            if rot["has_rank"]:
                text += f"，不计入“进前{TOP_K}次数”"
        sec.note(text + "。")


def _balance_spans(sec):
    """连续的半宽块是奇数个时，最后一个改为整宽，避免两列布局留下一块空白。"""
    run = []
    for b in sec.blocks + [None]:
        if b is not None and b.get("span") == "half":
            run.append(b)
            continue
        if len(run) % 2 == 1:
            run[-1]["span"] = "full"
        run = []
