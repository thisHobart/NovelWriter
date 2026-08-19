"""Design-generation-review loop, parameterized by domain profile."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from agents.review.domain_review_agent import DomainReview, DomainReviewAgent
from core.generation.cancellation import CancelToken, raise_if_cancelled
from core.generation.chapter_acceptance import (
    ChapterAcceptanceError,
    ChapterAcceptanceResult,
    ChapterAcceptanceService,
)
from core.generation.domain_profiles import (
    QUALITY_LOOP_OFF,
    DomainProfile,
    apply_quality_loop_mode,
    resolve_domain_profile,
    resolve_quality_loop_mode,
)
from core.generation.helper_fns import parse_scene_sections, write_file
from core.generation.planning_contract import (
    PlanningContractError,
    validate_planning_contract,
)
from core.generation.story_ledger import (
    StoryLedgerManager,
    case_bible_gaps,
    source_hash,
)


class QualityGateError(RuntimeError):
    """Raised when content still fails after the bounded retry loop.

    Carries whatever prose was produced before the gate failed so callers can
    archive it for inspection instead of writing it into the manuscript.
    """

    def __init__(
        self,
        message: str,
        partial_scenes: Optional[List[str]] = None,
        chapter_number: Optional[int] = None,
    ):
        super().__init__(message)
        self.partial_scenes = list(partial_scenes or [])
        self.chapter_number = chapter_number


def skipped_review(stage: str) -> DomainReview:
    """A stand-in verdict for stages the quality loop was told to skip."""
    return DomainReview(
        stage=stage,
        passed=True,
        reviewer_warning="质量闭环已关闭，本阶段未做领域评审",
    )


@dataclass
class ChapterLoopResult:
    scenes: List[str]
    contract: Dict[str, Any]
    base_revision: int
    plan_content: str
    plan_revised: bool
    plan_review: DomainReview
    scene_reviews: List[DomainReview] = field(default_factory=list)
    chapter_review: Optional[DomainReview] = None
    retry_count: int = 0
    profile_key: str = ""

    @property
    def chapter_content(self) -> str:
        return "\n\n---\n\n".join(self.scenes)


class ChapterGenerationLoop:
    """Run bounded plan, scene, and chapter quality loops."""

    def __init__(
        self,
        output_dir: str,
        model: str,
        logger: Optional[logging.Logger] = None,
        max_plan_retries: Optional[int] = None,
        max_scene_retries: Optional[int] = None,
        reviewer: Optional[DomainReviewAgent] = None,
        acceptance_service: Optional[ChapterAcceptanceService] = None,
        profile: Optional[DomainProfile] = None,
        cancel_token: Optional[CancelToken] = None,
        require_planning_contract: bool = False,
        max_acceptance_retries: int = 2,
    ):
        self.output_dir = output_dir
        self.model = model
        self.logger = logger or logging.getLogger("chapter-generation-loop")
        # None 表示「跟随领域档案」；显式数字覆盖档案默认值。
        self._max_plan_retries = max_plan_retries
        self._max_scene_retries = max_scene_retries
        self.max_plan_retries = max_plan_retries if max_plan_retries is not None else 2
        self.max_scene_retries = max_scene_retries if max_scene_retries is not None else 2
        self.ledger = StoryLedgerManager(output_dir)
        self.profile = profile
        self._injected_reviewer = reviewer
        self.reviewer = reviewer
        self.acceptance_service = acceptance_service or ChapterAcceptanceService(self.ledger)
        self.cancel_token = cancel_token
        self.require_planning_contract = require_planning_contract
        self.max_acceptance_retries = max(0, int(max_acceptance_retries))

    def _check_cancelled(self) -> None:
        raise_if_cancelled(self.cancel_token)

    def _bind_profile(self, parameters: Dict[str, Any], mode: str) -> DomainProfile:
        """Settle which domain profile governs this chapter, then build the reviewer.

        A project's profile is locked into the story bible the first time it is
        initialized. Re-resolving from parameters on every run would silently
        switch the review dimensions mid-book and make accepted chapters
        incomparable, so the locked value wins and drift is only reported.
        """
        resolved = self.profile or resolve_domain_profile(parameters)
        locked = self.ledger.locked_profile()
        if locked and locked.key != resolved.key:
            self.logger.warning(
                "Story bible is locked to domain profile '%s' but parameters resolve to "
                "'%s'; keeping the locked profile. Start a new project to change it.",
                locked.key,
                resolved.key,
            )
            resolved = locked

        resolved = apply_quality_loop_mode(resolved, mode)
        self.profile = resolved
        if self._max_plan_retries is None:
            self.max_plan_retries = resolved.max_plan_retries
        if self._max_scene_retries is None:
            self.max_scene_retries = resolved.max_scene_retries
        if self._injected_reviewer is None:
            self.reviewer = DomainReviewAgent(
                model=self.model,
                profile=resolved,
                logger=self.logger,
            )
        return resolved

    def run(
        self,
        chapter_number: int,
        plan_content: str,
        parameters: Dict[str, Any],
        lore: str,
        generate_scene: Callable[..., str],
        on_plan_revised: Optional[Callable[[str], None]] = None,
    ) -> ChapterLoopResult:
        self._check_cancelled()
        self.ledger.initialize(parameters)
        mode = resolve_quality_loop_mode(parameters)
        profile = self._bind_profile(parameters, mode)
        reviews_enabled = mode != QUALITY_LOOP_OFF
        self.logger.info(
            "Chapter %s uses domain profile '%s' (%s), quality loop mode '%s'",
            chapter_number,
            profile.key,
            profile.label,
            mode,
        )
        base_revision = self.ledger.current_revision()
        case_bible = self.ledger.load_case_bible()
        if reviews_enabled:
            design_context = self.ledger.load_design_context()
            if self.ledger.case_bible_needs_refresh(case_bible, design_context):
                self.logger.info(
                    "Building %s from whole-story structure and chapter outlines",
                    profile.bible_noun,
                )
                case_bible = self.reviewer.build_case_bible(
                    parameters,
                    lore,
                    design_context,
                    case_bible,
                )
                # 结构阶段已经明确声明过的骨架优先于二次提炼的结果：那是作者写下
                # 的，不是模型从散文里猜出来的，且已经通过了结构契约的校验与重试。
                declared = self.ledger.declared_story_spine()
                if declared:
                    self.logger.info(
                        "Applying %s declared by the structure stage: %s",
                        profile.bible_noun,
                        "、".join(sorted(declared)),
                    )
                    case_bible = self.ledger.merge_declared_story_spine(case_bible)
                case_bible = self.ledger.save_case_bible(case_bible, design_context)
            self._require_usable_case_bible(case_bible, profile, chapter_number)
        suspense_ledger = self.ledger.load_suspense_ledger()

        current_plan = plan_content
        plan_revised = False
        retry_count = 0
        contract = self.ledger.load_contract(chapter_number, current_plan)
        if contract is None:
            if self.require_planning_contract:
                raise QualityGateError(
                    f"第 {chapter_number} 章缺少与当前场景规划匹配的前置契约，"
                    "请返回场景规划阶段重新生成",
                    chapter_number=chapter_number,
                )
            # 关闭档不调用大模型建契约：用确定性回落契约，账本仍能连续记账。
            contract = (
                self.reviewer.build_chapter_contract(
                    chapter_number,
                    current_plan,
                    parameters,
                    lore,
                    case_bible,
                    suspense_ledger,
                )
                if reviews_enabled
                else self.reviewer.deterministic_contract(
                    chapter_number, parameters, current_plan
                )
            )

        if self.require_planning_contract:
            try:
                contract = validate_planning_contract(
                    contract, chapter_number, require_origin=True
                )
            except PlanningContractError as exc:
                raise QualityGateError(str(exc), chapter_number=chapter_number) from exc

        if not reviews_enabled:
            plan_review = skipped_review("plan")
        else:
            plan_review = self.reviewer.review_plan(
                current_plan, contract, case_bible, suspense_ledger
            )
            self.ledger.save_review(chapter_number, "plan", plan_review.to_dict())

            for attempt in range(self.max_plan_retries):
                if plan_review.passed:
                    break
                retry_count += 1
                self.logger.warning(
                    "Chapter %s plan failed domain gate (attempt %s): %s",
                    chapter_number,
                    attempt + 1,
                    "; ".join(plan_review.repair_instructions),
                )
                revised_plan = self.reviewer.revise_plan(current_plan, plan_review, contract)
                if not parse_scene_sections(revised_plan):
                    raise QualityGateError(
                        "场景规划修订结果没有可解析的场景标题", chapter_number=chapter_number
                    )
                current_plan = revised_plan
                plan_revised = True
                if not self.require_planning_contract:
                    contract = self.reviewer.build_chapter_contract(
                        chapter_number,
                        current_plan,
                        parameters,
                        lore,
                        case_bible,
                        suspense_ledger,
                    )
                plan_review = self.reviewer.review_plan(
                    current_plan, contract, case_bible, suspense_ledger
                )
                self.ledger.save_review(
                    chapter_number, f"plan_retry_{attempt + 1}", plan_review.to_dict()
                )

            if not plan_review.passed:
                raise QualityGateError(
                    f"第 {chapter_number} 章场景规划在 {self.max_plan_retries} 次修订后"
                    f"仍未通过{profile.label}质量检查",
                    chapter_number=chapter_number,
                )

        if plan_revised and on_plan_revised:
            on_plan_revised(current_plan)

        contract = self.ledger.save_contract(chapter_number, contract, current_plan)
        scenes = parse_scene_sections(current_plan)
        if not scenes:
            raise QualityGateError("场景规划中没有可生成的场景", chapter_number=chapter_number)

        generated_scenes: List[str] = []
        scene_reviews: List[DomainReview] = []
        previous_chapter_tail = self._load_previous_chapter_tail(chapter_number)

        for index, scene_plan in enumerate(scenes, start=1):
            # 场景边界是安全点：已完成的场景还在内存里，尚未落盘也未提交账本。
            self._check_cancelled()
            previous_tail = (
                generated_scenes[-1][-2500:]
                if generated_scenes
                else previous_chapter_tail
            )
            next_scene_plan = scenes[index] if index < len(scenes) else ""
            prose = generate_scene(
                scene_plan=scene_plan,
                scene_number=index,
                previous_scene_tail=previous_tail,
                next_scene_plan=next_scene_plan,
                contract=contract,
                profile=profile,
            )
            if not prose or not prose.strip():
                raise QualityGateError(
                    f"第 {chapter_number} 章场景 {index} 的正文为空",
                    partial_scenes=generated_scenes,
                    chapter_number=chapter_number,
                )

            if not reviews_enabled:
                generated_scenes.append(prose)
                scene_reviews.append(skipped_review(f"scene_{index}"))
                continue

            review = self.reviewer.review_scene(
                prose,
                scene_plan,
                index,
                previous_tail,
                next_scene_plan,
                contract,
                case_bible,
                suspense_ledger,
            )
            self.ledger.save_review(chapter_number, f"scene_{index}", review.to_dict())

            for attempt in range(self.max_scene_retries):
                if review.passed:
                    break
                retry_count += 1
                self.logger.warning(
                    "Chapter %s Scene %s failed domain gate (attempt %s): %s",
                    chapter_number,
                    index,
                    attempt + 1,
                    "; ".join(review.repair_instructions),
                )
                prose = self.reviewer.revise_scene(
                    prose,
                    review,
                    scene_plan,
                    previous_tail,
                    next_scene_plan,
                    contract,
                )
                if not prose or not prose.strip():
                    raise QualityGateError(
                        f"第 {chapter_number} 章场景 {index} 的修订正文为空",
                        partial_scenes=generated_scenes,
                        chapter_number=chapter_number,
                    )
                review = self.reviewer.review_scene(
                    prose,
                    scene_plan,
                    index,
                    previous_tail,
                    next_scene_plan,
                    contract,
                    case_bible,
                    suspense_ledger,
                )
                self.ledger.save_review(
                    chapter_number,
                    f"scene_{index}_retry_{attempt + 1}",
                    review.to_dict(),
                )

            if not review.passed:
                raise QualityGateError(
                    f"第 {chapter_number} 章场景 {index} 在 {self.max_scene_retries} 次修订后仍未通过质量检查",
                    partial_scenes=[*generated_scenes, prose],
                    chapter_number=chapter_number,
                )
            generated_scenes.append(prose)
            scene_reviews.append(review)

        # 这里不再检查取消：所有场景都已生成，收尾（评审与验收）应当走完，
        # 否则取消等于丢掉一整章的成果。下一章开始前才是下一个安全点。
        chapter_content = "\n\n---\n\n".join(generated_scenes)
        if not reviews_enabled:
            return ChapterLoopResult(
                scenes=generated_scenes,
                contract=contract,
                base_revision=base_revision,
                plan_content=current_plan,
                plan_revised=plan_revised,
                plan_review=plan_review,
                scene_reviews=scene_reviews,
                chapter_review=skipped_review("chapter"),
                retry_count=retry_count,
                profile_key=profile.key,
            )

        chapter_review = self.reviewer.review_chapter(
            chapter_content,
            contract,
            case_bible,
            suspense_ledger,
        )
        self.ledger.save_review(chapter_number, "chapter", chapter_review.to_dict())

        # A chapter-level failure is routed to the scene named in repair_scope.
        # The repaired scene and the assembled chapter must both pass again.
        repair_review = chapter_review
        repaired_scene_passed = True
        for attempt in range(self.max_scene_retries):
            if chapter_review.passed and repaired_scene_passed:
                break
            target = self._target_scene(repair_review.repair_scope, len(generated_scenes))
            retry_count += 1
            # 修第一场时同样要带上一章结尾，否则重写出来的开头会与上一章脱节。
            previous_tail = (
                generated_scenes[target - 2][-2500:] if target > 1 else previous_chapter_tail
            )
            next_scene_plan = scenes[target] if target < len(scenes) else ""
            generated_scenes[target - 1] = self.reviewer.revise_scene(
                generated_scenes[target - 1],
                repair_review,
                scenes[target - 1],
                previous_tail,
                next_scene_plan,
                contract,
            )
            if not generated_scenes[target - 1].strip():
                raise QualityGateError(
                    f"第 {chapter_number} 章场景 {target} 的章节级修订正文为空"
                )

            repaired_scene_review = self.reviewer.review_scene(
                generated_scenes[target - 1],
                scenes[target - 1],
                target,
                previous_tail,
                next_scene_plan,
                contract,
                case_bible,
                suspense_ledger,
            )
            repaired_scene_passed = repaired_scene_review.passed
            scene_reviews[target - 1] = repaired_scene_review
            self.ledger.save_review(
                chapter_number,
                f"chapter_retry_{attempt + 1}_scene_{target}",
                repaired_scene_review.to_dict(),
            )
            chapter_content = "\n\n---\n\n".join(generated_scenes)
            chapter_review = self.reviewer.review_chapter(
                chapter_content,
                contract,
                case_bible,
                suspense_ledger,
            )
            self.ledger.save_review(
                chapter_number,
                f"chapter_retry_{attempt + 1}",
                chapter_review.to_dict(),
            )
            repair_review = (
                repaired_scene_review if not repaired_scene_review.passed else chapter_review
            )

        if not chapter_review.passed or not repaired_scene_passed:
            raise QualityGateError(
                f"第 {chapter_number} 章在定向修订后仍未通过章节级质量检查，请人工审核",
                partial_scenes=generated_scenes,
                chapter_number=chapter_number,
            )

        return ChapterLoopResult(
            scenes=generated_scenes,
            contract=contract,
            base_revision=base_revision,
            plan_content=current_plan,
            plan_revised=plan_revised,
            plan_review=plan_review,
            scene_reviews=scene_reviews,
            chapter_review=chapter_review,
            retry_count=retry_count,
            profile_key=profile.key,
        )

    def accept_result(
        self,
        chapter_number: int,
        result: ChapterLoopResult,
        chapter_path: Optional[str] = None,
    ) -> ChapterAcceptanceResult:
        """Validate final saved prose and atomically advance accepted story state."""
        if not result.chapter_review or not result.chapter_review.passed:
            raise QualityGateError("不能把未通过章节级检查的内容写入悬疑账本")
        resolved_path = chapter_path or os.path.join(
            self.output_dir,
            "story",
            "content",
            "chapters",
            f"chapter_{chapter_number}.md",
        )
        def accept_current() -> ChapterAcceptanceResult:
            return self.acceptance_service.accept(
                chapter_number,
                result.chapter_content,
                result.contract,
                result.chapter_review.to_dict(),
                result.base_revision,
                resolved_path,
            )

        try:
            return accept_current()
        except ChapterAcceptanceError as acceptance_error:
            report = acceptance_error.report

        blocking = report.blocking_issues
        # Contract/canon/rebase failures cannot be repaired by changing prose;
        # automatically changing the trusted upstream contract here would undo
        # the purpose of the planning gate.
        if not blocking or any(issue.repair_target != "prose" for issue in blocking):
            raise ChapterAcceptanceError(report)
        if self.max_acceptance_retries == 0:
            raise ChapterAcceptanceError(report)

        scene_plans = parse_scene_sections(result.plan_content)
        if not scene_plans or len(scene_plans) != len(result.scenes):
            raise QualityGateError(
                "最终验收失败后无法把正文场景映射回场景规划",
                partial_scenes=result.scenes,
                chapter_number=chapter_number,
            )

        last_report = report
        for attempt in range(self.max_acceptance_retries):
            self.logger.warning(
                "Chapter %s failed final acceptance; prose repair %s/%s: %s",
                chapter_number,
                attempt + 1,
                self.max_acceptance_retries,
                "; ".join(issue.message for issue in last_report.blocking_issues),
            )
            scene_number, revised_scene = self.reviewer.revise_for_acceptance(
                result.scenes,
                scene_plans,
                last_report.to_dict(),
                result.contract,
            )
            target = scene_number - 1
            previous_tail = (
                result.scenes[target - 1][-2500:]
                if target > 0
                else self._load_previous_chapter_tail(chapter_number)
            )
            next_plan = scene_plans[target + 1] if target + 1 < len(scene_plans) else ""
            scene_review = self.reviewer.review_scene(
                revised_scene,
                scene_plans[target],
                scene_number,
                previous_tail,
                next_plan,
                result.contract,
                self.ledger.load_case_bible(),
                self.ledger.load_suspense_ledger(),
            )
            self.ledger.save_review(
                chapter_number,
                f"acceptance_retry_{attempt + 1}_scene_{scene_number}",
                scene_review.to_dict(),
            )
            if not scene_review.passed:
                continue

            candidate_scenes = list(result.scenes)
            candidate_scenes[target] = revised_scene
            chapter_review = self.reviewer.review_chapter(
                "\n\n---\n\n".join(candidate_scenes),
                result.contract,
                self.ledger.load_case_bible(),
                self.ledger.load_suspense_ledger(),
            )
            self.ledger.save_review(
                chapter_number,
                f"acceptance_retry_{attempt + 1}_chapter",
                chapter_review.to_dict(),
            )
            if not chapter_review.passed:
                continue

            result.scenes = candidate_scenes
            result.scene_reviews[target] = scene_review
            result.chapter_review = chapter_review
            result.retry_count += 1
            write_file(resolved_path, result.chapter_content)
            try:
                return accept_current()
            except ChapterAcceptanceError as acceptance_error:
                last_report = acceptance_error.report
                if any(
                    issue.repair_target != "prose"
                    for issue in last_report.blocking_issues
                ):
                    raise

        raise QualityGateError(
            f"第 {chapter_number} 章最终验收在 {self.max_acceptance_retries} 次正文修订后仍未通过",
            partial_scenes=result.scenes,
            chapter_number=chapter_number,
        )

    def _require_usable_case_bible(
        self,
        case_bible: Dict[str, Any],
        profile: DomainProfile,
        chapter_number: int,
    ) -> None:
        """Stop before writing prose against a story bible that says nothing.

        The bible is derived from the structure and chapter-outline stages, which
        have no gate of their own.  Checking it here is the first point where a
        thin upstream design becomes detectable, and it is still cheap to fix:
        every later check silently passes against an empty bible instead.
        """
        if not case_bible.get("generated_from_design"):
            # 项目还没有全书结构或章节大纲可供提炼。这属于流程尚未走到，不是这一
            # 步该拦的事，验收闸门已有 case_bible_not_ready 警告负责提醒。
            return
        gaps = case_bible_gaps(case_bible, profile)
        if not gaps:
            return
        detail = "；".join(gaps)
        self.ledger.save_review(
            chapter_number,
            "case_bible_gate",
            {"stage": "case_bible", "passed": False, "gaps": gaps},
        )
        raise QualityGateError(
            f"{profile.bible_noun}尚不足以支撑跨章校验：{detail}。"
            "请先补全全书结构与章节大纲，再重新生成本章。",
            chapter_number=chapter_number,
        )

    def _load_previous_chapter_tail(self, chapter_number: int) -> str:
        if chapter_number <= 1:
            return ""
        if not self.require_planning_contract:
            filename = f"chapter_{chapter_number - 1}.md"
            candidates = (
                os.path.join(self.output_dir, "story", "content", "chapters", filename),
                os.path.join(self.output_dir, "chapters", filename),
            )
            for path in candidates:
                if os.path.isfile(path):
                    try:
                        with open(path, "r", encoding="utf-8") as handle:
                            return handle.read()[-2500:]
                    except (OSError, UnicodeError) as exc:
                        self.logger.warning(
                            "Could not read previous chapter tail from %s: %s", path, exc
                        )
            return ""
        previous_number = chapter_number - 1
        ledger = self.ledger.load_suspense_ledger()
        accepted = next(
            (
                item
                for item in ledger.get("accepted_chapters", [])
                if isinstance(item, dict) and item.get("chapter") == previous_number
            ),
            None,
        )
        if not accepted or not accepted.get("content_hash"):
            raise QualityGateError(
                f"第 {previous_number} 章尚未验收，不能继续生成第 {chapter_number} 章",
                chapter_number=chapter_number,
            )
        filename = f"chapter_{chapter_number - 1}.md"
        candidates = (
            os.path.join(self.output_dir, "story", "content", "chapters", filename),
            os.path.join(self.output_dir, "chapters", filename),
        )
        for path in candidates:
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    content = handle.read()
                if source_hash(content) != accepted["content_hash"]:
                    raise QualityGateError(
                        f"第 {previous_number} 章文件与验收账本不一致，不能作为下一章上下文",
                        chapter_number=chapter_number,
                    )
                return content[-2500:]
            except (OSError, UnicodeError) as exc:
                self.logger.warning("Could not read previous chapter tail from %s: %s", path, exc)
        raise QualityGateError(
            f"第 {previous_number} 章已记入验收账本，但找不到正式章节文件",
            chapter_number=chapter_number,
        )

    @staticmethod
    def _target_scene(repair_scope: str, scene_count: int) -> int:
        match = re.search(r"(?:scene|场景)[_\s-]*(\d+)", repair_scope or "", re.IGNORECASE)
        if match:
            return max(1, min(scene_count, int(match.group(1))))
        return scene_count
