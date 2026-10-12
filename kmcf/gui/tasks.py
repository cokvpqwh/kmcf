"""后台工作线程：耗时操作（生成报告、任务计划、联网查询）都放这里，界面不卡。

用法：
    Task(fn, *args, on_done=回调, on_error=回调).start()
回调一定在界面线程里执行（QThread.finished 由线程对象所在的界面线程排队处理）。
运行中的线程登记在 _RUNNING 里，退出前用 wait_all() 等它们结束，
避免 “QThread: Destroyed while thread is still running”。
"""

import logging
import threading
import time
import traceback
from typing import Callable

from PySide6.QtCore import QThread, QTimer, Signal, Slot

log = logging.getLogger("kmcf")

_RUNNING: set["Task"] = set()
_FINISHED: list["Task"] = []  # 已结束、等下一轮事件循环再释放的线程对象（不能在自己的槽函数里销毁自己）


class Cancelled(BaseException):
    """用户取消。继承 BaseException：能穿过 generate() 里对 Exception 的捕获。"""


def _purge() -> None:
    _FINISHED.clear()


class Task(QThread):
    def __init__(self, fn: Callable, *args, on_done: Callable | None = None,
                 on_error: Callable | None = None, name: str = "", **kwargs):
        super().__init__()
        self._fn, self._args, self._kwargs = fn, args, kwargs
        self._on_done, self._on_error = on_done, on_error
        self.name = name or getattr(fn, "__name__", "task")
        self.result = None
        self.error: BaseException | None = None
        self.traceback = ""
        self.finished.connect(self._deliver)

    def start(self) -> "Task":
        _RUNNING.add(self)
        super().start()
        return self

    def run(self) -> None:
        try:
            self.result = self._fn(*self._args, **self._kwargs)
        except Cancelled as e:
            self.error = e
        except BaseException as e:  # noqa: BLE001 —— 工作线程里的任何异常都交回界面处理
            self.error = e
            self.traceback = traceback.format_exc()
            # ValueError 多为可预期的提示（如非交易日），记为 WARNING；同样保留 traceback，内部缺陷也能查到
            level = logging.WARNING if isinstance(e, ValueError) else logging.ERROR
            log.log(level, "后台任务 %s 失败：%s\n%s", self.name, e, self.traceback)

    @Slot()
    def _deliver(self) -> None:
        self.wait()  # finished 发出时线程可能还差最后一步，确保彻底结束
        _RUNNING.discard(self)
        _FINISHED.append(self)
        QTimer.singleShot(0, _purge)
        on_done, on_error = self._on_done, self._on_error
        self._on_done = self._on_error = None
        try:
            if self.error is not None:
                if on_error:
                    on_error(self.error)
            elif on_done:
                on_done(self.result)
        except RuntimeError as e:  # 回调所属的窗口/对话框已经关闭销毁
            log.debug("任务 %s 的回调已跳过：%s", self.name, e)


class GenerateTask(Task):
    """生成复盘报告：把 generate() 的进度回调转成 progress 信号；支持取消（在下一个进度点生效）。"""

    progress = Signal(int, str)

    def __init__(self, generate: Callable, date=None, force: bool = False, **kw):
        super().__init__(generate, date=date, force=force, progress=self._report, name="generate", **kw)
        self._cancel = threading.Event()
        self.date, self.force = date, force
        self.started_at = time.monotonic()

    def _report(self, pct: int, msg: str) -> None:
        if self._cancel.is_set():
            raise Cancelled()
        self.progress.emit(int(pct), str(msg))

    def cancel(self) -> None:
        self._cancel.set()

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()


class ChatTask(Task):
    """AI 对话（流式）：fn(*args, on_delta=, on_reasoning=, cancel=, **kwargs) 在工作线程运行。

    正文片段 -> delta 信号，思考过程片段 -> reasoning 信号（都排队到界面线程处理）；
    cancel() 设置取消事件，fn 应尽快返回已收到的内容。取消之后不再转发片段。
    expected：可预期的异常类型（如 AIError：Key 无效、余额不足、断网），只记一行 WARNING，不写 traceback。
    """

    delta = Signal(str)
    reasoning = Signal(str)

    def __init__(self, fn: Callable, *args, expected: tuple = (), **kw):
        cancel = threading.Event()
        super().__init__(fn, *args, on_delta=self._emit_delta, on_reasoning=self._emit_reasoning,
                         cancel=cancel, **{"name": "ai_chat", **kw})
        self._cancel = cancel
        self._expected = tuple(expected)
        self.started_at = time.monotonic()

    def _emit_delta(self, text: str) -> None:
        if text and not self._cancel.is_set():
            self.delta.emit(str(text))

    def _emit_reasoning(self, text: str) -> None:
        if text and not self._cancel.is_set():
            self.reasoning.emit(str(text))

    def run(self) -> None:
        if not self._expected:
            super().run()
            return
        try:
            self.result = self._fn(*self._args, **self._kwargs)
        except self._expected as e:
            self.error = e
            log.warning("AI 请求失败：%s", e)
        except BaseException as e:  # noqa: BLE001
            self.error = e
            self.traceback = traceback.format_exc()
            log.error("后台任务 %s 失败：%s\n%s", self.name, e, self.traceback)

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()


def running() -> list[Task]:
    return [t for t in _RUNNING if t.isRunning()]


def wait_all(timeout_ms: int = 10000) -> bool:
    """取消正在进行的生成 / AI 对话，并等待所有后台线程结束；超时返回 False。"""
    deadline = time.monotonic() + timeout_ms / 1000
    for t in list(_RUNNING):
        if isinstance(t, (GenerateTask, ChatTask)):
            t.cancel()
    for t in list(_RUNNING):
        left = max(0, int((deadline - time.monotonic()) * 1000))
        if t.isRunning() and not t.wait(left):
            return False
    return True
