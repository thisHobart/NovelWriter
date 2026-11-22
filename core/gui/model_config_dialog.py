"""Four-field model-service settings in the desktop application's visual style.

The generation dialog also keeps third-party services the person names: each keeps its own
address, key and model, so switching back to one needs no typing again."""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from core.common.api_models import fetch_models
from core.common.api_probe import ProbeResult, probe_connection
from core.common.api_providers import DEFAULT_API_MODEL, PROVIDER_LABELS
from core.common.api_settings import (
    check_service_name,
    checked_named_services,
    form_values,
    named_services,
    save_named_services,
    save_provider,
    validate_provider,
)
from core.gui.task_runner import TaskRunner
from core.gui.widgets.card import Card
from core.gui.widgets.controls import LabeledRow, hint_label, primary_button, secondary_button
from core.gui.widgets.page_header import PageHeader


@dataclass(frozen=True)
class ServiceProfile:
    """What one model-service dialog edits: its title, where the values live, how to check them.

    ``load(provider)`` returns the three fields; ``save(provider, base_url, api_key, model)``
    stores them. ``only_provider`` is ``(key, label)`` when a single kind of service fits;
    ``list_models_to_check`` makes the check button only list models, never send a request.
    ``load_named()`` / ``save_named(items, active)``, when given, put the person's own named
    third-party services in the 服务商 list in place of the single 第三方 API.
    """

    title: str
    subtitle: str
    load: Callable[[str], dict[str, str]]
    save: Callable[..., None]
    only_provider: tuple[str, str] | None = None
    list_models_to_check: bool = False
    load_named: Callable[[], dict | None] | None = None
    save_named: Callable[..., None] | None = None


# Looked up when called, so the shared settings functions can be swapped in tests.
GENERATION = ServiceProfile(
    "模型服务配置", "填写当前服务的连接信息，保存后立即生效",
    load=lambda provider: form_values(provider),
    save=lambda provider, **values: save_provider(provider, **values),
    load_named=lambda: named_services(),
    save_named=lambda items, active: save_named_services(items, active),
)
ADD_SERVICE = "add"
NAMED_KEY = "service:"
NAMED_HINT = ("服务商下拉框最后一项可添加 OpenAI 兼容的第三方服务并自己命名；每个服务的地址、密钥和模型"
              "保存后都会留着，之后直接切换。无需鉴权时密钥可留空；获取后可选择模型，也可手动输入。")


class ModelConfigDialog(QDialog):
    def __init__(self, provider: str = DEFAULT_API_MODEL, parent: QWidget | None = None,
                 *, profile: ServiceProfile = GENERATION) -> None:
        super().__init__(parent)
        self.profile = profile
        title = profile.title
        self.setObjectName("ModelConfigDialog")
        self.setWindowTitle(title)
        self.setMinimumWidth(620)
        self.resize(640, 400)
        self._drafts = {}
        self._current = None
        self._load_error = False
        self._names: dict[str, str] = {}
        self._next_service = 0
        self._active_name: str | None = None
        self._named_error: Exception | None = None
        self._test_runner = TaskRunner()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader(title, profile.subtitle))
        body = QVBoxLayout()
        body.setContentsMargins(24, 20, 24, 20)
        body.setSpacing(12)
        card = Card("连接设置")
        grid = QGridLayout()
        rows = LabeledRow(grid)
        self.provider_combo = QComboBox()
        self.rename_button = secondary_button("重命名")
        self.delete_button = secondary_button("删除")
        if profile.only_provider:
            self.provider_combo.addItem(profile.only_provider[1], profile.only_provider[0])
        elif profile.load_named is not None:
            self._fill_named_services()
        else:
            for key, label in PROVIDER_LABELS.items():
                self.provider_combo.addItem(label, key)
        provider_row = QWidget()
        provider_layout = QHBoxLayout(provider_row)
        provider_layout.setContentsMargins(0, 0, 0, 0)
        provider_layout.setSpacing(12)
        provider_layout.addWidget(self.provider_combo, 1)
        for button, tip, slot in (
            (self.rename_button, "给当前的第三方服务改名", self._rename_service),
            (self.delete_button, "从列表里删掉当前的第三方服务，点「保存配置」后生效", self._delete_service),
        ):
            button.setAutoDefault(False)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            button.setVisible(self._named_mode())
            provider_layout.addWidget(button)
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
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QComboBox.NoInsert)
        self.model_combo.setMaxVisibleItems(12)
        self.model_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.model_combo.setMinimumContentsLength(18)
        self.model_combo.completer().setCompletionMode(QCompleter.PopupCompletion)
        self.model_combo.completer().setFilterMode(Qt.MatchContains)
        self.model_combo.completer().setCaseSensitivity(Qt.CaseInsensitive)
        self.model_edit = self.model_combo.lineEdit()
        self.model_edit.setClearButtonEnabled(True)
        self.model_dropdown_action = self.model_edit.addAction(
            self.style().standardIcon(QStyle.SP_ArrowDown), QLineEdit.TrailingPosition,
        )
        self.model_dropdown_action.setToolTip("展开模型列表")
        self.model_dropdown_action.setEnabled(False)
        self.model_dropdown_action.triggered.connect(self._show_model_list)
        self.fetch_models_button = secondary_button("获取模型")
        self.fetch_models_button.setAutoDefault(False)
        self.fetch_models_button.setToolTip("使用当前地址和密钥查询模型列表，无需先填写模型或保存配置")
        self.fetch_models_button.clicked.connect(self._fetch_models)
        model_row = QWidget()
        model_layout = QHBoxLayout(model_row)
        model_layout.setContentsMargins(0, 0, 0, 0)
        model_layout.setSpacing(12)
        model_layout.addWidget(self.model_combo, 1)
        model_layout.addWidget(self.fetch_models_button)
        rows.add("服务商", provider_row)
        rows.add("接口地址", self.url_edit)
        rows.add("API 密钥", key_row)
        rows.add("模型", model_row)
        card.body.addLayout(grid)
        body.addWidget(card)
        body.addWidget(hint_label(
            NAMED_HINT if self._named_mode() else
            "第三方 API 支持 OpenAI 兼容服务，自行填写基础地址和密钥；无需鉴权时密钥可留空。获取后可选择模型，也可手动输入。"
        ))
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
        if profile.list_models_to_check:
            self.test_button.setText("检查连接")
            self.test_button.setToolTip("仅查询模型列表，不发送聊天或生图请求；不支持列表也可手动填模型并保存")
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
        start = provider
        if self._named_mode() and provider == "hosted-llm":
            start = self._key_of(self._active_name) or next(iter(self._named_keys()), "deepseek")
        self.provider_combo.setCurrentIndex(max(0, self.provider_combo.findData(start)))
        self.provider_combo.currentIndexChanged.connect(self._switch_provider)
        for edit in (self.url_edit, self.key_edit, self.model_edit):
            edit.textChanged.connect(self.test_status.hide)
        for edit in (self.url_edit, self.key_edit):
            edit.textChanged.connect(self._clear_models)
        self._switch_provider()

    def selected_provider(self) -> str:
        key = self.provider_combo.currentData()
        return "hosted-llm" if str(key).startswith(NAMED_KEY) else key

    def selected_name(self) -> str:
        """What the 服务商 list shows for the chosen service: a name of yours or a built-in label."""
        return self.provider_combo.currentText()

    # -- Named third-party services ------------------------------------------------------

    def _named_mode(self) -> bool:
        return self.profile.load_named is not None and not self.profile.only_provider

    def _named_keys(self) -> list[str]:
        return [key for key in (self.provider_combo.itemData(index)
                                for index in range(self.provider_combo.count()))
                if str(key).startswith(NAMED_KEY)]

    def _key_of(self, name: str | None) -> str | None:
        return next((key for key, known in self._names.items() if known == name), None)

    def _new_key(self, name: str) -> str:
        self._next_service += 1
        key = f"{NAMED_KEY}{self._next_service}"
        self._names[key] = name
        return key

    def _fill_named_services(self) -> None:
        try:
            stored = self.profile.load_named()
        except (OSError, ValueError) as exc:
            logging.getLogger(__name__).error("读取服务商列表失败", exc_info=True)
            self._named_error = exc
            stored = None
        if stored is None:
            # Before the first save: the one third-party service there was, under its old label.
            label = PROVIDER_LABELS["hosted-llm"]
            self.provider_combo.addItem(label, self._new_key(label))
            self._active_name = label
        else:
            self._active_name = stored["active"]
            for item in stored["items"]:
                key = self._new_key(item["name"])
                self._drafts[key] = {name: item[name] for name in ("base_url", "api_key", "model")}
                self.provider_combo.addItem(item["name"], key)
        for key, label in PROVIDER_LABELS.items():
            if key != "hosted-llm":
                self.provider_combo.addItem(label, key)
        self.provider_combo.insertSeparator(self.provider_combo.count())
        self.provider_combo.addItem("+ 添加服务商…", ADD_SERVICE)

    def _taken_names(self, *, besides: str | None = None) -> list[str]:
        builtin = [label for key, label in PROVIDER_LABELS.items() if key != "hosted-llm"]
        return builtin + [name for key, name in self._names.items() if key != besides]

    def ask_name(self, title: str, name: str = "") -> str | None:
        """The name typed in a small prompt, or None when cancelled."""
        text, accepted = QInputDialog.getText(self, title, "服务商名称：", QLineEdit.Normal, name)
        return text if accepted else None

    def _select(self, key: str | None) -> None:
        """Show another entry in the list without loading it."""
        self.provider_combo.blockSignals(True)
        self.provider_combo.setCurrentIndex(max(0, self.provider_combo.findData(key)))
        self.provider_combo.blockSignals(False)

    def _add_service(self) -> None:
        self._select(self._current)
        typed = self.ask_name("添加服务商")
        if typed is None:
            return
        try:
            name = check_service_name(typed, self._taken_names())
        except ValueError as exc:
            self._show_error(str(exc))
            return
        key = self._new_key(name)
        self._drafts[key] = {"base_url": "", "api_key": "", "model": ""}
        self.provider_combo.insertItem(len(self._named_keys()), name, key)
        self.provider_combo.setCurrentIndex(self.provider_combo.findData(key))
        self.url_edit.setFocus()

    def _rename_service(self) -> None:
        key = self._current
        if key not in self._names:
            return
        typed = self.ask_name("重命名服务商", self._names[key])
        if typed is None:
            return
        try:
            name = check_service_name(typed, self._taken_names(besides=key))
        except ValueError as exc:
            self._show_error(str(exc))
            return
        if self._active_name == self._names[key]:
            self._active_name = name
        self._names[key] = name
        self.provider_combo.setItemText(self.provider_combo.findData(key), name)
        self.error_label.hide()

    def _delete_service(self) -> None:
        key = self._current
        if key not in self._names:
            return
        index = self.provider_combo.findData(key)
        self._current = None
        self._drafts.pop(key, None)
        del self._names[key]
        self.provider_combo.blockSignals(True)
        self.provider_combo.removeItem(index)
        self.provider_combo.setCurrentIndex(max(0, index - 1))
        self.provider_combo.blockSignals(False)
        self._switch_provider()

    def _named_items(self) -> list[dict[str, str]]:
        items = []
        for key in self._named_keys():
            values = self._values() if key == self._current else self._drafts.get(key)
            if values is None:
                values = self.profile.load("hosted-llm")
            items.append({"name": self._names[key], **values})
        return items

    def _values(self) -> dict:
        return {"base_url": self.url_edit.text(), "api_key": self.key_edit.text(),
                "model": self.model_edit.text()}

    def _switch_provider(self, *_: object) -> None:
        if self.provider_combo.currentData() == ADD_SERVICE:
            self._add_service()
            return
        if self._current is not None and not self._load_error:
            self._drafts[self._current] = self._values()
        self._current = self.provider_combo.currentData()
        provider = self.selected_provider()
        self.show_key.setChecked(False)
        self.error_label.hide()
        self.test_status.hide()
        self._clear_models()
        self._load_error = False
        try:
            if self._named_error is not None:
                raise self._named_error
            values = self._drafts.get(self._current)
            if values is None:
                values = self.profile.load(provider)
        except (OSError, ValueError) as exc:
            logging.getLogger(__name__).error("读取模型配置失败", exc_info=True)
            values = {"base_url": "", "api_key": "", "model": ""}
            self._load_error = True
            self._show_error(f"{type(exc).__name__}: {exc}")
        self.save_button.setEnabled(not self._load_error)
        self.test_button.setEnabled(not self._load_error)
        self.fetch_models_button.setEnabled(not self._load_error)
        named = self._current in self._names
        self.rename_button.setEnabled(named and not self._load_error)
        self.delete_button.setEnabled(named and not self._load_error)
        self.url_edit.setText(values["base_url"])
        self.url_edit.setReadOnly(provider == "deepseek")
        self.url_edit.setToolTip("DeepSeek 官方使用固定接口地址" if provider == "deepseek" else "填写 API 基础地址，不包含 /chat/completions")
        self.url_edit.setPlaceholderText("例如 https://api.example.com/v1" if provider == "hosted-llm" else "")
        self.key_edit.setText(values["api_key"])
        self.key_edit.setPlaceholderText("无需鉴权时可留空" if provider == "hosted-llm" else "填写该服务的 API 密钥")
        self.model_edit.setText(values["model"])
        self.model_edit.setPlaceholderText("获取后点击箭头选择，或手动输入模型名称")

    def _clear_models(self) -> None:
        current = self.model_edit.text()
        self.model_combo.clear()
        self.model_edit.setText(current)
        self.model_dropdown_action.setEnabled(False)

    def _show_model_list(self) -> None:
        if self.isVisible() and self.model_combo.isEnabled() and self.model_combo.count():
            self.model_combo.showPopup()

    def _request_controls(self) -> list[QWidget]:
        return [self.provider_combo, self.rename_button, self.delete_button, self.url_edit,
                self.key_edit, self.model_combo, self.fetch_models_button, self.test_button,
                self.save_button, self.cancel_button]

    def _fetch_models(self) -> None:
        if self._test_runner.current is not None:
            return
        self.error_label.hide()
        self.test_status.hide()
        try:
            config = validate_provider(
                self.selected_provider(), **self._values(), require_model=False,
            )
        except ValueError as exc:
            self._show_error(str(exc))
            return
        self._set_test_status("running", "正在获取模型列表…")

        def success(model_ids: list[str]) -> None:
            current = self.model_edit.text()
            self.model_combo.clear()
            self.model_combo.addItems(model_ids)
            self.model_combo.setCurrentIndex(self.model_combo.findText(current))
            self.model_edit.setText(current)
            self.model_dropdown_action.setEnabled(bool(model_ids))
            if model_ids:
                self._set_test_status("success", f"已获取 {len(model_ids)} 个模型，请在模型下拉框中选择；也可手动输入。")
            else:
                self._set_test_status("empty", "接口未返回可用模型，可手动输入模型名称。")

        def show_fetched_models() -> None:
            if self.test_status.property("state") == "success":
                self._show_model_list()

        self._test_runner.run(
            lambda: fetch_models(config),
            disable=self._request_controls(),
            busy_button=self.fetch_models_button, busy_text="获取中…",
            on_success=success,
            on_error=lambda exc: self._set_test_status("error", f"获取模型失败：{type(exc).__name__}: {exc}"),
            on_done=show_fetched_models,
        )

    def _show_error(self, text: str) -> None:
        self.error_label.setText(text)
        self.error_label.show()

    def _save(self) -> None:
        if self._test_runner.current is not None:
            return
        try:
            named = None
            if self._named_mode():
                named = (self._named_items(), self._names.get(self._current, self._active_name))
                checked_named_services(*named)  # Nothing is written while any service is wrong.
            self.profile.save(self.selected_provider(), **self._values())
            if named is not None:
                self.profile.save_named(*named)
        except (OSError, ValueError) as exc:
            logging.getLogger(__name__).error("保存模型配置失败", exc_info=True)
            self._show_error(f"{type(exc).__name__}: {exc}")
            return
        self.accept()

    def _set_test_status(self, state: str, text: str) -> None:
        self.test_status.setProperty("state", state)
        self.test_status.style().unpolish(self.test_status)
        self.test_status.style().polish(self.test_status)
        self.test_status.setText(text)
        self.test_status.show()

    def _test_connection(self) -> None:
        if self.profile.list_models_to_check:
            self._fetch_models()
            return
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

        def success(result: ProbeResult) -> None:
            text = (f"连接成功 · {self.selected_name()} · 请求模型：{config.model}"
                    f" · 耗时 {result.elapsed:.1f} 秒")
            if result.returned_model != config.model:
                text += f"\n接口返回模型：{result.returned_model}"
            if result.total_tokens is not None:
                text += f" · 接口报告用量 {result.total_tokens} tokens"
            self._set_test_status("success", text)

        self._test_runner.run(
            lambda: probe_connection(config),
            disable=self._request_controls(),
            busy_button=self.test_button, busy_text="测试中…",
            on_success=success,
            on_error=lambda exc: self._set_test_status("error", f"连接测试失败：{type(exc).__name__}: {exc}"),
        )

    def reject(self) -> None:
        # Keep the dialog's callbacks alive until the short request finishes.
        if self._test_runner.current is None:
            super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._test_runner.current is not None:
            event.ignore()
        else:
            super().closeEvent(event)
