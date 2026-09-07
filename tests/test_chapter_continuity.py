# -*- coding: utf-8 -*-
"""章节之间衔接检查的回归测试。

每一条都对应一个可以判「不合格」的判据：契约里的衔接字段、章节功能重复、时间
读数倒流、悬念长期沉默、开头套语复用、契约声明的开场站位没有兑现。
"""

import pytest

from core.generation.chapter_continuity import (
    CHAPTER_FUNCTIONS,
    chapter_function_defects,
    continuity_defects,
    continuity_gate,
    continuity_instructions,
    continuity_schema_block,
    established_context,
    hand_off_anchor_finding,
    neglected_threads,
    opening_boilerplate_finding,
    opening_position_findings,
    opening_text,
    parse_event_time,
    protected_terms,
    scene_one_continuity_rules,
    shared_fragments,
    timeline_mesh_findings,
)


def _continuity(**overrides):
    block = {
        "picks_up_from": "上一章结尾顾阳波把铅盒推到桌子中央之后没有收回手",
        "time_gap": "紧接上一章结尾",
        "opening_positions": [{"character": "顾阳波", "location": "防空洞机房"}],
    }
    block.update(overrides)
    return block


def _contract(chapter, **overrides):
    payload = {
        "chapter": chapter,
        "chapter_function": "advance",
        "continuity": _continuity(),
        "timeline_events": [],
        "character_updates": [],
        "plot_thread_updates": [],
    }
    payload.update(overrides)
    return payload


# ------------------------------------------------------------- 契约衔接字段


def test_first_chapter_owes_no_hand_off():
    assert continuity_defects({"chapter": 1}, 1) == []


def test_a_missing_continuity_block_is_a_contract_defect():
    defects = continuity_defects({"chapter": 4}, 4)
    assert len(defects) == 1
    assert "continuity" in defects[0]
    assert "第 3 章" in defects[0]


def test_placeholder_hand_off_does_not_count_as_filled_in():
    contract = _contract(4, continuity=_continuity(picks_up_from="承接上一章"))
    defects = continuity_defects(contract, 4)
    assert any("picks_up_from" in item for item in defects)


@pytest.mark.parametrize("value", ["", "无", "待定", "  "])
def test_an_empty_time_gap_is_rejected(value):
    contract = _contract(4, continuity=_continuity(time_gap=value))
    assert any("time_gap" in item for item in continuity_defects(contract, 4))


def test_opening_positions_need_both_a_person_and_a_place():
    contract = _contract(
        4, continuity=_continuity(opening_positions=[{"character": "顾阳波", "location": ""}])
    )
    assert any("opening_positions" in item for item in continuity_defects(contract, 4))

    contract = _contract(4, continuity=_continuity(opening_positions=[]))
    assert any("opening_positions" in item for item in continuity_defects(contract, 4))


def test_a_filled_in_hand_off_passes():
    assert continuity_defects(_contract(4), 4) == []


# --------------------------------------------------------------- 章节功能


def test_a_missing_chapter_function_is_a_defect():
    assert chapter_function_defects({"chapter": 3}, (), 3)


def test_an_unknown_chapter_function_is_a_defect():
    assert chapter_function_defects({"chapter": 3, "chapter_function": "填充"}, (), 3)


def test_three_chapters_in_a_row_with_the_same_function_are_reported():
    prior = [_contract(1), _contract(2)]
    defects = chapter_function_defects(_contract(3), prior, 3)
    assert len(defects) == 1
    assert "advance" in defects[0]


def test_a_varied_function_sequence_is_accepted():
    prior = [_contract(1), _contract(2, chapter_function="reveal")]
    assert chapter_function_defects(_contract(3), prior, 3) == []


def test_every_declared_function_value_is_accepted():
    for function in CHAPTER_FUNCTIONS:
        assert chapter_function_defects({"chapter": 2, "chapter_function": function}, (), 2) == []


# --------------------------------------------------------------- 时间线


def test_event_times_split_into_a_day_label_and_a_clock_reading():
    assert parse_event_time("案发第三日 05:45") == ("案发第三日", 5 * 60 + 45)
    assert parse_event_time("05:45") == ("", 5 * 60 + 45)
    assert parse_event_time("清晨") is None


def test_time_running_backwards_inside_one_day_is_blocking():
    previous = _contract(
        3, timeline_events=[{"id": "TL-003-01", "time": "案发第三日 05:55"}]
    )
    current = _contract(
        4, timeline_events=[{"id": "TL-004-01", "time": "案发第三日 05:45"}]
    )
    findings = timeline_mesh_findings(current, [previous], 4)
    assert [item["code"] for item in findings] == ["timeline_regression"]
    assert findings[0]["severity"] == "blocking"


def test_reusing_an_older_day_label_is_reported_as_a_warning():
    contracts = [
        _contract(1, timeline_events=[{"id": "TL-001-01", "time": "案发第三日 01:00"}]),
        _contract(2, timeline_events=[{"id": "TL-002-01", "time": "决战日 09:30"}]),
    ]
    findings = timeline_mesh_findings(
        _contract(3, timeline_events=[{"id": "TL-003-01", "time": "案发第三日 06:00"}]),
        contracts,
        3,
    )
    assert [item["code"] for item in findings] == ["timeline_label_backtrack"]
    assert findings[0]["severity"] == "warning"


def test_a_brand_new_day_label_is_only_flagged_for_confirmation():
    previous = _contract(
        2, timeline_events=[{"id": "TL-002-01", "time": "案发第三日 05:55"}]
    )
    findings = timeline_mesh_findings(
        _contract(3, timeline_events=[{"id": "TL-003-01", "time": "决战清晨 06:40"}]),
        [previous],
        3,
    )
    assert [item["code"] for item in findings] == ["timeline_label_drift"]
    assert findings[0]["severity"] == "warning"


def test_time_moving_forward_inside_one_day_reports_nothing():
    previous = _contract(
        2, timeline_events=[{"id": "TL-002-01", "time": "案发第三日 05:45"}]
    )
    current = _contract(
        3, timeline_events=[{"id": "TL-003-01", "time": "案发第三日 06:10"}]
    )
    assert timeline_mesh_findings(current, [previous], 3) == []


# --------------------------------------------------------------- 悬念沉默


def _thread(thread_id, action, **extra):
    record = {"id": thread_id, "thread": "证物袋被调换", "action": action}
    record.update(extra)
    return record


def test_a_thread_untouched_for_too_long_is_listed():
    prior = [
        _contract(
            1,
            plot_thread_updates=[
                _thread("PT-001-01", "open", status="open", deadline_chapter=12)
            ],
        ),
        _contract(2),
        _contract(3),
        _contract(4),
        _contract(5),
    ]
    stale = neglected_threads(prior, 6)
    assert [item["id"] for item in stale] == ["PT-001-01"]
    assert stale[0]["last_touched"] == 1
    assert stale[0]["chapters_silent"] == 5


def test_a_recently_touched_thread_is_not_listed():
    prior = [
        _contract(
            1,
            plot_thread_updates=[
                _thread("PT-001-01", "open", status="open", deadline_chapter=12)
            ],
        ),
        _contract(2, plot_thread_updates=[_thread("PT-001-01", "advance", via_node_ids=["N1"])]),
    ]
    assert neglected_threads(prior, 3) == []


def test_a_closed_thread_is_never_reported_as_neglected():
    prior = [
        _contract(
            1,
            plot_thread_updates=[
                _thread("PT-001-01", "open", status="open", deadline_chapter=2)
            ],
        ),
        _contract(2, plot_thread_updates=[_thread("PT-001-01", "close", status="closed")]),
    ]
    assert neglected_threads(prior, 9) == []


# ----------------------------------------------------------- 已建立的场与人


def test_recent_chapters_decide_what_counts_as_already_established():
    prior = [
        _contract(
            1,
            timeline_events=[{"id": "TL-001-01", "location_id": "很久以前的矿区"}],
            character_updates=[{"id": "CU-001-01", "character": "陆巡"}],
        ),
        _contract(
            5,
            timeline_events=[{"id": "TL-005-01", "location_id": "防空洞机房"}],
            character_updates=[{"id": "CU-005-01", "character": "苏桐琳"}],
        ),
    ]
    established = established_context(prior, 6)
    assert established["locations"] == ["防空洞机房"]
    # 顾阳波来自第 5 章契约的 opening_positions，同样算读者刚认识过的人。
    assert set(established["characters"]) == {"苏桐琳", "顾阳波"}


# --------------------------------------------------------------- 写作硬要求


def test_scene_one_rules_repeat_the_hand_off_and_forbid_re_staging():
    rules = scene_one_continuity_rules(
        _contract(6),
        {"locations": ["防空洞机房"], "characters": ["苏桐琳"]},
    )
    joined = "\n".join(rules)
    assert "顾阳波把铅盒推到桌子中央" in joined
    assert "紧接上一章结尾" in joined
    assert "顾阳波 在 防空洞机房" in joined
    assert "防空洞机房" in joined and "禁止从零重新描写" in joined
    assert "苏桐琳" in joined and "禁止重新交代" in joined


def test_a_contract_without_a_hand_off_produces_no_rules():
    assert scene_one_continuity_rules({"chapter": 1}) == []


# --------------------------------------------------------------- 套语复用


def test_shared_fragments_finds_the_reused_run_of_prose():
    previous = "昏黄的灯光下，坍塌的水泥板压着排水铁管。"
    current = "他绕过坍塌的水泥板，继续往前走。"
    assert shared_fragments(previous, current) == ["坍塌的水泥板"]


def test_a_shared_proper_noun_is_not_boilerplate():
    previous = "白鹤山看守所的铁门缓缓合上。"
    current = "白鹤山看守所外面下起了雨。"
    assert shared_fragments(previous, current, protected=["白鹤山看守所"]) == []


def test_a_short_coincidence_is_not_counted():
    assert shared_fragments("他的手", "他的手抬了起来") == []


def test_opening_reuse_reports_a_quote_that_can_be_found_in_the_prose():
    previous = "防空洞拱顶不断滴落浑浊的地下水，发出沉闷的轻响。"
    current = "防空洞拱顶不断滴落浑浊的地下水，发出沉闷的轻响。她抬起头。"
    finding = opening_boilerplate_finding(current, previous)
    assert finding["count"] == 1
    assert finding["longest_fragment_chars"] >= 20
    assert finding["exceeded"] is True
    assert finding["quote"] in current


def test_three_small_shared_fragments_are_enough_to_fail():
    previous = "他深吸一口气，看着坍塌的水泥板，听见远处发出沉闷的回声。"
    current = "她深吸一口气，绕开坍塌的水泥板，身后发出沉闷的碰撞。"
    finding = opening_boilerplate_finding(current, previous)
    assert finding["count"] > 2
    assert finding["longest_fragment_chars"] < 20
    assert finding["exceeded"] is True


def test_openings_that_share_nothing_report_zero():
    finding = opening_boilerplate_finding(
        "她把封存单推回桌面，没有再看那份笔录一眼。",
        "雨停了，院子里只剩下滴水的声音。",
    )
    assert finding["count"] == 0
    assert finding["exceeded"] is False


def test_opening_text_drops_the_heading_and_whitespace():
    assert opening_text("# 第 4 章\n\n他走了进来。\n") == "他走了进来。"


# ------------------------------------------------------- 开场站位是否兑现


def test_a_declared_opening_character_who_never_shows_up_is_reported():
    prose = "苏桐琳把封存单推回桌面。她没有抬头。"
    findings = opening_position_findings(prose, _contract(4))
    assert [item["code"] for item in findings] == ["OPENING_POSITION_UNMET"]
    assert findings[0]["quote"] in prose
    assert "顾阳波" in findings[0]["problem"]


def test_a_declared_opening_character_who_does_show_up_is_accepted():
    prose = "顾阳波把封存单推回桌面。他没有抬头。"
    assert opening_position_findings(prose, _contract(4)) == []


# --------------------------------------------------------------- 整章闸门


def test_the_gate_blocks_a_chapter_that_rewrites_the_previous_opening():
    previous = (
        "防空洞拱顶不断滴落浑浊的地下水。水珠砸在积水的碎石地面上，发出单调的声响。"
        "黄色防爆灯挂在生锈的铁架上，昏暗的光线照着裸露在混凝土墙外的排水铁管。"
    )
    current = (
        "防空洞拱顶不断滴落浑浊的地下水。水珠砸在积水的碎石地面上，发出单调的声响。"
        "黄色防爆灯挂在生锈的铁架上，顾阳波抬起头。"
    )
    report = continuity_gate(current, previous, _contract(4), 4)
    assert report["passed"] is False
    codes = [item["code"] for item in report["hard_failures"]]
    assert "OPENING_BOILERPLATE_REUSE" in codes
    quote = next(
        item["quote"] for item in report["hard_failures"]
        if item["code"] == "OPENING_BOILERPLATE_REUSE"
    )
    assert quote in current
    # 整段照抄只留下一条很长的片段，条数根本不会超标——长度那条判据正是为它准备的。
    assert report["metrics"]["longest_shared_fragment_chars"] >= 20


def test_the_gate_passes_a_chapter_that_starts_from_the_previous_ending():
    previous = "顾阳波把铅盒推到桌子中央，谁也没有伸手去接。灯管在头顶滋滋作响。"
    current = "顾阳波的手还压在铅盒上。他终于松开五指，金属提手弹起一道细响。"
    report = continuity_gate(current, previous, _contract(4), 4)
    assert report["passed"] is True
    assert report["hard_failures"] == []
    assert report["metrics"]["shared_opening_fragments"] <= 2


def test_the_gate_carries_timeline_and_thread_findings_as_warnings():
    prior = [
        _contract(
            1,
            timeline_events=[{"id": "TL-001-01", "time": "案发第一日 01:00"}],
            plot_thread_updates=[
                _thread("PT-001-01", "open", status="open", deadline_chapter=20)
            ],
        ),
        _contract(2, timeline_events=[{"id": "TL-002-01", "time": "案发第一日 02:00"}]),
        _contract(3, timeline_events=[{"id": "TL-003-01", "time": "案发第一日 03:00"}]),
        _contract(4, timeline_events=[{"id": "TL-004-01", "time": "案发第一日 04:00"}]),
    ]
    contract = _contract(
        5, timeline_events=[{"id": "TL-005-01", "time": "决战清晨 06:00"}]
    )
    report = continuity_gate(
        "顾阳波的手还压在铅盒上。", "灯管在头顶滋滋作响。", contract, 5, prior_contracts=prior
    )
    assert report["passed"] is True
    assert any("决战清晨" in item for item in report["warnings"])
    assert any("PT-001-01" in item for item in report["warnings"])


def test_the_first_chapter_gate_has_nothing_to_compare_against():
    report = continuity_gate("第一章正文。", "", {"chapter": 1}, 1)
    assert report["passed"] is True
    assert report["metrics"]["shared_opening_fragments"] == 0


def test_protected_terms_are_collected_from_the_ledger_and_contracts():
    ledger = {
        "character_updates": [{"character": "顾阳波"}],
        "timeline_events": [{"location_id": "临山·防空洞机房"}],
    }
    names = protected_terms(ledger, [_contract(2)])
    assert "顾阳波" in names
    assert "防空洞机房" in names
    assert "临山" in names


# --------------------------------------------------------------- 规划提示词


def test_the_planning_prompt_asks_for_the_hand_off_fields():
    block = continuity_schema_block(7)
    assert "picks_up_from" in block
    assert "time_gap" in block
    assert "opening_positions" in block
    assert "第 6 章" in block


def test_the_planning_prompt_carries_the_previous_ending_and_what_is_established():
    text = continuity_instructions(
        7,
        previous_tail="他终于松开五指。",
        established={"locations": ["防空洞机房"], "characters": ["苏桐琳"]},
        previous_last_event={
            "event": "苏桐琳突破防火墙",
            "time": "案发第三日 05:45",
            "location_id": "防空洞机房",
        },
        neglected=[{"id": "PT-001-01", "thread": "证物袋被调换", "last_touched": 1}],
    )
    assert "他终于松开五指。" in text
    assert "防空洞机房" in text
    assert "苏桐琳" in text
    assert "案发第三日 05:45" in text
    assert "PT-001-01" in text


def test_the_first_chapter_gets_no_hand_off_instructions():
    assert continuity_instructions(1) == ""


# --------------------------------------------------------------- 承接锚点


def test_an_opening_that_refers_back_to_the_previous_ending_has_an_anchor():
    previous = "顾阳波把银灰色铅盒推到桌子中央，谁也没有伸手去接。灯管在头顶滋滋作响。"
    current = "谁也没有伸手去接那只铅盒。顾阳波终于自己收回了手。"
    finding = hand_off_anchor_finding(current, previous)
    assert finding["count"] >= 1
    assert "谁也没有伸手去接" in finding["longest"]


def test_an_opening_that_starts_over_has_no_anchor():
    previous = "顾阳波把铅盒推到桌子中央，谁也没有伸手去接。"
    current = "与此同时，三百公里外的雾平正下着雨。老城区的骑楼滴着水。"
    assert hand_off_anchor_finding(current, previous)["count"] == 0


def test_the_gate_reports_a_missing_hand_off_anchor_as_a_warning():
    report = continuity_gate(
        "与此同时，三百公里外的雾平正下着雨。顾阳波看着老城区骑楼上的积水。",
        "他把铅盒推到桌子中央，谁也没有伸手去接。",
        _contract(4),
        4,
    )
    assert report["metrics"]["hand_off_anchors"] == 0
    assert any("没有任何字面上的回指" in item for item in report["warnings"])
    # 换一套说法照样可以接上，所以这只是提示，不拦章。
    assert report["passed"] is True


def test_the_gate_counts_the_anchor_when_the_opening_picks_up_the_ending():
    report = continuity_gate(
        "谁也没有伸手去接那只铅盒。顾阳波终于自己收回了手。",
        "顾阳波把银灰色铅盒推到桌子中央，谁也没有伸手去接。灯管在头顶滋滋作响。",
        _contract(4),
        4,
    )
    assert report["metrics"]["hand_off_anchors"] >= 1
    assert not any("没有任何字面上的回指" in item for item in report["warnings"])
