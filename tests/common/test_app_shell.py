"""The app window holds two features, the novel and the short video.

Switching between them, closing the window, and running or stopping a task in each one.
Settings of the short video stay in a temp file; its project is a new one under tmp_path.
"""

import os
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QElapsedTimer, QSettings
from PySide6.QtGui import QCloseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from core.common.cancellation import CancelToken
from core.gui.main_window import MainWindow
from core.gui.pages.short_video import workbench as workbench_module
from core.short_video.v2.feedback import TaskContext


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    ini = str(tmp_path / "workbench.ini")
    QSettings(ini, QSettings.IniFormat).setValue("last_project", str(tmp_path / "video"))
    monkeypatch.setattr(workbench_module, "QSettings", lambda *_: QSettings(ini, QSettings.IniFormat))
    # Offscreen, a question box would wait forever; tests that expect one replace this.
    monkeypatch.setattr(QMessageBox, "question", lambda *_: pytest.fail("unexpected question box"))
    window = MainWindow()
    novel(window).parameters_page.load(str(tmp_path / "novel"))
    yield window
    for feature in (novel(window), video(window)):
        if feature is not None:
            feature.cancel_all()
    wait_for(app, lambda: not busy(window))
    window.deleteLater()
    app.processEvents()


def novel(window):
    return window.novel


def video(window):
    return window.video


def busy(window):
    return any(feature.busy for feature in (novel(window), video(window)) if feature is not None)


def wait_for(app, predicate):
    timer = QElapsedTimer()
    timer.start()
    while not predicate() and timer.elapsed() < 10000:
        app.processEvents()
        QTest.qWait(5)
    assert predicate(), "did not reach the expected state"
    app.processEvents()


class NovelWork:
    """Runs until released; reporting progress is where a stop request takes effect."""

    def __init__(self):
        self.entered, self.release = threading.Event(), threading.Event()

    def __call__(self, report):
        self.entered.set()
        while not self.release.wait(0.05):
            report("生成中", -1)
        return {}


class VideoWork:
    def __init__(self):
        self.command = None
        self.token = CancelToken()
        self.task_context = TaskContext("文字", target="镜头1")
        self.entered, self.release = threading.Event(), threading.Event()

    def __call__(self, report):
        self.entered.set()
        assert self.release.wait(8)
        self.token.raise_if_cancelled()
        return {"status": "succeeded"}


def test_novel_task_runs_and_stops_from_the_status_bar(app, window):
    work = NovelWork()
    novel(window).workflow_page.task_requested.emit(work, None, "生成中…", None)
    wait_for(app, work.entered.is_set)
    assert novel(window).busy
    assert not novel(window).workflow_page.video_button.isEnabled()
    window.status._stop.click()
    wait_for(app, lambda: not novel(window).busy)
    assert window.status._state._text.text() == "任务已取消"
    assert novel(window).workflow_page.video_button.isEnabled()


def test_switching_features_remembers_pages_and_waits_for_running_tasks(app, window):
    novel(window).workflow_page.video_button.click()
    workspace = video(window)
    assert workspace is not None and window.stack.currentWidget() is workspace
    assert novel(window).rail.isHidden()
    workspace.navigation["video_storyboard"].click()
    window.open_feature("novel")
    assert window.stack.currentWidget() is novel(window)
    assert novel(window).stack.currentWidget() is novel(window).workflow_page
    assert not novel(window).rail.isHidden()
    window.open_feature("video")
    assert workspace.right.currentWidget() is workspace.storyboard

    work = VideoWork()
    workspace.submit(work)
    wait_for(app, work.entered.is_set)
    window.open_feature("novel")
    assert window.stack.currentWidget() is workspace
    work.release.set()
    wait_for(app, lambda: not workspace.busy)
    window.open_feature("novel")
    assert window.stack.currentWidget() is novel(window)

    # A novel task keeps the window on the novel until it ends.
    novel_work = NovelWork()
    novel(window).workflow_page.task_requested.emit(novel_work, None, "生成中…", None)
    wait_for(app, novel_work.entered.is_set)
    window.open_feature("video")
    assert window.stack.currentWidget() is novel(window)
    novel_work.release.set()
    wait_for(app, lambda: not novel(window).busy)


def test_closing_asks_while_a_task_runs_and_stops_it(app, window, monkeypatch):
    work = NovelWork()
    novel(window).workflow_page.task_requested.emit(work, None, "生成中…", None)
    wait_for(app, work.entered.is_set)
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.No)
    kept = QCloseEvent()
    window.closeEvent(kept)
    assert not kept.isAccepted() and novel(window).busy
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.Yes)
    closed = QCloseEvent()
    window.closeEvent(closed)
    assert closed.isAccepted()
    wait_for(app, lambda: not novel(window).busy)
    assert window.status._state._text.text() == "任务已取消"


class ChooseService:
    """Stands in for the model-service dialog: the person picks ``provider`` and saves."""

    provider = "qwen"
    opened_with = []

    def __init__(self, provider, _parent):
        self.opened_with.append(provider)

    def exec(self):
        return QDialog.DialogCode.Accepted

    def selected_provider(self):
        return self.provider

    def deleteLater(self):
        pass


def test_each_feature_keeps_its_own_model_service(app, window, monkeypatch):
    from core.gui.pages.novel import workspace as novel_workspace

    monkeypatch.setattr(workbench_module, "ModelConfigDialog", ChooseService)
    monkeypatch.setattr(novel_workspace, "ModelConfigDialog", ChooseService)
    ChooseService.opened_with = []
    # The novel work in use was saved with DeepSeek.
    novel(window)._restore_model("deepseek")
    window.open_feature("video")
    workspace = video(window)
    assert workspace.model() == "deepseek"

    # 服务配置 → 生成模型配置 in the short video picks 千问 for the short video only.
    novel_status = []
    monkeypatch.setattr(novel(window), "model_settings_changed", lambda: novel_status.append(1))
    ChooseService.provider = "qwen"
    workspace.configure_text_model()
    assert ChooseService.opened_with == ["deepseek"]
    assert workspace.model() == "qwen" and workspace.model_config_button.text().endswith("千问官方")
    assert novel(window).selected_model() == "deepseek"
    assert novel_status == [1], "saved addresses and keys are shown again in the novel too"

    # The novel changing its service leaves the short video's alone, also after reopening.
    window.open_feature("novel")
    video_status = []
    monkeypatch.setattr(workspace, "model_settings_changed", lambda: video_status.append(1))
    ChooseService.provider = "hosted-llm"
    novel(window)._configure_model()
    assert novel(window).selected_model() == "hosted-llm"
    assert workspace.model() == "qwen" and video_status == [1]
    reopened = workbench_module.Workbench(window, inherit_model="hosted-llm")
    assert reopened.model() == "qwen"
    reopened.deleteLater()
