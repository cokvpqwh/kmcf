"""大盘与情绪模块的单元测试：纯计算 + 用假数据源跑一遍 build()（不联网）。

运行：.venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import datetime as dt
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kmcf import trade_calendar as tc  # noqa: E402
from kmcf.context import Context  # noqa: E402
from kmcf.fetch import FetchError, SnapshotUnavailable  # noqa: E402
from kmcf.history import History  # noqa: E402
from kmcf.sections import _market_calc as calc  # noqa: E402
from kmcf.sections import _market_data as mdata  # noqa: E402
from kmcf.sections import market  # noqa: E402


# ---------------------------------------------------------------- 合成数据

def make_zt(rows):
    """rows: [(代码, 名称, 连板数, 首次封板时间, 炸板次数, 封板资金, 所属行业)]"""
    return pd.DataFrame([{
        "代码": c, "名称": n, "连板数": lb, "首次封板时间": fbt, "最后封板时间": fbt, "炸板次数": zbc,
        "封板资金": fund, "所属行业": hy, "涨跌幅": 10.0, "成交额": 1e8, "流通市值": 5e9, "换手率": 3.0,
        "涨停统计": f"{lb}/{lb}",
    } for c, n, lb, fbt, zbc, fund, hy in rows])


ZT = make_zt([
    ("600825", "新华传媒", 3, "092500", 0, 9e8, "出版"),
    ("2242", "九阳股份", 3, "093027", 6, 7e7, "小家电"),     # 代码丢了前导 0
    ("600241", "时代万恒", 2, "092500", 1, 2e8, "电池"),
    ("001300", "三柏硕", 1, "100000", 0, 1e7, "文娱用品"),
    ("600812", "华北制药", 1, "093058", 0, 1e8, "化学制药"),
])
ZB = pd.DataFrame({"代码": ["605303", "920627"], "名称": ["园林股份", "力王股份"], "涨跌幅": [2.1, 24.1],
                   "首次封板时间": ["092502", "105812"], "炸板次数": [1, 2], "振幅": [17.7, 33.9],
                   "成交额": [8e8, 3e8], "所属行业": ["基础建设", "电池"]})
DT = pd.DataFrame({"代码": ["603400", "000001"], "名称": ["华之杰", "某某"], "涨跌幅": [-10.0, -10.0],
                   "连续跌停": [2, 1], "封单资金": [3e6, 9e6], "开板次数": [7, 0], "最后封板时间": ["150000", "93000"],
                   "成交额": [2e8, 1e8], "所属行业": ["通用设备", "银行"]})
PREV = pd.DataFrame({"代码": ["600825", "002242", "600000", "000002"], "名称": ["a", "b", "c", "d"],
                     "涨跌幅": [10.0, 10.0, -2.0, 1.0]})


def make_kline(dates, start=100.0, step=1.0, amount=True):
    n = len(dates)
    close = [start + step * i for i in range(n)]
    df = pd.DataFrame({"date": [d.isoformat() for d in dates], "open": [c - 0.5 for c in close], "close": close,
                       "high": [c + 1 for c in close], "low": [c - 1 for c in close], "volume": [1e9] * n})
    if amount:
        df["amount"] = [5e11] * n
    return df


# ---------------------------------------------------------------- 纯计算

class TestBasics(unittest.TestCase):
    def test_hms(self):
        self.assertEqual(calc.hms("092500"), "09:25:00")
        self.assertEqual(calc.hms(92500), "09:25:00")
        self.assertEqual(calc.hms("145236"), "14:52:36")
        self.assertEqual(calc.hms("0"), "—")
        self.assertEqual(calc.hms(None), "—")
        self.assertEqual(calc.hms("abc"), "—")

    def test_zttj_and_fmt_yi(self):
        self.assertEqual(calc.zttj_text("4/3"), "4天3板")
        self.assertEqual(calc.zttj_text(None), "—")
        self.assertEqual(calc.fmt_yi(2.18134e11, signed=True), "+2181亿")   # 不用千分位
        self.assertEqual(calc.fmt_yi(-5e10, signed=True), "-500亿")
        self.assertEqual(calc.fmt_yi(1.9e12), "1.90万亿")

    def test_norm_kline_and_index_day(self):
        raw = pd.DataFrame({"date": ["2026-10-09", "2026-10-08", "2026-10-08"], "open": ["1", "1", "1"],
                            "close": ["110", "100", "100"], "high": [1, 1, 1], "low": [1, 1, 1],
                            "volume": [1, 1, 1]})
        df = calc.norm_kline(raw)
        self.assertEqual(df["date"].tolist(), ["2026-10-08", "2026-10-09"])
        self.assertTrue(df["amount"].isna().all())
        day = calc.index_day(df, "2026-10-09")
        self.assertAlmostEqual(day["pct"], 10.0)
        self.assertIsNone(calc.index_day(df, "2026-10-10"))
        self.assertIsNone(calc.index_day(df, "2026-10-08")["pct"])  # 没有前一日
        self.assertTrue(calc.norm_kline(None).empty)

    def test_turnover_series(self):
        sh = pd.DataFrame({"date": ["d1", "d2", "d3"], "amount": [1.0, 2.0, 0.0]})
        sz = pd.DataFrame({"date": ["d2", "d3", "d4"], "amount": [10.0, 20.0, 30.0]})
        ser = calc.turnover_series(sh, sz)
        self.assertEqual(ser["date"].tolist(), ["d2"])        # d3 上证成交额为 0 视为缺失
        self.assertEqual(ser["turnover"].tolist(), [12.0])

    def test_exchange_amount(self):
        sse = pd.DataFrame({"单日情况": ["挂牌数", "成交金额"], "股票": [2361, "8890.97"], "主板A": [1, 6117.2],
                            "主板B": [1, 1.3], "科创板": [1, 2772.46]})
        self.assertAlmostEqual(calc.sse_stock_amount(sse), 8890.97e8, delta=1)
        old = sse.assign(股票="-")  # 老格式：没有合计列
        self.assertAlmostEqual(calc.sse_stock_amount(old), (6117.2 + 1.3 + 2772.46) * 1e8, delta=1)
        szse = pd.DataFrame({"证券类别": ["股票", "主板A股"], "成交金额": [1.012668e12, 5e11]})
        self.assertAlmostEqual(calc.szse_stock_amount(szse), 1.012668e12)
        self.assertIsNone(calc.szse_stock_amount(pd.DataFrame()))


class TestDataHelpers(unittest.TestCase):
    def test_timed(self):
        import time as _time

        def slow_func():
            _time.sleep(1.0)
            return 1
        f = mdata.timed(slow_func, 0.1)
        self.assertEqual(f.__name__, "slow_func")             # 缓存键不变
        t0 = _time.monotonic()
        with self.assertRaises(TimeoutError):
            f()
        with self.assertRaises(TimeoutError):                  # 同一包装对象再次调用立即失败
            f()
        self.assertLess(_time.monotonic() - t0, 0.8)
        self.assertEqual(mdata.timed(lambda: 2, 1)(), 2)

    def test_conn_error_and_mark_down(self):
        ctx = type("C", (), {"shared": {}})()
        mdata.mark_down(ctx, format_error(), "k")
        self.assertNotIn("k", ctx.shared)                      # 解析类错误不打标记
        err = FetchError("x")
        err.__cause__ = TimeoutError("slow")
        mdata.mark_down(ctx, err, "k")
        self.assertTrue(ctx.shared["k"])
        self.assertTrue(mdata.is_conn_error(conn_error()))
        self.assertTrue(mdata.is_conn_error(mdata.SiteDown("x")))
        self.assertFalse(mdata.is_conn_error(calc.PoolFormatError("x")))

    def test_pool_error_text(self):
        for e in (conn_error(), format_error(), mdata.SiteDown("x"), calc.PoolFormatError("返回数据缺少“代码”列")):
            t = mdata.pool_error_text(e)
            self.assertIn("东方财富涨停板专题", t)
            self.assertNotIn("stock_", t)
            self.assertNotIn("'data'", t)
        # 站点已标记失败而跳过：报告里只写“暂时无法访问”（运行时状态只写日志）
        self.assertEqual(mdata.pool_error_text(mdata.SiteDown("x", mdata.PUSH2EX_DOWN)), "东方财富涨停板专题暂时无法访问")

    def test_site_skip_text_for_persisted_flag(self):
        """push2his_down 跨次生成保留 3 小时：报告文字只写“暂时无法访问”，“3 小时内不再尝试”只写日志。"""
        ctx = type("C", (), {"shared": {mdata.PUSH2HIS_DOWN: True, mdata.SINA_DOWN: True},
                             "fetcher": type("F", (), {"raw_dir": None, "force": False})()})()
        with self.assertLogs("kmcf", "INFO") as logs, self.assertRaises(mdata.SiteDown) as cm:
            mdata.site_fetch(ctx, mdata.PUSH2HIS_DOWN, lambda: None)
        self.assertIn("暂时无法访问", str(cm.exception))
        self.assertNotIn("小时", str(cm.exception))
        self.assertTrue(any("3 小时内不再尝试" in x for x in logs.output))
        self.assertEqual(cm.exception.flag, mdata.PUSH2HIS_DOWN)
        with self.assertRaises(mdata.SiteDown) as cm:
            mdata.site_fetch(ctx, mdata.SINA_DOWN, lambda: None)
        self.assertIn("暂时无法访问", str(cm.exception))
        self.assertNotIn("本次", str(cm.exception))


class TestBreadth(unittest.TestCase):
    RAW = [{"-1": 740}, {"-10": 6}, {"-11": 8}, {"0": 103}, {"1": 970}, {"10": 40}, {"11": 69}, {"x": 5}]

    def test_fenbu(self):
        f = calc.parse_fenbu(self.RAW)
        self.assertEqual(f[11], 69)
        self.assertNotIn("x", f)
        b = calc.breadth_from_fenbu(f)
        self.assertEqual((b["up"], b["down"], b["flat"], b["zt"], b["dt"]), (970 + 40 + 69, 740 + 6 + 8, 103, 69, 8))
        opt = calc.fenbu_option(f)
        self.assertEqual(len(opt["xAxis"]["data"]), 23)
        self.assertEqual(opt["xAxis"]["data"][0], "跌停")
        self.assertEqual(opt["xAxis"]["data"][-1], "涨停")
        self.assertEqual(opt["series"][0]["data"][-1]["itemStyle"]["color"], calc.UP)
        self.assertEqual(opt["series"][0]["data"][0]["itemStyle"]["color"], calc.DOWN)

    def test_legu(self):
        df = pd.DataFrame({"item": ["上涨", "下跌", "平盘", "活跃度", "统计日期"],
                           "value": [2989.0, 2108.0, 113.0, "57.22%", "2026-10-09 15:00:00"]})
        d = calc.parse_legu(df)
        self.assertEqual(d["统计日期"], "2026-10-09 15:00:00")
        self.assertAlmostEqual(d["活跃度"], 57.22)
        b = calc.breadth_from_legu(d)
        self.assertEqual((b["up"], b["down"], b["flat"], b["total"]), (2989, 2108, 113, 5210))
        self.assertIsNone(calc.breadth_from_legu({}))


class TestPools(unittest.TestCase):
    def setUp(self):
        self.zt, self.zb, self.dt, self.prev = (calc.norm_pool(x) for x in (ZT, ZB, DT, PREV))

    def test_norm_pool(self):
        self.assertIn("002242", self.zt["代码"].tolist())
        self.assertIsNone(calc.norm_pool(None))
        self.assertTrue(calc.norm_pool(pd.DataFrame()).empty)
        self.assertTrue(calc.norm_pool(pd.DataFrame(), "zb").empty)    # 真空表：0 家是正常结果

    def test_norm_pool_format_change(self):
        """非空表缺必需列：必须报错，不能当成空表（否则会静默记成 0 家）。"""
        with self.assertRaises(calc.PoolFormatError) as cm:
            calc.norm_pool(ZB.rename(columns={"代码": "股票代码"}), "zb")
        self.assertIn("代码", str(cm.exception))
        with self.assertRaises(calc.PoolFormatError):
            calc.norm_pool(DT.rename(columns={"代码": "股票代码"}), "dt")
        with self.assertRaises(calc.PoolFormatError):
            calc.norm_pool(ZT.drop(columns=["名称"]), "zt")
        with self.assertRaises(calc.PoolFormatError):
            calc.norm_pool(PREV.drop(columns=["涨跌幅"]), "prev")

    def test_metrics_without_name_column(self):
        m = calc.pool_metrics(self.zt.drop(columns=["名称"]), None, None, None)
        self.assertEqual(m["max_board"], 3)
        self.assertEqual(m["max_board_names"], [])

    def test_metrics(self):
        m = calc.pool_metrics(self.zt, self.zb, self.dt, self.prev)
        self.assertEqual((m["zt_count"], m["zb_count"], m["dt_count"]), (5, 2, 2))
        self.assertAlmostEqual(m["zb_rate"], 2 / 7 * 100)
        self.assertEqual(m["max_board"], 3)
        self.assertEqual(m["max_board_names"], ["新华传媒", "九阳股份"])
        self.assertEqual(m["lianban_count"], 3)
        self.assertEqual(m["dt_lianxu"], 1)
        self.assertAlmostEqual(m["prev_zt_avg_pct"], (10 + 10 - 2 + 1) / 4)
        self.assertAlmostEqual(m["prev_zt_up_ratio"], 75.0)
        self.assertAlmostEqual(m["promote_rate"], 50.0)   # 600825、002242 晋级

    def test_metrics_missing_and_empty(self):
        m = calc.pool_metrics(None, None, None, None)
        self.assertEqual(m, {})
        m = calc.pool_metrics(self.zt, None, calc.norm_pool(pd.DataFrame()), None)
        self.assertNotIn("zb_rate", m)
        self.assertEqual((m["dt_count"], m["dt_lianxu"]), (0, 0))

    def test_ladder(self):
        levels = calc.ladder_levels(self.zt, first_board_limit=1)
        self.assertEqual([lv["label"] for lv in levels], ["3板", "2板", "首板"])
        top = levels[0]["items"]
        self.assertEqual(top[0]["text"], "新华传媒")          # 09:25 先封
        self.assertIn("一字", top[0]["sub"])
        self.assertIn("09:30", top[1]["sub"])                 # 有炸板，不算一字
        first = levels[-1]["items"]
        self.assertEqual(len(first), 2)                       # 1 只 + “另有”
        self.assertIn("另有1只", first[-1]["text"])
        self.assertEqual(calc.ladder_levels(None), [])

    def test_ladder_yizi(self):
        """一字板：首次封板 09:25:xx（不只是 09:25:00）、最后封板也在 09:25、炸板 0 次。"""
        def zt(first, last, zbc=0):
            return calc.norm_pool(pd.DataFrame({"代码": ["600001"], "名称": ["甲"], "连板数": [2],
                                                "首次封板时间": [first], "最后封板时间": [last],
                                                "炸板次数": [zbc], "所属行业": ["银行"]}), "zt")

        def sub(df):
            return calc.ladder_levels(df)[0]["items"][0]["sub"]

        self.assertIn("一字", sub(zt("092500", "092500")))
        self.assertIn("一字", sub(zt("092512", "092512")))        # 09:25:12 封板也是集合竞价封死
        self.assertIn("一字", sub(zt(92503, 92503)))              # 整数（前导 0 丢失）
        self.assertIn("一字", sub(zt("92503.0", None)))           # 最后封板时间缺失时不核对
        self.assertNotIn("一字", sub(zt("092512", "093512")))     # 最后封板在开盘后：中途开过板
        self.assertNotIn("一字", sub(zt("092512", "092512", 1)))  # 炸板过
        self.assertIn("09:31", sub(zt("093105", "093105")))
        self.assertTrue(calc.is_yizi("092559", "092559", 0))
        self.assertFalse(calc.is_yizi("093000", "093000", 0))
        self.assertFalse(calc.is_yizi(None, None, 0))

    def test_rows(self):
        rows = calc.zt_rows(self.zt)
        self.assertEqual([r["name"] for r in rows[:2]], ["新华传媒", "九阳股份"])  # 连板相同按封板资金
        self.assertEqual(rows[0]["fbt"], "09:25:00")
        self.assertEqual(calc.zb_rows(self.zb)[0]["name"], "力王股份")
        self.assertEqual(calc.dt_rows(self.dt)[0]["name"], "华之杰")
        self.assertEqual(calc.dt_rows(self.dt)[1]["lbt"], "09:30:00")
        self.assertEqual(calc.zt_rows(None), [])


    def test_names_and_industries_normalized(self):
        """涨停/炸板/跌停池：股票简称去空格（粤 传 媒），所属行业补全截断名、去掉“Ⅱ”。"""
        raw = make_zt([("002181", "粤 传 媒", 2, "093000", 0, 1e8, "互联网电"),
                       ("600864", "哈投股份", 1, "093100", 0, 1e8, "证券Ⅱ"),
                       ("600408", "安泰集团", 1, "093200", 0, 1e8, "焦炭Ⅱ"),
                       ("300001", "特锐德", 1, "093300", 0, 1e8, "自动化设"),
                       ("000001", "某　某", 1, "093400", 0, 1e8, None)])
        zt = calc.norm_pool(raw, "zt")
        self.assertEqual(zt["名称"].tolist()[:2], ["粤传媒", "哈投股份"])
        self.assertEqual(zt["所属行业"].tolist(), ["互联网电商", "证券", "焦炭", "自动化设备", ""])
        rows = calc.zt_rows(zt)
        self.assertEqual((rows[0]["name"], rows[0]["hy"]), ("粤传媒", "互联网电商"))
        levels = calc.ladder_levels(zt)
        self.assertEqual(levels[0]["items"][0]["text"], "粤传媒")
        self.assertTrue(levels[0]["items"][0]["sub"].startswith("互联网电商 · "))
        first = {it["text"]: it["sub"] for it in levels[1]["items"]}
        self.assertTrue(first["哈投股份"].startswith("证券 · "))
        self.assertEqual(first["某某"], "09:34")                          # 行业缺失时只显示时间
        self.assertEqual(calc.pool_metrics(zt, None, None, None)["max_board_names"], ["粤传媒"])
        # 直接用原始表（未经 norm_pool）也一样
        self.assertEqual(calc.zt_rows(raw)[0]["name"], "粤传媒")
        zb = calc.zb_rows(ZB.assign(名称=["园 林 股 份", "力王股份"], 所属行业=["房地产服", "农产品加"]))
        self.assertEqual({(r["name"], r["hy"]) for r in zb}, {("园林股份", "房地产服务"), ("力王股份", "农产品加工")})
        dt_ = calc.dt_rows(DT.assign(名称=["华 之 杰", "某某"], 所属行业=["计算机设", "证券Ⅱ"]))
        self.assertEqual([(r["name"], r["hy"]) for r in dt_], [("华之杰", "计算机设备"), ("某某", "证券")])
        # 旧的历史记录里存的带空格名称，读出来也去掉空格
        self.assertEqual(calc.from_history({"max_board_names": "粤 传 媒、新华传媒"})["max_board_names"],
                         ["粤传媒", "新华传媒"])


class TestHistory(unittest.TestCase):
    def test_roundtrip(self):
        m = {"zt_count": 69, "zb_count": 14, "dt_count": 8, "zb_rate": 16.8674, "max_board": 9,
             "max_board_names": ["x"], "prev_zt_avg_pct": 3.111}
        rec = calc.to_history(m, {"up": 3212, "down": 2047, "flat": 103}, 1.9e12)
        self.assertEqual(rec["zb_rate"], 16.87)
        self.assertEqual(rec["up_count"], 3212)
        self.assertEqual(rec["turnover"], 1.9e12)
        self.assertEqual(rec["max_board_names"], "x")
        row = {"date": "2026-10-09", **{k: (v if isinstance(v, str) else float(v)) for k, v in rec.items()},
               "other": 1.0, "zt_extra": float("nan")}
        back = calc.from_history(row)
        self.assertEqual(back["zt_count"], 69)
        self.assertIsInstance(back["zt_count"], int)
        self.assertEqual(back["max_board_names"], ["x"])
        self.assertNotIn("other", back)
        self.assertFalse(calc.needs_backfill(row))
        self.assertTrue(calc.needs_backfill({"zt_count": 1.0}))
        self.assertTrue(calc.needs_backfill(None))
        # 名称列为空（NaN）时不出现
        self.assertNotIn("max_board_names", calc.from_history({"max_board_names": float("nan"), "max_board": 3.0}))

    def test_names_roundtrip_via_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = History(Path(tmp) / "history.csv")
            h.upsert("2026-10-09", calc.to_history({"max_board": 3, "max_board_names": ["新华传媒", "九阳股份"]}))
            h.upsert("2026-10-08", {"zt_count": 5})
            rows = {r["date"]: r for r in h.load().to_dict("records")}
            self.assertEqual(calc.from_history(rows["2026-10-09"])["max_board_names"], ["新华传媒", "九阳股份"])
            self.assertNotIn("max_board_names", calc.from_history(rows["2026-10-08"]))

    def test_breadth_basis(self):
        """东方财富与乐咕的涨跌家数口径不同，写到不同的列。"""
        legu = calc.breadth_from_legu({"上涨": 2989.0, "下跌": 2108.0, "平盘": 113.0})
        rec = calc.to_history({}, legu)
        self.assertEqual((rec["up_count_legu"], rec["down_count_legu"], rec["flat_count_legu"]), (2989, 2108, 113))
        self.assertNotIn("up_count", rec)
        em = calc.breadth_from_fenbu({1: 10, -1: 5, 0: 2})
        self.assertEqual(calc.to_history({}, em)["up_count"], 10)
        b = calc.breadth_from_history(calc.from_history({**rec, "up_count": 3212.0, "down_count": 2047.0}))
        self.assertEqual((b["up"], b["basis"]), (3212, "em"))                # 优先东方财富口径
        b = calc.breadth_from_history(calc.from_history(rec))
        self.assertEqual((b["up"], b["total"], b["basis"]), (2989, 5210, "legu"))
        self.assertIsNone(calc.breadth_from_history({}))

    def test_trim(self):
        dates = ["a", "b", "c", "d"]
        rows = [{}, {}, {"zt_count": 1}, {}]
        d, r = calc.trim_leading_empty(dates, rows, "zt_count")
        self.assertEqual(d, ["c", "d"])
        self.assertEqual(calc.trim_leading_empty(dates, [{}] * 4, "zt_count"), ([], []))


class TestSummary(unittest.TestCase):
    def test_index_summary(self):
        idx = [{"name": "上证指数", "close": 3813.79, "pct": 0.05}, {"name": "深证成指", "close": 1, "pct": 0.17},
               {"name": "创业板指", "close": 1, "pct": 0.22}, {"name": "北证50", "close": 1, "pct": 2.49}]
        s = calc.index_summary(idx)
        self.assertIn("上证指数收于3813.79点（+0.05%），深证成指+0.17%、创业板指+0.22%", s)   # 统一带符号百分数
        self.assertIn("全部上涨", s)
        self.assertIn("北证50涨幅最大（+2.49%）", s)    # 前面没出现过，写数字
        self.assertIn("上证指数涨幅最小。", s)           # 前面已写过涨跌幅，不再重复数字
        self.assertEqual(s.count("+0.05%"), 1)
        idx[0]["pct"] = -1.2
        s = calc.index_summary(idx)
        self.assertIn("（-1.20%）", s)
        self.assertIn("3涨1跌", s)
        self.assertIn("上证指数跌幅最大", s)
        self.assertEqual(s.count("-1.20%"), 1)
        self.assertNotIn("3,", s)
        self.assertIsNone(calc.index_summary([]))
        s = calc.index_summary([{"name": "上证指数", "close": 3813.79, "pct": 0.05}])
        self.assertEqual(s, "上证指数收于3813.79点（+0.05%）。")

    def test_index_summary_flat(self):
        s = calc.index_summary([{"name": "A", "close": 1, "pct": 0.0}, {"name": "B", "close": 1, "pct": -0.5},
                                {"name": "C", "close": 1, "pct": -1.0}])
        self.assertIn("0涨2跌1平", s)
        self.assertNotIn("最小", s)                       # 不能把持平的指数说成“跌幅最小（+0.00%）”
        self.assertIn("C跌幅最大。", s)                     # C 已在前 3 个里写过 -1.00%
        self.assertEqual(s.count("-1.00%"), 1)
        s = calc.index_summary([{"name": n, "close": 1, "pct": 0.001} for n in "ABC"])
        self.assertIn("A收于1.00点（0.00%）", s)            # 按两位小数判断，0 不带正负号
        self.assertIn("全部持平", s)
        self.assertNotIn("最大", s)
        s = calc.index_summary([{"name": "A", "close": 1, "pct": 0.5}, {"name": "B", "close": 1, "pct": 0.0},
                                {"name": "C", "close": 1, "pct": 0.2}])
        self.assertIn("A涨幅最大。", s)
        self.assertNotIn("涨幅最小", s)
        # 7 个指数：前 3 个之外的最大/最小写数字
        idx = [{"name": f"I{i}", "close": 1, "pct": p} for i, p in enumerate([0.1, 0.2, 0.3, -1.5, 0.4, 2.0, 0.0])]
        s = calc.index_summary(idx)
        self.assertIn("7个主要指数5涨1跌1平", s)
        self.assertIn("I5涨幅最大（+2.00%）", s)
        self.assertIn("I3跌幅最大（-1.50%）", s)

    def test_board_summary_without_names(self):
        s = calc.board_summary({"max_board": 7, "lianban_count": 12, "promote_rate": 21.05})
        self.assertEqual(s, "最高连板7板；连板股12家，连板晋级率21.1%。")
        self.assertEqual(calc.board_summary({"max_board": 4}), "最高连板4板。")

    def test_other_summaries(self):
        s = calc.turnover_summary(1.9e12, 1.68e12)
        self.assertIn("1.90万亿", s)
        self.assertIn("放量2200亿（+13.1%）", s)
        self.assertIn("缩量", calc.turnover_summary(1.5e12, 1.68e12))
        self.assertIn("近20个交易日最高", calc.turnover_summary(2e12, 1.9e12, [1.8e12] * 19 + [2e12]))
        self.assertIsNone(calc.turnover_summary(None, None))
        self.assertIn("上涨家数占比60%", calc.breadth_summary({"up": 60, "down": 30, "flat": 10, "total": 100}))
        self.assertIn("炸板率16.9%", calc.limits_summary({"zt_count": 69, "dt_count": 8, "zb_count": 14,
                                                       "zb_rate": 16.87}))
        self.assertIsNone(calc.limits_summary({}))
        s = calc.breadth_limits_summary({"up": 60, "down": 30, "flat": 10, "total": 100},
                                        {"zt_count": 69, "dt_count": 8, "zb_count": 14, "zb_rate": 16.87})
        self.assertEqual(s, "上涨60家、下跌30家、平盘10家，上涨家数占比60%；涨停69家、跌停8家（不含ST），"
                            "炸板14家，炸板率16.9%。")
        self.assertEqual(calc.breadth_limits_summary(None, {"zt_count": 5}), "涨停5家（不含ST）。")
        self.assertIsNone(calc.breadth_limits_summary(None, {}))
        self.assertEqual(calc.FENBU_LABELS[-1], "-1~0%")
        self.assertEqual(calc.FENBU_LABELS[-2], "-2~-1%")
        self.assertEqual(calc.FENBU_LABELS[1], "0~1%")
        self.assertIn("均为首板", calc.board_summary({"max_board": 1}))
        self.assertIn("最高连板9板：新华传媒；连板股12家，连板晋级率27.9%",
                      calc.board_summary({"max_board": 9, "max_board_names": ["新华传媒"], "lianban_count": 12,
                                          "promote_rate": 27.91}))
        self.assertIn("+3.11%", calc.money_effect_summary({"prev_zt_avg_pct": 3.111, "prev_zt_up_ratio": 63}))
        # 合规：不出现建议/预测类字眼
        texts = [calc.turnover_summary(1.9e12, 1.68e12), calc.board_summary({"max_board": 9, "max_board_names": ["a"]})]
        for t in texts:
            for word in ("建议", "买入", "卖出", "持有", "预计", "看好"):
                self.assertNotIn(word, t)


class TestCharts(unittest.TestCase):
    def test_options_are_json(self):
        days = [dt.date(2026, 7, 1) + dt.timedelta(days=i) for i in range(80)]
        k = calc.norm_kline(make_kline(days))
        opts = [
            calc.kline_option(k, "上证指数", 60),
            calc.kline_option(calc.norm_kline(make_kline(days, amount=False)), "上证指数", 60),
            calc.normalized_option({"A": k, "B": k.assign(close=k["close"] * 2)}, k["date"].tail(60).tolist()),
            calc.turnover_option(["2026-10-08", "2026-10-09"], [1.6e12, None]),
            calc.limits_trend_option(["2026-10-09"], [69], [None], [16.87]),
            calc.board_trend_option(["2026-10-09"], [9.0], [3.11]),
        ]
        for o in opts:
            s = json.dumps(o, allow_nan=False, ensure_ascii=False)   # 不能有 NaN / 函数
            self.assertNotIn("function", s)
        kl = opts[0]
        self.assertEqual(len(kl["series"][0]["data"]), 60)
        self.assertEqual(kl["series"][-1]["name"], "成交额(亿元)")
        self.assertIsNotNone(kl["series"][3]["data"][0])          # MA20 用了更早的数据
        self.assertEqual(opts[1]["series"][-1]["name"], "成交量(亿股)")
        self.assertEqual(opts[2]["series"][0]["data"][0], 0.0)    # 区间首日为基准
        self.assertEqual(opts[2]["series"][1]["data"][0], 0.0)


# ---------------------------------------------------------------- 用假数据源跑 build()

def conn_error(name="x"):
    """模拟 Fetcher 重试后抛出的连接级失败。"""
    err = FetchError(f"{name} 连接失败")
    err.__cause__ = requests.ConnectionError("reset")
    return err


def format_error(name="x"):
    """模拟 akshare 格式变化（解析时 KeyError）后 Fetcher 抛出的失败。"""
    err = FetchError(f"{name} 获取失败（已重试 3 次）：'data'")
    err.__cause__ = KeyError("data")
    return err


class FakeFetcher:
    """按函数名返回合成数据。push2his 模拟被拦截；快照接口遵守 snapshot_ok。

    fail: {函数名: 异常工厂 或 callable(kw)->异常/None}；override: {函数名: callable(kw)->返回值}。
    """

    def __init__(self, trade_days, snapshot_ok, push2his_ok=False, fail=None, override=None):
        self.days = trade_days
        self.snapshot_ok = snapshot_ok
        self.push2his_ok = push2his_ok
        self.fail = fail or {}
        self.override = override or {}
        self.calls = []
        self.stats = {"network": 0, "cache": 0, "failed": 0}

    def names(self):
        return [c[0] for c in self.calls]

    def call(self, func, *args, snapshot=False, volatile=False, **kw):
        name = getattr(func, "__name__", "call")
        self.calls.append((name, kw))
        if snapshot and not self.snapshot_ok:
            raise SnapshotUnavailable(name)
        if name in self.fail:
            err = self.fail[name](kw)
            if err is not None:
                raise err
        if name in self.override:
            return self.override[name](kw)
        if name == "stock_zh_index_daily_em":
            if not self.push2his_ok:
                err = FetchError("push2his 被拦截")
                err.__cause__ = requests.ConnectionError("reset")
                raise err
            return make_kline([d for d in self.days if d.strftime("%Y%m%d") <= kw["end_date"]])
        if name == "sina_index_daily":
            return calc.norm_kline(make_kline(self.days, amount=False))
        if name == "stock_sse_deal_daily":
            return pd.DataFrame({"单日情况": ["成交金额"], "股票": [9000.0]})
        if name == "stock_szse_summary":
            return pd.DataFrame({"证券类别": ["股票"], "成交金额": [1.0e12]})
        if name == "stock_zt_pool_em":
            return ZT.copy()
        if name == "stock_zt_pool_zbgc_em":
            return ZB.copy()
        if name == "stock_zt_pool_dtgc_em":
            return DT.copy()
        if name == "stock_zt_pool_previous_em":
            return PREV.copy()
        if name == "em_zdfenbu":
            return {"qdate": kw["date"], "fenbu": calc.parse_fenbu(TestBreadth.RAW)}
        if name == "legu_market_activity":
            return {"上涨": 1.0, "下跌": 2.0, "平盘": 0.0}
        raise AssertionError(f"未预期的请求 {name}")


class TestBuild(unittest.TestCase):
    def setUp(self):
        self.saved_days = tc._DAYS
        today = dt.date.today()
        start = today - dt.timedelta(days=400)
        tc._DAYS = [start + dt.timedelta(days=i) for i in range(400) if (start + dt.timedelta(days=i)).weekday() < 5]
        self.trade_date = [d for d in tc._DAYS if d < today][-1]
        self.tmp = tempfile.TemporaryDirectory()
        self.history = History(Path(self.tmp.name) / "history.csv")

    def tearDown(self):
        tc._DAYS = self.saved_days
        self.tmp.cleanup()

    def run_build(self, snapshot_ok, push2his_ok=False, trade_date=None, fail=None, override=None):
        trade_date = trade_date or self.trade_date
        fetcher = FakeFetcher([d for d in tc._DAYS if d <= trade_date], snapshot_ok, push2his_ok, fail, override)
        ctx = Context(trade_date=trade_date, cfg={}, fetcher=fetcher, history=self.history,
                      snapshot_ok=snapshot_ok, log=logging.getLogger("kmcf.test"))
        return market.build(ctx).to_dict(), ctx, fetcher

    def test_degraded_mode(self):
        """push2his 被拦截 + 快照不可用：指数走新浪、成交额走交易所、无涨跌分布，其余照常。"""
        d, ctx, f = self.run_build(snapshot_ok=False)
        self.assertEqual(d["errors"], [])
        names = [c[0] for c in f.calls]
        self.assertEqual(names.count("stock_zh_index_daily_em"), 1)   # 失败一次后不再请求 push2his
        self.assertTrue(ctx.shared.get(mdata.PUSH2HIS_DOWN))
        labels = {k["label"]: k for k in d["kpis"]}
        self.assertEqual(labels["涨停家数（不含ST）"]["value"], "5")
        self.assertEqual(labels["沪深两市成交额"]["value"], "1.90万亿")
        self.assertNotIn("上涨 / 下跌家数", labels)
        titles = [b["title"] for b in d["blocks"]]
        self.assertFalse(any("涨跌分布" in t for t in titles))
        self.assertTrue(any("上证指数" in t for t in titles))
        self.assertTrue(any("连板梯队" in t for t in titles))
        self.assertTrue(any("只能取实时快照" in n for n in d["notes"]))
        self.assertTrue(any("新浪财经" in n for n in d["notes"]))
        self.assertTrue(any("上交所" in n for n in d["notes"]))
        rec = self.history.recent(self.trade_date.isoformat(), 1).iloc[0]
        self.assertEqual(int(rec["zt_count"]), 5)
        self.assertNotIn("up_count", rec.index)
        json.dumps(d, ensure_ascii=False, default=str)

    def test_normal_mode(self):
        """push2his 可用 + 快照可用：完整内容，涨跌家数写入 history。"""
        d, ctx, f = self.run_build(snapshot_ok=True, push2his_ok=True)
        self.assertEqual(d["errors"], [])
        labels = {k["label"]: k for k in d["kpis"]}
        self.assertEqual(labels["上涨 / 下跌家数"]["value"], "1079 / 754")
        self.assertEqual(labels["沪深两市成交额"]["value"], "1.00万亿")   # 两个合成指数各 5000 亿
        self.assertEqual(labels["上证指数"]["tone"], "up")
        titles = [b["title"] for b in d["blocks"]]
        self.assertTrue(any("涨跌分布" in t for t in titles))
        self.assertTrue(any("沪深两市成交额" in t for t in titles))
        self.assertTrue(any("涨跌停家数与炸板率" in t for t in titles))
        self.assertGreaterEqual(len(d["summary"]), 5)
        names = [c[0] for c in f.calls]
        self.assertNotIn("sina_index_daily", names)
        self.assertNotIn("stock_sse_deal_daily", names)
        rec = self.history.recent(self.trade_date.isoformat(), 1).iloc[0]
        self.assertEqual(int(rec["up_count"]), 1079)
        # 之后的运行：分批回填，补齐后只请求复盘日的 4 个池
        counts = [len(self.pool_calls(f))]
        for _ in range(6):
            d2, _, f2 = self.run_build(snapshot_ok=True, push2his_ok=True)
            counts.append(len(self.pool_calls(f2)))
            if counts[-1] == 4:
                break
        self.assertEqual(counts[0], 4 + 4 * market.SENTIMENT_BACKFILL_PER_RUN)
        self.assertEqual(counts[-1], 4)
        self.assertTrue(all(c <= 4 + 4 * market.SENTIMENT_BACKFILL_PER_RUN for c in counts))
        self.assertFalse(any("待补齐" in n for n in d2["notes"]))

    def test_old_date_uses_history(self):
        """复盘日超出涨停板专题回查窗口：不请求股票池，涨跌停指标取自 history。"""
        old = [d for d in tc._DAYS if d < dt.date.today() - dt.timedelta(days=60)][-1]
        self.history.upsert(old.isoformat(), {"zt_count": 50, "zb_count": 10, "dt_count": 3, "zb_rate": 16.67,
                                              "max_board": 4, "up_count": 3000, "down_count": 2000,
                                              "flat_count": 100})
        d, ctx, f = self.run_build(snapshot_ok=False, trade_date=old)
        names = [c[0] for c in f.calls]
        self.assertFalse(any(n.startswith("stock_zt_pool") for n in names))
        labels = {k["label"]: k for k in d["kpis"]}
        self.assertEqual(labels["涨停家数（不含ST）"]["value"], "50")
        self.assertEqual(labels["上涨 / 下跌家数"]["value"], "3000 / 2000")
        self.assertTrue(any("超过约30个自然日" in n for n in d["notes"]))
        self.assertFalse(any(b["type"] == "ladder" for b in d["blocks"]))
        # history 里没有名称时，要点不能出现“最高连板4板：；”
        self.assertIn("最高连板4板。", d["summary"])
        self.assertFalse(any("：；" in s for s in d["summary"]))

    # ---- 审查意见对应的场景

    def pool_calls(self, f, other_than=None):
        return [c for c in f.calls if c[0].startswith("stock_zt_pool")
                and (other_than is None or c[1]["date"] != other_than.strftime("%Y%m%d"))]

    def test_backfill_capped_newest_first(self):
        """新环境首跑：情绪与成交额都只回填一批（从近到远），并说明还有待补的交易日。"""
        d, ctx, f = self.run_build(snapshot_ok=False)
        backfill = self.pool_calls(f, other_than=self.trade_date)
        self.assertEqual(len(backfill), 4 * market.SENTIMENT_BACKFILL_PER_RUN)
        days = sorted({c[1]["date"] for c in backfill}, reverse=True)
        expect = [x.strftime("%Y%m%d") for x in reversed(ctx.recent_trade_days(market.SENTIMENT_BACKFILL_PER_RUN + 1)[:-1])]
        self.assertEqual(days, expect)                      # 紧挨复盘日的最近 5 个交易日
        self.assertTrue(any("待补齐" in n for n in d["notes"]))
        self.assertFalse(any("history.csv" in n for n in d["notes"]))   # 不写内部路径
        names = f.names()
        # 成交额：复盘日 + 前一日 + 一批回补（每天上交所、深交所各 1 次）
        self.assertEqual(names.count("stock_sse_deal_daily"), 2 + market.TURNOVER_BACKFILL_PER_RUN)
        self.assertEqual(names.count("stock_szse_summary"), 2 + market.TURNOVER_BACKFILL_PER_RUN)
        chart = next(b for b in d["blocks"] if b["title"].startswith("沪深两市成交额"))
        self.assertIn("待补", chart["note"])
        self.assertIn("深交所官网", chart["note"])

    def test_pool_format_change_is_error_not_zero(self):
        """炸板池/跌停池缺“代码”列：报错、不显示 0 家、不写 history，也不回填。"""
        d, ctx, f = self.run_build(snapshot_ok=True, override={
            "stock_zt_pool_zbgc_em": lambda kw: ZB.rename(columns={"代码": "股票代码"}),
            "stock_zt_pool_dtgc_em": lambda kw: DT.rename(columns={"代码": "股票代码"}),
        })
        errs = " ".join(d["errors"])
        self.assertIn("炸板池", errs)
        self.assertIn("跌停池", errs)
        self.assertIn("格式", errs)
        labels = {k["label"]: k for k in d["kpis"]}
        self.assertNotIn("炸板率", labels)
        self.assertNotIn("跌停家数（不含ST）", labels)
        self.assertEqual(labels["涨停家数（不含ST）"]["value"], "5")
        self.assertFalse(any("炸板0家" in s or "跌停0家" in s for s in d["summary"]))
        self.assertFalse(any(b["title"].startswith(("炸板池", "跌停池")) for b in d["blocks"]))
        rec = self.history.recent(self.trade_date.isoformat(), 1).iloc[0]
        self.assertTrue(pd.isna(rec.get("zb_count")) and pd.isna(rec.get("dt_count")))
        self.assertTrue(calc.needs_backfill(rec.to_dict()))     # 之后还会重新取这一天
        self.assertEqual(self.pool_calls(f, other_than=self.trade_date), [])
        self.assertTrue(any("暂时无法获取" in n and "留空" in n for n in d["notes"]))

    def test_zt_missing_name_does_not_crash(self):
        d, ctx, f = self.run_build(snapshot_ok=True, override={
            "stock_zt_pool_em": lambda kw: ZT.drop(columns=["名称"])})
        self.assertTrue(any(e.startswith("涨停池：") for e in d["errors"]))
        labels = {k["label"] for k in d["kpis"]}
        self.assertIn("上证指数", labels)                  # 其他部分照常
        self.assertIn("上涨 / 下跌家数", labels)
        self.assertIn("炸板池（2只）", [b["title"] for b in d["blocks"]])
        self.assertNotIn("涨停家数（不含ST）", labels)      # 没有涨停池、history 也没有，不显示而不是显示 0
        self.assertFalse(any("连板晋级率" in s for s in d["summary"]))

    def test_pool_failure_uses_history_with_note(self):
        """复盘日股票池连接失败：KPI 用 history 里当天的值，并说明来源；错误信息不暴露内部函数名。"""
        self.history.upsert(self.trade_date.isoformat(), {
            "zt_count": 52, "zb_count": 12, "dt_count": 9, "zb_rate": 18.75, "max_board": 7,
            "lianban_count": 12, "max_board_names": "新华传媒"})
        d, ctx, f = self.run_build(snapshot_ok=True, fail={n: lambda kw: conn_error() for n in (
            "stock_zt_pool_em", "stock_zt_pool_zbgc_em", "stock_zt_pool_dtgc_em", "stock_zt_pool_previous_em")})
        labels = {k["label"]: k for k in d["kpis"]}
        self.assertEqual(labels["涨停家数（不含ST）"]["value"], "52")
        self.assertEqual(labels["最高连板"]["sub"], "新华传媒")
        self.assertTrue(any("取自本地历史记录" in n and "情绪指标" in n for n in d["notes"]))
        self.assertTrue(any("最高连板7板：新华传媒" in s for s in d["summary"]))
        self.assertEqual(len(self.pool_calls(f)), 1)       # 第一次连接失败后，其余股票池不再请求
        self.assertFalse(any("stock_zt_pool" in e for e in d["errors"]))
        self.assertTrue(all("东方财富涨停板专题" in e for e in d["errors"] if "池" in e))

    def test_pool_format_errors_skip_backfill(self):
        """股票池接口非连接类错误（格式变化）：只尝试复盘日的 4 个池，不再逐日回填。"""
        d, ctx, f = self.run_build(snapshot_ok=True, fail={n: lambda kw: format_error() for n in (
            "stock_zt_pool_em", "stock_zt_pool_zbgc_em", "stock_zt_pool_dtgc_em", "stock_zt_pool_previous_em")})
        self.assertEqual(len(self.pool_calls(f)), 4)
        self.assertTrue(any("接口返回异常" in e for e in d["errors"]))
        self.assertFalse(any("'data'" in e for e in d["errors"]))

    def test_backfill_stops_on_first_failure(self):
        prev = self.trade_date
        prev = [x for x in tc._DAYS if x < prev][-1]
        bad = prev.strftime("%Y%m%d")
        d, ctx, f = self.run_build(snapshot_ok=True, push2his_ok=True, fail={
            "stock_zt_pool_em": lambda kw: format_error() if kw["date"] == bad else None})
        self.assertEqual(len(self.pool_calls(f, other_than=self.trade_date)), 1)   # 失败后立即停止
        self.assertTrue(any(f"{prev:%m-%d}的历史数据未取到" in n for n in d["notes"]))
        self.assertEqual(d["errors"], [])

    def test_network_down_short_circuits(self):
        """全网故障：每个站点只请求一次，不逐个指数/逐日重试。"""
        names = ("stock_zh_index_daily_em", "sina_index_daily", "stock_sse_deal_daily", "stock_szse_summary",
                 "em_zdfenbu", "legu_market_activity", "stock_zt_pool_em", "stock_zt_pool_zbgc_em",
                 "stock_zt_pool_dtgc_em", "stock_zt_pool_previous_em")
        d, ctx, f = self.run_build(snapshot_ok=True, fail={n: lambda kw: conn_error() for n in names})
        got = f.names()
        self.assertEqual(got.count("stock_zh_index_daily_em"), 1)
        self.assertEqual(got.count("sina_index_daily"), 1)
        self.assertEqual(got.count("stock_sse_deal_daily"), 1)
        self.assertEqual(got.count("stock_szse_summary"), 0)
        self.assertEqual(len(self.pool_calls(f)), 0)        # 涨跌分布（同一站点）已连接失败
        self.assertTrue(ctx.shared.get(mdata.SINA_DOWN) and ctx.shared.get(mdata.EXCHANGE_DOWN))
        self.assertTrue(d["errors"])
        self.assertEqual([k["value"] for k in d["kpis"]], ["—"] * len(calc.INDEXES))

    def test_turnover_from_history_note(self):
        """东方财富日线和交易所都不可用：成交额与趋势取自 history，注释不能说“已用交易所数据回补”。"""
        days = [x for x in tc._DAYS if x <= self.trade_date][-25:]
        for x in days:
            self.history.upsert(x.isoformat(), {"turnover": 1.5e12})
        d, ctx, f = self.run_build(snapshot_ok=True, fail={"stock_sse_deal_daily": lambda kw: conn_error()})
        chart = next(b for b in d["blocks"] if b["title"].startswith("沪深两市成交额"))
        self.assertIn("本地历史记录", chart["note"])
        self.assertNotIn("回补", chart["note"])
        self.assertTrue(any("取自本地历史记录" in n and "成交额" in n for n in d["notes"]))

    def test_exchange_mode_recent_high(self):
        """交易所备用模式下，“近N个交易日最高”也基于 history 序列判断。"""
        days = [x for x in tc._DAYS if x < self.trade_date][-30:]
        for x in days:
            self.history.upsert(x.isoformat(), {"turnover": 1.0e12})
        d, ctx, f = self.run_build(snapshot_ok=True)
        line = next(s for s in d["summary"] if s.startswith("沪深两市成交额"))
        self.assertIn("1.90万亿", line)
        self.assertIn("个交易日最高", line)

    def test_breadth_basis_in_history(self):
        """东方财富涨跌分布失败、改用乐咕：家数写到 *_legu 列，KPI 标明口径。"""
        d, ctx, f = self.run_build(snapshot_ok=True, push2his_ok=True,
                                   fail={"em_zdfenbu": lambda kw: format_error("em_zdfenbu")})
        labels = {k["label"]: k for k in d["kpis"]}
        self.assertIn("含ST", labels["上涨 / 下跌家数"]["sub"])
        rec = self.history.recent(self.trade_date.isoformat(), 1).iloc[0]
        self.assertEqual(int(rec["up_count_legu"]), 1)
        self.assertNotIn("up_count", rec.index)

    def test_em_breadth_scope_mentions_bse(self):
        d, ctx, f = self.run_build(snapshot_ok=True, push2his_ok=True)
        labels = {k["label"]: k for k in d["kpis"]}
        self.assertIn("含北交所", labels["上涨 / 下跌家数"]["sub"])
        self.assertTrue(any("含北交所" in n and "涨跌分布" in n for n in d["notes"]))


if __name__ == "__main__":
    unittest.main()
