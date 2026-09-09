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
            {"id": "T001", "fact": "赵崇德指使伪造公章", "reveal_at_section": "Act 3: Resolution"},
        ],
        "threads_closed": [],
    },
    {
        "section": "Act 2: Confrontation",
        "section_index": 2,
        "total_sections": 3,
        "threads_opened": [],
        "truths_introduced": [
            {"id": "T002", "fact": "专户享有查封豁免权", "reveal_at_section": "Act 2: Confrontation"},
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
