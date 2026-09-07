# -*- coding: utf-8 -*-
"""把无界面阶段管线包装成 TaskRunner 能跑的 work 函数。

生成逻辑直接在 Qt 后台线程中运行，进度通过 report() 返回主线程；本模块不导入
任何其他 GUI 框架，也不再启动兼容子进程。

页面只需要：
    task = runner.run(step_work("lore", output_dir, model),
                      pass_progress=True, busy_button=btn, busy_text="正在生成…")
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from core.generation.stage_pipeline import (
    StageGenerationError,
    run_stage,
    run_stage_action,
)

STEP_LABELS = {
    "lore": "世界设定",
    "structure": "故事结构",
    "scenes": "场景规划",
    "chapters": "章节撰写",
}


StepFailed = StageGenerationError


def step_work(step: str, output_dir: str, model: str = "") -> Callable[[Callable], Dict[str, Any]]:
    """返回一个 `work(report)`，交给 TaskRunner.run(..., pass_progress=True)。"""

    def work(report: Callable[[str, float], None]) -> Dict[str, Any]:
        label = STEP_LABELS.get(step, step)
        report(f"正在启动{label}…", 0.01)
        result = run_stage(step, output_dir, model, report=report)
        report(f"{label}已完成", 1.0)
        return result

    return work


def action_work(
    step: str,
    action: str,
    output_dir: str,
    model: str = "",
    *,
    parameters: Optional[Dict[str, Any]] = None,
    chapter_number: Optional[int] = None,
    options: Optional[Dict[str, Any]] = None,
) -> Callable[[Callable], Dict[str, Any]]:
    """Return a worker for one fine-grained action exposed by a stage page.

    `options` 走复审动作：重修带上作者勾选的条目，放行带上理由。
    """

    def work(report: Callable[[str, float], None]) -> Dict[str, Any]:
        label = STEP_LABELS.get(step, step)
        report(f"正在启动{label}…", 0.01)
        result = run_stage_action(
            step,
            action,
            output_dir,
            model,
            parameters=parameters,
            report=report,
            chapter_number=chapter_number,
            options=options,
        )
        report(f"{label}已完成", 1.0)
        return result

    return work


ORDERED_STEPS = ["lore", "structure", "scenes", "chapters"]


def full_workflow_work(output_dir: str, model: str = "",
                       steps: Optional[list] = None) -> Callable[[Callable], Dict[str, Any]]:
    """依次跑完四个阶段，进度按阶段等分。

    逐阶段跑，而不是一次性交给一个总编排：任一阶段失败时前面的产物都留在磁盘上，
    状态栏的进度也落在真实的阶段边界上。曾经那个 StoryGenerationOrchestrator 就是
    因此没有被界面接上，已随本次清理删除。
    """
    plan = steps or ORDERED_STEPS

    def work(report: Callable[[str, float], None]) -> Dict[str, Any]:
        results: Dict[str, Any] = {}
        span = 1.0 / len(plan)
        for index, step in enumerate(plan):
            base = index * span
            label = STEP_LABELS.get(step, step)

            def scoped(text: str, fraction: float, _base=base, _label=label) -> None:
                if fraction < 0:
                    report(f"{_label} · {text}", -1.0)
                else:
                    report(f"{_label} · {text}", _base + fraction * span)

            results[step] = step_work(step, output_dir, model)(scoped)
        report("完整流程已完成", 1.0)
        return results

    return work
