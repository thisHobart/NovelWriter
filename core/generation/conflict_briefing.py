"""Describe a blocked acceptance in terms a person can act on.

``ChapterAcceptanceError`` carries which record clashed, what the ledger holds,
what the chapter claims and why the chapter thinks the change is deliberate.
Flattening that into ``str(exc)`` is what left the author holding an
unactionable "需要人工裁定".

This module deliberately imports no GUI toolkit: the batch writer runs
headless and needs exactly the same description the dialog shows.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List

from core.generation.chapter_acceptance import ValidationIssue
from core.generation.story_ledger import StoryLedgerManager

KEEP_EXISTING = "keep_existing"
ACCEPT_REVERSAL = "accept_reversal"

ADJUDICATION_AWAITING_AUTHOR = "awaiting_author"
ADJUDICATION_UNEXPLAINED = "unexplained_reversal"
ADJUDICATION_NO_ANSWER = "no_answer"
ADJUDICATION_REPEATED = "repeated_verdict"
ADJUDICATION_REPAIR_FAILED = "repair_failed"

RESOLUTION_LABELS = {
    ACCEPT_REVERSAL: "author_accepted_reversal",
    KEEP_EXISTING: "author_kept_existing_canon",
}


@dataclass(frozen=True)
class ConflictChoice:
    """One clash, described the way a person needs to see it."""

    record_id: str
    field_name: str
    existing: str
    proposed: str
    reason: str = ""
    label: str = ""

    @property
    def is_declared_reversal(self) -> bool:
        return bool(self.reason)

    @property
    def display_name(self) -> str:
        """The record named the way a reader recognises it, without repetition."""
        if self.label and self.label != self.record_id:
            return f"{self.label}（{self.record_id}）"
        return self.record_id


@dataclass
class ConflictBriefing:
    """Everything a decision needs, derived from the report alone."""

    chapter: int
    choices: List[ConflictChoice] = field(default_factory=list)
    other_messages: List[str] = field(default_factory=list)

    @property
    def is_decidable(self) -> bool:
        """True when the author has a real choice, not just an error to read."""
        return bool(self.choices)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def build_briefing(chapter: int, issues: Iterable[ValidationIssue]) -> ConflictBriefing:
    """Split a report into "things you can decide" and "things you should know"."""
    briefing = ConflictBriefing(chapter=chapter)
    for issue in issues:
        details = issue.details or {}
        if issue.code == "fact_contradiction":
            briefing.choices.append(
                ConflictChoice(
                    record_id=_text(details.get("id")),
                    field_name=_text(details.get("field")) or "value",
                    existing=_text(details.get("existing")),
                    proposed=_text(details.get("new_value") or details.get("proposed")),
                    reason=_text(details.get("reason")),
                    label=_text(details.get("label") or details.get("fact") or details.get("attribute")),
                )
            )
        elif issue.code.endswith("_fact_conflict"):
            briefing.choices.append(
                ConflictChoice(
                    record_id=_text(details.get("id")),
                    field_name=_text(details.get("field")) or "value",
                    existing=_text(details.get("existing")),
                    proposed=_text(details.get("proposed")),
                    label=_text(details.get("label") or details.get("fact") or details.get("attribute")),
                )
            )
        else:
            briefing.other_messages.append(issue.message)
    return briefing


def needs_author_decision(briefing: ConflictBriefing, adjudication: str = "") -> bool:
    """Whether the stop is a story choice rather than an automatic repair bug."""
    if not briefing.is_decidable:
        return False
    return adjudication != ADJUDICATION_REPAIR_FAILED


def adjudication_intro(chapter: int, adjudication: str = "") -> str:
    """Name why automatic conflict handling stopped, without hiding its state."""
    if adjudication == ADJUDICATION_REPAIR_FAILED:
        return (
            f"第 {chapter} 章：模型判定应沿用账本，但自动改写契约后冲突仍然存在。"
            "这是契约修复失败，不是新的剧情裁定。"
        )
    if adjudication == ADJUDICATION_UNEXPLAINED:
        return (
            f"第 {chapter} 章：模型判定本章在推翻既有设定，但没有给出故事内理由。"
            "程序没有采纳这次反转，也没有用相同提示继续重复询问。"
        )
    if adjudication == ADJUDICATION_AWAITING_AUTHOR:
        return (
            f"第 {chapter} 章：模型判定这是有意的剧情反转并给出了理由，"
            "但模型无权单方面覆盖已验收设定，正在等待你确认。"
        )
    if adjudication == ADJUDICATION_NO_ANSWER:
        return (
            f"第 {chapter} 章：模型没有给出完整、明确的冲突判定。"
            "程序没有猜测其意图，也没有用相同提示继续重复询问。"
        )
    if adjudication == ADJUDICATION_REPEATED:
        return (
            f"第 {chapter} 章：冲突在上一轮处理后没有任何变化，"
            "程序已停止重复询问模型。"
        )
    return f"第 {chapter} 章与已接受设定冲突，需要人工裁定："


def describe_briefing(
    briefing: ConflictBriefing,
    conflict_path: str = "",
    adjudication: str = "",
) -> str:
    """One paragraph naming the clash and the decision that unblocks it.

    Batch writing has nobody to click a button, so the message has to carry
    enough for the author to rule on it later without digging through logs.
    """
    if not briefing.is_decidable:
        return "；".join(briefing.other_messages) or "章节未通过验收"

    lines = [adjudication_intro(briefing.chapter, adjudication)]
    for choice in briefing.choices:
        lines.append(
            f"  · {choice.display_name}：账本为「{choice.existing}」，"
            f"本章声明「{choice.proposed}」"
        )
        if choice.reason:
            lines.append(f"    本章给出的理由：{choice.reason}")
    lines.extend(f"  · {message}" for message in briefing.other_messages)
    if adjudication == ADJUDICATION_REPAIR_FAILED:
        lines.append(
            "请查看冲突记录并修复或重新生成本章契约；继续重写正文不会解决这个问题。"
        )
    elif adjudication == ADJUDICATION_UNEXPLAINED:
        lines.append(
            "请先检查本章是否确有足以推翻设定的剧情依据，再决定采纳本章或维持账本。"
        )
    elif adjudication == ADJUDICATION_AWAITING_AUTHOR:
        lines.append(
            "模型的理由只用于说明判断；仍须由你裁定「采纳本章」或「维持账本」。"
        )
    elif adjudication == ADJUDICATION_NO_ANSWER:
        lines.append("请人工检查冲突后裁定；程序不会把含糊回答当作默认许可。")
    else:
        lines.append(
            "在界面上裁定「采纳本章」或「维持账本」之前，本章及其后所有章节都无法提交。"
        )
    lines.append("在处理完成之前，本章及其后所有章节都无法提交。")
    if conflict_path:
        lines.append(f"冲突记录：{conflict_path}")
    return "\n".join(lines)


def _accepted_value(
    ledger: StoryLedgerManager, record_id: str, field_name: str
) -> Any:
    """Find the committed value for an older reversal record missing `existing`."""
    sources = [ledger.load_suspense_ledger(), ledger.load_case_bible()]
    for source in sources:
        if not isinstance(source, dict):
            continue
        for records in source.values():
            if not isinstance(records, list):
                continue
            for record in records:
                if (
                    isinstance(record, dict)
                    and str(record.get("id", "")) == str(record_id)
                    and record.get(field_name) not in (None, "")
                ):
                    return record.get(field_name)
    return None


def apply_decision(
    ledger: StoryLedgerManager,
    chapter: int,
    briefing: ConflictBriefing,
    decision: str,
    note: str = "",
) -> str:
    """Record the author's ruling so the next run does not ask again.

    An answer that does not survive the retry is not an answer, so both rulings
    are written to the ledger and every conflict this chapter has open is
    closed on the author's authority.
    """
    summary = RESOLUTION_LABELS.get(decision, RESOLUTION_LABELS[KEEP_EXISTING])
    if decision == ACCEPT_REVERSAL:
        for choice in briefing.choices:
            if choice.record_id:
                ledger.approve_contradiction(chapter, choice.record_id, note)
    else:
        # "维持账本" must change the saved contract, not merely close the
        # notification.  Otherwise the next run loads the same proposed value
        # and recreates the same conflict after another full prose generation.
        contract = ledger.load_contract(chapter)
        if contract is not None:
            changed = False
            choice_by_id = {
                choice.record_id: choice for choice in briefing.choices if choice.record_id
            }
            for field_name, records in list(contract.items()):
                if not isinstance(records, list):
                    continue
                if field_name == "facts_contradicted":
                    kept = [
                        record
                        for record in records
                        if not (
                            isinstance(record, dict)
                            and str(record.get("id", "")) in choice_by_id
                        )
                    ]
                    if len(kept) != len(records):
                        contract[field_name] = kept
                        changed = True
                    continue
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    choice = choice_by_id.get(str(record.get("id", "")))
                    if choice is None or not choice.field_name:
                        continue
                    existing = choice.existing
                    if existing in (None, ""):
                        existing = _accepted_value(
                            ledger, choice.record_id, choice.field_name
                        )
                    if existing in (None, ""):
                        continue
                    if record.get(choice.field_name) != existing:
                        record[choice.field_name] = existing
                        changed = True
            if changed:
                ledger.replace_saved_contract(chapter, contract)

    # Both rulings alter the authoritative generation context.  Persist the
    # invalidation before closing the conflict so no caller can resubmit the old
    # ChapterLoopResult after the author has decided.
    ledger.require_chapter_regeneration(
        chapter,
        summary,
        (choice.record_id for choice in briefing.choices),
    )

    for conflict in ledger.open_conflicts():
        if conflict.get("chapter") == chapter:
            ledger.resolve_conflict(_text(conflict.get("id")), summary, note)
    return summary


def conflict_record_path(ledger: StoryLedgerManager, chapter: int) -> str:
    """Where the full stored report for this chapter's open conflict lives."""
    entry: Dict[str, Any] = next(
        (item for item in ledger.open_conflicts() if item.get("chapter") == chapter), {}
    )
    relative = _text(entry.get("path"))
    return os.path.join(ledger.output_dir, relative) if relative else ""
