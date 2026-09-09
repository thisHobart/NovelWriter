"""叙事图必须由结构契约确定性地播种，不能一直是空的。

生产代码此前从不往图里加节点（只有 tools/run_e2e_10_chapters.py 会），于是新项目
的图永远为空。而契约校验按「有没有引用图节点」分两档：一旦引用了任何节点，
primary_thread / primary_action / via_node_ids 全部变成必填且必须指向真实节点——
空图上没有一条能满足。实测新建长篇的场景规划因此必然失败。
"""
import json

import pytest

from core.generation.narrative_graph import NarrativeGraphManager
from core.generation.stage_pipeline import _section_last_chapters, seed_narrative_graph
from core.generation.story_ledger import StoryLedgerManager


SECTIONS = [
    {
        "section": "Act 1: Setup",
        "section_index": 1,
        "total_sections": 3,
        "threads_opened": [
            {"id": "PT001", "thread": "工资专户能否解冻", "must_close_by_section": 3},
            {"id": "PT002", "thread": "伪造公章能否查实", "must_close_by_section": 2},
        ],
        "truths_introduced": [
            {"id": "T001", "fact": "赵崇德指使伪造公章", "reveal_at_section": 2, "depends_on": []},
        ],
        "threads_closed": [],
    },
    {
        "section": "Act 2: Confrontation",
        "section_index": 2,
        "total_sections": 3,
        "threads_opened": [],
        "truths_introduced": [
            {"id": "T002", "fact": "专户享有查封豁免权", "reveal_at_section": 3, "depends_on": ["T001"]},
        ],
        "threads_closed": ["PT002"],
    },
]


def _write_outlines(root, per_section):
    directory = root / "story" / "planning" / "chapter_outlines"
    directory.mkdir(parents=True, exist_ok=True)
    for index, chapters in enumerate(per_section, 1):
        body = "\n".join(f"### 第 {number} 章\n内容。" for number in chapters)
        (directory / f"chapter_outlines_act_{index}.md").write_text(body, encoding="utf-8")


def test_threads_and_truths_become_graph_nodes(tmp_path):
    manager = NarrativeGraphManager(str(tmp_path))
    result = manager.seed_from_structure(SECTIONS, {1: 7, 2: 14, 3: 20})

    assert result["added"] == 4
    nodes = {node["id"]: node for node in manager.load()["nodes"]}
    assert nodes["PT001"]["type"] == "thread"
    assert nodes["PT001"]["label"] == "工资专户能否解冻"
    assert nodes["PT001"]["planned_resolve_chapter"] == 20
    assert nodes["PT002"]["planned_resolve_chapter"] == 14
    # 真相是 fact，不是 reveal：reveal 节点要求有 clue/fact 前置，而结构阶段声明
    # 的是故事世界里成立的事实本身。
    assert nodes["T001"]["type"] == "fact"
    assert nodes["T002"]["label"] == "专户享有查封豁免权"


def test_seeding_twice_adds_nothing(tmp_path):
    manager = NarrativeGraphManager(str(tmp_path))
    first = manager.seed_from_structure(SECTIONS, {1: 7})
    second = manager.seed_from_structure(SECTIONS, {1: 7})

    assert first["added"] == 4
    assert second["added"] == 0
    assert second["revision"] == first["revision"]


def test_planning_context_is_no_longer_empty(tmp_path):
    """这才是播种的目的：模型能从真实存在的节点里选，而不是自己编一个 id。"""
    manager = NarrativeGraphManager(str(tmp_path))
    manager.seed_from_structure(SECTIONS, {1: 7, 2: 14, 3: 20})
    ledger = StoryLedgerManager(str(tmp_path))
    ledger.initialize({})

    context = manager.planning_context(3, ledger.load_suspense_ledger())
    assert len(context["available_threads"]) == 2
    assert len(context["available_facts"]) == 2
    assert context["graph_revision"] >= 1


def test_section_last_chapters_are_read_from_the_outlines(tmp_path):
    _write_outlines(tmp_path, [[1, 2, 3], [4, 5], [6, 7, 8]])
    assert _section_last_chapters(str(tmp_path)) == {1: 3, 2: 5, 3: 8}


def test_missing_outlines_are_tolerated(tmp_path):
    assert _section_last_chapters(str(tmp_path)) == {}


def test_a_project_without_a_structure_contract_is_left_alone(tmp_path):
    """短篇没有 structure_contract.json，图保持为空是正确的。"""
    result = seed_narrative_graph(str(tmp_path))
    assert result["added"] == 0


def test_seeding_never_blocks_scene_planning(tmp_path, monkeypatch):
    """播种是加分项，失败只该记日志，不该让整个场景规划停下来。"""
    ledger_dir = tmp_path / "story" / "structure"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "structure_contract.json").write_text(
        json.dumps({"sections": SECTIONS}, ensure_ascii=False), encoding="utf-8"
    )

    def explode(*args, **kwargs):
        raise RuntimeError("图坏了")

    monkeypatch.setattr(NarrativeGraphManager, "seed_from_structure", explode)
    result = seed_narrative_graph(str(tmp_path))
    assert result["added"] == 0
    assert "图坏了" in result["error"]


def test_a_real_structure_contract_seeds_both_kinds(tmp_path):
    ledger_dir = tmp_path / "story" / "structure"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "structure_contract.json").write_text(
        json.dumps({"sections": SECTIONS}, ensure_ascii=False), encoding="utf-8"
    )
    _write_outlines(tmp_path, [[1, 2], [3, 4], [5, 6]])

    result = seed_narrative_graph(str(tmp_path))

    assert result["added"] == 4
    types = {node["type"] for node in NarrativeGraphManager(str(tmp_path)).load()["nodes"]}
    assert types == {"thread", "fact"}


# --- 依赖变成 requires 边 ---------------------------------------------------


def test_depends_on_becomes_a_requires_edge(tmp_path):
    """图上此前一条边都没有，requires 查环与「揭示缺前置」两条检查从未真正跑过。"""
    manager = NarrativeGraphManager(str(tmp_path))
    result = manager.seed_from_structure(SECTIONS, {1: 7, 2: 14, 3: 20})

    assert result["edges"] == 1
    edges = manager.load()["edges"]
    assert [(e["type"], e["source_id"], e["target_id"]) for e in edges] == [
        ("requires", "T001", "T002")
    ]
    nodes = {node["id"]: node for node in manager.load()["nodes"]}
    assert nodes["T001"]["planned_reveal_chapter"] == 14
    assert nodes["T002"]["planned_reveal_chapter"] == 20


def test_seeding_edges_twice_adds_nothing(tmp_path):
    manager = NarrativeGraphManager(str(tmp_path))
    first = manager.seed_from_structure(SECTIONS, {1: 7, 2: 14, 3: 20})
    second = manager.seed_from_structure(SECTIONS, {1: 7, 2: 14, 3: 20})

    assert (first["added"], first["edges"]) == (4, 1)
    assert (second["added"], second["edges"]) == (0, 0)


def test_a_dependency_scheduled_after_its_dependent_is_reported(tmp_path):
    """图上完全无环，但第 14 章那一刻 T002 的前置一定不满足。"""
    sections = json.loads(json.dumps(SECTIONS))
    sections[0]["truths_introduced"][0]["reveal_at_section"] = 3
    sections[1]["truths_introduced"][0]["reveal_at_section"] = 2

    manager = NarrativeGraphManager(str(tmp_path))
    manager.seed_from_structure(sections, {1: 7, 2: 14, 3: 20})

    issues = manager.validate()
    reported = [
        issue for issue in issues
        if issue["code"] == "DEPENDENCY_SCHEDULED_AFTER_DEPENDENT"
    ]
    assert len(reported) == 1
    assert reported[0]["severity"] == "warning"
    assert reported[0]["details"]["source_planned_chapter"] == 20
    assert reported[0]["details"]["target_planned_chapter"] == 14


def test_a_consistent_schedule_reports_nothing(tmp_path):
    manager = NarrativeGraphManager(str(tmp_path))
    manager.seed_from_structure(SECTIONS, {1: 7, 2: 14, 3: 20})

    codes = {issue["code"] for issue in manager.validate()}
    assert "DEPENDENCY_SCHEDULED_AFTER_DEPENDENT" not in codes
    assert "REQUIRES_CYCLE" not in codes


def test_a_dependency_on_a_truth_no_section_declared_is_skipped(tmp_path):
    """契约那层已经拦过，播种再筛一次：一条建不出来的边不该拖垮整次播种。"""
    sections = json.loads(json.dumps(SECTIONS))
    sections[1]["truths_introduced"][0]["depends_on"] = ["T001", "T404"]

    manager = NarrativeGraphManager(str(tmp_path))
    result = manager.seed_from_structure(sections, {1: 7, 2: 14, 3: 20})

    assert result["added"] == 4
    assert result["edges"] == 1
