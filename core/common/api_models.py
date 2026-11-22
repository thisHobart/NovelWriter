"""Discover model IDs from the explicit OpenAI-compatible endpoint in the form."""

from __future__ import annotations

from . import api_providers as api

MODEL_LIST_TIMEOUT = 15.0


def fetch_models(config: api.APIConfig) -> list[str]:
    """One GET request; no generation, configuration writes or implicit retries."""
    with api.OpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=MODEL_LIST_TIMEOUT,
        max_retries=0,
    ) as client:
        page = client.models.list()
    data = getattr(page, "data", None)
    if not isinstance(data, list):
        raise ValueError("模型列表格式无效：需要 data 列表")
    model_ids = []
    for model in data:
        model_id = getattr(model, "id", None)
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("模型列表格式无效：模型缺少 id")
        model_ids.append(model_id)
    return sorted(set(model_ids), key=str.casefold)
