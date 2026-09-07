# -*- coding: utf-8 -*-
"""阶段 3 · 故事结构。

每一幕一张卡片，未通过评审的幕把意见原文挂在卡片上，而不是塞进对话框。
主按钮文案随状态变化——界面只呈现当前唯一有意义的动作。
规格见 docs/UI_CONTRACT.md 第 7 节。
"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.gui import icons, theme
from core.gui.pages.lore_page import read_only_text
from core.gui.pages.stage_base import StagePage
from core.gui.services import artifacts
from core.gui.services.step_runner import action_work, step_work
from core.gui.widgets import Card, GatePanel, hint_label, primary_button
from core.gui.widgets.inspector import StepButton


class SectionCard(QWidget):
    """一幕：序号 + 名称 + 状态徽标 + 摘要（点击展开全文）。"""

    toggled = Signal(str)

    def __init__(self, index: int, section: artifacts.Section, state: str,
                 review: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.section = section
        self._expanded = False

        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            f"background: {theme.CANVAS}; border: 1px solid {theme.LINE};"
            f"border-radius: 5px;")
        self.setCursor(Qt.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        chip_map = {
            "done": ("#EAF3EE", theme.SUCCESS, "通过", "check"),
            "rejected": ("#FAF2E4", theme.WARN, "未通过", "half"),
            "todo": ("#F2F2EF", theme.INK_400, "未生成", "empty"),
        }
        chip_bg, chip_fg, chip_text, chip_icon = chip_map.get(state, chip_map["todo"])

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(10)

        number = QLabel(f"第 {index} 幕")
        number.setStyleSheet(
            f"font-family: {theme.SERIF_STACK}; font-size: {theme.FS_SM}px;"
            f"color: {theme.INK_400}; border: none;")
        name = QLabel(section.title)
        name.setStyleSheet(
            f"font-family: {theme.SERIF_STACK}; font-size: 15px; font-weight: 600;"
            f"border: none;")

        chip = QLabel(chip_text)
        chip.setStyleSheet(
            f"background: {chip_bg}; color: {chip_fg}; border: none;"
            f"border-radius: 11px; padding: 3px 9px; font-size: {theme.FS_XS}px;")
        chip_icon_label = QLabel()
        chip_icon_label.setPixmap(icons.pixmap(chip_icon, chip_fg, 14))
        chip_icon_label.setStyleSheet("border: none;")

        top.addWidget(number)
        top.addWidget(name, 1)
        top.addWidget(chip_icon_label)
        top.addWidget(chip)
        layout.addLayout(top)

        preview = section.text.strip().replace("\n", " ")
        self._summary = QLabel(preview[:110] + ("…" if len(preview) > 110 else "")
                               or "（该幕文件为空）")
        self._summary.setWordWrap(True)
        self._summary.setStyleSheet(
            f"font-size: {theme.FS_SM}px; color: {theme.INK_600}; border: none;")
        layout.addWidget(self._summary)

        self._full = read_only_text(section.text, serif=True)
        self._full.setMinimumHeight(240)
        self._full.hide()
        layout.addWidget(self._full)

        if review:
            box = QLabel(review)
            box.setWordWrap(True)
            box.setStyleSheet(
                f"background: {theme.SURFACE}; border: 1px solid {theme.LINE};"
                f"border-left: 2px solid {chip_fg}; border-radius: {theme.RADIUS}px;"
                f"padding: 9px 11px; font-size: {theme.FS_XS}px; color: {theme.INK_600};")
            layout.addWidget(box)

        footer = QLabel(f"{section.path}    {section.updated}")
        footer.setObjectName("Tertiary")
        footer.setStyleSheet(
            f"font-size: {theme.FS_XS}px; color: {theme.INK_400}; border: none;")
        layout.addWidget(footer)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.LeftButton:
            self._expanded = not self._expanded
            self._full.setVisible(self._expanded)
            self._summary.setVisible(not self._expanded)
        super().mouseReleaseEvent(event)


class StructurePage(StagePage):
    stage_key = "structure"

    #: 结构小节里，名字含这些词的不算「幕」，单独归到弧光产物
    ARC_KEYS = ("character_arcs", "faction_arcs", "reconciled")

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("故事结构", "情节与故事弧")

        # 单步入口只在右栏「分步生成」里出现一次，页头只放跑完整阶段的主按钮。
        self.primary = primary_button("生成故事结构")
        self.header.add_action(self.primary)
        self.primary.clicked.connect(
            lambda: self._run_stage("full", self.primary))

        self._build_left()
        self._build_inspector()
        self.refresh()

    # ---------------------------------------------------------------- 左侧
    def _build_left(self) -> None:
        card = Card("幕结构", QLabel(""))
        card.body.setContentsMargins(0, 0, 0, 0)

        holder = QWidget()
        self._sections_layout = QVBoxLayout(holder)
        self._sections_layout.setContentsMargins(20, 16, 20, 16)
        self._sections_layout.setSpacing(11)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)
        card.body.addWidget(scroll)
        self.set_left(card)

    # ---------------------------------------------------------------- 右侧
    def _build_inspector(self) -> None:
        gate_card = Card("门禁")
        self.gate = GatePanel()
        gate_card.body.addWidget(self.gate)
        self.add_card(gate_card)

        self.steps_meta = QLabel("")
        self.steps_meta.setObjectName("Tertiary")
        steps = Card("分步生成", self.steps_meta)
        self.step_buttons = {
            "arcs": StepButton("arcs", "生成人物弧光"),
            "faction_arcs": StepButton("faction_arcs", "生成势力弧光"),
            "locations": StepButton("locations", "将地点融入故事弧"),
            "plot": StepButton("plot", "创建详细情节"),
        }
        for button in self.step_buttons.values():
            button.clicked.connect(self._run_stage)
            steps.body.addWidget(button)
        self.add_card(steps)

        review = Card("评审")
        self.review_summary = QLabel("")
        self.review_summary.setWordWrap(True)
        self.review_summary.setObjectName("Secondary")
        review.body.addWidget(self.review_summary)
        self.add_card(review)

        self.finish_inspector()

    # ---------------------------------------------------------------- 行为
    def _run_stage(self, action: str = "full", busy_button=None) -> None:
        labels = {
            "arcs": "正在生成人物弧光…",
            "faction_arcs": "正在生成势力弧光…",
            "locations": "正在融合地点与故事弧…",
            "plot": "正在生成详细情节…",
            "full": "正在生成结构…",
        }
        self.run_task(
            step_work("structure", self.output_dir) if action == "full" else
            action_work("structure", action, self.output_dir),
            busy_button=busy_button or self.step_buttons.get(action, self.primary),
            busy_text=labels[action],
            on_success=lambda _: (self.refresh(), self.artifacts_changed.emit()),
        )

    # ---------------------------------------------------------------- 刷新
    def _split_sections(self):
        acts, arcs = [], []
        for section in artifacts.structure_sections(self.output_dir):
            key = section.key.lower()
            (arcs if any(word in key for word in self.ARC_KEYS) else acts).append(section)
        return acts, arcs

    def refresh(self) -> None:
        acts, arcs = self._split_sections()
        self.clear_layout(self._sections_layout)

        if not acts:
            self._sections_layout.addWidget(
                hint_label("尚未生成幕结构。先完成世界设定，再点右上角「生成故事结构」。"))
        for index, section in enumerate(acts, start=1):
            state = "done" if section.text.strip() else "todo"
            review = "" if state == "done" else "该幕文件为空，需重新生成。"
            self._sections_layout.addWidget(SectionCard(index, section, state, review))
        self._sections_layout.addStretch(1)

        empty = [s.title for s in acts if not s.text.strip()]
        arc_keys = {s.key.lower() for s in arcs}
        self.step_buttons["arcs"].set_state(
            "done" if any("character_arcs" in k for k in arc_keys) else "todo")
        self.step_buttons["faction_arcs"].set_state(
            "done" if any("faction_arcs" in k for k in arc_keys) else "todo")
        self.step_buttons["locations"].set_state(
            "done" if any("reconciled" in k for k in arc_keys) else "todo")
        self.step_buttons["plot"].set_state(
            "done" if acts and not empty else ("todo" if not acts else "running"),
            f"{len(acts)} 幕" if acts else "")
        if empty:
            self.step_buttons["plot"].set_state("todo", f"{len(empty)} 幕待重跑")

        done = sum(1 for b in self.step_buttons.values() if b.state == "done")
        self.steps_meta.setText(f"{done} / 4 已完成")

        if not acts:
            self.gate.set_content("下一阶段被阻塞", ["尚未生成任何幕结构。"], theme.DANGER)
            self.primary.setText("生成故事结构")
        elif empty:
            self.gate.set_content(
                "下一阶段被阻塞",
                [f"{title}：文件为空。" for title in empty]
                + ["场景规划会在门禁处拒绝空的幕，重跑后自动解锁。"],
                theme.WARN,
            )
            self.primary.setText(f"重跑未完成的 {len(empty)} 幕")
        else:
            self.gate.set_content("门禁已通过", ["所有幕都有内容，可以进入场景规划。"],
                                  theme.SUCCESS)
            self.primary.setText("重新生成故事结构")

        self.review_summary.setText(
            f"共 {len(acts)} 幕，{len(acts) - len(empty)} 幕有内容。"
            f"弧光产物 {len(arcs)} 份。评审记录在 quality/reviews 下。"
            if acts else "尚无评审记录。"
        )
        self.header.set_subtitle(
            f"{len(acts)} 幕 · {len(acts) - len(empty)} 幕有内容" if acts else "情节与故事弧")
