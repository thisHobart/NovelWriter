"""Bounded article-adaptation use cases, with one shared generation lock per project."""

from __future__ import annotations

import logging
from collections.abc import Callable
from copy import deepcopy

from core.generation.cancellation import CancelToken

from . import models as model_contracts
from .contracts import ArtifactRecord, JsonObject, ProgressReporter, RequestRecorder, TextTransport
from .feedback import (
    ACTION_NAMES,
    TaskContext,
    TaskTracker,
    command_target,
)
from .models import (
    ASSET_KINDS,
    VIEWS,
    capture_model_snapshot,
    content_key,
    encode,
    identity,
    require,
    text,
    validate_choice,
)
from .project_service import ProjectService
from .prompt_compiler import compile_shots, finalize_asset_prompts
from .prompts import INSTRUCTIONS, stage_prompt
from .repository import ProjectRepository
from .review import QualityCheckResult
from .storyboard_skill import STORYBOARD_SKILLS, load_storyboard_skill
from .validation import rows, strings, validate_content

PREREQUISITES = {
    "novel_copy": (),
    "video_copy": ("novel_copy",),
    "asset_catalog": ("video_copy",),
    "storyboard": ("video_copy", "asset_catalog"),
    "asset_spec": ("asset_catalog",),
}
MAX_CONTEXT_CHARS = 48000
DEFAULT_MAX_RETRIES = 1
MAX_FORMAT_RETRIES = 2
logger = logging.getLogger(__name__)


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


def freeze_stage_rules(
    repository: ProjectRepository,
    stage: str,
    config: JsonObject,
    *,
    state: JsonObject | None = None,
) -> JsonObject:
    custom = repository.current(f"prompt_template:{stage}", state=state)
    instruction = custom["content"]["instruction"] if custom else INSTRUCTIONS[stage]
    skill_id = config.get("storyboard_skill_id") if stage == "storyboard" else None
    require(
        skill_id is None or skill_id in STORYBOARD_SKILLS,
        "INVALID_INPUT",
        "请选择可用的分镜 Skill 或不使用 Skill",
    )
    if skill_id is not None:
        instruction += "\n\n用户选择的分镜 Skill：\n" + load_storyboard_skill(skill_id)
    return {
        "instruction": instruction,
        "skill_id": skill_id,
        "prompt_ref": custom["id"] if custom else f"{stage}-default-v3",
    }


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
            type(max_retries) is int and 0 <= max_retries <= 1,
            "INVALID_INPUT",
            "质量修订次数只能是0或1",
        )
        self.max_context_chars = max_context_chars
        self.max_retries, self.quality_check = max_retries, quality_check

    def prepare_video_prompt(
        self,
        shot_id: str,
        images: dict[str, str | None],
        *,
        workflow_id: str,
        parameters: JsonObject,
        current_prompt: str = "",
    ) -> JsonObject:
        from .video_prompt import snapshot_video_inputs

        state = self.repository.load()
        board = self.repository.current("storyboard", state=state)
        require(board is not None, "MISSING_UPSTREAM", "请先生成并保存分镜")
        require(
            any(s["shot_id"] == shot_id for s in board["content"]["shots"]),
            "STALE_INPUT",
            "请选择当前分镜中的镜头",
        )
        bundle = self.repository.current("prompt_bundle", state=state)
        require(
            bundle is not None and self.repository.is_current(bundle["id"], state=state),
            "MISSING_UPSTREAM",
            "请先生成资产提示词并编排镜头提示词",
        )
        inputs = snapshot_video_inputs(self.repository.output_dir, images, workflow_id, parameters)
        return self.prepare(
            "video_prompt",
            mode="revise",
            target_id=bundle["id"],
            asks=["结合当前镜头、资产及实际选图，编写完整中文视频提示词"],
            shot_ids=[shot_id],
            video_inputs=inputs,
            current_video_prompt=current_prompt,
        )

    def shot_assets(self, shot_id: str, *, state: JsonObject | None = None) -> list[JsonObject]:
        """Read this shot's exact assets and image prompts, without preparing generation."""
        from .image_generation import ImageGenerationStore, image_for_view

        state = self.repository.load() if state is None else state
        board = self.repository.current("storyboard", state=state)
        shot = (
            next((s for s in board["content"]["shots"] if s["shot_id"] == shot_id), None)
            if board
            else None
        )
        if shot is None:
            return []
        catalog_id = board["dependency_refs"]["asset_catalog"]
        catalog = self.repository.record(catalog_id, state=state)
        entities = {entity["asset_id"]: entity for entity in catalog["content"]["entities"]}
        bundle = self.repository.current("prompt_bundle", state=state)
        compiled = (
            bundle is not None
            and bundle["content"]["storyboard_version_id"] == board["id"]
            and self.repository.is_current(bundle["id"], state=state)
        )
        spec_refs = {}
        if compiled:
            compiled_shot = next(s for s in bundle["content"]["shots"] if s["shot_id"] == shot_id)
            spec_refs = {
                (ref["asset_id"], ref["variant_id"]): ref["spec_version_id"]
                for ref in compiled_shot.get("asset_refs", [])
            }
        result, seen = [], set()
        image_state = ImageGenerationStore(self.repository.output_dir).load()
        for kind, refs in (
            ("scene", [shot["scene_ref"]]),
            ("character", shot["characters"]),
            ("prop", shot["props"]),
        ):
            for ref in refs:
                asset_id, variant_id = ref["asset_id"], ref["variant_id"]
                identity = (asset_id, variant_id)
                entity = entities[asset_id]
                if identity in seen or entity.get("usage") == "voice_only":
                    continue
                seen.add(identity)
                key = f"{ASSET_KINDS[kind]}:{asset_id}:{variant_id}"
                spec_id = spec_refs.get(identity) if compiled else state["current"].get(key)
                spec = self.repository.record(spec_id, state=state) if spec_id else None
                if spec and (
                    spec["key"] != key
                    or spec["dependency_refs"].get("asset_catalog") != catalog_id
                    or not self.repository.is_current(spec["id"], state=state)
                ):
                    spec = None
                views = {
                    view["view_id"]: view
                    for view in (spec or {}).get("content", {}).get("views", [])
                }
                variant = next(v for v in entity["variants"] if v["variant_id"] == variant_id)
                result.append(
                    {
                        "kind": kind,
                        "asset_id": asset_id,
                        "variant_id": variant_id,
                        "name": entity["name"],
                        "variant_label": variant["label"],
                        "spec_id": spec["id"] if spec else None,
                        "views": [
                            {
                                "view_id": view_id,
                                "label": views.get(view_id, {}).get("label", label),
                                "positive_prompt": views.get(view_id, {}).get(
                                    "positive_prompt", ""
                                ),
                                "negative_prompt": views.get(view_id, {}).get(
                                    "negative_prompt", ""
                                ),
                                "available": bool(
                                    views.get(view_id, {}).get("positive_prompt", "").strip()
                                ),
                            }
                            for view_id, label in VIEWS[kind]
                        ],
                    }
                )
                if spec:
                    for view in result[-1]["views"]:
                        if view["view_id"] in views:
                            view["image"] = image_for_view(
                                self.repository.output_dir,
                                catalog_id,
                                spec,
                                views[view["view_id"]],
                                state=image_state,
                            )
        return result

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
        fact_table_id: str | None = None,
        upstream_ids: dict[str, str] | None = None,
        frozen_rules: JsonObject | None = None,
        video_inputs: JsonObject | None = None,
        current_video_prompt: str = "",
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
        video_task = stage == "video_prompt"
        candidate = self.repository.record(target_id, state=state) if target_id else None
        if stage in {"shot_prompts", "video_prompt"}:
            require(candidate is not None, "INVALID_INPUT", "请选择需补充提示词的版本")
            kind = candidate["kind"]
            require(
                kind == "prompt_bundle",
                "INVALID_INPUT",
                "提示词目标类型不匹配",
            )
            require(
                self.repository.is_current(candidate["id"], state=state),
                "STALE_INPUT",
                "目标内容已被替换，请重新生成",
            )
            deps = deepcopy(candidate["dependency_refs"])
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
                "config": self.repository.record(board["dependency_refs"]["config"], state=state)[
                    "content"
                ],
            }
            if stage == "video_prompt":
                from .video_prompt import validate_video_inputs

                require(
                    video_inputs is not None and shot_ids is not None and len(shot_ids) == 1,
                    "INVALID_INPUT",
                    "请选择一个镜头和实际参考图片",
                )
                validate_video_inputs(self.repository.output_dir, video_inputs)
                selected = [s for s in board["content"]["shots"] if s["shot_id"] in shot_ids]
                require(len(selected) == 1, "INVALID_INPUT", "镜头不在当前分镜中")
                selected_refs = next(
                    s["asset_refs"]
                    for s in candidate["content"]["shots"]
                    if s["shot_id"] == shot_ids[0]
                )
                used = {r["spec_version_id"] for r in selected_refs}
                context["storyboard"] = {**board, "content": {"shots": deepcopy(selected)}}
                context["asset_specs"] = [
                    {
                        **spec,
                        "content": {
                            k: v
                            for k, v in spec["content"].items()
                            if k not in {"views", "positive_prompt", "negative_prompt"}
                        },
                    }
                    for spec in context["asset_specs"]
                    if spec["id"] in used
                ]
                context["video_inputs"] = deepcopy(video_inputs)
                context["current_video_prompt"] = current_video_prompt
                context["shot_assets"] = self.shot_assets(shot_ids[0], state=state)
                # Only the selected image's actual view needs a description, not every asset sheet.
                catalog_id = board["dependency_refs"]["asset_catalog"]
                for image in context["video_inputs"]["images"]:
                    source = image["asset_source"]
                    asset = next(
                        (
                            a
                            for a in context["shot_assets"]
                            if a["asset_id"] == source.get("asset_id")
                            and a["variant_id"] == source.get("variant_id")
                            and source.get("catalog_id") == catalog_id
                        ),
                        None,
                    )
                    image["asset_description"] = (
                        {
                            "name": asset["name"],
                            "state": asset["variant_label"],
                            "view": next(
                                (
                                    v["label"]
                                    for v in asset["views"]
                                    if v["view_id"] == source.get("view_id")
                                ),
                                "未确认视图",
                            ),
                        }
                        if asset
                        else None
                    )
                context["shot_assets"] = [
                    {k: v for k, v in a.items() if k != "views"} for a in context["shot_assets"]
                ]
                # The binding itself contains only the original input snapshot, not display labels.
                context["video_input_binding"] = deepcopy(video_inputs)
                from .video_context import prepare_evidence

                source = self.repository.record(article["source_id"], state=state)
                prepare_evidence(
                    context, board, source["content"]["normalized_text"], self.max_context_chars
                )
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
                require(
                    state["current"].get(key) == record["id"]
                    and self.repository.is_current(record["id"], state=state),
                    "STALE_INPUT",
                    "来源已被替换，请使用当前内容重新生成",
                )
                context[key] = record
                deps[key] = record["id"]
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
                and self.repository.is_current(candidate["id"], state=state),
                "STALE_INPUT",
                "请先选择有效的待修订版本",
            )
            require(
                isinstance(asks, list) and any(isinstance(a, str) and a.strip() for a in asks),
                "INVALID_INPUT",
                "请填写具体修改意见",
            )
            require(candidate["dependency_refs"] == deps, "STALE_INPUT", "待修订稿的来源已变化")
        if (
            stage not in {"novel_copy", "video_prompt"}
            and len(encode(context["article"])) > self.max_context_chars // 2
        ):
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
        rules = frozen_rules or freeze_stage_rules(
            self.repository, stage, config["content"], state=state
        )
        instruction, skill_id = rules["instruction"], rules["skill_id"]
        if shot_ids:
            context["selected_shot_ids"] = shot_ids
        prompt = stage_prompt(
            stage,
            context,
            instruction=instruction,
            candidate=candidate,
            asks=asks,
            entity=entity,
            kind=kind,
        )
        if video_task:
            from .video_context import check_budget

            check_budget(prompt, self.max_context_chars, "生成", code="INPUT_TOO_LARGE")
        require(
            len(prompt) <= self.max_context_chars,
            "INPUT_TOO_LARGE",
            f"当前输入约 {len(prompt)} 字符，超过本次 {self.max_context_chars} 字符容量；请缩小选区或确认分块计划",
        )
        return {
            **({"max_context_chars": self.max_context_chars} if video_task else {}),
            "selection_snapshot": {key: state["current"].get(key) for key in deps},
            "stage": stage,
            "max_retries": self.max_retries,
            "mode": mode,
            "kind": kind,
            "key": key,
            "model": self.model,
            "model_snapshot": capture_model_snapshot(self.model),
            "review_model_snapshot": model_contracts.capture_review_model_snapshot(),
            "request_id": request_id or identity("request"),
            "expected_revision": state["revision"],
            "input_refs": deps,
            "context": context,
            "prompt": prompt,
            "prompt_ref": rules["prompt_ref"],
            "requirements": {
                "instruction": instruction,
                "asks": asks or [],
                "mode": mode,
                "skill_id": skill_id,
            },
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
        on_request: RequestRecorder | None = None,
        on_context: Callable[[TaskContext], None] | None = None,
        task_id: str | None = None,
        resume: bool = False,
    ) -> JsonObject:
        from .runtime import execute_stage

        return execute_stage(
            self,
            command,
            token=token or CancelToken(),
            report=report or (lambda *args: None),
            on_context=on_context,
            on_request=on_request,
            task_id=task_id,
            resume=resume,
        )

    def _validate_output(self, command: JsonObject, content: JsonObject) -> JsonObject:
        state = self.repository.load()
        kind = command["kind"]
        if command["stage"] == "video_prompt":
            from .video_context import merge_result

            original = self.repository.record(command["candidate_id"], state=state)["content"]
            content = merge_result(content, original, command["shot_ids"][0])
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
        if command["stage"] == "video_prompt":
            from .video_prompt import validate_video_inputs

            validate_video_inputs(
                self.repository.output_dir, command["context"]["video_input_binding"]
            )
            selected = set(command["shot_ids"])
            old = {s["shot_id"]: s for s in original["shots"]}
            allowed = {"video_motion_prompt", "video_prompt_binding"}
            for shot in content["shots"]:
                if shot["shot_id"] not in selected:
                    continue
                require(
                    {k: v for k, v in shot.items() if k not in allowed}
                    == {k: v for k, v in old[shot["shot_id"]].items() if k not in allowed},
                    "INVALID_OUTPUT",
                    "视频提示词生成不能修改镜头画面文字或其他内容",
                )
                text(shot.get("video_motion_prompt"), "完整视频提示词")
                import re

                valid_refs = {
                    image["reference"].lower()
                    for image in command["context"]["video_inputs"]["images"]
                    if image["role"] == "reference"
                }
                require(
                    all(
                        f"<picture {int(number)}>" in valid_refs
                        for number in re.findall(
                            r"<Picture\s+(\d+)>", shot["video_motion_prompt"], re.IGNORECASE
                        )
                    ),
                    "INVALID_OUTPUT",
                    "视频提示词引用了未选择的参考图片编号",
                )
                shot["video_prompt_binding"] = deepcopy(command["context"]["video_input_binding"])
        return content

    def compile_storyboard_prompts(
        self, *, expected_revision: int, state: JsonObject | None = None
    ) -> ArtifactRecord:
        if state is None:
            with self.repository.transaction(expected_revision) as current:
                return self.compile_storyboard_prompts(
                    expected_revision=expected_revision, state=current
                )
        storyboard = self.repository.current("storyboard", state=state)
        require(
            storyboard is not None and self.repository.is_current(storyboard["id"], state=state),
            "STALE_INPUT",
            "请先保存有效分镜",
        )
        specs = [
            self.repository.record(ref, state=state)
            for key, ref in state["current"].items()
            if key.split(":")[0] in ASSET_KINDS.values()
        ]
        require(
            all(self.repository.is_current(s["id"], state=state) for s in specs),
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
        return self.repository._save(state, "prompt_bundle", content, deps)


class WorkspaceWork(TaskTracker):
    """TaskRunner-compatible work; captures input before leaving the GUI thread."""

    def __init__(
        self,
        repository: ProjectRepository,
        command: JsonObject,
        *,
        transport: TextTransport | None = None,
        token: CancelToken | None = None,
    ) -> None:
        super().__init__(
            ACTION_NAMES.get(command["stage"], "内容生成"), target=command_target(command)
        )
        self.output_dir = repository.output_dir
        self.command = deepcopy(command)
        self.token = token or CancelToken()
        self.transport = transport

    def __call__(self, report: ProgressReporter) -> JsonObject:
        return WorkflowService(
            ProjectRepository(self.output_dir), self.command["model"], transport=self.transport
        ).execute(self.command, token=self.token, report=report, on_context=self.accept_context)
