"""「自选股/持仓…」编辑对话框。"""

from typing import Callable

import shiboken6
from PySide6.QtCore import QItemSelection, QItemSelectionModel, Qt
from PySide6.QtGui import QColor, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
                               QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from . import logic, stock_names, theme
from .tasks import Task

COLUMNS = [  # (字段, 表头, 宽度, 右对齐)
    ("code", "代码", 90, False),
    ("name", "名称", 120, False),
    ("shares", "持仓股数", 100, True),
    ("cost", "成本价", 90, True),
    ("note", "备注", 0, False),
]
COL = {key: i for i, (key, *_rest) in enumerate(COLUMNS)}


class WatchlistTable(QTableWidget):
    """支持从 Excel / 文本粘贴多行（代码、名称、股数、成本、备注，用 Tab 或逗号分隔）。

    按键习惯同 Excel：Delete / Backspace 清空选中的单元格；Ctrl+Delete 删除选中的行。
    """

    def keyPressEvent(self, event):
        editing = self.state() == QAbstractItemView.State.EditingState
        if event.matches(QKeySequence.StandardKey.Paste) and not editing:
            text = QGuiApplication.clipboard().text()
            if text.strip():
                self.parent_dialog().paste_text(text)
                return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and not editing:
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier and event.key() == Qt.Key.Key_Delete:
                self.parent_dialog().delete_selected()
            else:
                self.parent_dialog().clear_selected_cells()
            return
        super().keyPressEvent(event)

    def parent_dialog(self) -> "WatchlistDialog":
        w = self.parent()
        while w is not None and not isinstance(w, WatchlistDialog):
            w = w.parent()
        return w


class WatchlistDialog(QDialog):
    def __init__(self, parent, items: list[dict],
                 save_fn: Callable[[list[dict]], dict] = logic.save_watchlist,
                 lookup_fn: Callable = stock_names.lookup, notice: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("自选股 / 持仓")
        self._save_fn = save_fn
        self._lookup_fn = lookup_fn
        self._lookup_task: Task | None = None
        self._dirty = False
        self.saved_items: list[dict] | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)
        hint = QLabel("代码为 6 位数字（如 600519）；名称可以留空，点「补全名称」自动查询。"
                      "持仓股数留空或为 0 表示只关注、不持仓。支持从 Excel 复制多行后按 Ctrl+V 粘贴；"
                      "Delete 清空单元格，Ctrl+Delete 删除整行。")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        tools = QHBoxLayout()
        tools.setSpacing(6)
        self.btn_add = QPushButton("＋ 添加")
        self.btn_del = QPushButton("删除选中")
        self.btn_up = QPushButton("上移")
        self.btn_down = QPushButton("下移")
        self.btn_names = QPushButton("补全名称")
        self.btn_del.setToolTip("删除选中单元格所在的行（Ctrl+Delete）")
        for b in (self.btn_add, self.btn_del, self.btn_up, self.btn_down):
            tools.addWidget(b)
        tools.addStretch(1)
        tools.addWidget(self.btn_names)
        lay.addLayout(tools)

        self.table = WatchlistTable(0, len(COLUMNS), self)
        self.table.setHorizontalHeaderLabels([c[1] for c in COLUMNS])
        header = self.table.horizontalHeader()
        for i, (_k, _l, width, _r) in enumerate(COLUMNS):
            if width:
                self.table.setColumnWidth(i, width)
        header.setSectionResizeMode(COL["note"], QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(30)
        # 按单元格选择（同 Excel）：Delete 只清空选中的单元格；点左侧行号可选中整行
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed
                                   | QAbstractItemView.EditTrigger.AnyKeyPressed
                                   | QAbstractItemView.EditTrigger.SelectedClicked)
        self.table.setAlternatingRowColors(False)
        lay.addWidget(self.table, 1)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(20)
        lay.addWidget(self.status)

        buttons = QDialogButtonBox()
        self.btn_save = buttons.addButton("保存", QDialogButtonBox.ButtonRole.AcceptRole)
        self.btn_save.setObjectName("Primary")
        self.btn_save.setDefault(True)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self.btn_add.clicked.connect(lambda: self.add_row())
        self.btn_del.clicked.connect(self.delete_selected)
        self.btn_up.clicked.connect(lambda: self.move_selected(-1))
        self.btn_down.clicked.connect(lambda: self.move_selected(1))
        self.btn_names.clicked.connect(self.fill_names)

        self.set_rows([{
            "code": it.get("code", ""), "name": it.get("name", ""), "shares": logic.fmt_amount(it.get("shares")),
            "cost": logic.fmt_amount(it.get("cost")), "note": it.get("note", ""),
        } for it in items])
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self._update_buttons()
        if notice:  # 例如：配置文件损坏，正在显示默认示例
            self._set_status(notice, error=True)
        self.resize(780, 500)

    # ------------------------------------------------------------ 表格读写
    def _make_item(self, key: str, text: str) -> QTableWidgetItem:
        it = QTableWidgetItem(str(text))
        if COLUMNS[COL[key]][3]:
            it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return it

    def set_rows(self, rows: list[dict]) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for key, *_rest in COLUMNS:
                self.table.setItem(r, COL[key], self._make_item(key, row.get(key, "")))
        self.table.blockSignals(False)
        self.validate(show=False)

    def rows(self) -> list[dict]:
        out = []
        for r in range(self.table.rowCount()):
            row = {}
            for key, *_rest in COLUMNS:
                it = self.table.item(r, COL[key])
                row[key] = it.text().strip() if it else ""
            out.append(row)
        return out

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        self._dirty = True
        if item.column() == COL["code"]:  # 'sh600519' / '600519.SH' 统一显示成 600519
            code = logic.parse_code(item.text())
            if code and code != item.text():
                self.table.blockSignals(True)
                item.setText(code)
                self.table.blockSignals(False)
        self.validate(show=False)

    # ------------------------------------------------------------ 编辑操作
    def add_row(self, values: dict | None = None) -> int:
        r = self.table.rowCount()
        self.table.blockSignals(True)
        self.table.insertRow(r)
        for key, *_rest in COLUMNS:
            self.table.setItem(r, COL[key], self._make_item(key, (values or {}).get(key, "")))
        self.table.blockSignals(False)
        self._dirty = True
        if values is None:
            self.table.setCurrentCell(r, COL["code"])
            self.table.editItem(self.table.item(r, COL["code"]))
        else:
            self.validate(show=False)
        self._update_buttons()
        return r

    def selected_rows(self) -> list[int]:
        return sorted({i.row() for i in self.table.selectedIndexes()})

    def clear_selected_cells(self) -> int:
        """清空选中单元格的内容（行保留），返回清空的单元格数。"""
        items = [it for it in self.table.selectedItems() if it.text()]
        if not items:
            return 0
        self.table.blockSignals(True)
        for it in items:
            it.setText("")
        self.table.blockSignals(False)
        self._dirty = True
        self.validate(show=False)
        return len(items)

    def delete_selected(self) -> None:
        rows = self.selected_rows()
        if not rows:
            return
        for r in reversed(rows):
            self.table.removeRow(r)
        self._dirty = True
        self.validate(show=False)
        self._update_buttons()

    def move_selected(self, step: int) -> None:
        rows = self.selected_rows()
        if not rows:
            return
        data = self.rows()
        n = len(data)
        if (step < 0 and rows[0] == 0) or (step > 0 and rows[-1] == n - 1):
            return
        order = list(range(n))
        for r in (rows if step < 0 else reversed(rows)):
            order[r], order[r + step] = order[r + step], order[r]
        col = max(0, self.table.currentColumn())
        self.set_rows([data[i] for i in order])
        model = self.table.model()
        sel = QItemSelection()
        for r in rows:
            sel.select(model.index(r + step, 0), model.index(r + step, len(COLUMNS) - 1))
        sm = self.table.selectionModel()
        sm.setCurrentIndex(model.index(rows[0] + step, col), QItemSelectionModel.SelectionFlag.NoUpdate)
        sm.select(sel, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        self._dirty = True
        self._update_buttons()

    def paste_text(self, text: str) -> int:
        """粘贴：单个值填入当前单元格；多行/多列（Tab 或逗号分隔：代码、名称、股数、成本、备注）
        追加为新行，不覆盖已有内容。返回新增/修改的行数。"""
        lines = [ln for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
        if not lines:
            return 0
        rows = [ln.split("\t") if "\t" in ln else ln.replace("，", ",").split(",") for ln in lines]
        self.table.blockSignals(True)
        if len(rows) == 1 and len(rows[0]) == 1 and self.table.currentItem() is not None:
            self.table.currentItem().setText(rows[0][0].strip())
        else:
            for cells in rows:
                r = self.table.rowCount()
                self.table.insertRow(r)
                for key, *_rest in COLUMNS:
                    val = cells[COL[key]].strip() if COL[key] < len(cells) else ""
                    self.table.setItem(r, COL[key], self._make_item(key, val))
        self.table.blockSignals(False)
        for r in range(self.table.rowCount()):  # 统一代码格式
            it = self.table.item(r, COL["code"])
            code = logic.parse_code(it.text())
            if code and code != it.text():
                self.table.blockSignals(True)
                it.setText(code)
                self.table.blockSignals(False)
        self._dirty = True
        self.validate(show=False)
        self._update_buttons()
        return len(rows)

    def _update_buttons(self) -> None:
        rows = self.selected_rows()
        n = self.table.rowCount()
        self.btn_del.setEnabled(bool(rows))
        self.btn_up.setEnabled(bool(rows) and rows[0] > 0)
        self.btn_down.setEnabled(bool(rows) and rows[-1] < n - 1)
        self.btn_names.setEnabled(self._lookup_task is None and n > 0)

    # ------------------------------------------------------------ 校验
    def validate(self, show: bool = True) -> tuple[list[dict], list[tuple[int, str, str]]]:
        items, errors = logic.validate_rows(self.rows())
        t = theme.current()
        bad = {(r, COL[f]): msg for r, f, msg in errors}
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            for c in range(len(COLUMNS)):
                it = self.table.item(r, c)
                if it is None:
                    continue
                if (r, c) in bad:
                    it.setBackground(QColor(t["up_soft"]))
                    it.setForeground(QColor(t["up"]))
                    it.setToolTip(bad[(r, c)])
                else:
                    it.setData(Qt.ItemDataRole.BackgroundRole, None)
                    it.setData(Qt.ItemDataRole.ForegroundRole, None)
                    it.setToolTip("")
        self.table.blockSignals(False)
        if errors:
            r, f, msg = errors[0]
            more = f"（共 {len(errors)} 处）" if len(errors) > 1 else ""
            self._set_status(f"第 {r + 1} 行「{COLUMNS[COL[f]][1]}」：{msg}{more}", error=True)
        elif not show and self.status.property("kind") == "error":
            self._set_status("")
        return items, errors

    def _set_status(self, text: str, error: bool = False) -> None:
        t = theme.current()
        self.status.setProperty("kind", "error" if error else "info")
        self.status.setStyleSheet(f"color: {t['up'] if error else t['muted']};")
        self.status.setText(text)

    # ------------------------------------------------------------ 补全名称
    def fill_names(self) -> None:
        if self._lookup_task is not None:
            return
        codes = []
        for row in self.rows():
            code = logic.parse_code(row["code"])
            if code and not row["name"]:
                codes.append(code)
        if not codes:
            any_code = any(logic.parse_code(r["code"]) for r in self.rows())
            self._set_status("所有股票都已填写名称" if any_code else "请先填写股票代码")
            return
        self._set_status(f"正在查询 {len(codes)} 只股票的名称…（新浪行情不可用时会改为下载交易所股票名单，可能需要几十秒）")
        self._lookup_task = Task(self._lookup_fn, codes, on_done=self._names_done,
                                 on_error=self._names_failed, name="stock_names")
        self._update_buttons()
        self._lookup_task.start()

    def _names_done(self, res: dict) -> None:
        if not shiboken6.isValid(self):
            return
        self._lookup_task = None
        names = res.get("names", {})
        filled = 0
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            code = logic.parse_code(self.table.item(r, COL["code"]).text())
            name_item = self.table.item(r, COL["name"])
            if code in names and not name_item.text().strip():
                name_item.setText(names[code])
                filled += 1
        self.table.blockSignals(False)
        if filled:
            self._dirty = True
        self.validate(show=False)
        missing = res.get("missing") or []
        if not names and res.get("errors"):
            self._set_status("查询失败：" + "、".join(res["errors"]) + "。请检查网络后重试，或手动填写名称。", error=True)
        else:
            text = f"已补全 {filled} 只股票的名称"
            if res.get("sources"):
                text += f"（数据来源：{'、'.join(res['sources'])}）"
            if missing:
                text += f"；未查到：{'、'.join(missing)}，请确认代码是否正确"
            self._set_status(text, error=bool(missing))
        self._update_buttons()

    def _names_failed(self, exc: BaseException) -> None:
        if not shiboken6.isValid(self):
            return
        self._lookup_task = None
        self._set_status(f"查询失败：{exc}。请检查网络后重试，或手动填写名称。", error=True)
        self._update_buttons()

    # ------------------------------------------------------------ 保存/取消
    def save(self) -> None:
        focus = QApplication.focusWidget()
        if focus is not None and self.table.isAncestorOf(focus):  # 让正在编辑的单元格先提交
            self.btn_save.setFocus()
        items, errors = self.validate(show=True)
        if errors:
            r, f, _msg = errors[0]
            self.table.setCurrentCell(r, COL[f])
            QMessageBox.warning(self, "请检查输入", f"有 {len(errors)} 处输入需要修改（已用红色标出）：\n\n"
                                + "\n".join(f"第 {r + 1} 行「{COLUMNS[COL[f]][1]}」：{m}" for r, f, m in errors[:8]))
            return
        try:
            cfg = self._save_fn(items)
        except Exception as e:  # 文件被占用、无写权限等
            QMessageBox.critical(self, "保存失败", f"配置文件保存失败：{e}")
            return
        self.saved_items = list(cfg.get("watchlist", items)) if isinstance(cfg, dict) else items
        self._dirty = False
        self.accept()

    def reject(self) -> None:
        if self._dirty:
            ans = QMessageBox.question(self, "放弃修改", "自选股有修改还没有保存，确定放弃吗？",
                                       QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                                       QMessageBox.StandardButton.Cancel)
            if ans != QMessageBox.StandardButton.Discard:
                return
        super().reject()
