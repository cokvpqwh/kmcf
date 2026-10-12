"""把报告数据渲染成单个 HTML 文件。"""

import datetime as dt
import json
import math

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import paths

ECHARTS_VERSION = "5.5.1"
# 报告优先用本地 reports/assets/echarts.min.js，失败再依次尝试 CDN
ECHARTS_SOURCES = [
    "assets/echarts.min.js",
    f"https://registry.npmmirror.com/echarts/{ECHARTS_VERSION}/files/dist/echarts.min.js",
    f"https://cdn.jsdelivr.net/npm/echarts@{ECHARTS_VERSION}/dist/echarts.min.js",
    f"https://cdnjs.cloudflare.com/ajax/libs/echarts/{ECHARTS_VERSION}/echarts.min.js",
    f"https://unpkg.com/echarts@{ECHARTS_VERSION}/dist/echarts.min.js",
]

_env: Environment | None = None


def _get_env() -> Environment:
    global _env
    if _env is None:
        _env = Environment(
            loader=FileSystemLoader(str(paths.TEMPLATE_DIR)),
            autoescape=select_autoescape(enabled_extensions=("html", "j2"), default_for_string=True),
            trim_blocks=True,
            lstrip_blocks=True,
        )
    return _env


def sanitize(obj):
    """递归转换成纯 JSON 类型：numpy/pandas 标量 -> Python，NaN/inf -> None。"""
    if isinstance(obj, dict):
        return {str(k): sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    if isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (dt.datetime, dt.date)):
        return obj.isoformat()
    if hasattr(obj, "item"):  # numpy 标量
        try:
            return sanitize(obj.item())
        except (ValueError, TypeError):
            pass
    if hasattr(obj, "isoformat"):  # pandas Timestamp
        return obj.isoformat()
    try:
        import pandas as pd
        if pd.isna(obj):
            return None
    except (TypeError, ValueError, ImportError):
        pass
    return str(obj)


def to_script_json(obj) -> str:
    """可安全内嵌到 <script> 里的 JSON。"""
    s = json.dumps(sanitize(obj), ensure_ascii=False, separators=(",", ":"))
    return s.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def render_report(report: dict) -> str:
    report = sanitize(report)
    charts = []
    for s in report["sections"]:
        for i, b in enumerate(s["blocks"]):
            if b["type"] == "chart":
                cid = f"chart-{s['id']}-{i}"
                b["chart_id"] = cid
                charts.append({"id": cid, "option": b["option"]})
    tpl = _get_env().get_template("report.html.j2")
    return tpl.render(r=report, charts_json=to_script_json(charts),
                      echarts_sources_json=to_script_json(ECHARTS_SOURCES))
