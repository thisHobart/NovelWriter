# -*- coding: utf-8 -*-
"""Framework-independent entry points for NovelWriter generation stages."""
from __future__ import annotations

import glob
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable

from core.config.story_options import GENDER_BIAS_MAP
from core.generation.ai_helper import DEFAULT_API_MODEL, set_backend
from core.generation.lore_pipeline import LorePipeline
from core.generation.scene_pipeline import ScenePipeline
from core.generation.short_story_pipeline import ShortStoryPipeline, WritingRequest
from core.generation.stage_context import ProgressReport, StageContext, make_context
from core.generation.structure_pipeline import StructurePipeline


class StageGenerationError(RuntimeError):
    """A stage finished without producing its required durable artifacts."""


def load_stage_parameters(output_dir: str) -> Dict[str, Any]:
    """Load parameters.txt as both display keys and internal snake_case keys."""
    candidates = (
        os.path.join(output_dir, "system", "parameters.txt"),
        os.path.join(output_dir, "parameters.txt"),
    )
    source: Dict[str, Any] = {}
    for path in candidates:
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                source[key.strip()] = value.strip()
        break

    parameters = dict(source)
    for display, value in source.items():
        internal = display.lower().replace(" ", "_")
        if str(value).lower() in {"true", "false"}:
            value = str(value).lower() == "true"
        parameters[internal] = value

    bias = parameters.get("gender_generation_bias_string", "Balanced (50F/50M)")
    female, male = GENDER_BIAS_MAP.get(str(bias), (50, 50))
    parameters.setdefault("female_percentage", female)
    parameters.setdefault("male_percentage", male)
    return parameters


class _ParameterSource:
    def __init__(self, parameters: Dict[str, Any]) -> None:
        self._parameters = parameters

    def get_current_parameters(self) -> Dict[str, Any]:
        return dict(self._parameters)


@dataclass
class GenerationHost:
    """Small compatibility object used by the extracted business methods."""

    context: StageContext
    logger: logging.Logger

    def __post_init__(self) -> None:
        self.output_dir = self.context.output_dir
        self.param_ui = _ParameterSource(self.context.parameters)

    def get_output_dir(self) -> str:
        return self.context.output_dir

    def get_selected_model(self) -> str:
        return self.context.model


def _files(output_dir: str, patterns: Iterable[str]) -> list[str]:
    found: set[str] = set()
    for pattern in patterns:
        for path in glob.glob(os.path.join(output_dir, pattern), recursive=True):
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                found.add(os.path.relpath(path, output_dir))
    return sorted(found)


def _require(output_dir: str, label: str, patterns: Iterable[str]) -> list[str]:
    patterns = tuple(patterns)
    found = _files(output_dir, patterns)
    missing = [
        pattern for pattern in patterns
        if not glob.glob(os.path.join(output_dir, pattern), recursive=True)
    ]
    if missing or not found:
        detail = "、".join(missing or patterns)
        raise StageGenerationError(f"{label}没有生成有效产物：{detail}")
    return found


def _build_context(
    step: str,
    output_dir: str,
    model: str,
    parameters: Dict[str, Any] | None,
    report: ProgressReport | None,
) -> tuple[StageContext, GenerationHost]:
    """Create the shared headless context, merging UI overrides with saved values."""
    params = load_stage_parameters(output_dir)
    params.update(parameters or {})
    if not params:
        raise StageGenerationError("尚未保存作品参数")
    selected_model = model or str(params.get("model") or DEFAULT_API_MODEL)
    backend = str(params.get("backend") or "api")
    set_backend(backend, selected_model)
    context = make_context(
        output_dir,
        selected_model,
        params,
        report,
        num_factions=int(params.get("num_factions", 6)),
        num_chars=int(params.get("num_characters", params.get("num_chars", 8))),
    )
    return context, GenerationHost(context, logging.getLogger(f"generation.{step}"))


def _run_lore(context: StageContext, host: GenerationHost) -> Dict[str, Any]:
    pipeline = LorePipeline(host)
    context.extras.setdefault("num_factions", 6)
    context.extras.setdefault("num_chars", 8)
    for fraction, label, operation in (
        (0.08, "生成势力", pipeline._generate_factions),
        (0.28, "生成人物", pipeline._generate_characters),
        (0.50, "生成世界观", pipeline._generate_lore),
        (0.72, "完善主要人物", pipeline._main_character_enhancement),
        (0.88, "推荐标题", pipeline._suggest_titles),
    ):
        context.progress(label, fraction)
        operation(context)
    files = _require(
        context.output_dir,
        "世界设定阶段",
        (
            "story/lore/factions.json",
            "story/lore/characters.json",
            "story/lore/generated_lore.md",
            "story/lore/lore_contract.json",
        ),
    )
    return {"step": "lore", "generated_files": files}


def _run_structure(context: StageContext, host: GenerationHost) -> Dict[str, Any]:
    pipeline = StructurePipeline(host)
    for fraction, label, operation in (
        (0.08, "生成人物弧光", pipeline._generate_arcs),
        (0.28, "生成势力弧光", pipeline._generate_faction_arcs),
        (0.48, "融合地点与故事弧", pipeline._add_planets_to_arcs),
    ):
        context.progress(label, fraction)
        operation(context)

    story_length = context.parameters.get("story_length", "Novel (Standard)")
    context.progress("生成详细故事结构", 0.68)
    if story_length == "Short Story":
        pipeline._outline_short_story_plot(context)
        plot_patterns = ("story/structure/plot_short_story_*.md",)
    else:
        pipeline.improve_structure(context)
        structure = context.parameters.get("story_structure", "6-Act Structure")
        plot_patterns = (
            f"story/structure/{str(structure).lower().replace(' ', '_')}_*.md",
            "story/structure/structure_contract.json",
        )

    files = _require(
        context.output_dir,
        "故事结构阶段",
        (
            "story/structure/character_arcs.md",
            "story/structure/faction_arcs.md",
            "story/structure/reconciled_arcs.md",
            "story/planning/reconciled_locations_arcs.md",
            *plot_patterns,
        ),
    )
    return {"step": "structure", "generated_files": files}


def seed_narrative_graph(output_dir: str) -> Dict[str, Any]:
    """把结构契约里已声明的悬念与真相登记为叙事图节点。

    生产代码此前从不往图里加节点，于是新项目的图永远是空的，而契约校验只要发现
    契约引用了任何节点就切到严格档，要求那些 id 指向真实存在的节点——空图上没有
    一条能满足。播种不花调用：结构阶段已经写好了 threads_opened 与
    truths_introduced，这里只是把它们搬到图上。

    章号来自章节大纲：大纲按部分分文件、章号连续，所以每一部分的最后一章就是
    该部分声明的悬念的计划了结章。拿不到就不写计划章号，只影响「悬念沉默过久」
    这类提示，不影响节点可用。

    任何一步取不到东西都只记一条日志：播种是加分项，不该让整个场景规划停下来。
    """
    from core.generation.narrative_graph import NarrativeGraphManager
    from core.generation.story_ledger import StoryLedgerManager

    logger = logging.getLogger("generation.scenes")
    try:
        contract = StoryLedgerManager(output_dir).load_structure_contract()
        sections = (contract or {}).get("sections") or []
        if not sections:
            logger.info("没有结构契约，叙事图不播种（短篇本来就没有这一份）")
            return {"added": 0}
        result = NarrativeGraphManager(output_dir).seed_from_structure(
            sections, _section_last_chapters(output_dir)
        )
        logger.info("叙事图播种：新增 %s 个节点，修订号 %s",
                    result.get("added"), result.get("revision"))
        return result
    except Exception as exc:  # noqa: BLE001  播种失败不该挡住场景规划
        logger.warning("叙事图播种失败，按空图继续：%s", exc)
        return {"added": 0, "error": str(exc)}


def _section_last_chapters(output_dir: str) -> Dict[int, int]:
    """每一部分的最后一章章号，从章节大纲文件里数出来。"""
    directory = os.path.join(output_dir, "story", "planning", "chapter_outlines")
    if not os.path.isdir(directory):
        return {}
    chapter_heading = re.compile(r"^#+\s*第\s*(\d+)\s*章", re.MULTILINE)
    found: Dict[str, int] = {}
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".md"):
            continue
        try:
            with open(os.path.join(directory, name), "r", encoding="utf-8") as handle:
                numbers = [int(item) for item in chapter_heading.findall(handle.read())]
        except (OSError, UnicodeError, ValueError):
            continue
        if numbers:
            found[name] = max(numbers)
    # 文件名按部分排序，第 N 个文件就是第 N 部分。
    return {index: last for index, (_, last) in enumerate(sorted(found.items()), 1)}


def _run_scenes(context: StageContext, host: GenerationHost) -> Dict[str, Any]:
    pipeline = ScenePipeline(host)
    story_length = context.parameters.get("story_length", "Novel (Standard)")
    if story_length == "Short Story":
        context.progress("规划短篇场景", 0.20)
        pipeline._plan_short_story_scenes(context)
        patterns = ("story/planning/scenes_short_story_*.md",)
    else:
        context.progress("生成章节大纲", 0.12)
        pipeline._generate_chapter_outline(context)
        # 章节大纲出来之后才知道每一部分covers哪几章，这时才能把结构契约里声明的
        # 悬念与真相登记成叙事图节点。不花任何调用，但决定了下一步逐章规划时模型
        # 能不能从真实存在的节点里选 primary_thread——图是空的时候它只能自己编，
        # 编出来的 id 会让契约切到严格档，六条规则一起报错。
        context.progress("登记叙事图节点", 0.30)
        seed_narrative_graph(context.output_dir)
        context.progress("逐章规划场景", 0.45)
        pipeline._plan_long_form_scenes(context)
        patterns = (
            "story/planning/chapter_outlines/chapter_outlines_*.md",
            "story/planning/detailed_scene_plans/scenes_*.md",
        )
    files = _require(context.output_dir, "场景规划阶段", patterns)
    return {"step": "scenes", "generated_files": files}


def _run_long_form_chapters(
    context: StageContext,
    mode: str = "all",
    chapter_number: int | None = None,
) -> Dict[str, Any]:
    from agents.writing.chapter_writing_agent import ChapterWritingAgent

    agent = ChapterWritingAgent(
        context.output_dir,
        app_instance=None,
        use_new_structure=True,
        model=context.model,
    )
    chapter_info, _ = agent.analyze_chapter_structure()
    if not chapter_info:
        raise StageGenerationError("没有找到可写作的章节大纲")
    plan = agent.create_writing_plan(chapter_info, batch_size=3)
    if mode == "next":
        plan.chapters_to_write = plan.chapters_to_write[:1]
        plan.batch_size = 1
    elif mode == "rewrite":
        if chapter_number is None:
            raise StageGenerationError("重写章节时缺少章节编号")
        known = {info.chapter_number for info in chapter_info}
        if chapter_number not in known:
            raise StageGenerationError(f"找不到第 {chapter_number} 章的规划")
        plan.chapters_to_write = [chapter_number]
        plan.chapters_completed = [
            number for number in plan.chapters_completed if number != chapter_number
        ]
        plan.batch_size = 1
    if plan.chapters_to_write:
        context.progress(f"撰写 {len(plan.chapters_to_write)} 章正文", 0.18)

        def chapter_progress(
            event: str,
            number: int,
            completed: int,
            total: int,
        ) -> None:
            fraction = completed / total if total else -1.0
            if event == "started":
                context.progress(
                    f"正在撰写第 {number} 章（已完成 {completed}/{total}）",
                    fraction,
                )
            else:
                context.progress(
                    f"第 {number} 章已完成（{completed}/{total}）",
                    fraction,
                )

        result = agent.write_chapters_batch(
            chapter_info,
            plan,
            progress_callback=chapter_progress,
        )
        if not result.success:
            messages = result.messages or (result.data or {}).get("errors", [])
            raise StageGenerationError("；".join(messages) or "章节生成失败")
    files = _require(
        context.output_dir,
        "章节写作阶段",
        ("story/content/chapters/chapter_*.md",),
    )
    return {"step": "chapters", "generated_files": files}


def _load_pending(output_dir: str, chapter_number: int | None):
    """取一章的待复审记录，顺便挡掉两种做不了的情况。"""
    from core.generation import pending_review

    if chapter_number is None:
        raise StageGenerationError("复审动作缺少章节编号")
    record = pending_review.load(output_dir, chapter_number)
    if record is None:
        raise StageGenerationError(f"第 {chapter_number} 章没有待复审记录")
    if not record.resumable:
        raise StageGenerationError(
            f"第 {chapter_number} 章只写到一半就被闸门拦下，没有完整章节可用；"
            "请重写本章，或先回场景规划改这一章的规划"
        )
    return record


def _review_pending_chapter(
    context: StageContext,
    chapter_number: int | None,
    action: str,
    options: Dict[str, Any],
) -> Dict[str, Any]:
    """执行作者对某一待复审章节的裁决：照建议重修，或人工放行。

    动作本身由写作 agent 完成，而不是在这里自己拼一个质量闭环：验收有可能判定
    这一章的契约要让位于账本，那时旧正文全部作废、需要逐场重生成，而重生成用得
    上的写作上下文与生成能力只有 agent 有。
    """
    from agents.writing.chapter_writing_agent import ChapterWritingAgent

    record = _load_pending(context.output_dir, chapter_number)
    number = record.chapter_number

    asks: list[str] = []
    if action == "revise":
        issue_ids = options.get("issue_ids")
        asks = record.asks_for(issue_ids if isinstance(issue_ids, list) else None)
        if not asks and not record.needs_review_rerun:
            raise StageGenerationError(
                f"第 {number} 章没有勾选任何可执行的修改建议，重修没有依据"
            )
        if asks:
            context.progress(f"照 {len(asks)} 条建议重修第 {number} 章", 0.15)
        else:
            # 评审没出过结论的那一稿：这条路是重跑评审，先给它补上判定。
            context.progress(f"重跑第 {number} 章的质量评审", 0.15)
    else:
        context.progress(f"人工放行第 {number} 章", 0.20)

    agent = ChapterWritingAgent(
        context.output_dir,
        app_instance=None,
        use_new_structure=True,
        model=context.model,
    )
    result = agent.review_pending_chapter(
        number,
        action,
        asks=asks,
        reason=str(options.get("reason") or "").strip(),
    )
    if not result.success:
        messages = result.messages or (result.data or {}).get("errors", [])
        raise StageGenerationError("；".join(messages) or f"第 {number} 章复审未完成")
    return {
        "step": "chapters",
        "chapter_number": number,
        "generated_files": _files(
            context.output_dir, (f"story/content/chapters/chapter_{number}.md",)
        ),
    }


def _run_chapters(context: StageContext, host: GenerationHost) -> Dict[str, Any]:
    if context.parameters.get("story_length", "Novel (Standard)") != "Short Story":
        return _run_long_form_chapters(context)
    pipeline = ShortStoryPipeline(host)
    context.progress("撰写短篇正文", 0.15)
    pipeline._write_short_story_prose(
        WritingRequest(
            output_dir=context.output_dir,
            model=context.model,
            chapter_number=1,
            parameters=context.parameters,
        )
    )
    files = _require(
        context.output_dir,
        "短篇写作阶段",
        ("story/content/prose_short_story_*.md",),
    )
    return {"step": "chapters", "generated_files": files}


def run_stage_action(
    step: str,
    action: str,
    output_dir: str,
    model: str = "",
    parameters: Dict[str, Any] | None = None,
    report: ProgressReport | None = None,
    chapter_number: int | None = None,
    options: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Run one user-visible action inside a stage.

    The Qt UI exposes fine-grained buttons. Keeping this dispatch here ensures
    those buttons remain GUI-independent and testable without importing Qt.

    `options` 只服务于复审动作：重修要知道作者勾了哪几条建议（`issue_ids`），
    放行要记下理由（`reason`）。这两样都不是作品参数，混进 parameters 会被当成
    生成参数存进档案。
    """
    if step not in {"lore", "structure", "scenes", "chapters"}:
        raise ValueError(f"未知生成阶段：{step}")
    context, host = _build_context(step, output_dir, model, parameters, report)

    if action in {"", "full"}:
        operations = {
            "lore": _run_lore,
            "structure": _run_structure,
            "scenes": _run_scenes,
            "chapters": _run_chapters,
        }
        result = operations[step](context, host)
    elif step == "lore":
        pipeline = LorePipeline(host)
        actions = {
            "factions": ("生成势力", pipeline._generate_factions,
                         ("story/lore/factions.json",)),
            "characters": ("生成人物", pipeline._generate_characters,
                           ("story/lore/characters.json",)),
            "lore": ("生成世界观", pipeline._generate_lore,
                     ("story/lore/generated_lore.md", "story/lore/lore_contract.json")),
            "enhance": ("完善主要人物", pipeline._main_character_enhancement,
                        ("story/lore/background_*.md",)),
            "titles": ("推荐标题", pipeline._suggest_titles,
                       ("story/planning/suggested_titles.md",)),
        }
        if action not in actions:
            raise ValueError(f"世界设定阶段没有动作：{action}")
        label, operation, patterns = actions[action]
        context.progress(label, 0.10)
        operation(context)
        files = _require(output_dir, label, patterns)
        result = {"step": step, "action": action, "generated_files": files}
    elif step == "structure":
        pipeline = StructurePipeline(host)
        actions = {
            "arcs": ("生成人物弧光", pipeline._generate_arcs,
                     ("story/structure/character_arcs.md",)),
            "faction_arcs": ("生成势力弧光", pipeline._generate_faction_arcs,
                             ("story/structure/faction_arcs.md",
                              "story/structure/reconciled_arcs.md")),
            "locations": ("融合地点与故事弧", pipeline._add_planets_to_arcs,
                          ("story/planning/reconciled_locations_arcs.md",)),
        }
        if action == "plot":
            context.progress("生成详细故事结构", 0.10)
            if context.parameters.get("story_length", "Novel (Standard)") == "Short Story":
                pipeline._outline_short_story_plot(context)
                patterns = ("story/structure/plot_short_story_*.md",)
            else:
                pipeline.improve_structure(context)
                patterns = ("story/structure/structure_contract.json",)
            files = _require(output_dir, "详细故事结构", patterns)
        else:
            if action not in actions:
                raise ValueError(f"故事结构阶段没有动作：{action}")
            label, operation, patterns = actions[action]
            context.progress(label, 0.10)
            operation(context)
            files = _require(output_dir, label, patterns)
        result = {"step": step, "action": action, "generated_files": files}
    elif step == "scenes":
        pipeline = ScenePipeline(host)
        short = context.parameters.get("story_length", "Novel (Standard)") == "Short Story"
        if action == "outline" and not short:
            context.progress("生成章节大纲", 0.10)
            pipeline._generate_chapter_outline(context)
            patterns = ("story/planning/chapter_outlines/chapter_outlines_*.md",)
        elif action == "scenes" and not short:
            context.progress("逐章规划场景", 0.10)
            pipeline._plan_long_form_scenes(context)
            patterns = ("story/planning/detailed_scene_plans/scenes_*.md",)
        elif action in {"outline", "scenes"} and short:
            context.progress("规划短篇场景", 0.10)
            pipeline._plan_short_story_scenes(context)
            patterns = ("story/planning/scenes_short_story_*.md",)
        else:
            raise ValueError(f"场景规划阶段没有动作：{action}")
        files = _require(output_dir, "场景规划", patterns)
        result = {"step": step, "action": action, "generated_files": files}
    elif action in {"revise", "waive"}:
        result = _review_pending_chapter(
            context, chapter_number, action, options or {}
        )
        result["action"] = action
    else:
        if action not in {"next", "all", "rewrite"}:
            raise ValueError(f"章节撰写阶段没有动作：{action}")
        if context.parameters.get("story_length", "Novel (Standard)") == "Short Story":
            result = _run_chapters(context, host)
        else:
            result = _run_long_form_chapters(context, action, chapter_number)
        result["action"] = action

    context.progress(f"{step} · {action or 'full'} 已完成", 1.0)
    return result


def run_stage(
    step: str,
    output_dir: str,
    model: str = "",
    parameters: Dict[str, Any] | None = None,
    report: ProgressReport | None = None,
) -> Dict[str, Any]:
    """Run one stage synchronously without importing any GUI toolkit."""
    if step not in {"lore", "structure", "scenes", "chapters"}:
        raise ValueError(f"未知生成阶段：{step}")
    context, host = _build_context(step, output_dir, model, parameters, report)
    operations = {
        "lore": _run_lore,
        "structure": _run_structure,
        "scenes": _run_scenes,
        "chapters": _run_chapters,
    }
    result = operations[step](context, host)
    context.progress(f"{step} 阶段已完成", 1.0)
    return result
