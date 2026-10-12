"""界面逻辑测试。

纯逻辑部分（kmcf.gui.logic / stock_names）不需要 Qt；后面几组用离屏 Qt 测试工作线程、
自选股对话框和主窗口（scheduler / generate 全部 mock，绝不注册真实的定时任务，也不联网）。
所有配置读写都指向临时目录，不会改动 data/config.json。

运行：.venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kmcf import paths  # noqa: E402
from kmcf.gui import logic, stock_names  # noqa: E402

D = dt.date


class TestLabels(unittest.TestCase):
    def test_report_label(self):
        self.assertEqual(logic.report_label(D(2026, 10, 9)), "2026-10-09 周五")
        self.assertEqual(logic.report_label(D(2026, 10, 11)), "2026-10-11 周日")

    def test_format_run_time(self):
        self.assertEqual(logic.format_run_time("2026-10-12 19:00"), "10-12 周一 19:00")
        self.assertEqual(logic.format_run_time("坏数据"), "坏数据")
        self.assertEqual(logic.format_run_time(""), "")


class TestWatchlistRows(unittest.TestCase):
    def test_parse_code(self):
        for text in ("600519", " 600519 ", "sh600519", "SH600519", "600519.SH", "600519.ss", "bj920000"):
            self.assertIsNotNone(logic.parse_code(text), text)
        self.assertEqual(logic.parse_code("sz000001"), "000001")
        for text in ("60051", "6005199", "abc", "", None, "600 51", "600519.HK"):
            self.assertIsNone(logic.parse_code(text), text)

    def test_parse_amount(self):
        self.assertEqual(logic.parse_amount(""), 0.0)
        self.assertEqual(logic.parse_amount("1,000"), 1000.0)
        self.assertEqual(logic.parse_amount(" 12.5 "), 12.5)
        self.assertIsNone(logic.parse_amount("-1"))
        self.assertIsNone(logic.parse_amount("abc"))
        self.assertIsNone(logic.parse_amount("nan"))
        self.assertIsNone(logic.parse_amount("inf"))

    def test_fmt_amount(self):
        self.assertEqual(logic.fmt_amount(0), "")
        self.assertEqual(logic.fmt_amount(0.0), "")
        self.assertEqual(logic.fmt_amount(1000.0), "1000")
        self.assertEqual(logic.fmt_amount(12.3456789), "12.3457")
        self.assertEqual(logic.fmt_amount(None), "")

    def test_validate_rows(self):
        rows = [
            {"code": "600519", "name": "贵州茅台", "shares": "100", "cost": "1500.5", "note": " 长线 "},
            {"code": "", "name": "", "shares": "", "cost": "", "note": ""},          # 空行忽略
            {"code": "60051", "name": "", "shares": "", "cost": "", "note": ""},     # 代码错
            {"code": "sh600519", "name": "", "shares": "", "cost": "", "note": ""},  # 重复
            {"code": "000001", "name": "", "shares": "abc", "cost": "-2", "note": ""},
            {"code": "", "name": "只有名称", "shares": "", "cost": "", "note": ""},   # 缺代码
            {"code": "300750.SZ", "name": "宁德时代", "shares": "", "cost": "", "note": ""},
        ]
        items, errors = logic.validate_rows(rows)
        self.assertEqual([i["code"] for i in items], ["600519", "300750"])
        self.assertEqual(items[0], {"code": "600519", "name": "贵州茅台", "shares": 100.0, "cost": 1500.5, "note": "长线"})
        err = {(r, f) for r, f, _m in errors}
        self.assertEqual(err, {(2, "code"), (3, "code"), (4, "shares"), (4, "cost"), (5, "code")})
        msgs = {(r, f): m for r, f, m in errors}
        self.assertIn("第 1 行", msgs[(3, "code")])
        self.assertIn("请填写", msgs[(5, "code")])

    def test_dedupe_and_save(self):
        store = {"cfg": {"watchlist": [], "top_n": 30, "schedule_time": "19:00", "extra": 1}}
        saved = []
        cfg = logic.save_watchlist(
            [{"code": "600519", "name": "A"}, {"code": "600519", "name": "B"}, {"code": "bad"},
             {"code": "300750", "shares": "200"}],
            load=lambda: dict(store["cfg"]), save=saved.append, backup=lambda: None)
        self.assertEqual([i["code"] for i in cfg["watchlist"]], ["600519", "300750"])
        self.assertEqual(cfg["watchlist"][0]["name"], "A")
        self.assertEqual(cfg["watchlist"][1]["shares"], 200.0)
        self.assertEqual(saved[0]["extra"], 1)
        self.assertEqual(saved[0]["top_n"], 30)

    def test_save_schedule_time(self):
        store = {"schedule_time": "19:00"}
        saved, backups = [], []
        kw = dict(load=lambda: dict(store), save=saved.append, backup=lambda: backups.append(1))
        self.assertFalse(logic.save_schedule_time("19:00", **kw))
        self.assertEqual(saved, [])
        self.assertTrue(logic.save_schedule_time("19:30", **kw))
        self.assertEqual(saved[0]["schedule_time"], "19:30")
        self.assertEqual(len(backups), 2)  # 每次写入前都检查原文件是否损坏

    def test_valid_time(self):
        self.assertEqual(logic.valid_time("19:00"), "19:00")
        self.assertEqual(logic.valid_time(" 9:05 "), "09:05")
        for bad in ("24:00", "19:60", "1900", "", None, "abc"):
            self.assertIsNone(logic.valid_time(bad), bad)
        self.assertEqual(logic.DEFAULT_SCHEDULE_TIME, "19:00")


class TestConfigFile(unittest.TestCase):
    """配置文件损坏时：能识别、写入前改名备份，不会用默认配置悄悄覆盖原文件。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "config.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_problem_detection(self):
        self.assertIsNone(logic.config_problem(self.cfg))  # 不存在不算损坏
        self.cfg.write_text('{"watchlist": [], "top_n": 20}', encoding="utf-8")
        self.assertIsNone(logic.config_problem(self.cfg))
        self.cfg.write_text('{"watchlist": [1, 2,]}', encoding="utf-8")  # 末尾多一个逗号
        self.assertTrue(logic.config_problem(self.cfg))
        self.cfg.write_text('[1, 2]', encoding="utf-8")
        self.assertIn("JSON 对象", logic.config_problem(self.cfg))

    def test_backup(self):
        self.cfg.write_text('{"watchlist": [1, 2,]}', encoding="utf-8")
        now = dt.datetime(2026, 10, 12, 19, 30, 5)
        with self.assertLogs("kmcf", level="WARNING"):
            b = logic.backup_corrupt_config(self.cfg, now=now)
        self.assertEqual(b.name, "config.json.bad-20261012-193005")
        self.assertEqual(b.read_text(encoding="utf-8"), '{"watchlist": [1, 2,]}')
        self.assertFalse(self.cfg.exists())
        self.assertIsNone(logic.backup_corrupt_config(self.cfg, now=now))  # 重复调用不再产生备份
        # 同一秒再次损坏：备份文件名不冲突
        self.cfg.write_text("{bad", encoding="utf-8")
        with self.assertLogs("kmcf", level="WARNING"):
            b2 = logic.backup_corrupt_config(self.cfg, now=now)
        self.assertEqual(b2.name, "config.json.bad-20261012-193005-1")
        # 正常的配置文件不动
        self.cfg.write_text('{"top_n": 20}', encoding="utf-8")
        self.assertIsNone(logic.backup_corrupt_config(self.cfg, now=now))
        self.assertTrue(self.cfg.exists())


class TestScheduleSummary(unittest.TestCase):
    def test_loading(self):
        on, title, _ = logic.schedule_summary(None)
        self.assertFalse(on)
        self.assertIn("读取", title)

    def test_not_installed(self):
        on, title, detail = logic.schedule_summary({"installed": False})
        self.assertEqual((on, title), (False, "未开启"))
        self.assertIn("每个工作日 19:00", detail)  # 默认晚上 7 点
        self.assertIn("19:30", logic.schedule_summary({"installed": False}, "19:30")[2])

    def test_error(self):
        on, title, detail = logic.schedule_summary({"installed": False, "error": "拒绝访问\n第二行"})
        self.assertFalse(on)
        self.assertEqual(title, "状态未知")
        self.assertIn("拒绝访问", detail)
        self.assertNotIn("第二行", detail)

    def test_installed(self):
        on, title, detail = logic.schedule_summary({
            "installed": True, "time": "19:00", "state": "Ready", "next_run": "2026-10-12 19:00",
            "last_run": "2026-10-09 19:00", "last_result": 0})
        self.assertTrue(on)
        self.assertEqual(title, "已开启：每个工作日 19:00")
        self.assertIn("下次运行 10-12 周一 19:00", detail)
        self.assertIn("上次运行 10-09 周五 19:00", detail)
        self.assertNotIn("异常", detail)

    def test_installed_failed_and_disabled(self):
        _on, _t, detail = logic.schedule_summary({
            "installed": True, "time": "19:00", "state": "Disabled", "next_run": "",
            "last_run": "2026-10-09 19:00", "last_result": 2})
        self.assertIn("禁用", detail)
        self.assertIn("返回码 2", detail)
        # 0x41303 = 尚未运行，不算异常
        _on, _t, detail = logic.schedule_summary({
            "installed": True, "time": "19:00", "last_run": "2026-10-09 19:00", "last_result": 267011})
        self.assertNotIn("异常", detail)


class TestGenerateHelpers(unittest.TestCase):
    def test_friendly_error(self):
        def resolve_trade_date():  # 与 kmcf.report.resolve_trade_date 同名：可预期的用户提示
            raise ValueError("2026-10-10 不是交易日")

        try:
            resolve_trade_date()
        except ValueError as e:
            title, text = logic.friendly_error(e)
        self.assertEqual((title, text), ("无法生成复盘", "2026-10-10 不是交易日"))

        # 其他 ValueError（内部错误）：仍显示消息，另外附上日志位置
        try:
            float("abc")
        except ValueError as e:
            title, text = logic.friendly_error(e, Path("X:/logs/kmcf.log"))
        self.assertEqual(title, "无法生成复盘")
        self.assertTrue(text.startswith("could not convert"))
        self.assertIn("kmcf.log", text)
        # 空消息的 ValueError 不会得到空文本
        title, text = logic.friendly_error(ValueError(""))
        self.assertTrue(text.startswith("无法生成复盘（未知原因）"))
        self.assertTrue(text.splitlines())

        title, text = logic.friendly_error(RuntimeError("连接被重置\n很长的堆栈"), Path("X:/logs/kmcf.log"))
        self.assertEqual(title, "生成失败")
        self.assertIn("连接被重置", text)
        self.assertNotIn("很长的堆栈", text)
        self.assertIn("kmcf.log", text)
        self.assertIn("…", logic.friendly_error(RuntimeError("x" * 500))[1])

    def test_latest_hint(self):
        self.assertIsNone(logic.latest_hint(D(2026, 10, 9), {D(2026, 10, 9)}))
        self.assertIsNone(logic.latest_hint(None, set()))
        hint = logic.latest_hint(D(2026, 10, 9), {D(2026, 10, 8)})
        self.assertIn("2026-10-09 周五", hint)
        self.assertIn("生成最新复盘", hint)
        self.assertNotIn("估算", hint)
        # 日历联网失败、按工作日估算时要提示
        self.assertIn("估算", logic.latest_hint(D(2026, 10, 5), set(), approximate=True))
        self.assertIn("估算", logic.latest_hint(D(2026, 10, 5), {D(2026, 10, 5)}, approximate=True))

    def test_latest_caption(self):
        self.assertEqual(logic.latest_caption(None, set()), "正在读取交易日历…")
        self.assertEqual(logic.latest_caption(None, set(), failed=True), "交易日历读取失败")
        self.assertEqual(logic.latest_caption(D(2026, 10, 9), {D(2026, 10, 9)}), "最近收盘：10-09 周五 · 已生成")
        self.assertEqual(logic.latest_caption(D(2026, 10, 12), {D(2026, 10, 9)}), "最近收盘：10-12 周一 · 未生成")
        self.assertIn("估算", logic.latest_caption(D(2026, 10, 5), set(), approximate=True))

    def test_check_review_date(self):
        trade = {D(2026, 9, 30), D(2026, 10, 9), D(2026, 10, 12)}
        is_td = trade.__contains__

        def prev(d):
            c = [x for x in trade if x < d]
            return max(c) if c else None

        latest = D(2026, 10, 9)
        ok, msg = logic.check_review_date(D(2026, 10, 9), latest, is_td, prev)
        self.assertTrue(ok)
        ok, msg = logic.check_review_date(D(2026, 10, 1), latest, is_td, prev)
        self.assertFalse(ok)
        self.assertIn("不是交易日", msg)
        self.assertIn("2026-09-30 周三", msg)
        ok, msg = logic.check_review_date(D(2026, 10, 12), latest, is_td, prev)
        self.assertFalse(ok)
        self.assertIn("还没有收盘", msg)

    def test_delete_report(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            for name in ("2026-10-09.html", "2026-10-09.json", "2026-10-08.html", "assets.txt"):
                (tmp / name).write_text("x", encoding="utf-8")
            removed = logic.delete_report(tmp / "2026-10-09.html")
            self.assertEqual(sorted(p.name for p in removed), ["2026-10-09.html", "2026-10-09.json"])
            self.assertEqual(sorted(p.name for p in tmp.iterdir()), ["2026-10-08.html", "assets.txt"])
            self.assertEqual([p.name for p in logic.delete_report(tmp / "2026-10-08.html")], ["2026-10-08.html"])
            with self.assertRaises(ValueError):
                logic.delete_report(tmp / "_dev_market.html")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestStockNames(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cache = self.tmp / "names.pkl"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_helpers(self):
        self.assertEqual(stock_names.list_key("600519"), "sh")
        self.assertEqual(stock_names.list_key("688981"), "kcb")
        self.assertEqual(stock_names.list_key("000001"), "sz")
        self.assertEqual(stock_names.list_key("300750"), "sz")
        self.assertEqual(stock_names.list_key("920000"), "bj")
        self.assertEqual(stock_names.list_key("830799"), "bj")
        self.assertIsNone(stock_names.list_key("999999"))
        self.assertEqual(stock_names.sina_symbol("688981"), "sh688981")
        self.assertEqual(stock_names.sina_symbol("920000"), "bj920000")
        self.assertEqual(stock_names.clean_name("万  科Ａ"), "万科A")
        self.assertEqual(stock_names.clean_name(" * ST 东 园 "), "*ST东园")

    def test_parse_sina(self):
        text = ('var hq_str_sh600519="贵州茅台,1255.420,1255.790";\n'
                'var hq_str_sz000002="万 科Ａ,4.130";\nvar hq_str_sz999999="";\n')
        self.assertEqual(stock_names.parse_sina(text), {"600519": "贵州茅台", "000002": "万科A"})

    def test_names_from_frame(self):
        import pandas as pd
        df = pd.DataFrame({"A股代码": [1, 2, 300750], "A股简称": ["平安银行", "万  科Ａ", None]})
        self.assertEqual(stock_names.names_from_frame(df, "A股代码", "A股简称"),
                         {"000001": "平安银行", "000002": "万科A"})
        self.assertEqual(stock_names.names_from_frame(pd.DataFrame(), "a", "b"), {})

    def test_lookup_order_and_cache(self):
        calls = []

        def sina(codes):
            calls.append(("sina", list(codes)))
            return {"600519": "贵州茅台", "300750": "宁德时代"}

        def lists(key):
            calls.append(("list", key))
            return {"688981": "中芯国际"} if key == "kcb" else {}

        r = stock_names.lookup(["600519", "300750", "688981", "bad"], cache_path=self.cache,
                               sina_fetcher=sina, list_fetcher=lists)
        self.assertEqual(r["names"], {"600519": "贵州茅台", "300750": "宁德时代", "688981": "中芯国际"})
        self.assertEqual(r["missing"], [])
        self.assertEqual(r["sources"], ["新浪行情", "交易所股票名单"])
        self.assertEqual(calls, [("sina", ["600519", "300750", "688981"]), ("list", "kcb")])

        # 第二次全部命中缓存，不再联网
        calls.clear()
        r = stock_names.lookup(["600519", "688981"], cache_path=self.cache, sina_fetcher=sina, list_fetcher=lists)
        self.assertEqual(calls, [])
        self.assertEqual(r["sources"], ["本地缓存"])

    def test_lookup_fallbacks(self):
        def sina_down(codes):
            raise ConnectionError("blocked")

        def lists(key):
            if key == "sz":
                raise ConnectionError("szse down")
            return {"600519": "贵州茅台"}

        now = dt.datetime(2026, 10, 11, 20, 0)
        with self.assertLogs("kmcf", level="WARNING"):
            r = stock_names.lookup(["600519", "000001"], cache_path=self.cache, now=now,
                                   sina_fetcher=sina_down, list_fetcher=lists)
        self.assertEqual(r["names"], {"600519": "贵州茅台"})
        self.assertEqual(r["missing"], ["000001"])
        self.assertIn("新浪行情查询失败", r["errors"])
        self.assertIn("深交所名单下载失败", r["errors"])
        cache = stock_names.load_cache(self.cache)
        self.assertEqual(cache["fetched"], {"sh": now})

        # 名单 1 天内已下载过：代码不在名单里时不重复下载
        calls = []
        r = stock_names.lookup(["600000"], cache_path=self.cache, now=now + dt.timedelta(hours=2),
                               sina_fetcher=lambda c: {}, list_fetcher=lambda k: calls.append(k) or {})
        self.assertEqual(calls, [])
        self.assertEqual(r["missing"], ["600000"])
        # 超过 1 天会重新下载
        r = stock_names.lookup(["600000"], cache_path=self.cache, now=now + dt.timedelta(days=2),
                               sina_fetcher=lambda c: {}, list_fetcher=lambda k: calls.append(k) or {"600000": "浦发银行"})
        self.assertEqual(calls, ["sh"])
        self.assertEqual(r["names"], {"600000": "浦发银行"})

    def test_corrupt_cache(self):
        self.cache.write_bytes(b"not a pickle")
        with self.assertLogs("kmcf", level="WARNING"):
            self.assertEqual(stock_names.load_cache(self.cache), {"names": {}, "fetched": {}})


# ====================================================================== 离屏 Qt 测试

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --log-level=3")
    from kmcf import qt_compat

    qt_compat.prepare()
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QMessageBox

    HAVE_QT = True
except Exception:  # noqa: BLE001
    HAVE_QT = False

_APP = None
_TMP_CFG: Path | None = None
_ORIG_CFG = paths.CONFIG_PATH


def setUpModule():
    """所有测试都使用临时配置文件。"""
    global _TMP_CFG
    _TMP_CFG = Path(tempfile.mkdtemp(prefix="kmcf_test_"))
    paths.CONFIG_PATH = _TMP_CFG / "config.json"


def tearDownModule():
    paths.CONFIG_PATH = _ORIG_CFG
    if _TMP_CFG:
        shutil.rmtree(_TMP_CFG, ignore_errors=True)


def _app():
    global _APP
    if _APP is None:
        from kmcf.gui import app as gui_app
        _APP = gui_app.create_app([])
    return _APP


def _wait(cond, timeout=10.0):
    app = _app()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    raise AssertionError("等待超时")


@unittest.skipUnless(HAVE_QT, "PySide6 不可用")
class TestTasks(unittest.TestCase):
    def test_callbacks_run_in_gui_thread(self):
        from kmcf.gui.tasks import Task, wait_all
        _app()
        main = threading.get_ident()
        seen = {}

        def work(x):
            seen["worker"] = threading.get_ident()
            return x * 2

        Task(work, 21, on_done=lambda r: seen.update(result=r, cb=threading.get_ident())).start()
        _wait(lambda: "result" in seen)
        self.assertEqual(seen["result"], 42)
        self.assertNotEqual(seen["worker"], main)
        self.assertEqual(seen["cb"], main)

        with self.assertLogs("kmcf", level="ERROR"):
            Task(lambda: 1 / 0, on_error=lambda e: seen.update(err=e)).start()
            _wait(lambda: "err" in seen)
        self.assertIsInstance(seen["err"], ZeroDivisionError)

        # ValueError 也要写日志（WARNING + traceback），内部缺陷才有据可查
        def bad_value():
            raise ValueError("Out of range float values are not JSON compliant: nan")

        with self.assertLogs("kmcf", level="WARNING") as cm:
            Task(bad_value, on_error=lambda e: seen.update(verr=e)).start()
            _wait(lambda: "verr" in seen)
        self.assertTrue(any("WARNING" in line and "Traceback" in line for line in cm.output))
        self.assertTrue(wait_all(2000))

    def test_generate_cancel(self):
        from kmcf.gui.tasks import Cancelled, GenerateTask
        _app()
        out = {}

        def gen(date=None, force=False, progress=None):
            for i in range(200):
                try:
                    progress(i, "步骤")
                except Exception:  # 模拟 generate() 吞掉 Exception —— Cancelled 不应被吞掉
                    pass
                time.sleep(0.01)
            return "done"

        t = GenerateTask(gen, on_done=lambda r: out.update(r=r), on_error=lambda e: out.update(e=e))
        progress = []
        t.progress.connect(lambda p, m: progress.append(p))
        t.start()
        _wait(lambda: len(progress) > 3)
        t.cancel()
        _wait(lambda: out)
        self.assertIsInstance(out.get("e"), Cancelled)
        self.assertLess(len(progress), 200)


@unittest.skipUnless(HAVE_QT, "PySide6 不可用")
class TestWatchlistDialog(unittest.TestCase):
    def test_validate_and_save(self):
        from kmcf.gui.watchlist_dialog import COL, WatchlistDialog
        _app()
        saved = []
        dlg = WatchlistDialog(None, [{"code": "600519", "name": "贵州茅台", "shares": 0, "cost": 0, "note": ""}],
                              save_fn=lambda items: saved.append(items) or {"watchlist": items})
        warnings = []
        orig = QMessageBox.warning
        QMessageBox.warning = staticmethod(lambda *a, **k: warnings.append(a))
        try:
            dlg.add_row({"code": "12345", "name": "", "shares": "", "cost": "", "note": ""})
            dlg.save()
            self.assertEqual(saved, [])
            self.assertEqual(len(warnings), 1)
            self.assertEqual(dlg.table.item(1, COL["code"]).toolTip(), "代码必须是 6 位数字")
            dlg.table.item(1, COL["code"]).setText("sz000001")
            self.assertEqual(dlg.table.item(1, COL["code"]).text(), "000001")  # 自动规整
            dlg.table.item(1, COL["shares"]).setText("1,000")
            n = dlg.paste_text("300750\t宁德时代\t200\t180.5\t备注")
            self.assertEqual(n, 1)
            dlg.table.selectRow(2)
            dlg.move_selected(-1)
            self.assertEqual(dlg.rows()[1]["code"], "300750")
            dlg.save()
        finally:
            QMessageBox.warning = orig
        self.assertEqual(len(saved), 1)
        self.assertEqual([i["code"] for i in saved[0]], ["600519", "300750", "000001"])
        self.assertEqual(saved[0][2]["shares"], 1000.0)
        self.assertEqual(saved[0][1]["cost"], 180.5)

    def test_delete_key_clears_cell_not_row(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from kmcf.gui.watchlist_dialog import COL, WatchlistDialog
        _app()
        dlg = WatchlistDialog(None, [
            {"code": "600519", "name": "贵州茅台", "shares": 100, "cost": 1500, "note": "长线"},
            {"code": "300750", "name": "宁德时代", "shares": 0, "cost": 0, "note": ""}])
        dlg.table.setCurrentCell(0, COL["note"])
        QTest.keyClick(dlg.table, Qt.Key.Key_Delete)  # 同 Excel：只清空单元格
        self.assertEqual(dlg.table.rowCount(), 2)
        self.assertEqual(dlg.table.item(0, COL["note"]).text(), "")
        self.assertEqual(dlg.table.item(0, COL["shares"]).text(), "100")
        self.assertEqual([r["code"] for r in dlg.rows()], ["600519", "300750"])
        QTest.keyClick(dlg.table, Qt.Key.Key_Delete, Qt.KeyboardModifier.ControlModifier)  # Ctrl+Delete 删整行
        QTest.keyRelease(dlg.table, Qt.Key.Key_Control)  # 复位全局的修饰键状态，免得影响后面的测试
        self.assertEqual([r["code"] for r in dlg.rows()], ["300750"])
        dlg._dirty = False
        dlg.deleteLater()

    def test_save_writes_only_watchlist(self):
        from kmcf.config import load_config, save_config
        from kmcf.gui.watchlist_dialog import WatchlistDialog
        _app()
        cfg = load_config()
        cfg.update({"top_n": 33, "schedule_time": "19:00"})
        save_config(cfg)
        dlg = WatchlistDialog(None, [])
        dlg.add_row({"code": "601318", "name": "中国平安", "shares": "100", "cost": "50", "note": "x"})
        dlg.save()
        saved = json.loads(paths.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(saved["watchlist"], [{"code": "601318", "name": "中国平安", "shares": 100.0,
                                               "cost": 50.0, "note": "x"}])
        self.assertEqual(saved["top_n"], 33)
        self.assertTrue(str(paths.CONFIG_PATH).startswith(str(_TMP_CFG)))


class FakeScheduler:
    """代替 kmcf.scheduler：记录调用，绝不调用 powershell。block 设置后 status() 会卡住直到 set。"""

    def __init__(self):
        self.calls, self.installed, self.time, self.threads = [], False, None, set()
        self.block: threading.Event | None = None

    def status(self):
        self.threads.add(threading.get_ident())
        self.calls.append("status")
        if self.block is not None:
            self.block.wait(10)
        if not self.installed:
            return {"installed": False}
        return {"installed": True, "time": self.time, "state": "Ready", "next_run": f"2026-10-12 {self.time}",
                "last_run": "", "last_result": 267011}

    def install(self, t):
        self.threads.add(threading.get_ident())
        self.calls.append(f"install {t}")
        self.installed, self.time = True, t
        return f"已设置：每个工作日 {t} 自动生成复盘"

    def uninstall(self):
        self.threads.add(threading.get_ident())
        self.calls.append("uninstall")
        self.installed = False
        return "已关闭自动复盘"


class _Patch:
    """临时替换对象属性（QMessageBox.question 等），退出时恢复。"""

    def __init__(self, obj, name, value):
        self.obj, self.name, self.value = obj, name, value

    def __enter__(self):
        self.had = self.name in self.obj.__dict__
        self.orig = self.obj.__dict__.get(self.name)
        setattr(self.obj, self.name, self.value)
        return self

    def __exit__(self, *exc):
        if self.had:
            setattr(self.obj, self.name, self.orig)
        else:
            delattr(self.obj, self.name)


@unittest.skipUnless(HAVE_QT, "PySide6 不可用")
class TestDateDialog(unittest.TestCase):
    def test_future_dates_greyed_and_approx_notice(self):
        from PySide6.QtCore import QDate
        from PySide6.QtWidgets import QLabel
        from kmcf.gui import theme
        from kmcf.gui.date_dialog import DateDialog
        _app()
        dlg = DateDialog(None, D(2026, 10, 9), {D(2026, 10, 9)}, approximate=False)
        faint = theme.current()["faint"]
        fmt = dlg.calendar.dateTextFormat
        self.assertEqual(fmt(QDate(2026, 10, 12)).foreground().color().name(), faint)  # 还没收盘：灰色
        self.assertEqual(fmt(QDate(2026, 10, 1)).foreground().color().name(), faint)   # 国庆休市：灰色
        self.assertNotEqual(fmt(QDate(2026, 9, 30)).foreground().color().name(), faint)  # 可选交易日
        self.assertEqual(dlg.calendar.maximumDate(), QDate(2026, 10, 9))
        self.assertFalse(any("估算" in w.text() for w in dlg.findChildren(QLabel)))
        dlg.deleteLater()
        dlg = DateDialog(None, D(2026, 10, 9), set(), approximate=True)
        self.assertTrue(any("估算" in w.text() for w in dlg.findChildren(QLabel)))
        dlg.deleteLater()


@unittest.skipUnless(HAVE_QT, "PySide6 不可用")
class TestMainWindow(unittest.TestCase):
    def setUp(self):
        from kmcf.gui.main_window import MainWindow
        _app()
        self.tmp = Path(tempfile.mkdtemp(prefix="kmcf_win_"))
        self.report = self.tmp / "2026-10-09.html"
        self.report.write_text("<html><body><h1>测试报告</h1></body></html>", encoding="utf-8")
        (self.tmp / "2026-10-09.json").write_text("{}", encoding="utf-8")
        self.sched = FakeScheduler()
        self.gen_calls = []
        self.latest = D(2026, 10, 9)
        self.latest_threads = []
        self.approx = False

        def fake_generate(date=None, force=False, progress=None):
            self.gen_calls.append((date, force, threading.get_ident()))
            for p in (10, 50, 100):
                progress(p, f"进度 {p}")
            return self.report

        def list_reports():
            return [(D.fromisoformat(p.stem), p) for p in sorted(self.tmp.glob("*.html"), reverse=True)]

        def latest_fn():
            self.latest_threads.append(threading.get_ident())
            return self.latest

        self.settings = QSettings(str(self.tmp / "settings.ini"), QSettings.Format.IniFormat)
        self.win = MainWindow(self.settings, generate_fn=fake_generate, scheduler=self.sched,
                              list_reports_fn=list_reports, latest_fn=latest_fn, calendar_fn=lambda: self.approx)
        self.win.start()
        _wait(lambda: self.win._sched_status is not None and self.win._latest is not None
              and self.win._latest_task is None)

    def tearDown(self):
        from kmcf.gui.tasks import wait_all
        if self.sched.block is not None:
            self.sched.block.set()
        self.assertTrue(wait_all(5000))
        _app().processEvents()
        self.win._gen_task = None
        self.win._quit_pending = False
        _app().setQuitOnLastWindowClosed(True)
        self.win.close()
        self.win.deleteLater()
        _app().processEvents()
        for _ in range(50):  # WebEngine 可能还短暂占用着报告文件：稍等再删临时目录
            shutil.rmtree(self.tmp, ignore_errors=True)
            if not self.tmp.exists():
                break
            _app().processEvents()
            time.sleep(0.02)

    def test_startup_state(self):
        from PySide6.QtWidgets import QTimeEdit
        w = self.win
        self.assertEqual(w.windowTitle(), "kmcf · A股每日复盘")
        self.assertEqual(w.list.count(), 1)
        self.assertEqual(w.list.item(0).text(), "2026-10-09 周五")
        self.assertEqual(Path(w.view.current_path), self.report)
        self.assertEqual(w.lbl_sched.text(), "未开启")
        self.assertIn("每个工作日 19:00", w.lbl_sched_detail.text())
        self.assertIn("已生成", w.lbl_latest.text())
        self.assertNotIn(threading.get_ident(), self.sched.threads)  # status 在工作线程
        self.assertNotIn(threading.get_ident(), self.latest_threads)  # 交易日历在工作线程
        # 侧栏没有可被鼠标滚轮误改的时间框
        self.assertEqual(w.findChildren(QTimeEdit), [])
        self.assertTrue(w.lbl_config_warn.isHidden())

    def test_missing_latest_hint(self):
        w = self.win
        w._on_latest(D(2026, 10, 12))  # 最近交易日还没有报告：只提示，不自动生成
        self.assertIn("可以生成 2026-10-12 周一 的复盘", w.lbl_status.text())
        self.assertIn("未生成", w.lbl_latest.text())
        self.assertFalse(w.is_generating())
        self.assertEqual(self.gen_calls, [])

    def test_approximate_calendar_notice(self):
        w = self.win
        self.approx = True
        w.refresh_latest()
        _wait(lambda: w._latest_approx)
        self.assertIn("估算", w.lbl_latest.text())
        self.assertIn("估算", w.lbl_status.text())

    def test_generate_flow(self):
        w = self.win
        self.assertTrue(w.start_generate(None, False))
        self.assertFalse(w.btn_generate.isEnabled())
        self.assertFalse(w.btn_date.isEnabled())
        self.assertFalse(w.start_generate(None, False))  # 运行中不能重复开始
        _wait(lambda: not w.is_generating())
        self.assertTrue(w.btn_generate.isEnabled())
        self.assertEqual(w.btn_generate.text(), "生成最新复盘")
        self.assertEqual(len(self.gen_calls), 1)
        self.assertNotEqual(self.gen_calls[0][2], threading.get_ident())
        self.assertIn("复盘报告已生成", w.lbl_status.text())

    def test_latest_refreshes_while_running(self):
        """软件开着跨到下一个交易日：生成后、定时刷新时，“最近收盘”和日期对话框的上限都会更新。"""
        from kmcf.gui import date_dialog
        w = self.win
        n0 = len(self.latest_threads)
        self.latest = D(2026, 10, 12)
        new_report = self.tmp / "2026-10-12.html"

        def gen(date=None, force=False, progress=None):
            new_report.write_text("<html><body>10-12</body></html>", encoding="utf-8")
            progress(100, "完成")
            return new_report

        w._generate_fn = gen
        w.start_generate(None, False)
        _wait(lambda: not w.is_generating() and w._latest == D(2026, 10, 12) and w._latest_task is None)
        self.assertEqual(w.lbl_latest.text(), "最近收盘：10-12 周一 · 已生成")
        self.assertGreater(len(self.latest_threads), n0)

        # 定时刷新（每 5 分钟）同样会重新计算
        self.latest = D(2026, 10, 13)
        w._auto_refresh()
        _wait(lambda: w._latest == D(2026, 10, 13))
        self.assertIn("可以生成 2026-10-13 周二 的复盘", w.lbl_status.text())

        # 「复盘指定日期…」：先在工作线程重新计算，再打开对话框；对话框最大日期 = 最新的收盘日
        self.latest = D(2026, 10, 14)
        seen = {}

        def fake_exec(dlg):
            seen["max"] = dlg.calendar.maximumDate().toPython()
            seen["latest"] = dlg.latest
            return 0

        _wait(lambda: w._latest_task is None)
        n1 = len(self.latest_threads)
        with _Patch(date_dialog.DateDialog, "exec", fake_exec):
            w.pick_date()
            self.assertFalse(w.btn_date.isEnabled())  # 等待日历期间禁用，界面线程不联网
            _wait(lambda: "max" in seen)
        self.assertEqual(seen["max"], D(2026, 10, 14))
        self.assertEqual(len(self.latest_threads), n1 + 1)
        self.assertNotIn(threading.get_ident(), self.latest_threads)
        _wait(lambda: w.btn_date.isEnabled())
        self.assertEqual(w.btn_date.text(), "复盘指定日期…")

    def test_schedule_refresh_when_next_run_passed(self):
        w = self.win
        w._sched_status = {"installed": True, "time": "19:00", "next_run": "2026-10-09 19:00"}
        w._last_sched_refresh = time.monotonic()
        self.assertTrue(w._schedule_due(1800))  # “下次运行”已经过去：要刷新“上次运行”
        w._sched_status = {"installed": True, "time": "19:00", "next_run": "2099-01-01 19:00"}
        self.assertFalse(w._schedule_due(1800))
        w._last_sched_refresh = time.monotonic() - 3600
        self.assertTrue(w._schedule_due(1800))
        n = self.sched.calls.count("status")
        w._auto_refresh()
        _wait(lambda: not w._sched_busy and self.sched.calls.count("status") == n + 1)

    def test_generate_value_error(self):
        w = self.win
        shown = []

        def resolve_trade_date():
            raise ValueError("2026-10-10 不是交易日，上一个交易日是 2026-10-09")

        def bad(date=None, force=False, progress=None):
            resolve_trade_date()

        w._generate_fn = bad
        with _Patch(QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a[1:3]))):
            with self.assertLogs("kmcf", level="WARNING"):
                w.start_generate(D(2026, 10, 10), False)
                _wait(lambda: not w.is_generating())
        self.assertEqual(shown, [("无法生成复盘", "2026-10-10 不是交易日，上一个交易日是 2026-10-09")])

    def test_generate_empty_value_error(self):
        w = self.win
        shown = []

        def bad(date=None, force=False, progress=None):
            progress(10, "进度10")
            raise ValueError("")

        w._generate_fn = bad
        with _Patch(QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a[1:3]))):
            with self.assertLogs("kmcf", level="WARNING"):
                w.start_generate(None, False)
                _wait(lambda: not w.is_generating() and shown)
        self.assertEqual(shown[0][0], "无法生成复盘")
        self.assertIn("未知原因", shown[0][1])
        self.assertIn("未知原因", w.lbl_status.text())  # 状态栏不会停在旧的进度文字

    def test_schedule_toggle_and_time(self):
        from kmcf.gui import schedule_dialog
        w = self.win
        w.toggle_schedule()
        _wait(lambda: not w._sched_busy and (w._sched_status or {}).get("installed"))
        self.assertEqual(self.sched.calls[:2], ["status", "install 19:00"])  # 默认晚上 7 点
        self.assertEqual(w.lbl_sched.text(), "已开启：每个工作日 19:00")
        self.assertEqual(w.btn_sched.text(), "关闭")

        # 「修改时间…」：取消不做任何事
        with _Patch(schedule_dialog.ScheduleTimeDialog, "exec", lambda dlg: 0):
            w.edit_schedule_time()
        self.assertNotIn("install 19:30", self.sched.calls)
        # 点「保存」才写配置并重新注册
        seen = {}

        def accept_1930(dlg):
            seen["initial"] = dlg.time_str()
            dlg.time_edit.setTime(dlg.time_edit.time().addSecs(30 * 60))
            return 1

        with _Patch(schedule_dialog.ScheduleTimeDialog, "exec", accept_1930):
            w.edit_schedule_time()
        self.assertEqual(seen["initial"], "19:00")
        _wait(lambda: not w._sched_busy and w._sched_status.get("time") == "19:30")
        self.assertIn("install 19:30", self.sched.calls)
        self.assertEqual(json.loads(paths.CONFIG_PATH.read_text(encoding="utf-8"))["schedule_time"], "19:30")

        w.toggle_schedule()
        _wait(lambda: not w._sched_busy and not w._sched_status.get("installed"))
        self.assertEqual(self.sched.calls[-2:], ["uninstall", "status"])
        self.assertNotIn(threading.get_ident(), self.sched.threads)
        w.apply_schedule_time("19:00")  # 未开启时只写配置，不注册
        self.assertEqual(self.sched.calls[-1], "status")
        self.assertEqual(json.loads(paths.CONFIG_PATH.read_text(encoding="utf-8"))["schedule_time"], "19:00")
        self.assertIn("19:00", w.lbl_sched_detail.text())

    def test_schedule_watchdog(self):
        """powershell 卡住时：超时后自动复盘区域恢复可用；迟到的结果仍会被采用。"""
        w = self.win
        w.SCHED_TIMEOUT_MS = {"status": 300, "change": 300}
        self.sched.block = threading.Event()
        w.refresh_schedule()
        self.assertFalse(w.btn_sched.isEnabled())
        with self.assertLogs("kmcf", level="WARNING"):
            _wait(lambda: not w._sched_busy, timeout=5)
        self.assertEqual(w.lbl_sched.text(), "状态未知")
        self.assertIn("超时", w.lbl_sched_detail.text())
        self.assertTrue(w.btn_sched.isEnabled())
        self.sched.installed, self.sched.time = True, "19:00"
        self.sched.block.set()
        _wait(lambda: (w._sched_status or {}).get("installed"))
        self.assertEqual(w.lbl_sched.text(), "已开启：每个工作日 19:00")
        self.sched.block = None

    def test_delete_report(self):
        w = self.win
        with _Patch(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)):
            self.assertTrue(w.delete_report(D(2026, 10, 9), self.report))
        self.assertFalse(self.report.exists())
        self.assertFalse((self.tmp / "2026-10-09.json").exists())
        self.assertEqual(w.list.count(), 0)
        self.assertIsNone(w.view.current_path)  # 显示空状态页
        self.assertFalse(w.lbl_empty.isHidden())

    def test_click_externally_deleted_report(self):
        w = self.win
        old = self.tmp / "2026-10-08.html"
        old.write_text("<html></html>", encoding="utf-8")
        w.refresh_reports()
        self.assertEqual(w.list.count(), 2)
        old.unlink()  # 在外部删除，目录监视还没刷新
        w.list.setCurrentRow(1)
        self.assertIn("已不存在", w.lbl_status.text())
        self.assertEqual(Path(w.view.current_path), self.report)  # 没有去加载不存在的文件
        _wait(lambda: w.list.count() == 1)

    def test_corrupt_config_backed_up_before_write(self):
        w = self.win
        bad = '{"watchlist": [{"code": "000001", "name": "平安银行"},], "schedule_time": "19:00"}'
        paths.CONFIG_PATH.write_text(bad, encoding="utf-8")
        try:
            w._check_config()
            self.assertFalse(w.lbl_config_warn.isHidden())
            with self.assertLogs("kmcf", level="WARNING"):
                w.apply_schedule_time("19:30")
            backups = list(paths.CONFIG_PATH.parent.glob("config.json.bad-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), bad)  # 原文件完整保留
            self.assertIn("备份", w.lbl_status.text())
            self.assertTrue(w.lbl_config_warn.isHidden())
            self.assertEqual(json.loads(paths.CONFIG_PATH.read_text(encoding="utf-8"))["schedule_time"], "19:30")
        finally:
            for p in paths.CONFIG_PATH.parent.glob("config.json.bad-*"):
                p.unlink()
            w.apply_schedule_time("19:00")

    def test_open_watchlist_with_corrupt_config(self):
        """配置损坏：打开自选股先警告，对话框标明是默认示例；保存时原文件被改名备份，不会被悄悄覆盖。"""
        from kmcf.gui import watchlist_dialog
        w = self.win
        bad = '{"watchlist": [{"code": "000001", "name": "平安银行", "shares": 300},], "top_n": 20}'
        paths.CONFIG_PATH.write_text(bad, encoding="utf-8")
        warnings, seen = [], {}

        def fake_exec(dlg):
            seen["codes"] = [r["code"] for r in dlg.rows()]
            seen["notice"] = dlg.status.text()
            dlg.add_row({"code": "000001", "name": "平安银行", "shares": "300", "cost": "", "note": ""})
            dlg.save()
            return dlg.result()

        try:
            with _Patch(QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a[1]))), \
                    _Patch(watchlist_dialog.WatchlistDialog, "exec", fake_exec), \
                    self.assertLogs("kmcf", level="WARNING"):
                w.open_watchlist()
            self.assertEqual(warnings, ["配置文件已损坏"])
            self.assertEqual(seen["codes"], ["600519", "300750"])  # 默认示例
            self.assertIn("默认示例", seen["notice"])
            backups = list(paths.CONFIG_PATH.parent.glob("config.json.bad-*"))
            self.assertEqual([b.read_text(encoding="utf-8") for b in backups], [bad])
            saved = json.loads(paths.CONFIG_PATH.read_text(encoding="utf-8"))
            self.assertIn("000001", [i["code"] for i in saved["watchlist"]])
            self.assertIn("备份", w.lbl_status.text())
            self.assertTrue(w.lbl_config_warn.isHidden())
        finally:
            for p in paths.CONFIG_PATH.parent.glob("config.json.bad-*"):
                p.unlink()

    def _slow_generate(self, gate: threading.Event, steps: int = 400):
        def gen(date=None, force=False, progress=None):
            for i in range(steps):
                progress(1 + i % 99, f"步骤 {i}")
                gate.wait(0.02)
            return self.report
        return gen

    def test_quit_then_second_launch_revives_window(self):
        """生成中关闭窗口（已隐藏、等生成停止），此时再次启动程序：撤销退出，窗口能正常显示和关闭。"""
        from PySide6.QtWidgets import QApplication
        w = self.win
        exits = []
        w._generate_fn = self._slow_generate(threading.Event())
        w.show()
        with _Patch(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)), \
                _Patch(QApplication, "exit", staticmethod(lambda code=0: exits.append(code))):
            w.start_generate(None, False)
            _wait(lambda: w.progress.value() > 0)
            self.assertFalse(w.close())  # 确认退出：窗口隐藏，等生成停止
            self.assertFalse(w.isVisible())
            self.assertTrue(w._quit_pending)
            self.assertFalse(QApplication.instance().quitOnLastWindowClosed())

            w.request_show()  # 第二个实例连上来
            self.assertTrue(w.isVisible())
            self.assertFalse(w._quit_pending)
            self.assertTrue(QApplication.instance().quitOnLastWindowClosed())
            self.assertFalse(w._quit_timer.isActive())
            _wait(lambda: not w.is_generating())
            self.assertEqual(w.lbl_status.text(), "已取消生成")
            self.assertEqual(exits, [])  # 撤销退出后不会再自己消失
            self.assertTrue(w.btn_generate.isEnabled())
            self.assertTrue(w.close())  # 现在能正常关闭
        self.assertFalse(w.isVisible())

    def test_finish_quit_uses_exit(self):
        from PySide6.QtWidgets import QApplication
        w = self.win
        exits = []
        w.show()
        w._quit_pending = True
        self.assertFalse(w.close())  # 退出等待中：只隐藏
        self.assertFalse(w.isVisible())
        w.show()
        with _Patch(QApplication, "exit", staticmethod(lambda code=0: exits.append(code))):
            w._finish_quit()
        self.assertEqual(exits, [0])  # QApplication.exit 不经过关窗流程，不会被拒绝
        self.assertTrue(w.close())  # _quitting 之后关窗一定接受
        w._quitting = False

    def test_generation_finishes_while_confirm_dialog_open(self):
        w = self.win
        gate = threading.Event()
        w._generate_fn = self._slow_generate(gate, steps=5)
        w.show()

        def ask(*a, **k):  # 对话框还开着的时候，生成结束了
            gate.set()
            _wait(lambda: not w.is_generating())
            return QMessageBox.StandardButton.Yes

        errors = []
        orig_hook = sys.excepthook
        sys.excepthook = lambda *exc: errors.append(exc)
        try:
            with _Patch(QMessageBox, "question", staticmethod(ask)):
                w.start_generate(None, False)
                self.assertTrue(w.close())
        finally:
            sys.excepthook = orig_hook
        self.assertEqual(errors, [])
        self.assertFalse(w._quit_pending)
        self.assertFalse(w.isVisible())


if __name__ == "__main__":
    unittest.main()
