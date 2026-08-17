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
    assert issue.repair_target == "human_decision"
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
