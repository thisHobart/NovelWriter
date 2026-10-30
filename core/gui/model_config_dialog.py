"""Four-field model-service settings in the desktop application's visual style."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QVBoxLayout, QWidget,
)

from core.generation.api_providers import DEFAULT_API_MODEL, PROVIDER_LABELS
from core.generation.api_settings import form_values, save_provider, validate_provider
from core.generation.api_probe import probe_connection
from core.gui.task_runner import TaskRunner
from core.gui.widgets.card import Card
from core.gui.widgets.controls import LabeledRow, hint_label, primary_button, secondary_button
from core.gui.widgets.page_header import PageHeader


class ModelConfigDialog(QDialog):
    def __init__(self, provider: str = DEFAULT_API_MODEL, parent=None):
        super().__init__(parent)
        self.setObjectName("ModelConfigDialog")
        self.setWindowTitle("模型服务配置")
        self.setMinimumWidth(620)
        self.resize(640, 400)
        self._drafts = {}
        self._current = None
        self._load_error = False
        self._test_runner = TaskRunner()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader("模型服务配置", "填写当前服务的连接信息，保存后立即生效"))
        body = QVBoxLayout()
        body.setContentsMargins(24, 20, 24, 20)
        body.setSpacing(12)
        card = Card("连接设置")
        grid = QGridLayout()
        rows = LabeledRow(grid)
        self.provider_combo = QComboBox()
        for key, label in PROVIDER_LABELS.items():
            self.provider_combo.addItem(label, key)
        self.url_edit = QLineEdit()
        self.url_edit.setClearButtonEnabled(True)
        self.key_edit = QLineEdit()
        self.key_edit.setClearButtonEnabled(True)
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.show_key = QCheckBox("显示")
        self.show_key.toggled.connect(lambda shown: self.key_edit.setEchoMode(
            QLineEdit.Normal if shown else QLineEdit.Password))
        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(12)
        key_layout.addWidget(self.key_edit, 1)
        key_layout.addWidget(self.show_key)
        self.model_edit = QLineEdit()
        self.model_edit.setClearButtonEnabled(True)
        rows.add("服务商", self.provider_combo)
        rows.add("接口地址", self.url_edit)
        rows.add("API 密钥", key_row)
        rows.add("模型", self.model_edit)
        card.body.addLayout(grid)
        body.addWidget(card)
        body.addWidget(hint_label("每个服务分别保存配置；hosted-llm 无需鉴权时可留空密钥。"))
        self.error_label = QLabel()
        self.error_label.setObjectName("FormError")
        self.error_label.setTextFormat(Qt.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        body.addWidget(self.error_label)
        self.test_status = QLabel()
        self.test_status.setObjectName("ProbeStatus")
        self.test_status.setTextFormat(Qt.PlainText)
        self.test_status.setWordWrap(True)
        self.test_status.hide()
        body.addWidget(self.test_status)
        body.addStretch(1)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.test_button = secondary_button("测试连接")
        self.test_button.setAutoDefault(False)
        self.test_button.setToolTip("测试当前填写的配置，无需先保存；仅发送一次极短请求，输出上限 2 tokens")
        self.test_button.clicked.connect(self._test_connection)
        actions.addWidget(self.test_button)
        actions.addStretch(1)
        self.cancel_button = secondary_button("取消")
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.reject)
        self.save_button = primary_button("保存配置")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._save)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.save_button)
        body.addLayout(actions)
        layout.addLayout(body, 1)
        self.provider_combo.setCurrentIndex(max(0, self.provider_combo.findData(provider)))
        self.provider_combo.currentIndexChanged.connect(self._switch_provider)
        for edit in (self.url_edit, self.key_edit, self.model_edit):
            edit.textChanged.connect(self.test_status.hide)
        self._switch_provider()

    def selected_provider(self) -> str:
        return self.provider_combo.currentData()

    def _values(self) -> dict:
        return {"base_url": self.url_edit.text(), "api_key": self.key_edit.text(),
                "model": self.model_edit.text()}

    def _switch_provider(self, *_):
        if self._current is not None and not self._load_error:
            self._drafts[self._current] = self._values()
        self._current = self.selected_provider()
        self.show_key.setChecked(False)
        self.error_label.hide()
        self.test_status.hide()
        self._load_error = False
        try:
            values = self._drafts.get(self._current)
            if values is None:
                values = form_values(self._current)
        except ValueError as exc:
            values = {"base_url": "", "api_key": "", "model": ""}
            self._load_error = True
            self._show_error(str(exc))
        self.save_button.setEnabled(not self._load_error)
        self.test_button.setEnabled(not self._load_error)
        self.url_edit.setText(values["base_url"])
        self.url_edit.setReadOnly(self._current == "deepseek")
        self.url_edit.setToolTip("DeepSeek 官方使用固定接口地址" if self._current == "deepseek" else "填写 API 基础地址，不包含 /chat/completions")
        self.key_edit.setText(values["api_key"])
        self.key_edit.setPlaceholderText("无需鉴权时可留空" if self._current == "hosted-llm" else "填写该服务的 API 密钥")
        self.model_edit.setText(values["model"])
        self.model_edit.setPlaceholderText("填写服务端实际模型名称")

    def _show_error(self, text: str):
        self.error_label.setText(text)
        self.error_label.show()

    def _save(self):
        if self._test_runner.current is not None:
            return
        try:
            save_provider(self.selected_provider(), **self._values())
        except ValueError as exc:
            self._show_error(str(exc))
            return
        self.accept()

    def _set_test_status(self, state: str, text: str):
        self.test_status.setProperty("state", state)
        self.test_status.style().unpolish(self.test_status)
        self.test_status.style().polish(self.test_status)
        self.test_status.setText(text)
        self.test_status.show()

    def _test_connection(self):
        if self._test_runner.current is not None:
            return
        self.error_label.hide()
        self.test_status.hide()
        try:
            config = validate_provider(self.selected_provider(), **self._values())
        except ValueError as exc:
            self._show_error(str(exc))
            return
        self._set_test_status("running", "正在测试当前填写的配置…仅发送一次极短请求。")

        def success(result):
            text = (f"连接成功 · {PROVIDER_LABELS[config.provider]} · 请求模型：{config.model}"
                    f" · 耗时 {result.elapsed:.1f} 秒")
            if result.returned_model != config.model:
                text += f"\n接口返回模型：{result.returned_model}"
            if result.total_tokens is not None:
                text += f" · 接口报告用量 {result.total_tokens} tokens"
            self._set_test_status("success", text)

        self._test_runner.run(
            lambda: probe_connection(config),
            disable=[self.provider_combo, self.url_edit, self.key_edit, self.model_edit,
                     self.save_button, self.cancel_button],
            busy_button=self.test_button, busy_text="测试中…",
            on_success=success,
            on_error=lambda exc: self._set_test_status("error", f"连接测试失败：{exc}"),
        )

    def reject(self):
        # Keep the dialog's callbacks alive until the short request finishes.
        if self._test_runner.current is None:
            super().reject()

    def closeEvent(self, event):
        if self._test_runner.current is not None:
            event.ignore()
        else:
            super().closeEvent(event)
