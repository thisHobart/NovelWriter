"""Project workflow catalog and the supported MiniMax H3 input mappings."""

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
        values.pop("image_workflow", None)
        if "video_workflow" in values:
            # 原单工作流配置转为项目内 JSON 快照，不再依赖外部文件路径。
            previous = values.pop("video_workflow")
            workflows = default_workflows()
            if previous:
                workflows[0]["graph"] = read_api_workflow(previous)
            values["workflows"] = workflows
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
    default: int | float | bool
    minimum: float
    maximum: float
    role: str = ""

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

    def bind(
        self, image_names: dict[str, str], prompt: str, job_id: str, parameters: JsonObject
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
        for image in self.image_inputs:
            graph[image.node_id]["inputs"]["image"] = image_names[image.node_id]
        for parameter in self.parameters:
            value = parameters.get(parameter.key, parameter.default)
            expected = type(parameter.default)
            valid = type(value) is expected or expected is float and type(value) is int
            require(
                valid and parameter.minimum <= value <= parameter.maximum,
                "INVALID_INPUT",
                f"{parameter.label}的类型或范围不正确",
            )
            graph[parameter.node_id]["inputs"][parameter.input_name] = value
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
    outputs = []
    for node_id, node in graph.items():
        if node["class_type"] == "SaveVideo":
            outputs.append(node_id)
    require(len(outputs) == 1, "INVALID_INPUT", "工作流需要一个 SaveVideo 输出节点")
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

    visit(outputs[0])
    generators = []
    for node_id, node in active.items():
        if node["class_type"] in {"MiniMaxH3ReferenceToVideo", "MiniMaxH3ImageToVideo"}:
            generators.append(node_id)
    require(len(generators) == 1, "INVALID_INPUT", "请选择已适配的 MiniMax H3 参考图或首尾帧工作流")
    generator = generators[0]
    inputs = active[generator]["inputs"]
    image_inputs = []
    kind = "reference"
    if active[generator]["class_type"] == "MiniMaxH3ImageToVideo":
        kind = "first_last"
        for name, label in (("first_frame", "首帧图片"), ("last_frame", "尾帧图片")):
            value = inputs.get(name)
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
            require(set(guides) == {0, -1}, "INVALID_INPUT", "请同时连接首帧和尾帧引导")
            previous = generator
            for frame_index, label, role in (
                (0, "首帧图片", "first_frame"),
                (-1, "尾帧图片", "last_frame"),
            ):
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
            parameters.append(
                WorkflowParameter(
                    node_id,
                    "megapixels",
                    "分辨率（百万像素）",
                    node["inputs"]["megapixels"],
                    0.1,
                    16,
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
        outputs[0],
        prompt_node,
        prompt_input,
        tuple(parameters),
        kind,
        generator,
    )
