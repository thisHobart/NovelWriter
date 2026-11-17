"""Chat lists the shots a change affects, revises the chosen ones, and the user applies them."""

import json
from copy import deepcopy

import pytest
from PySide6.QtCore import Qt

from core.short_video.v2 import conversation
from core.short_video.v2.conversation_context import build_conversation_context
from core.short_video.v2.models import WorkspaceError, identity
from core.short_video.v2.storyboard_chat import build_storyboard_chat_context
from core.short_video.v2.storyboard_edits import (
    EDIT_FIELDS,
    affected_shots,
    display_text,
    edit_base,
    reply_problems,
    revised_shots,
    shot_changes,
    shot_fingerprint,
    validate_storyboard_edit,
)
from test_shot_assets import scenario, snapshot  # noqa: F401
from test_storyboard_chat import all_records, chat_ui, draft, long_draft, offline  # noqa: F401
from test_video_v2_repository import project, prompt_project  # noqa: F401


def block(tag, value):
    return f"```{tag}\n{json.dumps(value, ensure_ascii=False)}\n```"


def affected_reply(*entries, missing=()):
    return "## 影响\n坠落物换了，几镜要一起改。\n" + block(
        "affected_shots", {"shots": list(entries), "missing_assets": list(missing)}
    )


def revised(shot, **changes):
    fields = {k: deepcopy(shot[k]) for k in EDIT_FIELDS}
    fields.update(shot_id=shot["shot_id"], **changes)
    return fields


def revised_reply(*shots):
    return "第1镜：改为放下钥匙。\n" + block("revised_shots", {"shots": list(shots)})


def entities(repository):
    return repository.current("asset_catalog")["content"]["entities"]


def test_affected_list_matches_draft_shots_and_blocks_are_hidden_in_chat(draft):
    shots = draft["shots"]
    reply = affected_reply(
        {"shot_id": "shot_3", "fields": ["动作", "对白"], "change": "去掉小羊"},
        {"shot_id": "unknown", "order": 1, "fields": "人物", "change": "按镜号找到"},
        {"shot_id": "shot_3", "change": "重复的一条不再列出"},
        {"shot_id": "gone", "order": 9, "change": "不在分镜中"},
        missing=["房子", " "],
    )
    listed = affected_shots(reply, shots)
    assert [(i["shot_id"], i["order"], i["fields"]) for i in listed["items"]] == [
        ("shot_1", 1, "人物"), ("shot_3", 3, "动作、对白")
    ]
    assert listed["items"][1]["change"] == "去掉小羊" and listed["missing_assets"] == ["房子"]
    shown = display_text(reply)
    assert "affected_shots" not in shown and shown.endswith("〔已列出受影响的镜头〕")
    assert affected_shots("只是回答问题。", shots) is None
    broken = affected_shots("```affected_shots\n{不是JSON\n```", shots)
    assert broken["items"] == [] and "JSON" in broken["error"]


def test_revised_shots_keep_ids_sources_and_report_each_rejected_shot(scenario, draft):
    shots = {s["shot_id"]: s for s in draft["shots"]}
    shots["shot_2"].update(dialogue_text="未保存的对白", character_text="未保存的人物")
    base = {sid: shots[sid] for sid in ("shot_1", "shot_2", "shot_3")}
    reply = revised_reply(
        revised(shots["shot_1"], action="寻井人放下铜钥匙", props=[], duration_ms=5000),
        revised(shots["shot_2"], props=[{"asset_id": "asset_house", "variant_id": "base"}]),
        {"shot_id": "shot_3", "action": "只给了动作"},
    )
    accepted, rejected = revised_shots(reply, base, entities(scenario))
    merged = accepted["shot_1"]
    assert merged["video_block_refs"] == shots["shot_1"]["video_block_refs"]
    assert merged["source_refs"] == shots["shot_1"]["source_refs"] and merged["props"] == []
    assert "资产目录中没有的道具：asset_house" in rejected["shot_2"]
    assert rejected["shot_3"].startswith("缺少景别、镜头运动")
    changes = shot_changes(shots["shot_1"], merged, entities(scenario))
    assert [label for label, _, _ in changes] == ["道具", "动作", "时长"]
    assert changes[2][1:] == ("7 秒", "5 秒")
    passed, problems = reply_problems(reply, base, entities(scenario))
    assert passed == 1 and problems[0].startswith("第2镜（shot_2）") and len(problems) == 2
    # Typed free text is replaced by the model's parsed values.
    clean, _ = revised_shots(revised_reply(revised(shots["shot_1"]) | {"shot_id": "shot_2"}), {"shot_2": shots["shot_2"]}, entities(scenario))
    assert "dialogue_text" not in clean["shot_2"] and "character_text" not in clean["shot_2"]
    with pytest.raises(WorkspaceError, match="revised_shots"):
        revised_shots("没有代码块", base, entities(scenario))
    assert reply_problems("没有代码块", base, entities(scenario)) == (0, ["回复缺少 revised_shots 代码块"])


def test_fingerprints_ignore_timeline_and_edit_marks_are_validated(draft):
    shot = deepcopy(draft["shots"][0])
    moved = {**shot, "order": 5, "start_ms": 9000, "end_ms": 16000}
    assert shot_fingerprint(shot) == shot_fingerprint(moved)
    assert shot_fingerprint(shot) != shot_fingerprint({**shot, "action": "改了"})
    value = {"shot_ids": ["shot_1"], "base": edit_base(draft["shots"], ["shot_1"])}
    assert validate_storyboard_edit(value) == value
    for bad in ({"shot_ids": [], "base": {}}, {"shot_ids": ["shot_1"], "base": {"shot_1": "x"}}):
        with pytest.raises(WorkspaceError):
            validate_storyboard_edit(bad)


def test_edit_context_attaches_chosen_shots_in_full_and_adds_edit_rules(scenario, draft):
    draft = long_draft(draft)
    items = [
        {"shot_id": "shot_7", "fields": "动作", "change": "改成房子"},
        {"shot_id": "shot_5", "fields": "人物", "change": "去掉小羊"},
    ]
    board = scenario.current("storyboard")
    value = build_storyboard_chat_context(draft, "shot_2", board, all_records(scenario), edit_items=items)
    assert [(i["shot_id"], i["order"], i["change"]) for i in value["edit_request"]] == [
        ("shot_5", 5, "去掉小羊"), ("shot_7", 7, "改成房子")
    ]
    assert [s["shot_id"] for s in value["edit_shots"]] == ["shot_5", "shot_7"]
    assert "visual_prompt_draft" in value["edit_shots"][1]
    assert [e["order"] for e in value["storyboard_outline"]] == [4, 6]
    assert value["storyboard_outline"][0]["shot_id"] == "shot_4"
    with pytest.raises(WorkspaceError, match="已不在分镜中"):
        build_storyboard_chat_context(draft, "shot_2", board, all_records(scenario), edit_items=[{"shot_id": "gone"}])
    message = {"id": identity("message"), "turn_id": identity("turn"), "role": "user", "content": "改", "status": "pending"}
    plain = build_storyboard_chat_context(draft, "shot_2", board, all_records(scenario))
    rules = build_conversation_context([message], message["id"], storyboard_context=plain).prompt.split("\n", 1)[0]
    assert "```affected_shots" in rules and "revised_shots" not in rules
    edit_rules = build_conversation_context([message], message["id"], storyboard_context=value).prompt.split("\n", 1)[0]
    assert "```revised_shots" in edit_rules and "不增删、拆分或合并镜头" in edit_rules


def edit_command(service, chat_id, repository, draft, shot_ids):
    items = [{"shot_id": sid, "change": "放下钥匙"} for sid in shot_ids]
    attached = build_storyboard_chat_context(draft, shot_ids[0], repository.current("storyboard"), all_records(repository), edit_items=items)
    return service.prepare(
        chat_id, "按清单修改第 1 镜", storyboard_context=attached,
        storyboard_edit={"shots": [s for s in draft["shots"] if s["shot_id"] in shot_ids], "entities": entities(repository)},
    )


def test_edit_reply_gets_one_format_repair_and_keeps_the_better_reply(scenario, draft, offline):
    shot = draft["shots"][0]
    bad = revised_reply(revised(shot, props=[{"asset_id": "asset_house", "variant_id": "base"}]))
    good = revised_reply(revised(shot, action="寻井人放下铜钥匙"))
    calls, replies = [], [bad, good]
    service = conversation.ConversationService(scenario, transport=lambda p: calls.append(p) or replies.pop(0))
    chat = service.store.create()
    command = edit_command(service, chat["id"], scenario, draft, ["shot_1"])
    assert command["context_snapshot"]["storyboard_edit"]["shot_ids"] == ["shot_1"]
    service.execute(command)
    assert len(calls) == 2 and "上次回复" in calls[1] and "资产目录中没有的道具" in calls[1]
    saved = service.store.get(chat["id"])["messages"]
    assert saved[0]["storyboard_edit"] == {"shot_ids": ["shot_1"], "base": edit_base(draft["shots"], ["shot_1"])}
    assert saved[1]["content"] == good
    # A repair that is no better keeps the first reply; a valid first reply needs no repair.
    calls.clear()
    replies[:] = [bad, "没有代码块"]
    service.execute(edit_command(service, chat["id"], scenario, draft, ["shot_1"]))
    assert len(calls) == 2 and service.store.get(chat["id"])["messages"][-1]["content"] == bad
    calls.clear()
    replies[:] = [good]
    service.execute(edit_command(service, chat["id"], scenario, draft, ["shot_1"]))
    assert len(calls) == 1


def run_chat(ui, reply):
    ui.workbench.transport = lambda _: reply
    return reply


def finish(ui):
    ui.pending[-1](lambda *_: None)
    ui.workbench.tasks.works.pop("text", None)
    ui.panel.refresh()


def test_chat_lists_affected_shots_revises_chosen_ones_and_applies_them_as_unsaved_edits(chat_ui, scenario, monkeypatch):
    from core.gui.pages.short_video import shot_edit_panel

    ui, panel = chat_ui, chat_ui.panel.edit_panel
    def documents():
        return scenario.load()["current"], {f.name: f.read_bytes() for f in (scenario.root / "artifacts").glob("*.json")}

    saved_before = documents()
    ui.page.editor.table.selectRow(1)
    run_chat(ui, affected_reply(
        {"shot_id": "shot_1", "fields": ["动作", "道具"], "change": "放下钥匙"},
        {"shot_id": "shot_3", "fields": ["动作"], "change": "看向井口"},
        missing=["房子"],
    ))
    ui.panel.input.setPlainText("坠落物改成房子")
    ui.panel.send()
    finish(ui)
    assert not panel.isHidden() and panel.title.text() == "受影响的镜头"
    assert list(panel.boxes) == ["shot_1", "shot_3"] and "资产目录里没有：房子" in panel.note.text()
    assert "affected_shots" not in ui.panel.messages.toPlainText()
    assert "〔已列出受影响的镜头〕" in ui.panel.messages.toPlainText()
    panel.boxes["shot_3"].setChecked(False)
    ui.page.editor.table.selectRow(2)  # Rebuilding for another shot keeps the choice.
    assert not panel.boxes["shot_3"].isChecked() and panel.boxes["shot_1"].isChecked()

    shot = ui.page.editor.draft_snapshot()["shots"][0]
    new_shot = revised(shot, action="寻井人放下铜钥匙", props=[], duration_ms=5000)
    run_chat(ui, revised_reply(new_shot))
    panel.edit_button.click()
    command = ui.pending[-1].command
    payload = json.loads(command["prompt"].split("\n", 1)[1])
    assert payload["conversation"][-1]["content"] == "按清单修改第 1 镜"
    assert [i["shot_id"] for i in payload["storyboard_context"]["edit_request"]] == ["shot_1"]
    assert payload["storyboard_context"]["reference"]["shot_id"] == "shot_3"
    finish(ui)
    assert panel.title.text() == "AI 修改结果" and panel.boxes["shot_1"].text() == "第 1 镜 · 改了 3 处"
    assert panel.apply_button.isEnabled() and not panel.undo_button.isEnabled()
    assert "〔已给出修改后的镜头内容〕" in ui.panel.messages.toPlainText()

    # The shot was edited after the request: applying asks first, and No changes nothing.
    ui.page.editor.table.selectRow(0)
    ui.page.editor.detail_fields["visual_prompt_draft"].setPlainText("用户之后写的画面")
    assert "又被编辑过" in panel.rows.findChildren(shot_edit_panel.QLabel)[0].text()
    edited = ui.page.editor.draft_snapshot()
    answers = [shot_edit_panel.QMessageBox.No, shot_edit_panel.QMessageBox.Yes]
    monkeypatch.setattr(shot_edit_panel.QMessageBox, "question", lambda *a: answers.pop(0))
    panel.apply_button.click()
    assert ui.page.editor.draft_snapshot() == edited
    panel.apply_button.click()
    applied = ui.page.editor.draft_snapshot()["shots"][0]
    assert applied["action"] == "寻井人放下铜钥匙" and applied["props"] == [] and applied["duration_ms"] == 5000
    assert applied["visual_prompt_draft"] == shot["visual_prompt_draft"]
    assert ui.page.editor.draft_snapshot()["shots"][1]["start_ms"] == 5000
    assert ui.page.dirty and ui.page.editor.marked == {"shot_1"}
    assert ui.page.editor.table.item(0, 5).background().color().name().upper() == "#EDF1F8"
    assert panel.boxes["shot_1"].text() == "第 1 镜 · 与分镜一致" and not panel.apply_button.isEnabled()
    assert documents() == saved_before and "尚未保存" in ui.notices[-1]

    panel.undo_button.click()
    assert ui.page.editor.draft_snapshot() == edited and ui.page.editor.marked == set()
    assert ui.page.dirty and not panel.undo_button.isEnabled()


def test_undo_returns_a_clean_page_and_later_edits_block_undo(chat_ui, scenario):
    ui, panel = chat_ui, chat_ui.panel.edit_panel
    ui.page.editor.table.selectRow(0)
    run_chat(ui, affected_reply({"shot_id": "shot_1", "change": "放下钥匙"}, {"shot_id": "shot_2", "change": "跟上"}))
    ui.panel.input.setPlainText("改")
    ui.panel.send()
    finish(ui)
    shot = ui.page.editor.draft_snapshot()["shots"][0]
    run_chat(ui, revised_reply(revised(shot, action="放下钥匙")))
    panel.request_edit()
    finish(ui)
    assert panel.boxes["shot_2"].text() == "第 2 镜 · 未通过检查"
    clean = ui.page.editor.draft_snapshot()
    panel.apply()
    assert ui.page.dirty and panel.undo_button.isEnabled()
    panel.undo_apply()
    assert not ui.page.dirty and ui.page.editor.draft_snapshot() == clean
    assert ui.page.status.text() != "有未保存修改"
    panel.apply()
    ui.page.editor.detail_fields["action"].setPlainText("应用后又改")
    assert not panel.undo_button.isEnabled()
    with pytest.raises(WorkspaceError, match="不能撤销"):
        panel.undo_apply()
    # Leaving the storyboard page hides the panel.
    ui.active[0] = None
    ui.panel.refresh_reference()
    assert panel.isHidden()


def test_panel_ignores_plain_replies_and_narrow_widths(chat_ui):
    ui, panel = chat_ui, chat_ui.panel.edit_panel
    ui.page.editor.table.selectRow(0)
    run_chat(ui, "普通回答，没有清单。")
    ui.panel.input.setPlainText("问个问题")
    ui.panel.send()
    finish(ui)
    assert panel.isHidden()
    run_chat(ui, affected_reply({"shot_id": "shot_1", "change": "很长的改法" * 40}))
    ui.panel.input.setPlainText("改")
    ui.panel.send()
    finish(ui)
    ui.panel.resize(280, 700)
    ui.panel.show()
    ui.app.processEvents()
    label = panel.rows.findChildren(type(panel.title))[0]
    assert label.wordWrap() and panel.rows.width() <= ui.panel.width()
    assert panel.rows.parentWidget().parentWidget().horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
    ui.panel.hide()
