"""Conversation files and final-message history, independent of model execution."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Literal

from core.common.locks import project_lock

from .contracts import JsonObject, ModelSnapshot
from .models import (
    ID_PATTERN,
    WorkspaceError,
    encode,
    fingerprint,
    identity,
    reject_json_constant,
    require,
    text,
    timestamp,
    validate_model_snapshot_format,
)
from .page_material import validate_attached
from .repository import ProjectRepository
from .storage import atomic_write_text
from .storyboard_chat import validate_storyboard_reference
from .storyboard_edits import validate_storyboard_edit


def select_completed_turns(messages: Sequence[JsonObject]) -> list[JsonObject]:
    """Select paired final turns without including stopped or unanswered messages."""
    replies = {
        message["parent_message_id"]: message
        for message in messages
        if message["role"] == "assistant"
        and message["status"] == "complete"
        and "parent_message_id" in message
    }
    history = []
    for message in messages:
        if message["role"] == "user" and message["status"] == "complete":
            reply = replies.get(message["id"])
            if reply and reply["turn_id"] == message["turn_id"]:
                history.extend((message, reply))
    return deepcopy(history)


class ConversationStore:
    """Use the existing conversations/*.json format, lock and atomic writer."""

    def __init__(self, repository: ProjectRepository) -> None:
        self.repository = repository

    def _path(self, conversation_id: str) -> Path:
        require(
            isinstance(conversation_id, str) and ID_PATTERN.fullmatch(conversation_id),
            "INVALID_INPUT",
            "会话编号无效",
        )
        return self.repository.root / "conversations" / f"{conversation_id}.json"

    def create(self, title: str = "新对话") -> JsonObject:
        text(title, "会话标题")
        with self.repository.transaction() as state:
            if state["project_id"] is None:
                state["project_id"] = identity("project")
            project_id = state["project_id"]
        created_at = timestamp()
        conversation = {
            "id": identity("conversation"),
            "project_id": project_id,
            "title": title,
            "revision": 0,
            "messages": [],
            "created_at": created_at,
            "updated_at": created_at,
        }
        atomic_write_text(self._path(conversation["id"]), encode(conversation))
        return conversation

    def get(self, conversation_id: str) -> JsonObject:
        path = self._path(conversation_id)
        try:
            conversation = json.loads(
                path.read_text(encoding="utf-8"), parse_constant=reject_json_constant
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise WorkspaceError("INVALID_CONVERSATION", "会话文件无法读取，原文件未修改") from exc
        require(
            isinstance(conversation, dict)
            and conversation.get("id") == conversation_id
            and conversation.get("project_id") == self.repository.load()["project_id"]
            and conversation["project_id"] is not None
            and type(conversation.get("revision")) is int
            and conversation["revision"] >= 0
            and isinstance(conversation.get("messages"), list)
            and isinstance(conversation.get("created_at"), str)
            and isinstance(conversation.get("updated_at"), str),
            "INVALID_CONVERSATION",
            "会话不属于当前项目或数据损坏",
        )
        message_ids = set()
        for message in conversation["messages"]:
            require(
                isinstance(message, dict)
                and isinstance(message.get("id"), str)
                and ID_PATTERN.fullmatch(message["id"])
                and message["id"] not in message_ids
                and isinstance(message.get("turn_id"), str)
                and ID_PATTERN.fullmatch(message["turn_id"])
                and isinstance(message.get("role"), str)
                and message.get("role") in {"user", "assistant"}
                and isinstance(message.get("status"), str)
                and message.get("status")
                in {"pending", "complete", "cancelled", "failed", "interrupted"}
                and isinstance(message.get("content"), str),
                "INVALID_CONVERSATION",
                "消息记录损坏",
            )
            validate_model_snapshot_format(message.get("model_snapshot"))
            if "storyboard_reference" in message:
                validate_storyboard_reference(message["storyboard_reference"])
            if "storyboard_edit" in message:
                validate_storyboard_edit(message["storyboard_edit"])
            if "attached" in message:
                validate_attached(message["attached"])
            message_ids.add(message["id"])
        return conversation

    def list(self) -> list[JsonObject]:
        directory = self.repository.root / "conversations"
        return sorted(
            [self.get(path.stem) for path in directory.glob("conversation_*.json")],
            key=lambda conversation: conversation["updated_at"],
            reverse=True,
        )

    def _update(
        self,
        conversation_id: str,
        operation: Callable[[JsonObject], JsonObject],
        expected_revision: int | None,
    ) -> JsonObject:
        path = self._path(conversation_id)
        with project_lock(str(path.with_suffix(".lock")), timeout=5):
            conversation = self.get(conversation_id)
            if expected_revision is not None:
                require(
                    type(expected_revision) is int
                    and conversation["revision"] == expected_revision,
                    "REVISION_CONFLICT",
                    "会话已更新，请刷新后重试",
                )
            previous = fingerprint(conversation)
            message = operation(conversation)
            if fingerprint(conversation) != previous:
                conversation["revision"] += 1
                conversation["updated_at"] = timestamp()
                atomic_write_text(path, encode(conversation))
        return deepcopy(message)

    def save_user_message(
        self,
        conversation_id: str,
        user_text: str,
        *,
        turn_id: str,
        model_snapshot: ModelSnapshot,
        expected_revision: int | None = None,
        storyboard_reference: JsonObject | None = None,
        storyboard_edit: JsonObject | None = None,
        attached: JsonObject | None = None,
    ) -> JsonObject:
        """attached is what a page other than the storyboard attached: {"page", "label"}."""
        text(user_text, "消息")
        require(
            isinstance(turn_id, str) and ID_PATTERN.fullmatch(turn_id),
            "INVALID_INPUT",
            "轮次编号无效",
        )
        model_snapshot = deepcopy(validate_model_snapshot_format(model_snapshot))
        reference = (
            validate_storyboard_reference(storyboard_reference)
            if storyboard_reference is not None
            else None
        )
        edit = validate_storyboard_edit(storyboard_edit) if storyboard_edit is not None else None
        note = validate_attached(attached) if attached is not None else None

        def append_user(conversation: JsonObject) -> JsonObject:
            require(
                not any(message["turn_id"] == turn_id for message in conversation["messages"]),
                "TURN_ALREADY_EXISTS",
                "本轮用户消息已经保存",
            )
            require(
                not any(message["status"] == "pending" for message in conversation["messages"]),
                "PROJECT_BUSY",
                "会话有未完成消息",
            )
            message = {
                "id": identity("message"),
                "turn_id": turn_id,
                "role": "user",
                "content": user_text,
                "status": "pending",
                "model_snapshot": model_snapshot,
                "created_at": timestamp(),
            }
            if reference is not None:
                message["storyboard_reference"] = deepcopy(reference)
            if edit is not None:
                message["storyboard_edit"] = deepcopy(edit)
            if note is not None:
                message["attached"] = deepcopy(note)
            conversation["messages"].append(message)
            return message

        return self._update(conversation_id, append_user, expected_revision)

    def save_final_reply(
        self,
        conversation_id: str,
        user_message_id: str,
        final_reply: str,
        *,
        status: Literal["complete", "cancelled"] = "complete",
        expected_revision: int | None = None,
    ) -> JsonObject:
        """The caller supplies only the final answer, never candidates or check feedback."""
        text(final_reply, "最终回复")
        require(status in {"complete", "cancelled"}, "INVALID_INPUT", "无效回复状态")

        def append_reply(conversation: JsonObject) -> JsonObject:
            user = next(
                (
                    message
                    for message in conversation["messages"]
                    if message["id"] == user_message_id
                ),
                None,
            )
            require(
                user is not None and user["role"] == "user" and user["status"] == "pending",
                "INVALID_INPUT",
                "最终回复必须对应本会话尚未完成的用户消息",
            )
            require(
                not any(
                    message.get("parent_message_id") == user_message_id
                    for message in conversation["messages"]
                ),
                "TURN_ALREADY_EXISTS",
                "本轮已有回复记录",
            )
            reply = {
                "id": identity("message"),
                "turn_id": user["turn_id"],
                "parent_message_id": user["id"],
                "role": "assistant",
                "content": final_reply,
                "status": status,
                "model_snapshot": deepcopy(user["model_snapshot"]),
                "created_at": timestamp(),
            }
            user["status"] = status
            conversation["messages"].append(reply)
            return reply

        return self._update(conversation_id, append_reply, expected_revision)

    def finish_without_reply(
        self,
        conversation_id: str,
        user_message_id: str,
        status: Literal["failed", "cancelled", "interrupted"],
        *,
        expected_revision: int | None = None,
    ) -> JsonObject:
        """Close a stopped turn without storing a candidate or fabricating an answer."""
        require(status in {"failed", "cancelled", "interrupted"}, "INVALID_INPUT", "无效结束状态")

        def finish_user(conversation: JsonObject) -> JsonObject:
            user = next(
                (
                    message
                    for message in conversation["messages"]
                    if message["id"] == user_message_id
                ),
                None,
            )
            require(
                user is not None and user["role"] == "user" and user["status"] == "pending",
                "INVALID_INPUT",
                "只能结束尚未完成的用户消息",
            )
            user["status"] = status
            return user

        return self._update(conversation_id, finish_user, expected_revision)

    def get_history(self, conversation_id: str, *, current_message_id: str) -> list[JsonObject]:
        """Return earlier completed turns and the current user once, in stored order."""
        messages = self.get(conversation_id)["messages"]
        current_index = next(
            (
                index
                for index, message in enumerate(messages)
                if message["id"] == current_message_id
            ),
            None,
        )
        require(current_index is not None, "INVALID_INPUT", "本轮用户消息不属于此会话")
        current = messages[current_index]
        require(
            current["role"] == "user" and current["status"] in {"pending", "complete"},
            "INVALID_INPUT",
            "请选择有效的本轮用户消息",
        )
        return select_completed_turns(messages[:current_index]) + [deepcopy(current)]

    def recover(self, active_turn_ids: set[str]) -> None:
        """Close interrupted pending turns locally; never resend a model request."""
        for conversation in self.list():
            if not any(
                message["status"] == "pending" and message["turn_id"] not in active_turn_ids
                for message in conversation["messages"]
            ):
                continue

            def mark_interrupted(value: JsonObject) -> JsonObject:
                for message in value["messages"]:
                    if message["status"] == "pending" and message["turn_id"] not in active_turn_ids:
                        message["status"] = "interrupted"
                return value

            self._update(conversation["id"], mark_interrupted, None)
