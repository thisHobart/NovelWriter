"""User-facing creation tasks: one action coordinates internal text stages."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy

from core.common.cancellation import CancelToken

from . import models as model_contracts
from .contracts import JsonObject, ProgressReporter, TextTransport
from .feedback import (
    ACTION_NAMES,
    UNREVIEWED_SUMMARY,
    TaskContext,
    TaskTracker,
    review_message,
)
from .models import (
    ASSET_KINDS,
    capture_model_snapshot,
    fingerprint,
    identity,
    require,
)
from .repository import ProjectRepository
from .validation import local_issues
from .workflow import WorkflowService, freeze_stage_rules


class CreationWork(TaskTracker):
    """One durable task owns the generation lock across all of its assets."""

    def __init__(
        self,
        repository: ProjectRepository,
        command: JsonObject,
        *,
        transport: TextTransport | None = None,
        resume: bool = False,
    ) -> None:
        super().__init__(ACTION_NAMES[command["stage"]])
        self.update_context(
            total=2 if command["stage"] == "storyboard_task" else len(command["items"])
        )
        self.output_dir = repository.output_dir
        self.command, self.transport, self.resume = deepcopy(command), transport, resume
        self.token = CancelToken()
        self.on_context = None

    def accept_context(self, context: TaskContext) -> None:
        super().accept_context(context)
        if self.on_context:
            self.on_context(self.task_context)

    def __call__(self, report: ProgressReporter) -> JsonObject:
        from .runtime import (
            complete_task,
            finish_failure,
            start_task,
            sync_failure_context,
            task_valid,
            validate_task,
        )

        repository = ProjectRepository(self.output_dir)
        task_id = self.command["request_id"]
        with repository.generation_lock():
            previous = repository.load()["tasks"].get(task_id)
            if previous:
                require(
                    previous["command_hash"] == fingerprint(self.command),
                    "REQUEST_KEY_CONFLICT",
                    "任务编号不能用于不同内容",
                )
            if previous and previous["status"] == "succeeded":
                require(
                    all(ref in repository.load()["records"] for ref in previous["outputs"]),
                    "OUTPUT_REPLACED",
                    "这次任务的内容已被替换，请重新生成",
                )
                return previous["result"]
            if self.resume:
                validate_task(repository, task_id, model=False)
            else:
                require(previous is None, "TASK_STOPPED", "任务已停止，请使用继续生成")
                start_task(repository, self.command)
            self.update_context(
                completed=repository.load()["tasks"][task_id].get("completed", 0), resumable=True
            )
            try:
                with repository.transaction() as state:
                    require(
                        task_valid(state, state["tasks"][task_id]),
                        "TARGET_VERSION_CONFLICT",
                        "输入已变化，请重新生成",
                    )
                    state["tasks"][task_id]["status"] = "running"
                result = self.execute_task(repository, report)
                with repository.transaction() as state:
                    complete_task(state, task_id, result)
                return result
            except Exception as exc:
                finish_failure(repository, task_id, exc, self.task_context)
                sync_failure_context(self, repository, task_id, exc)
                raise

    def execute_task(self, repository: ProjectRepository, report: ProgressReporter) -> JsonObject:
        from .runtime import accept_output, task_valid, validate_task

        task_id = self.command["request_id"]
        service = WorkflowService(repository, self.command["model"], transport=self.transport)

        def execute(item_key: str, prepare: Callable[[], JsonObject]) -> JsonObject:
            task = validate_task(repository, task_id)
            self.token.raise_if_cancelled()
            item = task["items"].get(item_key)
            if item and item.get("result"):
                return item["result"]
            if item is None:
                command = prepare()
                require(
                    command["review_model_snapshot"] == self.command["review_model_snapshot"],
                    "REVIEW_CONFIGURATION_CHANGED",
                    "校验模型配置已变化，请重新生成任务",
                )
                with repository.transaction() as state:
                    require(
                        task_valid(state, state["tasks"][task_id]),
                        "TARGET_VERSION_CONFLICT",
                        "输入已变化，请重新生成",
                    )
                    state["tasks"][task_id]["items"][item_key] = {"command": command}
            else:
                command = item["command"]
            result = service.execute(
                command,
                token=self.token,
                report=report,
                on_context=self.accept_context,
                task_id=task_id,
            )
            with repository.transaction() as state:
                require(
                    task_valid(state, state["tasks"][task_id]),
                    "TARGET_VERSION_CONFLICT",
                    "输入已变化，请重新生成",
                )
                state["tasks"][task_id]["items"][item_key] = {"result": result}
            self.update_context(
                completed=repository.load()["tasks"][task_id]["completed"], saved=False
            )
            return result

        results = []
        if self.command["stage"] == "storyboard_task":
            results.append(execute("catalog", lambda: deepcopy(self.command["first_command"])))
            results.append(
                execute(
                    "storyboard",
                    lambda: service.prepare("storyboard", frozen_rules=self.command["stage_rules"]),
                )
            )
        else:
            entities = {
                e["asset_id"]: e
                for e in repository.record(self.command["catalog_id"])["content"]["entities"]
            }
            for index, item in enumerate(self.command["items"]):
                self.update_context(
                    target=entities[item["entity_id"]]["name"], phase="准备", saved=False
                )
                results.append(
                    execute(
                        str(index),
                        lambda item=item: service.prepare(
                            "asset_spec",
                            **item,
                            upstream_ids={"asset_catalog": self.command["catalog_id"]},
                            frozen_rules=self.command["stage_rules"],
                        ),
                    )
                )
            validate_task(repository, task_id)
            self.token.raise_if_cancelled()
            self.update_context(target="镜头提示词", phase="编排", saved=False)
            report("正在编排镜头提示词…", -1)
            with repository.transaction() as state:
                task = state["tasks"][task_id]
                require(
                    task_valid(state, task),
                    "TARGET_VERSION_CONFLICT",
                    "镜头提示词或输入已变化，请重新生成",
                )
                if "compiled" not in task["items"]:
                    compiled = service.compile_storyboard_prompts(
                        expected_revision=state["revision"], state=state
                    )
                    accept_output(state, task_id, compiled)
                    task["items"]["compiled"] = {"result": compiled["id"]}
            self.update_context(completed=repository.load()["tasks"][task_id]["completed"])
        issues = [r for r in results if r["review_status"] == "issues"]
        unreviewed = [r for r in results if r["review_status"] == "unreviewed"]
        task = repository.load()["tasks"][task_id]
        return {
            "status": "succeeded",
            "review_status": "issues"
            if issues
            else "unreviewed"
            if unreviewed
            else "passed"
            if results
            else "not_requested",
            "version_id": task["outputs"][-1] if task["outputs"] else None,
            "calls": task["calls_used"],
            "review_summary": f"{len(issues)}项检查发现问题："
            + review_message("issues", issues[0].get("feedback"))
            if issues
            else f"{len(unreviewed)}项{UNREVIEWED_SUMMARY}"
            if unreviewed
            else "自动检查通过"
            if results
            else "已完成提示词编排，未额外进行AI审阅",
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
        "stage_rules": freeze_stage_rules(
            repository,
            "storyboard",
            repository.current("config", state=state)["content"],
            state=state,
        ),
        "model_snapshot": first["model_snapshot"],
        "review_model_snapshot": first["review_model_snapshot"],
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
        "stage_rules": freeze_stage_rules(
            repository,
            "asset_spec",
            repository.current("config", state=state)["content"],
            state=state,
        ),
        "catalog_id": catalog["id"],
        "request_id": identity("creation"),
        "model": model,
        "input_refs": {
            "storyboard": board["id"],
            "prompt_bundle": state["current"].get("prompt_bundle"),
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
        "review_model_snapshot": model_contracts.capture_review_model_snapshot() if items else None,
    }
