"""用户配置（自选股/持仓等），保存在 data/config.json。"""

import copy
import json
import logging
import os
import re
from urllib.parse import urlsplit

from . import paths

log = logging.getLogger("kmcf")

# AI 分析（DeepSeek 等 OpenAI 兼容接口）。API Key 只从环境变量读取（api_key_env 依次尝试），绝不写进配置文件。
DEFAULT_AI = {
    "enabled": True,              # 总开关：关闭后不调用 AI（报告里也不生成 AI 点评）
    "provider": "DeepSeek",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "models": ["deepseek-chat", "deepseek-reasoner"],
    "api_key_env": ["DEEPSEEK_API_KEY", "DEEPSEEK-API"],
    "auto_review": True,          # 生成报告时自动写“AI 复盘点评”
    "include_holdings": False,    # 是否把持仓数量/成本/盈亏发给 AI（默认不发）
    "timeout": 120,               # 单次请求等待响应的秒数（收到第一段内容之前）
    "temperature": 0.3,
    "review_budget": 90,          # 生成报告时 AI 点评整步的总时长上限（秒，含重试），超时就跳过点评
}

DEFAULT_CONFIG = {
    # 自选股/持仓：shares=0 表示只关注不持仓；cost 为持仓成本价（元/股）
    "watchlist": [
        {"code": "600519", "name": "贵州茅台", "shares": 0, "cost": 0.0, "note": "示例，可在软件里修改"},
        {"code": "300750", "name": "宁德时代", "shares": 0, "cost": 0.0, "note": "示例"},
    ],
    "top_n": 20,              # 各类排行榜显示条数
    "schedule_time": "19:00",  # 自动复盘时间（龙虎榜一般 17:00~18:30 才完整发布）
    "ai": copy.deepcopy(DEFAULT_AI),
}


_SECRET_RUN = re.compile(r"[A-Za-z0-9]{16,}")
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def looks_like_secret(text) -> bool:
    """这个字符串看起来像 API Key 本身（而不是环境变量名）吗？

    规则：sk- 开头；或超过 64 个字符；或含有 16 位以上连续的字母数字且其中夹着数字（Key 通常是随机串，
    环境变量名一般用下划线分隔，如 DEEPSEEK_API_KEY）。用于防止用户把 Key 误填进“Key 环境变量”，
    被写进配置文件、显示在界面或日志里。
    """
    s = str(text or "").strip()
    if not s:
        return False
    if s.lower().startswith("sk-") or len(s) > 64:
        return True
    for m in _SECRET_RUN.finditer(s):
        run = m.group()
        if any(c.isdigit() for c in run) and any(c.isalpha() for c in run):
            return True
    return False


def ai_endpoint_problem(base_url: str, env_names) -> str | None:
    """接口地址和 Key 环境变量的组合是否安全；有问题返回给用户看的中文说明，否则 None。

    - http:// 只允许本机地址（否则 API Key 会明文传输）；
    - 接口地址不是 DeepSeek（也不是本机）时，不能再用 DeepSeek 默认的 Key 环境变量，
      以免把 DeepSeek 的 Key 发给其他服务器：需要为该服务单独填写环境变量名。
    """
    try:
        parts = urlsplit(str(base_url or "").strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return "接口地址格式不正确"
    if parts.scheme.lower() not in ("http", "https") or not host:
        return "接口地址需要以 https:// 开头，例如 https://api.deepseek.com"
    local = host in _LOOPBACK_HOSTS or host.endswith(".localhost")
    if parts.scheme.lower() == "http" and not local:
        return ("接口地址必须以 https:// 开头（http:// 只允许本机地址，例如 http://127.0.0.1:8000），"
                "否则 API Key 会以明文在网络上传输")
    if not local and host != "deepseek.com" and not host.endswith(".deepseek.com"):
        defaults = {n.upper() for n in DEFAULT_AI["api_key_env"]}
        used = [n for n in (env_names or []) if isinstance(n, str) and n.strip().upper() in defaults]
        if used:
            return (f"接口地址已改为 {host}，但 Key 环境变量仍是 DeepSeek 的 {'、'.join(used)}："
                    "为避免把 DeepSeek 的 API Key 发给其他服务器，请为这个服务另外设置一个环境变量，"
                    "并在「AI 设置」的“Key 环境变量”里只填写它")
    return None


def strip_ai_defaults(ai):
    """去掉 AI 设置里与默认值相同的项（保存配置时用：以后默认值更新能自动生效）。不是 dict 原样返回。"""
    if not isinstance(ai, dict):
        return ai
    return {k: copy.deepcopy(v) for k, v in ai.items() if not (k in DEFAULT_AI and DEFAULT_AI[k] == v)}


def deep_merge(base: dict, override) -> dict:
    """把 override 递归合并进 base 的副本：字典逐层合并，其他类型（含列表）整体覆盖；保留 override 里的额外字段。"""
    out = copy.deepcopy(base)
    if not isinstance(override, dict):
        return out
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _to_float(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f == f else 0.0  # NaN -> 0


def normalize_item(item) -> dict | None:
    """把一条自选股记录规整成标准格式；代码不是 6 位数字时返回 None。"""
    if not isinstance(item, dict):
        return None
    code = re.sub(r"\D", "", str(item.get("code", "")))
    if len(code) != 6:
        return None
    return {
        "code": code,
        "name": str(item.get("name", "") or "").strip(),
        "shares": _to_float(item.get("shares")),
        "cost": _to_float(item.get("cost")),
        "note": str(item.get("note", "") or "").strip(),
    }


def load_config() -> dict:
    paths.ensure_dirs()
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if paths.CONFIG_PATH.exists():
        try:
            with open(paths.CONFIG_PATH, encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                cfg.update(user)
        except (OSError, ValueError) as e:  # ValueError 含 JSONDecodeError / UnicodeDecodeError
            log.warning("配置文件读取失败，使用默认配置：%s", e)
            cfg["_config_error"] = str(e)[:120]
    else:
        save_config(cfg)

    items, seen = [], set()
    for raw in cfg.get("watchlist") or []:
        item = normalize_item(raw)
        if item and item["code"] not in seen:
            seen.add(item["code"])
            items.append(item)
    cfg["watchlist"] = items
    cfg["ai"] = deep_merge(DEFAULT_AI, cfg.get("ai"))  # 用户只写了部分 AI 设置时，其余用默认值
    try:
        cfg["top_n"] = max(5, min(100, int(cfg.get("top_n", 20))))
    except (TypeError, ValueError):
        cfg["top_n"] = 20
    return cfg


def save_config(cfg: dict) -> None:
    cfg = {k: v for k, v in cfg.items() if not str(k).startswith("_")}  # _ 开头是运行时内部字段
    if "ai" in cfg:
        # load_config 把默认 AI 设置合并进了 cfg['ai']：只写用户改过的项，免得默认值被固化在文件里
        ai = strip_ai_defaults(cfg["ai"])
        if isinstance(ai, dict) and not ai:
            del cfg["ai"]
        else:
            cfg["ai"] = ai
    paths.ensure_dirs()
    tmp = paths.CONFIG_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, paths.CONFIG_PATH)
