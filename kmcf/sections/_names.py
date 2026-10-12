"""各复盘模块共用的名称规整（纯函数，不联网）。

normalize_industry：东方财富涨停池等接口的“所属行业”（申万二级口径）→ 统一显示名：
    补全被截断的名称（汽车零部 → 汽车零部件），去掉二级行业名末尾的“Ⅱ”（证券Ⅱ → 证券）。
clean_stock_name：股票简称去掉半角/全角空格（东方财富、新浪把三字简称写成“粤 传 媒”）。
"""

import math
import re

# 申万二级行业（2021 版）。东方财富涨停池的“所属行业”是这套名称，但最多只给 8 个 GBK 字节（4 个汉字），
# 例如“汽车零部件”变成“汽车零部”，这里用于补全；显示时去掉二级行业名末尾的“Ⅱ”。
SW_L2 = (
    "种植业 渔业 林业Ⅱ 饲料 农产品加工 养殖业 动物保健Ⅱ 农业综合Ⅱ "
    "化学原料 化学制品 化学纤维 塑料 橡胶 农化制品 非金属材料Ⅱ "
    "普钢 特钢Ⅱ 冶钢原料 工业金属 贵金属 小金属 能源金属 金属新材料 "
    "半导体 元件 光学光电子 消费电子 电子化学品Ⅱ 其他电子Ⅱ "
    "乘用车 商用车 汽车零部件 汽车服务 摩托车及其他 "
    "白色家电 黑色家电 小家电 厨卫电器 照明设备Ⅱ 家电零部件Ⅱ 其他家电Ⅱ "
    "白酒Ⅱ 非白酒 饮料乳品 休闲食品 调味发酵品Ⅱ 食品加工 "
    "纺织制造 服装家纺 饰品 造纸 包装印刷 家居用品 文娱用品 "
    "化学制药 中药Ⅱ 生物制品 医药商业 医疗器械 医疗服务 电力 燃气Ⅱ "
    "物流 铁路公路 航运港口 航空机场 房地产开发 房地产服务 "
    "贸易Ⅱ 一般零售 专业连锁Ⅱ 互联网电商 旅游零售Ⅱ "
    "体育Ⅱ 本地生活服务Ⅱ 专业服务 酒店餐饮 旅游及景区 教育 "
    "国有大型银行Ⅱ 股份制银行Ⅱ 城商行Ⅱ 农商行Ⅱ 证券Ⅱ 保险Ⅱ 多元金融 综合Ⅱ "
    "水泥 玻璃玻纤 装修建材 房屋建设Ⅱ 装修装饰Ⅱ 基础建设 专业工程 工程咨询服务Ⅱ "
    "电机Ⅱ 其他电源设备Ⅱ 光伏设备 风电设备 电池 电网设备 "
    "通用设备 专用设备 轨交设备Ⅱ 工程机械 自动化设备 "
    "航天装备Ⅱ 航空装备Ⅱ 地面兵装Ⅱ 航海装备Ⅱ 军工电子Ⅱ "
    "计算机设备 IT服务Ⅱ 软件开发 游戏Ⅱ 广告营销 影视院线 数字媒体 社交Ⅱ 出版 电视广播Ⅱ "
    "通信服务 通信设备 煤炭开采 焦炭Ⅱ 油气开采Ⅱ 油服工程 炼化及贸易 "
    "环境治理 环保设备Ⅱ 个护用品 化妆品 医疗美容"
).split()
SW_PLAIN = frozenset(n.rstrip("ⅡⅢ") for n in SW_L2)
EM_INDUSTRY_MAX_BYTES = 8   # 东方财富“所属行业”字段的最大长度（GBK 字节）

_MISSING_TEXT = frozenset({"", "nan", "NaN", "None", "<NA>", "-", "--", "—"})
_SPACES = re.compile(r"\s+")   # Python 的 \s 含全角空格 U+3000 与不换行空格 U+00A0


def _text(v) -> str:
    """None / NaN / 'nan' / '-' 之类 → ''，其余转成去掉首尾空白的字符串。"""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    s = str(v).strip()
    return "" if s in _MISSING_TEXT else s


def normalize_industry(name) -> str:
    """行业名统一：去空格、补全被截断的申万二级行业名、去掉末尾的“Ⅱ/Ⅲ”。缺失返回 ''（由调用方决定显示什么）。

    只在名称达到字段长度上限（被截断的特征）且申万二级里恰好有一个以它开头的行业时才补全；
    不认识的名称原样保留（只去掉末尾的“Ⅱ”）。
    """
    s = _SPACES.sub("", _text(name))
    plain = s.rstrip("ⅡⅢ")
    if not plain or plain in SW_PLAIN:
        return plain
    if len(s.encode("gbk", errors="replace")) >= EM_INDUSTRY_MAX_BYTES - 1:  # 被截断的名称
        cands = [f for f in SW_PLAIN if f.startswith(plain)]
        if len(cands) == 1:
            return cands[0]
    return plain


def clean_stock_name(name) -> str:
    """股票简称去掉所有半角/全角空格（“粤 传 媒” → “粤传媒”）。缺失返回 ''。"""
    return _SPACES.sub("", _text(name))
