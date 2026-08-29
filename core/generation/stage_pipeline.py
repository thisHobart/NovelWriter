# -*- coding: utf-8 -*-
"""Framework-independent entry points for NovelWriter generation stages."""
from __future__ import annotations

import glob
import logging
import os
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
        result = agent.write_chapters_batch(chapter_info, plan)
        if not result.success:
            messages = result.messages or (result.data or {}).get("errors", [])
            raise StageGenerationError("；".join(messages) or "章节生成失败")
    files = _require(
        context.output_dir,
        "章节写作阶段",
        ("story/content/chapters/chapter_*.md",),
    )
    return {"step": "chapters", "generated_files": files}


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
) -> Dict[str, Any]:
    """Run one user-visible action inside a stage.

    The Qt UI exposes fine-grained buttons. Keeping this dispatch here ensures
    those buttons remain GUI-independent and testable without importing Qt.
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
