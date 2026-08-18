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
