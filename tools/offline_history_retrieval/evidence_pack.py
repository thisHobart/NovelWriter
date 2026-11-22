"""Fit historical turns by preserving structured revisions and the original message IDs."""

import json
import re
from typing import NamedTuple

from history_retrieval import Message

REVISION_BLOCK = re.compile(
    r"```[ \t]*(affected_shots|revised_shots)[ \t]*\r?\n(.*?)```", re.DOTALL
)


class PackedTurn(NamedTuple):
    turn_id: str
    form: str
    text: str


def message_block(message: Message, content: str) -> str:
    return f"[{message['turn_id']} / {message['id']} / {message['role']}] {content}"


def compact_revision(content: str) -> str:
    matches = list(REVISION_BLOCK.finditer(content))
    revisions = [match for match in matches if match[1] == "revised_shots"]
    if len(revisions) != 1:
        return content
    try:
        revised = json.loads(revisions[0][2])
    except ValueError:
        return content
    encoded = json.dumps(revised, ensure_ascii=False, separators=(",", ":"))
    return REVISION_BLOCK.sub(
        lambda match: f"```revised_shots\n{encoded}\n```" if match[1] == "revised_shots" else "",
        content,
    ).strip()


def build_evidence(messages: list[Message]) -> dict[str, str]:
    full = "\n".join(message_block(message, message["content"]) for message in messages)
    compact = "\n".join(
        message_block(
            message,
            compact_revision(message["content"]) if message["role"] == "assistant" else message["content"],
        )
        for message in messages
    )
    versions = {"full": full}
    if len(compact) < len(full):
        versions["compact"] = compact
    return versions


def pack_evidence(
    turn_ids: list[str], evidence: dict[str, dict[str, str]], budget: int
) -> list[PackedTurn]:
    packed = []
    used = 0
    for turn_id in turn_ids:
        separator = 2 if packed else 0
        for form, text in evidence[turn_id].items():
            size = separator + len(text)
            if used + size <= budget:
                packed.append(PackedTurn(turn_id, form, text))
                used += size
                break
    return packed
