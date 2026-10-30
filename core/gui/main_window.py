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
    QDialog,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.gui import theme
from core.gui.model_config_dialog import ModelConfigDialog
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
from core.generation.lore_repository import LoreRepository
from core.lore_localization import file_label
from core.gui.services.step_runner import GenerationWork
from core.generation.parameters import normalize_parameters, creation_parameters
from core.generation.stage_pipeline import load_stage_parameters
from core.generation.ai_helper import (
    DEFAULT_API_MODEL,
    get_available_backends,
    get_supported_models,
)
from core.generation.api_providers import PROVIDER_LABELS, configuration_status
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
            get_model=self.selected_model,
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
        self.rail.configuration_requested.connect(self._configure_model)
        self.status.cancel_requested.connect(self.runner.cancel_current)
        self.runner.progress.connect(self._on_task_progress)
        self.runner.task_finished.connect(
            self.chapter_page.finish_generation_progress
        )
        self.runner.task_started.connect(lambda _: self.lore_page.set_generation_busy(True))
        self.runner.task_finished.connect(lambda: self.lore_page.set_generation_busy(False))

        page = self.parameters_page
        page.dirty_changed.connect(self._on_dirty_changed)
        page.saved.connect(self._on_saved)
        page.output_dir_changed.connect(self._on_output_dir_changed)
        page.work_title_changed.connect(self.rail.set_work_title)
        page.operation_failed.connect(self.status.set_error)
        page.model_loaded.connect(self._restore_model)
        for stepper in (self.lore_page.faction_stepper, self.lore_page.character_stepper):
            stepper.valueChanged.connect(lambda _: page.set_generation_counts(
                self.lore_page.faction_stepper.value(), self.lore_page.character_stepper.value()))

        for stage in self.stage_pages:
            stage.task_requested.connect(self._on_task_requested)
            stage.navigate_requested.connect(self._navigate)
            stage.status_message.connect(self._on_status_message)
            stage.artifacts_changed.connect(self.refresh_all)


    def _populate_backends(self) -> None:
        backends = list(get_available_backends()) or ["api"]
        models = list(get_supported_models()) or [DEFAULT_API_MODEL]

        self.rail.backend_combo.addItems([str(b) for b in backends])
        for model in models:
            self.rail.model_combo.addItem(PROVIDER_LABELS.get(model, model), model)
        self._restore_model(getattr(self.parameters_page, "loaded_model", ""))
        self.rail.model_changed.connect(self._on_model_changed)

    def selected_model(self) -> str:
        return self.rail.model_combo.currentData() or DEFAULT_API_MODEL

    def _restore_model(self, model: str) -> None:
        combo = self.rail.model_combo
        index = combo.findData(model)
        blocked = combo.blockSignals(True)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(blocked)
        self._sync_model_selection()

    def _sync_model_selection(self) -> None:
        for page in self.stage_pages:
            page.model = self.selected_model()
        self._refresh_key_status()

    def _on_model_changed(self, _text: str) -> None:
        self._sync_model_selection()
        self.parameters_page._mark_dirty()

    def _refresh_key_status(self) -> None:
        ok, detail = configuration_status(self.selected_model())
        self.rail.set_key_status(ok, "配置已就绪" if ok else "配置不完整")
        self.rail.key_status.setToolTip(detail)
        self.rail.model_combo.setToolTip(detail)

    def _configure_model(self) -> None:
        if self.runner.busy:
            return
        dialog = ModelConfigDialog(self.selected_model(), self)
        try:
            if dialog.exec() == QDialog.Accepted:
                index = self.rail.model_combo.findData(dialog.selected_provider())
                self.rail.model_combo.setCurrentIndex(index)
                self._sync_model_selection()
                self.status.set_done("模型配置已保存", "已生效，将用于下一次生成任务")
        finally:
            dialog.deleteLater()

    # ================================================== 任务
    def _choose_generation_parameters(self):
        stored = load_stage_parameters(self.output_dir())
        current = normalize_parameters(stored, self.parameters_page.parameters())
        use_saved = False
        if self.parameters_page.dirty or not stored or creation_parameters(current) != creation_parameters(stored):
            box = QMessageBox(self)
            box.setWindowTitle("作品参数尚未保存")
            box.setText("当前填写与已保存参数不同，请选择本次生成使用的版本。")
            save = box.addButton("保存并生成", QMessageBox.AcceptRole)
            old = box.addButton("使用已保存参数", QMessageBox.ActionRole)
            old.setEnabled(bool(stored))
            box.addButton("取消", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is save:
                if not self.parameters_page.save():
                    self._navigate("parameters")
                    return None
                chosen = load_stage_parameters(self.output_dir())
            elif box.clickedButton() is old and stored:
                chosen, use_saved = stored, True
            else:
                return None
        else:
            chosen = current
        chosen = normalize_parameters(chosen, {
            "model": self.selected_model(), "backend": self.rail.backend_combo.currentText(),
        })
        return chosen, use_saved

    def _on_task_requested(self, work: Callable, busy_button, busy_text: str,
                           on_success: Optional[Callable]) -> None:
        if self.runner.busy:
            self.status.set_dirty("已有任务在运行", "同一时刻只允许一个生成任务，请先等待或点「停止」。")
            return

        if not self.lore_page.resolve_draft():
            return
        use_saved = False
        if isinstance(work, GenerationWork):
            try:
                selection = self._choose_generation_parameters()
                if selection is None:
                    return
                snapshot, use_saved = selection
                work = work.bind_parameters(snapshot)
            except (OSError, ValueError) as exc:
                self.status.set_error("参数无法用于生成", str(exc))
                self._navigate("parameters")
                return
        action = getattr(work, "lore_action", None)
        if action == "repair":
            from core.generation.lore_run import load_receipt
            try:
                receipt = load_receipt(self.output_dir(), (work.options or {}).get("receipt_id"))
                action = {"world": "lore", "background": "enhance"}.get(receipt["kind"], receipt["kind"])
            except (OSError, ValueError, KeyError) as exc:
                self.status.set_error("无法读取设定修复对象", str(exc))
                return
        if action and action != "titles":
            try:
                status = LoreRepository(self.output_dir()).review_status()
                manual = status.get("manual_files", [])
                affected = [p for p in manual if action == "full" or
                            (action == "lore" and p.endswith(("generated_lore.md", "lore_contract.json"))) or
                            (action == "characters" and p.endswith("characters.json")) or
                            (action == "factions" and p.endswith("factions.json")) or
                            (action == "enhance" and "background_" in p)]
                if action == "enhance" and getattr(work, "options", None) and work.options.get("target"):
                    affected = [p for p in affected if p == work.options["target"]]
                if affected:
                    answer = QMessageBox.question(
                        self, "重新生成将覆盖人工修改",
                        "以下人工修改内容可能被覆盖，运行前会自动备份：\n" + "\n".join(file_label(path) for path in affected),
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                    )
                    if answer != QMessageBox.Yes:
                        return
            except Exception as exc:
                self.status.set_error("无法检查设定修改状态", str(exc))
                return

        self.status.set_busy(busy_text, ("本次使用已保存参数 · " if use_saved else "") + f"输出目录 {self.output_dir()}")

        def success(value) -> None:
            if on_success:
                on_success(value)
            # on_success 通常会刷新产物；完成提示必须最后设置，否则 refresh_all()
            # 会立即把它覆盖成“空闲”。
            from core.generation.parameter_compliance import collect_parameter_warnings
            warnings = collect_parameter_warnings(value)
            def statuses(item):
                if not isinstance(item, dict):
                    return []
                return [item.get("status")] + [s for v in item.values() if isinstance(v, dict) for s in statuses(v)]
            states = statuses(value)
            outcome = next((s for s in ("draft", "needs_changes", "unchecked") if s in states), None)
            if outcome == "draft":
                self.status.set_dirty("草稿已保留，可继续修改", "；".join(warnings))
            elif outcome == "unchecked":
                self.status.set_dirty("已生成，检查未完成", "；".join(warnings))
            elif warnings or outcome == "needs_changes":
                self.status.set_dirty("已生成，待修改", "；".join(warnings))
            else:
                self.status.set_done("任务已完成", busy_text.rstrip("…"))

        def error(exc: BaseException) -> None:
            self.refresh_all()
            detail = " | ".join(str(exc).splitlines()) or type(exc).__name__
            self.status.set_error("任务失败", detail)
            logger.error(
                "任务失败：%s",
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

        def cancelled() -> None:
            self.refresh_all()
            self.status.set_dirty("任务已取消", "已返回内容保留，可到「世界设定 → 生成结果与草稿」查看。")

        self.runner.run(
            work,
            disable=[self.rail.backend_combo, self.rail.model_combo, self.rail.config_button,
                     self.parameters_page.output_dir_edit, self.parameters_page.browse_button],
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
        if key != "lore" and not self.lore_page.resolve_draft():
            self.rail.set_active("lore")
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
        try:
            pending = LoreRepository(output_dir).review_status().get("pending", {})
            for stage in {item["stage"] for item in pending.values()}:
                if stage in states:
                    states[stage].detail += " · 设定已修改，待核对"
        except (OSError, ValueError):
            pass
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
        if not self.runner.busy:
            try:
                LoreRepository(output_dir).recover_pending()
            except Exception as exc:
                self.status.set_error("无法恢复设定保存状态", str(exc))
                return
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
            state = states.get(page.stage_key)
            needs_review = state and "设定已修改，待核对" in state.detail
            page.lore_review_notice.setText("设定已修改，待核对。已有内容保留，可继续生成；请到「世界设定 → 待核对」查看影响范围。" if needs_review else "")
            page.lore_review_notice.setVisible(bool(needs_review))
        for key, stepper in (("num_factions", self.lore_page.faction_stepper),
                             ("num_characters", self.lore_page.character_stepper)):
            blocked = stepper.blockSignals(True)
            try:
                stepper.set_value(int(self.parameters_page.generation_counts[key]))
            except (ValueError, TypeError):
                pass
            finally:
                stepper.blockSignals(blocked)
        self.workflow_page.set_states(states)

        current = self.stack.currentWidget()
        if isinstance(current, StagePage) and current is not self.workflow_page:
            current.refresh()

        if not self.runner.busy:
            self.status.set_idle(f"输出目录 {output_dir}")

    def _on_output_dir_changed(self, _path: str) -> None:
        if not self.lore_page.resolve_draft():
            self.parameters_page.output_dir_edit.setText(self.lore_page.output_dir)
            return
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

    # ================================================== 关闭
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if not self.lore_page.resolve_draft():
            event.ignore()
            return
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
