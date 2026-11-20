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
from core.gui.pages.novel.chapter_writing_page import ChapterWritingPage  # noqa: E402
from core.gui.pages.novel.lore_page import LorePage  # noqa: E402
from core.gui.pages.novel.parameters_page import ParametersPage  # noqa: E402
from core.gui.pages.novel.scene_plan_page import ScenePlanPage  # noqa: E402
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
