# -*- coding: utf-8 -*-
"""Framework-independent story-structure generation pipeline.

This module contains business generation only: no widgets, event loops, or
message boxes.  Inputs arrive through :class:`StageContext` and output remains
compatible with existing NovelWriter workspaces.
"""
from __future__ import annotations

from core.generation.errors import fail
from core.generation.ai_helper import send_prompt, get_backend
import re
from core.generation.helper_fns import open_file, write_file, read_json, write_json, save_prompt_to_file
from core.generation.design_contract import (
    DesignContractError,
    extract_structure_contract,
    generate_with_contract_retry,
    chronology_after,
    chronology_orders_used,
    open_threads_after,
    truths_after,
    structure_contract_instructions,
    validate_structure_sequence,
)

STRUCTURE_CONTRACT_RETRY_LIMIT = 2

from core.generation.prompt_context import first_field, format_faction_summary
from core.generation.domain_profiles import resolve_domain_profile
import os
import traceback
import json
from core.config.story_options import STRUCTURE_SECTIONS_MAP
from core.localization import zh_label
import logging
from core.config.directory_config import get_directory_manager

from core.generation.cancellation import CancelToken
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


class StructurePipeline:

    def __init__(self, app) -> None:
        self.app = app
        self.cancel_token = CancelToken()
        self.dir_manager = get_directory_manager(app.get_output_dir(), use_new_structure=True)

    def _generate_arcs(self, ui):
        selected_model = ui.model # Get selected model
        output_dir = ui.output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.app.logger.info(f"Generating Character Arcs using model: {selected_model}, output_dir: {output_dir}")
        
        lore_file_path = os.path.join(output_dir, "story", "lore", "generated_lore.md")
        characters_json_path = os.path.join(output_dir, "story", "lore", "characters.json") # Path to characters.json
        output_file_path = os.path.join(output_dir, "story", "structure", "character_arcs.md")
        
        # --- Dynamically determine backstory file paths --- 
        main_character_names_by_role = {
            "protagonist": None,
            "deuteragonist": None,
            "antagonist": None
        }
        character_roster_summaries = []
        all_characters_data = [] # To store all loaded character dicts

        try:
            # Load Full Character Roster from characters.json FIRST to get names for backstories
            full_character_data_json = read_json(characters_json_path)
            all_characters_data = full_character_data_json.get("characters", [])
            if not all_characters_data:
                self.app.logger.error(f"No characters found in {characters_json_path}. Cannot determine main character names or build roster.")
                show_error("错误", f"未从 {characters_json_path} 加载到人物，无法识别主要人物背景。")
                return
            
            self.app.logger.info(f"Loaded {len(all_characters_data)} characters from {characters_json_path}.")

            for char_info in all_characters_data:
                name = char_info.get("name", "Unknown")
                role_raw = char_info.get("role", "").lower()
                gender = char_info.get("gender", "无")
                faction = char_info.get("faction", "无所属势力")
                age = char_info.get("age", "无")
                title = char_info.get("title", "") 
                
                # Store names of main characters for backstory file lookup
                if role_raw in main_character_names_by_role:
                    safe_name_for_file = name.lower().replace(' ', '_').replace('/', '_').replace(':', '_')
                    main_character_names_by_role[role_raw] = safe_name_for_file # Store sanitized name
                    self.app.logger.info(f"Identified {role_raw}: {name} (sanitized for filename: {safe_name_for_file})")

                # Build summary for character roster (including flaws and strengths)
                summary_parts = [f"  - {name}（{char_info.get('role', '无')}）"]
                if title:
                    summary_parts.append(f"头衔：{title}")
                summary_parts.append(f"年龄：{age}")
                summary_parts.append(f"性别：{gender}")
                summary_parts.append(f"所属势力：{faction}")
                
                # 新卡是单数 goal/strength/flaw，旧项目是复数数组；两种都读。
                # 这份名单是人物弧光生成的唯一输入，读空了整段弧光就没有依据。
                for label, keys in (
                    ("职业", ("profession",)),
                    ("主要目标", ("goals", "goal")),
                    ("动机", ("motivations", "motivation")),
                    ("优点", ("strengths", "strength")),
                    ("缺点", ("flaws", "flaw")),
                    ("人物弧光", ("arc",)),
                ):
                    value = first_field(char_info, *keys)
                    if value:
                        summary_parts.append(f"{label}：{value}")

                opposes = char_info.get("opposes")
                if isinstance(opposes, dict) and opposes.get("character"):
                    summary_parts.append(
                        f"对抗：挡住「{opposes['character']}」的"
                        f"{opposes.get('blocked_goal', '目标')}"
                    )

                summary = ", ".join(summary_parts)
                character_roster_summaries.append(summary)

        except FileNotFoundError:
            self.app.logger.error(f"Characters.json not found at {characters_json_path}. Cannot proceed.", exc_info=True)
            show_error("错误", f"找不到人物文件：{characters_json_path}")
            return
        except (json.JSONDecodeError, ValueError) as e:
            self.app.logger.error(f"Error loading or parsing {characters_json_path}: {e}", exc_info=True)
            show_error("错误", f"解析人物文件失败：{characters_json_path}")
            return
        
        # Construct dynamic background file paths (use structured directory layout)
        background_files_paths = {}
        for role, char_file_name_part in main_character_names_by_role.items():
            if char_file_name_part:
                # Filename format from lore.py: background_{role}_{name_part}.md
                filename = f"background_{role}_{char_file_name_part}.md"
                # Use structured directory: story/lore/ instead of flat structure
                background_files_paths[role] = os.path.join(output_dir, "story", "lore", filename)
            else:
                self.app.logger.warning(f"Could not find character name for role: {role} in characters.json. Cannot load their backstory.")
        
        # --- End dynamic backstory file path determination ---

        try:
            lore_content = open_file(lore_file_path)
            self.app.logger.info(f"Loaded lore from {lore_file_path}")
        except FileNotFoundError:
            self.app.logger.warning(f"Lore file not found: {lore_file_path}. Arcs might lack context.")
            show_warning("文件缺失", f"找不到世界观文件：{lore_file_path}，故事弧可能缺少背景。")
            lore_content = "Lore context is missing."

        # Load Individual Character Backstories
        backstory_content = {}
        main_chars_with_backstories = [] # Stores the role ('protagonist', etc.)
        roles_to_load = ["protagonist", "deuteragonist", "antagonist"]

        for role in roles_to_load:
            filepath = background_files_paths.get(role)
            if filepath and os.path.exists(filepath):
                backstory_content[role] = open_file(filepath)
                main_chars_with_backstories.append(role)
                self.app.logger.info(f"Loaded backstory for {role} from {filepath}.")
            elif filepath: # Filepath was determined but doesn't exist
                self.app.logger.warning(f"Background file for {role} not found at {filepath}. Skipping {role} backstory.")
                backstory_content[role] = f"{zh_label(role.capitalize())}的背景故事缺失（找不到文件：{os.path.basename(filepath)}）。"
            else: # Filepath could not be determined (name for role not found)
                # Already logged earlier, but good to have a placeholder for prompt
                backstory_content[role] = f"{zh_label(role.capitalize())}的背景故事缺失（未识别到对应人物）。"

        if not main_chars_with_backstories:
            self.app.logger.error("No main character background files could be loaded (protagonist, deuteragonist, antagonist). Cannot generate arcs.")
            show_error("错误", "无法加载任何主要人物背景文件，不能生成人物弧光；详情请查看日志。")
            return

        # Construct the Prompt
        prompt_lines = [
            "我正在创作一部小说，需要规划主要人物的人物弧光。",
            f"请为以下角色设计有吸引力的人物弧光：{', '.join([zh_label(r.capitalize()) for r in main_chars_with_backstories])}。",
            "请以世界观设定、人物详细背景和下方完整人物名单为基础。",
            "每位主要人物都应经历显著而合理的发展或改变。",
            "可以引用或安排“完整人物名单”中的人物参与，但除非情节必需，不要新增重要的具名人物。",
            "\n## 世界观设定：",
            lore_content
        ]

        if character_roster_summaries:
            prompt_lines.append("\n## 人物名单（供参考）：")
            prompt_lines.extend(character_roster_summaries)
        else:
            prompt_lines.append("\n## 人物名单（供参考）：未从 characters.json 加载到其他人物")

        prompt_lines.append("\n## 主要人物背景：")
        for role, story in backstory_content.items():
            prompt_lines.append(f"\n### {zh_label(role.capitalize())}的背景故事：\n{story}")

        prompt_lines.append(
            f"\n现在请为 {', '.join([zh_label(r.capitalize()) for r in main_chars_with_backstories])} 生成人物弧光，"
            "重点体现有意义的成长，并与上述背景故事、世界观和人物名单建立联系。"
        )
        prompt = "\n".join(prompt_lines)
        
        # Save prompt to a file and log its path
        prompt_filepath = save_prompt_to_file(output_dir, "character_arc_prompt", prompt)
        
        if prompt_filepath:
            self.app.logger.info(f"Character Arc Generation Prompt (length {len(prompt)}) saved to: {prompt_filepath}")
        else:
            self.app.logger.error(f"Failed to save Character Arc Generation Prompt to a file. Prompt length: {len(prompt)}.")
            # As a fallback, if saving failed, log the prompt directly if DEBUG is on, or a snippet.
            # This ensures critical info isn't lost if file saving fails.
            if self.app.logger.isEnabledFor(logging.DEBUG): # Check if DEBUG is enabled for the app logger
                self.app.logger.debug(f"Fallback: Full Character Arc Prompt due to save failure:\n{prompt}")
            else:
                self.app.logger.warning("Prompt content not logged directly due to length and save failure. Enable DEBUG for full prompt.")

        # Send to LLM
        current_backend = get_backend()
        backend_info = f"{current_backend}" if current_backend != "api" else f"api/{selected_model}"
        log_msg_prompt_source = f"(from {prompt_filepath})" if prompt_filepath else "(from memory, save failed)"
        self.app.logger.info(f"Sending prompt {log_msg_prompt_source} to LLM ({backend_info}) for character arc generation...")
        response = send_prompt(prompt, model=selected_model)
        
        if not response:
             self.app.logger.error("Failed to generate character arcs from LLM. No response received.")
             show_error("错误", "大模型生成人物弧光失败。")
             return
        
        self.app.logger.info(f"Received character arcs from LLM. Length: {len(response)} chars.")
        # Save the response
        write_file(output_file_path, response)
        self.app.logger.info(f"Character arcs saved successfully to {output_file_path}")

    def _generate_faction_arcs(self, ui):
        selected_model = ui.model
        output_dir = ui.output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.app.logger.info(f"Faction Arc Generation & Reconciliation started. Model: {selected_model}, Output Dir: {output_dir}")

        # Define file paths consistently using output_dir
        parameters_file_path = self.dir_manager.get_parameters_path()
        character_arcs_file_path = os.path.join(output_dir, "story", "structure", "character_arcs.md")
        factions_json_file_path = os.path.join(output_dir, "story", "lore", "factions.json")
        lore_file_path = os.path.join(output_dir, "story", "lore", "generated_lore.md")
        faction_arcs_output_file_path = os.path.join(output_dir, "story", "structure", "faction_arcs.md")
        reconciled_output_file_path = os.path.join(output_dir, "story", "structure", "reconciled_arcs.md")
        
        selected_structure = "6-Act Structure" # Default if file/key is missing
        try:
            params = {}
            # Use open_file helper for reading parameters.txt if it's simple text,
            # or parse manually if open_file isn't suitable for this format.
            # For now, keeping manual open as it was.
            if not os.path.exists(parameters_file_path):
                raise FileNotFoundError
            with open(parameters_file_path, "r", encoding="utf-8") as f:
                for line in f:
                    if ":" in line:
                        key, value = line.split(":", 1)
                        params[key.strip()] = value.strip()
            selected_structure = params.get("Story Structure", selected_structure)
            self.app.logger.info(f"Using selected story structure: {selected_structure}")
        except FileNotFoundError:
            self.app.logger.warning(f"Parameters file not found: {parameters_file_path}. Using default structure: {selected_structure}")

        # --- Step 1: Generate Faction Arcs --- 
        self.app.logger.info("--- Step 1: Generating Faction Arcs ---")

        # Load required inputs (character arcs, lore, factions)
        try:
            char_arcs_content = open_file(character_arcs_file_path)
        except FileNotFoundError:
            self.app.logger.error(f"Character arcs file not found: {character_arcs_file_path}. Cannot proceed.")
            show_error("错误", f"找不到人物弧光文件：{character_arcs_file_path}，无法继续。")
            return
        
        try:
            lore_content = open_file(lore_file_path)
        except FileNotFoundError:
            show_warning("文件缺失", f"找不到世界观文件：{lore_file_path}，势力故事弧可能缺少背景。")
            self.app.logger.warning(f"Lore file not found: {lore_file_path}. Faction arcs might lack context.")
            lore_content = "Lore context is missing."
            
        # Load and parse factions.json
        try:
            # Assuming factions.json is read using helper_fns.read_json if available and suitable
            # or direct open as it was. For now, direct open.
            if not os.path.exists(factions_json_file_path):
                raise FileNotFoundError
            with open(factions_json_file_path, 'r', encoding='utf-8') as f:
                factions_data = json.load(f)
            
            # Extract top 5 factions based on military strength (similar to LorePromptGenerator)
            for faction in factions_data:
                military_strength = 0
                if "military_assets" in faction and isinstance(faction["military_assets"], dict):
                    military_strength = faction["military_assets"].get("total_military_personnel", 0)
                # Ensure conversion handles potential None or non-digit strings gracefully
                mil_val = str(military_strength).replace(',', '') if military_strength is not None else '0'
                faction["_military_strength_value"] = int(mil_val) if mil_val.isdigit() else 0
            
            factions_data.sort(key=lambda x: x.get("_military_strength_value", 0), reverse=True)
            major_factions = factions_data[:5]
            
            if not major_factions:
                self.app.logger.error(f"No faction data found or extracted from {factions_json_file_path}. Cannot generate faction arcs.")
                show_error("错误", f"未从 {factions_json_file_path} 找到或提取到势力数据。")
                return
                
            faction_overview = format_faction_summary(major_factions)
            
        except FileNotFoundError:
            self.app.logger.error(f"Factions JSON file not found: {factions_json_file_path}. Cannot proceed.")
            show_error("错误", f"找不到势力 JSON 文件：{factions_json_file_path}，无法继续。")
            return
        except json.JSONDecodeError:
             self.app.logger.error(f"Error decoding JSON from {factions_json_file_path}.")
             show_error("错误", f"解析势力 JSON 文件失败：{factions_json_file_path}")
             return

        # Build the first prompt for generating faction arcs (USING SELECTED STRUCTURE)
        prompt1_lines = [
            "我正在创作一部小说，需要规划主要势力的故事弧。",
            f"请使用“{zh_label(selected_structure)}”框架，根据以下主要势力的简介和特征设计有吸引力的故事弧：",
            "\n## 主要势力概览：",
            faction_overview,
            "\n势力弧必须以下方世界观和既有的人物弧光为基础，并与人物弧光形成合乎逻辑的互动。",
            "\n## 世界观设定：",
            lore_content,
            "\n## 人物弧光：",
            char_arcs_content,
            f"\n现在只输出这些主要势力基于“{zh_label(selected_structure)}”结构的故事弧。",
        ]
        prompt1 = "\n".join(prompt1_lines)

        # Save Prompt 1 (Faction Arc Generation)
        prompt1_filepath = save_prompt_to_file(output_dir, "faction_arc_generation_prompt", prompt1)
        if prompt1_filepath:
            self.app.logger.info(f"Faction Arc Generation Prompt (length {len(prompt1)}) saved to: {prompt1_filepath}")
        else:
            self.app.logger.error(f"Failed to save Faction Arc Generation Prompt. Length: {len(prompt1)}.")
            if self.app.logger.isEnabledFor(logging.DEBUG):
                self.app.logger.debug(f"Fallback: Full Faction Arc Generation Prompt:\n{prompt1}")
            else:
                self.app.logger.warning("Faction Arc Generation Prompt not logged directly. Enable DEBUG for full prompt.")
        
        # Send prompt 1 to LLM
        current_backend = get_backend()
        backend_info = f"{current_backend}" if current_backend != "api" else f"api/{selected_model}"
        log_msg_prompt1_source = f"(from {prompt1_filepath})" if prompt1_filepath else "(from memory, save failed)"
        self.app.logger.info(f"Sending Faction Arc Generation Prompt {log_msg_prompt1_source} to LLM ({backend_info})...")
        faction_arcs_response = send_prompt(prompt1, model=selected_model)
        
        if not faction_arcs_response:
            self.app.logger.error(f"Failed to generate faction arcs from LLM ({backend_info}). No response.")
            show_error("错误", "大模型生成势力故事弧失败。")
            return
        
        self.app.logger.info(f"Received faction arcs from LLM. Length: {len(faction_arcs_response)}.")
        # Save the faction arcs response
        write_file(faction_arcs_output_file_path, faction_arcs_response)
        self.app.logger.info(f"Faction arcs content saved to {faction_arcs_output_file_path}")

        # --- Step 2: Reconcile Character and Faction Arcs --- 
        self.app.logger.info("--- Step 2: Reconciling Character and Faction Arcs ---")

        # Build the second prompt for reconciling arcs (USING SELECTED STRUCTURE)
        prompt2_lines = [
             "请整合此前生成的主要人物弧光和主要势力故事弧。",
             "两组故事弧应一致、合理地交织，形成统一的叙事进程。",
             "\n## 人物弧光：",
             char_arcs_content,
             "\n## 势力故事弧：",
             faction_arcs_response, # Use the response from the first prompt
             f"\n请使用“{zh_label(selected_structure)}”框架，写出一条统一的综合故事弧，融合人物与势力两方面的关键发展。",
             "重点表现人物行动如何影响势力事件，以及势力事件如何反过来影响人物。",
             f"现在输出统一的“{zh_label(selected_structure)}”故事弧。",
        ]
        prompt2 = "\n".join(prompt2_lines)
        
        # Save Prompt 2 (Arc Reconciliation)
        prompt2_filepath = save_prompt_to_file(output_dir, "arc_reconciliation_prompt", prompt2)
        if prompt2_filepath:
            self.app.logger.info(f"Arc Reconciliation Prompt (length {len(prompt2)}) saved to: {prompt2_filepath}")
        else:
            self.app.logger.error(f"Failed to save Arc Reconciliation Prompt. Length: {len(prompt2)}.")
            if self.app.logger.isEnabledFor(logging.DEBUG):
                self.app.logger.debug(f"Fallback: Full Arc Reconciliation Prompt:\n{prompt2}")
            else:
                self.app.logger.warning("Arc Reconciliation Prompt not logged directly. Enable DEBUG for full prompt.")
        
        # Send prompt 2 to LLM
        current_backend = get_backend()
        backend_info = f"{current_backend}" if current_backend != "api" else f"api/{selected_model}"
        log_msg_prompt2_source = f"(from {prompt2_filepath})" if prompt2_filepath else "(from memory, save failed)"
        self.app.logger.info(f"Sending Arc Reconciliation Prompt {log_msg_prompt2_source} to LLM ({backend_info})...")
        reconciled_response = send_prompt(prompt2, model=selected_model)
        
        if not reconciled_response:
            self.app.logger.error(f"Failed to reconcile arcs using LLM ({backend_info}). No response.")
            show_error("错误", "大模型整合故事弧失败。")
            return
        
        self.app.logger.info(f"Received reconciled arcs from LLM. Length: {len(reconciled_response)}.")
        # Save the reconciled arcs
        write_file(reconciled_output_file_path, reconciled_response)
        self.app.logger.info(f"Reconciled story arcs content saved to {reconciled_output_file_path}")

    def _add_planets_to_arcs(self, ui):
        selected_model = ui.model
        output_dir = ui.output_dir
        os.makedirs(output_dir, exist_ok=True)
        
        params = ui.parameters
        current_genre = params.get("genre", "Sci-Fi")
        location_type_name = "地点"
        
        self.app.logger.info(f"Adding {location_type_name} to Arcs. Model: {selected_model}, Output Dir: {output_dir}")
        
        # Define file paths
        reconciled_arcs_file_path = os.path.join(output_dir, "story", "structure", "reconciled_arcs.md")
        factions_json_file_path = os.path.join(output_dir, "story", "lore", "factions.json")
        output_file_path = os.path.join(output_dir, "story", "planning", "reconciled_locations_arcs.md")
        
        try:
            reconciled_arcs_content = open_file(reconciled_arcs_file_path)
        except FileNotFoundError:
            show_error("错误", f"找不到整合后的故事弧文件：{reconciled_arcs_file_path}，无法继续。")
            return

        # Load factions and extract relevant location data using genre handler
        location_faction_info = []
        try:
            if not os.path.exists(factions_json_file_path):
                raise FileNotFoundError
            with open(factions_json_file_path, 'r', encoding='utf-8') as f:
                factions_data = json.load(f)
            
            for faction in factions_data if isinstance(factions_data, list) else []:
                if not isinstance(faction, dict):
                    continue
                faction_name = faction.get("faction_name") or faction.get("name") or "未知势力"
                # 势力卡统一 schema 之后，地点就在 territory 这一栏；旧项目里
                # 科幻的 systems、奇幻的 regions 仍然照顾到。
                location_name = faction.get("territory")
                if isinstance(location_name, dict):
                    location_name = location_name.get("name")
                if not location_name:
                    for key, inner, label in (
                        ("systems", "habitable_planets", "星系"),
                        ("regions", "cities", "区域"),
                    ):
                        for group in faction.get(key, []) or []:
                            items = group.get(inner, []) if isinstance(group, dict) else []
                            if items:
                                location_name = (
                                    f"{items[0].get('name', '未知地点')}，"
                                    f"位于{group.get('name', '未知' + label)}"
                                )
                                break
                        if location_name:
                            break
                if location_name:
                    location_faction_info.append(f"- {location_name}（由 {faction_name} 控制）")

        except FileNotFoundError:
             show_error("错误", f"找不到势力文件：{factions_json_file_path}，无法提取地点信息。")
             return
        except json.JSONDecodeError:
             show_error("错误", f"解析势力 JSON 文件失败：{factions_json_file_path}")
             return
             
        if not location_faction_info:
            show_warning("警告", "无法从 factions.json 提取相关地点/势力信息。")
            location_list_str = "没有可用的具体地点数据。"
        else:
            location_list_str = "\n".join(location_faction_info)

        # Build the prompt (genre-agnostic)
        prompt_lines = [
            f"我正在创作一部{zh_label(current_genre)}小说，需要把具体地点融入故事弧。",
            "下方先给出已经整合的人物与势力故事弧，随后列出关键地点及其控制势力。",
            "请重写故事弧，在事件发生处合理融入列表中的地点。",
            "所选地点必须与各部分涉及的势力在逻辑上相符。",
            "不要添加列表以外的地点；尽量保留原有结构和细节，只补充地点语境。",
            "\n## 已整合的故事弧：",
            reconciled_arcs_content,
            "\n## 关键地点及其控制势力：",
            location_list_str,
            "\n请输出融入地点后的修订版故事弧。",
        ]
        prompt = "\n".join(prompt_lines)

        # Save the prompt
        prompt_filepath = save_prompt_to_file(output_dir, "add_locations_to_arcs_prompt", prompt)
        if prompt_filepath:
            self.app.logger.info(f"Add {location_type_name} to Arcs Prompt (length {len(prompt)}) saved to: {prompt_filepath}")
        else:
            self.app.logger.error(f"Failed to save Add {location_type_name} to Arcs Prompt. Length: {len(prompt)}.")
            if self.app.logger.isEnabledFor(logging.DEBUG):
                self.app.logger.debug(f"Fallback: Full Add {location_type_name} to Arcs Prompt:\n{prompt}")
            else:
                self.app.logger.warning(f"Add {location_type_name} to Arcs Prompt not logged directly. Enable DEBUG for full prompt.")

        # Send prompt to LLM
        current_backend = get_backend()
        backend_info = f"{current_backend}" if current_backend != "api" else f"api/{selected_model}"
        log_msg_prompt_source = f"(from {prompt_filepath})" if prompt_filepath else "(from memory, save failed)"
        self.app.logger.info(f"Sending Add {location_type_name} to Arcs Prompt {log_msg_prompt_source} to LLM ({backend_info})...")
        response = send_prompt(prompt, model=selected_model)
        
        if not response:
            self.app.logger.error(f"Failed to get response from LLM ({backend_info}) when adding {location_type_name.lower()}.")
            show_error("错误", "添加地点信息时大模型未返回内容。")
            return

        self.app.logger.info(f"Received response from LLM for adding {location_type_name.lower()}. Length: {len(response)}.")
        # Save the response
        write_file(output_file_path, response)
        self.app.logger.info(f"Story arc with {location_type_name.lower()} saved to {output_file_path}")

    def improve_structure(self, ui):
        selected_model = ui.model
        output_dir = ui.output_dir
        os.makedirs(output_dir, exist_ok=True)
        # print(f"Improving structure with model: {selected_model}, output dir: {output_dir}") # Replaced
        self.app.logger.info(f"Improving structure. Model: {selected_model}, Output Dir: {output_dir}")

        # --- Read Parameters to get selected structure --- 
        parameters_file_path = self.dir_manager.get_parameters_path()
        selected_structure_name = "6-Act Structure" # Default
        story_length = "Novel (Standard)" # Default story length for prompt modification
        params = {}
        if not os.path.exists(parameters_file_path):
            self.app.logger.warning(f"Parameters file not found at {parameters_file_path}. Using default structure: {selected_structure_name}")
        else:
            with open(parameters_file_path, "r", encoding="utf-8") as f:
                for line in f:
                    if ":" in line:
                        key, value = line.split(":", 1)
                        params[key.strip()] = value.strip()
            loaded_structure = params.get("Story Structure")
            story_length = params.get("Story Length", story_length)
            if loaded_structure and loaded_structure.strip():
                selected_structure_name = loaded_structure
            else:
                self.app.logger.warning(f"'Story Structure' not found or empty in {parameters_file_path}. Using default: {selected_structure_name}")
        self.app.logger.info(f"Using selected story structure for improvement: {selected_structure_name} (Story Length: {story_length})")
        # --- End Reading Parameters ---
        domain_profile = resolve_domain_profile(params)

        # --- STRUCTURE_SECTIONS_MAP is now imported from parameters.py ---
        sections_to_iterate = STRUCTURE_SECTIONS_MAP.get(selected_structure_name)

        if not sections_to_iterate:
            show_error("错误", f"结构“{zh_label(selected_structure_name)}”没有定义组成部分，无法继续细化。")
            # print(f"Error: Section definitions not found for structure '{selected_structure_name}'.") # Replaced
            self.app.logger.error(f"Section definitions not found for structure '{selected_structure_name}'. Cannot improve structure.")
            return

        story_structure_path = os.path.join(output_dir, "story", "planning", "reconciled_locations_arcs.md")
        if not os.path.exists(story_structure_path):
            # Try legacy flat structure locations for backward compatibility
            story_structure_path = os.path.join(output_dir, "reconciled_locations_arcs.md")
            if not os.path.exists(story_structure_path):
                story_structure_path = os.path.join(output_dir, "reconciled_planets_arcs.md")
        story_structure_content = open_file(story_structure_path)
        
        
        # print(f"Iterating over sections for '{selected_structure_name}': {sections_to_iterate}") # Replaced
        self.app.logger.info(f"Iterating over sections for '{selected_structure_name}': {sections_to_iterate}")

        previous_section_content = "" # Initialize to store the output of the previous section
        previous_section_name_for_prompt = "" # Store the user-friendly name of the previous section
        section_contracts = []  # 每段的机器可读契约，最后合起来做全书校验

        for i, section_name in enumerate(sections_to_iterate):
            current_section_name_for_prompt = section_name # User-friendly name like "Act I: Setup"
            prompt_lines = [
                f"我正在使用“{zh_label(selected_structure_name)}”框架创作小说，需要细化各部分情节。",
                f"完整框架包含：{', '.join(zh_label(section) for section in sections_to_iterate)}。",
                f"请重点详细扩写 **{zh_label(current_section_name_for_prompt)}**。此处还不是写具体场景，而是为这一部分撰写更详细的情节概述。",
                f"\n## 整体故事结构（来自 {os.path.basename(story_structure_path)}）：\n{story_structure_content}"
            ]

            # Add context from the immediately preceding detailed section (if not the first section)
            if previous_section_content: # i > 0 would also work
                prompt_lines.append(f"\n\n## 紧邻的上一部分“{zh_label(previous_section_name_for_prompt)}”详细内容：\n{previous_section_content}")
                prompt_lines.append(f"\n请确保当前部分“{zh_label(current_section_name_for_prompt)}”既承接上述前文，也符合整体故事结构。")

            # Add novella-specific instruction if applicable
            if story_length == "Novella":
                prompt_lines.append("\n本故事是中篇小说，请控制本部分规模，保持紧凑和聚焦，同时保证本部分的信息完整。")

            prompt_lines.extend([
                f"\n\n请为 **{zh_label(current_section_name_for_prompt)}** 详细说明：\n",
                "- 关键事件和情节发展。\n",
                "- 人物（尤其主要人物）的行动、反应与成长。\n",
                "- 势力目标和冲突如何显现或推进。\n",
                "- 主要行动发生的地点；若焦点转移，请明确说明。\n",
                "- 本部分的整体基调和节奏。\n",
                "重要：不要为故事或本部分另起标题，标题将单独处理。\n",
                "请尽可能详细，以 Markdown 输出，不要使用代码围栏。"
            ])
            prompt = "\n".join(prompt_lines)
            prompt += "\n\n" + structure_contract_instructions(
                section_name=current_section_name_for_prompt,
                section_index=i + 1,
                total_sections=len(sections_to_iterate),
                known_threads=open_threads_after(section_contracts),
                central_conflict_schema=domain_profile.central_conflict_schema,
                known_truths=truths_after(section_contracts),
                known_events=chronology_after(section_contracts),
            )

            safe_structure_name_for_file = selected_structure_name.lower().replace(' ', '_').replace(':', '').replace('/', '_')
            safe_section_name_for_file = current_section_name_for_prompt.lower().replace(' ', '_').replace(':','').replace('/','_')
            prompt_base_name = f"improve_structure_{safe_structure_name_for_file}_{safe_section_name_for_file}_prompt"

            # Save the prompt for this section
            prompt_filepath = save_prompt_to_file(output_dir, prompt_base_name, prompt)
            
            if prompt_filepath:
                self.app.logger.info(f"Prompt for section '{current_section_name_for_prompt}' (length {len(prompt)}) saved to: {prompt_filepath}")
            else:
                self.app.logger.error(f"Failed to save prompt for section '{current_section_name_for_prompt}'. Prompt length: {len(prompt)}.")
                if self.app.logger.isEnabledFor(logging.DEBUG):
                    self.app.logger.debug(f"Fallback: Full prompt for section '{current_section_name_for_prompt}' due to save failure:\n{prompt}")
                else:
                    self.app.logger.warning(f"Prompt for section '{current_section_name_for_prompt}' not logged directly. Enable DEBUG for full prompt.")

            current_backend = get_backend()
            backend_info = f"{current_backend}" if current_backend != "api" else f"api/{selected_model}"
            log_msg_prompt_source = f"(from {prompt_filepath})" if prompt_filepath else "(from memory, save failed)"
            self.app.logger.info(f"Sending prompt for section '{current_section_name_for_prompt}' {log_msg_prompt_source} to LLM ({backend_info})...")
            def _log_contract_retry(attempt, error, section=current_section_name_for_prompt):
                self.app.logger.warning(
                    "Structure contract for '%s' failed validation; retry %s/%s: %s",
                    section,
                    attempt,
                    STRUCTURE_CONTRACT_RETRY_LIMIT,
                    error,
                )

            section_index = i + 1
            orders_taken = chronology_orders_used(section_contracts)
            truths_known = truths_after(section_contracts)
            try:
                response, section_contract = generate_with_contract_retry(
                    lambda request: send_prompt(request, model=selected_model),
                    prompt,
                    lambda text: extract_structure_contract(
                        text,
                        section_index,
                        len(sections_to_iterate),
                        central_conflict_schema=domain_profile.central_conflict_schema,
                        known_orders=orders_taken,
                        known_truths=truths_known,
                    ),
                    retry_limit=STRUCTURE_CONTRACT_RETRY_LIMIT,
                    on_retry=_log_contract_retry,
                )
            except DesignContractError as exc:
                self.app.logger.error(
                    "Section '%s' rejected by its structure contract: %s",
                    current_section_name_for_prompt,
                    exc,
                )
                show_error(
                    "结构契约未通过",
                    f"“{zh_label(current_section_name_for_prompt)}”未通过契约校验：{exc}",
                )
                return

            section_contracts.append(section_contract)

            safe_section_name_for_output = current_section_name_for_prompt.lower().replace(' ', '_').replace(':','').replace('/','_')
            output_filename_base = f"{selected_structure_name.lower().replace(' ', '_')}_{safe_section_name_for_output}.md"
            output_filename_full_path = os.path.join(output_dir, "story", "structure", output_filename_base)
            write_file(output_filename_full_path, response)
            self.app.logger.info(f"Saved details for section '{current_section_name_for_prompt}' to {output_filename_full_path}")

            # Update for the next iteration
            previous_section_content = response
            previous_section_name_for_prompt = current_section_name_for_prompt

        # 逐段校验只能保证单段自洽；悬念有没有人了结要等全部段落齐了才看得出来。
        try:
            validate_structure_sequence(section_contracts)
        except DesignContractError as exc:
            self.app.logger.error("Whole-story structure contract is inconsistent: %s", exc)
            show_error("全书结构不自洽", str(exc))
            return

        write_json(
            os.path.join(output_dir, "story", "structure", "structure_contract.json"),
            {"sections": section_contracts},
        )
        self.app.logger.info("Story structure improvement process complete!")

    def _outline_short_story_plot(self, ui):
        selected_model = ui.model
        output_dir = ui.output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.app.logger.info(f"Outlining Short Story Plot. Model: {selected_model}, Output Dir: {output_dir}")

        # --- Read Parameters ---
        if not (self.app and hasattr(self.app, 'param_ui')):
            self.app.logger.error("ParametersUI not available for short story plot outlining.")
            show_error("错误", "无法加载故事参数。")
            return
        
        parameters = ui.parameters
        selected_structure_name = parameters.get("story_structure")
        novel_title = parameters.get("novel_title", "未命名短篇小说")

        if not selected_structure_name:
            self.app.logger.error("No story structure selected in parameters. Cannot outline short story.")
            show_error("错误", "尚未选择故事结构，请先在“作品参数”中选择。")
            return

        # --- Get Structure Sections/Stages ---
        structure_stages = STRUCTURE_SECTIONS_MAP.get(selected_structure_name)
        if not structure_stages:
            self.app.logger.error(f"Definition for structure '{selected_structure_name}' not found in STRUCTURE_SECTIONS_MAP.")
            show_error("错误", f"找不到故事结构“{zh_label(selected_structure_name)}”的定义。")
            return
        stages_list_str = ", ".join(structure_stages)

        # --- Load Context Files (Optional, but good for consistency) ---
        lore_content = "没有可用的世界观内容。"
        reconciled_arcs_content = "没有可用的人物/势力故事弧背景。"
        characters_summary = "没有可用的人物名单。"
        factions_summary = "没有可用的势力概览。"

        lore_path = os.path.join(output_dir, "story", "lore", "generated_lore.md")
        if os.path.exists(lore_path):
            lore_content = open_file(lore_path)
            self.app.logger.info(f"Loaded lore from {lore_path}")

        arcs_path = os.path.join(output_dir, "story", "planning", "reconciled_locations_arcs.md")
        if not os.path.exists(arcs_path):
            arcs_path = os.path.join(output_dir, "reconciled_locations_arcs.md")
            if not os.path.exists(arcs_path):
                arcs_path = os.path.join(output_dir, "reconciled_planets_arcs.md")
        if os.path.exists(arcs_path):
            reconciled_arcs_content = open_file(arcs_path)
            self.app.logger.info(f"Loaded reconciled arcs from {arcs_path}")
        
        # Basic character and faction summaries (can be expanded)
        char_json_path = os.path.join(output_dir, "story", "lore", "characters.json")
        if os.path.exists(char_json_path):
            char_data = read_json(char_json_path)
            if char_data and "characters" in char_data:
                chars = [c.get('name', '无') for c in char_data["characters"]]
                characters_summary = f"主要人物：{', '.join(chars[:5])}{'……' if len(chars) > 5 else ''}（完整名单见 characters.json）。"
            self.app.logger.info(f"Loaded character data for summary from {char_json_path}")

        faction_json_path = os.path.join(output_dir, "story", "lore", "factions.json")
        if os.path.exists(faction_json_path):
            faction_data = read_json(faction_json_path)
            if faction_data:
                factions_list = [
                    f.get('faction_name') or f.get('name') or '无'
                    for f in faction_data
                    if isinstance(f, dict)
                ]
                factions_summary = f"主要势力：{', '.join(factions_list[:3])}{'……' if len(factions_list) > 3 else ''}（完整名单见 factions.json）。"
            self.app.logger.info(f"Loaded faction data for summary from {faction_json_path}")


        # --- Construct the Prompt ---
        prompt_lines = [
            f"请为短篇小说《{novel_title}》规划详细情节。",
            f"故事采用“{zh_label(selected_structure_name)}”框架，各阶段为：{', '.join(zh_label(stage) for stage in structure_stages)}。",
            "请生成一份从开端到结局、覆盖所有阶段的连贯详细情节。\n",
            "每个阶段都要详细说明：\n",
            "  - 关键事件和情节发展。\n",
            "  - 人物（尤其主要人物）的行动、反应与成长。\n",
            "  - 相关势力目标或冲突如何显现和推进。\n",
            "  - 本阶段主要行动发生的地点。\n",
            "  - 本阶段的整体基调和节奏。\n\n",
            "各阶段之间必须自然、连贯地推进，并逐步走向高潮和结局。",
            "\n## 世界观设定：",
            lore_content,
            f"\n## 人物弧光与势力背景（来自 {os.path.basename(arcs_path) if 'arcs_path' in locals() else '整合后的故事弧文件'}，如有）：",
            reconciled_arcs_content,
            "\n## 主要人物摘要：",
            characters_summary,
            "\n## 主要势力摘要：",
            factions_summary,
            "重要：不要在响应中另行生成故事标题，标题将单独处理。\n",
            f"现在请输出《{novel_title}》基于“{zh_label(selected_structure_name)}”的完整详细情节，合并为一份 Markdown 文档。",
        ]
        prompt = "\n".join(prompt_lines)

        safe_structure_name_for_file = selected_structure_name.lower().replace(' ', '_').replace(':', '').replace('/', '_')
        prompt_base_name = f"outline_short_story_plot_{safe_structure_name_for_file}_prompt"
        
        prompt_filepath = save_prompt_to_file(output_dir, prompt_base_name, prompt)
        if prompt_filepath:
            self.app.logger.info(f"Short Story Plot Outline Prompt (length {len(prompt)}) saved to: {prompt_filepath}")
        else:
            self.app.logger.error(f"Failed to save Short Story Plot Outline Prompt. Length: {len(prompt)}.")
            if self.app.logger.isEnabledFor(logging.DEBUG):
                self.app.logger.debug(f"Fallback: Full Short Story Plot Outline Prompt due to save failure:\n{prompt}")
            else:
                self.app.logger.warning("Short Story Plot Outline Prompt not logged directly. Enable DEBUG for full prompt.")

        current_backend = get_backend()
        backend_info = f"{current_backend}" if current_backend != "api" else f"api/{selected_model}"
        log_msg_prompt_source = f"(from {prompt_filepath})" if prompt_filepath else "(from memory, save failed)"
        self.app.logger.info(f"Sending Short Story Plot Outline Prompt {log_msg_prompt_source} to LLM ({backend_info})...")
        
        response = send_prompt(prompt, model=selected_model)

        if not response:
            self.app.logger.error(f"Failed to generate short story plot from LLM ({backend_info}). No response.")
            show_error("错误", "大模型生成短篇情节失败。")
            return
        
        self.app.logger.info(f"Received short story plot from LLM. Length: {len(response)} chars.")
        
        output_filename_base = f"plot_short_story_{safe_structure_name_for_file}.md"
        output_filename_full_path = os.path.join(output_dir, "story", "structure", output_filename_base)
        
        write_file(output_filename_full_path, response)
        self.app.logger.info(f"Short story plot saved successfully to {output_filename_full_path}")
