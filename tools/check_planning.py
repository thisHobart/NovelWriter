# -*- coding: utf-8 -*-
"""不花调用的规划检查：看「场景规划」这一步现在的产出和提示词。

    python tools/check_planning.py <作品目录> [--chapters]

读作品目录里已有的章节大纲、场景规划和章节契约，用正式流程同一套函数拼出每一幕、每一章
会收到的提示词（不发送），报告：

- 每章场景数；
- 场景边界：条数、代码认得出场次的条数、写了「停在哪里」（end_state）的条数、写第 N 场时
  应拦和实际拦的后面场次边界；
- 规划提示词：每章字数、每块字数、被截断的块、内部字段、「本章必须埋下 / 引入」里其实早已
  存在的条目、名字为空的条目、材料篇幅放不下去掉的块、本章大纲点名却没带卡片的人物；
- 章节大纲提示词的字数；
- 大纲和场景规划里点到、名称与规则登记表里却没有的人名和地名；
- 跨章检查的结果。

只读：不调用模型，不改作品目录里的任何文件。加 ``--chapters`` 再逐章列一行。
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config.directory_config import get_directory_manager  # noqa: E402
from core.config.story_options import STRUCTURE_SECTIONS_MAP, is_single_chapter, length_profile  # noqa: E402
from core.generation.helper_fns import (  # noqa: E402
    parse_chapter_numbers,
    parse_scene_sections,
    resolve_section_chapter_numbers,
)
from core.generation.lore_repository import lore_files  # noqa: E402
from core.generation.planning_checks import (  # noqa: E402
    LONG_FORM_SCENE_RANGE,
    NameFindings,
    chapter_count_note,
    listed_names,
    load_registry,
    scene_count_note,
    scene_range_for,
    unregistered_names,
)
from core.generation.planning_contract import (  # noqa: E402
    PlanningContractError,
    boundary_scene,
    collect_contract_defects,
    later_scene_boundaries,
    load_planning_contracts,
    thread_states,
)
from core.generation.planning_prompt import (  # noqa: E402
    MISSING_LORE,
    act_files,
    book_outlines,
    contract_instructions,
    outline_prompt,
    planning_lore,
    previous_act_ending,
    scene_plan_prompt,
    short_story_prompt,
    slug,
)
from core.generation.prompt_context import (  # noqa: E402
    build_location_guidance,
    format_genre_label,
    normalize_story_parameters,
)
from core.generation.prompt_material import OMITTED_HEADING  # noqa: E402
from core.generation.scene_material import mentioned, name_terms, setting_records  # noqa: E402
from core.generation.stage_pipeline import load_stage_parameters  # noqa: E402
from core.generation.story_ledger import StoryLedgerManager  # noqa: E402
from core.generation.workflow_status import expected_chapter_count  # noqa: E402
from core.localization import zh_label  # noqa: E402

#: 契约格式里本来就要模型照抄的英文字段在这一块，不算内部字段。
SCHEMA_BLOCK = "契约格式与填写规则"
#: 只对程序有用、模型用不上的字段。
INTERNAL_FIELDS = (
    "truncated", "omitted", "graph_revision", "ledger_revision", "source_hash", "updated_at",
    "schema_version", "origin", "narrative_graph_validation", "narrative_graph_managed",
    "continuation", "stale",
)
_INTERNAL = re.compile(r'"(_[A-Za-z_]+|' + "|".join(INTERNAL_FIELDS) + r')"\s*:')
_OBLIGATION = re.compile(r"^- (悬念|事实) ([A-Za-z]+-\d+-\d+).*本章必须(埋下|引入)", re.M)
# 名字为空：写成一对空括号，或者编号后面直接跟冒号、一个名字都没有。
_EMPTY_NAME = re.compile(r"(?:悬念|事实) ([A-Za-z]+-\d+-\d+)(?:（）|：)")
#: 旧契约里写「这一场到哪为止」的几种字段。
LEGACY_END_KEYS = ("end", "end_action", "end_line", "end_cut")
#: 认场次的标准答案：比正式代码宽，认不出时按在列表里的位置算。
_TRUE_SCENE_KEYS = ("scene_number", "scene_index", "scene", "scene_id", "first_stated_at", "introduced_at")
_TRUE_SCENE_TEXT = re.compile(r"(?:scene[_\s-]*(\d+)|场景\s*(\d+)|第\s*(\d+)\s*场)", re.I)


@dataclass
class ChapterReport:
    chapter: int
    section: str = ""
    scenes: Optional[int] = None
    boundaries: int = 0
    recognised: int = 0
    with_end_state: int = 0
    with_legacy_end: int = 0
    should_block_first: int = 0
    blocked_first: int = 0
    should_block_all: int = 0
    blocked_all: int = 0
    prompt_chars: int = 0
    blocks: Dict[str, int] = field(default_factory=dict)
    truncated_blocks: List[str] = field(default_factory=list)
    internal_fields: List[str] = field(default_factory=list)
    stale_obligations: List[str] = field(default_factory=list)
    empty_names: List[str] = field(default_factory=list)
    #: 材料篇幅放不下、这次没带的块。
    material_omitted: List[str] = field(default_factory=list)
    #: 本章大纲点名、人物资料里有卡片，提示词里却没带卡片的人物。
    missing_cards: List[str] = field(default_factory=list)
    #: 拼出来的提示词原文（不打印，测试拿它和正式流程比对）。
    prompt: str = ""


@dataclass
class PlanningReport:
    output_dir: str
    short_story: bool = False
    structure: str = ""
    expected_chapters: int = 0
    chapters: List[ChapterReport] = field(default_factory=list)
    outline_prompts: Dict[str, Dict[str, int]] = field(default_factory=dict)
    #: 每一幕大纲提示词的原文（不打印，测试拿它和正式流程比对）。
    outline_texts: Dict[str, str] = field(default_factory=dict)
    outline_names: NameFindings = field(default_factory=NameFindings)
    #: 篇幅档案建议的每章场景数、每幕章数（没有建议时是 None），和每一幕大纲实际写了几章。
    scene_range: Tuple[int, int] = LONG_FORM_SCENE_RANGE
    chapters_per_act: Optional[int] = None
    act_chapters: Dict[str, int] = field(default_factory=dict)
    plan_names: NameFindings = field(default_factory=NameFindings)
    registry_found: bool = False
    defects: List[Tuple[str, str]] = field(default_factory=list)
    contract_error: str = ""
    notes: List[str] = field(default_factory=list)


# ---------------------------------------------------------------- 收集


def collect(output_dir: str) -> PlanningReport:
    """读作品目录，拼出提示词，量出各项数字。只读，不调用模型。"""
    params = load_stage_parameters(output_dir)
    story_params = normalize_story_parameters(params)
    structure = params.get("story_structure", params.get("Story Structure", "6-Act Structure"))
    story_length = params.get("story_length", params.get("Story Length", "Novel (Standard)"))
    report = PlanningReport(output_dir=output_dir, structure=structure,
                            expected_chapters=expected_chapter_count(output_dir),
                            scene_range=scene_range_for(story_length))
    profile = length_profile(story_length)
    report.chapters_per_act = profile.chapters_per_act(structure) if profile else None
    registry = load_registry(output_dir)
    report.registry_found = bool(registry)
    try:
        contracts = load_planning_contracts(output_dir)
    except PlanningContractError as exc:
        contracts = []
        report.contract_error = str(exc)
    origins = _origins(contracts)
    genre_label = format_genre_label(story_params)
    location_guidance = build_location_guidance(story_params)
    lore = planning_lore(output_dir, short_story=is_single_chapter(story_length))
    if lore is None:
        report.notes.append("作品没有世界观，提示词里世界观一块写的是“缺少整体世界观背景。”")
        lore = MISSING_LORE

    if is_single_chapter(story_length):
        report.short_story = True
        _collect_short_story(report, output_dir, structure, story_params, genre_label,
                             location_guidance, lore, registry, origins)
    else:
        _collect_long_form(report, output_dir, structure, story_length, story_params,
                           genre_label, location_guidance, lore, registry, origins)

    if contracts:
        defects = collect_contract_defects(
            contracts, total_chapters=report.expected_chapters or None
        )
        report.defects = [(defect.code, str(defect)) for defect in defects]
    return report


def _collect_long_form(report, output_dir, structure, story_length, story_params, genre_label,
                       location_guidance, lore, registry, origins) -> None:
    sections = STRUCTURE_SECTIONS_MAP.get(structure) or []
    if not sections:
        report.notes.append(f"找不到故事结构“{structure}”的幕定义")
        return
    manager = get_directory_manager(output_dir, use_new_structure=True)
    outlines_dir = manager.get_chapter_outlines_path()
    plans_dir = manager.get_full_path("scene_plans_dir")

    # 生成章节大纲：每一幕都按「只重新生成这一幕」拼，起始章号接在前面几幕已有大纲之后。
    offset = 1
    for section in sections:
        files = act_files(output_dir, structure, section, outlines_dir, plans_dir)
        outline = _read(files.outline_file)
        content = _read(files.structure_file)
        if content is not None:
            prompt = outline_prompt(
                genre_label=genre_label, structure_name=structure, sections=sections,
                section_name=section, section_content=content, start_chapter=offset,
                story_params=story_params, location_guidance=location_guidance,
                output_dir=output_dir,
                previous_ending=previous_act_ending(output_dir, structure, sections, section, outlines_dir),
            )
            report.outline_prompts[section] = {"全文": len(prompt), **_sizes(prompt)}
            report.outline_texts[section] = str(prompt)
        if outline and parse_chapter_numbers(outline):
            report.act_chapters[section] = len(parse_chapter_numbers(outline))
            offset = max(offset, max(parse_chapter_numbers(outline)) + 1)
            report.outline_names.merge(unregistered_names(outline, registry))

    # 规划场景：章号的认法和正式流程一样。
    chapter_outlines = book_outlines(output_dir, structure, sections, outlines_dir)
    cards = _card_terms(output_dir)
    next_expected, claimed = 1, set()
    for section in sections:
        files = act_files(output_dir, structure, section, outlines_dir, plans_dir)
        outline = (_read(files.outline_file) or "").strip()
        if not outline:
            report.notes.append(f"“{section}”没有章节大纲")
            continue
        numbers, _warning = resolve_section_chapter_numbers(
            parse_chapter_numbers(outline), next_expected, claimed
        )
        if not numbers:
            report.notes.append(f"“{section}”的章节大纲里没有章标题")
            continue
        claimed.update(numbers)
        next_expected = max(next_expected, max(numbers) + 1)
        for number in numbers:
            plan = _read(files.scene_plan_file(number))
            prompt = scene_plan_prompt(
                genre_label=genre_label, chapter_number=number, story_length=story_length,
                structure_name=structure, sections=sections, section_name=section,
                story_params=story_params, section_outline=outline,
                location_guidance=location_guidance, lore_content=lore,
                contract_text=contract_instructions(output_dir, number, story_params),
                output_dir=output_dir, chapter_outlines=chapter_outlines,
            )
            item = _chapter(output_dir, number, section, plan, prompt, origins)
            item.material_omitted = list(getattr(prompt, "omitted", ()))
            item.missing_cards = _missing_cards(chapter_outlines.get(number, ""), prompt, cards)
            report.chapters.append(item)
            if plan:
                report.plan_names.merge(unregistered_names(plan, registry))


def _collect_short_story(report, output_dir, structure, story_params, genre_label,
                         location_guidance, lore, registry, origins) -> None:
    name = slug(structure or "")
    plot = _read(os.path.join(output_dir, "story", "structure", f"plot_short_story_{name}.md"))
    plan = _read(os.path.join(output_dir, "story", "planning", f"scenes_short_story_{name}.md"))
    if plot is None:
        report.notes.append("短篇还没有详细情节，规划提示词拼不出来")
        prompt = ""
    else:
        prompt = short_story_prompt(
            genre_label=genre_label,
            novel_title=story_params.get("novel_title", "未命名短篇小说"),
            structure_name=structure, story_params=story_params, plot=plot,
            location_guidance=location_guidance, lore_content=lore,
            contract_text=contract_instructions(output_dir, 1, story_params),
        )
    report.chapters.append(_chapter(output_dir, 1, "短篇", plan, prompt, origins))
    if plan:
        report.plan_names.merge(unregistered_names(plan, registry))


def _chapter(output_dir, number, section, plan, prompt, origins) -> ChapterReport:
    item = ChapterReport(chapter=number, section=section)
    if plan is not None:
        item.scenes = len(parse_scene_sections(plan))
    contract = _contract(output_dir, number)
    boundaries = contract.get("scene_boundaries") if isinstance(contract, dict) else None
    boundaries = boundaries if isinstance(boundaries, list) else []
    item.boundaries = len(boundaries)
    item.recognised = sum(boundary_scene(entry) is not None for entry in boundaries)
    item.with_end_state = sum(
        isinstance(entry, dict) and bool(str(entry.get("end_state", "") or "").strip())
        for entry in boundaries
    )
    item.with_legacy_end = sum(
        isinstance(entry, dict) and any(str(entry.get(key, "") or "").strip() for key in LEGACY_END_KEYS)
        for entry in boundaries
    )
    truth = [_true_scene(entry, position) for position, entry in enumerate(boundaries, 1)]
    last = max([item.scenes or 0, *truth]) if truth else (item.scenes or 0)
    for scene in range(1, last + 1):
        should = sum(value > scene for value in truth)
        actual = len(later_scene_boundaries(contract, scene))
        item.should_block_all += should
        item.blocked_all += actual
        if scene == 1:
            item.should_block_first, item.blocked_first = should, actual

    item.prompt = str(prompt)
    item.prompt_chars = len(prompt)
    item.blocks = _sizes(prompt)
    for label, text in getattr(prompt, "blocks", ()):
        if '"_context_meta"' in text and '"truncated": true' in text:
            item.truncated_blocks.append(label)
        if label != SCHEMA_BLOCK:
            for name in _INTERNAL.findall(text):
                if name not in item.internal_fields:
                    item.internal_fields.append(name)
    for kind, record_id, _verb in _OBLIGATION.findall(str(prompt)):
        origin = origins["thread" if kind == "悬念" else "fact"].get(record_id)
        if origin is not None and origin < number:
            item.stale_obligations.append(record_id)
    item.empty_names = list(dict.fromkeys(_EMPTY_NAME.findall(str(prompt))))
    return item


def _card_terms(output_dir: str) -> Dict[str, str]:
    """人物资料里有卡片的人物：名字和别名（登记表里的别名也算） → 规范名。"""
    registry, cast, _factions = setting_records(dict(lore_files(output_dir)))
    entries = [entry for entry in registry.get("canonical_characters") or [] if isinstance(entry, dict)]
    return name_terms(cast, entries) if cast else {}


def _missing_cards(outline: str, prompt: Any, cards: Dict[str, str]) -> List[str]:
    """本章大纲「涉及人物」里点名、有卡片的人物，提示词「本章人物」一块里却没有他的卡片。"""
    people, _places = listed_names(outline)
    named = mentioned("\n".join(people), cards)
    block = re.search(r"^## 本章人物[^\n]*\n(.*?)(?=^## |\Z)", str(prompt), flags=re.M | re.S)
    carded = set(re.findall(r"^### (.+)$", block.group(1), flags=re.M)) if block else set()
    return [name for name in named if name not in carded]


def _true_scene(entry: Any, position: int) -> int:
    """标准答案：这条边界属于第几场。写法认不出时按它在列表里的位置算。"""
    if isinstance(entry, dict):
        for key in _TRUE_SCENE_KEYS:
            value = entry.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            text = str(value or "").strip()
            if text.isdigit():
                return int(text)
            match = _TRUE_SCENE_TEXT.search(text)
            if match:
                return int(next(group for group in match.groups() if group))
        return position
    if isinstance(entry, int) and not isinstance(entry, bool):
        return entry
    text = str(entry).strip()
    if text.isdigit():
        return int(text)
    match = _TRUE_SCENE_TEXT.match(text)
    return int(next(group for group in match.groups() if group)) if match else position


def _origins(contracts: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """每条悬念在第几章埋下、每条事实在第几章引入。"""
    threads = {
        thread_id: state["opened_at"]
        for thread_id, state in thread_states(contracts).items()
        if state.get("opened_at") is not None
    }
    facts: Dict[str, int] = {}
    for contract in contracts:
        for record in contract.get("facts_added", []) or []:
            facts.setdefault(str(record.get("id", "")), int(contract["chapter"]))
    return {"thread": threads, "fact": facts}


def _contract(output_dir: str, number: int) -> Dict[str, Any]:
    text = _read(StoryLedgerManager(output_dir).chapter_contract_path(number))
    try:
        return json.loads(text) if text else {}
    except ValueError:
        return {}


def _read(path: str) -> Optional[str]:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeError):
        return None


def _sizes(prompt: Any) -> Dict[str, int]:
    sizes: Dict[str, int] = {}
    for label, text in getattr(prompt, "blocks", ()):
        sizes[label] = sizes.get(label, 0) + len(text)
    return sizes


# ---------------------------------------------------------------- 输出


def render(report: PlanningReport, *, chapters: bool = False) -> str:
    lines = [f"规划检查：{report.output_dir}"]
    planned = [item for item in report.chapters if item.scenes is not None]
    contracted = [item for item in report.chapters if item.boundaries or item.prompt_chars]
    lines.append(
        f"作品：{'短篇' if report.short_story else '长篇'}；章节大纲 {report.expected_chapters} 章；"
        f"场景规划 {len(planned)} 份；拼出规划提示词 {sum(1 for i in contracted if i.prompt_chars)} 份"
    )
    lines += [f"注意：{note}" for note in report.notes]
    if report.contract_error:
        lines.append(f"章节契约读不出来：{report.contract_error}")

    counts = [item.scenes for item in planned]
    lines.append("")
    lines.append(
        "【场景数】" + (_runs(planned) if planned else "没有场景规划")
        + (f"（最少 {min(counts)}，最多 {max(counts)}）" if counts else "")
    )
    if planned:
        # 和「规划场景」收下规划时写运行记录用的是同一个判断（只计数，不打回）。
        scene_range = report.scene_range
        outside = [item for item in planned if scene_count_note(item.scenes, scene_range)]
        lines.append(
            f"  建议每章 {scene_range[0]}–{scene_range[1]} 场；不在范围里的 {len(outside)} 章"
            + (f"（{_preview(f'第 {i.chapter} 章 {i.scenes} 场' for i in outside)}）" if outside else "")
        )

    if report.act_chapters:
        # 和「生成章节大纲」收下大纲时写运行记录用的是同一个判断（只计数，不打回）。
        expected = report.chapters_per_act
        off = [f"{zh_label(act)} {count} 章" for act, count in report.act_chapters.items()
               if chapter_count_note(count, expected)]
        lines.append("【每幕章数】" + "、".join(
            f"{zh_label(act)} {count}" for act, count in report.act_chapters.items()))
        lines.append(
            f"  建议每幕 {expected} 章；章数不同的幕 {len(off)} 个" + (f"（{_preview(off)}）" if off else "")
            if expected else "  篇幅档案对这个结构没有每幕章数的建议"
        )

    total = sum(item.boundaries for item in report.chapters)
    lines.append(
        f"【场景边界】共 {total} 条；代码认得出场次 {sum(i.recognised for i in report.chapters)} 条；"
        f"写了停在哪里（end_state）{sum(i.with_end_state for i in report.chapters)} 条"
        f"（旧写法里写了结尾的 {sum(i.with_legacy_end for i in report.chapters)} 条）"
    )
    lines.append(
        f"  写第 1 场时应拦 / 实际拦的后面场次边界：{sum(i.should_block_first for i in report.chapters)}"
        f" / {sum(i.blocked_first for i in report.chapters)}"
    )
    lines.append(
        f"  写每一场时合计应拦 / 实际拦：{sum(i.should_block_all for i in report.chapters)}"
        f" / {sum(i.blocked_all for i in report.chapters)}"
    )

    prompts = [item for item in report.chapters if item.prompt_chars]
    lines.append("")
    if prompts:
        sizes = [item.prompt_chars for item in prompts]
        lines.append(
            f"【规划提示词】{len(prompts)} 份；字数 最少 {min(sizes)} / 中位 {int(statistics.median(sizes))}"
            f" / 最多 {max(sizes)}"
        )
        lines.append("  每块字数（最少 / 中位 / 最多）：")
        for label in _labels(item.blocks for item in prompts):
            values = [item.blocks.get(label, 0) for item in prompts]
            lines.append(
                f"    {label}：{min(values)} / {int(statistics.median(values))} / {max(values)}"
            )
        truncated = Counter(label for item in prompts for label in item.truncated_blocks)
        lines.append(
            "  被截断的块："
            + ("、".join(f"{label} × {count} 份" for label, count in truncated.items()) or "无")
        )
        internal = [item for item in prompts if item.internal_fields]
        names = list(dict.fromkeys(name for item in internal for name in item.internal_fields))
        lines.append(
            f"  带内部字段的提示词：{len(internal)} 份" + (f"（{'、'.join(names)}）" if names else "")
        )
        flagged = [item for item in prompts if item.truncated_blocks or item.internal_fields]
        lines.append(f"  带截断或内部字段的提示词：{len(flagged)} 份")
        stale = [(item.chapter, record) for item in prompts for record in item.stale_obligations]
        lines.append(
            f"  「本章必须埋下 / 引入」里其实早已存在的条目：{len(stale)} 条"
            + (f"（涉及 {len({chapter for chapter, _ in stale})} 章）" if stale else "")
        )
        empty = [(item.chapter, record) for item in prompts for record in item.empty_names]
        lines.append(
            f"  名字为空的条目：{len(empty)} 条"
            + (f"（{_preview(f'第 {c} 章 {r}' for c, r in empty)}）" if empty else "")
        )
        dropped = Counter(label for item in prompts for label in item.material_omitted)
        lines.append(
            f"  材料篇幅放不下、去掉了块的提示词：{sum(1 for item in prompts if item.material_omitted)} 份"
            + (f"（{'、'.join(f'{label} × {count}' for label, count in dropped.items())}）" if dropped else "")
        )
        missing = [(item.chapter, name) for item in prompts for name in item.missing_cards]
        lines.append(
            f"  本章大纲点名、却没带卡片的人物：{len(missing)} 个"
            + (f"（{_preview(f'第 {c} 章 {n}' for c, n in missing)}）" if missing else "")
        )
    else:
        lines.append("【规划提示词】一份都拼不出来")

    if report.outline_prompts:
        totals = [sizes["全文"] for sizes in report.outline_prompts.values()]
        lines.append(
            f"【大纲提示词】{len(totals)} 份；字数 最少 {min(totals)} / 中位 "
            f"{int(statistics.median(totals))} / 最多 {max(totals)}；块："
            + "、".join(_labels({k: v for k, v in s.items() if k != "全文"} for s in report.outline_prompts.values()))
        )
        outline_sizes = list(report.outline_prompts.values())
        lines.append(
            "  带了设定材料的：" + "，".join(
                f"{label} {sum(label in sizes for sizes in outline_sizes)} 份"
                for label in ("名称与规则登记表", "人物和势力名单", "上一幕最后一章的大纲")
            )
            + f"；篇幅放不下、去掉了块的 {sum(OMITTED_HEADING in sizes for sizes in outline_sizes)} 份"
        )

    lines.append("")
    if report.registry_found:
        lines.append(
            "【登记表里没有的名字】大纲：人名 "
            f"{len(report.outline_names.people)} 个{_bracket(report.outline_names.people)}，地名 "
            f"{len(report.outline_names.places)} 个{_bracket(report.outline_names.places)}；"
            f"场景规划：人名 {len(report.plan_names.people)} 个{_bracket(report.plan_names.people)}，"
            f"地名 {len(report.plan_names.places)} 个{_bracket(report.plan_names.places)}"
        )
    else:
        lines.append("【登记表里没有的名字】作品没有名称与规则登记表，不判")

    by_code = Counter(code for code, _ in report.defects)
    lines.append(
        f"【跨章检查】{len(report.defects)} 处问题"
        + (f"（{'、'.join(f'{code} × {count}' for code, count in by_code.most_common())}）" if by_code else "")
    )

    if chapters:
        lines.append("")
        lines.append("逐章：章｜场景数｜边界 条数/认得出/写了 end_state｜写第 1 场应拦/实拦｜提示词字数｜问题")
        for item in report.chapters:
            issues = []
            if item.truncated_blocks:
                issues.append("截断：" + "、".join(item.truncated_blocks))
            if item.stale_obligations:
                issues.append(f"早已存在 {len(item.stale_obligations)} 条")
            if item.empty_names:
                issues.append("空名字：" + "、".join(item.empty_names))
            if item.material_omitted:
                issues.append("没带：" + "、".join(item.material_omitted))
            if item.missing_cards:
                issues.append("缺卡片：" + "、".join(item.missing_cards))
            lines.append(
                f"  第 {item.chapter} 章｜{item.scenes if item.scenes is not None else '无规划'}｜"
                f"{item.boundaries}/{item.recognised}/{item.with_end_state}｜"
                f"{item.should_block_first}/{item.blocked_first}｜{item.prompt_chars}｜"
                + ("；".join(issues) or "—")
            )
    return "\n".join(lines)


def _runs(items: Sequence[ChapterReport]) -> str:
    """「第1–10章 2场；第11–28章 3场」这样按连续章号合并。"""
    ordered = sorted(items, key=lambda item: item.chapter)
    runs: List[List[Any]] = []
    for item in ordered:
        if runs and runs[-1][2] == item.scenes and runs[-1][1] == item.chapter - 1:
            runs[-1][1] = item.chapter
        else:
            runs.append([item.chapter, item.chapter, item.scenes])
    return "；".join(
        (f"第{start}–{end}章" if start != end else f"第{start}章") + f" {scenes}场"
        for start, end, scenes in runs
    )


def _labels(groups: Iterable[Dict[str, int]]) -> List[str]:
    """各份提示词的块名合成一列；后面几份才有的块，插在它在那份里的前一块后面。"""
    seen: List[str] = []
    for group in groups:
        previous = None
        for label in group:
            if label not in seen:
                seen.insert(seen.index(previous) + 1 if previous in seen else len(seen), label)
            previous = label
    return seen


def _preview(items: Iterable[str], limit: int = 8) -> str:
    items = list(items)
    shown = "、".join(items[:limit])
    return shown + (f" 等 {len(items)} 条" if len(items) > limit else "")


def _bracket(items: Sequence[str]) -> str:
    return f"（{_preview(items, 10)}）" if items else ""


def main(argv: Sequence[str]) -> int:
    arguments = [item for item in argv if not item.startswith("--")]
    if len(arguments) != 1:
        print(__doc__)
        return 2
    output_dir = os.path.abspath(arguments[0])
    if not os.path.isdir(output_dir):
        print(f"找不到目录：{output_dir}")
        return 2
    # 读文件的工具函数会往标准输出打「Opening file: …」，收起来，只留报告；日志也先关掉，
    # 跑完恢复原样（同一进程里别的代码还要记日志）。
    previous = logging.root.manager.disable
    logging.disable(logging.WARNING)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            report = collect(output_dir)
    finally:
        logging.disable(previous)
    print(render(report, chapters="--chapters" in argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
