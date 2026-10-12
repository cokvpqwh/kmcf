"""生成一份复盘报告时，传给各模块的运行上下文。

ctx.shared 约定的键（模块之间共享，按 SECTIONS 顺序 market → sectors → funds → watchlist 生成）：
  站点标记（只在连接级失败时设为 True；之后同站点只读缓存，没有缓存就跳过）：
    push2_down       push2.eastmoney.com（东方财富实时行情列表）
    push2his_down    push2his.eastmoney.com（东方财富历史 K 线/资金流）
    push2ex_down     push2ex.eastmoney.com（涨停池、涨跌分布）
    datacenter_down  datacenter-web.eastmoney.com（龙虎榜等数据中心）
    ths_down         10jqka.com.cn（同花顺）
    sina_down / sina_kline_down / sina_hq_down   新浪各接口
    tx_down          腾讯行情    exchange_down   上交所/深交所官网
    其中 push2_down、push2his_down 会跨次生成保留 3 小时（见 report.PERSIST_SITE_FLAGS）。
  共享结果：
    market    {'metrics', 'breadth', 'turnover', 'turnover_prev', 'index'}（market 模块）
    zt_pool   规整后的涨停池 DataFrame，代码已补齐 6 位（market 模块）
    funds.lhb 合并后的龙虎榜 DataFrame，金额单位元，只含 A 股（funds 模块）
"""

import datetime as dt
import logging
from dataclasses import dataclass, field

from . import trade_calendar as tc
from .fetch import Fetcher
from .history import History


@dataclass
class Context:
    trade_date: dt.date          # 复盘的交易日
    cfg: dict                    # 用户配置（watchlist、top_n 等）
    fetcher: Fetcher
    history: History
    snapshot_ok: bool            # 此刻抓“实时快照”类接口能否代表 trade_date 的收盘
    log: logging.Logger
    shared: dict = field(default_factory=dict)  # 模块间共享的已计算结果（按约定的 key）

    @property
    def date_str(self) -> str:   # 'YYYYMMDD'，多数 AKShare 接口要这个格式
        return self.trade_date.strftime("%Y%m%d")

    @property
    def date_iso(self) -> str:   # 'YYYY-MM-DD'
        return self.trade_date.isoformat()

    def fetch(self, func, *args, snapshot: bool = False, volatile: bool = False, **kwargs):
        """统一的数据获取入口：带缓存/重试/限速。见 Fetcher.call。

        snapshot=True：只能取“最新实时快照”的接口（不能按日期查询）。
        volatile=True：盘后陆续发布的数据（龙虎榜等），当天晚上 8 点前不读缓存。
        其余关键字参数原样传给 AKShare 函数。
        """
        return self.fetcher.call(func, *args, snapshot=snapshot, volatile=volatile, **kwargs)

    def prev_trade_day(self, n: int = 1) -> dt.date | None:
        return tc.prev_trade_day(self.trade_date, n)

    def recent_trade_days(self, n: int) -> list[dt.date]:
        """截至复盘日（含）最近 n 个交易日，升序。"""
        return tc.recent_trade_days(self.trade_date, n)
