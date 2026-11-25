"""Freeze the actual video inputs and their exact reference-image numbering."""

from __future__ import annotations

import base64
import hashlib
from copy import deepcopy

from .contracts import JsonObject
from .media_repository import MediaRepository, image_suffix
from .media_workflows import MediaConfig, parse_video_workflow
from .models import fingerprint, require

MAX_PROMPT_IMAGE_BYTES = 20 * 1024 * 1024
_IMAGE_MIME_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}


def video_prompt_messages(output_dir: str, prompt: str, binding: JsonObject) -> list[JsonObject]:
    """Read the frozen pictures only when sending a request, in their exact slot order."""
    media = MediaRepository(output_dir)
    parts = [{"type": "text", "text": prompt}]
    remaining = MAX_PROMPT_IMAGE_BYTES
    for picture in binding["images"]:
        image = media.image(picture["image_id"])
        with media.resolve(image["relative_path"]).open("rb") as file:
            data = file.read(remaining + 1)
        require(
            len(data) <= remaining,
            "IMAGE_INPUT_TOO_LARGE",
            f"选图总大小超过 {MAX_PROMPT_IMAGE_BYTES // (1024 * 1024)} MiB，请缩小图片后重试",
        )
        require(
            hashlib.sha256(data).hexdigest() == picture["sha256"],
            "STALE_INPUT",
            "参考图片文件已变化，请重新导入",
        )
        remaining -= len(data)
        mime = _IMAGE_MIME_TYPES[image_suffix(data)]
        encoded = base64.b64encode(data).decode("ascii")
        parts.extend(
            (
                {"type": "text", "text": f"下一张是{picture['reference']}，以实际画面为准。"},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
            )
        )
    return [{"role": "user", "content": parts}]


def snapshot_video_inputs(
    output_dir: str,
    images: dict[str, str | None],
    workflow_id: str,
    parameters: JsonObject,
) -> JsonObject:
    selected = MediaConfig.load(output_dir).selected(workflow_id)
    template = parse_video_workflow(selected["graph"])
    chosen = {node: image for node, image in images.items() if image is not None}
    template = template.prepare_image_inputs(list(chosen))
    template.bind(chosen, "输入结构校验", "validation", parameters, driving_video="validation")
    media = MediaRepository(output_dir)
    bindings, number = [], 0
    for slot in template.image_inputs:
        image = media.image(chosen[slot.node_id])
        require(
            hashlib.sha256(media.resolve(image["relative_path"]).read_bytes()).hexdigest()
            == image["sha256"],
            "STALE_INPUT",
            "参考图片文件已变化，请重新导入",
        )
        if slot.role == "reference":
            number += 1
        source = image.get("source", {})
        bindings.append(
            {
                "node_id": slot.node_id,
                "image_id": image["image_id"],
                "sha256": image["sha256"],
                "role": slot.role,
                "reference": f"<Picture {number}>" if slot.role == "reference" else slot.label,
                "asset_source": {
                    key: deepcopy(source[key])
                    for key in (
                        "kind",
                        "catalog_id",
                        "asset_id",
                        "variant_id",
                        "view_id",
                        "spec_id",
                        "prompt_hash",
                    )
                    if key in source
                },
            }
        )
    duration = next(
        (parameters.get(p.key, p.default) for p in template.parameters if p.role == "duration"),
        None,
    )
    return {
        "workflow_id": workflow_id,
        "workflow_hash": fingerprint(selected["graph"]),
        "kind": template.kind,
        "images": bindings,
        "duration_seconds": duration,
    }


def validate_video_inputs(output_dir: str, frozen: JsonObject) -> None:
    parameters = {}
    selected = MediaConfig.load(output_dir).selected(frozen["workflow_id"])
    template = parse_video_workflow(selected["graph"])
    for parameter in template.parameters:
        if parameter.role == "duration":
            parameters[parameter.key] = frozen["duration_seconds"]
    current = snapshot_video_inputs(
        output_dir,
        {image["node_id"]: image["image_id"] for image in frozen["images"]},
        frozen["workflow_id"],
        parameters,
    )
    require(current == frozen, "STALE_INPUT", "视频工作流或参考图片已变化，请重新编写视频提示词")
