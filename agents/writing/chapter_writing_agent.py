#!/usr/bin/env python3
"""
Automated Chapter Writing Agent for NovelWriter.

This agent provides intelligent automation for chapter writing:
- Detects which chapters need to be written
- Writes chapters sequentially or in batches
- Integrates with existing chapter writing workflow
- Provides progress tracking and quality validation
"""

import os
import logging
import json
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, asdict

from agents.base.agent import BaseAgent, AgentResult
from core.generation.helper_fns import (
    archive_failed_generation,
    open_file,
    write_file,
    read_json,
    parse_chapter_numbers,
    parse_scene_sections,
    publish_chapter_with_acceptance,
    resolve_section_chapter_numbers,
)
from core.generation.ai_helper import send_prompt, get_backend
from core.generation.prompt_context import (
    analyze_chinese_prose_style,
    generate_prose_with_style_retry,
    format_faction_summary,
    find_scene_world_conflicts,
    sanitize_lore_content,
)
from core.generation.cancellation import CancelToken, GenerationCancelled, raise_if_cancelled
from core.generation.chapter_generation_loop import ChapterGenerationLoop, QualityGateError
from core.generation.chapter_acceptance import ChapterAcceptanceError
from core.generation.conflict_briefing import (
    build_briefing,
    conflict_record_path,
    describe_briefing,
    needs_author_decision,
)
from core.generation.planning_contract import (
    PlanningContractError,
    load_planning_contracts,
    validate_contract_sequence,
)
from core.generation.domain_profiles import DomainProfile, resolve_domain_profile
from core.generation.semantic_identity import (
    has_domain_id_collision,
    resolve_domain_identities,
)
from core.generation.scene_prompt import build_scene_prompt, scene_prompt_filename
from core.generation.story_ledger import StoryLedgerManager
from core.config.directory_config import get_directory_manager
from core.config.story_options import STRUCTURE_SECTIONS_MAP

# Import review system (with fallback if not available)
try:
    from agents.review.review_agent import ReviewAndRetryAgent, ContentReview
    REVIEW_AVAILABLE = True
except ImportError:
    REVIEW_AVAILABLE = False
    ContentReview = None


@dataclass
class SceneReview:
    """Review data for a single scene."""
    scene_number: int
    chapter_number: int
    quality_score: float
    word_count: int
    issues: List[str]
    strengths: List[str]
    suggestions: List[str]
    timestamp: str
    confidence: float


@dataclass
class ChapterReview:
    """Review data for a complete chapter."""
    chapter_number: int
    section_name: str
    overall_quality: float
    scene_reviews: List[SceneReview]
    coherence_score: float
    pacing_score: float
    character_development_score: float
    total_word_count: int
    issues: List[str]
    strengths: List[str]
    suggestions: List[str]
    timestamp: str
    confidence: float


@dataclass
class BatchReview:
    """Review data for a batch of chapters."""
    batch_number: int
    chapter_numbers: List[int]
    chapter_reviews: List[ChapterReview]
    consistency_score: float
    progression_score: float
    style_consistency_score: float
    total_word_count: int
    average_quality: float
    issues: List[str]
    strengths: List[str]
    suggestions: List[str]
    timestamp: str
    confidence: float


@dataclass
class QualityThresholds:
    """User-configurable quality thresholds for review system."""
    minimum_scene_quality: float = 0.6
    minimum_chapter_quality: float = 0.65
    minimum_batch_quality: float = 0.7
    coherence_threshold: float = 0.6
    pacing_threshold: float = 0.6
    character_development_threshold: float = 0.6
    consistency_threshold: float = 0.7
    progression_threshold: float = 0.6
    style_consistency_threshold: float = 0.65
    retry_below_threshold: bool = True
    max_retries: int = 2


@dataclass
class QualityTrend:
    """Quality trend data for analytics."""
    timestamp: str
    chapter_number: int
    scene_number: Optional[int] = None
    quality_score: float = 0.0
    review_type: str = "scene"  # "scene", "chapter", "batch"
    improvement_from_previous: Optional[float] = None
    issues_resolved: int = 0
    new_issues_found: int = 0
    retry_attempt: int = 0


@dataclass
class ChapterWritingPlan:
    """Plan for automated chapter writing."""
    total_chapters: int
    chapters_to_write: List[int]  # Chapter numbers to write
    chapters_completed: List[int]  # Already written chapters
    current_chapter: Optional[int] = None
    batch_size: int = 1  # How many chapters to write in one session
    quality_check: bool = True
    enable_reviews: bool = True  # Enable multi-level review system
    quality_thresholds: Optional[QualityThresholds] = None  # Phase 3: Configurable thresholds


@dataclass
class ChapterInfo:
    """Information about a specific chapter."""
    chapter_number: int
    section_name: str
    scene_plan_file: str
    output_file: str
    exists: bool = False
    plan_exists: bool = False


class ChapterWritingAgent(BaseAgent):
    """
    Automated agent for writing novel chapters.
    
    This agent can:
    - Analyze story structure to determine total chapters
    - Detect which chapters are already written
    - Write chapters automatically in sequence
    - Provide progress tracking and validation
    """
    
    # 类级默认值：绕过 __init__ 构造的实例（测试里用 object.__new__）也能安全读取。
    cancel_token: Optional[CancelToken] = None
    require_planning_contract: bool = True

    def __init__(self, output_dir: str, app_instance=None, use_new_structure: Optional[bool] = None,
                 quality_thresholds: Optional[QualityThresholds] = None,
                 model: Optional[str] = None,
                 cancel_token: Optional[CancelToken] = None):
        super().__init__(name="ChapterWritingAgent", model=model)
        self.output_dir = output_dir
        self.cancel_token = cancel_token
        self.require_planning_contract = True
        self.app = app_instance
        self.use_new_structure = (
            self._detect_structured_workspace(output_dir)
            if use_new_structure is None
            else use_new_structure
        )
        self.dir_manager = get_directory_manager(output_dir, self.use_new_structure)
        self.logger = logging.getLogger(self.__class__.__name__)
        
        # Phase 3: Configurable quality thresholds
        self.quality_thresholds = quality_thresholds or QualityThresholds()
        
        # Phase 3: Performance optimization - cache frequently accessed data
        self._quality_trends_cache = []
        self._last_cache_update = None
        self._cache_ttl_seconds = 300  # 5 minutes
        
        # Initialize review agent if available
        self.review_agent = None
        if REVIEW_AVAILABLE:
            try:
                self.review_agent = ReviewAndRetryAgent()
                self.logger.info("Review system initialized")
            except Exception as e:
                self.logger.warning(f"Failed to initialize review system: {e}")
                self.review_agent = None

    @staticmethod
    def _detect_structured_workspace(output_dir: str) -> bool:
        """Detect projects using story/system/quality directory layout."""
        structured_markers = (
            os.path.join(output_dir, "system", "parameters.txt"),
            os.path.join(output_dir, "story", "planning"),
            os.path.join(output_dir, "story", "structure"),
        )
        return any(os.path.exists(path) for path in structured_markers)
    
    def get_available_tools(self) -> List[str]:
        """Return list of available tools/capabilities for this agent."""
        return [
            "analyze_chapter_structure",
            "create_writing_plan", 
            "write_chapters_batch",
            "write_single_chapter",
            "get_progress_report",
            "quality_review_system",
            "batch_processing"
        ]
    
    def get_required_fields(self) -> List[str]:
        """Return list of required fields for task processing."""
        return [
            "task_type",  # "analyze_structure", "write_chapters", "write_single_chapter"
            "output_dir",
            "chapter_info_list"  # Optional, can be generated if not provided
        ]
    
    def process_task(self, task_data: Dict[str, Any]) -> AgentResult:
        """Process different types of chapter writing tasks."""
        try:
            task_type = task_data.get("task_type")
            
            if task_type == "analyze_structure":
                chapter_info_list, story_params = self.analyze_chapter_structure()
                return AgentResult(
                    success=True,
                    data={
                        "chapter_info_list": [asdict(info) for info in chapter_info_list],
                        "story_parameters": story_params,
                        "total_chapters": len(chapter_info_list)
                    },
                    messages=[f"结构分析完成：找到 {len(chapter_info_list)} 章"],
                    metrics={}
                )
                
            elif task_type == "write_chapters":
                chapter_info_list = task_data.get("chapter_info_list", [])
                if not chapter_info_list:
                    # Generate chapter info if not provided
                    chapter_info_list, _ = self.analyze_chapter_structure()
                
                batch_size = task_data.get("batch_size", 1)
                plan = self.create_writing_plan(chapter_info_list, batch_size)
                
                return self.write_chapters_batch(chapter_info_list, plan)
                
            elif task_type == "write_single_chapter":
                chapter_number = task_data.get("chapter_number")
                chapter_info_list = task_data.get("chapter_info_list", [])
                
                if not chapter_info_list:
                    chapter_info_list, _ = self.analyze_chapter_structure()
                
                # Find the specific chapter
                target_chapter = None
                for chapter_info in chapter_info_list:
                    if chapter_info.chapter_number == chapter_number:
                        target_chapter = chapter_info
                        break
                
                if not target_chapter:
                    return AgentResult(
                        success=False,
                        data={},
                        messages=[f"在故事结构中找不到第 {chapter_number} 章"],
                        metrics={}
                    )
                
                return self._write_single_chapter(target_chapter)
                
            else:
                return AgentResult(
                    success=False,
                    data={},
                    messages=[f"未知任务类型：{task_type}"],
                    metrics={}
                )
                
        except Exception as e:
            self.logger.error(f"Error processing task: {e}")
            return AgentResult(
                success=False,
                data={},
                messages=[f"任务处理失败：{str(e)}"],
                metrics={}
            )
        
    def analyze_chapter_structure(self) -> Tuple[List[ChapterInfo], Dict[str, Any]]:
        """
        Analyze the story structure to determine all chapters/stories that need to be written.
        Handles both novels (multiple chapters) and short stories (single story).
        
        Returns:
            Tuple of (chapter_info_list, story_parameters)
        """
        self.logger.info("Analyzing story structure...")
        
        # 1. Load story parameters
        parameters_file = self.dir_manager.get_parameters_path()
        story_params = self._load_story_parameters(parameters_file)
        
        structure_name = story_params.get("Story Structure", "6-Act Structure")
        story_length = story_params.get("Story Length", "Novel (Standard)")
        self.logger.info(
            "Chapter analysis parameters: file=%s, structure=%s, length=%s",
            parameters_file,
            structure_name,
            story_length,
        )
        
        # 2. Handle short stories differently
        if story_length == "Short Story":
            return self._analyze_short_story_structure(structure_name, story_params)
        
        # 3. Handle novels - get structure sections
        sections = STRUCTURE_SECTIONS_MAP.get(structure_name, [])
        if not sections:
            raise ValueError(f"Unknown story structure: {structure_name}")
            
        # 4. Analyze each section to find chapters
        chapter_info_list = []
        # 章号以章节大纲写明的为准，与 scene_pipeline 用同一套解析规则；
        # 两边必须一致，否则写作会去找一个编号不同的场景规划文件。
        next_expected_chapter = 1
        claimed_chapters = set()

        for section_name in sections:
            section_chapters = self._analyze_section_chapters(
                structure_name, section_name, next_expected_chapter, claimed_chapters
            )
            chapter_info_list.extend(section_chapters)
            for info in section_chapters:
                claimed_chapters.add(info.chapter_number)
                next_expected_chapter = max(next_expected_chapter, info.chapter_number + 1)
            
        self.logger.info(f"Found {len(chapter_info_list)} total chapters across {len(sections)} sections")
        return chapter_info_list, story_params
        
    def _load_story_parameters(self, parameters_file: str) -> Dict[str, str]:
        """Load story parameters from file."""
        params = {}
        try:
            if os.path.exists(parameters_file):
                with open(parameters_file, "r", encoding="utf-8") as f:
                    for line in f:
                        if ":" in line:
                            key, value = line.split(":", 1)
                            params[key.strip()] = value.strip()
        except Exception as e:
            self.logger.error(f"Error loading parameters: {e}")
            
        return params

    @staticmethod
    def _is_valid_generated_output(output_path: str) -> bool:
        """Return whether an existing prose file contains usable generated text."""
        if not os.path.isfile(output_path):
            return False

        try:
            with open(output_path, "r", encoding="utf-8") as output_file:
                content = output_file.read()
        except (OSError, UnicodeError):
            return False

        if not content.strip():
            return False

        error_markers = (
            "[[[ERROR GENERATING",
            "[[[大模型未返回",
            "[Error: Could not import NovelWriter AI functions",
        )
        return not any(marker in content for marker in error_markers)

    def _accepted_chapter_numbers(self) -> Optional[set]:
        """Return the ledger's completion set, or None for a legacy project.

        A prose file is only a draft until acceptance commits it.  Treating a
        rejected file as completed skips the blocked chapter and makes the next
        chapter report a predecessor error instead of surfacing the decision.
        """
        if hasattr(self, "_accepted_chapters_cache"):
            return self._accepted_chapters_cache
        ledger_path = StoryLedgerManager(self.output_dir).suspense_ledger_path
        if not os.path.isfile(ledger_path):
            self._accepted_chapters_cache = None
            return None
        try:
            ledger = read_json(ledger_path)
            accepted = ledger.get("accepted_chapters", [])
            if not isinstance(accepted, list):
                self._accepted_chapters_cache = None
            else:
                pending_chapters = {
                    int(item.get("chapter"))
                    for item in ledger.get("pending_regenerations", [])
                    if isinstance(item, dict) and item.get("chapter") is not None
                }
                self._accepted_chapters_cache = {
                    int(item.get("chapter"))
                    for item in accepted
                    if (
                        isinstance(item, dict)
                        and item.get("chapter") is not None
                        and item.get("content_hash")
                        and int(item.get("chapter")) not in pending_chapters
                    )
                }
        except (OSError, TypeError, ValueError):
            self._accepted_chapters_cache = None
        return self._accepted_chapters_cache

    def _is_completed_chapter(self, output_path: str, chapter_number: int) -> bool:
        """A usable file counts as complete only when the ledger accepted it."""
        if not self._is_valid_generated_output(output_path):
            return False
        accepted = self._accepted_chapter_numbers()
        return True if accepted is None else int(chapter_number) in accepted
        
    def _analyze_short_story_structure(self, structure_name: str, story_params: Dict[str, str]) -> Tuple[List[ChapterInfo], Dict[str, Any]]:
        """Analyze short story structure - returns single 'chapter' representing the whole story."""
        safe_struct = structure_name.lower().replace(' ', '_').replace(':', '').replace('/', '_').replace('(', '').replace(')', '').replace('!', '').replace(',', '')
        
        # For short stories, there's one scene plan file and one output file
        scene_plan_filename = f"scenes_short_story_{safe_struct}.md"
        if self.use_new_structure:
            scene_plan_path = os.path.join(
                self.dir_manager.get_full_path("planning_dir"),
                scene_plan_filename,
            )
            scene_plan_file = os.path.relpath(scene_plan_path, self.output_dir)
        else:
            scene_plan_file = scene_plan_filename
            scene_plan_path = os.path.join(self.output_dir, scene_plan_file)
        
        # Output file uses the story title
        novel_title = story_params.get("Novel Title", "未命名短篇小说")
        safe_title = novel_title.lower().replace(' ', '_').replace(':', '').replace('/', '')
        output_filename = f"prose_short_story_{safe_title}.md"
        if self.use_new_structure:
            output_path = os.path.join(
                self.dir_manager.get_full_path("content_dir"),
                output_filename,
            )
            output_file = os.path.relpath(output_path, self.output_dir)
        else:
            output_file = output_filename
            output_path = os.path.join(self.output_dir, output_file)
        
        # Create a single "chapter" info representing the whole short story
        story_info = ChapterInfo(
            chapter_number=1,  # Short stories are treated as "chapter 1"
            section_name="Complete Short Story",
            scene_plan_file=scene_plan_file,
            output_file=output_file,
            exists=self._is_completed_chapter(output_path, 1),
            plan_exists=os.path.isfile(scene_plan_path),
        )
        
        self.logger.info(f"Short story analysis: Scene plan = {scene_plan_file}, Output = {output_file}, Exists = {story_info.exists}")
        return [story_info], story_params
        
    def _analyze_section_chapters(
        self,
        structure_name: str,
        section_name: str,
        start_chapter: int,
        claimed_chapters: Optional[set] = None,
    ) -> List[ChapterInfo]:
        """Analyze a specific section to find its chapters."""
        safe_struct = structure_name.lower().replace(' ', '_').replace(':', '').replace('/', '_').replace('(', '').replace(')', '').replace('!', '').replace(',', '')
        safe_section = section_name.lower().replace(' ', '_').replace(':', '').replace('/', '_').replace('(', '').replace(')', '')
        
        # Find the chapter outline file for this section
        outline_file = f"chapter_outlines_{safe_struct}_{safe_section}.md"
        outline_path = os.path.join(
            self.dir_manager.get_chapter_outlines_path(), outline_file
        )
        # Read legacy flat outlines when resuming an older project.
        if not os.path.exists(outline_path):
            legacy_outline_path = os.path.join(self.output_dir, outline_file)
            if os.path.exists(legacy_outline_path):
                outline_path = legacy_outline_path
        
        chapters = []
        try:
            if os.path.exists(outline_path):
                content = open_file(outline_path)
                chapter_numbers, numbering_warning = resolve_section_chapter_numbers(
                    parse_chapter_numbers(content),
                    start_chapter,
                    claimed_chapters or (),
                )
                if numbering_warning:
                    self.logger.warning(
                        "Section '%s' chapter numbering: %s", section_name, numbering_warning
                    )

                for chapter_num in chapter_numbers:
                    
                    # Determine scene plan file using directory manager
                    scene_plans_dir = self.dir_manager.get_scene_plans_dir()
                    scene_plan_file = f"{scene_plans_dir}/scenes_{safe_struct}_{safe_section}_ch{chapter_num}.md"
                    scene_plan_path = os.path.join(self.output_dir, scene_plan_file)
                    if not os.path.exists(scene_plan_path):
                        legacy_scene_path = os.path.join(
                            self.output_dir,
                            "detailed_scene_plans",
                            os.path.basename(scene_plan_file),
                        )
                        if os.path.exists(legacy_scene_path):
                            scene_plan_path = legacy_scene_path
                            scene_plan_file = os.path.relpath(legacy_scene_path, self.output_dir)
                    
                    # Determine output file using directory manager
                    chapters_dir = self.dir_manager.get_chapters_dir()
                    output_file = f"{chapters_dir}/chapter_{chapter_num}.md"
                    output_path = os.path.join(self.output_dir, output_file)
                    
                    chapter_info = ChapterInfo(
                        chapter_number=chapter_num,
                        section_name=section_name,
                        scene_plan_file=scene_plan_file,
                        output_file=output_file,
                        exists=self._is_completed_chapter(output_path, chapter_num),
                        plan_exists=os.path.isfile(scene_plan_path),
                    )
                    chapters.append(chapter_info)
                    
        except Exception as e:
            self.logger.error(f"Error analyzing section {section_name}: {e}")
            
        return chapters
        
    def _acceptance_conflict_result(
        self, chapter_number: int, error: "ChapterAcceptanceError"
    ) -> AgentResult:
        """Report a blocked acceptance in the words the author needs to act on."""
        briefing = build_briefing(chapter_number, error.report.blocking_issues)
        ledger = StoryLedgerManager(self.output_dir)
        message = describe_briefing(
            briefing,
            conflict_record_path(ledger, chapter_number),
            error.adjudication,
        )
        self.logger.error("Chapter %s acceptance blocked: %s", chapter_number, message)
        return AgentResult(
            success=False,
            data={
                "chapter_number": chapter_number,
                "needs_human_decision": needs_author_decision(
                    briefing, error.adjudication
                ),
                "adjudication": error.adjudication,
                # Keep the original report so the GUI batch callback can open
                # the same decision dialog as the manual-writing path.
                "conflict_issues": [
                    issue.to_dict() for issue in error.report.blocking_issues
                ],
                "conflicts": [
                    {
                        "id": choice.record_id,
                        "field": choice.field_name,
                        "existing": choice.existing,
                        "proposed": choice.proposed,
                        "reason": choice.reason,
                    }
                    for choice in briefing.choices
                ],
            },
            messages=[message],
            metrics={},
        )

    def create_writing_plan(self, chapter_info_list: List[ChapterInfo], batch_size: int = 1,
                          quality_thresholds: Optional[QualityThresholds] = None) -> ChapterWritingPlan:
        """Create a plan for writing chapters."""
        chapters_to_write = [info.chapter_number for info in chapter_info_list if not info.exists]
        chapters_completed = [info.chapter_number for info in chapter_info_list if info.exists]
        
        return ChapterWritingPlan(
            total_chapters=len(chapter_info_list),
            chapters_to_write=chapters_to_write,
            chapters_completed=chapters_completed,
            batch_size=batch_size,
            quality_check=True,
            enable_reviews=True,
            quality_thresholds=quality_thresholds or self.quality_thresholds
        )
        
    def write_chapters_batch(self, chapter_info_list: List[ChapterInfo], plan: ChapterWritingPlan) -> AgentResult:
        """Write a batch of chapters automatically."""
        if not plan.chapters_to_write:
            return AgentResult(
                success=True,
                data={"completed_chapters": plan.chapters_completed},
                messages=["所有章节均已写完"],
                metrics={}
            )

        if self.require_planning_contract:
            try:
                validate_contract_sequence(
                    load_planning_contracts(self.output_dir),
                    total_chapters=plan.total_chapters,
                )
            except PlanningContractError as exc:
                targets = "、".join(str(number) for number in exc.chapters)
                hint = (
                    f"请回到“场景与章节规划”重新规划第 {targets} 章后再写作。"
                    if targets
                    else "请回到“场景与章节规划”重新规划后再写作。"
                )
                return AgentResult(
                    success=False,
                    data={"chapters_written": [], "blocking_chapters": list(exc.chapters)},
                    messages=[f"场景规划契约前置检查未通过：{exc}。{hint}"],
                    metrics={},
                )
            
        # Write chapters in batches with review collection
        chapters_written = []
        errors = []
        all_chapter_reviews = []  # Collect chapter reviews for batch analysis
        batch_number = 1
        cancellation_message = ""
        blocked_chapter: Optional[int] = None
        blocked_reason = ""
        blocked_data: Dict[str, Any] = {}
        
        for i in range(0, len(plan.chapters_to_write), plan.batch_size):
            batch = plan.chapters_to_write[i:i + plan.batch_size]
            batch_chapter_reviews = []  # Reviews for this specific batch
            
            self.logger.info(f"Starting batch {batch_number} with {len(batch)} chapters")
            
            for chapter_num in batch:
                try:
                    # 章节边界是取消的安全点：上一章已经落盘并提交，下一章尚未开始。
                    raise_if_cancelled(self.cancel_token)
                    chapter_info = next(ch for ch in chapter_info_list if ch.chapter_number == chapter_num)
                    result = self._write_single_chapter(chapter_info)
                    
                    if result.success:
                        chapters_written.append(chapter_num)
                        self.logger.info(f"Successfully wrote Chapter {chapter_num}")
                        
                        # Extract chapter review if available
                        if "chapter_review" in result.data and result.data["chapter_review"]:
                            # Load the full chapter review from file for batch analysis
                            chapter_review = self._load_chapter_review(chapter_num)
                            if chapter_review:
                                batch_chapter_reviews.append(chapter_review)
                                all_chapter_reviews.append(chapter_review)
                    else:
                        error_msg = result.messages[0] if result.messages else "未知错误"
                        errors.append(f"第 {chapter_num} 章：{error_msg}")
                        blocked_chapter, blocked_reason = chapter_num, error_msg
                        blocked_data = dict(result.data or {})
                        self.logger.error(
                            "Chapter %s blocked the run: %s", chapter_num, error_msg
                        )
                        # 小说章节具有顺序依赖；上一章未验收时继续写后文只会
                        # 把错误扩散到更多章节。
                        break

                except GenerationCancelled as cancelled:
                    # 已写完的章节保留，未开始的章节直接放弃。
                    cancellation_message = str(cancelled)
                    self.logger.info("Chapter batch cancelled before Chapter %s", chapter_num)
                    break
                except Exception as e:
                    error_msg = f"第 {chapter_num} 章：{str(e)}"
                    errors.append(error_msg)
                    blocked_chapter, blocked_reason = chapter_num, str(e)
                    self.logger.error(
                        f"Error writing Chapter {chapter_num}: {e}", exc_info=True
                    )
                    break
            
            # Perform batch-level review if we have chapter reviews
            if batch_chapter_reviews and self.review_agent and plan.enable_reviews:
                batch_review = self._review_batch(batch_chapter_reviews, batch_number)
                if batch_review:
                    self.logger.info(f"Batch {batch_number} review completed: "
                                   f"Quality {batch_review.average_quality:.2f}, "
                                   f"Consistency {batch_review.consistency_score:.2f}")
            
            if cancellation_message:
                break
            batch_number += 1
            if errors:
                break
                    
        # Prepare result with review metrics
        success = (
            not cancellation_message
            and not errors
            and len(chapters_written) == len(plan.chapters_to_write)
        )
        message = f"已写完 {len(chapters_written)} 章"
        if cancellation_message:
            message += "（已按请求停止）"
        if errors:
            # 原来只报「发生 N 个错误」，真正的原因留在 data 里，界面上看不到，
            # 得翻日志才知道停在哪一章、为什么停。这里把章号和原因写进消息本身。
            message += (
                f"，停在第 {blocked_chapter} 章：{blocked_reason}。"
                f"已写完的章节都已保留，重新点击写作会从第 {blocked_chapter} 章继续。"
            )

        # Add review summary to message
        if all_chapter_reviews:
            avg_quality = sum(review.overall_quality for review in all_chapter_reviews) / len(all_chapter_reviews)
            message += f"（平均质量：{avg_quality:.2f}）"
            
        # Prepare result data with review metrics
        result_data = {
            "chapters_written": chapters_written,
            "errors": errors,
            "blocked_chapter": blocked_chapter,
            "total_completed": len(plan.chapters_completed) + len(chapters_written),
            "reviews_generated": len(all_chapter_reviews)
        }
        # Preserve structured acceptance details through the outer batch result;
        # the GUI callback runs on the main thread and needs these to draw the
        # actual decision buttons.
        for key in (
            "chapter_number",
            "needs_human_decision",
            "adjudication",
            "conflict_issues",
            "conflicts",
        ):
            if key in blocked_data:
                result_data[key] = blocked_data[key]
        
        # Add review summary if available
        if all_chapter_reviews:
            result_data["review_summary"] = {
                "total_reviews": len(all_chapter_reviews),
                "average_quality": sum(review.overall_quality for review in all_chapter_reviews) / len(all_chapter_reviews),
                "average_coherence": sum(review.coherence_score for review in all_chapter_reviews) / len(all_chapter_reviews),
                "average_pacing": sum(review.pacing_score for review in all_chapter_reviews) / len(all_chapter_reviews),
                "total_issues": sum(len(review.issues) for review in all_chapter_reviews),
                "total_suggestions": sum(len(review.suggestions) for review in all_chapter_reviews)
            }
            
        return AgentResult(
            success=success,
            data=result_data,
            messages=[message],
            metrics={
                "chapters_written": len(chapters_written), 
                "errors": len(errors),
                "reviews_generated": len(all_chapter_reviews)
            }
        )
        
    def _write_single_chapter(self, chapter_info: ChapterInfo) -> AgentResult:
        """Write a single chapter or short story using the existing writing logic."""
        try:
            # Check if scene plan exists
            scene_plan_path = os.path.join(self.output_dir, chapter_info.scene_plan_file)
            if not os.path.exists(scene_plan_path):
                return AgentResult(
                    success=False,
                    data={},
                    messages=[f"找不到场景规划：{chapter_info.scene_plan_file}"],
                    metrics={}
                )
                
            # Load required context
            context = self._load_writing_context()
            
            # Determine if this is a short story
            is_short_story = chapter_info.section_name == "Complete Short Story"
            
            # Load scene plan
            scenes_content = open_file(scene_plan_path)
            if not scenes_content.strip():
                return AgentResult(
                    success=False,
                    data={},
                    messages=[f"场景规划为空：{chapter_info.scene_plan_file}"],
                    metrics={}
                )

            # Legacy domain prompts demonstrated C001/E001 in every chapter.
            # Repair a colliding contract before spending calls on prose; the
            # creative scene Markdown remains unchanged.
            manager = StoryLedgerManager(self.output_dir)
            contract = manager.load_contract(chapter_info.chapter_number, scenes_content)
            if contract is not None:
                try:
                    profile = manager.locked_profile()
                except (FileNotFoundError, OSError, ValueError):
                    profile = None
                profile = profile or resolve_domain_profile(context.get("parameters", {}))
                if has_domain_id_collision(
                    contract,
                    self.output_dir,
                    chapter_info.chapter_number,
                    profile=profile,
                ):
                    resolution = resolve_domain_identities(
                        contract,
                        self.output_dir,
                        chapter_info.chapter_number,
                        self._get_selected_model(),
                        send_prompt,
                        profile=profile,
                    )
                    for warning in resolution.warnings:
                        self.logger.warning(
                            "Chapter %s semantic contract warning: %s",
                            chapter_info.chapter_number,
                            warning,
                        )
                    if resolution.contract != contract:
                        manager.save_contract(
                            chapter_info.chapter_number,
                            resolution.contract,
                            scenes_content,
                        )
                        self.logger.info(
                            "Normalised legacy domain IDs in Chapter %s before prose generation",
                            chapter_info.chapter_number,
                        )
                
            # Parse scenes
            scenes = self._parse_scenes(scenes_content)
            if not scenes:
                return AgentResult(
                    success=False,
                    data={},
                    messages=[f"规划中没有找到场景：{chapter_info.scene_plan_file}"],
                    metrics={}
                )
                
            # 所有题材（含短篇）都走同一条设计-生成-审阅闭环；具体的评分维度、
            # 契约字段和门槛由领域档案决定。未通过闸门的正文不会落到稿件目录。
            scene_reviews = []
            quality_loop = ChapterGenerationLoop(
                output_dir=self.output_dir,
                model=self._get_selected_model(),
                logger=self.logger,
                cancel_token=self.cancel_token,
                require_planning_contract=self.require_planning_contract,
            )

            def generate_scene(**kwargs):
                return self._generate_scene_prose(
                    chapter_info.chapter_number,
                    kwargs["scene_number"],
                    kwargs["scene_plan"],
                    context,
                    is_short_story=is_short_story,
                    previous_scene_tail=kwargs.get("previous_scene_tail", ""),
                    next_scene_plan=kwargs.get("next_scene_plan", ""),
                    chapter_contract=kwargs.get("contract"),
                    profile=kwargs.get("profile"),
                )

            def save_revised_plan(revised_plan: str) -> None:
                archive_dir = os.path.join(
                    self.output_dir,
                    "archive",
                    "quality_loop",
                    "scene_plans",
                )
                os.makedirs(archive_dir, exist_ok=True)
                base_name = os.path.splitext(os.path.basename(scene_plan_path))[0]
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                archive_path = os.path.join(
                    archive_dir,
                    f"{base_name}_before_{timestamp}.md",
                )
                write_file(archive_path, scenes_content)
                write_file(scene_plan_path, revised_plan)
                self.logger.info("Quality loop revised scene plan; original archived at %s", archive_path)

            try:
                domain_loop_result = quality_loop.run(
                    chapter_number=chapter_info.chapter_number,
                    plan_content=scenes_content,
                    parameters=context.get("parameters", {}),
                    lore=context.get("lore", ""),
                    generate_scene=generate_scene,
                    on_plan_revised=save_revised_plan,
                )
            except QualityGateError as gate_error:
                archived = archive_failed_generation(
                    self.output_dir,
                    chapter_info.chapter_number,
                    gate_error.partial_scenes,
                    label="short_story" if is_short_story else "chapter",
                )
                self.logger.error(
                    "Chapter %s failed the quality gate: %s%s",
                    chapter_info.chapter_number,
                    gate_error,
                    f" Partial prose archived at {archived}." if archived else "",
                )
                message = str(gate_error)
                if archived:
                    message += f"（部分正文已归档：{archived}）"
                return AgentResult(
                    success=False,
                    data={"archived_partial_prose": archived},
                    messages=[message],
                    metrics={},
                )

            for waiver in domain_loop_result.gate_waivers:
                self.logger.warning(
                    "Chapter %s passed on a waiver: %s",
                    chapter_info.chapter_number,
                    waiver,
                )

            generated_scenes = domain_loop_result.scenes
            scenes = self._parse_scenes(domain_loop_result.plan_content)

            # Keep the existing generic review records as a secondary report,
            # but run them only on text accepted by the domain loop.
            if self.review_agent:
                for i, prose in enumerate(generated_scenes, 1):
                    scene_review = self._review_scene(prose, i, chapter_info.chapter_number)
                    if scene_review:
                        scene_reviews.append(scene_review)
                
            # Combine scenes into final content
            final_content = "\n\n---\n\n".join(generated_scenes)
            
            # Save to appropriate location
            output_path = os.path.join(self.output_dir, chapter_info.output_file)
            
            # For chapters, create subdirectory; for short stories, save directly
            output_directory = os.path.dirname(output_path)
            if output_directory:
                os.makedirs(output_directory, exist_ok=True)
            
            try:
                acceptance_result = publish_chapter_with_acceptance(
                    self.output_dir,
                    chapter_info.chapter_number,
                    output_path,
                    final_content,
                    lambda saved_path: quality_loop.accept_result(
                        chapter_info.chapter_number,
                        domain_loop_result,
                        chapter_path=saved_path,
                    ),
                )
            except ChapterAcceptanceError as acceptance_error:
                # Batch writing has nobody to ask, and one open conflict blocks
                # every later chapter too — so carry the decision the author
                # will have to make into the result rather than a bare repr.
                return self._acceptance_conflict_result(
                    chapter_info.chapter_number, acceptance_error
                )
            final_content = domain_loop_result.chapter_content
            
            # Perform chapter-level review if enabled
            chapter_review = None
            if self.review_agent:
                chapter_review = self._review_chapter(
                    chapter_content=final_content,
                    scene_reviews=scene_reviews,
                    chapter_number=chapter_info.chapter_number,
                    section_name=chapter_info.section_name,
                )
            
            # Prepare result data
            result_data = {
                "output_file": chapter_info.output_file, 
                "scenes_count": len(scenes), 
                "is_short_story": is_short_story,
                "scene_reviews_count": len(scene_reviews)
            }
            if acceptance_result:
                result_data["story_revision"] = acceptance_result.committed_revision
                result_data["chapter_delta_file"] = acceptance_result.delta_path

            if domain_loop_result:
                result_data["legal_suspense_review"] = {
                    "enabled": True,
                    "retry_count": domain_loop_result.retry_count,
                    "plan_revised": domain_loop_result.plan_revised,
                    "chapter_review": domain_loop_result.chapter_review.to_dict(),
                }
            
            # Add review data if available
            if chapter_review:
                result_data["chapter_review"] = {
                    "quality_score": chapter_review.overall_quality,
                    "coherence_score": chapter_review.coherence_score,
                    "pacing_score": chapter_review.pacing_score,
                    "issues_count": len(chapter_review.issues),
                    "suggestions_count": len(chapter_review.suggestions)
                }
            
            # Return appropriate success message
            if is_short_story:
                success_msg = "短篇小说已成功写完"
            else:
                success_msg = f"第 {chapter_info.chapter_number} 章已成功写完"
                if chapter_review:
                    success_msg += f"（质量：{chapter_review.overall_quality:.2f}）"
            
            return AgentResult(
                success=True,
                data=result_data,
                messages=[success_msg],
                metrics={"scenes_count": len(scenes), "scene_reviews": len(scene_reviews)}
            )
            
        except GenerationCancelled:
            # 取消不是章节级失败，交给批量循环终止整轮。
            raise
        except Exception as e:
            return AgentResult(
                success=False,
                data={},
                messages=[f"撰写章节时出错：{str(e)}"],
                metrics={}
            )
            
    def _load_writing_context(self) -> Dict[str, Any]:
        """Load context needed for chapter writing."""
        context = {}
        
        try:
            # Load story parameters
            params_file = self.dir_manager.get_parameters_path()
            context["parameters"] = self._load_story_parameters(params_file)
            
            # Load lore
            lore_file = os.path.join(
                self.output_dir, "story", "lore", "generated_lore.md"
            )
            if not os.path.exists(lore_file):
                lore_file = os.path.join(self.output_dir, "generated_lore.md")
            if os.path.exists(lore_file):
                context["lore"] = self._sanitize_lore_content(open_file(lore_file))
            else:
                context["lore"] = "Lore not available."
                
            # Load character roster
            context["characters"] = self._load_character_roster()
            
            # Load faction summary
            context["factions"] = self._load_faction_summary()
            
        except Exception as e:
            self.logger.error(f"Error loading writing context: {e}")
            
        return context

    @staticmethod
    def _sanitize_lore_content(content: str) -> str:
        """Backward-compatible wrapper for the shared lore sanitizer."""
        return sanitize_lore_content(content)
        
    def _load_character_roster(self) -> str:
        """Load character roster summary."""
        try:
            characters_file = os.path.join(
                self.output_dir, "story", "lore", "characters.json"
            )
            if not os.path.exists(characters_file):
                characters_file = os.path.join(self.output_dir, "characters.json")
            if os.path.exists(characters_file):
                characters_data = read_json(characters_file)
                if characters_data and "characters" in characters_data:
                    summaries = []
                    for char in characters_data["characters"]:
                        details = [
                            f"\n\n姓名：{char.get('name', '无')}",
                            f"角色：{char.get('role', '无')}",
                            f"性别：{char.get('gender', '无')}",
                            f"年龄：{char.get('age', '无')}",
                            f"外貌：{char.get('appearance_summary', '无')}"
                        ]
                        
                        goals = char.get('goals', [])
                        if goals:
                            details.append(f"主要目标：{goals[0]}")
                            
                        strengths = char.get('strengths', [])
                        if strengths:
                            details.append(f"主要优点：{strengths[0]}")
                            
                        flaws = char.get('flaws', [])
                        if flaws:
                            details.append(f"主要缺点：{flaws[0]}")
                            
                        backstory = char.get('backstory_summary', '')
                        if backstory:
                            details.append(f"背景故事：{backstory}")
                            
                        summaries.append("\n".join(details))
                        
                    return "主要人物：\n" + "\n".join(summaries)
        except Exception as e:
            self.logger.error(f"Error loading character roster: {e}")
            
        return "没有可用的人物名单。"
        
    def _load_faction_summary(self) -> str:
        """Load faction summary."""
        try:
            factions_file = os.path.join(
                self.output_dir, "story", "lore", "factions.json"
            )
            if not os.path.exists(factions_file):
                factions_file = os.path.join(self.output_dir, "factions.json")
            if os.path.exists(factions_file):
                factions_data = read_json(factions_file)
                if factions_data:
                    return format_faction_summary(factions_data)
        except Exception as e:
            self.logger.error(f"Error loading faction summary: {e}")
            
        return "没有可用的势力信息。"
        
    def _parse_scenes(self, scenes_content: str) -> List[str]:
        """Parse individual scenes from scene plan content."""
        return parse_scene_sections(scenes_content)
        
    def _get_selected_model(self) -> str:
        """Resolve the live GUI model or the orchestrator-provided model."""
        from core.generation.ai_helper import DEFAULT_API_MODEL

        if self.app and hasattr(self.app, "get_selected_model"):
            return self.app.get_selected_model()
        return self.model or DEFAULT_API_MODEL

    def _generate_scene_prose(
        self,
        chapter_num: int,
        scene_num: int,
        scene_plan: str,
        context: Dict[str, Any],
        is_short_story: bool = False,
        previous_scene_tail: str = "",
        next_scene_plan: str = "",
        chapter_contract: Optional[Dict[str, Any]] = None,
        profile: Optional[DomainProfile] = None,
    ) -> str:
        """Generate prose for a single scene using genuine NovelWriter AI functions."""

        story_params = context.get("parameters", {})

        conflicts = find_scene_world_conflicts(
            scene_plan,
            context.get("lore", ""),
            story_params,
        )
        if conflicts:
            raise ValueError(
                "场景规划包含世界观未定义的科幻设定："
                + "、".join(conflicts)
                + "。请重新生成场景规划。"
            )

        prompt = build_scene_prompt(
            scene_plan=scene_plan,
            scene_number=scene_num,
            parameters=story_params,
            lore=context.get("lore", ""),
            character_roster=context.get("characters", ""),
            faction_summary=context.get("factions", ""),
            profile=profile or resolve_domain_profile(story_params),
            chapter_number=None if is_short_story else chapter_num,
            structure_name=story_params.get("Story Structure", ""),
            novel_title=story_params.get("Novel Title", "") if is_short_story else "",
            contract=chapter_contract,
            previous_scene_tail=previous_scene_tail,
            next_scene_plan=next_scene_plan,
        )

        # Use genuine NovelWriter AI helper functions
        try:
            from core.generation.ai_helper import DEFAULT_API_MODEL, send_prompt
            from core.generation.helper_fns import save_prompt_to_file

            # Prefer the live GUI selection. Agentic/non-GUI callers pass the
            # orchestrator's model into this agent, so they retain the selected
            # provider and its matching credentials (for hosted-llm, the
            # HOSTED_LLM_* variables loaded by ai_helper).
            model = self._get_selected_model()
                
            # Save prompt to file (following existing pattern)
            prompt_filename = scene_prompt_filename(
                scene_num, None if is_short_story else chapter_num
            )

            try:
                save_prompt_to_file(self.output_dir, prompt_filename, prompt)
            except Exception as e:
                self.logger.warning(f"Could not save prompt to file: {e}")
            
            # Call the genuine NovelWriter AI helper
            current_backend = get_backend()
            backend_info = f"{current_backend}" if current_backend != "api" else f"api/{model}"
            
            if is_short_story:
                self.logger.info(f"Generating prose for Scene {scene_num} of short story using LLM ({backend_info})")
            else:
                self.logger.info(f"Generating prose for Chapter {chapter_num}, Scene {scene_num} using LLM ({backend_info})")
                
            label = (
                f"短篇第 {scene_num} 个场景"
                if is_short_story
                else f"第 {chapter_num} 章第 {scene_num} 个场景"
            )
            response = generate_prose_with_style_retry(
                lambda text: send_prompt(text, model=model),
                prompt,
                logger=self.logger,
                label=label,
            )
            
            if is_short_story:
                self.logger.info(f"Generated prose for Scene {scene_num} of short story. Length: {len(response)} chars")
            else:
                self.logger.info(f"Generated prose for Chapter {chapter_num}, Scene {scene_num}. Length: {len(response)} chars")
                
            return response
            
        except ImportError as e:
            error_msg = f"Could not import NovelWriter AI functions: {e}"
            self.logger.error(error_msg)
            raise RuntimeError(error_msg) from e
        except Exception as e:
            error_msg = f"Error calling LLM for scene generation: {e}"
            self.logger.error(error_msg)
            raise RuntimeError(error_msg) from e
            
    def get_progress_report(self, chapter_info_list: List[ChapterInfo]) -> Dict[str, Any]:
        """Get a progress report on chapter writing status."""
        total = len(chapter_info_list)
        completed = sum(1 for ch in chapter_info_list if ch.exists)
        remaining = total - completed
        incomplete = [chapter for chapter in chapter_info_list if not chapter.exists]
        missing_scene_plans = [
            chapter.chapter_number for chapter in incomplete if not chapter.plan_exists
        ]
        next_chapter = min(
            (chapter.chapter_number for chapter in incomplete),
            default=None,
        )
        next_info = next(
            (chapter for chapter in incomplete if chapter.chapter_number == next_chapter),
            None,
        )
        
        # Group by section
        section_progress = {}
        for chapter in chapter_info_list:
            section = chapter.section_name
            if section not in section_progress:
                section_progress[section] = {"total": 0, "completed": 0}
            section_progress[section]["total"] += 1
            if chapter.exists:
                section_progress[section]["completed"] += 1
                
        return {
            "total_chapters": total,
            "completed_chapters": completed,
            "remaining_chapters": remaining,
            "missing_scene_plans": missing_scene_plans,
            "completion_percentage": (completed / total * 100) if total > 0 else 0,
            "section_progress": section_progress,
            "next_chapter": next_chapter,
            "next_chapter_ready": bool(next_info and next_info.plan_exists),
        }
    
    # ========== REVIEW SYSTEM METHODS ==========
    
    def _review_scene(self, scene_content: str, scene_number: int, chapter_number: int, retry_attempt: int = 0) -> SceneReview:
        """Perform scene-level review using local analysis."""
        try:
            # Default values
            quality_score = 0.7  # Default quality score
            word_count = len(scene_content.split())
            issues = []
            strengths = []
            suggestions = []
            confidence = 0.8
            
            # Use review agent if available
            if self.review_agent:
                try:
                    review_result = self.review_agent.review_step_output(
                        "scene",
                        scene_content,
                        context={
                            "chapter_number": chapter_number,
                            "scene_number": scene_number,
                        },
                    )
                    if review_result:
                        quality_score = review_result.quality_score
                        issues = review_result.issues_found
                        strengths = review_result.strengths_found
                        suggestions = review_result.improvement_suggestions
                        confidence = review_result.confidence
                except Exception as e:
                    self.logger.warning(f"Error using review agent: {e}")
            
            # Fallback to basic analysis if no review agent or it failed
            if not issues and not strengths:
                # Basic analysis - word count based quality
                if word_count < 300:
                    issues.append("Scene is too short")
                    quality_score = 0.5
                elif word_count > 2000:
                    issues.append("Scene may be too long")
                    quality_score = 0.6
                    
                # Add generic strengths/suggestions
                strengths.append("Scene completed successfully")
                suggestions.append("Consider reviewing for pacing and character development")
            
            # Phase 3: Record quality trend data
            self.record_quality_trend(
                chapter_number=chapter_number,
                scene_number=scene_number,
                quality_score=quality_score,
                review_type="scene",
                retry_attempt=retry_attempt
            )
            
            # Phase 3: Check if retry is recommended based on thresholds
            should_retry = self.should_retry_based_on_thresholds(
                quality_score=quality_score,
                review_type="scene",
                retry_attempt=retry_attempt
            )
            
            if should_retry:
                suggestions.insert(0, f"Quality score {quality_score:.2f} below threshold "
                                  f"{self.quality_thresholds.minimum_scene_quality:.2f}. Retry recommended.")
            
            # Create scene review
            scene_review = SceneReview(
                scene_number=scene_number,
                chapter_number=chapter_number,
                quality_score=quality_score,
                word_count=word_count,
                issues=issues,
                strengths=strengths,
                suggestions=suggestions,
                timestamp=datetime.now().isoformat(),
                confidence=confidence
            )
            
            return scene_review
            
        except Exception as e:
            self.logger.error(f"Error reviewing scene: {e}")
            # Return default review on error
            return SceneReview(
                scene_number=scene_number,
                chapter_number=chapter_number,
                quality_score=0.5,
                word_count=len(scene_content.split()),
                issues=[f"Error during review: {str(e)}"],
                strengths=[],
                suggestions=["Retry scene review"],
                timestamp=datetime.now().isoformat(),
                confidence=0.3
            )
    
    def _review_chapter(self, chapter_content: str, scene_reviews: List[SceneReview], 
                      chapter_number: int, section_name: str, retry_attempt: int = 0) -> ChapterReview:
        """Perform chapter-level review using local analysis."""
        try:
            # Calculate overall quality from scene reviews
            scene_quality_scores = [scene.quality_score for scene in scene_reviews]
            overall_quality = sum(scene_quality_scores) / len(scene_quality_scores) if scene_quality_scores else 0.7
            
            # Calculate total word count
            total_word_count = sum(scene.word_count for scene in scene_reviews)
            
            # Analyze chapter coherence
            coherence_score = self._analyze_chapter_coherence(scene_reviews)
            
            # Analyze chapter pacing
            pacing_score = self._analyze_chapter_pacing(chapter_content, scene_reviews)
            
            # Analyze character development
            character_development_score = self._analyze_character_development(chapter_content)
            
            # Aggregate issues and suggestions
            all_issues = []
            all_strengths = []
            all_suggestions = []
            
            # Add scene-level issues/strengths/suggestions
            for scene in scene_reviews:
                for issue in scene.issues:
                    if issue not in all_issues:
                        all_issues.append(issue)
                        
                for strength in scene.strengths:
                    if strength not in all_strengths:
                        all_strengths.append(strength)
                        
                for suggestion in scene.suggestions:
                    if suggestion not in all_suggestions:
                        all_suggestions.append(suggestion)
            
            # Add chapter-level issues
            if coherence_score < self.quality_thresholds.coherence_threshold:
                all_issues.append("Low scene-to-scene coherence")
                all_suggestions.append("Improve transitions between scenes")
                
            if pacing_score < self.quality_thresholds.pacing_threshold:
                all_issues.append("Uneven pacing across scenes")
                all_suggestions.append("Balance scene lengths and tension")
                
            if character_development_score < self.quality_thresholds.character_development_threshold:
                all_issues.append("Limited character development")
                all_suggestions.append("Add more character moments and growth")
            
            # Calculate confidence
            confidence = min(0.9, sum(scene.confidence for scene in scene_reviews) / len(scene_reviews))
            
            # Phase 3: Record quality trend data
            self.record_quality_trend(
                chapter_number=chapter_number,
                scene_number=None,
                quality_score=overall_quality,
                review_type="chapter",
                retry_attempt=retry_attempt
            )
            
            # Phase 3: Check if retry is recommended based on thresholds
            should_retry = self.should_retry_based_on_thresholds(
                quality_score=overall_quality,
                review_type="chapter",
                retry_attempt=retry_attempt
            )
            
            if should_retry:
                all_suggestions.insert(0, f"Chapter quality score {overall_quality:.2f} below threshold "
                                     f"{self.quality_thresholds.minimum_chapter_quality:.2f}. Retry recommended.")
            
            # Create chapter review
            chapter_review = ChapterReview(
                chapter_number=chapter_number,
                section_name=section_name,
                overall_quality=overall_quality,
                scene_reviews=scene_reviews,
                coherence_score=coherence_score,
                pacing_score=pacing_score,
                character_development_score=character_development_score,
                total_word_count=total_word_count,
                issues=all_issues,
                strengths=all_strengths,
                suggestions=all_suggestions,
                timestamp=datetime.now().isoformat(),
                confidence=confidence
            )
            
            return chapter_review
            
        except Exception as e:
            self.logger.error(f"Error reviewing chapter: {e}")
            # Return default review on error
            return ChapterReview(
                chapter_number=chapter_number,
                section_name=section_name,
                overall_quality=0.5,
                scene_reviews=scene_reviews,
                coherence_score=0.5,
                pacing_score=0.5,
                character_development_score=0.5,
                total_word_count=sum(scene.word_count for scene in scene_reviews),
                issues=[f"Error during chapter review: {str(e)}"],
                strengths=[],
                suggestions=["Retry chapter review"],
                timestamp=datetime.now().isoformat(),
                confidence=0.3
            )
            
    def _review_batch(self, chapter_reviews: List[ChapterReview], batch_number: int, retry_attempt: int = 0) -> BatchReview:
        """Perform batch-level review across multiple chapters."""
        try:
            # Extract chapter numbers
            chapter_numbers = [review.chapter_number for review in chapter_reviews]
            
            # Calculate average quality
            chapter_quality_scores = [review.overall_quality for review in chapter_reviews]
            average_quality = sum(chapter_quality_scores) / len(chapter_quality_scores) if chapter_quality_scores else 0.7
            
            # Calculate total word count
            total_word_count = sum(review.total_word_count for review in chapter_reviews)
            
            # Analyze batch consistency
            consistency_score = self._analyze_batch_consistency(chapter_reviews)
            
            # Analyze story progression
            progression_score = self._analyze_story_progression(chapter_reviews)
            
            # Analyze style consistency
            style_consistency_score = self._analyze_style_consistency(chapter_reviews)
            
            # Aggregate issues and suggestions
            all_issues = []
            all_strengths = []
            all_suggestions = []
            
            # Add chapter-level issues/strengths/suggestions
            for review in chapter_reviews:
                for issue in review.issues:
                    if issue not in all_issues:
                        all_issues.append(issue)
                        
                for strength in review.strengths:
                    if strength not in all_strengths:
                        all_strengths.append(strength)
                        
                for suggestion in review.suggestions:
                    if suggestion not in all_suggestions:
                        all_suggestions.append(suggestion)
            
            # Add batch-level issues
            if consistency_score < self.quality_thresholds.consistency_threshold:
                all_issues.append("Inconsistent quality across chapters")
                all_suggestions.append("Standardize chapter quality and structure")
                
            if progression_score < self.quality_thresholds.progression_threshold:
                all_issues.append("Limited story progression across chapters")
                all_suggestions.append("Strengthen narrative arc across chapters")
                
            if style_consistency_score < self.quality_thresholds.style_consistency_threshold:
                all_issues.append("Inconsistent writing style")
                all_suggestions.append("Harmonize tone and style across chapters")
            
            # Calculate confidence
            confidence = min(0.9, sum(review.confidence for review in chapter_reviews) / len(chapter_reviews))
            
            # Phase 3: Record quality trend data
            # Use first chapter number as reference for the batch
            reference_chapter = chapter_numbers[0] if chapter_numbers else 0
            self.record_quality_trend(
                chapter_number=reference_chapter,
                scene_number=None,
                quality_score=average_quality,
                review_type="batch",
                retry_attempt=retry_attempt
            )
            
            # Phase 3: Check if retry is recommended based on thresholds
            should_retry = self.should_retry_based_on_thresholds(
                quality_score=average_quality,
                review_type="batch",
                retry_attempt=retry_attempt
            )
            
            if should_retry:
                all_suggestions.insert(0, f"Batch quality score {average_quality:.2f} below threshold "
                                     f"{self.quality_thresholds.minimum_batch_quality:.2f}. Retry recommended.")
            
            # Create batch review
            batch_review = BatchReview(
                batch_number=batch_number,
                chapter_numbers=chapter_numbers,
                chapter_reviews=chapter_reviews,
                consistency_score=consistency_score,
                progression_score=progression_score,
                style_consistency_score=style_consistency_score,
                total_word_count=total_word_count,
                average_quality=average_quality,
                issues=all_issues,
                strengths=all_strengths,
                suggestions=all_suggestions,
                timestamp=datetime.now().isoformat(),
                confidence=confidence
            )
            
            return batch_review
            
        except Exception as e:
            self.logger.error(f"Error reviewing batch: {e}")
            # Return default review on error
            return BatchReview(
                batch_number=batch_number,
                chapter_numbers=chapter_numbers,
                chapter_reviews=chapter_reviews,
                consistency_score=0.5,
                progression_score=0.5,
                style_consistency_score=0.5,
                total_word_count=sum(review.total_word_count for review in chapter_reviews),
                average_quality=0.5,
                issues=[f"Error during batch review: {str(e)}"],
                strengths=[],
                suggestions=["Retry batch review"],
                timestamp=datetime.now().isoformat(),
                confidence=0.3
            )
    
    def _analyze_chapter_coherence(self, scene_reviews: List[SceneReview]) -> float:
        """Analyze chapter coherence based on scene transitions and flow."""
        try:
            # Basic coherence analysis
            scenes_count = len(scene_reviews)
            if scenes_count == 0:
                return 0.5
            
            # Check for scene length variance (high variance = lower coherence)
            scene_lengths = [scene.word_count for scene in scene_reviews]
            if not scene_lengths:
                return 0.5
                
            avg_length = sum(scene_lengths) / len(scene_lengths)
            variance = sum((length - avg_length) ** 2 for length in scene_lengths) / len(scene_lengths)
            normalized_variance = min(1.0, variance / (avg_length * 0.5))
            
            # Higher variance = lower coherence
            coherence_base = 0.8 - (normalized_variance * 0.3)
            
            # Penalize for very few scenes (less than 3)
            if scenes_count < 3:
                coherence_base -= 0.1
                
            # Penalize for very short scenes
            if any(length < 200 for length in scene_lengths):
                coherence_base -= 0.1
                
            return max(0.1, min(0.9, coherence_base))
            
            # Average scene quality as coherence indicator
            avg_scene_quality = sum(review.quality_score for review in scene_reviews) / scenes_count
            
            # Adjust based on scene count (more scenes = potentially more complex)
            coherence_bonus = min(0.1, scenes_count * 0.02)
            
            return min(1.0, avg_scene_quality + coherence_bonus)
            
        except Exception as e:
            self.logger.error(f"Error analyzing chapter coherence: {e}")
            return 0.5
    
    def _analyze_chapter_pacing(self, chapter_content: str, scene_reviews: List[SceneReview]) -> float:
        """Analyze chapter pacing based on scene lengths and variety."""
        try:
            if not scene_reviews:
                return 0.5
            
            # Calculate scene length variance (good pacing has variety)
            scene_lengths = [review.word_count for review in scene_reviews]
            if len(scene_lengths) < 2:
                return 0.6  # Single scene, decent pacing
            
            avg_length = sum(scene_lengths) / len(scene_lengths)
            variance = sum((length - avg_length) ** 2 for length in scene_lengths) / len(scene_lengths)
            
            # Normalize variance to 0-1 scale (some variance is good)
            normalized_variance = min(1.0, variance / (avg_length ** 2))
            
            # Good pacing has moderate variance (not too uniform, not too chaotic)
            if 0.1 <= normalized_variance <= 0.4:
                pacing_score = 0.8
            elif normalized_variance < 0.1:
                pacing_score = 0.6  # Too uniform
            else:
                pacing_score = 0.5  # Too chaotic
            
            return pacing_score
            
        except Exception as e:
            self.logger.error(f"Error analyzing chapter pacing: {e}")
            return 0.5
    
    def _analyze_character_development(self, chapter_content: str) -> float:
        """Analyze character development indicators in the chapter."""
        try:
            # Simple heuristics for character development
            content_lower = chapter_content.lower()
            
            # Look for dialogue (character interaction)
            dialogue_count = content_lower.count('"') + content_lower.count("'")
            dialogue_score = min(0.3, dialogue_count * 0.01)
            
            # Look for emotional/internal words
            emotion_words = ['felt', 'thought', 'realized', 'wondered', 'hoped', 'feared', 'remembered']
            emotion_count = sum(content_lower.count(word) for word in emotion_words)
            emotion_score = min(0.3, emotion_count * 0.05)
            
            # Look for action/decision words
            action_words = ['decided', 'chose', 'determined', 'resolved', 'acted', 'moved']
            action_count = sum(content_lower.count(word) for word in action_words)
            action_score = min(0.2, action_count * 0.05)
            
            # Base score for having content
            base_score = 0.2
            
            total_score = base_score + dialogue_score + emotion_score + action_score
            return min(1.0, total_score)
            
        except Exception as e:
            self.logger.error(f"Error analyzing character development: {e}")
            return 0.5
    
    def _analyze_batch_consistency(self, chapter_reviews: List[ChapterReview]) -> float:
        """Analyze consistency across chapters in a batch."""
        try:
            if len(chapter_reviews) < 2:
                return 0.8  # Single chapter is consistent with itself
            
            # Check quality consistency
            qualities = [review.overall_quality for review in chapter_reviews]
            quality_variance = sum((q - sum(qualities)/len(qualities))**2 for q in qualities) / len(qualities)
            quality_consistency = max(0.0, 1.0 - quality_variance * 2)
            
            # Check word count consistency
            word_counts = [review.total_word_count for review in chapter_reviews]
            avg_words = sum(word_counts) / len(word_counts)
            word_variance = sum((w - avg_words)**2 for w in word_counts) / len(word_counts)
            word_consistency = max(0.0, 1.0 - (word_variance / (avg_words**2)))
            
            # Weighted average
            consistency = (quality_consistency * 0.7) + (word_consistency * 0.3)
            return min(1.0, consistency)
            
        except Exception as e:
            self.logger.error(f"Error analyzing batch consistency: {e}")
            return 0.5
    
    def _analyze_story_progression(self, chapter_reviews: List[ChapterReview]) -> float:
        """Analyze story progression across chapters."""
        try:
            if len(chapter_reviews) < 2:
                return 0.7  # Single chapter, assume decent progression
            
            # Check if chapters are in sequence
            chapter_numbers = [review.chapter_number for review in chapter_reviews]
            is_sequential = all(chapter_numbers[i] == chapter_numbers[i-1] + 1 
                              for i in range(1, len(chapter_numbers)))
            
            if is_sequential:
                progression_score = 0.8
            else:
                progression_score = 0.6  # Non-sequential, but still valid
            
            # Bonus for variety in sections
            sections = set(review.section_name for review in chapter_reviews)
            if len(sections) > 1:
                progression_score += 0.1
            
            return min(1.0, progression_score)
            
        except Exception as e:
            self.logger.error(f"Error analyzing story progression: {e}")
            return 0.5
    
    def _analyze_style_consistency(self, chapter_reviews: List[ChapterReview]) -> float:
        """Analyze style consistency across chapters."""
        try:
            if len(chapter_reviews) < 2:
                return 0.8  # Single chapter is consistent with itself
            
            # Use character development scores as style indicator
            char_dev_scores = [review.character_development_score for review in chapter_reviews]
            avg_char_dev = sum(char_dev_scores) / len(char_dev_scores)
            char_dev_variance = sum((s - avg_char_dev)**2 for s in char_dev_scores) / len(char_dev_scores)
            
            # Use pacing scores as another style indicator
            pacing_scores = [review.pacing_score for review in chapter_reviews]
            avg_pacing = sum(pacing_scores) / len(pacing_scores)
            pacing_variance = sum((s - avg_pacing)**2 for s in pacing_scores) / len(pacing_scores)
            
            # Lower variance = higher consistency
            char_consistency = max(0.0, 1.0 - char_dev_variance * 4)
            pacing_consistency = max(0.0, 1.0 - pacing_variance * 4)
            
            # Average the consistency measures
            style_consistency = (char_consistency + pacing_consistency) / 2
            return min(1.0, style_consistency)
            
        except Exception as e:
            self.logger.error(f"Error analyzing style consistency: {e}")
            return 0.5
    
    # ========== REVIEW DATA PERSISTENCE METHODS ==========
    
    def _save_scene_review(self, scene_review: SceneReview) -> None:
        """Save scene review data to the quality directory."""
        try:
            # Create quality/reviews directory if it doesn't exist
            reviews_dir = os.path.join(self.dir_manager.get_quality_dir(), "reviews")
            os.makedirs(reviews_dir, exist_ok=True)
            
            # Save scene review
            filename = f"scene_ch{scene_review.chapter_number}_sc{scene_review.scene_number}_review.json"
            filepath = os.path.join(reviews_dir, filename)
            
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(asdict(scene_review), f, indent=2, ensure_ascii=False)
                
            self.logger.debug(f"Saved scene review: {filename}")
            
        except Exception as e:
            self.logger.error(f"Error saving scene review: {e}")
    
    def _save_chapter_review(self, chapter_review: ChapterReview) -> None:
        """Save chapter review data to the quality directory."""
        try:
            # Create quality/reviews directory if it doesn't exist
            reviews_dir = os.path.join(self.dir_manager.get_quality_dir(), "reviews")
            os.makedirs(reviews_dir, exist_ok=True)
            
            # Save chapter review
            filename = f"chapter_{chapter_review.chapter_number}_review.json"
            filepath = os.path.join(reviews_dir, filename)
            
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(asdict(chapter_review), f, indent=2, ensure_ascii=False)
                
            self.logger.debug(f"Saved chapter review: {filename}")
            
        except Exception as e:
            self.logger.error(f"Error saving chapter review: {e}")
    
    def _save_batch_review(self, batch_review: BatchReview) -> None:
        """Save batch review data to the quality directory."""
        try:
            # Create quality/reviews directory if it doesn't exist
            reviews_dir = os.path.join(self.dir_manager.get_quality_dir(), "reviews")
            os.makedirs(reviews_dir, exist_ok=True)
            
            # Save batch review
            filename = f"batch_{batch_review.batch_number}_review.json"
            filepath = os.path.join(reviews_dir, filename)
            
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(asdict(batch_review), f, indent=2, ensure_ascii=False)
                
            self.logger.debug(f"Saved batch review: {filename}")
            
            # Also save summary metrics
            self._save_batch_metrics_summary(batch_review)
            
        except Exception as e:
            self.logger.error(f"Error saving batch review: {e}")
    
    def _save_batch_metrics_summary(self, batch_review: BatchReview) -> None:
        """Save batch metrics summary for dashboard/reporting."""
        try:
            # Create quality/metrics directory if it doesn't exist
            metrics_dir = os.path.join(self.dir_manager.get_quality_dir(), "metrics")
            os.makedirs(metrics_dir, exist_ok=True)
            
            # Create metrics summary
            metrics_summary = {
                "batch_number": batch_review.batch_number,
                "timestamp": batch_review.timestamp,
                "chapters_count": len(batch_review.chapter_numbers),
                "total_word_count": batch_review.total_word_count,
                "average_quality": batch_review.average_quality,
                "consistency_score": batch_review.consistency_score,
                "progression_score": batch_review.progression_score,
                "style_consistency_score": batch_review.style_consistency_score,
                "issues_count": len(batch_review.issues),
                "suggestions_count": len(batch_review.suggestions),
                "confidence": batch_review.confidence,
                "chapter_details": [
                    {
                        "chapter_number": ch.chapter_number,
                        "section_name": ch.section_name,
                        "quality_score": ch.overall_quality,
                        "word_count": ch.total_word_count,
                        "scene_count": len(ch.scene_reviews)
                    }
                    for ch in batch_review.chapter_reviews
                ]
            }
            
            # Save metrics summary
            filename = f"batch_{batch_review.batch_number}_metrics.json"
            filepath = os.path.join(metrics_dir, filename)
            
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(metrics_summary, f, indent=2, ensure_ascii=False)
                
            self.logger.debug(f"Saved batch metrics: {filename}")
            
        except Exception as e:
            self.logger.error(f"Error saving batch metrics: {e}")
    
    def _load_chapter_review(self, chapter_number: int) -> Optional[ChapterReview]:
        """Load chapter review from file."""
        try:
            reviews_dir = os.path.join(self.dir_manager.get_quality_dir(), "reviews")
            filename = f"chapter_{chapter_number}_review.json"
            filepath = os.path.join(reviews_dir, filename)
            
            if not os.path.exists(filepath):
                return None
                
            with open(filepath, 'r', encoding='utf-8') as f:
                review_data = json.load(f)
                
            # Convert scene reviews back to objects
            scene_reviews = []
            for scene_data in review_data.get('scene_reviews', []):
                scene_review = SceneReview(**scene_data)
                scene_reviews.append(scene_review)
            
            # Create chapter review object
            review_data['scene_reviews'] = scene_reviews
            chapter_review = ChapterReview(**review_data)
            
            return chapter_review
            
        except Exception as e:
            self.logger.error(f"Error loading chapter review for chapter {chapter_number}: {e}")
            return None
    
    # Phase 3: Advanced Analytics & Optimization Methods
    
    def record_quality_trend(self, chapter_number: int, scene_number: Optional[int], 
                           quality_score: float, review_type: str, retry_attempt: int = 0) -> None:
        """Record quality trend data for analytics."""
        try:
            timestamp = datetime.now().isoformat()
            
            # Calculate improvement from previous attempt
            improvement = self._calculate_quality_improvement(
                chapter_number, scene_number, quality_score, review_type, retry_attempt
            )
            
            trend = QualityTrend(
                timestamp=timestamp,
                chapter_number=chapter_number,
                scene_number=scene_number,
                quality_score=quality_score,
                review_type=review_type,
                improvement_from_previous=improvement,
                retry_attempt=retry_attempt
            )
            
            # Save trend data
            self._save_quality_trend(trend)
            
            # Update cache
            self._quality_trends_cache.append(trend)
            self._last_cache_update = datetime.now()
            
        except Exception as e:
            self.logger.error(f"Error recording quality trend: {e}")
    
    def _calculate_quality_improvement(self, chapter_number: int, scene_number: Optional[int],
                                     current_score: float, review_type: str, retry_attempt: int) -> Optional[float]:
        """Calculate quality improvement from previous attempt or similar content."""
        try:
            # Load previous trends for comparison
            trends = self.get_quality_trends()
            
            # Find most recent similar review
            previous_score = None
            for trend in reversed(trends):
                if (trend.chapter_number == chapter_number and 
                    trend.scene_number == scene_number and
                    trend.review_type == review_type and
                    trend.retry_attempt == retry_attempt - 1):
                    previous_score = trend.quality_score
                    break
            
            if previous_score is not None:
                return current_score - previous_score
            
            return None
            
        except Exception as e:
            self.logger.error(f"Error calculating quality improvement: {e}")
            return None
    
    def _save_quality_trend(self, trend: QualityTrend) -> None:
        """Save quality trend data to file."""
        try:
            trends_dir = os.path.join(self.dir_manager.get_quality_dir(), "trends")
            os.makedirs(trends_dir, exist_ok=True)
            
            # Create filename based on trend type and identifiers
            if trend.scene_number is not None:
                filename = f"trend_ch{trend.chapter_number}_sc{trend.scene_number}_{trend.review_type}.json"
            else:
                filename = f"trend_ch{trend.chapter_number}_{trend.review_type}.json"
            
            filepath = os.path.join(trends_dir, filename)
            
            # Load existing trends or create new list
            trends_list = []
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    trends_list = json.load(f)
            
            # Add new trend
            trends_list.append(asdict(trend))
            
            # Save updated trends
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(trends_list, f, indent=2, ensure_ascii=False)
                
        except Exception as e:
            self.logger.error(f"Error saving quality trend: {e}")
    
    def get_quality_trends(self, chapter_number: Optional[int] = None, 
                          review_type: Optional[str] = None) -> List[QualityTrend]:
        """Get quality trends with optional filtering."""
        try:
            # Check cache first (performance optimization)
            if (self._last_cache_update and 
                (datetime.now() - self._last_cache_update).total_seconds() < self._cache_ttl_seconds):
                trends = self._quality_trends_cache.copy()
            else:
                # Load from files
                trends = self._load_all_quality_trends()
                self._quality_trends_cache = trends.copy()
                self._last_cache_update = datetime.now()
            
            # Apply filters
            if chapter_number is not None:
                trends = [t for t in trends if t.chapter_number == chapter_number]
            
            if review_type is not None:
                trends = [t for t in trends if t.review_type == review_type]
            
            return sorted(trends, key=lambda t: t.timestamp)
            
        except Exception as e:
            self.logger.error(f"Error getting quality trends: {e}")
            return []
    
    def _load_all_quality_trends(self) -> List[QualityTrend]:
        """Load all quality trends from files."""
        trends = []
        try:
            trends_dir = os.path.join(self.dir_manager.get_quality_dir(), "trends")
            
            if not os.path.exists(trends_dir):
                return trends
            
            # Load all trend files
            for filename in os.listdir(trends_dir):
                if filename.endswith('.json'):
                    filepath = os.path.join(trends_dir, filename)
                    with open(filepath, 'r', encoding='utf-8') as f:
                        trend_data_list = json.load(f)
                    
                    for trend_data in trend_data_list:
                        trend = QualityTrend(**trend_data)
                        trends.append(trend)
            
            return trends
            
        except Exception as e:
            self.logger.error(f"Error loading quality trends: {e}")
            return trends
    
    def analyze_quality_trends(self, chapter_number: Optional[int] = None) -> Dict[str, Any]:
        """Analyze quality trends and provide insights."""
        try:
            trends = self.get_quality_trends(chapter_number)
            
            if not trends:
                return {
                    "total_reviews": 0,
                    "average_quality": 0.0,
                    "quality_trend": "no_data",
                    "improvement_rate": 0.0,
                    "retry_rate": 0.0
                }
            
            # Calculate basic statistics
            total_reviews = len(trends)
            average_quality = sum(t.quality_score for t in trends) / total_reviews
            
            # Calculate improvement trend
            improvements = [t.improvement_from_previous for t in trends if t.improvement_from_previous is not None]
            improvement_rate = sum(improvements) / len(improvements) if improvements else 0.0
            
            # Calculate retry rate
            retries = [t for t in trends if t.retry_attempt > 0]
            retry_rate = len(retries) / total_reviews if total_reviews > 0 else 0.0
            
            # Determine overall trend
            if len(trends) >= 3:
                recent_scores = [t.quality_score for t in trends[-3:]]
                early_scores = [t.quality_score for t in trends[:3]]
                recent_avg = sum(recent_scores) / len(recent_scores)
                early_avg = sum(early_scores) / len(early_scores)
                
                if recent_avg > early_avg + 0.05:
                    quality_trend = "improving"
                elif recent_avg < early_avg - 0.05:
                    quality_trend = "declining"
                else:
                    quality_trend = "stable"
            else:
                quality_trend = "insufficient_data"
            
            return {
                "total_reviews": total_reviews,
                "average_quality": round(average_quality, 3),
                "quality_trend": quality_trend,
                "improvement_rate": round(improvement_rate, 3),
                "retry_rate": round(retry_rate, 3),
                "latest_quality": trends[-1].quality_score if trends else 0.0,
                "best_quality": max(t.quality_score for t in trends),
                "worst_quality": min(t.quality_score for t in trends)
            }
            
        except Exception as e:
            self.logger.error(f"Error analyzing quality trends: {e}")
            return {"error": str(e)}
    
    def should_retry_based_on_thresholds(self, quality_score: float, review_type: str, 
                                       retry_attempt: int = 0) -> bool:
        """Determine if content should be retried based on configurable thresholds."""
        try:
            if not self.quality_thresholds.retry_below_threshold:
                return False
            
            if retry_attempt >= self.quality_thresholds.max_retries:
                return False
            
            # Check against appropriate threshold
            if review_type == "scene":
                return quality_score < self.quality_thresholds.minimum_scene_quality
            elif review_type == "chapter":
                return quality_score < self.quality_thresholds.minimum_chapter_quality
            elif review_type == "batch":
                return quality_score < self.quality_thresholds.minimum_batch_quality
            
            return False
            
        except Exception as e:
            self.logger.error(f"Error checking retry thresholds: {e}")
            return False
    
    def generate_quality_dashboard(self) -> Dict[str, Any]:
        """Generate comprehensive quality dashboard data."""
        try:
            # Overall trends analysis
            overall_analysis = self.analyze_quality_trends()
            
            # Per-chapter analysis
            chapter_analyses = {}
            trends = self.get_quality_trends()
            chapter_numbers = list(set(t.chapter_number for t in trends))
            
            for chapter_num in sorted(chapter_numbers):
                chapter_analyses[f"chapter_{chapter_num}"] = self.analyze_quality_trends(chapter_num)
            
            # Review type breakdown
            review_type_stats = {}
            for review_type in ["scene", "chapter", "batch"]:
                type_trends = self.get_quality_trends(review_type=review_type)
                if type_trends:
                    review_type_stats[review_type] = {
                        "count": len(type_trends),
                        "average_quality": sum(t.quality_score for t in type_trends) / len(type_trends),
                        "latest_quality": type_trends[-1].quality_score
                    }
            
            # Threshold compliance
            threshold_compliance = {
                "scene_threshold": self.quality_thresholds.minimum_scene_quality,
                "chapter_threshold": self.quality_thresholds.minimum_chapter_quality,
                "batch_threshold": self.quality_thresholds.minimum_batch_quality,
                "retry_enabled": self.quality_thresholds.retry_below_threshold,
                "max_retries": self.quality_thresholds.max_retries
            }
            
            return {
                "generated_at": datetime.now().isoformat(),
                "overall_analysis": overall_analysis,
                "chapter_analyses": chapter_analyses,
                "review_type_stats": review_type_stats,
                "threshold_compliance": threshold_compliance,
                "cache_status": {
                    "last_update": self._last_cache_update.isoformat() if self._last_cache_update else None,
                    "cached_trends": len(self._quality_trends_cache),
                    "ttl_seconds": self._cache_ttl_seconds
                }
            }
            
        except Exception as e:
            self.logger.error(f"Error generating quality dashboard: {e}")
            return {"error": str(e)}
    
    def save_quality_dashboard(self) -> str:
        """Save quality dashboard to file and return filepath."""
        try:
            dashboard_data = self.generate_quality_dashboard()
            
            # Save to quality/reports directory
            reports_dir = os.path.join(self.dir_manager.get_quality_dir(), "reports")
            os.makedirs(reports_dir, exist_ok=True)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"quality_dashboard_{timestamp}.json"
            filepath = os.path.join(reports_dir, filename)
            
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(dashboard_data, f, indent=2, ensure_ascii=False)
            
            # Also save as latest dashboard
            latest_filepath = os.path.join(reports_dir, "quality_dashboard_latest.json")
            with open(latest_filepath, 'w', encoding='utf-8') as f:
                json.dump(dashboard_data, f, indent=2, ensure_ascii=False)
            
            self.logger.info(f"Quality dashboard saved: {filename}")
            return filepath
            
        except Exception as e:
            self.logger.error(f"Error saving quality dashboard: {e}")
            return ""


# Convenience functions for integration
def analyze_story_chapters(output_dir: str, app_instance=None) -> Tuple[List[ChapterInfo], ChapterWritingPlan]:
    """Analyze story structure and create writing plan."""
    agent = ChapterWritingAgent(output_dir, app_instance)
    chapter_info_list, _ = agent.analyze_chapter_structure()
    plan = agent.create_writing_plan(chapter_info_list)
    return chapter_info_list, plan


def write_next_chapters(
    output_dir: str,
    batch_size: int = 1,
    app_instance=None,
    cancel_token: Optional[CancelToken] = None,
) -> AgentResult:
    """Write the next batch of chapters automatically."""
    agent = ChapterWritingAgent(output_dir, app_instance, cancel_token=cancel_token)
    chapter_info_list, _ = agent.analyze_chapter_structure()
    plan = agent.create_writing_plan(chapter_info_list, batch_size)
    return agent.write_chapters_batch(chapter_info_list, plan)


def get_chapter_progress(output_dir: str, app_instance=None) -> Dict[str, Any]:
    """Get progress report on chapter writing."""
    agent = ChapterWritingAgent(output_dir, app_instance)
    chapter_info_list, _ = agent.analyze_chapter_structure()
    return agent.get_progress_report(chapter_info_list)
