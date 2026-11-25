# -*- coding: utf-8 -*-
"""小说界面冒烟：能不能构造出来、能不能刷新、路由通不通。

需要 PySide6；没装就整体 skip，所以 CI 里不会因为缺 Qt 而红。
用 offscreen 平台，不需要显示器。
"""
import os

import pytest

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过界面冒烟测试")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QTextBrowser  # noqa: E402

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


def _tab_names(page):
    return [page.tabs.tabText(i).split(" ")[0] for i in range(page.tabs.count())]


def test_empty_lore_shows_one_explanation_and_no_tabs(app, tmp_path):
    page = LorePage()
    page.output_dir = str(tmp_path)
    page.refresh()
    assert page.tabs.count() == 0 and page.tabs.isHidden()
    assert page.status_label.text().startswith("还没有世界设定")
    assert page.status_action.isHidden()
    assert page.generate_button.text() == "生成世界设定"
    assert page.steps_meta.text() == "0 / 4 已完成"


def test_lore_tabs_appear_only_with_content(app, tmp_path):
    from core.generation.lore_repository import CHARACTERS, CONTRACT, encode
    _lore_project(tmp_path)
    page = LorePage()
    page.output_dir = str(tmp_path)
    page.refresh()
    assert _tab_names(page) == ["世界观", "势力", "人物"]      # 没有待核对、候选标题、登记表就不摆

    (tmp_path / CONTRACT).write_text(encode({"era": "近未来", "world_constraints": ["没有超自然"],
                                             "canonical_characters": [{"name": "张明", "aliases": ["老张"]}]}),
                                     encoding="utf-8")
    cast = {"characters": [{"name": "张明", "role": "protagonist"}, {"name": "李华", "role": "antagonist"}],
            "relationships": [{"character1": "张明", "character2": "李华", "type": "宿敌"}]}
    (tmp_path / CHARACTERS).write_text(encode(cast), encoding="utf-8")
    titles = tmp_path / "story/planning/suggested_titles.md"
    titles.parent.mkdir(parents=True, exist_ok=True)
    titles.write_text("1. **雾港回声** —— 解释\n\n2. 《静默证词》\n", encoding="utf-8")
    page.refresh()
    assert _tab_names(page) == ["世界观", "势力", "人物", "名称与规则", "人物关系", "候选标题"]
    contract_view = page.tabs.widget(3).findChildren(QTextBrowser)[0]
    assert "近未来" in contract_view.toPlainText() and "老张" in contract_view.toPlainText()
    relation_view = page.tabs.widget(4).findChildren(QTextBrowser)[0]
    assert "宿敌" in relation_view.toPlainText()

    chosen = []
    page.title_chosen.connect(chosen.append)
    uses = [b for b in page.tabs.widget(5).findChildren(QPushButton) if b.text() == "用作作品标题"]
    assert len(uses) == 2
    uses[1].click()
    assert chosen == ["静默证词"]
    assert any(b.text() == "换一组" for b in page.tabs.widget(5).findChildren(QPushButton))


def test_old_backgrounds_are_not_listed_or_sent_to_later_stages(app, tmp_path):
    from core.generation.lore_repository import LORE, current_backgrounds, lore_context
    _lore_project(tmp_path)
    (tmp_path / LORE / "background_protagonist_张明.md").write_text("张明的背景", encoding="utf-8")
    (tmp_path / LORE / "background_protagonist_王五.md").write_text("早已不在名单里的王五", encoding="utf-8")
    assert current_backgrounds(tmp_path) == ["background_protagonist_张明.md"]
    context = lore_context(tmp_path)
    assert "张明的背景" in context and "王五" not in context
    assert (tmp_path / LORE / "background_protagonist_王五.md").exists()   # 文件不删

    page = LorePage()
    page.output_dir = str(tmp_path)
    page.refresh()
    assert "人物背景" not in _tab_names(page)
    assert page.step_buttons["enhance"].state == "done"
    labels = [b.text() for b in page.tabs.widget(2).findChildren(QPushButton)]
    assert labels == ["编辑", "编辑详细背景"]


def test_use_as_title_saves_the_parameter_and_names_the_old_one(app, tmp_path):
    system = tmp_path / "system"
    system.mkdir()
    (system / "parameters.txt").write_text(
        f"Output Directory: {tmp_path}\nGenre: 科幻\nStory Length: Novella\nStory Structure: 3-Act Structure\n"
        "Novel Title: 静默星环\nTheme: 记忆的代价\nTone: 冷峻理性\n", encoding="utf-8")
    window = MainWindow()
    novel = window.novel
    novel.parameters_page.load(str(tmp_path))
    novel.lore_page.title_chosen.emit("雾港回声")
    assert params_store.load(str(tmp_path))["Novel Title"] == "雾港回声"
    assert novel.parameters_page.dirty is False


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


def _result(root, kind, status, disposition="current", **extra):
    import time
    from uuid import uuid4
    from core.generation.lore_run import save_receipt
    record = {"id": uuid4().hex, "kind": kind, "status": status, "disposition": disposition, "candidate": "x",
              "issues": [], "files": [], "calls": 2, "created_at": extra.pop("created_at", time.time()), **extra}
    save_receipt(str(root), record)
    return record


def _result_cards(page):
    from core.gui.widgets import Card
    index = _tab_names(page).index("生成结果")
    return page.tabs.widget(index).findChildren(Card)


def _card_title(card):
    return card.findChild(QLabel, "CardTitle").text()


def _buttons(widget):
    return [b.text() for b in widget.findChildren(QPushButton)]


def test_results_show_latest_card_per_product_with_only_relevant_buttons(app, tmp_path):
    from core.generation.lore_repository import FACTIONS
    from core.generation.lore_run import file_digests
    _lore_project(tmp_path)
    saved = (tmp_path / FACTIONS).read_text(encoding="utf-8")
    _result(tmp_path, "factions", "needs_changes", created_at=1, issues=["旧的问题"])
    _result(tmp_path, "factions", "needs_changes", created_at=2, issues=["势力数量不对"],
            files=[FACTIONS], file_digests=file_digests({FACTIONS: saved}))
    _result(tmp_path, "characters", "draft", "draft", created_at=3, issues=["返回内容尚不能读取"],
            remaining=["lore", "enhance", "titles"])
    _result(tmp_path, "world", "complete", created_at=4, notes=["基调：信息不足"])
    page = LorePage()
    page.output_dir = str(tmp_path)
    page.refresh()

    assert [_card_title(card) for card in _result_cards(page)] == [
        "世界观 · 已完成 · 有检查备注", "人物 · 没有采用：返回内容读不出来", "势力 · 有问题，已保存"]
    world, cast, faction = _result_cards(page)
    assert _buttons(world) == []
    assert _buttons(cast) == ["再修一次并继续生成", "编辑原文"]
    assert _buttons(faction) == ["让 AI 再修一次", "编辑", "更早的记录（1）"]
    assert page.step_buttons["factions"]._meta.text() == "待处理"
    assert page.step_buttons["lore"]._meta.text() != "待处理"


def test_manual_edit_turns_a_problem_result_into_edited(app, tmp_path):
    from core.generation.lore_repository import FACTIONS, LoreRepository, encode
    from core.generation.lore_run import file_digests
    _lore_project(tmp_path)
    saved = (tmp_path / FACTIONS).read_text(encoding="utf-8")
    _result(tmp_path, "factions", "needs_changes", issues=["势力描述太短"],
            files=[FACTIONS], file_digests=file_digests({FACTIONS: saved}))
    page = LorePage()
    page.output_dir = str(tmp_path)
    page.refresh()
    assert page.step_buttons["factions"]._meta.text() == "待处理"

    repo = LoreRepository(tmp_path)
    repo.save(repo.load_snapshot(), {FACTIONS: encode([{"name": "调查局", "description": "作者改写的描述"}])})
    page.refresh()
    (card,) = _result_cards(page)
    assert _card_title(card) == "势力 · 已手动修改"
    assert _buttons(card) == []
    assert page.step_buttons["factions"]._meta.text() != "待处理"

    page.undo_button.click()                       # 撤回手改，问题又回来
    (card,) = _result_cards(page)
    assert _card_title(card) == "势力 · 有问题，已保存"


def test_ready_setting_points_to_the_next_stage_and_hides_regeneration_in_more(app, tmp_path):
    from core.generation.lore_repository import LORE
    _lore_project(tmp_path)
    (tmp_path / LORE / "background_protagonist_张明.md").write_text("张明的背景", encoding="utf-8")
    _result(tmp_path, "world", "complete", notes=["基调：信息不足", "主题：信息不足"])
    page = LorePage()
    page.output_dir = str(tmp_path)
    page.refresh()
    assert page.generate_button.text() == "下一步：故事结构"
    assert page.regenerate_action.isVisible()
    assert page.status_label.text() == ("世界设定已就绪：1 个势力 · 1 个人物 · 世界观 · 1 份人物背景。"
                                        "有 2 条检查备注，在「生成结果」里。")
    assert page.status_action.isHidden()
    went = []
    page.navigate_requested.connect(went.append)
    page.generate_button.click()
    assert went == ["structure"]


def test_status_line_says_what_is_missing_and_offers_to_continue(app, tmp_path, monkeypatch):
    from core.generation.lore_repository import BODY
    _lore_project(tmp_path)
    (tmp_path / BODY).unlink()
    page = LorePage()
    page.output_dir = str(tmp_path)
    page.refresh()
    assert page.generate_button.text() == "生成世界设定"
    assert not page.regenerate_action.isVisible()
    assert page.status_label.text() == "还差世界观、主要人物背景。"
    assert page.status_action.text() == "接着生成后面几项"
    asked = []
    monkeypatch.setattr(page, "run_task", lambda work, **kw: asked.append(kw["busy_text"]))
    page.status_action.click()
    assert asked == ["正在接着生成…"]


def test_status_line_sends_problems_to_results_and_flags_stale_items(app, tmp_path, monkeypatch):
    _lore_project(tmp_path)
    page = LorePage()
    page.output_dir = str(tmp_path)
    draft = _result(tmp_path, "characters", "draft", "draft", created_at=3, issues=["返回内容尚不能读取"])
    page.refresh()
    assert page.status_label.text() == "1 份需要处理：人物（没有采用：返回内容读不出来）。"
    assert page.status_action.text() == "去处理"
    page._open_tab("世界观")
    page.status_action.click()
    assert _tab_names(page)[page.tabs.currentIndex()] == "生成结果"

    draft["reverted"] = True
    from core.generation.lore_run import save_receipt
    save_receipt(str(tmp_path), draft)
    _result(tmp_path, "characters", "complete", created_at=1)
    _result(tmp_path, "world", "complete", created_at=2)
    _result(tmp_path, "factions", "complete", created_at=4)
    page.refresh()
    assert page.status_label.text() == "人物、世界观是按旧势力写的。"
    assert page.status_action.text() == "按新势力重写后面几项"
    asked = []
    monkeypatch.setattr(page, "run_task", lambda work, **kw: asked.append(kw["busy_text"]))
    page.status_action.click()
    assert asked == ["正在按新势力重写…"]


def test_finished_generation_stays_on_the_current_tab(app, tmp_path):
    _lore_project(tmp_path)
    _result(tmp_path, "factions", "complete")
    page = LorePage()
    page.output_dir = str(tmp_path)
    page.refresh()
    page._open_tab("人物")
    page._show_generation_result()
    assert _tab_names(page)[page.tabs.currentIndex()] == "人物"


def test_step_rail_counts_results_waiting_for_the_author(app, tmp_path):
    _lore_project(tmp_path)
    _result(tmp_path, "characters", "draft", "draft", issues=["返回内容尚不能读取"])
    window = MainWindow()
    novel = window.novel
    novel.parameters_page.output_dir_edit.setText(str(tmp_path))
    assert novel._stage_states()["lore"].detail.endswith("· 1 份待处理")


def test_review_tab_groups_by_stage_jumps_there_and_can_be_cleared_and_undone(app, tmp_path):
    from core.generation.lore_repository import CHARACTERS, LoreRepository, encode
    _lore_project(tmp_path)
    for rel in ("story/structure/act.md", "story/planning/scenes_1.md"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("张明出场", encoding="utf-8")
    repo = LoreRepository(tmp_path)
    repo.save(repo.load_snapshot(), {CHARACTERS: encode({"characters": [{"name": "张远", "role": "protagonist"}]})})
    page = LorePage()
    page.output_dir = str(tmp_path)
    page.refresh()
    review = page.tabs.widget(_tab_names(page).index("待核对"))
    lines = [label.text() for label in review.findChildren(QLabel)]
    assert "故事结构：1 个文件是按旧设定写的，其中 1 个仍写着旧名「张明」（现名「张远」）。" in lines
    went = []
    page.navigate_requested.connect(went.append)
    next(b for b in review.findChildren(QPushButton) if b.text() == "去故事结构").click()
    assert went == ["structure"]

    next(b for b in review.findChildren(QPushButton) if b.text() == "全部标为已看过").click()
    assert "待核对" not in _tab_names(page)
    assert "全部标为已看过" in page.change_label.text()
    page.undo_button.click()
    assert "待核对" in _tab_names(page)
