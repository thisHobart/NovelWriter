"""Tests for the shared scene prompt builder used by both writing paths."""

import pytest

from core.generation.domain_profiles import (
    GENERAL,
    HORROR,
    LEGAL_SUSPENSE,
    resolve_domain_profile,
)
from core.generation.prompt_context import CHINESE_PROSE_REQUIREMENTS
from core.generation.scene_prompt import (
    BASE_SCENE_REQUIREMENTS,
    build_scene_prompt,
    scene_prompt_filename,
)


def _prompt(**overrides):
    kwargs = dict(
        scene_plan="### 场景 1：质证\n律师核对门禁记录。",
        scene_number=1,
        parameters={"Genre": "Mystery", "Subgenre": "Legal Thriller", "Story Length": "Novel (Standard)"},
        lore="圣兰卡市采用统一的刑事诉讼规则。",
        character_roster="主要人物：林衡",
        faction_summary="主要势力：鉴证科",
        profile=LEGAL_SUSPENSE,
        chapter_number=3,
        structure_name="3-Act Structure",
        section_name="Rising Action",
    )
    kwargs.update(overrides)
    return build_scene_prompt(**kwargs)


def test_prompt_carries_context_and_base_requirements():
    prompt = _prompt()

    assert "第 3 章中的一个场景" in prompt
    assert "圣兰卡市采用统一的刑事诉讼规则" in prompt
    assert "主要人物：林衡" in prompt
    assert "主要势力：鉴证科" in prompt
    assert "律师核对门禁记录" in prompt
    for requirement in BASE_SCENE_REQUIREMENTS:
        assert requirement in prompt
    for requirement in CHINESE_PROSE_REQUIREMENTS:
        assert requirement in prompt


def test_domain_rules_come_from_the_profile():
    legal = _prompt(profile=LEGAL_SUSPENSE)
    horror = _prompt(profile=HORROR)

    for rule in LEGAL_SUSPENSE.scene_writing_rules:
        assert rule in legal
        assert rule not in horror
    for rule in HORROR.scene_writing_rules:
        assert rule in horror
        assert rule not in legal


def test_general_profile_contributes_no_domain_rules():
    prompt = _prompt(profile=GENERAL)

    assert GENERAL.scene_writing_rules == ()
    for rule in LEGAL_SUSPENSE.scene_writing_rules:
        assert rule not in prompt
    # 通用要求仍然齐备。
    for requirement in BASE_SCENE_REQUIREMENTS:
        assert requirement in prompt


def test_short_story_prompt_drops_chapter_framing():
    prompt = _prompt(
        chapter_number=None,
        section_name="",
        novel_title="沉默的证词",
        profile=resolve_domain_profile({"Genre": "Mystery", "Subgenre": "Legal Thriller"}),
    )

    assert "短篇小说" in prompt
    assert "沉默的证词" in prompt
    assert "第 3 章" not in prompt
    assert "不要写其他场景，也不要概括整个故事。" in prompt


def test_continuity_sections_appear_only_when_supplied():
    without = _prompt()
    assert "本章质量契约" not in without
    assert "上一场或上一章的已验收结尾" not in without
    assert "下一场边界" not in without

    with_continuity = _prompt(
        contract={"core_question": "收据为何晚了三十二分钟？"},
        previous_scene_tail="上一场结尾：证人交出收据。",
        next_scene_plan="### 场景 2：休庭",
    )
    assert "本章质量契约" in with_continuity
    assert "收据为何晚了三十二分钟？" in with_continuity
    assert "证人交出收据" in with_continuity
    assert "### 场景 2：休庭" in with_continuity


@pytest.mark.parametrize(
    "chapter_number,expected",
    [
        (None, "short_story_scene_2_prompt"),
        (5, "write_chapter_5_scene_2_prompt"),
    ],
)
def test_prompt_archive_filenames_are_unchanged(chapter_number, expected):
    assert scene_prompt_filename(2, chapter_number) == expected


def test_hand_off_requirements_appear_as_hard_rules_for_the_first_scene():
    prompt = _prompt(
        continuity_rules=[
            "本场开头必须直接承接上一章结尾的这件事：林衡把收据推回桌面。",
            "以下地点在前面几章刚建立过，禁止从零重新描写环境：讯问室。",
        ]
    )

    assert "本章开场的硬性衔接要求" in prompt
    assert "林衡把收据推回桌面" in prompt
    assert "禁止从零重新描写环境" in prompt


def test_a_scene_without_hand_off_rules_gets_no_extra_section():
    assert "本章开场的硬性衔接要求" not in _prompt()
    assert "本章开场的硬性衔接要求" not in _prompt(continuity_rules=["", "  "])


NEXT_SCENE = """### 场景 2：寒霜绝域
* **环境与具体地点**：底层保税冷链舱，舱内温度骤降至零下二十摄氏度。
* **出场人物**：梁浩、专案小队特战队员。
* **关键行动/事件**：
  1. 梁浩剪断集装箱铅封，露出纵向排列的低温休眠舱。
"""


def test_the_writer_is_told_where_to_stop_but_not_what_happens_next():
    """把下一场整段发过去等于先递材料、再请模型别用，而它会用。"""
    prompt = _prompt(next_scene_plan=NEXT_SCENE)

    assert "### 场景 2：寒霜绝域" in prompt
    assert "本场必须在下一场开始之前收束" in prompt
    # 下一场的环境、人物和事件表一个都不该出现在写作提示词里。
    assert "零下二十摄氏度" not in prompt
    assert "专案小队特战队员" not in prompt
    assert "低温休眠舱" not in prompt


def test_a_final_scene_gets_no_stopping_point_section():
    assert "本场必须在下一场开始之前收束" not in _prompt(next_scene_plan="")


STAGED_CONTRACT = {
    "chapter": 3,
    "scene_boundaries": [
        "scene_1: 蒋静登庭指认",
        "scene_2: 郑娜敏签发逮捕令",
        "scene_3: 法官当庭裁决梁浩无罪",
    ],
}


def test_later_scenes_are_listed_as_forbidden_not_as_something_to_deliver():
    """删掉它们越界更严重——那几行同时在当栅栏用，要改的是名义不是有无。"""
    prompt = _prompt(scene_number=1, contract=STAGED_CONTRACT)

    assert "以下内容属于本章后面的场次，本场一个字都不许碰" in prompt
    assert "scene_2: 郑娜敏签发逮捕令" in prompt
    assert "scene_3: 法官当庭裁决梁浩无罪" in prompt
    # 后面场次不能留在写着「必须兑现」的那一份契约里。
    contract_block = prompt.split("本章质量契约")[1].split("以下内容属于本章后面的场次")[0]
    assert "scene_1: 蒋静登庭指认" in contract_block
    assert "scene_2" not in contract_block
    assert "scene_3" not in contract_block


def test_the_last_scene_has_nothing_left_to_forbid():
    prompt = _prompt(scene_number=3, contract=STAGED_CONTRACT)
    assert "以下内容属于本章后面的场次" not in prompt
    assert "scene_3: 法官当庭裁决梁浩无罪" in prompt
