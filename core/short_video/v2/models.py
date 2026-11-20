"""Shared values and explicit error contracts for the v2 workspace."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Container
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, NoReturn
from uuid import uuid4

from .contracts import JsonObject, ModelSnapshot, ReviewModelSnapshot
from .storyboard_skill import STORYBOARD_SKILLS


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    ).hexdigest()


def parse_json(raw: str) -> Any:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1])
    try:
        value = json.loads(text, parse_constant=reject_json_constant)
    except json.JSONDecodeError as exc:
        details = {
            "json_error": exc.msg,
            "line": exc.lineno,
            "column": exc.colno,
            "position": exc.pos,
        }
        # Plain non-JSON replies are already included once in the repair request.
        if exc.pos > 0:
            start, end = max(0, exc.pos - 40), min(len(exc.doc), exc.pos + 40)
            details.update(snippet_start=start, snippet=exc.doc[start:end])
        raise WorkspaceError(
            "INVALID_OUTPUT",
            f"回复不是有效 JSON 对象：{exc.msg}；第{exc.lineno}行、第{exc.colno}列，偏移{exc.pos}",
            details=details,
        ) from exc
    except ValueError as exc:
        raise WorkspaceError("INVALID_OUTPUT", "回复不是有效 JSON 对象") from exc
    return value


def parse_object(raw: str) -> JsonObject:
    value = parse_json(raw)
    require(isinstance(value, dict), "INVALID_OUTPUT", "需要 JSON 对象")
    return value


def format_error_feedback(error: WorkspaceError) -> str:
    """Include bounded syntax evidence in model feedback, without logging source text."""
    if "json_error" in error.details:
        return str(error) + "\nJSON解析详情：\n" + encode(error.details)
    if error.details.get("category") in {"schema", "reference"}:
        return str(error) + "\n审阅校验详情：\n" + encode(error.details)
    return str(error)


def reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"JSON 不允许数值 {value}")


TRACKS = {
    "themes": ["脑洞", "剧情", "奇幻", "末世", "玄幻", "冒险", "科幻", "豪门"],
    "settings": ["逆袭", "系统", "穿越", "重生", "反转", "异能", "娱乐圈", "总裁"],
    "backgrounds": ["异界", "都市", "校园", "架空", "年代", "职场", "古代", "乡村", "民国"],
}
STAGE_LABELS = {
    "novel_copy": "小说推文",
    "video_copy": "视频推广文案",
    "asset_catalog": "人物场景道具目录",
    "storyboard": "分镜",
    "character_spec": "人物设定",
    "scene_spec": "场景设定",
    "prop_spec": "道具设定",
    "prompt_bundle": "镜头提示词",
}
ASSET_KINDS = {"character": "character_spec", "scene": "scene_spec", "prop": "prop_spec"}
VIEWS = {
    "character": [
        ("head_front", "正面头部特写"),
        ("body_front", "全身正面"),
        ("body_side", "全身侧面"),
        ("body_back", "全身背面"),
    ],
    "scene": [("establishing", "空间全景"), ("reverse", "反向全景")],
    "prop": [("front", "正面"), ("three_quarter", "微侧面"), ("top", "微俯视")],
}
ACTIVE_RUNS = {"queued", "running"}
RUN_STATUSES = ACTIVE_RUNS | {
    "succeeded",
    "failed",
    "cancelled",
    "interrupted",
    "paused",
    "invalidated",
}
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*_[0-9a-f]{32}$")


class WorkspaceError(ValueError):
    def __init__(
        self, code: str, message: str, *, details: JsonObject | None = None, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.code, self.details, self.retryable = code, details or {}, retryable

    def result(self) -> JsonObject:
        return {
            "error": {
                "code": self.code,
                "message": str(self),
                "details": deepcopy(self.details),
                "retryable": self.retryable,
            }
        }


def identity(prefix: str = "item") -> str:
    return f"{prefix}_{uuid4().hex}"


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def capture_model_snapshot(provider: str) -> ModelSnapshot:
    """Keep the concrete model name, never credentials or a possibly secret URL."""
    from core.common.api_providers import get_config

    try:
        config = get_config(provider)
    except ValueError as exc:
        raise WorkspaceError("MODEL_CONFIGURATION_MISSING", f"文字服务配置无效：{exc}") from exc
    return {"provider": provider, "model": config.model}


def capture_review_model_snapshot() -> ReviewModelSnapshot:
    from .service_settings import get_review_config

    try:
        config = get_review_config()
    except ValueError as exc:
        raise WorkspaceError("REVIEW_CONFIGURATION_MISSING", str(exc)) from exc
    return {
        "provider": config.provider,
        "model": config.model,
        "endpoint_hash": fingerprint(config.base_url),
    }


def validate_review_model_snapshot(snapshot: ReviewModelSnapshot) -> None:
    require(
        isinstance(snapshot, dict) and isinstance(snapshot.get("endpoint_hash"), str),
        "INVALID_MODEL_SNAPSHOT",
        "任务缺少独立校验模型快照，请重新生成",
    )
    validate_model_snapshot_format(snapshot)
    require(
        capture_review_model_snapshot() == snapshot,
        "REVIEW_CONFIGURATION_CHANGED",
        "校验模型或接口地址已变化，请重新生成任务",
    )


def validate_model_snapshot_format(snapshot: object) -> ModelSnapshot:
    require(
        isinstance(snapshot, dict)
        and isinstance(snapshot.get("provider"), str)
        and isinstance(snapshot.get("model"), str)
        and bool(snapshot["model"]),
        "INVALID_MODEL_SNAPSHOT",
        "任务缺少当前格式的文字模型快照，请重新创建任务",
    )
    return snapshot


def validate_model_snapshot(snapshot: ModelSnapshot) -> None:
    validate_model_snapshot_format(snapshot)
    require(
        capture_model_snapshot(snapshot["provider"]) == snapshot,
        "MODEL_CONFIGURATION_CHANGED",
        "文字模型配置已变化，请重新确认任务输入",
    )


def reference_text(value: Any) -> Any:
    """Render references by their exact coordinates, avoiding repeated source quotes."""
    if isinstance(value, dict):
        return {
            k: [
                {name: reference_text(item) for name, item in ref.items() if name != "quote"}
                for ref in v
            ]
            if k == "source_refs" and isinstance(v, list)
            else reference_text(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [reference_text(item) for item in value]
    return value


def require(condition: object, code: str, message: str) -> None:
    if not condition:
        raise WorkspaceError(code, message)


def text(value: Any, label: str, *, empty: bool = False) -> str:
    require(
        isinstance(value, str) and (empty or bool(value.strip())), "INVALID_INPUT", f"请填写{label}"
    )
    return value


def validate_choice(value: object, choices: Container[str], label: str) -> str:
    choice = text(value, label)
    require(choice in choices, "INVALID_INPUT", f"{label}不在允许范围内")
    return choice


def positive_int(value: Any, label: str) -> int:
    require(type(value) is int and value > 0, "INVALID_INPUT", f"{label}必须是正整数")
    return value


def default_config() -> JsonObject:
    return {
        "title": "未命名推广项目",
        "tracks": {k: [] for k in TRACKS},
        "custom_tracks": [],
        "audience": "中文短视频观众",
        "tone": "自然、紧凑",
        "narrative_person": "source",
        "target_words": None,
        "target_duration_ms": None,
        "shot_duration_range_ms": [5000, 15000],
        "required": [],
        "forbidden": [],
        "permitted_adaptations": [],
        "text_provider": "hosted-llm",
        "prompt_refs": {},
        "ruleset_ref": "v2-default-1",
        "storyboard_skill_id": None,
    }


def validate_config(content: JsonObject) -> JsonObject:
    require(isinstance(content, dict), "INVALID_INPUT", "项目配置必须为对象")
    require(
        default_config().keys() - {"storyboard_skill_id"} <= content.keys(),
        "INVALID_INPUT",
        "项目配置缺少当前必需字段",
    )
    config = deepcopy(content)
    config.setdefault("storyboard_skill_id", None)
    skill_id = config["storyboard_skill_id"]
    require(
        skill_id is None or isinstance(skill_id, str) and skill_id in STORYBOARD_SKILLS,
        "INVALID_INPUT",
        "请选择可用的分镜 Skill 或不使用 Skill",
    )
    text(config["title"], "项目标题")
    for name in ("audience", "tone"):
        text(config[name], name, empty=True)
    require(config["narrative_person"] in {"first", "third", "source"}, "INVALID_INPUT", "人称无效")
    for name in ("target_words", "target_duration_ms"):
        if config[name] is not None:
            positive_int(config[name], name)
    duration = config["shot_duration_range_ms"]
    require(
        isinstance(duration, list) and len(duration) == 2, "INVALID_INPUT", "请设置镜头建议区间"
    )
    for value in duration:
        positive_int(value, "镜头时长")
    require(duration[0] <= duration[1], "INVALID_INPUT", "镜头时长区间顺序错误")
    require(
        isinstance(config["tracks"], dict) and set(config["tracks"]) == set(TRACKS),
        "INVALID_INPUT",
        "赛道必须包含主题、设定、背景",
    )
    for group, values in config["tracks"].items():
        require(
            isinstance(values, list)
            and all(v in TRACKS[group] for v in values)
            and len(set(values)) == len(values),
            "INVALID_INPUT",
            "赛道标签无效或重复",
        )
    for name in ("custom_tracks", "required", "forbidden", "permitted_adaptations"):
        require(isinstance(config[name], list), "INVALID_INPUT", f"{name}必须为列表")
        for item in config[name]:
            text(item, name)
    require(isinstance(config["prompt_refs"], dict), "INVALID_INPUT", "模板引用无效")
    text(config["text_provider"], "文字服务")
    text(config["ruleset_ref"], "规则集")
    return config


def content_key(kind: str, content: JsonObject) -> str:
    if kind in ASSET_KINDS.values():
        return f"{kind}:{text(content.get('asset_id'), '资产编号')}:{text(content.get('variant_id'), '状态编号')}"
    return kind
