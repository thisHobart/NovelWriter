"""Retrieve historical storyboard revisions with local fields and exact phrases."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
import json
import re
from typing import NamedTuple
from unicodedata import normalize

from evidence_pack import REVISION_BLOCK
from experiment_ranker import rank_turns
from history_retrieval import Message, find_turn_by_message_id
from revision_evidence import APPLIED_BLOCK, CARD_CHARS

FIELD_ALIASES = {
    "动作": ("动作", "action"),
    "取景范围": ("取景范围", "景别", "shot_size"),
    "镜头运动": ("镜头运动", "运镜", "camera_movement"),
    "音效": ("音效", "声音", "sound"),
    "场景": ("场景", "scene_ref"),
    "人物": ("人物", "角色", "characters"),
    "道具": ("道具", "props"),
    "对白": ("对白", "台词", "dialogue"),
    "旁白": ("旁白", "narration"),
    "时长": ("时长", "duration_ms"),
    "画面提示词": ("画面提示词", "visual_prompt_draft"),
}
FIELD_NAMES = {alias: main for main, aliases in FIELD_ALIASES.items() for alias in aliases}
FIELD_MENTION = re.compile("|".join(
    re.escape(alias) for alias in sorted(FIELD_NAMES, key=len, reverse=True) if not alias.isascii()
))
CHINESE_DIGITS = dict(zip("零一二三四五六七八九", range(10), strict=True))
CHINESE_DIGITS["两"] = 2
NUMBER = r"(?:[0-9]{1,3}|[一二两三四五六七八九十]{1,3})"
SHOT_MENTION = re.compile(
    rf"第\s*(?P<start>{NUMBER})(?:\s*(?:到|至|[-–—~])\s*第?\s*(?P<end>{NUMBER}))?\s*镜"
)
MESSAGE_ID = re.compile(r"(?<![a-z0-9_])(?:[mp][0-9]+|r[0-9]+[ua]|message_[0-9a-f]+)(?![a-z0-9_])", re.I)
APPLIED_QUERY = re.compile(r"(?:已|最后|实际|曾经|最终)?应用|写[回入]|生效|回执")
DRAFT_QUERY = re.compile(r"给出的|返回的改稿|草稿|未应用|没应用|尚未应用|未写回")
STRUCTURED_WORDS = re.compile(r"哪一次|哪一版|哪次|哪版|是什么|有什么|怎么写的|怎么写|最后|最近|当时|当前|最终|查找|给我|看看|的|请|查|改稿|改过|改动|了|[^\w]+")
METHOD = "C1-structured-phrase"
SETTINGS = {
    "candidate_fields": ["conversation", "cutoff", "shot", "field", "state"],
    "score": "existing contiguous phrases",
    "no_model": True,
}


class Value(NamedTuple):
    shot: int
    name: str
    new: object
    old: object = None


class Record(NamedTuple):
    turn_id: str
    message_id: str
    state: str
    values: tuple[Value, ...]
    wording: str


class Request(NamedTuple):
    shots: frozenset[int]
    fields: frozenset[str]
    state: str | None
    remainder: str


class Ranking(NamedTuple):
    rows: list[tuple[str, float | None, list[str]]]
    candidates: list[str]
    reason: str


def shot_number(value: object) -> int | None:
    raw = normalize("NFKC", str(value)).strip().lower().removeprefix("s")
    if raw.isascii() and raw.isdigit():
        return int(raw)
    if raw in CHINESE_DIGITS:
        return CHINESE_DIGITS[raw]
    if "十" in raw:
        tens, _, units = raw.partition("十")
        if (not tens or tens in CHINESE_DIGITS) and (not units or units in CHINESE_DIGITS):
            return 10 * CHINESE_DIGITS.get(tens, 1) + CHINESE_DIGITS.get(units, 0)
    return None


def draft_values(payload: dict) -> tuple[Value, ...]:
    values = []
    for row in payload.get("镜头", payload.get("shots", [])):
        if not isinstance(row, dict):
            continue
        shot = shot_number(row.get("镜号", row.get("shot_id")))
        if shot is not None:
            values.extend(Value(shot, FIELD_NAMES[name], field_value) for name, field_value in row.items()
                          if name in FIELD_NAMES)
    return tuple(values)


def receipt_values(payload: list) -> tuple[Value, ...]:
    values = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        shot = shot_number(row.get("镜号"))
        field = FIELD_NAMES.get(row.get("字段"))
        if shot is not None and field and "新值" in row:
            values.append(Value(shot, field, row["新值"], row.get("旧值")))
    return tuple(values)


def record_from_message(message: Message) -> Record | None:
    content = message["content"]
    turn_id, message_id = message["turn_id"], message["id"]
    try:
        if message["role"] == "program":
            block = APPLIED_BLOCK.search(content)
            payload = json.loads(block[1]) if block else None
            if isinstance(payload, list) and (values := receipt_values(payload)):
                wording = "\n".join(
                    f"第{value.shot}镜 {value.name}，改前：{value.old}；改后：{value.new}" for value in values
                )
                return Record(turn_id, message_id, "applied", values, wording)
        if message["role"] == "assistant":
            blocks = list(REVISION_BLOCK.finditer(content))
            drafts = [block for block in blocks if block[1] == "revised_shots"]
            if len(drafts) != 1:
                return None
            payload = json.loads(drafts[0][2])
            if isinstance(payload, dict) and (values := draft_values(payload)):
                wording = content[:blocks[0].start()].strip() or json.dumps(payload, ensure_ascii=False)
                return Record(turn_id, message_id, "draft", values, wording)
    except (ValueError, TypeError):
        return None
    return None


def mentioned_shots(question: str) -> frozenset[int]:
    shots = set()
    for mention in SHOT_MENTION.finditer(question):
        first = shot_number(mention["start"])
        last = shot_number(mention["end"]) if mention["end"] else first
        if first is not None and last is not None and first <= last:
            shots.update(range(first, last + 1))
    return frozenset(shots)


def parse_request(question: str, current_shot: int | None = None) -> Request:
    text = normalize("NFKC", question)
    shots = mentioned_shots(text)
    if not shots and current_shot is not None and "本镜" in text:
        shots = frozenset({current_shot})
    fields = frozenset(FIELD_NAMES[word] for word in FIELD_MENTION.findall(text))
    state = None
    if DRAFT_QUERY.search(text):
        state = "draft"
    elif APPLIED_QUERY.search(text):
        state = "applied"
    remainder = SHOT_MENTION.sub(" ", text)
    remainder = FIELD_MENTION.sub(" ", remainder)
    remainder = APPLIED_QUERY.sub(" ", DRAFT_QUERY.sub(" ", remainder))
    remainder = STRUCTURED_WORDS.sub("", remainder.replace("本镜", ""))
    return Request(shots, fields, state, remainder)


def fits(record: Record, request: Request) -> bool:
    if request.state and record.state != request.state:
        return False
    slots = defaultdict(set)
    for value in record.values:
        slots[value.shot].add(value.name)
    return (
        all(shot in slots and request.fields <= slots[shot] for shot in request.shots)
        if request.shots
        else any(request.fields <= fields for fields in slots.values())
    )


def rank_structured(
    question: str,
    visible_turns: dict[str, list[Message]],
    records: Sequence[Record],
    current_shot: int | None = None,
) -> Ranking:
    if match := MESSAGE_ID.search(question):
        messages = [m for group in visible_turns.values() for m in group]
        if found := find_turn_by_message_id(messages, match[0]):
            turn = found[0]["turn_id"]
            return Ranking([(turn, None, ["消息 ID"])], [turn], "message_id")
        return Ranking([], [], "unknown_or_invisible_id")

    request = parse_request(question, current_shot)
    candidates = [record for record in records if record.turn_id in visible_turns and fits(record, request)]
    candidate_turns = list(dict.fromkeys(record.turn_id for record in candidates))
    if request.shots and request.fields and request.state and not request.remainder and candidates:
        last = candidates[-1]
        return Ranking([(last.turn_id, None, ["镜号、字段、状态"])], candidate_turns, "exact_slots")
    texts = {record.turn_id: record.wording[:CARD_CHARS] for record in candidates}
    ranked = rank_turns(question, texts)
    return Ranking(ranked, candidate_turns, "phrase_match" if ranked else "no_match")

