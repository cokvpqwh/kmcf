"""带磁盘缓存、重试、限速、超时的 AKShare 调用器。

同一个交易日、同一个函数 + 参数只会真正请求一次，结果存到 data/raw/YYYYMMDD/。
这样重复生成报告很快，而且“实时快照”类数据在事后也能复现当天的报告。
"""

import functools
import hashlib
import logging
import os
import re
import threading
import time
from pathlib import Path

import pandas as pd

log = logging.getLogger("kmcf")

DEFAULT_TIMEOUT = 60.0  # 单次请求总时长上限（秒）。很多 akshare 函数调用 requests 时没有设超时


class FetchError(RuntimeError):
    pass


class SnapshotUnavailable(FetchError):
    """接口只提供最新快照，而当前时刻的快照不代表目标交易日。"""


class NoRetry(FetchError):
    """数据源函数主动抛出：重试也没用（如数据日期对不上、尚未发布、格式不对），立即失败且不缓存。"""


class FetchTimeout(FetchError):
    """单次请求超过时限。卡住的请求重试通常也会卡住，所以不再重试。"""


def cache_key(func, args, kwargs) -> str:
    name = getattr(func, "__name__", "call")
    parts = [str(a) for a in args] + [f"{k}={v}" for k, v in sorted(kwargs.items())]
    if not parts:
        return name
    raw = "_".join(parts)
    safe = re.sub(r"[^0-9A-Za-z一-鿿=.-]+", "-", raw)[:60]
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]
    return f"{name}__{safe}__{digest}"


def run_with_timeout(func, args: tuple, kwargs: dict, seconds: float | None):
    """在后台线程执行 func，超过 seconds 秒抛 FetchTimeout（卡住的线程是守护线程，结果被丢弃）。"""
    if not seconds:
        return func(*args, **kwargs)
    box: dict = {}

    def target():
        try:
            box["v"] = func(*args, **kwargs)
        except BaseException as e:  # noqa: BLE001  原样转交给调用方
            box["e"] = e

    th = threading.Thread(target=target, name=f"kmcf-{getattr(func, '__name__', 'call')}", daemon=True)
    th.start()
    th.join(seconds)
    if th.is_alive():
        msg = f"{getattr(func, '__name__', 'call')} 超过 {seconds:.0f} 秒未返回"
        raise FetchTimeout(msg) from TimeoutError(msg)  # cause 链上有 TimeoutError，模块可据此判断为连接级失败
    if "e" in box:
        raise box["e"]
    return box.get("v")


def with_timeout(func, seconds: float = DEFAULT_TIMEOUT):
    """包装一个函数使其带总超时，保留函数名（因此缓存键与直接调用相同）。"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return run_with_timeout(func, args, kwargs, seconds)
    return wrapper


class Fetcher:
    _lock = threading.Lock()
    _last_call = 0.0

    def __init__(self, raw_dir: Path, snapshot_ok: bool, force: bool = False,
                 retries: int = 3, min_interval: float = 0.35, same_day: bool = False,
                 timeout: float | None = DEFAULT_TIMEOUT):
        self.raw_dir = Path(raw_dir)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_ok = snapshot_ok
        self.force = force
        self.same_day = same_day  # 复盘日就是今天且还没到晚上，盘后数据（龙虎榜等）可能仍在陆续发布
        self.retries = retries
        self.min_interval = min_interval
        self.timeout = timeout
        self.stats = {"network": 0, "cache": 0, "failed": 0}

    # ------------------------------------------------------------ 缓存
    def cache_path(self, func, *args, **kwargs) -> Path:
        return self.raw_dir / f"{cache_key(func, args, kwargs)}.pkl"

    def _cache_readable(self, path: Path, snapshot: bool, volatile: bool) -> bool:
        skip_cache = self.force or (volatile and self.same_day)
        # 快照接口在快照已失效时，即使 force 也只能用缓存
        return path.exists() and (not skip_cache or (snapshot and not self.snapshot_ok))

    def has_cache(self, func, *args, snapshot: bool = False, volatile: bool = False, **kwargs) -> bool:
        """这次调用会不会直接读缓存（不发网络请求）。"""
        return self._cache_readable(self.cache_path(func, *args, **kwargs), snapshot, volatile)

    def drop_cache(self, func, *args, **kwargs) -> None:
        try:
            self.cache_path(func, *args, **kwargs).unlink(missing_ok=True)
        except OSError as e:
            log.warning("删除缓存失败：%s", e)

    def _load(self, path: Path):
        obj = pd.read_pickle(path)
        self.stats["cache"] += 1
        return obj

    def _save(self, path: Path, obj) -> None:
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            pd.to_pickle(obj, tmp)
            os.replace(tmp, path)
        except Exception as e:
            log.warning("缓存写入失败 %s：%s", path.name, e)
            tmp.unlink(missing_ok=True)

    def _throttle(self) -> None:
        with Fetcher._lock:
            wait = Fetcher._last_call + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            Fetcher._last_call = time.monotonic()

    # ------------------------------------------------------------ 调用
    def call(self, func, *args, snapshot: bool = False, cache: bool = True, volatile: bool = False,
             accept=None, timeout: float | None = None, retries: int | None = None, **kwargs):
        """调用 AKShare 函数（或自己写的取数函数）。

        snapshot=True：接口只返回“最新实时快照”（不能按日期查询）。此刻快照不代表目标交易日且无缓存时抛 SnapshotUnavailable。
        volatile=True：盘后陆续发布的数据（如龙虎榜），复盘日当天 20:00 前不读缓存，每次重新请求。
        accept=callable(result)->bool：结果不合格（如空表、缺少复盘日）时照常返回但不写缓存，下次重新请求。
        timeout：单次请求秒数上限（默认 60）；超时不再重试。retries：覆盖默认重试次数。
        func 抛出 NoRetry 时立即失败、不重试。重试后仍失败抛 FetchError；
        若是 force/volatile 跳过了已有缓存而重新请求失败，则退回使用旧缓存并记日志。
        空 DataFrame 是正常结果，原样返回。
        """
        name = getattr(func, "__name__", "call")
        path = self.cache_path(func, *args, **kwargs)

        if cache and self._cache_readable(path, snapshot, volatile):
            try:
                return self._load(path)
            except Exception as e:
                log.warning("缓存读取失败 %s：%s", path.name, e)

        if snapshot and not self.snapshot_ok:
            raise SnapshotUnavailable(
                f"{name} 只提供最新实时快照，当前时刻的数据不对应该交易日的收盘（盘中运行或历史日期），已跳过")

        tries = max(1, retries if retries is not None else self.retries)
        limit = self.timeout if timeout is None else timeout
        last_exc: Exception | None = None
        for attempt in range(1, tries + 1):
            self._throttle()
            try:
                result = run_with_timeout(func, args, kwargs, limit)
                if result is None:
                    raise FetchError("接口返回 None")
            except (NoRetry, FetchTimeout) as e:
                last_exc = e
                log.warning("%s 请求失败（不重试）：%s", name, e)
                break
            except Exception as e:
                last_exc = e
                log.warning("%s 第 %d 次请求失败：%s", name, attempt, e)
                if attempt < tries:
                    time.sleep(1.5 * attempt)
                continue
            self.stats["network"] += 1
            if cache and (accept is None or _accepted(accept, result)):
                self._save(path, result)
            return result

        if cache and path.exists():  # force / volatile 时跳过了旧缓存，重新请求失败就退回旧缓存
            try:
                obj = self._load(path)
                log.warning("%s 重新请求失败，改用之前的缓存：%s", name, last_exc)
                return obj
            except Exception:
                pass
        self.stats["failed"] += 1
        if isinstance(last_exc, FetchError) and not type(last_exc) is FetchError:
            raise last_exc  # NoRetry / FetchTimeout 等保持原类型，便于调用方区分
        raise FetchError(f"{name} 获取失败（已尝试 {attempt} 次）：{last_exc}") from last_exc


def _accepted(accept, result) -> bool:
    try:
        return bool(accept(result))
    except Exception as e:
        log.warning("accept 检查出错，结果不缓存：%s", e)
        return False
