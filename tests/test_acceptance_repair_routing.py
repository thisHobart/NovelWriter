"""Acceptance failures must go back to whatever can actually fix them.

The loop used to stop at the first non-prose failure and hand the author a
dialog.  Most of those failures are machine-fixable; the one that genuinely is
not — a deliberate reversal of established canon — should be the only thing
that reaches a person, and it should arrive already explained.
"""

import logging
import os

import pytest

from agents.review.domain_review_agent import DomainReview
from core.generation.chapter_acceptance import (
    ChapterAcceptanceError,
    ChapterAcceptanceService,
    ValidationIssue,
)
from core.generation.chapter_generation_loop import ChapterGenerationLoop, ChapterLoopResult
from core.generation.story_ledger import StoryLedgerManager

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
    """Answers only the call the contract route makes."""

    def __init__(self, decision, reason=""):
        self.decision = decision
        self.reason = reason
        self.calls = []

    def decide_fact_conflicts(self, chapter_number, conflicts):
        self.calls.append((chapter_number, conflicts))
        return {
            "decisions": [
                {
                    "id": item["id"],
                    "field": item["field"],
                    "decision": self.decision,
                    "reason": self.reason,
                }
                for item in conflicts
            ]
        }


def _write(tmp_path, chapter, body):
    path = tmp_path / "story" / "content" / "chapters" / f"chapter_{chapter}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return str(path)


@pytest.fixture
def project(tmp_path):
    """Chapter 1 accepted with 三十四年 on the ledger."""
    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Mystery", "Subgenre": "Legal Thriller"})
    service = ChapterAcceptanceService(manager)
    body = "金伯利在法医中心干了三十四年。"
    service.accept(
        1, body, _contract(1, "三十四年"), {"stage": "chapter", "passed": True},
        0, _write(tmp_path, 1, body),
    )
    return manager, service


def _loop(tmp_path, manager, service, reviewer):
    loop = ChapterGenerationLoop.__new__(ChapterGenerationLoop)
    loop.output_dir = str(tmp_path)
    loop.ledger = manager
    loop.acceptance_service = service
    loop.reviewer = reviewer
    loop.logger = logging.getLogger("routing-test")
    loop.max_acceptance_retries = 2
    return loop


def _clashing_result(tmp_path, manager):
    body = "金伯利在法医中心干了三十七年。"
    path = _write(tmp_path, 9, body)
    result = ChapterLoopResult(
        scenes=[body],
        contract=_contract(9, "三十七年"),
        base_revision=manager.current_revision(),
        plan_content="### 场景 1：占位\n规划",
        plan_revised=False,
        plan_review=_passing(),
        scene_reviews=[_passing()],
        chapter_review=_passing(),
    )
    return result, path


def _install_full_regeneration(loop, result, body):
    """Give a unit-constructed loop the generation context production `run` stores."""
    calls = []
    loop._generation_context = {
        "parameters": {},
        "lore": "",
        "generate_scene": object(),
        "on_plan_revised": None,
    }

    def fake_run(**kwargs):
        calls.append(kwargs)
        return ChapterLoopResult(
            scenes=[body],
            contract=result.contract,
            base_revision=result.base_revision,
            plan_content=result.plan_content,
            plan_revised=False,
            plan_review=_passing(),
            scene_reviews=[_passing()],
            chapter_review=_passing(),
        )

    loop.run = fake_run
    return calls


def test_a_slip_is_corrected_without_troubling_the_author(tmp_path, project):
    manager, service = project
    reviewer = _Reviewer("keep_existing")
    result, path = _clashing_result(tmp_path, manager)

    loop = _loop(tmp_path, manager, service, reviewer)
    regenerated_body = "金伯利在法医中心干了三十四年。新正文。"
    regeneration_calls = _install_full_regeneration(loop, result, regenerated_body)

    loop.accept_result(9, result, path)

    assert reviewer.calls, "the contract stage was never asked to decide"
    assert result.contract["character_updates"][0]["value"] == "三十四年"
    assert len(regeneration_calls) == 1
    assert result.chapter_content == regenerated_body
    assert open(path, encoding="utf-8").read() == regenerated_body
    archived = list(
        (tmp_path / "quality" / "contract_regenerations" / "chapter_9").glob(
            "invalidated_*.md"
        )
    )
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8") == "金伯利在法医中心干了三十七年。"
    assert manager.current_revision() == 2


def test_a_declared_reversal_reaches_the_author_with_its_reason(tmp_path, project):
    manager, service = project
    reviewer = _Reviewer("contradict", reason="第 9 章揭穿了伪造的工龄记录")
    result, path = _clashing_result(tmp_path, manager)

    with pytest.raises(ChapterAcceptanceError) as excinfo:
        _loop(tmp_path, manager, service, reviewer).accept_result(9, result, path)

    blocking = excinfo.value.report.blocking_issues
    # Exactly one question, and it is the one only a person can answer.
    assert [issue.code for issue in blocking] == ["fact_contradiction"]
    assert blocking[0].repair_target == "human_decision"
    declared = result.contract["facts_contradicted"][0]
    assert declared["id"] == "CU-001-01"
    assert declared["reason"] == "第 9 章揭穿了伪造的工龄记录"
    assert manager.current_revision() == 1, "canon must not move on an open question"


def test_a_reversal_without_a_reason_is_not_accepted(tmp_path, project):
    """An unexplained reversal is indistinguishable from a slip."""
    manager, service = project
    reviewer = _Reviewer("contradict", reason="   ")
    result, path = _clashing_result(tmp_path, manager)

    with pytest.raises(ChapterAcceptanceError) as excinfo:
        _loop(tmp_path, manager, service, reviewer).accept_result(9, result, path)

    assert result.contract["facts_contradicted"] == []
    assert manager.current_revision() == 1
    assert len(reviewer.calls) == 1, "unchanged evidence must not trigger the same question twice"
    assert excinfo.value.adjudication == "unexplained_reversal"


def test_an_ambiguous_answer_is_not_reasked_or_treated_as_keep_existing(tmp_path, project):
    manager, service = project
    reviewer = _Reviewer("uncertain")
    result, path = _clashing_result(tmp_path, manager)

    with pytest.raises(ChapterAcceptanceError) as excinfo:
        _loop(tmp_path, manager, service, reviewer).accept_result(9, result, path)

    assert len(reviewer.calls) == 1
    assert result.contract["character_updates"][0]["value"] == "三十七年"
    assert excinfo.value.adjudication == "no_answer"


def test_a_failed_keep_existing_rewrite_stops_before_reasking(tmp_path, project):
    manager, service = project
    reviewer = _Reviewer("keep_existing")
    result, path = _clashing_result(tmp_path, manager)
    loop = _loop(tmp_path, manager, service, reviewer)
    # Simulate a broken slot mapper claiming it changed the contract while
    # leaving the clashing value untouched.  Acceptance returns the same clash.
    loop._restore_accepted_value = lambda contract, conflict: True
    _install_full_regeneration(loop, result, "金伯利在法医中心干了三十七年。重生成仍冲突。")

    with pytest.raises(ChapterAcceptanceError) as excinfo:
        loop.accept_result(9, result, path)

    assert len(reviewer.calls) == 1
    assert excinfo.value.adjudication == "repair_failed"


def test_a_stale_base_revision_is_refreshed_rather_than_reported(tmp_path, project):
    manager, service = project
    reviewer = _Reviewer("keep_existing")
    body = "灯灭了。"
    path = _write(tmp_path, 9, body)
    result = ChapterLoopResult(
        scenes=[body],
        contract=_contract(9, "三十四年"),
        base_revision=0,  # written against the state before chapter 1 landed
        plan_content="### 场景 1：占位\n规划",
        plan_revised=False,
        plan_review=_passing(),
        scene_reviews=[_passing()],
        chapter_review=_passing(),
    )

    _loop(tmp_path, manager, service, reviewer).accept_result(9, result, path)

    assert result.base_revision == 1
    assert not reviewer.calls, "a stale revision is not a contract question"


def test_routing_prefers_the_most_authoritative_target():
    issues = [
        ValidationIssue("a", "", repair_target="prose"),
        ValidationIssue("b", "", repair_target="contract"),
        ValidationIssue("c", "", repair_target="rebase"),
    ]

    assert ChapterGenerationLoop._repair_route(issues) == "rebase"
    assert ChapterGenerationLoop._repair_route(issues[:2]) == "contract"
    assert ChapterGenerationLoop._repair_route(issues[:1]) == "prose"
    assert (
        ChapterGenerationLoop._repair_route(
            issues + [ValidationIssue("d", "", repair_target="human_decision")]
        )
        == "human_decision"
    )


def test_an_unknown_repair_target_is_never_silently_retried():
    issues = [ValidationIssue("x", "", repair_target="something_new")]

    assert ChapterGenerationLoop._repair_route(issues) == "human_decision"
