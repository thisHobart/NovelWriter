"""Persistent truth, suspense, and chapter-contract ledgers."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from glob import glob
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from filelock import FileLock

from core.generation.helper_fns import read_json
from core.generation.prompt_context import normalize_story_parameters


LEDGER_VERSION = 2


def source_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _unique_extend(target: List[Any], values: Iterable[Any]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _upsert_records(target: List[Dict[str, Any]], values: Iterable[Dict[str, Any]]) -> None:
    """Apply current-state records while leaving the immutable delta as history."""
    for value in values:
        if not isinstance(value, dict):
            continue
        record_id = value.get("id")
        if not record_id:
            if value not in target:
                target.append(deepcopy(value))
            continue
        existing = next((item for item in target if item.get("id") == record_id), None)
        if existing:
            existing.update(deepcopy(value))
        else:
            target.append(deepcopy(value))


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
        now = datetime.now().isoformat()

        if not os.path.exists(self.case_bible_path):
            case_bible = {
                "version": LEDGER_VERSION,
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
                "legal_system": {
                    "model": "虚构法域；具体规则以世界观为准，并在全书保持一致",
                    "baseline_rules": [
                        "侦查机关负责调查和收集证据，不能代替检察机关提起公诉",
                        "搜查住宅原则上需要法官签发的搜查令或世界观明确规定的紧急例外",
                        "决定性物证必须记录来源、提取、封存、移交和检验过程",
                        "法医与技术结论必须可以复核，不能只依赖权力人物的口头判断",
                    ],
                },
                "created_at": now,
                "updated_at": now,
            }
            _atomic_write_json(self.case_bible_path, case_bible)

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
                "accepted_chapters": [],
                "chapter_commits": [],
                "unresolved_conflicts": [],
                "resolved_conflicts": [],
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
                "revision": 0,
                "facts": [],
                "timeline_events": [],
                "character_updates": [],
                "plot_threads": [],
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

    def load_case_bible(self) -> Dict[str, Any]:
        return read_json(self.case_bible_path)

    def load_design_context(self, max_chars: int = 60000) -> str:
        """Load stable whole-story design sources used to establish case truth."""
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
            if total + len(section) > max_chars:
                remaining = max_chars - total
                if remaining > 200:
                    sections.append(section[:remaining])
                break
            sections.append(section)
            total += len(section)
        return "".join(sections).strip()

    @staticmethod
    def case_bible_needs_refresh(case_bible: Dict[str, Any], design_context: str) -> bool:
        if not design_context:
            return False
        expected_hash = source_hash(design_context)
        if case_bible.get("status") in {"uninitialized", "degraded"}:
            return True
        return bool(
            case_bible.get("generated_from_design")
            and case_bible.get("source_hash") != expected_hash
        )

    def save_case_bible(self, case_bible: Dict[str, Any], design_context: str) -> Dict[str, Any]:
        saved = deepcopy(case_bible)
        saved["version"] = LEDGER_VERSION
        saved["status"] = "degraded" if saved.get("case_bible_warning") else "ready"
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
        return contract

    def save_contract(
        self,
        chapter_number: int,
        contract: Dict[str, Any],
        plan_content: str,
    ) -> Dict[str, Any]:
        saved = deepcopy(contract)
        saved["chapter"] = chapter_number
        saved["source_hash"] = source_hash(plan_content)
        saved["updated_at"] = datetime.now().isoformat()
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
            _upsert_records(ledger.setdefault("facts", []), accepted_facts)
            _upsert_records(
                ledger.setdefault("timeline_events", []),
                chapter_delta.get("timeline_events", []),
            )
            _upsert_records(
                ledger.setdefault("character_updates", []),
                chapter_delta.get("character_updates", []),
            )
            _upsert_records(
                ledger.setdefault("plot_threads", []),
                chapter_delta.get("plot_thread_updates", []),
            )

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


def compact_json(data: Any, max_chars: int = 16000) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    return text if len(text) <= max_chars else text[:max_chars] + "\n...（已截断）"
