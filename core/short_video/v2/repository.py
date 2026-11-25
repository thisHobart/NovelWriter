"""Current-only creative content in an atomic manifest.

Opening a repository is read-only.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from copy import deepcopy
from pathlib import Path

from filelock import Timeout

from core.common.locks import project_lock

from .contracts import ArtifactRecord, JsonObject, OperationResult
from .models import (
    ACTIVE_RUNS,
    ID_PATTERN,
    RUN_STATUSES,
    WorkspaceError,
    content_key,
    encode,
    fingerprint,
    identity,
    reject_json_constant,
    require,
    timestamp,
)
from .storage import atomic_write_text
from .validation import SPEC_FIELDS, bind_view_prompts, validate_content

logger = logging.getLogger(__name__)

_shared = threading.local()


@contextmanager
def shared_project_reads() -> Iterator[None]:
    """Let one interface refresh read each project file once and share the result.

    Inside the period every reader on this thread gets the same parsed state, so readers must
    not change it. Each read still compares the file's modification time and size and reads
    again when they differ; writes always read their own copy and drop the shared one. Other
    threads (background tasks) are unaffected. Periods nest; the outermost one ends sharing.
    """
    outer = getattr(_shared, "states", None)
    if outer is None:
        _shared.states = {}
    try:
        yield
    finally:
        if outer is None:
            _shared.states = None


class ProjectRepository:
    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = str(Path(output_dir).resolve())
        self.root = Path(self.output_dir) / "short_video"
        self.path = self.root / "project_v2.json"

    @staticmethod
    def empty() -> JsonObject:
        return {
            "schema_version": 2,
            "runtime_schema_version": 1,
            "tasks": {},
            "revision": 0,
            "project_id": None,
            "records": {},
            "current": {},
            "confirmations": {},
            "reports": {},
            "runs": [],
            "requests": {},
            "exports": [],
        }

    def load(self) -> JsonObject:
        # Reads wait for the lock saves hold: while a read has the file open, Windows refuses a
        # save on another thread (the interface, another task) that swaps the file in.
        with self._read_lock():
            return self._load()

    def _read_lock(self) -> AbstractContextManager[object]:
        """The project lock; before the project folder exists there is nothing to wait for."""
        if not self.root.exists():
            return nullcontext()
        return project_lock(str(self.root / ".v2-project.lock"), timeout=5)

    def _load(self) -> JsonObject:
        states = getattr(_shared, "states", None)
        if states is None:
            return self._read()
        try:
            info = self.path.stat()
        except OSError:
            return self._read()
        stamp = (info.st_mtime_ns, info.st_size)
        cached = states.get(self._shared_key())
        if cached is not None and cached[0] == stamp:
            return cached[1]
        state = self._read()
        states[self._shared_key()] = (stamp, state)
        return state

    def _shared_key(self) -> str:
        return os.path.normcase(str(self.path))

    def _forget_shared(self) -> None:
        states = getattr(_shared, "states", None)
        if states is not None:
            states.pop(self._shared_key(), None)

    def _read(self) -> JsonObject:
        if not self.path.exists():
            return self.empty()
        try:
            state = json.loads(
                self.path.read_text(encoding="utf-8"), parse_constant=reject_json_constant
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise WorkspaceError("INVALID_PROJECT", "推广项目无法读取；原文件未修改") from exc
        require(
            isinstance(state, dict) and state.get("schema_version") == 2,
            "INVALID_PROJECT",
            "不支持的推广项目格式",
        )
        require(
            type(state.get("revision")) is int and state["revision"] >= 0,
            "INVALID_PROJECT",
            "项目修订号损坏",
        )
        require(
            "project_id" in state
            and (state["project_id"] is None or isinstance(state["project_id"], str)),
            "INVALID_PROJECT",
            "项目身份缺失或无效",
        )
        if "project_title" in state:
            title = state["project_title"]
            require(isinstance(title, str) and title.strip(), "INVALID_PROJECT", "项目名称无效")
        for key in ("records", "current", "confirmations", "reports", "requests"):
            require(isinstance(state.get(key), dict), "INVALID_PROJECT", f"项目缺少 {key}")
        for key in ("runs", "exports"):
            require(isinstance(state.get(key), list), "INVALID_PROJECT", f"项目缺少 {key}")
        require(
            type(state.get("runtime_schema_version")) is int
            and state["runtime_schema_version"] == 1,
            "INVALID_PROJECT",
            "不支持的运行任务格式",
        )
        require("tasks" in state, "INVALID_PROJECT", "项目缺少运行任务索引")
        require(isinstance(state["tasks"], dict), "INVALID_PROJECT", "运行任务索引损坏")
        for task_id, task in state["tasks"].items():
            require(
                isinstance(task, dict)
                and task.get("id") == task_id
                and isinstance(task.get("status"), str)
                and isinstance(task.get("outputs"), list)
                and isinstance(task.get("resumable"), bool),
                "INVALID_PROJECT",
                "运行检查点损坏；原文件保留",
            )
            if task["resumable"]:
                require(
                    fingerprint(task.get("command")) == task.get("command_hash"),
                    "INVALID_PROJECT",
                    "冻结的任务输入校验失败，原文件保留",
                )
                require(
                    isinstance(task.get("command"), dict)
                    and isinstance(task.get("expected"), dict)
                    and isinstance(task.get("items"), dict),
                    "INVALID_PROJECT",
                    "恢复输入缺失或损坏",
                )
        for record_id in state["records"]:
            self.record(record_id, state=state)
        for key, record_id in state["current"].items():
            require(
                isinstance(record_id, str) and record_id in state["records"],
                "INVALID_PROJECT",
                "当前内容引用丢失",
            )
            require(
                state["records"][record_id]["key"] == key, "INVALID_PROJECT", "当前内容绑定类型错误"
            )
        run_ids = set()
        for run in state["runs"]:
            require(
                isinstance(run, dict)
                and isinstance(run.get("id"), str)
                and isinstance(run.get("status"), str)
                and run["status"] in RUN_STATUSES
                and ID_PATTERN.fullmatch(run["id"])
                and run["id"] not in run_ids,
                "INVALID_PROJECT",
                "任务记录损坏",
            )
            if "checkpoint" in run:
                cp = run["checkpoint"]
                require(
                    fingerprint(cp) == run.get("checkpoint_hash"),
                    "INVALID_PROJECT",
                    "运行检查点损坏，原文件保留",
                )
                require(
                    isinstance(cp, dict)
                    and cp.get("step") in {"request", "validate", "review", "decide", "commit"}
                    and isinstance(cp.get("command"), dict)
                    and isinstance(cp.get("prompt"), str)
                    and isinstance(cp.get("generation_prompt"), str)
                    and type(cp.get("revision")) is int
                    and cp["revision"] >= 0
                    and type(cp.get("format_attempt")) is int
                    and cp["format_attempt"] >= 0
                    and (cp.get("response") is None or isinstance(cp["response"], str))
                    and (cp.get("candidate") is None or cp["candidate"] in state["records"])
                    and run.get("task_id") in state["tasks"],
                    "INVALID_PROJECT",
                    "运行检查点损坏，原文件保留",
                )
                require(
                    cp["step"] != "validate" or isinstance(cp.get("response"), str),
                    "INVALID_PROJECT",
                    "待校验的模型回复缺失",
                )
                require(
                    cp["step"] not in {"review", "decide", "commit"} or bool(cp.get("candidate")),
                    "INVALID_PROJECT",
                    "待审阅的候选缺失",
                )
                require(
                    cp["step"] not in {"decide", "commit"} or isinstance(cp.get("review"), dict),
                    "INVALID_PROJECT",
                    "审阅结果缺失",
                )
                if "review_repair" in cp:
                    repair = cp["review_repair"]
                    require(
                        cp["step"] == "review"
                        and isinstance(repair, dict)
                        and set(repair) == {"error", "response"}
                        and all(isinstance(value, str) for value in repair.values()),
                        "INVALID_PROJECT",
                        "审阅格式修复检查点损坏，原文件保留",
                    )
            run_ids.add(run["id"])
        return state

    def record(self, record_id: str, *, state: JsonObject | None = None) -> ArtifactRecord:
        require(
            isinstance(record_id, str) and ID_PATTERN.fullmatch(record_id) is not None,
            "INVALID_INPUT",
            "内容编号无效",
        )
        state = self.load() if state is None else state
        metadata = state["records"].get(record_id)
        require(
            isinstance(metadata, dict)
            and all(isinstance(metadata.get(key), str) for key in ("kind", "key", "hash")),
            "INVALID_PROJECT",
            "内容索引不存在或损坏",
        )
        try:
            record = json.loads(
                (self.root / "artifacts" / f"{record_id}.json").read_text(encoding="utf-8"),
                parse_constant=reject_json_constant,
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise WorkspaceError("INVALID_PROJECT", "内容文件丢失或损坏；项目未被覆盖") from exc
        required_fields = {
            "id",
            "kind",
            "key",
            "content",
            "content_hash",
            "dependency_refs",
            "origin",
            "created_at",
            "metadata",
        }
        require(
            isinstance(record, dict) and required_fields <= record.keys(),
            "INVALID_PROJECT",
            "内容文件缺少当前必需字段",
        )
        require(
            record["id"] == record_id
            and record["kind"] == metadata["kind"]
            and record["key"] == metadata["key"],
            "INVALID_PROJECT",
            "内容文件身份校验失败",
        )
        require(
            isinstance(record["content"], dict)
            and isinstance(record["metadata"], dict)
            and isinstance(record["dependency_refs"], dict),
            "INVALID_PROJECT",
            "内容或依赖格式损坏",
        )
        require(
            all(
                isinstance(reference, str) and reference in state["records"]
                for reference in record["dependency_refs"].values()
            ),
            "INVALID_PROJECT",
            "内容依赖丢失",
        )
        require(
            fingerprint(record) == metadata["hash"]
            and fingerprint(record["content"]) == record["content_hash"],
            "INVALID_PROJECT",
            "内容文件或指纹校验失败",
        )
        if record["kind"] == "source":
            origin = record["content"].get("origin")
            require(
                isinstance(origin, str) and origin in {"paste", "file"},
                "INVALID_PROJECT",
                "原文来源不是当前支持的格式",
            )
        if record["kind"] in SPEC_FIELDS:
            require(
                isinstance(record["content"].get("views"), list),
                "INVALID_PROJECT",
                "资产缺少视图列表",
            )
            require(
                all(
                    isinstance(view, dict)
                    and isinstance(view.get("spec_input_hash"), str)
                    and isinstance(view.get("content_hash"), str)
                    for view in record["content"]["views"]
                ),
                "INVALID_PROJECT",
                "资产视图缺少当前格式的提示词指纹",
            )
        return record

    def current(self, key: str, *, state: JsonObject | None = None) -> ArtifactRecord | None:
        state = self.load() if state is None else state
        ref = state["current"].get(key)
        return self.record(ref, state=state) if ref else None

    def is_current(
        self, record_id: str, *, state: JsonObject | None = None, visited: set[str] | None = None
    ) -> bool:
        state = self.load() if state is None else state
        visited = set() if visited is None else set(visited)
        require(record_id not in visited, "INVALID_PROJECT", "发现循环来源依赖")
        visited.add(record_id)
        record = self.record(record_id, state=state)
        if state["current"].get(record["key"]) != record_id:
            return False
        for key, ref in record["dependency_refs"].items():
            if state["current"].get(key) != ref or not self.is_current(
                ref, state=state, visited=visited
            ):
                return False
        return True

    @contextmanager
    def transaction(self, expected_revision: int | None = None) -> Iterator[JsonObject]:
        self.root.mkdir(parents=True, exist_ok=True)
        with project_lock(str(self.root / ".v2-project.lock"), timeout=5):
            # Writes never use the shared read: the state is changed in place below.
            state = self._read()
            if expected_revision is not None:
                require(
                    type(expected_revision) is int and expected_revision == state["revision"],
                    "REVISION_CONFLICT",
                    "项目已被其他操作更新；编辑内容保留，请刷新后比较",
                )
            before = fingerprint(state)
            yield state
            self._discard_history(state)
            if before == fingerprint(state):
                self._delete_unindexed_files(state)
                return
            state["revision"] += 1
            if not state["project_id"]:
                state["project_id"] = identity("project")
            self._forget_shared()
            atomic_write_text(self.path, encode(state))
            self._delete_unindexed_files(state)

    def _delete_unindexed_files(self, state: JsonObject) -> None:
        """Only delete known artifact filenames after the manifest is durable."""
        folder = self.root / "artifacts"
        if not folder.exists():
            return
        for path in folder.glob("*.json"):
            if ID_PATTERN.fullmatch(path.stem) and path.stem not in state["records"]:
                require(
                    path.resolve().is_relative_to(self.root.resolve()),
                    "INVALID_PROJECT",
                    "内容文件路径超出项目目录",
                )
                path.unlink(missing_ok=True)

    def _discard_history(self, state: JsonObject, *, reconcile: bool = True) -> None:
        from .runtime import invalidate, invalidation_reason, task_valid

        if reconcile:
            for task in state["tasks"].values():
                if task.get("resumable") and not task_valid(state, task):
                    invalidate(state, task, invalidation_reason(state, task))
        # Runs without text checkpoints (e.g. chat) retain their in-flight data.
        if any(r["status"] in ACTIVE_RUNS and "checkpoint" not in r for r in state["runs"]):
            return
        pinned = {
            run["checkpoint"]["candidate"]
            for run in state["runs"]
            if run.get("checkpoint", {}).get("candidate")
            and state["tasks"].get(run.get("task_id"), {}).get("resumable")
        }
        discarded = {
            record_id
            for record_id, metadata in state["records"].items()
            if (state["current"].get(metadata["key"]) != record_id and record_id not in pinned)
            or metadata["kind"] == "check_report"
        }
        while True:
            dependents = set()
            for record_id in state["records"].keys() - discarded:
                record = self.record(record_id, state=state)
                if discarded.intersection(record["dependency_refs"].values()):
                    dependents.add(record_id)
            if not dependents:
                break
            discarded.update(dependents)
        for record_id in discarded:
            state["records"].pop(record_id)
            state["confirmations"].pop(record_id, None)
            state["reports"].pop(record_id, None)
        state["current"] = {
            key: ref for key, ref in state["current"].items() if ref not in discarded
        }
        if reconcile:
            invalidated = False
            for task in state["tasks"].values():
                if task.get("resumable") and not task_valid(state, task):
                    invalidate(state, task, invalidation_reason(state, task))
                    invalidated = True
            if invalidated:
                self._discard_history(state, reconcile=False)
        state["confirmations"].clear()
        state["reports"].clear()
        for run in state["runs"]:
            run["output_refs"] = [ref for ref in run["output_refs"] if ref not in discarded]
            if run["stage"] != "chat" and "checkpoint" not in run:
                run.pop("command", None)
                run["snapshot"].pop("input_refs", None)

    def _save(
        self,
        state: JsonObject | None,
        kind: str,
        content: JsonObject,
        dependencies: dict[str, str] | None,
        *,
        origin: str = "manual",
        select: bool = True,
        key: str | None = None,
        metadata: JsonObject | None = None,
    ) -> ArtifactRecord:
        for binding, ref in dependencies.items():
            record = self.record(ref, state=state)
            require(record["key"] == binding, "INVALID_INPUT", "依赖类型与绑定名称不一致")
        key = key or content_key(kind, content)
        previous = self.current(key, state=state)
        value = validate_content(
            kind,
            content,
            resolve=lambda ref: self.record(ref, state=state),
            dependencies=dependencies,
        )
        if kind in SPEC_FIELDS:
            bind_view_prompts(kind, value, previous["content"] if previous else None)
        require(origin in {"manual", "model", "import"}, "INVALID_INPUT", "内容来源无效")
        record = {
            "id": identity("artifact"),
            "kind": kind,
            "key": key,
            "content": value,
            "content_hash": fingerprint(value),
            "dependency_refs": deepcopy(dependencies),
            "origin": origin,
            "created_at": timestamp(),
            "metadata": deepcopy(metadata or {}),
        }
        atomic_write_text(self.root / "artifacts" / f"{record['id']}.json", encode(record))
        state["records"][record["id"]] = {"kind": kind, "key": key, "hash": fingerprint(record)}
        if select:
            state["current"][key] = record["id"]
        return record

    def save(
        self,
        kind: str,
        content: JsonObject,
        dependencies: dict[str, str] | None,
        *,
        expected_revision: int,
        origin: str = "manual",
        select: bool = True,
        key: str | None = None,
        metadata: JsonObject | None = None,
    ) -> ArtifactRecord:
        with self.transaction(expected_revision) as state:
            for binding, ref in dependencies.items():
                require(
                    state["current"].get(binding) == ref and self.is_current(ref, state=state),
                    "STALE_INPUT",
                    "输入内容已被替换，请使用当前内容重新生成",
                )
            record = self._save(
                state,
                kind,
                content,
                dependencies,
                origin=origin,
                select=select,
                key=key,
                metadata=metadata,
            )
        return deepcopy(record)

    @contextmanager
    def generation_lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            with project_lock(str(self.root / ".v2-generation.lock"), timeout=0):
                yield
        except Timeout as exc:
            raise WorkspaceError("PROJECT_BUSY", "项目已有 LLM 任务，请等待完成或停止") from exc

    def start_run(
        self,
        command: JsonObject,
        *,
        expected_revision: int,
        snapshot: JsonObject,
        task_id: str | None = None,
        checkpoint: JsonObject | None = None,
    ) -> tuple[JsonObject, bool]:
        """Called under generation_lock; admission and deduplication are durable."""
        request_id = command.get("request_id")
        require(
            isinstance(request_id, str) and bool(request_id.strip()),
            "INVALID_INPUT",
            "请求编号不能为空",
        )
        command_hash = fingerprint(command)
        with self.transaction() as state:
            previous = state["requests"].get(request_id)
            if previous:
                require(
                    previous["command_hash"] == command_hash,
                    "REQUEST_KEY_CONFLICT",
                    "相同请求编号不能用于不同内容",
                )
                return next(
                    deepcopy(r) for r in state["runs"] if r["id"] == previous["run_id"]
                ), False
            require(
                expected_revision == state["revision"],
                "REVISION_CONFLICT",
                "任务输入已变化，请刷新后重试",
            )
            require(
                not any(r["status"] in ACTIVE_RUNS for r in state["runs"]),
                "PROJECT_BUSY",
                "项目已有活动任务",
            )
            run = {
                "id": identity("run"),
                "status": "running",
                "stage": command["stage"],
                "snapshot": deepcopy(snapshot),
                "command": deepcopy(command),
                "calls": [],
                "output_refs": [],
                "created_at": timestamp(),
                "error": None,
            }
            if checkpoint is not None:
                run.update(
                    task_id=task_id,
                    checkpoint=deepcopy(checkpoint),
                    checkpoint_hash=fingerprint(checkpoint),
                )
            state["runs"].append(run)
            state["requests"][request_id] = {"command_hash": command_hash, "run_id": run["id"]}
        return deepcopy(run), True

    def update_run(
        self, run_id: str, operation: Callable[[JsonObject, JsonObject], OperationResult]
    ) -> OperationResult:
        with self.transaction() as state:
            run = next((r for r in state["runs"] if r["id"] == run_id), None)
            require(run is not None, "INVALID_INPUT", "任务不存在")
            result = operation(run, state)
        return deepcopy(result)

    def record_run_failure(
        self, run_id: str, error: Exception, *, cancelled: bool, feedback: JsonObject | None = None
    ) -> None:
        """Persist task state without relabelling the original failure."""
        try:
            self.update_run(
                run_id,
                lambda run, _: run.update(
                    status="cancelled" if cancelled else "failed",
                    error=type(error).__name__,
                    **({"feedback": feedback} if feedback else {}),
                    ended_at=timestamp(),
                ),
            )
        except (OSError, WorkspaceError) as storage_error:
            error.add_note(f"运行 {run_id} 的失败状态未保存：{type(storage_error).__name__}")
            logger.error(
                "Failed to save run failure: run_id=%s error_type=%s",
                run_id,
                type(storage_error).__name__,
                exc_info=True,
            )

    def recover(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.generation_lock():
                with self.transaction() as state:
                    for task in state["tasks"].values():
                        if task["status"] == "running":
                            task.update(status="interrupted", ended_at=timestamp())
                    for run in state["runs"]:
                        if run["status"] in ACTIVE_RUNS:
                            run.update(status="interrupted", ended_at=timestamp())
                        for call in run.get("calls", []):
                            if call.get("state") == "sending":
                                call["state"] = "unknown"
        except WorkspaceError as exc:
            if exc.code != "PROJECT_BUSY":
                raise
