"""Switching pages refreshes only what is shown, one read of the project serves a whole refresh,
and the chat stream is redrawn only when what it shows has changed."""

import os
import threading
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_shot_assets import scenario  # noqa: F401
from test_video_v2_repository import novel, project, prompt_project  # noqa: F401

from core.short_video.v2 import conversation
from core.short_video.v2.models import fingerprint
from core.short_video.v2.repository import ProjectRepository, shared_project_reads

PAGE_KEYS = ["video_source", "video_adaptation", "video_storyboard", "video_prompts"]


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(
        conversation,
        "capture_model_snapshot",
        lambda _: {"provider": "hosted-llm", "model": "offline"},
    )
    monkeypatch.setattr(conversation, "validate_model_snapshot", lambda _: None)


@pytest.fixture
def ui(scenario, tmp_path, monkeypatch, offline):  # noqa: F811
    """The real short-video workbench on a test project; settings stay in a temp file."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from core.gui.pages.short_video import workbench as workbench_module

    app = QApplication.instance() or QApplication([])
    ini = str(tmp_path / "workbench.ini")
    settings = QSettings(ini, QSettings.IniFormat)
    settings.setValue("last_project", scenario.output_dir)
    settings.sync()
    monkeypatch.setattr(
        workbench_module, "QSettings", lambda *_: QSettings(ini, QSettings.IniFormat)
    )
    host = SimpleNamespace(status=Mock(), open_feature=Mock(), model_settings_saved=Mock())
    workbench = workbench_module.Workbench(host)
    workbench.resize(1400, 900)
    workbench.show()
    app.processEvents()
    yield SimpleNamespace(app=app, workbench=workbench, repository=scenario)
    workbench.close()
    workbench.deleteLater()
    app.processEvents()


def spy(monkeypatch, owner, name):
    calls = []
    original = getattr(owner, name)

    def wrapper(*args, **kwargs):
        calls.append(name)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, wrapper)
    return calls


def page_spies(monkeypatch, workbench):
    pages = {
        "video_source": workbench.source,
        "novel": workbench.novel,
        "video": workbench.video,
        "video_storyboard": workbench.storyboard,
        "video_prompts": workbench.specs,
        "chat": workbench.chat,
    }
    return {key: spy(monkeypatch, page, "refresh") for key, page in pages.items()}


def counted_reads(monkeypatch):
    reads = []
    original = ProjectRepository._read

    def counting(self):
        reads.append(self.path)
        return original(self)

    monkeypatch.setattr(ProjectRepository, "_read", counting)
    return reads


# -- only the shown page -------------------------------------------------------------------


def test_switching_refreshes_only_the_page_shown_and_leaves_the_chat_alone(ui, monkeypatch):
    workbench = ui.workbench
    workbench.adaptation.setCurrentIndex(0)
    calls = page_spies(monkeypatch, workbench)
    expected = {
        "video_source": "video_source",
        "video_adaptation": "novel",
        "video_storyboard": "video_storyboard",
        "video_prompts": "video_prompts",
    }
    for key in PAGE_KEYS:
        for value in calls.values():
            value.clear()
        assert workbench.navigate(key)
        ui.app.processEvents()
        refreshed = {name for name, value in calls.items() if value}
        assert refreshed == {expected[key]}, key


def test_a_hidden_page_catches_up_with_a_new_version_when_shown(ui, monkeypatch):
    workbench, repository = ui.workbench, ui.repository
    workbench.navigate("video_storyboard")
    old_id = workbench.storyboard.record["id"]
    workbench.navigate("video_source")
    board = repository.current("storyboard")
    content = deepcopy(board["content"])
    content["shots"][0]["action"] = "后台存入的新动作"
    saved = repository.save(
        "storyboard", content, board["dependency_refs"],
        expected_revision=repository.load()["revision"],
    )
    calls = page_spies(monkeypatch, workbench)
    workbench.refresh()  # After a save or a finished task: the shown page and the chat.
    assert calls["video_source"] and calls["chat"] and not calls["video_storyboard"]
    assert workbench.storyboard.record["id"] == old_id
    workbench.navigate("video_storyboard")
    assert workbench.storyboard.record["id"] == saved["id"]
    assert workbench.storyboard.editor.value()["shots"][0]["action"] == "后台存入的新动作"


def test_the_other_rewrite_tab_refreshes_when_it_is_shown(ui, monkeypatch):
    workbench = ui.workbench
    workbench.adaptation.setCurrentIndex(0)
    workbench.navigate("video_adaptation")
    calls = page_spies(monkeypatch, workbench)
    workbench.adaptation.setCurrentIndex(1)
    assert calls["video"] and not calls["novel"]
    workbench.navigate("video_source")
    calls["novel"].clear()
    workbench.adaptation.setCurrentIndex(0)  # Not on screen: waits until 改写 is shown.
    assert not calls["novel"]


def test_the_video_area_loads_only_on_the_shot_tab(ui, monkeypatch):
    workbench = ui.workbench
    media = workbench.specs.media
    workbench.specs.tabs.setCurrentIndex(0)  # 人物
    loads = spy(monkeypatch, media, "refresh")
    results = spy(monkeypatch, media, "refresh_result")
    workbench.navigate("video_prompts")
    workbench.refresh_task_controls()
    workbench.refresh()
    assert loads == [] and results == []
    workbench.specs.tabs.setCurrentIndex(3)  # 镜头
    assert loads and results
    results.clear()
    workbench.refresh_task_controls()
    assert results


# -- one read of the project per refresh -----------------------------------------------------


@pytest.mark.parametrize("key", PAGE_KEYS)
def test_one_switch_reads_the_project_file_once(ui, monkeypatch, key):
    workbench = ui.workbench
    workbench.specs.tabs.setCurrentIndex(3)  # The busiest page: 提示词 → 镜头.
    workbench.navigate("video_source" if key != "video_source" else "video_storyboard")
    reads = counted_reads(monkeypatch)
    workbench.navigate(key)
    assert len(reads) == 1
    reads.clear()
    workbench.refresh()
    assert len(reads) == 1


@pytest.mark.parametrize("key", PAGE_KEYS)
def test_a_refresh_leaves_the_shared_project_state_unchanged(ui, key):
    workbench = ui.workbench
    workbench.specs.tabs.setCurrentIndex(3)
    workbench.navigate(key)
    with shared_project_reads():
        shared = workbench.repository.load()
        before = fingerprint(shared)
        workbench.refresh()
        workbench.refresh_shown()
        assert workbench.repository.load() is shared
    assert fingerprint(shared) == before == fingerprint(workbench.repository.load())


def test_shared_reads_see_writes_and_stay_on_their_own_thread(project):  # noqa: F811
    assert project.load() is not project.load()
    with shared_project_reads():
        first = project.load()
        assert ProjectRepository(project.output_dir).load() is first
        other = []
        thread = threading.Thread(target=lambda: other.append(project.load()))
        thread.start()
        thread.join()
        assert other[0] is not first and other[0] == first
        saved = novel(project)  # A write in the same period.
        after = project.load()
        assert after is not first and after["revision"] == first["revision"] + 1
        assert after["current"]["novel_copy"] == saved["id"]
        assert project.load() is after
        # A write from another thread is noticed by the file's time and size.
        thread = threading.Thread(target=lambda: novel(ProjectRepository(project.output_dir)))
        thread.start()
        thread.join()
        assert project.load()["revision"] == after["revision"] + 1
    assert project.load() is not project.load()


# -- chat is redrawn only when its content changes ---------------------------------------------


def talk(repository, conversation_id, text):
    service = conversation.ConversationService(repository, transport=lambda _: "只保留一个动作。")
    service.execute(service.prepare(conversation_id, text))


def test_chat_is_not_redrawn_when_nothing_it_shows_has_changed(ui):
    workbench, repository = ui.workbench, ui.repository
    chat = workbench.chat
    workbench.navigate("video_source")
    conversation_id = conversation.ConversationService(repository).store.create()["id"]
    talk(repository, conversation_id, "镜头太长了吗")
    chat.conversation_id = conversation_id
    chat.refresh()
    drawn = list(chat.messages.items)
    assert len(drawn) == 2
    chat.refresh()
    workbench.refresh()
    workbench.navigate("video_adaptation")
    workbench.navigate("video_prompts")
    assert all(a is b for a, b in zip(chat.messages.items, drawn, strict=True))
    # The storyboard page brings cards under replies: drawn again.
    workbench.navigate("video_storyboard")
    on_storyboard = list(chat.messages.items)
    assert on_storyboard[0] is not drawn[0]
    chat.refresh()
    assert all(a is b for a, b in zip(chat.messages.items, on_storyboard, strict=True))
    # A new message is drawn.
    talk(repository, conversation_id, "再短一点")
    chat.refresh()
    assert len(chat.messages.items) == 4 and chat.messages.items[0] is not on_storyboard[0]
