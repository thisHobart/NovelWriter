"""Batch writing has nobody to click a button, so it must say enough to act on.

The batch loop already stops at the first blocked chapter — correctly, because
one open conflict blocks every later chapter's commit too.  What it used to
report was ``str(exc)``, which named neither the record nor the two values.
"""

import logging

from core.generation.chapter_acceptance import (
    ChapterAcceptanceError,
    ValidationIssue,
    ValidationReport,
)
from core.generation.conflict_briefing import build_briefing, describe_briefing
from core.generation.story_ledger import StoryLedgerManager


def _report(issues):
    report = ValidationReport(stage="acceptance_canon")
    for issue in issues:
        report.issues.append(issue)
    return report


def _contradiction():
    return ValidationIssue(
        "fact_contradiction",
        "章节正文提出了对既有事实 CU-001-01 的否定，需要人工裁定",
        repair_target="human_decision",
        details={
            "id": "CU-001-01",
            "attribute": "在法医中心的工龄",
            "existing": "三十四年",
            "new_value": "三十七年",
            "reason": "第 9 章揭穿了伪造的工龄记录",
        },
    )


def _raw_conflict():
    return ValidationIssue(
        "fact_fact_conflict",
        "fact F-014-02 的 value 与已接受状态冲突",
        repair_target="contract",
        details={
            "id": "F-014-02",
            "field": "value",
            "existing": "离境前清洗知情者的灭口行动",
            "proposed": "梁浩与郑娜敏发起的联合阻击方案",
            "fact": "双城雷霆作战计划",
        },
    )


class _Agent:
    """Just the piece under test, without the agent's heavy constructor."""

    def __init__(self, output_dir):
        from agents.writing.chapter_writing_agent import ChapterWritingAgent

        self.output_dir = str(output_dir)
        self.logger = logging.getLogger("batch-conflict-test")
        self._impl = ChapterWritingAgent._acceptance_conflict_result

    def run(self, chapter, error):
        return self._impl(self, chapter, error)


def test_the_batch_result_names_the_record_and_both_values(tmp_path):
    StoryLedgerManager(str(tmp_path)).initialize({"Genre": "Mystery"})
    error = ChapterAcceptanceError(_report([_contradiction()]))

    result = _Agent(tmp_path).run(9, error)

    assert not result.success
    message = result.messages[0]
    assert "在法医中心的工龄" in message
    assert "三十四年" in message and "三十七年" in message
    assert "第 9 章揭穿了伪造的工龄记录" in message
    # The author has to know why the run stopped rather than skipped ahead.
    assert "后所有章节都无法提交" in message


def test_the_conflicts_are_also_returned_as_data(tmp_path):
    StoryLedgerManager(str(tmp_path)).initialize({"Genre": "Mystery"})
    error = ChapterAcceptanceError(_report([_contradiction()]))

    result = _Agent(tmp_path).run(9, error)

    assert result.data["needs_human_decision"] is True
    assert result.data["conflicts"] == [
        {
            "id": "CU-001-01",
            "field": "value",
            "existing": "三十四年",
            "proposed": "三十七年",
            "reason": "第 9 章揭穿了伪造的工龄记录",
        }
    ]
    assert result.data["conflict_issues"][0]["code"] == "fact_contradiction"
    assert result.data["conflict_issues"][0]["repair_target"] == "human_decision"


def test_a_failure_with_nothing_to_decide_is_not_dressed_up_as_a_choice(tmp_path):
    StoryLedgerManager(str(tmp_path)).initialize({"Genre": "Mystery"})
    error = ChapterAcceptanceError(
        _report(
            [
                ValidationIssue(
                    "revision_conflict", "base_revision 已过期", repair_target="rebase"
                )
            ]
        )
    )

    result = _Agent(tmp_path).run(9, error)

    assert result.data["needs_human_decision"] is False
    assert result.data["conflicts"] == []
    assert result.messages[0] == "base_revision 已过期"


def test_a_failed_contract_rewrite_is_reported_as_a_repair_bug(tmp_path):
    StoryLedgerManager(str(tmp_path)).initialize({"Genre": "Mystery"})
    error = ChapterAcceptanceError(_report([_raw_conflict()]), "repair_failed")

    result = _Agent(tmp_path).run(15, error)

    assert result.data["needs_human_decision"] is False
    assert result.data["adjudication"] == "repair_failed"
    assert "模型判定应沿用账本" in result.messages[0]
    assert "契约修复失败" in result.messages[0]
    assert "继续重写正文不会解决" in result.messages[0]


def test_an_unexplained_reversal_says_why_it_stopped(tmp_path):
    StoryLedgerManager(str(tmp_path)).initialize({"Genre": "Mystery"})
    error = ChapterAcceptanceError(
        _report([_raw_conflict()]), "unexplained_reversal"
    )

    result = _Agent(tmp_path).run(15, error)

    assert result.data["needs_human_decision"] is True
    assert "判定本章在推翻既有设定" in result.messages[0]
    assert "没有给出故事内理由" in result.messages[0]
    assert "没有用相同提示继续重复询问" in result.messages[0]


def test_an_explained_reversal_says_the_author_still_decides(tmp_path):
    StoryLedgerManager(str(tmp_path)).initialize({"Genre": "Mystery"})
    error = ChapterAcceptanceError(_report([_contradiction()]), "awaiting_author")

    result = _Agent(tmp_path).run(9, error)

    assert result.data["needs_human_decision"] is True
    assert "有意的剧情反转并给出了理由" in result.messages[0]
    assert "模型无权单方面覆盖" in result.messages[0]
    assert "仍须由你裁定" in result.messages[0]


def test_the_headless_description_needs_no_gui_toolkit():
    """The batch writer must never pull tkinter in just to describe a clash."""
    import sys

    import core.generation.conflict_briefing as module

    assert "tkinter" not in sys.modules or "tkinter" not in dir(module)
    briefing = build_briefing(9, [_contradiction()])
    assert "三十七年" in describe_briefing(briefing)


def test_a_blocked_predecessor_is_explained_not_just_announced(tmp_path):
    """"第 15 章尚未验收" is a symptom; the author needs the cause and the fix."""
    import logging

    from core.generation.chapter_generation_loop import ChapterGenerationLoop

    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Mystery"})
    manager.record_conflicts(
        15,
        14,
        _report([
            ValidationIssue(
                "fact_fact_conflict",
                "fact F-014-01 的 value 与已接受状态冲突",
                repair_target="contract",
                details={
                    "id": "F-014-01",
                    "field": "value",
                    "existing": "绝密温控舱",
                    "proposed": "特制加固恒温舱",
                    "label": "NB-4 母液运输容器",
                },
            )
        ]).to_dict(),
    )

    loop = ChapterGenerationLoop.__new__(ChapterGenerationLoop)
    loop.output_dir = str(tmp_path)
    loop.ledger = manager
    loop.logger = logging.getLogger("predecessor-test")

    message = loop._describe_unaccepted_chapter(15, 16)

    assert "NB-4 母液运输容器（F-014-01）" in message
    assert "绝密温控舱" in message and "特制加固恒温舱" in message
    assert "请重新生成第 15 章" in message


def test_a_predecessor_with_no_prose_says_so(tmp_path):
    import logging

    from core.generation.chapter_generation_loop import ChapterGenerationLoop

    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Mystery"})
    loop = ChapterGenerationLoop.__new__(ChapterGenerationLoop)
    loop.output_dir = str(tmp_path)
    loop.ledger = manager
    loop.logger = logging.getLogger("predecessor-test")

    assert "还没有正文" in loop._describe_unaccepted_chapter(15, 16)
