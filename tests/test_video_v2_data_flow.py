"""UI → repository → real SDK boundary → repository → UI; no paid requests here."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QMessageBox

from core.generation import api_providers
from core.short_video.v2.models import ASSET_KINDS, WorkspaceError
from core.short_video.v2.repository import ProjectRepository
from core.short_video.v2.source import codepoint_to_qt
from core.short_video.v2.workflow import WorkflowService
from core.short_video.v2.conversation import ConversationService
from core.short_video.v2.project_service import ProjectService
from core.short_video.v2.validation import local_issues, view_input_hash
from core.short_video.v2.exporter import ExportService
from test_short_video_ui import qapp, video_window, enter, source, wait
from test_video_v2_workflow import sample, prepare_story, generate


def sdk(monkeypatch, responses):
    requests, responses = [], iter(responses)
    monkeypatch.setattr(api_providers, "get_config", lambda provider: api_providers.APIConfig(
        provider, "https://offline.example/v1", "model-selected-in-ui", "test-only"))
    class Client:
        def __init__(self, **options):
            assert options["max_retries"] == 0 and options["timeout"] <= 120
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def create(self, **params):
            requests.append(deepcopy(params))
            result = next(responses)
            if isinstance(result, Exception):
                raise result
            content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    monkeypatch.setattr(api_providers, "AsyncOpenAI", Client)
    return requests


def finish(qapp, window):
    wait(qapp, lambda: window.runner.current is None)


def test_widget_inputs_sdk_edit_next_stage_and_chat_discussion(qapp, video_window, monkeypatch):
    window = video_window
    workbench = enter(window)
    window._restore_model("deepseek")
    passage = "我回到乡村老宅😀，井边有父亲的铜钥匙。手机录音说：别回答。"
    article = "不应发送的序章\n" + passage + "\n不应发送的尾声"
    page = workbench.source
    page.article.setPlainText(article)
    page.title.setText("界面输入到模型核验")
    page.fields["tone"].setText("克制，短句")
    page.fields["target_duration_ms"].setText("120")
    for group, name in [("themes", "脑洞"), ("settings", "反转"), ("backgrounds", "乡村")]:
        next(c for c in page.track_boxes[group] if c.text() == name).setChecked(True)
    start = article.index(passage)
    cursor = page.article.textCursor()
    cursor.setPosition(codepoint_to_qt(article, start))
    cursor.setPosition(codepoint_to_qt(article, start + len(passage)), QTextCursor.KeepAnchor)
    page.article.setTextCursor(cursor)
    page.add_range()
    page.save_button.click()
    requests = sdk(monkeypatch, [sample(workbench.repository, "novel_copy"), {"issues": []}, sample(workbench.repository, "video_copy"), {"issues": []}, "这是模型返回的改进结尾。"])
    page.generate_button.click()
    finish(qapp, window)
    prompt = requests[0]["messages"][0]["content"]
    assert passage in prompt and "不应发送的序章" not in prompt and "不应发送的尾声" not in prompt
    assert '"target_duration_ms": 120000' in prompt and "反转" in prompt
    assert requests[0]["model"] == "model-selected-in-ui"
    workbench.novel.editor.fields[0][2].setPlainText("先别碰铜钥匙，这是手改的独特开头。")
    workbench.novel.next_button.click()
    finish(qapp, window)
    edited = workbench.repository.current("novel_copy")
    assert edited["id"] in requests[2]["messages"][0]["content"]
    assert edited["content"]["opening"]["text"] in requests[2]["messages"][0]["content"]
    video = workbench.repository.current("video_copy")
    workbench.chat.input.setPlainText("只改进结尾，保留原文事实。")
    workbench.chat.send_button.click()
    finish(qapp, window)
    context = json.loads(requests[4]["messages"][0]["content"].split("\n", 1)[1])
    assert set(context) == {"conversation"}
    assert passage not in requests[4]["messages"][0]["content"]
    assert video["id"] not in requests[4]["messages"][0]["content"]
    assert workbench.repository.current("video_copy")["id"] == video["id"]
    assert "这是模型返回的改进结尾。" in workbench.chat.messages.toPlainText()
    assert workbench.video.editor.value() == video["content"]
    workbench.video.editor.fields[3][2].setPlainText("这是我手动填写的第二段口播。")
    workbench.video.save_button.click()
    edited_video = ProjectRepository(workbench.output_dir).current("video_copy")
    assert edited_video["parent_id"] == video["id"]
    assert edited_video["content"]["blocks"][1]["narration"] == "这是我手动填写的第二段口播。"
    assert edited_video["content"]["blocks"][1]["visible_action"] == video["content"]["blocks"][1]["visible_action"]
    assert workbench.video.editor.value() == edited_video["content"] and len(requests) == 5


def test_sdk_failure_keeps_current_draft_and_error(qapp, video_window, monkeypatch):
    workbench = enter(video_window)
    source(workbench)
    old = generate(workbench.repository, "novel_copy")
    workbench.refresh()
    requests = sdk(monkeypatch, [ConnectionError("private-test-token")])
    workbench.novel.generate_button.click()
    finish(qapp, video_window)
    assert len(requests) == 1 and workbench.repository.current("novel_copy")["id"] == old["id"]
    assert workbench.novel.editor.value() == old["content"]
    from core.short_video.v2.models import encode
    assert "private-test-token" not in encode(workbench.repository.load()["runs"])
    assert "文字服务请求失败" in video_window.status._context.text()


def test_review_failure_retains_usable_draft_and_truthful_ui(qapp, video_window, monkeypatch):
    workbench = enter(video_window)
    source(workbench)
    requests = sdk(monkeypatch, [sample(workbench.repository, "novel_copy"), ConnectionError("review-down")])
    workbench.source.generate_button.click()
    finish(qapp, video_window)
    assert workbench.novel.record and len(requests) == 2
    assert "自动检查未完成" in workbench.novel.status.text()
    assert "自动检查未完成" in video_window.status._state._text.text()


def test_detail_edit_keeps_selected_shot_and_updates_prompt(qapp, video_window, monkeypatch):
    workbench = enter(video_window)
    source(workbench)
    prepare_story(workbench.repository)
    workbench.refresh()
    video_window._navigate("video_storyboard")
    editor = workbench.storyboard.editor
    editor.table.selectRow(1)
    selected_id = editor.selected_shots()[0]
    editor.detail_fields["action"].setPlainText("人工修改后的第二镜动作")
    assert editor.selected_shots() == [selected_id]
    workbench.storyboard.save_button.click()
    assert editor.selected_shots() == [selected_id]
    requests = sdk(monkeypatch, ["只讨论第二镜的建议"])
    workbench.chat.input.setPlainText("这一个镜头怎么改？")
    workbench.chat.send_button.click()
    finish(qapp, video_window)
    payload = json.loads(requests[0]["messages"][0]["content"].split("\n", 1)[1])
    assert set(payload) == {"conversation"}
    assert selected_id not in requests[0]["messages"][0]["content"]
    assert "人工修改后的第二镜动作" not in requests[0]["messages"][0]["content"]
    saved_board = workbench.repository.current("storyboard")
    assert saved_board["content"]["shots"][1]["action"] == "人工修改后的第二镜动作"
    assert "只讨论第二镜的建议" in workbench.chat.messages.toPlainText()
    editor.detail_fields["action"].setPlainText("青年握紧钥匙，缓慢退离井口。")
    workbench.storyboard.save_button.click()
    edited_board = workbench.repository.current("storyboard")
    assert edited_board["content"]["shots"][1]["action"] == "青年握紧钥匙，缓慢退离井口。"
    assert edited_board["content"]["shots"][1]["narration"] == saved_board["content"]["shots"][1]["narration"]
    assert edited_board["content"]["shots"][0] == saved_board["content"]["shots"][0]
    assert len(requests) == 1


def test_name_edit_uses_existing_entity_identity(video_window):
    workbench = enter(video_window)
    source(workbench)
    prepare_story(workbench.repository)
    workbench.refresh()
    editor = workbench.storyboard.editor
    first = editor.value()["shots"][0]["characters"][0]
    editor.table.item(0, 4).setText("返乡青年")
    workbench.storyboard.save()
    shot = workbench.repository.current("storyboard")["content"]["shots"][0]
    assert shot["characters"][0] == first and shot["character_labels"][first["asset_id"]] == "返乡青年"


def test_source_edits_survive_unrelated_chat_revisions(qapp, video_window, monkeypatch):
    workbench = enter(video_window)
    source(workbench)
    requests = sdk(monkeypatch, ["这是一条与保存无关的回复。"])
    workbench.source.title.setText("编辑期间发生聊天")
    workbench.chat.input.setPlainText("给我一个开头建议")
    workbench.chat.send_button.click()
    finish(qapp, video_window)
    assert len(requests) == 1 and workbench.source.dirty
    workbench.source.save()
    assert workbench.repository.current("config")["content"]["title"] == "编辑期间发生聊天"


def test_source_edits_reject_external_changes(video_window):
    workbench = enter(video_window)
    source(workbench)
    workbench.source.title.setText("尚未保存的标题")
    ProjectService(workbench.repository).import_text("另一个窗口更新的文章。", expected_revision=workbench.repository.load()["revision"])
    with pytest.raises(WorkspaceError) as exc:
        workbench.source.save()
    assert exc.value.code == "REVISION_CONFLICT" and workbench.source.dirty


def test_first_conversation_does_not_create_business_inputs(video_window):
    workbench = enter(video_window)
    workbench.source.title.setText("先写标题，再新建对话")
    convo = ConversationService(workbench.repository).store.create()
    assert convo["project_id"] and workbench.repository.current("config") is None
    workbench.source.article.setPlainText("我在乡村捡到父亲的钥匙。")
    workbench.source.save()
    assert workbench.repository.current("config")["content"]["title"] == "先写标题，再新建对话"


def test_chat_does_not_attach_selected_view_and_local_edit_without_model(qapp, video_window, monkeypatch):
    workbench = enter(video_window)
    source(workbench)
    prepare_story(workbench.repository)
    entity = workbench.repository.current("asset_catalog")["content"]["entities"][0]
    compiled = generate(workbench.repository, "asset_spec", entity=entity, kind="character_spec")
    workbench.refresh()
    video_window._navigate("video_prompts")
    workbench.specs.views.setCurrentIndex(1)
    requests = sdk(monkeypatch, ["全身正面站立，红色外套，纯白背景，真实摄影质感。"])
    workbench.chat.input.setPlainText("仅优化选定视图")
    workbench.chat.send_button.click()
    finish(qapp, video_window)
    payload = json.loads(requests[0]["messages"][0]["content"].split("\n", 1)[1])
    assert set(payload) == {"conversation"}
    assert compiled["id"] not in requests[0]["messages"][0]["content"]
    assert workbench.repository.current(compiled["key"])["id"] == compiled["id"]
    workbench.specs.editor.positive.setPlainText("人工修改正向提示词，短发，蓝色外套。")
    workbench.specs.save()
    edited = workbench.repository.current(compiled["key"])
    assert edited["content"]["views"][1]["positive_prompt"].startswith("人工修改")
    assert len(requests) == 1 and not workbench.specs.dirty


def test_selected_shot_prompt_regeneration_keeps_review_loop(qapp, video_window, monkeypatch):
    workbench = enter(video_window)
    source(workbench)
    prepare_story(workbench.repository)
    for entity in workbench.repository.current("asset_catalog")["content"]["entities"]:
        generate(workbench.repository, "asset_spec", entity=entity, kind=ASSET_KINDS[entity["kind"]])
    bundle = WorkflowService(workbench.repository).compile_storyboard_prompts(
        expected_revision=workbench.repository.load()["revision"]
    )
    workbench.refresh()
    video_window._navigate("video_prompts")
    workbench.specs.tabs.setCurrentIndex(3)
    workbench.specs.objects.setCurrentIndex(1)
    revised = deepcopy(bundle["content"])
    revised["shots"][1]["resolved_prompt"] = "铜钥匙在井沿，冷光特写，手缓慢离开。"
    requests = sdk(monkeypatch, [revised, {"issues": []}])
    workbench.specs.generate_button.click()
    finish(qapp, video_window)
    current = workbench.repository.current("prompt_bundle")
    assert current["content"]["shots"][1]["resolved_prompt"] == revised["shots"][1]["resolved_prompt"]
    assert current["content"]["shots"][0] == bundle["content"]["shots"][0]
    assert workbench.specs.editor.positive.toPlainText() == revised["shots"][1]["resolved_prompt"]
    assert len(requests) == 2
    assert "自动审阅" in requests[1]["messages"][0]["content"]


def test_changed_camera_requires_explicit_prompt_edit(video_window):
    workbench = enter(video_window)
    source(workbench)
    prepare_story(workbench.repository)
    entity = workbench.repository.current("asset_catalog")["content"]["entities"][0]
    compiled = generate(workbench.repository, "asset_spec", entity=entity, kind="character_spec")
    value = deepcopy(compiled["content"])
    value["views"][0]["camera_view"] = "低角度正面"
    changed = workbench.repository.save(compiled["kind"], value, compiled["dependency_refs"], parent_id=compiled["id"], expected_revision=workbench.repository.load()["revision"])
    assert [(i["rule_id"], i["target_path"]) for i in local_issues(changed["kind"], changed["content"])] == [("ASSET-003", "views.0")]
    value = deepcopy(changed["content"])
    value["views"][0]["positive_prompt"] += "低角度正面拍摄。"
    edited = workbench.repository.save(changed["kind"], value, changed["dependency_refs"], parent_id=changed["id"], expected_revision=workbench.repository.load()["revision"])
    assert local_issues(edited["kind"], edited["content"]) == []


def test_ui_exposes_programming_errors_and_reports_runtime_errors(video_window, monkeypatch):
    workbench = enter(video_window)
    def programming_error():
        raise ValueError("incorrect_internal_value")
    with pytest.raises(ValueError, match="incorrect_internal_value"):
        workbench.perform(programming_error)
    def expected_error():
        raise WorkspaceError("INVALID_INPUT", "请先保存原文")
    assert workbench.perform(expected_error) is False
    assert "请先保存原文" in video_window.status._context.text()
    monkeypatch.setattr(workbench, "refresh", programming_error)
    with pytest.raises(ValueError):
        video_window.refresh_all()
    assert not video_window._refreshing


def test_prompt_view_switch_cancel_preserves_editor_and_target(video_window, monkeypatch):
    workbench = enter(video_window)
    source(workbench)
    prepare_story(workbench.repository)
    entity = workbench.repository.current("asset_catalog")["content"]["entities"][0]
    original = generate(workbench.repository, "asset_spec", entity=entity, kind="character_spec")
    workbench.refresh()
    video_window._navigate("video_prompts")
    workbench.specs.editor.positive.setPlainText("未保存的正面提示词")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Cancel)
    workbench.specs.views.setCurrentIndex(1)
    assert workbench.specs.views.currentIndex() == 0 and workbench.specs.editor.index == 0
    assert workbench.specs.editor.positive.toPlainText() == "未保存的正面提示词"
    workbench.specs.tabs.setCurrentIndex(1)
    assert workbench.specs.tabs.currentIndex() == 0 and workbench.specs.record["id"] == original["id"]
    assert workbench.specs.dirty


def test_invalid_dialogue_keeps_typing_across_failed_saves(video_window):
    workbench = enter(video_window)
    source(workbench)
    prepare_story(workbench.repository)
    workbench.refresh()
    editor = workbench.storyboard.editor
    editor.table.selectRow(0)
    editor.detail_fields["dialogue"].setPlainText("没有人物分隔符")
    for _ in range(2):
        with pytest.raises(WorkspaceError, match="对白"):
            workbench.storyboard.save()
    assert editor.content["shots"][0]["dialogue_text"] == "没有人物分隔符" and workbench.storyboard.dirty
    editor.detail_fields["dialogue"].setPlainText("返乡主人公：别回答\n旁白：手机播放父亲的录音")
    workbench.storyboard.save()
    assert workbench.repository.current("storyboard")["content"]["shots"][0]["dialogue"][0]["text"] == "别回答"
    assert workbench.repository.current("storyboard")["content"]["shots"][0]["narration"] == "手机播放父亲的录音"


def test_saved_full_text_can_switch_to_unicode_selection(video_window):
    workbench = enter(video_window)
    source(workbench)
    page = workbench.source
    assert page.ranges == [{"start": 0, "end": len(page.article.toPlainText())}]
    cursor = page.article.textCursor()
    cursor.clearSelection()
    page.article.setTextCursor(cursor)
    with pytest.raises(WorkspaceError, match="选择文字"):
        page.add_range()
    assert page.full_radio.isChecked()
    start, end = 2, 10
    cursor.setPosition(codepoint_to_qt(page.article.toPlainText(), start))
    cursor.setPosition(codepoint_to_qt(page.article.toPlainText(), end), QTextCursor.KeepAnchor)
    page.article.setTextCursor(cursor)
    page.add_range()
    assert page.selection_radio.isChecked() and page.ranges == [{"start": start, "end": end}]
    page.save()
    assert workbench.repository.current("selection")["content"]["ranges"] == [{"start": start, "end": end}]


def test_agent_loop_limit_displays_latest_then_saves_manual_edit_and_continues(qapp, video_window, monkeypatch):
    workbench = enter(video_window)
    source(workbench)
    first = sample(workbench.repository, "novel_copy")
    second, latest = deepcopy(first), deepcopy(first)
    second["opening"]["text"] = "第二版开头"
    latest["opening"]["text"] = "达到重试上限时的最新开头"
    problem = {"issues": [{"rule_id": "TXT-003", "severity": "warning", "target_path": "opening.text", "explanation": "开头有冗余", "suggestion": "压缩开头", "quote": None, "source_refs": []}]}
    requests = sdk(monkeypatch, [first, problem, second, problem, latest, problem, sample(workbench.repository, "video_copy"), {"issues": []}])
    workbench.source.generate_button.click()
    finish(qapp, video_window)
    assert len(requests) == 6
    assert workbench.novel.editor.fields[0][2].toPlainText() == latest["opening"]["text"]
    assert "检查仍未通过" in workbench.novel.status.text()
    assert "检查仍未通过" in video_window.status._state._text.text()
    assert workbench.repository.load()["reports"] == {}
    workbench.novel.editor.fields[0][2].setPlainText("人工修改最新候选后继续")
    workbench.novel.next_button.click()
    finish(qapp, video_window)
    assert len(requests) == 8
    assert "人工修改最新候选后继续" in requests[6]["messages"][0]["content"]
    assert workbench.video.record and workbench.video.editor.value() == workbench.video.record["content"]
