"""Readable draft editors and a drag-sortable nine-column storyboard."""

from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.short_video.v2.contracts import JsonObject
from core.short_video.v2.models import identity, require
from core.short_video.v2.validation import recalculate_timeline

STORYBOARD_COLUMNS = [
    "镜号",
    "时间轴",
    "景别",
    "镜头运动",
    "人物",
    "动作",
    "对白",
    "音效",
    "画面生成提示词",
]


def format_shot_timeline(shot: JsonObject) -> str:
    def format_timestamp(milliseconds: int) -> str:
        seconds, millis = divmod(milliseconds, 1000)
        return f"{seconds // 60:02}:{seconds % 60:02}.{millis:03}"

    return f"{format_timestamp(shot['start_ms'])}–{format_timestamp(shot['end_ms'])}"


class CopyEditor(QWidget):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.content = None
        self.fields = []
        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.body = QWidget()
        self.form = QVBoxLayout(self.body)
        scroll.setWidget(self.body)
        layout.addWidget(scroll)

    def set_content(self, content: JsonObject | None) -> None:
        self.content = deepcopy(content)
        self.fields = []
        while self.form.count():
            item = self.form.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not content:
            self.form.addWidget(QLabel("从上一页继续生成，或在这里重新生成。"))
            return
        if "opening" in content:
            sections = [("开头", content["opening"], "text")]
            sections += [
                (f"正文 {i + 1}", paragraph, "text")
                for i, paragraph in enumerate(content["paragraphs"])
            ]
            sections += [("结尾", content["cliffhanger"], "text")]
            nodes = [
                self.content["opening"],
                *self.content["paragraphs"],
                self.content["cliffhanger"],
            ]
            for (label, _, field), node in zip(sections, nodes):
                self.add_field(label, node, field)
        else:
            for index, block in enumerate(self.content["blocks"]):
                label = {"opening": "开头", "body": "正文", "cliffhanger": "结尾"}[block["role"]]
                self.form.addWidget(QLabel(f"{index + 1} · {label}"))
                self.add_field("口播", block, "narration")
                self.add_field("动作", block, "visible_action")
                dialogue = QPlainTextEdit(format_dialogue(block["dialogue"]))
                dialogue.setPlaceholderText("人物：对白，每行一条；画外对白用 画外/人物：对白")
                dialogue.setMaximumHeight(100)
                dialogue.textChanged.connect(self.changed)
                self.form.addWidget(QLabel("对白"))
                self.form.addWidget(dialogue)
                self.fields.append((block, "dialogue", dialogue))
        self.form.addStretch()

    def add_field(self, label: str, node: JsonObject, field: str) -> None:
        self.form.addWidget(QLabel(label))
        edit = QPlainTextEdit(node[field])
        edit.setMinimumHeight(70)
        edit.setMaximumHeight(150)
        edit.textChanged.connect(self.changed)
        self.form.addWidget(edit)
        self.fields.append((node, field, edit))

    def value(self) -> JsonObject:
        require(self.content is not None, "INVALID_INPUT", "请先生成稿件")
        for node, field, edit in self.fields:
            node[field] = (
                parse_dialogue(edit.toPlainText(), node[field])
                if field == "dialogue"
                else edit.toPlainText()
            )
        return deepcopy(self.content)


def format_dialogue(lines: list[JsonObject], names: dict[str, str] | None = None) -> str:
    names = names or {}
    return "\n".join(
        ("画外/" if line["voice_over"] else "")
        + names.get(line.get("character_id"), line.get("speaker_label", "旁白"))
        + "："
        + line["text"]
        for line in lines
    )


def parse_dialogue(
    raw: str, previous: list[JsonObject], catalog: list[JsonObject] | None = None
) -> list[JsonObject]:
    result = []
    for index, line in enumerate(raw.splitlines()):
        if not line.strip():
            continue
        label, separator, text = line.partition("：")
        if not separator:
            label, separator, text = line.partition(":")
        require(
            bool(separator) and bool(text.strip()),
            "INVALID_INPUT",
            f"对白第 {index + 1} 行请填写 人物：对白",
        )
        voice_over = label.startswith("画外/")
        label = label.removeprefix("画外/").strip()
        item = {"speaker_label": label, "text": text.strip(), "voice_over": voice_over}
        if catalog is not None:
            matches = [
                e
                for e in catalog
                if e["kind"] == "character" and label in {e["name"], *e["aliases"]}
            ]
            if len(matches) == 1:
                item["character_id"] = matches[0]["asset_id"]
            else:
                old = previous[index] if index < len(previous) else None
                require(
                    not matches and old is not None and "character_id" in old,
                    "INVALID_INPUT",
                    f"无法确定“{label}”是哪位人物，请使用原文中的明确名字",
                )
                item["character_id"] = old["character_id"]
        result.append(item)
    return result


def format_shot_dialogue(shot: JsonObject, names: dict[str, str]) -> str:
    if "dialogue_text" in shot:
        return shot["dialogue_text"]
    return "\n".join(
        filter(
            None,
            [
                format_dialogue(shot["dialogue"], names),
                "旁白：" + shot["narration"] if shot["narration"] else "",
            ],
        )
    )


class ShotTable(QTableWidget):
    row_moved = Signal(int, int)

    def __init__(self) -> None:
        super().__init__(0, 9)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        # Ctrl／Shift selects several shots for one revision; editing still follows the current row.
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)

    def dropEvent(self, event: QDropEvent) -> None:
        target = self.indexAt(event.position().toPoint()).row()
        single = len(self.selectionModel().selectedRows()) <= 1
        if event.source() is self and self.currentRow() >= 0 and single:
            self.row_moved.emit(self.currentRow(), target if target >= 0 else self.rowCount() - 1)
            event.acceptProposedAction()
        else:
            event.ignore()


class StoryboardEditor(QWidget):
    changed = Signal()
    selection_changed = Signal()
    revise_requested = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.content, self.loading, self.names, self.catalog = None, False, {}, []
        layout = QVBoxLayout(self)
        self.table = ShotTable()
        self.table.setHorizontalHeaderLabels(STORYBOARD_COLUMNS)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_row_menu)
        self.table.row_moved.connect(self.move_shot)
        self.table.itemSelectionChanged.connect(self.select_shot)
        self.table.cellChanged.connect(self.edit_cell)
        layout.addWidget(self.table, 2)
        self.summary = QLabel()
        layout.addWidget(self.summary)
        form = QFormLayout()
        self.duration_input = QDoubleSpinBox()
        self.duration_input.setRange(0.001, 3600)
        self.duration_input.setDecimals(3)
        self.duration_input.setSuffix(" 秒")
        self.duration_input.valueChanged.connect(self.edit_details)
        form.addRow("时长", self.duration_input)
        self.detail_fields = {}
        for field, label in [
            ("action", "动作"),
            ("dialogue", "对白"),
            ("visual_prompt_draft", "画面提示词"),
        ]:
            edit = QPlainTextEdit()
            edit.setMaximumHeight(85)
            edit.textChanged.connect(self.edit_details)
            form.addRow(label, edit)
            self.detail_fields[field] = edit
        layout.addLayout(form)
        bar = QHBoxLayout()
        add = QPushButton("添加镜头")
        add.clicked.connect(self.add_shot)
        bar.addWidget(add)
        bar.addStretch()
        layout.addLayout(bar)

    def draft_snapshot(self) -> JsonObject:
        """Read visible edits without saving or validating incomplete free text."""
        return deepcopy(self.content) if self.content is not None else {"shots": []}

    def value(self) -> JsonObject:
        require(self.content is not None, "INVALID_INPUT", "请先生成分镜")
        # Deferred free-text name/dialogue validation leaves incomplete typing editable.
        content = deepcopy(self.content)
        for index, shot in enumerate(content["shots"]):
            for field, label in [
                ("shot_size", "景别"),
                ("camera_movement", "镜头运动"),
                ("action", "动作"),
                ("visual_prompt_draft", "画面提示词"),
            ]:
                require(
                    bool(shot[field].strip()),
                    "INVALID_INPUT",
                    f"镜头 {index + 1}「{label}」不能为空",
                )
            if "dialogue_text" in shot:
                dialogue_lines, narration_lines = [], []
                for line in shot.pop("dialogue_text").splitlines():
                    normalized = line.replace(":", "：", 1)
                    if normalized.startswith("旁白："):
                        narration_lines.append(normalized.removeprefix("旁白：").strip())
                    else:
                        dialogue_lines.append(line)
                shot["dialogue"] = parse_dialogue(
                    "\n".join(dialogue_lines), shot["dialogue"], self.catalog
                )
                shot["narration"] = "\n".join(narration_lines)
            if "character_text" in shot:
                labels = [
                    name.strip()
                    for name in shot.pop("character_text").replace(",", "、").split("、")
                    if name.strip() and name.strip() != "无"
                ]
                references, display = [], {}
                for position, label in enumerate(labels):
                    matches = [
                        e
                        for e in self.catalog
                        if e["kind"] == "character"
                        and e.get("usage") != "voice_only"
                        and label in {e["name"], *e["aliases"]}
                    ]
                    require(
                        len(matches) <= 1,
                        "INVALID_INPUT",
                        f"镜头 {index + 1}：名字“{label}”对应多个人物，请说明具体身份",
                    )
                    if matches:
                        entity = matches[0]
                        reference = next(
                            (r for r in shot["characters"] if r["asset_id"] == entity["asset_id"]),
                            {
                                "asset_id": entity["asset_id"],
                                "variant_id": entity["variants"][0]["variant_id"],
                            },
                        )
                    else:
                        require(
                            position < len(shot["characters"]),
                            "INVALID_INPUT",
                            f"镜头 {index + 1}：请在对话中说明新人物“{label}”的身份后重新生成",
                        )
                        reference = shot["characters"][position]
                    references.append(reference)
                    display[reference["asset_id"]] = label
                shot["characters"], shot["character_labels"] = references, display
        return recalculate_timeline(content)

    def set_content(self, content: JsonObject | None) -> None:
        selected = self.selected_shots()
        self.content = deepcopy(content)
        self.refresh_table(selected[0] if selected else None)

    def refresh_table(self, shot_id: str | None = None) -> None:
        self.loading = True
        shots = (self.content or {}).get("shots", [])
        if self.content and self.content.get("shots"):
            self.content = recalculate_timeline(self.content)
            shots = self.content["shots"]
        self.table.setRowCount(len(shots))
        selected = 0
        for row, shot in enumerate(shots):
            labels = {**self.names, **shot.get("character_labels", {})}
            values = [
                str(row + 1),
                format_shot_timeline(shot),
                shot["shot_size"],
                shot["camera_movement"],
                shot.get(
                    "character_text", "、".join(labels[r["asset_id"]] for r in shot["characters"])
                ),
                shot["action"],
                format_shot_dialogue(shot, labels),
                shot["sound"],
                shot["visual_prompt_draft"],
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col in {0, 1, 6}:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(row, col, item)
            if shot["shot_id"] == shot_id:
                selected = row
        for col, width in enumerate([55, 185, 90, 100, 120, 220, 220, 140, 280]):
            self.table.setColumnWidth(col, width)
        if shots:
            self.table.selectRow(selected)
        self.summary.setText(
            f"共 {len(shots)} 镜 · 预计 {sum(s['duration_ms'] for s in shots) / 1000:g} 秒；按动作与声音安排时长"
        )
        self.loading = False
        self.select_shot()

    def selected_shots(self) -> list[str]:
        row = self.table.currentRow()
        shots = (self.content or {}).get("shots", [])
        return [shots[row]["shot_id"]] if 0 <= row < len(shots) else []

    def select_shot(self) -> None:
        if self.loading:
            return
        row = self.table.currentRow()
        shots = (self.content or {}).get("shots", [])
        self.loading = True
        self.duration_input.setEnabled(0 <= row < len(shots))
        if 0 <= row < len(shots):
            shot = shots[row]
            self.duration_input.setValue(shot["duration_ms"] / 1000)
            for field, edit in self.detail_fields.items():
                edit.setPlainText(
                    format_shot_dialogue(shot, {**self.names, **shot.get("character_labels", {})})
                    if field == "dialogue"
                    else shot[field]
                )
        else:
            for edit in self.detail_fields.values():
                edit.clear()
        self.loading = False
        self.selection_changed.emit()

    def update_local_prompt(self, shot: JsonObject) -> None:
        shot.setdefault("visual_description", shot["visual_prompt_draft"])
        shot["visual_prompt_draft"] = (
            f"{shot['visual_description']}。{shot['shot_size']}，{shot['camera_movement']}。动作：{shot['action']}"
        )

    def edit_cell(self, row: int, col: int) -> None:
        if self.loading:
            return
        shot = self.content["shots"][row]
        field = {
            2: "shot_size",
            3: "camera_movement",
            4: "character_text",
            5: "action",
            7: "sound",
            8: "visual_prompt_draft",
        }[col]
        shot[field] = self.table.item(row, col).text()
        if field == "visual_prompt_draft":
            shot["visual_description"] = shot[field]
        elif field in {"action", "shot_size", "camera_movement"}:
            self.update_local_prompt(shot)
        self.refresh_table(shot["shot_id"])
        self.changed.emit()

    def edit_details(self) -> None:
        if self.loading or not self.selected_shots():
            return
        shot = self.content["shots"][self.table.currentRow()]
        shot["duration_ms"] = round(self.duration_input.value() * 1000)
        sender = self.sender()
        for field, edit in self.detail_fields.items():
            if sender is edit:
                shot["dialogue_text" if field == "dialogue" else field] = edit.toPlainText()
                if field == "action":
                    self.update_local_prompt(shot)
                if field == "visual_prompt_draft":
                    shot["visual_description"] = shot[field]
        # Updating the table must not reset the cursor in the active detail editor.
        self.loading = True
        self.content = recalculate_timeline(self.content)
        row = self.table.currentRow()
        for index, current in enumerate(self.content["shots"]):
            self.table.item(index, 1).setText(format_shot_timeline(current))
        for col, field in [(5, "action"), (8, "visual_prompt_draft")]:
            self.table.item(row, col).setText(shot[field])
        if sender is self.detail_fields["action"]:
            prompt_edit = self.detail_fields["visual_prompt_draft"]
            prompt_edit.blockSignals(True)
            prompt_edit.setPlainText(shot["visual_prompt_draft"])
            prompt_edit.blockSignals(False)
        self.loading = False
        self.summary.setText(
            f"共 {len(self.content['shots'])} 镜 · 预计 {self.content['shots'][-1]['end_ms'] / 1000:g} 秒；按动作与声音安排时长"
        )
        self.changed.emit()

    def selected_shot_ids(self) -> list[str]:
        """All selected rows in storyboard order (Ctrl／Shift adds rows)."""
        shots = (self.content or {}).get("shots", [])
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        return [shots[row]["shot_id"] for row in rows if 0 <= row < len(shots)]

    def show_row_menu(self, position: object) -> None:
        row = self.table.indexAt(position).row()
        if row < 0:
            return
        if row not in {index.row() for index in self.table.selectionModel().selectedRows()}:
            self.table.selectRow(row)
        selected = self.selected_shot_ids()
        menu = QMenu(self)
        menu.addAction(
            "按意见修订所选镜头…", lambda: self.revise_requested.emit(self.selected_shot_ids())
        )
        menu.addSeparator()
        # Structural edits act on one shot; with several rows selected they would be ambiguous.
        single = len(selected) == 1
        for label, action in (
            ("拆成两镜", self.split),
            ("与下一镜合并", self.merge),
            ("删除镜头", self.delete_shot),
        ):
            menu.addAction(label, action).setEnabled(single)
        menu.exec(self.table.viewport().mapToGlobal(position))

    def move_shot(self, source: int, target: int) -> None:
        shots = (self.content or {}).get("shots", [])
        if not 0 <= source < len(shots) or not 0 <= target < len(shots):
            return
        shot = shots.pop(source)
        shots.insert(target, shot)
        self.refresh_table(shot["shot_id"])
        self.changed.emit()

    def add_shot(self) -> None:
        shots = (self.content or {}).get("shots", [])
        if not shots:
            return
        row = max(0, self.table.currentRow())
        shot = deepcopy(shots[row])
        shot.update(
            shot_id=identity("shot"), action="填写动作", dialogue=[], narration="", duration_ms=8000
        )
        self.update_local_prompt(shot)
        shots.insert(row + 1, shot)
        self.refresh_table(shot["shot_id"])
        self.changed.emit()

    def split(self) -> None:
        shots = (self.content or {}).get("shots", [])
        row = self.table.currentRow()
        if row < 0 or shots[row]["duration_ms"] < 2:
            return
        second = deepcopy(shots[row])
        second["shot_id"] = identity("shot")
        second["duration_ms"] //= 2
        shots[row]["duration_ms"] -= second["duration_ms"]
        shots.insert(row + 1, second)
        self.refresh_table(second["shot_id"])
        self.changed.emit()

    def merge(self) -> None:
        shots = (self.content or {}).get("shots", [])
        row = self.table.currentRow()
        if row < 0 or row + 1 >= len(shots):
            return
        first, second = shots[row : row + 2]
        if first["scene_ref"] != second["scene_ref"]:
            self.summary.setText("这两镜发生在不同场景，请保留分镜或通过对话说明合并后的场景。")
            return
        first["duration_ms"] += second["duration_ms"]
        for field in ("action", "narration", "sound", "visual_prompt_draft"):
            first[field] += "\n" + second[field]
        for field in ("characters", "props", "source_refs"):
            first[field] += [value for value in second[field] if value not in first[field]]
        first["dialogue"] += second["dialogue"]
        first["video_block_refs"] = list(
            dict.fromkeys(first["video_block_refs"] + second["video_block_refs"])
        )
        del shots[row + 1]
        self.refresh_table(first["shot_id"])
        self.changed.emit()

    def delete_shot(self) -> None:
        row = self.table.currentRow()
        if self.content and row >= 0:
            if len(self.content["shots"]) == 1:
                self.summary.setText("至少保留一个镜头。")
                return
            removed = self.content["shots"].pop(row)
            neighbour = self.content["shots"][min(row, len(self.content["shots"]) - 1)]
            neighbour["video_block_refs"] = list(
                dict.fromkeys(neighbour["video_block_refs"] + removed["video_block_refs"])
            )
            neighbour["source_refs"] += [
                ref for ref in removed["source_refs"] if ref not in neighbour["source_refs"]
            ]
            self.refresh_table(neighbour["shot_id"])
            self.changed.emit()
