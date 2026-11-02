"""One explicitly bounded SDK request at a time, sharing provider configuration."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import time

from core.generation import api_providers
from core.generation.cancellation import CancelToken


class BudgetExhausted(RuntimeError):
    pass


class RecordingError(RuntimeError):
    """A failed durable write must never trigger another paid request."""


REQUEST_SECONDS = 120.0


@dataclass
class RequestBudget:
    max_calls: int
    seconds: float = 300
    clock: object = time.monotonic
    calls: int = 0
    started: float = field(init=False)

    def __post_init__(self):
        self.started = self.clock()

    @property
    def remaining(self):
        return self.seconds - (self.clock() - self.started)

    def can_spend(self, count=1, *, reserve_seconds=0):
        return (self.max_calls - self.calls >= count and self.remaining > 0
                and self.remaining >= reserve_seconds)

    def spend(self):
        if not self.can_spend():
            raise BudgetExhausted("已达到短视频任务的请求次数或时间预算")
        self.calls += 1


class ScriptLLM:
    def __init__(self, model, budget, *, token=None, record=None, transport=None):
        self.model, self.budget = model, budget
        self.token = token or CancelToken()
        self.record = record or (lambda *args: None)
        self.transport = transport

    def __call__(self, prompt):
        self.token.raise_if_cancelled()
        self.budget.spend()
        timeout = max(0.001, min(REQUEST_SECONDS, self.budget.remaining))
        response = ""
        error = None
        try:
            if self.transport is not None:
                # Injected transport is for offline tests; production uses the real bounded SDK.
                response = self.transport(prompt)
            else:
                config = api_providers.get_config(self.model)
                async def request():
                    async with api_providers.AsyncOpenAI(api_key=config.api_key, base_url=config.base_url,
                                                        max_retries=0, timeout=timeout) as client:
                        result = await asyncio.wait_for(client.chat.completions.create(
                            model=config.model, messages=[{"role": "user", "content": prompt}],
                            max_tokens=8192, temperature=0.7,
                            extra_body=api_providers.request_options(self.model)), timeout=timeout)
                        return result.choices[0].message.content or ""
                response = asyncio.run(request())
            if not isinstance(response, str):
                response = ""  # A malformed SDK value must not corrupt the durable text record.
                raise ValueError("模型未返回文字")
            return response
        except Exception as exc:
            error = type(exc).__name__  # SDK error strings can contain credentials; never persist them.
            raise
        finally:
            try:
                self.record(prompt, response, error)
            except OSError:
                raise RecordingError("运行记录保存失败，停止后续模型请求；旧项目文件保留") from None
        # Cancellation is checked by the pipeline only after the returned content is durable.
