"""Opt-in, provenance-preserving traces for LLM calls.

The shared backend currently returns text only, so exact token usage and price
are not available to NovelWriter.  A trace must say that explicitly instead of
inventing exact figures.  Character counts and clearly labelled token estimates
remain useful for comparing prompt growth across a reproducible quality run.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional


_ACTIVE_TRACE: ContextVar[Optional["LLMTraceRecorder"]] = ContextVar(
    "novelwriter_active_llm_trace", default=None
)
_ACTIVE_STAGE: ContextVar[str] = ContextVar("novelwriter_active_trace_stage", default="")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _estimated_tokens(text: str) -> int:
    """A labelled estimate only; never reported as provider usage."""
    if not text:
        return 0
    cjk = sum("\u3400" <= char <= "\u9fff" for char in text)
    other = len(text) - cjk
    return max(1, math.ceil(cjk / 1.5 + other / 4.0))


@dataclass
class LLMTraceRecorder:
    root: Path
    run_id: str
    mode: str
    calls: list[Dict[str, Any]] = field(default_factory=list)
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.run_dir = self.root / "system" / "quality_runs" / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.calls_path = self.run_dir / "llm_calls.jsonl"
        self.summary_path = self.run_dir / "llm_summary.json"

    def record(
        self,
        *,
        prompt: str,
        backend: str,
        model: str,
        invoke: Callable[[], str],
    ) -> str:
        sequence = len(self.calls) + 1
        started = time.perf_counter()
        response = ""
        error: Optional[BaseException] = None
        try:
            response = invoke()
            return response
        except BaseException as exc:
            error = exc
            raise
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
            record = {
                "sequence": sequence,
                "run_id": self.run_id,
                "mode": self.mode,
                "stage": _ACTIVE_STAGE.get(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "backend": backend,
                "model": model,
                "prompt": prompt,
                "prompt_sha256": _sha256(prompt),
                "prompt_chars": len(prompt),
                "response": response,
                "response_sha256": _sha256(response),
                "response_chars": len(response),
                "elapsed_ms": elapsed_ms,
                "success": error is None,
                "error_type": type(error).__name__ if error else "",
                "error": str(error) if error else "",
                "usage": {
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "total_tokens": None,
                    "source": "backend_not_exposed",
                    "estimated_prompt_tokens": _estimated_tokens(prompt),
                    "estimated_completion_tokens": _estimated_tokens(response),
                },
                "cost": {
                    "amount_usd": None,
                    "source": "pricing_and_provider_usage_not_exposed",
                },
            }
            self.calls.append(record)
            with self.calls_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def write_summary(self) -> Dict[str, Any]:
        summary = {
            "run_id": self.run_id,
            "mode": self.mode,
            "started_at": self.started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "call_count": len(self.calls),
            "failed_call_count": sum(not item["success"] for item in self.calls),
            "elapsed_ms": round(sum(item["elapsed_ms"] for item in self.calls), 3),
            "exact_token_usage_available": False,
            "exact_cost_available": False,
            "reason": "llm-backends 0.2.0 text API does not expose provider usage metadata",
        }
        self.summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return summary


@contextmanager
def trace_session(root: os.PathLike[str] | str, *, run_id: str, mode: str) -> Iterator[LLMTraceRecorder]:
    recorder = LLMTraceRecorder(Path(root), run_id=run_id, mode=mode)
    token = _ACTIVE_TRACE.set(recorder)
    try:
        yield recorder
    finally:
        _ACTIVE_TRACE.reset(token)
        recorder.write_summary()


@contextmanager
def trace_stage(stage: str) -> Iterator[None]:
    token = _ACTIVE_STAGE.set(stage)
    try:
        yield
    finally:
        _ACTIVE_STAGE.reset(token)


def trace_model_call(
    *,
    prompt: str,
    backend: str,
    model: str,
    invoke: Callable[[], str],
) -> str:
    recorder = _ACTIVE_TRACE.get()
    if recorder is None:
        return invoke()
    return recorder.record(prompt=prompt, backend=backend, model=model, invoke=invoke)
