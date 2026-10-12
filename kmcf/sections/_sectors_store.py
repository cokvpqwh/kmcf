"""板块轮动：逐日累积的板块涨跌幅（data/cache/sector_daily.csv）。

东方财富板块历史接口依赖 push2，同花顺历史需逐个板块请求，因此每天把当日完整的行业/概念排行
写入本地，近 5 日热力图、上榜次数等优先从这里读，不额外联网。

长表格式：date(YYYY-MM-DD), kind(industry/concept), source(em/ths/sina), name, pct, net, rank, full
full=1 表示来自当日完整排行（rank 有效）；full=0 为个别板块补齐的历史涨跌幅（rank 为空）。
"""

import logging
import os
import threading
from pathlib import Path

import pandas as pd

from .. import paths

log = logging.getLogger("kmcf")

COLUMNS = ["date", "kind", "source", "name", "pct", "net", "rank", "full"]
KEEP_DATES = 120  # 只保留最近这么多个交易日


class SectorStore:
    _lock = threading.Lock()

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else paths.CACHE_DIR / "sector_daily.csv"

    def load(self) -> pd.DataFrame:
        if not self.path.exists():
            return pd.DataFrame(columns=COLUMNS)
        try:
            df = pd.read_csv(self.path, dtype={"date": str, "kind": str, "source": str, "name": str},
                             encoding="utf-8-sig")
        except Exception as e:
            log.warning("sector_daily.csv 读取失败：%s", e)
            return pd.DataFrame(columns=COLUMNS)
        for c in COLUMNS:
            if c not in df.columns:
                df[c] = pd.NA
        return df[COLUMNS]

    def _write(self, df: pd.DataFrame) -> None:
        dates = sorted(df["date"].dropna().unique())
        if len(dates) > KEEP_DATES:
            df = df[df["date"].isin(dates[-KEEP_DATES:])]
        df = df.sort_values(["date", "kind", "source", "full", "rank"], ascending=[True, True, True, False, True])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".csv.{os.getpid()}.tmp")
        df.to_csv(tmp, index=False, encoding="utf-8-sig")
        os.replace(tmp, self.path)

    def upsert_day(self, date_iso: str, kind: str, source: str, boards: pd.DataFrame) -> None:
        """写入某日完整排行（覆盖该日同类同源的全部记录）。boards 需含 name/pct，可含 net。"""
        if boards is None or boards.empty:
            return
        d = boards[boards["pct"].notna()].sort_values("pct", ascending=False).reset_index(drop=True)
        new = pd.DataFrame({
            "date": date_iso, "kind": kind, "source": source, "name": d["name"].astype(str).values,
            "pct": d["pct"].round(4).values,
            "net": d["net"].values if "net" in d.columns else pd.NA,
            "rank": range(1, len(d) + 1), "full": 1,
        })
        with SectorStore._lock:
            old = self.load()
            keep = ~((old["date"] == date_iso) & (old["kind"] == kind) & (old["source"] == source))
            self._write(pd.concat([old[keep], new], ignore_index=True) if keep.any() else new)

    def add_partial(self, kind: str, source: str, name: str, pcts: dict) -> None:
        """补充个别板块的历史涨跌幅 {date_iso: pct}；不覆盖已有记录。"""
        if not pcts:
            return
        with SectorStore._lock:
            old = self.load()
            mine = old[(old["kind"] == kind) & (old["source"] == source) & (old["name"] == name)]
            have = set(mine["date"])
            rows = [{"date": d, "kind": kind, "source": source, "name": name, "pct": round(float(p), 4),
                     "net": pd.NA, "rank": pd.NA, "full": 0}
                    for d, p in pcts.items() if d not in have and p is not None]
            if rows:
                self._write(pd.concat([old, pd.DataFrame(rows, columns=COLUMNS)], ignore_index=True))

    def matrix(self, kind: str, source: str, dates: list[str], value: str = "pct",
               full_only: bool = False) -> pd.DataFrame:
        """行=板块，列=dates（缺失的日期为整列 NaN）。"""
        df = self.load()
        df = df[(df["kind"] == kind) & (df["source"] == source) & (df["date"].isin(dates))]
        if full_only:
            df = df[pd.to_numeric(df["full"], errors="coerce") == 1]
        if df.empty:
            return pd.DataFrame(columns=dates, dtype=float)
        df = df.assign(v=pd.to_numeric(df[value], errors="coerce"))
        m = df.pivot_table(index="name", columns="date", values="v", aggfunc="first")
        return m.reindex(columns=dates).astype(float)

    def full_dates(self, kind: str, source: str, dates: list[str]) -> list[str]:
        df = self.load()
        df = df[(df["kind"] == kind) & (df["source"] == source) & (df["date"].isin(dates))
                & (pd.to_numeric(df["full"], errors="coerce") == 1)]
        return sorted(set(df["date"]))

    def day(self, kind: str, date_iso: str) -> tuple[pd.DataFrame, str | None]:
        """某日的完整排行（任一数据源，优先 em > ths > sina）。"""
        df = self.load()
        df = df[(df["kind"] == kind) & (df["date"] == date_iso) & (pd.to_numeric(df["full"], errors="coerce") == 1)]
        for src in ("em", "ths", "sina"):
            d = df[df["source"] == src]
            if not d.empty:
                d = d.assign(pct=pd.to_numeric(d["pct"], errors="coerce"), net=pd.to_numeric(d["net"], errors="coerce"))
                return d.sort_values("pct", ascending=False).reset_index(drop=True), src
        return pd.DataFrame(columns=COLUMNS), None
