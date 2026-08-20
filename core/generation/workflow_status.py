"""Derive each workflow stage's real state from the artifacts on disk.

The persisted orchestrator flags only record what the agentic runner did.  A
stage driven from its own tab leaves no flag at all, so the panel used to fall
back to "files exist" — which cannot tell a finished plan from a half-finished
one, and cannot tell either from a plan that will be rejected the moment
writing starts.  These checks reuse the same gates the pipeline enforces, so
the panel and the generator can never disagree.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from glob import glob
from typing import Dict, List, Optional

from core.generation.design_contract import DesignContractError, validate_structure_sequence
from core.generation.planning_contract import (
    PlanningContractError,
    load_planning_contracts,
    validate_contract_sequence,
)
from core.generation.story_ledger import StoryLedgerManager

COMPLETE = "complete"
PARTIAL = "partial"
BLOCKED = "blocked"
EMPTY = "empty"

STEP_ORDER = ("lore", "structure", "scenes", "chapters")

# Matches "第 3 章", "Chapter 3", "### 第3章：标题" — the same headings scene
# planning counts, so the expected chapter total agrees across the app.
_CHAPTER_HEADING = re.compile(
    r"^\s*(?:#{2,6}\s*|\*{2,}\s*)?(?:Chapter\s*|第\s*)(\d+)(?:\s*章)?"
    r"(?:\s*[:：.\-]?\s*.*?)?\s*(?:\*{2,})?\s*$",
    re.MULTILINE | re.IGNORECASE,
)


@dataclass(frozen=True)
class StageStatus:
    """One stage's state, plus enough detail to act on it."""

    state: str
    detail: str
    done: int = 0
    total: int = 0

    @property
    def is_complete(self) -> bool:
        return self.state == COMPLETE


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeError):
        return ""


def expected_chapter_count(output_dir: str) -> int:
    """Chapters promised by the chapter outlines, deduplicated across sections."""
    outline_dir = os.path.join(output_dir, "story", "planning", "chapter_outlines")
    numbers: set[int] = set()
    for path in sorted(glob(os.path.join(outline_dir, "chapter_outlines_*.md"))):
        numbers.update(int(found) for found in _CHAPTER_HEADING.findall(_read(path)))
    return len(numbers)


def _assess_lore(output_dir: str) -> StageStatus:
    lore_dir = os.path.join(output_dir, "story", "lore")
    body = _read(os.path.join(lore_dir, "generated_lore.md")).strip()
    if not body:
        return StageStatus(EMPTY, "尚未生成世界观")
    if not os.path.isfile(os.path.join(lore_dir, "lore_contract.json")):
        return StageStatus(PARTIAL, "有世界观正文，但缺少名称契约，建议重新生成")
    return StageStatus(COMPLETE, "世界观与名称契约齐备")


def _assess_structure(output_dir: str) -> StageStatus:
    structure_dir = os.path.join(output_dir, "story", "structure")
    outlines = glob(os.path.join(structure_dir, "**", "*.md"), recursive=True)
    sections = StoryLedgerManager(output_dir).load_structure_contract().get("sections", [])
    if not sections:
        if not outlines:
            return StageStatus(EMPTY, "尚未生成故事结构")
        return StageStatus(PARTIAL, "有结构大纲，但缺少结构契约，建议重新生成")
    try:
        validate_structure_sequence(sections)
    except DesignContractError as exc:
        return StageStatus(BLOCKED, str(exc), len(sections), len(sections))
    return StageStatus(
        COMPLETE, f"{len(sections)} 个阶段的结构契约已通过校验", len(sections), len(sections)
    )


def _assess_scenes(output_dir: str, expected: int) -> StageStatus:
    try:
        contracts = load_planning_contracts(output_dir)
    except PlanningContractError as exc:
        return StageStatus(BLOCKED, str(exc))
    planned = len(contracts)
    total = expected or planned
    if not planned:
        return StageStatus(EMPTY, "尚未规划场景", 0, total)
    if expected and planned < expected:
        missing = sorted(
            set(range(1, expected + 1)) - {int(item["chapter"]) for item in contracts}
        )
        preview = "、".join(str(number) for number in missing[:6])
        return StageStatus(PARTIAL, f"还缺第 {preview} 章的场景规划", planned, total)
    try:
        # None means "do not demand a chapter count we could not determine".
        validate_contract_sequence(contracts, total_chapters=expected or None)
    except PlanningContractError as exc:
        return StageStatus(BLOCKED, str(exc), planned, total)
    return StageStatus(COMPLETE, f"{planned} 章场景规划已通过跨章校验", planned, total)



def _written_chapters(output_dir: str) -> List[int]:
    numbers: set[int] = set()
    for directory in ("story/content/chapters", "story/content"):
        for path in glob(os.path.join(output_dir, directory, "chapter_*.md")):
            match = re.search(r"chapter_(\d+)\.md$", os.path.basename(path))
            if match and _read(path).strip():
                numbers.add(int(match.group(1)))
    return sorted(numbers)


def _assess_chapters(output_dir: str, expected: int) -> StageStatus:
    written = _written_chapters(output_dir)
    total = expected or len(written)
    if not written:
        return StageStatus(EMPTY, "尚未撰写正文", 0, total)
    if expected and len(written) < expected:
        missing = sorted(set(range(1, expected + 1)) - set(written))
        preview = "、".join(str(number) for number in missing[:6])
        return StageStatus(PARTIAL, f"还缺第 {preview} 章正文", len(written), total)
    return StageStatus(COMPLETE, f"{len(written)} 章正文已写完", len(written), total)


def assess_workflow(output_dir: str) -> Dict[str, StageStatus]:
    """Assess every stage; a stage that cannot be read is reported, not hidden."""
    expected = expected_chapter_count(output_dir)
    checks = {
        "lore": lambda: _assess_lore(output_dir),
        "structure": lambda: _assess_structure(output_dir),
        "scenes": lambda: _assess_scenes(output_dir, expected),
        "chapters": lambda: _assess_chapters(output_dir, expected),
    }
    result: Dict[str, StageStatus] = {}
    for name, check in checks.items():
        try:
            result[name] = check()
        except (OSError, ValueError, TypeError) as exc:
            result[name] = StageStatus(BLOCKED, f"无法读取该阶段产物：{exc}")
    return result


def assess_stage(output_dir: str, step_name: str) -> Optional[StageStatus]:
    return assess_workflow(output_dir).get(step_name)
