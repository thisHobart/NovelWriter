import json

import pytest

from core.generation.planning_contract import (
    CONTRACT_END,
    CONTRACT_START,
    PlanningContractError,
    build_existing_planning_index,
    collect_history_defects,
    contract_for_scene,
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


def _continuity(chapter):
    """第二章起必须交代的衔接块；第一章没有上一章可接。"""
    if int(chapter) <= 1:
        return {}
    return {
        "continuity": {
            "picks_up_from": f"第 {chapter - 1} 章结尾程砚把封存单推回桌面那一刻",
            "time_gap": "紧接上一章结尾",
            "opening_positions": [{"character": "程砚", "location": "讯问室"}],
        }
    }


def _contract(chapter, threads=None, function="advance"):
    return {
        "chapter": chapter,
        "origin": "scene_planning",
        "schema_version": 2,
        "chapter_function": function,
        "facts_added": [],
        "facts_confirmed": [],
        "facts_contradicted": [],
        "timeline_events": [],
        "character_updates": [],
        "plot_thread_updates": threads or [],
        **_continuity(chapter),
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


def test_domain_schema_examples_use_chapter_scoped_ids():
    text = contract_output_instructions(
        2,
        domain_fields={
            "fair_play_clues": '[{"id":"C001","surface_meaning":"表面"}]',
            "evidence_updates": '[{"id":"E001","item":"证物"}]',
            "suspect_states": '[{"id":"S001","suspect":"人物"}]',
        },
    )

    assert '"id":"C-002-01"' in text
    assert '"id":"E-002-01"' in text
    assert '"id":"S-002-01"' in text
    assert '"id":"C001"' not in text


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


def test_history_check_reports_both_fact_definitions_for_repair():
    canonical = {
        **_contract(14),
        "facts_added": [
            {
                "id": "F-014-01",
                "fact": "海陵先驱号HL-0941集装箱用途",
                "value": "用于转移NB-4神经制剂与活体受试者的绝密温控舱",
            }
        ],
    }
    proposed = {
        **_contract(17),
        "facts_added": [
            {
                "id": "F-014-01",
                "fact": "海陵先驱号HL-0941集装箱用途",
                "value": "用于向公海转运实验样本的深冷集装箱",
            }
        ],
    }

    defects = collect_history_defects(proposed, [canonical], 17)

    assert [defect.code for defect in defects] == ["fact_definition_conflict"]
    message = str(defects[0])
    assert "第 14 章" in message and "第 17 章" in message
    assert "NB-4神经制剂" in message
    assert "向公海转运实验样本" in message
    assert "facts_confirmed" in message


def test_keeping_the_thread_is_not_enough_if_the_deadline_expires_first(tmp_path):
    """The repair that used to loop forever: thread kept, deadline unchanged."""
    project = _project(
        tmp_path,
        [
            _contract(3, [_open("PT-003-01", 5, "失踪证人")]),
            _contract(27, [_close("PT-003-01", "失踪证人")]),
        ],
    )
    replanned = _contract(3, [_open("PT-003-01", 5, "失踪证人")])

    with pytest.raises(PlanningContractError) as excinfo:
        validate_contract_against_history(
            replanned,
            load_planning_contracts(project),
            3,
            obligations=downstream_obligations(project, 3),
        )

    assert excinfo.value.code == "deadline_before_payoff"
    assert "请改成 27" in str(excinfo.value)


def test_a_deadline_that_covers_the_payoff_is_accepted(tmp_path):
    project = _project(
        tmp_path,
        [
            _contract(3, [_open("PT-003-01", 5, "失踪证人")]),
            _contract(27, [_close("PT-003-01", "失踪证人")]),
        ],
    )
    repaired = _contract(3, [_open("PT-003-01", 27, "失踪证人")])

    validate_contract_against_history(
        repaired,
        load_planning_contracts(project),
        3,
        obligations=downstream_obligations(project, 3),
    )


def test_the_prompt_states_the_deadline_the_payoff_requires(tmp_path):
    project = _project(
        tmp_path,
        [
            _contract(3, [_open("PT-003-01", 5, "失踪证人")]),
            _contract(27, [_close("PT-003-01", "失踪证人")]),
        ],
    )

    instructions = contract_output_instructions(
        3,
        build_existing_planning_index(project, 3),
        obligations=downstream_obligations(project, 3),
    )

    assert "deadline_chapter 不得早于第 27 章" in instructions
    # The JSON template must not suggest the current chapter as the deadline.
    assert '"deadline_chapter":3}' not in instructions


# --- 补洞：中途缺失的章节必须还能重新规划 -------------------------------------
#
# 一本已经规划到后面的书，中间某一章的规划失败留下空洞。重新规划那一章时，
# downstream_obligations 曾经把**整本书**（含更后面章节自己提出的悬念）都算作
# 这一章的义务，于是它被要求埋下一条属于第 14 章的悬念——永远无法满足，重试
# 用尽即失败，空洞再也补不回来。


def _holed_project(tmp_path):
    """第 1-5 章与第 14、19 章已规划，第 6-13 章是空洞。"""
    return _project(
        tmp_path,
        [
            _contract(1, [_open("PT-001-01", 6, "梁浩能否自证清白")]),
            _contract(5, [_open("PT-005-01", 6, "冷链车轨迹能否翻案")]),
            _contract(
                14,
                [
                    {"id": "PT-001-01", "thread": "梁浩能否自证清白", "status": "closed"},
                    {"id": "PT-005-01", "thread": "冷链车轨迹能否翻案", "status": "closed"},
                    _open("PT-014-01", 19, "双城雷霆行动能否奏效"),
                ],
            ),
            _contract(
                19,
                [{"id": "PT-014-01", "thread": "双城雷霆行动能否奏效", "status": "closed"}],
            ),
        ],
    )


def test_obligations_exclude_threads_a_later_chapter_raised_itself(tmp_path):
    project = _holed_project(tmp_path)

    obligations = downstream_obligations(project, 6)

    # 第 14 章自己提出、第 19 章了结的悬念与第 6 章无关。
    assert "PT-014-01" not in obligations["threads_closed_later"]
    # 第 1、5 章埋下、第 14 章了结的悬念仍然是第 6 章要保住的。
    assert set(obligations["threads_closed_later"]) == {"PT-001-01", "PT-005-01"}


def test_a_missing_middle_chapter_can_be_replanned(tmp_path):
    project = _holed_project(tmp_path)
    prior = load_planning_contracts(project)

    # 第 6 章把两条到期的悬念推迟到第 14 章——作者能写出来的合理契约。
    replanned = _contract(
        6,
        [
            {"id": "PT-001-01", "status": "open", "extend": True, "deadline_chapter": 14},
            {"id": "PT-005-01", "status": "open", "extend": True, "deadline_chapter": 14},
        ],
    )

    validate_contract_against_history(
        replanned, prior, 6, obligations=downstream_obligations(project, 6)
    )


def test_obligations_still_protect_a_thread_this_chapter_owns(tmp_path):
    """过滤不能把真正的义务一起滤掉：第 6 章埋下、第 14 章了结的悬念必须保住。"""
    project = _project(
        tmp_path,
        [
            _contract(6, [_open("PT-006-01", 14, "第六章埋下的悬念")]),
            _contract(
                14,
                [{"id": "PT-006-01", "thread": "第六章埋下的悬念", "status": "closed"}],
            ),
        ],
    )
    obligations = downstream_obligations(project, 6)
    assert "PT-006-01" in obligations["threads_closed_later"]

    # 重新规划第 6 章时把这条悬念删掉，必须被拦下。
    dropped = _contract(6, [])
    with pytest.raises(PlanningContractError, match="不能删掉") as excinfo:
        validate_contract_against_history(dropped, [], 6, obligations=obligations)
    assert excinfo.value.code == "thread_opening_dropped"


def test_obligations_exclude_facts_introduced_by_later_chapters(tmp_path):
    project = _project(
        tmp_path,
        [
            {**_contract(3), "facts_added": [{"id": "F-003-01", "fact": "弹道匹配度"}]},
            {
                **_contract(14),
                "facts_added": [{"id": "F-014-01", "fact": "集装箱用途"}],
            },
            {
                **_contract(20),
                "facts_confirmed": [
                    {"id": "F-003-01", "fact": "弹道匹配度"},
                    {"id": "F-014-01", "fact": "集装箱用途"},
                ],
            },
        ],
    )

    obligations = downstream_obligations(project, 6)

    assert "F-003-01" in obligations["facts_used_later"]
    assert "F-014-01" not in obligations["facts_used_later"]


def test_unknown_origin_is_kept_so_a_dropped_opening_is_still_caught(tmp_path):
    """来源既查不到落盘记录、ID 也没编码章号时，保守地保留该义务。"""
    project = _project(
        tmp_path,
        [
            _contract(
                14,
                [{"id": "mystery-thread", "thread": "来源不明的悬念", "status": "closed"}],
            )
        ],
    )

    obligations = downstream_obligations(project, 6)

    assert "mystery-thread" in obligations["threads_closed_later"]


def test_contract_prompt_explains_which_attributes_stay_stable():
    """stable 的含义必须写清楚。

    模板此前只在 JSON 骨架里给了一个写死的 "stable":true 示例、没有任何说明，
    模型于是把「心理状态」「行动目标」这类本该随剧情演进的属性也标成 stable，
    后面章节人物正常成长就被验收判成前后矛盾，整轮写作停机。
    """
    prompt = contract_output_instructions(3)

    assert '"stable":false' in prompt
    assert "心理状态" in prompt
    assert "拿不准就填 false" in prompt


def test_contract_allows_breather_chapter_without_irreversible_change():
    contract = _contract(3)
    contract["chapter_function"] = "breather"
    contract["irreversible_change"] = ""
    contract["ending_effect"] = "两人暂时停止争论，但没有和解"

    normalized = validate_planning_contract(contract, 3)

    assert normalized["chapter_function"] == "breather"
    assert normalized["irreversible_change"] == ""
    assert normalized["ending_effect"] == "两人暂时停止争论，但没有和解"


def test_old_contracts_get_compatible_chapter_function_defaults():
    contract = _contract(4)
    contract["irreversible_change"] = "证人撤回证词"

    normalized = validate_planning_contract(contract, 4)

    assert normalized["chapter_function"] == "advance"
    assert normalized["ending_effect"] == "证人撤回证词"


def _staged_contract():
    return {
        "chapter": 4,
        "scene_boundaries": [
            "scene_1: 蒋静登庭指认",
            "scene_2: 郑娜敏签发逮捕令",
            "scene_3: 法官当庭裁决梁浩无罪",
        ],
        "facts_added": [
            {"id": "F1", "first_stated_at": "scene_1"},
            {"id": "F3", "first_stated_at": "scene_3"},
        ],
        "fair_play_clues": [
            {"id": "C1", "introduced_at": "scene_1", "payoff_at": "scene_3"},
            {"id": "C3", "introduced_at": "scene_3"},
        ],
        "narrative_transitions": [
            {"node_id": "N1", "scene": "scene_2"},
            {"node_id": "N3", "scene": "scene_3"},
        ],
        "timeline_events": [{"id": "TL1", "event": "没有场次标记"}],
        "reader_must_not_know_yet": ["母本位置"],
        "forbidden_reveals": ["清道夫身份"],
    }


def test_a_scene_only_sees_the_contract_up_to_itself():
    """整份契约写着后面几场要做什么，原样发给写作等于先剧透再要求别写。"""
    scoped = contract_for_scene(_staged_contract(), 2)

    assert scoped["scene_boundaries"] == ["scene_1: 蒋静登庭指认", "scene_2: 郑娜敏签发逮捕令"]
    assert [item["id"] for item in scoped["facts_added"]] == ["F1"]
    assert [item["id"] for item in scoped["fair_play_clues"]] == ["C1"]
    assert [item["node_id"] for item in scoped["narrative_transitions"]] == ["N1"]


def test_a_clue_that_pays_off_later_still_reaches_the_scene_that_plants_it():
    """按埋下的场次过滤，不按兑现的场次——否则第一场就看不到自己要埋的线索。"""
    scoped = contract_for_scene(_staged_contract(), 1)
    assert [item["id"] for item in scoped["fair_play_clues"]] == ["C1"]


def test_records_without_a_scene_marker_are_always_kept():
    """看不出属于第几场的，有可能正是本场必须遵守的约束，宁可多给。"""
    scoped = contract_for_scene(_staged_contract(), 1)
    assert [item["id"] for item in scoped["timeline_events"]] == ["TL1"]
    assert scoped["reader_must_not_know_yet"] == ["母本位置"]
    assert scoped["forbidden_reveals"] == ["清道夫身份"]


def test_the_last_scene_sees_the_whole_contract():
    assert contract_for_scene(_staged_contract(), 3) == _staged_contract()


def test_filtering_never_touches_the_contract_the_gate_uses():
    original = _staged_contract()
    contract_for_scene(original, 1)
    assert original == _staged_contract()


def test_an_unknown_scene_number_leaves_the_contract_alone():
    assert contract_for_scene(_staged_contract(), None) == _staged_contract()


def test_free_text_that_happens_to_mention_a_later_scene_is_not_a_marker():
    """只看已知的场次标记键；正文里随口提到 scene 3 不算这条记录属于第三场。"""
    contract = {
        "reader_knows_after": [{"id": "K1", "note": "读者知道 scene 3 会揭晓母本"}],
        "scene_boundaries": [
            {"scene_number": 1, "end_state": "甲"},
            {"scene_number": 3, "end_state": "丙"},
        ],
    }
    scoped = contract_for_scene(contract, 1)
    assert [item["id"] for item in scoped["reader_knows_after"]] == ["K1"]
    assert [item["scene_number"] for item in scoped["scene_boundaries"]] == [1]


def test_empty_graph_tells_the_model_to_leave_the_graph_fields_blank():
    """空图上契约必须落进「非图支撑」那一档，否则六条规则一起报错。

    校验按「有没有引用图节点」分两档：零引用合法（既有项目一路三十章都跑在空图
    上），一旦引用任何节点，primary_thread / primary_action / via_node_ids /
    plot_thread_updates 全部变成必填且必须指向真实节点——空图上没有一条能满足。
    而 schema 仍写着「从 active_threads 选择」并给了 advance 示例，模型只能编一个
    情节线 id，契约当场升级成图支撑，重试两次后整个场景规划阶段崩溃。
    """
    empty = {
        "active_threads": [], "available_threads": [],
        "available_clues": [], "available_facts": [], "graph_revision": 0,
    }
    prompt = contract_output_instructions(1, narrative_context=empty)

    assert "本作目前没有叙事图节点" in prompt
    assert "不要自己编造节点 ID" in prompt
    assert '"plot_thread_updates"' in prompt


def test_a_populated_graph_keeps_the_original_instructions():
    populated = {
        "active_threads": [{"id": "PT-MAIN", "type": "thread"}],
        "available_threads": [], "available_clues": [], "available_facts": [],
        "graph_revision": 3,
    }
    prompt = contract_output_instructions(1, narrative_context=populated)

    assert "本作目前没有叙事图节点" not in prompt
    assert "PT-MAIN" in prompt


def _graph_context(active, available, revision=1):
    return {
        "active_threads": [{"id": item} for item in active],
        "available_threads": [{"id": item} for item in available],
        "available_clues": [],
        "available_facts": [],
        "graph_revision": revision,
    }


def test_unopened_threads_must_be_opened_before_they_can_advance():
    """「先 open 再 advance」是硬规则，图播种之后必须提前讲清楚。

    播种让节点存在了，但账本里一章都还没打开它们。模型看见 id 就直接写 advance，
    实测第 1、2 章都栽在这里，每章白烧一轮规划调用。
    """
    prompt = contract_output_instructions(
        1, narrative_context=_graph_context(active=[], available=["PT001", "PT002"])
    )

    assert "PT001、PT002" in prompt
    assert "primary_action 只能是 open" in prompt
    assert "deadline_chapter" in prompt
    assert "直接写 advance 会被判不合格" in prompt


def test_already_open_threads_are_listed_separately():
    prompt = contract_output_instructions(
        5, narrative_context=_graph_context(active=["PT001"], available=["PT001", "PT003"])
    )

    assert "已经打开、可以直接推进**的情节线：PT001" in prompt
    assert "还没有任何一章打开过**的情节线：PT003" in prompt


def test_the_model_is_told_not_to_invent_node_ids():
    """实测出现过「ChapterContract 引用了不存在的叙事图节点」。"""
    prompt = contract_output_instructions(
        3, narrative_context=_graph_context(active=["PT001"], available=["PT001"])
    )
    assert "不要自己编造" in prompt


def test_an_empty_graph_gets_the_other_block_instead():
    prompt = contract_output_instructions(1, narrative_context=_graph_context([], []))

    assert "本作目前没有叙事图节点" in prompt
    assert "关于叙事图上的情节线" not in prompt


def test_schema_examples_use_real_node_ids_when_the_graph_is_seeded():
    """示例原本写死成 "id":"PT-004-01"、"via_node_ids":["图节点ID"]。

    那是在演示怎么编一个不存在的 id。图播种之后，契约校验会因为这些编造的 id 判
    「引用了不存在的叙事图节点」——实测第 4、5 章连报五次，第 4 章两轮重试后硬失败。
    """
    context = _graph_context(active=["PT001"], available=["PT001", "PT002"])
    context["available_facts"] = [{"id": "T001"}, {"id": "T002"}]

    prompt = contract_output_instructions(4, narrative_context=context)

    assert '"id":"PT001"' in prompt
    assert '"via_node_ids":["T001"]' in prompt
    assert '"node_id":"T001"' in prompt
    assert "PT-004-01" not in prompt
    assert "图节点ID" not in prompt


def test_an_empty_graph_keeps_the_placeholder_examples():
    """空图时那几个字段本来就该留空，占位符无害。"""
    prompt = contract_output_instructions(4, narrative_context=_graph_context([], []))

    assert "PT-004-01" in prompt
    assert "本作目前没有叙事图节点" in prompt
