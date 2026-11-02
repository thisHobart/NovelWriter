from copy import deepcopy
import pytest

from core.generation.cancellation import CancelToken, GenerationCancelled
from core.short_video.models import encode
from core.short_video.pipeline import ScriptPipeline
from core.short_video.repository import ScriptRepository


def ready(tmp_path, requirements, outline):
    repo = ScriptRepository(tmp_path)
    repo.save_requirements(requirements)
    repo.save_outline(outline)
    repo.confirm_outline()
    return repo


def transport_for(*responses):
    responses = iter(responses)
    def call(prompt):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return encode(response) if isinstance(response, dict) else response
    return call


def test_outline_then_confirm_then_script(tmp_path, video_requirements, video_outline, video_script, video_review):
    repo = ScriptRepository(tmp_path)
    repo.save_requirements(video_requirements)
    result = ScriptPipeline(tmp_path, transport=transport_for(video_outline)).run("outline")
    assert result["calls"] == 1
    with pytest.raises(ValueError, match="确认"):
        ScriptPipeline(tmp_path, transport=lambda _: pytest.fail("must not call")).run("script")
    repo.confirm_outline()
    result = ScriptPipeline(tmp_path, transport=transport_for(video_script, video_review)).run("script")
    assert result["status"] == "reviewed" and result["calls"] == 2
    assert not repo.version()["finalized"]


def test_one_whole_script_repair_with_evidence(tmp_path, video_requirements, video_outline, video_script, video_review):
    repo = ready(tmp_path, video_requirements, video_outline)
    bad_review = deepcopy(video_review)
    bad_review["scores"]["details"] = 2
    bad_review["issues"] = [{"code": "DETAIL", "severity": "soft", "quote": video_script["scenes"][0]["action"],
                              "suggestion": "增加手指触碰旧邮戳的可见动作", "target": "script"}]
    revised = deepcopy(video_script)
    revised["scenes"][0]["action"] += "她用拇指擦去邮戳上的灰。"
    result = ScriptPipeline(tmp_path, transport=transport_for(video_script, bad_review, revised, video_review)).run("script")
    assert result["calls"] == 4 and result["status"] == "reviewed"
    assert len(repo.load()["versions"]) == 2
    prompt = repo.load()["runs"][-1]["calls"][2]["prompt"]
    assert "完整当前稿" in prompt and "爸，你还记得" in prompt and "增加手指" in prompt
    assert '"code": "DETAIL"' in prompt and '"quote":' in prompt


def test_failed_repair_keeps_better_version(tmp_path, video_requirements, video_outline, video_script, video_review):
    repo = ready(tmp_path, video_requirements, video_outline)
    review = deepcopy(video_review)
    review["scores"]["language"] = 2
    review["issues"] = [{"code": "LANG", "severity": "soft", "quote": "爸，你还记得这个地址吗？",
                          "suggestion": "改得更口语", "target": "script"}]
    result = ScriptPipeline(tmp_path, transport=transport_for(video_script, review, "not-json")).run("script")
    assert result["status"] == "needs_changes"
    assert repo.version()["content"] == video_script
    assert len(repo.load()["runs"][-1]["calls"]) == 3


@pytest.mark.parametrize("review", ["bad-json", RuntimeError("secret-key-not-for-log")])
def test_unavailable_review_is_unchecked_and_no_secret_persisted(tmp_path, video_requirements, video_outline, video_script, review):
    repo = ready(tmp_path, video_requirements, video_outline)
    result = ScriptPipeline(tmp_path, transport=transport_for(video_script, review)).run("script")
    assert result["status"] == "unchecked" and repo.version()["review"] is None
    assert "secret-key-not-for-log" not in repo.path.read_text(encoding="utf-8")


def test_cancel_after_model_return_preserves_candidate(tmp_path, video_requirements, video_outline, video_script):
    repo = ready(tmp_path, video_requirements, video_outline)
    token = CancelToken()
    def transport(prompt):
        token.cancel()
        return encode(video_script)
    with pytest.raises(GenerationCancelled):
        ScriptPipeline(tmp_path, token=token, transport=transport).run("script")
    assert repo.version()["content"] == video_script
    assert repo.load()["runs"][-1]["status"] == "cancelled"
    assert len(repo.load()["runs"][-1]["calls"]) == 1


def test_empty_or_invalid_outline_has_two_requests_and_retains_raw(tmp_path, video_requirements):
    repo = ScriptRepository(tmp_path)
    repo.save_requirements(video_requirements)
    with pytest.raises(RuntimeError):
        ScriptPipeline(tmp_path, transport=transport_for("not-json", "")).run("outline")
    run = repo.load()["runs"][-1]
    assert len(run["calls"]) == 2 and run["calls"][0]["response"] == "not-json"
    assert repo.load()["outline"] is None


def test_snapshot_and_changed_requirements_make_new_script_stale(tmp_path, video_requirements, video_outline, video_script, video_review):
    repo = ready(tmp_path, video_requirements, video_outline)
    responses = iter([video_script, video_review])
    def transport(prompt):
        repo.save_requirements(dict(video_requirements, tone="已改变"))
        return encode(next(responses))
    ScriptPipeline(tmp_path, transport=transport).run("script")
    assert repo.version()["source"] != repo.source(repo.load())
    assert repo.load()["runs"][-1]["snapshot"]["requirements"]["tone"] == video_requirements["tone"]


def test_new_generation_does_not_replace_authors_selected_draft(tmp_path, video_requirements, video_outline, video_script, video_review):
    repo = ready(tmp_path, video_requirements, video_outline)
    manual = repo.save_manual(video_script, repo.source(repo.load()))
    result = ScriptPipeline(tmp_path, transport=transport_for(video_script, video_review)).run("script")
    assert result["version_id"] != manual["id"]
    assert repo.version()["id"] == manual["id"]


def test_outline_fault_does_not_automatically_change_approved_outline(tmp_path, video_requirements, video_outline, video_script, video_review):
    repo = ready(tmp_path, video_requirements, video_outline)
    review = deepcopy(video_review)
    review["issues"] = [{"code": "PLAN", "severity": "hard", "quote": "她放下剪刀",
                         "suggestion": "先修改大纲", "target": "outline"}]
    result = ScriptPipeline(tmp_path, transport=transport_for(video_script, review)).run("script")
    assert result["calls"] == 2 and result["status"] == "needs_changes"
    assert repo.load()["outline"]["content"] == video_outline and repo.outline_ready(repo.load())


def test_submitted_work_rejects_changed_source_before_any_call(tmp_path, video_requirements, video_outline, monkeypatch):
    from core.short_video.pipeline import ScriptWork
    from core.short_video.llm import ScriptLLM
    repo = ready(tmp_path, video_requirements, video_outline)
    work = ScriptWork(tmp_path, "script", "hosted-llm")
    repo.save_requirements(dict(video_requirements, tone="改变了"))
    monkeypatch.setattr(ScriptLLM, "__call__", lambda *_: pytest.fail("no request allowed"))
    with pytest.raises(ValueError, match="提交任务后"):
        work(lambda *_: None)
    assert not repo.load()["runs"]


def test_worse_repair_cannot_displace_better_candidate(tmp_path, video_requirements, video_outline, video_script, video_review):
    repo = ready(tmp_path, video_requirements, video_outline)
    first = deepcopy(video_review)
    first["scores"]["language"] = 2
    first["issues"] = [{"code": "LANG", "severity": "soft", "quote": "爸，你还记得这个地址吗？",
                        "suggestion": "缩短对白", "target": "script"}]
    worse = deepcopy(video_review)
    worse["issues"] = [{"code": "CAUSE", "severity": "hard", "quote": "她放下剪刀",
                        "suggestion": "补充动作原因", "target": "script"}]
    result = ScriptPipeline(tmp_path, transport=transport_for(video_script, first, video_script, worse)).run("script")
    assert result["calls"] == 4
    assert repo.version()["id"] == repo.load()["versions"][0]["id"]


def test_invalid_generation_retry_and_review_stay_within_four_calls(tmp_path, video_requirements, video_outline, video_script, video_review):
    repo = ready(tmp_path, video_requirements, video_outline)
    review = deepcopy(video_review)
    review["scores"]["details"] = 2
    review["issues"] = [{"code": "DETAIL", "severity": "soft", "quote": "把旧邮戳转向门口的灯",
                         "suggestion": "增加手部细节", "target": "script"}]
    result = ScriptPipeline(tmp_path, transport=transport_for("", video_script, review)).run("script")
    assert result["calls"] == 3 and result["status"] == "needs_changes"
    assert len(repo.load()["runs"][-1]["calls"]) == 3  # Cannot afford both repair and re-review.


def test_recording_failure_stops_paid_requests(tmp_path, monkeypatch, video_requirements, video_outline, video_script):
    repo = ready(tmp_path, video_requirements, video_outline)
    calls = []
    def fail(*args):
        raise OSError("storage unavailable")
    monkeypatch.setattr(ScriptRepository, "record_call", fail)
    def transport(prompt):
        calls.append(prompt)
        return encode(video_script)
    with pytest.raises(RuntimeError):
        ScriptPipeline(tmp_path, transport=transport).run("script")
    assert len(calls) == 1
    assert not repo.load()["versions"]


def test_review_recording_failure_never_starts_a_repair(tmp_path, monkeypatch, video_requirements,
                                                      video_outline, video_script, video_review):
    repo = ready(tmp_path, video_requirements, video_outline)
    video_script["scenes"][0]["duration_seconds"] = 90  # An actionable local fault would otherwise cause a repair.
    original = ScriptRepository.record_call
    records = []
    def record(self, *args):
        records.append(args)
        if len(records) == 2:
            raise OSError("disk failure")
        return original(self, *args)
    monkeypatch.setattr(ScriptRepository, "record_call", record)
    result = ScriptPipeline(tmp_path, transport=transport_for(video_script, video_review)).run("script")
    assert len(records) == 2 and result["calls"] == 2
    assert repo.version()["status"] == "unchecked"


def test_revision_reserves_time_for_both_requests(tmp_path, video_requirements, video_outline,
                                                video_script, video_review):
    from core.short_video.llm import RequestBudget
    repo = ready(tmp_path, video_requirements, video_outline)
    clock = [0.0]
    video_review["issues"] = [{"code": "CAUSE", "severity": "hard", "quote": "她放下剪刀",
                               "suggestion": "补充放下剪刀的可见原因", "target": "script"}]
    responses = iter([video_script, video_review])
    def transport(prompt):
        clock[0] += 31  # Only 238 seconds left after draft and review.
        return encode(next(responses))
    result = ScriptPipeline(tmp_path, transport=transport,
        budget_factory=lambda **args: RequestBudget(**args, clock=lambda: clock[0])).run("script")
    assert result["calls"] == 2 and result["status"] == "needs_changes"
    assert repo.version()["issues"][0]["code"] == "CAUSE"


def test_total_deadline_prevents_review_and_retains_draft(tmp_path, video_requirements,
                                                       video_outline, video_script):
    from core.short_video.llm import RequestBudget
    repo = ready(tmp_path, video_requirements, video_outline)
    clock = [0.0]
    calls = []
    def transport(prompt):
        calls.append(prompt)
        clock[0] = 300
        return encode(video_script)
    result = ScriptPipeline(tmp_path, transport=transport,
        budget_factory=lambda **args: RequestBudget(**args, clock=lambda: clock[0])).run("script")
    assert len(calls) == 1 and result["calls"] == 1 and result["status"] == "unchecked"
    assert repo.version()["content"] == video_script


def test_cancellation_before_request_never_calls_model(tmp_path, video_requirements, video_outline):
    repo = ready(tmp_path, video_requirements, video_outline)
    token = CancelToken()
    token.cancel()
    with pytest.raises(GenerationCancelled):
        ScriptPipeline(tmp_path, token=token, transport=lambda _: pytest.fail("cancelled request")).run("script")
    assert repo.load()["runs"][-1]["status"] == "cancelled"
    assert not repo.load()["runs"][-1]["calls"]


def test_cancel_during_review_retains_verdict_without_revision(tmp_path, video_requirements, video_outline,
                                                            video_script, video_review):
    repo = ready(tmp_path, video_requirements, video_outline)
    token = CancelToken()
    responses = iter([video_script, video_review])
    calls = []
    def transport(prompt):
        calls.append(prompt)
        if len(calls) == 2:
            token.cancel()
        return encode(next(responses))
    with pytest.raises(GenerationCancelled):
        ScriptPipeline(tmp_path, token=token, transport=transport).run("script")
    assert len(calls) == 2 and repo.version()["status"] == "reviewed"
    assert repo.load()["runs"][-1]["status"] == "cancelled"


def test_finalized_model_version_is_not_reselected_by_generation(tmp_path, video_requirements, video_outline,
                                                               video_script, video_review):
    repo = ready(tmp_path, video_requirements, video_outline)
    first = ScriptPipeline(tmp_path, transport=transport_for(video_script, video_review)).run("script")
    repo.finalize(first["version_id"])
    second = ScriptPipeline(tmp_path, transport=transport_for(video_script, video_review)).run("script")
    assert second["version_id"] != first["version_id"]
    assert repo.version()["id"] == first["version_id"] and repo.version()["finalized"]


def test_proactive_revision_preserves_original_and_passes_full_inputs(tmp_path, video_requirements,
                                                                   video_outline, video_script, video_review):
    repo = ready(tmp_path, video_requirements, video_outline)
    original = repo.save_manual(video_script, repo.source(repo.load()))
    revised = deepcopy(video_script)
    revised["scenes"][0]["action"] += "她抹掉邮戳上的水珠。"
    result = ScriptPipeline(tmp_path, transport=transport_for(revised, video_review)).run(
        "revise", version_id=original["id"], asks=["增加水珠的细节"])
    assert result["status"] == "reviewed"
    assert repo.version()["id"] == original["id"]
    assert repo.version(original["id"])["content"] == video_script
    assert repo.version(result["version_id"])["content"] == revised
    prompt = repo.load()["runs"][-1]["calls"][0]["prompt"]
    assert "增加水珠的细节" in prompt and "完整当前稿" in prompt and "已确认大纲" in prompt


def test_outline_edit_retains_old_script_but_requires_new_confirmation(tmp_path, video_requirements,
                                                                    video_outline, video_script):
    repo = ready(tmp_path, video_requirements, video_outline)
    old = repo.save_manual(video_script, repo.source(repo.load()))
    changed = deepcopy(video_outline)
    changed["ending"] = "她将包裹交给父亲辨认。"
    repo.save_outline(changed)
    assert repo.version()["id"] == old["id"]
    assert repo.version()["content"] == video_script
    assert repo.version()["source"] != repo.source(repo.load())
    with pytest.raises(ValueError, match="确认"):
        ScriptPipeline(tmp_path, transport=lambda _: pytest.fail("unconfirmed outline")).run("script")
    repo.confirm_outline()
    assert repo.outline_ready(repo.load())
    assert repo.version()["source"] != repo.source(repo.load())
    assert not repo.load()["runs"]
