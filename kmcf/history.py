"""逐日累积的关键指标（data/history.csv），用来画情绪/成交额等趋势。"""

import logging
import os
import threading
from pathlib import Path

import pandas as pd

log = logging.getLogger("kmcf")


class History:
    """一行一个交易日，列为各模块写入的指标。date 列为 'YYYY-MM-DD' 字符串。"""

    _lock = threading.Lock()

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> pd.DataFrame:
        if not self.path.exists():
            return pd.DataFrame(columns=["date"])
        try:
            df = pd.read_csv(self.path, dtype={"date": str}, encoding="utf-8-sig")
        except Exception as e:
            log.warning("history.csv 读取失败：%s", e)
            return pd.DataFrame(columns=["date"])
        return df.sort_values("date").reset_index(drop=True)

    def upsert(self, date_iso: str, metrics: dict) -> None:
        """写入/更新某个交易日的若干指标（只覆盖给出的列）。"""
        with History._lock:
            df = self.load()
            if date_iso in set(df["date"]):
                idx = df.index[df["date"] == date_iso][0]
            else:
                idx = len(df)
                df.loc[idx, "date"] = date_iso
            for k, v in metrics.items():
                if k not in df.columns:
                    df[k] = pd.NA
                df[k] = df[k].astype(object)
                df.at[idx, k] = v
            df = df.sort_values("date").reset_index(drop=True)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".csv.tmp")
            df.to_csv(tmp, index=False, encoding="utf-8-sig")
            os.replace(tmp, self.path)

    def recent(self, until_iso: str, n: int) -> pd.DataFrame:
        """截至 until_iso（含）最近 n 行。"""
        df = self.load()
        df = df[df["date"] <= until_iso]
        return df.tail(n).reset_index(drop=True)
