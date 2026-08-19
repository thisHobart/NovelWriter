"""Machine-readable obligations produced during the lore and structure stages.

The scene-planning stage already emits a contract alongside its Markdown so that
later stages can be checked by a program instead of by eye.  Lore and structure
used to produce free text only, which left `case_bible` to be inferred from prose
by another model call: whatever the design never stated plainly could not be
recovered downstream, and nothing detected the omission until chapters were
already being written.

These contracts are declared by the same response that produces the Markdown, so
neither stage costs an extra generation call.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple


LORE_CONTRACT_START = "<!-- LORE_CONTRACT_START -->"
LORE_CONTRACT_END = "<!-- LORE_CONTRACT_END -->"
STRUCTURE_CONTRACT_START = "<!-- STRUCTURE_CONTRACT_START -->"
STRUCTURE_CONTRACT_END = "<!-- STRUCTURE_CONTRACT_END -->"
# v2 adds profile-specific central-conflict fields and explicit chronology
# events to the structure contract.  Existing v1 files remain readable; newly
# generated contracts advertise the stronger schema.
DESIGN_CONTRACT_SCHEMA_VERSION = 2

MIN_CANONICAL_CHARACTERS = 2
MIN_SECTION_TRUTHS = 1


class DesignContractError(ValueError):
    """A lore or structure artifact is missing a usable, trustworthy contract."""

    def __init__(self, message: str, *, code: str = "design_contract_invalid"):
        super().__init__(message)
        self.code = code


def _extract_marked_json(
    response: str,
    start: str,
    end: str,
    label: str,
) -> Tuple[str, Dict[str, Any]]:
    """Split one response into Markdown plus the single JSON block it carries."""
    if not response or start not in response or end not in response:
        raise DesignContractError(f"{label}缺少契约标记", code="contract_marker_missing")
    pattern = re.compile(re.escape(start) + r"\s*(.*?)\s*" + re.escape(end), flags=re.DOTALL)
    matches = list(pattern.finditer(response))
    if len(matches) != 1:
        raise DesignContractError(
            f"{label}必须且只能包含一个契约", code="contract_marker_repeated"
        )
    raw = matches[0].group(1).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
    try:
        contract = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DesignContractError(
            f"{label}契约不是合法 JSON：{exc.msg}", code="contract_not_json"
        ) from exc
    if not isinstance(contract, dict):
        raise DesignContractError(f"{label}契约必须是 JSON 对象", code="contract_not_object")
    markdown = (response[: matches[0].start()] + response[matches[0].end() :]).strip()
    if not markdown:
        raise DesignContractError(f"{label}除契约外没有正文", code="markdown_empty")
    return markdown, contract


def _named_records(
    contract: Dict[str, Any],
    field: str,
    label: str,
) -> List[Dict[str, Any]]:
    value = contract.get(field, [])
    if not isinstance(value, list):
        raise DesignContractError(f"{label}的 {field} 必须是数组", code="field_not_list")
    records = []
    for item in value:
        if not isinstance(item, dict):
            raise DesignContractError(
                f"{label}的 {field} 只能包含对象", code="field_not_objects"
            )
        records.append(item)
    return records


# --- lore ------------------------------------------------------------------


def lore_contract_instructions() -> str:
    """Appended to the existing lore prompt; reuses the same response."""
    return f"""

生成世界观正文后，在文档末尾追加以下机器可读契约。标记必须原样保留，标记之间只能放一个合法 JSON 对象，不要使用代码围栏。
{LORE_CONTRACT_START}
{{
  "era": "故事所处年代",
  "technology_level": "技术水平，例如「现实世界当代水平」",
  "canonical_characters": [
    {{"id":"CH001","name":"人物规范名","aliases":["正文中允许出现的其它称呼"],"role":"身份"}}
  ],
  "canonical_locations": [{{"id":"LOC001","name":"地点规范名"}}],
  "canonical_factions": [{{"id":"FAC001","name":"势力规范名"}}],
  "world_constraints": ["本作明确不存在的元素，例如「没有超自然力量」"]
}}
{LORE_CONTRACT_END}

canonical_characters 是全书人名的唯一权威：后续所有阶段只能使用这里的 name，同一人物的其它写法必须登记进该人物的 aliases，不得让同一个人以两个不同的 name 出现。地点和势力同理。
""".strip()


def validate_lore_contract(contract: Dict[str, Any]) -> Dict[str, Any]:
    """Check the world registry can actually anchor later stages."""
    if not isinstance(contract, dict):
        raise DesignContractError("世界观契约必须是 JSON 对象", code="contract_not_object")

    normalized = dict(contract)
    for field in ("era", "technology_level"):
        if not str(normalized.get(field, "")).strip():
            raise DesignContractError(f"世界观契约缺少 {field}", code="missing_field")

    constraints = normalized.setdefault("world_constraints", [])
    if not isinstance(constraints, list):
        raise DesignContractError("world_constraints 必须是数组", code="field_not_list")

    characters = _named_records(normalized, "canonical_characters", "世界观契约")
    if len(characters) < MIN_CANONICAL_CHARACTERS:
        raise DesignContractError(
            f"canonical_characters 只有 {len(characters)} 条，至少需要"
            f" {MIN_CANONICAL_CHARACTERS} 条主要人物",
            code="too_few_characters",
        )

    # 同一人物以两种写法登记为两条记录，正是「萨姆/山姆」这类前后不一致的源头：
    # 名字与别名必须在全书范围内互不相撞，才能当作规范名使用。
    claimed: Dict[str, str] = {}
    for group, field in (
        (characters, "canonical_characters"),
        (_named_records(normalized, "canonical_locations", "世界观契约"), "canonical_locations"),
        (_named_records(normalized, "canonical_factions", "世界观契约"), "canonical_factions"),
    ):
        normalized[field] = group
        for record in group:
            record_id = str(record.get("id", "")).strip()
            name = str(record.get("name", "")).strip()
            if not record_id or not name:
                raise DesignContractError(
                    f"{field} 中的记录必须同时提供 id 和 name", code="record_missing_id"
                )
            names = [name]
            aliases = record.get("aliases", [])
            if aliases is not None and not isinstance(aliases, list):
                raise DesignContractError(
                    f"{field} 中 {record_id} 的 aliases 必须是数组", code="field_not_list"
                )
            names.extend(str(alias).strip() for alias in (aliases or []))
            for candidate in names:
                if not candidate:
                    continue
                owner = claimed.get(candidate)
                if owner and owner != record_id:
                    raise DesignContractError(
                        f"名称「{candidate}」同时属于 {owner} 和 {record_id}，"
                        "同一个名字不能指向两个对象",
                        code="name_collision",
                    )
                claimed[candidate] = record_id

    normalized["schema_version"] = DESIGN_CONTRACT_SCHEMA_VERSION
    normalized["origin"] = "lore"
    return normalized


def extract_lore_contract(response: str) -> Tuple[str, Dict[str, Any]]:
    markdown, contract = _extract_marked_json(
        response, LORE_CONTRACT_START, LORE_CONTRACT_END, "世界观"
    )
    return markdown, validate_lore_contract(contract)


# --- structure -------------------------------------------------------------


def structure_contract_instructions(
    section_name: str,
    section_index: int,
    total_sections: int,
    known_threads: Iterable[Dict[str, Any]] = (),
    central_conflict_schema: Optional[Mapping[str, str]] = None,
) -> str:
    """Appended to the existing per-section structure prompt."""
    open_threads = [
        {
            "id": record.get("id"),
            "thread": record.get("thread"),
            "must_close_by_section": record.get("must_close_by_section"),
        }
        for record in known_threads
    ]
    conflict_schema = dict(central_conflict_schema or {
        "surface_answer": "表面答案",
        "true_answer": "事实真相",
        "stakes": "赌注",
    })
    spine_hint = (
        f"""
  "central_question": "全书最终追问",
  "central_conflict": {json.dumps(conflict_schema, ensure_ascii=False)},"""
        if section_index == 1
        else ""
    )
    return f"""

写完本部分正文后，在文档末尾追加以下机器可读契约。标记必须原样保留，标记之间只能放一个合法 JSON 对象，不要使用代码围栏。
{STRUCTURE_CONTRACT_START}
{{
  "section": "{section_name}",
  "section_index": {section_index},
  "total_sections": {total_sections},{spine_hint}
  "truths_introduced": [{{"id":"T{section_index:02d}1","fact":"本部分确立的关键真相","reveal_at_section":"最晚在哪一部分向读者揭晓"}}],
  "chronology_events": [{{"id":"TL{section_index:02d}1","order":1,"event":"按故事真实发生顺序记录的事件","known_initially_by":[]}}],
  "threads_opened": [{{"id":"PT{section_index:02d}1","thread":"本部分开启的悬念","must_close_by_section":{total_sections}}}],
  "threads_closed": ["本部分了结的悬念 id"]
}}
{STRUCTURE_CONTRACT_END}

只登记本部分正文已经明确安排的内容，没有的数组留空。chronology_events 只记录故事世界中真实发生的事件，order 表示全书真实发生顺序，不是读者得知顺序，也不能用普通事实凑数。开启的悬念必须给出不早于本部分的 must_close_by_section；了结悬念必须沿用已有 id。截至目前仍未了结的悬念如下，本部分若要了结其中任何一条，必须使用这里的 id：
{json.dumps(open_threads, ensure_ascii=False)}
""".strip()


def validate_structure_contract(
    contract: Dict[str, Any],
    section_index: int,
    total_sections: int,
    central_conflict_schema: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    if not isinstance(contract, dict):
        raise DesignContractError("结构契约必须是 JSON 对象", code="contract_not_object")

    normalized = dict(contract)
    try:
        declared = int(normalized.get("section_index"))
    except (TypeError, ValueError):
        raise DesignContractError("结构契约缺少有效 section_index", code="missing_field") from None
    if declared != int(section_index):
        raise DesignContractError(
            f"结构契约的 section_index 为 {declared}，当前是第 {section_index} 部分",
            code="section_mismatch",
        )
    normalized["section_index"] = declared
    normalized["total_sections"] = int(total_sections)

    truths = _named_records(normalized, "truths_introduced", "结构契约")
    normalized["truths_introduced"] = truths
    for record in truths:
        if not str(record.get("id", "")).strip() or not str(record.get("fact", "")).strip():
            raise DesignContractError(
                "truths_introduced 中的记录必须同时提供 id 和 fact", code="record_missing_id"
            )

    chronology = _named_records(normalized, "chronology_events", "结构契约")
    normalized["chronology_events"] = chronology
    for record in chronology:
        event_id = str(record.get("id", "")).strip()
        event = str(record.get("event", "")).strip()
        try:
            order = int(record.get("order"))
        except (TypeError, ValueError):
            order = 0
        if not event_id or not event or order < 1:
            raise DesignContractError(
                "chronology_events 中的记录必须提供 id、event 和大于零的 order",
                code="record_missing_id",
            )
        record["order"] = order
        known_by = record.setdefault("known_initially_by", [])
        if not isinstance(known_by, list):
            raise DesignContractError(
                f"chronology_events 中 {event_id} 的 known_initially_by 必须是数组",
                code="field_not_list",
            )

    opened = _named_records(normalized, "threads_opened", "结构契约")
    normalized["threads_opened"] = opened
    for record in opened:
        thread_id = str(record.get("id", "")).strip()
        if not thread_id:
            raise DesignContractError("threads_opened 中的记录必须提供 id", code="record_missing_id")
        try:
            deadline = int(record.get("must_close_by_section"))
        except (TypeError, ValueError):
            raise DesignContractError(
                f"悬念 {thread_id} 缺少有效的 must_close_by_section", code="missing_deadline"
            ) from None
        if deadline < section_index or deadline > total_sections:
            raise DesignContractError(
                f"悬念 {thread_id} 的 must_close_by_section 必须落在第 {section_index}"
                f" 到 {total_sections} 部分之间",
                code="deadline_out_of_range",
            )
        record["must_close_by_section"] = deadline

    closed = normalized.get("threads_closed", [])
    if not isinstance(closed, list):
        raise DesignContractError("threads_closed 必须是数组", code="field_not_list")
    normalized["threads_closed"] = [str(item).strip() for item in closed if str(item).strip()]

    if section_index == 1:
        if not str(normalized.get("central_question", "")).strip():
            raise DesignContractError(
                "第一部分的结构契约必须给出 central_question", code="missing_spine"
            )
        conflict = normalized.get("central_conflict")
        if not isinstance(conflict, dict) or not any(
            str(value).strip() for value in conflict.values()
        ):
            raise DesignContractError(
                "第一部分的结构契约必须给出 central_conflict", code="missing_spine"
            )
        required_conflict_fields = tuple((central_conflict_schema or {}).keys())
        missing_conflict_fields = [
            field for field in required_conflict_fields
            if not str(conflict.get(field, "")).strip()
        ]
        if missing_conflict_fields:
            raise DesignContractError(
                "第一部分的 central_conflict 缺少当前题材要求的字段："
                + "、".join(missing_conflict_fields),
                code="missing_spine",
            )
        if len(truths) < MIN_SECTION_TRUTHS:
            raise DesignContractError(
                "第一部分必须至少确立一条 truths_introduced，否则全书没有可核对的真相",
                code="missing_spine",
            )
        if not chronology:
            raise DesignContractError(
                "第一部分必须至少声明一条 chronology_events，不能用普通事实代替真实时间线",
                code="missing_spine",
            )

    normalized["schema_version"] = DESIGN_CONTRACT_SCHEMA_VERSION
    normalized["origin"] = "structure"
    return normalized


def extract_structure_contract(
    response: str,
    section_index: int,
    total_sections: int,
    central_conflict_schema: Optional[Mapping[str, str]] = None,
) -> Tuple[str, Dict[str, Any]]:
    markdown, contract = _extract_marked_json(
        response, STRUCTURE_CONTRACT_START, STRUCTURE_CONTRACT_END, "全书结构"
    )
    return markdown, validate_structure_contract(
        contract,
        section_index,
        total_sections,
        central_conflict_schema=central_conflict_schema,
    )


def open_threads_after(contracts: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Threads opened by earlier sections that no later section has closed yet."""
    opened: Dict[str, Dict[str, Any]] = {}
    for contract in sorted(contracts, key=lambda item: int(item.get("section_index", 0))):
        for record in contract.get("threads_opened", []):
            opened[str(record["id"])] = record
        for thread_id in contract.get("threads_closed", []):
            opened.pop(str(thread_id), None)
    return list(opened.values())


def validate_structure_sequence(contracts: Iterable[Dict[str, Any]]) -> None:
    """Check the whole-story spine holds across sections before planning starts.

    Catching a thread that no section ever closes is only cheap here.  The same
    omission found during chapter acceptance means the outline was already
    turned into prose.
    """
    ordered = sorted(contracts, key=lambda item: int(item.get("section_index", 0)))
    if not ordered:
        return

    known_truths: Dict[str, Dict[str, Any]] = {}
    opened: Dict[str, Tuple[int, int, Dict[str, Any]]] = {}
    closed: Dict[str, int] = {}
    for contract in ordered:
        index = int(contract["section_index"])
        for record in contract.get("truths_introduced", []):
            truth_id = str(record["id"])
            previous = known_truths.get(truth_id)
            if previous and previous.get("fact") != record.get("fact"):
                raise DesignContractError(
                    f"真相 {truth_id} 在不同部分被定义成了两件事",
                    code="truth_definition_conflict",
                )
            known_truths[truth_id] = record
        for record in contract.get("threads_opened", []):
            thread_id = str(record["id"])
            if thread_id in opened:
                raise DesignContractError(
                    f"悬念 {thread_id} 被重复开启", code="thread_reopened"
                )
            opened[thread_id] = (index, int(record["must_close_by_section"]), record)
        for thread_id in contract.get("threads_closed", []):
            if thread_id not in opened:
                raise DesignContractError(
                    f"第 {index} 部分了结了尚未开启的悬念 {thread_id}",
                    code="thread_closed_before_open",
                )
            if opened[thread_id][0] > index:
                raise DesignContractError(
                    f"悬念 {thread_id} 在开启前被了结", code="thread_closed_before_open"
                )
            closed[thread_id] = index

    total_sections = int(ordered[-1].get("total_sections", ordered[-1]["section_index"]))
    complete = int(ordered[-1]["section_index"]) >= total_sections
    for thread_id, (opened_at, deadline, _) in opened.items():
        closed_at = closed.get(thread_id)
        if closed_at is None:
            if complete or deadline <= int(ordered[-1]["section_index"]):
                raise DesignContractError(
                    f"悬念 {thread_id} 在第 {opened_at} 部分开启，"
                    f"但截止第 {deadline} 部分仍没有了结安排",
                    code="thread_missing_closure",
                )
        elif closed_at > deadline:
            raise DesignContractError(
                f"悬念 {thread_id} 计划在第 {closed_at} 部分了结，晚于截止第 {deadline} 部分",
                code="thread_closes_late",
            )


# --- shared retry driver ---------------------------------------------------


def generate_with_contract_retry(
    send: Callable[[str], str],
    prompt: str,
    parse: Callable[[str], Tuple[str, Dict[str, Any]]],
    *,
    retry_limit: int = 2,
    on_retry: Callable[[int, DesignContractError], None] | None = None,
) -> Tuple[str, Dict[str, Any]]:
    """Generate until the contract validates, feeding each failure back verbatim.

    The failure text is the repair instruction: it already names the offending
    field, so restating it is more precise than a generic "try again".
    """
    last_error: DesignContractError | None = None
    for attempt in range(retry_limit + 1):
        request = prompt
        if last_error is not None:
            request += (
                "\n\n上一次结果未通过契约校验。只修复下面指出的问题，"
                "不要改变已经写好的正文主旨：\n- " + str(last_error)
            )
        response = send(request)
        if not response or not response.strip():
            last_error = DesignContractError("大模型没有返回内容", code="empty_response")
        else:
            try:
                return parse(response)
            except DesignContractError as exc:
                last_error = exc
        if attempt < retry_limit and on_retry is not None:
            on_retry(attempt + 1, last_error)
    raise DesignContractError(
        f"在 {retry_limit} 次重试后契约仍未通过：{last_error}",
        code=getattr(last_error, "code", "contract_retry_exhausted"),
    )
