"""Combo box whose popup rows carry a × that asks to remove that row."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QModelIndex, QObject, QPointF, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QComboBox,
    QStyleOptionViewItem,
    QWidget,
)

from core.gui import theme

REMOVE_WIDTH = 30


def remove_rect(row: QRect) -> QRect:
    return QRect(row.right() - REMOVE_WIDTH + 1, row.top(), REMOVE_WIDTH, row.height())


class RemovableItemDelegate(QAbstractItemDelegate):
    """Keep the combo's own row painting and draw the × beside the row's text area."""

    def __init__(self, base: QAbstractItemDelegate, combo: RemovableComboBox) -> None:
        super().__init__(combo)
        self.base, self.combo = base, combo
        self.hovered_row = -1

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        if not self.combo.removable(index):
            self.base.paint(painter, option, index)
            return
        text_option = QStyleOptionViewItem(option)
        text_option.rect = option.rect.adjusted(0, 0, -REMOVE_WIDTH, 0)
        self.base.paint(painter, text_option, index)
        area = remove_rect(option.rect)
        hovered = index.row() == self.hovered_row
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        if hovered:
            tint = QColor(theme.DANGER)
            tint.setAlpha(28)
            painter.setPen(Qt.NoPen)
            painter.setBrush(tint)
            painter.drawRoundedRect(area.adjusted(4, 5, -4, -5), 4, 4)
        pen = QPen(QColor(theme.DANGER if hovered else theme.INK_400))
        pen.setWidthF(1.5)
        painter.setPen(pen)
        center, half = QPointF(area.center()) + QPointF(0.5, 0.5), 4.0
        painter.drawLine(center + QPointF(-half, -half), center + QPointF(half, half))
        painter.drawLine(center + QPointF(-half, half), center + QPointF(half, -half))
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        size = self.base.sizeHint(option, index)
        return size + QSize(REMOVE_WIDTH, 0) if self.combo.removable(index) else size


class RemovableComboBox(QComboBox):
    """Rows whose item data is not None show a ×; clicking it emits the data, not a selection.

    The owner removes the row (here or in sibling combos); the popup stays open so several
    entries can be removed in a row.
    """

    remove_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.delegate: RemovableItemDelegate | None = None
        # view() creates the popup and its own filters first, so this filter runs before them.
        view = self.view()
        view.setMouseTracking(True)
        view.viewport().installEventFilter(self)
        self.wrap_delegate()

    def wrap_delegate(self) -> None:
        current = self.view().itemDelegate()
        if current is not self.delegate:
            self.delegate = RemovableItemDelegate(current, self)
            self.view().setItemDelegate(self.delegate)

    def removable(self, index: QModelIndex) -> bool:
        return index.isValid() and index.data(Qt.UserRole) is not None

    def showPopup(self) -> None:
        # A style change can replace the popup delegate; wrap whichever one is current.
        self.wrap_delegate()
        super().showPopup()

    def remove_target(self, position) -> QModelIndex:
        view = self.view()
        index = view.indexAt(position)
        if self.removable(index) and remove_rect(view.visualRect(index)).contains(position):
            return index
        return QModelIndex()

    def set_hovered_row(self, row: int) -> None:
        if self.delegate and self.delegate.hovered_row != row:
            self.delegate.hovered_row = row
            self.view().viewport().update()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is not self.view().viewport():
            return super().eventFilter(watched, event)
        kind = event.type()
        if kind == QEvent.Leave:
            self.set_hovered_row(-1)
        elif kind in (
            QEvent.MouseMove,
            QEvent.MouseButtonPress,
            QEvent.MouseButtonRelease,
            QEvent.MouseButtonDblClick,
        ):
            target = self.remove_target(event.position().toPoint())
            self.set_hovered_row(target.row() if target.isValid() else -1)
            if target.isValid() and kind != QEvent.MouseMove:
                # Swallow press and release so the row is neither selected nor the popup closed.
                if kind == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                    data = target.data(Qt.UserRole)
                    QTimer.singleShot(0, self, lambda: self.request_remove(data))
                return True
        return super().eventFilter(watched, event)

    def request_remove(self, data: object) -> None:
        open_popup = self.view().isVisible()
        count = self.count()
        self.remove_requested.emit(data)
        if open_popup and self.count() != count:
            # Re-open so the popup shrinks to the remaining rows instead of keeping a blank one.
            self.hidePopup()
            self.showPopup()
