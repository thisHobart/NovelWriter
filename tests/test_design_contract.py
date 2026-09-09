"""Tests for the lore and structure stage contracts."""

import json

import pytest

from core.generation.design_contract import (
    DesignContractError,
    LORE_CONTRACT_END,
    LORE_CONTRACT_START,
    STRUCTURE_CONTRACT_END,
    STRUCTURE_CONTRACT_START,
    extract_lore_contract,
    extract_structure_contract,
    generate_with_contract_retry,
    open_threads_after,
    structure_contract_instructions,
    validate_structure_sequence,
)
from core.generation.domain_profiles import LEGAL_SUSPENSE


def _lore_payload(**overrides):
    payload = {
        "era": "现代",
        "technology_level": "现实世界当代水平",
        "canonical_characters": [
            {"id": "CH001", "name": "萨姆·金", "aliases": ["老萨姆"], "role": "前辩护律师"},
            {"id": "CH002", "name": "金伯利·罗宾逊", "aliases": [], "role": "法医技术员"},
        ],
        "canonical_locations": [{"id": "LOC001", "name": "圣兰卡法医鉴定中心"}],
        "canonical_factions": [{"id": "FAC001", "name": "翡翠暗影兄弟会"}],
        "world_constraints": ["没有超自然力量"],
    }
    payload.update(overrides)
    return payload


def _lore_response(**overrides):
    body = json.dumps(_lore_payload(**overrides), ensure_ascii=False)
    return f"# 世界观\n圣兰卡是一座海港城市。\n\n{LORE_CONTRACT_START}\n{body}\n{LORE_CONTRACT_END}"


def _structure_payload(index, total=3, **overrides):
    payload = {
        "section": f"第{index}幕",
        "section_index": index,
        "total_sections": total,
        "truths_introduced": [],
        "chronology_events": [],
        "threads_opened": [],
        "threads_closed": [],
    }
    if index == 1:
        payload["central_question"] = "谁伪造了法医时间戳？"
        payload["central_conflict"] = {
            "surface_answer": "流浪汉纵火",
            "true_answer": "专案组灭口",
            "stakes": "一个无辜者的死刑",
        }
        payload["truths_introduced"] = [
            {"id": "T011", "fact": "门禁时钟被调慢", "reveal_at_section": "第3幕"}
        ]
        payload["chronology_events"] = [
            {
                "id": "TL011",
                "order": 1,
                "event": "零点十二分运尸车进入法医中心",
                "known_initially_by": [],
            }
        ]
    payload.update(overrides)
    return payload


def _structure_response(index, total=3, **overrides):
    body = json.dumps(_structure_payload(index, total, **overrides), ensure_ascii=False)
    return f"## 第{index}幕\n情节推进。\n\n{STRUCTURE_CONTRACT_START}\n{body}\n{STRUCTURE_CONTRACT_END}"


# --- lore ------------------------------------------------------------------


def test_lore_contract_splits_markdown_and_registry():
    markdown, contract = extract_lore_contract(_lore_response())

    assert "圣兰卡是一座海港城市" in markdown
    assert LORE_CONTRACT_START not in markdown
    assert contract["origin"] == "lore"
    assert [c["name"] for c in contract["canonical_characters"]] == ["萨姆·金", "金伯利·罗宾逊"]


def test_lore_contract_rejects_one_name_owned_by_two_characters():
    """萨姆/山姆 这类前后不一致，根子是同一个人被登记成两条记录。"""
    payload = _lore_payload()
    payload["canonical_characters"].append(
        {"id": "CH003", "name": "老萨姆", "aliases": [], "role": "拾荒者"}
    )
    body = json.dumps(payload, ensure_ascii=False)

    with pytest.raises(DesignContractError) as exc_info:
        extract_lore_contract(f"# 世界观\n正文。\n{LORE_CONTRACT_START}\n{body}\n{LORE_CONTRACT_END}")

    assert exc_info.value.code == "name_collision"
    assert "老萨姆" in str(exc_info.value)


def test_lore_contract_requires_era_and_enough_characters():
    with pytest.raises(DesignContractError) as exc_info:
        extract_lore_contract(_lore_response(era=""))
    assert exc_info.value.code == "missing_field"

    with pytest.raises(DesignContractError) as exc_info:
        extract_lore_contract(_lore_response(canonical_characters=[]))
    assert exc_info.value.code == "too_few_characters"


def test_lore_contract_rejects_a_missing_or_duplicated_marker():
    with pytest.raises(DesignContractError) as exc_info:
        extract_lore_contract("# 世界观\n只有正文，没有契约。")
    assert exc_info.value.code == "contract_marker_missing"

    doubled = _lore_response() + "\n" + _lore_response()
    with pytest.raises(DesignContractError) as exc_info:
        extract_lore_contract(doubled)
    assert exc_info.value.code == "contract_marker_repeated"


# --- structure -------------------------------------------------------------


def test_structure_first_section_must_declare_the_spine():
    payload = _structure_payload(1)
    payload["central_question"] = ""
    body = json.dumps(payload, ensure_ascii=False)

    with pytest.raises(DesignContractError) as exc_info:
        extract_structure_contract(
            f"## 第1幕\n正文。\n{STRUCTURE_CONTRACT_START}\n{body}\n{STRUCTURE_CONTRACT_END}",
            1,
            3,
        )
    assert exc_info.value.code == "missing_spine"


def test_structure_contract_uses_and_enforces_the_domain_conflict_schema():
    text = structure_contract_instructions(
        "第1幕",
        1,
        3,
        central_conflict_schema=LEGAL_SUSPENSE.central_conflict_schema,
    )
    assert "legal_answer" in text
    assert "truth_answer" in text
    assert "surface_answer" not in text

    with pytest.raises(DesignContractError, match="legal_answer"):
        extract_structure_contract(
            _structure_response(1),
            1,
            3,
            central_conflict_schema=LEGAL_SUSPENSE.central_conflict_schema,
        )

    response = _structure_response(
        1,
        central_conflict={
            "legal_answer": "流浪汉纵火",
            "truth_answer": "专案组灭口",
            "moral_question": "结案率与程序正义",
        },
    )
    contract = extract_structure_contract(
        response,
        1,
        3,
        central_conflict_schema=LEGAL_SUSPENSE.central_conflict_schema,
    )[1]
    assert contract["central_conflict"]["truth_answer"] == "专案组灭口"


def test_first_structure_section_requires_an_explicit_chronology_event():
    with pytest.raises(DesignContractError, match="chronology_events"):
        extract_structure_contract(
            _structure_response(1, chronology_events=[]), 1, 3
        )


def test_structure_contract_rejects_a_deadline_outside_the_story():
    response = _structure_response(
        2,
        threads_opened=[{"id": "PT021", "thread": "沃伦被栽赃", "must_close_by_section": 9}],
    )

    with pytest.raises(DesignContractError) as exc_info:
        extract_structure_contract(response, 2, 3)
    assert exc_info.value.code == "deadline_out_of_range"


def test_structure_sequence_flags_a_thread_nobody_ever_closes():
    """沃伦·凯斯那条线被丢掉，正是这种情况。"""
    contracts = [
        extract_structure_contract(
            _structure_response(
                1,
                threads_opened=[
                    {"id": "PT011", "thread": "沃伦被栽赃", "must_close_by_section": 3}
                ],
            ),
            1,
            3,
        )[1],
        extract_structure_contract(_structure_response(2), 2, 3)[1],
        extract_structure_contract(_structure_response(3), 3, 3)[1],
    ]

    with pytest.raises(DesignContractError) as exc_info:
        validate_structure_sequence(contracts)
    assert exc_info.value.code == "thread_missing_closure"
    assert "PT011" in str(exc_info.value)


def test_structure_sequence_accepts_a_thread_closed_in_time():
    contracts = [
        extract_structure_contract(
            _structure_response(
                1,
                threads_opened=[
                    {"id": "PT011", "thread": "沃伦被栽赃", "must_close_by_section": 3}
                ],
            ),
            1,
            3,
        )[1],
        extract_structure_contract(_structure_response(2), 2, 3)[1],
        extract_structure_contract(
            _structure_response(3, threads_closed=["PT011"]), 3, 3
        )[1],
    ]

    validate_structure_sequence(contracts)


def test_structure_sequence_flags_contradictory_truth_definitions():
    contracts = [
        extract_structure_contract(_structure_response(1), 1, 3)[1],
        extract_structure_contract(
            _structure_response(
                2,
                truths_introduced=[
                    {"id": "T011", "fact": "门禁时钟被调快", "reveal_at_section": "第3幕"}
                ],
            ),
            2,
            3,
        )[1],
    ]

    with pytest.raises(DesignContractError) as exc_info:
        validate_structure_sequence(contracts)
    assert exc_info.value.code == "truth_definition_conflict"


def test_open_threads_after_reports_what_later_sections_must_close():
    first = extract_structure_contract(
        _structure_response(
            1,
            threads_opened=[
                {"id": "PT011", "thread": "沃伦被栽赃", "must_close_by_section": 3}
            ],
        ),
        1,
        3,
    )[1]

    assert [item["id"] for item in open_threads_after([first])] == ["PT011"]

    second = extract_structure_contract(
        _structure_response(2, threads_closed=["PT011"]), 2, 3
    )[1]
    assert open_threads_after([first, second]) == []


# --- retry driver ----------------------------------------------------------


def test_retry_feeds_the_failure_back_and_then_succeeds():
    prompts = []

    def send(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return "# 世界观\n正文但没有契约。"
        return _lore_response()

    markdown, contract = generate_with_contract_retry(
        send, "生成世界观", extract_lore_contract, retry_limit=2
    )

    assert len(prompts) == 2
    assert "上一次结果未通过契约校验" in prompts[1]
    assert "缺少契约标记" in prompts[1]
    assert contract["era"] == "现代"
    assert "圣兰卡是一座海港城市" in markdown


def test_retry_gives_up_after_the_limit_and_reports_the_last_reason():
    attempts = []

    def send(prompt):
        attempts.append(prompt)
        return "# 世界观\n始终不带契约。"

    with pytest.raises(DesignContractError) as exc_info:
        generate_with_contract_retry(
            send, "生成世界观", extract_lore_contract, retry_limit=2
        )

    assert len(attempts) == 3
    assert exc_info.value.code == "contract_marker_missing"


def test_later_sections_are_told_which_truths_are_already_declared():
    """全书校验要求同一个 truth id 各部分说的是同一件事，而各部分是分别生成的。

    此前只把「尚未了结的悬念」发下去，真相没发，于是第二幕会用不同措辞重新定义
    T001，等全部生成完才在合校时报错——整个结构阶段（实测 465 秒、八次调用）的
    产出一起作废，而且没有补救路径。
    """
    from core.generation.design_contract import (
        structure_contract_instructions,
        truths_after,
    )

    act_one = {
        "section_index": 1,
        "truths_introduced": [
            {"id": "T001", "fact": "保全裁定冻结的是同名的另一个账户", "reveal_at_section": "act_3"}
        ],
        "threads_opened": [],
        "threads_closed": [],
    }

    known = truths_after([act_one])
    assert [record["id"] for record in known] == ["T001"]

    prompt = structure_contract_instructions(
        section_name="act_2", section_index=2, total_sections=3, known_truths=known
    )
    assert "T001" in prompt
    assert "保全裁定冻结的是同名的另一个账户" in prompt
    assert "同一个 id 在全书只能指同一件事" in prompt


def test_the_first_section_has_no_declared_truths_to_reuse():
    from core.generation.design_contract import (
        structure_contract_instructions,
        truths_after,
    )

    assert truths_after([]) == []
    prompt = structure_contract_instructions(
        section_name="act_1", section_index=1, total_sections=3, known_truths=[]
    )
    assert "[]" in prompt


def test_truths_after_keeps_the_first_wording_when_a_later_section_repeats_an_id():
    """复述必须原样沿用，所以发下去的应当是最早那一版措辞。"""
    from core.generation.design_contract import truths_after

    first = {"section_index": 1, "truths_introduced": [{"id": "T001", "fact": "原始说法"}]}
    later = {"section_index": 2, "truths_introduced": [{"id": "T001", "fact": "改写过的说法"}]}
    known = truths_after([later, first])
    assert known == [{"id": "T001", "fact": "原始说法"}]


# --- 全书时间线编号 ---------------------------------------------------------


def test_a_section_may_not_reuse_an_order_an_earlier_section_claimed():
    """实测每个真实项目都撞车：第 6 部分从 1 重新起编，与第 1 部分整段重叠。"""
    from core.generation.design_contract import chronology_orders_used

    first = extract_structure_contract(_structure_response(1), 1, 3)[1]
    assert chronology_orders_used([first]) == [1]

    body = json.dumps(
        _structure_payload(
            2,
            chronology_events=[
                {"id": "TL021", "order": 1, "event": "同一个编号又用了一次"}
            ],
        ),
        ensure_ascii=False,
    )
    with pytest.raises(DesignContractError) as exc_info:
        extract_structure_contract(
            f"## 第2幕\n正文。\n{STRUCTURE_CONTRACT_START}\n{body}\n{STRUCTURE_CONTRACT_END}",
            2,
            3,
            known_orders=chronology_orders_used([first]),
        )
    assert exc_info.value.code == "chronology_order_reused"
    assert "TL021" in str(exc_info.value)


def test_two_events_in_one_section_may_not_share_an_order():
    body = json.dumps(
        _structure_payload(
            2,
            chronology_events=[
                {"id": "TL021", "order": 7, "event": "两件事"},
                {"id": "TL022", "order": 7, "event": "共用一个编号"},
            ],
        ),
        ensure_ascii=False,
    )
    with pytest.raises(DesignContractError) as exc_info:
        extract_structure_contract(
            f"## 第2幕\n正文。\n{STRUCTURE_CONTRACT_START}\n{body}\n{STRUCTURE_CONTRACT_END}",
            2,
            3,
        )
    assert exc_info.value.code == "chronology_order_duplicated"


def test_gaps_and_flashbacks_stay_legal():
    """编号只表示故事世界的真实先后：留空档、往前排都合法，唯独不能撞号。"""
    from core.generation.design_contract import chronology_orders_used

    first = extract_structure_contract(_structure_response(1), 1, 3)[1]
    body = json.dumps(
        _structure_payload(
            2,
            chronology_events=[
                {"id": "TL021", "order": 40, "event": "留了一大段空档"},
                {"id": "TL022", "order": 2, "event": "倒叙，排在第一部分之后但更早"},
            ],
        ),
        ensure_ascii=False,
    )
    _, second = extract_structure_contract(
        f"## 第2幕\n正文。\n{STRUCTURE_CONTRACT_START}\n{body}\n{STRUCTURE_CONTRACT_END}",
        2,
        3,
        known_orders=chronology_orders_used([first]),
    )
    assert chronology_orders_used([first, second]) == [1, 2, 40]


def test_later_sections_are_told_which_orders_are_taken():
    from core.generation.design_contract import (
        chronology_after,
        structure_contract_instructions,
    )

    first = extract_structure_contract(_structure_response(1), 1, 3)[1]
    prompt = structure_contract_instructions(
        section_name="act_2",
        section_index=2,
        total_sections=3,
        known_events=chronology_after([first]),
    )
    assert "已占用的编号是 1，" in prompt
    assert "零点十二分运尸车进入法医中心" in prompt


def test_the_first_section_is_told_it_may_start_at_one():
    from core.generation.design_contract import structure_contract_instructions

    prompt = structure_contract_instructions(
        section_name="act_1", section_index=1, total_sections=3, known_events=[]
    )
    assert "本部分是第一批登记时间线的，从 1 开始编即可" in prompt


def test_whole_story_check_catches_a_collision_the_per_section_check_never_saw():
    """各部分单独生成时若绕过了 known_orders，合校仍要拦下来。"""
    first = extract_structure_contract(_structure_response(1), 1, 3)[1]
    body = json.dumps(
        _structure_payload(
            2, chronology_events=[{"id": "TL021", "order": 1, "event": "撞号"}]
        ),
        ensure_ascii=False,
    )
    _, second = extract_structure_contract(
        f"## 第2幕\n正文。\n{STRUCTURE_CONTRACT_START}\n{body}\n{STRUCTURE_CONTRACT_END}",
        2,
        3,
    )

    with pytest.raises(DesignContractError) as exc_info:
        validate_structure_sequence([first, second])
    assert exc_info.value.code == "chronology_order_duplicated"
    assert "TL011" in str(exc_info.value) and "TL021" in str(exc_info.value)


def test_contracts_written_before_v3_are_not_retroactively_blocked():
    """current_work 六个部分撞了六个号，但正文已经定稿，不该把它的结构那一步锁上。"""
    old = [
        {
            "section_index": 1,
            "total_sections": 2,
            "schema_version": 2,
            "chronology_events": [{"id": "TL011", "order": 1, "event": "旧档"}],
            "truths_introduced": [],
            "threads_opened": [],
            "threads_closed": [],
        },
        {
            "section_index": 2,
            "total_sections": 2,
            "schema_version": 2,
            "chronology_events": [{"id": "TL021", "order": 1, "event": "旧档撞号"}],
            "truths_introduced": [],
            "threads_opened": [],
            "threads_closed": [],
        },
    ]

    validate_structure_sequence(old)
