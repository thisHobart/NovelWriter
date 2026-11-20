# -*- coding: utf-8 -*-
"""主窗：当前功能的侧栏 + 功能页面 + 底部状态栏。

小说创作和短视频是同一个软件里的两个功能，各管各的页面、任务、保存和模型选择。
主窗只做这几件事：放好两个功能、在它们之间切换、把状态栏的「停止」交给当前功能、
模型服务配置保存后通知两个功能、关窗前确认。主窗认识功能只通过 core.gui.feature。
"""
from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.gui import theme
from core.gui.feature import Feature
from core.gui.pages.novel.workspace import NovelWorkspace
from core.gui.widgets import AppStatusBar


class MainWindow(QMainWindow):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("NovelWriter")
        self.setMinimumSize(*theme.MIN_WINDOW)
        self.resize(1440, 900)

        root = QWidget()
        root.setObjectName("Root")
        root.setAttribute(Qt.WA_StyledBackground, True)
        self._outer = QHBoxLayout(root)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._outer.setSpacing(0)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        self.stack = QStackedWidget()
        right_layout.addWidget(self.stack, 1)
        self.status = AppStatusBar()
        right_layout.addWidget(self.status)
        self._outer.addWidget(right, 1)
        self.setCentralWidget(root)
        self.status.cancel_requested.connect(
            lambda: self.current.cancel_selected(self.status.selected_task())
        )

        self._mode = "novel"
        self.features: Dict[str, Feature] = {}
        self.novel = NovelWorkspace(self)
        self.video = None   # Opened the first time it is asked for.
        self._add_feature("novel", self.novel)
        self.novel.enter()

    @property
    def current(self) -> Feature:
        return self.features[self._mode]

    def _add_feature(self, name: str, feature: Feature) -> None:
        self.features[name] = feature
        self.stack.addWidget(feature)
        if feature.side_panel is not None:
            self._outer.insertWidget(0, feature.side_panel)
            feature.side_panel.setVisible(name == self._mode)

    # ================================================== 功能之间（见 core.gui.feature）
    def open_feature(self, name: str) -> None:
        if name not in {"novel", "video"} or name == self._mode:
            return
        if self.current.busy:
            self.status.show_notice("warning", "任务运行中，暂不能切换模式")
            return
        if not self.current.can_leave():
            return
        self.status.clear_tasks()
        if name == "video" and self.video is None:
            from core.gui.pages.short_video.workbench import Workbench

            # On first use the short video starts with the novel's model service.
            self.video = Workbench.open(self, inherit_model=self.novel.selected_model())
            if self.video is None:
                return
            self._add_feature("video", self.video)
        self._mode = name
        for key, feature in self.features.items():
            if feature.side_panel is not None:
                feature.side_panel.setVisible(key == name)
        self.stack.setCurrentWidget(self.current)
        self.current.enter()

    def model_settings_saved(self) -> None:
        for feature in self.features.values():
            feature.model_settings_changed()

    # ================================================== 关闭
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if not self.current.can_close():
            event.ignore()
            return
        if self.current.busy:
            answer = QMessageBox.question(
                self, "仍有任务在运行", "后台任务尚未结束，确定要退出吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.current.cancel_all()
        event.accept()
