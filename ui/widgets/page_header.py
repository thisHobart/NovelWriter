# -*- coding: utf-8 -*-
"""页头：标题 + 副标题 + 右侧动作区（主按钮永远在最右）。"""
from __future__ import annotations

from typing import Iterable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ui import theme


class PageHeader(QWidget):
    def __init__(self, title: str, subtitle: str = "",
                 actions: Iterable[QWidget] = (),
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Header")
        self.setFixedHeight(theme.HEADER_HEIGHT)
        self.setAttribute(Qt.WA_StyledBackground, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 0, 24, 0)
        layout.setSpacing(theme.SP[4])

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(2)
        self._title = QLabel(title)
        self._title.setObjectName("PageTitle")
        self._subtitle = QLabel(subtitle)
        self._subtitle.setObjectName("Secondary")
        text.addWidget(self._title)
        text.addWidget(self._subtitle)
        layout.addLayout(text)
        layout.addStretch(1)

        self._actions = QHBoxLayout()
        self._actions.setContentsMargins(0, 0, 0, 0)
        self._actions.setSpacing(theme.SP[2])
        for action in actions:
            self._actions.addWidget(action)
        layout.addLayout(self._actions)

    def set_subtitle(self, text: str) -> None:
        self._subtitle.setText(text)

    def set_title(self, text: str) -> None:
        self._title.setText(text)

    def add_action(self, widget: QWidget) -> QWidget:
        self._actions.addWidget(widget)
        return widget
