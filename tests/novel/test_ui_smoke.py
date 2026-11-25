# -*- coding: utf-8 -*-
"""小说界面冒烟：能不能构造出来、能不能刷新、路由通不通。

需要 PySide6；没装就整体 skip，所以 CI 里不会因为缺 Qt 而红。
用 offscreen 平台，不需要显示器。
"""
import os

import pytest

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过界面冒烟测试")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core.gui import theme  # noqa: E402
from core.gui.main_window import MainWindow  # noqa: E402
from core.gui.pages.novel import params_store  # noqa: E402
from core.gui.pages.novel.chapter_writing_page import ChapterWritingPage  # noqa: E402
from core.gui.pages.novel.lore_page import LorePage  # noqa: E402
from core.gui.pages.novel.parameters_page import ParametersPage  # noqa: E402
from core.gui.pages.novel.scene_plan_page import ScenePlanPage  # noqa: E402
from core.gui.pages.novel.story_options import LENGTH_OPTIONS  # noqa: E402
from core.gui.pages.novel.structure_page import StructurePage  # noqa: E402
from core.gui.pages.novel.workflow_page import WorkflowPage  # noqa: E402


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    instance.setStyleSheet(theme.build_qss())
    yield instance


@pytest.mark.parametrize("page_class", [
    WorkflowPage, LorePage, StructurePage, ScenePlanPage, ChapterWritingPage,
])
def test_stage_pages_construct_and_refresh(app, page_class, tmp_path):
    page = page_class()
    page.output_dir = str(tmp_path)      # 空目录：所有页面都要能画出空态
    page.refresh()
    assert page.header is not None


def test_parameters_page_round_trip(app, tmp_path):
    page = ParametersPage()
    page.output_dir_edit.setText(str(tmp_path))
    page.title_edit.setText("静默星环")
    page.genre_edit.setText("近未来科幻")
    assert page.dirty is True
    assert page.save() is True
    assert page.dirty is False

    reopened = ParametersPage()
    reopened.load(str(tmp_path))
    assert reopened.title_edit.text() == "静默星环"


def test_parameters_page_blocks_empty_title(app, tmp_path):
    page = ParametersPage()
    page.output_dir_edit.setText(str(tmp_path))
    page.title_edit.setText("")
    assert "novel_title" in page.validate()
    assert page.save() is False


def test_parameters_page_requires_genre(app, tmp_path):
    page = ParametersPage()
    page.output_dir_edit.setText(str(tmp_path))
    page.title_edit.setText("镜中囚徒")
    page.genre_edit.setText("")
    assert "genre" in page.validate()
    assert page.save() is False


def test_parameters_page_converts_old_file_and_drops_removed_fields(app, tmp_path):
    system = tmp_path / "system"
    system.mkdir()
    (system / "parameters.txt").write_text(
        f"Output Directory: {tmp_path}\nGenre: Romance\nSubgenre: Contemporary Romance\n"
        "Story Length: Novel (Epic)\nStory Structure: Save the Cat!\nNovel Title: 镜中囚徒\n"
        "Author Name: 小林\nTheme: 蜗牛的爱情\nTone: 阴郁\n"
        "Gender Generation Bias String: Mostly Male (25F/75M)\nFemale Percentage: 25\n"
        "Happy Ending: True\nProtagonist Type: Career Professional\nQuality Loop: strict\n"
        "Scene Dialogue: off\nNum Factions: 3\n",
        encoding="utf-8",
    )
    page = ParametersPage()
    page.load(str(tmp_path))

    assert page.genre_edit.text() == "爱情（当代爱情）"
    assert page.length_combo.currentText() == "长篇小说"
    assert page.quality_mode() == "strict"
    assert page.dirty is True          # 换算过旧格式，提醒作者保存
    assert page.save() is True

    saved = params_store.load(str(tmp_path))
    assert saved["Genre"] == "爱情（当代爱情）"
    assert saved["Story Length"] == "Novel (Standard)"
    assert saved["Story Structure"] == "Save the Cat!"     # 已有作品沿用原来的结构
    assert saved["Quality Loop"] == "strict"
    assert saved["Scene Dialogue"] == "off" and saved["Num Factions"] == "3"
    for removed in ("Subgenre", "Author Name", "Gender Generation Bias String",
                    "Female Percentage", "Happy Ending", "Protagonist Type"):
        assert removed not in saved


def test_length_change_picks_default_structure(app, tmp_path):
    page = ParametersPage()
    page.load(str(tmp_path))
    page.length_combo.setCurrentIndex(LENGTH_OPTIONS.index("Novel (Standard)"))
    assert page.parameters()["story_structure"] == "6-Act Structure"
    page.length_combo.setCurrentIndex(LENGTH_OPTIONS.index("Short Story"))
    assert page.parameters()["story_structure"] == "3-Act Structure"


def test_quality_mode_is_chosen_on_chapter_page(app):
    window = MainWindow()
    novel = window.novel
    novel._navigate("chapters")
    combo = novel.chapter_page.quality_combo
    assert combo.currentData() == novel.parameters_page.quality_mode()

    target = next(mode for mode in ("off", "standard", "strict") if mode != combo.currentData())
    combo.setCurrentIndex(combo.findData(target))
    assert novel.parameters_page.quality_mode() == target
    assert novel.parameters_page.parameters()["quality_loop"] == target
    assert novel.parameters_page.dirty is True


def test_main_window_routes_between_pages(app):
    window = MainWindow()
    novel = window.novel
    assert set(novel.pages) == {
        "workflow", "parameters", "lore", "structure", "scenes", "chapters"}
    for key in novel.pages:
        novel._navigate(key)
        assert novel.stack.currentWidget() is novel.pages[key]
    # 被阻塞的阶段也允许进入，页面自己画阻塞空态
    novel._on_step_selected("blocked:scenes")
    assert novel.stack.currentWidget() is novel.pages["scenes"]


def test_refresh_is_not_reentrant(app):
    window = MainWindow()
    novel = window.novel
    calls = []
    original = novel._stage_states
    novel._stage_states = lambda: (calls.append(1), original())[1]
    novel.refresh_all()
    assert len(calls) == 1, "一次 refresh_all 只应做一次磁盘评估"
