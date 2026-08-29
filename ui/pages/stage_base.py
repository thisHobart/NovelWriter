# -*- coding: utf-8 -*-
"""四个生成阶段页面的共同骨架。

统一结构：页头（标题 / 副标题 / 动作） + 左侧内容区 + 右侧 340px 检查器。
页面不自己起后台任务，而是发 `task_requested` 让 MainWindow 交给 TaskRunner，
这样「同一时刻只允许一个前台任务」的约束只在一处实现。
"""
from __future__ import annotations

from typing import Callable, List, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from ui import theme
from ui.widgets import Card, PageHeader


class StagePage(QWidget):
    """阶段页基类。子类实现 `refresh()`，其余由基类装配。"""

    #: work, busy_button, busy_text, on_success
    task_requested = Signal(object, object, str, object)
    navigate_requested = Signal(str)
    status_message = Signal(str, str)      # kind(info/warn/error/done), text
    artifacts_changed = Signal()

    stage_key = ""

    def __init__(self, title: str, subtitle: str = "",
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.output_dir = "current_work"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = PageHeader(title, subtitle)
        layout.addWidget(self.header)

        body = QWidget()
        self._body = QHBoxLayout(body)
        self._body.setContentsMargins(24, 22, 24, 22)
        self._body.setSpacing(18)
        layout.addWidget(body, 1)

        self._left_holder: Optional[QWidget] = None

        self._inspector = QWidget()
        self._inspector.setFixedWidth(theme.INSPECTOR_WIDTH)
        self._inspector_layout = QVBoxLayout(self._inspector)
        self._inspector_layout.setContentsMargins(0, 0, 0, 0)
        self._inspector_layout.setSpacing(18)

    # ---------------------------------------------------------------- 装配
    def set_left(self, widget: QWidget) -> None:
        """左侧内容区只能设一次；重设时替换旧的。"""
        if self._left_holder is not None:
            self._body.removeWidget(self._left_holder)
            self._left_holder.deleteLater()
        self._left_holder = widget
        self._body.insertWidget(0, widget, 1)

    def finish_inspector(self, stretch_last: bool = False) -> None:
        """所有检查器卡片加完后调用一次，把检查器挂到右侧。"""
        if not stretch_last:
            self._inspector_layout.addStretch(1)
        self._body.addWidget(self._inspector, 0)

    def add_card(self, card: Card, stretch: int = 0) -> Card:
        self._inspector_layout.addWidget(card, stretch)
        return card

    # ---------------------------------------------------------------- 任务
    def run_task(self, work: Callable, busy_button: Optional[QWidget] = None,
                 busy_text: str = "正在生成…",
                 on_success: Optional[Callable] = None) -> None:
        self.task_requested.emit(work, busy_button, busy_text, on_success)

    # ---------------------------------------------------------------- 数据
    def set_output_dir(self, output_dir: str) -> None:
        if output_dir and output_dir != self.output_dir:
            self.output_dir = output_dir
        self.refresh()

    def refresh(self) -> None:
        """子类重写：从磁盘重新读产物并刷新界面。"""

    # ---------------------------------------------------------------- 工具
    @staticmethod
    def clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            elif item.layout() is not None:
                StagePage.clear_layout(item.layout())
