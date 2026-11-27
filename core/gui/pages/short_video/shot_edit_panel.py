"""Card under the latest AI reply: pick the affected shots to revise, then apply the revisions."""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QTransform
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.gui import icons, theme
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

from .chat_stream import reveal
from .ui_helpers import create_action_button

if TYPE_CHECKING:
    from .artifact_page import ArtifactPage
    from .chat_panel import ChatPanel
    from .workbench import Workbench

CARD_STYLE = f"""
#shotCard {{ background:{theme.CANVAS}; border:1px solid {theme.LINE_STRONG};
    border-radius:{theme.RADIUS_CARD}px; }}
#cardHead {{ background:{theme.SURFACE}; border-bottom:1px solid {theme.LINE};
    border-top-left-radius:5px; border-top-right-radius:5px; }}
#cardHead[collapsed="true"] {{ border-bottom:0; border-radius:5px; }}
#cardFoot {{ background:{theme.SURFACE}; border-top:1px solid {theme.LINE};
    border-bottom-left-radius:5px; border-bottom-right-radius:5px; }}
#cardWarn {{ background:{theme.tint(theme.WARN)}; color:{theme.WARN}; font-size:{theme.FS_SM}px;
    border-bottom:1px solid {theme.LINE}; padding:{theme.SP[2]}px {theme.SP[3]}px; }}
#shotRow {{ border-bottom:1px solid {theme.LINE}; }}
#shotRow[last="true"] {{ border-bottom:0; }}
#shotRow QCheckBox {{ font-weight:600; }}
#shotRow QCheckBox[applied="true"]::indicator {{ background:{theme.SUCCESS}; border-color:{theme.SUCCESS}; }}
QLabel#countPill {{ font-size:{theme.FS_XS}px; color:{theme.INK_600}; background:{theme.RAIL};
    border-radius:9px; padding:0 7px; }}
QLabel#cardSub, QLabel#cardFootText {{ font-size:{theme.FS_SM}px; color:{theme.INK_600}; }}
QLabel#chip {{ font-size:{theme.FS_XS}px; color:{theme.INK_600}; border:1px solid {theme.LINE};
    border-radius:3px; padding:0 5px; }}
QLabel#chip[state="ok"] {{ color:{theme.SUCCESS}; background:{theme.tint(theme.SUCCESS)}; border:0; }}
QLabel#chip[state="bad"] {{ color:{theme.DANGER}; background:{theme.tint(theme.DANGER)}; border:0; }}
QLabel#rowText {{ font-size:{theme.FS_SM}px; color:{theme.INK_600}; }}
QLabel#rowText[off="true"] {{ color:{theme.INK_400}; }}
QLabel#rejected {{ font-size:{theme.FS_SM}px; color:{theme.DANGER}; background:{theme.tint(theme.DANGER)};
    border-radius:{theme.RADIUS}px; padding:6px {theme.SP[2]}px; }}
QLabel#diffLabel {{ font-size:{theme.FS_XS}px; font-weight:600; color:{theme.INK_600}; }}
QLabel#diffOld {{ font-size:{theme.FS_SM}px; color:{theme.INK_400}; }}
QLabel#diffNew {{ font-size:{theme.FS_SM}px; }}
QFrame#diffBar {{ background:{theme.ACCENT}; border:0; }}
QLabel#diffWarn {{ font-size:{theme.FS_SM}px; color:{theme.WARN}; }}
QPushButton#rowToggle {{ border:0; background:transparent; color:{theme.INK_600};
    font-size:{theme.FS_SM}px; padding:0 2px; height:22px; }}
"""


def _text(value: str) -> str:
    return value or "（空）"


def _label(text: str, name: str, *, wrap: bool = True) -> QLabel:
    label = QLabel(text)
    label.setObjectName(name)
    label.setWordWrap(wrap)
    return label


def _small(button: QPushButton) -> QPushButton:
    button.setFixedHeight(28)
    return button


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class CardHead(QWidget):
    def __init__(self, title: str = "", count: str = "", sub: str = "") -> None:
        super().__init__()
        self.setObjectName("cardHead")
        self.setAttribute(Qt.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(theme.SP[3], theme.SP[2], theme.SP[3], theme.SP[2])
        layout.setSpacing(theme.SP[2])
        self.title = QLabel(title)
        self.title.setStyleSheet(f"font-size:{theme.FS_MD}px; font-weight:600")
        layout.addWidget(self.title)
        self.count = _label(count, "countPill", wrap=False)
        layout.addWidget(self.count)
        layout.addStretch()
        self.sub = _label(sub, "cardSub", wrap=False)
        layout.addWidget(self.sub)


class CollapsedCard(QFrame):
    """An earlier reply's card, folded to one line once a newer request was sent."""

    def __init__(self, title: str, count: str, sub: str) -> None:
        super().__init__()
        self.setObjectName("shotCard")
        self.setStyleSheet(CARD_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        self.head = CardHead(title, count, sub)
        self.head.setProperty("collapsed", True)
        layout.addWidget(self.head)


def earlier_card(messages: list[JsonObject], index: int, shots: list[JsonObject]) -> QFrame | None:
    """The one-line card for an earlier reply that listed affected shots or revised shots."""
    reply = messages[index]
    blocks = reply_blocks(reply["content"])
    parent = next((m for m in messages if m["id"] == reply.get("parent_message_id")), {})
    if REVISED_TAG in blocks and "storyboard_edit" in parent:
        count = len(parent["storyboard_edit"]["shot_ids"])
        return CollapsedCard("AI 修改结果", f"{count} 镜", "已有新的回复")
    if AFFECTED_TAG not in blocks:
        return None
    order = parent.get("storyboard_reference", {}).get("shot_ids")
    listed = affected_shots(reply["content"], shots, order) or {"items": []}
    following = messages[index + 1] if index + 1 < len(messages) else {}
    edit = following.get("storyboard_edit") if following.get("role") == "user" else None
    sub = "已有新的回复"
    if edit:
        positions = {s["shot_id"]: i + 1 for i, s in enumerate(shots)}
        orders = sorted(positions[sid] for sid in edit["shot_ids"] if sid in positions)
        sub = f"已按第 {'、'.join(map(str, orders))} 镜发出修改" if orders else "已发出修改"
    return CollapsedCard("受影响的镜头", str(len(listed["items"])), sub)


class ShotRow(QWidget):
    """One shot: the checkbox carries the shot number, tags follow, details sit below."""

    def __init__(self, title: str, *, enabled: bool, checked: bool) -> None:
        super().__init__()
        self.setObjectName("shotRow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(theme.SP[3], 10, theme.SP[3], 10)
        self.layout_.setSpacing(theme.SP[1])
        self.head = QHBoxLayout()
        self.head.setSpacing(theme.SP[2])
        self.box = QCheckBox(title)
        self.box.setEnabled(enabled)
        self.box.setChecked(checked)
        self.head.addWidget(self.box)
        self.layout_.addLayout(self.head)
        self.state = ""
        self.toggle: QPushButton | None = None
        self.details: QWidget | None = None

    def add_chip(self, text: str, state: str = "") -> None:
        chip = _label(text, "chip", wrap=False)
        if state:
            chip.setProperty("state", state)
        self.head.addWidget(chip)

    def finish_head(self) -> None:
        self.head.addStretch()

    def add_body(self, widget: QWidget) -> QWidget:
        """Indent under the shot number, past the checkbox."""
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(24, 0, 0, 0)
        layout.addWidget(widget)
        self.layout_.addWidget(holder)
        return holder

    def add_toggle(self, details: QWidget, expanded: bool, on_toggle) -> None:
        self.details = self.add_body(details)
        self.toggle = QPushButton()
        self.toggle.setObjectName("rowToggle")
        self.toggle.setCursor(Qt.PointingHandCursor)
        self.toggle.setLayoutDirection(Qt.RightToLeft)
        self.toggle.clicked.connect(on_toggle)
        self.head.addWidget(self.toggle)
        self.set_expanded(expanded)

    def set_expanded(self, expanded: bool) -> None:
        if self.toggle is None or self.details is None:
            return
        self.details.setVisible(expanded)
        self.toggle.setText("收起" if expanded else "展开")
        chevron = icons.pixmap("chevron", theme.INK_600, 10)
        if expanded:
            chevron = chevron.transformed(QTransform().rotate(180))
        self.toggle.setIcon(chevron)


class ShotEditPanel(QFrame):
    """Shown under the latest reply while the storyboard page is open and that reply carries an
    affected-shot list or revised shots."""

    def __init__(self, chat: ChatPanel, workbench: Workbench) -> None:
        super().__init__()
        self.chat, self.workbench = chat, workbench
        self.setObjectName("shotCard")
        self.setStyleSheet(CARD_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        self.head = CardHead()
        self.title, self.count = self.head.title, self.head.count
        layout.addWidget(self.head)
        self.note = _label("", "cardWarn")
        layout.addWidget(self.note)
        self.rows = QWidget()
        self.rows_layout = QVBoxLayout(self.rows)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(0)
        layout.addWidget(self.rows)
        foot = QWidget()
        foot.setObjectName("cardFoot")
        foot.setAttribute(Qt.WA_StyledBackground, True)
        bar = QHBoxLayout(foot)
        bar.setContentsMargins(theme.SP[3], theme.SP[2], theme.SP[3], theme.SP[2])
        bar.setSpacing(theme.SP[2])
        self.foot_text = _label("", "cardFootText")
        self.foot_text.setTextFormat(Qt.RichText)
        self.foot_text.linkActivated.connect(lambda _: self.toggle_all())
        bar.addWidget(self.foot_text, 1)
        self.undo_button = _small(create_action_button("撤销应用", self.undo_apply, bar, workbench))
        self.edit_button = _small(
            create_action_button("修改所选镜头", self.request_edit, bar, workbench, primary=True)
        )
        self.apply_button = _small(
            create_action_button("应用到分镜", self.apply, bar, workbench, primary=True)
        )
        layout.addWidget(foot)
        self.mode: str | None = None
        self.reply_id: str | None = None
        self.items: list[JsonObject] = []
        self.results: dict[str, JsonObject] = {}
        self.boxes: dict[str, QCheckBox] = {}
        self.shot_rows: dict[str, ShotRow] = {}
        self.checked: dict[tuple[str, str], bool] = {}
        self.expanded: dict[tuple[str, str], bool] = {}
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
        self.clear_rows()
        if (
            page is None
            or reply is None
            or reply["role"] != "assistant"
            or reply["status"] != "complete"
        ):
            self.mode = None
            self.hide()
            self.chat.relayout_stream()
            return
        parent = next((m for m in messages if m["id"] == reply.get("parent_message_id")), {})
        blocks = reply_blocks(reply["content"])
        # Replies number shots as they stood when the question was sent.
        order = parent.get("storyboard_reference", {}).get("shot_ids")
        self.reply_id = reply["id"]
        self.note.hide()
        if REVISED_TAG in blocks and "storyboard_edit" in parent:
            self.mode = "result"
            self.show_results(reply["content"], parent["storyboard_edit"], page, order)
        elif AFFECTED_TAG in blocks:
            self.mode = "affected"
            self.show_affected(reply["content"], page, order)
        else:
            self.mode = None
        self.edit_button.setVisible(self.mode == "affected")
        self.apply_button.setVisible(self.mode == "result")
        self.undo_button.setVisible(self.mode == "result")
        rows = list(self.shot_rows.values())
        for row in rows:
            row.setProperty("last", row is rows[-1])
            _repolish(row)
        self.update_buttons()
        self.setVisible(self.mode is not None)
        reveal(self)
        self.chat.relayout_stream()

    def clear_rows(self) -> None:
        self.boxes.clear()
        self.shot_rows.clear()
        while self.rows_layout.count():
            widget = self.rows_layout.takeAt(0).widget()
            if widget:
                # Hidden rather than detached, so a pending queued show cannot open a window.
                widget.hide()
                widget.deleteLater()

    def add_row(self, shot_id: str, title: str, *, enabled: bool) -> ShotRow:
        key = (self.reply_id or "", shot_id)
        row = ShotRow(title, enabled=enabled, checked=enabled and self.checked.get(key, True))
        row.box.toggled.connect(lambda checked, key=key: self.remember(key, checked))
        self.rows_layout.addWidget(row)
        self.boxes[shot_id] = row.box
        self.shot_rows[shot_id] = row
        return row

    def row_text(self, shot_id: str) -> str:
        row = self.shot_rows[shot_id]
        return row.box.text() + (f" · {row.state}" if row.state else "")

    def remember(self, key: tuple[str, str], checked: bool) -> None:
        self.checked[key] = checked
        row = self.shot_rows.get(key[1])
        if row is not None and self.mode == "affected":
            for label in row.findChildren(QLabel, "rowText"):
                label.setProperty("off", not checked)
                _repolish(label)
        self.update_buttons()

    def chosen(self) -> list[str]:
        return [sid for sid, box in self.boxes.items() if box.isEnabled() and box.isChecked()]

    def toggle_all(self) -> None:
        boxes = [box for box in self.boxes.values() if box.isEnabled()]
        target = not all(box.isChecked() for box in boxes)
        for box in boxes:
            box.setChecked(target)

    def update_buttons(self) -> None:
        chosen = self.chosen()
        self.edit_button.setEnabled(bool(chosen))
        self.apply_button.setEnabled(bool(chosen))
        self.undo_button.setEnabled(self.can_undo())
        if self.mode == "affected":
            total = sum(box.isEnabled() for box in self.boxes.values())
            link = "全不选" if total and len(chosen) == total else "全选"
            self.foot_text.setText(
                f"已选 {len(chosen)} / {total} 镜 · "
                f"<a href='all' style='color:{theme.ACCENT}; text-decoration:none'>{link}</a>"
            )
        elif self.mode == "result":
            applied = self.applied_orders()
            self.foot_text.setText(
                f"已写入第 {'、'.join(applied)} 镜，尚未保存"
                if applied
                else "勾选的镜头写进分镜表格，算作未保存的修改"
            )

    def show_warning(self, lines: list[str]) -> None:
        self.note.setText("\n".join(lines))
        self.note.setVisible(bool(lines))

    # -- affected shots --------------------------------------------------------------------

    def show_affected(self, content: str, page: ArtifactPage, order: list[str] | None) -> None:
        shots = page.editor.draft_snapshot()["shots"]
        lengths = {shot["shot_id"]: shot["duration_ms"] for shot in shots}
        listed = affected_shots(content, shots, order)
        self.items = listed["items"]
        self.title.setText("受影响的镜头")
        self.count.setText(str(len(self.items)))
        notes = []
        if "error" in listed:
            notes.append("AI 给出的受影响镜头清单无法读取：" + listed["error"])
        elif not self.items:
            notes.append("清单里的镜头都已不在分镜中")
        if listed["missing_assets"]:
            notes.append(
                "资产目录里没有：" + "、".join(listed["missing_assets"])
                + "。这些只会写进画面，镜头的资产绑定不变。"
            )
        self.show_warning(notes)
        for item in self.items:
            row = self.add_row(item["shot_id"], f"第 {item['order']} 镜", enabled=True)
            current, suggested = lengths[item["shot_id"]], item.get("duration_ms")
            new_length = suggested is not None and suggested != current
            for field in filter(None, item["fields"].split("、")):
                if not (new_length and field == "时长"):
                    row.add_chip(field)
            if new_length:
                # 「修改所选镜头」 passes this length on, and the revised shot uses it.
                row.add_chip(f"时长 {current / 1000:g} → {suggested / 1000:g} 秒")
            row.finish_head()
            if item["change"]:
                text = _label(item["change"], "rowText")
                text.setProperty("off", not row.box.isChecked())
                row.add_body(text)

    def request_edit(self) -> None:
        chosen = set(self.chosen())
        items = [item for item in self.items if item["shot_id"] in chosen]
        require(bool(items), "INVALID_INPUT", "请先勾选要修改的镜头")
        self.chat.send_edit(items)

    # -- revised shots ---------------------------------------------------------------------

    def applied_orders(self) -> list[str]:
        page = self.page()
        if not self.can_undo() or page is None:
            return []
        positions = {s["shot_id"]: i + 1 for i, s in enumerate(page.editor.draft_snapshot()["shots"])}
        return [str(positions[sid]) for sid in self.undo["applied"] if sid in positions]

    def show_results(
        self, content: str, edit: JsonObject, page: ArtifactPage, order: list[str] | None
    ) -> None:
        self.title.setText("AI 修改结果")
        self.count.setText(f"{len(edit['shot_ids'])} 镜")
        shots = page.editor.draft_snapshot()["shots"]
        current = {s["shot_id"]: s for s in shots}
        entities = page.editor.catalog
        base = {sid: current[sid] for sid in edit["shot_ids"] if sid in current}
        self.results = {}
        try:
            accepted, rejected = revised_shots(content, base, entities, order)
        except WorkspaceError as exc:
            self.show_warning(["AI 回复中的修改结果无法读取：" + str(exc)])
            return
        applied = set(self.undo["applied"]) if self.can_undo() else set()
        positions = {s["shot_id"]: i + 1 for i, s in enumerate(shots)}
        first_open = True
        for sid in sorted(edit["shot_ids"], key=lambda s: positions.get(s, len(shots) + 1)):
            if sid not in current:
                row = self.add_row(sid, "这一镜已不在分镜里", enabled=False)
                row.finish_head()
                continue
            row = self.add_row(sid, f"第 {positions[sid]} 镜", enabled=False)
            if sid in rejected:
                row.state = "未通过检查"
                row.add_chip(row.state, "bad")
                row.finish_head()
                row.add_body(_label(rejected[sid], "rejected"))
                continue
            changes = shot_changes(current[sid], accepted[sid], entities)
            if not changes:
                row.state = "已写入分镜" if sid in applied else "与分镜一致"
                row.add_chip(row.state, "ok" if sid in applied else "")
                if sid in applied:
                    row.box.setChecked(True)
                    row.box.setProperty("applied", True)
                    _repolish(row.box)
                row.finish_head()
                continue
            self.results[sid] = accepted[sid]
            row.box.setEnabled(True)
            row.box.setChecked(self.checked.get((self.reply_id or "", sid), True))
            row.state = f"改了 {len(changes)} 处"
            row.add_chip(row.state)
            row.finish_head()
            details = QWidget()
            diff_layout = QVBoxLayout(details)
            diff_layout.setContentsMargins(0, theme.SP[1], 0, 0)
            diff_layout.setSpacing(theme.SP[2])
            for label, before, after in changes:
                diff = QVBoxLayout()
                diff.setSpacing(2)
                diff.addWidget(_label(label, "diffLabel"))
                old = _label(
                    f"<span style='text-decoration:line-through'>{html.escape(_text(before))}</span>",
                    "diffOld",
                )
                old.setTextFormat(Qt.RichText)
                diff.addWidget(old)
                new = _label(_text(after), "diffNew")
                new.setTextFormat(Qt.PlainText)
                new_line = QHBoxLayout()
                new_line.setSpacing(theme.SP[2])
                bar = QFrame()
                bar.setObjectName("diffBar")
                bar.setFixedWidth(2)
                new_line.addWidget(bar)
                new_line.addWidget(new, 1)
                diff.addLayout(new_line)
                diff_layout.addLayout(diff)
            if shot_fingerprint(current[sid]) != edit["base"][sid]:
                diff_layout.addWidget(
                    _label("AI 修改之后这一镜又被编辑过，应用会覆盖这些编辑。", "diffWarn")
                )
            key = (self.reply_id or "", sid)
            expanded = self.expanded.get(key, first_open)
            first_open = False
            row.add_toggle(details, expanded, lambda checked=False, key=key: self.toggle_row(key))

    def toggle_row(self, key: tuple[str, str]) -> None:
        row = self.shot_rows.get(key[1])
        if row is None or row.details is None:
            return
        self.expanded[key] = row.details.isHidden()
        row.set_expanded(self.expanded[key])
        self.chat.relayout_stream(follow=False)

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
        self.undo = {**before, "after": page.editor.draft_snapshot(), "applied": chosen}
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
