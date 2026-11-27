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


class GenerationWork:
    """任务请求先经过主窗口的参数选择，再绑定不可变的输入快照。"""

    def __init__(self, step, output_dir, model="", *, action=None, parameters=None,
                 chapter_number=None, options=None, steps=None):
        from copy import deepcopy
        self.step, self.output_dir, self.model = step, output_dir, model
        self.action = action
        self.parameters = deepcopy(parameters)
        self.chapter_number, self.options = chapter_number, deepcopy(options)
        self.steps = list(steps) if steps is not None else None
        self.lore_action = (action or "full") if step == "lore" else ("full" if step == "workflow" and "lore" in self.steps else None)

    def bind_parameters(self, parameters):
        from core.generation.parameters import freeze_parameters
        return GenerationWork(self.step, self.output_dir, self.model, action=self.action,
                              parameters=freeze_parameters(parameters), chapter_number=self.chapter_number,
                              options=self.options, steps=self.steps)

    def __call__(self, report):
        from core.generation.parameters import ParameterSnapshot, freeze_parameters
        from core.generation.stage_pipeline import load_stage_parameters
        if self.step == "workflow":
            snapshot = self.parameters
            if not isinstance(snapshot, ParameterSnapshot):
                stored = load_stage_parameters(self.output_dir, overrides=snapshot)
                if not stored:
                    raise StageGenerationError("尚未保存作品参数")
                snapshot = freeze_parameters(stored)
            results = {}
            span = 1.0 / len(self.steps)
            for index, step in enumerate(self.steps):
                base = index * span
                label = STEP_LABELS.get(step, step)
                def scoped(text, fraction, _base=base, _label=label):
                    report(f"{_label} · {text}", -1.0 if fraction < 0 else _base + fraction * span)
                results[step] = step_work(step, self.output_dir, self.model, parameters=snapshot)(scoped)
                if results[step].get("status") == "draft" or results[step].get("stopped"):
                    # 世界设定整段生成停在半路（读不出来，或势力、人物、世界观修过仍有问题）：后面阶段不跑。
                    report("世界设定停在半路，处理后可继续后续阶段", 1.0)
                    return results
            report("完整流程已完成", 1.0)
            return results
        label = STEP_LABELS.get(self.step, self.step)
        report(f"正在启动{label}…", 0.01)
        if self.action is None:
            kwargs = {"report": report}
            if self.parameters is not None:
                kwargs["parameters"] = self.parameters
            result = run_stage(self.step, self.output_dir, self.model, **kwargs)
        else:
            result = run_stage_action(self.step, self.action, self.output_dir, self.model,
                                      parameters=self.parameters, report=report,
                                      chapter_number=self.chapter_number, options=self.options)
        report(f"{label}已完成", 1.0)
        return result


def step_work(step, output_dir, model="", *, parameters=None):
    return GenerationWork(step, output_dir, model, parameters=parameters)


def action_work(step, action, output_dir, model="", *, parameters=None, chapter_number=None, options=None):
    return GenerationWork(step, output_dir, model, action=action, parameters=parameters,
                          chapter_number=chapter_number, options=options)


ORDERED_STEPS = ["lore", "structure", "scenes", "chapters"]


def full_workflow_work(output_dir, model="", steps=None, *, parameters=None):
    return GenerationWork("workflow", output_dir, model, steps=steps or ORDERED_STEPS, parameters=parameters)
