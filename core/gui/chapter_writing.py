from tkinter import ttk, messagebox
from core.gui.notifications import show_success, show_error
from core.generation.ai_helper import send_prompt, get_backend
import re
from core.generation.helper_fns import (
    open_file,
    write_file,
    save_prompt_to_file,
    read_json,
    parse_scene_sections,
)
from core.generation.prompt_context import (
    CHINESE_PROSE_REQUIREMENTS,
    analyze_chinese_prose_style,
    build_location_guidance,
    build_story_parameter_lines,
    find_scene_world_conflicts,
    format_faction_summary,
    format_genre_label,
    is_legal_suspense,
    normalize_story_parameters,
    sanitize_lore_content,
)
from core.generation.chapter_generation_loop import ChapterGenerationLoop
from core.generation.story_ledger import compact_json
import os
from core.gui.parameters import STRUCTURE_SECTIONS_MAP # Import for section mapping
from core.localization import zh_label

class ChapterWriting:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app  # Store the app instanc
        
        # Frame setup for chapter writing UI
        self.chapter_writing_frame = ttk.Frame(parent)
        self.chapter_writing_frame.pack(expand=True, fill="both")

        # Title Label
        self.title_label = ttk.Label(self.chapter_writing_frame, text="撰写章节", font=("Helvetica", 16))
        self.title_label.pack(pady=10)

        # Entry to select chapter number
        self.chapter_label = ttk.Label(self.chapter_writing_frame, text="输入章节编号：")
        self.chapter_label.pack()
        self.chapter_number_entry = ttk.Entry(self.chapter_writing_frame)
        self.chapter_number_entry.pack(pady=5)

        # Button to write chapter/story - command will be set to dispatcher
        self.write_prose_button = ttk.Button(self.chapter_writing_frame, text="开始撰写", command=self._dispatch_prose_generation)
        self.write_prose_button.pack(pady=20)

        # Button to re-write chapter - initially hidden
        self.rewrite_button = ttk.Button(self.chapter_writing_frame, text="重写", command=self.rewrite_chapter)
        # self.rewrite_button.pack(pady=20) # Keep hidden for now
        self.rewrite_button.pack_forget() # Explicitly hide

        # Separator for automation section
        separator = ttk.Separator(self.chapter_writing_frame, orient='horizontal')
        separator.pack(fill='x', pady=20)

        # Automation section label
        automation_label = ttk.Label(self.chapter_writing_frame, text="自动章节写作", font=("Helvetica", 14, "bold"))
        automation_label.pack(pady=(10, 5))

        # Progress display frame
        self.progress_frame = ttk.Frame(self.chapter_writing_frame)
        self.progress_frame.pack(pady=5)
        
        self.progress_label = ttk.Label(self.progress_frame, text="进度：正在分析…")
        self.progress_label.pack()

        # Automation buttons frame
        automation_buttons_frame = ttk.Frame(self.chapter_writing_frame)
        automation_buttons_frame.pack(pady=10)

        # Button to analyze chapters
        self.analyze_button = ttk.Button(automation_buttons_frame, text="📊 分析章节", command=self.analyze_chapters)
        self.analyze_button.pack(side="left", padx=5)

        # Button to write next chapter
        self.write_next_button = ttk.Button(automation_buttons_frame, text="✍️ 撰写下一章", command=self.write_next_chapter)
        self.write_next_button.pack(side="left", padx=5)

        # Button to write all remaining chapters
        self.write_all_button = ttk.Button(automation_buttons_frame, text="🚀 撰写全部章节", command=self.write_all_chapters)
        self.write_all_button.pack(side="left", padx=5)

        # Initialize progress display
        self.update_progress_display()

        # Register callback and call for initial setup
        if self.app and hasattr(self.app, 'param_ui') and hasattr(self.app.param_ui, 'add_callback'):
            self.app.param_ui.add_callback(self._update_ui_based_on_parameters)
        self._update_ui_based_on_parameters() # Set initial UI state

    def _update_ui_based_on_parameters(self):
        """Updates UI elements based on current story parameters."""
        if not (self.app and hasattr(self.app, 'param_ui') and 
                hasattr(self.app.param_ui, 'get_current_parameters') and 
                callable(self.app.param_ui.get_current_parameters)):
            if self.app and hasattr(self.app, 'logger'):
                self.app.logger.info("ChapterWriting: Parameters.py not fully available for UI update.")
            return

        try:
            params = self.app.param_ui.get_current_parameters()
            story_length = params.get("story_length")
            if self.app and hasattr(self.app, 'logger'):
                self.app.logger.info(f"ChapterWriting._update_ui: story_length = '{story_length}'")
        except Exception as e:
            if self.app and hasattr(self.app, 'logger'):
                self.app.logger.error(f"ChapterWriting: Error getting parameters: {e}", exc_info=True)
            return

        if story_length == "Short Story":
            self.chapter_label.pack_forget()
            self.chapter_number_entry.pack_forget()
            self.write_prose_button.config(text="撰写短篇小说")
            self.rewrite_button.pack_forget() # Ensure rewrite is hidden for short story
            if self.app and hasattr(self.app, 'logger'):
                self.app.logger.debug("ChapterWriting: Configured for Short Story.")
        elif story_length in ["Novella", "Novel (Standard)", "Novel (Epic)"]:
            self.chapter_label.pack(pady=(10,0)) # Re-pack if previously hidden
            self.chapter_number_entry.pack(pady=5) # Re-pack
            self.write_prose_button.config(text="撰写章节")
            # self.rewrite_button.pack(pady=20) # Decide later if rewrite is shown for longer forms
            self.rewrite_button.pack_forget() # Keep hidden for now for long form too
            if self.app and hasattr(self.app, 'logger'):
                self.app.logger.debug(f"ChapterWriting: Configured for {story_length}.")
        else: # Default or unknown
            self.chapter_label.pack(pady=(10,0)) 
            self.chapter_number_entry.pack(pady=5)
            self.write_prose_button.config(text="开始撰写")
            self.rewrite_button.pack_forget()
            if self.app and hasattr(self.app, 'logger'):
                self.app.logger.debug(f"ChapterWriting: Configured for default/unknown story length '{story_length}'.")
                
    def _dispatch_prose_generation(self):
        """Dispatches to the correct prose generation method based on story length."""
        if not (self.app and hasattr(self.app, 'param_ui')):
            self.app.logger.error("ChapterWriting: Parameters.py not available for dispatching prose generation.")
            show_error("错误", "无法确定正文生成所需的故事参数。")
            return

        params = self.app.param_ui.get_current_parameters()
        story_length = params.get("story_length")
        self.app.logger.info(f"ChapterWriting: Dispatching prose generation for story length: {story_length}")

        if story_length == "Short Story":
            self._write_short_story_prose()
        elif story_length in ["Novella", "Novel (Standard)", "Novel (Epic)"]:
            self.write_chapter() # Existing function, to be refactored
        else:
            self.app.logger.error(f"ChapterWriting: Unknown story length '{story_length}' for prose generation.")
            show_error("错误", f"章节写作不支持故事篇幅“{zh_label(story_length)}”。")
            
    def _write_short_story_prose(self):
        """Generates the full prose for a short story, scene by scene."""
        selected_model = self.app.get_selected_model()
        output_dir = self.app.get_output_dir()
        os.makedirs(output_dir, exist_ok=True)
        self.app.logger.info(f"Initiating short story prose generation. Model: {selected_model}, Output Dir: {output_dir}")

        try:
            parameters = self.app.param_ui.get_current_parameters()
            story_params = normalize_story_parameters(parameters)
            genre_label = format_genre_label(story_params)
            novel_title = parameters.get("novel_title", "未命名短篇小说")
            selected_structure_name = parameters.get("story_structure")

            if not selected_structure_name:
                self.app.logger.error("No story structure selected. Cannot determine input file for short story prose.")
                show_error("错误", "作品参数中尚未选择故事结构。")
                return

            # 1. Determine and Read Input File (scene plan for the short story)
            safe_structure_name = selected_structure_name.lower().replace(' ', '_').replace(':', '').replace('/', '_')
            scene_plan_filename = f"scenes_short_story_{safe_structure_name}.md"
            scene_plan_filepath = os.path.join(output_dir, "story", "planning", scene_plan_filename)

            self.app.logger.info(f"Attempting to load scene plan from: {scene_plan_filepath}")
            try:
                scene_plan_content = open_file(scene_plan_filepath)
                if not scene_plan_content.strip():
                    self.app.logger.error(f"Scene plan file '{scene_plan_filename}' is empty.")
                    show_error("错误", f"场景规划文件“{scene_plan_filename}”为空，无法生成正文。")
                    return
            except FileNotFoundError:
                self.app.logger.error(f"Scene plan file not found: {scene_plan_filepath}")
                show_error("错误", f"找不到场景规划文件“{scene_plan_filename}”，请先在“场景规划”页生成场景。")
                return
            except Exception as e:
                self.app.logger.error(f"Error reading scene plan file '{scene_plan_filepath}': {e}", exc_info=True)
                show_error("错误", f"无法读取场景规划文件：{e}")
                return

            # 2. Parse individual scenes using the shared LLM-tolerant parser.
            parsed_scenes = parse_scene_sections(scene_plan_content)
            self.app.logger.debug(f"Found {len(parsed_scenes)} scene headings.")

            if not parsed_scenes:
                self.app.logger.warning(f"Could not find any scene headings in '{scene_plan_filename}'. Treating entire content as one scene.")
                if scene_plan_content.strip():
                    parsed_scenes.append(scene_plan_content.strip())
            
            if not parsed_scenes:
                self.app.logger.error(f"Could not parse any scenes from '{scene_plan_filename}'. Check scene heading format (e.g., '### Scene 1: Title').")
                show_error("错误", "无法从规划中解析场景，请确保场景以“### 场景 X：...”或“## 场景 X - ...”开头。")
                return

            self.app.logger.info(f"Successfully parsed {len(parsed_scenes)} scenes from '{scene_plan_filename}'.")
            for idx, scene_text in enumerate(parsed_scenes):
                scene_preview = scene_text[:100].replace("\n", " ")
                self.app.logger.debug(
                    f"Parsed Scene {idx + 1} Content (first 100 chars): {scene_preview}..."
                )
            
            # --- Load Contextual Information (Lore, Characters, Factions) ---
            lore_content = "Lore context is missing or not loaded."
            try:
                lore_path = os.path.join(output_dir, "story", "lore", "generated_lore.md")
                if os.path.exists(lore_path):
                    lore_content = sanitize_lore_content(open_file(lore_path))
                    self.app.logger.info(f"Loaded lore context from {lore_path}")
            except Exception as e:
                self.app.logger.warning(f"Could not load lore for short story prose: {e}")

            character_roster_summary = "没有可用的人物名单。"
            try:
                characters_json_path = os.path.join(output_dir, "story", "lore", "characters.json")
                if not os.path.exists(characters_json_path):
                    characters_json_path = os.path.join(output_dir, "characters.json")
                if os.path.exists(characters_json_path):
                    characters_data = read_json(characters_json_path) # Assuming read_json is in helper_fns
                    if characters_data and "characters" in characters_data:
                        summaries = []
                        for char_info in characters_data["characters"]:
                            details = [f"\n\n姓名：{char_info.get('name', '无')}\n"]
                            details.append(f" - 角色：{char_info.get('role', '无')}\n")
                            details.append(f" - 性别：{char_info.get('gender', '无')}\n")
                            details.append(f" - 年龄：{char_info.get('age', '无')}\n")
                            details.append(f" - 外貌：{char_info.get('appearance_summary', '无')}\n")
                            goals = char_info.get('goals', [])
                            if goals: details.append(f" - 主要目标：{goals[0] if goals else '无'}\n")
                            strengths = char_info.get('strengths', [])
                            if strengths: details.append(f" - 主要优点：{strengths[0] if strengths else '无'}\n")
                            flaws = char_info.get('flaws', [])
                            if flaws: details.append(f" - 主要缺点：{flaws[0] if flaws else '无'}\n")
                            backstory = char_info.get('backstory_summary', '')
                            if backstory: details.append(f" - 背景摘要：{backstory}")
                            summaries.append("\n".join(details))
                        if summaries:
                            character_roster_summary = "主要人物：\n" + "\n".join(summaries)
                            self.app.logger.info(f"Loaded and summarized character roster from {characters_json_path}")
            except Exception as e:
                self.app.logger.warning(f"Could not load or process character roster from {characters_json_path}: {e}", exc_info=True)
            # --- Load Faction Summary ---
            faction_summary_info = "没有可用的势力信息。"
            try:
                factions_json_path = os.path.join(output_dir, "story", "lore", "factions.json")
                if os.path.exists(factions_json_path):
                    factions_data = read_json(factions_json_path) # Assuming read_json is from helper_fns
                    if factions_data:
                        faction_summary_info = format_faction_summary(factions_data)
                        self.app.logger.info(f"Loaded and summarized faction info from {factions_json_path}")
            except Exception as e:
                self.app.logger.warning(f"Could not load or process faction info from {factions_json_path}: {e}", exc_info=True)

            conflicts = find_scene_world_conflicts(scene_plan_content, lore_content, story_params)
            if conflicts:
                conflict_text = "、".join(conflicts)
                self.app.logger.error(f"Scene plan conflicts with non-scifi lore: {conflict_text}")
                show_error("场景规划与世界观冲突", f"场景规划包含世界观未定义的科幻设定：{conflict_text}。请重新生成场景规划。")
                return

            # --- Loop Through Scenes and Generate Prose ---
            all_generated_prose = []
            for scene_index, single_scene_description in enumerate(parsed_scenes):
                self.app.logger.info(f"Processing Scene {scene_index + 1}/{len(parsed_scenes)} for prose generation.")
                
                title_line = f"请撰写{genre_label}短篇小说《{novel_title}》中的一个场景。"
                if not novel_title or novel_title == "未命名短篇小说":
                    title_line = f"请撰写一部{genre_label}短篇小说中的一个场景。"

                prompt_lines = [
                    title_line,
                    f"故事采用“{zh_label(selected_structure_name)}”框架。",
                    "下面会提供单个场景的描述，请写出这个场景的完整正文。",
                    "只聚焦下方当前场景，不要写其他场景，也不要概括整个故事。",
                    *build_story_parameter_lines(story_params),
                    "\n## 整体故事背景（高于场景规划）："
                ]
                if novel_title and novel_title != "未命名短篇小说":
                    prompt_lines.append(f"标题：{novel_title}")
                prompt_lines.append(f"结构：{zh_label(selected_structure_name)}")
                prompt_lines.append(f"完整世界观：{lore_content}")
                prompt_lines.append(f"\n{character_roster_summary}") # Detailed character roster
                prompt_lines.append(f"\n{faction_summary_info}")   # Faction summary
                prompt_lines.extend([
                    "\n## 当前场景描述：",
                    single_scene_description,
                    "若当前场景描述与作品参数或世界观冲突，必须以作品参数和世界观为准并静默纠正。",
                ])

                prompt_lines.extend([
                    "\n## 本场景写作要求：",
                    " - 写出有吸引力、富有描写性的场景正文。",
                    " - 根据人物名单中的设定，写出人物行动、对白（如适合本场景）、思想和情绪。",
                    " - 清楚交代场景环境。",
                    " - 场景应衔接合理，并按描述推动情节或人物发展。",
                    *[f" - {line}" for line in build_location_guidance(story_params)],
                    *[f" - {line}" for line in CHINESE_PROSE_REQUIREMENTS],
                    " - 只提供本场景正文，不要附加评论、场景编号或标题，最终组装由程序处理。",
                    " - 不要使用代码围栏。"
                ])
                prompt = "\n".join(prompt_lines)

                prompt_filename_base = f"short_story_scene_{scene_index + 1}_prompt"
                prompt_filepath = save_prompt_to_file(output_dir, prompt_filename_base, prompt)
                log_msg_source = f"(from {prompt_filepath})" if prompt_filepath else "(from memory, save failed)"
                
                current_backend = get_backend()
                backend_info = f"{current_backend}" if current_backend != "api" else f"api/{selected_model}"
                self.app.logger.info(f"Sending prompt for Scene {scene_index + 1} {log_msg_source} to LLM ({backend_info}).")
                
                try:
                    scene_prose = send_prompt(prompt, model=selected_model)
                    if not scene_prose or not scene_prose.strip():
                        self.app.logger.warning(f"LLM returned empty or whitespace-only response for Scene {scene_index + 1}.")
                        scene_prose = f"[[[大模型未返回第 {scene_index + 1} 个场景的正文]]]"
                    else:
                        self.app.logger.info(f"Received prose for Scene {scene_index + 1}. Length: {len(scene_prose)} chars.")
                        style_warnings = analyze_chinese_prose_style(scene_prose)
                        if style_warnings:
                            self.app.logger.warning(f"Scene {scene_index + 1} Chinese style warnings: {'; '.join(style_warnings)}")
                    all_generated_prose.append(scene_prose)
                except Exception as e_llm:
                    self.app.logger.error(f"Error calling LLM for Scene {scene_index + 1}: {e_llm}", exc_info=True)
                    all_generated_prose.append(f"[[[ERROR GENERATING SCENE {scene_index + 1}: {e_llm}]]]")
                    # Optionally, decide if we should stop or continue with other scenes
                    # For now, it continues and marks the error.

            # --- Concatenate and Save Full Story ---
            if not all_generated_prose:
                self.app.logger.error("No prose was generated for any scene. Cannot save short story.")
                show_error("错误", "未能为短篇小说生成任何正文。")
                return

            full_story_content = "\n\n---\n\n".join(all_generated_prose) # Join scenes with a separator

            safe_title = novel_title.lower().replace(' ', '_').replace(':', '').replace('/', '')
            output_story_filename = f"prose_short_story_{safe_title}.md"
            os.makedirs(os.path.join(output_dir, "story", "content"), exist_ok=True)
            output_story_filepath = os.path.join(output_dir, "story", "content", output_story_filename)

            try:
                write_file(output_story_filepath, full_story_content)
                self.app.logger.info(f"Short story prose successfully written to: {output_story_filepath}")
                # show_success("Success", f"Short story prose generated and saved to {output_story_filename}")
            except Exception as e_write:
                self.app.logger.error(f"Error writing full short story to file '{output_story_filepath}': {e_write}", exc_info=True)
                show_error("错误", f"保存完整短篇小说失败：{e_write}")

        except Exception as e:
            self.app.logger.error(f"An unexpected error occurred in _write_short_story_prose: {e}", exc_info=True)
            show_error("错误", f"发生意外错误：{str(e)}")

    def normalize_markdown(self, scenes):
        # Match scene headings specifically (e.g., **Scene 1: ...**)
        normalized_scenes = re.sub(r"\*\*Scene (\d+): (.+?)\*\*", r"### Scene \1: \2", scenes)
        return normalized_scenes

    def write_chapter(self):
        selected_model = self.app.get_selected_model()
        output_dir = self.app.get_output_dir()
        os.makedirs(output_dir, exist_ok=True)
        self.app.logger.info(f"Initiating Write Chapter. Model: {selected_model}, Output Dir: {output_dir}")

        try:
            target_chapter_number_global = int(self.chapter_number_entry.get())
            if target_chapter_number_global <= 0:
                show_error("错误", "章节编号必须是正整数。")
                return
            self.app.logger.info(f"Target chapter (global): {target_chapter_number_global}")

            # 1. Access Core Parameters (Structure and Length)
            parameters_file_path = os.path.join(output_dir, "system", "parameters.txt")
            selected_structure_name = "6-Act Structure" # Default
            story_length = "Novel (Standard)" # Default
            params_from_file = {}
            try:
                if not os.path.exists(parameters_file_path):
                    self.app.logger.error(f"Parameters file not found: {parameters_file_path}. Cannot determine structure for chapter writing.")
                    show_error("错误", f"在 {output_dir} 中找不到参数文件 parameters.txt。")
                    return
                with open(parameters_file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if ":" in line:
                            key, value = line.split(":", 1)
                            params_from_file[key.strip()] = value.strip()
                loaded_structure = params_from_file.get("Story Structure")
                story_length = params_from_file.get("Story Length", story_length)
                if loaded_structure and loaded_structure.strip():
                    selected_structure_name = loaded_structure
                else:
                    self.app.logger.warning(f"'Story Structure' not found/empty in {parameters_file_path}. Using default: {selected_structure_name}")
                self.app.logger.info(f"Using structure: '{selected_structure_name}', Length: '{story_length}' for chapter writing.")
            except Exception as e_params:
                self.app.logger.error(f"Error reading parameters file ({parameters_file_path}): {e_params}. Using defaults.", exc_info=True)
                # Continue with defaults if param file reading fails, but log it.
            story_params = normalize_story_parameters(params_from_file)
            genre_label = format_genre_label(story_params)
            
            # 2. Determine the Correct Section for the Given Chapter Number
            sections_to_process = STRUCTURE_SECTIONS_MAP.get(selected_structure_name)
            if not sections_to_process:
                self.app.logger.error(f"Section definitions for '{selected_structure_name}' not found in STRUCTURE_SECTIONS_MAP.")
                show_error("错误", f"找不到故事结构“{zh_label(selected_structure_name)}”的定义。")
                return

            current_section_name_for_chapter = None
            chapter_count_accumulator = 0
            # chapter_number_within_section = 0 # Not strictly needed for filename, global number is used

            for section_name_iter in sections_to_process:
                safe_struct_name = selected_structure_name.lower().replace(' ', '_')
                safe_sect_name_iter = section_name_iter.lower().replace(' ', '_').replace(':','').replace('/','_')
                
                # This is the file that tells us how many chapters are in THIS section
                chapter_outline_input_base = f"chapter_outlines_{safe_struct_name}_{safe_sect_name_iter}.md"
                chapter_outline_input_filepath = os.path.join(
                    output_dir, "story", "planning", "chapter_outlines", chapter_outline_input_base
                )
                self.app.logger.debug(f"Checking chapter outline: {chapter_outline_input_filepath}")

                try:
                    outline_content = open_file(chapter_outline_input_filepath)
                    # Count chapters in this section's outline
                    chapters_in_this_section_outline = re.findall(
                        r"^(?:\*{2,}|#{2,})\s*(?:Chapter\s*|第\s*)(\d+)(?:\s*章)?(?:[:：\s\S]*?)?$",
                        outline_content,
                        re.MULTILINE | re.IGNORECASE,
                    )
                    num_chapters_in_section = len(chapters_in_this_section_outline)
                    self.app.logger.debug(f"Section '{section_name_iter}' has {num_chapters_in_section} chapters in its outline.")

                    if target_chapter_number_global <= chapter_count_accumulator + num_chapters_in_section:
                        current_section_name_for_chapter = section_name_iter
                        # chapter_number_within_section = target_chapter_number_global - chapter_count_accumulator
                        self.app.logger.info(f"Target chapter {target_chapter_number_global} found in section: '{current_section_name_for_chapter}'.")
                        break # Found the section
                    chapter_count_accumulator += num_chapters_in_section
                except FileNotFoundError:
                    self.app.logger.warning(f"Chapter outline file {chapter_outline_input_filepath} not found. Cannot determine chapter distribution for section '{section_name_iter}'.")
                    # This is problematic; we might not find the target chapter if outlines are missing.
                    # Consider how to handle this - for now, it will likely fail to find current_section_name_for_chapter.
                    continue # Try next section, but it's a data integrity issue.
                except Exception as e_outline:
                    self.app.logger.error(f"Error processing chapter outline {chapter_outline_input_filepath}: {e_outline}", exc_info=True)
                    continue
            
            if not current_section_name_for_chapter:
                self.app.logger.error(f"Could not determine which section target chapter {target_chapter_number_global} belongs to. Total chapters counted: {chapter_count_accumulator}.")
                show_error("错误", f"无法在已知结构阶段中定位第 {target_chapter_number_global} 章，请确保所有章节大纲均已生成。")
                return

            # 3. Construct the Correct Filename and Path for the detailed scene plan of the target chapter
            scene_plans_subdir_name = "detailed_scene_plans"
            safe_selected_structure_name_for_file = selected_structure_name.lower().replace(' ', '_')
            safe_current_section_name_for_file = current_section_name_for_chapter.lower().replace(' ', '_').replace(':','').replace('/','_')
            
            # The filename uses the global chapter number
            scene_plan_filename_base = f"scenes_{safe_selected_structure_name_for_file}_{safe_current_section_name_for_file}_ch{target_chapter_number_global}.md"
            scene_plan_filepath = os.path.join(output_dir, "story", "planning", scene_plans_subdir_name, scene_plan_filename_base)
            self.app.logger.info(f"Attempting to load scene plan for Chapter {target_chapter_number_global} from: {scene_plan_filepath}")

            # 4. Read the Scene File
            try:
                scenes_content_for_chapter = open_file(scene_plan_filepath)
                if not scenes_content_for_chapter.strip():
                    self.app.logger.error(f"Scene plan file '{scene_plan_filepath}' is empty.")
                    show_error("错误", f"第 {target_chapter_number_global} 章的场景规划文件为空。")
                    return
            except FileNotFoundError:
                self.app.logger.error(f"Scene plan file not found: {scene_plan_filepath}")
                show_error("错误", f"在“{scene_plans_subdir_name}”中找不到第 {target_chapter_number_global} 章的场景规划，请先规划场景。")
                return
            except Exception as e_read_scenes:
                self.app.logger.error(f"Error reading scene plan file '{scene_plan_filepath}': {e_read_scenes}", exc_info=True)
                show_error("错误", f"无法读取第 {target_chapter_number_global} 章的场景规划：{e_read_scenes}")
                return

            # --- Original logic from here, using scenes_content_for_chapter --- 
            # params = open_file("parameters.txt") # Already loaded as params_from_file
            # lore_content = open_file(os.path.join(output_dir, "story", "lore", "generated_lore.md"))
            # characters_content = open_file("characters.md") # Not used, character roster comes from JSON below
            # en_characters_content = open_file("characters.md") # Duplicate
            # relationships_content = open_file("relationships.md") # Not used
            # factions_content = open_file("factions.md") # Not used, faction summary comes from JSON below

            # The variable `scenes` was used for `scenes_content_for_chapter`
            # `scenes_content_for_chapter` holds the content of the specific chapter's scene plan file.
            
            # Detect scenes within this chapter's plan file
            # Normalize markdown formatting (if necessary, current scene plans should be ## Scene...)
            # scenes_content_for_chapter = self.normalize_markdown(scenes_content_for_chapter) # Current parser uses ## Scene, so normalize might not be needed if input is consistent

            scene_details_list = parse_scene_sections(scenes_content_for_chapter)
            self.app.logger.debug(f"Found {len(scene_details_list)} scene headings in Chapter {target_chapter_number_global}'s plan file ('{scene_plan_filename_base}').")

            if not scene_details_list:
                self.app.logger.error(f"No scene headings found within {scene_plan_filepath}. Cannot process chapter.")
                show_error("错误", f"第 {target_chapter_number_global} 章的规划中没有找到独立场景。")
                return
            
            self.app.logger.info(f"Successfully parsed {len(scene_details_list)} scenes for Chapter {target_chapter_number_global} from its plan file.")

            # --- Load Character Roster Summary ---
            character_roster_summary = "没有可用的人物名单。"
            try:
                characters_json_path = os.path.join(output_dir, "story", "lore", "characters.json")
                if os.path.exists(characters_json_path):
                    characters_data = read_json(characters_json_path)
                    if characters_data and "characters" in characters_data:
                        # (Identical summarization logic as in _write_short_story_prose)
                        summaries = []
                        for char_info in characters_data["characters"]:
                            details = [f"\n\n姓名：{char_info.get('name', '无')}\n"]
                            details.append(f" - 角色：{char_info.get('role', '无')}\n")
                            details.append(f" - 性别：{char_info.get('gender', '无')}\n")
                            details.append(f" - 年龄：{char_info.get('age', '无')}\n")
                            details.append(f" - 外貌：{char_info.get('appearance_summary', '无')}\n")
                            goals = char_info.get('goals', [])
                            if goals: details.append(f" - 主要目标：{goals[0] if goals else '无'}\n")
                            strengths = char_info.get('strengths', [])
                            if strengths: details.append(f" - 主要优点：{strengths[0] if strengths else '无'}\n")
                            flaws = char_info.get('flaws', [])
                            if flaws: details.append(f" - 主要缺点：{flaws[0] if flaws else '无'}\n")
                            backstory = char_info.get('backstory_summary', '')
                            if backstory: details.append(f" - 背景摘要：{backstory}")
                            summaries.append("\n".join(details))
                        if summaries:
                            character_roster_summary = "主要人物：\n" + "\n".join(summaries)
                            self.app.logger.info(f"Loaded character roster for Chapter {target_chapter_number_global}.")
            except Exception as e_char_load:
                self.app.logger.warning(f"Could not load/process character roster for Chapter {target_chapter_number_global}: {e_char_load}", exc_info=True)

            faction_summary_info = "没有可用的势力信息。"
            try:
                factions_json_path = os.path.join(output_dir, "story", "lore", "factions.json")
                if os.path.exists(factions_json_path):
                    factions_data = read_json(factions_json_path)
                    if factions_data:
                        faction_summary_info = format_faction_summary(factions_data)
                        self.app.logger.info(f"Loaded faction summary for Chapter {target_chapter_number_global}.")
            except Exception as e_faction_load:
                self.app.logger.warning(f"Could not load/process faction info for Chapter {target_chapter_number_global}: {e_faction_load}", exc_info=True)

            # Load lore content
            lore_content = "Lore context is missing or not loaded."
            try:
                lore_path = os.path.join(output_dir, "story", "lore", "generated_lore.md")
                if os.path.exists(lore_path):
                    lore_content = sanitize_lore_content(open_file(lore_path))
                    self.app.logger.info(f"Loaded lore content for Chapter {target_chapter_number_global}.")
            except Exception as e_lore_load:
                self.app.logger.warning(f"Could not load lore content for Chapter {target_chapter_number_global}: {e_lore_load}", exc_info=True)
            # --- End Context Loading --- 

            conflicts = find_scene_world_conflicts(scenes_content_for_chapter, lore_content, story_params)
            if conflicts:
                conflict_text = "、".join(conflicts)
                self.app.logger.error(f"Scene plan conflicts with non-scifi lore: {conflict_text}")
                show_error("场景规划与世界观冲突", f"第 {target_chapter_number_global} 章场景规划包含世界观未定义的科幻设定：{conflict_text}。请重新生成该章场景规划。")
                return

            generated_scenes_for_chapter = [] # Changed from `scenes` to avoid conflict with original `scenes_content_for_chapter`
            legal_loop_enabled = is_legal_suspense(story_params)

            def generate_scene_prose(
                scene_plan,
                scene_number,
                previous_scene_tail="",
                next_scene_plan="",
                contract=None,
            ):
                """Generate one scene with explicit continuity boundaries."""
                self.app.logger.info(
                    f"Processing Scene {scene_number} for Chapter {target_chapter_number_global}."
                )
                continuity_lines = []
                if contract:
                    continuity_lines.extend([
                        "\n## 本章质量契约（必须兑现，不得擅自增加真相）：",
                        compact_json(contract, max_chars=10000),
                    ])
                if previous_scene_tail:
                    continuity_lines.extend([
                        "\n## 上一场或上一章的已验收结尾（从这一状态续写，不得重演已完成动作）：",
                        previous_scene_tail,
                    ])
                if next_scene_plan:
                    continuity_lines.extend([
                        "\n## 下一场边界（仅用于控制本场收束；禁止提前写出下一场事件）：",
                        next_scene_plan,
                    ])

                prompt_lines = [
                    f"请撰写{genre_label}{zh_label(story_length)}第 {target_chapter_number_global} 章中的一个场景。",
                    f"故事采用“{zh_label(selected_structure_name)}”框架，当前位于“{zh_label(current_section_name_for_chapter)}”部分。",
                    f"下面会提供第 {target_chapter_number_global} 章内单个场景的详细规划，请只写这个场景的完整正文。",
                    "不要写其他场景，也不要概括本章。",
                    *build_story_parameter_lines(story_params),
                    "\n## 整体故事背景（高于场景规划）：",
                    f"完整世界观：{lore_content}",
                    f"\n{character_roster_summary}",
                    f"\n{faction_summary_info}",
                    f"\n## 当前场景描述（第 {target_chapter_number_global} 章，场景 {scene_number}）：",
                    scene_plan,
                    *continuity_lines,
                    "若当前场景描述与作品参数或世界观冲突，必须以作品参数和世界观为准并静默纠正。",
                    "\n## 本场景写作要求：",
                    " - 写出有吸引力、富有描写性的场景正文。",
                    " - 根据人物名单中的设定，写出人物行动、对白（如适合本场景）、思想和情绪。",
                    " - 清楚交代场景环境。",
                    " - 场景应衔接合理，并按描述推动情节或人物发展。",
                    " - 只推进一个明确的问题或张力，用可核实的动作、证物、证词和程序细节表现，不用抽象总结代替情节。",
                    " - 严格控制信息差：人物只能依据其已知信息行动，线索出现后才允许据此推断。",
                    " - 如涉及反转，必须由本章契约中已安排的公平伏笔触发，并改变人物的判断或行动。",
                    " - 法律程序须符合本章契约与案件底稿，不得让角色凭身份跳过取证、移交、质证等关键约束。",
                    " - 本场结束时人物处境必须发生具体变化；不要重复上一场已经完成的动作、介绍和环境描写。",
                    *[f" - {line}" for line in build_location_guidance(story_params)],
                    *[f" - {line}" for line in CHINESE_PROSE_REQUIREMENTS],
                    " - 只提供本场景正文，不要附加评论、场景编号或标题，最终章节组装由程序处理。",
                    " - 不要使用代码围栏。"
                ]
                prompt = "\n".join(prompt_lines)

                prompt_filename_base = f"write_chapter_{target_chapter_number_global}_scene_{scene_number}_prompt"
                prompt_filepath = save_prompt_to_file(output_dir, prompt_filename_base, prompt)
                log_msg_source = f"(from {prompt_filepath})" if prompt_filepath else "(from memory, save failed)"

                current_backend = get_backend()
                backend_info = f"{current_backend}" if current_backend != "api" else f"api/{selected_model}"
                self.app.logger.info(
                    f"Sending prompt for Chapter {target_chapter_number_global}, Scene {scene_number} "
                    f"{log_msg_source} to LLM ({backend_info})."
                )
                scene_prose_text = send_prompt(prompt, model=selected_model)
                if not scene_prose_text or not scene_prose_text.strip():
                    raise RuntimeError(
                        f"大模型未返回第 {target_chapter_number_global} 章第 {scene_number} 个场景的正文"
                    )

                self.app.logger.info(
                    f"Received prose for Chapter {target_chapter_number_global}, Scene {scene_number}. "
                    f"Length: {len(scene_prose_text)} chars."
                )
                style_warnings = analyze_chinese_prose_style(scene_prose_text)
                if style_warnings:
                    self.app.logger.warning(
                        f"Chapter {target_chapter_number_global} Scene {scene_number} "
                        f"Chinese style warnings: {'; '.join(style_warnings)}"
                    )
                return scene_prose_text

            if legal_loop_enabled:
                self.app.logger.info(
                    f"Chapter {target_chapter_number_global} is legal suspense; "
                    "enabling design-generation-review loop."
                )
                quality_loop = ChapterGenerationLoop(
                    output_dir=output_dir,
                    model=selected_model,
                    logger=self.app.logger,
                )

                def save_revised_plan(revised_plan):
                    from datetime import datetime

                    archive_dir = os.path.join(output_dir, "archive", "quality_loop", "scene_plans")
                    os.makedirs(archive_dir, exist_ok=True)
                    base_name = os.path.splitext(scene_plan_filename_base)[0]
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                    archive_path = os.path.join(
                        archive_dir,
                        f"{base_name}_before_{timestamp}.md",
                    )
                    write_file(archive_path, scenes_content_for_chapter)
                    write_file(scene_plan_filepath, revised_plan)
                    self.app.logger.info(
                        f"Quality loop revised the scene plan; original archived at {archive_path}."
                    )

                loop_result = quality_loop.run(
                    chapter_number=target_chapter_number_global,
                    plan_content=scenes_content_for_chapter,
                    parameters=story_params,
                    lore=lore_content,
                    generate_scene=generate_scene_prose,
                    on_plan_revised=save_revised_plan,
                )
                generated_scenes_for_chapter = loop_result.scenes
            else:
                # Preserve the original best-effort behavior for other genres.
                for scene_idx_in_chapter, single_scene_detail_from_plan in enumerate(scene_details_list, start=1):
                    try:
                        generated_scenes_for_chapter.append(
                            generate_scene_prose(
                                scene_plan=single_scene_detail_from_plan,
                                scene_number=scene_idx_in_chapter,
                            )
                        )
                    except Exception as e_llm_scene:
                        self.app.logger.error(
                            f"Error calling LLM for Chapter {target_chapter_number_global}, "
                            f"Scene {scene_idx_in_chapter}: {e_llm_scene}",
                            exc_info=True,
                        )
                        generated_scenes_for_chapter.append(
                            f"[[[ERROR GENERATING CHAPTER {target_chapter_number_global}, "
                            f"SCENE {scene_idx_in_chapter}: {e_llm_scene}]]]"
                        )
                
            # Combine all scenes for this chapter into one string
            chapter_content_full = "\n\n---\n\n".join(generated_scenes_for_chapter)

            # --- Create subdirectory for chapter prose output ---
            chapters_subdir_name = "chapters"
            full_chapters_subdir_path = os.path.join(output_dir, "story", "content", chapters_subdir_name)
            os.makedirs(full_chapters_subdir_path, exist_ok=True)
            # --- End subdirectory creation ---

            # Save the combined chapter to a file within the subdirectory
            chapter_filename_output = f"chapter_{target_chapter_number_global}.md"
            chapter_filepath_output = os.path.join(full_chapters_subdir_path, chapter_filename_output)
            
            write_file(chapter_filepath_output, chapter_content_full)
            if legal_loop_enabled:
                quality_loop.accept_result(target_chapter_number_global, loop_result)
            self.app.logger.info(f"Chapter {target_chapter_number_global} successfully written to: {chapter_filepath_output}")
            # show_success("Success", f"Chapter {target_chapter_number_global} generated and saved to {chapter_filename_output}")

        except ValueError:
            self.app.logger.error("Invalid chapter number entered.", exc_info=True) # Log before showing messagebox
            show_error("错误", "请输入有效的章节编号。")
        except Exception as e_main:
            self.app.logger.error(f"Failed to write chapter {target_chapter_number_global if 'target_chapter_number_global' in locals() else 'UNKNOWN'}: {e_main}", exc_info=True)
            show_error("错误", f"撰写章节失败：{str(e_main)}")

    def rewrite_chapter(self):
        # Ensure the `try` block is correctly paired with an `except` or `finally`
        try:
            chapter_number = int(self.chapter_number_entry.get())

            # Simplified parameter loading for rewrite - assumes files are in output_dir
            output_dir = self.app.get_output_dir() # Get output_dir
            parameters = open_file(os.path.join(output_dir, "system", "parameters.txt"))
            # lore_content = open_file(os.path.join(output_dir, "generated_lore.md")) # Consider if needed for rewrite
            # characters_content = open_file(os.path.join(output_dir, "characters.md")) # Likely not needed directly if chapter text is main input
            # factions_content = open_file(os.path.join(output_dir, "factions.md")) # Likely not needed
            # structure = open_file(os.path.join(output_dir, "story_structure.md"))  # Consider if high-level structure needed

            # --- Define chapters subdirectory --- 
            chapters_subdir_name = "chapters"
            full_chapters_subdir_path = os.path.join(output_dir, "story", "content", chapters_subdir_name)
            # Ensure it exists for both reading and writing, though reading assumes it was created by write_chapter
            os.makedirs(full_chapters_subdir_path, exist_ok=True) 
            # --- End subdirectory definition ---

            chapter_filename_input = f"chapter_{chapter_number}.md"
            # Read from the chapters subdirectory
            chapter_filepath_input = os.path.join(full_chapters_subdir_path, chapter_filename_input)
            
            try:
                with open(chapter_filepath_input, "r", encoding='utf-8') as chapter_file:
                    chapter_file_in = chapter_file.read()
            except FileNotFoundError:
                self.app.logger.error(f"Chapter file to rewrite not found: {chapter_filepath_input}")
                show_error("错误", f"找不到待重写的章节文件“{chapter_filename_input}”。")
                return
            except Exception as e_read_rewrite:
                self.app.logger.error(f"Error reading chapter file {chapter_filepath_input} for rewrite: {e_read_rewrite}", exc_info=True)
                show_error("错误", f"无法读取待重写章节：{e_read_rewrite}")
                return

            self.app.logger.info(f"Trying to re-write chapter {chapter_number}....") # Changed from print
            
            # TODO: The prompt for rewrite_chapter needs more context (lore, characters, factions)
            # similar to _write_short_story_prose and the refactored write_chapter to ensure consistency.
            # For now, it uses a simpler prompt structure.
            prompt = (
                f"请阅读并重写小说第 {chapter_number} 章的这一场景。\n"
                "当前场景太短，只是一份粗略草稿，请大幅扩写。"
                "同时检查叙事是否有吸引力、描写是否生动，并按原有规划发展人物和情节。\n"
                "请以叙事写作教授的视角思考：还缺少什么？"
                "以下是故事参数，供提醒：\n\n"
                f"{parameters}\n\n"
                "阅读正文时，请检查是否缺少以下内容：\n"
                "* 符合类型特点的地点描写\n"
                "* 人物对白\n"
                "* 人物描写\n"
                "* 人物思想和情绪\n"
                "* 人物内省（主要人物在思考什么）\n"
                "* 推动故事的人物行动\n"
                "* 对白之外的人物互动\n\n"
                "以下是本章正文：\n\n"
                f"{chapter_file_in}"
            )

            selected_model = self.app.get_selected_model() # Use selected model
            response = send_prompt(prompt, model=selected_model) # Pass selected_model

            output_rewrite_filename = f"re_chapter_{chapter_number}.md"
            # Write to the chapters subdirectory
            output_rewrite_filepath = os.path.join(full_chapters_subdir_path, output_rewrite_filename)
            
            write_file(output_rewrite_filepath, response)
            self.app.logger.info(f"Chapter {chapter_number} re-written and saved successfully to: {output_rewrite_filepath}") # Changed from print
            show_success("成功", f"第 {chapter_number} 章已重写并保存到 {output_rewrite_filename}")

        except ValueError:
            self.app.logger.error("Invalid chapter number entered for rewrite.", exc_info=True)
            show_error("错误", "请输入有效的待重写章节编号。")
        except Exception as e_rewrite_main:
            self.app.logger.error(f"Failed to RE-write chapter {chapter_number if 'chapter_number' in locals() else 'UNKNOWN'}: {e_rewrite_main}", exc_info=True)
            show_error("错误", f"重写章节失败：{str(e_rewrite_main)}")

    # ===== AUTOMATED CHAPTER WRITING METHODS =====
    
    def update_progress_display(self):
        """Update the progress display with current chapter status."""
        try:
            # Import here to avoid circular imports
            from agents.writing.chapter_writing_agent import get_chapter_progress
            
            output_dir = self.app.get_output_dir() if self.app else "current_work"
            progress = get_chapter_progress(output_dir, self.app)
            
            completed = progress.get('completed_chapters', 0)
            total = progress.get('total_chapters', 0)
            percentage = progress.get('completion_percentage', 0)
            next_chapter = progress.get('next_chapter')
            next_chapter_ready = progress.get('next_chapter_ready', False)
            missing_scene_plans = progress.get('missing_scene_plans', [])
            
            if total > 0:
                status_text = f"进度：已完成 {completed}/{total} 章（{percentage:.1f}%）"
                if next_chapter:
                    status_text += f" - 下一章：第 {next_chapter} 章"
                    if not next_chapter_ready:
                        status_text += "（缺少场景规划）"
                else:
                    status_text += " - 已全部完成！✅"
                if missing_scene_plans:
                    missing_text = "、".join(map(str, missing_scene_plans[:8]))
                    status_text += f"；缺少规划：第 {missing_text} 章"
                    if len(missing_scene_plans) > 8:
                        status_text += "等"
            else:
                status_text = "进度：未找到章节（请先生成故事结构）"
                
            self.progress_label.config(text=status_text)
            
            # Update button states
            has_chapters = total > 0
            has_remaining = next_chapter is not None and next_chapter_ready
            
            self.write_next_button.config(state="normal" if has_remaining else "disabled")
            self.write_all_button.config(state="normal" if has_remaining else "disabled")
            
        except Exception as e:
            self.progress_label.config(text=f"进度：发生错误 - {str(e)}")
            if self.app and hasattr(self.app, 'logger'):
                self.app.logger.error(f"Error updating progress display: {e}")
    
    def analyze_chapters(self):
        """Analyze the chapter structure and update progress display."""
        try:
            self.analyze_button.config(state="disabled", text="📊 正在分析…")
            self.update_progress_display()
            
            # Import here to avoid circular imports
            from agents.writing.chapter_writing_agent import analyze_story_chapters
            
            output_dir = self.app.get_output_dir() if self.app else "current_work"
            chapter_info_list, plan = analyze_story_chapters(output_dir, self.app)
            
            total = len(chapter_info_list)
            completed = len(plan.chapters_completed)
            remaining = len(plan.chapters_to_write)
            missing_scene_plans = [
                chapter.chapter_number
                for chapter in chapter_info_list
                if not chapter.exists and not chapter.plan_exists
            ]
            
            message = f"分析完成！\n\n章节总数：{total}\n已完成：{completed}\n剩余：{remaining}"
            
            if remaining > 0:
                next_chapters = plan.chapters_to_write[:5]  # Show first 5
                message += f"\n\n接下来撰写：{', '.join(map(str, next_chapters))}"
                if len(plan.chapters_to_write) > 5:
                    message += f"（另有 {len(plan.chapters_to_write) - 5} 章）"
            if missing_scene_plans:
                message += (
                    "\n\n尚未生成详细场景规划：第 "
                    + "、".join(map(str, missing_scene_plans))
                    + " 章。这些章节需先在“场景规划”页补齐。"
                )
            
            show_success("章节分析", message)
            
        except Exception as e:
            error_msg = f"分析章节时出错：{str(e)}"
            show_error("分析错误", error_msg)
            if self.app and hasattr(self.app, 'logger'):
                self.app.logger.error(f"Chapter analysis error: {e}")
        finally:
            self.analyze_button.config(state="normal", text="📊 分析章节")
            self.update_progress_display()
    
    def write_next_chapter(self):
        """Write the next chapter automatically."""
        try:
            self.write_next_button.config(state="disabled", text="✍️ 正在撰写…")
            
            # Import here to avoid circular imports
            from agents.writing.chapter_writing_agent import write_next_chapters
            
            output_dir = self.app.get_output_dir() if self.app else "current_work"
            result = write_next_chapters(output_dir, batch_size=1, app_instance=self.app)
            
            if result.success:
                chapters_written = result.data.get("chapters_written", [])
                if chapters_written:
                    chapter_num = chapters_written[0]
                    # show_success("Chapter Written", f"Successfully wrote Chapter {chapter_num}!")
                else:
                    show_success("已完成", "所有章节都已经写完！")
                    
            else:
                error_message = "; ".join(result.messages) if result.messages else "未知错误"
                show_error("写作错误", f"撰写章节失败：{error_message}")
                
        except Exception as e:
            error_msg = f"撰写下一章时出错：{str(e)}"
            show_error("写作错误", error_msg)
            if self.app and hasattr(self.app, 'logger'):
                self.app.logger.error(f"Next chapter writing error: {e}")
        finally:
            self.write_next_button.config(state="normal", text="✍️ 撰写下一章")
            self.update_progress_display()
    
    def write_all_chapters(self):
        """Write all remaining chapters automatically."""
        try:
            self.write_all_button.config(state="disabled", text="🚀 正在撰写全部章节…")
            
            # Import here to avoid circular imports
            from agents.writing.chapter_writing_agent import write_next_chapters
            
            output_dir = self.app.get_output_dir() if self.app else "current_work"
            
            # Write in larger batches for efficiency
            result = write_next_chapters(output_dir, batch_size=5, app_instance=self.app)
            
            if result.success:
                chapters_written = result.data.get("chapters_written", [])
                errors = result.data.get("errors", [])
                total_completed = result.data.get("total_completed", 0)
                
                message = f"Batch Writing Complete!\n\n"
                message += f"Chapters written: {len(chapters_written)}\n"
                message += f"Total completed: {total_completed}\n"
                
                if chapters_written:
                    message += f"\nNew chapters: {', '.join(map(str, chapters_written))}"
                
                if errors:
                    message += f"\n\nErrors ({len(errors)}): {'; '.join(errors[:3])}"
                    if len(errors) > 3:
                        message += f" (and {len(errors) - 3} more)"
                
                # show_success("Batch Writing Complete", message)
            else:
                error_message = "; ".join(result.messages) if result.messages else "未知错误"
                show_error("写作错误", f"撰写章节失败：{error_message}")
                
        except Exception as e:
            error_msg = f"撰写全部章节时出错：{str(e)}"
            show_error("写作错误", error_msg)
            if self.app and hasattr(self.app, 'logger'):
                self.app.logger.error(f"All chapters writing error: {e}")
        finally:
            self.write_all_button.config(state="normal", text="🚀 撰写全部章节")
            self.update_progress_display()
