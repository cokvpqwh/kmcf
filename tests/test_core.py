"""核心模块（fetch / report 锁与站点标记 / config / trade_calendar / stdio）的单元测试，不联网。"""

import datetime as dt
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from kmcf import config, paths, report, stdio  # noqa: E402
from kmcf import trade_calendar as tc  # noqa: E402
from kmcf.fetch import Fetcher, FetchError, FetchTimeout, NoRetry, SnapshotUnavailable, with_timeout  # noqa: E402


def make_fetcher(tmp, **kw):
    kw.setdefault("snapshot_ok", True)
    f = Fetcher(Path(tmp), retries=kw.pop("retries", 3), min_interval=0, **kw)
    return f


class FetcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sleep = mock.patch("kmcf.fetch.time.sleep").start()
        self.addCleanup(mock.patch.stopall)

    def test_cache_and_has_cache(self):
        calls = []

        def src(x):
            calls.append(x)
            return pd.DataFrame({"a": [x]})

        f = make_fetcher(self.tmp)
        self.assertFalse(f.has_cache(src, 1))
        f.call(src, 1)
        self.assertTrue(f.has_cache(src, 1))
        df = f.call(src, 1)
        self.assertEqual(calls, [1])
        self.assertEqual(df["a"].iloc[0], 1)
        f.drop_cache(src, 1)
        self.assertFalse(f.has_cache(src, 1))

    def test_accept_false_not_cached(self):
        f = make_fetcher(self.tmp)
        empty = lambda: pd.DataFrame()  # noqa: E731
        empty.__name__ = "empty_src"
        f.call(empty, accept=lambda df: not df.empty)
        self.assertFalse(f.has_cache(empty))

    def test_retry_then_fail(self):
        n = {"c": 0}

        def bad():
            n["c"] += 1
            raise ConnectionError("reset")

        f = make_fetcher(self.tmp)
        with self.assertRaises(FetchError) as cm:
            f.call(bad)
        self.assertEqual(n["c"], 3)
        self.assertIsInstance(cm.exception.__cause__, ConnectionError)
        self.assertEqual(f.stats["failed"], 1)

    def test_noretry_raised_once(self):
        n = {"c": 0}

        def not_ready():
            n["c"] += 1
            raise NoRetry("数据日期不对")

        f = make_fetcher(self.tmp)
        with self.assertRaises(NoRetry):
            f.call(not_ready)
        self.assertEqual(n["c"], 1)

    def test_timeout_no_retry_and_cause(self):
        n = {"c": 0}
        ev = threading.Event()

        def hang():
            n["c"] += 1
            ev.wait(5)
            return pd.DataFrame()

        f = make_fetcher(self.tmp, timeout=0.2)
        t0 = time.monotonic()
        with self.assertRaises(FetchTimeout) as cm:
            f.call(hang)
        ev.set()
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual(n["c"], 1)
        self.assertIsInstance(cm.exception.__cause__, TimeoutError)

    def test_force_falls_back_to_old_cache(self):
        ok = {"v": True}

        def src():
            if ok["v"]:
                return pd.DataFrame({"a": [1]})
            raise ConnectionError("down")

        make_fetcher(self.tmp).call(src)
        ok["v"] = False
        df = make_fetcher(self.tmp, force=True).call(src)
        self.assertEqual(df["a"].iloc[0], 1)

    def test_snapshot_unavailable_without_cache(self):
        f = make_fetcher(self.tmp, snapshot_ok=False)
        src = lambda: pd.DataFrame()  # noqa: E731
        src.__name__ = "snap_src"
        with self.assertRaises(SnapshotUnavailable):
            f.call(src, snapshot=True)

    def test_volatile_same_day_skips_cache(self):
        n = {"c": 0}

        def lhb():
            n["c"] += 1
            return pd.DataFrame({"a": [n["c"]]})

        make_fetcher(self.tmp).call(lhb)
        make_fetcher(self.tmp, same_day=True).call(lhb, volatile=True)
        self.assertEqual(n["c"], 2)
        make_fetcher(self.tmp, same_day=False).call(lhb, volatile=True)
        self.assertEqual(n["c"], 2)

    def test_with_timeout_keeps_name(self):
        def stock_x():
            return 1
        self.assertEqual(with_timeout(stock_x, 1).__name__, "stock_x")
        self.assertEqual(with_timeout(stock_x, 1)(), 1)


class LockAndFlagsTest(unittest.TestCase):
    def test_lock_blocks_second(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".lock"
            with report.GenerateLock(p):
                with self.assertRaises(ValueError):
                    with report.GenerateLock(p, wait=0):
                        pass
            with report.GenerateLock(p):  # 释放后可再次获取
                pass

    def test_site_flags_ttl(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(paths, "CACHE_DIR", Path(d)):
            now = dt.datetime(2026, 10, 9, 19, 0)
            report._save_site_flags({"push2_down": True, "ths_down": True}, {}, now)
            saved = json.loads((Path(d) / "site_status.json").read_text(encoding="utf-8"))
            self.assertEqual(set(saved), {"push2_down"})  # 只记录有备用源的站点
            self.assertEqual(report._load_site_flags(now + dt.timedelta(hours=1)), {"push2_down": True})
            self.assertEqual(report._load_site_flags(now + dt.timedelta(hours=4)), {})


class ConfigTest(unittest.TestCase):
    def test_corrupt_config_flag_and_save_strips_internal(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "config.json"
            cfg_path.write_text("{ not json", encoding="utf-8")
            with mock.patch.object(paths, "CONFIG_PATH", cfg_path), mock.patch.object(paths, "ensure_dirs"):
                cfg = config.load_config()
                self.assertIn("_config_error", cfg)
                self.assertEqual(cfg_path.read_text(encoding="utf-8"), "{ not json")  # 读的时候不覆盖原文件
                config.save_config(cfg)
                saved = json.loads(cfg_path.read_text(encoding="utf-8"))
                self.assertNotIn("_config_error", saved)


class CalendarTest(unittest.TestCase):
    def test_estimated_beyond_real_calendar(self):
        real = [dt.date(2026, 12, 30), dt.date(2026, 12, 31)]
        with mock.patch.object(tc, "_DAYS", None), mock.patch.object(tc, "_fetch_remote", return_value=real), \
                tempfile.TemporaryDirectory() as d, mock.patch.object(paths, "CACHE_DIR", Path(d)):
            tc.trade_days(refresh=True)
            self.assertFalse(tc.is_estimated(dt.date(2026, 12, 31)))
            self.assertTrue(tc.is_estimated(dt.date(2027, 1, 4)))
        tc._DAYS = None  # 让其他测试重新加载真实日历

    def test_resolve_bad_format_message(self):
        with self.assertRaises(ValueError) as cm:
            report.resolve_trade_date("2026-13-01", dt.datetime(2026, 10, 11, 12))
        self.assertIn("日期格式不对", str(cm.exception))


class CliOrderTest(unittest.TestCase):
    def test_scheduled_configures_network_before_calendar(self):
        from kmcf import cli
        order = []
        with mock.patch.object(cli.net, "configure", side_effect=lambda: order.append("net")), \
                mock.patch.object(cli, "setup_logging"), \
                mock.patch.object(cli.tc, "latest_completed_trade_day",
                                  side_effect=lambda now=None: order.append("calendar") or dt.date(2026, 10, 9)), \
                mock.patch.object(cli.tc, "is_trade_day", return_value=True), \
                mock.patch.object(cli, "generate", return_value=Path("x.html")):
            self.assertEqual(cli.main(["generate", "--scheduled"]), 0)
        self.assertEqual(order[:2], ["net", "calendar"])


class StdioTest(unittest.TestCase):
    def test_guard_replaces_none(self):
        old = sys.stderr
        try:
            sys.stderr = None
            stdio.guard()
            self.assertIsNotNone(sys.stderr)
            sys.stderr.write("x")
        finally:
            if sys.stderr is not old:
                sys.stderr.close()
            sys.stderr = old


if __name__ == "__main__":
    unittest.main()
