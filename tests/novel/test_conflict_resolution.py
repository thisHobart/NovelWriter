"""A ruling the author makes has to actually unblock the project.

Before this, ``unresolved_conflicts`` could only be cleared by the same chapter
being accepted again — and since the gate refuses to commit while any *other*
chapter has an open conflict, one undecided clash froze everything.
"""

import logging

import pytest

from agents.review.domain_review_agent import DomainReview
from core.generation.chapter_acceptance import (
    ChapterAcceptanceError,
    ChapterAcceptanceService,
    ValidationIssue,
)
from core.generation.chapter_generation_loop import (
    ChapterGenerationLoop,
    ChapterLoopResult,
    QualityGateError,
)
from core.generation.story_ledger import StoryLedgerManager
from core.generation.conflict_briefing import (
    ACCEPT_REVERSAL,
    KEEP_EXISTING,
    apply_decision,
    build_briefing,
)

WORKED = "在法医中心的工龄"


def _contract(chapter, value):
    return {
        "chapter": chapter,
        "origin": "scene_planning",
        "schema_version": 2,
        "core_question": "谁动了证物",
        "facts_added": [],
        "facts_confirmed": [],
        "facts_contradicted": [],
        "timeline_events": [],
        "character_updates": [
            {
                "id": "CU-001-01",
                "character": "金伯利",
                "attribute": WORKED,
                "value": value,
                "stable": True,
            }
        ],
        "plot_thread_updates": [],
        "scene_boundaries": [],
    }


def _passing():
    return DomainReview(stage="chapter", passed=True)


class _Reviewer:
    def __init__(self, decision, reason=""):
        self.decision = decision
        self.reason = reason

    def decide_fact_conflicts(self, chapter_number, conflicts):
        return {
            "decisions": [
                {"id": item["id"], "field": item["field"],
                 "decision": self.decision, "reason": self.reason}
                for item in conflicts
            ]
        }


def _write(tmp_path, chapter, body):
    path = tmp_path / "story" / "content" / "chapters" / f"chapter_{chapter}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return str(path)


def _loop(tmp_path, manager, service, reviewer):
    loop = ChapterGenerationLoop.__new__(ChapterGenerationLoop)
    loop.output_dir = str(tmp_path)
    loop.ledger = manager
    loop.acceptance_service = service
    loop.reviewer = reviewer
    loop.logger = logging.getLogger("conflict-test")
    loop.max_acceptance_retries = 2
    return loop


@pytest.fixture
def blocked(tmp_path):
    """Chapter 1 accepted; chapter 9 blocked on a declared reversal."""
    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Mystery", "Subgenre": "Legal Thriller"})
    service = ChapterAcceptanceService(manager)
    body1 = "金伯利在法医中心干了三十四年。"
    service.accept(
        1, body1, _contract(1, "三十四年"), {"stage": "chapter", "passed": True},
        0, _write(tmp_path, 1, body1),
    )

    body9 = "金伯利在法医中心干了三十七年。"
    path9 = _write(tmp_path, 9, body9)
    result = ChapterLoopResult(
        scenes=[body9], contract=_contract(9, "三十七年"),
        base_revision=manager.current_revision(),
        plan_content="### 场景 1：占位\n规划", plan_revised=False,
        plan_review=_passing(), scene_reviews=[_passing()], chapter_review=_passing(),
    )
    reviewer = _Reviewer("contradict", reason="第 9 章揭穿了伪造的工龄记录")
    with pytest.raises(ChapterAcceptanceError) as excinfo:
        _loop(tmp_path, manager, service, reviewer).accept_result(9, result, path9)
    return manager, service, result, path9, excinfo.value


def test_the_briefing_shows_both_values_and_the_stated_reason(blocked):
    _, _, _, _, error = blocked

    briefing = build_briefing(9, error.report.blocking_issues)

    assert briefing.is_decidable
    choice = briefing.choices[0]
    assert choice.record_id == "CU-001-01"
    assert choice.proposed == "三十七年"
    assert choice.reason == "第 9 章揭穿了伪造的工龄记录"
    assert choice.is_declared_reversal


def test_an_unresolvable_report_offers_no_false_choice():
    issues = [ValidationIssue("revision_conflict", "base_revision 已过期", repair_target="rebase")]

    briefing = build_briefing(9, issues)

    assert not briefing.is_decidable
    assert briefing.other_messages == ["base_revision 已过期"]


def test_accepting_the_reversal_lets_the_chapter_commit(tmp_path, blocked):
    manager, service, result, path9, error = blocked
    briefing = build_briefing(9, error.report.blocking_issues)

    apply_decision(manager, 9, briefing, ACCEPT_REVERSAL, note="作者确认")

    reviewer = _Reviewer("contradict", reason="第 9 章揭穿了伪造的工龄记录")
    with pytest.raises(QualityGateError, match="旧正文禁止提交"):
        _loop(tmp_path, manager, service, reviewer).accept_result(9, result, path9)

    pending = manager.pending_chapter_regeneration(9)
    regenerated_body = "复核原始档案后，金伯利确认自己已在法医中心工作三十七年。"
    regenerated = ChapterLoopResult(
        scenes=[regenerated_body],
        contract=manager.load_contract(9),
        base_revision=manager.current_revision(),
        plan_content=result.plan_content,
        plan_revised=False,
        plan_review=_passing(),
        scene_reviews=[_passing()],
        chapter_review=_passing(),
        regeneration_marker=pending["id"],
    )
    _write(tmp_path, 9, regenerated_body)
    _loop(tmp_path, manager, service, reviewer).accept_result(9, regenerated, path9)

    assert manager.current_revision() == 2
    assert manager.pending_chapter_regeneration(9) is None
    committed = manager.load_suspense_ledger()["character_updates"]
    assert [item["value"] for item in committed if item["id"] == "CU-001-01"] == ["三十七年"]


def test_a_ruling_closes_the_conflict_that_was_freezing_other_chapters(tmp_path, blocked):
    manager, _, _, _, error = blocked
    assert manager.open_conflicts(), "the blocked chapter should have left a conflict"

    apply_decision(manager, 9, build_briefing(9, error.report.blocking_issues), KEEP_EXISTING)

    assert manager.open_conflicts() == []
    closed = manager.load_suspense_ledger()["resolved_conflicts"]
    assert closed[-1]["resolution"] == "author_kept_existing_canon"
    assert closed[-1]["resolved_by"] == "human"
    assert manager.pending_chapter_regeneration(9) is not None
    contract = manager.load_contract(9)
    assert contract["character_updates"][0]["value"] == "三十四年"
    assert contract["facts_contradicted"] == []


def test_an_open_conflict_blocks_an_unrelated_chapter_until_it_is_ruled_on(
    tmp_path, blocked
):
    """The reason a stuck conflict had to become resolvable at all."""
    manager, service, _, _, error = blocked
    body = "另一条线索浮出水面。"
    path = _write(tmp_path, 2, body)
    other = _contract(2, "三十四年")

    with pytest.raises(ChapterAcceptanceError) as excinfo:
        service.accept(2, body, other, {"stage": "chapter", "passed": True},
                       manager.current_revision(), path)
    assert any(
        issue.code == "unresolved_project_conflicts"
        for issue in excinfo.value.report.blocking_issues
    )

    apply_decision(manager, 9, build_briefing(9, error.report.blocking_issues), KEEP_EXISTING)
    service.accept(2, body, other, {"stage": "chapter", "passed": True},
                   manager.current_revision(), path)

    assert manager.current_revision() == 2


def test_an_approval_survives_the_next_run(tmp_path, blocked):
    """An answer that has to be repeated on every retry is not an answer."""
    manager, _, _, _, error = blocked

    apply_decision(manager, 9, build_briefing(9, error.report.blocking_issues), ACCEPT_REVERSAL)

    assert manager.approved_contradictions(9) == {"CU-001-01"}
    assert StoryLedgerManager(str(tmp_path)).approved_contradictions(9) == {"CU-001-01"}
    assert StoryLedgerManager(str(tmp_path)).approved_contradictions(8) == set()
