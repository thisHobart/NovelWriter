"""Integration boundary tests for the framework-independent stage runner."""
from __future__ import annotations

import ast
import logging
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from core.generation import stage_pipeline
from core.generation.errors import GenerationPipelineError
from core.generation import lore_pipeline
from core.generation.lore_pipeline import LorePipeline
from core.generation.scene_pipeline import ScenePipeline
from core.generation.short_story_pipeline import ShortStoryPipeline
from core.generation.stage_context import make_context
from core.generation.structure_pipeline import StructurePipeline
from core.gui.services import step_runner


def test_stage_pipeline_imports_without_tkinter():
    command = [
        sys.executable,
        "-c",
        (
            "import sys; import core.generation.stage_pipeline; "
            "assert 'tkinter' not in sys.modules; "
            "assert 'core.gui' not in sys.modules; "
            "assert not any(name.startswith('PySide6') for name in sys.modules); "
            "print('headless')"
        ),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "headless"


def test_saved_parameters_are_normalized_for_generation(tmp_path):
    system = tmp_path / "system"
    system.mkdir()
    (system / "parameters.txt").write_text(
        "Genre: Mystery\n"
        "Story Length: Novel (Standard)\n"
        "Story Structure: 6-Act Structure\n"
        "Gender Generation Bias String: Mostly Female (75F/25M)\n"
        "Backend: api\nModel: hosted-model\n",
        encoding="utf-8",
    )

    params = stage_pipeline.load_stage_parameters(str(tmp_path))

    assert params["Genre"] == "Mystery"
    assert params["genre"] == "Mystery"
    assert params["story_structure"] == "6-Act Structure"
    assert (params["female_percentage"], params["male_percentage"]) == (75, 25)


def test_all_pipeline_objects_construct_without_a_ui(tmp_path):
    context = make_context(
        str(tmp_path), "test-model", {"genre": "Mystery", "story_length": "Novel"}
    )
    host = stage_pipeline.GenerationHost(context, logging.getLogger("test"))

    pipelines = [
        LorePipeline(host),
        StructurePipeline(host),
        ScenePipeline(host),
        ShortStoryPipeline(host),
    ]

    assert all(pipeline.app is host for pipeline in pipelines)


def test_ui_step_work_calls_headless_runner_and_forwards_progress(monkeypatch, tmp_path):
    calls = []

    def fake_run_stage(step, output_dir, model, parameters=None, report=None):
        calls.append((step, output_dir, model, parameters))
        report("管线进度", 0.5)
        return {"step": step, "generated_files": ["story/lore/generated_lore.md"]}

    monkeypatch.setattr(step_runner, "run_stage", fake_run_stage)
    progress = []
    result = step_runner.step_work("lore", str(tmp_path), "test-model")(
        lambda text, fraction: progress.append((text, fraction))
    )

    assert calls == [("lore", str(tmp_path), "test-model", None)]
    assert result["step"] == "lore"
    assert ("管线进度", 0.5) in progress


def test_ui_action_work_forwards_action_parameters_and_chapter(monkeypatch, tmp_path):
    calls = []

    def fake_run_action(
        step, action, output_dir, model, parameters=None, report=None,
        chapter_number=None, options=None,
    ):
        calls.append(
            (step, action, output_dir, model, parameters, chapter_number, options)
        )
        report("动作进度", 0.4)
        return {"step": step, "action": action}

    monkeypatch.setattr(step_runner, "run_stage_action", fake_run_action)
    progress = []
    result = step_runner.action_work(
        "chapters",
        "rewrite",
        str(tmp_path),
        "test-model",
        parameters={"num_characters": 7},
        chapter_number=3,
        options={"reason": "作者放行"},
    )(lambda text, fraction: progress.append((text, fraction)))

    assert calls == [(
        "chapters", "rewrite", str(tmp_path), "test-model",
        {"num_characters": 7}, 3, {"reason": "作者放行"},
    )]
    assert result == {"step": "chapters", "action": "rewrite"}
    assert ("动作进度", 0.4) in progress


def test_lore_action_merges_saved_parameters_with_ui_counts(monkeypatch, tmp_path):
    system = tmp_path / "system"
    system.mkdir()
    (system / "parameters.txt").write_text(
        "Genre: Mystery\nBackend: api\nModel: test-model\n",
        encoding="utf-8",
    )
    observed = {}

    def fake_generate(self, context):
        observed["genre"] = context.parameters["genre"]
        observed["count"] = context.get("num_factions")
        target = tmp_path / "story/lore/factions.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps([{"name": "测试势力"}]), encoding="utf-8")

    monkeypatch.setattr(stage_pipeline, "set_backend", lambda *_args: None)
    monkeypatch.setattr(LorePipeline, "_generate_factions", fake_generate)

    result = stage_pipeline.run_stage_action(
        "lore",
        "factions",
        str(tmp_path),
        parameters={"num_factions": 3},
    )

    assert observed == {"genre": "Mystery", "count": 3}
    assert result["action"] == "factions"
    assert result["generated_files"] == [os.path.join("story", "lore", "factions.json")]


def _project(tmp_path):
    system = tmp_path / "system"
    system.mkdir(exist_ok=True)
    (system / "parameters.txt").write_text(
        "Genre: Mystery\nBackend: api\nModel: test-model\n", encoding="utf-8"
    )
    return str(tmp_path)


def _pending(tmp_path, *, resumable=True, asks=True, verdict_unavailable=False):
    from core.generation import pending_review

    review = {
        "stage": "chapter",
        "passed": False,
        "pass_average": 3.2,
        "average_score": 2.8,
        "repair_instructions": ["把收据时间写清楚"] if asks else [],
    }
    record = pending_review.build(
        4,
        message="第 4 章仍未通过章节级质量检查",
        review={} if verdict_unavailable else review,
        prose="一场。\n\n二场。",
        stage=(
            pending_review.STAGE_CHAPTER if resumable else pending_review.STAGE_SCENE
        ),
        snapshot={"scenes": ["一场。", "二场。"]} if resumable else {},
        verdict_unavailable=verdict_unavailable,
    )
    pending_review.save(record, str(tmp_path))
    return record


def test_review_actions_need_a_record_to_act_on(monkeypatch, tmp_path):
    monkeypatch.setattr(stage_pipeline, "set_backend", lambda *_args: None)

    with pytest.raises(stage_pipeline.StageGenerationError, match="没有待复审记录"):
        stage_pipeline.run_stage_action(
            "chapters", "waive", _project(tmp_path), chapter_number=4
        )

    with pytest.raises(stage_pipeline.StageGenerationError, match="缺少章节编号"):
        stage_pipeline.run_stage_action("chapters", "revise", _project(tmp_path))


def test_a_half_written_draft_is_sent_back_to_a_rewrite(monkeypatch, tmp_path):
    """只写到一半的稿子没有完整章节，放行和定向重修都无从谈起。"""
    monkeypatch.setattr(stage_pipeline, "set_backend", lambda *_args: None)
    _pending(tmp_path, resumable=False)

    with pytest.raises(stage_pipeline.StageGenerationError, match="请重写本章"):
        stage_pipeline.run_stage_action(
            "chapters", "waive", _project(tmp_path), chapter_number=4
        )


def test_revising_without_a_single_checked_ask_is_refused(monkeypatch, tmp_path):
    """勾选框全被划掉时重修没有依据，比空跑一轮大模型早一步说清楚。"""
    monkeypatch.setattr(stage_pipeline, "set_backend", lambda *_args: None)
    _pending(tmp_path)

    with pytest.raises(stage_pipeline.StageGenerationError, match="没有勾选"):
        stage_pipeline.run_stage_action(
            "chapters",
            "revise",
            _project(tmp_path),
            chapter_number=4,
            options={"issue_ids": []},
        )


def test_a_verdictless_draft_may_be_sent_back_without_any_checked_ask(
    monkeypatch, tmp_path
):
    """评审自己没出过结论时没有清单可勾，这条路是重跑评审，不该被当成空重修拦下。"""
    from agents.writing import chapter_writing_agent as agent_module
    from agents.base.agent import AgentResult

    monkeypatch.setattr(stage_pipeline, "set_backend", lambda *_args: None)
    _pending(tmp_path, verdict_unavailable=True)
    seen = {}

    def fake_review(self, chapter_number, action, asks=None, reason=""):
        seen.update(action=action, asks=list(asks or []))
        target = tmp_path / "story/content/chapters/chapter_4.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("重跑评审后通过的正文", encoding="utf-8")
        return AgentResult(success=True, data={}, messages=["好了"], metrics={})

    monkeypatch.setattr(
        agent_module.ChapterWritingAgent, "review_pending_chapter", fake_review
    )
    monkeypatch.setattr(
        agent_module.ChapterWritingAgent, "__init__",
        lambda self, output_dir, **kwargs: setattr(self, "output_dir", output_dir),
    )

    stage_pipeline.run_stage_action(
        "chapters",
        "revise",
        _project(tmp_path),
        chapter_number=4,
        options={"issue_ids": []},
    )

    assert seen == {"action": "revise", "asks": []}


def test_review_actions_are_handed_to_the_writing_agent(monkeypatch, tmp_path):
    """复审动作必须交给写作 agent：只有它带得起整章重生成要用的上下文。"""
    from agents.writing import chapter_writing_agent as agent_module
    from agents.base.agent import AgentResult

    monkeypatch.setattr(stage_pipeline, "set_backend", lambda *_args: None)
    record = _pending(tmp_path)
    seen = {}

    def fake_review(self, chapter_number, action, asks=None, reason=""):
        seen.update(
            chapter=chapter_number, action=action, asks=list(asks or []), reason=reason
        )
        target = tmp_path / "story/content/chapters/chapter_4.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("放行后的正文", encoding="utf-8")
        return AgentResult(success=True, data={}, messages=["好了"], metrics={})

    monkeypatch.setattr(
        agent_module.ChapterWritingAgent, "review_pending_chapter", fake_review
    )
    monkeypatch.setattr(
        agent_module.ChapterWritingAgent, "__init__",
        lambda self, output_dir, **kwargs: setattr(self, "output_dir", output_dir),
    )

    result = stage_pipeline.run_stage_action(
        "chapters",
        "waive",
        _project(tmp_path),
        chapter_number=4,
        options={"reason": "开篇节奏我认了"},
    )

    assert seen == {
        "chapter": 4, "action": "waive", "asks": [], "reason": "开篇节奏我认了",
    }
    assert result["action"] == "waive"
    assert result["generated_files"]

    stage_pipeline.run_stage_action(
        "chapters",
        "revise",
        _project(tmp_path),
        chapter_number=4,
        options={"issue_ids": [record.issues[0].id]},
    )
    assert seen["action"] == "revise"
    assert seen["asks"] == ["把收据时间写清楚"]


def test_a_failed_review_action_surfaces_the_reason(monkeypatch, tmp_path):
    from agents.writing import chapter_writing_agent as agent_module
    from agents.base.agent import AgentResult

    monkeypatch.setattr(stage_pipeline, "set_backend", lambda *_args: None)
    _pending(tmp_path)
    monkeypatch.setattr(
        agent_module.ChapterWritingAgent, "__init__",
        lambda self, output_dir, **kwargs: setattr(self, "output_dir", output_dir),
    )
    monkeypatch.setattr(
        agent_module.ChapterWritingAgent,
        "review_pending_chapter",
        lambda self, *args, **kwargs: AgentResult(
            success=False, data={}, metrics={},
            messages=["第 4 章重修后仍未通过质量检查；1 处硬伤，仍待复审"],
        ),
    )

    with pytest.raises(stage_pipeline.StageGenerationError, match="仍待复审"):
        stage_pipeline.run_stage_action(
            "chapters", "waive", _project(tmp_path), chapter_number=4
        )


def test_pipeline_error_message_is_not_swallowed_or_rewritten():
    with pytest.raises(GenerationPipelineError) as exc_info:
        lore_pipeline.show_error("世界观生成失败", "模型返回内容为空")

    assert str(exc_info.value) == "世界观生成失败：模型返回内容为空"


def test_stage_entry_methods_have_no_terminal_catch_all_wrapper():
    targets = {
        "core/generation/lore_pipeline.py": {
            "_generate_factions", "_generate_characters", "_add_genre_specific_attributes",
            "_generate_lore", "_suggest_titles", "_main_character_enhancement",
        },
        "core/generation/structure_pipeline.py": {
            "_generate_arcs", "_generate_faction_arcs", "_add_planets_to_arcs",
            "improve_structure", "_outline_short_story_plot",
        },
        "core/generation/scene_pipeline.py": {
            "_generate_chapter_outline", "_plan_long_form_scenes",
            "_plan_short_story_scenes",
        },
        "core/generation/short_story_pipeline.py": {"_write_short_story_prose"},
    }
    for filename, method_names in targets.items():
        tree = ast.parse(Path(filename).read_text(encoding="utf-8"))
        methods = {
            node.name: node
            for class_node in tree.body
            if isinstance(class_node, ast.ClassDef)
            for node in class_node.body
            if isinstance(node, ast.FunctionDef) and node.name in method_names
        }
        assert methods.keys() == method_names
        for method in methods.values():
            terminal = method.body[-1]
            if not isinstance(terminal, ast.Try):
                continue
            broad = [
                handler for handler in terminal.handlers
                if handler.type is None
                or isinstance(handler.type, ast.Name)
                and handler.type.id in {"Exception", "BaseException"}
            ]
            assert not broad, f"{filename}:{method.name} still swallows all exceptions"


def test_full_workflow_runs_all_headless_stages_in_order(monkeypatch, tmp_path):
    seen = []

    def fake_run_stage(step, output_dir, model, parameters=None, report=None):
        seen.append(step)
        report(f"{step}-done", 1.0)
        return {"step": step}

    monkeypatch.setattr(step_runner, "run_stage", fake_run_stage)
    step_runner.full_workflow_work(str(tmp_path))(
        lambda _text, _fraction: None
    )

    assert seen == ["lore", "structure", "scenes", "chapters"]
