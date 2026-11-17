"""Shots the latest chat reply proposes to change: pick the affected shots, then apply revisions."""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.gui import theme
from core.short_video.v2.contracts import JsonObject
from core.short_video.v2.models import WorkspaceError, require
from core.short_video.v2.storyboard_edits import (
    AFFECTED_TAG,
    REVISED_TAG,
    affected_shots,
    reply_blocks,
    revised_shots,
    shot_changes,
    shot_fingerprint,
)

from .ui_helpers import create_action_button

if TYPE_CHECKING:
    from .artifact_page import ArtifactPage
    from .chat_panel import ChatPanel
    from .workbench import Workbench


def _html(value: str) -> str:
    return html.escape(value).replace("\n", "<br>") or "（空）"


class ShotEditPanel(QWidget):
    """Shown under the messages while the storyboard page is open and the last reply carries an
    affected-shot list or revised shots."""

    def __init__(self, chat: ChatPanel, workbench: Workbench) -> None:
        super().__init__()
        self.chat, self.workbench = chat, workbench
        self.setObjectName("shotEditPanel")
        self.setStyleSheet(
            f"#shotEditPanel {{ background: {theme.SURFACE}; border: 1px solid {theme.LINE};"
            f" border-radius: {theme.RADIUS_CARD}px; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.SP[2], theme.SP[2], theme.SP[2], theme.SP[2])
        self.title = QLabel()
        self.title.setStyleSheet(f"font-size:{theme.FS_MD}px; font-weight:600")
        layout.addWidget(self.title)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet(f"font-size:{theme.FS_SM}px; color:{theme.WARN}")
        layout.addWidget(self.note)
        self.rows = QWidget()
        self.rows_layout = QVBoxLayout(self.rows)
        self.rows_layout.setContentsMargins(0, 0, theme.SP[2], 0)
        self.rows_layout.setSpacing(theme.SP[2])
        scroll = QScrollArea()
        scroll.setWidget(self.rows)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setMaximumHeight(260)
        layout.addWidget(scroll)
        bar = QHBoxLayout()
        self.edit_button = create_action_button(
            "修改所选镜头", self.request_edit, bar, workbench, primary=True
        )
        self.apply_button = create_action_button(
            "应用到分镜", self.apply, bar, workbench, primary=True
        )
        self.undo_button = create_action_button("撤销应用", self.undo_apply, bar, workbench)
        bar.addStretch()
        layout.addLayout(bar)
        self.mode: str | None = None
        self.reply_id: str | None = None
        self.items: list[JsonObject] = []
        self.results: dict[str, JsonObject] = {}
        self.boxes: dict[str, QCheckBox] = {}
        self.checked: dict[tuple[str, str], bool] = {}
        self.undo: JsonObject | None = None
        self.hide()

    # -- state -----------------------------------------------------------------------------

    def page(self) -> ArtifactPage | None:
        page = getattr(self.workbench, "storyboard", None)
        if page is None or self.workbench.current_artifact_page() is not page or not page.record:
            return None
        return page

    def update_from(self, messages: list[JsonObject]) -> None:
        """Rebuild from the conversation's last reply and the current storyboard draft."""
        page = self.page()
        reply = messages[-1] if messages else None
        if (
            page is None
            or reply is None
            or reply["role"] != "assistant"
            or reply["status"] != "complete"
        ):
            self.mode = None
            self.hide()
            return
        parent = next((m for m in messages if m["id"] == reply.get("parent_message_id")), {})
        blocks = reply_blocks(reply["content"])
        self.reply_id = reply["id"]
        self.clear_rows()
        if REVISED_TAG in blocks and "storyboard_edit" in parent:
            self.mode = "result"
            self.show_results(reply["content"], parent["storyboard_edit"], page)
        elif AFFECTED_TAG in blocks:
            self.mode = "affected"
            self.show_affected(reply["content"], page)
        else:
            self.mode = None
        self.edit_button.setVisible(self.mode == "affected")
        self.apply_button.setVisible(self.mode == "result")
        self.undo_button.setVisible(self.mode == "result")
        self.update_buttons()
        self.setVisible(self.mode is not None)

    def clear_rows(self) -> None:
        self.boxes.clear()
        while self.rows_layout.count():
            widget = self.rows_layout.takeAt(0).widget()
            if widget:
                widget.setParent(None)
                widget.deleteLater()

    def add_row(self, shot_id: str, title: str, detail: str, *, enabled: bool) -> None:
        row = QWidget()
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(theme.SP[1])
        box = QCheckBox(title)
        key = (self.reply_id or "", shot_id)
        box.setEnabled(enabled)
        box.setChecked(enabled and self.checked.get(key, True))
        box.toggled.connect(lambda checked, key=key: self.remember(key, checked))
        row_layout.addWidget(box)
        if detail:
            label = QLabel(detail)
            label.setTextFormat(Qt.RichText)
            label.setWordWrap(True)
            label.setStyleSheet(f"font-size:{theme.FS_SM}px; margin-left:{theme.SP[6]}px")
            row_layout.addWidget(label)
        self.rows_layout.addWidget(row)
        self.boxes[shot_id] = box

    def remember(self, key: tuple[str, str], checked: bool) -> None:
        self.checked[key] = checked
        self.update_buttons()

    def chosen(self) -> list[str]:
        return [sid for sid, box in self.boxes.items() if box.isEnabled() and box.isChecked()]

    def update_buttons(self) -> None:
        self.edit_button.setEnabled(bool(self.chosen()))
        self.apply_button.setEnabled(bool(self.chosen()))
        self.undo_button.setEnabled(self.can_undo())

    # -- affected shots --------------------------------------------------------------------

    def show_affected(self, content: str, page: ArtifactPage) -> None:
        listed = affected_shots(content, page.editor.draft_snapshot()["shots"])
        self.items = listed["items"]
        self.title.setText("受影响的镜头")
        notes = []
        if "error" in listed:
            notes.append("AI 给出的受影响镜头清单无法读取：" + listed["error"])
        elif not self.items:
            notes.append("清单里的镜头都已不在分镜中")
        if listed["missing_assets"]:
            notes.append(
                "资产目录里没有：" + "、".join(listed["missing_assets"])
                + "。这些只会写进动作和画面提示词，镜头的资产绑定不变。"
            )
        self.note.setText("\n".join(notes))
        self.note.setStyleSheet(f"font-size:{theme.FS_SM}px; color:{theme.WARN}")
        self.note.setVisible(bool(notes))
        for item in self.items:
            title = f"第 {item['order']} 镜" + (f" · {item['fields']}" if item["fields"] else "")
            detail = _html(item["change"]) if item["change"] else ""
            self.add_row(item["shot_id"], title, detail, enabled=True)

    def request_edit(self) -> None:
        chosen = set(self.chosen())
        items = [item for item in self.items if item["shot_id"] in chosen]
        require(bool(items), "INVALID_INPUT", "请先勾选要修改的镜头")
        self.chat.send_edit(items)

    # -- revised shots ---------------------------------------------------------------------

    def show_results(self, content: str, edit: JsonObject, page: ArtifactPage) -> None:
        self.title.setText("AI 修改结果")
        shots = page.editor.draft_snapshot()["shots"]
        current = {s["shot_id"]: s for s in shots}
        entities = page.editor.catalog
        base = {sid: current[sid] for sid in edit["shot_ids"] if sid in current}
        self.results = {}
        try:
            accepted, rejected = revised_shots(content, base, entities)
        except WorkspaceError as exc:
            self.note.setText("AI 回复中的修改结果无法读取：" + str(exc))
            self.note.setStyleSheet(f"font-size:{theme.FS_SM}px; color:{theme.WARN}")
            self.note.show()
            return
        positions = {s["shot_id"]: i + 1 for i, s in enumerate(shots)}
        for sid in sorted(edit["shot_ids"], key=lambda s: positions.get(s, len(shots) + 1)):
            if sid not in current:
                self.add_row(sid, "这一镜已不在分镜里", "", enabled=False)
                continue
            title = f"第 {positions[sid]} 镜"
            if sid in rejected:
                self.add_row(sid, f"{title} · 未通过检查", _html(rejected[sid]), enabled=False)
                continue
            changes = shot_changes(current[sid], accepted[sid], entities)
            if not changes:
                self.add_row(sid, f"{title} · 与分镜一致", "", enabled=False)
                continue
            self.results[sid] = accepted[sid]
            detail = "".join(
                f"<p style='margin:0 0 {theme.SP[1]}px 0'><b>{html.escape(label)}</b><br>"
                f"<span style='color:{theme.INK_400}'>原来：{_html(before)}</span><br>"
                f"改成：{_html(after)}</p>"
                for label, before, after in changes
            )
            if shot_fingerprint(current[sid]) != edit["base"][sid]:
                detail += (
                    f"<p style='margin:0; color:{theme.WARN}'>"
                    "AI 修改之后这一镜又被编辑过，应用会覆盖这些编辑。</p>"
                )
            self.add_row(sid, f"{title} · 改了 {len(changes)} 处", detail, enabled=True)
        self.note.setText(
            "勾选的镜头会写进分镜表格，算作未保存的修改；点保存之前不会写入项目。"
        )
        self.note.setStyleSheet(f"font-size:{theme.FS_SM}px; color:{theme.INK_600}")
        self.note.show()

    def apply(self) -> None:
        page = self.page()
        require(page is not None and self.mode == "result", "INVALID_INPUT", "请先打开分镜页")
        chosen = self.chosen()
        require(bool(chosen), "INVALID_INPUT", "请先勾选要应用的镜头")
        shots = page.editor.draft_snapshot()["shots"]
        positions = {s["shot_id"]: i + 1 for i, s in enumerate(shots)}
        current = {s["shot_id"]: s for s in shots}
        reply = self.chat.current_messages[-1]
        parent = next(m for m in self.chat.current_messages if m["id"] == reply["parent_message_id"])
        base = parent["storyboard_edit"]["base"]
        overwritten = [
            str(positions[sid]) for sid in chosen if shot_fingerprint(current[sid]) != base[sid]
        ]
        if overwritten and (
            QMessageBox.question(
                self,
                "应用到分镜",
                f"第 {'、'.join(overwritten)} 镜在 AI 修改之后又被编辑过，应用会覆盖这些编辑。继续应用？",
            )
            != QMessageBox.Yes
        ):
            return
        before = {
            "content": page.editor.draft_snapshot(),
            "marked": set(page.editor.marked),
            "dirty": page.dirty,
            "record": page.record["id"],
        }
        page.editor.apply_shots([self.results[sid] for sid in chosen])
        self.undo = {**before, "after": page.editor.draft_snapshot()}
        self.workbench.notice(
            f"已把第 {'、'.join(str(positions[sid]) for sid in chosen)} 镜写进分镜，尚未保存。"
        )
        self.chat.refresh_reference()

    def can_undo(self) -> bool:
        page = self.page()
        return bool(
            self.undo
            and page is not None
            and page.record["id"] == self.undo["record"]
            and page.editor.draft_snapshot() == self.undo["after"]
        )

    def undo_apply(self) -> None:
        require(self.can_undo(), "INVALID_INPUT", "分镜在应用之后又有改动，不能撤销")
        page = self.page()
        undo, self.undo = self.undo, None
        page.editor.restore(undo["content"], undo["marked"])
        if not undo["dirty"]:
            page.dirty = False
            page.refresh()
        self.workbench.notice("已撤销应用，分镜恢复到应用之前。")
        self.chat.refresh_reference()
