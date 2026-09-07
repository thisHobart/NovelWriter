# ai_helper.py
#
# Backward-compatible LLM facade for NovelWriter, now backed by the shared
# `llm-backends` package (StoryDaemon docs/LLM_BACKENDS_INVENTORY.md section
# 7.4, step 4). The GUI and the agents import everything LLM-shaped from here:
# send_prompt / send_prompt_with_retry, the backend state (set_backend /
# get_backend / get_model), and the dropdown sources (get_supported_models /
# get_available_backends / check_cli_availability).
#
# What changed at adoption:
# - ONE model registry (the package's) instead of the two disagreeing local
#   ones this module and llm_interface/multi_provider_llm.py used to carry.
#   get_supported_models() now returns the package's primary keys; the old
#   NovelWriter names are gone from the list. "claude-4-5-sonnet" still works
#   via the package alias table; "claude-4-5-opus" has no package primary and
#   deliberately raises ValueError (it must not be silently re-pointed at a
#   newer Opus).
# - The default API model is the package's DEFAULT_API_MODEL instead of the
#   hardcoded "gpt-4o".
# - The Anthropic key is read by the package: ANTHROPIC_API_KEY is canonical,
#   with the historical CLAUDE_API_KEY spelling still honored as a deprecated
#   fallback, so existing .env files keep working.
# - The CLI backends strip provider API keys from their subprocess env by
#   default (the billing gotcha; this supersedes the interim _env.py hotfix).

import os  # noqa: F401  (kept for callers that reach through this module)
from typing import Optional

from dotenv import load_dotenv

from llm_backends import DEFAULT_API_MODEL
from llm_backends import multi_provider_llm as _mp
from llm_backends.multi_provider_llm import (  # noqa: F401  back-compat re-exports
    get_supported_models,
    resolve_model,
    send_prompt_claude,
    send_prompt_gemini,
    send_prompt_openai,
)

from .llm_interface import (
    check_cli_availability,  # noqa: F401  re-export (GUI dropdown source)
    get_available_backends,  # noqa: F401  re-export (GUI dropdown source)
    get_current_backend,
    initialize_llm,
    is_initialized,
    send_prompt as llm_send_prompt,
)
from .llm_trace import trace_model_call


load_dotenv()  # Load API keys from .env into the environment (app-owned; the package only reads os.environ)


# --- Backend State ---
# Backend selection state lives in llm_interface; we keep the current API
# model here for the API backend.
_current_model: str = DEFAULT_API_MODEL

# NovelWriter's system prompt for API generations (A5: system prompts are
# app-owned; the package default would otherwise apply).
ROLE_DESCRIPTION = (
    "你是一名乐于助人的小说写作助手。你只创作原创文本。"
)

# max_tokens for API generations (the CLI backends use their own defaults).
DEFAULT_MAX_TOKENS = 16384


def set_backend(backend: str, model: Optional[str] = None) -> None:
    """Set the LLM backend to use.

    Args:
        backend: Backend identifier ("api", "codex", "gemini-cli", "claude-cli")
        model: Model to use. For "api" this is a package registry key
               (defaults to the current model, initially DEFAULT_API_MODEL).
    """
    global _current_model
    if model:
        _current_model = model

    initialize_llm(backend=backend, model=_current_model)


def get_backend() -> str:
    """Get the current backend name."""
    # Use the llm_interface module's state (single source of truth)
    return get_current_backend() or "api"


def get_model() -> str:
    """Get the current model name (for API backend)."""
    return _current_model


#: 一次调用回空之后重试几次。空回复不是内容问题，是这一次调用没成，重问一遍
#: 往往就有了——托管端点在同一个提示词上时而返回正文、时而只返回空。
EMPTY_REPLY_RETRIES = 3


def _with_empty_reply_retry(model: str, call) -> str:
    """调用返回空正文时重问，别把它当成模型的回答。

    实测同一个提示词连发四次，两次拿到正文（642 / 3412 字），两次正文为空，而端点
    每次都报「正常结束」、也都计了几百到几千 token 的生成量。空回复交给上层，会被
    当成「模型答得不合规」，白白吃掉评审仅有的那次补救机会，最后整章停在待复审。
    """
    for attempt in range(EMPTY_REPLY_RETRIES):
        reply = call()
        if reply and str(reply).strip():
            return reply
        print(
            f"Model '{model}' returned an empty reply "
            f"({attempt + 1}/{EMPTY_REPLY_RETRIES}); asking again."
        )
    return ""


def _flatten(messages) -> str:
    """把多轮对话摊成一次性提问，给不支持消息列表的后端用。"""
    blocks = []
    for message in messages:
        role = message.get("role")
        label = "【我上一轮的要求】" if role == "user" else "【你上一轮的回答】"
        blocks.append(label + "\n" + str(message.get("content", "")))
    return "\n\n".join(blocks)


def send_conversation(messages, model=None) -> str:
    """按多轮对话提问：模型看得见自己上一轮写的东西。

    单场景重修用得上：一次性提问里，上一稿是「一段别人给的文字」，模型容易整体
    重写；作为对话，上一稿是它自己的回答，改动更贴着被点名的那几句走。

    后端不支持消息列表时摊平成一次提问，行为退回原样，不影响 CLI 后端。
    """
    messages = [dict(message) for message in messages if message.get("content")]
    if not messages:
        return ""
    if get_backend() != "api" or len(messages) == 1:
        return send_prompt(_flatten(messages), model=model)

    client = getattr(_mp, "_get_hosted_llm_client", None)
    resolved = model or _current_model
    if resolved != "hosted-llm" or client is None:
        # 只有自建的 OpenAI 兼容端点走真正的多轮；其余后端摊平，保持一致行为。
        return send_prompt(_flatten(messages), model=model)

    def invoke() -> str:
        response = client().chat.completions.create(
            model=os.environ.get("HOSTED_LLM_MODEL"),
            messages=[{"role": "system", "content": ROLE_DESCRIPTION}, *messages],
            max_tokens=DEFAULT_MAX_TOKENS,
            temperature=0.7,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        return response.choices[0].message.content or ""

    return _with_empty_reply_retry(
        resolved,
        lambda: trace_model_call(
            prompt=_flatten(messages),
            backend="api",
            model=resolved,
            invoke=invoke,
        ),
    )


def send_prompt(prompt, model=None):
    """Sends a prompt to the specified AI model.

    Routes to the appropriate backend based on current settings: CLI backends
    go through llm_interface; the API backend goes through the llm-backends
    model registry (which also accepts legacy aliases such as
    "claude-4-5-sonnet" and the "openrouter:<upstream-id>" passthrough form).

    Args:
        prompt: The text prompt to send.
        model: Model name (used for API backend, ignored for CLI backends).
               If None, uses the currently selected model.

    Returns:
        Generated text from the LLM.

    Raises:
        ValueError: If the model is not in the package registry (nor an alias).
    """
    global _current_model

    # Get current backend from the unified interface (single source of truth)
    current_backend = get_backend()

    # If using a CLI backend, use the unified interface directly
    if current_backend != "api":
        if not is_initialized():
            initialize_llm(backend=current_backend)
        print(f"Using CLI backend: {current_backend}")
        return trace_model_call(
            prompt=prompt,
            backend=current_backend,
            model=model or _current_model,
            invoke=lambda: llm_send_prompt(prompt),
        )

    # Use current model if none provided
    if model is None:
        model = _current_model

    # Validate/resolve against the package registry (raises ValueError with
    # the supported list for unknown names, e.g. the retired claude-4-5-opus).
    resolve_model(model)

    print(f"Attempting to use model: {model}")
    _current_model = model

    try:
        return _with_empty_reply_retry(
            model,
            lambda: trace_model_call(
                prompt=prompt,
                backend=current_backend,
                model=model,
                invoke=lambda: _mp.send_prompt(
                    prompt, model=model, max_tokens=DEFAULT_MAX_TOKENS
                ),
            ),
        )
    except Exception as e:
        print(f"Error calling model '{model}': {e}")
        raise


def send_prompt_with_retry(prompt, model=None, max_retries=3):
    """Send a prompt with automatic retry on failure.

    Args:
        prompt: The text prompt to send.
        model: Model name (used for API backend). If None, uses current model.
        max_retries: Maximum number of retry attempts.

    Returns:
        Generated text from the LLM.
    """
    if model is None:
        model = get_model()

    last_error: Optional[Exception] = None

    for attempt in range(max_retries):
        try:
            return send_prompt(prompt, model=model)
        except Exception as e:
            last_error = e
            print(f"Attempt {attempt + 1}/{max_retries} failed: {e}")
            if attempt < max_retries - 1:
                continue

    raise RuntimeError(
        f"Model '{model}' failed after {max_retries} attempts. Last error: {last_error}"
    )


# --- Provider-specific functions (backward compatibility) ---
# Thin delegates to the package provider functions. The old local client
# singletons are gone; the package manages clients (and the ANTHROPIC_API_KEY
# canon with the CLAUDE_API_KEY fallback) itself.


def send_prompt_oai(prompt, model=None, max_tokens=DEFAULT_MAX_TOKENS, temperature=0.7,
                    role_description=ROLE_DESCRIPTION):
    """Send prompts to OpenAI chat models (legacy helper)."""
    if model is None:
        model = get_model()
    return send_prompt_openai(
        prompt,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        role_description=role_description,
    )


def send_prompt_gemini_direct(prompt, model_name="gemini-2.5-pro", max_output_tokens=8192,
                              temperature=0.7):
    """Send a prompt to the Gemini API directly (legacy helper)."""
    return send_prompt_gemini(
        prompt,
        model_name=model_name,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
    )


def send_prompt_claude_direct(prompt, model="claude-sonnet-4-5-20250929", max_tokens=8192,
                              temperature=0.7,
                              role_description=(
                                  "你是一名擅长创作原创小说的专业创意写作者。"
                              )):
    """Send a prompt to Anthropic's Claude API directly (legacy helper)."""
    return send_prompt_claude(
        prompt,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        role_description=role_description,
    )
