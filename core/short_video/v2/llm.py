"""共享模型配置，发送文字请求并保存运行记录。"""

from __future__ import annotations

import asyncio

from core.generation import api_providers
from core.generation.cancellation import CancelToken

from .contracts import RequestRecorder, TextTransport
from .models import WorkspaceError


class RecordingError(RuntimeError):
    """A failed durable write must never trigger another paid request."""


class ScriptLLM:
    def __init__(
        self,
        model: str,
        *,
        token: CancelToken | None = None,
        record: RequestRecorder | None = None,
        transport: TextTransport | None = None,
    ) -> None:
        self.model = model
        self.token = token or CancelToken()
        self.record = record or (lambda *args: None)
        self.transport = transport

    def __call__(self, prompt: str) -> str:
        self.token.raise_if_cancelled()
        response = ""
        error = None
        try:
            if self.transport is not None:
                # 注入发送器用于离线测试，生产请求使用 SDK。
                response = self.transport(prompt)
            else:
                config = api_providers.get_config(self.model)

                async def request() -> str:
                    async with api_providers.AsyncOpenAI(
                        api_key=config.api_key,
                        base_url=config.base_url,
                        max_retries=0,
                    ) as client:
                        result = await client.chat.completions.create(
                            model=config.model,
                            messages=[{"role": "user", "content": prompt}],
                            max_tokens=8192,
                            temperature=0.7,
                            extra_body=api_providers.request_options(self.model),
                        )
                        return result.choices[0].message.content

                response = asyncio.run(request())
            if not isinstance(response, str):
                response = ""  # A malformed SDK value must not corrupt the durable text record.
                raise WorkspaceError("INVALID_OUTPUT", "模型未返回文字")
            return response
        except Exception as exc:
            # Record only the failure type; preserve the original exception for the caller.
            error = type(
                exc
            ).__name__  # SDK error strings can contain credentials; never persist them.
            raise
        finally:
            try:
                self.record(prompt, response, error)
            except OSError as exc:
                raise RecordingError(
                    "运行记录保存失败，停止后续模型请求；已有项目文件保留"
                ) from exc
        # The caller checks cancellation after the returned content is durable.
