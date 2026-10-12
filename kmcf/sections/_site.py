"""站点失败标记（ctx.shared 里的 xxx_down）的共用文案。

push2_down、push2his_down 会跨次生成保留 3 小时（见 report.PERSIST_SITE_FLAGS / SITE_FLAG_TTL），
其余标记只在同一次生成内有效。

报告里只写“暂时无法访问”：跳过的具体原因（本次生成中已失败 / 3 小时内不再尝试）是运行时状态，
只写日志，不进报告（报告会被保存、分享，过后再看这些状态没有意义）。

这里不直接 import report（report 导入各模块，会循环导入），常量的一致性由单元测试核对。
"""

import logging

log = logging.getLogger("kmcf")

PERSIST_FLAGS = frozenset({"push2_down", "push2his_down"})   # = report.PERSIST_SITE_FLAGS
PERSIST_HOURS = 3                                            # = report.SITE_FLAG_TTL（小时）
UNAVAILABLE = "暂时无法访问"


def persistent(flag: str) -> bool:
    return flag in PERSIST_FLAGS


def skip_detail(flag: str) -> str:
    """日志用的跳过原因，如“近期连接失败，3 小时内不再尝试”。"""
    if persistent(flag):
        return f"近期连接失败，{PERSIST_HOURS} 小时内不再尝试"
    return "本次生成中已连接失败，不再重复请求"


def skip_reason(flag: str) -> str:
    """站点已标记失败、且没有可用缓存而不再请求时，报告里的说明（“暂时无法访问”）；具体原因写日志。"""
    log.info("跳过请求（%s）：%s", flag, skip_detail(flag))
    return UNAVAILABLE


def skip_brief(flag: str) -> str:
    """放在括号里的简短说明（不再嵌套括号），如“东方财富（暂时无法访问）”。"""
    log.info("跳过请求（%s）：%s", flag, skip_detail(flag))
    return UNAVAILABLE
