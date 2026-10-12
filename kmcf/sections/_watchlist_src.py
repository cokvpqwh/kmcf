"""自选股模块的数据源辅助（私有，不加入 SECTIONS）。

- 代码 → 市场、涨跌停幅度规则
- 腾讯证券日K（东方财富 push2his 不可用时的备用源），返回与 stock_zh_a_hist 相同的列名
- 新浪 / 腾讯行情接口批量查股票简称

这里的联网函数都应通过 ctx.fetch 调用（缓存/重试/限速），本身只负责一次请求与解析。
"""

import datetime as dt
import functools
import json
import re
import threading

import numpy as np
import pandas as pd
import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
TIMEOUT = 12
ST_10PCT_FROM = dt.date(2025, 7, 7)   # 沪深主板风险警示（ST/*ST）股涨跌幅限制自该日起由 5% 调整为 10%
TX_KLINE_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
SINA_HQ_URL = "https://hq.sinajs.cn/list="
TX_QT_URL = "https://qt.gtimg.cn/q="

# 与 akshare.stock_zh_a_hist 一致的列（成交量：手；成交额：元；涨跌幅、换手率：%）
KLINE_COLS = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额", "涨跌幅", "换手率"]


# ---------------------------------------------------------------- 代码规则

def market_of(code: str) -> str | None:
    """6 位代码 → 'sh' / 'sz' / 'bj'。6 开头沪市，0/3 开头深市，4/8/9 开头北交所（900 为沪市B股）。
    另外兼容场内基金：5 开头沪市、1 开头深市。"""
    code = str(code).strip().zfill(6)
    if not code.isdigit() or len(code) != 6:
        return None
    if code.startswith("900"):
        return "sh"
    if code.startswith("200"):
        return "sz"
    head = code[0]
    if head in "65":
        return "sh"
    if head in "031":
        return "sz"
    if head in "489":
        return "bj"
    return None


def market_label(code: str) -> str:
    return {"sh": "沪市", "sz": "深市", "bj": "北交所"}.get(market_of(code) or "", "未知")


def em_supported(code: str) -> bool:
    """akshare.stock_zh_a_hist 的市场判断是“6 开头沪市，其余深市”，只对沪深京 A 股正确。"""
    code = str(code).zfill(6)
    if code.startswith(("900", "200")):
        return False
    return code[0] in "036" or market_of(code) == "bj"


def limit_ratios(code: str, name: str = "", trade_date: dt.date | None = None) -> tuple[float, ...]:
    """涨跌停幅度：北交所 30%，创业板/科创板 20%，主板 10%；
    主板 ST 股 2025-07-07 起为 10%，此前为 5%（trade_date 为空按现行规则）。"""
    code = str(code).zfill(6)
    if market_of(code) == "bj":
        return (0.30,)
    if code.startswith(("30", "688", "689")):
        return (0.20,)
    if "ST" in str(name).upper() and trade_date is not None and trade_date < ST_10PCT_FROM:
        return (0.05,)
    return (0.10,)


# ---------------------------------------------------------------- 网络

def _get_text(url: str, params, headers: dict, trust_env: bool) -> str:
    with requests.Session() as s:
        s.trust_env = trust_env
        r = s.get(url, params=params, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = "gbk"
        return r.text


def http_get(url: str, params: dict | None = None, headers: dict | None = None) -> str:
    """国内行情域名先直连（与 kmcf.net 的直连策略一致）。
    直连连不上（不是超时）且系统为该地址配置了代理时，再走系统代理试一次；超时直接抛出，不换路再等一遍。"""
    hdrs = {"User-Agent": UA, **(headers or {})}
    try:
        return _get_text(url, params, hdrs, trust_env=False)
    except requests.Timeout:
        raise
    except requests.ConnectionError:
        if not requests.utils.get_environ_proxies(url):
            raise
    return _get_text(url, params, hdrs, trust_env=True)


def with_timeout(func, seconds: float):
    """给没有设超时的 akshare 函数（涨停板行情等）加总超时，在线程里执行。
    保留函数名（缓存键与直接调用一致，可与其他模块共享缓存）；同一个包装对象超时一次后，
    ctx.fetch 的后续重试立即失败，不再每次等满超时。"""
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

        th = threading.Thread(target=run, name=f"kmcf-wl-{func.__name__}", daemon=True)
        th.start()
        th.join(seconds)
        if th.is_alive():
            timed_out.append(True)
            raise TimeoutError(f"{func.__name__} 超过 {seconds:.0f} 秒未返回")
        if "e" in box:
            raise box["e"]
        return box["v"]
    return wrapper


def _json_tail(text: str) -> dict:
    """去掉 'kline_dayqfq=' 之类的 JS 变量前缀。"""
    i = text.find("{")
    if i < 0:
        raise ValueError("返回内容不是 JSON")
    return json.loads(text[i:])


# ---------------------------------------------------------------- 腾讯日K（备用源）

def tx_stock_daily(symbol: str, start_date: str, end_date: str, adjust: str = "qfq") -> pd.DataFrame:
    """腾讯证券个股日K。symbol 为 6 位代码，日期 'YYYYMMDD'，adjust: 'qfq' 或 ''（不复权）。

    返回列与 stock_zh_a_hist 一致，另加 股票代码、名称（名称取自腾讯当前行情）。代码不存在时返回空表。
    """
    code = str(symbol).zfill(6)
    mk = market_of(code)
    if mk is None:
        return pd.DataFrame()
    sym = mk + code
    start = dt.datetime.strptime(start_date, "%Y%m%d").date()
    end = dt.datetime.strptime(end_date, "%Y%m%d").date()
    # 接口按“截至 end 的最近 count 根”返回，count 取区间内工作日数即可覆盖
    count = int(np.busday_count(start, end + dt.timedelta(days=1))) + 5
    param = f"{sym},day,{start.isoformat()},{end.isoformat()},{count},{adjust}"
    js = _json_tail(http_get(TX_KLINE_URL, params={"param": param}))
    if str(js.get("code", 0)) != "0":
        raise ValueError(f"腾讯日K返回错误：{js.get('msg')}")
    data = js.get("data")
    node = data.get(sym) if isinstance(data, dict) else None
    if not isinstance(node, dict):
        return pd.DataFrame()
    rows = node.get(f"{adjust}day") or node.get("day") or []
    qt = node.get("qt")
    qt = qt.get(sym) if isinstance(qt, dict) else None
    name = str(qt[1]).strip() if isinstance(qt, list) and len(qt) > 1 else ""
    df = parse_tx_rows(rows, code, prec=node.get("prec"))
    if df.empty:
        return df
    df = df[(df["日期"] >= start) & (df["日期"] <= end)].reset_index(drop=True)
    return df.assign(名称=name)


def parse_tx_rows(rows: list, code: str, prec=None) -> pd.DataFrame:
    """解析腾讯 newfqkline 行：[日期, 开, 收, 高, 低, 成交量, {}, 换手率%, 成交额(万元), ...]。"""
    recs = []
    for r in rows or []:
        if not isinstance(r, (list, tuple)) or len(r) < 6:
            continue
        recs.append({
            "日期": r[0], "开盘": r[1], "收盘": r[2], "最高": r[3], "最低": r[4], "成交量": r[5],
            "换手率": r[7] if len(r) > 7 else None,
            "成交额": r[8] if len(r) > 8 else None,
        })
    if not recs:
        return pd.DataFrame()
    df = pd.DataFrame(recs)
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce").dt.date
    for c in ("开盘", "收盘", "最高", "最低", "成交量", "换手率", "成交额"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["日期", "收盘"]).sort_values("日期").drop_duplicates("日期", keep="last")
    df = df.reset_index(drop=True)
    code = str(code).zfill(6)
    if code.startswith(("688", "689")):  # 腾讯科创板成交量单位是股，统一成手
        df["成交量"] = df["成交量"] / 100
    df["成交额"] = df["成交额"] * 1e4  # 万元 -> 元
    prev = df["收盘"].shift(1)
    p0 = pd.to_numeric(pd.Series([prec]), errors="coerce").iloc[0]
    if len(df) and pd.notna(p0) and p0 > 0:
        prev.iloc[0] = p0
    df["涨跌幅"] = (df["收盘"] / prev - 1) * 100
    df["股票代码"] = code
    return df[KLINE_COLS + ["股票代码"]]


# ---------------------------------------------------------------- 名称补全

def sina_names(codes: str) -> pd.DataFrame:
    """新浪行情批量查简称。codes：逗号分隔的 6 位代码。返回 [code, name]。"""
    syms = [market_of(c) + c for c in codes.split(",") if c and market_of(c)]
    if not syms:
        return pd.DataFrame(columns=["code", "name"])
    text = http_get(SINA_HQ_URL + ",".join(syms), headers={"Referer": "https://finance.sina.com.cn/"})
    return parse_sina_names(text)


def parse_sina_names(text: str) -> pd.DataFrame:
    out = []
    for m in re.finditer(r'hq_str_(?:sh|sz|bj)(\d{6})="([^"]*)"', text):
        name = m.group(2).split(",")[0].strip()
        if name:
            out.append({"code": m.group(1), "name": name})
    return pd.DataFrame(out, columns=["code", "name"])


def tx_names(codes: str) -> pd.DataFrame:
    """腾讯行情批量查简称。codes：逗号分隔的 6 位代码。返回 [code, name]。"""
    syms = [market_of(c) + c for c in codes.split(",") if c and market_of(c)]
    if not syms:
        return pd.DataFrame(columns=["code", "name"])
    return parse_tx_names(http_get(TX_QT_URL + ",".join(syms)))


def parse_tx_names(text: str) -> pd.DataFrame:
    out = []
    for m in re.finditer(r'v_(?:sh|sz|bj)(\d{6})="([^"]*)"', text):
        parts = m.group(2).split("~")
        if len(parts) > 2 and parts[1].strip():
            out.append({"code": m.group(1), "name": parts[1].strip()})
    return pd.DataFrame(out, columns=["code", "name"])
