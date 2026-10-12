"""桌面软件入口：main.py -> qt_compat.prepare() -> kmcf.gui.app.main()。"""

import logging
import os
import sys

from .. import qt_compat

qt_compat.prepare()  # 必须在导入 PySide6 之前（main.py 已调用，这里再保险一次）

from PySide6.QtCore import QLibraryInfo, QLocale, QSettings, Qt, QTranslator  # noqa: E402
from PySide6.QtNetwork import QLocalServer, QLocalSocket  # noqa: E402
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: E402,F401  必须在 QApplication 之前导入
from PySide6.QtWidgets import QApplication  # noqa: E402

from .. import net  # noqa: E402
from . import tasks, theme  # noqa: E402
from .icon import app_icon  # noqa: E402

log = logging.getLogger("kmcf")

ORG = APP = "kmcf"
APP_ID = "kmcf.AStockDailyReview"


def _instance_key() -> str:
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return f"kmcf-gui-{user}"


def _guard_stdio() -> None:
    """pythonw 启动时 sys.stdout/stderr 是 None，akshare 的 tqdm 进度条写 stderr 会直接报错。"""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def _excepthook(exc_type, exc, tb) -> None:
    """界面槽函数里未捕获的异常：写日志，不让程序退出。"""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    log.error("界面未捕获的异常", exc_info=(exc_type, exc, tb))


def _set_app_id() -> None:
    """让任务栏使用本程序的图标（否则显示为 python）。"""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass


def _install_translations(app: QApplication) -> None:
    """标准对话框按钮（是/否/取消）、日历月份等显示中文。"""
    QLocale.setDefault(QLocale(QLocale.Language.Chinese, QLocale.Country.China))
    path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    for name in ("qtbase_zh_CN", "qtwebengine_zh_CN"):
        tr = QTranslator(app)
        if tr.load(name, path):
            app.installTranslator(tr)


def _notify_running_instance() -> bool:
    """已有窗口在运行时，让它显示到前台，返回 True。"""
    sock = QLocalSocket()
    sock.connectToServer(_instance_key())
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return True
    return False


def _listen_for_instances(window) -> QLocalServer:
    QLocalServer.removeServer(_instance_key())
    server = QLocalServer(window)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)

    def on_conn():
        conn = server.nextPendingConnection()
        if conn is not None:
            conn.disconnected.connect(conn.deleteLater)
        # 窗口可能正处于“生成中退出、已隐藏、等生成停止”的状态：request_show 会撤销退出再显示
        window.request_show()

    server.newConnection.connect(on_conn)
    server.listen(_instance_key())
    return server


def create_app(argv: list[str] | None = None) -> QApplication:
    """创建并配置 QApplication（测试脚本也用它）。"""
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(list(argv if argv is not None else sys.argv))
    app.setOrganizationName(ORG)
    app.setApplicationName(APP)
    _install_translations(app)
    theme.apply(app)
    app.setWindowIcon(app_icon())
    return app


def main() -> int:
    from ..report import setup_logging

    setup_logging()
    _guard_stdio()
    net.configure()
    sys.excepthook = _excepthook
    _set_app_id()

    app = create_app()
    if _notify_running_instance():
        log.info("kmcf 已在运行，已切换到已有窗口")
        return 0

    from .main_window import MainWindow

    window = MainWindow(QSettings(ORG, APP))
    server = _listen_for_instances(window)

    def on_scheme_changed(_scheme):
        theme.apply(app)
        window.on_theme_changed()

    try:
        app.styleHints().colorSchemeChanged.connect(on_scheme_changed)
    except AttributeError:
        pass

    window.show()
    window.start()
    log.info("kmcf 界面已启动")
    code = app.exec()
    # 事件循环已结束，不再响应“显示窗口”请求：这时再次启动的程序会正常打开新窗口，而不是连到正在退出的进程
    server.close()

    # 等后台线程（生成/任务计划/联网查询）结束，避免 “QThread: Destroyed while thread is still running”
    if not tasks.wait_all(20000):
        log.warning("后台任务没有及时结束，强制退出")
        logging.shutdown()
        os._exit(code)
    return code
