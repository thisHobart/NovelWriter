"""Regression tests for incremental long-form scene planning."""

import json
import logging
import re

from core.generation import scene_pipeline as scene_plan_module
from core.generation.scene_pipeline import ScenePipeline as ScenePlanning
from core.generation.stage_context import context_from_host as snapshot_ui
import pytest

from core.generation.planning_contract import (
    CONTRACT_END,
    CONTRACT_START,
    PlanningContractError,
)
from core.generation.helper_fns import parse_chapter_numbers
from core.generation.story_ledger import StoryLedgerManager
from agents.writing.chapter_writing_agent import ChapterWritingAgent


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


def test_fact_conflict_feedback_survives_inside_the_targeted_retry(monkeypatch, tmp_path):
    planner = object.__new__(ScenePlanning)
    planner.app = FakeApp(tmp_path)
    plan_dir = tmp_path / "story" / "planning" / "detailed_scene_plans"
    plan_dir.mkdir(parents=True)
    manager = StoryLedgerManager(str(tmp_path))
    canonical_value = "用于转移NB-4神经制剂与活体受试者的绝密温控舱"
    conflicting_value = "用于向公海转运实验样本的深冷集装箱"
    retry_value = "又一种仍然不一致的自然语言改写"

    for chapter, value in ((14, canonical_value), (17, conflicting_value)):
        markdown = f"### 场景 1：第{chapter}章\n规划正文"
        (plan_dir / f"scenes_test_ch{chapter}.md").write_text(markdown, encoding="utf-8")
        manager.save_contract(
            chapter,
            {
                "chapter": chapter,
                "origin": "scene_planning",
                "schema_version": 2,
                "facts_added": [{
                    "id": "F-014-01",
                    "fact": "海陵先驱号HL-0941集装箱用途",
                    "value": value,
                }],
                "facts_confirmed": [],
                "facts_contradicted": [],
                "timeline_events": [],
                "character_updates": [],
                "plot_thread_updates": [],
            },
            markdown,
        )

    bad_retry = {
        "chapter": 17,
        "facts_added": [{
            "id": "F-014-01",
            "fact": "海陵先驱号HL-0941集装箱用途",
            "value": retry_value,
        }],
        "facts_confirmed": [],
        "facts_contradicted": [],
        "timeline_events": [],
        "character_updates": [],
        "plot_thread_updates": [],
    }
    good_retry = {
        "chapter": 17,
        "facts_added": [],
        "facts_confirmed": [{
            "id": "F-014-01",
            "fact": "海陵先驱号HL-0941集装箱用途",
            "value": canonical_value,
        }],
        "facts_contradicted": [],
        "timeline_events": [],
        "character_updates": [],
        "plot_thread_updates": [],
    }
    prompts = []

    def fake_send(prompt, model=None):
        prompts.append(prompt)
        payload = bad_retry if len(prompts) == 1 else good_retry
        return (
            "### 场景 1：第17章\n修订规划\n"
            + CONTRACT_START + "\n"
            + json.dumps(payload, ensure_ascii=False) + "\n"
            + CONTRACT_END
        )

    monkeypatch.setattr(scene_plan_module, "send_prompt", fake_send)
    planner._validate_sequence_with_retries(
        str(tmp_path), None, "hosted-llm", "世界观", {}
    )

    assert len(prompts) == 2
    assert canonical_value in prompts[0]
    assert conflicting_value in prompts[0]
    assert canonical_value in prompts[1]
    assert retry_value in prompts[1]
    repaired = manager.load_contract(17, "")
    assert repaired["facts_added"] == []
    assert repaired["facts_confirmed"][0]["value"] == canonical_value


def test_existing_domain_id_collision_is_normalised_without_rewriting_plan(
    monkeypatch, tmp_path
):
    planner = object.__new__(ScenePlanning)
    planner.app = FakeApp(tmp_path)
    manager = StoryLedgerManager(str(tmp_path))
    first_markdown = "### 场景 1：第一章\n缝合结线索"
    second_markdown = "### 场景 1：第二章\n喷溅血迹线索"
    first = {
        **_bare(1),
        "origin": "scene_planning",
        "schema_version": 2,
        "fair_play_clues": [{
            "id": "C001",
            "surface_meaning": "喉部的特殊缝合结",
            "true_meaning": "凶手模仿旧案手法",
        }],
    }
    second = {
        **_bare(2),
        "origin": "scene_planning",
        "schema_version": 2,
        "fair_play_clues": [{
            "id": "C001",
            "surface_meaning": "手背上的喷溅血迹",
            "true_meaning": "血迹由设备喷涂伪造",
        }],
    }
    manager.save_contract(1, first, first_markdown)
    manager.save_contract(2, second, second_markdown)

    monkeypatch.setattr(
        scene_plan_module,
        "send_prompt",
        lambda *args, **kwargs: json.dumps(
            {
                "relation": "different",
                "similarity": 0.1,
                "matched_id": None,
                "reason": "两条不同线索",
            },
            ensure_ascii=False,
        ),
    )

    changed = planner._normalise_existing_domain_collisions(
        str(tmp_path),
        2,
        second_markdown,
        "hosted-llm",
        {"Genre": "Mystery", "Subgenre": "Legal Thriller"},
    )

    assert changed is True
    assert manager.load_contract(2, second_markdown)["fair_play_clues"][0]["id"] == "C-002-01"
    # Only the machine-readable sidecar changes; creative planning stays intact.
    assert second_markdown == "### 场景 1：第二章\n喷溅血迹线索"


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

    # The repair round regenerates chapter 2; its replacement still ignores the
    # thread that is due there, which the per-chapter gate now rejects outright
    # rather than letting it drift to the final sequence check.
    assert excinfo.value.code == "thread_overdue"
    # 模型拿到了自己的原稿和问题清单后仍然原样返回，说明它修不动这条：
    # 第二次就停，不再把剩余的重试预算烧掉。
    assert len(calls) == 2
    assert "你的上一稿" in calls[1]


def _workspace(tmp_path, outlines):
    """Build a structured workspace with one chapter-outline file per section."""
    (tmp_path / "system").mkdir(parents=True, exist_ok=True)
    (tmp_path / "system" / "parameters.txt").write_text(
        "Genre: Mystery\nSubgenre: Legal Thriller\n"
        "Story Length: Novel (Epic)\nStory Structure: Episodic Structure\n",
        encoding="utf-8",
    )
    lore_dir = tmp_path / "story" / "lore"
    lore_dir.mkdir(parents=True, exist_ok=True)
    (lore_dir / "generated_lore.md").write_text("故事发生在圣兰卡。", encoding="utf-8")
    outline_dir = tmp_path / "story" / "planning" / "chapter_outlines"
    outline_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "story" / "planning" / "detailed_scene_plans").mkdir(parents=True, exist_ok=True)
    for safe_section, content in outlines.items():
        (outline_dir / f"chapter_outlines_episodic_structure_{safe_section}.md").write_text(
            content, encoding="utf-8"
        )
    return outline_dir


def _stub_planning(monkeypatch, asked):
    """Record the chapter each prompt asks for and answer with a valid plan."""

    def fake_send_prompt(prompt, model=None):
        match = re.search(r"故事第 (\d+) 章规划场景", prompt)
        chapter = int(match.group(1)) if match else -1
        asked.append(chapter)
        contract = {
            "chapter": chapter,
            "facts_added": [],
            "facts_confirmed": [],
            "facts_contradicted": [],
            "timeline_events": [],
            "character_updates": [],
            "plot_thread_updates": [],
        }
        return (
            f"### 场景 1：第{chapter}章内容\n正文。\n"
            + CONTRACT_START
            + "\n"
            + json.dumps(contract, ensure_ascii=False)
            + "\n"
            + CONTRACT_END
        )

    monkeypatch.setattr(scene_plan_module, "send_prompt", fake_send_prompt)
    monkeypatch.setattr(scene_plan_module, "save_prompt_to_file", lambda *a, **k: None)
    monkeypatch.setattr(scene_plan_module, "show_success", lambda *a, **k: None)


def test_scene_planning_uses_the_chapter_numbers_written_in_the_outline(monkeypatch, tmp_path):
    """大纲说第 5、6 章，就必须按第 5、6 章规划——不能按出现顺序改成 3、4。"""
    _workspace(
        tmp_path,
        {
            "episode_1_introduction": "### 第 1 章：开端\n\n### 第 2 章：线索\n",
            # 第二单元的大纲还没生成，中间留了一个洞
            "episode_3_midpoint_turning_point": "### 第 5 章：转折\n\n### 第 6 章：升级\n",
        },
    )
    asked = []
    warnings = []
    _stub_planning(monkeypatch, asked)
    monkeypatch.setattr(
        scene_plan_module, "show_warning", lambda title, message, *a, **k: warnings.append(message)
    )
    monkeypatch.setattr(
        scene_plan_module,
        "show_error",
        lambda title, message, *a, **k: pytest.fail(f"unexpected error: {message}"),
    )

    planner = object.__new__(ScenePlanning)
    planner.app = FakeApp(tmp_path)
    planner._plan_long_form_scenes(snapshot_ui(planner.app))

    assert asked == [1, 2, 5, 6]
    plan_dir = tmp_path / "story" / "planning" / "detailed_scene_plans"
    assert (plan_dir / "scenes_episodic_structure_episode_3_midpoint_turning_point_ch5.md").exists()
    assert (plan_dir / "scenes_episodic_structure_episode_3_midpoint_turning_point_ch6.md").exists()
    # 内容必须落在它自己的章号下，不能出现「第 5 章的规划存成 ch3」这种错位。
    assert "第5章内容" in (
        plan_dir / "scenes_episodic_structure_episode_3_midpoint_turning_point_ch5.md"
    ).read_text(encoding="utf-8")
    # 书里还缺整段的时候必须明说，而不是拿一本不完整的书去跑跨章校验。
    assert any("没有可用的章节大纲" in message for message in warnings)


def test_act_and_part_headings_do_not_shift_chapter_numbers(monkeypatch, tmp_path):
    """“第 N 幕/部分/节”不是章标题，不得计入章数。"""
    _workspace(
        tmp_path,
        {
            "episode_1_introduction": "## 第 1 幕：铺垫\n\n### 第 1 章：开端\n\n### 第 2 章：线索\n",
            "episode_2_rising_action": "### 第 3 章：施压\n\n#### 第 2 部分\n\n### 第 4 章：反制\n",
        },
    )
    asked = []
    _stub_planning(monkeypatch, asked)
    monkeypatch.setattr(scene_plan_module, "show_warning", lambda *a, **k: None)
    monkeypatch.setattr(scene_plan_module, "show_error", lambda *a, **k: None)

    planner = object.__new__(ScenePlanning)
    planner.app = FakeApp(tmp_path)
    planner._plan_long_form_scenes(snapshot_ui(planner.app))

    assert asked == [1, 2, 3, 4]


def test_planning_and_writing_agree_on_chapter_numbers(monkeypatch, tmp_path):
    """规划落盘的文件名，必须正好是章节写作要找的那些。"""
    _workspace(
        tmp_path,
        {
            "episode_1_introduction": "## 第 1 幕\n\n### 第 1 章：开端\n\n### 第 2 章：线索\n",
            "episode_2_rising_action": "### 第 3 章：施压\n\n#### 第 2 部分\n\n### 第 4 章：反制\n",
            "episode_3_midpoint_turning_point": "### 第 5 章：转折\n",
        },
    )
    asked = []
    _stub_planning(monkeypatch, asked)
    monkeypatch.setattr(scene_plan_module, "show_warning", lambda *a, **k: None)
    monkeypatch.setattr(scene_plan_module, "show_error", lambda *a, **k: None)

    planner = object.__new__(ScenePlanning)
    planner.app = FakeApp(tmp_path)
    planner._plan_long_form_scenes(snapshot_ui(planner.app))

    agent = ChapterWritingAgent(str(tmp_path), None, model="hosted-llm")
    infos, _ = agent.analyze_chapter_structure()

    assert [info.chapter_number for info in infos] == [1, 2, 3, 4, 5]
    assert all(info.plan_exists for info in infos), [
        info.chapter_number for info in infos if not info.plan_exists
    ]


# --- 重试反馈闭环 -------------------------------------------------------------
#
# 重试要真正起作用，模型必须拿到三样东西：它自己被拒的原稿、这一稿的**全部**问题、
# 以及此前几轮指出过的问题。少任何一样，重试都只是重新抽一次卡。


def _seed_overdue_project(tmp_path):
    """第 1 章埋下一条第 2 章到期的悬念。"""
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
            "plot_thread_updates": [
                {
                    "id": "PT-001-01",
                    "thread": "证物袋被调换",
                    "status": "open",
                    "deadline_chapter": 2,
                }
            ],
        },
        "### 场景 1：占位\n规划",
    )
    return str(tmp_path)


def _draft(title, contract_payload):
    return (
        f"### 场景 1：{title}\n规划正文。\n"
        + CONTRACT_START
        + "\n"
        + json.dumps(contract_payload, ensure_ascii=False)
        + "\n"
        + CONTRACT_END
    )


def _bare(chapter, **overrides):
    payload = {
        "chapter": chapter,
        "facts_added": [],
        "facts_confirmed": [],
        "facts_contradicted": [],
        "timeline_events": [],
        "character_updates": [],
        "plot_thread_updates": [],
    }
    payload.update(overrides)
    return payload


def test_retry_hands_the_rejected_draft_back_to_the_model(monkeypatch, tmp_path):
    project = _seed_overdue_project(tmp_path)
    planner = object.__new__(ScenePlanning)
    planner.app = FakeApp(tmp_path)

    drafts = [
        _draft("第一稿", _bare(2)),
        _draft(
            "第二稿",
            _bare(
                2,
                plot_thread_updates=[
                    {
                        "id": "PT-001-01",
                        "status": "open",
                        "extend": True,
                        "deadline_chapter": 5,
                    }
                ],
            ),
        ),
    ]
    prompts = []

    def fake_send(prompt, model=None):
        prompts.append(prompt)
        return drafts[min(len(prompts) - 1, len(drafts) - 1)]

    monkeypatch.setattr(scene_plan_module, "send_prompt", fake_send)
    planner._generate_valid_scene_response(
        "请为第 2 章规划场景。", "hosted-llm", 2, "世界观", {}, output_dir=project
    )

    assert len(prompts) == 2
    # 只说「修好它」而不给原稿，模型无从修起，只能整章重抽。
    assert "第一稿" in prompts[1]
    assert "你的上一稿" in prompts[1]
    assert "PT-001-01" in prompts[1]


def test_all_defects_in_one_draft_are_reported_together(monkeypatch, tmp_path):
    project = _seed_overdue_project(tmp_path)
    planner = object.__new__(ScenePlanning)
    planner.app = FakeApp(tmp_path)

    # 一稿同时犯两个独立的错：漏了到期悬念，又引用了不存在的事实。
    broken = _draft(
        "两个毛病",
        _bare(2, facts_confirmed=[{"id": "F-999-01", "fact": "凭空出现的事实"}]),
    )
    prompts = []

    def fake_send(prompt, model=None):
        prompts.append(prompt)
        return broken

    monkeypatch.setattr(scene_plan_module, "send_prompt", fake_send)
    with pytest.raises(PlanningContractError):
        planner._generate_valid_scene_response(
            "请为第 2 章规划场景。", "hosted-llm", 2, "世界观", {}, output_dir=project
        )

    # 两条问题必须在同一次反馈里出现，否则每修一条就要烧掉一次重试。
    assert "PT-001-01" in prompts[1]
    assert "F-999-01" in prompts[1]


def test_feedback_accumulates_so_a_fixed_defect_is_not_reintroduced(monkeypatch, tmp_path):
    project = _seed_overdue_project(tmp_path)
    planner = object.__new__(ScenePlanning)
    planner.app = FakeApp(tmp_path)

    drafts = [
        # 第一稿：事实引用错误
        _draft("第一稿", _bare(2, facts_confirmed=[{"id": "F-999-01", "fact": "不存在"}])),
        # 第二稿：修好了事实，但仍漏掉到期悬念
        _draft("第二稿", _bare(2)),
        _draft("第三稿", _bare(2)),
    ]
    prompts = []

    def fake_send(prompt, model=None):
        prompts.append(prompt)
        return drafts[min(len(prompts) - 1, len(drafts) - 1)]

    monkeypatch.setattr(scene_plan_module, "send_prompt", fake_send)
    with pytest.raises(PlanningContractError):
        planner._generate_valid_scene_response(
            "请为第 2 章规划场景。", "hosted-llm", 2, "世界观", {}, output_dir=project
        )

    # 第三次反馈里必须仍然带着第一次指出的事实问题，
    # 否则模型改完新问题就会把旧问题写回来。
    assert "F-999-01" in prompts[2]
    assert "PT-001-01" in prompts[2]


def test_no_progress_stops_early_instead_of_burning_the_budget(monkeypatch, tmp_path):
    project = _seed_overdue_project(tmp_path)
    planner = object.__new__(ScenePlanning)
    planner.app = FakeApp(tmp_path)
    same = _draft("原样返回", _bare(2))
    prompts = []

    def fake_send(prompt, model=None):
        prompts.append(prompt)
        return same

    monkeypatch.setattr(scene_plan_module, "send_prompt", fake_send)
    with pytest.raises(PlanningContractError):
        planner._generate_valid_scene_response(
            "请为第 2 章规划场景。", "hosted-llm", 2, "世界观", {}, output_dir=project
        )

    # 拿到原稿和问题后仍然一字不改，再试也是白试。
    assert len(prompts) == 2


def test_rejected_drafts_are_archived_for_inspection(monkeypatch, tmp_path):
    project = _seed_overdue_project(tmp_path)
    planner = object.__new__(ScenePlanning)
    planner.app = FakeApp(tmp_path)
    prompts = []

    def fake_send(prompt, model=None):
        prompts.append(prompt)
        return _draft(f"第{len(prompts)}稿", _bare(2))

    monkeypatch.setattr(scene_plan_module, "send_prompt", fake_send)
    with pytest.raises(PlanningContractError):
        planner._generate_valid_scene_response(
            "请为第 2 章规划场景。", "hosted-llm", 2, "世界观", {}, output_dir=project
        )

    archive = tmp_path / "archive" / "planning_retries" / "chapter_2"
    assert (archive / "attempt_1_response.md").exists()
    assert "第1稿" in (archive / "attempt_1_response.md").read_text(encoding="utf-8")
    defects = json.loads((archive / "attempt_1_defects.json").read_text(encoding="utf-8"))
    assert defects and defects[0]["code"] == "thread_overdue"
    # 归档只落在 archive/ 下，不能污染稿件或规划目录。
    assert not (tmp_path / "story" / "planning" / "detailed_scene_plans").exists()


# --- 章节大纲阶段 -------------------------------------------------------------
#
# 这一阶段此前比场景规划粗糙一截：不增量（无条件覆盖全部）、世界观冲突一次就丢掉
# 整段、日志走 print 查不到。一部书的整幕内容就是这样静默消失的。


def _outline_workspace(tmp_path, existing_outlines, structure_sections):
    (tmp_path / "system").mkdir(parents=True, exist_ok=True)
    (tmp_path / "system" / "parameters.txt").write_text(
        "Genre: Mystery\nSubgenre: Legal Thriller\n"
        "Story Length: Novel (Epic)\nStory Structure: Episodic Structure\n",
        encoding="utf-8",
    )
    structure_dir = tmp_path / "story" / "structure"
    structure_dir.mkdir(parents=True, exist_ok=True)
    for safe_section in structure_sections:
        (structure_dir / f"episodic_structure_{safe_section}.md").write_text(
            "本部分的详细规划内容。", encoding="utf-8"
        )
    outline_dir = tmp_path / "story" / "planning" / "chapter_outlines"
    outline_dir.mkdir(parents=True, exist_ok=True)
    for safe_section, content in existing_outlines.items():
        (outline_dir / f"chapter_outlines_episodic_structure_{safe_section}.md").write_text(
            content, encoding="utf-8"
        )
    return outline_dir


_ALL_SECTIONS = (
    "episode_1_introduction",
    "episode_2_rising_action",
    "episode_3_midpoint_turning_point",
    "episode_4_climax_actions",
    "episode_5_resolution_lead_to_next",
)


def _run_outline(planner, monkeypatch, responder, notices):
    monkeypatch.setattr(scene_plan_module, "send_prompt", responder)
    monkeypatch.setattr(scene_plan_module, "save_prompt_to_file", lambda *a, **k: None)
    for name in ("show_warning", "show_success", "show_error"):
        monkeypatch.setattr(
            scene_plan_module,
            name,
            lambda title, message, *a, _n=name, **k: notices.append((_n, title, str(message))),
        )
    planner._generate_chapter_outline(snapshot_ui(planner.app))


def test_existing_outlines_are_kept_and_only_the_missing_one_is_generated(
    monkeypatch, tmp_path
):
    """整批重写会让已有的场景规划对着一份变了的大纲，而它们不会因此失效——错位是静默的。"""
    outline_dir = _outline_workspace(
        tmp_path,
        {
            "episode_1_introduction": "### 第 1 章：开端\n\n### 第 2 章：线索\n",
            # 第二单元的大纲缺失
            "episode_3_midpoint_turning_point": "### 第 5 章：转折\n",
        },
        _ALL_SECTIONS,
    )
    untouched = (
        outline_dir / "chapter_outlines_episodic_structure_episode_1_introduction.md"
    ).read_text(encoding="utf-8")
    asked = []

    def responder(prompt, model=None):
        start = int(re.search(r"从第 (\d+) 章开始", prompt).group(1))
        asked.append(start)
        return f"### 第 {start} 章：新生成\n"

    planner = object.__new__(ScenePlanning)
    planner.app = FakeApp(tmp_path)
    notices = []
    _run_outline(planner, monkeypatch, responder, notices)

    # 只补缺的两段（第二单元和第四、五单元），已有的两段原样保留。
    assert (
        outline_dir / "chapter_outlines_episodic_structure_episode_1_introduction.md"
    ).read_text(encoding="utf-8") == untouched
    assert any("已跳过" in message for _, _, message in notices)
    # 第二单元接在第 2 章之后。
    assert asked[0] == 3


def test_a_world_conflict_retries_instead_of_dropping_the_whole_act(monkeypatch, tmp_path):
    outline_dir = _outline_workspace(
        tmp_path, {}, ("episode_1_introduction",)
    )
    prompts = []
    responses = [
        "### 第 1 章：星际法庭开庭\n梁浩乘悬浮车抵达。",  # 真科幻窜入
        "### 第 1 章：市法院开庭\n梁浩乘车抵达。",
    ]

    def responder(prompt, model=None):
        prompts.append(prompt)
        return responses[min(len(prompts) - 1, len(responses) - 1)]

    planner = object.__new__(ScenePlanning)
    planner.app = FakeApp(tmp_path)
    notices = []
    _run_outline(planner, monkeypatch, responder, notices)

    saved = outline_dir / "chapter_outlines_episodic_structure_episode_1_introduction.md"
    assert saved.exists(), "冲突后必须重试并保存，而不是整段丢弃"
    assert "星际" not in saved.read_text(encoding="utf-8")
    # 重试要带着被拒的原稿，模型才有东西可改。
    assert "你的上一稿" in prompts[1]
    assert "星际" in prompts[1]


def test_an_outline_without_chapter_headings_is_retried(monkeypatch, tmp_path):
    outline_dir = _outline_workspace(tmp_path, {}, ("episode_1_introduction",))
    prompts = []
    responses = ["这一部分讲了很多事，但没有任何章标题。", "### 第 1 章：开端\n正文。"]

    def responder(prompt, model=None):
        prompts.append(prompt)
        return responses[min(len(prompts) - 1, len(responses) - 1)]

    planner = object.__new__(ScenePlanning)
    planner.app = FakeApp(tmp_path)
    _run_outline(planner, monkeypatch, responder, [])

    saved = outline_dir / "chapter_outlines_episodic_structure_episode_1_introduction.md"
    assert parse_chapter_numbers(saved.read_text(encoding="utf-8")) == [1]
    assert "没有可识别的章标题" in prompts[1]


def test_filling_a_gap_reports_that_later_sections_need_renumbering(monkeypatch, tmp_path):
    """补上缺失的一幕会占用后面各段现有的号段，必须点破而不是留下两份第 3 章。"""
    _outline_workspace(
        tmp_path,
        {
            "episode_1_introduction": "### 第 1 章：开端\n\n### 第 2 章：线索\n",
            # 第二单元缺失；第三单元已经占着第 3、4 章
            "episode_3_midpoint_turning_point": "### 第 3 章：转折\n\n### 第 4 章：升级\n",
        },
        _ALL_SECTIONS,
    )

    def responder(prompt, model=None):
        start = int(re.search(r"从第 (\d+) 章开始", prompt).group(1))
        return f"### 第 {start} 章：新生成\n\n### 第 {start + 1} 章：新生成\n"

    planner = object.__new__(ScenePlanning)
    planner.app = FakeApp(tmp_path)
    notices = []
    _run_outline(planner, monkeypatch, responder, notices)

    renumber = [n for n in notices if n[1] == "章号需要重排"]
    assert renumber, [n[1] for n in notices]
    assert "都声称占用同一批章号" in renumber[0][2]
