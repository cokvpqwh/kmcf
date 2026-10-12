"""提示词与对话组装。AI 只做客观数据分析与解读，系统提示词明确禁止投资建议和涨跌预测。"""

from .context import history_context, report_context

DISCLAIMER = "AI 生成，仅供参考，不构成投资建议"
PROMPT_VERSION = "2026-10-12.2"   # 改动提示词时更新，AI 点评缓存随之失效

SYSTEM_PROMPT = """你是 kmcf 软件内置的“A股复盘数据分析助手”。用户每天收盘后用 kmcf 生成复盘报告，你根据报告数据帮用户理解当天市场发生了什么。

【数据依据】
1. 只依据下方提供的“复盘报告数据”和“近期历史指标”回答，结论要引用具体数字（指数涨跌幅、成交额、涨跌家数、涨跌停家数、炸板率、连板高度、板块涨跌幅、龙虎榜和资金净额等）。
2. 数据里没有的内容，直接说明“报告数据中没有这项信息”；不要编造或猜测新闻、政策、公告、传闻、公司基本面，也不要把数据之外的原因说成事实。可以客观指出数据之间的对应关系（如涨停股集中在哪些行业、成交额较前一日的变化）。
3. 注意数据口径说明（数据源、统计范围、两融 T+1 披露等），不同来源、不同口径的数字不要混在一起比较。

【合规要求（必须遵守）】
4. 只做客观的数据分析与解读，不提供任何投资建议，也不预测走势。禁止出现：
   - 买入、卖出、加仓、减仓、建仓、清仓、补仓、持有、止损、止盈、抄底、低吸、追涨等操作建议，以及仓位安排；
   - 目标价、支撑位、压力位等价格判断；
   - 推荐或暗示个股、板块（如“建议关注”“值得买入”“可以布局”“重点关注”）；
   - 对明天或未来涨跌的预测（如“有望上涨”“大概率反弹”“后市看好”“明日继续走强”）。
5. 如果用户询问买卖时机、仓位、能否持有、明天走势、推荐股票等，先礼貌说明你只做客观数据分析、不提供投资建议或预测，再给出与问题相关的客观数据。
6. 描述资金和情绪时用中性措辞（如“净流入”“涨停家数较前一日减少”），不使用煽动性、诱导性的词语。

【输出格式】
7. 用简体中文和 Markdown 回答，简洁分点；先给一两句概括，再列要点；数字沿用报告里的写法和单位。
8. 不需要写免责声明，软件界面会统一注明。"""

REVIEW_PROMPT = """请根据上面的复盘报告数据，写一段当日 A 股复盘点评，要求：
1. 全文 250~450 字（数字和标点也计入），宁短勿长；
2. 先用 2~3 句话（合计不超过 120 字）概述当日市场整体特征（指数、成交额、涨跌家数、涨跌停与情绪），再列出 3~5 个要点，每个要点用“- ”开头、不超过 70 字，要点之间不空行；
3. 要点从板块轮动、短线情绪（涨停/连板/炸板）、龙虎榜与资金等方面挑选当天最突出的事实，每个要点都引用报告中的具体数字，可以和近期历史指标对比；
4. 不要标题、不要表格、不要加粗、不要免责声明；
5. 只描述已经发生的数据事实，不给任何投资建议，不预测后市。"""

REWRITE_PROMPT = """你上面的点评包含了投资建议或预测类表述（{terms}），违反了要求。请删掉这些内容，只保留对已发生数据的客观描述，按原来的格式和字数要求重新输出完整点评。"""

QUICK_PROMPTS: list[tuple[str, str]] = [
    ("总结今日市场", "请总结今天 A 股市场的整体表现：主要指数涨跌、两市成交额变化、涨跌家数和涨跌停情况，并概括今天市场的主要特征。"),
    ("板块轮动分析", "请分析今天的板块轮动：哪些行业和概念领涨、领跌，涨停股集中在哪些行业，行业资金净流入/净流出居前的是哪些，强势行业近几日的持续性如何。"),
    ("龙虎榜与资金解读", "请解读今天的龙虎榜与资金数据：龙虎榜整体净买入情况、机构席位动向、活跃营业部、个股主力资金净流入/净流出居前的股票，以及北向成交额和两融余额的变化。"),
    ("短线情绪分析", "请从涨停家数、跌停家数、炸板率、最高连板与连板晋级率、昨日涨停股今日表现等指标分析今天的短线情绪，并和最近几个交易日对比。"),
    ("我的自选股表现", "请分析我的自选股今天的表现：各自涨跌幅、均线位置、量比与成交变化，是否出现涨停、跌停、炸板、上龙虎榜、创新高/新低等情况。"),
    ("对比近几日变化", "请对比最近几个交易日的关键指标（两市成交额、涨跌家数、涨跌停家数、炸板率、最高连板、龙虎榜和机构净买入等），说明今天和前几天相比有哪些明显变化。"),
]

MAX_HISTORY_MESSAGES = 16      # 带上最近几轮对话
MAX_HISTORY_MESSAGE_CHARS = 6000


def system_message(report: dict | None, *, include_holdings: bool = False, history_rows: int = 20,
                   max_chars: int = 15000) -> str:
    """系统提示词 + 报告数据 + 近期历史指标。"""
    parts = [SYSTEM_PROMPT, "", "=" * 12 + " 复盘报告数据 " + "=" * 12]
    ctx = report_context(report, include_holdings=include_holdings, max_chars=max_chars) if report else ""
    parts.append(ctx or "（当前没有打开的复盘报告，无法提供当天的数据。）")
    until = str(report.get("trade_date") or "")[:10] if isinstance(report, dict) else None
    hist = history_context(history_rows, until or None, include_holdings=include_holdings)
    if hist:
        parts += ["", "=" * 12 + " 近期历史指标 " + "=" * 12, hist]
    return "\n".join(parts)


def build_messages(question: str, report: dict | None, cfg: dict | None, *,
                   history: list[dict] | None = None, include_holdings: bool = False) -> list[dict]:
    """组装对话：[system(提示词 + 报告上下文 + 近期历史), *之前的对话, user(问题)]。"""
    msgs = [{"role": "system", "content": system_message(report, include_holdings=include_holdings)}]
    prev = []
    for m in history or []:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) \
                and m["content"].strip():
            text = m["content"]
            if len(text) > MAX_HISTORY_MESSAGE_CHARS:
                text = text[:MAX_HISTORY_MESSAGE_CHARS] + "……"
            prev.append({"role": m["role"], "content": text})
    prev = prev[-MAX_HISTORY_MESSAGES:]
    while prev and prev[0]["role"] != "user":   # 截断后从用户的提问开始
        prev.pop(0)
    msgs += prev
    msgs.append({"role": "user", "content": str(question or "").strip() or "请总结今天的市场。"})
    return msgs
