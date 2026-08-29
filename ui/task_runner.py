# -*- coding: utf-8 -*-
"""把耗时生成任务挪出 Qt 主线程。

替代 core/gui/task_runner.py（Tk 版用 root.after 轮询队列）。调用方约定见
docs/UI_CONTRACT.md 第 2 节：

    task = runner.run(
        work,                        # 后台线程执行，禁止触碰任何 QWidget
        disable=[btn_a, btn_b],      # 期间禁用，结束后恢复（异常也恢复）
        busy_button=btn_a,           # 该按钮额外显示 busy_text
        busy_text="正在生成…",
        on_success=..., on_error=..., on_cancelled=..., on_done=...,
    )

取消由状态栏统一接管：runner 把当前任务交给 MainWindow，由状态栏的「停止」
调用 task.cancel()。调用方不再自己传 cancel_button。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Iterable, Optional

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtWidgets import QWidget

try:  # 与旧版共用同一个取消令牌，业务代码无需改动
    from core.generation.cancellation import CancelToken, GenerationCancelled
except Exception:  # pragma: no cover - 独立运行界面层时的兜底
    class GenerationCancelled(Exception):
        """任务被用户取消。"""

    class CancelToken:  # type: ignore[no-redef]
        def __init__(self) -> None:
            self._cancelled = False

        def cancel(self) -> None:
            self._cancelled = True

        def reset(self) -> None:
            self._cancelled = False

        @property
        def cancelled(self) -> bool:
            return self._cancelled

        def raise_if_cancelled(self) -> None:
            if self._cancelled:
                raise GenerationCancelled()


class TaskSignals(QObject):
    """worker 与主线程之间唯一的通道。所有信号都在主线程投递。"""

    succeeded = Signal(object)
    failed = Signal(object)
    cancelled = Signal()
    finished = Signal()
    progress = Signal(str, float)  # 文案, 0..1（<0 表示不确定进度）


class BackgroundTask(QRunnable):
    """一次后台执行。对外只暴露 signals / cancel / running。"""

    def __init__(self, work: Callable[..., Any], token: CancelToken,
                 pass_progress: bool, logger: logging.Logger) -> None:
        super().__init__()
        self.signals = TaskSignals()
        self._work = work
        self._token = token
        self._pass_progress = pass_progress
        self._logger = logger
        self._running = True
        self.started_at = time.monotonic()
        self.setAutoDelete(True)

    # -- 主线程调用 --------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running

    def cancel(self) -> None:
        self._token.cancel()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    # -- 后台线程 ----------------------------------------------------------
    @Slot()
    def run(self) -> None:
        try:
            if self._pass_progress:
                value = self._work(self._report)
            else:
                value = self._work()
        except GenerationCancelled:
            self._logger.info("任务被取消")
            self.signals.cancelled.emit()
        except BaseException as exc:  # noqa: BLE001 - 交给 UI 呈现
            self._logger.error("后台任务失败：%s", exc, exc_info=True)
            self.signals.failed.emit(exc)
        else:
            self.signals.succeeded.emit(value)
        finally:
            self._running = False
            self.signals.finished.emit()

    def _report(self, text: str, fraction: float = -1.0) -> None:
        """worker 侧的进度上报入口；只发信号，不碰控件。"""
        self._token.raise_if_cancelled()
        self.signals.progress.emit(text, fraction)


class TaskRunner(QObject):
    """全局单实例，由 MainWindow 持有。

    同一时刻只允许一个前台可见任务（状态栏是单通道），第二个 run() 会被拒绝
    并回调 on_busy，避免两个生成任务互相覆盖进度显示。
    """

    task_started = Signal(object)   # BackgroundTask
    task_finished = Signal()
    progress = Signal(str, float)

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        super().__init__()
        self._pool = QThreadPool.globalInstance()
        self._pool.setMaxThreadCount(max(2, self._pool.maxThreadCount()))
        self._logger = logger or logging.getLogger("ui.task")
        self._current: Optional[BackgroundTask] = None

    @property
    def busy(self) -> bool:
        return self._current is not None and self._current.running

    @property
    def current(self) -> Optional[BackgroundTask]:
        return self._current

    def cancel_current(self) -> None:
        if self._current is not None:
            self._current.cancel()

    def run(
        self,
        work: Callable[..., Any],
        *,
        disable: Iterable[QWidget] = (),
        busy_button: Optional[QWidget] = None,
        busy_text: Optional[str] = None,
        on_success: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
        on_cancelled: Optional[Callable[[], None]] = None,
        on_done: Optional[Callable[[], None]] = None,
        on_busy: Optional[Callable[[], None]] = None,
        pass_progress: bool = False,
        cancel_token: Optional[CancelToken] = None,
    ) -> Optional[BackgroundTask]:
        if self.busy:
            self._logger.warning("已有任务在运行，拒绝新任务")
            if on_busy:
                on_busy()
            return None

        token = cancel_token or CancelToken()
        token.reset()
        widgets = [w for w in disable if w is not None]
        original_text = None
        if busy_button is not None and busy_text and hasattr(busy_button, "text"):
            original_text = busy_button.text()

        for widget in widgets:
            widget.setEnabled(False)
        if busy_button is not None:
            busy_button.setEnabled(False)
            if original_text is not None:
                busy_button.setText(busy_text)

        task = BackgroundTask(work, token, pass_progress, self._logger)
        self._current = task

        def restore() -> None:
            for widget in widgets:
                widget.setEnabled(True)
            if busy_button is not None:
                busy_button.setEnabled(True)
                if original_text is not None:
                    busy_button.setText(original_text)
            self._current = None
            self.task_finished.emit()
            if on_done:
                on_done()

        task.signals.progress.connect(self.progress, Qt.QueuedConnection)
        if on_success:
            task.signals.succeeded.connect(on_success, Qt.QueuedConnection)
        if on_error:
            task.signals.failed.connect(on_error, Qt.QueuedConnection)
        if on_cancelled:
            task.signals.cancelled.connect(on_cancelled, Qt.QueuedConnection)
        task.signals.finished.connect(restore, Qt.QueuedConnection)

        self.task_started.emit(task)
        self._pool.start(task)
        return task
