"""kmcf 入口。

    双击 kmcf.exe / 启动kmcf.bat        打开桌面软件
    kmcf.exe --scheduled                 定时任务：生成当天复盘（无界面）
    kmcf.exe generate|schedule ...       命令行，参数同 python -m kmcf.cli
    kmcf.exe --selftest out.png          打包冒烟测试（结果写 out.json）
"""

import datetime as dt
import os
import sys
import traceback
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

CLI_COMMANDS = ("generate", "schedule")


def _attach_console() -> None:
    """打包版是窗口程序，从命令行运行 generate/schedule 时挂到父控制台上，输出才看得见。"""
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        return
    try:
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
    except Exception:
        pass


def _crash_log(text: str) -> Path | None:
    """启动阶段出错（连日志都没初始化）时把错误写到文件，依次尝试 logs/ 和 %LOCALAPPDATA%\\kmcf。"""
    candidates = []
    try:
        from kmcf import paths
        candidates.append(paths.LOG_DIR)
    except Exception:
        pass
    candidates.append(Path(os.environ.get("LOCALAPPDATA", Path.home())) / "kmcf")
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            p = d / "crash.log"
            with open(p, "a", encoding="utf-8") as f:
                f.write(f"\n===== {dt.datetime.now():%Y-%m-%d %H:%M:%S} =====\n{text}")
            return p
        except OSError:
            continue
    return None


def _show_error(msg: str) -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, "kmcf 启动失败", 0x10)
            return
        except Exception:
            pass
    print(msg, file=sys.stderr)


def _run(args: list[str]) -> int:
    from kmcf import net, stdio
    stdio.guard()     # 无控制台运行时 stdout/stderr 为 None，akshare 的 tqdm 会报错
    net.configure()   # 国内数据域名直连；必须在任何联网（包括读取交易日历）之前
    if "--scheduled" in args:
        from kmcf.cli import main as cli_main
        return cli_main(["generate", "--scheduled"])
    if args and args[0] in CLI_COMMANDS:
        from kmcf.cli import main as cli_main
        return cli_main(args)
    if args and args[0] == "--selftest":
        from kmcf import qt_compat
        qt_compat.prepare()  # 必须在导入 PySide6 之前
        from kmcf.gui.selftest import main as selftest_main
        return selftest_main(args[1:])

    from kmcf import qt_compat
    qt_compat.prepare()  # 必须在导入 PySide6 之前
    from kmcf.gui.app import main as gui_main
    return gui_main()


def run() -> int:
    args = sys.argv[1:]
    gui_mode = not args or args[0] not in CLI_COMMANDS + ("--scheduled", "--selftest")
    if args and args[0] in CLI_COMMANDS:
        _attach_console()
    try:
        return _run(args)
    except SystemExit:
        raise
    except BaseException:
        text = traceback.format_exc()
        where = _crash_log(text)
        tip = f"错误详情已写入：{where}" if where else "错误详情无法写入日志文件。"
        if gui_mode:
            _show_error("kmcf 启动时出错。\n\n如果软件放在 C:\\Program Files 等需要管理员权限的目录，"
                        f"请移到 D:\\Apps 这类普通目录再运行。\n\n{tip}")
        else:
            print(f"kmcf 运行出错：{text.strip().splitlines()[-1]}\n{tip}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run())
