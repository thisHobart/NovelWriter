"""Bind model-authored asset views and compile shot prompts from exact asset variants."""

from __future__ import annotations

from copy import deepcopy

from .contracts import ArtifactRecord, JsonObject
from .models import ASSET_KINDS, VIEWS, fingerprint, require
from .validation import (
    PLACEHOLDER,
    SPEC_FIELDS,
    local_issues,
    validate_generated_asset_prompts,
    view_input_hash,
)


def finalize_asset_prompts(kind: str, spec: JsonObject) -> JsonObject:
    """Keep model-authored view text; only order views and bind internal provenance."""
    validate_generated_asset_prompts(kind, spec)
    value = deepcopy(spec)
    entity_kind = next(key for key, value in ASSET_KINDS.items() if value == kind)
    views = {view["view_id"]: view for view in value["views"]}
    value["views"] = [views[view_id] for view_id, _ in VIEWS[entity_kind]]
    for view in value["views"]:
        view["template_ref"] = "asset-prompts-llm-v1"
        view.setdefault("source_refs", deepcopy(value.get("source_refs", [])))
        view["content_hash"] = fingerprint([view["positive_prompt"], view["negative_prompt"]])
        view["spec_input_hash"] = view_input_hash(kind, value, view)
    value["prompts_generated"] = True
    return value


def compile_shots(
    storyboard: ArtifactRecord,
    specs: list[ArtifactRecord],
    *,
    asset_names: dict[str, str] | None = None,
) -> JsonObject:
    """No remote calls. Every shot uses exact spec variants, never display-name matching."""
    by_entity = {(s["content"]["asset_id"], s["content"]["variant_id"]): s for s in specs}
    result = []
    used = {}
    for shot in storyboard["content"]["shots"]:
        prompts, refs = [], []
        for ref in [shot["scene_ref"], *shot["characters"], *shot["props"]]:
            spec = by_entity.get((ref["asset_id"], ref["variant_id"]))
            require(spec is not None, "EXPORT_INCOMPLETE", "镜头引用的资产状态缺少设定")
            value = spec["content"]
            require(
                not any(i["severity"] == "error" for i in local_issues(spec["kind"], value)),
                "EXPORT_INCOMPLETE",
                "资产设定或视图提示词不完整或已过期，请重新生成并核对",
            )
            available = [
                v
                for v in value.get("views", [])
                if v.get("positive_prompt") and not PLACEHOLDER.search(v["positive_prompt"])
            ]
            require(bool(available), "EXPORT_INCOMPLETE", "资产视图提示词尚未生成")
            # Shot descriptions use the stable identity/layout, not white-background asset-sheet framing.
            fields = SPEC_FIELDS[spec["kind"]]
            name = {**(asset_names or {}), **shot.get("character_labels", {})}.get(
                ref["asset_id"],
                {"character_spec": "人物", "scene_spec": "场景", "prop_spec": "道具"}[spec["kind"]],
            )
            description = "，".join(
                dict.fromkeys(value[field].strip() for field in fields if value[field].strip())
            )
            if spec["kind"] == "scene_spec":
                description = f"{value['location']}，{value['time']}，{value['spatial_structure']}，{value['height_notes']}；中央{value['center']}；左区{value['left']}；右区{value['right']}；后方{value['rear']}；入口{value['entrances']}；标志陈设{value['landmarks']}；主光{value['main_light']}，辅光{value['fill_light']}，{value['atmosphere']}"
            prompts.append(name + "：" + description)
            refs.append({**ref, "spec_version_id": spec["id"]})
            used[spec["id"]] = spec
        visible = dict.fromkeys(
            part.strip().rstrip("。")
            for part in (
                f"{shot['shot_size']}，{shot['camera_movement']}",
                shot["action"],
            )
            if part.strip()
        )
        combined = (
            "。".join(visible)
            + "。\n固定设定（用于身份、布局和状态连续性，不要求全部细节同时可见）：\n"
        )
        combined += "\n".join(dict.fromkeys(prompts))
        result.append(
            {
                "shot_id": shot["shot_id"],
                "resolved_prompt": combined,
                "negative_prompt": "画面变形、身份与服装漂移、布局和道具状态矛盾、文字水印、模糊",
                "video_motion_prompt": shot.get("video_motion_prompt", ""),
                "asset_refs": refs,
                "source_refs": deepcopy(shot.get("source_refs", [])),
            }
        )
    return {
        "storyboard_version_id": storyboard["id"],
        "asset_spec_refs": list(used),
        "shots": result,
    }
