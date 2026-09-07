# -*- coding: utf-8 -*-
"""Framework-independent lore generation pipeline.

This module contains business generation only: no widgets, event loops, or
message boxes.  Inputs arrive through :class:`StageContext` and output remains
compatible with existing NovelWriter workspaces.
"""
from __future__ import annotations

from core.generation.errors import fail
from core.generation.ai_helper import send_prompt, get_backend
from core.generation.prompt_context import (
    CHARACTER_PROMPT_KEYS,
    format_faction_section,
    format_faction_summary,
)
from core.generation.lore_cast import (
    CAST_RETRY_LIMIT,
    CastError,
    generate_cast,
    generate_factions,
    storage_factions,
)
import json
import os
import logging
from core.generation.helper_fns import open_file, write_file, validate_json_schema, read_json, write_json, validate_json, save_prompt_to_file
from core.generation.design_contract import (
    DesignContractError,
    extract_lore_contract,
    generate_with_contract_retry,
    lore_contract_instructions,
)

LORE_CONTRACT_RETRY_LIMIT = 2

from core.localization import zh_field, zh_label
import random
from datetime import datetime
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


class LorePipeline:
    def __init__(self, app) -> None:
        self.app = app
        self.cancel_token = CancelToken()
        self.dir_manager = get_directory_manager(app.get_output_dir(), use_new_structure=True)

    def _generate_factions(self, ui):
        """势力卡由模型按故事前提直接用中文写，落盘前做确定性校验。

        以前这里调的是 `Generators/` 里按题材写死的英文模板：名字、目标、领地都是
        从十来条英文短语里随机抽的，抽完再把名字换成中文。结果是名字与类型对不上
        （「海陵岸线风险咨询」的 type 写着 Police Department），领地那一栏抽到的
        「Court and legal systems」还被当成地名转成了城市。
        """
        num_factions = ui.get("num_factions")
        params = ui.parameters
        selected_model = ui.model
        output_dir = ui.output_dir

        self.app.logger.info(
            "Generating %s factions for %s / %s",
            num_factions,
            params.get("genre", ""),
            params.get("subgenre", ""),
        )

        def _log_retry(attempt, error):
            self.app.logger.warning(
                "Faction cards failed validation; retry %s/%s: %s",
                attempt, CAST_RETRY_LIMIT, error,
            )

        try:
            factions = generate_factions(
                lambda request: send_prompt(request, model=selected_model),
                params,
                num_factions,
                on_retry=_log_retry,
            )
        except CastError as error:
            self.app.logger.error("Faction generation failed: %s", error, exc_info=True)
            show_error("错误", f"生成势力失败：{error}")
            return

        stored = storage_factions(factions)
        self.app.logger.info(
            "Generated %s factions: %s",
            len(stored),
            "、".join(str(item.get("name", "")) for item in stored),
        )

        lore_dir = self.dir_manager.get_path('lore_dir')
        lore_full_path = os.path.join(output_dir, lore_dir)
        os.makedirs(lore_full_path, exist_ok=True)
        factions_filepath = os.path.join(lore_full_path, "factions.json")
        write_json(factions_filepath, stored)
        self.app.logger.info(f"Saved factions to {factions_filepath}")

    def _generate_characters(self, ui):
        """人物卡与人物关系同样由模型按故事前提用中文写，落盘前做确定性校验。

        以前反派的目标是从一张英文短语表里抽的，抽到过「Help solve the case」——
        他和主角想要的是同一件事。现在反派必须显式写明他挡的是哪位主角的哪个目标，
        写不出来就退回重写。
        """
        num_chars = ui.get("num_chars")
        params = ui.parameters
        selected_model = ui.model
        output_dir = ui.output_dir
        female_percentage = params.get("female_percentage", 50)

        lore_dir = self.dir_manager.get_path('lore_dir')
        lore_full_path = os.path.join(output_dir, lore_dir)
        os.makedirs(lore_full_path, exist_ok=True)

        # 人物要落在已有的势力里，所以先把势力读回来当上下文。
        factions = []
        factions_filepath = os.path.join(lore_full_path, "factions.json")
        if os.path.exists(factions_filepath):
            try:
                loaded = read_json(factions_filepath)
                factions = loaded if isinstance(loaded, list) else loaded.get("factions", [])
            except (ValueError, IOError) as error:
                self.app.logger.warning("Could not read factions for character context: %s", error)

        self.app.logger.info(
            "Generating %s characters (female %s%%) for %s",
            num_chars, female_percentage, params.get("genre", ""),
        )

        def _log_retry(attempt, error):
            self.app.logger.warning(
                "Character cards failed validation; retry %s/%s: %s",
                attempt, CAST_RETRY_LIMIT, error,
            )

        try:
            cast = generate_cast(
                lambda request: send_prompt(request, model=selected_model),
                params,
                num_chars,
                female_percentage=female_percentage,
                factions=factions,
                on_retry=_log_retry,
            )
        except CastError as error:
            self.app.logger.error("Character generation failed: %s", error, exc_info=True)
            show_error("错误", f"生成人物失败：{error}")
            return

        characters = cast["characters"]
        payload = {
            "characters": characters,
            "relationships": cast.get("relationships", []),
            "metadata": {
                "generation_date": datetime.now().isoformat(),
                "total_characters": len(characters),
                "genre": params.get("genre", ""),
                "subgenre": params.get("subgenre", ""),
            },
        }

        women = sum(1 for c in characters if str(c.get("gender", "")).strip() == "女")
        self.app.logger.info(
            "Generated %s characters (%s female, %.0f%%; asked for %s%%): %s",
            len(characters), women,
            (women / len(characters) * 100) if characters else 0.0,
            female_percentage,
            "、".join(str(c.get("name", "")) for c in characters),
        )

        characters_filepath = os.path.join(lore_full_path, "characters.json")
        write_json(characters_filepath, payload)
        self.app.logger.info(f"Saved characters to {characters_filepath}")

    def _generate_lore(self, ui):
        """Generate lore using an internally constructed prompt and LLM"""
        self.app.logger.info("Lore generation process started.")
        selected_model = ui.model
        output_dir = ui.output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.app.logger.info(f"Using model: {selected_model} for lore generation.")
        self.app.logger.info(f"Output directory for lore files: {output_dir}")

        prompts_subdir = os.path.join(output_dir, "system", "prompts")
        os.makedirs(prompts_subdir, exist_ok=True)
        self.app.logger.info(f"Ensured prompts subdirectory exists at: {prompts_subdir}")

        # --- Load necessary data ---
        parameters_txt_path = os.path.join(output_dir, "system", "parameters.txt")
        characters_json_path = os.path.join(output_dir, "story", "lore", "characters.json")
        factions_json_path = os.path.join(output_dir, "story", "lore", "factions.json")

        story_params = {}
        if os.path.exists(parameters_txt_path):
            params_content = open_file(parameters_txt_path)
            for line in params_content.splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    story_params[key.strip()] = value.strip()
            self.app.logger.info(f"Loaded parameters from {parameters_txt_path}")
        else:
            self.app.logger.warning(f"Parameters file not found: {parameters_txt_path}. Proceeding without detailed parameters.")


        characters = []
        try:
            all_character_data = read_json(characters_json_path)
            characters = all_character_data.get("characters", [])
            if not characters:
                self.app.logger.warning(f"No characters found in {characters_json_path}")
            else:
                self.app.logger.info(f"Loaded {len(characters)} characters from {characters_json_path}")
        except FileNotFoundError:
            self.app.logger.warning(f"Character file not found: {characters_json_path}")
        except (json.JSONDecodeError, ValueError) as e:
            self.app.logger.error(f"Error loading character data from {characters_json_path}: {e}", exc_info=True)

        factions = []
        try:
            factions_data = read_json(factions_json_path)
            if factions_data:
                # Handle both direct list format and wrapped format
                if isinstance(factions_data, list):
                    # Direct list format (used by some genres)
                    factions = factions_data
                elif isinstance(factions_data, dict) and "factions" in factions_data:
                    # Wrapped format (used by Horror and potentially other genres)
                    factions = factions_data["factions"]
                else:
                    # Fallback: try to use the data as-is if it's a dict with faction-like structure
                    factions = factions_data if isinstance(factions_data, list) else []
                
                self.app.logger.info(f"Loaded {len(factions)} factions from {factions_json_path}")
            else:
                self.app.logger.warning(f"No factions found or empty data in {factions_json_path}")
        except FileNotFoundError:
            self.app.logger.warning(f"Faction file not found: {factions_json_path}")
        except (json.JSONDecodeError, ValueError) as e:
            self.app.logger.error(f"Error loading faction data from {factions_json_path}: {e}", exc_info=True)

        # --- Step 1: Construct the base prompt ---
        self.app.logger.info("Constructing base lore prompt...")
        prompt_lines = [
            "请为一部新故事创建基础世界观设定。",
            "根据提供的参数、人物摘要和势力摘要，生成丰富而详细的故事世界背景。",
            # "- Key historical events.",
            # "- Cultural details.",
            # "- Technological level and unique aspects.",
            # "- Potential conflicts and mysteries.",
            # "- Initial plot points or hooks.",
            "请确保世界观与所有已提供的信息一致。"
        ]

        prompt_lines.append("\n## 故事参数：")
        if story_params:
            for key, value in story_params.items():
                prompt_lines.append(f"- {key}: {value}")
        else:
            prompt_lines.append("- 未加载参数")

        prompt_lines.append("\n## 人物摘要：")
        if characters:
            for char_dict in characters:
                name = char_dict.get('name', '未知人物')
                role = char_dict.get('role', '未知角色')
                prompt_lines.append(f"- {name} ({role})")
        else:
            prompt_lines.append("- 未加载人物")

        prompt_lines.append("\n## 势力摘要：")
        prompt_lines.append("\n请重点处理前两个势力，其他势力稍后再处理。\n")
        if factions:
            prompt_lines.append(format_faction_summary(factions))
        else:
            prompt_lines.append("- 未加载势力")
        
        # Initial prompt content is now built
        prompt = "\n".join(prompt_lines)
        
        # --- Step 2: Enhance the prompt with detailed character and faction information ---
        self.app.logger.info("Enhancing prompt with faction capitals and detailed character info...")
        if factions:
            # Get current genre and appropriate handler
            params = ui.parameters
            current_genre = params.get("genre", "Sci-Fi")
            
            prompt += format_faction_section(factions)

        if characters:
            # Add a detailed character section to the prompt
            character_section = "\n## 人物详细信息：\n"
            
            # Sort characters by role priority
            role_priority = {"protagonist": 0, "deuteragonist": 1, "antagonist": 2}
            # Ensure characters is a list of dicts here when loaded from JSON
            sorted_chars = sorted(characters, key=lambda x: role_priority.get(x.get("role", "").lower(), 99))
            
            for char_dict in sorted_chars: # char_dict is a dictionary from characters.json
                char_name = char_dict.get('name', 'Unknown') # Use .get() for dict
                char_role = char_dict.get('role', '未知角色') # Use .get() for dict
                char_section_detail = f"\n### {char_name} ({char_role}):\n"
                
                # Add basic information
                basic_info = []
                basic_keys = CHARACTER_PROMPT_KEYS
                for key in basic_keys:
                    value = char_dict.get(key)
                    if value:
                        if isinstance(value, list):
                            basic_info.append(f"- {zh_field(key)}：{', '.join(value)}")
                        else:
                            basic_info.append(f"- {zh_field(key)}：{value}")
                
                # Add character traits (this section is now redundant since basic_keys already includes these)
                traits = []
                
                family_data = char_dict.get('family', {})
                if family_data:
                    family_info_list = ["- 家庭："]
                    parents = family_data.get('parents', [])
                    if parents:
                        parents_str = ", ".join([f"{p.get('name', '无')} ({p.get('relation', '无')}, {p.get('gender', '无')}, {p.get('status', '无')})"
                                               for p in parents])
                        family_info_list.append(f"  - 父母：{parents_str}")
                    siblings = family_data.get('siblings', [])
                    if siblings:
                        siblings_str = ", ".join([f"{s.get('name', '无')} ({s.get('relation', '无')}, {s.get('gender', '无')})"
                                                for s in siblings])
                        family_info_list.append(f"  - 兄弟姐妹：{siblings_str}")
                    spouse = family_data.get('spouse')
                    if spouse and isinstance(spouse, dict):
                        family_info_list.append(f"  - 配偶：{spouse.get('name', '无')}（{spouse.get('gender', '无')}）")
                    children_val = family_data.get('children', [])
                    if children_val:
                        children_str = ", ".join([f"{c.get('name', '无')} ({c.get('relation', '无')}, {c.get('gender', '无')})"
                                                for c in children_val])
                        family_info_list.append(f"  - 子女：{children_str}")
                    # Only extend traits if family_info_list has more than just the "- Family:" header
                    if len(family_info_list) > 1:
                         traits.extend(family_info_list)
                
                # Combine all information
                char_section_detail += "\n".join(basic_info + traits)
                character_section += char_section_detail

            # Add the character section to the prompt
            prompt += character_section
            
        prompt += "\n\n## 最终要求：\n请确保生成的世界观与上述人物细节和势力信息一致，尤其注意性别、关系和个人背景。在构建更广阔的世界背景时，应保留并尊重这些属性。"
        prompt += "\n\n重要：只生成所需的世界观、背景和初始情节点，不要在本次响应中生成故事标题；标题将单独生成和管理。"
        prompt += "\n\n现在生成世界观设定："
        prompt += "\n\n" + lore_contract_instructions()

        # --- Step 3: Save the final assembled prompt to a single, non-timestamped file ---
        main_lore_prompt_filepath = os.path.join(prompts_subdir, "main_lore_prompt.md")
        try:
            write_file(main_lore_prompt_filepath, prompt)
            self.app.logger.info(f"Definitive Main Lore Generation Prompt (length {len(prompt)}) saved to: {main_lore_prompt_filepath}")
        except IOError as e_write:
            self.app.logger.error(f"Failed to write Main Lore Generation Prompt to {main_lore_prompt_filepath}: {e_write}", exc_info=True)
            show_error("错误", f"保存世界观 Prompt 失败：{main_lore_prompt_filepath}")
            return False # Stop if we can't save the prompt

        # --- Step 4: Send the enhanced prompt to the LLM ---
        current_backend = get_backend()
        backend_info = f"{current_backend}" if current_backend != "api" else f"api/{selected_model}"
        self.app.logger.info(f"Sending main lore prompt from {main_lore_prompt_filepath} to LLM ({backend_info})...")
        def _log_contract_retry(attempt, error):
            self.app.logger.warning(
                "Lore contract failed validation; retry %s/%s: %s",
                attempt,
                LORE_CONTRACT_RETRY_LIMIT,
                error,
            )

        try:
            lore_text, lore_contract = generate_with_contract_retry(
                lambda request: send_prompt(request, model=selected_model),
                prompt,
                extract_lore_contract,
                retry_limit=LORE_CONTRACT_RETRY_LIMIT,
                on_retry=_log_contract_retry,
            )
        except DesignContractError as exc:
            self.app.logger.error("Lore rejected by its own contract: %s", exc)
            show_error("世界观契约未通过", str(exc))
            return False

        self.app.logger.info(f"Lore successfully generated by LLM. Response length: {len(lore_text)} chars.")
        # --- Step 5: Save the generated lore ---
        self.app.logger.info("Saving generated lore...")
        
        # Use structured directory for generated lore file
        lore_dir = self.dir_manager.get_path('lore_dir')
        lore_full_path = os.path.join(output_dir, lore_dir)
        os.makedirs(lore_full_path, exist_ok=True)
        generated_lore_filepath = os.path.join(lore_full_path, "generated_lore.md")
        write_file(generated_lore_filepath, lore_text)
        # 人名、地点和势力的规范名登记表：后续阶段据此判断「同一个对象」。
        write_json(os.path.join(lore_full_path, "lore_contract.json"), lore_contract)

        self.app.logger.info(f"Lore successfully generated and saved to {generated_lore_filepath}")
        # show_success("Success", f"Lore generated and saved to {generated_lore_filepath}.\n\nPrompt used is in {main_lore_prompt_filepath}")
        return True

    def _suggest_titles(self, ui):
        self.app.logger.info("Title suggestion process started.")
        selected_model = ui.model
        output_dir = ui.output_dir # This is typically "current_work"
        # The save_prompt_to_file function will handle creating the 'prompts' subdirectory within output_dir.
        # os.makedirs(os.path.join(output_dir, "prompts"), exist_ok=True) # Ensured by save_prompt_to_file

        lore_file_path = os.path.join(output_dir, "story", "lore", "generated_lore.md")
        params_file_path = os.path.join(output_dir, "system", "parameters.txt")

        if not os.path.exists(lore_file_path):
            self.app.logger.error(f"Lore file not found at {lore_file_path}. Cannot suggest titles.")
            show_error("错误", f"找不到世界观文件（{lore_file_path}），请先生成世界观。")
            return
        lore_content = open_file(lore_file_path)
        self.app.logger.info(f"Loaded lore content from {lore_file_path} for title suggestion.")

        # 2. Load Parameters (for genre, subgenre, themes - optional but good context)
        story_genre = "fiction"  # Default
        story_subgenre = ""    # Default
        story_themes = ""      # Default to empty, will be updated if themes are found

        if os.path.exists(params_file_path):
            params_content = open_file(params_file_path)
            current_params = {}
            for line in params_content.splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    current_params[key.strip().lower().replace(' ','_')] = value.strip()
            story_genre = current_params.get('genre', story_genre)
            story_subgenre = current_params.get('subgenre', story_subgenre)
            story_themes = current_params.get('theme', '').strip()
            self.app.logger.info(f"Loaded parameters for title context: Genre='{story_genre}', Subgenre='{story_subgenre}', Theme='{story_themes}'.")
        else:
            self.app.logger.warning(f"Parameters file not found at {params_file_path}. Proceeding without theme/genre context for titles.")

        # 3. Construct the prompt for title suggestions
        # Use full lore content, no truncation
        lore_for_prompt = lore_content

        prompt_lines = [
            f"以下是{zh_label(story_subgenre)}{zh_label(story_genre)}故事的世界观设定。请根据这些设定以及列出的主题（如有），推荐 5—10 个可用标题。",
            "请使用简单的编号列表，每行一个标题。\n\n例如：\n",
            "1. 标题一\n",
            "2. 另一个好标题\n",
            "3. 最后的建议\n",
            "",
            "## 故事世界观：",
            lore_for_prompt, # Using full lore
            ""
        ]
        # Conditionally add the "Key Themes" section
        if story_themes and story_themes.lower() != "not specified":
            prompt_lines.append(f"## 关键主题：{story_themes}")
            prompt_lines.append("")

        prompt_lines.append("响应中只提供标题编号列表：")
        title_prompt_content = "\n".join(prompt_lines)

        # 4. Save the title suggestion prompt
        title_prompt_base_name = "title_suggestion_prompt"
        # Pass output_dir directly; save_prompt_to_file will place it in the 'prompts' subfolder by default.
        title_prompt_filepath = save_prompt_to_file(output_dir, title_prompt_base_name, title_prompt_content)

        if title_prompt_filepath:
            self.app.logger.info(f"Title suggestion prompt (length {len(title_prompt_content)}) saved to: {title_prompt_filepath}")
        else:
            self.app.logger.error(f"Failed to save title suggestion prompt. Length: {len(title_prompt_content)}.")
            # Fallback logging if needed (similar to other prompt saves)
            if self.app.logger.isEnabledFor(logging.DEBUG):
                self.app.logger.debug(f"Fallback: Full title suggestion prompt:\n{title_prompt_content}")
            else:
                self.app.logger.warning("Title suggestion prompt content not logged. Enable DEBUG for full prompt.")

        # 5. Send to LLM
        log_msg_prompt_source = f"(from {title_prompt_filepath})" if title_prompt_filepath else "(from memory, save failed)"
        current_backend = get_backend()
        backend_info = f"{current_backend}" if current_backend != "api" else f"api/{selected_model}"
        self.app.logger.info(f"Sending title suggestion prompt {log_msg_prompt_source} to LLM (backend: {backend_info})...")
        suggested_titles_text = send_prompt(title_prompt_content, model=selected_model)

        if not suggested_titles_text:
            self.app.logger.error("Failed to get title suggestions from LLM.")
            show_error("错误", "无法从大模型获取标题建议。")
            return

        self.app.logger.info(f"Received title suggestions from LLM. Length: {len(suggested_titles_text)}.")

        # 6. Save suggested titles to a file
        # Use structured directory for suggested titles file
        planning_dir = self.dir_manager.get_path('planning_dir')
        planning_full_path = os.path.join(output_dir, planning_dir)
        os.makedirs(planning_full_path, exist_ok=True)
        suggested_titles_filepath = os.path.join(planning_full_path, "suggested_titles.md")
        try:
            write_file(suggested_titles_filepath, suggested_titles_text)
            self.app.logger.info(f"Suggested titles saved to: {suggested_titles_filepath}")
            # show_success("Success", f"Title suggestions have been saved to:\n{suggested_titles_filepath}\n\nPlease review this file and then update the Novel Title in the Parameters tab.")
        except IOError as e_write:
            self.app.logger.error(f"Failed to write suggested titles to {suggested_titles_filepath}: {e_write}", exc_info=True)

    def _main_character_enhancement(self, ui):
        self.app.logger.info("Main character enhancement process started.")
        selected_model = ui.model 
        output_dir = ui.output_dir
        os.makedirs(output_dir, exist_ok=True)

        self.app.logger.info(f"Using model: {selected_model} for main character enhancement")
        self.app.logger.info(f"Output directory for character files: {output_dir}")

        # Construct full paths for input files using structured directories
        characters_json_path = os.path.join(output_dir, "story", "lore", "characters.json")
        generated_lore_path = os.path.join(output_dir, "story", "lore", "generated_lore.md")

        try:
            lore_content = open_file(generated_lore_path)
            self.app.logger.info(f"Loaded lore content from {generated_lore_path}. Length: {len(lore_content)} chars.")
        except FileNotFoundError:
            self.app.logger.warning(f"Lore file {generated_lore_path} not found. Proceeding without lore context for backstories.")
            lore_content = "没有可用的整体世界观背景。"
        
        # Load character data
        try:
            all_character_data = read_json(characters_json_path)
            characters = all_character_data.get("characters", [])
            if not characters:
                self.app.logger.warning(f"No characters found in {characters_json_path}")
                characters = []
            else:
                self.app.logger.info(f"Loaded {len(characters)} characters for enhancement from {characters_json_path}")
                # self.app.logger.debug(f"Raw characters loaded from JSON: {characters}") # ADDED: Log all loaded characters
        except FileNotFoundError:
            self.app.logger.error(f"Character file {characters_json_path} not found. Cannot enhance.", exc_info=True)
            show_error("错误", f"找不到人物文件：{characters_json_path}")
            return
        except (json.JSONDecodeError, ValueError) as e:
            self.app.logger.error(f"Error decoding JSON from {characters_json_path}: {e}. Cannot enhance.", exc_info=True)
            show_error("错误", f"解析人物 JSON 文件失败（{characters_json_path}）：{e}")
            return

        # Identify and sort main characters (Protagonist, Deuteragonist, Antagonist)
        main_roles = ["protagonist", "deuteragonist", "antagonist"]
        main_chars_data = [c for c in characters if c.get("role", "").lower() in main_roles]
        role_priority = {"protagonist": 0, "deuteragonist": 1, "antagonist": 2}
        main_chars_data.sort(key=lambda x: role_priority.get(x.get("role", "").lower(), 99))

        if not main_chars_data:
            self.app.logger.warning("No Protagonist, Deuteragonist, or Antagonist found in characters.json for enhancement.")
            show_warning("警告", "characters.json 中没有找到主角、第二主角或反派。")
            return

        self.app.logger.info(f"Found main characters for enhancement (Count: {len(main_chars_data)}): {[c.get('name', 'NAME N/A') for c in main_chars_data]}")
        # self.app.logger.debug(f"Full main_chars_data content before loop: {main_chars_data}") # ADDED: Log full list before loop

        # --- Loop through main characters to generate backstories ---
        generated_backstories = {} # Store generated backstories

        for i, char_data_item in enumerate(main_chars_data): # Changed char_data to char_data_item and used enumerate
            self.app.logger.info(f"--- Iteration {i} for main character enhancement ---") # ADDED: Iteration log
            self.app.logger.debug(f"Processing char_data_item (type: {type(char_data_item)}): {char_data_item}") # ADDED: Log current item and its type
            
            char_name = char_data_item.get('name', 'Unknown Character') 
            char_role = char_data_item.get('role', 'Unknown Role')
            self.app.logger.info(f"Extracted - Name: '{char_name}', Role: '{char_role}'") # ADDED: Log extracted name/role

            self.app.logger.info(f"--- Generating backstory for: {char_name} ({char_role}) ---") # Original log line

            # Get current genre for appropriate prompt
            params = ui.parameters
            current_genre = params.get("genre", "Sci-Fi")
            current_subgenre = params.get("subgenre", "")
            
            # Build the prompt
            genre_text = f"{current_subgenre} {current_genre}" if current_subgenre else current_genre
            prompt_lines = [
                f"我正在创作一部{zh_label(current_subgenre)}{zh_label(current_genre)}小说，需要完善关键人物 {char_name}（{zh_label(char_role.capitalize())}）的背景故事。",
                "请生成详细背景，涵盖其家庭、成长经历、重大人生事件，以及其如何成为故事开始时的自己。",
                "内容应符合整体世界观和已提供的人物信息，包括年龄、性别和家庭成员。"
            ]

            # Add overall lore
            prompt_lines.append("\n## 整体世界观：")
            prompt_lines.append(lore_content)

            # Add current character details
            prompt_lines.append(f"\n## {char_name}（{zh_label(char_role.capitalize())}）的详细信息：")
            
            # Add basic character information using dict.get()
            character_keys = CHARACTER_PROMPT_KEYS
            for key in character_keys:
                value = char_data_item.get(key)
                if value:
                    if isinstance(value, list):
                        prompt_lines.append(f"- {zh_field(key)}：{', '.join(value)}")
                    else:
                        prompt_lines.append(f"- {zh_field(key)}：{value}")

            # Add formatted family details using dict.get()
            family_data = char_data_item.get('family', {})
            if family_data: # Check if family_data itself is not empty
                prompt_lines.append("- 家庭：")
                parents = family_data.get('parents', [])
                if parents:
                    parents_str = ", ".join([f"{p.get('name', '无')} ({p.get('relation', '无')}, {p.get('gender', '无')}, {p.get('status', '无')})"
                                           for p in parents])
                    prompt_lines.append(f"  - 父母：{parents_str}")
                
                siblings = family_data.get('siblings', [])
                if siblings:
                    siblings_str = ", ".join([f"{s.get('name', '无')} ({s.get('relation', '无')}, {s.get('gender', '无')})"
                                            for s in siblings])
                    prompt_lines.append(f"  - 兄弟姐妹：{siblings_str}")

                spouse = family_data.get('spouse') # Can be a dict or None
                if spouse and isinstance(spouse, dict):
                    prompt_lines.append(f"  - 配偶：{spouse.get('name', '无')}（{spouse.get('gender', '无')}）")

                children = family_data.get('children', [])
                if children:
                    children_str = ", ".join([f"{c.get('name', '无')} ({c.get('relation', '无')}, {c.get('gender', '无')})"
                                            for c in children])
                    prompt_lines.append(f"  - 子女：{children_str}")

            # Add previously generated backstories for context
            if generated_backstories:
                prompt_lines.append("\n## 其他主要人物背景（供衔接参考）：")
                for name, story in generated_backstories.items():
                    prompt_lines.append(f"### {name} 的背景故事：")
                    prompt_lines.append(story)
                    prompt_lines.append("\n---\n")
                prompt_lines.append(f"\n请确保为 {char_name} 生成的背景与这些既有背景一致或互补，并形成潜在联系或对照。")

            # Final instruction
            prompt_lines.append("\n现在生成背景故事：")
            prompt = "\n".join(prompt_lines)
            # self.app.logger.debug(f"Backstory prompt for {char_name} (length: {len(prompt)} chars):\n{prompt}") # Old direct logging

            # Save the prompt for this character's backstory to a file
            prompt_base_name = f"background_{char_role.lower().replace(' ', '_').replace('/', '_').replace(':', '_')}_{char_name.lower().replace(' ', '_').replace('/', '_').replace(':', '_')}_prompt"
            prompt_filepath = save_prompt_to_file(output_dir, prompt_base_name, prompt)

            if prompt_filepath:
                self.app.logger.info(f"Backstory prompt for {char_name} (length {len(prompt)}) saved to: {prompt_filepath}")
            else:
                self.app.logger.error(f"Failed to save backstory prompt for {char_name} to a file. Prompt length: {len(prompt)}.")
                if self.app.logger.isEnabledFor(logging.DEBUG):
                    self.app.logger.debug(f"Fallback: Full backstory prompt for {char_name} due to save failure:\n{prompt}")
                else:
                    self.app.logger.warning(f"Backstory prompt content for {char_name} not logged directly due to length and save failure. Enable DEBUG for full prompt.")

            # Send prompt to LLM
            log_msg_prompt_source = f"(from {prompt_filepath})" if prompt_filepath else "(from memory, save failed)"
            current_backend = get_backend()
            backend_info = f"{current_backend}" if current_backend != "api" else f"api/{selected_model}"
            self.app.logger.info(f"Sending backstory prompt for {char_name} {log_msg_prompt_source} to LLM (backend: {backend_info})...")
            response = send_prompt(prompt, model=selected_model)

            if not response:
                self.app.logger.warning(f"Failed to get backstory from LLM for {char_name}. Skipping.")
                continue
            
            self.app.logger.info(f"Received backstory for {char_name}. Length: {len(response)} chars.")
            # Save the generated backstory
            # Sanitize char_role and char_name for the filename to avoid issues with spaces or special characters
            safe_char_role = char_role.lower().replace(' ', '_').replace('/', '_').replace(':', '_')
            safe_char_name = char_name.lower().replace(' ', '_').replace('/', '_').replace(':', '_')
            base_filename = f"background_{safe_char_role}_{safe_char_name}.md"
            
            # Use structured directory for background files
            lore_dir = self.dir_manager.get_path('lore_dir')
            lore_full_path = os.path.join(output_dir, lore_dir)
            os.makedirs(lore_full_path, exist_ok=True)
            background_filepath = os.path.join(lore_full_path, base_filename)
            write_file(background_filepath, response)
            self.app.logger.info(f"Saved background for {char_name} to {background_filepath}")
            
            # Store for next iteration's context
            generated_backstories[char_name] = response

        self.app.logger.info("--- Main character enhancement process complete! ---")
