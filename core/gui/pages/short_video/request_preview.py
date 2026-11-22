"""「查看附带内容」: everything a send would give the AI now, with the rules folded away."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.gui import theme
from core.short_video.v2.conversation_context import ConversationContext


def context_hint(context: ConversationContext) -> str:
    """The line under the input when a send would leave part of the history out; else ""."""
    dropped = len(context.omitted_message_ids) // 2
    parts = []
    if context.omitted_reply_ids:
        parts.append(f"{len(context.omitted_reply_ids)} 轮的 AI 回复没有带上")
    if context.shortened_reply_ids:
        parts.append(f"{len(context.shortened_reply_ids)} 轮的 AI 回复只带了开头")
    if dropped:
        return f"最早 {dropped} 轮对话没有带上" + ("；之后 " + "，".join(parts) if parts else "")
    if parts:
        return "较早 " + "，".join(parts) + "，你的消息都带上了"
    return ""


def context_summary(context: ConversationContext, limit: int, *, attached: bool) -> str:
    sizes = [len(message["content"]) for message in context.request]
    turns = (len(context.request) - 2) // 2
    history = f"之前的 {turns} 轮对话 {sum(sizes[1:-1])} 字" if turns else "没有之前的对话"
    current = "附带内容和你的消息" if attached else "你的消息"
    lines = [
        f"点发送后，AI 会收到下面这些内容，共 {context.character_count} 字（上限 {limit} 字）：",
        f"规则 {sizes[0]} 字 · {history} · {current} {sizes[-1]} 字。",
    ]
    hint = context_hint(context)
    if hint:
        lines.append(hint + "；缩短的回复里写明了省略多少。")
    return "\n".join(lines)


def request_text(context: ConversationContext) -> str:
    """The past turns and this message exactly as sent, each headed by who says it."""
    dropped = len(context.omitted_message_ids) // 2
    shortened, omitted = set(context.shortened_reply_ids), set(context.omitted_reply_ids)
    replies = context.messages[1:-1:2]
    past = context.request[1:-1]
    parts = []
    for index, reply in enumerate(replies):
        number = dropped + index + 1
        state = (
            "（只带了开头）"
            if reply["id"] in shortened
            else "（没有带上）"
            if reply["id"] in omitted
            else ""
        )
        parts.append(f"── 第 {number} 轮 · 你 ──\n{past[2 * index]['content']}")
        parts.append(f"── 第 {number} 轮 · AI{state} ──\n{past[2 * index + 1]['content']}")
    parts.append(f"── 本次发送 ──\n{context.request[-1]['content']}")
    return "\n\n".join(parts)


class RequestPreviewDialog(QDialog):
    def __init__(
        self, parent: QWidget, context: ConversationContext, limit: int, *, attached: bool
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("发给 AI 的内容")
        self.resize(760, 640)
        layout = QVBoxLayout(self)
        layout.setSpacing(theme.SP[2])
        self.summary = QLabel(context_summary(context, limit, attached=attached))
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.summary)
        rules = context.request[0]["content"]
        self.rules_button = QToolButton()
        self.rules_button.setCheckable(True)
        self.rules_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.rules_button.setArrowType(Qt.RightArrow)
        self.rules_button.setText(f"规则（{len(rules)} 字）")
        self.rules_button.setToolTip("每次发送都带上的要求，按通用、本页、输出格式分段")
        self.rules_button.setAutoRaise(True)
        self.rules_button.toggled.connect(self.show_rules)
        layout.addWidget(self.rules_button)
        self.rules = QPlainTextEdit(rules)
        self.rules.setReadOnly(True)
        self.rules.setMaximumHeight(240)
        self.rules.hide()
        layout.addWidget(self.rules)
        self.body = QPlainTextEdit(request_text(context))
        self.body.setReadOnly(True)
        layout.addWidget(self.body, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def show_rules(self, shown: bool) -> None:
        self.rules.setVisible(shown)
        self.rules_button.setArrowType(Qt.DownArrow if shown else Qt.RightArrow)
