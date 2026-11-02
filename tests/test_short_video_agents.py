import asyncio
from copy import deepcopy
import httpx
from openai import AsyncOpenAI
import pytest

from agents.writing.short_video_script_agent import ShortVideoScriptAgent
from agents.review.short_video_review_agent import ShortVideoReviewAgent
from core.short_video.models import encode, ScriptRequirements
from core.short_video.validation import validate_story, validate_review, to_markdown, local_issues
from core.short_video.llm import ScriptLLM, RequestBudget, BudgetExhausted
from core.generation import api_providers


def test_agents_return_contract_and_full_context(video_requirements, video_outline, video_script, video_review):
    prompts = []
    def send(prompt):
        prompts.append(prompt)
        return encode(video_script)
    writer = ShortVideoScriptAgent(send=send)
    assert writer.process_task({"action": "script", "requirements": video_requirements,
                               "outline": video_outline}).success
    assert "已确认大纲" in prompts[0] and "一次写出完整" in prompts[0]
    reviewer = ShortVideoReviewAgent(send=lambda _: encode(video_review))
    assert reviewer.process_task({"requirements": video_requirements, "outline": video_outline,
                                 "script": video_script}).data["review"] == video_review


@pytest.mark.parametrize("change", ["duration", "reference", "speaker", "empty_action"])
def test_invalid_script_structure_is_rejected(video_requirements, video_script, change):
    if change == "duration":
        video_script["scenes"][0]["duration_seconds"] = float("inf")
    elif change == "reference":
        video_script["scenes"][0]["location_id"] = "unknown"
    elif change == "speaker":
        video_script["scenes"][-1]["dialogue"][0]["character_id"] = "unknown"
    else:
        video_script["scenes"][0]["action"] = ""
    with pytest.raises(ValueError):
        validate_story(video_script, video_requirements, "script")


def test_review_cannot_invent_evidence(video_script, video_review):
    video_review["issues"] = [{"code": "BAD", "severity": "hard", "quote": "正文里没有这句话",
                               "suggestion": "修正", "target": "script"}]
    with pytest.raises(ValueError, match="引文"):
        validate_review(video_review, video_script)


def test_review_accepts_real_multiline_action_evidence(video_script, video_review):
    action = "林青拿起包裹。\n她看向旧邮戳。"
    video_script["scenes"][0]["action"] = action
    video_review["issues"] = [{"code": "DETAIL", "severity": "soft", "quote": action,
                               "suggestion": "补充手指触摸邮戳的动作", "target": "script"}]
    assert validate_review(video_review, video_script)["issues"][0]["quote"] == action


def test_duration_and_markdown_are_honest_estimates(video_requirements, video_script):
    assert not local_issues(video_script, video_requirements)
    video_script["scenes"][0]["duration_seconds"] = 90
    assert local_issues(video_script, video_requirements)[0]["code"] == "DURATION"
    assert "估计" in to_markdown(video_script)


def test_budget_counts_failures_and_never_implicitly_retries():
    records = []
    budget = RequestBudget(max_calls=2)
    def fail(prompt):
        raise TimeoutError("unavailable")
    llm = ScriptLLM("hosted-llm", budget, record=lambda *args: records.append(args), transport=fail)
    for _ in range(2):
        with pytest.raises(TimeoutError):
            llm("prompt")
    with pytest.raises(BudgetExhausted):
        llm("prompt")
    assert budget.calls == 2 and len(records) == 2


def test_recording_failure_is_distinct_from_retryable_model_failure():
    from core.short_video.llm import RecordingError
    budget = RequestBudget(max_calls=4)
    def disk_failure(*args):
        raise OSError("read-only storage")
    with pytest.raises(RecordingError):
        ScriptLLM("hosted-llm", budget, transport=lambda _: "候选原文", record=disk_failure)("prompt")
    assert budget.calls == 1


def test_wall_clock_budget_uses_injected_clock():
    clock = [0.0]
    budget = RequestBudget(max_calls=4, seconds=300, clock=lambda: clock[0])
    clock[0] = 300
    with pytest.raises(BudgetExhausted):
        budget.spend()
    assert budget.calls == 0


def test_sdk_nontext_response_cannot_corrupt_request_history(tmp_path, video_requirements):
    from core.short_video.repository import ScriptRepository
    repo = ScriptRepository(tmp_path)
    repo.save_requirements(video_requirements)
    run = repo.start_run("outline", repo.load(), "hosted-llm")
    llm = ScriptLLM("hosted-llm", RequestBudget(max_calls=2), transport=lambda _: {"unexpected": "value"},
                    record=lambda *args: repo.record_call(run, *args))
    with pytest.raises(ValueError, match="未返回文字"):
        llm("prompt")
    assert repo.load()["runs"][0]["calls"][0]["response"] == ""


def test_real_sdk_deadline_and_retry_options(monkeypatch):
    options, cancelled = [], []
    async def slow(request):
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
    def client(**kwargs):
        options.append(kwargs)
        return AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(slow)))
    monkeypatch.setattr(api_providers, "AsyncOpenAI", client)
    monkeypatch.setattr(api_providers, "get_config", lambda _: api_providers.APIConfig("hosted-llm", "https://offline/v1", "test", "unused"))
    budget = RequestBudget(max_calls=4, seconds=0.2)
    with pytest.raises(TimeoutError):
        ScriptLLM("hosted-llm", budget)("test")
    assert cancelled and options[0]["max_retries"] == 0 and options[0]["timeout"] <= 0.2
    assert budget.calls == 1


def test_quality_materials_prepare_twelve_cases_without_model_calls(tmp_path, monkeypatch):
    from core.evaluation.short_video_probe import prepare, main, compare
    monkeypatch.setattr(ScriptLLM, "__call__", lambda *_: pytest.fail("paid request forbidden"))
    output = tmp_path / "evaluation"
    cases = prepare(output)
    assert len(cases) == 12
    assert {c["duration_seconds"] for c in cases} == {30, 90}
    with pytest.raises(FileExistsError):
        prepare(output)
    with pytest.raises(SystemExit) as error:
        main(["outline", "--output", str(output)])
    assert error.value.code == 2
    with pytest.raises(ValueError, match="确认大纲"):
        compare(output, "hosted-llm")
    assert not (output / "comparison").exists()


def test_quality_comparison_is_balanced_and_private(tmp_path, monkeypatch, video_outline, video_script, video_review):
    import csv
    import json
    from core.evaluation.short_video_probe import prepare, compare
    from core.short_video.repository import ScriptRepository
    output = tmp_path / "evaluation"
    cases = prepare(output)
    for case in cases:
        repo = ScriptRepository(output / case["id"])
        outline = deepcopy(video_outline)
        for beat in outline["beats"]:
            beat["duration_seconds"] = case["duration_seconds"] / 3
        repo.save_outline(outline)
        repo.confirm_outline()
    calls = []
    def offline(self, prompt):
        calls.append(prompt)
        self.budget.spend()
        if "独立中文短视频剧本审阅者" in prompt:
            response = encode(video_review)
        else:
            script = deepcopy(video_script)
            seconds = 30 if '"duration_seconds": 30,' in prompt else 90
            for scene in script["scenes"]:
                scene["duration_seconds"] = seconds / 3
            response = encode(script)
        self.record(prompt, response, None)
        return response
    monkeypatch.setattr(ScriptLLM, "__call__", offline)
    compare(output, "hosted-llm")
    folder = output / "comparison"
    mapping = json.loads((folder / "manifest.private.json").read_text(encoding="utf-8"))
    assert sum(row["A"] == "candidate" for row in mapping) == 6
    assert len(calls) == 36 and len(list(folder.glob("case_*/A.md"))) == 12
    with (folder / "ratings.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert len(rows) == 13 and all(len(row) == len(rows[0]) for row in rows)
    with pytest.raises(FileExistsError):
        compare(output, "hosted-llm")
    assert len(calls) == 36
