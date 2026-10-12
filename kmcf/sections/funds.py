"""龙虎榜与资金：看清当天大资金（主力、机构、游资、北向、两融）在做什么。

只陈述客观数据（净买入/净流出金额、连续天数、上榜原因等），不做任何买卖建议或预测。

数据源（主 → 备）：
- 大盘资金流向：东方财富 push2his（akshare.stock_market_fund_flow，可按日期回溯约 120 个交易日，加 20 秒总超时）
  → 新浪全市场个股资金流（只展示主力净流入/净流出家数，口径不同，不给大盘方向结论）。
- 龙虎榜 / 机构买卖 / 活跃营业部 / 北向成交 / 两融：东方财富数据中心（按日期查询，盘后陆续发布）。
  两融：两融账户统计（更及时）→ 两市合计历史（交易所口径）。
- 个股资金流排行（只有实时快照）：东方财富 push2 → 新浪 → 同花顺；后两者的涨跌幅、成交额改用新浪收盘行情。

金额口径：龙虎榜、机构的合计/家数/最多个股只统计“当日”上榜口径；只因“连续 3 日 / 10 日 / 严重异常期间”
等多日累计原因上榜的股票，金额是多日累计，单独标注，不混进合计、柱图和历史指标。

某个站点已连接失败（被拦截/超时），在 ctx.shared 记标记，后续同站点请求直接跳过（有可用缓存时除外）；
push2_down、push2his_down 会跨次生成保留 3 小时（见 report.PERSIST_SITE_FLAGS），其余标记只在本次生成内有效。

ctx.shared['funds.flow_rank']：个股资金排行（净流入/净流出前 N，含代码、名次、金额、口径来源），
供后面的“自选股/持仓”模块标注自选股是否进入排行；没取到排行时不设置。
"""

import datetime as dt
import math
from contextlib import contextmanager

import requests

from ..context import Context
from ..fetch import FetchError, SnapshotUnavailable, cache_key
from ..section import Section, col, fmt_amount, fmt_pct, num, tone_of
from . import _funds_calc as C
from . import _funds_data as D
from ._site import skip_reason

ID = "funds"
TITLE = "龙虎榜与资金"

LHB_PUBLISH_DONE = dt.time(18, 30)  # 龙虎榜一般 17:00~18:30 发布完
VOLATILE_UNTIL = dt.time(20, 0)     # 与 Fetcher 一致：复盘日当天 20:00 前，盘后数据可能仍在发布
MARKET_FLOW_TIMEOUT = 20            # akshare.stock_market_fund_flow 没设超时，这里限制总时长（秒）

# ctx.shared 里的“该站点已连接失败”标记（push2_down / push2his_down / sina_down / ths_down 与其他模块共用；
# push2_down、push2his_down 跨次生成保留 3 小时，其余只在本次生成内有效）
FLAG_PUSH2 = "push2_down"          # push2.eastmoney.com：个股资金排行
FLAG_PUSH2HIS = "push2his_down"    # push2his.eastmoney.com：大盘资金流向
FLAG_DC = "datacenter_down"        # datacenter-web.eastmoney.com：龙虎榜/机构/营业部/北向/两融
FLAG_SINA = "sina_down"            # vip.stock.finance.sina.com.cn：新浪个股资金流
FLAG_SINA_HQ = "sina_hq_down"      # hq.sinajs.cn：新浪收盘行情
FLAG_THS = "ths_down"              # data.10jqka.com.cn：同花顺个股资金流
SITE_NAMES = {FLAG_PUSH2: "push2.eastmoney.com", FLAG_PUSH2HIS: "push2his.eastmoney.com",
              FLAG_DC: "东方财富数据中心", FLAG_SINA: "新浪财经", FLAG_SINA_HQ: "新浪行情",
              FLAG_THS: "同花顺"}


class _Fail(RuntimeError):
    """本模块生成的中文错误说明，原样显示给用户。"""


class _Skip(_Fail):
    """站点已知不可用（本次生成中或近 3 小时内连接失败）而跳过（不重复请求）。"""


def _now() -> dt.datetime:
    return dt.datetime.now()


# ---------------------------------------------------------------- 错误说明

_CONN_TYPES = (requests.ConnectionError, ConnectionError)
_TIMEOUT_TYPES = (requests.Timeout, TimeoutError)
_CONN_WORDS = ("RemoteDisconnected", "Connection aborted", "ConnectionReset", "连接被重置", "Max retries exceeded")


def _err_chain(e: BaseException | None) -> list[BaseException]:
    out = []
    while e is not None and e not in out and len(out) < 6:
        out.append(e)
        e = e.__cause__ or e.__context__
    return out


def _is_conn_error(e: BaseException) -> bool:
    """连接级失败（被拦截、断开、超时）。解析类错误不算，不会让后续模块跳过该站点。"""
    return any(isinstance(x, _CONN_TYPES + _TIMEOUT_TYPES) for x in _err_chain(e))


def _short_err(e: BaseException, limit: int = 160) -> str:
    """把异常变成一句简短的中文说明（连接断开/超时/字段缺失等常见情况直接翻译）。"""
    if isinstance(e, _Fail):
        s = str(e).strip()
    else:
        chain = _err_chain(e)
        if any(isinstance(x, _TIMEOUT_TYPES) for x in chain):
            return "请求超时"
        if any(isinstance(x, _CONN_TYPES) for x in chain):
            return "服务器断开连接（可能被限流）"
        key = next((x for x in chain if isinstance(x, KeyError)), None)
        if key is not None:
            return f"接口返回格式变化（缺少 {key.args[0] if key.args else '?'} 字段）"
        text = " ".join(str(x) for x in chain)
        if any(k in text for k in _CONN_WORDS):
            return "服务器断开连接（可能被限流）"
        if "timed out" in text.lower():
            return "请求超时"
        if "Expecting value" in text:
            return "接口返回内容不是 JSON（可能被限制访问）"
        base = e.__cause__ if isinstance(e, FetchError) and e.__cause__ is not None else e
        s = str(base).strip()
    s = s.splitlines()[0] if s else type(e).__name__
    return s if len(s) <= limit else s[:limit] + "…"


@contextmanager
def _part(sec: Section, label: str):
    """sec.guard + 把原始英文异常翻译成中文说明。"""
    with sec.guard(label):
        try:
            yield
        except _Fail:
            raise
        except Exception as e:
            raise _Fail(_short_err(e)) from e


# ---------------------------------------------------------------- 取数辅助

def _cache_file(ctx: Context, func, kwargs: dict):
    return ctx.fetcher.raw_dir / f"{cache_key(func, (), kwargs)}.pkl"


def _cache_usable(ctx: Context, func, kwargs: dict, snapshot: bool = False, volatile: bool = False) -> bool:
    """这次调用会不会直接读缓存（规则与 Fetcher.call 一致）。"""
    if not _cache_file(ctx, func, kwargs).exists():
        return False
    f = ctx.fetcher
    skip = bool(getattr(f, "force", False)) or (volatile and bool(getattr(f, "same_day", False)))
    return not skip or (snapshot and not ctx.snapshot_ok)


def _site_fetch(ctx: Context, flag: str, func, *, snapshot: bool = False, volatile: bool = False,
                mark_any: bool = False, **kwargs):
    """ctx.fetch + 站点失败标记。

    该站点已标记连接失败、且这次需要真正联网时直接跳过（抛 _Skip）；连接级失败时设置标记。
    mark_any=True：任何失败都设标记（同花顺返回非数据页通常就是被限制访问）。
    """
    needs_network = not _cache_usable(ctx, func, kwargs, snapshot, volatile) and not (snapshot and not ctx.snapshot_ok)
    if ctx.shared.get(flag) and needs_network:
        site = SITE_NAMES[flag]
        raise _Skip(f"{site}{' ' if site[-1].isascii() else ''}{skip_reason(flag)}")
    try:
        return ctx.fetch(func, snapshot=snapshot, volatile=volatile, **kwargs)
    except SnapshotUnavailable:
        raise
    except FetchError as e:
        if mark_any or _is_conn_error(e):
            ctx.shared[flag] = True
        raise


def _fetch_dated(ctx: Context, func, accept, flag: str = FLAG_DC, **kwargs):
    """盘后按日期发布的数据（volatile）。结果不完整（accept 为 False，如空表）时删掉缓存，下次生成重新请求。"""
    df = _site_fetch(ctx, flag, func, volatile=True, **kwargs)
    if not accept(df):
        try:
            _cache_file(ctx, func, kwargs).unlink(missing_ok=True)
        except OSError:
            pass
    return df


def _nonempty(df) -> bool:
    return df is not None and not df.empty


def _has_date(column: str, day: dt.date):
    def check(df) -> bool:
        if df is None or df.empty or column not in df.columns:
            return False
        dates = [C.to_date(v) for v in df[column]]
        return day in dates
    return check


def _is_today(ctx: Context) -> bool:
    return ctx.trade_date >= _now().date()


def _same_day_pending(ctx: Context) -> bool:
    """复盘日就是今天且不到 20:00：盘后数据可能还没发布完。"""
    now = _now()
    return ctx.trade_date == now.date() and now.time() < VOLATILE_UNTIL


def _lhb_same_day_early(ctx: Context) -> bool:
    now = _now()
    return ctx.trade_date == now.date() and now.time() < LHB_PUBLISH_DONE


def _lhb_empty_text(ctx: Context) -> str:
    if _is_today(ctx):
        return "龙虎榜尚未发布（通常17:00后），晚些重新生成即可看到"
    return f"未查询到{ctx.date_iso}的龙虎榜数据"


def _records(df) -> list[dict]:
    return [] if df is None or df.empty else df.to_dict("records")


def _hbar_height(n: int) -> int:
    return max(160, 26 * n + 30)


def _name(d: dict | None) -> str:
    if not d:
        return "—"
    return str(d.get("name") or d.get("code") or "—")


def _top_title(prefix: str, n: int, top_n: int, extra: str = "") -> str:
    """'机构净买入前20' / 不足 top_n 只时 '机构净买入（共11只）' / 没有时 '机构净买入（无）'。extra 为附加说明。"""
    tail = f"，{extra}" if extra else ""
    if not n:
        return f"{prefix}（无{tail}）"
    if n < top_n:
        return f"{prefix}（共{n}只{tail}）"
    return f"{prefix}前{n}" + (f"（{extra}）" if extra else "")


def _day_rows(df):
    return df[~df["multi_day"].astype(bool)]


def _md_rows(df):
    return df[df["multi_day"].astype(bool)]


def _sina_all(ctx: Context, st: dict):
    """新浪全市场个股资金流（大盘备用源与个股排行共用一次请求；失败结果也记住，不重复请求）。"""
    if "_sina" not in st:
        try:
            st["_sina"] = _site_fetch(ctx, FLAG_SINA, D.sina_fund_flow_all, snapshot=True)
        except Exception as e:  # noqa: BLE001  记下来，调用方各自处理
            st["_sina"] = e
    v = st["_sina"]
    if isinstance(v, Exception):
        raise v
    return v


# ---------------------------------------------------------------- 1. 大盘资金流向

def _market_flow(ctx: Context, sec: Section, st: dict) -> None:
    """主：东方财富 push2his（可按日期回溯）；备：新浪个股主力净流入家数（快照，口径不同，不给方向结论）。"""
    try:
        _market_flow_em(ctx, sec, st)
        return
    except Exception as e:
        em_err = _short_err(e)
        em_down = isinstance(e, _Skip) or _is_conn_error(e)
        ctx.log.warning("[funds] 大盘资金流向（东方财富）失败：%s", em_err)
    # 错误框里一句话说清缺了什么；站点跳过、超时等具体原因只写日志
    em_text = ("东方财富大盘资金流" + ("暂时无法访问" if em_down else f"未取到（{em_err}）")
               + "，主力净流入金额与趋势缺失")
    try:
        ok = _market_flow_sina(ctx, sec, st)
    except SnapshotUnavailable:
        ok = False
    except Exception as e:
        raise _Fail(f"{em_text}；新浪备用源：{_short_err(e)}") from e
    if not ok:
        if not ctx.snapshot_ok:   # 历史日期 / 盘中运行：新浪只有实时快照，属于预期降级，写进口径说明
            sec.note("大盘资金流向（新浪备用源）：只有实时快照，当前不对应复盘日且无当日缓存，本次省略。")
            sec.error(em_text)
            return
        raise _Fail(f"{em_text}；新浪备用源只有实时快照，当前不对应复盘日且无当日缓存")
    sec.error(f"{em_text}，仅展示新浪口径的主力净流入/净流出个股家数")


def _market_flow_em(ctx: Context, sec: Section, st: dict) -> None:
    import akshare as ak

    func = D.with_timeout(ak.stock_market_fund_flow, MARKET_FLOW_TIMEOUT)  # 保留函数名，缓存键不变
    mff = _fetch_dated(ctx, func, accept=_has_date("日期", ctx.trade_date), flag=FLAG_PUSH2HIS)
    series = C.market_flow_series(mff, ctx.trade_date, 60)
    day = C.market_flow_day(series, ctx.trade_date)
    if day is None:
        latest = series["date"].iloc[-1].isoformat() if len(series) else "无"
        raise _Fail(f"暂无{ctx.date_iso}的数据（最新为{latest}）")
    if day["main"] is None:
        raise _Fail(f"{ctx.date_iso}的主力净流入数据缺失")

    sec.note("大盘资金流向：东方财富（沪深两市合计，主力 = 超大单 + 大单）。")
    st["flow"] = day
    sec.kpi("主力净流入", fmt_amount(day["main"], signed=True), f"占成交额{fmt_pct(day['main_ratio'])}",
            tone_of(day["main"]))
    sec.kpi("超大单净流入", fmt_amount(day["super"], signed=True), f"大单{fmt_amount(day['big'], signed=True)}",
            tone_of(day["super"]))
    retail = (num(day["mid"], 0) or 0) + (num(day["small"], 0) or 0)
    sec.kpi("中小单净流入", fmt_amount(retail, signed=True),
            f"中单{fmt_amount(day['mid'], signed=True)} / 小单{fmt_amount(day['small'], signed=True)}",
            tone_of(retail))
    k = day["streak"]
    streak_txt = f"主力资金连续{abs(k)}日{'净流入' if k > 0 else '净流出'}；" if abs(k) >= 2 else ""
    sec.chart(f"大盘主力资金净流入（近{len(series)}个交易日）", C.flow_option(series), height=300,
              note=f"{streak_txt}近5日累计{fmt_amount(day['sum5'], signed=True)}，"
                   f"近20日累计{fmt_amount(day['sum20'], signed=True)}。红柱为净流入，绿柱为净流出。")


def _market_flow_sina(ctx: Context, sec: Section, st: dict) -> bool:
    """备用：新浪口径的主力净流入/净流出家数。只能取实时快照；成功返回 True。"""
    total = C.sina_market_total(_sina_all(ctx, st))
    if total is None:
        raise _Fail("新浪返回的个股数量过少，不像全市场数据")
    st["flow_alt"] = total
    sec.kpi("主力净流入个股（新浪）", f"{total['n_in']}家",
            f"净流出{total['n_out']}家 · 覆盖{total['covered']}只", "neutral")
    sec.note(f"大盘资金流向（新浪备用源）：当日有成交的A股{total['count']}只，其中{total['no_main']}只新浪未给出主力数据，"
             f"不计入家数。新浪口径全部个股主力净流入合计{fmt_amount(total['main'], signed=True)}，"
             "新浪与东方财富划分主力的标准不同，该合计不代表大盘主力资金的整体流向，也不写入历史趋势。")
    return True


# ---------------------------------------------------------------- 2. 龙虎榜

LHB_COLUMNS = [
    col("name", "名称"), col("code", "代码", "code"), col("close", "收盘价", "price"),
    col("pct", "涨跌幅", "pct"), col("net", "龙虎榜净买额", "flow"), col("buy", "买入额", "amount"),
    col("sell", "卖出额", "amount"), col("net_ratio", "净买额占成交", "pct"),
    col("turnover", "换手率", "pct_plain"), col("basis", "金额口径"), col("reasons", "上榜原因"),
    col("explain", "解读"),
]


def _md_text(s: dict) -> str:
    """'另有 14 只仅因多日累计上榜（+0.90亿），未计入'。"""
    if not s.get("md_n"):
        return ""
    amt = f"（{fmt_amount(s['md_net'], signed=True)}）" if s.get("md_net") is not None else ""
    return f"另有{s['md_n']}只仅因多日累计上榜{amt}，未计入"


def _lhb(ctx: Context, sec: Section, st: dict) -> None:
    raw = _fetch_dated(ctx, D.em_lhb_detail, accept=_nonempty, date=ctx.date_iso)
    lhb = C.merge_lhb(raw)
    ctx.shared["funds.lhb"] = lhb  # 供其他模块（如自选股）查询是否上榜
    st["lhb_df"] = lhb
    sec.note("龙虎榜、机构、营业部：东方财富数据中心（按日期查询）。")
    if lhb.empty:
        st["lhb_empty"] = _lhb_empty_text(ctx)
        sec.note(f"龙虎榜：{st['lhb_empty']}。")   # 状态说明写进口径说明，不占用复盘要点
        if _is_today(ctx):
            sec.kpi("龙虎榜", "未发布", "通常17:00后发布", "warn")
        else:
            sec.kpi("龙虎榜", "无数据", "该日无龙虎榜记录", "warn")
        return
    if _lhb_same_day_early(ctx):
        sec.note("现在早于18:30，龙虎榜、机构和营业部数据可能仍在陆续发布，晚些重新生成可得到完整数据。")

    s = C.lhb_stats(lhb)
    st["lhb"] = s
    sec.kpi("龙虎榜上榜", f"{s['count']}只", f"当日口径净买入{s['buy_n']}只 / 净卖出{s['sell_n']}只")
    sub = f"当日上榜{s['day_n']}只合计"
    if s["md_n"]:
        sub += "；" + _md_text(s)
    sec.kpi("龙虎榜净买入（当日口径）", fmt_amount(s["net_sum"], signed=True), sub, tone_of(s["net_sum"]))
    if s["net_sum"] is None:
        sec.error("龙虎榜：当日上榜个股的净买额全部缺失，未统计合计")

    day = _day_rows(lhb)
    buys = day[day["net"] > 0].head(10)
    sells = day[day["net"] < 0].sort_values("net").head(10)
    if not buys.empty:
        sec.chart(_top_title("龙虎榜净买入", len(buys), 10, "当日口径"),
                  C.hbar_option(buys["name"].tolist(), [C.to_yi(v) for v in buys["net"]]),
                  height=_hbar_height(len(buys)), span="half")
    if not sells.empty:
        sec.chart(_top_title("龙虎榜净卖出", len(sells), 10, "当日口径"),
                  C.hbar_option(sells["name"].tolist(), [C.to_yi(v) for v in sells["net"]], label_pos="left"),
                  height=_hbar_height(len(sells)), span="half")
    note = ("只含 A 股（已排除可转债、B 股）。同一股票的多个上榜原因已合并为一行，金额优先取当日上榜口径。"
            "只因多日累计原因上榜的股票，金额为3日或多日累计（见“金额口径”列），不计入上方合计和柱图。")
    if (lhb["basis"] == C.BASIS[C.RANK_NO_SEAT]).any():
        note += "“连续10个交易日/严重异常期间”类记录数据中心没有给出席位明细，金额显示为“—”。"
    note += "净买额占成交 = 龙虎榜净买额 / 个股成交额。“解读”为东方财富对上榜席位的描述。"
    sec.table(f"龙虎榜个股（{s['count']}只，按净买额排序）", LHB_COLUMNS, _records(lhb), max_rows=300, note=note)


# ---------------------------------------------------------------- 3. 机构动向

ORG_COLUMNS = [
    col("name", "名称"), col("code", "代码", "code"), col("pct", "涨跌幅", "pct"),
    col("seats", "机构席位", align="center"), col("net", "机构净买额", "flow"), col("ratio", "占成交", "pct"),
]
ORG_MD_COLUMNS = ORG_COLUMNS + [col("basis", "金额口径"), col("reasons", "上榜原因")]


def _org(ctx: Context, sec: Section, st: dict, top_n: int) -> None:
    raw = _fetch_dated(ctx, D.em_lhb_org, accept=_nonempty, date=ctx.date_iso)
    org = C.normalize_org(raw)
    if org.empty:
        if st.get("lhb_empty"):
            return  # 龙虎榜本身还没发布，已有提示
        lhb = st.get("lhb_df")
        n = C.explain_mentions_org(lhb)
        if lhb is None:
            sec.note("机构动向：机构买卖统计暂未获取到。")
        elif n:
            sec.note(f"机构动向：机构买卖统计暂未获取到（数据中心可能尚未发布），龙虎榜“解读”中有{n}只股票提到机构席位，"
                     "可晚些重新生成。")
        else:
            sec.note("机构动向：当日龙虎榜没有机构专用席位成交记录。")
        return

    s = C.flow_stats(org)
    st["org"] = s
    sub = f"净买入{s['buy_n']}只 / 净卖出{s['sell_n']}只"
    if s["md_n"]:
        sub += "；" + _md_text(s)
    sec.kpi("机构净买入（当日口径）", fmt_amount(s["net_sum"], signed=True), sub, tone_of(s["net_sum"]))

    day, md = _day_rows(org), _md_rows(org)
    buys = day[day["net"] > 0].head(top_n)
    sells = day[day["net"] < 0].sort_values("net").head(top_n)
    if day.empty:
        sec.note("机构动向：当日上榜口径没有机构专用席位记录。")
    else:
        note = "机构席位 = 龙虎榜“机构专用”买入/卖出席位数；占成交 = 机构净买额 / 个股成交额；只含当日上榜口径。"
        sec.table(_top_title("机构净买入", len(buys), top_n), ORG_COLUMNS, _records(buys), span="half", note=note)
        sec.table(_top_title("机构净卖出", len(sells), top_n), ORG_COLUMNS, _records(sells), span="half")
    if not md.empty:
        sec.table(f"仅因多日累计上榜的机构买卖（{len(md)}只，金额为多日累计，未计入合计）", ORG_MD_COLUMNS,
                  _records(md), note="这些股票当日只因“连续3个交易日/连续10个交易日/严重异常期间”等多日累计原因上榜，"
                                     "机构买卖额和占成交都是整个统计期间的累计值，不能与当日数据相加。")


# ---------------------------------------------------------------- 4. 活跃营业部

BRANCH_COLUMNS = [
    col("name", "营业部"), col("buy_n", "买入只数", "int"), col("sell_n", "卖出只数", "int"),
    col("buy", "买入总额", "amount"), col("sell", "卖出总额", "amount"), col("net", "净额", "flow"),
    col("stocks", "涉及个股"),
]


def _branch(ctx: Context, sec: Section, st: dict, top_n: int) -> None:
    raw = _fetch_dated(ctx, D.em_lhb_branch, accept=_nonempty, date=ctx.date_iso)
    br = C.normalize_branch(raw)
    if br.empty:
        lhb = st.get("lhb_df")
        if lhb is not None and not lhb.empty:
            sec.note("活跃营业部：数据暂未获取到（数据中心可能尚未发布），可晚些重新生成。")
        return
    st["hsgt_seats"] = _records(br[br["kind"] == "北向"])
    act = C.top_active_branches(br, top_n)
    if act.empty:
        sec.note("活跃营业部：当日上榜席位只有沪/深股通专用和机构专用席位。")
        return
    yyb = br[br["kind"] == "营业部"]
    st["branch"] = {"most_active": yyb.loc[yyb["total"].idxmax()].to_dict(), "count": int(len(yyb))}
    sec.table(f"活跃营业部（买卖总额前{len(act)}，按净额排序）", BRANCH_COLUMNS, _records(act),
              note=f"当日共{len(yyb)}家营业部上榜；已排除沪/深股通专用和机构专用席位。表中既有常见的游资营业部，"
                   "也可能有外资券商、券商总部等席位，并非都是游资。“涉及个股”含该营业部买入和卖出的上榜股票。")


# ---------------------------------------------------------------- 5. 个股主力资金（快照，三个源依次尝试）

def _rank_em(ctx: Context, st: dict, n: int):
    kw_in, kw_out = {"direction": "in", "n": n + 10}, {"direction": "out", "n": n + 10}
    din = _site_fetch(ctx, FLAG_PUSH2, D.em_fund_flow_rank, snapshot=True, **kw_in)
    dout = _site_fetch(ctx, FLAG_PUSH2, D.em_fund_flow_rank, snapshot=True, **kw_out)
    day = C.em_rank_date(din)
    if day is not None and day != ctx.trade_date:
        raise _Fail(f"快照日期{day}与复盘日不符")
    return C.normalize_em_rank(din), C.normalize_em_rank(dout)


def _rank_sina(ctx: Context, st: dict, n: int):
    rank = C.normalize_sina_rank(_sina_all(ctx, st))  # 与大盘备用源共用一次请求
    return rank, rank


def _rank_ths(ctx: Context, st: dict, n: int):
    pages = max(1, math.ceil(n / 50))
    din = _site_fetch(ctx, FLAG_THS, D.ths_fund_flow_rank, snapshot=True, mark_any=True, direction="in", pages=pages)
    dout = _site_fetch(ctx, FLAG_THS, D.ths_fund_flow_rank, snapshot=True, mark_any=True, direction="out", pages=pages)
    return C.normalize_ths_rank(din), C.normalize_ths_rank(dout)


EM = "东方财富"
FLOW_RANK = "funds.flow_rank"   # ctx.shared：个股资金排行（见模块说明），自选股模块读取
RANK_SOURCES = [
    # (名称, 取数函数, 净额列名, 占比列名, 口径说明)
    (EM, _rank_em, "主力净流入", "主力净占比", "主力 = 超大单 + 大单"),
    ("新浪财经", _rank_sina, "主力净流入", "主力净占比", "新浪主力口径，划分标准与东方财富不同"),
    ("同花顺", _rank_ths, "资金净额", "净额占成交", "同花顺按主动买卖统计的流入减流出，不区分单子大小，与“主力”口径不同"),
]


def _close_quotes(ctx: Context, frames: list):
    """新浪/同花顺的资金流统计不含尾盘集合竞价：涨跌幅、成交额改用新浪收盘行情（核对行情日期 = 复盘日）。"""
    codes = sorted({c for f in frames for c in f["code"] if c})
    q = _site_fetch(ctx, FLAG_SINA_HQ, D.sina_quotes, snapshot=True, codes=",".join(codes))
    out = [C.apply_quotes(f, q, ctx.trade_date) for f in frames]
    if not any(f["pct"].notna().any() for f in out if len(f)):
        raise _Fail("新浪收盘行情的日期与复盘日不符")
    return out


def _blank_quotes(df):
    return df.assign(price=float("nan"), pct=float("nan"), amount=float("nan"))


def _stock_flow(ctx: Context, sec: Section, st: dict, top_n: int) -> None:
    fails, skipped, snap, chosen = {}, {}, [], None
    down = set()   # 连接级失败或已标记跳过的源：说明里只写“暂时无法访问”
    for name, getter, net_label, ratio_label, basis in RANK_SOURCES:
        try:
            rin, rout = getter(ctx, st, top_n)
        except SnapshotUnavailable:
            snap.append(name)
            continue
        except _Skip as e:
            skipped[name] = str(e)
            down.add(name)
            continue
        except Exception as e:  # 网络失败、格式变化等，换下一个源
            ctx.log.warning("[funds] 个股资金流 %s 失败：%s", name, e)
            fails[name] = _short_err(e)
            if _is_conn_error(e):
                down.add(name)
            continue
        inflow, outflow = C.split_rank(rin, top_n, "in"), C.split_rank(rout, top_n, "out")
        if inflow.empty and outflow.empty:
            fails[name] = "返回数据为空"
            continue
        chosen = (name, net_label, ratio_label, basis, inflow, outflow)
        break

    if chosen is None:
        if snap and not fails:
            sec.note("个股资金排行只有实时快照，当前时刻的数据不对应复盘日（盘中运行或历史日期）且无当日缓存，本次省略。")
        else:
            parts = [f"{k}：{v}" for k, v in {**fails, **skipped}.items()] + [f"{k}：快照不对应复盘日" for k in snap]
            sec.error("个股主力资金：所有数据源均不可用（" + "；".join(parts) + "）")
        return

    name, net_label, ratio_label, basis, inflow, outflow = chosen
    quote_msg = ""
    if name != EM:
        try:
            inflow, outflow = _close_quotes(ctx, [inflow, outflow])
            quote_msg = "涨跌幅、成交额取自新浪收盘行情。"
        except Exception as e:  # noqa: BLE001  行情拿不到就不显示涨跌幅/成交额，排行本身照常展示
            ctx.log.warning("[funds] 收盘行情获取失败：%s", e)
            inflow, outflow = _blank_quotes(inflow), _blank_quotes(outflow)
            quote_msg = f"收盘行情获取失败（{_short_err(e)}），表中不显示涨跌幅和成交额。"
    has_quote = any(f["pct"].notna().any() for f in (inflow, outflow) if len(f))

    word_in, word_out = ("主力净流入", "主力净流出") if net_label == "主力净流入" else ("资金净流入", "资金净流出")
    st["rank"] = {"source": name, "label": net_label, "word_in": word_in, "word_out": word_out,
                  "top_in": inflow.iloc[0].to_dict() if len(inflow) else None,
                  "top_out": outflow.iloc[0].to_dict() if len(outflow) else None}
    ctx.shared[FLOW_RANK] = _flow_rank_shared(name, basis, net_label, word_in, word_out, top_n, inflow, outflow)
    cols = [col("name", "名称"), col("code", "代码", "code")]
    if has_quote:
        cols.append(col("pct", "涨跌幅", "pct"))
    cols += [col("net", net_label, "flow"), col("ratio", ratio_label, "pct")]
    if has_quote:
        cols.append(col("amount", "成交额", "amount"))
    sec.table(_top_title(f"个股{word_in}", len(inflow), top_n), cols, _records(inflow), span="half")
    sec.table(_top_title(f"个股{word_out}", len(outflow), top_n), cols, _records(outflow), span="half")

    when = "收盘后快照" if ctx.snapshot_ok else "当日收盘后缓存的快照"
    msg = f"个股资金排行：{name}{when}（{basis}）。"
    if name == "新浪财经":
        msg += "新浪资金流统计不含尾盘集合竞价，" + quote_msg
    elif quote_msg:
        msg += quote_msg
    if name != EM:
        if EM in down:
            msg += f"东方财富暂时无法访问，已改用{name}。"
        elif EM in snap:
            msg += "未使用东方财富：快照不对应复盘日且无当日缓存。"
        else:
            msg += f"未使用东方财富：{fails.get(EM, '不可用')}。"
    sec.note(msg)


def _flow_rank_shared(source: str, basis: str, net_label: str, word_in: str, word_out: str, top_n: int,
                      inflow, outflow) -> dict:
    """ctx.shared['funds.flow_rank']：净流入/净流出前 N（名次从 1 起，金额单位元）。

    {"source": "新浪财经", "basis": 口径说明, "net_label": "主力净流入" | "资金净额",
     "word_in": "主力净流入" | "资金净流入", "word_out": "主力净流出" | "资金净流出", "top_n": N,
     "inflow": [{"code", "name", "rank", "net"}, ...], "outflow": [...]}
    """
    def items(df) -> list[dict]:
        return [{"code": str(r.get("code") or ""), "name": _name(r), "rank": i, "net": num(r.get("net"))}
                for i, r in enumerate(_records(df), 1)]
    return {"source": source, "basis": basis, "net_label": net_label, "word_in": word_in, "word_out": word_out,
            "top_n": int(top_n), "inflow": items(inflow), "outflow": items(outflow)}


# ---------------------------------------------------------------- 6. 北向资金

def _seats_text(seats: list[dict]) -> str:
    parts = []
    for s in seats:
        net = num(s.get("net"))
        if net is None:
            continue
        word = "净买入" if net >= 0 else "净卖出"
        parts.append(f"{s['name']}{word}{fmt_amount(abs(net))}（涉及{s.get('stock_n', 0)}只）")
    return "龙虎榜上榜个股中，" + "，".join(parts) + "。" if parts else ""


def _northbound(ctx: Context, sec: Section, st: dict) -> None:
    sec.note("北向资金：2024年8月起沪深港通不再披露每日净买入，这里只展示北向成交总额（东方财富数据中心）"
             "和龙虎榜上的沪/深股通专用席位，二者都不代表北向资金整体的净流向。")
    pending = _same_day_pending(ctx)
    # 复盘日当天 20:00 前缺复盘日数据：可能还没发布，删缓存下次重取；其余情况缺数据多为港股休市，保留缓存
    accept = _has_date("TRADE_DATE", ctx.trade_date) if pending else _nonempty
    raw = _fetch_dated(ctx, D.em_northbound_hist, accept=accept, date=ctx.date_iso, n=30)
    nb = C.northbound_series(raw, ctx.trade_date, 20)
    seats = _seats_text(st.get("hsgt_seats") or [])
    if nb.empty:
        sec.note("北向成交数据暂缺（数据中心未返回成交总额）。" + seats)
        return
    last = nb.iloc[-1]
    sub = f"{last['date']:%m-%d}"
    if len(nb) >= 2 and num(nb["amount"].iloc[-2]):
        chg = (last["amount"] / nb["amount"].iloc[-2] - 1) * 100
        sub += f" · 较前日{chg:+.1f}%"
    if last["date"] != ctx.trade_date:
        if pending:
            sub += "（复盘日暂无数据）"
            sec.note(f"北向成交：数据中心暂无{ctx.date_iso}的记录（可能尚未发布，或港股休市），"
                     f"显示{last['date']:%m-%d}的数据。")
        else:
            sub += "（复盘日无北向成交记录）"
            sec.note(f"北向成交：数据中心没有{ctx.date_iso}的记录（通常是港股休市、北向暂停交易），"
                     f"显示上一交易日{last['date']:%m-%d}的数据。")
    sec.kpi("北向成交总额", fmt_amount(last["amount"]), sub)
    st["northbound"] = {"amount": float(last["amount"]), "date": last["date"]}
    sec.chart(f"北向成交总额（近{len(nb)}个交易日）", C.northbound_option(nb), height=260, span="half",
              note=seats or "成交总额 = 买入成交额 + 卖出成交额。")


# ---------------------------------------------------------------- 7. 两融

def _margin(ctx: Context, sec: Section, st: dict) -> None:
    ms, src, errs = None, "", []
    for func, source, label in ((D.em_margin_daily, "daily", "两融账户统计"),
                                (D.em_margin_total, "total", "沪深两市融资融券合计")):
        try:
            raw = _fetch_dated(ctx, func, accept=_has_date(
                "STATISTICS_DATE" if source == "daily" else "DIM_DATE", ctx.trade_date), date=ctx.date_iso, n=60)
            ms = C.margin_series(raw, source, ctx.trade_date, 60)
        except Exception as e:
            errs.append(f"{label}：{_short_err(e)}")
            continue
        if not ms.empty:
            src = f"东方财富数据中心·{label}"
            break
    if ms is None or ms.empty:
        raise _Fail("两融数据不可用" + (f"（{'；'.join(errs)}）" if errs else ""))

    last = ms.iloc[-1]
    chg = num(last["fin"] - ms["fin"].iloc[-2]) if len(ms) >= 2 else None
    data_date = last["date"]
    prev_date = ms["date"].iloc[-2] if len(ms) >= 2 else None
    st["margin"] = {"fin": float(last["fin"]), "margin": float(last["margin"]), "chg": chg, "date": data_date,
                    "prev_date": prev_date}
    sec.kpi("两融余额", fmt_amount(last["margin"]), _fin_text(st["margin"]), tone_of(chg))
    ratio = num(last.get("trade_ratio"))
    note = f"融资买入额{fmt_amount(last['fin_buy'])}" + (f"（占A股成交额{ratio:.2f}%）。" if ratio else "。")
    sec.chart(f"融资余额（近{len(ms)}个交易日，截至{data_date:%m-%d}）", C.margin_option(ms), height=260,
              span="half", note=note if num(last.get("fin_buy")) is not None else None)
    msg = f"两融：{src}。"
    if data_date != ctx.trade_date:
        msg += f"两融数据T+1披露，复盘日{ctx.date_iso}的数据尚未发布，KPI和图表截至{data_date.isoformat()}。"
    sec.note(msg)


def _fin_text(mg: dict) -> str:
    """'融资余额2.53万亿（10-09），较10-08减少145.41亿'（KPI 与要点共用）。"""
    s = f"融资余额{fmt_amount(mg['fin'])}（{mg['date']:%m-%d}）"
    if mg.get("chg") is not None and mg.get("prev_date") is not None:
        word = "增加" if mg["chg"] >= 0 else "减少"
        s += f"，较{mg['prev_date']:%m-%d}{word}{fmt_amount(abs(mg['chg']))}"
    return s


# ---------------------------------------------------------------- 要点 & 历史指标

def _flow_word(v, pos="净流入", neg="净流出") -> str:
    return pos if (num(v, 0) or 0) >= 0 else neg


def _tops_text(s: dict, buy_word: str, sell_word: str) -> str:
    parts = []
    if s.get("top_buy"):
        parts.append(f"{buy_word}：{_name(s['top_buy'])}（{fmt_amount(s['top_buy']['net'], signed=True)}）")
    if s.get("top_sell"):
        parts.append(f"{sell_word}：{_name(s['top_sell'])}（{fmt_amount(s['top_sell']['net'], signed=True)}）")
    return "，".join(parts)


def _summary(sec: Section, st: dict) -> None:
    """复盘要点：只放有数据的客观要点（数据不可用的说明已在错误框/口径说明里）。

    写法与“大盘与情绪”模块一致：句末用“。”，名称/文字与数字、数字与单位之间不加空格（如“净买入7.85亿”）。
    """
    f = st.get("flow")
    if f:
        s = f"主力资金{_flow_word(f['main'])}{fmt_amount(abs(f['main']))}"
        if num(f.get("main_ratio")) is not None:
            s += f"（占成交额{abs(num(f['main_ratio'])):.2f}%）"
        k = f["streak"]
        if abs(k) >= 2:
            s += f"，连续{abs(k)}日{'净流入' if k > 0 else '净流出'}"
        s += f"；近5日累计{_flow_word(f['sum5'])}{fmt_amount(abs(f['sum5']))}"
        sec.add_summary(s + "。")

    lhb = st.get("lhb")
    if lhb:
        s = f"龙虎榜{lhb['count']}只个股上榜"
        if lhb["net_sum"] is not None and lhb["day_n"]:
            s += f"，当日口径合计{_flow_word(lhb['net_sum'], '净买入', '净卖出')}{fmt_amount(abs(lhb['net_sum']))}"
        if lhb["md_n"]:
            s += f"（另有{lhb['md_n']}只仅因多日累计上榜，未计入）"
        tops = _tops_text(lhb, "净买入最多", "净卖出最多")
        sec.add_summary(s + (f"；{tops}" if tops else "") + "。")

    org = st.get("org")
    if org and org["day_n"] and org["net_sum"] is not None:
        s = f"机构席位（当日口径）合计{_flow_word(org['net_sum'], '净买入', '净卖出')}{fmt_amount(abs(org['net_sum']))}"
        tops = _tops_text(org, "机构净买入最多", "净卖出最多")
        sec.add_summary(s + (f"；{tops}" if tops else "") + "。")

    br = st.get("branch")
    if br and br["most_active"]:
        m = br["most_active"]
        sec.add_summary(f"最活跃营业部：{_name(m)}（买卖总额{fmt_amount(m['total'])}，"
                        f"净额{fmt_amount(m['net'], signed=True)}，涉及{m['stock_n']}只）。")

    rk = st.get("rank")
    if rk and (rk["top_in"] or rk["top_out"]):
        parts = []
        if rk["top_in"]:
            parts.append(f"{rk['word_in']}最多：{_name(rk['top_in'])}（{fmt_amount(rk['top_in']['net'], signed=True)}）")
        if rk["top_out"]:
            parts.append(f"{rk['word_out']}最多：{_name(rk['top_out'])}（{fmt_amount(rk['top_out']['net'], signed=True)}）")
        sec.add_summary("个股" + "，".join(parts) + f"，数据源为{rk['source']}。")

    mg = st.get("margin")
    if mg and mg["chg"] is not None:
        sec.add_summary(_fin_text(mg) + "。")


def _save_history(ctx: Context, st: dict) -> None:
    """逐日指标（元）：东方财富主力净流入、龙虎榜上榜家数、龙虎榜/机构当日口径净买入。缺失或 NaN 的值跳过。"""
    lhb, org = st.get("lhb") or {}, st.get("org") or {}
    values = {"funds_main_net": (st.get("flow") or {}).get("main"),
              "funds_lhb_count": lhb.get("count"), "funds_lhb_net": lhb.get("net_sum"),
              "funds_org_net": org.get("net_sum")}
    metrics = {k: round(f) for k, v in values.items() if (f := num(v)) is not None}
    if metrics:
        ctx.history.upsert(ctx.date_iso, metrics)


# ---------------------------------------------------------------- 入口

def build(ctx: Context) -> Section:
    sec = Section(ID, TITLE)
    top_n = int(ctx.cfg.get("top_n", 20) or 20)
    st: dict = {}  # 各子功能算出的事实，最后统一生成要点

    with _part(sec, "大盘资金流向"):
        _market_flow(ctx, sec, st)
    with _part(sec, "龙虎榜"):
        _lhb(ctx, sec, st)
    with _part(sec, "机构动向"):
        _org(ctx, sec, st, top_n)
    with _part(sec, "活跃营业部"):
        _branch(ctx, sec, st, top_n)
    with _part(sec, "个股主力资金"):
        _stock_flow(ctx, sec, st, top_n)
    with _part(sec, "北向资金"):
        _northbound(ctx, sec, st)
    with _part(sec, "两融"):
        _margin(ctx, sec, st)

    with _part(sec, "复盘要点"):
        _summary(sec, st)
    with _part(sec, "写入历史指标"):
        _save_history(ctx, st)
    return sec
