"""Shot changes proposed in chat: the affected-shot list, the revised shots and merging them.

The model writes both as fenced JSON blocks at the end of an ordinary reply, naming shots by
number and assets as 「名称（状态）」, the way the attached storyboard shows them; they are mapped
back to the shots and catalog entries here. Replies saved earlier used ids and English keys and
are still read. Nothing here touches the project: revised shots are merged into the editor
draft only when the user applies them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy

from .contracts import JsonObject
from .models import WorkspaceError, fingerprint, reject_json_constant, require
from .storyboard_chat import asset_label, asset_names, shot_dialogue_text, shot_span
from .validation import KIND_LABELS, validate_shot

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
)
FIELD_LABELS = {
    "shot_size": "取景范围",
    "camera_movement": "镜头运动",
    "scene_ref": "场景",
    "characters": "人物",
    "props": "道具",
    "action": "动作",
    "dialogue": "对白",
    "narration": "旁白",
    "sound": "音效",
    "duration_ms": "时长",
}
# Free text typed into the editor that replaces parsed fields until the draft is saved.
_TYPED_FIELDS = ("dialogue_text", "character_text")


def reply_blocks(content: str) -> dict[str, str]:
    """The last block of each kind in a reply."""
    return {tag: body for tag, body in _BLOCK.findall(content)}


def display_text(content: str) -> str:
    """A reply as shown in chat: each block becomes a one-line note."""
    return _BLOCK.sub(lambda m: _BLOCK_NOTES[m.group(1)], content).rstrip()


def _number(entry: JsonObject) -> int | None:
    """The shot number an entry gives: 「镜号」, or "order" in replies saved earlier."""
    number = entry.get("镜号", entry.get("order"))
    return number if type(number) is int and number > 0 else None


def _position(
    entry: JsonObject, positions: Mapping[str, int], order: Sequence[str] | None, count: int
) -> int | None:
    """Where an entry's shot is in the current draft: by id, else by its number.

    order is the shot order when the request was sent, so numbers still find their shots after
    shots were added or removed; without it the number counts in the current draft.
    """
    index = positions.get(entry.get("shot_id"))
    number = _number(entry)
    if index is None and number is not None:
        if order:
            index = positions.get(order[number - 1]) if number <= len(order) else None
        elif number <= count:
            index = number - 1
    return index


def _block_numbers(body: str, shot_ids: Sequence[str] | None) -> list[int]:
    """Shot numbers a block names; entries that give only an id are looked up in shot_ids."""
    try:
        value = _load(body)
    except WorkspaceError:
        return []
    numbers = []
    for entry in value["shots"]:
        if not isinstance(entry, dict):
            continue
        number = _number(entry)
        if number is not None:
            numbers.append(number)
        elif shot_ids and entry.get("shot_id") in shot_ids:
            numbers.append(list(shot_ids).index(entry["shot_id"]) + 1)
    return numbers


def _history_note(tag: str, body: str, shot_ids: Sequence[str] | None) -> str:
    span = shot_span(_block_numbers(body, shot_ids))
    if tag == AFFECTED_TAG:
        return f"〔列出了受影响的镜头：{span}〕" if span else "〔列出了受影响的镜头〕"
    return f"〔给出了{span or '这几镜'}修改后的内容；是否写入以当前分镜为准〕"


def history_notes(content: str, shot_ids: Sequence[str] | None = None) -> list[str]:
    """One note per block of a past reply, in order."""
    return [_history_note(tag, body, shot_ids) for tag, body in _BLOCK.findall(content)]


def history_text(content: str, shot_ids: Sequence[str] | None = None) -> str:
    """A past reply as later rounds send it: each block becomes a note naming its shots.

    shot_ids is the storyboard order when the reply was written, for blocks that give only ids.
    """
    return _BLOCK.sub(lambda m: _history_note(m.group(1), m.group(2), shot_ids), content).rstrip()


def reply_prose(content: str) -> str:
    """A reply without its blocks."""
    return _BLOCK.sub("", content).strip()


def _load(body: str) -> JsonObject:
    """The block's JSON with its shot list under "shots"; current replies call it 「镜头」."""
    try:
        value = json.loads(body, parse_constant=reject_json_constant)
    except (ValueError, WorkspaceError) as exc:
        raise WorkspaceError("INVALID_OUTPUT", "代码块不是有效的 JSON") from exc
    if isinstance(value, dict) and "shots" not in value and "镜头" in value:
        value = {**value, "shots": value["镜头"]}
    require(
        isinstance(value, dict) and isinstance(value.get("shots"), list),
        "INVALID_OUTPUT",
        "代码块缺少“镜头”列表",
    )
    return value


def _words(value: object) -> str:
    if isinstance(value, list):
        return "、".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip() if isinstance(value, (str, int, float)) else ""


def _milliseconds(value: object, scale: int = 1) -> int | None:
    """A length in whole milliseconds, at least 0.1 second; anything else is None.

    Current replies give seconds (scale 1000); replies saved earlier gave milliseconds.
    """
    # NaN and Infinity never get here: the block's JSON is read without them.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = round(value * scale)
    return value if value >= 100 else None


def affected_shots(
    content: str, shots: list[JsonObject], order: Sequence[str] | None = None
) -> JsonObject | None:
    """The reply's affected-shot list matched to the current draft, or None without a list.

    A shot is found by its number (see _position for order) or, in replies saved earlier, by
    shot_id; shots no longer in the draft are dropped. An item keeps the suggested length as
    duration_ms when the reply gives one.
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
        index = _position(entry, positions, order, len(shots))
        if index is None or index in found:
            continue
        found[index] = {
            "shot_id": shots[index]["shot_id"],
            "order": index + 1,
            "fields": _words(entry.get("要改", entry.get("fields"))),
            "change": _words(entry.get("改法", entry.get("change"))),
        }
        if "建议时长" in entry:
            duration = _milliseconds(entry["建议时长"], 1000)
        else:
            duration = _milliseconds(entry.get("duration_ms"))
        if duration is not None:
            found[index]["duration_ms"] = duration
    missing = value.get("缺少的资产", value.get("missing_assets"))
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
    return shot


def check_merged(shot: JsonObject, entities: list[JsonObject]) -> None:
    require(
        type(shot.get("duration_ms")) is int and shot["duration_ms"] > 0,
        "INVALID_OUTPUT",
        "时长必须是正整数毫秒",
    )
    validate_shot(shot, {e["asset_id"]: e for e in entities})


_TEXT_FIELDS = ("shot_size", "camera_movement", "action", "narration", "sound")


def _ref(entity: JsonObject, variant: JsonObject) -> JsonObject:
    return {"asset_id": entity["asset_id"], "variant_id": variant["variant_id"]}


def _found(name: str, candidates: list[JsonObject], names: Mapping[str, str]) -> list[JsonObject]:
    """Entries a written name means: the name as the material shows it, else a name or alias."""
    shown = [e for e in candidates if names[e["asset_id"]] == name]
    return shown or [e for e in candidates if name in {e["name"], *e.get("aliases", [])}]


def _choices(found: list[JsonObject], names: Mapping[str, str]) -> str:
    return "或".join(f"“{names[e['asset_id']]}”" for e in found)


def _asset(
    label: object, kind: str, entities: list[JsonObject], names: Mapping[str, str]
) -> tuple[JsonObject | None, str]:
    """「名称（状态）」 as a catalog reference, or None and what is wrong with it."""
    word = KIND_LABELS[kind]
    written = label.strip().replace("(", "（").replace(")", "）") if isinstance(label, str) else ""
    if not written:
        return None, f"{word}没有写名称"
    candidates = [e for e in entities if e["kind"] == kind]
    for entity in candidates:
        for variant in entity["variants"]:
            if written == f"{names[entity['asset_id']]}（{variant['label']}）":
                return _ref(entity, variant), ""
    match = re.fullmatch(r"(.+?)（(.*)）", written)
    name, state = (match.group(1).strip(), match.group(2).strip()) if match else (written, "")
    found = _found(name, candidates, names)
    if not found:
        return None, f"{word}“{written}”不在资产目录里"
    if len(found) > 1:
        return None, f"{word}“{name}”对应多个对象，要写成{_choices(found, names)}"
    entity = found[0]
    if not state and len(entity["variants"]) == 1:
        return _ref(entity, entity["variants"][0]), ""
    variant = next((v for v in entity["variants"] if v["label"] == state), None)
    if variant is None:
        what = f"没有“{state}”这个状态" if state else "没有写状态"
        states = "｜".join(v["label"] for v in entity["variants"])
        return None, f"{word}“{names[entity['asset_id']]}”{what}，资产目录里的状态：{states}"
    return _ref(entity, variant), ""


def _speaker(
    label: object, entities: list[JsonObject], names: Mapping[str, str]
) -> tuple[JsonObject | None, str]:
    """The character a dialogue line names, voice-only ones included."""
    written = label.strip() if isinstance(label, str) else ""
    if not written:
        return None, "对白没有写说话人"
    characters = [e for e in entities if e["kind"] == "character"]
    # 「寻井人（夏装）」 names the same speaker as 「寻井人」.
    found = _found(written, characters, names) or _found(
        re.sub(r"\s*[（(].*[）)]$", "", written), characters, names
    )
    if len(found) == 1:
        return found[0], ""
    if not found:
        return None, f"说话人“{written}”不在资产目录里"
    return None, f"说话人“{written}”对应多个人物，要写成{_choices(found, names)}"


def _labels(value: object) -> list[str] | None:
    """Names in a 人物 or 道具 list; text is split at 「、」 outside brackets, since a state may
    contain one. 「无」 means none; None when the value is neither a list nor text."""
    if isinstance(value, str):
        items, depth, current = [], 0, ""
        for char in value:
            depth += (char in "（(") - (char in "）)")
            if char in "、,，" and depth <= 0:
                items.append(current)
                current = ""
            else:
                current += char
        value = [*items, current]
    if not isinstance(value, list):
        return None
    return [str(v).strip() for v in value if str(v).strip() not in ("", "无")]


def _named_fields(
    entry: JsonObject, entities: list[JsonObject], names: Mapping[str, str]
) -> tuple[JsonObject, list[str]]:
    """Draft fields from a revised shot written with the form's names, and what doesn't fit."""
    if FIELD_LABELS["shot_size"] not in entry and "景别" in entry:
        entry = {**entry, FIELD_LABELS["shot_size"]: entry["景别"]}
    absent = [label for label in FIELD_LABELS.values() if label not in entry]
    if absent:
        return {}, ["缺少" + "、".join(absent)]
    fields = {field: entry[FIELD_LABELS[field]] for field in _TEXT_FIELDS}
    problems = []
    fields["scene_ref"], problem = _asset(entry["场景"], "scene", entities, names)
    problems += [problem] if problem else []
    for field, kind in (("characters", "character"), ("props", "prop")):
        labels = _labels(entry[FIELD_LABELS[field]])
        if labels is None:
            problems.append(f"{FIELD_LABELS[field]}要写成名称列表")
            continue
        fields[field] = []
        for label in labels:
            ref, problem = _asset(label, kind, entities, names)
            if ref is None:
                problems.append(problem)
            else:
                fields[field].append(ref)
    lines = entry["对白"] if entry["对白"] not in ("", "无", None) else []
    fields["dialogue"] = []
    if not isinstance(lines, list):
        problems.append("对白要写成列表")
        lines = []
    for line in lines:
        if not isinstance(line, dict):
            problems.append("对白的每一句要写成{说话人, 台词, 画外}")
            continue
        speaker, problem = _speaker(line.get("说话人"), entities, names)
        voice_over = line.get("画外", False)
        if speaker is None:
            problems.append(problem)
        elif type(voice_over) is not bool:
            problems.append("“画外”要写 true 或 false")
        else:
            fields["dialogue"].append(
                {"character_id": speaker["asset_id"], "text": line.get("台词"), "voice_over": voice_over}
            )
    fields["duration_ms"] = _milliseconds(entry["时长"], 1000)
    if fields["duration_ms"] is None:
        problems.append("时长要写成秒数，如 6.5")
    return fields, problems


def revised_shots(
    content: str,
    base: dict[str, JsonObject],
    entities: list[JsonObject],
    order: Sequence[str] | None = None,
) -> tuple[dict[str, JsonObject], dict[str, str]]:
    """Merged shots for each requested shot that passes the checks, and reasons for the rest.

    base maps the requested shot ids to the shots the model was given (or the current draft);
    order is the shot order when the request was sent, mapping 「镜号」 back to those shots.
    """
    body = reply_blocks(content).get(REVISED_TAG)
    require(body is not None, "INVALID_OUTPUT", f"回复缺少 {REVISED_TAG} 代码块")
    value = _load(body)
    names = asset_names(entities)
    returned = {}
    for entry in value["shots"]:
        if not isinstance(entry, dict):
            continue
        shot_id, number = entry.get("shot_id"), _number(entry)
        if not isinstance(shot_id, str) and number is not None:
            if order:
                shot_id = order[number - 1] if number <= len(order) else None
            else:
                shot_id = next((s for s, shot in base.items() if shot.get("order") == number), None)
        if isinstance(shot_id, str):
            returned[shot_id] = entry
    accepted, rejected = {}, {}
    for shot_id, current in base.items():
        entry = returned.get(shot_id)
        if entry is None:
            rejected[shot_id] = "AI 没有给出这一镜"
            continue
        if "shot_id" in entry:
            # Saved earlier: English keys and catalog ids.
            absent = [FIELD_LABELS[k] for k in EDIT_FIELDS if k not in entry]
            fields = {k: entry[k] for k in EDIT_FIELDS if k in entry}
            problems = ["缺少" + "、".join(absent)] if absent else []
        else:
            fields, problems = _named_fields(entry, entities, names)
        if problems:
            rejected[shot_id] = "；".join(problems)
            continue
        merged = merge_shot(current, fields)
        try:
            check_merged(merged, entities)
        except WorkspaceError as exc:
            rejected[shot_id] = str(exc)
            continue
        accepted[shot_id] = merged
    return accepted, rejected


def reply_problems(
    content: str,
    base: dict[str, JsonObject],
    entities: list[JsonObject],
    order: Sequence[str] | None = None,
) -> tuple[int, list[str]]:
    """How many shots pass, and what a format repair has to fix, numbered by shot."""
    try:
        accepted, rejected = revised_shots(content, base, entities, order)
    except WorkspaceError as exc:
        return 0, [str(exc)]

    def number(shot_id: str) -> object:
        return list(order).index(shot_id) + 1 if order and shot_id in order else base[shot_id].get("order", "?")

    return len(accepted), [f"第{number(sid)}镜：{reason}" for sid, reason in rejected.items()]


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
