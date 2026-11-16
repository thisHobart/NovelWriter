"""Project discussions with optional live, read-only storyboard attachments."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from PySide6.QtGui import (
    QColor,
    QFont,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFrameFormat,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from core.gui import theme
from core.short_video.v2.contracts import JsonObject
from core.short_video.v2.conversation import ChatWork, ConversationService
from core.short_video.v2.models import encode, require
from core.short_video.v2.storyboard_chat import read_storyboard_chat_context

from .ui_helpers import create_action_button, show_text_dialog

if TYPE_CHECKING:
    from .workbench import Workbench


class ChatPanel(QWidget):
    def __init__(self, workbench: Workbench) -> None:
        super().__init__()
        self.workbench, self.conversation_id = workbench, None
        layout = QVBoxLayout(self)
        title_bar = QHBoxLayout()
        heading = QLabel("与 AI 讨论")
        heading.setStyleSheet("font-size:18px; font-weight:600")
        title_bar.addWidget(heading, 1)
        history = QPushButton("对话")
        self.history_menu = QMenu(history)
        self.history_menu.addAction("新对话", lambda: workbench.perform(self.create))
        self.history_menu.addAction(
            "选择已有对话", lambda: workbench.perform(self.choose_conversation)
        )
        self.history_menu.addAction("重试上次问题", lambda: workbench.perform(self.retry))
        history.setMenu(self.history_menu)
        title_bar.addWidget(history)
        layout.addLayout(title_bar)
        self.conversations = QComboBox(self)
        self.conversations.hide()
        self.messages = QTextBrowser()
        self.messages.setOpenExternalLinks(False)
        self.messages.setOpenLinks(False)
        self.messages.document().setDocumentMargin(theme.SP[3])
        layout.addWidget(self.messages, 2)
        self.context_panel = QWidget()
        self.context_panel.setObjectName("storyboardChatContext")
        self.context_panel.setStyleSheet(
            f"#storyboardChatContext {{ background: {theme.ACCENT_TINT}; border-radius: {theme.RADIUS_CARD}px; }}"
        )
        context_layout = QVBoxLayout(self.context_panel)
        context_layout.setContentsMargins(theme.SP[2], theme.SP[2], theme.SP[2], theme.SP[2])
        context_bar = QHBoxLayout()
        self.attach_shot = QCheckBox("附带当前分镜")
        self.attach_shot.setChecked(True)
        context_bar.addWidget(self.attach_shot, 1)
        self.preview_context_button = create_action_button(
            "查看附带内容", self.preview_context, context_bar, workbench
        )
        context_layout.addLayout(context_bar)
        self.context_label = QLabel("未附带分镜")
        self.context_label.setWordWrap(True)
        context_layout.addWidget(self.context_label)
        self.context_note = QLabel("本镜 · 前后镜 · 关联文案 · 相关资产 · 原文片段")
        self.context_note.setWordWrap(True)
        self.context_note.setStyleSheet(f"font-size:{theme.FS_XS}px; color:{theme.ACCENT}")
        context_layout.addWidget(self.context_note)
        layout.addWidget(self.context_panel)
        self.context_panel.hide()
        self.attach_shot.toggled.connect(self.refresh_reference)
        self.input = QPlainTextEdit()
        self.input.setPlaceholderText("输入消息，与 AI 对话")
        self.input.setMaximumHeight(110)
        layout.addWidget(self.input)
        bar = QHBoxLayout()
        self.send_button = create_action_button("发送", self.send, bar, workbench)
        self.stop_button = create_action_button("停止对话", self.stop, bar, workbench)
        layout.addLayout(bar)
        self.conversations.currentIndexChanged.connect(lambda: workbench.perform(self.select))
        self.setMinimumWidth(270)

    def refresh_reference(self) -> None:
        page = getattr(self.workbench, "storyboard", None)
        active = page is not None and self.workbench.current_artifact_page() is page
        self.context_panel.setVisible(active)
        selected = page.editor.selected_shots() if active and page.record else []
        self.attach_shot.setEnabled(bool(selected))
        attached = bool(selected) and self.attach_shot.isChecked()
        self.preview_context_button.setEnabled(attached)
        self.context_note.setVisible(attached)
        if not attached:
            self.context_label.setText("未附带分镜 · 本次按普通聊天发送")
            return
        index = page.editor.table.currentRow()
        self.context_label.setText(
            f"当前附带：第 {index + 1} 镜 · " + ("含未保存修改" if page.dirty else "已保存版本")
        )

    def selected_context(self) -> JsonObject | None:
        page = getattr(self.workbench, "storyboard", None)
        if (
            page is None
            or self.workbench.current_artifact_page() is not page
            or not self.attach_shot.isChecked()
            or page.record is None
        ):
            return None
        selected = page.editor.selected_shots()
        if not selected:
            return None
        return read_storyboard_chat_context(
            self.workbench.repository,
            page.editor.draft_snapshot(),
            selected[0],
            page.record,
            unsaved=page.dirty,
        )

    def preview_context(self) -> None:
        context = self.selected_context()
        require(context is not None, "INVALID_INPUT", "请先在分镜页选中要讨论的镜头")
        show_text_dialog(self, "本次附带的分镜上下文", encode(context), readonly=True)

    def choose_conversation(self) -> None:
        labels = [self.conversations.itemText(i) for i in range(self.conversations.count())]
        if not labels:
            return
        label, ok = QInputDialog.getItem(
            self, "已有对话", "选择对话", labels, max(0, self.conversations.currentIndex()), False
        )
        if ok:
            self.conversations.setCurrentIndex(labels.index(label))

    def service(self) -> ConversationService:
        return ConversationService(
            self.workbench.repository, self.workbench.model(), transport=self.workbench.transport
        )

    def create(self) -> None:
        conversation = self.service().store.create()
        self.conversation_id = conversation["id"]
        self.workbench.settings.setValue(
            self.workbench.setting_key("conversation"), self.conversation_id
        )
        self.refresh()

    def select(self) -> None:
        if self.conversations.currentData():
            self.conversation_id = self.conversations.currentData()
            self.workbench.settings.setValue(
                self.workbench.setting_key("conversation"), self.conversation_id
            )
            self.refresh()

    def refresh(self) -> None:
        self.refresh_reference()
        conversations = self.service().store.list()
        self.conversation_id = self.conversation_id or self.workbench.settings.value(
            self.workbench.setting_key("conversation"), ""
        )
        self.conversations.blockSignals(True)
        self.conversations.clear()
        for conversation in conversations:
            self.conversations.addItem(
                conversation["title"] + " · " + conversation["updated_at"][:16], conversation["id"]
            )
        index = self.conversations.findData(self.conversation_id)
        self.conversations.setCurrentIndex(index if index >= 0 else 0)
        self.conversation_id = self.conversations.currentData()
        self.conversations.blockSignals(False)
        messages = (
            self.service().store.get(self.conversation_id)["messages"]
            if self.conversation_id
            else []
        )
        self.render_messages(messages)
        active = self.workbench.tasks.works.get("text")
        self.stop_button.setEnabled(
            isinstance(active, ChatWork)
            and self.workbench.host.runner.busy
            and active.command["conversation_id"] == self.conversation_id
        )

    def render_messages(self, messages: Sequence[JsonObject]) -> None:
        scrollbar = self.messages.verticalScrollBar()
        previous_position = scrollbar.value()
        follow_latest = previous_position == scrollbar.maximum()
        self.messages.clear()
        document = self.messages.document()
        for message in messages:
            cursor = QTextCursor(document)
            cursor.movePosition(QTextCursor.End)
            frame_format = QTextFrameFormat()
            frame_format.setBottomMargin(theme.SP[4])
            frame = cursor.insertFrame(frame_format)
            cursor = frame.firstCursorPosition()
            sender_format = QTextCharFormat()
            sender_format.setFontWeight(QFont.Weight.DemiBold)
            sender_format.setForeground(QColor(theme.ACCENT))
            sender = "你" if message["role"] == "user" else "AI"
            reference = message.get("storyboard_reference")
            if reference:
                sender += f" · 第 {reference['order']} 镜"
                if reference["unsaved"]:
                    sender += " · 含未保存修改"
            cursor.insertText(sender, sender_format)
            content_format = QTextBlockFormat()
            content_format.setTopMargin(theme.SP[2])
            cursor.insertBlock(content_format, QTextCharFormat())
            if message["role"] == "assistant":
                cursor.insertMarkdown(
                    message["content"],
                    QTextDocument.MarkdownDialectGitHub | QTextDocument.MarkdownNoHTML,
                )
            else:
                cursor.insertText(message["content"])
        block = document.begin()
        while block.isValid():
            block_format = block.blockFormat()
            block_format.setLineHeight(145, QTextBlockFormat.ProportionalHeight.value)
            QTextCursor(block).setBlockFormat(block_format)
            block = block.next()
        scrollbar.setValue(scrollbar.maximum() if follow_latest else previous_position)

    def stop(self) -> None:
        active = self.workbench.tasks.works.get("text")
        if (
            isinstance(active, ChatWork)
            and active.command["conversation_id"] == self.conversation_id
        ):
            self.workbench.host.runner.cancel_current()

    def send(self) -> None:
        require(not self.workbench.host.runner.busy, "PROJECT_BUSY", "已有任务运行，请等待或停止")
        context = self.selected_context()
        if not self.conversation_id:
            self.create()
        command = self.service().prepare(
            self.conversation_id, self.input.toPlainText(), storyboard_context=context
        )
        self.workbench.submit(
            ChatWork(self.workbench.repository, command, transport=self.workbench.transport),
            self.send_button,
        )
        self.input.clear()

    def retry(self) -> None:
        require(self.conversation_id is not None, "INVALID_INPUT", "尚无历史消息")
        users = [
            m
            for m in self.service().store.get(self.conversation_id)["messages"]
            if m["role"] == "user"
        ]
        require(bool(users), "INVALID_INPUT", "尚无历史问题")
        self.input.setPlainText(users[-1]["content"])
        self.workbench.notice("问题已放回输入框。请核对后再点击发送；不会自动重试。")
