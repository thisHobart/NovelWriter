"""Exercise real SDK requests against a fake transport, without network calls."""
import json

import httpx
import pytest
from openai import OpenAI

from core.generation import api_providers as api
from core.generation import ai_helper, api_settings


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(api_settings, "settings_file", lambda: tmp_path / "llm_settings.json")
    for name in (
        "HOSTED_LLM_BASE_URL", "HOSTED_LLM_URL", "HOSTED_LLM_PORT",
        "HOSTED_LLM_API_KEY", "HOSTED_LLM_MODEL", "DEEPSEEK_API_KEY",
        "DEEPSEEK_MODEL", "DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL", "QWEN_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("provider,url,actual_model,key,extra", [
    ("hosted-llm", "https://local.example/v1", "local-model", "local-key",
     {"chat_template_kwargs": {"enable_thinking": False}}),
    ("deepseek", "https://api.deepseek.com/v1", "deepseek-flash", "deepseek-key",
     {"thinking": {"type": "disabled"}}),
    ("qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus", "qwen-key",
     {"enable_thinking": False}),
])
def test_requests_use_selected_service_and_own_credentials(
    monkeypatch, provider, url, actual_model, key, extra,
):
    for name, value in {
        "HOSTED_LLM_BASE_URL": "https://local.example/v1",
        "HOSTED_LLM_MODEL": "local-model", "HOSTED_LLM_API_KEY": "local-key",
        "DEEPSEEK_API_KEY": "deepseek-key", "DASHSCOPE_API_KEY": "qwen-key",
        "OPENAI_BASE_URL": "https://wrong.example/v1", "OPENAI_API_KEY": "wrong-key",
    }.items():
        monkeypatch.setenv(name, value)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 0,
            "model": actual_model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "正确服务的回复"}}],
        })

    monkeypatch.setattr(api, "OpenAI", lambda **kwargs: OpenAI(
        **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ))
    ai_helper.set_backend("api", provider)
    assert ai_helper.send_prompt("写一段小说") == "正确服务的回复"
    conversation = [{"role": "user", "content": "初稿"},
                    {"role": "assistant", "content": "上一稿"},
                    {"role": "user", "content": "请修改"}]
    assert ai_helper.send_conversation(conversation) == "正确服务的回复"
    assert len(requests) == 2
    for request in requests:
        assert str(request.url) == url + "/chat/completions"
        assert request.headers["authorization"] == "Bearer " + key
        body = json.loads(request.content)
        assert body["model"] == actual_model
        assert body["max_tokens"] == 16384
        for name, value in extra.items():
            assert body[name] == value
    assert json.loads(requests[-1].content)["messages"][1:] == conversation


def test_hosted_accepts_legacy_split_address_and_no_auth(monkeypatch):
    monkeypatch.setenv("HOSTED_LLM_URL", "127.0.0.1")
    monkeypatch.setenv("HOSTED_LLM_PORT", "8000")
    monkeypatch.setenv("HOSTED_LLM_MODEL", "my-model")
    config = api.get_config("hosted-llm")
    assert config.base_url == "http://127.0.0.1:8000/v1"
    assert config.api_key == "not-needed"
    assert api.configuration_status("hosted-llm")[0]


def test_status_requires_selected_provider_configuration(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "irrelevant")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-only")
    assert api.configuration_status("deepseek")[0]
    assert not api.configuration_status("hosted-llm")[0]
    assert api.configuration_status("qwen") == (False, "缺少 DASHSCOPE_API_KEY")
    monkeypatch.setenv("HOSTED_LLM_BASE_URL", "https://host.example/v1")
    assert api.configuration_status("hosted-llm") == (False, "缺少 HOSTED_LLM_MODEL")


@pytest.mark.parametrize("url", ["missing-scheme", "ftp://example.com", "https://example.com:bad"])
def test_invalid_endpoint_is_reported_without_a_request(monkeypatch, url):
    monkeypatch.setenv("HOSTED_LLM_BASE_URL", url)
    assert not api.configuration_status("hosted-llm")[0]


def test_frozen_environment_file_follows_executable_not_working_directory(monkeypatch, tmp_path):
    executable = tmp_path / "installed" / "NovelWriter.exe"
    monkeypatch.setattr(api.sys, "frozen", True, raising=False)
    monkeypatch.setattr(api.sys, "executable", str(executable))
    monkeypatch.chdir(tmp_path)
    assert api.environment_file() == executable.parent / ".env"


def test_qwen_region_and_model_override(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://workspace.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setenv("QWEN_MODEL", "qwen-custom")
    config = api.get_config("qwen")
    assert config.model == "qwen-custom"
    assert "ap-southeast-1" in config.base_url
    assert "test-key" not in repr(config)
