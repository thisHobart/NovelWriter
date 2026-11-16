"""Opt-in console diagnostics, separate from application logs and project records."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from threading import Lock
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .contracts import JsonObject

_lock = Lock()
_secrets: set[str] = set()
_secret_fields = {"api_key", "authorization", "access_token", "secret", "password"}


def enabled() -> bool:
    return os.environ.get("NOVELWRITER_LLM_TRACE", "").lower() in {"1", "true", "yes", "on"}


def endpoint_label(address: str) -> str:
    parsed = urlsplit(address)
    return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))


def _redact_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if str(key).lower().replace("-", "_") in _secret_fields
            else _redact_fields(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_fields(item) for item in value]
    return value


def trace_event(
    event: str, metadata: JsonObject, payload: Any = None, *, secrets: tuple[str, ...] = ()
) -> None:
    if not enabled():
        return
    stream = sys.stderr if sys.stderr is not None else sys.stdout
    if stream is None:
        return
    with _lock:
        _secrets.update(secret for secret in secrets if secret)
        header = json.dumps(
            _redact_fields({"time": datetime.now(timezone.utc).isoformat(), **metadata}),
            ensure_ascii=False,
            default=str,
        )
        body = (
            payload
            if isinstance(payload, str)
            else json.dumps(_redact_fields(payload), ensure_ascii=False, indent=2, default=str)
        )
        message = f"\n[LLM_TRACE {event}] {header}\n{body}\n[END LLM_TRACE {event}]\n"
        for secret in sorted(_secrets, key=len, reverse=True):
            message = message.replace(secret, "[REDACTED]")
        try:
            print(message, file=stream, flush=True)
        except (OSError, UnicodeError, ValueError):
            # An unavailable console must not resend or fail a model request.
            pass
