from tkinter import ttk, messagebox
from core.gui.notifications import show_success, show_error, show_warning
from core.generation.ai_helper import send_prompt, get_backend
import re
from core.generation.helper_fns import open_file, write_file, save_prompt_to_file
import os
from core.gui.parameters import STRUCTURE_SECTIONS_MAP
from core.localization import zh_label
from core.config.directory_config import get_directory_manager


class ScenePlanning:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app  # Store the app instance

        # Frame setup for chapter writing UI
        self.scene_chapter_planning_frame = ttk.Frame(parent)
        self.scene_chapter_planning_frame.pack(expand=True, fill="both")

        # Title Label
        self.title_label = ttk.Label(self.scene_chapter_planning_frame, text="场景与章节规划", font=("Helvetica", 16))
        self.title_label.pack(pady=10)

        # Generate Chapter Outline Button
        self.chapter_outline_button = ttk.Button(self.scene_chapter_planning_frame, text="生成章节大纲", command=self.generate_chapter_outline)
        self.chapter_outline_button.pack(pady=20)

        # Generate scene plan
        self.plan_scenes_button = ttk.Button(
            self.scene_chapter_planning_frame, 
            text="规划场景",
            command=self._dispatch_scene_planning
        )
        self.plan_scenes_button.pack(pady=20)

        # Register callback to update UI elements based on parameters
        if self.app and hasattr(self.app, 'param_ui') and hasattr(self.app.param_ui, 'add_callback'):
            self.app.param_ui.add_callback(self._update_ui_based_on_parameters)
        self._update_ui_based_on_parameters() # Call once for initial setup

    def _get_directory_manager(self, output_dir=None):
        """Return the directory manager for the current structured workspace."""
        if output_dir is None:
            output_dir = self.app.get_output_dir()
        return get_directory_manager(output_dir, use_new_structure=True)


    def _update_ui_based_on_parameters(self):
        """Updates UI elements like button visibility based on current story parameters."""
        if not (self.app and hasattr(self.app, 'param_ui') and hasattr(self.app.param_ui, 'get_current_parameters') and callable(self.app.param_ui.get_current_parameters)):
            if self.app and hasattr(self.app, 'logger'):
                self.app.logger.info("ScenePlanning: ParametersUI not fully available for UI update.")
            return

        try:
            params = self.app.param_ui.get_current_parameters()
            story_length = params.get("story_length")
            if self.app and hasattr(self.app, 'logger'):
                self.app.logger.info(f"ScenePlanning._update_ui: story_length = '{story_length}'")
        except Exception as e:
            if self.app and hasattr(self.app, 'logger'):
                self.app.logger.error(f"ScenePlanning: Error getting parameters: {e}", exc_info=True)
            return

        # Update Chapter Outline Button visibility
        if hasattr(self, 'chapter_outline_button'):
            is_short_story = (story_length == "Short Story")
            
            # Get current mapped state for logging, but don't solely rely on it for action
            current_mapped_state = self.chapter_outline_button.winfo_ismapped()
            self.app.logger.info(f"ScenePlanning: Updating. is_short_story: {is_short_story}, button_is_currently_visible (winfo_ismapped before action): {current_mapped_state}")

            if is_short_story:
                self.app.logger.debug(f"ScenePlanning: Short Story selected. Attempting pack_forget(). Current ismapped: {current_mapped_state}")
                self.chapter_outline_button.pack_forget()
                self.app.logger.debug(f"ScenePlanning: After pack_forget(). New winfo_ismapped: {self.chapter_outline_button.winfo_ismapped()}")
            else: # Not a short story (Novella, Novel, etc.)
                self.app.logger.debug(f"ScenePlanning: Not Short Story. Attempting pack(). Current ismapped: {current_mapped_state}")
                # Ensure plan_scenes_button exists before trying to pack before it
                if hasattr(self, 'plan_scenes_button') and self.plan_scenes_button.winfo_exists():
                    self.chapter_outline_button.pack(pady=20, before=self.plan_scenes_button)
                else:
                    # Fallback: If plan_scenes_button isn't there, pack normally.
                    self.chapter_outline_button.pack(pady=20) 
                self.app.logger.debug(f"ScenePlanning: After pack(). New winfo_ismapped: {self.chapter_outline_button.winfo_ismapped()}")
        else:
            if self.app and hasattr(self.app, 'logger'):
                self.app.logger.error("ScenePlanning: chapter_outline_button attribute MISSING when trying to update UI.")
        
        # Future: Update Plan Scenes button text if desired
        # if hasattr(self, 'plan_scenes_button'):
        #     if story_length == "Short Story":
        #         self.plan_scenes_button.config(text="Plan Short Story Scenes")
        #     else:
        #         self.plan_scenes_button.config(text="Plan Chapter Scenes")


    def _dispatch_scene_planning(self):
        """Dispatches to the correct scene planning method based on story length."""
        if not (self.app and hasattr(self.app, 'param_ui')):
            self.app.logger.error("ScenePlanning: Parameters.py not available for dispatching scene planning.")
            show_error("错误", "无法确定场景规划所需的故事参数。")
            return

        params = self.app.param_ui.get_current_parameters()
        story_length = params.get("story_length")
        self.app.logger.info(f"ScenePlanning: Dispatching scene planning for story length: {story_length}")

        if story_length == "Short Story":
            self._plan_short_story_scenes()
        elif story_length in ["Novella", "Novel (Standard)", "Novel (Epic)"]:
            self._plan_long_form_scenes() # Changed from self.scene_plan
        else:
            self.app.logger.error(f"ScenePlanning: Unknown story length '{story_length}'.")
            show_error("错误", f"场景规划不支持故事篇幅“{zh_label(story_length)}”。")


    # Generate an outline of the chapters given the 6-act story structure
    def generate_chapter_outline(self):
        selected_model = self.app.get_selected_model() # Use app-wide selected model
        output_dir = self.app.get_output_dir() # Get user-defined output directory
        os.makedirs(output_dir, exist_ok=True)
        dir_manager = self._get_directory_manager(output_dir)
        chapter_outlines_dir = dir_manager.get_chapter_outlines_path()
        print(f"Generating chapter outlines with model: {selected_model}, output dir: {output_dir}")

        # --- Read Parameters to get selected structure --- 
        parameters_file_path = dir_manager.get_parameters_path()
        selected_structure_name = "6-Act Structure" # Default
        try:
            params = {}
            if not os.path.exists(parameters_file_path):
                print(f"Warning: Parameters file not found at {parameters_file_path}. Using default structure: {selected_structure_name}")
            else:
                with open(parameters_file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if ":" in line:
                            key, value = line.split(":", 1)
                            params[key.strip()] = value.strip()
                loaded_structure = params.get("Story Structure")
                if loaded_structure and loaded_structure.strip():
                    selected_structure_name = loaded_structure
                else:
                    print(f"Warning: 'Story Structure' not found or empty in {parameters_file_path}. Using default: {selected_structure_name}")
            print(f"Using selected story structure for chapter outlines: {selected_structure_name}")
        except Exception as e:
            print(f"Error reading parameters file ({parameters_file_path}): {e}. Using default structure: {selected_structure_name}")
        # --- End Reading Parameters ---

        # --- STRUCTURE_SECTIONS_MAP is now imported from parameters.py ---
        # The local definition has been removed. 
        # The TODO comment about moving it is also resolved by this change.
        sections_to_process = STRUCTURE_SECTIONS_MAP.get(selected_structure_name)
        if not sections_to_process:
            show_error("错误", f"找不到故事结构“{zh_label(selected_structure_name)}”的阶段定义。")
            print(f"Error: No sections defined for structure '{selected_structure_name}'.")
            return

        try:
            chapter_number_offset = 1 # To keep track of chapter numbers across sections

            for current_section_name in sections_to_process:
                safe_selected_structure_name = selected_structure_name.lower().replace(' ', '_')
                safe_section_name = current_section_name.lower().replace(' ', '_').replace(':','').replace('/','_')
                
                input_filename_base = f"{safe_selected_structure_name}_{safe_section_name}.md"
                input_filepath = os.path.join(output_dir, "story", "structure", input_filename_base)

                print(f"Processing section: {current_section_name} from file: {input_filepath}")
                
                try:
                    detailed_section_content = open_file(input_filepath)
                except FileNotFoundError:
                    show_warning("文件缺失", f"找不到“{zh_label(current_section_name)}”的详细规划（文件：{input_filename_base}），将跳过该部分。")
                    print(f"Warning: File {input_filepath} not found. Skipping section '{current_section_name}'.")
                    continue # Skip to the next section

                prompt = (
                    f"请为采用“{zh_label(selected_structure_name)}”框架的小说生成章节大纲。"
                    f"该结构包含：{', '.join(zh_label(section) for section in sections_to_process)}。\n"
                    f"当前重点是 **{zh_label(current_section_name)}**。以下是这一部分的详细规划：\n\n{detailed_section_content}\n\n"
                    f"请根据“{zh_label(current_section_name)}”的详细规划，逐章生成大纲。"
                    "每一章都应有明确目的，并推动这一结构部分的故事。"
                    "为每章建议所含场景，并列出本章涉及的人物、势力和地点（包括行星）。"
                    f"“{zh_label(current_section_name)}”从第 {chapter_number_offset} 章开始，请依次分配章号。\n"
                    "请以 Markdown 格式输出，不要使用代码围栏，也不要在响应中写出“Markdown”一词。"
                )

                current_backend = get_backend()
                backend_info = f"{current_backend}" if current_backend != "api" else f"api/{selected_model}"
                print(f"--- Chapter Outline Prompt for {current_section_name} (Starts Chapter {chapter_number_offset}, Backend: {backend_info}) ---")
                # print(prompt) # Uncomment for debugging full prompt
                print("----------------------------------------------------------------")
                response = send_prompt(prompt, model=selected_model)

                # Dynamically count chapters in the LLM's response for this section
                # Adjusted regex to be more flexible with markdown chapter headings (##, ###, **** etc.)
                chapters_in_response = re.findall(
                    r"^\s*(?:#{2,6}\s*|\*{2,}\s*)?(?:Chapter\s*\d+|第\s*\d+\s*章)(?:\s*[:：.\-]?\s*.*?)?\s*(?:\*{2,})?\s*$",
                    response,
                    re.MULTILINE | re.IGNORECASE,
                )
                chapter_count_for_section = len(chapters_in_response)
                
                print(f"LLM generated {chapter_count_for_section} chapters for section '{current_section_name}'. Next section will start after chapter {chapter_number_offset + chapter_count_for_section -1}")
                chapter_number_offset += chapter_count_for_section # Update for the next section

                # Save the chapter outline to a markdown file
                output_filename_base = f"chapter_outlines_{safe_selected_structure_name}_{safe_section_name}.md"
                os.makedirs(chapter_outlines_dir, exist_ok=True)
                output_filepath = os.path.join(chapter_outlines_dir, output_filename_base)
                write_file(output_filepath, response)
                print(f"Chapter outline for {current_section_name} saved to {output_filepath}")

            # show_success("Success", f"Chapter outlines for '{selected_structure_name}' generated successfully.")

        except FileNotFoundError as fnf_e: # Should be caught per-file above, but as a fallback
            show_error("错误", f"找不到必需文件：{fnf_e}")
            print(f"Error: File not found - {fnf_e}")
        except Exception as e:
            print(f"Failed to generate chapter outline: {e}")
            import traceback
            traceback.print_exc()
            show_error("错误", f"生成章节大纲失败：{str(e)}")


    # Generate an outline of the scenes within each chapter
    # TODO: need to make sure character names are correct
    # TODO: need to use reconciled_planets_arcs.md for locations and story structure
    def scene_plan(self):
        selected_model = self.app.get_selected_model() # Standardize model getting
        output_dir = self.app.get_output_dir()
        os.makedirs(output_dir, exist_ok=True)
        dir_manager = self._get_directory_manager(output_dir)
        chapter_outlines_dir = dir_manager.get_chapter_outlines_path()
        scene_plans_dir = dir_manager.get_full_path("scene_plans_dir")
        print(f"Generating scene plans with model: {selected_model}, output dir: {output_dir}")

        # --- Read Parameters to get selected structure --- 
        parameters_file_path = dir_manager.get_parameters_path()
        selected_structure_name = "6-Act Structure" # Default
        try:
            params = {}
            if not os.path.exists(parameters_file_path):
                print(f"Warning: Parameters file not found at {parameters_file_path}. Using default structure for scene planning: {selected_structure_name}")
            else:
                with open(parameters_file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if ":" in line:
                            key, value = line.split(":", 1)
                            params[key.strip()] = value.strip()
                loaded_structure = params.get("Story Structure")
                if loaded_structure and loaded_structure.strip():
                    selected_structure_name = loaded_structure
                else:
                    print(f"Warning: 'Story Structure' not found or empty in {parameters_file_path}. Using default for scene planning: {selected_structure_name}")
            print(f"Using selected story structure for scene planning: {selected_structure_name}")
        except Exception as e:
            print(f"Error reading parameters file ({parameters_file_path}): {e}. Using default structure for scene planning: {selected_structure_name}")
        # --- End Reading Parameters ---

        # STRUCTURE_SECTIONS_MAP is imported from parameters.py
        sections_to_process = STRUCTURE_SECTIONS_MAP.get(selected_structure_name)

        if not sections_to_process:
            show_error("错误", f"找不到故事结构“{zh_label(selected_structure_name)}”的阶段定义。")
            print(f"Error (Scene Plan): No sections defined for structure '{selected_structure_name}'.")
            return

        try:
            # Load the overall lore content once
            lore_content_path = os.path.join(output_dir, "story", "lore", "generated_lore.md")
            try:
                lore_content = open_file(lore_content_path).strip()
            except FileNotFoundError:
                show_warning("文件缺失", f"找不到世界观文件 {lore_content_path}，场景规划可能缺少背景。")
                lore_content = "缺少整体世界观背景。"
            
            overall_chapter_number = 1 # Global chapter counter across all sections

            for current_section_name in sections_to_process:
                safe_selected_structure_name = selected_structure_name.lower().replace(' ', '_')
                safe_section_name = current_section_name.lower().replace(' ', '_').replace(':','').replace('/','_')
                
                # Input filename from generate_chapter_outline
                chapter_outline_input_base = f"chapter_outlines_{safe_selected_structure_name}_{safe_section_name}.md"
                chapter_outline_input_filepath = os.path.join(chapter_outlines_dir, chapter_outline_input_base)

                print(f"Scene Planning for section: {current_section_name} using file: {chapter_outline_input_filepath}")

                try:
                    section_chapter_outline_content = open_file(chapter_outline_input_filepath).strip()
                except FileNotFoundError:
                    show_warning("文件缺失", f"找不到“{zh_label(current_section_name)}”的章节大纲（文件：{chapter_outline_input_base}），将跳过该部分。")
                    print(f"Warning: File {chapter_outline_input_filepath} not found. Skipping scene planning for '{current_section_name}'.")
                    continue

                if not section_chapter_outline_content:
                    print(f"Warning: Chapter outline file {chapter_outline_input_filepath} is empty. Skipping scene planning for '{current_section_name}'.")
                    continue

                # Count chapters within this specific section's outline file
                # Using a more robust regex that looks for "Chapter X" or "Chapter X: Title"
                # This count is to iterate through the chapters *within this section file*
                chapters_in_section_file = re.findall(
                    r"^\s*(?:#{2,6}\s*|\*{2,}\s*)?(?:Chapter\s*|第\s*)(\d+)(?:\s*章)?(?:\s*[:：.\-]?\s*.*?)?\s*(?:\*{2,})?\s*$",
                    section_chapter_outline_content,
                    re.MULTILINE | re.IGNORECASE,
                )
                
                if not chapters_in_section_file:
                    print(f"Warning: No chapters detected in {chapter_outline_input_filepath}. Skipping scene planning for '{current_section_name}'.")
                    continue
                
                print(f"Detected {len(chapters_in_section_file)} chapters in outline for section '{current_section_name}'. Starting global chapter number for this section: {overall_chapter_number}")

                # Iterate for each chapter found in *this section's outline file*
                # The actual chapter number for the prompt is overall_chapter_number
                for i in range(len(chapters_in_section_file)):
                    current_chapter_for_prompt = overall_chapter_number + i
                    
                    prompt = (
                        f"请为科幻小说第 {current_chapter_for_prompt} 章规划场景。\n"
                        f"故事采用“{zh_label(selected_structure_name)}”框架，包含：{', '.join(zh_label(section) for section in sections_to_process)}。\n"
                        f"当前正在设计 **{zh_label(current_section_name)}** 的场景。\n\n"
                        f"以下是包含第 {current_chapter_for_prompt} 章的“{zh_label(current_section_name)}”逐章大纲：\n{section_chapter_outline_content}\n\n"
                        f"请重点把上述大纲中的第 {current_chapter_for_prompt} 章扩展为详细场景。"
                        "每个场景需说明：环境（行星、具体地点）、出场人物、关键行动/事件、关键对白片段（如有必要），以及它如何推动本章情节或人物发展。"
                        f"请保持人物弧光、势力和地点（包括行星）与第 {current_chapter_for_prompt} 章大纲中的建议一致。\n"
                        f"整体世界观如下，供参考：\n{lore_content}\n\n"
                        "请使用结构清晰的 Markdown，每个场景设置标题。"
                    )

                    current_backend = get_backend()
                    backend_info = f"{current_backend}" if current_backend != "api" else f"api/{selected_model}"
                    print(f"--- Scene Plan Prompt for Chapter {current_chapter_for_prompt} (Section: {current_section_name}, Backend: {backend_info}) ---")
                    # print(prompt) # Uncomment for full prompt debugging
                    print("-------------------------------------------------------------------")
                    response = send_prompt(prompt, model=selected_model)

                    output_scene_plan_base = f"scenes_{safe_selected_structure_name}_{safe_section_name}_ch{current_chapter_for_prompt}.md"
                    os.makedirs(scene_plans_dir, exist_ok=True)
                    output_scene_plan_filepath = os.path.join(scene_plans_dir, output_scene_plan_base)
                    write_file(output_scene_plan_filepath, response)
                    print(f"Scene plan for Chapter {current_chapter_for_prompt} saved to {output_scene_plan_filepath}")
                
                overall_chapter_number += len(chapters_in_section_file) # Increment global chapter counter by number of chapters processed in this section

            # show_success("Success", f"Scene plans for '{selected_structure_name}' generated successfully.")

        except FileNotFoundError as fnf_e:
            show_error("错误", f"场景规划找不到必需文件：{fnf_e}")
            print(f"Error (Scene Plan): File not found - {fnf_e}")
        except Exception as e:
            print(f"Failed to generate scene plans: {e}")
            import traceback
            traceback.print_exc()
            show_error("错误", f"生成场景规划失败：{str(e)}")

    # Renamed from scene_plan to indicate its use for longer forms
    def _plan_long_form_scenes(self):
        selected_model = self.app.get_selected_model() 
        output_dir = self.app.get_output_dir()
        os.makedirs(output_dir, exist_ok=True)
        dir_manager = self._get_directory_manager(output_dir)
        chapter_outlines_dir = dir_manager.get_chapter_outlines_path()
        scene_plans_dir = dir_manager.get_full_path("scene_plans_dir")
        self.app.logger.info(f"Planning Long-Form Scenes (Novella/Novel/Epic). Model: {selected_model}, Output Dir: {output_dir}")

        # --- Read Parameters to get selected structure and length ---
        parameters_file_path = dir_manager.get_parameters_path()
        selected_structure_name = "6-Act Structure" # Default
        story_length = "Novel (Standard)" # Default for prompt context
        try:
            params_from_file = {}
            if not os.path.exists(parameters_file_path):
                self.app.logger.warning(f"Parameters file not found at {parameters_file_path}. Using defaults for scene planning.")
            else:
                with open(parameters_file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if ":" in line:
                            key, value = line.split(":", 1)
                            params_from_file[key.strip()] = value.strip()
                loaded_structure = params_from_file.get("Story Structure")
                story_length = params_from_file.get("Story Length", story_length) # Get actual story length
                if loaded_structure and loaded_structure.strip():
                    selected_structure_name = loaded_structure
                else:
                    self.app.logger.warning(f"'Story Structure' not found/empty in {parameters_file_path}. Using default for scene planning: {selected_structure_name}")
            self.app.logger.info(f"Using structure: {selected_structure_name}, Length: {story_length} for long-form scene planning.")
        except Exception as e:
            self.app.logger.error(f"Error reading parameters file ({parameters_file_path}): {e}. Using defaults.", exc_info=True)
        # --- End Reading Parameters ---

        sections_to_process = STRUCTURE_SECTIONS_MAP.get(selected_structure_name)
        if not sections_to_process:
            self.app.logger.error(f"Scene Plan: Section definitions for '{selected_structure_name}' not found.")
            show_error("错误", f"找不到故事结构“{zh_label(selected_structure_name)}”的阶段定义。")
            return

        try:
            lore_content_path = os.path.join(output_dir, "story", "lore", "generated_lore.md")
            try:
                lore_content = open_file(lore_content_path).strip()
            except FileNotFoundError:
                show_warning("文件缺失", f"找不到世界观文件 {lore_content_path}，场景规划可能缺少背景。")
                self.app.logger.warning(f"Lore file {lore_content_path} not found for scene planning.")
                lore_content = "缺少整体世界观背景。"
            
            overall_chapter_number = 1 

            for current_section_name in sections_to_process:
                safe_selected_structure_name = selected_structure_name.lower().replace(' ', '_')
                safe_section_name = current_section_name.lower().replace(' ', '_').replace(':','').replace('/','_')
                
                chapter_outline_input_base = f"chapter_outlines_{safe_selected_structure_name}_{safe_section_name}.md"
                chapter_outline_input_filepath = os.path.join(chapter_outlines_dir, chapter_outline_input_base)

                self.app.logger.info(f"Scene Planning for section: {current_section_name} using chapter outline: {chapter_outline_input_filepath}")

                try:
                    section_chapter_outline_content = open_file(chapter_outline_input_filepath).strip()
                except FileNotFoundError:
                    show_warning("文件缺失", f"找不到“{zh_label(current_section_name)}”的章节大纲（文件：{chapter_outline_input_base}），将跳过该部分。")
                    self.app.logger.warning(f"File {chapter_outline_input_filepath} not found. Skipping scene planning for '{current_section_name}'.")
                    continue

                if not section_chapter_outline_content:
                    self.app.logger.warning(f"Chapter outline file {chapter_outline_input_filepath} is empty. Skipping scene planning for '{current_section_name}'.")
                    continue

                chapters_in_section_file = re.findall(
                    r"^\s*(?:#{2,6}\s*|\*{2,}\s*)?(?:Chapter\s*|第\s*)(\d+)(?:\s*章)?(?:\s*[:：.\-]?\s*.*?)?\s*(?:\*{2,})?\s*$",
                    section_chapter_outline_content,
                    re.MULTILINE | re.IGNORECASE,
                )
                
                if not chapters_in_section_file:
                    self.app.logger.warning(f"No chapters detected in {chapter_outline_input_filepath}. Skipping scene planning for '{current_section_name}'.")
                    continue
                
                self.app.logger.info(f"Detected {len(chapters_in_section_file)} chapters in outline for section '{current_section_name}'. Starting global chapter number for this section: {overall_chapter_number}")

                for i in range(len(chapters_in_section_file)):
                    current_chapter_for_prompt = overall_chapter_number + i
                    
                    prompt_lines = [
                        f"请为故事第 {current_chapter_for_prompt} 章规划场景（篇幅：{zh_label(story_length)}）。",
                        f"故事采用“{zh_label(selected_structure_name)}”框架，包含：{', '.join(zh_label(section) for section in sections_to_process)}。",
                        f"当前正在设计 **{zh_label(current_section_name)}** 的场景。",
                        f"\n以下是包含第 {current_chapter_for_prompt} 章的“{zh_label(current_section_name)}”逐章大纲：\n{section_chapter_outline_content}",
                        f"\n请重点把上述大纲中的第 {current_chapter_for_prompt} 章扩展为详细场景。"
                    ]
                    
                    if story_length == "Novella":
                        prompt_lines.append("本故事是中篇小说，本章场景应紧凑而有冲击力，聚焦必要的情节推进和人物时刻。")
                    
                    prompt_lines.extend([
                        "每个场景需说明：环境（行星、具体地点）、出场人物、关键行动/事件、关键对白片段（如有必要），以及它如何推动本章情节或人物发展。",
                        f"请保持人物弧光、势力和地点（包括行星）与第 {current_chapter_for_prompt} 章大纲中的建议一致。",
                        f"整体世界观如下，供参考：\n{lore_content}",
                        "\n请使用结构清晰的 Markdown，每个场景设置标题。"
                    ])
                    prompt = "\n".join(prompt_lines)

                    self.app.logger.info(f"--- Scene Plan Prompt for Chapter {current_chapter_for_prompt} (Section: {current_section_name}, Length: {story_length}) ---")
                    # Log prompt saving here (using save_prompt_to_file from helper_fns)
                    prompt_base_name = f"plan_scenes_ch{current_chapter_for_prompt}_{safe_selected_structure_name}_{safe_section_name}_prompt"
                    prompt_filepath = save_prompt_to_file(output_dir, prompt_base_name, prompt) # Assuming save_prompt_to_file is imported
                    
                    current_backend = get_backend()
                    backend_info = f"{current_backend}" if current_backend != "api" else f"api/{selected_model}"
                    log_msg_source = f"(from {prompt_filepath})" if prompt_filepath else "(from memory, save failed)"
                    self.app.logger.info(f"Sending Scene Planning Prompt {log_msg_source} to LLM ({backend_info})...")

                    response = send_prompt(prompt, model=selected_model)

                    # --- Create subdirectory for these scene plans ---
                    output_scene_plan_base = f"scenes_{safe_selected_structure_name}_{safe_section_name}_ch{current_chapter_for_prompt}.md"
                    os.makedirs(scene_plans_dir, exist_ok=True)
                    output_scene_plan_filepath = os.path.join(scene_plans_dir, output_scene_plan_base)
                    write_file(output_scene_plan_filepath, response)
                    self.app.logger.info(f"Scene plan for Chapter {current_chapter_for_prompt} saved to {output_scene_plan_filepath}")
                
                overall_chapter_number += len(chapters_in_section_file) 

            #show_success("Success", f"Scene plans for '{selected_structure_name}' generated successfully.")

        except FileNotFoundError as fnf_e:
            self.app.logger.error(f"Scene Plan: File not found - {fnf_e}", exc_info=True)
            show_error("错误", f"场景规划找不到必需文件：{fnf_e}")
        except Exception as e:
            self.app.logger.error(f"Failed to generate scene plans: {e}", exc_info=True)
            show_error("错误", f"生成场景规划失败：{str(e)}")


    def _plan_short_story_scenes(self):
        selected_model = self.app.get_selected_model()
        output_dir = self.app.get_output_dir()
        os.makedirs(output_dir, exist_ok=True)
        self.app.logger.info(f"Planning Short Story Scenes. Model: {selected_model}, Output Dir: {output_dir}")

        # --- Read Parameters ---
        if not (self.app and hasattr(self.app, 'param_ui')):
            self.app.logger.error("ScenePlanning: Parameters.py not available for short story scene planning.")
            show_error("错误", "无法加载故事参数。")
            return
        
        parameters = self.app.param_ui.get_current_parameters()
        selected_structure_name = parameters.get("story_structure")
        novel_title = parameters.get("novel_title", "未命名短篇小说")

        if not selected_structure_name:
            self.app.logger.error("ScenePlanning: No story structure selected for short story scene planning.")
            show_error("错误", "尚未选择故事结构，请先在“作品参数”中选择。")
            return

        # --- Input File: Detailed Short Story Plot ---
        # Filename based on the output of _outline_short_story_plot in story_structure.py
        safe_structure_name_for_file = selected_structure_name.lower().replace(' ', '_').replace(':', '').replace('/', '_')
        detailed_plot_filename = f"plot_short_story_{safe_structure_name_for_file}.md"
        detailed_plot_filepath = os.path.join(output_dir, "story", "structure", detailed_plot_filename)

        try:
            self.app.logger.info(f"Loading detailed short story plot from: {detailed_plot_filepath}")
            short_story_plot_content = open_file(detailed_plot_filepath)
            if not short_story_plot_content.strip():
                self.app.logger.error(f"Detailed short story plot file is empty: {detailed_plot_filepath}")
                show_error("错误", f"详细情节文件“{detailed_plot_filename}”为空，无法规划场景。")
                return
        except FileNotFoundError:
            self.app.logger.error(f"Detailed short story plot file not found: {detailed_plot_filepath}")
            show_error("错误", f"找不到详细情节文件“{detailed_plot_filename}”，请先在“故事结构”页生成。")
            return
        except Exception as e:
            self.app.logger.error(f"Error loading detailed short story plot file '{detailed_plot_filepath}': {e}", exc_info=True)
            show_error("错误", f"无法读取“{detailed_plot_filename}”。")
            return

        # --- Load Lore Context (Optional but good) ---
        lore_content = "缺少整体世界观背景。"
        try:
            lore_content_path = os.path.join(output_dir, "story", "lore", "generated_lore.md")
            if os.path.exists(lore_content_path):
                lore_content = open_file(lore_content_path).strip()
                self.app.logger.info(f"Loaded lore context from {lore_content_path}")
        except Exception as e:
            self.app.logger.warning(f"Could not load lore for short story scene planning: {e}")

        # --- Construct the Prompt ---
        prompt_lines = [
            f"请为短篇小说《{novel_title}》规划场景。",
            f"故事采用“{zh_label(selected_structure_name)}”框架。",
            "以下是完整短篇小说的详细总体情节：\n\n",
            "--- 短篇小说详细情节 ---",
            short_story_plot_content,
            "\n\n--- 短篇小说详细情节结束 ---",
            "\n请根据详细情节，把故事拆分成一系列清晰、独立的场景。",
            "每个场景需说明：\n",
            "  - 建议的场景编号（如场景 1、场景 2）。\n",
            "  - 环境（行星、具体地点）。\n",
            "  - 出场人物。\n",
            "  - 场景中的关键行动和事件。\n",
            "  - 关键对白片段或对白概要。\n",
            "  - 该场景如何依据详细情节推动整体故事，或发展人物/主题。\n",
            "确保场景之间衔接自然，并覆盖详细情节中的完整叙事弧。\n",
            "\n重要格式要求：",
            "每个场景必须以 Markdown 标题开始，例如“### 场景 <编号>：<场景标题>”或“## 场景 <编号> - <场景标题>”。",
            "标题下方再列出该场景的环境、人物、关键行动等要点。",
            "\n如有需要，可参考以下整体世界观：",
            lore_content,
            "\n现在请按场景输出完整的短篇规划，合并为一份结构清晰的 Markdown 文档，不要使用代码围栏。"
        ]
        prompt = "\n".join(prompt_lines)

        prompt_base_name = f"plan_scenes_short_story_{safe_structure_name_for_file}_prompt"
        prompt_filepath = save_prompt_to_file(output_dir, prompt_base_name, prompt)
        
        current_backend = get_backend()
        backend_info = f"{current_backend}" if current_backend != "api" else f"api/{selected_model}"
        log_msg_source = f"(from {prompt_filepath})" if prompt_filepath else "(from memory, save failed)"
        self.app.logger.info(f"Sending Short Story Scene Planning Prompt {log_msg_source} to LLM ({backend_info})...")

        response = send_prompt(prompt, model=selected_model)

        if not response:
            self.app.logger.error(f"Failed to generate short story scenes from LLM ({backend_info}). No response.")
            show_error("错误", "大模型生成短篇场景失败。")
            return
        
        self.app.logger.info(f"Received short story scenes from LLM. Length: {len(response)} chars.")

        output_filename_base = f"scenes_short_story_{safe_structure_name_for_file}.md"
        os.makedirs(os.path.join(output_dir, "story", "planning"), exist_ok=True)
        output_filename_full_path = os.path.join(output_dir, "story", "planning", output_filename_base)
        
        try:
            write_file(output_filename_full_path, response)
            self.app.logger.info(f"Short story scenes saved successfully to {output_filename_full_path}")
            # show_success("Success", f"Short story scenes generated and saved to {output_filename_full_path}")
        except Exception as e:
            self.app.logger.error(f"Error saving short story scenes to {output_filename_full_path}: {e}", exc_info=True)
            show_error("错误", f"保存短篇场景失败：{e}")
