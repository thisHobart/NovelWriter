"""Regression coverage for the workspace's standalone LLM and durable storage."""
import asyncio

import httpx
import pytest
from openai import AsyncOpenAI

from core.generation import api_providers
from core.short_video.v2 import storage
from core.short_video.v2.llm import (
    BudgetExhausted,
    RecordingError,
    RequestBudget,
    ScriptLLM,
    TextProviderError,
)
from core.short_video.v2.repository import ProjectRepository


def test_budget_counts_failures_and_never_implicitly_retries():
    records = []
    budget = RequestBudget(max_calls=2)
    def fail(prompt):
        raise TimeoutError("unavailable")
    llm = ScriptLLM("hosted-llm", budget, record=lambda *args: records.append(args), transport=fail)
    for _ in range(2):
        with pytest.raises(TextProviderError) as error:
            llm("prompt")
        assert isinstance(error.value.__cause__, TimeoutError)
    with pytest.raises(BudgetExhausted):
        llm("prompt")
    assert budget.calls == 2 and len(records) == 2


def test_recording_failure_is_distinct_from_retryable_model_failure():
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


def test_sdk_nontext_response_cannot_corrupt_request_history(tmp_path):
    repository = ProjectRepository(tmp_path)
    run, _ = repository.start_run({"stage": "chat", "request_id": "nontext"}, expected_revision=0, snapshot={})
    def record(prompt, response, error):
        repository.update_run(run["id"], lambda r, _: r["calls"].append({"prompt": prompt, "response": response, "error": error}))
    llm = ScriptLLM("hosted-llm", RequestBudget(max_calls=2), transport=lambda _: {"unexpected": "value"}, record=record)
    with pytest.raises(ValueError, match="未返回文字"):
        llm("prompt")
    call = repository.load()["runs"][0]["calls"][0]
    assert call["response"] == "" and call["error"] == "WorkspaceError"


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
    with pytest.raises(TextProviderError) as error:
        ScriptLLM("hosted-llm", budget)("test")
    assert isinstance(error.value.__cause__, TimeoutError)
    assert cancelled and options[0]["max_retries"] == 0 and options[0]["timeout"] <= 0.2
    assert budget.calls == 1


def test_failed_replace_preserves_manifest_and_cleans_temporary_file(tmp_path, monkeypatch):
    repository = ProjectRepository(tmp_path)
    with repository.transaction() as state:
        state["project_id"] = "project_" + "0" * 32
    original = repository.path.read_bytes()
    def fail(*args):
        raise OSError("disk failure")
    monkeypatch.setattr(storage.os, "replace", fail)
    with pytest.raises(OSError):
        with repository.transaction() as state:
            state["exports"].append({"test": True})
    assert repository.path.read_bytes() == original
    assert not list(repository.root.glob(".workspace-*"))
