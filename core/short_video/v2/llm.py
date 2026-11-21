"""共享模型配置，发送文字或图文请求并保存运行记录。"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from uuid import uuid4

import httpx
from openai import APIConnectionError, APIStatusError

from core.common import api_providers
from core.common.cancellation import CancelToken

from .contracts import JsonObject, ModelRequest, RequestRecorder, TextTransport
from .llm_trace import enabled, endpoint_label, trace_event
from .models import WorkspaceError, require

NETWORK_MAX_RETRIES = 2
MAX_RETRY_WAIT = 60.0
RetryReporter = Callable[[Exception, int, int, float], None]


def retry_delay(error: Exception, attempt: int) -> float | None:
    """Only retry transient transport/service failures, never local storage errors."""
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None)
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
    if isinstance(error, (APIStatusError, httpx.HTTPStatusError)):
        if status not in {408, 409, 429} and not (isinstance(status, int) and 500 <= status < 600):
            return None
        if response is not None and response.headers.get("x-should-retry") == "false":
            return None
    elif not isinstance(
        error, (APIConnectionError, httpx.TransportError, TimeoutError, ConnectionError)
    ):
        return None
    if response is not None:
        for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
            value = response.headers.get(name)
            if value is None:
                continue
            try:
                delay = float(value) * scale
            except ValueError:
                try:
                    delay = parsedate_to_datetime(value).timestamp() - time.time()
                except (ValueError, TypeError, OverflowError):
                    continue
            if delay > MAX_RETRY_WAIT:
                return None  # Do not resend earlier than the server's requested delay.
            if 0 <= delay <= MAX_RETRY_WAIT:
                return delay
    return min(2.0**attempt, 8.0) + random.uniform(0, 0.25)


class RecordingError(RuntimeError):
    """A failed durable write must never trigger another paid request."""


def _valid_message(message: object) -> bool:
    if not isinstance(message, dict) or message.get("role") not in ("system", "user", "assistant"):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    return (
        message["role"] == "user"
        and isinstance(content, list)
        and bool(content)
        and all(
            isinstance(part, dict)
            and (
                (part.get("type") == "text" and isinstance(part.get("text"), str))
                or (
                    part.get("type") == "image_url"
                    and isinstance(part.get("image_url"), dict)
                    and isinstance(part["image_url"].get("url"), str)
                    and bool(part["image_url"]["url"])
                )
            )
            for part in content
        )
    )


class ScriptLLM:
    def __init__(
        self,
        model: str,
        *,
        token: CancelToken | None = None,
        record: RequestRecorder | None = None,
        transport: TextTransport | None = None,
        network_retries: int | None = None,
        before_request: Callable[[], None] | None = None,
        on_retry: RetryReporter | None = None,
        purpose: str = "generation",
        trace_context: Callable[[], JsonObject] | None = None,
    ) -> None:
        self.model = model
        self.token = token or CancelToken()
        self.record = record or (lambda *args: None)
        self.transport = transport
        require(purpose in {"generation", "review"}, "INVALID_INPUT", "未知模型用途")
        self.purpose = purpose
        self.trace_context = trace_context
        self.trace_id = uuid4().hex[:12]
        self.trace_secrets: tuple[str, ...] = ()
        self.network_retries = NETWORK_MAX_RETRIES if network_retries is None else network_retries
        require(
            type(self.network_retries) is int and self.network_retries >= 0,
            "INVALID_INPUT",
            "网络重试次数必须为非负整数",
        )
        self.before_request = before_request or (lambda: None)
        self.on_retry = on_retry

    def __call__(self, prompt: ModelRequest) -> str:
        """A plain prompt or role/content messages, including optional user image parts."""
        has_images = False
        if not isinstance(prompt, str):
            require(
                isinstance(prompt, list)
                and bool(prompt)
                and all(_valid_message(m) for m in prompt),
                "INVALID_INPUT",
                "模型请求的消息格式无效",
            )
            has_images = any(
                part["type"] == "image_url"
                for message in prompt
                if isinstance(message["content"], list)
                for part in message["content"]
            )
        for attempt in range(self.network_retries + 1):
            self.token.raise_if_cancelled()
            self.before_request()
            try:
                response = self._request(prompt)
            except Exception as exc:
                self.trace(
                    "API_ERROR",
                    {"type": type(exc).__name__, "message": str(exc)},
                    network_attempt=attempt,
                )
                # Recording is outside the retried operation: a disk/hook failure
                # must stop immediately, even if it resembles a network exception.
                self._record(prompt, "", type(exc).__name__)
                delay = retry_delay(exc, attempt) if attempt < self.network_retries else None
                if delay is None:
                    if has_images and isinstance(exc, APIStatusError):
                        if exc.status_code == 413:
                            raise WorkspaceError(
                                "IMAGE_INPUT_TOO_LARGE",
                                "生成模型拒绝了过大的图片请求，请缩小图片后重试",
                            ) from None
                        if exc.status_code in {400, 415, 422}:
                            raise WorkspaceError(
                                "MODEL_IMAGE_INPUT_REJECTED",
                                f"生成模型拒绝图文请求（{exc.status_code}），"
                                "请确认接口和模型支持多图输入，并检查图片格式和大小",
                            ) from None
                        if "data:image/" in str(exc).lower():
                            raise WorkspaceError(
                                "MODEL_IMAGE_INPUT_REJECTED",
                                f"生成模型请求失败（{exc.status_code}），请检查接口状态",
                            ) from None
                    raise
                self.token.raise_if_cancelled()
                if self.on_retry:
                    self.on_retry(exc, attempt + 1, self.network_retries, delay)
                self._wait(delay)
                continue
            self._record(prompt, response, None)
            return response
        raise AssertionError("unreachable")

    def trace(self, event: str, payload: object, **metadata: object) -> None:
        if not enabled():
            return
        context = {"trace_id": self.trace_id, "purpose": self.purpose, "provider": self.model}
        if self.trace_context is not None:
            context.update(self.trace_context())
        context.update(metadata)
        trace_event(event, context, payload, secrets=self.trace_secrets)

    def _wait(self, delay: float) -> None:
        self.token.event.wait(delay)
        self.token.raise_if_cancelled()

    def _record(self, prompt: ModelRequest, response: str, error: str | None) -> None:
        try:
            self.record(prompt, response, error)
        except OSError as exc:
            raise RecordingError("运行记录保存失败，停止后续模型请求；已有项目文件保留") from exc

    def _request(self, prompt: ModelRequest) -> str:
        messages = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else prompt
        if self.transport is not None:
            self.trace("REQUEST", prompt, transport="injected", model=self.model)
            response = self.transport(prompt)
            self.trace(
                "RESPONSE", response, transport="injected", response_type=type(response).__name__
            )
        else:
            if self.purpose == "review":
                from .service_settings import get_review_config

                config = get_review_config(self.model)
            else:
                config = api_providers.get_config(self.model)
            self.trace_secrets = (config.api_key,)
            options = api_providers.request_options(self.model)
            self.trace(
                "REQUEST",
                prompt,
                endpoint=endpoint_label(config.base_url),
                model=config.model,
                message_roles=[message["role"] for message in messages],
                max_tokens=8192,
                temperature=0.7,
                extra_body=options,
            )

            async def request() -> str:
                async with api_providers.AsyncOpenAI(
                    api_key=config.api_key,
                    base_url=config.base_url,
                    max_retries=0,  # This layer owns observable, cancellable network retries.
                ) as client:
                    result = await client.chat.completions.create(
                        model=config.model,
                        messages=messages,
                        max_tokens=8192,
                        temperature=0.7,
                        extra_body=options,
                    )
                    choice = result.choices[0]
                    if enabled():
                        usage = getattr(result, "usage", None)
                        self.trace(
                            "RESPONSE",
                            choice.message.content,
                            model=getattr(result, "model", config.model),
                            finish_reason=getattr(choice, "finish_reason", None),
                            usage=usage.model_dump() if usage is not None else None,
                            refusal=getattr(choice.message, "refusal", None),
                        )
                    return choice.message.content

            response = asyncio.run(request())
        if not isinstance(response, str):
            raise WorkspaceError("INVALID_OUTPUT", "模型未返回文字")
        return response
