"""Durable, latest-only checkpoints for the short-video text executor."""

from __future__ import annotations

import logging
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict
from typing import TYPE_CHECKING

import httpx
from openai import APIError

from core.common.cancellation import CancelToken, GenerationCancelled

from . import models as model_contracts
from .contracts import (
    ArtifactRecord,
    JsonObject,
    ModelRequest,
    ProgressReporter,
    RequestRecorder,
    TextTransport,
)
from .feedback import (
    ACTION_NAMES,
    TaskContext,
    TaskTracker,
    command_target,
    concise,
    failure_feedback,
    readable_fields,
)
from .llm import RecordingError, ScriptLLM
from .llm_trace import trace_event
from .models import (
    WorkspaceError,
    encode,
    fingerprint,
    format_error_feedback,
    identity,
    parse_object,
    require,
    timestamp,
    validate_model_snapshot,
)
from .prompts import default_prompt_ref
from .review import QualityCheckResult, check_candidate
from .video_context import validate_video_command, video_command_ready

if TYPE_CHECKING:
    from .repository import ProjectRepository
    from .workflow import WorkflowService

RUNTIME_VERSION = 2
logger = logging.getLogger(__name__)
PHASES = {
    "request": "生成",
    "validate": "结构与引用校验",
    "review": "自动审阅",
    "decide": "处理审阅结果",
    "commit": "选用结果",
}


def checkpoint_phase(cp: JsonObject) -> str:
    if cp["step"] == "request":
        return "格式修复" if cp["format_attempt"] else "质量修订" if cp["revision"] else "生成"
    if cp["step"] == "review" and "review_repair" in cp:
        return "审阅格式修复"
    return PHASES[cp["step"]]


def family(stage: str) -> str:
    return {
        "asset_spec": "prompt_task",
        "storyboard": "storyboard_task",
        "asset_catalog": "storyboard_task",
    }.get(stage, stage)


def task_valid(state: JsonObject, task: JsonObject) -> bool:
    return (
        bool(task.get("resumable"))
        and task.get("runtime_version") == RUNTIME_VERSION
        and (task["stage"] != "video_prompt" or video_command_ready(task.get("command", {})))
        and all(state["current"].get(k) == v for k, v in task["expected"].items())
    )


def invalidation_reason(state: JsonObject, task: JsonObject) -> str:
    if task.get("runtime_version") != RUNTIME_VERSION:
        return "运行格式已变化，请重新生成"
    labels = {
        "source": "原文",
        "selection": "原文范围",
        "config": "配置或Skill",
        "novel_copy": "小说推文",
        "video_copy": "视频文案",
        "storyboard": "分镜",
        "asset_catalog": "资产目录",
        "prompt_bundle": "镜头提示词",
    }
    changed = [
        "阶段提示词" if k.startswith("prompt_template:") else labels.get(k, "资产提示词")
        for k, v in task["expected"].items()
        if state["current"].get(k) != v
    ]
    return (
        "、".join(dict.fromkeys(changed)) + "已变化，请重新生成"
        if changed
        else "任务输入不完整，请重新生成"
    )


def invalidate(state: JsonObject, task: JsonObject, reason: str) -> None:
    task.update(status="invalidated", resumable=False, reason=reason, ended_at=timestamp())
    task.pop("command", None)
    task.pop("expected", None)
    task.pop("items", None)
    for run in state["runs"]:
        if run.get("task_id") == task["id"] and run["status"] != "succeeded":
            run.update(status="invalidated", output_refs=[])
            run.pop("checkpoint", None)
            run.pop("checkpoint_hash", None)


def start_task(repository: ProjectRepository, command: JsonObject) -> JsonObject:
    """Admission is durable and only happens while holding the generation lock."""
    validate_video_command(command)
    with repository.transaction() as state:
        task_id = command["request_id"]
        previous = state["tasks"].get(task_id)
        if previous:
            require(
                previous["command_hash"] == fingerprint(command),
                "REQUEST_KEY_CONFLICT",
                "任务编号不能用于不同内容",
            )
            return deepcopy(previous)
        expected = deepcopy(command.get("selection_snapshot", command["input_refs"]))
        if "key" in command:
            expected[command["key"]] = command["selected_before"]
        stages = (
            ["asset_catalog", "storyboard"]
            if command["stage"] == "storyboard_task"
            else ["asset_spec"]
            if command["stage"] == "prompt_task"
            else [command["stage"]]
        )
        for stage in stages:
            key = f"prompt_template:{stage}"
            rules = (
                command["first_command"]
                if stage == "asset_catalog" and command["stage"] == "storyboard_task"
                else command.get("stage_rules", command)
            )
            ref = rules["prompt_ref"]
            expected[key] = None if ref == default_prompt_ref(stage) else ref
        require(
            all(state["current"].get(k) == v for k, v in expected.items()),
            "STALE_INPUT",
            "任务输入已变化，请重新生成",
        )
        for old in state["tasks"].values():
            if old["family"] == family(command["stage"]) and old.get("resumable"):
                invalidate(state, old, "已被新生成任务替代")
        task = {
            "id": task_id,
            "runtime_version": RUNTIME_VERSION,
            "family": family(command["stage"]),
            "stage": command["stage"],
            "command": deepcopy(command),
            "command_hash": fingerprint(command),
            "expected": expected,
            "status": "running",
            "resumable": True,
            "outputs": [],
            "calls_used": 0,
            "completed": 0,
            "created_at": timestamp(),
            "items": {},
        }
        state["tasks"][task_id] = task
    return deepcopy(task)


def validate_task(repository: ProjectRepository, task_id: str, *, model: bool = True) -> JsonObject:
    state = repository.load()
    task = state["tasks"][task_id]
    require(
        task.get("resumable") and task_valid(state, task),
        "TARGET_VERSION_CONFLICT",
        task.get("reason") or invalidation_reason(state, task),
    )
    if model and task["command"].get("model_snapshot") is not None:
        validate_model_snapshot(task["command"]["model_snapshot"])
    if model and task["command"].get("review_model_snapshot") is not None:
        model_contracts.validate_review_model_snapshot(task["command"]["review_model_snapshot"])
    return task


def finish_failure(
    repository: ProjectRepository, task_id: str, exc: Exception, context: TaskContext
) -> None:
    """Keep the original exception, including when recording its failure also fails."""
    try:
        with repository.transaction() as state:
            task = state["tasks"][task_id]
            if task["status"] == "invalidated":
                return
            paused = isinstance(exc, GenerationCancelled)
            retryable = paused or isinstance(
                exc,
                (APIError, httpx.HTTPError, TimeoutError, ConnectionError, OSError, RecordingError),
            )
            if isinstance(exc, WorkspaceError):
                retryable = exc.code in {
                    "MODEL_CONFIGURATION_CHANGED",
                    "MODEL_CONFIGURATION_MISSING",
                    "REVIEW_CONFIGURATION_CHANGED",
                    "REVIEW_CONFIGURATION_MISSING",
                } or (exc.code == "CONTEXT_TOO_LARGE" and task["stage"] == "video_prompt")
            task.update(
                status="paused" if paused else "failed",
                resumable=retryable,
                feedback=failure_feedback(context, exc),
                ended_at=timestamp(),
            )
            for run in state["runs"]:
                if run.get("task_id") == task_id and run["status"] == "running":
                    run.update(
                        status=task["status"], feedback=task["feedback"], error=type(exc).__name__
                    )
                    if not retryable:
                        run.pop("checkpoint", None)
                        run.pop("checkpoint_hash", None)
                        run["output_refs"] = []
            if not retryable:
                task.pop("command", None)
                task.pop("items", None)
                task.pop("expected", None)
    except (OSError, WorkspaceError) as storage_error:
        exc.add_note(f"任务状态未保存：{type(storage_error).__name__}")


def sync_failure_context(
    tracker: TaskTracker, repository: ProjectRepository, task_id: str, exc: Exception
) -> None:
    try:
        state = repository.load()
        task = state["tasks"][task_id]
        runs = [r for r in state["runs"] if r.get("task_id") == task_id]
        last = runs[-1] if runs else {}
        saved = bool(last.get("checkpoint", {}).get("candidate"))
        if not tracker.task_context.total:
            saved = saved or any(
                ref in state["current"].values() for ref in last.get("output_refs", [])
            )
        tracker.update_context(resumable=task.get("resumable", False), saved=saved)
    except (OSError, WorkspaceError) as storage_error:
        tracker.update_context(resumable=False, saved=False)
        exc.add_note(f"无法读取已保存任务状态：{type(storage_error).__name__}")


def complete_task(state: JsonObject, task_id: str, result: JsonObject) -> None:
    task = state["tasks"][task_id]
    require(task_valid(state, task), "TARGET_VERSION_CONFLICT", "输入或目标内容已变化，请重新生成")
    task.update(status="succeeded", resumable=False, result=deepcopy(result), ended_at=timestamp())
    for key in ("command", "expected", "items"):
        task.pop(key, None)


def accept_output(
    state: JsonObject,
    task_id: str,
    record: ArtifactRecord,
    *,
    run_id: str | None = None,
    result: JsonObject | None = None,
) -> None:
    """Call inside the same transaction that selects the business output."""
    task = state["tasks"][task_id]
    task["expected"][record["key"]] = record["id"]
    if record["id"] not in task["outputs"]:
        task["outputs"].append(record["id"])
        if not (task["stage"] == "prompt_task" and record["kind"] == "prompt_bundle"):
            task["completed"] += 1
    if run_id:
        task["items"][run_id] = deepcopy(result)


def get_recovery(repository: ProjectRepository, stage: str) -> JsonObject | None:
    state = repository.load()
    tasks = [t for t in state["tasks"].values() if t["family"] == family(stage)]
    if not tasks:
        return None
    task = deepcopy(tasks[-1])
    if not task.get("resumable") and task["status"] != "invalidated":
        return None
    task["valid"] = bool(task.get("resumable") and task_valid(state, task))
    if not task["valid"] and "reason" not in task:
        task["reason"] = invalidation_reason(state, task)
    task["phase"] = (
        "镜头提示词编排"
        if task["stage"] == "prompt_task"
        and task.get("completed") == len(task.get("command", {}).get("items", []))
        else "准备"
    )
    task["target"] = ACTION_NAMES.get(task["stage"], "当前任务")
    task["unknown"] = False
    task["candidate_saved"] = False
    task["response_saved"] = False
    for run in state["runs"]:
        if run.get("task_id") != task["id"] or "checkpoint" not in run:
            continue
        cp = run["checkpoint"]
        task["phase"] = checkpoint_phase(cp)
        task["candidate_saved"] = bool(cp.get("candidate"))
        task["response_saved"] = cp.get("response") is not None
        task["target"] = command_target(cp["command"])
        task["unknown"] = any(c.get("state") in {"sending", "unknown"} for c in run["calls"][-1:])
    return task


def recovery_message(task: JsonObject) -> str:
    text = f"未完成任务，已完成 {task.get('completed', 0)} 项；可从{task['target']} · {task['phase']}继续。"
    if task.get("candidate_saved"):
        text += "候选已保存。"
    elif task.get("response_saved"):
        text += "模型回复已保存。"
    if task.get("feedback"):
        text += task["feedback"]["summary"] + "。"
    if task["unknown"]:
        text += "上次请求未取得完整回复；继续将重试，可能重复计费。"
    return text


class ResumeWork(TaskTracker):
    def __init__(
        self, repository: ProjectRepository, task_id: str, *, transport: TextTransport | None = None
    ) -> None:
        task = repository.load()["tasks"][task_id]
        super().__init__(ACTION_NAMES.get(task["stage"], "内容生成"))
        self.repository, self.task_id, self.transport = repository, task_id, transport
        self.output_dir = repository.output_dir
        self.token = CancelToken()

    def accept_context(self, context: TaskContext) -> None:
        self.task_context = context

    def __call__(self, report: ProgressReporter) -> JsonObject:
        task = validate_task(self.repository, self.task_id, model=False)
        if task["stage"] in {"storyboard_task", "prompt_task"}:
            from .creation import CreationWork

            work = CreationWork(
                self.repository, task["command"], transport=self.transport, resume=True
            )
            work.token = self.token
            work.on_context = self.accept_context
            try:
                return work(report)
            finally:
                self.task_context = work.task_context
        from .workflow import WorkflowService

        return WorkflowService(
            self.repository, task["command"]["model"], transport=self.transport
        ).execute(
            task["command"],
            token=self.token,
            report=report,
            on_context=self.accept_context,
            resume=True,
        )


class AgentExecution:
    """Each step is replayable; only the latest pending response/candidate is retained."""

    def __init__(
        self,
        service: WorkflowService,
        command: JsonObject,
        token: CancelToken,
        report: ProgressReporter,
        on_context: Callable[[TaskContext], None] | None,
        on_request: RequestRecorder | None,
        task_id: str,
    ) -> None:
        self.service, self.repository, self.command = service, service.repository, command
        self.token, self.report, self.on_context, self.on_request = (
            token,
            report,
            on_context,
            on_request,
        )
        self.task_id = task_id
        self.trace_call_id: str | None = None
        self.tracker = TaskTracker(
            ACTION_NAMES.get(command["stage"], "内容生成"), target=command_target(command)
        )

    def save(self) -> None:
        def update(run: JsonObject, state: JsonObject) -> None:
            task = state["tasks"][self.task_id]
            require(
                task.get("resumable") and task_valid(state, task),
                "TARGET_VERSION_CONFLICT",
                "输入或目标内容已变化，请重新生成",
            )
            run["checkpoint"] = deepcopy(self.cp)
            run["checkpoint_hash"] = fingerprint(self.cp)

        self.repository.update_run(self.run_id, update)

    def phase(self) -> None:
        name = checkpoint_phase(self.cp)
        self.tracker.update_context(
            phase=name, saved=bool(self.cp.get("candidate")), resumable=True, network_retries=0
        )
        if self.on_context:
            self.on_context(self.tracker.task_context)
        self.report(f"{self.tracker.task_context.target} · {name}…", -1)

    def trace_metadata(self) -> JsonObject:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "stage": self.command["stage"],
            "phase": checkpoint_phase(self.cp),
            "quality_revision": self.cp["revision"],
            "generation_format_attempt": self.cp["format_attempt"],
            "review_format_attempt": int("review_repair" in self.cp),
            "candidate_id": self.cp.get("candidate"),
            "call_id": self.trace_call_id,
        }

    def send(self, prompt: str, call_type: str) -> str:
        if self.cp.get("response") is not None:
            trace_event(
                "REUSED_RESPONSE",
                self.trace_metadata(),
                {"response_chars": len(self.cp["response"]), "new_api_call": False},
            )
            return self.cp["response"]
        compact = self.command["stage"] == "video_prompt"
        if compact:
            from .video_context import check_budget

            phase = (
                ("审阅格式修复" if "review_repair" in self.cp else "审阅")
                if call_type == "quality_check"
                else (
                    "格式修复"
                    if self.cp["format_attempt"]
                    else "修订"
                    if self.cp["revision"]
                    else "生成"
                )
            )
            check_budget(prompt, self.command["max_context_chars"], phase)
        call = {}
        network_attempt = -1
        purpose = "review" if call_type == "quality_check" else "generation"
        snapshot = self.command[
            "review_model_snapshot" if purpose == "review" else "model_snapshot"
        ]
        request: ModelRequest = prompt
        if compact and purpose == "generation":
            from .video_prompt import video_prompt_messages

            request = video_prompt_messages(
                self.repository.output_dir, prompt, self.command["context"]["video_input_binding"]
            )

        def begin() -> None:
            nonlocal call, network_attempt
            self.token.raise_if_cancelled()
            validate_task(self.repository, self.task_id)
            if self.command["stage"] == "video_prompt":
                from .video_prompt import validate_video_inputs

                validate_video_inputs(
                    self.repository.output_dir, self.command["context"]["video_input_binding"]
                )
            model_contracts.validate_review_model_snapshot(self.command["review_model_snapshot"])
            network_attempt += 1
            self.tracker.update_context(network_retries=network_attempt)
            if self.on_context:
                self.on_context(self.tracker.task_context)
            call = {
                "id": identity("call"),
                "type": call_type,
                "purpose": purpose,
                "model_snapshot": deepcopy(snapshot),
                "step_id": f"{self.run_id}:{self.cp['revision']}:{self.cp['format_attempt']}:{call_type}",
                "network_attempt": network_attempt,
                "format_attempt": (
                    int("review_repair" in self.cp)
                    if purpose == "review"
                    else self.cp["format_attempt"]
                ),
                "state": "sending",
                "created_at": timestamp(),
            }
            if purpose == "review":
                call["step_id"] += f":review_format_{call['format_attempt']}"
            if compact:
                from .video_context import request_sizes

                call.update(
                    input_chars=len(prompt),
                    context_sizes=request_sizes(prompt),
                    supplement_omitted=len(
                        self.command["context"]["supplement_manifest"]["omitted"]
                    ),
                )
            with self.repository.transaction() as state:
                require(
                    task_valid(state, state["tasks"][self.task_id]),
                    "TARGET_VERSION_CONFLICT",
                    "输入已变化，请重新生成",
                )
                run = next(r for r in state["runs"] if r["id"] == self.run_id)
                run["calls"].append(call)
                state["tasks"][self.task_id]["calls_used"] += 1
            self.trace_call_id = call["id"]

        def record(_request: ModelRequest, response: str, error: str | None) -> None:
            with self.repository.transaction() as state:
                run = next(r for r in state["runs"] if r["id"] == self.run_id)
                entry = next(c for c in run["calls"] if c["id"] == call["id"])
                entry.update(
                    state=(
                        "failed"
                        if error
                        in {
                            "RateLimitError",
                            "BadRequestError",
                            "AuthenticationError",
                            "PermissionDeniedError",
                            "NotFoundError",
                            "InternalServerError",
                            "APIStatusError",
                        }
                        else "unknown"
                    )
                    if error
                    else "received",
                    error=error,
                )
                if error is None and state["tasks"][self.task_id].get("resumable"):
                    self.cp["response"] = response
                    if call_type == "generation":
                        self.cp["step"] = "validate"
                    run["checkpoint"] = deepcopy(self.cp)
                    run["checkpoint_hash"] = fingerprint(self.cp)
            if self.on_request:
                self.on_request("", "", error)

        return ScriptLLM(
            snapshot.get("provider", self.command["model"]),
            purpose=purpose,
            token=self.token,
            transport=self.service.transport,
            record=record,
            before_request=begin,
            trace_context=lambda: {
                **self.trace_metadata(),
                "call_id": call.get("id"),
                "network_attempt": network_attempt,
            },
            on_retry=lambda error, attempt, maximum, delay: self.tracker.report_network_retry(
                error, attempt, maximum, delay, self.report, self.on_context
            ),
        )(request)

    def video_generation_prompt(self, *, repair: JsonObject | None = None) -> str:
        from .prompts import video_request
        from .video_context import local_result

        context = self.command["context"]
        reference = self.cp.get("candidate") or self.command["candidate_id"]
        draft = local_result(
            self.repository.record(reference)["content"], self.command["shot_ids"][0]
        )["video_motion_prompt"]
        if not self.cp.get("candidate") and context.get("current_video_prompt", "").strip():
            draft = context["current_video_prompt"]
        return video_request(
            context,
            instruction=self.command["requirements"]["instruction"],
            asks=self.command["requirements"]["asks"],
            draft=draft,
            feedback=self.cp.get("review", {}).get("feedback", ""),
            repair=repair,
        )

    def execute(self) -> JsonObject:
        from .workflow import MAX_FORMAT_RETRIES, normalize_catalog

        state = self.repository.load()
        existing = state["requests"].get(self.command["request_id"])
        if existing:
            run = next(r for r in state["runs"] if r["id"] == existing["run_id"])
            if run["status"] == "succeeded":
                require(bool(run["output_refs"]), "OUTPUT_REPLACED", "任务结果已被替换，请重新生成")
                return deepcopy(run["result"])
            require(
                run.get("task_id") == self.task_id and "checkpoint" in run,
                "TASK_STOPPED",
                "运行检查点缺失，无法继续，请重新生成",
            )
            self.command = deepcopy(run["checkpoint"]["command"])
            self.cp = deepcopy(run["checkpoint"])
            self.run_id = run["id"]
            self.repository.update_run(self.run_id, lambda r, _: r.update(status="running"))
        else:
            admitted = deepcopy(self.command)
            admitted["expected_revision"] = state["revision"]
            self.cp = {
                "command": deepcopy(self.command),
                "step": "request",
                "prompt": self.command["prompt"],
                "generation_prompt": self.command["prompt"],
                "format_attempt": 0,
                "revision": 0,
                "response": None,
                "candidate": None,
            }
            run, _ = self.repository.start_run(
                admitted,
                expected_revision=state["revision"],
                snapshot={
                    "input_refs": admitted["input_refs"],
                    "model": admitted["model"],
                    "prompt_ref": admitted["prompt_ref"],
                    "model_snapshot": admitted["model_snapshot"],
                    "review_model_snapshot": admitted["review_model_snapshot"],
                },
                task_id=self.task_id,
                checkpoint=self.cp,
            )
            self.run_id = run["id"]
        while True:
            self.phase()
            self.token.raise_if_cancelled()
            validate_task(self.repository, self.task_id)
            step = self.cp["step"]
            if step == "request":
                require(
                    self.cp["format_attempt"] <= MAX_FORMAT_RETRIES,
                    "INVALID_OUTPUT",
                    "格式修复次数已用尽；原稿保留",
                )
                require(
                    self.command["stage"] == "video_prompt"
                    or len(self.cp["prompt"]) <= self.service.max_context_chars,
                    "CONTEXT_TOO_LARGE",
                    "当前请求上下文超限",
                )
                self.send(self.cp["prompt"], "generation")
            elif step == "validate":
                try:
                    content = parse_object(self.cp["response"])
                    if self.command["stage"] == "asset_catalog":
                        content = normalize_catalog(content, self.command.get("previous_catalog"))
                    content = self.service._validate_output(self.command, content)
                except WorkspaceError as exc:
                    if exc.code not in {"INVALID_INPUT", "INVALID_OUTPUT"}:
                        raise
                    trace_event(
                        "GENERATION_PARSE_ERROR",
                        self.trace_metadata(),
                        {
                            "code": exc.code,
                            "error": str(exc),
                            "details": exc.details,
                            "will_repair": self.cp["format_attempt"] < MAX_FORMAT_RETRIES,
                        },
                    )
                    if self.cp["format_attempt"] >= MAX_FORMAT_RETRIES:
                        raise WorkspaceError(
                            "INVALID_OUTPUT", "未取得可用稿件：" + str(exc) + "；原稿保留"
                        ) from exc
                    repair_prompt = (
                        self.video_generation_prompt(
                            repair={
                                "error": format_error_feedback(exc),
                                "response": self.cp["response"],
                                "instruction": "仅修复JSON、字段、枚举或引用，不改动叙事",
                            }
                        )
                        if self.command["stage"] == "video_prompt"
                        else self.cp["generation_prompt"]
                        + "\n仅修复JSON、字段、枚举或引用，不改动叙事。错误："
                        + format_error_feedback(exc)
                        + "\n上次回复："
                        + encode({"response": self.cp["response"]})
                    )
                    self.cp.update(
                        step="request",
                        format_attempt=self.cp["format_attempt"] + 1,
                        prompt=repair_prompt,
                        response=None,
                    )
                    self.save()
                    continue
                from .video_context import local_result

                trace_event(
                    "PARSED_CANDIDATE",
                    self.trace_metadata(),
                    local_result(content, self.command["shot_ids"][0])
                    if self.command["stage"] == "video_prompt"
                    else content,
                )
                with self.repository.transaction() as state:
                    candidate = self.repository._save(
                        state,
                        self.command["kind"],
                        content,
                        self.command["input_refs"],
                        origin="model",
                        select=False,
                        metadata={"run_id": self.run_id, "prompt_ref": self.command["prompt_ref"]},
                    )
                    self.cp.update(
                        step="review",
                        candidate=candidate["id"],
                        response=None,
                        prompt="",
                        generation_prompt="",
                    )
                    self.cp.pop("review", None)
                    self.cp.pop("review_repair", None)
                    run = next(r for r in state["runs"] if r["id"] == self.run_id)
                    run.update(
                        checkpoint=deepcopy(self.cp),
                        checkpoint_hash=fingerprint(self.cp),
                        output_refs=[candidate["id"]],
                    )
            elif step == "review":
                candidate = self.repository.record(self.cp["candidate"])
                try:
                    result = (
                        self.service.quality_check(self.command, candidate)
                        if self.service.quality_check
                        else check_candidate(
                            self.command,
                            candidate,
                            lambda prompt: self.send(prompt, "quality_check"),
                            self.repository,
                            **(
                                {"repair": self.cp["review_repair"]}
                                if "review_repair" in self.cp
                                else {}
                            ),
                        )
                    )
                except WorkspaceError as exc:
                    trace_event(
                        "REVIEW_PARSE_ERROR",
                        self.trace_metadata(),
                        {
                            "code": exc.code,
                            "error": str(exc),
                            "details": exc.details,
                            "will_repair": exc.code in {"INVALID_INPUT", "INVALID_OUTPUT"}
                            and not self.service.quality_check
                            and self.cp.get("response") is not None
                            and "review_repair" not in self.cp,
                        },
                    )
                    if (
                        self.service.quality_check
                        or exc.code not in {"INVALID_INPUT", "INVALID_OUTPUT"}
                        or self.cp.get("response") is None
                    ):
                        raise
                    if "review_repair" in self.cp:
                        raise WorkspaceError(
                            "INVALID_OUTPUT", "未取得有效校验结果：" + str(exc) + "；原稿保留"
                        ) from exc
                    self.cp.update(
                        review_repair={
                            "error": format_error_feedback(exc),
                            "response": self.cp["response"],
                        },
                        response=None,
                    )
                    self.save()
                    continue
                if not result.passed:
                    logger.warning("%s：%s", self.tracker.task_context.target, result.feedback)
                trace_event("PARSED_REVIEW", self.trace_metadata(), asdict(result))
                self.cp.update(step="decide", review=asdict(result), response=None)
                self.cp.pop("review_repair", None)
                self.save()
            elif step == "decide":
                result = QualityCheckResult(**self.cp["review"])
                if result.passed or self.cp["revision"] >= self.command["max_retries"]:
                    self.cp["step"] = "commit"
                else:
                    latest = self.repository.record(self.cp["candidate"])
                    prompt = (
                        self.video_generation_prompt()
                        if self.command["stage"] == "video_prompt"
                        else self.command["prompt"]
                        + "\n根据检查反馈修订最新候选，保留原始任务与上下文，返回完整JSON：\n"
                        + encode({"candidate": latest["content"], "feedback": result.feedback})
                    )
                    self.cp.update(
                        step="request",
                        prompt=prompt,
                        generation_prompt=prompt,
                        format_attempt=0,
                        revision=self.cp["revision"] + 1,
                    )
                self.save()
            else:
                return self.commit()

    def commit(self) -> JsonObject:
        if self.command["stage"] == "video_prompt":
            from .video_prompt import validate_video_inputs

            validate_video_inputs(
                self.repository.output_dir, self.command["context"]["video_input_binding"]
            )
        review = QualityCheckResult(**self.cp["review"])
        feedback = {
            "phase": "自动审阅",
            "target": self.tracker.task_context.target,
            "summary": "自动检查通过"
            if review.passed
            else readable_fields(
                concise(review.feedback.splitlines()[0] if review.feedback else "检查发现问题")
            ),
            "issue_count": review.issue_count or (0 if review.passed else 1),
        }
        with self.repository.transaction() as state:
            task = state["tasks"][self.task_id]
            require(
                task_valid(state, task),
                "TARGET_VERSION_CONFLICT",
                "输入或当前内容已变化，请重新生成",
            )
            previous = dict(state["current"])
            latest = self.repository.record(self.cp["candidate"], state=state)
            state["current"][self.command["key"]] = latest["id"]
            result = {
                "run_id": self.run_id,
                "status": "succeeded",
                "version_id": latest["id"],
                "selected": True,
                "review_status": "passed" if review.passed else "issues",
                "feedback": feedback,
                "retries_used": self.cp["revision"],
                "invalidated_refs": {},
            }
            run = next(r for r in state["runs"] if r["id"] == self.run_id)
            run.update(
                **{
                    k: v
                    for k, v in result.items()
                    if k not in {"run_id", "version_id", "invalidated_refs"}
                },
                output_refs=[latest["id"]],
                review_result={**asdict(review), "candidate_id": latest["id"]},
                result=deepcopy(result),
                ended_at=timestamp(),
            )
            run.pop("checkpoint", None)
            run.pop("checkpoint_hash", None)
            accept_output(state, self.task_id, latest, run_id=self.run_id, result=result)
            # Prune before updating the expected pointers for our own invalidations.
            self.repository._discard_history(state, reconcile=False)
            result["invalidated_refs"] = {
                k: v for k, v in previous.items() if k not in state["current"]
            }
            for key, ref in result["invalidated_refs"].items():
                if task["expected"].get(key) == ref:
                    task["expected"][key] = None
            run["result"] = deepcopy(result)
            task["items"][self.run_id] = deepcopy(result)
            for item in task["items"].values():
                if (
                    isinstance(item, dict)
                    and item.get("command", {}).get("request_id") == self.command["request_id"]
                ):
                    item.clear()
                    item["result"] = deepcopy(result)
        trace_event("COMMITTED_RESULT", self.trace_metadata(), result)
        return result


def execute_stage(
    service: WorkflowService,
    command: JsonObject,
    *,
    token: CancelToken,
    report: ProgressReporter,
    on_context: Callable[[TaskContext], None] | None,
    on_request: RequestRecorder | None,
    task_id: str | None = None,
    resume: bool = False,
) -> JsonObject:
    """Standalone stages own a task/lock; composed stages share their parent's lock."""
    validate_video_command(command)
    require(
        type(command.get("max_retries")) is int and 0 <= command["max_retries"] <= 1,
        "INVALID_INPUT",
        "质量修订次数只能是0或1",
    )
    if task_id is not None:
        return AgentExecution(
            service, command, token, report, on_context, on_request, task_id
        ).execute()
    with service.repository.generation_lock():
        task = service.repository.load()["tasks"].get(command["request_id"])
        if task:
            require(
                task["command_hash"] == fingerprint(command),
                "REQUEST_KEY_CONFLICT",
                "任务编号不能用于不同内容",
            )
        if task and task["status"] == "succeeded":
            require(
                all(ref in service.repository.load()["records"] for ref in task["outputs"]),
                "OUTPUT_REPLACED",
                "任务结果已被替换，请重新生成",
            )
            return deepcopy(task["result"])
        if resume:
            task = validate_task(service.repository, command["request_id"], model=False)
        else:
            require(task is None, "TASK_STOPPED", "任务已停止，请使用继续生成")
            task = start_task(service.repository, command)
        with service.repository.transaction() as state:
            require(
                task_valid(state, state["tasks"][task["id"]]),
                "TARGET_VERSION_CONFLICT",
                "输入已变化，请重新生成",
            )
            state["tasks"][task["id"]]["status"] = "running"
        execution = AgentExecution(
            service, command, token, report, on_context, on_request, task["id"]
        )
        try:
            result = execution.execute()
            with service.repository.transaction() as state:
                complete_task(state, task["id"], result)
            return result
        except Exception as exc:
            finish_failure(service.repository, task["id"], exc, execution.tracker.task_context)
            sync_failure_context(execution.tracker, service.repository, task["id"], exc)
            if on_context:
                on_context(execution.tracker.task_context)
            raise
