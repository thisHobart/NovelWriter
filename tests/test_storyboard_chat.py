"""Current UI drafts become bounded chat evidence, never automatic document edits."""

import json
import os
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.short_video.v2 import conversation
from core.short_video.v2.conversation_context import build_conversation_context
from core.short_video.v2.models import WorkspaceError, encode, identity
from core.short_video.v2.storyboard_chat import (
    build_storyboard_chat_context,
    read_storyboard_chat_context,
)
from test_shot_assets import scenario, snapshot  # noqa: F401
from test_video_v2_repository import project, prompt_project  # noqa: F401


@pytest.fixture
def draft(scenario):
    value = deepcopy(scenario.current("storyboard")["content"])
    shot = value["shots"][1]
    shot.update(action="界面最新动作", video_block_refs=["b1"], source_refs=[
        {"source_id": scenario.current("source")["id"], "start": 6, "end": 10},
        {"source_id": scenario.current("source")["id"], "start": 8, "end": 13},
    ])
    return value


def context(repository, draft, shot_id="shot_2", **kwargs):
    return read_storyboard_chat_context(repository, draft, shot_id, repository.current("storyboard"), **kwargs)


def all_records(repository):
    state = repository.load()
    return {key: repository.record(key, state=state) for key in state["records"]}


def test_read_only_context_uses_current_shot_neighbours_exact_assets_and_merged_evidence(scenario, draft):
    before = snapshot(scenario), deepcopy(draft)
    value = context(scenario, draft, unsaved=True)
    assert value["reference"] == {"shot_id": "shot_2", "order": 2, "unsaved": True,
                                  "storyboard_id": scenario.current("storyboard")["id"]}
    assert value["shot"]["action"] == "界面最新动作"
    assert [(s["relation"], s["shot_id"]) for s in value["adjacent_shots"]] == [("previous", "shot_1"), ("next", "shot_3")]
    assert [b["id"] for b in value["video_blocks"]] == ["b1"]
    assert [(a["asset_id"], a["variant_id"]) for a in value["assets"]] == [("asset_scene", "base"), ("asset_character", "summer")]
    assert all("views" not in a.get("spec", {}) and "positive_prompt" not in a.get("spec", {}) for a in value["assets"])
    assert value["source_segments"] == [{"source_id": scenario.current("source")["id"], "start": 6, "end": 13, "text": "井口响了三声。"}]
    assert "井口响了三声。" in encode(value) and encode(value).count("井口响了三声。") == 1
    assert "asset_unused" not in encode(value) and "shots" not in value
    assert (snapshot(scenario), draft) == before
    value["shot"]["action"] = "修改返回值不影响编辑稿"
    assert draft == before[1]


@pytest.mark.parametrize("shot_id,neighbours", [("shot_1", ["shot_2"]), ("shot_3", ["shot_2"])])
def test_edge_shots_only_include_existing_neighbours(scenario, draft, shot_id, neighbours):
    assert [s["shot_id"] for s in context(scenario, draft, shot_id)["adjacent_shots"]] == neighbours


def test_incomplete_edit_and_unresolved_names_replace_stale_parsed_fields(scenario, draft):
    shot = draft["shots"][1]
    shot.update(character_text="还未说明身份的新人物", dialogue_text="新人：还没写完\n旁白：新的旁白", narration="旧旁白不应再次出现")
    draft["shots"][0]["action"] = ""  # Another incomplete shot must not block this discussion.
    value = context(scenario, draft)
    assert value["shot"]["dialogue_text"] == shot["dialogue_text"]
    assert "dialogue" not in value["shot"] and "narration" not in value["shot"]
    assert value["shot"]["characters"] == []
    assert value["shot"]["unresolved_character_labels"] == ["还未说明身份的新人物"]
    assert [a["asset_id"] for a in value["assets"]] == ["asset_scene"]
    assert "旧旁白不应再次出现" not in encode(value)


def test_ambiguous_names_are_not_assigned_to_one_of_the_twins(scenario, draft):
    draft["shots"][1]["character_text"] = "寻井人"
    value = context(scenario, draft)
    assert value["shot"]["characters"] == []
    assert value["shot"]["unresolved_character_labels"] == ["寻井人"]


def test_voice_over_prefix_and_ambiguous_speakers_use_only_current_raw_dialogue(scenario, draft):
    shot = draft["shots"][1]
    shot.update(character_text="无", dialogue_text="画外/画外音：有人吗？\n寻井人：同名人物尚未确定\n旁白：新的旁白")
    value = context(scenario, draft)
    assert [a["asset_id"] for a in value["assets"]] == ["asset_scene", "asset_voice"]
    assert value["shot"]["unresolved_speaker_labels"] == ["寻井人"]
    assert "dialogue" not in value["shot"]


def test_missing_bound_material_is_reported_without_latest_version_fallback(scenario, draft):
    board = scenario.current("storyboard")
    records = all_records(scenario)
    old = records.pop(board["dependency_refs"]["video_copy"])
    records["unrelated-new-video"] = {**old, "id": "unrelated-new-video", "content": {"blocks": [{"id": "b1", "narration": "不能代入的新版本"}]}}
    value = build_storyboard_chat_context(draft, "shot_2", board, records)
    assert value["video_blocks"] == [] and "不能代入的新版本" not in encode(value)
    assert any("文案块缺失" in note for note in value["missing"])
    missing = build_storyboard_chat_context(draft, "shot_2", board, {})
    assert missing["assets"] == missing["source_segments"] == []
    assert any("资产目录缺失" in note for note in missing["missing"])


@pytest.mark.parametrize("failure", ["invalid_range", "false_quote", "wrong_source", "budget"])
def test_invalid_or_oversized_original_evidence_is_omitted_whole(scenario, draft, failure):
    refs = draft["shots"][1]["source_refs"]
    if failure == "invalid_range":
        refs[:] = [{**refs[0], "end": 999999}]
    elif failure == "false_quote":
        refs[:] = [{**refs[0], "quote": "伪造引文"}]
    elif failure == "wrong_source":
        refs[:] = [{**refs[0], "source_id": "another-source"}]
    value = build_storyboard_chat_context(draft, "shot_2", scenario.current("storyboard"), all_records(scenario), max_source_chars=1 if failure == "budget" else 6000)
    assert value["source_segments"] == [] and value["missing"]
    assert "不能声称已核实原文" in encode(value)


def test_single_new_shot_can_be_discussed_without_generated_specs(scenario, draft):
    board = scenario.current("storyboard")
    draft["shots"] = [{**draft["shots"][1], "shot_id": "new-unsaved-shot"}]
    records = {k: r for k, r in all_records(scenario).items() if not r["kind"].endswith("_spec")}
    value = build_storyboard_chat_context(draft, "new-unsaved-shot", board, records, unsaved=True)
    assert value["adjacent_shots"] == [] and value["reference"]["order"] == 1
    assert all("spec" not in a and "notice" in a for a in value["assets"])
    with pytest.raises(WorkspaceError, match="重新选择"):
        build_storyboard_chat_context(draft, "removed-shot", board, records)


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(conversation, "capture_model_snapshot", lambda _: {"provider": "hosted-llm", "model": "offline"})
    monkeypatch.setattr(conversation, "validate_model_snapshot", lambda _: None)


def test_chat_freezes_draft_sends_once_keeps_original_and_records_only_reference_metadata(scenario, draft, offline):
    calls = []
    service = conversation.ConversationService(scenario, transport=lambda p: calls.append(p) or "## 建议\n简化动作，保持声音。")
    chat = service.store.create()
    current_before = deepcopy(scenario.load()["current"])
    artifacts_before = {p.name: p.read_bytes() for p in (scenario.root / "artifacts").glob("*.json")}
    attached = context(scenario, draft, unsaved=True)
    command = service.prepare(chat["id"], "帮我修改这个动作", storyboard_context=attached)
    attached["shot"]["action"] = "准备之后再次修改，不应进入已经冻结的请求"
    draft["shots"][1]["action"] = "界面继续编辑"
    service.execute(command)
    payload = json.loads(calls[0].split("\n", 1)[1])
    assert len(calls) == 1 and payload["storyboard_context"]["shot"]["action"] == "界面最新动作"
    assert payload["conversation"][-1]["content"] == "帮我修改这个动作"
    saved = service.store.get(chat["id"])
    user = saved["messages"][0]
    assert user["content"] == "帮我修改这个动作" and user["storyboard_reference"]["shot_id"] == "shot_2"
    assert "storyboard_context" not in user and "source_segments" not in encode(user)
    assert saved["messages"][1]["content"].startswith("## 建议")
    assert scenario.load()["current"] == current_before
    assert artifacts_before == {p.name: p.read_bytes() for p in (scenario.root / "artifacts").glob("*.json")}
    next_command = service.prepare(chat["id"], "按刚才的建议再简化", storyboard_context=context(scenario, draft, unsaved=True))
    next_payload = json.loads(next_command["prompt"].split("\n", 1)[1])
    assert len(next_payload["conversation"]) == 3
    assert next_payload["storyboard_context"]["shot"]["action"] == "界面继续编辑"
    assert "界面最新动作" not in next_command["prompt"]
    assert next_payload["conversation"][0]["storyboard_reference"]["shot_id"] == "shot_2"


def test_context_budget_counts_attachment_and_drops_history_before_refusing(scenario, draft):
    attached = context(scenario, draft)
    user_id, turn = identity("message"), identity("turn")
    history = [
        {"id": user_id, "turn_id": turn, "role": "user", "content": "旧问题" * 100, "status": "complete"},
        {"id": identity("message"), "turn_id": turn, "parent_message_id": user_id, "role": "assistant", "content": "旧答复" * 100, "status": "complete"},
        {"id": identity("message"), "turn_id": identity("turn"), "role": "user", "content": "当前问题", "status": "pending"},
    ]
    minimum = build_conversation_context(history[-1:], history[-1]["id"], storyboard_context=attached)
    bounded = build_conversation_context(history, history[-1]["id"], storyboard_context=attached, max_context_chars=minimum.character_count)
    assert bounded.omitted_message_ids == tuple(m["id"] for m in history[:-1])
    assert bounded.prompt.count("界面最新动作") == 1
    with pytest.raises(WorkspaceError, match="关闭附带分镜"):
        build_conversation_context(history, history[-1]["id"], storyboard_context=attached, max_context_chars=minimum.character_count - 1)


@pytest.fixture
def chat_ui(scenario, tmp_path, offline):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from core.gui.pages.short_video.artifact_page import ArtifactPage
    from core.gui.pages.short_video.chat_panel import ChatPanel

    app = QApplication.instance() or QApplication([])
    pending, notices = [], []
    workbench = SimpleNamespace(
        repository=scenario, model=lambda: "hosted-llm", transport=lambda _: "建议只保留一个动作。",
        settings=QSettings(str(tmp_path / "ui.ini"), QSettings.IniFormat), setting_key=lambda key: key,
        perform=lambda operation: operation(), notice=notices.append, active_work=None,
        host=SimpleNamespace(runner=SimpleNamespace(busy=False, cancel_current=Mock())),
    )
    from core.gui.pages.short_video.task_channels import TaskChannels
    workbench.tasks = TaskChannels(workbench.host.runner)
    page = ArtifactPage(workbench, "storyboard", "分镜")
    workbench.storyboard = page
    active = [page]
    workbench.current_artifact_page = lambda: active[0]
    panel = ChatPanel(workbench)
    workbench.chat = panel

    def submit(work, button):
        workbench.tasks.works["text"] = work
        pending.append(work)

    workbench.submit = submit
    page.editor.selection_changed.connect(panel.refresh_reference)
    page.editor.changed.connect(panel.refresh_reference)
    page.refresh()
    panel.refresh()
    yield SimpleNamespace(app=app, page=page, panel=panel, workbench=workbench, pending=pending, notices=notices, active=active)
    panel.close()
    page.close()
    app.processEvents()


def test_ui_preview_reads_unsaved_edits_without_validation_or_model_and_tracks_selection(chat_ui, scenario, monkeypatch):
    from core.gui.pages.short_video import chat_panel

    ui = chat_ui
    before = snapshot(scenario)
    ui.page.editor.table.selectRow(1)
    ui.page.editor.detail_fields["action"].setPlainText("编辑中，尚未保存")
    ui.page.editor.detail_fields["dialogue"].setPlainText("新名字：对白还在写\n旁白：新旁白")
    assert ui.page.dirty and "第 2 镜" in ui.panel.context_label.text() and "未保存" in ui.panel.context_label.text()
    ui.page.editor.content["shots"][0]["action"] = ""
    before_draft = ui.page.editor.draft_snapshot()
    preview = []
    monkeypatch.setattr(chat_panel, "show_text_dialog", lambda *args, **kwargs: preview.append((json.loads(args[2]), kwargs)))
    ui.panel.preview_context_button.click()
    assert preview[0][0] == ui.panel.selected_context() and preview[0][1] == {"readonly": True}
    assert preview[0][0]["shot"]["dialogue_text"].endswith("旁白：新旁白")
    assert "narration" not in preview[0][0]["shot"]
    assert ui.page.editor.draft_snapshot() == before_draft and ui.page.dirty
    assert snapshot(scenario) == before and ui.pending == []
    ui.page.editor.table.selectRow(2)
    assert "第 3 镜" in ui.panel.context_label.text()
    ui.panel.attach_shot.setChecked(False)
    assert ui.panel.selected_context() is None and "普通聊天" in ui.panel.context_label.text()
    ui.panel.attach_shot.setChecked(True)
    ui.active[0] = None
    ui.panel.refresh_reference()
    assert ui.panel.context_panel.isHidden() and ui.panel.selected_context() is None
    ui.active[0] = ui.page
    ui.panel.refresh_reference()
    assert not ui.panel.context_panel.isHidden() and ui.panel.attach_shot.isChecked()


def test_ui_send_freezes_selection_but_never_saves_or_changes_editor(chat_ui, scenario):
    ui = chat_ui
    ui.page.editor.table.selectRow(1)
    ui.page.editor.detail_fields["action"].setPlainText("发送时的动作")
    ui.panel.input.setPlainText("简化这镜")
    original = scenario.current("storyboard")["id"]
    ui.panel.send()
    assert len(ui.pending) == 1 and ui.panel.input.toPlainText() == ""
    ui.page.editor.detail_fields["action"].setPlainText("等待回复时继续修改")
    ui.page.editor.table.selectRow(2)
    draft_before_reply = ui.page.editor.draft_snapshot()
    command = ui.pending[0].command
    payload = json.loads(command["prompt"].split("\n", 1)[1])
    assert payload["storyboard_context"]["shot"]["shot_id"] == "shot_2"
    assert payload["storyboard_context"]["shot"]["action"] == "发送时的动作"
    ui.pending[0](lambda *_: None)
    ui.page.refresh()
    ui.panel.refresh()
    assert "你 · 第 2 镜 · 含未保存修改" in ui.panel.messages.toPlainText()
    assert "建议只保留一个动作" in ui.panel.messages.toPlainText()
    assert ui.page.editor.draft_snapshot() == draft_before_reply and ui.page.dirty
    assert scenario.current("storyboard")["id"] == original
    ui.panel.retry()
    assert ui.panel.input.toPlainText() == "简化这镜"
    ui.panel.send()
    assert ui.pending[-1].command["context_snapshot"]["storyboard_reference"]["shot_id"] == "shot_3"


def test_plain_chat_and_missing_selection_dont_carry_last_shot(chat_ui):
    ui = chat_ui
    ui.active[0] = None
    ui.panel.input.setPlainText("普通聊天问题")
    ui.panel.send()
    payload = json.loads(ui.pending[0].command["prompt"].split("\n", 1)[1])
    assert set(payload) == {"conversation"}
    assert "storyboard_reference" not in ui.pending[0].command["context_snapshot"]
    ui.active[0] = ui.page
    ui.page.editor.set_content(None)
    ui.panel.refresh_reference()
    assert ui.panel.selected_context() is None
    assert "未附带分镜" in ui.panel.context_label.text()


def test_failed_chat_retry_reads_new_draft_and_cancel_preserves_documents(chat_ui, scenario):
    from core.generation.cancellation import GenerationCancelled

    ui = chat_ui
    original = scenario.current("storyboard")["id"]
    ui.page.editor.table.selectRow(1)
    ui.page.editor.detail_fields["action"].setPlainText("失败前的编辑")
    ui.workbench.transport = Mock(side_effect=ValueError("模拟调用失败"))
    ui.panel.input.setPlainText("调整一下")
    ui.panel.send()
    with pytest.raises(ValueError, match="模拟调用失败"):
        ui.pending[0](lambda *_: None)
    ui.panel.retry()
    ui.page.editor.detail_fields["action"].setPlainText("重试时最新的编辑")
    ui.workbench.transport = Mock(return_value="普通建议")
    ui.panel.send()
    latest = ui.pending[-1]
    assert "重试时最新的编辑" in latest.command["prompt"] and "失败前的编辑" not in latest.command["prompt"]
    latest.token.cancel()
    with pytest.raises(GenerationCancelled):
        latest(lambda *_: None)
    ui.workbench.transport.assert_not_called()
    assert ui.page.dirty and ui.page.editor.content["shots"][1]["action"] == "重试时最新的编辑"
    assert scenario.current("storyboard")["id"] == original
    messages = ui.panel.service().store.get(ui.panel.conversation_id)["messages"]
    assert [m["status"] for m in messages] == ["failed", "cancelled"]


def test_attachment_controls_fit_narrow_chat_and_toggling_never_discards_input(chat_ui):
    ui = chat_ui
    ui.panel.resize(320, 760)
    ui.panel.show()
    ui.app.processEvents()
    ui.panel.input.setPlainText("还未发送的问题")
    assert ui.panel.context_panel.width() <= ui.panel.width()
    assert ui.panel.preview_context_button.isVisible() and ui.panel.input.height() >= 60
    ui.panel.attach_shot.setChecked(False)
    assert ui.panel.input.toPlainText() == "还未发送的问题" and ui.panel.context_note.isHidden()
    ui.panel.attach_shot.setChecked(True)
    assert not ui.panel.context_note.isHidden()
