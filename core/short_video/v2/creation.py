"""User-facing creation tasks: one action coordinates internal text stages."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from core.generation.cancellation import CancelToken

from .contracts import JsonObject, ProgressReporter, TextTransport
from .llm import BudgetExhausted, RequestBudget
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
from .workflow import STAGE_MAX_CALLS, WorkflowService


class CreationWork:
    def __init__(
        self,
        repository: ProjectRepository,
        command: JsonObject,
        *,
        transport: TextTransport | None = None,
    ) -> None:
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
                if self.command["stage"] == "prompt_task":
                    state = repository.load()
                    require(
                        all(ref in state["records"] for ref in previous["outputs"]),
                        "PROMPT_REPLACED",
                        "这次任务的提示词已被替换，请使用当前提示词或重新生成",
                    )
                return previous["result"]
            raise WorkspaceError(
                "TASK_STOPPED", "这个任务已经停止，已完成内容保留；请从当前页面重新生成未完成内容"
            )
        budget = RequestBudget(self.command["max_calls"], seconds=self.command["seconds"])
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
            result = self.execute_task(repository, budget, report, task, task_path)
            task.update(
                status="succeeded", result=result, calls_used=budget.calls, ended_at=timestamp()
            )
            atomic_write_text(task_path, encode(task))
            return result
        except BudgetExhausted:
            if not task["outputs"]:
                task.update(status="failed", calls_used=budget.calls)
                atomic_write_text(task_path, encode(task))
                raise
            result = {
                "status": "partial",
                "review_status": "incomplete",
                "calls": budget.calls,
                "version_id": task["outputs"][-1],
            }
            task.update(
                status="partial", result=result, calls_used=budget.calls, ended_at=timestamp()
            )
            atomic_write_text(task_path, encode(task))
            return result
        except Exception as exc:
            task.update(
                status="cancelled" if self.token.cancelled else "failed",
                error=type(exc).__name__,
                calls_used=budget.calls,
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
        budget: RequestBudget,
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
            require(
                all(state["current"].get(key) == value for key, value in frozen.items()),
                "STALE_INPUT",
                "输入在生成期间发生变化，已完成内容保留，请重新选择当前内容",
            )
            self.token.raise_if_cancelled()

        def execute(command: JsonObject) -> JsonObject:
            verify_inputs()
            result = service.execute(command, token=self.token, report=report, budget=budget)
            results.append(result)
            record = repository.record(result["version_id"])
            task["outputs"].append(record["id"])
            task["calls_used"] = budget.calls
            atomic_write_text(task_path, encode(task))
            frozen[record["key"]] = record["id"]
            require(
                result["selected"],
                "TARGET_VERSION_CONFLICT",
                "已生成候选，但当前稿已被修改，候选保留在历史版本",
            )
            return result

        verify_inputs()
        if self.command["stage"] == "storyboard_task":
            catalog = execute(self.command["first_command"])
            frozen["asset_catalog"] = catalog["version_id"]
            execute(service.prepare("storyboard", use_stale=self.command["use_stale"]))
        else:
            for item in self.command["items"]:
                verify_inputs()
                report("正在生成资产提示词…", -1)
                execute(
                    service.prepare(
                        "asset_spec",
                        **item,
                        use_stale=self.command["use_stale"],
                        upstream_ids={"asset_catalog": self.command["catalog_id"]},
                    )
                )
            verify_inputs()
            service.compile_storyboard_prompts(
                expected_revision=repository.load()["revision"], use_stale=self.command["use_stale"]
            )
        return {
            "status": "succeeded",
            "review_status": (
                "passed"
                if all(r["review_status"] == "passed" for r in results)
                else "issues"
                if any(r["review_status"] == "issues" for r in results)
                else "incomplete"
            )
            if results
            else "not_requested",
            "version_id": results[-1]["version_id"] if results else None,
            "calls": budget.calls,
        }


def prepare_storyboard_task(
    repository: ProjectRepository, model: str, *, use_stale: bool = False
) -> JsonObject:
    first = WorkflowService(repository, model).prepare("asset_catalog", use_stale=use_stale)
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
        "use_stale": use_stale,
        "max_calls": 2 * STAGE_MAX_CALLS,
        "seconds": 600,
    }


def prepare_prompt_task(
    repository: ProjectRepository,
    model: str,
    *,
    use_stale: bool = False,
    asset_key: str | None = None,
) -> JsonObject:
    state = repository.load()
    board = repository.current("storyboard", state=state)
    require(board is not None, "MISSING_UPSTREAM", "请先生成分镜")
    require(
        use_stale or repository.is_current(board["id"], state=state),
        "STALE_INPUT",
        "当前分镜基于旧内容，请选择重新生成或明确沿用",
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
        use_stale or state["current"].get("asset_catalog") == catalog["id"],
        "STALE_INPUT",
        "分镜使用另一版人物和场景，请重新生成分镜或先恢复其历史内容",
    )
    # Each item gets an Agent Loop with its own retry limit; the entire task also has a hard deadline.
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
        "use_stale": use_stale,
        "max_calls": max(1, STAGE_MAX_CALLS * len(items)),
        "seconds": min(1800, max(300, 300 * len(items))),
    }
