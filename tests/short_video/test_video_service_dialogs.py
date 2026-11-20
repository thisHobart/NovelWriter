# -*- coding: utf-8 -*-
"""The short video's own service dialogs: review model and image API."""
import os

import pytest

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过界面冒烟测试")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core.gui import theme  # noqa: E402


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    instance.setStyleSheet(theme.build_qss())
    yield instance


def test_review_model_dialog_saves_independently_with_shared_controls(app, monkeypatch, tmp_path):
    from core.common import api_settings, api_providers
    from core.gui.model_config_dialog import ModelConfigDialog
    from core.gui.pages.short_video.workbench import REVIEW_SERVICE
    from core.short_video.v2 import service_settings

    monkeypatch.setattr(api_settings, "settings_file", lambda: tmp_path / "llm_settings.json")
    api_settings.save_provider("hosted-llm", "https://generate.test/v1", "generation-key", "generator")
    original = api_providers.get_config("hosted-llm")
    dialog = ModelConfigDialog("hosted-llm", profile=REVIEW_SERVICE)
    assert dialog.windowTitle() == "校验模型配置"
    assert dialog.key_edit.text() == "" and dialog.model_edit.text() == ""
    assert dialog.test_button.isEnabled() and dialog.fetch_models_button.isEnabled()
    dialog.url_edit.setText("https://review.test/v1")
    dialog.key_edit.setText("review-key")
    dialog.model_edit.setText("reviewer")
    dialog._save()
    assert dialog.result() == dialog.DialogCode.Accepted.value
    assert service_settings.get_review_config().model == "reviewer"
    assert api_providers.get_config("hosted-llm") == original
    dialog.close()


def test_image_configuration_is_independent_and_connection_check_never_generates(app, monkeypatch, tmp_path):
    from unittest.mock import Mock
    from core.common import api_settings, api_providers
    from core.gui import model_config_dialog
    from core.gui.pages.short_video.workbench import IMAGE_SERVICE
    from core.short_video.v2 import service_settings

    monkeypatch.setattr(api_settings, "settings_file", lambda: tmp_path / "settings.json")
    api_settings.save_provider("hosted-llm", "https://generate.test/v1", "text-key", "generator")
    service_settings.save_review("hosted-llm", "https://review.test/v1", "review-key", "reviewer")
    generation = api_providers.get_config("hosted-llm")
    review = service_settings.get_review_config()
    dialog = model_config_dialog.ModelConfigDialog("hosted-llm", profile=IMAGE_SERVICE)
    assert dialog.provider_combo.count() == 1
    assert dialog.key_edit.text() == "" and dialog.model_edit.text() == "" and dialog.url_edit.text() == ""
    dialog.url_edit.setText("https://images.test/v1")
    dialog.key_edit.setText("image-key")
    dialog.model_edit.setText("image-model")
    probe = Mock(side_effect=AssertionError("must not send a chat probe"))
    monkeypatch.setattr(model_config_dialog, "probe_connection", probe)
    fetch = Mock(side_effect=ValueError("models endpoint not supported"))
    monkeypatch.setattr(model_config_dialog, "fetch_models", fetch)
    def run(work, **kwargs):
        try:
            kwargs["on_success"](work())
        except ValueError as exc:
            kwargs["on_error"](exc)
    monkeypatch.setattr(dialog._test_runner, "run", run)
    dialog.test_button.click()
    assert "models endpoint not supported" in dialog.test_status.text()
    dialog._save()
    assert dialog.result() == dialog.DialogCode.Accepted.value
    assert service_settings.get_image_config().model == "image-model"
    assert api_providers.get_config("hosted-llm") == generation
    assert service_settings.get_review_config() == review
    assert fetch.call_args.args[0].api_key == "image-key"
    probe.assert_not_called()
    dialog.close()
