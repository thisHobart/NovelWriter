"""Configuration persistence and real dialog/main-window interactions, offline."""
import json
import os
import threading

import httpx
import pytest
from openai import OpenAI

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit

from core.generation import api_providers as api, api_settings as settings
from core.gui import theme
from core.gui.main_window import MainWindow
from core.gui.model_config_dialog import ModelConfigDialog
from core.gui import model_config_dialog as dialog_module
from core.generation.api_probe import ProbeResult


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "settings_file", lambda: tmp_path / "llm_settings.json")
    for name in {name for variables in settings.VARIABLES.values() for name in variables if name} | {
        "HOSTED_LLM_URL", "HOSTED_LLM_PORT",
    }:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.build_qss())
    return app


def test_saved_settings_override_environment_and_preserve_other_services(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "old-environment-key")
    settings.save_provider("deepseek", settings.DEFAULT_URLS["deepseek"], "new-key", "custom-model")
    settings.save_provider("qwen", settings.DEFAULT_URLS["qwen"], "qwen-key", "qwen-custom")
    config = api.get_config("deepseek")
    assert (config.api_key, config.model) == ("new-key", "custom-model")
    assert "new-key" not in repr(config)
    assert api.get_config("qwen").api_key == "qwen-key"
    assert os.environ["DEEPSEEK_API_KEY"] == "old-environment-key"
    assert set(json.loads(settings.settings_file().read_text())["providers"]) == {"deepseek", "qwen"}


def test_hosted_legacy_configuration_prefills_and_saves_without_key(monkeypatch):
    monkeypatch.setenv("HOSTED_LLM_URL", "127.0.0.1")
    monkeypatch.setenv("HOSTED_LLM_PORT", "23333")
    monkeypatch.setenv("HOSTED_LLM_MODEL", "local-model")
    values = settings.form_values("hosted-llm")
    assert values["base_url"] == "http://127.0.0.1:23333/v1"
    settings.save_provider("hosted-llm", **values)
    assert api.get_config("hosted-llm").api_key == "not-needed"


def test_request_uses_saved_configuration_without_restart(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "old-key")
    settings.save_provider("qwen", "https://region.example/compatible-mode/v1", "saved-key", "saved-model")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": "saved-model",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "reply"}}],
        })

    monkeypatch.setattr(api, "OpenAI", lambda **kwargs: OpenAI(
        **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ))
    assert api.send_messages("qwen", [{"role": "user", "content": "hello"}]) == "reply"
    assert str(requests[0].url) == "https://region.example/compatible-mode/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer saved-key"
    assert json.loads(requests[0].content)["model"] == "saved-model"


@pytest.mark.parametrize("url,key,model,error", [
    ("bad-url", "secret", "model", "接口地址"),
    (settings.DEFAULT_URLS["deepseek"], "", "model", "API 密钥"),
    (settings.DEFAULT_URLS["deepseek"], "secret", "", "模型名称"),
    (settings.DEFAULT_URLS["deepseek"], "secret\nline", "model", "换行"),
])
def test_invalid_form_never_writes(url, key, model, error):
    with pytest.raises(ValueError, match=error):
        settings.save_provider("deepseek", url, key, model)
    assert not settings.settings_file().exists()


def test_failed_atomic_save_preserves_working_configuration(monkeypatch):
    settings.save_provider("deepseek", settings.DEFAULT_URLS["deepseek"], "old-key", "old-model")
    before = settings.settings_file().read_bytes()

    def denied(*_):
        raise PermissionError("failure containing private data")

    monkeypatch.setattr(settings.os, "replace", denied)
    with pytest.raises(ValueError, match="写入权限") as error:
        settings.save_provider("deepseek", settings.DEFAULT_URLS["deepseek"], "new-key", "new-model")
    assert "private data" not in str(error.value)
    assert settings.settings_file().read_bytes() == before
    assert api.get_config("deepseek").api_key == "old-key"
    assert not list(settings.settings_file().parent.glob(".llm_settings-*.tmp"))


def test_corrupt_configuration_is_reported_and_not_overwritten(qapp):
    settings.settings_file().write_text('{"secret": "private-key" invalid', encoding="utf-8")
    before = settings.settings_file().read_bytes()
    assert api.configuration_status("deepseek")[0] is False
    dialog = ModelConfigDialog("deepseek")
    assert not dialog.save_button.isEnabled()
    assert "private-key" not in dialog.error_label.text()
    assert settings.settings_file().read_bytes() == before
    dialog.close()


def test_dialog_four_fields_masking_provider_drafts_and_cancel(qapp):
    dialog = ModelConfigDialog("deepseek")
    assert len(dialog.findChildren(QLineEdit)) == 3
    assert dialog.provider_combo.count() == 3
    assert dialog.url_edit.isReadOnly()
    dialog.key_edit.setText("draft-key")
    dialog.show_key.setChecked(True)
    assert dialog.key_edit.echoMode() == QLineEdit.Normal
    dialog.provider_combo.setCurrentIndex(dialog.provider_combo.findData("qwen"))
    assert not dialog.url_edit.isReadOnly()
    assert dialog.key_edit.text() == ""
    assert dialog.key_edit.echoMode() == QLineEdit.Password
    dialog.provider_combo.setCurrentIndex(dialog.provider_combo.findData("deepseek"))
    assert dialog.key_edit.text() == "draft-key"
    dialog.cancel_button.click()
    assert dialog.result() == QDialog.Rejected
    assert not settings.settings_file().exists()


def test_main_window_button_opens_dialog_saves_and_refreshes_status(qapp, tmp_path):
    window = MainWindow()
    window.parameters_page.output_dir_edit.setText(str(tmp_path))
    window._restore_model("deepseek")
    assert window.rail.key_status._text.text() == "配置不完整"
    errors = []

    def fill_dialog():
        try:
            dialog = QApplication.activeModalWidget()
            assert isinstance(dialog, ModelConfigDialog)
            assert dialog.selected_provider() == "deepseek"
            dialog.save_button.click()
            assert dialog.result() == QDialog.Rejected
            assert "API 密钥" in dialog.error_label.text()
            dialog.key_edit.setText("saved-from-gui")
            dialog.model_edit.setText("gui-model")
            dialog.save_button.click()
        except BaseException as exc:
            errors.append(exc)
            QApplication.activeModalWidget().reject()

    QTimer.singleShot(0, fill_dialog)
    window.rail.config_button.click()
    assert not errors
    assert window.rail.key_status._text.text() == "配置已就绪"
    assert all(page.model == "deepseek" for page in window.stage_pages)
    assert api.get_config("deepseek").model == "gui-model"
    window.close()


def test_configuration_button_locked_for_running_task(qapp, tmp_path):
    window = MainWindow()
    window.parameters_page.output_dir_edit.setText(str(tmp_path))
    release = threading.Event()

    def work(report):
        release.wait(3)

    try:
        window._on_task_requested(work, None, "测试…", None)
        assert not window.rail.config_button.isEnabled()
        release.set()
        for _ in range(200):
            qapp.processEvents()
            if window.runner.current is None:
                break
            QTest.qWait(10)
        assert not window.runner.busy
        assert window.rail.config_button.isEnabled()
    finally:
        release.set()
        window.close()


def _finish_probe(qapp, dialog):
    for _ in range(300):
        qapp.processEvents()
        if dialog._test_runner.current is None:
            return
        QTest.qWait(10)
    raise AssertionError("probe did not finish")


@pytest.mark.parametrize("failure", [False, True])
def test_probe_uses_unsaved_form_on_worker_and_restores_controls(qapp, monkeypatch, failure):
    settings.save_provider("deepseek", settings.DEFAULT_URLS["deepseek"], "saved-key", "saved-model")
    before = settings.settings_file().read_bytes()
    dialog = ModelConfigDialog("qwen")
    dialog.url_edit.setText("https://draft.example/v1")
    dialog.key_edit.setText("draft-key")
    dialog.model_edit.setText("draft-model")
    dialog.show()
    release = threading.Event()
    calls = []

    def probe(config):
        calls.append((config, threading.get_ident()))
        release.wait(3)
        if failure:
            raise ValueError("HTTP 401：密钥验证失败")
        return ProbeResult("returned-model", 0.2, 8)

    monkeypatch.setattr(dialog_module, "probe_connection", probe)
    try:
        dialog.test_button.click()
        assert not dialog.provider_combo.isEnabled()
        assert not dialog.save_button.isEnabled()
        assert not dialog.test_button.isEnabled()
        assert not dialog.cancel_button.isEnabled()
        assert dialog.test_status.property("state") == "running"
        dialog._test_connection()  # Repeated requests must be rejected even programmatically.
        dialog.reject()
        assert dialog.isVisible()
        release.set()
        _finish_probe(qapp, dialog)
        assert len(calls) == 1
        config, worker_thread = calls[0]
        assert config == api.APIConfig("qwen", "https://draft.example/v1", "draft-model", "draft-key")
        assert worker_thread != threading.get_ident()
        assert settings.settings_file().read_bytes() == before
        assert dialog.save_button.isEnabled()
        assert dialog.test_button.isEnabled()
        assert dialog.cancel_button.isEnabled()
        assert dialog.test_status.property("state") == ("error" if failure else "success")
        assert ("密钥验证失败" if failure else "8 tokens") in dialog.test_status.text()
        if not failure:
            assert "请求模型：draft-model" in dialog.test_status.text()
            assert "接口返回模型：returned-model" in dialog.test_status.text()
        dialog.model_edit.setText("changed-model")
        assert dialog.test_status.isHidden()
    finally:
        release.set()
        _finish_probe(qapp, dialog)
        dialog.close()


def test_probe_validates_locally_without_network_or_saving(qapp, monkeypatch):
    calls = []
    monkeypatch.setattr(dialog_module, "probe_connection", lambda config: calls.append(config))
    dialog = ModelConfigDialog("deepseek")
    dialog.key_edit.clear()
    dialog.test_button.click()
    assert "API 密钥" in dialog.error_label.text()
    assert calls == []
    assert dialog._test_runner.current is None
    assert not settings.settings_file().exists()
    dialog.close()
