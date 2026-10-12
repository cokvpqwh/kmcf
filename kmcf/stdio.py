"""pythonw / 打包后的无控制台程序里 sys.stdout、sys.stderr 是 None。

akshare 内部大量使用 tqdm 进度条，写 stderr 时会抛 AttributeError，导致 19:00 定时任务里
龙虎榜、同花顺等接口全部失败。所有入口（GUI、定时任务、命令行、generate）都先调用 guard()。
"""

import os
import sys


def guard() -> None:
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except OSError:
                pass
