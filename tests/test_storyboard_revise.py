"""Revising selected storyboard shots from written feedback, leaving every other shot as saved."""

import os
from copy import deepcopy
from unittest.mock import Mock

import pytest

import core.short_video.v2.runtime as runtime_module
import core.short_video.v2.workflow as workflow_module
from core.short_video.v2.models import WorkspaceError, encode
from core.short_video.v2.workflow import WorkflowService, keep_unselected_shots
from test_video_v2_repository import project, prompt_project  # noqa: F401


def three_shots(repository):
    board = repository.current("storyboard")
    shots = []
    for number in (1, 2, 3):
        shot = deepcopy(board["content"]["shots"][0])
        shot.update(shot_id=f"shot_{number}", action=f"原动作{number}", duration_ms=3000)
        shots.append(shot)
    return repository.save("storyboard", {"shots": shots}, board["dependency_refs"],
                           expected_revision=repository.load()["revision"])


def test_unselected_shots_and_order_come_from_the_original():
    original = {"shots": [{"shot_id": f"s{i}", "action": f"原{i}"} for i in range(3)]}
    returned = {"shots": [{"shot_id": "s2", "action": "被误改"}, {"shot_id": "s1", "action": "新1"}],
                "extra": "模型多给的字段"}
    merged = keep_unselected_shots(returned, original, ["s1"])
    assert merged == {"shots": [{"shot_id": "s0", "action": "原0"}, {"shot_id": "s1", "action": "新1"},
                                {"shot_id": "s2", "action": "原2"}]}
    with pytest.raises(WorkspaceError, match="缺少所选镜头：第2镜"):
        keep_unselected_shots({"shots": [{"shot_id": "s0"}]}, original, ["s1"])


def test_partial_revision_changes_only_selected_shots_and_scopes_prompt_and_review(prompt_project, monkeypatch):
    repository = prompt_project
    monkeypatch.setattr(workflow_module, "capture_model_snapshot", lambda _: {"model": "offline"})
    monkeypatch.setattr(runtime_module, "validate_model_snapshot", lambda _: None)
    original = three_shots(repository)
    calls = []

    def send(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            assert "本次只修订第2镜（shot_id：shot_2）" in prompt
            assert "坠落物改为房子" in prompt
            shots = deepcopy(original["content"]["shots"])
            shots[1]["action"] = "房子从天而降"
            shots[0]["action"] = "模型顺手改了第一镜"
            return encode({"shots": shots[:2]})  # Omitting shot 3 must not delete it.
        assert "本次只修订了第2镜" in prompt
        return '{"issues":[]}'

    service = WorkflowService(repository, transport=send)
    command = service.prepare("storyboard", mode="revise", target_id=original["id"],
                              asks=["坠落物改为房子"], shot_ids=["shot_2"])
    result = service.execute(command)
    shots = repository.record(result["version_id"])["content"]["shots"]
    assert [s["action"] for s in shots] == ["原动作1", "房子从天而降", "原动作3"]
    assert repository.current("storyboard")["id"] == result["version_id"]
    assert len(calls) == 2


@pytest.fixture
def storyboard_page(prompt_project, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from types import SimpleNamespace

    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from core.gui.pages.short_video.artifact_page import ArtifactPage
    from core.gui.pages.short_video.task_channels import TaskChannels

    app = QApplication.instance() or QApplication([])
    three_shots(prompt_project)
    submitted = []
    workbench = SimpleNamespace(
        repository=prompt_project, model=lambda: "hosted-llm", transport=lambda _: "",
        settings=QSettings(str(tmp_path / "ui.ini"), QSettings.IniFormat), setting_key=lambda key: key,
        perform=lambda operation: operation(), notice=lambda _: None, active_work=None,
        host=SimpleNamespace(runner=SimpleNamespace(busy=False, cancel_current=Mock())),
        submit=lambda work, button: submitted.append(work), update_shot_prompts=lambda: None,
    )
    workbench.tasks = TaskChannels(workbench.host.runner)
    page = ArtifactPage(workbench, "storyboard", "分镜")
    workbench.refresh = page.refresh
    page.refresh()
    yield SimpleNamespace(app=app, page=page, submitted=submitted)
    page.close()
    app.processEvents()


def test_page_selects_several_shots_saves_edits_and_submits_one_revision(storyboard_page, monkeypatch):
    from PySide6.QtCore import QItemSelectionModel

    from core.gui.pages.short_video import artifact_page

    page = storyboard_page.page
    table = page.editor.table
    table.selectRow(0)
    table.selectionModel().select(
        table.model().index(2, 0), QItemSelectionModel.Select | QItemSelectionModel.Rows
    )
    assert page.editor.selected_shot_ids() == ["shot_1", "shot_3"]
    page.editor.content["shots"][1]["action"] = "未保存的编辑"
    page.mark_dirty()
    asked = []
    monkeypatch.setattr(artifact_page, "ask_revision",
                        lambda parent, orders: asked.append(orders) or "坠落物改为房子")
    page.editor.revise_requested.emit(page.editor.selected_shot_ids())
    assert asked == [[1, 3]]
    work = storyboard_page.submitted[0]
    assert work.command["mode"] == "revise" and work.command["shot_ids"] == ["shot_1", "shot_3"]
    assert work.command["requirements"]["asks"] == ["坠落物改为房子"]
    saved = page.workbench.repository.current("storyboard")
    assert work.command["candidate_id"] == saved["id"]
    assert saved["content"]["shots"][1]["action"] == "未保存的编辑"


def test_cancelled_revision_submits_nothing_and_keeps_edits_unsaved(storyboard_page, monkeypatch):
    from core.gui.pages.short_video import artifact_page

    page = storyboard_page.page
    before = page.workbench.repository.current("storyboard")["id"]
    page.editor.content["shots"][0]["action"] = "仍未保存"
    page.mark_dirty()
    monkeypatch.setattr(artifact_page, "ask_revision", lambda parent, orders: None)
    page.revise_shots(["shot_1"])
    assert storyboard_page.submitted == [] and page.dirty
    assert page.workbench.repository.current("storyboard")["id"] == before
