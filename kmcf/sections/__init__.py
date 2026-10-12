"""复盘模块注册表。报告按这里的顺序展示。

每个模块需要提供 ID、TITLE 和 build(ctx: Context) -> Section。
"""

from . import funds, market, sectors, watchlist

SECTIONS = [market, sectors, funds, watchlist]
