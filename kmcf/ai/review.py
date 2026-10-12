"""报告生成时的“AI 复盘点评”。

- 未启用 / 未开自动点评 / 找不到 API Key：返回 None（报告里不显示卡片）；
- 成功：{'blocks': [{'type': 'p', 'text': ...} | {'type': 'ul', 'items': [...]}], 'provider', 'model', 'cached', 'text'}；
- 失败：{'error': 中文说明}（报告里显示一行灰色说明，报告照常生成）。
结果按 (日期, 上下文哈希, 模型) 缓存到 data/cache/ai/，同样的数据重复生成不再调用 API。
输出做合规检查：命中禁用说法时带提醒重写一次，仍命中则删掉相关句子；命中缓存时也会再检查一遍。

整步有总时长上限 ai.review_budget（默认 90 秒，含重试和重写）：到点就停止并跳过点评，
不会把报告生成（以及 19:00 的定时任务）拖住；等待期间约每秒报告一次进度，界面上的“取消”能及时生效。
"""

import datetime as dt
import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

from .. import paths
from .client import AIError, chat
from .compliance import compliance_hits, scrub_text
from .prompts import PROMPT_VERSION, REVIEW_PROMPT, REWRITE_PROMPT, build_messages
from .settings import ai_settings, find_keys

log = logging.getLogger("kmcf")

REVIEW_MAX_TOKENS = 1200
REVIEW_RETRIES = 1        # 点评请求遇到 429 / 5xx / 超时最多重试 1 次（总时长另有上限）
REWRITE_MIN_LEFT = 25.0   # 剩余时间不足这么多秒时不再请求重写，直接删掉不合规的句子
CACHE_KEEP_DAYS = 120
PROGRESS_INTERVAL = 0.8   # 流式生成时最多每 0.8 秒报告一次进度
_SENTENCE_END = "。！？!?；;"


def cache_dir() -> Path:
    return paths.CACHE_DIR / "ai"


# ---------------------------------------------------------------- Markdown -> blocks

_LIST_RE = re.compile(r"^\s*(?:[-*+]\s+|[•·]\s*|\d{1,2}(?:\.(?!\d)|[、)）])\s*|[（(]\d{1,2}[)）]\s*)")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def strip_inline(text: str) -> str:
    """去掉行内 Markdown 标记：**粗体**、__粗体__、*斜体*、`代码`、[文字](链接)、~~删除线~~。"""
    s = str(text or "")
    s = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"(\*\*|__)(.+?)\1", r"\2", s)
    s = re.sub(r"~~(.+?)~~", r"\1", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"\1", s)
    s = s.replace("**", "").replace("__", "")
    return " ".join(s.split()).strip()


def markdown_blocks(md: str) -> list[dict]:
    """把 Markdown 文本解析成段落 / 无序列表块（有序列表也按无序列表处理，标题当作段落）。"""
    blocks: list[dict] = []
    para: list[str] = []
    items: list[str] | None = None
    gap = False   # 上一行是空行（列表项之间隔空行仍算同一个列表）

    def flush_para():
        nonlocal para
        text = strip_inline("".join(x.strip() for x in para))
        if text:
            blocks.append({"type": "p", "text": text})
        para = []

    def flush_list():
        nonlocal items
        if items:
            cleaned = [strip_inline(x) for x in items]
            cleaned = [x for x in cleaned if x]
            if cleaned:
                blocks.append({"type": "ul", "items": cleaned})
        items = None

    for raw in str(md or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_para()
            gap = True
            continue
        if re.fullmatch(r"[-*_=]{3,}", stripped) or _TABLE_SEP_RE.fullmatch(stripped):
            continue   # 分隔线 / 表格分隔行
        stripped = re.sub(r"^>+\s*", "", stripped)           # 引用
        stripped = re.sub(r"^#{1,6}\s*", "", stripped)       # 标题当作普通段落
        if stripped.startswith("|") and stripped.endswith("|"):   # 表格行 -> 列表项
            cells = [c.strip() for c in stripped.strip("|").split("|") if c.strip()]
            stripped = "- " + "，".join(cells)
        m = _LIST_RE.match(stripped)
        if m and len(stripped) > m.end():
            flush_para()
            if items is None:
                items = []
            items.append(stripped[m.end():])
            gap = False
            continue
        if items is not None and not gap and raw[:1] in (" ", "\t"):   # 紧跟的缩进续行并入上一个列表项
            items[-1] += stripped
            continue
        flush_list()
        para.append(stripped)
        gap = False
    flush_para()
    flush_list()
    return blocks


def trim_incomplete(text: str) -> str:
    """回答因长度上限被截断时，去掉最后半句（保留到最后一个句末标点或换行）。"""
    t = str(text or "").rstrip()
    if not t or t[-1] in _SENTENCE_END:
        return t
    cut = max(t.rfind(ch) for ch in _SENTENCE_END + "\n")
    return t[:cut + 1].rstrip() if cut > 0 else t


def blocks_text(blocks: list[dict]) -> str:
    out = []
    for b in blocks:
        if b.get("type") == "ul":
            out.append("\n".join(f"- {x}" for x in b.get("items") or []))
        else:
            out.append(b.get("text") or "")
    return "\n\n".join(x for x in out if x)


def scrub_blocks(blocks: list[dict]) -> tuple[list[dict], int]:
    """删掉含禁用说法的句子；删空的段落/列表项一并去掉。返回 (新 blocks, 删除句数)。"""
    out, removed = [], 0
    for b in blocks:
        if b.get("type") == "ul":
            kept = []
            for it in b.get("items") or []:
                text, n = scrub_text(it)
                removed += n
                if text:
                    kept.append(text)
            if kept:
                out.append({"type": "ul", "items": kept})
        else:
            text, n = scrub_text(b.get("text") or "")
            removed += n
            if text:
                out.append({"type": "p", "text": text})
    return out, removed


# ---------------------------------------------------------------- 缓存

def _cache_path(date: str, model: str, digest: str) -> Path:
    safe_model = re.sub(r"[^0-9A-Za-z._-]+", "-", model)[:40] or "model"
    safe_date = re.sub(r"[^0-9-]", "", date)[:10] or "nodate"
    return cache_dir() / f"{safe_date}_{safe_model}_{digest}.json"


def _load_cache(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    blocks = data.get("blocks") if isinstance(data, dict) else None
    if not isinstance(blocks, list) or not blocks:
        return None
    for b in blocks:
        if not isinstance(b, dict) or b.get("type") not in ("p", "ul"):
            return None
    return data


def _save_cache(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log.warning("AI 点评缓存写入失败：%s", e)


def _prune_cache(now: float | None = None) -> None:
    now = now or time.time()
    try:
        for p in cache_dir().glob("*.json"):
            if now - p.stat().st_mtime > CACHE_KEEP_DAYS * 86400:
                p.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------- 主流程

def review_for_report(data: dict, cfg: dict | None, *, progress: Callable[[str], None] | None = None,
                      cancel: threading.Event | None = None) -> dict | None:
    """生成“AI 复盘点评”。见模块说明。

    progress(文字)：生成过程中的进度说明（流式输出时节流调用）；回调里抛出的 BaseException（如取消）原样向上传递。
    cancel：被 set 时停止请求并返回 None。
    """
    s = ai_settings(cfg)
    if not s["enabled"] or not s["auto_review"]:
        return None
    if not isinstance(data, dict) or not data.get("sections"):
        return None
    if not find_keys(s["api_key_env"]):
        return None

    def say(msg: str) -> None:
        if progress:
            progress(msg)

    provider, model = s["provider"], s["model"]
    include = s["include_holdings"]
    trade_date = str(data.get("trade_date") or "")[:10]
    try:
        messages = build_messages(REVIEW_PROMPT, data, cfg, include_holdings=include)
    except Exception as e:
        log.warning("AI 点评上下文整理失败：%s", e, exc_info=True)
        return {"error": f"整理报告数据时出错（{type(e).__name__}）"}
    digest_src = json.dumps([PROMPT_VERSION, s["base_url"], model, s["temperature"], messages],
                            ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(digest_src.encode("utf-8")).hexdigest()[:16]
    path = _cache_path(trade_date, model, digest)

    cached = _load_cache(path)
    if cached:
        # 缓存也再过一遍合规检查（禁用词表可能更新过，缓存文件也可能被改过）；删光了就当作没有缓存
        blocks, removed = scrub_blocks(cached["blocks"])
        if blocks:
            say("AI 复盘点评（使用缓存）")
            log.info("AI 复盘点评：命中缓存 %s%s", path.name, f"（按现行规则删除了 {removed} 句）" if removed else "")
            text = blocks_text(blocks) if removed else (cached.get("text") or blocks_text(blocks))
            return {"blocks": blocks, "provider": cached.get("provider") or provider,
                    "model": cached.get("model") or model, "cached": True, "text": text}
        log.info("AI 复盘点评：缓存 %s 未通过现行合规检查，重新生成", path.name)

    budget = float(s["review_budget"])
    started = time.monotonic()
    deadline = started + budget
    status = {"msg": "AI 复盘点评", "n": 0, "t": 0.0}

    def set_status(msg: str) -> None:
        status["msg"] = msg
        say(msg)

    def heartbeat() -> None:   # 等待期间约每秒一次：刷新进度，GenerateTask 借此及时响应“取消生成”
        say(status["msg"])

    def on_delta(piece: str) -> None:
        status["n"] += len(piece)
        now = time.monotonic()
        if now - status["t"] >= PROGRESS_INTERVAL:
            status["t"] = now
            set_status(f"AI 复盘点评：{provider} 生成中（已 {status['n']} 字）")

    def ask(msgs: list[dict]):
        return chat(msgs, cfg, model=model, stream=True, on_delta=on_delta, cancel=cancel,
                    max_tokens=REVIEW_MAX_TOKENS, deadline=deadline, retries=REVIEW_RETRIES, heartbeat=heartbeat)

    set_status("AI 复盘点评")
    try:
        text = ask(messages)
        if cancel is not None and cancel.is_set():
            return None
        finish = getattr(text, "finish_reason", None)
        hits = compliance_hits(text)
        if hits and deadline - time.monotonic() < REWRITE_MIN_LEFT:
            log.info("AI 复盘点评含不合规说法（%s），剩余时间不足，直接删除相关句子", "、".join(hits))
        elif hits:
            log.info("AI 复盘点评含不合规说法（%s），要求重写", "、".join(hits))
            set_status("AI 复盘点评：调整措辞")
            retry = messages + [{"role": "assistant", "content": str(text)},
                                {"role": "user", "content": REWRITE_PROMPT.format(terms="、".join(hits[:8]))}]
            status["n"] = 0
            try:
                text2 = ask(retry)
                if cancel is not None and cancel.is_set():
                    return None
                if text2.strip():
                    text, finish = text2, getattr(text2, "finish_reason", None)
            except AIError as e:
                log.warning("AI 点评重写失败，改为删除不合规句子：%s", e)
    except AIError as e:
        if getattr(e, "kind", None) == "deadline":
            return {"error": f"AI 点评超时（{budget:.0f} 秒内未完成），已跳过；报告其余部分不受影响"}
        return {"error": str(e)}

    if finish == "length":   # 达到长度上限：去掉最后半句，不写缓存（下次生成重新请求）
        log.info("AI 复盘点评达到长度上限，已去掉不完整的结尾")
        text = trim_incomplete(text)
    blocks, removed = scrub_blocks(markdown_blocks(text))
    if removed:
        log.info("AI 复盘点评：删除了 %d 句不合规内容", removed)
    if not blocks:
        return {"error": "AI 返回的点评没有通过合规检查（含投资建议或预测），本次不显示"}
    clean = blocks_text(blocks)
    elapsed = round(time.monotonic() - started, 1)
    if finish in (None, "stop"):   # 只缓存正常结束的完整点评（None：未知，按正常处理）
        _save_cache(path, {"version": 1, "prompt_version": PROMPT_VERSION, "trade_date": trade_date,
                           "provider": provider, "model": model, "digest": digest,
                           "created_at": dt.datetime.now().isoformat(timespec="seconds"),
                           "elapsed": elapsed, "removed": removed, "text": clean, "blocks": blocks})
        _prune_cache()
    else:
        log.info("AI 复盘点评结束原因为 %s，不写缓存", finish)
    log.info("AI 复盘点评完成：%d 字，用时 %.1f 秒", len(clean), elapsed)
    return {"blocks": blocks, "provider": provider, "model": model, "cached": False, "text": clean}
