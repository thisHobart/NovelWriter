"""Application-wide input rules for parameter controls."""

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import (
    QAbstractSlider,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QScrollBar,
)


class ParameterWheelGuard(QObject):
    """Let wheel events scroll containers instead of changing parameters."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            event.type() == QEvent.Type.Wheel
            and isinstance(watched, (QAbstractSpinBox, QComboBox, QAbstractSlider))
            and not isinstance(watched, QScrollBar)
        ):
            event.ignore()
            return True
        return False


def install_parameter_wheel_guard(app: QApplication) -> None:
    """Install once; also covers controls created later in pages and dialogs."""
    if not hasattr(app, "_parameter_wheel_guard"):
        guard = ParameterWheelGuard(app)
        app.installEventFilter(guard)
        app._parameter_wheel_guard = guard
