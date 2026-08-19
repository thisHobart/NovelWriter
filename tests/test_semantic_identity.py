import json

from core.generation.semantic_identity import resolve_contract_identities
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
