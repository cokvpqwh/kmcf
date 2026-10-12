"""界面配色与样式表（亮/暗两套，跟随系统）。颜色与报告模板保持一致。"""

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPalette, QPen
from PySide6.QtWidgets import QApplication

from .. import paths

PRIMARY = "#3e63dd"          # 主按钮强调色
PRIMARY_HOVER = "#3557c7"
PRIMARY_PRESSED = "#2f4eb5"
FONT_FAMILY = "Microsoft YaHei UI"

LIGHT = {
    "name": "light",
    "bg": "#f5f6f8", "card": "#ffffff", "card2": "#f9fafb", "hover": "#f0f1f3", "pressed": "#e8e9ec",
    "text": "#1c2024", "muted": "#60646c", "faint": "#8b8d98",
    "border": "#e3e5e9", "border_strong": "#cdd0d6",
    "accent": "#3e63dd", "accent_soft": "#edf1fd", "accent_text": "#2f4eb5",
    "primary_disabled": "#a9b9ef",
    "up": "#e5484d", "up_soft": "#feebec", "down": "#12a150", "down_soft": "#e6f6eb",
    "warn": "#b25e00", "warn_soft": "#fff4e0",
    "accent_border": "#c3d0f6", "accent_hover": "#e2e9fc", "ok": "#12a150",
}

DARK = {
    "name": "dark",
    "bg": "#111214", "card": "#191a1d", "card2": "#1f2124", "hover": "#25272b", "pressed": "#2c2e33",
    "text": "#edeef0", "muted": "#a0a3ab", "faint": "#6f727a",
    "border": "#2c2e33", "border_strong": "#3a3d43",
    "accent": "#7b9cff", "accent_soft": "#1c2440", "accent_text": "#a8bcff",
    "primary_disabled": "#2b3a6b",
    "up": "#ff6369", "up_soft": "#3b1219", "down": "#3dd68c", "down_soft": "#0f2e1d",
    "warn": "#ffb224", "warn_soft": "#2e2009",
    "accent_border": "#2f3f75", "accent_hover": "#24305a", "ok": "#3dd68c",
}

_current = LIGHT


def current() -> dict:
    return _current


def system_is_dark(app: QApplication) -> bool:
    try:
        return app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:  # 旧版 Qt
        return False


def build_palette(t: dict) -> QPalette:
    p = QPalette()
    c = QColor
    roles = {
        QPalette.ColorRole.Window: t["bg"], QPalette.ColorRole.WindowText: t["text"],
        QPalette.ColorRole.Base: t["card"], QPalette.ColorRole.AlternateBase: t["card2"],
        QPalette.ColorRole.Text: t["text"], QPalette.ColorRole.Button: t["card"],
        QPalette.ColorRole.ButtonText: t["text"], QPalette.ColorRole.BrightText: "#ffffff",
        QPalette.ColorRole.Highlight: PRIMARY, QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.ToolTipBase: t["card"], QPalette.ColorRole.ToolTipText: t["text"],
        QPalette.ColorRole.PlaceholderText: t["faint"], QPalette.ColorRole.Link: t["accent"],
        QPalette.ColorRole.Light: t["card"], QPalette.ColorRole.Midlight: t["card2"],
        QPalette.ColorRole.Mid: t["border_strong"], QPalette.ColorRole.Dark: t["border_strong"],
        QPalette.ColorRole.Shadow: "#000000",
    }
    for role, color in roles.items():
        p.setColor(role, c(color))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, c(t["faint"]))
    return p


def _arrow_png(color: str, name: str) -> str | None:
    """画一个下拉箭头（2 倍分辨率的 PNG，放在 data/cache/ui），返回 QSS 能用的路径；失败返回 None。"""
    try:
        folder = paths.CACHE_DIR / "ui"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"combo_arrow_{name}.png"
        img = QImage(20, 12, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(color), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                            Qt.PenJoinStyle.RoundJoin))
        painter.drawPolyline([QPointF(4, 3), QPointF(10, 9), QPointF(16, 3)])
        painter.end()
        if not img.save(str(path), "PNG"):
            return None
        return path.as_posix()
    except Exception:  # noqa: BLE001  画不出来就用 Fusion 自带的样式
        return None


def _combo_arrow_qss(t: dict) -> str:
    """下拉框右侧的按钮：去掉 Fusion 默认的凹陷边框，换成和圆角边框协调的细箭头。"""
    arrow = _arrow_png(t["muted"], t.get("name", "light"))
    if not arrow:
        return ""
    return (f"QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 20px; "
            f"border: none; background: transparent; }}\n"
            f'QComboBox::down-arrow {{ image: url("{arrow}"); width: 10px; height: 6px; }}\n'
            f"QComboBox::down-arrow:disabled {{ image: url(\"{arrow}\"); }}\n")


def build_qss(t: dict) -> str:
    return f"""
QMainWindow, QDialog {{ background: {t['bg']}; }}
QWidget {{ color: {t['text']}; }}
QToolTip {{ background: {t['card']}; color: {t['text']}; border: 1px solid {t['border']}; padding: 4px 6px; }}

#Sidebar {{ background: {t['card']}; border-right: 1px solid {t['border']}; }}
#AppTitle {{ font-size: 15pt; font-weight: 700; }}
#AppSubtitle {{ color: {t['muted']}; }}
#SectionLabel {{ color: {t['faint']}; font-weight: 600; font-size: 8.5pt; }}
#Caption {{ color: {t['muted']}; font-size: 8.5pt; }}
#Hint {{ color: {t['muted']}; }}
#ErrorText {{ color: {t['up']}; }}
#WarnText {{ color: {t['warn']}; font-size: 8.5pt; }}

QPushButton {{
  background: {t['card']}; color: {t['text']}; border: 1px solid {t['border']};
  border-radius: 6px; padding: 6px 12px; min-height: 18px;
}}
QPushButton:hover {{ background: {t['hover']}; border-color: {t['border_strong']}; }}
QPushButton:pressed {{ background: {t['pressed']}; }}
QPushButton:disabled {{ color: {t['faint']}; background: {t['card2']}; border-color: {t['border']}; }}
QPushButton:default {{ border-color: {PRIMARY}; }}
QPushButton#Primary {{
  background: {PRIMARY}; color: #ffffff; border: none; font-weight: 600;
  padding: 10px 12px; font-size: 10.5pt;
}}
QPushButton#Primary:hover {{ background: {PRIMARY_HOVER}; }}
QPushButton#Primary:pressed {{ background: {PRIMARY_PRESSED}; }}
QPushButton#Primary:disabled {{ background: {t['primary_disabled']}; color: rgba(255, 255, 255, 0.85); }}
QDialog QPushButton#Primary {{ padding: 6px 18px; font-size: 9.5pt; }}
QPushButton#Danger {{ color: {t['up']}; }}
QPushButton#Link {{ background: transparent; border: none; color: {t['accent']}; padding: 2px 4px; }}
QPushButton#Link:hover {{ text-decoration: underline; }}
QPushButton#Link:disabled {{ color: {t['faint']}; }}

QListWidget#History {{ background: transparent; border: none; outline: 0; }}
QListWidget#History::item {{ padding: 7px 10px; border-radius: 6px; margin: 1px 0; color: {t['text']}; }}
QListWidget#History::item:hover {{ background: {t['hover']}; }}
QListWidget#History::item:selected {{ background: {t['accent_soft']}; color: {t['accent_text']}; }}

QFrame#Panel {{ background: {t['card2']}; border: 1px solid {t['border']}; border-radius: 8px; }}
QFrame#Panel QLabel {{ background: transparent; }}

QStatusBar {{ background: {t['card']}; border-top: 1px solid {t['border']}; color: {t['muted']}; }}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {t['muted']}; padding: 0 6px; }}
QProgressBar {{ border: none; background: {t['border']}; border-radius: 3px; max-height: 6px; min-height: 6px; }}
QProgressBar::chunk {{ background: {PRIMARY}; border-radius: 3px; }}

QLineEdit, QTimeEdit, QDateEdit {{
  background: {t['card']}; border: 1px solid {t['border']}; border-radius: 6px; padding: 4px 8px;
  selection-background-color: {PRIMARY}; selection-color: #ffffff;
}}
QLineEdit:focus, QTimeEdit:focus, QDateEdit:focus {{ border-color: {PRIMARY}; }}
QTimeEdit:disabled {{ color: {t['faint']}; background: {t['card2']}; }}
/* 不用 NoButtons（与样式表一起用时 Qt 会把输入框宽度算成 1px），改为把上下按钮宽度设为 0 */
QTimeEdit::up-button, QTimeEdit::down-button {{ width: 0px; border: none; }}

QTableWidget {{
  background: {t['card']}; border: 1px solid {t['border']}; border-radius: 6px;
  gridline-color: {t['border']}; selection-background-color: {t['accent_soft']}; selection-color: {t['text']};
}}
QTableWidget QLineEdit {{ border-radius: 0; padding: 0 4px; }}
QHeaderView::section {{
  background: {t['card2']}; color: {t['muted']}; border: none; border-bottom: 1px solid {t['border']};
  padding: 6px 8px; font-weight: 600;
}}
QTableCornerButton::section {{ background: {t['card2']}; border: none; }}

QMenu {{ background: {t['card']}; border: 1px solid {t['border']}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px 6px 14px; border-radius: 4px; }}
QMenu::item:selected {{ background: {t['accent_soft']}; color: {t['text']}; }}
QMenu::item:disabled {{ color: {t['faint']}; }}
QMenu::separator {{ height: 1px; background: {t['border']}; margin: 4px 6px; }}

QCheckBox {{ spacing: 6px; }}

QCalendarWidget QWidget#qt_calendar_navigationbar {{ background: {t['card']}; border-bottom: 1px solid {t['border']}; }}
QCalendarWidget QToolButton {{
  color: {t['text']}; background: transparent; border: none; border-radius: 4px; padding: 4px 8px; font-weight: 600;
}}
QCalendarWidget QToolButton:hover {{ background: {t['hover']}; }}
QCalendarWidget QToolButton::menu-indicator {{ image: none; width: 0; }}
QCalendarWidget QSpinBox {{ background: {t['card']}; border: 1px solid {t['border']}; border-radius: 4px; padding: 2px 4px; }}
QCalendarWidget QAbstractItemView:enabled {{
  background: {t['card']}; selection-background-color: {PRIMARY}; selection-color: #ffffff; outline: 0;
}}
QCalendarWidget QAbstractItemView:disabled {{ color: {t['faint']}; }}
/* ---- AI 分析：侧栏按钮、停靠面板、对话区 ---- */
QPushButton#AIButton {{
  background: {t['accent_soft']}; color: {t['accent_text']}; border: 1px solid {t['accent_border']};
  font-weight: 600; padding: 8px 12px;
}}
QPushButton#AIButton:hover {{ background: {t['accent_hover']}; border-color: {PRIMARY}; }}
QPushButton#AIButton:pressed {{ background: {t['accent_border']}; }}
QPushButton#AIButton:checked {{ background: {t['accent_hover']}; border: 1px solid {PRIMARY}; }}
QPushButton#AIButton:checked:hover {{ background: {t['accent_border']}; }}
QDockWidget {{ color: {t['text']}; }}
QDockWidget::title {{
  background: {t['card']}; border-bottom: 1px solid {t['border']}; border-left: 1px solid {t['border']};
  padding: 7px 10px; text-align: left;
}}
QDockWidget::close-button, QDockWidget::float-button {{ border: none; background: transparent; padding: 1px; }}
QDockWidget::close-button:hover, QDockWidget::float-button:hover {{ background: {t['hover']}; border-radius: 4px; }}
QMainWindow::separator {{ background: {t['border']}; width: 1px; height: 1px; }}
QMainWindow::separator:hover {{ background: {PRIMARY}; }}
#AIPanel {{ background: {t['card']}; border-left: 1px solid {t['border']}; }}
#AIPanel QLabel {{ background: transparent; }}
#AIReportDate {{ font-weight: 600; }}
QPushButton#Chip {{
  background: {t['card2']}; color: {t['text']}; border: 1px solid {t['border']}; border-radius: 13px;
  padding: 4px 11px; min-height: 16px;
}}
QPushButton#Chip:hover {{ background: {t['accent_soft']}; color: {t['accent_text']}; border-color: {t['accent_border']}; }}
QPushButton#Chip:pressed {{ background: {t['accent_hover']}; }}
QPushButton#Chip:disabled {{ color: {t['faint']}; background: {t['card2']}; border-color: {t['border']}; }}
QFrame#KeyOk {{ background: {t['card2']}; border: 1px solid {t['border']}; border-radius: 6px; }}
QFrame#KeyWarn {{ background: {t['warn_soft']}; border: 1px solid {t['warn']}; border-radius: 6px; }}
QFrame#KeyWarn QLabel {{ color: {t['text']}; }}
QFrame#KeyWarn QLabel#KeyWarnTitle {{ color: {t['warn']}; font-weight: 600; }}
QTextBrowser#Chat {{
  background: {t['card']}; border: 1px solid {t['border']}; border-radius: 8px;
  selection-background-color: {PRIMARY}; selection-color: #ffffff;
}}
QPlainTextEdit#ChatInput {{
  background: {t['card']}; border: 1px solid {t['border_strong']}; border-radius: 8px; padding: 4px 6px;
  selection-background-color: {PRIMARY}; selection-color: #ffffff;
}}
QPlainTextEdit#ChatInput:focus {{ border-color: {PRIMARY}; }}
QComboBox {{
  background: {t['card']}; border: 1px solid {t['border']}; border-radius: 6px; padding: 3px 8px; min-height: 18px;
}}
QComboBox:hover {{ border-color: {t['border_strong']}; }}
QComboBox:focus {{ border-color: {PRIMARY}; }}
{_combo_arrow_qss(t)}QComboBox QAbstractItemView {{
  background: {t['card']}; border: 1px solid {t['border']}; selection-background-color: {t['accent_soft']};
  selection-color: {t['text']}; outline: 0;
}}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {t['border_strong']}; border-radius: 4px; min-height: 30px; margin: 2px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
"""


def apply(app: QApplication, dark: bool | None = None) -> dict:
    """设置 Fusion 风格、字体、调色板和样式表。dark=None 时跟随系统。"""
    global _current
    if dark is None:
        dark = system_is_dark(app)
    _current = DARK if dark else LIGHT
    app.setStyle("Fusion")
    font = QFont(FONT_FAMILY)
    font.setPointSizeF(9.5)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(font)
    app.setPalette(build_palette(_current))
    app.setStyleSheet(build_qss(_current))
    return _current
