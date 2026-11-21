"""The 分镜 page attaches its current content as readable text; chat never edits documents."""

import os
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.short_video.v2 import conversation, storyboard_chat
from core.short_video.v2.conversation_context import build_conversation_context
from core.short_video.v2.models import WorkspaceError, encode, identity
from core.short_video.v2.source import sentence_range
from core.short_video.v2.storyboard_chat import (
    asset_names,
    fit_shots,
    mentioned_shots,
    read_storyboard_material,
    shot_text,
    storyboard_material,
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


def material(repository, draft, shot_id="shot_2", board=None, **kwargs):
    board = board or repository.current("storyboard")
    return read_storyboard_material(repository, draft, shot_id, board, **kwargs)


def all_records(repository):
    state = repository.load()
    return {key: repository.record(key, state=state) for key in state["records"]}


def section(text, heading):
    """The body of the material's 「## heading…」 section."""
    start = text.index("\n## " + heading) + 1
    end = text.find("\n## ", start)
    return text[start : end if end > 0 else None].split("\n", 1)[1].strip()


def shot_block(text, number):
    start = text.index(f"### 第 {number} 镜\n")
    end = text.find("\n\n", start)
    return text[start : end if end > 0 else None]


def attached(request):
    """The material sent with the current message, before the question."""
    return request[-1]["content"].rsplit("\n\n【我的问题】\n", 1)[0]


def request_text(request):
    return "\n".join(message["content"] for message in request)


def test_material_writes_every_shot_with_form_fields_and_assets_by_name(scenario, draft):
    before = snapshot(scenario), deepcopy(draft)
    value = material(scenario, draft, unsaved=True, question="第3镜呢")
    text = value["material"]
    # The shot order goes with the reference, so later replies' shot numbers still find their shots.
    assert value["reference"] == {"shot_id": "shot_2", "order": 2, "unsaved": True,
                                  "storyboard_id": scenario.current("storyboard")["id"],
                                  "shot_ids": ["shot_1", "shot_2", "shot_3"]}
    assert (value["page"], value["label"], value["edit"], value["edit_shot_ids"], value["notes"]) == (
        "storyboard", "第 2 镜 · 含未保存修改", False, [], []
    )
    assert text.startswith("【附带资料】分镜页的当前内容（含未保存修改）。")
    assert section(text, "分镜").splitlines()[:2] == [
        "共 3 镜，合计 21 秒；全片须少于 60 秒，项目没有设目标时长。常规单镜 5–8 秒，"
        "插入和短反应镜头可以更短；视频模型一次能生成 4–15 秒。",
        "当前选中第 2 镜；你的消息提到第 3 镜。",
    ]
    assert shot_block(text, 2).splitlines() == [
        "### 第 2 镜", "时间段：7–14 秒", "时长：7 秒", "景别：中景", "镜头运动：固定镜头",
        "场景：老宅（常态）", "人物：寻井人（夏装）", "道具：无", "动作：界面最新动作", "对白：无",
        "旁白：井里是谁", "音效：风声", "画面提示词：老宅井边举起铜钥匙",
    ]
    # The other shots are complete too; objects sharing a name are told apart by 「·2」.
    assert "对白：画外/画外音：门外有人。" in shot_block(text, 1) and "画面提示词：" in shot_block(text, 1)
    assert "人物：寻井人·2（常态）" in shot_block(text, 3)
    # One object per line, states split by 「｜」 since a state may contain 「、」.
    assert section(text, "资产目录").splitlines() == [
        "人物：", "- 寻井人：常态｜夏装", "- 寻井人·2：常态｜夏装", "- 画外音〔只出声，不出镜〕",
        "场景：", "- 老宅：常态", "道具：", "- 铜钥匙：常态", "- 未使用的盒子：常态",
    ]
    # Specs only for the selected and the mentioned shot, without the view prompts.
    specs = section(text, "资产设定")
    assert "## 资产设定（第 2、3 镜用到的）" in text and "外貌：明确的视觉设定" in specs
    assert [line for line in specs.splitlines() if line.startswith("### ")] == [
        "### 场景 · 老宅（常态）", "### 人物 · 寻井人（夏装）", "### 人物 · 寻井人·2（常态）",
    ]
    # A short selection goes whole, so the model can check any shot against it.
    assert "## 原文（改编范围全文，22 字）" in text
    assert section(text, "原文") == "乡村😀老宅\n井口响了三声。\n父亲留下铜钥匙。"
    assert section(text, "项目要求") == "受众：中文短视频观众\n风格：自然、紧凑"
    for hidden in ("asset_", "shot_", "variant", "artifact_", "b1", "禁止改变", "## 资料说明"):
        assert hidden not in text
    assert (snapshot(scenario), draft) == before


def test_typed_names_not_yet_matched_stay_as_typed_with_a_note(scenario, draft):
    shot = draft["shots"][1]
    shot.update(character_text="还未说明身份的新人物、寻井人", narration="旧旁白不应再次出现",
                dialogue_text="画外/画外音：有人吗？\n寻井人：同名人物尚未确定\n新人：还没写完\n旁白：新的旁白")
    draft["shots"][0]["action"] = ""  # Another incomplete shot must not block this discussion.
    text = material(scenario, draft)["material"]
    block = shot_block(text, 2)
    assert ("人物：还未说明身份的新人物、寻井人"
            "〔资产目录里没有这个人物：还未说明身份的新人物〕〔对应多个人物：寻井人〕") in block
    assert ("对白：画外/画外音：有人吗？\n  寻井人：同名人物尚未确定\n"
            "  新人：还没写完〔资产目录里没有这个说话人：新人〕〔说话人对应多个人物：寻井人〕") in block
    assert "旁白：新的旁白" in block and "旧旁白不应再次出现" not in text
    assert "动作：无" in shot_block(text, 1)
    # Only objects identified for sure bring their specs: the scene and the voice, neither twin.
    headings = [line for line in section(text, "资产设定").splitlines() if line.startswith("### ")]
    assert headings == ["### 场景 · 老宅（常态）", "### 人物 · 画外音（常态）"]
    assert "### 人物 · 画外音（常态）\n（基础设定还没有生成）" in text


def test_missing_bound_material_is_noted_and_newer_versions_are_not_used(scenario, draft):
    board, catalog = scenario.current("storyboard"), scenario.current("asset_catalog")
    records = all_records(scenario)
    content = deepcopy(catalog["content"])
    content["entities"][0]["name"] = "改名后的新版本"
    scenario.save("asset_catalog", content, catalog["dependency_refs"],
                  expected_revision=scenario.load()["revision"])
    # A replaced catalog leaves this storyboard without its own; the newer one is never used.
    replaced = material(scenario, draft, board=board)
    assert "改名后的新版本" not in replaced["material"]
    assert replaced["notes"] == ["本版分镜绑定的资产目录缺失，不能确认人物与资产身份。"]
    value = storyboard_material(draft, "shot_2", board, {})
    assert "场景：（资产目录里没有的对象）" in shot_block(value["material"], 2)
    assert value["notes"] == [
        "本版分镜绑定的资产目录缺失，不能确认人物与资产身份。",
        "本版分镜绑定的原文或选区缺失，不能核对原文。",
        "本版分镜绑定的项目要求缺失。",
    ]
    assert section(value["material"], "资料说明").splitlines() == ["- " + n for n in value["notes"]]
    for heading in ("## 资产目录", "## 资产设定", "## 原文", "## 项目要求"):
        assert heading not in value["material"]
    # With a catalog, a shot bound to an object it lacks is said once.
    draft["shots"][1]["props"] = [{"asset_id": "gone", "variant_id": "base"}] * 2
    value = storyboard_material(draft, "shot_2", board, records)
    assert value["notes"] == ["有镜头绑定了资产目录里没有的对象。"]
    assert "道具：（资产目录里没有的对象）、（资产目录里没有的对象）" in shot_block(value["material"], 2)


@pytest.mark.parametrize("failure", ["invalid_range", "false_quote", "wrong_source", "budget"])
def test_invalid_or_oversized_original_parts_are_left_out_whole(scenario, draft, failure, monkeypatch):
    # A selection over the limit sends only the parts the discussed shots cite.
    monkeypatch.setattr(storyboard_chat, "SOURCE_CHARS", 1 if failure == "budget" else 10)
    refs = draft["shots"][1]["source_refs"]
    if failure == "invalid_range":
        refs[:] = [{**refs[0], "end": 999999}]
    elif failure == "false_quote":
        refs[:] = [{**refs[0], "quote": "伪造引文"}]
    elif failure == "wrong_source":
        refs[:] = [{**refs[0], "source_id": "another-source"}]
    value = material(scenario, draft)
    assert "## 原文" not in value["material"] and "井口响了三声" not in value["material"]
    first = "一段原文超出篇幅，没有附带（7 字）。" if failure == "budget" else "第 2 镜有一处无效的原文引用，没有附带。"
    assert value["notes"] == [first, "本轮没有可用的原文片段，不能说核实过原文。"]
    assert section(value["material"], "资料说明").endswith("- 本轮没有可用的原文片段，不能说核实过原文。")


@pytest.mark.parametrize("start,end", [(7, 15), (8, 10), (5, 12)])
def test_drifted_original_positions_are_sent_as_whole_sentences(scenario, draft, start, end, monkeypatch):
    monkeypatch.setattr(storyboard_chat, "SOURCE_CHARS", 10)
    source_id = scenario.current("source")["id"]
    draft["shots"][1]["source_refs"] = [{"source_id": source_id, "start": start, "end": end}]
    value = material(scenario, draft)
    assert "## 原文（第 2 镜对应的片段）" in value["material"]
    assert section(value["material"], "原文") == "井口响了三声。"
    assert value["notes"] == []


def long_draft(draft):
    for number in range(4, 8):
        shot = deepcopy(draft["shots"][2])
        shot.update(shot_id=f"shot_{number}", action=f"动作{number}" + "很长的描述" * 20)
        draft["shots"].append(shot)
    draft["shots"][4]["character_text"] = "还未保存的新人物"
    return draft


def test_fit_shots_keeps_focus_and_neighbours_and_shortens_the_farthest_first():
    # Eight shots whose four forms are 40, 30, 20 and 10 characters; each sent shot adds 2.
    blocks = [["x" * (40 - 10 * level) for level in range(4)] + [""] for _ in range(8)]
    assert fit_shots(blocks, {1}, 336) == ([0] * 8, 336)
    assert fit_shots(blocks, {1}, 335) == ([0, 0, 0, 0, 0, 0, 0, 1], 326)
    assert fit_shots(blocks, {1}, 300) == ([0, 0, 0, 0, 1, 1, 1, 1], 296)
    # Every other shot is shortened one step before any is shortened again.
    assert fit_shots(blocks, {1}, 200) == ([0, 0, 0, 2, 3, 3, 3, 3], 196)
    # Shots in focus and their neighbours stay whole even when nothing else is left.
    assert fit_shots(blocks, {1}, 10) == ([0, 0, 0, 4, 4, 4, 4, 4], 126)
    # With two shots in focus, the shot midway between them is the farthest.
    assert fit_shots(blocks, {0, 7}, 335) == ([0, 0, 0, 0, 1, 0, 0, 0], 326)


def test_shortened_shots_drop_the_prompt_then_clip_action_and_sound_then_keep_the_basics(scenario):
    entities = scenario.current("asset_catalog")["content"]["entities"]
    shot = deepcopy(scenario.current("storyboard")["content"]["shots"][1])
    shot.update(action="很长" * 40, sound="声" * 70)
    full, no_prompt, clipped, basics = (
        shot_text(shot, 2, 7000, entities, asset_names(entities), level) for level in range(4)
    )
    assert full == no_prompt + "\n画面提示词：老宅井边举起铜钥匙"
    assert "动作：" + "很长" * 30 + "…" in clipped and "音效：" + "声" * 60 + "…" in clipped
    assert clipped.replace("很长" * 30 + "…", "很长" * 40).replace("声" * 60 + "…", "声" * 70) == no_prompt
    assert basics == "### 第 2 镜\n时长：7 秒；场景：老宅（常态）；人物：寻井人（夏装）"


def test_long_storyboards_shorten_far_shots_first_and_say_so(scenario, draft):
    board, records = scenario.current("storyboard"), all_records(scenario)
    draft = long_draft(draft)
    whole = storyboard_material(draft, "shot_2", board, records)
    assert whole["notes"] == [] and all(f"### 第 {n} 镜\n" in whole["material"] for n in range(1, 8))
    assert "人物：还未保存的新人物〔资产目录里没有这个人物：还未保存的新人物〕" in shot_block(whole["material"], 5)
    # The budget counts the shots as sent, each followed by a blank line.
    size = len(section(whole["material"], "分镜").split("\n\n", 1)[1]) + 2
    assert storyboard_material(draft, "shot_2", board, records, budget=size)["notes"] == []
    tight = storyboard_material(draft, "shot_2", board, records, budget=size - 1)
    assert tight["notes"] == ["第 7 镜省去了画面提示词。"]
    assert "画面提示词：" not in shot_block(tight["material"], 7) and "画面提示词：" in shot_block(tight["material"], 6)
    assert section(tight["material"], "资料说明") == "- 第 7 镜省去了画面提示词。"
    least = storyboard_material(draft, "shot_2", board, records, budget=1)
    assert least["notes"] == ["第 4–7 镜没有附带。"]
    assert "共 7 镜" in least["material"] and "### 第 4 镜" not in least["material"]
    assert all("画面提示词：" in shot_block(least["material"], n) for n in (1, 2, 3))
    # A shot the message names keeps itself and its neighbours whole.
    named = storyboard_material(draft, "shot_2", board, records, question="第6镜呢", budget=1)
    assert named["notes"] == ["第 4 镜没有附带。"]
    assert "当前选中第 2 镜；你的消息提到第 6 镜。" in named["material"]


@pytest.mark.parametrize("question,expected", [
    ("第5镜和第7镜怎么衔接", [5, 7]),
    ("第五个镜头太长", [5]),
    ("镜头5、6的动作", [5, 6]),
    ("第3到5镜", [3, 4, 5]),
    ("第3-5镜和第十镜", [3, 4, 5, 10]),
    ("7号镜头", [7]),
    ("第二、第四镜", [2, 4]),
    ("第十二个分镜", [12]),
    ("第20镜", []),
    ("这一镜的动作", []),
    ("第一次见面", []),
])
def test_messages_name_shots_by_number(question, expected):
    assert mentioned_shots(question, 12) == expected


SNAP_TEXT ="周围死寂。\n\n就在他快要撑不住时，小羊从天而降。暴风雪吞没了视线。\n\n他说：“走吧。”然后离开。"


def snap(start, end, lo=0, hi=len(SNAP_TEXT), value=SNAP_TEXT):
    s, e = sentence_range(value, start, end, lo, hi)
    return value[s:e]


def test_sentence_range_pulls_back_small_overshoot_and_extends_mid_sentence_cuts():
    after_first = SNAP_TEXT.index("就")
    # End overshoots a few characters into the next sentence: pulled back, not extended.
    assert snap(0, after_first + 3) == "周围死寂。"
    # Start drops the first character of a sentence: moved back to the sentence start.
    # End stops mid-sentence, nearer the next full stop: extended to it.
    begin = SNAP_TEXT.index("在他快")
    cut = SNAP_TEXT.index("了视线")
    assert snap(begin, cut) == "就在他快要撑不住时，小羊从天而降。暴风雪吞没了视线。"
    # Start a few characters before the next sentence: the fragment is dropped.
    assert snap(SNAP_TEXT.index("了视线"), len(SNAP_TEXT)) == "他说：“走吧。”然后离开。"


def test_sentence_range_keeps_closing_quotes_and_selection_limits():
    quote_end = SNAP_TEXT.index("。”") + 1
    assert snap(SNAP_TEXT.index("他说"), quote_end) == "他说：“走吧。”"
    lo, hi = SNAP_TEXT.index("小羊"), SNAP_TEXT.index("暴风雪") + 2
    assert snap(lo + 1, hi - 1, lo, hi) == "小羊从天而降。暴风"
    # A short piece inside one sentence becomes that sentence.
    assert snap(SNAP_TEXT.index("撑不住"), SNAP_TEXT.index("撑不住") + 3) == "就在他快要撑不住时，小羊从天而降。"


def test_sentence_range_keeps_coordinates_without_nearby_punctuation():
    value = "甲" * 400
    assert sentence_range(value, 180, 220, 0, len(value)) == (180, 220)


def test_single_new_shot_can_be_discussed_without_generated_specs(scenario, draft):
    board = scenario.current("storyboard")
    draft["shots"] = [{**draft["shots"][1], "shot_id": "new-unsaved-shot"}]
    records = {k: r for k, r in all_records(scenario).items() if not r["kind"].endswith("_spec")}
    value = storyboard_material(draft, "new-unsaved-shot", board, records, unsaved=True)
    assert value["reference"]["order"] == 1 and "共 1 镜，合计 7 秒" in value["material"]
    assert section(value["material"], "资产设定").count("（基础设定还没有生成）") == 2
    with pytest.raises(WorkspaceError, match="重新选择"):
        storyboard_material(draft, "removed-shot", board, records)


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
    frozen = material(scenario, draft, unsaved=True)
    command = service.prepare(chat["id"], "帮我修改这个动作", attachment=frozen)
    frozen["material"] = "准备之后再次修改，不应进入已经冻结的请求"
    draft["shots"][1]["action"] = "界面继续编辑"
    service.execute(command)
    assert len(calls) == 1 and "动作：界面最新动作" in shot_block(attached(calls[0]), 2)
    # The rules go first on their own; the material comes before the question, which is last.
    assert [m["role"] for m in calls[0]] == ["system", "user"]
    assert "## 本页要求" in calls[0][0]["content"] and "界面最新动作" not in calls[0][0]["content"]
    assert calls[0][-1]["content"].endswith("\n\n【我的问题】\n帮我修改这个动作")
    saved = service.store.get(chat["id"])
    user = saved["messages"][0]
    assert user["content"] == "帮我修改这个动作" and user["storyboard_reference"]["shot_id"] == "shot_2"
    assert "【附带资料】" not in encode(user) and "material" not in user
    assert saved["messages"][1]["content"].startswith("## 建议")
    assert scenario.load()["current"] == current_before
    assert artifacts_before == {p.name: p.read_bytes() for p in (scenario.root / "artifacts").glob("*.json")}
    next_command = service.prepare(chat["id"], "按刚才的建议再简化", attachment=material(scenario, draft, unsaved=True))
    request = next_command["messages"]
    assert [m["role"] for m in request] == ["system", "user", "assistant", "user"]
    assert "动作：界面继续编辑" in shot_block(attached(request), 2)
    assert "界面最新动作" not in request_text(request)
    # Earlier questions say which shot they were about; their material is not sent again.
    assert request[1]["content"] == "〔当时附带分镜，选中第 2 镜〕\n帮我修改这个动作"


def test_context_budget_counts_attachment_and_drops_history_before_refusing(scenario, draft):
    attachment = material(scenario, draft)
    user_id, turn = identity("message"), identity("turn")
    history = [
        {"id": user_id, "turn_id": turn, "role": "user", "content": "旧问题" * 100, "status": "complete"},
        {"id": identity("message"), "turn_id": turn, "parent_message_id": user_id, "role": "assistant", "content": "旧答复" * 100, "status": "complete"},
        {"id": identity("message"), "turn_id": identity("turn"), "role": "user", "content": "当前问题", "status": "pending"},
    ]
    minimum = build_conversation_context(history[-1:], history[-1]["id"], attachment=attachment)
    bounded = build_conversation_context(history, history[-1]["id"], attachment=attachment, max_context_chars=minimum.character_count)
    assert bounded.omitted_message_ids == tuple(m["id"] for m in history[:-1])
    assert request_text(bounded.request).count("界面最新动作") == 1
    with pytest.raises(WorkspaceError, match="取消附带"):
        build_conversation_context(history, history[-1]["id"], attachment=attachment, max_context_chars=minimum.character_count - 1)


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
    )
    from core.gui.pages.short_video.task_channels import TaskChannels
    workbench.tasks = TaskChannels()
    workbench.tasks.runners["text"] = SimpleNamespace(busy=False, cancel_current=Mock())
    page = ArtifactPage(workbench, "storyboard", "分镜")
    workbench.storyboard = page
    active = [page]
    workbench.current_artifact_page = lambda: active[0]
    workbench.shown_pages = lambda: [active[0]] if active[0] is not None else []
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
    from core.gui.pages.short_video.request_preview import RequestPreviewDialog

    ui = chat_ui
    before = snapshot(scenario)
    ui.page.editor.table.selectRow(1)
    ui.page.editor.detail_fields["action"].setPlainText("编辑中，尚未保存")
    ui.page.editor.detail_fields["dialogue"].setPlainText("新名字：对白还在写\n旁白：新旁白")
    assert ui.page.dirty and "第 2 镜" in ui.panel.context_label.text() and "未保存" in ui.panel.context_label.text()
    ui.page.editor.content["shots"][0]["action"] = ""
    before_draft = ui.page.editor.draft_snapshot()
    shown = []
    monkeypatch.setattr(RequestPreviewDialog, "exec", lambda dialog: shown.append(dialog))
    ui.panel.preview_context_button.click()
    sent = shown[0].body.toPlainText()
    assert ui.panel.selected_attachment()["material"] in sent
    block = shot_block(sent, 2)
    assert "动作：编辑中，尚未保存" in block and "旁白：新旁白" in block and "井里是谁" not in block
    assert "对白：新名字：对白还在写〔资产目录里没有这个说话人：新名字〕" in block
    assert ui.page.editor.draft_snapshot() == before_draft and ui.page.dirty
    assert snapshot(scenario) == before and ui.pending == []
    ui.page.editor.table.selectRow(2)
    assert "第 3 镜" in ui.panel.context_label.text()
    ui.panel.attach_shot.detach_button.click()
    assert ui.panel.selected_attachment() is None and not ui.panel.attach_shot.isChecked()
    assert ui.panel.attach_shot.chip.isHidden() and not ui.panel.attach_shot.attach_button.isHidden()
    assert ui.panel.preview_context_button.isHidden()
    ui.panel.attach_shot.attach_button.click()
    assert "第 3 镜" in ui.panel.context_label.text() and not ui.panel.attach_shot.chip.isHidden()
    ui.active[0] = None
    ui.panel.refresh_reference()
    assert ui.panel.context_panel.isHidden() and ui.panel.selected_attachment() is None
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
    sent = attached(command["messages"])
    assert "当前选中第 2 镜" in sent and "动作：发送时的动作" in shot_block(sent, 2)
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
    request = ui.pending[0].command["messages"]
    assert [m["role"] for m in request] == ["system", "user"] and request[1]["content"] == "普通聊天问题"
    assert "本页要求" not in request[0]["content"]
    assert "storyboard_reference" not in ui.pending[0].command["context_snapshot"]
    ui.active[0] = ui.page
    ui.page.editor.set_content(None)
    ui.panel.refresh_reference()
    assert ui.panel.selected_attachment() is None
    assert not ui.panel.attach_shot.isEnabled() and ui.panel.attach_shot.chip.isHidden()
    assert ui.panel.attach_shot.attach_button.toolTip() == "先在分镜表格里选中要讨论的镜头"


def test_failed_chat_retry_reads_new_draft_and_cancel_preserves_documents(chat_ui, scenario):
    from core.common.cancellation import GenerationCancelled

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
    sent = request_text(latest.command["messages"])
    assert "重试时最新的编辑" in sent and "失败前的编辑" not in sent
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
    assert ui.panel.preview_context_button.isVisible() and ui.panel.input.height() >= 52
    assert "整份分镜" in ui.panel.attach_shot.chip.toolTip()
    ui.panel.attach_shot.setChecked(False)
    assert ui.panel.input.toPlainText() == "还未发送的问题" and not ui.panel.preview_context_button.isVisible()
    ui.panel.attach_shot.setChecked(True)
    assert ui.panel.preview_context_button.isVisible() and ui.panel.attach_shot.chip.isVisible()
    # The input grows with long text but stops at about five lines.
    ui.panel.input.setPlainText("\n".join(["一行"] * 20))
    assert 52 < ui.panel.input.height() <= 120


def test_send_turns_into_stop_while_this_conversation_waits_for_a_reply(chat_ui):
    ui = chat_ui
    ui.panel.input.setPlainText("问个问题")
    ui.panel.send()
    ui.workbench.tasks.runners["text"].busy = True
    ui.panel.refresh()
    assert ui.panel.send_button.text() == "停止" and ui.panel.send_button.isEnabled()
    assert ui.panel.messages.toPlainText().endswith("正在回复…")
    ui.panel.send_button.click()
    ui.workbench.tasks.runners["text"].cancel_current.assert_called_once()
    # Another conversation's reply does not turn this one's button into 「停止」.
    ui.panel.create()
    assert ui.panel.send_button.text() == "发送"
    ui.workbench.tasks.runners["text"].busy = False
    ui.workbench.tasks.works.pop("text")
    ui.panel.refresh_send_button()
    assert ui.panel.send_button.text() == "发送" and ui.panel.send_button.isEnabled()


def test_preview_is_the_request_a_send_would_make_and_saves_nothing(scenario, draft, offline):
    service = conversation.ConversationService(scenario, transport=lambda _: "## 建议\n简化动作。")
    chat = service.store.create()
    service.execute(service.prepare(chat["id"], "第一个问题", attachment=material(scenario, draft)))
    before = snapshot(scenario)
    attachment = material(scenario, draft, question="第3镜呢")
    preview = service.preview(chat["id"], "第3镜呢", attachment=attachment)
    assert snapshot(scenario) == before
    assert preview.request == service.prepare(chat["id"], "第3镜呢", attachment=attachment)["messages"]
    # A new conversation with nothing typed yet still shows what a send would carry.
    empty = service.preview(None, "  ")
    assert [m["role"] for m in empty.request] == ["system", "user"]
    assert empty.request[-1]["content"] == conversation.PREVIEW_QUESTION
    assert "## 本页要求" not in empty.request[0]["content"]


def test_context_hint_says_what_a_send_leaves_out():
    from core.gui.pages.short_video.request_preview import context_hint
    from core.short_video.v2.conversation_context import ConversationContext

    def leaving_out(turns=0, omitted=0, shortened=0):
        return context_hint(ConversationContext(
            [], [], tuple(f"m{i}" for i in range(2 * turns)),
            tuple(f"s{i}" for i in range(shortened)), tuple(f"o{i}" for i in range(omitted)),
        ))

    assert leaving_out() == ""
    assert leaving_out(omitted=3) == "较早 3 轮的 AI 回复没有带上，你的消息都带上了"
    assert leaving_out(shortened=2) == "较早 2 轮的 AI 回复只带了开头，你的消息都带上了"
    assert leaving_out(omitted=3, shortened=2) == (
        "较早 3 轮的 AI 回复没有带上，2 轮的 AI 回复只带了开头，你的消息都带上了"
    )
    assert leaving_out(turns=1) == "最早 1 轮对话没有带上"
    assert leaving_out(turns=2, omitted=4) == "最早 2 轮对话没有带上；之后 4 轮的 AI 回复没有带上"


def chat_turns(ui, monkeypatch, replies, limit=48000):
    """Plain chat turns through the panel, with a service whose capacity the test sets."""
    service = conversation.ConversationService(ui.workbench.repository, max_context_chars=limit)
    monkeypatch.setattr(ui.panel, "service", lambda: service)
    for number, reply in enumerate(replies, 1):
        ui.workbench.transport = lambda _, reply=reply: reply
        ui.panel.input.setPlainText(f"问题{number}")
        ui.panel.send()
        ui.pending[-1](lambda *_: None)
        ui.workbench.tasks.works.pop("text", None)
    ui.panel.refresh()
    return service


def test_preview_dialog_shows_every_part_as_sent_with_the_rules_folded(chat_ui, monkeypatch):
    from core.gui.pages.short_video.request_preview import RequestPreviewDialog

    ui = chat_ui
    ui.page.editor.table.selectRow(1)
    service = chat_turns(ui, monkeypatch, ["建议只保留一个动作。"])
    shown = []
    monkeypatch.setattr(RequestPreviewDialog, "exec", lambda dialog: shown.append(dialog))
    ui.panel.input.setPlainText("第3镜呢")
    ui.panel.preview_context_button.click()
    dialog = shown[0]
    context, attached_now = ui.panel.request_preview()
    assert attached_now and len(ui.pending) == 1
    assert dialog.rules.isHidden() and dialog.rules.toPlainText() == context.request[0]["content"]
    assert dialog.rules_button.text() == f"规则（{len(context.request[0]['content'])} 字）"
    dialog.rules_button.click()
    assert not dialog.rules.isHidden()
    body = dialog.body.toPlainText()
    assert body.startswith(
        "── 第 1 轮 · 你 ──\n〔当时附带分镜，选中第 2 镜〕\n问题1\n\n── 第 1 轮 · AI ──\n建议只保留一个动作。\n\n"
        "── 本次发送 ──\n【附带资料】分镜页的当前内容（已保存）。"
    )
    assert "当前选中第 2 镜；你的消息提到第 3 镜。" in body and body.endswith("\n\n【我的问题】\n第3镜呢")
    sizes = [len(m["content"]) for m in context.request]
    assert dialog.summary.text() == (
        f"点发送后，AI 会收到下面这些内容，共 {sum(sizes)} 字（上限 48000 字）：\n"
        f"规则 {sizes[0]} 字 · 之前的 1 轮对话 {sizes[1] + sizes[2]} 字 · 附带内容和你的消息 {sizes[3]} 字。"
    )
    assert service.store.get(ui.panel.conversation_id)["messages"][-1]["content"] == "建议只保留一个动作。"


def test_hint_under_the_input_says_which_replies_a_send_leaves_out(chat_ui, monkeypatch):
    from core.gui.pages.short_video.request_preview import RequestPreviewDialog

    ui = chat_ui
    ui.active[0] = None
    long = "## 分析\n第一段结论。\n\n" + "细节" * 300
    service = chat_turns(ui, monkeypatch, [long, long, "好的。"])
    ui.panel.update_context_hint()
    assert ui.panel.context_hint.isHidden() and ui.panel.context_hint.text() == ""
    full = service.preview(ui.panel.conversation_id, "").character_count
    service.max_context_chars = full - 1
    ui.panel.update_context_hint()
    assert ui.panel.context_hint.text() == "较早 1 轮的 AI 回复只带了开头，你的消息都带上了"
    assert not ui.panel.context_hint.isHidden() and not ui.panel.context_hint.property("warning")
    shown = []
    monkeypatch.setattr(RequestPreviewDialog, "exec", lambda dialog: shown.append(dialog))
    ui.panel.preview_context()
    body = shown[0].body.toPlainText()
    assert "── 第 1 轮 · AI（只带了开头） ──\n第一段结论。\n〔后面约 600 字因篇幅省略〕" in body
    assert "── 第 2 轮 · AI ──\n## 分析" in body and "── 本次发送 ──\n（输入框里的消息）" in body
    assert "较早 1 轮的 AI 回复只带了开头，你的消息都带上了；缩短的回复里写明了省略多少。" in shown[0].summary.text()
    # When even the rules and this message don't fit, the hint warns before sending.
    service.max_context_chars = 10
    ui.panel.update_context_hint()
    assert "已超过容量" in ui.panel.context_hint.text() and ui.panel.context_hint.property("warning")
    # Typing or editing restarts a short wait instead of working the hint out on every key.
    ui.panel.hint_timer.stop()
    ui.panel.input.setPlainText("新问题")
    assert ui.panel.hint_timer.isActive()
