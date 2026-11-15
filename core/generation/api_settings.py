"""Persistent desktop settings; environment variables remain a fallback."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .api_providers import APIConfig

VARIABLES = {
    "hosted-llm": ("HOSTED_LLM_BASE_URL", "HOSTED_LLM_API_KEY", "HOSTED_LLM_MODEL"),
    "deepseek": (None, "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL"),
    "qwen": ("DASHSCOPE_BASE_URL", "DASHSCOPE_API_KEY", "QWEN_MODEL"),
}
DEFAULT_URLS = {
    "hosted-llm": "http://127.0.0.1:8000/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}
DEFAULT_MODELS = {"hosted-llm": "", "deepseek": "deepseek-flash", "qwen": "qwen-plus"}


def settings_file() -> Path:
    from .api_providers import environment_file
    return environment_file().with_name("llm_settings.json")


def _read() -> dict:
    try:
        content = settings_file().read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": 1, "providers": {}}
    document = json.loads(content)
    if (not isinstance(document, dict) or document.get("version") != 1
            or not isinstance(document.get("providers"), dict)):
        raise ValueError("模型配置文件 llm_settings.json 格式不正确")
    for provider, values in document["providers"].items():
        if not isinstance(values, dict) or any(
            not isinstance(values.get(name), str) for name in ("base_url", "api_key", "model")
        ):
            raise ValueError("模型配置文件 llm_settings.json 字段格式不正确")
    review = document.get("review")
    image = document.get("image")
    if image is not None and (not isinstance(image, dict) or any(
            not isinstance(image.get(field), str) for field in ("base_url", "api_key", "model"))):
        raise ValueError("生图 API 配置字段格式不正确")
    if review is not None:
        if (not isinstance(review, dict) or review.get("provider") not in VARIABLES
                or not isinstance(review.get("providers"), dict)):
            raise ValueError("校验模型配置格式不正确")
        for values in review["providers"].values():
            if not isinstance(values, dict) or any(
                    not isinstance(values.get(name), str) for name in ("base_url", "api_key", "model")):
                raise ValueError("校验模型配置字段格式不正确")
    return document


def provider_overrides(provider: str) -> dict[str, str]:
    values = _read()["providers"].get(provider, {})
    return {variable: values[field] for variable, field in
            zip(VARIABLES[provider], ("base_url", "api_key", "model"))
            if variable and field in values}


def review_provider() -> str:
    return _read().get("review", {}).get("provider", "hosted-llm")


def get_review_config(provider: str | None = None) -> APIConfig:
    """Review uses an explicit independent profile, never generation or .env fallback."""
    review = _read().get("review", {})
    selected = review.get("provider")
    values = review.get("providers", {}).get(selected)
    if not values:
        raise ValueError("请先在“服务配置 → 校验模型配置”中配置校验模型")
    if provider is not None and selected != provider:
        raise ValueError("校验模型服务已变化，请重新准备任务")
    return validate_provider(selected, **values)


def get_image_config() -> APIConfig:
    values = _read().get("image")
    if not values:
        raise ValueError("请先在“服务配置 → 生图 API 配置”中配置图片接口")
    return validate_provider("hosted-llm", **values)


def form_values(provider: str, *, purpose: str = "generation") -> dict[str, str]:
    from .api_providers import get_config
    if purpose == "image":
        return dict(_read().get("image", {"base_url": "", "api_key": "", "model": ""}))
    if purpose == "review":
        values = _read().get("review", {}).get("providers", {}).get(provider)
        return dict(values) if values else {
            "base_url": DEFAULT_URLS[provider], "api_key": "", "model": DEFAULT_MODELS[provider],
        }
    if purpose != "generation":
        raise ValueError("不支持的模型用途")
    variables = VARIABLES[provider]
    effective = dict(os.environ)
    effective.update(provider_overrides(provider))
    base_url = effective.get(variables[0], "").strip() if variables[0] else ""
    if provider == "hosted-llm" and not base_url and effective.get("HOSTED_LLM_URL"):
        try:
            base_url = get_config(provider, {"HOSTED_LLM_MODEL": "preview"}).base_url
        except ValueError:
            base_url = effective.get("HOSTED_LLM_URL", "")
    return {
        "base_url": base_url or DEFAULT_URLS[provider],
        "api_key": effective.get(variables[1], "").strip(),
        "model": effective.get(variables[2], "").strip() or DEFAULT_MODELS[provider],
    }


def validate_provider(provider: str, base_url: str, api_key: str, model: str,
                      *, require_model: bool = True) -> APIConfig:
    """Validate the form itself, without reading or changing saved settings."""
    from .api_providers import APIConfig, _validate_url
    if provider not in VARIABLES:
        raise ValueError("请选择支持的服务商")
    values = {"base_url": base_url.strip(), "api_key": api_key.strip(), "model": model.strip()}
    if any(any(character in value for character in "\r\n\x00") for value in values.values()):
        raise ValueError("配置内容不能包含换行或空字符")
    values["base_url"] = _validate_url(values["base_url"], "接口地址")
    if provider == "deepseek" and values["base_url"] != DEFAULT_URLS[provider]:
        raise ValueError("DeepSeek 官方使用固定接口地址")
    if require_model and not values["model"]:
        raise ValueError("请填写模型名称")
    if provider != "hosted-llm" and not values["api_key"]:
        raise ValueError("请填写 API 密钥")
    return APIConfig(provider, values["base_url"], values["model"], values["api_key"] or "not-needed")


def save_provider(provider: str, base_url: str, api_key: str, model: str,
                  *, purpose: str = "generation") -> None:
    config = validate_provider(provider, base_url, api_key, model)
    values = {"base_url": config.base_url, "api_key": api_key.strip(), "model": config.model}
    document = _read()
    if purpose == "image":
        document["image"] = values
    elif purpose == "review":
        review = document.setdefault("review", {"provider": provider, "providers": {}})
        review["provider"] = provider
        review["providers"][provider] = values
    elif purpose == "generation":
        document["providers"][provider] = values
    else:
        raise ValueError("不支持的模型用途")
    path = settings_file()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".llm_settings-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            failure = sys.exception()
            try:
                temporary.unlink(missing_ok=True)
            except OSError as cleanup_error:
                if failure is None:
                    raise
                failure.add_note(f"清理临时配置文件失败：{type(cleanup_error).__name__}: {cleanup_error}")
