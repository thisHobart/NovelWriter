# -*- coding: utf-8 -*-
"""底部状态栏——全局唯一的后台任务通道。

同一时刻只显示一个任务：状态、子进度、已用时、停止按钮。
「停止」只在任务运行时出现，点击后立刻变为「正在停止…」并禁用，
直到 worker 抛出 GenerationCancelled。规格见 docs/UI_CONTRACT.md 第 1.2 节。
"""
from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QWidget

from core.gui import theme
from core.gui.widgets.card import vline
from core.gui.widgets.controls import IconLabel, danger_button


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

        self._state = IconLabel("空闲", "dot", theme.INK_400, 14)
        layout.addWidget(self._state)

        self._sep = vline()
        layout.addWidget(self._sep)

        self._context = QLabel("")
        self._context.setObjectName("Secondary")
        layout.addWidget(self._context)

        self._progress = QProgressBar()
        self._progress.setFixedSize(200, 5)
        self._progress.setTextVisible(False)
        self._progress.hide()
        layout.addWidget(self._progress)

        self._elapsed = QLabel("")
        self._elapsed.setObjectName("Tertiary")
        self._elapsed.hide()
        layout.addWidget(self._elapsed)

        layout.addStretch(1)

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
        self._state.set_state("spinner", theme.ACCENT, text)
        self._context.setText(context)
        self._progress.show()
        self._progress.setRange(0, 0)  # 不确定进度
        self._elapsed.show()
        self._stop.show()
        self._reset_stop()
        self._started_at = time.monotonic()
        self._tick()
        self._timer.start()

    def set_progress(self, text: str, fraction: float = -1.0) -> None:
        self._state.set_text(text)
        if fraction < 0:
            self._progress.setRange(0, 0)
        else:
            self._progress.setRange(0, 100)
            self._progress.setValue(int(max(0.0, min(1.0, fraction)) * 100))

    def set_dirty(self, text: str, context: str = "") -> None:
        self._timer.stop()
        self._state.set_state("half", theme.WARN, text)
        self._context.setText(context)
        self._progress.hide()
        self._elapsed.hide()
        self._stop.hide()

    def set_blocked(self, text: str, context: str = "") -> None:
        self._timer.stop()
        self._state.set_state("lock", theme.DANGER, text)
        self._context.setText(context)
        self._progress.hide()
        self._elapsed.hide()
        self._stop.hide()

    def set_done(self, text: str, context: str = "") -> None:
        self._timer.stop()
        self._state.set_state("check", theme.SUCCESS, text)
        self._context.setText(context)
        self._progress.hide()
        self._elapsed.hide()
        self._stop.hide()

    def set_error(self, text: str, context: str = "") -> None:
        self._timer.stop()
        self._state.set_state("half", theme.DANGER, text)
        self._context.setText(context)
        self._progress.hide()
        self._elapsed.hide()
        self._stop.hide()

    def set_right(self, text: str) -> None:
        self._right.setText(text)

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
        self._elapsed.setText(
            f"已用 {minutes} 分 {secs} 秒" if minutes else f"已用 {secs} 秒"
        )
