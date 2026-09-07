# -*- coding: utf-8 -*-
"""Framework-independent scene-planning pipeline.

This module contains business generation only: no widgets, event loops, or
message boxes.  Inputs arrive through :class:`StageContext` and output remains
compatible with existing NovelWriter workspaces.
"""
from __future__ import annotations

from core.generation.errors import fail
from core.generation.ai_helper import send_prompt, get_backend
import json
from glob import glob
from core.generation.helper_fns import (
    open_file,
    parse_chapter_numbers,
    parse_scene_sections,
    resolve_section_chapter_numbers,
    save_prompt_to_file,
    write_file,
)
from core.generation.prompt_context import (
    build_location_guidance,
    build_story_parameter_lines,
    find_scene_world_conflicts,
    format_genre_label,
    normalize_story_parameters,
    sanitize_lore_content,
)
from core.generation.planning_contract import (
    CONTRACT_SCHEMA_VERSION,
    PlanningContractError,
    build_existing_planning_index,
    collect_history_defects,
    contract_output_instructions,
    downstream_obligations,
    extract_scene_plan_contract,
    load_planning_contracts,
    validate_contract_sequence,
    validate_planning_contract,
)
from core.generation.chapter_continuity import (
    CHAPTER_FUNCTIONS,
    CONTINUITY_KEY,
    chapter_function_defects,
    continuity_defects,
    continuity_instructions,
    continuity_schema_block,
    established_context,
    neglected_threads,
)
from core.generation.narrative_graph import NarrativeGraphManager
from core.generation.story_ledger import StoryLedgerManager
from core.generation.semantic_identity import (
    has_domain_id_collision,
    resolve_contract_identities,
    resolve_domain_identities,
)
from agents.review.domain_review_agent import DomainReviewAgent, DomainReviewError
from core.generation.domain_profiles import (
    QUALITY_LOOP_OFF,
    apply_quality_loop_mode,
    resolve_domain_profile,
    resolve_quality_loop_mode,
)
import os
import re
import logging
from core.config.story_options import STRUCTURE_SECTIONS_MAP
from core.localization import zh_label
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


class ScenePipeline:
    planning_retry_limit = 2

    def __init__(self, app) -> None:
        self.app = app
        self.cancel_token = CancelToken()
        self.dir_manager = get_directory_manager(app.get_output_dir(), use_new_structure=True)

    def _get_directory_manager(self, output_dir=None):
        """Return the directory manager for the current structured workspace."""
        if output_dir is None:
            output_dir = self.app.get_output_dir()
        return get_directory_manager(output_dir, use_new_structure=True)

    @staticmethod
    def _has_usable_scene_plan(scene_plan_path, output_dir=None, chapter_number=None):
        """Return True only for an existing, non-empty, parseable scene plan."""
        if not os.path.isfile(scene_plan_path):
            return False
        try:
            with open(scene_plan_path, "r", encoding="utf-8") as scene_file:
                content = scene_file.read()
        except (OSError, UnicodeError):
            return False
        if not parse_scene_sections(content):
            return False
        if output_dir is None or chapter_number is None:
            return True
        manager = StoryLedgerManager(output_dir)
        contract = manager.load_contract(chapter_number, content)
        if contract is None:
            return False
        # 已验收章节的规划不再重生成：它对应的正文已经落盘并记进账本，重规划只会
        # 让两者对不上。只有还没写的章节才要求补齐衔接字段——那正是重规划还便宜、
        # 也还有意义的时候。
        accepted = {
            int(item.get("chapter"))
            for item in manager.load_suspense_ledger().get("accepted_chapters", [])
            if isinstance(item, dict) and item.get("chapter")
        }
        try:
            validate_planning_contract(
                contract,
                chapter_number,
                require_origin=True,
                require_continuity=int(chapter_number) not in accepted,
            )
        except PlanningContractError:
            return False
        return True

    @staticmethod
    def _continuity_context(output_dir, chapter_number):
        """第 N 章规划时，关于「从第 N-1 章什么状态接过来」的全部已知事实。

        全部来自已经落盘的契约与正文，不发起任何调用。
        """
        chapter_number = int(chapter_number)
        if chapter_number <= 1:
            return {}
        contracts = load_planning_contracts(output_dir)
        prior = [item for item in contracts if int(item["chapter"]) < chapter_number]
        previous = next(
            (item for item in prior if int(item["chapter"]) == chapter_number - 1), None
        )
        events = (previous or {}).get("timeline_events", []) or []
        tail = ""
        for folder in ("story/content/chapters", "chapters"):
            candidate = os.path.join(
                output_dir, *folder.split("/"), f"chapter_{chapter_number - 1}.md"
            )
            if os.path.isfile(candidate):
                try:
                    with open(candidate, "r", encoding="utf-8") as handle:
                        tail = handle.read()[-2000:]
                except (OSError, UnicodeError):
                    tail = ""
                break
        return {
            "previous_tail": tail,
            "established": established_context(prior, chapter_number),
            "previous_last_event": events[-1] if events else None,
            "neglected": neglected_threads(prior, chapter_number),
        }

    @staticmethod
    def _contract_instructions(output_dir, chapter_number, story_params):
        index = build_existing_planning_index(output_dir, chapter_number)
        graph_manager = NarrativeGraphManager(output_dir)
        ledger_manager = StoryLedgerManager(output_dir)
        ledger_manager.initialize(story_params)
        narrative_context = graph_manager.planning_context(
            chapter_number, ledger_manager.load_suspense_ledger()
        )
        profile = resolve_domain_profile(story_params)
        domain_fields = {
            field.name: field.schema_hint for field in profile.contract_fields
        }
        return contract_output_instructions(
            chapter_number,
            index,
            domain_fields=domain_fields,
            obligations=downstream_obligations(output_dir, chapter_number),
            narrative_context=narrative_context,
            continuity_context=ScenePipeline._continuity_context(
                output_dir, chapter_number
            ),
        )

    @staticmethod
    def _save_scene_plan_and_contract(
        output_dir,
        scene_plan_path,
        response,
        chapter_number,
        *,
        scene_markdown=None,
        contract=None,
    ):
        # Callers that already validated and semantically normalised the
        # contract must be able to save that exact object.  Re-extracting the
        # raw LLM response here would silently discard the corrections.
        if scene_markdown is None or contract is None:
            scene_markdown, contract = extract_scene_plan_contract(response, chapter_number)
        write_file(scene_plan_path, scene_markdown)
        StoryLedgerManager(output_dir).save_contract(chapter_number, contract, scene_markdown)
        return scene_markdown

    @staticmethod
    def _defect_signature(defects):
        return tuple(sorted((getattr(d, "code", ""), str(d)) for d in defects))

    def _validate_planning_draft(
        self,
        response,
        chapter_number,
        lore_content,
        story_params,
        output_dir,
        selected_model,
        require_complete_sequence,
    ):
        """Check one draft and report **every** defect it can, not just the first.

        Returns ``(result, defects)``; ``result`` is the accepted triple when the
        draft is clean. Schema errors stop the pass because the later checks need
        a normalised contract, but a contract defect and a world-building
        conflict are independent — reporting them one release at a time wastes a
        retry per defect and lets the model reintroduce what it just fixed.
        """
        try:
            scene_markdown, contract = extract_scene_plan_contract(response, chapter_number)
        except PlanningContractError as exc:
            return None, [exc]

        project_dir = output_dir or getattr(self.app, "output_dir", None)
        defects = []
        if project_dir:
            resolution = resolve_contract_identities(
                contract,
                project_dir,
                chapter_number,
                selected_model,
                send_prompt,
                profile=resolve_domain_profile(story_params),
            )
            try:
                contract = validate_planning_contract(
                    resolution.contract, chapter_number, require_continuity=True
                )
            except PlanningContractError as exc:
                return None, [exc]
            contract["schema_version"] = CONTRACT_SCHEMA_VERSION
            contract["origin"] = "scene_planning"
            for warning in resolution.warnings:
                self.app.logger.warning(
                    "Chapter %s semantic contract warning: %s", chapter_number, warning
                )
            # Cross-chapter rules run here, not only in the final sequence gate,
            # so a dangling reference is reported to the model that made it while
            # it can still be retried.
            defects.extend(
                collect_history_defects(
                    contract,
                    load_planning_contracts(project_dir),
                    chapter_number,
                    obligations=downstream_obligations(project_dir, chapter_number),
                )
            )
            graph_manager = NarrativeGraphManager(project_dir)
            ledger_manager = StoryLedgerManager(project_dir)
            ledger_manager.initialize(story_params)
            contract["narrative_graph_revision"] = graph_manager.current_revision()
            graph_issues = graph_manager.validate_contract(
                contract,
                ledger_manager.load_suspense_ledger(),
                ignore_revision=True,
            )
            defects.extend(
                PlanningContractError(
                    str(issue.get("message", "叙事图契约校验失败")),
                    chapters=(chapter_number,),
                    code=str(issue.get("code", "narrative_graph_contract_invalid")).lower(),
                )
                for issue in graph_issues
                if issue.get("severity") == "error"
            )

        if require_complete_sequence:
            try:
                validate_contract_sequence([contract], total_chapters=chapter_number)
            except PlanningContractError as exc:
                defects.append(exc)

        conflicts = find_scene_world_conflicts(scene_markdown, lore_content, story_params)
        if conflicts:
            defects.append(
                PlanningContractError(
                    "包含世界观未定义的内容：" + "、".join(conflicts),
                    chapters=(chapter_number,),
                    code="world_conflict",
                )
            )

        if defects:
            return None, defects
        return (response, scene_markdown, contract), []

    @staticmethod
    def _repair_instructions(rejected_draft, defects):
        """Give the model its own rejected draft plus every problem found so far.

        Without the draft, "只修复下面指出的问题" is unfollowable: the model has
        nothing to repair and re-rolls the chapter from scratch, losing whatever
        it got right. Without the accumulated list it fixes the newest complaint
        and reintroduces the previous one.
        """
        lines = [
            "\n\n上一次结果未通过前置规划验收。下面是你上一稿的原文，"
            "请在它的基础上只修复列出的问题，保留其余内容，"
            "不要改变章节大纲中的核心事件：",
            "\n### 你的上一稿（需要修订的原文）：",
            rejected_draft.strip()[:12000],
            "\n### 必须全部修复的问题（含此前几次指出过的）：",
        ]
        lines.extend(f"- {message}" for message in defects)
        lines.append(
            "\n请输出完整的修订版场景规划与契约，不要只输出改动片段。"
        )
        return "\n".join(lines)

    def _archive_planning_attempt(
        self, output_dir, chapter_number, attempt, prompt, response, defects
    ):
        """Keep every rejected draft so the retry loop can be inspected later.

        Only the accepted plan is saved into the project; without this the
        question "did the feedback actually improve the output?" has no evidence
        behind it at all.
        """
        if not output_dir:
            return
        try:
            archive_dir = os.path.join(
                output_dir, "archive", "planning_retries", f"chapter_{chapter_number}"
            )
            os.makedirs(archive_dir, exist_ok=True)
            stamp = f"attempt_{attempt + 1}"
            write_file(
                os.path.join(archive_dir, f"{stamp}_prompt.md"), prompt or ""
            )
            write_file(
                os.path.join(archive_dir, f"{stamp}_response.md"), response or "（空响应）"
            )
            write_file(
                os.path.join(archive_dir, f"{stamp}_defects.json"),
                json.dumps(
                    [
                        {"code": getattr(d, "code", ""), "message": str(d)}
                        for d in defects
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        except OSError as exc:
            self.app.logger.warning(
                "Could not archive planning attempt for Chapter %s: %s", chapter_number, exc
            )

    def _generate_valid_scene_response(
        self,
        prompt,
        selected_model,
        chapter_number,
        lore_content,
        story_params,
        require_complete_sequence=False,
        output_dir=None,
    ):
        """Generate one scene plan, feeding each rejected draft back for repair."""
        seen_defects = []
        rejected_draft = ""
        previous_signature = None
        last_error = None

        for attempt in range(self.planning_retry_limit + 1):
            retry_prompt = prompt
            if seen_defects:
                retry_prompt += self._repair_instructions(rejected_draft, seen_defects)

            response = send_prompt(retry_prompt, model=selected_model)
            if not response or not response.strip():
                result, defects = None, [PlanningContractError("大模型没有返回场景规划")]
            else:
                result, defects = self._validate_planning_draft(
                    response,
                    chapter_number,
                    lore_content,
                    story_params,
                    output_dir,
                    selected_model,
                    require_complete_sequence,
                )
            if result is not None:
                return result

            self._archive_planning_attempt(
                output_dir, chapter_number, attempt, retry_prompt, response, defects
            )
            last_error = defects[0]
            rejected_draft = response or ""
            for defect in defects:
                if all(str(defect) != str(existing) for existing in seen_defects):
                    seen_defects.append(defect)

            signature = self._defect_signature(defects)
            if attempt > 0 and signature == previous_signature:
                # 已经把问题连同原稿交回去了，模型仍然原样重现同一组缺陷：
                # 再试只是重复烧钱，停下来把问题交给人。
                self.app.logger.warning(
                    "Chapter %s planning made no progress on retry %s; stopping early",
                    chapter_number,
                    attempt,
                )
                break
            previous_signature = signature

            if attempt < self.planning_retry_limit:
                self.app.logger.warning(
                    "Chapter %s planning failed validation; retry %s/%s: %s",
                    chapter_number,
                    attempt + 1,
                    self.planning_retry_limit,
                    "；".join(str(defect) for defect in defects),
                )

        summary = "；".join(str(defect) for defect in seen_defects) or str(last_error)
        raise PlanningContractError(
            f"第 {chapter_number} 章场景规划在 {self.planning_retry_limit} 次重试后仍未通过：{summary}",
            chapters=(chapter_number,),
            code=getattr(last_error, "code", "planning_retry_exhausted"),
        )

    def _plan_path_for_chapter(self, output_dir, chapter_number):
        matches = glob(
            os.path.join(
                output_dir, "story", "planning", "**", f"*_ch{chapter_number}.md"
            ),
            recursive=True,
        )
        return matches[0] if matches else None

    def backfill_chapter_continuity(
        self, output_dir, chapter_number, selected_model, story_params
    ):
        """给一份旧契约补上 continuity 与 chapter_function，不重写场景规划。

        这两个字段都是后来才加进契约的。整章重规划当然也能补上，但那会把一份已经
        过了跨章校验、已经被后面章节依赖的规划整个重掷一次，只为拿几个字段；这里
        只问缺的那几个，其余部分一个字不动。

        返回 True 表示补上了，False 表示本来就有。补不上会抛
        `PlanningContractError`，和其它契约缺陷走同一条路。
        """
        chapter_number = int(chapter_number)
        manager = StoryLedgerManager(output_dir)
        scene_plan_path = self._plan_path_for_chapter(output_dir, chapter_number)
        if scene_plan_path is None:
            raise PlanningContractError(
                f"找不到第 {chapter_number} 章的场景规划，无法补衔接字段",
                chapters=(chapter_number,),
                code="chapter_continuity_missing",
            )
        scene_markdown = open_file(scene_plan_path)
        contract = manager.load_contract(chapter_number, scene_markdown)
        if contract is None:
            raise PlanningContractError(
                f"第 {chapter_number} 章没有与场景规划匹配的契约",
                chapters=(chapter_number,),
                code="planning_contract_invalid",
            )
        prior = [
            item
            for item in load_planning_contracts(output_dir)
            if int(item["chapter"]) < chapter_number
        ]

        def defects_of(candidate):
            return [
                *chapter_function_defects(candidate, prior, chapter_number),
                *continuity_defects(candidate, chapter_number),
            ]

        if not defects_of(contract):
            return False

        context = self._continuity_context(output_dir, chapter_number)
        recent = ", ".join(
            "第 {chapter} 章 {function}".format(
                chapter=item["chapter"],
                function=item.get("chapter_function") or "未填",
            )
            for item in prior[-3:]
        )
        base_prompt = f"""请为第 {chapter_number} 章补写机器可读的章节衔接声明与章节功能，不要改动场景规划本身。

本章已经通过验收的场景规划：
{scene_markdown}

{continuity_instructions(chapter_number, **context)}

chapter_function 说明本章在全书中承担什么功能，取值只能是
{"|".join(CHAPTER_FUNCTIONS)}。前面几章的功能是：{recent or "无"}。
连着三章都是同一种功能读者会觉得情节在原地打转，请挑真正贴合本章场景的那一个。

只输出一个 JSON 对象，不要代码围栏、不要任何解释：
{{
  "chapter_function": "{"|".join(CHAPTER_FUNCTIONS)}",
{continuity_schema_block(chapter_number).strip().rstrip(',')}
}}
"""
        prompt = base_prompt
        last_defects = []
        for attempt in range(self.planning_retry_limit + 1):
            response = send_prompt(prompt, model=selected_model)
            try:
                payload = json.loads(
                    re.sub(
                        r"^```(?:json)?|```$",
                        "",
                        (response or "").strip(),
                        flags=re.MULTILINE,
                    ).strip()
                )
            except (TypeError, ValueError):
                payload = {}
            block = payload.get(CONTINUITY_KEY, payload)
            candidate = {**contract, CONTINUITY_KEY: block}
            function = str(payload.get("chapter_function", "")).strip().lower()
            if function:
                candidate["chapter_function"] = function
            last_defects = defects_of(candidate)
            if not last_defects:
                manager.save_contract(chapter_number, candidate, scene_markdown)
                self.app.logger.info(
                    "Backfilled the chapter %s hand-off without rewriting its plan",
                    chapter_number,
                )
                return True
            prompt = base_prompt + "\n上一次的回答不合格：\n" + "\n".join(
                f"- {item}" for item in last_defects
            )
        raise PlanningContractError(
            f"第 {chapter_number} 章的衔接与章节功能补写 {self.planning_retry_limit} "
            "次仍不合格：" + "；".join(last_defects),
            chapters=(chapter_number,),
            code="chapter_continuity_missing",
        )

    def _normalise_repeated_thread(self, output_dir, chapter_number, scene_markdown, selected_model):
        """Repair a duplicated thread ID without touching the creative Markdown.

        Older projects may already contain this structural error.  Identical
        declarations are removed; a reused ID with a different meaning is judged
        semantically and can receive a new program-assigned ID.
        """
        manager = StoryLedgerManager(output_dir)
        current = manager.load_contract(chapter_number, scene_markdown)
        if current is None:
            return False
        resolution = resolve_contract_identities(
            current, output_dir, chapter_number, selected_model, send_prompt
        )
        if resolution.contract == current:
            return False
        manager.save_contract(chapter_number, resolution.contract, scene_markdown)
        self.app.logger.info(
            "Normalised repeated thread ID in Chapter %s without rewriting Markdown",
            chapter_number,
        )
        return True

    def _normalise_existing_domain_collisions(
        self,
        output_dir,
        chapter_number,
        scene_markdown,
        selected_model,
        story_params,
    ):
        """Repair legacy C001/E001-style collisions without rewriting prose."""
        manager = StoryLedgerManager(output_dir)
        current = manager.load_contract(chapter_number, scene_markdown)
        if current is None:
            return False
        profile = resolve_domain_profile(story_params)
        if not has_domain_id_collision(
            current, output_dir, chapter_number, profile=profile
        ):
            return False
        resolution = resolve_domain_identities(
            current,
            output_dir,
            chapter_number,
            selected_model,
            send_prompt,
            profile=profile,
        )
        for warning in resolution.warnings:
            self.app.logger.warning(
                "Chapter %s semantic contract warning: %s", chapter_number, warning
            )
        if resolution.contract == current:
            return False
        manager.save_contract(chapter_number, resolution.contract, scene_markdown)
        self.app.logger.info(
            "Normalised legacy domain IDs in Chapter %s without rewriting Markdown",
            chapter_number,
        )
        return True

    def _validate_sequence_with_retries(
        self,
        output_dir,
        total_chapters,
        selected_model,
        lore_content,
        story_params,
    ):
        """Repair only chapters named by the cross-chapter contract gate.

        The budget is per-defect rather than per-run: a long book can legitimately
        need several unrelated repairs, and a single shared counter used to give
        up while the plan was still improving.  Progress is tracked instead — the
        same failure surviving its own repair means the loop cannot fix it, so it
        stops rather than burning calls.
        """
        budget = self.planning_retry_limit + 1 + int(total_chapters or 0)
        previous_signature = None
        for _ in range(budget):
            try:
                validate_contract_sequence(
                    load_planning_contracts(output_dir),
                    total_chapters=total_chapters,
                )
                return
            except PlanningContractError as exc:
                signature = (exc.code, str(exc))
                if not exc.chapters or signature == previous_signature:
                    raise
                previous_signature = signature
                # Errors name repair targets best-first: for a dangling close the
                # chapter that should have raised the thread comes before the one
                # that closes it, so the payoff scene is not rewritten away.
                candidates = [
                    (number, self._plan_path_for_chapter(output_dir, number))
                    for number in exc.chapters
                ]
                chapter_number, scene_plan_path = next(
                    ((number, path) for number, path in candidates if path),
                    (exc.chapters[0], None),
                )
                if scene_plan_path is None:
                    raise PlanningContractError(
                        f"{exc}；且找不到第 {chapter_number} 章场景规划，无法自动修复",
                        chapters=(chapter_number,),
                        code=exc.code,
                    ) from exc
                scene_markdown = open_file(scene_plan_path)
                if exc.code == "thread_reopened" and self._normalise_repeated_thread(
                    output_dir, chapter_number, scene_markdown, selected_model
                ):
                    previous_signature = None
                    continue
                repair_prompt = f"""请修复第 {chapter_number} 章场景规划，使它通过跨章契约验收。只处理指出的问题，不改变章节大纲中的核心事件和场景数量。

跨章验收错误：
{exc}

当前场景规划：
{scene_markdown}

{self._contract_instructions(output_dir, chapter_number, story_params)}
"""
                response, repaired_markdown, repaired_contract = self._generate_valid_scene_response(
                    repair_prompt,
                    selected_model,
                    chapter_number,
                    lore_content,
                    story_params,
                    output_dir=output_dir,
                )
                self._save_scene_plan_and_contract(
                    output_dir,
                    scene_plan_path,
                    response,
                    chapter_number,
                    scene_markdown=repaired_markdown,
                    contract=repaired_contract,
                )
                self.app.logger.info(
                    "Repaired Chapter %s planning contract after sequence failure (%s)",
                    chapter_number,
                    exc.code,
                )
        validate_contract_sequence(
            load_planning_contracts(output_dir), total_chapters=total_chapters
        )

    @staticmethod
    def _has_usable_chapter_outline(outline_path):
        """True only for an existing outline that declares real chapter headings."""
        if not os.path.isfile(outline_path):
            return False
        try:
            with open(outline_path, "r", encoding="utf-8") as handle:
                content = handle.read()
        except (OSError, UnicodeError):
            return False
        return bool(parse_chapter_numbers(content))

    def _outline_reviewer(self, selected_model, story_params):
        """大纲阶段的评审；质量闭环关掉时返回 None，一次调用都不发。"""
        mode = resolve_quality_loop_mode(story_params)
        if mode == QUALITY_LOOP_OFF:
            return None
        profile = apply_quality_loop_mode(resolve_domain_profile(story_params), mode)
        # 发送器显式传本模块的 send_prompt：评审默认用它自己模块里的那一个，
        # 而这一阶段的测试都打在本模块上——不传就会在测试里真的发起网络调用。
        return DomainReviewAgent(
            model=selected_model,
            profile=profile,
            logger=self.app.logger,
            send_prompt_fn=send_prompt,
        )

    def _generate_valid_outline_response(
        self, prompt, selected_model, section_name, section_content, story_params
    ):
        """Generate one section's chapter outline, retrying with its own draft.

        A world-building conflict used to discard the whole section with no retry
        at all — one stray word and an entire act silently vanished from the book.

        结构检查通过之后还要过一遍大纲评审：大纲里「辩护律师当庭指挥法警抓人」这种
        事，此前一路穿到整章写完才被抓住，而那时重修改不掉——大纲要求这么写。在这
        里拦一次一个结构部分只花一次调用，下游少赔一轮定向重修就回本了。
        """
        reviewer = self._outline_reviewer(selected_model, story_params)
        seen = []
        rejected = ""
        previous = None
        for attempt in range(self.planning_retry_limit + 1):
            retry_prompt = prompt
            if seen:
                retry_prompt += self._repair_instructions(rejected, seen)

            response = send_prompt(retry_prompt, model=selected_model)
            problems = []
            if not response or not response.strip():
                problems.append("大模型没有返回章节大纲")
            else:
                conflicts = find_scene_world_conflicts(
                    response, section_content, story_params
                )
                if conflicts:
                    problems.append(
                        "包含上游设定未定义的科幻内容：" + "、".join(conflicts)
                    )
                elif not parse_chapter_numbers(response):
                    problems.append(
                        "没有可识别的章标题。每一章都必须单独成行，"
                        "写成“### 第 N 章：标题”，不要用“第一章”这种中文数字"
                    )
            # 结构不成立时不评审：评审要引大纲原文，而这一稿连章标题都还没有。
            if not problems and reviewer is not None:
                problems.extend(
                    self._outline_review_problems(
                        reviewer, response, section_name, section_content, seen
                    )
                )
            if not problems:
                return response

            rejected = response or ""
            for problem in problems:
                if problem not in seen:
                    seen.append(problem)
            self.app.logger.warning(
                "Section '%s' outline failed validation (attempt %s/%s): %s",
                section_name,
                attempt + 1,
                self.planning_retry_limit + 1,
                "；".join(problems),
            )
            if attempt > 0 and problems == previous:
                self.app.logger.warning(
                    "Section '%s' outline made no progress; stopping early", section_name
                )
                break
            previous = problems
        raise PlanningContractError(
            f"“{zh_label(section_name)}”的章节大纲在 {self.planning_retry_limit} 次重试后仍未通过："
            + "；".join(seen),
            code="outline_retry_exhausted",
        )

    def _outline_review_problems(
        self, reviewer, outline, section_name, section_content, already_seen
    ):
        """大纲评审开出的问题；评审自己失灵时放行，不让整段大纲卡死。

        判不出来和判不合格对作者是两回事：前者只说明这一次调用没成，而大纲阶段
        没有待复审出口，卡在这里等于整部书写不下去。失灵记进日志，人能查。
        """
        try:
            review = reviewer.review_chapter_outline(
                outline,
                section_name,
                section_content,
                repairs_requested=list(already_seen) or None,
            )
        except DomainReviewError as error:
            self.app.logger.warning(
                "Section '%s' outline review returned no verdict, letting it through: %s",
                section_name,
                error,
            )
            return []
        if review.passed:
            return []
        self.app.logger.warning(
            "Section '%s' outline failed review (avg %.2f/%.2f)",
            section_name,
            review.average_score,
            review.pass_average,
        )
        return review.asks or ["章节大纲未通过评审，但评审没有给出具体修复项"]

    def _generate_chapter_outline(self, ui):
        """Runs on a worker thread; UI values arrive via the snapshot."""
        selected_model = ui.model
        output_dir = ui.output_dir
        os.makedirs(output_dir, exist_ok=True)
        dir_manager = self._get_directory_manager(output_dir)
        chapter_outlines_dir = dir_manager.get_chapter_outlines_path()
        self.app.logger.info(
            "Generating chapter outlines. Model: %s, Output dir: %s", selected_model, output_dir
        )

        parameters_file_path = dir_manager.get_parameters_path()
        selected_structure_name = "6-Act Structure"  # Default
        params = {}
        if not os.path.exists(parameters_file_path):
            self.app.logger.warning(
                "Parameters file not found at %s. Using default structure: %s",
                parameters_file_path,
                selected_structure_name,
            )
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
                self.app.logger.warning(
                    "'Story Structure' missing in %s. Using default: %s",
                    parameters_file_path,
                    selected_structure_name,
                )
        story_params = normalize_story_parameters(params)
        genre_label = format_genre_label(story_params)
        location_guidance = build_location_guidance(story_params)

        sections_to_process = STRUCTURE_SECTIONS_MAP.get(selected_structure_name)
        if not sections_to_process:
            show_error("错误", f"找不到故事结构“{zh_label(selected_structure_name)}”的阶段定义。")
            self.app.logger.error(
                "No sections defined for structure '%s'.", selected_structure_name
            )
            return

        chapter_number_offset = 1  # To keep track of chapter numbers across sections
        generated_sections = []
        skipped_sections = []
        failed_sections = []
        missing_structure = []
        # 章号 -> 声明它的部分。补一段缺失的大纲会占用后面各段现有的号段，
        # 那些段的大纲随即过时；不点破的话，两份大纲会同时自称第 11 章。
        claimed_chapters = {}
        renumber_needed = []

        def claim(section, numbers):
            clashes = sorted(
                {claimed_chapters[number] for number in numbers if number in claimed_chapters}
            )
            for owner in clashes:
                if (owner, section) not in renumber_needed:
                    renumber_needed.append((owner, section))
            for number in numbers:
                claimed_chapters.setdefault(number, section)

        for current_section_name in sections_to_process:
            safe_selected_structure_name = selected_structure_name.lower().replace(' ', '_')
            safe_section_name = current_section_name.lower().replace(' ', '_').replace(':','').replace('/','_')

            input_filename_base = f"{safe_selected_structure_name}_{safe_section_name}.md"
            input_filepath = os.path.join(output_dir, "story", "structure", input_filename_base)
            output_filename_base = f"chapter_outlines_{safe_selected_structure_name}_{safe_section_name}.md"
            output_filepath = os.path.join(chapter_outlines_dir, output_filename_base)

            # 已有可用大纲就跳过。整批重写会让下游 23 份场景规划和契约
            # 对着一份已经变了的大纲，而它们并不会因此失效——错位是静默的。
            if self._has_usable_chapter_outline(output_filepath):
                existing = parse_chapter_numbers(open_file(output_filepath))
                claim(current_section_name, existing)
                chapter_number_offset = max(chapter_number_offset, max(existing) + 1)
                skipped_sections.append(current_section_name)
                self.app.logger.info(
                    "Skipping section '%s'; usable outline already exists with chapters %s",
                    current_section_name,
                    existing,
                )
                continue

            self.app.logger.info(
                "Processing section: %s from file: %s", current_section_name, input_filepath
            )
            try:
                detailed_section_content = open_file(input_filepath)
            except FileNotFoundError:
                show_warning("文件缺失", f"找不到“{zh_label(current_section_name)}”的详细规划（文件：{input_filename_base}），将跳过该部分。")
                self.app.logger.warning(
                    "File %s not found. Skipping section '%s'.",
                    input_filepath,
                    current_section_name,
                )
                missing_structure.append(current_section_name)
                continue

            prompt = (
                f"请为一部{genre_label}小说生成章节大纲。"
                f"小说采用“{zh_label(selected_structure_name)}”框架。"
                f"该结构包含：{', '.join(zh_label(section) for section in sections_to_process)}。\n"
                f"当前重点是 **{zh_label(current_section_name)}**。以下是这一部分的详细规划：\n\n{detailed_section_content}\n\n"
                f"请根据“{zh_label(current_section_name)}”的详细规划，逐章生成大纲。"
                "每一章都应有明确目的，并推动这一结构部分的故事。"
                "为每章建议所含场景，并列出本章涉及的人物、势力和具体地点。"
                f"“{zh_label(current_section_name)}”从第 {chapter_number_offset} 章开始，请依次分配章号。\n"
                "每一章必须单独起一行标题，写成“### 第 N 章：标题”，使用阿拉伯数字，"
                "不要用“第一章”这样的中文数字，也不要把章标题混进正文段落。\n"
                + "\n".join(build_story_parameter_lines(story_params)) + "\n"
                + "\n".join(f"- {line}" for line in location_guidance) + "\n"
                "请以 Markdown 格式输出，不要使用代码围栏，也不要在响应中写出“Markdown”一词。"
            )
            save_prompt_to_file(
                output_dir,
                f"chapter_outlines_{safe_selected_structure_name}_{safe_section_name}_prompt",
                prompt,
            )

            current_backend = get_backend()
            backend_info = f"{current_backend}" if current_backend != "api" else f"api/{selected_model}"
            self.app.logger.info(
                "Requesting chapter outline for '%s' (starts at chapter %s, backend %s)",
                current_section_name,
                chapter_number_offset,
                backend_info,
            )
            try:
                response = self._generate_valid_outline_response(
                    prompt,
                    selected_model,
                    current_section_name,
                    detailed_section_content,
                    story_params,
                )
            except PlanningContractError as exc:
                failed_sections.append((current_section_name, str(exc)))
                self.app.logger.error(
                    "Section '%s' outline generation failed: %s", current_section_name, exc
                )
                continue

            chapters_in_response = parse_chapter_numbers(response)
            claim(current_section_name, chapters_in_response)
            # 以大纲写出的最大章号推进，而不是按数量累加：模型偶尔会跳号，
            # 按数量累加会让下一部分的起始章号与本部分末章重叠。
            chapter_number_offset = max(
                chapter_number_offset, max(chapters_in_response) + 1
            )
            self.app.logger.info(
                "Section '%s' outline declares chapters %s; next section starts at %s",
                current_section_name,
                chapters_in_response,
                chapter_number_offset,
            )

            os.makedirs(chapter_outlines_dir, exist_ok=True)
            write_file(output_filepath, response)
            generated_sections.append(current_section_name)
            self.app.logger.info(
                "Chapter outline for %s saved to %s", current_section_name, output_filepath
            )

        if failed_sections:
            detail = "\n".join(
                f"{zh_label(section)}：{reason}" for section, reason in failed_sections
            )
            show_error("章节大纲未完成", "以下部分没有生成可用大纲：\n" + detail)
        if missing_structure:
            show_warning(
                "缺少上游详细规划",
                "以下部分在“故事结构”页还没有详细规划，已跳过：\n"
                + "、".join(zh_label(section) for section in missing_structure),
            )
        if renumber_needed:
            detail = "\n".join(
                f"{zh_label(first)} 与 {zh_label(second)} 都声称占用同一批章号"
                for first, second in renumber_needed
            )
            show_warning(
                "章号需要重排",
                "补上缺失的部分后，它占用了后面各段原有的章号：\n"
                + detail
                + "\n\n请删掉后面这些部分的章节大纲文件再点一次“生成章节大纲”，"
                "让它们重新编号；随后对应章节的场景规划也要一并重跑。",
            )

        if generated_sections:
            message = "已生成：" + "、".join(
                zh_label(section) for section in generated_sections
            )
        else:
            message = "没有缺失的章节大纲，无需重新生成。"
        if skipped_sections:
            message += f"\n已跳过 {len(skipped_sections)} 个现有有效大纲。"
        if not failed_sections:
            show_success("章节大纲完成", message)

    def _plan_long_form_scenes(self, ui):
        selected_model = ui.model 
        output_dir = ui.output_dir
        os.makedirs(output_dir, exist_ok=True)
        dir_manager = self._get_directory_manager(output_dir)
        chapter_outlines_dir = dir_manager.get_chapter_outlines_path()
        scene_plans_dir = dir_manager.get_full_path("scene_plans_dir")
        self.app.logger.info(f"Planning Long-Form Scenes (Novella/Novel/Epic). Model: {selected_model}, Output Dir: {output_dir}")

        # --- Read Parameters to get selected structure and length ---
        parameters_file_path = dir_manager.get_parameters_path()
        selected_structure_name = "6-Act Structure" # Default
        story_length = "Novel (Standard)" # Default for prompt context
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
            story_length = params_from_file.get("Story Length", story_length)
            if loaded_structure and loaded_structure.strip():
                selected_structure_name = loaded_structure
            else:
                self.app.logger.warning(f"'Story Structure' not found/empty in {parameters_file_path}. Using default for scene planning: {selected_structure_name}")
        self.app.logger.info(f"Using structure: {selected_structure_name}, Length: {story_length} for long-form scene planning.")
        # --- End Reading Parameters ---
        story_params = normalize_story_parameters(params_from_file)
        genre_label = format_genre_label(story_params)
        location_guidance = build_location_guidance(story_params)

        sections_to_process = STRUCTURE_SECTIONS_MAP.get(selected_structure_name)
        if not sections_to_process:
            self.app.logger.error(f"Scene Plan: Section definitions for '{selected_structure_name}' not found.")
            show_error("错误", f"找不到故事结构“{zh_label(selected_structure_name)}”的阶段定义。")
            return

        lore_content_path = os.path.join(output_dir, "story", "lore", "generated_lore.md")
        try:
            lore_content = sanitize_lore_content(open_file(lore_content_path))
        except FileNotFoundError:
            show_warning("文件缺失", f"找不到世界观文件 {lore_content_path}，场景规划可能缺少背景。")
            self.app.logger.warning(f"Lore file {lore_content_path} not found for scene planning.")
            lore_content = "缺少整体世界观背景。"
        
        # 章号以章节大纲写明的为准。此前这里用的是「按出现顺序累加」的计数器，
        # 任何被跳过的部分都会让后续所有章号整体前移，规划内容和文件名随之错位。
        next_expected_chapter = 1
        claimed_chapters = set()
        generated_chapters = []
        skipped_chapters = []
        failed_chapters = []
        incomplete_sections = []

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
                incomplete_sections.append((current_section_name, "缺少章节大纲文件"))
                continue

            if not section_chapter_outline_content:
                self.app.logger.warning(f"Chapter outline file {chapter_outline_input_filepath} is empty. Skipping scene planning for '{current_section_name}'.")
                incomplete_sections.append((current_section_name, "章节大纲为空"))
                continue

            section_chapter_numbers, numbering_warning = resolve_section_chapter_numbers(
                parse_chapter_numbers(section_chapter_outline_content),
                next_expected_chapter,
                claimed_chapters,
            )
            if numbering_warning:
                self.app.logger.warning(
                    "Section '%s' chapter numbering: %s",
                    current_section_name,
                    numbering_warning,
                )
            if not section_chapter_numbers:
                self.app.logger.warning(f"No chapters detected in {chapter_outline_input_filepath}. Skipping scene planning for '{current_section_name}'.")
                incomplete_sections.append((current_section_name, numbering_warning or "大纲中没有章标题"))
                continue
            if numbering_warning:
                show_warning(
                    "章号已调整",
                    f"“{zh_label(current_section_name)}”：{numbering_warning}",
                )

            claimed_chapters.update(section_chapter_numbers)
            next_expected_chapter = max(next_expected_chapter, max(section_chapter_numbers) + 1)
            self.app.logger.info(
                "Section '%s' owns chapters %s",
                current_section_name,
                section_chapter_numbers,
            )

            for current_chapter_for_prompt in section_chapter_numbers:
                output_scene_plan_base = (
                    f"scenes_{safe_selected_structure_name}_{safe_section_name}"
                    f"_ch{current_chapter_for_prompt}.md"
                )
                os.makedirs(scene_plans_dir, exist_ok=True)
                output_scene_plan_filepath = os.path.join(
                    scene_plans_dir,
                    output_scene_plan_base,
                )

                if self._has_usable_scene_plan(
                    output_scene_plan_filepath,
                    output_dir,
                    current_chapter_for_prompt,
                ):
                    # Old prompts demonstrated C001/E001 in every chapter.
                    # Repair only contracts that actually collide; keep the
                    # accepted creative Markdown byte-for-byte unchanged.
                    existing_markdown = open_file(output_scene_plan_filepath)
                    self._normalise_existing_domain_collisions(
                        output_dir,
                        current_chapter_for_prompt,
                        existing_markdown,
                        selected_model,
                        story_params,
                    )
                    skipped_chapters.append(current_chapter_for_prompt)
                    self.app.logger.info(
                        "Skipping Chapter %s scene planning; usable file already exists: %s",
                        current_chapter_for_prompt,
                        output_scene_plan_filepath,
                    )
                    continue
                
                prompt_lines = [
                    f"请为{genre_label}故事第 {current_chapter_for_prompt} 章规划场景（篇幅：{zh_label(story_length)}）。",
                    f"故事采用“{zh_label(selected_structure_name)}”框架，包含：{', '.join(zh_label(section) for section in sections_to_process)}。",
                    f"当前正在设计 **{zh_label(current_section_name)}** 的场景。",
                    *build_story_parameter_lines(story_params),
                    f"\n以下是包含第 {current_chapter_for_prompt} 章的“{zh_label(current_section_name)}”逐章大纲：\n{section_chapter_outline_content}",
                    f"\n请重点把上述大纲中的第 {current_chapter_for_prompt} 章扩展为详细场景。"
                ]
                
                if story_length == "Novella":
                    prompt_lines.append("本故事是中篇小说，本章场景应紧凑而有冲击力，聚焦必要的情节推进和人物时刻。")
                
                prompt_lines.extend([
                    "每个场景需说明：环境与具体地点、出场人物、关键行动/事件、关键对白片段（如有必要），以及它如何推动本章情节或人物发展。",
                    f"请保持人物弧光、势力和地点与第 {current_chapter_for_prompt} 章大纲中的建议一致。",
                    *[f"- {line}" for line in location_guidance],
                    f"整体世界观如下，供参考：\n{lore_content}",
                    "\n请使用结构清晰的 Markdown。每个场景必须使用阿拉伯数字编号，并以独立标题开始，例如“### 场景 1：场景标题”或“## 场景 2 - 场景标题”。不要使用“场景一”之类的中文数字编号，也不要只使用加粗文本充当场景标题。",
                    self._contract_instructions(
                        output_dir, current_chapter_for_prompt, story_params
                    ),
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

                try:
                    response, scene_markdown, contract = self._generate_valid_scene_response(
                        prompt,
                        selected_model,
                        current_chapter_for_prompt,
                        lore_content,
                        story_params,
                        output_dir=output_dir,
                    )
                except PlanningContractError as exc:
                    # Keep going so one bad chapter does not abandon the run,
                    # but remember the hole: the cross-chapter gate below is
                    # meaningless while chapters are missing.
                    failed_chapters.append((current_chapter_for_prompt, str(exc)))
                    self.app.logger.error(
                        "Chapter %s scene planning failed: %s",
                        current_chapter_for_prompt,
                        exc,
                    )
                    continue

                self._save_scene_plan_and_contract(
                    output_dir,
                    output_scene_plan_filepath,
                    response,
                    current_chapter_for_prompt,
                    scene_markdown=scene_markdown,
                    contract=contract,
                )
                generated_chapters.append(current_chapter_for_prompt)
                self.app.logger.info(f"Scene plan for Chapter {current_chapter_for_prompt} saved to {output_scene_plan_filepath}")

        if failed_chapters:
            detail = "\n".join(
                f"第 {number} 章：{reason}" for number, reason in failed_chapters
            )
            show_error(
                "场景规划未完成",
                "以下章节没有生成可用规划，未做跨章校验，请修复后重新规划：\n" + detail,
            )
            return

        if incomplete_sections:
            # 跨章校验会按 1..N 逐章核对，书里还缺着整段的时候跑它只会得到
            # 一串「找不到第 N 章」，并触发无从修起的重试。先把缺口说清楚。
            detail = "\n".join(
                f"{zh_label(section)}：{reason}" for section, reason in incomplete_sections
            )
            show_warning(
                "章节大纲不完整",
                "以下部分没有可用的章节大纲，已跳过；补齐后再重新规划才会做跨章校验：\n"
                + detail,
            )
        else:
            self._validate_sequence_with_retries(
                output_dir,
                max(claimed_chapters) if claimed_chapters else 0,
                selected_model,
                lore_content,
                story_params,
            )
        if generated_chapters:
            generated_text = "、".join(map(str, generated_chapters))
            message = f"已生成第 {generated_text} 章的场景规划。"
        else:
            message = "没有缺失或损坏的场景规划，无需重新生成。"
        if skipped_chapters:
            message += f"\n已跳过 {len(skipped_chapters)} 个现有有效文件。"
        show_success("增量场景规划完成", message)

    def _plan_short_story_scenes(self, ui):
        selected_model = ui.model
        output_dir = ui.output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.app.logger.info(f"Planning Short Story Scenes. Model: {selected_model}, Output Dir: {output_dir}")

        # --- Read Parameters ---
        if not (self.app and hasattr(self.app, 'param_ui')):
            self.app.logger.error("ScenePlanning: Parameters.py not available for short story scene planning.")
            show_error("错误", "无法加载故事参数。")
            return
        
        parameters = ui.parameters
        story_params = normalize_story_parameters(parameters)
        genre_label = format_genre_label(story_params)
        location_guidance = build_location_guidance(story_params)
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
        except FileNotFoundError:
            self.app.logger.error(f"Detailed short story plot file not found: {detailed_plot_filepath}")
            show_error("错误", f"找不到详细情节文件“{detailed_plot_filename}”，请先在“故事结构”页生成。")
            return
        if not short_story_plot_content.strip():
            self.app.logger.error(f"Detailed short story plot file is empty: {detailed_plot_filepath}")
            show_error("错误", f"详细情节文件“{detailed_plot_filename}”为空，无法规划场景。")
            return

        # --- Load Lore Context (Optional but good) ---
        lore_content = "缺少整体世界观背景。"
        lore_content_path = os.path.join(output_dir, "story", "lore", "generated_lore.md")
        if os.path.exists(lore_content_path):
            lore_content = sanitize_lore_content(open_file(lore_content_path))
            self.app.logger.info(f"Loaded lore context from {lore_content_path}")

        # --- Construct the Prompt ---
        prompt_lines = [
            f"请为{genre_label}短篇小说《{novel_title}》规划场景。",
            f"故事采用“{zh_label(selected_structure_name)}”框架。",
            *build_story_parameter_lines(story_params),
            "以下是完整短篇小说的详细总体情节：\n\n",
            "--- 短篇小说详细情节 ---",
            short_story_plot_content,
            "\n\n--- 短篇小说详细情节结束 ---",
            "\n请根据详细情节，把故事拆分成一系列清晰、独立的场景。",
            "每个场景需说明：\n",
            "  - 建议的场景编号（如场景 1、场景 2）。\n",
            "  - 环境与具体地点。\n",
            "  - 出场人物。\n",
            "  - 场景中的关键行动和事件。\n",
            "  - 关键对白片段或对白概要。\n",
            "  - 该场景如何依据详细情节推动整体故事，或发展人物/主题。\n",
            "确保场景之间衔接自然，并覆盖详细情节中的完整叙事弧。\n",
            *[f"- {line}" for line in location_guidance],
            "\n重要格式要求：",
            "每个场景必须以 Markdown 标题开始，并使用阿拉伯数字编号，例如“### 场景 1：<场景标题>”或“## 场景 2 - <场景标题>”。不要使用中文数字编号或只加粗的标题。",
            "标题下方再列出该场景的环境、人物、关键行动等要点。",
            "\n如有需要，可参考以下整体世界观：",
            lore_content,
            "\n现在请按场景输出完整的短篇规划，合并为一份结构清晰的 Markdown 文档，不要使用代码围栏。",
            self._contract_instructions(output_dir, 1, story_params),
        ]
        prompt = "\n".join(prompt_lines)

        prompt_base_name = f"plan_scenes_short_story_{safe_structure_name_for_file}_prompt"
        prompt_filepath = save_prompt_to_file(output_dir, prompt_base_name, prompt)
        
        current_backend = get_backend()
        backend_info = f"{current_backend}" if current_backend != "api" else f"api/{selected_model}"
        log_msg_source = f"(from {prompt_filepath})" if prompt_filepath else "(from memory, save failed)"
        self.app.logger.info(f"Sending Short Story Scene Planning Prompt {log_msg_source} to LLM ({backend_info})...")

        try:
            response, scene_markdown, contract = self._generate_valid_scene_response(
                prompt,
                selected_model,
                1,
                lore_content,
                story_params,
                require_complete_sequence=True,
                output_dir=output_dir,
            )
        except PlanningContractError as exc:
            self.app.logger.error("Short story planning contract is invalid: %s", exc)
            show_error("场景规划契约无效", f"短篇场景未保存：{exc}")
            return

        conflicts = find_scene_world_conflicts(scene_markdown, lore_content, story_params)
        if conflicts:
            show_error(
                "场景规划与世界观冲突",
                "生成结果包含世界观未定义的科幻内容："
                + "、".join(conflicts)
                + "。结果未保存，请重新生成。",
            )
            return
        
        self.app.logger.info(f"Received short story scenes from LLM. Length: {len(response)} chars.")

        output_filename_base = f"scenes_short_story_{safe_structure_name_for_file}.md"
        os.makedirs(os.path.join(output_dir, "story", "planning"), exist_ok=True)
        output_filename_full_path = os.path.join(output_dir, "story", "planning", output_filename_base)
        
        self._save_scene_plan_and_contract(
            output_dir,
            output_filename_full_path,
            response,
            1,
            scene_markdown=scene_markdown,
            contract=contract,
        )
        self.app.logger.info(f"Short story scenes saved successfully to {output_filename_full_path}")
