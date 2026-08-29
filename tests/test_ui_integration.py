# -*- coding: utf-8 -*-
"""PySide6 UI integration tests.

These tests exercise the real window, widgets, signals, thread pool, status bar,
and artifact readers.  Only the external generation boundary is replaced, so
the suite never calls an LLM or mutates the repository's current_work project.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="PySide6 is required for UI integration tests")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core.gui import theme  # noqa: E402
from core.gui.main_window import MainWindow  # noqa: E402
from core.gui.pages import chapter_writing_page as chapter_module  # noqa: E402
from core.gui.pages import lore_page as lore_module  # noqa: E402
from core.gui.pages import scene_plan_page as scene_module  # noqa: E402
from core.gui.pages import structure_page as structure_module  # noqa: E402
from core.gui.pages import workflow_page as workflow_module  # noqa: E402
from core.gui import params_store  # noqa: E402
from core.gui.widgets.step_rail import StepState  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.build_qss())
    yield app


@pytest.fixture
def window(qapp, tmp_path):
    instance = MainWindow()
    instance.parameters_page.output_dir_edit.setText(str(tmp_path))
    instance.parameters_page.output_dir_edit.editingFinished.emit()
    instance.show()
    qapp.processEvents()
    yield instance
    if instance.runner.busy:
        instance.runner.cancel_current()
        _wait_until(qapp, lambda: not instance.runner.busy)
    instance.close()
    instance.deleteLater()
    qapp.processEvents()


def _wait_until(qapp, predicate, timeout_ms: int = 4000) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    raise AssertionError("timed out waiting for UI state")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _status_text(window: MainWindow) -> str:
    return window.status._state._text.text()  # noqa: SLF001 - UI assertion


def _status_context(window: MainWindow) -> str:
    return window.status._context.text()  # noqa: SLF001 - UI assertion


def _instant_work(value=None):
    def work(report):
        report("集成测试动作", 0.5)
        return value

    return work


def _click_and_wait(qapp, window, widget) -> None:
    QTest.mouseClick(widget, Qt.LeftButton)
    _wait_until(
        qapp,
        lambda: not window.runner.busy and not window.status._stop.isVisible(),  # noqa: SLF001
    )


def test_sidebar_navigation_and_parameter_save(qapp, window, tmp_path):
    page = window.parameters_page
    page.title_edit.setText("UI Integration Novel")

    QTest.mouseClick(page.save_button, Qt.LeftButton)
    qapp.processEvents()

    parameter_file = tmp_path / "system" / "parameters.txt"
    assert parameter_file.is_file()
    assert "Novel Title: UI Integration Novel" in parameter_file.read_text(encoding="utf-8")
    assert window.rail.rows["parameters"]._state.state == "complete"  # noqa: SLF001
    assert window.rail._work_title.text() == "UI Integration Novel"  # noqa: SLF001
    assert _status_text(window) == "参数已保存"

    QTest.mouseClick(window.rail.rows["lore"], Qt.LeftButton)
    qapp.processEvents()
    assert window.stack.currentWidget() is window.lore_page


def test_blocked_sidebar_still_opens_page_and_explains_reason(qapp, window):
    row = window.rail.rows["structure"]
    row.set_state(StepState("blocked", "需先完成世界设定", False))
    assert not row._state.clickable  # noqa: SLF001 - integration assertion

    QTest.mouseClick(row, Qt.LeftButton)
    qapp.processEvents()

    assert window.stack.currentWidget() is window.structure_page
    assert _status_text(window) == "故事结构被阻塞"
    assert _status_context(window)


def test_parameter_validation_and_dependent_controls(qapp, window):
    page = window.parameters_page
    page.title_edit.clear()

    QTest.mouseClick(page.save_button, Qt.LeftButton)
    qapp.processEvents()

    assert _status_text(window) == "保存失败"
    assert page.title_edit.toolTip() == "作品标题不能为空"

    page.title_edit.setText("联动参数测试")
    if page.length_combo.count() > 1:
        page.length_combo.setCurrentIndex(1)
    assert page.structure_combo.count() > 0
    assert page.structure_combo.isEnabled()

    old_hint = page.quality_hint.text()
    if page.quality_combo.count() > 1:
        page.quality_combo.setCurrentIndex(
            (page.quality_combo.currentIndex() + 1) % page.quality_combo.count()
        )
    assert page.quality_hint.text()
    assert page.quality_hint.text() != old_hint


def test_parameter_io_error_is_shown_verbatim(qapp, window, monkeypatch):
    page = window.parameters_page
    page.title_edit.setText("参数错误展示测试")

    def fail_save(*_args, **_kwargs):
        raise PermissionError("参数目录没有写入权限")

    monkeypatch.setattr(params_store, "save", fail_save)
    QTest.mouseClick(page.save_button, Qt.LeftButton)
    qapp.processEvents()

    assert _status_text(window) == "保存失败"
    assert _status_context(window) == "参数目录没有写入权限"


def test_workflow_cards_resume_and_analysis(qapp, window, monkeypatch):
    calls = []

    def fake_step_work(step, output_dir, model=""):
        calls.append(("single", step, output_dir))
        return _instant_work({"step": step})

    def fake_full_work(output_dir, model="", steps=None):
        calls.append(("full", tuple(steps or ()), output_dir))
        return _instant_work({"steps": steps})

    monkeypatch.setattr(workflow_module, "step_work", fake_step_work)
    monkeypatch.setattr(workflow_module, "full_workflow_work", fake_full_work)
    monkeypatch.setattr(
        workflow_module.QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.Yes,
    )
    window._navigate("workflow")

    _click_and_wait(qapp, window, window.workflow_page.run_all_button)
    assert calls[-1][0:2] == ("full", ())

    QTest.mouseClick(window.workflow_page.cards["lore"]._open, Qt.LeftButton)  # noqa: SLF001
    assert window.stack.currentWidget() is window.lore_page
    window._navigate("workflow")

    _click_and_wait(qapp, window, window.workflow_page.cards["lore"]._run)  # noqa: SLF001
    assert calls[-1][0:2] == ("single", "lore")

    window.workflow_page.set_states({
        "lore": StepState("complete", "完成", True),
        "structure": StepState("partial", "部分", True),
        "scenes": StepState("idle", "未开始", True),
        "chapters": StepState("blocked", "被阻塞", False),
    })
    _click_and_wait(qapp, window, window.workflow_page.resume_button)
    assert calls[-1][0:2] == ("full", ("structure", "scenes"))

    QTest.mouseClick(window.workflow_page.analyze_button, Qt.LeftButton)
    assert "正文累计" in _status_context(window)

    QTest.mouseClick(window.workflow_page.reset_button, Qt.LeftButton)
    qapp.processEvents()
    assert _status_text(window) == "工作流状态已重置"


def test_missing_output_folder_actions_report_warning(qapp, window):
    window._navigate("lore")
    QTest.mouseClick(window.lore_page.open_folder_button, Qt.LeftButton)
    assert _status_text(window) == "提示"
    assert "目录还不存在" in _status_context(window)

    window._navigate("chapters")
    QTest.mouseClick(window.chapter_page.open_folder_button, Qt.LeftButton)
    assert _status_text(window) == "提示"
    assert "目录还不存在" in _status_context(window)


def test_lore_action_buttons_and_generation_counts(
    qapp, window, tmp_path, monkeypatch,
):
    calls = []

    def fake_action_work(
        step, action, output_dir, model="", *, parameters=None, chapter_number=None,
    ):
        calls.append((step, action, output_dir, parameters, chapter_number))
        return _instant_work({"step": step, "action": action})

    monkeypatch.setattr(lore_module, "action_work", fake_action_work)
    window._navigate("lore")
    page = window.lore_page

    assert not page.titles_button.isEnabled()
    assert not page.enhance_button.isEnabled()
    assert page.step_buttons["enhance"].state == "locked"

    _write(tmp_path / "story/lore/generated_lore.md", "# 世界观\n可推荐标题。")
    _write(
        tmp_path / "story/lore/characters.json",
        json.dumps([{"name": "测试主角", "role": "Protagonist"}], ensure_ascii=False),
    )
    page.refresh()
    assert page.titles_button.isEnabled()
    assert page.enhance_button.isEnabled()

    _click_and_wait(qapp, window, page.titles_button)
    _click_and_wait(qapp, window, page.enhance_button)

    page.faction_stepper.set_value(3)
    page.character_stepper.set_value(4)
    _click_and_wait(qapp, window, page.step_buttons["factions"])

    assert [call[1] for call in calls] == ["titles", "enhance", "factions"]
    assert calls[-1][3] == {"num_factions": 3, "num_characters": 4}


def test_structure_action_buttons_dispatch_individual_actions(
    qapp, window, monkeypatch,
):
    calls = []

    def fake_action_work(step, action, output_dir, model="", **kwargs):
        calls.append((step, action))
        return _instant_work({"step": step, "action": action})

    monkeypatch.setattr(structure_module, "action_work", fake_action_work)
    window._navigate("structure")
    page = window.structure_page

    _click_and_wait(qapp, window, page.arcs_button)
    _click_and_wait(qapp, window, page.faction_arcs_button)
    _click_and_wait(qapp, window, page.step_buttons["locations"])
    _click_and_wait(qapp, window, page.step_buttons["plot"])

    assert calls == [
        ("structure", "arcs"),
        ("structure", "faction_arcs"),
        ("structure", "locations"),
        ("structure", "plot"),
    ]


def test_scene_gate_force_action_and_plan_selection(
    qapp, window, tmp_path, monkeypatch,
):
    full_calls = []
    action_calls = []

    def fake_step_work(step, output_dir, model=""):
        full_calls.append(step)
        return _instant_work({"step": step})

    def fake_action_work(step, action, output_dir, model="", **kwargs):
        action_calls.append((step, action))
        return _instant_work({"step": step, "action": action})

    monkeypatch.setattr(scene_module, "step_work", fake_step_work)
    monkeypatch.setattr(scene_module, "action_work", fake_action_work)
    window._navigate("scenes")
    page = window.scene_page

    assert page.stack.currentIndex() == 0
    assert not page.primary.isEnabled()
    assert not page.outline_button.isEnabled()
    QTest.mouseClick(page.goto_structure_button, Qt.LeftButton)
    assert window.stack.currentWidget() is window.structure_page

    window._navigate("scenes")
    _click_and_wait(qapp, window, page.force_button)
    assert full_calls == ["scenes"]
    assert not page.primary.isEnabled()

    _write(tmp_path / "story/lore/generated_lore.md", "世界观")
    _write(tmp_path / "story/structure/act_1.md", "第一幕")
    _write(
        tmp_path / "story/planning/detailed_scene_plans/scenes_6-act_structure_beginning_ch1.md",
        "第一章场景内容",
    )
    _write(
        tmp_path / "story/planning/detailed_scene_plans/scenes_6-act_structure_beginning_ch2.md",
        "### 第二章场景\n\n**第二章场景内容**",
    )
    page.refresh()
    assert page.outline_button.isEnabled()
    assert page.plan_list.count() == 2
    assert page.plan_view.content_font_size == 15
    page.plan_list.setCurrentRow(1)
    assert "第二章场景内容" in page.plan_view.toPlainText()
    assert "###" not in page.plan_view.toPlainText()
    assert "**" not in page.plan_view.toPlainText()

    _click_and_wait(qapp, window, page.outline_button)
    _click_and_wait(qapp, window, page.step_buttons["scenes"])
    assert action_calls == [("scenes", "outline"), ("scenes", "scenes")]


def test_chapter_controls_analysis_rewrite_and_html_escaping(
    qapp, window, tmp_path, monkeypatch,
):
    calls = []

    def fake_action_work(
        step, action, output_dir, model="", *, parameters=None, chapter_number=None,
    ):
        calls.append((step, action, chapter_number))
        return _instant_work({"step": step, "action": action})

    monkeypatch.setattr(chapter_module, "action_work", fake_action_work)
    monkeypatch.setattr(
        chapter_module.QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.Yes,
    )
    _write(
        tmp_path / "story/planning/detailed_scene_plans/chapter_1.md",
        "第一章规划",
    )
    _write(
        tmp_path / "story/planning/detailed_scene_plans/chapter_2.md",
        "第二章规划",
    )
    _write(
        tmp_path / "story/content/chapters/chapter_1.md",
        "# 第一章\n门上写着 <禁入> & 请回头。",
    )
    window._navigate("chapters")
    page = window.chapter_page

    assert "<禁入> & 请回头" in page.prose.toPlainText()
    QTest.mouseClick(page.analyze_button, Qt.LeftButton)
    assert "最短为第 1 章" in _status_context(window)

    _click_and_wait(qapp, window, page.primary)
    _click_and_wait(qapp, window, page.step_buttons["all"])
    _click_and_wait(qapp, window, page.rewrite_button)

    assert calls == [
        ("chapters", "next", None),
        ("chapters", "all", None),
        ("chapters", "rewrite", 1),
    ]


def test_failed_background_task_restores_ui_and_shows_error(
    qapp, window, monkeypatch,
):
    def fake_step_work(step, output_dir, model=""):
        def work(report):
            report("准备失败", 0.2)
            raise RuntimeError("集成测试故障")

        return work

    monkeypatch.setattr(lore_module, "step_work", fake_step_work)
    window._navigate("lore")
    QTest.mouseClick(window.lore_page.generate_button, Qt.LeftButton)
    _wait_until(
        qapp,
        lambda: (
            not window.runner.busy
            and _status_text(window) == "任务失败"
            and window.lore_page.generate_button.isEnabled()
            and not window.status._stop.isVisible()  # noqa: SLF001
        ),
    )

    assert _status_context(window) == "集成测试故障"
    assert window.lore_page.generate_button.isEnabled()
    assert window.lore_page.generate_button.text() == "生成世界设定"


def test_artifacts_unlock_pages_and_render_chapter(qapp, window, tmp_path):
    window._navigate("scenes")
    assert window.scene_page.stack.currentIndex() == 0
    assert not window.scene_page.primary.isEnabled()

    _write(tmp_path / "story/lore/generated_lore.md", "# 世界观\n集成测试世界")
    _write(tmp_path / "story/structure/act_1.md", "# 第一幕\n冲突建立")
    window.scene_page.refresh()
    qapp.processEvents()

    assert window.scene_page.stack.currentIndex() == 1
    assert window.scene_page.primary.isEnabled()

    _write(
        tmp_path / "story/planning/detailed_scene_plans/chapter_1.md",
        "# 第 1 章场景\n主角进入测试场景。",
    )
    _write(
        tmp_path / "story/content/chapters/chapter_1.md",
        "# 第一章 集成\n主角推开门，界面正确显示了这一段正文。",
    )
    window._navigate("chapters")
    qapp.processEvents()

    assert window.chapter_page.chapter_list.count() == 1
    assert "第 1 章" in window.chapter_page.chapter_title.text()
    assert "界面正确显示" in window.chapter_page.prose.toPlainText()
    assert window.chapter_page.primary.isEnabled()


def test_generation_request_updates_artifacts_and_status(
    qapp, window, tmp_path, monkeypatch,
):
    def fake_step_work(step: str, output_dir: str, model: str = ""):
        assert step == "lore"
        assert output_dir == str(tmp_path)

        def work(report):
            report("正在生成测试世界观", 0.5)
            lore_dir = Path(output_dir) / "story/lore"
            _write(lore_dir / "generated_lore.md", "# 世界观\n后台任务生成成功。")
            _write(lore_dir / "lore_contract.json", json.dumps({"names": []}))
            return {"step": step}

        return work

    monkeypatch.setattr(lore_module, "step_work", fake_step_work)
    window._navigate("lore")
    QTest.mouseClick(window.lore_page.generate_button, Qt.LeftButton)

    _wait_until(
        qapp,
        lambda: (
            not window.runner.busy
            and _status_text(window) == "任务已完成"
            and window.lore_page.generate_button.isEnabled()
            and not window.status._stop.isVisible()  # noqa: SLF001
        ),
    )

    assert window.rail.rows["lore"]._state.state == "complete"  # noqa: SLF001
    assert window.lore_page.generate_button.isEnabled()
    assert window.lore_page.generate_button.text() == "重新生成世界设定"
    assert (tmp_path / "story/lore/generated_lore.md").is_file()


def test_stop_button_cancels_running_ui_task(qapp, window, tmp_path, monkeypatch):
    def fake_step_work(step: str, output_dir: str, model: str = ""):
        def work(report):
            for index in range(200):
                time.sleep(0.005)
                report("等待取消", index / 200)
            return {"step": step}

        return work

    monkeypatch.setattr(lore_module, "step_work", fake_step_work)
    window._navigate("lore")
    QTest.mouseClick(window.lore_page.generate_button, Qt.LeftButton)
    _wait_until(qapp, lambda: window.runner.busy and window.status._stop.isVisible())  # noqa: SLF001

    QTest.mouseClick(window.status._stop, Qt.LeftButton)  # noqa: SLF001
    _wait_until(
        qapp,
        lambda: (
            not window.runner.busy
            and _status_text(window) == "任务已取消"
            and window.lore_page.generate_button.isEnabled()
            and not window.status._stop.isVisible()  # noqa: SLF001
        ),
    )

    assert window.lore_page.generate_button.isEnabled()
    assert window.status._stop.text() == "停止"  # noqa: SLF001
    assert not window.status._stop.isVisible()  # noqa: SLF001
