# -*- coding: utf-8 -*-
"""发给模型的内容只用中文：要求写中文，小标题不用英文文件名，人物和势力的取值先换成中文。

字段名、编号和固定取值（role、protagonist、open 这类）是给程序读回结果用的，仍是英文。
"""
import re

from core.config.story_options import act_file_stem, act_for_file
from core.generation import ai_helper
from core.generation.lore_repository import BODY, CHARACTERS, CONTRACT, FACTIONS, encode, lore_context
from core.generation.prompt_context import format_faction_summary
from core.localization import structure_file_label


def test_every_api_call_asks_for_chinese_only(monkeypatch):
    sent = []
    monkeypatch.setattr(ai_helper, "get_backend", lambda: "api")
    monkeypatch.setattr(ai_helper.api_providers, "send_messages",
                        lambda model, messages, tokens: sent.append(messages) or "好")
    assert ai_helper.send_prompt("写一段开场。", model=ai_helper.DEFAULT_API_MODEL) == "好"
    system = sent[0][0]
    assert system["role"] == "system"
    assert "一律用简体中文写" in system["content"]
    assert "不要在中文后面用括号补英文或拼音" in system["content"]
    assert "字段名、编号、固定取值和标记照题目给的模板原样写" in system["content"]
    assert sent[0][1] == {"role": "user", "content": "写一段开场。"}


def test_channels_without_a_system_message_put_the_rule_in_front(monkeypatch):
    sent = []
    monkeypatch.setattr(ai_helper, "get_backend", lambda: "codex")
    monkeypatch.setattr(ai_helper, "is_initialized", lambda: True)
    monkeypatch.setattr(ai_helper, "llm_send_prompt", lambda prompt: sent.append(prompt) or "好")
    ai_helper.send_prompt("写一段开场。")
    assert sent == [ai_helper.LANGUAGE_RULE + "\n\n写一段开场。"]


def test_world_setting_headings_are_chinese_names_not_file_paths(tmp_path):
    files = {
        BODY: "# 世界\n白螺里。",
        CHARACTERS: encode({"characters": [{"name": "沈宁", "role": "protagonist"}]}),
        FACTIONS: encode([{"name": "住建局"}]),
        CONTRACT: encode({"canonical_characters": [{"name": "沈宁"}]}),
        "story/lore/background_protagonist_沈宁.md": "沈宁的背景",
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    context = lore_context(tmp_path)
    headings = re.findall(r"^### (.+)$", context, flags=re.M)
    assert headings == ["名称与规则登记表", "世界观正文", "人物资料", "势力资料", "人物背景 · 主角 · 沈宁"]
    assert "story/lore" not in context


def test_faction_summary_translates_known_values_and_names_the_place():
    summary = format_faction_summary([
        {"name": "海陵刑侦支队", "type": "Police Department", "goal": "查清旧案"},
        {"name": "住建局", "territory": {"name": "白螺里", "kind": "街区"}},
    ])
    assert "类型：警察部门" in summary
    assert "管辖或活动范围：白螺里（街区）" in summary
    assert "'name'" not in summary and "kind" not in summary


def test_structure_files_are_named_by_act_in_chinese():
    assert structure_file_label("story/structure/6-act_structure_first_climax.md") == "故事结构 · 第一次高潮"
    assert structure_file_label(
        "story/planning/chapter_outlines/chapter_outlines_6-act_structure_resolution.md") == "章节大纲 · 结局"
    assert structure_file_label("story/structure/3-act_structure_act_1_setup.md") == "故事结构 · 铺垫"
    assert structure_file_label("story/structure/faction_arcs.md") == "势力弧光"


def test_act_file_names_match_what_the_structure_stage_writes():
    # 和故事结构阶段写文件的拼法一致，界面和提示词才认得出旧作品里的每一幕。
    assert act_file_stem("6-Act Structure", "Solution Finding") == "6-act_structure_solution_finding"
    assert act_file_stem("Episodic Structure", "Episode 3: Midpoint/Turning Point") == \
        "episodic_structure_episode_3_midpoint_turning_point"
    assert act_for_file("6-ACT_STRUCTURE_RESOLUTION") == (5, "Resolution")
    assert act_for_file("character_arcs") is None
