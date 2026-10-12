"""各复盘模块共用的名称规整（kmcf/sections/_names.py）与站点跳过文案（_site.py）的单元测试。

运行：.venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kmcf import report  # noqa: E402
from kmcf.sections import _names as names  # noqa: E402
from kmcf.sections import _site as site  # noqa: E402


class TestNormalizeIndustry(unittest.TestCase):
    def test_truncated_and_suffix(self):
        """与原 sectors 模块的补全映射行为一致，并覆盖集成测试报告里出现的名称。"""
        cases = {
            # 东方财富“所属行业”最多 4 个汉字：截断名补全为申万二级全称
            "汽车零部": "汽车零部件", "房地产开": "房地产开发", "自动化设": "自动化设备", "其他电源": "其他电源设备",
            "电子化学": "电子化学品", "家电零部": "家电零部件", "工程咨询": "工程咨询服务", "光学光电": "光学光电子",
            "农产品加": "农产品加工", "计算机设": "计算机设备", "互联网电": "互联网电商", "房地产服": "房地产服务",
            # 二级行业名末尾的“Ⅱ”去掉
            "IT服务Ⅱ": "IT服务", "中药Ⅱ": "中药", "焦炭Ⅱ": "焦炭", "证券Ⅱ": "证券", "游戏Ⅱ": "游戏",
            # 本来就完整的、不认识的名称原样保留
            "电池": "电池", "其他电子": "其他电子", "医疗器械": "医疗器械", "某新行业": "某新行业",
            " 出版 ": "出版", "汽车零部件": "汽车零部件",
        }
        for raw, full in cases.items():
            self.assertEqual(names.normalize_industry(raw), full, raw)

    def test_ambiguous_prefix_not_completed(self):
        # “房地产”开头的有“房地产开发”“房地产服务”两个：名称没被截断（不足 4 字）时不猜
        self.assertEqual(names.normalize_industry("房地产"), "房地产")
        self.assertEqual(names.normalize_industry("医疗"), "医疗")

    def test_missing(self):
        for v in (None, float("nan"), "", " ", "nan", "None", "-", "—"):
            self.assertEqual(names.normalize_industry(v), "", repr(v))

    def test_idempotent(self):
        for n in names.SW_L2:
            once = names.normalize_industry(n)
            self.assertEqual(names.normalize_industry(once), once)
            self.assertFalse(once.endswith("Ⅱ"))


class TestCleanStockName(unittest.TestCase):
    def test_spaces_removed(self):
        self.assertEqual(names.clean_stock_name("粤 传 媒"), "粤传媒")
        self.assertEqual(names.clean_stock_name("五　粮　液"), "五粮液")     # 全角空格
        self.assertEqual(names.clean_stock_name(" 七 匹 狼 "), "七匹狼")
        self.assertEqual(names.clean_stock_name("万 科Ａ"), "万科Ａ")           # 不换行空格
        self.assertEqual(names.clean_stock_name("*ST 华铁"), "*ST华铁")
        self.assertEqual(names.clean_stock_name("宁德时代"), "宁德时代")
        self.assertEqual(names.clean_stock_name("深物业A"), "深物业A")

    def test_missing(self):
        for v in (None, float("nan"), "", "  ", "nan", "None"):
            self.assertEqual(names.clean_stock_name(v), "", repr(v))
        self.assertEqual(names.clean_stock_name(600519), "600519")


class TestSiteSkipText(unittest.TestCase):
    def test_constants_match_report(self):
        """_site 不能 import report（循环导入），常量在这里核对。"""
        self.assertEqual(set(site.PERSIST_FLAGS), set(report.PERSIST_SITE_FLAGS))
        self.assertEqual(dt.timedelta(hours=site.PERSIST_HOURS), report.SITE_FLAG_TTL)

    def test_texts(self):
        """报告文字只写“暂时无法访问”；“3 小时内不再尝试”“本次生成中已失败”这类运行时状态只写日志。"""
        for flag in ("push2_down", "push2his_down"):
            with self.assertLogs("kmcf", "INFO") as logs:
                self.assertEqual(site.skip_reason(flag), "暂时无法访问")
                self.assertEqual(site.skip_brief(flag), "暂时无法访问")
            self.assertTrue(all("3 小时内不再尝试" in x for x in logs.output))
            self.assertIn("3 小时内不再尝试", site.skip_detail(flag))
        for flag in ("push2ex_down", "datacenter_down", "ths_down", "sina_down", "tx_down"):
            with self.assertLogs("kmcf", "INFO") as logs:
                self.assertEqual(site.skip_reason(flag), "暂时无法访问")
                self.assertEqual(site.skip_brief(flag), "暂时无法访问")
            self.assertTrue(all("本次生成中已连接失败" in x for x in logs.output))
            self.assertNotIn("小时", site.skip_detail(flag))


if __name__ == "__main__":
    unittest.main()
