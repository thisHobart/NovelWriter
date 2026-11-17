"""Read-only, bounded evidence for discussing one editable storyboard shot."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import TYPE_CHECKING

from .contracts import ArtifactRecord, JsonObject
from .models import WorkspaceError, encode, reference_text, require
from .source import sentence_range, validate_source_ref
from .validation import SPEC_FIELDS

if TYPE_CHECKING:
    from .repository import ProjectRepository


def validate_storyboard_reference(value: JsonObject) -> JsonObject:
    require(
        isinstance(value, dict)
        and all(isinstance(value.get(k), str) and value[k] for k in ("shot_id", "storyboard_id"))
        and type(value.get("order")) is int
        and value["order"] > 0
        and type(value.get("unsaved")) is bool,
        "INVALID_INPUT",
        "聊天镜头标注无效",
    )
    return {k: value[k] for k in ("shot_id", "storyboard_id", "order", "unsaved")}


def _cast(shot: JsonObject, entities: list[JsonObject]) -> tuple[list[JsonObject], list[str]]:
    if "character_text" not in shot:
        return deepcopy(shot.get("characters", [])), []
    refs, unresolved = [], []
    for label in shot["character_text"].replace(",", "、").replace("，", "、").split("、"):
        label = label.strip()
        if not label or label == "无":
            continue
        matches = [
            e
            for e in entities
            if e["kind"] == "character"
            and e.get("usage") != "voice_only"
            and label in {e["name"], *e.get("aliases", [])}
        ]
        if len(matches) != 1:
            unresolved.append(label)
            continue
        entity = matches[0]
        refs.append(
            next(
                (
                    deepcopy(r)
                    for r in shot.get("characters", [])
                    if r["asset_id"] == entity["asset_id"]
                ),
                {"asset_id": entity["asset_id"], "variant_id": entity["variants"][0]["variant_id"]},
            )
        )
    return refs, unresolved


def _shot(
    shot: JsonObject, order: int, entities: list[JsonObject], *, neighbour: bool = False
) -> JsonObject:
    fields = ("shot_id", "duration_ms", "scene_ref", "props", "action", "sound")
    if not neighbour:
        fields += ("shot_size", "camera_movement", "visual_prompt_draft")
    result = {k: deepcopy(shot[k]) for k in fields if k in shot}
    result["order"] = order
    result["characters"], unresolved = _cast(shot, entities)
    if "character_text" in shot:
        result["character_text"] = shot["character_text"]
    if unresolved:
        result["unresolved_character_labels"] = unresolved
    if "dialogue_text" in shot:
        # The edit box contains both dialogue and narration; old parsed values are stale.
        result["dialogue_text"] = shot["dialogue_text"]
    else:
        result.update(
            dialogue=deepcopy(shot.get("dialogue", [])), narration=shot.get("narration", "")
        )
    if neighbour:
        refs = [result.get("scene_ref"), *result["characters"], *result.get("props", [])]
        result["asset_labels"] = {
            ref["asset_id"]: e["name"]
            for ref in refs
            if ref
            for e in entities
            if e["asset_id"] == ref["asset_id"]
        }
    return reference_text(result)


OUTLINE_CLIP = 60


def asset_label(ref: JsonObject, entities: list[JsonObject]) -> str:
    entity = next((e for e in entities if e["asset_id"] == ref.get("asset_id")), None)
    if entity is None:
        return str(ref.get("asset_id"))
    variant = next(
        (v for v in entity["variants"] if v["variant_id"] == ref.get("variant_id")), None
    )
    return entity["name"] + ("/" + variant["label"] if variant else "")


def shot_dialogue_text(shot: JsonObject, entities: list[JsonObject]) -> str:
    """Same wording as the storyboard's dialogue column, including unsaved free text."""
    if "dialogue_text" in shot:
        return shot["dialogue_text"]
    names = {e["asset_id"]: e["name"] for e in entities}
    lines = [
        ("画外/" if line.get("voice_over") else "")
        + names.get(line.get("character_id"), line.get("speaker_label", "旁白"))
        + "："
        + line.get("text", "")
        for line in shot.get("dialogue", [])
    ]
    if shot.get("narration"):
        lines.append("旁白：" + shot["narration"])
    return "\n".join(lines)


def _clip(value: str) -> str:
    return value if len(value) <= OUTLINE_CLIP else value[:OUTLINE_CLIP] + "…"


def _outline_entry(
    shot: JsonObject, order: int, entities: list[JsonObject], level: int
) -> JsonObject:
    """level 0 is complete, 1 clips action and dialogue, 2 keeps only assets."""
    entry: JsonObject = {
        "shot_id": shot.get("shot_id"),
        "order": order,
        "duration_ms": shot.get("duration_ms"),
    }
    if shot.get("scene_ref"):
        entry["scene"] = asset_label(shot["scene_ref"], entities)
    if "character_text" in shot:
        entry["characters"] = shot["character_text"]
    else:
        entry["characters"] = [asset_label(r, entities) for r in shot.get("characters", [])]
    entry["props"] = [asset_label(r, entities) for r in shot.get("props", [])]
    if level < 2:
        action, dialogue = shot.get("action", ""), shot_dialogue_text(shot, entities)
        entry["action"] = _clip(action) if level else action
        if dialogue:
            entry["dialogue_text"] = _clip(dialogue) if level else dialogue
    return entry


def storyboard_outline(
    shots: list[JsonObject],
    detailed: set[int],
    entities: list[JsonObject],
    current: int,
    limit: int,
) -> tuple[list[JsonObject], list[str]]:
    """One entry per other shot, shortened in steps and then dropped farthest-first to fit."""
    indexes = [i for i in range(len(shots)) if i not in detailed]
    for level in (0, 1, 2):
        outline = [_outline_entry(shots[i], i + 1, entities, level) for i in indexes]
        if len(encode(outline)) <= limit:
            notes = []
            if level == 1:
                notes.append(f"全稿概要超出预算，动作与对白已截取前{OUTLINE_CLIP}字")
            elif level == 2:
                notes.append("全稿概要超出预算，只保留镜号、时长和资产")
            return outline, notes
    kept = sorted(indexes, key=lambda i: abs(i - current))
    while kept and len(encode([_outline_entry(shots[i], i + 1, entities, 2) for i in kept])) > limit:
        kept.pop()
    kept.sort()
    omitted = len(indexes) - len(kept)
    outline = [_outline_entry(shots[i], i + 1, entities, 2) for i in kept]
    return outline, [f"全稿概要超出预算，只保留镜号、时长和资产，离本镜最远的{omitted}个镜头未列出"]


def catalog_summary(entities: list[JsonObject]) -> list[JsonObject]:
    """Every catalog object with its states, so changes can name what exists and what is missing."""
    return [
        {
            "asset_id": e["asset_id"],
            "kind": e["kind"],
            "name": e["name"],
            **({"aliases": list(e["aliases"])} if e.get("aliases") else {}),
            **({"usage": e["usage"]} if e.get("usage") == "voice_only" else {}),
            "variants": [
                {"variant_id": v["variant_id"], "label": v["label"]} for v in e["variants"]
            ],
        }
        for e in entities
    ]


def _speakers(shot: JsonObject, entities: list[JsonObject]) -> tuple[list[JsonObject], list[str]]:
    """Resolve raw speaker names only when unique; never reuse a stale dialogue identity."""
    refs, unresolved = [], []
    if "dialogue_text" in shot:
        speakers = []
        for line in shot["dialogue_text"].splitlines():
            label, separator, _ = line.replace(":", "：", 1).partition("：")
            label = label.strip().removeprefix("画外/").strip()
            if not separator or label == "旁白":
                continue
            matches = [
                e
                for e in entities
                if e["kind"] == "character" and label in {e["name"], *e.get("aliases", [])}
            ]
            if len(matches) != 1:
                unresolved.append(label)
            else:
                speakers.append(matches[0])
    else:
        ids = {line.get("character_id") for line in shot.get("dialogue", [])}
        speakers = [e for e in entities if e["kind"] == "character" and e["asset_id"] in ids]
    for entity in speakers:
        refs.append(
            next(
                (r for r in shot["characters"] if r["asset_id"] == entity["asset_id"]),
                {"asset_id": entity["asset_id"], "variant_id": entity["variants"][0]["variant_id"]},
            )
        )
    return refs, unresolved


def build_storyboard_chat_context(
    draft: JsonObject,
    shot_id: str,
    storyboard: ArtifactRecord,
    records: Mapping[str, ArtifactRecord],
    *,
    unsaved: bool = False,
    max_source_chars: int = 6000,
    max_outline_chars: int = 12000,
    edit_items: list[JsonObject] | None = None,
) -> JsonObject:
    """Pure projection of a UI snapshot and already-read, provenance-bound records.

    edit_items asks for revised shots: each item names a shot and how to change it, and those
    shots are attached in full instead of in the outline.
    """
    shots = draft.get("shots", [])
    index = next((i for i, s in enumerate(shots) if s["shot_id"] == shot_id), None)
    require(index is not None, "INVALID_INPUT", "当前选中的镜头已不存在，请重新选择")
    positions = {s["shot_id"]: i for i, s in enumerate(shots)}
    edits = []
    for item in edit_items or []:
        require(
            item.get("shot_id") in positions,
            "INVALID_INPUT",
            "要修改的镜头已不在分镜中，请重新选择",
        )
        edits.append((positions[item["shot_id"]], item))
    edits.sort(key=lambda pair: pair[0])
    require(
        len({i for i, _ in edits}) == len(edits), "INVALID_INPUT", "要修改的镜头重复"
    )
    require(
        type(max_source_chars) is int and max_source_chars >= 0, "INVALID_INPUT", "原文预算无效"
    )
    require(
        type(max_outline_chars) is int and max_outline_chars >= 0,
        "INVALID_INPUT",
        "全稿概要预算无效",
    )
    deps = storyboard["dependency_refs"]
    catalog = records.get(deps.get("asset_catalog"))
    entities = catalog["content"]["entities"] if catalog else []
    shot = _shot(shots[index], index + 1, entities)
    missing = []
    result: JsonObject = {
        "reference": validate_storyboard_reference(
            {
                "shot_id": shot_id,
                "storyboard_id": storyboard["id"],
                "order": index + 1,
                "unsaved": unsaved,
            }
        ),
        "shot": shot,
        "adjacent_shots": [
            {
                "relation": "previous" if i < index else "next",
                **_shot(shots[i], i + 1, entities, neighbour=True),
            }
            for i in (index - 1, index + 1)
            if 0 <= i < len(shots)
        ],
        "shot_count": len(shots),
        "storyboard_outline": [],
        "asset_catalog": catalog_summary(entities),
        "assets": [],
        "source_segments": [],
        "requirements": {},
        "missing": missing,
        "sources": {
            "storyboard_id": storyboard["id"],
            **{
                k: deps[k]
                for k in ("asset_catalog", "config", "selection")
                if k in deps
            },
        },
        "notice": "当前镜头来自本轮编辑稿。关联资料仅作依据，未提供内容不能视为已核实；历史建议不代表已应用。",
    }
    if not catalog:
        missing.append("本版分镜绑定的资产目录缺失，不能确认人物与资产身份")
    if edits:
        result["edit_request"] = [
            {
                "shot_id": item["shot_id"],
                "order": i + 1,
                "fields": item.get("fields", ""),
                "change": item.get("change", ""),
            }
            for i, item in edits
        ]
        result["edit_shots"] = [_shot(shots[i], i + 1, entities) for i, _ in edits]
    # Changes to a key plot element span many shots; the outline shows the whole storyboard.
    result["storyboard_outline"], notes = storyboard_outline(
        shots,
        {index - 1, index, index + 1, *(i for i, _ in edits)},
        entities,
        index,
        max_outline_chars,
    )
    missing.extend(notes)
    # Video copy blocks are not attached: their narration is already in the shot, and their
    # multi-shot action summary is covered by the shot, its neighbours and the original text.
    config = records.get(deps.get("config"))
    if config:
        result["requirements"] = {
            k: deepcopy(config["content"][k])
            for k in (
                "audience",
                "tone",
                "required",
                "forbidden",
                "permitted_adaptations",
                "target_duration_ms",
            )
            if k in config["content"]
        }
    else:
        missing.append("本版分镜绑定的项目要求缺失")
    speakers, unresolved = _speakers(shot, entities)
    if unresolved:
        shot["unresolved_speaker_labels"] = unresolved
    references = [shot.get("scene_ref"), *shot["characters"], *shot.get("props", []), *speakers]
    seen = set()
    for ref in references:
        if not ref or (ref["asset_id"], ref["variant_id"]) in seen:
            continue
        seen.add((ref["asset_id"], ref["variant_id"]))
        entity = next((e for e in entities if e["asset_id"] == ref["asset_id"]), None)
        variant = (
            next((v for v in entity["variants"] if v["variant_id"] == ref["variant_id"]), None)
            if entity
            else None
        )
        if not variant:
            missing.append(f"资产或状态未能解析：{ref['asset_id']} / {ref['variant_id']}")
            continue
        asset = {
            **ref,
            "name": entity["name"],
            "kind": entity["kind"],
            "variant_label": variant["label"],
        }
        spec = next(
            (
                r
                for r in records.values()
                if r["kind"] == entity["kind"] + "_spec"
                and r["content"].get("asset_id") == ref["asset_id"]
                and r["content"].get("variant_id") == ref["variant_id"]
                and r["dependency_refs"].get("asset_catalog") == deps.get("asset_catalog")
            ),
            None,
        )
        if spec:
            asset["spec_id"] = spec["id"]
            asset["spec"] = {
                k: deepcopy(spec["content"][k])
                for k in (*SPEC_FIELDS[spec["kind"]], "style")
                if k in spec["content"]
            }
        else:
            asset["notice"] = "基础设定尚未生成或不属于本版资产目录"
        result["assets"].append(asset)
    selection = records.get(deps.get("selection"))
    source = records.get(selection["content"]["source_id"]) if selection else None
    if source:
        result["sources"]["source_id"] = source["id"]
    text = source["content"]["normalized_text"] if source else ""
    ranges = []
    # Shots being revised need their own original text too.
    refs = [
        ref
        for i in dict.fromkeys([index, *(i for i, _ in edits)])
        for ref in shots[i].get("source_refs", [])
    ]
    for ref in refs:
        if not source:
            missing.append("本版分镜绑定的原文或选区缺失")
            break
        try:
            validate_source_ref(ref, source)
        except WorkspaceError:
            missing.append("本镜存在无效原文引用，该引用未提供")
            continue
        for selected in selection["content"]["ranges"]:
            start, end = max(ref["start"], selected["start"]), min(ref["end"], selected["end"])
            if start < end:
                # Model-written coordinates drift; the excerpt is widened or trimmed to sentences.
                start, end = sentence_range(
                    text, start, end, selected["start"], selected["end"]
                )
            if start < end:
                ranges.append((start, end))
    merged = []
    for start, end in sorted(set(ranges)):
        if merged and not text[merged[-1][1] : start].strip():
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    for start, end in merged:
        # Only the text is sent: the model has no numbered original to place coordinates in.
        segment = text[start:end]
        if len(encode([*result["source_segments"], segment])) <= max_source_chars:
            result["source_segments"].append(segment)
        else:
            missing.append(f"一段原文超出补充预算，未提供（{end - start} 字）")
    if not result["source_segments"]:
        missing.append("本轮未提供可用原文片段，不能声称已核实原文")
    return deepcopy(result)


def read_storyboard_chat_context(
    repository: ProjectRepository,
    draft: JsonObject,
    shot_id: str,
    storyboard: ArtifactRecord,
    *,
    unsaved: bool = False,
    edit_items: list[JsonObject] | None = None,
) -> JsonObject:
    """Read only bound dependencies and relevant specs; never replace them with latest drafts."""
    state = repository.load()
    records = {}
    for ref in storyboard["dependency_refs"].values():
        if ref in state["records"]:
            records[ref] = repository.record(ref, state=state)
    selection = records.get(storyboard["dependency_refs"].get("selection"))
    source_id = selection["content"]["source_id"] if selection else None
    if source_id in state["records"]:
        records[source_id] = repository.record(source_id, state=state)
    selected = next((s for s in draft.get("shots", []) if s["shot_id"] == shot_id), {})
    catalog = records.get(storyboard["dependency_refs"].get("asset_catalog"))
    entities = catalog["content"]["entities"] if catalog else []
    # Only specs for this shot are loaded, including possible newly typed cast names.
    projected = _shot(selected, 1, entities)
    speakers, _ = _speakers(projected, entities)
    references = [
        projected.get("scene_ref"),
        *projected["characters"],
        *projected.get("props", []),
        *speakers,
    ]
    keys = {
        f"{e['kind']}_spec:{r['asset_id']}:{r['variant_id']}"
        for r in references
        if r
        for e in entities
        if e["asset_id"] == r["asset_id"]
    }
    for key, ref in state["current"].items():
        if key in keys:
            spec = repository.record(ref, state=state)
            if spec["dependency_refs"].get("asset_catalog") == storyboard["dependency_refs"].get(
                "asset_catalog"
            ):
                records[ref] = spec
    return build_storyboard_chat_context(
        draft, shot_id, storyboard, records, unsaved=unsaved, edit_items=edit_items
    )
