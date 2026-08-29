# -*- coding: utf-8 -*-
"""PySide6 界面入口：python -m ui.app 或 python main.py --qt"""
from __future__ import annotations

import logging
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from ui import theme
from ui.main_window import MainWindow


def _pick_ui_font() -> QFont:
    """按 token 里的字体栈挑第一个系统里真实存在的字族。"""
    from PySide6.QtGui import QFontDatabase

    families = QFontDatabase.families()
    for name in ("Noto Sans SC", "PingFang SC", "Microsoft YaHei", "Segoe UI"):
        if name in families:
            return QFont(name, 10)
    return QFont()


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
