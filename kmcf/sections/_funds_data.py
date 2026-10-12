"""「龙虎榜与资金」模块的数据获取函数（只负责联网取原始数据，统一经 ctx.fetch 调用以便缓存）。

数据源（2026-10 实测）：
- 东方财富数据中心 datacenter-web.eastmoney.com：龙虎榜明细 / 机构买卖 / 活跃营业部 / 北向成交 / 两融，可按日期查询。
  直接请求接口而不用 akshare 对应函数，原因：akshare 在“当天尚无数据”时抛 TypeError（会被当成失败重试 3 次），
  且机构/营业部两个函数按位置给列命名，接口加列就会错位。
- 东方财富 push2.eastmoney.com：个股资金流排行（实时快照）。只取前 N 条，不像 akshare 那样翻全部 50 多页。
  目前被拦截，失败时依次切换新浪、同花顺。
- 新浪 vip.stock.finance.sina.com.cn：个股资金流（实时快照），一次请求取全市场，
  同时作为个股排行和大盘资金流向的备用源（口径与东方财富不同，页面上会注明）。
- 同花顺 data.10jqka.com.cn：个股资金流“即时”排行（实时快照）。只取需要的 1~2 页，避免 akshare 那样翻 100 多页。
- 新浪 hq.sinajs.cn：个股收盘行情（实时快照，带行情日期）。新浪/同花顺的资金流统计不含尾盘集合竞价，
  个股排行表里的涨跌幅、成交额改用这里的收盘数据。
- 大盘资金流向用 akshare.stock_market_fund_flow（push2his）。它的请求没有设超时，调用方用 with_timeout 包一层。

所有函数返回 DataFrame；“数据为空”返回空 DataFrame（不抛异常），网络/格式错误抛异常交给 Fetcher 重试。
"""

import functools
import threading
import time
from io import StringIO

import pandas as pd
import requests

from . import _funds_calc as C

DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
PUSH2_URL = "https://push2.eastmoney.com/api/qt/clist/get"
THS_URL = "http://data.10jqka.com.cn/funds/ggzjl/field/zjjlr/order/{order}/page/{page}/ajax/1/free/1/"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")
EM_HEADERS = {"User-Agent": UA, "Referer": "https://data.eastmoney.com/"}
TIMEOUT = (5, 15)  # (连接超时, 读取超时) 秒

# 沪深京 A 股（不含 B 股）：深主板、中小板、创业板、沪主板、科创板、北交所
A_SHARE_FS = "m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2,m:0+t:81+s:2048"


# ---------------------------------------------------------------- 通用

def with_timeout(func, seconds: float):
    """给没有设超时的 akshare 函数加总超时（在后台线程里执行）。保留函数名，缓存键与直接调用一致。

    同一个包装对象超时一次后，后续调用（Fetcher 的重试）立即失败，不再每次等满超时。
    """
    timed_out = []

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if timed_out:
            raise TimeoutError(f"{func.__name__} 上次请求已超时，不再重试")
        box = {}

        def run():
            try:
                box["v"] = func(*args, **kwargs)
            except BaseException as e:  # noqa: BLE001  原样转交给调用方
                box["e"] = e

        th = threading.Thread(target=run, name=f"kmcf-{func.__name__}", daemon=True)
        th.start()
        th.join(seconds)
        if th.is_alive():
            timed_out.append(True)
            raise TimeoutError(f"{func.__name__} 超过 {seconds:.0f} 秒未返回")
        if "e" in box:
            raise box["e"]
        return box["v"]
    return wrapper


# ---------------------------------------------------------------- 东方财富数据中心

def dc_report(report: str, filter: str = "", sort_columns: str = "", sort_types: str = "",
              columns: str = "ALL", page_size: int = 500, max_pages: int = 10) -> pd.DataFrame:
    """东方财富数据中心通用查询，返回原始英文列名的 DataFrame。没有数据时返回空表。"""
    params = {"reportName": report, "columns": columns, "filter": filter, "pageNumber": 1,
              "pageSize": page_size, "sortColumns": sort_columns, "sortTypes": sort_types,
              "source": "WEB", "client": "WEB"}
    frames = []
    page = 1
    while True:
        params["pageNumber"] = page
        r = requests.get(DC_URL, params=params, headers=EM_HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        j = r.json()
        res = j.get("result")
        if not res:
            # code=9201 / “返回数据为空”：该日期还没有数据，属于正常情况
            if j.get("code") == 9201 or "为空" in str(j.get("message", "")):
                break
            raise RuntimeError(f"数据中心 {report} 返回异常：{j.get('code')} {j.get('message')}")
        frames.append(pd.DataFrame(res.get("data") or []))
        pages = int(res.get("pages") or 1)
        if page >= min(pages, max_pages):
            break
        page += 1
        time.sleep(0.3)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def em_lhb_detail(date: str) -> pd.DataFrame:
    """龙虎榜明细（每个上榜原因一行）。date='YYYY-MM-DD'。金额单位：元。"""
    return dc_report(
        "RPT_DAILYBILLBOARD_DETAILSNEW", f"(TRADE_DATE>='{date}')(TRADE_DATE<='{date}')",
        "BILLBOARD_NET_AMT,SECURITY_CODE", "-1,1",
        columns="SECURITY_CODE,SECURITY_NAME_ABBR,TRADE_DATE,EXPLAIN,CLOSE_PRICE,CHANGE_RATE,"
                "BILLBOARD_NET_AMT,BILLBOARD_BUY_AMT,BILLBOARD_SELL_AMT,BILLBOARD_DEAL_AMT,ACCUM_AMOUNT,"
                "DEAL_NET_RATIO,DEAL_AMOUNT_RATIO,TURNOVERRATE,FREE_MARKET_CAP,EXPLANATION,SECURITY_TYPE_CODE")


def em_lhb_org(date: str) -> pd.DataFrame:
    """机构买卖每日统计（龙虎榜机构专用席位）。金额单位：元。"""
    return dc_report("RPT_ORGANIZATION_TRADE_DETAILS", f"(TRADE_DATE>='{date}')(TRADE_DATE<='{date}')",
                     "NET_BUY_AMT,SECURITY_CODE", "-1,1")


def em_lhb_branch(date: str) -> pd.DataFrame:
    """每日活跃营业部。金额单位：元。"""
    return dc_report("RPT_OPERATEDEPT_ACTIVE", f"(ONLIST_DATE>='{date}')(ONLIST_DATE<='{date}')",
                     "TOTAL_NETAMT,OPERATEDEPT_CODE", "-1,1")


def em_northbound_hist(date: str, n: int = 30) -> pd.DataFrame:
    """北向资金（沪股通+深股通）逐日成交，截至 date 的最近 n 条。DEAL_AMT 单位：百万元。"""
    return dc_report("RPT_MUTUAL_DEAL_HISTORY", f"(MUTUAL_TYPE=\"005\")(TRADE_DATE<='{date}')",
                     "TRADE_DATE", "-1", page_size=n, max_pages=1)


def em_margin_daily(date: str, n: int = 60) -> pd.DataFrame:
    """两融账户统计（全市场逐日），截至 date 的最近 n 条。余额/买入额单位：亿元。"""
    return dc_report("RPTA_WEB_MARGIN_DAILYTRADE", f"(STATISTICS_DATE<='{date}')",
                     "STATISTICS_DATE", "-1", page_size=n, max_pages=1)


def em_margin_total(date: str, n: int = 60) -> pd.DataFrame:
    """两融历史（沪深两市合计，交易所口径），截至 date 的最近 n 条。金额单位：元。备用源。"""
    return dc_report("RPTA_RZRQ_LSHJ", f"(DIM_DATE<='{date}')", "DIM_DATE", "-1", page_size=n, max_pages=1)


# ---------------------------------------------------------------- 个股资金流排行（快照）

def em_fund_flow_rank(direction: str = "in", n: int = 20) -> pd.DataFrame:
    """东方财富个股今日主力净流入排行（push2，实时快照）。direction: in=净流入最多, out=净流出最多。

    返回列：code name price pct amount main_net main_ratio super_net big_net mid_net small_net ts
    金额单位：元；pct/main_ratio 为百分比数值；ts 为行情时间戳（秒）。
    """
    params = {"fid": "f62", "po": "1" if direction == "in" else "0", "pz": str(n), "pn": "1", "np": "1",
              "fltt": "2", "invt": "2", "ut": "b2884a393a59ad64002292a3e90d46a5", "fs": A_SHARE_FS,
              "fields": "f12,f14,f2,f3,f6,f62,f184,f66,f72,f78,f84,f124"}
    r = requests.get(PUSH2_URL, params=params, headers=EM_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    data = (r.json() or {}).get("data") or {}
    diff = data.get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    df = pd.DataFrame(diff)
    if df.empty:
        raise RuntimeError("push2 个股资金流返回空数据")
    return df.rename(columns={"f12": "code", "f14": "name", "f2": "price", "f3": "pct", "f6": "amount",
                              "f62": "main_net",
                              "f184": "main_ratio", "f66": "super_net", "f72": "big_net", "f78": "mid_net",
                              "f84": "small_net", "f124": "ts"})


SINA_RANK_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                 "MoneyFlow.ssl_bkzj_ssggzj")


def sina_fund_flow_all(num: int = 8000) -> pd.DataFrame:
    """新浪个股资金流（实时快照），一次取全市场（约 6400 行，含 ETF/债券，由计算模块过滤 A 股），按主力净流入降序。

    金额单位元；changeratio / r0_ratio 为小数；r0_net 为新浪口径的主力净流入。
    一份数据同时用于“个股资金排行”和“大盘资金流向”的备用源（同一调用共享缓存）。
    """
    params = {"page": 1, "num": int(num), "sort": "r0_net", "asc": 0, "bankuai": "", "shichang": ""}
    headers = {"User-Agent": UA, "Referer": "https://vip.stock.finance.sina.com.cn/moneyflow/"}
    r = requests.get(SINA_RANK_URL, params=params, headers=headers, timeout=(5, 30))
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list) or not data:
        raise RuntimeError("新浪个股资金流返回空数据")
    df = pd.DataFrame(data)
    if "r0_net" not in df.columns or "symbol" not in df.columns:
        raise RuntimeError("新浪个股资金流格式异常（缺少 symbol/r0_net）")
    return df


SINA_HQ_URL = "https://hq.sinajs.cn/list="


def sina_quotes(codes: str) -> pd.DataFrame:
    """新浪个股行情（实时快照，收盘后即收盘数据）。codes：逗号分隔的 6 位代码（一次不超过约 100 只）。

    返回列：code name prev_close price amount date（金额单位元，date 为行情日期，调用方据此核对是否为复盘日）。
    """
    syms = [C.sina_symbol(c) for c in codes.split(",") if c.strip()]
    syms = [s for s in syms if s]
    if not syms:
        return pd.DataFrame(columns=C.QUOTE_COLS)
    headers = {"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"}
    r = requests.get(SINA_HQ_URL + ",".join(syms), headers=headers, timeout=(5, 15))
    r.raise_for_status()
    r.encoding = "gbk"
    df = C.parse_sina_quotes(r.text)
    if df.empty:
        raise RuntimeError("新浪行情返回内容无法解析")
    return df


def _ths_headers() -> dict:
    """同花顺接口需要 hexin-v 头（由 akshare 自带的 ths.js 计算）。"""
    import py_mini_racer
    from akshare.datasets import get_ths_js

    js = py_mini_racer.MiniRacer()
    with open(get_ths_js("ths.js"), encoding="utf-8") as f:
        js.eval(f.read())
    return {
        "Accept": "text/html, */*; q=0.01", "hexin-v": js.call("v"), "Host": "data.10jqka.com.cn",
        "Referer": "http://data.10jqka.com.cn/funds/ggzjl/", "User-Agent": UA,
        "X-Requested-With": "XMLHttpRequest",
    }


def ths_fund_flow_rank(direction: str = "in", pages: int = 1) -> pd.DataFrame:
    """同花顺个股资金流“即时”排行，按净额排序取前 pages 页（每页 50 只）。

    返回 read_html 的原始表格（中文列名，金额为“1.23亿”“-4567.8万”这样的字符串），由计算模块解析。
    """
    headers = _ths_headers()
    order = "desc" if direction == "in" else "asc"
    frames = []
    for page in range(1, max(1, pages) + 1):
        if page > 1:
            time.sleep(1.0)  # 同花顺容易封 IP，翻页放慢
        r = requests.get(THS_URL.format(order=order, page=page), headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        if "<table" not in r.text:
            raise RuntimeError("同花顺返回内容不是数据表（可能被限制访问）")
        frames.append(pd.read_html(StringIO(r.text))[0])
    df = pd.concat(frames, ignore_index=True)
    if df.empty or not any("净额" in str(c) for c in df.columns):
        raise RuntimeError("同花顺个股资金流表格格式异常")
    return df
