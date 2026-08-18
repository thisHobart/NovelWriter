import json

import pytest

from core.generation.planning_contract import (
    CONTRACT_END,
    CONTRACT_START,
    PlanningContractError,
    extract_scene_plan_contract,
    validate_contract_sequence,
)


def _contract(chapter, threads=None):
    return {
        "chapter": chapter,
        "origin": "scene_planning",
        "schema_version": 2,
        "facts_added": [],
        "facts_confirmed": [],
        "facts_contradicted": [],
        "timeline_events": [],
        "character_updates": [],
        "plot_thread_updates": threads or [],
    }


def _response(contract):
    return (
        "### 场景 1：开端\n计划内容\n\n"
        + CONTRACT_START
        + "\n"
        + json.dumps(contract, ensure_ascii=False)
        + "\n"
        + CONTRACT_END
    )


def test_scene_response_is_split_into_markdown_and_contract():
    markdown, contract = extract_scene_plan_contract(_response(_contract(2)), 2)
    assert markdown == "### 场景 1：开端\n计划内容"
    assert contract["origin"] == "scene_planning"
    assert contract["schema_version"] == 2


def test_open_thread_requires_a_real_deadline():
    contract = _contract(
        2,
        [{"id": "PT-002-01", "thread": "失踪钥匙", "status": "open"}],
    )
    with pytest.raises(PlanningContractError, match="截止章"):
        extract_scene_plan_contract(_response(contract), 2)


def test_sequence_rejects_thread_without_closure_before_deadline():
    contracts = [
        _contract(
            1,
            [{"id": "PT-001-01", "thread": "失踪钥匙", "status": "open", "deadline_chapter": 2}],
        ),
        _contract(2),
    ]
    with pytest.raises(PlanningContractError, match="仍无关闭计划"):
        validate_contract_sequence(contracts, total_chapters=2)


def test_sequence_accepts_closure_on_deadline():
    contracts = [
        _contract(
            1,
            [{"id": "PT-001-01", "thread": "失踪钥匙", "status": "open", "deadline_chapter": 2}],
        ),
        _contract(
            2,
            [{"id": "PT-001-01", "thread": "失踪钥匙", "status": "closed", "deadline_chapter": 2}],
        ),
    ]
    validate_contract_sequence(contracts, total_chapters=2)
