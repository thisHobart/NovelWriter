"""Current-only creative content in an atomic manifest.

Opening a repository is read-only.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from filelock import Timeout

from core.generation.locks import project_lock

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


class ProjectRepository:
    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = str(Path(output_dir).resolve())
        self.root = Path(self.output_dir) / "short_video"
        self.path = self.root / "project_v2.json"

    @staticmethod
    def empty() -> JsonObject:
        return {
            "schema_version": 2,
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
        for key in ("records", "current", "confirmations", "reports", "requests"):
            require(isinstance(state.get(key), dict), "INVALID_PROJECT", f"项目缺少 {key}")
        for key in ("runs", "exports"):
            require(isinstance(state.get(key), list), "INVALID_PROJECT", f"项目缺少 {key}")
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
            state = self.load()
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

    def _discard_history(self, state: JsonObject) -> None:
        # In-flight candidates and their original editing target live until the run ends.
        if any(run["status"] in ACTIVE_RUNS for run in state["runs"]):
            return
        discarded = {
            record_id
            for record_id, metadata in state["records"].items()
            if state["current"].get(metadata["key"]) != record_id
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
        state["confirmations"].clear()
        state["reports"].clear()
        for run in state["runs"]:
            run["output_refs"] = [ref for ref in run["output_refs"] if ref not in discarded]
            if run["stage"] != "chat":
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
        self, command: JsonObject, *, expected_revision: int, snapshot: JsonObject
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
                    for run in state["runs"]:
                        if run["status"] in ACTIVE_RUNS:
                            run.update(status="interrupted", ended_at=timestamp())
        except WorkspaceError as exc:
            if exc.code != "PROJECT_BUSY":
                raise
