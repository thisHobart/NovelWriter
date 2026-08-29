# -*- coding: utf-8 -*-
"""一次阶段生成所需的全部输入，与界面框架无关。

原先叫 `UiSnapshot`，长在 `core/gui/task_runner.py` 里：Tk 变量只能在主线程读，
所以后台任务开跑前先在主线程把值一次读齐，worker 只从快照取值。

Qt 界面接手后这个约束没变（QWidget 同样不许跨线程碰），但"快照"已经不只是界面
取值了——编排器无头运行时也要构造一个，所以改名 `StageContext` 并搬到
`core/generation/` 下，成为生成层的入参类型。

`report` 是可选的进度回调 `(text, fraction) -> None`，fraction 为负表示"不确定进度"。
生成函数调用它来汇报子步骤；取消检查由回调实现方负责（Qt 的 TaskRunner 会在
report 里检查取消令牌并抛 GenerationCancelled）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

ProgressReport = Callable[[str, float], None]


@dataclass
class StageContext:
    """阶段生成的入参快照。"""

    output_dir: str = "current_work"
    model: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)
    extras: Dict[str, Any] = field(default_factory=dict)
    report: Optional[ProgressReport] = None

    def get(self, name: str, default: Any = None) -> Any:
        """取阶段自己的输入（数量、章节编号等）。"""
        return self.extras.get(name, default)

    def progress(self, text: str, fraction: float = -1.0) -> None:
        """汇报子步骤进度。没有回调时静默丢弃，生成逻辑不必判空。"""
        if self.report is not None:
            self.report(text, fraction)


# 迁移期的旧名字。新代码一律用 StageContext。
UiSnapshot = StageContext


def make_context(output_dir: str, model: str = "", parameters: Optional[Dict] = None,
                 report: Optional[ProgressReport] = None, **extras) -> StageContext:
    """构造一个阶段上下文。无头调用（编排器、测试）用这个入口。"""
    return StageContext(
        output_dir=output_dir or "current_work",
        model=model or "",
        parameters=dict(parameters or {}),
        extras=extras,
        report=report,
    )
