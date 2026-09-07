# -*- coding: utf-8 -*-
"""章节之间的衔接：契约字段、写作硬要求，以及不花调用的确定性检查。

连载小说读起来像一条线，靠的是每一章从上一章的状态接过来，而不是每章开头都重
新锚定一次时间、重新描写一遍地点、重新介绍一次在场的人。这个模块把「接上没有」
从读感变成可核对的东西，分三层：

1. **契约层**：`continuity` 是场景规划必须填的一块——承接什么、隔了多久、开场
   时人物各在哪。填不出来就说明这一章还没想清楚它从哪里开始，按契约不合格退回
   重规划。
2. **写作层**：第一场的提示词把这三项写成硬要求，并把上几章已经建立过的地点和
   已经介绍过的人物列出来，明确禁止重新铺陈。
3. **验收层**：能用统计判的就地判——相邻两章开头共用的套语片段、契约声明的开场
   人物是否真的出现在开头、时间读数有没有倒流、悬念是不是很多章没被碰过。判不
   了的（「读起来是不是接着上一章」）才留给读者盲测。

这里只做确定性判断，不发起任何模型调用。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


CONTINUITY_KEY = "continuity"

CHAPTER_FUNCTIONS = (
    "advance",
    "reveal",
    "relationship",
    "aftermath",
    "transition",
    "breather",
)

#: 「开头」取多少字参与套语比对。一场戏的开场铺陈通常在这个量级以内；取得太短
#: 会漏掉第二段才开始的重新布景，取得太长会把正常的剧情推进也算进来。
OPENING_CHARS = 1500

#: 判定套语复用的最短片段。五个汉字以上的连续重合已经不是巧合，而四个字以下会
#: 大量命中「他的手」「与此同时」这类无法避免的常用搭配。
MIN_FRAGMENT_CHARS = 5

#: 相邻两章开头允许的共用片段条数。超过这个数就不是偶合，是同一段开场被重写了
#: 一遍。第 18–21 章的实测值是每对 1–8 条，门槛定在这里既拦得住重铺，又不会因为
#: 一两句必要的回指就判死。
MAX_SHARED_OPENING_FRAGMENTS = 2

#: 单条片段长到这个程度就已经是一整句被原样搬过来，条数再少也不能放行。实测已
#: 验收章节之间最长的共用片段是 11 个字，20 字留出了充分余量。
MAX_SHARED_FRAGMENT_CHARS = 20

#: 一条悬念连续多少章没有出现在任何 plot_thread_updates 里就该被提醒。既有 30 章
#: 的实测最大间隔是 5 章，取 3 章能在它变成「读者已经忘了」之前叫住。
THREAD_NEGLECT_GAP = 3

#: 找「承接点」时，本章开头取多少字、上一章结尾取多少字，以及片段最短多长。
#: 承接通常发生在开篇几段之内，所以窗口比套语检查窄；片段门槛放到 4 个字，是因为
#: 这里重合是目的而不是毛病，宁可多找到几处也不要漏掉真正的回指。
HAND_OFF_OPENING_CHARS = 800
HAND_OFF_ENDING_CHARS = 2500
HAND_OFF_ANCHOR_CHARS = 4

_PLACEHOLDERS = frozenset(
    {
        "",
        "无",
        "无。",
        "暂无",
        "待定",
        "待补充",
        "未知",
        "不适用",
        "不详",
        "略",
        "空",
        "n/a",
        "na",
        "none",
        "null",
        "tbd",
        "-",
        "—",
        "/",
    }
)

_CJK = re.compile(r"[㐀-鿿]")
_HEADING = re.compile(r"^\s*#[^\n]*\n")


# --------------------------------------------------------------- 基础工具


def _text(value: Any) -> str:
    return str(value or "").strip()


def _is_placeholder(value: Any) -> bool:
    stripped = _text(value)
    return stripped.lower() in _PLACEHOLDERS


def _cjk_count(text: str) -> int:
    return len(_CJK.findall(text or ""))


def collapse(text: str) -> str:
    """去掉换行与空白，只留下读者实际读到的字符序列。

    中文正文里空白只出现在段落之间，去掉之后跨段落的重合片段也能被发现；引文能
    否逐字检索另行核对，不靠这一步保证。
    """
    return re.sub(r"\s+", "", text or "")


def opening_text(content: str, limit: int = OPENING_CHARS) -> str:
    """一章的开头：去掉标题行，压掉空白，取前 `limit` 个字符。"""
    body = _HEADING.sub("", content or "", count=1)
    return collapse(body)[:limit]


# --------------------------------------------------------------- 契约层


def continuity_schema_block(chapter_number: int) -> str:
    """写进场景规划提示词的 `continuity` 契约片段。"""
    return f"""  "{CONTINUITY_KEY}": {{
    "picks_up_from": "本章开场直接承接第 {chapter_number - 1} 章结尾的哪一个动作或悬念（写那件事本身，不要写“承接上一章”这种空话）",
    "time_gap": "与第 {chapter_number - 1} 章结尾之间隔了多久，例如“紧接上一章结尾”“约二十分钟后”“次日清晨”",
    "opening_positions": [{{"character":"人物规范名","location":"本章开场时他在哪"}}],
    "carried_tension": "开场时读者仍悬着、本章要继续加压的那件事；允许为空"
  }},"""


def continuity_instructions(
    chapter_number: int,
    *,
    previous_tail: str = "",
    established: Optional[Dict[str, List[str]]] = None,
    previous_last_event: Optional[Dict[str, Any]] = None,
    neglected: Sequence[Dict[str, Any]] = (),
) -> str:
    """规划第 N 章时，关于「从上一章什么状态接过来」要交代的全部上下文。"""
    if int(chapter_number) <= 1:
        return ""
    established = established or {}
    lines = [
        f"\n关于 {CONTINUITY_KEY}（第 {chapter_number} 章必填，三项都不能空着，也不能写“无”“待定”）：",
        "- picks_up_from 必须指向第 %d 章结尾确实发生过的那件事，而不是本章自己新起的头。"
        % (chapter_number - 1),
        "- time_gap 必须给出可读的时间差；无论隔了多久，读者都要能算出来。",
        "- opening_positions 至少一条，且这些人物必须真的出现在本章开头。",
    ]
    if previous_last_event:
        lines.append(
            "- 第 {chapter} 章最后一个记账事件是「{event}」，时间 {time}，地点 {location}。"
            "本章的第一个 timeline_events 时间不得早于它。".format(
                chapter=chapter_number - 1,
                event=_text(previous_last_event.get("event")) or "未命名",
                time=_text(previous_last_event.get("time")) or "未记",
                location=_text(previous_last_event.get("location_id")) or "未记",
            )
        )
    locations = established.get("locations") or []
    characters = established.get("characters") or []
    if locations:
        lines.append(
            "- 以下地点前面几章已经建立过，本章不要从零重新描写它的环境："
            + "、".join(locations[:12])
        )
    if characters:
        lines.append(
            "- 以下人物读者已经认识，本章不要重新交代他们的身份与来历："
            + "、".join(characters[:12])
        )
    if neglected:
        lines.append(
            "- 以下悬念已经连续几章没有被碰过，本章优先考虑推进或明确延期："
            + "；".join(
                f"{item['id']}（{item.get('thread', '')}，上次出现在第 {item['last_touched']} 章）"
                for item in neglected[:6]
            )
        )
    if previous_tail:
        lines.append(
            f"\n第 {chapter_number - 1} 章的结尾原文（只用来决定本章从哪里接起）：\n"
            + previous_tail[-2000:]
        )
    return "\n".join(lines)


def continuity_defects(contract: Dict[str, Any], chapter_number: int) -> List[str]:
    """契约的衔接字段哪里不合格；返回空列表表示可用。

    第一章没有上一章可接，不受此约束。
    """
    chapter_number = int(chapter_number)
    if chapter_number <= 1:
        return []
    block = contract.get(CONTINUITY_KEY)
    if not isinstance(block, dict) or not block:
        return [
            f"章节契约缺少 {CONTINUITY_KEY}：必须写明本章从第 {chapter_number - 1} 章的"
            "什么状态接过来（picks_up_from、time_gap、opening_positions）"
        ]

    defects: List[str] = []
    picks_up = _text(block.get("picks_up_from"))
    if _is_placeholder(picks_up) or _cjk_count(picks_up) < 8:
        defects.append(
            f"{CONTINUITY_KEY}.picks_up_from 必须具体说明本章承接第 {chapter_number - 1} 章"
            "结尾的哪一个动作或悬念，不能留空、写“无”，也不能只写一句“承接上一章”"
        )
    gap = _text(block.get("time_gap"))
    if _is_placeholder(gap) or _cjk_count(gap) + len(re.findall(r"\d", gap)) < 2:
        defects.append(
            f"{CONTINUITY_KEY}.time_gap 必须给出与第 {chapter_number - 1} 章结尾的时间差，"
            "例如“紧接上一章结尾”“约二十分钟后”“次日清晨”"
        )
    positions = block.get("opening_positions")
    if not isinstance(positions, list) or not positions:
        defects.append(
            f"{CONTINUITY_KEY}.opening_positions 必须至少写一条：本章开场时谁在哪"
        )
    else:
        for index, item in enumerate(positions, start=1):
            if not isinstance(item, dict):
                defects.append(
                    f"{CONTINUITY_KEY}.opening_positions 第 {index} 项必须是"
                    '{"character":"人物","location":"地点"} 形式的对象'
                )
                continue
            character = _text(item.get("character"))
            location = _text(item.get("location"))
            if _is_placeholder(character) or _is_placeholder(location):
                defects.append(
                    f"{CONTINUITY_KEY}.opening_positions 第 {index} 项的 character 与 "
                    "location 都必须填具体的人物名和地点，不能留空或写“无”"
                )
    return defects


def chapter_function_defects(
    contract: Dict[str, Any],
    prior_contracts: Iterable[Dict[str, Any]],
    chapter_number: int,
) -> List[str]:
    """本章的功能定位有没有填，以及是不是连着三章都在做同一件事。

    `chapter_function` 一路留空，就没人看得出连着五章都是「继续推进」；判据取
    「本章与紧邻的前两章完全相同」，那已经是读者能感到重复的长度。
    """
    chapter_number = int(chapter_number)
    function = _text(contract.get("chapter_function")).lower()
    if not function:
        return [
            "章节契约缺少 chapter_function：必须从 "
            + "|".join(CHAPTER_FUNCTIONS)
            + " 中选一个，说明本章在全书中承担什么功能"
        ]
    if function not in CHAPTER_FUNCTIONS:
        return [
            f"chapter_function「{function}」不在允许的取值内："
            + "|".join(CHAPTER_FUNCTIONS)
        ]

    by_chapter = {
        int(item.get("chapter", 0)): _text(item.get("chapter_function")).lower()
        for item in prior_contracts
    }
    previous = [by_chapter.get(chapter_number - 1), by_chapter.get(chapter_number - 2)]
    if previous and all(value == function for value in previous):
        return [
            f"第 {chapter_number - 2}、{chapter_number - 1} 章的 chapter_function 都是"
            f"「{function}」，本章又是同一个：连着三章承担同一种功能，读者会觉得"
            "情节在原地打转。请改成真正贴合本章的功能，或调整本章的场景安排"
        ]
    return []


# --------------------------------------------------------- 时间线跨章咬合

_DAY_TIME = re.compile(r"^(?P<day>.*?)\s*(?P<clock>\d{1,2}):(?P<minute>[0-5]\d)(?::[0-5]\d)?\s*$")


def parse_event_time(value: Any) -> Optional[Tuple[str, int]]:
    """把「案发第三日 05:45」拆成（日标签, 当日分钟数）。

    日标签是自由文本，不同章之间未必可排序；只有标签相同时两个读数才可比较。
    """
    match = _DAY_TIME.match(_text(value))
    if not match:
        return None
    hour = int(match.group("clock"))
    minute = int(match.group("minute"))
    if hour > 23:
        return None
    return _text(match.group("day")), hour * 60 + minute


def timeline_mesh_findings(
    contract: Dict[str, Any],
    prior_contracts: Iterable[Dict[str, Any]],
    chapter_number: int,
) -> List[Dict[str, Any]]:
    """相邻两章的时间读数对不对得上。

    日标签由模型自由书写（「案发第三日」「决战清晨」「决战当夜」），跨标签无法
    排序，所以只有两种情况能下确定的结论：同一个标签下时间倒流，以及本章用回了
    一个更早的章节才用过的旧标签。其余的标签漂移只作为提示报出来——那是记账口径
    的问题，不是这一章写错了。
    """
    chapter_number = int(chapter_number)
    prior = sorted(
        (item for item in prior_contracts if int(item.get("chapter", 0)) < chapter_number),
        key=lambda item: int(item.get("chapter", 0)),
    )
    if not prior:
        return []
    events = [
        event
        for event in contract.get("timeline_events", [])
        if isinstance(event, dict) and parse_event_time(event.get("time"))
    ]
    if not events:
        return []

    previous = prior[-1]
    previous_events = [
        event
        for event in previous.get("timeline_events", [])
        if isinstance(event, dict) and parse_event_time(event.get("time"))
    ]
    if not previous_events:
        return []

    findings: List[Dict[str, Any]] = []
    previous_day, previous_minutes = parse_event_time(previous_events[-1].get("time"))
    current_day, current_minutes = parse_event_time(events[0].get("time"))

    if current_day == previous_day and current_minutes < previous_minutes:
        findings.append(
            {
                "code": "timeline_regression",
                "severity": "blocking",
                "message": (
                    f"第 {previous['chapter']} 章最后一个事件在「{previous_day} "
                    f"{previous_minutes // 60:02d}:{previous_minutes % 60:02d}」，"
                    f"本章第一个事件却回到同一天的 "
                    f"{current_minutes // 60:02d}:{current_minutes % 60:02d}，时间倒流了"
                ),
            }
        )
        return findings

    if current_day != previous_day:
        last_used = {
            parse_event_time(event.get("time"))[0]: int(item.get("chapter", 0))
            for item in prior
            for event in item.get("timeline_events", [])
            if isinstance(event, dict) and parse_event_time(event.get("time"))
        }
        seen_at = last_used.get(current_day)
        if seen_at is not None and seen_at < int(previous.get("chapter", 0)):
            findings.append(
                {
                    "code": "timeline_label_backtrack",
                    "severity": "warning",
                    "message": (
                        f"本章的日标签「{current_day}」上一次出现是在第 {seen_at} 章，"
                        f"而第 {previous['chapter']} 章用的是「{previous_day}」；"
                        "两章的时间读数对不起来，账本无法判断本章到底发生在哪一天"
                    ),
                }
            )
        elif seen_at is None:
            findings.append(
                {
                    "code": "timeline_label_drift",
                    "severity": "warning",
                    "message": (
                        f"本章引入了新的日标签「{current_day}」，而第 "
                        f"{previous['chapter']} 章用的是「{previous_day}」；"
                        f"continuity.time_gap 写的是「"
                        f"{_text((contract.get(CONTINUITY_KEY) or {}).get('time_gap'))}」，"
                        "请确认两者说的是同一段时间差"
                    ),
                }
            )
    return findings


def neglected_threads(
    prior_contracts: Iterable[Dict[str, Any]],
    chapter_number: int,
    *,
    max_gap: int = THREAD_NEGLECT_GAP,
) -> List[Dict[str, Any]]:
    """已经埋下、还没了结，而且连续 `max_gap` 章没被碰过的悬念。"""
    from core.generation.planning_contract import thread_states

    chapter_number = int(chapter_number)
    prior = [
        item for item in prior_contracts if int(item.get("chapter", 0)) < chapter_number
    ]
    if not prior:
        return []
    last_touched: Dict[str, int] = {}
    for item in sorted(prior, key=lambda value: int(value.get("chapter", 0))):
        chapter = int(item.get("chapter", 0))
        for record in item.get("plot_thread_updates", []):
            thread_id = _text(record.get("id"))
            if thread_id:
                last_touched[thread_id] = chapter

    stale: List[Dict[str, Any]] = []
    for thread_id, state in sorted(thread_states(prior).items()):
        if state.get("opened_at") is None or state.get("closed_at") is not None:
            continue
        touched = last_touched.get(thread_id, state["opened_at"])
        if chapter_number - int(touched) > int(max_gap):
            stale.append(
                {
                    "id": thread_id,
                    "thread": state.get("thread", ""),
                    "last_touched": int(touched),
                    "chapters_silent": chapter_number - int(touched),
                }
            )
    stale.sort(key=lambda item: (-item["chapters_silent"], item["id"]))
    return stale


# --------------------------------------------------------------- 写作层


def established_context(
    prior_contracts: Iterable[Dict[str, Any]],
    chapter_number: int,
    *,
    lookback: int = 3,
) -> Dict[str, List[str]]:
    """最近 `lookback` 章里已经建立过的地点和已经露过面的人物。

    这两份名单是第一场写作提示词里「不要重新铺陈」的对象。只看最近几章：更早出
    现过一次的地点，隔了十章再描写一遍是必要的重新引入，不是重复布景。
    """
    chapter_number = int(chapter_number)
    window = [
        item
        for item in prior_contracts
        if chapter_number - int(lookback) <= int(item.get("chapter", 0)) < chapter_number
    ]
    locations: List[str] = []
    characters: List[str] = []
    for item in sorted(window, key=lambda value: int(value.get("chapter", 0))):
        for event in item.get("timeline_events", []):
            if not isinstance(event, dict):
                continue
            location = _text(event.get("location_id"))
            if location and location not in locations:
                locations.append(location)
        for update in item.get("character_updates", []):
            if not isinstance(update, dict):
                continue
            name = _text(update.get("character"))
            if name and name not in characters:
                characters.append(name)
        block = item.get(CONTINUITY_KEY)
        if isinstance(block, dict):
            for position in block.get("opening_positions", []) or []:
                if not isinstance(position, dict):
                    continue
                name = _text(position.get("character"))
                if name and name not in characters:
                    characters.append(name)
    return {"locations": locations, "characters": characters}


def scene_one_continuity_rules(
    contract: Dict[str, Any],
    established: Optional[Dict[str, List[str]]] = None,
) -> List[str]:
    """第一场写作提示词里的硬要求。

    只对第一场成立：中间场次接的是上一场，不是上一章，把同一批要求发过去只会让
    模型在第三场再写一次开场承接。
    """
    block = contract.get(CONTINUITY_KEY)
    if not isinstance(block, dict) or not block:
        return []
    established = established or {}
    rules: List[str] = []
    picks_up = _text(block.get("picks_up_from"))
    gap = _text(block.get("time_gap"))
    positions = [
        item for item in block.get("opening_positions", []) or [] if isinstance(item, dict)
    ]
    if picks_up:
        rules.append(
            f"本场开头必须直接承接上一章结尾的这件事：{picks_up}。"
            "开篇前三段之内就要让读者认出它，不要另起炉灶。"
        )
    if gap:
        rules.append(
            f"本场开场与上一章结尾相隔：{gap}。时间差要在正文里可感，"
            "但不要用一整段旁白重新报一遍时刻表。"
        )
    if positions:
        listed = "；".join(
            f"{_text(item.get('character'))} 在 {_text(item.get('location'))}"
            for item in positions
            if _text(item.get("character"))
        )
        if listed:
            rules.append(f"开场时人物各自的位置已经定好：{listed}。不得改动。")
    carried = _text(block.get("carried_tension"))
    if carried:
        rules.append(f"开场时读者仍悬着的是：{carried}。本场要继续加压，不要先把它解掉。")

    locations = (established.get("locations") or [])[:12]
    characters = (established.get("characters") or [])[:12]
    if locations:
        rules.append(
            "以下地点在前面几章刚建立过，读者已经知道它长什么样，禁止从零重新描写"
            "环境（渗水、灯光、管线、气味这类铺陈一律不要再来一遍）："
            + "、".join(locations)
            + "。需要提到时只写这一场新出现的变化。"
        )
    if characters:
        rules.append(
            "以下人物读者已经认识，禁止重新交代他们的身份、职务、来历或与主角的关系："
            + "、".join(characters)
            + "。直接用名字行动即可。"
        )
    rules.append(
        "不要用「与此同时」「另一边」这类空转过场重新铺场；开头第一句就应当是"
        "承接上一章的具体动作、对话或后果。"
    )
    return rules


# --------------------------------------------------------------- 验收层


def shared_fragments(
    previous_opening: str,
    opening: str,
    *,
    min_len: int = MIN_FRAGMENT_CHARS,
    protected: Sequence[str] = (),
) -> List[str]:
    """两段开头之间共用的连续片段，长到不可能是巧合的那些。

    专有名词被排除在外：两章都提到「白鹤山看守所」不是套语，而是同一个地方。做法
    是把已知的人物名与地点名从片段里抠掉，剩下的字不足三个就当作纯专名丢弃。
    """
    if not previous_opening or not opening:
        return []
    seeds = {
        previous_opening[index : index + min_len]
        for index in range(len(previous_opening) - min_len + 1)
    }
    found: List[str] = []
    index = 0
    while index <= len(opening) - min_len:
        if opening[index : index + min_len] in seeds:
            end = index + min_len
            while end < len(opening) and opening[index : end + 1] in previous_opening:
                end += 1
            fragment = opening[index:end]
            if fragment not in found:
                found.append(fragment)
            index = end
        else:
            index += 1

    ordered_protected = sorted(
        {_text(name) for name in protected if _cjk_count(_text(name)) >= 2},
        key=len,
        reverse=True,
    )
    kept: List[str] = []
    for fragment in found:
        if _cjk_count(fragment) < min_len:
            continue
        residue = fragment
        for name in ordered_protected:
            residue = residue.replace(name, "")
        if _cjk_count(residue) < 3:
            continue
        kept.append(fragment)
    return kept


def protected_terms(
    suspense_ledger: Optional[Dict[str, Any]] = None,
    contracts: Iterable[Dict[str, Any]] = (),
) -> List[str]:
    """套语比对时要排除的专有名词：人物规范名与地点名。"""
    names: List[str] = []

    def add(value: Any) -> None:
        text = _text(value)
        if text and text not in names:
            names.append(text)

    for record in (suspense_ledger or {}).get("character_updates", []) or []:
        if isinstance(record, dict):
            add(record.get("character"))
    for record in (suspense_ledger or {}).get("timeline_events", []) or []:
        if isinstance(record, dict):
            location = _text(record.get("location_id"))
            for part in re.split(r"[·•・,，、/]", location):
                add(part)
            add(location)
    for contract in contracts:
        for record in contract.get("character_updates", []) or []:
            if isinstance(record, dict):
                add(record.get("character"))
        for record in contract.get("timeline_events", []) or []:
            if isinstance(record, dict):
                location = _text(record.get("location_id"))
                for part in re.split(r"[·•・,，、/]", location):
                    add(part)
                add(location)
    return names


def opening_boilerplate_finding(
    chapter_content: str,
    previous_chapter_content: str,
    *,
    protected: Sequence[str] = (),
    limit: int = MAX_SHARED_OPENING_FRAGMENTS,
) -> Dict[str, Any]:
    """本章开头与上一章开头共用了多少套语，以及能不能据此判失败。

    返回的 `quote` 一定是正文里逐字可检索的片段；找不到可检索的引文就只报数字，
    不升级成硬失败——本项目里所有硬失败都要求作者能拿引文回正文定位。
    """
    fragments = shared_fragments(
        opening_text(previous_chapter_content),
        opening_text(chapter_content),
        protected=protected,
    )
    quotable = [
        fragment for fragment in fragments if fragment and fragment in (chapter_content or "")
    ]
    quotable.sort(key=len, reverse=True)
    longest = max((len(fragment) for fragment in fragments), default=0)
    return {
        "fragments": fragments,
        "count": len(fragments),
        "shared_chars": sum(len(fragment) for fragment in fragments),
        "longest_fragment_chars": longest,
        "quotable": quotable,
        "quote": quotable[0] if quotable else "",
        # 两种越界都算：碎片多说明整段开场被重铺了一遍，单条特别长说明有一整句
        # 被原样搬了过来——后者条数上根本不会超标，只看条数会漏掉最露骨的那种。
        "exceeded": (
            len(fragments) > int(limit) or longest >= MAX_SHARED_FRAGMENT_CHARS
        ),
        "limit": int(limit),
    }


def hand_off_anchor_finding(
    chapter_content: str,
    previous_chapter_content: str,
    *,
    protected: Sequence[str] = (),
    opening_window: int = HAND_OFF_OPENING_CHARS,
    ending_window: int = HAND_OFF_ENDING_CHARS,
) -> Dict[str, Any]:
    """本章开头有没有一处字面回指上一章的结尾。

    这是「开头能不能在上一章结尾里指出承接点」的可自动核对版本。它与套语检查方向
    相反、判据也相反：和上一章**开头**重合是重新布景（要拦），和上一章**结尾**
    重合是承接（要有）。片段门槛放到 4 个字，因为这里重合是目的而不是毛病。

    找不到锚点不等于没接上——同一件事可以完全换一套说法。所以它只报数字与提示，
    不作硬失败；真正判「读起来接不接得上」的是读者盲测。
    """
    ending = collapse(previous_chapter_content or "")[-int(ending_window) :]
    opening = opening_text(chapter_content, opening_window)
    anchors = shared_fragments(
        ending, opening, min_len=HAND_OFF_ANCHOR_CHARS, protected=protected
    )
    anchors.sort(key=len, reverse=True)
    return {
        "anchors": anchors,
        "count": len(anchors),
        "longest": anchors[0] if anchors else "",
    }


def opening_position_findings(
    chapter_content: str,
    contract: Dict[str, Any],
    *,
    window: int = OPENING_CHARS,
) -> List[Dict[str, Any]]:
    """契约声明开场时在场的人，正文开头是不是真的写到了。

    这一条把「上一章结尾谁在哪、下一章开头谁在哪」里可确定性核对的那一半接上：
    契约声明的开场站位如果在正文里根本找不到人，那份站位就只是纸面上的。
    """
    block = contract.get(CONTINUITY_KEY)
    if not isinstance(block, dict):
        return []
    opening = opening_text(chapter_content, window)
    if not opening:
        return []
    anchor = _first_quotable_sentence(chapter_content)
    findings: List[Dict[str, Any]] = []
    for position in block.get("opening_positions", []) or []:
        if not isinstance(position, dict):
            continue
        character = _text(position.get("character"))
        location = _text(position.get("location"))
        if not character or character in opening:
            continue
        findings.append(
            {
                "code": "OPENING_POSITION_UNMET",
                "quote": anchor,
                "problem": (
                    f"契约声明本章开场时{character}在{location or '指定地点'}，"
                    f"但开头 {window} 字里没有出现{character}；"
                    "读者读到的开场和账本记的开场不是同一个"
                ),
                "change": (
                    f"在开头让{character}以具体动作出场，"
                    f"或把 continuity.opening_positions 改成正文真正写到的人"
                ),
            }
        )
    return findings


_SENTENCE_END = re.compile(r"[。！？!?]")


def _first_quotable_sentence(chapter_content: str, minimum: int = 8, maximum: int = 40) -> str:
    """正文开头第一句完整的话，用作缺席类问题的可检索锚点。"""
    body = _HEADING.sub("", chapter_content or "", count=1).lstrip()
    for line in body.splitlines():
        line = line.strip()
        if _cjk_count(line) < minimum:
            continue
        match = _SENTENCE_END.search(line)
        sentence = line[: match.end()] if match else line
        if _cjk_count(sentence) < minimum:
            continue
        return sentence[:maximum]
    return ""


def continuity_gate(
    chapter_content: str,
    previous_chapter_content: str,
    contract: Dict[str, Any],
    chapter_number: int,
    *,
    protected: Sequence[str] = (),
    prior_contracts: Iterable[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    """一整章的确定性衔接检查结果，不发起任何调用。

    输出与领域评审同构（hard_failures / repair_scope / metrics），这样它可以直接
    作为章节级闸门的第四份「评审」并入合议，重修路由也不必为它另开一条路。
    """
    chapter_number = int(chapter_number)
    prior_contracts = list(prior_contracts)
    metrics: Dict[str, Any] = {
        "chapter": chapter_number,
        "shared_opening_fragments": 0,
        "shared_opening_fragment_list": [],
        "declared_opening_characters": 0,
        "unmet_opening_positions": 0,
    }
    hard_failures: List[Dict[str, Any]] = []
    warnings: List[str] = []

    if chapter_number > 1 and previous_chapter_content:
        boilerplate = opening_boilerplate_finding(
            chapter_content, previous_chapter_content, protected=protected
        )
        metrics["shared_opening_fragments"] = boilerplate["count"]
        metrics["shared_opening_chars"] = boilerplate["shared_chars"]
        metrics["longest_shared_fragment_chars"] = boilerplate["longest_fragment_chars"]
        metrics["shared_opening_fragment_list"] = boilerplate["fragments"]
        if boilerplate["exceeded"]:
            listed = "、".join(f"「{item}」" for item in boilerplate["fragments"][:6])
            if boilerplate["quote"]:
                hard_failures.append(
                    {
                        "code": "OPENING_BOILERPLATE_REUSE",
                        "quote": boilerplate["quote"],
                        "problem": (
                            f"本章开头与第 {chapter_number - 1} 章开头逐字共用了 "
                            f"{boilerplate['count']} 处片段、共 "
                            f"{boilerplate['shared_chars']} 个字（{listed}），"
                            "读者会觉得这一章又把上一章的开场重写了一遍"
                        ),
                        "change": (
                            "把开头改写成承接上一章结尾的具体动作或后果，"
                            "删掉与上一章开头重复的环境铺陈用语"
                        ),
                    }
                )
            else:
                warnings.append(
                    f"本章开头与上一章开头共用 {boilerplate['count']} 处片段"
                    f"（{listed}），但没有可逐字检索的引文，只作提示"
                )

        anchor = hand_off_anchor_finding(
            chapter_content, previous_chapter_content, protected=protected
        )
        metrics["hand_off_anchors"] = anchor["count"]
        metrics["hand_off_anchor_list"] = anchor["anchors"][:8]
        if not anchor["count"]:
            warnings.append(
                f"本章开头与第 {chapter_number - 1} 章结尾没有任何字面上的回指，"
                "请确认开头确实接着上一章那件事，而不是另起炉灶"
            )

    positions = opening_position_findings(chapter_content, contract)
    block = contract.get(CONTINUITY_KEY)
    if isinstance(block, dict):
        metrics["declared_opening_characters"] = len(
            [item for item in block.get("opening_positions", []) or [] if isinstance(item, dict)]
        )
    metrics["unmet_opening_positions"] = len(positions)
    hard_failures.extend(item for item in positions if item.get("quote"))
    warnings.extend(
        item["problem"] for item in positions if not item.get("quote")
    )

    # 时间读数的问题在规划阶段就是阻断项（那时改契约还便宜）。走到这里说明它是
    # 从旧契约带进来的：正文里没有一句可以逐字引用的话对应它，而本项目所有硬失败
    # 都要求作者能拿引文回正文定位，所以这里只报，不拦。
    for finding in timeline_mesh_findings(contract, prior_contracts, chapter_number):
        warnings.append(finding["message"])
        metrics.setdefault("timeline_findings", []).append(finding)

    stale = neglected_threads(prior_contracts, chapter_number)
    if stale:
        metrics["neglected_threads"] = stale
        warnings.append(
            "以下悬念已连续多章没有被推进："
            + "；".join(
                f"{item['id']}（第 {item['last_touched']} 章之后再没出现）"
                for item in stale[:5]
            )
        )

    return {
        "stage": "continuity",
        "passed": not hard_failures,
        "hard_failures": hard_failures,
        "warnings": warnings,
        "repair_scope": "scene_1" if hard_failures else "",
        "metrics": metrics,
    }
