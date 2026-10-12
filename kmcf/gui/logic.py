"""界面用到的纯逻辑：不依赖 Qt，便于单元测试。"""

import datetime as dt
import json
import logging
import math
import os
import re
import traceback
from pathlib import Path
from typing import Callable

from .. import paths
from ..config import (ai_endpoint_problem, load_config, looks_like_secret, normalize_item, save_config,
                      strip_ai_defaults)

log = logging.getLogger("kmcf")

WEEKDAYS = "一二三四五六日"
FIELDS = ("code", "name", "shares", "cost", "note")
LOG_FILE = paths.LOG_DIR / "kmcf.log"
DEFAULT_SCHEDULE_TIME = "19:00"  # 用户要求：每个工作日晚上 7 点自动复盘


# ---------------------------------------------------------------- 日期显示

def weekday_cn(d: dt.date) -> str:
    return "周" + WEEKDAYS[d.weekday()]


def report_label(d: dt.date) -> str:
    """历史列表里的显示文字：2026-10-09 周五"""
    return f"{d.isoformat()} {weekday_cn(d)}"


def format_run_time(text: str) -> str:
    """'2026-10-12 19:00' -> '10-12 周一 19:00'；解析不了就原样返回。"""
    try:
        t = dt.datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return str(text or "")
    return f"{t:%m-%d} {weekday_cn(t.date())} {t:%H:%M}"


# ---------------------------------------------------------------- 自选股表格

_CODE_RE = re.compile(r"^(?:sh|sz|bj)?(\d{6})(?:\.(?:sh|ss|sz|bj))?$", re.I)


def parse_code(text) -> str | None:
    """'600519' / 'sh600519' / '600519.SH' -> '600519'；不合法返回 None。"""
    s = re.sub(r"\s+", "", str(text or ""))
    m = _CODE_RE.match(s)
    return m.group(1) if m else None


def parse_amount(text) -> float | None:
    """股数/成本价：空 -> 0；非负数字 -> float；其他 -> None（不合法）。"""
    s = re.sub(r"[\s,，]", "", str(text or ""))
    if not s:
        return 0.0
    try:
        v = float(s)
    except ValueError:
        return None
    if not math.isfinite(v) or v < 0:
        return None
    return v


def fmt_amount(v) -> str:
    """表格里显示数字：0 显示为空，整数不带小数点。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(f) or f == 0:
        return ""
    if f.is_integer():
        return str(int(f))
    return f"{f:.4f}".rstrip("0").rstrip(".")


def validate_rows(rows: list[dict]) -> tuple[list[dict], list[tuple[int, str, str]]]:
    """校验对话框里的表格内容。

    rows：每行 {code, name, shares, cost, note} 的原始文字。完全空白的行忽略。
    返回 (规整后的条目, 错误列表[(行号, 字段, 说明)])。有错误的行不进入条目。
    """
    items: list[dict] = []
    errors: list[tuple[int, str, str]] = []
    seen: dict[str, int] = {}
    for i, row in enumerate(rows):
        raw = {k: str(row.get(k, "") or "").strip() for k in FIELDS}
        if not any(raw.values()):
            continue
        ok = True
        code = parse_code(raw["code"])
        if code is None:
            errors.append((i, "code", "代码必须是 6 位数字" if raw["code"] else "请填写股票代码"))
            ok = False
        elif code in seen:
            errors.append((i, "code", f"代码重复（与第 {seen[code] + 1} 行相同）"))
            ok = False
        else:
            seen[code] = i
        shares = parse_amount(raw["shares"])
        if shares is None:
            errors.append((i, "shares", "持仓股数应为不小于 0 的数字"))
            ok = False
        cost = parse_amount(raw["cost"])
        if cost is None:
            errors.append((i, "cost", "成本价应为不小于 0 的数字"))
            ok = False
        if not ok:
            continue
        item = normalize_item({"code": code, "name": raw["name"], "shares": shares,
                               "cost": cost, "note": raw["note"]})
        if item:
            items.append(item)
    return items, errors


def dedupe(items: list[dict]) -> list[dict]:
    """normalize_item 规整 + 按代码去重（保留第一条）。"""
    out, seen = [], set()
    for raw in items:
        item = normalize_item(raw)
        if item and item["code"] not in seen:
            seen.add(item["code"])
            out.append(item)
    return out


# ---------------------------------------------------------------- 配置文件

def config_problem(path: Path | None = None) -> str | None:
    """配置文件存在但无法解析时返回原因（load_config 遇到这种情况会静默改用默认配置）。"""
    path = Path(path or paths.CONFIG_PATH)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:  # 被占用/无权限：不当作损坏，交给后续读写报错
        return None
    except ValueError as e:  # JSONDecodeError / UnicodeDecodeError
        return str(e)
    if not isinstance(data, dict):
        return "内容不是 JSON 对象"
    return None


def backup_corrupt_config(path: Path | None = None, now: dt.datetime | None = None) -> Path | None:
    """配置文件损坏时改名备份为 config.json.bad-YYYYmmdd-HHMMSS，返回备份路径；没损坏返回 None。

    在写入新配置之前调用，避免用默认配置覆盖用户手工编辑（但格式写错）的原文件。
    用改名而不是复制，所以重复调用不会产生多份备份。
    """
    path = Path(path or paths.CONFIG_PATH)
    problem = config_problem(path)
    if problem is None:
        return None
    now = now or dt.datetime.now()
    stem = f"{path.name}.bad-{now:%Y%m%d-%H%M%S}"
    dst, i = path.with_name(stem), 1
    while dst.exists():
        dst, i = path.with_name(f"{stem}-{i}"), i + 1
    os.replace(path, dst)
    log.warning("配置文件无法解析（%s），已备份为 %s", problem, dst)
    return dst


def save_watchlist(items: list[dict], load: Callable[[], dict] = load_config,
                   save: Callable[[dict], None] = save_config,
                   backup: Callable[[], Path | None] = backup_corrupt_config) -> dict:
    """只替换配置里的 watchlist，其他设置原样保留。返回保存后的配置。原文件损坏时先备份。"""
    backup()
    cfg = load()
    cfg["watchlist"] = dedupe(items)
    save(cfg)
    return cfg


def save_schedule_time(time_str: str, load: Callable[[], dict] = load_config,
                       save: Callable[[dict], None] = save_config,
                       backup: Callable[[], Path | None] = backup_corrupt_config) -> bool:
    """把自动复盘时间写回配置；值没变返回 False（不写文件）。原文件损坏时先备份。"""
    backup()
    cfg = load()
    if cfg.get("schedule_time") == time_str:
        return False
    cfg["schedule_time"] = time_str
    save(cfg)
    return True


# ---------------------------------------------------------------- AI 设置
# 只保存“用哪个环境变量”的名字，API Key 本身从不写进配置文件。

AI_FIELDS = ("enabled", "provider", "base_url", "model", "models", "api_key_env", "auto_review", "include_holdings")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,99}$")
SECRET_PLACEHOLDER = "（疑似 API Key）"   # 误把 Key 本身填进“变量名”时，用它代替原文，绝不回显 Key
SECRET_HINT = ("看起来填的是 API Key 本身：这里请填写存放 Key 的环境变量名（例如 DEEPSEEK_API_KEY），"
               "Key 只放在环境变量里，不要填进软件")


def parse_env_names(text) -> tuple[list[str], list[str]]:
    """'DEEPSEEK_API_KEY, DEEPSEEK-API' -> (['DEEPSEEK_API_KEY', 'DEEPSEEK-API'], 不合法的名字)。

    逗号（中英文）、分号、空白都可以分隔；重复的只保留第一个（Windows 环境变量名不区分大小写）。
    看起来像 API Key 本身的（sk- 开头、长随机串等）一律算不合法，并以 SECRET_PLACEHOLDER 代替原文返回。
    """
    names, bad, seen = [], [], set()
    for part in re.split(r"[,，;；\s]+", str(text or "")):
        part = part.strip()
        if not part:
            continue
        if looks_like_secret(part):
            if SECRET_PLACEHOLDER not in bad:
                bad.append(SECRET_PLACEHOLDER)
        elif not _ENV_NAME_RE.match(part):
            bad.append(part)
        elif part.upper() not in seen:
            seen.add(part.upper())
            names.append(part)
    return names, bad


def normalize_base_url(text) -> str | None:
    """接口地址：必须是 http(s):// 开头的网址，去掉末尾的 /；不合法返回 None。"""
    s = str(text or "").strip().rstrip("/")
    if not re.fullmatch(r"https?://[^\s/]+(/[^\s]*)?", s, flags=re.I):
        return None
    return s


def endpoint_problem(base_url: str, env_names) -> str | None:
    """接口地址 + Key 环境变量的组合是否安全（http 只允许本机；非 DeepSeek 地址不能用 DeepSeek 的 Key 变量）。"""
    return ai_endpoint_problem(base_url, env_names)


def merge_models(models, model: str) -> list[str]:
    """模型下拉列表：保持原有顺序、去重去空；当前模型不在列表里就加到最前面。"""
    out: list[str] = []
    for m in (str(x).strip() for x in (models or [])):
        if m and m not in out:
            out.append(m)
    model = str(model or "").strip()
    if model and model not in out:
        out.insert(0, model)
    return out


def save_ai_settings(values: dict, load: Callable[[], dict] = load_config,
                     save: Callable[[dict], None] = save_config,
                     backup: Callable[[], Path | None] = backup_corrupt_config) -> dict:
    """把「AI 设置」对话框的内容合并进配置里的 cfg['ai']（其他字段、ai 里未编辑的字段原样保留）。

    值为 None 的字段从 cfg['ai'] 删除（即恢复默认值）。values 只接受 AI_FIELDS 里的字段，
    任何像 key / api_key / token 的字段一律丢弃，API Key 绝不写入文件。原文件损坏时先备份。
    与默认值相同的项（包括 load_config 合并进来的 timeout / temperature 等）不写入，以后默认值更新能自动生效。
    """
    backup()
    cfg = load()
    cur = cfg.get("ai") if isinstance(cfg.get("ai"), dict) else {}
    new = {k: v for k, v in cur.items() if k not in ("api_key", "key", "token")}
    for k, v in values.items():
        if k not in AI_FIELDS:
            continue
        if v is None:
            new.pop(k, None)
        else:
            new[k] = v
    cfg["ai"] = strip_ai_defaults(new)
    save(cfg)
    return cfg


def valid_time(text) -> str | None:
    """'19:00' / '9:05' -> 'HH:MM'；不合法返回 None。"""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(text or "").strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}"


# ---------------------------------------------------------------- 自动复盘状态

# 任务计划程序的特殊返回码：0x41301 正在运行，0x41303 尚未运行，0x41325 已排队
_SCHED_NEUTRAL = {267009, 267011, 267045}


def schedule_summary(st: dict | None, time_str: str = DEFAULT_SCHEDULE_TIME) -> tuple[bool, str, str]:
    """scheduler.status() 的结果 -> (是否已开启, 状态文字, 补充说明)。st=None 表示正在读取。

    time_str：未开启时点「开启」将使用的时间（配置里的 schedule_time，默认 19:00）。
    """
    if st is None:
        return False, "正在读取状态…", ""
    if not st.get("installed"):
        if st.get("error"):
            first = str(st["error"]).strip().splitlines()[0][:80] if str(st["error"]).strip() else ""
            return False, "状态未知", "读取任务计划失败" + (f"：{first}" if first else "")
        return False, "未开启", f"开启后，每个工作日 {time_str} 自动生成复盘（节假日自动跳过）"

    title = f"已开启：每个工作日 {st.get('time') or '?'}"
    parts = []
    if str(st.get("state", "")).lower() == "disabled":
        parts.append("任务已在“任务计划程序”中被禁用")
    elif st.get("next_run"):
        parts.append(f"下次运行 {format_run_time(st['next_run'])}")
    if st.get("last_run"):
        code = st.get("last_result")
        if code in (None, 0) or code in _SCHED_NEUTRAL:
            parts.append(f"上次运行 {format_run_time(st['last_run'])}")
        else:
            parts.append(f"上次运行 {format_run_time(st['last_run'])} 异常（返回码 {code}）")
    return True, title, "\n".join(parts)


# ---------------------------------------------------------------- 生成与报告

# report.resolve_trade_date 抛出的 ValueError 是给用户的提示（非交易日、还没收盘），其他 ValueError 多半是内部错误
_HINT_FUNCS = {"resolve_trade_date"}


def is_user_hint(exc: BaseException) -> bool:
    """ValueError 是否来自“可预期的用户提示”（按抛出位置的函数名判断）。"""
    tb = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ is not None else []
    return bool(tb) and tb[-1].name in _HINT_FUNCS


def friendly_error(exc: BaseException, log_file: Path = LOG_FILE) -> tuple[str, str]:
    """生成失败时给用户看的 (标题, 正文)。

    ValueError 原样显示消息（如“2026-10-10 不是交易日”）；不是来自日期检查的 ValueError 另外附上日志位置。
    """
    if isinstance(exc, ValueError):
        msg = str(exc).strip() or "无法生成复盘（未知原因）"
        if not is_user_hint(exc):
            msg += f"\n\n详细信息已写入日志文件：\n{log_file}"
        return "无法生成复盘", msg
    text = str(exc).strip()
    msg = text.splitlines()[0] if text else type(exc).__name__
    if len(msg) > 150:
        msg = msg[:150] + "…"
    return "生成失败", f"生成复盘报告时出错：{msg}\n\n详细信息已写入日志文件：\n{log_file}"


APPROX_CALENDAR_TIP = "交易日历获取失败，暂按工作日估算，节假日可能不准"


def latest_hint(latest: dt.date | None, report_dates: set[dt.date], approximate: bool = False) -> str | None:
    """最近一个已收盘交易日还没有报告时，状态栏给出的提示。approximate=True 表示日历是按工作日估算的。"""
    if latest is None or latest in report_dates:
        return APPROX_CALENDAR_TIP if approximate and latest is not None else None
    hint = f"可以生成 {report_label(latest)} 的复盘：点击左侧「生成最新复盘」"
    return hint + f"（注意：{APPROX_CALENDAR_TIP}）" if approximate else hint


def latest_caption(latest: dt.date | None, report_dates: set[dt.date], failed: bool = False,
                   approximate: bool = False) -> str:
    """侧栏「生成最新复盘」下方的说明文字。"""
    if latest is None:
        return "交易日历读取失败" if failed else "正在读取交易日历…"
    text = f"最近收盘：{latest:%m-%d} {weekday_cn(latest)} · " + ("已生成" if latest in report_dates else "未生成")
    return text + "\n（日历按工作日估算，节假日可能不准）" if approximate else text


def check_review_date(d: dt.date, latest: dt.date, is_trade_day: Callable[[dt.date], bool],
                      prev_trade_day: Callable[[dt.date], dt.date | None] | None = None) -> tuple[bool, str]:
    """“复盘指定日期”能否选择 d。返回 (可以, 提示文字)。"""
    label = report_label(d)
    if not is_trade_day(d):
        tip = f"{label} 不是交易日（休市）"
        prev = prev_trade_day(d) if prev_trade_day else None
        if prev and prev <= latest:
            tip += f"，上一个交易日是 {report_label(prev)}"
        return False, tip
    if d > latest:
        return False, f"{label} 还没有收盘，最近已收盘的交易日是 {report_label(latest)}"
    return True, f"{label} · 交易日"


def report_files(html_path: Path) -> list[Path]:
    """一份报告对应的文件（只有 html 和 json）。"""
    html_path = Path(html_path)
    return [html_path.with_suffix(".html"), html_path.with_suffix(".json")]


def delete_report(html_path: Path) -> list[Path]:
    """删除一份报告的 html/json，返回实际删除的文件。只删除 YYYY-MM-DD 命名的报告文件。"""
    html_path = Path(html_path)
    dt.date.fromisoformat(html_path.stem)  # 不是报告文件名时抛 ValueError
    removed = []
    for p in report_files(html_path):
        if p.exists():
            p.unlink()
            removed.append(p)
    return removed
