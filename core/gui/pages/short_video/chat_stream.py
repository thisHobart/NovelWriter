"""Chat messages as widgets: your messages on the right, AI replies on the left, cards inline."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import (
    QFont,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
)
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from core.gui import theme
from core.gui.widgets.controls import VScrollArea

BUBBLE_PAD = (12, 8)


def wrap_height(widget: QWidget) -> QWidget:
    """Height follows width, so the stream can lay out wrapped text before it is shown."""
    policy = widget.sizePolicy()
    policy.setHeightForWidth(True)
    widget.setSizePolicy(policy)
    return widget


def reveal(widget: QWidget) -> None:
    """Show now the children Qt would show on its next pass, so they count in heights measured
    right away: a child added to a visible widget stays hidden until that queued show runs."""
    for child in widget.findChildren(QWidget):
        if (
            child.isHidden()
            and not child.isWindow()
            and not child.testAttribute(Qt.WA_WState_ExplicitShowHide)
        ):
            child.show()


def markdown_html(text: str, font: QFont) -> str:
    document = QTextDocument()
    document.setDefaultFont(font)
    document.setMarkdown(
        text, QTextDocument.MarkdownDialectGitHub | QTextDocument.MarkdownNoHTML
    )
    block = document.begin()
    while block.isValid():
        block_format = block.blockFormat()
        block_format.setLineHeight(150, QTextBlockFormat.ProportionalHeight.value)
        if block_format.bottomMargin() > theme.SP[2]:
            block_format.setBottomMargin(theme.SP[2])
        cursor = QTextCursor(block)
        cursor.setBlockFormat(block_format)
        if block_format.headingLevel():
            # Headings read as bold body text instead of the document's oversized headings.
            heading = QTextCharFormat()
            heading.setProperty(QTextFormat.FontSizeAdjustment, 0)
            heading.setProperty(QTextFormat.FontPixelSize, theme.FS_MD)
            heading.setFontWeight(QFont.Weight.DemiBold)
            cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
            cursor.mergeCharFormat(heading)
        block = block.next()
    return document.toHtml()


def meta_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"font-size:{theme.FS_XS}px; color:{theme.INK_400}")
    return label


class Bubble(QLabel):
    """Your message: as wide as its longest line, wrapping at most at 88% of the stream."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.setTextFormat(Qt.PlainText)
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.setContentsMargins(BUBBLE_PAD[0], BUBBLE_PAD[1], BUBBLE_PAD[0], BUBBLE_PAD[1])
        self.setStyleSheet(
            f"background:{theme.ACCENT_TINT}; border-radius:10px;"
            f" border-bottom-right-radius:3px"
        )
        wrap_height(self)
        self.ensurePolished()
        self.setMaximumWidth(self.natural_width())

    def natural_width(self) -> int:
        metrics = self.fontMetrics()
        lines = self.text().splitlines() or [""]
        return max(metrics.horizontalAdvance(line) for line in lines) + 2 * BUBBLE_PAD[0] + 4

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt 命名
        width = min(self.natural_width(), 160)
        return QSize(width, self.heightForWidth(width))


class UserMessage(QWidget):
    def __init__(self, text: str, tag: str = "") -> None:
        super().__init__()
        self.plain = "你" + (f" · {tag}" if tag else "") + "\n" + text
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SP[1])
        meta = QHBoxLayout()
        meta.setSpacing(6)
        meta.addStretch()
        if tag:
            self.tag = QLabel(tag)
            self.tag.setStyleSheet(
                f"font-size:{theme.FS_XS}px; color:{theme.ACCENT}; background:{theme.ACCENT_TINT};"
                f" border-radius:3px; padding:0 5px"
            )
            meta.addWidget(self.tag)
        meta.addWidget(meta_label("你"))
        layout.addLayout(meta)
        row = QHBoxLayout()
        row.setSpacing(0)
        row.addStretch(12)
        self.bubble = Bubble(text)
        row.addWidget(self.bubble, 88)
        layout.addLayout(row)
        wrap_height(self)


class AiMessage(QWidget):
    """A reply on the left without a background; cards about it sit right under the text."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.plain = "AI\n" + text
        self.cards = QVBoxLayout()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SP[1])
        layout.addWidget(meta_label("AI"))
        self.body = QLabel()
        self.body.setTextFormat(Qt.RichText)
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.ensurePolished()
        self.body.setText(markdown_html(text, self.body.font()))
        wrap_height(self.body)
        layout.addWidget(self.body)
        self.cards.setContentsMargins(0, theme.SP[1], 0, 0)
        self.cards.setSpacing(theme.SP[2])
        layout.addLayout(self.cards)
        wrap_height(self)

    def add_card(self, card: QWidget) -> None:
        self.cards.addWidget(card)


class TypingLine(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.plain = "AI\n正在回复…"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SP[1])
        layout.addWidget(meta_label("AI"))
        text = QLabel("正在回复…")
        text.setStyleSheet(f"font-size:{theme.FS_SM}px; color:{theme.INK_400}")
        layout.addWidget(text)


class MessageStream(VScrollArea):
    """The conversation, filling the panel's height and scrolling as one column."""

    def __init__(self) -> None:
        super().__init__()
        self.inner = QWidget()
        self.inner.setObjectName("chatStream")
        self.inner.setAttribute(Qt.WA_StyledBackground, True)
        self.inner.setStyleSheet(f"#chatStream {{ background:{theme.CANVAS}; }}")
        self.column = QVBoxLayout(self.inner)
        self.column.setContentsMargins(theme.SP[4], theme.SP[4], theme.SP[4], theme.SP[2])
        self.column.setSpacing(theme.SP[4])
        self.column.addStretch(1)
        self.items: list[QWidget] = []
        self.setWidget(self.inner)

    def clear(self, keep: QWidget) -> None:
        """Drop every message; `keep` (the live card) moves back to the stream unshown."""
        if keep.parentWidget() is not self.inner:
            keep.setParent(self.inner)
        for widget in self.items:
            self.column.removeWidget(widget)
            if widget is not keep:
                widget.setParent(None)
                widget.deleteLater()
        self.items.clear()

    def add(self, widget: QWidget) -> None:
        self.column.insertWidget(self.column.count() - 1, wrap_height(widget))
        if not widget.testAttribute(Qt.WA_WState_ExplicitShowHide):
            widget.show()
        reveal(widget)
        self.items.append(widget)

    def toPlainText(self) -> str:  # noqa: N802 - mirrors QTextEdit
        return "\n\n".join(getattr(widget, "plain", "") for widget in self.items)

    def at_end(self) -> bool:
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - theme.SP[1]

    def scroll_to(self, position: int | None) -> None:
        """None follows the latest message once the new layout has settled."""

        def apply() -> None:
            bar = self.verticalScrollBar()
            bar.setValue(bar.maximum() if position is None else position)

        apply()
        QTimer.singleShot(0, apply)
