"""Design-generation-review loop, parameterized by domain profile."""

from __future__ import annotations

import logging
import os
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from agents.review.domain_review_agent import DomainReview, DomainReviewAgent
from core.generation.cancellation import CancelToken, raise_if_cancelled
from core.generation.chapter_acceptance import (
    ChapterAcceptanceError,
    ChapterAcceptanceResult,
    ChapterAcceptanceService,
    ValidationIssue,
    ValidationReport,
)
from core.generation.domain_profiles import (
    QUALITY_LOOP_OFF,
    STRICT_PASS_AVERAGE_BONUS,
    DomainProfile,
    apply_quality_loop_mode,
    resolve_domain_profile,
    resolve_quality_loop_mode,
)
from core.generation.helper_fns import parse_scene_sections, write_file
from core.generation.narrative_quality import analyze_narrative_quality
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


# 可放行的最大分差。取严格档相对标准档的加分幅度：差距不超过这个数的稿子，
# 本质上是「标准档能过、严格档差一点」，不构成让整轮写作停机的理由。
WAIVABLE_SHORTFALL = STRICT_PASS_AVERAGE_BONUS

# 分差是浮点减法算出来的（3.2 - 3.0 = 0.20000000000000018），直接和上限比大小
# 会把正好卡在边界上的稿子判成超标。
_SHORTFALL_TOLERANCE = 1e-6


def waiver_reason(review: DomainReview, stage_label: str) -> Optional[str]:
    """重试耗尽后判断这份未过闸的评审能否放行，不能则返回 None。

    闸门拦下的其实是两类东西：一类是硬伤——硬失败代码、必要维度不及格，那是
    契约违约、信息越界、前后矛盾，必须挡住；另一类只是平均分差几毫，评审自己
    在 evidence 里逐条写着「通过」、repair_scope 填 "none"。为后一类中断整轮
    写作是不成比例的：它拦下的不是错误，只是不够出彩，而重修又拿不到任何可执行
    依据，重试只能靠运气翻身。这里放行后者并留下警告，硬伤仍旧照挡。

    差得多也不放行：那已经不是「不够出彩」，而是这一稿确实没写好。
    """
    if not review.soft_failure or review.shortfall > WAIVABLE_SHORTFALL + _SHORTFALL_TOLERANCE:
        return None
    return (
        f"{stage_label} 在重试耗尽后仍差 {review.shortfall:.2f} 分，按差分放行："
        f"平均 {review.average_score:.2f} 分，门槛 {review.pass_average:.2f} 分；"
        f"评审未报告硬失败，必要维度全部达标。"
    )


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
    gate_waivers: List[str] = field(default_factory=list)
    regeneration_marker: str = ""
    narrative_report: Dict[str, Any] = field(default_factory=dict)

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

    def _review_complete_chapter(
        self,
        chapter_content: str,
        contract: Dict[str, Any],
        case_bible: Dict[str, Any],
        suspense_ledger: Dict[str, Any],
        previous_chapter_tail: str,
        repairs_requested: Optional[List[str]] = None,
    ) -> DomainReview:
        """Use independent contract/reader/plausibility gates when available.

        The fallback keeps injected test reviewers and third-party reviewer
        implementations source-compatible.
        """

        bundle = getattr(self.reviewer, "review_chapter_bundle", None)
        if callable(bundle):
            return bundle(
                chapter_content,
                contract,
                case_bible,
                suspense_ledger,
                previous_chapter_tail=previous_chapter_tail,
                repairs_requested=repairs_requested,
            )
        return self.reviewer.review_chapter(
            chapter_content,
            contract,
            case_bible,
            suspense_ledger,
            repairs_requested=repairs_requested,
        )

    @staticmethod
    def _retry_is_stuck(review: DomainReview, requested: List[str]) -> bool:
        """这一轮重修有没有可能带来新结果。

        两种情况不可能：评审判不通过却开不出任何修复项，重修只能瞎猜；以及评审
        原样退回上一轮提过的同一批修复项——那批要求已经被照做过一次而问题依旧，
        再用同样的说法要求第三次，得到的还是同一稿。场景规划那边早就按「同一个
        错误挺过自己的修复就停」收手，这里补齐，把省下的调用留给真正能改的问题。
        """
        if not review.has_actionable_repair:
            return True
        return bool(requested) and set(review.asks) == set(requested)

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
        # Final acceptance may repair a contract.  If that happens, the prose
        # generated from the old contract is invalid and this exact generation
        # context is required to rebuild every scene before acceptance retries.
        self._generation_context = {
            "parameters": parameters,
            "lore": lore,
            "generate_scene": generate_scene,
            "on_plan_revised": on_plan_revised,
        }
        self._check_cancelled()
        self.ledger.initialize(parameters)
        pending_regeneration = self.ledger.pending_chapter_regeneration(chapter_number)
        regeneration_marker = str((pending_regeneration or {}).get("id", ""))
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
        gate_waivers: List[str] = []
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

        # Reveal deadlines belong to the story-wide source of truth, while scene
        # generation consumes the chapter contract.  Bridge the two here so a
        # legacy or hand-authored contract cannot accidentally expose a truth
        # before the chapter declared by the case bible.
        contract = self._apply_case_bible_knowledge_boundaries(
            contract, case_bible, chapter_number
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

            unmet_asks: List[str] = []
            for attempt in range(self.max_plan_retries):
                if plan_review.passed:
                    break
                retry_count += 1
                requested = plan_review.asks
                previous_average = plan_review.average_score
                self.logger.warning(
                    "Chapter %s plan failed domain gate (attempt %s): %s",
                    chapter_number,
                    attempt + 1,
                    "; ".join(requested) or "评审未给出具体修复项",
                )
                revised_plan = self.reviewer.revise_plan(
                    current_plan, plan_review, contract, unmet_asks=unmet_asks
                )
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
                    contract = self._apply_case_bible_knowledge_boundaries(
                        contract, case_bible, chapter_number
                    )
                plan_review = self.reviewer.review_plan(
                    current_plan,
                    contract,
                    case_bible,
                    suspense_ledger,
                    repairs_requested=requested,
                )
                self.ledger.save_review(
                    chapter_number, f"plan_retry_{attempt + 1}", plan_review.to_dict()
                )
                unmet_asks = plan_review.unmet_asks_from(requested)
                improved = plan_review.average_score > previous_average
                if not improved and self._retry_is_stuck(plan_review, requested):
                    self.logger.warning(
                        "Chapter %s plan stopped retrying: the same repairs survived "
                        "their own revision",
                        chapter_number,
                    )
                    break

            if not plan_review.passed:
                stage_label = f"第 {chapter_number} 章场景规划"
                reason = waiver_reason(plan_review, stage_label)
                if reason is None:
                    raise QualityGateError(
                        f"{stage_label}在 {self.max_plan_retries} 次修订后"
                        f"仍未通过{profile.label}质量检查",
                        chapter_number=chapter_number,
                    )
                self.logger.warning("Chapter %s plan waived: %s", chapter_number, reason)
                plan_review = plan_review.waive(reason)
                gate_waivers.append(reason)
                self.ledger.save_review(chapter_number, "plan_waived", plan_review.to_dict())

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

            # 保留历次重修中最好的一稿：重修不保证单调变好，放弃时该交出最好的
            # 结果，而不是碰巧最后生成的那一稿。
            best_prose, best_review = prose, review
            unmet_asks = []
            for attempt in range(self.max_scene_retries):
                if review.passed:
                    break
                retry_count += 1
                if best_review.average_score > review.average_score:
                    # 上一轮改差了。接着这一稿往下改，退步会一路累积；退回目前
                    # 最好的一稿重来，每轮至少从同一个高度出发。评审必须跟着一起
                    # 回退，否则它引用的原文根本不在待修订的稿子里。
                    prose, review = best_prose, best_review
                requested = review.asks
                self.logger.warning(
                    "Chapter %s Scene %s failed domain gate (attempt %s, avg %.2f/%.2f): %s",
                    chapter_number,
                    index,
                    attempt + 1,
                    review.average_score,
                    review.pass_average,
                    "; ".join(requested) or "评审未给出具体修复项",
                )
                prose = self.reviewer.revise_scene(
                    prose,
                    review,
                    scene_plan,
                    previous_tail,
                    next_scene_plan,
                    contract,
                    unmet_asks=unmet_asks,
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
                    repairs_requested=requested,
                )
                self.ledger.save_review(
                    chapter_number,
                    f"scene_{index}_retry_{attempt + 1}",
                    review.to_dict(),
                )
                improved = review.average_score > best_review.average_score
                if review.passed or improved:
                    best_prose, best_review = prose, review
                unmet_asks = review.unmet_asks_from(requested)
                if not review.passed and not improved and self._retry_is_stuck(review, requested):
                    self.logger.warning(
                        "Chapter %s Scene %s stopped retrying: %s",
                        chapter_number,
                        index,
                        "the same repairs survived their own revision"
                        if unmet_asks
                        else "no actionable feedback, no gain",
                    )
                    break

            prose, review = best_prose, best_review
            if not review.passed:
                stage_label = f"第 {chapter_number} 章场景 {index}"
                reason = waiver_reason(review, stage_label)
                if reason is None:
                    raise QualityGateError(
                        f"{stage_label} 在 {self.max_scene_retries} 次修订后仍未通过质量检查",
                        partial_scenes=[*generated_scenes, prose],
                        chapter_number=chapter_number,
                    )
                self.logger.warning(
                    "Chapter %s Scene %s waived: %s", chapter_number, index, reason
                )
                review = review.waive(reason)
                gate_waivers.append(reason)
                self.ledger.save_review(
                    chapter_number, f"scene_{index}_waived", review.to_dict()
                )
            generated_scenes.append(prose)
            scene_reviews.append(review)

        # 这里不再检查取消：所有场景都已生成，收尾（评审与验收）应当走完，
        # 否则取消等于丢掉一整章的成果。下一章开始前才是下一个安全点。
        chapter_content = "\n\n---\n\n".join(generated_scenes)
        narrative_report = analyze_narrative_quality(chapter_content).to_dict()
        self.ledger.save_review(chapter_number, "narrative_signals", narrative_report)
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
                gate_waivers=gate_waivers,
                regeneration_marker=regeneration_marker,
                narrative_report=narrative_report,
            )

        chapter_review = self._review_complete_chapter(
            chapter_content,
            contract,
            case_bible,
            suspense_ledger,
            previous_chapter_tail,
        )
        self.ledger.save_review(chapter_number, "chapter", chapter_review.to_dict())

        # A chapter-level failure is routed to the scene named in repair_scope.
        # The repaired scene and the assembled chapter must both pass again.
        repair_review = chapter_review
        repaired_scene_passed = True
        # 章节级重修就地覆盖某一场，而重修不保证变好。留一份当前最好的整章快照，
        # 放弃时交出它，别让一次变差的定向修订把已经更好的稿子顶掉。
        best_scenes = list(generated_scenes)
        best_scene_reviews = list(scene_reviews)
        best_chapter_review = chapter_review
        unmet_asks = []
        for attempt in range(self.max_scene_retries):
            if chapter_review.passed and repaired_scene_passed:
                break
            target = self._target_scene(repair_review.repair_scope, len(generated_scenes))
            retry_count += 1
            requested = repair_review.asks
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
                unmet_asks=unmet_asks,
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
                repairs_requested=requested,
            )
            repaired_scene_passed = repaired_scene_review.passed
            scene_reviews[target - 1] = repaired_scene_review
            self.ledger.save_review(
                chapter_number,
                f"chapter_retry_{attempt + 1}_scene_{target}",
                repaired_scene_review.to_dict(),
            )
            chapter_content = "\n\n---\n\n".join(generated_scenes)
            chapter_review = self._review_complete_chapter(
                chapter_content,
                contract,
                case_bible,
                suspense_ledger,
                previous_chapter_tail,
                repairs_requested=requested,
            )
            self.ledger.save_review(
                chapter_number,
                f"chapter_retry_{attempt + 1}",
                chapter_review.to_dict(),
            )
            repair_review = (
                repaired_scene_review if not repaired_scene_review.passed else chapter_review
            )
            improved = chapter_review.average_score > best_chapter_review.average_score
            if (chapter_review.passed and repaired_scene_passed) or improved:
                best_scenes = list(generated_scenes)
                best_scene_reviews = list(scene_reviews)
                best_chapter_review = chapter_review
            unmet_asks = repair_review.unmet_asks_from(requested)
            if (
                repaired_scene_passed
                and not chapter_review.passed
                and not improved
                and self._retry_is_stuck(chapter_review, requested)
            ):
                # 同上：没有可执行依据又没有进步，继续重修只是重复同一次调用。
                self.logger.warning(
                    "Chapter %s stopped chapter-level retrying: %s",
                    chapter_number,
                    "the same repairs survived their own revision"
                    if unmet_asks
                    else "no actionable feedback, no gain",
                )
                break

        generated_scenes = best_scenes
        scene_reviews = best_scene_reviews
        chapter_review = best_chapter_review
        narrative_report = analyze_narrative_quality(
            "\n\n---\n\n".join(generated_scenes)
        ).to_dict()

        blocked = [
            review for review in (chapter_review, *scene_reviews) if not review.passed
        ]
        if blocked:
            stage_label = f"第 {chapter_number} 章章节级检查"
            reason = (
                waiver_reason(blocked[0], stage_label)
                if all(review.soft_failure for review in blocked)
                else None
            )
            if reason is None:
                raise QualityGateError(
                    f"第 {chapter_number} 章在定向修订后仍未通过章节级质量检查，请人工审核",
                    partial_scenes=generated_scenes,
                    chapter_number=chapter_number,
                )
            self.logger.warning("Chapter %s waived: %s", chapter_number, reason)
            if not chapter_review.passed:
                chapter_review = chapter_review.waive(reason)
            scene_reviews = [
                review if review.passed else review.waive(reason) for review in scene_reviews
            ]
            gate_waivers.append(reason)
            self.ledger.save_review(
                chapter_number, "chapter_waived", chapter_review.to_dict()
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
            gate_waivers=gate_waivers,
            regeneration_marker=regeneration_marker,
            narrative_report=narrative_report,
        )

    # Repair routes, most authoritative first.  A human ruling outranks every
    # automatic fix, and a stale base revision has to be refreshed before any
    # other repair is even meaningful.
    _REPAIR_ORDER = ("human_decision", "rebase", "contract", "prose")

    @classmethod
    def _repair_route(cls, issues: List[ValidationIssue]) -> str:
        targets = {issue.repair_target for issue in issues}
        for route in cls._REPAIR_ORDER:
            if route in targets:
                return route
        # dependency_audit / case_bible only ever arrive as warnings; anything
        # else blocking is not something this loop knows how to repair.
        return "human_decision"

    def accept_result(
        self,
        chapter_number: int,
        result: ChapterLoopResult,
        chapter_path: Optional[str] = None,
    ) -> ChapterAcceptanceResult:
        """Validate final saved prose and atomically advance accepted story state.

        Failures are routed by what actually has to change.  Sending everything
        to a prose rewrite cannot fix a contract that disagrees with the ledger,
        and stopping at the first non-prose failure — which is what this used to
        do — dumps a machine-fixable problem on the author.
        """
        if not result.chapter_review or not result.chapter_review.passed:
            raise QualityGateError("不能把未通过章节级检查的内容写入悬疑账本")
        pending_regeneration = self.ledger.pending_chapter_regeneration(chapter_number)
        pending_marker = str((pending_regeneration or {}).get("id", ""))
        if pending_marker and result.regeneration_marker != pending_marker:
            raise QualityGateError(
                "作者裁定已经更新章节契约，裁定前生成的旧正文禁止提交；请完整重生成本章",
                partial_scenes=result.scenes,
                chapter_number=chapter_number,
            )
        resolved_path = chapter_path or os.path.join(
            self.output_dir,
            "story",
            "content",
            "chapters",
            f"chapter_{chapter_number}.md",
        )

        def accept_current() -> ChapterAcceptanceResult:
            accepted = self.acceptance_service.accept(
                chapter_number,
                result.chapter_content,
                result.contract,
                result.chapter_review.to_dict(),
                result.base_revision,
                resolved_path,
            )
            if result.regeneration_marker:
                try:
                    self.ledger.complete_chapter_regeneration(
                        chapter_number, result.regeneration_marker
                    )
                except Exception:  # the chapter is already committed; keep that truth
                    self.logger.exception(
                        "Chapter %s committed but regeneration marker cleanup failed",
                        chapter_number,
                    )
            return accepted

        self.adjudication = ""
        # A contract clash that survived its own adjudication is not new input.
        # Remember each exact clash so the next acceptance pass can stop before
        # asking the same model the same question again.
        self._attempted_contract_conflicts = set()
        try:
            return accept_current()
        except ChapterAcceptanceError as acceptance_error:
            report = acceptance_error.report

        for attempt in range(self.max_acceptance_retries):
            blocking = report.blocking_issues
            if not blocking:
                raise ChapterAcceptanceError(report, self.adjudication)
            route = self._repair_route(blocking)
            self.logger.warning(
                "Chapter %s failed acceptance (%s/%s), route=%s: %s",
                chapter_number,
                attempt + 1,
                self.max_acceptance_retries,
                route,
                "; ".join(issue.message for issue in blocking),
            )
            if route == "human_decision":
                raise ChapterAcceptanceError(report, self.adjudication)
            # 只有正文重修值得再来一次：它每次调用都会拿到一份不同的稿子。
            # rebase 和 contract 报告「什么都没动」时，输入与上一轮完全相同，
            # 再走一遍必然得到同一个结果，只是把剩下的预算烧掉。
            retryable = route == "prose"
            if route == "rebase":
                changed = self._repair_rebase(result)
            elif route == "contract":
                changed = self._repair_contract(chapter_number, result, blocking)
                if changed and self.adjudication == self.ADJUDICATION_RESOLVED:
                    # keep_existing changed the source of truth.  The current
                    # prose was written from the rejected contract and may not
                    # be accepted merely because the metadata now agrees.
                    changed = self._regenerate_after_contract_change(
                        chapter_number, result, resolved_path
                    )
            else:
                changed = self._repair_prose(
                    chapter_number, result, report, resolved_path, attempt
                )
            if not changed:
                # Nothing moved, so re-running acceptance would fail identically.
                if not retryable:
                    break
                continue
            try:
                return accept_current()
            except ChapterAcceptanceError as acceptance_error:
                report = acceptance_error.report

        raise ChapterAcceptanceError(report, self.adjudication)

    def _regenerate_after_contract_change(
        self,
        chapter_number: int,
        result: ChapterLoopResult,
        resolved_path: str,
    ) -> bool:
        """Invalidate old prose and rerun the complete chapter quality loop."""
        context = getattr(self, "_generation_context", None)
        if not isinstance(context, dict):
            raise QualityGateError(
                "章节契约已修正，但缺少完整重生成上下文，不能复用旧正文",
                partial_scenes=result.scenes,
                chapter_number=chapter_number,
            )

        old_content = result.chapter_content
        archive_dir = os.path.join(
            self.output_dir,
            "quality",
            "contract_regenerations",
            f"chapter_{chapter_number}",
        )
        archive_path = os.path.join(
            archive_dir, f"invalidated_{source_hash(old_content)[:12]}.md"
        )
        write_file(archive_path, old_content)
        self.logger.warning(
            "Chapter %s contract changed; archived invalidated prose at %s and regenerating all scenes",
            chapter_number,
            archive_path,
        )

        prior_retries = result.retry_count
        regenerated = self.run(
            chapter_number=chapter_number,
            plan_content=result.plan_content,
            parameters=context["parameters"],
            lore=context["lore"],
            generate_scene=context["generate_scene"],
            on_plan_revised=context.get("on_plan_revised"),
        )
        result.__dict__.update(regenerated.__dict__)
        result.retry_count += prior_retries + 1
        write_file(resolved_path, result.chapter_content)
        self.ledger.save_review(
            chapter_number,
            "contract_regeneration",
            {
                "stage": "contract_regeneration",
                "passed": True,
                "invalidated_content_hash": source_hash(old_content),
                "regenerated_content_hash": source_hash(result.chapter_content),
                "invalidated_prose": os.path.relpath(archive_path, self.output_dir),
            },
        )
        return True

    def _repair_rebase(self, result: ChapterLoopResult) -> bool:
        """Refresh the revision this chapter was built against.

        Another chapter committed while this one was being written.  Nothing
        about the chapter is wrong; only its idea of "current" is.
        """
        current = int(self.ledger.load_suspense_ledger().get("revision", 0) or 0)
        if current == result.base_revision:
            return False
        result.base_revision = current
        return True

    # How an automatic contract repair ended.  These are the reasons a run can
    # stop, and they call for different advice.
    ADJUDICATION_RESOLVED = "resolved"
    ADJUDICATION_AWAITING_AUTHOR = "awaiting_author"
    ADJUDICATION_UNEXPLAINED = "unexplained_reversal"
    ADJUDICATION_NO_ANSWER = "no_answer"
    ADJUDICATION_REPEATED = "repeated_verdict"
    ADJUDICATION_REPAIR_FAILED = "repair_failed"

    @staticmethod
    def _conflict_signature(conflict: Dict[str, Any]) -> Tuple[str, str, str, str, str]:
        """The exact question posed for one clash, independent of dict order."""
        return (
            str(conflict.get("code", "")),
            str(conflict.get("id", "")),
            str(conflict.get("field", "")),
            str(conflict.get("existing", "")),
            str(conflict.get("proposed", "")),
        )

    def _repair_contract(
        self,
        chapter_number: int,
        result: ChapterLoopResult,
        blocking: List[ValidationIssue],
    ) -> bool:
        """Resolve contract-vs-ledger clashes by making the model take a position.

        Rewriting the contract to match the ledger would quietly delete a
        deliberate plot reversal; keeping the chapter's value would quietly
        corrupt established canon.  Asking which one it is turns the clash into
        a declared, reviewable decision instead of a silent edit.
        """
        conflicts = [
            dict(issue.details, code=issue.code)
            for issue in blocking
            if issue.code.endswith("_fact_conflict")
        ]
        if not conflicts:
            self.adjudication = ""
            return False

        signatures = {self._conflict_signature(item) for item in conflicts}
        attempted = getattr(self, "_attempted_contract_conflicts", set())
        repeated = signatures & attempted
        if repeated:
            # Acceptance has returned an identical clash after the previous
            # judgement.  Nothing in the evidence or prompt has changed, so a
            # second model call is repetition, not another repair opportunity.
            self.logger.warning(
                "Chapter %s has %s unchanged contract conflict(s); stopping before re-asking",
                chapter_number,
                len(repeated),
            )
            if self.adjudication == self.ADJUDICATION_RESOLVED:
                self.adjudication = self.ADJUDICATION_REPAIR_FAILED
            elif not self.adjudication:
                self.adjudication = self.ADJUDICATION_REPEATED
            return False
        attempted.update(signatures)
        self._attempted_contract_conflicts = attempted

        try:
            verdict = self.reviewer.decide_fact_conflicts(chapter_number, conflicts)
        except Exception as exc:  # a failed judgement must not lose the chapter
            self.logger.error("Chapter %s conflict adjudication failed: %s", chapter_number, exc)
            self.adjudication = self.ADJUDICATION_NO_ANSWER
            return False

        decisions = {
            (str(item.get("id")), str(item.get("field"))): item
            for item in verdict.get("decisions", [])
            if isinstance(item, dict)
        }
        changed = False
        unexplained = False
        awaiting_author = False
        answered = False
        unanswered = False
        for conflict in conflicts:
            decision = decisions.get((str(conflict.get("id")), str(conflict.get("field"))))
            if not decision:
                unanswered = True
                continue
            answered = True
            decision_kind = str(decision.get("decision"))
            if decision_kind == "contradict":
                reason = str(decision.get("reason", "")).strip()
                if not reason:
                    # An unexplained reversal is exactly what a person must rule
                    # on, so it is left to fail rather than accepted on faith.
                    unexplained = True
                    continue
                if self._declare_contradiction(result.contract, conflict, reason):
                    changed = True
                    awaiting_author = True
            elif decision_kind == "keep_existing":
                changed |= self._restore_accepted_value(result.contract, conflict)
            else:
                # Unknown or hedged answers are not aliases for keep_existing.
                # Treating them as one would silently overwrite the chapter.
                unanswered = True

        if unexplained:
            self.adjudication = self.ADJUDICATION_UNEXPLAINED
        elif awaiting_author:
            self.adjudication = self.ADJUDICATION_AWAITING_AUTHOR
        elif unanswered or not answered:
            self.adjudication = self.ADJUDICATION_NO_ANSWER
        else:
            self.adjudication = self.ADJUDICATION_RESOLVED

        if changed:
            self.ledger.save_contract(chapter_number, result.contract, result.plan_content)
        # An unanswered or unexplained judgement matters just as much as a
        # successful rewrite.  Persist every outcome so the stop can be audited.
        self.ledger.save_review(
            chapter_number,
            "contract_conflict_adjudication",
            {
                "stage": "contract",
                "outcome": self.adjudication,
                "conflicts": conflicts,
                "decisions": verdict.get("decisions", []),
            },
        )
        return changed

    # Keyed by the record_type CanonConsistencyGate actually emits, which is not
    # always the contract slot's name ("character_attribute" vs
    # "character_updates").
    _CONFLICT_SLOTS = {
        "fact": ("facts_added", "facts_confirmed"),
        "character_attribute": ("character_updates",),
        "clue": ("clue_updates",),
        "evidence": ("evidence_updates",),
        "timeline": ("timeline_events",),
    }

    @classmethod
    def _conflict_slots(cls, code: str) -> Tuple[str, ...]:
        return cls._CONFLICT_SLOTS.get(code[: -len("_fact_conflict")], ())

    @classmethod
    def _restore_accepted_value(
        cls, contract: Dict[str, Any], conflict: Dict[str, Any]
    ) -> bool:
        """Put the ledger's value back into this chapter's contract."""
        target_id = str(conflict.get("id"))
        proposed_id = str(conflict.get("proposed_id") or target_id)
        field_name = str(conflict.get("field"))
        changed = False
        for slot in cls._conflict_slots(str(conflict.get("code", ""))):
            for record in contract.get(slot, []):
                if str(record.get("id")) not in {target_id, proposed_id}:
                    continue
                record["id"] = target_id
                record[field_name] = conflict.get("existing")
                changed = True
        return changed

    @classmethod
    def _declare_contradiction(
        cls, contract: Dict[str, Any], conflict: Dict[str, Any], reason: str
    ) -> bool:
        """Move a clashing record into the slot that says "this is on purpose"."""
        target_id = str(conflict.get("id"))
        already = any(
            str(record.get("id")) == target_id
            for record in contract.get("facts_contradicted", [])
        )
        if already:
            return False
        contract.setdefault("facts_contradicted", []).append(
            {
                "id": target_id,
                "reason": reason,
                "new_value": conflict.get("proposed"),
                "existing": conflict.get("existing"),
                "label": conflict.get("label", ""),
                "field": conflict.get("field"),
            }
        )
        return True

    def _repair_prose(
        self,
        chapter_number: int,
        result: ChapterLoopResult,
        report: ValidationReport,
        resolved_path: str,
        attempt: int,
    ) -> bool:
        """Rewrite the one scene the report points at, if the rewrite passes review."""
        scene_plans = parse_scene_sections(result.plan_content)
        if not scene_plans or len(scene_plans) != len(result.scenes):
            raise QualityGateError(
                "最终验收失败后无法把正文场景映射回场景规划",
                partial_scenes=result.scenes,
                chapter_number=chapter_number,
            )
        scene_number, revised_scene = self.reviewer.revise_for_acceptance(
            result.scenes,
            scene_plans,
            report.to_dict(),
            result.contract,
        )
        target = scene_number - 1
        previous_tail = (
            result.scenes[target - 1][-2500:]
            if target > 0
            else self._load_previous_chapter_tail(chapter_number)
        )
        next_plan = scene_plans[target + 1] if target + 1 < len(scene_plans) else ""
        # 验收报告列的是确定性校验挑出来的硬问题，比评分意见具体得多。带着它去
        # 复评，评审先核对这几条有没有落实，而不是把重修稿当成一份新稿从头挑毛病
        # ——后者会让每一轮的判定标准都不一样，重修永远追不上。
        requested = [issue.message for issue in report.blocking_issues]
        scene_review = self.reviewer.review_scene(
            revised_scene,
            scene_plans[target],
            scene_number,
            previous_tail,
            next_plan,
            result.contract,
            self.ledger.load_case_bible(),
            self.ledger.load_suspense_ledger(),
            repairs_requested=requested,
        )
        self.ledger.save_review(
            chapter_number,
            f"acceptance_retry_{attempt + 1}_scene_{scene_number}",
            scene_review.to_dict(),
        )
        if not scene_review.passed:
            return False

        candidate_scenes = list(result.scenes)
        candidate_scenes[target] = revised_scene
        chapter_review = self._review_complete_chapter(
            "\n\n---\n\n".join(candidate_scenes),
            result.contract,
            self.ledger.load_case_bible(),
            self.ledger.load_suspense_ledger(),
            self._load_previous_chapter_tail(chapter_number),
            repairs_requested=requested,
        )
        self.ledger.save_review(
            chapter_number,
            f"acceptance_retry_{attempt + 1}_chapter",
            chapter_review.to_dict(),
        )
        if not chapter_review.passed:
            return False

        result.scenes = candidate_scenes
        result.scene_reviews[target] = scene_review
        result.chapter_review = chapter_review
        result.narrative_report = analyze_narrative_quality(result.chapter_content).to_dict()
        result.retry_count += 1
        write_file(resolved_path, result.chapter_content)
        return True

    def _describe_unaccepted_chapter(
        self, previous_number: int, chapter_number: int
    ) -> str:
        """Explain why the earlier chapter is stuck, in terms that can be acted on."""
        lines = [
            f"第 {previous_number} 章尚未验收，不能继续生成第 {chapter_number} 章。"
        ]
        blocking = [
            item
            for item in self.ledger.open_conflicts()
            if item.get("chapter") == previous_number
        ]
        if blocking:
            briefing = self._briefing_for(previous_number, blocking[0])
            if briefing is not None and briefing.is_decidable:
                for choice in briefing.choices:
                    lines.append(
                        f"原因：{choice.display_name} 与账本冲突——"
                        f"账本为「{choice.existing}」，第 {previous_number} 章声明"
                        f"「{choice.proposed}」。"
                    )
                lines.append(
                    f"请重新生成第 {previous_number} 章：契约冲突会先交给模型判定，"
                    "确属剧情反转时再由你裁定。"
                )
                return "\n".join(lines)
            lines.append(
                f"第 {previous_number} 章有 {len(blocking)} 处未决冲突，"
                f"需要先处理才能继续。"
            )
            return "\n".join(lines)

        chapter_file = os.path.join(
            self.output_dir, "story", "content", "chapters", f"chapter_{previous_number}.md"
        )
        if os.path.isfile(chapter_file):
            lines.append(
                f"第 {previous_number} 章的正文已存在但未通过验收，请重新生成该章。"
            )
        else:
            lines.append(f"第 {previous_number} 章还没有正文，请先写完该章。")
        return "\n".join(lines)

    def _briefing_for(self, chapter_number: int, conflict: Dict[str, Any]):
        """Rebuild the decision view from a stored conflict record."""
        from core.generation.chapter_acceptance import ValidationIssue
        from core.generation.conflict_briefing import build_briefing

        stored = self.ledger.load_conflict(str(conflict.get("id")))
        if not stored:
            return None
        issues = [
            ValidationIssue(
                code=str(item.get("code", "")),
                message=str(item.get("message", "")),
                severity=str(item.get("severity", "blocking")),
                repair_target=str(item.get("repair_target", "delta")),
                details=item.get("details") or {},
            )
            for item in stored.get("report", {}).get("issues", [])
            if item.get("severity", "blocking") == "blocking"
        ]
        return build_briefing(chapter_number, issues)

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

    @staticmethod
    def _apply_case_bible_knowledge_boundaries(
        contract: Dict[str, Any],
        case_bible: Dict[str, Any],
        chapter_number: int,
    ) -> Dict[str, Any]:
        """Copy story-wide reveal deadlines into a chapter's generation boundary.

        Older contracts do not have ``withheld_truth_ids`` and are deliberately
        accepted.  The returned copy gains that audit field plus the human-readable
        facts used by existing prompts and reviewers; caller-owned data is never
        mutated.
        """

        enriched = deepcopy(contract)
        withheld_ids = list(enriched.get("withheld_truth_ids") or [])
        withheld_facts = list(enriched.get("reader_must_not_know_yet") or [])

        def reveal_chapter(value: Any) -> Optional[int]:
            if isinstance(value, bool):
                return None
            if isinstance(value, int):
                return value
            if not isinstance(value, str):
                return None
            text = value.strip()
            for pattern in (r"^chapter[_\s-]*(\d+)$", r"^第\s*(\d+)\s*章$"):
                match = re.match(pattern, text, flags=re.IGNORECASE)
                if match:
                    return int(match.group(1))
            return None

        truths = case_bible.get("truth", []) if isinstance(case_bible, dict) else []
        for truth in truths if isinstance(truths, list) else []:
            if not isinstance(truth, dict):
                continue
            boundary = reveal_chapter(truth.get("must_not_reveal_before"))
            if boundary is None or chapter_number >= boundary:
                continue
            truth_id = str(truth.get("id", "")).strip()
            fact = str(truth.get("fact", "")).strip()
            if truth_id and truth_id not in withheld_ids:
                withheld_ids.append(truth_id)
            if fact and fact not in withheld_facts:
                withheld_facts.append(fact)

        enriched["withheld_truth_ids"] = withheld_ids
        enriched["reader_must_not_know_yet"] = withheld_facts
        return enriched

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
            # "第 15 章尚未验收" is a symptom, not a cause.  The author needs to
            # know what is actually holding that chapter up and what closes it,
            # or they are left clicking 写作 and getting the same wall back.
            raise QualityGateError(
                self._describe_unaccepted_chapter(previous_number, chapter_number),
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
