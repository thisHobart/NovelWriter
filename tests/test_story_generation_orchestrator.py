"""Regression tests for sequential GUI operations in the complete workflow."""

import logging
import os
import threading
import time
from types import SimpleNamespace

import pytest

from agents.orchestration.story_generation_orchestrator import StoryGenerationOrchestrator


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
