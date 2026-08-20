"""Machine-readable chapter obligations produced during scene planning."""

from __future__ import annotations

import json
import os
import re
from glob import glob
from typing import Any, Dict, Iterable, Iterator, List, Tuple

from core.generation.helper_fns import parse_scene_sections
from core.generation.story_ledger import StoryLedgerManager, compact_json


CONTRACT_START = "<!-- CHAPTER_CONTRACT_START -->"
CONTRACT_END = "<!-- CHAPTER_CONTRACT_END -->"
CONTRACT_SCHEMA_VERSION = 2
STATE_LIST_FIELDS = (
    "facts_added",
    "facts_confirmed",
    "facts_contradicted",
    "timeline_events",
    "character_updates",
    "plot_thread_updates",
)


class PlanningContractError(ValueError):
    """A scene plan is missing a usable, trustworthy chapter contract."""

    def __init__(
        self,
        message: str,
        *,
        chapters: Iterable[int] = (),
        code: str = "planning_contract_invalid",
    ):
        super().__init__(message)
        # Order matters: callers list repair targets best-first, and the
        # planning UI walks them in that order.
        ordered: List[int] = []
        for chapter in chapters:
            number = int(chapter)
            if number > 0 and number not in ordered:
                ordered.append(number)
        self.chapters = tuple(ordered)
        self.code = code


def contract_output_instructions(
    chapter_number: int,
    existing_index: Dict[str, Any] | None = None,
    domain_fields: Dict[str, str] | None = None,
    obligations: Dict[str, Any] | None = None,
) -> str:
    """Instructions appended to the existing scene-planning request.

    This deliberately uses the same LLM response as the Markdown plan; it does
    not introduce another generation call.
    """
    index = existing_index or {}
    # The open-thread list is the part the model gets wrong most often, so it is
    # spelled out rather than left to be inferred from a truncated index dump.
    open_threads = index.get("open_plot_threads", [])
    due_now = set(index.get("threads_due_this_chapter", []))
    if open_threads:
        thread_lines = "\n".join(
            "- {id}｜{thread}｜第 {opened} 章提出｜{deadline}{due}".format(
                id=state.get("id", ""),
                thread=state.get("thread", ""),
                opened=state.get("opened_at", "?"),
                deadline=(
                    f"计划第 {state['deadline_chapter']} 章了结"
                    if state.get("deadline_chapter")
                    else "未定截止章"
                ),
                due="（本章必须了结）" if state.get("id") in due_now else "",
            )
            for state in open_threads
        )
        thread_block = (
            "仍未了结的悬念（前面已经埋下、还欠读者一个答案；"
            "只有这些 id 可以在本章 close）：\n"
            f"{thread_lines}"
        )
    else:
        thread_block = (
            "仍未了结的悬念：无。前面没有任何悬念在等答案，"
            "所以本章的 close 记录只能是本章自己埋下又自己了结的。"
        )

    obligation_block = ""
    if obligations:
        lines = [
            f"- 悬念 {info['id']}（{info.get('thread', '')}）：第 {info['closed_at']} 章要了结它，本章必须埋下"
            for info in obligations.get("threads_closed_later", {}).values()
        ] + [
            f"- 事实 {info['id']}（{info.get('fact', '')}）：第 {info['used_at']} 章要引用它，本章必须引入"
            for info in obligations.get("facts_used_later", {}).values()
        ]
        if lines:
            obligation_block = (
                "\n后续章节已经规划完毕，并依赖本章提供以下内容，必须原样保留：\n"
                + "\n".join(lines)
                + "\n"
            )

    index_text = compact_json(
        {
            "facts": index.get("facts", []),
            "timeline_events": index.get("timeline_events", []),
            "closed_plot_threads": index.get("closed_plot_threads", []),
        },
        5000,
    )
    domain_lines = "\n".join(
        f'  "{name}": {schema_hint},'
        for name, schema_hint in (domain_fields or {}).items()
    )
    return f"""

完成场景规划后，在文档末尾追加以下机器可读契约。标记必须原样保留，标记之间只能放一个合法 JSON 对象，不要使用代码围栏。
{CONTRACT_START}
{{
  "chapter": {chapter_number},
  "core_question": "本章集中追问的问题",
  "reader_knows_before": [],
  "reader_knows_after": [],
  "reader_must_not_know_yet": [],
  "scene_boundaries": [],
  "personal_cost": "本章不可逆个人代价；没有则留空",
  "cost_character": "承担代价的人物；没有则留空",
  "irreversible_change": "本章结束后的不可逆变化",
{domain_lines}
  "facts_added": [{{"id":"F-{chapter_number:03d}-01","fact":"属性名","value":"取值","first_stated_at":"scene_1"}}],
  "facts_confirmed": [{{"id":"已有事实ID","fact":"属性名","value":"既有取值"}}],
  "facts_contradicted": [{{"id":"已有事实ID","reason":"推翻理由","new_value":"新取值"}}],
  "timeline_events": [{{"id":"TL-{chapter_number:03d}-01","event":"事件名称","time":"明确时刻","location_id":"地点"}}],
  "character_updates": [{{"id":"CU-{chapter_number:03d}-01","character":"人物规范名","attribute":"属性名","value":"取值","stable":true}}],
  "plot_thread_updates": [{{"id":"PT-{chapter_number:03d}-01","thread":"悬念的一句话描述","status":"open","deadline_chapter":{chapter_number}}}]
}}
{CONTRACT_END}

契约只记录本章场景已经明确安排的内容，没有相应内容的数组留空。

关于 plot_thread_updates：status 填 "open" 表示**本章埋下**这条悬念（读者开始好奇），填 "closed" 表示**本章了结**它（读者得到答案）。
- 埋下：必须给出不早于本章的 deadline_chapter，说明最晚第几章揭晓；id 不能与任何既有悬念重复。
- 了结：必须沿用下方“仍未了结的悬念”中的 id。不要了结已经了结过的，也不要凭章号规律推测一个不在列表里的 id——那会让本章为一个读者从未见过的谜面写揭晓戏。
- 本章自己埋下又自己了结的悬念：只写一条 closed 记录，并加上 "opened_in_chapter": true。
- 下方标注“本章必须了结”的悬念，本章必须处理：要么了结它，要么写一条 {{"id":"该悬念id","status":"open","extend":true,"deadline_chapter":新的章号}} 把揭晓明确推迟到后面某一章。不处理就等于让读者一直悬着却没人记账。

{thread_block}
{obligation_block}
既有事实、时间事件与已了结线索如下，重复出现的必须沿用其中的 id：
{index_text}
""".strip()


def extract_scene_plan_contract(response: str, chapter_number: int) -> Tuple[str, Dict[str, Any]]:
    """Split one model response into clean Markdown and a validated contract."""
    if not response or CONTRACT_START not in response or CONTRACT_END not in response:
        raise PlanningContractError("场景规划缺少章节契约标记")
    pattern = re.compile(
        re.escape(CONTRACT_START) + r"\s*(.*?)\s*" + re.escape(CONTRACT_END),
        flags=re.DOTALL,
    )
    matches = list(pattern.finditer(response))
    if len(matches) != 1:
        raise PlanningContractError("场景规划必须且只能包含一个章节契约")
    raw = matches[0].group(1).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
    try:
        contract = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlanningContractError(f"章节契约不是合法 JSON：{exc.msg}") from exc
    markdown = (response[: matches[0].start()] + response[matches[0].end() :]).strip()
    if not parse_scene_sections(markdown):
        raise PlanningContractError("场景规划中没有可解析的场景标题")
    normalized = validate_planning_contract(contract, chapter_number)
    normalized["schema_version"] = CONTRACT_SCHEMA_VERSION
    normalized["origin"] = "scene_planning"
    return markdown, normalized


def validate_planning_contract(
    contract: Dict[str, Any],
    chapter_number: int,
    *,
    require_origin: bool = False,
) -> Dict[str, Any]:
    if not isinstance(contract, dict):
        raise PlanningContractError("章节契约必须是 JSON 对象")
    try:
        declared_chapter = int(contract.get("chapter"))
    except (TypeError, ValueError):
        raise PlanningContractError("章节契约缺少有效 chapter") from None
    if declared_chapter != int(chapter_number):
        raise PlanningContractError(
            f"章节契约章号为 {declared_chapter}，当前场景规划是第 {chapter_number} 章"
        )
    if require_origin and contract.get("origin") != "scene_planning":
        raise PlanningContractError("章节契约不是由场景规划阶段生成的")

    normalized = dict(contract)
    for field in STATE_LIST_FIELDS:
        value = normalized.setdefault(field, [])
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise PlanningContractError(f"章节契约字段 {field} 必须是对象数组")

    seen_ids = set()
    for field in ("facts_added", "facts_confirmed", "facts_contradicted", "timeline_events"):
        for record in normalized[field]:
            if not str(record.get("id", "")).strip():
                raise PlanningContractError(f"章节契约字段 {field} 中的记录缺少 id")
    for record in normalized["plot_thread_updates"]:
        thread_id = str(record.get("id", "")).strip()
        status = str(record.get("status", "")).strip().lower()
        if not thread_id:
            raise PlanningContractError("每条线索更新都必须提供 id")
        if thread_id in seen_ids:
            raise PlanningContractError(f"本章线索 id 重复：{thread_id}")
        seen_ids.add(thread_id)
        if status not in {"open", "closed"}:
            raise PlanningContractError(
                f"悬念 {_thread_label(thread_id, record)} 的 status 只能填 open（本章埋下）"
                "或 closed（本章了结）"
            )
        record["status"] = status
        if status == "open":
            if record.get("extend") and int(record.get("deadline_chapter", 0)) <= chapter_number:
                raise PlanningContractError(
                    f"悬念 {_thread_label(thread_id, record)} 延期后的 deadline_chapter "
                    f"必须晚于本章第 {chapter_number} 章"
                )
            try:
                deadline = int(record.get("deadline_chapter"))
            except (TypeError, ValueError):
                raise PlanningContractError(
                    f"本章埋下的悬念 {_thread_label(thread_id, record)} 缺少有效的 "
                    "deadline_chapter，必须说明最晚在第几章了结"
                ) from None
            if deadline < chapter_number:
                raise PlanningContractError(
                    f"悬念 {_thread_label(thread_id, record)} 的截止章早于埋下它的本章"
                )
            record["deadline_chapter"] = deadline
    return normalized


def load_planning_contracts(output_dir: str) -> List[Dict[str, Any]]:
    manager = StoryLedgerManager(output_dir)
    contracts: List[Dict[str, Any]] = []
    for path in glob(os.path.join(manager.contract_dir, "chapter_*.json")):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                contract = json.load(handle)
            chapter = int(contract.get("chapter"))
            contracts.append(validate_planning_contract(contract, chapter, require_origin=True))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise PlanningContractError(f"无法使用章节契约 {path}：{exc}") from exc
    return sorted(contracts, key=lambda item: int(item["chapter"]))


_ID_CHAPTER_PATTERN = re.compile(r"^[A-Za-z]+-(\d+)-")


def _source_chapter_from_id(record_id: str) -> int | None:
    """The chapter encoded in an ID such as ``PT-005-01``, if it has one."""
    match = _ID_CHAPTER_PATTERN.match(str(record_id))
    return int(match.group(1)) if match else None


def _thread_label(thread_id: str, *sources: Dict[str, Any]) -> str:
    """Name a thread the way a person reads it: ID plus what it actually is.

    A bare ``PT-005-01`` tells the author nothing about which mystery broke.
    """
    for source in sources:
        name = str((source or {}).get("thread", "")).strip()
        if name:
            return f"{thread_id}（{name}）"
    return str(thread_id)


def thread_states(contracts: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Fold the per-chapter update stream into one state per thread.

    Planning contracts record *updates*, not state.  Every consumer that needs
    to know whether a thread is still waiting for its payoff has to replay the
    stream; doing it in one place keeps the prompt, the acceptance gate and the
    semantic resolver from disagreeing about what "open" means.
    """
    states: Dict[str, Dict[str, Any]] = {}
    for contract in sorted(contracts, key=lambda item: int(item.get("chapter", 0))):
        chapter = int(contract.get("chapter", 0))
        for raw in contract.get("plot_thread_updates", []):
            thread_id = str(raw.get("id", "")).strip()
            if not thread_id:
                continue
            state = states.setdefault(
                thread_id, {"id": thread_id, "opened_at": None, "closed_at": None}
            )
            if raw.get("thread"):
                state["thread"] = raw.get("thread")
            if str(raw.get("status", "")).lower() == "open":
                # An "extend" record is not a second raising of the thread; it
                # only restates when the payoff is now due.
                if state["opened_at"] is None and not raw.get("extend"):
                    state["opened_at"] = chapter
                state["deadline_chapter"] = raw.get("deadline_chapter")
            else:
                if state["opened_at"] is None and raw.get("opened_in_chapter") is True:
                    state["opened_at"] = chapter
                    state.setdefault("deadline_chapter", chapter)
                if state["closed_at"] is None:
                    state["closed_at"] = chapter
    return states


def open_threads_before(contracts: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Threads that have been raised and still owe the reader a payoff."""
    return [
        state
        for state in thread_states(contracts).values()
        if state["opened_at"] is not None and state["closed_at"] is None
    ]


def build_existing_planning_index(output_dir: str, before_chapter: int) -> Dict[str, Any]:
    """The prior-planning view handed to the model for one chapter.

    Threads are given as resolved *state* rather than as the raw update stream:
    a model shown "PT-005-01 open, PT-005-01 closed" has to work out for itself
    that the thread is spent, and it regularly gets that wrong by closing it a
    second time.
    """
    before_chapter = int(before_chapter)
    contracts = [
        item for item in load_planning_contracts(output_dir)
        if int(item["chapter"]) < before_chapter
    ]
    states = thread_states(contracts)
    open_threads = [
        state for state in states.values()
        if state["opened_at"] is not None and state["closed_at"] is None
    ]
    open_threads.sort(
        key=lambda state: (_deadline_or_last(state, before_chapter), state["id"])
    )
    return {
        "facts": [record for item in contracts for record in item.get("facts_added", [])],
        "timeline_events": [
            record for item in contracts for record in item.get("timeline_events", [])
        ],
        "open_plot_threads": open_threads,
        "closed_plot_threads": [
            {"id": state["id"], "thread": state.get("thread", ""), "closed_at": state["closed_at"]}
            for state in states.values()
            if state["closed_at"] is not None
        ],
        "threads_due_this_chapter": [
            state["id"] for state in open_threads
            if _deadline_or_last(state, before_chapter) <= before_chapter
        ],
    }


def _deadline_or_last(state: Dict[str, Any], fallback: int) -> int:
    try:
        return int(state.get("deadline_chapter"))
    except (TypeError, ValueError):
        return int(fallback)


def downstream_obligations(output_dir: str, chapter_number: int) -> Dict[str, Any]:
    """What already-planned later chapters depend on this chapter providing.

    Replanning one chapter in the middle of a finished plan is the single most
    common way a project acquires a dangling reference: the regenerated chapter
    only ever sees chapters before it, so it can silently drop a thread that
    chapter 8 is counting on opening.
    """
    chapter_number = int(chapter_number)
    later = [
        item for item in load_planning_contracts(output_dir)
        if int(item["chapter"]) > chapter_number
    ]
    threads: Dict[str, Dict[str, Any]] = {}
    for contract in later:
        for record in contract.get("plot_thread_updates", []):
            if str(record.get("status", "")).lower() != "closed":
                continue
            if record.get("opened_in_chapter") is True:
                continue
            threads.setdefault(
                str(record.get("id", "")),
                {
                    "id": str(record.get("id", "")),
                    "thread": record.get("thread", ""),
                    "closed_at": int(contract["chapter"]),
                },
            )
    facts: Dict[str, Dict[str, Any]] = {}
    for contract in later:
        for field in ("facts_confirmed", "facts_contradicted"):
            for record in contract.get(field, []):
                facts.setdefault(
                    str(record.get("id", "")),
                    {
                        "id": str(record.get("id", "")),
                        "fact": record.get("fact", ""),
                        "used_at": int(contract["chapter"]),
                    },
                )
    return {"threads_closed_later": threads, "facts_used_later": facts}


def validate_contract_against_history(
    contract: Dict[str, Any],
    prior: Iterable[Dict[str, Any]],
    chapter_number: int,
    *,
    obligations: Dict[str, Any] | None = None,
) -> None:
    """Check one chapter against its neighbours while the model can still fix it.

    ``validate_contract_sequence`` catches the same errors, but only once every
    chapter is on disk — by then the offending chapter has lost its generation
    context and the only repair available is a blind regeneration.  Running the
    cross-chapter rules here puts the failure back inside the retry loop, where
    the exact message is fed to the model that produced it.
    """
    chapter_number = int(chapter_number)
    prior = [item for item in prior if int(item.get("chapter", 0)) < chapter_number]
    states = thread_states(prior)
    open_ids = {
        thread_id for thread_id, state in states.items()
        if state["opened_at"] is not None and state["closed_at"] is None
    }
    known_facts = {
        str(record.get("id", ""))
        for item in prior
        for record in item.get("facts_added", [])
    }
    known_facts |= {
        str(record.get("id", "")) for record in contract.get("facts_added", [])
    }

    for record in contract.get("plot_thread_updates", []):
        thread_id = str(record.get("id", ""))
        if record.get("status") == "open":
            if thread_id in states and record.get("extend"):
                if thread_id not in open_ids:
                    raise PlanningContractError(
                        f"悬念 {_thread_label(thread_id, record, states[thread_id])}"
                        f"已在第 {states[thread_id]['closed_at']} 章了结，无法延期",
                        chapters=(chapter_number,),
                        code="thread_closed_twice",
                    )
                continue
            if thread_id in states:
                raise PlanningContractError(
                    f"悬念 {_thread_label(thread_id, record, states[thread_id])}"
                    f"已在第 {states[thread_id]['opened_at']} 章埋下，本章不能再埋一次；"
                    "如果这是另一条悬念，请换一个 id；"
                    '如果只是想推迟它的了结章，请加上 "extend": true 并给出新的 deadline_chapter',
                    chapters=(chapter_number,),
                    code="thread_reopened",
                )
            continue
        if thread_id in open_ids or record.get("opened_in_chapter") is True:
            continue
        closed_at = states.get(thread_id, {}).get("closed_at")
        if closed_at is not None:
            raise PlanningContractError(
                f"悬念 {_thread_label(thread_id, record, states.get(thread_id, {}))} "
                f"已在第 {closed_at} 章了结，本章不能再了结一次",
                chapters=(chapter_number,),
                code="thread_closed_twice",
            )
        raise PlanningContractError(
            f"第 {chapter_number} 章要了结悬念 {_thread_label(thread_id, record)}，"
            "但前面没有任何一章埋下它。只能了结“仍未了结的悬念”中列出的 id；"
            "如果这条悬念是本章自己提出、自己解决的，"
            '请在该记录上加 "opened_in_chapter": true',
            chapters=(chapter_number,),
            code="thread_closed_before_open",
        )

    # A deadline the model set for itself is only worth something if someone
    # checks it on the day.  Left to the final gate, a thread can drift twenty
    # chapters past its payoff before anyone notices, and by then the chapter
    # that could still have fixed it cheaply is long gone.
    handled = {
        str(record.get("id", "")) for record in contract.get("plot_thread_updates", [])
    }
    for thread_id in sorted(open_ids - handled):
        state = states[thread_id]
        try:
            deadline = int(state.get("deadline_chapter"))
        except (TypeError, ValueError):
            continue
        if deadline > chapter_number:
            continue
        raise PlanningContractError(
            f"悬念 {_thread_label(thread_id, state)}在第 {state['opened_at']} 章埋下时"
            f"承诺最晚第 {deadline} 章了结，本章已到期。"
            "要么在本章了结它（status 填 closed），"
            '要么写一条 {"id":"%s","status":"open","extend":true,"deadline_chapter":<新的章号>} '
            "把了结章明确推迟" % thread_id,
            chapters=(chapter_number, state["opened_at"]),
            code="thread_overdue",
        )

    for field in ("facts_confirmed", "facts_contradicted"):
        for record in contract.get(field, []):
            fact_id = str(record.get("id", ""))
            if fact_id not in known_facts:
                raise PlanningContractError(
                    f"第 {chapter_number} 章的 {field} 引用了尚未引入的事实 {fact_id}",
                    chapters=(chapter_number,),
                    code="unknown_fact_reference",
                )

    if not obligations:
        return
    declared_threads = {
        str(record.get("id", "")) for record in contract.get("plot_thread_updates", [])
    }
    for thread_id, info in obligations.get("threads_closed_later", {}).items():
        if thread_id in declared_threads or thread_id in states:
            continue
        raise PlanningContractError(
            f"第 {info['closed_at']} 章要了结悬念 {_thread_label(thread_id, info)}，"
            "本章必须保留埋下它的那条记录，不能删掉",
            chapters=(chapter_number,),
            code="thread_opening_dropped",
        )
    declared_facts = {
        str(record.get("id", "")) for record in contract.get("facts_added", [])
    }
    for fact_id, info in obligations.get("facts_used_later", {}).items():
        if fact_id in declared_facts or fact_id in known_facts:
            continue
        raise PlanningContractError(
            f"第 {info['used_at']} 章要引用事实 {fact_id}（{info.get('fact', '')}），"
            f"本章必须保留它的引入记录",
            chapters=(chapter_number,),
            code="fact_introduction_dropped",
        )


def iter_contract_defects(
    contracts: Iterable[Dict[str, Any]],
    *,
    total_chapters: int | None = None,
) -> Iterator[PlanningContractError]:
    """Yield every cross-chapter defect instead of stopping at the first.

    Deciding whether a half-finished project is worth repairing needs the whole
    list: one dangling thread is a repair, forty is a re-plan.  The rules live
    here once; the blocking gate below is just "the first thing this yields".
    """
    ordered = sorted(contracts, key=lambda item: int(item.get("chapter", 0)))
    by_chapter = {int(item.get("chapter", 0)): item for item in ordered}
    if total_chapters is not None:
        missing = [number for number in range(1, total_chapters + 1) if number not in by_chapter]
        if missing:
            preview = "、".join(str(number) for number in missing[:10])
            yield PlanningContractError(
                f"缺少第 {preview} 章的场景规划契约",
                chapters=missing[:10],
                code="missing_chapter_contract",
            )

    opened: Dict[str, Tuple[int, int]] = {}
    closed: Dict[str, int] = {}
    thread_names: Dict[str, Dict[str, Any]] = {}
    known_facts: Dict[str, Dict[str, Any]] = {}
    for contract in ordered:
        chapter = int(contract["chapter"])
        try:
            validate_planning_contract(contract, chapter, require_origin=True)
        except PlanningContractError as exc:
            yield PlanningContractError(
                f"第 {chapter} 章的契约本身不合法：{exc}",
                chapters=(chapter,),
                code=getattr(exc, "code", "planning_contract_invalid"),
            )
            continue
        for record in contract.get("facts_added", []):
            fact_id = str(record["id"])
            previous = known_facts.get(fact_id)
            if previous and (
                previous.get("fact") != record.get("fact")
                or previous.get("value") != record.get("value")
            ):
                yield PlanningContractError(
                    f"事实 {fact_id} 在规划阶段出现互相冲突的定义",
                    chapters=(chapter,),
                    code="fact_definition_conflict",
                )
                continue
            known_facts[fact_id] = record
        for field in ("facts_confirmed", "facts_contradicted"):
            for record in contract.get(field, []):
                fact_id = str(record["id"])
                if fact_id not in known_facts:
                    yield PlanningContractError(
                        f"第 {chapter} 章的 {field} 引用了尚未引入的事实 {fact_id}",
                        chapters=(chapter,),
                        code="unknown_fact_reference",
                    )
        for record in contract.get("plot_thread_updates", []):
            thread_id = str(record["id"])
            if record["status"] == "open":
                if thread_id in opened and record.get("extend"):
                    opened_at, _ = opened[thread_id]
                    opened[thread_id] = (opened_at, int(record["deadline_chapter"]))
                    continue
                if thread_id in opened:
                    yield PlanningContractError(
                        f"悬念 {_thread_label(thread_id, record)} 被重复埋下",
                        chapters=(chapter,),
                        code="thread_reopened",
                    )
                    continue
                opened[thread_id] = (chapter, int(record["deadline_chapter"]))
                thread_names.setdefault(thread_id, record)
            else:
                if thread_id not in opened:
                    if record.get("opened_in_chapter") is True:
                        opened[thread_id] = (chapter, chapter)
                        thread_names.setdefault(thread_id, record)
                    else:
                        # Prefer repairing the chapter that was supposed to raise
                        # the thread: rewriting the closing chapter instead would
                        # throw away the payoff its outline was built around.
                        source = _source_chapter_from_id(thread_id)
                        targets = [chapter]
                        if source is not None and source < chapter and source in by_chapter:
                            targets.insert(0, source)
                        yield PlanningContractError(
                            f"第 {chapter} 章要了结悬念 {_thread_label(thread_id, record)}，"
                            "但前面没有任何一章埋下它",
                            chapters=targets,
                            code="thread_closed_before_open",
                        )
                        continue
                closed[thread_id] = chapter

    for thread_id, (opened_at, deadline) in opened.items():
        closed_at = closed.get(thread_id)
        label = _thread_label(thread_id, thread_names.get(thread_id, {}))
        if closed_at is not None and closed_at < opened_at:
            yield PlanningContractError(
                f"悬念 {label}的了结章（第 {closed_at} 章）"
                f"早于埋下章（第 {opened_at} 章）",
                chapters=(opened_at, closed_at),
                code="thread_closed_before_open",
            )
        elif closed_at is None and (total_chapters is not None or deadline in by_chapter):
            # The deadline chapter is where the payoff was promised, so it gets
            # first refusal; the opening chapter is the fallback, because moving
            # its own deadline is also a legitimate fix.
            yield PlanningContractError(
                f"悬念 {label}在第 {opened_at} 章埋下，"
                f"但直到截止的第 {deadline} 章都没有安排了结",
                chapters=(deadline, opened_at),
                code="thread_missing_closure",
            )
        elif closed_at is not None and closed_at > deadline:
            # The deadline chapter never mentions this thread, so regenerating
            # it cannot help.  Only the chapter that set the deadline, or the
            # one that closes it, can resolve the disagreement.
            yield PlanningContractError(
                f"悬念 {label}在第 {opened_at} 章埋下时"
                f"承诺第 {deadline} 章了结，"
                f"实际却排到第 {closed_at} 章。"
                f"请把第 {opened_at} 章的 deadline_chapter 改为 {closed_at}，"
                f"或把了结提前到第 {deadline} 章",
                chapters=(opened_at, closed_at),
                code="thread_closes_late",
            )


def collect_contract_defects(
    contracts: Iterable[Dict[str, Any]],
    *,
    total_chapters: int | None = None,
    limit: int = 200,
) -> List[PlanningContractError]:
    """Every defect in one pass, so a project can be triaged before repair."""
    found: List[PlanningContractError] = []
    for defect in iter_contract_defects(contracts, total_chapters=total_chapters):
        found.append(defect)
        if len(found) >= limit:
            break
    return found


def validate_contract_sequence(
    contracts: Iterable[Dict[str, Any]],
    *,
    total_chapters: int | None = None,
) -> None:
    """Raise on the first cross-chapter defect, before any prose is generated."""
    for defect in iter_contract_defects(contracts, total_chapters=total_chapters):
        raise defect
