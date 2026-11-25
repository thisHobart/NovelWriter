# -*- coding: utf-8 -*-
"""写一场戏的设定材料：由代码按本场挑人物、势力，整块取舍。"""
import re

import pytest

import agents.writing.chapter_writing_agent as writing_module
from agents.writing.chapter_writing_agent import ChapterWritingAgent
from core.generation.domain_profiles import resolve_domain_profile
from core.generation.lore_repository import BODY, CHARACTERS, CONTRACT, FACTIONS, encode
from core.generation.scene_material import build_scene_material
from core.generation.scene_prompt import build_scene_prompt


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _work(root):
    _write(root, CONTRACT, encode({
        "era": "当代雨季",
        "world_constraints": ["没有超自然力量"],
        "canonical_characters": [
            {"id": "CH001", "name": "沈宁", "aliases": ["阿宁"], "role": "主角；镜子翻修工"},
            {"id": "CH002", "name": "陈放", "aliases": [], "role": "第二主角；维修工"},
            {"id": "CH003", "name": "罗静", "aliases": [], "role": "对立角色；项目经理"},
            {"id": "CH004", "name": "何秀兰", "aliases": [], "role": "配角；沈宁的母亲"},
        ],
        "canonical_factions": [
            {"id": "FAC001", "name": "白螺区住建局", "aliases": ["住建局"]},
            {"id": "FAC002", "name": "长栖住房租赁公司", "aliases": ["长栖"]},
            {"id": "FAC003", "name": "白螺里住户档案小组", "aliases": ["档案小组"]},
        ],
        "canonical_locations": [
            {"id": "LOC001", "name": "白螺里", "aliases": []},
            {"id": "LOC002", "name": "白螺里七号楼", "aliases": ["七号楼"]},
            {"id": "LOC003", "name": "白螺里旧洗衣房", "aliases": ["旧洗衣房"]},
        ],
        "schema_version": 4,
    }))
    _write(root, CHARACTERS, encode({"characters": [
        {"name": "沈宁", "role": "protagonist", "gender": "女", "faction": "白螺里住户档案小组",
         "goal": "撤掉共同续租确认", "arc": "第六幕拿到独立钥匙"},
        {"name": "陈放", "role": "deuteragonist", "gender": "男", "faction": "无势力隶属",
         "goal": "替她保住名额", "arc": "第五幕学会先问她"},
        {"name": "罗静", "role": "antagonist", "gender": "女", "faction": "长栖住房租赁公司",
         "goal": "按期完成换签"},
        {"name": "何秀兰", "role": "supporting", "gender": "女", "goal": "保住独居租约"},
    ]}))
    _write(root, FACTIONS, encode([
        {"name": "白螺区住建局", "goal": "查清退租回执", "territory": "白螺区住建局三楼受理厅"},
        {"name": "长栖住房租赁公司", "goal": "保住经营权", "territory": "白螺里三号楼长栖租务大厅"},
        {"name": "白螺里住户档案小组", "goal": "重建办理经过", "territory": "白螺里旧洗衣房"},
    ]))
    _write(root, BODY, "# 世界\n白螺里的住房规则。")
    _write(root, "story/lore/background_protagonist_沈宁.md", "## 童年\n" + "沈宁在白螺里长大。" * 40)
    _write(root, "story/lore/background_antagonist_罗静.md", "罗静十年前入职长栖。")
    return root


PLAN = ("### 场景 1：楼梯上的信\n* **环境与具体地点**：白螺里七号楼楼梯。\n"
        "* **出场人物**：阿宁、陈放。\n* **关键行动/事件**：阿宁没有去拿门口的信。")


def _headings(material):
    return re.findall(r"^## (.+)$", material, flags=re.M)


def test_a_scene_gets_cards_only_for_the_people_it_names(tmp_path):
    material = build_scene_material(str(_work(tmp_path)), PLAN)
    assert material.characters == ("沈宁", "陈放")            # 别名「阿宁」认作沈宁
    assert _headings(material) == [
        "名称与规则登记表（必须遵守）", "本场人物（必须遵守）", "相关势力（必须遵守）",
        "世界观正文（必须遵守）", "人物背景 · 主角 · 沈宁（参考）",
    ]
    assert "### 沈宁" in material and "### 陈放" in material and "### 罗静" not in material
    # 没出场的人物只在登记表里留一行名字和身份；背景只带本场人物的。
    assert "- 罗静：对立角色；项目经理" in material
    assert "罗静十年前入职长栖" not in material
    # 卡片写成中文条目；成长方向写的是后面几幕的事，不带。
    assert "目标：撤掉共同续租确认" in material and "角色定位：主角" in material
    assert "第六幕" not in material and "第五幕" not in material
    assert '"role"' not in material and "CH001" not in material and "格式版本" not in material
    assert "\n### 世界\n" in material and "\n#### 童年\n" in material   # 正文、背景自己的标题降两级


def test_factions_come_from_affiliation_names_and_places(tmp_path):
    root = str(_work(tmp_path))
    assert build_scene_material(root, PLAN).factions == ("白螺里住户档案小组",)   # 沈宁所属
    named = PLAN + "\n长栖的人在楼下等。"
    assert build_scene_material(root, named).factions == ("长栖住房租赁公司", "白螺里住户档案小组")
    # 规划只点到地点：地盘就在那儿的势力也带上。「白螺里」不会因为是「白螺里旧洗衣房」的一部分就算进去。
    place = "### 场景 2：底册\n* **环境与具体地点**：旧洗衣房。\n* **出场人物**：陈放。"
    assert build_scene_material(root, place).factions == ("白螺里住户档案小组",)


def test_the_contract_names_people_and_the_first_scene_brings_the_opening_cast(tmp_path):
    root = str(_work(tmp_path))
    contract = {"chapter": 3, "cost_character": "罗静",
                "continuity": {"opening_positions": [{"character": "何秀兰", "location": "七号楼一层"}]}}
    first = build_scene_material(root, PLAN, contract=contract, scene_number=1)
    assert first.characters == ("沈宁", "陈放", "罗静", "何秀兰")
    second = build_scene_material(root, PLAN, contract=contract, scene_number=2)
    assert second.characters == ("沈宁", "陈放", "罗静")


def test_a_plan_naming_nobody_known_falls_back_to_the_lead(tmp_path):
    material = build_scene_material(str(_work(tmp_path)), "### 场景 1：雨夜\n一个陌生人走进巷子。")
    assert material.characters == ("沈宁",)
    assert "场景规划里没有认出登记过的人物，先带主角的资料。" in material


def test_over_budget_drops_whole_blocks_from_the_least_important(tmp_path):
    root = str(_work(tmp_path))
    full = build_scene_material(root, PLAN)
    trimmed = build_scene_material(root, PLAN, max_chars=len(full) - 100)
    assert trimmed.omitted == ("人物背景 · 主角 · 沈宁",)
    assert "沈宁在白螺里长大" not in trimmed
    assert trimmed.endswith("- 人物背景 · 主角 · 沈宁（366 字）")

    tight = build_scene_material(root, PLAN, max_chars=10)
    # 登记表和本场人物篇幅再紧也带；其余整块让出，不在块中间截断。
    assert _headings(tight) == ["名称与规则登记表（必须遵守）", "本场人物（必须遵守）",
                                "篇幅放不下、这次没带的材料"]
    assert tight.omitted == ("相关势力", "世界观正文", "人物背景 · 主角 · 沈宁")


def test_a_work_without_lore_files_gets_no_material(tmp_path):
    assert build_scene_material(str(tmp_path), PLAN) == ""


def test_the_scene_prompt_carries_the_material_instead_of_the_whole_setting(tmp_path):
    material = build_scene_material(str(_work(tmp_path)), PLAN)
    common = dict(scene_plan=PLAN, scene_number=1, parameters={"genre": "爱情"},
                  lore="整份世界设定", character_roster="全部人物名单", faction_summary="全部势力",
                  profile=resolve_domain_profile({"genre": "爱情"}), chapter_number=3)
    prompt = build_scene_prompt(**common, material=material)
    assert material in prompt
    for whole in ("整份世界设定", "全部人物名单", "全部势力", "完整世界观"):
        assert whole not in prompt
    assert prompt.index("## 名称与规则登记表") < prompt.index("## 当前场景描述")
    # 没有材料（旧目录结构的作品、评测脚本）时照旧带整份的三样。
    legacy = build_scene_prompt(**common)
    assert "完整世界观：整份世界设定" in legacy and "全部人物名单" in legacy


def test_the_chapter_writer_hands_each_scene_its_own_material(tmp_path, monkeypatch):
    seen = {}

    class Captured(Exception):
        pass

    def fake_prompt(**kwargs):
        seen.update(kwargs)
        raise Captured

    monkeypatch.setattr(writing_module, "build_scene_prompt", fake_prompt)
    agent = object.__new__(ChapterWritingAgent)
    agent.output_dir = str(_work(tmp_path))
    with pytest.raises(Captured):
        agent._generate_scene_prose(3, 1, PLAN, {"parameters": {}, "lore": "白螺里的住房规则。"},
                                    chapter_contract={"chapter": 3, "cost_character": "罗静"})
    assert seen["material"].characters == ("沈宁", "陈放", "罗静")
