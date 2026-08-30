"""Real, resumable ten-chapter end-to-end generation and quality audit.

This runner deliberately uses the configured real backend.  It creates an
isolated story workspace, generates stable design artifacts, then plans and
accepts one chapter at a time so NarrativeGraph readiness is based on the
latest accepted StoryLedger rather than on speculative future prose.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Dict, Iterable, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents.review.domain_review_agent import extract_json_object
from agents.writing.chapter_writing_agent import ChapterWritingAgent
from core.generation.ai_helper import send_prompt, set_backend
from core.generation.helper_fns import parse_scene_sections
from core.generation.llm_trace import trace_session, trace_stage
from core.generation.narrative_graph import NarrativeGraphManager
from core.generation.narrative_quality import (
    analyze_narrative_quality,
    summarize_narrative_reports,
)
from core.generation.planning_contract import validate_planning_contract
from core.generation.prompt_context import analyze_chinese_prose_style
from core.generation.scene_pipeline import ScenePipeline
from core.generation.stage_pipeline import (
    GenerationHost,
    load_stage_parameters,
    run_stage,
)
from core.generation.stage_context import make_context
from core.generation.story_ledger import StoryLedgerManager, _atomic_write_json


RUN_ID = "real_e2e_10_chapters_v1"
MODEL = "hosted-llm"
BACKEND = "api"
CHAPTER_COUNT = 10
SECTION_CHAPTERS = {
    "Act 1: Setup": (1, 2, 3),
    "Act 2: Confrontation": (4, 5, 6, 7),
    "Act 3: Resolution": (8, 9, 10),
}
PARAMETERS = {
    "Output Directory": "",
    "Genre": "Mystery",
    "Subgenre": "Forensic Mystery",
    "Story Length": "Novella",
    "Story Structure": "3-Act Structure",
    "Novel Title": "第七码",
    "Author Name": "端到端质量测试",
    "Theme": "当可量化的证据与人的记忆互相冲突时，真相是否仍能免于权力塑形。",
    "Tone": "冷峻克制、法医细节可信、悬念递进、人物选择带有道德代价。",
    "Gender Generation Bias String": "Balanced (50F/50M)",
    "Quality Loop": "strict",
    "Backend": BACKEND,
    "Model": MODEL,
    "Female Percentage": "50",
    "Male Percentage": "50",
    "Num Factions": "3",
    "Num Characters": "4",
}


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def run_dir(root: Path) -> Path:
    return root / "system" / "quality_runs" / RUN_ID


def report_path(root: Path) -> Path:
    return run_dir(root) / "stage_report.json"


def load_report(root: Path) -> Dict[str, Any]:
    path = report_path(root)
    if not path.exists():
        return {
            "run_id": RUN_ID,
            "mode": "real",
            "model": MODEL,
            "started_at": now(),
            "phases": {},
            "chapters": {},
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {
            "run_id": RUN_ID,
            "mode": "real",
            "model": MODEL,
            "started_at": now(),
            "phases": {},
            "chapters": {},
        }


def save_report(root: Path, report: Dict[str, Any]) -> None:
    report["updated_at"] = now()
    run_dir(root).mkdir(parents=True, exist_ok=True)
    _atomic_write_json(str(report_path(root)), report)


def record_phase(
    root: Path,
    name: str,
    *,
    status: str,
    quality: Optional[Dict[str, Any]] = None,
    artifacts: Iterable[str] = (),
    error: str = "",
) -> None:
    report = load_report(root)
    report.setdefault("phases", {})[name] = {
        "status": status,
        "quality": quality or {},
        "artifacts": list(artifacts),
        "error": error,
        "finished_at": now(),
    }
    save_report(root, report)


def setup(root: Path) -> None:
    system = root / "system"
    system.mkdir(parents=True, exist_ok=True)
    parameters = dict(PARAMETERS)
    parameters["Output Directory"] = str(root)
    text = "\n".join(f"{key}: {value}" for key, value in parameters.items()) + "\n"
    (system / "parameters.txt").write_text(text, encoding="utf-8")
    run_dir(root).mkdir(parents=True, exist_ok=True)
    quality = {
        "parameter_count": len(parameters),
        "chapter_target": CHAPTER_COUNT,
        "backend": BACKEND,
        "model": MODEL,
        "isolated_output": True,
    }
    record_phase(
        root,
        "setup",
        status="passed",
        quality=quality,
        artifacts=("system/parameters.txt",),
    )


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def _nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def run_lore(root: Path) -> None:
    setup(root)
    required = (
        root / "story" / "lore" / "factions.json",
        root / "story" / "lore" / "characters.json",
        root / "story" / "lore" / "generated_lore.md",
        root / "story" / "lore" / "lore_contract.json",
    )
    if not all(_nonempty(path) for path in required):
        with trace_stage("lore_full"):
            result = run_stage(
                "lore",
                str(root),
                MODEL,
                {
                    "backend": BACKEND,
                    "model": MODEL,
                    "num_factions": 3,
                    "num_characters": 4,
                    "num_chars": 4,
                    "female_percentage": 50,
                    "male_percentage": 50,
                },
                report=lambda label, fraction: logging.info(
                    "lore %.0f%% %s", fraction * 100, label
                ),
            )
    else:
        result = {"generated_files": [str(path.relative_to(root)) for path in required]}
    factions = _load_json(required[0], [])
    characters = _load_json(required[1], [])
    lore = required[2].read_text(encoding="utf-8")
    faction_records = factions.get("factions", []) if isinstance(factions, dict) else factions
    character_records = (
        characters.get("characters", []) if isinstance(characters, dict) else characters
    )
    quality = {
        "faction_count": len(faction_records) if isinstance(faction_records, list) else 0,
        "character_count": len(character_records) if isinstance(character_records, list) else 0,
        "lore_char_count": len(re.sub(r"\s+", "", lore)),
        "lore_contract_valid": bool(_load_json(required[3], {})),
        "all_required_artifacts_nonempty": all(_nonempty(path) for path in required),
    }
    status = "passed" if all(
        (
            quality["faction_count"] >= 2,
            quality["character_count"] >= 3,
            quality["lore_char_count"] >= 1000,
            quality["lore_contract_valid"],
            quality["all_required_artifacts_nonempty"],
        )
    ) else "failed"
    record_phase(
        root,
        "lore",
        status=status,
        quality=quality,
        artifacts=result.get("generated_files", ()),
    )
    if status != "passed":
        raise RuntimeError(f"lore quality checks failed: {quality}")


def run_structure(root: Path) -> None:
    run_lore(root)
    contract_path = root / "story" / "structure" / "structure_contract.json"
    if not _nonempty(contract_path):
        with trace_stage("structure_full"):
            result = run_stage(
                "structure",
                str(root),
                MODEL,
                {"backend": BACKEND, "model": MODEL},
                report=lambda label, fraction: logging.info(
                    "structure %.0f%% %s", fraction * 100, label
                ),
            )
    else:
        result = {
            "generated_files": [
                str(path.relative_to(root))
                for path in (root / "story" / "structure").glob("*")
                if path.is_file()
            ]
        }
    contract = _load_json(contract_path, {})
    sections = contract.get("sections", []) if isinstance(contract, dict) else []
    section_paths = [
        root / "story" / "structure" / "3-act_structure_act_1_setup.md",
        root / "story" / "structure" / "3-act_structure_act_2_confrontation.md",
        root / "story" / "structure" / "3-act_structure_act_3_resolution.md",
    ]
    quality = {
        "structure_contract_sections": len(sections),
        "section_files_nonempty": all(_nonempty(path) for path in section_paths),
        "section_char_counts": {
            path.name: len(re.sub(r"\s+", "", path.read_text(encoding="utf-8")))
            if path.exists()
            else 0
            for path in section_paths
        },
        "character_arcs_nonempty": _nonempty(
            root / "story" / "structure" / "character_arcs.md"
        ),
        "reconciled_locations_nonempty": _nonempty(
            root / "story" / "planning" / "reconciled_locations_arcs.md"
        ),
    }
    status = "passed" if (
        quality["structure_contract_sections"] == 3
        and quality["section_files_nonempty"]
        and quality["character_arcs_nonempty"]
        and quality["reconciled_locations_nonempty"]
    ) else "failed"
    record_phase(
        root,
        "structure",
        status=status,
        quality=quality,
        artifacts=result.get("generated_files", ()),
    )
    if status != "passed":
        raise RuntimeError(f"structure quality checks failed: {quality}")


def ask_json(
    root: Path,
    stage: str,
    prompt: str,
    validate: Callable[[Dict[str, Any]], List[str]],
    attempts: int = 3,
) -> Dict[str, Any]:
    request = prompt
    problems: List[str] = []
    for attempt in range(1, attempts + 1):
        with trace_stage(f"{stage}_attempt_{attempt}"):
            response = send_prompt(request, model=MODEL)
        try:
            payload = extract_json_object(response)
            problems = validate(payload)
        except Exception as exc:  # structured retry retains the exact parser problem
            payload = {}
            problems = [f"JSON parsing failed: {exc}"]
        if not problems:
            return payload
        request = (
            prompt
            + "\n\n上一次结果未通过确定性校验。只修复下列问题并重新输出完整 JSON：\n- "
            + "\n- ".join(problems[:30])
        )
    raise RuntimeError(f"{stage} failed after {attempts} attempts: {'; '.join(problems)}")


def _design_sources(root: Path) -> str:
    parts: List[str] = []
    for path in sorted((root / "story" / "structure").glob("3-act_structure_*.md")):
        parts.append(f"## {path.stem}\n{path.read_text(encoding='utf-8')}")
    lore = root / "story" / "lore" / "generated_lore.md"
    if lore.exists():
        parts.append("## 世界观\n" + lore.read_text(encoding="utf-8")[:14000])
    return "\n\n".join(parts)


def _outline_problems(payload: Dict[str, Any]) -> List[str]:
    chapters = payload.get("chapters")
    if not isinstance(chapters, list):
        return ["chapters 必须是数组"]
    problems: List[str] = []
    numbers = []
    for index, chapter in enumerate(chapters):
        if not isinstance(chapter, dict):
            problems.append(f"chapters[{index}] 必须是对象")
            continue
        try:
            numbers.append(int(chapter.get("chapter")))
        except (TypeError, ValueError):
            problems.append(f"chapters[{index}].chapter 无效")
        for field in ("title", "act", "purpose", "ending_hook"):
            if not str(chapter.get(field, "")).strip():
                problems.append(f"chapters[{index}].{field} 为空")
        for field in ("events", "characters", "locations", "thread_goals"):
            if not isinstance(chapter.get(field), list) or not chapter.get(field):
                problems.append(f"chapters[{index}].{field} 必须是非空数组")
    if numbers != list(range(1, CHAPTER_COUNT + 1)):
        problems.append(f"章号必须严格为 1..10，实际为 {numbers}")
    for section, expected in SECTION_CHAPTERS.items():
        actual = tuple(
            int(item.get("chapter"))
            for item in chapters
            if isinstance(item, dict) and item.get("act") == section
        )
        if actual != expected:
            problems.append(f"{section} 必须包含 {expected}，实际为 {actual}")
    return problems


def generate_outline(root: Path) -> Dict[str, Any]:
    path = run_dir(root) / "outline_design.json"
    existing = _load_json(path, {})
    if existing and not _outline_problems(existing):
        return existing
    prompt = f"""你是中文法医悬疑中篇小说的总策划。根据下方已通过契约校验的世界观与三幕结构，设计恰好十章的逐章大纲。

硬约束：
- 全书只能有第 1 到第 10 章，不得多章、少章、跳号或合并章。
- 第一幕 1-3 章，第二幕 4-7 章，第三幕 8-10 章。
- 每章都有不可替换的调查动作、人物选择、信息增量和结尾钩子。
- 线索必须先展示、后推理、再揭示；第 4、7、10 章分别形成递进揭示。
- 法医、监控、时间线、证据保全和程序行为必须现实可信。
- 最终答案在第 10 章落地，同时保留人物承担的道德代价，不能靠新人物或新证据突袭解谜。

只输出一个 JSON 对象：
{{"chapters":[{{"chapter":1,"title":"中文章名","act":"Act 1: Setup","purpose":"本章独有目标","events":["事件"],"characters":["人物"],"locations":["地点"],"thread_goals":["要打开、推进或关闭的情节线"],"evidence_state":"本章证据状态变化","reader_knowledge":"读者章末知道什么","ending_hook":"具体钩子"}}]}}

设计材料：
{_design_sources(root)[:42000]}
"""
    payload = ask_json(root, "exact_10_chapter_outline", prompt, _outline_problems)
    _atomic_write_json(str(path), payload)
    outline_dir = root / "story" / "planning" / "chapter_outlines"
    outline_dir.mkdir(parents=True, exist_ok=True)
    for section, numbers in SECTION_CHAPTERS.items():
        safe = section.lower().replace(" ", "_").replace(":", "").replace("/", "_")
        output = outline_dir / f"chapter_outlines_3-act_structure_{safe}.md"
        lines = [f"# {section}", ""]
        for chapter in payload["chapters"]:
            if int(chapter["chapter"]) not in numbers:
                continue
            lines.extend(
                [
                    f"### 第 {chapter['chapter']} 章：{chapter['title']}",
                    f"- 本章目的：{chapter['purpose']}",
                    "- 关键事件：" + "；".join(chapter["events"]),
                    "- 出场人物：" + "、".join(chapter["characters"]),
                    "- 具体地点：" + "、".join(chapter["locations"]),
                    "- 情节线目标：" + "；".join(chapter["thread_goals"]),
                    f"- 证据状态：{chapter.get('evidence_state', '')}",
                    f"- 读者知识：{chapter.get('reader_knowledge', '')}",
                    f"- 结尾钩子：{chapter['ending_hook']}",
                    "",
                ]
            )
        output.write_text("\n".join(lines), encoding="utf-8")
    quality = {
        "chapter_count": len(payload["chapters"]),
        "chapter_numbers": [item["chapter"] for item in payload["chapters"]],
        "act_distribution": {key: list(value) for key, value in SECTION_CHAPTERS.items()},
        "validation_errors": _outline_problems(payload),
    }
    record_phase(
        root,
        "chapter_outline",
        status="passed",
        quality=quality,
        artifacts=[
            str(path.relative_to(root)),
            *[
                str(item.relative_to(root))
                for item in sorted(outline_dir.glob("chapter_outlines_*.md"))
            ],
        ],
    )
    return payload


def _graph_operations(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    operations = [
        {"op": "add_node", "node": node}
        for node in payload.get("nodes", [])
        if isinstance(node, dict)
    ]
    operations.extend(
        {"op": "add_edge", "edge": edge}
        for edge in payload.get("edges", [])
        if isinstance(edge, dict)
    )
    return operations


def generate_graph(root: Path, outline: Dict[str, Any]) -> NarrativeGraphManager:
    ledger_manager = StoryLedgerManager(str(root))
    ledger_manager.initialize(load_stage_parameters(str(root)))
    manager = NarrativeGraphManager(str(root))
    if manager.current_revision() > 0:
        return manager

    def validate(payload: Dict[str, Any]) -> List[str]:
        nodes = payload.get("nodes")
        edges = payload.get("edges")
        problems: List[str] = []
        if not isinstance(nodes, list) or not isinstance(edges, list):
            return ["nodes 和 edges 必须是数组"]
        counts = {
            kind: sum(isinstance(node, dict) and node.get("type") == kind for node in nodes)
            for kind in ("thread", "clue", "fact", "reveal")
        }
        if counts["thread"] < 2:
            problems.append("至少需要 2 个 thread 节点")
        if counts["clue"] < 7:
            problems.append("至少需要 7 个 clue 节点")
        if counts["fact"] < 3:
            problems.append("至少需要 3 个 fact 节点")
        if counts["reveal"] != 3:
            problems.append("必须恰好有 3 个 reveal 节点")
        reveals = sorted(
            int(node.get("planned_reveal_chapter", 0) or 0)
            for node in nodes
            if isinstance(node, dict) and node.get("type") == "reveal"
        )
        if reveals != [4, 7, 10]:
            problems.append(f"reveal 计划章必须为 [4,7,10]，实际为 {reveals}")
        issues = manager.validate_change(
            _graph_operations(payload), ledger_manager.load_suspense_ledger()
        )
        problems.extend(
            f"{issue.get('code')}: {issue.get('message')}"
            for issue in issues
            if issue.get("severity") == "error"
        )
        return problems

    prompt = f"""你是叙事工程师。根据十章大纲建立第一版 NarrativeGraph，只输出 JSON，不要输出解释。

硬约束：
- node.type 只能是 thread、clue、fact、reveal。
- edge.type 只能是 belongs_to、supports、requires、advances。
- 至少 2 条 thread、7 个 clue、3 个 fact，恰好 3 个 reveal。
- 三个 reveal 的 planned_reveal_chapter 必须分别是 4、7、10。
- clue 必须有 planned_introduce_chapter，分布在揭示章之前，并通过 belongs_to 属于 thread。
- 每个 reveal 至少 requires 两个此前计划展示的 clue 或 fact；requires 不能成环。
- 每个 reveal 都通过 advances 推进 thread。
- 所有边都使用“前置/动作节点 -> 后置/结果节点”的方向：
  - belongs_to: clue -> thread；
  - supports: clue -> fact；
  - requires: clue 或 fact -> reveal（绝不能写成 reveal -> clue/fact）；
  - advances: reveal -> thread。
- 图中只放稳定定义和计划，不得出现 status、reader_status、reveal_status、thread_status、actual_chapter 等动态字段。
- 重要 thread 设置 importance="high"、max_silent_chapters=3、planned_resolve_chapter=10。
- 线索写清 surface_meaning 和 true_meaning，所有 id 和边 id 唯一且使用 ASCII。

输出：
{{"nodes":[{{"id":"PT-MAIN","type":"thread","label":"主案","importance":"high","max_silent_chapters":3,"planned_resolve_chapter":10}},{{"id":"C-01","type":"clue","label":"线索","surface_meaning":"表层","true_meaning":"真实","planned_introduce_chapter":1}},{{"id":"F-01","type":"fact","label":"事实"}},{{"id":"R-01","type":"reveal","label":"揭示","planned_reveal_chapter":4}}],"edges":[{{"id":"E-01","type":"belongs_to","source_id":"C-01","target_id":"PT-MAIN"}}]}}

十章大纲：
{json.dumps(outline, ensure_ascii=False, indent=2)}
"""
    payload = ask_json(root, "narrative_graph_design", prompt, validate)
    revision = manager.apply_change(
        _graph_operations(payload),
        reason="real ten-chapter end-to-end story design",
        expected_revision=0,
        actor="planner",
    )
    ledger = StoryLedgerManager(str(root)).load_suspense_ledger()
    issues = manager.validate(ledger)
    quality = {
        "revision": revision,
        "node_count": len(manager.load()["nodes"]),
        "edge_count": len(manager.load()["edges"]),
        "errors": [item for item in issues if item.get("severity") == "error"],
        "warnings": [item for item in issues if item.get("severity") == "warning"],
    }
    record_phase(
        root,
        "narrative_graph",
        status="passed" if not quality["errors"] else "failed",
        quality=quality,
        artifacts=(
            "system/story_ledgers/narrative_graph.json",
            "system/story_ledgers/narrative_graph_changes.jsonl",
        ),
    )
    if quality["errors"]:
        raise RuntimeError(f"narrative graph validation failed: {quality['errors']}")
    return manager


def run_design(root: Path) -> None:
    run_structure(root)
    outline = generate_outline(root)
    graph = generate_graph(root, outline)
    graph_data = graph.load()
    record_phase(
        root,
        "design",
        status="passed",
        quality={
            "outline_chapter_count": len(outline.get("chapters", [])),
            "graph_revision": graph_data.get("revision", 0),
            "graph_node_count": len(graph_data.get("nodes", [])),
            "graph_edge_count": len(graph_data.get("edges", [])),
        },
        artifacts=(
            "story/planning/chapter_outlines/e2e_10_chapter_outline.json",
            "system/story_ledgers/narrative_graph.json",
        ),
    )


def _chapter_design(outline: Dict[str, Any], chapter: int) -> Dict[str, Any]:
    return next(item for item in outline["chapters"] if int(item["chapter"]) == chapter)


def _section_for(chapter: int) -> str:
    return next(section for section, numbers in SECTION_CHAPTERS.items() if chapter in numbers)


def _scene_plan_path(root: Path, chapter: int) -> Path:
    section = _section_for(chapter)
    safe = section.lower().replace(" ", "_").replace(":", "").replace("/", "_")
    return (
        root
        / "story"
        / "planning"
        / "detailed_scene_plans"
        / f"scenes_3-act_structure_{safe}_ch{chapter}.md"
    )


def plan_chapter(
    root: Path,
    pipeline: ScenePipeline,
    outline: Dict[str, Any],
    graph: NarrativeGraphManager,
    chapter: int,
) -> Dict[str, Any]:
    path = _scene_plan_path(root, chapter)
    ledger_manager = StoryLedgerManager(str(root))
    if path.exists():
        markdown = path.read_text(encoding="utf-8")
        contract = ledger_manager.load_contract(chapter, markdown)
        if contract and not contract.get("stale") and parse_scene_sections(markdown):
            return contract
    parameters = load_stage_parameters(str(root))
    ledger = ledger_manager.load_suspense_ledger()
    context = graph.planning_context(chapter, ledger)
    design = _chapter_design(outline, chapter)
    graph_data = graph.load()
    due_clues = [
        node
        for node in graph_data["nodes"]
        if node.get("type") == "clue"
        and int(node.get("planned_introduce_chapter", 0) or 0) == chapter
    ]
    due_reveals = [
        node
        for node in context["ready_reveals"]
        if int(node.get("planned_reveal_chapter", 0) or 0) <= chapter
    ]
    active_threads = [item.get("id") for item in context["active_threads"]]
    prompt = f"""请为中文法医悬疑小说《第七码》第 {chapter} 章生成两场戏的详细场景规划，并在末尾输出机器可读 ChapterContract。

本章总纲：
{json.dumps(design, ensure_ascii=False, indent=2)}

本章叙事执行提示：
- 计划本章引入的 clue：{json.dumps(due_clues, ensure_ascii=False)}
- 当前已经 ready 且到期的 reveal：{json.dumps(due_reveals, ensure_ascii=False)}
- 当前 active thread：{json.dumps(active_threads, ensure_ascii=False)}
- 若没有 active thread，从 available_threads 选择一条并 action=open；已有则优先 advance/touch。
- 引入 clue 时使用 introduce_to_reader；让 fact 可推断前，必须先展示支持它的 clue。
- 只有 ready_reveals 可以 execute，并同时列入 allowed_reveals。
- narrative_transitions 严格按正文发生顺序排列。
- 第 10 章在完成所有所需 reveal 后关闭主要 thread；此前不要提前关闭。
- plot_thread_updates 中 primary_thread/primary_action 必须有同 id/action 的记录；advance 必须提供 via_node_ids。
- 全章恰好两个场景，每场包含具体地点、人物目标、可见行动、证据处理、信息变化、场尾转折。
- 不写正文，不增加图中没有的重要线索、事实或揭示。

世界观摘要：
{(root / 'story' / 'lore' / 'generated_lore.md').read_text(encoding='utf-8')[:14000]}

完整规划上下文：
{json.dumps(context, ensure_ascii=False, indent=2)}

输出 Markdown，场景标题必须为“### 场景 1：标题”和“### 场景 2：标题”。

{pipeline._contract_instructions(str(root), chapter, parameters)}
"""
    response, markdown, contract = pipeline._generate_valid_scene_response(
        prompt,
        MODEL,
        chapter,
        (root / "story" / "lore" / "generated_lore.md").read_text(encoding="utf-8"),
        parameters,
        output_dir=str(root),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pipeline._save_scene_plan_and_contract(
        str(root),
        str(path),
        response,
        chapter,
        scene_markdown=markdown,
        contract=contract,
    )
    return contract


def _latest_review(root: Path, chapter: int, prefix: str) -> Dict[str, Any]:
    directory = root / "quality" / "legal_suspense_reviews" / f"chapter_{chapter}"
    candidates = sorted(directory.glob(f"{prefix}_*.json")) if directory.exists() else []
    return _load_json(candidates[-1], {}) if candidates else {}


def _chapter_quality(root: Path, chapter: int) -> Dict[str, Any]:
    path = root / "story" / "content" / "chapters" / f"chapter_{chapter}.md"
    text = path.read_text(encoding="utf-8")
    narrative = analyze_narrative_quality(text).to_dict()
    style = analyze_chinese_prose_style(text)
    artifact = _latest_review(root, chapter, "acceptance_artifact")
    canon = _latest_review(root, chapter, "acceptance_canon")
    chapter_review = _latest_review(root, chapter, "chapter")
    return {
        "path": str(path.relative_to(root)),
        "char_count": narrative["char_count"],
        "scene_separator_count": text.count("\n\n---\n\n"),
        "style_warnings": style,
        "narrative_signals": narrative,
        "artifact_acceptance_passed": artifact.get("passed"),
        "canon_acceptance_passed": canon.get("passed"),
        "domain_review_passed": chapter_review.get("passed"),
        "domain_average_score": chapter_review.get("average_score"),
        "domain_waived": chapter_review.get("waived", False),
    }


def run_chapters(root: Path) -> None:
    run_design(root)
    parameters = load_stage_parameters(str(root))
    context = make_context(str(root), MODEL, parameters)
    host = GenerationHost(context, logging.getLogger("e2e.scene_planning"))
    pipeline = ScenePipeline(host)
    outline = _load_json(run_dir(root) / "outline_design.json", {})
    graph = NarrativeGraphManager(str(root))
    ledger_manager = StoryLedgerManager(str(root))

    for chapter in range(1, CHAPTER_COUNT + 1):
        accepted = {
            int(item.get("chapter"))
            for item in ledger_manager.load_suspense_ledger().get("accepted_chapters", [])
            if isinstance(item, dict) and item.get("chapter")
        }
        if chapter in accepted:
            quality = _chapter_quality(root, chapter)
            report = load_report(root)
            report.setdefault("chapters", {})[str(chapter)] = {
                "status": "passed",
                "resumed": True,
                "quality": quality,
            }
            save_report(root, report)
            continue

        logging.info("planning chapter %s", chapter)
        with trace_stage(f"chapter_{chapter}_scene_planning"):
            contract = plan_chapter(root, pipeline, outline, graph, chapter)
        markdown = _scene_plan_path(root, chapter).read_text(encoding="utf-8")
        contract = validate_planning_contract(contract, chapter, require_origin=True)
        graph_issues = graph.validate_contract(
            contract, ledger_manager.load_suspense_ledger()
        )
        if any(item.get("severity") == "error" for item in graph_issues):
            raise RuntimeError(f"chapter {chapter} contract graph errors: {graph_issues}")

        logging.info("writing and accepting chapter %s", chapter)
        agent = ChapterWritingAgent(
            str(root),
            app_instance=None,
            use_new_structure=True,
            model=MODEL,
        )
        infos, _ = agent.analyze_chapter_structure()
        info = next((item for item in infos if item.chapter_number == chapter), None)
        if info is None:
            raise RuntimeError(f"chapter {chapter} is missing from analyzed structure")
        with trace_stage(f"chapter_{chapter}_write_review_accept"):
            result = agent._write_single_chapter(info)
        if not result.success:
            report = load_report(root)
            report.setdefault("chapters", {})[str(chapter)] = {
                "status": "failed",
                "messages": result.messages,
                "data": result.data,
                "finished_at": now(),
            }
            save_report(root, report)
            raise RuntimeError(
                f"chapter {chapter} failed: {'; '.join(result.messages or [])}"
            )
        quality = _chapter_quality(root, chapter)
        report = load_report(root)
        report.setdefault("chapters", {})[str(chapter)] = {
            "status": "passed",
            "contract": {
                "graph_revision": contract.get("narrative_graph_revision"),
                "primary_thread": contract.get("primary_thread"),
                "primary_action": contract.get("primary_action"),
                "transitions": contract.get("narrative_transitions", []),
            },
            "generation_result": result.data,
            "quality": quality,
            "finished_at": now(),
        }
        save_report(root, report)

    ledger = ledger_manager.load_suspense_ledger()
    accepted = sorted(
        int(item.get("chapter"))
        for item in ledger.get("accepted_chapters", [])
        if isinstance(item, dict) and item.get("chapter")
    )
    record_phase(
        root,
        "chapters",
        status="passed" if accepted == list(range(1, 11)) else "failed",
        quality={
            "accepted_chapters": accepted,
            "ledger_revision": ledger.get("revision"),
            "chapter_commit_count": len(ledger.get("chapter_commits", [])),
            "pending_narrative_suggestions": ledger.get(
                "pending_narrative_suggestions", []
            ),
        },
        artifacts=[
            f"story/content/chapters/chapter_{chapter}.md"
            for chapter in accepted
        ],
    )


def _holistic_problems(payload: Dict[str, Any]) -> List[str]:
    problems: List[str] = []
    scores = payload.get("scores")
    required = (
        "plot_coherence",
        "character_continuity",
        "fair_play",
        "pacing",
        "prose_style",
        "emotional_impact",
        "ending_payoff",
    )
    if not isinstance(scores, dict):
        return ["scores 必须是对象"]
    for field in required:
        try:
            value = float(scores.get(field))
        except (TypeError, ValueError):
            problems.append(f"scores.{field} 必须是数字")
            continue
        if not 0 <= value <= 10:
            problems.append(f"scores.{field} 必须在 0..10")
    if not isinstance(payload.get("strengths"), list):
        problems.append("strengths 必须是数组")
    if not isinstance(payload.get("issues"), list):
        problems.append("issues 必须是数组")
    if not str(payload.get("verdict", "")).strip():
        problems.append("verdict 不能为空")
    return problems


def audit(root: Path) -> Dict[str, Any]:
    run_chapters(root)
    chapter_paths = [
        root / "story" / "content" / "chapters" / f"chapter_{number}.md"
        for number in range(1, 11)
    ]
    texts = [path.read_text(encoding="utf-8") for path in chapter_paths]
    corpus = summarize_narrative_reports(texts)
    style = {
        str(number): analyze_chinese_prose_style(text)
        for number, text in enumerate(texts, 1)
    }
    ledger_manager = StoryLedgerManager(str(root))
    ledger = ledger_manager.load_suspense_ledger()
    graph_manager = NarrativeGraphManager(str(root))
    graph_issues = graph_manager.validate(ledger)
    terminal_state_issues = graph_manager.validate_terminal_state(CHAPTER_COUNT, ledger)
    contract_checks = {}
    for number in range(1, 11):
        contract = ledger_manager.load_contract(number)
        contract_checks[str(number)] = {
            "exists": contract is not None,
            "stale": (contract or {}).get("stale"),
            "graph_errors": [
                item
                for item in graph_manager.validate_contract(contract or {}, ledger)
                if item.get("severity") == "error"
            ],
        }

    excerpts = []
    for number, text in enumerate(texts, 1):
        excerpts.append(
            {
                "chapter": number,
                "opening": text[:1400],
                "ending": text[-1400:],
                "char_count": len(re.sub(r"\s+", "", text)),
                "signals": analyze_narrative_quality(text).to_dict(),
            }
        )
    holistic_prompt = f"""你是独立中文类型小说终审。请依据十章首尾原文、全量确定性统计、章节验收结果和故事账本，对《第七码》做全书质量审计。

规则：
- 只依据提供的真实文本和数据，不替作者补设定。
- 每个问题必须指出章号并附提供材料中的短引文；没有证据不要报告。
- 重点检查跨章因果、人物动机、线索公平性、程序可信度、节奏重复、语言模板感和结尾兑现。
- 分数为 0..10；7 表示可顺畅读完，8 表示成熟可用，9 以上需有明确不可替代性。

只输出 JSON：
{{"scores":{{"plot_coherence":0,"character_continuity":0,"fair_play":0,"pacing":0,"prose_style":0,"emotional_impact":0,"ending_payoff":0}},"strengths":[{{"chapter":1,"quote":"短引文","assessment":"成立原因"}}],"issues":[{{"severity":"major|minor","chapter":1,"quote":"短引文","problem":"具体问题","minimum_fix":"最小修改"}}],"verdict":"总体结论","publication_readiness":"ready|revise|not_ready"}}

章节材料：
{json.dumps(excerpts, ensure_ascii=False)}

Ledger 摘要：
{json.dumps({"revision": ledger.get("revision"), "states": ledger.get("narrative_node_states", {}), "open_threads": [item for item in ledger.get("plot_threads", []) if item.get("status") != "closed"]}, ensure_ascii=False)}
"""
    holistic_path = run_dir(root) / "holistic_review.json"
    holistic = _load_json(holistic_path, {})
    if not holistic or _holistic_problems(holistic):
        holistic = ask_json(
            root, "holistic_ten_chapter_review", holistic_prompt, _holistic_problems
        )
        _atomic_write_json(str(holistic_path), holistic)

    complete_path = root / "story" / "content" / "novel_complete.md"
    complete_path.write_text(
        "\n\n".join(
            f"# 第 {number} 章\n\n{text}"
            for number, text in enumerate(texts, 1)
        ),
        encoding="utf-8",
    )
    scores = [float(value) for value in holistic["scores"].values()]
    final = {
        "status": "passed" if (
            len(texts) == 10
            and sorted(
                int(item.get("chapter"))
                for item in ledger.get("accepted_chapters", [])
                if isinstance(item, dict) and item.get("chapter")
            )
            == list(range(1, 11))
            and not any(item.get("severity") == "error" for item in graph_issues)
            and not any(
                item.get("severity") == "error" for item in terminal_state_issues
            )
            and all(not item["graph_errors"] and not item["stale"] for item in contract_checks.values())
        ) else "failed",
        "chapter_count": len(texts),
        "total_non_whitespace_chars": sum(
            len(re.sub(r"\s+", "", text)) for text in texts
        ),
        "chapter_char_counts": {
            str(number): len(re.sub(r"\s+", "", text))
            for number, text in enumerate(texts, 1)
        },
        "narrative_corpus": corpus,
        "style_warnings": style,
        "graph_issues": graph_issues,
        "terminal_state_issues": terminal_state_issues,
        "contract_checks": contract_checks,
        "holistic_review": holistic,
        "holistic_average_score": round(mean(scores), 2),
        "artifacts": [
            str(complete_path.relative_to(root)),
            str(holistic_path.relative_to(root)),
        ],
    }
    _atomic_write_json(str(run_dir(root) / "final_quality_report.json"), final)
    record_phase(
        root,
        "final_audit",
        status=final["status"],
        quality=final,
        artifacts=final["artifacts"],
    )
    report = load_report(root)
    report["status"] = final["status"]
    report["finished_at"] = now()
    save_report(root, report)
    return final


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--phase",
        choices=("setup", "lore", "structure", "design", "chapters", "audit", "all"),
        default="all",
    )
    args = parser.parse_args(argv)
    root = Path(args.output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    run_dir(root).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(run_dir(root) / "e2e.log", encoding="utf-8"),
        ],
        force=True,
    )
    set_backend(BACKEND, MODEL)
    actions = {
        "setup": lambda: setup(root),
        "lore": lambda: run_lore(root),
        "structure": lambda: run_structure(root),
        "design": lambda: run_design(root),
        "chapters": lambda: run_chapters(root),
        "audit": lambda: audit(root),
        "all": lambda: audit(root),
    }
    try:
        with trace_session(root, run_id=RUN_ID, mode="real"):
            result = actions[args.phase]()
        if isinstance(result, dict):
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        logging.exception("E2E phase %s failed", args.phase)
        record_phase(root, args.phase, status="failed", error=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
