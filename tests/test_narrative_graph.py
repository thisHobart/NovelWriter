import json
from pathlib import Path

import pytest

from core.generation.chapter_acceptance import ChapterAcceptanceService
from core.generation.narrative_graph import NarrativeGraphError, NarrativeGraphManager
from core.generation.story_ledger import RevisionConflictError, StoryLedgerManager, source_hash


def _operations(*, second_reveal=False):
    operations = [
        {
            "op": "add_node",
            "node": {
                "id": "PT-MAIN",
                "type": "thread",
                "label": "主案",
                "importance": "high",
                "max_silent_chapters": 2,
                "planned_resolve_chapter": 8,
            },
        },
        {
            "op": "add_node",
            "node": {
                "id": "C-CAMERA",
                "type": "clue",
                "surface_meaning": "摄像头失灵",
                "true_meaning": "时间被篡改",
            },
        },
        {"op": "add_node", "node": {"id": "F-TIME", "type": "fact", "label": "死亡时间"}},
        {
            "op": "add_node",
            "node": {
                "id": "R-BODY-MOVED",
                "type": "reveal",
                "label": "尸体被移动",
                "planned_reveal_chapter": 2,
            },
        },
        {
            "op": "add_edge",
            "edge": {
                "id": "E-BELONGS",
                "type": "belongs_to",
                "source_id": "C-CAMERA",
                "target_id": "PT-MAIN",
            },
        },
        {
            "op": "add_edge",
            "edge": {
                "id": "E-SUPPORTS",
                "type": "supports",
                "source_id": "C-CAMERA",
                "target_id": "F-TIME",
            },
        },
        {
            "op": "add_edge",
            "edge": {
                "id": "E-REQUIRES",
                "type": "requires",
                "source_id": "C-CAMERA",
                "target_id": "R-BODY-MOVED",
            },
        },
        {
            "op": "add_edge",
            "edge": {
                "id": "E-ADVANCES",
                "type": "advances",
                "source_id": "R-BODY-MOVED",
                "target_id": "PT-MAIN",
            },
        },
    ]
    if second_reveal:
        operations.extend(
            [
                {
                    "op": "add_node",
                    "node": {"id": "R-TIME", "type": "reveal", "label": "真实时间"},
                },
                {
                    "op": "add_edge",
                    "edge": {
                        "id": "E-REQ-FACT",
                        "type": "requires",
                        "source_id": "F-TIME",
                        "target_id": "R-TIME",
                    },
                },
                {
                    "op": "add_edge",
                    "edge": {
                        "id": "E-ADV-TIME",
                        "type": "advances",
                        "source_id": "R-TIME",
                        "target_id": "PT-MAIN",
                    },
                },
            ]
        )
    return operations


def _graph(tmp_path, *, second_reveal=False):
    manager = NarrativeGraphManager(str(tmp_path))
    manager.apply_change(
        _operations(second_reveal=second_reveal),
        reason="initial story design",
        expected_revision=0,
        actor="planner",
    )
    return manager


def _ledger(tmp_path):
    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Mystery"})
    return manager


def _delta(chapter, revision, transitions, *, plot_updates=None, marker="a"):
    return {
        "chapter": chapter,
        "base_revision": revision,
        "content_hash": source_hash(f"chapter-{chapter}-{marker}"),
        "contract_hash": source_hash(f"contract-{chapter}-{marker}"),
        "facts_added": [],
        "facts_confirmed": [],
        "timeline_events": [],
        "character_updates": [],
        "plot_thread_updates": plot_updates or [],
        "narrative_transitions": transitions,
        "unregistered_narrative_elements": [],
    }


def _contract(chapter, graph_revision, *, transitions=None, updates=None):
    transitions = transitions or []
    updates = updates or [{"id": "PT-MAIN", "action": "touch"}]
    primary_action = str((updates[0] if updates else {}).get("action") or "touch")
    allowed_reveals = [
        str(item.get("node_id"))
        for item in transitions
        if isinstance(item, dict)
        and item.get("node_id") in {"R-BODY-MOVED", "R-TIME"}
        and item.get("transition") in {"execute", "reveal"}
    ]
    return {
        "chapter": chapter,
        "origin": "scene_planning",
        "schema_version": 2,
        "narrative_graph_revision": graph_revision,
        "primary_thread": "PT-MAIN",
        "secondary_thread": "",
        "primary_action": primary_action,
        "secondary_action": "",
        "crossover": "",
        "allowed_reveals": allowed_reveals,
        "forbidden_reveals": [],
        "intentionally_silent_threads": [],
        "plot_thread_updates": updates,
        "narrative_transitions": transitions,
        "facts_added": [],
        "facts_confirmed": [],
        "facts_contradicted": [],
        "timeline_events": [],
        "character_updates": [],
        "reader_knows_after": [],
    }


def test_first_load_initialises_empty_graph(tmp_path):
    graph = NarrativeGraphManager(str(tmp_path)).load()
    assert graph == {"version": 1, "revision": 0, "nodes": [], "edges": [], "updated_at": ""}
    assert (tmp_path / "system" / "story_ledgers" / "narrative_graph.json").is_file()


def test_adds_four_node_and_edge_types(tmp_path):
    manager = _graph(tmp_path)
    graph = manager.load()
    assert {node["type"] for node in graph["nodes"]} == {"thread", "clue", "fact", "reveal"}
    assert {edge["type"] for edge in graph["edges"]} == {
        "belongs_to",
        "supports",
        "requires",
        "advances",
    }
    assert graph["revision"] == 1


def test_terminal_audit_rejects_due_reveal_and_thread_left_open(tmp_path):
    manager = _graph(tmp_path)
    ledger = {
        "narrative_node_states": {
            "R-BODY-MOVED": {"reveal_status": "ready"},
            "PT-MAIN": {"thread_status": "active"},
        }
    }

    issues = manager.validate_terminal_state(8, ledger)

    assert {item["code"] for item in issues} == {
        "PLANNED_REVEAL_UNRESOLVED_AT_END",
        "PLANNED_THREAD_UNCLOSED_AT_END",
    }


def test_terminal_audit_accepts_executed_reveal_and_closed_thread(tmp_path):
    manager = _graph(tmp_path)
    ledger = {
        "narrative_node_states": {
            "R-BODY-MOVED": {"reveal_status": "executed"},
            "PT-MAIN": {"thread_status": "closed"},
        }
    }

    assert manager.validate_terminal_state(8, ledger) == []


@pytest.mark.parametrize(
    "operation,code",
    [
        ({"op": "add_node", "node": {"id": "F-TIME", "type": "fact"}}, "DUPLICATE_NODE_ID"),
        (
            {
                "op": "add_edge",
                "edge": {
                    "id": "E-REQUIRES",
                    "type": "requires",
                    "source_id": "C-CAMERA",
                    "target_id": "R-BODY-MOVED",
                },
            },
            "DUPLICATE_EDGE_ID",
        ),
    ],
)
def test_duplicate_node_or_edge_is_rejected(tmp_path, operation, code):
    manager = _graph(tmp_path)
    with pytest.raises(NarrativeGraphError) as exc:
        manager.apply_change([operation], "duplicate", 1)
    assert any(issue["code"] == code for issue in exc.value.issues)


def test_edge_to_missing_node_is_rejected(tmp_path):
    manager = _graph(tmp_path)
    with pytest.raises(NarrativeGraphError) as exc:
        manager.apply_change(
            [
                {
                    "op": "add_edge",
                    "edge": {
                        "id": "E-BAD",
                        "type": "supports",
                        "source_id": "C-CAMERA",
                        "target_id": "F-MISSING",
                    },
                }
            ],
            "bad reference",
            1,
        )
    assert any(issue["code"] == "EDGE_NODE_NOT_FOUND" for issue in exc.value.issues)


def test_requires_cycle_is_rejected(tmp_path):
    manager = _graph(tmp_path)
    with pytest.raises(NarrativeGraphError) as exc:
        manager.apply_change(
            [
                {"op": "add_node", "node": {"id": "F-A", "type": "fact"}},
                {"op": "add_node", "node": {"id": "F-B", "type": "fact"}},
                {
                    "op": "add_edge",
                    "edge": {"id": "E-A-B", "type": "requires", "source_id": "F-A", "target_id": "F-B"},
                },
                {
                    "op": "add_edge",
                    "edge": {"id": "E-B-A", "type": "requires", "source_id": "F-B", "target_id": "F-A"},
                },
            ],
            "cycle",
            1,
        )
    assert any(issue["code"] == "REQUIRES_CYCLE" for issue in exc.value.issues)


def test_clue_without_thread_fails_validation(tmp_path):
    manager = NarrativeGraphManager(str(tmp_path))
    with pytest.raises(NarrativeGraphError) as exc:
        manager.apply_change(
            [{"op": "add_node", "node": {"id": "C-ORPHAN", "type": "clue"}}],
            "orphan clue",
            0,
        )
    assert any(issue["code"] == "CLUE_WITHOUT_THREAD" for issue in exc.value.issues)


def test_reveal_without_prerequisite_fails_validation(tmp_path):
    manager = NarrativeGraphManager(str(tmp_path))
    with pytest.raises(NarrativeGraphError) as exc:
        manager.apply_change(
            [{"op": "add_node", "node": {"id": "R-EMPTY", "type": "reveal"}}],
            "orphan reveal",
            0,
        )
    assert any(issue["code"] == "REVEAL_WITHOUT_PREREQUISITE" for issue in exc.value.issues)


def test_unseen_clue_blocks_reveal_and_seen_clue_makes_it_ready(tmp_path):
    graph = _graph(tmp_path)
    ledger_manager = _ledger(tmp_path)
    ledger = ledger_manager.load_suspense_ledger()
    assert [node["id"] for node in graph.blocked_reveals(2, ledger)] == ["R-BODY-MOVED"]
    unfair = graph.validate_delta(
        _delta(
            1,
            0,
            [{"node_id": "F-TIME", "transition": "make_inferable"}],
        ),
        ledger,
    )
    assert any(issue["code"] == "FAIR_INFERENCE_CLUE_UNSEEN" for issue in unfair)
    graph.apply_transitions_to_ledger(
        graph.load(),
        ledger,
        {
            "chapter": 1,
            "narrative_transitions": [
                {"node_id": "C-CAMERA", "transition": "introduce_to_reader"}
            ],
        },
    )
    assert [node["id"] for node in graph.ready_reveals(2, ledger)] == ["R-BODY-MOVED"]


def test_reveal_delta_updates_ledger_and_reapplication_is_idempotent(tmp_path):
    graph = _graph(tmp_path)
    ledger = _ledger(tmp_path)
    first = _delta(
        1,
        0,
        [{"node_id": "C-CAMERA", "transition": "introduce_to_reader"}],
    )
    revision, _ = ledger.accept_chapter_with_delta(1, {}, {}, first, 0)
    assert revision == 1
    second = _delta(
        2,
        1,
        [{"node_id": "R-BODY-MOVED", "transition": "execute"}],
    )
    revision, path = ledger.accept_chapter_with_delta(2, {}, {}, second, 1)
    repeated, repeated_path = ledger.accept_chapter_with_delta(2, {}, {}, second, 1)
    state = ledger.load_suspense_ledger()
    assert revision == repeated == 2
    assert path == repeated_path
    assert state["narrative_node_states"]["R-BODY-MOVED"]["reveal_status"] == "executed"
    assert len([item for item in state["chapter_commits"] if item["chapter"] == 2]) == 1
    assert graph.ready_reveals(3, state) == []


def test_revision_failure_does_not_leave_an_unapplied_delta(tmp_path):
    _graph(tmp_path)
    ledger = _ledger(tmp_path)
    ledger.accept_chapter(1, {}, {}, expected_revision=0)
    delta = _delta(
        2,
        0,
        [{"node_id": "C-CAMERA", "transition": "introduce_to_reader"}],
        marker="stale",
    )
    expected_path = Path(
        ledger.chapter_delta_path(
            2,
            base_revision=0,
            content_hash=delta["content_hash"],
            contract_hash=delta["contract_hash"],
        )
    )
    with pytest.raises(RevisionConflictError):
        ledger.accept_chapter_with_delta(2, {}, {}, delta, 0)
    assert not expected_path.exists()


def test_expected_graph_revision_conflict_is_rejected(tmp_path):
    manager = _graph(tmp_path)
    with pytest.raises(RevisionConflictError):
        manager.apply_change(
            [{"op": "add_node", "node": {"id": "F-NEW", "type": "fact"}}],
            "stale writer",
            0,
        )


def test_related_contract_becomes_stale_after_graph_change(tmp_path):
    graph = _graph(tmp_path)
    ledger = _ledger(tmp_path)
    contract = _contract(
        3,
        1,
        transitions=[{"node_id": "C-CAMERA", "transition": "introduce_to_reader"}],
    )
    ledger.save_contract(3, contract, "### 场景 1\n计划")
    graph.update_node("C-CAMERA", {"surface_meaning": "录像时间跳变"}, 1)
    loaded = ledger.load_contract(3)
    assert loaded["stale"] is True
    assert any(
        issue["code"] == "CONTRACT_GRAPH_REVISION_STALE"
        for issue in loaded["narrative_graph_validation"]
    )


def test_unrelated_graph_change_revalidates_contract_without_staling_it(tmp_path):
    graph = _graph(tmp_path)
    ledger = _ledger(tmp_path)
    ledger.save_contract(
        3,
        _contract(
            3,
            1,
            transitions=[{"node_id": "C-CAMERA", "transition": "introduce_to_reader"}],
        ),
        "### 场景 1\n计划",
    )
    graph.add_node({"id": "F-UNRELATED", "type": "fact"}, 1)
    loaded = ledger.load_contract(3)
    assert loaded["stale"] is False
    assert loaded["narrative_graph_revision"] == 2


def test_node_used_in_accepted_chapter_cannot_be_silently_deprecated(tmp_path):
    graph = _graph(tmp_path)
    ledger = _ledger(tmp_path)
    delta = _delta(
        1,
        0,
        [{"node_id": "C-CAMERA", "transition": "introduce_to_reader"}],
    )
    ledger.accept_chapter_with_delta(1, {}, {}, delta, 0)
    with pytest.raises(NarrativeGraphError) as exc:
        graph.apply_change(
            [{"op": "deprecate_node", "node_id": "C-CAMERA", "reason": "retire"}],
            "retire used clue",
            1,
        )
    assert any(issue["code"] == "HISTORICAL_NARRATIVE_REFERENCE" for issue in exc.value.issues)


def test_change_log_records_reason_actor_and_revisions(tmp_path):
    graph = _graph(tmp_path)
    lines = Path(graph.change_log_path).read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[-1])
    assert record["actor"] == "planner"
    assert record["reason"] == "initial story design"
    assert (record["base_revision"], record["result_revision"]) == (0, 1)


def test_old_story_without_graph_still_loads(tmp_path):
    ledger = _ledger(tmp_path)
    assert ledger.current_revision() == 0
    assert NarrativeGraphManager(str(tmp_path)).load()["revision"] == 0


def test_legacy_dynamic_node_fields_migrate_to_story_ledger(tmp_path):
    ledger = _ledger(tmp_path)
    graph_path = tmp_path / "system" / "story_ledgers" / "narrative_graph.json"
    graph_path.write_text(
        json.dumps(
            {
                "version": 1,
                "revision": 4,
                "nodes": [
                    {
                        "id": "C-OLD",
                        "type": "clue",
                        "reader_status": "introduced",
                        "introduced_chapter": 1,
                    }
                ],
                "edges": [],
                "updated_at": "",
            }
        ),
        encoding="utf-8",
    )
    manager = NarrativeGraphManager(str(tmp_path))
    assert "reader_status" not in manager.load()["nodes"][0]
    state = ledger.load_suspense_ledger()["narrative_node_states"]["C-OLD"]
    assert state["reader_status"] == "introduced"
    assert state["introduced_chapter"] == 1


def test_planning_context_computes_active_starved_ready_and_blocked(tmp_path):
    graph = _graph(tmp_path, second_reveal=True)
    ledger_manager = _ledger(tmp_path)
    ledger = ledger_manager.load_suspense_ledger()
    ledger["accepted_chapters"] = [{"chapter": 1}]
    ledger["narrative_node_states"] = {
        "PT-MAIN": {
            "thread_status": "active",
            "opened_chapter": 1,
            "last_advance_chapter": 1,
        },
        "C-CAMERA": {"reader_status": "introduced", "introduced_chapter": 1},
    }
    context = graph.planning_context(5, ledger)
    assert [item["id"] for item in context["active_threads"]] == ["PT-MAIN"]
    assert [item["id"] for item in context["starved_threads"]] == ["PT-MAIN"]
    assert [item["id"] for item in context["ready_reveals"]] == ["R-BODY-MOVED"]
    assert [item["id"] for item in context["blocked_reveals"]] == ["R-TIME"]


def test_end_to_end_contract_acceptance_delta_ledger_and_next_context(tmp_path):
    graph = _graph(tmp_path)
    ledger = _ledger(tmp_path)
    plan = "### 场景 1\n发现摄像头时间跳变"
    saved = ledger.save_contract(
        1,
        _contract(
            1,
            graph.current_revision(),
            transitions=[
                {
                    "node_id": "C-CAMERA",
                    "transition": "introduce_to_reader",
                    "scene": "scene_1",
                }
            ],
            updates=[
                {
                    "id": "PT-MAIN",
                    "action": "open",
                    "deadline_chapter": 8,
                }
            ],
        ),
        plan,
    )
    chapter_path = tmp_path / "story" / "content" / "chapters" / "chapter_1.md"
    chapter_path.parent.mkdir(parents=True)
    chapter_path.write_text("摄像头的时间戳突然向后跳了七分钟。", encoding="utf-8")
    result = ChapterAcceptanceService(ledger).accept(
        1,
        chapter_path.read_text(encoding="utf-8"),
        saved,
        {
            "average_score": 4,
            "passed": True,
            "unregistered_narrative_elements": [
                {
                    "type": "unregistered_narrative_element",
                    "description": "现场出现一枚蓝色袖扣",
                    "suggested_node_type": "clue",
                }
            ],
        },
        0,
        str(chapter_path),
    )
    next_context = graph.planning_context(2, ledger.load_suspense_ledger())
    assert result.committed_revision == 1
    assert Path(result.delta_path).is_file()
    assert [item["id"] for item in next_context["active_threads"]] == ["PT-MAIN"]
    assert [item["id"] for item in next_context["ready_reveals"]] == ["R-BODY-MOVED"]
    suggestions = ledger.load_suspense_ledger()["pending_narrative_suggestions"]
    assert suggestions[0]["description"] == "现场出现一枚蓝色袖扣"
