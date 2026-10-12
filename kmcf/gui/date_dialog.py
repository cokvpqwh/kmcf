"""「复盘指定日期…」对话框：只允许选择已收盘的交易日。"""

import datetime as dt

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor, QFont, QTextCharFormat
from PySide6.QtWidgets import (QCalendarWidget, QCheckBox, QDialog, QDialogButtonBox, QLabel, QVBoxLayout)

from .. import trade_calendar as tc
from . import logic, theme


def _qdate(d: dt.date) -> QDate:
    return QDate(d.year, d.month, d.day)


def _pydate(q: QDate) -> dt.date:
    return dt.date(q.year(), q.month(), q.day())


class DateDialog(QDialog):
    def __init__(self, parent, latest: dt.date, report_dates: set[dt.date], initial: dt.date | None = None,
                 approximate: bool | None = None):
        super().__init__(parent)
        self.setWindowTitle("复盘指定日期")
        self.latest = latest
        self.report_dates = set(report_dates)
        self._ok = False
        if approximate is None:
            approximate = bool(getattr(tc, "approximate", False))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)
        intro = QLabel("选择一个已收盘的交易日，生成当天的复盘报告。灰色日期为休市日或还没有收盘，加粗的日期已有报告。")
        intro.setObjectName("Hint")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        if approximate:
            warn = QLabel(f"注意：{logic.APPROX_CALENDAR_TIP}。")
            warn.setWordWrap(True)
            warn.setStyleSheet(f"color: {theme.current()['warn']};")
            lay.addWidget(warn)
        self.approximate = approximate

        cal = QCalendarWidget(self)
        cal.setFirstDayOfWeek(Qt.DayOfWeek.Monday)
        cal.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        cal.setHorizontalHeaderFormat(QCalendarWidget.HorizontalHeaderFormat.ShortDayNames)
        cal.setGridVisible(False)
        days = tc.trade_days()
        cal.setMinimumDate(_qdate(days[0] if days else dt.date(2005, 1, 4)))
        cal.setMaximumDate(_qdate(latest))
        cal.setMinimumHeight(260)
        self.calendar = cal
        lay.addWidget(cal)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(40)
        lay.addWidget(self.status)

        self.force_box = QCheckBox("重新获取数据（忽略已缓存的数据，耗时较长）")
        lay.addWidget(self.force_box)

        buttons = QDialogButtonBox()
        self.ok_btn = buttons.addButton("开始复盘", QDialogButtonBox.ButtonRole.AcceptRole)
        self.ok_btn.setObjectName("Primary")
        self.ok_btn.setDefault(True)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._try_accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self._setup_formats()
        cal.currentPageChanged.connect(self._format_page)
        cal.selectionChanged.connect(self._update)
        cal.activated.connect(lambda _d: self._try_accept())

        start = initial if initial and initial <= latest else latest
        cal.setSelectedDate(_qdate(start))
        cal.setCurrentPage(start.year, start.month)
        self._format_page(start.year, start.month)
        self._update()
        self.resize(420, 470)

    # ------------------------------------------------------------ 日历着色
    def _setup_formats(self) -> None:
        t = theme.current()
        self._fmt_closed = QTextCharFormat()
        self._fmt_closed.setForeground(QColor(t["faint"]))
        self._fmt_report = QTextCharFormat()
        self._fmt_report.setFontWeight(QFont.Weight.Bold)
        self._fmt_report.setForeground(QColor(t["accent_text"]))
        self._fmt_normal = QTextCharFormat()
        self._fmt_normal.setForeground(QColor(t["text"]))
        self._fmt_other = QTextCharFormat()  # 不属于当前月份的日期
        self._fmt_other.setForeground(QColor(t["border_strong"]))
        self._fmt_other_report = QTextCharFormat(self._fmt_other)
        self._fmt_other_report.setFontWeight(QFont.Weight.Bold)
        # 默认周末是红色；A 股里红色表示上涨，这里改成灰色
        for day in (Qt.DayOfWeek.Saturday, Qt.DayOfWeek.Sunday):
            self.calendar.setWeekdayTextFormat(day, self._fmt_closed)

    def _format_page(self, year: int, month: int) -> None:
        first = dt.date(year, month, 1)
        d = first - dt.timedelta(days=7)
        end = first + dt.timedelta(days=45)
        while d <= end:
            if d.month != month:
                fmt = self._fmt_other_report if d in self.report_dates else self._fmt_other
            elif d > self.latest or not tc.is_trade_day(d):  # 还没收盘的日期也显示成灰色（不可选）
                fmt = self._fmt_closed
            elif d in self.report_dates:
                fmt = self._fmt_report
            else:
                fmt = self._fmt_normal
            self.calendar.setDateTextFormat(_qdate(d), fmt)
            d += dt.timedelta(days=1)

    # ------------------------------------------------------------ 选择
    def selected_date(self) -> dt.date:
        return _pydate(self.calendar.selectedDate())

    def force(self) -> bool:
        return self.force_box.isChecked()

    def _update(self) -> None:
        d = self.selected_date()
        ok, msg = logic.check_review_date(d, self.latest, tc.is_trade_day, tc.prev_trade_day)
        t = theme.current()
        if ok:
            extra = []
            if d in self.report_dates:
                extra.append("已有报告，将重新生成")
            if d != self.latest:
                extra.append("历史日期：涨跌分布、板块实时排行等快照类数据只能使用当天曾缓存的数据，没有缓存的部分会跳过")
            msg = msg + ("<br>" + "；".join(extra) if extra else "")
            self.status.setStyleSheet(f"color: {t['muted']};")
        else:
            self.status.setStyleSheet(f"color: {t['up']};")
        self.status.setText(msg)
        self._ok = ok
        self.ok_btn.setEnabled(ok)

    def _try_accept(self) -> None:
        if self._ok:
            self.accept()
