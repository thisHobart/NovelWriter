# -*- coding: utf-8 -*-
"""「规划场景」的材料：按重要性排成几块，整块取舍，不截断，不带内部字段。"""
import contextlib
import io
import json
import logging
import os
import re
import sys

import pytest

import core.generation.scene_pipeline as scene_pipeline
from core.generation.lore_repository import BODY, CHARACTERS, CONTRACT, FACTIONS, encode
from core.generation.planning_contract import graph_context_text
from core.generation.planning_prompt import (
    MISSING_LORE,
    book_outlines,
    contract_instructions,
    planning_material,
)
from core.generation.scene_pipeline import ScenePipeline
from core.generation.stage_context import make_context
from core.generation.stage_pipeline import GenerationHost, load_stage_parameters

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
import check_planning  # noqa: E402

ORDER = [
    "本章大纲（必须遵守）",
    "名称与规则登记表（必须遵守）",
    "本章人物（必须遵守）",
    "相关势力（必须遵守）",
    "上一章结束时的状态（必须遵守）",
    "仍未了结的悬念和后续章节依赖（必须遵守）",
    "既有事实和时间事件（必须遵守）",
    "前后相邻两章的大纲（参考）",
    "世界观正文（参考）",
]
OUTLINES = "story/planning/chapter_outlines/chapter_outlines_6-act_structure_beginning.md"


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _contract(chapter, **fields):
    return json.dumps({"chapter": chapter, "origin": "scene_planning", "schema_version": 2,
                       "chapter_function": ["advance", "reveal", "relationship"][chapter % 3], **fields},
                      ensure_ascii=False)


def _work(root, *, lore=True):
    """三章：第 2 章点名陈放、码头；第 3 章点名阿宁（沈宁的别名）和旧洗衣房。"""
    _write(root, "system/parameters.txt",
           "Genre: 悬疑\nStory Length: Novel (Standard)\nStory Structure: 6-Act Structure\nTheme: 真相\n")
    if lore:
        _write(root, CONTRACT, encode({
            "era": "当代",
            "canonical_characters": [{"id": "CH001", "name": "沈宁", "aliases": ["阿宁"], "role": "主角"},
                                     {"id": "CH002", "name": "陈放", "aliases": [], "role": "维修工"},
                                     {"id": "CH003", "name": "罗静", "aliases": [], "role": "项目经理"}],
            "canonical_factions": [{"id": "FAC001", "name": "长栖住房租赁公司", "aliases": ["长栖"]}],
            "canonical_locations": [{"id": "LOC001", "name": "白螺里", "aliases": []},
                                    {"id": "LOC002", "name": "白螺里旧洗衣房", "aliases": ["旧洗衣房"]},
                                    {"id": "LOC003", "name": "南湾码头", "aliases": ["码头"]}],
            "schema_version": 4,
        }))
        _write(root, CHARACTERS, encode({"characters": [
            {"name": "沈宁", "role": "protagonist", "goal": "查清信是谁寄的", "arc": "第六幕拿到钥匙"},
            {"name": "陈放", "role": "deuteragonist", "faction": "长栖住房租赁公司", "goal": "保住名额"},
            {"name": "罗静", "role": "antagonist", "goal": "按期换签"},
        ]}))
        _write(root, FACTIONS, encode([
            {"name": "长栖住房租赁公司", "goal": "保住经营权", "territory": "南湾码头"},
        ]))
        _write(root, BODY, "# 世界\n白螺里的住房规则。")
    _write(root, "story/structure/6-act_structure_beginning.md", "# 开端\n沈宁收到一封信。")
    _write(root, OUTLINES,
           "### 第 1 章：信\n* **涉及人物**：沈宁。\n* **具体地点**：白螺里。\n\n"
           "### 第 2 章：钥匙\n* **涉及人物**：陈放。\n* **具体地点**：码头。\n\n"
           "### 第 3 章：门\n* **涉及人物**：阿宁。\n* **具体地点**：旧洗衣房。\n")
    plans = "story/planning/detailed_scene_plans/scenes_6-act_structure_beginning_ch{}.md"
    for number in (1, 2, 3):
        _write(root, plans.format(number), "### 场景 1：开场\n沈宁出门。\n\n### 场景 2：收尾\n她回家。")
    contracts = "system/story_ledgers/chapter_contracts/chapter_{}.json"
    _write(root, contracts.format(1), _contract(
        1,
        facts_added=[{"id": "F-001-01", "fact": "钥匙", "value": "在沈宁的信封里", "first_stated_at": "scene_1"},
                     {"id": "F-001-02", "fact": "罗静的工牌", "value": "落在三号楼", "first_stated_at": "scene_2"}],
        timeline_events=[{"id": "TL-001-01", "event": "沈宁收到信", "time": "周一 08:00",
                          "location_id": "白螺里", "scene": "scene_1"}],
        plot_thread_updates=[{"id": "PT-001-01", "thread": "谁寄的信", "status": "open", "deadline_chapter": 3}],
    ))
    _write(root, contracts.format(2), _contract(
        2,
        facts_added=[{"id": "F-002-01", "fact": "船期", "value": "周五", "first_stated_at": "scene_1"}],
        timeline_events=[{"id": "TL-002-01", "event": "陈放去码头", "time": "周二 09:00",
                          "location_id": "南湾码头", "scene": "scene_1"}],
    ))
    _write(root, contracts.format(3), _contract(
        3,
        facts_confirmed=[{"id": "F-001-02", "fact": "罗静的工牌", "value": "落在三号楼"}],
        plot_thread_updates=[{"id": "PT-001-01", "thread": "谁寄的信", "status": "closed"}],
    ))
    return root


def _quiet(call, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return call(*args, **kwargs)


def _prompts(root):
    report = _quiet(check_planning.collect, str(root))
    return report, {item.chapter: item for item in report.chapters}


def _material(root, chapter, **kwargs):
    outlines = _quiet(book_outlines, str(root), "6-Act Structure", ["Beginning"],
                      str(root / "story/planning/chapter_outlines"))
    contract = _quiet(contract_instructions, str(root), chapter, {})
    return _quiet(planning_material, str(root), chapter, outlines, contract, lore_content=MISSING_LORE, **kwargs)


def _section(text, label):
    """材料里一块的正文（到下一个二级标题为止）。"""
    match = re.search(r"^## " + re.escape(label) + r"\n(.*?)(?=^## |\Z)", text, flags=re.M | re.S)
    return match.group(1) if match else ""


def test_the_material_comes_in_order_with_its_authority(tmp_path):
    _report, by_chapter = _prompts(_work(tmp_path))
    prompt = by_chapter[2].prompt
    headings = [line for line in re.findall(r"^## (.+)$", prompt, flags=re.M) if line in ORDER]
    assert headings == ORDER
    outline = _section(prompt, ORDER[0])
    assert "第 2 章：钥匙" in outline and "第 1 章：信" not in outline and "第 3 章：门" not in outline
    neighbours = _section(prompt, "前后相邻两章的大纲（参考）")
    assert "第 1 章：信" in neighbours and "第 3 章：门" in neighbours
    # 材料在前，要求和契约格式在后；契约格式里才有英文字段名。
    assert prompt.index("## 世界观正文（参考）") < prompt.index("请把「本章大纲」扩展为详细场景")
    assert prompt.index("请把「本章大纲」扩展为详细场景") < prompt.index("<!-- CHAPTER_CONTRACT_START -->")


def test_nothing_is_cut_and_no_internal_field_reaches_the_model(tmp_path):
    _report, by_chapter = _prompts(_work(tmp_path))
    for item in by_chapter.values():
        assert item.truncated_blocks == [] and item.internal_fields == [], item.chapter
        assert '"facts":' not in item.prompt and "_context_meta" not in item.prompt
    facts = _section(by_chapter[3].prompt, "既有事实和时间事件（必须遵守）")
    assert "- F-001-01｜钥匙｜在沈宁的信封里" in facts
    assert "- TL-001-01｜周一 08:00｜白螺里｜沈宁收到信" in facts
    assert "first_stated_at" not in facts and "scene_1" not in facts


def test_people_the_outline_names_get_their_cards(tmp_path):
    report, by_chapter = _prompts(_work(tmp_path))
    people = _section(by_chapter[2].prompt, "本章人物（必须遵守）")
    assert "### 陈放" in people and "### 沈宁" not in people
    # 成长方向写的是后面的事，不带；别名「阿宁」认作沈宁。
    people = _section(by_chapter[3].prompt, "本章人物（必须遵守）")
    assert "### 沈宁" in people and "第六幕" not in people
    factions = _section(by_chapter[2].prompt, "相关势力（必须遵守）")
    assert "### 长栖住房租赁公司" in factions
    assert all(item.missing_cards == [] for item in report.chapters)
    assert by_chapter[2].material_omitted == []


def test_facts_are_picked_by_id_and_by_the_names_the_outline_mentions(tmp_path):
    root = _work(tmp_path)
    facts = _section(_material(root, 3), "既有事实和时间事件（必须遵守）")
    assert "F-001-01" in facts                 # 点到沈宁
    assert "F-002-01" in facts and "TL-002-01" in facts   # 上一章记下的
    assert "F-001-02" not in facts             # 罗静的事，第 3 章没点到她
    assert "另有 1 条事实没有列出" in facts
    assert "PT-001-01" not in facts            # 还没了结（第 3 章才了结），不在已了结里
    # 重规划第 2 章：第 3 章要引用 F-001-02，按编号带上，哪怕第 2 章没点到罗静。
    assert "F-001-02" in _section(_material(root, 2), "既有事实和时间事件（必须遵守）")


def test_over_budget_whole_blocks_go_from_the_end_and_are_listed(tmp_path):
    material = _material(_work(tmp_path), 2, max_chars=10)
    assert [label for label in material.kept] == [
        "本章大纲", "名称与规则登记表", "本章人物", "仍未了结的悬念和后续章节依赖"]
    assert list(material.omitted) == [
        "相关势力", "上一章结束时的状态", "既有事实和时间事件", "前后相邻两章的大纲", "世界观正文"]
    listed = _section(material, "篇幅放不下、这次没带的材料")
    assert all(f"- {label}（" in listed for label in material.omitted)
    assert "".join(text for _label, text in material.blocks) == material


def test_the_planning_material_works_without_setting_files(tmp_path):
    root = _work(tmp_path, lore=False)
    material = _material(root, 2)
    assert "## 本章大纲（必须遵守）" in material and "名称与规则登记表" not in material
    assert "## 世界观正文（参考）\n" + MISSING_LORE in material
    _report, by_chapter = _prompts(root)
    assert "## 本章大纲（必须遵守）" in by_chapter[2].prompt


def test_the_run_log_says_which_blocks_were_kept_and_dropped(tmp_path, monkeypatch):
    root = _work(tmp_path)
    for number in (1, 2, 3):
        os.remove(root / f"story/planning/detailed_scene_plans/scenes_6-act_structure_beginning_ch{number}.md")
    monkeypatch.setattr(scene_pipeline, "send_prompt", lambda prompt, **_: "")
    records = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("test_planning_material")
    logger.setLevel(logging.INFO)
    logger.addHandler(Keep())
    host = GenerationHost(make_context(str(root), "fake", load_stage_parameters(str(root))), logger)
    with contextlib.redirect_stdout(io.StringIO()), pytest.raises(Exception):
        ScenePipeline(host)._plan_long_form_scenes(host.context)
    lines = [line for line in records if line.startswith("第 2 章规划材料")]
    assert lines and "带了：本章大纲、名称与规则登记表、本章人物" in lines[0]
    assert "篇幅放不下没带：无" in lines[0]


def test_the_narrative_graph_is_listed_in_chinese_without_program_fields():
    context = {
        "active_threads": [{"id": "T001", "type": "thread", "label": "谁寄的信",
                            "planned_resolve_chapter": 5, "state": {"thread_status": "open"}}],
        "blocked_reveals": [{"id": "R002", "label": "凶手身份"}],
        "available_clues": [{"id": "C003", "label": "湿脚印", "available_action": "reuse"}],
        "graph_revision": 3, "ledger_revision": 7,
        "warnings": [{"node_id": "T001", "message": "重要情节线超过 max_silent_chapters 未推进"}],
    }
    text = graph_context_text(context)
    assert "- 已经打开的情节线：T001（谁寄的信，计划第 5 章了结）" in text
    assert "- 还不能揭示的（绝对不能选）：R002（凶手身份）" in text
    assert "- 可以用的线索：C003（湿脚印，读者已经见过）" in text
    for word in ("revision", "thread_status", "max_silent_chapters", "{"):
        assert word not in text
    # 一个节点都没有时整块不出现，由「叙事图规则」说明这些字段留空。
    assert graph_context_text({"active_threads": [], "graph_revision": 0, "ledger_revision": 25}) == ""
