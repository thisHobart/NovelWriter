"""Compile the SCAIL-2 shot template into one recoverable video task."""

from __future__ import annotations

import math
import re
from copy import deepcopy
from dataclasses import replace

from .contracts import JsonObject
from .media_workflows import (
    SCAIL_ASPECT_RATIO_CHOICES,
    ImageInput,
    VideoWorkflow,
    WorkflowParameter,
    output_dependencies,
)
from .models import require

FPS = 24
MAX_BATCH_FRAMES = 4096  # ImageFromBatch and RepeatImageBatch accept batches up to this length.
MAX_SECONDS = MAX_BATCH_FRAMES // FPS
VIDEO = "417"
OUTPUT = "139"
TEXT = "368"
SCENE_A = "scene"
SCENE_B = "scene_b"


class DualSceneWorkflow(VideoWorkflow):
    def prepare_image_inputs(self, node_ids: list[str]) -> VideoWorkflow:
        selected = set(node_ids)
        all_slots = {slot.node_id for slot in self.image_inputs}
        require(len(node_ids) == len(selected) and selected in (all_slots, all_slots - {SCENE_B}),
                "INVALID_INPUT", "请选择两张角色参考图和一张场景参考图")
        return replace(self, image_inputs=tuple(slot for slot in self.image_inputs if slot.node_id in selected))

    def bind(
        self, image_names: dict[str, str], prompt: str, job_id: str, parameters: JsonObject,
        *, driving_video: str | None = None,
    ) -> JsonObject:
        graph = super().bind(image_names, prompt, job_id, parameters, driving_video=driving_video)
        return self._bind_timeline(graph, preview=False)

    def bind_tracking_preview(self, job_id: str, parameters: JsonObject, *, driving_video: str | None) -> JsonObject:
        # The dependency walk for a preview only reaches tracking and compositing inputs.
        unused_images = {slot.node_id: "unused.png" for slot in self.image_inputs}
        graph = super().bind(unused_images, self.prompt, job_id, parameters, driving_video=driving_video)
        graph["402"]["inputs"]["value"] = min(512, graph["402"]["inputs"]["value"])
        graph[OUTPUT]["inputs"].pop("audio", None)
        graph[OUTPUT]["inputs"]["filename_prefix"] += "_tracking"
        return self._bind_timeline(graph, preview=True)

    def _bind_timeline(self, graph: JsonObject, *, preview: bool) -> JsonObject:
        final = graph[OUTPUT]["inputs"]
        transition = round(final.pop("scene_transition") * FPS)
        end = round(final.pop("action_end") * FPS)
        hold = final.pop("hold_tail")
        cuts = final.pop("tracking_cuts")
        ranges = final.pop("clip_ranges").strip()
        if final.pop("single_shot") or len(self.image_inputs) == 3:
            return _compose(graph, [(0, 0)], transition, hold=False, preview=preview, clips=True)
        if ranges:
            intervals = _clip_intervals(ranges, transition)
            return _compose(graph, intervals, transition, hold=False, preview=preview, clips=True)
        starts = _shot_starts(cuts, transition, end)
        return _compose(graph, list(zip(starts, [*starts[1:], end])), transition, hold and not preview, preview=preview)


def parse_dual_scene(graph: JsonObject) -> VideoWorkflow:
    slots = (
        ImageInput("106", "角色 A", "role_a"),
        ImageInput("role_b", "角色 B", "role_b"),
        ImageInput(SCENE_A, "场景 A", "scene_a"),
        ImageInput(SCENE_B, "场景 B", "scene_b"),
    )
    classes = {
        **{slot.node_id: "LoadImage" for slot in slots},
        VIDEO: "VHS_LoadVideo", OUTPUT: "VHS_VideoCombine", TEXT: "WanVideoTextEncodeCached",
        "415": "ResizeImageAdvanced", "402": "INTConstant", "349": "WanVideoSchedulerv2",
        "348": "WanVideoSamplerv2", "443": "CLIPTextEncode", "source_b_text": "CLIPTextEncode",
        "447": "SCAILPose2SCAIL2Condition",
        "drive_semantic": "ImageCompositeMasked", "drive_union": "MaskComposite",
    }
    for node_id, kind in classes.items():
        require(graph.get(node_id, {}).get("class_type") == kind, "INVALID_INPUT", f"双人动作迁移工作流需要节点 {node_id}（{kind}）")
    active = output_dependencies(graph, OUTPUT)
    require(
        VIDEO in active and graph[VIDEO]["inputs"].get("force_rate") == FPS
        and all(slot.node_id in active for slot in slots[:-1])
        and active.get("447", {}).get("inputs", {}).get("mode") == "animation",
        "INVALID_INPUT",
        "双人动作迁移需要 24 帧原视频、动画模式、双角色和场景输入",
    )
    output = graph[OUTPUT]["inputs"]
    image_link = output.get("images")
    require(
        output.get("save_output") is True and isinstance(image_link, list) and len(image_link) == 2
        and isinstance(image_link[0], str) and graph.get(image_link[0], {}).get("class_type") == "WanVideoDecode",
        "INVALID_INPUT",
        "双人动作迁移工作流需要保存解码后的画面",
    )
    parameters = [
        WorkflowParameter(OUTPUT, "single_shot", "单镜头", False, 0, 1, "single_shot"),
        WorkflowParameter(OUTPUT, "scene_transition", "切换到场景 B（秒）", 4.9, 0.2, MAX_SECONDS, "scene_transition"),
        WorkflowParameter(OUTPUT, "clip_ranges", "单独输出的时间段（秒）", "", role="clip_ranges"),
        WorkflowParameter(OUTPUT, "action_end", "动作截止（秒，0 为全片）", 0.0, 0, MAX_SECONDS, "action_end"),
        WorkflowParameter(OUTPUT, "tracking_cuts", "重新跟踪时间（秒）", "", role="tracking_cuts"),
        WorkflowParameter(OUTPUT, "hold_tail", "保持原片时长", True, 0, 1, "hold_tail"),
        WorkflowParameter("415", "aspect_ratio", "画幅", "16:9", choices=SCAIL_ASPECT_RATIO_CHOICES),
    ]
    for node_id, name, label, minimum, maximum in (
        ("443", "text", "原片人物 A 描述", None, None),
        ("source_b_text", "text", "原片人物 B 描述", None, None),
        ("402", "value", "输出长边（像素）", 256, 2048),
        ("349", "steps", "采样步数", 1, 100),
        ("348", "seed", "随机种子", 0, 2**64 - 1),
    ):
        value = graph[node_id]["inputs"].get(name)
        valid = bool(value.strip()) if isinstance(value, str) and minimum is None else (
            type(value) is int and minimum is not None and minimum <= value <= maximum
        )
        require(valid, "INVALID_INPUT", f"双人动作迁移工作流字段 {label} 设置无效")
        role = "source_character" if minimum is None else "output_edge" if node_id == "402" else ""
        parameters.append(WorkflowParameter(node_id, name, label, value, minimum, maximum, role))
    if "audio" in output:
        parameters.append(WorkflowParameter(OUTPUT, "audio", "保留原声", True, 0, 1, "source_audio"))
    require(isinstance(graph[TEXT]["inputs"].get("positive_prompt"), str), "INVALID_INPUT", "工作流需要视频提示词")
    active[SCENE_B] = graph[SCENE_B]
    return DualSceneWorkflow(deepcopy(active), slots, OUTPUT, TEXT, "positive_prompt", tuple(parameters),
                             "scail2_dual_scene", "348", VIDEO)


def _shot_starts(cut_text: str, transition: int, end: int) -> list[int]:
    cuts = {0, transition}
    for part in re.split(r"[\s,，;；]+", cut_text.strip()):
        if not part:
            continue
        try:
            seconds = float(part)
        except ValueError:
            seconds = -1
        require(math.isfinite(seconds) and 0 < seconds < MAX_SECONDS, "INVALID_INPUT",
                "重新跟踪时间请填写用逗号分隔的正秒数")
        cuts.add(round(seconds * FPS))
    starts = sorted(cuts)
    require(not end or starts[-1] < end, "INVALID_INPUT", "动作截止时间必须晚于场景切换和重新跟踪时间")
    require(all(right - left >= 5 for left, right in zip(starts, starts[1:])) and (not end or end - starts[-1] >= 5),
            "INVALID_INPUT", "每段动作至少需要 5 帧，请拉开切换或重新跟踪时间")
    return starts


def _node(kind: str, **inputs: object) -> JsonObject:
    return {"class_type": kind, "inputs": inputs}


def _clip_intervals(text: str, transition: int) -> list[tuple[int, int]]:
    intervals = []
    prev_end = 0
    for item in re.split(r"[,，;；\n]", text):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[-–—]\s*(\d+(?:\.\d+)?)\s*", item)
        require(match is not None, "INVALID_INPUT", "时间段请按 0-0.75, 0.95-1.75 的格式填写")
        first, last = map(float, match.groups())
        require(0 <= first < last <= MAX_SECONDS, "INVALID_INPUT", f"每段需在 0–{MAX_SECONDS} 秒内，且结束晚于开始")
        start, end = round(first * FPS), round(last * FPS)
        require(prev_end <= start, "INVALID_INPUT", "时间段需按时间排列，且不能重叠")
        bounds = [start, transition, end] if start < transition < end else [start, end]
        for a, b in zip(bounds, bounds[1:]):
            require(b - a >= 5, "INVALID_INPUT", "每段至少需要 5 帧，请拉开起止时间和场景切换时间")
            intervals.append((a, b))
        prev_end = end
    return intervals


def _pad_clip(graph: JsonObject, video: str, prefix: str) -> tuple[str, str]:
    # Pad Wan's 4n+1 input with the final frame inside the requested interval.
    # The export is trimmed to the actual loaded count, including near EOF.
    count = prefix + "padded_count"
    remaining = prefix + "padding_count"
    graph[count] = _node("ComfyMathExpression", expression="ceil((a - 1) / 4) * 4 + 1", **{"values.a": [video, 1]})
    graph[remaining] = _node("ComfyMathExpression", expression="max(1, a - b)", **{"values.a": [count, 1], "values.b": [video, 1]})
    last, hold, joined, padded = (prefix + suffix for suffix in ("clip_last", "clip_hold", "clip_join", "clip_padded"))
    graph[last] = _node("ImageFromBatch", image=[video, 0], batch_index=-1, length=1)
    graph[hold] = _node("RepeatImageBatch", image=[last, 0], amount=[remaining, 1])
    graph[joined] = _node("ImageBatch", image1=[video, 0], image2=[hold, 0])
    graph[padded] = _node("ImageFromBatch", image=[joined, 0], batch_index=0, length=[count, 1])
    return padded, count


def _compose(base: JsonObject, intervals: list[tuple[int, int]], transition: int, hold: bool, *, preview: bool, clips: bool = False) -> JsonObject:
    final = base[OUTPUT]["inputs"]
    graph: JsonObject = {}
    layers = []
    last_length = 0
    last_video = ""
    for shot, (start, stop) in enumerate(intervals):
        length = stop - start if stop else 0
        # Wan processes 4n+1 frames. Generate any extra frames, then trim at the exact cut.
        cap = ((length + 2) // 4) * 4 + 1 if length else 0
        prefix = f"shot_{shot}_"
        video = deepcopy(base[VIDEO])
        video_id = prefix + VIDEO
        video["inputs"].update(skip_first_frames=start, frame_load_cap=length if clips else cap,
                               select_every_nth=1, format="None" if clips else "Wan")
        graph[video_id] = video
        padded, count = _pad_clip(graph, video_id, prefix) if clips and not preview else (None, None)
        scene = SCENE_B if start >= transition else SCENE_A
        graph[scene] = base[scene]
        routed = {VIDEO: video_id, SCENE_A: scene}

        def route_link(link: list) -> list:
            if padded and link == [VIDEO, 0]:
                return [padded, 0]
            if count and link == ["394", 6]:
                return [count, 1]
            return [route(link[0]), link[1]]

        def route(node_id: str) -> str:
            if node_id not in routed:
                original = base[node_id]
                inputs = {
                    name: route_link(value)
                    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) else value
                    for name, value in original["inputs"].items()
                }
                if node_id == "resize_scene" and (scene == SCENE_B or clips and stop == 0):
                    # The landscape shot retains the entire second background, including tall details.
                    inputs["fit"] = "fill"
                bound = node_id if inputs == original["inputs"] else prefix + node_id
                graph[bound] = {**original, "inputs": inputs}
                routed[node_id] = bound
            return routed[node_id]

        if preview:
            original = [route("415"), 0]
            tint = prefix + "tint"
            graph[tint] = _node("ImageBlend", image1=original, image2=[route("drive_semantic"), 0],
                                blend_factor=0.5, blend_mode="normal")
            overlay = prefix + "preview"
            graph[overlay] = _node("ImageCompositeMasked", destination=original, source=[tint, 0],
                                   mask=[route("drive_union"), 0], x=0, y=0, resize_source=False)
            layer = [overlay, 0]
        else:
            decode_node, output_index = final["images"]
            layer = [route(decode_node), output_index]
        if length or clips:
            index = prefix + "slice"
            graph[index] = _node("ImageFromBatch", image=layer, batch_index=0, length=[video_id, 1] if clips else length)
            layer = [index, 0]
        if clips and not preview:
            output_id = OUTPUT if shot == 0 else f"{OUTPUT}_clip_{shot + 1:03}"
            inputs = {**final, "images": layer, "frame_rate": FPS,
                      "filename_prefix": f"{final['filename_prefix']}_片段{shot + 1:02}"}
            if "audio" in inputs:
                inputs["audio"] = [video_id, 2]
            graph[output_id] = {**base[OUTPUT], "inputs": inputs}
            continue
        layers.append(layer)
        last_video = prefix + VIDEO
        last_length = length

    if clips and not preview:
        return graph

    # Only a tiny decoded batch is needed to preserve the exact resampled original duration and audio.
    keep_audio = "audio" in final
    if hold or keep_audio:
        probe = deepcopy(base[VIDEO]["inputs"])
        probe.update(custom_width=32, custom_height=32, frame_load_cap=0, skip_first_frames=0,
                     select_every_nth=1, format="None")
        probe.pop("vae", None)
        probe.pop("meta_batch", None)
        graph["source_probe"] = _node("VHS_LoadVideo", **probe)

    if hold:
        graph["hold_length"] = _node(
            "ComfyMathExpression", expression="max(1, a - b - min(c, d))",
            **{"values.a": ["source_probe", 1], "values.b": intervals[-1][0],
               "values.c": [last_video, 1], "values.d": last_length or MAX_BATCH_FRAMES},
        )
        graph["last_frame"] = _node("ImageFromBatch", image=layers[-1], batch_index=-1, length=1)
        graph["held_scene"] = _node("RepeatImageBatch", image=["last_frame", 0], amount=["hold_length", 1])
        layers.append(["held_scene", 0])

    joined = layers[0]
    if len(layers) > 1:
        graph["joined_scenes"] = _node(
            "ImageBatchMulti", inputcount=len(layers),
            **{f"image_{i}": layer for i, layer in enumerate(layers, 1)},
        )
        joined = ["joined_scenes", 0]
    if hold:
        graph["trim_to_source"] = _node("ImageFromBatch", image=joined, batch_index=0, length=["source_probe", 1])
        joined = ["trim_to_source", 0]

    final["images"] = joined
    final["frame_rate"] = FPS
    if keep_audio:
        final["audio"] = ["source_probe", 2]
        end = intervals[-1][1]
        if not hold and end:
            graph["audio_cut"] = _node("TrimAudioDuration", audio=final["audio"], start_index=0, duration=end / FPS)
            final["audio"] = ["audio_cut", 0]
    graph[OUTPUT] = base[OUTPUT]
    return output_dependencies(graph, OUTPUT)
