"""Bounded article-adaptation use cases, with one shared generation lock per project."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy

from openai import APIError

from core.generation.cancellation import CancelToken, GenerationCancelled

from .contracts import ArtifactRecord, JsonObject, ProgressReporter, RequestRecorder, TextTransport
from .llm import BudgetExhausted, RequestBudget, ScriptLLM
from .models import (
    ASSET_KINDS,
    PROMPT_KINDS,
    WorkspaceError,
    capture_model_snapshot,
    content_key,
    encode,
    identity,
    parse_object,
    require,
    text,
    timestamp,
    validate_choice,
    validate_model_snapshot,
)
from .project_service import ProjectService
from .prompt_compiler import compile_asset, compile_shots, finalize_asset_prompts, plan_views
from .prompts import INSTRUCTIONS, stage_prompt
from .repository import ProjectRepository
from .review import QualityCheckResult, check_candidate
from .validation import rows, strings, validate_content

PREREQUISITES = {
    "novel_copy": (),
    "video_copy": ("novel_copy",),
    "asset_catalog": ("video_copy",),
    "storyboard": ("video_copy", "asset_catalog"),
    "asset_spec": ("asset_catalog",),
}
MAX_CONTEXT_CHARS = 48000
DEFAULT_MAX_RETRIES = 2
STAGE_MAX_CALLS = 2 * (1 + DEFAULT_MAX_RETRIES)


def normalize_catalog(content: JsonObject, previous: ArtifactRecord | None = None) -> JsonObject:
    """Stable entity IDs are allocated by the app, with explicit alias ambiguity checks."""
    value = deepcopy(content)
    old = (previous or {}).get("content", {}).get("entities", [])
    used, identities = set(), set()
    for entity in rows(value.get("entities"), "目录实体", key=None):
        validate_choice(entity.get("kind"), ASSET_KINDS, "资产类型")
        text(entity.get("name"), "资产名称")
        strings(entity.get("aliases", []), "资产别名")
        names = {entity.get("name"), *entity.get("aliases", [])}
        matches = [
            e
            for e in old
            if e["kind"] == entity.get("kind") and names & {e["name"], *e.get("aliases", [])}
        ]
        require(
            len(matches) <= 1,
            "INVALID_OUTPUT",
            f"“{entity['name']}”对应多个原有对象，请说明这些名字是否指同一对象",
        )
        match = matches[0] if matches else None
        entity["asset_id"] = match["asset_id"] if match else identity("asset")
        require(
            entity["asset_id"] not in identities, "INVALID_OUTPUT", "目录中同一实体重复，请合并别名"
        )
        identities.add(entity["asset_id"])
        entity.setdefault("usage", "on_screen")
        entity.setdefault("aliases", [])
        entity.setdefault("inferred_fields", [])
        old_variants = {v["label"]: v["variant_id"] for v in match["variants"]} if match else {}
        for variant in rows(entity.get("variants"), "状态变体", key=None):
            text(variant.get("label"), "状态名称")
            variant["variant_id"] = old_variants.get(
                variant.get("label"), variant.get("variant_id") or identity("variant")
            )
        signature = (entity.get("kind"), entity.get("name"))
        require(signature not in used, "INVALID_OUTPUT", "相同名称实体需先消歧")
        used.add(signature)
    return value


class WorkflowService:
    def __init__(
        self,
        repository: ProjectRepository,
        model: str = "hosted-llm",
        *,
        transport: TextTransport | None = None,
        max_context_chars: int = MAX_CONTEXT_CHARS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        quality_check: Callable[[JsonObject, ArtifactRecord], QualityCheckResult] | None = None,
    ) -> None:
        self.repository, self.model, self.transport = repository, model, transport
        require(
            type(max_retries) is int and max_retries >= 0, "INVALID_INPUT", "重试次数必须为非负整数"
        )
        self.max_context_chars = max_context_chars
        self.max_retries, self.quality_check = max_retries, quality_check

    def prepare(
        self,
        stage: str,
        *,
        mode: str = "generate",
        target_id: str | None = None,
        asks: list[str] | None = None,
        entity_id: str | None = None,
        variant_id: str | None = None,
        kind: str | None = None,
        shot_ids: list[str] | None = None,
        request_id: str | None = None,
        use_stale: bool = False,
        fact_table_id: str | None = None,
        upstream_ids: dict[str, str] | None = None,
    ) -> JsonObject:
        require(stage in INSTRUCTIONS, "INVALID_INPUT", "未知生成阶段")
        require(mode in {"generate", "revise"}, "INVALID_INPUT", "未知生成方式")
        state = self.repository.load()
        article = ProjectService(self.repository).context(state=state)
        config = self.repository.current("config", state=state)
        require(
            config is not None and self.repository.is_current(article["selection_id"], state=state),
            "STALE_INPUT",
            "请先保存当前有效文章与创作配置",
        )
        deps = {"config": config["id"], "selection": article["selection_id"]}
        if fact_table_id:
            facts = self.repository.record(fact_table_id, state=state)
            require(
                facts["kind"] == "fact_table"
                and facts["dependency_refs"]["selection"] == article["selection_id"],
                "STALE_INPUT",
                "分段事实表与文章范围不一致",
            )
            deps["fact_table"] = fact_table_id
            article = {
                **article,
                "text": "全文已按用户选择分段处理，使用共享事实表；原文按坐标保留。",
                "source_refs": [
                    {k: v for k, v in ref.items() if k != "quote"} for ref in article["source_refs"]
                ],
                "segments": [],
                "facts": facts["content"]["facts"],
                "representation": "explicit_chunk_fact_table",
            }
        context = {"article": article, "config": config["content"]}
        candidate = self.repository.record(target_id, state=state) if target_id else None
        if stage in {"view_prompts", "video_motion_prompt", "shot_prompts"}:
            require(candidate is not None, "INVALID_INPUT", "请选择需补充提示词的版本")
            kind = candidate["kind"]
            require(
                (stage == "view_prompts" and kind in ASSET_KINDS.values())
                or (stage == "video_motion_prompt" and kind == "storyboard")
                or (stage == "shot_prompts" and kind == "prompt_bundle"),
                "INVALID_INPUT",
                "提示词目标类型不匹配",
            )
            require(
                (use_stale or self.repository.is_current(candidate["id"], state=state)),
                "STALE_INPUT",
                "目标内容基于旧来源，请重新生成或明确沿用",
            )
            deps = deepcopy(candidate["dependency_refs"])
            if stage == "shot_prompts":
                board = self.repository.record(deps["storyboard"], state=state)
                article = ProjectService(self.repository).context(
                    state=state, selection_id=board["dependency_refs"]["selection"]
                )
                context = {
                    "article": article,
                    "storyboard": board,
                    "asset_specs": [
                        self.repository.record(ref, state=state)
                        for ref in candidate["content"]["asset_spec_refs"]
                    ],
                    "config": self.repository.record(
                        board["dependency_refs"]["config"], state=state
                    )["content"],
                }
        else:
            for key in PREREQUISITES[stage]:
                record = (
                    self.repository.record(upstream_ids[key], state=state)
                    if upstream_ids and key in upstream_ids
                    else self.repository.current(key, state=state)
                )
                require(
                    record is not None,
                    "MISSING_UPSTREAM",
                    f"请先生成 {key}",
                )
                if state["current"].get(key) != record["id"] or not self.repository.is_current(
                    record["id"], state=state
                ):
                    require(
                        use_stale, "STALE_INPUT", "当前稿基于旧内容；请选择重新生成或明确沿用原稿"
                    )
                    deps.update(record["dependency_refs"])
                context[key] = record
                deps[key] = record["id"]
            if use_stale:
                article = ProjectService(self.repository).context(
                    state=state, selection_id=deps["selection"]
                )
                context.update(
                    article=article,
                    config=self.repository.record(deps["config"], state=state)["content"],
                )
            kind = kind if stage == "asset_spec" else stage
        if stage == "asset_spec":
            require(kind in ASSET_KINDS.values(), "INVALID_INPUT", "请选择人物、场景或道具类型")
            entity = next(
                (
                    e
                    for e in context["asset_catalog"]["content"]["entities"]
                    if e["asset_id"] == entity_id
                ),
                None,
            )
            require(
                entity is not None and ASSET_KINDS[entity["kind"]] == kind,
                "INVALID_INPUT",
                "目录实体不存在或类型不符",
            )
            require(
                variant_id in {v["variant_id"] for v in entity["variants"]},
                "INVALID_INPUT",
                "资产状态不存在",
            )
            context.update(entity=entity, variant_id=variant_id)
        else:
            entity = None
        if mode == "revise":
            require(
                candidate is not None
                and candidate["kind"] == kind
                and (use_stale or self.repository.is_current(candidate["id"], state=state)),
                "STALE_INPUT",
                "请先选择有效的待修订版本",
            )
            require(
                isinstance(asks, list) and any(isinstance(a, str) and a.strip() for a in asks),
                "INVALID_INPUT",
                "请填写具体修改意见",
            )
            require(candidate["dependency_refs"] == deps, "STALE_INPUT", "待修订稿的来源已变化")
        if stage != "novel_copy" and len(encode(context["article"])) > self.max_context_chars // 2:
            # Traverse the actual frozen lineage, including explicitly reused old drafts.
            pending = list(deps.values())
            visited = set()
            while pending:
                reference = pending.pop()
                if reference in visited:
                    continue
                visited.add(reference)
                record = self.repository.record(reference, state=state)
                if (
                    record["kind"] == "fact_table"
                    and record["dependency_refs"]["selection"] == context["article"]["selection_id"]
                ):
                    article = context["article"]
                    context["article"] = {
                        **article,
                        "text": "本次文章资料见共享事实表。",
                        "segments": [],
                        "source_refs": [
                            {k: v for k, v in ref.items() if k != "quote"}
                            for ref in article["source_refs"]
                        ],
                        "facts": record["content"]["facts"],
                        "representation": "explicit_chunk_fact_table",
                        "fact_table_version_id": record["id"],
                        "notice": "全文经用户启用的分段计划处理；未将全文直接放入本次请求，原文按坐标保留。",
                    }
                    deps["fact_table"] = record["id"]
                    break
                pending.extend(record["dependency_refs"].values())
        key = content_key(
            kind,
            candidate["content"]
            if candidate
            else {"asset_id": entity_id, "variant_id": variant_id},
        )
        custom = self.repository.current(f"prompt_template:{stage}", state=state)
        instruction = custom["content"]["instruction"] if custom else INSTRUCTIONS[stage]
        if shot_ids:
            context["selected_shot_ids"] = shot_ids
        if stage == "view_prompts":
            candidate = deepcopy(candidate)
            candidate["content"]["views"] = plan_views(kind, candidate["content"])
        prompt = stage_prompt(
            stage,
            context,
            instruction=instruction,
            candidate=candidate,
            asks=asks,
            entity=entity,
            kind=kind,
        )
        require(
            len(prompt) <= self.max_context_chars,
            "INPUT_TOO_LARGE",
            f"当前输入约 {len(prompt)} 字符，超过本次 {self.max_context_chars} 字符容量；请缩小选区或确认分块计划",
        )
        return {
            "selection_snapshot": {key: state["current"].get(key) for key in deps},
            "use_stale": use_stale,
            "stage": stage,
            "max_retries": self.max_retries,
            "mode": mode,
            "kind": kind,
            "key": key,
            "model": self.model,
            "model_snapshot": capture_model_snapshot(self.model),
            "request_id": request_id or identity("request"),
            "expected_revision": state["revision"],
            "input_refs": deps,
            "context": context,
            "prompt": prompt,
            "prompt_ref": custom["id"] if custom else f"{stage}-default-v2",
            "requirements": {"instruction": instruction, "asks": asks or [], "mode": mode},
            "candidate_id": target_id,
            "selected_before": state["current"].get(key),
            "shot_ids": shot_ids or [],
            "previous_catalog": self.repository.current("asset_catalog", state=state)
            if stage == "asset_catalog"
            else None,
        }

    def execute(
        self,
        command: JsonObject,
        *,
        token: CancelToken | None = None,
        report: ProgressReporter | None = None,
        budget: RequestBudget | None = None,
        on_request: RequestRecorder | None = None,
    ) -> JsonObject:
        """Run the Agent Loop; max_retries excludes the initial generation."""
        token = token or CancelToken()
        report = report or (lambda *args: None)
        max_retries = command["max_retries"]
        require(
            type(max_retries) is int and max_retries >= 0, "INVALID_INPUT", "重试次数必须为非负整数"
        )
        stage_max_calls = 2 * (1 + max_retries)
        budget = budget or RequestBudget(max_calls=stage_max_calls)
        call_limit = min(budget.max_calls, budget.calls + stage_max_calls)
        with self.repository.generation_lock():
            run, created = self.repository.start_run(
                command,
                expected_revision=command["expected_revision"],
                snapshot={
                    "input_refs": command["input_refs"],
                    "model": command["model"],
                    "prompt_ref": command["prompt_ref"],
                    "model_snapshot": command["model_snapshot"],
                    "budget": {
                        "stage_max_calls": call_limit - budget.calls,
                        "task_max_calls": budget.max_calls,
                        "remaining_seconds": budget.remaining,
                    },
                },
            )
            if not created:
                require(
                    command["kind"] not in PROMPT_KINDS or bool(run["output_refs"]),
                    "PROMPT_REPLACED",
                    "这次任务的提示词已被替换或未完成，请使用当前提示词或重新生成",
                )
                return {
                    "run_id": run["id"],
                    "status": run["status"],
                    "version_id": next(iter(run["output_refs"]), None),
                    "selected": run.get("selected", False),
                    "review_status": run.get("review_status", "unavailable"),
                    "retries_used": run.get("retries_used", 0),
                }

            call_type = "generation"

            def record_call(prompt: str, response: str, error: str | None) -> None:
                self.repository.update_run(
                    run["id"],
                    lambda r, _: r["calls"].append(
                        {
                            "type": call_type,
                            "error": error,
                            "created_at": timestamp(),
                        }
                    ),
                )

                if on_request:
                    on_request("", "", error)

            send = ScriptLLM(
                command["model"], budget, token=token, transport=self.transport, record=record_call
            )
            candidates = []

            def generate_candidate(prompt: str) -> ArtifactRecord:
                generation_prompt = prompt
                while budget.calls < call_limit:
                    report("正在生成…" if not candidates else "正在调整…", -1)
                    raw = send(prompt)
                    try:
                        content = parse_object(raw)
                        if command["stage"] == "asset_catalog":
                            content = normalize_catalog(content, command.get("previous_catalog"))
                        content = self._validate_output(command, content)
                    except WorkspaceError as exc:
                        if exc.code not in {"INVALID_INPUT", "INVALID_OUTPUT"}:
                            raise
                        token.raise_if_cancelled()
                        if budget.calls >= call_limit or not budget.can_spend():
                            raise WorkspaceError(
                                "INVALID_OUTPUT", "未取得可用稿件：" + str(exc) + "；原稿保留"
                            ) from exc
                        prompt = (
                            generation_prompt
                            + "\n修复上次格式错误，只输出完整JSON。错误："
                            + str(exc)
                            + "\n上次原始回复："
                            + encode({"response": raw})
                        )
                        require(
                            len(prompt) <= self.max_context_chars,
                            "INPUT_TOO_LARGE",
                            "格式修复上下文超限",
                        )
                        continue
                    # Persist before any further paid request, even if cancellation just arrived.
                    with self.repository.transaction() as state:
                        version = self.repository._save(
                            state,
                            command["kind"],
                            content,
                            command["input_refs"],
                            parent_id=command.get("candidate_id"),
                            origin="model",
                            select=False,
                            metadata={
                                "run_id": run["id"],
                                "prompt_ref": command["prompt_ref"],
                                **({"plan_id": command["plan_id"]} if "plan_id" in command else {}),
                            },
                        )
                        candidates.append(version)
                        next(r for r in state["runs"] if r["id"] == run["id"])["output_refs"] = [
                            v["id"] for v in candidates
                        ]
                    token.raise_if_cancelled()
                    return version
                raise BudgetExhausted("本阶段请求预算已耗尽")

            latest = None
            retries_used = 0
            review_status = "unavailable"
            try:
                validate_model_snapshot(command["model_snapshot"])
                prompt = command["prompt"]
                # Agent Loop owns inputs, generation and retry control, never business rules.
                for attempt in range(max_retries + 1):
                    call_type = "generation"
                    latest = generate_candidate(prompt)
                    retries_used = attempt
                    call_type = "quality_check"
                    if budget.calls >= call_limit:
                        raise BudgetExhausted("没有剩余检查预算")
                    result = (
                        self.quality_check(command, latest)
                        if self.quality_check is not None
                        else check_candidate(command, latest, send, self.repository)
                    )
                    token.raise_if_cancelled()
                    review_status = "passed" if result.passed else "issues"
                    if result.passed or attempt == max_retries:
                        break
                    if call_limit - budget.calls < 2 or not budget.can_spend(2):
                        review_status = "incomplete"
                        break
                    prompt = (
                        command["prompt"]
                        + "\n根据外部质量检查反馈修改最新候选，保留原始任务与上下文，只返回完整JSON：\n"
                        + encode({"candidate": latest["content"], "feedback": result.feedback})
                    )
                    require(
                        len(prompt) <= self.max_context_chars,
                        "CONTEXT_TOO_LARGE",
                        "Agent Loop 修改上下文超限",
                    )
            except (APIError, TimeoutError, ConnectionError, BudgetExhausted) as exc:
                if latest is None:
                    self.repository.record_run_failure(run["id"], exc, cancelled=token.cancelled)
                    raise
                review_status = "incomplete" if isinstance(exc, BudgetExhausted) else "unavailable"
            except WorkspaceError as exc:
                if latest is None or exc.code not in {
                    "INVALID_INPUT",
                    "INVALID_OUTPUT",
                    "CONTEXT_TOO_LARGE",
                    "INPUT_TOO_LARGE",
                }:
                    self.repository.record_run_failure(run["id"], exc, cancelled=token.cancelled)
                    raise
                review_status = "unavailable"
            except Exception as exc:
                self.repository.record_run_failure(
                    run["id"],
                    exc,
                    cancelled=isinstance(exc, GenerationCancelled) or token.cancelled,
                )
                raise

            try:
                token.raise_if_cancelled()
                with self.repository.transaction() as state:
                    unchanged_inputs = all(
                        state["current"].get(k) == v
                        for k, v in command["selection_snapshot"].items()
                    )
                    select = (
                        unchanged_inputs
                        and state["current"].get(command["key"]) == command["selected_before"]
                    )
                    require(
                        command["kind"] not in PROMPT_KINDS or select,
                        "TARGET_VERSION_CONFLICT",
                        "提示词在生成期间已被修改，当前内容保留，请重新生成",
                    )
                    if select:
                        state["current"][command["key"]] = latest["id"]
                    current_run = next(r for r in state["runs"] if r["id"] == run["id"])
                    current_run.update(
                        status="succeeded",
                        output_refs=[
                            latest["id"],
                            *[v["id"] for v in candidates if v["id"] != latest["id"]],
                        ],
                        selected=select,
                        ended_at=timestamp(),
                        review_status=review_status,
                        retries_used=retries_used,
                    )
                return {
                    "run_id": run["id"],
                    "status": "succeeded",
                    "version_id": latest["id"],
                    "selected": select,
                    "review_status": review_status,
                    "retries_used": retries_used,
                }
            except Exception as exc:
                self.repository.record_run_failure(
                    run["id"],
                    exc,
                    cancelled=isinstance(exc, GenerationCancelled) or token.cancelled,
                )
                raise

    def _validate_output(self, command: JsonObject, content: JsonObject) -> JsonObject:
        state = self.repository.load()
        kind = command["kind"]
        if kind == "storyboard" and isinstance(content.get("shots"), list):
            # Resolve an exact, unambiguous video speaker label to the catalog's stable ID.
            # Resolve provider labels before structural validation; raw requests stay transient.
            catalog = self.repository.record(command["input_refs"]["asset_catalog"], state=state)[
                "content"
            ]["entities"]
            for shot in content["shots"]:
                require(isinstance(shot, dict), "INVALID_OUTPUT", "镜头必须是对象")
                for line in rows(shot.get("dialogue"), "对白", key=None, empty=True):
                    if not line.get("character_id") and line.get("speaker_label"):
                        matches = [
                            e
                            for e in catalog
                            if e["kind"] == "character"
                            and line["speaker_label"] in {e["name"], *e.get("aliases", [])}
                        ]
                        require(
                            len(matches) == 1, "INVALID_OUTPUT", "对白名称无法唯一映射到人物目录"
                        )
                        line["character_id"] = matches[0]["asset_id"]
        content = validate_content(
            kind,
            content,
            resolve=lambda ref: self.repository.record(ref, state=state),
            dependencies=command["input_refs"],
        )
        if command["candidate_id"]:
            original = self.repository.record(command["candidate_id"], state=state)["content"]
            if kind in ASSET_KINDS.values():
                require(
                    (content["asset_id"], content["variant_id"])
                    == (original["asset_id"], original["variant_id"]),
                    "INVALID_OUTPUT",
                    "修订不能改变资产身份或状态编号",
                )
                if command["stage"] == "view_prompts":
                    require(
                        all(
                            content.get(field) == value
                            for field, value in original.items()
                            if field != "views"
                        ),
                        "INVALID_OUTPUT",
                        "视图提示词任务不能改写当前设定",
                    )
                    planned = plan_views(kind, original)
                    content = finalize_asset_prompts(
                        kind, content, expected_view_ids=[view["view_id"] for view in planned]
                    )
            if command["stage"] == "video_motion_prompt":

                def fixed(shot: JsonObject) -> JsonObject:
                    return {k: v for k, v in shot.items() if k != "video_motion_prompt"}

                require(
                    [fixed(s) for s in content["shots"]] == [fixed(s) for s in original["shots"]],
                    "INVALID_OUTPUT",
                    "视频动作提示词任务不能改写镜头其他字段",
                )
            if kind == "prompt_bundle":
                require(
                    all(
                        content[field] == original[field]
                        for field in ("storyboard_version_id", "asset_spec_refs")
                    ),
                    "INVALID_OUTPUT",
                    "镜头提示词修订不能改变分镜和资产来源",
                )
                old_shots = {shot["shot_id"]: shot for shot in original["shots"]}
                for shot in content["shots"]:
                    require(
                        all(
                            shot.get(field) == old_shots[shot["shot_id"]].get(field)
                            for field in ("asset_refs", "source_refs")
                        ),
                        "INVALID_OUTPUT",
                        "镜头提示词修订不能改变人物、场景和原文依据",
                    )
            if kind in {"storyboard", "prompt_bundle"} and command["shot_ids"]:
                selected = set(command["shot_ids"])
                old = {s["shot_id"]: s for s in original["shots"]}
                new = {s["shot_id"]: s for s in content["shots"]}
                require(
                    selected <= old.keys() and old.keys() == new.keys(),
                    "INVALID_OUTPUT",
                    "局部修订不能增删其他镜头",
                )

                def stable(shot: JsonObject) -> JsonObject:
                    return {
                        k: v for k, v in shot.items() if k not in {"order", "start_ms", "end_ms"}
                    }

                require(
                    all(stable(old[k]) == stable(new[k]) for k in old.keys() - selected),
                    "INVALID_OUTPUT",
                    "局部修订改写了未选镜头",
                )
        if command["stage"] == "asset_spec":
            require(
                content.get("asset_id") == command["context"]["entity"]["asset_id"]
                and content.get("variant_id") == command["context"]["variant_id"],
                "INVALID_OUTPUT",
                "返回的资产身份或状态与任务不符",
            )
            require(
                content.get("design_confirmed", False) is False,
                "INVALID_OUTPUT",
                "视觉补充必须与原文事实分开",
            )
            content = finalize_asset_prompts(kind, content)
        return content

    def compile_asset_prompts(self, version_id: str, *, expected_revision: int) -> ArtifactRecord:
        record = self.repository.record(version_id)
        require(
            self.repository.is_current(version_id)
            and self.repository.current(record["key"])["id"] == version_id,
            "STALE_INPUT",
            "资产设定不是当前有效版本",
        )
        content = compile_asset(record["kind"], record["content"])
        return self.repository.save(
            record["kind"],
            content,
            record["dependency_refs"],
            parent_id=version_id,
            expected_revision=expected_revision,
            metadata={"compiler": "article-asset-v1"},
        )

    def compile_storyboard_prompts(
        self, *, expected_revision: int, use_stale: bool = False
    ) -> ArtifactRecord:
        state = self.repository.load()
        storyboard = self.repository.current("storyboard", state=state)
        require(
            storyboard is not None
            and (use_stale or self.repository.is_current(storyboard["id"], state=state)),
            "STALE_INPUT",
            "请先保存有效分镜",
        )
        specs = [
            self.repository.record(ref, state=state)
            for key, ref in state["current"].items()
            if key.split(":")[0] in ASSET_KINDS.values()
        ]
        require(
            use_stale or all(self.repository.is_current(s["id"], state=state) for s in specs),
            "STALE_INPUT",
            "资产设定来源已过期",
        )
        catalog = self.repository.record(
            storyboard["dependency_refs"]["asset_catalog"], state=state
        )
        content = compile_shots(
            storyboard,
            specs,
            asset_names={
                entity["asset_id"]: entity["name"] for entity in catalog["content"]["entities"]
            },
        )
        deps = {"storyboard": storyboard["id"]}
        deps.update({s["key"]: s["id"] for s in specs if s["id"] in content["asset_spec_refs"]})
        return self.repository.save(
            "prompt_bundle",
            content,
            deps,
            expected_revision=expected_revision,
            allow_stale=use_stale,
        )


class WorkspaceWork:
    """TaskRunner-compatible work; captures input before leaving the GUI thread."""

    def __init__(
        self,
        repository: ProjectRepository,
        command: JsonObject,
        *,
        transport: TextTransport | None = None,
        token: CancelToken | None = None,
    ) -> None:
        self.output_dir = repository.output_dir
        self.command = deepcopy(command)
        self.token = token or CancelToken()
        self.transport = transport

    def __call__(self, report: ProgressReporter) -> JsonObject:
        return WorkflowService(
            ProjectRepository(self.output_dir), self.command["model"], transport=self.transport
        ).execute(self.command, token=self.token, report=report)
