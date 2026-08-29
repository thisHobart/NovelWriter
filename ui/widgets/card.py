# -*- coding: utf-8 -*-
"""卡片容器：可选标题栏 + 内容区。所有页面的分区都用它，保证内边距一致。"""
from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ui import theme


def hline() -> QFrame:
    line = QFrame()
    line.setObjectName("HLine")
    line.setFixedHeight(1)
    return line


def vline(height: int = 16) -> QFrame:
    line = QFrame()
    line.setObjectName("VLine")
    line.setFixedSize(1, height)
    return line


class Card(QFrame):
    """带 1px 边框与 6px 圆角的白底容器。

    body 是一个纵向布局，调用方直接往 card.body 里 addWidget/addLayout。
    """

    def __init__(self, title: str = "", right: Optional[QWidget] = None,
                 padding: tuple[int, int, int, int] = (20, 18, 20, 20),
                 spacing: int = theme.SP[3], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        if title:
            head = QWidget()
            head.setObjectName("CardHeader")
            head_layout = QHBoxLayout(head)
            head_layout.setContentsMargins(20, 14, 20, 14)
            head_layout.setSpacing(theme.SP[3])
            label = QLabel(title)
            label.setObjectName("CardTitle")
            head_layout.addWidget(label)
            head_layout.addStretch(1)
            if right is not None:
                head_layout.addWidget(right)
            outer.addWidget(head)

        body_widget = QWidget()
        self.body = QVBoxLayout(body_widget)
        left, top, rightm, bottom = padding
        self.body.setContentsMargins(left, top, rightm, bottom)
        self.body.setSpacing(spacing)
        outer.addWidget(body_widget, 1)
        self._body_widget = body_widget

    def add(self, widget: QWidget) -> QWidget:
        self.body.addWidget(widget)
        return widget
