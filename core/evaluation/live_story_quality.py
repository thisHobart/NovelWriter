"""Cost-bounded real-model run for the fixed three-chapter mystery fixture."""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List

from agents.review.domain_review_agent import DomainReviewAgent
from core.evaluation.fixed_story_quality import (
    CASE_BIBLE,
    CHARACTERS,
    FACTIONS,
    INITIAL_SCENES,
    LORE,
    PARAMETERS,
    PLANS,
    SEED,
    _contract,
)
from core.evaluation.live_chapter_probe import _review_rank
from core.evaluation.live_quality_probe import _reachable, _target
from core.evaluation.reader_blind_test import create_blind_test
from core.generation.ai_helper import send_prompt, set_backend
from core.generation.chapter_acceptance import ChapterAcceptanceService
from core.generation.chapter_generation_loop import ChapterGenerationLoop, waiver_reason
from core.generation.domain_profiles import get_domain_profile
from core.generation.helper_fns import parse_scene_sections
from core.generation.llm_trace import trace_session, trace_stage
from core.generation.narrative_quality import analyze_narrative_quality
from core.generation.prompt_context import analyze_chinese_prose_style
from core.generation.scene_prompt import build_scene_prompt
from core.generation.story_ledger import StoryLedgerManager


RUN_ID = "live_fixed_mystery_3ch_v1"
LIVE_MODEL = "hosted-llm"
MAX_MODEL_CALLS = 24
MAX_RESUME_CALLS = 10


class ModelCallBudgetExceeded(RuntimeError):
    pass


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _write_json(path: Path, value: Any) -> None:
    _write_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def _print_json(value: Any) -> None:
    encoding = str(getattr(sys.stdout, "encoding", "") or "utf-8").lower()
    print(json.dumps(value, ensure_ascii="utf" not in encoding, indent=2))


def _load_trace(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    calls = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            calls.append(value)
    return calls


def _review_bundle(
    reviewer: DomainReviewAgent,
    prose: str,
    contract: Dict[str, Any],
    case_bible: Dict[str, Any],
    ledger: Dict[str, Any],
    previous_tail: str,
    *,
    suffix: str = "",
    requested: Dict[str, List[str]] | None = None,
):
    requested = requested or {}
    with trace_stage(f"contract_compliance_review{suffix}"):
        contract_review = reviewer.review_chapter(
            prose,
            contract,
            case_bible,
            ledger,
            repairs_requested=requested.get("contract"),
        )
    with trace_stage(f"reader_blind_review{suffix}"):
        blind_review = reviewer.review_reader_blind(
            prose,
            previous_tail,
            repairs_requested=requested.get("reader_blind"),
        )
    with trace_stage(f"plausibility_review{suffix}"):
        plausibility_review = reviewer.review_plausibility(
            prose,
            case_bible,
            repairs_requested=requested.get("plausibility"),
        )
    merged = reviewer._merge_reviews(
        "chapter",
        {
            "contract": contract_review,
            "reader_blind": blind_review,
            "plausibility": plausibility_review,
        },
    )
    return contract_review, blind_review, plausibility_review, merged


def _review_dict(contract_review, blind_review, plausibility_review, merged):
    return {
        "contract": contract_review.to_dict(),
        "reader_blind": blind_review.to_dict(),
        "plausibility": plausibility_review.to_dict(),
        "merged": merged.to_dict(),
    }


def _stage_summary(calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    deterministic_errors = [
        f"{call.get('error_type')}: {call.get('error')}"
        for call in calls
        if not call.get("success", True)
    ]
    quality_issues = []
    if any("不符合 schema" in call.get("prompt", "") for call in calls):
        quality_issues.append("structured review required one bounded schema repair")
    oversized = [call.get("prompt_chars", 0) for call in calls if call.get("prompt_chars", 0) > 12000]
    if oversized:
        quality_issues.append(
            f"prompt context exceeded 12000 chars (max={max(oversized)})"
        )
    return {
        "passed": not deterministic_errors,
        "call_count": len(calls),
        "elapsed_ms": round(sum(float(call.get("elapsed_ms", 0)) for call in calls), 3),
        "input": [call["prompt"] for call in calls],
        "actual_prompts": [call["prompt"] for call in calls],
        "outputs": [call["response"] for call in calls],
        "parsed_output": "see chapters.*.reviews; raw response retained here",
        "usage": {
            "exact_available": all(
                call.get("usage", {}).get("total_tokens") is not None for call in calls
            ) if calls else False,
            "exact_total_tokens": sum(
                int(call.get("usage", {}).get("total_tokens") or 0) for call in calls
            ) if calls else None,
            "estimated_prompt_tokens": sum(
                int(call.get("usage", {}).get("estimated_prompt_tokens") or 0)
                for call in calls
            ),
            "estimated_completion_tokens": sum(
                int(call.get("usage", {}).get("estimated_completion_tokens") or 0)
                for call in calls
            ),
        },
        "cost": {
            "amount_usd": None,
            "reason": "configured text backend does not expose provider usage or pricing",
        },
        "deterministic_errors": deterministic_errors,
        "model_output_quality_issues": quality_issues,
        "propagates_to_later_stages": bool(deterministic_errors),
    }


def run_live_story(output_dir: str | Path) -> tuple[int, Dict[str, Any]]:
    root = Path(output_dir)
    run_dir = root / "system" / "quality_runs" / RUN_ID
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "stage_report.json"
    trace_path = run_dir / "llm_calls.jsonl"
    previous_report: Dict[str, Any] = {}
    if report_path.exists():
        try:
            previous_report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous_report = {}
    if previous_report.get("status") == "passed":
        return 0, previous_report
    host, port = _target()
    reachable, error = _reachable(host, port)
    recovery = (
        f'.\\.venv\\Scripts\\python.exe -m core.evaluation.live_story_quality '
        f'--output-dir "{root}"'
    )
    if not reachable:
        report = {
            "run_id": RUN_ID,
            "mode": "real",
            "status": "blocked",
            "attempted_model_calls": 0,
            "reason": f"configured endpoint is not accepting TCP connections ({error})",
            "recovery": recovery,
        }
        _write_json(run_dir / "blocker.json", report)
        return 2, report

    parameters = deepcopy(PARAMETERS)
    parameters.update({"Backend": "api", "Model": LIVE_MODEL})
    profile = get_domain_profile("legal_suspense")
    manager = StoryLedgerManager(str(root))
    manager.initialize(parameters)
    case_bible = manager.save_case_bible(
        deepcopy(CASE_BIBLE), "固定真实三章探针设计上下文"
    )
    set_backend("api", LIVE_MODEL)
    historical_calls = _load_trace(trace_path)
    ledger_before = manager.load_suspense_ledger()
    accepted = sorted(
        int(item.get("chapter"))
        for item in ledger_before.get("accepted_chapters", [])
        if isinstance(item, dict) and item.get("chapter")
    )
    resuming = bool(previous_report and (accepted or historical_calls))
    attempt_budget = MAX_RESUME_CALLS if resuming else MAX_MODEL_CALLS
    call_counter = 0

    def budgeted_send(prompt: str, model: str | None = None) -> str:
        nonlocal call_counter
        if call_counter >= attempt_budget:
            raise ModelCallBudgetExceeded(
                f"本次真实模型调用达到上限 {attempt_budget}，已停止后续生成"
            )
        call_counter += 1
        return send_prompt(prompt, model=model)

    reviewer = DomainReviewAgent(
        model=LIVE_MODEL,
        profile=profile,
        send_prompt_fn=budgeted_send,
    )
    roster = json.dumps(CHARACTERS, ensure_ascii=False)
    factions = json.dumps(FACTIONS, ensure_ascii=False)
    chapter_reports: Dict[str, Any] = deepcopy(previous_report.get("chapters", {}))
    previous_tail = ""
    if accepted:
        previous_path = (
            root / "story" / "content" / "chapters" / f"chapter_{accepted[-1]}.md"
        )
        if previous_path.exists():
            previous_tail = previous_path.read_text(encoding="utf-8")[-2500:]
    stopped_reason = ""
    started = time.perf_counter()

    try:
        with trace_session(root, run_id=RUN_ID, mode="real") as recorder:
            for chapter_number in range(len(accepted) + 1, 4):
                contract = ChapterGenerationLoop._apply_case_bible_knowledge_boundaries(
                    _contract(chapter_number), case_bible, chapter_number
                )
                manager.save_contract(chapter_number, contract, PLANS[chapter_number])
                scene_plans = parse_scene_sections(PLANS[chapter_number])
                scenes: List[str] = []
                scene_sources: List[str] = []
                for scene_number, scene_plan in enumerate(scene_plans, 1):
                    next_plan = (
                        scene_plans[scene_number]
                        if scene_number < len(scene_plans)
                        else ""
                    )
                    continuity_tail = scenes[-1][-2500:] if scenes else previous_tail
                    prompt = build_scene_prompt(
                        scene_plan=scene_plan,
                        scene_number=scene_number,
                        parameters=parameters,
                        lore=LORE,
                        character_roster=roster,
                        faction_summary=factions,
                        profile=profile,
                        chapter_number=chapter_number,
                        structure_name="3-Act Structure",
                        novel_title="雨夜门禁",
                        contract=contract,
                        previous_scene_tail=continuity_tail,
                        next_scene_plan=next_plan,
                    )
                    generation_stage = (
                        f"chapter_{chapter_number}_scene_{scene_number}_prose_generation"
                    )
                    cached = next(
                        (
                            str(call.get("response", "")).strip()
                            for call in reversed(historical_calls)
                            if call.get("stage") == generation_stage
                            and call.get("success")
                            and str(call.get("response", "")).strip()
                        ),
                        "",
                    )
                    if cached:
                        scene = cached
                        scene_sources.append("cached_live_trace")
                    else:
                        with trace_stage(generation_stage):
                            scene = budgeted_send(prompt, model=LIVE_MODEL).strip()
                        scene_sources.append("new_live_call")
                    if not scene:
                        raise ValueError(
                            f"第 {chapter_number} 章场景 {scene_number} 返回空正文"
                        )
                    scenes.append(scene)

                prose = "\n\n---\n\n".join(scenes)
                initial_prose = prose
                reviews = _review_bundle(
                    reviewer,
                    prose,
                    contract,
                    case_bible,
                    manager.load_suspense_ledger(),
                    previous_tail,
                    suffix=f"_chapter_{chapter_number}",
                )
                contract_review, blind_review, plausibility_review, merged = reviews
                baseline_reviews = _review_dict(*reviews)
                retry = None

                if not merged.passed:
                    requested = {
                        "contract": contract_review.asks,
                        "reader_blind": blind_review.asks,
                        "plausibility": plausibility_review.asks,
                    }
                    target = ChapterGenerationLoop._target_scene(
                        merged.repair_scope, len(scenes)
                    )
                    prior = scenes[target - 2][-2500:] if target > 1 else previous_tail
                    next_plan = scene_plans[target] if target < len(scene_plans) else ""
                    with trace_stage(
                        f"targeted_revision_and_retry_chapter_{chapter_number}"
                    ):
                        revised_scene = reviewer.revise_scene(
                            scenes[target - 1],
                            merged,
                            scene_plans[target - 1],
                            prior,
                            next_plan,
                            contract,
                        )
                    if not revised_scene.strip():
                        raise ValueError(
                            f"第 {chapter_number} 章定向修订返回空正文"
                        )
                    candidate_scenes = list(scenes)
                    candidate_scenes[target - 1] = revised_scene
                    candidate_prose = "\n\n---\n\n".join(candidate_scenes)
                    revised_reviews = _review_bundle(
                        reviewer,
                        candidate_prose,
                        contract,
                        case_bible,
                        manager.load_suspense_ledger(),
                        previous_tail,
                        suffix=f"_chapter_{chapter_number}_retry_1",
                        requested=requested,
                    )
                    revised_merged = revised_reviews[-1]
                    improved = _review_rank(revised_merged) > _review_rank(merged)
                    retry = {
                        "attempt": 1,
                        "target_scene": target,
                        "requested": requested,
                        "before_average": merged.average_score,
                        "after_average": revised_merged.average_score,
                        "before_passed": merged.passed,
                        "after_passed": revised_merged.passed,
                        "improved": improved,
                        "kept_revision": improved,
                    }
                    if improved:
                        scenes = candidate_scenes
                        prose = candidate_prose
                        reviews = revised_reviews
                        contract_review, blind_review, plausibility_review, merged = reviews

                gate_waiver = ""
                if not merged.passed:
                    gate_waiver = waiver_reason(
                        merged, f"第 {chapter_number} 章三路独立审阅"
                    ) or ""
                    if gate_waiver:
                        merged = merged.waive(gate_waiver)
                        reviews = (
                            contract_review,
                            blind_review,
                            plausibility_review,
                            merged,
                        )

                style_issues = analyze_chinese_prose_style(prose)
                narrative = analyze_narrative_quality(prose).to_dict()
                # Sentence-length and structural counters are diagnostics.  They
                # are deliberately not a mechanical gate; the isolated reader
                # review must quote an actual reading consequence to block prose.
                passed = merged.passed
                chapter_report = {
                    "chapter": chapter_number,
                    "status": "review_passed" if passed else "quality_failed",
                    "initial_prose": initial_prose,
                    "prose": prose,
                    "baseline_reviews": baseline_reviews,
                    "reviews": _review_dict(*reviews),
                    "retry": retry,
                    "gate_waiver": gate_waiver,
                    "style_issues": style_issues,
                    "style_issues_are_diagnostic_only": True,
                    "narrative_signals": narrative,
                    "contract": contract,
                    "scene_sources": scene_sources,
                }
                chapter_reports[str(chapter_number)] = chapter_report
                if not passed:
                    stopped_reason = f"第 {chapter_number} 章质量门未通过"
                    break

                chapter_path = (
                    root
                    / "story"
                    / "content"
                    / "chapters"
                    / f"chapter_{chapter_number}.md"
                )
                _write_text(chapter_path, prose)
                acceptance = ChapterAcceptanceService(manager).accept(
                    chapter_number=chapter_number,
                    reviewed_content=prose,
                    contract=contract,
                    chapter_review=merged.to_dict(),
                    base_revision=manager.current_revision(),
                    chapter_path=str(chapter_path),
                )
                chapter_report["acceptance"] = {
                    "revision": acceptance.committed_revision,
                    "artifact_passed": acceptance.artifact_report.passed,
                    "consistency_passed": acceptance.consistency_report.passed,
                }
                if not (
                    acceptance.artifact_report.passed
                    and acceptance.consistency_report.passed
                ):
                    stopped_reason = f"第 {chapter_number} 章确定性验收未通过"
                    break
                accepted.append(chapter_number)
                previous_tail = prose[-2500:]

        trace_calls = _load_trace(trace_path)
    except Exception as exc:
        trace_calls = _load_trace(trace_path)
        stopped_reason = f"{type(exc).__name__}: {exc}"

    ledger = manager.load_suspense_ledger()
    isolation = {
        "reader_blind_has_no_contract": all(
            "章节契约：" not in call["prompt"]
            for call in trace_calls
            if call["stage"].startswith("reader_blind_review")
        ),
        "reader_blind_declares_hidden_intent": all(
            "你没有章节大纲、章节契约" in call["prompt"]
            for call in trace_calls
            if call["stage"].startswith("reader_blind_review")
        ),
        "plausibility_has_no_contract": all(
            "章节契约：" not in call["prompt"]
            for call in trace_calls
            if call["stage"].startswith("plausibility_review")
        ),
    }
    success = (
        accepted == [1, 2, 3]
        and not ledger.get("unresolved_conflicts")
        and all(isolation.values())
    )
    ab_summary = None
    if success:
        baseline_dir = root / "baseline"
        for chapter_number in range(1, 4):
            baseline = "\n\n---\n\n".join(
                INITIAL_SCENES[(chapter_number, scene)] for scene in (1, 2)
            )
            _write_text(baseline_dir / f"chapter_{chapter_number}.md", baseline)
        create_blind_test(
            baseline_dir,
            root / "story" / "content" / "chapters",
            root / "ab_blind_test",
            participants=30,
            seed=SEED,
        )
        with (root / "ab_blind_test" / "responses.csv").open(
            "r", encoding="utf-8-sig", newline=""
        ) as handle:
            response_rows = sum(1 for _ in csv.DictReader(handle))
        ab_summary = {
            "packets": len(list((root / "ab_blind_test" / "packets").glob("*.md"))),
            "response_rows": response_rows,
            "summary_command": (
                f'.\\.venv\\Scripts\\python.exe -m core.evaluation.reader_blind_test '
                f'summarize --test-dir "{root / "ab_blind_test"}"'
            ),
        }

    stages = {}
    for stage_name in sorted({call["stage"] for call in trace_calls}):
        stages[stage_name] = _stage_summary(
            [call for call in trace_calls if call["stage"] == stage_name]
        )
    report = {
        "run_id": RUN_ID,
        "mode": "real",
        "status": "passed" if success else "quality_failed",
        "configured_model": LIVE_MODEL,
        "fixed_seed": SEED,
        "call_budget": {
            "initial": MAX_MODEL_CALLS,
            "resume": MAX_RESUME_CALLS,
            "this_attempt": attempt_budget,
        },
        "attempted_model_calls": len(trace_calls),
        "budgeted_send_attempts_this_attempt": call_counter,
        "resumed_from_previous_failure": resuming,
        "schema_retry_calls": sum(
            "不符合 schema" in call["prompt"] for call in trace_calls
        ),
        "elapsed_ms": round(
            float(previous_report.get("elapsed_ms", 0) or 0)
            + (time.perf_counter() - started) * 1000,
            3,
        ),
        "accepted_chapters": accepted,
        "ledger_revision": ledger.get("revision"),
        "unresolved_conflicts": ledger.get("unresolved_conflicts", []),
        "isolation": isolation,
        "chapters": chapter_reports,
        "stages": stages,
        "ab_blind_test": ab_summary,
        "reason": stopped_reason,
        "recovery": recovery if not success else "",
    }
    _write_json(report_path, report)
    return (0 if success else 1), report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行固定三章中文悬疑真实模型质量流程")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    code, report = run_live_story(args.output_dir)
    _print_json(
        {
            "run_id": report["run_id"],
            "status": report["status"],
            "attempted_model_calls": report.get("attempted_model_calls", 0),
            "accepted_chapters": report.get("accepted_chapters", []),
            "reason": report.get("reason", ""),
        }
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
