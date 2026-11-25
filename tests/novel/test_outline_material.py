# -*- coding: utf-8 -*-
"""「生成章节大纲」带上名称与规则登记表、人物和势力名单、上一幕最后一章的大纲。"""
import contextlib
import io
import logging
import os
import re
import sys

import core.generation.scene_pipeline as scene_pipeline
from core.generation.lore_repository import BODY, CHARACTERS, CONTRACT, FACTIONS, encode
from core.generation.planning_prompt import outline_material, previous_act_ending
from core.generation.scene_pipeline import ScenePipeline
from core.generation.stage_context import make_context
from core.generation.stage_pipeline import GenerationHost, load_stage_parameters

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
import check_planning  # noqa: E402

OUTLINES = "story/planning/chapter_outlines/chapter_outlines_6-act_structure_{}.md"
SECTIONS = ["Beginning", "Rising Action"]


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _work(root, *, lore=True):
    _write(root, "system/parameters.txt",
           "Genre: 悬疑\nStory Length: Novel (Standard)\nStory Structure: 6-Act Structure\nTheme: 真相\n")
    if lore:
        _write(root, CONTRACT, encode({
            "era": "当代",
            "canonical_characters": [{"id": "CH001", "name": "沈宁", "aliases": ["阿宁"], "role": "主角；镜子翻修工"},
                                     {"id": "CH002", "name": "陈放", "aliases": [], "role": "维修工"}],
            "canonical_factions": [{"id": "FAC001", "name": "长栖住房租赁公司", "aliases": ["长栖"]}],
            "canonical_locations": [{"id": "LOC001", "name": "白螺里", "aliases": []}],
            "schema_version": 4,
        }))
        _write(root, CHARACTERS, encode({"characters": [
            {"name": "沈宁", "role": "protagonist", "occupation": "镜子翻修工", "faction": "无势力隶属",
             "goal": "查清信是谁寄的", "arc": "第六幕拿到钥匙"},
            {"name": "陈放", "role": "deuteragonist", "occupation": "维修工", "title": "District Attorney",
             "faction": "长栖住房租赁公司"},
        ]}))
        _write(root, FACTIONS, encode([
            {"name": "长栖住房租赁公司", "type": "公司", "goal": "保住经营权", "territory": "白螺里三号楼"},
        ]))
        _write(root, BODY, "# 世界\n白螺里的住房规则。")
    _write(root, "story/structure/6-act_structure_beginning.md", "# 开端\n沈宁收到一封信。")
    _write(root, "story/structure/6-act_structure_rising_action.md", "# 情节上升\n沈宁去找陈放。")
    _write(root, OUTLINES.format("beginning"),
           "### 第 1 章：信\n* **涉及人物**：沈宁。\n\n### 第 2 章：钥匙\n* **涉及人物**：陈放。\n* 结尾：沈宁把钥匙藏进鞋柜。\n")
    return root


def _quiet(call, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return call(*args, **kwargs)


def _section(text, label):
    match = re.search(r"^## " + re.escape(label) + r"\n(.*?)(?=^## |\Z)", text, flags=re.M | re.S)
    return match.group(1) if match else ""


def test_the_second_act_outline_prompt_gets_the_registry_the_cast_and_the_last_chapter(tmp_path):
    root = _work(tmp_path)
    report = _quiet(check_planning.collect, str(root))
    prompt = report.outline_texts["Rising Action"]
    assert "名称与规则登记表" in report.outline_prompts["Rising Action"]
    registry = _section(prompt, "名称与规则登记表（必须遵守）")
    assert "- 沈宁（又称阿宁）：主角；镜子翻修工" in registry and "schema_version" not in registry
    cast = _section(prompt, "人物和势力名单（必须遵守）")
    assert "- 沈宁：主角；镜子翻修工" in cast
    assert "- 陈放：第二主角；维修工；所属：长栖住房租赁公司" in cast
    assert "- 长栖住房租赁公司：公司；活动地点：白螺里三号楼" in cast
    assert "第六幕" not in prompt and "查清信是谁寄的" not in prompt   # 成长方向和目标不带
    assert "检察官" not in cast and "District" not in cast             # 称号常和职业对不上，不带
    ending = _section(prompt, "上一幕最后一章的大纲（参考）")
    assert ending.startswith("### 第 2 章：钥匙") and "第 1 章：信" not in ending
    # 第一幕前面没有幕；原来的几块都还在。
    first = report.outline_texts["Beginning"]
    assert "上一幕最后一章的大纲" not in first and "名称与规则登记表" in first
    for label in ("任务说明", "这一幕的故事结构", "写法要求", "作品参数", "地点要求", "输出格式"):
        assert label in report.outline_prompts["Rising Action"], label
    assert "登记表里没有的新人物" in prompt
    assert ("带了设定材料的：名称与规则登记表 2 份，人物和势力名单 2 份，上一幕最后一章的大纲 1 份；"
            "篇幅放不下、去掉了块的 0 份") in check_planning.render(report)


def test_the_outline_material_drops_whole_blocks_from_the_end(tmp_path):
    root = _work(tmp_path)
    ending = previous_act_ending(str(root), "6-Act Structure", SECTIONS, "Rising Action",
                                 str(root / "story/planning/chapter_outlines"))
    material = outline_material(str(root), ending, max_chars=10)
    assert list(material.kept) == ["名称与规则登记表"]
    assert list(material.omitted) == ["人物和势力名单", "上一幕最后一章的大纲"]
    assert "## 篇幅放不下、这次没带的材料" in material


def test_an_old_outline_with_second_level_chapter_headings_still_ends_at_level_three(tmp_path):
    root = _work(tmp_path, lore=False)
    _write(root, OUTLINES.format("beginning"),
           "## 第 1 章：信\n沈宁收信。\n\n## 第 2 章：钥匙\n#### 场景\n沈宁藏钥匙。\n")
    ending = previous_act_ending(str(root), "6-Act Structure", SECTIONS, "Rising Action",
                                 str(root / "story/planning/chapter_outlines"))
    text = outline_material(str(root), ending)
    block = _section(text, "上一幕最后一章的大纲（参考）")
    assert block.startswith("### 第 2 章：钥匙") and "##### 场景" in block
    assert previous_act_ending(str(root), "6-Act Structure", SECTIONS, "Beginning",
                               str(root / "story/planning/chapter_outlines")) == ""


def test_the_outline_prompt_works_without_setting_files(tmp_path):
    root = _work(tmp_path, lore=False)
    report = _quiet(check_planning.collect, str(root))
    prompt = report.outline_texts["Rising Action"]
    assert "名称与规则登记表" not in prompt and "人物和势力名单" not in prompt
    assert "## 上一幕最后一章的大纲（参考）" in prompt


def test_the_outline_button_sends_what_the_check_builds(tmp_path, monkeypatch):
    root = _work(tmp_path)
    report = _quiet(check_planning.collect, str(root))
    sent = []
    monkeypatch.setattr(scene_pipeline, "send_prompt", lambda prompt, **_: sent.append(prompt) or "")
    monkeypatch.setattr(scene_pipeline, "show_error", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(scene_pipeline, "show_warning", lambda *a, **k: None, raising=False)
    host = GenerationHost(make_context(str(root), "fake", load_stage_parameters(str(root))),
                          logging.getLogger("test_outline_material"))
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            ScenePipeline(host)._generate_chapter_outline(host.context)
        except Exception:
            pass
    assert sent, "「生成章节大纲」没有发出提示词"
    assert sent[0] == report.outline_texts["Rising Action"]
