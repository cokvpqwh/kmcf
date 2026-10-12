"""板块轮动模块的单元测试（合成数据，不联网）。

运行：.venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import datetime as dt
import json
import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kmcf.fetch import FetchError, SnapshotUnavailable, cache_key  # noqa: E402
from kmcf.render import render_report  # noqa: E402
from kmcf.section import Section  # noqa: E402
from kmcf.sections import _sectors_calc as calc  # noqa: E402
from kmcf.sections import _sectors_data as data  # noqa: E402
from kmcf.sections import sectors  # noqa: E402
from kmcf.sections._sectors_store import SectorStore  # noqa: E402


def ths_industry_raw():
    return pd.DataFrame({
        "序号": [1, 2, 3, 4],
        "板块": ["影视院线", "贵金属", "元件", "银行"],
        "涨跌幅": [7.23, 5.27, -3.63, 0.0],
        "总成交量": [1609.08, 1045.12, 3000.0, 100.0],
        "总成交额": [105.94, 265.47, 300.0, 50.0],   # 亿元
        "净流入": [16.04, 23.02, -27.33, 1.5],      # 亿元
        "上涨家数": [20, 14, 3, 20],
        "下跌家数": [0, 0, 60, 10],
        "均价": [6.58, 25.4, 30.0, 5.0],
        "领涨股": ["捷成股份", "西部黄金", "澳弘电子", "某银行"],
        "领涨股-最新价": [5.56, 26.55, 53.35, 5.0],
        "领涨股-涨跌幅": [10.98, 9.98, 10.0, 1.0],
    })


def ths_flow_raw(names, pcts, nets):
    """同花顺资金流（行业/概念同结构），净额单位亿元。"""
    n = len(names)
    return pd.DataFrame({"序号": range(1, n + 1), "行业": names, "行业指数": [1000.0] * n, "行业-涨跌幅": pcts,
                         "流入资金": [10.0] * n, "流出资金": [9.0] * n, "净额": nets, "公司家数": [20] * n,
                         "领涨股": ["某股"] * n, "领涨股-涨跌幅": [10.0] * n, "当前价": [5.0] * n})


def zt_pool_raw(rows):
    """rows: [(代码, 名称, 所属行业, 连板数, 首次封板时间)]"""
    return pd.DataFrame(rows, columns=["代码", "名称", "所属行业", "连板数", "首次封板时间"])


def conn_error(msg="连接被重置"):
    """模拟 Fetcher 包装后的连接级失败（FetchError 以 requests 异常为 cause）。"""
    try:
        raise requests.ConnectionError(msg)
    except requests.ConnectionError as e:
        err = FetchError(f"获取失败：{msg}")
        err.__cause__ = e
        return err


class TestStandardize(unittest.TestCase):
    def test_ths_industry_units_and_order(self):
        df = calc.standardize(ths_industry_raw(), calc.THS_INDUSTRY_MAP, calc.THS_INDUSTRY_SCALE)
        self.assertEqual(list(df.columns), calc.STD_COLS)
        self.assertEqual(list(df["name"]), ["影视院线", "贵金属", "银行", "元件"])  # 按涨跌幅降序
        self.assertAlmostEqual(df.loc[0, "amount"], 105.94e8)
        self.assertAlmostEqual(df.loc[3, "net"], -27.33e8)
        self.assertEqual(df.loc[0, "lead"], "捷成股份")
        self.assertTrue(df["count"].isna().all())

    def test_ths_concept_units(self):
        raw = ths_flow_raw(["AI视频", "MLOps概念"], ["5.48%", "1.20%"], [23.42, 0.80])
        df = data.standardize("concept", "ths", raw)
        self.assertAlmostEqual(df.loc[0, "pct"], 5.48)
        self.assertAlmostEqual(df.loc[0, "net"], 23.42e8)
        self.assertAlmostEqual(df.loc[1, "net"], 0.80e8)
        self.assertEqual(df.loc[1, "count"], 20)

    def test_sina_industry_units_and_exclusion(self):
        raw = pd.DataFrame({"label": ["new_ysjs", "new_cxg", "new_blhy"], "板块": ["有色金属", "次新股", "玻璃行业"],
                            "公司家数": [80, 50, 20], "涨跌幅": [2.5, 9.0, -1.0], "总成交额": [3.2e10, 1e9, 5e8],
                            "股票名称": ["甲", "乙", "丙"], "个股-涨跌幅": [10.0, 20.0, 1.0]})
        df = data.standardize("industry", "sina", raw)
        self.assertEqual(list(df["name"]), ["有色金属", "玻璃行业"])  # 次新股不是行业，剔除
        self.assertAlmostEqual(df.loc[0, "amount"], 3.2e10)          # 新浪成交额已是元
        self.assertEqual(df.loc[0, "code"], "new_ysjs")
        self.assertTrue(df["net"].isna().all())
        con = data.standardize("concept", "sina", raw)
        self.assertIn("次新股", list(con["name"]))  # 概念的剔除由 filter_concepts 负责

    def test_em_strings_dashes_and_duplicates(self):
        raw = pd.DataFrame([
            {"f12": "BK1027", "f14": "小金属", "f3": 2.5, "f6": 1.2e10, "f62": 3e8, "f104": 20, "f105": 5,
             "f128": "某股", "f136": 10.01},
            {"f12": "BK0001", "f14": "半导体", "f3": "-", "f6": "-", "f62": "-", "f104": "-", "f105": "-",
             "f128": "-", "f136": "-"},
            {"f12": "BK1027", "f14": "小金属", "f3": 2.5, "f6": 1, "f62": 1, "f104": 1, "f105": 1,
             "f128": "x", "f136": 1},
            {"f12": "BK9999", "f14": "", "f3": 1.0},
        ])
        df = calc.standardize(raw, calc.EM_MAP)
        self.assertEqual(list(df["name"]), ["小金属", "半导体"])
        self.assertEqual(df.loc[0, "code"], "BK1027")
        self.assertAlmostEqual(df.loc[0, "net"], 3e8)
        self.assertTrue(pd.isna(df.loc[1, "pct"]))
        self.assertEqual(df.loc[1, "lead"], "")

    def test_to_num_percent_strings(self):
        s = pd.Series(["7.23%", "-1,234.5", None, "--"], dtype="str")
        out = calc.to_num(s)
        self.assertAlmostEqual(out[0], 7.23)
        self.assertAlmostEqual(out[1], -1234.5)
        self.assertTrue(out[2:].isna().all())

    def test_empty(self):
        self.assertTrue(calc.standardize(None, calc.SINA_MAP).empty)
        self.assertEqual(list(calc.standardize(pd.DataFrame(), calc.SINA_MAP).columns), calc.STD_COLS)

    def test_fmt_flow(self):
        self.assertEqual(calc.fmt_flow(0.8e8, coarse=True), "+0.80亿")
        self.assertEqual(calc.fmt_flow(-12.06e8, coarse=True), "-12.06亿")
        self.assertEqual(calc.fmt_flow(0.8e8), "+8000.00万")
        self.assertEqual(calc.fmt_flow(None, coarse=True), "—")


class TestConceptFilter(unittest.TestCase):
    def test_exclusions(self):
        for name in ["融资融券", "昨日涨停_含一字", "昨日连板", "深股通", "沪股通", "MSCI中国", "标准普尔",
                     "富时罗素概念", "转债标的", "预盈预增", "同花顺漂亮100", "HS300_", "基金重仓", "证金持股", "",
                     # 同花顺 / 新浪实际出现过的非题材标签
                     "2026一季报预增", "2026中报预增", "科创次新股", "摘帽", "摘帽概念", "高股息精选", "超级品牌",
                     "中字头股票", "ST板块", "准ST股", "含GDR", "含B股", "含H股", "央企50", "超大盘", "科创50",
                     "业绩预升", "参股金融", "本月解禁"]:
            self.assertFalse(calc.is_theme_concept(name), name)
        for name in ["AI应用", "短剧游戏", "转基因", "低空经济", "中特估", "DeepSeek概念", "重组蛋白", "预制菜",
                     "固态电池", "IP经济(谷子经济)", "商业航天"]:
            self.assertTrue(calc.is_theme_concept(name), name)

    def test_filter_df(self):
        df = pd.DataFrame({"name": ["AI应用", "融资融券", "固态电池"], "pct": [3.0, 2.0, 1.0]})
        self.assertEqual(list(calc.filter_concepts(df)["name"]), ["AI应用", "固态电池"])


class TestRanking(unittest.TestCase):
    def setUp(self):
        self.df = pd.DataFrame({"name": list("ABCDEFG"), "pct": [5, 4, 3, 2, 1, -1, -2],
                                "net": [1e8, -2e8, 3e8, None, -1e8, 0.0, 2e8]})

    def test_top_bottom_no_overlap(self):
        top, bottom = calc.top_bottom(self.df, "pct", 3, 2)
        self.assertEqual(list(top["name"]), ["A", "B", "C"])
        self.assertEqual(list(bottom["name"]), ["F", "G"])
        top, bottom = calc.top_bottom(self.df.head(4), "pct", 3, 5)  # 不足时不重叠
        self.assertEqual(list(bottom["name"]), ["D"])

    def test_flow_split(self):
        inflow, outflow = calc.flow_split(self.df, 2)
        self.assertEqual(list(inflow["name"]), ["C", "G"])
        self.assertEqual(list(outflow["name"]), ["E", "B"])  # 流出最多的在最后

    def test_ranked(self):
        r = calc.ranked(self.df)
        self.assertEqual(list(r["rank"]), list(range(1, 8)))

    def test_top_ties(self):
        df = pd.DataFrame({"industry": ["甲", "乙", "丙"], "count": [4, 4, 2]})
        self.assertEqual(calc.top_ties(df, "count", "industry"), ["甲", "乙"])
        self.assertEqual(calc.top_ties(df.iloc[0:0], "count", "industry"), [])


class TestZt(unittest.TestCase):
    def test_zt_by_industry(self):
        zt = zt_pool_raw([
            ("000001", "甲", "电池", 1, "100000"), ("000002", "乙", "电池", 3, "093000"),
            ("000003", "丙", "出版", 2, "092500"), ("000004", "丁", None, 1, "140000"),
            ("000005", "戊", "电池", 3, "092500"),
        ])
        s = calc.zt_by_industry(zt)
        self.assertEqual(list(s["industry"]), ["电池", "出版", "其他"])
        self.assertEqual(list(s["count"]), [3, 1, 1])
        self.assertEqual(s.loc[0, "max_board"], 3)
        # 连板数降序，同板数按首次封板时间升序
        self.assertEqual(s.loc[0, "stocks"], [("戊", 3), ("乙", 3), ("甲", 1)])
        self.assertTrue(calc.zt_by_industry(pd.DataFrame()).empty)

    def test_industry_names_normalized(self):
        """行业名补全/去“Ⅱ”已移到 _names.normalize_industry（逐例测试见 test_names.py），这里只测汇总时的合并。"""
        zt = zt_pool_raw([("1", "a", "汽车零部", 1, "0930"), ("2", "b", "汽车零部件", 1, "0931"),
                          ("3", "粤 传 媒", "证券Ⅱ", 2, "0932"), ("4", None, "", 1, "0933")])
        s = calc.zt_by_industry(zt)
        self.assertEqual(s.to_dict("records")[0]["industry"], "汽车零部件")
        self.assertEqual(s.loc[0, "count"], 2)  # 截断名与全名合并统计
        rec = s.set_index("industry")
        self.assertEqual(rec.at["证券", "stocks"], [("粤传媒", 2)])   # 去掉“Ⅱ”，股票简称去空格
        self.assertEqual(rec.at["其他", "stocks"], [("4", 1)])       # 行业缺失归“其他”，名称缺失用代码

    def test_zt_matrix_missing_day(self):
        d1 = zt_pool_raw([("1", "a", "电池", 1, "0930"), ("2", "b", "电池", 1, "0930")])
        d3 = zt_pool_raw([("3", "c", "出版", 1, "0930")])
        m = calc.zt_matrix({"2026-10-08": d1, "2026-10-09": None, "2026-10-10": d3})
        self.assertEqual(list(m.columns), ["2026-10-08", "2026-10-09", "2026-10-10"])
        self.assertEqual(m.at["电池", "2026-10-08"], 2)
        self.assertEqual(m.at["电池", "2026-10-10"], 0)
        self.assertTrue(m["2026-10-09"].isna().all())
        self.assertTrue(calc.zt_matrix({"d": None}).empty)
        # 有列但 0 行 = 当日无涨停，整列为 0（不是缺失）
        m2 = calc.zt_matrix({"a": d1, "b": zt_pool_raw([])})
        self.assertEqual(m2.at["电池", "b"], 0)


class TestRotation(unittest.TestCase):
    def test_streak_and_compound(self):
        self.assertEqual(calc.streak([1, -1, 2, 3]), 2)
        self.assertEqual(calc.streak([1, None, 2]), 1)
        self.assertEqual(calc.streak([1, 2, -0.5]), 0)
        self.assertEqual(calc.streak([3, 12, 5], lambda r: r <= 10), 1)
        self.assertAlmostEqual(calc.compound([10, -10]), -1.0)
        self.assertIsNone(calc.compound([None, float("nan")]))

    def test_streak_open_ended(self):
        self.assertEqual(calc.streak_info([1, 2, 3]), (3, True))        # 数到窗口起点：实际可能更长
        self.assertEqual(calc.streak_info([-1, 1, 2]), (2, False))
        self.assertEqual(calc.streak_info([None, 1]), (1, True))        # 前一天缺失：未知
        self.assertEqual(calc.streak_info([1, -2]), (0, False))
        self.assertEqual(calc.streak_info([]), (0, False))
        self.assertEqual(calc.fmt_streak(5, True), "≥5")
        self.assertEqual(calc.fmt_streak(3, False), "3")
        self.assertEqual(calc.fmt_streak(0, True), "0")
        self.assertEqual(calc.streak_words(5, True), "至少5日")
        self.assertEqual(calc.streak_words(3, False), "3日")

    def test_rotation_rows(self):
        dates = ["d1", "d2", "d3"]
        pct = pd.DataFrame({"d1": [1.0, None], "d2": [2.0, -1.0], "d3": [3.0, 4.0]}, index=["A", "B"])
        rank = pd.DataFrame({"d1": [None, None], "d2": [5.0, 30.0], "d3": [1.0, 2.0]}, index=["A", "B"])
        rows = {r["name"]: r for r in calc.rotation_rows(pct, rank, top=10)}
        self.assertAlmostEqual(rows["A"]["cum"], (1.01 * 1.02 * 1.03 - 1) * 100)
        self.assertEqual(rows["A"]["up_days"], "3/3")
        self.assertEqual(rows["A"]["streak"], 3)
        self.assertEqual(rows["A"]["streak_txt"], "≥3")             # 整个窗口都在涨
        self.assertEqual(rows["A"]["top_hits"], 2)      # 只统计有完整排行的 d2、d3
        self.assertEqual(rows["A"]["top_streak"], 2)
        self.assertEqual(rows["A"]["top_streak_txt"], "≥2")         # d1 没有排行记录
        self.assertIsNone(rows["B"]["cum"])             # 有缺失日不算累计
        self.assertEqual(rows["B"]["up_days"], "1/2")
        self.assertEqual(rows["B"]["streak_txt"], "1")
        self.assertEqual(rows["B"]["top_streak"], 1)
        self.assertEqual(rows["B"]["top_streak_txt"], "1")
        no_rank = calc.rotation_rows(pct[dates], None)
        self.assertNotIn("top_hits", no_rank[0])

    def test_presence(self):
        rank = pd.DataFrame({"d1": [1.0, 20.0, 3.0], "d2": [2.0, 5.0, 40.0]}, index=["A", "B", "C"])
        p = calc.presence(rank, top=10).set_index("name")
        self.assertEqual(p.at["A", "hits"], 2)
        self.assertEqual(p.at["A", "streak"], 2)
        self.assertTrue(p.at["A", "streak_open"])
        self.assertFalse(p.at["B", "streak_open"])
        self.assertEqual(p.at["C", "streak"], 0)
        self.assertTrue(calc.presence(None).empty)

    def test_trend_text(self):
        stats = [
            {"name": "A", "cum": 5.0, "streak": 1, "top_streak": 3},
            {"name": "B", "cum": -1.0, "streak": 4},
            {"name": "C", "cum": None, "streak": 4},
        ]
        text = sectors._trend_text(stats, 5, 5)
        self.assertIn("今日涨幅前3的行业近5日累计：A+5.00%、B-1.00%、C（缺少完整数据）", text)
        self.assertIn("B、C已连涨4日", text)
        self.assertIn("A（3日）", text)
        self.assertTrue(text.endswith("。"))
        self.assertEqual(sectors._trend_text([{"name": "x", "cum": None, "streak": 0}], 5), "")
        # 完整排行不足 3 天：不写“连续位列涨幅前N”
        self.assertNotIn("连续位列", sectors._trend_text(stats, 5, 2))
        # 连续上榜不足 3 日：不写
        stats[0]["top_streak"] = 2
        self.assertNotIn("连续位列", sectors._trend_text(stats, 5, 5))

    def test_trend_text_first_lacks_history(self):
        stats = [{"name": "A", "cum": None, "streak": 1}, {"name": "B", "cum": 2.0, "streak": 1},
                 {"name": "C", "cum": 1.0, "streak": 5, "streak_open": True, "top_streak": 5, "top_streak_open": True}]
        text = sectors._trend_text(stats, 5, 3)
        self.assertIn("今日涨幅前3的行业近5日累计：A（缺少完整数据）、B+2.00%、C+1.00%", text)
        self.assertNotIn("前2", text)
        self.assertIn("C已连涨至少5日", text)
        self.assertIn("C（至少5日）", text)

    def test_zt_hot_text(self):
        dates = ["d1", "d2", "d3", "d4", "d5"]
        m = pd.DataFrame({d: [3.0, 2.0, 2.0, 1.0, 1.0, 1.0, 1.0, 1.0] for d in dates},
                         index=["电池", "化学制品", "出版", "汽车零部件", "化学制药", "电力", "房地产开发", "零星"])
        m.loc["零星", ["d1", "d2", "d3"]] = 0.0     # 只有 2 天有涨停，不够 4 天
        zt_trend = {"m": m, "days_with": (m > 0).sum(axis=1), "n_days": 5}
        text = sectors._zt_hot_text(zt_trend)
        self.assertTrue(text.startswith("近5个交易日中至少4天出现涨停的行业：电池（15只次）"))
        self.assertIn("等7个行业", text)
        self.assertTrue(text.endswith("。"))
        self.assertNotIn("零星", text)
        few = {"m": m.head(2), "days_with": (m.head(2) > 0).sum(axis=1), "n_days": 5}
        self.assertNotIn("等", sectors._zt_hot_text(few))
        self.assertEqual(sectors._zt_hot_text({"m": m, "days_with": m.sum(axis=1), "n_days": 2}), "")


class TestNotes(unittest.TestCase):
    def _st(self, **kw):
        st = {"from_store": [], "ind_src": "ths", "ind_net": "ths_flow", "con_src": "ths", "con_net": "ths_flow"}
        st.update(kw)
        return st

    def test_same_source_merged(self):
        """行业与概念的数据源说明相同时合并成一条。"""
        sec = Section("sectors", "板块轮动")
        sectors._notes(sec, self._st())
        src = [n for n in sec.notes if "数据源" in n]
        self.assertEqual(len(src), 1)
        self.assertTrue(src[0].startswith("行业、概念排行数据源：同花顺；净流入为同花顺资金流向“净额”"))

    def test_different_sources_kept_apart(self):
        sec = Section("sectors", "板块轮动")
        sectors._notes(sec, self._st(ind_failed=["东方财富（连接失败或超时）"]))
        src = [n for n in sec.notes if "数据源" in n]
        self.assertEqual(len(src), 2)
        self.assertIn("行业排行数据源：同花顺（东方财富暂时无法获取，已改用此源）", src[0])
        self.assertTrue(src[1].startswith("概念排行数据源：同花顺"))

    def test_zt_note_mentions_ranking_only_when_present(self):
        stats = pd.DataFrame({"industry": ["电池"], "count": [1]})
        sec = Section("sectors", "板块轮动")
        sectors._notes(sec, self._st(zt_stats=stats, ind=pd.DataFrame()))
        self.assertTrue(any("与上方行业排行的数据源和分类不同" in n for n in sec.notes))
        sec = Section("sectors", "板块轮动")
        sectors._notes(sec, {"from_store": [], "zt_stats": stats})
        self.assertFalse(any("上方" in n for n in sec.notes))


class TestChartOptions(unittest.TestCase):
    def test_hbar(self):
        opt = calc.hbar_option(["A", "⋯", "B"], [4.0, None, -1.5])
        json.dumps(opt)
        items = opt["series"][0]["data"]
        self.assertEqual(items[0]["value"], "4.00")       # 定点字符串，标签显示 4.00
        self.assertEqual(items[0]["itemStyle"]["color"], calc.UP)
        self.assertEqual(items[1], "-")
        self.assertEqual(items[2]["itemStyle"]["color"], calc.DOWN)
        self.assertEqual(items[2]["label"]["position"], "left")
        self.assertEqual(opt["xAxis"]["boundaryGap"][0], "16%")
        self.assertTrue(opt["yAxis"]["inverse"])

    def test_heatmap(self):
        m = pd.DataFrame({"d1": [1.234, None], "d2": [-8.0, 0.0]}, index=["A", "B"])
        opt = calc.heatmap_option(["A", "B"], ["d1", "d2"], ["1", "2"], m)
        json.dumps(opt)
        self.assertEqual(opt["series"][0]["data"], [[0, 0, "1.23"], [1, 0, "-8.00"], [1, 1, "0.00"]])
        self.assertEqual(opt["visualMap"]["min"], -opt["visualMap"]["max"])
        cnt = calc.heatmap_option(["A", "B"], ["d1", "d2"], ["1", "2"], m.fillna(0).abs(), diverging=False, digits=0)
        self.assertEqual(len(cnt["series"][0]["data"]), 2)  # 0 值留空
        self.assertEqual(cnt["visualMap"]["min"], 1)

    def test_balance_spans(self):
        sec = Section("t", "t")
        for span in ("full", "half", "half", "half", "full", "half"):
            sec.chart(span, {}, span=span)
        sectors._balance_spans(sec)
        self.assertEqual([b["span"] for b in sec.blocks], ["full", "half", "half", "full", "full", "full"])


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SectorStore(Path(self.tmp.name) / "sector_daily.csv")
        self.day1 = pd.DataFrame({"name": ["A", "B", "C"], "pct": [3.0, 1.0, -2.0], "net": [1e8, None, -1e8]})

    def tearDown(self):
        self.tmp.cleanup()

    def test_upsert_and_matrix(self):
        self.store.upsert_day("2026-10-08", "industry", "ths", self.day1)
        self.store.upsert_day("2026-10-09", "industry", "ths", self.day1.assign(pct=[1.0, 2.0, 3.0]))
        self.store.upsert_day("2026-10-09", "industry", "ths", self.day1.assign(pct=[-1.0, 2.0, 3.0]))  # 覆盖
        self.store.upsert_day("2026-10-09", "concept", "ths", self.day1)
        dates = ["2026-10-07", "2026-10-08", "2026-10-09"]
        m = self.store.matrix("industry", "ths", dates)
        self.assertEqual(list(m.columns), dates)
        self.assertTrue(m["2026-10-07"].isna().all())
        self.assertEqual(m.at["A", "2026-10-09"], -1.0)
        r = self.store.matrix("industry", "ths", dates, value="rank", full_only=True)
        self.assertEqual(r.at["C", "2026-10-09"], 1)
        self.assertEqual(self.store.full_dates("industry", "ths", dates), ["2026-10-08", "2026-10-09"])
        day, src = self.store.day("industry", "2026-10-09")
        self.assertEqual(src, "ths")
        self.assertEqual(list(day["name"]), ["C", "B", "A"])
        self.assertIsNone(self.store.day("industry", "2026-01-01")[1])

    def test_partial_does_not_override(self):
        self.store.upsert_day("2026-10-08", "industry", "ths", self.day1)
        self.store.add_partial("industry", "ths", "A", {"2026-10-08": 99.0, "2026-10-07": 1.5})
        self.store.add_partial("industry", "ths", "A", {"2026-10-07": 7.0})  # 已有不覆盖
        m = self.store.matrix("industry", "ths", ["2026-10-07", "2026-10-08"])
        self.assertEqual(m.at["A", "2026-10-08"], 3.0)
        self.assertEqual(m.at["A", "2026-10-07"], 1.5)
        self.assertEqual(self.store.full_dates("industry", "ths", ["2026-10-07", "2026-10-08"]), ["2026-10-08"])
        r = self.store.matrix("industry", "ths", ["2026-10-07"], value="rank", full_only=True)
        self.assertTrue(r.empty or r.isna().all().all())

    def test_prune(self):
        with mock.patch("kmcf.sections._sectors_store.KEEP_DATES", 2):
            for d in ("2026-10-07", "2026-10-08", "2026-10-09"):
                self.store.upsert_day(d, "industry", "ths", self.day1)
        self.assertEqual(sorted(self.store.load()["date"].unique()), ["2026-10-08", "2026-10-09"])


# ---------------------------------------------------------------- 数据层（假 ctx，不联网）

class FakeFetcher:
    def __init__(self, raw_dir, force=False):
        self.raw_dir = Path(raw_dir)
        self.force = force


class FakeCtx:
    def __init__(self, raw_dir, snapshot_ok=True, fetch_impl=None, trade_date=dt.date(2026, 10, 9), force=False):
        self.trade_date = trade_date
        self.cfg = {"top_n": 10}
        self.fetcher = FakeFetcher(raw_dir, force)
        self.snapshot_ok = snapshot_ok
        self.log = logging.getLogger("kmcf.test")
        self.shared = {}
        self.calls = []
        self._impl = fetch_impl or (lambda func, *a, **kw: func(*a, **kw))

    @property
    def date_str(self):
        return self.trade_date.strftime("%Y%m%d")

    @property
    def date_iso(self):
        return self.trade_date.isoformat()

    def fetch(self, func, *args, snapshot=False, volatile=False, **kwargs):
        self.calls.append(func.__name__)
        return self._impl(func, *args, **kwargs)

    def recent_trade_days(self, n):
        days = [dt.date(2026, 9, 28), dt.date(2026, 9, 29), dt.date(2026, 9, 30), dt.date(2026, 10, 8), self.trade_date]
        return days[-n:]


class FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def ths_flow_page(rows, pages):
    head = "".join(f"<th>{c}</th>" for c in data.THS_FLOW_COLS)
    body = "".join(
        f"<tr><td>{i}</td><td>{n}</td><td>1000.5</td><td>{p}</td><td>10.5</td><td>9.5</td><td>{net}</td>"
        f"<td>20</td><td>某股</td><td>10.01%</td><td>5.5</td></tr>" for i, (n, p, net) in enumerate(rows, 1))
    return (f'<div class="m-page"><span class="page_info">1/{pages}</span></div>'
            f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")


class TestDataLayer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _cache(self, func, kwargs, obj):
        pd.to_pickle(obj, Path(self.tmp.name) / f"{cache_key(func, (), kwargs)}.pkl")

    def test_with_timeout(self):
        def quick(x):
            return x * 2

        def slow():
            time.sleep(2)

        def boom():
            raise ValueError("bad")

        self.assertEqual(data.with_timeout(quick, 5)(3), 6)
        self.assertEqual(data.with_timeout(quick, 5).__name__, "quick")  # 缓存键不变
        wrapped = data.with_timeout(slow, 0.2)
        with self.assertRaises(TimeoutError):
            wrapped()
        t0 = time.monotonic()
        with self.assertRaises(TimeoutError) as cm:
            wrapped()  # 重试立即失败，并保留超时原因
        self.assertLess(time.monotonic() - t0, 0.1)
        self.assertIn("未返回", str(cm.exception))
        with self.assertRaises(ValueError):
            data.with_timeout(boom, 5)()

    def test_site_flag_skips_after_connection_failure(self):
        def failing(func, *a, **kw):
            raise conn_error()

        ctx = FakeCtx(self.tmp.name, fetch_impl=failing)

        def em_func():
            return pd.DataFrame()

        with self.assertRaises(FetchError):
            data.fetch(ctx, "em", em_func, snapshot=True)
        self.assertTrue(ctx.shared["push2_down"])
        with self.assertRaises(data.SiteSkipped) as cm:
            data.fetch(ctx, "em", em_func, snapshot=True)
        self.assertEqual(ctx.calls, ["em_func"])  # 第二次没有再请求
        # 报告文字只写“暂时无法访问”（“3 小时内不再尝试”等运行时状态只写日志）
        self.assertIn("暂时无法访问", str(cm.exception))
        self.assertNotIn("小时", str(cm.exception))
        self.assertEqual(data.reason(cm.exception), "暂时无法访问")
        ctx.shared["ths_down"] = True             # 只在本次生成内有效的标记
        with self.assertRaises(data.SiteSkipped) as cm2:
            data.fetch(ctx, "ths", em_func, snapshot=True)
        self.assertIn("暂时无法访问", str(cm2.exception))
        self.assertNotIn("本次", str(cm2.exception))
        self.assertEqual(data.reason(cm2.exception), "暂时无法访问")
        # 有当天缓存时不跳过
        self._cache(em_func, {}, "x")
        ctx._impl = lambda func, *a, **kw: "cached"
        self.assertEqual(data.fetch(ctx, "em", em_func), "cached")

    def test_non_network_error_does_not_flag(self):
        def wrap(func, *a, **kw):
            try:
                return func(*a, **kw)
            except Exception as e:
                raise FetchError(f"{func.__name__} 获取失败") from e

        ctx = FakeCtx(self.tmp.name, fetch_impl=wrap)

        def local_bug():
            raise AttributeError("'NoneType' object has no attribute 'write'")

        def site_down():
            raise requests.ConnectionError("reset")

        with self.assertRaises(FetchError):
            data.fetch(ctx, "ths", local_bug)
        self.assertNotIn("ths_down", ctx.shared)   # 解析/本地错误不影响同站其他接口和其他模块
        with self.assertRaises(FetchError):
            data.fetch(ctx, "ths", site_down)
        self.assertTrue(ctx.shared["ths_down"])

    def test_fetch_survives_missing_stderr(self):
        from tqdm import tqdm

        def uses_tqdm():
            return sum(i for i in tqdm(range(3), leave=False))

        ctx = FakeCtx(self.tmp.name)
        old = sys.stderr
        try:
            sys.stderr = None     # pythonw（定时任务）下的情形
            self.assertEqual(data.fetch(ctx, "ths", uses_tqdm), 3)
            self.assertIsNotNone(sys.stderr)
        finally:
            if sys.stderr is not old and sys.stderr is not None:
                sys.stderr.close()
            sys.stderr = old

    def test_force_falls_back_to_existing_cache(self):
        def failing(func, *a, **kw):
            raise conn_error()

        def ths_func():
            return pd.DataFrame()

        ctx = FakeCtx(self.tmp.name, fetch_impl=failing, force=True)
        self._cache(ths_func, {}, pd.DataFrame({"a": [1]}))
        out = data.fetch(ctx, "ths", ths_func)          # 强制刷新失败 -> 沿用已有缓存
        self.assertEqual(len(out), 1)
        self.assertTrue(ctx.shared["ths_down"])
        ctx.calls.clear()
        out = data.fetch(ctx, "ths", ths_func)          # 站点已标记失败：不再重试，直接用缓存
        self.assertEqual(ctx.calls, [])
        self.assertEqual(len(out), 1)

        def other():
            return None

        with self.assertRaises(data.SiteSkipped):       # 没有缓存的同站请求直接跳过
            data.fetch(ctx, "ths", other)
        self.assertEqual(ctx.calls, [])

    def test_snapshot_unavailable_does_not_flag(self):
        def snap(func, *a, **kw):
            raise SnapshotUnavailable("快照不可用")

        ctx = FakeCtx(self.tmp.name, snapshot_ok=False, fetch_impl=snap)
        with self.assertRaises(SnapshotUnavailable):
            data.fetch(ctx, "ths", lambda: None, snapshot=True)
        self.assertNotIn("ths_down", ctx.shared)

    def test_load_boards_fallback(self):
        ctx = FakeCtx(self.tmp.name)

        def raw(c, kind, src):
            if src == "em":
                raise conn_error("push2 连接被重置")
            return ths_industry_raw()

        def flow(symbol="即时"):
            return ths_flow_raw(["影视院线", "贵金属", "元件", "银行"], [7.23, 5.27, -3.63, 0.0],
                                [14.46, 26.36, -12.06, -13.79])
        flow.__name__ = "stock_fund_flow_industry"

        with mock.patch.object(data, "_raw", side_effect=raw), mock.patch.object(data, "stock_fund_flow_industry", flow):
            b = data.load_boards(ctx, "industry")
        self.assertEqual(b.src, "ths")
        self.assertEqual(len(b.df), 4)
        self.assertEqual(b.failed, ["东方财富（连接失败或超时）"])
        self.assertEqual(b.net_basis, "ths_flow")      # 行业净流入改用资金流净额，与概念同口径
        self.assertAlmostEqual(b.df.set_index("name").at["影视院线", "net"], 14.46e8)
        self.assertAlmostEqual(b.df.set_index("name").at["影视院线", "amount"], 105.94e8)

    def test_ths_industry_net_fallback_to_summary(self):
        ctx = FakeCtx(self.tmp.name)
        ind = calc.standardize(ths_industry_raw(), calc.THS_INDUSTRY_MAP, calc.THS_INDUSTRY_SCALE)

        def boom(symbol="即时"):
            raise ValueError("页面结构异常")
        boom.__name__ = "stock_fund_flow_industry"

        with mock.patch.object(data, "stock_fund_flow_industry", boom):
            df, basis = data._ths_industry_net(ctx, ind)
        self.assertEqual(basis, "ths_summary")
        self.assertAlmostEqual(df.set_index("name").at["影视院线", "net"], 16.04e8)

        def other_names(symbol="即时"):
            return ths_flow_raw(["甲", "乙", "丙", "影视院线"], [1, 2, 3, 4], [1, 2, 3, 4])
        other_names.__name__ = "stock_fund_flow_industry"
        with mock.patch.object(data, "stock_fund_flow_industry", other_names):
            _, basis = data._ths_industry_net(ctx, ind)
        self.assertEqual(basis, "ths_summary")          # 名称对不上时不混用

    def test_load_boards_prefers_cached_source(self):
        ctx = FakeCtx(self.tmp.name)
        _, func, kw = data._spec("industry", "ths")
        self._cache(func, kw, "x")
        tried = []

        def raw(c, kind, src):
            tried.append(src)
            return ths_industry_raw()

        with mock.patch.object(data, "_raw", side_effect=raw), \
                mock.patch.object(data, "_ths_industry_net", side_effect=lambda c, df: (df, "ths_flow")):
            b = data.load_boards(ctx, "industry")
        self.assertEqual(tried, ["ths"])               # 不先等东方财富失败
        self.assertEqual((b.src, b.failed), ("ths", []))
        ctx.fetcher.force = True
        tried.clear()
        with mock.patch.object(data, "_raw", side_effect=raw), \
                mock.patch.object(data, "_ths_industry_net", side_effect=lambda c, df: (df, "ths_flow")):
            data.load_boards(ctx, "industry")
        self.assertEqual(tried[0], "em")               # --force 时按正常顺序

    def test_load_boards_all_snapshot_missing(self):
        ctx = FakeCtx(self.tmp.name, snapshot_ok=False)
        with mock.patch.object(data, "_raw", side_effect=SnapshotUnavailable("x")):
            with self.assertRaises(SnapshotUnavailable):
                data.load_boards(ctx, "concept")
        with mock.patch.object(data, "_raw", side_effect=FetchError("down")):
            with self.assertRaises(FetchError) as cm:
                data.load_boards(ctx, "concept")
        self.assertNotIsInstance(cm.exception, SnapshotUnavailable)
        msg = str(cm.exception)
        self.assertIn("东方财富（返回数据异常）", msg)
        self.assertLess(len(msg), 160)                  # 不会被 Section 截断

    def test_ths_fund_flow_parsing(self):
        pages = [FakeResp(ths_flow_page([("影视院线", "7.23%", 14.46), ("贵金属", "5.27%", 26.36)], 2)),
                 FakeResp(ths_flow_page([("元件", "-3.63%", -12.06)], 2))]
        with mock.patch.object(data, "_ths_v", return_value="v"), \
                mock.patch.object(data.requests, "get", side_effect=pages) as get, \
                mock.patch.object(data.time, "sleep"):
            df = data.stock_fund_flow_industry()
        self.assertEqual(get.call_count, 2)
        self.assertIn("/funds/hyzjl/", get.call_args_list[0].args[0])
        self.assertIn("/page/2/", get.call_args_list[1].args[0])
        self.assertEqual(list(df.columns), data.THS_FLOW_COLS)
        self.assertEqual(list(df["序号"]), [1, 2, 3])
        self.assertAlmostEqual(df.loc[2, "行业-涨跌幅"], -3.63)
        self.assertAlmostEqual(df.loc[0, "领涨股-涨跌幅"], 10.01)
        self.assertAlmostEqual(df.loc[2, "净额"], -12.06)
        std = data.standardize("concept", "ths", df)   # 同结构，净额亿元 -> 元
        self.assertAlmostEqual(std.set_index("name").at["元件", "net"], -12.06e8)
        with mock.patch.object(data, "_ths_v", return_value="v"), \
                mock.patch.object(data.requests, "get", return_value=FakeResp("<html>blocked</html>")):
            with self.assertRaises(ValueError):
                data.stock_fund_flow_concept()
        with self.assertRaises(ValueError):
            data.stock_fund_flow_industry("3日排行")

    def test_industry_history_long_holiday(self):
        dates = ["2026-02-24", "2026-02-25", "2026-02-26", "2026-02-27", "2026-03-02"]
        days = ["2026-02-12", "2026-02-13"] + dates
        closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0]
        got = {}

        def impl(func, *a, **kw):
            got.update(kw)
            start = dt.datetime.strptime(kw["start_date"], "%Y%m%d").strftime("%Y-%m-%d")
            df = pd.DataFrame({"日期": days, "收盘价": [str(c) for c in closes]})
            return df[df["日期"] >= start].reset_index(drop=True)

        ctx = FakeCtx(self.tmp.name, fetch_impl=impl, trade_date=dt.date(2026, 3, 2))
        with mock.patch.object(data.tc, "prev_trade_day", return_value=dt.date(2026, 2, 13)):
            h = data.industry_history(ctx, "ths", {"name": "电池"}, dates)
        self.assertLessEqual(got["start_date"], "20260213")   # 覆盖节前最后一个交易日
        self.assertEqual(got["end_date"], "20260302")
        self.assertEqual(sorted(h), dates)
        self.assertAlmostEqual(h["2026-02-24"], (102 / 101 - 1) * 100)
        self.assertEqual(data.industry_history(ctx, "em", {"name": "x", "code": ""}, dates), {})

    def test_zt_pool_statuses(self):
        today = dt.date.today()
        recent = (today - dt.timedelta(days=3)).strftime("%Y%m%d")
        old = (today - dt.timedelta(days=60)).strftime("%Y%m%d")
        ctx = FakeCtx(self.tmp.name, fetch_impl=lambda f, *a, **kw: pd.DataFrame())
        self.assertEqual(data.zt_pool(ctx, recent).status, "no_data")          # 接口 data=null
        ctx._impl = lambda f, *a, **kw: pd.DataFrame({"代码": ["1"], "名称": ["a"]})
        self.assertEqual(data.zt_pool(ctx, recent).status, "bad_format")       # 缺“所属行业”
        ctx._impl = lambda f, *a, **kw: zt_pool_raw([])
        self.assertEqual(data.zt_pool(ctx, recent).status, "empty")
        ctx._impl = lambda f, *a, **kw: zt_pool_raw([("1", "a", "电池", 1, "0930")])
        res = data.zt_pool(ctx, recent)
        self.assertEqual((res.status, len(res.df)), ("ok", 1))

        def down(f, *a, **kw):
            raise conn_error()
        ctx2 = FakeCtx(self.tmp.name, fetch_impl=down)
        res = data.zt_pool(ctx2, recent)
        self.assertEqual((res.status, res.reason), ("failed", "连接失败或超时"))
        self.assertTrue(ctx2.shared["push2ex_down"])

        ctx.calls.clear()
        self.assertEqual(data.zt_pool(ctx, old).status, "out_of_window")
        self.assertEqual(ctx.calls, [])                                         # 超出回查范围不请求
        self._cache(data.ak.stock_zt_pool_em, {"date": old}, pd.DataFrame())
        self.assertEqual(data.zt_pool(ctx, old).status, "out_of_window")       # 以前缓存的空表也不算“为空”
        self._cache(data.ak.stock_zt_pool_em, {"date": old}, zt_pool_raw([("1", "a", "电池", 1, "0930")]))
        ctx.fetcher.force = True
        self.assertEqual(data.zt_pool(ctx, old).status, "ok")                  # 以前取到的照常用（--force 也不重取）
        self.assertEqual(ctx.calls, [])


# ---------------------------------------------------------------- 整体 build（数据层全部打桩）

def ok_pool(rows):
    return data.ZtPool(zt_pool_raw(rows), "ok")


class TestBuild(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store_path = Path(self.tmp.name) / "sector_daily.csv"
        self.store = SectorStore(store_path)
        self.patches = [mock.patch.object(sectors, "SectorStore", lambda: SectorStore(store_path))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _zt(self, ctx, date_str):
        if date_str == "20260929":
            return data.ZtPool(None, "failed", "连接失败或超时")
        return ok_pool([("1", "甲", "电池", 2, "0930"), ("2", "乙", "电池", 1, "1000"),
                        ("3", "丙", "影视院线", 1, "0931"), ("4", "丁", "汽车零部", 1, "0932"),
                        ("5", "戊", "汽车零部件", 1, "0933")])

    def _boards(self):
        ind = calc.standardize(ths_industry_raw(), calc.THS_INDUSTRY_MAP, calc.THS_INDUSTRY_SCALE)
        con = pd.DataFrame({"name": ["AI应用", "融资融券", "短剧游戏", "MLOps概念"], "pct": [5.0, 4.0, 3.0, 1.0],
                            "net": [2e9, 1e9, -1e9, 0.8e8]})
        con = calc.standardize(con, {"name": "name", "pct": "pct", "net": "net"})

        def boards(c, kind):
            if kind == "industry":
                return data.Boards(ind, "ths", ["东方财富（连接失败或超时）"], "ths_flow")
            return data.Boards(con, "ths", [], "ths_flow")
        return boards

    def test_build_full(self):
        ctx = FakeCtx(self.tmp.name)
        boards = self._boards()
        hist = {"2026-09-28": 1.0, "2026-09-29": 2.0, "2026-09-30": -1.0, "2026-10-08": 0.5}
        # 前一交易日的概念排行已在本地累积：AI应用 连续 2 日位列前 10
        self.store.upsert_day("2026-10-08", "concept", "ths",
                              pd.DataFrame({"name": ["AI应用", "固态电池"], "pct": [6.0, 1.0], "net": [1e8, 1e8]}))
        with mock.patch.object(data, "load_boards", side_effect=boards), \
                mock.patch.object(data, "zt_pool", side_effect=self._zt), \
                mock.patch.object(data, "ths_industry_names", return_value={"影视院线", "贵金属"}), \
                mock.patch.object(data, "industry_history", return_value=hist) as ih:
            sec = sectors.build(ctx)
        d = sec.to_dict()
        self.assertEqual(d["errors"], [])
        titles = [b["title"] for b in d["blocks"]]
        self.assertTrue(any(t.startswith("行业涨跌幅") for t in titles))
        self.assertTrue(any(t.startswith("概念涨幅前") for t in titles))
        self.assertIn("今日涨幅前4的行业：近5日涨跌幅", titles)
        self.assertTrue(any("涨停行业近5日" in t for t in titles))
        self.assertIn("行业资金：净流入前3/净流出前1", titles)
        self.assertIn("行业涨跌幅：前4", titles)
        self.assertIn("行业板块涨跌幅排行（共4个）", titles)    # 不写“点表头可排序”
        for t in titles:                               # 标题里数字与单位/中文之间不加空格
            self.assertNotRegex(t, r"\d [\u4e00-\u9fff]|[\u4e00-\u9fff] \d", t)
        self.assertEqual(ih.call_count, 2)  # 只补名称表里存在的行业
        self.assertEqual(d["summary"][0], "行业板块2涨1跌1平（共4个）；涨幅居前：影视院线+7.23%、贵金属+5.27%；跌幅居前：元件-3.63%。")
        # 截断的“汽车零部”与“汽车零部件”合并；并列第一时 KPI 写出全部并列行业
        self.assertIn("涨停股集中在电池（2只）、汽车零部件（2只）、影视院线（1只），涨停池共5只（不含ST股）。", d["summary"])
        self.assertIn("行业资金净流入居前：贵金属+23.02亿、影视院线+16.04亿、银行+1.50亿；净流出居前：元件-27.33亿。",
                      d["summary"])
        for line in d["summary"]:                      # 句末统一“。”，数字与单位之间不加空格
            self.assertTrue(line.endswith("。"), line)
            self.assertNotRegex(line, r"\d [%亿万只日个天]")
        kpi = {k["label"]: k for k in d["kpis"]}
        self.assertEqual(kpi["涨停最集中"]["value"], "电池、汽车零部件")
        self.assertTrue(kpi["涨停最集中"]["sub"].startswith("并列 · 各2只"))
        self.assertEqual(kpi["行业涨 / 跌"]["value"], "2 / 1")
        self.assertEqual(kpi["行业涨 / 跌"]["sub"], "平盘1 · 共4个行业")
        for k in d["kpis"]:
            self.assertNotRegex(k["sub"], r"\d [\u4e00-\u9fff]|[\u4e00-\u9fff] \d", k)
        self.assertEqual(kpi["行业净流入第一"]["value"], "贵金属")
        self.assertEqual(kpi["行业净流入第一"]["sub"], "+23.02亿")
        con_table = next(b for b in d["blocks"] if b["type"] == "table" and b["title"].startswith("概念涨幅前"))
        self.assertEqual([r[0]["text"] for r in con_table["rows"]], ["AI应用", "短剧游戏", "MLOps概念"])  # 融资融券已剔除
        self.assertEqual(con_table["columns"][2]["label"], "净流入（亿元）")
        self.assertEqual(con_table["rows"][2][2]["text"], "+0.80")      # 不再显示成“8000.00万”；正数带“+”
        self.assertEqual(con_table["rows"][2][2]["tone"], "up")
        self.assertEqual(con_table["rows"][1][2]["text"], "-10.00")
        notes = " ".join(d["notes"])
        self.assertIn("同花顺资金流向“净额”", notes)
        self.assertIn("不含ST股", notes)
        self.assertIn("与上方行业排行的数据源和分类不同", notes)
        self.assertNotIn("汽车零部→汽车零部件", notes)   # 实现细节不写进说明
        self.assertNotIn("sector_daily.csv", notes)       # 不写内部路径
        self.assertNotIn("可排序", notes)
        # 本地只有 2 个完整排行日：不写“连续位列涨幅前N”
        self.assertFalse(any("连续位列" in s for s in d["summary"]))
        self.assertTrue(any(s.startswith("持续性：") and "影视院线+9.91%" in s for s in d["summary"]))
        persist = next(b for b in d["blocks"] if b["title"].startswith("今日涨幅前4的行业：持续性"))
        self.assertIn("连涨天数", [c["label"] for c in persist["columns"]])
        spans = [b["span"] for b in d["blocks"]]
        self.assertEqual(spans.count("half") % 2, 0)
        html = render_report({"version": "t", "trade_date": "2026-10-09", "weekday": "", "generated_at": "",
                              "snapshot_ok": True, "calendar_approximate": False, "banners": [],
                              "sections": [d], "stats": {"network": 0, "cache": 0, "failed": 0}, "elapsed": 0})
        self.assertIn("板块轮动", html)
        # 第二次生成：历史已存入本地，不再补齐
        with mock.patch.object(data, "load_boards", side_effect=boards), \
                mock.patch.object(data, "zt_pool", side_effect=self._zt), \
                mock.patch.object(data, "ths_industry_names", return_value={"影视院线", "贵金属"}), \
                mock.patch.object(data, "industry_history", return_value=hist) as ih2:
            sectors.build(ctx)
        self.assertEqual(ih2.call_count, 0)

    def test_build_degraded(self):
        ctx = FakeCtx(self.tmp.name, snapshot_ok=False)
        with mock.patch.object(data, "load_boards", side_effect=SnapshotUnavailable("x")), \
                mock.patch.object(data, "zt_pool", side_effect=self._zt):
            sec = sectors.build(ctx)
        d = sec.to_dict()
        # 快照不可用是历史日期 / 盘中运行的预期降级：写进说明，不进错误框
        self.assertEqual(d["errors"], [])
        snap = [n for n in d["notes"] if "板块实时排行" in n]
        self.assertEqual(len(snap), 1)
        self.assertIn("行业、概念", snap[0])
        self.assertIn("照常显示", snap[0])
        self.assertTrue(snap[0].endswith("。"))
        titles = [b["title"] for b in d["blocks"]]
        self.assertTrue(any(t.startswith("涨停股行业分布") for t in titles))
        self.assertTrue(any("涨停行业近5日" in t for t in titles))
        self.assertTrue(d["summary"][0].startswith("涨停股集中在电池（2只）"))
        self.assertFalse(any("上方" in n for n in d["notes"]))   # 没有行业排行时不提“与上方排行不同”
        heat = next(b for b in d["blocks"] if "涨停行业近5日" in b["title"])
        self.assertIn("09-29", heat["note"])  # 失败的日期写进说明
        self.assertEqual([b["span"] for b in d["blocks"]], ["half", "half", "full"])  # 落单的半宽块改整宽

    def test_build_history_from_store(self):
        """快照不可用、没有原始缓存，但本地累积里有复盘日的完整排行：用它出简版排行和资金流。"""
        ctx = FakeCtx(self.tmp.name, snapshot_ok=False)
        ind = calc.standardize(ths_industry_raw(), calc.THS_INDUSTRY_MAP, calc.THS_INDUSTRY_SCALE)
        self.store.upsert_day("2026-10-09", "industry", "ths", ind)
        with mock.patch.object(data, "load_boards", side_effect=SnapshotUnavailable("x")), \
                mock.patch.object(data, "zt_pool", side_effect=self._zt), \
                mock.patch.object(data, "ths_industry_names", return_value=set()):
            d = sectors.build(ctx).to_dict()
        self.assertEqual(d["errors"], [])
        self.assertTrue(any(n.startswith("概念板块实时排行") for n in d["notes"]))   # 只有概念缺（写进说明）
        table = next(b for b in d["blocks"] if b["title"].startswith("行业板块涨跌幅排行"))
        self.assertIn("取自本地累积", table["title"])
        self.assertEqual([c["label"] for c in table["columns"]], ["行业", "涨跌幅", "净流入（亿元）"])
        self.assertTrue(any(t["title"].startswith("行业资金") for t in d["blocks"]))
        self.assertTrue(any("取自本地逐日记录" in n for n in d["notes"]))
        self.assertTrue(d["summary"][0].startswith("行业板块2涨1跌1平"))
        self.assertEqual(table["rows"][0][2]["text"][0], "+")          # 净流入（亿元）正数带“+”

    def test_build_zt_out_of_window(self):
        ctx = FakeCtx(self.tmp.name, snapshot_ok=False, trade_date=dt.date(2026, 6, 1))
        with mock.patch.object(data, "load_boards", side_effect=SnapshotUnavailable("x")), \
                mock.patch.object(data, "zt_pool", return_value=data.ZtPool(None, "out_of_window")):
            d = sectors.build(ctx).to_dict()
        text = " ".join(d["notes"] + d["errors"])
        self.assertIn("超过约30个自然日", text)
        self.assertNotIn("为空", text)
        self.assertNotIn("不受影响", text)
        self.assertNotIn("照常显示", text)
        self.assertEqual(d["blocks"], [])
        self.assertEqual(d["kpis"], [])

    def test_build_zt_bad_format_and_no_data(self):
        ctx = FakeCtx(self.tmp.name, snapshot_ok=False)
        for status, word in (("bad_format", "所属行业"), ("no_data", "没有返回")):
            with mock.patch.object(data, "load_boards", side_effect=SnapshotUnavailable("x")), \
                    mock.patch.object(data, "zt_pool", return_value=data.ZtPool(None, status)):
                d = sectors.build(ctx).to_dict()
            self.assertTrue(any(e.startswith("涨停股行业分布：") and word in e for e in d["errors"]), status)
            self.assertTrue(any("涨停行业近5日热度" in n for n in d["notes"]), status)


if __name__ == "__main__":
    unittest.main()
