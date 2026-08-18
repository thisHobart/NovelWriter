"""Chapter-delta extraction and pre-commit acceptance gates."""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from core.generation.domain_profiles import GENERAL, DomainProfile, get_domain_profile
from core.generation.story_ledger import (
    CHARACTER_ATTRIBUTE_KEY,
    RevisionConflictError,
    StoryLedgerManager,
    source_hash,
)


DELTA_VERSION = 1
GENERATION_ERROR_MARKERS = ("[[[ERROR GENERATING", "[[[生成失败", "[[[ERROR")
TIME_PATTERN = re.compile(
    r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?"
    r"|(?:凌晨|早上|上午|中午|下午|傍晚|晚上)?"
    r"[零〇一二三四五六七八九十百两\d]{1,4}点"
    r"(?:[零〇一二三四五六七八九十百两\d]{1,4}分)?"
)
_CN_DIGITS = "零〇一二三四五六七八九十百千万两"
# 中文小说里的数量绝大多数写作「三十四年」而不是「34年」，只匹配阿拉伯数字会
# 让 numeric_expressions 对中文正文近乎空转。
NUMBER_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_])(?:\d+(?:\.\d+)?|[{_CN_DIGITS}]{{1,6}})"
    r"(?:秒|分钟|分|小时|天|周|月|年|岁|人|件|起|次|米|毫米|公里|元|万|份|页|条|章|%)"
    r"|(?<![A-Za-z0-9_])\d+(?:\.\d+)?"
)
VOLATILE_CONTRACT_FIELDS = {"created_at", "updated_at", "generated_at", "accepted_at"}

_CN_NUMERALS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def parse_cn_number(text: str) -> Optional[int]:
    """Parse an Arabic or Chinese numeral into an int, or None if it is not one."""
    if text is None:
        return None
    text = str(text).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)

    total = 0
    section = 0
    current = 0
    seen = False
    for char in text:
        if char in _CN_NUMERALS:
            current = _CN_NUMERALS[char]
            seen = True
        elif char in _CN_UNITS:
            unit = _CN_UNITS[char]
            seen = True
            if unit == 10000:
                total = (total + section + current) * unit
                section = 0
            else:
                # 「十四」= 10 + 4：单位前没有数字时隐含 1。
                section += (current or 1) * unit
            current = 0
        else:
            return None
    if not seen:
        return None
    return total + section + current


_MEASURE_PATTERN = re.compile(
    rf"^([{_CN_DIGITS}\d]+(?:\.\d+)?)\s*"
    r"(秒|分钟|分|小时|天|周|个?月|年|岁|人|件|起|次|米|毫米|公里|元|万|份|页|条|章|%)?$"
)


def parse_measure(text: str) -> Optional[tuple[float, str]]:
    """Parse '三十四年' / '34年' / '34' into a comparable (number, unit) pair.

    Chinese and Arabic numerals must compare equal, otherwise a chapter that
    spells a figure differently would read as a contradiction.
    """
    if text is None:
        return None
    match = _MEASURE_PATTERN.match(str(text).strip())
    if not match:
        return None
    number = match.group(1)
    if re.fullmatch(r"\d+(?:\.\d+)?", number):
        value = float(number)
    else:
        parsed = parse_cn_number(number)
        if parsed is None:
            return None
        value = float(parsed)
    unit = match.group(2) or ""
    return value, unit.lstrip("个")


def values_conflict(old_value: Any, new_value: Any) -> bool:
    """Whether two declared values genuinely disagree.

    Falls back to whitespace-insensitive string comparison for anything that is
    not a measurement.
    """
    old_measure = parse_measure(old_value)
    new_measure = parse_measure(new_value)
    if old_measure and new_measure:
        old_number, old_unit = old_measure
        new_number, new_unit = new_measure
        if old_unit and new_unit and old_unit != new_unit:
            # 单位不同就不是同一个量，交给人判断而不是在这里下结论。
            return False
        return old_number != new_number
    normalize = lambda value: re.sub(r"\s+", "", str(value))
    return normalize(old_value) != normalize(new_value)


def parse_clock(text: str) -> Optional[tuple[int, int]]:
    """Normalize a clock expression to (hour % 12, minute).

    Comparing the hour modulo 12 deliberately treats 下午三点 and 三点 as the
    same reading: this feeds warning-level checks, where a false alarm on the
    12-hour ambiguity is worse than a missed one.
    """
    if not text:
        return None
    text = str(text).strip()

    digital = re.match(r"^(?:(\d{1,2}):(\d{2}))(?::\d{2})?$", text)
    if digital:
        return int(digital.group(1)) % 12, int(digital.group(2))

    chinese = re.match(
        rf"^(?:凌晨|早上|上午|中午|下午|傍晚|晚上|午夜)?"
        rf"([{_CN_DIGITS}\d]{{1,4}})点(?:([{_CN_DIGITS}\d]{{1,4}})分)?$",
        text,
    )
    if not chinese:
        return None
    hour = parse_cn_number(chinese.group(1))
    if hour is None:
        return None
    minute = 0
    if chinese.group(2):
        minute = parse_cn_number(chinese.group(2))
        if minute is None:
            return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour % 12, minute


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    severity: str = "blocking"
    repair_target: str = "delta"
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "repair_target": self.repair_target,
            "details": deepcopy(self.details),
        }


@dataclass
class ValidationReport:
    stage: str
    issues: List[ValidationIssue] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(issue.severity == "blocking" for issue in self.issues)

    @property
    def blocking_issues(self) -> List[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "blocking"]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "passed": self.passed,
            "issues": [issue.to_dict() for issue in self.issues],
            "created_at": datetime.now().isoformat(),
        }


@dataclass
class ChapterDelta:
    chapter: int
    base_revision: int
    content_hash: str
    contract_hash: str
    facts_added: List[Dict[str, Any]] = field(default_factory=list)
    facts_confirmed: List[Dict[str, Any]] = field(default_factory=list)
    facts_contradicted: List[Dict[str, Any]] = field(default_factory=list)
    character_updates: List[Dict[str, Any]] = field(default_factory=list)
    timeline_events: List[Dict[str, Any]] = field(default_factory=list)
    clue_updates: List[Dict[str, Any]] = field(default_factory=list)
    evidence_updates: List[Dict[str, Any]] = field(default_factory=list)
    plot_thread_updates: List[Dict[str, Any]] = field(default_factory=list)
    knowledge_updates: List[Dict[str, Any]] = field(default_factory=list)
    personal_cost_updates: List[Dict[str, Any]] = field(default_factory=list)
    text_signals: Dict[str, List[str]] = field(default_factory=dict)
    extracted_at: str = field(default_factory=lambda: datetime.now().isoformat())
    extraction_mode: str = "contract_and_text_signals"
    semantic_extraction_status: str = "pending"
    version: int = DELTA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return deepcopy(self.__dict__)


@dataclass
class ChapterAcceptanceResult:
    delta: ChapterDelta
    artifact_report: ValidationReport
    consistency_report: ValidationReport
    committed_revision: int
    delta_path: str
    artifact_report_path: str
    consistency_report_path: str


class ChapterAcceptanceError(RuntimeError):
    """Raised when the final chapter artifact cannot be committed."""

    def __init__(self, report: ValidationReport):
        self.report = report
        messages = "; ".join(issue.message for issue in report.blocking_issues)
        super().__init__(messages or f"Chapter acceptance failed during {report.stage}")


def _records(value: Any, source: str = "contract") -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    records = []
    for item in value:
        if isinstance(item, dict):
            record = deepcopy(item)
            record.setdefault("source", source)
        elif item not in (None, ""):
            record = {"value": str(item), "source": source}
        else:
            continue
        records.append(record)
    return records


def _unique_matches(pattern: re.Pattern[str], content: str) -> List[str]:
    values = []
    for match in pattern.finditer(content):
        value = match.group(0)
        if value not in values:
            values.append(value)
    return values


def stable_contract_hash(contract: Dict[str, Any]) -> str:
    stable_contract = {
        key: value
        for key, value in contract.items()
        if key not in VOLATILE_CONTRACT_FIELDS
    }
    return source_hash(
        json.dumps(
            stable_contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _slot_records(
    contract: Dict[str, Any],
    profile: DomainProfile,
    slot: str,
) -> List[Dict[str, Any]]:
    """Collect every contract field the profile routes into one delta slot."""
    records: List[Dict[str, Any]] = []
    for spec in profile.fields_for_slot(slot):
        records.extend(_records(contract.get(spec.name)))
    return records


class DefaultChapterDeltaExtractor:
    """Build a deterministic delta envelope from final prose and contract declarations.

    The extractor deliberately does not claim to understand every prose fact. It
    records contract-declared state transitions and stable textual signals. A
    semantic/LLM extractor can replace this class without changing the commit path.

    Which contract fields feed the two tracked record streams (clue_updates /
    evidence_updates) is decided by the domain profile, not hardcoded here.
    """

    def extract(
        self,
        chapter_number: int,
        content: str,
        contract: Dict[str, Any],
        base_revision: int,
        profile: Optional[DomainProfile] = None,
    ) -> ChapterDelta:
        profile = profile or GENERAL
        knowledge_updates = [
            {"audience": "reader", "fact": str(fact), "source": "contract"}
            for fact in contract.get("reader_knows_after", [])
            if fact not in (None, "")
        ]
        character_knowledge = contract.get("character_knowledge_after", {})
        if isinstance(character_knowledge, dict):
            for character, facts in character_knowledge.items():
                if not isinstance(facts, list):
                    continue
                knowledge_updates.extend(
                    {
                        "audience": "character",
                        "character": str(character),
                        "fact": str(fact),
                        "source": "contract",
                    }
                    for fact in facts
                    if fact not in (None, "")
                )

        plot_updates = _records(contract.get("plot_thread_updates"))
        irreversible_change = contract.get("irreversible_change")
        if irreversible_change:
            plot_updates.append(
                {
                    "id": f"chapter.{chapter_number}.irreversible_change",
                    "type": "irreversible_change",
                    "value": str(irreversible_change),
                    "source": "contract",
                }
            )

        personal_cost_updates = []
        personal_cost = contract.get("personal_cost")
        if personal_cost:
            personal_cost_updates.append(
                {
                    "character": str(contract.get("cost_character") or "未指定人物"),
                    "cost": str(personal_cost),
                    "source": "contract",
                }
            )

        return ChapterDelta(
            chapter=chapter_number,
            base_revision=base_revision,
            content_hash=source_hash(content),
            contract_hash=stable_contract_hash(contract),
            facts_added=_records(contract.get("facts_added")),
            facts_confirmed=_records(contract.get("facts_confirmed")),
            facts_contradicted=_records(contract.get("facts_contradicted")),
            character_updates=_records(contract.get("character_updates")),
            timeline_events=_records(contract.get("timeline_events")),
            clue_updates=_slot_records(contract, profile, "clue_updates"),
            evidence_updates=_slot_records(contract, profile, "evidence_updates"),
            plot_thread_updates=plot_updates,
            knowledge_updates=knowledge_updates,
            personal_cost_updates=personal_cost_updates,
            text_signals={
                "time_expressions": _unique_matches(TIME_PATTERN, content),
                "numeric_expressions": _unique_matches(NUMBER_PATTERN, content),
            },
        )


class ArtifactValidator:
    """Cheap deterministic checks that must pass before Canon comparison."""

    @staticmethod
    def _check_declared_times_against_prose(
        issues: List[ValidationIssue],
        delta: ChapterDelta,
    ) -> None:
        """Warn when a declared event time never shows up in the prose.

        `text_signals` was extracted and stored but never read by anything, so a
        chapter could declare an event at 00:12 while the prose said 三点零八分
        and nothing noticed. Only fires when the prose does state some clock
        time: a chapter with no clock reference at all simply keeps the event
        off-page, which is not an error.
        """
        prose_times = {
            parsed
            for expression in delta.text_signals.get("time_expressions", [])
            if (parsed := parse_clock(expression)) is not None
        }
        if not prose_times:
            return

        for event in delta.timeline_events:
            declared = parse_clock(event.get("time"))
            if declared is None or declared in prose_times:
                continue
            issues.append(
                ValidationIssue(
                    "declared_time_absent_from_prose",
                    f"契约声明事件 {event.get('id')}（{event.get('event', '未命名')}）"
                    f"发生在 {event.get('time')}，但正文里没有出现这个时刻",
                    severity="warning",
                    repair_target="prose",
                    details={
                        "id": event.get("id"),
                        "declared_time": event.get("time"),
                        "prose_times": sorted(
                            delta.text_signals.get("time_expressions", [])
                        )[:20],
                    },
                )
            )

    @staticmethod
    def _check_declared_numbers_against_prose(
        issues: List[ValidationIssue],
        delta: ChapterDelta,
    ) -> None:
        """Warn when a declared stable measurement never appears in the prose.

        Pairs with the ledger-side check: the gate keeps one value per
        (character, attribute) across chapters, and this keeps the prose honest
        about the value the contract declared for this chapter.
        """
        prose_measures = {
            measure
            for expression in delta.text_signals.get("numeric_expressions", [])
            if (measure := parse_measure(expression)) is not None
        }
        if not prose_measures:
            return
        prose_numbers = {number for number, _ in prose_measures}

        for record in delta.character_updates:
            if not record.get("stable"):
                continue
            declared = parse_measure(record.get("value"))
            if declared is None:
                continue
            number, unit = declared
            # 单位可能被正文省略（「三十四年」写成「三十四个年头」），只比数值。
            if number in prose_numbers:
                continue
            issues.append(
                ValidationIssue(
                    "declared_number_absent_from_prose",
                    f"契约声明 {record.get('character')} 的"
                    f"{record.get('attribute')} 为 {record.get('value')}，"
                    f"但正文里没有出现这个数值",
                    severity="warning",
                    repair_target="prose",
                    details={
                        "character": record.get("character"),
                        "attribute": record.get("attribute"),
                        "declared_value": record.get("value"),
                        "declared_number": number,
                        "declared_unit": unit,
                    },
                )
            )

    def validate(
        self,
        delta: ChapterDelta,
        final_content: str,
        expected_content_hash: str,
        contract: Dict[str, Any],
    ) -> ValidationReport:
        issues: List[ValidationIssue] = []
        if not final_content.strip():
            issues.append(ValidationIssue("empty_chapter", "最终章节正文为空", repair_target="prose"))
        if any(marker in final_content for marker in GENERATION_ERROR_MARKERS):
            issues.append(
                ValidationIssue(
                    "generation_error_marker",
                    "最终章节正文仍包含生成失败占位符",
                    repair_target="prose",
                )
            )
        if delta.content_hash != expected_content_hash:
            issues.append(
                ValidationIssue(
                    "saved_content_mismatch",
                    "落盘正文与通过审阅的正文不一致",
                    repair_target="prose",
                    details={
                        "saved_content_hash": delta.content_hash,
                        "reviewed_content_hash": expected_content_hash,
                    },
                )
            )
        if delta.chapter <= 0:
            issues.append(ValidationIssue("invalid_chapter", "章节编号必须大于零"))
        contract_chapter = contract.get("chapter")
        if contract_chapter not in (None, delta.chapter):
            issues.append(
                ValidationIssue(
                    "contract_chapter_mismatch",
                    "章节契约编号与待提交章节不一致",
                    details={"contract_chapter": contract_chapter, "chapter": delta.chapter},
                )
            )
        if delta.base_revision < 0:
            issues.append(ValidationIssue("invalid_base_revision", "base_revision 不能小于零"))

        self._check_declared_times_against_prose(issues, delta)
        self._check_declared_numbers_against_prose(issues, delta)

        for field_name, records in (
            ("facts_added", delta.facts_added),
            ("facts_confirmed", delta.facts_confirmed),
            ("facts_contradicted", delta.facts_contradicted),
            ("timeline_events", delta.timeline_events),
            ("character_updates", delta.character_updates),
            ("clue_updates", delta.clue_updates),
            ("evidence_updates", delta.evidence_updates),
            ("plot_thread_updates", delta.plot_thread_updates),
        ):
            seen = set()
            for index, record in enumerate(records):
                record_id = record.get("id")
                if not record_id:
                    issues.append(
                        ValidationIssue(
                            "missing_stable_id",
                            f"{field_name} 第 {index + 1} 项缺少稳定 id",
                            details={"field": field_name, "index": index},
                        )
                    )
                    continue
                if record_id in seen:
                    issues.append(
                        ValidationIssue(
                            "duplicate_stable_id",
                            f"{field_name} 中重复出现 id: {record_id}",
                            details={"field": field_name, "id": record_id},
                        )
                    )
                seen.add(record_id)

        return ValidationReport(stage="artifact_validation", issues=issues)


class CanonConsistencyGate:
    """Compare a validated delta with the currently committed story state."""

    def validate(
        self,
        delta: ChapterDelta,
        case_bible: Dict[str, Any],
        suspense_ledger: Dict[str, Any],
        profile: Optional[DomainProfile] = None,
    ) -> ValidationReport:
        profile = profile or GENERAL
        issues: List[ValidationIssue] = []
        current_revision = int(suspense_ledger.get("revision", 0) or 0)
        accepted_entry = next(
            (
                item
                for item in suspense_ledger.get("accepted_chapters", [])
                if item.get("chapter") == delta.chapter
            ),
            None,
        )
        is_idempotent = bool(
            accepted_entry
            and accepted_entry.get("content_hash")
            and accepted_entry.get("content_hash") == delta.content_hash
            and accepted_entry.get("contract_hash") == delta.contract_hash
        )
        unrelated_conflicts = [
            conflict
            for conflict in suspense_ledger.get("unresolved_conflicts", [])
            if conflict.get("chapter") != delta.chapter
        ]
        if unrelated_conflicts:
            issues.append(
                ValidationIssue(
                    "unresolved_project_conflicts",
                    "项目仍有其他章节的未决冲突，必须先裁定或修复",
                    repair_target="human_decision",
                    details={
                        "conflict_ids": [
                            conflict.get("id") for conflict in unrelated_conflicts if conflict.get("id")
                        ]
                    },
                )
            )
        if current_revision != delta.base_revision and not is_idempotent:
            issues.append(
                ValidationIssue(
                    "revision_conflict",
                    "生成章节所依据的 base_revision 已过期，需要重新构造上下文",
                    repair_target="rebase",
                    details={
                        "base_revision": delta.base_revision,
                        "current_revision": current_revision,
                    },
                )
            )

        case_status = case_bible.get("status", "uninitialized")
        if case_status != "ready":
            issues.append(
                ValidationIssue(
                    "case_bible_not_ready",
                    f"{profile.bible_noun}状态为 {case_status}；本次允许提交，但应补做底稿审计",
                    severity="warning",
                    repair_target="case_bible",
                )
            )

        self._check_stable_record_conflicts(
            issues,
            delta.clue_updates,
            suspense_ledger.get("clues", []),
            record_type="clue",
            immutable_fields=profile.immutable_fields_for_slot("clue_updates"),
            identity_fields=profile.identity_fields_for_slot("clue_updates"),
        )
        self._check_stable_record_conflicts(
            issues,
            delta.evidence_updates,
            suspense_ledger.get("evidence", []),
            record_type="evidence",
            immutable_fields=profile.immutable_fields_for_slot("evidence_updates"),
            identity_fields=profile.identity_fields_for_slot("evidence_updates"),
        )
        existing_facts = list(case_bible.get("truth", []))
        existing_facts.extend(suspense_ledger.get("facts", []))
        self._check_stable_record_conflicts(
            issues,
            [*delta.facts_added, *delta.facts_confirmed],
            existing_facts,
            record_type="fact",
            immutable_fields=("fact", "value"),
            identity_fields=("fact",),
        )
        existing_timeline = list(case_bible.get("chronology", []))
        existing_timeline.extend(suspense_ledger.get("timeline_events", []))
        self._check_stable_record_conflicts(
            issues,
            delta.timeline_events,
            existing_timeline,
            record_type="timeline",
            immutable_fields=("event", "time", "time_start", "time_end", "location_id"),
            identity_fields=("event",),
        )

        for contradicted in delta.facts_contradicted:
            issues.append(
                ValidationIssue(
                    "fact_contradiction",
                    f"章节正文提出了对既有事实 {contradicted.get('id')} 的否定，需要人工裁定",
                    repair_target="human_decision",
                    details=deepcopy(contradicted),
                )
            )

        # 只有声明为 stable 的属性才锁定取值：伤势、位置这类会随剧情变化的状态
        # 若一并锁死，正常推进也会被判成矛盾。
        self._check_stable_record_conflicts(
            issues,
            [record for record in delta.character_updates if record.get("stable")],
            [
                record
                for record in suspense_ledger.get("character_updates", [])
                if isinstance(record, dict) and record.get("stable")
            ],
            record_type="character_attribute",
            immutable_fields=("value",),
            identity_fields=CHARACTER_ATTRIBUTE_KEY,
        )

        self._check_overdue_plot_threads(
            issues,
            delta.chapter,
            delta.plot_thread_updates,
            suspense_ledger.get("plot_threads", []),
        )

        if accepted_entry and accepted_entry.get("content_hash") not in (None, delta.content_hash):
            issues.append(
                ValidationIssue(
                    "chapter_rewrite",
                    "该章节已有已接受版本；本次提交将形成新的 revision",
                    severity="warning",
                    repair_target="dependency_audit",
                    details={
                        "previous_revision": accepted_entry.get("committed_revision"),
                        "previous_content_hash": accepted_entry.get("content_hash"),
                    },
                )
            )

        return ValidationReport(stage="canon_consistency", issues=issues)

    @staticmethod
    def _check_overdue_plot_threads(
        issues: List[ValidationIssue],
        chapter: int,
        proposed_threads: Iterable[Dict[str, Any]],
        existing_threads: Iterable[Dict[str, Any]],
    ) -> None:
        """Flag threads that were opened with a deadline and never closed.

        A thread only participates if it declared a positive `deadline_chapter`;
        without a declared deadline there is nothing to be late for. Missing the
        deadline exactly on this chapter still blocks, because revising this
        chapter can resolve it — an already-passed deadline only warns, so an
        early oversight cannot deadlock every remaining chapter.
        """
        state: Dict[str, Dict[str, Any]] = {}
        for record in existing_threads:
            if isinstance(record, dict) and record.get("id"):
                state[str(record["id"])] = dict(record)
        for record in proposed_threads:
            if not isinstance(record, dict) or not record.get("id"):
                continue
            state.setdefault(str(record["id"]), {}).update(record)

        for thread_id, record in state.items():
            if str(record.get("status", "")).lower() != "open":
                continue
            try:
                deadline = int(record.get("deadline_chapter") or 0)
            except (TypeError, ValueError):
                continue
            if deadline <= 0 or chapter < deadline:
                continue
            issues.append(
                ValidationIssue(
                    "plot_thread_overdue",
                    f"线索 {thread_id}（{record.get('thread', '未命名')}）应在第 {deadline} 章前"
                    f"闭合，到第 {chapter} 章仍为 open",
                    severity="blocking" if chapter == deadline else "warning",
                    repair_target="contract",
                    details={
                        "id": thread_id,
                        "thread": record.get("thread"),
                        "deadline_chapter": deadline,
                        "chapter": chapter,
                    },
                )
            )

    @staticmethod
    def _identity_key(record: Dict[str, Any], identity_fields: Iterable[str]) -> Optional[str]:
        """Build a content-based key so one thing stays one record across chapters.

        Matching on `id` alone is not enough: nothing forces the model to reuse a
        previously minted id, and a fresh id would otherwise let a contradicting
        record slip past the gate unnoticed.
        """
        parts = []
        for field_name in identity_fields:
            value = record.get(field_name)
            if value in (None, ""):
                continue
            parts.append(re.sub(r"\s+", "", str(value)).lower())
        return "|".join(parts) if parts else None

    @classmethod
    def _check_stable_record_conflicts(
        cls,
        issues: List[ValidationIssue],
        proposed_records: Iterable[Dict[str, Any]],
        existing_records: Iterable[Dict[str, Any]],
        record_type: str,
        immutable_fields: Iterable[str],
        identity_fields: Iterable[str] = (),
    ) -> None:
        existing_records = [
            record for record in existing_records if isinstance(record, dict)
        ]
        immutable_fields = tuple(immutable_fields)
        identity_fields = tuple(identity_fields)

        existing_by_id = {
            record.get("id"): record for record in existing_records if record.get("id")
        }
        existing_by_identity: Dict[str, Dict[str, Any]] = {}
        for record in existing_records:
            key = cls._identity_key(record, identity_fields)
            if key:
                existing_by_identity.setdefault(key, record)

        for proposed in proposed_records:
            record_id = proposed.get("id")
            existing = existing_by_id.get(record_id) if record_id else None
            matched_by_identity = False

            if not existing:
                # The id did not match anything already accepted. Fall back to the
                # natural key so a renamed id cannot smuggle in a contradiction.
                key = cls._identity_key(proposed, identity_fields)
                existing = existing_by_identity.get(key) if key else None
                matched_by_identity = existing is not None

            if not existing:
                continue

            if matched_by_identity:
                issues.append(
                    ValidationIssue(
                        f"{record_type}_identity_reused",
                        f"{record_type} “{cls._identity_key(proposed, identity_fields)}”"
                        f" 已存在（id {existing.get('id')}），本章却使用了新 id {record_id}",
                        severity="warning",
                        repair_target="contract",
                        details={
                            "existing_id": existing.get("id"),
                            "proposed_id": record_id,
                            "identity_fields": list(identity_fields),
                        },
                    )
                )

            for field_name in immutable_fields:
                old_value = existing.get(field_name)
                new_value = proposed.get(field_name)
                if old_value in (None, "") or new_value in (None, ""):
                    continue
                if not values_conflict(old_value, new_value):
                    continue
                issues.append(
                    ValidationIssue(
                        f"{record_type}_fact_conflict",
                        f"{record_type} {existing.get('id') or record_id} 的 {field_name}"
                        f" 与已接受状态冲突",
                        repair_target="human_decision",
                        details={
                            "id": existing.get("id") or record_id,
                            "proposed_id": record_id,
                            "field": field_name,
                            "existing": old_value,
                            "proposed": new_value,
                        },
                    )
                )


class ChapterAcceptanceService:
    """Extract, validate, and commit one final chapter artifact."""

    def __init__(
        self,
        ledger: StoryLedgerManager,
        extractor: Optional[DefaultChapterDeltaExtractor] = None,
        artifact_validator: Optional[ArtifactValidator] = None,
        consistency_gate: Optional[CanonConsistencyGate] = None,
        profile: Optional[DomainProfile] = None,
    ):
        self.ledger = ledger
        self.profile = profile
        self.extractor = extractor or DefaultChapterDeltaExtractor()
        self.artifact_validator = artifact_validator or ArtifactValidator()
        self.consistency_gate = consistency_gate or CanonConsistencyGate()

    def resolve_profile(self, contract: Dict[str, Any]) -> DomainProfile:
        """Pick the profile that governs this commit.

        Resolved per call rather than at construction: the service is often built
        before the ledger has a case bible, and the contract itself records which
        profile produced it.
        """
        if self.profile:
            return self.profile
        contract_key = contract.get("domain_profile")
        if contract_key:
            return get_domain_profile(contract_key)
        return self.ledger.locked_profile() or GENERAL

    def accept(
        self,
        chapter_number: int,
        reviewed_content: str,
        contract: Dict[str, Any],
        chapter_review: Dict[str, Any],
        base_revision: int,
        chapter_path: str,
    ) -> ChapterAcceptanceResult:
        if not chapter_path or not os.path.isfile(chapter_path):
            report = ValidationReport(
                stage="artifact_validation",
                issues=[
                    ValidationIssue(
                        "chapter_file_missing",
                        f"章节文件不存在，不能提交状态：{chapter_path}",
                        repair_target="prose",
                    )
                ],
            )
            raise ChapterAcceptanceError(report)

        try:
            with open(chapter_path, "r", encoding="utf-8") as handle:
                final_content = handle.read()
        except (OSError, UnicodeError) as exc:
            report = ValidationReport(
                stage="artifact_validation",
                issues=[
                    ValidationIssue(
                        "chapter_file_unreadable",
                        f"无法读取章节文件：{exc}",
                        repair_target="prose",
                    )
                ],
            )
            raise ChapterAcceptanceError(report) from exc

        profile = self.resolve_profile(contract)
        delta = self.extractor.extract(
            chapter_number, final_content, contract, base_revision, profile
        )
        artifact_report = self.artifact_validator.validate(
            delta,
            final_content,
            source_hash(reviewed_content),
            contract,
        )
        artifact_report_path = self.ledger.save_review(
            chapter_number,
            "acceptance_artifact",
            artifact_report.to_dict(),
        )
        if not artifact_report.passed:
            self.ledger.record_conflicts(chapter_number, base_revision, artifact_report.to_dict())
            raise ChapterAcceptanceError(artifact_report)

        consistency_report = self.consistency_gate.validate(
            delta,
            self.ledger.load_case_bible(),
            self.ledger.load_suspense_ledger(),
            profile,
        )
        consistency_report_path = self.ledger.save_review(
            chapter_number,
            "acceptance_canon",
            consistency_report.to_dict(),
        )
        if not consistency_report.passed:
            self.ledger.record_conflicts(chapter_number, base_revision, consistency_report.to_dict())
            raise ChapterAcceptanceError(consistency_report)

        delta_path = self.ledger.save_chapter_delta(chapter_number, delta.to_dict())
        try:
            committed_revision = self.ledger.accept_chapter(
                chapter_number,
                contract,
                chapter_review,
                chapter_delta=delta.to_dict(),
                expected_revision=base_revision,
                delta_path=delta_path,
            )
        except RevisionConflictError as exc:
            commit_report = ValidationReport(
                stage="canon_commit",
                issues=[
                    ValidationIssue(
                        "revision_conflict",
                        "提交时 story revision 已变化，需要重新构造上下文",
                        repair_target="rebase",
                        details={
                            "base_revision": base_revision,
                            "current_revision": self.ledger.current_revision(),
                        },
                    )
                ],
            )
            self.ledger.record_conflicts(chapter_number, base_revision, commit_report.to_dict())
            raise ChapterAcceptanceError(commit_report) from exc
        return ChapterAcceptanceResult(
            delta=delta,
            artifact_report=artifact_report,
            consistency_report=consistency_report,
            committed_revision=committed_revision,
            delta_path=delta_path,
            artifact_report_path=artifact_report_path,
            consistency_report_path=consistency_report_path,
        )
