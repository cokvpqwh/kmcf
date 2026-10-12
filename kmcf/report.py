"""生成一份完整的每日复盘报告。GUI 和定时任务都调用 generate()。"""

import datetime as dt
import json
import logging
import logging.handlers
import os
import shutil
import sys
import time
import traceback
import urllib.request
from pathlib import Path
from typing import Callable

from . import __version__, net, paths, stdio
from . import trade_calendar as tc
from .config import load_config
from .context import Context
from .fetch import Fetcher
from .history import History
from .render import ECHARTS_VERSION, ECHARTS_SOURCES, render_report, sanitize
from .section import Section
from .sections import SECTIONS

log = logging.getLogger("kmcf")

WEEKDAYS = "一二三四五六日"
Progress = Callable[[int, str], None]

RAW_KEEP_DAYS = 180  # data/raw 下原始数据缓存保留天数（自然日）
# 跨次生成记住“站点已被拦截”的标记，避免每次都先等主数据源重试失败。只记录有备用源的站点。
PERSIST_SITE_FLAGS = {"push2_down", "push2his_down"}
SITE_FLAG_TTL = dt.timedelta(hours=3)


def setup_logging(level: int = logging.INFO) -> None:
    paths.ensure_dirs()
    logger = logging.getLogger("kmcf")
    if logger.handlers:
        return
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(paths.LOG_DIR / "kmcf.log", maxBytes=2_000_000,
                                              backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if sys.stderr is not None and getattr(sys.stderr, "name", "") != os.devnull:  # 无控制台时不输出到屏幕
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)


def report_path(trade_date: dt.date) -> Path:
    return paths.REPORT_DIR / f"{trade_date.isoformat()}.html"


def list_reports() -> list[tuple[dt.date, Path]]:
    """已生成的报告，按日期倒序。"""
    out = []
    for p in paths.REPORT_DIR.glob("*.html"):
        try:
            out.append((dt.date.fromisoformat(p.stem), p))
        except ValueError:
            continue
    return sorted(out, reverse=True)


def ensure_echarts() -> bool:
    """把 echarts.min.js 缓存到 reports/assets，报告离线也能显示图表。失败不影响生成。"""
    target = paths.ASSET_DIR / "echarts.min.js"
    if target.exists() and target.stat().st_size > 500_000:
        return True
    paths.ASSET_DIR.mkdir(parents=True, exist_ok=True)
    bundled = paths.PKG_DIR / "assets" / "echarts.min.js"  # 打包版自带一份，首次运行直接复制，不用联网
    if bundled.exists() and bundled.stat().st_size > 500_000:
        try:
            shutil.copyfile(bundled, target)
            return True
        except OSError as e:
            log.warning("复制内置 echarts 失败：%s", e)
    for url in ECHARTS_SOURCES[1:]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"kmcf/{__version__}"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
            if len(data) > 500_000 and b"echarts" in data[:4000]:
                tmp = target.with_suffix(".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, target)
                log.info("已缓存 echarts %s（%s）", ECHARTS_VERSION, url)
                return True
        except Exception as e:
            log.warning("echarts 下载失败 %s：%s", url, e)
    return False


def resolve_trade_date(date_arg: str | dt.date | None, now: dt.datetime) -> dt.date:
    if date_arg is None:
        return tc.latest_completed_trade_day(now)
    if isinstance(date_arg, dt.date):
        d = date_arg
    else:
        s = str(date_arg).strip().replace("-", "").replace("/", "")
        try:
            d = dt.datetime.strptime(s, "%Y%m%d").date()
        except ValueError:
            raise ValueError(f"日期格式不对：{date_arg!r}，请写成 20261009 或 2026-10-09") from None
    if not tc.is_trade_day(d):
        prev = tc.prev_trade_day(d)
        raise ValueError(f"{d.isoformat()} 不是交易日" + (f"，上一个交易日是 {prev.isoformat()}" if prev else ""))
    if d > tc.latest_completed_trade_day(now):
        raise ValueError(f"{d.isoformat()} 还没有收盘，无法复盘")
    return d


class GenerateLock:
    """同一时间只允许一个复盘在生成（GUI 手动生成和 19:00 定时任务可能撞上）。"""

    def __init__(self, path: Path, wait: float = 0):
        self.path = path
        self.wait = wait
        self.f = None

    def __enter__(self):
        try:
            import msvcrt
        except ImportError:  # 非 Windows：不加锁
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "a+")
        deadline = time.monotonic() + self.wait
        while True:
            try:
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.f.close()
                    self.f = None
                    raise ValueError("另一个复盘正在生成（可能是定时任务），请稍后再试") from None
                time.sleep(2)

    def __exit__(self, *exc):
        if self.f is not None:
            try:
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self.f.close()
            self.f = None
        return False


def _site_flags_path() -> Path:
    return paths.CACHE_DIR / "site_status.json"


def _load_site_flags(now: dt.datetime) -> dict:
    try:
        saved = json.loads(_site_flags_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    flags = {}
    for key, ts in saved.items():
        try:
            if key in PERSIST_SITE_FLAGS and now - dt.datetime.fromisoformat(ts) < SITE_FLAG_TTL:
                flags[key] = True
        except (TypeError, ValueError):
            continue
    return flags


def _save_site_flags(shared: dict, loaded: dict, now: dt.datetime) -> None:
    """本次新发现被拦截的站点记下时间；本次没再标记（说明访问成功或没访问）的旧标记保留到过期。"""
    try:
        saved = json.loads(_site_flags_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    for key in PERSIST_SITE_FLAGS:
        if shared.get(key) and key not in loaded:
            saved[key] = now.isoformat(timespec="seconds")
    saved = {k: v for k, v in saved.items() if k in PERSIST_SITE_FLAGS}
    try:
        _site_flags_path().write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("站点状态写入失败：%s", e)


def _ai_review(data: dict, cfg: dict, report) -> dict | None:
    """生成“AI 复盘点评”（见 kmcf.ai.review_for_report）。任何异常都不影响报告生成。

    这一步有总时长上限（ai.review_budget，默认 90 秒），超时就跳过点评；等待期间约每秒调用一次进度回调，
    进度回调里抛出的 BaseException（用户取消）照常向上传递，中止生成。
    """
    try:
        from .ai import review_for_report
    except Exception as e:  # noqa: BLE001  AI 模块本身出问题也不能让报告失败
        log.warning("AI 模块加载失败，跳过 AI 复盘点评：%s", e)
        return None
    try:
        return review_for_report(data, cfg, progress=lambda msg: report(88, msg))
    except Exception as e:  # noqa: BLE001
        log.warning("AI 复盘点评失败：%s", type(e).__name__, exc_info=True)
        return {"error": f"AI 复盘点评生成时出错（{type(e).__name__}），报告其余部分不受影响"}


def _prune_raw(now: dt.datetime) -> None:
    cutoff = (now - dt.timedelta(days=RAW_KEEP_DAYS)).strftime("%Y%m%d")
    for d in paths.RAW_DIR.iterdir() if paths.RAW_DIR.exists() else []:
        if d.is_dir() and d.name.isdigit() and len(d.name) == 8 and d.name < cutoff:
            shutil.rmtree(d, ignore_errors=True)


def generate(date: str | dt.date | None = None, force: bool = False,
             progress: Progress | None = None, now: dt.datetime | None = None,
             lock_wait: float = 0) -> Path:
    """生成复盘报告，返回 HTML 路径。

    date: 复盘交易日（默认 = 最近一个已收盘交易日）
    force: 忽略已缓存的原始数据重新请求（快照类数据在已失效时仍用缓存）
    progress: 进度回调 progress(百分比, 文字)。回调里抛出的 BaseException（非 Exception）会中止生成。
    lock_wait: 已有复盘在生成时最多等待的秒数；等不到抛 ValueError。
    """
    stdio.guard()
    setup_logging()
    net.configure()
    paths.ensure_dirs()
    started = time.monotonic()
    now = now or dt.datetime.now()

    last = {"line": None}

    def report(pct: int, msg: str) -> None:
        # 等待 AI 点评时每秒会用同样的文字报告一次进度（借此及时响应取消），日志只记内容有变化的
        if (pct, msg) != last["line"]:
            last["line"] = (pct, msg)
            log.info("[%3d%%] %s", pct, msg)
        if progress:
            try:
                progress(pct, msg)
            except Exception:
                pass

    report(2, "读取交易日历")
    trade_date = resolve_trade_date(date, now)

    with GenerateLock(paths.DATA_DIR / ".generate.lock", wait=lock_wait):
        return _generate(trade_date, force, report, now, started)


def _generate(trade_date: dt.date, force: bool, report, now: dt.datetime, started: float) -> Path:
    snapshot_ok = tc.snapshot_available_for(trade_date, now)
    cfg = load_config()
    same_day = trade_date == now.date() and now.time() < dt.time(20, 0)
    fetcher = Fetcher(paths.RAW_DIR / trade_date.strftime("%Y%m%d"), snapshot_ok=snapshot_ok,
                      force=force, same_day=same_day)
    loaded_flags = {} if force else _load_site_flags(now)
    ctx = Context(trade_date=trade_date, cfg=cfg, fetcher=fetcher, history=History(paths.HISTORY_PATH),
                  snapshot_ok=snapshot_ok, log=log, shared=dict(loaded_flags))

    report(5, "准备图表组件")
    ensure_echarts()

    sections = []
    n = len(SECTIONS)
    for i, mod in enumerate(SECTIONS):
        report(8 + int(85 * i / n), f"正在分析：{mod.TITLE}")
        try:
            sec = mod.build(ctx)
        except Exception as e:
            log.error("模块 %s 生成失败：%s\n%s", mod.ID, e, traceback.format_exc())
            sec = Section(mod.ID, mod.TITLE)
            sec.error(f"本模块生成失败：{e}")
        sections.append(sec.to_dict())

    _save_site_flags(ctx.shared, loaded_flags, now)

    banners = []
    if cfg.get("_config_error"):
        banners.append(f"配置文件 data/config.json 无法读取（{cfg['_config_error']}），本报告使用的是默认自选股。"
                       "请在软件的『自选股/持仓』里重新保存。")
    if not snapshot_ok:
        if trade_date == tc.latest_completed_trade_day(now):
            banners.append("现在是交易时段，实时快照类数据（全市场涨跌分布、板块实时排行等）已经是今天的盘中数据，"
                           "不能代表复盘日收盘：这部分只使用之前缓存的数据，没有缓存就省略。"
                           "按日期查询的数据（涨停池、龙虎榜、K线等）不受影响。")
        else:
            banners.append("复盘的是历史交易日：实时快照类数据（全市场涨跌分布、板块实时排行等）只能使用当天曾缓存的数据，"
                           "没有缓存的部分会省略；按日期查询的数据（涨停池、龙虎榜、K线等）不受影响。")

    elapsed = round(time.monotonic() - started, 1)
    data = {
        "version": __version__,
        "trade_date": trade_date.isoformat(),
        "weekday": "周" + WEEKDAYS[trade_date.weekday()],
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "snapshot_ok": snapshot_ok,
        "calendar_approximate": tc.approximate or tc.is_estimated(trade_date),
        "banners": banners,
        "sections": sections,
        "stats": dict(fetcher.stats),
        "elapsed": elapsed,
        "ai": None,
    }

    data["ai"] = _ai_review(data, cfg, report)   # 未启用 AI 或没有 API Key 时为 None（报告里不显示卡片）
    data["elapsed"] = round(time.monotonic() - started, 1)

    report(95, "渲染报告")
    html = render_report(data)
    out = report_path(trade_date)
    tmp = out.with_suffix(".html.tmp")
    tmp.write_text(html, encoding="utf-8")
    os.replace(tmp, out)
    json_path = out.with_suffix(".json")
    tmp = json_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sanitize(data), ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, json_path)

    try:
        _prune_raw(now)
    except OSError as e:
        log.warning("清理旧缓存失败：%s", e)

    report(100, f"完成：{out.name}（{data['elapsed']} 秒）")
    return out
