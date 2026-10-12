"""按股票代码查名称（自选股对话框的「补全名称」）。不依赖 Qt，在工作线程里调用。

查询顺序：本地缓存 -> 新浪行情 hq.sinajs.cn（一次请求查完所有缺失代码，1 秒内）
          -> AKShare 交易所股票名单（stock_info_a_code_name 拆成沪主板/科创板/深市/北交所四份，
             只下载需要的那几份，任何一份失败不影响其他；整份下载约 45 秒，所以放在新浪之后兜底）。
结果缓存到 data/cache/stock_names.pkl：{"names": {代码: 名称}, "fetched": {名单: 下载时间}}。
"""

import datetime as dt
import logging
import os
import re
import unicodedata
from pathlib import Path
from typing import Callable

import pandas as pd

from .. import paths

log = logging.getLogger("kmcf")

CACHE_PATH = paths.CACHE_DIR / "stock_names.pkl"
LIST_MAX_AGE = dt.timedelta(days=1)  # 名单下载后 1 天内不重复下载

# 名单 key -> 说明
LISTS = {"sh": "上交所主板", "kcb": "科创板", "sz": "深交所", "bj": "北交所"}


def list_key(code: str) -> str | None:
    """代码属于哪份交易所名单。"""
    if code.startswith(("688", "689")):
        return "kcb"
    if code.startswith("6"):
        return "sh"
    if code.startswith(("0", "3")):
        return "sz"
    if code.startswith(("4", "8", "92")):
        return "bj"
    return None


def sina_symbol(code: str) -> str | None:
    key = list_key(code)
    if key in ("sh", "kcb"):
        return "sh" + code
    if key == "sz":
        return "sz" + code
    if key == "bj":
        return "bj" + code
    return None


def clean_name(name) -> str:
    """'万  科Ａ' -> '万科A'：全角转半角、去掉空白。"""
    s = unicodedata.normalize("NFKC", str(name or ""))
    return re.sub(r"\s+", "", s)


# ---------------------------------------------------------------- 网络请求（可在测试中替换）

def fetch_list(key: str) -> dict[str, str]:
    """下载一份交易所名单 -> {代码: 名称}。"""
    import akshare as ak

    if key == "sh":
        df = ak.stock_info_sh_name_code(symbol="主板A股")
        code_col, name_col = "证券代码", "证券简称"
    elif key == "kcb":
        df = ak.stock_info_sh_name_code(symbol="科创板")
        code_col, name_col = "证券代码", "证券简称"
    elif key == "sz":
        df = ak.stock_info_sz_name_code(symbol="A股列表")
        code_col, name_col = "A股代码", "A股简称"
    elif key == "bj":
        df = ak.stock_info_bj_name_code()
        code_col, name_col = "证券代码", "证券简称"
    else:
        raise ValueError(key)
    return names_from_frame(df, code_col, name_col)


def names_from_frame(df: pd.DataFrame, code_col: str, name_col: str) -> dict[str, str]:
    out = {}
    if df is None or df.empty or code_col not in df or name_col not in df:
        return out
    codes = df[code_col].astype(str).str.extract(r"(\d+)")[0].fillna("").str.zfill(6)
    for code, name in zip(codes, df[name_col]):
        name = clean_name(name)
        if len(code) == 6 and name and name.lower() != "nan":
            out[code] = name
    return out


def fetch_sina(codes: list[str]) -> dict[str, str]:
    """新浪行情：var hq_str_sh600519="贵州茅台,..."; 返回查到的 {代码: 名称}。"""
    import requests

    symbols = [s for s in (sina_symbol(c) for c in codes) if s]
    if not symbols:
        return {}
    r = requests.get("https://hq.sinajs.cn/list=" + ",".join(symbols),
                     headers={"Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"},
                     timeout=10)
    r.raise_for_status()
    return parse_sina(r.content.decode("gb18030", errors="replace"))


def parse_sina(text: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(r'hq_str_(?:sh|sz|bj)(\d{6})="([^",]*)', text):
        name = clean_name(m.group(2))
        if name:
            out[m.group(1)] = name
    return out


# ---------------------------------------------------------------- 缓存

def load_cache(path: Path = CACHE_PATH) -> dict:
    try:
        obj = pd.read_pickle(path)
        if isinstance(obj, dict) and isinstance(obj.get("names"), dict):
            obj.setdefault("fetched", {})
            return obj
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("股票名称缓存读取失败：%s", e)
    return {"names": {}, "fetched": {}}


def save_cache(cache: dict, path: Path = CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    pd.to_pickle(cache, tmp)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 查询

def _from_sina(missing: list[str], sina_fetcher, names: dict, cache: dict, sources: list, errors: list) -> bool:
    try:
        got = sina_fetcher(missing)
    except Exception as e:
        log.warning("新浪行情查询股票名称失败：%s", e)
        errors.append("新浪行情查询失败")
        return False
    got = {c: n for c, n in (got or {}).items() if c in missing}
    if not got:
        return False
    names.update(got)
    cache["names"].update(got)
    sources.append("新浪行情")
    return True


def _from_lists(missing: list[str], list_fetcher, now: dt.datetime, names: dict, cache: dict,
                sources: list, errors: list, progress) -> bool:
    """下载缺失代码所在、且 1 天内没下载过的交易所名单。"""
    keys = []
    for c in missing:
        k = list_key(c)
        fetched = cache["fetched"].get(k)
        if k and k not in keys and (not fetched or now - fetched > LIST_MAX_AGE):
            keys.append(k)
    changed = False
    for k in keys:
        if progress:
            progress(f"正在下载{LISTS[k]}股票名单…")
        try:
            got = list_fetcher(k)
        except Exception as e:
            log.warning("股票名单下载失败（%s）：%s", LISTS[k], e)
            errors.append(f"{LISTS[k]}名单下载失败")
            continue
        if not got:
            errors.append(f"{LISTS[k]}名单为空")
            continue
        cache["names"].update(got)
        cache["fetched"][k] = now
        changed = True
    hit = False
    for c in missing:
        if c in cache["names"]:
            names[c] = cache["names"][c]
            hit = True
    if hit:
        sources.append("交易所股票名单")
    return changed


def lookup(codes: list[str], *, cache_path: Path = CACHE_PATH, now: dt.datetime | None = None,
           list_fetcher: Callable[[str], dict] = fetch_list,
           sina_fetcher: Callable[[list[str]], dict] = fetch_sina,
           progress: Callable[[str], None] | None = None) -> dict:
    """查询一批代码的名称：本地缓存 -> 新浪行情（一次请求，秒级）-> 交易所名单（AKShare，较慢）。

    新浪查不到或不可用时才下载交易所名单（上交所约 2 秒，深交所/北交所各约 20 秒）。
    返回 {"names": {代码: 名称}, "missing": [查不到的代码], "sources": [实际用到的数据源], "errors": [失败说明]}
    """
    now = now or dt.datetime.now()
    codes = list(dict.fromkeys(str(c) for c in codes if re.fullmatch(r"\d{6}", str(c))))
    cache = load_cache(cache_path)
    names: dict[str, str] = {c: cache["names"][c] for c in codes if c in cache["names"]}
    sources: list[str] = ["本地缓存"] if names else []
    errors: list[str] = []
    changed = False

    missing = [c for c in codes if c not in names and sina_symbol(c)]
    if missing:
        if progress:
            progress("正在从新浪行情查询…")
        changed |= _from_sina(missing, sina_fetcher, names, cache, sources, errors)

    missing = [c for c in codes if c not in names]
    if missing:
        changed |= _from_lists(missing, list_fetcher, now, names, cache, sources, errors, progress)

    if changed:
        try:
            save_cache(cache, cache_path)
        except Exception as e:
            log.warning("股票名称缓存写入失败：%s", e)
    return {"names": names, "missing": [c for c in codes if c not in names],
            "sources": sources, "errors": errors}
