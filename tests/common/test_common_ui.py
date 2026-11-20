# -*- coding: utf-8 -*-
"""Shared interface parts: theme, the model-service dialog, background tasks."""
import os

import pytest

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过界面冒烟测试")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core.gui import theme  # noqa: E402


def test_theme_uses_readable_chinese_font_stacks():
    assert theme.SANS_STACK == '"Microsoft YaHei UI"'
    assert theme.SERIF_STACK == '"Microsoft YaHei UI"'


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    instance.setStyleSheet(theme.build_qss())
    yield instance


def test_model_configuration_read_failure_keeps_controls_disabled_and_shows_type(app, monkeypatch, caplog):
    from unittest.mock import Mock
    from core.gui import model_config_dialog

    failure = PermissionError("configuration access denied")
    monkeypatch.setattr(model_config_dialog, "form_values", Mock(side_effect=failure))
    dialog = model_config_dialog.ModelConfigDialog()

    assert dialog._load_error is True
    assert not dialog.save_button.isEnabled()
    assert not dialog.test_button.isEnabled()
    assert "PermissionError: configuration access denied" in dialog.error_label.text()
    assert caplog.records[-1].exc_info[1] is failure
    dialog.close()


def test_model_configuration_save_failure_preserves_error_type_in_feedback(app, monkeypatch, caplog):
    from unittest.mock import Mock
    from core.gui import model_config_dialog

    monkeypatch.setattr(model_config_dialog, "form_values", lambda provider, **kwargs: {
        "base_url": "https://example.test/v1", "api_key": "", "model": "test-model",
    })
    dialog = model_config_dialog.ModelConfigDialog()
    failure = PermissionError("configuration write denied")
    monkeypatch.setattr(model_config_dialog, "save_provider", Mock(side_effect=failure))

    dialog._save()

    assert "PermissionError: configuration write denied" in dialog.error_label.text()
    assert dialog.result() == model_config_dialog.QDialog.DialogCode.Rejected.value
    assert caplog.records[-1].exc_info[1] is failure
    dialog.close()


def test_background_task_reports_the_original_exception_and_full_traceback(app, caplog):
    import logging
    from unittest.mock import Mock
    from core.common.cancellation import CancelToken
    from core.gui.task_runner import BackgroundTask

    failure = AttributeError("internal task defect")
    task = BackgroundTask(Mock(side_effect=failure), CancelToken(), False, logging.getLogger("test.task"))
    errors = []
    finished = []
    task.signals.failed.connect(errors.append)
    task.signals.finished.connect(lambda: finished.append(True))

    task.run()

    assert errors == [failure]
    assert not task.running
    assert finished == [True]
    assert caplog.records[-1].exc_info[1] is failure
