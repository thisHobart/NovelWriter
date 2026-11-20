"""Building blocks of the 「镜头」 tab: image slot cards and their grid, the two-column layout,
folding sections, a switch and small status marks."""

from __future__ import annotations

import math

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPixmap, QTransform
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.gui import icons, theme

# A prompt about 300 wide beside the narrowest video card.
TWO_COLUMNS_AT = 680
SLOT_MIN_WIDTH = 148
SLOT_GAP = 10
SLOT_FOOT = 33

WIDGET_STYLE = f"""
#slotCard {{ background:{theme.CANVAS}; border:1px solid {theme.LINE_STRONG};
    border-radius:{theme.RADIUS_CARD}px; }}
#slotCard[empty="true"] {{ background:{theme.SURFACE}; border:1px dashed {theme.LINE_STRONG}; }}
#slotFoot {{ border-top:1px solid {theme.LINE}; }}
#slotCard[empty="true"] #slotFoot {{ border-top:0; }}
QLabel#slotName {{ font-size:{theme.FS_SM}px; }}
QLabel#slotName[empty="true"] {{ color:{theme.INK_400}; }}
QPushButton#mini {{ height:22px; padding:0 7px; font-size:{theme.FS_XS}px; border-radius:3px; }}
QPushButton#addSlot {{ height:auto; border:1px dashed {theme.LINE_STRONG}; border-radius:{theme.RADIUS_CARD}px;
    background:transparent; color:{theme.INK_600}; font-size:{theme.FS_SM}px; }}
QPushButton#addSlot:hover {{ color:{theme.ACCENT}; border-color:{theme.ACCENT}; background:{theme.ACCENT_TINT}; }}
QPushButton#addSlot:disabled {{ color:{theme.INK_400}; border-color:{theme.LINE}; background:transparent; }}
QPushButton#foldHead {{ height:auto; border:0; background:transparent; padding:{theme.SP[2]}px 0;
    text-align:left; }}
QLabel#pill {{ font-size:{theme.FS_XS}px; border-radius:9px; padding:0 8px; background:{theme.RAIL};
    color:{theme.INK_600}; }}
QLabel#pill[tone="ok"] {{ background:{theme.tint(theme.SUCCESS)}; color:{theme.SUCCESS}; }}
QLabel#pill[tone="bad"] {{ background:{theme.tint(theme.DANGER)}; color:{theme.DANGER}; }}
QLabel#pill[tone="run"] {{ background:{theme.ACCENT_TINT}; color:{theme.ACCENT}; }}
QLabel#pill[tone="warn"] {{ background:{theme.tint(theme.WARN)}; color:{theme.WARN}; }}
"""


def repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def set_tone(widget: QWidget, tone: str) -> None:
    if widget.property("tone") != tone:
        widget.setProperty("tone", tone)
        repolish(widget)


def mini_button(text: str = "", icon: str = "") -> QPushButton:
    button = QPushButton(text)
    button.setObjectName("mini")
    button.setFixedHeight(22)
    button.setCursor(Qt.PointingHandCursor)
    if icon:
        button.setIcon(icons.icon(icon, theme.INK_600, 10))
        button.setFixedWidth(22)
    return button


def muted(text: str = "", size: int = theme.FS_SM) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"font-size:{size}px; color:{theme.INK_600}")
    return label


def section_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"font-size:{theme.FS_MD}px; font-weight:600")
    return label


# -- image slots --------------------------------------------------------------------------------


class Thumb(QPushButton):
    """The 16:9 picture of a slot: the image cropped to fill, or an invitation to choose one."""

    def __init__(self) -> None:
        super().__init__()
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet("QPushButton { border:0; background:transparent; padding:0; height:auto; }")
        self.setAttribute(Qt.WA_Hover, True)
        self.source: QPixmap | None = None
        self.scaled: tuple[QSize, QPixmap] | None = None
        self.tag, self.frame_tag = "", False

    def set_picture(self, pixmap: QPixmap | None, tag: str = "", *, frame: bool = False) -> None:
        if pixmap is not self.source:
            self.source, self.scaled = pixmap, None
        self.tag, self.frame_tag = tag, frame
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        clip = QPainterPath()
        # Round only the top corners: the bottom of the rounded rect falls outside the widget.
        clip.addRoundedRect(QRectF(rect.adjusted(0, 0, 0, theme.RADIUS_CARD)), 5, 5)
        painter.setClipPath(clip)
        if self.source is not None and not self.source.isNull():
            ratio = self.devicePixelRatioF()
            if self.scaled is None or self.scaled[0] != rect.size():
                image = self.source.scaled(
                    rect.size() * ratio, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
                )
                image.setDevicePixelRatio(ratio)
                self.scaled = (rect.size(), image)
            image = self.scaled[1]
            x = (image.width() / ratio - rect.width()) / 2
            y = (image.height() / ratio - rect.height()) / 2
            painter.drawPixmap(int(-x), int(-y), image)
        else:
            hovered = self.underMouse() and self.isEnabled()
            painter.fillRect(rect, QColor(theme.ACCENT_TINT if hovered else theme.SURFACE))
            font = QFont(self.font())
            font.setPixelSize(theme.FS_SM)
            painter.setFont(font)
            painter.setPen(QColor(theme.ACCENT if self.isEnabled() else theme.INK_400))
            painter.drawText(rect, Qt.AlignCenter, "＋ 选择图片")
        if self.tag:
            font = QFont(self.font())
            font.setPixelSize(theme.FS_XS)
            painter.setFont(font)
            width = painter.fontMetrics().horizontalAdvance(self.tag) + 12
            pill = QRectF(6, 6, width, 18)
            background = QColor(theme.ACCENT if self.frame_tag else theme.INK_900)
            background.setAlpha(230 if self.frame_tag else 190)
            painter.setPen(Qt.NoPen)
            painter.setBrush(background)
            painter.drawRoundedRect(pill, 3, 3)
            painter.setPen(QColor(theme.CANVAS))
            painter.drawText(pill, Qt.AlignCenter, self.tag)


class SlotCard(QFrame):
    """One image position of the workflow: its picture, <Picture N> tag, name, 换图 and ×."""

    changed = Signal()
    pick_requested = Signal()

    def __init__(self, node_id: str, caption: str, *, frame: bool = False) -> None:
        super().__init__()
        self.node_id, self.caption, self.frame = node_id, caption, frame
        self.image_id: str | None = None
        self.reference = ""
        self.image_name = ""
        self.setObjectName("slotCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        self.thumb = Thumb()
        self.thumb.clicked.connect(self.pick_requested)
        layout.addWidget(self.thumb, 1)
        foot = QWidget()
        foot.setObjectName("slotFoot")
        foot.setAttribute(Qt.WA_StyledBackground, True)
        foot.setFixedHeight(SLOT_FOOT - 2)
        bar = QHBoxLayout(foot)
        bar.setContentsMargins(theme.SP[2], 0, 6, 0)
        bar.setSpacing(theme.SP[1])
        self.name = QLabel()
        self.name.setObjectName("slotName")
        self.name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        bar.addWidget(self.name, 1)
        self.change_button = mini_button("换图")
        self.change_button.setToolTip("从本镜资产、项目图片或文件里换一张")
        self.change_button.clicked.connect(self.pick_requested)
        bar.addWidget(self.change_button)
        self.clear_button = mini_button(icon="close")
        self.clear_button.setToolTip("清空这个位置")
        self.clear_button.clicked.connect(lambda: self.set_image(None))
        bar.addWidget(self.clear_button)
        layout.addWidget(foot)
        self.show_image("", None)

    def set_image(self, image_id: str | None) -> None:
        if image_id != self.image_id:
            self.image_id = image_id
            self.changed.emit()

    def show_image(self, name: str, pixmap: QPixmap | None) -> None:
        self.image_name = name if self.image_id else ""
        empty = self.image_id is None
        self.setProperty("empty", empty)
        self.name.setProperty("empty", empty)
        repolish(self)
        repolish(self.name)
        self.change_button.setVisible(not empty)
        self.clear_button.setVisible(not empty)
        self.thumb.set_picture(None if empty else pixmap, self.tag_text(), frame=self.frame)
        self.elide_name()

    def set_reference(self, reference: str) -> None:
        self.reference = reference
        self.thumb.set_picture(self.thumb.source, self.tag_text(), frame=self.frame)

    def tag_text(self) -> str:
        if self.image_id is None:
            return ""
        return self.caption if self.frame else self.reference

    def summary(self) -> str:
        """How the slot reads: 「图片 2 · <Picture 1>」, 「首帧图片 · 未选」."""
        if self.image_id is None:
            return f"{self.caption} · 未选"
        return f"{self.caption} · {self.reference}" if self.reference and not self.frame else self.caption

    def elide_name(self) -> None:
        text = self.image_name or f"{self.caption} · 未选"
        width = max(20, self.name.width())
        self.name.setText(self.name.fontMetrics().elidedText(text, Qt.ElideRight, width))
        self.name.setToolTip(self.image_name)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self.elide_name()


class AddSlotButton(QPushButton):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("addSlot")
        self.setCursor(Qt.PointingHandCursor)

    def set_room(self, left: int) -> None:
        self.setText("＋ 添加参考图片\n" + (f"还能加 {left} 张" if left else "已到 9 张上限"))


class SlotGrid(QWidget):
    """Cards in as many columns as fit (each at least 148 wide), height following the width."""

    def __init__(self) -> None:
        super().__init__()
        self.cells: list[QWidget] = []
        policy = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def set_cells(self, cells: list[QWidget]) -> None:
        for cell in self.cells:
            if cell not in cells:
                # Hidden rather than detached, so it never shows as a window of its own.
                cell.hide()
                cell.deleteLater()
        self.cells = list(cells)
        for cell in self.cells:
            if cell.parentWidget() is not self:
                cell.setParent(self)
            if not cell.testAttribute(Qt.WA_WState_ExplicitShowHide):
                cell.show()
        self.updateGeometry()
        self.place()

    def visible_cells(self) -> list[QWidget]:
        return [cell for cell in self.cells if not cell.isHidden()]

    @staticmethod
    def metrics(width: int) -> tuple[int, float, int]:
        columns = max(1, (width + SLOT_GAP) // (SLOT_MIN_WIDTH + SLOT_GAP))
        cell_width = (width - SLOT_GAP * (columns - 1)) / columns
        return columns, cell_width, round(cell_width * 9 / 16) + SLOT_FOOT

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt 命名
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt 命名
        count = len(self.visible_cells())
        if not count:
            return 0
        columns, _, cell_height = self.metrics(width)
        rows = math.ceil(count / columns)
        return rows * cell_height + (rows - 1) * SLOT_GAP

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt 命名
        width = 2 * SLOT_MIN_WIDTH + SLOT_GAP
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt 命名
        return QSize(SLOT_MIN_WIDTH, 0)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self.place()

    def place(self) -> None:
        columns, cell_width, cell_height = self.metrics(self.width())
        for index, cell in enumerate(self.visible_cells()):
            row, column = divmod(index, columns)
            left = round(column * (cell_width + SLOT_GAP))
            right = round((column + 1) * (cell_width + SLOT_GAP)) - SLOT_GAP
            cell.setGeometry(left, row * (cell_height + SLOT_GAP), right - left, cell_height)


# -- layout -------------------------------------------------------------------------------------


class ColumnGrid(QWidget):
    """Once the page is 680 wide: images across the top, the prompt and the video card side by
    side below them, image prompts at the bottom. One column below that, with the video card
    after the prompt. The width alone decides, so changing a parameter never moves the card."""

    rearranged = Signal()

    def __init__(self, slots: QWidget, prompt: QWidget, video: QWidget, extra: QWidget) -> None:
        super().__init__()
        self.parts = (slots, prompt, video, extra)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(18)
        self.grid.setVerticalSpacing(18)
        self.wide: bool | None = None
        self.arrange(False)

    def arrange(self, wide: bool) -> None:
        if wide == self.wide:
            return
        self.wide = wide
        slots, prompt, video, extra = self.parts
        for part in self.parts:
            self.grid.removeWidget(part)
        for row in range(5):
            self.grid.setRowStretch(row, 0)
        if wide:
            self.grid.addWidget(slots, 0, 0, 1, 2)
            # The prompt grows to the card's height; the card keeps its own when it is shorter.
            self.grid.addWidget(prompt, 1, 0)
            self.grid.addWidget(video, 1, 1, Qt.AlignTop)
            self.grid.addWidget(extra, 2, 0, 1, 2)
            self.grid.setRowStretch(3, 1)
            self.grid.setColumnStretch(0, 16)
            self.grid.setColumnStretch(1, 10)
            self.grid.setColumnMinimumWidth(1, 320)
        else:
            for row, part in enumerate((slots, prompt, video, extra)):
                self.grid.addWidget(part, row, 0)
            self.grid.setRowStretch(4, 1)
            self.grid.setColumnStretch(0, 1)
            self.grid.setColumnStretch(1, 0)
            self.grid.setColumnMinimumWidth(1, 0)
        self.rearranged.emit()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self.arrange(event.size().width() >= TWO_COLUMNS_AT)


class SplitBody(QWidget):
    """Two parts side by side once this is wide enough, else one under the other."""

    rearranged = Signal()

    def __init__(self, first: QWidget, second: QWidget, at: int) -> None:
        super().__init__()
        self.parts, self.at, self.wide = (first, second), at, None
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(16)
        self.grid.setVerticalSpacing(12)
        self.arrange(False)

    def arrange(self, wide: bool) -> None:
        if wide == self.wide:
            return
        self.wide = wide
        first, second = self.parts
        for part in self.parts:
            self.grid.removeWidget(part)
        if wide:
            self.grid.addWidget(first, 0, 0, Qt.AlignTop)
            self.grid.addWidget(second, 0, 1, Qt.AlignTop)
            self.grid.setColumnStretch(1, 1)
        else:
            self.grid.addWidget(first, 0, 0)
            self.grid.addWidget(second, 1, 0)
            self.grid.setColumnStretch(1, 0)
        self.grid.setColumnStretch(0, 1)
        self.rearranged.emit()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self.arrange(event.size().width() >= self.at)


class Fold(QWidget):
    """A section that opens and closes from its title line."""

    toggled = Signal(bool)

    def __init__(self, title: str, note: str = "", *, small: bool = False) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.head = QPushButton()
        self.head.setObjectName("foldHead")
        self.head.setCursor(Qt.PointingHandCursor)
        head = QHBoxLayout(self.head)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(theme.SP[2])
        self.chevron = QLabel()
        self.title = QLabel(title)
        self.title.setStyleSheet(
            f"font-size:{theme.FS_SM if small else theme.FS_MD}px; font-weight:600"
        )
        self.note = muted(note)
        for part in (self.chevron, self.title, self.note):
            part.setAttribute(Qt.WA_TransparentForMouseEvents)
            head.addWidget(part)
        head.addStretch()
        self.head.setMinimumHeight(30)
        self.head.clicked.connect(lambda: self.set_expanded(self.body.isHidden(), emit=True))
        layout.addWidget(self.head)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, theme.SP[1], 0, theme.SP[2])
        self.body_layout.setSpacing(6)
        layout.addWidget(self.body)
        self.set_expanded(False)

    def expanded(self) -> bool:
        return not self.body.isHidden()

    def set_expanded(self, expanded: bool, *, emit: bool = False) -> None:
        self.body.setVisible(expanded)
        chevron = icons.pixmap("chevron", theme.INK_600, 10)
        if not expanded:
            chevron = chevron.transformed(QTransform().rotate(-90))
        self.chevron.setPixmap(chevron)
        if emit:
            self.toggled.emit(expanded)


class Switch(QCheckBox):
    """On/off parameter drawn as a switch; still a check box for reading and writing. As tall as
    other controls so its row lines up; the track sits in the middle."""

    def __init__(self) -> None:
        super().__init__()
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(36, theme.CONTROL_HEIGHT)

    def hitButton(self, pos) -> bool:  # noqa: N802 - Qt 命名
        return self.rect().contains(pos)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        top = (self.height() - 22) / 2
        track = QRectF(1, top + 1, 34, 20)
        on = self.isChecked()
        color = QColor(theme.ACCENT if on else theme.LINE_STRONG)
        if not self.isEnabled():
            color = QColor(theme.ACCENT_DISABLED if on else theme.LINE)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(track, 10, 10)
        painter.setBrush(QColor(theme.CANVAS))
        painter.drawEllipse(QRectF(17 if on else 3, top + 3, 16, 16))


class Dot(QLabel):
    """Small round state mark before a status sentence."""

    COLORS = {"ok": theme.SUCCESS, "bad": theme.DANGER, "run": theme.ACCENT, "warn": theme.WARN}

    def __init__(self, size: int = 8) -> None:
        super().__init__()
        self.size_ = size
        self.setFixedSize(size, size)
        self.set_tone("")

    def set_tone(self, tone: str) -> None:
        color = self.COLORS.get(tone, theme.INK_400)
        self.setStyleSheet(f"background:{color}; border-radius:{self.size_ // 2}px")
