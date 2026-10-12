"""「龙虎榜与资金」模块的单元测试（合成数据，不联网）。

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

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kmcf.context import Context  # noqa: E402
from kmcf.fetch import FetchError, SnapshotUnavailable  # noqa: E402
from kmcf.section import DOWN, UP  # noqa: E402
from kmcf.sections import _funds_calc as C  # noqa: E402
from kmcf.sections import _funds_data as D  # noqa: E402
from kmcf.sections import funds  # noqa: E402

D0 = dt.date(2026, 10, 9)


def conn_err(msg="连接被重置"):
    """模拟 Fetcher 重试后抛出的连接级失败（FetchError，原因是 requests.ConnectionError）。"""
    e = FetchError(f"获取失败（已重试 3 次）：{msg}")
    e.__cause__ = requests.ConnectionError(msg)
    return e


def quote_text(rows, day="2026-10-09"):
    """合成 hq.sinajs.cn 返回：rows = [(symbol, name, prev_close, price, amount)]。"""
    out = []
    for sym, name, prev, price, amount in rows:
        f = ([name, str(prev), str(prev), str(price), "0", "0", "0", "0", "100", str(amount)]
             + ["0"] * 20 + [day, "15:00:00", "00"])
        out.append(f'var hq_str_{sym}="{",".join(f)}";')
    return "\n".join(out)


def lhb_row(code, name, net, reason, deal=None, explain="1家机构买入，成功率40.00%", pct=5.0):
    deal = abs(net) * 3 if deal is None else deal
    return {"SECURITY_CODE": code, "SECURITY_NAME_ABBR": name, "TRADE_DATE": "2026-10-09 00:00:00",
            "EXPLAIN": explain, "CLOSE_PRICE": 10.0, "CHANGE_RATE": pct, "BILLBOARD_NET_AMT": net,
            "BILLBOARD_BUY_AMT": (deal + net) / 2, "BILLBOARD_SELL_AMT": (deal - net) / 2,
            "BILLBOARD_DEAL_AMT": deal, "ACCUM_AMOUNT": deal * 4, "DEAL_NET_RATIO": net / (deal * 4) * 100,
            "DEAL_AMOUNT_RATIO": 25.0, "TURNOVERRATE": 12.3, "FREE_MARKET_CAP": 5e9, "EXPLANATION": reason,
            "SECURITY_TYPE_CODE": "058001001"}


def org_row(code, name, net, reason="日涨幅偏离值达到7%的前5只证券", buy_n=2, sell_n=1):
    buy = max(net, 0) + 1e7
    return {"SECUCODE": f"{code}.SZ", "SECURITY_NAME_ABBR": name, "SECURITY_CODE": code,
            "TRADE_DATE": "2026-10-09 00:00:00", "CLOSE_PRICE": 10.0, "CHANGE_RATE": 3.0,
            "BUY_TIMES": buy_n, "SELL_TIMES": sell_n, "BUY_AMT": buy, "SELL_AMT": buy - net,
            "NET_BUY_AMT": net, "ACCUM_AMOUNT": 1e9, "RATIO": net / 1e9 * 100, "TURNOVERRATE": 8.0,
            "FREECAP": 100.0, "EXPLANATION": reason}


def branch_row(name, net, buy, n_buy=3, n_sell=1, stocks="甲 乙 丙", abbr=None):
    return {"OPERATEDEPT_NAME": name, "ONLIST_DATE": "2026-10-09 00:00:00", "BUYER_APPEAR_NUM": n_buy,
            "SELLER_APPEAR_NUM": n_sell, "TOTAL_BUYAMT": buy, "TOTAL_SELLAMT": buy - net, "TOTAL_NETAMT": net,
            "BUY_STOCK": "", "OPERATEDEPT_CODE": "1", "SECURITY_NAME_ABBR": stocks,
            "OPERATEDEPT_CODE_OLD": "8", "ORG_NAME_ABBR": abbr if abbr is not None else name}


def mff_frame(days, mains):
    return pd.DataFrame({
        "日期": days, "上证-收盘价": [3800 + i for i in range(len(days))], "上证-涨跌幅": [0.1] * len(days),
        "深证-收盘价": [12000.0] * len(days), "深证-涨跌幅": [0.2] * len(days),
        "主力净流入-净额": mains, "主力净流入-净占比": [-0.9] * len(days),
        "超大单净流入-净额": [m * 0.5 for m in mains], "超大单净流入-净占比": [0.0] * len(days),
        "大单净流入-净额": [m * 0.5 for m in mains], "大单净流入-净占比": [0.0] * len(days),
        "中单净流入-净额": [-m * 0.3 for m in mains], "中单净流入-净占比": [0.0] * len(days),
        "小单净流入-净额": [-m * 0.7 for m in mains], "小单净流入-净占比": [0.0] * len(days),
    })


class TestHelpers(unittest.TestCase):
    def test_short_reason(self):
        cases = {
            "日涨幅偏离值达到7%的前5只证券": "日涨幅偏离7%",
            "有价格涨跌幅限制的日收盘价格涨幅偏离值达到7%的前五只证券": "日涨幅偏离7%",
            "连续三个交易日内，涨幅偏离值累计达到20%的证券": "3日涨幅偏离20%",
            "非S证券连续三个交易日内收盘价格跌幅偏离值累计达到20%的证券": "3日跌幅偏离20%",
            "日振幅值达到15%的前5只证券": "日振幅15%",
            "有价格涨跌幅限制的日价格振幅达到30%的前五只证券": "日振幅30%",
            "当日换手率达到20%的前5只股票": "日换手率20%",
            "当日收盘价涨幅达到20%的前5只股票": "日涨幅20%",
            "连续10个交易日内4次出现同正向异常波动的证券": "10日内4次同正向异常波动",
            "退市整理期": "退市整理期",
            # “跌幅”已表明方向，去掉重复的负号
            "当日收盘价跌幅达到-20%的前5只股票": "日跌幅20%",
            "有价格涨跌幅限制的日收盘价格跌幅偏离值达到-7%的前五只证券": "日跌幅偏离7%",
            "有价格涨跌幅限制的日收盘价格跌幅偏离值达到7%的前五只证券": "日跌幅偏离7%",
            # “+40%(-40%)” -> “±40%”
            "北交所股票连续3个交易日内日收盘价涨跌幅偏离值累计达到+40%(-40%)": "北交所3日涨跌幅偏离±40%",
            "北交所股票最近3个有成交的交易日以内收盘价涨跌幅偏离值累计达到+40%(-40%)": "北交所3日涨跌幅偏离±40%",
            "非ST、*ST和S证券连续三个交易日内收盘价格涨幅偏离值累计达到20%的证券": "3日涨幅偏离20%",
            "ST、*ST证券连续三个交易日内收盘价格涨幅偏离值累计达到12%的证券": "ST股3日涨幅偏离12%",
            "连续三个交易日内，涨幅偏离值累计达到12%的ST证券、*ST证券和未完成股改证券": "ST股3日涨幅偏离12%",
            "严重异常期间日收盘价格涨幅偏离值累计达到100%的证券": "严重异常期间日涨幅偏离100%",
        }
        for raw, want in cases.items():
            self.assertEqual(C.short_reason(raw), want, raw)

    def test_multi_day_and_explain(self):
        self.assertFalse(C.is_multi_day("日换手率达到20%的前5只证券"))
        self.assertTrue(C.is_multi_day("连续三个交易日内，涨幅偏离值累计达到20%的证券"))
        self.assertTrue(C.is_multi_day("连续10个交易日内4次出现同正向异常波动的证券"))
        self.assertEqual(C.clean_explain("3家机构买入，成功率26.20%"), "3家机构买入")
        self.assertEqual(C.clean_explain("主力做T，成功率33.07%"), "主力做T")
        self.assertEqual(C.clean_explain(None), "")

    def test_norm_code_and_streak(self):
        self.assertEqual(C.norm_code(420), "000420")
        self.assertEqual(C.norm_code("sz002487"), "002487")
        self.assertEqual(C.norm_code("600825.SH"), "600825")
        self.assertEqual(C.streak([1, -2, -3, -4]), -3)
        self.assertEqual(C.streak([-1, 2, 3]), 2)
        self.assertEqual(C.streak([1, 2, 0]), 0)
        self.assertEqual(C.streak([]), 0)
        self.assertEqual(C.streak([None, 5]), 1)

    def test_name_list(self):
        self.assertEqual(C.name_list(["甲", "乙"]), "甲、乙")
        self.assertEqual(C.name_list(list("abcdefgh"), limit=3), "a、b、c等8只")


class TestLhb(unittest.TestCase):
    def setUp(self):
        self.raw = pd.DataFrame([
            lhb_row("300981", "中红医疗", 1.26e8, "日涨幅达到15%的前5只证券", deal=2.8e8),
            lhb_row("300981", "中红医疗", 0.79e8, "连续三个交易日内，涨幅偏离值累计达到30%的证券", deal=5.8e8),
            lhb_row("000678", "襄阳轴承", -1.8e7, "日换手率达到20%的前5只证券", deal=3.7e8),
            lhb_row("000678", "襄阳轴承", -1.8e7, "日振幅值达到15%的前5只证券", deal=3.7e8),
            lhb_row("600825", "新华传媒", 0.0, "连续10个交易日内4次出现同正向异常波动的证券", deal=11e8),
            lhb_row("600825", "新华传媒", 4.9e7, "非S证券连续三个交易日内收盘价格涨幅偏离值累计达到20%的证券", deal=1.7e8),
            lhb_row(420, "吉林化纤", 1.78e8, "日涨幅偏离值达到7%的前5只证券"),  # 代码被解析成整数
            lhb_row("002636", "金安国纪", -2.0e8, "日跌幅偏离值达到7%的前5只证券", pct=-9.9),
        ])

    def test_merge_one_row_per_stock(self):
        m = C.merge_lhb(self.raw)
        self.assertEqual(len(m), 5)
        self.assertEqual(m["code"].tolist()[0], "000420")          # 按净买额降序，代码补齐 6 位
        self.assertEqual(m["net"].tolist(), sorted(m["net"].tolist(), reverse=True))
        zh = m.set_index("code").loc["300981"]
        self.assertAlmostEqual(zh["net"], 1.26e8)                   # 优先当日口径
        self.assertEqual(zh["basis"], "当日")
        self.assertEqual(zh["reasons"], "日涨幅15%；3日涨幅偏离30%")
        self.assertEqual(zh["explain"], "1家机构买入")
        xh = m.set_index("code").loc["600825"]
        self.assertAlmostEqual(xh["net"], 4.9e7)                    # 都是多日：3日累计优先于 10 日异常波动
        self.assertEqual(xh["basis"], "3日累计")
        self.assertTrue(xh["multi_day"])
        xy = m.set_index("code").loc["000678"]
        self.assertEqual(xy["reasons"], "日换手率20%；日振幅15%")

    def test_stats_only_count_day_basis(self):
        s = C.lhb_stats(C.merge_lhb(self.raw))
        self.assertEqual(s["count"], 5)                             # 上榜家数含多日累计
        self.assertEqual((s["day_n"], s["md_n"]), (4, 1))
        self.assertEqual((s["buy_n"], s["sell_n"]), (2, 2))          # 新华传媒（3日累计）不计入
        self.assertEqual(s["top_buy"]["name"], "吉林化纤")
        self.assertEqual(s["top_sell"]["name"], "金安国纪")
        self.assertAlmostEqual(s["net_sum"], 1.78e8 + 1.26e8 - 1.8e7 - 2.0e8)
        self.assertAlmostEqual(s["md_net"], 4.9e7)

    def test_multi_day_top_not_used(self):
        """净买入最多的是 3 日累计口径时，要点里的“净买入最多”只看当日口径。"""
        raw = pd.DataFrame([lhb_row("002733", "雄韬股份", 9e8, "连续三个交易日内，涨幅偏离值累计达到20%的证券"),
                            lhb_row("300821", "东岳硅材", 1.8e8, "日涨幅偏离值达到7%的前5只证券")])
        s = C.lhb_stats(C.merge_lhb(raw))
        self.assertEqual(s["top_buy"]["name"], "东岳硅材")
        self.assertAlmostEqual(s["net_sum"], 1.8e8)

    def test_no_seat_record(self):
        """“连续10个交易日”类记录：买入 = 卖出 = 累计成交额、净额 0，没有席位数据，金额置空。"""
        row = lhb_row("600825", "新华传媒", 0.0, "连续10个交易日内4次出现同正向异常波动的证券", deal=11.26e8)
        row.update(BILLBOARD_BUY_AMT=1126173900.0, BILLBOARD_SELL_AMT=1126173800.0, BILLBOARD_DEAL_AMT=1126173900.0,
                   ACCUM_AMOUNT=1126173900, DEAL_NET_RATIO=0.000009)
        m = C.merge_lhb(pd.DataFrame([row]))
        r = m.iloc[0]
        self.assertTrue(pd.isna(r["net"]) and pd.isna(r["buy"]) and pd.isna(r["sell"]) and pd.isna(r["net_ratio"]))
        self.assertEqual(r["basis"], "多日累计（无席位明细）")
        s = C.lhb_stats(m)
        self.assertEqual((s["count"], s["day_n"], s["md_n"]), (1, 0, 1))
        self.assertEqual(s["net_sum"], 0.0)
        self.assertIsNone(s["md_net"])
        # 同时有当日记录时，取当日记录
        m2 = C.merge_lhb(pd.DataFrame([row, lhb_row("600825", "新华传媒", 3e7, "日涨幅偏离值达到7%的前5只证券")]))
        self.assertAlmostEqual(m2.iloc[0]["net"], 3e7)
        self.assertEqual(m2.iloc[0]["basis"], "当日")

    def test_excludes_bonds_and_b_shares(self):
        raw = pd.concat([self.raw, pd.DataFrame([
            lhb_row("127080", "声迅转债", 7.6e6, "日涨幅达到15%的前5只可转债"),
            lhb_row("900937", "华电B股", -1.2e5, "有价格涨跌幅限制的日收盘价格跌幅偏离值达到7%的前五只证券"),
            lhb_row("920190", "雷神科技", 5e6, "北交所股票最近3个有成交的交易日以内收盘价涨跌幅偏离值累计达到+40%(-40%)")])])
        m = C.merge_lhb(raw).set_index("code")
        self.assertNotIn("127080", m.index)
        self.assertNotIn("900937", m.index)
        self.assertEqual(m.loc["920190", "basis"], "3日累计")
        self.assertEqual(m.loc["920190", "reasons"], "北交所3日涨跌幅偏离±40%")

    def test_missing_column_raises(self):
        with self.assertRaises(ValueError) as cm:
            C.merge_lhb(self.raw.rename(columns={"BILLBOARD_NET_AMT": "NET_AMT"}))
        self.assertIn("缺少 BILLBOARD_NET_AMT", str(cm.exception))

    def test_missing_name_falls_back_to_code(self):
        raw = self.raw.copy()
        raw["SECURITY_NAME_ABBR"] = None
        m = C.merge_lhb(raw)
        self.assertNotIn("nan", m["name"].tolist())
        self.assertEqual(m.set_index("code").loc["300981", "name"], "300981")

    def test_empty(self):
        self.assertTrue(C.merge_lhb(pd.DataFrame()).empty)
        s = C.lhb_stats(C.merge_lhb(None))
        self.assertEqual(s["count"], 0)
        self.assertIsNone(s["top_buy"])


class TestOrgBranch(unittest.TestCase):
    def test_org_dedupe_and_sort(self):
        raw = pd.DataFrame([
            org_row("002487", "大金重工", 3.4e7, "日振幅值达到15%的前5只证券", 1, 1),
            org_row("002487", "大金重工", 3.4e7, "日跌幅偏离值达到7%的前5只证券", 1, 1),
            org_row("600663", "陆家嘴", -1.8e8, buy_n=0, sell_n=5),
            org_row("600825", "新华传媒", 1.1e8, buy_n=2, sell_n=2),
        ])
        org = C.normalize_org(raw)
        self.assertEqual(org["name"].tolist(), ["新华传媒", "大金重工", "陆家嘴"])
        self.assertEqual(org.iloc[2]["seats"], "买0 / 卖5")
        self.assertIn("日振幅15%", org.iloc[1]["reasons"])
        self.assertTrue(C.normalize_org(pd.DataFrame()).empty)
        with self.assertRaises(ValueError):
            C.normalize_org(raw.rename(columns={"NET_BUY_AMT": "NETAMT"}))

    def test_org_multi_day_excluded_from_stats(self):
        """只有“连续10个交易日”记录的机构数据（多日累计）不进当日合计、净买/净卖最多。"""
        raw = pd.DataFrame([
            org_row("688137", "近岸蛋白", -2.23e8, "有价格涨跌幅限制的连续10个交易日内收盘价格涨幅偏离值累计达到100%的证券"),
            org_row("002827", "高争民爆", 3.2e7, "连续三个交易日内，跌幅偏离值累计达到20%的证券"),
            org_row("301218", "华是科技", 1.46e8),
            org_row("301520", "万邦医药", -3.7e7),
        ])
        org = C.normalize_org(raw)
        self.assertEqual(org.set_index("code").loc["688137", "basis"], "多日累计")
        self.assertEqual(org.set_index("code").loc["002827", "basis"], "3日累计")
        s = C.flow_stats(org)
        self.assertAlmostEqual(s["net_sum"], 1.46e8 - 3.7e7)
        self.assertEqual(s["top_sell"]["name"], "万邦医药")
        self.assertEqual(s["top_buy"]["name"], "华是科技")
        self.assertEqual((s["md_n"], s["day_n"]), (2, 2))
        self.assertAlmostEqual(s["md_net"], -2.23e8 + 3.2e7)

    def test_branch_name(self):
        """简称不以“营业部/分公司/总部/专用”结尾（看不出是哪个席位）时，用全称并去掉公司后缀。"""
        cases = [
            ("国泰海通", "国泰海通证券股份有限公司总部", "国泰海通证券总部"),
            ("华泰证券营业总部(非营业场所)", "华泰证券股份有限公司总部", "华泰证券总部"),
            ("东方证券东方资财", "东方证券股份有限公司东方资财", "东方证券东方资财"),
            ("中信建投", "中信建投证券有限责任公司北京分公司", "中信建投证券北京分公司"),
            ("开源证券西安西大街证券营业部", "开源证券股份有限公司西安西大街证券营业部", "开源证券西安西大街证券营业部"),
            ("中信证券上海分公司", "中信证券股份有限公司上海分公司", "中信证券上海分公司"),
            ("深股通专用", "深股通专用", "深股通专用"),
            ("", "机构专用", "机构专用"),
            ("某证券", "", "某证券"),
        ]
        for short, full, want in cases:
            self.assertEqual(C.branch_name(short, full), want, (short, full))
        raw = pd.DataFrame([branch_row("国泰海通证券股份有限公司总部", -3e7, 2e8, abbr="国泰海通")])
        br = C.normalize_branch(raw)
        self.assertEqual(br.iloc[0]["name"], "国泰海通证券总部")
        self.assertEqual(br.iloc[0]["kind"], "营业部")

    def test_branch(self):
        raw = pd.DataFrame([
            branch_row("开源证券股份有限公司西安西大街证券营业部", 5.2e8, 7.8e8, 12, 7,
                       "甲 乙 丙 丁 戊 己 庚", abbr="开源证券西安西大街证券营业部"),
            branch_row("深股通专用", 2.2e8, 16e8, 19, 20),
            branch_row("沪股通专用", -1.1e8, 5e8),
            branch_row("机构专用", 1e8, 3e8),
            branch_row("国泰海通证券股份有限公司总部", -3e7, 2e8, abbr=""),
        ])
        br = C.normalize_branch(raw)
        self.assertEqual(br.set_index("full_name").loc["深股通专用", "kind"], "北向")
        self.assertEqual(br.set_index("full_name").loc["机构专用", "kind"], "机构")
        top = br.iloc[0]
        self.assertEqual(top["name"], "开源证券西安西大街证券营业部")
        self.assertEqual(top["stock_n"], 7)
        self.assertEqual(top["stocks"], "甲、乙、丙、丁、戊、己等7只")
        self.assertAlmostEqual(top["total"], 7.8e8 + 2.6e8)
        # 简称为空时回退到全称（去掉“股份有限公司”）
        self.assertIn("国泰海通证券总部", br["name"].tolist())
        # 简称、个股为真正的空值（pandas 3 的 astype(str) 会保留 NaN）
        raw2 = raw.assign(ORG_NAME_ABBR=None, SECURITY_NAME_ABBR=None)
        br2 = C.normalize_branch(raw2)
        self.assertNotIn("nan", " ".join(br2["name"]))
        self.assertEqual(br2.iloc[0]["name"], "开源证券西安西大街证券营业部")
        self.assertEqual(br2.iloc[0]["full_name"], "开源证券股份有限公司西安西大街证券营业部")
        self.assertEqual(br2.iloc[0]["stock_n"], 0)
        with self.assertRaises(ValueError):
            C.normalize_branch(raw.drop(columns=["TOTAL_NETAMT"]))
        act = C.top_active_branches(br, 10)
        self.assertEqual(set(act["kind"]), {"营业部"})
        self.assertEqual(len(act), 2)
        self.assertEqual(len(C.top_active_branches(br, 1)), 1)


class TestMarketFlow(unittest.TestCase):
    def test_series_and_day(self):
        days = [dt.date(2026, 9, 28), dt.date(2026, 9, 29), dt.date(2026, 9, 30), dt.date(2026, 10, 8),
                D0, dt.date(2026, 10, 12)]
        mains = [3e9, 1e9, -2e9, -4e9, -1.7e10, 9e9]
        s = C.market_flow_series(mff_frame(days, mains), D0, 60)
        self.assertEqual(s["date"].iloc[-1], D0)          # 复盘日之后的数据被截掉
        self.assertEqual(len(s), 5)
        day = C.market_flow_day(s, D0)
        self.assertAlmostEqual(day["main"], -1.7e10)
        self.assertEqual(day["streak"], -3)
        self.assertAlmostEqual(day["sum5"], sum(mains[:5]))
        self.assertAlmostEqual(day["mid"] + day["small"], 1.7e10)
        # 复盘日没有数据
        self.assertIsNone(C.market_flow_day(C.market_flow_series(mff_frame(days[:4], mains[:4]), D0), D0))
        self.assertTrue(C.market_flow_series(pd.DataFrame(), D0).empty)
        # 列名变化 -> 明确报错；复盘日主力值为空 -> main 为 None
        with self.assertRaises(ValueError):
            C.market_flow_series(mff_frame(days, mains).rename(columns={"主力净流入-净额": "主力净流入净额"}), D0)
        nan_day = C.market_flow_day(C.market_flow_series(mff_frame(days, mains[:4] + [float("nan"), 1.0]), D0), D0)
        self.assertIsNone(nan_day["main"])

    def test_flow_option_is_json_and_colored(self):
        days = [dt.date(2026, 10, 8), D0]
        s = C.market_flow_series(mff_frame(days, [2e9, -3e9]), D0)
        opt = C.flow_option(s)
        json.dumps(opt)
        bars = opt["series"][0]["data"]
        self.assertEqual([b["value"] for b in bars], [20.0, -30.0])
        self.assertEqual([b["itemStyle"]["color"] for b in bars], [UP, DOWN])


class TestRank(unittest.TestCase):
    def test_sina(self):
        data = [
            {"symbol": "sh511260", "name": "国债ETF", "trade": "134.8", "changeratio": "0.0007", "amount": "4.5e9",
             "r0_net": "2.6e9", "r0_ratio": "0.58"},
            {"symbol": "sz300750", "name": "宁德时代", "trade": "297.76", "changeratio": "0.0383958",
             "amount": "15891923846", "r0_net": "1958163442.72", "r0_ratio": "0.123218"},
            {"symbol": "sh600418", "name": "江淮汽车", "trade": "40", "changeratio": "-0.0797", "amount": "6.3e9",
             "r0_net": "-1.62e9", "r0_ratio": "-0.2559"},
            {"symbol": "bj920627", "name": "力王股份", "trade": "8", "changeratio": "0.2", "amount": "3e8",
             "r0_net": "1e7", "r0_ratio": "0.03"},
        ]
        r = C.normalize_sina_rank(data)
        self.assertEqual(r["code"].tolist(), ["300750", "600418", "920627"])   # ETF 被过滤
        self.assertAlmostEqual(r.iloc[0]["pct"], 3.83958)
        self.assertAlmostEqual(r.iloc[0]["ratio"], 12.3218)
        self.assertEqual(C.split_rank(r, 5, "in")["name"].tolist(), ["宁德时代", "力王股份"])
        self.assertEqual(C.split_rank(r, 5, "out")["name"].tolist(), ["江淮汽车"])
        self.assertIsNone(C.sina_market_total(data))                          # 不像全市场
        big = [{"symbol": f"sz{300000 + i}", "name": f"S{i}", "trade": "10", "changeratio": "0.01",
                "amount": "1e8", "r0_net": "1e6" if i % 3 else "-2e6", "r0_ratio": "0.01"} for i in range(1200)]
        t = C.sina_market_total(big)
        self.assertEqual(t["count"], 1200)
        self.assertEqual((t["n_in"], t["n_out"]), (800, 400))
        self.assertAlmostEqual(t["main"], 800 * 1e6 - 400 * 2e6)

    def test_sina_filters_stale_and_untraded(self):
        """停牌/退市（成交额 0）、北交所旧代码（bj4/bj8，数据过期）不算；r0_in = r0_out = 0 的不计入主力家数。"""
        def row(sym, amount, r0_in, r0_out, name="X"):
            return {"symbol": sym, "name": name, "trade": "10", "changeratio": "0.01", "amount": str(amount),
                    "r0_in": str(r0_in), "r0_out": str(r0_out), "r0_net": str(r0_in - r0_out), "r0_ratio": "0.01"}
        data = ([row(f"sz{300000 + i}", 1e8, 2e6, 1e6) for i in range(1000)]
                + [row(f"sh{600000 + i}", 1e8, 0, 0) for i in range(100)]            # 有成交、无主力数据
                + [row(f"sz{1000 + i:06d}", 0, 0, 0, "某某退") for i in range(50)]    # 成交额 0
                + [row(f"bj{833000 + i}", 3e8, 5e6, 1e6, "N旧码") for i in range(30)])  # 北交所旧代码
        r = C.normalize_sina_rank(data)
        self.assertEqual(len(r), 1100)
        self.assertFalse(r["code"].str.startswith("833").any())
        t = C.sina_market_total(data)
        self.assertEqual((t["count"], t["covered"], t["no_main"]), (1100, 1000, 100))
        self.assertEqual((t["n_in"], t["n_out"]), (1000, 0))
        with self.assertRaises(ValueError):
            C.normalize_sina_rank([{k: v for k, v in d.items() if k != "amount"} for d in data[:3]])

    def test_close_quotes(self):
        text = quote_text([("sz300489", "光智科技", 263.0, 278.82, 4506745364.25),
                           ("bj920627", "力王股份", 17.83, 22.13, 357229397.02),
                           ("sh600519", "贵州茅台", 1255.79, 1263.0, 4453223189.0)])
        text += '\nvar hq_str_sh600000="";'
        q = C.parse_sina_quotes(text)
        self.assertEqual(q["code"].tolist(), ["300489", "920627", "600519"])
        self.assertEqual(q.iloc[0]["date"], D0)
        rank = pd.DataFrame({"code": ["300489", "920627", "000001"], "name": ["光智科技", "力王股份", "平安银行"],
                             "price": [276.0, 22.36, 11.6], "pct": [4.94, 25.4, -1.0], "net": [3.4e8, 1e7, -1e8],
                             "ratio": [7.7, 2.0, -5.0], "amount": [4.47e9, 3.5e8, 1.2e9]})
        out = C.apply_quotes(rank, q, D0)
        self.assertAlmostEqual(out.iloc[0]["pct"], (278.82 / 263 - 1) * 100, places=3)
        self.assertAlmostEqual(out.iloc[0]["amount"], 4506745364.25)
        self.assertAlmostEqual(out.iloc[1]["pct"], 24.1167, places=3)
        self.assertTrue(pd.isna(out.iloc[2]["pct"]))                      # 没有行情的不沿用旧值
        self.assertAlmostEqual(out.iloc[0]["ratio"], 7.7)                  # 资金流占比保持数据源口径
        stale = C.apply_quotes(rank, C.parse_sina_quotes(quote_text([("sz300489", "光智科技", 1, 2, 3)], "2026-10-08")),
                               D0)
        self.assertTrue(stale["pct"].isna().all())                        # 行情日期不是复盘日
        self.assertEqual(C.sina_symbol("920627"), "bj920627")
        self.assertEqual(C.sina_symbol(2), "sz000002")
        self.assertEqual(C.sina_symbol("688244"), "sh688244")

    def test_ths(self):
        df = pd.DataFrame({"序号": [1, 2, 3], "股票代码": [300530, 2912, 600354],
                           "股票简称": ["领湃科技", "中新赛克", "敦煌种业"], "最新价": [28.9, 28.55, 9.99],
                           "涨跌幅": ["20.02%", "10.02%", "10.02%"], "换手率": ["15.04%", "15.62%", "23.67%"],
                           "流入资金(元)": ["3.72亿", "3.12亿", "6.71亿"], "流出资金(元)": ["3.51亿", "3.59亿", "5.28亿"],
                           "净额(元)": ["2098.28万", "-4715.59万", "1.43亿"], "成交额(元)": ["7.23亿", "6.72亿", "11.99亿"]})
        r = C.normalize_ths_rank(df)
        self.assertEqual(r["code"].tolist(), ["300530", "002912", "600354"])
        self.assertAlmostEqual(r.iloc[1]["net"], -4715.59e4)
        self.assertAlmostEqual(r.iloc[2]["amount"], 11.99e8)
        self.assertAlmostEqual(r.iloc[0]["pct"], 20.02)
        self.assertAlmostEqual(r.iloc[2]["ratio"], round(1.43 / 11.99 * 100, 2))
        self.assertEqual(C.split_rank(r, 1, "in")["name"].tolist(), ["敦煌种业"])
        with self.assertRaises(ValueError):
            C.normalize_ths_rank(pd.DataFrame({"x": [1]}))

    def test_em(self):
        ts = dt.datetime(2026, 10, 9, 15, 0).timestamp()
        df = pd.DataFrame({"code": ["300750", "000001", "600000"], "name": ["宁德时代", "平安银行", "停牌股"],
                           "price": [297.76, 11.6, "-"], "pct": [3.84, -1.4, "-"], "amount": [1.5e10, 6e9, "-"],
                           "main_net": [1.9e9, -1.2e8, "-"], "main_ratio": [12.3, -2.0, "-"],
                           "ts": [ts, ts, ts - 86400 * 30]})
        r = C.normalize_em_rank(df)
        self.assertEqual(len(r), 2)                       # 停牌（"-"）被去掉
        self.assertEqual(C.em_rank_date(df), D0)


class TestNorthboundMargin(unittest.TestCase):
    def test_northbound(self):
        raw = pd.DataFrame({"TRADE_DATE": ["2026-10-12 00:00:00", "2026-10-09 00:00:00", "2026-10-08 00:00:00"],
                            "DEAL_AMT": [1.0, 296735.35, 277155.11], "DEAL_NUM": [1, 15745268, 14029178],
                            "NET_DEAL_AMT": [None, None, None]})
        s = C.northbound_series(raw, D0, 20)
        self.assertEqual(s["date"].tolist(), [dt.date(2026, 10, 8), D0])
        self.assertAlmostEqual(s["amount"].iloc[-1], 296735.35e6)      # 百万元 -> 元
        opt = C.northbound_option(s)
        json.dumps(opt)
        self.assertNotIn("scale", opt["yAxis"])                         # 柱状图从 0 开始
        self.assertEqual(opt["yAxis"]["min"], 0)
        with self.assertRaises(ValueError):
            C.northbound_series(raw.drop(columns=["DEAL_AMT"]), D0)

    def test_margin_daily_and_total(self):
        daily = pd.DataFrame({"STATISTICS_DATE": ["2026-10-09 00:00:00", "2026-10-08 00:00:00"],
                              "FIN_BALANCE": [25300.46, 25445.87], "LOAN_BALANCE": [296.1, 295.7],
                              "MARGIN_BALANCE": [25596.6, 25741.6], "FIN_BUY_AMT": [1504.15, 1391.25],
                              "TRADE_AMT_RATIO": [7.9, 8.28]})
        s = C.margin_series(daily, "daily", D0)
        self.assertEqual(s["date"].tolist(), [dt.date(2026, 10, 8), D0])
        self.assertAlmostEqual(s["fin"].iloc[-1], 25300.46e8)          # 亿元 -> 元
        total = pd.DataFrame({"DIM_DATE": ["2026-10-08 00:00:00"], "RZYE": [2544596119081],
                              "RZMRE": [139125330065], "RQYE": [29563438604], "RZRQYE": [2574159557685]})
        t = C.margin_series(total, "total", D0)
        self.assertAlmostEqual(t["margin"].iloc[0], 2574159557685)
        opt = C.margin_option(s)
        json.dumps(opt)
        self.assertEqual(opt["series"][1]["data"][1]["itemStyle"]["color"], DOWN)   # 融资余额减少 -> 绿

    def test_hbar(self):
        opt = C.hbar_option(["甲", "乙"], [-1.2, -0.5], label_pos="left")
        json.dumps(opt)
        self.assertFalse(opt["yAxis"]["axisLine"]["onZero"])
        self.assertEqual(opt["series"][0]["data"][0]["itemStyle"]["color"], DOWN)


# ---------------------------------------------------------------- build() 的集成测试（假的 Fetcher）

class FakeHistory:
    def __init__(self):
        self.rows = {}

    def upsert(self, date_iso, metrics):
        self.rows.setdefault(date_iso, {}).update(metrics)


class FakeFetcher:
    """按函数名返回合成数据；值为异常实例时抛出。snapshot_ok=False 时快照接口抛 SnapshotUnavailable。"""

    def __init__(self, raw_dir, data: dict, snapshot_ok=True):
        self.raw_dir = Path(raw_dir)
        self.data = data
        self.snapshot_ok = snapshot_ok
        self.calls = []

    def call(self, func, *args, snapshot=False, volatile=False, **kwargs):
        name = getattr(func, "__name__", "call")
        self.calls.append(name)
        if snapshot and not self.snapshot_ok:
            raise SnapshotUnavailable(f"{name} 快照不可用")
        v = self.data.get(name, FetchError(f"{name} 无测试数据"))
        if isinstance(v, Exception):
            raise v
        return v


def make_ctx(tmp, data, trade_date=D0, snapshot_ok=True):
    return Context(trade_date=trade_date, cfg={"top_n": 5}, fetcher=FakeFetcher(tmp, data, snapshot_ok),
                   history=FakeHistory(), snapshot_ok=snapshot_ok, log=logging.getLogger("kmcf.test"))


DC_FUNCS = ("em_lhb_detail", "em_lhb_org", "em_lhb_branch", "em_northbound_hist", "em_margin_daily", "em_margin_total")
AFTER_CLOSE = dt.datetime(2026, 10, 12, 0, 30)   # 复盘日 D0 之后（历史日期）


def kpi(d, label):
    return next((k for k in d["kpis"] if k["label"] == label), None)


def block(d, prefix):
    return next((b for b in d["blocks"] if b["title"].startswith(prefix)), None)


class TestBuild(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        days = [dt.date(2026, 9, 29), dt.date(2026, 9, 30), dt.date(2026, 10, 8), D0]
        self.days = days
        self.full = {
            "stock_market_fund_flow": mff_frame(days, [1e9, -2e9, -4e9, -1.7e10]),
            "em_lhb_detail": pd.DataFrame([
                lhb_row("000420", "吉林化纤", 1.78e8, "日涨幅偏离值达到7%的前5只证券"),
                lhb_row("002636", "金安国纪", -2.0e8, "日跌幅偏离值达到7%的前5只证券"),
                lhb_row("002733", "雄韬股份", 3.0e8, "连续三个交易日内，涨幅偏离值累计达到20%的证券")]),
            "em_lhb_org": pd.DataFrame([org_row("600825", "新华传媒", 1.1e8, "连续10个交易日内4次出现同正向异常波动的证券"),
                                        org_row("000420", "吉林化纤", 8e7), org_row("600663", "陆家嘴", -1.8e8)]),
            "em_lhb_branch": pd.DataFrame([branch_row("某证券某营业部", 5e8, 7e8), branch_row("深股通专用", 2e8, 9e8)]),
            "em_fund_flow_rank": conn_err("push2 连接被重置"),
            "sina_fund_flow_all": pd.DataFrame([
                {"symbol": "sz300750", "name": "宁 德 时 代", "trade": "297", "changeratio": "0.03", "amount": "1.5e10",
                 "r0_net": "1.9e9", "r0_ratio": "0.12"},
                {"symbol": "sh600418", "name": "江淮汽车", "trade": "40", "changeratio": "-0.08", "amount": "6e9",
                 "r0_net": "-1.6e9", "r0_ratio": "-0.25"}]),
            "sina_quotes": C.parse_sina_quotes(quote_text([("sz300750", "宁德时代", 286.75, 297.76, 1.6e10),
                                                           ("sh600418", "江淮汽车", 43.47, 40.0, 6.3e9)])),
            "em_northbound_hist": pd.DataFrame({"TRADE_DATE": ["2026-10-09", "2026-10-08"],
                                                "DEAL_AMT": [296735.35, 277155.11], "DEAL_NUM": [1, 1]}),
            "em_margin_daily": pd.DataFrame({"STATISTICS_DATE": ["2026-10-09", "2026-10-08"],
                                             "FIN_BALANCE": [25300.0, 25445.0], "LOAN_BALANCE": [296.0, 295.0],
                                             "MARGIN_BALANCE": [25596.0, 25740.0], "FIN_BUY_AMT": [1504.0, 1391.0],
                                             "TRADE_AMT_RATIO": [7.9, 8.3]}),
        }
        self.big_sina = pd.DataFrame([{"symbol": f"sz{300000 + i}", "name": f"S{i}", "trade": "10",
                                       "changeratio": "0.01", "amount": "1e8", "r0_in": "2e6", "r0_out": "1e6",
                                       "r0_net": "1e6", "r0_ratio": "0.01"} for i in range(1500)])

    def tearDown(self):
        self.tmp.cleanup()

    def cache_path(self, func, **kwargs):
        from kmcf.fetch import cache_key
        return Path(self.tmp.name) / f"{cache_key(func, (), kwargs)}.pkl"

    def test_full_data_with_push2_fallback(self):
        ctx = make_ctx(self.tmp.name, self.full)
        d = funds.build(ctx).to_dict()
        json.dumps(d, ensure_ascii=False, default=str)
        labels = [k["label"] for k in d["kpis"]]
        self.assertEqual(labels[:3], ["主力净流入", "超大单净流入", "中小单净流入"])
        self.assertIn("龙虎榜上榜", labels)
        self.assertIn("两融余额", labels)
        self.assertTrue(d["summary"][0].startswith("主力资金净流出170.00亿"))
        self.assertIn("连续3日净流出", d["summary"][0])
        for line in d["summary"]:                      # 句末统一“。”，数字与单位之间不加空格
            self.assertTrue(line.endswith("。"), line)
            self.assertNotRegex(line, r"\d [%亿万只日]")
        # 龙虎榜：雄韬股份（3日累计）不进合计和柱图
        lhb_sum = [s for s in d["summary"] if s.startswith("龙虎榜")][0]
        self.assertIn("龙虎榜3只个股上榜，当日口径合计净卖出2200.00万", lhb_sum)
        self.assertIn("另有1只仅因多日累计上榜", lhb_sum)
        self.assertIn("净买入最多：吉林化纤", lhb_sum)
        self.assertEqual(kpi(d, "龙虎榜净买入（当日口径）")["value"], "-2200.00万")
        self.assertNotIn("雄韬股份", block(d, "龙虎榜净买入（共")["option"]["yAxis"]["data"])
        self.assertTrue(block(d, "龙虎榜净买入（共")["title"].endswith("只，当日口径）"))
        # 机构：新华传媒（10 日累计）不进合计，单独列表
        org_sum = [s for s in d["summary"] if s.startswith("机构")][0]
        self.assertIn("机构净买入最多：吉林化纤", org_sum)
        self.assertEqual(kpi(d, "机构净买入（当日口径）")["value"], "-1.00亿")
        self.assertEqual(len(block(d, "仅因多日累计上榜的机构买卖")["rows"]), 1)
        self.assertEqual(block(d, "机构净买入")["title"], "机构净买入（共1只）")   # 不足 top_n 只时写“共n只”
        # 个股排行：push2 失败 -> 新浪；涨跌幅/成交额来自收盘行情
        self.assertTrue(ctx.shared.get("push2_down"))
        rank = block(d, "个股主力净流入")
        self.assertEqual(rank["title"], "个股主力净流入（共1只）")
        self.assertEqual(rank["note"], "")                          # 数据源只在口径说明里写一次
        labels = [c["label"] for c in rank["columns"]]
        self.assertIn("涨跌幅", labels)
        self.assertEqual(rank["rows"][0][labels.index("涨跌幅")]["text"], "+3.84%")
        self.assertEqual(rank["rows"][0][labels.index("成交额")]["text"], "160.00亿")
        self.assertTrue(any("新浪资金流统计不含尾盘集合竞价" in n for n in d["notes"]))
        self.assertTrue(any(n.endswith("东方财富暂时无法访问，已改用新浪财经。") for n in d["notes"]))
        self.assertEqual(rank["rows"][0][0]["text"], "宁德时代")                   # 新浪简称里的空格已去掉
        self.assertIn("个股主力净流入最多：宁德时代（+19.00亿），主力净流出最多：江淮汽车（-16.00亿），数据源为新浪财经。",
                      d["summary"])
        # 供自选股模块使用的排行：名次从 1 起，金额单位元，带口径来源
        fr = ctx.shared["funds.flow_rank"]
        self.assertEqual((fr["source"], fr["word_in"], fr["word_out"], fr["top_n"]), ("新浪财经", "主力净流入", "主力净流出", 5))
        self.assertIn("新浪主力口径", fr["basis"])
        self.assertEqual(fr["inflow"], [{"code": "300750", "name": "宁德时代", "rank": 1, "net": 1.9e9}])
        self.assertEqual(fr["outflow"], [{"code": "600418", "name": "江淮汽车", "rank": 1, "net": -1.6e9}])
        json.dumps(fr, ensure_ascii=False)
        self.assertEqual(d["errors"], [])
        h = ctx.history.rows[D0.isoformat()]
        self.assertEqual(h["funds_lhb_count"], 3)
        self.assertEqual(h["funds_lhb_net"], -22000000)
        self.assertEqual(h["funds_org_net"], -100000000)
        self.assertEqual(h["funds_main_net"], -17000000000)
        # 北向图的备注里带上股通专用席位；营业部标题不再叫“游资”
        nb_note = block(d, "北向成交总额")["note"]
        self.assertTrue(nb_note.startswith("龙虎榜上榜个股中，"), nb_note)
        self.assertIn("深股通专用净买入", nb_note)
        self.assertNotIn("）（", nb_note)                             # 括号不连用
        self.assertTrue(block(d, "活跃营业部（买卖总额前")["note"].count("并非都是游资"))
        # 两融：KPI 写明融资余额，要点写具体日期而不是“前一交易日”
        mk = kpi(d, "两融余额")
        self.assertEqual(mk["sub"], "融资余额2.53万亿（10-09），较10-08减少145.00亿")
        self.assertIn("融资余额2.53万亿（10-09），较10-08减少145.00亿。", d["summary"])
        self.assertFalse(any("前一交易日" in s for s in d["summary"]))
        # 标题、KPI 里数字与单位/中文之间不加空格；说明里不写内部实现细节
        for t in [b["title"] for b in d["blocks"]] + [k["sub"] for k in d["kpis"]] + [k["value"] for k in d["kpis"]]:
            self.assertNotRegex(t, r"\d [\u4e00-\u9fff]|[\u4e00-\u9fff] \d", t)
        self.assertFalse(any("统一为元" in n or "成功率" in n for n in d["notes"]))

    def test_market_flow_falls_back_to_sina(self):
        data = dict(self.full, stock_market_fund_flow=conn_err("push2his 连接被重置"), sina_fund_flow_all=self.big_sina)
        ctx = make_ctx(self.tmp.name, data)
        d = funds.build(ctx).to_dict()
        k = d["kpis"][0]
        self.assertEqual(k["label"], "主力净流入个股（新浪）")
        self.assertEqual(k["tone"], "neutral")                         # 不给方向色
        # 大盘资金流不可用的说明只在错误框/口径说明里，不占用复盘要点
        self.assertTrue(d["summary"][0].startswith("龙虎榜3只个股上榜"))
        self.assertFalse(any("不可用" in s or "新浪口径" in s for s in d["summary"]))
        self.assertTrue(ctx.shared.get("push2his_down"))
        self.assertTrue(any("新浪口径" in e for e in d["errors"]))
        h = ctx.history.rows.get(D0.isoformat(), {})
        self.assertNotIn("funds_main_net", h)
        self.assertIn("funds_lhb_net", h)
        # 大盘备用源与个股排行共用一次新浪请求
        self.assertEqual(ctx.fetcher.calls.count("sina_fund_flow_all"), 1)

    def test_market_flow_skipped_by_persisted_flag(self):
        """push2his_down 来自之前的生成（保留 3 小时）：报告里只写“暂时无法访问”，“3 小时”等运行时状态只写日志。"""
        ctx = make_ctx(self.tmp.name, dict(self.full, sina_fund_flow_all=self.big_sina))
        ctx.shared["push2his_down"] = True
        d = funds.build(ctx).to_dict()
        self.assertNotIn("stock_market_fund_flow", ctx.fetcher.calls)
        err = next(e for e in d["errors"] if e.startswith("东方财富大盘资金流"))
        self.assertEqual(err, "东方财富大盘资金流暂时无法访问，主力净流入金额与趋势缺失，"
                              "仅展示新浪口径的主力净流入/净流出个股家数")
        text = " ".join(d["errors"] + d["notes"])
        self.assertNotIn("小时", text)
        self.assertNotIn("本次生成中", text)

    def test_market_flow_history_date_sina_snapshot_unavailable(self):
        """历史日期：东方财富不可用、新浪只有快照 —— 新浪部分写进说明，错误框只留一句。"""
        ctx = make_ctx(self.tmp.name, dict(self.full, stock_market_fund_flow=conn_err("push2his 连接被重置")),
                       snapshot_ok=False)
        d = funds.build(ctx).to_dict()
        flow_err = [e for e in d["errors"] if "大盘资金流" in e]
        self.assertEqual(flow_err, ["东方财富大盘资金流暂时无法访问，主力净流入金额与趋势缺失"])
        self.assertTrue(any(n.startswith("大盘资金流向（新浪备用源）") and n.endswith("本次省略。") for n in d["notes"]))
        self.assertIsNone(kpi(d, "主力净流入个股（新浪）"))

    def test_market_flow_nan_day_falls_back_and_history_kept(self):
        data = dict(self.full, stock_market_fund_flow=mff_frame(self.days, [1e9, -2e9, -4e9, float("nan")]),
                    sina_fund_flow_all=self.big_sina)
        ctx = make_ctx(self.tmp.name, data)
        d = funds.build(ctx).to_dict()
        self.assertEqual(d["kpis"][0]["label"], "主力净流入个股（新浪）")
        self.assertTrue(any("主力净流入数据缺失" in e for e in d["errors"]))
        self.assertFalse(any("写入历史指标" in e for e in d["errors"]))
        h = ctx.history.rows[D0.isoformat()]
        self.assertNotIn("funds_main_net", h)
        self.assertEqual(h["funds_lhb_count"], 3)
        self.assertFalse(ctx.shared.get("push2his_down"))             # 数据问题不是连接问题

    def test_market_flow_parse_error_does_not_mark_down(self):
        e = FetchError("stock_market_fund_flow 获取失败（已重试 3 次）：'klines'")
        e.__cause__ = KeyError("klines")
        ctx = make_ctx(self.tmp.name, dict(self.full, stock_market_fund_flow=e, sina_fund_flow_all=self.big_sina))
        d = funds.build(ctx).to_dict()
        self.assertFalse(ctx.shared.get("push2his_down"))
        self.assertTrue(any("接口返回格式变化（缺少 klines 字段）" in x for x in d["errors"]))

    def test_renamed_columns_report_errors_not_zero(self):
        data = dict(self.full,
                    em_lhb_detail=self.full["em_lhb_detail"].rename(columns={"BILLBOARD_NET_AMT": "NET_AMT"}),
                    em_lhb_org=self.full["em_lhb_org"].rename(columns={"NET_BUY_AMT": "NETAMT"}))
        ctx = make_ctx(self.tmp.name, data)
        d = funds.build(ctx).to_dict()
        self.assertTrue(any(e.startswith("龙虎榜：") and "缺少 BILLBOARD_NET_AMT" in e for e in d["errors"]))
        self.assertTrue(any(e.startswith("机构动向：") and "缺少 NET_BUY_AMT" in e for e in d["errors"]))
        self.assertFalse(any("合计净买入 0" in s for s in d["summary"]))
        h = ctx.history.rows[D0.isoformat()]
        for key in ("funds_lhb_count", "funds_lhb_net", "funds_org_net"):
            self.assertNotIn(key, h)
        self.assertNotIn("funds.lhb", ctx.shared)                      # 自选股模块会自己再取

    def test_datacenter_down_skips_remaining_requests(self):
        data = dict(self.full, **{name: conn_err() for name in DC_FUNCS})
        ctx = make_ctx(self.tmp.name, data)
        d = funds.build(ctx).to_dict()
        self.assertTrue(ctx.shared.get("datacenter_down"))
        self.assertEqual(sum(ctx.fetcher.calls.count(n) for n in DC_FUNCS), 1)   # 只请求了第一个
        self.assertTrue(any(e.startswith("龙虎榜：服务器断开连接") for e in d["errors"]))
        self.assertTrue(any(e.startswith("两融：") and "东方财富数据中心暂时无法访问" in e for e in d["errors"]))
        self.assertFalse(any("本次生成中" in e for e in d["errors"]))
        self.assertFalse(any("Connection" in e or "aborted" in e for e in d["errors"]))

    def test_org_and_branch_empty_while_lhb_published(self):
        data = dict(self.full, em_lhb_org=pd.DataFrame(), em_lhb_branch=pd.DataFrame())
        ctx = make_ctx(self.tmp.name, data)
        p_org = self.cache_path(D.em_lhb_org, date=D0.isoformat())
        p_org.write_bytes(b"x")
        d = funds.build(ctx).to_dict()
        self.assertTrue(any("机构买卖统计暂未获取到" in n and "3只股票提到机构席位" in n for n in d["notes"]))
        self.assertFalse(any("没有机构专用席位成交记录" in n for n in d["notes"]))
        self.assertTrue(any(n.startswith("活跃营业部：数据暂未获取到") for n in d["notes"]))
        self.assertFalse(p_org.exists())                               # 空结果不留缓存

    def test_quotes_fail_hides_price_columns(self):
        ctx = make_ctx(self.tmp.name, dict(self.full, sina_quotes=conn_err()))
        d = funds.build(ctx).to_dict()
        labels = [c["label"] for c in block(d, "个股主力净流入")["columns"]]
        self.assertNotIn("涨跌幅", labels)
        self.assertNotIn("成交额", labels)
        self.assertTrue(any("收盘行情获取失败" in n for n in d["notes"]))
        self.assertTrue(ctx.shared.get("sina_hq_down"))

    def test_respects_flags_from_other_modules(self):
        ths = pd.DataFrame({"序号": [1, 2], "股票代码": [300530, 2912], "股票简称": ["领湃科技", "中新赛克"],
                            "最新价": [28.9, 28.55], "涨跌幅": ["20.02%", "10.02%"], "换手率": ["1%", "1%"],
                            "流入资金(元)": ["3.72亿", "3.12亿"], "流出资金(元)": ["3.51亿", "3.59亿"],
                            "净额(元)": ["2098.28万", "-4715.59万"], "成交额(元)": ["7.23亿", "6.72亿"]})
        ctx = make_ctx(self.tmp.name, dict(self.full, ths_fund_flow_rank=ths))
        ctx.shared.update(push2_down=True, sina_down=True)
        d = funds.build(ctx).to_dict()
        self.assertNotIn("em_fund_flow_rank", ctx.fetcher.calls)
        self.assertNotIn("sina_fund_flow_all", ctx.fetcher.calls)
        self.assertIsNotNone(block(d, "个股资金净流入"))
        note = next(n for n in d["notes"] if n.startswith("个股资金排行"))
        self.assertTrue(note.startswith("个股资金排行：同花顺"), note)
        # push2_down 跨次生成保留 3 小时：报告里只写“暂时无法访问”
        self.assertIn("东方财富暂时无法访问，已改用同花顺。", note)
        self.assertFalse(any("小时" in n or "本次生成中" in n for n in d["notes"]))
        self.assertEqual(ctx.shared["funds.flow_rank"]["word_in"], "资金净流入")   # 同花顺口径
        self.assertEqual(ctx.shared["funds.flow_rank"]["source"], "同花顺")

    def test_flag_set_but_cache_usable_is_not_skipped(self):
        ctx = make_ctx(self.tmp.name, self.full)
        ctx.shared["datacenter_down"] = True
        self.cache_path(D.em_lhb_detail, date=D0.isoformat()).write_bytes(b"x")
        d = funds.build(ctx).to_dict()
        self.assertIn("em_lhb_detail", ctx.fetcher.calls)
        self.assertIsNotNone(kpi(d, "龙虎榜上榜"))
        self.assertNotIn("em_lhb_org", ctx.fetcher.calls)               # 没缓存的仍然跳过

    def test_northbound_missing_day_past_date_is_holiday(self):
        nb = pd.DataFrame({"TRADE_DATE": ["2026-10-08", "2026-09-30"], "DEAL_AMT": [277155.11, 207942.0]})
        ctx = make_ctx(self.tmp.name, dict(self.full, em_northbound_hist=nb))
        p = self.cache_path(D.em_northbound_hist, date=D0.isoformat(), n=30)
        p.write_bytes(b"x")
        with mock.patch.object(funds, "_now", return_value=AFTER_CLOSE):
            d = funds.build(ctx).to_dict()
        self.assertIn("复盘日无北向成交记录", kpi(d, "北向成交总额")["sub"])
        self.assertTrue(any("通常是港股休市" in n for n in d["notes"]))
        self.assertTrue(p.exists())                                    # 休市不是“未发布”，保留缓存

    def test_northbound_missing_day_same_evening_is_pending(self):
        nb = pd.DataFrame({"TRADE_DATE": ["2026-10-08"], "DEAL_AMT": [277155.11]})
        ctx = make_ctx(self.tmp.name, dict(self.full, em_northbound_hist=nb))
        p = self.cache_path(D.em_northbound_hist, date=D0.isoformat(), n=30)
        p.write_bytes(b"x")
        with mock.patch.object(funds, "_now", return_value=dt.datetime(2026, 10, 9, 19, 0)):
            d = funds.build(ctx).to_dict()
        self.assertIn("复盘日暂无数据", kpi(d, "北向成交总额")["sub"])
        self.assertFalse(p.exists())                                   # 下次生成重新取

    def test_northbound_all_null(self):
        nb = pd.DataFrame({"TRADE_DATE": ["2026-10-09"], "DEAL_AMT": [None]})
        d = funds.build(make_ctx(self.tmp.name, dict(self.full, em_northbound_hist=nb))).to_dict()
        self.assertIsNone(kpi(d, "北向成交总额"))
        self.assertTrue(any(n.startswith("北向成交数据暂缺") for n in d["notes"]))

    def test_everything_down_no_snapshot(self):
        ctx = make_ctx(self.tmp.name, {}, snapshot_ok=False)
        d = funds.build(ctx).to_dict()                     # 不抛异常
        self.assertTrue(d["errors"])
        self.assertNotIn("funds.flow_rank", ctx.shared)    # 没有排行时不设置，自选股模块也就不显示
        self.assertTrue(any("实时快照" in n for n in d["notes"]))
        self.assertEqual(d["summary"], [])

    def test_no_snapshot_uses_cached_wording(self):
        ctx = make_ctx(self.tmp.name, self.full, snapshot_ok=False)
        self.cache_path(D.sina_fund_flow_all).write_bytes(b"x")
        codes = "300750,600418"
        self.cache_path(D.sina_quotes, codes=codes).write_bytes(b"x")
        ctx.fetcher.snapshot_ok = True       # 假 Fetcher：有缓存时快照照常返回（真实 Fetcher 读缓存）
        ctx.fetcher.data["em_fund_flow_rank"] = SnapshotUnavailable("快照不可用")
        d = funds.build(ctx).to_dict()
        note = next(n for n in d["notes"] if n.startswith("个股资金排行"))
        self.assertIn("当日收盘后缓存的快照", note)
        self.assertIn("快照不对应复盘日且无当日缓存", note)
        self.assertNotIn("实时快照", note)

    def test_lhb_not_published_today(self):
        today = dt.date.today()
        data = {k: v for k, v in self.full.items()}
        data.update(em_lhb_detail=pd.DataFrame(), em_lhb_org=pd.DataFrame(), em_lhb_branch=pd.DataFrame())
        ctx = make_ctx(self.tmp.name, data, trade_date=today)
        # 缓存文件存在时应被删除（空表不应留在缓存里）
        p = self.cache_path(D.em_lhb_detail, date=today.isoformat())
        p.write_bytes(b"x")
        d = funds.build(ctx).to_dict()
        self.assertTrue(any(n.startswith("龙虎榜：龙虎榜尚未发布（通常17:00后）") for n in d["notes"]))
        self.assertFalse(any("尚未发布" in s for s in d["summary"]))           # 状态说明不占用复盘要点
        self.assertEqual(kpi(d, "龙虎榜")["value"], "未发布")
        self.assertFalse(any("龙虎榜" in e for e in d["errors"]))
        self.assertFalse(any(n.startswith("机构动向") or n.startswith("活跃营业部") for n in d["notes"]))
        self.assertFalse(p.exists())

    def test_lhb_empty_on_history_date(self):
        data = dict(self.full, em_lhb_detail=pd.DataFrame())
        with mock.patch.object(funds, "_now", return_value=AFTER_CLOSE):
            d = funds.build(make_ctx(self.tmp.name, data)).to_dict()
        k = kpi(d, "龙虎榜")
        self.assertEqual((k["value"], k["sub"]), ("无数据", "该日无龙虎榜记录"))
        self.assertTrue(any(n.startswith("龙虎榜：未查询到2026-10-09的龙虎榜数据") for n in d["notes"]))
        self.assertFalse(any("未查询到" in s for s in d["summary"]))

    def test_short_err(self):
        self.assertIn("限流", funds._short_err(FetchError("x 获取失败：('Connection aborted.', RemoteDisconnected())")))
        self.assertEqual(funds._short_err(RuntimeError("abc")), "abc")
        self.assertIn("限流", funds._short_err(conn_err()))
        e = FetchError("y 获取失败")
        e.__cause__ = requests.ReadTimeout("read timed out")
        self.assertEqual(funds._short_err(e), "请求超时")
        self.assertEqual(funds._short_err(KeyError("SECURITY_CODE")), "接口返回格式变化（缺少 SECURITY_CODE 字段）")
        self.assertEqual(funds._short_err(funds._Fail("中文说明")), "中文说明")
        self.assertTrue(funds._is_conn_error(conn_err()))
        self.assertFalse(funds._is_conn_error(FetchError("解析失败")))

    def test_with_timeout(self):
        import time as _t

        def slow():
            _t.sleep(1.0)
            return 1

        wrapped = D.with_timeout(slow, 0.1)
        self.assertEqual(wrapped.__name__, "slow")                     # 缓存键不变
        t0 = _t.monotonic()
        with self.assertRaises(TimeoutError):
            wrapped()
        with self.assertRaises(TimeoutError):
            wrapped()                                                  # 第二次立即失败
        self.assertLess(_t.monotonic() - t0, 0.8)
        self.assertEqual(D.with_timeout(lambda: 2, 1)(), 2)


if __name__ == "__main__":
    unittest.main()
