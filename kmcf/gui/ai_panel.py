"""「AI 分析」面板：主窗口右侧的可停靠面板，围绕当前查看的复盘报告向 AI 提问。

- AI 只做客观数据分析与解读（系统提示词在 kmcf.ai 里，明确禁止投资建议），
  每条 AI 回复下方都注明“AI 生成，仅供参考，不构成投资建议”；
- API Key 只从环境变量读取：界面只调用 kmcf.ai.key_status()，只显示变量名，从不接触 Key 的值；
- 持仓数量/成本/盈亏默认不发送，勾选“包含我的持仓数据”后才发送；取消勾选后，之前带持仓数据的对话轮次也不再带给 AI；
- 回答结束后用 kmcf.ai.compliance_hits 检查，含疑似投资建议/预测表述时在回复下方醒目提示；
- AI 回复里的网址点击后先确认（显示完整网址）再用浏览器打开；
- 请求放在工作线程（tasks.ChatTask）里流式进行，片段经信号回到界面线程，按 REFRESH_MS 节流刷新，
  而且只重排最后一条回复；同一时间只允许一个请求；停止 / 关闭面板 / 退出程序都会取消请求。

kmcf.ai 在第一次打开面板时才导入（ai_module 参数可替换，测试时传入假的实现）。
"""

import datetime as dt
import html
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication, QKeyEvent, QTextBlockFormat, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDockWidget, QFrame, QHBoxLayout, QLabel, QLayout,
                               QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QTextBrowser, QVBoxLayout,
                               QWidget)

from . import logic, theme
from .ai_markdown import md_to_html, plain_text
from .tasks import ChatTask

log = logging.getLogger("kmcf")

DISCLAIMER = "AI 生成，仅供参考，不构成投资建议"
COMPLIANCE_WARN = "注意：本回答含疑似操作建议或预测表述（{terms}），请忽略这部分内容；AI 只做客观数据分析，不提供投资建议。"
TRUNCATED_NOTE = "回答达到长度上限，内容可能不完整"
PLACEHOLDER = "输入问题，Enter 发送，Shift+Enter 换行"
ENV_HELP = ("设置方法：按 Win 键搜索“编辑账户的环境变量”并打开 → 在“用户变量”里点「新建」→ "
            "变量名填 {env}，变量值填你的 API Key → 一路点「确定」。")
LINK_THINK = "think"
LINK_RETRY = "retry"


def _import_ai():
    """第一次打开面板时才导入 kmcf.ai（写成普通 import，PyInstaller 能静态发现）。"""
    import kmcf.ai
    return kmcf.ai


def _default_load_cfg() -> dict:
    from ..config import load_config
    return load_config()


def _now() -> str:
    return dt.datetime.now().strftime("%H:%M")


def _esc_lines(text: str) -> str:
    return html.escape(str(text or "")).replace("\n", "<br>")


class PanelError(Exception):
    """给用户看的错误（读取报告数据失败等），不写 traceback。"""


def read_report(json_path: Path) -> dict:
    """读取 reports/YYYY-MM-DD.json（在工作线程调用）。"""
    try:
        data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PanelError("这份报告缺少数据文件（.json），请重新生成复盘后再试") from None
    except (OSError, ValueError) as e:
        raise PanelError(f"读取报告数据失败：{e}") from None
    if not isinstance(data, dict):
        raise PanelError("报告数据格式不正确，请重新生成复盘后再试")
    return data


def chat_job(ai, json_path: Path, question: str, cfg: dict, history: list[dict], include_holdings: bool,
             model: str, max_tokens: int | None, *, on_delta, on_reasoning, cancel) -> str:
    """工作线程：读取报告 -> 组装消息 -> 流式请求。返回完整正文（取消时返回已收到的部分）。"""
    report = read_report(json_path)
    messages = ai.build_messages(question, report, cfg, history=history, include_holdings=include_holdings)
    if cancel.is_set():
        return ""
    return ai.chat(messages, cfg, model=model or None, stream=True, on_delta=on_delta,
                   on_reasoning=on_reasoning, cancel=cancel, max_tokens=max_tokens)


@dataclass
class ChatItem:
    kind: str                  # user / ai / note
    text: str = ""             # 用户问题（显示用）/ AI 正文（Markdown）/ 提示文字
    time: str = ""
    provider: str = ""
    model: str = ""
    reasoning: str = ""        # deepseek-reasoner 的思考过程
    state: str = "done"        # waiting / thinking / streaming / stopping / stopped / done / error
    error: str = ""
    show_reasoning: bool = False
    question: str = ""         # 实际发给 AI 的问题（快捷问题按钮的完整提问），重试时用
    flags: tuple = ()          # 合规检查命中的疑似投资建议/预测表述
    truncated: bool = False    # 回答因长度上限被截断


# ====================================================================== 小部件

class FlowLayout(QLayout):
    """自动换行的流式布局（快捷问题按钮）。"""

    def __init__(self, parent=None, hspacing: int = 6, vspacing: int = 6):
        super().__init__(parent)
        self._items = []
        self._h, self._v = hspacing, vspacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i: int):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i: int):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._layout(rect, False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _layout(self, rect: QRect, test: bool) -> int:
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = r.x(), r.y(), 0
        for it in self._items:
            w = it.widget()
            if w is not None and w.isHidden():
                continue
            sz = it.sizeHint()
            if x > r.x() and x + sz.width() > r.right() + 1:
                x, y, line_h = r.x(), y + line_h + self._v, 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), sz))
            x += sz.width() + self._h
            line_h = max(line_h, sz.height())
        return y + line_h - rect.y() + m.bottom()


class ChatInput(QPlainTextEdit):
    """输入框：Enter（或 Ctrl+Enter）发送，Shift+Enter 换行。"""

    submitted = Signal()

    def keyPressEvent(self, e: QKeyEvent) -> None:
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.submitted.emit()
            return
        super().keyPressEvent(e)


# ====================================================================== 面板

class AIPanel(QWidget):
    HISTORY_TURNS = 6          # 带给 AI 的最近对话轮数
    HISTORY_CHARS = 12000      # 带给 AI 的历史对话总字数上限
    REFRESH_MS = 80            # 流式输出的刷新间隔
    STOP_GRACE_MS = 3000       # 点「停止」后最多等这么久；还没停下就放弃这个请求（结果丢弃），界面恢复可用

    statusMessage = Signal(str)

    def __init__(self, parent=None, ai_module=None, load_cfg: Callable[[], dict] | None = None):
        super().__init__(parent)
        self.setObjectName("AIPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._ai = ai_module if ai_module is not None else _import_ai()
        self._load_cfg = load_cfg or _default_load_cfg
        self._cfg: dict = {}
        self._settings: dict = {}
        self._key: dict = {"found": False, "env": None}
        self._report_json: Path | None = None
        self._report_date: dt.date | None = None
        self._items: list[ChatItem] = []
        self._history: list[dict] = []
        self._epoch = 0            # 切换报告 / 清空对话时加 1：之前发出的请求结果不再写进上下文
        self._task: ChatTask | None = None
        self._task_seq = 0
        self._task_epoch = 0
        self._task_holdings = False  # 当前请求是否带了持仓数据（写进对话历史，取消勾选后不再发送这些轮次）
        self._last_report_json: Path | None = None  # 上一份非空的报告（经过“暂无报告”再切到别的报告也要清空上下文）
        self._pending_q = ""
        self._pending_notes: list[str] = []
        self._tail_pos = 0         # 最后一条消息在文档里的起始位置（流式刷新只替换这之后的内容）
        self._closed = False
        self._rendered_any = False  # 对话区是否已有消息（否则显示的是欢迎语）
        self.max_tokens: int | None = None

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(self.REFRESH_MS)
        self._refresh_timer.timeout.connect(self._refresh_tail)
        self._stop_timer = QTimer(self)
        self._stop_timer.setSingleShot(True)
        self._stop_timer.timeout.connect(self._abandon)

        self._build_ui()
        self.reload_config(reset_holdings=True)
        self._render_all()

    # ------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(8)

        # 报告日期 + 设置
        row = QHBoxLayout()
        row.setSpacing(6)
        lbl = QLabel("分析报告")
        lbl.setObjectName("SectionLabel")
        row.addWidget(lbl)
        self.lbl_report = QLabel("暂无报告")
        self.lbl_report.setObjectName("AIReportDate")
        row.addWidget(self.lbl_report)
        row.addStretch(1)
        self.btn_settings = QPushButton("AI 设置…")
        self.btn_settings.setObjectName("Link")
        self.btn_settings.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_settings.setToolTip("服务地址、模型、API Key 环境变量名、生成复盘时是否自动写 AI 点评")
        self.btn_settings.clicked.connect(self.open_settings)
        row.addWidget(self.btn_settings)
        v.addLayout(row)

        # 模型 + 持仓
        row = QHBoxLayout()
        row.setSpacing(6)
        lbl = QLabel("模型")
        lbl.setObjectName("Hint")
        row.addWidget(lbl)
        self.combo_model = QComboBox()
        self.combo_model.setMinimumWidth(150)
        self.combo_model.setToolTip("deepseek-chat：速度快；deepseek-reasoner：先思考再回答，更慢但更细致")
        row.addWidget(self.combo_model)
        row.addStretch(1)
        self.chk_holdings = QCheckBox("包含我的持仓数据")
        self.chk_holdings.setToolTip("勾选后会把持仓数量/成本/盈亏发给 AI；不勾选时只发送自选股的行情数据")
        self.chk_holdings.toggled.connect(self._on_holdings_toggled)
        row.addWidget(self.chk_holdings)
        v.addLayout(row)
        self.lbl_holdings = QLabel("已勾选：持仓数量/成本/盈亏会发给 AI")
        self.lbl_holdings.setObjectName("WarnText")
        self.lbl_holdings.setVisible(False)
        v.addWidget(self.lbl_holdings)

        # Key 状态
        self.key_ok = QFrame()
        self.key_ok.setObjectName("KeyOk")
        kh = QHBoxLayout(self.key_ok)
        kh.setContentsMargins(10, 5, 10, 5)
        kh.setSpacing(6)
        self.lbl_key_dot = QLabel("●")
        kh.addWidget(self.lbl_key_dot)
        self.lbl_key = QLabel("")
        self.lbl_key.setObjectName("Caption")
        self.lbl_key.setWordWrap(True)
        self.lbl_key.setToolTip("API Key 只从环境变量读取，不会保存在软件里")
        kh.addWidget(self.lbl_key, 1)
        v.addWidget(self.key_ok)

        self.key_warn = QFrame()
        self.key_warn.setObjectName("KeyWarn")
        kw = QVBoxLayout(self.key_warn)
        kw.setContentsMargins(10, 8, 10, 8)
        kw.setSpacing(4)
        self.lbl_warn_title = QLabel("")
        self.lbl_warn_title.setObjectName("KeyWarnTitle")
        self.lbl_warn_title.setWordWrap(True)
        kw.addWidget(self.lbl_warn_title)
        self.lbl_warn_text = QLabel("")
        self.lbl_warn_text.setObjectName("Caption")
        self.lbl_warn_text.setWordWrap(True)
        self.lbl_warn_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        kw.addWidget(self.lbl_warn_text)
        wrow = QHBoxLayout()
        wrow.setSpacing(6)
        wrow.addStretch(1)
        self.btn_recheck = QPushButton("重新检测")
        self.btn_recheck.setToolTip("设置好环境变量后点这里，通常不用重启软件")
        self.btn_recheck.clicked.connect(lambda: self.refresh_key(announce=True))
        wrow.addWidget(self.btn_recheck)
        self.btn_warn_settings = QPushButton("AI 设置…")
        self.btn_warn_settings.clicked.connect(self.open_settings)
        wrow.addWidget(self.btn_warn_settings)
        kw.addLayout(wrow)
        v.addWidget(self.key_warn)

        # 快捷问题
        self.quick_box = QWidget()
        self.quick_box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        flow = FlowLayout(self.quick_box, 6, 6)
        self.quick_buttons: list[QPushButton] = []
        for label, prompt in list(getattr(self._ai, "QUICK_PROMPTS", []) or []):
            b = QPushButton(str(label))
            b.setObjectName("Chip")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(str(prompt))
            b.clicked.connect(lambda _c=False, q=str(prompt), lab=str(label): self.send(q, label=lab))
            flow.addWidget(b)
            self.quick_buttons.append(b)
        v.addWidget(self.quick_box)

        # 对话区
        self.chat = QTextBrowser()
        self.chat.setObjectName("Chat")
        self.chat.setOpenLinks(False)
        self.chat.setOpenExternalLinks(False)
        self.chat.anchorClicked.connect(self._on_anchor)
        self.chat.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.chat.customContextMenuRequested.connect(self._chat_menu)
        doc = self.chat.document()
        doc.setDocumentMargin(10)
        doc.setIndentWidth(18)
        v.addWidget(self.chat, 1)

        # 输入
        self.input = ChatInput()
        self.input.setObjectName("ChatInput")
        self.input.setPlaceholderText(PLACEHOLDER)
        self.input.setTabChangesFocus(True)
        lh = self.input.fontMetrics().lineSpacing()
        self.input.setFixedHeight(int(lh * 3 + 18))
        self.input.submitted.connect(self.send)
        v.addWidget(self.input)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.btn_clear = QPushButton("清空对话")
        self.btn_clear.setObjectName("Link")
        self.btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clear.clicked.connect(self.clear)
        row.addWidget(self.btn_clear)
        row.addStretch(1)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setToolTip("停止当前回答（已收到的内容会保留）")
        self.btn_stop.clicked.connect(self.stop)
        row.addWidget(self.btn_stop)
        self.btn_send = QPushButton("发送")
        self.btn_send.setObjectName("Primary")
        self.btn_send.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_send.setStyleSheet("padding: 6px 18px; font-size: 9.5pt;")
        self.btn_send.clicked.connect(lambda: self.send())
        row.addWidget(self.btn_send)
        v.addLayout(row)

        self.lbl_disclaimer = QLabel(f"{DISCLAIMER}。AI 只做客观数据解读，不提供买卖建议。")
        self.lbl_disclaimer.setObjectName("Caption")
        self.lbl_disclaimer.setWordWrap(True)
        v.addWidget(self.lbl_disclaimer)

    # ------------------------------------------------------------ 配置与 Key
    def reload_config(self, reset_holdings: bool = False) -> None:
        """重新读取配置（打开面板、保存 AI 设置之后）。"""
        try:
            cfg = self._load_cfg()
        except Exception as e:  # noqa: BLE001
            log.warning("读取配置失败：%s", e)
            cfg = {}
        self._cfg = cfg if isinstance(cfg, dict) else {}
        try:
            self._settings = dict(self._ai.ai_settings(self._cfg))
        except Exception as e:  # noqa: BLE001
            log.warning("读取 AI 设置失败：%s", e)
            self._settings = {}
        s = self._settings
        models = logic.merge_models(s.get("models"), s.get("model", ""))
        cur = self.combo_model.currentText()
        self.combo_model.blockSignals(True)
        self.combo_model.clear()
        self.combo_model.addItems(models)
        keep = cur if (cur and cur in models and not reset_holdings) else s.get("model", "")
        if keep:
            self.combo_model.setCurrentText(keep)
        self.combo_model.blockSignals(False)
        if reset_holdings:
            self.chk_holdings.setChecked(bool(s.get("include_holdings", False)))
        self.refresh_key()

    def provider(self) -> str:
        return str(self._settings.get("provider") or "AI")

    def _env_names(self) -> list[str]:
        envs = self._settings.get("api_key_env") or []
        if isinstance(envs, str):
            envs = [envs]
        return [str(e) for e in envs if str(e).strip()]

    def refresh_key(self, announce: bool = False) -> dict:
        """重新检测 API Key（只看是否存在、来自哪个变量，从不读取或显示它的值）。"""
        try:
            st = dict(self._ai.key_status(self._cfg))
        except Exception as e:  # noqa: BLE001
            log.warning("检测 API Key 失败：%s", e)
            st = {"found": False, "env": None}
        self._key = {"found": bool(st.get("found")), "env": st.get("env")}
        self._update_key_ui()
        self._update_state()
        if announce:
            if self._key["found"]:
                self.statusMessage.emit(f"已检测到 {self.provider()} API Key（环境变量 {self._key['env']}）")
            else:
                self.statusMessage.emit(f"仍未检测到 {self.provider()} API Key，请检查环境变量名是否正确")
        return dict(self._key)

    def ai_enabled(self) -> bool:
        return bool(self._settings.get("enabled", True))

    def key_found(self) -> bool:
        return bool(self._key.get("found"))

    def _update_key_ui(self) -> None:
        prov = self.provider()
        envs = self._env_names()
        first = envs[0] if envs else "DEEPSEEK_API_KEY"
        # 不能发送时快捷问题反正不可用：先收起来，把位置让给设置说明（窗口较矮时说明文字不被挤掉）
        self.quick_box.setVisible(self.ai_enabled() and self.key_found())
        if not self.ai_enabled():
            self.key_ok.setVisible(False)
            self.key_warn.setVisible(True)
            self.lbl_warn_title.setText("AI 分析已关闭")
            self.lbl_warn_text.setText("在「AI 设置…」里勾选“启用 AI 分析”后即可使用。")
            self.btn_recheck.setVisible(False)
            return
        self.btn_recheck.setVisible(True)
        if self.key_found():
            self.key_warn.setVisible(False)
            self.key_ok.setVisible(True)
            self.lbl_key_dot.setStyleSheet(f"color: {theme.PRIMARY}; font-size: 9pt;")
            self.lbl_key.setText(f"已连接 {prov}（来自环境变量 {self._key.get('env') or first}）")
        else:
            self.key_ok.setVisible(False)
            self.key_warn.setVisible(True)
            self.lbl_warn_title.setText(f"未检测到 {prov} API Key")
            others = "、".join(envs[1:])
            more = f"也会依次尝试：{others}。" if others else ""
            self.lbl_warn_text.setText(
                f"请在系统环境变量里设置 {first}，设置好后点「重新检测」即可，无需重启软件。{more}\n"
                + ENV_HELP.format(env=first))
            self.key_warn.setToolTip(f"未检测到 {prov} API Key：请在系统环境变量里设置 {first}，"
                                     "设置好后点「重新检测」即可，无需重启软件")

    # ------------------------------------------------------------ 报告
    def set_report(self, path) -> None:
        """主窗口切换报告时调用。path 是报告的 .html 或 .json 路径；None 表示没有报告。"""
        new_json = Path(path).with_suffix(".json") if path else None
        if new_json == self._report_json:
            return
        new_date = None
        if new_json is not None:
            try:
                new_date = dt.date.fromisoformat(new_json.stem)
            except ValueError:
                new_date = None
        last = self._last_report_json   # 上一份非空的报告（中间可能经过“暂无报告”）
        self._report_json, self._report_date = new_json, new_date
        self._update_report_label()
        if new_json is not None:
            self._last_report_json = new_json
        if last is not None and new_json is not None and new_json != last and (self._items or self._history):
            label = logic.report_label(new_date) if new_date else new_json.stem
            self._epoch += 1           # 之后的提问只基于新报告，不再带旧报告的对话上下文
            self._history.clear()
            if self._items:
                self._add_note(f"已切换到 {label} 的报告")
        self._update_state()
        if not self._items:
            self._render_all()

    def _update_report_label(self) -> None:
        if self._report_json is None:
            self.lbl_report.setText("暂无报告")
        elif self._report_date is not None:
            self.lbl_report.setText(logic.report_label(self._report_date))
        else:
            self.lbl_report.setText(self._report_json.stem)

    def report_date(self) -> dt.date | None:
        return self._report_date

    # ------------------------------------------------------------ 状态
    def is_busy(self) -> bool:
        return self._task is not None

    def _blocked_reason(self) -> str:
        if not self.ai_enabled():
            return "AI 分析已关闭，可在「AI 设置…」里开启"
        if not self.key_found():
            return f"未检测到 {self.provider()} API Key，暂时不能发送"
        if self._report_json is None:
            return "还没有复盘报告：请先点击左侧「生成最新复盘」"
        return ""

    def _update_state(self) -> None:
        busy = self.is_busy()
        reason = self._blocked_reason()
        ready = not reason
        self.btn_send.setEnabled(ready and not busy)
        for b in self.quick_buttons:
            b.setEnabled(ready and not busy)
        stopping = busy and bool(self._items) and self._items[-1].state == "stopping"
        self.btn_stop.setEnabled(busy and not stopping)
        self.btn_stop.setText("正在停止…" if stopping else "停止")
        self.btn_clear.setEnabled(not busy and bool(self._items))
        self.combo_model.setEnabled(not busy)
        self.chk_holdings.setEnabled(not busy)
        self.input.setPlaceholderText(reason or ("AI 正在回答…（可以先输入下一个问题）" if busy else PLACEHOLDER))
        self.btn_send.setToolTip(reason or "发送（Enter）")

    @Slot(bool)
    def _on_holdings_toggled(self, on: bool) -> None:
        self.lbl_holdings.setVisible(bool(on))
        if not on and any(m.get("holdings") for m in self._history):
            self._add_note("已取消“包含我的持仓数据”：之前带持仓数据的对话不会再发给 AI")

    def focus_input(self) -> None:
        self.input.setFocus()

    # ------------------------------------------------------------ 发送
    def _trimmed_history(self, include_holdings: bool | None = None) -> list[dict]:
        """带给 AI 的最近几轮对话。不包含持仓时，跳过之前带持仓数据的轮次（AI 的回答里可能复述了持仓）。"""
        if include_holdings is None:
            include_holdings = self.chk_holdings.isChecked()
        h = [m for m in self._history if include_holdings or not m.get("holdings")]
        h = h[-2 * self.HISTORY_TURNS:]
        out, total = [], 0
        for m in reversed(h):
            total += len(m.get("content", ""))
            if total > self.HISTORY_CHARS and out:
                break
            out.append({"role": m.get("role"), "content": m.get("content", "")})
        out.reverse()
        while out and out[0].get("role") != "user":
            out.pop(0)
        return out

    def send(self, text: str | None = None, label: str | None = None) -> bool:
        """发送问题。text=None 时取输入框内容；label 是对话区显示的文字（快捷问题按钮用按钮名）。"""
        if self._closed:
            return False
        if self.is_busy():
            self.statusMessage.emit("AI 正在回答，请稍候，或点「停止」后再发送")
            return False
        from_input = text is None
        question = (self.input.toPlainText() if from_input else str(text)).strip()
        if not question:
            return False
        # 每次发送前重新读取配置和 Key（可能刚在别处改了设置 / 环境变量）
        self.reload_config()
        reason = self._blocked_reason()
        if reason:
            self.statusMessage.emit(reason)
            return False
        if from_input:
            self.input.clear()
        model = self.combo_model.currentText().strip() or str(self._settings.get("model") or "")
        self._items.append(ChatItem("user", text=label or question, time=_now(), question=question))
        self._append_item(self._items[-1])
        item = ChatItem("ai", time=_now(), provider=self.provider(), model=model, state="waiting", question=question)
        self._items.append(item)
        self._append_item(item)
        self._scroll_to_bottom()

        self._task_seq += 1
        seq = self._task_seq
        self._task_epoch = self._epoch
        self._pending_q = question
        holdings = self.chk_holdings.isChecked()
        self._task_holdings = holdings
        expected = tuple(x for x in (getattr(self._ai, "AIError", None), PanelError) if isinstance(x, type))
        task = ChatTask(chat_job, self._ai, self._report_json, question, self._cfg, self._trimmed_history(holdings),
                        holdings, model, self.max_tokens, expected=expected,
                        on_done=lambda r, s=seq: self._on_done(r, s),
                        on_error=lambda e, s=seq: self._on_error(e, s))
        task.delta.connect(self._on_delta)
        task.reasoning.connect(self._on_reasoning)
        self._task = task
        self._update_state()
        task.start()
        return True

    def _current_ai(self) -> ChatItem | None:
        if self._items and self._items[-1].kind == "ai":
            return self._items[-1]
        return None

    @Slot(str)
    def _on_delta(self, s: str) -> None:
        if self.sender() is not self._task or self._closed:
            return
        item = self._current_ai()
        if item is None:
            return
        item.text += s
        if item.state in ("waiting", "thinking"):
            item.state = "streaming"
        self._schedule_refresh()

    @Slot(str)
    def _on_reasoning(self, s: str) -> None:
        if self.sender() is not self._task or self._closed:
            return
        item = self._current_ai()
        if item is None:
            return
        item.reasoning += s
        if item.state == "waiting":
            item.state = "thinking"
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _finish(self) -> None:
        self._task = None
        self._stop_timer.stop()
        self._refresh_timer.stop()
        self._refresh_tail()
        self._flush_notes()
        self._update_state()

    def _on_done(self, result, seq: int) -> None:
        if self._closed or seq != self._task_seq or self._task is None:
            return  # 已放弃的请求（停止后超时）或面板已关闭
        cancelled = self._task.cancelled
        item = self._current_ai()
        if item is not None:
            if isinstance(result, str) and result.strip():
                item.text = str(result)  # 以返回的完整正文为准
            item.state = "stopped" if cancelled else "done"
            item.truncated = getattr(result, "finish_reason", None) == "length"
            item.flags = tuple(self._compliance_hits(item.text))
            if item.text.strip() and self._task_epoch == self._epoch:
                h = self._task_holdings
                self._history += [{"role": "user", "content": self._pending_q, "holdings": h},
                                  {"role": "assistant", "content": item.text, "holdings": h}]
        self._finish()
        if cancelled:
            self.statusMessage.emit("已停止 AI 回答")

    def _on_error(self, exc: BaseException, seq: int) -> None:
        if self._closed or seq != self._task_seq or self._task is None:
            return
        item = self._current_ai()
        if item is not None:
            item.state = "error"
            ai_error = getattr(self._ai, "AIError", None)
            if isinstance(exc, PanelError) or (isinstance(ai_error, type) and isinstance(exc, ai_error)):
                item.error = str(exc) or "请求失败"
            else:
                first = (str(exc).strip().splitlines() or [type(exc).__name__])[0][:150]
                item.error = f"出错了：{first}（详细信息已写入日志文件 {logic.LOG_FILE}）"
            item.flags = tuple(self._compliance_hits(item.text))   # 中途出错时已显示的部分也要检查
        self._finish()
        self.statusMessage.emit("AI 请求失败：" + (item.error if item else str(exc)))

    def _compliance_hits(self, text: str) -> list[str]:
        """AI 回答里的疑似投资建议/预测表述（kmcf.ai.compliance_hits）；检查不了时返回 []。"""
        fn = getattr(self._ai, "compliance_hits", None)
        if not callable(fn) or not str(text or "").strip():
            return []
        try:
            hits = [str(h) for h in (fn(text) or [])]
        except Exception as e:  # noqa: BLE001
            log.debug("合规检查失败：%s", e)
            return []
        if hits:
            log.info("AI 回答含疑似投资建议/预测表述：%s（已在界面提示）", "、".join(hits[:8]))
        return hits

    def stop(self) -> None:
        """停止当前回答：通知工作线程尽快结束；已收到的内容保留。"""
        task = self._task
        if task is None:
            return
        task.cancel()
        item = self._current_ai()
        if item is not None and item.state not in ("stopped", "done", "error"):
            item.state = "stopping"
            self._refresh_tail()
        self._update_state()
        self._stop_timer.start(self.STOP_GRACE_MS)

    def _abandon(self) -> None:
        """停止后迟迟没有结束（例如还在等服务器第一个字节）：丢弃这个请求，界面恢复可用。
        线程本身仍登记在 tasks._RUNNING 里，结束后自然释放，不会出现“线程运行中被销毁”。"""
        if self._task is None:
            return
        log.info("AI 请求停止超时，已放弃等待")
        self._task_seq += 1
        item = self._current_ai()
        if item is not None:
            item.state = "stopped"
            item.flags = tuple(self._compliance_hits(item.text))
        self._finish()

    def shutdown(self) -> None:
        """面板 / 主窗口关闭：取消请求，之后的回调全部忽略。"""
        if self._task is not None:
            self._task.cancel()
        self._closed = True
        self._refresh_timer.stop()
        self._stop_timer.stop()

    def clear(self) -> None:
        if self.is_busy():
            return
        self._items.clear()
        self._history.clear()
        self._pending_notes.clear()
        self._epoch += 1
        self._render_all()
        self._update_state()

    # ------------------------------------------------------------ 提示消息
    def _add_note(self, text: str) -> None:
        if self.is_busy():  # 正在输出：等这条回答结束再插入，保证流式刷新的总是最后一条
            self._pending_notes.append(text)
            return
        self._items.append(ChatItem("note", text=text, time=_now()))
        self._append_item(self._items[-1])
        self._scroll_to_bottom()

    def _flush_notes(self) -> None:
        notes, self._pending_notes = self._pending_notes, []
        for n in notes:
            self._add_note(n)

    # ------------------------------------------------------------ 渲染
    def _item_html(self, item: ChatItem, idx: int) -> str:
        t = theme.current()
        small = f"color:{t['faint']}; font-size:8.5pt;"
        if item.kind == "user":
            return (f'<span style="{small}">你 · {item.time}</span>'
                    f'<table width="88%" align="right" cellspacing="0" cellpadding="8" bgcolor="{t["accent_soft"]}">'
                    f'<tr><td style="color:{t["text"]};">{_esc_lines(item.text)}</td></tr></table>')
        if item.kind == "note":
            return f'<span style="{small}">—— {html.escape(item.text)} ——</span>'

        # 第一段（标题行）的段落格式由 _insert_item 设置，这里只写文字格式
        parts = [f'<span style="color:{t["accent_text"]}; font-weight:600;">{html.escape(item.provider or "AI")}</span>'
                 f'<span style="{small}">&nbsp;·&nbsp;{html.escape(item.model)}&nbsp;·&nbsp;{item.time}</span>']
        if item.reasoning:
            n = len(item.reasoning)
            thinking = item.state == "thinking"
            title = f"思考中…（{n} 字）" if thinking else f"思考过程（{n} 字）"
            action = "收起" if item.show_reasoning else "展开"
            parts.append(f'<p style="margin-top:0px; margin-bottom:4px; {small}">{title} · '
                         f'<a href="kmcf:{LINK_THINK}/{idx}" style="color:{t["accent"]}; text-decoration:none;">'
                         f"{action}</a></p>")
            if item.show_reasoning:
                parts.append(f'<table width="100%" cellspacing="0" cellpadding="6" bgcolor="{t["card2"]}" '
                             f'style="margin-bottom:6px;"><tr><td style="{small}">'
                             f"{_esc_lines(item.reasoning)}</td></tr></table>")
        if item.text:
            parts.append(md_to_html(item.text, t))
        elif item.state == "waiting":
            parts.append(f'<p style="margin-top:0px; {small}"><i>正在分析报告数据…</i></p>')
        elif item.state == "thinking" and not item.reasoning:
            parts.append(f'<p style="margin-top:0px; {small}"><i>思考中…</i></p>')
        if item.truncated and item.state == "done":
            parts.append(f'<p style="margin-top:4px; {small}">（{TRUNCATED_NOTE}）</p>')
        if item.flags:
            terms = "、".join(item.flags[:6])
            parts.append(f'<p style="margin-top:6px; color:{t["warn"]}; font-weight:600;">'
                         f"{html.escape(COMPLIANCE_WARN.format(terms=terms))}</p>")
        if item.state == "stopping":
            parts.append(f'<p style="{small}"><i>正在停止…</i></p>')
        elif item.state == "stopped":
            parts.append(f'<p style="{small}">（已停止）</p>')
        elif item.state == "error":
            parts.append(f'<p style="margin-top:4px; color:{t["up"]};">请求失败：{_esc_lines(item.error)}'
                         f'&nbsp;&nbsp;<a href="kmcf:{LINK_RETRY}/{idx}" style="color:{t["accent"]};">重试</a></p>')
        if item.text:
            parts.append(f'<p style="margin-top:6px; margin-bottom:2px; color:{t["faint"]}; font-size:8pt;">'
                         f"{DISCLAIMER}</p>")
        return "".join(parts)

    def _welcome_html(self) -> str:
        t = theme.current()
        if self._report_json is None:
            body = "还没有复盘报告。<br>请先点击左侧「生成最新复盘」，再回来向 AI 提问。"
        else:
            body = ("基于当前报告的数据向 AI 提问，<br>可以点上方的快捷问题，或在下方输入。<br><br>"
                    "AI 只做客观数据分析与解读，不提供买卖建议。")
        return (f'<p align="center" style="margin-top:40px; color:{t["faint"]}; line-height:150%;">{body}</p>')

    _ALIGN = {"user": Qt.AlignmentFlag.AlignRight, "note": Qt.AlignmentFlag.AlignHCenter}

    def _insert_item(self, cur: QTextCursor, item: ChatItem, idx: int) -> None:
        """在文档末尾（cur）插入一条消息。

        insertHtml 会把 HTML 的第一段并入光标所在的段落，第一段写在 HTML 里的段落格式（上边距、对齐）不会生效，
        所以先插入一个带好段落格式的空段落，再插入 HTML。
        """
        doc = self.chat.document()
        fmt = QTextBlockFormat()
        fmt.setTopMargin(4 if doc.isEmpty() else 16)
        fmt.setBottomMargin(3)
        fmt.setAlignment(self._ALIGN.get(item.kind, Qt.AlignmentFlag.AlignLeft))
        if doc.isEmpty():
            cur.setBlockFormat(fmt)
        else:
            cur.insertBlock(fmt, QTextCharFormat())
        start = cur.position()
        cur.insertHtml(self._item_html(item, idx))
        # 文档为空时 insertHtml 会用 HTML 第一段的格式覆盖上面设好的段落格式（第一条“你 · 时间”变成左对齐），再补设一次
        head = QTextCursor(doc)
        head.setPosition(start)
        head.mergeBlockFormat(fmt)

    def _append_item(self, item: ChatItem) -> None:
        """在文档末尾追加一条消息（item 必须是 self._items 的最后一条）。"""
        if not self._rendered_any:  # 正显示欢迎语
            self.chat.clear()
            self._rendered_any = True
        cur = QTextCursor(self.chat.document())
        cur.movePosition(QTextCursor.MoveOperation.End)
        self._tail_pos = cur.position()
        self._insert_item(cur, item, len(self._items) - 1)

    def _render_all(self) -> None:
        """整篇重画（主题变化、展开思考过程、清空等）。保持滚动位置。"""
        sb = self.chat.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4
        pos = sb.value()
        self.chat.clear()
        self._rendered_any = False
        self._tail_pos = 0
        if not self._items:
            self.chat.setHtml(self._welcome_html())
            return
        self._rendered_any = True
        doc = self.chat.document()
        for i, item in enumerate(self._items):
            cur = QTextCursor(doc)
            cur.movePosition(QTextCursor.MoveOperation.End)
            self._tail_pos = cur.position()
            self._insert_item(cur, item, i)
        if at_bottom:
            self._scroll_to_bottom()
        else:
            sb.setValue(pos)

    def _refresh_tail(self) -> None:
        """只替换最后一条消息（流式输出时），前面的消息不重排。"""
        if not self._items or not self._rendered_any:
            self._render_all()
            return
        sb = self.chat.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 24
        pos = sb.value()
        doc = self.chat.document()
        cur = QTextCursor(doc)
        cur.setPosition(min(self._tail_pos, max(0, doc.characterCount() - 1)))
        cur.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        cur.removeSelectedText()
        self._insert_item(cur, self._items[-1], len(self._items) - 1)
        if at_bottom:
            self._scroll_to_bottom()
        else:
            sb.setValue(pos)

    def _scroll_to_bottom(self) -> None:
        sb = self.chat.verticalScrollBar()
        sb.setValue(sb.maximum())

    def apply_theme(self) -> None:
        self._update_key_ui()
        self._render_all()

    # ------------------------------------------------------------ 链接与复制
    @Slot(QUrl)
    def _on_anchor(self, url: QUrl) -> None:
        if url.scheme() == "kmcf":
            what, _sep, arg = url.path().partition("/")
            try:
                idx = int(arg)
            except ValueError:
                return
            if not (0 <= idx < len(self._items)):
                return
            item = self._items[idx]
            if what == LINK_THINK:
                item.show_reasoning = not item.show_reasoning
                if idx == len(self._items) - 1:
                    self._refresh_tail()
                else:
                    self._render_all()
            elif what == LINK_RETRY and not self.is_busy():
                user = self._items[idx - 1] if idx > 0 and self._items[idx - 1].kind == "user" else None
                self.send(item.question, label=user.text if user else None)
            return
        if url.scheme().lower() in ("http", "https") and self.confirm_open_url(url):
            QDesktopServices.openUrl(url)

    def confirm_open_url(self, url: QUrl) -> bool:
        """AI 回复里的链接可能是编造的或不安全的：显示完整网址，确认后才打开（默认“否”）。"""
        ans = QMessageBox.question(
            self, "打开链接",
            f"即将用浏览器打开 AI 回复中的链接：\n\n{url.toString()}\n\n"
            "链接由 AI 生成，可能不准确或不安全，请确认网址可信后再打开。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        return ans == QMessageBox.StandardButton.Yes

    def last_answer(self) -> str:
        for item in reversed(self._items):
            if item.kind == "ai" and item.text.strip():
                return item.text
        return ""

    def conversation_text(self) -> str:
        out = []
        for item in self._items:
            if item.kind == "user":
                out.append(f"你（{item.time}）：\n{item.text}")
            elif item.kind == "note":
                out.append(f"—— {item.text} ——")
            elif item.text.strip():
                warn = f"\n（{COMPLIANCE_WARN.format(terms='、'.join(item.flags[:6]))}）" if item.flags else ""
                out.append(f"{item.provider}（{item.model}，{item.time}）：\n{plain_text(item.text)}{warn}\n（{DISCLAIMER}）")
            elif item.error:
                out.append(f"{item.provider}：请求失败：{item.error}")
        return "\n\n".join(out)

    def _chat_menu(self, pos) -> None:
        menu: QMenu = self.chat.createStandardContextMenu(pos)
        menu.addSeparator()
        last = self.last_answer()
        a = menu.addAction("复制最后一条 AI 回复", lambda: QGuiApplication.clipboard().setText(
            f"{plain_text(last)}\n（{DISCLAIMER}）"))
        a.setEnabled(bool(last))
        a = menu.addAction("复制全部对话", lambda: QGuiApplication.clipboard().setText(self.conversation_text()))
        a.setEnabled(bool(self._items))
        menu.exec(self.chat.viewport().mapToGlobal(pos))
        menu.deleteLater()

    # ------------------------------------------------------------ 设置
    def open_settings(self) -> None:
        from .ai_settings_dialog import AISettingsDialog

        dlg = AISettingsDialog(self.window(), self._ai, self._load_cfg())
        try:
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self.reload_config(reset_holdings=True)
                msg = "AI 设置已保存"
                if dlg.backup_path is not None:
                    msg += f"；损坏的原配置文件已备份为 {dlg.backup_path.name}"
                self.statusMessage.emit(msg)
        finally:
            dlg.deleteLater()


class AIDock(QDockWidget):
    """可停靠（右侧）、可拖动调整宽度、可浮动的「AI 分析」面板外壳。关闭时取消正在进行的请求。"""

    def __init__(self, panel: AIPanel, parent=None):
        super().__init__("AI 分析", parent)
        self.setObjectName("AIDock")
        self.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea)
        self.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable
                         | QDockWidget.DockWidgetFeature.DockWidgetMovable
                         | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.panel = panel
        self.setWidget(panel)
        self.setMinimumWidth(340)

    def closeEvent(self, event) -> None:
        self.panel.stop()
        super().closeEvent(event)
