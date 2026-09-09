"""The lore and structure GUI stages must actually enforce their contracts."""

import json

import pytest

from core.generation.design_contract import (
    LORE_CONTRACT_END,
    LORE_CONTRACT_START,
    STRUCTURE_CONTRACT_END,
    STRUCTURE_CONTRACT_START,
)


def test_lore_stage_requests_and_enforces_its_contract():
    """世界观提示词必须带契约要求，且校验失败要重试而不是照单落盘。"""
    import core.generation.lore_pipeline as lore_module

    source = open(lore_module.__file__, encoding="utf-8").read()

    assert "lore_contract_instructions()" in source, "世界观提示词没有要求产出契约"
    assert "generate_with_contract_retry" in source, "世界观没有接入带反馈的重试"
    assert "extract_lore_contract" in source, "世界观没有校验契约"
    assert "lore_contract.json" in source, "规范名登记表没有落盘"
    # 旧的「拿到什么就写什么」不能还留着
    assert "lore_text = send_prompt(prompt, model=selected_model)" not in source


def test_structure_stage_requests_and_enforces_its_contract():
    import core.generation.structure_pipeline as structure_module

    source = open(structure_module.__file__, encoding="utf-8").read()

    assert "structure_contract_instructions(" in source, "结构提示词没有要求产出契约"
    assert "generate_with_contract_retry" in source, "结构没有接入带反馈的重试"
    assert "validate_structure_sequence(section_contracts)" in source, "缺少全书跨段校验"
    assert "open_threads_after(section_contracts)" in source, "未把未了结悬念带给下一段"
    assert "chronology_after(section_contracts)" in source, "未把已登记的时间线带给下一段"
    assert "known_orders=orders_taken" in source, "未把已占用的时间线编号交给逐段校验"
    assert "structure_contract.json" in source, "全书结构契约没有落盘"


def test_lore_and_structure_markers_do_not_collide():
    """两套标记必须互不包含，否则一个阶段的契约会被另一个误提取。"""
    markers = (
        LORE_CONTRACT_START,
        LORE_CONTRACT_END,
        STRUCTURE_CONTRACT_START,
        STRUCTURE_CONTRACT_END,
    )
    assert len(set(markers)) == len(markers)
    for outer in markers:
        for inner in markers:
            if outer is not inner:
                assert inner not in outer


def test_structure_instructions_carry_open_threads_forward():
    from core.generation.design_contract import structure_contract_instructions

    text = structure_contract_instructions(
        section_name="第2幕",
        section_index=2,
        total_sections=3,
        known_threads=[
            {"id": "PT011", "thread": "沃伦被栽赃", "must_close_by_section": 3}
        ],
    )

    assert "PT011" in text
    assert "沃伦被栽赃" in text
    # 只有第一段才需要声明全书骨架
    assert "central_question" not in text


def test_first_structure_section_is_asked_for_the_spine():
    from core.generation.design_contract import structure_contract_instructions

    text = structure_contract_instructions(
        section_name="第1幕", section_index=1, total_sections=3
    )

    assert "central_question" in text
    assert "central_conflict" in text
    assert "chronology_events" in text


def test_structure_instructions_use_the_selected_domain_schema():
    from core.generation.design_contract import structure_contract_instructions
    from core.generation.domain_profiles import LEGAL_SUSPENSE

    text = structure_contract_instructions(
        section_name="第1幕",
        section_index=1,
        total_sections=3,
        central_conflict_schema=LEGAL_SUSPENSE.central_conflict_schema,
    )

    assert "legal_answer" in text
    assert "truth_answer" in text
    assert "moral_question" in text
