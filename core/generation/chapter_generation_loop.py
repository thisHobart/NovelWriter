"""Design-generation-review loop, parameterized by domain profile."""

from __future__ import annotations

import logging
import os
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from agents.review.domain_review_agent import (
    DomainReview,
    DomainReviewAgent,
    DomainReviewError,
    SceneDialogue,
)
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
from core.generation.chapter_continuity import (
    continuity_gate,
    established_context,
    protected_terms,
    scene_one_continuity_rules,
)
from core.generation.helper_fns import parse_scene_sections, write_file
from core.generation.plan_fault import describe_plan_faults, plan_mandated_failures
from core.generation.narrative_quality import analyze_narrative_quality
from core.generation.planning_contract import (
    PlanningContractError,
    load_planning_contracts,
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

    也带上抬手那一刻的评审结论与现场：闸门早就算出了哪一句触硬伤、差几分、要改
    成什么，此前这些只随重试记录留在盘上，报错本身只剩一句「请人工审核」，作者
    得回头按时间戳翻几十份 JSON 才能开始判断。
    """

    def __init__(
        self,
        message: str,
        partial_scenes: Optional[List[str]] = None,
        chapter_number: Optional[int] = None,
        review: Optional[Dict[str, Any]] = None,
        snapshot: Optional[Dict[str, Any]] = None,
        verdict_unavailable: bool = False,
    ):
        super().__init__(message)
        self.partial_scenes = list(partial_scenes or [])
        self.chapter_number = chapter_number
        self.review = review or {}
        self.snapshot = snapshot or {}
        # 评审自己没能给出结论（回复没写完、格式不对），不是判了不合格。两者
        # 的出口不同：前者该重跑评审，后者该按清单重修。
        self.verdict_unavailable = bool(verdict_unavailable)


#: 关掉单场景多轮重修的参数值。默认开：改同一场时让模型看见自己上一稿，
#: 改动更贴着被点名的那几句走，而不是整体重写一遍。
SCENE_DIALOGUE_OFF = {"off", "false", "no", "关闭", "关"}


def scene_dialogue_enabled(parameters: Dict[str, Any]) -> bool:
    return str((parameters or {}).get("Scene Dialogue", "on")).strip().lower()         not in SCENE_DIALOGUE_OFF


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
    # 差额只来自其中一份评审。报合议后的平均分会写出「平均 3.59 分、门槛 3.20 分，
    # 仍差 0.08 分」这种自相矛盾的记录，作者事后查不出是哪一份短了。
    source = review.shortfall_source
    scores = (
        f"差在 {source}"
        if source
        else f"平均 {review.average_score:.2f} 分，门槛 {review.pass_average:.2f} 分"
    )
    return (
        f"{stage_label} 在重试耗尽后仍差 {review.shortfall:.2f} 分，按差分放行："
        f"{scores}；评审未报告硬失败，必要维度全部达标。"
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


def review_from_dict(payload):
    """从存档还原一份评审。`to_dict` 会多带一个算出来的 average_score，滤掉。"""
    if not isinstance(payload, dict):
        return None
    fields = DomainReview.__dataclass_fields__
    return DomainReview(**{k: v for k, v in payload.items() if k in fields})


def snapshot_of(result: ChapterLoopResult) -> Dict[str, Any]:
    """把一次循环的现场收成纯 JSON，供待复审记录保存。

    存的是重新落地这一章所需的全部输入：正文、契约、账本基线、场景规划与三份
    评审。案情圣经、悬念账本、上一章结尾都能从账本重新读出来，不必入档。
    """
    return {
        "scenes": list(result.scenes),
        "contract": result.contract,
        "base_revision": int(result.base_revision),
        "plan_content": result.plan_content,
        "plan_revised": bool(result.plan_revised),
        "plan_review": result.plan_review.to_dict() if result.plan_review else None,
        "scene_reviews": [review.to_dict() for review in result.scene_reviews],
        "chapter_review": (
            result.chapter_review.to_dict() if result.chapter_review else None
        ),
        "retry_count": int(result.retry_count),
        "profile_key": result.profile_key,
        "gate_waivers": list(result.gate_waivers),
        "regeneration_marker": result.regeneration_marker,
        "narrative_report": result.narrative_report,
    }


def restore_result(snapshot: Dict[str, Any]) -> ChapterLoopResult:
    """把待复审记录里的现场还原成一份循环结果。"""
    scene_reviews = [
        review
        for review in (
            review_from_dict(raw) for raw in snapshot.get("scene_reviews") or []
        )
        if review is not None
    ]
    return ChapterLoopResult(
        scenes=list(snapshot.get("scenes") or []),
        contract=snapshot.get("contract") or {},
        base_revision=int(snapshot.get("base_revision") or 0),
        plan_content=str(snapshot.get("plan_content") or ""),
        plan_revised=bool(snapshot.get("plan_revised")),
        plan_review=(
            review_from_dict(snapshot.get("plan_review")) or skipped_review("plan")
        ),
        scene_reviews=scene_reviews,
        chapter_review=review_from_dict(snapshot.get("chapter_review")),
        retry_count=int(snapshot.get("retry_count") or 0),
        profile_key=str(snapshot.get("profile_key") or ""),
        gate_waivers=list(snapshot.get("gate_waivers") or []),
        regeneration_marker=str(snapshot.get("regeneration_marker") or ""),
        narrative_report=snapshot.get("narrative_report") or {},
    )


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
        # 手上最新的一稿正文。评审失灵时要连它一起交出去，否则这一遍写出来的
        # 东西一个字都留不下，只能整章重写。
        self._latest_scenes: List[str] = []
        # 同一场的多轮重修是否走对话；由作品参数决定，绑定档案时读一次。
        self._scene_dialogue = True

    def _check_cancelled(self) -> None:
        raise_if_cancelled(self.cancel_token)

    def _prior_contracts(self, chapter_number: int) -> List[Dict[str, Any]]:
        """已经落盘的、本章之前的章节契约；读不出来就当没有。

        衔接检查是加分项，不该因为账本里有一份坏契约就让整章写不下去。
        """
        try:
            return [
                item
                for item in load_planning_contracts(self.output_dir)
                if int(item.get("chapter", 0)) < int(chapter_number)
            ]
        except (PlanningContractError, OSError, ValueError) as exc:
            self.logger.warning(
                "Chapter %s continuity context unavailable: %s", chapter_number, exc
            )
            return []

    def _plan_faults(
        self, review: DomainReview, scenes: List[str]
    ) -> List[Dict[str, Any]]:
        """这份评审里，哪几条硬伤是场景规划自己要求的。

        判据在 `core/generation/plan_fault.py`：硬伤必须附正文逐字引文，引文与规划
        共用的片段够长，就说明正文只是照规划写的。找出来之后写进评审警告，作者在
        待复审界面上能直接看到该回规划里改哪一句。
        """
        try:
            protected = protected_terms(self.ledger.load_suspense_ledger())
        except (OSError, ValueError, TypeError):
            # 人名地名只是用来把纯专名的重合排除掉，读不到账本就少一层过滤，
            # 判据照常成立——不该因为这一步让整轮重修停下来。
            protected = ()
        faults = plan_mandated_failures(review.hard_failures, scenes, protected=protected)
        if not faults:
            return []
        description = describe_plan_faults(faults)
        if description and description not in (review.reviewer_warning or ""):
            review.reviewer_warning = "; ".join(
                part for part in (review.reviewer_warning, description) if part
            )
        self.logger.warning("Plan-level defects, not repairable in prose:\n%s", description)
        return faults

    @staticmethod
    def _without_plan_faults(
        asks: List[str], plan_faults: List[Dict[str, Any]]
    ) -> List[str]:
        """把出在规划里的那几条从重修清单里摘掉，其余照常发下去。

        按引文匹配：一条 ask 的文本里带着某处规划缺陷的引文，就是同一条。
        """
        quotes = [
            str(item.get("quote", "")).strip()
            for item in plan_faults
            if str(item.get("quote", "")).strip()
        ]
        if not quotes:
            return asks
        return [ask for ask in asks if not any(quote in ask for quote in quotes)]

    def _continuity_rules(
        self, chapter_number: int, contract: Dict[str, Any]
    ) -> List[str]:
        """第一场提示词里的硬性衔接要求。"""
        if int(chapter_number) <= 1:
            return []
        prior = self._prior_contracts(chapter_number)
        return scene_one_continuity_rules(
            contract, established_context(prior, chapter_number)
        )

    def _continuity_report(
        self, chapter_number: int, chapter_content: str, contract: Dict[str, Any]
    ) -> Dict[str, Any]:
        """整章写完之后的确定性衔接检查，不花任何调用。"""
        prior = self._prior_contracts(chapter_number)
        report = continuity_gate(
            chapter_content,
            self._load_previous_chapter_content(chapter_number),
            contract,
            chapter_number,
            protected=protected_terms(self.ledger.load_suspense_ledger(), prior),
            prior_contracts=prior,
        )
        # 存一份带统计数字的原始结果：合议后的评审只留得下硬失败，套语条数、
        # 时间读数、悬念沉默章数这些量化结果没有别的地方可看。
        self.ledger.save_review(chapter_number, "continuity", report)
        return report

    def _review_complete_chapter(
        self,
        chapter_content: str,
        contract: Dict[str, Any],
        case_bible: Dict[str, Any],
        suspense_ledger: Dict[str, Any],
        previous_chapter_tail: str,
        repairs_requested: Optional[List[str]] = None,
        chapter_number: Optional[int] = None,
    ) -> DomainReview:
        """Use independent contract/reader/plausibility gates when available.

        The fallback keeps injected test reviewers and third-party reviewer
        implementations source-compatible.
        """

        bundle = getattr(self.reviewer, "review_chapter_bundle", None)
        if callable(bundle):
            # 衔接检查是确定性的，它不占一次调用，但结论必须和三份评审一起合议：
            # 否则「开头又把上一章重写了一遍」会在闸门之外被无声吞掉。
            continuity_report = (
                self._continuity_report(chapter_number, chapter_content, contract)
                if chapter_number
                else None
            )
            return bundle(
                chapter_content,
                contract,
                case_bible,
                suspense_ledger,
                previous_chapter_tail=previous_chapter_tail,
                repairs_requested=repairs_requested,
                continuity_report=continuity_report,
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
        self._scene_dialogue = scene_dialogue_enabled(parameters)
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

    def bind_generation_context(
        self,
        parameters: Dict[str, Any],
        lore: str,
        generate_scene: Callable[..., str],
        on_plan_revised: Optional[Callable[[str], None]] = None,
    ) -> None:
        """记下重新生成这一章所需的输入。

        最终验收有可能判定这一章的契约要让位于账本。那时按旧契约写出来的正文
        整体作废，必须逐场重生成，靠的就是这份上下文。正常写作路径进 run() 时
        自动记下；复审路径（照建议重修、人工放行）没有跑过生成，必须由调用方
        显式绑定——不绑的话，作者点下去等到最后一步才换来一句「缺少完整重生成
        上下文」，而那时已经烧掉了整轮调用。
        """
        self._generation_context = {
            "parameters": parameters,
            "lore": lore,
            "generate_scene": generate_scene,
            "on_plan_revised": on_plan_revised,
        }

    def _save_unavailable_verdict(
        self, chapter_number: int, error: DomainReviewError
    ) -> str:
        """把评审失灵的现场存进本章的评审目录：错在哪、大模型原样回了什么。

        只记一句错误摘要是查不出原因的——「评审里没有评分表」既可能是模型漏写了
        字段，也可能是回复根本没写完，两者的处置完全不同。回复原文留在盘上，下
        次报同一句话时能直接看出是哪一种。
        """
        return self.ledger.save_review(
            chapter_number,
            "review_unavailable",
            {
                "stage": getattr(error, "stage", ""),
                "error": str(error),
                "model": self.model,
                "responses": list(getattr(error, "responses", []) or []),
            },
        )

    def _verdict_unavailable(
        self,
        chapter_number: int,
        error: DomainReviewError,
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> QualityGateError:
        """评审没能出结论时，按「待复审」交出去，而不是让整章白跑。

        判不合格与判不出来是两码事，但对作者来说后果一样：这一章不能进正式稿。
        既然如此，正文就该和判不合格时一样留住，让作者能重跑评审或直接放行。
        """
        path = self._save_unavailable_verdict(chapter_number, error)
        self.logger.error(
            "Chapter %s has no verdict: %s (raw response saved at %s)",
            chapter_number,
            error,
            path,
        )
        return QualityGateError(
            f"第 {chapter_number} 章的质量评审没能给出结论：{error}",
            partial_scenes=list(self._latest_scenes),
            chapter_number=chapter_number,
            snapshot=snapshot,
            verdict_unavailable=True,
        )

    def run(
        self,
        chapter_number: int,
        plan_content: str,
        parameters: Dict[str, Any],
        lore: str,
        generate_scene: Callable[..., str],
        on_plan_revised: Optional[Callable[[str], None]] = None,
    ) -> ChapterLoopResult:
        self._latest_scenes = []
        try:
            return self._run(
                chapter_number,
                plan_content,
                parameters,
                lore,
                generate_scene,
                on_plan_revised,
            )
        except DomainReviewError as error:
            # 章节闸门那一处自己带着完整现场兜住了；能落到这里的是规划评审与
            # 场景评审失灵，那时整章还没写完，交出去的只有半稿。
            raise self._verdict_unavailable(chapter_number, error) from error

    def _run(
        self,
        chapter_number: int,
        plan_content: str,
        parameters: Dict[str, Any],
        lore: str,
        generate_scene: Callable[..., str],
        on_plan_revised: Optional[Callable[[str], None]] = None,
    ) -> ChapterLoopResult:
        self.bind_generation_context(
            parameters, lore, generate_scene, on_plan_revised
        )
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
                # 衔接字段在这里也要求非空：正文马上就要照它写第一场，缺了它这一章
                # 只能凭空开头。契约不合格就退回场景规划重做，那时改还便宜。
                contract = validate_planning_contract(
                    contract,
                    chapter_number,
                    require_origin=True,
                    require_continuity=True,
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
        self._latest_scenes = generated_scenes
        previous_chapter_tail = self._load_previous_chapter_tail(chapter_number)
        continuity_rules = self._continuity_rules(chapter_number, contract)

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
                # 只有第一场接的是上一章；后面几场接的是上一场。
                continuity_rules=continuity_rules if index == 1 else (),
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
            # 这一场自己的对话：几轮重修共用一段，模型看得见它上一稿改了什么。
            dialogue = SceneDialogue() if self._scene_dialogue else None
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
                    dialogue=dialogue,
                    continuity_rules=continuity_rules if index == 1 else (),
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
                        review=review.to_dict(),
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

        # 整章已经写完，从这里往后每一次评审失灵都会赔掉一整章的调用。评审判不
        # 出结论时按待复审交出去，连同现场一起——作者重跑一次评审就能接着走，不必
        # 从头重写。
        chapter_review = None
        try:
            chapter_review = self._review_complete_chapter(
                chapter_content,
                contract,
                case_bible,
                suspense_ledger,
                previous_chapter_tail,
                chapter_number=chapter_number,
            )
            # 标注要赶在存盘之前：作者事后翻账本时，该回规划改哪一句必须在记录里。
            self._plan_faults(chapter_review, scenes)
            self.ledger.save_review(chapter_number, "chapter", chapter_review.to_dict())

            (
                generated_scenes,
                scene_reviews,
                chapter_review,
                retry_count,
            ) = self._repair_chapter(
                chapter_number,
                scenes,
                generated_scenes,
                scene_reviews,
                chapter_review,
                contract,
                case_bible,
                suspense_ledger,
                previous_chapter_tail,
                retry_count,
            )
        except DomainReviewError as error:
            raise self._verdict_unavailable(
                chapter_number,
                error,
                snapshot=snapshot_of(
                    ChapterLoopResult(
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
                ),
            ) from error
        narrative_report = analyze_narrative_quality(
            "\n\n---\n\n".join(generated_scenes)
        ).to_dict()

        # 先把现场成型再判定：闸门拦下时要连同这份现场一起交出去，作者才可能在
        # 界面上直接重修或放行，而不是只拿到一句话和一个归档路径。
        result = ChapterLoopResult(
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
                    review=(
                        chapter_review if not chapter_review.passed else blocked[0]
                    ).to_dict(),
                    snapshot=snapshot_of(result),
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
            result.chapter_review = chapter_review
            result.scene_reviews = scene_reviews

        return result

    # ------------------------------------------------------------ 复审出口
    def revise_pending(
        self,
        chapter_number: int,
        snapshot: Dict[str, Any],
        parameters: Dict[str, Any],
        asks: Optional[List[str]] = None,
    ) -> ChapterLoopResult:
        """从待复审现场接着做定向重修，改哪几条由作者说了算。

        走的是闸门自己那条定向修订路：改 repair_scope 指到的那一场，再整章复评。
        自动重试与这一次的区别只有依据来自哪里——自动重试拿评审全量的 asks，这里
        拿作者勾选后剩下的那几条。案情圣经、悬念账本、上一章结尾都从账本重新读，
        所以现场快照只需存正文、契约和评审。

        上一轮评审自己没出结论时，这条路就是「重跑评审」：先给这一稿补上判定，
        通过就直接交付，不通过再照新判定重修。
        """
        result = restore_result(snapshot)
        if not result.scenes:
            raise QualityGateError(
                f"第 {chapter_number} 章没有可重修的现场，请重写本章",
                chapter_number=chapter_number,
            )
        self._check_cancelled()
        self.ledger.initialize(parameters)
        self._bind_profile(parameters, resolve_quality_loop_mode(parameters))
        self._latest_scenes = list(result.scenes)
        scenes = parse_scene_sections(result.plan_content)
        if len(scenes) != len(result.scenes):
            raise QualityGateError(
                f"第 {chapter_number} 章的场景规划已经改过，无法与旧稿逐场对应，请重写本章",
                partial_scenes=result.scenes,
                chapter_number=chapter_number,
            )
        try:
            return self._revise_pending(
                chapter_number, result, scenes, parameters, asks
            )
        except DomainReviewError as error:
            raise self._verdict_unavailable(
                chapter_number, error, snapshot=snapshot_of(result)
            ) from error

    def _revise_pending(
        self,
        chapter_number: int,
        result: ChapterLoopResult,
        scenes: List[str],
        parameters: Dict[str, Any],
        asks: Optional[List[str]] = None,
    ) -> ChapterLoopResult:
        if result.chapter_review is None:
            result.chapter_review = self._review_complete_chapter(
                result.chapter_content,
                result.contract,
                self.ledger.load_case_bible(),
                self.ledger.load_suspense_ledger(),
                self._load_previous_chapter_tail(chapter_number),
                chapter_number=chapter_number,
            )
            self.ledger.save_review(
                chapter_number, "chapter_rerun", result.chapter_review.to_dict()
            )
            if result.chapter_review.passed and all(
                review.passed for review in result.scene_reviews
            ):
                result.base_revision = self.ledger.current_revision()
                return result
        carried = [ask for ask in (asks or []) if str(ask).strip()]
        # 作者只勾了出在规划里的那几条时，重修拿不到任何能在正文里改的东西。
        # 这时改一场正文既改不到点上又要花两次调用，不如直接说清楚该去哪儿改。
        if carried:
            plan_faults = self._plan_faults(result.chapter_review, scenes)
            if plan_faults and not self._without_plan_faults(carried, plan_faults):
                raise QualityGateError(
                    f"第 {chapter_number} 章：勾选的条目全部出在场景规划里，"
                    "重写正文改不掉。\n" + describe_plan_faults(plan_faults),
                    chapter_number=chapter_number,
                    review=result.chapter_review.to_dict(),
                    snapshot=snapshot_of(result),
                )
        (
            revised_scenes,
            scene_reviews,
            chapter_review,
            retry_count,
        ) = self._repair_chapter(
            chapter_number,
            scenes,
            list(result.scenes),
            list(result.scene_reviews),
            result.chapter_review,
            result.contract,
            self.ledger.load_case_bible(),
            self.ledger.load_suspense_ledger(),
            self._load_previous_chapter_tail(chapter_number),
            result.retry_count,
            carried_asks=carried or None,
        )
        result.scenes = revised_scenes
        result.scene_reviews = scene_reviews
        result.chapter_review = chapter_review
        result.retry_count = retry_count
        # 账本基线可能在这一章卡住期间被别的章节推进过，重新取一次，否则验收
        # 会因为基线过期而走一轮本可避免的 rebase 修复。
        result.base_revision = self.ledger.current_revision()
        result.narrative_report = analyze_narrative_quality(
            result.chapter_content
        ).to_dict()

        blocked = [
            review
            for review in (chapter_review, *scene_reviews)
            if not review.passed
        ]
        if blocked:
            raise QualityGateError(
                f"第 {chapter_number} 章重修后仍未通过质量检查",
                partial_scenes=result.scenes,
                chapter_number=chapter_number,
                review=(
                    chapter_review if not chapter_review.passed else blocked[0]
                ).to_dict(),
                snapshot=snapshot_of(result),
            )
        return result

    def accept_waived(
        self,
        chapter_number: int,
        result: ChapterLoopResult,
        reason: str = "",
        chapter_path: Optional[str] = None,
    ) -> ChapterAcceptanceResult:
        """作者看过问题后决定收下这一稿。

        放行只改评分判定，不跳过验收：正文照样走账本提交，前后矛盾与契约冲突
        仍会挡下来。放行理由写进评审记录，事后查得出这一章是被谁放过去的。
        """
        note = f"作者人工放行：{reason}" if reason.strip() else "作者人工放行"
        if result.chapter_review is None:
            # 评审没能给出结论的那一稿：放行就是作者替它签字。记下来，事后查得出
            # 这一章根本没被评审判定过，而不是判过之后被放过去的。
            result.chapter_review = DomainReview(
                stage="chapter",
                passed=True,
                waived=True,
                reviewer_warning=f"{note}（质量评审未能给出结论）",
            )
        elif not result.chapter_review.passed:
            result.chapter_review = result.chapter_review.waive(note)
        result.scene_reviews = [
            review if review.passed else review.waive(note)
            for review in result.scene_reviews
        ]
        result.gate_waivers = [*result.gate_waivers, note]
        self.logger.warning("Chapter %s waived by author: %s", chapter_number, note)
        if result.chapter_review is not None:
            self.ledger.save_review(
                chapter_number, "chapter_waived_by_author", result.chapter_review.to_dict()
            )
        return self.accept_result(chapter_number, result, chapter_path=chapter_path)

    # Repair routes, most authoritative first.  A human ruling outranks every
    # automatic fix, and a stale base revision has to be refreshed before any
    # other repair is even meaningful.
    _REPAIR_ORDER = ("human_decision", "rebase", "contract", "prose")

    #: 修复条目里引用原文的写法：硬伤是「原文：「…」」，加分项是「原文「…」缺少…」，
    #: 评审自己写的修复指令也惯用同一对括号。
    _QUOTE_IN_ASK = re.compile(r"「([^」]{4,})」")

    @classmethod
    def _scene_of_ask(
        cls,
        ask: str,
        generated_scenes: List[str],
        fallback: int,
    ) -> int:
        """这一条要改的东西在第几场：按它引用的原文去正文里找。"""
        for quote in cls._QUOTE_IN_ASK.findall(ask or ""):
            fragment = quote.strip().splitlines()[0].strip()
            if len(fragment) < 6:
                continue  # 太短的片段会在全章命中一堆无关位置
            for index, scene in enumerate(generated_scenes, start=1):
                if fragment in scene:
                    return index
        return fallback

    @classmethod
    def _asks_by_scene(
        cls,
        asks: List[str],
        generated_scenes: List[str],
        fallback: int,
    ) -> Dict[int, List[str]]:
        """把修复清单按「这条说的是哪一场」分派下去。

        评审给的 repair_scope 只写一个场号，但它开出的条目常常散落在整章：某章
        十三条带原文引用的条目里，五条在第一场、一条在第二场、七条在第三场，而
        repair_scope 只写了第三场。照它只重写一场，另外六条就成了发给模型却没有
        对应正文的要求——提示词里还写着「逐条对应，不要遗漏」，模型要么忽略它们，
        要么把别场的内容拽进这一场。

        找不到出处的条目归给 repair_scope 指的那一场：那是评审自己的判断，没有更
        好的依据时听它的。
        """
        grouped: Dict[int, List[str]] = {}
        for ask in asks:
            scene = cls._scene_of_ask(ask, generated_scenes, fallback)
            grouped.setdefault(scene, []).append(ask)
        return dict(sorted(grouped.items()))

    def _repair_chapter(
        self,
        chapter_number: int,
        scenes: List[str],
        generated_scenes: List[str],
        scene_reviews: List[DomainReview],
        chapter_review: DomainReview,
        contract: Dict[str, Any],
        case_bible: Dict[str, Any],
        suspense_ledger: Dict[str, Any],
        previous_chapter_tail: str,
        retry_count: int = 0,
        carried_asks: Optional[List[str]] = None,
    ) -> Tuple[List[str], List[DomainReview], DomainReview, int]:
        """定向重修：一轮里把有问题的每一场都改到，然后整章复评。

        修复清单先按条目引用的原文分派到各场，哪几场有条目就改哪几场，每一场只
        收到属于它自己的那几条。改完的每一场和重新拼起来的整章都要再过一遍评审。

        闸门拦下之后自动跑一遍，作者在「待复审」里点「照建议重修」时再跑一遍——
        两条路走同一段代码，区别只有 `carried_asks`：作者划掉的条目不进提示词，
        且只作用于第一轮，之后仍以新评审自己提出的改动为准。
        """
        repair_review = chapter_review
        repaired_scene_passed = True
        # 每一场自己的对话，跨轮沿用：第二轮改同一场时，模型手上还有它第一轮
        # 改过什么、当时被要求了什么。
        dialogues: Dict[int, SceneDialogue] = {}
        # 评审中途失灵时要交出改到一半的这一稿，而不是最初那一稿。
        self._latest_scenes = generated_scenes
        # 章节级重修就地覆盖某一场，而重修不保证变好。留一份当前最好的整章快照，
        # 放弃时交出它，别让一次变差的定向修订把已经更好的稿子顶掉。
        best_scenes = list(generated_scenes)
        best_scene_reviews = list(scene_reviews)
        best_chapter_review = chapter_review
        unmet_asks = []
        # 上一轮已经报过的规划缺陷引文。第一轮为空，所以规划缺陷一定先得到一次
        # 重修机会——引文照搬了规划不等于正文没救，直接全撤实测更糟。
        reported_fault_quotes: set[str] = set()
        for attempt in range(self.max_scene_retries):
            if chapter_review.passed and repaired_scene_passed:
                break
            # 每一轮重修之前是安全点：这一章尚未验收，继续下去要再花约十次调用。
            # 停在这里丢掉的和「下一场之前」停下丢掉的是同一类东西——一份没通过
            # 闸门、本来也不会落盘的稿子。评审本身仍然不设检查点：那时整章的生成
            # 成本已经付出，半路停下等于白扔一章。
            self._check_cancelled()
            fallback = self._target_scene(
                repair_review.repair_scope, len(generated_scenes)
            )
            retry_count += 1
            requested = carried_asks or repair_review.asks
            carried_asks = None
            # 出在规划里的那几条，先给重修一次机会再撤：引文照搬了规划不等于正文
            # 没救——规划那句话往某个方向推，正文往往仍有回旋余地。实测直接全撤会
            # 让重修手上什么都不剩，同样的问题原样留到最后，整章掉进待复审。
            # 同一句引文被重新报出来，才算证明正文确实改不动。
            detected_faults = self._plan_faults(repair_review, scenes)
            plan_faults = [
                fault
                for fault in detected_faults
                if str(fault.get("quote", "")).strip() in reported_fault_quotes
            ]
            requested = self._without_plan_faults(requested, plan_faults)
            # 引文是稳定的标识：正文那句话没改，下一轮评审还会引同一句。整条修复
            # 说明不行——评审每轮都会把同一个问题换个说法重写，逐字比对永远匹配
            # 不上（四个副本的轨迹里「仍未解决」标记一次都没出现过）。
            reported_fault_quotes = {
                str(fault.get("quote", "")).strip()
                for fault in detected_faults
                if str(fault.get("quote", "")).strip()
            }
            # 评审什么都没开出来时保留老行为：改 repair_scope 指的那一场，改写依据
            # 退回评审给的大方向。
            grouped = self._asks_by_scene(requested, generated_scenes, fallback) or {
                fallback: []
            }

            repaired_reviews: List[DomainReview] = []
            # 升序逐场：后一场要拿前一场改完之后的结尾去接，倒着改会接到旧文。
            for target, scene_asks in grouped.items():
                # 修第一场时同样要带上一章结尾，否则重写出来的开头会与上一章脱节。
                previous_tail = (
                    generated_scenes[target - 2][-2500:]
                    if target > 1
                    else previous_chapter_tail
                )
                next_scene_plan = scenes[target] if target < len(scenes) else ""
                generated_scenes[target - 1] = self.reviewer.revise_scene(
                    generated_scenes[target - 1],
                    repair_review,
                    scenes[target - 1],
                    previous_tail,
                    next_scene_plan,
                    contract,
                    unmet_asks=[ask for ask in unmet_asks if ask in scene_asks],
                    dialogue=(
                        dialogues.setdefault(target, SceneDialogue())
                        if self._scene_dialogue
                        else None
                    ),
                    # 改写依据必须和复评依据是同一份，而且只能是这一场自己的那
                    # 几条：把整章的清单发过来，模型手上却只有这一场的正文。
                    asks=scene_asks,
                    # 重写第一场时把开场的衔接要求一起带上，否则刚接上的开头会在
                    # 定向重修里被改回重新布景那一版。
                    continuity_rules=(
                        self._continuity_rules(chapter_number, contract)
                        if target == 1
                        else ()
                    ),
                )
                if not generated_scenes[target - 1].strip():
                    raise QualityGateError(
                        f"第 {chapter_number} 章场景 {target} 的章节级修订正文为空"
                    )

                scene_review = self.reviewer.review_scene(
                    generated_scenes[target - 1],
                    scenes[target - 1],
                    target,
                    previous_tail,
                    next_scene_plan,
                    contract,
                    case_bible,
                    suspense_ledger,
                    repairs_requested=scene_asks,
                )
                scene_reviews[target - 1] = scene_review
                repaired_reviews.append(scene_review)
                self.ledger.save_review(
                    chapter_number,
                    f"chapter_retry_{attempt + 1}_scene_{target}",
                    scene_review.to_dict(),
                )

            repaired_scene_passed = all(
                review.passed for review in repaired_reviews
            )
            chapter_content = "\n\n---\n\n".join(generated_scenes)
            chapter_review = self._review_complete_chapter(
                chapter_content,
                contract,
                case_bible,
                suspense_ledger,
                previous_chapter_tail,
                repairs_requested=requested,
                chapter_number=chapter_number,
            )
            self._plan_faults(chapter_review, scenes)
            self.ledger.save_review(
                chapter_number,
                f"chapter_retry_{attempt + 1}",
                chapter_review.to_dict(),
            )
            # 还有场次没过就以它为准：整章评审看不见「这一场自己还不成立」。
            failed_scene = next(
                (review for review in repaired_reviews if not review.passed), None
            )
            repair_review = failed_scene or chapter_review
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

        return best_scenes, best_scene_reviews, best_chapter_review, retry_count

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

        验收里的修复会重写正文并重新评审，所以这里同样可能撞上评审失灵；那时把
        这一稿交回待复审，不要让一整章连同验收修复的成果一起丢掉。
        """
        self._latest_scenes = list(result.scenes)
        try:
            return self._accept_result(chapter_number, result, chapter_path)
        except DomainReviewError as error:
            raise self._verdict_unavailable(
                chapter_number, error, snapshot=snapshot_of(result)
            ) from error

    def _accept_result(
        self,
        chapter_number: int,
        result: ChapterLoopResult,
        chapter_path: Optional[str] = None,
    ) -> ChapterAcceptanceResult:
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
            chapter_number=chapter_number,
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

    def _load_previous_chapter_content(self, chapter_number: int) -> str:
        """上一章的完整正文，读不到就返回空串。

        与 `_load_previous_chapter_tail` 分开：那一份是喂给写作和评审的上下文，
        缺了它这一章根本没法接着写，所以它该报错；这一份只用来统计两章开头之间
        的套语重合，读不到时不做这项统计即可，不该让整章停下来。
        """
        if int(chapter_number) <= 1:
            return ""
        filename = f"chapter_{int(chapter_number) - 1}.md"
        for candidate in (
            os.path.join(self.output_dir, "story", "content", "chapters", filename),
            os.path.join(self.output_dir, "chapters", filename),
        ):
            if not os.path.isfile(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as handle:
                    return handle.read()
            except (OSError, UnicodeError) as exc:
                self.logger.warning(
                    "Could not read previous chapter from %s: %s", candidate, exc
                )
        return ""

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
