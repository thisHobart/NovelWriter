"""Deterministic v2 contracts and local checks, independent of model opinions."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterator
from copy import deepcopy
from typing import Any

from .contracts import ArtifactRecord, JsonObject
from .models import (
    ASSET_KINDS,
    WorkspaceError,
    fingerprint,
    positive_int,
    require,
    text,
    validate_choice,
    validate_config,
)
from .source import validate_ranges, validate_source_ref

PLACEHOLDER = re.compile(
    r"\{[A-Za-z_][A-Za-z_0-9]*\}|\{\{[^{}]+\}\}|【(?:道具名称|时代背景|年龄|性别|[^】]*待填[^】]*)】"
)
UNRESOLVED = re.compile(r"^(?:原文)?(?:未明确|未描述|待填写|待确定|待确认|待定)[。；，\s]*$|不设定")
SPEC_FIELDS = {
    "character_spec": (
        "age",
        "gender",
        "appearance",
        "hair",
        "makeup",
        "clothes",
        "accessories",
        "period",
        "state",
        "identity_constraints",
    ),
    "scene_spec": (
        "drama_type",
        "era",
        "location",
        "time",
        "spatial_structure",
        "height_notes",
        "center",
        "left",
        "right",
        "rear",
        "entrances",
        "landmarks",
        "main_light",
        "fill_light",
        "atmosphere",
        "composition",
    ),
    "prop_spec": (
        "name",
        "description",
        "era",
        "size_shape",
        "material",
        "main_color",
        "secondary_color",
        "components",
        "state",
    ),
}


def view_input_hash(kind: str, spec: JsonObject, view: JsonObject) -> str:
    """Bind a view's text to the exact visual design and camera inputs."""
    return fingerprint(
        {
            "kind": kind,
            "spec": {
                key: spec.get(key)
                for key in (
                    *SPEC_FIELDS[kind],
                    "asset_id",
                    "variant_id",
                    "style",
                    "negative_prompt",
                )
            },
            "view": {key: view.get(key) for key in ("view_id", "camera_view", "composition")},
        }
    )


def bind_view_prompts(kind: str, content: JsonObject, parent: JsonObject | None = None) -> None:
    """Retain stale text for review; explicit text edits can establish a new binding."""
    previous = {v["view_id"]: v for v in (parent or {}).get("views", [])}
    for view in content.get("views", []):
        current_hash = view_input_hash(kind, content, view)
        old = previous.get(view["view_id"])
        if old and all(view.get(k) == old.get(k) for k in ("positive_prompt", "negative_prompt")):
            if view.get("spec_input_hash") != current_hash:
                view["spec_input_hash"] = old["spec_input_hash"]
        else:
            view["spec_input_hash"] = current_hash
        view["content_hash"] = fingerprint(
            [view.get("positive_prompt", ""), view.get("negative_prompt", "")]
        )


def rows(value: Any, label: str, *, key: str = "id", empty: bool = False) -> list[JsonObject]:
    require(
        isinstance(value, list)
        and (empty or bool(value))
        and all(isinstance(r, dict) for r in value),
        "INVALID_OUTPUT",
        f"{label}必须为对象列表",
    )
    if key:
        ids = [text(row.get(key), label + "编号") for row in value]
        require(len(set(ids)) == len(ids), "INVALID_OUTPUT", f"{label}编号重复")
    return value


def strings(value: Any, label: str) -> None:
    require(isinstance(value, list), "INVALID_OUTPUT", f"{label}必须为文字列表")
    for item in value:
        text(item, label)


def walk_source_refs(value: Any) -> Iterator[JsonObject]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "source_refs":
                require(isinstance(item, list), "INVALID_OUTPUT", "source_refs 必须为列表")
                yield from item
            else:
                yield from walk_source_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_source_refs(item)


def _dialogue(lines: object) -> None:
    for line in rows(lines, "对白", key=None, empty=True):
        text(line.get("text"), "对白")
        text(line.get("speaker_label", line.get("character_id")), "说话者")
        if "voice_over" in line:
            require(type(line["voice_over"]) is bool, "INVALID_OUTPUT", "画外音标志必须为布尔值")


def recalculate_timeline(content: JsonObject) -> JsonObject:
    content = deepcopy(content)
    cursor = 0
    for index, shot in enumerate(rows(content.get("shots"), "镜头", key="shot_id"), 1):
        duration = positive_int(shot.get("duration_ms"), "镜头时长")
        shot.update(order=index, start_ms=cursor, end_ms=cursor + duration)
        cursor += duration
    return content


def _asset_ref(value: Any, kind: str, entities: dict[str, JsonObject]) -> JsonObject:
    require(isinstance(value, dict), "INVALID_OUTPUT", "资产引用必须为对象")
    asset_id, variant = (
        text(value.get("asset_id"), "资产编号"),
        text(value.get("variant_id"), "资产状态编号"),
    )
    entity = entities.get(asset_id)
    require(
        entity is not None and entity["kind"] == kind, "INVALID_OUTPUT", f"引用了未知{kind}实体"
    )
    require(
        variant in {v["variant_id"] for v in entity["variants"]},
        "INVALID_OUTPUT",
        "引用了未知资产状态",
    )
    return entity


def validate_content(
    kind: str,
    content: object,
    *,
    resolve: Callable[[str], ArtifactRecord] | None = None,
    dependencies: dict[str, str] | None = None,
) -> JsonObject:
    """Normalize calculated fields; reject malformed data before it reaches storage."""
    require(isinstance(content, dict), "INVALID_OUTPUT", "内容必须是对象")
    value = deepcopy(content)
    dependencies = dependencies or {}
    if kind == "config":
        return validate_config(value)
    if kind == "source":
        text(value.get("normalized_text"), "原文")
        require(
            value.get("text_sha256")
            == hashlib.sha256(value["normalized_text"].encode("utf-8")).hexdigest(),
            "INVALID_INPUT",
            "原文指纹不一致",
        )
        validate_choice(value.get("origin"), {"paste", "file"}, "来源类型")
        rows(value.get("paragraphs"), "段落", key="paragraph_id")
    elif kind == "selection":
        require(resolve is not None, "INVALID_INPUT", "选区缺少原文")
        source = resolve(value.get("source_id"))
        require(
            source["kind"] == "source" and dependencies.get("source") == source["id"],
            "INVALID_INPUT",
            "选区来源绑定错误",
        )
        value["ranges"] = validate_ranges(source["content"]["normalized_text"], value.get("ranges"))
        selected = "\n".join(
            source["content"]["normalized_text"][r["start"] : r["end"]] for r in value["ranges"]
        )
        require(
            value.get("selected_text_sha256")
            == hashlib.sha256(selected.encode("utf-8")).hexdigest(),
            "INVALID_INPUT",
            "选区指纹不一致",
        )
    elif kind == "novel_copy":
        for key in ("opening", "cliffhanger"):
            require(isinstance(value.get(key), dict), "INVALID_OUTPUT", f"缺少{key}")
            text(value[key].get("text"), key)
        for row in rows(value.get("paragraphs"), "推文段落"):
            text(row.get("text"), "正文")
        for row in rows(value.get("changes", []), "删改说明", key=None, empty=True):
            validate_choice(
                row.get("type"), {"delete", "compress", "rephrase", "adapt"}, "删改类型"
            )
            text(row.get("reason"), "删改原因")
    elif kind == "fact_table":
        for fact in rows(value.get("facts"), "共享事实表", key=None):
            text(fact.get("text"), "事实摘要")
            require(bool(fact.get("source_refs")), "INVALID_OUTPUT", "事实摘要必须有原文依据")
        require(
            {"selection", "config"} <= dependencies.keys(),
            "INVALID_OUTPUT",
            "共享事实表缺少原文和配置绑定",
        )
    elif kind == "video_copy":
        roles = set()
        for block in rows(value.get("blocks"), "视频文案块"):
            validate_choice(block.get("role"), {"opening", "body", "cliffhanger"}, "视频块用途")
            roles.add(block["role"])
            text(block.get("narration"), "旁白", empty=True)
            text(block.get("visible_action"), "可见动作", empty=True)
            _dialogue(block.get("dialogue"))
            require(
                bool(
                    block["narration"].strip()
                    or block["visible_action"].strip()
                    or block["dialogue"]
                ),
                "INVALID_OUTPUT",
                "视频块不能全部为空",
            )
        require({"opening", "cliffhanger"} <= roles, "INVALID_OUTPUT", "视频文案缺少开头或结尾悬念")
    elif kind == "asset_catalog":
        entities = rows(value.get("entities"), "资产目录", key="asset_id")
        for entity in entities:
            validate_choice(entity.get("kind"), ASSET_KINDS, "资产类型")
            text(entity.get("name"), "资产名称")
            strings(entity.get("aliases", []), "别名")
            require(
                entity.get("usage", "on_screen") in {"on_screen", "voice_only"},
                "INVALID_OUTPUT",
                "资产用途无效",
            )
            require(
                entity.get("usage") != "voice_only" or entity["kind"] == "character",
                "INVALID_OUTPUT",
                "仅人物可标为声音角色",
            )
            for variant in rows(entity.get("variants"), "资产状态", key="variant_id"):
                text(variant.get("label"), "状态名称")
    elif kind == "storyboard":
        require(resolve is not None, "INVALID_OUTPUT", "分镜缺少来源")
        catalog = resolve(dependencies.get("asset_catalog"))["content"]
        video = resolve(dependencies.get("video_copy"))["content"]
        entities = {e["asset_id"]: e for e in catalog["entities"]}
        blocks = {b["id"] for b in video["blocks"]}
        covered = set()
        value = recalculate_timeline(value)
        for shot in value["shots"]:
            for field in ("shot_size", "camera_movement", "action", "visual_prompt_draft"):
                text(shot.get(field), field)
            for field in ("sound", "narration"):
                text(shot.get(field), field, empty=True)
            _asset_ref(shot.get("scene_ref"), "scene", entities)
            for field, entity_kind in (("characters", "character"), ("props", "prop")):
                for ref in rows(shot.get(field), field, key=None, empty=True):
                    entity = _asset_ref(ref, entity_kind, entities)
                    require(
                        entity.get("usage") != "voice_only",
                        "INVALID_OUTPUT",
                        "仅声音人物不能出现在画面人物列表",
                    )
            _dialogue(shot.get("dialogue"))
            cast = {r["asset_id"] for r in shot["characters"]}
            for line in shot["dialogue"]:
                require(
                    line.get("character_id") in entities
                    and entities[line["character_id"]]["kind"] == "character",
                    "INVALID_OUTPUT",
                    "对白人物不存在",
                )
                require(
                    line["character_id"] in cast or line.get("voice_over") is True,
                    "INVALID_OUTPUT",
                    "对白人物不在本镜，需明确画外音",
                )
            refs = shot.get("video_block_refs")
            strings(refs, "视频块引用")
            require(
                bool(refs) and set(refs) <= blocks, "INVALID_OUTPUT", "镜头引用未知视频块或没有来源"
            )
            covered.update(refs)
        require(covered == blocks, "INVALID_OUTPUT", "分镜没有覆盖全部视频文案块")
    elif kind in SPEC_FIELDS:
        require(resolve is not None, "INVALID_OUTPUT", "资产设定缺少目录")
        catalog = resolve(dependencies.get("asset_catalog"))["content"]
        entities = {e["asset_id"]: e for e in catalog["entities"]}
        entity_kind = next(k for k, v in ASSET_KINDS.items() if v == kind)
        _asset_ref(value, entity_kind, entities)
        for field in SPEC_FIELDS[kind]:
            text(value.get(field), field, empty=True)
        strings(value.get("inferred_fields", []), "设计补充")
        require(
            type(value.get("design_confirmed", False)) is bool,
            "INVALID_OUTPUT",
            "设计补充确认值无效",
        )
        text(value.get("style", "写实真人短剧"), "风格")
        for field in ("positive_prompt", "negative_prompt"):
            text(value.get(field, ""), field, empty=True)
        for view in rows(value.get("views"), "视图", key="view_id", empty=True):
            text(view.get("label"), "视图名称")
            text(view.get("camera_view"), "视角")
            text(view.get("composition"), "构图")
            require(type(view.get("required", True)) is bool, "INVALID_OUTPUT", "视图必需标记无效")
            for field in ("positive_prompt", "negative_prompt"):
                text(view.get(field, ""), field, empty=True)
    elif kind == "prompt_bundle":
        require(
            resolve is not None and "storyboard" in dependencies,
            "INVALID_OUTPUT",
            "镜头提示词缺少分镜绑定",
        )
        board = resolve(dependencies["storyboard"])
        require(
            value.get("storyboard_version_id") == board["id"],
            "INVALID_OUTPUT",
            "提示词分镜版本不一致",
        )
        require(
            isinstance(value.get("asset_spec_refs"), list)
            and set(value["asset_spec_refs"]) <= set(dependencies.values()),
            "INVALID_OUTPUT",
            "提示词资产设定没有精确绑定",
        )
        require(
            {s.get("shot_id") for s in value.get("shots", [])}
            == {s["shot_id"] for s in board["content"]["shots"]},
            "INVALID_OUTPUT",
            "提示词没有对应全部镜头",
        )
        for shot in rows(value.get("shots"), "镜头提示词", key="shot_id"):
            text(shot.get("resolved_prompt"), "画面提示词")
            text(shot.get("negative_prompt"), "负面提示词")
            text(shot.get("video_motion_prompt", ""), "视频动作提示词", empty=True)
    elif kind == "prompt_template":
        for field in ("stage", "instruction"):
            text(value.get(field), field)
    elif kind == "check_report":
        require(value.get("engine") in {"code", "ai", "human"}, "INVALID_OUTPUT", "检查引擎无效")
        require(
            value.get("status") in {"passed", "issues", "unavailable"},
            "INVALID_OUTPUT",
            "检查状态无效",
        )
        for issue in rows(value.get("issues"), "检查问题", key="id", empty=True):
            require(
                issue.get("severity") in {"error", "warning"}, "INVALID_OUTPUT", "问题严重度无效"
            )
            for field in ("rule_id", "target_path", "explanation", "suggestion"):
                text(issue.get(field), field)
    else:
        raise WorkspaceError("INVALID_INPUT", f"未知内容类型：{kind}")
    required = {
        "novel_copy": {"selection", "config"},
        "video_copy": {"novel_copy"},
        "asset_catalog": {"video_copy"},
        "storyboard": {"video_copy", "asset_catalog"},
    }
    if kind in ASSET_KINDS.values():
        require("asset_catalog" in dependencies, "INVALID_INPUT", "资产缺少目录依赖")
    require(required.get(kind, set()) <= dependencies.keys(), "INVALID_INPUT", "缺少必需的来源绑定")
    refs = list(walk_source_refs(value))
    if refs:
        require(resolve is not None, "INVALID_OUTPUT", "缺少原文引用解析器")
        selections, seen, pending = [], set(), list(dependencies.values())
        while pending:
            ref_id = pending.pop()
            if ref_id in seen:
                continue
            seen.add(ref_id)
            dependency = resolve(ref_id)
            if dependency["kind"] == "selection":
                selections.append(dependency["content"])
            pending.extend(dependency["dependency_refs"].values())
        for ref in refs:
            source = resolve(ref.get("source_id")) if isinstance(ref, dict) else None
            require(
                source is not None and source["kind"] == "source", "INVALID_OUTPUT", "未知原文引用"
            )
            validate_source_ref(ref, source)
            require(
                any(
                    s["source_id"] == source["id"]
                    and any(
                        r["start"] <= ref["start"] < ref["end"] <= r["end"] for r in s["ranges"]
                    )
                    for s in selections
                ),
                "INVALID_OUTPUT",
                "引文超出本次文章选区",
            )
    # JSON serialization rejects NaN, Infinity and arbitrary SDK objects.
    fingerprint(value)
    return value


def issue(
    rule: str,
    path: str,
    explanation: str,
    suggestion: str,
    *,
    severity: str = "warning",
    quote: str | None = None,
) -> JsonObject:
    return {
        "id": fingerprint([rule, path, explanation])[:20],
        "rule_id": rule,
        "severity": severity,
        "target_path": path,
        "quote": quote,
        "source_refs": [],
        "explanation": explanation,
        "suggestion": suggestion,
        "disposition": "open",
    }


def local_issues(
    kind: str, content: JsonObject, config: JsonObject | None = None
) -> list[JsonObject]:
    result = []
    if kind == "storyboard":
        lower, upper = (config or {}).get("shot_duration_range_ms", [5000, 15000])
        for index, shot in enumerate(content["shots"]):
            if not lower <= shot["duration_ms"] <= upper:
                result.append(
                    issue(
                        "SHOT-003",
                        f"shots.{index}.duration_ms",
                        "镜头超出建议的5–15秒范围",
                        "按叙事节奏酌情调整时长",
                    )
                )
    if kind in SPEC_FIELDS:
        for field in SPEC_FIELDS[kind]:
            value = content.get(field, "")
            if not value.strip() or PLACEHOLDER.search(value) or UNRESOLVED.search(value):
                result.append(
                    issue(
                        "ASSET-001",
                        field,
                        "资产设定尚未填写具体值",
                        "填写具体设计，将补充与原文事实区分",
                        severity="error",
                    )
                )
        for index, view in enumerate(content.get("views", [])):
            if view.get("spec_input_hash") != view_input_hash(kind, content, view):
                result.append(
                    issue(
                        "ASSET-003",
                        f"views.{index}",
                        "提示词依赖的设定或视角已改变",
                        "重新生成提示词，或逐视图人工核对并编辑提示词",
                        severity="error",
                    )
                )
            for field in ("positive_prompt", "negative_prompt"):
                value = view.get(field, "")
                if not value.strip() or PLACEHOLDER.search(value):
                    result.append(
                        issue(
                            "ASSET-001",
                            f"views.{index}.{field}",
                            "提示词缺失或仍包含模板变量",
                            "生成或补齐具体提示词",
                            severity="error",
                        )
                    )
    return result
