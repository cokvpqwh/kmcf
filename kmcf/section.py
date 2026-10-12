"""复盘报告的“板块（Section）”构建器。

每个复盘模块（kmcf/sections/*.py）提供：
    ID: str          英文 id，如 "market"
    TITLE: str       中文标题，如 "大盘与情绪"
    build(ctx) -> Section

Section 只装“已经格式化好的展示数据”，模板负责排版，模块之间互不依赖。
颜色遵循 A 股习惯：红涨绿跌。
"""

import logging
import math
import traceback
from contextlib import contextmanager

log = logging.getLogger("kmcf")

# 图表用色（直接写进 ECharts option；亮/暗主题下都可读）
UP = "#e5484d"      # 涨：红
DOWN = "#12a150"    # 跌：绿
FLAT = "#8b8d98"    # 平
ACCENT = "#3e63dd"  # 中性强调色（成交额、指数等）
SERIES = ["#3e63dd", "#e5484d", "#f59e0b", "#12a150", "#8e4ec6", "#0ea5e9", "#d6409f", "#64748b"]

TONES = ("up", "down", "flat", "neutral", "warn")


# ---------------------------------------------------------------- 数值工具

def is_missing(v) -> bool:
    if v is None:
        return True
    try:
        return isinstance(v, float) and math.isnan(v) or (hasattr(v, "__float__") and math.isnan(float(v)))
    except (TypeError, ValueError):
        return False


def num(v, default=None):
    """转成 float；无法转换或 NaN 时返回 default。支持 '1.2万'/'3.4亿'/'5%' 这类字符串。"""
    if v is None:
        return default
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if s in ("", "-", "--", "—"):
            return default
        mult = 1.0
        if s.endswith("%"):
            s = s[:-1]
        if s.endswith("万亿"):
            mult, s = 1e12, s[:-2]
        elif s.endswith("亿"):
            mult, s = 1e8, s[:-1]
        elif s.endswith("万"):
            mult, s = 1e4, s[:-1]
        try:
            return float(s) * mult
        except ValueError:
            return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(f) or math.isinf(f) else f


def tone_of(v, eps: float = 1e-9) -> str:
    f = num(v)
    if f is None:
        return "neutral"
    if f > eps:
        return "up"
    if f < -eps:
        return "down"
    return "flat"


def fmt_amount(v, signed: bool = False) -> str:
    """金额（元）自动换算：万亿 / 亿 / 万。"""
    f = num(v)
    if f is None:
        return "—"
    sign = "+" if signed and f > 0 else ""
    a = abs(f)
    if a >= 1e12:
        s = f"{f / 1e12:.2f}万亿"
    elif a >= 1e8:
        s = f"{f / 1e8:.2f}亿"
    elif a >= 1e4:
        s = f"{f / 1e4:.2f}万"
    else:
        s = f"{f:.0f}"
    return sign + s


def fmt_pct(v, signed: bool = True, digits: int = 2) -> str:
    """百分数，输入已是百分比单位（2.35 -> +2.35%）。"""
    f = num(v)
    if f is None:
        return "—"
    return f"{f:+.{digits}f}%" if signed else f"{f:.{digits}f}%"


def fmt_num(v, digits: int = 2, signed: bool = False) -> str:
    f = num(v)
    if f is None:
        return "—"
    return f"{f:+,.{digits}f}" if signed else f"{f:,.{digits}f}"


def fmt_int(v) -> str:
    f = num(v)
    return "—" if f is None else f"{f:,.0f}"


# 列格式：fmt -> (格式化函数, 是否默认按正负着色)
_FORMATS = {
    "text": (lambda v: "—" if is_missing(v) or v == "" else str(v), False),
    "code": (lambda v: "—" if is_missing(v) else str(v).zfill(6) if str(v).isdigit() else str(v), False),
    "int": (fmt_int, False),
    "num": (lambda v: fmt_num(v, 2), False),
    "num1": (lambda v: fmt_num(v, 1), False),
    "num_signed": (lambda v: "—" if num(v) is None else f"{num(v):+.2f}", True),  # 带符号数值（如净流入亿元），着色
    "price": (lambda v: fmt_num(v, 2), False),
    "pct": (lambda v: fmt_pct(v, True), True),       # 涨跌幅类，带符号着色
    "pct_plain": (lambda v: fmt_pct(v, False), False),  # 换手率、占比类，不着色
    "amount": (lambda v: fmt_amount(v), False),      # 成交额、市值
    "flow": (lambda v: fmt_amount(v, signed=True), True),  # 净流入，带符号着色
}


def col(key: str, label: str, fmt: str = "text", sign: bool | None = None, align: str | None = None) -> dict:
    """定义表格列。sign=None 时按 fmt 的默认规则决定是否红绿着色。"""
    if fmt not in _FORMATS:
        raise ValueError(f"未知列格式 {fmt}")
    if sign is None:
        sign = _FORMATS[fmt][1]
    if align is None:
        align = "left" if fmt in ("text", "code") else "right"
    return {"key": key, "label": label, "fmt": fmt, "sign": sign, "align": align}


# ---------------------------------------------------------------- Section

class Section:
    def __init__(self, id: str, title: str):
        self.id = id
        self.title = title
        self.summary: list[str] = []   # 复盘要点（会汇总到报告顶部）
        self.kpis: list[dict] = []
        self.blocks: list[dict] = []
        self.notes: list[str] = []     # 数据口径说明
        self.errors: list[str] = []    # 某部分数据获取失败等

    # -- 内容
    def add_summary(self, text: str) -> None:
        if text:
            self.summary.append(text)

    def kpi(self, label: str, value: str, sub: str | None = None, tone: str = "neutral") -> None:
        if tone not in TONES:
            tone = "neutral"
        self.kpis.append({"label": label, "value": value, "sub": sub or "", "tone": tone})

    def chart(self, title: str, option: dict, height: int = 320, span: str = "full",
              note: str | None = None) -> None:
        """ECharts 图表。option 必须是可 JSON 序列化的 dict（不能含 JS 函数）。"""
        self.blocks.append({"type": "chart", "title": title, "option": option,
                            "height": int(height), "span": _span(span), "note": note or ""})

    def table(self, title: str, columns: list[dict], rows: list[dict], span: str = "full",
              note: str | None = None, max_rows: int | None = None, row_tone_key: str | None = None) -> None:
        """表格。rows 是原始值字典列表，按 columns 的 fmt 统一格式化。

        row_tone_key：指定某个字段的正负决定整行名称列颜色（可选）。
        """
        if max_rows is not None:
            rows = rows[:max_rows]
        out_rows = []
        for r in rows:
            cells = []
            for c in columns:
                v = r.get(c["key"])
                fmt_fn = _FORMATS[c["fmt"]][0]
                text = fmt_fn(v)
                f = num(v)
                cells.append({
                    "text": text,
                    "tone": tone_of(v) if c["sign"] else "neutral",
                    "v": f if f is not None and c["fmt"] not in ("text", "code") else str(text),
                })
            out_rows.append(cells)
        self.blocks.append({"type": "table", "title": title, "columns": columns, "rows": out_rows,
                            "span": _span(span), "note": note or "", "empty": not out_rows})

    def ladder(self, title: str, levels: list[dict], span: str = "full", note: str | None = None) -> None:
        """梯队/分组展示。levels: [{"label": "5板", "items": [{"text": "名称", "sub": "代码/备注", "tone": "up"}]}]"""
        self.blocks.append({"type": "ladder", "title": title, "levels": levels,
                            "span": _span(span), "note": note or ""})

    def note(self, text: str) -> None:
        if text and text not in self.notes:
            self.notes.append(text)

    def error(self, text: str) -> None:
        if text and text not in self.errors:
            self.errors.append(text)

    @contextmanager
    def guard(self, label: str):
        """包住一段子功能：出错时记录错误、继续生成其他部分。"""
        try:
            yield
        except Exception as e:
            log.warning("[%s] %s 失败：%s", self.id, label, _short(e))
            log.debug("[%s] %s 失败详情：\n%s", self.id, label, traceback.format_exc())
            self.error(f"{label}：{_short(e)}")

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "summary": self.summary, "kpis": self.kpis,
                "blocks": self.blocks, "notes": self.notes, "errors": self.errors}


def _span(span: str) -> str:
    return span if span in ("full", "half") else "full"


def _short(e: Exception, limit: int = 160) -> str:
    s = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
    return s if len(s) <= limit else s[:limit] + "…"
