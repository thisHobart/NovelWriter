"""Shared wire contract and bounded diagnostics for automatic review reports."""

from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator

from .contracts import JsonObject
from .models import WorkspaceError, parse_json

MAX_ERRORS = 10
MAX_VALUE_CHARS = 200
_MISSING = object()
_EXAMPLE_PATHS = {
    "novel_copy": "opening.text",
    "video_copy": "blocks[0].narration",
    "asset_catalog": "entities[0].name",
    "storyboard": "shots[0].action",
    "character_spec": "views[0].positive_prompt",
    "scene_spec": "views[0].positive_prompt",
    "prop_spec": "views[0].positive_prompt",
    "prompt_bundle": "shots[0].resolved_prompt",
    "video_prompt": "video_motion_prompt",
}
_BASE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["issues"],
    "properties": {"issues": {"type": "array", "items": {"$ref": "#/$defs/issue"}}},
    "$defs": {
        "issue": {
            "type": "object",
            "required": ["rule_id", "severity", "target_path", "explanation", "suggestion"],
            "properties": {
                **{
                    name: {"type": "string", "pattern": r"\S"}
                    for name in ("rule_id", "target_path", "explanation", "suggestion")
                },
                "severity": {"enum": ["warning"]},
                "quote": {"type": ["string", "null"]},
                "source_refs": {"type": "array", "items": {"$ref": "#/$defs/source_ref"}},
            },
        },
        "source_ref": {
            "type": "object",
            "required": ["source_id", "start", "end"],
            "properties": {
                "source_id": {"type": "string"},
                "start": {"type": "integer"},
                "end": {"type": "integer"},
                "quote": {"type": "string"},
            },
        },
    },
}


def review_schema(kind: str) -> JsonObject:
    """Return an isolated schema; stage overrides never mutate the shared base."""
    if kind not in _EXAMPLE_PATHS:
        raise ValueError(f"未知审阅类型：{kind}")
    schema = deepcopy(_BASE_SCHEMA)
    if kind == "video_prompt":
        schema["$defs"]["issue"]["properties"]["target_path"]["const"] = "video_motion_prompt"
    return schema


@lru_cache(maxsize=len(_EXAMPLE_PATHS))
def _validator(kind: str) -> Draft202012Validator:
    schema = review_schema(kind)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def output_contract(kind: str) -> JsonObject:
    # Validate our own schema before constructing any paid request.
    validator = _validator(kind)
    return {
        "schema": deepcopy(validator.schema),
        "example": {
            "issues": [
                {
                    "rule_id": "REVIEW_EXAMPLE",
                    "severity": "warning",
                    "target_path": _EXAMPLE_PATHS[kind],
                    "explanation": "独立结构示例：说明实际发现的问题及原因",
                    "suggestion": "独立结构示例：填写对应的可执行修改建议",
                    "quote": None,
                    "source_refs": [],
                }
            ],
        },
    }


def field_path(parts: list[str | int]) -> str:
    path = ""
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        elif part.isidentifier():
            path += ("." if path else "") + part
        else:
            path += f"[{json.dumps(part, ensure_ascii=False)}]"
    return path or "$"


def value_preview(value: Any) -> str:
    if value is _MISSING:
        return "<missing>"
    result = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return result if len(result) <= MAX_VALUE_CHARS else result[: MAX_VALUE_CHARS - 1] + "…"


def _value_type(value: Any) -> str:
    if value is _MISSING:
        return "missing"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    return "integer" if isinstance(value, int) else "number"


def diagnostic(
    path: str, rule: str, message: str, expected: Any, actual: Any = _MISSING
) -> JsonObject:
    return {
        "path": path,
        "rule": rule,
        "message": message,
        "expected": expected,
        "actual_type": _value_type(actual),
        "actual_preview": value_preview(actual),
    }


def reference_error(
    path: str,
    message: str,
    actual: Any,
    *,
    rule: str = "source_reference",
    expected: Any = "本次选区内真实原文的准确引用",
) -> WorkspaceError:
    return WorkspaceError(
        "INVALID_OUTPUT",
        f"{path}：{message}",
        details={
            "category": "reference",
            "errors": [diagnostic(path, rule, message, expected, actual)],
            "omitted_count": 0,
        },
    )


def parse_report(raw: str, kind: str) -> JsonObject:
    try:
        value = parse_json(raw)
    except WorkspaceError as exc:
        # Preserve the native parser evidence; do not infer an unescaped quote.
        exc.details = {"category": "syntax", **exc.details}
        if "json_error" not in exc.details and exc.__cause__ is not None:
            exc.details["json_error"] = str(exc.__cause__)
        raise
    errors = []
    required_groups = set()
    for error in _validator(kind).iter_errors(value):
        parts = list(error.absolute_path)
        if error.validator == "required":
            group = tuple(parts)
            if group in required_groups:
                continue
            required_groups.add(group)
            for name in error.validator_value:
                if name not in error.instance:
                    path = field_path([*parts, name])
                    expected = {
                        "required": True,
                        **error.schema.get("properties", {}).get(name, {}),
                    }
                    errors.append(diagnostic(path, "required", "缺少必填字段", expected))
        else:
            path = field_path(parts)
            message = {
                "type": "字段类型不符合要求",
                "pattern": "文字不能全为空白",
                "enum": "字段取值不在允许范围内",
                "const": "视频审阅只能定位当前镜头的video_motion_prompt",
            }.get(error.validator, "字段不符合报告结构规则")
            errors.append(
                diagnostic(
                    path,
                    error.validator,
                    message,
                    error.validator_value,
                    error.instance,
                )
            )
    if errors:
        # Stable across object field order; limit only diagnostics, never the raw reply.
        errors.sort(key=lambda row: (row["path"], row["rule"], row["message"]))
        omitted = max(0, len(errors) - MAX_ERRORS)
        first = errors[0]
        raise WorkspaceError(
            "INVALID_OUTPUT",
            f"审阅报告结构错误：{first['path']}：{first['message']}",
            details={"category": "schema", "errors": errors[:MAX_ERRORS], "omitted_count": omitted},
        )
    return value
