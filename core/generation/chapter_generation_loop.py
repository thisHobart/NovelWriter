"""Design-generation-review loop for legal suspense chapters."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from agents.review.legal_suspense_review_agent import DomainReview, LegalSuspenseReviewAgent
from core.generation.chapter_acceptance import (
    ChapterAcceptanceResult,
    ChapterAcceptanceService,
)
from core.generation.helper_fns import parse_scene_sections
from core.generation.story_ledger import StoryLedgerManager


class QualityGateError(RuntimeError):
    """Raised when content still fails after the bounded retry loop."""


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
        max_plan_retries: int = 2,
        max_scene_retries: int = 2,
        reviewer: Optional[LegalSuspenseReviewAgent] = None,
        acceptance_service: Optional[ChapterAcceptanceService] = None,
    ):
        self.output_dir = output_dir
        self.model = model
        self.logger = logger or logging.getLogger("chapter-generation-loop")
        self.max_plan_retries = max_plan_retries
        self.max_scene_retries = max_scene_retries
        self.ledger = StoryLedgerManager(output_dir)
        self.reviewer = reviewer or LegalSuspenseReviewAgent(model=model, logger=self.logger)
        self.acceptance_service = acceptance_service or ChapterAcceptanceService(self.ledger)

    def run(
        self,
        chapter_number: int,
        plan_content: str,
        parameters: Dict[str, Any],
        lore: str,
        generate_scene: Callable[..., str],
        on_plan_revised: Optional[Callable[[str], None]] = None,
    ) -> ChapterLoopResult:
        self.ledger.initialize(parameters)
        base_revision = self.ledger.current_revision()
        case_bible = self.ledger.load_case_bible()
        design_context = self.ledger.load_design_context()
        if self.ledger.case_bible_needs_refresh(case_bible, design_context):
            self.logger.info("Building case bible from whole-story structure and chapter outlines")
            case_bible = self.reviewer.build_case_bible(
                parameters,
                lore,
                design_context,
                case_bible,
            )
            case_bible = self.ledger.save_case_bible(case_bible, design_context)
        suspense_ledger = self.ledger.load_suspense_ledger()

        current_plan = plan_content
        plan_revised = False
        retry_count = 0
        contract = self.ledger.load_contract(chapter_number, current_plan)
        if contract is None:
            contract = self.reviewer.build_chapter_contract(
                chapter_number,
                current_plan,
                parameters,
                lore,
                case_bible,
                suspense_ledger,
            )

        plan_review = self.reviewer.review_plan(current_plan, contract, case_bible, suspense_ledger)
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
                raise QualityGateError("场景规划修订结果没有可解析的场景标题")
            current_plan = revised_plan
            plan_revised = True
            contract = self.reviewer.build_chapter_contract(
                chapter_number,
                current_plan,
                parameters,
                lore,
                case_bible,
                suspense_ledger,
            )
            plan_review = self.reviewer.review_plan(current_plan, contract, case_bible, suspense_ledger)
            self.ledger.save_review(chapter_number, f"plan_retry_{attempt + 1}", plan_review.to_dict())

        if not plan_review.passed:
            raise QualityGateError(
                f"第 {chapter_number} 章场景规划在 {self.max_plan_retries} 次修订后仍未通过法律悬疑质量检查"
            )

        if plan_revised and on_plan_revised:
            on_plan_revised(current_plan)

        contract = self.ledger.save_contract(chapter_number, contract, current_plan)
        scenes = parse_scene_sections(current_plan)
        if not scenes:
            raise QualityGateError("场景规划中没有可生成的场景")

        generated_scenes: List[str] = []
        scene_reviews: List[DomainReview] = []
        previous_chapter_tail = self._load_previous_chapter_tail(chapter_number)

        for index, scene_plan in enumerate(scenes, start=1):
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
            )
            if not prose or not prose.strip():
                raise QualityGateError(f"第 {chapter_number} 章场景 {index} 的正文为空")
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
                        f"第 {chapter_number} 章场景 {index} 的修订正文为空"
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
                    f"第 {chapter_number} 章场景 {index} 在 {self.max_scene_retries} 次修订后仍未通过质量检查"
                )
            generated_scenes.append(prose)
            scene_reviews.append(review)

        chapter_content = "\n\n---\n\n".join(generated_scenes)
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
            previous_tail = generated_scenes[target - 2][-2500:] if target > 1 else ""
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
                f"第 {chapter_number} 章在定向修订后仍未通过章节级质量检查，请人工审核"
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
        return self.acceptance_service.accept(
            chapter_number,
            result.chapter_content,
            result.contract,
            result.chapter_review.to_dict(),
            result.base_revision,
            resolved_path,
        )

    def _load_previous_chapter_tail(self, chapter_number: int) -> str:
        if chapter_number <= 1:
            return ""
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
                    return handle.read()[-2500:]
            except (OSError, UnicodeError) as exc:
                self.logger.warning("Could not read previous chapter tail from %s: %s", path, exc)
        return ""

    @staticmethod
    def _target_scene(repair_scope: str, scene_count: int) -> int:
        match = re.search(r"(?:scene|场景)[_\s-]*(\d+)", repair_scope or "", re.IGNORECASE)
        if match:
            return max(1, min(scene_count, int(match.group(1))))
        return scene_count
