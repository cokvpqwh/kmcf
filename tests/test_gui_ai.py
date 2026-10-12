"""「AI 分析」入口（界面）测试。

kmcf.ai 一律用 FakeAI 代替：不联网、不读取真实的 API Key、不调用 DeepSeek。
所有配置读写指向临时目录，不会改动 data/config.json；离屏 Qt（QT_QPA_PLATFORM=offscreen）。

运行：.venv\\Scripts\\python.exe -m unittest tests.test_gui_ai -v
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

from kmcf import paths, qt_compat  # noqa: E402
from kmcf.gui import logic  # noqa: E402
from kmcf.gui.ai_markdown import inline, md_to_html, plain_text  # noqa: E402

qt_compat.prepare()  # 必须在导入 PySide6 之前

D = dt.date
T = {"accent": "#3e63dd", "accent_text": "#2f4eb5", "card2": "#f9fafb", "border": "#e3e5e9",
     "border_strong": "#cdd0d6", "muted": "#60646c"}  # Markdown 转换用到的颜色（不导入 Qt）


# ====================================================================== 纯逻辑

class TestAISettingsLogic(unittest.TestCase):
    def test_parse_env_names(self):
        self.assertEqual(logic.parse_env_names("DEEPSEEK_API_KEY, DEEPSEEK-API"),
                         (["DEEPSEEK_API_KEY", "DEEPSEEK-API"], []))
        self.assertEqual(logic.parse_env_names(" A ，b;c  a "), (["A", "b", "c"], []))  # 不区分大小写去重
        names, bad = logic.parse_env_names("OK_1, 1BAD, X=Y")
        self.assertEqual(names, ["OK_1"])
        self.assertEqual(bad, ["1BAD", "X=Y"])
        self.assertEqual(logic.parse_env_names(""), ([], []))
        # 误把 Key 本身粘进来：算不合法，而且返回值里不出现 Key 原文
        secret = "sk-0123456789abcdef0123456789abcdef"
        names, bad = logic.parse_env_names(f"MY_KEY, {secret}")
        self.assertEqual(names, ["MY_KEY"])
        self.assertEqual(bad, [logic.SECRET_PLACEHOLDER])
        self.assertNotIn(secret, repr((names, bad)))

    def test_endpoint_problem(self):
        self.assertIsNone(logic.endpoint_problem("https://api.deepseek.com", ["DEEPSEEK_API_KEY"]))
        self.assertIsNone(logic.endpoint_problem("http://127.0.0.1:8000/v1", ["DEEPSEEK_API_KEY"]))
        self.assertIsNone(logic.endpoint_problem("https://api.example.com/v1", ["MY_KEY"]))
        self.assertIn("https", logic.endpoint_problem("http://api.example.com/v1", ["MY_KEY"]))
        msg = logic.endpoint_problem("https://api.example.com/v1", ["MY_KEY", "deepseek-api"])
        self.assertIn("deepseek-api", msg)
        self.assertIn("api.example.com", msg)

    def test_normalize_base_url(self):
        self.assertEqual(logic.normalize_base_url(" https://api.deepseek.com/ "), "https://api.deepseek.com")
        self.assertEqual(logic.normalize_base_url("http://127.0.0.1:8000/v1"), "http://127.0.0.1:8000/v1")
        for bad in ("api.deepseek.com", "ftp://x", "", None, "https://", "https://a b"):
            self.assertIsNone(logic.normalize_base_url(bad), bad)

    def test_merge_models(self):
        self.assertEqual(logic.merge_models(["deepseek-chat", "deepseek-reasoner"], "deepseek-chat"),
                         ["deepseek-chat", "deepseek-reasoner"])
        self.assertEqual(logic.merge_models(["deepseek-chat", "deepseek-reasoner"], "deepseek-reasoner"),
                         ["deepseek-chat", "deepseek-reasoner"])
        self.assertEqual(logic.merge_models(["a", "", "a", "b"], "x"), ["x", "a", "b"])
        self.assertEqual(logic.merge_models(None, ""), [])

    def test_save_ai_settings(self):
        store = {"watchlist": [{"code": "600519"}], "schedule_time": "19:00",
                 "ai": {"timeout": 60, "model": "old", "api_key": "sk-should-not-survive"}}
        saved, backups = [], []
        cfg = logic.save_ai_settings(
            {"model": "deepseek-reasoner", "provider": None, "api_key": "sk-x", "token": "t", "bogus": 1},
            load=lambda: json.loads(json.dumps(store)), save=saved.append, backup=lambda: backups.append(1))
        self.assertEqual(len(backups), 1)
        self.assertEqual(cfg["ai"], {"timeout": 60, "model": "deepseek-reasoner"})  # 未编辑的字段保留
        self.assertEqual(saved[0]["watchlist"], [{"code": "600519"}])
        self.assertEqual(saved[0]["schedule_time"], "19:00")
        text = json.dumps(saved[0], ensure_ascii=False)
        self.assertNotIn("sk-", text)  # 任何 Key 字段都不会写进配置
        self.assertNotIn("token", text)

    def test_save_ai_settings_skips_merged_defaults(self):
        """load_config 合并进来的默认值（timeout / temperature 等）不会被写进配置文件。"""
        from kmcf.config import DEFAULT_AI
        store = {"watchlist": [], "ai": dict(DEFAULT_AI)}   # 相当于 load_config() 的结果
        saved = []
        cfg = logic.save_ai_settings({"model": "deepseek-reasoner"}, load=lambda: json.loads(json.dumps(store)),
                                     save=saved.append, backup=lambda: None)
        self.assertEqual(cfg["ai"], {"model": "deepseek-reasoner"})
        self.assertEqual(saved[0]["ai"], {"model": "deepseek-reasoner"})


class TestMarkdown(unittest.TestCase):
    def test_escape_and_inline(self):
        h = md_to_html("<script>alert(1)</script> **粗体** *斜体* `code` ~~删~~", T)
        self.assertNotIn("<script>", h)
        self.assertIn("&lt;script&gt;", h)
        self.assertIn("<b>粗体</b>", h)
        self.assertIn("<i>斜体</i>", h)
        self.assertIn("<s>删</s>", h)
        self.assertIn("code", h)
        self.assertNotIn("<i>", inline("3*4*5 与 a * b * c", T))  # 乘号不是斜体
        self.assertNotIn("<b>", inline("**未闭合", T))
        self.assertIn('href="https://example.com/a?b=1&amp;c=2"', inline("[链接](https://example.com/a?b=1&c=2)", T))
        self.assertNotIn("href", inline("[x](javascript:alert(1))", T))

    def test_blocks(self):
        md = ("# 标题\n\n第一段\n第二行\n\n- 甲\n- 乙\n  - 乙一\n- 丙\n\n3. 三\n4. 四\n\n1、中文编号\n\n"
              "> 引用\n\n---\n\n| 指标 | 数值 |\n|:--|--:|\n| 涨停 | 46 |\n| 跌停 | 19 |\n\n```\nx = 1 < 2\n```\n结尾")
        h = md_to_html(md, T)
        self.assertIn("font-weight:600", h)        # 标题
        self.assertIn("第一段<br>第二行", h)
        self.assertEqual(h.count("<ul"), 2)       # 嵌套列表
        self.assertEqual(h.count("<li"), 7)
        self.assertIn('<ol start="3"', h)
        self.assertIn("中文编号", h)
        self.assertIn("<hr>", h)
        self.assertIn("<th", h)
        self.assertIn('align="right"', h)
        self.assertEqual(h.count("<th"), 2)
        self.assertEqual(md_to_html("| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n", T).count("<tr>"), 3)
        self.assertIn("x = 1 &lt; 2", h)
        self.assertTrue(h.endswith("结尾</p>"))
        self.assertEqual(h.count("<ul") + h.count("<ol"), h.count("</ul>") + h.count("</ol>"))

    def test_partial_stream(self):
        for md in ("```python\nprint(1", "| a | b |\n|--", "- 一\n- ", "**半个", "1. "):
            md_to_html(md, T)  # 不抛异常
        self.assertIn("print(1", md_to_html("```python\nprint(1", T))

    def test_plain_text(self):
        self.assertEqual(plain_text("## 标题\n**粗体**文字"), "标题\n粗体文字")


# ====================================================================== 离屏 Qt

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --log-level=3")
    from kmcf import qt_compat

    qt_compat.prepare()
    from PySide6.QtCore import QSettings, Qt, QUrl
    from PySide6.QtWidgets import QMessageBox

    HAVE_QT = True
except Exception:  # noqa: BLE001
    HAVE_QT = False

_APP = None
_TMP_CFG: Path | None = None
_ORIG_CFG = paths.CONFIG_PATH


def setUpModule():
    global _TMP_CFG
    _TMP_CFG = Path(tempfile.mkdtemp(prefix="kmcf_ai_test_"))
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
        time.sleep(0.005)
    raise AssertionError("等待超时")


class _Patch:
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


class FakeAI:
    """代替 kmcf.ai（按接口约定）：记录调用；chat 的行为由 script / mode 决定。"""

    class AIError(Exception):
        pass

    QUICK_PROMPTS = [
        ("总结今日市场", "请总结今天的市场表现"),
        ("板块轮动分析", "请分析板块轮动"),
        ("龙虎榜与资金解读", "请解读龙虎榜与资金"),
        ("短线情绪分析", "请分析短线情绪"),
        ("我的自选股表现", "请分析我的自选股表现"),
        ("对比近几日变化", "请对比近几日变化"),
    ]
    DEFAULTS = {"enabled": True, "provider": "DeepSeek", "base_url": "https://api.deepseek.com",
                "model": "deepseek-chat", "models": ["deepseek-chat", "deepseek-reasoner"],
                "api_key_env": ["DEEPSEEK_API_KEY", "DEEPSEEK-API"], "auto_review": True,
                "include_holdings": False, "timeout": 120, "temperature": 0.3}

    def __init__(self):
        self.found = True
        self.env = "DEEPSEEK_API_KEY"
        self.builds: list[dict] = []
        self.chats: list[dict] = []
        self.script: list[tuple[str, str]] = [("d", "今日市场**整体走弱**。"), ("d", "\n\n- 涨停 46 家\n- 跌停 19 家")]
        self.delay = 0.0
        self.mode = "script"          # script / block（等待取消）/ ignore_cancel / error
        self.error = None
        self.cancels: list[threading.Event] = []
        self.release = threading.Event()

    def ai_settings(self, cfg):
        s = json.loads(json.dumps(self.DEFAULTS))
        ai = (cfg or {}).get("ai")
        if isinstance(ai, dict):
            s.update(ai)
        return s

    def key_status(self, cfg):
        s = self.ai_settings(cfg)
        return {"found": self.found, "env": self.env if self.found else None,
                "provider": s["provider"], "model": s["model"]}

    def build_messages(self, question, report, cfg, *, history=None, include_holdings=False):
        self.builds.append({"question": question, "report": report, "history": list(history or []),
                            "include_holdings": include_holdings, "thread": threading.get_ident()})
        return [{"role": "system", "content": "SYS"}, *(history or []), {"role": "user", "content": question}]

    def chat(self, messages, cfg, *, model=None, stream=False, on_delta=None, on_reasoning=None, cancel=None,
             timeout=None, max_tokens=None):
        self.chats.append({"messages": messages, "model": model, "stream": stream, "thread": threading.get_ident(),
                           "max_tokens": max_tokens})
        self.cancels.append(cancel)
        if self.mode == "error":
            raise self.error
        out = ""
        if self.mode == "block":
            on_delta("部分内容")
            out = "部分内容"
            while not cancel.wait(0.01):
                pass
            return out
        if self.mode == "ignore_cancel":
            on_delta("慢")
            self.release.wait(5)
            return "慢慢的完整回答"
        for kind, text in self.script:
            if cancel is not None and cancel.is_set():
                break
            if kind == "r":
                on_reasoning(text)
            else:
                on_delta(text)
                out += text
            if self.delay:
                time.sleep(self.delay)
        return out


def _report(date: str, **extra) -> dict:
    d = {"version": 1, "trade_date": date, "weekday": "周一", "banners": [],
         "sections": [{"id": "market", "title": "大盘与情绪", "summary": ["上证指数下跌0.43%"],
                       "kpis": [], "blocks": [], "notes": [], "errors": []}]}
    d.update(extra)
    return d


@unittest.skipUnless(HAVE_QT, "PySide6 不可用")
class TestAIPanel(unittest.TestCase):
    def setUp(self):
        from kmcf.gui.ai_panel import AIPanel
        _app()
        self.tmp = Path(tempfile.mkdtemp(prefix="kmcf_ai_panel_"))
        for d in ("2026-10-09", "2026-10-12"):
            (self.tmp / f"{d}.json").write_text(json.dumps(_report(d), ensure_ascii=False), encoding="utf-8")
            (self.tmp / f"{d}.html").write_text("<html></html>", encoding="utf-8")
        self.ai = FakeAI()
        self.cfg = {"watchlist": [], "ai": {}}
        self.panel = AIPanel(None, ai_module=self.ai, load_cfg=lambda: json.loads(json.dumps(self.cfg)))
        self.panel.set_report(self.tmp / "2026-10-12.html")
        self.panel.resize(420, 760)
        self.panel.show()
        _app().processEvents()

    def tearDown(self):
        from kmcf.gui.tasks import wait_all
        self.ai.release.set()
        self.panel.shutdown()
        self.assertTrue(wait_all(5000))
        _app().processEvents()
        self.panel.close()
        self.panel.deleteLater()
        _app().processEvents()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def text(self) -> str:
        return self.panel.chat.toPlainText()

    def _send_and_wait(self, *a, **k):
        self.assertTrue(self.panel.send(*a, **k))
        self.assertTrue(self.panel.is_busy())
        _wait(lambda: not self.panel.is_busy())

    def test_build(self):
        from PySide6.QtWidgets import QLabel
        p = self.panel
        self.assertEqual(p.lbl_report.text(), "2026-10-12 周一")
        self.assertEqual([b.text() for b in p.quick_buttons], [x[0] for x in FakeAI.QUICK_PROMPTS])
        self.assertEqual([p.combo_model.itemText(i) for i in range(p.combo_model.count())],
                         ["deepseek-chat", "deepseek-reasoner"])
        self.assertEqual(p.combo_model.currentText(), "deepseek-chat")
        self.assertFalse(p.chk_holdings.isChecked())
        self.assertIn("持仓数量/成本/盈亏", p.chk_holdings.toolTip())
        self.assertTrue(p.lbl_holdings.isHidden())
        self.assertFalse(p.key_ok.isHidden())
        self.assertTrue(p.key_warn.isHidden())
        self.assertEqual(p.lbl_key.text(), "已连接 DeepSeek（来自环境变量 DEEPSEEK_API_KEY）")
        self.assertTrue(p.btn_send.isEnabled())
        self.assertFalse(p.btn_stop.isEnabled())
        self.assertIn("Enter 发送", p.input.placeholderText())
        self.assertIn("不构成投资建议", p.lbl_disclaimer.text())
        self.assertIn("快捷问题", self.text())  # 欢迎语
        labels = " ".join(w.text() for w in p.findChildren(QLabel))
        self.assertNotIn("sk-", labels)

    def test_no_key_disables_send(self):
        p = self.panel
        self.ai.found = False
        p.refresh_key()
        self.assertFalse(p.key_warn.isHidden())
        self.assertTrue(p.key_ok.isHidden())
        self.assertEqual(p.lbl_warn_title.text(), "未检测到 DeepSeek API Key")
        self.assertTrue(p.lbl_warn_text.text().startswith(
            "请在系统环境变量里设置 DEEPSEEK_API_KEY，设置好后点「重新检测」即可，无需重启软件"))
        self.assertEqual(p.key_warn.toolTip(), "未检测到 DeepSeek API Key：请在系统环境变量里设置 DEEPSEEK_API_KEY，"
                                               "设置好后点「重新检测」即可，无需重启软件")
        self.assertNotIn("重启软件", p.lbl_warn_text.text().replace("无需重启软件", ""))
        self.assertIn("DEEPSEEK-API", p.lbl_warn_text.text())
        self.assertFalse(p.btn_send.isEnabled())
        self.assertTrue(all(not b.isEnabled() for b in p.quick_buttons))
        p.input.setPlainText("你好")
        self.assertFalse(p.send())
        self.assertEqual(self.ai.chats, [])
        self.assertEqual(p.input.toPlainText(), "你好")  # 没发出去，输入内容保留
        # 设置好环境变量后「重新检测」即可使用
        self.ai.found = True
        p.btn_recheck.click()
        self.assertTrue(p.btn_send.isEnabled())
        self.assertFalse(p.key_ok.isHidden())

    def test_stream_display_and_history(self):
        p = self.panel
        self.ai.script = [("r", "先看指数，"), ("r", "再看情绪。")] + [("d", f"第{i}段。") for i in range(1, 6)] + \
                         [("d", "\n\n| 指标 | 数值 |\n|--|--|\n| 涨停 | 46 |")]
        self.ai.delay = 0.03
        p.input.setPlainText("今天市场怎么样？")
        self.assertTrue(p.send())
        self.assertEqual(p.input.toPlainText(), "")
        self.assertFalse(p.btn_send.isEnabled())
        self.assertTrue(p.btn_stop.isEnabled())
        self.assertFalse(p.quick_buttons[0].isEnabled())
        _wait(lambda: "第2段" in self.text() and p.is_busy())  # 流式：还没结束就已经显示
        self.assertNotIn("第5段", self.text())
        _wait(lambda: not p.is_busy())
        txt = self.text()
        self.assertIn("今天市场怎么样？", txt)
        self.assertIn("第1段。第2段。第3段。第4段。第5段。", txt)
        self.assertIn("思考过程（10 字）", txt)
        self.assertNotIn("先看指数", txt)  # 思考过程默认折叠
        self.assertIn("AI 生成，仅供参考，不构成投资建议", txt)
        self.assertIn("<table", p.chat.toHtml())  # Markdown 表格
        # 在工作线程调用；默认不包含持仓；报告是当前选中的那份
        main = threading.get_ident()
        self.assertNotEqual(self.ai.chats[0]["thread"], main)
        self.assertNotEqual(self.ai.builds[0]["thread"], main)
        self.assertTrue(self.ai.chats[0]["stream"])
        self.assertEqual(self.ai.chats[0]["model"], "deepseek-chat")
        b = self.ai.builds[0]
        self.assertFalse(b["include_holdings"])
        self.assertEqual(b["report"]["trade_date"], "2026-10-12")
        self.assertEqual(b["history"], [])
        self.assertEqual([m["role"] for m in p._history], ["user", "assistant"])
        self.assertTrue(p.btn_send.isEnabled())
        self.assertFalse(p.btn_stop.isEnabled())
        # 展开思考过程
        p._on_anchor(QUrl("kmcf:think/1"))
        self.assertIn("先看指数，再看情绪。", self.text())
        p._on_anchor(QUrl("kmcf:think/1"))
        self.assertNotIn("先看指数", self.text())
        # 第二问带上第一轮对话；换模型；勾选持仓
        self.ai.delay = 0
        p.combo_model.setCurrentText("deepseek-reasoner")
        p.chk_holdings.setChecked(True)
        self.assertFalse(p.lbl_holdings.isHidden())
        self._send_and_wait("那情绪呢？")
        b2 = self.ai.builds[1]
        self.assertEqual([m["role"] for m in b2["history"]], ["user", "assistant"])
        self.assertEqual(b2["history"][0]["content"], "今天市场怎么样？")
        self.assertTrue(b2["include_holdings"])
        self.assertEqual(self.ai.chats[1]["model"], "deepseek-reasoner")
        self.assertEqual(self.text().count("不构成投资建议"), 2)
        self.assertIn("今天市场怎么样？", p.conversation_text())
        self.assertIn("（AI 生成，仅供参考，不构成投资建议）", p.conversation_text())

    def test_refresh_is_throttled(self):
        p = self.panel
        n = {"tail": 0}
        orig = p._refresh_tail

        def counting():
            n["tail"] += 1
            orig()

        p._refresh_tail = counting
        self.ai.script = [("d", "字")] * 400
        self.ai.delay = 0.001
        self._send_and_wait("测试")
        self.assertIn("字" * 400, self.text())
        self.assertLess(n["tail"], 100)  # 400 个片段，刷新次数远少于片段数

    def test_quick_prompt(self):
        p = self.panel
        p.quick_buttons[1].click()
        _wait(lambda: not p.is_busy())
        self.assertEqual(self.ai.builds[0]["question"], "请分析板块轮动")
        self.assertIn("板块轮动分析", self.text())  # 对话区显示按钮名
        self.assertEqual(p._history[0]["content"], "请分析板块轮动")  # 上下文里是完整提问

    def test_enter_sends_shift_enter_newline(self):
        from PySide6.QtTest import QTest
        p = self.panel
        p.input.setFocus()
        p.input.insertPlainText("第一行")  # QTest.keyClicks 输入中文会让 PySide6 崩溃，中文用 insertPlainText
        QTest.keyClick(p.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
        QTest.keyRelease(p.input, Qt.Key.Key_Shift)
        p.input.insertPlainText("第二行")
        self.assertEqual(p.input.toPlainText(), "第一行\n第二行")
        self.assertEqual(self.ai.chats, [])
        QTest.keyClick(p.input, Qt.Key.Key_Return)
        _wait(lambda: self.ai.builds and not p.is_busy())
        self.assertEqual(self.ai.builds[0]["question"], "第一行\n第二行")
        self.assertEqual(p.input.toPlainText(), "")

    def test_stop(self):
        p = self.panel
        self.ai.mode = "block"
        self.assertTrue(p.send("慢问题"))
        _wait(lambda: "部分内容" in self.text())
        self.assertTrue(p.btn_stop.isEnabled())
        p.btn_stop.click()
        self.assertTrue(self.ai.cancels[0].is_set())
        _wait(lambda: not p.is_busy(), timeout=3)
        self.assertEqual(p._items[-1].state, "stopped")
        self.assertIn("（已停止）", self.text())
        self.assertIn("部分内容", self.text())  # 已收到的内容保留
        self.assertTrue(p.btn_send.isEnabled())
        self.assertEqual(p.btn_stop.text(), "停止")

    def test_stop_abandons_unresponsive_request(self):
        """停止后线程迟迟不结束（还在等服务器）：超时后放弃，界面恢复可用；迟到的结果被丢弃。"""
        from kmcf.gui.tasks import running
        p = self.panel
        p.STOP_GRACE_MS = 200
        self.ai.mode = "ignore_cancel"
        p.send("问题")
        _wait(lambda: "慢" in self.text())
        p.stop()
        self.assertEqual(p.btn_stop.text(), "正在停止…")
        self.assertIn("正在停止…", self.text())
        _wait(lambda: not p.is_busy(), timeout=3)
        self.assertTrue(p.btn_send.isEnabled())
        self.assertTrue(any(t.isRunning() for t in running()))  # 线程仍在，由 tasks 持有，不会被销毁
        self.ai.release.set()
        _wait(lambda: not any(t.isRunning() for t in running()))
        _app().processEvents()
        self.assertNotIn("慢慢的完整回答", self.text())
        self.assertEqual(p._history, [])

    def test_error_shown_and_retry(self):
        p = self.panel
        self.ai.mode = "error"
        self.ai.error = FakeAI.AIError("API Key 无效，请检查环境变量 DEEPSEEK_API_KEY 的值是否正确")
        with self.assertLogs("kmcf", level="WARNING") as cm:
            self._send_and_wait("问题")
        self.assertFalse(any("Traceback" in line for line in cm.output))  # 可预期的错误不写 traceback
        self.assertIn("请求失败：API Key 无效", self.text())
        self.assertIn("重试", self.text())
        self.assertEqual(p._history, [])
        self.assertTrue(p.btn_send.isEnabled())
        self.ai.mode = "script"
        p._on_anchor(QUrl("kmcf:retry/1"))
        _wait(lambda: not p.is_busy())
        self.assertEqual(self.ai.builds[-1]["question"], "问题")
        self.assertEqual(len(p._history), 2)

    def test_unexpected_error(self):
        self.ai.mode = "error"
        self.ai.error = RuntimeError("boom")
        with self.assertLogs("kmcf", level="ERROR"):
            self._send_and_wait("问题")
        self.assertIn("出错了：boom", self.text())

    def test_switch_report(self):
        p = self.panel
        self._send_and_wait("问题一")
        self.assertEqual(len(p._history), 2)
        p.set_report(self.tmp / "2026-10-09.html")
        self.assertEqual(p.lbl_report.text(), "2026-10-09 周五")
        self.assertIn("已切换到 2026-10-09 周五 的报告", self.text())
        self.assertIn("问题一", self.text())  # 旧消息保留显示
        self.assertEqual(p._history, [])     # 上下文清空
        p.set_report(self.tmp / "2026-10-09.html")  # 同一份报告：不重复提示
        self.assertEqual(self.text().count("已切换到"), 1)
        self._send_and_wait("问题二")
        b = self.ai.builds[-1]
        self.assertEqual(b["report"]["trade_date"], "2026-10-09")
        self.assertEqual(b["history"], [])

    def test_switch_report_while_streaming(self):
        p = self.panel
        self.ai.mode = "block"
        p.send("问题一")
        _wait(lambda: "部分内容" in self.text())
        p.set_report(self.tmp / "2026-10-09.html")
        self.assertNotIn("已切换到", self.text())  # 等这条回答结束再插入
        p.stop()
        _wait(lambda: not p.is_busy())
        txt = self.text()
        self.assertIn("已切换到 2026-10-09 周五 的报告", txt)
        self.assertLess(txt.index("部分内容"), txt.index("已切换到"))
        self.assertEqual(p._history, [])  # 旧报告的回答不进入新报告的上下文

    def test_no_report(self):
        p = self.panel
        p.set_report(None)
        self.assertEqual(p.lbl_report.text(), "暂无报告")
        self.assertFalse(p.btn_send.isEnabled())
        self.assertIn("生成最新复盘", p.input.placeholderText())
        self.assertIn("生成最新复盘", self.text())
        self.assertFalse(p.send("问题"))
        p.set_report(self.tmp / "2026-10-12.html")
        self.assertTrue(p.btn_send.isEnabled())

    def test_missing_json(self):
        (self.tmp / "2026-10-12.json").unlink()
        with self.assertLogs("kmcf", level="WARNING"):
            self._send_and_wait("问题")
        self.assertIn("缺少数据文件", self.text())
        self.assertEqual(self.ai.chats, [])

    def test_clear(self):
        p = self.panel
        self._send_and_wait("测试问句")
        self.assertTrue(p.btn_clear.isEnabled())
        p.btn_clear.click()
        self.assertEqual(p._items, [])
        self.assertEqual(p._history, [])
        self.assertNotIn("测试问句", self.text())
        self.assertFalse(p.btn_clear.isEnabled())

    def test_theme_switch_keeps_conversation(self):
        from kmcf.gui import theme as th
        p = self.panel
        self._send_and_wait("测试问句")
        th.apply(_app(), dark=True)
        try:
            p.apply_theme()
            self.assertIn("测试问句", self.text())
            self.assertIn(th.DARK["accent_soft"], p.chat.toHtml())  # 用户消息的底色换成暗色
        finally:
            th.apply(_app(), dark=False)
            p.apply_theme()
        self.assertIn(th.LIGHT["accent_soft"], p.chat.toHtml())

    def test_history_trimmed(self):
        p = self.panel
        p._history = [{"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 1000} for i in range(30)]
        h = p._trimmed_history()
        self.assertLessEqual(len(h), 2 * p.HISTORY_TURNS)
        self.assertEqual(h[0]["role"], "user")
        p._history = [{"role": "user", "content": "a" * 9000}, {"role": "assistant", "content": "b" * 9000}]
        h = p._trimmed_history()
        self.assertEqual(h, [])  # 只剩 assistant 开头的残缺一轮时整体丢弃

    def test_switch_report_via_no_report(self):
        """报告 A → 暂无报告（删除了当前报告）→ 报告 B：A 的问答不能带进 B 的上下文。"""
        p = self.panel
        self._send_and_wait("问题一")
        self.assertEqual(len(p._history), 2)
        p.set_report(None)
        p.set_report(self.tmp / "2026-10-09.html")
        self.assertEqual(p._history, [])
        self.assertIn("已切换到 2026-10-09 周五 的报告", self.text())
        self._send_and_wait("问题二")
        self.assertEqual(self.ai.builds[-1]["history"], [])
        p.set_report(None)                               # B → 暂无 → B：同一份报告，上下文保留
        p.set_report(self.tmp / "2026-10-09.html")
        self.assertEqual(len(p._history), 2)

    def test_unchecking_holdings_drops_holdings_turns(self):
        p = self.panel
        p.chk_holdings.setChecked(True)
        self._send_and_wait("我的持仓怎么样？")
        self.assertTrue(all(m["holdings"] for m in p._history))
        p.chk_holdings.setChecked(False)
        self.assertIn("之前带持仓数据的对话不会再发给 AI", self.text())
        self._send_and_wait("大盘怎么样？")
        b = self.ai.builds[-1]
        self.assertFalse(b["include_holdings"])
        self.assertEqual(b["history"], [])                     # 带持仓的那一轮没有带上
        p.chk_holdings.setChecked(True)
        self._send_and_wait("再看看持仓")
        self.assertEqual(len(self.ai.builds[-1]["history"]), 4)   # 勾选时两轮都带上
        self.assertTrue(all(set(m) == {"role", "content"} for m in self.ai.builds[-1]["history"]))

    def test_compliance_warning_shown(self):
        p = self.panel
        self.ai.compliance_hits = lambda text: ["建议买入"] if "建议买入" in text else []
        self.ai.script = [("d", "今日指数下跌。建议买入券商股。")]
        with self.assertLogs("kmcf", level="INFO") as cm:
            self._send_and_wait("能买吗")
        self.assertIn("疑似操作建议或预测表述（建议买入）", self.text())
        self.assertIn("不提供投资建议", self.text())
        self.assertTrue(any("建议买入" in line for line in cm.output))
        self.assertIn("疑似操作建议", p.conversation_text())
        self.ai.script = [("d", "今日指数下跌。")]
        self._send_and_wait("今天呢")
        self.assertEqual(self.text().count("疑似操作建议"), 1)    # 合规的回答不提示

    def test_truncated_answer_noted(self):
        import kmcf.ai as real
        self.ai.chat = lambda *a, on_delta=None, **k: (on_delta("很长的回答"), real.AIText("很长的回答", "length"))[1]
        self._send_and_wait("问题")
        self.assertIn("回答达到长度上限", self.text())

    def test_links_need_confirmation(self):
        from unittest import mock
        p = self.panel
        opened = []
        with mock.patch("kmcf.gui.ai_panel.QDesktopServices.openUrl", side_effect=opened.append):
            with mock.patch.object(p, "confirm_open_url", return_value=False) as ask:
                p._on_anchor(QUrl("https://example.com/x"))
            self.assertEqual(ask.call_count, 1)
            self.assertEqual(opened, [])
            with mock.patch.object(p, "confirm_open_url", return_value=True):
                p._on_anchor(QUrl("https://example.com/x"))
            self.assertEqual([u.toString() for u in opened], ["https://example.com/x"])
            with _Patch(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)):
                self.assertFalse(p.confirm_open_url(QUrl("https://example.com/y")))

    def test_first_user_message_right_aligned(self):
        p = self.panel
        for _ in range(2):
            self._send_and_wait("问题")
            doc = p.chat.document()
            heads = []
            block = doc.begin()
            while block.isValid():
                if block.text().startswith("你 · "):
                    heads.append(block.blockFormat().alignment())
                block = block.next()
            self.assertTrue(heads)
            self.assertTrue(all(a & Qt.AlignmentFlag.AlignRight for a in heads), heads)
            p.clear()

    def test_ai_disabled(self):
        p = self.panel
        self.cfg["ai"] = {"enabled": False}
        p.reload_config()
        self.assertFalse(p.btn_send.isEnabled())
        self.assertIn("已关闭", p.lbl_warn_title.text())

    def test_key_value_never_displayed(self):
        """界面只显示变量名：即使环境变量里有值，任何标签和对话区都不会出现它。"""
        from PySide6.QtWidgets import QLabel
        secret = "sk-kmcf-test-DO-NOT-SHOW-123"
        os.environ["KMCF_TEST_FAKE_KEY"] = secret
        try:
            self.ai.env = "KMCF_TEST_FAKE_KEY"
            self.panel.refresh_key(announce=True)
            self._send_and_wait("问题")
            shown = " ".join(w.text() for w in self.panel.findChildren(QLabel)) + self.text()
            self.assertIn("KMCF_TEST_FAKE_KEY", shown)
            self.assertNotIn(secret, shown)
            self.assertNotIn(secret, self.panel.chat.toHtml())
        finally:
            del os.environ["KMCF_TEST_FAKE_KEY"]


@unittest.skipUnless(HAVE_QT, "PySide6 不可用")
class TestAIPanelWithRealCore(unittest.TestCase):
    """面板 + 真实的 kmcf.ai（只替换 chat 和 key_status：不联网、不依赖本机环境变量）。"""

    def test_real_build_messages_and_quick_prompts(self):
        import types

        import kmcf.ai as real
        from kmcf.gui.ai_panel import AIPanel
        from kmcf.gui.tasks import wait_all
        _app()
        seen = {}

        def fake_chat(messages, cfg, *, model=None, stream=False, on_delta=None, on_reasoning=None, cancel=None,
                      timeout=None, max_tokens=None):
            seen.update(messages=messages, model=model, stream=stream)
            on_delta("今日上证指数下跌0.43%。")
            return "今日上证指数下跌0.43%。"

        ai = types.SimpleNamespace(**{k: getattr(real, k) for k in real.__all__})
        ai.chat = fake_chat
        ai.key_status = lambda cfg: {"found": True, "env": "DEEPSEEK_API_KEY", "provider": "DeepSeek",
                                     "model": "deepseek-chat"}
        self.assertEqual([x[0] for x in real.QUICK_PROMPTS][:6],
                         ["总结今日市场", "板块轮动分析", "龙虎榜与资金解读", "短线情绪分析", "我的自选股表现", "对比近几日变化"])
        tmp = Path(tempfile.mkdtemp(prefix="kmcf_ai_real_"))
        try:
            (tmp / "2026-10-12.json").write_text(json.dumps(_report("2026-10-12"), ensure_ascii=False),
                                                 encoding="utf-8")
            panel = AIPanel(None, ai_module=ai, load_cfg=lambda: {"ai": {}})
            panel.set_report(tmp / "2026-10-12.html")
            panel.quick_buttons[0].click()
            _wait(lambda: not panel.is_busy())
            msgs = seen["messages"]
            self.assertEqual(msgs[0]["role"], "system")
            self.assertIn("不提供任何投资建议", msgs[0]["content"])   # 系统提示词禁止投资建议
            self.assertIn("上证指数下跌0.43%", msgs[0]["content"])     # 报告上下文
            self.assertEqual(msgs[-1], {"role": "user", "content": real.QUICK_PROMPTS[0][1]})
            self.assertTrue(seen["stream"])
            self.assertIn("今日上证指数下跌0.43%", panel.chat.toPlainText())
            self.assertIn(real.DISCLAIMER, panel.chat.toPlainText())
            panel.shutdown()
            self.assertTrue(wait_all(3000))
            panel.deleteLater()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(HAVE_QT, "PySide6 不可用")
class TestAISettingsDialog(unittest.TestCase):
    def setUp(self):
        _app()
        from kmcf.config import load_config, save_config
        cfg = load_config()
        cfg.update({"schedule_time": "19:30", "top_n": 33,
                    "watchlist": [{"code": "600519", "name": "贵州茅台", "shares": 100, "cost": 1500, "note": ""}],
                    "ai": {"timeout": 90}})
        save_config(cfg)
        self.ai = FakeAI()

    def test_save_preserves_other_config(self):
        from kmcf.config import load_config
        from kmcf.gui.ai_settings_dialog import AISettingsDialog
        dlg = AISettingsDialog(None, self.ai, load_config())
        self.assertEqual(dlg.ed_base_url.text(), "https://api.deepseek.com")
        self.assertEqual(dlg.ed_env.text(), "DEEPSEEK_API_KEY, DEEPSEEK-API")
        self.assertTrue(dlg.chk_auto.isChecked())
        self.assertFalse(dlg.chk_holdings.isChecked())
        dlg.ed_base_url.setText("api.example.com")
        dlg.save()
        self.assertNotEqual(dlg.result(), 1)  # 地址不合法：不保存
        self.assertIn("http", dlg.status.text())
        dlg.ed_base_url.setText("https://api.example.com/v1/")
        dlg.combo_model.setCurrentText("my-model")
        dlg.ed_env.setText("MY_KEY，DEEPSEEK_API_KEY")
        dlg.chk_auto.setChecked(False)
        dlg.save()   # 换成别的服务器却还用 DeepSeek 的 Key 变量：不保存
        self.assertNotEqual(dlg.result(), 1)
        self.assertIn("DEEPSEEK_API_KEY", dlg.status.text())
        dlg.ed_base_url.setText("http://api.example.com/v1/")
        dlg.ed_env.setText("MY_KEY，OTHER_KEY")
        dlg.save()   # 远程 http：Key 会明文传输，不保存
        self.assertNotEqual(dlg.result(), 1)
        self.assertIn("https://", dlg.status.text())
        dlg.ed_base_url.setText("https://api.example.com/v1/")
        dlg.save()
        self.assertEqual(dlg.result(), 1)
        saved = json.loads(paths.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertTrue(str(paths.CONFIG_PATH).startswith(str(_TMP_CFG)))
        ai = saved["ai"]
        self.assertEqual(ai["base_url"], "https://api.example.com/v1")
        self.assertEqual(ai["model"], "my-model")
        self.assertEqual(ai["models"], ["my-model", "deepseek-chat", "deepseek-reasoner"])
        self.assertEqual(ai["api_key_env"], ["MY_KEY", "OTHER_KEY"])
        self.assertFalse(ai["auto_review"])
        self.assertEqual(ai["timeout"], 90)          # 对话框没有的字段保留
        self.assertNotIn("provider", ai)             # 与默认值相同：不写入
        self.assertNotIn("include_holdings", ai)
        self.assertEqual(saved["schedule_time"], "19:30")
        self.assertEqual(saved["top_n"], 33)
        self.assertEqual(saved["watchlist"][0]["code"], "600519")
        self.assertNotIn("key\":", json.dumps(ai).lower().replace("api_key_env", ""))
        dlg.deleteLater()

    def test_reset_defaults_and_check_key(self):
        from kmcf.gui.ai_settings_dialog import AISettingsDialog
        cfg = {"ai": {"model": "x", "base_url": "https://a.b", "api_key_env": ["A"]}}
        dlg = AISettingsDialog(None, self.ai, cfg, save_fn=lambda v: v)
        self.assertEqual(dlg.combo_model.currentText(), "x")
        dlg.reset_defaults()
        self.assertEqual(dlg.combo_model.currentText(), "deepseek-chat")
        self.assertEqual(dlg.ed_base_url.text(), "https://api.deepseek.com")
        dlg.check_key()
        self.assertIn("DEEPSEEK_API_KEY", dlg.status.text())
        self.ai.found = False
        dlg.check_key()
        self.assertIn("没有在", dlg.status.text())
        dlg.ed_env.setText("")
        dlg.save()
        self.assertNotEqual(dlg.result(), 1)
        dlg.ed_env.setText("1BAD")
        dlg.save()
        self.assertIn("不合法", dlg.status.text())
        # 误把 Key 粘进来：提示填变量名，Key 从输入框里去掉，任何地方都不回显
        secret = "sk-0123456789abcdef0123456789abcdef"
        dlg.ed_env.setText(f"MY_KEY, {secret}")
        dlg.save()
        self.assertNotEqual(dlg.result(), 1)
        self.assertIn("API Key 本身", dlg.status.text())
        self.assertNotIn(secret, dlg.status.text())
        self.assertEqual(dlg.ed_env.text(), "MY_KEY")
        dlg.ed_env.setText(secret)
        dlg.check_key()
        self.assertIn("API Key 本身", dlg.status.text())
        self.assertNotIn(secret, dlg.status.text() + dlg.ed_env.text())
        dlg.deleteLater()
        # 配置文件损坏（load_config 改用默认配置）：提示保存时会先备份原文件
        dlg = AISettingsDialog(None, self.ai, {"_config_error": "Expecting value"}, save_fn=lambda v: v)
        self.assertFalse(dlg.status.isHidden())
        self.assertIn("备份", dlg.status.text())
        dlg.deleteLater()


@unittest.skipUnless(HAVE_QT, "PySide6 不可用")
class TestMainWindowAI(unittest.TestCase):
    def setUp(self):
        from kmcf.gui.main_window import MainWindow
        _app()
        self.tmp = Path(tempfile.mkdtemp(prefix="kmcf_ai_win_"))
        for d in ("2026-10-09", "2026-10-12"):
            (self.tmp / f"{d}.html").write_text(f"<html><body>{d}</body></html>", encoding="utf-8")
            (self.tmp / f"{d}.json").write_text(json.dumps(_report(d)), encoding="utf-8")

        class Sched:
            def status(self):
                return {"installed": False}

        def list_reports():
            return [(D.fromisoformat(p.stem), p) for p in sorted(self.tmp.glob("*.html"), reverse=True)]

        self.ai = FakeAI()
        self.settings = QSettings(str(self.tmp / "settings.ini"), QSettings.Format.IniFormat)
        self.win = MainWindow(self.settings, generate_fn=lambda **k: None, scheduler=Sched(),
                              list_reports_fn=list_reports, latest_fn=lambda: D(2026, 10, 12),
                              calendar_fn=lambda: False, ai_module=self.ai)
        self.win.resize(1280, 820)
        self.win.show()
        self.win.start()
        _wait(lambda: self.win._sched_status is not None and self.win._latest_task is None)

    def tearDown(self):
        from kmcf.gui.tasks import wait_all
        self.ai.release.set()
        self.assertTrue(wait_all(5000))
        _app().processEvents()
        self.win.close()
        self.win.deleteLater()
        _app().processEvents()
        for _ in range(50):
            shutil.rmtree(self.tmp, ignore_errors=True)
            if not self.tmp.exists():
                break
            _app().processEvents()
            time.sleep(0.02)

    def test_sidebar_button_and_dock(self):
        from PySide6.QtWidgets import QDockWidget
        w = self.win
        side = w.btn_generate.parentWidget().layout()
        idx = [side.indexOf(x) for x in (w.btn_generate, w.btn_ai, w.list)]
        self.assertEqual(idx, sorted(idx))  # 「生成最新复盘」下方、历史列表上方
        self.assertEqual(w.btn_ai.text(), "AI 分析")
        self.assertIn("Ctrl+K", w.btn_ai.toolTip())
        self.assertIsNone(w._ai_dock)  # 第一次打开时才创建
        w.btn_ai.click()
        self.assertTrue(w.btn_ai.isChecked())
        dock = w._ai_dock
        self.assertIsInstance(dock, QDockWidget)
        self.assertFalse(dock.isHidden())
        self.assertEqual(w.dockWidgetArea(dock), Qt.DockWidgetArea.RightDockWidgetArea)
        self.assertTrue(dock.features() & QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        _app().processEvents()
        self.assertTrue(380 <= dock.width() <= 460, dock.width())
        self.assertGreater(w.view.width(), 360)  # 报告和对话并排
        panel = w._ai_panel
        self.assertEqual(panel.report_date(), D(2026, 10, 12))
        # 切换报告：面板跟着切换
        w.list.setCurrentRow(1)
        self.assertEqual(panel.report_date(), D(2026, 10, 9))
        # 再点一次关闭
        w.btn_ai.click()
        self.assertTrue(dock.isHidden())
        self.assertFalse(w.btn_ai.isChecked())
        self.assertTrue(w.toggle_ai_panel())  # Ctrl+K 同一个入口
        self.assertTrue(w.btn_ai.isChecked())
        self.assertIs(w._ai_dock, dock)  # 复用同一个面板

    def test_close_dock_or_window_stops_request(self):
        w = self.win
        w.toggle_ai_panel(True)
        panel = w._ai_panel
        self.ai.mode = "block"
        self.assertTrue(panel.send("问题"))
        _wait(lambda: "部分内容" in panel.chat.toPlainText())
        w._ai_dock.close()  # 关闭面板：停止回答
        self.assertTrue(self.ai.cancels[0].is_set())
        _wait(lambda: not panel.is_busy())
        w.toggle_ai_panel(True)
        self.assertTrue(panel.send("问题二"))
        _wait(lambda: len(self.ai.cancels) == 2)
        self.assertTrue(w.close())  # 关闭主窗口：取消请求，线程安全结束
        self.assertTrue(self.ai.cancels[1].is_set())
        from kmcf.gui.tasks import wait_all
        self.assertTrue(wait_all(3000))
        self.assertEqual(str(self.settings.value("ai/open")).lower(), "true")

    def test_narrow_window_keeps_report_readable(self):
        """面板打开后窗口缩到最小：报告区至少 360 像素，窗口最小宽度随之变大；关闭面板后恢复。"""
        from kmcf.gui import main_window as mw
        w = self.win
        w.toggle_ai_panel(True)
        _app().processEvents()
        w.resize(900, 560)
        for _ in range(5):
            _app().processEvents()
        self.assertGreaterEqual(w.view.width(), mw.REPORT_MIN_WIDTH)
        self.assertGreaterEqual(w._ai_dock.width(), w._ai_dock.minimumWidth())
        self.assertGreaterEqual(w.width(), mw.SIDEBAR_WIDTH + mw.REPORT_MIN_WIDTH + w._ai_dock.minimumWidth())
        w.toggle_ai_panel(False)
        for _ in range(5):
            _app().processEvents()
        self.assertEqual(w.minimumWidth(), mw.WINDOW_MIN_SIZE[0])

    def test_quit_while_generating_hides_floating_panel(self):
        w = self.win
        w.toggle_ai_panel(True)
        w._ai_dock.setFloating(True)
        _app().processEvents()

        class FakeTask:
            def __init__(self):
                self.cancelled = False

            def cancel(self):
                self.cancelled = True

        task = FakeTask()
        w._gen_task = task
        try:
            with _Patch(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)):
                w.close()
            _app().processEvents()
            self.assertTrue(task.cancelled)
            self.assertTrue(w._quit_pending)
            self.assertFalse(w.isVisible())
            self.assertTrue(w._ai_dock.isHidden())                         # 浮动面板不能留在屏幕上
            self.assertEqual(str(self.settings.value("ai/open")).lower(), "true")   # 下次启动仍恢复面板
            w.request_show()                                                # 再次启动程序：撤销退出
            _app().processEvents()
            self.assertFalse(w._quit_pending)
            self.assertFalse(w._ai_dock.isHidden())
        finally:
            w._quit_timer.stop()
            w._gen_task = None
            _app().setQuitOnLastWindowClosed(True)

    def test_panel_load_failure(self):
        """kmcf.ai 缺失或导入出错：提示一次，其他功能不受影响。"""
        from unittest import mock
        w = self.win
        shown = []
        w._ai_module = None  # 使用真正的 import kmcf.ai，这里让它导入失败
        with _Patch(QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a[2]))), \
                mock.patch.dict(sys.modules, {"kmcf.ai": None}), self.assertLogs("kmcf", level="ERROR"):
            self.assertFalse(w.toggle_ai_panel())
        self.assertIn("AI 分析模块加载失败", shown[0])
        self.assertFalse(w.btn_ai.isChecked())
        self.assertIsNone(w._ai_dock)

    def test_reopen_at_startup(self):
        from kmcf.gui.main_window import MainWindow
        self.settings.setValue("ai/open", True)
        self.settings.setValue("ai/width", 380)
        w2 = MainWindow(self.settings, generate_fn=lambda **k: None, scheduler=self.win._scheduler,
                        list_reports_fn=self.win._list_reports_fn, latest_fn=lambda: D(2026, 10, 12),
                        calendar_fn=lambda: False, ai_module=self.ai)
        try:
            w2.resize(1280, 820)
            w2.show()
            w2.start()
            _wait(lambda: w2._ai_dock is not None and w2._latest_task is None and w2._sched_status is not None)
            self.assertTrue(w2.btn_ai.isChecked())
            _app().processEvents()
            self.assertTrue(360 <= w2._ai_dock.width() <= 400, w2._ai_dock.width())
        finally:
            w2.close()
            w2.deleteLater()
            _app().processEvents()


if __name__ == "__main__":
    unittest.main()
