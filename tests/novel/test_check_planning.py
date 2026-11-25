# -*- coding: utf-8 -*-
"""不花调用的规划检查：只读、不调模型，拼出的提示词和「规划场景」正式流程一字不差。"""
import contextlib
import io
import json
import logging
import os
import shutil
import sys

import pytest

import core.generation.scene_pipeline as scene_pipeline
from core.generation.lore_repository import BODY, CONTRACT, encode
from core.generation.narrative_graph import NarrativeGraphManager
from core.generation.planning_checks import listed_names, unregistered_names
from core.generation.planning_contract import contract_instruction_blocks, contract_output_instructions
from core.generation.planning_prompt import outline_prompt, short_story_prompt
from core.generation.scene_pipeline import ScenePipeline
from core.generation.stage_context import make_context
from core.generation.stage_pipeline import GenerationHost, load_stage_parameters

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
import check_planning  # noqa: E402


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _contract(chapter, **fields):
    return json.dumps({"chapter": chapter, "origin": "scene_planning", "schema_version": 2,
                       "chapter_function": ["setup", "advance", "reversal"][chapter - 1], **fields},
                      ensure_ascii=False)


def _plan(*titles):
    return "\n\n".join(
        f"### 场景 {index}：{title}\n* **环境与具体地点**：白螺里七号楼。\n* **出场人物**：沈宁、陈放。"
        for index, title in enumerate(titles, 1)
    )


def _work(root):
    """三章的样例作品：第 1 章埋下悬念、引入事实，第 3 章了结；场景边界用了几种旧写法。"""
    _write(root, "system/parameters.txt",
           "Genre: 悬疑\nStory Length: Novel (Standard)\nStory Structure: 6-Act Structure\nTheme: 真相\n")
    _write(root, CONTRACT, encode({
        "era": "当代",
        "canonical_characters": [{"id": "CH001", "name": "沈宁", "aliases": ["阿宁"]},
                                 {"id": "CH002", "name": "陈放", "aliases": []}],
        "canonical_locations": [{"id": "LOC001", "name": "白螺里", "aliases": []}],
        "schema_version": 4,
    }))
    _write(root, BODY, "# 世界\n白螺里的住房规则。")
    _write(root, "story/structure/6-act_structure_beginning.md", "# 开端\n沈宁收到一封信。")
    _write(root, "story/planning/chapter_outlines/chapter_outlines_6-act_structure_beginning.md",
           "### 第 1 章：信\n* **涉及人物**：沈宁、陆巡、宏升死士。\n* **具体地点**：白螺里·七号楼。\n\n"
           "### 第 2 章：钥匙\n* **涉及人物**：陈放。\n* **具体地点**：海港市·码头。\n\n"
           "### 第 3 章：门\n* **涉及人物**：阿宁。\n* **具体地点**：白螺里·旧洗衣房。\n")
    plans = "story/planning/detailed_scene_plans/scenes_6-act_structure_beginning_ch{}.md"
    _write(root, plans.format(1), _plan("信", "楼梯"))
    _write(root, plans.format(2), _plan("钥匙", "码头", "回家"))
    _write(root, plans.format(3), _plan("门", "洗衣房"))
    contracts = "system/story_ledgers/chapter_contracts/chapter_{}.json"
    _write(root, contracts.format(1), _contract(
        1,
        scene_boundaries=[{"scene_index": 1, "location": "七号楼"}, {"scene_index": 2, "location": "楼梯"}],
        facts_added=[{"id": "F-001-01", "fact": "钥匙", "value": "在信封里"}],
        plot_thread_updates=[{"id": "PT-001-01", "thread": "谁寄的信", "status": "open", "deadline_chapter": 3}],
    ))
    _write(root, contracts.format(2), _contract(
        2,
        scene_boundaries=["场景 1：钥匙", "scene_2: 码头", "scene_3: 回家"],
        facts_confirmed=[{"id": "F-001-01", "fact": "", "value": "在信封里"}],
    ))
    _write(root, contracts.format(3), _contract(
        3,
        scene_boundaries=[{"scene_number": 1, "end_state": "沈宁推开门"},
                          {"scene_number": 2, "end_state": "洗衣房的灯灭了"}],
        plot_thread_updates=[{"id": "PT-001-01", "thread": "谁寄的信", "status": "closed"}],
    ))
    return root


def _collect(root):
    with contextlib.redirect_stdout(io.StringIO()):
        return check_planning.collect(str(root))


def test_the_check_reports_what_planning_left_behind(tmp_path):
    report = _collect(_work(tmp_path))
    by_chapter = {item.chapter: item for item in report.chapters}
    assert [by_chapter[n].scenes for n in (1, 2, 3)] == [2, 3, 2]
    # 场景边界：第 1 章写在 scene_index 里、第 2 章开头一条写成「场景 1：」，都认得出。
    assert (by_chapter[1].should_block_first, by_chapter[1].blocked_first) == (1, 1)
    assert (by_chapter[2].should_block_first, by_chapter[2].blocked_first) == (2, 2)
    assert by_chapter[2].recognised == 3
    assert by_chapter[3].with_end_state == 2
    # 重规划第 2 章时，第 1 章就埋下的悬念写成「第 1 章已埋下」，不再是「本章必须埋下」。
    assert by_chapter[2].stale_obligations == []
    assert "悬念 PT-001-01（谁寄的信）：第 1 章已埋下，第 3 章要了结" in by_chapter[2].prompt
    # 第 2 章引用事实时没写名字，重规划第 1 章的提示词取第 1 章引入时写的名字。
    assert by_chapter[1].empty_names == []
    assert "事实 F-001-01（钥匙）：第 2 章要引用它，本章必须引入" in by_chapter[1].prompt
    # 规划提示词按块量过；材料里没有截断。
    assert by_chapter[2].blocks["本章大纲"] > 0 and by_chapter[2].truncated_blocks == []
    assert set(report.outline_prompts) == {"Beginning"}
    # 身份称呼不算人名；「白螺里·七号楼」的城市登记过，「海港市」没有。
    assert report.outline_names.people == ["陆巡"]
    assert report.outline_names.places == ["海港市"]
    assert report.defects == []

    text = check_planning.render(report, chapters=True)
    assert "写第 1 场时应拦 / 实际拦的后面场次边界：4 / 4" in text
    assert "第 2 章｜3｜3/3/0｜2/2" in text


def test_the_check_never_calls_the_model(tmp_path, monkeypatch, capsys):
    def refuse(*_args, **_kwargs):
        raise AssertionError("规划检查不该调用模型")

    for module in list(sys.modules.values()):
        for name in ("send_prompt", "send_conversation"):
            if callable(getattr(module, name, None)):
                monkeypatch.setattr(module, name, refuse, raising=False)
    assert check_planning.main([str(_work(tmp_path))]) == 0
    assert "【场景边界】" in capsys.readouterr().out


def test_the_check_leaves_every_file_in_the_work_untouched(tmp_path):
    root = _work(tmp_path / "work")

    def snapshot():
        return {
            os.path.relpath(os.path.join(folder, name), root): os.stat(os.path.join(folder, name)).st_mtime_ns
            for folder, dirs, files in os.walk(root)
            for name in files + dirs
        }

    before = snapshot()
    with contextlib.redirect_stdout(io.StringIO()):
        assert check_planning.main([str(root)]) == 0
    assert snapshot() == before


def test_the_check_builds_the_prompts_the_planning_button_sends(tmp_path, monkeypatch):
    root = _work(tmp_path / "work")
    report = _collect(root)
    copy = tmp_path / "copy"
    shutil.copytree(root, copy)
    sent = []
    monkeypatch.setattr(scene_pipeline, "send_prompt", lambda prompt, **_: sent.append(prompt) or "")
    monkeypatch.setattr(ScenePipeline, "_has_usable_scene_plan", staticmethod(lambda *a, **k: False))
    params = load_stage_parameters(str(copy))
    host = GenerationHost(make_context(str(copy), "fake", params), logging.getLogger("test"))
    with contextlib.redirect_stdout(io.StringIO()), pytest.raises(Exception):
        ScenePipeline(host)._plan_long_form_scenes(host.context)  # 三章都没过，最后报错
    first = {}
    for prompt in sent:
        first.setdefault(prompt.splitlines()[0], prompt)
    assert len(first) == 3
    for item in report.chapters:
        key = next(line for line in first if f"第 {item.chapter} 章规划场景" in line)
        assert first[key] == item.prompt


def test_the_read_only_graph_creates_nothing(tmp_path):
    context = NarrativeGraphManager.reader(str(tmp_path)).planning_context(3, {})
    assert context["active_threads"] == [] and context["graph_revision"] == 0
    assert list(tmp_path.iterdir()) == []


def test_prompt_blocks_add_up_to_the_prompt():
    contract = contract_output_instructions(2, {"open_plot_threads": []})
    blocks = contract_instruction_blocks(2, {"open_plot_threads": []})
    assert "".join(text for _label, text in blocks).strip() == contract
    assert [label for label, _ in blocks][:3] == ["契约格式与填写规则", "仍未了结的悬念", "后续章节依赖本章"]

    outline = outline_prompt(
        genre_label="悬疑", structure_name="6-Act Structure", sections=["Beginning"],
        section_name="Beginning", section_content="沈宁收到一封信。", start_chapter=1,
        story_params={}, location_guidance=["地点只使用世界观已经定义的城市。"],
    )
    assert "".join(text for _label, text in outline.blocks) == outline
    short = short_story_prompt(
        genre_label="悬疑", novel_title="雨夜", structure_name="3-Act Structure", story_params={},
        plot="雨夜回到旧楼。", location_guidance=[], lore_content="当代港城。", contract_text="契约",
    )
    assert "".join(text for _label, text in short.blocks) == short
    assert short.blocks[-1] == ("契约格式说明", "契约")


def test_names_are_read_from_the_people_and_place_lines():
    plan = ("* **出场人物**：阿宁、宏升巡逻清道夫（两名重装守卫）、陆巡。\n"
            "* **环境与具体地点**：海港市·码头、自动化门吊阴影区。海面上起了雾。")
    people, places = listed_names(plan)
    assert people == ["阿宁", "宏升巡逻清道夫", "陆巡"]
    assert places == ["海港市·码头", "自动化门吊阴影区"]
    registry = {"canonical_characters": [{"name": "沈宁", "aliases": ["阿宁"]}],
                "canonical_locations": [{"name": "白螺里"}]}
    found = unregistered_names(plan, registry)
    assert (found.people, found.places) == (["陆巡"], ["海港市"])
    assert unregistered_names(plan, {}).people == []


def test_the_check_counts_obligations_with_or_without_a_name():
    prompt = ("- 悬念 PT-003-01：第 9 章要了结它，本章必须埋下\n"
              "- 事实 F-001-01（钥匙）：第 9 章要引用它，本章必须引入\n"
              "- 事实 F-002-01（）：第 9 章要引用它，本章必须引入\n")
    assert [record for _kind, record, _verb in check_planning._OBLIGATION.findall(prompt)] == [
        "PT-003-01", "F-001-01", "F-002-01"]
    assert check_planning._EMPTY_NAME.findall(prompt) == ["PT-003-01", "F-002-01"]
