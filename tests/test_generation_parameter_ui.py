"""所有 GUI 入口的参数选择及数量联动使用真实窗口，模型边界离线替换。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from copy import deepcopy
import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from core.gui.main_window import MainWindow
from core.gui.services import step_runner
from core.generation.stage_pipeline import load_stage_parameters
from core.generation.parameters import creation_parameters
from core.gui import params_store


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, tmp_path):
    w = MainWindow()
    w.parameters_page.output_dir_edit.setText(str(tmp_path))
    w.parameters_page.title_edit.setText("参数传递测试")
    w.parameters_page.theme_edit.setText("旧主题")
    assert w.parameters_page.save()
    w.refresh_all()
    yield w
    w.close()
    w.deleteLater()


def choose(monkeypatch, choice, seen=None):
    def fake_exec(box):
        if seen is not None:
            seen.append([(b.text(), b.isEnabled()) for b in box.buttons()])
        box.selected_for_test = next(b for b in box.buttons() if b.text() == choice)
    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda box: box.selected_for_test)


def capture_runs(window, monkeypatch):
    seen = []
    def fake_stage(step, output_dir, model="", parameters=None, report=None):
        seen.append((step, deepcopy(parameters)))
        return {}
    monkeypatch.setattr(step_runner, "run_stage", fake_stage)
    monkeypatch.setattr(step_runner, "run_stage_action", lambda step, action, output_dir, model="", **kw:
                        fake_stage(step, output_dir, model, parameters=kw.get("parameters")))
    monkeypatch.setattr(window.runner, "run", lambda work, **kw: work(lambda *args: None))
    return seen


@pytest.mark.parametrize("choice,expected,count", [("保存并生成", "新主题", 1), ("使用已保存参数", "旧主题", 1), ("取消", None, 0)])
def test_unsaved_choice_uses_exact_selected_version(window, monkeypatch, choice, expected, count):
    window.parameters_page.theme_edit.setText("新主题")
    seen = capture_runs(window, monkeypatch)
    choose(monkeypatch, choice)
    window.lore_page._run_step("full")
    assert len(seen) == count
    if seen:
        assert seen[0][1]["theme"] == seen[0][1]["Theme"] == expected
    assert window.parameters_page.theme_edit.text() == "新主题"
    assert load_stage_parameters(window.output_dir())["theme"] == ("新主题" if choice == "保存并生成" else "旧主题")
    assert window.parameters_page.dirty is (choice != "保存并生成")


def test_saved_unchanged_parameters_do_not_prompt(window, monkeypatch):
    assert creation_parameters(load_stage_parameters(window.output_dir())) == creation_parameters(window.parameters_page.parameters())
    monkeypatch.setattr(QMessageBox, "exec", lambda *args: pytest.fail("已保存且未改变的参数不能重复询问"))
    seen = capture_runs(window, monkeypatch)
    window.workflow_page._run_single("lore")
    assert seen[0][1]["theme"] == "旧主题"


def test_failed_save_never_starts_task(window, monkeypatch):
    window.parameters_page.theme_edit.setText("新主题")
    seen = capture_runs(window, monkeypatch)
    choose(monkeypatch, "保存并生成")
    monkeypatch.setattr(params_store, "save", lambda *args: (_ for _ in ()).throw(OSError("磁盘写入失败")))
    window.workflow_page._run_all()
    assert not seen
    assert window.stack.currentWidget() is window.parameters_page
    assert window.parameters_page.dirty


def test_no_saved_parameters_disables_old_version(window, tmp_path, monkeypatch):
    (tmp_path / "system/parameters.txt").unlink()
    seen = []
    choose(monkeypatch, "取消", seen)
    assert window._choose_generation_parameters() is None
    assert ("使用已保存参数", False) in seen[0]


@pytest.mark.parametrize("entry", ["full", "characters", "workflow", "resume"])
def test_counts_are_shared_across_all_entrypoints(window, monkeypatch, entry):
    window.lore_page.faction_stepper.set_value(2)
    window.lore_page.character_stepper.set_value(3)
    assert window.parameters_page.dirty
    seen = capture_runs(window, monkeypatch)
    choose(monkeypatch, "保存并生成")
    if entry in {"full", "characters"}:
        window.lore_page._run_step(entry)
    elif entry == "workflow":
        window.workflow_page._run_all()
    else:
        from core.gui.widgets.step_rail import StepState
        window.workflow_page.set_states({"lore": StepState("partial")})
        window.workflow_page._resume()
    assert seen
    assert all(p["num_factions"] == 2 and p["num_characters"] == 3 for _, p in seen)
    stored = load_stage_parameters(window.output_dir())
    assert stored["num_factions"] == 2 and stored["num_characters"] == 3


def test_using_saved_counts_does_not_mix_current_controls(window, monkeypatch):
    window.lore_page.faction_stepper.set_value(2)
    window.lore_page.character_stepper.set_value(3)
    seen = capture_runs(window, monkeypatch)
    choose(monkeypatch, "使用已保存参数")
    window.lore_page._run_step("full")
    assert seen[0][1]["num_factions"] == 6 and seen[0][1]["num_characters"] == 8
    assert window.lore_page.faction_stepper.value() == 2 and window.lore_page.character_stepper.value() == 3


def test_background_work_keeps_original_snapshot_when_drafts_change(window, monkeypatch):
    tasks = []
    monkeypatch.setattr(window.runner, "run", lambda work, **kw: tasks.append(work))
    window.workflow_page._run_all()
    assert len(tasks) == 1
    window.parameters_page.theme_edit.setText("运行后修改")
    params_store.save(window.output_dir(), {"genre": "Mystery", "theme": "外部文件变化"})
    observed = []
    monkeypatch.setattr(step_runner, "run_stage", lambda step, root, model="", **kw: observed.append(deepcopy(kw["parameters"])) or {})
    tasks[0](lambda *args: None)
    assert len(observed) == 4 and all(p["theme"] == "旧主题" for p in observed)


def test_effective_difference_prompts_even_without_dirty_flag(window, monkeypatch):
    params_store.save(window.output_dir(), {**window.parameters_page.parameters(), "theme": "外部保存的主题"})
    assert not window.parameters_page.dirty
    choose(monkeypatch, "使用已保存参数")
    seen = capture_runs(window, monkeypatch)
    window.lore_page._run_step("factions")
    assert seen[0][1]["theme"] == "外部保存的主题"
    assert window.parameters_page.theme_edit.text() == "旧主题"


def test_loading_counts_and_unknown_parameters_preserves_them(window, monkeypatch):
    params_store.save(window.output_dir(), {**window.parameters_page.parameters(), "Num Chars": 3, "num_characters": 3,
                                          "num_factions": 2, "legacy_extra": "历史中文要求", "custom_zero": 0})
    window.parameters_page.load(window.output_dir())
    window.refresh_all()
    assert window.lore_page.faction_stepper.value() == 2 and window.lore_page.character_stepper.value() == 3
    window.parameters_page.theme_edit.setText("重新编辑")
    assert window.parameters_page.save()
    saved = load_stage_parameters(window.output_dir())
    assert saved["num_characters"] == 3 and saved["legacy_extra"] == "历史中文要求" and saved["custom_zero"] == "0"


def test_diagnostics_button_opens_local_records(window, tmp_path, monkeypatch):
    (tmp_path / "system/generation_runs").mkdir(parents=True)
    opened = []
    monkeypatch.setattr(os, "startfile", opened.append)
    window.lore_page.open_diagnostics_button.click()
    from pathlib import Path
    assert Path(opened[0]) == tmp_path / "system/generation_runs"
