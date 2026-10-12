"""AI 设置与 API Key 查找。

API Key 只从环境变量读取（按 api_key_env 的顺序依次尝试），绝不写进配置文件、日志、报告或异常信息。
Windows 下当前进程环境里没有时，再读注册表里的用户/系统环境变量：
这样软件比环境变量先启动（或从任务计划启动）也能用，设置好变量后无需重启软件。
"""

import os

from ..config import DEFAULT_AI, ai_endpoint_problem, deep_merge, looks_like_secret

_REG_LOCATIONS = (
    ("HKEY_CURRENT_USER", r"Environment"),
    ("HKEY_LOCAL_MACHINE", r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
)


def _to_bool(v, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "on", "是", "开"):
            return True
        if s in ("0", "false", "no", "off", "否", "关", ""):
            return False
    return default


def _to_num(v, default: float, lo: float, hi: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return default
    return max(lo, min(hi, f))


def _str_list(v) -> list[str]:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple)):
        return []
    out = []
    for x in v:
        if isinstance(x, str) and x.strip() and x.strip() not in out:
            out.append(x.strip())
    return out


def ai_settings(cfg: dict | None) -> dict:
    """cfg['ai'] 与默认值合并并规整类型（保留用户的额外字段）。cfg 为 None 或没有 'ai' 时返回默认值。"""
    raw = (cfg or {}).get("ai") if isinstance(cfg, dict) else None
    s = deep_merge(DEFAULT_AI, raw if isinstance(raw, dict) else {})
    d = DEFAULT_AI
    for key in ("enabled", "auto_review", "include_holdings"):
        s[key] = _to_bool(s.get(key), d[key])
    for key in ("provider", "model"):
        v = s.get(key)
        s[key] = v.strip() if isinstance(v, str) and v.strip() else d[key]
    url = s.get("base_url")
    url = url.strip().rstrip("/") if isinstance(url, str) else ""
    s["base_url"] = url if url.lower().startswith(("https://", "http://")) else d["base_url"]
    models = _str_list(s.get("models")) or list(d["models"])
    if s["model"] not in models:
        models.insert(0, s["model"])
    s["models"] = models
    # 看起来像 Key 本身的“变量名”（误把 Key 粘进了配置）直接丢弃，绝不拿去显示或查找
    envs = [n for n in _str_list(s.get("api_key_env")) if not looks_like_secret(n)]
    s["api_key_env"] = envs or list(d["api_key_env"])
    s["timeout"] = _to_num(s.get("timeout"), float(d["timeout"]), 10.0, 600.0)
    s["temperature"] = _to_num(s.get("temperature"), float(d["temperature"]), 0.0, 2.0)
    s["review_budget"] = _to_num(s.get("review_budget"), float(d["review_budget"]), 20.0, 300.0)
    return s


def endpoint_problem(s: dict) -> str | None:
    """ai_settings() 结果里的接口地址 + Key 环境变量是否安全（见 config.ai_endpoint_problem）。"""
    return ai_endpoint_problem(s.get("base_url"), s.get("api_key_env"))


# ---------------------------------------------------------------- API Key

def _registry_env(name: str) -> str | None:
    """读注册表里的环境变量（先用户、后系统）；非 Windows 或读取失败返回 None。"""
    try:
        import winreg
    except ImportError:
        return None
    for hive_name, sub in _REG_LOCATIONS:
        try:
            with winreg.OpenKey(getattr(winreg, hive_name), sub) as k:
                value, typ = winreg.QueryValueEx(k, name)
        except OSError:
            continue
        if not isinstance(value, str) or not value.strip():
            continue
        if typ == winreg.REG_EXPAND_SZ:
            try:
                value = winreg.ExpandEnvironmentStrings(value)
            except OSError:
                pass
        return value
    return None


def _clean_key(value) -> str | None:
    """去掉首尾空白和引号；只接受可打印 ASCII（HTTP 头只能放这些字符），否则视为无效。"""
    if not isinstance(value, str):
        return None
    v = value.strip().strip("\"'").strip()
    if not v or any(not (33 <= ord(c) < 127) for c in v):
        return None
    return v


def find_keys(names) -> list[tuple[str, str, str]]:
    """按顺序查找 API Key，返回 [(变量名, key, 来源 'env'|'registry')]，值相同的只保留第一个。

    返回值含 key 本身，只在发请求时使用，调用方不得打印或保存。
    """
    out, seen = [], set()
    for name in _str_list(names):
        value, source = os.environ.get(name), "env"
        if not (value and value.strip()):
            value, source = _registry_env(name), "registry"
        key = _clean_key(value)
        if key and key not in seen:
            seen.add(key)
            out.append((name, key, source))
    return out


def key_status(cfg: dict | None) -> dict:
    """API Key 是否已配置（只返回变量名，绝不包含 key 的值）。"""
    s = ai_settings(cfg)
    keys = find_keys(s["api_key_env"])
    return {
        "found": bool(keys),
        "env": keys[0][0] if keys else None,
        "source": keys[0][2] if keys else None,
        "provider": s["provider"],
        "model": s["model"],
        "enabled": s["enabled"],
    }
