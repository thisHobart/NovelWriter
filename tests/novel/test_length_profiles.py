# -*- coding: utf-8 -*-
"""篇幅档案：「故事篇幅」每个选项都有具体要求（全篇多长、几幕几章、每章几场、每场多少字、
写几个势力和人物、写法特点），各步只取自己要用的那部分；新加的「短视频脚本」走短篇那条流程。"""
import contextlib
import io
import logging
import os
import sys

import pytest

import core.generation.scene_pipeline as scene_pipeline
import core.generation.structure_pipeline as structure_pipeline
from core.config.story_options import (
    LENGTH_OPTIONS,
    counts_for,
    default_structure_for,
    is_single_chapter,
    length_profile,
)
from core.generation import stage_pipeline
from core.generation.lore_repository import CONTRACT, encode
from core.generation.planning_checks import chapter_count_note, scene_range_for
from core.generation.planning_prompt import outline_prompt, scene_plan_prompt, short_story_prompt
from core.generation.prompt_context import build_story_parameter_lines, normalize_story_parameters
from core.generation.scene_pipeline import ScenePipeline
from core.generation.stage_context import make_context
from core.generation.stage_pipeline import GenerationHost, load_stage_parameters
from core.generation.structure_pipeline import StructurePipeline
from core.localization import zh_label

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
import check_planning  # noqa: E402

SIX_ACTS = ("Beginning", "Rising Action", "First Climax", "Solution Finding", "Second Climax", "Resolution")


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class _Records(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _host(tmp_path, name, parameters=None):
    logger = logging.getLogger(name)
    records = _Records()
    logger.addHandler(records)
    logger.setLevel(logging.INFO)
    return GenerationHost(make_context(str(tmp_path), "fake", parameters or {}), logger), records


class _Sent(BaseException):
    """拿到第一份提示词就停下，不往下跑。"""


def test_every_length_option_has_a_profile_and_the_old_choices_keep_their_numbers():
    assert LENGTH_OPTIONS == ["Short Story", "Novella", "Novel (Standard)", "Short Video Script"]
    assert all(length_profile(option) for option in LENGTH_OPTIONS)
    # 原来三档的结构、势力和人物数不变。
    assert [default_structure_for(o) for o in LENGTH_OPTIONS[:3]] == ["3-Act Structure", "3-Act Structure", "6-Act Structure"]
    assert [counts_for(o) for o in LENGTH_OPTIONS[:3]] == [(3, 5), (4, 6), (6, 8)]
    assert zh_label("Short Video Script") == "短视频脚本"
    assert counts_for("Short Video Script") == (2, 3)
    # 以前的取值（参数页会换成长篇）没有档案，照旧走长篇流程。
    assert length_profile("Novel (Epic)") is None and length_profile("") is None
    assert [is_single_chapter(o) for o in LENGTH_OPTIONS] == [True, False, False, True]
    assert not is_single_chapter("Novel (Epic)")


def test_the_summary_says_how_long_each_choice_is():
    assert length_profile("Novel (Standard)").summary() == "全书约 10 万字，6 幕、约 30 章，每章 2–4 场，每场约 1000–1500 字"
    assert length_profile("Novella").summary() == "全书约 4 万字，3 幕、约 12 章，每章 2–4 场，每场约 1000–1500 字"
    assert length_profile("Short Story").summary() == "全篇约 1 万字，一章写完，分 3–6 场，每场约 1500–2500 字"
    assert length_profile("Short Video Script").summary() == "全篇约 2000 字以内，一章写完，分 2–3 场，每场约 500–800 字"
    # 以前选过别的结构的作品：不提幕数和章数，免得和作品自己的结构对不上。
    assert length_profile("Novel (Standard)").summary("Hero's Journey") == "全书约 10 万字，每章 2–4 场，每场约 1000–1500 字"
    assert length_profile("Novel (Standard)").chapters_per_act("6-Act Structure") == 5
    assert length_profile("Novella").chapters_per_act("3-Act Structure") == 4
    assert length_profile("Novel (Standard)").chapters_per_act("Hero's Journey") is None
    assert length_profile("Short Story").chapters_per_act("3-Act Structure") is None


def test_the_work_parameters_carry_the_profile():
    lines = build_story_parameter_lines({"Genre": "悬疑", "Story Length": "Novel (Standard)", "Story Structure": "6-Act Structure"})
    assert "- 篇幅：长篇小说（全书约 10 万字，6 幕、约 30 章，每章 2–4 场，每场约 1000–1500 字）" in lines
    assert "- 篇幅特点：主线加两三条支线交替推进；两次高潮各有收获和代价；每幕结尾留下推动下一幕的问题。" in lines
    video = build_story_parameter_lines({"Genre": "悬疑", "Story Length": "Short Video Script"})
    assert "- 篇幅：短视频脚本（全篇约 2000 字以内，一章写完，分 2–3 场，每场约 500–800 字）" in video
    assert any(line.startswith("- 篇幅特点：一个核心事件，两三个人物") for line in video)
    # 不认识的篇幅照旧只写名字。
    old = build_story_parameter_lines({"Genre": "悬疑", "Story Length": "Novel (Epic)"})
    assert not any("篇幅特点" in line or "（全书" in line for line in old)


def _outline(structure, section, start):
    params = normalize_story_parameters({"Genre": "悬疑", "Story Length": "Novel (Standard)", "Story Structure": structure})
    return str(outline_prompt(
        genre_label="悬疑", structure_name=structure, sections=SIX_ACTS, section_name=section,
        section_content="# 情节上升\n沈宁追查那封信。", start_chapter=start, story_params=params,
        location_guidance=[],
    ))


def test_the_outline_prompt_says_how_many_chapters_this_act_gets():
    text = _outline("6-Act Structure", "Rising Action", 6)
    assert "“情节上升”从第 6 章开始，请依次分配章号，这一幕写 5 章（第 6–10 章）。" in text
    # 结构和档案对不上时不规定章数，照旧由模型分。
    assert "这一幕写" not in _outline("Hero's Journey", "Rising Action", 6)


def test_the_scene_plan_prompt_says_how_many_scenes_and_how_long_each_is(tmp_path):
    def prompt(length):
        params = normalize_story_parameters({"Genre": "悬疑", "Story Length": length, "Story Structure": "3-Act Structure"})
        return str(scene_plan_prompt(
            genre_label="悬疑", chapter_number=1, story_length=length, structure_name="3-Act Structure",
            sections=("Act 1: Setup", "Act 2: Confrontation", "Act 3: Resolution"), section_name="Act 1: Setup",
            story_params=params, section_outline="沈宁收到一封信。", location_guidance=[], lore_content="当代港城。",
            contract_text="（契约格式）", output_dir=str(tmp_path), chapter_outlines={1: "### 第 1 章：信\n沈宁收到一封信。"},
        ))

    novella = prompt("Novella")
    assert "本章安排 2–4 个场景，每场写成正文约 1000–1500 字；按这个量安排每场的事件，一场不要塞进太多事。" in novella
    assert "本故事是中篇小说" not in novella
    assert "本章安排" not in prompt("Novel (Epic)")


def _short(length):
    params = normalize_story_parameters({"Genre": "悬疑", "Story Length": length})
    return str(short_story_prompt(
        genre_label="悬疑", novel_title="信", structure_name="3-Act Structure", story_params=params,
        plot="沈宁收到一封信。", location_guidance=[], lore_content="当代港城。", contract_text="（契约格式）",
    ))


def test_a_short_video_script_is_planned_as_two_or_three_short_scenes():
    video = _short("Short Video Script")
    assert "请为悬疑短视频脚本《信》规划场景。" in video
    assert "把故事拆分成 2-3 个清晰、独立的场景" in video and "也不要超过 3 个场景。每场写成正文约 500–800 字。" in video
    story = _short("Short Story")
    assert "请为悬疑短篇小说《信》规划场景。" in story
    assert "把故事拆分成 3-6 个清晰、独立的场景" in story and "每场写成正文约 1500–2500 字" in story
    assert scene_range_for("Short Video Script") == (2, 3) and scene_range_for("Short Story") == (3, 6)
    assert scene_range_for("Novella") == (2, 4) and scene_range_for("Novel (Epic)") == (2, 4)


def test_a_short_video_script_goes_through_the_short_story_flow(tmp_path, monkeypatch):
    _write(tmp_path, "system/parameters.txt", "Genre: 悬疑\nStory Length: Short Video Script\nBackend: api\nModel: fake\n")
    ran = []

    def fake_plan(self, ui):
        ran.append(ui.parameters["story_length"])
        _write(tmp_path, "story/planning/scenes_short_story_3-act_structure.md", "### 场景 1：信\n")

    monkeypatch.setattr(stage_pipeline, "set_backend", lambda *_args: None)
    monkeypatch.setattr(stage_pipeline, "_guard_upstream_lore", lambda *_args: None)
    monkeypatch.setattr(ScenePipeline, "_plan_short_story_scenes", fake_plan)
    monkeypatch.setattr(ScenePipeline, "_generate_chapter_outline",
                        lambda *_a: pytest.fail("短视频脚本不该生成章节大纲"))
    result = stage_pipeline.run_stage_action("scenes", "outline", str(tmp_path))
    assert ran == ["Short Video Script"]
    assert result["generated_files"] == [os.path.join("story", "planning", "scenes_short_story_3-act_structure.md")]


def test_the_structure_step_gives_each_act_its_share_of_chapters(tmp_path, monkeypatch):
    _write(tmp_path, "story/planning/reconciled_locations_arcs.md", "# 故事弧\n沈宁追查那封信。")
    sent = []

    def fake_send(prompt, **_):
        sent.append(prompt)
        raise _Sent()

    monkeypatch.setattr(structure_pipeline, "send_prompt", fake_send)
    host, _records = _host(tmp_path, "test_length_profiles.structure",
                           {"genre": "悬疑", "story_length": "Novella", "story_structure": "3-Act Structure"})
    with contextlib.redirect_stdout(io.StringIO()), pytest.raises(_Sent):
        StructurePipeline(host).improve_structure(host.context)
    assert "本作是中篇小说：全书约 4 万字，3 幕、约 12 章，每章 2–4 场，每场约 1000–1500 字。这一部分大约写 4 章，" in sent[0]
    assert "写法特点：一条主线加一条支线" in sent[0]
    assert "保持紧凑和聚焦" not in sent[0]


def test_the_short_plot_outline_is_sized_by_the_profile(tmp_path, monkeypatch):
    sent = []

    def fake_send(prompt, **_):
        sent.append(prompt)
        raise _Sent()

    monkeypatch.setattr(structure_pipeline, "send_prompt", fake_send)
    host, _records = _host(tmp_path, "test_length_profiles.plot",
                           {"genre": "悬疑", "story_length": "Short Video Script",
                            "story_structure": "3-Act Structure", "novel_title": "信"})
    with contextlib.redirect_stdout(io.StringIO()), pytest.raises(_Sent):
        StructurePipeline(host)._outline_short_story_plot(host.context)
    assert "请为短视频脚本《信》规划详细情节。" in sent[0]
    assert "篇幅：全篇约 2000 字以内，一章写完，分 2–3 场，每场约 500–800 字。请按这个规模安排情节。" in sent[0]


def test_an_outline_with_the_wrong_number_of_chapters_is_logged_not_sent_back(tmp_path, monkeypatch):
    _write(tmp_path, "system/parameters.txt",
           "Genre: 悬疑\nStory Length: Novel (Standard)\nStory Structure: 6-Act Structure\nQuality Loop: off\n")
    _write(tmp_path, CONTRACT, encode({"era": "当代", "canonical_characters": [{"id": "CH001", "name": "沈宁", "aliases": []}],
                                       "canonical_locations": [], "schema_version": 4}))
    _write(tmp_path, "story/structure/6-act_structure_beginning.md", "# 开端\n沈宁收到一封信。")
    sent = []
    monkeypatch.setattr(scene_pipeline, "send_prompt", lambda prompt, **_: sent.append(prompt) or
                        "### 第 1 章：信\n* **涉及人物**：沈宁。\n\n### 第 2 章：楼道\n* **涉及人物**：沈宁。\n")
    for name in ("show_error", "show_warning", "show_info"):
        monkeypatch.setattr(scene_pipeline, name, lambda *a, **k: None, raising=False)
    host, records = _host(tmp_path, "test_length_profiles.outline", load_stage_parameters(str(tmp_path)))
    with contextlib.redirect_stdout(io.StringIO()):
        ScenePipeline(host)._generate_chapter_outline(host.context)
    assert len(sent) == 1
    assert "这一幕写 5 章（第 1–5 章）" in sent[0]
    assert "“开端”大纲写了 2 章，建议 5 章（只记录，不打回）" in records.lines
    assert chapter_count_note(5, 5) == "" and chapter_count_note(3, None) == ""


def test_the_check_counts_chapters_per_act(tmp_path):
    _write(tmp_path, "system/parameters.txt",
           "Genre: 悬疑\nStory Length: Novel (Standard)\nStory Structure: 6-Act Structure\n")
    _write(tmp_path, "story/structure/6-act_structure_beginning.md", "# 开端\n沈宁收到一封信。")
    _write(tmp_path, "story/planning/chapter_outlines/chapter_outlines_6-act_structure_beginning.md",
           "### 第 1 章：信\n* **涉及人物**：沈宁。\n\n### 第 2 章：楼道\n* **涉及人物**：沈宁。\n")
    with contextlib.redirect_stdout(io.StringIO()):
        text = check_planning.render(check_planning.collect(str(tmp_path)))
    assert "【每幕章数】开端 2" in text
    assert "建议每幕 5 章；章数不同的幕 1 个（开端 2 章）" in text


def test_the_scene_writing_prompt_names_a_short_video_script(tmp_path):
    from core.generation.domain_profiles import resolve_domain_profile
    from core.generation.scene_prompt import build_scene_prompt

    params = {"Genre": "悬疑", "Story Length": "Short Video Script"}
    text = build_scene_prompt(scene_plan="沈宁拆开信。", scene_number=1, parameters=params, lore="当代港城。",
                              character_roster="", faction_summary="", profile=resolve_domain_profile(params),
                              novel_title="信")
    assert "请撰写悬疑短视频脚本《信》中的一个场景。" in text
    assert "每场约 500–800 字" in text


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PySide6", reason="未安装 PySide6，跳过参数页的检查")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_the_parameters_page_explains_the_chosen_length(app, tmp_path):
    from core.gui.pages.novel.parameters_page import ParametersPage, length_hint

    page = ParametersPage()
    page.load(str(tmp_path))
    assert [page.length_combo.itemText(i) for i in range(page.length_combo.count())] == [
        "短篇小说", "中篇小说", "长篇小说", "短视频脚本"]
    page.length_combo.setCurrentIndex(LENGTH_OPTIONS.index("Novel (Standard)"))
    assert page.length_hint.text().startswith("全书约 10 万字，6 幕、约 30 章，每章 2–4 场，每场约 1000–1500 字；写 6 个势力、8 个人物。")
    page.length_combo.setCurrentIndex(LENGTH_OPTIONS.index("Short Video Script"))
    assert page.length_hint.text().startswith("全篇约 2000 字以内，一章写完，分 2–3 场，每场约 500–800 字；写 2 个势力、3 个人物。")
    params = page.parameters()
    assert params["story_length"] == "Short Video Script" and params["story_structure"] == "3-Act Structure"
    assert (params["num_factions"], params["num_characters"]) == (2, 3)
    assert length_hint("Novel (Epic)") == ""
