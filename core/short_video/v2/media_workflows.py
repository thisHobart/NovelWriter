"""Project workflow catalog and supported video input mappings."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from .contracts import JsonObject
from .models import encode, require
from .storage import atomic_write_text

WORKFLOW_DIRECTORY = Path(__file__).with_name("workflows")
MAX_REFERENCE_IMAGES = 9
ASPECT_RATIO_CHOICES = (
    ("9:16 (Portrait Widescreen)", "9:16 竖屏"),
    ("16:9 (Widescreen)", "16:9 横屏"),
    ("21:9 (Ultrawide)", "21:9 超宽屏"),
    ("1:1 (Square)", "1:1 方形"),
    ("2:3 (Portrait Photo)", "2:3 竖屏"),
    ("3:4 (Portrait Standard)", "3:4 竖屏"),
    ("3:2 (Photo)", "3:2 横屏"),
    ("4:3 (Standard)", "4:3 横屏"),
)
SCAIL_ASPECT_RATIO_CHOICES = (
    ("original", "跟随原视频"),
    ("16:9", "16:9 横屏"),
    ("4:3", "4:3 横屏"),
    ("3:2", "3:2 横屏"),
    ("1:1", "1:1 方形"),
    ("9:16", "9:16 竖屏"),
    ("3:4", "3:4 竖屏"),
    ("2:3", "2:3 竖屏"),
)
# Every shot starts as a vertical video of about one megapixel, whatever the workflow JSON
# holds; a shot keeps what was set for it.
SHOT_ASPECT_RATIO = "9:16 (Portrait Widescreen)"
SHOT_MEGAPIXELS = 1.0


def default_workflows() -> list[JsonObject]:
    workflows = []
    for workflow_id, name, filename in (
        ("minimax-reference", "H3 参考图", "minimax_h3_reference.json"),
        ("minimax-first-last", "H3 参考图＋首尾帧", "minimax_h3_first_last.json"),
    ):
        workflows.append(
            {
                "workflow_id": workflow_id,
                "name": name,
                "graph": read_api_workflow(WORKFLOW_DIRECTORY / filename),
            }
        )
    return workflows


@dataclass
class MediaConfig:
    base_url: str = "http://127.0.0.1:8188"
    workflows: list[JsonObject] = field(default_factory=default_workflows)
    selected_workflow_id: str = "minimax-reference"

    @classmethod
    def load(cls, output_dir: str | Path) -> MediaConfig:
        path = Path(output_dir) / "short_video" / "media" / "settings.json"
        if not path.exists():
            return cls()
        values = json.loads(path.read_text(encoding="utf-8"))
        require(
            isinstance(values, dict)
            and values.keys() == {"base_url", "workflows", "selected_workflow_id"}
            and isinstance(values["base_url"], str)
            and isinstance(values["workflows"], list)
            and isinstance(values["selected_workflow_id"], str),
            "INVALID_INPUT",
            "视频工作流配置不是当前格式",
        )
        return cls(**values)

    def selected(self, workflow_id: str | None = None) -> JsonObject:
        selected_id = workflow_id or self.selected_workflow_id
        for workflow in self.workflows:
            if workflow["workflow_id"] == selected_id:
                return workflow
        raise ValueError(f"工作流不存在：{selected_id}")

    def put_workflow(self, name: str, api_json: str, workflow_id: str | None = None) -> str:
        require(bool(name.strip()), "INVALID_INPUT", "请填写工作流名称")
        graph = json.loads(api_json)
        parse_video_workflow(graph)
        record = {"workflow_id": workflow_id or uuid4().hex, "name": name.strip(), "graph": graph}
        if workflow_id is None:
            self.workflows.append(record)
        else:
            self.workflows[self.workflows.index(self.selected(workflow_id))] = record
        self.selected_workflow_id = record["workflow_id"]
        return record["workflow_id"]

    def validate(self) -> None:
        address = urlsplit(self.base_url)
        require(
            address.scheme in {"http", "https"} and bool(address.hostname),
            "INVALID_INPUT",
            "请输入有效的 ComfyUI HTTP 地址",
        )
        self.selected()
        for workflow in self.workflows:
            parse_video_workflow(workflow["graph"])

    def save(self, output_dir: str | Path) -> None:
        self.validate()
        path = Path(output_dir) / "short_video" / "media" / "settings.json"
        atomic_write_text(
            path,
            encode(
                {
                    "base_url": self.base_url,
                    "workflows": self.workflows,
                    "selected_workflow_id": self.selected_workflow_id,
                }
            ),
        )


# What the 镜头 video area holds for each shot and workflow, kept across restarts.
SHOT_DRAFT_FIELDS = (
    "images",
    "driving_video_id",
    "prompt",
    "parameters",
    "additional_reference_slots",
    "prompt_image_bindings",
    "video_prompt_fingerprint",
    "video_prompt_binding",
)


def shot_drafts_path(output_dir: str | Path) -> Path:
    return Path(output_dir) / "short_video" / "media" / "shot_drafts.json"


def _prompt_image_bindings(value: object) -> dict[int, str]:
    """<Picture N> number → image node; JSON keeps the numbers as text, the video area compares
    them as numbers."""
    if not isinstance(value, dict):
        return {}
    return {
        int(number): node_id
        for number, node_id in value.items()
        if str(number).isdigit() and int(number) > 0 and isinstance(node_id, str)
    }


def _stored_draft(draft: JsonObject) -> JsonObject:
    stored = {k: v for k, v in draft.items() if k in SHOT_DRAFT_FIELDS}
    if "prompt_image_bindings" in stored:
        stored["prompt_image_bindings"] = _prompt_image_bindings(stored["prompt_image_bindings"])
    return stored


def load_shot_drafts(output_dir: str | Path) -> dict[str, dict[str, JsonObject]]:
    """{shot_id: {workflow_id: draft}}; empty when nothing was saved or the file is unreadable."""
    path = shot_drafts_path(output_dir)
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(values, dict):
        return {}
    return {
        shot_id: {
            workflow_id: _stored_draft(draft)
            for workflow_id, draft in workflows.items()
            if isinstance(workflow_id, str) and isinstance(draft, dict)
        }
        for shot_id, workflows in values.items()
        if isinstance(shot_id, str) and isinstance(workflows, dict)
    }


def save_shot_drafts(output_dir: str | Path, drafts: dict[str, dict[str, JsonObject]]) -> None:
    stored = {
        shot_id: {
            workflow_id: {k: v for k, v in draft.items() if k in SHOT_DRAFT_FIELDS}
            for workflow_id, draft in workflows.items()
        }
        for shot_id, workflows in drafts.items()
        if shot_id and workflows
    }
    path = shot_drafts_path(output_dir)
    if stored or path.exists():
        atomic_write_text(path, encode(stored))


@dataclass(frozen=True)
class ImageInput:
    node_id: str
    label: str
    role: str = "reference"


@dataclass(frozen=True)
class WorkflowParameter:
    node_id: str
    input_name: str
    label: str
    default: int | float | bool | str
    minimum: float | None = None
    maximum: float | None = None
    role: str = ""
    choices: tuple[tuple[str, str], ...] = ()

    @property
    def key(self) -> str:
        return f"{self.node_id}.{self.input_name}"


@dataclass(frozen=True)
class VideoWorkflow:
    graph: JsonObject
    image_inputs: tuple[ImageInput, ...]
    output_node: str
    prompt_node: str
    prompt_input: str
    parameters: tuple[WorkflowParameter, ...]
    kind: str
    generator_node: str
    driving_video_node: str | None = None

    @property
    def prompt(self) -> str:
        return self.graph[self.prompt_node]["inputs"][self.prompt_input]

    @property
    def reference_count(self) -> int:
        return sum(image.role == "reference" for image in self.image_inputs)

    def prepare_reference_inputs(self, node_ids: list[str]) -> VideoWorkflow:
        """只为本轮已选参考图接线，未选节点由依赖裁剪移出执行图。"""
        require(self.kind == "reference", "INVALID_INPUT", "当前工作流不支持动态参考图片")
        require(bool(node_ids), "INVALID_INPUT", "请至少选择一张参考图片")
        require(len(node_ids) <= MAX_REFERENCE_IMAGES, "INVALID_INPUT", "最多使用 9 张参考图片")
        graph = deepcopy(self.graph)
        inputs = graph[self.generator_node]["inputs"]
        for name in list(inputs):
            if name.startswith("ref_images.ref_image_"):
                del inputs[name]
        reference_nodes = {image.node_id for image in self.image_inputs}
        for index, node_id in enumerate(node_ids):
            require(
                node_id not in graph or node_id in reference_nodes,
                "INVALID_INPUT",
                f"参考图片节点编号与其他节点冲突：{node_id}",
            )
            if node_id not in graph:
                graph[node_id] = {"class_type": "LoadImage", "inputs": {"image": ""}}
            inputs[f"ref_images.ref_image_{index}"] = [node_id, 0]
        return parse_video_workflow(graph)

    def prepare_image_inputs(self, node_ids: list[str]) -> VideoWorkflow:
        """Retain selected image slots and bypass unused frame conditions for this run."""
        if self.kind == "reference":
            return self.prepare_reference_inputs(node_ids)
        if self.driving_video_node is not None:
            require(
                node_ids == [self.image_inputs[0].node_id],
                "INVALID_INPUT",
                "请选择一张角色参考图",
            )
            return self
        require(bool(node_ids), "INVALID_INPUT", "请至少选择一张参考图、首帧或尾帧图片")
        selected = set(node_ids)
        require(
            len(selected) == len(node_ids)
            and selected <= {image.node_id for image in self.image_inputs},
            "INVALID_INPUT",
            "选择了当前工作流不存在的图片输入",
        )
        graph = deepcopy(self.graph)
        inputs = graph[self.generator_node]["inputs"]
        for name in list(inputs):
            if name.startswith("ref_images.ref_image_"):
                del inputs[name]
        references = [
            image
            for image in self.image_inputs
            if image.role == "reference" and image.node_id in selected
        ]
        for index, image in enumerate(references):
            inputs[f"ref_images.ref_image_{index}"] = [image.node_id, 0]
        for image in self.image_inputs:
            if image.role == "reference" or image.node_id in selected:
                continue
            if graph[self.generator_node]["class_type"] == "MiniMaxH3ImageToVideo":
                inputs.pop(image.role, None)
                continue
            guide_id = next(
                key
                for key, node in graph.items()
                if node["class_type"] == "MiniMaxH3AddGuide"
                and node["inputs"]["image"] == [image.node_id, 0]
            )
            previous = graph[guide_id]["inputs"]["positive"]
            for node in graph.values():
                for name, value in node["inputs"].items():
                    if value == [guide_id, 0]:
                        node["inputs"][name] = deepcopy(previous)
            del graph[guide_id]
        return parse_video_workflow(graph)

    def bind(
        self, image_names: dict[str, str], prompt: str, job_id: str, parameters: JsonObject,
        *, driving_video: str | None = None,
    ) -> JsonObject:
        require(bool(prompt.strip()), "INVALID_INPUT", "请填写视频提示词")
        require(
            set(image_names) == {image.node_id for image in self.image_inputs}
            and all(image_names.values()),
            "INVALID_INPUT",
            "请为每个工作流图片输入选择图片",
        )
        known = {parameter.key for parameter in self.parameters}
        require(not parameters.keys() - known, "INVALID_INPUT", "包含当前工作流不支持的参数")
        graph = deepcopy(self.graph)
        if self.driving_video_node is not None:
            require(bool(driving_video), "INVALID_INPUT", "请选择原视频")
            graph[self.driving_video_node]["inputs"]["video"] = driving_video
        for image in self.image_inputs:
            graph[image.node_id]["inputs"]["image"] = image_names[image.node_id]
        for parameter in self.parameters:
            if parameter.role == "source_aspect" and parameter.key not in parameters:
                # Jobs saved before the aspect control keep their original resize settings.
                continue
            value = parameters.get(parameter.key, parameter.default)
            if parameter.choices:
                require(
                    any(value == choice for choice, _ in parameter.choices),
                    "INVALID_INPUT",
                    f"{parameter.label}不在可选范围内",
                )
            elif type(parameter.default) is str:
                require(
                    isinstance(value, str) and (bool(value.strip()) or parameter.role in {"tracking_cuts", "clip_ranges"}),
                    "INVALID_INPUT",
                    f"请填写{parameter.label}",
                )
            else:
                expected = type(parameter.default)
                valid = type(value) is expected or expected is float and type(value) is int
                require(
                    valid and parameter.minimum <= value <= parameter.maximum,
                    "INVALID_INPUT",
                    f"{parameter.label}的类型或范围不正确",
                )
            inputs = graph[parameter.node_id]["inputs"]
            if parameter.role == "source_audio":
                if not value:
                    inputs.pop(parameter.input_name, None)
            else:
                inputs[parameter.input_name] = value
                if parameter.role == "source_aspect" and value == "original":
                    inputs["fit"] = "letterbox"
        graph[self.prompt_node]["inputs"][self.prompt_input] = prompt
        graph[self.output_node]["inputs"]["filename_prefix"] = f"video/NovelWriter/{job_id}"
        return graph


def validate_api_graph(graph: JsonObject) -> None:
    require(isinstance(graph, dict) and bool(graph), "INVALID_INPUT", "工作流必须是 API JSON 对象")
    for node_id, node in graph.items():
        require(
            isinstance(node, dict)
            and isinstance(node.get("class_type"), str)
            and isinstance(node.get("inputs"), dict),
            "INVALID_INPUT",
            f"节点 {node_id} 不是 API 格式；请导出 API 工作流 JSON",
        )


def read_api_workflow(path: str | Path) -> JsonObject:
    graph = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    validate_api_graph(graph)
    return graph


def parse_video_workflow(graph: JsonObject) -> VideoWorkflow:
    validate_api_graph(graph)
    if any(isinstance(meta, dict) and meta.get("novelwriter_dual_scene") is True
           for meta in (node.get("_meta") for node in graph.values())):
        from .scail_dual_scene import parse_dual_scene

        return parse_dual_scene(graph)
    if any(node["class_type"] == "SCAILPose2SCAIL2Condition" for node in graph.values()):
        return parse_scail_replacement(graph)
    outputs = [node_id for node_id, node in graph.items() if node["class_type"] == "SaveVideo"]
    require(len(outputs) == 1, "INVALID_INPUT", "工作流需要一个 SaveVideo 输出节点")
    active = output_dependencies(graph, outputs[0])

    generators = []
    for node_id, node in active.items():
        if node["class_type"] in {"MiniMaxH3ReferenceToVideo", "MiniMaxH3ImageToVideo"}:
            generators.append(node_id)
    require(len(generators) == 1, "INVALID_INPUT", "请选择已适配的 MiniMax H3 参考图、首尾帧或 SCAIL-2 换角工作流")
    generator = generators[0]
    return parse_h3_workflow(active, outputs[0], generator)


def output_dependencies(graph: JsonObject, output: str) -> JsonObject:
    active: JsonObject = {}

    def visit(node_id: str) -> None:
        if node_id in active:
            return
        require(node_id in graph, "INVALID_INPUT", f"工作流引用了不存在的节点 {node_id}")
        node = graph[node_id]
        active[node_id] = node
        for value in node["inputs"].values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                visit(value[0])

    visit(output)
    return active


def parse_scail_replacement(graph: JsonObject) -> VideoWorkflow:
    outputs = []
    for node_id, node in graph.items():
        inputs = node["inputs"]
        source = inputs.get("images")
        if (
            node["class_type"] == "VHS_VideoCombine"
            and inputs.get("save_output") is True
            and isinstance(source, list)
            and len(source) == 2
            and isinstance(source[0], str)
            and graph.get(source[0], {}).get("class_type") == "WanVideoDecode"
        ):
            outputs.append(node_id)
    require(len(outputs) == 1, "INVALID_INPUT", "SCAIL-2 工作流需要一个直接保存解码结果的 Video Combine 节点")
    output = outputs[0]
    active = output_dependencies(graph, output)

    def one_node(node_type: str) -> str:
        matches = [node_id for node_id, node in active.items() if node["class_type"] == node_type]
        require(len(matches) == 1, "INVALID_INPUT", f"SCAIL-2 工作流需要一个 {node_type} 节点")
        return matches[0]

    def linked_input(node_id: str, name: str, node_type: str) -> str:
        link = active[node_id]["inputs"].get(name)
        require(
            isinstance(link, list) and len(link) == 2 and isinstance(link[0], str)
            and active.get(link[0], {}).get("class_type") == node_type,
            "INVALID_INPUT",
            f"SCAIL-2 的 {name} 需要连接 {node_type}",
        )
        return link[0]

    condition = one_node("SCAILPose2SCAIL2Condition")
    require(active[condition]["inputs"].get("mode") == "replacement", "INVALID_INPUT", "请选择 SCAIL-2 换角模式")
    mask = one_node("SCAILPose2ColoredMask")
    video_tracker = linked_input(mask, "driving_track_data", "SAM3_VideoTrack")
    image_tracker = linked_input(mask, "ref_track_data", "SAM3_VideoTrack")
    video_text = linked_input(video_tracker, "conditioning", "CLIPTextEncode")
    image_text = linked_input(image_tracker, "conditioning", "CLIPTextEncode")
    image = one_node("LoadImage")
    video = one_node("VHS_LoadVideo")
    sampler = one_node("WanVideoSamplerv2")
    scheduler = linked_input(sampler, "scheduler", "WanVideoSchedulerv2")
    prompt = linked_input(sampler, "text_embeds", "WanVideoTextEncodeCached")
    resize = linked_input(condition, "driving_video", "ResizeImageAdvanced")
    size = linked_input(resize, "scale_to_length", "INTConstant")
    fields = (
        (video_text, "text", "原视频人物描述", None, None),
        (image_text, "text", "参考图角色描述", None, None),
        (video, "frame_load_cap", "最多读取帧数（0 为全片）", 0, 10000),
        (size, "value", "输出长边（像素）", 256, 2048),
        (scheduler, "steps", "采样步数", 1, 100),
        (sampler, "seed", "随机种子", 0, 2**64 - 1),
    )
    parameters = []
    resize_inputs = active[resize]["inputs"]
    aspect = resize_inputs.get("aspect_ratio")
    aspect_choices = SCAIL_ASPECT_RATIO_CHOICES
    if aspect == "custom":
        aspect_choices += (("custom", "工作流自定义"),)
    if resize_inputs.get("resize_mode") == "aspect_ratio" and any(
        aspect == choice for choice, _ in aspect_choices
    ):
        parameters.append(WorkflowParameter(
            resize, "aspect_ratio", "画幅", "original", role="source_aspect", choices=aspect_choices,
        ))
    for node_id, key, label, minimum, maximum in fields:
        value = active[node_id]["inputs"].get(key)
        require(
            isinstance(value, str) and bool(value.strip()) if minimum is None
            else type(value) is int and minimum <= value <= maximum,
            "INVALID_INPUT",
            f"SCAIL-2 的{label}需要可编辑的有效值",
        )
        parameters.append(WorkflowParameter(node_id, key, label, value, minimum, maximum))
    if "audio" in active[output]["inputs"]:
        parameters.append(WorkflowParameter(output, "audio", "保留原声", True, 0, 1, "source_audio"))
    require(isinstance(active[prompt]["inputs"].get("positive_prompt"), str), "INVALID_INPUT", "视频提示词不是文本")
    return VideoWorkflow(
        deepcopy(active), (ImageInput(image, "角色参考图"),), output, prompt, "positive_prompt",
        tuple(parameters), "scail2_replacement", sampler, video,
    )


def parse_h3_workflow(active: JsonObject, output: str, generator: str) -> VideoWorkflow:
    inputs = active[generator]["inputs"]
    image_inputs = []
    kind = "reference"
    if active[generator]["class_type"] == "MiniMaxH3ImageToVideo":
        kind = "first_last"
        for name, label in (("first_frame", "首帧图片"), ("last_frame", "尾帧图片")):
            value = inputs.get(name)
            if value is None:
                continue
            require(isinstance(value, list), "INVALID_INPUT", f"首尾帧工作流需要连接{label}")
            image_inputs.append(ImageInput(value[0], label, name))
    else:
        names = []
        for name in inputs:
            if name.startswith("ref_images.ref_image_"):
                names.append(name)
        require(len(names) <= MAX_REFERENCE_IMAGES, "INVALID_INPUT", "最多使用 9 张参考图片")
        names.sort(key=lambda name: int(name.rsplit("_", 1)[1]))
        for index, name in enumerate(names, 1):
            image_inputs.append(ImageInput(inputs[name][0], f"参考图片 {index}"))
        guides = {}
        for node_id, node in active.items():
            if node["class_type"] == "MiniMaxH3AddGuide":
                frame_index = node["inputs"]["frame_idx"]
                require(
                    frame_index in {0, -1} and frame_index not in guides,
                    "INVALID_INPUT",
                    "当前首尾帧引导需要分别使用 frame_idx = 0 和 -1",
                )
                guides[frame_index] = node_id
        if guides:
            previous = generator
            for frame_index, label, role in (
                (0, "首帧图片", "first_frame"),
                (-1, "尾帧图片", "last_frame"),
            ):
                if frame_index not in guides:
                    continue
                guide_id = guides[frame_index]
                guide = active[guide_id]["inputs"]
                require(
                    guide["positive"] == [previous, 0] and guide["latent"] == [generator, 1],
                    "INVALID_INPUT",
                    "首尾帧引导应依次连接参考图条件，并使用同一生成潜空间",
                )
                image_inputs.append(ImageInput(guide["image"][0], label, role))
                previous = guide_id
            kind = "reference_first_last"
    require(bool(image_inputs), "INVALID_INPUT", "工作流没有连接图片输入")
    for image in image_inputs:
        require(
            active[image.node_id]["class_type"] == "LoadImage",
            "INVALID_INPUT",
            f"{image.label}需要直接连接 LoadImage 节点",
        )
    mapped = {image.node_id for image in image_inputs}
    connected = {node_id for node_id, node in active.items() if node["class_type"] == "LoadImage"}
    require(
        len(mapped) == len(image_inputs) and mapped == connected,
        "INVALID_INPUT",
        "图片输入需要独立的 LoadImage 节点，且所有有效图片输入均须属于已适配模板",
    )
    prompt_node, prompt_input = generator, "prompt"
    value = inputs[prompt_input]
    if isinstance(value, list):
        prompt_node = value[0]
        require(
            active[prompt_node]["class_type"] in {"PrimitiveString", "PrimitiveStringMultiline"},
            "INVALID_INPUT",
            "视频提示词需要直接文本或 PrimitiveString 文本节点",
        )
        prompt_input = "value"
    require(
        isinstance(active[prompt_node]["inputs"][prompt_input], str),
        "INVALID_INPUT",
        "视频提示词不是文本",
    )
    parameters = []
    for node_id, node in active.items():
        node_type = node["class_type"]
        title = node.get("_meta", {}).get("title", node_type)
        if node_type == "RandomNoise":
            parameters.append(
                WorkflowParameter(
                    node_id, "noise_seed", "随机种子", node["inputs"]["noise_seed"], 0, 2**64 - 1
                )
            )
        elif node_type == "PrimitiveBoolean":
            label = "加速模式（Lightning）" if "Lightning" in title else title
            parameters.append(
                WorkflowParameter(node_id, "value", label, node["inputs"]["value"], 0, 1)
            )
        elif node_type == "PrimitiveInt":
            label = title
            if title == "Int (Full)":
                label = "采样步数（常规）"
            elif title == "Int (Lightning LoRA)":
                label = "采样步数（加速）"
            parameters.append(
                WorkflowParameter(node_id, "value", label, node["inputs"]["value"], 1, 10000)
            )
        elif node_type == "ResolutionSelector":
            aspect = node["inputs"].get("aspect_ratio")
            if (
                inputs.get("width") == [node_id, 0]
                and inputs.get("height") == [node_id, 1]
                and any(aspect == choice for choice, _ in ASPECT_RATIO_CHOICES)
            ):
                parameters.append(
                    WorkflowParameter(
                        node_id,
                        "aspect_ratio",
                        "画幅",
                        SHOT_ASPECT_RATIO,
                        choices=ASPECT_RATIO_CHOICES,
                    )
                )
            parameters.append(
                WorkflowParameter(
                    node_id, "megapixels", "分辨率（百万像素）", SHOT_MEGAPIXELS, 0.1, 16
                )
            )
        elif node_type == "ComfyMathExpression" and "values.a" in node["inputs"]:
            duration_node = node["inputs"]["values.a"][0]
            if active[duration_node]["class_type"] == "PrimitiveFloat":
                parameters.append(
                    WorkflowParameter(
                        duration_node,
                        "value",
                        "时长（秒）",
                        float(active[duration_node]["inputs"]["value"]),
                        1,
                        150,
                        role="duration",
                    )
                )
    return VideoWorkflow(
        deepcopy(active),
        tuple(image_inputs),
        output,
        prompt_node,
        prompt_input,
        tuple(parameters),
        kind,
        generator,
    )
