"""Project discussions with optional live, read-only storyboard attachments."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.gui import icons, theme
from core.short_video.v2.contracts import JsonObject
from core.short_video.v2.conversation import ChatWork, ConversationService
from core.short_video.v2.models import WorkspaceError, encode, require
from core.short_video.v2.storyboard_chat import read_storyboard_chat_context
from core.short_video.v2.storyboard_edits import display_text

from .chat_stream import AiMessage, MessageStream, TypingLine, UserMessage
from .shot_edit_panel import ShotEditPanel, earlier_card
from .ui_helpers import create_action_button, show_text_dialog

if TYPE_CHECKING:
    from .workbench import Workbench

ATTACHED_PARTS = "附带：本镜、前后镜、全稿概要、资产目录、相关资产、原文片段"
MENU_CONVERSATIONS = 12
INPUT_HEIGHT = (52, 120)

PANEL_STYLE = f"""
#chatPanel {{ background:{theme.CANVAS}; }}
#chatHead {{ border-bottom:1px solid {theme.LINE}; }}
#chatComposer {{ border-top:1px solid {theme.LINE}; }}
#chatBox {{ border:1px solid {theme.LINE_STRONG}; border-radius:8px; background:{theme.CANVAS}; }}
#chatBox[focused="true"] {{ border-color:{theme.ACCENT}; }}
QPlainTextEdit#chatInput {{ border:0; background:transparent; padding:0; }}
#attachChip {{ background:{theme.ACCENT_TINT}; border-radius:11px; }}
#attachChip QLabel {{ color:{theme.ACCENT}; font-size:{theme.FS_SM}px; }}
QToolButton#detachButton {{ border:0; border-radius:9px; background:transparent; }}
QToolButton#detachButton:hover {{ background:{theme.tint(theme.ACCENT, 40)}; }}
QPushButton#attachButton {{ height:22px; padding:0 10px; border:1px dashed {theme.LINE_STRONG};
    border-radius:11px; color:{theme.INK_600}; font-size:{theme.FS_SM}px; background:transparent; }}
QPushButton#attachButton:hover {{ color:{theme.ACCENT}; border-color:{theme.ACCENT}; }}
QPushButton#attachButton:disabled {{ color:{theme.INK_400}; border-color:{theme.LINE}; }}
QPushButton#linkButton {{ height:22px; padding:0; border:0; background:transparent;
    color:{theme.ACCENT}; font-size:{theme.FS_SM}px; }}
QPushButton#linkButton:disabled {{ color:{theme.INK_400}; }}
QPushButton#conversationButton {{ height:28px; padding:0 8px; text-align:left; }}
QPushButton#newConversationButton {{ height:28px; padding:0; }}
"""


class AttachChip(QWidget):
    """「附带分镜 · 第 N 镜」 inside the input box: × detaches, the dashed button re-attaches."""

    toggled = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self._checked = True
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SP[1])
        self.chip = QFrame()
        self.chip.setObjectName("attachChip")
        chip_layout = QHBoxLayout(self.chip)
        chip_layout.setContentsMargins(10, 1, 3, 1)
        chip_layout.setSpacing(theme.SP[1])
        self.label = QLabel()
        chip_layout.addWidget(self.label)
        self.detach_button = QToolButton()
        self.detach_button.setObjectName("detachButton")
        self.detach_button.setIcon(icons.icon("close", theme.ACCENT, 12))
        self.detach_button.setFixedSize(18, 18)
        self.detach_button.setCursor(Qt.PointingHandCursor)
        self.detach_button.setToolTip("不附带分镜，按普通聊天发送")
        self.detach_button.clicked.connect(lambda: self.setChecked(False))
        chip_layout.addWidget(self.detach_button)
        self.chip.setToolTip(ATTACHED_PARTS)
        layout.addWidget(self.chip)
        self.attach_button = QPushButton("+ 附带当前分镜")
        self.attach_button.setObjectName("attachButton")
        self.attach_button.setCursor(Qt.PointingHandCursor)
        self.attach_button.clicked.connect(lambda: self.setChecked(True))
        layout.addWidget(self.attach_button)
        layout.addStretch()
        self.sync()

    def isChecked(self) -> bool:  # noqa: N802 - mirrors QCheckBox
        return self._checked

    def setChecked(self, checked: bool) -> None:  # noqa: N802 - mirrors QCheckBox
        if checked == self._checked:
            return
        self._checked = checked
        self.sync()
        self.toggled.emit(checked)

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt 命名
        super().changeEvent(event)
        if event.type() == QEvent.EnabledChange:
            self.sync()

    def sync(self) -> None:
        attached = self._checked and self.isEnabled()
        self.chip.setVisible(attached)
        self.attach_button.setVisible(not attached)
        self.attach_button.setToolTip(
            ATTACHED_PARTS if self.isEnabled() else "先在分镜表格里选中要讨论的镜头"
        )


class FocusFrame(QObject):
    """Outline the input box in the accent colour while you type."""

    def __init__(self, box: QFrame) -> None:
        super().__init__(box)
        self.box = box

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt 命名
        if event.type() in (QEvent.FocusIn, QEvent.FocusOut):
            self.box.setProperty("focused", event.type() == QEvent.FocusIn)
            self.box.style().unpolish(self.box)
            self.box.style().polish(self.box)
        return False


class ChatPanel(QWidget):
    def __init__(self, workbench: Workbench) -> None:
        super().__init__()
        self.workbench, self.conversation_id = workbench, None
        self.setObjectName("chatPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(PANEL_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        head = QWidget()
        head.setObjectName("chatHead")
        head.setAttribute(Qt.WA_StyledBackground, True)
        head.setFixedHeight(48)
        title_bar = QHBoxLayout(head)
        title_bar.setContentsMargins(theme.SP[4], 0, theme.SP[3], 0)
        title_bar.setSpacing(theme.SP[2])
        heading = QLabel("与 AI 讨论")
        heading.setStyleSheet(f"font-size:{theme.FS_LG}px; font-weight:600")
        title_bar.addWidget(heading, 1)
        self.conversation_button = QPushButton("对话")
        self.conversation_button.setObjectName("conversationButton")
        self.conversation_button.setToolTip("切换对话")
        self.conversation_button.setMaximumWidth(170)
        self.history_menu = QMenu(self.conversation_button)
        self.history_menu.aboutToShow.connect(self.fill_history_menu)
        self.conversation_button.setMenu(self.history_menu)
        title_bar.addWidget(self.conversation_button)
        self.new_button = QPushButton()
        self.new_button.setObjectName("newConversationButton")
        self.new_button.setIcon(icons.icon("plus", theme.INK_600, 14))
        self.new_button.setFixedSize(28, 28)
        self.new_button.setToolTip("新对话")
        self.new_button.clicked.connect(lambda: workbench.perform(self.create))
        title_bar.addWidget(self.new_button)
        layout.addWidget(head)
        self.conversations = QComboBox(self)
        self.conversations.hide()

        self.messages = MessageStream()
        layout.addWidget(self.messages, 1)
        self.current_messages: list[JsonObject] = []
        self.rendered_with_page = False
        self.edit_panel = ShotEditPanel(self, workbench)

        composer = QWidget()
        composer.setObjectName("chatComposer")
        composer.setAttribute(Qt.WA_StyledBackground, True)
        composer_layout = QVBoxLayout(composer)
        composer_layout.setContentsMargins(theme.SP[3], theme.SP[2], theme.SP[3], theme.SP[3])
        self.box = QFrame()
        self.box.setObjectName("chatBox")
        box_layout = QVBoxLayout(self.box)
        box_layout.setContentsMargins(10, theme.SP[2], 10, theme.SP[2])
        box_layout.setSpacing(6)
        self.context_panel = QWidget()
        context_layout = QHBoxLayout(self.context_panel)
        context_layout.setContentsMargins(0, 0, 0, 0)
        self.attach_shot = AttachChip()
        self.context_label = self.attach_shot.label
        context_layout.addWidget(self.attach_shot)
        box_layout.addWidget(self.context_panel)
        self.context_panel.hide()
        self.attach_shot.toggled.connect(self.refresh_reference)
        self.input = QPlainTextEdit()
        self.input.setObjectName("chatInput")
        self.input.setFrameShape(QFrame.NoFrame)
        self.input.setPlaceholderText("输入消息，与 AI 对话")
        self.input.document().setDocumentMargin(0)
        self.input.textChanged.connect(self.fit_input)
        self.input.installEventFilter(FocusFrame(self.box))
        box_layout.addWidget(self.input)
        box_foot = QHBoxLayout()
        box_foot.setSpacing(theme.SP[2])
        self.preview_context_button = create_action_button(
            "查看附带内容", self.preview_context, box_foot, workbench
        )
        self.preview_context_button.setObjectName("linkButton")
        self.preview_context_button.setCursor(Qt.PointingHandCursor)
        box_foot.addStretch()
        self.send_button = create_action_button(
            "发送", self.send_or_stop, box_foot, workbench, primary=True
        )
        self.send_button.setFixedHeight(28)
        box_layout.addLayout(box_foot)
        composer_layout.addWidget(self.box)
        layout.addWidget(composer)
        self.fit_input()
        self.conversations.currentIndexChanged.connect(lambda: workbench.perform(self.select))
        self.setMinimumWidth(280)

    # -- input -----------------------------------------------------------------------------

    def fit_input(self) -> None:
        """Grow with the text from two lines up to about five, then scroll."""
        lines = max(2, int(self.input.document().documentLayout().documentSize().height()))
        height = lines * self.input.fontMetrics().lineSpacing() + 2 * self.input.frameWidth() + 6
        self.input.setFixedHeight(min(INPUT_HEIGHT[1], max(INPUT_HEIGHT[0], height)))

    def chatting(self) -> bool:
        active = self.workbench.tasks.works.get("text")
        return (
            isinstance(active, ChatWork)
            and self.workbench.host.runner.busy
            and active.command["conversation_id"] == self.conversation_id
        )

    def refresh_send_button(self) -> None:
        """While this conversation waits for a reply, 「发送」 turns into 「停止」."""
        chatting = self.chatting()
        self.send_button.setText("停止" if chatting else "发送")
        self.send_button.setIcon(icons.icon("stop", theme.INK_600, 12) if chatting else QIcon())
        self.send_button.setProperty("variant", "secondary" if chatting else "primary")
        self.send_button.style().unpolish(self.send_button)
        self.send_button.style().polish(self.send_button)
        self.send_button.setEnabled(chatting or not self.workbench.tasks.busy("text"))

    def send_or_stop(self) -> None:
        if self.chatting():
            self.stop()
        else:
            self.send()

    # -- attached storyboard ---------------------------------------------------------------

    def refresh_reference(self) -> None:
        page = getattr(self.workbench, "storyboard", None)
        active = page is not None and self.workbench.current_artifact_page() is page
        if (self.edit_panel.page() is not None) != self.rendered_with_page:
            self.render_messages(self.current_messages)
        self.edit_panel.update_from(self.current_messages)
        self.context_panel.setVisible(active)
        selected = page.editor.selected_shots() if active and page.record else []
        self.attach_shot.setEnabled(bool(selected))
        attached = bool(selected) and self.attach_shot.isChecked()
        self.preview_context_button.setVisible(attached)
        if not attached:
            self.context_label.setText("未附带分镜")
            return
        index = page.editor.table.currentRow()
        self.context_label.setText(
            f"附带分镜 · 第 {index + 1} 镜 · " + ("含未保存修改" if page.dirty else "已保存")
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

    # -- conversations ---------------------------------------------------------------------

    def fill_history_menu(self) -> None:
        self.history_menu.clear()
        count = self.conversations.count()
        for index in range(min(count, MENU_CONVERSATIONS)):
            action = self.history_menu.addAction(
                self.conversations.itemText(index),
                lambda index=index: self.conversations.setCurrentIndex(index),
            )
            action.setCheckable(True)
            action.setChecked(index == self.conversations.currentIndex())
        if count > MENU_CONVERSATIONS:
            self.history_menu.addAction(
                "更多对话…", lambda: self.workbench.perform(self.choose_conversation)
            )
        if count:
            self.history_menu.addSeparator()
        self.history_menu.addAction("重试上次问题", lambda: self.workbench.perform(self.retry))

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
        current = next((c for c in conversations if c["id"] == self.conversation_id), None)
        title = current["title"] if current else "对话"
        self.conversation_button.setText(
            self.conversation_button.fontMetrics().elidedText(title, Qt.ElideRight, 120)
        )
        self.conversation_button.setToolTip(
            "切换对话 · " + self.conversations.currentText() if current else "切换对话"
        )
        messages = list(
            self.service().store.get(self.conversation_id)["messages"]
            if self.conversation_id
            else []
        )
        pending = self.pending_message(messages)
        if pending is not None:
            messages.append(pending)
        self.current_messages = messages
        self.render_messages(messages)
        self.refresh_send_button()
        self.refresh_reference()

    # -- messages --------------------------------------------------------------------------

    def pending_message(self, messages: list[JsonObject]) -> JsonObject | None:
        """Your request while the reply is on its way, before the task has saved it."""
        if not self.chatting() or (
            messages and messages[-1]["role"] == "user" and messages[-1]["status"] == "pending"
        ):
            return None
        command = self.workbench.tasks.works["text"].command
        snapshot = command["context_snapshot"]
        return {
            "id": "pending",
            "role": "user",
            "content": command["user_text"],
            "status": "pending",
            **{k: snapshot[k] for k in ("storyboard_reference", "storyboard_edit") if k in snapshot},
        }

    def render_messages(self, messages: Sequence[JsonObject]) -> None:
        bar = self.messages.verticalScrollBar()
        follow = self.messages.at_end() or not self.messages.items
        position = bar.value()
        page = self.edit_panel.page()
        self.rendered_with_page = page is not None
        shots = page.editor.draft_snapshot()["shots"] if page else None
        self.messages.clear(keep=self.edit_panel)
        for index, message in enumerate(messages):
            if message["role"] == "user":
                reference = message.get("storyboard_reference")
                tag = ""
                if reference:
                    tag = f"第 {reference['order']} 镜"
                    if reference["unsaved"]:
                        tag += " · 含未保存修改"
                self.messages.add(UserMessage(message["content"], tag))
                continue
            reply = AiMessage(display_text(message["content"]))
            if shots is not None and message["status"] == "complete":
                if index == len(messages) - 1:
                    reply.add_card(self.edit_panel)
                else:
                    card = earlier_card(list(messages), index, shots)
                    if card is not None:
                        reply.add_card(card)
            self.messages.add(reply)
        if self.chatting() and (not messages or messages[-1]["role"] == "user"):
            self.messages.add(TypingLine())
        self.messages.sync_content_height()
        self.messages.scroll_to(None if follow else position)

    def relayout_stream(self, *, follow: bool | None = None) -> None:
        """Cards changed height: re-measure the stream, staying at the latest message if there."""
        at_end = self.messages.at_end() if follow is None else follow
        position = self.messages.verticalScrollBar().value()
        self.edit_panel.updateGeometry()
        host = self.edit_panel.parentWidget()
        if host is not None:
            host.updateGeometry()
        self.messages.sync_content_height()
        self.messages.scroll_to(None if at_end else position)

    # -- sending ---------------------------------------------------------------------------

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

    def send_edit(self, items: list[JsonObject]) -> None:
        """Ask for revised shots; the selected shot stays the discussion's current shot."""
        require(not self.workbench.host.runner.busy, "PROJECT_BUSY", "已有任务运行，请等待或停止")
        page = self.workbench.storyboard
        require(page.record is not None, "INVALID_INPUT", "请先生成分镜")
        draft = page.editor.draft_snapshot()
        selected = page.editor.selected_shots()
        wanted = {item["shot_id"] for item in items}
        context = read_storyboard_chat_context(
            self.workbench.repository,
            draft,
            selected[0] if selected else items[0]["shot_id"],
            page.record,
            unsaved=page.dirty,
            edit_items=items,
        )
        if not self.conversation_id:
            self.create()
        text = "按清单修改第 " + "、".join(str(item["order"]) for item in items) + " 镜"
        try:
            command = self.service().prepare(
                self.conversation_id,
                text,
                storyboard_context=context,
                storyboard_edit={
                    "shots": [s for s in draft["shots"] if s["shot_id"] in wanted],
                    "entities": page.editor.catalog,
                },
            )
        except WorkspaceError as exc:
            if exc.code == "CONTEXT_TOO_LARGE":
                raise WorkspaceError(
                    "CONTEXT_TOO_LARGE", "所选镜头的内容超过对话容量，请少勾选几个镜头"
                ) from exc
            raise
        self.workbench.submit(
            ChatWork(self.workbench.repository, command, transport=self.workbench.transport),
            self.send_button,
        )

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
