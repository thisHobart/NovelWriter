"""Real Qt v2 workbench operations; offline transport, temporary projects only."""
import os
import time
from copy import deepcopy
import json

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt, QSettings
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QFileDialog, QDialog, QComboBox, QCheckBox, QInputDialog, QMenu, QPushButton
import pytest

from core.gui.main_window import MainWindow
from core.short_video.v2.models import ASSET_KINDS, encode
from test_video_v2_workflow import ARTICLE, sample


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def wait(qapp, predicate, timeout=60000):
    end = time.monotonic() + timeout / 1000
    while time.monotonic() < end:
        qapp.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    raise AssertionError("Qt task did not finish")


@pytest.fixture
def video_window(qapp, monkeypatch, tmp_path):
    import core.gui.pages.short_video.workbench as module
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module, "QSettings", lambda *args: QSettings(str(tmp_path / "ui.ini"), QSettings.IniFormat))
    window = MainWindow()
    window.show()
    qapp.processEvents()
    yield window
    if window.runner.current is not None:
        window.runner.cancel_current()
        wait(qapp, lambda: window.runner.current is None)
    if window.video_workspace:
        window.video_workspace.source.dirty = False
        for page in window.video_workspace.artifact_pages():
            page.dirty = False
    window.parameters_page._dirty = False
    window.close()
    window.deleteLater()
    qapp.processEvents()


def enter(window):
    window.workflow_page.video_button.click()
    assert window._mode == "video"
    return window.video_workspace


def source(workbench):
    workbench.source.title.setText("乡村井边")
    workbench.source.article.setPlainText(ARTICLE)
    workbench.source.save_button.click()
    assert workbench.repository.current("source")["content"]["normalized_text"] == ARTICLE


def transport(workbench, requests):
    def send(prompt):
        requests.append(prompt)
        if prompt.startswith("自动审阅"):
            return encode({"issues": []})
        for stage in ("novel_copy", "video_copy", "asset_catalog", "storyboard", "asset_spec"):
            if "阶段：" + stage in prompt:
                params = {}
                if stage == "asset_spec":
                    context = json.loads(prompt.split("冻结的输入资料：\n", 1)[1].split("\n输出结构样例：", 1)[0])
                    params = {"entity": context["entity"], "kind": ASSET_KINDS[context["entity"]["kind"]]}
                return encode(sample(workbench.repository, stage, **params))
        return "井底响了三声，我握住父亲的铜钥匙。"
    return send


def generate(qapp, window, page):
    page.generate_button.click()
    wait(qapp, lambda: window.runner.current is None)
    assert page.record is not None, window.status._context.text()


def test_four_workspaces_collapsible_chat_and_novel_restore(video_window):
    window = video_window
    assert window.video_workspace is None
    workbench = enter(window)
    assert not workbench.repository.path.exists()
    assert list(workbench.pages) == ["video_source", "video_adaptation", "video_storyboard", "video_prompts"]
    assert window.rail.isHidden()
    assert not workbench.source.requirements.isVisible()
    workbench.toggle_chat()
    assert workbench.chat.isHidden()
    workbench.toggle_chat()
    source(workbench)
    window._navigate("video_adaptation")
    assert workbench.chat.isVisible()
    window.switch_mode("novel")
    assert window.stack.currentWidget() is window.workflow_page and not window.rail.isHidden()
    window.switch_mode("video")
    assert workbench.right.currentWidget() is workbench.adaptation


def test_next_buttons_complete_main_path_without_confirm(qapp, video_window, monkeypatch, tmp_path):
    window = video_window
    workbench = enter(window)
    workbench.source.article.setPlainText(ARTICLE)
    requests = []
    workbench.transport = transport(workbench, requests)
    workbench.source.generate_button.click()
    wait(qapp, lambda: window.runner.current is None)
    assert workbench.novel.record is not None, window.status._context.text()
    workbench.novel.editor.fields[0][2].setPlainText("手改开头：铜钥匙就在井边。")
    workbench.novel.next_button.click()
    wait(qapp, lambda: window.runner.current is None)
    assert workbench.video.record is not None, window.status._context.text()
    video_request = next(p for p in requests if "阶段：video_copy" in p)
    assert "手改开头：铜钥匙就在井边。" in video_request
    workbench.video.next_button.click()
    wait(qapp, lambda: window.runner.current is None)
    assert workbench.storyboard.record is not None, window.status._context.text()
    assert workbench.storyboard.editor.table.columnCount() == 9
    workbench.storyboard.next_button.click()
    wait(qapp, lambda: window.runner.current is None)
    assert workbench.repository.current("prompt_bundle"), window.status._context.text()
    assert workbench.specs.record is not None
    assert len(requests) == 14  # 7 generation stages, each with review
    assert workbench.repository.load()["confirmations"] == {}
    assert len(workbench.specs.record["content"]["views"]) == 4
    output = tmp_path / "成品.xlsx"
    def choose_export(dialog):
        for check in dialog.findChildren(QCheckBox):
            check.setChecked(check.text() == "分镜")
        dialog.findChild(QComboBox).setCurrentIndex(1)
        return QDialog.Accepted
    monkeypatch.setattr(QDialog, "exec", choose_export)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a: (str(output), "Excel (*.xlsx)"))
    workbench.export_button.click()
    from openpyxl import load_workbook
    book = load_workbook(output)
    assert book["分镜脚本"].max_column == 9 and book["分镜脚本"].max_row == 3
    assert not (workbench.repository.root / "media").exists()


def test_chat_has_no_document_context_or_implicit_save(qapp, video_window):
    workbench = enter(video_window)
    source(workbench)
    requests = []
    workbench.transport = transport(workbench, requests)
    video_window._navigate("video_adaptation")
    generate(qapp, video_window, workbench.novel)
    current = workbench.novel.record["id"]
    workbench.chat.input.setPlainText("帮我改进开头")
    workbench.chat.send_button.click()
    wait(qapp, lambda: video_window.runner.current is None)
    assert ARTICLE not in requests[-1] and current not in requests[-1]
    assert workbench.repository.current("novel_copy")["id"] == current
    assert workbench.chat.messages.toPlainText().strip()
    assert workbench.repository.current("novel_copy")["id"] == current
    cid = workbench.chat.conversation_id
    messages_before_switch = workbench.chat.service().store.get(cid)["messages"]
    assert all("context_snapshot" not in message for message in messages_before_switch)
    video_window._navigate("video_source")
    assert workbench.chat.service().store.get(cid)["messages"] == messages_before_switch
    workbench.source.article.setPlainText("尚未保存且不应发送的原文")
    workbench.chat.input.setPlainText("一般讨论")
    workbench.chat.send_button.click()
    wait(qapp, lambda: video_window.runner.current is None)
    payload = json.loads(requests[-1].split("\n", 1)[1])
    assert set(payload) == {"conversation"}
    assert payload["conversation"][0]["content"] == "帮我改进开头"
    assert "尚未保存且不应发送的原文" not in requests[-1]
    assert workbench.source.dirty
    assert workbench.repository.current("source")["content"]["normalized_text"] == ARTICLE


def test_content_menus_offer_history_and_templates_without_duplicate_editing(video_window):
    workbench = enter(video_window)
    for page in (workbench.novel, workbench.video, workbench.storyboard):
        menus = page.findChildren(QMenu)
        assert len(menus) == 1
        assert [action.text() for action in menus[0].actions()] == ["历史版本", "编辑阶段提示词"]
    assert [action.text() for action in workbench.chat.history_menu.actions()] == [
        "新对话", "选择已有对话", "重试上次问题"
    ]
    assert [button.text() for button in workbench.chat.findChildren(QPushButton)] == [
        "对话", "发送", "停止对话"
    ]


def test_chat_send_button_uses_new_capacity_builder_and_store(qapp, video_window):
    from core.short_video.v2.models import identity

    workbench = enter(video_window)
    requests = []
    workbench.transport = transport(workbench, requests)
    workbench.chat.create()
    conversation_id = workbench.chat.conversation_id
    store = workbench.chat.service().store
    for question, answer in [("最早的问题" * 5000, "最早的回答" * 5000), ("最近的问题", "最近的回答")]:
        user = store.save_user_message(
            conversation_id, question, turn_id=identity("run"),
            model_snapshot={"provider": "hosted-llm", "model": "workspace-test-model"},
        )
        store.save_final_reply(conversation_id, user["id"], answer)
    workbench.chat.input.setPlainText("本轮唯一问题")
    workbench.chat.send_button.click()
    wait(qapp, lambda: video_window.runner.current is None)
    assert len(requests) == 1
    payload = json.loads(requests[0].split("\n", 1)[1])
    assert set(payload) == {"conversation"}
    assert [message["content"] for message in payload["conversation"]] == ["最近的问题", "最近的回答", "本轮唯一问题"]
    messages = store.get(conversation_id)["messages"]
    assert len(messages) == 6 and messages[-2]["content"] == "本轮唯一问题"
    assert all("context_snapshot" not in message for message in messages)


def test_unsaved_switch_conflict_and_failed_open_preserve_inputs(video_window, monkeypatch, tmp_path):
    workbench = enter(video_window)
    source(workbench)
    workbench.source.title.setText("未保存标题")
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.Cancel)
    video_window.switch_mode("novel")
    assert video_window._mode == "video" and workbench.source.dirty
    from core.short_video.v2.project_service import ProjectService
    ProjectService(workbench.repository).import_text(ARTICLE + "新段落", expected_revision=workbench.repository.load()["revision"])
    assert not workbench.perform(workbench.source.save) and workbench.source.title.text() == "未保存标题"
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.Discard)
    workbench.resolve_edits()
    previous = workbench.output_dir
    broken = tmp_path / "broken" / "short_video"
    broken.mkdir(parents=True)
    (broken / "project_v2.json").write_text("broken", encoding="utf-8")
    workbench.directory.setText(str(broken.parent))
    assert not workbench.perform(workbench.open_project)
    assert workbench.output_dir == previous and workbench.directory.text() == previous


def test_chat_stop_keeps_returned_reply(qapp, video_window):
    import threading
    workbench = enter(video_window)
    source(workbench)
    entered, release = threading.Event(), threading.Event()
    def send(prompt):
        entered.set()
        release.wait(5)
        return "停止前已返回的回复"
    workbench.transport = send
    workbench.chat.input.setPlainText("询问原文")
    workbench.chat.send_button.click()
    wait(qapp, entered.is_set)
    assert workbench.chat.stop_button.isEnabled()
    workbench.chat.stop_button.click()
    release.set()
    wait(qapp, lambda: video_window.runner.current is None)
    messages = workbench.chat.service().store.get(workbench.chat.conversation_id)["messages"]
    assert messages[-1]["content"] == "停止前已返回的回复" and messages[-1]["status"] == "cancelled"
    assert "停止前已返回的回复" in workbench.chat.messages.toPlainText()
    assert not workbench.chat.stop_button.isEnabled()


def test_storyboard_local_edit_split_drag_delete_and_timeline(qapp, video_window):
    from test_video_v2_workflow import prepare_story
    workbench = enter(video_window)
    source(workbench)
    prepare_story(workbench.repository)
    workbench.refresh()
    video_window._navigate("video_storyboard")
    editor = workbench.storyboard.editor
    editor.table.selectRow(0)
    original_id = editor.selected_shots()[0]
    editor.detail_fields["action"].setPlainText("手指离开铜钥匙")
    assert "手指离开铜钥匙" in editor.value()["shots"][0]["visual_prompt_draft"]
    editor.split()
    editor.move_shot(1, 2)
    editor.table.selectRow(2)
    editor.duration_input.setValue(2.5)  # advisory only, no reason required
    workbench.storyboard.save_button.click()
    shots = workbench.repository.current("storyboard")["content"]["shots"]
    assert shots[0]["shot_id"] == original_id and shots[0]["start_ms"] == 0
    assert shots[1]["start_ms"] == shots[0]["end_ms"] and shots[-1]["duration_ms"] == 2500
    editor.add_shot()
    assert len(editor.value()["shots"]) == 4
    editor.delete_shot()
    assert len(editor.value()["shots"]) == 3


def test_projects_do_not_mix_chat(video_window, tmp_path):
    workbench = enter(video_window)
    source(workbench)
    workbench.chat.create()
    first_root, cid = workbench.output_dir, workbench.chat.conversation_id
    workbench.directory.setText(str(tmp_path / "another_project"))
    workbench.open_project()
    assert workbench.chat.conversation_id is None
    source(workbench)
    workbench.chat.create()
    assert workbench.chat.conversation_id != cid
    workbench.directory.setText(first_root)
    workbench.open_project()
    assert workbench.chat.conversation_id == cid


def test_save_failure_prevents_next_paid_request(qapp, video_window, monkeypatch):
    workbench = enter(video_window)
    source(workbench)
    requests = []
    workbench.transport = transport(workbench, requests)
    generate(qapp, video_window, workbench.novel)
    before = len(requests)
    workbench.novel.editor.fields[0][2].setPlainText("磁盘失败时保留编辑")
    def fail(*args, **kwargs):
        raise PermissionError("disk occupied")
    monkeypatch.setattr(workbench.repository, "save", fail)
    workbench.novel.next_button.click()
    assert workbench.novel.dirty and len(requests) == before
    assert workbench.novel.editor.value()["opening"]["text"] == "磁盘失败时保留编辑"


def test_chat_works_before_saving_source(qapp, video_window):
    workbench = enter(video_window)
    requests = []
    workbench.transport = lambda prompt: requests.append(prompt) or "你好，有什么可以帮助你？"
    workbench.source.article.setPlainText("未保存的草稿")
    workbench.chat.input.setPlainText("你好")
    workbench.chat.send_button.click()
    wait(qapp, lambda: video_window.runner.current is None)
    assert len(requests) == 1
    assert "未保存的草稿" not in requests[0]
    assert workbench.repository.current("source") is None and workbench.source.dirty
    assert "你好，有什么可以帮助你？" in workbench.chat.messages.toPlainText()
