"""大盘与情绪：主要指数、两市成交额、涨跌家数与分布、涨跌停/炸板/连板等短线情绪。

数据获取见 _market_data.py（主数据源 + 备用数据源），纯计算与图表配置见 _market_calc.py。
只陈述客观数据，不给任何买卖建议或预测。
"""

import time

import pandas as pd

from ..context import Context
from ..fetch import FetchError, SnapshotUnavailable
from ..section import Section, col, fmt_amount, fmt_pct, tone_of
from . import _market_calc as calc
from . import _market_data as data

ID = "market"
TITLE = "大盘与情绪"

CHART_DAYS = 60   # 指数/成交额图表天数
TREND_DAYS = 20   # 情绪趋势天数
# 历史回填分批进行（从近到远），避免新环境首跑时请求过多、耗时过长；之后每次生成继续补
SENTIMENT_BACKFILL_PER_RUN = 5    # 情绪指标：每天 4 次请求（4 个股票池）
TURNOVER_BACKFILL_PER_RUN = 10    # 成交额（日线成交额不可用时用交易所官网）：每天 2 次请求
BACKFILL_BUDGET_S = 20            # 每类回填单次运行最多花的时间（秒），超时下次再补
SRC_NAME = {"em": "东方财富", "sina": "新浪财经"}
POOLS = (("zt", "涨停池"), ("zb", "炸板池"), ("dt", "跌停池"), ("prev", "昨日涨停池"))


def build(ctx: Context) -> Section:
    sec = Section(ID, TITLE)
    st = {
        "idx": {}, "idx_src": {}, "idx_day": {},                       # 指数日线 / 来源 / 复盘日数据
        "turnover": None, "turnover_prev": None, "turnover_src": None, "turnover_series": None,
        "turnover_trend": None,                                        # {"dates", "values", "note"}
        "breadth": None, "fenbu": None, "breadth_src": None,
        "pools": {}, "pool_failed": False,                             # pool_failed：复盘日有股票池请求出错
    }
    hist: dict = {}
    with sec.guard("读取历史指标"):
        hist = _history_map(ctx)
    hist_today = calc.from_history(hist.get(ctx.date_iso))

    with sec.guard("主要指数"):
        _load_indexes(ctx, sec, st)
    with sec.guard("两市成交额"):
        _load_turnover(ctx, st, hist)
    with sec.guard("成交额趋势"):
        _turnover_trend(ctx, st)
    with sec.guard("涨跌家数"):
        _load_breadth(ctx, sec, st, hist_today)
    with sec.guard("涨停板股票池"):
        _load_pools(ctx, sec, st)

    p = st["pools"]
    metrics = dict(hist_today)   # 股票池拿不到时（如复盘日太早）用历史记录里的当日指标兜底
    with sec.guard("情绪指标"):
        metrics = _metrics(ctx, sec, st, hist_today)

    with sec.guard("保存历史指标"):
        rec = calc.to_history(metrics, st["breadth"], st["turnover"])
        if rec:
            ctx.history.upsert(ctx.date_iso, rec)

    with sec.guard("关键指标"):
        _kpis(sec, st, metrics)
    with sec.guard("复盘要点"):
        _summary(sec, st, metrics)

    with sec.guard("上证指数K线"):
        _chart_kline(sec, st)
    with sec.guard("指数走势对比"):
        _chart_normalized(sec, st)
    with sec.guard("成交额趋势图"):
        _chart_turnover(sec, st)
    with sec.guard("涨跌分布"):
        _chart_fenbu(sec, st)
    with sec.guard("情绪趋势"):
        _sentiment_trend(ctx, sec, st)
    with sec.guard("连板梯队"):
        _ladder(sec, p.get("zt"))
    with sec.guard("股票池明细"):
        _tables(sec, p)

    with sec.guard("口径说明"):
        _notes(sec, st)
    ctx.shared["market"] = {"metrics": metrics, "breadth": st["breadth"], "turnover": st["turnover"],
                            "turnover_prev": st["turnover_prev"], "index": st["idx_day"]}
    if p.get("zt") is not None:
        ctx.shared["zt_pool"] = p["zt"]
    return sec


# ---------------------------------------------------------------- 数据加载

def _history_map(ctx) -> dict:
    df = ctx.history.load()
    if df.empty or "date" not in df.columns:
        return {}
    return {str(r["date"]): r for r in df.to_dict("records")}


def _load_indexes(ctx, sec, st) -> None:
    failed = []
    for symbol, name in calc.INDEXES:
        try:
            df, src = data.load_index(ctx, symbol)
        except Exception as e:
            ctx.log.warning("[market] %s 日线获取失败：%s", name, e)
            failed.append(name)
            continue
        st["idx"][symbol], st["idx_src"][symbol] = df, src
        day = calc.index_day(df, ctx.date_iso)
        if day:
            st["idx_day"][symbol] = {"name": name, **day}
    if failed:
        sec.error(f"主要指数：{'、'.join(failed)} 日线获取失败（东方财富与新浪财经均不可用）")
    missing = [name for s, name in calc.INDEXES if s in st["idx"] and s not in st["idx_day"]]
    if missing:
        sec.error(f"主要指数：{'、'.join(missing)} 的数据尚未更新到复盘日")


def _load_turnover(ctx, st, hist) -> None:
    """主：东方财富上证指数+深证成指成交额；次：交易所官网成交概况；再次：本地历史记录。"""
    prev_day = ctx.prev_trade_day()
    prev_iso = prev_day.isoformat() if prev_day else None
    if st["idx_src"].get("sh000001") == "em" and st["idx_src"].get("sz399001") == "em":
        ser = calc.turnover_series(st["idx"]["sh000001"], st["idx"]["sz399001"])
        if not ser.empty and ser["date"].iloc[-1] == ctx.date_iso:
            st["turnover_series"] = ser
            st["turnover"] = float(ser["turnover"].iloc[-1])
            if len(ser) >= 2 and ser["date"].iloc[-2] == prev_iso:
                st["turnover_prev"] = float(ser["turnover"].iloc[-2])
            st["turnover_src"] = "em"
            _backfill_turnover(ctx, ser, hist)
            return

    def one_day(d, d_iso):
        """(成交额, 来源)：交易所官网优先，失败再用历史记录。"""
        try:
            v = data.exchange_turnover(ctx, d)
            if v is not None:
                return v, "exchange"
        except FetchError as e:
            ctx.log.warning("[market] %s 交易所成交额获取失败：%s", d_iso, e)
        return calc.from_history(hist.get(d_iso)).get("turnover"), "history"

    t, src = one_day(ctx.trade_date, ctx.date_iso)
    if t is None:
        raise ValueError("东方财富指数日线、交易所成交概况和本地历史记录都没有复盘日的成交额")
    prev = None
    if prev_day:
        prev, prev_src = one_day(prev_day, prev_iso)
        if prev is not None and prev_src == "exchange" and "turnover" not in calc.from_history(hist.get(prev_iso)):
            ctx.history.upsert(prev_iso, {"turnover": round(prev, 0)})
    st.update(turnover=t, turnover_prev=prev, turnover_src=src)


def _backfill_turnover(ctx, ser: pd.DataFrame, hist: dict) -> None:
    """把日线里的历史成交额补进 history，push2his 不可用时成交额趋势图仍可画。"""
    tail = ser.tail(CHART_DAYS)
    for d_iso, v in zip(tail["date"], tail["turnover"]):
        if d_iso == ctx.date_iso:
            continue  # 复盘日随其他指标一起写
        if "turnover" not in calc.from_history(hist.get(d_iso)):
            ctx.history.upsert(d_iso, {"turnover": round(float(v), 0)})


def _backfill_turnover_exchange(ctx) -> int:
    """日线成交额不可用时，用交易所官网把近 CHART_DAYS 日缺失的成交额补进 history（从近到远、分批），
    遇到失败即停。返回仍待回补的交易日数。"""
    hist = _history_map(ctx)
    todo = [d for d in reversed(ctx.recent_trade_days(CHART_DAYS))
            if d != ctx.trade_date and "turnover" not in calc.from_history(hist.get(d.isoformat()))]
    done, t0 = 0, time.monotonic()
    for d in todo[:TURNOVER_BACKFILL_PER_RUN]:
        if time.monotonic() - t0 > BACKFILL_BUDGET_S:
            break
        try:
            v = data.exchange_turnover(ctx, d)
        except FetchError as e:
            ctx.log.warning("[market] 回补 %s 成交额失败，停止回补：%s", d, e)
            break
        if v is None:
            ctx.log.warning("[market] %s 交易所成交概况缺少股票成交金额，停止回补", d)
            break
        ctx.history.upsert(d.isoformat(), {"turnover": round(v, 0)})
        done += 1
    return len(todo) - done


def _turnover_trend(ctx, st) -> None:
    """成交额趋势数据（图表和“近N日最高/最低”共用）：东方财富日线；否则 history（交易所官网分批回补）。"""
    ser = st["turnover_series"]
    if ser is not None:
        tail = ser.tail(CHART_DAYS)
        st["turnover_trend"] = {"dates": tail["date"].tolist(), "values": tail["turnover"].tolist(), "note": None}
        return
    if st["turnover"] is None:
        return
    remaining = _backfill_turnover_exchange(ctx) if st["turnover_src"] == "exchange" else 0
    hist = _history_map(ctx)
    dates, values = [], []
    for d in ctx.recent_trade_days(CHART_DAYS):
        v = st["turnover"] if d == ctx.trade_date else calc.from_history(hist.get(d.isoformat())).get("turnover")
        if v is not None:
            dates.append(d.isoformat())
            values.append(v)
    if st["turnover_src"] == "exchange":
        note = "东方财富指数日线暂时无法获取：趋势取自本地历史记录，缺失的交易日用上交所、深交所官网数据补齐"
        if remaining:
            note += f"（还有{remaining}个交易日待补）"
        note += "。"
    else:
        note = "东方财富指数日线与交易所官网暂时无法获取：趋势取自本地历史记录，缺失的交易日不显示。"
    st["turnover_trend"] = {"dates": dates, "values": values, "note": note}


def _load_breadth(ctx, sec, st, hist_today) -> None:
    """主：东方财富涨跌分布；次：乐咕乐股；再次：本地历史记录（只有家数，没有分布）。"""
    snapshot_missing = False
    try:
        r = data.site_fetch(ctx, data.PUSH2EX_DOWN, data.em_zdfenbu, date=ctx.date_str, snapshot=True)
        st.update(fenbu=r["fenbu"], breadth=calc.breadth_from_fenbu(r["fenbu"]), breadth_src="em")
        return
    except SnapshotUnavailable:
        snapshot_missing = True
    except FetchError as e:
        ctx.log.warning("[market] 东方财富涨跌分布获取失败，改用乐咕乐股：%s", e)
    try:
        b = calc.breadth_from_legu(ctx.fetch(data.legu_market_activity, date_iso=ctx.date_iso, snapshot=True))
        if b:
            st.update(breadth=b, breadth_src="legu")
            return
    except SnapshotUnavailable:
        snapshot_missing = True
    except FetchError as e:
        ctx.log.warning("[market] 乐咕乐股涨跌家数获取失败：%s", e)

    b = calc.breadth_from_history(hist_today)
    if b:
        st.update(breadth=b, breadth_src="history")
        return
    if snapshot_missing:
        sec.note("涨跌家数与涨跌分布只能取实时快照：当前快照不代表复盘日收盘、也没有当日缓存，本次省略。")
    else:
        sec.error("涨跌家数：东方财富与乐咕乐股均获取失败")


def _load_pools(ctx, sec, st) -> None:
    if not data.pool_window_ok(ctx.trade_date):
        sec.note("复盘日距今超过约30个自然日，东方财富涨停板专题已不能按日期查询："
                 "涨跌停指标取自本地历史记录（如有），连板梯队和股票池明细本次省略。")
        return
    for kind, label in POOLS:
        try:
            df = data.fetch_pool(ctx, kind, ctx.trade_date)
        except Exception as e:   # FetchError / SiteDown / PoolFormatError 等，原始异常只写日志
            st["pool_failed"] = True
            ctx.log.warning("[market] %s获取失败：%s", label, e)
            sec.error(f"{label}：{data.pool_error_text(e)}")
            continue
        if df is None:
            sec.error(f"{label}：数据为空（数据源可能尚未更新）")
            continue
        st["pools"][kind] = df


def _metrics(ctx, sec, st, hist_today) -> dict:
    """当日情绪指标：股票池现算；拿不到的指标用 history 里当天的值兜底，并在口径说明里写明。"""
    p = st["pools"]
    fresh = calc.pool_metrics(p.get("zt"), p.get("zb"), p.get("dt"), p.get("prev"))
    from_hist = [label for k, label in calc.METRIC_LABELS.items() if k in hist_today and k not in fresh]
    if from_hist and data.pool_window_ok(ctx.trade_date):
        what = "、".join(from_hist) if len(from_hist) <= 4 else "涨跌停、炸板、连板等情绪指标"
        sec.note(f"{what}取自本地历史记录：本次相关股票池没有取到，改用复盘日当天此前成功获取并保存的数据。")
    return {**hist_today, **fresh}


def _backfill_sentiment(ctx, st, days, hist) -> dict:
    """近 N 日情绪指标：history 里缺的、且还能按日期查到的交易日，取股票池回填并写入 history。

    从近到远、每次最多 SENTIMENT_BACKFILL_PER_RUN 天；复盘日的股票池本次出过错就不回填；遇到失败即停。
    返回 {"filled", "remaining", "status": ok|failed|skipped, "failed_day"}。
    """
    todo = [d for d in reversed(days) if d != ctx.trade_date and data.pool_window_ok(d)
            and calc.needs_backfill(hist.get(d.isoformat()))]
    res = {"filled": 0, "remaining": len(todo), "status": "ok", "failed_day": None}
    if not todo:
        return res
    if st["pool_failed"] or ctx.shared.get(data.PUSH2EX_DOWN):
        res["status"] = "skipped"
        return res
    t0 = time.monotonic()
    for d in todo[:SENTIMENT_BACKFILL_PER_RUN]:
        if time.monotonic() - t0 > BACKFILL_BUDGET_S:
            break
        try:
            pools = {kind: data.fetch_pool(ctx, kind, d) for kind in data.POOL_FUNCS}
            if pools["zt"] is None:
                raise ValueError("涨停池为空")
        except Exception as e:
            ctx.log.warning("[market] 回填 %s 情绪指标失败，停止回填：%s", d, e)
            res.update(status="failed", failed_day=d)
            break
        rec = calc.to_history(calc.pool_metrics(pools["zt"], pools["zb"], pools["dt"], pools["prev"]))
        ctx.history.upsert(d.isoformat(), rec)
        res["filled"] += 1
    res["remaining"] = len(todo) - res["filled"]
    return res


# ---------------------------------------------------------------- 展示：KPI / 要点

def _kpis(sec, st, m) -> None:
    for symbol, name in calc.INDEXES:
        day = st["idx_day"].get(symbol)
        if day and day.get("pct") is not None:
            sec.kpi(name, fmt_pct(day["pct"]), f"收{day['close']:.2f}", tone_of(day["pct"]))
        else:
            sec.kpi(name, "—", "暂无数据")

    t, prev = st["turnover"], st["turnover_prev"]
    if t is not None:
        sub, tone = "上一交易日数据缺失", "neutral"
        if prev:
            diff = t - prev
            sub, tone = f"较前日{calc.fmt_yi(diff, signed=True)}（{diff / prev * 100:+.1f}%）", tone_of(diff)
        sec.kpi("沪深两市成交额", fmt_amount(t), sub, tone)

    b = st["breadth"]
    if b and b.get("total"):
        tone = "up" if b["up"] > b["down"] else "down" if b["down"] > b["up"] else "flat"
        scope = calc.BREADTH_SCOPE.get(b.get("basis"), "")
        sub = f"平盘{b['flat']} · 上涨占比{b['up'] / b['total'] * 100:.0f}%" + (f" · {scope}" if scope else "")
        sec.kpi("上涨 / 下跌家数", f"{b['up']} / {b['down']}", sub, tone)

    if m.get("zt_count") is not None:
        sub = f"连板股{m['lianban_count']}家" if m.get("lianban_count") is not None else ""
        sec.kpi("涨停家数（不含ST）", str(m["zt_count"]), sub, "up" if m["zt_count"] else "flat")
    if m.get("dt_count") is not None:
        sub = f"连续跌停{m['dt_lianxu']}家" if m.get("dt_lianxu") else ""
        sec.kpi("跌停家数（不含ST）", str(m["dt_count"]), sub, "down" if m["dt_count"] else "flat")
    if m.get("zb_rate") is not None:
        zb = m.get("zb_count")
        sub = f"炸板{zb}家 · 触板{m['zt_count'] + zb}家" if zb is not None and m.get("zt_count") is not None else ""
        sec.kpi("炸板率", f"{m['zb_rate']:.1f}%", sub)
    if m.get("max_board") is not None:
        names = m.get("max_board_names") or []
        sub = "、".join(names[:2]) + (f"等{len(names)}只" if len(names) > 2 else "")
        sec.kpi("最高连板", f"{m['max_board']}板", sub, "up" if m["max_board"] >= 2 else "neutral")
    if m.get("prev_zt_avg_pct") is not None:
        parts = []
        if m.get("promote_rate") is not None:
            parts.append(f"晋级率{m['promote_rate']:.1f}%")
        if m.get("prev_zt_up_ratio") is not None:
            parts.append(f"收涨{m['prev_zt_up_ratio']:.0f}%")
        sec.kpi("昨日涨停今日均涨", fmt_pct(m["prev_zt_avg_pct"]), " · ".join(parts), tone_of(m["prev_zt_avg_pct"]))


def _summary(sec, st, m) -> None:
    idx = [st["idx_day"][s] for s, _ in calc.INDEXES if s in st["idx_day"]]
    trend = st["turnover_trend"]
    recent = trend["values"] if trend else None
    lines = [   # 报告顶部“今日要点”只取前 3 条：指数、成交额、涨跌家数与涨跌停
        calc.index_summary(idx),
        calc.turnover_summary(st["turnover"], st["turnover_prev"], recent),
        calc.breadth_limits_summary(st["breadth"], m),
        calc.board_summary(m),
        calc.money_effect_summary(m),
    ]
    for line in lines:
        sec.add_summary(line)


# ---------------------------------------------------------------- 展示：图表

def _chart_kline(sec, st) -> None:
    df = st["idx"].get("sh000001")
    if df is None or len(df) < 5:
        return
    src = st["idx_src"].get("sh000001")
    note = None if src == "em" else "备用数据源（新浪财经）没有成交额，下方柱为成交量。"
    sec.chart(f"上证指数（近{CHART_DAYS}个交易日）", calc.kline_option(df, "上证指数", CHART_DAYS), height=380, note=note)


def _chart_normalized(sec, st) -> None:
    base = st["idx"].get("sh000001")
    if base is None or base.empty:
        base = next((df for df in st["idx"].values() if not df.empty), None)
    if base is None:
        return
    dates = base["date"].tail(CHART_DAYS).tolist()
    frames = {name: st["idx"][s] for s, name in calc.INDEXES if s in st["idx"] and not st["idx"][s].empty}
    if len(frames) < 2:
        return
    sec.chart(f"主要指数区间涨跌幅（近{CHART_DAYS}个交易日）", calc.normalized_option(frames, dates),
              height=320, span="half", note="以区间首日收盘为基准的累计涨跌幅。")


def _chart_turnover(sec, st) -> None:
    trend = st["turnover_trend"]
    if not trend:
        return
    dates, values = trend["dates"], trend["values"]
    if len(values) < 5:
        sec.note("成交额趋势：可用的历史数据不足5个交易日，本次省略。")
        return
    sec.chart(f"沪深两市成交额（近{len(values)}个交易日）", calc.turnover_option(dates, values),
              height=320, span="half", note=trend["note"])


def _chart_fenbu(sec, st) -> None:
    if not st["fenbu"]:
        return
    b = st["breadth"]
    sec.chart("涨跌分布（家数）", calc.fenbu_option(st["fenbu"]), height=300,
              note=f"上涨{b['up']}家，下跌{b['down']}家，平盘{b['flat']}家（沪深京A股，含北交所，不含ST）；"
                   "“>9%”“<-9%”两档为涨跌幅超过±9%但未涨停/跌停的股票。")


def _sentiment_trend(ctx, sec, st) -> None:
    days = ctx.recent_trade_days(TREND_DAYS)
    hist = _history_map(ctx)
    res = _backfill_sentiment(ctx, st, days, hist)
    if res["filled"]:
        hist = _history_map(ctx)
    dates = [d.isoformat() for d in days]
    rows = [calc.from_history(hist.get(d)) for d in dates]
    dates, rows = calc.trim_leading_empty(dates, rows, "zt_count")
    if res["status"] == "failed":
        sec.note(f"情绪趋势：{res['failed_day']:%m-%d}的历史数据未取到，缺失的日期留空。")
    elif res["status"] == "skipped":
        sec.note(f"情绪趋势：东方财富涨停板专题暂时无法获取，缺失的{res['remaining']}个交易日留空。")
    elif res["remaining"]:
        sec.note(f"情绪趋势：还有{res['remaining']}个交易日的数据待补齐，缺失的日期留空。")
    if sum(1 for r in rows if "zt_count" in r) < 2:
        sec.note("情绪趋势：可用的历史数据不足2个交易日，本次省略。")
        return
    sec.chart(f"涨跌停家数与炸板率（近{len(dates)}个交易日）",
              calc.limits_trend_option(dates, [r.get("zt_count") for r in rows], [r.get("dt_count") for r in rows],
                                       [r.get("zb_rate") for r in rows]), height=300, span="half")
    sec.chart(f"最高连板与昨日涨停溢价（近{len(dates)}个交易日）",
              calc.board_trend_option(dates, [r.get("max_board") for r in rows],
                                      [r.get("prev_zt_avg_pct") for r in rows]), height=300, span="half",
              note="溢价 = 昨日涨停股今日平均涨幅。")


def _ladder(sec, zt) -> None:
    if zt is None:
        return
    levels = calc.ladder_levels(zt)
    sec.ladder(f"连板梯队（涨停{len(zt)}家）", levels,
               note="按连板数分组，组内按首次封板时间排序；“一字”指集合竞价即封涨停且全天未开板。")


def _tables(sec, p) -> None:
    zt, zb, dt = p.get("zt"), p.get("zb"), p.get("dt")
    if zt is not None:
        sec.table(f"涨停池（{len(zt)}只）", [
            col("name", "名称"), col("code", "代码", "code"), col("lb", "连板", "int"), col("tj", "涨停统计"),
            col("pct", "涨跌幅", "pct"), col("amount", "成交额", "amount"), col("fund", "封板资金", "amount"),
            col("fbt", "首次封板"), col("lbt", "最后封板"), col("zbc", "炸板次数", "int"),
            col("hs", "换手率", "pct_plain"), col("ltsz", "流通市值", "amount"), col("hy", "所属行业"),
        ], calc.zt_rows(zt), note="按连板数、封板资金排序；涨停统计“N天M板”指近N个交易日内涨停M次。")
    if zb is not None:
        sec.table(f"炸板池（{len(zb)}只）", [
            col("name", "名称"), col("code", "代码", "code"), col("pct", "涨跌幅", "pct"),
            col("amount", "成交额", "amount"), col("fbt", "首次封板"), col("zbc", "炸板次数", "int"),
            col("zf", "振幅", "pct_plain"), col("hy", "所属行业"),
        ], calc.zb_rows(zb), span="half", note="按涨跌幅排序；炸板指盘中触及涨停、收盘未封住。")
    if dt is not None:
        sec.table(f"跌停池（{len(dt)}只）", [
            col("name", "名称"), col("code", "代码", "code"), col("pct", "涨跌幅", "pct"),
            col("amount", "成交额", "amount"), col("fund", "封单资金", "amount"), col("days", "连续跌停", "int"),
            col("oc", "开板次数", "int"), col("hy", "所属行业"),
        ], calc.dt_rows(dt), span="half", note="按连续跌停天数、封单资金排序。")


# ---------------------------------------------------------------- 口径说明

def _notes(sec, st) -> None:
    srcs = set(st["idx_src"].values())
    if srcs:
        text = "、".join(SRC_NAME.get(s, s) for s in sorted(srcs))
        extra = "（东方财富日线不可用时改用新浪财经）" if "sina" in srcs else ""
        sec.note(f"指数行情来源：{text}{extra}；涨跌幅 = 复盘日收盘 / 上一交易日收盘 - 1。")

    src = st["turnover_src"]
    if src == "em":
        sec.note("两市成交额 = 上证指数成交额（沪市全部股票，含科创板）+ 深证成指成交额（东方财富口径为深市全部股票，"
                 "含创业板，与深证综指一致），不含北交所；与交易所公布的股票成交金额相差约0.1%以内。来源：东方财富指数日线。")
    elif src == "exchange":
        sec.note("两市成交额 = 上交所“股票”成交金额 + 深交所“股票”成交金额（含B股，不含基金/债券，不含北交所）。"
                 "来源：上交所、深交所官网每日成交概况（东方财富指数日线不可用时的备用源）。")
    elif src == "history":
        sec.note("两市成交额取自本地历史记录（当日曾成功获取的数据），不含北交所。")

    bsrc = st["breadth_src"]
    basis = (st["breadth"] or {}).get("basis")
    if bsrc == "em":
        sec.note("涨跌家数与涨跌分布：东方财富涨停板专题“涨跌分布”（实时快照，已核对数据日期为复盘日）；"
                 "统计范围为沪深京A股（含北交所），不含ST股，与只统计沪深两市的软件家数会有差异。")
    elif bsrc == "legu":
        sec.note("涨跌家数：乐咕乐股赚钱效应统计（沪深A股，含ST，不含停牌，不含北交所；实时快照，已核对统计日期）。"
                 "东方财富涨跌分布不可用，本次没有涨跌分布图。")
    elif bsrc == "history":
        scope = ("东方财富涨跌分布口径：沪深京A股，含北交所，不含ST" if basis == "em"
                 else "乐咕乐股口径：沪深A股，含ST，不含北交所")
        sec.note(f"涨跌家数取自本地历史记录（复盘日当天曾成功获取的快照；{scope}）。")

    if st["pools"]:
        sec.note("涨停/炸板/跌停/昨日涨停：东方财富涨停板专题，按日期查询（含创业板、科创板、北交所，不含ST股；"
                 "涨停池不含上市后连续一字板未开板的新股）。"
                 "炸板 = 盘中触及涨停但收盘未封住；炸板率 = 炸板 / (涨停 + 炸板)；"
                 "连板晋级率 = 昨日涨停股中今日继续涨停的比例；“所属行业”为申万二级行业。")
    sec.note("情绪趋势：涨停板专题只能回查最近约30个自然日，更早的数据来自本地历史记录，缺失日期留空。")
