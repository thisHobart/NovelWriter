import json

import pytest

from core.generation.planning_contract import (
    CONTRACT_END,
    CONTRACT_START,
    PlanningContractError,
    build_existing_planning_index,
    contract_output_instructions,
    downstream_obligations,
    extract_scene_plan_contract,
    load_planning_contracts,
    open_threads_before,
    validate_contract_against_history,
    collect_contract_defects,
    validate_contract_sequence,
    validate_planning_contract,
)
from core.generation.story_ledger import StoryLedgerManager


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
    with pytest.raises(PlanningContractError, match="deadline_chapter"):
        extract_scene_plan_contract(_response(contract), 2)


def test_sequence_rejects_thread_without_closure_before_deadline():
    contracts = [
        _contract(
            1,
            [{"id": "PT-001-01", "thread": "失踪钥匙", "status": "open", "deadline_chapter": 2}],
        ),
        _contract(2),
    ]
    with pytest.raises(PlanningContractError, match="都没有安排了结"):
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


def _project(tmp_path, contracts):
    manager = StoryLedgerManager(str(tmp_path))
    for contract in contracts:
        manager.save_contract(
            contract["chapter"], contract, "### 场景 1：占位\n规划内容"
        )
    return str(tmp_path)


def _open(thread_id, deadline, thread="被调换的证物袋"):
    return {
        "id": thread_id,
        "thread": thread,
        "status": "open",
        "deadline_chapter": deadline,
    }


def _close(thread_id, thread="被调换的证物袋", **extra):
    return {"id": thread_id, "thread": thread, "status": "closed", **extra}


def test_closing_an_unopened_thread_is_caught_before_the_chapter_is_saved(tmp_path):
    project = _project(tmp_path, [_contract(5, [_open("PT-005-01", 8)])])
    dangling = _contract(8, [_close("PT-006-02", "消失的门禁记录")])
    with pytest.raises(PlanningContractError) as excinfo:
        validate_contract_against_history(dangling, load_planning_contracts(project), 8)
    assert excinfo.value.code == "thread_closed_before_open"


def test_self_contained_thread_may_close_in_its_own_chapter(tmp_path):
    project = _project(tmp_path, [_contract(1, [])])
    contract = _contract(2, [_close("PT-002-01", opened_in_chapter=True)])
    validate_contract_against_history(contract, load_planning_contracts(project), 2)


def test_a_spent_thread_cannot_be_closed_a_second_time(tmp_path):
    project = _project(
        tmp_path,
        [_contract(1, [_open("PT-001-01", 3)]), _contract(3, [_close("PT-001-01")])],
    )
    with pytest.raises(PlanningContractError) as excinfo:
        validate_contract_against_history(
            _contract(5, [_close("PT-001-01")]), load_planning_contracts(project), 5
        )
    assert excinfo.value.code == "thread_closed_twice"


def test_replanning_a_chapter_may_not_drop_a_thread_a_later_chapter_closes(tmp_path):
    project = _project(
        tmp_path,
        [_contract(5, [_open("PT-005-01", 8)]), _contract(8, [_close("PT-005-01")])],
    )
    replanned = _contract(5, [_open("PT-005-02", 7, "证物袋编号对不上")])
    with pytest.raises(PlanningContractError) as excinfo:
        validate_contract_against_history(
            replanned,
            load_planning_contracts(project),
            5,
            obligations=downstream_obligations(project, 5),
        )
    assert excinfo.value.code == "thread_opening_dropped"


def test_replanning_a_chapter_may_add_threads_while_keeping_its_obligations(tmp_path):
    project = _project(
        tmp_path,
        [_contract(5, [_open("PT-005-01", 8)]), _contract(8, [_close("PT-005-01")])],
    )
    replanned = _contract(
        5, [_open("PT-005-01", 8), _open("PT-005-02", 7, "证物袋编号对不上")]
    )
    validate_contract_against_history(
        replanned,
        load_planning_contracts(project),
        5,
        obligations=downstream_obligations(project, 5),
    )


def test_dangling_close_names_the_opening_chapter_as_the_repair_target(tmp_path):
    project = _project(
        tmp_path,
        [
            _contract(5, [_open("PT-005-02", 7, "证物袋编号对不上")]),
            _contract(7, [_close("PT-005-02", "证物袋编号对不上")]),
            _contract(8, [_close("PT-005-01")]),
        ],
    )
    with pytest.raises(PlanningContractError) as excinfo:
        validate_contract_sequence(load_planning_contracts(project))
    assert excinfo.value.code == "thread_closed_before_open"
    assert excinfo.value.chapters[0] == 5


def test_planning_index_reports_thread_state_not_the_raw_update_stream(tmp_path):
    project = _project(
        tmp_path,
        [
            _contract(1, [_open("PT-001-01", 2, "失踪钥匙")]),
            _contract(2, [_close("PT-001-01", "失踪钥匙")]),
            _contract(3, [_open("PT-003-01", 6, "被调换的证物袋")]),
        ],
    )
    index = build_existing_planning_index(project, 6)
    assert [item["id"] for item in index["open_plot_threads"]] == ["PT-003-01"]
    assert [item["id"] for item in index["closed_plot_threads"]] == ["PT-001-01"]
    assert index["threads_due_this_chapter"] == ["PT-003-01"]

    instructions = contract_output_instructions(6, index)
    assert "PT-003-01" in instructions
    assert "本章必须了结" in instructions


def test_open_threads_before_ignores_threads_that_opened_and_closed_together():
    contracts = [_contract(1, [_close("PT-001-01", opened_in_chapter=True)])]
    assert open_threads_before(contracts) == []


def _extend(thread_id, deadline, thread="被调换的证物袋"):
    return {
        "id": thread_id,
        "thread": thread,
        "status": "open",
        "extend": True,
        "deadline_chapter": deadline,
    }


def test_a_thread_due_this_chapter_must_be_closed_or_deferred(tmp_path):
    project = _project(tmp_path, [_contract(3, [_open("PT-003-01", 5, "未寄出的信")])])

    with pytest.raises(PlanningContractError) as excinfo:
        validate_contract_against_history(
            _contract(5, []), load_planning_contracts(project), 5
        )

    assert excinfo.value.code == "thread_overdue"
    assert excinfo.value.chapters == (5, 3)


def test_a_thread_not_yet_due_is_left_alone(tmp_path):
    project = _project(tmp_path, [_contract(3, [_open("PT-003-01", 9, "未寄出的信")])])

    validate_contract_against_history(
        _contract(5, []), load_planning_contracts(project), 5
    )


def test_an_explicit_deferral_satisfies_the_deadline(tmp_path):
    project = _project(tmp_path, [_contract(3, [_open("PT-003-01", 5, "未寄出的信")])])
    deferred = _contract(5, [_extend("PT-003-01", 27, "未寄出的信")])

    validate_contract_against_history(deferred, load_planning_contracts(project), 5)

    _project(tmp_path, [deferred, _contract(27, [_close("PT-003-01", "未寄出的信")])])
    validate_contract_sequence(load_planning_contracts(str(tmp_path)))


def test_a_deferral_must_move_the_deadline_forward(tmp_path):
    with pytest.raises(PlanningContractError, match="必须晚于本章"):
        validate_planning_contract(
            _contract(5, [_extend("PT-003-01", 4, "未寄出的信")]), 5
        )


def test_a_late_closure_names_the_chapter_that_set_the_deadline(tmp_path):
    project = _project(
        tmp_path,
        [
            _contract(3, [_open("PT-003-01", 5, "未寄出的信")]),
            _contract(27, [_close("PT-003-01", "未寄出的信")]),
        ],
    )

    with pytest.raises(PlanningContractError) as excinfo:
        validate_contract_sequence(load_planning_contracts(project))

    assert excinfo.value.code == "thread_closes_late"
    # Chapter 5 never mentions the thread, so it must not be the repair target.
    assert excinfo.value.chapters == (3, 27)
    assert "deadline_chapter 改为 27" in str(excinfo.value)


def test_an_unclosed_thread_offers_the_opening_chapter_as_a_fallback(tmp_path):
    project = _project(
        tmp_path,
        [_contract(1, [_open("PT-001-01", 3, "失踪钥匙")]), _contract(3, [])],
    )

    with pytest.raises(PlanningContractError) as excinfo:
        validate_contract_sequence(load_planning_contracts(project))

    assert excinfo.value.code == "thread_missing_closure"
    assert excinfo.value.chapters == (3, 1)


def test_all_defects_are_collected_not_just_the_blocking_one(tmp_path):
    project = _project(
        tmp_path,
        [
            _contract(1, [_open("PT-001-01", 2, "失踪钥匙")]),
            _contract(2, [_close("PT-004-02", "匿名举报信")]),
            _contract(3, []),
        ],
    )

    defects = collect_contract_defects(load_planning_contracts(project), total_chapters=3)
    codes = sorted(defect.code for defect in defects)

    # The blocking gate reports only the first of these.
    assert codes == ["thread_closed_before_open", "thread_missing_closure"]
    with pytest.raises(PlanningContractError) as excinfo:
        validate_contract_sequence(load_planning_contracts(project), total_chapters=3)
    assert excinfo.value.code == defects[0].code


def test_a_clean_project_collects_nothing(tmp_path):
    project = _project(
        tmp_path,
        [
            _contract(1, [_open("PT-001-01", 2, "失踪钥匙")]),
            _contract(2, [_close("PT-001-01", "失踪钥匙")]),
        ],
    )

    assert collect_contract_defects(load_planning_contracts(project), total_chapters=2) == []
