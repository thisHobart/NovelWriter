"""Collect evidence of delivered or applied revisions from a completed chat history."""

import json
import re
from typing import NamedTuple

from evidence_pack import REVISION_BLOCK
from history_retrieval import Message

APPLIED_BLOCK = re.compile(r"```applied_diff[ \t]*\r?\n(.*?)```", re.DOTALL)
CARD_CHARS = 1050


class RevisionEvidence(NamedTuple):
    message_id: str
    state: str
    text: str


def revision_evidence(messages: list[Message]) -> RevisionEvidence | None:
    # Applied changes carry stronger state evidence than a draft in the same turn.
    for message in messages:
        if message["role"] != "program":
            continue
        block = APPLIED_BLOCK.search(message["content"])
        if not block:
            continue
        try:
            changes = json.loads(block[1])
            description = "\n".join(
                f"第{row['镜号']}镜 {row['字段']}，改前：{row['旧值']}；改后：{row['新值']}"
                for row in changes
            )
        except (ValueError, TypeError, KeyError):
            continue
        if description:
            return RevisionEvidence(message["id"], "有应用回执", description[:CARD_CHARS])
    for message in messages:
        if message["role"] != "assistant":
            continue
        content = message["content"]
        blocks = list(REVISION_BLOCK.finditer(content))
        revisions = [match for match in blocks if match[1] == "revised_shots"]
        if len(revisions) != 1:
            continue
        try:
            payload = json.loads(revisions[0][2])
        except ValueError:
            continue
        if not isinstance(payload, dict) or not (payload.get("镜头") or payload.get("shots")):
            continue
        description = content[:blocks[0].start()].strip()
        if not description:
            description = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return RevisionEvidence(message["id"], "AI已返回改稿，应用未确认", description[:CARD_CHARS])
    return None
