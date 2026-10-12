"""「AI 设置…」对话框：服务名称、接口地址、模型、API Key 环境变量名、自动点评、默认包含持仓。

保存到配置文件的 cfg['ai']（其他配置原样保留；配置文件损坏时先改名备份）。
API Key 本身不在这里填写，也不会写进配置文件：软件只从环境变量读取。
与默认值相同的项不写进配置，以后默认值更新（例如新模型）能自动生效。
"""

from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QVBoxLayout)

from . import logic, theme


class AISettingsDialog(QDialog):
    def __init__(self, parent, ai_module, cfg: dict,
                 save_fn: Callable[[dict], dict] | None = None):
        super().__init__(parent)
        self.setWindowTitle("AI 设置")
        self._ai = ai_module
        self._cfg = cfg if isinstance(cfg, dict) else {}
        self._save_fn = save_fn
        self.backup_path: Path | None = None
        self.saved: dict | None = None
        try:
            self._defaults = dict(ai_module.ai_settings({}))
        except Exception:  # noqa: BLE001
            self._defaults = {}
        s = dict(ai_module.ai_settings(self._cfg))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)
        intro = QLabel("AI 分析使用 OpenAI 兼容的对话接口，默认是 DeepSeek；也可以改成其他 OpenAI 兼容接口"
                       "（填对应的 https 接口地址、模型名，并为它单独设置一个存放 Key 的环境变量）。\n"
                       "API Key 不会保存在软件里，只从环境变量读取。")
        intro.setObjectName("Hint")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.chk_enabled = QCheckBox("启用 AI 分析")
        form.addRow("", self.chk_enabled)
        self.ed_provider = QLineEdit()
        self.ed_provider.setPlaceholderText("DeepSeek")
        form.addRow("服务名称", self.ed_provider)
        self.ed_base_url = QLineEdit()
        self.ed_base_url.setPlaceholderText("https://api.deepseek.com")
        self.ed_base_url.setToolTip("OpenAI 兼容接口的地址，软件会请求 <接口地址>/chat/completions")
        form.addRow("接口地址", self.ed_base_url)
        self.combo_model = QComboBox()
        self.combo_model.setEditable(True)
        self.combo_model.setToolTip("可以从列表里选，也可以直接输入模型名")
        form.addRow("模型", self.combo_model)

        env_row = QHBoxLayout()
        env_row.setSpacing(6)
        self.ed_env = QLineEdit()
        self.ed_env.setPlaceholderText("DEEPSEEK_API_KEY, DEEPSEEK-API")
        self.ed_env.setToolTip("存放 API Key 的环境变量名；多个用逗号分隔，按顺序尝试，使用第一个有值的")
        env_row.addWidget(self.ed_env, 1)
        self.btn_check = QPushButton("检测")
        self.btn_check.setToolTip("检查这些环境变量里有没有 API Key（只显示变量名，不显示 Key）")
        self.btn_check.clicked.connect(self.check_key)
        env_row.addWidget(self.btn_check)
        form.addRow("Key 环境变量", env_row)
        self.lbl_env = QLabel("多个变量名用逗号分隔，按顺序尝试。")
        self.lbl_env.setObjectName("Caption")
        self.lbl_env.setWordWrap(True)
        form.addRow("", self.lbl_env)

        self.chk_auto = QCheckBox("生成复盘时自动写 AI 点评")
        self.chk_auto.setToolTip("报告里会多一段「AI 复盘点评」；同一份数据只调用一次 AI（结果有缓存）")
        form.addRow("", self.chk_auto)
        self.chk_holdings = QCheckBox("默认包含持仓数据（持仓数量/成本/盈亏会发给 AI）")
        form.addRow("", self.chk_holdings)
        lay.addLayout(form)

        note = QLabel("AI 只做客观数据分析与解读，不提供投资建议；AI 生成的内容仅供参考。")
        note.setObjectName("Caption")
        note.setWordWrap(True)
        lay.addWidget(note)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setVisible(False)
        lay.addWidget(self.status)

        buttons = QDialogButtonBox()
        self.btn_reset = buttons.addButton("恢复默认", QDialogButtonBox.ButtonRole.ResetRole)
        self.btn_reset.clicked.connect(self.reset_defaults)
        self.btn_save = buttons.addButton("保存", QDialogButtonBox.ButtonRole.AcceptRole)
        self.btn_save.setObjectName("Primary")
        self.btn_save.setDefault(True)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self._models = logic.merge_models(s.get("models"), s.get("model", ""))
        self.set_values(s)
        if self._cfg.get("_config_error"):  # load_config 遇到损坏的配置文件时会改用默认配置
            self._set_status("注意：配置文件无法读取，下面显示的是默认设置；保存时会先把原文件改名备份", error=True)
        w = 540  # 自动换行的说明文字：按固定宽度算出需要的高度，避免被截断
        for lbl in (intro, note):
            lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            lbl.setMinimumHeight(lbl.heightForWidth(w - 36) + lbl.fontMetrics().lineSpacing() // 2)
        self.resize(w, max(self.sizeHint().height(), lay.heightForWidth(w)))

    # ------------------------------------------------------------ 读写界面
    def set_values(self, s: dict) -> None:
        self.chk_enabled.setChecked(bool(s.get("enabled", True)))
        self.ed_provider.setText(str(s.get("provider") or ""))
        self.ed_base_url.setText(str(s.get("base_url") or ""))
        models = logic.merge_models(s.get("models"), s.get("model", ""))
        self.combo_model.clear()
        self.combo_model.addItems(models)
        self.combo_model.setCurrentText(str(s.get("model") or ""))
        envs = s.get("api_key_env") or []
        if isinstance(envs, str):
            envs = [envs]
        self.ed_env.setText(", ".join(str(e) for e in envs))
        self.chk_auto.setChecked(bool(s.get("auto_review", True)))
        self.chk_holdings.setChecked(bool(s.get("include_holdings", False)))
        self._set_status("")

    def reset_defaults(self) -> None:
        self._models = logic.merge_models(self._defaults.get("models"), self._defaults.get("model", ""))
        self.set_values(self._defaults)
        self._set_status("已恢复默认设置，点「保存」后生效")

    def _set_status(self, text: str, error: bool = False) -> None:
        t = theme.current()
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {t['up'] if error else t['muted']};")
        self.status.setVisible(bool(text))

    def values(self) -> tuple[dict | None, str]:
        """校验并返回 (要保存的设置, 错误说明)。"""
        provider = self.ed_provider.text().strip() or str(self._defaults.get("provider") or "DeepSeek")
        base_url = logic.normalize_base_url(self.ed_base_url.text())
        if base_url is None:
            return None, "接口地址需要以 http:// 或 https:// 开头，例如 https://api.deepseek.com"
        model = self.combo_model.currentText().strip()
        if not model:
            return None, "请填写模型名，例如 deepseek-chat"
        names, bad, err = self._env_names()
        if err:
            return None, err
        problem = logic.endpoint_problem(base_url, names)
        if problem:
            return None, problem
        return {
            "enabled": self.chk_enabled.isChecked(),
            "provider": provider,
            "base_url": base_url,
            "model": model,
            "models": logic.merge_models(self._models, model),
            "api_key_env": names,
            "auto_review": self.chk_auto.isChecked(),
            "include_holdings": self.chk_holdings.isChecked(),
        }, ""

    def _env_names(self) -> tuple[list[str], list[str], str]:
        """解析“Key 环境变量”：(变量名, 不合法的, 错误说明)。误填了 Key 本身时从输入框里去掉它，绝不回显。"""
        names, bad = logic.parse_env_names(self.ed_env.text())
        if logic.SECRET_PLACEHOLDER in bad:
            self.ed_env.setText(", ".join(names))   # Key 不能留在界面上（也不会被保存）
            return names, bad, logic.SECRET_HINT
        if bad:
            return names, bad, f"环境变量名不合法：{'、'.join(bad)}（只能包含字母、数字、下划线、横线和点）"
        if not names:
            return names, bad, "请至少填写一个存放 API Key 的环境变量名，例如 DEEPSEEK_API_KEY"
        return names, bad, ""

    # ------------------------------------------------------------ 操作
    def check_key(self) -> None:
        names, bad, err = self._env_names()
        if err:
            self._set_status(err if logic.SECRET_PLACEHOLDER in bad else "请先填写正确的环境变量名", error=True)
            return
        cfg = dict(self._cfg)
        cfg["ai"] = {**(self._cfg.get("ai") if isinstance(self._cfg.get("ai"), dict) else {}), "api_key_env": names}
        try:
            st = self._ai.key_status(cfg)
        except Exception as e:  # noqa: BLE001
            self._set_status(f"检测失败：{e}", error=True)
            return
        if st.get("found"):
            self._set_status(f"已检测到 API Key：来自环境变量 {st.get('env')}")
        else:
            self._set_status(f"没有在 {'、'.join(names)} 里找到 API Key。请在系统环境变量里设置后再检测。", error=True)

    def save(self) -> None:
        vals, err = self.values()
        if vals is None:
            self._set_status(err, error=True)
            return
        # 与默认值相同的项不写进配置（None 表示删除该项），默认值以后更新时能自动生效
        to_save = {k: (None if self._defaults.get(k) == v else v) for k, v in vals.items()}

        def backup():  # 配置文件损坏时先改名备份（记下备份文件名，告诉用户）
            self.backup_path = logic.backup_corrupt_config()
            return self.backup_path

        try:
            if self._save_fn is not None:
                self.saved = self._save_fn(to_save)
            else:
                self.saved = logic.save_ai_settings(to_save, backup=backup)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", f"AI 设置保存失败：{e}")
            return
        self.accept()
