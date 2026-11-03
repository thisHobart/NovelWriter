"""Project conversations using only user messages and chat history."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
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

from core.short_video.v2.conversation import ChatWork, ConversationService
from core.short_video.v2.models import require

from .ui_helpers import create_action_button

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
        layout.addWidget(self.messages, 2)
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
        self.messages.clear()
        if self.conversation_id:
            for message in self.service().store.get(self.conversation_id)["messages"]:
                cursor = self.messages.textCursor()
                cursor.movePosition(QTextCursor.End)
                cursor.insertText(
                    f"\n{'你' if message['role'] == 'user' else 'AI'}\n{message['content']}\n"
                )
        self.stop_button.setEnabled(
            isinstance(self.workbench.active_work, ChatWork)
            and self.workbench.host.runner.busy
            and self.workbench.active_work.command["conversation_id"] == self.conversation_id
        )

    def stop(self) -> None:
        if (
            isinstance(self.workbench.active_work, ChatWork)
            and self.workbench.active_work.command["conversation_id"] == self.conversation_id
        ):
            self.workbench.host.runner.cancel_current()

    def send(self) -> None:
        require(not self.workbench.host.runner.busy, "PROJECT_BUSY", "已有任务运行，请等待或停止")
        if not self.conversation_id:
            self.create()
        command = self.service().prepare(self.conversation_id, self.input.toPlainText())
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
