"""One small, non-retrying request against an explicit configuration snapshot."""
from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

from . import api_providers as api

PROBE_MAX_TOKENS = 2
PROBE_TIMEOUT = 15.0


@dataclass(frozen=True)
class ProbeResult:
    returned_model: str
    elapsed: float
    total_tokens: int | None


def probe_connection(config: api.APIConfig) -> ProbeResult:
    started = monotonic()
    # No saved/environment configuration lookup and no automatic retries.
    with api.OpenAI(api_key=config.api_key, base_url=config.base_url,
                    timeout=PROBE_TIMEOUT, max_retries=0) as client:
        response = client.chat.completions.create(
            model=config.model,
            messages=[{"role": "user", "content": "Reply OK"}],
            max_tokens=PROBE_MAX_TOKENS,
            temperature=0.7,
            extra_body=api.request_options(config.provider),
        )

    choices = getattr(response, "choices", None)
    content = getattr(getattr(choices[0], "message", None), "content", None) if choices else None
    if not isinstance(content, str) or not content.strip():
        raise ValueError("接口已响应但未返回文本：请检查模型是否支持关闭思考和普通文本调用（测试输出上限为 2 tokens）。")
    usage = getattr(response, "usage", None)
    total = getattr(usage, "total_tokens", None)
    return ProbeResult(getattr(response, "model", "") or config.model,
                       monotonic() - started, total if isinstance(total, int) else None)
