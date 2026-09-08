# -*- coding: utf-8 -*-
"""Framework-independent short-story prose pipeline.

This module contains business generation only: no widgets, event loops, or
message boxes.  Inputs arrive through :class:`StageContext` and output remains
compatible with existing NovelWriter workspaces.
"""
from __future__ import annotations

from core.generation.errors import fail
from core.generation.ai_helper import send_prompt, get_backend
import re
from core.generation.helper_fns import (
    archive_failed_generation,
    open_file,
    write_file,
    save_prompt_to_file,
    read_json,
    parse_scene_sections,
    publish_chapter_with_acceptance,
)
from core.generation.prompt_context import (
    analyze_chinese_prose_style,
    generate_prose_with_style_retry,
    find_scene_world_conflicts,
    first_field,
    format_faction_summary,
    normalize_story_parameters,
    sanitize_lore_content,
)
from core.generation.chapter_generation_loop import ChapterGenerationLoop, QualityGateError
from core.generation.chapter_acceptance import ChapterAcceptanceError, ValidationIssue
from core.generation.story_ledger import StoryLedgerManager
from core.generation.domain_profiles import resolve_domain_profile
from core.generation.scene_prompt import build_scene_prompt, scene_prompt_filename
from core.generation.cancellation import CancelToken, GenerationCancelled
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict
import os
import logging
from core.config.directory_config import get_directory_manager
from core.config.story_options import STRUCTURE_SECTIONS_MAP
from core.localization import zh_label

from core.generation.stage_context import StageContext


def _log_notice(level: str, title: str, message: str) -> None:
    logging.getLogger(__name__).log(
        getattr(logging, level, logging.INFO), "%s: %s", title, message
    )


def show_success(title: str, message: str, *args, **kwargs) -> None:
    _log_notice("INFO", title, message)


def show_warning(title: str, message: str, *args, **kwargs) -> None:
    _log_notice("WARNING", title, message)


def show_error(title: str, message: str, *args, **kwargs) -> None:
    fail(title, message)


@dataclass
class WritingRequest:
    """生成短篇正文所需的不可变请求数据。"""

    output_dir: str
    model: str
    chapter_number: int = 0
    parameters: Dict[str, Any] = field(default_factory=dict)

class ShortStoryPipeline:

    def __init__(self, app) -> None:
        self.app = app
        self.cancel_token = CancelToken()
        self.dir_manager = get_directory_manager(app.get_output_dir(), use_new_structure=True)

    def _write_short_story_prose(
        self, request: "WritingRequest", _decision_regeneration_attempt: int = 0
    ):
        """Generate a short story through the same design-generation-review loop.

        Short stories are treated as chapter 1 so they share one code path with
        chapters: same contract, same reviews, same acceptance into the ledger.

        Runs on a worker thread: every value it needs from the UI arrives in
        the request snapshot, and it must not touch Tk widgets.
        """
        selected_model = request.model
        output_dir = request.output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.app.logger.info(f"Initiating short story prose generation. Model: {selected_model}, Output Dir: {output_dir}")

        parameters = request.parameters
        story_params = normalize_story_parameters(parameters)
        novel_title = parameters.get("novel_title", "") or ""
        selected_structure_name = parameters.get("story_structure")

        if not selected_structure_name:
            self.app.logger.error("No story structure selected. Cannot determine input file for short story prose.")
            show_error("错误", "作品参数中尚未选择故事结构。")
            return

        safe_structure_name = selected_structure_name.lower().replace(' ', '_').replace(':', '').replace('/', '_')
        scene_plan_filename = f"scenes_short_story_{safe_structure_name}.md"
        scene_plan_filepath = os.path.join(output_dir, "story", "planning", scene_plan_filename)

        self.app.logger.info(f"Attempting to load scene plan from: {scene_plan_filepath}")
        try:
            scene_plan_content = open_file(scene_plan_filepath)
        except FileNotFoundError:
            self.app.logger.error(f"Scene plan file not found: {scene_plan_filepath}")
            show_error("错误", f"找不到场景规划文件“{scene_plan_filename}”，请先在“场景规划”页生成场景。")
            return

        if not scene_plan_content.strip():
            self.app.logger.error(f"Scene plan file '{scene_plan_filename}' is empty.")
            show_error("错误", f"场景规划文件“{scene_plan_filename}”为空，无法生成正文。")
            return

        if not parse_scene_sections(scene_plan_content):
            self.app.logger.error(f"Could not parse any scenes from '{scene_plan_filename}'.")
            show_error("错误", "无法从规划中解析场景，请确保场景以“### 场景 X：...”或“## 场景 X - ...”开头。")
            return

        lore_content = self._load_lore(output_dir)
        character_roster_summary = self._load_character_roster(output_dir)
        faction_summary_info = self._load_faction_summary(output_dir)

        conflicts = find_scene_world_conflicts(scene_plan_content, lore_content, story_params)
        if conflicts:
            conflict_text = "、".join(conflicts)
            self.app.logger.error(f"Scene plan conflicts with non-scifi lore: {conflict_text}")
            show_error("场景规划与世界观冲突", f"场景规划包含世界观未定义的科幻设定：{conflict_text}。请重新生成场景规划。")
            return

        quality_loop = ChapterGenerationLoop(
            output_dir=output_dir,
            model=selected_model,
            logger=self.app.logger,
            cancel_token=self.cancel_token,
            require_planning_contract=True,
        )

        def generate_scene_prose(
            scene_plan,
            scene_number,
            previous_scene_tail="",
            next_scene_plan="",
            contract=None,
            profile=None,
        ):
            self.app.logger.info(f"Processing Scene {scene_number} for the short story.")
            prompt = build_scene_prompt(
                scene_plan=scene_plan,
                scene_number=scene_number,
                parameters=story_params,
                lore=lore_content,
                character_roster=character_roster_summary,
                faction_summary=faction_summary_info,
                profile=profile or resolve_domain_profile(story_params),
                chapter_number=None,
                structure_name=selected_structure_name,
                novel_title=novel_title,
                contract=contract,
                previous_scene_tail=previous_scene_tail,
                next_scene_plan=next_scene_plan,
            )
            prompt_filepath = save_prompt_to_file(
                output_dir, scene_prompt_filename(scene_number), prompt
            )
            log_msg_source = f"(from {prompt_filepath})" if prompt_filepath else "(from memory, save failed)"
            current_backend = get_backend()
            backend_info = f"{current_backend}" if current_backend != "api" else f"api/{selected_model}"
            self.app.logger.info(
                f"Sending prompt for Scene {scene_number} {log_msg_source} to LLM ({backend_info})."
            )
            scene_prose = generate_prose_with_style_retry(
                lambda text: send_prompt(text, model=selected_model),
                prompt,
                logger=self.app.logger,
                label=f"第 {scene_number} 个场景",
            )
            self.app.logger.info(
                f"Received prose for Scene {scene_number}. Length: {len(scene_prose)} chars."
            )
            return scene_prose

        def save_revised_plan(revised_plan):
            archive_dir = os.path.join(output_dir, "archive", "quality_loop", "scene_plans")
            os.makedirs(archive_dir, exist_ok=True)
            base_name = os.path.splitext(scene_plan_filename)[0]
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            archive_path = os.path.join(archive_dir, f"{base_name}_before_{timestamp}.md")
            write_file(archive_path, scene_plan_content)
            write_file(scene_plan_filepath, revised_plan)
            self.app.logger.info(
                f"Quality loop revised the scene plan; original archived at {archive_path}."
            )

        try:
            loop_result = quality_loop.run(
                chapter_number=1,
                plan_content=scene_plan_content,
                parameters=story_params,
                lore=lore_content,
                generate_scene=generate_scene_prose,
                on_plan_revised=save_revised_plan,
            )
        except QualityGateError as gate_error:
            archived = archive_failed_generation(
                output_dir, 1, gate_error.partial_scenes, label="short_story"
            )
            self.app.logger.error(
                f"Short story failed the quality gate: {gate_error}"
                + (f" Partial prose archived at {archived}." if archived else "")
            )
            detail = f"\n\n已生成的部分正文归档在：\n{archived}" if archived else ""
            show_error("质量检查未通过", f"{gate_error}{detail}")
            return

        for waiver in loop_result.gate_waivers:
            self.app.logger.warning(f"Short story passed on a waiver: {waiver}")

        full_story_content = loop_result.chapter_content
        safe_title = (novel_title or "未命名短篇小说").lower().replace(' ', '_').replace(':', '').replace('/', '')
        output_story_filename = f"prose_short_story_{safe_title}.md"
        os.makedirs(os.path.join(output_dir, "story", "content"), exist_ok=True)
        output_story_filepath = os.path.join(output_dir, "story", "content", output_story_filename)

        try:
            publish_chapter_with_acceptance(
                output_dir,
                1,
                output_story_filepath,
                full_story_content,
                lambda saved_path: quality_loop.accept_result(
                    1, loop_result, chapter_path=saved_path
                ),
            )
        except ChapterAcceptanceError as acceptance_error:
            summary = self._handle_acceptance_conflict(
                1, acceptance_error, output_dir
            )
            if summary and _decision_regeneration_attempt < 1:
                return self._write_short_story_prose(
                    request, _decision_regeneration_attempt + 1
                )
            return
        self.app.logger.info(f"Short story prose successfully written to: {output_story_filepath}")

    def _load_lore(self, output_dir):
        """Load sanitized lore, tolerating a missing file."""
        lore_path = os.path.join(output_dir, "story", "lore", "generated_lore.md")
        if os.path.exists(lore_path):
            content = sanitize_lore_content(open_file(lore_path))
            self.app.logger.info(f"Loaded lore context from {lore_path}")
            return content
        return "Lore context is missing or not loaded."

    def _load_character_roster(self, output_dir):
        """Summarize the character roster for prompt context."""
        characters_json_path = os.path.join(output_dir, "story", "lore", "characters.json")
        if not os.path.exists(characters_json_path):
            characters_json_path = os.path.join(output_dir, "characters.json")
        if not os.path.exists(characters_json_path):
            return "没有可用的人物名单。"
        characters_data = read_json(characters_json_path)
        if not characters_data or "characters" not in characters_data:
            return "没有可用的人物名单。"
        summaries = []
        for char_info in characters_data["characters"]:
            details = [f"\n\n姓名：{char_info.get('name', '无')}\n"]
            details.append(f" - 角色：{char_info.get('role', '无')}\n")
            details.append(f" - 性别：{char_info.get('gender', '无')}\n")
            details.append(f" - 年龄：{char_info.get('age', '无')}\n")
            details.append(f" - 外貌：{first_field(char_info, 'appearance_summary', 'description') or '无'}\n")
            # 新卡是单数，旧项目是复数数组；两种都读。
            for label, keys in (
                ("职业", ("profession",)),
                ("主要目标", ("goals", "goal")),
                ("主要优点", ("strengths", "strength")),
                ("主要缺点", ("flaws", "flaw")),
                ("背景摘要", ("backstory_summary", "background")),
            ):
                value = first_field(char_info, *keys)
                if value:
                    details.append(f" - {label}：{value}\n")
            summaries.append("\n".join(details))
        if summaries:
            self.app.logger.info(f"Loaded and summarized character roster from {characters_json_path}")
            return "主要人物：\n" + "\n".join(summaries)
        return "没有可用的人物名单。"

    def _load_faction_summary(self, output_dir):
        """Summarize faction data for prompt context."""
        factions_json_path = os.path.join(output_dir, "story", "lore", "factions.json")
        if os.path.exists(factions_json_path):
            factions_data = read_json(factions_json_path)
            if factions_data:
                self.app.logger.info(f"Loaded and summarized faction info from {factions_json_path}")
                return format_faction_summary(factions_data)
        return "没有可用的势力信息。"

    def _handle_acceptance_conflict(self, chapter_number, error, output_dir):
        raise RuntimeError(
            f"第 {chapter_number} 章存在需要作者裁定的契约冲突；"
            "请在重新生成前修正故事账本。"
        ) from error
