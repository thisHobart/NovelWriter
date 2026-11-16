"""Fixed-workflow video tasks, separate from the text Agent Loop."""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from uuid import uuid4

import httpx

from core.generation.cancellation import CancelToken, GenerationCancelled

from .comfy_mcp import ComfyMCPClient
from .contracts import JsonObject, ProgressReporter
from .feedback import TaskTracker, failure_feedback
from .media_repository import MediaRepository
from .media_workflows import MediaConfig, parse_video_workflow
from .models import encode, fingerprint, require, timestamp


def validate_nodes(workflow: JsonObject, nodes: JsonObject) -> None:
    for node_id, node in workflow.items():
        kind = node["class_type"]
        require(kind in nodes, "INVALID_INPUT", f"ComfyUI 缺少节点 {node_id}：{kind}")
        definitions = nodes[kind]["input"]
        inputs = {**definitions.get("required", {}), **definitions.get("optional", {})}
        for name, definition in inputs.items():
            # 模板内的旧文件名会在上传后替换，不能用服务器现有文件列表提前拒绝。
            if kind == "LoadImage" and name == "image":
                continue
            if name not in node["inputs"]:
                continue
            value = node["inputs"][name]
            if definition[0] in ("INT", "FLOAT") and type(value) in (int, float):
                bounds = definition[1]
                require(
                    bounds.get("min", float("-inf")) <= value <= bounds.get("max", float("inf")),
                    "INVALID_INPUT",
                    f"节点 {node_id} 的 {name} 超出允许范围：{value}",
                )
            choices = definition[0]
            if choices == "COMBO":
                choices = definition[1]["options"]
            if isinstance(choices, list) and not isinstance(value, list):
                require(
                    value in choices, "INVALID_INPUT", f"节点 {node_id} 的 {name} 不可用：{value}"
                )


class MediaService:
    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = str(Path(output_dir).resolve())
        self.repository = MediaRepository(output_dir)

    def prepare_video(
        self,
        images: dict[str, str | None],
        prompt: str,
        *,
        workflow_id: str | None = None,
        parameters: JsonObject | None = None,
        target: JsonObject | None = None,
        request_id: str | None = None,
    ) -> JsonObject:
        config = MediaConfig.load(self.output_dir)
        config.validate()
        selected = config.selected(workflow_id)
        template = parse_video_workflow(selected["graph"])
        selected_images = {}
        for node_id, image_id in images.items():
            if image_id is not None:
                selected_images[node_id] = image_id
        template = template.prepare_image_inputs(list(selected_images))
        # 在本地确认所有输入完整且参数类型正确，再进行外部上传与提交。
        template.bind(selected_images, prompt, "validation", parameters or {})
        for number in re.findall(r"<Picture\s+(\d+)>", prompt, flags=re.IGNORECASE):
            require(
                1 <= int(number) <= template.reference_count,
                "INVALID_INPUT",
                f"当前工作流连接 {template.reference_count} 张参考图片，提示词引用超出范围",
            )
        records = {}
        for node_id, image_id in selected_images.items():
            image = self.repository.image(image_id)
            data = self.repository.resolve(image["relative_path"]).read_bytes()
            require(
                hashlib.sha256(data).hexdigest() == image["sha256"],
                "INVALID_INPUT",
                "图片已被修改，请重新导入",
            )
            records[node_id] = image
        return {
            "request_id": request_id or uuid4().hex,
            "base_url": config.base_url,
            "workflow_id": selected["workflow_id"],
            "workflow_name": selected["name"],
            "images": records,
            "parameters": parameters or {},
            "prompt": prompt,
            "target": target or {},
            "workflow": template.graph,
            "workflow_hash": fingerprint(template.graph),
            "output_node": template.output_node,
        }


class MediaWork(TaskTracker):
    def __init__(
        self,
        output_dir: str,
        command: JsonObject | None = None,
        *,
        job_id: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__("视频生成")
        self.output_dir, self.command, self.job_id = output_dir, command, job_id
        self.transport = transport
        self.token = CancelToken()

    def __call__(self, report: ProgressReporter) -> JsonObject:
        return asyncio.run(self.execute(report))

    async def execute(self, report: ProgressReporter) -> JsonObject:
        repository = MediaRepository(self.output_dir)
        if self.job_id is not None:
            job = repository.job(self.job_id)
        else:
            assert self.command is not None
            job = None
            for existing in repository.jobs():
                if existing["request_id"] == self.command["request_id"]:
                    require(
                        existing["command_hash"] == fingerprint(self.command),
                        "INVALID_INPUT",
                        "同一请求编号的输入不一致",
                    )
                    job = existing
                    break
            if job is None:
                job = {
                    "job_id": uuid4().hex,
                    "request_id": self.command["request_id"],
                    "command_hash": fingerprint(self.command),
                    "input_snapshot": self.command,
                    "status": "prepared",
                    "remote_job_id": None,
                    "outputs": [],
                    "created_at": timestamp(),
                }
                repository.save_job(job)
        if job["status"] == "succeeded":
            return job
        command = job["input_snapshot"]
        self.update_context(target="ComfyUI", phase="连接服务")
        client = ComfyMCPClient(command["base_url"], repository, transport=self.transport)
        try:
            report("正在连接 ComfyUI…", -1)
            async with client.connect():
                if job["status"] == "prepared":
                    self.token.raise_if_cancelled()
                    self.update_context(phase="工作流校验")
                    report("正在校验 ComfyUI 工作流…", -1)
                    information = await client.call("comfy_status")
                    validate_nodes(command["workflow"], information["nodes"])
                    self.update_context(phase="上传参考图片")
                    report("正在上传参考图片…", -1)
                    if "images" in command:
                        images = command["images"]
                    else:
                        # 已提交的旧单图任务仍可按原始快照继续执行。
                        images = {command["image_node"]: command["image"]}
                    uploaded_images = {}
                    for node_id, image in images.items():
                        self.token.raise_if_cancelled()
                        uploaded = await client.call(
                            "comfy_upload_image", {"image_id": image["image_id"]}
                        )
                        uploaded_images[node_id] = uploaded["image"]
                    self.token.raise_if_cancelled()
                    template = parse_video_workflow(command["workflow"])
                    workflow = template.bind(
                        uploaded_images,
                        command["prompt"],
                        job["job_id"],
                        command.get("parameters", {}),
                    )
                    job["submitted_workflow"] = workflow
                    job["status"] = "submitting"
                    repository.save_job(job)
                    self.update_context(phase="提交远程任务")
                    report("正在提交视频任务…", -1)
                    submitted = await client.call(
                        "comfy_submit_workflow",
                        {"workflow": workflow, "request_id": job["request_id"]},
                    )
                    require(
                        bool(submitted.get("prompt_id")),
                        "INVALID_OUTPUT",
                        "ComfyUI 未返回任务编号：" + encode(submitted),
                    )
                    job["remote_job_id"] = submitted["prompt_id"]
                    job["status"] = "queued"
                    repository.save_job(job)
                if job["remote_job_id"] is None:
                    self.update_context(phase="查询原任务")
                    report("正在查询原视频任务…", -1)
                    found = await client.call(
                        "comfy_find_request", {"request_id": job["request_id"]}
                    )
                    job["remote_job_id"] = found["remote_job_id"]
                    if job["remote_job_id"] is None:
                        job["status"] = "unknown"
                        repository.save_job(job)
                        return job
                while True:
                    self.update_context(phase="等待远程视频")
                    self.token.raise_if_cancelled(
                        "已停止本地等待；ComfyUI 可能仍在运行，可查询原任务"
                    )
                    result = await client.call(
                        "comfy_get_job", {"remote_job_id": job["remote_job_id"]}
                    )
                    if result["state"] == "finished":
                        return await self.collect(
                            client, repository, job, result["history"], report
                        )
                    previous_status = job["status"]
                    job["status"] = result["state"]
                    if job["status"] != previous_status:
                        repository.save_job(job)
                    if result["state"] == "unknown":
                        return job
                    if job["status"] != previous_status:
                        report(
                            "ComfyUI 正在生成视频…"
                            if result["state"] == "running"
                            else "ComfyUI 视频任务正在排队…",
                            -1,
                        )
                    for _ in range(10):
                        self.token.raise_if_cancelled(
                            "已停止本地等待；ComfyUI 可能仍在运行，可查询原任务"
                        )
                        await asyncio.sleep(0.2)
        except GenerationCancelled as exc:
            job["status"] = "interrupted" if job["status"] != "prepared" else "cancelled"
            self.update_context(remote_status=job["status"])
            try:
                repository.save_job(job)
            except OSError as state_error:
                exc.add_note(
                    f"另有媒体失败状态保存错误：{type(state_error).__name__}: {state_error}"
                )
            raise
        except Exception as exc:
            # 任务边界维护失败状态，原始异常交给 TaskRunner 统一记录和展示。
            if job["status"] == "submitting":
                rejected = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {
                    400,
                    401,
                    403,
                    404,
                    422,
                }
                job["status"] = "failed" if rejected else "unknown"
            elif job["remote_job_id"] is None:
                job["status"] = "failed"
            self.update_context(remote_status=job["status"])
            job["error"] = {"type": type(exc).__name__, "message": str(exc)}
            job["feedback"] = failure_feedback(self.task_context, exc)
            try:
                repository.save_job(job)
            except OSError as state_error:
                exc.add_note(
                    f"另有媒体失败状态保存错误：{type(state_error).__name__}: {state_error}"
                )
            raise

    async def collect(
        self,
        client: ComfyMCPClient,
        repository: MediaRepository,
        job: JsonObject,
        history: JsonObject,
        report: ProgressReporter,
    ) -> JsonObject:
        self.update_context(phase="读取远程结果")
        status = history["status"]
        if status["status_str"] == "error":
            job["status"] = "failed"
            repository.save_job(job)
            raise RuntimeError("ComfyUI 执行失败：" + encode(status.get("messages", [])))
        require(status.get("completed") is True, "INVALID_OUTPUT", "ComfyUI 历史任务未完成")
        output_node = job["input_snapshot"]["output_node"]
        outputs = history["outputs"].get(output_node, {})
        descriptors = []
        for field in ("images", "videos", "gifs"):
            descriptors.extend(outputs.get(field, []))
        require(bool(descriptors), "INVALID_OUTPUT", f"输出节点 {output_node} 未返回视频文件")
        job["status"] = "collecting"
        repository.save_job(job)
        self.update_context(phase="保存视频")
        report("正在保存生成的视频…", -1)
        saved = []
        for index, descriptor in enumerate(descriptors):
            self.token.raise_if_cancelled()
            saved.append(
                await client.call(
                    "comfy_download_video",
                    {
                        "job_id": job["job_id"],
                        "output": descriptor,
                        "index": index,
                    },
                )
            )
        job["outputs"] = saved
        job["status"] = "succeeded"
        job.pop("error", None)
        job.pop("feedback", None)
        repository.save_job(job)
        self.update_context(saved=True, phase="视频已保存")
        return job
