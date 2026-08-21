"""Tests for ChapterDelta extraction and chapter acceptance gates."""

import json

import pytest

from core.generation.chapter_acceptance import (
    ChapterAcceptanceError,
    ChapterAcceptanceService,
)
from core.generation.story_ledger import StoryLedgerManager


def _contract(chapter, clue_meaning="设备时钟被调整"):
    return {
        "chapter": chapter,
        "core_question": "收据为何晚了三十二分钟？",
        "reader_knows_after": ["收据时间与门禁记录冲突"],
        "character_knowledge_after": {"林衡": ["门禁记录可能不可信"]},
        "fair_play_clues": [
            {
                "id": "C001",
                "surface_meaning": "打印延迟",
                "true_meaning": clue_meaning,
            }
        ],
        "evidence_updates": [
            {
                "id": "E001",
                "item": "门禁记录",
                "status": "封存",
                "custodian": "鉴证科",
            }
        ],
        "personal_cost": "林衡失去案卷访问权",
        "cost_character": "林衡",
        "irreversible_change": "调查转入私下",
    }


def _review():
    return {"average_score": 3.8, "passed": True}


def _write_chapter(tmp_path, chapter, content):
    path = tmp_path / "story" / "content" / "chapters" / f"chapter_{chapter}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _manager(tmp_path):
    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Mystery", "Subgenre": "Legal Thriller"})
    return manager


def test_acceptance_commits_delta_and_advances_revision(tmp_path):
    manager = _manager(tmp_path)
    service = ChapterAcceptanceService(manager)
    content = "林衡在23:45核对门禁记录。收据晚了32分钟。"
    chapter_path = _write_chapter(tmp_path, 1, content)

    result = service.accept(
        chapter_number=1,
        reviewed_content=content,
        contract=_contract(1),
        chapter_review=_review(),
        base_revision=0,
        chapter_path=str(chapter_path),
    )

    assert result.committed_revision == 1
    assert result.artifact_report.passed
    assert result.consistency_report.passed
    assert result.delta.text_signals["time_expressions"] == ["23:45"]
    assert "32分钟" in result.delta.text_signals["numeric_expressions"]

    delta = json.loads(open(result.delta_path, encoding="utf-8").read())
    ledger = manager.load_suspense_ledger()
    assert delta["base_revision"] == 0
    assert delta["content_hash"] == ledger["accepted_chapters"][0]["content_hash"]
    assert ledger["revision"] == 1
    assert ledger["accepted_chapters"][0]["committed_revision"] == 1
    assert len(ledger["chapter_commits"]) == 1


def test_acceptance_rejects_saved_content_changed_after_review(tmp_path):
    manager = _manager(tmp_path)
    service = ChapterAcceptanceService(manager)
    chapter_path = _write_chapter(tmp_path, 1, "正文被保存阶段改写。")

    with pytest.raises(ChapterAcceptanceError) as exc_info:
        service.accept(
            chapter_number=1,
            reviewed_content="通过审阅的正文。",
            contract=_contract(1),
            chapter_review=_review(),
            base_revision=0,
            chapter_path=str(chapter_path),
        )

    assert exc_info.value.report.issues[0].code == "saved_content_mismatch"
    ledger = manager.load_suspense_ledger()
    assert ledger["revision"] == 0
    assert ledger["accepted_chapters"] == []
    assert ledger["unresolved_conflicts"][0]["stage"] == "artifact_validation"


def test_acceptance_rejects_stale_base_revision(tmp_path):
    manager = _manager(tmp_path)
    service = ChapterAcceptanceService(manager)

    first_content = "第一章确认门禁记录存在异常。"
    first_path = _write_chapter(tmp_path, 1, first_content)
    service.accept(1, first_content, _contract(1), _review(), 0, str(first_path))

    second_content = "第二章仍基于旧状态生成。"
    second_path = _write_chapter(tmp_path, 2, second_content)
    with pytest.raises(ChapterAcceptanceError) as exc_info:
        service.accept(2, second_content, _contract(2), _review(), 0, str(second_path))

    assert any(issue.code == "revision_conflict" for issue in exc_info.value.report.issues)
    ledger = manager.load_suspense_ledger()
    assert ledger["revision"] == 1
    assert [entry["chapter"] for entry in ledger["accepted_chapters"]] == [1]


def test_acceptance_blocks_conflicting_stable_clue_fact(tmp_path):
    manager = _manager(tmp_path)
    service = ChapterAcceptanceService(manager)

    first_content = "第一章提出打印延迟的表面解释。"
    first_path = _write_chapter(tmp_path, 1, first_content)
    service.accept(1, first_content, _contract(1), _review(), 0, str(first_path))

    second_content = "第二章再次解释同一线索。"
    second_path = _write_chapter(tmp_path, 2, second_content)
    conflicting_contract = _contract(2, clue_meaning="记录完全准确，从未被调整")

    with pytest.raises(ChapterAcceptanceError) as exc_info:
        service.accept(2, second_content, conflicting_contract, _review(), 1, str(second_path))

    issue = next(issue for issue in exc_info.value.report.issues if issue.code == "clue_fact_conflict")
    # A clashing stable value goes back to the contract stage to be declared as
    # either a slip or a deliberate reversal; only a declared reversal
    # (fact_contradiction) is a question for the author.
    assert issue.repair_target == "contract"
    assert issue.details["id"] == "C001"
    assert manager.current_revision() == 1


def test_successful_retry_resolves_same_chapter_conflict(tmp_path):
    manager = _manager(tmp_path)
    service = ChapterAcceptanceService(manager)
    chapter_path = _write_chapter(tmp_path, 1, "修复后的正文。")

    with pytest.raises(ChapterAcceptanceError):
        service.accept(1, "旧的审阅正文。", _contract(1), _review(), 0, str(chapter_path))

    ledger = manager.load_suspense_ledger()
    assert len(ledger["unresolved_conflicts"]) == 1

    result = service.accept(1, "修复后的正文。", _contract(1), _review(), 0, str(chapter_path))

    ledger = manager.load_suspense_ledger()
    assert result.committed_revision == 1
    assert ledger["unresolved_conflicts"] == []
    assert ledger["resolved_conflicts"][0]["committed_revision"] == 1


def test_unrelated_unresolved_conflict_blocks_next_chapter(tmp_path):
    manager = _manager(tmp_path)
    service = ChapterAcceptanceService(manager)
    manager.record_conflicts(
        chapter_number=1,
        base_revision=0,
        report={"stage": "canon_consistency", "passed": False, "issues": []},
    )
    chapter_path = _write_chapter(tmp_path, 2, "第二章正文。")

    with pytest.raises(ChapterAcceptanceError) as exc_info:
        service.accept(2, "第二章正文。", _contract(2), _review(), 0, str(chapter_path))

    assert any(
        issue.code == "unresolved_project_conflicts" for issue in exc_info.value.report.issues
    )
    assert manager.current_revision() == 0


def test_same_content_with_changed_contract_is_not_idempotent(tmp_path):
    manager = _manager(tmp_path)
    service = ChapterAcceptanceService(manager)
    content = "同一版正文。"
    chapter_path = _write_chapter(tmp_path, 1, content)

    first = service.accept(1, content, _contract(1), _review(), 0, str(chapter_path))
    changed_contract = _contract(1)
    changed_contract["reader_knows_after"] = ["读者获得了另一条状态信息"]
    second = service.accept(1, content, changed_contract, _review(), 1, str(chapter_path))

    assert first.committed_revision == 1
    assert second.committed_revision == 2
    assert first.delta_path != second.delta_path
    assert len(manager.load_suspense_ledger()["chapter_commits"]) == 2


def test_contract_timestamp_change_remains_idempotent(tmp_path):
    manager = _manager(tmp_path)
    service = ChapterAcceptanceService(manager)
    content = "正文和业务契约都没有变化。"
    chapter_path = _write_chapter(tmp_path, 1, content)
    first_contract = _contract(1)
    first_contract["updated_at"] = "2026-01-01T00:00:00"
    second_contract = _contract(1)
    second_contract["updated_at"] = "2026-08-17T12:00:00"

    first = service.accept(1, content, first_contract, _review(), 0, str(chapter_path))
    second = service.accept(1, content, second_contract, _review(), 0, str(chapter_path))

    assert first.committed_revision == 1
    assert second.committed_revision == 1
    assert first.delta.contract_hash == second.delta.contract_hash
    assert len(manager.load_suspense_ledger()["chapter_commits"]) == 1


def _long_range_contract(chapter, *, death_method, murder_time):
    """Contract carrying the long-range state slots (facts / timeline)."""
    contract = _contract(chapter)
    contract["facts_added"] = [
        {
            "id": "F001",
            "fact": "保罗·米勒的死亡方式",
            "value": death_method,
            "first_stated_at": "scene_1",
        }
    ]
    contract["timeline_events"] = [
        {
            "id": "TL001",
            "event": "保罗·米勒遇害",
            "time": murder_time,
            "location_id": "法医中心后巷",
        }
    ]
    return contract


def test_contract_fact_and_timeline_slots_reach_the_delta(tmp_path):
    """The schema fields must actually flow into ChapterDelta, not stay empty."""
    manager = _manager(tmp_path)
    service = ChapterAcceptanceService(manager)
    content = "第一章确立死亡方式与遇害时刻。"
    path = _write_chapter(tmp_path, 1, content)

    result = service.accept(
        1,
        content,
        _long_range_contract(1, death_method="后巷近距离两枪", murder_time="00:12"),
        _review(),
        0,
        str(path),
    )

    assert result.delta.facts_added[0]["value"] == "后巷近距离两枪"
    assert result.delta.timeline_events[0]["time"] == "00:12"


def test_acceptance_blocks_contradicting_fact_value(tmp_path):
    """Chapter 9 re-inventing the victim's manner of death must be rejected."""
    manager = _manager(tmp_path)
    service = ChapterAcceptanceService(manager)

    first_content = "第一章：保罗在后巷中枪。"
    first_path = _write_chapter(tmp_path, 1, first_content)
    service.accept(
        1,
        first_content,
        _long_range_contract(1, death_method="后巷近距离两枪", murder_time="00:12"),
        _review(),
        0,
        str(first_path),
    )

    second_content = "第九章：山姆回忆保罗在解剖台被绞杀。"
    second_path = _write_chapter(tmp_path, 9, second_content)

    with pytest.raises(ChapterAcceptanceError) as exc_info:
        service.accept(
            9,
            second_content,
            _long_range_contract(9, death_method="解剖台钢丝绞杀", murder_time="00:12"),
            _review(),
            1,
            str(second_path),
        )

    issue = next(i for i in exc_info.value.report.issues if i.code == "fact_fact_conflict")
    assert issue.details["id"] == "F001"
    assert issue.details["existing"] == "后巷近距离两枪"
    assert issue.details["proposed"] == "解剖台钢丝绞杀"
    assert manager.current_revision() == 1


def test_acceptance_blocks_contradicting_timeline_time(tmp_path):
    """The same event must not acquire a second clock time in a later chapter."""
    manager = _manager(tmp_path)
    service = ChapterAcceptanceService(manager)

    first_content = "第一章：零点十二分。"
    first_path = _write_chapter(tmp_path, 1, first_content)
    service.accept(
        1,
        first_content,
        _long_range_contract(1, death_method="后巷近距离两枪", murder_time="00:12"),
        _review(),
        0,
        str(first_path),
    )

    fourth_content = "第四章：山姆说是三点零八分。"
    fourth_path = _write_chapter(tmp_path, 4, fourth_content)

    with pytest.raises(ChapterAcceptanceError) as exc_info:
        service.accept(
            4,
            fourth_content,
            _long_range_contract(4, death_method="后巷近距离两枪", murder_time="03:08"),
            _review(),
            1,
            str(fourth_path),
        )

    issue = next(i for i in exc_info.value.report.issues if i.code == "timeline_fact_conflict")
    assert issue.details["id"] == "TL001"
    assert issue.details["field"] == "time"
    assert manager.current_revision() == 1


def test_new_id_cannot_smuggle_a_contradicting_fact(tmp_path):
    """Minting a fresh id for the same fact must not bypass the gate."""
    manager = _manager(tmp_path)
    service = ChapterAcceptanceService(manager)

    first_content = "第一章：保罗在后巷中枪。"
    first_path = _write_chapter(tmp_path, 1, first_content)
    service.accept(
        1,
        first_content,
        _long_range_contract(1, death_method="后巷近距离两枪", murder_time="00:12"),
        _review(),
        0,
        str(first_path),
    )

    # Same fact, contradicting value, but the model invented a new id.
    renamed = _long_range_contract(9, death_method="解剖台钢丝绞杀", murder_time="00:12")
    renamed["facts_added"][0]["id"] = "F077"
    second_content = "第九章：改用了新的事实编号。"
    second_path = _write_chapter(tmp_path, 9, second_content)

    with pytest.raises(ChapterAcceptanceError) as exc_info:
        service.accept(9, second_content, renamed, _review(), 1, str(second_path))

    issues = exc_info.value.report.issues
    conflict = next(i for i in issues if i.code == "fact_fact_conflict")
    assert conflict.details["id"] == "F001"
    assert conflict.details["proposed_id"] == "F077"
    assert any(i.code == "fact_identity_reused" for i in issues)
    assert manager.current_revision() == 1


def test_id_churn_without_contradiction_is_only_a_warning(tmp_path):
    """Re-numbering a record with unchanged content should not block the commit."""
    manager = _manager(tmp_path)
    service = ChapterAcceptanceService(manager)

    first_content = "第一章确立事实。"
    first_path = _write_chapter(tmp_path, 1, first_content)
    service.accept(
        1,
        first_content,
        _long_range_contract(1, death_method="后巷近距离两枪", murder_time="00:12"),
        _review(),
        0,
        str(first_path),
    )

    renamed = _long_range_contract(2, death_method="后巷近距离两枪", murder_time="00:12")
    renamed["facts_added"][0]["id"] = "F077"
    second_content = "第二章复述同一事实但换了编号。"
    second_path = _write_chapter(tmp_path, 2, second_content)

    result = service.accept(2, second_content, renamed, _review(), 1, str(second_path))

    assert result.committed_revision == 2
    assert any(
        issue.code == "fact_identity_reused"
        for issue in result.consistency_report.issues
    )


def _thread_contract(chapter, *, thread_id="PT001", status="open", deadline=0):
    contract = _contract(chapter)
    contract["plot_thread_updates"] = [
        {
            "id": thread_id,
            "thread": "沃伦·凯斯被栽赃的指控",
            "status": status,
            "deadline_chapter": deadline,
        }
    ]
    return contract


def test_thread_open_past_deadline_blocks_at_the_deadline_chapter(tmp_path):
    manager = _manager(tmp_path)
    service = ChapterAcceptanceService(manager)

    opening = "第二章：接下委托。"
    opening_path = _write_chapter(tmp_path, 2, opening)
    service.accept(
        2, opening, _thread_contract(2, deadline=5), _review(), 0, str(opening_path)
    )

    content = "第五章：仍然没有交代沃伦。"
    path = _write_chapter(tmp_path, 5, content)
    with pytest.raises(ChapterAcceptanceError) as exc_info:
        service.accept(5, content, _contract(5), _review(), 1, str(path))

    issue = next(
        i for i in exc_info.value.report.issues if i.code == "plot_thread_overdue"
    )
    assert issue.severity == "blocking"
    assert issue.details["deadline_chapter"] == 5
    assert manager.current_revision() == 1


def test_closing_the_thread_in_the_deadline_chapter_passes(tmp_path):
    manager = _manager(tmp_path)
    service = ChapterAcceptanceService(manager)

    opening = "第二章：接下委托。"
    opening_path = _write_chapter(tmp_path, 2, opening)
    service.accept(
        2, opening, _thread_contract(2, deadline=5), _review(), 0, str(opening_path)
    )

    content = "第五章：沃伦当庭无罪释放。"
    path = _write_chapter(tmp_path, 5, content)
    result = service.accept(
        5,
        content,
        _thread_contract(5, status="closed", deadline=5),
        _review(),
        1,
        str(path),
    )

    assert result.committed_revision == 2
    threads = manager.load_suspense_ledger()["plot_threads"]
    assert next(t for t in threads if t["id"] == "PT001")["status"] == "closed"


def test_thread_without_declared_deadline_never_blocks(tmp_path):
    manager = _manager(tmp_path)
    service = ChapterAcceptanceService(manager)

    opening = "第二章：开了一条没有期限的线。"
    opening_path = _write_chapter(tmp_path, 2, opening)
    service.accept(2, opening, _thread_contract(2), _review(), 0, str(opening_path))

    content = "第二十章：这条线仍然开着。"
    path = _write_chapter(tmp_path, 20, content)
    result = service.accept(20, content, _contract(20), _review(), 1, str(path))

    assert result.committed_revision == 2


def test_declared_time_absent_from_prose_is_reported(tmp_path):
    """契约声明零点十二分、正文却写三点零八分，必须被发现。"""
    manager = _manager(tmp_path)
    service = ChapterAcceptanceService(manager)

    content = "山姆说他看了表，三点零八分，火警还没响。"
    path = _write_chapter(tmp_path, 4, content)
    result = service.accept(
        4,
        content,
        _long_range_contract(4, death_method="后巷近距离两枪", murder_time="00:12"),
        _review(),
        0,
        str(path),
    )

    issue = next(
        i
        for i in result.artifact_report.issues
        if i.code == "declared_time_absent_from_prose"
    )
    assert issue.severity == "warning"
    assert issue.details["declared_time"] == "00:12"


def test_declared_time_present_in_prose_is_not_reported(tmp_path):
    manager = _manager(tmp_path)
    service = ChapterAcceptanceService(manager)

    content = "墙上的挂钟停在零点十二分，枪声就是那一刻响的。"
    path = _write_chapter(tmp_path, 1, content)
    result = service.accept(
        1,
        content,
        _long_range_contract(1, death_method="后巷近距离两枪", murder_time="00:12"),
        _review(),
        0,
        str(path),
    )

    assert not any(
        i.code == "declared_time_absent_from_prose"
        for i in result.artifact_report.issues
    )


def test_prose_without_any_clock_time_is_not_reported(tmp_path):
    """正文完全不提时刻时不应误报——事件可能发生在幕后。"""
    manager = _manager(tmp_path)
    service = ChapterAcceptanceService(manager)

    content = "他们在雨里走了很久，谁也没有说话。"
    path = _write_chapter(tmp_path, 7, content)
    result = service.accept(
        7,
        content,
        _long_range_contract(7, death_method="后巷近距离两枪", murder_time="00:12"),
        _review(),
        0,
        str(path),
    )

    assert not any(
        i.code == "declared_time_absent_from_prose"
        for i in result.artifact_report.issues
    )


def _attribute_contract(chapter, *, value, attribute="在法医中心的工龄", stable=True):
    contract = _contract(chapter)
    contract["character_updates"] = [
        {
            "id": f"CU{chapter:03d}",
            "character": "金伯利·罗宾逊",
            "attribute": attribute,
            "value": value,
            "stable": stable,
        }
    ]
    return contract


def test_stable_attribute_drift_is_blocked(tmp_path):
    """金伯利的工龄从三十四年漂到三十七年，必须被拦住。"""
    manager = _manager(tmp_path)
    service = ChapterAcceptanceService(manager)

    first = "她在法医中心工作了三十四年。"
    first_path = _write_chapter(tmp_path, 1, first)
    service.accept(
        1, first, _attribute_contract(1, value="三十四年"), _review(), 0, str(first_path)
    )

    second = "为警署做了三十七年法医比对。"
    second_path = _write_chapter(tmp_path, 17, second)
    with pytest.raises(ChapterAcceptanceError) as exc_info:
        service.accept(
            17,
            second,
            _attribute_contract(17, value="三十七年"),
            _review(),
            1,
            str(second_path),
        )

    issue = next(
        i
        for i in exc_info.value.report.issues
        if i.code == "character_attribute_fact_conflict"
    )
    assert issue.details["existing"] == "三十四年"
    assert issue.details["proposed"] == "三十七年"
    assert manager.current_revision() == 1


def test_same_number_written_differently_is_not_a_conflict(tmp_path):
    """三十四年 与 34年 是同一个值，不该报冲突。"""
    manager = _manager(tmp_path)
    service = ChapterAcceptanceService(manager)

    first = "她在法医中心工作了三十四年。"
    first_path = _write_chapter(tmp_path, 1, first)
    service.accept(
        1, first, _attribute_contract(1, value="三十四年"), _review(), 0, str(first_path)
    )

    second = "工龄34年的资深技术员。"
    second_path = _write_chapter(tmp_path, 2, second)
    result = service.accept(
        2, second, _attribute_contract(2, value="34年"), _review(), 1, str(second_path)
    )

    assert result.committed_revision == 2


def test_unstable_attribute_may_change_freely(tmp_path):
    """伤势这类状态属性会随剧情变化，不应被锁死。"""
    manager = _manager(tmp_path)
    service = ChapterAcceptanceService(manager)

    first = "他的右臂中了一枪。"
    first_path = _write_chapter(tmp_path, 8, first)
    service.accept(
        8,
        first,
        _attribute_contract(8, attribute="伤势", value="右臂贯穿伤", stable=False),
        _review(),
        0,
        str(first_path),
    )

    second = "断了两根肋骨。"
    second_path = _write_chapter(tmp_path, 13, second)
    result = service.accept(
        13,
        second,
        _attribute_contract(13, attribute="伤势", value="左侧两根肋骨骨折", stable=False),
        _review(),
        1,
        str(second_path),
    )

    assert result.committed_revision == 2


def test_two_attributes_of_one_character_do_not_overwrite(tmp_path):
    """同一人物的多项属性必须各自成条，不能互相覆盖。"""
    manager = _manager(tmp_path)
    service = ChapterAcceptanceService(manager)

    content = "六十七岁的她在法医中心工作了三十四年。"
    path = _write_chapter(tmp_path, 1, content)
    contract = _attribute_contract(1, value="三十四年")
    contract["character_updates"].append(
        {
            "id": "CU002",
            "character": "金伯利·罗宾逊",
            "attribute": "年龄",
            "value": "六十七岁",
            "stable": True,
        }
    )
    service.accept(1, content, contract, _review(), 0, str(path))

    stored = manager.load_suspense_ledger()["character_updates"]
    attributes = {r["attribute"]: r["value"] for r in stored}
    assert attributes == {"在法医中心的工龄": "三十四年", "年龄": "六十七岁"}


def test_declared_number_absent_from_prose_is_reported(tmp_path):
    manager = _manager(tmp_path)
    service = ChapterAcceptanceService(manager)

    content = "她在法医中心工作了四十年，见过太多尸体。"
    path = _write_chapter(tmp_path, 6, content)
    result = service.accept(
        6, content, _attribute_contract(6, value="三十四年"), _review(), 0, str(path)
    )

    issue = next(
        i
        for i in result.artifact_report.issues
        if i.code == "declared_number_absent_from_prose"
    )
    assert issue.severity == "warning"
    assert issue.details["declared_number"] == 34.0
