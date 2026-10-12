"""AI 分析入口（默认 DeepSeek，OpenAI 兼容接口）。

AI 只做客观的数据分析与解读，不提供投资建议；所有 AI 输出处都要注明 DISCLAIMER。
API Key 只从环境变量读取（默认依次尝试 DEEPSEEK_API_KEY、DEEPSEEK-API），绝不写入配置、日志、报告或异常信息。
持仓数量/成本/盈亏默认不发送给 AI（ai.include_holdings=False）。

    from kmcf import ai
    ai.key_status(cfg)                                    # {'found', 'env', 'provider', 'model', ...}，不含 Key
    msgs = ai.build_messages("总结今天的市场", report, cfg)
    text = ai.chat(msgs, cfg, stream=True, on_delta=print, cancel=event)   # text.finish_reason：'stop'/'length'/...
"""

from .client import AIError, AIText, chat
from .compliance import FORBIDDEN_PATTERNS, FORBIDDEN_TERMS, compliance_hits
from .context import history_context, report_context
from .prompts import DISCLAIMER, QUICK_PROMPTS, REVIEW_PROMPT, SYSTEM_PROMPT, build_messages
from .review import markdown_blocks, review_for_report
from .settings import ai_settings, endpoint_problem, key_status

__all__ = [
    "ai_settings", "key_status", "AIError", "chat", "report_context", "history_context", "build_messages",
    "SYSTEM_PROMPT", "QUICK_PROMPTS", "review_for_report",
    # 附加
    "DISCLAIMER", "REVIEW_PROMPT", "FORBIDDEN_TERMS", "FORBIDDEN_PATTERNS", "compliance_hits", "markdown_blocks",
    "AIText", "endpoint_problem",
]
