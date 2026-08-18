"""Regression tests for incremental long-form scene planning."""

import logging

from core.gui import scene_plan as scene_plan_module
from core.gui.scene_plan import ScenePlanning
from core.gui.task_runner import snapshot_ui


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

    calls = []

    def fake_send_prompt(prompt, model=None):
        calls.append((prompt, model))
        return "### 场景 1：新生成内容\n只应写入第二章。"

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
