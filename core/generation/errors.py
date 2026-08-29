# -*- coding: utf-8 -*-
"""Generation errors that are safe to show verbatim in the UI."""
from __future__ import annotations


class GenerationPipelineError(RuntimeError):
    """A generation step cannot continue and the message is user-facing."""


def fail(title: str, message: str) -> None:
    """Stop the current stage with the exact message shown by the task runner."""
    prefix = f"{title}：" if title else ""
    raise GenerationPipelineError(f"{prefix}{message}")
