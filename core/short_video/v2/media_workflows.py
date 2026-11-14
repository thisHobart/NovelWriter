"""ComfyUI configuration and explicit inputs of the selected API workflow."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import JsonObject
from .models import encode, require
from .storage import atomic_write_text

DEFAULT_VIDEO_WORKFLOW = Path(__file__).with_name("workflows") / "minimax_h3_reference.json"


@dataclass
class MediaConfig:
    base_url: str = "http://127.0.0.1:8188"
    video_workflow: str = str(DEFAULT_VIDEO_WORKFLOW)
    image_workflow: str = ""

    @classmethod
    def load(cls, output_dir: str | Path) -> MediaConfig:
        path = Path(output_dir) / "short_video" / "media" / "settings.json"
        if not path.exists():
            return cls()
        values = json.loads(path.read_text(encoding="utf-8"))
        if not values.get("video_workflow"):
            values["video_workflow"] = str(DEFAULT_VIDEO_WORKFLOW)
        return cls(**values)

    def validate(self, *, include_image: bool = False) -> None:
        address = urlsplit(self.base_url)
        require(
            address.scheme in {"http", "https"} and bool(address.hostname),
            "INVALID_INPUT",
            "请输入有效的 ComfyUI HTTP 地址",
        )
        load_video_workflow(self.video_workflow)
        if include_image and self.image_workflow:
            read_api_workflow(self.image_workflow)

    def save(self, output_dir: str | Path) -> None:
        self.validate(include_image=True)
        path = Path(output_dir) / "short_video" / "media" / "settings.json"
        values = asdict(self)
        if Path(self.video_workflow).resolve() == DEFAULT_VIDEO_WORKFLOW.resolve():
            # 单文件安装包每次解压目录不同，不能把本次 _MEIPASS 路径写进配置。
            values["video_workflow"] = ""
        atomic_write_text(path, encode(values))


@dataclass(frozen=True)
class VideoWorkflow:
    graph: JsonObject
    image_node: str
    output_node: str
    prompt_node: str
    prompt_input: str

    @property
    def prompt(self) -> str:
        return self.graph[self.prompt_node]["inputs"][self.prompt_input]

    def bind(self, image_name: str, prompt: str, job_id: str) -> JsonObject:
        require(bool(prompt.strip()), "INVALID_INPUT", "请填写视频提示词")
        graph = deepcopy(self.graph)
        graph[self.image_node]["inputs"]["image"] = image_name
        graph[self.prompt_node]["inputs"][self.prompt_input] = prompt
        graph[self.output_node]["inputs"]["filename_prefix"] = f"video/NovelWriter/{job_id}"
        return graph


def read_api_workflow(path: str | Path) -> JsonObject:
    graph = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    require(isinstance(graph, dict) and bool(graph), "INVALID_INPUT", "工作流必须是 API JSON 对象")
    for node_id, node in graph.items():
        require(
            isinstance(node, dict)
            and isinstance(node.get("class_type"), str)
            and isinstance(node.get("inputs"), dict),
            "INVALID_INPUT",
            f"节点 {node_id} 不是 API 格式；请导出 API 工作流 JSON",
        )
    return graph


def load_video_workflow(path: str | Path) -> VideoWorkflow:
    graph = read_api_workflow(path)
    outputs = []
    for node_id, node in graph.items():
        if node["class_type"] == "SaveVideo":
            outputs.append(node_id)
    require(len(outputs) == 1, "INVALID_INPUT", "第一期工作流需要一个 SaveVideo 输出节点")
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
    images = []
    prompt_inputs = []
    for node_id, node in active.items():
        if node["class_type"] == "LoadImage":
            images.append(node_id)
        if node["class_type"] == "MiniMaxH3ReferenceToVideo":
            prompt_inputs.append((node_id, "prompt"))
    require(len(images) == 1, "INVALID_INPUT", "第一期仅支持一个实际接入输出的 LoadImage 节点")
    require(
        len(prompt_inputs) == 1, "INVALID_INPUT", "请使用已适配的 MiniMax H3 参考图转视频工作流"
    )
    prompt_node, prompt_input = prompt_inputs[0]
    value = active[prompt_node]["inputs"][prompt_input]
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
    return VideoWorkflow(active, images[0], outputs[0], prompt_node, prompt_input)
