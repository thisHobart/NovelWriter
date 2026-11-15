"""User-facing creation tasks: one action coordinates internal text stages."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from core.generation.cancellation import CancelToken

from .contracts import JsonObject, ProgressReporter, TextTransport
from .feedback import ACTION_NAMES, TaskTracker, failure_feedback, review_message
from .models import (
    ASSET_KINDS,
    WorkspaceError,
    capture_model_snapshot,
    encode,
    fingerprint,
    identity,
    require,
    timestamp,
    validate_model_snapshot,
)
from .repository import ProjectRepository
from .storage import atomic_write_text
from .validation import local_issues
from .workflow import WorkflowService


class CreationWork(TaskTracker):
    def __init__(
        self,
        repository: ProjectRepository,
        command: JsonObject,
        *,
        transport: TextTransport | None = None,
    ) -> None:
        super().__init__(ACTION_NAMES[command["stage"]])
        self.update_context(
            total=2 if command["stage"] == "storyboard_task" else len(command["items"])
        )
        self.output_dir = repository.output_dir
        self.command = deepcopy(command)
        self.transport = transport
        self.token = CancelToken()

    def __call__(self, report: ProgressReporter) -> JsonObject:
        repository = ProjectRepository(self.output_dir)
        task_path = repository.root / "tasks" / (self.command["request_id"] + ".json")
        if task_path.exists():
            try:
                previous = json.loads(task_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise WorkspaceError("INVALID_TASK", "生成任务记录损坏，未重新发送") from exc
            require(
                previous["command_hash"] == fingerprint(self.command),
                "REQUEST_KEY_CONFLICT",
                "任务编号不能用于不同内容",
            )
            if previous["status"] == "succeeded":
                state = repository.load()
                require(
                    all(ref in state["records"] for ref in previous["outputs"]),
                    "OUTPUT_REPLACED",
                    "这次任务的内容已被替换，请使用当前内容或重新生成",
                )
                return previous["result"]
            raise WorkspaceError(
                "TASK_STOPPED", "这个任务已经停止，已完成内容保留；请从当前页面重新生成未完成内容"
            )
        task = {
            "command": self.command,
            "command_hash": fingerprint(self.command),
            "status": "running",
            "calls_used": 0,
            "created_at": timestamp(),
            "outputs": [],
        }
        atomic_write_text(task_path, encode(task))
        try:
            result = self.execute_task(repository, report, task, task_path)
            self.update_context(phase="保存任务记录")
            task.pop("command", None)
            task.update(status="succeeded", result=result, ended_at=timestamp())
            atomic_write_text(task_path, encode(task))
            return result
        except Exception as exc:
            task.pop("command", None)
            task.update(
                status="cancelled" if self.token.cancelled else "failed",
                error=type(exc).__name__,
                feedback=failure_feedback(self.task_context, exc),
                ended_at=timestamp(),
            )
            try:
                atomic_write_text(task_path, encode(task))
            except OSError as storage_error:
                exc.add_note(f"任务状态保存失败：{type(storage_error).__name__}")
            raise

    def execute_task(
        self,
        repository: ProjectRepository,
        report: ProgressReporter,
        task: JsonObject,
        task_path: Path,
    ) -> JsonObject:
        service = WorkflowService(repository, self.command["model"], transport=self.transport)
        results = []
        frozen = deepcopy(self.command["input_refs"])

        def verify_inputs() -> None:
            if self.command["model_snapshot"] is not None:
                validate_model_snapshot(self.command["model_snapshot"])
            state = repository.load()
            retained = set(state["current"].values())
            self.update_context(completed=sum(ref in retained for ref in task["outputs"]))
            require(
                all(state["current"].get(key) == value for key, value in frozen.items()),
                "STALE_INPUT",
                "输入在生成期间发生变化，请使用当前内容重新生成",
            )
            self.token.raise_if_cancelled()

        def execute(command: JsonObject) -> JsonObject:
            verify_inputs()

            def record_call(sent: str, response: str, error: str | None) -> None:
                task["calls_used"] += 1
                atomic_write_text(task_path, encode(task))

            result = service.execute(
                command,
                token=self.token,
                report=report,
                on_request=record_call,
                on_context=self.accept_context,
            )
            results.append(result)
            record = repository.record(result["version_id"])
            task["outputs"].append(record["id"])
            atomic_write_text(task_path, encode(task))
            frozen[record["key"]] = record["id"]
            for key, ref in result.get("invalidated_refs", {}).items():
                if frozen.get(key) == ref:
                    frozen[key] = None
            require(
                result["selected"],
                "TARGET_VERSION_CONFLICT",
                "当前内容已被修改，本次候选不替换当前内容",
            )
            self.update_context(completed=self.task_context.completed + 1, saved=False)
            return result

        verify_inputs()
        if self.command["stage"] == "storyboard_task":
            catalog = execute(self.command["first_command"])
            frozen["asset_catalog"] = catalog["version_id"]
            execute(service.prepare("storyboard"))
        else:
            entities = {
                e["asset_id"]: e
                for e in repository.record(self.command["catalog_id"])["content"]["entities"]
            }
            for item in self.command["items"]:
                entity = entities[item["entity_id"]]
                self.update_context(target=entity["name"], phase="准备", saved=False)
                verify_inputs()
                report("正在生成资产提示词…", -1)
                execute(
                    service.prepare(
                        "asset_spec",
                        **item,
                        upstream_ids={"asset_catalog": self.command["catalog_id"]},
                    )
                )
            verify_inputs()
            self.update_context(target="镜头提示词", phase="编排", saved=False)
            report("正在编排镜头提示词…", -1)
            service.compile_storyboard_prompts(expected_revision=repository.load()["revision"])
        unavailable = [r for r in results if r["review_status"] in {"unavailable", "incomplete"}]
        issues = [r for r in results if r["review_status"] == "issues"]
        summaries = []
        for status, group, label in (
            ("unavailable", unavailable, "检查未完成"),
            ("issues", issues, "检查发现问题"),
        ):
            if group:
                feedback = group[0].get("feedback") or {}
                summaries.append(
                    f"{len(group)}项{label}：{feedback.get('target', '')} "
                    + review_message(status, feedback)
                )
        return {
            "status": "succeeded",
            "review_status": "unavailable"
            if unavailable
            else "issues"
            if issues
            else "passed"
            if results
            else "not_requested",
            "version_id": results[-1]["version_id"] if results else None,
            "calls": task["calls_used"],
            "review_summary": "；".join(summaries)
            or ("自动检查通过" if results else "已完成提示词编排，未额外进行AI审阅"),
        }


def prepare_storyboard_task(repository: ProjectRepository, model: str) -> JsonObject:
    first = WorkflowService(repository, model).prepare("asset_catalog")
    state = repository.load()
    pointers = {
        **first["selection_snapshot"],
        **{key: state["current"].get(key) for key in ("storyboard", "asset_catalog")},
    }
    return {
        "stage": "storyboard_task",
        "request_id": identity("creation"),
        "model": model,
        "input_refs": pointers,
        "first_command": first,
        "model_snapshot": first["model_snapshot"],
    }


def prepare_prompt_task(
    repository: ProjectRepository,
    model: str,
    *,
    asset_key: str | None = None,
) -> JsonObject:
    state = repository.load()
    board = repository.current("storyboard", state=state)
    require(board is not None, "MISSING_UPSTREAM", "请先生成分镜")
    require(
        repository.is_current(board["id"], state=state),
        "STALE_INPUT",
        "当前分镜来源已变化，请重新生成分镜",
    )
    catalog = repository.record(board["dependency_refs"]["asset_catalog"], state=state)
    items = []
    for entity in catalog["content"]["entities"]:
        if entity.get("usage") == "voice_only":
            continue
        for variant in entity["variants"]:
            kind = ASSET_KINDS[entity["kind"]]
            key = f"{kind}:{entity['asset_id']}:{variant['variant_id']}"
            existing = repository.current(key, state=state)
            if asset_key and key != asset_key:
                continue
            if (
                asset_key
                or not existing
                or existing["dependency_refs"]["asset_catalog"] != catalog["id"]
                or any(i["severity"] == "error" for i in local_issues(kind, existing["content"]))
            ):
                items.append(
                    {
                        "kind": kind,
                        "entity_id": entity["asset_id"],
                        "variant_id": variant["variant_id"],
                    }
                )
    require(
        state["current"].get("asset_catalog") == catalog["id"],
        "STALE_INPUT",
        "分镜与当前资产目录不一致，请重新生成分镜",
    )
    # 每项资产独立执行质量修复循环，不设置整轮调用次数和时长上限。
    return {
        "stage": "prompt_task",
        "catalog_id": catalog["id"],
        "request_id": identity("creation"),
        "model": model,
        "input_refs": {
            "storyboard": board["id"],
            "asset_catalog": state["current"].get("asset_catalog"),
            "config": state["current"].get("config"),
            "selection": state["current"].get("selection"),
            **{
                f"{item['kind']}:{item['entity_id']}:{item['variant_id']}": state["current"].get(
                    f"{item['kind']}:{item['entity_id']}:{item['variant_id']}"
                )
                for item in items
            },
            **{
                key: state["current"].get(key)
                for key in state["current"]
                if key.split(":")[0] in ASSET_KINDS.values()
            },
        },
        "items": items,
        "model_snapshot": capture_model_snapshot(model) if items else None,
    }
