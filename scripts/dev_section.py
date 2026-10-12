"""开发调试：单独运行一个复盘模块，打印结果摘要，并生成只含该模块的预览 HTML。

用法（在项目根目录）：
    .venv\\Scripts\\python.exe scripts\\dev_section.py market
    .venv\\Scripts\\python.exe scripts\\dev_section.py sectors --date 20261009
    .venv\\Scripts\\python.exe scripts\\dev_section.py funds --no-snapshot   # 模拟盘中/历史日期，快照接口不可用
    .venv\\Scripts\\python.exe scripts\\dev_section.py watchlist --dump      # 打印完整 JSON

数据走 ctx.fetch 的磁盘缓存（data/raw/YYYYMMDD/），重复运行不会重复请求网络；--force 强制重新请求。
预览文件：reports/_dev_<模块>.html
"""

import argparse
import datetime as dt
import importlib
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kmcf import net, paths  # noqa: E402
from kmcf import trade_calendar as tc  # noqa: E402
from kmcf.config import load_config  # noqa: E402
from kmcf.context import Context  # noqa: E402
from kmcf.fetch import Fetcher  # noqa: E402
from kmcf.history import History  # noqa: E402
from kmcf.render import render_report, sanitize  # noqa: E402
from kmcf.report import resolve_trade_date, setup_logging  # noqa: E402


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser()
    p.add_argument("section", help="模块名：market / sectors / funds / watchlist")
    p.add_argument("--date", help="YYYYMMDD，默认最近一个已收盘交易日")
    p.add_argument("--no-snapshot", action="store_true", help="模拟快照不可用（盘中或历史日期）")
    p.add_argument("--force", action="store_true", help="忽略缓存重新请求")
    p.add_argument("--dump", action="store_true", help="打印完整 Section JSON")
    args = p.parse_args()

    setup_logging(logging.INFO)
    net.configure()
    paths.ensure_dirs()
    now = dt.datetime.now()
    trade_date = resolve_trade_date(args.date, now)
    snapshot_ok = tc.snapshot_available_for(trade_date, now) and not args.no_snapshot
    fetcher = Fetcher(paths.RAW_DIR / trade_date.strftime("%Y%m%d"), snapshot_ok=snapshot_ok, force=args.force,
                      same_day=trade_date == now.date() and now.time() < dt.time(20, 0))
    ctx = Context(trade_date=trade_date, cfg=load_config(), fetcher=fetcher, history=History(paths.HISTORY_PATH),
                  snapshot_ok=snapshot_ok, log=logging.getLogger("kmcf"))

    mod = importlib.import_module(f"kmcf.sections.{args.section}")
    t0 = time.monotonic()
    sec = mod.build(ctx)
    elapsed = time.monotonic() - t0
    d = sanitize(sec.to_dict())

    print(f"\n===== {mod.TITLE} ({mod.ID}) | {trade_date} | snapshot_ok={snapshot_ok} | {elapsed:.1f}s | {fetcher.stats}")
    print("summary:")
    for s in d["summary"]:
        print("  -", s)
    print("kpis:")
    for k in d["kpis"]:
        print(f"  {k['label']}: {k['value']}  {k['sub']}  [{k['tone']}]")
    print("blocks:")
    for b in d["blocks"]:
        extra = ""
        if b["type"] == "table":
            extra = f"{len(b['rows'])} rows, cols={[c['label'] for c in b['columns']]}"
            if b["rows"]:
                extra += f"\n      first row: {[c['text'] for c in b['rows'][0]]}"
        elif b["type"] == "chart":
            series = b["option"].get("series", [])
            series = series if isinstance(series, list) else [series]
            extra = f"series={[(s.get('type'), s.get('name'), len(s.get('data') or [])) for s in series]}"
        elif b["type"] == "ladder":
            extra = f"levels={[(lv['label'], len(lv['items'])) for lv in b['levels']]}"
        print(f"  [{b['type']}/{b['span']}] {b['title']}  {extra}")
    print("notes:", d["notes"])
    print("errors:", d["errors"])
    if args.dump:
        print(json.dumps(d, ensure_ascii=False, indent=1))

    report = {
        "version": "dev", "trade_date": trade_date.isoformat(), "weekday": "",
        "generated_at": now.strftime("%Y-%m-%d %H:%M"), "snapshot_ok": snapshot_ok,
        "calendar_approximate": tc.approximate, "banners": [], "sections": [d],
        "stats": dict(fetcher.stats), "elapsed": round(elapsed, 1),
    }
    out = paths.REPORT_DIR / f"_dev_{mod.ID}.html"
    out.write_text(render_report(report), encoding="utf-8")
    print("preview:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
