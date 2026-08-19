"""Regression tests for incremental long-form scene planning."""

import json
import logging

from core.gui import scene_plan as scene_plan_module
from core.gui.scene_plan import ScenePlanning
from core.gui.task_runner import snapshot_ui
import pytest

from core.generation.planning_contract import (
    CONTRACT_END,
    CONTRACT_START,
    PlanningContractError,
)
from core.generation.story_ledger import StoryLedgerManager


class FakeApp:
    def __init__(self, output_dir):
        self.output_dir = str(output_dir)
        self.logger = logging.getLogger("incremental-scene-planning-test")

    def get_selected_model(self):
        return "hosted-llm"

    def get_output_dir(self):
        return self.output_dir


def test_existing_scene_plans_are_skipped_and_only_missing_are_generated(monkeypatch, tmp_path):
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "parameters.txt").write_text(
        "Genre: Mystery\n"
        "Subgenre: Legal Thriller\n"
        "Story Length: Novel (Epic)\n"
        "Story Structure: Episodic Structure\n",
        encoding="utf-8",
    )
    lore_dir = tmp_path / "story" / "lore"
    lore_dir.mkdir(parents=True)
    (lore_dir / "generated_lore.md").write_text("故事发生在圣兰卡。", encoding="utf-8")

    outline_dir = tmp_path / "story" / "planning" / "chapter_outlines"
    plan_dir = tmp_path / "story" / "planning" / "detailed_scene_plans"
    outline_dir.mkdir(parents=True)
    plan_dir.mkdir(parents=True)
    (outline_dir / "chapter_outlines_episodic_structure_episode_1_introduction.md").write_text(
        "### 第 1 章：已有章节\n\n### 第 2 章：缺失章节\n",
        encoding="utf-8",
    )

    first_plan = plan_dir / "scenes_episodic_structure_episode_1_introduction_ch1.md"
    original_content = "### 场景 1：原有内容\n不得覆盖。"
    first_plan.write_text(original_content, encoding="utf-8")
    StoryLedgerManager(str(tmp_path)).save_contract(
        1,
        {
            "chapter": 1,
            "origin": "scene_planning",
            "schema_version": 2,
            "facts_added": [],
            "facts_confirmed": [],
            "facts_contradicted": [],
            "timeline_events": [],
            "character_updates": [],
            "plot_thread_updates": [],
        },
        original_content,
    )

    calls = []

    def fake_send_prompt(prompt, model=None):
        calls.append((prompt, model))
        contract = {
            "chapter": 2,
            "facts_added": [],
            "facts_confirmed": [],
            "facts_contradicted": [],
            "timeline_events": [],
            "character_updates": [],
            "plot_thread_updates": [],
        }
        return (
            "### 场景 1：新生成内容\n只应写入第二章。\n"
            + CONTRACT_START
            + "\n"
            + json.dumps(contract, ensure_ascii=False)
            + "\n"
            + CONTRACT_END
        )

    monkeypatch.setattr(scene_plan_module, "send_prompt", fake_send_prompt)
    monkeypatch.setattr(scene_plan_module, "save_prompt_to_file", lambda *args, **kwargs: None)
    monkeypatch.setattr(scene_plan_module, "show_warning", lambda *args, **kwargs: None)
    monkeypatch.setattr(scene_plan_module, "show_success", lambda *args, **kwargs: None)

    planner = object.__new__(ScenePlanning)
    planner.app = FakeApp(tmp_path)
    # 规划现在跑在后台线程上，界面取值由主线程快照传入。
    planner._plan_long_form_scenes(snapshot_ui(planner.app))

    second_plan = plan_dir / "scenes_episodic_structure_episode_1_introduction_ch2.md"
    assert len(calls) == 1
    assert calls[0][1] == "hosted-llm"
    assert "第 2 章" in calls[0][0]
    assert first_plan.read_text(encoding="utf-8") == original_content
    assert "新生成内容" in second_plan.read_text(encoding="utf-8")


def test_empty_or_unparseable_scene_plan_is_not_treated_as_usable(tmp_path):
    path = tmp_path / "scene.md"
    path.write_text("只有章节说明，没有场景标题。", encoding="utf-8")
    assert not ScenePlanning._has_usable_scene_plan(str(path))

    path.write_text("### 场景 1：有效场景\n规划内容", encoding="utf-8")
    assert ScenePlanning._has_usable_scene_plan(str(path))


def test_scene_contract_generation_retries_with_validation_feedback(monkeypatch, tmp_path):
    planner = object.__new__(ScenePlanning)
    planner.app = FakeApp(tmp_path)
    valid_contract = {
        "chapter": 1,
        "facts_added": [],
        "facts_confirmed": [],
        "facts_contradicted": [],
        "timeline_events": [],
        "character_updates": [],
        "plot_thread_updates": [],
    }
    responses = [
        "### 场景 1：缺少契约\n规划",
        "### 场景 1：修复完成\n规划\n"
        + CONTRACT_START
        + "\n"
        + json.dumps(valid_contract, ensure_ascii=False)
        + "\n"
        + CONTRACT_END,
    ]
    prompts = []

    def fake_send(prompt, model=None):
        prompts.append(prompt)
        return responses.pop(0)

    monkeypatch.setattr(scene_plan_module, "send_prompt", fake_send)
    _, markdown, _ = planner._generate_valid_scene_response(
        "原始规划请求", "hosted-llm", 1, "世界观", {}
    )

    assert len(prompts) == 2
    assert "上一次结果未通过前置规划验收" in prompts[1]
    assert "修复完成" in markdown


def test_cross_chapter_contract_failure_repairs_only_deadline_chapter(monkeypatch, tmp_path):
    planner = object.__new__(ScenePlanning)
    planner.app = FakeApp(tmp_path)
    plan_dir = tmp_path / "story" / "planning" / "detailed_scene_plans"
    plan_dir.mkdir(parents=True)
    plans = {
        1: "### 场景 1：提出钥匙疑点\n规划",
        2: "### 场景 1：截止章\n规划",
    }
    manager = StoryLedgerManager(str(tmp_path))
    for chapter, markdown in plans.items():
        (plan_dir / f"scenes_test_ch{chapter}.md").write_text(markdown, encoding="utf-8")
        threads = []
        if chapter == 1:
            threads = [
                {
                    "id": "PT-001-01",
                    "thread": "失踪钥匙",
                    "status": "open",
                    "deadline_chapter": 2,
                }
            ]
        manager.save_contract(
            chapter,
            {
                "chapter": chapter,
                "origin": "scene_planning",
                "schema_version": 2,
                "facts_added": [],
                "facts_confirmed": [],
                "facts_contradicted": [],
                "timeline_events": [],
                "character_updates": [],
                "plot_thread_updates": threads,
            },
            markdown,
        )

    repaired_contract = {
        "chapter": 2,
        "facts_added": [],
        "facts_confirmed": [],
        "facts_contradicted": [],
        "timeline_events": [],
        "character_updates": [],
        "plot_thread_updates": [
            {"id": "PT-001-01", "thread": "失踪钥匙", "status": "closed"}
        ],
    }
    calls = []

    def fake_send(prompt, model=None):
        calls.append(prompt)
        return (
            plans[2]
            + "\n"
            + CONTRACT_START
            + "\n"
            + json.dumps(repaired_contract, ensure_ascii=False)
            + "\n"
            + CONTRACT_END
        )

    monkeypatch.setattr(scene_plan_module, "send_prompt", fake_send)
    planner._validate_sequence_with_retries(
        str(tmp_path), 2, "hosted-llm", "世界观", {}
    )

    assert len(calls) == 1
    assert manager.load_contract(2, plans[2])["plot_thread_updates"][0]["status"] == "closed"


def test_repeated_open_is_repaired_without_rewriting_markdown_or_calling_model(
    monkeypatch, tmp_path
):
    planner = object.__new__(ScenePlanning)
    planner.app = FakeApp(tmp_path)
    plan_dir = tmp_path / "story" / "planning" / "detailed_scene_plans"
    plan_dir.mkdir(parents=True)
    manager = StoryLedgerManager(str(tmp_path))
    markdown_by_chapter = {
        1: "### 场景 1：钥匙失踪\n原始内容一",
        2: "### 场景 1：继续调查\n原始内容二",
        3: "### 场景 1：找到钥匙\n原始内容三",
    }
    updates = {
        1: [{"id": "PT-001-01", "thread": "失踪钥匙", "status": "open", "deadline_chapter": 3}],
        2: [{"id": "PT-001-01", "thread": "失踪钥匙", "status": "open", "deadline_chapter": 3}],
        3: [{"id": "PT-001-01", "thread": "找到钥匙", "status": "closed"}],
    }
    for chapter in (1, 2, 3):
        path = plan_dir / f"scenes_test_ch{chapter}.md"
        path.write_text(markdown_by_chapter[chapter], encoding="utf-8")
        manager.save_contract(
            chapter,
            {
                **json.loads(json.dumps({
                    "chapter": chapter,
                    "origin": "scene_planning",
                    "schema_version": 2,
                    "facts_added": [], "facts_confirmed": [],
                    "facts_contradicted": [], "timeline_events": [],
                    "character_updates": [],
                })),
                "plot_thread_updates": updates[chapter],
            },
            markdown_by_chapter[chapter],
        )

    monkeypatch.setattr(
        scene_plan_module,
        "send_prompt",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("结构性重复开启不应调用模型")
        ),
    )
    planner._validate_sequence_with_retries(
        str(tmp_path), 3, "hosted-llm", "世界观", {}
    )

    assert manager.load_contract(2, markdown_by_chapter[2])["plot_thread_updates"] == []
    assert (plan_dir / "scenes_test_ch2.md").read_text(encoding="utf-8") == markdown_by_chapter[2]


def _plan_project(tmp_path, updates):
    """Write one scene plan plus contract per chapter and return the manager."""
    plan_dir = tmp_path / "story" / "planning" / "detailed_scene_plans"
    plan_dir.mkdir(parents=True, exist_ok=True)
    manager = StoryLedgerManager(str(tmp_path))
    for chapter, threads in updates.items():
        markdown = f"### 场景 1：第{chapter}章\n规划正文"
        (plan_dir / f"scenes_test_ch{chapter}.md").write_text(markdown, encoding="utf-8")
        manager.save_contract(
            chapter,
            {
                "chapter": chapter,
                "origin": "scene_planning",
                "schema_version": 2,
                "facts_added": [],
                "facts_confirmed": [],
                "facts_contradicted": [],
                "timeline_events": [],
                "character_updates": [],
                "plot_thread_updates": threads,
            },
            markdown,
        )
    return manager


def test_dangling_close_repairs_the_chapter_that_should_open_the_thread(
    monkeypatch, tmp_path
):
    planner = object.__new__(ScenePlanning)
    planner.app = FakeApp(tmp_path)
    manager = _plan_project(
        tmp_path,
        {
            5: [
                {
                    "id": "PT-005-02",
                    "thread": "证物袋编号对不上",
                    "status": "open",
                    "deadline_chapter": 8,
                }
            ],
            8: [
                {"id": "PT-005-01", "thread": "被调换的证物袋", "status": "closed"},
                {"id": "PT-005-02", "thread": "证物袋编号对不上", "status": "closed"},
            ],
        },
    )
    repaired = {
        "chapter": 5,
        "facts_added": [],
        "facts_confirmed": [],
        "facts_contradicted": [],
        "timeline_events": [],
        "character_updates": [],
        "plot_thread_updates": [
            {
                "id": "PT-005-01",
                "thread": "被调换的证物袋",
                "status": "open",
                "deadline_chapter": 8,
            },
            {
                "id": "PT-005-02",
                "thread": "证物袋编号对不上",
                "status": "open",
                "deadline_chapter": 8,
            },
        ],
    }
    prompts = []

    def fake_send(prompt, model=None):
        prompts.append(prompt)
        return (
            "### 场景 1：第5章\n修好的规划"
            + "\n"
            + CONTRACT_START
            + "\n"
            + json.dumps(repaired, ensure_ascii=False)
            + "\n"
            + CONTRACT_END
        )

    monkeypatch.setattr(scene_plan_module, "send_prompt", fake_send)
    planner._validate_sequence_with_retries(
        str(tmp_path), None, "hosted-llm", "世界观", {}
    )

    assert "请修复第 5 章场景规划" in prompts[0]
    assert manager.load_contract(8, "")["plot_thread_updates"][0]["id"] == "PT-005-01"


def test_sequence_repair_stops_when_a_failure_survives_its_own_repair(
    monkeypatch, tmp_path
):
    planner = object.__new__(ScenePlanning)
    planner.app = FakeApp(tmp_path)
    _plan_project(
        tmp_path,
        {
            1: [
                {
                    "id": "PT-001-01",
                    "thread": "失踪钥匙",
                    "status": "open",
                    "deadline_chapter": 2,
                }
            ],
            2: [],
        },
    )
    unchanged = {
        "chapter": 2,
        "facts_added": [],
        "facts_confirmed": [],
        "facts_contradicted": [],
        "timeline_events": [],
        "character_updates": [],
        "plot_thread_updates": [],
    }
    calls = []

    def fake_send(prompt, model=None):
        calls.append(prompt)
        return (
            "### 场景 1：第2章\n没有修好"
            + "\n"
            + CONTRACT_START
            + "\n"
            + json.dumps(unchanged, ensure_ascii=False)
            + "\n"
            + CONTRACT_END
        )

    monkeypatch.setattr(scene_plan_module, "send_prompt", fake_send)
    with pytest.raises(PlanningContractError) as excinfo:
        planner._validate_sequence_with_retries(
            str(tmp_path), 2, "hosted-llm", "世界观", {}
        )

    assert excinfo.value.code == "thread_missing_closure"
    # One repair round, then the identical failure proves the loop cannot help.
    assert len(calls) == 1
