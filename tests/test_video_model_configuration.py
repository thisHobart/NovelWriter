"""The video workbench reuses the novel model configuration and SDK settings."""

import json
import threading
from types import SimpleNamespace

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton

from core.generation import api_providers, api_settings
from core.gui.model_config_dialog import ModelConfigDialog
from test_model_config_dialog import isolated_settings
from test_short_video_ui import enter, qapp, video_window, wait
from test_video_v2_workflow import ARTICLE, sample


def open_model_dialog(button, edit):
    errors = []

    def fill():
        dialog = QApplication.activeModalWidget()
        try:
            assert isinstance(dialog, ModelConfigDialog)
            edit(dialog)
        except BaseException as exc:
            errors.append(exc)
            dialog.reject()

    QTimer.singleShot(0, fill)
    button.click()
    assert not errors, errors


def save_qwen(dialog):
    dialog.provider_combo.setCurrentIndex(dialog.provider_combo.findData("qwen"))
    dialog.url_edit.setText("https://video-model.example/v1")
    dialog.key_edit.setText("video-test-key")
    dialog.model_edit.setText("video-configured-model")
    assert dialog.key_edit.echoMode() == QLineEdit.Password
    assert dialog.test_button.isEnabled()
    dialog.save_button.click()


def test_video_config_button_saves_shared_settings_used_by_generation_and_chat(
    qapp, video_window, monkeypatch
):
    window = video_window
    window._restore_model("deepseek")
    workbench = enter(window)
    assert workbench.model_config_button.isVisible()
    open_model_dialog(workbench.model_config_button, save_qwen)
    assert workbench.model() == window.selected_model() == "qwen"
    assert "video-configured-model" in workbench.model_config_button.toolTip()
    assert all(page.model == "qwen" for page in window.stage_pages)
    saved = api_settings.settings_file().read_text(encoding="utf-8")
    assert json.loads(saved)["providers"]["qwen"]["model"] == "video-configured-model"

    connections, requests = [], []

    class Client:
        def __init__(self, **options):
            connections.append(options)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def create(self, **params):
            requests.append(params)
            prompt = params["messages"][0]["content"]
            if prompt.startswith("自动审阅"):
                content = '{"issues": []}'
            elif "阶段：novel_copy" in prompt:
                content = json.dumps(sample(workbench.repository, "novel_copy"), ensure_ascii=False)
            else:
                content = "根据当前文章讨论开头。"
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    monkeypatch.setattr(api_providers, "AsyncOpenAI", Client)
    workbench.source.article.setPlainText(ARTICLE)
    workbench.source.generate_button.click()
    wait(qapp, lambda: window.runner.current is None)
    assert workbench.novel.record is not None, window.status._context.text()
    workbench.chat.input.setPlainText("讨论这个开头")
    workbench.chat.send_button.click()
    wait(qapp, lambda: window.runner.current is None)
    assert len(requests) == 3
    assert all(request["model"] == "video-configured-model" for request in requests)
    assert all(
        connection["base_url"] == "https://video-model.example/v1" for connection in connections
    )
    assert all(connection["api_key"] == "video-test-key" for connection in connections)
    assert "video-test-key" not in workbench.model_config_button.toolTip()
    window.switch_mode("novel")
    assert window.selected_model() == "qwen"


def test_project_settings_cannot_restore_the_old_model_after_configuration(video_window):
    window = video_window
    window._restore_model("deepseek")
    workbench = enter(window)
    errors = []

    def configure_in_project_settings():
        dialog = QApplication.activeModalWidget()
        try:
            assert dialog.windowTitle() == "项目设置"
            button = next(
                button for button in dialog.findChildren(QPushButton) if button.text() == "模型配置"
            )
            open_model_dialog(button, save_qwen)
            assert any(
                "video-configured-model" in label.text() for label in dialog.findChildren(QLabel)
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            dialog.reject()

    QTimer.singleShot(0, configure_in_project_settings)
    workbench.project_settings()
    assert not errors, errors
    assert workbench.model() == "qwen"
    assert api_providers.get_config("qwen").model == "video-configured-model"


def test_cancel_preserves_shared_configuration_and_mode_changes_refresh_button(video_window):
    api_settings.save_provider(
        "deepseek", api_settings.DEFAULT_URLS["deepseek"], "saved-key", "saved-model"
    )
    window = video_window
    window._restore_model("deepseek")
    workbench = enter(window)
    original = api_settings.settings_file().read_bytes()

    def cancel(dialog):
        dialog.provider_combo.setCurrentIndex(dialog.provider_combo.findData("qwen"))
        dialog.key_edit.setText("discarded-key")
        dialog.model_edit.setText("discarded-model")
        dialog.cancel_button.click()

    open_model_dialog(workbench.model_config_button, cancel)
    assert workbench.model() == "deepseek"
    assert api_settings.settings_file().read_bytes() == original
    window.switch_mode("novel")
    api_settings.save_provider(
        "qwen", api_settings.DEFAULT_URLS["qwen"], "shared-key", "shared-model"
    )
    window._restore_model("qwen")
    assert "shared-model" in workbench.model_config_button.toolTip()
    window.switch_mode("video")
    assert workbench.model() == "qwen"


def test_video_model_configuration_is_locked_during_a_request(qapp, video_window, monkeypatch):
    api_settings.save_provider(
        "deepseek", api_settings.DEFAULT_URLS["deepseek"], "saved-key", "saved-model"
    )
    window = video_window
    window._restore_model("deepseek")
    workbench = enter(window)
    release, entered = threading.Event(), threading.Event()

    def respond(prompt):
        entered.set()
        release.wait(5)
        return "已返回讨论回复"

    workbench.transport = respond
    workbench.chat.input.setPlainText("讨论选题")
    workbench.chat.send_button.click()
    try:
        wait(qapp, entered.is_set)
        assert not workbench.model_config_button.isEnabled()

        def reject_configuration(*args):
            raise AssertionError("must not open while running")

        monkeypatch.setattr("core.gui.main_window.ModelConfigDialog", reject_configuration)
        workbench.model_config_button.click()
        window._configure_model()
    finally:
        release.set()
        wait(qapp, lambda: window.runner.current is None)
    assert workbench.model_config_button.isEnabled()
