import json

import pytest

from core.generation.llm_trace import trace_model_call, trace_session, trace_stage


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_trace_records_actual_prompt_output_timing_and_unavailable_usage(tmp_path):
    with trace_session(tmp_path, run_id="fixed-run", mode="scripted_offline") as recorder:
        with trace_stage("chapter_contract"):
            result = trace_model_call(
                prompt="只输出 JSON：{\"ok\": true}",
                backend="scripted",
                model="fixture-v1",
                invoke=lambda: '{"ok": true}',
            )

    assert result == '{"ok": true}'
    records = _read_jsonl(recorder.calls_path)
    assert len(records) == 1
    call = records[0]
    assert call["run_id"] == "fixed-run"
    assert call["mode"] == "scripted_offline"
    assert call["stage"] == "chapter_contract"
    assert call["prompt"] == "只输出 JSON：{\"ok\": true}"
    assert call["response"] == '{"ok": true}'
    assert call["elapsed_ms"] >= 0
    assert call["usage"]["prompt_tokens"] is None
    assert call["usage"]["source"] == "backend_not_exposed"
    assert call["cost"]["amount_usd"] is None
    summary = json.loads(recorder.summary_path.read_text(encoding="utf-8"))
    assert summary["call_count"] == 1
    assert summary["failed_call_count"] == 0


def test_trace_records_failed_call_and_reraises(tmp_path):
    def fail():
        raise RuntimeError("endpoint unavailable")

    with trace_session(tmp_path, run_id="failed-run", mode="real") as recorder:
        with pytest.raises(RuntimeError, match="endpoint unavailable"):
            trace_model_call(
                prompt="ping",
                backend="api",
                model="hosted-llm",
                invoke=fail,
            )

    call = _read_jsonl(recorder.calls_path)[0]
    assert call["success"] is False
    assert call["error_type"] == "RuntimeError"
    assert "endpoint unavailable" in call["error"]
