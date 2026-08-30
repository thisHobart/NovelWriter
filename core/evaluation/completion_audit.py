"""Deterministic completion audit for the fixed NovelWriter quality fixture."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Dict, Iterable

from core.evaluation.fixed_story_quality import REQUIRED_STAGES
from core.generation.narrative_quality import analyze_narrative_quality
from core.generation.prompt_context import analyze_chinese_prose_style
from core.generation.story_ledger import source_hash


def _check(checks: Dict[str, Dict[str, Any]], name: str, passed: bool, evidence: Any) -> None:
    checks[name] = {"passed": bool(passed), "evidence": evidence}


def _json_files(root: Path) -> Iterable[Path]:
    yield from root.rglob("*.json")


def _jsonl_calls(path: Path) -> list[Dict[str, Any]]:
    calls = []
    if not path.exists():
        return calls
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number} 不是 JSON 对象")
        calls.append(value)
    return calls


def run_completion_audit(real_dir: str | Path, offline_dir: str | Path) -> Dict[str, Any]:
    real = Path(real_dir)
    offline = Path(offline_dir)
    checks: Dict[str, Dict[str, Any]] = {}
    parse_errors = []
    json_count = 0
    for root in (real, offline):
        for path in _json_files(root):
            json_count += 1
            try:
                json.loads(path.read_text(encoding="utf-8-sig"))
            except Exception as exc:
                parse_errors.append(f"{path}: {type(exc).__name__}: {exc}")
    for path in (
        real / "system" / "quality_runs" / "live_fixed_mystery_3ch_v1" / "llm_calls.jsonl",
        real / "system" / "quality_runs" / "live_style_polish_v1" / "llm_calls.jsonl",
        offline / "system" / "quality_runs" / "fixed_mystery_3ch_v1" / "llm_calls.jsonl",
    ):
        try:
            _jsonl_calls(path)
        except Exception as exc:
            parse_errors.append(f"{path}: {type(exc).__name__}: {exc}")
    _check(checks, "structured_outputs_parse", not parse_errors, {"json_files": json_count, "errors": parse_errors})

    offline_report = json.loads(
        (offline / "system" / "quality_runs" / "fixed_mystery_3ch_v1" / "stage_report.json").read_text(encoding="utf-8")
    )
    missing_stages = sorted(set(REQUIRED_STAGES) - set(offline_report.get("stages", {})))
    _check(
        checks,
        "all_required_pipeline_stages_recorded",
        not missing_stages and len(offline_report.get("stages", {})) == len(REQUIRED_STAGES),
        {"stage_count": len(offline_report.get("stages", {})), "missing": missing_stages},
    )

    main_path = real / "system" / "quality_runs" / "live_fixed_mystery_3ch_v1" / "stage_report.json"
    main = json.loads(main_path.read_text(encoding="utf-8"))
    ledger = json.loads(
        (real / "system" / "story_ledgers" / "suspense_ledger.json").read_text(encoding="utf-8")
    )
    accepted = sorted(int(item["chapter"]) for item in ledger.get("accepted_chapters", []))
    _check(
        checks,
        "three_chapters_accepted_and_saved",
        accepted == [1, 2, 3] and int(ledger.get("revision", 0)) == 4,
        {"accepted": accepted, "revision": ledger.get("revision")},
    )
    _check(
        checks,
        "no_unresolved_canon_conflicts",
        not ledger.get("unresolved_conflicts") and not ledger.get("pending_regenerations"),
        {
            "unresolved_conflicts": ledger.get("unresolved_conflicts", []),
            "pending_regenerations": ledger.get("pending_regenerations", []),
        },
    )

    hash_mismatches = []
    accepted_by_chapter = {int(item["chapter"]): item for item in ledger["accepted_chapters"]}
    final_diagnostics = {}
    final_review_failures = []
    for chapter in (1, 2, 3):
        chapter_path = real / "story" / "content" / "chapters" / f"chapter_{chapter}.md"
        text = chapter_path.read_text(encoding="utf-8")
        if source_hash(text) != accepted_by_chapter[chapter].get("content_hash"):
            hash_mismatches.append(chapter)
        narrative = analyze_narrative_quality(text)
        style = analyze_chinese_prose_style(text)
        review = main["chapters"][str(chapter)]["reviews"]["merged"]
        if not review.get("passed") or review.get("hard_failures"):
            final_review_failures.append(chapter)
        final_diagnostics[str(chapter)] = {
            "chars": len(text),
            "style_warnings": style,
            "embodied_emotion": narrative.embodied_emotion.count,
            "narrative_warnings": narrative.warnings,
            "review_passed": review.get("passed"),
        }
    _check(checks, "accepted_files_match_ledger_hashes", not hash_mismatches, {"mismatches": hash_mismatches})
    _check(checks, "final_independent_reviews_pass", not final_review_failures, {"failed_chapters": final_review_failures})
    _check(
        checks,
        "blind_and_plausibility_contexts_are_isolated",
        all(main.get("isolation", {}).values()),
        main.get("isolation", {}),
    )
    body_warning_chapters = [
        chapter
        for chapter, item in final_diagnostics.items()
        if any("身体化情绪套语" in warning for warning in item["narrative_warnings"])
    ]
    _check(
        checks,
        "no_template_body_reaction_saturation",
        not body_warning_chapters,
        {"warning_chapters": body_warning_chapters, "chapters": final_diagnostics},
    )

    duplicate_groups = {}
    for field in ("facts", "timeline_events", "clues", "evidence", "plot_threads"):
        ids = [str(item.get("id")) for item in ledger.get(field, []) if isinstance(item, dict) and item.get("id")]
        duplicate_groups[field] = sorted({item for item in ids if ids.count(item) > 1})
    knowledge_duplicates = {
        character: len(facts) != len(set(facts))
        for character, facts in ledger.get("character_knowledge", {}).items()
    }
    _check(
        checks,
        "ledger_ids_and_character_knowledge_are_unique",
        not any(duplicate_groups.values()) and not any(knowledge_duplicates.values()),
        {"duplicate_ids": duplicate_groups, "character_knowledge_duplicates": knowledge_duplicates},
    )

    manifest = json.loads((real / "ab_blind_test" / "manifest.private.json").read_text(encoding="utf-8"))
    packet_count = len(list((real / "ab_blind_test" / "packets").glob("*.md")))
    with (real / "ab_blind_test" / "responses.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        response_rows = len(list(csv.DictReader(handle)))
    balance_errors = {}
    for item_id in manifest.get("items", {}):
        positions = [mapping[item_id] for mapping in manifest["assignments"].values()]
        if positions.count("a") != 15 or positions.count("b") != 15:
            balance_errors[item_id] = {"a": positions.count("a"), "b": positions.count("b")}
    _check(
        checks,
        "ab_materials_ready_for_30_readers",
        packet_count == 30 and response_rows == 90 and not balance_errors,
        {"packets": packet_count, "response_rows": response_rows, "balance_errors": balance_errors},
    )

    core_calls = _jsonl_calls(
        real / "system" / "quality_runs" / "live_fixed_mystery_3ch_v1" / "llm_calls.jsonl"
    )
    polish_calls = _jsonl_calls(
        real / "system" / "quality_runs" / "live_style_polish_v1" / "llm_calls.jsonl"
    )
    calls = core_calls + polish_calls
    missing_trace_fields = [
        call.get("sequence")
        for call in calls
        if not all(key in call for key in ("prompt", "response", "elapsed_ms", "usage", "cost", "stage"))
    ]
    exact_usage_available = any(call.get("usage", {}).get("total_tokens") is not None for call in calls)
    _check(
        checks,
        "real_calls_are_traceable_and_cost_unavailability_explicit",
        len(calls) == 46 and not missing_trace_fields and not exact_usage_available,
        {
            "real_calls": len(calls),
            "schema_repair_prompts": sum("不符合 schema" in call.get("prompt", "") for call in calls),
            "max_prompt_chars": max((int(call.get("prompt_chars", 0)) for call in calls), default=0),
            "estimated_prompt_tokens": sum(int(call.get("usage", {}).get("estimated_prompt_tokens") or 0) for call in calls),
            "estimated_completion_tokens": sum(int(call.get("usage", {}).get("estimated_completion_tokens") or 0) for call in calls),
            "exact_usage_available": exact_usage_available,
            "cost_reason": "provider usage/pricing not exposed by configured text backend",
            "missing_trace_fields": missing_trace_fields,
        },
    )

    retry_failures = [
        chapter
        for chapter, data in main.get("chapters", {}).items()
        if data.get("retry") and not data["retry"].get("improved")
    ]
    polish = main.get("chapters", {}).get("3", {}).get("style_polish", {})
    _check(
        checks,
        "retries_improve_or_stop",
        not retry_failures
        and int(polish.get("body_count_after", 999)) < int(polish.get("body_count_before", 0)),
        {"unimproved_kept_retries": retry_failures, "style_polish": polish},
    )

    report = {
        "audit": "fixed_mystery_end_to_end_completion_v1",
        "passed": all(item["passed"] for item in checks.values()),
        "real_run": str(real.resolve()),
        "offline_run": str(offline.resolve()),
        "checks": checks,
    }
    output = real / "system" / "quality_runs" / "completion_audit.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="审计固定三章质量改造完成状态")
    parser.add_argument("--real-dir", required=True)
    parser.add_argument("--offline-dir", required=True)
    args = parser.parse_args(argv)
    report = run_completion_audit(args.real_dir, args.offline_dir)
    print(json.dumps({"passed": report["passed"], "checks": len(report["checks"])}, ensure_ascii=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
