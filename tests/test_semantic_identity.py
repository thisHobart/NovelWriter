import json

from core.generation.domain_profiles import LEGAL_SUSPENSE
from core.generation.semantic_identity import (
    resolve_contract_identities,
    resolve_domain_identities,
)
from core.generation.story_ledger import StoryLedgerManager


def _contract(chapter, *, facts=None, threads=None, events=None, attributes=None):
    return {
        "chapter": chapter,
        "origin": "scene_planning",
        "schema_version": 2,
        "facts_added": facts or [],
        "facts_confirmed": [],
        "facts_contradicted": [],
        "timeline_events": events or [],
        "character_updates": attributes or [],
        "plot_thread_updates": threads or [],
    }


def _decides(relation, similarity, matched_id=None):
    def send(prompt, model=None):
        return json.dumps({
            "relation": relation,
            "similarity": similarity,
            "matched_id": matched_id,
            "reason": "test",
        }, ensure_ascii=False)
    return send


def _save(tmp_path, contract):
    StoryLedgerManager(str(tmp_path)).save_contract(
        contract["chapter"], contract, f"### 场景 1：第{contract['chapter']}章"
    )


def test_exact_repeated_open_is_removed_without_model_call(tmp_path):
    _save(tmp_path, _contract(1, threads=[{
        "id": "PT-001-01", "thread": "失踪的钥匙",
        "status": "open", "deadline_chapter": 3,
    }]))

    def forbidden(*args, **kwargs):
        raise AssertionError("相同 ID 的结构错误不应调用模型")

    result = resolve_contract_identities(
        _contract(2, threads=[{
            "id": "PT-001-01", "thread": "失踪的钥匙",
            "status": "open", "deadline_chapter": 3,
        }]),
        str(tmp_path), 2, "current-model", forbidden,
    )

    assert result.contract["plot_thread_updates"] == []
    assert result.decisions[0]["action"] == "duplicate_open_removed"


def test_reused_id_for_related_thread_gets_new_program_id(tmp_path):
    _save(tmp_path, _contract(1, threads=[{
        "id": "PT-001-01", "thread": "失踪的钥匙",
        "status": "open", "deadline_chapter": 4,
    }]))

    def semantic_reply(prompt, model=None):
        return '{"relation":"related","similarity":0.77,"matched_id":null,"reason":"指纹是新的独立线索"}'

    result = resolve_contract_identities(
        _contract(2, threads=[{
            "id": "PT-001-01", "thread": "钥匙上的陌生指纹",
            "status": "open", "deadline_chapter": 4,
        }]),
        str(tmp_path), 2, "current-model", semantic_reply,
    )

    assert result.contract["plot_thread_updates"][0]["id"] == "PT-002-01"
    assert result.decisions[0]["action"] == "colliding_id_reassigned"


def test_semantically_same_close_reuses_canonical_id(tmp_path):
    _save(tmp_path, _contract(1, threads=[{
        "id": "PT-001-01", "thread": "失踪的钥匙",
        "status": "open", "deadline_chapter": 2,
    }]))
    prompts = []

    def semantic_reply(prompt, model=None):
        prompts.append((prompt, model))
        return json.dumps({
            "relation": "same", "similarity": 0.94,
            "matched_id": "PT-001-01", "reason": "均指向同一把失踪钥匙",
        }, ensure_ascii=False)

    result = resolve_contract_identities(
        _contract(2, threads=[{
            "id": "PT-002-07", "thread": "找回那把不见的钥匙", "status": "closed",
        }]),
        str(tmp_path), 2, "current-model", semantic_reply,
    )

    assert result.contract["plot_thread_updates"][0]["id"] == "PT-001-01"
    assert prompts[0][1] == "current-model"
    assert "不创作或改写故事" in prompts[0][0]


def test_related_thread_remains_independent(tmp_path):
    _save(tmp_path, _contract(1, threads=[{
        "id": "PT-001-01", "thread": "失踪的钥匙",
        "status": "open", "deadline_chapter": 4,
    }]))

    def semantic_reply(prompt, model=None):
        return '{"relation":"related","similarity":0.78,"matched_id":null,"reason":"同案相关但不是同一线索"}'

    result = resolve_contract_identities(
        _contract(2, threads=[{
            "id": "PT-002-01", "thread": "钥匙上的陌生指纹",
            "status": "open", "deadline_chapter": 4,
        }]),
        str(tmp_path), 2, "current-model", semantic_reply,
    )

    assert result.contract["plot_thread_updates"][0]["id"] == "PT-002-01"


def test_invalid_semantic_json_gets_one_format_retry_then_stays_uncertain(tmp_path):
    _save(tmp_path, _contract(1, threads=[{
        "id": "PT-001-01", "thread": "失踪的钥匙",
        "status": "open", "deadline_chapter": 4,
    }]))
    calls = []

    def invalid_reply(prompt, model=None):
        calls.append(prompt)
        return "无法判断"

    result = resolve_contract_identities(
        _contract(2, threads=[{
            "id": "PT-002-01", "thread": "钥匙去向",
            "status": "open", "deadline_chapter": 4,
        }]),
        str(tmp_path), 2, "current-model", invalid_reply,
    )

    assert len(calls) == 2
    assert "只把结论修复" in calls[1]
    assert result.contract["plot_thread_updates"][0]["id"] == "PT-002-01"
    assert result.decisions[0]["model_decision"]["relation"] == "uncertain"


def test_same_fact_moves_from_added_to_confirmed(tmp_path):
    _save(tmp_path, _contract(1, facts=[{
        "id": "F-001-01", "fact": "顾临的年龄", "value": "34岁",
    }]))

    def semantic_reply(prompt, model=None):
        return '{"relation":"same","similarity":0.91,"matched_id":"F-001-01","reason":"同一人物的同一属性"}'

    result = resolve_contract_identities(
        _contract(2, facts=[{
            "id": "F-002-01", "fact": "顾临年纪", "value": "三十四岁",
        }]),
        str(tmp_path), 2, "current-model", semantic_reply,
    )

    assert result.contract["facts_added"] == []
    assert result.contract["facts_confirmed"][0]["id"] == "F-001-01"


def test_reused_fact_id_for_different_fact_gets_new_program_id(tmp_path):
    _save(tmp_path, _contract(1, facts=[{
        "id": "F-001-01", "fact": "顾临的年龄", "value": "34岁",
    }]))

    def semantic_reply(prompt, model=None):
        return '{"relation":"different","similarity":0.12,"matched_id":null,"reason":"年龄与住址是不同事实"}'

    result = resolve_contract_identities(
        _contract(2, facts=[{
            "id": "F-001-01", "fact": "顾临的住址", "value": "东城区",
        }]),
        str(tmp_path), 2, "current-model", semantic_reply,
    )

    assert result.contract["facts_added"][0]["id"] == "F-002-01"
    assert result.decisions[0]["action"] == "colliding_id_reassigned"


def test_domain_clue_id_collision_gets_a_chapter_scoped_id(tmp_path):
    first = _contract(1)
    first["fair_play_clues"] = [{
        "id": "C001",
        "surface_meaning": "喉部的特殊缝合结",
        "true_meaning": "凶手模仿旧案手法",
    }]
    _save(tmp_path, first)
    second = _contract(2)
    second["fair_play_clues"] = [{
        "id": "C001",
        "surface_meaning": "手背上的喷溅血迹",
        "true_meaning": "血迹由高压设备喷涂伪造",
    }]

    result = resolve_contract_identities(
        second,
        str(tmp_path),
        2,
        "current-model",
        _decides("different", 0.12),
        profile=LEGAL_SUSPENSE,
    )

    assert result.contract["fair_play_clues"][0]["id"] == "C-002-01"
    assert result.decisions[-1]["action"] == "colliding_id_reassigned"


def test_semantically_same_clue_uses_the_canonical_id_and_meaning(tmp_path):
    first = _contract(1)
    first["fair_play_clues"] = [
        {
            "id": "C001",
            "surface_meaning": "喉部的特殊缝合结",
            "true_meaning": "凶手模仿旧案手法",
        },
        {
            "id": "C002",
            "surface_meaning": "手背上的喷溅血迹",
            "true_meaning": "血迹由高压设备喷涂伪造",
        },
    ]
    _save(tmp_path, first)
    second = _contract(2)
    second["fair_play_clues"] = [{
        "id": "C001",
        "surface_meaning": "梁浩手背的微量喷溅血迹",
        "true_meaning": "推弹器和喷涂设备制造了虚假射击物证",
    }]

    result = resolve_contract_identities(
        second,
        str(tmp_path),
        2,
        "current-model",
        _decides("same", 0.95, "C002"),
        profile=LEGAL_SUSPENSE,
    )

    clue = result.contract["fair_play_clues"][0]
    assert clue["id"] == "C002"
    assert clue["surface_meaning"] == "手背上的喷溅血迹"
    assert clue["true_meaning"] == "血迹由高压设备喷涂伪造"


def test_domain_only_repair_does_not_call_model_for_unrelated_core_records(tmp_path):
    first = _contract(1, facts=[{
        "id": "F-001-01", "fact": "死亡方式", "value": "枪伤",
    }])
    first["fair_play_clues"] = [{
        "id": "C001",
        "surface_meaning": "喉部的特殊缝合结",
        "true_meaning": "凶手模仿旧案手法",
    }]
    _save(tmp_path, first)
    second = _contract(2, facts=[{
        "id": "F-002-01", "fact": "逃亡路线", "value": "进入山林",
    }])
    second["fair_play_clues"] = [{
        "id": "C001",
        "surface_meaning": "手背上的喷溅血迹",
        "true_meaning": "血迹由设备喷涂伪造",
    }]
    calls = []

    def decide(prompt, model=None):
        calls.append(prompt)
        return json.dumps({
            "relation": "different",
            "similarity": 0.1,
            "matched_id": None,
            "reason": "不同线索",
        }, ensure_ascii=False)

    result = resolve_domain_identities(
        second,
        str(tmp_path),
        2,
        "current-model",
        decide,
        profile=LEGAL_SUSPENSE,
    )

    assert len(calls) == 1
    assert result.contract["facts_added"] == second["facts_added"]
    assert result.contract["fair_play_clues"][0]["id"] == "C-002-01"


def test_character_attribute_written_two_ways_is_canonicalised(tmp_path):
    """「萨姆·金」和「山姆·金」写的是同一个人的同一项属性，必须并成一条。"""
    _save(tmp_path, _contract(1, attributes=[{
        "id": "CU-001-01", "character": "萨姆·金",
        "attribute": "执业年限", "value": "三十年", "stable": True,
    }]))

    result = resolve_contract_identities(
        _contract(9, attributes=[{
            "id": "CU-009-01", "character": "山姆·金",
            "attribute": "执业年限", "value": "三十年", "stable": True,
        }]),
        str(tmp_path), 9, "m", _decides("same", 0.93, "CU-001-01"),
    )

    record = result.contract["character_updates"][0]
    assert record["id"] == "CU-001-01"
    assert record["character"] == "萨姆·金"
    assert result.decisions[-1]["action"] == "canonicalized"


def test_same_character_attribute_key_needs_no_model_call(tmp_path):
    """写法完全一致时靠复合键直接对齐，不该浪费一次语义判断。"""
    _save(tmp_path, _contract(1, attributes=[{
        "id": "CU-001-01", "character": "金伯利·罗宾逊",
        "attribute": "在法医中心的工龄", "value": "三十四年", "stable": True,
    }]))

    def forbidden(*args, **kwargs):
        raise AssertionError("复合键已经能对齐，不应调用模型")

    result = resolve_contract_identities(
        _contract(9, attributes=[{
            "id": "CU-009-01", "character": "金伯利·罗宾逊",
            "attribute": "在法医中心的工龄", "value": "34年", "stable": True,
        }]),
        str(tmp_path), 9, "m", forbidden,
    )

    # 三十四年 与 34年 是同一个值，应当归一而不是判成冲突。
    assert result.contract["character_updates"][0]["id"] == "CU-001-01"
    assert result.warnings == []


def test_conflicting_stable_attribute_warns_with_both_values(tmp_path):
    _save(tmp_path, _contract(1, attributes=[{
        "id": "CU-001-01", "character": "金伯利·罗宾逊",
        "attribute": "在法医中心的工龄", "value": "三十四年", "stable": True,
    }]))

    result = resolve_contract_identities(
        _contract(17, attributes=[{
            "id": "CU-017-01", "character": "金伯利·罗宾逊",
            "attribute": "在法医中心的工龄", "value": "三十七年", "stable": True,
        }]),
        str(tmp_path), 17, "m", _decides("different", 0.2),
    )

    assert len(result.warnings) == 1
    warning = result.warnings[0]
    assert "三十四年" in warning and "三十七年" in warning
    assert warning.count("CU-001-01") == 1
    # 未自动合并，冲突留给验收闸门确定性拦截。
    assert result.contract["character_updates"][0]["id"] == "CU-017-01"


def test_fact_value_conflict_warning_names_both_values(tmp_path):
    """告警要说清楚哪两个取值在打架，只报 id 等于什么都没说。"""
    _save(tmp_path, _contract(1, facts=[{
        "id": "F-001-01", "fact": "保罗·米勒的死亡方式", "value": "后巷近距离两枪",
    }]))

    result = resolve_contract_identities(
        _contract(9, facts=[{
            "id": "F-001-01", "fact": "保罗·米勒的死亡方式", "value": "解剖台钢丝绞杀",
        }]),
        str(tmp_path), 9, "m", _decides("same", 0.95, "F-001-01"),
    )

    assert len(result.warnings) == 1
    warning = result.warnings[0]
    assert "后巷近距离两枪" in warning
    assert "解剖台钢丝绞杀" in warning
    assert warning.count("F-001-01") == 1


def test_replanned_chapter_keeps_a_thread_a_later_chapter_closes(tmp_path):
    """A near-duplicate open must survive when a planned successor closes it."""
    _save(tmp_path, _contract(1, threads=[{
        "id": "PT-001-01", "thread": "失踪的钥匙",
        "status": "open", "deadline_chapter": 8,
    }]))
    _save(tmp_path, _contract(5, threads=[{
        "id": "PT-005-01", "thread": "被调换的证物袋",
        "status": "open", "deadline_chapter": 8,
    }]))
    _save(tmp_path, _contract(8, threads=[
        {"id": "PT-001-01", "thread": "失踪的钥匙", "status": "closed"},
        {"id": "PT-005-01", "thread": "被调换的证物袋", "status": "closed"},
    ]))

    result = resolve_contract_identities(
        _contract(5, threads=[{
            "id": "PT-005-01", "thread": "被调换的证物袋",
            "status": "open", "deadline_chapter": 8,
        }]),
        str(tmp_path), 5, "current-model", _decides("same", 0.95, "PT-001-01"),
    )

    kept = [item["id"] for item in result.contract["plot_thread_updates"]]
    assert kept == ["PT-005-01"]
    assert any("后续章节要了结" in warning for warning in result.warnings)


def test_extension_keeps_its_thread_id_and_needs_no_model_call(tmp_path):
    """延期是同一条悬念的再次声明，不是新线索。

    到期未了结时，校验器给模型的修复指令就是写一条
    {"id": <原 id>, "status": "open", "extend": true, "deadline_chapter": N}。
    那条建议里没有 thread 字段，一旦落进语义比对就会被判成「撞了 ID 的新线索」
    并改名——原悬念仍然到期未处理，模型照着提示改也永远修不好。
    """
    _save(tmp_path, _contract(1, threads=[{
        "id": "PT-001-01", "thread": "证物袋被调换",
        "status": "open", "deadline_chapter": 2,
    }]))

    def forbidden(*args, **kwargs):
        raise AssertionError("延期是结构性声明，不需要语义判定")

    result = resolve_contract_identities(
        _contract(2, threads=[{
            "id": "PT-001-01", "status": "open",
            "extend": True, "deadline_chapter": 5,
        }]),
        str(tmp_path), 2, "current-model", forbidden,
    )

    threads = result.contract["plot_thread_updates"]
    assert len(threads) == 1
    assert threads[0]["id"] == "PT-001-01"
    assert threads[0]["deadline_chapter"] == 5
    assert threads[0]["extend"] is True


def test_the_repair_the_error_message_suggests_actually_passes(tmp_path):
    """端到端：按错误提示写出的修复，必须真的能通过校验。"""
    from core.generation.planning_contract import (
        collect_history_defects,
        downstream_obligations,
        load_planning_contracts,
        validate_planning_contract,
    )

    _save(tmp_path, _contract(1, threads=[{
        "id": "PT-001-01", "thread": "证物袋被调换",
        "status": "open", "deadline_chapter": 2,
    }]))

    resolution = resolve_contract_identities(
        _contract(2, threads=[{
            "id": "PT-001-01", "status": "open",
            "extend": True, "deadline_chapter": 5,
        }]),
        str(tmp_path), 2, "current-model", _decides("unrelated", 0.0),
    )
    contract = validate_planning_contract(resolution.contract, 2)

    defects = collect_history_defects(
        contract,
        load_planning_contracts(str(tmp_path)),
        2,
        obligations=downstream_obligations(str(tmp_path), 2),
    )
    assert defects == [], [str(defect) for defect in defects]


def test_a_genuinely_new_thread_that_collides_is_still_renamed(tmp_path):
    """过滤只放行 extend：没有 extend 的撞号仍然要改名。"""
    _save(tmp_path, _contract(1, threads=[{
        "id": "PT-001-01", "thread": "失踪的钥匙",
        "status": "open", "deadline_chapter": 6,
    }]))

    result = resolve_contract_identities(
        _contract(2, threads=[{
            "id": "PT-001-01", "thread": "完全不同的另一条悬念",
            "status": "open", "deadline_chapter": 8,
        }]),
        str(tmp_path), 2, "current-model", _decides("unrelated", 0.0),
    )

    assert result.contract["plot_thread_updates"][0]["id"] != "PT-001-01"
