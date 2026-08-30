import csv
import json

from core.evaluation.fixed_story_quality import REQUIRED_STAGES, run_fixed_story_quality


def test_fixed_story_runs_three_chapters_to_acceptance_and_ab_export(tmp_path):
    report = run_fixed_story_quality(tmp_path)

    assert report["mode"] == "scripted_offline"
    assert report["seed"] == 20260829
    assert report["accepted_chapters"] == [1, 2, 3]
    assert report["ledger_revision"] == 3
    assert report["unresolved_conflicts"] == []
    assert report["retry_count"] >= 1
    assert set(report["stages"]) == set(REQUIRED_STAGES)
    assert all(report["stages"][stage]["passed"] for stage in REQUIRED_STAGES)
    elapsed = [report["stages"][stage]["elapsed_ms"] for stage in REQUIRED_STAGES]
    assert all(value >= 0 for value in elapsed)
    assert len(set(elapsed)) > 2
    assert all(
        not diagnostics["style"]
        for diagnostics in report["chapter_diagnostics"].values()
    )

    run_dir = tmp_path / "system" / "quality_runs" / report["run_id"]
    calls = [
        json.loads(line)
        for line in (run_dir / "llm_calls.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert calls and all(call["mode"] == "scripted_offline" for call in calls)
    assert any(call["stage"] == "targeted_revision_and_retry" for call in calls)

    blind_prompts = [call["prompt"] for call in calls if call["stage"] == "reader_blind_review"]
    assert blind_prompts
    assert all("你没有章节大纲、章节契约" in prompt for prompt in blind_prompts)
    assert all("章节契约：" not in prompt for prompt in blind_prompts)

    plausibility_prompts = [
        call["prompt"] for call in calls if call["stage"] == "plausibility_review"
    ]
    assert plausibility_prompts
    assert all("只有这里写明的规则才可覆盖现实常识" in prompt for prompt in plausibility_prompts)
    assert all("章节契约：" not in prompt for prompt in plausibility_prompts)

    baseline = (tmp_path / "baseline" / "chapter_2.md").read_text(encoding="utf-8")
    candidate = (tmp_path / "story" / "content" / "chapters" / "chapter_2.md").read_text(
        encoding="utf-8"
    )
    assert "盘片已经碎裂" in baseline and "恢复了完整录像" in baseline
    assert "独立审计副本" in candidate
    assert "恢复了完整录像" not in candidate

    with (tmp_path / "ab_blind_test" / "responses.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 90
    assert len(list((tmp_path / "ab_blind_test" / "packets").glob("reader_*.md"))) == 30
