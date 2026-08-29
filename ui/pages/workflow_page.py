# -*- coding: utf-8 -*-
"""工作流总览（主窗壳的默认落地页）。

四个阶段各一张卡片，状态以磁盘产物为准；右栏是断点确认与自动化开关。
对应设计画布第一块板。
"""
from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from ui import icons, theme
from ui.pages.stage_base import StagePage
from ui.services import artifacts
from ui.services.step_runner import full_workflow_work, step_work
from ui.widgets import Card, hint_label, primary_button, secondary_button
from ui.widgets.step_rail import StepState

STAGE_META = [
    ("lore", "世界设定", "世界构建与背景"),
    ("structure", "故事结构", "情节与故事弧"),
    ("scenes", "场景规划", "详细场景大纲"),
    ("chapters", "章节撰写", "最终故事正文"),
]


class StageCard(QWidget):
    run_requested = Signal(str)
    open_requested = Signal(str)

    def __init__(self, key: str, name: str, desc: str,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.key = key
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            f"background: {theme.CANVAS}; border: 1px solid {theme.LINE};"
            f"border-radius: {theme.RADIUS_CARD}px;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(12)

        titles = QVBoxLayout()
        titles.setContentsMargins(0, 0, 0, 0)
        titles.setSpacing(3)
        name_label = QLabel(name)
        name_label.setStyleSheet(
            f"font-size: 14px; font-weight: 600; border: none;")
        desc_label = QLabel(desc)
        desc_label.setStyleSheet(
            f"font-size: {theme.FS_XS}px; color: {theme.INK_400}; border: none;")
        titles.addWidget(name_label)
        titles.addWidget(desc_label)
        top.addLayout(titles, 1)

        self._chip_icon = QLabel()
        self._chip = QLabel()
        top.addWidget(self._chip_icon)
        top.addWidget(self._chip)
        layout.addLayout(top)

        self._detail = QLabel()
        self._detail.setWordWrap(True)
        layout.addWidget(self._detail)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(10)
        self._files = QLabel()
        self._files.setStyleSheet(
            f"font-size: {theme.FS_XS}px; color: {theme.INK_400}; border: none;")
        self._open = secondary_button("打开")
        self._run = secondary_button("单独运行")
        self._open.clicked.connect(lambda: self.open_requested.emit(self.key))
        self._run.clicked.connect(lambda: self.run_requested.emit(self.key))
        bottom.addWidget(self._files, 1)
        bottom.addWidget(self._open)
        bottom.addWidget(self._run)
        layout.addLayout(bottom)

    def apply(self, state: StepState, files: int) -> None:
        palette = {
            "complete": ("#EAF3EE", theme.SUCCESS, "已完成", "check"),
            "partial": ("#FAF2E4", theme.WARN, "部分完成", "half"),
            "blocked": ("#F6F1F0", theme.DANGER, "被阻塞", "lock"),
            "running": (theme.ACCENT_TINT, theme.ACCENT, "进行中", "spinner"),
            "idle": ("#F2F2EF", theme.INK_400, "未开始", "empty"),
        }
        bg, fg, text, icon_name = palette.get(state.state, palette["idle"])
        self._chip_icon.setPixmap(icons.pixmap(icon_name, fg, 14))
        self._chip_icon.setStyleSheet("border: none;")
        self._chip.setText(text)
        self._chip.setStyleSheet(
            f"background: {bg}; color: {fg}; border: none; border-radius: 11px;"
            f"padding: 3px 9px; font-size: {theme.FS_XS}px;")

        detail = state.detail or ""
        self._detail.setVisible(bool(detail))
        self._detail.setText(detail)
        self._detail.setStyleSheet(
            f"background: {theme.SURFACE}; border: 1px solid {theme.LINE};"
            f"border-left: 2px solid {fg}; border-radius: {theme.RADIUS}px;"
            f"padding: 9px 11px; font-size: {theme.FS_XS}px; color: {theme.INK_600};")
        self._files.setText(f"{files} 个文件")
        self._run.setEnabled(state.state != "blocked")


class WorkflowPage(StagePage):
    stage_key = "workflow"

    reset_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("工作流总览", "四个生成阶段的实时状态以磁盘产物为准")

        self.refresh_button = secondary_button("刷新", "refresh")
        self.scan_button = secondary_button("扫描已有成果", "folder")
        self.run_all_button = primary_button("运行完整流程")
        for button in (self.refresh_button, self.scan_button, self.run_all_button):
            self.header.add_action(button)
        self.refresh_button.clicked.connect(self.refresh)
        self.scan_button.clicked.connect(self.refresh)
        self.run_all_button.clicked.connect(self._run_all)

        self._states: Dict[str, StepState] = {}
        self._build_left()
        self._build_inspector()

    # ---------------------------------------------------------------- 左侧
    def _build_left(self) -> None:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        caption_row = QHBoxLayout()
        caption_row.setContentsMargins(0, 0, 0, 0)
        caption = QLabel("阶段产物")
        caption.setObjectName("SectionLabel")
        self.summary = QLabel("")
        self.summary.setObjectName("Secondary")
        caption_row.addWidget(caption)
        caption_row.addStretch(1)
        caption_row.addWidget(self.summary)
        layout.addLayout(caption_row)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(14)
        self.cards: Dict[str, StageCard] = {}
        for index, (key, name, desc) in enumerate(STAGE_META):
            card = StageCard(key, name, desc)
            card.run_requested.connect(self._run_single)
            card.open_requested.connect(self.navigate_requested)
            grid.addWidget(card, index // 2, index % 2)
            self.cards[key] = card
        layout.addLayout(grid)
        layout.addStretch(1)
        self.set_left(holder)

    # ---------------------------------------------------------------- 右侧
    def _build_inspector(self) -> None:
        checkpoint = Card("断点")
        self.checkpoint_text = QLabel("")
        self.checkpoint_text.setWordWrap(True)
        self.checkpoint_text.setObjectName("Secondary")
        checkpoint.body.addWidget(self.checkpoint_text)
        self.add_card(checkpoint)

        automation = Card("自动化")
        self.resume_button = secondary_button("从断点继续")
        self.analyze_button = secondary_button("分析当前内容")
        self.reset_button = secondary_button("重置工作流状态")
        self.resume_button.clicked.connect(self._resume)
        self.analyze_button.clicked.connect(self._analyze)
        self.reset_button.clicked.connect(self._reset)
        for button in (self.resume_button, self.analyze_button, self.reset_button):
            automation.body.addWidget(button)
        automation.body.addWidget(hint_label(
            "重置只清空运行记录，不删除任何已生成的文件。"))
        self.add_card(automation)

        self.finish_inspector()

    # ---------------------------------------------------------------- 行为
    def _run_all(self) -> None:
        self.run_task(
            full_workflow_work(self.output_dir),
            busy_button=self.run_all_button,
            busy_text="正在运行完整流程…",
            on_success=lambda _: (self.refresh(), self.artifacts_changed.emit()),
        )

    def _run_single(self, key: str) -> None:
        self.run_task(
            step_work(key, self.output_dir),
            busy_button=self.run_all_button,
            busy_text=f"正在运行{dict((k, n) for k, n, _ in STAGE_META).get(key, key)}…",
            on_success=lambda _: (self.refresh(), self.artifacts_changed.emit()),
        )

    def _resume(self) -> None:
        pending = [key for key, state in self._states.items()
                   if state.state in ("idle", "partial")]
        if not pending:
            self.status_message.emit("info", "没有待继续的阶段。")
            return
        self.run_task(
            full_workflow_work(self.output_dir, steps=pending),
            busy_button=self.resume_button,
            busy_text="正在继续…",
            on_success=lambda _: (self.refresh(), self.artifacts_changed.emit()),
        )

    def _analyze(self) -> None:
        counts = artifacts.stage_file_counts(self.output_dir)
        chapters = artifacts.chapters(self.output_dir)
        words = sum(c.words for c in chapters)
        self.status_message.emit(
            "info",
            "设定 {lore} · 结构 {structure} · 场景 {scenes} · 章节 {chapters} 个文件，"
            "正文累计 {words:,} 字".format(words=words, **counts),
        )

    def _reset(self) -> None:
        answer = QMessageBox.question(
            self, "重置工作流状态",
            "只清空运行记录（system 下的 checkpoint 状态），不会删除任何已生成的文件。继续吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.reset_requested.emit()

    # ---------------------------------------------------------------- 刷新
    def set_states(self, states: Dict[str, StepState]) -> None:
        self._states = states
        counts = artifacts.stage_file_counts(self.output_dir)
        complete = 0
        for key, card in self.cards.items():
            state = states.get(key, StepState())
            card.apply(state, counts.get(key, 0))
            complete += 1 if state.state == "complete" else 0
        self.summary.setText(f"{complete} / 4 阶段已完成")

        blocked = [key for key, state in states.items() if state.state == "blocked"]
        partial = [key for key, state in states.items() if state.state == "partial"]
        names = dict((k, n) for k, n, _ in STAGE_META)
        if blocked:
            self.checkpoint_text.setText(
                "被阻塞：" + "、".join(names[k] for k in blocked)
                + "。先解决前置阶段的问题，或在对应页面选择强制执行。")
        elif partial:
            self.checkpoint_text.setText(
                "部分完成：" + "、".join(names[k] for k in partial)
                + "。可以点「从断点继续」补齐。")
        elif complete == 4:
            self.checkpoint_text.setText("四个阶段都已完成，可以去章节撰写页通读全文。")
        else:
            self.checkpoint_text.setText("尚未开始。填好作品参数后点「运行完整流程」。")

    def refresh(self) -> None:
        self.artifacts_changed.emit()
