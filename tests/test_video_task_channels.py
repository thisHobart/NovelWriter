"""Offline concurrency, task ownership, input guards and output-directory access."""

import os
import threading
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QElapsedTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from core.generation import api_providers, api_settings
from core.generation.cancellation import CancelToken
from core.gui.main_window import MainWindow
from core.gui.pages.short_video.task_channels import TaskChannels
from core.gui.task_runner import TaskRunner
from core.gui.widgets.status_bar import AppStatusBar
from core.short_video.v2.feedback import TaskContext
from core.short_video.v2.image_generation import ImageGenerationService, ImageGenerationWork
from core.short_video.v2.media_repository import MediaRepository
from core.short_video.v2.media_service import MediaService, MediaWork
from core.short_video.v2.models import WorkspaceError, encode
from core.short_video.v2.workflow import WorkspaceWork
from test_asset_image_generation import configured, png, reply
from test_comfyui_video import ComfyServer, reference_panel, save_ui_image
from test_video_prompt import BODY, add_shots, prepare, ready, response
from test_video_v2_repository import project, prompt_page, prompt_project


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def wait_for(app, predicate):
    timer = QElapsedTimer()
    timer.start()
    while not predicate() and timer.elapsed() < 25000:
        app.processEvents()
        QTest.qWait(5)
    assert predicate(), "task did not reach the expected boundary"
    app.processEvents()


def window_for(channels):
    return SimpleNamespace(
        _mode="video", runner=channels.runners["text"], status=AppStatusBar(),
        video_workspace=SimpleNamespace(tasks=channels, refresh_task_controls=lambda: None),
        refresh_all=lambda: None, _update_task_guards=lambda: None,
    )


class BlockingWork:
    def __init__(self, name="文字"):
        self.command = None
        self.token = CancelToken()
        self.task_context = TaskContext(name, target="镜头1")
        self.entered, self.release = threading.Event(), threading.Event()

    def __call__(self, report):
        self.entered.set()
        assert self.release.wait(8)
        self.token.raise_if_cancelled()
        report("完成", 1)
        return {"status": "succeeded"}


def test_channel_remains_owned_until_queued_completion_is_processed(app):
    runner = TaskRunner()
    runner._pool = SimpleNamespace(start=lambda task: None)
    results = []
    task = runner.run(lambda: "first", on_success=results.append)
    task.run()
    assert not task.running and runner.busy
    assert runner.run(lambda: "second") is None
    app.processEvents()
    assert not runner.busy and results == ["first"]
    runner.run(lambda: "second", on_success=results.append).run()
    app.processEvents()
    assert results == ["first", "second"] and not runner.busy


def test_selected_stop_and_terminal_results_never_stop_or_replace_another_channel(app):
    channels = TaskChannels(TaskRunner())
    window = window_for(channels)
    text, other = BlockingWork(), BlockingWork("图片")
    # Classification is covered by the real SDK concurrency test below.
    original = TaskChannels.channel
    channels.channel = lambda work: "image" if work is other else original(work)
    try:
        MainWindow._on_video_task_requested(window, text, None, "")
        MainWindow._on_video_task_requested(window, other, None, "")
        wait_for(app, lambda: text.entered.is_set() and other.entered.is_set())
        with pytest.raises(WorkspaceError, match="该类任务"):
            MainWindow._on_video_task_requested(window, BlockingWork(), None, "")
        assert window.status.tasks.currentData() == "text"
        window.status.tasks.setCurrentIndex(window.status.tasks.findData("image"))
        MainWindow._cancel_selected_task(window)
        assert other.token.cancelled and not text.token.cancelled
        other.release.set()
        wait_for(app, lambda: not channels.busy("image"))
        assert channels.busy("text") and window.status.task_count.text() == "运行中 1"
        assert window.status._stop.isHidden()
        window.status.show_notice("error", "目录打开失败", "系统拒绝打开")
        window.status.update_task("text", text, 0.5)
        assert window.status._right.text() == "目录打开失败"
        window.status.tasks.setCurrentIndex(window.status.tasks.findData("text"))
        assert not window.status._stop.isHidden() and window.status._stop.isEnabled()
        window.status.show_notice("success", "编辑已保存")
        assert window.status._stop.isEnabled()
        text.release.set()
        wait_for(app, lambda: not channels.any_busy)
        assert not channels.works
    finally:
        text.release.set()
        other.release.set()
        wait_for(app, lambda: not channels.any_busy)
        window.status.close()


def test_actual_sdk_text_images_and_comfy_execute_together_with_frozen_video(
    app, configured, ready, png, monkeypatch
):
    repository, image, _ = ready
    for purpose, host in (("generation", "generate"), ("review", "review")):
        kwargs = {} if purpose == "generation" else {"purpose": purpose}
        api_settings.save_provider(
            "hosted-llm", f"https://{host}.test/v1", "test-key", host, **kwargs
        )
    events = {name: threading.Event() for name in ("text", "image", "video")}
    releases = {name: threading.Event() for name in events}
    calls = []

    def block(name):
        events[name].set()
        assert releases[name].wait(50)

    def sdk(request):
        host = request.url.host
        calls.append(host)
        if host == "images.test":
            block("image")
            return reply(png)
        if host == "generate.test":
            block("text")
        content = encode(response(repository)) if host == "generate.test" else '{"issues":[]}'
        return httpx.Response(200, json={
            "id": "mock", "object": "chat.completion", "created": 0, "model": "mock",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
        })

    factory = api_providers.AsyncOpenAI
    def client(**kwargs):
        if "http_client" not in kwargs:
            kwargs["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(sdk))
        return factory(**kwargs)
    monkeypatch.setattr(api_providers, "AsyncOpenAI", client)
    text_command = prepare(ready)
    text_command["model_snapshot"] = {"provider": "hosted-llm", "model": "generate"}
    text = WorkspaceWork(repository, text_command)
    image_command = ImageGenerationService(repository).prepare(
        [ImageGenerationService(repository).rows()[0]["key"]]
    )
    image_work = ImageGenerationWork(repository, image_command, transport=httpx.MockTransport(sdk))
    command = MediaService(repository.output_dir).prepare_video(
        {"137": image["image_id"]}, "提交时的旧提示词", workflow_id="minimax-reference",
        target={"storyboard_version_id": repository.current("storyboard")["id"], "shot_id": "shot_1"},
    )
    server = ComfyServer()

    def comfy(request):
        if not events["video"].is_set():
            block("video")
        return server.request(request)

    video = MediaWork(repository.output_dir, command, transport=httpx.MockTransport(comfy))
    channels = TaskChannels(TaskRunner())
    window = window_for(channels)
    try:
        # Video already submitted first: starting new text for this shot must not stop it.
        for work in (video, text, image_work):
            MainWindow._on_video_task_requested(window, work, None, "")
        wait_for(app, lambda: all(event.is_set() for event in events.values()))
        assert all(channels.busy(channel) for channel in events if channel != "text")
        assert channels.busy("text") and window.status.task_count.text() == "运行中 3"
        ImageGenerationService(repository).recover()
        assert ImageGenerationService(repository).store.batch(image_command["id"])["status"] == "running"
        releases["text"].set()
        wait_for(app, lambda: not channels.busy("text"))
        assert channels.busy("video") and channels.busy("image")
        assert repository.current("prompt_bundle")["content"]["shots"][0]["video_motion_prompt"] == BODY
        releases["image"].set()
        releases["video"].set()
        wait_for(app, lambda: not channels.any_busy)
        assert set(calls) == {"generate.test", "review.test", "images.test"}
        assert ImageGenerationService(repository).store.batch(image_command["id"])["status"] == "succeeded"
        job = MediaRepository(repository.output_dir).jobs()[0]
        assert job["status"] == "succeeded" and job["input_snapshot"]["prompt"] == "提交时的旧提示词"
        assert all(entry.result[0] == "success" for entry in window.status._tasks.values())
    finally:
        for event in releases.values():
            event.set()
        wait_for(app, lambda: not channels.any_busy)
        window.status.close()


def test_same_shot_prompt_guards_inputs_and_new_video_but_not_other_shots_or_query(
    app, ready, prompt_page
):
    repository, image, _ = ready
    add_shots(repository, 2)
    page, _ = prompt_page
    page.tabs.setCurrentIndex(3)
    panel = page.media
    panel.use_image(image["image_id"], "137")
    panel.workbench.tasks.runners["text"] = SimpleNamespace(busy=False)
    text = WorkspaceWork(repository, prepare(ready))
    channels = panel.workbench.tasks
    channels.admit(text)
    channels.runners["text"].busy = True
    panel.refresh_result()
    assert not panel.generate_button.isEnabled() and not panel.input_area.isEnabled()
    assert not panel.parameter_area.isEnabled() and not panel.prompt.isEnabled()
    assert "正在编写" in panel.prompt_status.text()
    with pytest.raises(WorkspaceError, match="正在编写"):
        panel.generate()
    with pytest.raises(WorkspaceError, match="正在编写"):
        panel.use_image(image["image_id"], "137")
    submitted = []
    panel.workbench.submit = lambda work, button: submitted.append(work)
    panel.current_job = {"job_id": "a" * 32}
    panel.query()
    assert submitted[-1].job_id == "a" * 32
    page.objects.setCurrentIndex(page.objects.findData("shot_2"))
    panel.use_image(image["image_id"], "137")
    assert panel.generate_button.isEnabled() and panel.input_area.isEnabled()
    panel.generate()
    assert submitted[-1].command["target"]["shot_id"] == "shot_2"
    page.objects.setCurrentIndex(page.objects.findData("shot_1"))
    assert not panel.generate_button.isEnabled()
    channels.runners["text"].busy = False
    channels.finish("text", text)
    panel.refresh_result()
    assert panel.generate_button.isEnabled() and panel.prompt.isEnabled()


def test_resume_freezes_same_prompt_target_before_execution(app, ready):
    from core.short_video.v2.runtime import ResumeWork
    from core.short_video.v2.workflow import WorkflowService
    repository, _, _ = ready
    command = prepare(ready)
    service = WorkflowService(repository, transport=lambda _: encode(response(repository)),
                              quality_check=Mock(side_effect=ConnectionError("interrupt")))
    with pytest.raises(ConnectionError):
        service.execute(command)
    channels = TaskChannels(TaskRunner())
    work = ResumeWork(repository, command["request_id"])
    channels.admit(work)
    assert channels.prompt_target == (command["context"]["storyboard"]["id"], "shot_1")


@pytest.mark.parametrize("state", ["empty", "running", "failed"])
def test_open_directory_without_video_in_every_task_state(app, tmp_path, monkeypatch, state):
    from core.gui.pages.short_video import media_preview
    _, panel, _ = reference_panel(tmp_path)
    if state != "empty":
        panel.workbench.tasks.runners["video"] = SimpleNamespace(busy=state == "running")
        panel.refresh_result()
    opened = []
    monkeypatch.setattr(media_preview.QDesktopServices, "openUrl", lambda url: (opened.append(url), True)[1])
    path = tmp_path / "short_video" / "media" / "videos"
    assert panel.video_path is None and not path.exists() and panel.open_button.isEnabled()
    panel.open_location()
    assert path.is_dir() and opened[0].toLocalFile() == path.as_posix()
    panel.close()


def test_open_directory_reports_creation_and_system_open_errors(app, tmp_path, monkeypatch):
    from core.gui.pages.short_video import media_preview
    _, panel, _ = reference_panel(tmp_path)
    path = tmp_path / "short_video" / "media" / "videos"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a directory")
    with pytest.raises(FileExistsError):
        panel.open_location()
    path.unlink()
    monkeypatch.setattr(media_preview.QDesktopServices, "openUrl", lambda _: False)
    with pytest.raises(WorkspaceError, match="无法打开视频目录"):
        panel.open_location()
    panel.close()


def test_workspace_guards_project_configuration_exit_and_preserves_drafts(
    app, ready, tmp_path, monkeypatch
):
    from PySide6.QtCore import QSettings
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QMessageBox
    from core.gui.pages.short_video import workbench as workbench_module
    repository, image, _ = ready
    settings = QSettings(str(tmp_path / "workbench.ini"), QSettings.IniFormat)
    settings.setValue("last_project", repository.output_dir)
    monkeypatch.setattr(workbench_module, "QSettings", lambda *_: settings)
    window = MainWindow()
    window._build_video_pages()
    window._mode = "video"
    workspace = window.video_workspace
    window.stack.setCurrentWidget(workspace)
    workspace.specs.tabs.setCurrentIndex(3)
    workspace.specs.media.use_image(image["image_id"], "137")
    workspace.specs.media.prompt.setPlainText("视频输入框里的草稿")
    workspace.specs.editor.positive.setPlainText("画面提示词未保存编辑")
    text, image_work, video = BlockingWork(), BlockingWork("图片"), BlockingWork("视频")
    mapping = {text: "text", image_work: "image", video: "video"}
    workspace.tasks.channel = lambda work: mapping[work]
    try:
        for work in mapping:
            window._on_video_task_requested(work, None, "")
        wait_for(app, lambda: all(work.entered.is_set() for work in mapping))
        assert not workspace.project_button.isEnabled()
        assert not workspace.model_config_button.isEnabled()
        assert not workspace.specs.images_page.concurrency.isEnabled()
        assert not workspace.specs.media.generate_prompt_button.isEnabled()
        assert workspace.specs.media.open_button.isEnabled()
        with pytest.raises(WorkspaceError, match="切换项目"):
            workspace.open_project()
        with pytest.raises(WorkspaceError, match="修改"):
            workspace.configure_comfy()
        window.switch_mode("novel")
        assert window._mode == "video"
        text.release.set()
        wait_for(app, lambda: not workspace.tasks.busy("text"))
        # Other channels do not block text; refreshing does not discard either editor draft.
        assert workspace.chat.send_button.isEnabled()
        assert workspace.specs.media.generate_prompt_button.isEnabled()
        assert workspace.specs.media.prompt.toPlainText() == "视频输入框里的草稿"
        assert workspace.specs.editor.positive.toPlainText() == "画面提示词未保存编辑"
        workspace.resolve_edits = lambda: True
        monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.No)
        rejected = QCloseEvent()
        window.closeEvent(rejected)
        assert not rejected.isAccepted() and not video.token.cancelled
        monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.Yes)
        accepted = QCloseEvent()
        window.closeEvent(accepted)
        assert accepted.isAccepted() and video.token.cancelled and image_work.token.cancelled
    finally:
        for work in mapping:
            work.release.set()
        wait_for(app, lambda: not workspace.tasks.any_busy)
        workspace.resolve_edits = lambda: True
        window.close()
