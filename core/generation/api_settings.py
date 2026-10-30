"""Persistent desktop settings; environment variables remain a fallback."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

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
        document = json.loads(settings_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "providers": {}}
    except (OSError, ValueError):
        raise ValueError("无法读取模型配置文件 llm_settings.json，请检查文件权限或格式") from None
    if (not isinstance(document, dict) or document.get("version") != 1
            or not isinstance(document.get("providers"), dict)):
        raise ValueError("模型配置文件 llm_settings.json 格式不正确")
    for provider, values in document["providers"].items():
        if not isinstance(values, dict) or any(
            not isinstance(values.get(name), str) for name in ("base_url", "api_key", "model")
        ):
            raise ValueError("模型配置文件 llm_settings.json 字段格式不正确")
    return document


def provider_overrides(provider: str) -> dict[str, str]:
    values = _read()["providers"].get(provider, {})
    return {variable: values[field] for variable, field in
            zip(VARIABLES[provider], ("base_url", "api_key", "model"))
            if variable and field in values}


def form_values(provider: str) -> dict[str, str]:
    from .api_providers import get_config
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


def validate_provider(provider: str, base_url: str, api_key: str, model: str):
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
    if not values["model"]:
        raise ValueError("请填写模型名称")
    if provider != "hosted-llm" and not values["api_key"]:
        raise ValueError("请填写 API 密钥")
    return APIConfig(provider, values["base_url"], values["model"], values["api_key"] or "not-needed")


def save_provider(provider: str, base_url: str, api_key: str, model: str) -> None:
    config = validate_provider(provider, base_url, api_key, model)
    values = {"base_url": config.base_url, "api_key": api_key.strip(), "model": config.model}
    document = _read()
    document["providers"][provider] = values
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
    except OSError:
        raise ValueError("无法保存模型配置，请检查程序所在文件夹的写入权限") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
