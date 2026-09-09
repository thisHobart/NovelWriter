"""Versioned narrative design graph and deterministic story-state queries.

The graph is the relatively stable design.  Runtime facts about what the reader
has seen and what a chapter actually executed live in ``suspense_ledger.json``.
All structural writes go through :class:`NarrativeGraphManager` so revision
checks, validation, impact analysis, and the change log cannot be bypassed.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from glob import glob
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from core.generation.locks import project_lock

from core.generation.story_ledger import (
    RevisionConflictError,
    StoryLedgerManager,
    _atomic_write_json,
)


GRAPH_VERSION = 1
ALLOWED_NODE_TYPES = frozenset({"thread", "clue", "fact", "reveal"})
ALLOWED_EDGE_TYPES = frozenset({"belongs_to", "supports", "requires", "advances"})
ALLOWED_OPERATIONS = frozenset(
    {
        "add_node",
        "update_node",
        "deprecate_node",
        "add_edge",
        "update_edge",
        "deprecate_edge",
    }
)
ALLOWED_THREAD_ACTIONS = frozenset(
    {"open", "touch", "advance", "complicate", "cross", "close"}
)
ALLOWED_TRANSITIONS = frozenset(
    {"introduce_to_reader", "make_inferable", "reveal", "execute", "deprecate"}
)

# These fields describe runtime state, not design.  Old graphs containing them
# are migrated into ``narrative_node_states`` without bumping graph revision.
DYNAMIC_NODE_FIELDS = frozenset(
    {
        "status",
        "reader_status",
        "reveal_status",
        "thread_status",
        "introduced_chapter",
        "inferable_chapter",
        "revealed_chapter",
        "executed_chapter",
        "closed_chapter",
        "actual_chapter",
        "actual_chapters",
        "last_touch_chapter",
        "last_advance_chapter",
    }
)


class NarrativeGraphError(ValueError):
    """A graph or graph-referenced artifact failed deterministic validation."""

    def __init__(self, issues: Iterable[Dict[str, Any]], message: str = ""):
        self.issues = [deepcopy(issue) for issue in issues]
        summary = message or "; ".join(
            str(issue.get("message", issue.get("code", "invalid narrative graph")))
            for issue in self.issues
            if issue.get("severity") == "error"
        )
        super().__init__(summary or "Narrative graph validation failed")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _issue(
    code: str,
    message: str,
    *,
    severity: str = "error",
    node_id: str = "",
    edge_id: str = "",
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "code": code,
        "severity": severity,
        "message": message,
        "details": deepcopy(details or {}),
    }
    if node_id:
        result["node_id"] = node_id
    if edge_id:
        result["edge_id"] = edge_id
    return result


def _has_errors(issues: Iterable[Dict[str, Any]]) -> bool:
    return any(issue.get("severity") == "error" for issue in issues)


def _edge_endpoint(edge: Dict[str, Any], side: str) -> str:
    aliases = (
        ("source_id", "source", "from_node_id", "from")
        if side == "source"
        else ("target_id", "target", "to_node_id", "to")
    )
    for key in aliases:
        value = str(edge.get(key, "")).strip()
        if value:
            return value
    return ""


def _normalise_edge(edge: Dict[str, Any]) -> Dict[str, Any]:
    normalised = deepcopy(edge)
    normalised["source_id"] = _edge_endpoint(normalised, "source")
    normalised["target_id"] = _edge_endpoint(normalised, "target")
    for alias in ("source", "from_node_id", "from", "target", "to_node_id", "to"):
        normalised.pop(alias, None)
    return normalised


def _node_id_set(values: Any) -> Set[str]:
    if not isinstance(values, list):
        return set()
    return {str(value).strip() for value in values if str(value).strip()}


def contract_node_references(contract: Dict[str, Any]) -> Set[str]:
    """Return every narrative graph id referenced by a chapter contract."""
    refs: Set[str] = set()
    for field in (
        "allowed_reveals",
        "forbidden_reveals",
        "forbidden_nodes",
        "intentionally_silent_threads",
    ):
        refs.update(_node_id_set(contract.get(field)))
    for field in ("primary_thread", "secondary_thread"):
        value = str(contract.get(field, "")).strip()
        if value:
            refs.add(value)
    for update in contract.get("plot_thread_updates", []):
        if not isinstance(update, dict):
            continue
        thread_id = str(update.get("id", "")).strip()
        # v2 contracts used this stream as a standalone suspense ledger and its
        # ids predate NarrativeGraph.  Only the extended action/via form opts
        # the record into graph referential integrity.
        if thread_id and (
            update.get("narrative_graph_managed") is True
            or (
                update.get("narrative_graph_managed") is not False
                and ("action" in update or update.get("via_node_ids"))
            )
        ):
            refs.add(thread_id)
        refs.update(_node_id_set(update.get("via_node_ids")))
    for transition in contract.get("narrative_transitions", []):
        if isinstance(transition, dict) and str(transition.get("node_id", "")).strip():
            refs.add(str(transition["node_id"]).strip())
    return refs


def delta_node_references(delta: Dict[str, Any]) -> Set[str]:
    refs = contract_node_references(delta)
    for transition in delta.get("narrative_transitions", []):
        if isinstance(transition, dict) and str(transition.get("node_id", "")).strip():
            refs.add(str(transition["node_id"]).strip())
    return refs


def _atomic_append_jsonl(path: str, record: Dict[str, Any]) -> None:
    """Append one JSONL record via atomic replacement, preserving valid history."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    previous = b""
    if os.path.exists(path):
        with open(path, "rb") as handle:
            previous = handle.read()
    line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=directory,
            prefix=f".{os.path.basename(path)}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(previous)
            if previous and not previous.endswith(b"\n"):
                handle.write(b"\n")
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.remove(temporary)


def _thread_node(
    record: Dict[str, Any], section_last_chapter: Dict[int, int]
) -> Optional[Dict[str, Any]]:
    node_id = str(record.get("id", "")).strip()
    label = str(record.get("thread", "")).strip()
    if not node_id or not label:
        return None
    node: Dict[str, Any] = {"id": node_id, "type": "thread", "label": label}
    try:
        section = int(record.get("must_close_by_section"))
    except (TypeError, ValueError):
        section = 0
    planned = section_last_chapter.get(section)
    if planned:
        node["planned_resolve_chapter"] = int(planned)
    return node


def _fact_node(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """结构契约里的一条「真相」对应图上的一个 fact 节点。

    不是 reveal：reveal 节点要求有 clue 或 fact 前置（公平推理——揭晓必须建立在
    读者见过的证据上），而结构阶段声明的是故事世界里成立的事实本身，什么时候揭给
    读者是账本里的运行期状态，不是图的结构。
    """
    node_id = str(record.get("id", "")).strip()
    label = str(record.get("fact", "")).strip()
    if not node_id or not label:
        return None
    return {"id": node_id, "type": "fact", "label": label}


class NarrativeGraphManager:
    """Own and query the current story's versioned narrative design graph."""

    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self.ledger_dir = os.path.join(output_dir, "system", "story_ledgers")
        self.graph_path = os.path.join(self.ledger_dir, "narrative_graph.json")
        self.change_log_path = os.path.join(
            self.ledger_dir, "narrative_graph_changes.jsonl"
        )
        self.lock_path = os.path.join(self.ledger_dir, ".narrative_graph.lock")
        self._ensure_initialised()
        self._migrate_legacy_dynamic_fields()

    @staticmethod
    def empty_graph() -> Dict[str, Any]:
        return {
            "version": GRAPH_VERSION,
            "revision": 0,
            "nodes": [],
            "edges": [],
            "updated_at": "",
        }

    def _ensure_initialised(self) -> None:
        os.makedirs(self.ledger_dir, exist_ok=True)
        if os.path.exists(self.graph_path):
            return
        with project_lock(self.lock_path):
            if not os.path.exists(self.graph_path):
                _atomic_write_json(self.graph_path, self.empty_graph())

    def _read_graph(self) -> Dict[str, Any]:
        try:
            with open(self.graph_path, "r", encoding="utf-8") as handle:
                graph = json.load(handle)
        except (OSError, ValueError, TypeError) as exc:
            raise NarrativeGraphError(
                [_issue("NARRATIVE_GRAPH_UNREADABLE", f"无法读取叙事图：{exc}")]
            ) from exc
        if not isinstance(graph, dict):
            raise NarrativeGraphError(
                [_issue("NARRATIVE_GRAPH_INVALID", "叙事图根节点必须是 JSON 对象")]
            )
        graph.setdefault("version", GRAPH_VERSION)
        graph.setdefault("revision", 0)
        graph.setdefault("nodes", [])
        graph.setdefault("edges", [])
        graph.setdefault("updated_at", "")
        return graph

    def _migrate_legacy_dynamic_fields(self) -> None:
        """Move old node runtime fields to the StoryLedger without losing them."""
        with project_lock(self.lock_path):
            graph = self._read_graph()
            migrated_states: Dict[str, Dict[str, Any]] = {}
            changed = False
            for node in graph.get("nodes", []):
                if not isinstance(node, dict):
                    continue
                node_id = str(node.get("id", "")).strip()
                dynamic = {
                    key: deepcopy(node[key])
                    for key in list(node)
                    if key in DYNAMIC_NODE_FIELDS
                }
                if not dynamic or not node_id:
                    continue
                for key in dynamic:
                    node.pop(key, None)
                legacy_status = dynamic.pop("status", None)
                if legacy_status is not None:
                    if node.get("type") == "thread":
                        dynamic.setdefault("thread_status", legacy_status)
                    elif node.get("type") == "reveal":
                        dynamic.setdefault("reveal_status", legacy_status)
                    else:
                        dynamic.setdefault("reader_status", legacy_status)
                migrated_states[node_id] = dynamic
                changed = True
            if not changed:
                return

            ledger_manager = StoryLedgerManager(self.output_dir)
            ledger_manager.initialize()
            with project_lock(ledger_manager.suspense_ledger_lock_path):
                ledger = ledger_manager.load_suspense_ledger()
                states = ledger.setdefault("narrative_node_states", {})
                for node_id, dynamic in migrated_states.items():
                    state = states.setdefault(node_id, {})
                    for key, value in dynamic.items():
                        state.setdefault(key, value)
                ledger["narrative_graph_migrated_at"] = _now()
                ledger["updated_at"] = _now()
                _atomic_write_json(ledger_manager.suspense_ledger_path, ledger)
            graph["version"] = GRAPH_VERSION
            _atomic_write_json(self.graph_path, graph)

    def load(self) -> Dict[str, Any]:
        """Load a detached copy.  Existing stories are initialised on construction."""
        return deepcopy(self._read_graph())

    def current_revision(self) -> int:
        return int(self._read_graph().get("revision", 0) or 0)

    def _load_ledger(self) -> Dict[str, Any]:
        manager = StoryLedgerManager(self.output_dir)
        if not os.path.exists(manager.suspense_ledger_path):
            return {
                "revision": 0,
                "accepted_chapters": [],
                "narrative_node_states": {},
            }
        return manager.load_suspense_ledger()

    @staticmethod
    def _node_map(graph: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        return {
            str(node.get("id")): node
            for node in graph.get("nodes", [])
            if isinstance(node, dict) and node.get("id")
        }

    @staticmethod
    def _edge_map(graph: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        return {
            str(edge.get("id")): edge
            for edge in graph.get("edges", [])
            if isinstance(edge, dict) and edge.get("id")
        }

    @staticmethod
    def _active_edges(graph: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            edge
            for edge in graph.get("edges", [])
            if isinstance(edge, dict) and not edge.get("deprecated")
        ]

    def add_node(self, node: Dict[str, Any], expected_revision: int) -> int:
        return self.apply_change(
            [{"op": "add_node", "node": deepcopy(node)}],
            reason=f"add narrative node {node.get('id', '')}",
            expected_revision=expected_revision,
        )

    def update_node(
        self, node_id: str, changes: Dict[str, Any], expected_revision: int
    ) -> int:
        return self.apply_change(
            [{"op": "update_node", "node_id": node_id, "changes": deepcopy(changes)}],
            reason=f"update narrative node {node_id}",
            expected_revision=expected_revision,
        )

    def add_edge(self, edge: Dict[str, Any], expected_revision: int) -> int:
        return self.apply_change(
            [{"op": "add_edge", "edge": deepcopy(edge)}],
            reason=f"add narrative edge {edge.get('id', '')}",
            expected_revision=expected_revision,
        )

    def update_edge(
        self, edge_id: str, changes: Dict[str, Any], expected_revision: int
    ) -> int:
        return self.apply_change(
            [{"op": "update_edge", "edge_id": edge_id, "changes": deepcopy(changes)}],
            reason=f"update narrative edge {edge_id}",
            expected_revision=expected_revision,
        )

    def propose_change(self, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        ledger = self._load_ledger()
        issues = self.validate_change(operations, ledger)
        impact = self.analyze_impact(operations, ledger)
        return {
            "operations": deepcopy(operations),
            "valid": not _has_errors(issues) and not impact["historical_conflicts"],
            "validation_issues": issues,
            "impact": impact,
            "base_revision": self.current_revision(),
        }

    @staticmethod
    def _operation_name(operation: Dict[str, Any]) -> str:
        return str(operation.get("op") or operation.get("operation") or "").strip()

    def _apply_operations(
        self, graph: Dict[str, Any], operations: Iterable[Dict[str, Any]]
    ) -> Dict[str, Any]:
        working = deepcopy(graph)
        nodes = self._node_map(working)
        edges = self._edge_map(working)
        for operation in operations:
            op = self._operation_name(operation)
            if op == "add_node":
                node = deepcopy(operation.get("node") or operation.get("value") or {})
                working.setdefault("nodes", []).append(node)
                if node.get("id"):
                    nodes[str(node["id"])] = node
            elif op in {"update_node", "deprecate_node"}:
                node_id = str(operation.get("node_id") or operation.get("id") or "")
                node = nodes.get(node_id)
                if node is not None:
                    if op == "deprecate_node":
                        node.update(
                            {
                                "deprecated": True,
                                "deprecated_at": _now(),
                                "deprecation_reason": str(
                                    operation.get("reason", "structural change")
                                ),
                            }
                        )
                    else:
                        node.update(deepcopy(operation.get("changes") or {}))
            elif op == "add_edge":
                edge = _normalise_edge(
                    deepcopy(operation.get("edge") or operation.get("value") or {})
                )
                working.setdefault("edges", []).append(edge)
                if edge.get("id"):
                    edges[str(edge["id"])] = edge
            elif op in {"update_edge", "deprecate_edge"}:
                edge_id = str(operation.get("edge_id") or operation.get("id") or "")
                edge = edges.get(edge_id)
                if edge is not None:
                    if op == "deprecate_edge":
                        edge.update(
                            {
                                "deprecated": True,
                                "deprecated_at": _now(),
                                "deprecation_reason": str(
                                    operation.get("reason", "structural change")
                                ),
                            }
                        )
                    else:
                        changes = deepcopy(operation.get("changes") or {})
                        edge.update(changes)
                        normalised = _normalise_edge(edge)
                        edge.clear()
                        edge.update(normalised)
        return working

    def validate_change(
        self,
        operations: List[Dict[str, Any]],
        ledger: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        issues: List[Dict[str, Any]] = []
        if not isinstance(operations, list) or not operations:
            return [_issue("GRAPH_CHANGE_EMPTY", "叙事图变更必须包含至少一个操作")]
        graph = self.load()
        node_ids = set(self._node_map(graph))
        edge_ids = set(self._edge_map(graph))
        for index, operation in enumerate(operations):
            if not isinstance(operation, dict):
                issues.append(
                    _issue(
                        "GRAPH_OPERATION_INVALID",
                        "叙事图操作必须是对象",
                        details={"operation_index": index},
                    )
                )
                continue
            op = self._operation_name(operation)
            if op not in ALLOWED_OPERATIONS:
                issues.append(
                    _issue(
                        "GRAPH_OPERATION_UNSUPPORTED",
                        f"不支持的叙事图操作：{op or '<empty>'}",
                        details={"operation_index": index},
                    )
                )
                continue
            if op == "add_node":
                node = operation.get("node") or operation.get("value") or {}
                node_id = str(node.get("id", "")).strip() if isinstance(node, dict) else ""
                if not node_id:
                    issues.append(_issue("NODE_ID_REQUIRED", "新增节点必须提供 id"))
                elif node_id in node_ids:
                    issues.append(
                        _issue("DUPLICATE_NODE_ID", f"节点 id 重复：{node_id}", node_id=node_id)
                    )
                else:
                    node_ids.add(node_id)
                dynamic = sorted(set(node or {}) & DYNAMIC_NODE_FIELDS) if isinstance(node, dict) else []
                if dynamic:
                    issues.append(
                        _issue(
                            "DYNAMIC_STATE_IN_GRAPH",
                            "运行状态必须写入 StoryLedger，不能写入 NarrativeGraph",
                            node_id=node_id,
                            details={"fields": dynamic},
                        )
                    )
            elif op in {"update_node", "deprecate_node"}:
                node_id = str(operation.get("node_id") or operation.get("id") or "")
                if node_id not in node_ids:
                    issues.append(
                        _issue("NODE_NOT_FOUND", f"节点不存在：{node_id}", node_id=node_id)
                    )
                dynamic = sorted(set(operation.get("changes") or {}) & DYNAMIC_NODE_FIELDS)
                if "id" in (operation.get("changes") or {}):
                    issues.append(
                        _issue(
                            "NODE_ID_IMMUTABLE",
                            "节点 id 创建后不可修改；请新增节点并保留历史节点",
                            node_id=node_id,
                        )
                    )
                if dynamic:
                    issues.append(
                        _issue(
                            "DYNAMIC_STATE_IN_GRAPH",
                            "运行状态必须写入 StoryLedger，不能写入 NarrativeGraph",
                            node_id=node_id,
                            details={"fields": dynamic},
                        )
                    )
            elif op == "add_edge":
                edge = operation.get("edge") or operation.get("value") or {}
                edge_id = str(edge.get("id", "")).strip() if isinstance(edge, dict) else ""
                if not edge_id:
                    issues.append(_issue("EDGE_ID_REQUIRED", "新增边必须提供 id"))
                elif edge_id in edge_ids:
                    issues.append(
                        _issue("DUPLICATE_EDGE_ID", f"边 id 重复：{edge_id}", edge_id=edge_id)
                    )
                else:
                    edge_ids.add(edge_id)
            else:
                edge_id = str(operation.get("edge_id") or operation.get("id") or "")
                if edge_id not in edge_ids:
                    issues.append(
                        _issue("EDGE_NOT_FOUND", f"边不存在：{edge_id}", edge_id=edge_id)
                    )
                if "id" in (operation.get("changes") or {}):
                    issues.append(
                        _issue(
                            "EDGE_ID_IMMUTABLE",
                            "边 id 创建后不可修改；请新增边并保留历史边",
                            edge_id=edge_id,
                        )
                    )

        candidate = self._apply_operations(graph, operations)
        issues.extend(self._validate_graph(candidate, ledger))
        return issues

    def validate(self, ledger: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        return self._validate_graph(self.load(), ledger)

    def validate_terminal_state(
        self,
        final_chapter: int,
        ledger: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Validate that design milestones due by the ending were actually completed.

        Ordinary graph validation deliberately permits a planner to carry active
        threads and ready reveals into later chapters.  A finite-story audit needs
        a stricter boundary: once the declared final chapter reaches a node's
        planned chapter, leaving that milestone open is an error rather than a
        starvation warning.
        """
        graph = self.load()
        ledger = ledger if ledger is not None else self._load_ledger()
        states = ledger.get("narrative_node_states", {})
        if not isinstance(states, dict):
            states = {}
        issues: List[Dict[str, Any]] = []
        for node in graph.get("nodes", []):
            if not isinstance(node, dict) or node.get("deprecated"):
                continue
            node_id = str(node.get("id", "")).strip()
            node_type = str(node.get("type", "")).strip()
            state = states.get(node_id, {})
            if not isinstance(state, dict):
                state = {}
            if node_type == "reveal":
                try:
                    due = int(node.get("planned_reveal_chapter", 0) or 0)
                except (TypeError, ValueError):
                    due = 0
                if due and due <= final_chapter and state.get("reveal_status") != "executed":
                    issues.append(
                        _issue(
                            "PLANNED_REVEAL_UNRESOLVED_AT_END",
                            "计划在结局前完成的揭示仍未执行",
                            node_id=node_id,
                            details={
                                "planned_reveal_chapter": due,
                                "final_chapter": final_chapter,
                                "actual_status": state.get("reveal_status", "not_started"),
                            },
                        )
                    )
            elif node_type == "thread":
                try:
                    due = int(node.get("planned_resolve_chapter", 0) or 0)
                except (TypeError, ValueError):
                    due = 0
                if due and due <= final_chapter and state.get("thread_status") != "closed":
                    issues.append(
                        _issue(
                            "PLANNED_THREAD_UNCLOSED_AT_END",
                            "计划在结局前关闭的情节线仍处于活动状态",
                            node_id=node_id,
                            details={
                                "planned_resolve_chapter": due,
                                "final_chapter": final_chapter,
                                "actual_status": state.get("thread_status", "not_started"),
                            },
                        )
                    )
        return issues

    def _validate_graph(
        self, graph: Dict[str, Any], ledger: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        issues: List[Dict[str, Any]] = []
        nodes = graph.get("nodes", [])
        edges = graph.get("edges", [])
        if not isinstance(nodes, list) or not isinstance(edges, list):
            return [_issue("GRAPH_COLLECTION_INVALID", "nodes 和 edges 必须是数组")]

        node_ids: Set[str] = set()
        node_map: Dict[str, Dict[str, Any]] = {}
        for node in nodes:
            if not isinstance(node, dict):
                issues.append(_issue("NODE_INVALID", "图节点必须是对象"))
                continue
            node_id = str(node.get("id", "")).strip()
            node_type = str(node.get("type", "")).strip()
            if not node_id:
                issues.append(_issue("NODE_ID_REQUIRED", "图节点缺少 id"))
                continue
            if node_id in node_ids:
                issues.append(
                    _issue("DUPLICATE_NODE_ID", f"节点 id 重复：{node_id}", node_id=node_id)
                )
            node_ids.add(node_id)
            node_map[node_id] = node
            if node_type not in ALLOWED_NODE_TYPES:
                issues.append(
                    _issue(
                        "NODE_TYPE_INVALID",
                        f"节点 {node_id} 的类型 {node_type!r} 不受支持",
                        node_id=node_id,
                        details={"allowed_types": sorted(ALLOWED_NODE_TYPES)},
                    )
                )
            dynamic = sorted(set(node) & DYNAMIC_NODE_FIELDS)
            if dynamic:
                issues.append(
                    _issue(
                        "DYNAMIC_STATE_IN_GRAPH",
                        "运行状态不能作为 NarrativeGraph 权威字段",
                        node_id=node_id,
                        details={"fields": dynamic},
                    )
                )

        edge_ids: Set[str] = set()
        active_edges: List[Dict[str, Any]] = []
        for raw_edge in edges:
            if not isinstance(raw_edge, dict):
                issues.append(_issue("EDGE_INVALID", "图边必须是对象"))
                continue
            edge = _normalise_edge(raw_edge)
            edge_id = str(edge.get("id", "")).strip()
            edge_type = str(edge.get("type", "")).strip()
            source_id = edge.get("source_id", "")
            target_id = edge.get("target_id", "")
            if not edge_id:
                issues.append(_issue("EDGE_ID_REQUIRED", "图边缺少 id"))
                continue
            if edge_id in edge_ids:
                issues.append(
                    _issue("DUPLICATE_EDGE_ID", f"边 id 重复：{edge_id}", edge_id=edge_id)
                )
            edge_ids.add(edge_id)
            if edge_type not in ALLOWED_EDGE_TYPES:
                issues.append(
                    _issue(
                        "EDGE_TYPE_INVALID",
                        f"边 {edge_id} 的类型 {edge_type!r} 不受支持",
                        edge_id=edge_id,
                        details={"allowed_types": sorted(ALLOWED_EDGE_TYPES)},
                    )
                )
            missing = [node_id for node_id in (source_id, target_id) if node_id not in node_ids]
            if missing:
                issues.append(
                    _issue(
                        "EDGE_NODE_NOT_FOUND",
                        f"边 {edge_id} 引用了不存在的节点",
                        edge_id=edge_id,
                        details={"missing_node_ids": missing},
                    )
                )
                continue
            if not edge.get("deprecated"):
                active_edges.append(edge)
            if source_id in node_map and target_id in node_map:
                source_type = node_map[source_id].get("type")
                target_type = node_map[target_id].get("type")
                valid_shape = {
                    "belongs_to": source_type == "clue" and target_type == "thread",
                    "supports": target_type == "fact",
                    "requires": source_id != target_id,
                    "advances": target_type == "thread",
                }.get(edge_type, True)
                if not valid_shape:
                    issues.append(
                        _issue(
                            "EDGE_SHAPE_INVALID",
                            f"边 {edge_id} 的端点类型与 {edge_type} 语义不匹配",
                            edge_id=edge_id,
                            details={
                                "source_id": source_id,
                                "source_type": source_type,
                                "target_id": target_id,
                                "target_type": target_type,
                            },
                        )
                    )

        # requires is a dependency DAG.  source -> target means target requires source.
        adjacency: Dict[str, List[str]] = {node_id: [] for node_id in node_ids}
        for edge in active_edges:
            if edge.get("type") == "requires":
                adjacency[edge["source_id"]].append(edge["target_id"])
        colours: Dict[str, int] = {node_id: 0 for node_id in node_ids}

        def visit(node_id: str, path: List[str]) -> None:
            colours[node_id] = 1
            for target_id in adjacency.get(node_id, []):
                if colours[target_id] == 0:
                    visit(target_id, path + [target_id])
                elif colours[target_id] == 1:
                    cycle_start = path.index(target_id) if target_id in path else 0
                    issues.append(
                        _issue(
                            "REQUIRES_CYCLE",
                            "requires 关系形成了循环依赖",
                            node_id=target_id,
                            details={"cycle": path[cycle_start:] + [target_id]},
                        )
                    )
            colours[node_id] = 2

        for candidate_id in sorted(node_ids):
            if colours[candidate_id] == 0:
                visit(candidate_id, [candidate_id])

        for node_id, node in node_map.items():
            if node.get("deprecated"):
                continue
            if node.get("type") == "clue" and not any(
                edge.get("type") == "belongs_to" and edge.get("source_id") == node_id
                for edge in active_edges
            ):
                issues.append(
                    _issue(
                        "CLUE_WITHOUT_THREAD",
                        "线索至少必须通过 belongs_to 属于一条情节线",
                        node_id=node_id,
                    )
                )
            if node.get("type") == "reveal" and not any(
                edge.get("type") == "requires"
                and edge.get("target_id") == node_id
                and node_map.get(edge.get("source_id"), {}).get("type") in {"clue", "fact"}
                for edge in active_edges
            ):
                issues.append(
                    _issue(
                        "REVEAL_WITHOUT_PREREQUISITE",
                        "揭示至少需要一个 clue 或 fact 前置节点",
                        node_id=node_id,
                    )
                )

        if ledger is not None:
            states = ledger.get("narrative_node_states", {})
            if not isinstance(states, dict):
                states = {}
            for node_id, node in node_map.items():
                state = states.get(node_id, {}) if isinstance(states.get(node_id), dict) else {}
                if node.get("type") == "reveal" and state.get("reveal_status") == "executed":
                    missing = self._missing_dependencies(node_id, graph, ledger)
                    if missing:
                        issues.append(
                            _issue(
                                "REVEAL_DEPENDENCY_UNSATISFIED",
                                "揭示的前置条件尚未满足",
                                node_id=node_id,
                                details={"missing_node_ids": missing},
                            )
                        )
                if node.get("type") == "thread" and state.get("thread_status") == "closed":
                    missing_reveals = self._thread_required_reveals(node_id, graph, ledger)
                    if missing_reveals:
                        issues.append(
                            _issue(
                                "THREAD_CLOSED_BEFORE_REQUIRED_REVEALS",
                                "情节线关闭前必须完成其要求的揭示",
                                node_id=node_id,
                                details={"missing_reveal_ids": missing_reveals},
                            )
                        )
            issues.extend(self._starvation_warnings(graph, ledger))
        return issues

    def _referenced_in_history(
        self, node_ids: Set[str], ledger: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        ledger_manager = StoryLedgerManager(self.output_dir)
        if ledger is None:
            try:
                ledger = ledger_manager.load_suspense_ledger()
            except (OSError, ValueError, TypeError):
                ledger = {}
        ledger = deepcopy(ledger)
        accepted = {
            int(item.get("chapter", 0))
            for item in ledger.get("accepted_chapters", [])
            if isinstance(item, dict)
        }
        affected_contracts: List[int] = []
        affected_chapters: Set[int] = set()
        for path in glob(os.path.join(ledger_manager.contract_dir, "chapter_*.json")):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    contract = json.load(handle)
                chapter = int(contract.get("chapter", 0) or 0)
            except (OSError, ValueError, TypeError):
                continue
            if contract_node_references(contract) & node_ids:
                affected_contracts.append(chapter)
                if chapter in accepted:
                    affected_chapters.add(chapter)
        for path in glob(os.path.join(ledger_manager.delta_dir, "chapter_*.json")):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    delta = json.load(handle)
                chapter = int(delta.get("chapter", 0) or 0)
            except (OSError, ValueError, TypeError):
                continue
            if delta_node_references(delta) & node_ids:
                affected_chapters.add(chapter)
        states = ledger.get("narrative_node_states", {})
        state_refs = node_ids & set(states) if isinstance(states, dict) else set()
        for node_id in state_refs:
            state = states.get(node_id, {})
            if isinstance(state, dict):
                for key in (
                    "introduced_chapter",
                    "inferable_chapter",
                    "revealed_chapter",
                    "executed_chapter",
                    "closed_chapter",
                ):
                    try:
                        if state.get(key):
                            affected_chapters.add(int(state[key]))
                    except (TypeError, ValueError):
                        pass
        return {
            "affected_contracts": sorted(set(affected_contracts)),
            "affected_chapters": sorted(chapter for chapter in affected_chapters if chapter > 0),
            "accepted_chapters": accepted,
            "ledger_node_ids": sorted(state_refs),
        }

    def _affected_nodes_for_operations(
        self, graph: Dict[str, Any], operations: Iterable[Dict[str, Any]]
    ) -> Set[str]:
        affected: Set[str] = set()
        edge_map = self._edge_map(graph)
        for operation in operations:
            op = self._operation_name(operation)
            if "node" in op:
                node = operation.get("node") or operation.get("value") or {}
                node_id = str(operation.get("node_id") or operation.get("id") or node.get("id", ""))
                if node_id:
                    affected.add(node_id)
            else:
                edge = operation.get("edge") or operation.get("value") or {}
                edge_id = str(operation.get("edge_id") or operation.get("id") or edge.get("id", ""))
                current = edge_map.get(edge_id, {})
                combined = deepcopy(current)
                combined.update(deepcopy(edge))
                combined.update(deepcopy(operation.get("changes") or {}))
                affected.update(
                    node_id
                    for node_id in (
                        _edge_endpoint(combined, "source"),
                        _edge_endpoint(combined, "target"),
                    )
                    if node_id
                )
        # Any dependency neighbour may change readiness even when it was not
        # directly named by the operation.
        changed = True
        active_edges = self._active_edges(graph)
        while changed:
            changed = False
            for edge in active_edges:
                source_id = _edge_endpoint(edge, "source")
                target_id = _edge_endpoint(edge, "target")
                if source_id in affected or target_id in affected:
                    before = len(affected)
                    affected.update((source_id, target_id))
                    changed = changed or len(affected) != before
        return affected

    def analyze_impact(
        self, operations: List[Dict[str, Any]], ledger: Dict[str, Any]
    ) -> Dict[str, Any]:
        graph = self.load()
        affected = self._affected_nodes_for_operations(graph, operations)
        references = self._referenced_in_history(affected, ledger)
        accepted = references.pop("accepted_chapters")
        future_contracts = [
            chapter for chapter in references["affected_contracts"] if chapter not in accepted
        ]
        historical = sorted(
            set(references["affected_chapters"])
            | {chapter for chapter in references["affected_contracts"] if chapter in accepted}
        )
        conflicts = [
            {
                "code": "HISTORICAL_NARRATIVE_REFERENCE",
                "severity": "error",
                "chapter": chapter,
                "message": "图变更影响已经验收的章节，不能自动重写历史正文",
                "details": {"affected_node_ids": sorted(affected)},
            }
            for chapter in historical
        ]
        return {
            "affected_nodes": sorted(affected),
            "affected_contracts": sorted(references["affected_contracts"]),
            "affected_chapters": historical,
            "requires_replanning_from_chapter": min(future_contracts) if future_contracts else None,
            "historical_conflicts": conflicts,
            "warnings": [],
        }

    def _mark_contracts_stale(self, impact: Dict[str, Any], revision: int) -> None:
        ledger_manager = StoryLedgerManager(self.output_dir)
        ledger = (
            ledger_manager.load_suspense_ledger()
            if os.path.exists(ledger_manager.suspense_ledger_path)
            else {}
        )
        accepted = {
            int(item.get("chapter", 0))
            for item in ledger.get("accepted_chapters", [])
            if isinstance(item, dict)
        }
        for chapter in impact.get("affected_contracts", []):
            if chapter in accepted:
                continue
            path = ledger_manager.chapter_contract_path(int(chapter))
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    contract = json.load(handle)
            except (OSError, ValueError, TypeError):
                continue
            contract["stale"] = True
            contract["stale_reason"] = "referenced narrative graph elements changed"
            contract["stale_at_graph_revision"] = revision
            contract["stale_node_ids"] = sorted(
                contract_node_references(contract) & set(impact.get("affected_nodes", []))
            )
            contract["updated_at"] = _now()
            _atomic_write_json(path, contract)

    def _recompute_ledger_derived_state(self, graph: Dict[str, Any]) -> None:
        """Refresh graph-dependent runtime states after a structural revision."""
        ledger_manager = StoryLedgerManager(self.output_dir)
        ledger_manager.initialize()
        with project_lock(ledger_manager.suspense_ledger_lock_path):
            ledger = ledger_manager.load_suspense_ledger()
            self.apply_transitions_to_ledger(
                graph,
                ledger,
                {"chapter": self._current_chapter(ledger), "narrative_transitions": []},
            )
            cache = ledger.setdefault("narrative_derived_cache", {})
            cache["graph_revision"] = int(graph.get("revision", 0) or 0)
            cache["ledger_revision"] = int(ledger.get("revision", 0) or 0)
            ledger["updated_at"] = _now()
            _atomic_write_json(ledger_manager.suspense_ledger_path, ledger)

    def apply_change(
        self,
        operations: List[Dict[str, Any]],
        reason: str,
        expected_revision: int,
        actor: str = "system",
    ) -> int:
        if not str(reason).strip():
            raise NarrativeGraphError(
                [_issue("GRAPH_CHANGE_REASON_REQUIRED", "叙事图变更必须说明原因")]
            )
        # Validation and impact analysis are mandatory and intentionally run
        # before entering the write section.  They are repeated against the
        # locked revision below so no concurrent writer can invalidate them.
        ledger = self._load_ledger()
        initial_issues = self.validate_change(operations, ledger)
        initial_impact = self.analyze_impact(operations, ledger)
        if _has_errors(initial_issues):
            raise NarrativeGraphError(initial_issues)
        if initial_impact["historical_conflicts"]:
            raise NarrativeGraphError(initial_impact["historical_conflicts"])

        with project_lock(self.lock_path):
            graph = self._read_graph()
            base_revision = int(graph.get("revision", 0) or 0)
            if base_revision != int(expected_revision):
                raise RevisionConflictError(
                    f"Expected narrative graph revision {expected_revision}, found {base_revision}"
                )
            # Validate the exact locked snapshot without calling public load().
            candidate = self._apply_operations(graph, operations)
            locked_issues = self._validate_graph(candidate, ledger)
            if _has_errors(locked_issues):
                raise NarrativeGraphError(locked_issues)
            affected = self._affected_nodes_for_operations(graph, operations)
            result_revision = base_revision + 1
            candidate["version"] = GRAPH_VERSION
            candidate["revision"] = result_revision
            candidate["updated_at"] = _now()
            change = {
                "change_id": str(uuid.uuid4()),
                "timestamp": _now(),
                "actor": str(actor or "system"),
                "reason": str(reason),
                "base_revision": base_revision,
                "result_revision": result_revision,
                "operations": deepcopy(operations),
                "affected_nodes": sorted(affected),
                "affected_contracts": initial_impact["affected_contracts"],
                "affected_chapters": initial_impact["affected_chapters"],
                "requires_replanning_from_chapter": initial_impact[
                    "requires_replanning_from_chapter"
                ],
            }
            _atomic_write_json(self.graph_path, candidate)
            _atomic_append_jsonl(self.change_log_path, change)
        self._mark_contracts_stale(initial_impact, result_revision)
        self._recompute_ledger_derived_state(candidate)
        return result_revision

    def _state_for(self, node_id: str, ledger: Dict[str, Any]) -> Dict[str, Any]:
        states = ledger.get("narrative_node_states", {})
        if not isinstance(states, dict):
            return {}
        state = states.get(node_id, {})
        return state if isinstance(state, dict) else {}

    def _node_satisfied(
        self, node_id: str, graph: Dict[str, Any], ledger: Dict[str, Any]
    ) -> bool:
        node = self._node_map(graph).get(node_id, {})
        state = self._state_for(node_id, ledger)
        node_type = node.get("type")
        if node_type == "clue":
            return state.get("reader_status") in {
                "introduced",
                "seen",
                "inferable",
                "revealed",
                "public",
            }
        if node_type == "fact":
            return state.get("reader_status") in {"inferable", "revealed", "public"}
        if node_type == "reveal":
            return state.get("reveal_status") == "executed"
        if node_type == "thread":
            return state.get("thread_status") in {"open", "active", "closed"}
        return False

    def _missing_dependencies(
        self, node_id: str, graph: Dict[str, Any], ledger: Dict[str, Any]
    ) -> List[str]:
        required = [
            _edge_endpoint(edge, "source")
            for edge in self._active_edges(graph)
            if edge.get("type") == "requires"
            and _edge_endpoint(edge, "target") == node_id
        ]
        return sorted(
            dependency
            for dependency in required
            if not self._node_satisfied(dependency, graph, ledger)
        )

    def dependencies_satisfied(self, node_id: str, ledger: Dict[str, Any]) -> bool:
        graph = self.load()
        if node_id not in self._node_map(graph):
            return False
        return not self._missing_dependencies(node_id, graph, ledger)

    @staticmethod
    def _planned_chapter(node: Dict[str, Any]) -> int:
        for field in (
            "planned_reveal_chapter",
            "planned_resolve_chapter",
            "planned_chapter",
        ):
            try:
                if node.get(field) not in (None, ""):
                    return int(node[field])
            except (TypeError, ValueError):
                pass
        return 0

    def ready_reveals(
        self, chapter_number: int, ledger: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        graph = self.load()
        ready: List[Dict[str, Any]] = []
        for node_id, node in self._node_map(graph).items():
            if node.get("type") != "reveal" or node.get("deprecated"):
                continue
            state = self._state_for(node_id, ledger)
            if state.get("reveal_status") == "executed":
                continue
            planned = self._planned_chapter(node)
            if planned and planned > int(chapter_number):
                continue
            if not self._missing_dependencies(node_id, graph, ledger):
                item = deepcopy(node)
                item["derived_status"] = "ready"
                ready.append(item)
        return sorted(ready, key=lambda item: (self._planned_chapter(item), str(item["id"])))

    def blocked_reveals(
        self, chapter_number: int, ledger: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        graph = self.load()
        blocked: List[Dict[str, Any]] = []
        for node_id, node in self._node_map(graph).items():
            if node.get("type") != "reveal" or node.get("deprecated"):
                continue
            state = self._state_for(node_id, ledger)
            if state.get("reveal_status") == "executed":
                continue
            missing = self._missing_dependencies(node_id, graph, ledger)
            planned = self._planned_chapter(node)
            if missing or (planned and planned > int(chapter_number)):
                item = deepcopy(node)
                item["derived_status"] = "blocked"
                item["missing_node_ids"] = missing
                if planned and planned > int(chapter_number):
                    item["blocked_until_chapter"] = planned
                blocked.append(item)
        return sorted(blocked, key=lambda item: (self._planned_chapter(item), str(item["id"])))

    def _thread_required_reveals(
        self, thread_id: str, graph: Dict[str, Any], ledger: Dict[str, Any]
    ) -> List[str]:
        nodes = self._node_map(graph)
        reveal_ids: Set[str] = set()
        for edge in self._active_edges(graph):
            if (
                edge.get("type") == "advances"
                and _edge_endpoint(edge, "target") == thread_id
                and nodes.get(_edge_endpoint(edge, "source"), {}).get("type") == "reveal"
            ):
                reveal_ids.add(_edge_endpoint(edge, "source"))
        for node_id, node in nodes.items():
            if node.get("type") == "reveal" and str(node.get("thread_id", "")) == thread_id:
                reveal_ids.add(node_id)
        return sorted(
            reveal_id
            for reveal_id in reveal_ids
            if self._state_for(reveal_id, ledger).get("reveal_status") != "executed"
        )

    @staticmethod
    def _current_chapter(ledger: Dict[str, Any]) -> int:
        chapters = [
            int(item.get("chapter", 0) or 0)
            for item in ledger.get("accepted_chapters", [])
            if isinstance(item, dict)
        ]
        return max(chapters, default=0)

    @staticmethod
    def _important(node: Dict[str, Any]) -> bool:
        importance = str(node.get("importance", "")).lower()
        try:
            priority = int(node.get("priority", 0) or 0)
        except (TypeError, ValueError):
            priority = 0
        return importance in {"important", "high", "critical", "major"} or priority >= 2

    def _starvation_warnings(
        self, graph: Dict[str, Any], ledger: Dict[str, Any], chapter_number: int = 0
    ) -> List[Dict[str, Any]]:
        chapter = int(chapter_number or self._current_chapter(ledger))
        warnings: List[Dict[str, Any]] = []
        for node_id, node in self._node_map(graph).items():
            if node.get("type") != "thread" or node.get("deprecated") or not self._important(node):
                continue
            state = self._state_for(node_id, ledger)
            if state.get("thread_status") in {"closed", "abandoned"}:
                continue
            try:
                maximum = int(node.get("max_silent_chapters", 3) or 3)
                last = int(
                    state.get("last_advance_chapter")
                    or state.get("last_touch_chapter")
                    or state.get("opened_chapter")
                    or 0
                )
            except (TypeError, ValueError):
                continue
            if chapter - last > maximum:
                warnings.append(
                    _issue(
                        "THREAD_STARVED",
                        "重要情节线超过 max_silent_chapters 未推进",
                        severity="warning",
                        node_id=node_id,
                        details={
                            "current_chapter": chapter,
                            "last_advance_chapter": last,
                            "max_silent_chapters": maximum,
                        },
                    )
                )
        return warnings

    def thread_context(
        self, thread_ids: List[str], ledger: Dict[str, Any]
    ) -> Dict[str, Any]:
        graph = self.load()
        nodes = self._node_map(graph)
        selected = {thread_id for thread_id in thread_ids if thread_id in nodes}
        related = set(selected)
        related_edges = []
        for edge in self._active_edges(graph):
            source_id = _edge_endpoint(edge, "source")
            target_id = _edge_endpoint(edge, "target")
            if source_id in selected or target_id in selected:
                related.update((source_id, target_id))
                related_edges.append(deepcopy(edge))
        return {
            "threads": [
                {**deepcopy(nodes[node_id]), "state": deepcopy(self._state_for(node_id, ledger))}
                for node_id in sorted(selected)
            ],
            "related_nodes": [
                {**deepcopy(nodes[node_id]), "state": deepcopy(self._state_for(node_id, ledger))}
                for node_id in sorted(related - selected)
                if node_id in nodes
            ],
            "edges": related_edges,
            "graph_revision": int(graph.get("revision", 0) or 0),
            "ledger_revision": int(ledger.get("revision", 0) or 0),
        }

    # ------------------------------------------------------------------ 播种

    def seed_from_structure(
        self,
        sections: Sequence[Dict[str, Any]],
        section_last_chapter: Optional[Dict[int, int]] = None,
    ) -> Dict[str, Any]:
        """把结构阶段已经声明的悬念与真相变成叙事图节点。

        在此之前，生产代码里没有任何一处会往图里加节点——只有
        ``tools/run_e2e_10_chapters.py`` 会。于是图永远是空的，而契约校验只要发现
        契约引用了任何节点就会切到严格档，要求 ``primary_thread`` /
        ``primary_action`` / ``via_node_ids`` 全部指向真实存在的节点。空图上没有一条
        能满足，新项目的场景规划因此必然失败（``current_work`` 躲过只是因为它的契约
        恰好从未引用过节点）。

        播种不花任何调用：结构契约里的 ``threads_opened`` 与 ``truths_introduced``
        已经带着 id、文字和「最晚在第几部分了结/揭晓」，直接转成 thread 与 reveal
        节点即可。``section_last_chapter`` 把部分序号换成章号（章节大纲按部分分文件，
        章号连续），拿不到就不写计划章号——那只影响「悬念沉默过久」这类提示，不影响
        节点本身可选。

        重复调用是安全的：已经在图上的 id 不会再加一次。
        """
        existing = {str(node.get("id", "")) for node in self.load().get("nodes", [])}
        mapping = dict(section_last_chapter or {})
        operations: List[Dict[str, Any]] = []
        seen: Set[str] = set()

        for section in sorted(
            [item for item in sections if isinstance(item, dict)],
            key=lambda item: int(item.get("section_index", 0) or 0),
        ):
            for record in section.get("threads_opened", []) or []:
                node = _thread_node(record, mapping)
                if node and node["id"] not in existing and node["id"] not in seen:
                    seen.add(node["id"])
                    operations.append({"op": "add_node", "node": node})
            for record in section.get("truths_introduced", []) or []:
                node = _fact_node(record)
                if node and node["id"] not in existing and node["id"] not in seen:
                    seen.add(node["id"])
                    operations.append({"op": "add_node", "node": node})

        if not operations:
            return {"added": 0, "revision": self.current_revision()}

        revision = self.apply_change(
            operations,
            "从结构契约播种：把已声明的悬念与真相登记为叙事图节点",
            self.current_revision(),
        )
        return {"added": len(operations), "revision": revision}


    def planning_context(
        self, chapter_number: int, ledger: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        ledger = deepcopy(ledger if ledger is not None else self._load_ledger())
        graph = self.load()
        active_threads: List[Dict[str, Any]] = []
        available_threads: List[Dict[str, Any]] = []
        due: List[Dict[str, Any]] = []
        chapter = int(chapter_number)
        for node_id, node in self._node_map(graph).items():
            if node.get("type") != "thread" or node.get("deprecated"):
                continue
            state = self._state_for(node_id, ledger)
            if state.get("thread_status") not in {"closed", "abandoned"}:
                available_threads.append({**deepcopy(node), "state": deepcopy(state)})
            if state.get("thread_status") in {"open", "active"}:
                item = {**deepcopy(node), "state": deepcopy(state)}
                active_threads.append(item)
                planned = self._planned_chapter(node)
                if planned and planned <= chapter + 2:
                    due.append(item)
        starvation = self._starvation_warnings(graph, ledger, chapter)
        starved_ids = {item.get("node_id") for item in starvation}
        ready = self.ready_reveals(chapter, ledger)
        blocked = self.blocked_reveals(chapter, ledger)
        blocked_ids = {str(item.get("id")) for item in blocked}
        available_clues = []
        forbidden_nodes = []
        for node_id, node in self._node_map(graph).items():
            state = self._state_for(node_id, ledger)
            if node.get("deprecated"):
                forbidden_nodes.append(deepcopy(node))
            elif node.get("type") == "clue":
                item = {**deepcopy(node), "state": deepcopy(state)}
                item["available_action"] = (
                    "reuse"
                    if state.get("reader_status")
                    in {"introduced", "seen", "inferable", "revealed"}
                    else "introduce_to_reader"
                )
                available_clues.append(item)
        forbidden_nodes.extend(
            deepcopy(self._node_map(graph)[node_id])
            for node_id in sorted(blocked_ids)
            if node_id in self._node_map(graph)
        )
        return {
            "active_threads": active_threads,
            "available_threads": available_threads,
            "threads_due_soon": due,
            "starved_threads": [
                item for item in active_threads if str(item.get("id")) in starved_ids
            ],
            "ready_reveals": ready,
            "blocked_reveals": blocked,
            "available_clues": available_clues,
            "available_facts": [
                {**deepcopy(node), "state": deepcopy(self._state_for(node_id, ledger))}
                for node_id, node in self._node_map(graph).items()
                if node.get("type") == "fact" and not node.get("deprecated")
            ],
            "forbidden_nodes": forbidden_nodes,
            "graph_revision": int(graph.get("revision", 0) or 0),
            "ledger_revision": int(ledger.get("revision", 0) or 0),
            "warnings": starvation,
        }

    def _changes_after(self, revision: int) -> List[Dict[str, Any]]:
        if not os.path.exists(self.change_log_path):
            return []
        changes: List[Dict[str, Any]] = []
        try:
            with open(self.change_log_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    if int(record.get("result_revision", 0) or 0) > int(revision):
                        changes.append(record)
        except (OSError, ValueError, TypeError):
            return []
        return changes

    def validate_contract(
        self,
        contract: Dict[str, Any],
        ledger: Optional[Dict[str, Any]] = None,
        *,
        ignore_revision: bool = False,
    ) -> List[Dict[str, Any]]:
        graph = self.load()
        ledger = ledger if ledger is not None else self._load_ledger()
        nodes = self._node_map(graph)
        issues: List[Dict[str, Any]] = []
        chapter = int(contract.get("chapter", 0) or 0)
        refs = contract_node_references(contract)
        graph_backed_contract = bool(refs)
        for node_id in sorted(refs):
            node = nodes.get(node_id)
            if node is None:
                issues.append(
                    _issue(
                        "CONTRACT_NODE_NOT_FOUND",
                        "ChapterContract 引用了不存在的叙事图节点",
                        node_id=node_id,
                        details={"chapter": chapter},
                    )
                )
            elif node.get("deprecated"):
                issues.append(
                    _issue(
                        "CONTRACT_NODE_FORBIDDEN",
                        "ChapterContract 引用了已禁止的叙事图节点",
                        node_id=node_id,
                        details={"chapter": chapter},
                    )
                )
        if graph_backed_contract:
            primary_thread = str(contract.get("primary_thread", "")).strip()
            primary_action = str(contract.get("primary_action", "")).strip().lower()
            if not primary_thread:
                issues.append(
                    _issue(
                        "PRIMARY_THREAD_REQUIRED",
                        "图契约必须明确选择 primary_thread",
                        details={"chapter": chapter},
                    )
                )
            elif nodes.get(primary_thread, {}).get("type") != "thread":
                issues.append(
                    _issue(
                        "PRIMARY_THREAD_INVALID",
                        "primary_thread 必须引用 thread 节点",
                        node_id=primary_thread,
                        details={"chapter": chapter},
                    )
                )
            if primary_action not in ALLOWED_THREAD_ACTIONS:
                issues.append(
                    _issue(
                        "PRIMARY_ACTION_REQUIRED",
                        "图契约必须明确选择有效的 primary_action",
                        details={
                            "chapter": chapter,
                            "allowed_actions": sorted(ALLOWED_THREAD_ACTIONS),
                        },
                    )
                )
            managed_updates = {
                str(update.get("id", "")): str(
                    update.get("action") or update.get("status") or ""
                ).lower()
                for update in contract.get("plot_thread_updates", [])
                if isinstance(update, dict)
                and (
                    update.get("narrative_graph_managed") is True
                    or update.get("narrative_graph_managed") is not False
                    and ("action" in update or update.get("via_node_ids"))
                )
            }
            if primary_thread and managed_updates.get(primary_thread) not in {
                primary_action,
                "closed" if primary_action == "close" else primary_action,
            }:
                issues.append(
                    _issue(
                        "PRIMARY_THREAD_UPDATE_REQUIRED",
                        "primary_thread/primary_action 必须有对应的 plot_thread_updates 记录",
                        node_id=primary_thread,
                        details={"chapter": chapter, "primary_action": primary_action},
                    )
                )
            secondary_thread = str(contract.get("secondary_thread", "")).strip()
            secondary_action = str(contract.get("secondary_action", "")).strip().lower()
            if secondary_thread and nodes.get(secondary_thread, {}).get("type") != "thread":
                issues.append(
                    _issue(
                        "SECONDARY_THREAD_INVALID",
                        "secondary_thread 必须引用 thread 节点",
                        node_id=secondary_thread,
                        details={"chapter": chapter},
                    )
                )
            if secondary_thread and secondary_action not in ALLOWED_THREAD_ACTIONS:
                issues.append(
                    _issue(
                        "SECONDARY_ACTION_REQUIRED",
                        "选择 secondary_thread 后必须给出有效的 secondary_action",
                        node_id=secondary_thread,
                        details={"chapter": chapter},
                    )
                )
            if secondary_thread and managed_updates.get(secondary_thread) not in {
                secondary_action,
                "closed" if secondary_action == "close" else secondary_action,
            }:
                issues.append(
                    _issue(
                        "SECONDARY_THREAD_UPDATE_REQUIRED",
                        "secondary_thread/secondary_action 必须有对应的 plot_thread_updates 记录",
                        node_id=secondary_thread,
                        details={"chapter": chapter, "secondary_action": secondary_action},
                    )
                )
            if not secondary_thread and secondary_action:
                issues.append(
                    _issue(
                        "SECONDARY_ACTION_WITHOUT_THREAD",
                        "未选择 secondary_thread 时 secondary_action 必须为空",
                        details={"chapter": chapter},
                    )
                )
            for field in (
                "allowed_reveals",
                "forbidden_reveals",
                "intentionally_silent_threads",
            ):
                if not isinstance(contract.get(field), list):
                    issues.append(
                        _issue(
                            "NARRATIVE_SELECTION_FIELD_REQUIRED",
                            f"图契约字段 {field} 必须明确提供数组（允许为空）",
                            details={"chapter": chapter, "field": field},
                        )
                    )
        for update in contract.get("plot_thread_updates", []):
            if not isinstance(update, dict):
                issues.append(_issue("CONTRACT_THREAD_UPDATE_INVALID", "情节线更新必须是对象"))
                continue
            action = str(update.get("action") or update.get("status") or "").lower()
            if action == "closed":
                action = "close"
            if action not in ALLOWED_THREAD_ACTIONS:
                issues.append(
                    _issue(
                        "CONTRACT_THREAD_ACTION_INVALID",
                        f"不支持的情节线 action：{action}",
                        node_id=str(update.get("id", "")),
                    )
                )
            if action == "advance" and not _node_id_set(update.get("via_node_ids")):
                issues.append(
                    _issue(
                        "THREAD_ADVANCE_WITHOUT_NODE",
                        "Contract 声明 advance 情节线时必须提供 via_node_ids",
                        node_id=str(update.get("id", "")),
                    )
                )
        simulated = deepcopy(ledger)
        transitions = contract.get("narrative_transitions", [])
        if not isinstance(transitions, list):
            issues.append(_issue("CONTRACT_TRANSITIONS_INVALID", "narrative_transitions 必须是数组"))
            transitions = []
        for transition in transitions:
            if not isinstance(transition, dict):
                issues.append(_issue("CONTRACT_TRANSITION_INVALID", "叙事状态转换必须是对象"))
                continue
            node_id = str(transition.get("node_id", ""))
            name = str(transition.get("transition", ""))
            if name not in ALLOWED_TRANSITIONS:
                issues.append(
                    _issue(
                        "CONTRACT_TRANSITION_INVALID",
                        f"不支持的叙事状态转换：{name}",
                        node_id=node_id,
                    )
                )
                continue
            if node_id not in nodes:
                continue
            node_type = nodes[node_id].get("type")
            allowed_node_types = {
                "introduce_to_reader": {"clue", "fact"},
                "make_inferable": {"fact"},
                "reveal": {"fact", "reveal"},
                "execute": {"reveal"},
                "deprecate": ALLOWED_NODE_TYPES,
            }[name]
            if node_type not in allowed_node_types:
                issues.append(
                    _issue(
                        "TRANSITION_NODE_TYPE_INVALID",
                        f"{name} 不能应用到 {node_type} 节点",
                        node_id=node_id,
                        details={"allowed_node_types": sorted(allowed_node_types)},
                    )
                )
                continue
            if name == "make_inferable":
                unseen_clues = sorted(
                    _edge_endpoint(edge, "source")
                    for edge in self._active_edges(graph)
                    if edge.get("type") == "supports"
                    and _edge_endpoint(edge, "target") == node_id
                    and nodes.get(_edge_endpoint(edge, "source"), {}).get("type") == "clue"
                    and not self._node_satisfied(
                        _edge_endpoint(edge, "source"), graph, simulated
                    )
                )
                if unseen_clues:
                    issues.append(
                        _issue(
                            "FAIR_INFERENCE_CLUE_UNSEEN",
                            "读者尚未见过的 clue 不能用于公平推理",
                            node_id=node_id,
                            details={"unseen_clue_ids": unseen_clues, "chapter": chapter},
                        )
                    )
            if name in {"execute", "reveal"} and nodes[node_id].get("type") == "reveal":
                missing = self._missing_dependencies(node_id, graph, simulated)
                if missing:
                    issues.append(
                        _issue(
                            "REVEAL_DEPENDENCY_UNSATISFIED",
                            "揭示的前置条件尚未满足",
                            node_id=node_id,
                            details={"missing_node_ids": missing, "chapter": chapter},
                        )
                    )
            self.apply_transitions_to_ledger(
                graph, simulated, {"chapter": chapter, "narrative_transitions": [transition]}
            )

        for update in contract.get("plot_thread_updates", []):
            if not isinstance(update, dict):
                continue
            action = str(update.get("action") or update.get("status") or "").lower()
            if action == "closed":
                action = "close"
            thread_id = str(update.get("id", ""))
            if action == "close" and thread_id in nodes:
                missing = self._thread_required_reveals(thread_id, graph, simulated)
                if missing:
                    issues.append(
                        _issue(
                            "THREAD_CLOSED_BEFORE_REQUIRED_REVEALS",
                            "情节线关闭前必须完成其要求的揭示",
                            node_id=thread_id,
                            details={"missing_reveal_ids": missing, "chapter": chapter},
                        )
                    )

        blocked = {str(item.get("id")) for item in self.blocked_reveals(chapter, ledger)}
        selected = _node_id_set(contract.get("allowed_reveals"))
        selected.update(
            str(item.get("node_id"))
            for item in transitions
            if isinstance(item, dict) and item.get("transition") in {"execute", "reveal"}
        )
        planned_executions = {
            str(item.get("node_id"))
            for item in transitions
            if isinstance(item, dict)
            and item.get("transition") in {"execute", "reveal"}
            and nodes.get(str(item.get("node_id")), {}).get("type") == "reveal"
        }
        undeclared_executions = planned_executions - _node_id_set(
            contract.get("allowed_reveals")
        )
        for node_id in sorted(undeclared_executions):
            issues.append(
                _issue(
                    "REVEAL_NOT_ALLOWED_BY_CONTRACT",
                    "计划执行的 reveal 必须列入 allowed_reveals",
                    node_id=node_id,
                    details={"chapter": chapter},
                )
            )
        for node_id in sorted(selected & blocked):
            issues.append(
                _issue(
                    "BLOCKED_REVEAL_SELECTED",
                    "规划器不得选择 blocked_reveals",
                    node_id=node_id,
                    details={"chapter": chapter},
                )
            )
        forbidden = _node_id_set(contract.get("forbidden_nodes")) | _node_id_set(
            contract.get("forbidden_reveals")
        )
        for node_id in sorted(selected & forbidden):
            issues.append(
                _issue(
                    "FORBIDDEN_NODE_SELECTED",
                    "规划器选择了明确禁止的节点",
                    node_id=node_id,
                    details={"chapter": chapter},
                )
            )

        introduced = {
            str(item.get("node_id"))
            for item in transitions
            if isinstance(item, dict) and item.get("transition") == "introduce_to_reader"
        }
        executed = {
            str(item.get("node_id"))
            for item in transitions
            if isinstance(item, dict) and item.get("transition") in {"execute", "reveal"}
        }
        if introduced and executed and not str(
            contract.get("same_chapter_resolution_exception_reason", "")
        ).strip():
            introduced_threads = {
                _edge_endpoint(edge, "target")
                for edge in self._active_edges(graph)
                if edge.get("type") == "belongs_to"
                and _edge_endpoint(edge, "source") in introduced
                and self._important(nodes.get(_edge_endpoint(edge, "target"), {}))
            }
            executed_threads = {
                _edge_endpoint(edge, "target")
                for edge in self._active_edges(graph)
                if edge.get("type") == "advances"
                and _edge_endpoint(edge, "source") in executed
            }
            if introduced_threads & executed_threads:
                issues.append(
                    _issue(
                        "IMPORTANT_MYSTERY_SAME_CHAPTER_RESOLUTION",
                        "同一章不能首次提出重要谜面并彻底解决，除非契约说明例外原因",
                        details={
                            "chapter": chapter,
                            "thread_ids": sorted(introduced_threads & executed_threads),
                        },
                    )
                )

        declared_revision = int(contract.get("narrative_graph_revision", 0) or 0)
        current_revision = int(graph.get("revision", 0) or 0)
        if not ignore_revision and declared_revision != current_revision:
            changed_nodes: Set[str] = set()
            for change in self._changes_after(declared_revision):
                changed_nodes.update(str(item) for item in change.get("affected_nodes", []))
            if refs & changed_nodes or contract.get("stale"):
                issues.append(
                    _issue(
                        "CONTRACT_GRAPH_REVISION_STALE",
                        "ChapterContract 引用的叙事图元素已经变化，必须重新规划",
                        details={
                            "chapter": chapter,
                            "contract_revision": declared_revision,
                            "current_revision": current_revision,
                            "affected_node_ids": sorted(refs & changed_nodes),
                        },
                    )
                )
            else:
                issues.append(
                    _issue(
                        "CONTRACT_GRAPH_REVISION_REVALIDATION_REQUIRED",
                        "图 revision 已变化，但本契约引用未受影响；重新校验后可继续",
                        severity="warning",
                        details={
                            "chapter": chapter,
                            "contract_revision": declared_revision,
                            "current_revision": current_revision,
                        },
                    )
                )
        return issues

    def reconcile_contract(
        self,
        contract: Dict[str, Any],
        ledger: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        reconciled = deepcopy(contract)
        issues = self.validate_contract(reconciled, ledger)
        if _has_errors(issues):
            reconciled["stale"] = True
            reconciled["narrative_graph_validation"] = issues
            return reconciled, issues
        current = self.current_revision()
        reconciled["narrative_graph_revision"] = current
        reconciled["stale"] = False
        reconciled.pop("stale_reason", None)
        reconciled.pop("stale_node_ids", None)
        reconciled["narrative_graph_validation"] = issues
        reconciled["graph_revalidated_at"] = _now()
        return reconciled, issues

    def validate_delta(
        self, delta: Dict[str, Any], ledger: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        graph = self.load()
        nodes = self._node_map(graph)
        issues: List[Dict[str, Any]] = []
        chapter = int(delta.get("chapter", 0) or 0)
        for node_id in sorted(delta_node_references(delta)):
            node = nodes.get(node_id)
            if node is None:
                issues.append(
                    _issue(
                        "DELTA_NODE_NOT_FOUND",
                        "ChapterDelta 引用了不存在的叙事图节点",
                        node_id=node_id,
                        details={"chapter": chapter},
                    )
                )
            elif node.get("deprecated"):
                issues.append(
                    _issue(
                        "DELTA_NODE_FORBIDDEN",
                        "ChapterDelta 引用了已禁止的叙事图节点",
                        node_id=node_id,
                        details={"chapter": chapter},
                    )
                )
        simulated = deepcopy(ledger)
        transitions = delta.get("narrative_transitions", [])
        if not isinstance(transitions, list):
            return issues + [_issue("DELTA_TRANSITIONS_INVALID", "narrative_transitions 必须是数组")]
        for transition in transitions:
            if not isinstance(transition, dict):
                issues.append(_issue("DELTA_TRANSITION_INVALID", "Delta 状态转换必须是对象"))
                continue
            node_id = str(transition.get("node_id", ""))
            name = str(transition.get("transition", ""))
            if name not in ALLOWED_TRANSITIONS:
                issues.append(
                    _issue(
                        "DELTA_TRANSITION_INVALID",
                        f"不支持的 Delta 状态转换：{name}",
                        node_id=node_id,
                    )
                )
                continue
            if node_id not in nodes:
                continue
            node_type = nodes[node_id].get("type")
            allowed_node_types = {
                "introduce_to_reader": {"clue", "fact"},
                "make_inferable": {"fact"},
                "reveal": {"fact", "reveal"},
                "execute": {"reveal"},
                "deprecate": ALLOWED_NODE_TYPES,
            }[name]
            if node_type not in allowed_node_types:
                issues.append(
                    _issue(
                        "TRANSITION_NODE_TYPE_INVALID",
                        f"{name} 不能应用到 {node_type} 节点",
                        node_id=node_id,
                        details={"allowed_node_types": sorted(allowed_node_types)},
                    )
                )
                continue
            if name == "make_inferable":
                unseen_clues = sorted(
                    _edge_endpoint(edge, "source")
                    for edge in self._active_edges(graph)
                    if edge.get("type") == "supports"
                    and _edge_endpoint(edge, "target") == node_id
                    and nodes.get(_edge_endpoint(edge, "source"), {}).get("type") == "clue"
                    and not self._node_satisfied(
                        _edge_endpoint(edge, "source"), graph, simulated
                    )
                )
                if unseen_clues:
                    issues.append(
                        _issue(
                            "FAIR_INFERENCE_CLUE_UNSEEN",
                            "读者尚未见过的 clue 不能用于公平推理",
                            node_id=node_id,
                            details={"unseen_clue_ids": unseen_clues, "chapter": chapter},
                        )
                    )
            if name in {"execute", "reveal"} and node_type == "reveal":
                missing = self._missing_dependencies(node_id, graph, simulated)
                if missing:
                    issues.append(
                        _issue(
                            "REVEAL_DEPENDENCY_UNSATISFIED",
                            "揭示的前置条件尚未满足",
                            node_id=node_id,
                            details={"missing_node_ids": missing, "chapter": chapter},
                        )
                    )
            self.apply_transitions_to_ledger(
                graph, simulated, {"chapter": chapter, "narrative_transitions": [transition]}
            )
        for update in delta.get("plot_thread_updates", []):
            if not isinstance(update, dict):
                continue
            action = str(update.get("action") or update.get("status") or "").lower()
            if action == "closed":
                action = "close"
            thread_id = str(update.get("id", ""))
            if action == "close" and thread_id in nodes:
                missing = self._thread_required_reveals(thread_id, graph, simulated)
                if missing:
                    issues.append(
                        _issue(
                            "THREAD_CLOSED_BEFORE_REQUIRED_REVEALS",
                            "情节线关闭前必须完成其要求的揭示",
                            node_id=thread_id,
                            details={"missing_reveal_ids": missing, "chapter": chapter},
                        )
                    )
        return issues

    def apply_transitions_to_ledger(
        self, graph: Dict[str, Any], ledger: Dict[str, Any], delta: Dict[str, Any]
    ) -> None:
        """Apply graph-referenced runtime transitions to an in-memory ledger."""
        chapter = int(delta.get("chapter", 0) or 0)
        nodes = self._node_map(graph)
        states = ledger.setdefault("narrative_node_states", {})
        for transition in delta.get("narrative_transitions", []):
            if not isinstance(transition, dict):
                continue
            node_id = str(transition.get("node_id", ""))
            name = str(transition.get("transition", ""))
            node = nodes.get(node_id, {})
            state = states.setdefault(node_id, {})
            chapters = state.setdefault("actual_chapters", [])
            if chapter and chapter not in chapters:
                chapters.append(chapter)
            if name == "introduce_to_reader":
                state["reader_status"] = "introduced"
                state.setdefault("introduced_chapter", chapter)
            elif name == "make_inferable":
                state["reader_status"] = "inferable"
                state.setdefault("inferable_chapter", chapter)
            elif name in {"reveal", "execute"}:
                if node.get("type") == "reveal":
                    state["reveal_status"] = "executed"
                    state.setdefault("executed_chapter", chapter)
                else:
                    state["reader_status"] = "revealed"
                    state.setdefault("revealed_chapter", chapter)
            elif name == "deprecate":
                state["runtime_status"] = "deprecated"
                state.setdefault("deprecated_chapter", chapter)

        for update in delta.get("plot_thread_updates", []):
            if not isinstance(update, dict):
                continue
            thread_id = str(update.get("id", ""))
            if not thread_id or thread_id not in nodes:
                continue
            action = str(update.get("action") or update.get("status") or "").lower()
            if action == "closed":
                action = "close"
            state = states.setdefault(thread_id, {})
            if action == "open":
                state["thread_status"] = "open"
                state.setdefault("opened_chapter", chapter)
                state["last_touch_chapter"] = chapter
            elif action in {"touch", "complicate", "cross"}:
                state.setdefault("thread_status", "active")
                state["last_touch_chapter"] = chapter
            elif action == "advance":
                state["thread_status"] = "active"
                state["last_touch_chapter"] = chapter
                state["last_advance_chapter"] = chapter
            elif action == "close":
                state["thread_status"] = "closed"
                state["last_touch_chapter"] = chapter
                state["closed_chapter"] = chapter

        # ready/blocked is stored in StoryLedger as the current runtime state,
        # while the lists themselves remain derived and revision-stamped.
        for node_id, node in nodes.items():
            if node.get("type") != "reveal" or node.get("deprecated"):
                continue
            state = states.setdefault(node_id, {})
            if state.get("reveal_status") == "executed":
                continue
            state["reveal_status"] = (
                "blocked" if self._missing_dependencies(node_id, graph, ledger) else "ready"
            )
        ledger["narrative_derived_cache"] = {
            "graph_revision": int(graph.get("revision", 0) or 0),
            "ledger_revision": int(ledger.get("revision", 0) or 0),
            "computed_at": _now(),
        }
