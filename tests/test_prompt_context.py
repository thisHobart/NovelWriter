"""Regression tests for shared story prompt constraints."""

import inspect

from core.generation.prompt_context import (
    CHINESE_PROSE_REQUIREMENTS,
    analyze_chinese_prose_style,
    build_location_guidance,
    find_scene_world_conflicts,
    format_faction_summary,
    format_genre_label,
    is_legal_suspense,
)
from core.gui.chapter_writing import ChapterWriting
from core.gui.scene_plan import ScenePlanning


MYSTERY_PARAMS = {"Genre": "Mystery", "Subgenre": "Legal Thriller"}


def test_mystery_identity_and_location_rules_are_not_scifi():
    assert format_genre_label(MYSTERY_PARAMS) == "悬疑推理（法律惊悚）"
    guidance = "\n".join(build_location_guidance(MYSTERY_PARAMS))
    assert "不得把城市改写成星球" in guidance
    assert "城市、街区、建筑和房间" in guidance


def test_legal_suspense_detection_supports_saved_and_localized_values():
    assert is_legal_suspense(MYSTERY_PARAMS)
    assert is_legal_suspense({"Genre": "悬疑推理", "Subgenre": "法律惊悚"})
    assert not is_legal_suspense({"Genre": "Mystery", "Subgenre": "Cozy Mystery"})


def test_scifi_location_rules_still_allow_planets():
    guidance = "\n".join(build_location_guidance({"Genre": "Sci-Fi"}))
    assert "行星、城市" in guidance
    assert "不得把城市改写成星球" not in guidance


def test_scene_conflicts_only_flag_additions_absent_from_lore():
    conflicts = find_scene_world_conflicts(
        "地点：圣兰卡星；交通：悬浮车",
        "圣兰卡是一座沿海城市。",
        MYSTERY_PARAMS,
    )
    assert conflicts == ["悬浮车"] or conflicts == ["星球", "悬浮车"]
    assert "行星" not in conflicts


def test_current_faction_schema_formats_without_empty_names():
    summary = format_faction_summary([
        {
            "name": "市检察院",
            "description": "负责重大案件公诉。",
            "goals": ["维护证据完整性"],
        }
    ])
    assert "势力名称：市检察院" in summary
    assert "简介：负责重大案件公诉。" in summary
    assert "势力名称：无" not in summary


def test_gui_generation_sources_have_no_scifi_prompt_literals():
    source = inspect.getsource(ScenePlanning) + inspect.getsource(ChapterWriting)
    assert "请为科幻小说" not in source
    assert "请撰写科幻" not in source
    assert "地点（包括行星）" not in source
    assert "环境（行星" not in source


def test_chinese_prose_rules_cover_style_and_technical_accuracy():
    rules = "\n".join(CHINESE_PROSE_REQUIREMENTS)
    assert "自然、克制的现代中文" in rules
    assert "避免连续堆叠形容词" in rules
    assert "法律、法医和技术术语必须准确" in rules


def test_style_analyzer_flags_repetition_and_long_sentences():
    prose = "冰冷。冰冷。冰冷。" + ("这是一个塞入了过多动作和解释、没有及时停顿的句子" * 5) + "。"
    warnings = analyze_chinese_prose_style(prose)
    assert any("冰冷×3" in warning for warning in warnings)
    assert any("长句比例过高" in warning for warning in warnings)
