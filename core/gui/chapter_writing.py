from tkinter import ttk, messagebox
from core.gui.notifications import show_success, show_error, show_info, show_warning
from core.generation.ai_helper import send_prompt, get_backend
import re
from core.generation.helper_fns import (
    archive_failed_generation,
    open_file,
    write_file,
    save_prompt_to_file,
    read_json,
    parse_scene_sections,
)
from core.generation.prompt_context import (
    analyze_chinese_prose_style,
    find_scene_world_conflicts,
    format_faction_summary,
    normalize_story_parameters,
    sanitize_lore_content,
)
from core.generation.chapter_generation_loop import ChapterGenerationLoop, QualityGateError
from core.generation.domain_profiles import resolve_domain_profile
from core.generation.scene_prompt import build_scene_prompt, scene_prompt_filename
from core.generation.cancellation import CancelToken, GenerationCancelled
from core.gui.task_runner import run_in_background
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict
import os
from core.gui.parameters import STRUCTURE_SECTIONS_MAP # Import for section mapping
from core.localization import zh_label

@dataclass
class WritingRequest:
    """界面输入的快照。

    后台线程不得读写 Tk 部件，因此所有来自界面的取值（模型、输出目录、章节编号、
    作品参数）都在主线程一次性读好，再整体交给工作线程。
    """

    output_dir: str
    model: str
    chapter_number: int = 0
    parameters: Dict[str, Any] = field(default_factory=dict)


class ChapterWriting:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app  # Store the app instanc
        self.cancel_token = CancelToken()
        
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
        self.rewrite_button = ttk.Button(self.chapter_writing_frame, text="重写", command=self.dispatch_rewrite)
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

        # 停止按钮：只在后台任务运行时可用。取消是协作式的，会在下一个场景或
        # 章节边界生效，已完成的章节保持不变。
        self.cancel_button = ttk.Button(automation_buttons_frame, text="⏹ 停止", state="disabled")
        self.cancel_button.pack(side="left", padx=5)

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
                
    # --- 主线程调度：读取界面输入，把耗时工作交给后台线程 ---

    def _busy_widgets(self):
        return (
            self.write_prose_button,
            self.rewrite_button,
            self.analyze_button,
            self.write_next_button,
            self.write_all_button,
        )

    def _start_background(self, work, *, busy_button, busy_text, on_success=None, refresh_progress=True):
        """Run `work` off the main thread with the tab's buttons locked."""
        def done():
            if refresh_progress:
                self.update_progress_display()

        return run_in_background(
            self.app.root,
            work,
            on_success=on_success,
            on_error=lambda exc: show_error("错误", str(exc)),
            on_cancelled=lambda exc: show_info("已停止", str(exc)),
            on_done=done,
            busy_widgets=self._busy_widgets(),
            busy_button=busy_button,
            busy_text=busy_text,
            cancel_button=self.cancel_button,
            cancel_token=self.cancel_token,
            logger=self.app.logger if self.app else None,
        )

    def _build_request(self, parameters=None, chapter_number=0):
        """Snapshot every UI-derived value the worker will need."""
        return WritingRequest(
            output_dir=self.app.get_output_dir() if self.app else "current_work",
            model=self.app.get_selected_model() if self.app else "",
            chapter_number=chapter_number,
            parameters=parameters or {},
        )

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
            request = self._build_request(parameters=params)
            self._start_background(
                lambda: self._write_short_story_prose(request),
                busy_button=self.write_prose_button,
                busy_text="正在撰写短篇…",
            )
            return

        if story_length not in ["Novella", "Novel (Standard)", "Novel (Epic)"]:
            self.app.logger.error(f"ChapterWriting: Unknown story length '{story_length}' for prose generation.")
            show_error("错误", f"章节写作不支持故事篇幅“{zh_label(story_length)}”。")
            return

        try:
            chapter_number = int(self.chapter_number_entry.get())
        except ValueError:
            self.app.logger.error("Invalid chapter number entered.", exc_info=True)
            show_error("错误", "请输入有效的章节编号。")
            return
        if chapter_number <= 0:
            show_error("错误", "章节编号必须是正整数。")
            return

        request = self._build_request(parameters=params, chapter_number=chapter_number)
        self._start_background(
            lambda: self.write_chapter(request),
            busy_button=self.write_prose_button,
            busy_text=f"正在撰写第 {chapter_number} 章…",
        )

    def dispatch_rewrite(self):
        """Read the chapter number on the main thread, then rewrite in the background."""
        try:
            chapter_number = int(self.chapter_number_entry.get())
        except ValueError:
            self.app.logger.error("Invalid chapter number entered for rewrite.", exc_info=True)
            show_error("错误", "请输入有效的待重写章节编号。")
            return

        request = self._build_request(chapter_number=chapter_number)
        self._start_background(
            lambda: self.rewrite_chapter(request),
            busy_button=self.rewrite_button,
            busy_text="正在重写…",
            refresh_progress=False,
        )

    def analyze_chapters(self):
        """Analyze the chapter structure and update progress display."""
        output_dir = self.app.get_output_dir() if self.app else "current_work"

        def work():
            from agents.writing.chapter_writing_agent import analyze_story_chapters

            chapter_info_list, plan = analyze_story_chapters(output_dir, None)
            missing_scene_plans = [
                chapter.chapter_number
                for chapter in chapter_info_list
                if not chapter.exists and not chapter.plan_exists
            ]
            return {
                "total": len(chapter_info_list),
                "completed": len(plan.chapters_completed),
                "remaining": len(plan.chapters_to_write),
                "next_chapters": list(plan.chapters_to_write),
                "missing_scene_plans": missing_scene_plans,
            }

        def report(summary):
            message = (
                f"分析完成！\n\n章节总数：{summary['total']}\n"
                f"已完成：{summary['completed']}\n剩余：{summary['remaining']}"
            )
            next_chapters = summary["next_chapters"]
            if next_chapters:
                message += f"\n\n接下来撰写：{', '.join(map(str, next_chapters[:5]))}"
                if len(next_chapters) > 5:
                    message += f"（另有 {len(next_chapters) - 5} 章）"
            if summary["missing_scene_plans"]:
                message += (
                    "\n\n尚未生成详细场景规划：第 "
                    + "、".join(map(str, summary["missing_scene_plans"]))
                    + " 章。这些章节需先在“场景规划”页补齐。"
                )
            show_success("章节分析", message)

        self._start_background(
            work,
            busy_button=self.analyze_button,
            busy_text="📊 正在分析…",
            on_success=report,
        )

    def write_next_chapter(self):
        """Write the next chapter automatically."""
        self._start_batch_write(batch_size=1, busy_button=self.write_next_button, busy_text="✍️ 正在撰写…")

    def write_all_chapters(self):
        """Write all remaining chapters automatically."""
        self._start_batch_write(batch_size=5, busy_button=self.write_all_button, busy_text="🚀 正在撰写全部章节…")

    def _start_batch_write(self, batch_size, busy_button, busy_text):
        output_dir = self.app.get_output_dir() if self.app else "current_work"
        model = self.app.get_selected_model() if self.app else None
        token = self.cancel_token

        def work():
            from agents.writing.chapter_writing_agent import ChapterWritingAgent

            # app_instance 会被后台线程用来读取界面上的模型选择，所以这里不传实例，
            # 改为把主线程读到的模型直接交给智能体。
            agent = ChapterWritingAgent(output_dir, None, model=model, cancel_token=token)
            chapter_info_list, _ = agent.analyze_chapter_structure()
            plan = agent.create_writing_plan(chapter_info_list, batch_size)
            return agent.write_chapters_batch(chapter_info_list, plan)

        def report(result):
            if not result.success:
                error_message = "; ".join(result.messages) if result.messages else "未知错误"
                show_error("写作错误", f"撰写章节失败：{error_message}")
                return
            chapters_written = result.data.get("chapters_written", [])
            errors = result.data.get("errors", [])
            if not chapters_written and not errors:
                show_success("已完成", "所有章节都已经写完！")
                return
            message = f"已写完 {len(chapters_written)} 章"
            if chapters_written:
                message += f"：第 {'、'.join(map(str, chapters_written))} 章"
            if errors:
                message += f"\n\n{len(errors)} 章失败：{'; '.join(errors[:3])}"
                if len(errors) > 3:
                    message += f"（另有 {len(errors) - 3} 条）"
                show_warning("批量写作结束", message)
            else:
                show_success("批量写作完成", message)

        self._start_background(
            work,
            busy_button=busy_button,
            busy_text=busy_text,
            on_success=report,
        )

    def _write_short_story_prose(self, request: "WritingRequest"):
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

        try:
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
            except Exception as e:
                self.app.logger.error(f"Error reading scene plan file '{scene_plan_filepath}': {e}", exc_info=True)
                show_error("错误", f"无法读取场景规划文件：{e}")
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
                scene_prose = send_prompt(prompt, model=selected_model)
                if not scene_prose or not scene_prose.strip():
                    raise RuntimeError(f"大模型未返回第 {scene_number} 个场景的正文")
                self.app.logger.info(
                    f"Received prose for Scene {scene_number}. Length: {len(scene_prose)} chars."
                )
                style_warnings = analyze_chinese_prose_style(scene_prose)
                if style_warnings:
                    self.app.logger.warning(
                        f"Scene {scene_number} Chinese style warnings: {'; '.join(style_warnings)}"
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

            full_story_content = loop_result.chapter_content
            safe_title = (novel_title or "未命名短篇小说").lower().replace(' ', '_').replace(':', '').replace('/', '')
            output_story_filename = f"prose_short_story_{safe_title}.md"
            os.makedirs(os.path.join(output_dir, "story", "content"), exist_ok=True)
            output_story_filepath = os.path.join(output_dir, "story", "content", output_story_filename)

            write_file(output_story_filepath, full_story_content)
            quality_loop.accept_result(1, loop_result, chapter_path=output_story_filepath)
            self.app.logger.info(f"Short story prose successfully written to: {output_story_filepath}")

        except GenerationCancelled:
            raise  # 由 task_runner 统一报告为「已停止」，不是错误
        except Exception as e:
            self.app.logger.error(f"An unexpected error occurred in _write_short_story_prose: {e}", exc_info=True)
            show_error("错误", f"发生意外错误：{str(e)}")

    def _load_lore(self, output_dir):
        """Load sanitized lore, tolerating a missing file."""
        try:
            lore_path = os.path.join(output_dir, "story", "lore", "generated_lore.md")
            if os.path.exists(lore_path):
                content = sanitize_lore_content(open_file(lore_path))
                self.app.logger.info(f"Loaded lore context from {lore_path}")
                return content
        except Exception as e:
            self.app.logger.warning(f"Could not load lore: {e}", exc_info=True)
        return "Lore context is missing or not loaded."

    def _load_character_roster(self, output_dir):
        """Summarize the character roster for prompt context."""
        characters_json_path = os.path.join(output_dir, "story", "lore", "characters.json")
        try:
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
                details.append(f" - 外貌：{char_info.get('appearance_summary', '无')}\n")
                goals = char_info.get('goals', [])
                if goals:
                    details.append(f" - 主要目标：{goals[0]}\n")
                strengths = char_info.get('strengths', [])
                if strengths:
                    details.append(f" - 主要优点：{strengths[0]}\n")
                flaws = char_info.get('flaws', [])
                if flaws:
                    details.append(f" - 主要缺点：{flaws[0]}\n")
                backstory = char_info.get('backstory_summary', '')
                if backstory:
                    details.append(f" - 背景摘要：{backstory}")
                summaries.append("\n".join(details))
            if summaries:
                self.app.logger.info(f"Loaded and summarized character roster from {characters_json_path}")
                return "主要人物：\n" + "\n".join(summaries)
        except Exception as e:
            self.app.logger.warning(
                f"Could not load or process character roster from {characters_json_path}: {e}",
                exc_info=True,
            )
        return "没有可用的人物名单。"

    def _load_faction_summary(self, output_dir):
        """Summarize faction data for prompt context."""
        factions_json_path = os.path.join(output_dir, "story", "lore", "factions.json")
        try:
            if os.path.exists(factions_json_path):
                factions_data = read_json(factions_json_path)
                if factions_data:
                    self.app.logger.info(f"Loaded and summarized faction info from {factions_json_path}")
                    return format_faction_summary(factions_data)
        except Exception as e:
            self.app.logger.warning(
                f"Could not load or process faction info from {factions_json_path}: {e}",
                exc_info=True,
            )
        return "没有可用的势力信息。"

    def normalize_markdown(self, scenes):
        # Match scene headings specifically (e.g., **Scene 1: ...**)
        normalized_scenes = re.sub(r"\*\*Scene (\d+): (.+?)\*\*", r"### Scene \1: \2", scenes)
        return normalized_scenes

    def write_chapter(self, request: "WritingRequest"):
        """Runs on a worker thread; UI values arrive via the request snapshot."""
        selected_model = request.model
        output_dir = request.output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.app.logger.info(f"Initiating Write Chapter. Model: {selected_model}, Output Dir: {output_dir}")

        try:
            target_chapter_number_global = request.chapter_number
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

            character_roster_summary = self._load_character_roster(output_dir)
            faction_summary_info = self._load_faction_summary(output_dir)
            lore_content = self._load_lore(output_dir)

            conflicts = find_scene_world_conflicts(scenes_content_for_chapter, lore_content, story_params)
            if conflicts:
                conflict_text = "、".join(conflicts)
                self.app.logger.error(f"Scene plan conflicts with non-scifi lore: {conflict_text}")
                show_error("场景规划与世界观冲突", f"第 {target_chapter_number_global} 章场景规划包含世界观未定义的科幻设定：{conflict_text}。请重新生成该章场景规划。")
                return

            quality_loop = ChapterGenerationLoop(
                output_dir=output_dir,
                model=selected_model,
                logger=self.app.logger,
                cancel_token=self.cancel_token,
            )

            def generate_scene_prose(
                scene_plan,
                scene_number,
                previous_scene_tail="",
                next_scene_plan="",
                contract=None,
                profile=None,
            ):
                """Generate one scene with explicit continuity boundaries."""
                self.app.logger.info(
                    f"Processing Scene {scene_number} for Chapter {target_chapter_number_global}."
                )
                prompt = build_scene_prompt(
                    scene_plan=scene_plan,
                    scene_number=scene_number,
                    parameters=story_params,
                    lore=lore_content,
                    character_roster=character_roster_summary,
                    faction_summary=faction_summary_info,
                    profile=profile or resolve_domain_profile(story_params),
                    chapter_number=target_chapter_number_global,
                    structure_name=selected_structure_name,
                    section_name=current_section_name_for_chapter,
                    contract=contract,
                    previous_scene_tail=previous_scene_tail,
                    next_scene_plan=next_scene_plan,
                )

                prompt_filepath = save_prompt_to_file(
                    output_dir,
                    scene_prompt_filename(scene_number, target_chapter_number_global),
                    prompt,
                )
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

            def save_revised_plan(revised_plan):
                archive_dir = os.path.join(output_dir, "archive", "quality_loop", "scene_plans")
                os.makedirs(archive_dir, exist_ok=True)
                base_name = os.path.splitext(scene_plan_filename_base)[0]
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                archive_path = os.path.join(archive_dir, f"{base_name}_before_{timestamp}.md")
                write_file(archive_path, scenes_content_for_chapter)
                write_file(scene_plan_filepath, revised_plan)
                self.app.logger.info(
                    f"Quality loop revised the scene plan; original archived at {archive_path}."
                )

            try:
                loop_result = quality_loop.run(
                    chapter_number=target_chapter_number_global,
                    plan_content=scenes_content_for_chapter,
                    parameters=story_params,
                    lore=lore_content,
                    generate_scene=generate_scene_prose,
                    on_plan_revised=save_revised_plan,
                )
            except QualityGateError as gate_error:
                # 未通过质量闸门的正文绝不写入稿件目录，只归档供人工查看。
                archived = archive_failed_generation(
                    output_dir, target_chapter_number_global, gate_error.partial_scenes
                )
                self.app.logger.error(
                    f"Chapter {target_chapter_number_global} failed the quality gate: {gate_error}"
                    + (f" Partial prose archived at {archived}." if archived else "")
                )
                detail = f"\n\n已生成的部分正文归档在：\n{archived}" if archived else ""
                show_error("质量检查未通过", f"{gate_error}{detail}")
                return

            generated_scenes_for_chapter = loop_result.scenes

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
            quality_loop.accept_result(
                target_chapter_number_global,
                loop_result,
                chapter_path=chapter_filepath_output,
            )
            self.app.logger.info(f"Chapter {target_chapter_number_global} successfully written to: {chapter_filepath_output}")
            # show_success("Success", f"Chapter {target_chapter_number_global} generated and saved to {chapter_filename_output}")

        except GenerationCancelled:
            raise  # 由 task_runner 统一报告为「已停止」，不是错误
        except Exception as e_main:
            self.app.logger.error(f"Failed to write chapter {target_chapter_number_global if 'target_chapter_number_global' in locals() else 'UNKNOWN'}: {e_main}", exc_info=True)
            show_error("错误", f"撰写章节失败：{str(e_main)}")

    def rewrite_chapter(self, request: "WritingRequest"):
        """Runs on a worker thread; UI values arrive via the request snapshot."""
        try:
            chapter_number = request.chapter_number
            output_dir = request.output_dir
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

            response = send_prompt(prompt, model=request.model)

            output_rewrite_filename = f"re_chapter_{chapter_number}.md"
            # Write to the chapters subdirectory
            output_rewrite_filepath = os.path.join(full_chapters_subdir_path, output_rewrite_filename)
            
            write_file(output_rewrite_filepath, response)
            self.app.logger.info(f"Chapter {chapter_number} re-written and saved successfully to: {output_rewrite_filepath}") # Changed from print
            show_success("成功", f"第 {chapter_number} 章已重写并保存到 {output_rewrite_filename}")

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
    
