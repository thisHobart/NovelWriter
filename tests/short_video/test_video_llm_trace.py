"""Full console diagnostics stay opt-in, redact credentials and preserve requests."""

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI

from core.common import api_providers
from core.short_video.v2 import llm, llm_trace


def test_trace_is_silent_by_default(monkeypatch, capsys):
    monkeypatch.delenv("NOVELWRITER_LLM_TRACE", raising=False)
    calls = []
    sender = llm.ScriptLLM("offline", transport=lambda prompt: calls.append(prompt) or "回复",
                           trace_context=lambda: pytest.fail("disabled trace evaluated context"))
    assert sender("请求正文") == "回复"
    assert calls == ["请求正文"]
    captured = capsys.readouterr()
    assert captured.err == captured.out == ""


def test_trace_redacts_active_keys_fields_and_endpoint_credentials(monkeypatch, capsys, caplog):
    monkeypatch.setenv("NOVELWRITER_LLM_TRACE", "1")
    monkeypatch.setattr(llm_trace, "_secrets", set())
    llm_trace.trace_event("TEST", {"api_key": "metadata-key"}, {
        "nested": [{"Authorization": "Bearer header-key", "api-key": "field-key"}],
        "text": "known-secret",
    }, secrets=("known-secret",))
    llm_trace.trace_event("FOLLOWUP", {}, "known-secret remains redacted")
    captured = capsys.readouterr().err
    assert "[REDACTED]" in captured
    assert all(key not in captured for key in ["metadata-key", "header-key", "field-key", "known-secret"])
    assert "LLM_TRACE" not in caplog.text
    assert llm_trace.endpoint_label("https://user:password@example.test:444/v1?api_key=secret#fragment") == "https://example.test:444/v1"


@pytest.mark.parametrize("failure", [OSError("console unavailable"), ValueError("closed console"), UnicodeError("bad encoding")])
def test_console_failure_does_not_fail_or_repeat_request(monkeypatch, failure):
    class BrokenConsole:
        def write(self, _):
            raise failure

    monkeypatch.setenv("NOVELWRITER_LLM_TRACE", "1")
    monkeypatch.setattr(llm_trace.sys, "stderr", BrokenConsole())
    calls = []
    sender = llm.ScriptLLM("offline", transport=lambda prompt: calls.append(prompt) or "回复")
    assert sender("请求") == "回复" and calls == ["请求"]


def test_real_sdk_trace_shows_usage_truncation_and_masks_actual_api_key(monkeypatch, capsys):
    monkeypatch.setenv("NOVELWRITER_LLM_TRACE", "1")
    key = "test-active-api-key"
    monkeypatch.setattr(api_providers, "get_config", lambda _: SimpleNamespace(
        base_url="https://example.test/v1", api_key=key, model="trace-model"))
    monkeypatch.setattr(api_providers, "request_options", lambda _: {})
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer " + key
        return httpx.Response(200, json={
            "id": "mock", "object": "chat.completion", "created": 1, "model": "trace-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "partial " + key}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    def client(**kwargs):
        return AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(api_providers, "AsyncOpenAI", client)
    assert llm.ScriptLLM("offline")("exact prompt " + key) == "partial " + key
    captured = capsys.readouterr().err
    assert key not in captured and "[REDACTED]" in captured
    assert '"finish_reason": "length"' in captured and '"total_tokens": 15' in captured
    assert calls[0]["messages"] == [{"role": "user", "content": "exact prompt " + key}]


def test_transport_error_is_traced_without_retry_for_local_failure(monkeypatch, capsys):
    monkeypatch.setenv("NOVELWRITER_LLM_TRACE", "1")
    calls = []

    def send(prompt):
        calls.append(prompt)
        raise ValueError("invalid provider response")

    with pytest.raises(ValueError, match="invalid provider response"):
        llm.ScriptLLM("offline", transport=send)("原始请求")
    captured = capsys.readouterr().err
    assert "LLM_TRACE REQUEST" in captured and "LLM_TRACE API_ERROR" in captured
    assert "invalid provider response" in captured and calls == ["原始请求"]
