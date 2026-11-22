"""回归异常原样传播、有限重试，以及模型格式错误和程序错误的区别。"""
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError

from agents.writing.chapter_writing_agent import ChapterWritingAgent, QualityThresholds
from core.common import api_probe, api_providers, api_settings
from core.generation import ai_helper, helper_fns
from core.generation.design_contract import DesignContractError
from core.generation.lore_run import readable_json
from core.generation.parameter_compliance import ComplianceReview, ComplianceServiceError
from core.generation.stage_context import make_context


def make_review(tmp_path, send):
    context = make_context(str(tmp_path), "test-model", {})
    context.extras["run_directory"] = str(tmp_path / "run")
    return ComplianceReview(context, "characters", send)


@pytest.mark.parametrize("phase", ["readable", "parse"])
@pytest.mark.parametrize("error_type", [TypeError, KeyError, AttributeError, ValueError])
def test_internal_candidate_errors_propagate_without_regeneration(tmp_path, phase, error_type):
    send = Mock(return_value="candidate")
    review = make_review(tmp_path, send)
    failure = error_type("internal defect")
    callbacks = {"readable": lambda raw: raw, "parse": lambda raw: raw}
    callbacks[phase] = Mock(side_effect=failure)

    with pytest.raises(error_type) as caught:
        review.run("generate", callbacks["parse"], callbacks["readable"])

    assert caught.value is failure
    assert send.call_count == 1
    assert review.receipt["status"] == "failed"
    assert review.receipt["exit_reason"] == "execution_error"


def test_invalid_model_json_still_repairs_and_returns_a_checked_candidate(tmp_path):
    send = Mock(side_effect=["invalid JSON", '[{"name": "Alice"}]'])
    review = make_review(tmp_path, send)

    result = review.run(
        "generate characters",
        lambda raw: readable_json(raw, "characters"),
        lambda raw: readable_json(raw, "characters"),
    )

    assert result == {"characters": [{"name": "Alice"}], "relationships": []}
    assert send.call_count == 2
    assert review.receipt["status"] == "complete"


def test_contract_failure_still_repairs_without_losing_readable_content(tmp_path):
    send = Mock(side_effect=["first candidate", "repaired candidate"])
    review = make_review(tmp_path, send)
    parse = Mock(side_effect=[DesignContractError("missing field"), "checked"])

    assert review.run("generate", parse, lambda raw: raw) == "checked"
    assert "first candidate" in send.call_args.args[0]
    assert "missing field" in send.call_args.args[0]
    assert review.receipt["status"] == "complete"


@pytest.mark.parametrize("invalid_report", [
    "not JSON",
    '{"status": [], "checks": []}',
    '{"status": "pass", "checks": [{"parameter_key": []}]}',
    '{"status": "pass", "checks": [{"parameter_key": "genre", "status": []}]}',
])
def test_malformed_external_check_reports_have_an_explicit_format_error(tmp_path, invalid_report):
    review = make_review(tmp_path, Mock(return_value=invalid_report))
    review.parameters = {"genre": "mystery"}

    with pytest.raises(ComplianceServiceError):
        review("candidate")


def test_internal_check_error_is_not_converted_to_a_service_error(tmp_path, monkeypatch):
    review = make_review(tmp_path, Mock(return_value="{}"))
    review.parameters = {"genre": "mystery"}
    failure = KeyError("internal parser bug")
    monkeypatch.setattr(json, "loads", Mock(side_effect=failure))

    with pytest.raises(KeyError) as caught:
        review("candidate")

    assert caught.value is failure


def api_failure(kind):
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    if kind == "connection":
        return APIConnectionError(request=request)
    if kind == "timeout":
        return APITimeoutError(request=request)
    if isinstance(kind, int):
        response = httpx.Response(kind, request=request)
        return APIStatusError("service failure", response=response, body=None)
    return kind("internal defect")


@pytest.mark.parametrize("entry", ["interface", "helper"])
@pytest.mark.parametrize("kind, attempts", [
    (TypeError, 1), (KeyError, 1), (ValueError, 1), (401, 1), (403, 1), (404, 1),
    ("connection", 3), ("timeout", 3), (429, 3), (503, 3),
])
def test_retry_boundaries_preserve_original_error_and_skip_permanent_failures(monkeypatch, entry, kind, attempts):
    failure = api_failure(kind)
    cause = OSError("original cause")
    failure.__cause__ = cause
    suppressed = failure.__suppress_context__
    call = Mock(side_effect=failure)
    if entry == "interface":
        interface = api_providers.ConfiguredAPIInterface("hosted-llm")
        monkeypatch.setattr(interface, "generate", call)
        invoke = lambda: interface.generate_with_retry("prompt", max_retries=3)
    else:
        monkeypatch.setattr(ai_helper, "send_prompt", call)
        invoke = lambda: ai_helper.send_prompt_with_retry("prompt", model="hosted-llm", max_retries=3)

    with pytest.raises(type(failure)) as caught:
        invoke()

    assert caught.value is failure
    assert caught.value.__cause__ is cause
    assert caught.value.__suppress_context__ is suppressed
    assert call.call_count == attempts


def test_transient_failure_can_recover_within_the_retry_limit(monkeypatch):
    interface = api_providers.ConfiguredAPIInterface("hosted-llm")
    call = Mock(side_effect=[api_failure("timeout"), "generated"])
    monkeypatch.setattr(interface, "generate", call)

    assert interface.generate_with_retry("prompt", max_retries=2) == "generated"
    assert call.call_count == 2


def test_explicit_retry_limit_is_not_multiplied_by_sdk_retries(monkeypatch):
    requests = []
    sdk_client = api_providers.OpenAI

    def respond(request):
        requests.append(request)
        return httpx.Response(503, json={"error": {"message": "temporarily unavailable"}})

    def make_client(**options):
        return sdk_client(http_client=httpx.Client(transport=httpx.MockTransport(respond)), **options)

    config = api_providers.APIConfig("hosted-llm", "https://example.test/v1", "test-model", "test-key")
    monkeypatch.setattr(api_providers, "get_config", lambda provider: config)
    monkeypatch.setattr(api_providers, "OpenAI", make_client)
    interface = api_providers.ConfiguredAPIInterface("hosted-llm")

    with pytest.raises(APIStatusError) as caught:
        interface.generate_with_retry("prompt", max_retries=3)

    assert caught.value.status_code == 503
    assert len(requests) == 3


@pytest.mark.parametrize("kind", [AttributeError, "timeout", 401])
def test_connection_probe_exposes_original_sdk_or_program_error(monkeypatch, kind):
    failure = api_failure(kind)
    client_factory = MagicMock()
    client = client_factory.return_value.__enter__.return_value
    client.chat.completions.create.side_effect = failure
    monkeypatch.setattr(api_providers, "OpenAI", client_factory)
    config = api_providers.APIConfig("hosted-llm", "https://example.test/v1", "test-model", "test-key")

    with pytest.raises(type(failure)) as caught:
        api_probe.probe_connection(config)

    assert caught.value is failure
    assert client.chat.completions.create.call_count == 1
    assert client_factory.call_args.kwargs["max_retries"] == 0


@pytest.mark.parametrize("operation", ["open_file", "read_json", "write_file", "write_json"])
def test_file_helpers_preserve_permission_error(tmp_path, monkeypatch, operation):
    path = tmp_path / "data.json"
    path.write_text("{}", encoding="utf-8")
    failure = PermissionError("resource unavailable")
    monkeypatch.setattr(helper_fns, "open", Mock(side_effect=failure), raising=False)
    arguments = (path, "content") if operation.startswith("write") else (path,)

    with pytest.raises(PermissionError) as caught:
        getattr(helper_fns, operation)(*arguments)

    assert caught.value is failure


def test_file_helpers_do_not_disguise_invalid_data_or_json(tmp_path):
    with pytest.raises(TypeError):
        helper_fns.write_file(tmp_path / "text.txt", None)
    with pytest.raises(TypeError):
        helper_fns.write_json(tmp_path / "data.json", object())
    with pytest.raises(TypeError):
        helper_fns.save_prompt_to_file(str(tmp_path), "prompt", None)
    path = tmp_path / "invalid.json"
    path.write_text("invalid JSON", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        helper_fns.read_json(path)


def test_configuration_distinguishes_missing_corrupt_and_unreadable_files(tmp_path, monkeypatch):
    path = tmp_path / "llm_settings.json"
    monkeypatch.setattr(api_settings, "settings_file", lambda: path)
    assert api_settings._read() == {"version": 1, "providers": {}}
    path.write_text("invalid JSON", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        api_settings._read()
    failure = PermissionError("configuration access denied")
    monkeypatch.setattr(Path, "read_text", Mock(side_effect=failure))
    with pytest.raises(PermissionError) as caught:
        api_settings._read()
    assert caught.value is failure


def test_atomic_configuration_save_preserves_the_primary_error_even_if_cleanup_fails(tmp_path, monkeypatch):
    path = tmp_path / "llm_settings.json"
    path.write_text('{"version": 1, "providers": {}}', encoding="utf-8")
    monkeypatch.setattr(api_settings, "settings_file", lambda: path)
    failure = PermissionError("replace denied")
    monkeypatch.setattr(api_settings.os, "replace", Mock(side_effect=failure))
    monkeypatch.setattr(Path, "unlink", Mock(side_effect=PermissionError("cleanup denied")))

    with pytest.raises(PermissionError) as caught:
        api_settings.save_provider("hosted-llm", "https://example.test/v1", "", "test-model")

    assert caught.value is failure
    assert "cleanup denied" in failure.__notes__[0]
    assert json.loads(path.read_text(encoding="utf-8")) == {"version": 1, "providers": {}}


@pytest.mark.parametrize("method, arguments", [
    ("_analyze_chapter_coherence", ([object()],)),
    ("_analyze_chapter_pacing", ("content", [object()])),
    ("_analyze_character_development", (None,)),
    ("_analyze_batch_consistency", ([object(), object()],)),
    ("_analyze_story_progression", ([object(), object()],)),
    ("_analyze_style_consistency", ([object(), object()],)),
    ("_review_chapter", ("content", [object()], 1, "section")),
    ("_review_batch", ([object()], 1)),
])
def test_review_calculation_errors_do_not_produce_default_scores(method, arguments):
    agent = ChapterWritingAgent.__new__(ChapterWritingAgent)
    with pytest.raises(AttributeError):
        getattr(agent, method)(*arguments)


def test_scene_review_propagates_the_review_agent_error():
    agent = ChapterWritingAgent.__new__(ChapterWritingAgent)
    failure = AttributeError("invalid review result")
    agent.review_agent = SimpleNamespace(review_step_output=Mock(side_effect=failure))

    with pytest.raises(AttributeError) as caught:
        agent._review_scene("scene content", 1, 1)

    assert caught.value is failure


def test_successful_scene_chapter_and_batch_reviews_still_save_results(tmp_path):
    agent = ChapterWritingAgent.__new__(ChapterWritingAgent)
    agent.logger = logging.getLogger("test.chapter-review")
    agent.review_agent = None
    agent.quality_thresholds = QualityThresholds()
    agent.dir_manager = SimpleNamespace(get_quality_dir=lambda: str(tmp_path))
    agent._quality_trends_cache = []
    agent._last_cache_update = None
    agent._cache_ttl_seconds = 300
    content = "word " * 500

    scene = agent._review_scene(content, 1, 1)
    chapter = agent._review_chapter(content, [scene], 1, "section")
    batch = agent._review_batch([chapter], 1)
    agent._save_scene_review(scene)
    agent._save_chapter_review(chapter)
    agent._save_batch_review(batch)

    assert chapter.overall_quality == scene.quality_score
    assert batch.average_quality == chapter.overall_quality
    assert len(agent.get_quality_trends()) == 3
    assert agent._load_chapter_review(1) == chapter


def test_short_video_sdk_request_has_no_application_deadline(monkeypatch):
    import asyncio
    from core.short_video.v2.llm import ScriptLLM

    config = api_providers.APIConfig("hosted-llm", "https://example.test/v1", "test-model", "test-key")
    client_factory = MagicMock()
    client = client_factory.return_value.__aenter__.return_value
    client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="generated"))],
    ))
    monkeypatch.setattr(api_providers, "get_config", lambda provider: config)
    monkeypatch.setattr(api_providers, "AsyncOpenAI", client_factory)
    monkeypatch.setattr(asyncio, "wait_for", Mock(side_effect=AssertionError("unexpected application deadline")))
    record = Mock()

    assert ScriptLLM("hosted-llm", record=record)("prompt") == "generated"

    assert "timeout" not in client_factory.call_args.kwargs
    assert client_factory.call_args.kwargs["max_retries"] == 0
    record.assert_called_once_with("prompt", "generated", None)


def test_lore_repairs_can_complete_beyond_the_old_call_and_time_limits(tmp_path):
    send = Mock(side_effect=[
        TimeoutError(), TimeoutError(), "first",
        TimeoutError(), TimeoutError(), "second",
        TimeoutError(), TimeoutError(), "third",
    ])
    review = make_review(tmp_path, send)
    review.started -= 10000
    parse = Mock(side_effect=[DesignContractError("missing A"), DesignContractError("missing B"), "checked"])

    assert review.run("generate", parse, lambda raw: raw) == "checked"
    assert send.call_count == 9
    assert review.receipt["status"] == "complete"
    assert review.receipt["elapsed_seconds"] >= 10000
