# -*- coding: utf-8 -*-
"""PySide6 界面入口：``python -m core.gui.app`` 或 ``python main.py``。"""
from __future__ import annotations

import logging
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from core.gui import theme
from core.gui.main_window import MainWindow


def _pick_ui_font() -> QFont:
    """创建项目统一字体，不维护额外的应用级回退链。"""
    font = QFont("Microsoft YaHei UI", 10)
    font.setHintingPreference(QFont.PreferFullHinting)
    font.setStyleStrategy(QFont.PreferAntialias | QFont.PreferQuality)
    return font


def create_app(argv: list[str] | None = None) -> tuple[QApplication, MainWindow]:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("NovelWriter")
    app.setFont(_pick_ui_font())
    app.setStyleSheet(theme.build_qss())
    window = MainWindow()
    return app, window


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app, window = create_app()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
