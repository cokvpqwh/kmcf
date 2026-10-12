"""在导入 PySide6 之前调用 prepare()。

Anaconda 的 Python 会把 anaconda3\\Library\\bin 加进 DLL 搜索目录，里面有一份 ICU 73 的
icuuc.dll（导出函数名带版本号）。pip 版 Qt 6 依赖的是 Windows 系统自带的 icuuc.dll，
结果加载到 Anaconda 那份就会报 “DLL load failed: 找不到指定的程序”。
先按完整路径把系统 ICU 加载进进程，Qt 就会直接复用它。
"""

import ctypes
import os
import sys

_done = False


def prepare() -> None:
    global _done
    if _done or sys.platform != "win32":
        return
    sysdir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    for name in ("icuuc.dll", "icuin.dll", "icu.dll"):
        path = os.path.join(sysdir, name)
        if os.path.exists(path):
            try:
                ctypes.WinDLL(path)
            except OSError:
                pass
    _done = True
