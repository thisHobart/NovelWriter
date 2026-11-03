"""Build bounded chat history without project documents or model calls."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass

from .contracts import JsonObject
from .models import WorkspaceError, encode, require, text

DEFAULT_SYSTEM_RULES = (
    "你是中文网文视频推广工作台中的对话助手。根据用户消息和历史对话回答。"
    "你无法自动读取项目文档；需要材料时请用户在消息中提供。"
    "只提供文字回复，不执行软件操作、不生成图片或视频。"
)


@dataclass(frozen=True)
class ConversationContext:
    prompt: str
    messages: list[JsonObject]
    omitted_message_ids: tuple[str, ...]

    @property
    def character_count(self) -> int:
        return len(self.prompt)


def build_conversation_context(
    history: Sequence[JsonObject],
    current_message_id: str,
    *,
    system_rules: str = DEFAULT_SYSTEM_RULES,
    max_context_chars: int = 48000,
) -> ConversationContext:
    """Keep rules and current user; drop oldest complete turns to fit."""
    text(system_rules, "系统规则")
    require(
        type(max_context_chars) is int and max_context_chars > 0,
        "INVALID_INPUT",
        "上下文容量必须是正整数字符数",
    )
    messages = []
    seen = {}
    for original in history:
        message = {
            field: deepcopy(original[field])
            for field in ("id", "turn_id", "role", "content", "status")
        }
        if message["role"] == "assistant":
            message["parent_message_id"] = original["parent_message_id"]
        message_id = message["id"]
        if message_id in seen:
            require(seen[message_id] == message, "INVALID_INPUT", "同一消息编号的内容不一致")
            continue
        require(
            message["role"] in {"user", "assistant"}
            and isinstance(message["content"], str)
            and bool(message["content"].strip()),
            "INVALID_INPUT",
            "上下文只接受用户消息和最终回复",
        )
        messages.append(message)
        seen[message_id] = message
    require(
        bool(messages)
        and messages[-1]["id"] == current_message_id
        and messages[-1]["role"] == "user"
        and messages[-1]["status"] in {"pending", "complete"},
        "INVALID_INPUT",
        "历史必须包含本轮用户消息，且位于末尾",
    )
    previous = messages[:-1]
    require(len(previous) % 2 == 0, "INVALID_INPUT", "历史消息必须按完整轮次提供")
    for index in range(0, len(previous), 2):
        user, reply = previous[index : index + 2]
        require(
            user["role"] == "user"
            and reply["role"] == "assistant"
            and user["status"] == reply["status"] == "complete"
            and user["turn_id"] == reply["turn_id"]
            and reply["parent_message_id"] == user["id"],
            "INVALID_INPUT",
            "历史只能包含完整用户消息和对应最终回复",
        )
    omitted = []
    while True:
        prompt = (
            system_rules
            + "\n"
            + encode(
                {
                    "conversation": [
                        {"role": message["role"], "content": message["content"]}
                        for message in messages
                    ],
                }
            )
        )
        if len(prompt) <= max_context_chars:
            return ConversationContext(prompt, messages, tuple(omitted))
        if len(messages) == 1:
            raise WorkspaceError(
                "CONTEXT_TOO_LARGE",
                "系统规则和本轮消息已超过容量；请缩短消息",
                details={"required_chars": len(prompt), "max_context_chars": max_context_chars},
            )
        omitted.extend(message["id"] for message in messages[:2])
        messages = messages[2:]
