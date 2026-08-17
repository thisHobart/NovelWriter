"""Chapter-delta extraction and pre-commit acceptance gates."""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from core.generation.story_ledger import (
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
NUMBER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])\d+(?:\.\d+)?"
    r"(?:秒|分钟|分|小时|天|周|月|年|人|件|起|次|米|公里|元|万|%)?"
)
VOLATILE_CONTRACT_FIELDS = {"created_at", "updated_at", "generated_at", "accepted_at"}


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


class DefaultChapterDeltaExtractor:
    """Build a deterministic delta envelope from final prose and contract declarations.

    The extractor deliberately does not claim to understand every prose fact. It
    records contract-declared state transitions and stable textual signals. A
    semantic/LLM extractor can replace this class without changing the commit path.
    """

    def extract(
        self,
        chapter_number: int,
        content: str,
        contract: Dict[str, Any],
        base_revision: int,
    ) -> ChapterDelta:
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
            clue_updates=_records(contract.get("fair_play_clues")),
            evidence_updates=_records(contract.get("evidence_updates")),
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
    ) -> ValidationReport:
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
                    f"案件底稿状态为 {case_status}；本次允许提交，但应补做案件底稿审计",
                    severity="warning",
                    repair_target="case_bible",
                )
            )

        self._check_stable_record_conflicts(
            issues,
            delta.clue_updates,
            suspense_ledger.get("clues", []),
            record_type="clue",
            immutable_fields=("true_meaning",),
        )
        self._check_stable_record_conflicts(
            issues,
            delta.evidence_updates,
            suspense_ledger.get("evidence", []),
            record_type="evidence",
            immutable_fields=("item", "origin", "type"),
        )
        existing_facts = list(case_bible.get("truth", []))
        existing_facts.extend(suspense_ledger.get("facts", []))
        self._check_stable_record_conflicts(
            issues,
            [*delta.facts_added, *delta.facts_confirmed],
            existing_facts,
            record_type="fact",
            immutable_fields=("fact", "value"),
        )
        existing_timeline = list(case_bible.get("chronology", []))
        existing_timeline.extend(suspense_ledger.get("timeline_events", []))
        self._check_stable_record_conflicts(
            issues,
            delta.timeline_events,
            existing_timeline,
            record_type="timeline",
            immutable_fields=("event", "time", "time_start", "time_end", "location_id"),
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
    def _check_stable_record_conflicts(
        issues: List[ValidationIssue],
        proposed_records: Iterable[Dict[str, Any]],
        existing_records: Iterable[Dict[str, Any]],
        record_type: str,
        immutable_fields: Iterable[str],
    ) -> None:
        existing_by_id = {
            record.get("id"): record
            for record in existing_records
            if isinstance(record, dict) and record.get("id")
        }
        for proposed in proposed_records:
            record_id = proposed.get("id")
            existing = existing_by_id.get(record_id)
            if not existing:
                continue
            for field_name in immutable_fields:
                old_value = existing.get(field_name)
                new_value = proposed.get(field_name)
                if old_value in (None, "") or new_value in (None, "") or old_value == new_value:
                    continue
                issues.append(
                    ValidationIssue(
                        f"{record_type}_fact_conflict",
                        f"{record_type} {record_id} 的 {field_name} 与已接受状态冲突",
                        repair_target="human_decision",
                        details={
                            "id": record_id,
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
    ):
        self.ledger = ledger
        self.extractor = extractor or DefaultChapterDeltaExtractor()
        self.artifact_validator = artifact_validator or ArtifactValidator()
        self.consistency_gate = consistency_gate or CanonConsistencyGate()

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

        delta = self.extractor.extract(chapter_number, final_content, contract, base_revision)
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
