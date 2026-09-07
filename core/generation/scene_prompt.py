"""场景写作 prompt 的唯一构造点。

GUI 阶段管线和智能体（agents/writing/chapter_writing_agent.py）
此前各自拼装了一份几乎相同、但已经开始漂移的场景 prompt。两条路径现在都调用
这里，领域专属的写作约束由 `DomainProfile.scene_writing_rules` 注入。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from core.generation.domain_profiles import DomainProfile
from core.generation.helper_fns import scene_heading
from core.generation.planning_contract import contract_for_scene, later_scene_boundaries
from core.generation.prompt_context import (
    CHINESE_PROSE_REQUIREMENTS,
    build_location_guidance,
    build_story_parameter_lines,
    format_genre_label,
    normalize_story_parameters,
)
from core.generation.story_ledger import compact_json
from core.localization import zh_label


# 与题材无关的场景写作要求。领域专属要求来自 profile.scene_writing_rules。
BASE_SCENE_REQUIREMENTS = (
    "写出有吸引力、富有描写性的场景正文。",
    "根据人物名单中的设定，写出人物行动、对白（如适合本场景）、思想和情绪。",
    "清楚交代场景环境。",
    "场景应衔接合理，并按描述推动情节或人物发展。",
    "所有世界构建细节必须严格遵守所提供的世界观。",
)

CLOSING_REQUIREMENTS = (
    "只提供本场景正文，不要附加评论、场景编号或标题，最终组装由程序处理。",
    "不要使用代码围栏。",
)


def scene_prompt_filename(scene_number: int, chapter_number: Optional[int] = None) -> str:
    """Prompt 存档文件名（沿用既有命名，避免旧项目的存档路径改变）。"""
    if chapter_number is None:
        return f"short_story_scene_{scene_number}_prompt"
    return f"write_chapter_{chapter_number}_scene_{scene_number}_prompt"


def build_scene_prompt(
    *,
    scene_plan: str,
    scene_number: int,
    parameters: Optional[Dict[str, Any]],
    lore: str,
    character_roster: str,
    faction_summary: str,
    profile: DomainProfile,
    chapter_number: Optional[int] = None,
    structure_name: str = "",
    section_name: str = "",
    novel_title: str = "",
    contract: Optional[Dict[str, Any]] = None,
    previous_scene_tail: str = "",
    next_scene_plan: str = "",
    continuity_rules: Sequence[str] = (),
) -> str:
    """构造一个场景的写作 prompt。

    `chapter_number` 为 None 表示短篇小说（整篇作为单章处理）。

    `continuity_rules` 只在本章第一场传入：它说的是「从上一章的什么状态接过来」，
    第二场往后接的是上一场，把同一批要求再发一遍只会让模型在半途又写一次开场。
    """
    params = normalize_story_parameters(parameters)
    genre_label = format_genre_label(params)
    story_length = params.get("Story Length", "")
    structure_name = structure_name or params.get("Story Structure", "")

    lines = list(_header_lines(genre_label, story_length, structure_name, section_name, chapter_number, novel_title))
    lines.extend(build_story_parameter_lines(params))
    lines.extend(
        [
            "- 类型、主题和基调不得被世界观资料中的其他类型元素覆盖。",
            "- 事实冲突时依次以作品参数、整体世界观、当前场景规划为准；场景规划中的冲突内容必须静默纠正。",
            "- 不要混合互相冲突的地点、时代或机构，也不要把名称相近的设定擅自视为同一对象。",
            "\n## 整体故事背景（高于场景规划）：",
        ]
    )
    if novel_title:
        lines.append(f"标题：{novel_title}")
    lines.extend(
        [
            f"完整世界观：{lore or '无可用内容'}",
            f"\n{character_roster or '没有可用的人物信息'}",
            f"\n{faction_summary or '没有可用的势力信息'}",
            f"\n## 当前场景描述（{_scene_locator(chapter_number, scene_number)}）：",
            scene_plan,
        ]
    )
    lines.extend(
        _continuity_lines(
            # 契约整份写着全章每一场分别要做什么，而抬头是「必须兑现」——写第一场
            # 的模型会照着把后面几场也兑现了。所以后面场次不放在这一份里，改用
            # 「禁止触碰」的名义单列。闸门用的仍是完整契约。
            contract_for_scene(contract, scene_number),
            previous_scene_tail,
            next_scene_plan,
            continuity_rules,
            later_scene_boundaries(contract, scene_number),
        )
    )
    lines.append("若当前场景描述与作品参数或世界观冲突，必须以作品参数和世界观为准并静默纠正。")

    lines.append("\n## 本场景写作要求：")
    requirements = [
        *BASE_SCENE_REQUIREMENTS,
        *profile.scene_writing_rules,
        *build_location_guidance(params),
        *CHINESE_PROSE_REQUIREMENTS,
        *CLOSING_REQUIREMENTS,
    ]
    lines.extend(f" - {line}" for line in requirements)
    return "\n".join(lines)


def _header_lines(
    genre_label: str,
    story_length: str,
    structure_name: str,
    section_name: str,
    chapter_number: Optional[int],
    novel_title: str,
):
    if chapter_number is None:
        titled = f"《{novel_title}》" if novel_title else ""
        yield f"请撰写{genre_label}短篇小说{titled}中的一个场景。"
        if structure_name:
            yield f"故事采用“{zh_label(structure_name)}”框架。"
        yield "下面会提供短篇小说中单个场景的详细规划，请只写这个场景的完整正文。"
        yield "不要写其他场景，也不要概括整个故事。"
        return

    length_label = zh_label(story_length) if story_length else ""
    yield f"请撰写{genre_label}{length_label}第 {chapter_number} 章中的一个场景。"
    if structure_name and section_name:
        yield (
            f"故事采用“{zh_label(structure_name)}”框架，"
            f"当前位于“{zh_label(section_name)}”部分。"
        )
    elif structure_name:
        yield f"故事采用“{zh_label(structure_name)}”框架。"
    yield f"下面会提供第 {chapter_number} 章内单个场景的详细规划，请只写这个场景的完整正文。"
    yield "不要写其他场景，也不要概括本章。"


def _scene_locator(chapter_number: Optional[int], scene_number: int) -> str:
    if chapter_number is None:
        return f"场景 {scene_number}"
    return f"第 {chapter_number} 章，场景 {scene_number}"


def _continuity_lines(
    contract: Optional[Dict[str, Any]],
    previous_scene_tail: str,
    next_scene_plan: str,
    continuity_rules: Sequence[str] = (),
    later_boundaries: Sequence[str] = (),
):
    if contract:
        # 标题要说清这一份是截到本场为止的：不然模型会把「后面几场的内容不在这里」
        # 读成「本章没有别的事要发生」，反过来又去抢写。
        yield (
            "\n## 本章质量契约（只列到本场为止；后面几场要做什么另见下方禁止触碰的"
            "清单。列出的必须兑现，不得擅自增加真相）："
        )
        yield compact_json(contract, max_chars=10000)
    if previous_scene_tail:
        yield "\n## 上一场或上一章的已验收结尾（从这一状态续写，不得重演已完成动作）："
        yield previous_scene_tail
    if next_scene_plan:
        # 只给标题，不给下一场的环境、人物和事件表：把整段发过去等于先递材料、
        # 再请模型别用，而它会用。标题足够说明本场该停在哪儿。
        yield "\n## 本场必须在下一场开始之前收束。下一场是（只用来定收束位置，其中的事件一个都不许提前写出来）："
        yield scene_heading(next_scene_plan)
    later = [str(item).strip() for item in later_boundaries if str(item).strip()]
    if later:
        # 这几行必须让模型看见——它靠这些才知道自己这一场到哪儿为止。实测把它们
        # 一并删掉之后越界反而更严重：第一场一口气把后面两场都写完了。要紧的是
        # 名义：躺在「必须兑现」里它会去兑现，写成禁令才是栅栏。
        yield (
            "\n## 以下内容属于本章后面的场次，本场一个字都不许碰"
            "（列在这里只为让你知道自己这一场到哪儿为止）："
        )
        yield from (f" - {item}" for item in later)
    rules = [str(rule).strip() for rule in continuity_rules if str(rule).strip()]
    if rules:
        yield (
            "\n## 本章开场的硬性衔接要求（本场是本章第一场，下面每一条都必须做到，"
            "做不到就是这一场没写对）："
        )
        yield from (f" - {rule}" for rule in rules)
