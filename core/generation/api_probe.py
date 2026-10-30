"""One small, non-retrying request against an explicit configuration snapshot."""
from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

from openai import APIConnectionError, APIStatusError, APITimeoutError

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
    try:
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
    except APITimeoutError:
        raise ValueError("请求超时：请检查接口地址、网络及服务运行状态。") from None
    except APIConnectionError:
        raise ValueError("无法连接服务：请检查接口地址、端口、网络、代理或证书。") from None
    except APIStatusError as exc:
        # Do not forward response bodies: providers can echo credentials in errors.
        explanations = {
            400: "请求参数被拒绝，请检查模型名称及是否支持当前调用方式。",
            401: "密钥验证失败，请检查 API 密钥及其所属地域。",
            402: "账户余额不足，请检查服务账户。",
            403: "没有访问权限，请检查密钥权限及模型是否已开通。",
            404: "接口或模型不存在，请检查接口地址和模型名称。",
            422: "请求参数不被支持，请检查模型及接口兼容性。",
            429: "请求受限，请检查额度、余额或频率限制，稍后再试。",
        }
        detail = explanations.get(exc.status_code, "服务处理失败，请稍后重试或检查服务状态。")
        raise ValueError(f"HTTP {exc.status_code}：{detail}") from None
    except Exception:
        raise ValueError("接口响应无法解析，请检查是否为 OpenAI 兼容的文本生成接口。") from None

    choices = getattr(response, "choices", None)
    content = getattr(getattr(choices[0], "message", None), "content", None) if choices else None
    if not isinstance(content, str) or not content.strip():
        raise ValueError("接口已响应但未返回文本：请检查模型是否支持关闭思考和普通文本调用（测试输出上限为 2 tokens）。")
    usage = getattr(response, "usage", None)
    total = getattr(usage, "total_tokens", None)
    return ProbeResult(getattr(response, "model", "") or config.model,
                       monotonic() - started, total if isinstance(total, int) else None)
