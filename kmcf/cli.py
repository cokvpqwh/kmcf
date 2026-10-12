"""命令行入口：

    python -m kmcf.cli generate [--date YYYYMMDD] [--force] [--open]
    python -m kmcf.cli schedule install|uninstall|status [--time HH:MM]
"""

import argparse
import datetime as dt
import json
import logging
import sys
import webbrowser

from . import net, scheduler, stdio
from . import trade_calendar as tc
from .config import load_config
from .report import generate, report_path, setup_logging

log = logging.getLogger("kmcf")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kmcf", description="kmcf A股每日复盘")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="生成复盘报告")
    g.add_argument("--date", help="交易日 YYYYMMDD，默认最近一个已收盘的交易日")
    g.add_argument("--force", action="store_true", help="忽略缓存，重新获取数据")
    g.add_argument("--open", action="store_true", help="生成后用浏览器打开")
    g.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)

    s = sub.add_parser("schedule", help="管理每日自动复盘（Windows 任务计划）")
    s.add_argument("action", choices=["install", "uninstall", "status"])
    s.add_argument("--time", help="HH:MM，默认读取配置 schedule_time")

    args = p.parse_args(argv)
    stdio.guard()
    net.configure()  # 定时任务会先查交易日历，必须先设好国内域名直连
    setup_logging()

    if args.cmd == "generate":
        if args.scheduled:
            now = dt.datetime.now()
            target = tc.latest_completed_trade_day(now)
            # 节假日补跑时，如果该交易日的报告已经有了就不重复生成
            if not tc.is_trade_day(now.date()) and report_path(target).exists():
                log.info("今天不是交易日，%s 的报告已存在，跳过", target)
                return 0
        try:
            out = generate(args.date, force=args.force, lock_wait=900 if args.scheduled else 0)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        print(out)
        if args.open:
            webbrowser.open(out.as_uri())
        return 0

    if args.cmd == "schedule":
        try:
            if args.action == "install":
                print(scheduler.install(args.time or load_config().get("schedule_time", "19:00")))
            elif args.action == "uninstall":
                print(scheduler.uninstall())
            else:
                print(json.dumps(scheduler.status(), ensure_ascii=False, indent=2))
        except (RuntimeError, ValueError) as e:
            print(e, file=sys.stderr)
            return 1
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
