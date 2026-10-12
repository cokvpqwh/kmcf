"""界面自检（打包后冒烟测试用）：启动主窗口 -> 加载最新报告 -> 等 ECharts 渲染 -> 截图 -> 写 JSON -> 退出。

    kmcf.exe --selftest out.png [--offscreen] [--timeout 20]
    python main.py --selftest out.png

结果写到 out.png（窗口截图，报告滚动到第一个图表）、out_top.png（报告顶部）和 out.json：
    {"ok": true, "charts": 9, "canvases": 9, "title": "kmcf 复盘 2026-10-09", ...}
退出码 0 = 正常（echarts 已加载、所有图表容器都画出了 canvas、标题正确），1 = 异常。

自检不会修改任务计划：传给主窗口的是假的 scheduler（status 返回“未开启”，install/uninstall 直接报错），
窗口设置写到临时 ini，不读写注册表里的 kmcf 设置；也不启动单实例服务，不影响正在运行的 kmcf。
"""

import argparse
import ctypes
import json
import logging
import os
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path

from .. import qt_compat

qt_compat.prepare()  # 必须在导入 PySide6 之前

log = logging.getLogger("kmcf")

# 统计报告页面里的图表状态（在页面主世界执行，才能看到 window.echarts）
_PROBE_JS = r"""
(function () {
  var els = document.querySelectorAll('.chart');
  var canvases = 0, rendered = 0, failed = 0, empty = 0;
  els.forEach(function (e) {
    var c = e.querySelectorAll('canvas');
    canvases += c.length;
    var drawn = false;
    c.forEach(function (cv) { if (cv.width > 0 && cv.height > 0) drawn = true; });
    if (drawn) rendered++;
    if (e.querySelector('.chart-fail')) failed++;
  });
  document.querySelectorAll('.empty').forEach(function () { empty++; });
  return JSON.stringify({
    echarts: !!window.echarts,
    echarts_version: window.echarts ? window.echarts.version : null,
    containers: els.length, rendered: rendered, canvases: canvases, failed: failed,
    sections: document.querySelectorAll('section.card').length, empty_blocks: empty,
    tables: document.querySelectorAll('table').length,
    title: document.title, ready: document.readyState, url: location.href
  });
})()
"""


# 把第一个图表滚动到窗口上部（瞬间滚动，报告 CSS 里有 scroll-behavior: smooth）
_SCROLL_JS = r"""
(function () {
  var e = document.querySelector('.chart canvas');
  e = e ? e.closest('.chart') : null;
  if (!e) return false;
  var y = e.getBoundingClientRect().top + window.scrollY - 150;
  window.scrollTo({top: Math.max(0, y), behavior: 'instant'});
  return true;
})()
"""


class _NoScheduler:
    """假的任务计划：自检绝不注册/删除 Windows 定时任务，也不调用 powershell。"""

    TASK_NAME = "kmcf_daily_review"

    @staticmethod
    def status() -> dict:
        return {"installed": False, "selftest": True}

    @staticmethod
    def install(*_a, **_k):
        raise RuntimeError("自检模式不修改任务计划")

    @staticmethod
    def uninstall(*_a, **_k):
        raise RuntimeError("自检模式不修改任务计划")


def _process_age_ms() -> float | None:
    """本进程从创建到现在的毫秒数（包括 PyInstaller 引导程序和 Python 启动时间）。"""
    if sys.platform != "win32":
        return None
    try:
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
        k32.GetSystemTimeAsFileTime.argtypes = [ctypes.POINTER(ctypes.c_ulonglong)]
        created, ex, kt, ut, now = (ctypes.c_ulonglong() for _ in range(5))
        if not k32.GetProcessTimes(k32.GetCurrentProcess(), ctypes.byref(created), ctypes.byref(ex),
                                   ctypes.byref(kt), ctypes.byref(ut)):
            return None
        k32.GetSystemTimeAsFileTime(ctypes.byref(now))
        return (now.value - created.value) / 10_000  # FILETIME 单位 100ns
    except Exception:
        return None


def _loaded_modules(keywords=("icu", "ssl", "crypto", "qt6core", "python3")) -> list[str]:
    """进程里已加载的 DLL（只列关心的），用来确认 ICU 来自 System32、OpenSSL 来自包内。"""
    return [m for m in _all_modules() if any(k in os.path.basename(m).lower() for k in keywords)]


# 打包版里这些动态库/扩展模块必须来自包内（_internal）；从 Anaconda 等外部目录加载说明打包不完整，
# 换到没有 Anaconda 的电脑上会打不开或 https 失败。ICU 例外：必须来自 System32（不在这里检查）。
_MUST_BUNDLE = re.compile(r"(\.pyd$)|^(python3\d*|libssl|libcrypto|libffi|ffi|sqlite3|zlib|libbz2|liblzma|libexpat|"
                          r"libmpdec|qt6|pyside6|shiboken6|mini_racer|libcurl)[-\w.]*\.dll$", re.IGNORECASE)


def _foreign_modules() -> list[str]:
    """不在程序目录、也不在 Windows 目录下的已加载模块（输入法、杀毒软件注入的 DLL 也会出现在这里，仅供参考）。"""
    from .. import paths

    def canon(p: str) -> str:  # 统一大小写、8.3 短文件名、符号链接后再比较
        try:
            return os.path.normcase(os.path.realpath(p))
        except (OSError, ValueError):
            return os.path.normcase(p)

    roots = [canon(str(paths.ROOT)).rstrip("\\/") + os.sep,
             canon(os.environ.get("SystemRoot", r"C:\Windows")).rstrip("\\/") + os.sep]
    return [m for m in _all_modules() if not any(canon(m).startswith(r) for r in roots)]


def _all_modules() -> list[str]:
    """进程里已加载的全部模块（DLL / pyd）的完整路径。"""
    if sys.platform != "win32":
        return []
    try:
        psapi, k32 = ctypes.WinDLL("psapi"), ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.EnumProcessModules.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_ulong,
                                             ctypes.POINTER(ctypes.c_ulong)]
        psapi.GetModuleFileNameExW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_ulong]
        proc = k32.GetCurrentProcess()
        arr = (ctypes.c_void_p * 4096)()
        needed = ctypes.c_ulong()
        if not psapi.EnumProcessModules(proc, arr, ctypes.sizeof(arr), ctypes.byref(needed)):
            return []
        out = []
        buf = ctypes.create_unicode_buffer(1024)
        for i in range(min(needed.value // ctypes.sizeof(ctypes.c_void_p), len(arr))):
            if psapi.GetModuleFileNameExW(proc, arr[i], buf, 1024):
                out.append(buf.value)
        return sorted(out)
    except Exception:
        return []


def _translations_info() -> dict:
    """Qt 自带翻译（标准按钮“确定/取消”等）是否能找到：打包后 QLibraryInfo 指向 _internal/PySide6/translations。"""
    try:
        from PySide6.QtCore import QLibraryInfo
        path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        return {"path": path, **{n: os.path.exists(os.path.join(path, n + ".qm"))
                                 for n in ("qtbase_zh_CN", "qtwebengine_zh_CN")}}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def _blank_ratio(img) -> float:
    """截图里与左上角同色的像素比例（抽样），用来判断报告区域是不是一片空白。"""
    w, h = img.width(), img.height()
    if w == 0 or h == 0:
        return 1.0
    ref = img.pixel(w - 5, h // 2)
    same = total = 0
    for x in range(w // 3, w, max(1, w // 60)):
        for y in range(40, h - 40, max(1, h // 40)):
            total += 1
            same += img.pixel(x, y) == ref
    return same / total if total else 1.0


def run(png: Path, timeout_s: float = 20.0, offscreen: bool = False) -> int:
    t_start = time.perf_counter()
    age_at_start = _process_age_ms()
    png = Path(png).resolve()
    out_json = png.with_suffix(".json")
    png.parent.mkdir(parents=True, exist_ok=True)
    result: dict = {"ok": False, "charts": 0, "canvases": 0, "title": "", "errors": []}

    def write_result() -> None:
        try:
            out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            log.error("自检结果写入失败：%s", e)

    if offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    try:
        from .. import net, paths
        from ..report import setup_logging

        setup_logging()
        net.configure()

        import PySide6
        from PySide6.QtCore import QSettings, QTimer, qVersion
        from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401  必须在 QApplication 之前导入

        from .app import _set_app_id, create_app
        from . import tasks

        _set_app_id()
        app = create_app([sys.argv[0]])
        from .main_window import MainWindow

        tmp_ini = Path(tempfile.gettempdir()) / f"kmcf_selftest_{os.getpid()}.ini"
        settings = QSettings(str(tmp_ini), QSettings.Format.IniFormat)
        window = MainWindow(settings, scheduler=_NoScheduler())

        import ssl
        result.update({
            "frozen": bool(getattr(sys, "frozen", False)),
            "executable": sys.executable,
            "root": str(paths.ROOT),
            "python": sys.version.split()[0],
            "qt": qVersion(),
            "pyside6": PySide6.__version__,
            "openssl": ssl.OPENSSL_VERSION,
            "platform": app.platformName(),
            "qt_translations": _translations_info(),
            "process_age_at_selftest_ms": round(age_at_start) if age_at_start is not None else None,
        })

        state = {"loaded": None, "probe": None, "done": False, "t_load": None, "final_pending": False,
                 "t_shown": None}

        def grab():
            img = window.grab().toImage()
            blank, method = _blank_ratio(img), "widget"
            if blank > 0.97 and not offscreen:  # 极少数显卡/驱动下 grab 拿不到网页内容：改用屏幕截图
                screen = window.screen() or app.primaryScreen()
                if screen is not None:
                    img2 = screen.grabWindow(window.winId()).toImage()
                    if not img2.isNull() and _blank_ratio(img2) < blank:
                        img, blank, method = img2, _blank_ratio(img2), "screen"
            return img, blank, method

        def finish(code_ok: bool, reason: str = "") -> None:
            """记录结果，先截报告顶部（_top.png），再滚动到第一个图表截主截图（界面 + 图表同框）。"""
            if state["done"]:
                return
            state["done"] = True
            probe = state["probe"] or {}
            result["charts"] = int(probe.get("rendered") or 0)
            result["canvases"] = int(probe.get("canvases") or 0)
            result["title"] = str(probe.get("title") or window.view.title() or "")
            result["probe"] = probe
            result["report"] = str(window.view.current_path) if window.view.current_path else None
            result["report_ready_ms"] = round((time.perf_counter() - t_start) * 1000)
            if reason:
                result["errors"].append(reason)

            top, blank, method = grab()
            top_path = png.with_name(png.stem + "_top.png")
            if top.save(str(top_path), "PNG"):
                result["screenshot_top"] = str(top_path)
            if result["charts"] <= 0:
                finalize(code_ok, (top, blank, method))
                return
            QTimer.singleShot(4000, lambda: finalize(code_ok))  # 保险：JS 回调没来也要结束
            window.view.page().runJavaScript(_SCROLL_JS, 0,
                                             lambda _r: QTimer.singleShot(900, lambda: finalize(code_ok)))

        def finalize(code_ok: bool, shot=None) -> None:
            if state.get("finalized"):
                return
            state["finalized"] = True
            img, blank, method = shot or grab()
            result["screenshot_method"] = method
            result["screenshot_blank_ratio"] = round(blank, 3)
            if not img.save(str(png), "PNG"):
                result["errors"].append(f"截图保存失败：{png}")
            result["screenshot"] = str(png)
            result["loaded_modules"] = _loaded_modules()
            icu_bad = [m for m in result["loaded_modules"]
                       if os.path.basename(m).lower().startswith("icu")
                       and "system32" not in m.lower() and "syswow64" not in m.lower()]
            if icu_bad:
                result["errors"].append("加载了非系统目录的 ICU：" + "; ".join(icu_bad))
            result["foreign_modules"] = _foreign_modules()
            if getattr(sys, "frozen", False):
                leaked = [m for m in result["foreign_modules"] if _MUST_BUNDLE.search(os.path.basename(m))]
                if leaked:
                    result["errors"].append("这些动态库应来自包内，却从外部目录加载（打包不完整？）：" + "; ".join(leaked))
            title_ok = result["title"].startswith("kmcf")
            result["ok"] = bool(code_ok and title_ok and not icu_bad and not result["errors"])
            write_result()
            window.close()
            QTimer.singleShot(0, lambda: app.exit(0 if result["ok"] else 1))

        def on_load_finished(ok: bool) -> None:
            state["loaded"] = ok
            if state["t_load"] is None:
                state["t_load"] = time.perf_counter()
                result["page_load_ms"] = round((state["t_load"] - t_start) * 1000)
            if not ok:
                log.warning("自检：报告页面加载失败 %s", window.view.url().toString())

        window.view.loadFinished.connect(on_load_finished)

        def on_probe(res) -> None:
            if state["done"]:
                return
            try:
                state["probe"] = json.loads(res) if isinstance(res, str) else None
            except ValueError:
                state["probe"] = None
            p = state["probe"] or {}
            n = int(p.get("containers") or 0)
            if p.get("echarts") and n > 0 and int(p.get("rendered") or 0) >= n and p.get("ready") == "complete":
                # 全部画出来了：再等一会儿让 400ms 的入场动画结束，截图更完整
                if not state["final_pending"]:
                    state["final_pending"] = True
                    QTimer.singleShot(1200, lambda: probe_then(final=True))

        def probe_then(final: bool = False) -> None:
            if state["done"]:
                return

            def cb(res):
                if final:
                    try:
                        state["probe"] = json.loads(res) if isinstance(res, str) else state["probe"]
                    except ValueError:
                        pass
                    p = state["probe"] or {}
                    n = int(p.get("containers") or 0)
                    finish(bool(p.get("echarts")) and n > 0 and int(p.get("rendered") or 0) >= n
                           and not int(p.get("failed") or 0))
                else:
                    on_probe(res)

            window.view.page().runJavaScript(_PROBE_JS, 0, cb)

        poll = QTimer(window)
        poll.setInterval(500)

        def tick() -> None:
            if state["done"]:
                poll.stop()
                return
            if state["final_pending"]:  # 已经全部渲染，正在等最后一次截图
                return
            if state["loaded"] and window.view.current_path is not None:
                probe_then()
            # 从窗口显示开始计时：刚复制到新电脑时杀毒软件扫描 Qt WebEngine，导入和建窗口可能就要十几秒
            base = state["t_shown"] if state["t_shown"] is not None else t_start
            if time.perf_counter() - base > timeout_s:
                poll.stop()
                p = state["probe"] or {}
                if window.view.current_path is None:
                    why = "没有找到报告（reports 目录为空），只显示了空状态页"
                elif not state["loaded"]:
                    why = f"报告页面 {timeout_s:.0f} 秒内没有加载完成"
                elif not p.get("echarts"):
                    why = "ECharts 没有加载（reports/assets/echarts.min.js 缺失且无法联网？）"
                else:
                    why = f"图表没有全部渲染：{p.get('rendered')}/{p.get('containers')}"
                finish(False, why)

        poll.timeout.connect(tick)

        def on_shown() -> None:
            state["t_shown"] = time.perf_counter()
            age = _process_age_ms()
            result["window_shown_ms"] = round((time.perf_counter() - t_start) * 1000)
            result["process_age_at_window_ms"] = round(age) if age is not None else None
            log.info("自检：窗口已显示（进程启动后 %s ms）", result["process_age_at_window_ms"])

        window.show()
        QTimer.singleShot(0, on_shown)
        window.start()
        poll.start()
        code = app.exec()

        if not tasks.wait_all(15000):
            result["errors"].append("后台任务没有及时结束")
            result["ok"] = False
            write_result()
            logging.shutdown()
            os._exit(1)
        try:
            tmp_ini.unlink(missing_ok=True)
        except OSError:
            pass
        log.info("自检结束：ok=%s charts=%s canvases=%s", result["ok"], result["charts"], result["canvases"])
        return 0 if (code == 0 and result["ok"]) else 1
    except Exception as e:  # noqa: BLE001
        result["errors"].append(f"{type(e).__name__}: {e}")
        result["traceback"] = traceback.format_exc()
        log.error("自检异常：%s", result["traceback"])
        write_result()
        return 1


def main(argv: list[str] | None = None) -> int:
    from .. import stdio
    stdio.guard()
    ap = argparse.ArgumentParser(prog="kmcf --selftest", description="kmcf 界面自检")
    ap.add_argument("png", nargs="?", default="kmcf_selftest.png", help="截图保存路径（同名 .json 为结果）")
    ap.add_argument("--timeout", type=float, default=20.0,
                    help="窗口显示后等待报告和图表渲染的最长秒数，默认 20")
    ap.add_argument("--offscreen", action="store_true", help="使用 Qt offscreen 平台（不显示窗口）")
    args = ap.parse_args(argv)
    return run(Path(args.png), timeout_s=args.timeout, offscreen=args.offscreen)


if __name__ == "__main__":
    sys.exit(main())
