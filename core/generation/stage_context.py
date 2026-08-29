# -*- coding: utf-8 -*-
"""一次阶段生成所需的全部输入，与界面框架无关。

生成管线不能读取 QWidget，因此后台任务启动前把模型、参数与输出目录组成一个
普通数据对象。编排器、测试和其他无界面调用方使用同一个类型。

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


def context_from_host(host, **extras) -> StageContext:
    """Build a context from a lightweight host object; primarily useful in tests."""
    parameters = {}
    source = getattr(host, "param_ui", None)
    if source is not None and hasattr(source, "get_current_parameters"):
        try:
            parameters = source.get_current_parameters()
        except Exception:  # noqa: BLE001 - a broken optional source means no parameters
            parameters = {}
    return make_context(
        host.get_output_dir() if host is not None else "current_work",
        host.get_selected_model() if host is not None else "",
        parameters,
        **extras,
    )
