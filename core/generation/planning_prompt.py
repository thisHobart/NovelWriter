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
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple

from core.generation.chapter_continuity import established_context, neglected_threads
from core.generation.domain_profiles import resolve_domain_profile
from core.generation.narrative_graph import NarrativeGraphManager
from core.generation.planning_contract import (
    build_existing_planning_index,
    contract_instruction_blocks,
    downstream_obligations,
    load_planning_contracts,
)
from core.generation.helper_fns import open_file
from core.generation.lore_repository import lore_context
from core.generation.prompt_context import build_story_parameter_lines, sanitize_lore_content
from core.generation.story_ledger import StoryLedgerManager
from core.localization import zh_label

#: 短篇的场景数上下限。短篇整篇是作为**一个章节**送进质量闸门的，而那套闸门的
#: 重修预算与分数门槛都是在 2-3 场的章节上实测定下来的。实测一篇 10 场的短篇在
#: 三轮评审里分数一路走低（2.52 → 2.43 → 1.91），30 分钟后判不通过、整篇作废。
#: 这个上限让短篇的形状落回闸门能处理的范围。
SHORT_STORY_MIN_SCENES = 3
SHORT_STORY_MAX_SCENES = 6

#: 作品没有世界观时，「规划场景」里世界观那一块写这句。
MISSING_LORE = "缺少整体世界观背景。"

Block = Tuple[str, str]


class PlanningPrompt(str):
    """一份提示词（就是一段文字），另带按先后排的（中文名，原文）。"""

    blocks: Tuple[Block, ...] = ()


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


# ---------------------------------------------------------------- 生成章节大纲


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
) -> PlanningPrompt:
    """「生成章节大纲」给一幕的提示词。"""
    section = zh_label(section_name)
    return _prompt([
        (
            "任务说明",
            f"请为一部{genre_label}小说生成章节大纲。"
            f"小说采用“{zh_label(structure_name)}”框架。"
            f"该结构包含：{', '.join(zh_label(item) for item in sections)}。\n"
            f"当前重点是 **{section}**。以下是这一部分的详细规划：\n\n",
        ),
        ("这一幕的故事结构", section_content),
        (
            "写法要求",
            f"\n\n请根据“{section}”的详细规划，逐章生成大纲。"
            "每一章都应有明确目的，并推动这一结构部分的故事。"
            "为每章建议所含场景，并列出本章涉及的人物、势力和具体地点。"
            f"“{section}”从第 {start_chapter} 章开始，请依次分配章号。\n"
            "每一章必须单独起一行标题，写成“### 第 N 章：标题”，使用阿拉伯数字，"
            "不要用“第一章”这样的中文数字，也不要把章标题混进正文段落。\n",
        ),
        ("作品参数", "\n".join(build_story_parameter_lines(story_params)) + "\n"),
        ("地点要求", "\n".join(f"- {line}" for line in location_guidance) + "\n"),
        ("输出格式", "请以 Markdown 格式输出，不要使用代码围栏，也不要在响应中写出“Markdown”一词。"),
    ])


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
) -> PlanningPrompt:
    """「规划场景」给长篇一章的提示词。"""
    section = zh_label(section_name)
    requirements = [f"\n请重点把上述大纲中的第 {chapter_number} 章扩展为详细场景。"]
    if story_length == "Novella":
        requirements.append("本故事是中篇小说，本章场景应紧凑而有冲击力，聚焦必要的情节推进和人物时刻。")
    requirements += [
        "每个场景需说明：环境与具体地点、出场人物、关键行动/事件、关键对白片段（如有必要），以及它如何推动本章情节或人物发展。",
        f"请保持人物弧光、势力和地点与第 {chapter_number} 章大纲中的建议一致。",
        *[f"- {line}" for line in location_guidance],
    ]
    blocks = _joined([
        ("任务说明", [
            f"请为{genre_label}故事第 {chapter_number} 章规划场景（篇幅：{zh_label(story_length)}）。",
            f"故事采用“{zh_label(structure_name)}”框架，包含：{', '.join(zh_label(item) for item in sections)}。",
            f"当前正在设计 **{section}** 的场景。",
        ]),
        ("作品参数", build_story_parameter_lines(story_params)),
        ("本幕逐章大纲", [f"\n以下是包含第 {chapter_number} 章的“{section}”逐章大纲：\n{section_outline}"]),
        ("场景要求", requirements),
        ("世界观", [f"整体世界观如下，供参考：\n{lore_content}"]),
        ("场景标题格式", [
            "\n请使用结构清晰的 Markdown。每个场景必须使用阿拉伯数字编号，并以独立标题开始，例如“### 场景 1：场景标题”或“## 场景 2 - 场景标题”。不要使用“场景一”之类的中文数字编号，也不要只使用加粗文本充当场景标题。",
        ]),
        ("契约格式说明", [contract_text]),
    ])
    return _prompt(_with_contract(blocks, contract_text))


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
    "PlanningPrompt",
    "SHORT_STORY_MAX_SCENES",
    "SHORT_STORY_MIN_SCENES",
    "act_files",
    "block_sizes",
    "continuity_context",
    "contract_instructions",
    "outline_prompt",
    "planning_lore",
    "scene_plan_prompt",
    "short_story_prompt",
    "slug",
]
