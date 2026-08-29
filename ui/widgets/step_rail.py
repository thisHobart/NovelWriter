# -*- coding: utf-8 -*-
"""左侧流程步骤栏。

取代旧版的 ttk.Notebook：五个阶段有依赖顺序，Tab 的「平级并列」语义是错的。
每行直接呈现 workflow_status 的 COMPLETE / PARTIAL / BLOCKED，被阻塞的阶段
带锁形图标并把阻塞原因常驻显示（旧版只在弹窗里出现）。
规格见 docs/UI_CONTRACT.md 第 1.1 节。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui import icons, theme
from ui.widgets.card import hline


@dataclass
class StepState:
    """一个阶段在步骤栏上的完整可视状态。"""

    state: str = "idle"        # complete / partial / blocked / running / idle
    detail: str = "未开始"      # 副文案；blocked 时应为阻塞原因原文
    clickable: bool = True


class StepRow(QWidget):
    clicked = Signal(str)

    def __init__(self, key: str, index: int, name: str,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.key = key
        self._active = False
        self._state = StepState()

        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_StyledBackground, True)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(14, 9, 12, 9)
        outer.setSpacing(10)

        self._bar = QWidget()
        self._bar.setFixedWidth(2)
        outer.insertWidget(0, self._bar)

        self._num = QLabel(str(index))
        self._num.setFixedWidth(14)
        self._num.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._num.setStyleSheet(
            f"font-family: {theme.SERIF_STACK}; font-size: {theme.FS_SM}px;"
            f"color: {theme.INK_400};"
        )
        outer.addWidget(self._num, 0, Qt.AlignTop)

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(3)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        self._icon = QLabel()
        self._icon.setFixedSize(16, 16)
        self._name = QLabel(name)
        top.addWidget(self._icon)
        top.addWidget(self._name)
        top.addStretch(1)
        column.addLayout(top)

        self._detail = QLabel()
        self._detail.setWordWrap(True)
        self._detail.setContentsMargins(22, 0, 0, 0)
        column.addWidget(self._detail)

        outer.addLayout(column, 1)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self._repaint()

    # -- 对外 --------------------------------------------------------------
    def set_state(self, state: StepState) -> None:
        self._state = state
        self._repaint()

    def set_active(self, active: bool) -> None:
        self._active = active
        self._repaint()

    # -- 内部 --------------------------------------------------------------
    def _repaint(self) -> None:
        state = self._state
        color = theme.STATE_COLORS.get(state.state, theme.INK_400)
        self._icon.setPixmap(icons.state_pixmap(state.state, 16))

        if self._active:
            bg, border = theme.CANVAS, theme.LINE
            weight, name_color = "600", theme.INK_900
            self._bar.setStyleSheet(f"background: {theme.ACCENT}; border-radius: 1px;")
        else:
            bg, border = "transparent", "transparent"
            weight = "400"
            name_color = theme.INK_400 if state.state == "blocked" else theme.INK_600
            self._bar.setStyleSheet("background: transparent;")

        self.setStyleSheet(
            f"background: {bg}; border: 1px solid {border};"
            f"border-radius: 5px;"
        )
        self._name.setStyleSheet(
            f"font-size: {theme.FS_MD}px; font-weight: {weight}; color: {name_color};"
        )
        self._detail.setStyleSheet(f"font-size: {theme.FS_XS}px; color: {color};")
        self._detail.setText(state.detail)
        self.setCursor(Qt.PointingHandCursor if state.clickable else Qt.ForbiddenCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.key)
        super().mouseReleaseEvent(event)


class OverviewRow(QWidget):
    """侧栏顶部的「工作流总览」入口。它不是流程的一步，所以不带序号。"""

    clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._active = False
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_StyledBackground, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 8, 12, 8)
        layout.setSpacing(9)

        self._icon = QLabel()
        self._icon.setFixedSize(15, 15)
        self._icon.setPixmap(icons.pixmap("doc", theme.INK_600, 15))
        self._label = QLabel("工作流总览")
        layout.addWidget(self._icon)
        layout.addWidget(self._label, 1)
        self.set_active(False)

    def set_active(self, active: bool) -> None:
        self._active = active
        background = theme.CANVAS if active else "transparent"
        border = theme.LINE if active else "transparent"
        weight = "600" if active else "400"
        color = theme.INK_900 if active else theme.INK_600
        self.setStyleSheet(
            f"background: {background}; border: 1px solid {border};"
            f"border-radius: 5px;"
        )
        self._label.setStyleSheet(
            f"font-size: {theme.FS_MD}px; font-weight: {weight}; color: {color};")
        self._icon.setPixmap(icons.pixmap("doc", color, 15))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class StepRail(QWidget):
    """完整侧栏：应用名 + 作品名 + 步骤列表 + 底部全局设置。"""

    step_selected = Signal(str)
    backend_changed = Signal(str)
    model_changed = Signal(str)

    STEPS = [
        ("parameters", "作品参数"),
        ("lore", "世界设定"),
        ("structure", "故事结构"),
        ("scenes", "场景规划"),
        ("chapters", "章节撰写"),
    ]

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Rail")
        self.setFixedWidth(theme.RAIL_WIDTH)
        self.setAttribute(Qt.WA_StyledBackground, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 顶部标题
        head = QWidget()
        head_layout = QVBoxLayout(head)
        head_layout.setContentsMargins(18, 20, 18, 16)
        head_layout.setSpacing(3)
        app_name = QLabel("NovelWriter")
        app_name.setObjectName("AppName")
        self._work_title = QLabel("未命名作品")
        self._work_title.setObjectName("Secondary")
        head_layout.addWidget(app_name)
        head_layout.addWidget(self._work_title)
        layout.addWidget(head)
        layout.addWidget(self._padded(hline()))

        # 步骤列表
        steps_widget = QWidget()
        steps_layout = QVBoxLayout(steps_widget)
        steps_layout.setContentsMargins(10, 16, 10, 10)
        steps_layout.setSpacing(2)

        # 总览不属于流程的任何一步，单独放在最上面
        self.overview_row = OverviewRow()
        self.overview_row.clicked.connect(lambda: self.step_selected.emit("workflow"))
        steps_layout.addWidget(self.overview_row)
        steps_layout.addSpacing(10)

        caption = QLabel("创作流程")
        caption.setObjectName("SectionLabel")
        caption.setContentsMargins(14, 0, 0, 8)
        steps_layout.addWidget(caption)

        self.rows: dict[str, StepRow] = {}
        for index, (key, name) in enumerate(self.STEPS, start=1):
            row = StepRow(key, index, name)
            row.clicked.connect(self._on_row_clicked)
            steps_layout.addWidget(row)
            self.rows[key] = row
        steps_layout.addStretch(1)
        layout.addWidget(steps_widget, 1)

        layout.addWidget(self._padded(hline()))

        # 底部全局设置：后端 / 模型 / 密钥状态
        from PySide6.QtWidgets import QComboBox  # 局部导入，避免头部过长

        foot = QWidget()
        foot_layout = QVBoxLayout(foot)
        foot_layout.setContentsMargins(18, 14, 18, 18)
        foot_layout.setSpacing(11)

        self.backend_combo = self._labeled_combo(foot_layout, "运行后端", QComboBox())
        self.model_combo = self._labeled_combo(foot_layout, "模型", QComboBox())
        self.backend_combo.currentTextChanged.connect(self.backend_changed)
        self.model_combo.currentTextChanged.connect(self.model_changed)

        from ui.widgets.controls import IconLabel

        self.key_status = IconLabel("密钥状态未知", "dot", theme.INK_400, 8)
        foot_layout.addWidget(self.key_status)
        layout.addWidget(foot)

    # -- 对外 --------------------------------------------------------------
    def set_work_title(self, title: str) -> None:
        self._work_title.setText(title or "未命名作品")

    def set_step_state(self, key: str, state: StepState) -> None:
        row = self.rows.get(key)
        if row is not None:
            row.set_state(state)

    def set_active(self, key: str) -> None:
        self.overview_row.set_active(key == "workflow")
        for row_key, row in self.rows.items():
            row.set_active(row_key == key)

    def set_key_status(self, ok: bool, text: str) -> None:
        self.key_status.set_state("dot", theme.SUCCESS if ok else theme.WARN, text)

    # -- 内部 --------------------------------------------------------------
    def _on_row_clicked(self, key: str) -> None:
        row = self.rows[key]
        if row._state.clickable:  # noqa: SLF001 - 同模块内的自有状态
            self.step_selected.emit(key)
        else:
            self.step_selected.emit(f"blocked:{key}")

    @staticmethod
    def _padded(widget: QWidget) -> QWidget:
        wrapper = QWidget()
        layout = QHBoxLayout(wrapper)
        layout.setContentsMargins(18, 0, 18, 0)
        layout.addWidget(widget)
        return wrapper

    @staticmethod
    def _labeled_combo(layout: QVBoxLayout, label: str, combo):
        caption = QLabel(label)
        caption.setObjectName("Tertiary")
        combo.setFixedHeight(30)
        layout.addWidget(caption)
        layout.addWidget(combo)
        return combo
