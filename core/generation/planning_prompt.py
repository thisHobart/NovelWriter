# -*- coding: utf-8 -*-
"""「场景规划」页两个按钮发给模型的提示词：只拼文字，不调用模型，也不写文件。

「生成章节大纲」每一幕一份，「规划场景」每章一份（短篇整篇一份）。正式流程
（``scene_pipeline``）和不花调用的规划检查（``tools/check_planning.py``）用的都是这里的
函数，检查脚本量到的就是模型会收到的。

每份提示词是一段文字，另带 ``blocks``：按先后排的（中文名，原文），原文首尾相接就是整段
提示词。检查脚本按块量字数、找截断。

正式流程在拼「规划场景」的契约格式说明之前，先把账本和叙事图文件建好、旧字段迁走
（``ScenePipeline._contract_instructions``）；这里只读。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple

from core.generation.chapter_continuity import established_context, neglected_threads
from core.generation.domain_profiles import resolve_domain_profile
from core.generation.helper_fns import (
    CHAPTER_HEADING_PATTERN,
    open_file,
    parse_chapter_numbers,
    resolve_section_chapter_numbers,
)
from core.generation.lore_repository import BODY, lore_context, lore_files, name_of
from core.generation.narrative_graph import NarrativeGraphManager
from core.generation.planning_checks import listed_names
from core.generation.planning_contract import (
    MATERIAL_LABELS,
    _source_chapter_from_id,
    build_existing_planning_index,
    contract_instruction_blocks,
    downstream_obligations,
    established_records_text,
    load_planning_contracts,
)
from core.generation.prompt_context import build_story_parameter_lines, sanitize_lore_content
from core.generation.prompt_material import (
    OMITTED_HEADING,
    MaterialBlock,
    fit_blocks,
    render_blocks,
)
from core.generation.scene_material import (
    card_text,
    mentioned,
    pick_characters,
    pick_factions,
    registry_text,
    setting_records,
    setting_terms,
)
from core.generation.story_ledger import StoryLedgerManager
from core.localization import zh_label
from core.lore_localization import display_value

#: 短篇的场景数上下限。短篇整篇是作为**一个章节**送进质量闸门的，而那套闸门的
#: 重修预算与分数门槛都是在 2-3 场的章节上实测定下来的。实测一篇 10 场的短篇在
#: 三轮评审里分数一路走低（2.52 → 2.43 → 1.91），30 分钟后判不通过、整篇作废。
#: 这个上限让短篇的形状落回闸门能处理的范围。
SHORT_STORY_MIN_SCENES = 3
SHORT_STORY_MAX_SCENES = 6

#: 作品没有世界观时，「规划场景」里世界观那一块写这句。
MISSING_LORE = "缺少整体世界观背景。"

#: 「规划场景」每章材料的总字数上限（不含要求和契约格式）。和写一场戏的设定材料一样。
PLANNING_BUDGET = 30000

#: 「生成章节大纲」每一幕的设定材料（登记表、人物和势力名单、上一幕最后一章）的字数上限。
OUTLINE_BUDGET = 12000

Block = Tuple[str, str]


class PlanningPrompt(str):
    """一份提示词（就是一段文字），另带按先后排的（中文名，原文）。

    「规划场景」长篇每章的提示词另带材料的取舍：kept、omitted 是带上和去掉的块名，
    characters 是带了卡片的人物。
    """

    blocks: Tuple[Block, ...] = ()
    kept: Tuple[str, ...] = ()
    omitted: Tuple[str, ...] = ()
    characters: Tuple[str, ...] = ()


def _prompt(blocks: Sequence[Block]) -> PlanningPrompt:
    prompt = PlanningPrompt("".join(text for _label, text in blocks))
    prompt.blocks = tuple(blocks)
    return prompt


def _joined(groups: Sequence[Tuple[str, Sequence[str]]]) -> List[Block]:
    """按行拼的提示词分成几块：块内各行用换行连起来，块与块之间也是一个换行。

    一行都没有的块不出现，这样拼出来的文字和把所有行直接用换行连起来完全一样。
    """
    present = [(label, "\n".join(lines)) for label, lines in groups if lines]
    return [
        (label, text + ("\n" if index < len(present) - 1 else ""))
        for index, (label, text) in enumerate(present)
    ]


def _with_contract(blocks: List[Block], contract_text: str) -> List[Block]:
    """契约格式说明排在最后；它自己也分了块，就把最后一块展开成那几块。"""
    inner = list(getattr(contract_text, "blocks", ()) or ())
    if (
        blocks
        and blocks[-1][0] == "契约格式说明"
        and inner
        and "".join(part for _name, part in inner) == blocks[-1][1]
    ):
        return blocks[:-1] + inner
    return blocks


def _stripped(blocks: Sequence[Block]) -> List[Block]:
    """整段去掉首尾空白，去掉的部分落在哪几块就从哪几块里扣。"""
    full = "".join(text for _label, text in blocks)
    lead = len(full) - len(full.lstrip())
    trail = len(full) - len(full.rstrip())
    result = [[label, text] for label, text in blocks]
    for item in result:
        cut = min(lead, len(item[1]))
        item[1], lead = item[1][cut:], lead - cut
    for item in reversed(result):
        cut = min(trail, len(item[1]))
        item[1], trail = item[1][: len(item[1]) - cut], trail - cut
    return [(label, text) for label, text in result]


# ---------------------------------------------------------------- 文件在哪


def slug(name: str) -> str:
    """故事结构和幕名在文件名里的写法。"""
    return name.lower().replace(" ", "_").replace(":", "").replace("/", "_")


@dataclass(frozen=True)
class ActFiles:
    """一幕的几份文件：故事结构页写的详细规划、这一幕的章节大纲、逐章场景规划。"""

    section: str
    structure_file: str
    outline_file: str
    scene_plans_dir: str
    structure_slug: str
    section_slug: str

    def scene_plan_file(self, chapter_number: int) -> str:
        return os.path.join(
            self.scene_plans_dir,
            f"scenes_{self.structure_slug}_{self.section_slug}_ch{chapter_number}.md",
        )


def act_files(
    output_dir: str, structure_name: str, section_name: str, chapter_outlines_dir: str,
    scene_plans_dir: str,
) -> ActFiles:
    structure_slug = structure_name.lower().replace(" ", "_")
    section_slug = slug(section_name)
    return ActFiles(
        section=section_name,
        structure_file=os.path.join(
            output_dir, "story", "structure", f"{structure_slug}_{section_slug}.md"
        ),
        outline_file=os.path.join(
            chapter_outlines_dir, f"chapter_outlines_{structure_slug}_{section_slug}.md"
        ),
        scene_plans_dir=scene_plans_dir,
        structure_slug=structure_slug,
        section_slug=section_slug,
    )


def planning_lore(output_dir: str, *, short_story: bool = False) -> str | None:
    """「规划场景」带的世界观：整理好的世界设定优先，没有就用世界观正文。

    两样都没有时返回 None，由调用方提示作者并改用 ``MISSING_LORE``。短篇只在世界观正文
    文件存在时才带。
    """
    path = os.path.join(output_dir, "story", "lore", "generated_lore.md")
    if short_story and not os.path.exists(path):
        return None
    try:
        return lore_context(output_dir) or sanitize_lore_content(open_file(path))
    except FileNotFoundError:
        return None


def chapter_parts(outline: str) -> List[Tuple[int, str]]:
    """一幕大纲按章切开：[(大纲里写的章号, 这一章那一段)]，按先后；同一章写了两次的合在一起。

    章标题的认法和 ``parse_chapter_numbers`` 一样，所以切出来的段数和它认出的章数一样。
    """
    text = outline or ""
    matches = list(CHAPTER_HEADING_PATTERN.finditer(text))
    parts: Dict[int, List[str]] = {}
    for index, match in enumerate(matches):
        number = int(match.group("en") or match.group("zh"))
        if number <= 0:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        parts.setdefault(number, []).append(text[match.start():end].strip())
    return [(number, "\n\n".join(chunks)) for number, chunks in parts.items()]


def book_outlines(
    output_dir: str, structure_name: str, sections: Sequence[str], chapter_outlines_dir: str
) -> Dict[int, str]:
    """全书每章那一段大纲：{章号: 文字}。章号的认法和「规划场景」逐幕认的一样。

    大纲里的章号和前面几幕撞了、或者不是递增的，「规划场景」会按顺序改号；这里按同样
    的先后把第几段配给第几章。
    """
    outlines: Dict[int, str] = {}
    next_expected, claimed = 1, set()
    for section in sections:
        files = act_files(output_dir, structure_name, section, chapter_outlines_dir, "")
        try:
            with open(files.outline_file, "r", encoding="utf-8") as handle:
                outline = handle.read().strip()
        except (OSError, UnicodeError):
            continue
        if not outline:
            continue
        numbers, _warning = resolve_section_chapter_numbers(
            parse_chapter_numbers(outline), next_expected, claimed
        )
        if not numbers:
            continue
        claimed.update(numbers)
        next_expected = max(next_expected, max(numbers) + 1)
        for number, (_written, text) in zip(numbers, chapter_parts(outline)):
            outlines[number] = text
    return outlines


# ---------------------------------------------------------------- 生成章节大纲


def previous_act_ending(
    output_dir: str,
    structure_name: str,
    sections: Sequence[str],
    section_name: str,
    chapter_outlines_dir: str,
) -> str:
    """这一幕前面最近一幕已有大纲的最后一章那一段；第一幕或前面都没有大纲时是空文字。"""
    sections = list(sections)
    if section_name not in sections:
        return ""
    for earlier in reversed(sections[: sections.index(section_name)]):
        files = act_files(output_dir, structure_name, earlier, chapter_outlines_dir, "")
        try:
            with open(files.outline_file, "r", encoding="utf-8") as handle:
                parts = chapter_parts(handle.read())
        except (OSError, UnicodeError):
            continue
        if parts:
            return parts[-1][1]
    return ""


def cast_roster(cast: Sequence[Dict[str, Any]], faction_cards: Sequence[Dict[str, Any]]) -> str:
    """人物和势力名单：每人一行名字和身份（角色定位、职业、所属势力），每个势力一行
    类型和活动地点。目标、成长方向这些不带：大纲只需要知道有谁、是什么人。

    卡片里的称号（title）不带：生成卡片时它常和职业对不上（辩护律师配“地方检察官”），
    放进大纲就成了自相矛盾的身份。"""

    def shown(value: Any) -> str:
        if isinstance(value, str):
            return display_value(value).strip()
        if isinstance(value, (list, tuple)):
            return "、".join(item for item in (shown(part) for part in value) if item)
        return ""

    lines: List[str] = []
    if cast:
        lines.append("人物：")
        for card in cast:
            parts: List[str] = []
            for key in ("role", "profession", "occupation"):
                text = shown(card.get(key))
                if text and text not in parts:
                    parts.append(text)
            faction = shown(card.get("faction"))
            if faction and not faction.startswith("无"):
                parts.append(f"所属：{faction}")
            lines.append(f"- {name_of(card)}：{'；'.join(parts)}" if parts else f"- {name_of(card)}")
    if faction_cards:
        lines.append("势力：")
        for card in faction_cards:
            parts = [text for text in (shown(card.get("type")),) if text]
            territory = shown(card.get("territory")) or shown(card.get("territories"))
            if territory:
                parts.append(f"活动地点：{territory}")
            lines.append(f"- {name_of(card)}：{'；'.join(parts)}" if parts else f"- {name_of(card)}")
    return "\n".join(lines)


def outline_material(
    output_dir: str, previous_ending: str = "", *, max_chars: int = OUTLINE_BUDGET
) -> PlanningMaterial:
    """「生成章节大纲」一幕的设定材料：名称与规则登记表（必须遵守，总是带）、人物和势力
    名单（必须遵守）、上一幕最后一章的大纲（参考）。超出篇幅时从最后一块起整块去掉。

    作品没有设定文件时只有最后一块。
    """
    files = dict(lore_files(output_dir))
    registry, cast, faction_cards = setting_records(files)
    blocks: List[MaterialBlock] = []
    if files:
        blocks.append(MaterialBlock("registry", "名称与规则登记表", registry_text(registry).strip(),
                                    required=True, authority="必须遵守"))
    roster = cast_roster(cast, faction_cards)
    if roster:
        blocks.append(MaterialBlock("roster", "人物和势力名单", roster, authority="必须遵守", demote=False))
    if str(previous_ending or "").strip():
        blocks.append(MaterialBlock("ending", "上一幕最后一章的大纲", _headings_from_third(previous_ending.strip()),
                                    authority="参考", demote=False))
    blocks = [block for block in blocks if block.text]
    return _fitted(blocks, max_chars)


def _headings_from_third(text: str) -> str:
    """把一段大纲里的标题整体挪到最高一级是三级（“### 第 N 章：标题”），
    和要求的章标题写法一致，又不和块标题（二级）混在一起。"""
    levels = [len(match.group(1)) for match in re.finditer(r"^(#{1,6})(?=\s)", text, flags=re.M)]
    shift = 3 - min(levels) if levels else 0
    if shift <= 0:
        return text
    return re.sub(r"^(#{1,6})(?=\s)", lambda match: "#" * min(6, len(match.group(1)) + shift), text, flags=re.M)


def _fitted(blocks: Sequence[MaterialBlock], max_chars: int) -> PlanningMaterial:
    """按 ``fit_blocks`` 取舍后的材料，blocks 按块切开（首尾相接就是整段）。"""
    text, kept, omitted = fit_blocks(blocks, max_chars)
    parts = [(block.label, render_blocks([block])) for block in kept]
    if omitted:
        parts.append((OMITTED_HEADING, render_blocks([], omitted)))
    material = PlanningMaterial(text)
    material.blocks = tuple(
        (label, part + ("\n\n" if index < len(parts) - 1 else ""))
        for index, (label, part) in enumerate(parts)
    )
    material.kept = tuple(block.label for block in kept)
    material.omitted = tuple(block.label for block in omitted)
    return material


def outline_prompt(
    *,
    genre_label: str,
    structure_name: str,
    sections: Sequence[str],
    section_name: str,
    section_content: str,
    start_chapter: int,
    story_params: Dict[str, Any],
    location_guidance: Sequence[str],
    output_dir: str | None = None,
    previous_ending: str = "",
    material_budget: int = OUTLINE_BUDGET,
) -> PlanningPrompt:
    """「生成章节大纲」给一幕的提示词。给了 ``output_dir`` 时，这一幕的故事结构后面接上
    设定材料（``outline_material``）。"""
    section = zh_label(section_name)
    material = (
        outline_material(output_dir, previous_ending, max_chars=material_budget)
        if output_dir else PlanningMaterial("")
    )
    material_blocks = [
        (label, ("\n\n" if index == 0 else "") + text)
        for index, (label, text) in enumerate(material.blocks)
    ]
    naming = ""
    if "名称与规则登记表" in material.kept:
        naming += (
            "列人物、势力和地点时用名称与规则登记表里的名字；确实需要登记表里没有的新人物时，"
            "在“涉及人物”里写明此人的身份。"
        )
    if "上一幕最后一章的大纲" in material.kept:
        naming += "“上一幕最后一章的大纲”只用来接上前情，不要重写那一章的事。"
    prompt = _prompt([
        (
            "任务说明",
            f"请为一部{genre_label}小说生成章节大纲。"
            f"小说采用“{zh_label(structure_name)}”框架。"
            f"该结构包含：{', '.join(zh_label(item) for item in sections)}。\n"
            f"当前重点是 **{section}**。以下是这一部分的详细规划：\n\n",
        ),
        ("这一幕的故事结构", section_content),
        *material_blocks,
        (
            "写法要求",
            f"\n\n请根据“{section}”的详细规划，逐章生成大纲。"
            "每一章都应有明确目的，并推动这一结构部分的故事。"
            "为每章建议所含场景，并列出本章涉及的人物、势力和具体地点。"
            + naming +
            f"“{section}”从第 {start_chapter} 章开始，请依次分配章号。\n"
            "每一章必须单独起一行标题，写成“### 第 N 章：标题”，使用阿拉伯数字，"
            "不要用“第一章”这样的中文数字，也不要把章标题混进正文段落。\n",
        ),
        ("作品参数", "\n".join(build_story_parameter_lines(story_params)) + "\n"),
        ("地点要求", "\n".join(f"- {line}" for line in location_guidance) + "\n"),
        ("输出格式", "请以 Markdown 格式输出，不要使用代码围栏，也不要在响应中写出“Markdown”一词。"),
    ])
    prompt.kept, prompt.omitted = material.kept, material.omitted
    return prompt


# ---------------------------------------------------------------- 规划场景


def continuity_context(output_dir: str, chapter_number: int) -> Dict[str, Any]:
    """第 N 章规划时，关于「从第 N-1 章什么状态接过来」的全部已知事实。

    全部来自已经落盘的契约与正文，不发起任何调用。
    """
    chapter_number = int(chapter_number)
    if chapter_number <= 1:
        return {}
    contracts = load_planning_contracts(output_dir)
    prior = [item for item in contracts if int(item["chapter"]) < chapter_number]
    previous = next(
        (item for item in prior if int(item["chapter"]) == chapter_number - 1), None
    )
    events = (previous or {}).get("timeline_events", []) or []
    tail = ""
    for folder in ("story/content/chapters", "chapters"):
        candidate = os.path.join(
            output_dir, *folder.split("/"), f"chapter_{chapter_number - 1}.md"
        )
        if os.path.isfile(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as handle:
                    tail = handle.read()[-2000:]
            except (OSError, UnicodeError):
                tail = ""
            break
    # 「连着三章同一个功能」是硬规则，模型必须先知道前两章填了什么。
    recent_functions = {
        int(item["chapter"]): str(item.get("chapter_function", "") or "")
        for item in prior
        if int(item["chapter"]) in (chapter_number - 1, chapter_number - 2)
    }
    return {
        "previous_tail": tail,
        "established": established_context(prior, chapter_number),
        "previous_last_event": events[-1] if events else None,
        "neglected": neglected_threads(prior, chapter_number),
        "recent_functions": recent_functions,
    }


def _suspense_ledger(output_dir: str) -> Dict[str, Any]:
    path = StoryLedgerManager(output_dir).suspense_ledger_path
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def contract_instructions(
    output_dir: str, chapter_number: int, story_params: Dict[str, Any]
) -> PlanningPrompt:
    """「规划场景」里的契约格式说明，连同仍未了结的悬念、后续章节依赖、既有事实等几块。"""
    index = build_existing_planning_index(output_dir, chapter_number)
    narrative_context = NarrativeGraphManager.reader(output_dir).planning_context(
        chapter_number, _suspense_ledger(output_dir)
    )
    profile = resolve_domain_profile(story_params)
    domain_fields = {field.name: field.schema_hint for field in profile.contract_fields}
    blocks = contract_instruction_blocks(
        chapter_number,
        index,
        domain_fields=domain_fields,
        obligations=downstream_obligations(output_dir, chapter_number),
        narrative_context=narrative_context,
        continuity_context=continuity_context(output_dir, chapter_number),
    )
    return _prompt(_stripped(blocks))


class PlanningMaterial(str):
    """「规划场景」一章的材料（就是一段文字），另带 blocks（按先后排的（块名，原文），
    首尾相接就是整段）、kept / omitted（带上、去掉的块名）和 characters（带了卡片的人物）。"""

    blocks: Tuple[Block, ...] = ()
    kept: Tuple[str, ...] = ()
    omitted: Tuple[str, ...] = ()
    characters: Tuple[str, ...] = ()


def planning_material(
    output_dir: str,
    chapter_number: int,
    chapter_outlines: Dict[int, str],
    contract_text: Any,
    *,
    lore_content: str = MISSING_LORE,
    section_outline: str = "",
    max_chars: int = PLANNING_BUDGET,
) -> PlanningMaterial:
    """「规划场景」一章的材料，按重要性排成九块，超出篇幅时从最后一块起整块去掉：

    1. 本章大纲；2. 名称与规则登记表；3. 本章人物（本章大纲点名的，挑法和写场景材料一样）；
    4. 相关势力；5. 上一章结束时的状态；6. 仍未了结的悬念和后续章节依赖；7. 既有事实和时间
    事件（按编号和本章大纲点到的名字挑）；8. 前后相邻两章的大纲（参考）；9. 世界观正文（参考）。

    1、2、3、6 篇幅再紧也带：前三块是本章必须照着写的，第 6 块是契约必须照抄的编号，
    少了它新规划一定过不了检查。第 5、6 块取自契约格式说明里的同名几块（``contract_text``
    的 blocks）。作品没有设定文件时没有 2–4 块，世界观正文用 ``lore_content``。
    """
    chapter_number = int(chapter_number)
    outline = str(chapter_outlines.get(chapter_number) or "").strip()
    if not outline and str(section_outline or "").strip():
        outline = (
            f"没能从这一幕的大纲里单独切出第 {chapter_number} 章，下面是这一幕的全部大纲，"
            f"只规划其中第 {chapter_number} 章。\n\n{section_outline.strip()}"
        )
    files = dict(lore_files(output_dir))
    registry, cast, faction_cards = setting_records(files)
    contract_parts = dict(getattr(contract_text, "blocks", ()) or ())
    blocks: List[MaterialBlock] = []

    def add(key, label, text, authority, *, required=False, demote=True):
        if str(text or "").strip():
            blocks.append(MaterialBlock(key, label, str(text).strip(), required=required,
                                        authority=authority, demote=demote))

    add("outline", "本章大纲", outline, "必须遵守", required=True)
    if files:
        add("registry", "名称与规则登记表", registry_text(registry), "必须遵守", required=True)
    chosen, fallback = pick_characters(outline, cast, registry)
    if chosen:
        note = "本章大纲里没有认出登记过的人物，先带主角的资料。\n\n" if fallback else ""
        add("characters", "本章人物", note + "\n\n".join(card_text(card) for card in chosen),
            "必须遵守", required=True, demote=False)
    factions = pick_factions(outline, chosen, faction_cards, registry)
    if factions:
        add("factions", "相关势力", "\n\n".join(card_text(card) for card in factions),
            "必须遵守", demote=False)
    add("previous", "上一章结束时的状态", contract_parts.get("上一章结束时的状态"), "必须遵守")
    threads = "\n\n".join(
        str(contract_parts.get(label) or "").strip()
        for label in ("仍未了结的悬念", "后续章节依赖本章")
        if str(contract_parts.get(label) or "").strip()
    )
    add("threads", "仍未了结的悬念和后续章节依赖", threads, "必须遵守", required=True)
    add("records", "既有事实和时间事件",
        _picked_records(output_dir, chapter_number, outline, registry, cast, faction_cards),
        "必须遵守")
    neighbours = "\n\n".join(
        chapter_outlines[number]
        for number in (chapter_number - 1, chapter_number + 1)
        if str(chapter_outlines.get(number) or "").strip()
    )
    add("neighbours", "前后相邻两章的大纲", neighbours, "参考")
    if BODY in files:
        add("body", "世界观正文", sanitize_lore_content(files[BODY]), "参考")
    elif not files:
        add("body", "世界观正文", lore_content or MISSING_LORE, "参考")

    material = _fitted(blocks, max_chars)
    material.characters = tuple(name_of(card) for card in chosen)
    return material


def _picked_records(
    output_dir: str,
    chapter_number: int,
    outline: str,
    registry: Dict[str, Any],
    cast: Sequence[Dict[str, Any]],
    faction_cards: Sequence[Dict[str, Any]],
) -> str:
    """既有事实和时间事件里和本章有关的：后面章节要用到的、上一章记下的（按编号），
    以及点到本章大纲里的人物、势力、地点的（按名字）。已了结的悬念都列。"""
    index = build_existing_planning_index(output_dir, chapter_number)
    wanted = set(downstream_obligations(output_dir, chapter_number).get("facts_used_later", {}))
    terms = setting_terms(registry, cast, faction_cards)
    # 大纲里写了、登记表里没有的名字也拿来认：作品没有设定文件时就只剩这些。
    people, places = listed_names(outline)
    for item in [*people, *(part for place in places for part in re.split(r"[·・]", place))]:
        name = re.sub(r"[（(].*$", "", item).strip()
        if 2 <= len(name) <= 8:
            terms.setdefault(name, name)
    named = set(mentioned(outline, terms))
    previous = chapter_number - 1

    def related(record: Dict[str, Any], *fields: str) -> bool:
        record_id = str(record.get("id", ""))
        if record_id in wanted or _source_chapter_from_id(record_id) == previous:
            return True
        text = "\n".join(str(record.get(name) or "") for name in fields)
        return bool(named & set(mentioned(text, terms)))

    facts = [record for record in index.get("facts", []) if related(record, "fact", "value")]
    events = [
        record for record in index.get("timeline_events", [])
        if related(record, "event", "location_id")
    ]
    skipped = [
        f"{count} 条{kind}"
        for count, kind in (
            (len(index.get("facts", [])) - len(facts), "事实"),
            (len(index.get("timeline_events", [])) - len(events), "时间事件"),
        )
        if count
    ]
    note = (
        f"另有 {'、'.join(skipped)}没有列出：它们和本章大纲点到的人物、地点无关，"
        "也不是上一章记下的或后面章节要用到的。"
        if skipped else ""
    )
    return established_records_text(
        facts, events, index.get("closed_plot_threads", []), note=note
    )


def scene_plan_prompt(
    *,
    genre_label: str,
    chapter_number: int,
    story_length: str,
    structure_name: str,
    sections: Sequence[str],
    section_name: str,
    story_params: Dict[str, Any],
    section_outline: str,
    location_guidance: Sequence[str],
    lore_content: str,
    contract_text: str,
    output_dir: str,
    chapter_outlines: Dict[int, str],
    material_budget: int = PLANNING_BUDGET,
) -> PlanningPrompt:
    """「规划场景」给长篇一章的提示词：任务和作品参数，材料（``planning_material``），
    写法要求，最后是契约格式与规则。契约格式说明里属于材料的几块排进材料，不在后面重复。"""
    section = zh_label(section_name)
    material = planning_material(
        output_dir, chapter_number, chapter_outlines, contract_text,
        lore_content=lore_content, section_outline=section_outline, max_chars=material_budget,
    )
    requirements = ["请把「本章大纲」扩展为详细场景。"]
    if story_length == "Novella":
        requirements.append("本故事是中篇小说，本章场景应紧凑而有冲击力，聚焦必要的情节推进和人物时刻。")
    requirements += [
        "每个场景需说明：环境与具体地点、出场人物、关键行动/事件、关键对白片段（如有必要），以及它如何推动本章情节或人物发展。",
        "请保持人物弧光、势力和地点与本章大纲中的建议一致；前后相邻两章的大纲只用来划清本章写到哪里为止，不要把那两章的事写进本章。",
        *[f"- {line}" for line in location_guidance],
    ]
    head = _joined([
        ("任务说明", [
            f"请为{genre_label}故事第 {chapter_number} 章规划场景（篇幅：{zh_label(story_length)}）。",
            f"故事采用“{zh_label(structure_name)}”框架，包含：{', '.join(zh_label(item) for item in sections)}。",
            f"当前正在设计 **{section}** 的场景。",
        ]),
        ("作品参数", build_story_parameter_lines(story_params)),
    ])
    asks = _joined([
        ("场景要求", requirements),
        ("场景标题格式", [
            "请使用结构清晰的 Markdown。每个场景必须使用阿拉伯数字编号，并以独立标题开始，例如“### 场景 1：场景标题”或“## 场景 2 - 场景标题”。不要使用“场景一”之类的中文数字编号，也不要只使用加粗文本充当场景标题。",
        ]),
    ])
    rules = [
        (label, text.strip())
        for label, text in (getattr(contract_text, "blocks", ()) or (("契约格式说明", str(contract_text)),))
        if label not in MATERIAL_LABELS and text.strip()
    ]
    rules = [
        (label, text + ("\n\n" if index < len(rules) - 1 else ""))
        for index, (label, text) in enumerate(rules)
    ]
    # 四组之间空一行：任务和作品参数、材料、写法要求、契约格式与规则。
    blocks: List[Block] = []
    for group in (head, material.blocks, asks, rules):
        if not group:
            continue
        if blocks:
            label, text = blocks[-1]
            blocks[-1] = (label, text.rstrip("\n") + "\n\n")
        blocks.extend(group)
    prompt = _prompt(blocks)
    prompt.kept, prompt.omitted, prompt.characters = material.kept, material.omitted, material.characters
    return prompt


def short_story_prompt(
    *,
    genre_label: str,
    novel_title: str,
    structure_name: str,
    story_params: Dict[str, Any],
    plot: str,
    location_guidance: Sequence[str],
    lore_content: str,
    contract_text: str,
) -> PlanningPrompt:
    """「规划场景」给短篇整篇的提示词。"""
    blocks = _joined([
        ("任务说明", [
            f"请为{genre_label}短篇小说《{novel_title}》规划场景。",
            f"故事采用“{zh_label(structure_name)}”框架。",
        ]),
        ("作品参数", build_story_parameter_lines(story_params)),
        ("详细情节", [
            "以下是完整短篇小说的详细总体情节：\n\n",
            "--- 短篇小说详细情节 ---",
            plot,
            "\n\n--- 短篇小说详细情节结束 ---",
        ]),
        ("场景要求", [
            # 场景数必须给上限。这里以前只说「拆分成一系列场景」，实测模型给了 10 个，
            # 而下游把整篇短篇当作**一个章节**送进质量闸门——那套闸门的重修预算（2 轮）
            # 与分数门槛都是在 2-3 场的章节上实测定下来的。10 场进去的结果是三轮评审
            # 分数一路走低（2.52 → 2.43 → 1.91），30 分钟后判不通过、整篇作废。
            f"\n请根据详细情节，把故事拆分成 {SHORT_STORY_MIN_SCENES}-{SHORT_STORY_MAX_SCENES} "
            f"个清晰、独立的场景。这是短篇，不要按长篇那样一场一场铺开；"
            f"宁可让单场承载更多内容，也不要超过 {SHORT_STORY_MAX_SCENES} 个场景。",
            "每个场景需说明：\n",
            "  - 建议的场景编号（如场景 1、场景 2）。\n",
            "  - 环境与具体地点。\n",
            "  - 出场人物。\n",
            "  - 场景中的关键行动和事件。\n",
            "  - 关键对白片段或对白概要。\n",
            "  - 该场景如何依据详细情节推动整体故事，或发展人物/主题。\n",
            "确保场景之间衔接自然，并覆盖详细情节中的完整叙事弧。\n",
            *[f"- {line}" for line in location_guidance],
        ]),
        ("场景标题格式", [
            "\n重要格式要求：",
            "每个场景必须以 Markdown 标题开始，并使用阿拉伯数字编号，例如“### 场景 1：<场景标题>”或“## 场景 2 - <场景标题>”。不要使用中文数字编号或只加粗的标题。",
            "标题下方再列出该场景的环境、人物、关键行动等要点。",
        ]),
        ("世界观", ["\n如有需要，可参考以下整体世界观：", lore_content]),
        ("输出要求", [
            "\n现在请按场景输出完整的短篇规划，合并为一份结构清晰的 Markdown 文档，不要使用代码围栏。",
        ]),
        ("契约格式说明", [contract_text]),
    ])
    return _prompt(_with_contract(blocks, contract_text))


def block_sizes(prompt: Any) -> Dict[str, int]:
    """每块多少字（同名的块合在一起算）。"""
    sizes: Dict[str, int] = {}
    for label, text in getattr(prompt, "blocks", ()) or ((("全文", str(prompt)),)):
        sizes[label] = sizes.get(label, 0) + len(text)
    return sizes


__all__ = [
    "ActFiles",
    "MISSING_LORE",
    "OUTLINE_BUDGET",
    "PLANNING_BUDGET",
    "PlanningMaterial",
    "PlanningPrompt",
    "SHORT_STORY_MAX_SCENES",
    "SHORT_STORY_MIN_SCENES",
    "act_files",
    "block_sizes",
    "book_outlines",
    "cast_roster",
    "chapter_parts",
    "continuity_context",
    "contract_instructions",
    "outline_material",
    "outline_prompt",
    "planning_lore",
    "planning_material",
    "previous_act_ending",
    "scene_plan_prompt",
    "short_story_prompt",
    "slug",
]
