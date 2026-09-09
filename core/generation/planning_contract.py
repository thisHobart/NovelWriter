"""Machine-readable chapter obligations produced during scene planning."""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from glob import glob
from typing import Any, Dict, Iterable, Iterator, List, Tuple

from core.generation.chapter_continuity import (
    CHAPTER_FUNCTIONS,
    CONTINUITY_KEY,
    chapter_function_defects,
    continuity_defects,
    continuity_instructions,
    continuity_schema_block,
    timeline_mesh_findings,
)
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
    "narrative_transitions",
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
    narrative_context: Dict[str, Any] | None = None,
    continuity_context: Dict[str, Any] | None = None,
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
            f"- 悬念 {info['id']}（{info.get('thread', '')}）：第 {info['closed_at']} 章要了结它，"
            f"本章必须埋下，且 deadline_chapter 不得早于第 {info['closed_at']} 章"
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
    narrative_text = compact_json(narrative_context or {}, 8000)

    # 叙事图上一个可选节点都没有时，必须明说这几个字段留空。
    #
    # 契约校验对「有没有引用图节点」分两档：零引用是合法的（既有项目一路三十章
    # 都跑在空图上），一旦引用了任何节点，primary_thread、primary_action、
    # via_node_ids、plot_thread_updates 就全部变成必填且必须指向真实存在的节点。
    # 空图上这一档没有任何一条能满足。
    #
    # 而上面的 schema 仍然写着「从 active_threads 选择」并给了一条 advance 示例，
    # 于是模型只能自己编一个情节线 id——契约当场升级成「图支撑」，六条规则一起
    # 报错，重试两次后整个场景规划阶段崩溃。实测短篇链路必然踩中：它从头到尾没有
    # 任何一步会往图里加节点。
    graph_is_empty = not any(
        (narrative_context or {}).get(key)
        for key in ("active_threads", "available_threads", "available_clues", "available_facts")
    )
    empty_graph_block = ""
    graph_usage_block = ""
    if not graph_is_empty:
        context = narrative_context or {}
        active = [
            str(item.get("id", "")) for item in context.get("active_threads", [])
            if str(item.get("id", "")).strip()
        ]
        available = [
            str(item.get("id", "")) for item in context.get("available_threads", [])
            if str(item.get("id", "")).strip()
        ]
        unopened = [item for item in available if item not in active]
        # 「先 open 再 advance」是硬规则：touch/advance/complicate/cross 都要求这条线
        # 已经被前面某一章打开过。叙事图播种之后节点是存在的，但账本里一章都还没打开
        # 它们，模型看见 id 就直接写 advance——实测第 1、2 章都栽在这里，每章白烧
        # 一轮规划调用。这条在构造提示词时就完全知道，没有理由让校验退回去重问。
        lines = ["", "关于叙事图上的情节线（下面这几条规则是硬性的）："]
        if active:
            lines.append(
                "- **已经打开、可以直接推进**的情节线：" + "、".join(active)
                + "。对它们可以用 touch / advance / complicate / cross / close。"
            )
        if unopened:
            lines.append(
                "- **图上有、但还没有任何一章打开过**的情节线：" + "、".join(unopened)
                + "。选中其中任何一条，primary_action 只能是 open，"
                "plot_thread_updates 里也必须有一条同 id、action 为 open 的记录，"
                "并给出不早于本章的 deadline_chapter（最晚第几章了结）。"
                "对没打开过的线直接写 advance 会被判不合格。"
            )
        if not active and unopened:
            lines.append(
                "- 本章之前没有任何情节线处于打开状态，所以本章的 primary_action "
                "只能是 open。"
            )
        lines.append(
            "- primary_thread 与 primary_action 必须在 plot_thread_updates 里有一条"
            "对应记录，否则算没记账。"
        )
        lines.append(
            "- 所有节点 id 只能从上面列出的清单里取，不要自己编造——"
            "图上不存在的 id 会让整份契约不合格。"
        )
        graph_usage_block = "\n".join(lines) + "\n"

    # schema 里的示例原本写死成 "id":"PT-004-01"、"via_node_ids":["图节点ID"]——
    # 那是在演示怎么编一个不存在的 id。图是空的时候无所谓，图一旦播种，契约校验
    # 就会因为这些编造的 id 判「引用了不存在的叙事图节点」。实测第 4、5 章连报
    # 五次，第 4 章两轮重试后硬失败。示例改用图上真实存在的 id。
    context = narrative_context or {}
    _thread_ids = [
        str(item.get("id", "")).strip()
        for group in ("active_threads", "available_threads")
        for item in context.get(group, [])
        if str(item.get("id", "")).strip()
    ]
    _node_ids = [
        str(item.get("id", "")).strip()
        for group in ("available_facts", "available_clues")
        for item in context.get(group, [])
        if str(item.get("id", "")).strip()
    ]
    thread_example = _thread_ids[0] if _thread_ids else f"PT-{chapter_number:03d}-01"
    node_example = _node_ids[0] if _node_ids else (_thread_ids[0] if _thread_ids else "图节点ID")
    if graph_is_empty:
        empty_graph_block = """
**本作目前没有叙事图节点**（上面的 active_threads、available_threads、
available_clues、available_facts 都是空的）。因此下面这几个字段必须留空，
不要自己编造节点 ID：
- "primary_thread"、"secondary_thread"、"primary_action"、"secondary_action"、
  "crossover" 一律填空字符串 ""；
- "plot_thread_updates"、"narrative_transitions"、"allowed_reveals"、
  "forbidden_reveals"、"intentionally_silent_threads" 一律填空数组 []。
本章的悬念与推进照常写进 core_question、reader_knows_after 和场景规划正文，
不需要挂到叙事图上。
"""
    def chapter_scoped_ids(schema_hint: str) -> str:
        """Turn static examples such as C001 into chapter-unique sample IDs."""
        return re.sub(
            r'("id"\s*:\s*")([A-Za-z]+)001(")',
            lambda match: (
                f'{match.group(1)}{match.group(2).upper()}-'
                f'{int(chapter_number):03d}-01{match.group(3)}'
            ),
            schema_hint,
        )

    domain_lines = "\n".join(
        f'  "{name}": {chapter_scoped_ids(schema_hint)},'
        for name, schema_hint in (domain_fields or {}).items()
    )
    chapter_function_values = "|".join(CHAPTER_FUNCTIONS)
    # 第一章没有上一章可接，衔接块整段不出现在模板里，免得模型为它编一个来源。
    continuity_schema = (
        "\n" + continuity_schema_block(chapter_number)
        if int(chapter_number) > 1
        else ""
    )
    continuity_block = continuity_instructions(
        chapter_number, **(continuity_context or {})
    )
    return f"""

完成场景规划后，在文档末尾追加以下机器可读契约。标记必须原样保留，标记之间只能放一个合法 JSON 对象，不要使用代码围栏。
{CONTRACT_START}
{{
  "chapter": {chapter_number},
  "chapter_function": "{chapter_function_values}",{continuity_schema}
  "core_question": "本章集中追问的问题",
  "reader_knows_before": [],
  "reader_knows_after": [],
  "reader_must_not_know_yet": [],
  "scene_boundaries": [],
  "personal_cost": "本章个人代价；没有则留空",
  "cost_character": "承担代价的人物；没有则留空",
  "irreversible_change": "确有不可逆变化时填写；过渡、余波、关系或沉淀章可留空",
  "ending_effect": "推进、代价、认知变化、关系位移或有意留白中的实际收束效果",
  "narrative_graph_revision": {(narrative_context or {}).get("graph_revision", 0)},
  "primary_thread": "从 active_threads 选择；首次开启时可从 available_threads 选择",
  "secondary_thread": "允许为空",
  "primary_action": "open|touch|advance|complicate|cross|close",
  "secondary_action": "允许为空",
  "crossover": "允许为空",
  "allowed_reveals": [],
  "forbidden_reveals": [],
  "intentionally_silent_threads": [],
{domain_lines}
  "facts_added": [{{"id":"F-{chapter_number:03d}-01","fact":"属性名","value":"取值","first_stated_at":"scene_1"}}],
  "facts_confirmed": [{{"id":"已有事实ID","fact":"属性名","value":"既有取值"}}],
  "facts_contradicted": [{{"id":"已有事实ID","reason":"推翻理由","new_value":"新取值"}}],
  "timeline_events": [{{"id":"TL-{chapter_number:03d}-01","event":"事件名称","time":"明确时刻","location_id":"地点","scene":"scene_1"}}],
  "character_updates": [{{"id":"CU-{chapter_number:03d}-01","character":"人物规范名","attribute":"属性名","value":"取值","stable":false}}],
  "plot_thread_updates": [{{"id":"{thread_example}","action":"advance","via_node_ids":["{node_example}"],"progress_note":"实际推进"}}],
  "narrative_transitions": [{{"node_id":"{node_example}","transition":"introduce_to_reader|make_inferable|reveal|execute|deprecate","scene":"scene_1"}}]
}}
{CONTRACT_END}

契约只记录本章场景已经明确安排的内容，没有相应内容的数组留空。

timeline_events 的 scene 填这件事发生在本章第几场（scene_1、scene_2…）。写第 N 场时，
提示词里只会放到第 N 场为止的内容；标错场次会让某一场提前拿到后面才该发生的事。

关于 plot_thread_updates：status 填 "open" 表示**本章埋下**这条悬念（读者开始好奇），填 "closed" 表示**本章了结**它（读者得到答案）。
- 埋下：必须给出不早于本章的 deadline_chapter，说明最晚第几章揭晓；id 不能与任何既有悬念重复。
- 了结：必须沿用下方“仍未了结的悬念”中的 id。不要了结已经了结过的，也不要凭章号规律推测一个不在列表里的 id——那会让本章为一个读者从未见过的谜面写揭晓戏。
- 本章自己埋下又自己了结的悬念：只写一条 closed 记录，并加上 "opened_in_chapter": true。
- 下方标注“本章必须了结”的悬念，本章必须处理：要么了结它，要么写一条 {{"id":"该悬念id","status":"open","extend":true,"deadline_chapter":新的章号}} 把揭晓明确推迟到后面某一章。不处理就等于让读者一直悬着却没人记账。

关于 character_updates 的 stable：填 true 表示这项属性一经确立、此后各章都必须复述同一取值。只有工龄、年龄、籍贯、亲属关系、血型这类一旦定下就不该再变的属性才填 true。
- 心理状态、认知立场、行动目标、所在位置、伤势、掌握的情报等会随剧情推进变化的，一律填 false——人物弧光本来就要变，填 true 会让后面章节的正常成长被判成前后矛盾。
- 拿不准就填 false。

{thread_block}
{obligation_block}
本章规划前叙事图上下文如下。blocked_reveals 和 forbidden_nodes 绝对不能选择；
plot_thread_updates 声明 advance 时必须用 via_node_ids 指出实际推进节点：
{narrative_text}
{empty_graph_block}{graph_usage_block}

既有事实、时间事件与已了结线索如下，重复出现的必须沿用其中的 id：
{index_text}
{continuity_block}
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
    normalized = validate_planning_contract(
        contract, chapter_number, require_continuity=True
    )
    normalized["schema_version"] = CONTRACT_SCHEMA_VERSION
    normalized["origin"] = "scene_planning"
    return markdown, normalized


def validate_planning_contract(
    contract: Dict[str, Any],
    chapter_number: int,
    *,
    require_origin: bool = False,
    require_continuity: bool = False,
) -> Dict[str, Any]:
    """Check one chapter contract, optionally demanding the continuity block.

    ``require_continuity`` is off by default so that contracts written before
    the block existed still load: an accepted chapter must not become invalid
    retroactively.  It is switched on wherever a contract is being *produced*
    (scene planning) or is about to drive prose (the writing loop), which is
    exactly where an empty hand-off is still cheap to fix.
    """
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
    if contract.get("stale"):
        raise PlanningContractError(
            "章节契约引用的叙事图已经变化，必须重新规划",
            chapters=(chapter_number,),
            code="narrative_graph_contract_stale",
        )

    normalized = dict(contract)
    if require_continuity:
        defects = [
            *chapter_function_defects(normalized, (), chapter_number),
            *continuity_defects(normalized, chapter_number),
        ]
        if defects:
            raise PlanningContractError(
                "；".join(defects),
                chapters=(chapter_number,),
                code="chapter_continuity_missing",
            )
    normalized.setdefault("chapter_function", "advance")
    normalized.setdefault(
        "ending_effect", str(normalized.get("irreversible_change", ""))
    )
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
        graph_managed = (
            bool(record.get("narrative_graph_managed"))
            if "narrative_graph_managed" in record
            else ("action" in record or bool(record.get("via_node_ids")))
        )
        action = str(record.get("action") or record.get("status") or "").strip().lower()
        if action == "closed":
            action = "close"
        if action not in {"open", "touch", "advance", "complicate", "cross", "close"}:
            raise PlanningContractError(
                f"情节线 {_thread_label(thread_id, record)} 的 action 不受支持"
            )
        record["action"] = action
        record["narrative_graph_managed"] = graph_managed
        # Preserve the v2 state-stream fields for old consumers.  Non-terminal
        # actions keep the thread open but are explicitly marked continuation.
        status = "closed" if action == "close" else "open"
        if action not in {"open", "close"}:
            record["continuation"] = True
        if not thread_id:
            raise PlanningContractError("每条线索更新都必须提供 id")
        if thread_id in seen_ids:
            raise PlanningContractError(f"本章线索 id 重复：{thread_id}")
        seen_ids.add(thread_id)
        record["status"] = status
        if action == "advance" and not [
            item for item in record.get("via_node_ids", []) if str(item).strip()
        ]:
            raise PlanningContractError(
                f"情节线 {_thread_label(thread_id, record)} 声明 advance 时必须提供 via_node_ids",
                chapters=(chapter_number,),
                code="thread_advance_without_node",
            )
        if action == "open":
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
    transitions = normalized.setdefault("narrative_transitions", [])
    for transition in transitions:
        node_id = str(transition.get("node_id", "")).strip()
        name = str(transition.get("transition", "")).strip()
        if not node_id:
            raise PlanningContractError("narrative_transitions 中的记录缺少 node_id")
        if name not in {
            "introduce_to_reader",
            "make_inferable",
            "reveal",
            "execute",
            "deprecate",
        }:
            raise PlanningContractError(f"不支持的叙事状态转换：{name}")
    return normalized


_SCENE_MARKER = re.compile(r"scene[_\s-]*(\d+)", re.IGNORECASE)

#: 记录里可能出现的「这条属于第几场」标记。`payoff_at` 不在其中：一条线索在第一场
#: 埋下、第三场兑现，写第一场时仍旧需要看见它。
_SCENE_MARKER_KEYS = ("first_stated_at", "introduced_at", "scene", "scene_id")


def _marked_scene(value: Any) -> int | None:
    match = _SCENE_MARKER.search(str(value or ""))
    return int(match.group(1)) if match else None


def contract_for_scene(
    contract: Dict[str, Any], scene_number: int | None
) -> Dict[str, Any]:
    """写第 N 场时该看到的那一份契约副本：本场及此前的内容。

    整份契约里写着全章每一场分别要做什么，而这一份的抬头是「必须兑现」——写第一场
    的模型照着把第二、三场也兑现了。所以属于后面场次的记录不放在这一份里。

    **后面场次的边界不是靠删掉来防的**：实测把它们一并删掉之后越界反而更严重——
    那几行同时还在当栅栏用，模型靠它们才知道自己这一场到哪儿为止，删掉就一路写到
    底。边界要单独交给 `later_scene_boundaries`，在提示词里以「禁止触碰」的名义
    列出来，而不是躺在「必须兑现」的清单里。

    **闸门用的仍是完整契约**：这里过滤的只是提示词副本，没有放宽任何检查。看不出
    属于第几场的记录一律保留——那有可能正是本场必须遵守的约束，宁可多给也不能漏。
    """
    if not isinstance(contract, dict) or not scene_number or int(scene_number) <= 0:
        return contract
    scene_number = int(scene_number)
    scoped = deepcopy(contract)

    def belongs_to_a_later_scene(item: Any, is_boundary: bool) -> bool:
        marked = None
        if isinstance(item, dict):
            # 只看已知的场次标记键，不把整个字典 str() 之后拿去匹配：那样任何
            # 一个恰好写着 "scene 3" 的自由文本都会被当成场次标记。
            keys = ("scene_number", *_SCENE_MARKER_KEYS) if is_boundary else _SCENE_MARKER_KEYS
            for key in keys:
                marked = _marked_scene(item.get(key))
                if marked is None and str(item.get(key, "")).strip().isdigit():
                    marked = int(item[key])
                if marked is not None:
                    break
        elif is_boundary:
            marked = _marked_scene(item)
        return marked is not None and marked > scene_number

    for name, value in list(scoped.items()):
        if not isinstance(value, list):
            continue
        is_boundary = name == "scene_boundaries"
        scoped[name] = [
            item for item in value if not belongs_to_a_later_scene(item, is_boundary)
        ]
    return scoped


def later_scene_boundaries(
    contract: Dict[str, Any], scene_number: int | None
) -> List[str]:
    """属于第 N 场之后的场次边界，写成一行一条给提示词用。

    这些行必须让写第 N 场的模型看见——它靠这些才知道自己这一场到哪儿为止。要紧的
    是名义：放在「必须兑现」的契约清单里，模型会去兑现；放在「禁止触碰」的清单里，
    才是栅栏。
    """
    if not isinstance(contract, dict) or not scene_number or int(scene_number) <= 0:
        return []
    scene_number = int(scene_number)
    boundaries = contract.get("scene_boundaries")
    if not isinstance(boundaries, list):
        return []
    later: List[str] = []
    for item in boundaries:
        if isinstance(item, dict):
            marked = _marked_scene(item.get("scene_number"))
            if marked is None and str(item.get("scene_number", "")).strip().isdigit():
                marked = int(item["scene_number"])
            if marked is None:
                marked = next(
                    (
                        _marked_scene(item.get(key))
                        for key in _SCENE_MARKER_KEYS
                        if _marked_scene(item.get(key)) is not None
                    ),
                    None,
                )
            text = json.dumps(item, ensure_ascii=False)
        else:
            marked = _marked_scene(item)
            text = str(item)
        if marked is not None and marked > scene_number:
            later.append(text)
    return later


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


def _fact_definition_conflict(
    fact_id: str,
    existing: Dict[str, Any],
    proposed: Dict[str, Any],
    existing_chapter: int,
    proposed_chapter: int,
) -> PlanningContractError:
    """Build feedback that gives a repair model enough information to act."""
    canonical = json.dumps(
        {
            "id": fact_id,
            "fact": existing.get("fact", ""),
            "value": existing.get("value", ""),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    conflicting = json.dumps(
        {
            "id": fact_id,
            "fact": proposed.get("fact", ""),
            "value": proposed.get("value", ""),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return PlanningContractError(
        f"事实 {fact_id} 已在第 {existing_chapter} 章定义为 {canonical}；"
        f"第 {proposed_chapter} 章却在 facts_added 中再次定义为 {conflicting}。"
        "如果本章只是沿用同一事实，请从 facts_added 删除它并放入 facts_confirmed，"
        "沿用首次定义的 id、fact 和 value；如果确实是另一条事实，请使用新的 id",
        chapters=(proposed_chapter,),
        code="fact_definition_conflict",
    )


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
            action = str(raw.get("action") or raw.get("status") or "").lower()
            if action not in {"close", "closed"}:
                # An "extend" record is not a second raising of the thread; it
                # only restates when the payoff is now due.
                if state["opened_at"] is None and not raw.get("extend"):
                    state["opened_at"] = chapter
                if raw.get("deadline_chapter") not in (None, ""):
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
    contracts = load_planning_contracts(output_dir)
    later = [item for item in contracts if int(item["chapter"]) > chapter_number]

    # 只有「本章或更早引入」的东西才是本章的义务。少了这一层过滤，一本已经规划到
    # 第 23 章的书在补第 6 章时，会被要求埋下第 14 章才提出的悬念——一个永远无法
    # 满足的条件，于是中间的空洞再也补不回来。
    opened_at: Dict[str, int] = {}
    introduced_at: Dict[str, int] = {}
    for contract in contracts:
        chapter = int(contract["chapter"])
        for record in contract.get("plot_thread_updates", []):
            if (
                str(record.get("action") or record.get("status") or "").lower() != "open"
                or record.get("extend")
            ):
                continue
            opened_at.setdefault(str(record.get("id", "")), chapter)
        for record in contract.get("facts_added", []):
            introduced_at.setdefault(str(record.get("id", "")), chapter)

    def belongs_to_a_later_chapter(record_id: str, known: Dict[str, int]) -> bool:
        # 落盘的契约最准确；没有落盘时退回 ID 里编码的来源章（PT-014-01 → 14）。
        # 两者都问不出来时保留该项：它可能正是本章漏掉的那条，宁可多问一次。
        origin = known.get(record_id)
        if origin is None:
            origin = _source_chapter_from_id(record_id)
        return origin is not None and origin > chapter_number

    threads: Dict[str, Dict[str, Any]] = {}
    for contract in later:
        for record in contract.get("plot_thread_updates", []):
            if str(record.get("action") or record.get("status") or "").lower() not in {
                "close",
                "closed",
            }:
                continue
            if record.get("opened_in_chapter") is True:
                continue
            thread_id = str(record.get("id", ""))
            if belongs_to_a_later_chapter(thread_id, opened_at):
                continue
            threads.setdefault(
                thread_id,
                {
                    "id": thread_id,
                    "thread": record.get("thread", ""),
                    "closed_at": int(contract["chapter"]),
                },
            )
    facts: Dict[str, Dict[str, Any]] = {}
    for contract in later:
        for field in ("facts_confirmed", "facts_contradicted"):
            for record in contract.get(field, []):
                fact_id = str(record.get("id", ""))
                if belongs_to_a_later_chapter(fact_id, introduced_at):
                    continue
                facts.setdefault(
                    fact_id,
                    {
                        "id": fact_id,
                        "fact": record.get("fact", ""),
                        "used_at": int(contract["chapter"]),
                    },
                )
    return {"threads_closed_later": threads, "facts_used_later": facts}


def iter_history_defects(
    contract: Dict[str, Any],
    prior: Iterable[Dict[str, Any]],
    chapter_number: int,
    *,
    obligations: Dict[str, Any] | None = None,
) -> Iterator[PlanningContractError]:
    """Yield every neighbour-level defect in one chapter, best-first.

    Reporting one defect per attempt costs a retry per defect and lets the model
    reintroduce what it just fixed: it never sees the full list it has to satisfy
    at once. The rules live here; `validate_contract_against_history` below is
    just "raise the first thing this yields", matching how the cross-chapter
    gate is built.

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
    prior_facts: Dict[str, Tuple[int, Dict[str, Any]]] = {}
    for item in sorted(prior, key=lambda value: int(value.get("chapter", 0))):
        source_chapter = int(item.get("chapter", 0))
        for record in item.get("facts_added", []):
            fact_id = str(record.get("id", ""))
            if fact_id:
                prior_facts.setdefault(fact_id, (source_chapter, record))
    known_facts |= {
        str(record.get("id", "")) for record in contract.get("facts_added", [])
    }

    # 章节功能与时间读数只有放在邻章旁边才看得出问题：连着三章都是 advance，
    # 或者本章第一个事件回到上一章末尾之前，单看这一份契约都完全合法。
    for message in chapter_function_defects(contract, prior, chapter_number):
        yield PlanningContractError(
            message, chapters=(chapter_number,), code="chapter_function_monotony"
        )
    for finding in timeline_mesh_findings(contract, prior, chapter_number):
        if finding.get("severity") != "blocking":
            continue
        yield PlanningContractError(
            finding["message"], chapters=(chapter_number,), code=finding["code"]
        )

    for record in contract.get("facts_added", []):
        fact_id = str(record.get("id", ""))
        previous = prior_facts.get(fact_id)
        if previous is None:
            continue
        existing_chapter, existing = previous
        if (
            existing.get("fact") == record.get("fact")
            and existing.get("value") == record.get("value")
        ):
            continue
        yield _fact_definition_conflict(
            fact_id, existing, record, existing_chapter, chapter_number
        )

    for record in contract.get("plot_thread_updates", []):
        thread_id = str(record.get("id", ""))
        action = str(record.get("action") or record.get("status") or "").lower()
        if action not in {"close", "closed"}:
            if action in {"touch", "advance", "complicate", "cross"}:
                if thread_id not in open_ids:
                    yield PlanningContractError(
                        f"情节线 {_thread_label(thread_id, record)} 尚未打开，不能执行 {action}",
                        chapters=(chapter_number,),
                        code="thread_action_before_open",
                    )
                continue
            if thread_id in states and record.get("extend"):
                if thread_id not in open_ids:
                    yield PlanningContractError(
                        f"悬念 {_thread_label(thread_id, record, states[thread_id])}"
                        f"已在第 {states[thread_id]['closed_at']} 章了结，无法延期",
                        chapters=(chapter_number,),
                        code="thread_closed_twice",
                    )
                continue
            if thread_id in states:
                yield PlanningContractError(
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
            # 「已经了结过」和「从未埋下」互相排斥，只能报其中一条。
            yield PlanningContractError(
                f"悬念 {_thread_label(thread_id, record, states.get(thread_id, {}))} "
                f"已在第 {closed_at} 章了结，本章不能再了结一次",
                chapters=(chapter_number,),
                code="thread_closed_twice",
            )
            continue
        yield PlanningContractError(
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
        yield PlanningContractError(
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
                yield PlanningContractError(
                    f"第 {chapter_number} 章的 {field} 引用了尚未引入的事实 {fact_id}",
                    chapters=(chapter_number,),
                    code="unknown_fact_reference",
                )

    if not obligations:
        return
    declared_threads = {
        str(record.get("id", "")) for record in contract.get("plot_thread_updates", [])
    }
    declared_by_id = {
        str(record.get("id", "")): record
        for record in contract.get("plot_thread_updates", [])
    }
    for thread_id, info in obligations.get("threads_closed_later", {}).items():
        declared = declared_by_id.get(thread_id)
        if declared is None and thread_id not in states:
            yield PlanningContractError(
                f"第 {info['closed_at']} 章要了结悬念 {_thread_label(thread_id, info)}，"
                "本章必须保留埋下它的那条记录，不能删掉",
                chapters=(chapter_number,),
                code="thread_opening_dropped",
            )
        # Keeping the thread is not enough: a deadline that expires before the
        # payoff chapter is the same contradiction, just written down.
        if declared is None or str(declared.get("status", "")).lower() != "open":
            continue
        closed_at = int(info["closed_at"])
        try:
            deadline = int(declared.get("deadline_chapter"))
        except (TypeError, ValueError):
            continue
        if deadline < closed_at:
            yield PlanningContractError(
                f"悬念 {_thread_label(thread_id, declared, info)}要到第 {closed_at} 章才了结，"
                f"本章却把 deadline_chapter 写成第 {deadline} 章。"
                f"请改成 {closed_at}",
                chapters=(chapter_number,),
                code="deadline_before_payoff",
            )
    declared_facts = {
        str(record.get("id", "")) for record in contract.get("facts_added", [])
    }
    for fact_id, info in obligations.get("facts_used_later", {}).items():
        if fact_id in declared_facts or fact_id in known_facts:
            continue
        yield PlanningContractError(
            f"第 {info['used_at']} 章要引用事实 {fact_id}（{info.get('fact', '')}），"
            f"本章必须保留它的引入记录",
            chapters=(chapter_number,),
            code="fact_introduction_dropped",
        )


def validate_contract_against_history(
    contract: Dict[str, Any],
    prior: Iterable[Dict[str, Any]],
    chapter_number: int,
    *,
    obligations: Dict[str, Any] | None = None,
) -> None:
    """Raise on the first neighbour-level defect in one chapter."""
    for defect in iter_history_defects(
        contract, prior, chapter_number, obligations=obligations
    ):
        raise defect


def collect_history_defects(
    contract: Dict[str, Any],
    prior: Iterable[Dict[str, Any]],
    chapter_number: int,
    *,
    obligations: Dict[str, Any] | None = None,
    limit: int = 20,
) -> List[PlanningContractError]:
    """Every neighbour-level defect at once, so one retry can fix them all."""
    found: List[PlanningContractError] = []
    for defect in iter_history_defects(
        contract, prior, chapter_number, obligations=obligations
    ):
        found.append(defect)
        if len(found) >= limit:
            break
    return found


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
    known_facts: Dict[str, Tuple[int, Dict[str, Any]]] = {}
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
                previous[1].get("fact") != record.get("fact")
                or previous[1].get("value") != record.get("value")
            ):
                yield _fact_definition_conflict(
                    fact_id,
                    previous[1],
                    record,
                    previous[0],
                    chapter,
                )
                continue
            if previous is None:
                known_facts[fact_id] = (chapter, record)
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
            action = str(record.get("action") or record.get("status") or "").lower()
            if action not in {"close", "closed"}:
                if action in {"touch", "advance", "complicate", "cross"}:
                    if thread_id not in opened or thread_id in closed:
                        yield PlanningContractError(
                            f"第 {chapter} 章在情节线 {thread_id} 打开前执行 {action}",
                            chapters=(chapter,),
                            code="thread_action_before_open",
                        )
                    continue
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
