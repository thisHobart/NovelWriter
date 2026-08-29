# -*- coding: utf-8 -*-
"""阶段 4 · 场景规划。

两个形态：被阻塞时显示「为什么空、怎么解开」的空态；解锁后显示章节大纲与
逐章场景规划。规格见 docs/UI_CONTRACT.md 第 8 节。
"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ui import icons, theme
from ui.pages.lore_page import read_only_text
from ui.pages.stage_base import StagePage
from ui.services import artifacts
from ui.services.step_runner import step_work
from ui.widgets import (
    ArtifactList,
    Card,
    GatePanel,
    hint_label,
    primary_button,
    secondary_button,
)
from ui.widgets.inspector import StepButton


class ScenePlanPage(StagePage):
    stage_key = "scenes"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("场景规划", "详细场景大纲")

        self.outline_button = secondary_button("生成章节大纲")
        self.primary = primary_button("规划场景")
        self.header.add_action(self.outline_button)
        self.header.add_action(self.primary)
        self.outline_button.clicked.connect(self._run_stage)
        self.primary.clicked.connect(self._run_stage)

        self._build_left()
        self._build_inspector()
        self.refresh()

    # ---------------------------------------------------------------- 左侧
    def _build_left(self) -> None:
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_blocked_view())
        self.stack.addWidget(self._build_plans_view())
        self.set_left(self.stack)

    def _build_blocked_view(self) -> QWidget:
        holder = QWidget()
        holder.setAttribute(Qt.WA_StyledBackground, True)
        holder.setStyleSheet(
            f"background: {theme.SURFACE}; border: 1px dashed {theme.LINE_STRONG};"
            f"border-radius: {theme.RADIUS_CARD}px;")

        outer = QVBoxLayout(holder)
        outer.setContentsMargins(40, 40, 40, 40)
        outer.addStretch(1)

        column = QWidget()
        column.setMaximumWidth(520)
        column.setStyleSheet("border: none; background: transparent;")
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(20)
        layout.setAlignment(Qt.AlignHCenter)

        badge = QLabel()
        badge.setAlignment(Qt.AlignCenter)
        badge.setPixmap(icons.pixmap("lock", theme.INK_400, 26))
        layout.addWidget(badge)

        self.blocked_title = QLabel("需先完成「故事结构」")
        self.blocked_title.setAlignment(Qt.AlignCenter)
        self.blocked_title.setStyleSheet(
            f"font-family: {theme.SERIF_STACK}; font-size: 18px; font-weight: 600;"
            f"border: none;")
        layout.addWidget(self.blocked_title)

        self.blocked_detail = QLabel()
        self.blocked_detail.setAlignment(Qt.AlignCenter)
        self.blocked_detail.setWordWrap(True)
        self.blocked_detail.setObjectName("Secondary")
        layout.addWidget(self.blocked_detail)

        reasons = QWidget()
        reasons.setStyleSheet(
            f"background: {theme.CANVAS}; border: 1px solid {theme.LINE};"
            f"border-radius: {theme.RADIUS_CARD}px;")
        self._reasons_layout = QVBoxLayout(reasons)
        self._reasons_layout.setContentsMargins(18, 16, 18, 16)
        self._reasons_layout.setSpacing(12)
        layout.addWidget(reasons)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.setAlignment(Qt.AlignHCenter)
        self.goto_structure_button = primary_button("前往故事结构")
        self.force_button = secondary_button("忽略门禁，强制规划")
        self.goto_structure_button.clicked.connect(
            lambda: self.navigate_requested.emit("structure"))
        self.force_button.clicked.connect(self._run_stage)
        buttons.addWidget(self.goto_structure_button)
        buttons.addWidget(self.force_button)
        layout.addLayout(buttons)

        layout.addWidget(hint_label(
            "强制规划不会删除任何已有产物，但生成的场景不会被标记为已通过。"))

        outer.addWidget(column, 0, Qt.AlignHCenter)
        outer.addStretch(1)
        return holder

    def _build_plans_view(self) -> QWidget:
        card = Card("场景规划", QLabel(""))
        card.body.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setStyleSheet(f"QSplitter::handle {{ background: {theme.LINE}; }}")

        self.plan_list = ArtifactList()
        self.plan_list.setFixedWidth(220)
        self.plan_list.currentRowChanged.connect(self._on_plan_selected)
        splitter.addWidget(self.plan_list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(20, 16, 20, 16)
        self.plan_view = read_only_text("")
        right_layout.addWidget(self.plan_view)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)

        card.body.addWidget(splitter)
        return card

    # ---------------------------------------------------------------- 右侧
    def _build_inspector(self) -> None:
        deps = Card("输入依赖")
        self._deps_layout = QVBoxLayout()
        self._deps_layout.setContentsMargins(0, 0, 0, 0)
        self._deps_layout.setSpacing(9)
        deps.body.addLayout(self._deps_layout)
        deps.body.addWidget(hint_label(
            "门禁读取磁盘产物，不依赖运行记录。手改过文件后点侧栏「刷新」重新评估。"))
        self.add_card(deps)

        self.steps_meta = QLabel("")
        self.steps_meta.setObjectName("Tertiary")
        steps = Card("分步生成", self.steps_meta)
        self.step_buttons = {
            "outline": StepButton("outline", "生成章节大纲"),
            "scenes": StepButton("scenes", "规划场景"),
        }
        for button in self.step_buttons.values():
            button.clicked.connect(lambda _key: self._run_stage())
            steps.body.addWidget(button)
        self.add_card(steps)

        self.files_meta = QLabel("")
        self.files_meta.setObjectName("Tertiary")
        outputs = Card("产物", self.files_meta)
        self.outputs_hint = hint_label("")
        outputs.body.addWidget(self.outputs_hint)
        self.add_card(outputs)

        self.gate = GatePanel()
        gate_card = Card("门禁")
        gate_card.body.addWidget(self.gate)
        self.add_card(gate_card)

        self.finish_inspector()

    # ---------------------------------------------------------------- 行为
    def _run_stage(self) -> None:
        self.run_task(
            step_work("scenes", self.output_dir),
            busy_button=self.primary,
            busy_text="正在规划场景…",
            on_success=lambda _: (self.refresh(), self.artifacts_changed.emit()),
        )

    def _on_plan_selected(self, row: int) -> None:
        if 0 <= row < len(self._plans):
            self.plan_view.setPlainText(self._plans[row].text or "（该文件为空）")

    # ---------------------------------------------------------------- 刷新
    def _dependency_rows(self):
        lore_files = artifacts.lore_file_count(self.output_dir)
        acts = [s for s in artifacts.structure_sections(self.output_dir)
                if not any(w in s.key.lower() for w in ("arcs", "reconciled"))]
        filled = [s for s in acts if s.text.strip()]
        return lore_files, acts, filled

    def refresh(self) -> None:
        lore_files, acts, filled = self._dependency_rows()
        self._plans = artifacts.scene_plans(self.output_dir)
        outlines = artifacts.chapter_outlines(self.output_dir)

        blocked = (lore_files == 0) or (not acts) or (len(filled) < len(acts))

        # --- 依赖清单 ---
        self.clear_layout(self._deps_layout)
        for text, state in [
            (f"世界设定 · {lore_files} 个文件", "done" if lore_files else "todo"),
            (f"故事结构 · {len(filled)} / {len(acts) or '?'} 幕有内容",
             "done" if acts and len(filled) == len(acts) else
             ("partial" if acts else "todo")),
        ]:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            color = {"done": theme.SUCCESS, "partial": theme.WARN}.get(state, theme.INK_400)
            icon_name = {"done": "check", "partial": "half"}.get(state, "empty")
            badge = QLabel()
            badge.setPixmap(icons.pixmap(icon_name, color, 14))
            label = QLabel(text)
            label.setStyleSheet(f"font-size: {theme.FS_SM}px; color: {color};")
            row_layout.addWidget(badge)
            row_layout.addWidget(label, 1)
            self._deps_layout.addWidget(row)

        # --- 阻塞原因 ---
        self.clear_layout(self._reasons_layout)
        reasons = []
        if lore_files == 0:
            reasons.append(("世界设定尚未生成", "场景规划需要势力与人物作为输入。"))
        if not acts:
            reasons.append(("故事结构尚未生成", "没有幕就没法切分场景。"))
        for section in acts:
            if not section.text.strip():
                reasons.append((section.title, "该幕文件为空，需要重新生成。"))
        for title, detail in reasons:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(9)
            badge = QLabel()
            badge.setPixmap(icons.pixmap("half", theme.WARN, 15))
            badge.setStyleSheet("border: none;")
            column = QVBoxLayout()
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(2)
            head = QLabel(title)
            head.setStyleSheet(f"font-size: {theme.FS_MD}px; border: none;")
            body = QLabel(detail)
            body.setWordWrap(True)
            body.setStyleSheet(
                f"font-size: {theme.FS_XS}px; color: {theme.INK_600}; border: none;")
            column.addWidget(head)
            column.addWidget(body)
            row_layout.addWidget(badge, 0, Qt.AlignTop)
            row_layout.addLayout(column, 1)
            self._reasons_layout.addWidget(row)

        self.blocked_detail.setText(
            "场景规划以有内容的幕为输入。当前有 "
            f"{len(acts) - len(filled) if acts else '全部'} 幕不可用，"
            "规划出的场景会在下一道门禁处被拒绝。"
        )

        # --- 视图切换 ---
        if blocked and not self._plans:
            self.stack.setCurrentIndex(0)
        else:
            self.stack.setCurrentIndex(1)
            self.plan_list.set_items([
                (plan.title, plan.updated, "done" if plan.text.strip() else "todo", plan.path)
                for plan in self._plans
            ])
            if self._plans and self.plan_list.currentRow() < 0:
                self.plan_list.setCurrentRow(0)

        # --- 检查器 ---
        self.step_buttons["outline"].set_state(
            "done" if outlines else ("locked" if blocked else "todo"),
            f"{len(outlines)} 份" if outlines else ("待解锁" if blocked else ""))
        self.step_buttons["scenes"].set_state(
            "done" if self._plans else ("locked" if blocked else "todo"),
            f"{len(self._plans)} 章" if self._plans else ("待解锁" if blocked else ""))
        done = sum(1 for b in self.step_buttons.values() if b.state == "done")
        self.steps_meta.setText(f"{done} / 2")

        total = len(outlines) + len(self._plans)
        self.files_meta.setText(f"{total} 个文件")
        self.outputs_hint.setText(
            f"章节大纲 {len(outlines)} 份，逐章场景规划 {len(self._plans)} 份。"
            if total else "尚无场景规划产物。解锁后每章生成一份 scene plan。")

        if blocked:
            self.gate.set_content("被阻塞", [f"{t}：{d}" for t, d in reasons], theme.DANGER)
            self.primary.setEnabled(False)
            self.primary.setToolTip("需先完成故事结构；也可在左侧选择强制规划。")
        else:
            self.gate.set_content("门禁已通过", ["输入依赖齐备，可以规划场景。"],
                                  theme.SUCCESS)
            self.primary.setEnabled(True)
            self.primary.setToolTip("")

        self.primary.setText("重新规划场景" if self._plans else "规划场景")
        self.header.set_subtitle(
            f"{len(self._plans)} 章场景 · {len(outlines)} 份章节大纲"
            if total else "被阻塞 · 需先完成故事结构" if blocked else "详细场景大纲")
