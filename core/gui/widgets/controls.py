# -*- coding: utf-8 -*-
"""基础控件与工厂函数。

只在这里给控件套 objectName / 动态属性，页面代码不写样式字面值。
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QWidget,
)

from core.gui import icons, theme


# --- 文本 ------------------------------------------------------------------
def _label(text: str, name: str, wrap: bool = False) -> QLabel:
    label = QLabel(text)
    label.setObjectName(name)
    label.setWordWrap(wrap)
    return label


def body_label(text: str, wrap: bool = False) -> QLabel:
    return _label(text, "Body", wrap)


def hint_label(text: str, wrap: bool = True) -> QLabel:
    return _label(text, "Hint", wrap)


def section_label(text: str) -> QLabel:
    return _label(text, "SectionLabel")


class VScrollArea(QScrollArea):
    """纵向滚动区，内容里有自动换行的标签时用它，别用裸的 QScrollArea。

    换行标签的高度取决于宽度，这在 Qt 里叫 heightForWidth。QScrollArea 的
    widgetResizable 不问这个，直接把内容压成视口那么高——几十行的清单会被挤成
    每行几像素，文字互相叠在一起。这里在宽度变化后按当前宽度重算一次内容高度。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet("QScrollArea { background: transparent; }")

    def sync_content_height(self) -> None:
        """内容增删之后调用一次。

        优先问布局，问不出来就逐个问子控件：QBoxLayout 只有在自己重新算过几何
        之后才承认「高度随宽度变」，刚插完控件那一刻它还按空布局回答。
        """
        inner = self.widget()
        layout = inner.layout() if inner is not None else None
        width = self.viewport().width()
        if layout is None or width <= 0:
            return
        layout.invalidate()
        height = layout.heightForWidth(width) if layout.hasHeightForWidth() else -1
        if height < 0:
            margins = layout.contentsMargins()
            height = margins.top() + margins.bottom()
            visible = 0
            for index in range(layout.count()):
                child = layout.itemAt(index).widget()
                if child is None or child.isHidden():
                    continue
                inner_width = width - margins.left() - margins.right()
                own = (
                    child.heightForWidth(inner_width)
                    if child.sizePolicy().hasHeightForWidth()
                    else child.sizeHint().height()
                )
                height += max(own, child.minimumSizeHint().height())
                visible += 1
            height += layout.spacing() * max(0, visible - 1)
        inner.setMinimumHeight(height)

    def setWidget(self, widget: QWidget) -> None:  # noqa: N802 - Qt 命名
        super().setWidget(widget)
        self.sync_content_height()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self.sync_content_height()


# --- 按钮 ------------------------------------------------------------------
def _button(text: str, variant: str, icon_name: Optional[str],
            icon_color: str) -> QPushButton:
    button = QPushButton(text)
    button.setProperty("variant", variant)
    button.setCursor(Qt.PointingHandCursor)
    if icon_name:
        button.setIcon(icons.icon(icon_name, icon_color, 15))
    button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
    return button


def primary_button(text: str, icon_name: Optional[str] = None) -> QPushButton:
    return _button(text, "primary", icon_name, theme.CANVAS)


def secondary_button(text: str, icon_name: Optional[str] = None) -> QPushButton:
    return _button(text, "secondary", icon_name, theme.INK_600)


def danger_button(text: str, icon_name: Optional[str] = None) -> QPushButton:
    return _button(text, "danger", icon_name, theme.CANVAS)


# --- 图标 + 文字 ------------------------------------------------------------
class IconLabel(QWidget):
    """一行「图标 + 文字」，状态提示的通用形态。"""

    def __init__(self, text: str = "", icon_name: str = "dot",
                 color: str = theme.INK_600, size: int = 14,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        self._icon = QLabel()
        self._icon.setFixedSize(size, size)
        self._text = QLabel(text)
        layout.addWidget(self._icon)
        layout.addWidget(self._text)
        self._size = size
        self.set_state(icon_name, color)

    def set_state(self, icon_name: str, color: str, text: Optional[str] = None) -> None:
        self._icon.setPixmap(icons.pixmap(icon_name, color, self._size))
        self._text.setStyleSheet(f"color: {color}; font-size: {theme.FS_SM}px;")
        if text is not None:
            self._text.setText(text)

    def set_text(self, text: str) -> None:
        self._text.setText(text)


# --- 表单行 ----------------------------------------------------------------
class LabeledRow:
    """把「右对齐标签 + 控件 + 可选说明」按同一栅格排进 QGridLayout。"""

    LABEL_WIDTH = 96

    def __init__(self, grid: QGridLayout) -> None:
        self._grid = grid
        self._row = 0
        grid.setColumnMinimumWidth(0, self.LABEL_WIDTH)
        grid.setColumnStretch(1, 1)
        grid.setHorizontalSpacing(theme.SP[3])
        grid.setVerticalSpacing(theme.SP[2])

    def add(self, label: str, widget: QWidget, hint: str = "") -> QWidget:
        text = QLabel(label)
        text.setObjectName("FieldLabel")
        text.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        text.setFixedWidth(self.LABEL_WIDTH)
        self._grid.addWidget(text, self._row, 0)
        self._grid.addWidget(widget, self._row, 1)
        self._row += 1
        if hint:
            self._grid.addWidget(hint_label(hint), self._row, 1)
            self._row += 1
        return widget

    def add_full(self, widget: QWidget) -> QWidget:
        self._grid.addWidget(widget, self._row, 0, 1, 2)
        self._row += 1
        return widget


# --- 数量步进器 -------------------------------------------------------------
class Stepper(QWidget):
    """整数步进器（势力数量、人物数量等）。上下限由调用方给定。"""

    valueChanged = Signal(int)

    def __init__(self, value: int = 1, minimum: int = 1, maximum: int = 10,
                 width: int = 120, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._value = value
        self._min = minimum
        self._max = maximum

        self.setFixedSize(width, theme.CONTROL_HEIGHT)
        self.setStyleSheet(
            f"background: {theme.CANVAS}; border: 1px solid {theme.LINE};"
            f"border-radius: {theme.RADIUS}px;"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)

        self._minus = self._make_step("−", -1)
        self._display = QLabel(str(value))
        self._display.setAlignment(Qt.AlignCenter)
        self._display.setStyleSheet("border: none;")
        self._plus = self._make_step("+", 1)

        layout.addWidget(self._minus)
        layout.addWidget(self._display, 1)
        layout.addWidget(self._plus)
        self._sync()

    def _make_step(self, glyph: str, delta: int) -> QPushButton:
        button = QPushButton(glyph)
        button.setProperty("variant", "ghost")
        button.setFixedSize(28, theme.CONTROL_HEIGHT - 2)
        button.setCursor(Qt.PointingHandCursor)
        button.setStyleSheet(
            f"border: none; background: transparent; color: {theme.INK_600};"
            f"font-size: 15px;"
        )
        button.clicked.connect(lambda: self.set_value(self._value + delta))
        return button

    def value(self) -> int:
        return self._value

    def set_value(self, value: int) -> None:
        value = max(self._min, min(self._max, int(value)))
        if value == self._value:
            return
        self._value = value
        self._display.setText(str(value))
        self._sync()
        self.valueChanged.emit(value)

    def _sync(self) -> None:
        self._minus.setEnabled(self._value > self._min)
        self._plus.setEnabled(self._value < self._max)
