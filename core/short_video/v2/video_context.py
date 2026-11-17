"""Bounded, immutable storyboard context and local model results for one video shot."""

from __future__ import annotations

import logging
from copy import deepcopy

from .contracts import JsonObject
from .models import encode, reference_text, require, text

logger = logging.getLogger(__name__)


def video_command_ready(command: JsonObject) -> bool:
    context = command.get("context")
    if not isinstance(context, dict):
        return False
    article = context.get("article")
    return (
        type(command.get("max_context_chars")) is int
        and command["max_context_chars"] > 0
        and isinstance(article, dict)
        and isinstance(article.get("adjacent_shots"), list)
        and isinstance(context.get("supplement_manifest"), dict)
        and isinstance(command.get("shot_ids"), list)
        and len(command["shot_ids"]) == 1
        and context.get("selected_shot_ids") == command["shot_ids"]
    )


def validate_video_command(command: JsonObject) -> None:
    if command["stage"] == "video_prompt":
        require(
            video_command_ready(command),
            "INVALID_INPUT",
            "视频提示词任务缺少本镜资料或输入预算，请重新编写",
        )


def prepare_evidence(context: JsonObject, board: JsonObject, limit: int) -> None:
    """Attach the immediate neighbour shots; video prompts are written from the storyboard only."""
    article = context["article"]
    shot = context["storyboard"]["content"]["shots"][0]
    evidence: JsonObject = {"adjacent_shots": []}
    omitted = []
    budget = min(6000, limit // 4)
    shots = board["content"]["shots"]
    index = next(i for i, s in enumerate(shots) if s["shot_id"] == shot["shot_id"])
    for neighbour in (index - 1, index + 1):
        if 0 <= neighbour < len(shots):
            value = reference_text(
                {
                    k: shots[neighbour][k]
                    for k in ("shot_id", "scene_ref", "characters", "props", "action")
                    if k in shots[neighbour]
                }
            )
            value["relation"] = "previous" if neighbour < index else "next"
            evidence["adjacent_shots"].append(value)
            if len(encode(evidence)) > budget:
                evidence["adjacent_shots"].pop()
                omitted.append({"shot_id": value["shot_id"]})
    # Source ids stay for task binding; no original text is read or sent.
    context["article"] = {
        "selection_id": article["selection_id"],
        "source_id": article["source_id"],
        **evidence,
        "representation": "storyboard_only_v1",
    }
    context["supplement_manifest"] = {
        "budget_chars": budget,
        "included_chars": len(encode(evidence)),
        "omitted": omitted,
    }
    context["article"]["omitted_items"] = len(omitted)


def model_context(context: JsonObject) -> JsonObject:
    """Render a single factual context, excluding drafts and persistence controls."""
    names = {a["spec_id"]: a for a in context["shot_assets"]}
    assets = []
    for spec in context["asset_specs"]:
        labels = names.get(spec["id"], {})
        assets.append(
            {
                **spec["content"],
                "name": labels.get("name", ""),
                "variant_label": labels.get("variant_label", ""),
            }
        )
    config = context["config"]
    return reference_text(
        {
            # Without original text, the shot's source coordinates carry no usable meaning.
            "shot": {
                k: v
                for k, v in context["storyboard"]["content"]["shots"][0].items()
                if k != "source_refs"
            },
            "assets": assets,
            "video_inputs": context["video_inputs"],
            # Older commands may still hold original fragments; they are not sent any more.
            "adjacent_shots": context["article"]["adjacent_shots"],
            "requirements": {
                k: config[k]
                for k in ("audience", "tone", "required", "forbidden", "permitted_adaptations")
                if k in config
            },
        }
    )


def local_result(content: JsonObject, shot_id: str) -> JsonObject:
    shot = next(s for s in content["shots"] if s["shot_id"] == shot_id)
    return {"shot_id": shot_id, "video_motion_prompt": shot.get("video_motion_prompt", "")}


def merge_result(value: JsonObject, original: JsonObject, shot_id: str) -> JsonObject:
    require(
        set(value) == {"shot_id", "video_motion_prompt"} and value["shot_id"] == shot_id,
        "INVALID_OUTPUT",
        "只返回当前shot_id和video_motion_prompt，不返回完整镜头集合或额外字段",
    )
    text(value["video_motion_prompt"], "完整视频提示词")
    result = deepcopy(original)
    shot = next(s for s in result["shots"] if s["shot_id"] == shot_id)
    shot["video_motion_prompt"] = value["video_motion_prompt"]
    return result


def assemble_request(prefix: str, parts: JsonObject) -> str:
    """Keep block boundaries measurable for every generation/review/repair request."""
    return prefix + "\n" + "\n".join(f"【{key}】\n{encode(value)}" for key, value in parts.items())


def request_sizes(prompt: str) -> dict[str, int]:
    import re

    blocks = re.split(r"(?m)^【([^\n]+)】\n", prompt)
    result = {"rules": len(blocks[0])}
    for index in range(1, len(blocks), 2):
        name = blocks[index]
        result[name] = result.get(name, 0) + len(blocks[index + 1])
    return result


def check_budget(prompt: str, limit: int, phase: str, *, code: str = "CONTEXT_TOO_LARGE") -> None:
    sizes = request_sizes(prompt)
    logger.info("%s：输入%d/%d字符，分块=%s", phase, len(prompt), limit, sizes)
    require(
        len(prompt) <= limit,
        code,
        f"{phase}上下文超限：{len(prompt)}/{limit}字符；分块字符数：{encode(sizes)}；原稿及已有候选保留",
    )
