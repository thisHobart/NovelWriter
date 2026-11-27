"""Chat lists the shots a change affects, revises the chosen ones, and the user applies them."""

import json
from copy import deepcopy

import pytest
from PySide6.QtCore import Qt

from core.short_video.v2 import conversation
from core.short_video.v2.conversation_context import build_conversation_context
from core.short_video.v2.models import WorkspaceError, identity
from core.short_video.v2.storyboard_chat import storyboard_material, validate_storyboard_reference
from core.short_video.v2.storyboard_edits import (
    affected_shots,
    display_text,
    edit_base,
    history_text,
    reply_problems,
    revised_shots,
    shot_changes,
    shot_fingerprint,
    validate_storyboard_edit,
)
from test_shot_assets import scenario, snapshot  # noqa: F401
from test_storyboard_chat import all_records, attached, chat_ui, draft, long_draft, offline, section  # noqa: F401
from test_video_v2_repository import project, prompt_project  # noqa: F401


def block(tag, value):
    return f"```{tag}\n{json.dumps(value, ensure_ascii=False)}\n```"


def affected_reply(*entries, missing=()):
    return "## 影响\n坠落物换了，几镜要一起改。\n" + block(
        "affected_shots", {"镜头": list(entries), "缺少的资产": list(missing)}
    )


def revised_reply(*shots):
    return "第1镜：改为放下钥匙。\n" + block("revised_shots", {"镜头": list(shots)})


def entities(repository):
    return repository.current("asset_catalog")["content"]["entities"]


def test_affected_list_matches_draft_shots_and_blocks_are_hidden_in_chat(draft):
    shots = draft["shots"]
    reply = affected_reply(
        {"镜号": 3, "要改": ["画面", "对白"], "改法": "去掉小羊"},
        {"镜号": 1, "要改": "人物", "改法": "按镜号找到"},
        {"镜号": 3, "改法": "重复的一条不再列出"},
        {"镜号": 9, "改法": "不在分镜中"},
        missing=["房子", " "],
    )
    listed = affected_shots(reply, shots)
    assert [(i["shot_id"], i["order"], i["fields"]) for i in listed["items"]] == [
        ("shot_1", 1, "人物"), ("shot_3", 3, "画面、对白")
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
        named(1, 画面="寻井人放下铜钥匙", 时长=5,
              人物=["寻井人（常态）", "寻井人（常态）"],
              对白=[{"说话人": "画外音", "台词": "门外有人。", "画外": True}]),
        named(2, 道具=["房子（常态）"]),
        {"镜号": 3, "画面": "只给了画面"},
    )
    accepted, rejected = revised_shots(reply, base, entities(scenario))
    merged = accepted["shot_1"]
    assert merged["video_block_refs"] == shots["shot_1"]["video_block_refs"]
    assert merged["source_refs"] == shots["shot_1"]["source_refs"] and merged["props"] == []
    assert "道具“房子（常态）”不在资产目录里" in rejected["shot_2"]
    assert rejected["shot_3"].startswith("缺少取景范围、镜头运动")
    changes = shot_changes(shots["shot_1"], merged, entities(scenario))
    assert [label for label, _, _ in changes] == ["道具", "画面", "时长"]
    assert changes[2][1:] == ("7 秒", "5 秒")
    passed, problems = reply_problems(reply, base, entities(scenario))
    # The repair names shots by number only, as the material does.
    assert passed == 1 and problems[0].startswith("第2镜：") and len(problems) == 2
    assert not any("shot_" in problem for problem in problems)
    # Typed free text is replaced by the model's parsed values.
    clean, _ = revised_shots(revised_reply(named(2)), {"shot_2": shots["shot_2"]}, entities(scenario))
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


def test_edit_material_lists_the_shots_to_revise_and_adds_edit_rules(scenario, draft):
    draft = long_draft(draft)
    items = [
        {"shot_id": "shot_7", "fields": "画面", "change": "改成房子"},
        {"shot_id": "shot_5", "fields": "人物", "change": "去掉小羊"},
    ]
    board = scenario.current("storyboard")
    value = storyboard_material(draft, "shot_2", board, all_records(scenario), edit_items=items)
    text = value["material"]
    assert value["edit"] is True and value["edit_shot_ids"] == ["shot_5", "shot_7"]
    assert "当前选中第 2 镜；本轮要修改第 5、7 镜。" in text
    assert section(text, "本轮要修改的镜头").splitlines() == [
        "第 5 镜 · 要改：人物 · 改法：去掉小羊", "第 7 镜 · 要改：画面 · 改法：改成房子"
    ]
    # Revised shots name objects the way the material does, so no ids are needed anywhere.
    assert not any(token in text for token in ("asset_", "shot_", "variant"))
    with pytest.raises(WorkspaceError, match="已不在分镜中"):
        storyboard_material(draft, "shot_2", board, all_records(scenario), edit_items=[{"shot_id": "gone"}])
    message = {"id": identity("message"), "turn_id": identity("turn"), "role": "user", "content": "改", "status": "pending"}
    plain = storyboard_material(draft, "shot_2", board, all_records(scenario))
    assert "## 本轮要修改的镜头" not in plain["material"]
    rules = build_conversation_context([message], message["id"], attachment=plain).request[0]["content"]
    assert "```affected_shots" in rules and "revised_shots" not in rules
    edit_rules = build_conversation_context([message], message["id"], attachment=value).request[0]["content"]
    assert "```revised_shots" in edit_rules and "不增删、拆分或合并镜头" in edit_rules
    # An edit round's output format is the revision only.
    assert "affected_shots" not in edit_rules
    assert [title for title in ("## 通用要求", "## 本页要求", "## 输出格式") if title in edit_rules] == [
        "## 通用要求", "## 本页要求", "## 输出格式"
    ]


def edit_command(service, chat_id, repository, draft, shot_ids):
    items = [{"shot_id": sid, "change": "放下钥匙"} for sid in shot_ids]
    attachment = storyboard_material(draft, shot_ids[0], repository.current("storyboard"), all_records(repository), edit_items=items)
    return service.prepare(
        chat_id, "按清单修改第 1 镜", attachment=attachment,
        storyboard_edit={"shots": [s for s in draft["shots"] if s["shot_id"] in shot_ids], "entities": entities(repository)},
    )


def test_edit_reply_gets_one_format_repair_and_keeps_the_better_reply(scenario, draft, offline):
    bad = revised_reply(named(1, 道具=["房子（常态）"]))
    good = revised_reply(named(1, 画面="寻井人放下铜钥匙"))
    calls, replies = [], [bad, good]
    service = conversation.ConversationService(scenario, transport=lambda p: calls.append(p) or replies.pop(0))
    chat = service.store.create()
    command = edit_command(service, chat["id"], scenario, draft, ["shot_1"])
    assert command["context_snapshot"]["storyboard_edit"]["shot_ids"] == ["shot_1"]
    service.execute(command)
    # The repair is one more turn of the same conversation: the reply, then what to fix.
    assert len(calls) == 2 and calls[1][:-2] == calls[0]
    assert calls[1][-2] == {"role": "assistant", "content": bad}
    assert calls[1][-1]["role"] == "user" and calls[1][-1]["content"].startswith("上次回复有这些问题：\n- 第1镜")
    assert "道具“房子（常态）”不在资产目录里" in calls[1][-1]["content"]
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
        {"镜号": 1, "要改": ["画面", "道具"], "改法": "放下钥匙"},
        {"镜号": 3, "要改": ["画面"], "改法": "看向井口"},
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
    new_shot = named(1, 画面="寻井人放下铜钥匙", 时长=5,
                     人物=["寻井人（常态）", "寻井人（常态）"],
                     对白=[{"说话人": "画外音", "台词": "门外有人。", "画外": True}])
    run_chat(ui, revised_reply(new_shot))
    panel.edit_button.click()
    command = ui.pending[-1].command
    sent = attached(command["messages"])
    assert command["messages"][-1]["content"].endswith("【我的问题】\n按清单修改第 1 镜")
    assert section(sent, "本轮要修改的镜头") == "第 1 镜 · 要改：画面、道具 · 改法：放下钥匙"
    assert "当前选中第 3 镜；本轮要修改第 1 镜。" in sent
    assert command["context_snapshot"]["storyboard_reference"]["shot_id"] == "shot_3"
    finish(ui)
    assert panel.title.text() == "AI 修改结果" and panel.row_text("shot_1") == "第 1 镜 · 改了 3 处"
    assert panel.apply_button.isEnabled() and not panel.undo_button.isEnabled()
    assert "〔已给出修改后的镜头内容〕" in ui.panel.messages.toPlainText()
    # The card follows the latest reply; the earlier list folds to one line.
    assert panel.parentWidget() is ui.panel.messages.items[-1]
    folded = ui.panel.messages.findChildren(shot_edit_panel.CollapsedCard)
    assert [(c.head.title.text(), c.head.count.text(), c.head.sub.text()) for c in folded] == [
        ("受影响的镜头", "2", "已按第 1 镜发出修改")
    ]

    # The shot was edited after the request: applying asks first, and No changes nothing.
    ui.page.editor.table.selectRow(0)
    ui.page.editor.detail_fields["sound"].setPlainText("用户之后写的音效")
    assert any("又被编辑过" in label.text() for label in panel.rows.findChildren(shot_edit_panel.QLabel))
    edited = ui.page.editor.draft_snapshot()
    answers = [shot_edit_panel.QMessageBox.No, shot_edit_panel.QMessageBox.Yes]
    monkeypatch.setattr(shot_edit_panel.QMessageBox, "question", lambda *a: answers.pop(0))
    panel.apply_button.click()
    assert ui.page.editor.draft_snapshot() == edited
    panel.apply_button.click()
    applied = ui.page.editor.draft_snapshot()["shots"][0]
    assert applied["action"] == "寻井人放下铜钥匙" and applied["props"] == [] and applied["duration_ms"] == 5000
    assert applied["sound"] == shot["sound"]
    assert ui.page.editor.draft_snapshot()["shots"][1]["start_ms"] == 5000
    assert ui.page.dirty and ui.page.editor.marked == {"shot_1"}
    assert ui.page.editor.table.item(0, 4).background().color().name().upper() == "#EDF1F8"
    assert panel.row_text("shot_1") == "第 1 镜 · 已写入分镜" and not panel.apply_button.isEnabled()
    assert panel.foot_text.text() == "已写入第 1 镜，尚未保存"
    assert documents() == saved_before and "尚未保存" in ui.notices[-1]

    panel.undo_button.click()
    assert ui.page.editor.draft_snapshot() == edited and ui.page.editor.marked == set()
    assert ui.page.dirty and not panel.undo_button.isEnabled()
    # Undo brings back the later 音效 edit too, so the revision differs in one more field.
    assert panel.row_text("shot_1") == "第 1 镜 · 改了 4 处" and panel.apply_button.isEnabled()


def test_undo_returns_a_clean_page_and_later_edits_block_undo(chat_ui, scenario):
    ui, panel = chat_ui, chat_ui.panel.edit_panel
    ui.page.editor.table.selectRow(0)
    run_chat(ui, affected_reply({"镜号": 1, "改法": "放下钥匙"}, {"镜号": 2, "改法": "跟上"}))
    ui.panel.input.setPlainText("改")
    ui.panel.send()
    finish(ui)
    run_chat(ui, revised_reply(named(1, 画面="放下钥匙")))
    panel.request_edit()
    finish(ui)
    assert panel.row_text("shot_2") == "第 2 镜 · 未通过检查" and not panel.boxes["shot_2"].isEnabled()
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
    run_chat(ui, affected_reply({"镜号": 1, "改法": "很长的改法" * 40}))
    ui.panel.input.setPlainText("改")
    ui.panel.send()
    finish(ui)
    ui.panel.resize(280, 700)
    ui.panel.show()
    ui.app.processEvents()
    labels = panel.rows.findChildren(type(panel.title), "rowText")
    assert labels and all(label.wordWrap() for label in labels)
    assert panel.rows.width() <= ui.panel.width()
    assert ui.panel.messages.horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
    ui.panel.hide()


def test_switching_away_and_back_never_opens_chat_messages_as_windows(chat_ui):
    from test_comfyui_video import watch_windows

    ui = chat_ui
    ui.panel.resize(384, 700)
    ui.panel.show()
    ui.page.editor.table.selectRow(1)
    run_chat(ui, affected_reply(
        {"镜号": 1, "要改": ["画面"], "改法": "放下钥匙"},
        {"镜号": 3, "要改": ["画面"], "改法": "落点改为房子"},
    ))
    ui.panel.input.setPlainText("坠落物改成房子，后面怎么改？")
    ui.panel.send()
    finish(ui)
    ui.app.processEvents()
    assert ui.panel.edit_panel.isVisible()
    watch = watch_windows(ui.app)
    try:
        # A page switch redraws the chat twice in a row: once for the page change, once for the
        # workbench refresh. Messages from the first redraw must not open as windows.
        for active in (None, ui.page, None, ui.page):
            ui.active[0] = active
            ui.panel.refresh_reference()
            ui.panel.refresh()
            for _ in range(3):
                ui.app.processEvents()
        assert watch.opened == []
    finally:
        ui.app.removeEventFilter(watch)
    assert ui.panel.edit_panel.isVisible() and ui.panel.edit_panel.mode == "affected"


def test_affected_list_keeps_suggested_lengths_and_the_edit_request_passes_them_on(scenario, draft):
    reply = affected_reply(
        {"镜号": 1, "要改": ["画面", "时长"], "改法": "放慢", "建议时长": 9},
        {"镜号": 2, "改法": "跟上", "建议时长": "9秒"},
        {"镜号": 3, "改法": "收尾", "建议时长": 6.5},
    )
    items = affected_shots(reply, draft["shots"])["items"]
    assert [i.get("duration_ms") for i in items] == [9000, None, 6500]
    board = scenario.current("storyboard")
    value = storyboard_material(draft, "shot_1", board, all_records(scenario), edit_items=items)
    assert section(value["material"], "本轮要修改的镜头").splitlines() == [
        "第 1 镜 · 要改：画面、时长 · 改法：放慢 · 建议时长 9 秒",
        "第 2 镜 · 改法：跟上",
        "第 3 镜 · 改法：收尾 · 建议时长 6.5 秒",
    ]
    # The attachment already carries the total and project requirements.
    assert section(value["material"], "分镜").startswith(
        "共 3 镜，合计 21 秒；全片须少于 60 秒，项目没有设目标时长。"
    )


def test_rules_ask_for_lengths_and_table_names_and_a_card_even_for_one_shot(scenario, draft):
    message = {"id": identity("message"), "turn_id": identity("turn"), "role": "user", "content": "改", "status": "pending"}
    board = scenario.current("storyboard")
    plain = storyboard_material(draft, "shot_2", board, all_records(scenario))
    rules = build_conversation_context([message], message["id"], attachment=plain).request[0]["content"]
    for phrase in ("“时长：当前X秒 → 建议Y秒”", "“时长：保持X秒”", "少于60秒", "只改一镜也是", "“第N镜”",
                   "取景范围只写简短名称", "镜头运动用一句话", '"建议时长":6'):
        assert phrase in rules
    assert "只牵涉一个镜头" not in rules
    assert "音效、时长。" in rules and "画面提示词" not in rules
    edit = storyboard_material(draft, "shot_2", board, all_records(scenario), edit_items=[{"shot_id": "shot_1"}])
    edit_rules = build_conversation_context([message], message["id"], attachment=edit).request[0]["content"]
    assert "清单给了建议时长的镜头用这个时长" in edit_rules and "取景范围只写简短名称" in edit_rules
    assert "画面提示词" not in edit_rules


def test_one_shot_card_shows_the_suggested_length_and_the_revision_is_asked_to_follow_it(chat_ui):
    from core.gui.pages.short_video import shot_edit_panel

    ui, panel = chat_ui, chat_ui.panel.edit_panel
    ui.page.editor.table.selectRow(0)
    run_chat(ui, affected_reply({"镜号": 1, "要改": ["画面", "时长"], "改法": "放慢动作", "建议时长": 9}))
    ui.panel.input.setPlainText("这一镜动作太赶")
    ui.panel.send()
    finish(ui)
    assert not panel.isHidden() and list(panel.boxes) == ["shot_1"]
    chips = [c.text() for c in panel.shot_rows["shot_1"].findChildren(shot_edit_panel.QLabel, "chip")]
    assert chips == ["画面", "时长 7 → 9 秒"]
    panel.edit_button.click()
    request = ui.pending[-1].command["messages"]
    assert section(attached(request), "本轮要修改的镜头") == "第 1 镜 · 要改：画面、时长 · 改法：放慢动作 · 建议时长 9 秒"
    assert "清单给了建议时长的镜头用这个时长" in request[0]["content"]


def named(number, **changes):
    """A revised shot as current replies write it: form names, 「名称（状态）」 and seconds."""
    value = {"镜号": number, "取景范围": "中景", "镜头运动": "固定镜头", "场景": "老宅（常态）",
             "人物": ["寻井人（常态）"], "道具": [], "画面": "举起铜钥匙", "对白": [],
             "旁白": "井里是谁", "音效": "风声", "时长": 7}
    value.update(changes)
    return value


def named_affected(*entries, missing=()):
    return "## 影响\n几镜要一起改。\n" + block("affected_shots", {"镜头": list(entries), "缺少的资产": list(missing)})


def test_revised_shots_written_with_names_map_back_to_catalog_entries(scenario, draft):
    shots = {s["shot_id"]: s for s in draft["shots"]}
    order = [s["shot_id"] for s in draft["shots"]]
    reply = revised_reply(
        named(1, 人物=["寻井人（夏装）", "寻井人·2（常态）"], 时长=5.5, 对白=[
            {"说话人": "画外音", "台词": "门外有人。", "画外": True},
            {"说话人": "寻井人·2（常态）", "台词": "谁？", "画外": False},
        ]),
        # An object with one state needs only its name; ASCII brackets are read too.
        named(3, 取景范围="近景", 场景="老宅", 人物="寻井人·2(常态)"),
    )
    base = {"shot_1": shots["shot_1"], "shot_3": shots["shot_3"]}
    accepted, rejected = revised_shots(reply, base, entities(scenario), order)
    assert rejected == {}
    first = accepted["shot_1"]
    assert first["characters"] == [{"asset_id": "asset_character", "variant_id": "summer"},
                                   {"asset_id": "asset_twin", "variant_id": "base"}]
    assert first["props"] == [] and first["duration_ms"] == 5500
    assert first["dialogue"] == [
        {"character_id": "asset_voice", "text": "门外有人。", "voice_over": True},
        {"character_id": "asset_twin", "text": "谁？", "voice_over": False},
    ]
    assert first["shot_id"] == "shot_1" and first["source_refs"] == shots["shot_1"]["source_refs"]
    assert accepted["shot_3"]["scene_ref"] == {"asset_id": "asset_scene", "variant_id": "base"}
    assert accepted["shot_3"]["shot_size"] == "近景"
    assert accepted["shot_3"]["characters"] == [{"asset_id": "asset_twin", "variant_id": "base"}]


def test_old_storyboard_reply_formats_are_rejected(scenario, draft):
    base = {"shot_1": draft["shots"][0]}
    catalog = entities(scenario)
    legacy = {"shots": [{"shot_id": "shot_1", "order": 1, "fields": "画面", "change": "停下"}]}
    assert "缺少“镜头”列表" in affected_shots(block("affected_shots", legacy), draft["shots"])["error"]
    assert reply_problems(block("revised_shots", legacy), base, catalog) == (0, ["代码块缺少“镜头”列表"])
    assert revised_shots(revised_reply(legacy["shots"][0]), base, catalog) == ({}, {"shot_1": "AI 没有给出这一镜"})

    shot = named(1, 景别="近景")
    del shot["取景范围"]
    assert revised_shots(revised_reply(shot), base, catalog) == ({}, {"shot_1": "缺少取景范围"})


def test_names_that_dont_fit_the_catalog_are_explained_by_shot_number_and_name(scenario, draft):
    shot = draft["shots"][0]
    order = [s["shot_id"] for s in draft["shots"]]
    reply = revised_reply(named(
        1, 场景="老宅（夜晚）", 人物=["寻井人"], 道具=["房子（常态）"], 时长="七秒",
        对白=[{"说话人": "陌生人", "台词": "你好"}],
    ))
    _, rejected = revised_shots(reply, {"shot_1": shot}, entities(scenario), order)
    assert rejected["shot_1"].split("；") == [
        "场景“老宅”没有“夜晚”这个状态，资产目录里的状态：常态",
        "人物“寻井人”没有写状态，资产目录里的状态：常态｜夏装",
        "道具“房子（常态）”不在资产目录里",
        "说话人“陌生人”不在资产目录里",
        "时长要写成秒数，如 6.5",
    ]
    passed, problems = reply_problems(reply, {"shot_1": shot}, entities(scenario), order)
    assert passed == 0 and problems == ["第1镜：" + rejected["shot_1"]]
    # A name two objects share as an alias asks for the names the material shows.
    catalog = deepcopy(entities(scenario))
    for entity in catalog[:1] + [e for e in catalog if e["asset_id"] == "asset_twin"]:
        entity["aliases"] = ["井边人"]
    _, rejected = revised_shots(revised_reply(named(1, 人物=["井边人（常态）"])), {"shot_1": shot}, catalog, order)
    assert rejected["shot_1"] == "人物“井边人”对应多个对象，要写成“寻井人”或“寻井人·2”"
    _, rejected = revised_shots(revised_reply({"镜号": 1, "画面": "只给了画面"}), {"shot_1": shot}, catalog, order)
    assert rejected["shot_1"].startswith("缺少取景范围、镜头运动、场景")


def test_affected_list_written_with_form_names_and_seconds(draft):
    order = [s["shot_id"] for s in draft["shots"]]
    reply = named_affected(
        {"镜号": 3, "要改": ["画面", "时长"], "改法": "放慢", "建议时长": 6.5},
        {"镜号": 1, "要改": "人物", "改法": "去掉小羊", "建议时长": "9秒"},
        {"镜号": 9, "改法": "不在分镜中"},
        missing=["房子"],
    )
    listed = affected_shots(reply, draft["shots"], order)
    assert listed["items"] == [
        {"shot_id": "shot_1", "order": 1, "fields": "人物", "change": "去掉小羊"},
        {"shot_id": "shot_3", "order": 3, "fields": "画面、时长", "change": "放慢", "duration_ms": 6500},
    ]
    assert listed["missing_assets"] == ["房子"]
    # Later rounds get a note naming the shots instead of the block.
    assert history_text(reply) == "## 影响\n几镜要一起改。\n〔列出了受影响的镜头：第 1、3、9 镜〕"
    assert history_text(revised_reply(named(2))).endswith("〔给出了第 2 镜修改后的内容；是否写入以当前分镜为准〕")


def test_shot_numbers_in_replies_follow_the_order_when_the_question_was_sent(scenario, draft):
    order = [s["shot_id"] for s in draft["shots"]]
    # A shot added at the start after the question moves every shot down by one.
    shots = [{**deepcopy(draft["shots"][0]), "shot_id": "new_shot"}, *draft["shots"]]
    reply = named_affected({"镜号": 3, "改法": "看向井口"})
    assert [(i["shot_id"], i["order"]) for i in affected_shots(reply, shots, order)["items"]] == [("shot_3", 4)]
    # Questions saved before the order was kept count numbers in the current draft.
    assert [(i["shot_id"], i["order"]) for i in affected_shots(reply, shots)["items"]] == [("shot_2", 3)]
    current = {s["shot_id"]: s for s in shots}
    revision = revised_reply(named(3, 人物=["寻井人·2（常态）"], 画面="看向井口"))
    accepted, rejected = revised_shots(revision, {"shot_3": current["shot_3"]}, entities(scenario), order)
    assert rejected == {} and accepted["shot_3"]["action"] == "看向井口"


def test_reference_keeps_the_shot_order_and_rejects_one_that_disagrees():
    reference = {"shot_id": "b", "storyboard_id": "s", "order": 2, "unsaved": False}
    assert validate_storyboard_reference(reference) == reference
    assert validate_storyboard_reference({**reference, "shot_ids": ["a", "b"]})["shot_ids"] == ["a", "b"]
    for ids in (["b", "a"], ["a"], ["a", "b", "b"], "ab", ["a", ""]):
        with pytest.raises(WorkspaceError):
            validate_storyboard_reference({**reference, "shot_ids": ids})


def test_a_wrong_name_gets_one_repair_that_names_the_shot_and_the_name(scenario, draft, offline):
    bad = revised_reply(named(1, 道具=["房子（常态）"]))
    good = revised_reply(named(1, 画面="寻井人放下铜钥匙"))
    calls, replies = [], [bad, good]
    service = conversation.ConversationService(scenario, transport=lambda p: calls.append(p) or replies.pop(0))
    chat = service.store.create()
    service.execute(edit_command(service, chat["id"], scenario, draft, ["shot_1"]))
    assert calls[1][-1]["content"].startswith("上次回复有这些问题：\n- 第1镜：道具“房子（常态）”不在资产目录里")
    saved = service.store.get(chat["id"])["messages"]
    assert saved[1]["content"] == good
    assert saved[0]["storyboard_reference"]["shot_ids"] == ["shot_1", "shot_2", "shot_3"]


def test_cards_read_current_replies_that_name_shots_by_number(chat_ui):
    from core.gui.pages.short_video import shot_edit_panel

    ui, panel = chat_ui, chat_ui.panel.edit_panel
    ui.page.editor.table.selectRow(1)
    run_chat(ui, named_affected({"镜号": 3, "要改": ["人物"], "改法": "换夏装", "建议时长": 6}))
    ui.panel.input.setPlainText("第3镜换夏装")
    ui.panel.send()
    finish(ui)
    assert list(panel.boxes) == ["shot_3"]
    chips = [c.text() for c in panel.shot_rows["shot_3"].findChildren(shot_edit_panel.QLabel, "chip")]
    assert chips == ["人物", "时长 7 → 6 秒"]
    run_chat(ui, revised_reply(named(3, 人物=["寻井人·2（夏装）"], 时长=6)))
    panel.edit_button.click()
    sent = attached(ui.pending[-1].command["messages"])
    assert section(sent, "本轮要修改的镜头") == "第 3 镜 · 要改：人物 · 改法：换夏装 · 建议时长 6 秒"
    finish(ui)
    assert panel.row_text("shot_3") == "第 3 镜 · 改了 2 处"
    panel.apply_button.click()
    applied = ui.page.editor.draft_snapshot()["shots"][2]
    assert applied["characters"] == [{"asset_id": "asset_twin", "variant_id": "summer"}]
    assert applied["duration_ms"] == 6000
    # The next question carries the reply as a note naming the shot it revised.
    run_chat(ui, "好的。")
    ui.panel.input.setPlainText("还有别的问题吗")
    ui.panel.send()
    replies = [m["content"] for m in ui.pending[-1].command["messages"] if m["role"] == "assistant"]
    assert replies[-1].endswith("〔给出了第 3 镜修改后的内容；是否写入以当前分镜为准〕")
    assert replies[0].endswith("〔列出了受影响的镜头：第 3 镜〕")
