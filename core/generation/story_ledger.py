"""Persistent truth, suspense, and chapter-contract ledgers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from glob import glob
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from filelock import FileLock

from core.generation.domain_profiles import DomainProfile, get_domain_profile, resolve_domain_profile
from core.generation.helper_fns import read_json
from core.generation.prompt_context import normalize_story_parameters


# v3：case_bible 的 `legal_system` 泛化为 `domain_rules`，并记录 `domain_profile`。
LEDGER_VERSION = 3

# 低于这个条数的底稿无法支撑跨章事实比对：章节契约要靠既有 truth 记录复用 id，
# 没有可引用的条目时每章都会重新发明一套事实。
MIN_CASE_BIBLE_TRUTHS = 3


def source_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _unique_extend(target: List[Any], values: Iterable[Any]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


# 人物属性的身份是「谁的哪项属性」，不是记录编号。chapter_acceptance 的一致性
# 闸门共用这个键，两边必须一致。
CHARACTER_ATTRIBUTE_KEY: Tuple[str, ...] = ("character", "attribute")


def _record_key(
    record: Dict[str, Any], key_fields: Tuple[str, ...]
) -> Optional[Tuple[str, ...]]:
    """Build the merge key for a record, or None when it carries no key."""
    if key_fields:
        parts = tuple(
            re.sub(r"\s+", "", str(record.get(field, ""))).lower()
            for field in key_fields
        )
        return parts if all(parts) else None
    record_id = record.get("id")
    return (str(record_id),) if record_id else None


def _upsert_records(
    target: List[Dict[str, Any]],
    values: Iterable[Dict[str, Any]],
    key_fields: Tuple[str, ...] = (),
) -> None:
    """Apply current-state records while leaving the immutable delta as history.

    `key_fields` overrides the default id-based merge for streams whose identity
    is compound. Character attributes are the motivating case: keying them by id
    alone would let one character's tenure and age overwrite each other.
    """
    for value in values:
        if not isinstance(value, dict):
            continue
        key = _record_key(value, key_fields)
        if key is None:
            if value not in target:
                target.append(deepcopy(value))
            continue
        existing = next(
            (item for item in target if _record_key(item, key_fields) == key), None
        )
        if existing:
            existing.update(deepcopy(value))
        else:
            target.append(deepcopy(value))


# 记录最后一次被章节声明或复述的章号。上下文预算不够时，越久没有被任何章节
# 提起的记录越先让位——契约要求「本章复述或依赖的既有事实必须放进
# facts_confirmed」，所以真正在用的记录会不断被刷新，自然留在前面。
TOUCHED_AT_FIELD = "_last_touched_chapter"


def _stamp_touched(
    records: Iterable[Dict[str, Any]], chapter_number: int
) -> List[Dict[str, Any]]:
    stamped: List[Dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        item = deepcopy(record)
        item[TOUCHED_AT_FIELD] = chapter_number
        stamped.append(item)
    return stamped


def _touched_at(record: Any) -> int:
    if not isinstance(record, dict):
        return 0
    try:
        return int(record.get(TOUCHED_AT_FIELD) or 0)
    except (TypeError, ValueError):
        return 0


def _atomic_write_json(path: str, data: Any) -> None:
    """Replace one JSON file atomically after the complete payload is written."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=f".{os.path.basename(path)}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = handle.name
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


def case_bible_gaps(
    case_bible: Dict[str, Any], profile: Optional[DomainProfile] = None
) -> List[str]:
    """Report why a story bible cannot ground cross-chapter checks yet.

    A bible that parsed as JSON is not the same as a bible that says anything.
    Without a central question and at least a few established truths, every
    downstream consistency check compares new chapters against an empty set and
    silently passes, which is exactly how a novel drifts apart chapter by
    chapter.  Reported as data so callers decide whether to block or warn.
    """
    gaps: List[str] = []
    if not str(case_bible.get("central_question", "")).strip():
        gaps.append("central_question 为空：全书没有确定要追问的核心问题")

    truths = [
        record
        for record in case_bible.get("truth", [])
        if isinstance(record, dict)
        and str(record.get("id", "")).strip()
        and str(record.get("fact", "")).strip()
    ]
    if len(truths) < MIN_CASE_BIBLE_TRUTHS:
        gaps.append(
            f"truth 只有 {len(truths)} 条可用记录，至少需要 {MIN_CASE_BIBLE_TRUTHS} 条"
            "（章节契约要靠它复用事实 id）"
        )

    if not [
        record
        for record in case_bible.get("chronology", [])
        if isinstance(record, dict) and str(record.get("event", "")).strip()
    ]:
        gaps.append("chronology 为空：没有可核对的故事真实时间线")

    conflict = case_bible.get("central_conflict")
    if not isinstance(conflict, dict) or not conflict:
        gaps.append("central_conflict 为空：没有定义全书对抗结构")
    elif profile is not None:
        missing = [
            key
            for key in profile.central_conflict_schema
            if not str(conflict.get(key, "")).strip()
        ]
        if missing:
            gaps.append("central_conflict 缺少字段：" + "、".join(missing))
    return gaps


class RevisionConflictError(RuntimeError):
    """Raised when a chapter commit is based on a stale story revision."""


class StoryLedgerManager:
    """Manage durable story truth without mixing it into generated prose files."""

    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self.ledger_dir = os.path.join(output_dir, "system", "story_ledgers")
        self.contract_dir = os.path.join(self.ledger_dir, "chapter_contracts")
        self.delta_dir = os.path.join(self.ledger_dir, "chapter_deltas")
        self.conflict_dir = os.path.join(self.ledger_dir, "conflicts")
        self.review_dir = os.path.join(output_dir, "quality", "legal_suspense_reviews")

    @property
    def case_bible_path(self) -> str:
        return os.path.join(self.ledger_dir, "case_bible.json")

    @property
    def suspense_ledger_path(self) -> str:
        return os.path.join(self.ledger_dir, "suspense_ledger.json")

    @property
    def suspense_ledger_lock_path(self) -> str:
        return os.path.join(self.ledger_dir, ".suspense_ledger.lock")

    def chapter_contract_path(self, chapter_number: int) -> str:
        return os.path.join(self.contract_dir, f"chapter_{chapter_number}.json")

    def chapter_delta_path(
        self,
        chapter_number: int,
        base_revision: Optional[int] = None,
        content_hash: str = "",
        contract_hash: str = "",
    ) -> str:
        if base_revision is None and not content_hash and not contract_hash:
            return os.path.join(self.delta_dir, f"chapter_{chapter_number}.json")
        content_suffix = content_hash[:12] or "unknown"
        contract_suffix = contract_hash[:12] or "unknown"
        return os.path.join(
            self.delta_dir,
            f"chapter_{chapter_number}_base_{base_revision}_{content_suffix}_{contract_suffix}.json",
        )

    def initialize(self, parameters: Optional[Dict[str, Any]] = None) -> None:
        os.makedirs(self.contract_dir, exist_ok=True)
        os.makedirs(self.delta_dir, exist_ok=True)
        os.makedirs(self.conflict_dir, exist_ok=True)
        os.makedirs(self.review_dir, exist_ok=True)
        params = normalize_story_parameters(parameters)
        profile = resolve_domain_profile(params)
        now = datetime.now().isoformat()

        if not os.path.exists(self.case_bible_path):
            case_bible = {
                "version": LEDGER_VERSION,
                "domain_profile": profile.key,
                "project": {
                    "genre": params.get("Genre", ""),
                    "subgenre": params.get("Subgenre", ""),
                    "theme": params.get("Theme", ""),
                    "tone": params.get("Tone", ""),
                },
                "central_question": params.get("Theme", ""),
                "status": "uninitialized",
                "generated_from_design": False,
                "source_hash": "",
                "truth": [],
                "chronology": [],
                "central_conflict": {},
                "fair_play_obligations": [],
                "domain_rules": {
                    "model": profile.rules_model,
                    "baseline_rules": list(profile.baseline_rules),
                },
                "created_at": now,
                "updated_at": now,
            }
            _atomic_write_json(self.case_bible_path, case_bible)
        else:
            self._migrate_case_bible(profile, now)

        if not os.path.exists(self.suspense_ledger_path):
            suspense_ledger = {
                "version": LEDGER_VERSION,
                "clues": [],
                "evidence": [],
                "reader_knowledge": [],
                "character_knowledge": {},
                "personal_costs": {},
                "facts": [],
                "timeline_events": [],
                "character_updates": [],
                "plot_threads": [],
                "narrative_node_states": {},
                "pending_narrative_suggestions": [],
                "applied_chapter_deltas": [],
                "accepted_chapters": [],
                "chapter_commits": [],
                "unresolved_conflicts": [],
                "resolved_conflicts": [],
                "pending_regenerations": [],
                "completed_regenerations": [],
                "revision": 0,
                "created_at": now,
                "updated_at": now,
            }
            _atomic_write_json(self.suspense_ledger_path, suspense_ledger)
        else:
            suspense_ledger = self.load_suspense_ledger()
            migrated = False
            defaults = {
                "version": LEDGER_VERSION,
                "chapter_commits": [],
                "unresolved_conflicts": [],
                "resolved_conflicts": [],
                "pending_regenerations": [],
                "completed_regenerations": [],
                "revision": 0,
                "facts": [],
                "timeline_events": [],
                "character_updates": [],
                "plot_threads": [],
                "narrative_node_states": {},
                "pending_narrative_suggestions": [],
                "applied_chapter_deltas": [],
            }
            for key, value in defaults.items():
                if key not in suspense_ledger:
                    suspense_ledger[key] = deepcopy(value)
                    migrated = True
            if suspense_ledger.get("version") != LEDGER_VERSION:
                suspense_ledger["version"] = LEDGER_VERSION
                migrated = True
            if migrated:
                suspense_ledger["updated_at"] = now
                _atomic_write_json(self.suspense_ledger_path, suspense_ledger)

    def _migrate_case_bible(self, profile: DomainProfile, now: str) -> None:
        """Bring a pre-v3 case bible up to the domain-profile layout."""
        case_bible = self.load_case_bible()
        if not case_bible:
            return
        migrated = False

        legal_system = case_bible.pop("legal_system", None)
        if legal_system is not None and "domain_rules" not in case_bible:
            case_bible["domain_rules"] = legal_system
            migrated = True
        elif legal_system is not None:
            migrated = True

        if not case_bible.get("domain_profile"):
            # 迁移前只有法律悬疑会走闭环，所以带 legal_system 的底稿必定是法律悬疑；
            # 其余情况按当前参数分派。
            case_bible["domain_profile"] = (
                "legal_suspense" if legal_system is not None else profile.key
            )
            migrated = True

        if not isinstance(case_bible.get("domain_rules"), dict):
            case_bible["domain_rules"] = {
                "model": profile.rules_model,
                "baseline_rules": list(profile.baseline_rules),
            }
            migrated = True

        if case_bible.get("version") != LEDGER_VERSION:
            case_bible["version"] = LEDGER_VERSION
            migrated = True

        if migrated:
            case_bible["updated_at"] = now
            _atomic_write_json(self.case_bible_path, case_bible)

    def load_case_bible(self) -> Dict[str, Any]:
        return read_json(self.case_bible_path)

    def locked_profile(self) -> Optional[DomainProfile]:
        """Return the profile this project committed to, if the bible records one.

        The profile determines the review dimensions and thresholds, so switching
        it mid-book would make already-accepted chapters incomparable. Callers
        should prefer this over re-resolving from parameters.
        """
        key = self.load_case_bible().get("domain_profile")
        return get_domain_profile(key) if key else None

    def load_design_context(self, max_chars: int = 60000) -> str:
        """Load stable whole-story design sources used to establish case truth."""
        structure_contract = self.load_structure_contract()
        contract_section = ""
        if structure_contract:
            canonical_contract = json.dumps(
                structure_contract, ensure_ascii=False, sort_keys=True
            )
            contract_section = (
                "\n## 来源：story/structure/structure_contract.json"
                "（结构阶段的完整声明，优先于散文）\n"
                f"完整契约指纹：{source_hash(canonical_contract)}\n"
                + json.dumps(structure_contract, ensure_ascii=False, indent=2)
                + "\n"
            )

        # 为完整结构契约预留预算。即使契约异常庞大而需要截短，位于开头的完整
        # 指纹仍会让任何字段变化进入 case_bible 的 source_hash。
        reserved = min(len(contract_section), max_chars)
        prose_budget = max(0, max_chars - reserved)
        patterns = (
            os.path.join(self.output_dir, "story", "structure", "*.md"),
            os.path.join(
                self.output_dir,
                "story",
                "planning",
                "chapter_outlines",
                "*.md",
            ),
        )
        paths = sorted(path for pattern in patterns for path in glob(pattern))
        sections = []
        total = 0
        for path in paths:
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    content = handle.read().strip()
            except OSError:
                continue
            if not content:
                continue
            relative_path = os.path.relpath(path, self.output_dir)
            section = f"\n## 来源：{relative_path}\n{content}\n"
            if total + len(section) > prose_budget:
                remaining = prose_budget - total
                if remaining > 200:
                    partial = section[:remaining]
                    sections.append(partial)
                    total += len(partial)
                break
            sections.append(section)
            total += len(section)

        if contract_section:
            sections.append(contract_section[: max_chars - total])
        return "".join(sections).strip()

    def load_structure_contract(self) -> Dict[str, Any]:
        """Load the declarations the structure stage made about the whole story.

        These are authored, not inferred: preferring them over a second pass of
        prose summarising keeps the spine exactly as the design stage stated it.
        """
        path = os.path.join(self.output_dir, "story", "structure", "structure_contract.json")
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def declared_story_spine(self) -> Dict[str, Any]:
        """Flatten the per-section structure contracts into case-bible fields."""
        sections = self.load_structure_contract().get("sections", [])
        if not isinstance(sections, list) or not sections:
            return {}
        ordered = sorted(
            (item for item in sections if isinstance(item, dict)),
            key=lambda item: int(item.get("section_index", 0) or 0),
        )
        if not ordered:
            return {}

        spine: Dict[str, Any] = {}
        first = ordered[0]
        if str(first.get("central_question", "")).strip():
            spine["central_question"] = first["central_question"]
        if isinstance(first.get("central_conflict"), dict) and first["central_conflict"]:
            spine["central_conflict"] = deepcopy(first["central_conflict"])

        truths = []
        chronology = []
        for section in ordered:
            for record in section.get("truths_introduced", []):
                if not isinstance(record, dict) or not record.get("id"):
                    continue
                truths.append(
                    {
                        "id": record["id"],
                        "fact": record.get("fact", ""),
                        "source": f"structure:{section.get('section', '')}",
                        "must_not_reveal_before": record.get("reveal_at_section", ""),
                    }
                )
            for record in section.get("chronology_events", []):
                if not isinstance(record, dict) or not record.get("id"):
                    continue
                chronology.append(deepcopy(record))
        if truths:
            spine["truth"] = truths
        if chronology:
            chronology.sort(key=lambda item: int(item.get("order", 0) or 0))
            spine["chronology"] = chronology
        return spine

    def merge_declared_story_spine(
        self, case_bible: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Merge authored declarations into an inferred bible without data loss.

        Declared values win when they describe the same field or record, while
        additional truths and chronology extracted from chapter outlines remain
        available.  This is deliberately not ``dict.update``: replacing whole
        lists would throw away useful design facts merely because the structure
        contract did not repeat them.
        """
        merged = deepcopy(case_bible)
        declared = self.declared_story_spine()
        if not declared:
            return merged

        if "central_question" in declared:
            merged["central_question"] = declared["central_question"]
        if "central_conflict" in declared:
            conflict = deepcopy(merged.get("central_conflict", {}))
            if not isinstance(conflict, dict):
                conflict = {}
            conflict.update(deepcopy(declared["central_conflict"]))
            merged["central_conflict"] = conflict

        def merge_records(
            current: Any,
            authored: Any,
            *,
            fallback_field: str,
        ) -> List[Dict[str, Any]]:
            result = [
                deepcopy(item) for item in (current or []) if isinstance(item, dict)
            ]
            for record in (authored or []):
                if not isinstance(record, dict):
                    continue
                record_id = str(record.get("id", "")).strip()
                fallback = re.sub(
                    r"\s+", "", str(record.get(fallback_field, ""))
                ).lower()
                existing = next(
                    (
                        item for item in result
                        if (
                            record_id
                            and str(item.get("id", "")).strip() == record_id
                        )
                        or (
                            fallback
                            and re.sub(
                                r"\s+", "", str(item.get(fallback_field, ""))
                            ).lower() == fallback
                        )
                    ),
                    None,
                )
                if existing is None:
                    result.append(deepcopy(record))
                else:
                    # Authored structure wins when it actually says something,
                    # but schema placeholders such as an empty reveal deadline
                    # must not erase a concrete deadline already extracted into
                    # the case bible.
                    meaningful = {
                        key: deepcopy(value)
                        for key, value in record.items()
                        if value is not None
                        and not (isinstance(value, str) and not value.strip())
                    }
                    existing.update(meaningful)
            return result

        merged["truth"] = merge_records(
            merged.get("truth", []), declared.get("truth", []), fallback_field="fact"
        )
        merged["chronology"] = merge_records(
            merged.get("chronology", []),
            declared.get("chronology", []),
            fallback_field="event",
        )
        def chronology_order(item: Dict[str, Any]) -> int:
            try:
                return int(item.get("order", 10**9) or 10**9)
            except (TypeError, ValueError):
                return 10**9

        merged["chronology"].sort(key=chronology_order)
        return merged

    @staticmethod
    def case_bible_needs_refresh(case_bible: Dict[str, Any], design_context: str) -> bool:
        if not design_context:
            return False
        expected_hash = source_hash(design_context)
        if case_bible.get("status") in {"uninitialized", "degraded", "incomplete"}:
            return True
        return bool(
            case_bible.get("generated_from_design")
            and case_bible.get("source_hash") != expected_hash
        )

    def save_case_bible(self, case_bible: Dict[str, Any], design_context: str) -> Dict[str, Any]:
        saved = deepcopy(case_bible)
        saved["version"] = LEDGER_VERSION
        if not saved.get("domain_profile"):
            saved["domain_profile"] = self.load_case_bible().get("domain_profile", "")
        profile = get_domain_profile(saved.get("domain_profile", ""))
        gaps = case_bible_gaps(saved, profile)
        saved["gaps"] = gaps
        if saved.get("case_bible_warning"):
            saved["status"] = "degraded"
        else:
            saved["status"] = "incomplete" if gaps else "ready"
        saved["generated_from_design"] = True
        saved["source_hash"] = source_hash(design_context)
        saved["updated_at"] = datetime.now().isoformat()
        _atomic_write_json(self.case_bible_path, saved)
        return saved

    def load_suspense_ledger(self) -> Dict[str, Any]:
        return read_json(self.suspense_ledger_path)

    def current_revision(self) -> int:
        return int(self.load_suspense_ledger().get("revision", 0) or 0)

    def load_contract(self, chapter_number: int, plan_content: str = "") -> Optional[Dict[str, Any]]:
        path = self.chapter_contract_path(chapter_number)
        if not os.path.exists(path):
            return None
        contract = read_json(path)
        if plan_content and contract.get("source_hash") != source_hash(plan_content):
            return None
        # A graph revision mismatch may be harmless (an unrelated node changed)
        # or may invalidate this exact plan.  Reconcile explicitly and persist
        # the audit result; never let generation silently ignore the mismatch.
        from core.generation.narrative_graph import NarrativeGraphManager

        graph_manager = NarrativeGraphManager(self.output_dir)
        reconciled, _ = graph_manager.reconcile_contract(
            contract, self.load_suspense_ledger()
        )
        if reconciled != contract:
            _atomic_write_json(path, reconciled)
        return reconciled

    def save_contract(
        self,
        chapter_number: int,
        contract: Dict[str, Any],
        plan_content: str,
    ) -> Dict[str, Any]:
        from core.generation.narrative_graph import NarrativeGraphError, NarrativeGraphManager

        self.initialize()
        saved = deepcopy(contract)
        saved["chapter"] = chapter_number
        saved["source_hash"] = source_hash(plan_content)
        saved["updated_at"] = datetime.now().isoformat()
        graph_manager = NarrativeGraphManager(self.output_dir)
        current_graph_revision = graph_manager.current_revision()
        if (
            "narrative_graph_revision" in saved
            and int(saved.get("narrative_graph_revision", 0) or 0)
            != current_graph_revision
        ):
            saved, revision_issues = graph_manager.reconcile_contract(
                saved, self.load_suspense_ledger()
            )
            if any(issue.get("severity") == "error" for issue in revision_issues):
                raise NarrativeGraphError(revision_issues)
        saved["narrative_graph_revision"] = current_graph_revision
        saved["stale"] = False
        issues = graph_manager.validate_contract(
            saved, self.load_suspense_ledger(), ignore_revision=True
        )
        if any(issue.get("severity") == "error" for issue in issues):
            raise NarrativeGraphError(issues)
        saved["narrative_graph_validation"] = issues
        _atomic_write_json(self.chapter_contract_path(chapter_number), saved)
        return saved

    def replace_saved_contract(
        self, chapter_number: int, contract: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Persist a human ruling without changing the plan source identity."""
        existing = self.load_contract(int(chapter_number)) or {}
        saved = deepcopy(contract)
        saved.setdefault("source_hash", existing.get("source_hash", ""))
        return self.save_contract(
            int(chapter_number), saved, ""
        ) if not saved.get("source_hash") else self._replace_contract_with_graph_validation(
            int(chapter_number), saved
        )

    def _replace_contract_with_graph_validation(
        self, chapter_number: int, contract: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Validate a replacement while preserving its original plan hash."""
        from core.generation.narrative_graph import NarrativeGraphError, NarrativeGraphManager

        saved = deepcopy(contract)
        saved["chapter"] = int(chapter_number)
        saved["updated_at"] = datetime.now().isoformat()
        graph_manager = NarrativeGraphManager(self.output_dir)
        current_graph_revision = graph_manager.current_revision()
        if (
            "narrative_graph_revision" in saved
            and int(saved.get("narrative_graph_revision", 0) or 0)
            != current_graph_revision
        ):
            saved, revision_issues = graph_manager.reconcile_contract(
                saved, self.load_suspense_ledger()
            )
            if any(issue.get("severity") == "error" for issue in revision_issues):
                raise NarrativeGraphError(revision_issues)
        saved["narrative_graph_revision"] = current_graph_revision
        saved["stale"] = False
        issues = graph_manager.validate_contract(
            saved, self.load_suspense_ledger(), ignore_revision=True
        )
        if any(issue.get("severity") == "error" for issue in issues):
            raise NarrativeGraphError(issues)
        saved["narrative_graph_validation"] = issues
        _atomic_write_json(self.chapter_contract_path(chapter_number), saved)
        return saved

    def save_review(self, chapter_number: int, stage: str, data: Dict[str, Any]) -> str:
        chapter_dir = os.path.join(self.review_dir, f"chapter_{chapter_number}")
        os.makedirs(chapter_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = os.path.join(chapter_dir, f"{stage}_{timestamp}.json")
        _atomic_write_json(path, data)
        return path

    def save_chapter_delta(self, chapter_number: int, chapter_delta: Dict[str, Any]) -> str:
        path = self.chapter_delta_path(
            chapter_number,
            base_revision=int(chapter_delta.get("base_revision", 0) or 0),
            content_hash=str(chapter_delta.get("content_hash", "")),
            contract_hash=str(chapter_delta.get("contract_hash", "")),
        )
        _atomic_write_json(path, chapter_delta)
        return path

    def accept_chapter_with_delta(
        self,
        chapter_number: int,
        contract: Dict[str, Any],
        chapter_review: Dict[str, Any],
        chapter_delta: Dict[str, Any],
        expected_revision: int,
    ) -> Tuple[int, str]:
        """Atomically coordinate Delta persistence with the locked Ledger commit.

        A failed commit removes a newly prepared Delta, so callers never observe
        a durable Delta that was not applied to the StoryLedger.  Repeating the
        same content/contract pair reuses its deterministic path and remains
        idempotent.
        """
        from core.generation.narrative_graph import NarrativeGraphError, NarrativeGraphManager

        graph_manager = NarrativeGraphManager(self.output_dir)
        ledger_snapshot = self.load_suspense_ledger()
        issues = graph_manager.validate_delta(chapter_delta, ledger_snapshot)
        if any(issue.get("severity") == "error" for issue in issues):
            raise NarrativeGraphError(issues)
        graph = graph_manager.load()
        path = self.chapter_delta_path(
            chapter_number,
            base_revision=int(chapter_delta.get("base_revision", 0) or 0),
            content_hash=str(chapter_delta.get("content_hash", "")),
            contract_hash=str(chapter_delta.get("contract_hash", "")),
        )
        existed = os.path.exists(path)
        with FileLock(self.suspense_ledger_lock_path):
            try:
                _atomic_write_json(path, chapter_delta)
                revision = self._accept_chapter_locked(
                    chapter_number,
                    contract,
                    chapter_review,
                    chapter_delta=chapter_delta,
                    expected_revision=expected_revision,
                    delta_path=path,
                    narrative_graph=graph,
                    narrative_graph_manager=graph_manager,
                )
            except Exception:
                if not existed and os.path.exists(path):
                    os.remove(path)
                raise
        return revision, path

    def record_conflicts(
        self,
        chapter_number: int,
        base_revision: int,
        report: Dict[str, Any],
    ) -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        conflict_id = f"chapter_{chapter_number}_{timestamp}"
        conflict = {
            "id": conflict_id,
            "chapter": chapter_number,
            "base_revision": base_revision,
            "status": "open",
            "report": deepcopy(report),
            "created_at": datetime.now().isoformat(),
        }
        path = os.path.join(self.conflict_dir, f"{conflict_id}.json")
        _atomic_write_json(path, conflict)

        with FileLock(self.suspense_ledger_lock_path):
            ledger = self.load_suspense_ledger()
            ledger.setdefault("unresolved_conflicts", []).append(
                {
                    "id": conflict_id,
                    "chapter": chapter_number,
                    "base_revision": base_revision,
                    "stage": report.get("stage", "unknown"),
                    "path": os.path.relpath(path, self.output_dir),
                    "created_at": conflict["created_at"],
                }
            )
            ledger["updated_at"] = datetime.now().isoformat()
            _atomic_write_json(self.suspense_ledger_path, ledger)
        return path

    def approve_contradiction(
        self, chapter_number: int, record_id: str, note: str = ""
    ) -> None:
        """Record that the author signed off on this chapter overturning a fact.

        The gate blocks a declared reversal because only a person can say
        whether overturning established canon is the story working or the story
        breaking.  Once they have said so, the answer has to be durable — asking
        again on every retry is how a chapter becomes unwritable.
        """
        with FileLock(self.suspense_ledger_lock_path):
            ledger = self.load_suspense_ledger()
            approvals = ledger.setdefault("approved_contradictions", [])
            key = {"chapter": int(chapter_number), "id": str(record_id)}
            if not any(
                item.get("chapter") == key["chapter"] and str(item.get("id")) == key["id"]
                for item in approvals
                if isinstance(item, dict)
            ):
                approvals.append(
                    {**key, "note": note, "approved_at": datetime.now().isoformat()}
                )
                ledger["updated_at"] = datetime.now().isoformat()
                _atomic_write_json(self.suspense_ledger_path, ledger)

    def approved_contradictions(self, chapter_number: int) -> Set[str]:
        return {
            str(item.get("id"))
            for item in self.load_suspense_ledger().get("approved_contradictions", [])
            if isinstance(item, dict) and item.get("chapter") == int(chapter_number)
        }

    def require_chapter_regeneration(
        self,
        chapter_number: int,
        reason: str,
        record_ids: Iterable[str] = (),
    ) -> Dict[str, Any]:
        """Persist that an author's ruling invalidated all previously generated prose."""
        created_at = datetime.now().isoformat()
        marker = {
            "id": f"chapter_{int(chapter_number)}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}",
            "chapter": int(chapter_number),
            "reason": str(reason),
            "record_ids": sorted({str(item) for item in record_ids if str(item)}),
            "created_at": created_at,
        }
        with FileLock(self.suspense_ledger_lock_path):
            ledger = self.load_suspense_ledger()
            pending = ledger.setdefault("pending_regenerations", [])
            pending[:] = [
                item
                for item in pending
                if not (
                    isinstance(item, dict)
                    and item.get("chapter") == int(chapter_number)
                )
            ]
            pending.append(marker)
            ledger["updated_at"] = created_at
            _atomic_write_json(self.suspense_ledger_path, ledger)
        return deepcopy(marker)

    def pending_chapter_regeneration(
        self, chapter_number: int
    ) -> Optional[Dict[str, Any]]:
        return next(
            (
                deepcopy(item)
                for item in self.load_suspense_ledger().get("pending_regenerations", [])
                if isinstance(item, dict)
                and item.get("chapter") == int(chapter_number)
            ),
            None,
        )

    def complete_chapter_regeneration(
        self, chapter_number: int, marker_id: str
    ) -> bool:
        """Clear exactly the ruling marker satisfied by the accepted new prose."""
        completed_at = datetime.now().isoformat()
        with FileLock(self.suspense_ledger_lock_path):
            ledger = self.load_suspense_ledger()
            pending = ledger.setdefault("pending_regenerations", [])
            marker = next(
                (
                    item
                    for item in pending
                    if isinstance(item, dict)
                    and item.get("chapter") == int(chapter_number)
                    and str(item.get("id", "")) == str(marker_id)
                ),
                None,
            )
            if marker is None:
                return False
            pending.remove(marker)
            completed = deepcopy(marker)
            completed["completed_at"] = completed_at
            ledger.setdefault("completed_regenerations", []).append(completed)
            ledger["updated_at"] = completed_at
            _atomic_write_json(self.suspense_ledger_path, ledger)
        return True

    def open_conflicts(self) -> List[Dict[str, Any]]:
        """Every conflict still waiting on a decision, newest first."""
        conflicts = [
            deepcopy(item)
            for item in self.load_suspense_ledger().get("unresolved_conflicts", [])
            if isinstance(item, dict)
        ]
        conflicts.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return conflicts

    def load_conflict(self, conflict_id: str) -> Optional[Dict[str, Any]]:
        """The full stored report for one conflict, or None if it is gone."""
        path = os.path.join(self.conflict_dir, f"{conflict_id}.json")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def resolve_conflict(
        self,
        conflict_id: str,
        resolution: str,
        note: str = "",
    ) -> bool:
        """Close a conflict on a person's authority.

        Until now the only way out of ``unresolved_conflicts`` was for the same
        chapter to be accepted again.  A conflict the author decides to live
        with therefore stayed open forever — and because the acceptance gate
        refuses to commit while *any other* chapter has an open conflict, one
        undecided clash quietly froze the whole project.
        """
        resolved_at = datetime.now().isoformat()
        with FileLock(self.suspense_ledger_lock_path):
            ledger = self.load_suspense_ledger()
            unresolved = ledger.setdefault("unresolved_conflicts", [])
            entry = next(
                (item for item in unresolved if item.get("id") == conflict_id), None
            )
            if entry is None:
                return False
            unresolved.remove(entry)
            closed = deepcopy(entry)
            closed.update(
                {
                    "status": "resolved",
                    "resolution": resolution,
                    "note": note,
                    "resolved_by": "human",
                    "resolved_at": resolved_at,
                }
            )
            ledger.setdefault("resolved_conflicts", []).append(closed)
            ledger["updated_at"] = resolved_at
            _atomic_write_json(self.suspense_ledger_path, ledger)

        stored = self.load_conflict(conflict_id)
        if stored is not None:
            stored.update(
                {
                    "status": "resolved",
                    "resolution": resolution,
                    "note": note,
                    "resolved_by": "human",
                    "resolved_at": resolved_at,
                }
            )
            _atomic_write_json(
                os.path.join(self.conflict_dir, f"{conflict_id}.json"), stored
            )
        return True

    def accept_chapter(
        self,
        chapter_number: int,
        contract: Dict[str, Any],
        chapter_review: Dict[str, Any],
        chapter_delta: Optional[Dict[str, Any]] = None,
        expected_revision: Optional[int] = None,
        delta_path: str = "",
    ) -> int:
        with FileLock(self.suspense_ledger_lock_path):
            return self._accept_chapter_locked(
                chapter_number,
                contract,
                chapter_review,
                chapter_delta=chapter_delta,
                expected_revision=expected_revision,
                delta_path=delta_path,
            )

    def _accept_chapter_locked(
        self,
        chapter_number: int,
        contract: Dict[str, Any],
        chapter_review: Dict[str, Any],
        chapter_delta: Optional[Dict[str, Any]] = None,
        expected_revision: Optional[int] = None,
        delta_path: str = "",
        narrative_graph: Optional[Dict[str, Any]] = None,
        narrative_graph_manager: Any = None,
    ) -> int:
        ledger = self.load_suspense_ledger()
        current_revision = int(ledger.get("revision", 0) or 0)
        content_hash = (chapter_delta or {}).get("content_hash", "")
        contract_hash = (chapter_delta or {}).get("contract_hash", "")
        accepted = ledger.setdefault("accepted_chapters", [])
        previous_entry = next(
            (item for item in accepted if item.get("chapter") == chapter_number),
            None,
        )
        is_idempotent = bool(
            previous_entry
            and (
                (
                    content_hash
                    and contract_hash
                    and previous_entry.get("content_hash") == content_hash
                    and previous_entry.get("contract_hash") == contract_hash
                )
                or (not content_hash and not previous_entry.get("content_hash"))
            )
        )
        if expected_revision is not None and current_revision != expected_revision and not is_idempotent:
            raise RevisionConflictError(
                f"Expected story revision {expected_revision}, found {current_revision}"
            )

        knowledge_updates = (chapter_delta or {}).get("knowledge_updates", [])
        reader_facts = [
            update.get("fact")
            for update in knowledge_updates
            if isinstance(update, dict)
            and update.get("audience") == "reader"
            and update.get("fact")
        ]
        if not reader_facts:
            reader_facts = contract.get("reader_knows_after", [])
        _unique_extend(ledger.setdefault("reader_knowledge", []), reader_facts)

        character_updates = contract.get("character_knowledge_after", {})
        delta_character_updates: Dict[str, List[str]] = {}
        for update in knowledge_updates:
            if not isinstance(update, dict) or update.get("audience") != "character":
                continue
            character = update.get("character")
            fact = update.get("fact")
            if character and fact:
                delta_character_updates.setdefault(str(character), []).append(str(fact))
        if delta_character_updates:
            character_updates = delta_character_updates
        if isinstance(character_updates, dict):
            for character, facts in character_updates.items():
                if not isinstance(facts, list):
                    continue
                known = ledger.setdefault("character_knowledge", {}).setdefault(character, [])
                _unique_extend(known, facts)

        clue_updates = (chapter_delta or {}).get("clue_updates") or contract.get("fair_play_clues", [])
        for clue in clue_updates:
            if not isinstance(clue, dict):
                continue
            clue_id = clue.get("id")
            existing = next((item for item in ledger.setdefault("clues", []) if item.get("id") == clue_id), None)
            if existing:
                existing.update(clue)
            else:
                ledger["clues"].append(clue)

        evidence_updates = (chapter_delta or {}).get("evidence_updates") or contract.get("evidence_updates", [])
        for evidence in evidence_updates:
            if not isinstance(evidence, dict):
                continue
            evidence_id = evidence.get("id")
            existing = next(
                (
                    item
                    for item in ledger.setdefault("evidence", [])
                    if item.get("id") == evidence_id
                ),
                None,
            )
            if existing:
                existing.update(evidence)
            else:
                ledger["evidence"].append(evidence)

        if chapter_delta:
            accepted_facts = list(chapter_delta.get("facts_added", []))
            accepted_facts.extend(chapter_delta.get("facts_confirmed", []))
            _upsert_records(
                ledger.setdefault("facts", []),
                _stamp_touched(accepted_facts, chapter_number),
            )
            _upsert_records(
                ledger.setdefault("timeline_events", []),
                _stamp_touched(chapter_delta.get("timeline_events", []), chapter_number),
            )
            _upsert_records(
                ledger.setdefault("character_updates", []),
                _stamp_touched(
                    chapter_delta.get("character_updates", []), chapter_number
                ),
                key_fields=CHARACTER_ATTRIBUTE_KEY,
            )
            _upsert_records(
                ledger.setdefault("plot_threads", []),
                _stamp_touched(
                    chapter_delta.get("plot_thread_updates", []), chapter_number
                ),
            )

            if narrative_graph_manager is None:
                from core.generation.narrative_graph import NarrativeGraphManager

                narrative_graph_manager = NarrativeGraphManager(self.output_dir)
            if narrative_graph is None:
                narrative_graph = narrative_graph_manager.load()
            narrative_graph_manager.apply_transitions_to_ledger(
                narrative_graph, ledger, chapter_delta
            )
            suggestions = ledger.setdefault("pending_narrative_suggestions", [])
            for suggestion in chapter_delta.get("unregistered_narrative_elements", []):
                if not isinstance(suggestion, dict):
                    continue
                item = deepcopy(suggestion)
                item.setdefault("type", "unregistered_narrative_element")
                item.setdefault("chapter", chapter_number)
                identity = (
                    item.get("type"),
                    int(item.get("chapter", chapter_number) or chapter_number),
                    str(item.get("description", "")),
                )
                if not any(
                    isinstance(existing, dict)
                    and (
                        existing.get("type"),
                        int(existing.get("chapter", 0) or 0),
                        str(existing.get("description", "")),
                    )
                    == identity
                    for existing in suggestions
                ):
                    suggestions.append(item)

        personal_cost_updates = (chapter_delta or {}).get("personal_cost_updates", [])
        if personal_cost_updates:
            for update in personal_cost_updates:
                if not isinstance(update, dict) or not update.get("cost"):
                    continue
                cost_character = str(update.get("character") or "未指定人物")
                costs = ledger.setdefault("personal_costs", {}).setdefault(cost_character, [])
                _unique_extend(costs, [update["cost"]])
        else:
            cost = contract.get("personal_cost")
            cost_character = contract.get("cost_character") or "未指定人物"
            if cost:
                costs = ledger.setdefault("personal_costs", {}).setdefault(cost_character, [])
                _unique_extend(costs, [cost])

        committed_revision = current_revision if is_idempotent else current_revision + 1
        accepted_entry = {
            "chapter": chapter_number,
            "core_question": contract.get("core_question", ""),
            "irreversible_change": contract.get("irreversible_change", ""),
            "score": chapter_review.get("average_score", 0),
            "base_revision": expected_revision if expected_revision is not None else current_revision,
            "committed_revision": committed_revision,
            "content_hash": content_hash,
            "contract_hash": contract_hash,
            "delta_path": os.path.relpath(delta_path, self.output_dir) if delta_path else "",
            "accepted_at": datetime.now().isoformat(),
        }
        accepted[:] = [item for item in accepted if item.get("chapter") != chapter_number]
        accepted.append(accepted_entry)
        if not is_idempotent:
            ledger.setdefault("chapter_commits", []).append(deepcopy(accepted_entry))
        self._resolve_chapter_conflicts(ledger, chapter_number, committed_revision)
        ledger["revision"] = committed_revision
        if chapter_delta:
            delta_receipt = {
                "chapter": chapter_number,
                "content_hash": content_hash,
                "contract_hash": contract_hash,
                "committed_revision": committed_revision,
            }
            receipts = ledger.setdefault("applied_chapter_deltas", [])
            if not any(
                isinstance(item, dict)
                and item.get("content_hash") == content_hash
                and item.get("contract_hash") == contract_hash
                for item in receipts
            ):
                receipts.append(delta_receipt)
            cache = ledger.get("narrative_derived_cache")
            if isinstance(cache, dict):
                cache["ledger_revision"] = committed_revision
        ledger["version"] = LEDGER_VERSION
        ledger["updated_at"] = datetime.now().isoformat()
        _atomic_write_json(self.suspense_ledger_path, ledger)
        return committed_revision

    @staticmethod
    def _resolve_chapter_conflicts(
        ledger: Dict[str, Any],
        chapter_number: int,
        committed_revision: int,
    ) -> None:
        unresolved = ledger.setdefault("unresolved_conflicts", [])
        resolved = ledger.setdefault("resolved_conflicts", [])
        remaining = []
        resolved_at = datetime.now().isoformat()
        for conflict in unresolved:
            if conflict.get("chapter") != chapter_number:
                remaining.append(conflict)
                continue
            resolved_entry = deepcopy(conflict)
            resolved_entry.update(
                {
                    "status": "resolved",
                    "resolution": "superseded_by_accepted_revision",
                    "committed_revision": committed_revision,
                    "resolved_at": resolved_at,
                }
            )
            resolved.append(resolved_entry)
        unresolved[:] = remaining


def build_ledger_prompt_view(
    ledger: Dict[str, Any],
    chapter_number: int = 0,
) -> Dict[str, Any]:
    """Return the current story state needed by generation prompts.

    Commit history, accepted chapter receipts and resolved conflicts are audit
    data, not generation context.  Keeping them out prevents the useful state
    near the end of the ledger from being displaced as a novel grows.
    """

    def deadline(record: Dict[str, Any]) -> int:
        try:
            return int(record.get("deadline_chapter") or 10**9)
        except (TypeError, ValueError):
            return 10**9

    threads = [
        deepcopy(item)
        for item in ledger.get("plot_threads", [])
        if isinstance(item, dict) and str(item.get("status", "open")).lower() != "closed"
    ]
    threads.sort(
        key=lambda item: (
            0 if chapter_number and deadline(item) <= chapter_number else 1,
            deadline(item),
            str(item.get("id", "")),
        )
    )
    conflicts = [
        deepcopy(item)
        for item in ledger.get("unresolved_conflicts", [])
        if isinstance(item, dict)
    ]

    def by_relevance(key: str) -> List[Any]:
        """Most recently touched first, so budget trimming drops the stalest."""
        records = [deepcopy(item) for item in ledger.get(key, [])]
        indexed = list(enumerate(records))
        indexed.sort(key=lambda pair: (-_touched_at(pair[1]), pair[0]))
        return [record for _, record in indexed]

    return {
        "revision": int(ledger.get("revision", 0) or 0),
        "clues": deepcopy(ledger.get("clues", [])),
        "evidence": deepcopy(ledger.get("evidence", [])),
        "reader_knowledge": deepcopy(ledger.get("reader_knowledge", [])),
        "character_knowledge": deepcopy(ledger.get("character_knowledge", {})),
        "personal_costs": deepcopy(ledger.get("personal_costs", {})),
        "facts": by_relevance("facts"),
        "timeline_events": by_relevance("timeline_events"),
        "character_updates": by_relevance("character_updates"),
        "open_plot_threads": threads,
        "narrative_node_states": deepcopy(ledger.get("narrative_node_states", {})),
        "pending_narrative_suggestions": deepcopy(
            ledger.get("pending_narrative_suggestions", [])
        ),
        "unresolved_conflicts": conflicts,
        "_context_meta": {
            "chapter": chapter_number,
            "excluded_audit_fields": [
                "accepted_chapters",
                "chapter_commits",
                "resolved_conflicts",
            ],
            "source_counts": {
                key: len(value)
                for key, value in ledger.items()
                if isinstance(value, list)
            },
        },
    }


# 最小截断信封 {"_context_meta":{"truncated":true,"context_overflow":true}} 缩进后
# 就是这么大，压不下去。低于这个预算无法兑现「输出不超过 max_chars」的约定，
# 与其静默超标不如直接报错。
MIN_COMPACT_JSON_CHARS = 80


def compact_json(data: Any, max_chars: int = 16000) -> str:
    """Serialize a bounded context without ever cutting JSON text in half.

    Every list keeps the same fraction of its items, so no single stream is
    drained while others stay whole; long strings are shortened only as a last
    resort.  The output always remains parseable JSON and carries omission
    metadata so a model is never shown a silently truncated document.
    """
    if max_chars < MIN_COMPACT_JSON_CHARS:
        raise ValueError(
            f"max_chars 至少需要 {MIN_COMPACT_JSON_CHARS}，否则连截断信封都放不下"
        )

    working = deepcopy(data)
    omitted: Dict[str, int] = {}

    def dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, indent=2)

    def list_candidates(value: Any, path: str = "$") -> List[Tuple[str, List[Any]]]:
        found: List[Tuple[str, List[Any]]] = []
        if isinstance(value, list):
            if value:
                found.append((path, value))
            for index, item in enumerate(value):
                found.extend(list_candidates(item, f"{path}[{index}]"))
        elif isinstance(value, dict):
            for key, item in value.items():
                if key != "_context_meta":
                    found.extend(list_candidates(item, f"{path}.{key}"))
        return found

    def string_candidates(value: Any, path: str = "$") -> List[Tuple[int, str, Any, Any]]:
        found: List[Tuple[int, str, Any, Any]] = []
        if isinstance(value, dict):
            for key, item in value.items():
                child_path = f"{path}.{key}"
                if isinstance(item, str) and len(item) > 32:
                    found.append((len(item), child_path, value, key))
                else:
                    found.extend(string_candidates(item, child_path))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                child_path = f"{path}[{index}]"
                if isinstance(item, str) and len(item) > 32:
                    found.append((len(item), child_path, value, index))
                else:
                    found.extend(string_candidates(item, child_path))
        return found

    def update_omission_meta() -> None:
        if not omitted or not isinstance(working, dict):
            return
        meta = working.setdefault("_context_meta", {})
        if not isinstance(meta, dict):
            meta = {"original_context_meta": meta}
            working["_context_meta"] = meta
        meta["truncated"] = True
        meta["omitted"] = omitted

    # Proportional reduction is a one-parameter problem: every list keeps the
    # same fraction of its items.  Binary-searching that fraction costs a
    # handful of serializations instead of one per dropped item, which is what
    # made a 120-chapter ledger spend minutes in here.
    snapshots = [(path, target, list(target)) for path, target in list_candidates(working)]

    def apply_fraction(fraction: float) -> int:
        omitted.clear()
        for path, target, original in snapshots:
            keep = len(original) if fraction >= 1.0 else int(len(original) * fraction)
            target[:] = original[:keep]
            dropped = len(original) - keep
            if dropped:
                omitted[path] = dropped
        update_omission_meta()
        return len(dump(working))

    if snapshots and apply_fraction(1.0) > max_chars:
        low, high = 0.0, 1.0
        best = 0.0
        for _ in range(24):
            middle = (low + high) / 2
            if apply_fraction(middle) <= max_chars:
                best, low = middle, middle
            else:
                high = middle
        apply_fraction(best)

    while len(dump(working)) > max_chars:
        strings = string_candidates(working)
        if not strings:
            break
        length, path, parent, key = max(strings, key=lambda item: item[0])
        parent[key] = parent[key][: max(16, length // 2)] + "…"
        omitted[path] = omitted.get(path, 0) + 1
        update_omission_meta()

    text = dump(working)
    # Metadata itself can exceed a very small budget.  Return a minimal, valid
    # envelope rather than violating the size contract or emitting broken JSON.
    if len(text) > max_chars:
        minimal = {"_context_meta": {"truncated": True, "context_overflow": True}}
        text = dump(minimal)
    return text
