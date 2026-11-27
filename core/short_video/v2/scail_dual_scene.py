"""Compile the SCAIL-2 shot template into one recoverable video task."""

from __future__ import annotations

from copy import deepcopy

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
VIDEO = "417"
OUTPUT = "139"
TEXT = "368"
SCENE = "scene"
DRIVE_SWITCH = "pose_drive_switch"
PREFIX = "shot_0_"


class DualSceneWorkflow(VideoWorkflow):
    def prepare_image_inputs(self, node_ids: list[str]) -> VideoWorkflow:
        require(sorted(node_ids) == sorted(slot.node_id for slot in self.image_inputs),
                "INVALID_INPUT", "请选择两张角色参考图和一张场景参考图")
        return self

    def bind(
        self, image_names: dict[str, str], prompt: str, job_id: str, parameters: JsonObject,
        *, driving_video: str | None = None,
    ) -> JsonObject:
        graph = super().bind(image_names, prompt, job_id, parameters, driving_video=driving_video)
        condition = graph["447"]["inputs"]
        if condition.get("pose_video") == [DRIVE_SWITCH, 0]:
            driver = graph[DRIVE_SWITCH]["inputs"]
            # Route only the selected branch before composing the clip so the original
            # driver does not require SAM 3D Body nodes or weights on the server.
            condition["pose_video"] = driver["on_true" if driver["switch"] else "on_false"]
        return _compose(graph, preview=False)

    def bind_tracking_preview(self, job_id: str, parameters: JsonObject, *, driving_video: str | None) -> JsonObject:
        # The dependency walk for a preview only reaches tracking and compositing inputs.
        unused_images = {slot.node_id: "unused.png" for slot in self.image_inputs}
        graph = super().bind(unused_images, self.prompt, job_id, parameters, driving_video=driving_video)
        graph["402"]["inputs"]["value"] = min(512, graph["402"]["inputs"]["value"])
        graph[OUTPUT]["inputs"].pop("audio", None)
        graph[OUTPUT]["inputs"]["filename_prefix"] += "_tracking"
        return _compose(graph, preview=True)


def parse_dual_scene(graph: JsonObject) -> VideoWorkflow:
    slots = (
        ImageInput("106", "角色 A", "role_a"),
        ImageInput("role_b", "角色 B", "role_b"),
        ImageInput(SCENE, "场景", "scene"),
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
        and all(slot.node_id in active for slot in slots)
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
    parameters = [WorkflowParameter("415", "aspect_ratio", "画幅", "16:9", choices=SCAIL_ASPECT_RATIO_CHOICES)]
    if active["447"]["inputs"].get("pose_video") == [DRIVE_SWITCH, 0]:
        driver = active[DRIVE_SWITCH]
        inputs = driver["inputs"]
        require(
            driver["class_type"] == "ComfySwitchNode"
            and type(inputs.get("switch")) is bool
            and inputs.get("on_false") == ["drive_rgb", 0]
            and inputs.get("on_true") == ["mesh_render", 0],
            "INVALID_INPUT",
            "3D 网格驱动需要连接原视频和网格渲染两路画面",
        )
        parameters.append(WorkflowParameter(DRIVE_SWITCH, "switch", "3D 网格驱动", inputs["switch"], 0, 1))
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
    return DualSceneWorkflow(deepcopy(active), slots, OUTPUT, TEXT, "positive_prompt", tuple(parameters),
                             "scail2_dual_scene", "348", VIDEO)


def _node(kind: str, **inputs: object) -> JsonObject:
    return {"class_type": kind, "inputs": inputs}


def _pad_clip(graph: JsonObject, video: str, prefix: str) -> tuple[str, str]:
    # Pad Wan's 4n+1 input with the clip's final frame.
    # The export is trimmed to the actual loaded count.
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


def _compose(base: JsonObject, *, preview: bool) -> JsonObject:
    """Generate the whole original clip as one shot and export it at its loaded length."""
    final = base[OUTPUT]["inputs"]
    graph: JsonObject = {}
    video_id = PREFIX + VIDEO
    video = deepcopy(base[VIDEO])
    video["inputs"].update(skip_first_frames=0, frame_load_cap=0, select_every_nth=1, format="None")
    graph[video_id] = video
    padded, count = (None, None) if preview else _pad_clip(graph, video_id, PREFIX)
    routed = {VIDEO: video_id}

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
            if node_id == "resize_scene":
                # Keep the entire scene image, including tall details, instead of cropping it.
                inputs["fit"] = "fill"
            bound = node_id if inputs == original["inputs"] else PREFIX + node_id
            graph[bound] = {**original, "inputs": inputs}
            routed[node_id] = bound
        return routed[node_id]

    if preview:
        original = [route("415"), 0]
        tint = PREFIX + "tint"
        graph[tint] = _node("ImageBlend", image1=original, image2=[route("drive_semantic"), 0],
                            blend_factor=0.5, blend_mode="normal")
        overlay = PREFIX + "preview"
        graph[overlay] = _node("ImageCompositeMasked", destination=original, source=[tint, 0],
                               mask=[route("drive_union"), 0], x=0, y=0, resize_source=False)
        layer = [overlay, 0]
    else:
        decode_node, output_index = final["images"]
        layer = [route(decode_node), output_index]
    trimmed = PREFIX + "slice"
    graph[trimmed] = _node("ImageFromBatch", image=layer, batch_index=0, length=[video_id, 1])
    final["images"] = [trimmed, 0]
    final["frame_rate"] = FPS
    if "audio" in final:
        final["audio"] = [video_id, 2]
    graph[OUTPUT] = base[OUTPUT]
    return output_dependencies(graph, OUTPUT)
