"""The storyboard page: a read-only shot list, a form for one shot, and shot operations that keep
every line once."""

import os
from copy import deepcopy

import pytest

from core.short_video.v2.models import WorkspaceError
from test_shot_assets import scenario, snapshot  # noqa: F401
from test_storyboard_chat import chat_ui, offline  # noqa: F401
from test_video_v2_repository import project, prompt_project  # noqa: F401


@pytest.fixture
def editor(scenario):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from core.gui.pages.short_video.v2_editor import StoryboardEditor

    app = QApplication.instance() or QApplication([])
    catalog = scenario.current("asset_catalog")["content"]["entities"]
    widget = StoryboardEditor()
    widget.catalog = catalog
    widget.names = {e["asset_id"]: e["name"] for e in catalog}
    widget.set_content(scenario.current("storyboard")["content"])
    widget.changes = []
    widget.changed.connect(lambda: widget.changes.append(True))
    yield widget
    widget.close()
    app.processEvents()


def shots(editor):
    return editor.draft_snapshot()["shots"]


def with_tail(shot, description, action=None):
    """A prompt as an earlier editor left it after an action edit."""
    action = shot["action"] if action is None else action
    shot["visual_description"] = description
    shot["visual_prompt_draft"] = f"{description}。{shot['shot_size']}，{shot['camera_movement']}。动作：{action}"


def test_shot_list_is_read_only_and_the_form_edits_the_selected_shot(editor):
    from PySide6.QtWidgets import QAbstractItemView, QPushButton

    table = editor.table
    assert [table.horizontalHeaderItem(c).text() for c in range(table.columnCount())] == [
        "镜号", "时长", "景别", "人物", "动作"
    ]
    assert table.editTriggers() == QAbstractItemView.NoEditTriggers and table.verticalHeader().isHidden()
    assert table.item(1, 1).text() == "7 秒" and table.item(1, 1).toolTip().startswith("00:07.0–00:14.0")
    table.selectRow(1)
    assert editor.heading.text() == "第 2 镜 · 00:07.0–00:14.0"
    assert editor.scene_label.text() == "老宅/常态" and editor.props_label.text() == "无"
    assert editor.detail_fields["characters"].toPlainText() == "寻井人"
    common = next(b for b in editor.form.findChildren(QPushButton) if b.text() == "常用景别")
    next(a for a in common.menu().actions() if a.text() == "近景").trigger()
    editor.detail_fields["camera_movement"].setPlainText("缓慢推近")
    editor.detail_fields["sound"].setPlainText("井水回声")
    shot = shots(editor)[1]
    assert (shot["shot_size"], shot["camera_movement"], shot["sound"]) == ("近景", "缓慢推近", "井水回声")
    assert table.item(1, 2).text() == "近景" and len(editor.changes) == 3


def test_editing_the_action_leaves_the_picture_prompt_alone(editor):
    editor.table.selectRow(0)
    prompt = shots(editor)[0]["visual_prompt_draft"]
    editor.detail_fields["action"].setPlainText("放下铜钥匙")
    editor.size_input.setText("特写")
    editor.detail_fields["camera_movement"].setPlainText("缓慢推近")
    shot = shots(editor)[0]
    assert shot["action"] == "放下铜钥匙" and shot["visual_prompt_draft"] == prompt
    assert "visual_description" not in shot and editor.table.item(0, 4).text() == "放下铜钥匙"
    assert not editor.prompt_note.isHidden() and editor.prompt_note.text() == "动作已改，画面提示词可能需要同步"
    editor.table.selectRow(1)
    assert editor.prompt_note.isHidden()
    editor.table.selectRow(0)
    assert not editor.prompt_note.isHidden()
    editor.detail_fields["visual_prompt_draft"].setPlainText("老宅井边放下铜钥匙")
    assert editor.prompt_note.isHidden() and shots(editor)[0]["visual_prompt_draft"] == "老宅井边放下铜钥匙"


def test_opening_removes_only_the_exact_tail_an_earlier_editor_appended(editor, scenario):
    content = deepcopy(scenario.current("storyboard")["content"])
    first, second, third = content["shots"]
    with_tail(first, "老宅井边举起铜钥匙。")
    with_tail(second, "井边", action="之后又改过的动作")  # The tail no longer matches the action.
    third["visual_description"] = third["visual_prompt_draft"]
    editor.changes.clear()
    editor.set_content(content)
    cleaned, kept, untouched = shots(editor)
    assert cleaned["visual_prompt_draft"] == "老宅井边举起铜钥匙。" and "visual_description" not in cleaned
    assert kept["visual_prompt_draft"] == second["visual_prompt_draft"] and kept["visual_description"] == "井边"
    assert untouched == {**third, "start_ms": 14000, "end_ms": 21000, "order": 3}
    assert editor.notice.text().startswith("第 1 镜的画面提示词末尾") and len(editor.changes) == 1
    # A storyboard without the tail opens unchanged and without the notice.
    editor.set_content(scenario.current("storyboard")["content"])
    assert editor.notice.isHidden() and len(editor.changes) == 1


def test_opening_the_page_with_an_old_tail_waits_for_save(chat_ui, scenario):
    ui = chat_ui
    board = scenario.current("storyboard")
    content = deepcopy(board["content"])
    with_tail(content["shots"][0], "老宅井边举起铜钥匙")
    scenario.save("storyboard", content, board["dependency_refs"], expected_revision=scenario.load()["revision"])
    before = snapshot(scenario)
    ui.page.refresh()
    assert ui.page.dirty and ui.page.status.text() == "有未保存修改"
    assert ui.page.editor.draft_snapshot()["shots"][0]["visual_prompt_draft"] == "老宅井边举起铜钥匙"
    assert snapshot(scenario) == before
    ui.workbench.update_shot_prompts = lambda: None
    ui.workbench.refresh = ui.page.refresh
    ui.page.save()
    saved = scenario.current("storyboard")["content"]["shots"][0]
    assert saved["visual_prompt_draft"] == "老宅井边举起铜钥匙" and "visual_description" not in saved
    assert not ui.page.dirty and ui.page.editor.notice.isHidden()


def test_split_keeps_each_line_once_and_selects_the_second_half(editor):
    editor.table.selectRow(0)
    editor.duration_input.setValue(5.5)
    editor.split()
    first, second = shots(editor)[:2]
    assert (first["duration_ms"], second["duration_ms"]) == (2800, 2700) and len(shots(editor)) == 4
    assert first["narration"] == "井里是谁" and first["sound"] == "风声" and first["dialogue"]
    assert (second["dialogue"], second["narration"], second["sound"]) == ([], "", "")
    assert second["action"] == first["action"] and second["visual_prompt_draft"] == first["visual_prompt_draft"]
    assert editor.table.currentRow() == 1 and editor.notice.text().startswith("第 1、2 镜内容相同")
    editor.split()  # 2.7 seconds still splits; 1.3 seconds is too short.
    editor.table.selectRow(1)
    editor.split()
    assert len(shots(editor)) == 5 and "不到 2 秒" in editor.notice.text()


def test_add_keeps_the_scene_and_saving_points_at_the_empty_fields(editor):
    editor.table.selectRow(1)
    editor.add_shot()
    base, added = shots(editor)[1:3]
    assert editor.table.currentRow() == 2 and added["scene_ref"] == base["scene_ref"]
    assert added["video_block_refs"] == base["video_block_refs"]
    assert all(added[k] == "" for k in ("shot_size", "camera_movement", "action", "visual_prompt_draft", "sound", "narration"))
    assert added["characters"] == added["props"] == added["dialogue"] == []
    assert editor.table.item(2, 4).text() == "（未填写动作）"
    editor.table.selectRow(0)
    with pytest.raises(WorkspaceError, match="第 3 镜的景别、镜头运动、动作、画面提示词还没填写"):
        editor.value()
    assert editor.table.currentRow() == 2
    assert editor.size_input.property("missing") and editor.detail_fields["action"].property("missing")
    editor.detail_fields["action"].setPlainText("\n放下铜钥匙\n")
    assert not editor.detail_fields["action"].property("missing")
    editor.size_input.setText("近景")
    editor.detail_fields["camera_movement"].setPlainText("固定")
    editor.detail_fields["visual_prompt_draft"].setPlainText("井边放下铜钥匙")
    assert editor.value()["shots"][2]["action"] == "放下铜钥匙"


def test_merge_keeps_unsaved_lines_and_joins_the_text(editor):
    editor.table.selectRow(0)
    editor.detail_fields["action"].setPlainText("举起铜钥匙。")
    editor.table.selectRow(1)
    editor.detail_fields["dialogue"].setPlainText("画外/画外音：有人吗\n旁白：风更大了")
    editor.table.selectRow(0)
    editor.merge()
    merged = shots(editor)[0]
    assert len(shots(editor)) == 2 and merged["duration_ms"] == 14000
    assert merged["dialogue_text"] == "画外/画外音：门外有人。\n旁白：井里是谁\n画外/画外音：有人吗\n旁白：风更大了"
    assert merged["action"] == "举起铜钥匙。举起铜钥匙" and merged["sound"] == "风声；风声"
    assert merged["visual_prompt_draft"] == "老宅井边举起铜钥匙；老宅井边举起铜钥匙"
    assert editor.notice.text().startswith("第 1、2 镜已合并为第 1 镜")
    saved = editor.value()["shots"][0]
    assert [(line["character_id"], line["text"]) for line in saved["dialogue"]] == [
        ("asset_voice", "门外有人。"), ("asset_voice", "有人吗")
    ]
    assert saved["narration"] == "井里是谁\n风更大了"
    # Each narration line comes back after 「旁白：」, so the box saves the same again.
    editor.set_content(editor.value())
    assert editor.detail_fields["dialogue"].toPlainText().endswith("旁白：井里是谁\n旁白：风更大了")
    # Typed names are carried over as typed, not dropped.
    editor.detail_fields["characters"].setPlainText("寻井人")
    editor.table.selectRow(1)
    editor.detail_fields["characters"].setPlainText("寻井人，画外音")
    editor.table.selectRow(0)
    editor.merge()
    assert shots(editor)[0]["character_text"] == "寻井人、画外音"


def test_lengths_are_hinted_and_the_total_is_compared_with_the_limit(editor, scenario):
    from core.gui import theme

    box = editor.duration_input
    assert (box.decimals(), box.singleStep(), box.minimum(), box.maximum()) == (1, 0.5, 1, 15)
    editor.table.selectRow(0)
    assert editor.duration_hint.text() == "常规 5–8 秒" and not editor.duration_hint.property("level")
    assert editor.summary.text() == "共 3 镜 · 21 秒 · 少于 60 秒"
    editor.duration_input.setValue(3.5)
    assert shots(editor)[0]["duration_ms"] == 3500 and shots(editor)[1]["start_ms"] == 3500
    assert editor.duration_hint.property("level") == "danger" and "最短生成 4 秒" in editor.duration_hint.text()
    length = editor.table.item(0, 1)
    assert length.foreground().color().name().upper() == theme.DANGER and not length.icon().isNull()
    editor.duration_input.setValue(9)
    assert editor.duration_hint.property("level") == "warn" and editor.summary.text() == "共 3 镜 · 23 秒 · 少于 60 秒"
    editor.target_ms = 20000
    editor.update_summary()
    assert editor.summary.text() == "共 3 镜 · 23 秒 · 比目标 20 秒多 3 秒" and editor.summary.property("warn")
    # Longer than one video run: the box still shows the real length, and the total warns.
    content = deepcopy(scenario.current("storyboard")["content"])
    for shot, duration in zip(content["shots"], (30000, 20000, 15500)):
        shot["duration_ms"] = duration
    editor.target_ms = None
    editor.set_content(content)
    editor.table.selectRow(0)
    assert box.value() == 30 and "超过 15 秒" in editor.duration_hint.text()
    assert editor.summary.text() == "共 3 镜 · 65.5 秒 · 比 60 秒上限多 5.5 秒" and editor.summary.property("warn")
    # Typing elsewhere never rounds a length the box shows with one decimal.
    content["shots"][1]["duration_ms"] = 2750
    editor.set_content(content)
    editor.table.selectRow(1)
    editor.detail_fields["action"].setPlainText("另一个动作")
    assert shots(editor)[1]["duration_ms"] == 2750
