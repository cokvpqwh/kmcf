"""右侧报告区域：QWebEngineView 显示本地 HTML 报告。

- 本地报告允许读取本地文件和远程 CDN（echarts 先读 reports/assets，失败再走 CDN）；
- 报告里的外部链接交给系统浏览器；
- https://kmcf.invalid/动作 形式的链接（空状态页里的按钮）转成 actionRequested 信号
  （.invalid 是保留域名，永远不会真的联网；自定义协议的链接到不了 acceptNavigationRequest，所以用它）；
- 按界面主题给报告注入 data-theme，保证报告和软件外壳的亮/暗一致。
"""

import html
from pathlib import Path

from PySide6.QtCore import QUrl, Signal, Slot
from PySide6.QtGui import QAction, QColor, QDesktopServices, QGuiApplication, QKeySequence
from PySide6.QtWebEngineCore import (QWebEngineNewWindowRequest, QWebEnginePage, QWebEngineProfile,
                                     QWebEngineScript, QWebEngineSettings)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QMenu

from . import theme

ACTION_HOST = "kmcf.invalid"  # 空状态页按钮：https://kmcf.invalid/generate
ZOOM_STEPS = (0.67, 0.75, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0)


def action_url(action: str) -> str:
    return f"https://{ACTION_HOST}/{action}"


def _theme_script(name: str) -> QWebEngineScript:
    js = (
        "(function(){var t=%r;function s(){var e=document.documentElement;"
        "if(e){e.setAttribute('data-theme',t);return true}return false}"
        "if(!s()){var o=new MutationObserver(function(){if(s())o.disconnect()});"
        "o.observe(document,{childList:true,subtree:true});}})();" % name
    )
    script = QWebEngineScript()
    script.setName("kmcf-theme")
    script.setSourceCode(js)
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
    script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
    script.setRunsOnSubFrames(False)
    return script


class ReportPage(QWebEnginePage):
    actionRequested = Signal(str)

    def __init__(self, profile: QWebEngineProfile, parent=None):
        super().__init__(profile, parent)
        s = self.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.FocusOnNavigationEnabled, False)
        s.setDefaultTextEncoding("utf-8")
        self.newWindowRequested.connect(self._on_new_window)

    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:
        scheme = url.scheme().lower()
        if url.host().lower() == ACTION_HOST:
            self.actionRequested.emit(url.path().strip("/").lower())
            return False
        if is_main_frame and scheme in ("http", "https", "mailto"):
            QDesktopServices.openUrl(url)  # 外部链接用系统浏览器打开
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)

    @Slot(QWebEngineNewWindowRequest)
    def _on_new_window(self, request: QWebEngineNewWindowRequest) -> None:
        url = request.requestedUrl()
        if url.isValid() and url.scheme().lower() in ("http", "https", "mailto", "file"):
            QDesktopServices.openUrl(url)

    def javaScriptConsoleMessage(self, level, message, line, source):  # 报告里的 JS 日志不打印到控制台
        pass


class ReportView(QWebEngineView):
    actionRequested = Signal(str)
    zoomChanged = Signal(float)
    reportChanged = Signal(object)  # 当前显示的报告路径（Path；空状态页为 None），AI 分析面板据此切换报告

    def __init__(self, parent=None):
        super().__init__(parent)
        self._page = ReportPage(QWebEngineProfile.defaultProfile(), self)
        self._page.actionRequested.connect(self.actionRequested)
        self.setPage(self._page)
        self.current_path: Path | None = None
        self.apply_theme(theme.current(), reload=False)
        self._add_shortcuts()

    # ------------------------------------------------------------ 主题
    def apply_theme(self, t: dict, reload: bool = True) -> None:
        scripts = self._page.scripts()
        for old in scripts.find("kmcf-theme"):
            scripts.remove(old)
        scripts.insert(_theme_script(t["name"]))
        self._page.setBackgroundColor(QColor(t["bg"]))
        if reload:
            if self.current_path is not None:
                self.reload()
            else:
                self.show_empty()

    # ------------------------------------------------------------ 内容
    def show_report(self, path: Path, force_reload: bool = False) -> None:
        path = Path(path)
        url = QUrl.fromLocalFile(str(path))
        if self.current_path == path and self.url() == url:
            if force_reload:
                self.reload()
            self.reportChanged.emit(path)
            return
        self.current_path = path
        self.setUrl(url)
        self.reportChanged.emit(path)

    def show_empty(self, sections: list[str] | None = None) -> None:
        self.current_path = None
        self.setHtml(empty_state_html(theme.current(), sections or []), QUrl("about:blank"))
        self.reportChanged.emit(None)

    # ------------------------------------------------------------ 缩放
    def set_zoom(self, factor: float) -> None:
        factor = max(ZOOM_STEPS[0], min(ZOOM_STEPS[-1], float(factor)))
        self.setZoomFactor(factor)
        self.zoomChanged.emit(factor)

    def zoom_step(self, direction: int) -> None:
        cur = self.zoomFactor()
        if direction > 0:
            nxt = next((z for z in ZOOM_STEPS if z > cur + 1e-3), ZOOM_STEPS[-1])
        else:
            nxt = next((z for z in reversed(ZOOM_STEPS) if z < cur - 1e-3), ZOOM_STEPS[0])
        self.set_zoom(nxt)

    def _add_shortcuts(self) -> None:
        for keys, slot in (((QKeySequence.StandardKey.ZoomIn, "Ctrl+="), lambda: self.zoom_step(1)),
                           ((QKeySequence.StandardKey.ZoomOut,), lambda: self.zoom_step(-1)),
                           (("Ctrl+0",), lambda: self.set_zoom(1.0))):
            act = QAction(self)
            act.setShortcuts([QKeySequence(k) for k in keys])
            act.triggered.connect(slot)
            self.addAction(act)

    # ------------------------------------------------------------ 右键菜单
    def contextMenuEvent(self, event):
        menu = QMenu(self)
        req = self.lastContextMenuRequest()
        link = req.linkUrl() if req is not None else QUrl()
        if link.isValid() and not link.isEmpty() and link.scheme() in ("http", "https"):
            menu.addAction("在浏览器中打开链接", lambda: QDesktopServices.openUrl(link))
            menu.addAction("复制链接地址", lambda: QGuiApplication.clipboard().setText(link.toString()))
            menu.addSeparator()
        if self.hasSelection():
            menu.addAction("复制", lambda: self._page.triggerAction(QWebEnginePage.WebAction.Copy))
        menu.addAction("全选", lambda: self._page.triggerAction(QWebEnginePage.WebAction.SelectAll))
        menu.addSeparator()
        if self.current_path is not None:
            menu.addAction("重新加载", self.reload)
            path = self.current_path
            menu.addAction("在浏览器中打开", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))))
            menu.addSeparator()
        menu.addAction("放大（Ctrl +）", lambda: self.zoom_step(1))
        menu.addAction("缩小（Ctrl -）", lambda: self.zoom_step(-1))
        menu.addAction(f"实际大小（当前 {round(self.zoomFactor() * 100)}%）", lambda: self.set_zoom(1.0))
        menu.exec(event.globalPos())


# ---------------------------------------------------------------- 空状态页

_ICON_SVG = """<svg width="64" height="64" viewBox="0 0 64 64" aria-hidden="true">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#5a7bf0"/>
<stop offset="1" stop-color="#2f4eb5"/></linearGradient></defs>
<rect x="3" y="3" width="58" height="58" rx="13" fill="url(#g)"/>
<g fill="rgba(255,255,255,.24)"><rect x="12" y="45" width="5" height="6" rx="1"/><rect x="20" y="42" width="5" height="9" rx="1"/>
<rect x="28" y="44" width="5" height="7" rx="1"/><rect x="36" y="40" width="5" height="11" rx="1"/>
<rect x="44" y="37" width="5" height="14" rx="1"/></g>
<polyline points="13,41 25,32 34,37 49,19" fill="none" stroke="#fff" stroke-width="5"
 stroke-linecap="round" stroke-linejoin="round"/>
<circle cx="49" cy="19" r="5.5" fill="#ff4d4f" stroke="#fff" stroke-width="2"/></svg>"""


def empty_state_html(t: dict, sections: list[str]) -> str:
    chips = "".join(f"<span class='chip'>{html.escape(s)}</span>" for s in sections)
    chips_block = f"<div class='chips'><span class='chips-label'>报告包含</span>{chips}</div>" if chips else ""
    return f"""<!doctype html>
<html lang="zh-CN" data-theme="{t['name']}"><head><meta charset="utf-8"><title>kmcf</title>
<style>
:root {{ --bg:{t['bg']}; --card:{t['card']}; --text:{t['text']}; --muted:{t['muted']}; --faint:{t['faint']};
  --border:{t['border']}; --accent:{theme.PRIMARY}; --accent-soft:{t['accent_soft']}; --up:{t['up']}; }}
* {{ box-sizing: border-box; }}
html, body {{ height: 100%; }}
body {{ margin: 0; background: var(--bg); color: var(--text); display: flex; align-items: center; justify-content: center;
  font: 14px/1.7 "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", system-ui, sans-serif; }}
.wrap {{ max-width: 720px; padding: 40px 32px; }}
.head {{ display: flex; align-items: center; gap: 16px; }}
h1 {{ font-size: 24px; margin: 0; letter-spacing: .5px; }}
.lead {{ color: var(--muted); margin: 6px 0 0; }}
.cta {{ display: inline-block; margin: 28px 0 8px; background: var(--accent); color: #fff; text-decoration: none;
  padding: 10px 22px; border-radius: 8px; font-weight: 600; }}
.cta:hover {{ filter: brightness(1.08); }}
.tip {{ color: var(--faint); font-size: 12.5px; }}
.steps {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-top: 28px; }}
.step {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }}
.step b {{ display: block; margin-bottom: 4px; }}
.step span {{ color: var(--muted); font-size: 13px; }}
.num {{ display: inline-flex; width: 22px; height: 22px; border-radius: 50%; background: var(--accent-soft);
  color: var(--accent); font-size: 12px; font-weight: 700; align-items: center; justify-content: center; margin-right: 6px; }}
.chips {{ margin-top: 22px; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
.chips-label {{ color: var(--faint); font-size: 12.5px; margin-right: 4px; }}
.chip {{ border: 1px solid var(--border); background: var(--card); border-radius: 999px; padding: 2px 12px; font-size: 12.5px; color: var(--muted); }}
.foot {{ margin-top: 28px; color: var(--faint); font-size: 12px; }}
@media (max-width: 640px) {{ .steps {{ grid-template-columns: 1fr; }} }}
</style></head>
<body><div class="wrap">
  <div class="head">{_ICON_SVG}<div><h1>kmcf · A股每日复盘</h1>
    <p class="lead">收盘后一键汇总大盘、板块、资金和你的自选股，生成图文复盘报告，历史报告随时回看。</p></div></div>
  <a class="cta" href="{action_url('generate')}">生成最新复盘</a>
  <div class="tip">也可以点击左侧「生成最新复盘」。首次生成需要联网获取数据，通常需要 1～3 分钟。</div>
  <div class="steps">
    <div class="step"><b><span class="num">1</span>生成复盘</b><span>默认复盘最近一个已收盘的交易日，也可以选择历史日期。</span></div>
    <div class="step"><b><span class="num">2</span>设置自选股</b><span>在「自选股/持仓…」里添加股票代码和持仓，报告会单独列出。</span></div>
    <div class="step"><b><span class="num">3</span>自动复盘</b><span>开启后每个工作日晚上自动生成，打开软件就能看到当天报告。</span></div>
  </div>
  {chips_block}
  <div class="foot">数据来自公开市场数据接口，仅供复盘参考，不构成投资建议。</div>
</div></body></html>"""
