"""自选股/持仓模块的单元测试（全部离线，用合成数据）。

运行：.venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import datetime as dt
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kmcf.context import Context  # noqa: E402
from kmcf.fetch import FetchError  # noqa: E402
from kmcf.history import History  # noqa: E402
from kmcf.render import sanitize  # noqa: E402
from kmcf.section import DOWN, UP  # noqa: E402
from kmcf.sections import _watchlist_calc as calc  # noqa: E402
from kmcf.sections import _watchlist_src as src  # noqa: E402
from kmcf.sections import watchlist  # noqa: E402

TD = dt.date(2026, 10, 9)
ADVICE_WORDS = ("建议", "买入", "卖出", "持有", "加仓", "减仓", "止损", "止盈", "看好", "看空", "推荐", "机会")


def make_raw(end=TD, n=130, start_price=10.0, end_price=20.0, last_volume=None, code="600001", name=None):
    """合成 stock_zh_a_hist 格式的日K（中文列名）。"""
    days = pd.bdate_range(end=pd.Timestamp(end), periods=n).date
    close = np.linspace(start_price, end_price, n).round(2)
    vol = np.full(n, 1e5)
    if last_volume is not None:
        vol[-1] = last_volume
    df = pd.DataFrame({
        "日期": days, "股票代码": code, "开盘": (close * 0.99).round(2), "收盘": close,
        "最高": (close * 1.01).round(2), "最低": (close * 0.98).round(2), "成交量": vol,
        "成交额": close * vol * 100, "振幅": 3.0, "涨跌幅": pd.Series(close).pct_change().mul(100).values,
        "涨跌额": 0.0, "换手率": 1.0,
    })
    if name is not None:
        df = df.assign(名称=name)
    return df


def em_lhb_raw(rows=None):
    """合成东方财富数据中心龙虎榜明细（_funds_data.em_lhb_detail 的原始英文列，每个上榜原因一行）。"""
    rows = rows or [
        ("000420", "吉林化纤", "有价格涨跌幅限制的日收盘价格涨幅偏离值达到7%的前五只证券", 10.12, 1.7838e8, 2.335e8, 5.51e7, "3家机构买入"),
        ("600825", "新华传媒", "有价格涨跌幅限制的连续3个交易日内收盘价格涨幅偏离值累计达到20%的证券",
         10.0, 4.977e7, 1.1346e8, 6.369e7, "普通席位买入"),
        ("600825", "新华传媒", "非ST、*ST和S证券连续10个交易日内（含当日）收盘价格涨幅偏离值累计达到100%的证券",
         10.0, 0.0, 1.126e9, 1.126e9, "普通席位买入"),
    ]
    return pd.DataFrame([{
        "SECURITY_CODE": c, "SECURITY_NAME_ABBR": n, "EXPLANATION": r, "CHANGE_RATE": p, "CLOSE_PRICE": 10.0,
        "BILLBOARD_NET_AMT": net, "BILLBOARD_BUY_AMT": b, "BILLBOARD_SELL_AMT": s, "BILLBOARD_DEAL_AMT": b + s,
        "ACCUM_AMOUNT": 6.26e8, "DEAL_NET_RATIO": net / 6.26e8 * 100, "TURNOVERRATE": 4.78, "FREE_MARKET_CAP": 1e10,
        "EXPLAIN": e} for c, n, r, p, net, b, s, e in rows])


def k_of(rows):
    """[(date, open, close, high, low, volume)] -> 标准化日K。"""
    df = pd.DataFrame(rows, columns=["日期", "开盘", "收盘", "最高", "最低", "成交量"])
    return calc.normalize_kline(df, rows[-1][0])


# ---------------------------------------------------------------- 数据源辅助

class TestSrc(unittest.TestCase):
    def test_market_of(self):
        cases = {"600519": "sh", "688981": "sh", "000001": "sz", "300750": "sz", "301001": "sz",
                 "920799": "bj", "430047": "bj", "830799": "bj", "900901": "sh", "200002": "sz",
                 "510300": "sh", "159915": "sz"}
        for code, mk in cases.items():
            self.assertEqual(src.market_of(code), mk, code)
        self.assertIsNone(src.market_of("abc"))
        self.assertEqual(src.market_of(519), "sz")  # 数字会补齐前导 0 -> 000519

    def test_em_supported(self):
        self.assertTrue(src.em_supported("600519"))
        self.assertTrue(src.em_supported("300750"))
        self.assertTrue(src.em_supported("920799"))
        self.assertFalse(src.em_supported("510300"))
        self.assertFalse(src.em_supported("900901"))

    def test_limit_ratios(self):
        self.assertEqual(src.limit_ratios("600519", "贵州茅台"), (0.10,))
        self.assertEqual(src.limit_ratios("300750"), (0.20,))
        self.assertEqual(src.limit_ratios("688981"), (0.20,))
        self.assertEqual(src.limit_ratios("920799"), (0.30,))
        self.assertEqual(src.limit_ratios("300001", "ST创业"), (0.20,))
        # 主板 ST：2025-07-07 起 10%，之前 5%；不再同时接受两个候选
        self.assertEqual(src.limit_ratios("600001", "*ST测试", TD), (0.10,))
        self.assertEqual(src.limit_ratios("600001", "*ST测试", dt.date(2025, 7, 7)), (0.10,))
        self.assertEqual(src.limit_ratios("600001", "*ST测试", dt.date(2025, 7, 4)), (0.05,))
        self.assertEqual(src.limit_ratios("000001", "ST测试"), (0.10,))

    def test_http_get_direct_first_and_no_retry_on_timeout(self):
        import requests
        calls = []

        def fake(url, params, headers, trust_env):
            calls.append(trust_env)
            raise requests.ConnectTimeout("timeout")

        with mock.patch.object(src, "_get_text", side_effect=fake), \
                mock.patch.object(src.requests.utils, "get_environ_proxies", return_value={"https": "http://p"}):
            with self.assertRaises(requests.Timeout):
                src.http_get("https://qt.gtimg.cn/q=sh600519")
        self.assertEqual(calls, [False])                       # 超时不换路再等一遍

        calls.clear()

        def refused(url, params, headers, trust_env):
            calls.append(trust_env)
            if not trust_env:
                raise requests.ConnectionError("refused")
            return "ok"

        with mock.patch.object(src, "_get_text", side_effect=refused), \
                mock.patch.object(src.requests.utils, "get_environ_proxies", return_value={"https": "http://p"}):
            self.assertEqual(src.http_get("https://qt.gtimg.cn/q=sh600519"), "ok")
        self.assertEqual(calls, [False, True])                  # 直连被拒，再走系统代理
        calls.clear()
        with mock.patch.object(src, "_get_text", side_effect=refused), \
                mock.patch.object(src.requests.utils, "get_environ_proxies", return_value={}):
            with self.assertRaises(requests.ConnectionError):
                src.http_get("https://qt.gtimg.cn/q=sh600519")
        self.assertEqual(calls, [False])                       # 没配代理就不重试

    def test_with_timeout_keeps_name_and_stops_retrying(self):
        import threading
        ev = threading.Event()

        def stock_zt_pool_em(date):
            ev.wait(5)
            return pd.DataFrame()

        w = src.with_timeout(stock_zt_pool_em, 0.05)
        self.assertEqual(w.__name__, "stock_zt_pool_em")       # 缓存键不变
        with self.assertRaises(TimeoutError):
            w(date="20261009")
        with self.assertRaises(TimeoutError):                   # 第二次立即失败
            w(date="20261009")
        ev.set()
        self.assertEqual(src.with_timeout(lambda: 1, 1)(), 1)

    def test_parse_tx_rows(self):
        rows = [["2026-10-08", "10.00", "10.50", "10.60", "9.90", "1000.00", {}, "1.20", "105.5", ""],
                ["2026-10-09", "10.50", "11.55", "11.55", "10.40", "2000.00", {}, "2.40", "220.0", ""],
                ["bad"]]
        df = src.parse_tx_rows(rows, "600001", prec="10.00")
        self.assertEqual(len(df), 2)
        self.assertAlmostEqual(df["成交额"].iloc[0], 1055000.0)       # 万元 -> 元
        self.assertAlmostEqual(df["涨跌幅"].iloc[0], 5.0)              # 用 prec 算第一根
        self.assertAlmostEqual(df["涨跌幅"].iloc[1], 10.0)
        self.assertAlmostEqual(df["换手率"].iloc[1], 2.40)
        self.assertEqual(df["日期"].iloc[1], dt.date(2026, 10, 9))
        kcb = src.parse_tx_rows(rows[:2], "688001")
        self.assertAlmostEqual(kcb["成交量"].iloc[0], 10.0)            # 科创板：股 -> 手
        self.assertTrue(np.isnan(kcb["涨跌幅"].iloc[0]))                # 没有 prec 时第一根为空
        self.assertTrue(src.parse_tx_rows([], "600001").empty)

    def test_parse_names(self):
        sina = ('var hq_str_sh600519="贵州茅台,1255.420,1255.790";\n'
                'var hq_str_sh688999="";\nvar hq_str_bj920799="艾融软件,29.33";')
        df = src.parse_sina_names(sina)
        self.assertEqual(dict(zip(df["code"], df["name"])), {"600519": "贵州茅台", "920799": "艾融软件"})
        tx = 'v_sz300750="51~宁德时代~300750~297.75";\nv_sh688999="";'
        df = src.parse_tx_names(tx)
        self.assertEqual(dict(zip(df["code"], df["name"])), {"300750": "宁德时代"})


# ---------------------------------------------------------------- 指标

class TestMetrics(unittest.TestCase):
    def test_normalize_truncates_and_dedupes(self):
        raw = make_raw(end=dt.date(2026, 10, 14), n=135)        # 含复盘日之后的数据
        raw = pd.concat([raw, raw.tail(1)], ignore_index=True)  # 重复行
        k = calc.normalize_kline(raw, TD)
        self.assertEqual(k["date"].iloc[-1], TD)
        self.assertTrue(k["date"].is_unique)
        self.assertEqual(list(k.columns), calc.KCOLS)
        self.assertTrue(calc.normalize_kline(None, TD).empty)
        self.assertTrue(calc.normalize_kline(pd.DataFrame(), TD).empty)

    def test_normalize_fills_missing_pct(self):
        raw = make_raw(n=10).drop(columns=["涨跌幅"])
        k = calc.normalize_kline(raw, TD)
        exp = (k["close"].iloc[-1] / k["close"].iloc[-2] - 1) * 100
        self.assertAlmostEqual(k["pct"].iloc[-1], exp)

    def test_uptrend_metrics(self):
        k = calc.normalize_kline(make_raw(last_volume=3e5), TD)
        m = calc.compute_metrics(k, TD)
        self.assertTrue(m["traded"])
        self.assertAlmostEqual(m["close"], 20.0)
        self.assertAlmostEqual(m["ma"][5], k["close"].iloc[-5:].mean())
        self.assertAlmostEqual(m["vol_ratio"], 3.0)
        self.assertEqual(m["ma_text"], "站上全部均线")
        self.assertGreater(m["dist"][20], 0)
        self.assertAlmostEqual(m["chg20"], (20.0 / k["close"].iloc[-21] - 1) * 100)
        self.assertGreater(m["pos60"], 90)
        self.assertTrue(m["new_high60"])
        self.assertFalse(m["new_low60"])
        self.assertAlmostEqual(m["prev_close"], k["close"].iloc[-2])
        self.assertAlmostEqual(m["pct"], (20.0 / k["close"].iloc[-2] - 1) * 100)
        labels = calc.status_labels(m, None, False, False, False)
        self.assertIn("创60日新高", labels)
        self.assertIn("放量", labels)

    def test_downtrend_and_shrink(self):
        k = calc.normalize_kline(make_raw(start_price=20, end_price=10, last_volume=4e4), TD)
        m = calc.compute_metrics(k, TD)
        self.assertEqual(m["ma_text"], "低于全部均线")
        self.assertTrue(m["new_low60"])
        self.assertIn("缩量", calc.status_labels(m, None, False, False, False))

    def test_suspended_and_empty(self):
        k = calc.normalize_kline(make_raw(end=dt.date(2026, 9, 30), n=60), TD)
        m = calc.compute_metrics(k, TD)
        self.assertTrue(m["has_data"])
        self.assertFalse(m["traded"])
        self.assertIsNone(m["pct"])
        self.assertEqual(m["prev_close"], m["close"])
        self.assertEqual(calc.status_labels(m, None, False, False, True), ["停牌/无数据"])
        e = calc.compute_metrics(calc.normalize_kline(None, TD), TD)
        self.assertEqual(calc.status_labels(e, None, False, False, False), ["无数据"])

    def test_short_history(self):
        k = calc.normalize_kline(make_raw(n=8), TD)
        m = calc.compute_metrics(k, TD)
        self.assertIsNotNone(m["ma"][5])
        self.assertIsNone(m["ma"][10])
        self.assertIsNone(m["chg20"])
        self.assertIsNone(m["pos60"])
        self.assertFalse(m["new_high60"])
        self.assertEqual(m["ma_text"], "站上5（不足10日）")       # 不再说“站上全部均线”
        m25 = calc.compute_metrics(calc.normalize_kline(make_raw(n=25), TD), TD)
        self.assertIsNone(m25["pos60"])                            # 不足 60 根不算 60日位置
        self.assertEqual(m25["ma_text"], "站上5/10/20（不足60日）")

    def test_prev_close_from_series_not_rounded_pct(self):
        """东方财富日K的涨跌幅只保留 2 位小数：昨收必须取上一根收盘，否则大仓位当日盈亏差几十元。"""
        days = pd.bdate_range(end=pd.Timestamp(TD), periods=3).date
        raw = pd.DataFrame({"日期": days, "开盘": [1250.0, 1255.0, 1255.42], "收盘": [1250.0, 1255.79, 1263.0],
                            "最高": [1260.0, 1260.0, 1282.0], "最低": [1240.0, 1250.0, 1255.42],
                            "成交量": [3e4, 3e4, 3.5e4], "涨跌幅": [0.1, 0.46, 0.57], "涨跌额": [1.0, 5.79, 7.21]})
        m = calc.compute_metrics(calc.normalize_kline(raw, TD), TD)
        self.assertAlmostEqual(m["prev_close"], 1255.79)
        self.assertAlmostEqual(1000 * (m["close"] - m["prev_close"]), 7210.0, places=6)
        self.assertAlmostEqual(m["pct"], (1263.0 / 1255.79 - 1) * 100)
        rows, tot = calc.holdings([{"code": "600519", "label": "茅台", "shares": 1000, "cost": 0,
                                    "close": m["close"], "prev_close": m["prev_close"], "pct": m["pct"],
                                    "traded": True}])
        self.assertAlmostEqual(tot["day_pnl"], 7210.0, places=6)

    def test_first_day_single_bar(self):
        """上市首日只有一根K线：有涨跌幅时用它反推昨收；没有时标“涨跌幅缺失”，当日盈亏为空而不是 0。"""
        raw = pd.DataFrame({"日期": [TD], "开盘": [25.0], "收盘": [30.0], "最高": [32.0], "最低": [24.0],
                            "成交量": [1e5], "成交额": [3e8]})
        m = calc.compute_metrics(calc.normalize_kline(raw, TD), TD)
        self.assertTrue(m["traded"])
        self.assertIsNone(m["pct"])
        self.assertIsNone(m["prev_close"])
        self.assertEqual(calc.status_labels(m, None, False, False, False), ["涨跌幅缺失"])
        stock = {"code": "301999", "label": "新股", "shares": 100, "cost": 0, "close": 30.0, "prev_close": None,
                 "pct": None, "traded": True, "labels": ["涨跌幅缺失"]}
        rows, tot = calc.holdings([stock])
        self.assertIsNone(rows[0]["day_pnl"])
        self.assertIsNone(tot["day_pnl"])
        self.assertEqual(tot["no_day"], ["新股"])
        text = "".join(calc.summary_lines([stock], tot))
        self.assertNotIn("均无行情数据", text)
        self.assertIn("缺少涨跌幅", text)
        self.assertNotIn("当日盈亏", text)
        m2 = calc.compute_metrics(calc.normalize_kline(raw.assign(涨跌幅=[200.0]), TD), TD)
        self.assertAlmostEqual(m2["prev_close"], 10.0)
        self.assertAlmostEqual(m2["pct"], 200.0)

    def test_cross_ma20(self):
        days = pd.bdate_range(end=pd.Timestamp(TD), periods=25).date
        closes = [10.0] * 23 + [9.0, 11.0]          # 昨日在 20 日线下方，今日站上
        rows = [(d, c, c, c, c, 1e5) for d, c in zip(days, closes)]
        m = calc.compute_metrics(k_of(rows), TD)
        self.assertTrue(m["cross_up20"])
        self.assertIn("上穿20日线", calc.status_labels(m, None, False, False, False))
        closes = [10.0] * 23 + [11.0, 9.0]
        rows = [(d, c, c, c, c, 1e5) for d, c in zip(days, closes)]
        self.assertTrue(calc.compute_metrics(k_of(rows), TD)["cross_down20"])

    def test_ma_text_mixed(self):
        self.assertEqual(calc.ma_text(10, {5: 9, 10: 9.5, 20: 11, 60: 12}), "站上5/10，低于20/60")
        self.assertEqual(calc.ma_text(10, {5: 9, 10: 11, 20: None, 60: None}), "站上5，低于10（不足20日）")
        self.assertEqual(calc.ma_text(10, {5: 11, 10: 12, 20: None, 60: None}), "低于5/10（不足20日）")
        self.assertIsNone(calc.ma_text(10, {5: None, 10: None}))
        self.assertIsNone(calc.ma_text(None, {5: 1}))


# ---------------------------------------------------------------- 涨跌停

class TestLimit(unittest.TestCase):
    def test_limit_price_half_up(self):
        self.assertEqual(calc.limit_price(11.76, 0.10), 12.94)
        self.assertEqual(calc.limit_price(3.46, 0.10), 3.81)
        self.assertEqual(calc.limit_price(1.15, 0.10), 1.27)   # 1.265 -> 四舍五入 1.27
        self.assertEqual(calc.limit_price(10.0, 0.10, up=False), 9.0)
        self.assertEqual(calc.limit_price(24.35, 0.30), 31.66)

    def _k(self, prev, o, c, h, lo, end=TD):
        days = pd.bdate_range(end=pd.Timestamp(end), periods=8).date
        rows = [(d, prev, prev, prev, prev, 1e5) for d in days[:-1]] + [(days[-1], o, c, h, lo, 2e5)]
        return k_of(rows)

    def test_infer_main_board(self):
        self.assertEqual(calc.infer_limit(self._k(10, 10, 11, 11, 10), TD, "600001"),
                         {"zt": True, "zb": False, "dt": False})
        self.assertEqual(calc.infer_limit(self._k(10, 10, 10.5, 11, 10), TD, "600001"),
                         {"zt": False, "zb": True, "dt": False})
        self.assertEqual(calc.infer_limit(self._k(10, 10, 9, 10, 9), TD, "600001"),
                         {"zt": False, "zb": False, "dt": True})
        self.assertEqual(calc.infer_limit(self._k(10, 10, 10.9, 10.95, 10), TD, "600001"),
                         {"zt": False, "zb": False, "dt": False})

    def test_infer_boards(self):
        self.assertTrue(calc.infer_limit(self._k(10, 10, 12, 12, 10), TD, "300001")["zt"])
        self.assertFalse(calc.infer_limit(self._k(10, 10, 11, 11, 10), TD, "300001")["zt"])
        self.assertTrue(calc.infer_limit(self._k(10, 10, 13, 13, 10), TD, "920001")["zt"])

    def test_infer_st_current_rule(self):
        """主板 ST 现行 10%：+5% 收盘、冲高 +8% 回落、-5% 收盘都不是涨停/炸板/跌停。"""
        none = {"zt": False, "zb": False, "dt": False}
        st = ("600001", "*ST测试")
        self.assertEqual(calc.infer_limit(self._k(3.0, 3.0, 3.15, 3.15, 3.0), TD, *st), none)
        self.assertEqual(calc.infer_limit(self._k(3.0, 3.0, 3.09, 3.24, 3.0), TD, *st), none)
        self.assertEqual(calc.infer_limit(self._k(3.0, 3.0, 2.85, 3.0, 2.85), TD, *st), none)
        self.assertTrue(calc.infer_limit(self._k(3.0, 3.0, 3.3, 3.3, 3.0), TD, *st)["zt"])
        self.assertTrue(calc.infer_limit(self._k(3.0, 3.1, 3.2, 3.3, 3.0), TD, *st)["zb"])
        self.assertTrue(calc.infer_limit(self._k(3.0, 3.0, 2.7, 3.0, 2.7), TD, *st)["dt"])
        # 2025-07-07 之前按 5%
        old = dt.date(2025, 6, 30)
        self.assertTrue(calc.infer_limit(self._k(3.0, 3.0, 3.15, 3.15, 3.0, end=old), old, *st)["zt"])
        self.assertTrue(calc.infer_limit(self._k(3.0, 3.0, 3.09, 3.15, 3.0, end=old), old, *st)["zb"])
        self.assertFalse(calc.infer_limit(self._k(3.0, 3.0, 3.09, 3.15, 3.0), TD, *st)["zb"])

    def test_infer_skips_new_listing_and_halt(self):
        days = pd.bdate_range(end=pd.Timestamp(TD), periods=3).date
        rows = [(days[0], 10, 10, 10, 10, 1), (days[1], 10, 10, 10, 10, 1), (days[2], 10, 11, 11, 10, 1)]
        self.assertFalse(calc.infer_limit(k_of(rows), TD, "600001")["zt"])
        k = self._k(10, 10, 11, 11, 10)
        self.assertFalse(calc.infer_limit(k, TD + dt.timedelta(days=3), "600001")["zt"])

    def test_status_labels_from_pool(self):
        m = calc.compute_metrics(calc.normalize_kline(make_raw(), TD), TD)
        self.assertEqual(calc.status_labels(m, {"连板数": 3}, False, False, True)[:2], ["3连板", "上龙虎榜"])
        self.assertEqual(calc.status_labels(m, {}, False, False, False)[0], "涨停")
        self.assertIn("炸板", calc.status_labels(m, None, True, False, False))
        self.assertIn("跌停", calc.status_labels(m, None, False, True, False))

    def test_normalize_lhb(self):
        funds_fmt = pd.DataFrame({"code": [420], "name": ["吉林化纤"], "pct": [10.1], "net": [1.78e8],
                                  "buy": [2.3e8], "sell": [5.5e7], "net_ratio": [19.9], "reasons": ["涨幅偏离；换手率"],
                                  "basis": ["当日"], "explain": ["3家机构买入"]})
        df = calc.normalize_lhb(funds_fmt)
        self.assertEqual(list(df.columns), calc.LHB_COLS)
        self.assertEqual(df["code"].iloc[0], "000420")
        self.assertEqual(df["ratio"].iloc[0], 19.9)
        self.assertEqual(df["basis"].iloc[0], "当日")
        self.assertIsNone(calc.normalize_lhb(pd.DataFrame({"代码": ["000420"]})))   # 认不出格式：None 而非空表
        self.assertTrue(calc.normalize_lhb(None).empty)
        self.assertTrue(calc.normalize_lhb(pd.DataFrame()).empty)

    def test_merge_lhb_basis_matches_funds(self):
        """单独运行时用“龙虎榜与资金”模块同一套整理逻辑：每股一行，多日累计口径有标注。"""
        from kmcf.sections import _funds_calc
        raw = em_lhb_raw()
        df = calc.normalize_lhb(_funds_calc.merge_lhb(raw))
        self.assertEqual(df["code"].tolist().count("600825"), 1)
        row = df[df["code"] == "600825"].iloc[0]
        self.assertIn("累计", row["basis"])                     # “3日累计”/“多日累计”等，具体文字由 funds 模块决定
        self.assertAlmostEqual(row["net"], 4.977e7)
        self.assertEqual(df[df["code"] == "000420"].iloc[0]["basis"], "当日")

    def test_pool_codes(self):
        self.assertIsNone(calc.pool_codes(None))
        self.assertEqual(calc.pool_codes(pd.DataFrame()), {})
        d = calc.pool_codes(pd.DataFrame({"代码": [1, "600825"], "名称": ["平安", "新华传媒"]}))
        self.assertEqual(set(d), {"000001", "600825"})
        # 列名变化：视为不可用（None），改用日K推算，而不是“池可用、未命中”
        self.assertIsNone(calc.pool_codes(pd.DataFrame({"股票代码": ["600001"]})))
        # 带 NaN 的浮点代码列
        d = calc.pool_codes(pd.DataFrame({"代码": [600001.0, np.nan], "名称": ["甲", "乙"]}))
        self.assertEqual(set(d), {"600001"})


# ---------------------------------------------------------------- 持仓与历史日期修正

class TestHoldings(unittest.TestCase):
    def test_holdings(self):
        stocks = [
            {"code": "600001", "label": "甲", "shares": 100, "cost": 10, "close": 12, "prev_close": 11.5,
             "pct": 4.35, "traded": True},
            {"code": "600002", "label": "乙", "shares": 200, "cost": 0, "close": 5, "prev_close": 5,
             "pct": None, "traded": False},
            {"code": "600003", "label": "丙", "shares": 0, "cost": 0, "close": 8, "prev_close": 8, "traded": True},
            {"code": "600004", "label": "丁", "shares": 50, "cost": 20, "close": None, "prev_close": None},
        ]
        rows, tot = calc.holdings(stocks)
        self.assertEqual(tot["count"], 3)
        self.assertEqual(tot["valued"], 2)
        self.assertAlmostEqual(tot["mv"], 100 * 12 + 200 * 5)
        self.assertAlmostEqual(tot["day_pnl"], 100 * 0.5)
        self.assertAlmostEqual(tot["day_pct"], 50 / (2200 - 50) * 100)
        self.assertAlmostEqual(tot["float_pnl"], 200)            # 只计填写了成本的
        self.assertAlmostEqual(tot["float_pct"], 20.0)
        self.assertEqual(tot["no_cost"], ["乙"])
        self.assertEqual([r["name"] for r in rows][:2], ["甲", "乙"])  # 按市值排序
        self.assertAlmostEqual(sum(r["weight"] for r in rows if r["weight"]), 100.0)
        self.assertIsNone(rows[-1]["mv"])

    def test_no_holdings(self):
        rows, tot = calc.holdings([{"code": "600001", "shares": 0, "close": 1}])
        self.assertEqual((rows, tot["count"], tot["mv"]), ([], 0, None))

    def test_money(self):
        self.assertEqual(calc.money(3601, signed=True), "+3601元")
        self.assertEqual(calc.money(-120), "-120元")
        self.assertEqual(calc.money(123456), "12.35万")
        self.assertEqual(calc.money(None), "—")

    def test_ex_event(self):
        self.assertFalse(calc.ex_event(1298.0, 1281.6, 1326.0, 1309.6))   # 前复权减去同样的分红：差不变
        self.assertFalse(calc.ex_event(5.0, 5.5, 10.0, 11.0))             # 送转：比不变
        self.assertTrue(calc.ex_event(1326.0, 1281.6, 1326.0, 1309.6))    # 当天除息

    def test_actual_fix(self):
        d0, d1 = dt.date(2026, 5, 29), dt.date(2026, 6, 1)
        kq = k_of([(d0, 1242.58, 1297.98, 1300.98, 1241.98, 1), (d1, 1298.98, 1281.58, 1298.98, 1273.29, 1)])
        ka = k_of([(d0, 1270.6, 1326.0, 1329.0, 1270.0, 1), (d1, 1327.0, 1309.6, 1327.0, 1301.31, 1)])
        fix = calc.actual_fix(kq, ka, d1)
        self.assertTrue(fix["exact"])
        self.assertAlmostEqual(fix["close"], 1309.6)
        self.assertAlmostEqual(fix["pct"], (1309.6 / 1326.0 - 1) * 100)
        self.assertAlmostEqual(fix["prev_close"], 1326.0)
        self.assertIsNone(calc.actual_fix(ka, ka, d1))                   # 价格一致无需修正
        self.assertIsNone(calc.actual_fix(kq, ka.iloc[:1], d1))          # 不复权数据缺复盘日


# ---------------------------------------------------------------- 文字与图表

class TestSummaryCharts(unittest.TestCase):
    def _stocks(self):
        return [
            {"code": "600001", "label": "甲", "pct": 10.0, "traded": True, "labels": ["2连板", "上龙虎榜", "放量"]},
            {"code": "600002", "label": "乙", "pct": -3.2, "traded": True, "labels": ["创60日新低"]},
            {"code": "600003", "label": "丙", "pct": 0.0, "traded": True, "labels": []},
            {"code": "600004", "label": "丁", "pct": None, "traded": False, "labels": ["停牌/无数据"]},
        ]

    def test_summary_lines(self):
        tot = {"count": 1, "mv": 123456.0, "day_pnl": 3601.0, "day_pct": 3.0, "float_pnl": -500.0, "float_pct": -0.4}
        lines = calc.summary_lines(self._stocks(), tot)
        text = "\n".join(lines)
        self.assertIn("自选股4只：上涨1只、下跌1只、平盘1只、停牌/无数据1只", lines[0])
        self.assertIn("最强甲（+10.00%）", lines[0])
        self.assertIn("持仓1只，市值12.35万，当日盈亏+3601元", lines[1])
        for line in lines:                              # 句末统一“。”，数字与单位之间不加空格
            self.assertTrue(line.endswith("。"), line)
            self.assertNotRegex(line, r"\d [%亿万只日元]")
        self.assertIn("涨停：甲", text)
        self.assertIn("上龙虎榜：甲", text)
        self.assertIn("创60日新低：乙", text)
        self.assertIn("停牌/无数据：丁", text)
        for w in ADVICE_WORDS:
            self.assertNotIn(w, text)

    def test_flow_rank_helpers(self):
        rank = {"source": "新浪财经", "basis": "新浪主力口径", "net_label": "主力净流入", "word_in": "主力净流入",
                "word_out": "主力净流出", "top_n": 20,
                "inflow": [{"code": "300750", "name": "宁德时代", "rank": 1, "net": 1.958e9},
                           {"code": "600001", "name": "甲", "rank": 2, "net": 5e8}],
                "outflow": [{"code": "600002", "name": "乙", "rank": 3, "net": -2e8}]}
        stocks = self._stocks()
        hits = calc.flow_rank_hits(stocks, rank)
        self.assertEqual(set(hits), {"600001", "600002"})                  # 300750 不是自选股
        self.assertEqual(hits["600001"], {"dir": "inflow", "word": "主力净流入", "rank": 2, "net": 5e8})
        self.assertEqual(calc.flow_rank_label(hits["600002"]), "主力净流出第3")
        line = calc.flow_rank_line(stocks, hits, rank)
        # 简洁写法：数据源与口径在口径说明里写，要点只写名次与金额
        self.assertEqual(line, "个股资金排行：甲主力净流入第2（+5.00亿）、乙主力净流出第3（-2.00亿）。")
        # 按“净流入在前、各按名次”排列，与自选股顺序无关
        self.assertEqual(calc.flow_rank_line(stocks[::-1], hits, rank), line)
        lines = calc.summary_lines(stocks, None, "ok", line)
        self.assertIn(line, lines)
        self.assertEqual(lines.index(line), len(lines) - 2)                # 放在当日事件之后、停牌名单之前
        self.assertEqual(lines[-1], "停牌/无数据：丁。")
        # 没有排行数据（单独运行自选股模块）或都不在排行里：不显示
        self.assertEqual(calc.flow_rank_hits(stocks, None), {})
        self.assertIsNone(calc.flow_rank_line(stocks, {}, rank))
        self.assertEqual(calc.flow_rank_hits(stocks, {"inflow": [{"code": "999999", "rank": 1}]}), {})
        for w in ADVICE_WORDS:
            self.assertNotIn(w, line)

    def test_summary_quiet_day(self):
        """没有任何事件时不写“自选股当日无……情况”。"""
        lines = calc.summary_lines([{"code": "1", "label": "甲", "pct": 1.0, "traded": True, "labels": []}], None)
        self.assertEqual(len(lines), 1)
        self.assertNotIn("最强", lines[0])
        self.assertFalse(any("当日无" in x for x in lines))

    def test_summary_lhb_unavailable(self):
        """龙虎榜未发布/获取失败时，不能写“无…龙虎榜”。"""
        quiet = [{"code": "1", "label": "甲", "pct": 1.0, "traded": True, "labels": []}]
        for state in ("empty", "failed", "format"):
            lines = calc.summary_lines(quiet, None, state)
            self.assertEqual(len(lines), 1)                 # 没有事件就不写，更不会断言“未上榜”
            self.assertNotIn("龙虎榜", "".join(lines))
        busy = [{"code": "1", "label": "甲", "pct": 10.0, "traded": True, "labels": ["涨停"]}]
        self.assertIn("未核对上榜情况", calc.summary_lines(busy, None, "empty")[1])
        self.assertNotIn("未核对", calc.summary_lines(busy, None, "ok")[1])

    def test_summary_tie(self):
        same = [{"code": "1", "label": "甲", "pct": 1.0, "traded": True, "labels": []},
                {"code": "2", "label": "乙", "pct": 1.0, "traded": True, "labels": []}]
        line = calc.summary_lines(same, None)[0]
        self.assertNotIn("最强", line)
        self.assertIn("涨跌幅相同", line)
        self.assertTrue(calc.breadth(same)["tie"])
        self.assertFalse(calc.breadth(self._stocks())["tie"])

    def test_volume_color_follows_candle(self):
        """开=收（一字板）时成交量颜色与 ECharts 的K线颜色一致：与前一根收盘比较。"""
        days = pd.bdate_range(end=pd.Timestamp(TD), periods=3).date
        k = k_of([(days[0], 3.0, 3.0, 3.0, 3.0, 1e5), (days[1], 2.7, 2.7, 2.7, 2.7, 1e4),
                  (days[2], 2.97, 2.97, 2.97, 2.97, 1e4)])
        colors = [d["itemStyle"]["color"] for d in calc.kline_option(k)["series"][-1]["data"]]
        self.assertEqual(colors, [UP, DOWN, UP])   # 第一根算涨；一字跌停为绿；一字涨停为红
        self.assertTrue(calc.candle_up(10, 11))
        self.assertFalse(calc.candle_up(11, 10, 9))
        self.assertTrue(calc.candle_up(10, 10, 10))

    def test_kline_option(self):
        k = calc.normalize_kline(make_raw(), TD)
        opt = calc.kline_option(k, cost=18.0)
        json.dumps(sanitize(opt), allow_nan=False)
        types = [s["type"] for s in opt["series"]]
        self.assertEqual(types, ["candlestick", "line", "line", "line", "bar"])
        candle = opt["series"][0]
        self.assertEqual(len(candle["data"]), calc.CHART_DAYS)
        last = k.iloc[-1]
        self.assertEqual(candle["data"][-1], [round(last["open"], 2), round(last["close"], 2),
                                              round(last["low"], 2), round(last["high"], 2)])
        self.assertEqual(candle["itemStyle"], {"color": UP, "color0": DOWN, "borderColor": UP, "borderColor0": DOWN})
        self.assertEqual(candle["markLine"]["data"][0]["yAxis"], 18.0)
        self.assertEqual(opt["series"][-1]["data"][-1]["itemStyle"]["color"], UP)   # 收盘 >= 开盘
        self.assertIsNotNone(opt["series"][3]["data"][0])                             # MA20 用了更早的数据
        self.assertEqual(len(opt["xAxis"][0]["data"]), calc.CHART_DAYS)
        self.assertNotIn("markLine", calc.kline_option(k, cost=100.0)["series"][0])  # 成本远离区间不画

    def test_pct_bar_option(self):
        opt = calc.pct_bar_option([("甲", 2.5), ("乙", -10.0), ("丙", 0.0)])
        self.assertEqual(opt["yAxis"]["data"], ["乙", "丙", "甲"])   # 升序：最强在最上
        colors = [d["itemStyle"]["color"] for d in opt["series"][0]["data"]]
        self.assertEqual(colors[0], DOWN)
        self.assertEqual(colors[2], UP)
        self.assertLess(opt["xAxis"]["min"], -10.0)
        self.assertGreater(opt["xAxis"]["max"], 2.5)
        flat = calc.pct_bar_option([("甲", 0.0)])
        self.assertLess(flat["xAxis"]["min"], flat["xAxis"]["max"])

    def test_trend_and_pie(self):
        self.assertIsNone(calc.trend_option(pd.DataFrame({"date": ["2026-10-09"], "持仓市值": [1.0]})))
        h = pd.DataFrame({"date": ["2026-10-07", "2026-10-08", "2026-10-09"], "持仓市值": [1e5, 1.1e5, 1.05e5],
                          "持仓当日盈亏": [None, 1e4, -5e3]})
        opt = calc.trend_option(h)
        self.assertEqual(opt["series"][1]["data"][0], None)
        self.assertEqual(opt["series"][1]["data"][2]["itemStyle"]["color"], DOWN)
        pie = calc.weight_pie_option([{"name": "甲", "mv": 100.0}, {"name": "乙", "mv": None}])
        self.assertEqual(len(pie["series"][0]["data"]), 1)


# ---------------------------------------------------------------- build() 编排（假数据源）

class FakeFetcher:
    """按函数名返回假数据。override[name] 可以是 DataFrame、异常实例或 callable(**kw)。"""

    def __init__(self, raw_dir, klines):
        self.raw_dir, self.force, self.same_day = Path(raw_dir), False, False
        self.klines = klines
        self.calls = []
        self.override = {}
        self.stats = {"network": 0, "cache": 0, "failed": 0}

    def call(self, func, *args, snapshot=False, volatile=False, **kw):
        name = func.__name__
        self.calls.append(name)
        if name in self.override:
            v = self.override[name]
            if isinstance(v, BaseException):
                raise v
            return v(**kw) if callable(v) else v
        if name == "stock_zh_a_hist":
            raise FetchError("push2his 连接被重置")
        if name == "tx_stock_daily":
            if kw["symbol"] not in self.klines:
                raise FetchError("腾讯日K返回错误")
            return self.klines[kw["symbol"]]
        if name == "stock_zt_pool_em":
            return pd.DataFrame({"代码": ["600001"], "名称": ["测试甲"], "连板数": [2]})
        if name in ("stock_zt_pool_zbgc_em", "stock_zt_pool_dtgc_em"):
            return pd.DataFrame()
        if name == "em_lhb_detail":
            return em_lhb_raw([("300003", "测试丙", "日涨幅偏离值达到7%的证券", 8.0, 1e7, 3e7, 2e7, "")])
        if name == "sina_names":
            return pd.DataFrame({"code": ["300003"], "name": ["测试丙"]})
        raise AssertionError(f"意外的请求 {name}")


def net_error(msg="timeout"):
    """模拟 ctx.fetch 重试后抛出的、根因是网络超时的 FetchError。"""
    import requests
    try:
        raise FetchError(msg) from requests.ConnectTimeout(msg)
    except FetchError as e:
        return e


def limit_up_raw(code="600001", name=None, ratio=0.10):
    """最后一天收在涨停价的日K。"""
    raw = make_raw(code=code, name=name)
    prev = float(raw["收盘"].iloc[-2])
    up = calc.limit_price(prev, ratio)
    raw.loc[raw.index[-1], ["收盘", "最高"]] = [up, up]
    raw.loc[raw.index[-1], "涨跌幅"] = (up / prev - 1) * 100
    return raw


class FakeContext(Context):
    def recent_trade_days(self, n):
        return list(pd.bdate_range(end=pd.Timestamp(self.trade_date), periods=n).date)

    def prev_trade_day(self, n=1):
        return self.recent_trade_days(n + 1)[0]


class TestBuild(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, val in (("latest_completed_trade_day", TD), ("is_trade_day", False)):
            p = mock.patch.object(watchlist.tc, name, return_value=val)
            p.start()
            self.addCleanup(p.stop)
        logging.disable(logging.WARNING)   # 故意制造的接口失败会打印告警，测试时静音
        self.addCleanup(logging.disable, logging.NOTSET)

    def ctx(self, items, klines):
        f = FakeFetcher(Path(self.tmp.name) / "raw", klines)
        return FakeContext(trade_date=TD, cfg={"watchlist": items}, fetcher=f,
                           history=History(Path(self.tmp.name) / "history.csv"), snapshot_ok=True,
                           log=logging.getLogger("kmcf.test"))

    @staticmethod
    def table(d, title):
        return next(b for b in d["blocks"] if b["title"] == title)

    @staticmethod
    def cell(tbl, row, key):
        return tbl["rows"][row][[c["key"] for c in tbl["columns"]].index(key)]["text"]

    def test_empty_watchlist(self):
        d = watchlist.build(self.ctx([], {})).to_dict()
        self.assertEqual(d["errors"], [])
        self.assertIn(watchlist.EMPTY_TIP, d["notes"])
        self.assertEqual(d["blocks"], [])

    def test_fallback_and_partial_failure(self):
        items = [{"code": "600001", "name": "", "shares": 100, "cost": 15},
                 {"code": "000002", "name": "测试乙", "shares": 0, "cost": 0},
                 {"code": "300003", "name": "", "shares": 0, "cost": 0},
                 {"code": "bad", "name": "无效"}]
        klines = {"600001": make_raw(code="600001", name="测试甲"),
                  "300003": make_raw(code="300003", start_price=30, end_price=20)}
        ctx = self.ctx(items, klines)
        d = sanitize(watchlist.build(ctx).to_dict())
        json.dumps(d, allow_nan=False)

        self.assertEqual(ctx.fetcher.calls.count("stock_zh_a_hist"), 1)   # 失败一次后不再尝试
        self.assertTrue(ctx.shared.get(watchlist.EM_DOWN))
        self.assertFalse(ctx.shared.get(watchlist.TX_DOWN))               # 单只股票的数据问题不熔断
        self.assertEqual(len(d["errors"]), 1)
        self.assertIn("测试乙", d["errors"][0])

        overview = self.table(d, "自选股总览")
        names = [r[0]["text"] for r in overview["rows"]]
        self.assertEqual(names, ["测试甲", "测试乙", "测试丙"])               # 名称补全：K线自带 / 新浪
        self.assertIn("2连板", self.cell(overview, 0, "status"))
        self.assertIn("上龙虎榜", self.cell(overview, 2, "status"))
        self.assertEqual(self.cell(overview, 1, "status"), "无数据")

        titles = [b["title"] for b in d["blocks"]]
        self.assertIn("持仓明细", titles)
        self.assertIn("自选股龙虎榜", titles)
        self.assertEqual(sum(1 for b in d["blocks"] if b["type"] == "chart" and "（" in b["title"]), 2)
        labels = [k["label"] for k in d["kpis"]]
        self.assertIn("持仓市值", labels)
        self.assertIn("累计浮动盈亏", labels)
        self.assertTrue(any("腾讯证券" in n for n in d["notes"]))
        self.assertNotIn(watchlist.NO_HOLD_TIP, d["notes"])
        # 标题、KPI 里数字与单位/中文之间不加空格；不写“点击表头可排序”这类操作提示
        for t in titles + [k["sub"] for k in d["kpis"]] + [k["value"] for k in d["kpis"]]:
            self.assertNotRegex(t, r"\d [一-鿿]|[一-鿿] \d", t)
        self.assertFalse(any("可排序" in (b.get("note") or "") for b in d["blocks"]))
        self.assertFalse(any("小时" in n for n in d["notes"] + d["errors"]))
        for w in ADVICE_WORDS:
            self.assertNotIn(w, "".join(d["summary"]))
        hist = ctx.history.load()
        self.assertAlmostEqual(float(hist["持仓市值"].iloc[-1]), 100 * 20.0)
        hold = self.table(d, "持仓明细")
        self.assertTrue(self.cell(hold, 0, "mv").endswith("元"))           # 2000 -> “2000元”
        self.assertRegex(self.cell(hold, 0, "day_pnl"), r"^[+-]?\d+元$")
        self.assertEqual(self.cell(hold, 0, "float_pnl"), "+500元")

    def test_tx_breaker_and_merged_error(self):
        """腾讯日K网络失败一次后熔断：后续股票不再请求，错误合并成一条。"""
        items = [{"code": f"60000{i}", "name": f"股{i}", "shares": 0, "cost": 0} for i in range(1, 6)]
        ctx = self.ctx(items, {})
        ctx.fetcher.override["tx_stock_daily"] = net_error()
        d = watchlist.build(ctx).to_dict()
        self.assertEqual(ctx.fetcher.calls.count("tx_stock_daily"), 1)
        self.assertTrue(ctx.shared.get(watchlist.TX_DOWN))
        kerr = [e for e in d["errors"] if "日K" in e]
        self.assertEqual(len(kerr), 1)
        # push2his_down 会跨次生成保留 3 小时，东方财富一侧不能写“本次”
        self.assertIn("东方财富与腾讯证券日K接口暂时无法访问", kerr[0])
        self.assertNotIn("近期", kerr[0])
        self.assertIn("股1、股2、股3、股4、股5", kerr[0])
        self.assertNotIn("tx_stock_daily", kerr[0])
        self.assertEqual(d["summary"], [])     # 全部获取失败：错误框里已有说明，要点不再重复“无行情数据”
        stocks = [{"code": "1", "label": "甲", "pct": None, "traded": False, "labels": [], "src": "tx"}]
        self.assertIn("均无行情数据", "".join(calc.summary_lines(stocks, None)))   # 有日K但复盘日停牌：照常写

    def test_uses_shared_funds_lhb(self):
        ctx = self.ctx([{"code": "600001", "name": "甲", "shares": 0, "cost": 0}],
                       {"600001": make_raw(code="600001")})
        ctx.shared["funds.lhb"] = pd.DataFrame({"code": ["600001"], "name": ["甲"], "pct": [5.0], "net": [1e7],
                                                "buy": [2e7], "sell": [1e7], "net_ratio": [3.0],
                                                "reasons": ["日振幅值达到15%"], "basis": ["当日"]})
        d = watchlist.build(ctx).to_dict()
        self.assertNotIn("em_lhb_detail", ctx.fetcher.calls)
        lhb_tbl = self.table(d, "自选股龙虎榜")
        self.assertEqual(self.cell(lhb_tbl, 0, "reason"), "日振幅值达到15%")
        self.assertEqual(self.cell(lhb_tbl, 0, "basis"), "当日")

    def test_flow_rank_from_funds(self):
        """“龙虎榜与资金”模块先运行并给出个股资金排行：自选股进入排行时在状态标签和要点里注明名次。"""
        items = [{"code": "600001", "name": "甲", "shares": 0, "cost": 0},
                 {"code": "600002", "name": "乙", "shares": 0, "cost": 0}]
        ctx = self.ctx(items, {c["code"]: make_raw(code=c["code"]) for c in items})
        ctx.shared["funds.flow_rank"] = {
            "source": "新浪财经", "basis": "新浪主力口径，划分标准与东方财富不同", "net_label": "主力净流入",
            "word_in": "主力净流入", "word_out": "主力净流出", "top_n": 20,
            "inflow": [{"code": "300750", "name": "宁德时代", "rank": 1, "net": 1.958e9},
                       {"code": "600002", "name": "乙", "rank": 2, "net": 3.1e8}],
            "outflow": []}
        d = watchlist.build(ctx).to_dict()
        overview = self.table(d, "自选股总览")
        self.assertNotIn("主力", self.cell(overview, 0, "status"))
        self.assertIn("主力净流入第2", self.cell(overview, 1, "status"))
        self.assertIn("个股资金排行：乙主力净流入第2（+3.10亿）。", d["summary"])
        self.assertTrue(any("主力净流入第N" in n and "新浪主力口径" in n for n in d["notes"]))
        # 没有排行数据时不显示
        d2 = watchlist.build(self.ctx(items, {c["code"]: make_raw(code=c["code"]) for c in items})).to_dict()
        self.assertFalse(any("资金排行" in s for s in d2["summary"]))
        self.assertFalse(any("主力" in n for n in d2["notes"]))

    def test_stock_names_without_spaces(self):
        """配置、涨停池、新浪行情里的三字简称带空格（粤 传 媒）：统一去掉。"""
        items = [{"code": "002181", "name": "粤 传 媒", "shares": 0, "cost": 0},
                 {"code": "600001", "name": "", "shares": 0, "cost": 0},
                 {"code": "300004", "name": "", "shares": 0, "cost": 0}]   # 不在涨停池/龙虎榜里，走新浪补名称
        klines = {c["code"]: make_raw(code=c["code"]) for c in items}
        ctx = self.ctx(items, klines)
        ctx.fetcher.override["stock_zt_pool_em"] = pd.DataFrame({"代码": ["600001"], "名称": ["测 试 甲"], "连板数": [1]})
        ctx.fetcher.override["sina_names"] = pd.DataFrame({"code": ["300004"], "name": ["测　试 丁"]})
        d = watchlist.build(ctx).to_dict()
        names = [r[0]["text"] for r in self.table(d, "自选股总览")["rows"]]
        self.assertEqual(names, ["粤传媒", "测试甲", "测试丁"])
        self.assertFalse(any(" " in s for s in d["summary"] if "粤" in s))

    def test_standalone_lhb_matches_funds_basis(self):
        """单独运行：多日累计口径的金额要标明“多日累计”，同一股票只占一行。"""
        ctx = self.ctx([{"code": "600825", "name": "新华传媒", "shares": 0, "cost": 0}],
                       {"600825": make_raw(code="600825")})
        ctx.fetcher.override["em_lhb_detail"] = em_lhb_raw()
        d = watchlist.build(ctx).to_dict()
        tbl = self.table(d, "自选股龙虎榜")
        self.assertEqual(len(tbl["rows"]), 1)
        self.assertIn("累计", self.cell(tbl, 0, "basis"))
        self.assertEqual(self.cell(tbl, 0, "net"), "+4977.00万")
        self.assertIn("金额口径", [c["label"] for c in tbl["columns"]])

    def test_lhb_failure_summary_and_error(self):
        ctx = self.ctx([{"code": "600001", "name": "甲", "shares": 0, "cost": 0}],
                       {"600001": make_raw(code="600001")})
        ctx.fetcher.override["em_lhb_detail"] = FetchError("datacenter 失败")
        ctx.fetcher.override["stock_zt_pool_em"] = pd.DataFrame({"代码": ["000001"], "名称": ["平安银行"]})
        d = watchlist.build(ctx).to_dict()
        self.assertTrue(any("龙虎榜数据获取失败" in e for e in d["errors"]))
        text = "".join(d["summary"])
        self.assertIn("未核对上榜情况", text)
        self.assertNotIn("、龙虎榜、", text)

    def test_lhb_unrecognized_format(self):
        ctx = self.ctx([{"code": "600001", "name": "甲", "shares": 0, "cost": 0}],
                       {"600001": make_raw(code="600001")})
        ctx.shared["funds.lhb"] = pd.DataFrame({"代码": ["600001"], "名称": ["甲"]})
        d = watchlist.build(ctx).to_dict()
        self.assertTrue(any("格式无法识别" in e for e in d["errors"]))
        self.assertFalse(any("龙虎榜暂无数据" in n for n in d["notes"]))
        self.assertIn("未核对上榜情况", "".join(d["summary"]))

    def test_empty_lhb_cache_dropped(self):
        ctx = self.ctx([{"code": "600001", "name": "甲", "shares": 0, "cost": 0}],
                       {"600001": make_raw(code="600001")})
        kw = dict(date=ctx.date_iso)
        path = Path(ctx.fetcher.raw_dir) / f"{watchlist.cache_key(watchlist.funds_data.em_lhb_detail, (), kw)}.pkl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"stale")
        ctx.fetcher.override["em_lhb_detail"] = pd.DataFrame()
        d = watchlist.build(ctx).to_dict()
        self.assertFalse(path.exists())
        self.assertTrue(any("龙虎榜暂无数据" in n for n in d["notes"]))
        self.assertIn("龙虎榜暂未发布", "".join(d["summary"]))
        self.assertEqual(d["errors"], [])

    def test_push2ex_down_skips_pools(self):
        ctx = self.ctx([{"code": "600001", "name": "甲", "shares": 0, "cost": 0}],
                       {"600001": make_raw(code="600001")})
        ctx.shared[watchlist.EX_DOWN] = True
        d = watchlist.build(ctx).to_dict()
        self.assertFalse(any(c.startswith("stock_zt_pool") for c in ctx.fetcher.calls))
        self.assertTrue(any("推算" in n and "暂时无法获取" in n for n in d["notes"]))
        self.assertFalse(any("没有该日数据" in n or "只提供最近30天" in n for n in d["notes"]))

    def test_pool_format_change_falls_back_to_inference(self):
        ctx = self.ctx([{"code": "600001", "name": "甲", "shares": 0, "cost": 0}],
                       {"600001": limit_up_raw("600001")})
        ctx.fetcher.override["stock_zt_pool_em"] = pd.DataFrame({"股票代码": ["600001"], "名称": ["甲"]})
        d = watchlist.build(ctx).to_dict()
        self.assertIn("涨停", self.cell(self.table(d, "自选股总览"), 0, "status"))
        self.assertTrue(any("涨停池返回格式无法识别" in n for n in d["notes"]))

    def test_st_not_mislabeled(self):
        """ST 股收 +5%、冲高 +8% 回落：现行 10% 规则下不是涨停/炸板。"""
        raw = make_raw(code="600735")
        prev = float(raw["收盘"].iloc[-2])
        raw.loc[raw.index[-1], ["收盘", "最高"]] = [round(prev * 1.05, 2), round(prev * 1.08, 2)]
        ctx = self.ctx([{"code": "600735", "name": "ST测试", "shares": 0, "cost": 0}], {"600735": raw})
        d = watchlist.build(ctx).to_dict()
        status = self.cell(self.table(d, "自选股总览"), 0, "status")
        self.assertNotIn("涨停", status)
        self.assertNotIn("炸板", status)
        self.assertTrue(any("2025-07-07" in n for n in d["notes"]))
        ctx2 = self.ctx([{"code": "600735", "name": "*ST测试", "shares": 0, "cost": 0}],
                        {"600735": limit_up_raw("600735")})
        self.assertIn("涨停", self.cell(self.table(watchlist.build(ctx2).to_dict(), "自选股总览"), 0, "status"))

    def test_names_fallback_when_sina_format_changes(self):
        ctx = self.ctx([{"code": "300002", "name": "", "shares": 0, "cost": 0}],
                       {"300002": make_raw(code="300002")})
        ctx.fetcher.override["sina_names"] = pd.DataFrame({"symbol": ["300002"], "nm": ["新浪名"]})
        ctx.fetcher.override["tx_names"] = pd.DataFrame({"code": ["300002"], "name": ["神州泰岳"]})
        d = watchlist.build(ctx).to_dict()
        self.assertEqual(self.table(d, "自选股总览")["rows"][0][0]["text"], "神州泰岳")
        self.assertFalse(any("名称补全" in e for e in d["errors"]))

    def test_history_note_only_with_holdings(self):
        with mock.patch.object(watchlist.tc, "latest_completed_trade_day", return_value=TD + dt.timedelta(days=3)):
            d = watchlist.build(self.ctx([{"code": "600001", "name": "甲", "shares": 0, "cost": 0}],
                                         {"600001": make_raw(code="600001")})).to_dict()
            self.assertFalse(any("历史交易日" in n for n in d["notes"]))
            d = watchlist.build(self.ctx([{"code": "600001", "name": "甲", "shares": 100, "cost": 10}],
                                         {"600001": make_raw(code="600001")})).to_dict()
            self.assertTrue(any("历史交易日" in n for n in d["notes"]))

    def test_need_actual_check(self):
        sat, mon = dt.date(2026, 10, 10), dt.date(2026, 10, 12)
        with mock.patch.object(watchlist.tc, "is_trade_day", side_effect=lambda d: d.weekday() < 5):
            self.assertFalse(watchlist._need_actual_check(TD, False, sat))   # 周末复盘周五：之后没开过盘
            self.assertTrue(watchlist._need_actual_check(TD, False, mon))    # 周一复盘上周五
            self.assertFalse(watchlist._need_actual_check(TD, False, TD))    # 当天复盘
            self.assertTrue(watchlist._need_actual_check(TD, True, sat))     # 历史日期

    def test_no_holdings_tip(self):
        ctx = self.ctx([{"code": "600001", "name": "甲", "shares": 0, "cost": 0}],
                       {"600001": make_raw(code="600001")})
        d = watchlist.build(ctx).to_dict()
        self.assertIn(watchlist.NO_HOLD_TIP, d["notes"])
        self.assertNotIn("持仓明细", [b["title"] for b in d["blocks"]])
        self.assertFalse(Path(self.tmp.name, "history.csv").exists())

    def test_many_stocks_chart_limit(self):
        items = [{"code": f"6000{i:02d}", "name": f"股{i}", "shares": 0, "cost": 0} for i in range(1, 11)]
        klines = {it["code"]: make_raw(code=it["code"]) for it in items}
        d = watchlist.build(self.ctx(items, klines)).to_dict()
        kcharts = [b for b in d["blocks"] if b["type"] == "chart" and "（" in b["title"]]
        self.assertEqual(len(kcharts), watchlist.MAX_KLINE_CHARTS)
        self.assertTrue(all(b["span"] == "half" for b in kcharts))
        self.assertTrue(any("只画前8只" in n for n in d["notes"]))
        self.assertNotIn("最强", [k["label"] for k in d["kpis"]])   # 10 只涨跌幅相同，不分最强最弱


if __name__ == "__main__":
    unittest.main()
