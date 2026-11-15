"""Independent review configuration, one revision, and durable final feedback."""

from copy import deepcopy
import json

import httpx
import pytest
from openai import AsyncOpenAI

from core.generation import api_providers, api_settings
from core.short_video.v2 import creation, llm, models, runtime, workflow
from core.short_video.v2.models import WorkspaceError, encode
from core.short_video.v2.project_service import ProjectService
from core.short_video.v2.repository import ProjectRepository
from test_video_v2_repository import ARTICLE, novel


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(api_settings, "settings_file", lambda: tmp_path / "llm_settings.json")
    api_settings.save_provider("hosted-llm", "https://generate.test/v1", "generation-key", "generator")
    api_settings.save_provider("hosted-llm", "https://review.test/v1", "review-key", "reviewer", purpose="review")
    repository = ProjectRepository(tmp_path / "project")
    ProjectService(repository).import_text(ARTICLE, expected_revision=0)
    novel(repository)
    return repository


def issue(explanation="动作节奏需要调整"):
    return {"rule_id": "TEXT-001", "severity": "warning", "target_path": "opening.text",
            "explanation": explanation, "suggestion": "保留核心信息，简化开头", "quote": None, "source_refs": []}


def test_review_configuration_is_independent_and_never_falls_back(configured, monkeypatch):
    original = api_providers.get_config("hosted-llm")
    review = api_settings.get_review_config()
    assert (review.base_url, review.model, review.api_key) == ("https://review.test/v1", "reviewer", "review-key")
    api_settings.save_provider("qwen", api_settings.DEFAULT_URLS["qwen"], "qwen-review-key", "reviewer-2", purpose="review")
    assert api_providers.get_config("hosted-llm") == original
    assert api_settings.review_provider() == "qwen"
    assert api_settings.get_review_config().model == "reviewer-2"
    assert api_settings.form_values("hosted-llm", purpose="review")["api_key"] == "review-key"
    with pytest.raises(ValueError, match="校验模型服务已变化"):
        api_settings.get_review_config("hosted-llm")
    data = json.loads(api_settings.settings_file().read_text(encoding="utf-8"))
    data.pop("review")
    api_settings.settings_file().write_text(encode(data), encoding="utf-8")
    monkeypatch.setenv("HOSTED_LLM_API_KEY", "environment-generation-key")
    assert api_settings.form_values("hosted-llm", purpose="review")["api_key"] == ""
    with pytest.raises(ValueError, match="配置校验模型"):
        api_settings.get_review_config()


@pytest.mark.parametrize("first_pass,final_pass", [(True, True), (False, True), (False, False)])
def test_real_sdk_routes_generation_and_review_and_stops_after_one_revision(configured, monkeypatch, first_pass, final_pass):
    repository = configured
    content = repository.current("novel_copy")["content"]
    calls = []
    generated, reviewed = [], []

    def handler(request):
        body = json.loads(request.content)
        calls.append((request.url.host, body["model"], request.headers["authorization"]))
        if body["model"] == "generator":
            candidate = deepcopy(content)
            candidate["opening"]["text"] += f"第{len(generated) + 1}稿。"
            generated.append(candidate)
            if len(generated) == 2:
                assert "保留核心信息，简化开头" in body["messages"][0]["content"]
            result = candidate
        else:
            assert generated[-1]["opening"]["text"] in body["messages"][0]["content"]
            passed = first_pass if not reviewed else final_pass
            result = {"issues": [] if passed else [issue(f"第{len(reviewed) + 1}次检查仍有问题")]}
            reviewed.append(result)
        return httpx.Response(200, json={
            "id": "mock", "object": "chat.completion", "created": 1, "model": body["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": encode(result)}, "finish_reason": "stop"}],
        })

    def client(**kwargs):
        assert kwargs["max_retries"] == 0
        return AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(api_providers, "AsyncOpenAI", client)
    service = workflow.WorkflowService(repository)
    result = service.execute(service.prepare("novel_copy"))
    expected = [("generate.test", "generator", "Bearer generation-key"), ("review.test", "reviewer", "Bearer review-key")]
    assert calls == expected * (1 if first_pass else 2)
    assert result["retries_used"] == (0 if first_pass else 1)
    assert repository.current("novel_copy")["content"] == generated[-1]
    run = repository.load()["runs"][-1]
    assert run["review_result"]["candidate_id"] == result["version_id"]
    assert run["review_result"]["passed"] == (first_pass or final_pass)
    assert [{k: v for k, v in i.items() if k not in {"id", "disposition"}}
            for i in run["review_result"]["issues"]] == reviewed[-1]["issues"]
    assert run["snapshot"]["review_model_snapshot"]["model"] == "reviewer"
    assert [c["purpose"] for c in run["calls"]] == ["generation", "review"] * len(generated)
    assert [c["model_snapshot"]["model"] for c in run["calls"]] == ["generator", "reviewer"] * len(generated)
    assert "generation-key" not in encode(repository.load()) and "review-key" not in encode(repository.load())


def test_missing_review_configuration_stops_before_generation(configured):
    data = json.loads(api_settings.settings_file().read_text(encoding="utf-8"))
    data.pop("review")
    api_settings.settings_file().write_text(encode(data), encoding="utf-8")
    calls = []
    service = workflow.WorkflowService(configured, transport=lambda p: calls.append(p))
    with pytest.raises(WorkspaceError) as caught:
        service.prepare("novel_copy")
    assert caught.value.code == "REVIEW_CONFIGURATION_MISSING"
    assert not calls and not configured.load()["tasks"]


@pytest.mark.parametrize("change", ["model", "base_url", "provider"])
def test_review_configuration_change_blocks_recovery_until_restored(configured, monkeypatch, change):
    content = configured.current("novel_copy")["content"]
    command = workflow.WorkflowService(configured).prepare("novel_copy")
    monkeypatch.setattr(llm, "NETWORK_MAX_RETRIES", 0)
    replies = iter([encode(content), ConnectionError("review connection failed")])

    def send(_):
        value = next(replies)
        if isinstance(value, Exception):
            raise value
        return value

    with pytest.raises(ConnectionError):
        workflow.WorkflowService(configured, transport=send).execute(command)
    assert configured.load()["runs"][-1]["checkpoint"]["step"] == "review"
    values = api_settings.form_values("hosted-llm", purpose="review")
    provider = "hosted-llm"
    if change == "provider":
        provider = "qwen"
        values["base_url"] = api_settings.DEFAULT_URLS["qwen"]
    else:
        values[change] = "reviewer-changed" if change == "model" else "https://other.test/v1"
    api_settings.save_provider(provider, **values, purpose="review")
    calls = []
    with pytest.raises(WorkspaceError) as caught:
        runtime.ResumeWork(configured, command["request_id"], transport=lambda p: calls.append(p))(lambda *_: None)
    assert caught.value.code == "REVIEW_CONFIGURATION_CHANGED"
    assert calls == []
    api_settings.save_provider("hosted-llm", "https://review.test/v1", "new-review-key", "reviewer", purpose="review")
    result = runtime.ResumeWork(configured, command["request_id"], transport=lambda p: calls.append(p) or '{"issues":[]}')(lambda *_: None)
    assert result["review_status"] == "passed"
    assert len(calls) == 1 and calls[0].startswith("自动审阅")


def test_second_review_network_failure_resumes_review_without_third_generation(configured, monkeypatch):
    content = configured.current("novel_copy")["content"]
    command = workflow.WorkflowService(configured).prepare("novel_copy")
    purposes = []
    responses = iter([encode(content), encode({"issues": [issue()]}), encode(content), ConnectionError("second review failed")])
    monkeypatch.setattr(llm, "NETWORK_MAX_RETRIES", 0)

    def request(self, _):
        purposes.append(self.purpose)
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(llm.ScriptLLM, "_request", request)
    with pytest.raises(ConnectionError):
        workflow.WorkflowService(configured).execute(command)
    assert purposes == ["generation", "review", "generation", "review"]
    assert configured.load()["runs"][-1]["checkpoint"]["revision"] == 1
    monkeypatch.setattr(llm.ScriptLLM, "_request", lambda self, _: purposes.append(self.purpose) or encode({"issues": [issue()]}))
    result = runtime.ResumeWork(configured, command["request_id"])(lambda *_: None)
    assert result["review_status"] == "issues" and result["retries_used"] == 1
    assert purposes == ["generation", "review", "generation", "review", "review"]
    assert configured.load()["runs"][-1]["review_result"]["issue_count"] == 1


def test_storyboard_composition_freezes_same_review_model(configured):
    # A catalog+storyboard parent and both children must bind the same reviewer.
    configured.save("video_copy", {"blocks": [
        {"id": "b1", "role": "opening", "narration": "井口有声音", "visible_action": "靠近井口", "dialogue": []},
        {"id": "b2", "role": "cliffhanger", "narration": "是谁？", "visible_action": "", "dialogue": []},
    ]}, {"novel_copy": configured.current("novel_copy")["id"]}, expected_revision=configured.load()["revision"])
    command = creation.prepare_storyboard_task(configured, "hosted-llm")
    assert command["review_model_snapshot"] == command["first_command"]["review_model_snapshot"]
    assert command["review_model_snapshot"] == models.capture_review_model_snapshot()


def test_quality_revision_limit_cannot_exceed_one(configured):
    with pytest.raises(WorkspaceError, match="质量修订次数"):
        workflow.WorkflowService(configured, max_retries=2)
