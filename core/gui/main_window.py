# -*- coding: utf-8 -*-
"""主窗壳：左侧步骤栏 + 页面堆栈 + 底部状态栏。

只做四件事：装配页面、路由、把页面的任务请求交给唯一的 TaskRunner、
把工作流状态分发给步骤栏与总览页。业务逻辑一律在 services/ 与 core/ 里。
"""
from __future__ import annotations

import logging
import os
from typing import Callable, Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.gui import theme
from core.gui.pages import (
    ChapterWritingPage,
    LorePage,
    ParametersPage,
    ScenePlanPage,
    StagePage,
    StructurePage,
    WorkflowPage,
)
from core.gui.task_runner import TaskRunner
from core.gui.widgets import AppStatusBar, StepRail
from core.gui.widgets.step_rail import StepState
from core.generation.ai_helper import (
    DEFAULT_API_MODEL,
    check_cli_availability,
    get_available_backends,
    get_supported_models,
)
from core.generation.workflow_status import (
    BLOCKED,
    COMPLETE,
    PARTIAL,
    assess_workflow,
)

logger = logging.getLogger("core.gui.main")

STAGE_NAMES = {
    "lore": "世界设定",
    "structure": "故事结构",
    "scenes": "场景规划",
    "chapters": "章节撰写",
}


class MainWindow(QMainWindow):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("NovelWriter")
        self.setMinimumSize(*theme.MIN_WINDOW)
        self.resize(1440, 900)

        self.runner = TaskRunner(logger)
        self._refreshing = False   # 防止 artifacts_changed 与 refresh_all 互相递归

        root = QWidget()
        root.setObjectName("Root")
        root.setAttribute(Qt.WA_StyledBackground, True)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.rail = StepRail()
        outer.addWidget(self.rail)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        self.stack = QStackedWidget()
        right_layout.addWidget(self.stack, 1)
        self.status = AppStatusBar()
        right_layout.addWidget(self.status)
        outer.addWidget(right, 1)
        self.setCentralWidget(root)

        self.pages: Dict[str, QWidget] = {}
        self._build_pages()
        self._wire()
        self._populate_backends()

        self._navigate("workflow")
        self.refresh_all()

    # ================================================== 装配
    def _build_pages(self) -> None:
        self.workflow_page = WorkflowPage()
        self.parameters_page = ParametersPage(
            get_backend=lambda: self.rail.backend_combo.currentText(),
            get_model=lambda: self.rail.model_combo.currentText(),
        )
        self.lore_page = LorePage()
        self.structure_page = StructurePage()
        self.scene_page = ScenePlanPage()
        self.chapter_page = ChapterWritingPage()

        for key, page in [
            ("workflow", self.workflow_page),
            ("parameters", self.parameters_page),
            ("lore", self.lore_page),
            ("structure", self.structure_page),
            ("scenes", self.scene_page),
            ("chapters", self.chapter_page),
        ]:
            self.pages[key] = page
            self.stack.addWidget(page)

    @property
    def stage_pages(self):
        return [self.workflow_page, self.lore_page, self.structure_page,
                self.scene_page, self.chapter_page]

    def _wire(self) -> None:
        self.rail.step_selected.connect(self._on_step_selected)
        self.status.cancel_requested.connect(self.runner.cancel_current)
        self.runner.progress.connect(self._on_task_progress)
        self.runner.task_finished.connect(
            self.chapter_page.finish_generation_progress
        )

        page = self.parameters_page
        page.dirty_changed.connect(self._on_dirty_changed)
        page.saved.connect(self._on_saved)
        page.output_dir_changed.connect(self._on_output_dir_changed)
        page.work_title_changed.connect(self.rail.set_work_title)
        page.operation_failed.connect(self.status.set_error)

        for stage in self.stage_pages:
            stage.task_requested.connect(self._on_task_requested)
            stage.navigate_requested.connect(self._navigate)
            stage.status_message.connect(self._on_status_message)
            stage.artifacts_changed.connect(self.refresh_all)

        self.workflow_page.reset_requested.connect(self._reset_workflow_state)

    def _populate_backends(self) -> None:
        backends = list(get_available_backends()) or ["api"]
        models = list(get_supported_models()) or [DEFAULT_API_MODEL]

        self.rail.backend_combo.addItems([str(b) for b in backends])
        self.rail.model_combo.addItems([str(m) for m in models])
        self._refresh_key_status()
        self.rail.backend_changed.connect(lambda _: self._refresh_key_status())

    def _refresh_key_status(self) -> None:
        backend = self.rail.backend_combo.currentText()
        if backend and backend != "api":
            ok = bool(check_cli_availability(backend))
            self.rail.set_key_status(ok, "CLI 可用" if ok else "CLI 不可用")
            return
        ok = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"))
        self.rail.set_key_status(ok, "密钥已配置" if ok else "未检测到密钥")

    # ================================================== 任务
    def _on_task_requested(self, work: Callable, busy_button, busy_text: str,
                           on_success: Optional[Callable]) -> None:
        if self.runner.busy:
            self.status.set_dirty("已有任务在运行", "同一时刻只允许一个生成任务，请先等待或点「停止」。")
            return

        self.status.set_busy(busy_text, f"输出目录 {self.output_dir()}")

        def success(value) -> None:
            if on_success:
                on_success(value)
            # on_success 通常会刷新产物；完成提示必须最后设置，否则 refresh_all()
            # 会立即把它覆盖成“空闲”。
            self.status.set_done("任务已完成", busy_text.rstrip("…"))

        def error(exc: BaseException) -> None:
            detail = " | ".join(str(exc).splitlines()) or type(exc).__name__
            self.status.set_error("任务失败", detail)
            logger.error(
                "任务失败：%s",
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

        def cancelled() -> None:
            self.refresh_all()
            self.status.set_dirty("任务已取消", "已生成的产物保留在磁盘上。")

        self.runner.run(
            work,
            busy_button=busy_button,
            busy_text=busy_text,
            pass_progress=True,
            on_success=success,
            on_error=error,
            on_cancelled=cancelled,
        )

    def _on_status_message(self, kind: str, text: str) -> None:
        {
            "info": lambda: self.status.set_idle(text),
            "warn": lambda: self.status.set_dirty("提示", text),
            "error": lambda: self.status.set_error("出错", text),
            "done": lambda: self.status.set_done("完成", text),
        }.get(kind, lambda: self.status.set_idle(text))()

    def _on_task_progress(self, text: str, fraction: float) -> None:
        """Update the status bar and refresh chapters at durable checkpoints."""
        self.status.set_progress(text, fraction)
        self.chapter_page.handle_generation_progress(text)

    # ================================================== 路由
    def _navigate(self, key: str) -> None:
        if key not in self.pages:
            return
        self.rail.set_active(key)
        self.stack.setCurrentWidget(self.pages[key])
        page = self.pages[key]
        if isinstance(page, StagePage):
            page.set_output_dir(self.output_dir())

    def _on_step_selected(self, key: str) -> None:
        if key.startswith("blocked:"):
            stage = key.split(":", 1)[1]
            state = self._stage_states().get(stage)
            self.status.set_blocked(
                f"{STAGE_NAMES.get(stage, stage)}被阻塞",
                state.detail if state else "请先完成前置阶段。",
            )
            # 被阻塞也允许进入：页面本身会画出阻塞空态并给出解法
            self._navigate(stage)
            return
        self._navigate(key)

    # ================================================== 状态
    def output_dir(self) -> str:
        return self.parameters_page.output_dir()

    def _stage_states(self) -> Dict[str, StepState]:
        """以磁盘产物为准评估四个生成阶段。"""
        output_dir = self.output_dir()
        states: Dict[str, StepState] = {}
        for stage, result in (assess_workflow(output_dir) or {}).items():
            if result.state == COMPLETE:
                states[stage] = StepState("complete", "已完成", True)
            elif result.state == PARTIAL:
                progress = (f" {result.done}/{result.total}"
                            if getattr(result, "total", 0) else "")
                detail = getattr(result, "detail", "") or f"部分完成{progress}"
                states[stage] = StepState("partial", detail, True)
            elif result.state == BLOCKED:
                states[stage] = StepState(
                    "blocked", getattr(result, "detail", "") or "需先完成前置阶段", False)
            else:
                states[stage] = StepState("idle", "未开始", True)
        return states

    def refresh_all(self) -> None:
        """产物或输出目录变化后的统一刷新入口。

        页面 refresh() 里可能再发 artifacts_changed（总览页就是这么做的），
        这里用一个重入标志挡掉，避免一次刷新变成一串磁盘扫描。
        """
        if self._refreshing:
            return
        self._refreshing = True
        try:
            self._refresh_all()
        finally:
            self._refreshing = False

    def _refresh_all(self) -> None:
        output_dir = self.output_dir()
        states = self._stage_states()

        saved = os.path.exists(os.path.join(output_dir, "system", "parameters.txt"))
        self.rail.set_step_state(
            "parameters",
            StepState("complete" if saved else "idle",
                      "已保存" if saved else "未保存", True),
        )
        for stage, state in states.items():
            self.rail.set_step_state(stage, state)

        for page in self.stage_pages:
            page.output_dir = output_dir
        self.workflow_page.set_states(states)

        current = self.stack.currentWidget()
        if isinstance(current, StagePage) and current is not self.workflow_page:
            current.refresh()

        if not self.runner.busy:
            self.status.set_idle(f"输出目录 {output_dir}")

    def _on_output_dir_changed(self, _path: str) -> None:
        self.refresh_all()

    def _on_dirty_changed(self, dirty: bool) -> None:
        if dirty:
            self.status.set_dirty(
                "参数已修改，尚未保存",
                "改动题材 / 篇幅 / 结构会使已生成的下游产物与当前参数不一致",
            )
        elif not self.runner.busy:
            self.status.set_idle(f"输出目录 {self.output_dir()}")

    def _on_saved(self, path: str) -> None:
        if not path:
            errors = self.parameters_page.validate()
            self.status.set_error(
                "保存失败", "；".join(errors.values()) or "写入参数文件时出错，详见日志")
            return
        self.refresh_all()
        self.status.set_done("参数已保存", path)

    def _reset_workflow_state(self) -> None:
        """只清运行记录，不动任何产物文件。"""
        try:
            from agents.orchestration.checkpoint_state import CheckpointStateManager

            manager = CheckpointStateManager(output_dir=self.output_dir(), logger=logger)
            state = manager.load_state()
            if state is not None:
                manager.reset_workflow(state)
        except Exception as exc:  # noqa: BLE001
            logger.error("重置工作流状态失败：%s", exc, exc_info=True)
            self.refresh_all()
            self.status.set_error("重置失败", str(exc))
            return
        self.refresh_all()
        self.status.set_done("工作流状态已重置", "已生成的文件都保留在磁盘上。")

    # ================================================== 关闭
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self.runner.busy:
            answer = QMessageBox.question(
                self, "仍有任务在运行", "后台任务尚未结束，确定要退出吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.runner.cancel_current()
        event.accept()
