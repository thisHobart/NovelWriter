"""Chinese dynamic form labels must preserve existing story parameter values."""
import os
import re

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QLabel

from core.config.story_options import SUBGENRES
from core.gui import params_store
from core.gui.pages.parameters_page import ParametersPage
from core.localization import zh_label


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def page(qapp, tmp_path):
    instance = ParametersPage()
    instance.output_dir_edit.setText(str(tmp_path))
    instance.title_edit.setText("中文参数测试")
    yield instance
    instance.close()
    instance.deleteLater()


def select_subgenre(page, genre, subgenre):
    page.genre_combo.setCurrentText(zh_label(genre))
    page.subgenre_combo.setCurrentText(zh_label(subgenre))


def test_all_builtin_dynamic_labels_and_options_are_chinese(page):
    for genre, subgenres in SUBGENRES.items():
        for subgenre in subgenres:
            select_subgenre(page, genre, subgenre)
            for index in range(page.dynamic_tabs.count()):
                tab = page.dynamic_tabs.widget(index)
                for label in tab.findChildren(QLabel):
                    if label.objectName() == "FieldLabel":
                        assert not re.search(r"[A-Za-z]", label.text()), (genre, subgenre, label.text())
            for widget in page._dynamic_widgets.values():
                if isinstance(widget, QComboBox):
                    for index in range(widget.count()):
                        assert re.search(r"[\u4e00-\u9fff]", widget.itemText(index)), (
                            genre, subgenre, widget.itemText(index),
                        )
                        assert widget.itemData(index) is not None


def test_screenshot_fields_and_dropdown_show_chinese(page):
    select_subgenre(page, "Sci-Fi", "Space Opera")
    labels = {label.text() for label in page.dynamic_tabs.widget(0).findChildren(QLabel)}
    assert {"星际旅行", "先进科技", "多种族共存", "太空战斗"} <= labels
    combo = page._dynamic_widgets["protagonist_type"]
    combo.setCurrentIndex(combo.findData("Merchant Captain"))
    assert combo.currentText() == "商船船长"
    assert page.parameters()["protagonist_type"] == "Merchant Captain"


@pytest.mark.parametrize("genre,subgenre", [
    ("Sci-Fi", "Hard Sci-Fi"),
    ("Fantasy", "Dark Fantasy"),
    ("Romance", "Contemporary Romance"),
])
def test_chinese_selection_saves_and_reloads_original_values(page, tmp_path, genre, subgenre):
    select_subgenre(page, genre, subgenre)
    expected = {}
    for key, widget in page._dynamic_widgets.items():
        if isinstance(widget, QComboBox):
            widget.setCurrentIndex(widget.count() - 1)
            expected[key] = widget.currentData()
        elif isinstance(widget, QCheckBox):
            widget.setChecked(not widget.isChecked())
            expected[key] = widget.isChecked()
    assert page.save()
    stored = params_store.load(str(tmp_path))
    for key, value in expected.items():
        assert stored[key.replace("_", " ").title()] == str(value)
    page.load(str(tmp_path))
    assert {key: page.parameters()[key] for key in expected} == expected
    for widget in page._dynamic_widgets.values():
        if isinstance(widget, QComboBox):
            assert re.search(r"[\u4e00-\u9fff]", widget.currentText())


def test_legacy_english_values_load_into_chinese_dropdowns(page, tmp_path):
    params_store.save(str(tmp_path), {
        "novel_title": "旧作品", "genre": "Sci-Fi", "subgenre": "Space Opera",
        "protagonist_type": "Diplomat", "conflict_scale": "Galactic",
        "space_combat": False,
    })
    page.load(str(tmp_path))
    assert page._dynamic_widgets["protagonist_type"].currentText() == "外交官"
    assert page._dynamic_widgets["conflict_scale"].currentText() == "银河系规模"
    assert page.parameters()["protagonist_type"] == "Diplomat"
    assert page.parameters()["conflict_scale"] == "Galactic"
    assert page.parameters()["space_combat"] is False
