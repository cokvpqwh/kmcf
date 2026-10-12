"""OpenAI 兼容的对话接口客户端（默认 DeepSeek）：POST {base_url}/chat/completions。

- 请求在一个后台线程里进行，调用方线程每 POLL 秒检查一次取消和截止时间：
  等服务器第一个字节、收 ': keep-alive' 保活行时也能立刻停下（不再被 requests 的阻塞读卡住）；
  回调（on_delta / on_reasoning / heartbeat）都在调用方线程里执行，回调抛出的异常原样向上传递；
- timeout：发出请求后最多等这么久还没收到任何内容（保活行不算）就算超时；
  deadline：整次调用（含重试、重试前的等待）的截止时间（time.monotonic() 的值），到点立即停止；
- 遇到 429 / 5xx / 超时 / 连接失败最多重试 retries 次（默认 2）；流式只在还没收到任何内容时才重试；
- 流式解析 SSE（data: {...}，以 data: [DONE] 结束；忽略空行、注释行和 event:/id: 字段；\\r\\n、\\n、\\r 换行都支持），
  正文片段回调 on_delta，deepseek-reasoner 的思考过程回调 on_reasoning；
  流既没有 [DONE] 也没有 finish_reason 就断开时按“回答不完整”报错（AIError.partial 带已收到的内容）；
- 返回值 AIText 就是正文字符串，另带 finish_reason（'stop' / 'length' 达到长度上限 / 'cancelled' 已取消）；
- 所有错误都转换成 AIError，str(e) 是给用户看的中文说明，绝不包含 API Key。
"""

import json
import logging
import math
import queue
import re
import socket
import threading
import time
from typing import Callable

import requests

from .. import __version__, net
from .settings import ai_settings, endpoint_problem, find_keys

log = logging.getLogger("kmcf")

MAX_RETRIES = 2              # 可重试错误默认最多再试 2 次
CONNECT_TIMEOUT = 15.0       # 建立连接的秒数上限
MAX_BODY = 4 * 1024 * 1024   # 非流式响应体上限
RETRY_STATUS = (429, 500, 502, 503, 504)
MAX_RETRY_DELAY = 20.0       # 重试前最多等待的秒数（含 Retry-After）
POLL = 0.1                   # 调用方线程检查取消 / 截止时间的间隔（秒）
HEARTBEAT_INTERVAL = 1.0     # heartbeat 回调的间隔（秒）
ABORT_JOIN = 0.5             # 中止后最多等请求线程收尾的秒数（之后不再等，线程会自行结束）


class AIError(Exception):
    """AI 调用失败。str(e) 是给用户看的中文说明（不含 API Key）。

    status：HTTP 状态码（网络错误时为 None）；partial：出错前已收到的正文（流式中途断开时）；
    kind：None 或 'timeout'（等不到响应）/ 'deadline'（超过总时长上限）/ 'incomplete'（连接中断，回答不完整）。
    """

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False, partial: str = "",
                 retry_after: float | None = None, kind: str | None = None):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.partial = partial
        self.retry_after = retry_after
        self.kind = kind


class AIText(str):
    """chat() 的返回值：就是完整正文（str），另带 finish_reason。

    finish_reason：'stop' 正常结束；'length' 达到长度上限（max_tokens），内容被截断；
    'cancelled' 被取消（内容是已收到的部分）；其他值为服务器原样返回。
    """

    finish_reason: str | None

    def __new__(cls, text: str = "", finish_reason: str | None = None):
        obj = super().__new__(cls, text)
        obj.finish_reason = finish_reason
        return obj

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


# ---------------------------------------------------------------- 文字与脱敏

STATUS_TEXT = {
    400: "请求格式错误（400）",
    401: "API Key 无效（401）：请检查环境变量 {env} 里的 Key 是否正确、是否已在 DeepSeek 开放平台删除或停用",
    402: "账户余额不足（402）：请到 DeepSeek 开放平台（platform.deepseek.com）充值后再试",
    422: "请求参数错误（422）",
    429: "请求太频繁（429）：已触发 DeepSeek 速率限制，请稍后再试",
    500: "DeepSeek 服务器内部错误（500），请稍后再试",
    502: "DeepSeek 服务器网关错误（502），请稍后再试",
    503: "DeepSeek 服务器繁忙（503），请稍后再试",
    504: "DeepSeek 服务器响应超时（504），请稍后再试",
}

_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[ _-]?key\s*[:=：]\s*)\S+"),     # “Your api key: ****abcd” 之类的回显
    re.compile(r"(?i)(bearer\s+)\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"\*{3,}[A-Za-z0-9_\-]{2,}"),
)


def scrub(text, *secrets: str) -> str:
    """去掉文字里可能出现的 Key（含服务端回显的掩码片段）。"""
    s = str(text or "")
    for sec in secrets:
        if sec:
            s = s.replace(sec, "***")
    for pat in _SECRET_PATTERNS:
        s = pat.sub(lambda m: (m.group(1) if m.re.groups else "") + "***", s)
    return s


def _short(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


def _server_detail(body: bytes) -> str:
    """响应体里的 error.message（没有就取前一段文字）。"""
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return _short(body.decode("utf-8", "replace")) if body else ""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return _short(err.get("message") or err.get("type") or "")
        if isinstance(err, str):
            return _short(err)
        if isinstance(data.get("message"), str):
            return _short(data["message"])
    return ""


def http_error(status: int, body: bytes, env: str, key: str = "", retry_after: float | None = None) -> AIError:
    base = STATUS_TEXT.get(status, f"DeepSeek 接口返回错误（HTTP {status}）").format(env=env or "DEEPSEEK_API_KEY")
    detail = "" if status == 401 else scrub(_server_detail(body), key)   # 401 的回显含 Key 片段，不展示
    msg = base + (f"。服务器说明：{detail}" if detail else "")
    return AIError(msg, status=status, retryable=status in RETRY_STATUS, retry_after=retry_after)


def _net_error(e: Exception, timeout: float) -> AIError:
    text = str(e)
    if isinstance(e, requests.exceptions.SSLError):
        return AIError("与 DeepSeek 服务器建立安全连接（HTTPS）失败：请检查电脑时间是否正确、网络是否被拦截", retryable=False)
    if isinstance(e, requests.exceptions.Timeout) or "timed out" in text.lower():
        return AIError(f"请求超时（{timeout:.0f} 秒内没有响应）：DeepSeek 服务器可能繁忙，请稍后再试",
                       retryable=True, kind="timeout")
    if isinstance(e, requests.exceptions.ConnectionError):
        return AIError("无法连接 DeepSeek 服务器：请检查网络连接后再试", retryable=True)
    return AIError(f"请求 DeepSeek 失败（{type(e).__name__}）", retryable=False)


def _deadline_error(total: float | None, partial: str = "") -> AIError:
    span = f"（{total:.0f} 秒）" if total and total > 0 else ""
    return AIError(f"AI 回答超时：超过总时长上限{span}，已停止", kind="deadline", partial=partial)


def _empty_reason(finish) -> str:
    if finish == "content_filter":
        return "回答被 DeepSeek 内容安全策略拦截，请换个问法再试"
    if finish == "insufficient_system_resource":
        return "DeepSeek 推理资源不足，回答被中断，请稍后再试"
    if finish == "length":
        return "回答长度超过上限，没有生成正文（可减少提问内容后再试）"
    return "DeepSeek 返回了空回答，请稍后再试"


# ---------------------------------------------------------------- 工具

def endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


def _sleep(seconds: float, cancel: threading.Event | None) -> None:
    """可被 cancel 打断的等待（测试里会替换掉）。"""
    seconds = max(0.0, float(seconds))
    if cancel is not None:
        cancel.wait(seconds)
    else:
        time.sleep(seconds)


def _cancelled(cancel: threading.Event | None) -> bool:
    return cancel is not None and cancel.is_set()


def _check_messages(messages) -> list[dict]:
    if not isinstance(messages, list) or not messages:
        raise AIError("没有要发送的对话内容")
    out = []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ("system", "user", "assistant") \
                or not isinstance(m.get("content"), str):
            raise AIError("对话内容格式不对（每条需要 role 和 content）")
        out.append({"role": m["role"], "content": m["content"]})
    return out


def _retry_after(resp) -> float | None:
    """Retry-After 头（秒）；没有、不是有限的非负数时返回 None。"""
    try:
        v = float(resp.headers.get("Retry-After"))
    except (TypeError, ValueError, AttributeError):
        return None
    return v if math.isfinite(v) and v >= 0 else None


def _retry_delay(e: AIError, attempt: int) -> float:
    d = e.retry_after if e.retry_after is not None else (3.0 * attempt if e.status == 429 else 2.0 * attempt)
    try:
        d = float(d)
    except (TypeError, ValueError):
        d = 2.0
    if not math.isfinite(d):
        d = MAX_RETRY_DELAY
    return max(0.0, min(d, MAX_RETRY_DELAY))


# ---------------------------------------------------------------- 主入口

def chat(messages: list[dict], cfg: dict | None, *, model: str | None = None, stream: bool = False,
         on_delta: Callable[[str], None] | None = None, on_reasoning: Callable[[str], None] | None = None,
         cancel: threading.Event | None = None, timeout: float | None = None,
         max_tokens: int | None = None, deadline: float | None = None, retries: int | None = None,
         heartbeat: Callable[[], None] | None = None) -> str:
    """发送一轮对话，返回完整正文（AIText，带 finish_reason）。

    stream=True 时每收到正文片段调用 on_delta(片段)，思考过程片段调用 on_reasoning(片段)；
    非流式时（若提供）各调用一次，传入完整内容。cancel 被 set 时尽快（POLL 秒内）停止并返回已收到的正文。
    deadline：整次调用的截止时间（time.monotonic()），到点抛 AIError(kind='deadline')；
    retries：可重试错误最多重试几次（默认 MAX_RETRIES）；
    heartbeat()：等待期间约每秒调用一次（报告生成时用来刷新进度、及时响应“取消生成”）。
    回调都在调用方线程执行，回调里抛出的异常原样向上传递（请求随即中止）。
    """
    s = ai_settings(cfg)
    if not s["enabled"]:
        raise AIError("AI 分析已关闭：请在设置里开启（配置项 ai.enabled）")
    msgs = _check_messages(messages)
    problem = endpoint_problem(s)
    if problem:
        raise AIError(problem)
    keys = find_keys(s["api_key_env"])
    if not keys:
        names = "、".join(s["api_key_env"])
        raise AIError(f"未找到 {s['provider']} API Key：请在 Windows 环境变量中设置 {names}（任选一个），"
                      "保存后重新点一次即可，无需重启软件")
    net.configure()  # api.deepseek.com 直连，不走本机代理软件

    model = (model or s["model"]).strip()
    limit = float(timeout) if timeout else s["timeout"]
    body = {"model": model, "messages": msgs, "stream": bool(stream)}
    if "reasoner" not in model:   # deepseek-reasoner 不支持 temperature（设置了也无效）
        body["temperature"] = s["temperature"]
    if max_tokens:
        body["max_tokens"] = int(max_tokens)
    url = endpoint(s["base_url"])
    max_retries = MAX_RETRIES if retries is None else max(0, int(retries))

    key_idx, attempt, started = 0, 0, time.monotonic()
    total = (deadline - started) if deadline is not None else None
    log.info("AI 请求：%s %s（%s，%d 条消息）", s["provider"], model, "流式" if stream else "非流式", len(msgs))
    while True:
        if _cancelled(cancel):
            return AIText("", "cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            err = _deadline_error(total)
            log.warning("AI 请求失败：%s", err)
            raise err
        env, key, _src = keys[key_idx]
        state = {"content": [], "delivered": False}
        try:
            text = _attempt(url, body, key, env, limit, stream, on_delta, on_reasoning, cancel, state,
                            deadline, total, heartbeat)
        except AIError as e:
            if e.status == 401 and key_idx + 1 < len(keys):   # 第一个 Key 无效，换下一个环境变量里的 Key
                log.warning("AI：环境变量 %s 中的 Key 无效，改用 %s", env, keys[key_idx + 1][0])
                key_idx += 1
                continue
            left = None if deadline is None else deadline - time.monotonic()
            if e.retryable and not state["delivered"] and attempt < max_retries and not _cancelled(cancel) \
                    and (left is None or left > 3.0):
                attempt += 1
                delay = _retry_delay(e, attempt)
                if left is not None:
                    delay = max(0.0, min(delay, left - 3.0))
                log.warning("AI 请求失败（%s），%.0f 秒后第 %d 次重试", e, delay, attempt)
                _wait_retry(delay, cancel, heartbeat)
                if _cancelled(cancel):
                    return AIText("", "cancelled")
                continue
            if not e.partial and state["content"]:
                e.partial = "".join(state["content"])
            log.warning("AI 请求失败：%s", e)
            raise
        log.info("AI 回答%s：%d 字，用时 %.1f 秒%s", "（已取消）" if text.finish_reason == "cancelled" else "完成",
                 len(text), time.monotonic() - started, "（达到长度上限）" if text.truncated else "")
        return text


def _wait_retry(delay: float, cancel, heartbeat) -> None:
    """重试前等待：没有 heartbeat 时一次等完；有 heartbeat 时分段等，每段之间调用它（可抛出取消）。"""
    if heartbeat is None:
        _sleep(delay, cancel)
        return
    end = time.monotonic() + delay
    while not _cancelled(cancel):
        left = end - time.monotonic()
        if left <= 0:
            break
        _sleep(min(HEARTBEAT_INTERVAL, left), cancel)
        heartbeat()


def _attempt(url, body, key, env, limit, stream, on_delta, on_reasoning, cancel, state, deadline, total,
             heartbeat) -> "AIText":
    """发一次请求：请求线程负责网络读写和解析，本线程（调用方）分发回调、检查取消 / 超时 / 截止时间。"""
    q: queue.Queue = queue.Queue()
    abort = threading.Event()
    holder: dict = {}
    worker = threading.Thread(target=_worker, args=(url, body, key, env, limit, stream, abort, q, holder),
                              name="kmcf-ai-request", daemon=True)
    sent = time.monotonic()
    last_beat = sent
    finished = False
    worker.start()
    try:
        while True:
            try:
                item = q.get(timeout=POLL)
            except queue.Empty:
                item = None
            if item is not None:
                kind = item[0]
                if kind == "delta":
                    state["delivered"] = True
                    state["content"].append(item[1])
                    if on_delta:
                        on_delta(item[1])
                elif kind == "reasoning":
                    state["delivered"] = True
                    if on_reasoning:
                        on_reasoning(item[1])
                elif kind == "end":
                    finished = True
                    return _finish(item[1], item[2], stream, state)
                elif kind == "error":
                    finished = True
                    err = item[1]
                    if state["delivered"] and err.retryable:   # 已经输出了内容：不重试，按“回答不完整”报错
                        err = AIError(f"{err}（连接中断，回答不完整）", status=err.status, retryable=False,
                                      partial="".join(state["content"]), kind="incomplete")
                    raise err
            if _cancelled(cancel):
                return AIText("".join(state["content"]), "cancelled")
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                raise _deadline_error(total, "".join(state["content"]))
            if not state["delivered"] and now - sent >= limit:   # 只收到保活行、迟迟没有内容
                raise AIError(f"请求超时（{limit:.0f} 秒内没有收到回答）：DeepSeek 服务器可能繁忙，请稍后再试",
                              retryable=True, kind="timeout")
            if heartbeat is not None and now - last_beat >= HEARTBEAT_INTERVAL:
                last_beat = now
                heartbeat()
    finally:
        if not finished:
            abort.set()
            _interrupt(holder)
            worker.join(ABORT_JOIN)


def _finish(finish, done: bool, stream: bool, state) -> "AIText":
    text = "".join(state["content"])
    if not text.strip():
        raise AIError(_empty_reason(finish),
                      retryable=not state["delivered"] and finish in (None, "insufficient_system_resource"))
    if stream and not done and not finish:   # 既没有 [DONE] 也没有 finish_reason：连接被提前关闭
        raise AIError("连接中断，回答不完整：DeepSeek 服务器提前关闭了连接，请稍后再试", partial=text,
                      kind="incomplete")
    if finish == "insufficient_system_resource":
        raise AIError("DeepSeek 推理资源不足，回答被中断（内容不完整），请稍后再试", partial=text, kind="incomplete")
    return AIText(text, finish or "stop")


def _interrupt(holder: dict) -> None:
    """中止时尽力关闭底层连接（shutdown 不需要拿读缓冲区的锁），让请求线程里阻塞中的读取尽快返回。

    调用方此时已经返回，不依赖这里成功：请求线程每收到一块数据（含保活行）都会检查中止标记，
    最迟在 timeout 秒后读超时结束（daemon 线程，不影响程序退出）。
    """
    resp = holder.get("resp")
    if resp is None:
        return   # 还在等响应头：请求线程会在超时后自行结束（daemon 线程，不影响退出）
    raw = getattr(resp, "raw", None)
    conn = getattr(raw, "connection", None) or getattr(raw, "_connection", None)
    candidates = (
        lambda: getattr(conn, "sock", None),
        # urllib3 HTTPResponse._fp（http.client.HTTPResponse）.fp（BufferedReader）.raw（SocketIO）._sock
        lambda: raw._fp.fp.raw._sock,
    )
    for get in candidates:
        try:
            sock = get()
        except Exception:  # noqa: BLE001
            continue
        if isinstance(sock, socket.socket):
            try:
                sock.shutdown(socket.SHUT_RDWR)
                return
            except OSError:
                continue


# ---------------------------------------------------------------- 请求线程

def _worker(url, body, key, env, limit, stream, abort: threading.Event, q: queue.Queue, holder: dict) -> None:
    """在请求线程里发请求、读响应、解析，结果放进队列：('delta'|'reasoning', 片段) / ('end', finish, done) /
    ('error', AIError)。最后一条（end/error）在关闭响应之后才放入。"""
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "User-Agent": f"kmcf/{__version__}",
    }
    result = None
    resp = None
    try:
        try:
            resp = requests.post(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers,
                                 stream=True, timeout=(min(CONNECT_TIMEOUT, limit), limit), allow_redirects=False)
        except requests.exceptions.RequestException as e:
            result = ("error", _net_error(e, limit))
            return
        holder["resp"] = resp
        if abort.is_set():
            return
        status = int(getattr(resp, "status_code", 0) or 0)
        if status != 200:
            raw = _read_body(resp, abort, 64 * 1024)
            result = ("error", http_error(status, raw, env, key, _retry_after(resp)))
            return
        if stream:
            finish, done = _read_stream(resp, q, abort, key)
        else:
            raw = _read_body(resp, abort, MAX_BODY)
            if abort.is_set():
                return
            finish, done = _parse_full(raw, q, key), True
        result = ("end", finish, done)
    except AIError as e:
        result = ("error", e)
    except requests.exceptions.RequestException as e:
        result = ("error", _net_error(e, limit))
    except Exception as e:  # noqa: BLE001  响应格式异常等：转成中文说明，不让线程带着异常退出
        log.debug("AI 响应处理出错：%s", type(e).__name__, exc_info=True)
        result = ("error", AIError(f"DeepSeek 返回的数据无法处理（{type(e).__name__}），请稍后再试", retryable=True))
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
        if result is not None:
            q.put(result)


def _read_body(resp, abort: threading.Event, limit: int) -> bytes:
    buf = bytearray()
    for chunk in resp.iter_content(chunk_size=None):
        if abort.is_set():
            break
        if chunk:
            buf += chunk
            if len(buf) > limit:
                break
    return bytes(buf)


def _parse_full(raw: bytes, q: queue.Queue, key: str):
    """非流式响应：放入思考过程和正文，返回 finish_reason。"""
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise AIError("DeepSeek 返回的数据无法解析，请稍后再试", retryable=True) from None
    if isinstance(data, dict) and data.get("error"):
        raise AIError("DeepSeek 返回错误：" + scrub(_server_detail(raw), key))
    choices = data.get("choices") if isinstance(data, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices else None
    if not isinstance(choice, dict):
        raise AIError("DeepSeek 返回的数据格式不对（缺少 choices）")
    msg = choice.get("message")
    msg = msg if isinstance(msg, dict) else {}
    content = msg.get("content") if isinstance(msg.get("content"), str) else ""
    reasoning = msg.get("reasoning_content") if isinstance(msg.get("reasoning_content"), str) else ""
    finish = choice.get("finish_reason") if isinstance(choice.get("finish_reason"), str) else None
    if reasoning:
        q.put(("reasoning", reasoning))
    if not content.strip():
        raise AIError(_empty_reason(finish), retryable=finish in (None, "insufficient_system_resource"))
    q.put(("delta", content))
    return finish


_EOL = re.compile(rb"\r\n|\r|\n")


def _iter_lines(resp, abort: threading.Event):
    """按行切分响应流（\\r\\n、\\n 或单独的 \\r），逐块读取以便及时响应中止。"""
    buf = b""
    for chunk in resp.iter_content(chunk_size=None):
        if abort.is_set():
            return
        if not chunk:
            continue
        buf += chunk
        while True:
            m = _EOL.search(buf)
            if m is None or (m.group() == b"\r" and m.end() == len(buf)):   # 末尾的 \r 可能是被切开的 \r\n
                break
            line, buf = buf[:m.start()], buf[m.end():]
            yield line.decode("utf-8", "replace")
            if abort.is_set():
                return
    if buf and not abort.is_set():
        for part in _EOL.split(buf):
            yield part.decode("utf-8", "replace")


def _read_stream(resp, q: queue.Queue, abort: threading.Event, key: str):
    """解析 SSE，把片段放进队列；返回 (finish_reason, 是否收到 [DONE])。格式不对的事件直接跳过。"""
    finish = None
    data_lines: list[str] = []

    def dispatch(payload: str) -> bool:
        """处理一个事件；返回 True 表示流结束（[DONE]）。"""
        nonlocal finish
        if payload.strip() == "[DONE]":
            return True
        try:
            ev = json.loads(payload)
        except ValueError:
            log.debug("AI 流式事件无法解析，已忽略")
            return False
        if not isinstance(ev, dict):
            return False
        if ev.get("error"):
            raise AIError("DeepSeek 返回错误：" + scrub(_server_detail(payload.encode("utf-8")), key))
        choices = ev.get("choices")
        for ch in choices if isinstance(choices, list) else []:
            if not isinstance(ch, dict):
                continue
            delta = ch.get("delta")
            if isinstance(delta, dict):
                r = delta.get("reasoning_content")
                if isinstance(r, str) and r:
                    q.put(("reasoning", r))
                c = delta.get("content")
                if isinstance(c, str) and c:
                    q.put(("delta", c))
            fr = ch.get("finish_reason")
            if isinstance(fr, str) and fr:
                finish = fr
        return False

    done = False
    for line in _iter_lines(resp, abort):
        if line == "":                       # 空行：一个事件结束（也可能是 keep-alive）
            if data_lines:
                payload, data_lines = "\n".join(data_lines), []
                if dispatch(payload):
                    done = True
                    break
            continue
        if line.startswith(":"):             # 注释行，如 ': keep-alive'
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
        # 其他字段（event:/id:/retry:）忽略
    if not done and data_lines and not abort.is_set():
        done = dispatch("\n".join(data_lines))
    return finish, done
