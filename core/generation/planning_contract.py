"""Machine-readable chapter obligations produced during scene planning."""

from __future__ import annotations

import json
import os
import re
from glob import glob
from typing import Any, Dict, Iterable, List, Tuple

from core.generation.helper_fns import parse_scene_sections
from core.generation.story_ledger import StoryLedgerManager, compact_json


CONTRACT_START = "<!-- CHAPTER_CONTRACT_START -->"
CONTRACT_END = "<!-- CHAPTER_CONTRACT_END -->"
CONTRACT_SCHEMA_VERSION = 2
STATE_LIST_FIELDS = (
    "facts_added",
    "facts_confirmed",
    "facts_contradicted",
    "timeline_events",
    "character_updates",
    "plot_thread_updates",
)


class PlanningContractError(ValueError):
    """A scene plan is missing a usable, trustworthy chapter contract."""

    def __init__(
        self,
        message: str,
        *,
        chapters: Iterable[int] = (),
        code: str = "planning_contract_invalid",
    ):
        super().__init__(message)
        self.chapters = tuple(
            sorted({int(chapter) for chapter in chapters if int(chapter) > 0})
        )
        self.code = code


def contract_output_instructions(
    chapter_number: int,
    existing_index: Dict[str, Any] | None = None,
    domain_fields: Dict[str, str] | None = None,
) -> str:
    """Instructions appended to the existing scene-planning request.

    This deliberately uses the same LLM response as the Markdown plan; it does
    not introduce another generation call.
    """
    index_text = compact_json(existing_index or {}, 5000)
    domain_lines = "\n".join(
        f'  "{name}": {schema_hint},'
        for name, schema_hint in (domain_fields or {}).items()
    )
    return f"""

完成场景规划后，在文档末尾追加以下机器可读契约。标记必须原样保留，标记之间只能放一个合法 JSON 对象，不要使用代码围栏。
{CONTRACT_START}
{{
  "chapter": {chapter_number},
  "core_question": "本章集中追问的问题",
  "reader_knows_before": [],
  "reader_knows_after": [],
  "reader_must_not_know_yet": [],
  "scene_boundaries": [],
  "personal_cost": "本章不可逆个人代价；没有则留空",
  "cost_character": "承担代价的人物；没有则留空",
  "irreversible_change": "本章结束后的不可逆变化",
{domain_lines}
  "facts_added": [{{"id":"F-{chapter_number:03d}-01","fact":"属性名","value":"取值","first_stated_at":"scene_1"}}],
  "facts_confirmed": [{{"id":"已有事实ID","fact":"属性名","value":"既有取值"}}],
  "facts_contradicted": [{{"id":"已有事实ID","reason":"推翻理由","new_value":"新取值"}}],
  "timeline_events": [{{"id":"TL-{chapter_number:03d}-01","event":"事件名称","time":"明确时刻","location_id":"地点"}}],
  "character_updates": [{{"id":"CU-{chapter_number:03d}-01","character":"人物规范名","attribute":"属性名","value":"取值","stable":true}}],
  "plot_thread_updates": [{{"id":"PT-{chapter_number:03d}-01","thread":"线索名称","status":"open","deadline_chapter":{chapter_number}}}]
}}
{CONTRACT_END}

契约只记录本章场景已经明确安排的内容，没有相应内容的数组留空。新开启线索必须给出不早于本章的 deadline_chapter；关闭线索必须沿用已有 id，并填 status 为 closed。同一章内提出并解决的线索只写一条 closed 记录，同时添加 "opened_in_chapter": true。既有规划索引如下，重复出现的事实、事件和线索必须沿用其中的 id：
{index_text}
""".strip()


def extract_scene_plan_contract(response: str, chapter_number: int) -> Tuple[str, Dict[str, Any]]:
    """Split one model response into clean Markdown and a validated contract."""
    if not response or CONTRACT_START not in response or CONTRACT_END not in response:
        raise PlanningContractError("场景规划缺少章节契约标记")
    pattern = re.compile(
        re.escape(CONTRACT_START) + r"\s*(.*?)\s*" + re.escape(CONTRACT_END),
        flags=re.DOTALL,
    )
    matches = list(pattern.finditer(response))
    if len(matches) != 1:
        raise PlanningContractError("场景规划必须且只能包含一个章节契约")
    raw = matches[0].group(1).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
    try:
        contract = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlanningContractError(f"章节契约不是合法 JSON：{exc.msg}") from exc
    markdown = (response[: matches[0].start()] + response[matches[0].end() :]).strip()
    if not parse_scene_sections(markdown):
        raise PlanningContractError("场景规划中没有可解析的场景标题")
    normalized = validate_planning_contract(contract, chapter_number)
    normalized["schema_version"] = CONTRACT_SCHEMA_VERSION
    normalized["origin"] = "scene_planning"
    return markdown, normalized


def validate_planning_contract(
    contract: Dict[str, Any],
    chapter_number: int,
    *,
    require_origin: bool = False,
) -> Dict[str, Any]:
    if not isinstance(contract, dict):
        raise PlanningContractError("章节契约必须是 JSON 对象")
    try:
        declared_chapter = int(contract.get("chapter"))
    except (TypeError, ValueError):
        raise PlanningContractError("章节契约缺少有效 chapter") from None
    if declared_chapter != int(chapter_number):
        raise PlanningContractError(
            f"章节契约章号为 {declared_chapter}，当前场景规划是第 {chapter_number} 章"
        )
    if require_origin and contract.get("origin") != "scene_planning":
        raise PlanningContractError("章节契约不是由场景规划阶段生成的")

    normalized = dict(contract)
    for field in STATE_LIST_FIELDS:
        value = normalized.setdefault(field, [])
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise PlanningContractError(f"章节契约字段 {field} 必须是对象数组")

    seen_ids = set()
    for field in ("facts_added", "facts_confirmed", "facts_contradicted", "timeline_events"):
        for record in normalized[field]:
            if not str(record.get("id", "")).strip():
                raise PlanningContractError(f"章节契约字段 {field} 中的记录缺少 id")
    for record in normalized["plot_thread_updates"]:
        thread_id = str(record.get("id", "")).strip()
        status = str(record.get("status", "")).strip().lower()
        if not thread_id:
            raise PlanningContractError("每条线索更新都必须提供 id")
        if thread_id in seen_ids:
            raise PlanningContractError(f"本章线索 id 重复：{thread_id}")
        seen_ids.add(thread_id)
        if status not in {"open", "closed"}:
            raise PlanningContractError(f"线索 {thread_id} 的 status 只能是 open 或 closed")
        record["status"] = status
        if status == "open":
            try:
                deadline = int(record.get("deadline_chapter"))
            except (TypeError, ValueError):
                raise PlanningContractError(f"新开启线索 {thread_id} 缺少有效截止章") from None
            if deadline < chapter_number:
                raise PlanningContractError(f"线索 {thread_id} 的截止章早于开启章")
            record["deadline_chapter"] = deadline
    return normalized


def load_planning_contracts(output_dir: str) -> List[Dict[str, Any]]:
    manager = StoryLedgerManager(output_dir)
    contracts: List[Dict[str, Any]] = []
    for path in glob(os.path.join(manager.contract_dir, "chapter_*.json")):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                contract = json.load(handle)
            chapter = int(contract.get("chapter"))
            contracts.append(validate_planning_contract(contract, chapter, require_origin=True))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise PlanningContractError(f"无法使用章节契约 {path}：{exc}") from exc
    return sorted(contracts, key=lambda item: int(item["chapter"]))


def build_existing_planning_index(output_dir: str, before_chapter: int) -> Dict[str, Any]:
    contracts = [
        item for item in load_planning_contracts(output_dir)
        if int(item["chapter"]) < int(before_chapter)
    ]
    return {
        "facts": [record for item in contracts for record in item.get("facts_added", [])],
        "timeline_events": [
            record for item in contracts for record in item.get("timeline_events", [])
        ],
        "plot_threads": [
            record for item in contracts for record in item.get("plot_thread_updates", [])
        ],
    }


def validate_contract_sequence(
    contracts: Iterable[Dict[str, Any]],
    *,
    total_chapters: int | None = None,
) -> None:
    """Validate cross-chapter thread obligations before prose generation."""
    ordered = sorted(contracts, key=lambda item: int(item.get("chapter", 0)))
    by_chapter = {int(item.get("chapter", 0)): item for item in ordered}
    if total_chapters is not None:
        missing = [number for number in range(1, total_chapters + 1) if number not in by_chapter]
        if missing:
            preview = "、".join(str(number) for number in missing[:10])
            raise PlanningContractError(
                f"缺少第 {preview} 章的场景规划契约",
                chapters=missing[:10],
                code="missing_chapter_contract",
            )

    opened: Dict[str, Tuple[int, int]] = {}
    closed: Dict[str, int] = {}
    known_facts: Dict[str, Dict[str, Any]] = {}
    for contract in ordered:
        chapter = int(contract["chapter"])
        validate_planning_contract(contract, chapter, require_origin=True)
        for record in contract.get("facts_added", []):
            fact_id = str(record["id"])
            previous = known_facts.get(fact_id)
            if previous and (
                previous.get("fact") != record.get("fact")
                or previous.get("value") != record.get("value")
            ):
                raise PlanningContractError(
                    f"事实 {fact_id} 在规划阶段出现互相冲突的定义",
                    chapters=(chapter,),
                    code="fact_definition_conflict",
                )
            known_facts[fact_id] = record
        for field in ("facts_confirmed", "facts_contradicted"):
            for record in contract.get(field, []):
                fact_id = str(record["id"])
                if fact_id not in known_facts:
                    raise PlanningContractError(
                        f"第 {chapter} 章的 {field} 引用了尚未引入的事实 {fact_id}",
                        chapters=(chapter,),
                        code="unknown_fact_reference",
                    )
        for record in contract.get("plot_thread_updates", []):
            thread_id = str(record["id"])
            if record["status"] == "open":
                if thread_id in opened:
                    raise PlanningContractError(
                        f"线索 {thread_id} 被重复开启",
                        chapters=(chapter,),
                        code="thread_reopened",
                    )
                opened[thread_id] = (chapter, int(record["deadline_chapter"]))
            else:
                if thread_id not in opened:
                    if record.get("opened_in_chapter") is True:
                        opened[thread_id] = (chapter, chapter)
                    else:
                        raise PlanningContractError(
                            f"第 {chapter} 章关闭了尚未开启的线索 {thread_id}",
                            chapters=(chapter,),
                            code="thread_closed_before_open",
                        )
                closed[thread_id] = chapter

    for thread_id, (opened_at, deadline) in opened.items():
        closed_at = closed.get(thread_id)
        if closed_at is not None and closed_at < opened_at:
            raise PlanningContractError(
                f"线索 {thread_id} 在开启前被关闭",
                chapters=(opened_at, closed_at),
                code="thread_closed_before_open",
            )
        if closed_at is None and (total_chapters is not None or deadline in by_chapter):
            raise PlanningContractError(
                f"线索 {thread_id} 在第 {opened_at} 章开启，但截止第 {deadline} 章仍无关闭计划",
                chapters=(deadline,),
                code="thread_missing_closure",
            )
        if closed_at is not None and closed_at > deadline:
            raise PlanningContractError(
                f"线索 {thread_id} 计划在第 {closed_at} 章关闭，晚于截止第 {deadline} 章",
                chapters=(deadline, closed_at),
                code="thread_closes_late",
            )
