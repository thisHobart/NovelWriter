# -*- coding: utf-8 -*-
"""世界观冲突检查：打回的说法按作品题材说，不再写死「科幻内容」；
样例作品里被误判的「全息」「跃迁」三稿现在不再打回。"""
import contextlib
import inspect
import io
import json
import logging

import core.generation.scene_pipeline as scene_pipeline
from core.generation.planning_contract import CONTRACT_END, CONTRACT_START
from core.generation.prompt_context import find_scene_world_conflicts, world_conflict_message
from core.generation.scene_pipeline import ScenePipeline
from core.generation.stage_context import make_context
from core.generation.stage_pipeline import GenerationHost

MYSTERY = {"Genre": "悬疑", "Quality Loop": "off"}
LORE = "21世纪现代，现实世界当代水平。海陵湾区，临山、汇州、雾平。"

#: 样例作品第 8、10、16 章第一稿里被打回的原句（当时分别报「全息」「跃迁」「跃迁」）。
OLD_FALSE_ALARMS = (
    "  * 顾阳波身着笔挺的深色法医制服作为控方核心专家证人出庭。他调出全息级高精度显微投影，以绝对冷静的逻辑陈述物证闭环。",
    "  * **心理防线重塑与弧光跃迁**：长期折磨梁浩的弑罪内疚与创伤恐惧彻底粉碎，完成了从“逃亡者”到“猎手”的根本性心理蜕变。",
    "  顾阳波从偏执的“复仇猎手”转变为追寻真相的“执灯者”，完成了从被蒙蔽者到破局核心的关键跃迁。",
)


def test_the_three_old_false_alarms_are_no_longer_flagged():
    for line in OLD_FALSE_ALARMS:
        assert find_scene_world_conflicts(line, LORE, MYSTERY) == [], line


def test_real_additions_are_still_caught():
    plan = "梁浩登上开往另一颗星球的星际货船。"
    assert find_scene_world_conflicts(plan, LORE, MYSTERY) == ["星球", "星际"]
    # 世界观里写了的不算；科幻作品不查。
    assert find_scene_world_conflicts(plan, LORE + "星际航线、星球殖民。", MYSTERY) == []
    assert find_scene_world_conflicts(plan, LORE, {"Genre": "科幻"}) == []


def test_the_message_names_the_work_by_its_own_genre():
    assert world_conflict_message(["行星"], {"Genre": "悬疑"}) == "写了这部悬疑小说的世界观里没有的设定：行星"
    assert world_conflict_message(["星际", "虫洞"], {"Genre": "Romance"}) == "写了这部爱情小说的世界观里没有的设定：星际、虫洞"
    assert world_conflict_message(["行星"], {"Genre": "Historical Fiction"}).startswith("写了这部历史小说的")
    # 题材名没有中文对照时不把英文塞进句子。
    assert world_conflict_message(["行星"], {"Genre": "General Fiction"}) == "写了这部小说的世界观里没有的设定：行星"
    assert world_conflict_message(["行星"], {"Genre": "悬疑"}, source="故事结构") == "写了这部悬疑小说的故事结构里没有的设定：行星"
    for genre in ("悬疑", "Romance", "Fantasy", "General Fiction"):
        assert "科幻" not in world_conflict_message(["行星"], {"Genre": genre})


def test_the_planning_step_no_longer_hard_codes_a_genre_in_its_rejections():
    source = inspect.getsource(scene_pipeline)
    assert "科幻内容" not in source and "世界观未定义的内容" not in source


def _host(tmp_path, name):
    return GenerationHost(make_context(str(tmp_path), "fake", {}), logging.getLogger(name))


def test_an_outline_with_an_addition_is_sent_back_in_the_works_own_terms(tmp_path, monkeypatch):
    replies = ["### 第 1 章：信\n* **涉及人物**：沈宁。\n沈宁收到一张星际邮轮的船票。\n",
               "### 第 1 章：信\n* **涉及人物**：沈宁。\n沈宁收到一张渡轮的船票。\n"]
    sent = []
    monkeypatch.setattr(scene_pipeline, "send_prompt", lambda prompt, **_: sent.append(prompt) or replies[len(sent) - 1])
    with contextlib.redirect_stdout(io.StringIO()):
        outline = ScenePipeline(_host(tmp_path, "test_world_conflict.outline"))._generate_valid_outline_response(
            "请生成大纲。", "fake", "Beginning", "# 开端\n沈宁收到一封信。", MYSTERY
        )
    assert "渡轮" in outline and len(sent) == 2
    assert "- 大纲写了这部悬疑小说的故事结构里没有的设定：星际" in sent[1]
    assert "科幻" not in sent[1]


def _scene_reply(text):
    contract = {
        "chapter": 1, "chapter_function": "advance", "core_question": "谁寄的信",
        "scene_boundaries": [{"scene_number": 1, "must_do": ["看信"], "must_not_do": [], "end_state": "沈宁收起信"},
                             {"scene_number": 2, "must_do": ["下楼"], "must_not_do": [], "end_state": "陈放开门"}],
        "facts_added": [], "timeline_events": [], "plot_thread_updates": [],
    }
    return (f"### 场景 1：信\n{text}\n\n### 场景 2：楼梯\n沈宁下楼去找陈放。\n\n"
            f"{CONTRACT_START}\n{json.dumps(contract, ensure_ascii=False)}\n{CONTRACT_END}")


def test_a_scene_plan_with_an_addition_is_sent_back_in_the_works_own_terms(tmp_path, monkeypatch):
    replies = [_scene_reply("沈宁在信里读到另一颗行星的地址。"), _scene_reply("沈宁在信里读到白螺里的地址。")]
    sent = []
    monkeypatch.setattr(scene_pipeline, "send_prompt", lambda prompt, **_: sent.append(prompt) or replies[len(sent) - 1])
    with contextlib.redirect_stdout(io.StringIO()):
        ScenePipeline(_host(tmp_path, "test_world_conflict.scene"))._generate_valid_scene_response(
            "请规划第 1 章。", "fake", 1, LORE, MYSTERY, output_dir=str(tmp_path)
        )
    assert len(sent) == 2
    assert "- 场景规划写了这部悬疑小说的世界观里没有的设定：行星" in sent[1]
    assert "科幻" not in sent[1].split("### 必须全部修复的问题")[-1]
