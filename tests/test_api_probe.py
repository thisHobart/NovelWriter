"""Exercise minimal probes through the real SDK with offline transports."""
import json

import httpx
import pytest
from openai import OpenAI

from core.generation import api_providers as api
from core.generation.api_probe import PROBE_MAX_TOKENS, PROBE_TIMEOUT, probe_connection


def _response(**overrides):
    body = {
        "id": "test", "object": "chat.completion", "created": 0, "model": "returned-model",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "OK"}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8},
    }
    body.update(overrides)
    return httpx.Response(200, json=body)


@pytest.mark.parametrize("provider,extra", [
    ("hosted-llm", {"chat_template_kwargs": {"enable_thinking": False}}),
    ("deepseek", {"thinking": {"type": "disabled"}}),
    ("qwen", {"enable_thinking": False}),
])
def test_probe_uses_explicit_config_and_small_budget_once(monkeypatch, provider, extra):
    requests, options = [], []
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://wrong.example")

    def respond(request):
        requests.append(request)
        return _response()

    def client(**kwargs):
        options.append(kwargs)
        return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(respond)))

    def no_lookup(*_):
        raise AssertionError("The probe must not read saved configuration")

    monkeypatch.setattr(api, "OpenAI", client)
    monkeypatch.setattr(api, "get_config", no_lookup)
    config = api.APIConfig(provider, "https://draft.example/v1", "draft-model", "draft-key")
    result = probe_connection(config)
    assert len(requests) == 1
    assert str(requests[0].url) == "https://draft.example/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer draft-key"
    body = json.loads(requests[0].content)
    assert body["model"] == "draft-model"
    assert body["messages"] == [{"role": "user", "content": "Reply OK"}]
    assert body["max_tokens"] == PROBE_MAX_TOKENS == 2
    assert all(body[key] == value for key, value in extra.items())
    assert options[0]["max_retries"] == 0
    assert options[0]["timeout"] == PROBE_TIMEOUT
    assert result.total_tokens == 8
    assert result.returned_model == "returned-model"
    assert result.elapsed >= 0


@pytest.mark.parametrize("status,expected", [
    (400, "请求参数"), (401, "密钥验证"), (402, "余额"), (403, "访问权限"),
    (404, "接口或模型"), (422, "参数不被支持"), (429, "请求受限"), (500, "服务处理失败"),
])
def test_http_errors_are_useful_safe_and_never_retried(monkeypatch, status, expected):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": "rejected sensitive-test-key"}})

    monkeypatch.setattr(api, "OpenAI", lambda **kwargs: OpenAI(
        **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ))
    with pytest.raises(ValueError, match=expected) as error:
        probe_connection(api.APIConfig("qwen", "https://test.example/v1", "model", "sensitive-test-key"))
    assert f"HTTP {status}" in str(error.value)
    assert "sensitive-test-key" not in str(error.value)
    assert len(requests) == 1


@pytest.mark.parametrize("failure,expected", [(httpx.ReadTimeout, "请求超时"), (httpx.ConnectError, "无法连接")])
def test_network_errors_without_retry(monkeypatch, failure, expected):
    requests = []

    def respond(request):
        requests.append(request)
        raise failure("private-key", request=request)

    monkeypatch.setattr(api, "OpenAI", lambda **kwargs: OpenAI(
        **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ))
    with pytest.raises(ValueError, match=expected) as error:
        probe_connection(api.APIConfig("hosted-llm", "https://test.example/v1", "model", "private-key"))
    assert "private-key" not in str(error.value)
    assert len(requests) == 1


@pytest.mark.parametrize("choices", [[], [{"index": 0, "message": {"role": "assistant", "content": ""}}]])
def test_empty_responses_are_not_reported_as_success(monkeypatch, choices):
    monkeypatch.setattr(api, "OpenAI", lambda **kwargs: OpenAI(
        **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(lambda _: _response(choices=choices))),
    ))
    with pytest.raises(ValueError, match="未返回文本"):
        probe_connection(api.APIConfig("deepseek", "https://test.example/v1", "model", "key"))


def test_usage_is_optional(monkeypatch):
    monkeypatch.setattr(api, "OpenAI", lambda **kwargs: OpenAI(
        **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(lambda _: _response(usage=None))),
    ))
    assert probe_connection(api.APIConfig("qwen", "https://test.example/v1", "model", "key")).total_tokens is None
