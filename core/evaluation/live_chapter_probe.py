"""Generate and independently review one fixed chapter with the configured live model."""
from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict

from agents.review.domain_review_agent import DomainReviewAgent
from core.evaluation.fixed_story_quality import (
    CASE_BIBLE,
    CHARACTERS,
    FACTIONS,
    LORE,
    MODEL as SCRIPTED_MODEL,
    PARAMETERS,
    _contract,
)
from core.evaluation.live_quality_probe import _reachable, _target
from core.generation.ai_helper import send_prompt, set_backend
from core.generation.chapter_acceptance import ChapterAcceptanceService
from core.generation.chapter_generation_loop import ChapterGenerationLoop
from core.generation.domain_profiles import get_domain_profile
from core.generation.llm_trace import trace_session, trace_stage
from core.generation.narrative_quality import analyze_narrative_quality
from core.generation.prompt_context import analyze_chinese_prose_style
from core.generation.scene_prompt import build_scene_prompt
from core.generation.story_ledger import StoryLedgerManager


RUN_ID = "live_single_chapter_v1"
LIVE_MODEL = "hosted-llm"
PLAN = """### 场景 1：一条不能证明人的记录
雨夜，刑警程砚在档案数字化中心核对现场。陈岚的门禁卡在21:17被读取，但门禁只记录卡，不能证明持卡人。损坏硬盘只封存，不得恢复或承担核心推理。写成一章克制自然的中文悬疑正文，约700至1000字；本章只提出谁使用门禁卡的问题，不揭晓凶手。"""


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _review_rank(review: Any) -> tuple[int, int, int, int, float]:
    components = review.component_reviews or {}
    component_passes = sum(bool(item.get("passed")) for item in components.values())
    return (
        int(review.passed),
        -len(review.hard_failures),
        -len(review.blocking_dimensions),
        component_passes,
        review.average_score,
    )


def _print_json(value: Any) -> None:
    """Print reproducibly even when Windows inherited a cp1252 stdout."""

    encoding = str(getattr(sys.stdout, "encoding", "") or "utf-8").lower()
    ascii_only = "utf" not in encoding
    print(json.dumps(value, ensure_ascii=ascii_only, indent=2))


def run_probe(output_dir: str | Path) -> tuple[int, Dict[str, Any]]:
    root = Path(output_dir)
    run_dir = root / "system" / "quality_runs" / RUN_ID
    run_dir.mkdir(parents=True, exist_ok=True)
    host, port = _target()
    reachable, error = _reachable(host, port)
    if not reachable:
        report = {
            "run_id": RUN_ID,
            "mode": "real",
            "status": "blocked",
            "attempted_model_calls": 0,
            "reason": f"configured endpoint is not accepting TCP connections ({error})",
            "recovery": (
                f".\\.venv\\Scripts\\python.exe -m core.evaluation.live_chapter_probe "
                f"--output-dir \"{root}\""
            ),
        }
        _write_json(run_dir / "blocker.json", report)
        return 2, report

    profile = get_domain_profile("legal_suspense")
    parameters = deepcopy(PARAMETERS)
    parameters.update({"Backend": "api", "Model": LIVE_MODEL})
    manager = StoryLedgerManager(str(root))
    manager.initialize(parameters)
    case_bible = manager.save_case_bible(deepcopy(CASE_BIBLE), "固定单章真实探针设计上下文")
    contract = ChapterGenerationLoop._apply_case_bible_knowledge_boundaries(
        _contract(1), case_bible, chapter_number=1
    )
    manager.save_contract(1, contract, PLAN)
    set_backend("api", LIVE_MODEL)
    reviewer = DomainReviewAgent(
        model=LIVE_MODEL,
        profile=profile,
        send_prompt_fn=send_prompt,
    )
    prompt = build_scene_prompt(
        scene_plan=PLAN,
        scene_number=1,
        parameters=parameters,
        lore=LORE,
        character_roster=json.dumps(CHARACTERS, ensure_ascii=False),
        faction_summary=json.dumps(FACTIONS, ensure_ascii=False),
        profile=profile,
        chapter_number=1,
        structure_name="3-Act Structure",
        novel_title="雨夜门禁",
        contract=contract,
    )

    try:
        with trace_session(root, run_id=RUN_ID, mode="real") as recorder:
            with trace_stage("scene_prose_generation"):
                prose = send_prompt(prompt, model=LIVE_MODEL).strip()
            if not prose:
                raise ValueError("真实模型返回空章节")
            with trace_stage("contract_compliance_review"):
                contract_review = reviewer.review_chapter(
                    prose, contract, case_bible, manager.load_suspense_ledger()
                )
            with trace_stage("reader_blind_review"):
                blind_review = reviewer.review_reader_blind(prose, "")
            with trace_stage("plausibility_review"):
                plausibility_review = reviewer.review_plausibility(prose, case_bible)
            merged = reviewer._merge_reviews(
                "chapter",
                {
                    "contract": contract_review,
                    "reader_blind": blind_review,
                    "plausibility": plausibility_review,
                },
            )
            baseline_prose = prose
            baseline_reviews = {
                "contract": contract_review.to_dict(),
                "reader_blind": blind_review.to_dict(),
                "plausibility": plausibility_review.to_dict(),
                "merged": merged.to_dict(),
            }
            retry_history = []
            if not merged.passed:
                before = merged
                requested = {
                    "contract": contract_review.asks,
                    "reader_blind": blind_review.asks,
                    "plausibility": plausibility_review.asks,
                }
                with trace_stage("targeted_revision_and_retry"):
                    revised_prose = reviewer.revise_scene(
                        prose,
                        merged,
                        PLAN,
                        "",
                        "",
                        contract,
                    )
                if not revised_prose:
                    raise ValueError("真实模型返回空的定向修订稿")
                with trace_stage("contract_compliance_review_retry_1"):
                    revised_contract = reviewer.review_chapter(
                        revised_prose,
                        contract,
                        case_bible,
                        manager.load_suspense_ledger(),
                        repairs_requested=requested["contract"],
                    )
                with trace_stage("reader_blind_review_retry_1"):
                    revised_blind = reviewer.review_reader_blind(
                        revised_prose, "", repairs_requested=requested["reader_blind"]
                    )
                with trace_stage("plausibility_review_retry_1"):
                    revised_plausibility = reviewer.review_plausibility(
                        revised_prose,
                        case_bible,
                        repairs_requested=requested["plausibility"],
                    )
                revised_merged = reviewer._merge_reviews(
                    "chapter",
                    {
                        "contract": revised_contract,
                        "reader_blind": revised_blind,
                        "plausibility": revised_plausibility,
                    },
                )
                improved = _review_rank(revised_merged) > _review_rank(before)
                retry_history.append(
                    {
                        "attempt": 1,
                        "requested": requested,
                        "before_average": before.average_score,
                        "after_average": revised_merged.average_score,
                        "before_passed": before.passed,
                        "after_passed": revised_merged.passed,
                        "improved": improved,
                        "kept_revision": improved,
                    }
                )
                if improved:
                    prose = revised_prose
                    contract_review = revised_contract
                    blind_review = revised_blind
                    plausibility_review = revised_plausibility
                    merged = revised_merged

        style_issues = analyze_chinese_prose_style(prose)
        narrative = analyze_narrative_quality(prose).to_dict()
        trace_by_stage = {
            stage: [call for call in recorder.calls if call["stage"].startswith(stage)]
            for stage in (
                "scene_prose_generation",
                "contract_compliance_review",
                "reader_blind_review",
                "plausibility_review",
            )
        }
        isolation = {
            "reader_blind_has_no_contract": all(
                "章节契约：" not in call["prompt"]
                for call in trace_by_stage["reader_blind_review"]
            ),
            "reader_blind_declares_hidden_intent": all(
                "你没有章节大纲、章节契约" in call["prompt"]
                for call in trace_by_stage["reader_blind_review"]
            ),
            "plausibility_has_no_contract": all(
                "章节契约：" not in call["prompt"]
                for call in trace_by_stage["plausibility_review"]
            ),
        }
        passed = merged.passed and not style_issues and all(isolation.values())
        acceptance = None
        chapter_path = root / "story" / "content" / "chapters" / "chapter_1.md"
        if passed:
            _write_text(chapter_path, prose)
            acceptance = ChapterAcceptanceService(manager).accept(
                chapter_number=1,
                reviewed_content=prose,
                contract=contract,
                chapter_review=merged.to_dict(),
                base_revision=0,
                chapter_path=str(chapter_path),
            )
            passed = acceptance.artifact_report.passed and acceptance.consistency_report.passed
        status = "passed" if passed else "quality_failed"
        report = {
            "run_id": RUN_ID,
            "mode": "real",
            "status": status,
            "configured_model": LIVE_MODEL,
            "upstream_scripted_model_not_used": SCRIPTED_MODEL,
            "attempted_model_calls": len(recorder.calls),
            "schema_retry_calls": sum(
                "不符合 schema" in call["prompt"] for call in recorder.calls
            ),
            "prose": prose,
            "prose_chars": len(prose),
            "baseline_prose": baseline_prose,
            "baseline_reviews": baseline_reviews,
            "retry_count": len(retry_history),
            "retry_history": retry_history,
            "style_issues": style_issues,
            "narrative_signals": narrative,
            "reviews": {
                "contract": contract_review.to_dict(),
                "reader_blind": blind_review.to_dict(),
                "plausibility": plausibility_review.to_dict(),
                "merged": merged.to_dict(),
            },
            "isolation": isolation,
            "acceptance": (
                {
                    "revision": acceptance.committed_revision,
                    "artifact_passed": acceptance.artifact_report.passed,
                    "consistency_passed": acceptance.consistency_report.passed,
                }
                if acceptance
                else None
            ),
            "continue_to_three_chapters": status == "passed",
        }
        code = 0 if status == "passed" else 1
    except Exception as exc:
        partial_reviews = {}
        for name, local_name in (
            ("contract", "contract_review"),
            ("reader_blind", "blind_review"),
            ("plausibility", "plausibility_review"),
        ):
            value = locals().get(local_name)
            if value is not None:
                partial_reviews[name] = value.to_dict()
        report = {
            "run_id": RUN_ID,
            "mode": "real",
            "status": "blocked",
            "attempted_model_calls": len(recorder.calls) if "recorder" in locals() else 0,
            "schema_retry_calls": (
                sum("不符合 schema" in call["prompt"] for call in recorder.calls)
                if "recorder" in locals()
                else 0
            ),
            "reason": f"{type(exc).__name__}: {exc}",
            "prose": locals().get("prose", ""),
            "partial_reviews": partial_reviews,
            "continue_to_three_chapters": False,
        }
        code = 2
    _write_json(run_dir / "probe_report.json", report)
    return code, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行固定悬疑故事的真实单章质量探针")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    code, report = run_probe(args.output_dir)
    _print_json(report)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
