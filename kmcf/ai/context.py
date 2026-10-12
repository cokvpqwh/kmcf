"""把复盘报告（reports/*.json）和逐日历史（data/history.csv）压成给 AI 看的紧凑中文文本。

- 只用已经格式化好的文字（KPI 的 value/sub、表格单元格的 text），去掉图表；
- 表格按“列名|列名…”表头 + 每行“值|值…”输出，行数随长度预算自动减少；
- include_holdings=False（默认）时，“自选股/持仓”模块里与持仓数量/成本/市值/盈亏相关的要点、指标、表格、列、
  口径说明一律剔除，自选股备注（用户自由填写，可能含持仓信息）也不发送；history.csv 里的持仓列同样剔除。
"""

import math

from .. import paths

# 持仓相关字段的关键词（只在“自选股/持仓”模块和 history.csv 的持仓列上使用）
HOLDING_WORDS = ("持仓", "持股", "股数", "成本", "市值", "盈亏", "仓位")
PRIVATE_COLUMNS = ("备注",)   # 自选股备注是用户自由填写的文字，不发送

# (每张表最多行数, 是否带口径说明)：从最详细开始，超出 max_chars 就降一级
_LEVELS = ((15, True), (12, True), (10, True), (8, False), (6, False), (4, False), (2, False), (0, False))
_NOTE_LIMIT = 160
_LADDER_ITEMS = 12


def _clean(text) -> str:
    """单元格/文字：去掉换行和表格分隔符。"""
    if text is None:
        return ""
    if isinstance(text, float) and not math.isfinite(text):
        return ""
    return " ".join(str(text).replace("|", "/").split())


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _is_holding_section(sec: dict) -> bool:
    title = str(sec.get("title") or "")
    return sec.get("id") == "watchlist" or "持仓" in title or "自选" in title


def _has_holding_word(text) -> bool:
    t = str(text or "")
    return any(w in t for w in HOLDING_WORDS)


def _table_text(b: dict, max_rows: int, drop_cols: set[int]) -> list[str]:
    cols = [c for i, c in enumerate(b.get("columns") or []) if i not in drop_cols]
    rows = b.get("rows") or []
    title = _clean(b.get("title")) or "表格"
    if not cols:
        return []
    if not rows:
        return [f"【表格】{title}：暂无数据"]
    n = len(rows)
    if max_rows <= 0:
        return [f"【表格】{title}（共 {n} 行，篇幅所限未列出）"]
    keep = [i for i in range(len(b.get("columns") or [])) if i not in drop_cols]

    def fmt(row) -> str:
        cells = []
        for i in keep:
            cell = row[i] if i < len(row) else {}
            cells.append(_clean(cell.get("text") if isinstance(cell, dict) else cell) or "—")
        return "|".join(cells)

    head = "|".join(_clean(c.get("label")) for c in cols)
    # 排行类长表：前后各取一部分（如行业涨跌幅排行既看领涨也看领跌）
    if n > max_rows and "排行" in title and max_rows >= 6:
        top, bottom = max_rows - max_rows // 3, max_rows // 3
        lines = [f"【表格】{title}（共 {n} 行，列出前 {top} 行和后 {bottom} 行）", head]
        lines += [fmt(r) for r in rows[:top]]
        lines.append(f"……（中间 {n - top - bottom} 行省略）")
        lines += [fmt(r) for r in rows[n - bottom:]]
        return lines
    shown = rows[:max_rows]
    tip = f"（共 {n} 行）" if n <= max_rows else f"（共 {n} 行，列出前 {len(shown)} 行）"
    return [f"【表格】{title}{tip}", head] + [fmt(r) for r in shown]


def _ladder_text(b: dict, max_rows: int) -> list[str]:
    levels = b.get("levels") or []
    title = _clean(b.get("title")) or "梯队"
    if not levels:
        return []
    lines = [f"【梯队】{title}"]
    limit = max(4, min(_LADDER_ITEMS, max_rows or 4))
    for lv in levels[: max(3, max_rows or 3)]:
        items = lv.get("items") or []
        names = []
        for it in items[:limit]:
            t = _clean(it.get("text") if isinstance(it, dict) else it)
            sub = _clean(it.get("sub")) if isinstance(it, dict) else ""
            names.append(f"{t}（{sub}）" if sub else t)
        more = f" 等{len(items)}只" if len(items) > limit else ""
        lines.append(f"{_clean(lv.get('label'))}：{'、'.join(names)}{more}")
    if len(levels) > max(3, max_rows or 3):
        lines.append(f"……（其余 {len(levels) - max(3, max_rows or 3)} 档省略）")
    return lines


def _section_text(sec: dict, include_holdings: bool, max_rows: int, with_notes: bool) -> list[str]:
    private = _is_holding_section(sec) and not include_holdings
    lines = [f"## {_clean(sec.get('title')) or sec.get('id') or '模块'}"]

    summary = [s for s in sec.get("summary") or [] if not (private and _has_holding_word(s))]
    if summary:
        lines.append("要点：")
        lines += [f"- {_clean(s)}" for s in summary]

    kpis = [k for k in sec.get("kpis") or [] if isinstance(k, dict)
            and not (private and (_has_holding_word(k.get("label")) or _has_holding_word(k.get("sub"))))]
    if kpis:
        lines.append("指标：")
        for k in kpis:
            sub = _clean(k.get("sub"))
            lines.append(f"- {_clean(k.get('label'))}：{_clean(k.get('value'))}" + (f"（{sub}）" if sub else ""))

    for b in sec.get("blocks") or []:
        if not isinstance(b, dict):
            continue
        kind = b.get("type")
        if kind == "table":
            if private and _has_holding_word(b.get("title")):
                continue
            drop = set()
            if private:
                drop = {i for i, c in enumerate(b.get("columns") or [])
                        if _has_holding_word(c.get("label")) or any(w in str(c.get("label") or "") for w in PRIVATE_COLUMNS)}
            lines += _table_text(b, max_rows, drop)
        elif kind == "ladder":
            if private and _has_holding_word(b.get("title")):
                continue
            lines += _ladder_text(b, max_rows)
        # 图表（chart）不发送

    errors = [e for e in sec.get("errors") or [] if not (private and _has_holding_word(e))]
    if errors:
        lines.append("数据缺失：" + "；".join(_short(_clean(e), _NOTE_LIMIT) for e in errors))
    if with_notes:
        notes = [n for n in sec.get("notes") or [] if not (private and _has_holding_word(n))]
        if notes:
            lines.append("口径说明：")
            lines += [f"- {_short(_clean(n), _NOTE_LIMIT)}" for n in notes]
    return lines


def _render(report: dict, include_holdings: bool, max_rows: int, with_notes: bool) -> str:
    head = f"复盘日：{_clean(report.get('trade_date'))} {_clean(report.get('weekday'))}".rstrip()
    lines = [head]
    if report.get("calendar_approximate"):
        lines.append("提示：交易日历为按工作日估算，可能不准确。")
    for b in report.get("banners") or []:
        lines.append(f"提示：{_clean(b)}")
    for sec in report.get("sections") or []:
        if isinstance(sec, dict):
            lines.append("")
            lines += _section_text(sec, include_holdings, max_rows, with_notes)
    return "\n".join(lines).strip()


def report_context(report: dict | None, include_holdings: bool = False, max_chars: int = 15000) -> str:
    """把报告数据压成给 AI 看的紧凑中文文本（不含图表；默认剔除持仓数量/成本/市值/盈亏）。"""
    if not isinstance(report, dict) or not report:
        return ""
    max_chars = max(500, int(max_chars or 15000))
    text = ""
    for max_rows, with_notes in _LEVELS:
        text = _render(report, include_holdings, max_rows, with_notes)
        if len(text) <= max_chars:
            return text
    tail = "\n……（内容过长，已截断）"
    return text[: max_chars - len(tail)] + tail


# ---------------------------------------------------------------- history.csv

# 列名 -> (中文名, 格式)；格式：int 整数 / num1 一位小数 / num2 两位小数 / yi0 亿元取整 / yi2 亿元两位 / text
HISTORY_COLUMNS = {
    "turnover": ("两市成交额(亿)", "yi0"),
    "up_count": ("上涨家数", "int"),
    "down_count": ("下跌家数", "int"),
    "flat_count": ("平盘家数", "int"),
    "zt_count": ("涨停", "int"),
    "dt_count": ("跌停", "int"),
    "zb_count": ("炸板", "int"),
    "zb_rate": ("炸板率%", "num1"),
    "lianban_count": ("连板股", "int"),
    "max_board": ("最高板", "int"),
    "max_board_names": ("最高板个股", "text"),
    "promote_rate": ("连板晋级率%", "num1"),
    "prev_zt_avg_pct": ("昨涨停今均涨%", "num2"),
    "prev_zt_up_ratio": ("昨涨停收涨比%", "num1"),
    "dt_lianxu": ("连续跌停", "int"),
    "funds_main_net": ("主力净流入(亿)", "yi2"),
    "funds_lhb_count": ("龙虎榜只数", "int"),
    "funds_lhb_net": ("龙虎榜净买(亿)", "yi2"),
    "funds_org_net": ("机构净买(亿)", "yi2"),
}


def _missing(v) -> bool:
    if v is None:
        return True
    try:
        import pandas as pd
        return bool(pd.isna(v))
    except (TypeError, ValueError, ImportError):
        return False


def _fmt_hist(v, fmt: str) -> str:
    if _missing(v):
        return ""
    if fmt == "text":
        return _clean(v)
    try:
        f = float(v)
    except (TypeError, ValueError):
        return _clean(v)
    if not math.isfinite(f):
        return ""
    if fmt == "int":
        return f"{f:.0f}"
    if fmt == "num1":
        return f"{f:.1f}"
    if fmt == "num2":
        return f"{f:.2f}"
    if fmt == "yi0":
        return f"{f / 1e8:.0f}"
    if fmt == "yi2":
        return f"{f / 1e8:+.2f}"
    if abs(f) >= 1e8:
        return f"{f / 1e8:.2f}亿"
    return f"{f:.2f}".rstrip("0").rstrip(".")


def history_context(n: int = 20, until: str | None = None, *, include_holdings: bool = False) -> str:
    """data/history.csv 最近 n 行（截至 until 含当日）的紧凑表格文本；没有文件或没有数据返回 ''。"""
    path = paths.HISTORY_PATH
    if not path.exists():
        return ""
    try:
        from ..history import History
        df = History(path).load()
    except Exception:
        return ""
    if df is None or df.empty or "date" not in df.columns:
        return ""
    df = df[df["date"].astype(str).str.len() > 0]
    if until:
        df = df[df["date"].astype(str) <= str(until)[:10]]
    df = df.tail(max(1, int(n or 20)))
    if df.empty:
        return ""

    cols = [c for c in HISTORY_COLUMNS if c in df.columns]
    for c in df.columns:   # 新增的未知列也带上（持仓列默认不发送）
        if c == "date" or c in cols:
            continue
        if _has_holding_word(c) and not include_holdings:
            continue
        cols.append(c)
    records = df.to_dict("records")
    keep = []
    for c in cols:
        fmt = HISTORY_COLUMNS.get(c, (c, "auto"))[1]
        if any(_fmt_hist(r.get(c), fmt) for r in records):   # 整列为空的不输出
            keep.append(c)
    if not keep:
        return ""
    header = "日期|" + "|".join(HISTORY_COLUMNS.get(c, (_clean(c), ""))[0] for c in keep)
    lines = [f"近 {len(records)} 个交易日关键指标（本地逐日记录，空白表示当日未记录；成交额、资金单位为亿元）：", header]
    for r in records:
        cells = [_clean(r.get("date"))]
        for c in keep:
            cells.append(_fmt_hist(r.get(c), HISTORY_COLUMNS.get(c, (c, "auto"))[1]))
        lines.append("|".join(cells))
    return "\n".join(lines)
