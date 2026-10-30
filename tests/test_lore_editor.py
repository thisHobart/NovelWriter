"""Qt editor tests run locally, with no model calls or external UI automation."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from core.gui.pages.lore_editor import LoreEditor, ValueForm
from core.gui.pages.lore_page import LorePage
from core.generation.lore_repository import BODY, CHARACTERS, FACTIONS, CONTRACT, LORE, LoreRepository
from test_lore_repository import project, write


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_markdown_save_and_cancel_leave_readonly_component_unchanged(qapp, project, monkeypatch):
    from core.gui.pages.lore_page import MarkdownView
    assert MarkdownView("正文").isReadOnly()
    editor = LoreEditor(str(project), BODY)
    editor.field.setPlainText("# 用户编辑的世界")
    assert editor.dirty
    assert editor.dirty_label.text() == "未保存"
    editor.tabs.setCurrentIndex(1)
    assert "用户编辑的世界" in editor.preview_view.toPlainText()
    assert editor.save()
    assert editor.result() == QDialog.Accepted
    assert (project / BODY).read_text(encoding="utf-8") == "# 用户编辑的世界"
    cancel = LoreEditor(str(project), BODY)
    cancel.field.setPlainText("放弃的草稿")
    monkeypatch.setattr(cancel, "resolve_draft", lambda: True)
    cancel.reject()
    assert (project / BODY).read_text(encoding="utf-8") == "# 用户编辑的世界"


def test_recursive_form_preserves_legacy_arrays_and_nested_values(qapp, project):
    editor = LoreEditor(str(project), CHARACTERS, ("characters", 0))
    fields = editor.field.children_fields
    fields["name"].control.setText("张远")
    fields["background"].control.setPlainText("新的动机与背景")
    fields["age"].control.setText("31")
    assert editor.save()
    cast = json.loads((project / CHARACTERS).read_text(encoding="utf-8"))
    assert cast["characters"][0]["goals"] == ["破案", "归家"]
    assert cast["characters"][0]["extra"] == {"keep": 7}
    assert cast["characters"][0]["age"] == 31
    assert cast["relationships"][0]["character1"] == "张远"


def test_registry_form_has_typed_rules_and_readonly_ids(qapp, project):
    editor = LoreEditor(str(project), CONTRACT)
    fields = editor.field.children_fields
    fields["era"].control.setText("近未来")
    fields["world_constraints"].control.setPlainText("没有超能力\n科技有代价")
    identifier = fields["canonical_characters"].children_fields[0].children_fields["id"]
    assert identifier.control is None
    assert editor.save()
    contract = json.loads((project / CONTRACT).read_text(encoding="utf-8"))
    assert contract["world_constraints"] == ["没有超能力", "科技有代价"]


def test_failed_save_and_busy_state_keep_draft(qapp, project):
    editor = LoreEditor(str(project), BODY)
    editor.field.setPlainText("我的草稿")
    editor.set_busy(True)
    assert not editor.save_button.isEnabled()
    assert not editor.save()
    editor.set_busy(False)
    write(project, BODY, "外部修改")
    assert not editor.save()
    assert editor.dirty
    assert editor.field.toPlainText() == "我的草稿"
    assert "文件已" in editor.message.text()
    assert (project / BODY).read_text(encoding="utf-8") == "外部修改"


def test_refresh_does_not_replace_active_editor(qapp, project):
    page = LorePage()
    page.set_output_dir(str(project))
    editor = LoreEditor(str(project), BODY, parent=page)
    page.active_editor = editor
    editor.field.setPlainText("尚未保存的正文")
    tabs = page.tabs.widget(0)
    page.refresh()
    assert page.tabs.widget(0) is tabs
    assert page.active_editor.field.toPlainText() == "尚未保存的正文"
    page.set_generation_busy(True)
    assert not editor.save_button.isEnabled()
    page.set_generation_busy(False)
    assert editor.save_button.isEnabled()
    page.active_editor = None


@pytest.mark.parametrize("choice,expected", [("保存", True), ("放弃", True), ("取消", False)])
def test_unsaved_dialog_decisions(qapp, project, monkeypatch, choice, expected):
    editor = LoreEditor(str(project), BODY)
    before = (project / BODY).read_text(encoding="utf-8")
    editor.field.setPlainText("待保存的正文")
    picked = []
    def fake_exec(box):
        picked.append(next(button for button in box.buttons() if button.text() == choice))
    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: picked[-1])
    assert editor.resolve_draft() is expected
    assert (project / BODY).read_text(encoding="utf-8") == ("待保存的正文" if choice == "保存" else before)


def test_mainwindow_guards_navigation_project_switch_exit_and_task(qapp, project, monkeypatch):
    from core.gui.main_window import MainWindow
    from PySide6.QtGui import QCloseEvent
    window = MainWindow()
    window.parameters_page.output_dir_edit.setText(str(project))
    window.refresh_all()
    window._navigate("lore")
    editor = LoreEditor(str(project), BODY, parent=window.lore_page)
    window.lore_page.active_editor = editor
    editor.field.setPlainText("未保存")
    monkeypatch.setattr(editor, "resolve_draft", lambda: False)
    window._navigate("structure")
    assert window.stack.currentWidget() is window.lore_page
    window.parameters_page.output_dir_edit.setText(str(project / "another"))
    window._on_output_dir_changed(str(project / "another"))
    assert window.output_dir() == str(project)
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted()
    called = []
    window._on_task_requested(lambda report: called.append(True), None, "测试", None)
    assert not called and not window.runner.busy
    window.lore_page.active_editor = None
    window.close()


def test_downstream_banner_preserves_workflow_state(qapp, project):
    from core.gui.main_window import MainWindow
    window = MainWindow()
    window.parameters_page.output_dir_edit.setText(str(project))
    before = window._stage_states()
    repo = LoreRepository(project)
    repo.save(repo.load_snapshot(), {BODY: "新世界设定"})
    window.refresh_all()
    after = window._stage_states()
    for stage in ("structure", "scenes", "chapters"):
        assert after[stage].state == before[stage].state
        assert after[stage].clickable == before[stage].clickable
        assert "待核对" in after[stage].detail
    assert "待核对" in window.structure_page.lore_review_notice.text()
    window.close()


def test_regeneration_confirmation_includes_manual_files_and_can_cancel(qapp, project, monkeypatch):
    from core.gui.main_window import MainWindow
    from core.gui.services.step_runner import step_work, full_workflow_work
    repo = LoreRepository(project)
    repo.save(repo.load_snapshot(), {BODY: "人工设定"})
    window = MainWindow()
    window.parameters_page.output_dir_edit.setText(str(project))
    window.refresh_all()
    confirmations = []
    from core.generation.parameters import freeze_parameters
    monkeypatch.setattr(window, "_choose_generation_parameters", lambda: (freeze_parameters({"genre": "Mystery"}), False))
    def deny(parent, title, text, *args):
        confirmations.append(text)
        return QMessageBox.No
    monkeypatch.setattr(QMessageBox, "question", deny)
    for work in (step_work("lore", str(project)), full_workflow_work(str(project))):
        window._on_task_requested(work, None, "生成", None)
        assert not window.runner.busy
    assert len(confirmations) == 2
    assert all("世界观正文" in text and "备份" in text for text in confirmations)
    assert repo.load_snapshot().files[BODY] == "人工设定"
    window.close()


def test_editor_accepts_explicit_registry_mapping(qapp, project, monkeypatch):
    from PySide6.QtWidgets import QInputDialog
    registry = json.loads((project / CONTRACT).read_text(encoding="utf-8"))
    registry["canonical_characters"][0]["name"] = "历史登记名"
    write(project, CONTRACT, registry)
    editor = LoreEditor(str(project), CHARACTERS, ("characters", 0))
    editor.field.children_fields["name"].control.setText("张远")
    calls = []
    def choose(parent, title, text, choices, *args):
        calls.append(choices)
        return choices[0], True
    monkeypatch.setattr(QInputDialog, "getItem", choose)
    assert editor.save()
    assert calls
    saved = json.loads((project / CONTRACT).read_text(encoding="utf-8"))
    assert saved["canonical_characters"][0]["name"] == "张远"


def test_legacy_english_fields_display_chinese_without_changing_original_values(qapp):
    from PySide6.QtWidgets import QLabel
    data = {
        "name": "张明", "gender": "Male", "profession": "Police Officer",
        "motivations": ["Seeking justice for the innocent"], "resources": ["High-tech surveillance equipment"],
        "family": {"parents": [{"name": "张父", "relation": "Father", "status": "Deceased"}]},
        "jurisdiction": "Federal", "funding_level": "Heavily funded", "reputation": "Respected",
    }
    form = ValueForm(data)
    labels = [label.text() for label in form.findChildren(QLabel)]
    assert "资源" in labels and "动机" in labels and "亲属关系" in labels and "资金水平" in labels
    fields = form.children_fields
    assert fields["gender"].control.currentText() == "男"
    assert fields["profession"].control.text() == "警员"
    assert fields["motivations"].control.toPlainText() == "为无辜者伸张正义"
    assert fields["resources"].control.toPlainText() == "高科技监控设备"
    assert form.value() == data
    fields["profession"].control.setText("刑警队长")
    saved = form.value()
    assert saved["profession"] == "刑警队长"
    assert saved["gender"] == "Male" and saved["resources"] == data["resources"]


def test_faction_preview_uses_chinese_descriptions_instead_of_raw_json(qapp, project):
    data = json.loads((project / FACTIONS).read_text(encoding="utf-8"))
    data[0].update(type="Police Department", resources=["Forensic laboratories and crime scene units"],
                   description="A well-equipped police department with modern forensic capabilities")
    write(project, FACTIONS, data)
    before = (project / FACTIONS).read_bytes()
    editor = LoreEditor(str(project), FACTIONS, (0,))
    assert editor.windowTitle() == "编辑势力资料 · 调查局"
    editor.tabs.setCurrentIndex(1)
    text = editor.preview_view.toPlainText()
    assert "类型：警察部门" in text and "法证实验室与现场勘查队伍" in text
    assert "现代鉴证能力" in text
    assert "resources" not in text and "Police Department" not in text
    assert not editor.dirty
    assert editor.save()
    assert (project / FACTIONS).read_bytes() == before


def test_card_summary_and_background_labels_are_chinese(qapp):
    from PySide6.QtWidgets import QLabel
    from core.gui.pages.lore_page import EntityCard
    from core.gui.services.artifacts import Entity
    from core.lore_localization import file_label
    card = EntityCard(Entity("调查所", "势力", "A reputable agency known for solving difficult mysteries", {}))
    assert any("侦破疑难案件" in label.text() for label in card.findChildren(QLabel))
    assert file_label("background_protagonist_张明.md") == "人物背景 · 主角 · 张明"


def test_validation_messages_use_chinese_field_labels(qapp, project):
    editor = LoreEditor(str(project), CHARACTERS, ("characters", 0))
    fields = editor.field.children_fields
    fields["age"].control.setText("不是数字")
    assert not editor.save()
    assert "年龄需要填写有效的整数" in editor.message.text()
    fields["age"].control.setText("30")
    fields["background"].control.clear()
    assert not editor.save()
    assert "背景" in editor.message.text() and "不能为空" in editor.message.text()
    assert "characters" not in editor.message.text() and "background" not in editor.message.text()


def test_unknown_extension_keys_keep_data_but_use_chinese_labels(qapp):
    from PySide6.QtWidgets import QLabel
    form = ValueForm({"custom_extension": "自定义内容", "another_extension": ["一", "二"]})
    labels = [label.text() for label in form.findChildren(QLabel)]
    assert "扩展属性 1" in labels and "扩展属性 2" in labels
    assert form.value() == {"custom_extension": "自定义内容", "another_extension": ["一", "二"]}
