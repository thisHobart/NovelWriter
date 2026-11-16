"""Embedded MCP bridge for the local ComfyUI server, without a child process."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .contracts import JsonObject
from .media_repository import MediaRepository
from .models import require


class ComfyMCPClient:
    def __init__(
        self,
        base_url: str,
        repository: MediaRepository,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url, self.repository, self.transport = base_url, repository, transport
        self.original_error: Exception | None = None

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[ComfyMCPClient]:
        # localhost 不使用系统代理；每次 HTTP 操作有网络超时，GPU 任务不设总期限。
        async with httpx.AsyncClient(
            base_url=self.base_url.rstrip("/"),
            trust_env=False,
            timeout=httpx.Timeout(60, connect=5),
            transport=self.transport,
        ) as http:
            self.http = http
            server = self.create_server()
            async with AsyncExitStack() as resources:
                self.session = await resources.enter_async_context(Client(server))
                try:
                    yield self
                finally:
                    # 先正常关闭 SDK 的 TaskGroup，避免把业务异常包装成 ExceptionGroup。
                    await resources.aclose()

    async def invoke(self, operation: Callable[[], Awaitable[JsonObject]]) -> JsonObject:
        # SDK 会隐藏意外工具错误；内置服务保留原对象，在客户端边界重新抛出。
        try:
            return await operation()
        except Exception as exc:
            self.original_error = exc
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc

    async def call(self, name: str, arguments: JsonObject | None = None) -> JsonObject:
        self.original_error = None
        result = await self.session.call_tool(name, arguments or {})
        if self.original_error is not None:
            raise self.original_error
        if result.is_error:
            messages = []
            for item in result.content:
                if item.type == "text":
                    messages.append(item.text)
            raise RuntimeError("MCP 工具调用失败：" + "\n".join(messages))
        require(
            isinstance(result.structured_content, dict),
            "INVALID_OUTPUT",
            f"MCP 工具 {name} 未返回对象",
        )
        return result.structured_content

    async def request_json(self, method: str, path: str, **kwargs: Any) -> JsonObject:
        response = await self.http.request(method, path, **kwargs)
        response.raise_for_status()
        result = response.json()
        require(isinstance(result, dict), "INVALID_OUTPUT", f"ComfyUI {path} 未返回 JSON 对象")
        return result

    def create_server(self) -> MCPServer:
        server = MCPServer("NovelWriter ComfyUI", log_level="ERROR")

        @server.tool()
        async def comfy_status() -> JsonObject:
            """Read server information and node definitions without generating media."""

            async def read() -> JsonObject:
                system = await self.request_json("GET", "/system_stats")
                nodes = await self.request_json("GET", "/object_info")
                return {"system": system["system"], "nodes": nodes}

            return await self.invoke(read)

        @server.tool()
        async def comfy_upload_image(image_id: str) -> JsonObject:
            """Upload an image already imported into this project's media repository."""

            async def upload() -> JsonObject:
                record = self.repository.image(image_id)
                path = self.repository.resolve(record["relative_path"])
                data = path.read_bytes()
                require(
                    hashlib.sha256(data).hexdigest() == record["sha256"],
                    "INVALID_INPUT",
                    "导入图片已被外部修改，请重新导入",
                )
                result = await self.request_json(
                    "POST",
                    "/upload/image",
                    files={"image": (path.name, data)},
                    data={"type": "input", "subfolder": "NovelWriter", "overwrite": "false"},
                )
                require(
                    result.get("type") == "input", "INVALID_OUTPUT", "ComfyUI 未保存到图片输入目录"
                )
                name, folder = result["name"], result.get("subfolder", "")
                return {"image": f"{folder}/{name}" if folder else name}

            return await self.invoke(upload)

        @server.tool()
        async def comfy_submit_workflow(workflow: JsonObject, request_id: str) -> JsonObject:
            """Submit exactly once; retain a client tag for reconciling lost responses."""
            return await self.invoke(
                lambda: self.request_json(
                    "POST",
                    "/prompt",
                    json={
                        "prompt": workflow,
                        "client_id": request_id,
                        "extra_data": {"novelwriter_request_id": request_id},
                    },
                )
            )

        @server.tool()
        async def comfy_get_job(remote_job_id: str) -> JsonObject:
            """Read the history and queue of one previously submitted workflow."""

            async def read() -> JsonObject:
                history = await self.request_json("GET", f"/history/{remote_job_id}")
                if remote_job_id in history:
                    return {"history": history[remote_job_id], "state": "finished"}
                queue = await self.request_json("GET", "/queue")
                for key, state in [("queue_running", "running"), ("queue_pending", "queued")]:
                    for item in queue[key]:
                        if item[1] == remote_job_id:
                            return {"state": state}
                return {"state": "unknown"}

            return await self.invoke(read)

        @server.tool()
        async def comfy_find_request(request_id: str) -> JsonObject:
            """Find an accepted request after the submission response was lost."""

            async def find() -> JsonObject:
                queue = await self.request_json("GET", "/queue")
                history = await self.request_json("GET", "/history")
                prompts = queue["queue_running"] + queue["queue_pending"]
                for item in history.values():
                    prompts.append(item["prompt"])
                for prompt in prompts:
                    if prompt[3].get("novelwriter_request_id") == request_id:
                        return {"remote_job_id": prompt[1]}
                return {"remote_job_id": None}

            return await self.invoke(find)

        @server.tool()
        async def comfy_download_video(job_id: str, output: JsonObject, index: int) -> JsonObject:
            """Retrieve one declared output and save its complete video file locally."""

            async def download() -> JsonObject:
                suffix = Path(output["filename"]).suffix.lower()
                require(
                    suffix in {".mp4", ".mov", ".webm", ".mkv"},
                    "INVALID_OUTPUT",
                    f"输出不是支持的视频文件：{suffix}",
                )
                response = await self.http.get(
                    "/view",
                    params={
                        "filename": output["filename"],
                        "subfolder": output.get("subfolder", ""),
                        "type": output.get("type", "output"),
                    },
                )
                response.raise_for_status()
                data = response.content
                require(
                    data[4:8] == b"ftyp" or data.startswith(b"\x1aE\xdf\xa3"),
                    "INVALID_OUTPUT",
                    "下载内容不是 MP4／MOV／WebM／MKV 视频",
                )
                relative_path = self.repository.video_output_path(job_id, index, suffix)
                self.repository.write_file(relative_path, data)
                return {
                    "relative_path": relative_path,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                    "remote_output_ref": output,
                    "metadata": output.get("metadata", {}),
                }

            return await self.invoke(download)

        return server
