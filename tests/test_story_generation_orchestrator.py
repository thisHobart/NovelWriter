"""Regression tests for sequential GUI operations in the complete workflow."""

import logging
import os
import threading
import time
from types import SimpleNamespace

import pytest

from agents.orchestration.story_generation_orchestrator import StoryGenerationOrchestrator
from agents.orchestration.story_generation_orchestrator import StoryGenerationPlan
from agents.orchestration.checkpoint_state import (
    CheckpointStateManager,
    CheckpointStatus,
)


class FakeTask:
    def __init__(self, *, value=None, error=None):
        self.done = threading.Event()
        self.value = value
        self.error = error

    def result(self):
        if self.error is not None:
            raise self.error
        return self.value


class CompletingRoot:
    def __init__(self, task):
        self.task = task
        self.updates = 0

    def update(self):
        self.updates += 1
        if self.updates == 2:
            self.task.done.set()


def _orchestrator_with_root(root):
    orchestrator = object.__new__(StoryGenerationOrchestrator)
    orchestrator.app_instance = SimpleNamespace(root=root)
    orchestrator.logger = logging.getLogger("workflow-wait-test")
    return orchestrator


def test_gui_operation_waits_for_completion_before_returning():
    task = FakeTask(value="已生成")
    root = CompletingRoot(task)
    orchestrator = _orchestrator_with_root(root)

    result = orchestrator._run_gui_operation(lambda: task, "生成章节大纲")

    assert result == "已生成"
    assert root.updates >= 3  # 两次等待轮询，加一次完成后的回调处理。


def test_gui_operation_propagates_worker_failure():
    task = FakeTask(error=RuntimeError("LLM 调用失败"))
    task.done.set()
    orchestrator = _orchestrator_with_root(CompletingRoot(task))

    with pytest.raises(RuntimeError, match="LLM 调用失败"):
        orchestrator._run_gui_operation(lambda: task, "规划场景")


def test_gui_operation_requires_a_background_task_handle():
    task = FakeTask()
    orchestrator = _orchestrator_with_root(CompletingRoot(task))

    with pytest.raises(RuntimeError, match="未能启动后台任务"):
        orchestrator._run_gui_operation(lambda: None, "生成世界观")


def test_required_output_must_be_created_by_the_current_operation(tmp_path):
    output = tmp_path / "story" / "lore" / "generated_lore.md"
    output.parent.mkdir(parents=True)
    output.write_text("旧的世界观", encoding="utf-8")
    old_ns = time.time_ns() - 10_000_000_000
    os.utime(output, ns=(old_ns, old_ns))

    orchestrator = object.__new__(StoryGenerationOrchestrator)
    orchestrator._last_operation_started_ns = time.time_ns()

    with pytest.raises(RuntimeError, match="没有生成有效输出"):
        orchestrator._require_outputs(
            str(tmp_path),
            "生成世界观",
            relative_paths=("story/lore/generated_lore.md",),
        )

    output.write_text("本轮新生成的世界观", encoding="utf-8")
    os.utime(output, ns=(time.time_ns(), time.time_ns()))
    orchestrator._require_outputs(
        str(tmp_path),
        "生成世界观",
        relative_paths=("story/lore/generated_lore.md",),
    )


def _stateful_orchestrator(tmp_path):
    orchestrator = object.__new__(StoryGenerationOrchestrator)
    orchestrator.output_dir = str(tmp_path)
    orchestrator.logger = logging.getLogger("workflow-progress-test")
    orchestrator.workflow_steps = ["lore", "structure", "scenes", "chapters"]
    orchestrator.step_dependencies = {
        "structure": ["lore"],
        "scenes": ["lore", "structure"],
        "chapters": ["lore", "structure", "scenes"],
    }
    orchestrator.state_manager = CheckpointStateManager(str(tmp_path))
    orchestrator.workflow_state = None
    orchestrator.checkpoint_mode_enabled = False
    orchestrator.current_checkpoint = None
    orchestrator.progress_callback = None
    return orchestrator


def test_complete_workflow_persists_each_step_progress_and_file_count(tmp_path):
    orchestrator = _stateful_orchestrator(tmp_path)
    output_files = {
        "lore": tmp_path / "story" / "lore" / "generated_lore.md",
        "structure": tmp_path / "story" / "structure" / "act_1.md",
        "scenes": (
            tmp_path / "story" / "planning" / "detailed_scene_plans" / "scenes_test.md"
        ),
        "chapters": tmp_path / "story" / "content" / "chapters" / "chapter_1.md",
    }

    def generate(step, parameters, generated):
        path = output_files[step]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{step} 输出", encoding="utf-8")
        return {"success": True, "content": f"{step} 内容"}

    events = []
    orchestrator._generate_workflow_step = generate
    orchestrator._validate_workflow_step = lambda *args: {
        "quality_score": 0.9,
        "recommendations": [],
        "needs_improvement": False,
    }
    orchestrator.progress_callback = lambda step, status: events.append(
        (step, status)
    )
    plan = StoryGenerationPlan(
        workflow_steps=orchestrator.workflow_steps,
        current_step="lore",
        parameters={},
        quality_standards={},
        use_agentic_validation=True,
    )

    result = orchestrator._execute_generation_workflow(plan)
    saved = orchestrator.state_manager.load_state()

    assert result.success
    assert all(
        saved.steps[step].status == CheckpointStatus.COMPLETED
        for step in orchestrator.workflow_steps
    )
    assert all(saved.steps[step].output_files for step in orchestrator.workflow_steps)
    assert events == [
        event
        for step in orchestrator.workflow_steps
        for event in (
            (step, CheckpointStatus.IN_PROGRESS),
            (step, CheckpointStatus.COMPLETED),
        )
    ]


def test_complete_workflow_persists_failed_status_after_bounded_retries(tmp_path):
    orchestrator = _stateful_orchestrator(tmp_path)
    orchestrator._generate_workflow_step = lambda *args: {
        "success": False,
        "error": "模型超时",
    }
    orchestrator._validate_workflow_step = lambda *args: pytest.fail(
        "失败的生成不应进入验收"
    )
    plan = StoryGenerationPlan(
        workflow_steps=orchestrator.workflow_steps,
        current_step="lore",
        parameters={},
        quality_standards={},
        use_agentic_validation=True,
    )

    result = orchestrator._execute_generation_workflow(plan)
    saved = orchestrator.state_manager.load_state()

    assert not result.success
    assert saved.steps["lore"].status == CheckpointStatus.FAILED
    assert saved.steps["lore"].retry_count == 3
    assert saved.steps["lore"].error_message == "模型超时"
    assert saved.steps["structure"].status == CheckpointStatus.NOT_STARTED
