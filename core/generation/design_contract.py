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
# events to the structure contract.  v3 additionally guarantees that the
# chronology `order` numbers are unique across the whole book.  Existing files
# remain readable; the whole-story check that needs that guarantee is skipped
# for anything written before v3, so a finished project is never retroactively
# marked blocked over a defect its own generation run could not have caught.
# v4 makes `reveal_at_section` a validated number and adds `depends_on`, which
# is where the narrative graph's `requires` edges come from.
DESIGN_CONTRACT_SCHEMA_VERSION = 4
CHRONOLOGY_UNIQUE_ORDER_SINCE = 3
TRUTH_DEPENDENCY_SINCE = 4

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
    known_truths: Iterable[Dict[str, Any]] = (),
    known_events: Iterable[Dict[str, Any]] = (),
) -> str:
    """Appended to the existing per-section structure prompt."""
    # 提示词构造不该因为一条记录残缺就把整个阶段带崩：拿不到编号的记录照样列出来
    # 给模型看，只是不计入「已占用」。
    declared_events = [
        {
            "id": record.get("id"),
            "order": record.get("order"),
            "event": record.get("event"),
        }
        for record in known_events
        if str(record.get("id", "")).strip()
    ]
    used_orders: List[int] = []
    for record in declared_events:
        try:
            order = int(record["order"])
        except (KeyError, TypeError, ValueError):
            continue
        if order > 0 and order not in used_orders:
            used_orders.append(order)
    used_orders.sort()
    shared_order_rules = (
        "编号只表示故事世界里的真实先后，允许留空档（比如上一部分用到 9，"
        "本部分从 20 开始），也允许本部分的事件排在前面部分之前（倒叙是合法的），"
        "唯独不允许两条事件共用一个编号。两件事若真的同时发生，仍要给两个不同的"
        "编号，在 event 文字里说明它们同时。"
    )
    if used_orders:
        chronology_block = (
            "\n前面各部分已经登记的时间线事件如下。**order 是全书共用的一条编号，"
            "不是本部分内部的计数**：已占用的编号是 "
            + "、".join(str(number) for number in used_orders)
            + "，本部分的每一条事件都必须另取一个没出现过的数字。\n"
            + shared_order_rules
            + "\n"
            + json.dumps(declared_events, ensure_ascii=False)
        )
    else:
        chronology_block = (
            "\n**order 是全书共用的一条编号，不是本部分内部的计数。**"
            "本部分是第一批登记时间线的，从 1 开始编即可，后面的部分会接着往下取。\n"
            + shared_order_rules
        )
    # 揭晓部分要一起发下去：depends_on 要求被依赖的真相不晚于依赖它的那条揭晓，
    # 模型看不见前面各条排在第几部分就没法遵守。
    declared_truths = [
        {
            "id": record.get("id"),
            "fact": record.get("fact"),
            "reveal_at_section": record.get("reveal_at_section"),
        }
        for record in known_truths
        if str(record.get("id", "")).strip()
    ]
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
  "truths_introduced": [{{"id":"T{section_index:02d}1","fact":"本部分确立的关键真相","reveal_at_section":{total_sections},"depends_on":[]}}],
  "chronology_events": [{{"id":"TL{section_index:02d}1","order":1,"event":"按故事真实发生顺序记录的事件","known_initially_by":[]}}],
  "threads_opened": [{{"id":"PT{section_index:02d}1","thread":"本部分开启的悬念","must_close_by_section":{total_sections}}}],
  "threads_closed": ["本部分了结的悬念 id"]
}}
{STRUCTURE_CONTRACT_END}

只登记本部分正文已经明确安排的内容，没有的数组留空。chronology_events 只记录故事世界中真实发生的事件，order 表示全书真实发生顺序，不是读者得知顺序，也不能用普通事实凑数。开启的悬念必须给出不早于本部分的 must_close_by_section；了结悬念必须沿用已有 id。截至目前仍未了结的悬念如下，本部分若要了结其中任何一条，必须使用这里的 id：
{json.dumps(open_threads, ensure_ascii=False)}

reveal_at_section 必须是**数字**（第几部分），不能写部分的名字，取值落在第 {section_index}
到 {total_sections} 部分之间——真相不能在它被确立之前就揭晓。

depends_on 登记**这条真相要成立，读者必须先知道哪几条真相**，填的是真相 id，只能取
本部分或前面部分已经确立的 id。没有前置就留空数组，不要为了填而填。被依赖的真相必须
在依赖它的那条之前（或同一部分）揭晓，否则读者会先看到结论、后看到依据。依赖关系不能
绕成环——A 依赖 B、B 又依赖 A，两条就都永远揭不了。

前面各部分已经确立的真相如下。**同一个 id 在全书只能指同一件事**：要复述其中任何一条，
必须连 id 带 fact 原样沿用，一个字都不要改写；本部分新确立的真相另起一个没用过的 id。
{json.dumps(declared_truths, ensure_ascii=False)}

{chronology_block}
""".strip()


def _validate_truth_dependencies(
    truths: List[Dict[str, Any]],
    earlier: Iterable[Dict[str, Any]] = (),
) -> None:
    """Check `depends_on` names real truths and never schedules a premise late.

    这是叙事图上 requires 边的来源。图那边早就有查环（三色 DFS）和「揭示缺前置」
    两条检查，但生产代码从不建边，所以两条都从未真正执行过。边一旦由这里播种出来，
    环和「依据排在结论之后」就都变成会真实发生的错误，而在这里拦下来只花一次重试，
    等到图上再拦，播种失败只记日志、图会静悄悄地保持为空。

    档期这一条不需要真的排一遍拓扑序：只要每条依赖边都满足「被依赖的不晚于依赖它
    的」，按揭晓部分排出来的顺序就已经是一个合法拓扑序，逐边检查与整体排序等价。
    """
    earlier_records = [
        record for record in earlier if str(record.get("id", "")).strip()
    ]
    known_sections: Dict[str, int] = {}
    for record in list(earlier_records) + list(truths):
        truth_id = str(record.get("id", "")).strip()
        if not truth_id:
            continue
        try:
            known_sections[truth_id] = int(record.get("reveal_at_section"))
        except (TypeError, ValueError):
            known_sections.setdefault(truth_id, 0)

    # 前面部分的依赖也放进邻接表：环不一定全落在本部分之内。
    adjacency: Dict[str, List[str]] = {}
    for record in earlier_records:
        truth_id = str(record.get("id", "")).strip()
        raw = record.get("depends_on") or []
        if isinstance(raw, list):
            adjacency.setdefault(truth_id, []).extend(
                str(item).strip() for item in raw if str(item).strip()
            )

    for record in truths:
        truth_id = str(record.get("id", "")).strip()
        raw = record.get("depends_on", [])
        if raw in (None, ""):
            raw = []
        if not isinstance(raw, list):
            raise DesignContractError(
                f"真相 {truth_id} 的 depends_on 必须是数组", code="field_not_list"
            )
        depends_on = [str(item).strip() for item in raw if str(item).strip()]
        record["depends_on"] = depends_on
        for dependency in depends_on:
            if dependency == truth_id:
                raise DesignContractError(
                    f"真相 {truth_id} 的 depends_on 指向了它自己",
                    code="dependency_self_reference",
                )
            if dependency not in known_sections:
                raise DesignContractError(
                    f"真相 {truth_id} 依赖的 {dependency} 不是任何一部分确立过的真相。"
                    f"depends_on 只能填本部分或前面部分已经确立的真相 id。",
                    code="dependency_not_found",
                )
            premise = known_sections[dependency]
            target = known_sections.get(truth_id, 0)
            if premise and target and premise > target:
                raise DesignContractError(
                    f"真相 {truth_id} 计划在第 {target} 部分揭晓，却依赖第 {premise}"
                    f" 部分才揭晓的 {dependency}：读者会先看到结论、后看到依据",
                    code="dependency_revealed_too_late",
                )
        # 本部分复述某条真相时给的依赖以本部分为准，不与前面那份合并。
        adjacency[truth_id] = depends_on

    # 只有本部分新写的依赖会引入新环，但环可能穿过前面部分的真相，所以整张图一起查。
    colours: Dict[str, int] = {}

    def visit(node: str, path: List[str]) -> None:
        colours[node] = 1
        for dependency in adjacency.get(node, []):
            state = colours.get(dependency, 0)
            if state == 0:
                visit(dependency, path + [dependency])
            elif state == 1:
                start = path.index(dependency) if dependency in path else 0
                cycle = " → ".join(path[start:] + [dependency])
                raise DesignContractError(
                    f"真相之间的 depends_on 绕成了环：{cycle}。"
                    f"环上的每一条都要等别人先揭晓，结果谁也揭不了。",
                    code="dependency_cycle",
                )
        colours[node] = 2

    for candidate in sorted(adjacency):
        if colours.get(candidate, 0) == 0:
            visit(candidate, [candidate])


def validate_structure_contract(
    contract: Dict[str, Any],
    section_index: int,
    total_sections: int,
    central_conflict_schema: Optional[Mapping[str, str]] = None,
    known_orders: Iterable[int] = (),
    known_truths: Iterable[Dict[str, Any]] = (),
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
        truth_id = str(record.get("id", "")).strip()
        if not truth_id or not str(record.get("fact", "")).strip():
            raise DesignContractError(
                "truths_introduced 中的记录必须同时提供 id 和 fact", code="record_missing_id"
            )
        # reveal_at_section 此前只在 schema 里写着「最晚在哪一部分向读者揭晓」，
        # 一个字的校验都没有。实测模型有时写数字、有时写部分的名字（current_work
        # 第 2 部分的两条写的是「情节上升」），播种时又被整个丢掉，于是没有一个
        # 真相节点带着计划揭晓章号——「计划在结局前完成的揭示仍未执行」这条检查
        # 因此永远不会响。
        try:
            reveal_at = int(record.get("reveal_at_section"))
        except (TypeError, ValueError):
            raise DesignContractError(
                f"真相 {truth_id} 的 reveal_at_section 必须是数字（第几部分），"
                f"不能写部分的名字",
                code="reveal_section_invalid",
            ) from None
        if reveal_at < section_index or reveal_at > total_sections:
            raise DesignContractError(
                f"真相 {truth_id} 的 reveal_at_section 必须落在第 {section_index}"
                f" 到 {total_sections} 部分之间",
                code="reveal_section_out_of_range",
            )
        record["reveal_at_section"] = reveal_at

    _validate_truth_dependencies(truths, known_truths)

    chronology = _named_records(normalized, "chronology_events", "结构契约")
    normalized["chronology_events"] = chronology
    # order 声称是「全书真实发生顺序」，但各部分是分别生成的，谁也看不见别人用过
    # 什么号。实测每一个真实项目都撞车：current_work 六个部分里，第 4、5 部分并列
    # 用 13–15，第 6 部分从 1 重新开始，与第 1 部分整段重叠，六个编号重复。一条声
    # 称是全书总序的字段实际是六段互不相干的局部计数，下游拿它排不出任何顺序。
    #
    # 在这里拦，是因为这里还救得回来：单段契约不合格会走 generate_with_contract_retry
    # 的重试，报错原文直接当修复指令发回去，代价是一次重试。等到全部段落生成完再合校，
    # 整个结构阶段（实测三幕 350 秒、六部分 465 秒）一起作废，而且没有补救路径。
    taken = {int(number) for number in known_orders}
    seen_orders: Dict[int, str] = {}
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
        if order in seen_orders:
            raise DesignContractError(
                f"时间线事件 {event_id} 与本部分的 {seen_orders[order]} 都用了 order "
                f"{order}。order 是全书唯一编号，每条事件必须各占一个。",
                code="chronology_order_duplicated",
            )
        if order in taken:
            raise DesignContractError(
                f"时间线事件 {event_id} 用的 order {order} 已经被前面的部分占用。"
                f"order 是全书共用的一条编号，请改用一个没出现过的数字"
                f"（已占用：{'、'.join(str(number) for number in sorted(taken))}）。",
                code="chronology_order_reused",
            )
        seen_orders[order] = event_id
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
    known_orders: Iterable[int] = (),
    known_truths: Iterable[Dict[str, Any]] = (),
) -> Tuple[str, Dict[str, Any]]:
    markdown, contract = _extract_marked_json(
        response, STRUCTURE_CONTRACT_START, STRUCTURE_CONTRACT_END, "全书结构"
    )
    return markdown, validate_structure_contract(
        contract,
        section_index,
        total_sections,
        central_conflict_schema=central_conflict_schema,
        known_orders=known_orders,
        known_truths=known_truths,
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


def truths_after(contracts: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Truths already declared by earlier sections, in declaration order.

    后面几部分必须看得见这些：全书校验要求同一个 truth id 在各部分里说的是同一件
    事，而各部分是分别生成的。此前只把「尚未了结的悬念」发下去，真相没发，于是第二
    幕会用不同的措辞重新定义 T001，等到全部生成完才在合校时报错——整个结构阶段
    （实测 465 秒、八次调用）的产出一起作废，而且没有补救路径。
    """
    known: Dict[str, Dict[str, Any]] = {}
    for contract in sorted(contracts, key=lambda item: int(item.get("section_index", 0))):
        for record in contract.get("truths_introduced", []):
            known.setdefault(str(record.get("id", "")), record)
    return [record for key, record in known.items() if key]


def chronology_after(contracts: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Timeline events already registered by earlier sections, in story order.

    同真相一样，这些必须发给下一部分：order 是全书共用的一条编号，而各部分分别
    生成，看不见别人用过什么号。不发下去，模型只能从 1 或从本部分序号重新起编，
    撞车是必然的。
    """
    events: Dict[str, Dict[str, Any]] = {}
    for contract in sorted(contracts, key=lambda item: int(item.get("section_index", 0))):
        for record in contract.get("chronology_events", []):
            event_id = str(record.get("id", "")).strip()
            if event_id:
                events.setdefault(event_id, record)
    return sorted(events.values(), key=lambda item: int(item.get("order", 0) or 0))


def chronology_orders_used(contracts: Iterable[Dict[str, Any]]) -> List[int]:
    """The order numbers earlier sections have already claimed."""
    used: set[int] = set()
    for record in chronology_after(contracts):
        try:
            order = int(record.get("order"))
        except (TypeError, ValueError):
            continue
        if order > 0:
            used.add(order)
    return sorted(used)


def validate_structure_sequence(contracts: Iterable[Dict[str, Any]]) -> None:
    """Check the whole-story spine holds across sections before planning starts.

    Catching a thread that no section ever closes is only cheap here.  The same
    omission found during chapter acceptance means the outline was already
    turned into prose.
    """
    ordered = sorted(contracts, key=lambda item: int(item.get("section_index", 0)))
    if not ordered:
        return

    # 时间线编号的唯一性只在 v3 起才由生成过程保证。更早写下的契约每一份都撞车
    # （见 validate_structure_contract 里的说明），而这个函数同时被流程门禁和总览
    # 页的状态判定调用——对着已经定稿的项目报错，只会把它的结构那一步锁上，写完
    # 的正文一个字也不会因此变好。所以旧档不查，新档必查。
    versions = [int(contract.get("schema_version", 0) or 0) for contract in ordered]
    checks_unique_orders = all(
        version >= CHRONOLOGY_UNIQUE_ORDER_SINCE for version in versions
    )
    checks_dependencies = all(version >= TRUTH_DEPENDENCY_SINCE for version in versions)
    seen_orders: Dict[int, Tuple[int, str]] = {}

    known_truths: Dict[str, Dict[str, Any]] = {}
    opened: Dict[str, Tuple[int, int, Dict[str, Any]]] = {}
    closed: Dict[str, int] = {}
    for contract in ordered:
        index = int(contract["section_index"])
        if checks_unique_orders:
            for record in contract.get("chronology_events", []):
                event_id = str(record.get("id", "")).strip()
                try:
                    order = int(record.get("order"))
                except (TypeError, ValueError):
                    continue
                if order in seen_orders:
                    previous_section, previous_id = seen_orders[order]
                    raise DesignContractError(
                        f"时间线 order {order} 被第 {previous_section} 部分的 "
                        f"{previous_id} 和第 {index} 部分的 {event_id} 同时占用，"
                        f"全书排不出真实先后",
                        code="chronology_order_duplicated",
                    )
                seen_orders[order] = (index, event_id)
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

    # 依赖是逐段校验的，但那时只看得见当时已有的部分。全部齐了再合起来查一遍：
    # 被依赖的真相是否真的存在、有没有排在依赖它的那条之后、整体有没有绕成环。
    #
    # 用每一次出现的并集，不是按 id 去重后的最后一份：一条真相被后面的部分复述时
    # 可以带不同的 depends_on，而播种会把每一次出现的依赖都建成边。这里只看最后
    # 一份，就会漏掉第一份声明、稍后却被真的建出来的那条边。
    merged: Dict[str, Dict[str, Any]] = {}
    for contract in ordered:
        for record in contract.get("truths_introduced", []):
            truth_id = str(record.get("id", "")).strip()
            if not truth_id:
                continue
            entry = merged.setdefault(
                truth_id,
                {
                    "id": truth_id,
                    "fact": record.get("fact"),
                    "reveal_at_section": record.get("reveal_at_section"),
                    "depends_on": [],
                },
            )
            for dependency in record.get("depends_on") or []:
                premise = str(dependency).strip()
                if premise and premise not in entry["depends_on"]:
                    entry["depends_on"].append(premise)
    if checks_dependencies:
        _validate_truth_dependencies(list(merged.values()))

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
