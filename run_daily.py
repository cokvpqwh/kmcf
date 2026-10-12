"""定时任务入口：由 Windows 任务计划程序在交易日收盘后调用，生成当天复盘报告。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kmcf import stdio  # noqa: E402

stdio.guard()  # pythonw 下没有 stdout/stderr，必须在导入 akshare 之前处理

from kmcf.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["generate", "--scheduled"]))
