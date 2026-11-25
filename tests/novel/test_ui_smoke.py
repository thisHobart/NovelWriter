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
from core.gui.pages.novel import params_store, story_options  # noqa: E402
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
    page.genre_combo.setEditText("近未来科幻")
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
    page.genre_combo.setEditText("")
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

    assert page.genre_combo.currentText() == "爱情（当代爱情）"
    assert page.length_combo.currentText() == "长篇小说"
    assert page.quality_mode() == "strict"
    assert page.dirty is True          # 换算过旧格式，提醒作者保存
    assert page.save() is True

    saved = params_store.load(str(tmp_path))
    assert saved["Genre"] == "爱情（当代爱情）"
    assert saved["Story Length"] == "Novel (Standard)"
    assert saved["Story Structure"] == "Save the Cat!"     # 已有作品沿用原来的结构
    assert saved["Quality Loop"] == "strict"
    assert saved["Scene Dialogue"] == "off"
    assert saved["Num Factions"] == "6"        # 数量按篇幅定：长篇 6 个势力，旧文件里的 3 不再生效
    for removed in ("Subgenre", "Author Name", "Gender Generation Bias String",
                    "Female Percentage", "Happy Ending", "Protagonist Type"):
        assert removed not in saved


def _choose_genre(page, genre):
    combo = page.genre_combo
    index = combo.findText(genre)
    combo.setCurrentIndex(index)
    combo.activated.emit(index)


def test_genre_offers_presets_and_accepts_custom_text(app, tmp_path):
    page = ParametersPage()
    page.load(str(tmp_path))
    combo = page.genre_combo
    assert combo.isEditable()
    assert [combo.itemText(i) for i in range(combo.count())] == ["悬疑推理", "恐怖", "科幻", "爱情"]
    assert combo.currentText() == ""            # 新作品不替作者选题材

    combo.setEditText("赛博朋克复仇")
    combo.lineEdit().editingFinished.emit()
    assert page.parameters()["genre"] == "赛博朋克复仇"
    assert combo.count() == 4                   # 自己写的题材不加进列表


def test_theme_and_tone_follow_the_chosen_genre(app, tmp_path):
    page = ParametersPage()
    page.load(str(tmp_path))
    _choose_genre(page, "科幻")
    theme_items = [page.theme_combo.itemText(i) for i in range(page.theme_combo.count())]
    assert theme_items == list(story_options.THEME_OPTIONS["科幻"])
    assert page.theme_combo.currentText() == theme_items[0]
    assert page.tone_combo.currentText() == story_options.TONE_OPTIONS["科幻"][0]

    page.tone_combo.setEditText("冷到骨子里")   # 作者自己写的基调
    _choose_genre(page, "爱情")
    assert page.theme_combo.currentText() == story_options.THEME_OPTIONS["爱情"][0]
    assert page.tone_combo.currentText() == "冷到骨子里"
    tone_items = [page.tone_combo.itemText(i) for i in range(page.tone_combo.count())]
    assert tone_items == list(story_options.TONE_OPTIONS["爱情"])


def test_custom_genre_uses_presets_of_matching_keywords(app):
    assert story_options.preset_genre_for("都市悬疑") == "悬疑推理"
    assert story_options.preset_genre_for("爱情（当代爱情）") == "爱情"
    assert story_options.preset_genre_for("古风权谋") == ""
    assert story_options.theme_options_for("古风权谋") == list(story_options.THEME_OPTIONS[""])


def test_loading_keeps_saved_theme_and_tone(app, tmp_path):
    system = tmp_path / "system"
    system.mkdir()
    (system / "parameters.txt").write_text(
        f"Output Directory: {tmp_path}\nGenre: 科幻\nStory Length: Novella\n"
        "Novel Title: 静默星环\nTheme: 记忆的代价\nTone: 何以为人\n",
        encoding="utf-8",
    )
    page = ParametersPage()
    page.load(str(tmp_path))
    assert page.theme_combo.currentText() == "记忆的代价"
    assert page.tone_combo.currentText() == "何以为人"
    assert page.dirty is False
    assert page.theme_combo.itemText(0) == story_options.THEME_OPTIONS["科幻"][0]


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


def _lore_project(root):
    from core.generation.lore_repository import BODY, CHARACTERS, FACTIONS, encode
    for rel, value in ((BODY, "# 世界\n张明在调查局工作。"),
                       (CHARACTERS, encode({"characters": [{"name": "张明", "role": "protagonist"}]})),
                       (FACTIONS, encode([{"name": "调查局", "description": "原描述"}]))):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")


def test_lore_page_offers_undo_and_redo_after_a_change(app, tmp_path):
    from core.generation.lore_repository import BODY, LoreRepository
    _lore_project(tmp_path)
    page = LorePage()
    page.output_dir = str(tmp_path)
    page.refresh()
    assert page.change_bar.isHidden()            # 还没有改动，不摆撤回

    repo = LoreRepository(tmp_path)
    repo.save(repo.load_snapshot(), {BODY: "改过的世界观"})
    page.refresh()
    assert not page.change_bar.isHidden()
    assert "编辑世界观" in page.change_label.text()
    page.undo_button.click()
    assert repo.load_snapshot().files[BODY] == "# 世界\n张明在调查局工作。"
    assert not page.redo_button.isHidden()
    page.redo_button.click()
    assert repo.load_snapshot().files[BODY] == "改过的世界观"


def test_counts_follow_story_length_and_are_not_asked(app, tmp_path):
    from core.generation.stage_pipeline import load_stage_parameters
    page = ParametersPage()
    page.output_dir_edit.setText(str(tmp_path))
    page.title_edit.setText("雾港来信")
    page.genre_combo.setEditText("悬疑推理")
    page.length_combo.setCurrentIndex(LENGTH_OPTIONS.index("Short Story"))
    assert page.save() is True
    saved = load_stage_parameters(str(tmp_path))
    assert (saved["num_factions"], saved["num_characters"]) == (3, 5)

    lore = LorePage()
    lore.output_dir = str(tmp_path)
    lore.refresh()
    assert not hasattr(lore, "faction_stepper")
    assert "3 个势力 · 5 个人物" in lore.counts_hint.text()


def test_old_counts_in_parameter_file_no_longer_override_length(app, tmp_path):
    from core.generation.stage_pipeline import load_stage_parameters
    system = tmp_path / "system"
    system.mkdir()
    (system / "parameters.txt").write_text(
        "Genre: 科幻\nStory Length: Novella\nNovel Title: 静默星环\nNum Factions: 12\nNum Characters: 1\n",
        encoding="utf-8")
    saved = load_stage_parameters(str(tmp_path))      # 旧文件里超出范围的数量也不报错
    assert (saved["num_factions"], saved["num_characters"]) == (4, 6)


def test_runtime_only_change_does_not_ask_which_parameters_to_use(app, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QMessageBox
    system = tmp_path / "system"
    system.mkdir()
    (system / "parameters.txt").write_text(
        f"Output Directory: {tmp_path}\nGenre: 科幻\nStory Length: Novella\nStory Structure: 3-Act Structure\n"
        "Novel Title: 静默星环\nTheme: 记忆的代价\nTone: 冷峻理性\nQuality Loop: standard\n",
        encoding="utf-8")
    window = MainWindow()
    novel = window.novel
    novel.parameters_page.load(str(tmp_path))
    asked = []
    monkeypatch.setattr(QMessageBox, "exec", lambda self: asked.append(self.windowTitle()) or 0)
    novel.parameters_page.set_quality_mode("strict")   # 只改运行设置
    assert novel.parameters_page.dirty is True
    snapshot, use_saved = novel._choose_generation_parameters()
    assert asked == [] and use_saved is False
    assert snapshot["quality_loop"] == "strict"

    novel.parameters_page.title_edit.setText("另一个标题")   # 改了作品参数，仍要问
    assert novel._choose_generation_parameters() is None
    assert asked == ["作品参数尚未保存"]


def test_refresh_is_not_reentrant(app):
    window = MainWindow()
    novel = window.novel
    calls = []
    original = novel._stage_states
    novel._stage_states = lambda: (calls.append(1), original())[1]
    novel.refresh_all()
    assert len(calls) == 1, "一次 refresh_all 只应做一次磁盘评估"
