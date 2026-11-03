"""Text dialogs and workbench action buttons."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QBoxLayout,
    QDialog,
    QDialogButtonBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.gui.widgets.controls import primary_button

if TYPE_CHECKING:
    from .workbench import Workbench


def create_action_button(
    title: str,
    callback: Callable[[], object],
    layout: QBoxLayout,
    workbench: Workbench,
    *,
    primary: bool = False,
) -> QPushButton:
    widget = primary_button(title) if primary else QPushButton(title)
    widget.clicked.connect(lambda: workbench.perform(callback))
    layout.addWidget(widget)
    return widget


def show_text_dialog(
    parent: QWidget, title: str, value: str, *, readonly: bool = False
) -> str | None:
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.resize(760, 620)
    layout = QVBoxLayout(dialog)
    editor = QPlainTextEdit()
    editor.setPlainText(value)
    editor.setReadOnly(readonly)
    layout.addWidget(editor)
    buttons = QDialogButtonBox(
        QDialogButtonBox.Close if readonly else QDialogButtonBox.Save | QDialogButtonBox.Cancel
    )
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    return editor.toPlainText() if dialog.exec() == QDialog.Accepted else None
