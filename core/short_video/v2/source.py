"""Unicode-codepoint source ranges; Qt UTF-16 offsets are converted explicitly."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .contracts import ArtifactRecord, JsonObject, SourceRange
from .models import WorkspaceError, require, text


def normalize_source(value: str) -> str:
    text(value, "原文")
    return value.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def source_content(
    value: str,
    *,
    title: str = "原文",
    origin: str = "paste",
    filename: str | None = None,
    raw: bytes | None = None,
) -> JsonObject:
    normalized = normalize_source(value)
    require(origin in {"paste", "file"}, "INVALID_INPUT", "来源类型无效，仅支持粘贴或文件导入")
    paragraphs = [
        {"paragraph_id": f"p{i + 1}", "start": m.start(), "end": m.end()}
        for i, m in enumerate(re.finditer(r"[^\n]+", normalized))
        if m.group().strip()
    ]
    return {
        "title": text(title, "原文标题"),
        "origin": origin,
        "original_file_name": filename,
        "raw_sha256": hashlib.sha256(raw if raw is not None else value.encode("utf-8")).hexdigest(),
        "normalized_text": normalized,
        "text_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "paragraphs": paragraphs,
    }


def read_source_file(filename: str | Path, *, encoding: str = "utf-8-sig") -> JsonObject:
    path = Path(filename)
    require(path.suffix.lower() in {".txt", ".md"}, "INVALID_INPUT", "仅支持 TXT 或 Markdown 文件")
    raw = path.read_bytes()
    try:
        value = raw.decode(encoding)
    except (UnicodeError, LookupError) as exc:
        raise WorkspaceError("SOURCE_ENCODING_ERROR", "文件解码失败，请选择正确编码后重试") from exc
    return source_content(value, title=path.stem, origin="file", filename=path.name, raw=raw)


def validate_ranges(value: str, ranges: list[SourceRange]) -> list[SourceRange]:
    require(isinstance(ranges, list) and bool(ranges), "INVALID_INPUT", "请选择非空原文范围")
    normalized = []
    for row in ranges:
        require(isinstance(row, dict), "INVALID_INPUT", "原文范围无效")
        start, end = row.get("start"), row.get("end")
        require(
            type(start) is int and type(end) is int and 0 <= start < end <= len(value),
            "INVALID_INPUT",
            "原文选区越界或为空",
        )
        normalized.append({"start": start, "end": end})
    normalized.sort(key=lambda row: row["start"])
    require(
        all(a["end"] <= b["start"] for a, b in zip(normalized, normalized[1:])),
        "INVALID_INPUT",
        "原文选区不能重叠",
    )
    require(
        any(value[r["start"] : r["end"]].strip() for r in normalized),
        "INVALID_INPUT",
        "选区不能只有空白",
    )
    return normalized


def selection_content(source: ArtifactRecord, ranges: list[SourceRange]) -> JsonObject:
    value = source["content"]["normalized_text"]
    ranges = validate_ranges(value, ranges)
    selected = "\n".join(value[r["start"] : r["end"]] for r in ranges)
    return {
        "source_id": source["id"],
        "ranges": ranges,
        "selected_text_sha256": hashlib.sha256(selected.encode("utf-8")).hexdigest(),
    }


def source_references(source: ArtifactRecord, selection: ArtifactRecord) -> list[JsonObject]:
    """Return exact original ranges, never offsets into a concatenated selection."""
    return [
        {
            "source_id": source["id"],
            **row,
            "quote": source["content"]["normalized_text"][row["start"] : row["end"]],
        }
        for row in selection["content"]["ranges"]
    ]


def validate_source_ref(ref: JsonObject, source: ArtifactRecord) -> None:
    require(
        isinstance(ref, dict) and ref.get("source_id") == source["id"],
        "INVALID_INPUT",
        "原文引用不存在",
    )
    value = source["content"]["normalized_text"]
    start, end = ref.get("start"), ref.get("end")
    validate_ranges(value, [{"start": start, "end": end}])
    if "quote" in ref:
        require(ref["quote"] == value[start:end], "INVALID_INPUT", "引文与原文位置不一致")
    if ref.get("paragraph_id"):
        paragraph = next(
            (
                p
                for p in source["content"]["paragraphs"]
                if p["paragraph_id"] == ref["paragraph_id"]
            ),
            None,
        )
        require(
            paragraph is not None and paragraph["start"] <= start < end <= paragraph["end"],
            "INVALID_INPUT",
            "引文与段落位置不一致",
        )


TERMINAL_MARKS = frozenset("。！？!?…")
CLOSING_MARKS = frozenset("”’」』）)】》")
SENTENCE_SHRINK_LIMIT = 10
SENTENCE_GROW_LIMIT = 150


def _sentence_start(value: str, position: int, lo: int, hi: int) -> bool:
    """A boundary follows terminal punctuation or a line break, plus any closing quotes."""
    if position in (lo, hi):
        return True
    if value[position] in TERMINAL_MARKS or value[position] in CLOSING_MARKS:
        return False
    index = position - 1
    while index > lo and value[index] in CLOSING_MARKS:
        index -= 1
    return value[index] in TERMINAL_MARKS or value[index] == "\n"


def _nearest_sentence_start(
    value: str, position: int, lo: int, hi: int, inward: int, shrink_limit: int
) -> int:
    for distance in range(SENTENCE_GROW_LIMIT + 1):
        # On a tie the outward boundary wins, so no referenced text is dropped.
        for candidate, shrinking in (
            (position - inward * distance, False),
            (position + inward * distance, True),
        ):
            if shrinking and distance > shrink_limit:
                continue
            if lo <= candidate <= hi and _sentence_start(value, candidate, lo, hi):
                return candidate
    return position


def sentence_range(value: str, start: int, end: int, lo: int, hi: int) -> tuple[int, int]:
    """Move model-written coordinates to whole sentences inside the selected range [lo, hi).

    Coordinates usually drift by a few characters, so each end moves to its nearest boundary:
    at most 10 characters inward (dropping a neighbour's fragment) or 150 outward. A piece
    inside a single sentence becomes that whole sentence; without punctuation nearby, the
    original coordinate is kept.
    """
    snapped_start = _nearest_sentence_start(value, start, lo, hi, 1, SENTENCE_SHRINK_LIMIT)
    snapped_end = _nearest_sentence_start(value, end, lo, hi, -1, SENTENCE_SHRINK_LIMIT)
    if snapped_start >= snapped_end:
        snapped_start = _nearest_sentence_start(value, start, lo, hi, 1, 0)
        snapped_end = _nearest_sentence_start(value, end, lo, hi, -1, 0)
    while snapped_start < snapped_end and value[snapped_start].isspace():
        snapped_start += 1
    while snapped_end > snapped_start and value[snapped_end - 1].isspace():
        snapped_end -= 1
    return snapped_start, snapped_end


def qt_to_codepoint(value: str, utf16_position: int) -> int:
    require(type(utf16_position) is int and utf16_position >= 0, "INVALID_INPUT", "光标位置无效")
    offset = 0
    for index, char in enumerate(value):
        if offset == utf16_position:
            return index
        offset += 2 if ord(char) > 0xFFFF else 1
        require(offset <= utf16_position, "INVALID_INPUT", "光标不能位于 Unicode 代理对中间")
    require(offset == utf16_position, "INVALID_INPUT", "光标超出文本范围")
    return len(value)


def codepoint_to_qt(value: str, position: int) -> int:
    require(type(position) is int and 0 <= position <= len(value), "INVALID_INPUT", "文本位置越界")
    return len(value[:position].encode("utf-16-le")) // 2
