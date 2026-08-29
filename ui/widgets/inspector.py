# -*- coding: utf-8 -*-
"""右栏检查器里的几个复用块：分步按钮、门禁面板、产物列表。"""
from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui import icons, theme


class StepButton(QWidget):
    """一行「状态图标 + 名称 + 计数」的分步生成按钮。

    按钮本身就是状态显示，不再另起一行说明文字。
    state: done / running / locked / todo
    """

    clicked = Signal(str)

    def __init__(self, key: str, label: str, meta: str = "",
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.key = key
        self._state = "todo"

        self.setFixedHeight(34)
        self.setAttribute(Qt.WA_StyledBackground, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(8)

        self._icon = QLabel()
        self._icon.setFixedSize(14, 14)
        self._label = QLabel(label)
        self._meta = QLabel(meta)
        self._meta.setObjectName("Tertiary")

        layout.addWidget(self._icon)
        layout.addWidget(self._label, 1)
        layout.addWidget(self._meta)
        self.set_state("todo", meta)

    def set_state(self, state: str, meta: Optional[str] = None) -> None:
        self._state = state
        palette = {
            "done": ("check", theme.SUCCESS, theme.INK_900, theme.LINE_STRONG, theme.CANVAS),
            "running": ("spinner", theme.ACCENT, theme.ACCENT, theme.ACCENT, theme.ACCENT_TINT),
            "locked": ("lock", theme.INK_400, theme.INK_400, theme.LINE, theme.SURFACE),
            "todo": ("empty", theme.INK_400, theme.INK_900, theme.LINE_STRONG, theme.CANVAS),
        }
        icon_name, icon_color, text_color, border, background = palette.get(
            state, palette["todo"])
        self._icon.setPixmap(icons.pixmap(icon_name, icon_color, 14))
        self._label.setStyleSheet(f"font-size: {theme.FS_MD}px; color: {text_color};")
        self.setStyleSheet(
            f"background: {background}; border: 1px solid {border};"
            f"border-radius: {theme.RADIUS}px;"
        )
        self.setCursor(Qt.ForbiddenCursor if state in ("locked", "running")
                       else Qt.PointingHandCursor)
        if meta is not None:
            self._meta.setText(meta)

    @property
    def state(self) -> str:
        return self._state

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.LeftButton and self._state not in ("locked", "running"):
            self.clicked.emit(self.key)
        super().mouseReleaseEvent(event)


class GatePanel(QWidget):
    """门禁说明：为什么下一阶段被锁、怎么解开。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(10)

        self._title = QLabel()
        self._title_row = QHBoxLayout()
        self._title_row.setContentsMargins(0, 0, 0, 0)
        self._title_row.setSpacing(7)
        self._title_icon = QLabel()
        self._title_icon.setFixedSize(15, 15)
        self._title_row.addWidget(self._title_icon)
        self._title_row.addWidget(self._title, 1)
        self._layout.addLayout(self._title_row)

        self._items = QVBoxLayout()
        self._items.setContentsMargins(0, 0, 0, 0)
        self._items.setSpacing(6)
        self._layout.addLayout(self._items)

    def set_content(self, title: str, lines: Iterable[str],
                    tone: str = theme.WARN) -> None:
        self._title_icon.setPixmap(icons.pixmap(
            "half" if tone != theme.SUCCESS else "check", tone, 15))
        self._title.setText(title)
        self._title.setStyleSheet(
            f"font-size: {theme.FS_SM}px; font-weight: 500; color: {tone};")

        while self._items.count():
            item = self._items.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for line in lines:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            dot = QLabel()
            dot.setFixedSize(4, 4)
            dot.setStyleSheet(f"background: {tone}; border-radius: 2px;")
            text = QLabel(line)
            text.setWordWrap(True)
            text.setObjectName("Secondary")
            row_layout.addWidget(dot, 0, Qt.AlignTop)
            row_layout.addWidget(text, 1)
            self._items.addWidget(row)


class ArtifactList(QListWidget):
    """产物列表：左侧图标表示状态，双击回调打开。"""

    opened = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QListWidget.NoFrame)
        self.setStyleSheet(
            f"QListWidget {{ background: transparent; }}"
            f"QListWidget::item {{ height: 32px; padding: 0 8px;"
            f" border-radius: {theme.RADIUS}px; }}"
            f"QListWidget::item:selected {{ background: {theme.ACCENT_TINT};"
            f" color: {theme.INK_900}; }}"
        )
        self.itemDoubleClicked.connect(
            lambda item: self.opened.emit(item.data(Qt.UserRole) or ""))

    def set_items(self, rows: List[Tuple[str, str, str, str]]) -> None:
        """rows: (显示名, 右侧元信息, 状态, 关联数据)"""
        self.clear()
        for label, meta, state, payload in rows:
            item = QListWidgetItem(f"{label}    {meta}" if meta else label)
            item.setIcon(icons.icon(*_state_icon(state), 14))
            item.setData(Qt.UserRole, payload)
            self.addItem(item)


def _state_icon(state: str) -> Tuple[str, str]:
    return {
        "done": ("check", theme.SUCCESS),
        "running": ("spinner", theme.ACCENT),
        "todo": ("empty", theme.INK_400),
        "locked": ("lock", theme.INK_400),
    }.get(state, ("empty", theme.INK_400))
