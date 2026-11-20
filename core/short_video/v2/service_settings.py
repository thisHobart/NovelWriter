"""Review-model and image-API settings of the short video feature.

They sit in their own sections (``review`` and ``image``) of the shared settings file, next to
the model-service addresses and keys both features use; only this module reads or checks them.
"""
from __future__ import annotations

from core.common.api_providers import APIConfig
from core.common.api_settings import (
    DEFAULT_MODELS,
    DEFAULT_URLS,
    VARIABLES,
    read_section,
    saved_values,
    validate_provider,
    write_section,
)

FIELDS = ("base_url", "api_key", "model")


def _complete(values: object) -> bool:
    return isinstance(values, dict) and all(isinstance(values.get(name), str) for name in FIELDS)


def _review() -> dict:
    review = read_section("review")
    if review is None:
        return {}
    if (not isinstance(review, dict) or review.get("provider") not in VARIABLES
            or not isinstance(review.get("providers"), dict)):
        raise ValueError("校验模型配置格式不正确")
    if not all(_complete(values) for values in review["providers"].values()):
        raise ValueError("校验模型配置字段格式不正确")
    return review


def _image() -> dict | None:
    image = read_section("image")
    if image is not None and not _complete(image):
        raise ValueError("生图 API 配置字段格式不正确")
    return image


def review_provider() -> str:
    return _review().get("provider", "hosted-llm")


def get_review_config(provider: str | None = None) -> APIConfig:
    """Review uses an explicit independent profile, never generation or .env fallback."""
    review = _review()
    selected = review.get("provider")
    values = review.get("providers", {}).get(selected)
    if not values:
        raise ValueError("请先在“服务配置 → 校验模型配置”中配置校验模型")
    if provider is not None and selected != provider:
        raise ValueError("校验模型服务已变化，请重新准备任务")
    return validate_provider(selected, **values)


def get_image_config() -> APIConfig:
    values = _image()
    if not values:
        raise ValueError("请先在“服务配置 → 生图 API 配置”中配置图片接口")
    return validate_provider("hosted-llm", **values)


def review_form_values(provider: str) -> dict[str, str]:
    values = _review().get("providers", {}).get(provider)
    return dict(values) if values else {
        "base_url": DEFAULT_URLS[provider], "api_key": "", "model": DEFAULT_MODELS[provider],
    }


def image_form_values(provider: str = "hosted-llm") -> dict[str, str]:
    return dict(_image() or {"base_url": "", "api_key": "", "model": ""})


def save_review(provider: str, base_url: str, api_key: str, model: str) -> None:
    values = saved_values(provider, base_url, api_key, model)
    review = _review() or {"provider": provider, "providers": {}}
    review["provider"] = provider
    review["providers"][provider] = values
    write_section("review", review)


def save_image(provider: str, base_url: str, api_key: str, model: str) -> None:
    write_section("image", saved_values(provider, base_url, api_key, model))
