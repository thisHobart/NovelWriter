# -*- coding: utf-8 -*-
"""界面冒烟：能不能构造出来、能不能刷新、路由通不通。

需要 PySide6；没装就整体 skip，所以 CI 里不会因为缺 Qt 而红。
用 offscreen 平台，不需要显示器。
"""
import os

import pytest

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过界面冒烟测试")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core.gui import theme  # noqa: E402
from core.gui.main_window import MainWindow  # noqa: E402
from core.gui.pages import (  # noqa: E402
    ChapterWritingPage,
    LorePage,
    ParametersPage,
    ScenePlanPage,
    StructurePage,
    WorkflowPage,
)


def test_theme_uses_readable_chinese_font_stacks():
    assert theme.SANS_STACK == '"Microsoft YaHei UI"'
    assert theme.SERIF_STACK == '"Microsoft YaHei UI"'


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    instance.setStyleSheet(theme.build_qss())
    yield instance


@pytest.mark.parametrize("page_class", [
    WorkflowPage, LorePage, StructurePage, ScenePlanPage, ChapterWritingPage,
])
def test_stage_pages_construct_and_refresh(app, page_class, tmp_path):
    page = page_class()
    page.output_dir = str(tmp_path)      # 空目录：所有页面都要能画出空态
    page.refresh()
    assert page.header is not None


def test_parameters_page_round_trip(app, tmp_path):
    page = ParametersPage()
    page.output_dir_edit.setText(str(tmp_path))
    page.title_edit.setText("静默星环")
    assert page.dirty is True
    assert page.save() is True
    assert page.dirty is False

    reopened = ParametersPage()
    reopened.load(str(tmp_path))
    assert reopened.title_edit.text() == "静默星环"


def test_parameters_page_blocks_empty_title(app, tmp_path):
    page = ParametersPage()
    page.output_dir_edit.setText(str(tmp_path))
    page.title_edit.setText("")
    assert "novel_title" in page.validate()
    assert page.save() is False


def test_main_window_routes_between_pages(app):
    window = MainWindow()
    assert set(window.pages) == {
        "workflow", "parameters", "lore", "structure", "scenes", "chapters"}
    for key in window.pages:
        window._navigate(key)
        assert window.stack.currentWidget() is window.pages[key]
    # 被阻塞的阶段也允许进入，页面自己画阻塞空态
    window._on_step_selected("blocked:scenes")
    assert window.stack.currentWidget() is window.pages["scenes"]


def test_refresh_is_not_reentrant(app):
    window = MainWindow()
    calls = []
    original = window._stage_states
    window._stage_states = lambda: (calls.append(1), original())[1]
    window.refresh_all()
    assert len(calls) == 1, "一次 refresh_all 只应做一次磁盘评估"


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


def test_review_model_dialog_saves_independently_with_shared_controls(app, monkeypatch, tmp_path):
    from core.generation import api_settings, api_providers
    from core.gui.model_config_dialog import ModelConfigDialog

    monkeypatch.setattr(api_settings, "settings_file", lambda: tmp_path / "llm_settings.json")
    api_settings.save_provider("hosted-llm", "https://generate.test/v1", "generation-key", "generator")
    original = api_providers.get_config("hosted-llm")
    dialog = ModelConfigDialog("hosted-llm", purpose="review")
    assert dialog.windowTitle() == "校验模型配置"
    assert dialog.key_edit.text() == "" and dialog.model_edit.text() == ""
    assert dialog.test_button.isEnabled() and dialog.fetch_models_button.isEnabled()
    dialog.url_edit.setText("https://review.test/v1")
    dialog.key_edit.setText("review-key")
    dialog.model_edit.setText("reviewer")
    dialog._save()
    assert dialog.result() == dialog.DialogCode.Accepted.value
    assert api_settings.get_review_config().model == "reviewer"
    assert api_providers.get_config("hosted-llm") == original
    dialog.close()


def test_image_configuration_is_independent_and_connection_check_never_generates(app, monkeypatch, tmp_path):
    from unittest.mock import Mock
    from core.generation import api_settings, api_providers
    from core.gui import model_config_dialog

    monkeypatch.setattr(api_settings, "settings_file", lambda: tmp_path / "settings.json")
    api_settings.save_provider("hosted-llm", "https://generate.test/v1", "text-key", "generator")
    api_settings.save_provider("hosted-llm", "https://review.test/v1", "review-key", "reviewer", purpose="review")
    generation = api_providers.get_config("hosted-llm")
    review = api_settings.get_review_config()
    dialog = model_config_dialog.ModelConfigDialog("hosted-llm", purpose="image")
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
    assert api_settings.get_image_config().model == "image-model"
    assert api_providers.get_config("hosted-llm") == generation
    assert api_settings.get_review_config() == review
    assert fetch.call_args.args[0].api_key == "image-key"
    probe.assert_not_called()
    dialog.close()


def test_background_task_reports_the_original_exception_and_full_traceback(app, caplog):
    import logging
    from unittest.mock import Mock
    from core.generation.cancellation import CancelToken
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
