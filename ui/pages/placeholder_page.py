# -*- coding: utf-8 -*-
"""尚未迁移的阶段占位页。

迁移期间旧界面仍在 core/gui/ 下可用；这里明确告诉用户该阶段走哪条路，
而不是给一个空白页。
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ui import icons, theme
from ui.widgets import PageHeader


class PlaceholderPage(QWidget):
    def __init__(self, title: str, subtitle: str, note: str,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = PageHeader(title, subtitle)
        layout.addWidget(self.header)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(24, 24, 24, 24)
        body_layout.addStretch(1)

        icon = QLabel()
        icon.setAlignment(Qt.AlignCenter)
        icon.setPixmap(icons.pixmap("doc", theme.INK_400, 32))
        body_layout.addWidget(icon)

        heading = QLabel("该阶段尚未迁移到新界面")
        heading.setAlignment(Qt.AlignCenter)
        heading.setStyleSheet(
            f"font-family: {theme.SERIF_STACK}; font-size: 18px; font-weight: 600;"
        )
        body_layout.addSpacing(16)
        body_layout.addWidget(heading)

        detail = QLabel(note)
        detail.setAlignment(Qt.AlignCenter)
        detail.setWordWrap(True)
        detail.setObjectName("Secondary")
        detail.setMaximumWidth(520)
        body_layout.addSpacing(8)
        body_layout.addWidget(detail, 0, Qt.AlignHCenter)
        body_layout.addStretch(1)

        layout.addWidget(body, 1)
