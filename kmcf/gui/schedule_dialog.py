"""「修改时间…」对话框：自动复盘的时间（默认每个工作日 19:00）。

侧栏里不再放可直接滚动/输入的时间框：用户只需要晚上 7 点，改时间是次要操作，
必须在这里点「保存」才会写入配置、重新注册任务，避免鼠标滚轮经过时误改。
"""

from PySide6.QtCore import Qt, QTime
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QTimeEdit,
                               QVBoxLayout)

from . import logic


class ScheduleTimeDialog(QDialog):
    def __init__(self, parent, current: str, installed: bool):
        super().__init__(parent)
        self.setWindowTitle("自动复盘时间")
        self.installed = installed

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)
        intro = QLabel(f"每个工作日几点自动生成复盘？默认 {logic.DEFAULT_SCHEDULE_TIME}（晚上 7 点）。\n"
                       "龙虎榜等盘后数据一般 17:00～18:30 才发布完整，太早生成会缺少这些内容。")
        intro.setObjectName("Hint")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("每个工作日"))
        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat("HH:mm")
        self.time_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.time_edit.setFixedWidth(80)
        self.time_edit.setTime(QTime.fromString(logic.valid_time(current) or logic.DEFAULT_SCHEDULE_TIME, "HH:mm"))
        self.time_edit.setToolTip("直接输入时间，或用 ↑↓ 键调整")
        row.addWidget(self.time_edit)
        row.addStretch(1)
        self.btn_default = QPushButton(f"恢复默认 {logic.DEFAULT_SCHEDULE_TIME}")
        self.btn_default.setObjectName("Link")
        self.btn_default.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_default.clicked.connect(
            lambda: self.time_edit.setTime(QTime.fromString(logic.DEFAULT_SCHEDULE_TIME, "HH:mm")))
        row.addWidget(self.btn_default)
        lay.addLayout(row)

        tip = QLabel("保存后立即更新 Windows 任务计划。" if installed else "保存后，点「开启」按这个时间注册自动复盘。")
        tip.setObjectName("Caption")
        lay.addWidget(tip)

        buttons = QDialogButtonBox()
        self.ok_btn = buttons.addButton("保存", QDialogButtonBox.ButtonRole.AcceptRole)
        self.ok_btn.setObjectName("Primary")
        self.ok_btn.setDefault(True)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.time_edit.setFocus()
        self.time_edit.setSelectedSection(QTimeEdit.Section.HourSection)
        w = 440  # 自动换行的说明文字：按固定宽度算出需要的高度，避免被截断
        self.resize(w, max(self.sizeHint().height(), lay.heightForWidth(w)))

    def time_str(self) -> str:
        return self.time_edit.time().toString("HH:mm")
