"""主窗口：左侧边栏（生成、AI 分析、历史复盘、自选股、自动复盘）+ 中间报告 + 右侧可停靠的 AI 分析面板。"""

import datetime as dt
import logging
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QFileSystemWatcher, QSettings, Qt, QTimer, QUrl, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProgressBar,
                               QPushButton, QVBoxLayout, QWidget)

from .. import paths
from . import logic, theme
from .icon import paint_icon, sparkle_icon
from .report_view import ReportView
from .tasks import Cancelled, GenerateTask, Task

log = logging.getLogger("kmcf")

APP_TITLE = "kmcf · A股每日复盘"
DATE_ROLE = Qt.ItemDataRole.UserRole
PATH_ROLE = Qt.ItemDataRole.UserRole + 1

AUTO_REFRESH_MS = 5 * 60 * 1000  # 软件长时间开着：每 5 分钟重新计算“最近收盘交易日”（跨日、收盘后会变）
ACTIVATE_REFRESH_S = 120         # 窗口重新激活时，距上次刷新超过 2 分钟就刷新
SCHED_REFRESH_S = 30 * 60        # 自动复盘状态至少 30 分钟刷新一次；过了“下次运行”时间也会刷新
SCHED_ACTIVATE_S = 10 * 60       # 窗口激活时，自动复盘状态超过 10 分钟没刷新就刷新
QUIT_GRACE_MS = 120_000          # 生成中退出：最多再等 2 分钟让当前模块结束
AI_DOCK_WIDTH = 420              # AI 分析面板默认宽度（可拖动调整，记住上次的宽度）
REPORT_MIN_WIDTH = 360           # 报告区最小宽度（AI 面板打开、窗口变窄时优先保证报告可读）
SIDEBAR_WIDTH = 248
WINDOW_MIN_SIZE = (900, 560)


# ---------------------------------------------------------------- 默认实现（测试时可替换）

def _default_generate(date=None, force=False, progress=None):
    from .. import report
    return report.generate(date, force=force, progress=progress)


def _default_list_reports():
    from .. import report
    return report.list_reports()


def _latest_trade_day() -> dt.date:
    from .. import trade_calendar as tc
    return tc.latest_completed_trade_day()


_CAL_RETRY_S = 30 * 60
_cal_checked_at: float | None = None


def _calendar_state() -> bool:
    """预加载交易日历并返回是否为“按工作日估算”的日历。

    日历缓存过期时会联网，所以只在工作线程里调用；日期对话框随后在界面线程读内存里的日历。
    之前获取失败（估算日历）时，每 30 分钟重试一次。
    """
    global _cal_checked_at
    from .. import trade_calendar as tc
    tc.trade_days()
    now = time.monotonic()
    if _cal_checked_at is None:
        _cal_checked_at = now
    elif tc.approximate and now - _cal_checked_at > _CAL_RETRY_S:
        _cal_checked_at = now
        tc.trade_days(refresh=True)
    return bool(tc.approximate)


def _section_titles() -> list[str]:
    try:
        from ..sections import SECTIONS
        return [m.TITLE for m in SECTIONS]
    except Exception:  # 模块开发中出错时不影响界面
        return []


def reveal_in_folder(path: Path) -> None:
    path = Path(path)
    if sys.platform == "win32" and path.exists():
        subprocess.Popen(["explorer", "/select,", str(path)])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))


class MainWindow(QMainWindow):
    # powershell 卡住时的看门狗（毫秒）：读取状态 / 注册或删除任务
    SCHED_TIMEOUT_MS = {"status": 30_000, "change": 60_000}

    def __init__(self, settings: QSettings | None = None, generate_fn=None, scheduler=None,
                 list_reports_fn=None, latest_fn=None, calendar_fn=None, ai_module=None):
        super().__init__()
        self.settings = settings if settings is not None else QSettings()
        self._generate_fn = generate_fn or _default_generate
        if scheduler is None:
            from .. import scheduler as scheduler_mod
            scheduler = scheduler_mod
        self._scheduler = scheduler
        self._list_reports_fn = list_reports_fn or _default_list_reports
        self._latest_fn = latest_fn or _latest_trade_day
        self._calendar_fn = calendar_fn or _calendar_state

        self._reports: list[tuple[dt.date, Path]] = []
        self._started = False
        # 最近收盘交易日
        self._latest: dt.date | None = None
        self._latest_approx = False
        self._latest_failed = False
        self._latest_task: Task | None = None
        self._latest_waiters: list = []
        self._latest_announce = False
        self._date_waiting = False
        self._last_auto_refresh = 0.0
        # 生成与退出
        self._gen_task: GenerateTask | None = None
        self._quit_pending = False
        self._quitting = False
        # 自动复盘
        self._sched_status: dict | None = None
        self._sched_busy = False
        self._sched_kind = "status"
        self._sched_seq = 0
        self._last_sched_refresh = 0.0
        self._pending_install: str | None = None
        self._sched_cfg_time = self._config_time()
        # 配置文件
        self._config_issue: str | None = None
        self._section_titles = _section_titles()
        # AI 分析面板（第一次打开时才创建、才导入 kmcf.ai）
        self._ai_module = ai_module
        self._ai_dock = None
        self._ai_panel = None
        self._ai_hidden_for_quit = False

        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(*WINDOW_MIN_SIZE)
        self._build_ui()
        self._build_timers()
        self._restore_settings()

    # ================================================================ 界面
    def _build_ui(self) -> None:
        central = QWidget()
        h = QHBoxLayout(central)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        h.addWidget(self._build_sidebar())
        self.view = ReportView(self)
        self.view.setMinimumWidth(REPORT_MIN_WIDTH)  # 打开 AI 面板后窗口变窄时，先压缩面板，报告区至少留这么宽
        self.view.actionRequested.connect(self._on_page_action)
        self.view.zoomChanged.connect(lambda z: self.settings.setValue("view/zoom", z))
        h.addWidget(self.view, 1)
        self.setCentralWidget(central)

        sb = self.statusBar()
        sb.setSizeGripEnabled(True)
        self.lbl_status = QLabel("就绪")
        self.lbl_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        sb.addWidget(self.lbl_status, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(220)
        self.progress.setVisible(False)
        sb.addPermanentWidget(self.progress)

        QShortcut(QKeySequence("F5"), self, activated=self.generate_latest)
        QShortcut(QKeySequence("Ctrl+K"), self, activated=lambda: self.toggle_ai_panel())
        self.view.reportChanged.connect(self._on_report_changed)

        # 定时任务（或命令行）在后台生成了新报告时，自动刷新列表
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(lambda _p: self._dir_timer.start())
        self._ensure_watch()

    def _build_timers(self) -> None:
        self._dir_timer = QTimer(self)
        self._dir_timer.setSingleShot(True)
        self._dir_timer.setInterval(1500)
        self._dir_timer.timeout.connect(self._rescan_reports)

        self._auto_timer = QTimer(self)  # 软件长时间开着：定时刷新最近交易日和自动复盘状态
        self._auto_timer.setInterval(AUTO_REFRESH_MS)
        self._auto_timer.timeout.connect(lambda: self._auto_refresh())

        self._sched_watchdog = QTimer(self)
        self._sched_watchdog.setSingleShot(True)
        self._sched_watchdog.timeout.connect(self._on_sched_timeout)

        self._quit_timer = QTimer(self)  # 生成中退出的保险：最多再等 2 分钟
        self._quit_timer.setSingleShot(True)
        self._quit_timer.setInterval(QUIT_GRACE_MS)
        self._quit_timer.timeout.connect(self._finish_quit)

    def _build_sidebar(self) -> QWidget:
        side = QFrame()
        side.setObjectName("Sidebar")
        side.setFixedWidth(SIDEBAR_WIDTH)
        v = QVBoxLayout(side)
        v.setContentsMargins(16, 18, 16, 12)
        v.setSpacing(8)

        # 标题
        head = QHBoxLayout()
        head.setSpacing(10)
        self.logo = QLabel()
        self._paint_logo()
        head.addWidget(self.logo)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        t1 = QLabel("kmcf")
        t1.setObjectName("AppTitle")
        t2 = QLabel("A股每日复盘")
        t2.setObjectName("AppSubtitle")
        titles.addWidget(t1)
        titles.addWidget(t2)
        head.addLayout(titles, 1)
        v.addLayout(head)
        v.addSpacing(10)

        # 生成
        self.btn_generate = QPushButton("生成最新复盘")
        self.btn_generate.setObjectName("Primary")
        self.btn_generate.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_generate.setToolTip("生成最近一个已收盘交易日的复盘报告（F5）")
        self.btn_generate.clicked.connect(self.generate_latest)
        v.addWidget(self.btn_generate)
        self.lbl_latest = QLabel("正在读取交易日历…")
        self.lbl_latest.setObjectName("Caption")
        self.lbl_latest.setWordWrap(True)
        v.addWidget(self.lbl_latest)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.btn_date = QPushButton("复盘指定日期…")
        self.btn_date.setToolTip("选择一个已收盘的交易日生成复盘")
        self.btn_date.clicked.connect(self.pick_date)
        self.btn_refetch = QPushButton("重新获取数据")
        self.btn_refetch.setToolTip("忽略已缓存的数据，重新联网获取并生成当前查看的这一天的复盘")
        self.btn_refetch.clicked.connect(lambda: self.refetch())
        for b in (self.btn_date, self.btn_refetch):
            b.setStyleSheet("padding: 6px 4px;")
            row.addWidget(b, 1)
        v.addLayout(row)

        # AI 分析：打开 / 关闭右侧的对话面板
        self.btn_ai = QPushButton("AI 分析")
        self.btn_ai.setObjectName("AIButton")
        self.btn_ai.setCheckable(True)
        self.btn_ai.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_ai.setToolTip("打开 / 关闭右侧的 AI 分析面板，基于当前报告向 AI 提问（Ctrl+K）\n"
                               "AI 只做客观数据解读，仅供参考，不构成投资建议")
        self.btn_ai.clicked.connect(self.toggle_ai_panel)
        self._paint_ai_icon()
        v.addWidget(self.btn_ai)
        v.addSpacing(8)

        # 历史复盘
        hist_head = QHBoxLayout()
        lbl = QLabel("历史复盘")
        lbl.setObjectName("SectionLabel")
        hist_head.addWidget(lbl)
        hist_head.addStretch(1)
        self.lbl_count = QLabel("")
        self.lbl_count.setObjectName("Caption")
        hist_head.addWidget(self.lbl_count)
        v.addLayout(hist_head)

        self.list = QListWidget()
        self.list.setObjectName("History")
        self.list.setUniformItemSizes(True)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._list_menu)
        self.list.currentItemChanged.connect(self._on_current_changed)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self.list,
                  activated=self._delete_current, context=Qt.ShortcutContext.WidgetShortcut)
        v.addWidget(self.list, 1)
        self.lbl_empty = QLabel("还没有复盘报告，点击上方「生成最新复盘」")
        self.lbl_empty.setObjectName("Caption")
        self.lbl_empty.setWordWrap(True)
        self.lbl_empty.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.lbl_empty.setContentsMargins(10, 6, 10, 0)
        self.lbl_empty.setVisible(False)
        v.addWidget(self.lbl_empty, 1)

        self.lbl_config_warn = QLabel("配置文件无法读取，当前使用默认自选股")
        self.lbl_config_warn.setObjectName("WarnText")
        self.lbl_config_warn.setWordWrap(True)
        self.lbl_config_warn.setVisible(False)
        v.addWidget(self.lbl_config_warn)
        self.btn_watchlist = QPushButton("自选股 / 持仓…")
        self.btn_watchlist.setToolTip("编辑自选股和持仓（代码、持仓股数、成本价）")
        self.btn_watchlist.clicked.connect(self.open_watchlist)
        v.addWidget(self.btn_watchlist)
        v.addSpacing(4)
        v.addWidget(self._build_schedule_panel())
        return side

    def _build_schedule_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("Panel")
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(12, 10, 12, 10)
        pv.setSpacing(4)
        top = QHBoxLayout()
        lbl = QLabel("自动复盘")
        lbl.setObjectName("SectionLabel")
        top.addWidget(lbl)
        top.addStretch(1)
        self.lbl_sched_dot = QLabel("●")
        top.addWidget(self.lbl_sched_dot)
        pv.addLayout(top)

        self.lbl_sched = QLabel("正在读取状态…")
        self.lbl_sched.setStyleSheet("font-weight: 600;")
        self.lbl_sched.setWordWrap(True)
        pv.addWidget(self.lbl_sched)
        self.lbl_sched_detail = QLabel("")
        self.lbl_sched_detail.setObjectName("Caption")
        self.lbl_sched_detail.setWordWrap(True)
        pv.addWidget(self.lbl_sched_detail)

        # 时间固定显示在状态里（默认每个工作日 19:00），修改时间是次要操作，放在「修改时间…」对话框里
        row = QHBoxLayout()
        row.setSpacing(6)
        row.setContentsMargins(0, 4, 0, 0)
        self.btn_sched = QPushButton("开启")
        self.btn_sched.clicked.connect(self.toggle_schedule)
        row.addWidget(self.btn_sched, 1)
        self.btn_sched_time = QPushButton("修改时间…")
        self.btn_sched_time.setObjectName("Link")
        self.btn_sched_time.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_sched_time.setToolTip("修改每个工作日自动复盘的时间（默认 19:00）")
        self.btn_sched_time.clicked.connect(self.edit_schedule_time)
        row.addWidget(self.btn_sched_time)
        pv.addLayout(row)
        self._update_schedule_ui()
        return panel

    def _paint_logo(self) -> None:
        dpr = self.devicePixelRatioF() or 1.0
        pm = paint_icon(int(36 * dpr))
        pm.setDevicePixelRatio(dpr)
        self.logo.setPixmap(pm)
        self.logo.setFixedSize(36, 36)

    def _paint_ai_icon(self) -> None:
        self.btn_ai.setIcon(sparkle_icon(theme.current()["accent_text"], size=16))

    def on_theme_changed(self) -> None:
        self.view.apply_theme(theme.current())
        self._update_schedule_ui()
        self._paint_ai_icon()
        if self._ai_panel is not None:
            self._ai_panel.apply_theme()

    def _update_action_buttons(self) -> None:
        gen = self._gen_task is not None
        self.btn_generate.setEnabled(not gen)
        self.btn_date.setEnabled(not gen and not self._date_waiting)
        self.btn_refetch.setEnabled(not gen)

    # ================================================================ 设置
    def _restore_settings(self) -> None:
        geo = self.settings.value("window/geometry")
        restored = False
        if geo is not None:
            try:
                restored = self.restoreGeometry(geo)
            except TypeError:
                restored = False
        if not restored:
            screen = QGuiApplication.primaryScreen()
            avail = screen.availableGeometry() if screen else None
            w, h = 1280, 820
            if avail is not None:
                w, h = min(w, int(avail.width() * 0.95)), min(h, int(avail.height() * 0.92))
                self.resize(w, h)
                self.move(avail.center().x() - w // 2, avail.center().y() - h // 2)
            else:
                self.resize(w, h)
        try:
            zoom = float(self.settings.value("view/zoom", 1.0))
        except (TypeError, ValueError):
            zoom = 1.0
        if abs(zoom - 1.0) > 1e-3:
            self.view.set_zoom(zoom)

    def _save_settings(self) -> None:
        self.settings.setValue("window/geometry", self.saveGeometry())
        dock = self._ai_dock
        self.settings.setValue("ai/open", bool(dock is not None and not dock.isHidden()))
        if dock is not None and not dock.isHidden() and not dock.isFloating():
            self.settings.setValue("ai/width", dock.width())
        self.settings.sync()

    @staticmethod
    def _config_time() -> str:
        try:
            from ..config import load_config
            t = load_config().get("schedule_time")
        except Exception:
            t = None
        return logic.valid_time(t) or logic.DEFAULT_SCHEDULE_TIME

    def _check_config(self) -> str | None:
        """检查 data/config.json 能否解析；损坏时在侧栏提示（load_config 会静默改用默认配置）。"""
        self._config_issue = logic.config_problem()
        self.lbl_config_warn.setVisible(bool(self._config_issue))
        if self._config_issue:
            self.lbl_config_warn.setToolTip(f"{paths.CONFIG_PATH}\n{self._config_issue}\n\n"
                                            "生成的报告会使用默认自选股。打开「自选股/持仓…」重新保存即可修复（原文件会先备份）。")
        return self._config_issue

    # ================================================================ 启动与定时刷新
    def start(self) -> None:
        """窗口显示后调用：加载报告列表、读取交易日历和自动复盘状态（后两者在后台线程）。"""
        self._started = True
        self._check_config()
        self.refresh_reports()
        self.refresh_latest(announce=True)
        self.refresh_schedule()
        self._last_auto_refresh = time.monotonic()
        self._auto_timer.start()
        if self._config_issue:
            self._set_status("配置文件无法读取，报告会使用默认自选股：请打开「自选股/持仓…」检查")
        if str(self.settings.value("ai/open", "false")).lower() in ("true", "1"):  # 上次退出时面板是打开的
            QTimer.singleShot(0, lambda: self.toggle_ai_panel(True))

    def _auto_refresh(self, activated: bool = False) -> None:
        """定时（或窗口重新激活时）刷新：最近收盘交易日、自动复盘状态、配置文件状态。"""
        self._last_auto_refresh = time.monotonic()
        self._check_config()
        self.refresh_latest()
        if self._schedule_due(SCHED_ACTIVATE_S if activated else SCHED_REFRESH_S):
            self.refresh_schedule()

    def _schedule_due(self, max_age_s: float) -> bool:
        if time.monotonic() - self._last_sched_refresh > max_age_s:
            return True
        st = self._sched_status or {}
        if st.get("installed") and st.get("next_run"):  # 过了“下次运行”时间：任务应该已经跑过，刷新“上次运行”
            try:
                nxt = dt.datetime.strptime(str(st["next_run"]), "%Y-%m-%d %H:%M")
            except ValueError:
                return False
            return dt.datetime.now() >= nxt + dt.timedelta(minutes=1)
        return False

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if (event.type() == QEvent.Type.ActivationChange and getattr(self, "_started", False) and self.isActiveWindow()
                and time.monotonic() - self._last_auto_refresh > ACTIVATE_REFRESH_S):
            self._auto_refresh(activated=True)

    def refresh_latest(self, then=None, announce: bool = False) -> None:
        """在工作线程重新计算最近一个已收盘交易日。then(ok, error) 在完成后（界面线程）调用。"""
        if then is not None:
            self._latest_waiters.append(then)
        if announce:
            self._latest_announce = True
        if self._latest_task is not None:  # 已经在算，结果出来后一起通知
            return
        self._latest_task = Task(self._latest_info, on_done=self._on_latest_info,
                                 on_error=self._on_latest_failed, name="latest_day")
        self._latest_task.start()

    def _latest_info(self) -> tuple[dt.date, bool]:  # 工作线程
        approx = bool(self._calendar_fn())
        return self._latest_fn(), approx

    def _on_latest_info(self, res) -> None:
        self._latest_task = None
        d, approx = res
        self._on_latest(d, approx)
        self._notify_latest_waiters(True, None)

    def _on_latest(self, d: dt.date, approximate: bool = False) -> None:
        changed = d != self._latest or approximate != self._latest_approx
        self._latest, self._latest_approx, self._latest_failed = d, approximate, False
        self._update_latest_caption()
        if (changed or self._latest_announce) and self._gen_task is None and not self._config_issue:
            hint = logic.latest_hint(d, {x for x, _p in self._reports}, approximate)
            if hint:
                self._set_status(hint)
        self._latest_announce = False

    def _on_latest_failed(self, exc: BaseException) -> None:
        self._latest_task = None
        self._latest_failed = True
        self._latest_announce = False
        self._update_latest_caption()
        self._notify_latest_waiters(False, exc)

    def _notify_latest_waiters(self, ok: bool, err) -> None:
        waiters, self._latest_waiters = self._latest_waiters, []
        for cb in waiters:  # 放到下一轮事件循环：回调里可能打开模态对话框
            QTimer.singleShot(0, lambda cb=cb: cb(ok, err))

    def _update_latest_caption(self) -> None:
        self.lbl_latest.setText(logic.latest_caption(self._latest, {d for d, _p in self._reports},
                                                     self._latest_failed, self._latest_approx))
        self.lbl_latest.setToolTip(logic.APPROX_CALENDAR_TIP if self._latest_approx else "")

    def _set_status(self, text: str) -> None:
        self.lbl_status.setText(text)

    # ================================================================ 报告列表
    def _ensure_watch(self) -> None:
        """监视 reports 目录（启动时目录可能还不存在，或被删除后重建）。"""
        d = str(paths.REPORT_DIR)
        if paths.REPORT_DIR.exists() and d not in self._watcher.directories():
            self._watcher.addPath(d)

    def refresh_reports(self, select: dt.date | None = None, load: bool = True) -> None:
        self._ensure_watch()
        try:
            self._reports = list(self._list_reports_fn())
        except Exception as e:
            log.warning("读取报告列表失败：%s", e)
            self._reports = []
        keep = select or self.current_date()
        self.list.blockSignals(True)
        self.list.clear()
        row = 0
        for i, (d, p) in enumerate(self._reports):
            it = QListWidgetItem(logic.report_label(d))
            it.setData(DATE_ROLE, d.isoformat())
            it.setData(PATH_ROLE, str(p))
            it.setToolTip(str(p))
            self.list.addItem(it)
            if d == keep:
                row = i
        if self._reports:
            self.list.setCurrentRow(row)
        self.list.blockSignals(False)
        n = len(self._reports)
        self.lbl_count.setText(f"共 {n} 份" if n else "")
        self.lbl_empty.setVisible(n == 0)
        self.list.setVisible(n > 0)
        self._update_action_buttons()
        self._update_latest_caption()
        if load:
            if self._reports:
                self.view.show_report(self._reports[row][1])
            else:
                self.view.show_empty(self._section_titles)

    def _rescan_reports(self) -> None:
        if self._gen_task is not None:  # 自己正在生成，完成后会刷新
            return
        before = {d for d, _p in self._reports}
        cur = self.view.current_path
        self.refresh_reports(load=cur is None or not Path(cur).exists())
        new = sorted({d for d, _p in self._reports} - before)
        if new:
            self._set_status(f"发现新的复盘报告：{logic.report_label(new[-1])}，可在左侧列表中查看")
            # 多半是 19:00 的自动复盘跑完了：顺便刷新最近交易日和“上次运行”
            self.refresh_latest()
            self.refresh_schedule()

    def current_date(self) -> dt.date | None:
        p = self.view.current_path
        if p is None:
            return None
        try:
            return dt.date.fromisoformat(Path(p).stem)
        except ValueError:
            return None

    @staticmethod
    def _item_info(it: QListWidgetItem | None) -> tuple[dt.date, Path] | None:
        if it is None:
            return None
        return dt.date.fromisoformat(it.data(DATE_ROLE)), Path(it.data(PATH_ROLE))

    @Slot(QListWidgetItem, QListWidgetItem)
    def _on_current_changed(self, cur: QListWidgetItem | None, _prev=None) -> None:
        info = self._item_info(cur)
        if not info:
            return
        d, path = info
        if not path.exists():  # 在外部被删除了，目录监视还没来得及刷新
            self._set_status(f"{logic.report_label(d)} 的报告文件已不存在（可能已在外部删除），列表已刷新")
            QTimer.singleShot(0, lambda: self.refresh_reports())
            return
        self.view.show_report(path)

    def _list_menu(self, pos) -> None:
        it = self.list.itemAt(pos)
        info = self._item_info(it)
        if not info:
            return
        d, path = info
        busy = self._gen_task is not None
        menu = QMenu(self)
        menu.addAction("在浏览器中打开", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))))
        menu.addAction("打开所在文件夹", lambda: reveal_in_folder(path))
        menu.addSeparator()
        a = menu.addAction("重新获取数据并生成", lambda: self.refetch(d))
        a.setEnabled(not busy)
        menu.addSeparator()
        a = menu.addAction("删除该报告", lambda: self.delete_report(d, path))
        a.setEnabled(not busy)
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def _delete_current(self) -> None:
        info = self._item_info(self.list.currentItem())
        if info and self._gen_task is None:
            self.delete_report(*info)

    def delete_report(self, d: dt.date, path: Path) -> bool:
        label = logic.report_label(d)
        ans = QMessageBox.question(
            self, "删除报告",
            f"确定删除 {label} 的复盘报告吗？\n\n只删除报告文件（.html 和 .json），已缓存的行情数据会保留，之后可以重新生成。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return False
        try:
            logic.delete_report(path)
        except Exception as e:
            QMessageBox.warning(self, "删除失败", f"无法删除报告文件：{e}")
            return False
        if self.view.current_path is not None and Path(self.view.current_path) == Path(path):
            self.view.current_path = None
            keep = None
        else:
            keep = self.current_date()
        self.refresh_reports(select=keep)
        self._set_status(f"已删除 {label} 的复盘报告")
        return True

    @Slot(str)
    def _on_page_action(self, action: str) -> None:
        if action == "generate":
            self.generate_latest()
        elif action == "watchlist":
            self.open_watchlist()

    # ================================================================ 生成
    def generate_latest(self) -> None:
        self.start_generate(None, False)

    def pick_date(self) -> None:
        """先在工作线程重新计算最近收盘交易日（并预加载日历），再打开日期对话框，界面不会卡住。"""
        if self._gen_task is not None or self._date_waiting:
            return
        self._date_waiting = True
        self.btn_date.setText("读取交易日历…")
        self._update_action_buttons()
        self.refresh_latest(then=self._open_date_dialog)

    def _open_date_dialog(self, ok: bool, err=None) -> None:
        from .date_dialog import DateDialog

        self._date_waiting = False
        self.btn_date.setText("复盘指定日期…")
        self._update_action_buttons()
        if self._gen_task is not None or self._quit_pending:
            return
        if not ok or self._latest is None:
            QMessageBox.warning(self, "无法读取交易日历", f"交易日历读取失败：{err}\n\n请检查网络后重试。")
            return
        dlg = DateDialog(self, self._latest, {d for d, _p in self._reports}, initial=self.current_date(),
                         approximate=self._latest_approx)
        try:
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self.start_generate(dlg.selected_date(), dlg.force())
        finally:
            dlg.deleteLater()

    def refetch(self, d: dt.date | None = None) -> None:
        if self._gen_task is not None:
            return
        d = d or self.current_date() or self._latest
        label = logic.report_label(d) if d else "最近一个交易日"
        ans = QMessageBox.question(
            self, "重新获取数据",
            f"重新联网获取 {label} 的数据并生成复盘？\n\n会忽略已缓存的数据，耗时较长。"
            "历史日期的实时快照类数据（涨跌分布、板块实时排行等）仍只能使用当天的缓存。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes)
        if ans == QMessageBox.StandardButton.Yes:
            self.start_generate(d, True)

    def start_generate(self, date: dt.date | None = None, force: bool = False) -> bool:
        if self._gen_task is not None:
            return False
        task = GenerateTask(self._generate_fn, date=date, force=force,
                            on_done=self._on_generated, on_error=self._on_generate_failed)
        task.progress.connect(self._on_progress)
        self._gen_task = task
        self._set_busy(True)
        what = logic.report_label(date) if date else "最新"
        self._set_status(f"开始生成{what}复盘" + ("（重新获取数据）" if force else "") + "…")
        task.start()
        return True

    def is_generating(self) -> bool:
        return self._gen_task is not None

    def _set_busy(self, busy: bool) -> None:
        self.btn_generate.setText("正在生成…" if busy else "生成最新复盘")
        self._update_action_buttons()
        self.progress.setVisible(busy)
        self.progress.setValue(0)

    @Slot(int, str)
    def _on_progress(self, pct: int, msg: str) -> None:
        if self._gen_task is None or self._gen_task.cancelled:
            return
        self.progress.setValue(max(0, min(100, pct)))
        self.btn_generate.setText(f"正在生成… {pct}%")
        self._set_status(msg)

    def _on_generated(self, path) -> None:
        task, self._gen_task = self._gen_task, None
        self._set_busy(False)
        if self._quit_pending:
            self._finish_quit()
            return
        path = Path(path)
        try:
            d = dt.date.fromisoformat(path.stem)
        except ValueError:
            d = None
        self.refresh_reports(select=d, load=False)
        self.view.show_report(path, force_reload=True)
        elapsed = f"（用时 {task.elapsed():.0f} 秒）" if task is not None else ""
        self._set_status(f"复盘报告已生成：{logic.report_label(d) if d else path.name}{elapsed}")
        self.refresh_latest()  # 软件开着跨了交易日时，侧栏的“最近收盘”随之更新

    def _on_generate_failed(self, exc: BaseException) -> None:
        self._gen_task = None
        self._set_busy(False)
        if self._quit_pending:
            self._finish_quit()
            return
        if isinstance(exc, Cancelled):
            self._set_status("已取消生成")
            return
        title, text = logic.friendly_error(exc)
        self._set_status(title + "：" + (text.splitlines() or [""])[0])
        QMessageBox.warning(self, title, text)

    # ================================================================ 自选股
    def open_watchlist(self) -> None:
        from ..config import load_config
        from .watchlist_dialog import WatchlistDialog

        notice = None
        if self._check_config():
            QMessageBox.warning(
                self, "配置文件已损坏",
                f"配置文件无法读取：\n{paths.CONFIG_PATH}\n（{self._config_issue}）\n\n"
                "接下来显示的是默认示例，不是你原来的自选股。\n"
                "点「保存」会先把原文件改名备份（config.json.bad-日期时间），再写入新的自选股；"
                "也可以点「取消」，先用记事本修正原文件。")
            notice = "注意：配置文件无法读取，下面是默认示例；保存时会先备份原文件"
        try:
            items = load_config().get("watchlist", [])
        except Exception as e:
            QMessageBox.warning(self, "读取配置失败", f"配置文件读取失败：{e}")
            return
        backups: list[Path] = []

        def save_fn(new_items):
            b = logic.backup_corrupt_config()
            if b:
                backups.append(b)
            return logic.save_watchlist(new_items)

        dlg = WatchlistDialog(self, items, save_fn=save_fn, notice=notice)
        try:
            if dlg.exec() == QDialog.DialogCode.Accepted:
                n = len(dlg.saved_items or [])
                msg = f"自选股已保存（{n} 只），下次生成复盘时生效"
                if backups:
                    msg += f"；损坏的原配置文件已备份为 {backups[0].name}"
                self._set_status(msg)
        finally:
            dlg.deleteLater()
            self._check_config()

    # ================================================================ 自动复盘
    def _sched_installed(self) -> bool:
        return bool(self._sched_status and self._sched_status.get("installed"))

    def refresh_schedule(self) -> None:
        if self._sched_busy:
            return
        self._last_sched_refresh = time.monotonic()
        self._start_sched(self._scheduler.status, (), "status")

    def toggle_schedule(self) -> None:
        if self._sched_busy:
            return
        if self._sched_installed():
            self._start_sched(self._scheduler.uninstall, (), "change")
        else:
            self._sched_cfg_time = self._config_time()  # 默认 19:00
            self._start_sched(self._scheduler.install, (self._sched_cfg_time,), "change")

    def _start_sched(self, fn, args: tuple, kind: str) -> None:
        """在工作线程调用 scheduler（powershell，1~3 秒）；看门狗防止 powershell 卡住时这一块一直不可用。"""
        self._sched_seq += 1
        seq = self._sched_seq
        self._sched_busy = True
        self._sched_kind = kind
        self._update_schedule_ui()
        self._sched_watchdog.start(self.SCHED_TIMEOUT_MS[kind])
        if kind == "status":
            done, fail = self._on_sched_status, self._on_sched_status_error
        else:
            done, fail = self._on_sched_changed, self._on_sched_failed
        Task(fn, *args, on_done=lambda r, s=seq: done(r, s), on_error=lambda e, s=seq: fail(e, s),
             name=f"schedule_{getattr(fn, '__name__', kind)}").start()

    def _sched_finish(self, seq: int | None) -> bool:
        """结果属于当前操作时结束“处理中”状态并返回 True；看门狗超时后迟到的结果返回 False。"""
        if seq is None or seq == self._sched_seq:
            self._sched_watchdog.stop()
            self._sched_busy = False
            return True
        return False

    def _on_sched_status(self, st, seq: int | None = None) -> None:
        if not self._sched_finish(seq) and self._sched_busy:
            return  # 迟到的结果，而且已经有新的操作在进行：丢弃
        self._sched_status = st if isinstance(st, dict) else {"installed": False, "error": str(st)}
        self._update_schedule_ui()
        if self._pending_install and not self._sched_busy:
            t, self._pending_install = self._pending_install, None
            if self._sched_installed() and self._sched_status.get("time") != t:
                self._start_sched(self._scheduler.install, (t,), "change")

    def _on_sched_status_error(self, exc: BaseException, seq: int | None = None) -> None:
        self._on_sched_status({"installed": False, "error": str(exc)}, seq)

    def _on_sched_changed(self, msg, seq: int | None = None) -> None:
        self._sched_finish(seq)
        if msg:
            self._set_status(str(msg))
        self.refresh_schedule()

    def _on_sched_failed(self, exc: BaseException, seq: int | None = None) -> None:
        current = self._sched_finish(seq)
        self._pending_install = None
        if current:
            QMessageBox.warning(self, "自动复盘设置失败",
                                f"{exc}\n\n可以稍后重试，或在 Windows“任务计划程序”里查看名为 kmcf_daily_review 的任务。")
        else:
            log.warning("自动复盘设置失败（已超时）：%s", exc)
        self.refresh_schedule()

    def _on_sched_timeout(self) -> None:
        what = "读取任务计划" if self._sched_kind == "status" else "设置自动复盘"
        log.warning("%s超时（%s 秒没有响应）", what, self.SCHED_TIMEOUT_MS[self._sched_kind] // 1000)
        self._sched_seq += 1  # 之后迟到的结果按“过期”处理
        self._sched_busy = False
        self._pending_install = None
        self._sched_status = {"installed": False, "timeout": True,
                              "error": f"{what}超时（powershell 没有响应），可以稍后重试"}
        self._update_schedule_ui()
        self._set_status(f"自动复盘：{what}超时，可以稍后重试")

    def edit_schedule_time(self) -> None:
        from .schedule_dialog import ScheduleTimeDialog

        if self._sched_busy:
            return
        installed = self._sched_installed()
        cur = (self._sched_status or {}).get("time") if installed else None
        dlg = ScheduleTimeDialog(self, cur or self._config_time(), installed)
        try:
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            t = dlg.time_str()
        finally:
            dlg.deleteLater()
        self.apply_schedule_time(t)

    def apply_schedule_time(self, t: str) -> None:
        """保存自动复盘时间（「修改时间…」对话框点了保存之后）；任务已开启时重新注册。"""
        try:
            backup = logic.backup_corrupt_config()
            logic.save_schedule_time(t)
        except Exception as e:
            QMessageBox.warning(self, "保存失败", f"自动复盘时间保存失败：{e}")
            return
        self._sched_cfg_time = t
        note = f"（损坏的原配置文件已备份为 {backup.name}）" if backup else ""
        if backup:
            self._check_config()
        if not self._sched_installed():
            self._set_status(f"自动复盘时间已设为每个工作日 {t}，点击「开启」后生效{note}")
            self._update_schedule_ui()
            return
        if self._sched_status.get("time") == t:
            self._set_status(f"自动复盘时间没有变化：每个工作日 {t}{note}")
            return
        if self._sched_busy:  # 对话框打开期间开始了定时刷新：等它结束再注册
            self._pending_install = t
            return
        self._start_sched(self._scheduler.install, (t,), "change")

    def _update_schedule_ui(self) -> None:
        t = theme.current()
        loading = self._sched_status is None
        on, title, detail = logic.schedule_summary(None if loading else self._sched_status, self._sched_cfg_time)
        if self._sched_status and self._sched_status.get("timeout"):
            title, detail = "状态未知", str(self._sched_status.get("error", ""))
        if self._sched_busy and not loading:
            detail = "正在更新…"
        self.lbl_sched.setText(title)
        self.lbl_sched_detail.setText(detail)
        self.lbl_sched_detail.setVisible(bool(detail))
        err = bool(self._sched_status and self._sched_status.get("error") and not on)
        color = theme.PRIMARY if on else (t["warn"] if err else t["faint"])
        self.lbl_sched_dot.setStyleSheet(f"color: {color}; font-size: 10pt;")
        self.btn_sched.setText("处理中…" if self._sched_busy else ("关闭" if on else "开启"))
        self.btn_sched.setToolTip("关闭每个工作日的自动复盘" if on
                                  else f"每个工作日 {self._sched_cfg_time} 自动生成复盘")
        self.btn_sched.setEnabled(not self._sched_busy)
        self.btn_sched_time.setEnabled(not self._sched_busy)

    # ================================================================ AI 分析
    def toggle_ai_panel(self, show: bool | None = None) -> bool:
        """打开 / 关闭右侧的 AI 分析面板（侧栏按钮、Ctrl+K）。返回面板现在是否打开。"""
        dock = self._ai_dock
        if show is None:
            show = dock is None or dock.isHidden()
        if show:
            if dock is None and not self._create_ai_dock():
                self._sync_ai_button()
                return False
            self._ai_dock.show()
            self._ai_dock.raise_()
            self._ai_panel.reload_config()
            self._ai_panel.focus_input()
        elif dock is not None:
            dock.close()  # AIDock.closeEvent 会停止正在进行的回答
        self._sync_ai_button()
        return bool(self._ai_dock is not None and not self._ai_dock.isHidden())

    def _create_ai_dock(self) -> bool:
        try:
            from .ai_panel import AIDock, AIPanel
            panel = AIPanel(None, ai_module=self._ai_module)
        except Exception as e:  # noqa: BLE001 —— kmcf.ai 缺失或出错时不影响其他功能
            log.exception("AI 分析面板加载失败")
            QMessageBox.warning(self, "AI 分析",
                                f"AI 分析模块加载失败：{e}\n\n详细信息已写入日志文件：\n{logic.LOG_FILE}")
            return False
        panel.statusMessage.connect(self._set_status)
        panel.set_report(self.view.current_path)
        dock = AIDock(panel, self)
        dock.visibilityChanged.connect(lambda _v: self._sync_ai_button())
        dock.visibilityChanged.connect(lambda _v: self._fit_min_width())
        dock.topLevelChanged.connect(lambda _f: self._fit_min_width())
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self._ai_dock, self._ai_panel = dock, panel
        try:
            w = int(self.settings.value("ai/width", AI_DOCK_WIDTH))
        except (TypeError, ValueError):
            w = AI_DOCK_WIDTH
        low = dock.minimumWidth()
        w = max(low, min(w, max(low, self.width() - SIDEBAR_WIDTH - REPORT_MIN_WIDTH)))  # 报告区至少留 360 像素
        self.resizeDocks([dock], [w], Qt.Orientation.Horizontal)
        self._fit_min_width()
        return True

    def _fit_min_width(self) -> None:
        """AI 面板停靠在右侧时，窗口最小宽度 = 侧栏 + 报告区最小宽度 + 面板最小宽度（布局算出的最小尺寸），
        窗口再窄就会把内容挤出窗口；面板关闭或浮动时恢复原来的最小宽度。等布局更新后再算。"""
        QTimer.singleShot(0, self, self._apply_min_width)

    def _apply_min_width(self) -> None:
        self.setMinimumWidth(max(WINDOW_MIN_SIZE[0], self.minimumSizeHint().width()))

    def _sync_ai_button(self) -> None:
        dock = self._ai_dock
        self.btn_ai.setChecked(bool(dock is not None and not dock.isHidden()))

    def _on_report_changed(self, path) -> None:
        if self._ai_panel is not None:
            self._ai_panel.set_report(path)

    def _shutdown_ai(self) -> None:
        if self._ai_panel is not None:
            self._ai_panel.shutdown()

    # ================================================================ 退出
    def closeEvent(self, event) -> None:
        if self._quitting:
            event.accept()
            return
        if self._quit_pending:  # 已确认退出、正在等生成停止：只隐藏
            self.hide()
            event.ignore()
            return
        if self._gen_task is not None:
            ans = QMessageBox.question(
                self, "正在生成复盘",
                "复盘报告还在生成中，确定要退出吗？\n\n已经获取的数据会保留在缓存里，下次生成会更快。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            task = self._gen_task
            self._save_settings()
            if task is None:  # 对话框打开期间生成已经结束：直接退出
                event.accept()
                return
            self._quit_pending = True
            task.cancel()  # 在下一个进度点停止
            if self._ai_panel is not None:
                self._ai_panel.stop()
            if self._ai_dock is not None and not self._ai_dock.isHidden():
                self._ai_hidden_for_quit = True   # ai/open 已经在上面保存；浮动的面板不能留在屏幕上
                self._ai_dock.hide()
            QApplication.instance().setQuitOnLastWindowClosed(False)
            self.hide()
            event.ignore()
            self._quit_timer.start()  # 保险：最多再等 2 分钟
            return
        self._save_settings()
        self._shutdown_ai()
        event.accept()

    def request_show(self) -> None:
        """再次启动程序（双击快捷方式）时由单实例服务调用：把窗口切到前台；正在退出时撤销退出。"""
        if self._quit_pending:
            self._cancel_quit()
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def _cancel_quit(self) -> None:
        self._quit_pending = False
        self._quit_timer.stop()
        if self._ai_hidden_for_quit and self._ai_dock is not None:
            self._ai_dock.show()
        self._ai_hidden_for_quit = False
        app = QApplication.instance()
        if app is not None:
            app.setQuitOnLastWindowClosed(True)
        if self._gen_task is not None:  # 生成已经取消，等它在下一个进度点停下
            self.btn_generate.setText("正在取消…")
            self._set_status("上一次生成已在退出时取消，正在停止，完成后可以重新生成…")

    def _finish_quit(self) -> None:
        # 不用 QApplication.quit()：Qt6 的 quit() 会先尝试关闭所有窗口，有窗口拒绝关闭就放弃退出
        self._quitting = True
        self._shutdown_ai()
        self._quit_timer.stop()
        QApplication.exit(0)
