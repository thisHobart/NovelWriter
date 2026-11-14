"""The three API services offered by NovelWriter's desktop application."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from openai import OpenAI, AsyncOpenAI, APIConnectionError, APIStatusError

DEFAULT_API_MODEL = "hosted-llm"
PROVIDER_LABELS = {
    "hosted-llm": "第三方 API",
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
    """Only send provider-specific options to the matching official API."""
    if provider == "qwen":
        return {"enable_thinking": False}
    elif provider == "deepseek":
        return {"thinking": {"type": "disabled"}}
    return {}


def send_messages(provider: str, messages: list[dict], max_tokens: int = 16384) -> str:
    config = get_config(provider)
    # Explicit credentials and endpoint prevent OpenAI environment settings
    # from redirecting requests for a different service.
    # 重试由显式调用方管理，不叠加 SDK 自动重试。
    with OpenAI(api_key=config.api_key, base_url=config.base_url, max_retries=0) as client:
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
        if max_retries <= 0:
            raise ValueError("max_retries 必须大于 0")
        for attempt in range(max_retries):
            try:
                return self.generate(prompt, max_tokens)
            except (APIConnectionError, APIStatusError) as exc:
                if isinstance(exc, APIStatusError) and exc.status_code not in {408, 409, 429} and exc.status_code < 500:
                    raise
                if attempt == max_retries - 1:
                    raise
