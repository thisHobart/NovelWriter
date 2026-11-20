"""Shot changes proposed in chat: the affected-shot list, the revised shots and merging them.

The model writes both as fenced JSON blocks at the end of an ordinary reply. Nothing here touches
the project: revised shots are merged into the editor draft only when the user applies them.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy

from .contracts import JsonObject
from .models import WorkspaceError, fingerprint, reject_json_constant, require
from .storyboard_chat import asset_label, shot_dialogue_text
from .validation import validate_shot

AFFECTED_TAG = "affected_shots"
REVISED_TAG = "revised_shots"
_BLOCK = re.compile(
    rf"```[ \t]*({AFFECTED_TAG}|{REVISED_TAG})[ \t]*\r?\n(.*?)```", re.DOTALL
)
_BLOCK_NOTES = {
    AFFECTED_TAG: "〔已列出受影响的镜头〕",
    REVISED_TAG: "〔已给出修改后的镜头内容〕",
}

# Every field the model returns for a revised shot; ids, order, sources and video blocks stay.
EDIT_FIELDS = (
    "shot_size",
    "camera_movement",
    "scene_ref",
    "characters",
    "props",
    "action",
    "dialogue",
    "narration",
    "sound",
    "duration_ms",
    "visual_prompt_draft",
)
FIELD_LABELS = {
    "shot_size": "景别",
    "camera_movement": "镜头运动",
    "scene_ref": "场景",
    "characters": "人物",
    "props": "道具",
    "action": "动作",
    "dialogue": "对白",
    "narration": "旁白",
    "sound": "音效",
    "duration_ms": "时长",
    "visual_prompt_draft": "画面提示词",
}
# Free text typed into the editor that replaces parsed fields until the draft is saved.
_TYPED_FIELDS = ("dialogue_text", "character_text")


def reply_blocks(content: str) -> dict[str, str]:
    """The last block of each kind in a reply."""
    return {tag: body for tag, body in _BLOCK.findall(content)}


def display_text(content: str) -> str:
    """A reply as shown in chat: each block becomes a one-line note."""
    return _BLOCK.sub(lambda m: _BLOCK_NOTES[m.group(1)], content).rstrip()


def _load(body: str) -> JsonObject:
    try:
        value = json.loads(body, parse_constant=reject_json_constant)
    except (ValueError, WorkspaceError) as exc:
        raise WorkspaceError("INVALID_OUTPUT", "代码块不是有效的 JSON") from exc
    require(
        isinstance(value, dict) and isinstance(value.get("shots"), list),
        "INVALID_OUTPUT",
        "代码块缺少 shots 列表",
    )
    return value


def _words(value: object) -> str:
    if isinstance(value, list):
        return "、".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip() if isinstance(value, (str, int, float)) else ""


def _milliseconds(value: object) -> int | None:
    """A suggested length: whole milliseconds, at least 0.1 second; anything else is ignored."""
    # NaN and Infinity never get here: the block's JSON is read without them.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = round(value)
    return value if value >= 100 else None


def affected_shots(content: str, shots: list[JsonObject]) -> JsonObject | None:
    """The reply's affected-shot list matched to the current draft, or None without a list.

    A shot is found by shot_id, or by its number when the id is unknown; shots no longer in the
    draft are dropped. An item keeps the suggested length as duration_ms when the reply gives one.
    """
    body = reply_blocks(content).get(AFFECTED_TAG)
    if body is None:
        return None
    try:
        value = _load(body)
    except WorkspaceError as exc:
        return {"items": [], "missing_assets": [], "error": str(exc)}
    positions = {s["shot_id"]: i for i, s in enumerate(shots)}
    found: dict[int, JsonObject] = {}
    for entry in value["shots"]:
        if not isinstance(entry, dict):
            continue
        index = positions.get(entry.get("shot_id"))
        order = entry.get("order")
        if index is None and type(order) is int and 1 <= order <= len(shots):
            index = order - 1
        if index is None or index in found:
            continue
        found[index] = {
            "shot_id": shots[index]["shot_id"],
            "order": index + 1,
            "fields": _words(entry.get("fields")),
            "change": _words(entry.get("change")),
        }
        duration = _milliseconds(entry.get("duration_ms"))
        if duration is not None:
            found[index]["duration_ms"] = duration
    missing = value.get("missing_assets")
    return {
        "items": [found[i] for i in sorted(found)],
        "missing_assets": [w for w in (_words(m) for m in missing or []) if w]
        if isinstance(missing, list)
        else [],
    }


def merge_shot(current: JsonObject, fields: JsonObject) -> JsonObject:
    """The draft shot with the model's fields; typed free text is replaced by the new values."""
    shot = {k: deepcopy(v) for k, v in current.items() if k not in _TYPED_FIELDS}
    shot.update(deepcopy(fields))
    labels = current.get("character_labels")
    if isinstance(labels, dict):
        kept = {r["asset_id"]: labels[r["asset_id"]] for r in shot["characters"] if r.get("asset_id") in labels}
        if kept:
            shot["character_labels"] = kept
        else:
            shot.pop("character_labels", None)
    if shot["visual_prompt_draft"] != current.get("visual_prompt_draft"):
        # Same as editing the prompt in the form: an older copy kept beside it would go stale.
        shot.pop("visual_description", None)
    return shot


def check_merged(shot: JsonObject, entities: list[JsonObject]) -> None:
    require(
        type(shot.get("duration_ms")) is int and shot["duration_ms"] > 0,
        "INVALID_OUTPUT",
        "时长必须是正整数毫秒",
    )
    validate_shot(shot, {e["asset_id"]: e for e in entities})


def revised_shots(
    content: str, base: dict[str, JsonObject], entities: list[JsonObject]
) -> tuple[dict[str, JsonObject], dict[str, str]]:
    """Merged shots for each requested shot that passes the checks, and reasons for the rest.

    base maps the requested shot ids to the shots the model was given (or the current draft).
    """
    body = reply_blocks(content).get(REVISED_TAG)
    require(body is not None, "INVALID_OUTPUT", f"回复缺少 {REVISED_TAG} 代码块")
    value = _load(body)
    returned = {
        entry["shot_id"]: entry
        for entry in value["shots"]
        if isinstance(entry, dict) and isinstance(entry.get("shot_id"), str)
    }
    accepted, rejected = {}, {}
    for shot_id, current in base.items():
        entry = returned.get(shot_id)
        if entry is None:
            rejected[shot_id] = "AI 没有给出这一镜"
            continue
        absent = [FIELD_LABELS[k] for k in EDIT_FIELDS if k not in entry]
        if absent:
            rejected[shot_id] = "缺少" + "、".join(absent)
            continue
        merged = merge_shot(current, {k: entry[k] for k in EDIT_FIELDS})
        try:
            check_merged(merged, entities)
        except WorkspaceError as exc:
            rejected[shot_id] = str(exc)
            continue
        accepted[shot_id] = merged
    return accepted, rejected


def reply_problems(
    content: str, base: dict[str, JsonObject], entities: list[JsonObject]
) -> tuple[int, list[str]]:
    """How many shots pass, and what a format repair has to fix, numbered by shot."""
    try:
        accepted, rejected = revised_shots(content, base, entities)
    except WorkspaceError as exc:
        return 0, [str(exc)]
    return len(accepted), [
        f"第{base[shot_id].get('order', '?')}镜（{shot_id}）：{reason}"
        for shot_id, reason in rejected.items()
    ]


def _shown(shot: JsonObject, field: str, entities: list[JsonObject]) -> str:
    if field == "scene_ref":
        return asset_label(shot["scene_ref"], entities) if shot.get("scene_ref") else ""
    if field == "characters":
        if "character_text" in shot:
            return shot["character_text"]
        return "、".join(asset_label(r, entities) for r in shot.get("characters", []))
    if field == "props":
        return "、".join(asset_label(r, entities) for r in shot.get("props", []))
    if field == "dialogue":
        return shot_dialogue_text(shot, entities)
    if field == "duration_ms":
        return f"{shot['duration_ms'] / 1000:g} 秒" if shot.get("duration_ms") else ""
    return str(shot.get(field, ""))


def shot_changes(
    current: JsonObject, merged: JsonObject, entities: list[JsonObject]
) -> list[tuple[str, str, str]]:
    """(label, before, after) for each visible field that differs; narration shows with dialogue."""
    return [
        (FIELD_LABELS[field], before, after)
        for field in EDIT_FIELDS
        if field != "narration"
        for before, after in [(_shown(current, field, entities), _shown(merged, field, entities))]
        if before != after
    ]


def shot_fingerprint(shot: JsonObject) -> str:
    """Content identity of a draft shot; position and timeline move when other shots change."""
    return fingerprint({k: v for k, v in shot.items() if k not in ("order", "start_ms", "end_ms")})


def edit_base(shots: list[JsonObject], shot_ids: list[str]) -> dict[str, str]:
    """Fingerprints of the requested shots, to tell whether they were edited after the request."""
    return {s["shot_id"]: shot_fingerprint(s) for s in shots if s["shot_id"] in shot_ids}


def validate_storyboard_edit(value: object) -> JsonObject:
    require(
        isinstance(value, dict)
        and isinstance(value.get("shot_ids"), list)
        and bool(value["shot_ids"])
        and all(isinstance(s, str) and s.strip() for s in value["shot_ids"])
        and len(set(value["shot_ids"])) == len(value["shot_ids"])
        and isinstance(value.get("base"), dict)
        and set(value["base"]) == set(value["shot_ids"])
        and all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) for v in value["base"].values()),
        "INVALID_INPUT",
        "聊天镜头修改标注无效",
    )
    return {"shot_ids": list(value["shot_ids"]), "base": dict(value["base"])}
