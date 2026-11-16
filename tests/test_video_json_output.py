"""ASCII quotes must survive JSON serialization and one model format repair."""

import json

import pytest

from core.short_video.v2.models import (
    WorkspaceError,
    encode,
    format_error_feedback,
    parse_object,
)


TEXT = 'reference 字段为"首帧图片"；路径 C:\\clips\\shot.mp4\n保留英文引号。'


@pytest.mark.parametrize("fenced", [False, True])
def test_escaped_quotes_backslashes_and_newlines_roundtrip_without_rewriting(fenced):
    raw = encode({"explanation": TEXT})
    assert r'\"首帧图片\"' in raw
    assert r'C:\\clips\\shot.mp4\n' in raw
    if fenced:
        raw = "```json\n" + raw + "\n```"
    assert parse_object(raw) == {"explanation": TEXT}


@pytest.mark.parametrize("fenced", [False, True])
def test_unescaped_quotes_report_exact_position_and_bounded_snippet(fenced):
    broken = encode({"explanation": TEXT}).replace(r'\"首帧图片\"', '"首帧图片"')
    with pytest.raises(json.JSONDecodeError) as original:
        json.loads(broken)
    raw = "```json\n" + broken + "\n```" if fenced else broken
    with pytest.raises(WorkspaceError) as caught:
        parse_object(raw)
    error, expected = caught.value, original.value
    assert error.code == "INVALID_OUTPUT"
    assert (error.details["line"], error.details["column"], error.details["position"]) == (
        expected.lineno, expected.colno, expected.pos)
    assert len(error.details["snippet"]) <= 80
    assert '"首帧图片"' in error.details["snippet"]
    assert "snippet" in format_error_feedback(error)
    assert "首帧图片" not in str(error)


@pytest.mark.parametrize("raw", ['{"text":NaN}', '{"text":Infinity}', '[]'])
def test_other_invalid_outputs_are_not_treated_as_quote_errors(raw):
    with pytest.raises(WorkspaceError) as caught:
        parse_object(raw)
    assert caught.value.code == "INVALID_OUTPUT"
    assert "json_error" not in caught.value.details
