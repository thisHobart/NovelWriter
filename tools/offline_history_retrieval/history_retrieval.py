"""Retrieve a complete conversation turn from in-memory messages."""

from collections.abc import Sequence
from typing import TypedDict


class Message(TypedDict):
    id: str
    turn_id: str
    role: str
    content: str


def find_turn_by_message_id(messages: Sequence[Message], message_id: str) -> list[Message]:
    """Return the messages in the referenced turn, in their original order."""
    for message in messages:
        if message["id"] == message_id:
            turn_id = message["turn_id"]
            return [entry for entry in messages if entry["turn_id"] == turn_id]
    return []
