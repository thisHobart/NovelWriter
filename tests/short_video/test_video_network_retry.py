"""Bounded network retries are independent of quality revision and checkpoint recovery."""

from copy import deepcopy
from datetime import datetime, timezone
from email.utils import format_datetime
from unittest.mock import Mock

import httpx
import pytest
from openai import APIConnectionError, APIStatusError

from core.common.cancellation import CancelToken, GenerationCancelled
from core.short_video.v2 import llm, runtime, workflow
from core.short_video.v2.feedback import TaskContext, TaskTracker, task_message
from core.short_video.v2.models import WorkspaceError, encode
from test_video_v2_repository import project, novel  # noqa: F401


@pytest.fixture(autouse=True)
def no_real_wait(monkeypatch):
    monkeypatch.setattr(llm.ScriptLLM, "_wait", lambda self, _: self.token.raise_if_cancelled())
    monkeypatch.setattr(workflow, "capture_model_snapshot", lambda _: {"provider": "hosted-llm", "model": "offline"})
    monkeypatch.setattr(runtime, "validate_model_snapshot", lambda _: None)


def status_error(status, headers=None):
    response = httpx.Response(status, headers=headers, request=httpx.Request("POST", "https://example.test"))
    return APIStatusError("original service error", response=response, body={})


@pytest.mark.parametrize("error", [
    APIConnectionError(request=httpx.Request("POST", "https://example.test")),
    httpx.ConnectError("connection failed"), httpx.ReadTimeout("timeout"),
    TimeoutError("timeout"), ConnectionError("reset"),
    *[status_error(code) for code in (408, 409, 429, 500, 502, 503, 504)],
])
def test_transient_error_is_retried_then_succeeds(error):
    transport = Mock(side_effect=[error, "ready"])
    record, before, retry = Mock(), Mock(), Mock()
    assert llm.ScriptLLM("offline", transport=transport, record=record,
                         before_request=before, on_retry=retry)("same prompt") == "ready"
    assert transport.call_args_list[0] == transport.call_args_list[1]
    assert transport.call_count == before.call_count == record.call_count == 2
    assert record.call_args_list[0].args == ("same prompt", "", type(error).__name__)
    assert record.call_args_list[1].args == ("same prompt", "ready", None)
    assert retry.call_args.args[:3] == (error, 1, 2)


@pytest.mark.parametrize("error", [
    *[status_error(code) for code in (400, 401, 403, 404, 422)],
    ValueError("bad config"), WorkspaceError("INVALID_OUTPUT", "bad JSON"),
    PermissionError("disk denied"), OSError("disk full"), RuntimeError("programming error"),
    GenerationCancelled(),
])
def test_permanent_or_local_errors_are_not_retried(error):
    transport = Mock(side_effect=error)
    with pytest.raises(type(error)) as caught:
        llm.ScriptLLM("offline", transport=transport)("prompt")
    assert caught.value is error
    assert transport.call_count == 1


def test_exhaustion_preserves_last_original_error_and_uses_backoff():
    errors = [ConnectionError(f"failure {i}") for i in range(3)]
    transport = Mock(side_effect=errors)
    retry = Mock()
    with pytest.raises(ConnectionError) as caught:
        llm.ScriptLLM("offline", transport=transport, on_retry=retry)("prompt")
    assert caught.value is errors[-1]
    assert transport.call_count == 3 and retry.call_count == 2
    assert 1 <= retry.call_args_list[0].args[3] <= 1.25
    assert 2 <= retry.call_args_list[1].args[3] <= 2.25


@pytest.mark.parametrize("headers,expected", [
    ({"Retry-After": "3"}, 3), ({"retry-after-ms": "1500"}, 1.5),
    ({"Retry-After": "0"}, 0), ({"Retry-After": "90"}, None),
    ({"x-should-retry": "false"}, None),
])
def test_server_delay_and_no_retry_header_are_respected(headers, expected):
    assert llm.retry_delay(status_error(429, headers), 0) == expected


def test_retry_after_http_date_and_invalid_header(monkeypatch):
    monkeypatch.setattr(llm.time, "time", lambda: 1000)
    date = format_datetime(datetime.fromtimestamp(1005, timezone.utc), usegmt=True)
    assert llm.retry_delay(status_error(429, {"Retry-After": date}), 0) == 5
    assert 1 <= llm.retry_delay(status_error(503, {"Retry-After": "bad"}), 0) <= 1.25


def test_stopping_during_backoff_prevents_next_request(monkeypatch):
    # Exercise the actual Event.wait path, without a sleep or paid request.
    monkeypatch.undo()
    token = CancelToken()
    transport = Mock(side_effect=ConnectionError("offline"))
    with pytest.raises(GenerationCancelled):
        llm.ScriptLLM("offline", token=token, transport=transport,
                      on_retry=lambda *_: token.cancel())("prompt")
    assert transport.call_count == 1


@pytest.mark.parametrize("record_failure", [OSError("disk full"), TimeoutError("storage timed out")])
def test_record_failure_never_resends_received_response(record_failure):
    transport = Mock(return_value="already received")
    with pytest.raises(llm.RecordingError) as caught:
        llm.ScriptLLM("offline", transport=transport, record=Mock(side_effect=record_failure))("prompt")
    assert caught.value.__cause__ is record_failure
    assert transport.call_count == 1


def test_request_admission_hook_failure_is_not_retried():
    transport = Mock()
    error = TimeoutError("project storage timed out")
    with pytest.raises(TimeoutError) as caught:
        llm.ScriptLLM("offline", transport=transport, before_request=Mock(side_effect=error))("prompt")
    assert caught.value is error and transport.call_count == 0


def test_generation_and_review_retry_without_consuming_quality_revision(project):
    original = novel(project)
    replies = iter([ConnectionError("first connection"), encode(original["content"]),
                    status_error(503), '{"issues":[]}'])
    calls, progress = [], []

    def send(prompt):
        calls.append(prompt)
        value = next(replies)
        if isinstance(value, Exception):
            raise value
        return value

    service = workflow.WorkflowService(project, transport=send)
    result = service.execute(service.prepare("novel_copy"), report=lambda text, _: progress.append(text))
    assert result["retries_used"] == 0 and result["review_status"] == "passed"
    assert calls[0] == calls[1] and calls[2] == calls[3]
    state = project.load()
    entries = state["runs"][-1]["calls"]
    assert [e["state"] for e in entries] == ["unknown", "received", "failed", "received"]
    assert [e["network_attempt"] for e in entries] == [0, 1, 0, 1]
    assert entries[0]["step_id"] == entries[1]["step_id"]
    assert entries[2]["step_id"] == entries[3]["step_id"]
    assert next(iter(state["tasks"].values()))["calls_used"] == 4
    assert sum("网络重试1/2" in text for text in progress) == 2


def test_review_exhaustion_keeps_candidate_and_manual_resume_skips_generation(project):
    original = novel(project)
    calls = []

    def send(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return encode(original["content"])
        raise ConnectionError("review failed")

    command = workflow.WorkflowService(project).prepare("novel_copy")
    work = workflow.WorkspaceWork(project, command, transport=send)
    with pytest.raises(ConnectionError):
        work(lambda *_: None)
    assert len(calls) == 4 and work.task_context.network_retries == 2
    assert "已自动重试2次" in task_message(work.task_context, error=ConnectionError())[2]
    run = project.load()["runs"][-1]
    assert run["checkpoint"]["step"] == "review" and run["feedback"]["network_retries"] == 2
    assert project.current("novel_copy")["id"] == original["id"]
    resumed = Mock(return_value='{"issues":[]}')
    result = runtime.ResumeWork(project, command["request_id"], transport=resumed)(lambda *_: None)
    assert resumed.call_count == 1
    assert result["version_id"] == run["checkpoint"]["candidate"]


def test_input_change_during_backoff_prevents_retry(project, monkeypatch):
    original = novel(project)
    command = workflow.WorkflowService(project).prepare("novel_copy")
    transport = Mock(side_effect=ConnectionError("offline"))

    def wait(self, delay):
        config = deepcopy(project.current("config")["content"])
        config["tone"] = "changed during retry"
        project.save("config", config, {}, expected_revision=project.load()["revision"])

    monkeypatch.setattr(llm.ScriptLLM, "_wait", wait)
    with pytest.raises(WorkspaceError, match="变化"):
        workflow.WorkflowService(project, transport=transport).execute(command)
    assert transport.call_count == 1


def test_retry_feedback_preserves_original_phase_and_partial_progress():
    tracker = TaskTracker("资产提示词生成", target="人物")
    tracker.update_context(phase="自动审阅", completed=2, total=5)
    report = Mock()
    tracker.report_network_retry(ConnectionError(), 1, 2, 1.5, report)
    assert tracker.task_context.phase == "自动审阅" and tracker.task_context.completed == 2
    assert "网络重试1/2" in report.call_args.args[0]


def test_real_sdk_transport_uses_only_the_shared_retry_layer(monkeypatch):
    from types import SimpleNamespace
    from openai import AsyncOpenAI
    from core.common import api_providers

    calls, options = [], []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("temporary connection failure", request=request)
        if len(calls) == 2:
            return httpx.Response(503, json={"error": {"message": "temporarily unavailable"}})
        return httpx.Response(200, json={
            "id": "mock-completion", "object": "chat.completion", "created": 1, "model": "offline",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ready"}, "finish_reason": "stop"}],
        })

    def client(**kwargs):
        options.append(kwargs["max_retries"])
        return AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(api_providers, "get_config", lambda _: SimpleNamespace(
        api_key="offline-test", base_url="https://example.test/v1", model="offline"))
    monkeypatch.setattr(api_providers, "request_options", lambda _: {})
    monkeypatch.setattr(api_providers, "AsyncOpenAI", client)
    record = Mock()
    assert llm.ScriptLLM("offline", record=record)("prompt") == "ready"
    assert len(calls) == record.call_count == 3
    assert options == [0, 0, 0]
