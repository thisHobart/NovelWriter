"""Persistent truth, suspense, and chapter-contract ledgers."""

from __future__ import annotations

import hashlib
import json
import os
from glob import glob
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from core.generation.helper_fns import read_json, write_json
from core.generation.prompt_context import normalize_story_parameters


LEDGER_VERSION = 1


def source_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _unique_extend(target: List[Any], values: Iterable[Any]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


class StoryLedgerManager:
    """Manage durable story truth without mixing it into generated prose files."""

    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self.ledger_dir = os.path.join(output_dir, "system", "story_ledgers")
        self.contract_dir = os.path.join(self.ledger_dir, "chapter_contracts")
        self.review_dir = os.path.join(output_dir, "quality", "legal_suspense_reviews")

    @property
    def case_bible_path(self) -> str:
        return os.path.join(self.ledger_dir, "case_bible.json")

    @property
    def suspense_ledger_path(self) -> str:
        return os.path.join(self.ledger_dir, "suspense_ledger.json")

    def chapter_contract_path(self, chapter_number: int) -> str:
        return os.path.join(self.contract_dir, f"chapter_{chapter_number}.json")

    def initialize(self, parameters: Optional[Dict[str, Any]] = None) -> None:
        os.makedirs(self.contract_dir, exist_ok=True)
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
            write_json(self.case_bible_path, case_bible)

        if not os.path.exists(self.suspense_ledger_path):
            suspense_ledger = {
                "version": LEDGER_VERSION,
                "clues": [],
                "evidence": [],
                "reader_knowledge": [],
                "character_knowledge": {},
                "personal_costs": {},
                "accepted_chapters": [],
                "created_at": now,
                "updated_at": now,
            }
            write_json(self.suspense_ledger_path, suspense_ledger)

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
        write_json(self.case_bible_path, saved)
        return saved

    def load_suspense_ledger(self) -> Dict[str, Any]:
        return read_json(self.suspense_ledger_path)

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
        write_json(self.chapter_contract_path(chapter_number), saved)
        return saved

    def save_review(self, chapter_number: int, stage: str, data: Dict[str, Any]) -> str:
        chapter_dir = os.path.join(self.review_dir, f"chapter_{chapter_number}")
        os.makedirs(chapter_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = os.path.join(chapter_dir, f"{stage}_{timestamp}.json")
        write_json(path, data)
        return path

    def accept_chapter(
        self,
        chapter_number: int,
        contract: Dict[str, Any],
        chapter_review: Dict[str, Any],
    ) -> None:
        ledger = self.load_suspense_ledger()
        _unique_extend(ledger.setdefault("reader_knowledge", []), contract.get("reader_knows_after", []))

        character_updates = contract.get("character_knowledge_after", {})
        if isinstance(character_updates, dict):
            for character, facts in character_updates.items():
                if not isinstance(facts, list):
                    continue
                known = ledger.setdefault("character_knowledge", {}).setdefault(character, [])
                _unique_extend(known, facts)

        for clue in contract.get("fair_play_clues", []):
            if not isinstance(clue, dict):
                continue
            clue_id = clue.get("id")
            existing = next((item for item in ledger.setdefault("clues", []) if item.get("id") == clue_id), None)
            if existing:
                existing.update(clue)
            else:
                ledger["clues"].append(clue)

        for evidence in contract.get("evidence_updates", []):
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

        cost = contract.get("personal_cost")
        cost_character = contract.get("cost_character") or "未指定人物"
        if cost:
            costs = ledger.setdefault("personal_costs", {}).setdefault(cost_character, [])
            _unique_extend(costs, [cost])

        accepted = ledger.setdefault("accepted_chapters", [])
        accepted_entry = {
            "chapter": chapter_number,
            "core_question": contract.get("core_question", ""),
            "irreversible_change": contract.get("irreversible_change", ""),
            "score": chapter_review.get("average_score", 0),
            "accepted_at": datetime.now().isoformat(),
        }
        accepted[:] = [item for item in accepted if item.get("chapter") != chapter_number]
        accepted.append(accepted_entry)
        ledger["updated_at"] = datetime.now().isoformat()
        write_json(self.suspense_ledger_path, ledger)


def compact_json(data: Any, max_chars: int = 16000) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    return text if len(text) <= max_chars else text[:max_chars] + "\n...（已截断）"
