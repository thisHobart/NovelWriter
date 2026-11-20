"""Top of the 「镜头」 tab: a chip per storyboard shot, then the chosen shot's length, action and
check result."""

from __future__ import annotations

from html import escape

from PySide6.QtCore import QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QTextLayout, QTransform
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.gui import icons, theme

from .video_widgets import muted

MARK_COLORS = {"ok": theme.SUCCESS, "bad": theme.DANGER, "run": theme.ACCENT, "warn": theme.WARN}
MARK_NOTES = {"ok": "已有视频", "bad": "生成失败", "run": "正在生成", "warn": "待确认"}
CHIP_GAP = 4
CHIP_HEIGHT = 30

HEADER_STYLE = f"""
QPushButton#shotChip {{ min-width:40px; height:{CHIP_HEIGHT}px; padding:0 8px; border:1px solid {theme.LINE_STRONG};
    border-radius:4px; background:{theme.CANVAS}; color:{theme.INK_900}; font-size:{theme.FS_SM}px; }}
QPushButton#shotChip:hover {{ background:{theme.SURFACE}; }}
QPushButton#shotChip:checked {{ background:{theme.ACCENT}; border-color:{theme.ACCENT}; color:white;
    font-weight:600; }}
#shotHead {{ background:{theme.CANVAS}; border:1px solid {theme.LINE}; border-radius:{theme.RADIUS_CARD}px; }}
#issueButton {{ border-radius:4px; background:{theme.tint(theme.WARN)}; }}
#issueButton:hover {{ background:{theme.tint(theme.WARN, 46)}; }}
#issueButton:focus {{ border:1px solid {theme.WARN}; }}
QLabel#shotNotice {{ font-size:{theme.FS_SM}px; color:{theme.INK_900}; background:{theme.tint(theme.WARN)};
    border-radius:4px; padding:6px 8px; }}
"""


class ShotChip(QPushButton):
    """「01」 with a small mark in the corner when the shot has a video (green) or a failure (red)."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.setObjectName("shotChip")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.mark = ""

    def set_mark(self, mark: str) -> None:
        if mark != self.mark:
            self.mark = mark
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().paintEvent(event)
        if not self.mark:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QColor(theme.CANVAS))
        painter.setBrush(QColor(MARK_COLORS[self.mark]))
        painter.drawEllipse(QRectF(self.width() - 10, 3, 7, 7))


class ShotChips(QWidget):
    """One chip per shot, left to right, going on to the next row when the shots do not fit."""

    chosen = Signal(int)
    reflowed = Signal()  # the number of rows changed

    def __init__(self) -> None:
        super().__init__()
        policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.group.idClicked.connect(self.chosen)
        self.chips: list[ShotChip] = []
        self.rows = 0

    def set_count(self, count: int) -> None:
        if count == len(self.chips):
            return
        while len(self.chips) > count:
            chip = self.chips.pop()
            self.group.removeButton(chip)
            # Hidden rather than detached, so it never shows as a window of its own.
            chip.hide()
            chip.deleteLater()
        while len(self.chips) < count:
            chip = ShotChip(f"{len(self.chips) + 1:02}")
            chip.setParent(self)
            chip.show()
            self.group.addButton(chip, len(self.chips))
            self.chips.append(chip)
        self.updateGeometry()
        self.place()

    def set_current(self, index: int) -> None:
        if 0 <= index < len(self.chips):
            self.chips[index].setChecked(True)

    def positions(self, width: int) -> list[QRect]:
        boxes, x, y = [], 0, 0
        for chip in self.chips:
            size = chip.sizeHint()
            if x and x + size.width() > width:
                x, y = 0, y + size.height() + CHIP_GAP
            boxes.append(QRect(x, y, size.width(), size.height()))
            x += size.width() + CHIP_GAP
        return boxes

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt 命名
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt 命名
        boxes = self.positions(width)
        return boxes[-1].bottom() + 1 if boxes else 0

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt 命名
        boxes = self.positions(2**20)
        return QSize(boxes[-1].right() + 1, boxes[-1].height()) if boxes else QSize(0, 0)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt 命名
        widest = max((chip.sizeHint().width() for chip in self.chips), default=0)
        return QSize(widest, self.heightForWidth(widest))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self.place()

    def place(self) -> None:
        boxes = self.positions(self.width())
        for chip, box in zip(self.chips, boxes, strict=True):
            chip.setGeometry(box)
        rows = len({box.top() for box in boxes})
        if rows != self.rows:
            self.rows = rows
            self.reflowed.emit()

    def set_marks(self, marks: list[str]) -> None:
        for number, (chip, mark) in enumerate(zip(self.chips, marks, strict=False), 1):
            chip.set_mark(mark)
            note = MARK_NOTES.get(mark)
            chip.setToolTip(f"镜头 {number:02}" + (f" · {note}" if note else ""))


class IssueButton(QWidget):
    """「⚠ 检查发现 N 项问题 ⌄」: sized by its parts, which a push button with a layout is not."""

    clicked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("issueButton")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_Hover, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setFixedHeight(22)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self.clicked.emit()
        else:
            super().keyPressEvent(event)


class ClampLabel(QLabel):
    """Wrapped text cut to a number of lines, the last ending in an ellipsis; full text on hover."""

    def __init__(self, lines: int = 2) -> None:
        super().__init__()
        self.lines, self.full = lines, ""
        self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def set_full_text(self, text: str) -> None:
        self.full = " ".join(text.split())
        self.setToolTip(self.full)
        self.clamp()

    def clamp(self) -> None:
        width = self.contentsRect().width()
        if width <= 0 or not self.full:
            self.setText(self.full)
            return
        layout = QTextLayout(self.full, self.font())
        layout.beginLayout()
        starts = []
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            starts.append(line.textStart())
        layout.endLayout()
        if len(starts) <= self.lines:
            self.setText(self.full)
            return
        last = starts[self.lines - 1]
        tail = self.fontMetrics().elidedText(self.full[last:], Qt.ElideRight, width)
        self.setText(self.full[:last] + "\n" + tail)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self.clamp()


class ShotHeader(QWidget):
    """Shot chips above a card with 「镜头 01」, 「分镜 N 秒」, the action and the check line."""

    chosen = Signal(int)
    resized = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setStyleSheet(HEADER_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 16)
        layout.setSpacing(12)
        bar = QHBoxLayout()
        bar.setSpacing(10)
        self.chips = ShotChips()
        self.chips.chosen.connect(self.chosen)
        self.chips.reflowed.connect(self.resized)
        # Level with the first row of chips when they take more than one.
        label = muted("镜头")
        label.setFixedHeight(CHIP_HEIGHT + 2)  # the chip's 1px border above and below
        bar.addWidget(label, 0, Qt.AlignTop)
        bar.addWidget(self.chips, 1)
        layout.addLayout(bar)

        self.card = QFrame()
        self.card.setObjectName("shotHead")
        card = QVBoxLayout(self.card)
        card.setContentsMargins(14, 12, 14, 12)
        card.setSpacing(4)
        title = QHBoxLayout()
        title.setSpacing(8)
        self.title = QLabel()
        self.title.setStyleSheet(f"font-size:{theme.FS_LG}px; font-weight:600")
        title.addWidget(self.title)
        self.duration = muted()
        title.addWidget(self.duration)
        title.addStretch()
        card.addLayout(title)
        self.action = ClampLabel()
        self.action.setStyleSheet(f"font-size:{theme.FS_SM}px; color:{theme.INK_600}")
        card.addWidget(self.action)
        self.check = QWidget()
        check = QHBoxLayout(self.check)
        check.setContentsMargins(0, 2, 0, 0)
        check.setSpacing(8)
        self.saved = muted("已保存")
        check.addWidget(self.saved)
        self.result = QLabel()
        self.result.setStyleSheet(f"font-size:{theme.FS_SM}px")
        check.addWidget(self.result)
        self.issue_button = IssueButton()
        self.issue_button.clicked.connect(lambda: self.show_issues(self.issues.isHidden()))
        inside = QHBoxLayout(self.issue_button)
        inside.setContentsMargins(8, 0, 8, 0)
        inside.setSpacing(5)
        warn = QLabel()
        warn.setPixmap(icons.pixmap("warn", theme.WARN, 14))
        self.issue_text = QLabel()
        self.issue_text.setStyleSheet(
            f"font-size:{theme.FS_SM}px; color:{theme.WARN}; background:transparent"
        )
        self.chevron = QLabel()
        for part in (warn, self.issue_text, self.chevron):
            part.setAttribute(Qt.WA_TransparentForMouseEvents)
            inside.addWidget(part)
        check.addWidget(self.issue_button)
        check.addStretch()
        card.addWidget(self.check)
        self.stale = QLabel()
        self.stale.setWordWrap(True)
        self.stale.setStyleSheet(f"font-size:{theme.FS_SM}px; color:{theme.WARN}")
        card.addWidget(self.stale)
        self.issues = QLabel()
        self.issues.setWordWrap(True)
        self.issues.setTextFormat(Qt.RichText)
        self.issues.setStyleSheet(f"font-size:{theme.FS_SM}px; color:{theme.INK_900}")
        self.issues.hide()
        card.addWidget(self.issues)
        self.notice = QLabel()
        self.notice.setObjectName("shotNotice")
        self.notice.setWordWrap(True)
        self.notice.hide()
        card.addWidget(self.notice)
        layout.addWidget(self.card)
        self.issue_key = None

    def set_shot(self, number: int, duration_ms: int | None, action: str) -> None:
        self.title.setText(f"镜头 {number:02}")
        self.duration.setText(f"分镜 {duration_ms / 1000:g} 秒" if duration_ms else "")
        self.action.set_full_text(action)
        self.action.setVisible(bool(action))

    def set_check(self, check: dict | None) -> None:
        """「已保存 · 检查通过」, or a 「检查发现 N 项问题」 button that opens the list."""
        self.check.setVisible(check is not None)
        check = check or {"status": "", "summary": "", "issues": [], "stale": []}
        status = check["status"]
        has_issues = status == "issues"
        self.issue_button.setVisible(has_issues)
        self.result.setVisible(not has_issues)
        color = {"passed": theme.SUCCESS, "other_shots": theme.INK_600}.get(status, theme.INK_600)
        self.result.setText(f"· <span style='color:{color}'>{escape(check['summary'])}</span>")
        self.issue_text.setText(check["summary"])
        items = "".join(f"<li>{escape(text)}</li>" for text in check["issues"])
        self.issues.setText(
            f"<ol style='margin-top:0; margin-bottom:0; margin-left:0; -qt-list-indent:1'>{items}</ol>"
        )
        key = (status, tuple(check["issues"]))
        if key != self.issue_key:
            self.issue_key = key
            self.show_issues(False)
        if not has_issues:
            self.show_issues(False)
        stale = check["stale"]
        self.stale.setText("、".join(stale) + "已更新，当前内容仍使用旧版来源" if stale else "")
        self.stale.setVisible(bool(stale))

    def show_issues(self, shown: bool) -> None:
        self.issues.setVisible(shown)
        chevron = icons.pixmap("chevron", theme.WARN, 9)
        if shown:
            chevron = chevron.transformed(QTransform().rotate(180))
        self.chevron.setPixmap(chevron)
        self.resized.emit()

    def set_notice(self, text: str) -> None:
        self.notice.setText(text)
        self.notice.setVisible(bool(text))
