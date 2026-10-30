"""The three API services offered by NovelWriter's desktop application."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from openai import OpenAI, AsyncOpenAI

DEFAULT_API_MODEL = "hosted-llm"
PROVIDER_LABELS = {
    "hosted-llm": "hosted-llm",
    "deepseek": "DeepSeek 官方",
    "qwen": "千问官方",
}


def environment_file() -> Path:
    """Use the executable's directory even when launched via a shortcut."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / ".env"
    return Path(__file__).resolve().parents[2] / ".env"


@dataclass(frozen=True)
class APIConfig:
    provider: str
    base_url: str
    model: str
    api_key: str = field(repr=False)


def _validate_url(value: str, variable: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                 and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment)
        parsed.port  # Validate malformed port numbers as well.
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f"{variable} 必须是完整的 HTTP/HTTPS API 地址")
    return value.rstrip("/")


def get_config(provider: str, overrides: dict[str, str] | None = None) -> APIConfig:
    if provider not in PROVIDER_LABELS:
        raise ValueError(f"不支持的模型服务：{provider}")
    from .api_settings import provider_overrides
    environment = dict(os.environ)
    environment.update(provider_overrides(provider))
    environment.update(overrides or {})

    def _env(name: str, default: str = "") -> str:
        return environment.get(name, "").strip() or default

    if provider == "hosted-llm":
        url = _env("HOSTED_LLM_BASE_URL")
        if not url:
            host = _env("HOSTED_LLM_URL")
            if not host:
                raise ValueError("缺少 HOSTED_LLM_BASE_URL（或 HOSTED_LLM_URL）")
            url = host if "://" in host else f"http://{host}"
            parsed = urlsplit(_validate_url(url, "HOSTED_LLM_URL"))
            port = _env("HOSTED_LLM_PORT")
            authority = parsed.netloc
            if port and parsed.port is None:
                authority += f":{port}"
            url = f"{parsed.scheme}://{authority}{parsed.path.rstrip('/') or '/v1'}"
        url = _validate_url(url, "HOSTED_LLM_BASE_URL")
        model = _env("HOSTED_LLM_MODEL")
        if not model:
            raise ValueError("缺少 HOSTED_LLM_MODEL")
        # Local OpenAI-compatible servers often run without authentication.
        key = _env("HOSTED_LLM_API_KEY", "not-needed")
    elif provider == "deepseek":
        url = "https://api.deepseek.com/v1"
        model = _env("DEEPSEEK_MODEL", "deepseek-flash")
        key = _env("DEEPSEEK_API_KEY")
        if not key:
            raise ValueError("缺少 DEEPSEEK_API_KEY")
    else:
        url = _validate_url(
            _env("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            "DASHSCOPE_BASE_URL",
        )
        model = _env("QWEN_MODEL", "qwen-plus")
        key = _env("DASHSCOPE_API_KEY")
        if not key:
            raise ValueError("缺少 DASHSCOPE_API_KEY")
    return APIConfig(provider, url, model, key)


def configuration_status(provider: str) -> tuple[bool, str]:
    """Local validation only; this does not claim the remote key was verified."""
    try:
        config = get_config(provider)
    except ValueError as exc:
        return False, str(exc)
    return True, f"配置已就绪 · {config.model}"


def request_options(provider: str) -> dict:
    """Use the same non-thinking mode for generation and connection probes."""
    if provider == "hosted-llm":
        return {"chat_template_kwargs": {"enable_thinking": False}}
    elif provider == "qwen":
        return {"enable_thinking": False}
    elif provider == "deepseek":
        return {"thinking": {"type": "disabled"}}
    return {}


def send_messages(provider: str, messages: list[dict], max_tokens: int = 16384) -> str:
    config = get_config(provider)
    # Explicit credentials and endpoint prevent OpenAI environment settings
    # from redirecting requests for a different service.
    from core.generation.lore_run import ACTIVE_BUDGET
    budget = ACTIVE_BUDGET.get()
    options = {}
    if budget is not None:
        import asyncio
        limit = max(0.001, min(120.0, budget.remaining))
        options.update(timeout=limit, max_retries=0)
        async def bounded_request():
            async with AsyncOpenAI(api_key=config.api_key, base_url=config.base_url, **options) as client:
                # SDK 的 read timeout 按网络分段计时；另外限制整次请求的墙钟时间，
                # 避免持续慢速返回绕过 120 秒 / 剩余总时间。
                return await asyncio.wait_for(client.chat.completions.create(
                    model=config.model, messages=messages, max_tokens=max_tokens,
                    temperature=0.7, extra_body=request_options(provider),
                ), timeout=limit)
        response = asyncio.run(bounded_request())
        return response.choices[0].message.content or ""
    with OpenAI(api_key=config.api_key, base_url=config.base_url, **options) as client:
        response = client.chat.completions.create(
            model=config.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.7,
            extra_body=request_options(provider),
        )
    return response.choices[0].message.content or ""


class ConfiguredAPIInterface:
    """Adapter for the existing headless LLM interface."""

    def __init__(self, model: str):
        self.model = model

    def generate(self, prompt: str, max_tokens: int = 16384) -> str:
        return send_messages(self.model, [{"role": "user", "content": prompt}], max_tokens)

    def generate_with_retry(self, prompt: str, max_tokens: int = 16384,
                            max_retries: int = 3) -> str:
        for attempt in range(max_retries):
            try:
                return self.generate(prompt, max_tokens)
            except Exception:
                if attempt == max_retries - 1:
                    raise
        raise ValueError("max_retries 必须大于 0")
