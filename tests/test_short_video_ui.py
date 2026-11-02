"""Actual Qt pages, task runner and SDK boundary; no network or production project writes."""
import asyncio
import os
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QFileDialog, QTreeWidgetItemIterator
import pytest

from core.gui.main_window import MainWindow
from core.generation import api_providers
from core.short_video.models import encode
from core.short_video.pipeline import ScriptWork
from core.short_video.repository import ScriptRepository


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def video_window(qapp, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    window = MainWindow()
    window.show()
    qapp.processEvents()
    yield window
    if window.runner.current is not None:
        window.runner.cancel_current()
        wait(qapp, lambda: window.runner.current is None)
    if window.video_workspace:
        for page in window.video_workspace.pages.values():
            page.dirty = False
    window.parameters_page._dirty = False
    window.close()
    window.deleteLater()
    qapp.processEvents()


def wait(qapp, predicate, timeout=8000):
    end = time.monotonic() + timeout / 1000
    while time.monotonic() < end:
        qapp.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    raise AssertionError("Qt task did not finish")


def click(qapp, window, button):
    QTest.mouseClick(button, Qt.LeftButton)
    wait(qapp, lambda: window.runner.current is None)


def fake_sdk(monkeypatch, responses, delay=0):
    requests = []
    responses = iter(responses)
    class Client:
        def __init__(self, **options):
            assert options["max_retries"] == 0
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def create(self, **kwargs):
            requests.append(kwargs)
            if delay:
                await asyncio.sleep(delay)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=encode(next(responses))))])
    monkeypatch.setattr(api_providers, "AsyncOpenAI", Client)
    monkeypatch.setattr(api_providers, "get_config", lambda _: api_providers.APIConfig("hosted-llm", "https://offline/v1", "offline", "unused"))
    return requests


def enter(window):
    window.workflow_page.video_button.click()
    assert window._mode == "video"
    return window.video_workspace


def test_lazy_home_switch_and_novel_navigation_restored(video_window, tmp_path):
    window = video_window
    assert window.video_workspace is None
    assert not (tmp_path / "current_video_work").exists()
    novel_directory = window.output_dir()
    ws = enter(window)
    assert not ws.repo.root.exists()
    assert window.status._context.text() == f"输出目录 {ws.output_dir}"
    assert set(window.rail.rows) == {"video_requirements", "video_outline", "video_script", "video_results"}
    window._navigate("video_outline")
    window.switch_mode("novel")
    assert window.stack.currentWidget() is window.workflow_page
    assert window.output_dir() == novel_directory and "lore" in window.rail.rows
    window.switch_mode("video")
    assert window.stack.currentWidget() is ws.pages["video_outline"]


def test_real_buttons_generate_confirm_review_and_finalize(qapp, video_window, monkeypatch,
                                                          video_outline, video_script, video_review):
    window = video_window
    requests = fake_sdk(monkeypatch, [video_outline, video_script, video_review])
    ws = enter(window)
    page = ws.pages["video_requirements"]
    page.fields["title"].setText("退回包裹")
    page.fields["idea"].setPlainText("一个迟到十年的包裹。")
    assert page.save()
    # Guard against any accidental invocation of the novel lore validation/recovery.
    monkeypatch.setattr(window.lore_page, "resolve_draft", lambda: pytest.fail("script task entered novel lore guard"))
    monkeypatch.setattr(window.lore_page, "set_generation_busy", lambda _: pytest.fail("script task refreshed novel lore"))
    monkeypatch.setattr(window.chapter_page, "finish_generation_progress", lambda: pytest.fail("script task refreshed novel chapters"))
    window._navigate("video_outline")
    outline_page = ws.pages["video_outline"]
    click(qapp, window, outline_page.generate_button)
    assert ws.repo.load()["outline"] and not ws.repo.outline_ready(ws.repo.load())
    outline_page.confirm_button.click()
    assert ws.repo.outline_ready(ws.repo.load())
    window._navigate("video_script")
    script_page = ws.pages["video_script"]
    click(qapp, window, script_page.generate_button)
    assert len(requests) == 3
    assert ws.repo.version()["status"] == "reviewed"
    assert script_page.editor.value() == video_script
    script_page.finalize_button.click()
    assert ws.repo.version()["finalized"]
    assert script_page.isEnabled() and window.workflow_page.video_button.isEnabled()


def test_unsaved_switch_cancel_and_discard(video_window, monkeypatch):
    ws = enter(video_window)
    page = ws.pages["video_requirements"]
    original = page.values()
    page.fields["idea"].setPlainText("未保存创意")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Cancel)
    video_window.switch_mode("novel")
    assert video_window._mode == "video" and page.dirty
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Discard)
    video_window.switch_mode("novel")
    assert video_window._mode == "novel" and not page.dirty
    assert page.values() == original


def test_running_blocks_switch_cancel_preserves_returned_script(qapp, video_window, monkeypatch,
                                                              video_requirements, video_outline, video_script):
    ws = enter(video_window)
    ws.repo.save_requirements(video_requirements)
    ws.repo.save_outline(video_outline)
    ws.repo.confirm_outline()
    requests = fake_sdk(monkeypatch, [video_script], delay=0.2)
    video_window.refresh_all()
    video_window._navigate("video_script")
    page = ws.pages["video_script"]
    page.generate_button.click()
    assert video_window.runner.busy
    wait(qapp, lambda: bool(requests))
    video_window.switch_mode("novel")
    assert video_window._mode == "video"
    assert video_window.status._stop.isVisible() and video_window.status._stop.isEnabled()
    video_window.status._stop.click()
    wait(qapp, lambda: video_window.runner.current is None)
    assert len(requests) == 1 and ws.repo.version()["content"] == video_script
    assert ws.repo.load()["runs"][-1]["status"] == "cancelled"
    assert page.isEnabled()


def test_editing_creates_manual_version_and_results_can_select(video_window, video_requirements,
                                                            video_outline, video_script):
    ws = enter(video_window)
    ws.repo.save_requirements(video_requirements)
    ws.repo.save_outline(video_outline)
    ws.repo.confirm_outline()
    initial = ws.repo.save_manual(video_script, ws.repo.source(ws.repo.load()))
    video_window.refresh_all()
    page = ws.pages["video_script"]
    content = page.editor.value()
    content["scenes"][0]["action"] = "她把旧包裹举到灯下。"
    page.editor.set_content(content)
    page.mark_dirty()
    assert page.save()
    assert len(ws.repo.load()["versions"]) == 2
    results = ws.pages["video_results"]
    results.versions.setCurrentIndex(results.versions.findData(initial["id"]))
    results.select()
    assert ws.repo.version()["id"] == initial["id"]
    assert page.editor.value() == video_script


def test_unsaved_save_navigation_and_ui_markdown_export(qapp, video_window, monkeypatch, tmp_path,
                                                     video_requirements, video_outline, video_script):
    ws = enter(video_window)
    ws.repo.save_requirements(video_requirements)
    ws.repo.save_outline(video_outline)
    ws.repo.confirm_outline()
    ws.repo.save_manual(video_script, ws.repo.source(ws.repo.load()))
    video_window.refresh_all()
    video_window._navigate("video_script")
    page = ws.pages["video_script"]
    iterator = QTreeWidgetItemIterator(page.editor.tree)
    action = None
    while iterator.value():
        item = iterator.value()
        if item.data(0, Qt.UserRole) == ["scenes", 0, "action"]:
            action = item
            break
        iterator += 1
    assert action is not None
    page.editor.tree.setCurrentItem(action)
    page.editor.text_editor.setFocus()
    QTest.keyClick(page.editor.text_editor, Qt.Key_End, Qt.ControlModifier)
    QTest.keyClicks(page.editor.text_editor, " Detail.")
    assert page.dirty and page.editor.value()["scenes"][0]["action"].endswith(" Detail.")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Save)
    video_window._navigate("video_results")
    assert not page.dirty and len(ws.repo.load()["versions"]) == 2
    saved = ws.repo.version()
    results = ws.pages["video_results"]
    results.versions.setCurrentIndex(results.versions.findData(saved["id"]))
    target = tmp_path / "exported_script.md"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(target), "Markdown (*.md)"))
    results.export_button.click()
    text = target.read_text(encoding="utf-8")
    assert " Detail." in text and "候选稿" in text and "估计" in text


def test_open_recovers_draft_and_interrupted_run_without_requests(video_window, monkeypatch, tmp_path,
                                                                video_requirements, video_outline, video_script):
    from core.short_video.llm import ScriptLLM
    root = tmp_path / "reopen_this_project"
    repo = ScriptRepository(root)
    repo.save_requirements(video_requirements)
    repo.save_outline(video_outline)
    repo.confirm_outline()
    original = repo.save_manual(video_script, repo.source(repo.load()))
    run_id = repo.start_run("script", repo.load(), "hosted-llm")
    repo.record_call(run_id, "prompt", "未完成草稿")
    monkeypatch.setattr(ScriptLLM, "__call__", lambda *_: pytest.fail("restart must not call model"))
    ws = enter(video_window)
    page = ws.pages["video_requirements"]
    page.directory.setText(str(root))
    page.open_button.click()
    assert ws.repo.version()["id"] == original["id"]
    assert ws.repo.load()["runs"][0]["status"] == "interrupted"
    assert video_window.status._context.text() == f"输出目录 {root}"
    video_window._navigate("video_results")
    assert "未完成草稿" in ws.pages["video_results"].raw.toPlainText()
    assert ws.pages["video_script"].editor.value() == video_script


def test_corrupt_project_open_keeps_current_workspace(video_window, tmp_path):
    ws = enter(video_window)
    previous = ws.output_dir
    bad = ScriptRepository(tmp_path / "broken")
    bad.root.mkdir(parents=True)
    state = bad.load()
    state["outline"] = {"content": None}
    bad.path.write_text(encode(state), encoding="utf-8")
    original = bad.path.read_bytes()
    page = ws.pages["video_requirements"]
    page.directory.setText(str(bad.root.parent))
    page.open_button.click()
    assert ws.output_dir == previous and page.directory.text() == previous
    assert bad.path.read_bytes() == original


def test_external_requirements_save_does_not_overwrite_local_edits(video_window, video_requirements):
    ws = enter(video_window)
    page = ws.pages["video_requirements"]
    page.fields["title"].setText("当前窗口的标题")
    page.fields["idea"].setPlainText("当前窗口的创意")
    ws.repo.save_requirements(video_requirements)
    assert not page.save()
    assert page.dirty and page.fields["title"].text() == "当前窗口的标题"
    assert ws.repo.load()["requirements"] == video_requirements


def test_reference_names_follow_character_edits_and_list_editing(qapp, video_script):
    from core.gui.pages.short_video.editor import StoryEditor
    editor = StoryEditor("script")
    editor.set_content(video_script)
    def find(path):
        iterator = QTreeWidgetItemIterator(editor.tree)
        while iterator.value():
            item = iterator.value()
            if item.data(0, Qt.UserRole) == path:
                return item
            iterator += 1
        raise AssertionError(f"Missing field {path}")
    name = find(["characters", 0, "name"])
    name.setText(1, "林小青")
    speaker = editor.tree.itemWidget(find(["scenes", 2, "dialogue", 0, "character_id"]), 1)
    assert speaker.currentText() == "林小青" and speaker.currentData() == "p1"
    editor.tree.setCurrentItem(find(["scenes"]))
    editor.add_button.click()
    assert len(editor.value()["scenes"]) == 4
    editor.tree.setCurrentItem(find(["scenes", 3]))
    editor.remove_button.click()
    assert len(editor.value()["scenes"]) == 3
    editor.deleteLater()
    qapp.processEvents()
