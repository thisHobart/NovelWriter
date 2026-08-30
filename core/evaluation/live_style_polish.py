"""Re-review and re-accept a style-only revision of an accepted live chapter."""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from agents.review.domain_review_agent import (
    DomainReview,
    DomainReviewAgent,
    extract_json_object,
)
from core.evaluation.fixed_story_quality import CASE_BIBLE, CHARACTERS, PLANS, SEED
from core.evaluation.live_quality_probe import _reachable, _target
from core.evaluation.live_story_quality import _review_bundle, _review_dict
from core.evaluation.reader_blind_test import create_blind_test
from core.generation.ai_helper import send_prompt, set_backend
from core.generation.chapter_acceptance import ChapterAcceptanceService
from core.generation.chapter_generation_loop import waiver_reason
from core.generation.domain_profiles import get_domain_profile
from core.generation.llm_trace import trace_session, trace_stage
from core.generation.narrative_quality import analyze_narrative_quality
from core.generation.prompt_context import analyze_chinese_prose_style
from core.generation.story_ledger import StoryLedgerManager


RUN_ID = "live_style_polish_v1"
LIVE_MODEL = "hosted-llm"
MAX_CALLS = 8


class PolishCallBudgetExceeded(RuntimeError):
    pass


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _trace_call_count(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            if isinstance(json.loads(line), dict):
                count += 1
        except json.JSONDecodeError:
            continue
    return count


def _print_json(value: Any) -> None:
    encoding = str(getattr(sys.stdout, "encoding", "") or "utf-8").lower()
    print(json.dumps(value, ensure_ascii="utf" not in encoding, indent=2))


def _time_tokens(text: str) -> set[str]:
    return set(re.findall(r"(?<!\d)(?:[01]?\d|2[0-3])[:：][0-5]\d(?!\d)", text))


def _review_from_dict(data: Dict[str, Any]) -> DomainReview:
    return DomainReview(
        stage=str(data.get("stage", "chapter")),
        passed=bool(data.get("passed")),
        scores=dict(data.get("scores", {})),
        hard_failures=list(data.get("hard_failures", [])),
        evidence=list(data.get("evidence", [])),
        repair_scope=str(data.get("repair_scope", "")),
        repair_instructions=list(data.get("repair_instructions", [])),
        upgrades=list(data.get("upgrades", [])),
        strengths=list(data.get("strengths", [])),
        reviewer_warning=str(data.get("reviewer_warning", "")),
        pass_average=float(data.get("pass_average", 0) or 0),
        blocking_dimensions=list(data.get("blocking_dimensions", [])),
        component_reviews=dict(data.get("component_reviews", {})),
        waived=bool(data.get("waived")),
    )


def _polish_candidates(trace_path: Path, chapter_number: int) -> List[Dict[str, Any]]:
    if not trace_path.exists():
        return []
    candidates = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        try:
            call = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(call, dict)
            and str(call.get("stage", "")).startswith(
                f"chapter_{chapter_number}_style_polish"
            )
            and call.get("success")
            and str(call.get("response", "")).strip()
        ):
            candidates.append(call)
    return candidates


def apply_grounded_quote_change(content: str, failure: Dict[str, Any]) -> str:
    """Apply one explicit quote replacement, or return an empty string."""

    quote = str(failure.get("quote", "")).strip()
    change = str(failure.get("change", "")).strip()
    if not quote or quote not in content or content.count(quote) != 1:
        return ""
    replacement_match = re.search(r"改为[：:]?\s*[“\"]([^”\"]+)[”\"]", change)
    spoken = re.findall(r"：“([^”]+)”", quote)
    if not replacement_match or not spoken:
        return ""
    replacement_quote = quote.replace(spoken[-1], replacement_match.group(1), 1)
    if replacement_quote == quote:
        return ""
    return content.replace(quote, replacement_quote, 1)


def _grounded_hard_failures_from_trace(
    trace_path: Path, stage: str, content: str
) -> List[Dict[str, Any]]:
    failures = []
    if not trace_path.exists():
        return failures
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        try:
            call = json.loads(line)
            if call.get("stage") != stage or not call.get("success"):
                continue
            raw = extract_json_object(str(call.get("response", "")))
        except (json.JSONDecodeError, ValueError, AttributeError):
            continue
        for failure in raw.get("hard_failures", []):
            if (
                isinstance(failure, dict)
                and str(failure.get("quote", "")).strip() in content
                and str(failure.get("problem", "")).strip()
                and str(failure.get("change", "")).strip()
            ):
                failures.append(failure)
    return failures


def deterministic_polish_issues(
    before: str,
    after: str,
    *,
    expected_characters: List[str],
) -> List[str]:
    issues = []
    if not after.strip():
        return ["修订正文为空"]
    if before.count("\n\n---\n\n") != after.count("\n\n---\n\n"):
        issues.append("场景分隔线数量发生变化")
    missing_times = sorted(_time_tokens(before) - _time_tokens(after))
    if missing_times:
        issues.append("修订丢失明确时间：" + ", ".join(missing_times))
    missing_characters = [
        name for name in expected_characters if name in before and name not in after
    ]
    if missing_characters:
        issues.append("修订丢失既有人物：" + ", ".join(missing_characters))
    for anchor in ("门禁", "硬盘", "审计副本", "封存"):
        if anchor in before and anchor not in after:
            issues.append(f"修订丢失证据锚点：{anchor}")
    return issues


def run_style_polish(
    output_dir: str | Path,
    *,
    chapter_number: int = 3,
) -> tuple[int, Dict[str, Any]]:
    root = Path(output_dir)
    run_dir = root / "system" / "quality_runs" / RUN_ID
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "report.json"
    trace_path = run_dir / "llm_calls.jsonl"
    previous_report: Dict[str, Any] = {}
    if report_path.exists():
        try:
            previous_report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous_report = {}
    if previous_report.get("status") == "passed":
        cumulative_calls = _trace_call_count(trace_path)
        previous_report["successful_attempt_model_calls"] = previous_report.get(
            "successful_attempt_model_calls",
            previous_report.get("attempted_model_calls", 0),
        )
        previous_report["attempted_model_calls"] = cumulative_calls
        previous_report["cumulative_model_calls"] = cumulative_calls
        _write_json(report_path, previous_report)
        main_report_path = (
            root
            / "system"
            / "quality_runs"
            / "live_fixed_mystery_3ch_v1"
            / "stage_report.json"
        )
        if main_report_path.exists():
            main_report = json.loads(main_report_path.read_text(encoding="utf-8"))
            main_report.setdefault("style_polish", {})[
                "cumulative_model_calls"
            ] = cumulative_calls
            main_report["total_real_model_calls_including_style_polish"] = (
                int(main_report.get("attempted_model_calls", 0)) + cumulative_calls
            )
            _write_json(main_report_path, main_report)
        return 0, previous_report
    host, port = _target()
    reachable, error = _reachable(host, port)
    if not reachable:
        report = {
            "run_id": RUN_ID,
            "mode": "real",
            "status": "blocked",
            "attempted_model_calls": 0,
            "reason": f"configured endpoint is not accepting TCP connections ({error})",
        }
        _write_json(run_dir / "report.json", report)
        return 2, report

    manager = StoryLedgerManager(str(root))
    contract = manager.load_contract(chapter_number, PLANS[chapter_number])
    if not contract:
        report = {
            "run_id": RUN_ID,
            "mode": "real",
            "status": "blocked",
            "attempted_model_calls": 0,
            "reason": f"第 {chapter_number} 章契约不存在或与固定规划不匹配",
        }
        _write_json(run_dir / "report.json", report)
        return 2, report
    chapter_path = (
        root / "story" / "content" / "chapters" / f"chapter_{chapter_number}.md"
    )
    if not chapter_path.exists():
        report = {
            "run_id": RUN_ID,
            "mode": "real",
            "status": "blocked",
            "attempted_model_calls": 0,
            "reason": f"第 {chapter_number} 章已验收正文不存在",
        }
        _write_json(run_dir / "report.json", report)
        return 2, report

    original = chapter_path.read_text(encoding="utf-8")
    before_style = analyze_chinese_prose_style(original)
    before_narrative = analyze_narrative_quality(original)
    case_bible = manager.load_case_bible() or CASE_BIBLE
    previous_path = (
        root / "story" / "content" / "chapters" / f"chapter_{chapter_number - 1}.md"
    )
    previous_tail = (
        previous_path.read_text(encoding="utf-8")[-2500:]
        if previous_path.exists()
        else ""
    )
    set_backend("api", LIVE_MODEL)
    calls = 0

    def budgeted_send(prompt: str, model: str | None = None) -> str:
        nonlocal calls
        if calls >= MAX_CALLS:
            raise PolishCallBudgetExceeded(
                f"语言修订真实调用达到上限 {MAX_CALLS}，未改写正式章节"
            )
        calls += 1
        return send_prompt(prompt, model=model)

    reviewer = DomainReviewAgent(
        model=LIVE_MODEL,
        profile=get_domain_profile("legal_suspense"),
        send_prompt_fn=budgeted_send,
    )
    started = time.perf_counter()
    previous_merged_data = (
        previous_report.get("after", {}).get("reviews", {}).get("merged", {})
    )
    candidates = _polish_candidates(trace_path, chapter_number)
    cached_candidate = str(candidates[-1].get("response", "")).strip() if candidates else ""
    resuming = bool(
        previous_report.get("status") == "quality_failed"
        and cached_candidate
        and previous_merged_data.get("hard_failures")
    )
    deterministic_candidate = ""
    deterministic_failure: Dict[str, Any] = {}
    initial_candidate = next(
        (
            str(call.get("response", "")).strip()
            for call in candidates
            if call.get("stage") == f"chapter_{chapter_number}_style_polish"
        ),
        "",
    )
    if previous_report.get("status") == "quality_failed" and initial_candidate:
        for failure in _grounded_hard_failures_from_trace(
            trace_path,
            f"reader_blind_review_chapter_{chapter_number}_style_polish",
            initial_candidate,
        ):
            patched = apply_grounded_quote_change(initial_candidate, failure)
            if patched:
                deterministic_candidate = patched
                deterministic_failure = failure
                break
    deterministic_resume = bool(deterministic_candidate)
    if deterministic_resume:
        resuming = False
    requested = None
    try:
        with trace_session(root, run_id=RUN_ID, mode="real") as recorder:
            if deterministic_resume:
                polished = deterministic_candidate
                hard_review = DomainReview(
                    stage="reader_blind",
                    passed=False,
                    hard_failures=[deterministic_failure],
                    repair_scope="scene_2",
                    pass_average=3.2,
                )
                requested = {
                    "contract": [],
                    "reader_blind": hard_review.asks,
                    "plausibility": [],
                }
            elif resuming:
                prior_merged = _review_from_dict(previous_merged_data)
                prior_reviews = previous_report["after"]["reviews"]
                requested = {
                    name: _review_from_dict(prior_reviews[name]).asks
                    for name in ("contract", "reader_blind", "plausibility")
                }
                with trace_stage(f"chapter_{chapter_number}_style_polish_retry_1"):
                    polished = reviewer.revise_scene(
                        cached_candidate,
                        prior_merged,
                        PLANS[chapter_number],
                        previous_tail,
                        "",
                        contract,
                    )
            else:
                with trace_stage(f"chapter_{chapter_number}_style_polish"):
                    polished = reviewer.revise_chapter_style(
                        original,
                        contract,
                        before_style,
                        list(before_narrative.embodied_emotion.examples),
                    )
            invariant_issues = deterministic_polish_issues(
                original,
                polished,
                expected_characters=[str(item["name"]) for item in CHARACTERS],
            )
            after_style = analyze_chinese_prose_style(polished)
            after_narrative = analyze_narrative_quality(polished)
            if invariant_issues:
                raise ValueError("；".join(invariant_issues))
            reviews = _review_bundle(
                reviewer,
                polished,
                contract,
                case_bible,
                manager.load_suspense_ledger(),
                previous_tail,
                suffix=(
                    f"_chapter_{chapter_number}_style_polish_deterministic_fix"
                    if deterministic_resume
                    else f"_chapter_{chapter_number}_style_polish_retry_1"
                    if resuming
                    else f"_chapter_{chapter_number}_style_polish"
                ),
                requested=requested,
            )
            merged = reviews[-1]
            gate_waiver = ""
            if not merged.passed:
                gate_waiver = waiver_reason(
                    merged, f"第 {chapter_number} 章语言修订后三路审阅"
                ) or ""
                if gate_waiver:
                    merged = merged.waive(gate_waiver)
                    reviews = (*reviews[:-1], merged)

        body_improved = (
            after_narrative.embodied_emotion.count
            < before_narrative.embodied_emotion.count
            and not any(
                "身体化情绪套语" in warning
                for warning in after_narrative.warnings
            )
        )
        style_improved = len(after_style) < len(before_style)
        passed = merged.passed and body_improved and style_improved
        if not passed:
            reason = (
                "语言修订未同时满足三路审阅、身体套语收敛和确定性文风改善"
            )
            report = {
                "run_id": RUN_ID,
                "mode": "real",
                "status": "quality_failed",
                "attempted_model_calls": len(recorder.calls),
                "resumed_from_failed_candidate": resuming,
                "deterministic_grounded_fix": deterministic_resume,
                "reason": reason,
                "before": {
                    "style": before_style,
                    "narrative": before_narrative.to_dict(),
                },
                "after": {
                    "style": after_style,
                    "narrative": after_narrative.to_dict(),
                    "reviews": _review_dict(*reviews),
                },
            }
            _write_json(report_path, report)
            return 1, report

        candidate_path = run_dir / f"chapter_{chapter_number}_candidate.md"
        candidate_path.write_text(polished, encoding="utf-8")
        acceptance = ChapterAcceptanceService(manager).accept(
            chapter_number=chapter_number,
            reviewed_content=polished,
            contract=contract,
            chapter_review=merged.to_dict(),
            base_revision=manager.current_revision(),
            chapter_path=str(candidate_path),
        )
        chapter_path.write_text(polished, encoding="utf-8")

        main_report_path = (
            root
            / "system"
            / "quality_runs"
            / "live_fixed_mystery_3ch_v1"
            / "stage_report.json"
        )
        main_report = json.loads(main_report_path.read_text(encoding="utf-8"))
        chapter_report = main_report["chapters"][str(chapter_number)]
        chapter_report["prose_before_style_polish"] = original
        chapter_report["prose"] = polished
        chapter_report["reviews"] = _review_dict(*reviews)
        chapter_report["style_issues"] = after_style
        chapter_report["narrative_signals"] = after_narrative.to_dict()
        chapter_report["style_polish"] = {
            "run_id": RUN_ID,
            "body_count_before": before_narrative.embodied_emotion.count,
            "body_count_after": after_narrative.embodied_emotion.count,
            "style_warning_count_before": len(before_style),
            "style_warning_count_after": len(after_style),
            "gate_waiver": gate_waiver,
        }
        chapter_report["acceptance"] = {
            "revision": acceptance.committed_revision,
            "artifact_passed": acceptance.artifact_report.passed,
            "consistency_passed": acceptance.consistency_report.passed,
        }
        main_report["ledger_revision"] = acceptance.committed_revision
        main_report["style_polish"] = {
            "run_id": RUN_ID,
            "chapter": chapter_number,
            "successful_attempt_model_calls": len(recorder.calls),
            "cumulative_model_calls": _trace_call_count(trace_path),
            "resumed_from_failed_candidate": resuming,
            "deterministic_grounded_fix": deterministic_resume,
        }
        main_report["total_real_model_calls_including_style_polish"] = (
            int(main_report.get("attempted_model_calls", 0))
            + _trace_call_count(trace_path)
        )

        create_blind_test(
            root / "baseline",
            root / "story" / "content" / "chapters",
            root / "ab_blind_test",
            participants=30,
            seed=SEED,
        )
        _write_json(main_report_path, main_report)
        report = {
            "run_id": RUN_ID,
            "mode": "real",
            "status": "passed",
            "attempted_model_calls": _trace_call_count(trace_path),
            "successful_attempt_model_calls": len(recorder.calls),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "chapter": chapter_number,
            "revision": acceptance.committed_revision,
            "before": {
                "style": before_style,
                "embodied_count": before_narrative.embodied_emotion.count,
            },
            "after": {
                "style": after_style,
                "embodied_count": after_narrative.embodied_emotion.count,
                "reviews": _review_dict(*reviews),
            },
        }
    except Exception as exc:
        report = {
            "run_id": RUN_ID,
            "mode": "real",
            "status": "blocked",
            "attempted_model_calls": len(recorder.calls) if "recorder" in locals() else 0,
            "reason": f"{type(exc).__name__}: {exc}",
            "official_chapter_unchanged": True,
        }
    _write_json(report_path, report)
    return (0 if report["status"] == "passed" else 2), report


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实模型章节语言收敛与重新验收")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--chapter", type=int, default=3)
    args = parser.parse_args(argv)
    code, report = run_style_polish(args.output_dir, chapter_number=args.chapter)
    _print_json(
        {
            "run_id": report["run_id"],
            "status": report["status"],
            "attempted_model_calls": report.get("attempted_model_calls", 0),
            "reason": report.get("reason", ""),
            "revision": report.get("revision"),
        }
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
