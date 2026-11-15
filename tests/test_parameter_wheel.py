"""Parameter wheels must scroll the page without changing focused controls."""

import os
from collections.abc import Iterator

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QComboBox,
    QDial,
    QDoubleSpinBox,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.gui.input_behavior import install_parameter_wheel_guard  # noqa: E402


@pytest.fixture
def app() -> Iterator[QApplication]:
    app = QApplication.instance() or QApplication([])
    install_parameter_wheel_guard(app)
    guard = app._parameter_wheel_guard
    install_parameter_wheel_guard(app)
    assert app._parameter_wheel_guard is guard
    yield app
    app.removeEventFilter(guard)
    del app._parameter_wheel_guard
    guard.deleteLater()


@pytest.mark.parametrize("kind", [QSpinBox, QDoubleSpinBox, QComboBox, QSlider, QDial])
@pytest.mark.parametrize("focused", [False, True])
def test_wheel_preserves_parameters_and_scrolls_parent(
    app: QApplication,
    kind: type[QSpinBox | QDoubleSpinBox | QComboBox | QSlider | QDial],
    focused: bool,
) -> None:
    area = QScrollArea()
    area.setWidgetResizable(True)
    content = QWidget()
    content.setMinimumHeight(1200)
    layout = QVBoxLayout(content)
    control = kind()
    control.setFixedHeight(40)
    if isinstance(control, QComboBox):
        control.addItems(["模型 A", "模型 B", "模型 C"])
        control.setCurrentIndex(1)
        value = control.currentIndex
    else:
        control.setRange(0, 100)
        control.setValue(50)
        value = control.value
    layout.addWidget(control)
    layout.addStretch()
    area.setWidget(content)
    area.resize(400, 250)
    area.show()
    area.activateWindow()
    (control if focused else area).setFocus()
    app.processEvents()
    before = value()
    position = control.mapTo(area, control.rect().center())
    QTest.wheelEvent(area.windowHandle(), position, QPoint(0, -120))
    app.processEvents()
    assert value() == before
    assert area.verticalScrollBar().value() > 0

    # Keyboard changes remain available after blocking the wheel.
    control.setFocus()
    QTest.keyClick(control, Qt.Key.Key_Up)
    assert value() != before
    area.close()


def test_editable_combo_and_popup_keep_normal_input_and_scrolling(app: QApplication) -> None:
    combo = QComboBox()
    combo.setEditable(True)
    combo.addItems([f"模型 {i}" for i in range(80)])
    combo.setCurrentIndex(40)
    combo.show()
    combo.activateWindow()
    combo.lineEdit().setFocus()
    app.processEvents()
    QTest.wheelEvent(combo.windowHandle(), combo.rect().center(), QPoint(0, -120))
    assert combo.currentIndex() == 40
    combo.lineEdit().selectAll()
    QTest.keyClicks(combo.lineEdit(), "custom-model")
    assert combo.currentText() == "custom-model"

    combo.showPopup()
    app.processEvents()
    view = combo.view()
    scrollbar = view.verticalScrollBar()
    scrollbar.setValue(0)
    popup = view.window()
    position = view.viewport().mapTo(popup, view.viewport().rect().center())
    QTest.wheelEvent(popup.windowHandle(), position, QPoint(0, -120))
    app.processEvents()
    assert scrollbar.value() > 0
    combo.hidePopup()
    combo.close()
