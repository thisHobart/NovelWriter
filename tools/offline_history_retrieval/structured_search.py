"""Retrieve historical storyboard revisions with local fields and exact phrases."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
import json
from math import fsum, log
import re
from typing import NamedTuple
from unicodedata import normalize

from evidence_pack import REVISION_BLOCK
from experiment_ranker import query_phrases
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
METHOD = "C5-storyboard-terms"
SETTINGS = {
    "candidate_fields": ["conversation", "cutoff", "shot", "field", "state"],
    "request_scope": "distinguish a preceding context clause",
    "lexical_normalization": "fixed storyboard aliases",
    "score": "query coverage by weighted 2–4 character terms",
    "evidence_gate": "non-metadata content support and new-value check for substitutions",
    "no_model": True,
}


class Value(NamedTuple):
    shot: int
    name: str
    new: object
    old: object = None


class Record(NamedTuple):
    session_id: str
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


def record_from_message(message: Message, session_id: str) -> Record | None:
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
                return Record(session_id, turn_id, message_id, "applied", values, wording)
        if message["role"] == "assistant":
            blocks = list(REVISION_BLOCK.finditer(content))
            drafts = [block for block in blocks if block[1] == "revised_shots"]
            if len(drafts) != 1:
                return None
            payload = json.loads(drafts[0][2])
            if isinstance(payload, dict) and (values := draft_values(payload)):
                wording = content[:blocks[0].start()].strip() or json.dumps(payload, ensure_ascii=False)
                return Record(session_id, turn_id, message_id, "draft", values, wording)
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


CONTEXT_BOUNDARY = re.compile(r"[，,；;]\s*(?=哪(?:一)?[份版稿回次]|找(?:一下)?|回查)")


def request_clause(question: str) -> str:
    """Use a prior shot as context when the question explicitly introduces a later request."""
    boundary = CONTEXT_BOUNDARY.search(question)
    if boundary:
        context, request = question[:boundary.start()], question[boundary.end():]
        if re.search(r"已经|问题|作为背景", context) and len(request) >= 8:
            return request
    return question


def parse_request(question: str, current_shot: int | None = None) -> Request:
    text = request_clause(normalize("NFKC", question))
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


STORYBOARD_ALIASES = {
    "正脸": ("脸部正面", "人物正面", "角色正面", "主角正面", "主人公正面", "正面脸"),
    "第一人称": ("主观视点", "主观视角", "主观镜头"),
    "回头": ("回过头", "向后转", "回头看", "调头", "扭头", "回望"),
    "小屋": ("木屋", "小木屋", "小房子"),
    "消散": ("化掉", "化开", "散成飞雪", "散成雪花", "化成雪花"),
    "阳光": ("日光", "光亮"),
    "桌边": ("餐桌前", "桌子前", "桌前", "桌旁"),
    "进食": ("用餐", "吃饭", "吃东西"),
    "坐着": ("就座", "坐下来", "坐下", "坐在", "已坐"),
    "屋内": ("室内", "房里", "屋里", "房内"),
    "进屋": ("入室", "入屋", "走进屋", "进入屋内"),
    "屋外": ("室外", "房外", "门外"),
    "饮料": ("饮品", "汽水"),
    "卡片": ("明信片",),
    "对岸": ("对面",),
    "雪散": ("雪幕散开", "雪幕散去", "飞雪散去", "风雪散开"),
}
STORYBOARD_WORDS = {word: main for main, variants in STORYBOARD_ALIASES.items() for word in variants}
STORYBOARD_TERMS = re.compile("|".join(re.escape(word) for word in sorted(STORYBOARD_WORDS, key=len, reverse=True)))


def normalize_storyboard_text(text: str) -> str:
    return STORYBOARD_TERMS.sub(lambda match: STORYBOARD_WORDS[match[0]], normalize("NFKC", text).lower())


def character_terms(text: str) -> set[str]:
    return {
        block[offset:offset + size]
        for block in re.findall(r"[\u3400-\u9fffA-Za-z0-9]+", normalize_storyboard_text(text))
        for size in (2, 3, 4)
        for offset in range(len(block) - size + 1)
    }


def rank_characters(question: str, candidates: Sequence[Record]) -> list[tuple[str, float | None, list[str]]]:
    terms = character_terms(" ".join(query_phrases(normalize_storyboard_text(question))))
    documents = {record.turn_id: character_terms(record.wording[:CARD_CHARS]) for record in candidates}
    if not documents or not terms:
        return []
    weights = {
        term: (len(term) - 1) * (1 + log((len(documents) + 1) / (1 + sum(
            term in text for text in documents.values()
        )))) for term in terms
    }
    total = fsum(weights.values())
    ranked = []
    for turn, document in documents.items():
        matches = document & terms
        if matches:
            score = round(fsum(weights[word] for word in matches) / total, 12)
            features = sorted(matches, key=lambda word: (-weights[word], word))[:8]
            ranked.append((turn, score, ["字片段 " + word for word in features]))
    return sorted(ranked, key=lambda row: (-row[1], -int(row[0][1:])))


ANSWER_TARGET = re.compile(r"(?:改成|换成|改为|换为|替换[成为]|调整[成为])\s*(?:了)?([^，。！？?；;]+)")
QUESTION_FRAME = re.compile(
    r"哪一份|哪一稿|哪一版|哪一次|哪份|哪版|哪稿|哪回|哪次|找一下|回查|找回|"
    r"有没有|确认过|返回的|明确写|怎么写|是什么|写进了|加了|的版本|"
    r"用不到|不用再|不必再|不用|不必|这个|当天|当时|改稿|修改稿"
)


def content_terms(text: str) -> set[str]:
    without_slots = SHOT_MENTION.sub(" ", normalize_storyboard_text(text))
    without_slots = FIELD_MENTION.sub(" ", without_slots)
    without_slots = APPLIED_QUERY.sub(" ", DRAFT_QUERY.sub(" ", without_slots))
    without_slots = QUESTION_FRAME.sub(" ", without_slots)
    return character_terms(" ".join(query_phrases(without_slots)))


def text_of(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def proof_terms(record: Record, request: Request, new_only: bool) -> set[str]:
    values = [
        value for value in record.values
        if (not request.shots or value.shot in request.shots)
        and (not request.fields or value.name in request.fields)
    ]
    text = [text_of(value.new) for value in values if value.new is not None]
    if not new_only:
        text.extend(text_of(value.old) for value in values if value.old is not None)
        if record.state == "draft" and not request.fields:
            text.append(record.wording)
    return character_terms(" ".join(text))


def has_support(question: str, request: Request, record: Record, terms: set[str]) -> bool:
    target = ANSWER_TARGET.search(question)
    if target and (required := content_terms(target[1])):
        matches = required & proof_terms(record, request, new_only=True)
        minimum = min(2, sum(len(word) == 2 for word in required))
        if sum(len(word) == 2 for word in matches) < minimum:
            return False
    matches = terms & proof_terms(record, request, new_only=bool(request.state == "applied" and "已" in question))
    return len(matches) >= 2 or bool(matches and any(len(word) >= 3 for word in matches))


def rank_structured(
    question: str,
    visible_turns: dict[str, list[Message]],
    records: Sequence[Record],
    session_id: str,
    current_shot: int | None = None,
) -> Ranking:
    if match := MESSAGE_ID.search(question):
        messages = [m for group in visible_turns.values() for m in group]
        if found := find_turn_by_message_id(messages, match[0]):
            turn = found[0]["turn_id"]
            return Ranking([(turn, None, ["消息 ID"])], [turn], "message_id")
        return Ranking([], [], "unknown_or_invisible_id")

    request = parse_request(question, current_shot)
    visible_sources = {(key, message['id']) for key, messages in visible_turns.items() for message in messages}
    candidates = [record for record in records if record.session_id == session_id
                  and (record.turn_id, record.message_id) in visible_sources and fits(record, request)]
    candidate_turns = list(dict.fromkeys(record.turn_id for record in candidates))
    if request.shots and request.fields and request.state and not request.remainder and candidates:
        last = candidates[-1]
        return Ranking([(last.turn_id, None, ["镜号、字段、状态"])], candidate_turns, "exact_slots")
    terms = content_terms(question)
    verified = {record.turn_id for record in candidates if has_support(question, request, record, terms)}
    ranked = [row for row in rank_characters(question, candidates) if row[0] in verified]
    return Ranking(ranked, candidate_turns, "content_match" if ranked else "no_supported_match")
