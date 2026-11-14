"""Single-image reference-to-video tasks, separate from the text Agent Loop."""

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
from .media_repository import MediaRepository
from .media_workflows import MediaConfig, VideoWorkflow, load_video_workflow
from .models import encode, fingerprint, require, timestamp


def validate_nodes(workflow: JsonObject, nodes: JsonObject) -> None:
    for node_id, node in workflow.items():
        kind = node["class_type"]
        require(kind in nodes, "INVALID_INPUT", f"ComfyUI 缺少节点 {node_id}：{kind}")
        required = nodes[kind]["input"].get("required", {})
        for name, definition in required.items():
            if name not in node["inputs"]:
                continue
            value = node["inputs"][name]
            choices = definition[0]
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
        image_id: str,
        prompt: str,
        *,
        target: JsonObject | None = None,
        request_id: str | None = None,
    ) -> JsonObject:
        config = MediaConfig.load(self.output_dir)
        config.validate()
        template = load_video_workflow(config.video_workflow)
        require(bool(prompt.strip()), "INVALID_INPUT", "请填写视频提示词")
        for number in re.findall(r"<Picture\s+(\d+)>", prompt, flags=re.IGNORECASE):
            require(
                number == "1",
                "INVALID_INPUT",
                "当前工作流只连接一张图片，请将视频提示词改为仅引用 <Picture 1>",
            )
        image = self.repository.image(image_id)
        data = self.repository.resolve(image["relative_path"]).read_bytes()
        require(
            hashlib.sha256(data).hexdigest() == image["sha256"],
            "INVALID_INPUT",
            "图片已被修改，请重新导入",
        )
        return {
            "request_id": request_id or uuid4().hex,
            "base_url": config.base_url,
            "image": image,
            "prompt": prompt,
            "target": target or {},
            "workflow": template.graph,
            "workflow_hash": fingerprint(template.graph),
            "image_node": template.image_node,
            "output_node": template.output_node,
            "prompt_node": template.prompt_node,
            "prompt_input": template.prompt_input,
        }


class MediaWork:
    def __init__(
        self,
        output_dir: str,
        command: JsonObject | None = None,
        *,
        job_id: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
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
        client = ComfyMCPClient(command["base_url"], repository, transport=self.transport)
        try:
            async with client.connect():
                if job["status"] == "prepared":
                    self.token.raise_if_cancelled()
                    report("正在校验 ComfyUI 工作流…", -1)
                    information = await client.call("comfy_status")
                    validate_nodes(command["workflow"], information["nodes"])
                    report("正在上传参考图片…", -1)
                    uploaded = await client.call(
                        "comfy_upload_image", {"image_id": command["image"]["image_id"]}
                    )
                    self.token.raise_if_cancelled()
                    template = VideoWorkflow(
                        command["workflow"],
                        command["image_node"],
                        command["output_node"],
                        command["prompt_node"],
                        command["prompt_input"],
                    )
                    workflow = template.bind(uploaded["image"], command["prompt"], job["job_id"])
                    job["submitted_workflow"] = workflow
                    job["status"] = "submitting"
                    repository.save_job(job)
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
                    found = await client.call(
                        "comfy_find_request", {"request_id": job["request_id"]}
                    )
                    job["remote_job_id"] = found["remote_job_id"]
                    if job["remote_job_id"] is None:
                        job["status"] = "unknown"
                        repository.save_job(job)
                        return job
                while True:
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
            job["error"] = {"type": type(exc).__name__, "message": str(exc)}
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
        repository.save_job(job)
        return job
