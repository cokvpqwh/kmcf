"""AI 回复的 Markdown -> QTextBrowser 能显示的 HTML 子集。纯函数，不依赖 Qt，便于测试。

支持大模型常用的写法：标题、粗体/斜体/删除线、行内代码、代码块、无序/有序列表（含嵌套）、
表格、引用、分隔线、链接（只认 http/https）。流式输出时内容可能只写了一半（未闭合的 ** 或 ```），
这时按原样显示，不会报错。所有文本都先做 HTML 转义，模型输出里的 HTML 标签不会被执行。
"""

import html
import re

FONT_MONO = "Consolas, 'Microsoft YaHei UI', monospace"

_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_HR = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_UL = re.compile(r"^(\s*)[-*+•]\s+(.*)$")
_OL = re.compile(r"^(\s*)(\d{1,3})(?:[.)]\s+|、\s*)(.*)$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")

_HEAD_SIZE = {1: "12.5pt", 2: "11.5pt", 3: "10.5pt"}


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def inline(text: str, t: dict) -> str:
    """行内格式：先整体转义，再把 `code`、**粗体**、*斜体*、~~删除线~~、[文字](网址) 换成标签。"""
    s = _esc(text)
    codes: list[str] = []

    def keep_code(m: re.Match) -> str:
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    s = re.sub(r"`([^`\n]+)`", keep_code, s)
    s = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)",
               lambda m: f'<a href="{m.group(2)}" style="color:{t["accent"]};">{m.group(1)}</a>', s)
    s = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w*])__(?=\S)(.+?)(?<=\S)__(?![\w*])", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])", r"<i>\1</i>", s)
    s = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<s>\1</s>", s)

    def put_code(m: re.Match) -> str:
        return (f'<span style="font-family:{FONT_MONO}; background-color:{t["card2"]}; '
                f'color:{t["accent_text"]};">&nbsp;{codes[int(m.group(1))]}&nbsp;</span>')

    return re.sub(r"\x00(\d+)\x00", put_code, s)


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s)]


def _table_html(header: list[str], aligns: list[str], rows: list[list[str]], t: dict) -> str:
    n = len(header)
    b = t["border_strong"]

    def cell(tag: str, text: str, i: int, head: bool) -> str:
        align = aligns[i] if i < len(aligns) else ""
        attr = f' align="{align}"' if align else ""
        bg = f' bgcolor="{t["card2"]}"' if head else ""
        weight = " font-weight:600;" if head else ""
        return (f'<{tag}{attr}{bg} style="border: 1px solid {b}; padding: 3px 6px;{weight}">'
                f"{inline(text, t)}</{tag}>")

    out = [f'<table cellspacing="0" cellpadding="4" border="1" '
           f'style="border-collapse: collapse; border-color: {b}; margin-top: 4px; margin-bottom: 6px;">']
    out.append("<tr>" + "".join(cell("th", h, i, True) for i, h in enumerate(header)) + "</tr>")
    for r in rows:
        r = (r + [""] * n)[:n]
        out.append("<tr>" + "".join(cell("td", c, i, False) for i, c in enumerate(r)) + "</tr>")
    out.append("</table>")
    return "".join(out)


def _aligns(sep: str) -> list[str]:
    out = []
    for c in _split_row(sep):
        c = c.strip()
        if c.startswith(":") and c.endswith(":"):
            out.append("center")
        elif c.endswith(":"):
            out.append("right")
        else:
            out.append("")
    return out


def md_to_html(text: str, t: dict) -> str:
    """把 Markdown 文本转成 HTML 片段（不含 <html>/<body>）。t 是 theme 的颜色表。"""
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    stack: list[list] = []  # 打开的列表：[标签 ul/ol, 缩进, 是否有未闭合的 <li>]
    p_style = "margin-top:3px; margin-bottom:5px;"

    def flush_para() -> None:
        if para:
            out.append(f'<p style="{p_style}">' + "<br>".join(inline(x, t) for x in para) + "</p>")
            para.clear()

    def close_top() -> None:
        tag, _indent, li_open = stack.pop()
        if li_open:
            out.append("</li>")
        out.append(f"</{tag}>")

    def close_lists() -> None:
        while stack:
            close_top()

    def list_item(tag: str, indent: int, content: str, start: int | None = None) -> None:
        flush_para()
        while stack and indent < stack[-1][1] - 1:
            close_top()
        if stack and abs(indent - stack[-1][1]) <= 1 and stack[-1][0] != tag:
            close_top()
        if not stack or indent > stack[-1][1] + 1:
            start_attr = f' start="{start}"' if tag == "ol" and start and start != 1 else ""
            out.append(f'<{tag}{start_attr} style="margin-top:2px; margin-bottom:4px; -qt-list-indent:1;">')
            stack.append([tag, indent, False])
        top = stack[-1]
        if top[2]:
            out.append("</li>")
        out.append(f'<li style="margin-top:1px; margin-bottom:2px;">{inline(content, t)}')
        top[2] = True

    def next_nonblank(j: int) -> str | None:
        while j < len(lines):
            if lines[j].strip():
                return lines[j]
            j += 1
        return None

    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        m = _FENCE.match(line)
        if m:  # 代码块（未闭合时一直到结尾）
            flush_para()
            close_lists()
            fence = m.group(1)
            i += 1
            code = []
            while i < n and not lines[i].strip().startswith(fence):
                code.append(lines[i])
                i += 1
            i += 1
            body = _esc("\n".join(code)) or "&nbsp;"
            out.append(f'<table width="100%" cellspacing="0" cellpadding="6" bgcolor="{t["card2"]}" '
                       f'style="margin-top:4px; margin-bottom:6px;"><tr><td>'
                       f'<pre style="font-family:{FONT_MONO}; margin:0;">{body}</pre></td></tr></table>')
            continue

        if not stripped:
            flush_para()
            if stack:  # 列表项之间的空行：下一个非空行还是列表项就不结束列表
                nxt = next_nonblank(i + 1)
                if nxt is None or not (_UL.match(nxt) or _OL.match(nxt) or (nxt[:1].isspace() and stack)):
                    close_lists()
            i += 1
            continue

        m = _HEADING.match(line)
        if m:
            flush_para()
            close_lists()
            level = len(m.group(1))
            size = _HEAD_SIZE.get(level, "10pt")
            out.append(f'<p style="margin-top:10px; margin-bottom:4px; font-size:{size}; font-weight:600;">'
                       f"{inline(m.group(2), t)}</p>")
            i += 1
            continue

        if _HR.match(line) and not para:
            close_lists()
            out.append("<hr>")
            i += 1
            continue

        if "|" in line and i + 1 < n and "|" in lines[i + 1] and _TABLE_SEP.match(lines[i + 1]):
            flush_para()
            close_lists()
            header = _split_row(line)
            aligns = _aligns(lines[i + 1])
            i += 2
            rows = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_split_row(lines[i]))
                i += 1
            out.append(_table_html(header, aligns, rows, t))
            continue

        m = _QUOTE.match(line)
        if m:
            flush_para()
            close_lists()
            quote = []
            while i < n:
                q = _QUOTE.match(lines[i])
                if not q:
                    break
                quote.append(q.group(1))
                i += 1
            body = "<br>".join(inline(x, t) for x in quote)
            out.append(f'<table width="100%" cellspacing="0" cellpadding="0" style="margin-top:4px; margin-bottom:6px;">'
                       f'<tr><td width="3" bgcolor="{t["border_strong"]}"></td>'
                       f'<td style="padding-left:8px; color:{t["muted"]};">{body}</td></tr></table>')
            continue

        m = _UL.match(line)
        if m and not _HR.match(line):
            list_item("ul", len(m.group(1).expandtabs(4)), m.group(2))
            i += 1
            continue
        m = _OL.match(line)
        if m:
            list_item("ol", len(m.group(1).expandtabs(4)), m.group(3), int(m.group(2)))
            i += 1
            continue

        if stack and line[:1].isspace():  # 列表项的续行
            out.append("<br>" + inline(stripped, t))
            i += 1
            continue

        close_lists()
        para.append(stripped)
        i += 1

    flush_para()
    close_lists()
    return "".join(out)


def plain_text(text: str) -> str:
    """复制用：去掉最常见的 Markdown 标记（粗体、标题井号），其余原样保留。"""
    s = str(text or "")
    s = re.sub(r"^\s{0,3}#{1,6}\s+", "", s, flags=re.M)
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    return s
