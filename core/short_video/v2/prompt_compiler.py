"""Concrete, view-specific text prompts, compiled from versioned article-based specs."""

from __future__ import annotations

from copy import deepcopy

from .contracts import ArtifactRecord, JsonObject
from .models import ASSET_KINDS, VIEWS, fingerprint, require, text
from .validation import (
    PLACEHOLDER,
    SPEC_FIELDS,
    UNRESOLVED,
    local_issues,
    validate_generated_asset_prompts,
    view_input_hash,
)

NEGATIVE = {
    "character_spec": "额外人物、重复肢体、手部畸形、面部变形、服装漂移、文字水印、复杂背景、过曝、模糊",
    "scene_spec": "人物、人影、人体、遮挡通道、门窗漂移、空间结构矛盾、文字水印、模糊",
    "prop_spec": "人物、手部、多余道具、结构变形、比例失调、复杂背景、文字水印、模糊",
}


def plan_views(kind: str, spec: JsonObject) -> list[JsonObject]:
    entity_kind = next(k for k, v in ASSET_KINDS.items() if v == kind)
    old = {v["view_id"]: v for v in spec.get("views", [])}
    defaults = []
    for view_id, label in VIEWS[entity_kind]:
        composition = (
            "头部特写"
            if view_id == "head_front"
            else ("完整全身" if entity_kind == "character" else "完整主体")
        )
        defaults.append(
            {
                "view_id": view_id,
                "label": label,
                "camera_view": label,
                "composition": composition,
                "required": True,
                "positive_prompt": "",
                "negative_prompt": "",
                **old.pop(view_id, {}),
            }
        )
    return defaults + list(old.values())


def compile_asset(kind: str, spec: JsonObject) -> JsonObject:
    require(kind in SPEC_FIELDS, "INVALID_INPUT", "不支持的资产设定类型")
    value = deepcopy(spec)
    for field in SPEC_FIELDS[kind]:
        text(value.get(field), field)
        require(not PLACEHOLDER.search(value[field]), "INVALID_INPUT", f"{field}仍有模板占位符")
        require(
            not UNRESOLVED.search(value[field]),
            "INVALID_INPUT",
            f"{field}尚未填写具体候选值，请先补充并核对",
        )
    value["views"] = plan_views(kind, value)
    style = value.get("style", "写实真人短剧")
    for view in value["views"]:
        direction, composition = view["camera_view"], view["composition"]
        if kind == "character_spec":
            detail = "，".join(
                value[k]
                for k in (
                    "age",
                    "gender",
                    "appearance",
                    "hair",
                    "makeup",
                    "clothes",
                    "accessories",
                    "period",
                    "state",
                )
            )
            prompt = (
                f"{style}人物定妆照，{detail}，{direction}，{composition}，"
                f"{value['identity_constraints']}，纯白背景，光线均匀，真实皮肤质感，五官稳定，服装材质清晰，高清细节。"
            )
        elif kind == "scene_spec":
            # Cardinal layout is a world-space description, not a screen-left instruction.
            prompt = (
                f"{style}场景资产描述，{value['drama_type']}，{value['era']}，{value['location']}，{value['time']}，"
                f"空间结构：{value['spatial_structure']}；高度：{value['height_notes']}。"
                f"以固定布局坐标为准：中央{value['center']}，布局左区{value['left']}，布局右区{value['right']}，"
                f"后区{value['rear']}；出入口{value['entrances']}；标志陈设{value['landmarks']}。"
                f"主光{value['main_light']}，辅光{value['fill_light']}，{value['atmosphere']}，"
                f"{direction}，{composition}，{value['composition']}，空置无人，主要通道无遮挡，真实摄影质感，高清细节。"
                "视角变化只改变摄影机位置，陈设保持固定布局，不将左右区域镜像互换。"
            )
        else:
            prompt = (
                f"{style}道具资产描述，{value['era']}的{value['name']}，{value['description']}，"
                f"大小形状{value['size_shape']}，材质{value['material']}，主色{value['main_color']}，"
                f"辅助色{value['secondary_color']}，结构{value['components']}，当前状态{value['state']}，"
                f"{direction}，{composition}，单个道具独立展示，纯白背景，柔和均匀光线，无人物无手部，"
                "完整轮廓与结构细节，比例自然，真实摄影质感，高清细节。"
            )
        view["positive_prompt"] = prompt
        view["negative_prompt"] = value.get("negative_prompt") or NEGATIVE[kind]
        view["template_ref"] = "article-asset-compiler-v1"
        view["source_refs"] = deepcopy(value.get("source_refs", []))
        view["content_hash"] = fingerprint([prompt, view["negative_prompt"]])
        view["spec_input_hash"] = view_input_hash(kind, value, view)
    value["prompts_generated"] = True
    return value


def finalize_asset_prompts(
    kind: str, spec: JsonObject, *, expected_view_ids: list[str] | None = None
) -> JsonObject:
    """Keep model-authored view text; only order views and bind internal provenance."""
    validate_generated_asset_prompts(kind, spec, expected_view_ids=expected_view_ids)
    value = deepcopy(spec)
    value["views"] = plan_views(kind, value)
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
        prompts, negatives, refs = [], [], []
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
            description = "，".join(value[field] for field in fields)
            if spec["kind"] == "scene_spec":
                description = f"{value['location']}，{value['time']}，{value['spatial_structure']}，{value['height_notes']}；中央{value['center']}；左区{value['left']}；右区{value['right']}；后方{value['rear']}；入口{value['entrances']}；标志陈设{value['landmarks']}；主光{value['main_light']}，辅光{value['fill_light']}，{value['atmosphere']}"
            prompts.append(name + "：" + description)
            negatives.append(value.get("negative_prompt") or NEGATIVE[spec["kind"]])
            refs.append({**ref, "spec_version_id": spec["id"]})
            used[spec["id"]] = spec
        combined = (
            f"{shot['visual_prompt_draft']}。{shot['shot_size']}，{shot['camera_movement']}。"
            f"可见动作：{shot['action']}。固定设定：" + "\n".join(prompts)
        )
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
