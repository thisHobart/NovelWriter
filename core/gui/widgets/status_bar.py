# -*- coding: utf-8 -*-
"""底部状态栏：单个任务，或按通道列出的多个任务（选中哪个就显示哪个的进度和停止）。

同一时刻只显示一个任务：状态、子进度、已用时、停止按钮。
「停止」只在任务运行时出现，点击后立刻变为「正在停止…」并禁用，
直到 worker 抛出 GenerationCancelled。规格见 docs/feature_boundaries.md「共用界面」。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QPaintEvent
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QProgressBar, QSizePolicy, QWidget

from core.gui import theme
from core.gui.widgets.card import vline
from core.gui.widgets.controls import IconLabel, danger_button


@dataclass(frozen=True)
class TaskText:
    """What the task list shows for one running task; asked for again on every refresh."""

    action: str
    target: str = ""
    location: str = ""
    stopping: bool = False


@dataclass
class TaskDisplay:
    key: object
    describe: Callable[[], TaskText]
    started_at: float
    fraction: float = -1.0
    result: tuple[str, str, str] | None = None


class ElidedStatusLabel(QLabel):
    """Keep full text available to code, while painting within the available width."""

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self.setTextFormat(Qt.PlainText)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setPen(self.palette().windowText().color())
        painter.setFont(self.font())
        value = self.fontMetrics().elidedText(
            self.text(), Qt.ElideRight, self.contentsRect().width()
        )
        painter.drawText(self.contentsRect(), Qt.AlignLeft | Qt.AlignVCenter, value)


class AppStatusBar(QWidget):
    cancel_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("StatusBar")
        self.setFixedHeight(theme.STATUSBAR_HEIGHT)
        self.setAttribute(Qt.WA_StyledBackground, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 0, 24, 0)
        layout.setSpacing(theme.SP[4])

        self.tasks = QComboBox()
        self.tasks.setMinimumWidth(160)
        self.tasks.setMaximumWidth(260)
        self.tasks.hide()
        self.tasks.currentIndexChanged.connect(self.render_task)
        layout.addWidget(self.tasks)
        self.task_count = QLabel()
        self.task_count.hide()
        layout.addWidget(self.task_count)
        self._tasks: dict[str, TaskDisplay] = {}
        self._task_notice: tuple[str, str] | None = None

        self._state = IconLabel("空闲", "dot", theme.INK_400, 14)
        layout.addWidget(self._state)

        self._sep = vline()
        layout.addWidget(self._sep)

        self._context = ElidedStatusLabel("")
        self._context.setObjectName("Secondary")
        layout.addWidget(self._context, 1)

        self._progress = QProgressBar()
        self._progress.setFixedSize(200, 5)
        self._progress.setTextVisible(False)
        self._progress.hide()
        layout.addWidget(self._progress)

        self._elapsed = QLabel("")
        self._elapsed.setObjectName("Tertiary")
        self._elapsed.hide()
        layout.addWidget(self._elapsed)

        self._right = QLabel("")
        self._right.setObjectName("Tertiary")
        layout.addWidget(self._right)

        self._stop = danger_button("停止", "stop")
        self._stop.hide()
        self._stop.clicked.connect(self._on_stop)
        layout.addWidget(self._stop)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._started_at = 0.0

    def start_task(self, channel: str, key: object, describe: Callable[[], TaskText]) -> None:
        """List a task under its channel; ``key`` tells later updates which task they are for."""
        self._task_notice = None
        self._right.clear()
        self._right.setToolTip("")
        self._tasks[channel] = TaskDisplay(key, describe, time.monotonic())
        self._refresh_tasks()

    def update_task(self, channel: str, key: object, fraction: float) -> None:
        entry = self._tasks.get(channel)
        if entry is not None and entry.key is key:
            entry.fraction = fraction
            self._refresh_tasks()

    def finish_task(self, channel: str, key: object, result: tuple[str, str, str]) -> None:
        entry = self._tasks.get(channel)
        if entry is not None and entry.key is key:
            entry.result = result
            if all(item.result is not None for item in self._tasks.values()):
                self._task_notice = None
            self._refresh_tasks()

    def selected_task(self) -> str | None:
        """The channel whose task is picked in the task list."""
        return self.tasks.currentData()

    def clear_tasks(self) -> None:
        self._tasks.clear()
        self._task_notice = None
        self.tasks.clear()
        self.tasks.hide()
        self.task_count.hide()

    def show_notice(self, severity: str, title: str, detail: str = "") -> None:
        if any(entry.result is None for entry in self._tasks.values()):
            self._task_notice = (title, detail)
            self.render_task()
        else:
            {"error": self.set_error, "warning": self.set_dirty, "success": self.set_done}[
                severity
            ](title, detail)

    def _render_task_notice(self) -> None:
        if self._task_notice is not None:
            self._right.setText(self._task_notice[0])
            self._right.setToolTip(self._task_notice[1])

    def _refresh_tasks(self) -> None:
        selected = self.tasks.currentData()
        self.tasks.blockSignals(True)
        self.tasks.clear()
        for channel, entry in self._tasks.items():
            text = entry.describe()
            state = (
                ("正在停止" if text.stopping else "运行中")
                if entry.result is None
                else entry.result[1]
            )
            label = " · ".join(part for part in (text.action, text.target, state) if part)
            self.tasks.addItem(label, channel)
            self.tasks.setItemData(self.tasks.count() - 1, state, Qt.ToolTipRole)
        index = self.tasks.findData(selected)
        self.tasks.setCurrentIndex(index if index >= 0 else 0)
        self.tasks.blockSignals(False)
        self.tasks.setVisible(bool(self._tasks))
        running = sum(entry.result is None for entry in self._tasks.values())
        self.task_count.setText(f"运行中 {running}")
        self.task_count.setVisible(bool(self._tasks))
        self.render_task()

    def render_task(self, _index: int = -1) -> None:
        entry = self._tasks.get(self.tasks.currentData())
        if entry is None:
            return
        if entry.result is not None:
            severity, title, detail = entry.result
            {"error": self.set_error, "warning": self.set_dirty, "success": self.set_done}[
                severity
            ](title, detail)
            self._render_task_notice()
            return
        text = entry.describe()
        self._state.set_state("spinner", theme.ACCENT, text.action + "中…")
        self._context.setText(text.location)
        self._started_at = entry.started_at
        self._step_started_at = entry.started_at
        self._progress.show()
        if entry.fraction < 0:
            self._progress.setRange(0, 0)
        else:
            self._progress.setRange(0, 100)
            self._progress.setValue(int(max(0.0, min(1.0, entry.fraction)) * 100))
        self._elapsed.show()
        self._stop.show()
        stopping = text.stopping
        self._stop.setText("正在停止…" if stopping else "停止")
        self._stop.setEnabled(not stopping)
        self._tick()
        self._timer.start()
        self._render_task_notice()

    # -- 状态切换 ----------------------------------------------------------
    def set_idle(self, context: str = "", right: str = "") -> None:
        self._timer.stop()
        self._state.set_state("dot", theme.INK_400, "空闲")
        self._context.setText(context)
        self._right.setText(right)
        self._progress.hide()
        self._elapsed.hide()
        self._stop.hide()
        self._reset_stop()

    def set_busy(self, text: str, context: str = "") -> None:
        self._right.clear()
        self._state.set_state("spinner", theme.ACCENT, text)
        self._context.setText(context)
        self._progress.show()
        self._progress.setRange(0, 0)  # 不确定进度
        self._elapsed.show()
        self._stop.show()
        self._reset_stop()
        self._started_at = time.monotonic()
        self._step_started_at = self._started_at
        self._step_label = ""
        self._tick()
        self._timer.start()

    def set_progress(self, text: str, fraction: float = -1.0) -> None:
        label = text.split("｜", 1)[0]
        if label != getattr(self, "_step_label", ""):
            self._step_label = label
            self._step_started_at = time.monotonic()
        self._state.set_text(text)
        if fraction < 0:
            self._progress.setRange(0, 0)
        else:
            self._progress.setRange(0, 100)
            self._progress.setValue(int(max(0.0, min(1.0, fraction)) * 100))

    def set_dirty(self, text: str, context: str = "") -> None:
        self._right.clear()
        self._timer.stop()
        self._state.set_state("half", theme.WARN, text)
        self._context.setText(context)
        self._progress.hide()
        self._elapsed.hide()
        self._stop.hide()

    def set_blocked(self, text: str, context: str = "") -> None:
        self._right.clear()
        self._timer.stop()
        self._state.set_state("lock", theme.DANGER, text)
        self._context.setText(context)
        self._progress.hide()
        self._elapsed.hide()
        self._stop.hide()

    def set_done(self, text: str, context: str = "") -> None:
        self._right.clear()
        self._timer.stop()
        self._state.set_state("check", theme.SUCCESS, text)
        self._context.setText(context)
        self._progress.hide()
        self._elapsed.hide()
        self._stop.hide()

    def set_error(self, text: str, context: str = "") -> None:
        self._right.clear()
        self._timer.stop()
        self._state.set_state("half", theme.DANGER, text)
        self._context.setText(context)
        self._progress.hide()
        self._elapsed.hide()
        self._stop.hide()

    def set_right(self, text: str) -> None:
        self._right.setText(text)

    def set_context(self, text: str) -> None:
        self._context.setText(text)

    # -- 内部 --------------------------------------------------------------
    def _on_stop(self) -> None:
        self._stop.setText("正在停止…")
        self._stop.setEnabled(False)
        self.cancel_requested.emit()

    def _reset_stop(self) -> None:
        self._stop.setText("停止")
        self._stop.setEnabled(True)

    def _tick(self) -> None:
        seconds = int(time.monotonic() - self._started_at)
        minutes, secs = divmod(seconds, 60)
        self._elapsed.setText(f"已用 {minutes} 分 {secs} 秒" if minutes else f"已用 {secs} 秒")
        if "｜" in self._state._text.text():
            step_seconds = int(
                time.monotonic() - getattr(self, "_step_started_at", self._started_at)
            )
            self._elapsed.setText(f"本步骤 {step_seconds} 秒 · 总用时 {seconds} 秒")
