"""AI 分析模块（kmcf.ai）的单元测试：全部使用假响应，不联网、不读取真实 API Key。"""

import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from kmcf import ai, config, net, paths, report  # noqa: E402
from kmcf.ai import client, compliance, prompts, review, settings  # noqa: E402
from kmcf.render import render_report  # noqa: E402

_LOGGER = logging.getLogger("kmcf")
_OLD_LEVEL = _LOGGER.level


def setUpModule():
    _LOGGER.setLevel(logging.CRITICAL)   # 测试里故意制造的错误不刷屏


def tearDownModule():
    _LOGGER.setLevel(_OLD_LEVEL)


TEST_KEY = "sk-unittest-0123456789abcdef"
TEST_KEY2 = "sk-unittest-second-9876543210"
ENV_A, ENV_B = "KMCF_UNITTEST_KEY_A", "KMCF_UNITTEST_KEY_B"


def cfg_with(**ai_kw) -> dict:
    a = {"api_key_env": [ENV_A, ENV_B]}
    a.update(ai_kw)
    return {"ai": a}


class FakeResp:
    """requests.Response 的最小替身：status_code / headers / iter_content / close。"""

    def __init__(self, status=200, chunks=(), headers=None, fail_at=None, on_chunk=None, gap=0.0):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = [c if isinstance(c, bytes) else c.encode("utf-8") for c in chunks]
        self.fail_at = fail_at
        self.on_chunk = on_chunk
        self.gap = gap            # 每块之前等待的秒数（模拟慢流、保活）
        self.closed = False

    def iter_content(self, chunk_size=None):
        for i, c in enumerate(self._chunks):
            if self.gap:
                time.sleep(self.gap)
            if self.closed:
                raise requests.exceptions.ConnectionError("connection closed")
            if self.fail_at is not None and i == self.fail_at:
                raise requests.exceptions.ConnectionError("Read timed out.")
            if self.on_chunk:
                self.on_chunk(i)
            yield c

    def close(self):
        self.closed = True


def json_resp(content="你好", reasoning=None, status=200, finish="stop"):
    msg = {"role": "assistant", "content": content}
    if reasoning:
        msg["reasoning_content"] = reasoning
    body = json.dumps({"choices": [{"index": 0, "message": msg, "finish_reason": finish}]}, ensure_ascii=False)
    return FakeResp(status, ["\n\n", body])   # 非流式排队时服务器会先发空行保活


def error_resp(status, message="", headers=None):
    body = json.dumps({"error": {"message": message, "type": "x"}}) if message else ""
    return FakeResp(status, [body], headers=headers)


def sse_chunks(pieces, reasoning=(), split=7) -> list[bytes]:
    """把若干正文片段编成 SSE 字节流，再按 split 字节切块（会切断 JSON 和多字节汉字）。"""
    events = [": keep-alive\n\n"]
    for r in reasoning:
        events.append("data: " + json.dumps({"choices": [{"delta": {"reasoning_content": r}}]}, ensure_ascii=False) + "\n\n")
    for p in pieces:
        events.append("data: " + json.dumps({"choices": [{"delta": {"content": p}}]}, ensure_ascii=False) + "\r\n\r\n")
    events.append("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}) + "\n\n")
    events.append("data: [DONE]\n\n")
    raw = "".join(events).encode("utf-8")
    return [raw[i:i + split] for i in range(0, len(raw), split)]


class KeyEnvMixin:
    """测试期间：环境变量里只有测试 Key，注册表读取被替换，不触碰真实 Key。"""

    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k not in (ENV_A, ENV_B)}
        env[ENV_A] = TEST_KEY
        self._env = mock.patch.dict(os.environ, env, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)
        self.reg = mock.patch.object(settings, "_registry_env", return_value=None).start()
        mock.patch.object(client.net, "configure").start()
        self.sleep = mock.patch.object(client, "_sleep").start()
        self.addCleanup(mock.patch.stopall)


# ---------------------------------------------------------------- 设置与 Key

class SettingsTest(unittest.TestCase):
    def test_defaults(self):
        s = ai.ai_settings({})
        self.assertEqual(s["provider"], "DeepSeek")
        self.assertEqual(s["base_url"], "https://api.deepseek.com")
        self.assertEqual(s["model"], "deepseek-chat")
        self.assertEqual(s["models"], ["deepseek-chat", "deepseek-reasoner"])
        self.assertEqual(s["api_key_env"], ["DEEPSEEK_API_KEY", "DEEPSEEK-API"])
        self.assertTrue(s["enabled"])
        self.assertTrue(s["auto_review"])
        self.assertFalse(s["include_holdings"])
        self.assertEqual(s["timeout"], 120)
        self.assertEqual(s["temperature"], 0.3)
        self.assertEqual(s["review_budget"], 90)
        self.assertEqual(ai.ai_settings(None), s)
        self.assertEqual(ai.ai_settings({"ai": {"review_budget": 5000}})["review_budget"], 300)
        self.assertEqual(ai.ai_settings({"ai": {"review_budget": 1}})["review_budget"], 20)

    def test_secret_like_env_names_ignored(self):
        for secret in ("sk-0123456789abcdef0123456789abcdef", "0123456789abcdef0123456789abcdef", "a1B2c3D4e5F6g7H8i9"):
            self.assertTrue(config.looks_like_secret(secret), secret)
            s = ai.ai_settings({"ai": {"api_key_env": [secret]}})
            self.assertEqual(s["api_key_env"], ["DEEPSEEK_API_KEY", "DEEPSEEK-API"])
            self.assertNotIn(secret, json.dumps(s))
        for name in ("DEEPSEEK_API_KEY", "DEEPSEEK-API", "MY_OPENAI_KEY_2", "KMCF_UNITTEST_KEY_A", "OPENAI_API_KEY"):
            self.assertFalse(config.looks_like_secret(name), name)
        s = ai.ai_settings({"ai": {"api_key_env": ["sk-0123456789abcdef0123", "MY_KEY"]}})
        self.assertEqual(s["api_key_env"], ["MY_KEY"])

    def test_save_config_strips_ai_defaults(self):
        """保存自选股等配置时，不把合并进来的 AI 默认值固化进文件；用户改过的项保留。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text(json.dumps({"top_n": 30}), encoding="utf-8")
            with mock.patch.object(paths, "CONFIG_PATH", p), mock.patch.object(paths, "ensure_dirs"):
                cfg = config.load_config()
                config.save_config(cfg)
                self.assertNotIn("ai", json.loads(p.read_text(encoding="utf-8")))
                cfg = config.load_config()
                cfg["ai"]["model"] = "deepseek-reasoner"
                cfg["ai"]["custom"] = 1
                config.save_config(cfg)
                self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["ai"], {"model": "deepseek-reasoner", "custom": 1})
                self.assertEqual(config.load_config()["ai"]["model"], "deepseek-reasoner")
        with tempfile.TemporaryDirectory() as d:   # 首次运行写出的默认配置里也没有 ai
            p = Path(d) / "config.json"
            with mock.patch.object(paths, "CONFIG_PATH", p), mock.patch.object(paths, "ensure_dirs"):
                config.load_config()
            self.assertNotIn("ai", json.loads(p.read_text(encoding="utf-8")))

    def test_merge_and_normalize(self):
        s = ai.ai_settings({"ai": {"model": "my-model", "extra": {"a": 1}, "timeout": "abc", "temperature": 9,
                                   "include_holdings": "false", "enabled": "yes", "api_key_env": "MY_KEY",
                                   "base_url": "https://example.com/v1/"}})
        self.assertEqual(s["model"], "my-model")
        self.assertIn("my-model", s["models"])
        self.assertIn("deepseek-reasoner", s["models"])
        self.assertEqual(s["extra"], {"a": 1})           # 保留用户的额外字段
        self.assertEqual(s["timeout"], 120)               # 非法值回到默认
        self.assertEqual(s["temperature"], 2.0)           # 超范围截断
        self.assertFalse(s["include_holdings"])
        self.assertTrue(s["enabled"])
        self.assertEqual(s["api_key_env"], ["MY_KEY"])
        self.assertEqual(s["base_url"], "https://example.com/v1")
        self.assertEqual(client.endpoint(s["base_url"]), "https://example.com/v1/chat/completions")
        bad = ai.ai_settings({"ai": {"base_url": "ftp://x", "model": "", "api_key_env": []}})
        self.assertEqual(bad["base_url"], "https://api.deepseek.com")
        self.assertEqual(bad["model"], "deepseek-chat")
        self.assertEqual(bad["api_key_env"], ["DEEPSEEK_API_KEY", "DEEPSEEK-API"])

    def test_load_config_deep_merges_ai(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            original = json.dumps({"top_n": 30, "ai": {"model": "deepseek-reasoner", "custom": "x"}}, ensure_ascii=False)
            p.write_text(original, encoding="utf-8")
            with mock.patch.object(paths, "CONFIG_PATH", p), mock.patch.object(paths, "ensure_dirs"):
                cfg = config.load_config()
            self.assertEqual(cfg["ai"]["model"], "deepseek-reasoner")
            self.assertEqual(cfg["ai"]["custom"], "x")
            self.assertTrue(cfg["ai"]["enabled"])
            self.assertEqual(cfg["ai"]["base_url"], "https://api.deepseek.com")
            self.assertFalse(cfg["ai"]["include_holdings"])
            self.assertEqual(p.read_text(encoding="utf-8"), original)   # 读取不改写文件

    def test_load_config_without_ai_gets_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text(json.dumps({"top_n": 30}), encoding="utf-8")
            with mock.patch.object(paths, "CONFIG_PATH", p), mock.patch.object(paths, "ensure_dirs"):
                cfg = config.load_config()
            self.assertEqual(cfg["ai"], config.DEFAULT_AI)
            self.assertIsNot(cfg["ai"], config.DEFAULT_AI)
            self.assertNotIn("api_key", cfg["ai"])   # 配置里没有存放 Key 的字段（只有变量名列表 api_key_env）

    def test_deep_merge(self):
        out = config.deep_merge({"a": {"b": 1, "c": 2}, "l": [1]}, {"a": {"b": 9, "d": 4}, "l": [2, 3], "x": 1})
        self.assertEqual(out, {"a": {"b": 9, "c": 2, "d": 4}, "l": [2, 3], "x": 1})

    def test_net_direct_domain(self):
        self.assertIn("deepseek.com", net.DIRECT_DOMAINS)


class KeyStatusTest(KeyEnvMixin, unittest.TestCase):
    def test_found_in_env_without_leaking_value(self):
        st = ai.key_status(cfg_with())
        self.assertTrue(st["found"])
        self.assertEqual(st["env"], ENV_A)
        self.assertEqual(st["source"], "env")
        self.assertEqual(st["provider"], "DeepSeek")
        self.assertEqual(st["model"], "deepseek-chat")
        dumped = json.dumps(st, ensure_ascii=False) + repr(st)
        self.assertNotIn(TEST_KEY, dumped)
        self.assertNotIn(TEST_KEY[-6:], dumped)

    def test_registry_fallback(self):
        del os.environ[ENV_A]
        self.reg.side_effect = lambda name: TEST_KEY2 if name == ENV_B else None
        st = ai.key_status(cfg_with())
        self.assertEqual((st["found"], st["env"], st["source"]), (True, ENV_B, "registry"))
        self.assertNotIn(TEST_KEY2, repr(st))

    def test_not_found(self):
        del os.environ[ENV_A]
        st = ai.key_status(cfg_with())
        self.assertEqual((st["found"], st["env"]), (False, None))

    def test_invalid_and_duplicate_keys(self):
        os.environ[ENV_A] = "  '密钥'  "
        self.assertEqual(settings.find_keys([ENV_A]), [])
        os.environ[ENV_A] = f'  "{TEST_KEY}" '
        os.environ[ENV_B] = TEST_KEY
        keys = settings.find_keys([ENV_A, ENV_B])
        self.assertEqual([(k[0], k[1]) for k in keys], [(ENV_A, TEST_KEY)])   # 去引号、去空白、同值去重


# ---------------------------------------------------------------- chat

class ChatTest(KeyEnvMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.post = mock.patch.object(client.requests, "post").start()
        self.msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "今天怎么样"}]

    def sent(self, i=-1) -> tuple[dict, dict]:
        args, kwargs = self.post.call_args_list[i]
        return kwargs["headers"], json.loads(kwargs["data"].decode("utf-8"))

    def test_non_stream_success_and_request_shape(self):
        self.post.return_value = json_resp("今日大盘下跌。", reasoning="思考")
        deltas, reasons = [], []
        out = ai.chat(self.msgs, cfg_with(), max_tokens=200, on_delta=deltas.append, on_reasoning=reasons.append)
        self.assertEqual(out, "今日大盘下跌。")
        self.assertEqual((deltas, reasons), (["今日大盘下跌。"], ["思考"]))
        args, kwargs = self.post.call_args
        self.assertEqual(args[0], "https://api.deepseek.com/chat/completions")
        headers, body = self.sent()
        self.assertEqual(headers["Authorization"], f"Bearer {TEST_KEY}")
        self.assertTrue(headers["User-Agent"].startswith("kmcf/"))
        self.assertEqual(body["model"], "deepseek-chat")
        self.assertEqual(body["messages"], self.msgs)
        self.assertFalse(body["stream"])
        self.assertEqual(body["max_tokens"], 200)
        self.assertEqual(body["temperature"], 0.3)
        self.assertTrue(self.post.return_value.closed)

    def test_reasoner_omits_temperature(self):
        self.post.return_value = json_resp("好")
        ai.chat(self.msgs, cfg_with(), model="deepseek-reasoner")
        _, body = self.sent()
        self.assertEqual(body["model"], "deepseek-reasoner")
        self.assertNotIn("temperature", body)

    def test_stream_sse(self):
        pieces = ["今日", "上证指数", "下跌0.43%，", "成交额1.54万亿。"]
        self.post.return_value = FakeResp(200, sse_chunks(pieces, reasoning=["先看", "指数"], split=5))
        deltas, reasons = [], []
        out = ai.chat(self.msgs, cfg_with(), stream=True, on_delta=deltas.append, on_reasoning=reasons.append)
        self.assertEqual(out, "".join(pieces))
        self.assertEqual(deltas, pieces)
        self.assertEqual(reasons, ["先看", "指数"])
        _, body = self.sent()
        self.assertTrue(body["stream"])

    def test_stream_without_done_marker(self):
        # 有 finish_reason、只是没有 [DONE]：完整
        raw = ('data: {"choices":[{"delta":{"content":"甲"}}]}\n\n'
               'data: {"choices":[{"delta":{"content":"乙"},"finish_reason":"stop"}]}')
        self.post.return_value = FakeResp(200, [raw])
        out = ai.chat(self.msgs, cfg_with(), stream=True)
        self.assertEqual((out, out.finish_reason), ("甲乙", "stop"))

    def test_stream_closed_early_is_incomplete(self):
        """既没有 [DONE] 也没有 finish_reason（含半截 JSON）就断开：不能当成完整回答。"""
        for raw in ('data: {"choices":[{"delta":{"content":"前半段"}}]}\n\ndata: {"choices":[{"del',
                    'data: {"choices":[{"delta":{"content":"前半段"}}]}\n\n'):
            self.post.reset_mock()
            self.post.return_value = FakeResp(200, [raw])
            with self.assertRaises(ai.AIError) as cm:
                ai.chat(self.msgs, cfg_with(), stream=True)
            self.assertEqual(cm.exception.partial, "前半段")
            self.assertEqual(cm.exception.kind, "incomplete")
            self.assertIn("不完整", str(cm.exception))
            self.assertEqual(self.post.call_count, 1)

    def test_finish_length_reported(self):
        raw = ('data: {"choices":[{"delta":{"content":"很长的回答"}}]}\n\n'
               'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\ndata: [DONE]\n\n')
        self.post.return_value = FakeResp(200, [raw])
        out = ai.chat(self.msgs, cfg_with(), stream=True)
        self.assertIsInstance(out, ai.AIText)
        self.assertEqual(out, "很长的回答")
        self.assertTrue(out.truncated)
        self.post.return_value = json_resp("短", finish="length")
        self.assertEqual(ai.chat(self.msgs, cfg_with()).finish_reason, "length")

    def test_malformed_events_skipped(self):
        """choices 里是 null / 字符串、delta 不是 dict 等异常事件直接跳过，不抛 AttributeError。"""
        events = ['{"choices":[null]}', '{"choices":"x"}', '{"choices":[{"delta":"x"}]}', '[1,2]',
                  '{"choices":[{"delta":{"content":5}}]}', '{"choices":[{"delta":{"content":"好"},"finish_reason":"stop"}]}']
        raw = "".join(f"data: {e}\n\n" for e in events) + "data: [DONE]\n\n"
        self.post.return_value = FakeResp(200, [raw])
        self.assertEqual(ai.chat(self.msgs, cfg_with(), stream=True), "好")
        self.post.return_value = FakeResp(200, ['{"choices":[null]}'])
        with self.assertRaises(ai.AIError):
            ai.chat(self.msgs, cfg_with())

    def test_cr_only_line_endings(self):
        events = [json.dumps({"choices": [{"delta": {"content": p}}]}, ensure_ascii=False) for p in ("甲", "乙")]
        events.append(json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}))
        raw = ("".join(f"data: {e}\r\r" for e in events) + "data: [DONE]\r\r").encode("utf-8")
        for split in (1, 3, 1000):   # \r 正好落在块末尾时也不能出错
            self.post.return_value = FakeResp(200, [raw[i:i + split] for i in range(0, len(raw), split)])
            self.assertEqual(ai.chat(self.msgs, cfg_with(), stream=True), "甲乙", split)
        mixed = ('data: {"choices":[{"delta":{"content":"一"}}]}\r\n\r\n'
                 'data: {"choices":[{"delta":{"content":"二"},"finish_reason":"stop"}]}\r')
        self.post.return_value = FakeResp(200, [mixed[:47], mixed[47:]])
        self.assertEqual(ai.chat(self.msgs, cfg_with(), stream=True), "一二")

    def test_bad_retry_after(self):
        for value in ("-5", "nan", "inf", "abc"):
            self.assertIsNone(client._retry_after(FakeResp(429, headers={"Retry-After": value})), value)
        self.post.side_effect = [error_resp(429, "x", {"Retry-After": "-5"}), json_resp("好")]
        self.assertEqual(ai.chat(self.msgs, cfg_with()), "好")
        self.assertGreaterEqual(self.sleep.call_args.args[0], 0)
        client._sleep(-1, None)   # 负数不会让 time.sleep 抛 ValueError（报告生成时 cancel=None）

    def test_no_redirects(self):
        self.post.return_value = json_resp("好")
        ai.chat(self.msgs, cfg_with())
        self.assertIs(self.post.call_args.kwargs["allow_redirects"], False)
        self.post.return_value = error_resp(302)
        with self.assertRaises(ai.AIError) as cm:
            ai.chat(self.msgs, cfg_with())
        self.assertIn("302", str(cm.exception))

    def test_unexpected_exception_becomes_aierror(self):
        class Boom(FakeResp):
            def iter_content(self, chunk_size=None):
                raise AttributeError("'NoneType' object has no attribute 'get'")
                yield b""

        self.post.return_value = Boom(200)
        with self.assertRaises(ai.AIError) as cm:
            ai.chat(self.msgs, cfg_with(), stream=True, retries=0)
        self.assertIn("无法处理", str(cm.exception))

    def test_endpoint_safety(self):
        """http 只允许本机；接口地址不是 DeepSeek 时不能用 DeepSeek 默认的 Key 环境变量。"""
        os.environ["DEEPSEEK_API_KEY"] = TEST_KEY
        for ai_cfg in ({"base_url": "http://api.deepseek.com"},
                       {"base_url": "https://evil.example.com/v1"},
                       {"base_url": "https://evil.example.com/v1", "api_key_env": ["MY_KEY", "DEEPSEEK-API"]}):
            with self.assertRaises(ai.AIError) as cm:
                ai.chat(self.msgs, {"ai": ai_cfg})
            self.assertNotIn(TEST_KEY, str(cm.exception))
        self.post.assert_not_called()
        self.assertIn("https", str(ai.AIError(ai.endpoint_problem(ai.ai_settings({"ai": {"base_url": "http://x.com"}})))))
        self.post.side_effect = lambda *a, **k: json_resp("好")
        ai.chat(self.msgs, cfg_with(base_url="https://other.example.com/v1"))   # 单独的变量名：可以
        self.assertEqual(self.post.call_args.args[0], "https://other.example.com/v1/chat/completions")
        ai.chat(self.msgs, cfg_with(base_url="http://127.0.0.1:8000/v1"))        # 本机 http：可以
        ai.chat(self.msgs, {"ai": {"base_url": "https://api.deepseek.com/v1"}})   # DeepSeek 自己：可以
        self.assertEqual(self.post.call_count, 3)


    def test_401_message_and_no_leak(self):
        self.post.return_value = error_resp(401, f"Authentication Fails, Your api key: ****{TEST_KEY[-4:]} is invalid")
        with self.assertRaises(ai.AIError) as cm:
            ai.chat(self.msgs, cfg_with())
        msg = str(cm.exception)
        self.assertIn("API Key 无效", msg)
        self.assertIn(ENV_A, msg)
        self.assertNotIn(TEST_KEY, msg)
        self.assertNotIn(TEST_KEY[-4:], msg)
        self.assertEqual(cm.exception.status, 401)
        self.assertEqual(self.post.call_count, 1)   # 401 不重试
        self.sleep.assert_not_called()

    def test_401_falls_back_to_second_key(self):
        os.environ[ENV_B] = TEST_KEY2
        self.post.side_effect = [error_resp(401, "bad"), json_resp("好")]
        self.assertEqual(ai.chat(self.msgs, cfg_with()), "好")
        self.assertEqual(self.sent(0)[0]["Authorization"], f"Bearer {TEST_KEY}")
        self.assertEqual(self.sent(1)[0]["Authorization"], f"Bearer {TEST_KEY2}")

    def test_402_no_retry(self):
        self.post.return_value = error_resp(402, "Insufficient Balance")
        with self.assertRaises(ai.AIError) as cm:
            ai.chat(self.msgs, cfg_with())
        self.assertIn("余额不足", str(cm.exception))
        self.assertIn("Insufficient Balance", str(cm.exception))
        self.assertEqual(self.post.call_count, 1)

    def test_429_retries_then_fails(self):
        self.post.side_effect = [error_resp(429, "Rate Limit", {"Retry-After": "5"}) for _ in range(3)]
        with self.assertRaises(ai.AIError) as cm:
            ai.chat(self.msgs, cfg_with())
        self.assertIn("请求太频繁", str(cm.exception))
        self.assertEqual(self.post.call_count, 3)   # 首次 + 重试 2 次
        self.assertEqual(self.sleep.call_count, 2)
        self.assertEqual(self.sleep.call_args_list[0].args[0], 5.0)

    def test_503_then_success(self):
        self.post.side_effect = [error_resp(503, "Server Overloaded"), json_resp("好")]
        self.assertEqual(ai.chat(self.msgs, cfg_with()), "好")
        self.assertEqual(self.post.call_count, 2)

    def test_422_detail_scrubbed(self):
        self.post.return_value = error_resp(422, f"Invalid param, key {TEST_KEY} Bearer {TEST_KEY}")
        with self.assertRaises(ai.AIError) as cm:
            ai.chat(self.msgs, cfg_with())
        self.assertIn("参数错误", str(cm.exception))
        self.assertNotIn(TEST_KEY, str(cm.exception))

    def test_timeout_retries(self):
        self.post.side_effect = requests.exceptions.ReadTimeout("Read timed out. (read timeout=120)")
        with self.assertRaises(ai.AIError) as cm:
            ai.chat(self.msgs, cfg_with())
        self.assertIn("超时", str(cm.exception))
        self.assertEqual(self.post.call_count, 3)

    def test_body_read_timeout_non_stream_retries(self):
        self.post.side_effect = [FakeResp(200, ["\n", "{"], fail_at=1), json_resp("好")]
        self.assertEqual(ai.chat(self.msgs, cfg_with()), "好")

    def test_connection_error(self):
        self.post.side_effect = requests.exceptions.ConnectionError("Max retries exceeded")
        with self.assertRaises(ai.AIError) as cm:
            ai.chat(self.msgs, cfg_with())
        self.assertIn("无法连接", str(cm.exception))

    def test_stream_broken_after_content_keeps_partial(self):
        first = 'data: {"choices":[{"delta":{"content":"第一段"}}]}\n\n'
        resp = FakeResp(200, [first, 'data: {"choices":[{"delta":{"con', "x"], fail_at=1)
        self.post.return_value = resp
        with self.assertRaises(ai.AIError) as cm:
            ai.chat(self.msgs, cfg_with(), stream=True)
        self.assertEqual(cm.exception.partial, "第一段")
        self.assertIn("不完整", str(cm.exception))
        self.assertEqual(self.post.call_count, 1)   # 已经输出了内容就不再重试

    def test_cancel_during_stream(self):
        cancel = threading.Event()
        pieces = ["一", "二", "三", "四"]

        def on_delta(_):
            cancel.set()

        resp = FakeResp(200, sse_chunks(pieces, split=1000))
        self.post.return_value = resp
        out = ai.chat(self.msgs, cfg_with(), stream=True, on_delta=on_delta, cancel=cancel)
        self.assertEqual(out, "一")
        self.assertTrue(resp.closed)

    def test_cancel_before_start(self):
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(ai.chat(self.msgs, cfg_with(), cancel=cancel), "")
        self.post.assert_not_called()

    def test_callback_base_exception_propagates_and_closes(self):
        class Stop(BaseException):
            pass

        resp = FakeResp(200, sse_chunks(["一", "二"]))
        self.post.return_value = resp

        def boom(_):
            raise Stop()

        with self.assertRaises(Stop):
            ai.chat(self.msgs, cfg_with(), stream=True, on_delta=boom)
        self.assertTrue(resp.closed)

    def test_no_key(self):
        del os.environ[ENV_A]
        with self.assertRaises(ai.AIError) as cm:
            ai.chat(self.msgs, cfg_with())
        self.assertIn("未找到", str(cm.exception))
        self.assertIn(ENV_A, str(cm.exception))
        self.post.assert_not_called()

    def test_disabled(self):
        with self.assertRaises(ai.AIError):
            ai.chat(self.msgs, cfg_with(enabled=False))
        self.post.assert_not_called()

    def test_empty_answer(self):
        self.post.return_value = json_resp("", finish="content_filter")
        with self.assertRaises(ai.AIError) as cm:
            ai.chat(self.msgs, cfg_with())
        self.assertIn("内容安全", str(cm.exception))

    def test_bad_messages(self):
        with self.assertRaises(ai.AIError):
            ai.chat([{"role": "tool", "content": "x"}], cfg_with())
        with self.assertRaises(ai.AIError):
            ai.chat([], cfg_with())


class ChatTimingTest(KeyEnvMixin, unittest.TestCase):
    """取消 / 总时长 / 首包超时在“等服务器第一个字节”和“只收到保活行”时都要及时生效（真实的等待，不打桩 sleep）。"""

    def setUp(self):
        super().setUp()
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.post = mock.patch.object(client.requests, "post").start()
        self.msgs = [{"role": "user", "content": "问"}]

    def hang(self, *a, **k):   # 服务器接受连接但一直不响应
        self.release.wait(10)
        raise requests.exceptions.ReadTimeout("Read timed out.")

    def keepalive(self, n=200, gap=0.05):
        return FakeResp(200, [": keep-alive\n\n"] * n, gap=gap)

    def test_cancel_while_waiting_for_first_byte(self):
        self.post.side_effect = self.hang
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        t0 = time.monotonic()
        out = ai.chat(self.msgs, cfg_with(), stream=True, cancel=cancel)
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertEqual((out, out.finish_reason), ("", "cancelled"))

    def test_cancel_during_keepalive(self):
        resp = self.keepalive()
        self.post.return_value = resp
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        t0 = time.monotonic()
        ai.chat(self.msgs, cfg_with(), stream=True, cancel=cancel)
        self.assertLess(time.monotonic() - t0, 1.5)
        time.sleep(0.2)
        self.assertTrue(resp.closed)   # 请求线程随后也会结束、关闭响应

    def test_deadline_with_keepalive_only(self):
        self.post.return_value = self.keepalive()
        t0 = time.monotonic()
        with self.assertRaises(ai.AIError) as cm:
            ai.chat(self.msgs, cfg_with(), stream=True, deadline=time.monotonic() + 0.6)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(cm.exception.kind, "deadline")
        self.assertEqual(self.post.call_count, 1)

    def test_deadline_while_waiting_and_no_retry_past_deadline(self):
        self.post.side_effect = self.hang
        t0 = time.monotonic()
        with self.assertRaises(ai.AIError) as cm:
            ai.chat(self.msgs, cfg_with(), deadline=time.monotonic() + 0.5)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(cm.exception.kind, "deadline")

    def test_slow_stream_bounded_by_deadline(self):
        chunks = ['data: {"choices":[{"delta":{"content":"字"}}]}\n\n' for _ in range(100)]
        self.post.return_value = FakeResp(200, chunks, gap=0.05)
        with self.assertRaises(ai.AIError) as cm:
            ai.chat(self.msgs, cfg_with(), stream=True, deadline=time.monotonic() + 0.8)
        self.assertEqual(cm.exception.kind, "deadline")
        self.assertTrue(cm.exception.partial.startswith("字"))

    def test_first_content_timeout_counts_keepalive(self):
        self.post.return_value = self.keepalive()
        t0 = time.monotonic()
        with self.assertRaises(ai.AIError) as cm:
            ai.chat(self.msgs, cfg_with(), stream=True, timeout=0.5, retries=0)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(cm.exception.kind, "timeout")
        self.assertIn("超时", str(cm.exception))

    def test_heartbeat_runs_in_caller_thread_and_can_abort(self):
        class Stop(BaseException):
            pass

        self.post.side_effect = self.hang
        seen = []

        def beat():
            seen.append(threading.get_ident())
            if len(seen) >= 2:
                raise Stop()

        t0 = time.monotonic()
        with self.assertRaises(Stop):
            ai.chat(self.msgs, cfg_with(), stream=True, heartbeat=beat)
        self.assertLess(time.monotonic() - t0, 3.5)
        self.assertEqual(set(seen), {threading.get_ident()})


# ---------------------------------------------------------------- 上下文

def kpi(label, value, sub="", tone="neutral"):
    return {"label": label, "value": value, "sub": sub, "tone": tone}


def table(title, labels, rows):
    cols = [{"key": f"c{i}", "label": lb, "fmt": "text", "sign": False, "align": "left"} for i, lb in enumerate(labels)]
    return {"type": "table", "title": title, "columns": cols, "span": "full", "note": "", "empty": not rows,
            "rows": [[{"text": str(x), "tone": "neutral", "v": str(x)} for x in r] for r in rows]}


def sample_report(n_rows=5) -> dict:
    market = {
        "id": "market", "title": "大盘与情绪",
        "summary": ["上证指数下跌0.43%。", "涨停46家。"],
        "kpis": [kpi("上证指数", "-0.43%", "收 3,797.24", "down"), kpi("涨停家数", "46")],
        "blocks": [
            {"type": "chart", "title": "上证指数走势图", "option": {"series": [{"data": [111111, 222222]}]},
             "height": 300, "span": "full", "note": ""},
            {"type": "ladder", "title": "连板梯队", "span": "full", "note": "",
             "levels": [{"label": "10板", "items": [{"text": "新华传媒", "sub": "出版", "tone": "up"}]}]},
            table("涨停池", ["名称", "代码", "涨跌幅"], [[f"股票{i}", f"{600000 + i}", "+10.00%"] for i in range(n_rows)]),
        ],
        "notes": ["涨停数据来自东方财富。"], "errors": [],
    }
    watch = {
        "id": "watchlist", "title": "自选股/持仓",
        "summary": ["自选股2只：上涨2只。", "持仓2只，市值98,765.43元，当日盈亏+1,234元，累计浮动盈亏+5,678元。"],
        "kpis": [kpi("持仓市值", "98,765.43元", "2 只持仓"), kpi("持仓当日盈亏", "+1,234元", "+1.27%", "up"),
                 kpi("累计浮动盈亏", "+5,678元", "收益率 +6.1%", "up"), kpi("持仓数量", "2 只", "当日盈利 2 · 亏损 0"),
                 kpi("自选股涨 / 跌", "2 / 0", "共 2 只")],
        "blocks": [
            table("自选股总览", ["名称", "代码", "涨跌幅", "备注"], [["贵州茅台", "600519", "+1.98%", "我的秘密备注成本1200"]]),
            table("持仓明细", ["名称", "股数", "成本价", "市值", "浮动盈亏"], [["贵州茅台", "300", "1,201.00", "38,640", "+2,600"]]),
            {"type": "chart", "title": "持仓市值占比", "option": {}, "height": 260, "span": "half", "note": ""},
        ],
        "notes": ["当日盈亏 = 持股数 × (收盘价 − 昨收)。", "量比 = 当日成交量 ÷ 前 5 日均量。"],
        "errors": [],
    }
    return {"version": "t", "trade_date": "2026-10-12", "weekday": "周一", "generated_at": "2026-10-12 19:00",
            "snapshot_ok": True, "banners": ["这是横幅提示"], "sections": [market, watch],
            "stats": {"network": 1, "cache": 0, "failed": 0}, "elapsed": 1.0,
            "ai": {"blocks": [{"type": "p", "text": "旧的AI点评不应再发给AI"}]}}


HOLDING_STRINGS = ("98,765.43", "1,234", "5,678", "持仓明细", "成本价", "浮动盈亏", "持仓市值", "持仓数量",
                   "我的秘密备注", "持股数", "1,201.00", "38,640")


class ReportContextTest(unittest.TestCase):
    def test_excludes_holdings_by_default(self):
        text = ai.report_context(sample_report())
        for s in HOLDING_STRINGS:
            self.assertNotIn(s, text, s)
        self.assertIn("自选股2只：上涨2只。", text)
        self.assertIn("自选股涨 / 跌：2 / 0", text)
        self.assertIn("贵州茅台|600519|+1.98%", text)   # 总览保留，但去掉了备注列
        self.assertIn("量比", text)                     # 无关口径说明保留

    def test_includes_holdings_when_asked(self):
        text = ai.report_context(sample_report(), include_holdings=True)
        for s in ("98,765.43", "持仓明细", "成本价", "我的秘密备注"):
            self.assertIn(s, text)

    def test_content_and_no_charts(self):
        text = ai.report_context(sample_report())
        self.assertIn("复盘日：2026-10-12 周一", text)
        self.assertIn("这是横幅提示", text)
        self.assertIn("## 大盘与情绪", text)
        self.assertIn("- 上证指数：-0.43%（收 3,797.24）", text)
        self.assertIn("名称|代码|涨跌幅", text)
        self.assertIn("10板：新华传媒（出版）", text)
        self.assertNotIn("上证指数走势图", text)
        self.assertNotIn("111111", text)
        self.assertNotIn("旧的AI点评", text)
        self.assertNotIn("19:00", text)   # 生成时间不进上下文（否则缓存永远不命中）

    def test_budget(self):
        rep = sample_report(n_rows=200)
        full = ai.report_context(rep, max_chars=100000)
        self.assertIn("列出前 15 行", full)
        small = ai.report_context(rep, max_chars=900)
        self.assertLessEqual(len(small), 900)
        self.assertIn("大盘与情绪", small)
        self.assertEqual(ai.report_context(None), "")
        self.assertEqual(ai.report_context({}), "")

    def test_ranking_table_head_and_tail(self):
        rep = sample_report()
        rep["sections"][0]["blocks"].append(table("行业板块涨跌幅排行", ["行业", "涨跌幅"],
                                                  [[f"行业{i}", f"{5 - i * 0.1:+.2f}%"] for i in range(90)]))
        text = ai.report_context(rep)
        self.assertIn("行业0|", text)
        self.assertIn("行业89|", text)
        self.assertIn("中间", text)

    @unittest.skipUnless((paths.REPORT_DIR / "2026-10-12.json").exists(), "没有样例报告")
    def test_real_report(self):
        rep = json.loads((paths.REPORT_DIR / "2026-10-12.json").read_text(encoding="utf-8"))
        text = ai.report_context(rep)
        self.assertLessEqual(len(text), 15000)
        self.assertIn("大盘与情绪", text)
        self.assertIn("龙虎榜", text)


class HistoryContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "history.csv"
        p = mock.patch.object(paths, "HISTORY_PATH", self.path)
        p.start()
        self.addCleanup(p.stop)

    def write(self):
        lines = ["date,zt_count,turnover,funds_lhb_net,持仓市值,max_board_names,empty_col"]
        for i in range(1, 31):
            lines.append(f"2026-09-{i:02d},{i},{1.5e12 + i * 1e8},{-2.5e8},{98765},名{i},")
        self.path.write_text("\n".join(lines), encoding="utf-8-sig")

    def test_missing_file(self):
        self.assertEqual(ai.history_context(), "")

    def test_rows_until_and_privacy(self):
        self.write()
        text = ai.history_context(5, "2026-09-20")
        rows = [ln for ln in text.splitlines() if ln.startswith("2026-")]
        self.assertEqual([r[:10] for r in rows], [f"2026-09-{d}" for d in range(16, 21)])
        self.assertIn("涨停", text)
        self.assertIn("两市成交额(亿)", text)
        self.assertIn("15020", rows[-1])          # 1.5e12 + 20e8 元 = 15020 亿
        self.assertIn("-2.50", rows[-1])
        self.assertNotIn("持仓", text)
        self.assertNotIn("98765", text)
        self.assertNotIn("empty_col", text)        # 整列为空不输出
        with_hold = ai.history_context(5, "2026-09-20", include_holdings=True)
        self.assertIn("持仓市值", with_hold)
        self.assertEqual(len([ln for ln in ai.history_context(20).splitlines() if ln.startswith("2026-")]), 20)


class PromptTest(unittest.TestCase):
    def test_system_prompt_rules(self):
        sp = ai.SYSTEM_PROMPT
        for s in ("A股复盘数据分析助手", "不提供任何投资建议", "目标价", "预测", "简体中文", "Markdown", "没有这项信息"):
            self.assertIn(s, sp)
        self.assertIn("250~450", ai.REVIEW_PROMPT)
        self.assertIn("不要标题", ai.REVIEW_PROMPT)

    def test_quick_prompts(self):
        labels = [x[0] for x in ai.QUICK_PROMPTS]
        for lb in ("总结今日市场", "板块轮动分析", "龙虎榜与资金解读", "短线情绪分析", "我的自选股表现", "对比近几日变化"):
            self.assertIn(lb, labels)
        for lb, q in ai.QUICK_PROMPTS:
            self.assertTrue(q.strip())
            self.assertEqual(compliance.compliance_hits(q), [], lb)

    def test_build_messages(self):
        with mock.patch.object(prompts, "history_context", return_value="HIST-TABLE") as hc:
            hist = [{"role": "assistant", "content": "孤立的回答"}, {"role": "user", "content": "问1"},
                    {"role": "assistant", "content": "答1"}, {"role": "tool", "content": "x"},
                    {"role": "user", "content": "  "}]
            msgs = ai.build_messages("问2", sample_report(), {}, history=hist)
        # 开头孤立的 assistant、未知角色、空内容都被去掉
        self.assertEqual([m["role"] for m in msgs], ["system", "user", "assistant", "user"])
        self.assertTrue(msgs[0]["content"].startswith(ai.SYSTEM_PROMPT))
        self.assertIn("## 大盘与情绪", msgs[0]["content"])
        self.assertIn("HIST-TABLE", msgs[0]["content"])
        self.assertNotIn("98,765.43", msgs[0]["content"])
        self.assertEqual(msgs[-1], {"role": "user", "content": "问2"})
        self.assertEqual(hc.call_args.args[1], "2026-10-12")
        msgs = ai.build_messages("问", sample_report(), {}, include_holdings=True)
        self.assertIn("98,765.43", msgs[0]["content"])
        msgs = ai.build_messages("问", None, {})
        self.assertIn("没有打开的复盘报告", msgs[0]["content"])


# ---------------------------------------------------------------- 合规与解析

class ComplianceTest(unittest.TestCase):
    def test_terms_constant(self):
        self.assertIsInstance(ai.FORBIDDEN_TERMS, tuple)
        self.assertIsInstance(ai.FORBIDDEN_PATTERNS, tuple)
        for t in ("建议买入", "建议", "推荐", "目标价", "有望", "大概率", "看好", "重点关注", "上涨空间", "补涨"):
            self.assertIn(t, ai.FORBIDDEN_TERMS)
        # 加仓 / 止损 / 抄底 / 翻倍 只在操作语境里算违规（客观数据里也会出现）
        for t in ("可以加仓", "及时止损", "逢低抄底", "有翻倍空间"):
            self.assertTrue(ai.compliance_hits(t), t)

    MUST_HIT = ("建议投资者关注半导体板块", "不建议买入该股", "可以持有宁德时代", "看好半导体板块", "推荐宁德时代",
                "半导体板块值得关注", "可适当关注券商板块", "宜回避高位股", "投资者可逢低关注", "预计明天高开",
                "明日大概率震荡上行", "有望冲击20元", "建议仓位不超过五成", "存在较大上涨空间", "可能迎来补涨",
                "不建议买入高位股，可以持有低位蓝筹。", "控制仓位", "切勿追高", "保持观望", "下个交易日或将高开",
                "不构成投资建议，建议关注券商", "我不做预测，但明天大概率上涨", "建议寻求买入机会")
    MUST_PASS = ("两市成交额较前一日翻倍", "融资客加仓银行股", "跌停后止损盘涌出",
                 "龙虎榜净买入2.39亿，机构席位净卖出6.27亿", "北向资金抄底银行股。", "基金重仓股普遍下跌。",
                 "抱歉，我只做客观数据分析，不提供买卖时机、仓位或个股推荐等投资建议。",
                 "如需投资建议，建议咨询持牌投资顾问。", "您的问题涉及投资建议，我只能提供客观数据。",
                 "值得关注的是，跌停家数增加到30家。", "股价站上5日均线，均线上行。", "节后首个交易日上证指数上涨1.2%。",
                 "超跌板块今日迎来反弹。", "仓位占比12%。", "我无法预测明日涨跌停家数。", "成交额超预期放大。")

    def test_reviewer_cases(self):
        for t in self.MUST_HIT:
            self.assertTrue(ai.compliance_hits(t), t)
        for t in self.MUST_PASS:
            self.assertEqual(ai.compliance_hits(t), [], t)
        self.assertEqual(ai.compliance_hits("建议逢低买入"), ["建议逢低买入"])   # 重叠的命中合并成一条

    def test_scrub_reviewer_sample(self):
        md = ("涨停家数78家；建议投资者关注机器人板块。不建议买入高位股，可以持有低位蓝筹。"
              "两市成交额较前一日翻倍。融资客加仓银行股。")
        blocks, n = review.scrub_blocks(ai.markdown_blocks(md))
        text = review.blocks_text(blocks)
        self.assertEqual(n, 2)
        self.assertNotIn("建议", text)
        self.assertNotIn("持有", text)
        self.assertIn("翻倍", text)
        self.assertIn("加仓银行股", text)

    def test_hits(self):
        self.assertEqual(ai.compliance_hits("建议买入新华传媒。"), ["建议买入"])
        self.assertIn("目标价", ai.compliance_hits("目标价15元"))
        self.assertTrue(ai.compliance_hits("明日大概率继续反弹"))
        self.assertTrue(ai.compliance_hits("后市有望上涨"))
        self.assertTrue(ai.compliance_hits("投资者应该逢低买入"))
        self.assertTrue(ai.compliance_hits("可以考虑减仓"))

    def test_objective_text_passes(self):
        ok = ("龙虎榜当日口径净买入2.39亿，机构席位净卖出6.27亿；主力净流入最多的是金健米业。"
              "昨日涨停股今日平均涨幅+1.81%，连板晋级率26.1%。以上不构成投资建议，本助手不提供买卖建议，也不做预测。"
              "炸板率35.2%，较前一日上升。")
        self.assertEqual(ai.compliance_hits(ok), [])

    def test_scrub(self):
        text, n = compliance.scrub_text("成交额1.54万亿。建议逢低买入。涨停46家。")
        self.assertEqual(text, "成交额1.54万亿。涨停46家。")
        self.assertEqual(n, 1)


class MarkdownTest(unittest.TestCase):
    def test_blocks(self):
        md = ("## 今日复盘\n\n**今日**两市成交额1.54万亿，\n较前日缩量。\n\n"
              "- 要点**一**：涨停46家\n* 要点二 `代码`\n  续行\n1. 要点三\n2、要点四\n\n---\n"
              "1.54万亿是今天的成交额。\n\n[链接文字](http://x)结尾")
        blocks = ai.markdown_blocks(md)
        self.assertEqual(blocks[0], {"type": "p", "text": "今日复盘"})
        self.assertEqual(blocks[1], {"type": "p", "text": "今日两市成交额1.54万亿，较前日缩量。"})
        self.assertEqual(blocks[2], {"type": "ul", "items": ["要点一：涨停46家", "要点二 代码续行", "要点三", "要点四"]})
        self.assertEqual(blocks[3], {"type": "p", "text": "1.54万亿是今天的成交额。"})
        self.assertEqual(blocks[4], {"type": "p", "text": "链接文字结尾"})
        for b in blocks:
            self.assertNotIn("*", json.dumps(b, ensure_ascii=False))

    def test_loose_list_and_paragraph_after_list(self):
        blocks = ai.markdown_blocks("概述一句。\n\n- 要点一\n\n- 要点二\n\n结尾段落。")
        self.assertEqual(blocks, [{"type": "p", "text": "概述一句。"}, {"type": "ul", "items": ["要点一", "要点二"]},
                                  {"type": "p", "text": "结尾段落。"}])

    def test_scrub_blocks(self):
        blocks, n = review.scrub_blocks([{"type": "p", "text": "指数下跌。建议减仓。"},
                                         {"type": "ul", "items": ["后市看好", "涨停46家"]}])
        self.assertEqual(blocks, [{"type": "p", "text": "指数下跌。"}, {"type": "ul", "items": ["涨停46家"]}])
        self.assertEqual(n, 2)


# ---------------------------------------------------------------- review_for_report

REVIEW_MD = "今日三大指数全部下跌，两市成交额1.54万亿。\n\n- 涨停46家，炸板率35.2%。\n- 龙虎榜净买入2.39亿。"


class ReviewTest(KeyEnvMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        mock.patch.object(paths, "CACHE_DIR", Path(self.tmp.name)).start()
        self.chat = mock.patch.object(review, "chat").start()
        mock.patch.object(prompts, "history_context", return_value="").start()

    def test_disabled_or_no_key_returns_none(self):
        self.assertIsNone(ai.review_for_report(sample_report(), cfg_with(enabled=False)))
        self.assertIsNone(ai.review_for_report(sample_report(), cfg_with(auto_review=False)))
        del os.environ[ENV_A]
        self.assertIsNone(ai.review_for_report(sample_report(), cfg_with()))
        self.chat.assert_not_called()

    def test_success_then_cache(self):
        self.chat.return_value = REVIEW_MD
        said = []
        out = ai.review_for_report(sample_report(), cfg_with(), progress=said.append)
        self.assertFalse(out["cached"])
        self.assertEqual(out["provider"], "DeepSeek")
        self.assertEqual(out["model"], "deepseek-chat")
        self.assertEqual(out["blocks"], [{"type": "p", "text": "今日三大指数全部下跌，两市成交额1.54万亿。"},
                                         {"type": "ul", "items": ["涨停46家，炸板率35.2%。", "龙虎榜净买入2.39亿。"]}])
        self.assertIn("AI 复盘点评", said)
        msgs = self.chat.call_args.args[0]
        self.assertTrue(self.chat.call_args.kwargs["stream"])
        self.assertEqual(msgs[-1]["content"], ai.REVIEW_PROMPT)
        self.assertNotIn("98,765.43", json.dumps(msgs, ensure_ascii=False))   # 默认不发送持仓
        files = list((Path(self.tmp.name) / "ai").glob("2026-10-12_deepseek-chat_*.json"))
        self.assertEqual(len(files), 1)
        self.assertNotIn(TEST_KEY, files[0].read_text(encoding="utf-8"))

        rep2 = sample_report()
        rep2["generated_at"] = "2026-10-12 21:30"   # 只是生成时间不同：命中缓存
        rep2["elapsed"] = 99
        again = ai.review_for_report(rep2, cfg_with())
        self.assertTrue(again["cached"])
        self.assertEqual(again["blocks"], out["blocks"])
        self.assertEqual(self.chat.call_count, 1)

        rep3 = sample_report()
        rep3["sections"][0]["summary"].append("新的数据")   # 数据变了：重新请求
        ai.review_for_report(rep3, cfg_with())
        self.assertEqual(self.chat.call_count, 2)
        ai.review_for_report(sample_report(), cfg_with(model="deepseek-reasoner"))   # 换模型：重新请求
        self.assertEqual(self.chat.call_count, 3)

    def test_include_holdings_setting(self):
        self.chat.return_value = REVIEW_MD
        ai.review_for_report(sample_report(), cfg_with(include_holdings=True))
        self.assertIn("98,765.43", json.dumps(self.chat.call_args.args[0], ensure_ascii=False))

    def test_compliance_retry(self):
        self.chat.side_effect = ["今日指数下跌。建议逢低买入新华传媒。\n\n- 涨停46家。", REVIEW_MD]
        out = ai.review_for_report(sample_report(), cfg_with())
        self.assertEqual(self.chat.call_count, 2)
        retry_msgs = self.chat.call_args_list[1].args[0]
        self.assertEqual(retry_msgs[-2]["role"], "assistant")
        self.assertIn("逢低买入", retry_msgs[-1]["content"])
        self.assertNotIn("逢低", json.dumps(out["blocks"], ensure_ascii=False))

    def test_compliance_still_bad_removes_sentences(self):
        self.chat.side_effect = ["今日指数下跌。建议加仓。\n\n- 涨停46家。\n- 后市看好。", "今日指数下跌。明日有望上涨。"]
        out = ai.review_for_report(sample_report(), cfg_with())
        self.assertEqual(out["blocks"], [{"type": "p", "text": "今日指数下跌。"}])

    def test_all_removed_is_error(self):
        self.chat.side_effect = ["建议买入。", "建议加仓。"]
        out = ai.review_for_report(sample_report(), cfg_with())
        self.assertIn("error", out)

    def test_ai_error(self):
        self.chat.side_effect = ai.AIError("账户余额不足（402）")
        out = ai.review_for_report(sample_report(), cfg_with())
        self.assertEqual(out, {"error": "账户余额不足（402）"})
        self.assertEqual(list(Path(self.tmp.name).rglob("*.json")), [])   # 失败不缓存

    def test_progress_cancel_propagates(self):
        class Cancelled(BaseException):
            pass

        def progress(msg):
            raise Cancelled()

        with self.assertRaises(Cancelled):
            ai.review_for_report(sample_report(), cfg_with(), progress=progress)

    def test_budget_passed_to_chat(self):
        self.chat.return_value = REVIEW_MD
        t0 = time.monotonic()
        ai.review_for_report(sample_report(), cfg_with(review_budget=45))
        kw = self.chat.call_args.kwargs
        self.assertAlmostEqual(kw["deadline"] - t0, 45, delta=2)
        self.assertEqual(kw["retries"], review.REVIEW_RETRIES)
        self.assertTrue(callable(kw["heartbeat"]))

    def test_deadline_error(self):
        self.chat.side_effect = ai.AIError("AI 回答超时", kind="deadline")
        out = ai.review_for_report(sample_report(), cfg_with())
        self.assertIn("AI 点评超时（90 秒内未完成），已跳过", out["error"])
        self.assertEqual(list(Path(self.tmp.name).rglob("*.json")), [])

    def test_rewrite_skipped_when_time_is_short(self):
        self.chat.side_effect = ["今日指数下跌。建议加仓。\n\n- 涨停46家。", REVIEW_MD]
        out = ai.review_for_report(sample_report(), cfg_with(review_budget=20))   # 剩余时间不足 25 秒
        self.assertEqual(self.chat.call_count, 1)
        self.assertNotIn("加仓", json.dumps(out["blocks"], ensure_ascii=False))

    def test_truncated_not_cached(self):
        self.chat.return_value = ai.AIText("今日指数下跌。\n\n- 涨停46家。\n- 龙虎榜净买入2.39亿，机构", "length")
        out = ai.review_for_report(sample_report(), cfg_with())
        self.assertEqual(out["blocks"], [{"type": "p", "text": "今日指数下跌。"}, {"type": "ul", "items": ["涨停46家。"]}])
        self.assertEqual(list(Path(self.tmp.name).rglob("*.json")), [])   # 不完整的点评不缓存
        ai.review_for_report(sample_report(), cfg_with())
        self.assertEqual(self.chat.call_count, 2)

    def test_cache_hit_is_rechecked(self):
        self.chat.return_value = REVIEW_MD
        ai.review_for_report(sample_report(), cfg_with())
        path = next((Path(self.tmp.name) / "ai").glob("*.json"))
        data = json.loads(path.read_text(encoding="utf-8"))
        data["blocks"] = [{"type": "p", "text": "今日指数下跌。建议买入半导体龙头。"}, {"type": "ul", "items": ["后市看好"]}]
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        out = ai.review_for_report(sample_report(), cfg_with())
        self.assertTrue(out["cached"])
        self.assertEqual(out["blocks"], [{"type": "p", "text": "今日指数下跌。"}])
        self.assertNotIn("建议", out["text"])
        self.assertEqual(self.chat.call_count, 1)
        data["blocks"] = [{"type": "p", "text": "建议买入半导体龙头。"}]   # 删光了：当作没有缓存，重新生成
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        out = ai.review_for_report(sample_report(), cfg_with())
        self.assertFalse(out["cached"])
        self.assertEqual(self.chat.call_count, 2)


class ReviewCancelIntegrationTest(KeyEnvMixin, unittest.TestCase):
    """真实的 chat：服务器迟迟不响应时，生成报告的“取消”（进度回调抛出）在一两秒内生效。"""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        mock.patch.object(paths, "CACHE_DIR", Path(self.tmp.name)).start()
        mock.patch.object(prompts, "history_context", return_value="").start()
        self.release = threading.Event()
        self.addCleanup(self.release.set)

        def hang(*a, **k):
            self.release.wait(10)
            raise requests.exceptions.ReadTimeout("Read timed out.")

        mock.patch.object(client.requests, "post", side_effect=hang).start()

    def test_cancel_via_progress_heartbeat(self):
        class Cancelled(BaseException):
            pass

        calls = []

        def progress(msg):
            calls.append(msg)
            if len(calls) >= 3:   # 第一次是“AI 复盘点评”，之后是等待中的心跳
                raise Cancelled()

        t0 = time.monotonic()
        with self.assertRaises(Cancelled):
            report._ai_review(sample_report(), cfg_with(), lambda pct, msg: progress(msg))
        self.assertLess(time.monotonic() - t0, 4.0)


class ReportIntegrationTest(unittest.TestCase):
    def test_ai_review_never_breaks_report(self):
        with mock.patch("kmcf.ai.review_for_report", side_effect=RuntimeError("boom")):
            out = report._ai_review(sample_report(), {}, lambda p, m: None)
        self.assertIn("error", out)

    def test_ai_review_cancel_propagates(self):
        class Cancelled(BaseException):
            pass

        with mock.patch("kmcf.ai.review_for_report", side_effect=Cancelled()):
            with self.assertRaises(Cancelled):
                report._ai_review(sample_report(), {}, lambda p, m: None)

    def test_template_card(self):
        rep = sample_report()
        rep["ai"] = {"blocks": [{"type": "p", "text": "概述<script>"}, {"type": "ul", "items": ["要点甲", "要点乙"]}],
                     "provider": "DeepSeek", "model": "deepseek-chat", "cached": False}
        html = render_report(rep)
        self.assertIn('id="ai-review"', html)
        self.assertIn('href="#ai-review"', html)
        self.assertIn("由 DeepSeek（deepseek-chat）根据本报告数据生成 · 仅供参考，不构成投资建议", html)
        self.assertIn("<li>要点甲</li>", html)
        self.assertIn("概述&lt;script&gt;", html)
        self.assertLess(html.index('id="highlights"'), html.index('id="ai-review"'))
        self.assertLess(html.index('id="ai-review"'), html.index('id="market"'))

        rep["ai"] = {"error": "API Key 无效（401）"}
        html = render_report(rep)
        self.assertIn("AI 复盘点评未生成：API Key 无效（401）", html)

        rep["ai"] = None
        html = render_report(rep)
        self.assertNotIn('id="ai-review"', html)
        self.assertNotIn('href="#ai-review"', html)
        del rep["ai"]
        self.assertNotIn('id="ai-review"', render_report(rep))


if __name__ == "__main__":
    unittest.main()
