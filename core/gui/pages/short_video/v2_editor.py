"""Readable draft editors, and the storyboard: a read-only shot list above a form for one shot."""

from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QDropEvent, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.gui import theme
from core.short_video.v2.contracts import JsonObject
from core.short_video.v2.models import identity, require
from core.short_video.v2.storyboard_chat import asset_label
from core.short_video.v2.validation import SHOT_TEXT_FIELDS, recalculate_timeline

STORYBOARD_COLUMNS = ["镜号", "时长", "取景范围", "人物", "动作"]
# Widths of every column but the last, which takes the rest.
COLUMN_WIDTHS = [48, 84, 96, 120]
SHOT_SIZES = ["远景", "全景", "中景", "近景", "特写", "主观视角"]
DURATION_INPUT_RANGE_SECONDS = (1, 15)
# Trimmed on save: a pasted value often brings a line break along.
TRIMMED_FIELDS = (
    "shot_size",
    "camera_movement",
    "action",
    "narration",
    "sound",
)
MARKED_NOTE = "由 AI 讨论修改，尚未保存"
SENTENCE_ENDS = "。！？；…!?;"

EDITOR_STYLE = f"""
QPlainTextEdit {{ border:1px solid {theme.LINE}; border-radius:{theme.RADIUS}px;
    background:{theme.CANVAS}; padding:2px 4px; selection-background-color:{theme.ACCENT_TINT};
    selection-color:{theme.INK_900}; }}
QPlainTextEdit:hover {{ border-color:{theme.LINE_STRONG}; }}
QPlainTextEdit:focus {{ border-color:{theme.ACCENT}; }}
QPlainTextEdit[missing="true"], QLineEdit[missing="true"] {{ border:1px solid {theme.DANGER}; }}
QSplitter::handle:vertical {{ height:8px; }}
QSplitter::handle:vertical:hover {{ background:{theme.LINE}; }}
QLabel#summary {{ font-size:{theme.FS_SM}px; color:{theme.INK_600}; }}
QLabel#notice {{ background:{theme.tint(theme.WARN)}; color:{theme.WARN}; font-size:{theme.FS_SM}px;
    border-radius:{theme.RADIUS}px; padding:6px {theme.SP[2]}px; }}
QLabel#formHeading {{ font-size:{theme.FS_MD}px; font-weight:600; }}
QLabel#readOnly {{ font-size:{theme.FS_SM}px; color:{theme.INK_600}; }}
"""


def format_shot_timeline(shot: JsonObject) -> str:
    def timestamp(milliseconds: int) -> str:
        minutes, tenths = divmod(round(milliseconds / 100), 600)
        return f"{minutes:02}:{tenths // 10:02}.{tenths % 10}"

    return f"{timestamp(shot['start_ms'])}–{timestamp(shot['end_ms'])}"


def seconds_text(milliseconds: int) -> str:
    return f"{milliseconds / 1000:g} 秒"


def one_line(value: str) -> str:
    return " ".join(value.split())


def join_text(first: str, second: str) -> str:
    """Two shots' text as one: a semicolon between them unless the first already ends a sentence."""
    first, second = first.strip(), second.strip()
    if not first or not second:
        return first or second
    return first + ("" if first[-1] in SENTENCE_ENDS else "；") + second


def split_names(value: str) -> list[str]:
    names = value.replace(",", "、").replace("，", "、").split("、")
    return [name.strip() for name in names if name.strip() and name.strip() != "无"]


def repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class CopyEditor(QWidget):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.content = None
        self.fields = []
        self.rows: list[tuple[str, QPlainTextEdit | None]] = []
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
        # What the form shows, in order: (label, box) per field, (heading, None) per block.
        self.rows = []
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
                self.rows.append((f"{index + 1} · {label}", None))
                self.add_field("口播", block, "narration")
                self.add_field("动作", block, "visible_action")
                dialogue = QPlainTextEdit(format_dialogue(block["dialogue"]))
                dialogue.setPlaceholderText("人物：对白，每行一条；画外对白用 画外/人物：对白")
                dialogue.setMaximumHeight(100)
                dialogue.textChanged.connect(self.changed)
                self.form.addWidget(QLabel("对白"))
                self.form.addWidget(dialogue)
                self.fields.append((block, "dialogue", dialogue))
                self.rows.append(("对白", dialogue))
        self.form.addStretch()

    def add_field(self, label: str, node: JsonObject, field: str) -> None:
        self.form.addWidget(QLabel(label))
        edit = QPlainTextEdit(node[field])
        edit.setMinimumHeight(70)
        edit.setMaximumHeight(150)
        edit.textChanged.connect(self.changed)
        self.form.addWidget(edit)
        self.fields.append((node, field, edit))
        self.rows.append((label, edit))

    def form_rows(self) -> list[tuple[str, str | None]]:
        """The draft as the form shows it, unsaved edits included: (label, text) per field and
        (heading, None) per block of the 视频推广文案."""
        return [(label, None if edit is None else edit.toPlainText()) for label, edit in self.rows]

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
                if e["kind"] == "character" and label in {e["name"], *e.get("aliases", [])}
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
    """The dialogue box: one line per spoken line, then each narration line after 「旁白：」."""
    if "dialogue_text" in shot:
        return shot["dialogue_text"]
    lines = [format_dialogue(shot["dialogue"], names)] if shot["dialogue"] else []
    lines += ["旁白：" + line for line in shot["narration"].splitlines() if line.strip()]
    return "\n".join(lines)


class ShotTable(QTableWidget):
    """The shot list: read-only, one line per shot, reordered by dragging a row."""

    row_moved = Signal(int, int)

    def __init__(self) -> None:
        super().__init__(0, len(STORYBOARD_COLUMNS))
        self.setHorizontalHeaderLabels(STORYBOARD_COLUMNS)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setWordWrap(False)
        self.verticalHeader().hide()
        self.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate(COLUMN_WIDTHS):
            self.setColumnWidth(column, width)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)

    def dropEvent(self, event: QDropEvent) -> None:
        target = self.indexAt(event.position().toPoint()).row()
        if event.source() is self and self.currentRow() >= 0:
            self.row_moved.emit(self.currentRow(), target if target >= 0 else self.rowCount() - 1)
            event.acceptProposedAction()
        else:
            event.ignore()


class GrowingText(QPlainTextEdit):
    """A text box as tall as its text, from min_lines to max_lines; longer text scrolls."""

    def __init__(self, min_lines: int, max_lines: int, placeholder: str) -> None:
        super().__init__()
        self.lines = (min_lines, max_lines)
        self.setPlaceholderText(placeholder)
        self.setTabChangesFocus(True)
        self.document().setDocumentMargin(2)
        self.textChanged.connect(self.fit)
        self.fit()

    def fit(self) -> None:
        layout, block, text = self.document().documentLayout(), self.document().begin(), 0.0
        while block.isValid():
            # Lays the block out at the current width first, so wrapped lines count.
            text += layout.blockBoundingRect(block).height()
            block = block.next()
        line = self.fontMetrics().lineSpacing()
        text = min(self.lines[1] * line, max(self.lines[0] * line, text))
        # Border and padding: whatever the box keeps around its text area.
        chrome = self.height() - self.viewport().height()
        height = max(theme.CONTROL_HEIGHT, round(text + 2 * self.document().documentMargin() + chrome))
        if height != self.height():
            self.setFixedHeight(height)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        # A new width wraps the text differently.
        self.fit()


class StoryboardEditor(QWidget):
    """The shot list is only for looking, choosing and reordering; the form below edits one shot."""

    changed = Signal()
    selection_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.content, self.loading, self.names, self.catalog = None, False, {}, []
        # Shots written in from chat, tinted until the storyboard is saved or reloaded.
        self.marked: set[str] = set()
        self.setStyleSheet(EDITOR_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.summary = QLabel()
        self.summary.setObjectName("summary")
        self.summary.setWordWrap(True)
        bar.addWidget(self.summary, 1)
        add = QPushButton("添加镜头")
        add.setToolTip("在选中的镜头后面加一镜，沿用它的场景")
        add.clicked.connect(self.add_shot)
        bar.addWidget(add)
        layout.addLayout(bar)
        self.notice = QLabel()
        self.notice.setObjectName("notice")
        self.notice.setWordWrap(True)
        self.notice.hide()
        layout.addWidget(self.notice)
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.table = ShotTable()
        self.table.setMinimumHeight(120)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_row_menu)
        self.table.row_moved.connect(self.move_shot)
        self.table.itemSelectionChanged.connect(self.select_shot)
        self.splitter.addWidget(self.table)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(160)
        self.form = QWidget()
        scroll.setWidget(self.form)
        self.splitter.addWidget(scroll)
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 3)
        # Weights for the first layout: the form gets the larger share, like later resizes.
        self.splitter.setSizes([200, 300])
        layout.addWidget(self.splitter, 1)
        self.build_form()

    def build_form(self) -> None:
        form = QFormLayout(self.form)
        form.setContentsMargins(0, theme.SP[2], theme.SP[2], theme.SP[2])
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(theme.SP[3])
        form.setVerticalSpacing(theme.SP[2])
        self.heading = QLabel()
        self.heading.setObjectName("formHeading")
        form.addRow(self.heading)
        self.duration_input = QDoubleSpinBox()
        self.duration_input.setDecimals(1)
        self.duration_input.setSingleStep(0.5)
        self.duration_input.setRange(*DURATION_INPUT_RANGE_SECONDS)
        self.duration_input.setSuffix(" 秒")
        self.duration_input.setFixedWidth(110)
        self.duration_input.valueChanged.connect(self.edit_duration)
        form.addRow(self.field_label("时长"), self.duration_input)
        self.size_input = QLineEdit()
        self.size_input.setPlaceholderText("必填，例如中景，也可从右边的菜单选择")
        self.size_input.textChanged.connect(self.edit_size)
        common = QPushButton("常用取景范围")
        menu = QMenu(common)
        for size in SHOT_SIZES:
            menu.addAction(size, lambda size=size: self.size_input.setText(size))
        common.setMenu(menu)
        size = QHBoxLayout()
        size.setSpacing(theme.SP[2])
        size.addWidget(self.size_input, 1)
        size.addWidget(common)
        form.addRow(self.field_label("取景范围"), size)
        self.detail_fields: dict[str, GrowingText] = {}
        self.add_text_row(form, "camera_movement", "镜头运动", 1, 4, "必填：用一句话写机位怎么动")
        bound = "场景和道具绑定资产目录，这里不能改；要换时在与 AI 的讨论里提出"
        self.scene_label = self.read_only_label(bound)
        form.addRow(self.field_label("场景"), self.scene_label)
        self.add_text_row(form, "characters", "人物", 1, 3, "多个人物用、分开；没有出镜人物就留空")
        self.props_label = self.read_only_label(bound)
        form.addRow(self.field_label("道具"), self.props_label)
        self.add_text_row(form, "action", "动作", 2, 8, "必填：这一镜里看得见的动作")
        self.add_text_row(
            form,
            "dialogue",
            "对白（含旁白）",
            2,
            8,
            "人物：对白，每行一条；旁白：旁白内容；人物不在画面里时写 画外/人物：对白",
        )
        self.add_text_row(form, "sound", "音效", 1, 4, "没有可以留空")

    @staticmethod
    def field_label(text: str) -> QLabel:
        # As tall as a one-line box, so the label lines up with the box's first line.
        label = QLabel(text)
        label.setObjectName("FieldLabel")
        label.setFixedHeight(theme.CONTROL_HEIGHT)
        return label

    @staticmethod
    def read_only_label(tooltip: str) -> QLabel:
        label = QLabel()
        label.setObjectName("readOnly")
        label.setWordWrap(True)
        label.setMinimumHeight(theme.CONTROL_HEIGHT)
        label.setToolTip(tooltip)
        return label

    def add_text_row(
        self,
        form: QFormLayout,
        field: str,
        label: str,
        min_lines: int,
        max_lines: int,
        placeholder: str,
    ) -> None:
        edit = GrowingText(min_lines, max_lines, placeholder)
        edit.textChanged.connect(lambda field=field: self.edit_field(field))
        self.detail_fields[field] = edit
        form.addRow(self.field_label(label), edit)

    # -- content ---------------------------------------------------------------------------

    def draft_snapshot(self) -> JsonObject:
        """Read visible edits without saving or validating incomplete free text."""
        return deepcopy(self.content) if self.content is not None else {"shots": []}

    def value(self) -> JsonObject:
        require(self.content is not None, "INVALID_INPUT", "请先生成分镜")
        # Deferred free-text name/dialogue validation leaves incomplete typing editable.
        content = deepcopy(self.content)
        for index, shot in enumerate(content["shots"]):
            for field in TRIMMED_FIELDS:
                shot[field] = shot[field].strip()
            empty = [field for field in SHOT_TEXT_FIELDS if not shot[field]]
            if empty:
                self.show_missing(index, empty)
            require(
                not empty,
                "INVALID_INPUT",
                f"第 {index + 1} 镜的{'、'.join(SHOT_TEXT_FIELDS[f] for f in empty)}还没填写",
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
                shot["narration"] = "\n".join(line for line in narration_lines if line)
            if "character_text" in shot:
                references, display = [], {}
                for position, label in enumerate(split_names(shot.pop("character_text"))):
                    matches = [
                        e
                        for e in self.catalog
                        if e["kind"] == "character"
                        and e.get("usage") != "voice_only"
                        and label in {e["name"], *e.get("aliases", [])}
                    ]
                    require(
                        len(matches) <= 1,
                        "INVALID_INPUT",
                        f"第 {index + 1} 镜：名字“{label}”对应多个人物，请说明具体身份",
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
                            f"第 {index + 1} 镜：请在对话中说明新人物“{label}”的身份后重新生成",
                        )
                        reference = shot["characters"][position]
                    references.append(reference)
                    display[reference["asset_id"]] = label
                shot["characters"], shot["character_labels"] = references, display
        return recalculate_timeline(content)

    def set_content(self, content: JsonObject | None) -> None:
        selected = self.selected_shots()
        self.content = deepcopy(content)
        self.marked.clear()
        self.refresh_table(selected[0] if selected else None)
        self.show_notice("")

    def apply_shots(self, shots: list[JsonObject]) -> None:
        """Replace draft shots by shot_id, as an unsaved edit."""
        require(self.content is not None, "INVALID_INPUT", "请先生成分镜")
        replacements = {shot["shot_id"]: deepcopy(shot) for shot in shots}
        self.content["shots"] = [
            replacements.get(shot["shot_id"], shot) for shot in self.content["shots"]
        ]
        self.marked |= replacements.keys()
        selected = self.selected_shots()
        self.refresh_table(selected[0] if selected else None)
        self.changed.emit()

    def restore(self, content: JsonObject, marked: set[str]) -> None:
        """Put back an earlier draft, e.g. to undo shots applied from chat."""
        selected = self.selected_shots()
        self.content = deepcopy(content)
        self.marked = set(marked)
        self.refresh_table(selected[0] if selected else None)
        self.changed.emit()

    # -- shot list -------------------------------------------------------------------------

    def labels(self, shot: JsonObject) -> dict[str, str]:
        return {**self.names, **shot.get("character_labels", {})}

    def cast_text(self, shot: JsonObject) -> str:
        if "character_text" in shot:
            return shot["character_text"]
        labels = self.labels(shot)
        return "、".join(labels.get(r["asset_id"], r["asset_id"]) for r in shot["characters"])

    def refresh_table(self, shot_id: str | None = None) -> None:
        self.loading = True
        if self.content and self.content.get("shots"):
            self.content = recalculate_timeline(self.content)
        shots = (self.content or {}).get("shots", [])
        self.table.setRowCount(len(shots))
        selected = 0
        for row, shot in enumerate(shots):
            self.fill_row(row)
            if shot["shot_id"] == shot_id:
                selected = row
        if shots:
            self.table.selectRow(selected)
            self.table.scrollToItem(self.table.item(selected, 0))
        self.update_summary()
        self.loading = False
        self.select_shot()

    def fill_row(self, row: int) -> None:
        shot = self.content["shots"][row]
        timeline = format_shot_timeline(shot)
        cast, action = self.cast_text(shot), one_line(shot["action"])
        cells = [
            (str(row + 1), timeline),
            (seconds_text(shot["duration_ms"]), timeline),
            (one_line(shot["shot_size"]), shot["shot_size"].strip()),
            (one_line(cast), cast.strip()),
            (action or "（未填写动作）", shot["action"].strip()),
        ]
        marked = shot["shot_id"] in self.marked
        for column, (text, tip) in enumerate(cells):
            item = self.table.item(row, column)
            if item is None:
                item = QTableWidgetItem()
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(row, column, item)
            item.setText(text)
            item.setToolTip("\n".join(filter(None, [tip, MARKED_NOTE if marked else ""])))
            item.setData(Qt.BackgroundRole, QColor(theme.ACCENT_TINT) if marked else None)
            item.setData(Qt.ForegroundRole, None)
        if not action:
            self.table.item(row, 4).setData(Qt.ForegroundRole, QColor(theme.DANGER))

    def update_summary(self) -> None:
        shots = (self.content or {}).get("shots", [])
        total = sum(shot["duration_ms"] for shot in shots)
        self.summary.setText(f"共 {len(shots)} 镜 · {seconds_text(total)}" if shots else "")

    def show_notice(self, text: str) -> None:
        self.notice.setText(text)
        self.notice.setVisible(bool(text))

    def selected_shots(self) -> list[str]:
        row = self.table.currentRow()
        shots = (self.content or {}).get("shots", [])
        return [shots[row]["shot_id"]] if 0 <= row < len(shots) else []

    def current(self) -> tuple[int, JsonObject] | None:
        row = self.table.currentRow()
        shots = (self.content or {}).get("shots", [])
        return (row, shots[row]) if 0 <= row < len(shots) else None

    # -- form ------------------------------------------------------------------------------

    def select_shot(self) -> None:
        if self.loading:
            return
        picked = self.current()
        self.loading = True
        self.form.setEnabled(picked is not None)
        for widget in (self.size_input, *self.detail_fields.values()):
            self.set_missing(widget, False)
        if picked is not None:
            row, shot = picked
            self.update_heading(row, shot)
            self.show_duration(shot["duration_ms"])
            self.size_input.setText(one_line(shot["shot_size"]))
            scene = shot.get("scene_ref")
            self.scene_label.setText(asset_label(scene, self.catalog) if scene else "无")
            props = "、".join(asset_label(ref, self.catalog) for ref in shot.get("props", []))
            self.props_label.setText(props or "无")
            for field, edit in self.detail_fields.items():
                edit.setPlainText(self.field_text(shot, field))
        else:
            shots = (self.content or {}).get("shots", [])
            self.heading.setText("在上面的列表里选一镜" if shots else "")
            self.size_input.clear()
            self.scene_label.clear()
            self.props_label.clear()
            for edit in self.detail_fields.values():
                edit.clear()
        self.loading = False
        self.selection_changed.emit()

    def field_text(self, shot: JsonObject, field: str) -> str:
        """What a box shows; a pasted line break around the text is left out, as on save."""
        if field == "dialogue":
            return format_shot_dialogue(shot, self.labels(shot)).strip()
        if field == "characters":
            return self.cast_text(shot).strip()
        return shot[field].strip()

    def update_heading(self, row: int, shot: JsonObject) -> None:
        text = f"第 {row + 1} 镜 · {format_shot_timeline(shot)}"
        if shot["shot_id"] in self.marked:
            text += f" · {MARKED_NOTE}"
        self.heading.setText(text)

    def show_duration(self, milliseconds: int) -> None:
        seconds = milliseconds / 1000
        low, high = DURATION_INPUT_RANGE_SECONDS
        self.duration_input.setRange(min(low, seconds), max(high, seconds))
        self.duration_input.setValue(seconds)

    def set_missing(self, widget: QWidget, missing: bool) -> None:
        if bool(widget.property("missing")) != missing:
            widget.setProperty("missing", missing)
            repolish(widget)

    def show_missing(self, index: int, fields: list[str]) -> None:
        """A save stopped at an empty required field: select that shot and outline the boxes."""
        self.table.selectRow(index)
        self.table.scrollToItem(self.table.item(index, 0))
        widgets = {"shot_size": self.size_input, **self.detail_fields}
        for field in fields:
            self.set_missing(widgets[field], True)

    def edit_duration(self, value: float) -> None:
        picked = None if self.loading else self.current()
        if picked is None:
            return
        picked[1]["duration_ms"] = max(1, round(value * 1000))
        self.content = recalculate_timeline(self.content)
        # Every later shot moves: refresh all rows, the total and this shot's heading.
        for row in range(self.table.rowCount()):
            self.fill_row(row)
        self.update_summary()
        row, shot = self.current()
        self.update_heading(row, shot)
        self.changed.emit()

    def edit_size(self, text: str) -> None:
        picked = None if self.loading else self.current()
        if picked is None:
            return
        row, shot = picked
        shot["shot_size"] = text
        self.set_missing(self.size_input, False)
        self.fill_row(row)
        self.changed.emit()

    def edit_field(self, field: str) -> None:
        picked = None if self.loading else self.current()
        if picked is None:
            return
        row, shot = picked
        edit = self.detail_fields[field]
        key = {"dialogue": "dialogue_text", "characters": "character_text"}.get(field, field)
        shot[key] = edit.toPlainText()
        self.set_missing(edit, False)
        self.fill_row(row)
        self.changed.emit()

    # -- shot operations -------------------------------------------------------------------

    def show_row_menu(self, position: object) -> None:
        row = self.table.indexAt(position).row()
        if row < 0:
            return
        self.table.selectRow(row)
        menu = QMenu(self)
        menu.addAction("在后面添加镜头", self.add_shot)
        menu.addAction("拆成两镜", self.split)
        merge = menu.addAction("与下一镜合并", self.merge)
        merge.setEnabled(row + 1 < self.table.rowCount())
        menu.addAction("删除镜头", self.delete_shot)
        menu.exec(self.table.viewport().mapToGlobal(position))

    def move_shot(self, source: int, target: int) -> None:
        shots = (self.content or {}).get("shots", [])
        if not 0 <= source < len(shots) or not 0 <= target < len(shots):
            return
        shot = shots.pop(source)
        shots.insert(target, shot)
        self.show_notice("")
        self.refresh_table(shot["shot_id"])
        self.changed.emit()

    def add_shot(self) -> None:
        """A blank shot after the selected one, in its scene and part of the script."""
        shots = (self.content or {}).get("shots", [])
        if not shots:
            return
        row = max(0, self.table.currentRow())
        base = shots[row]
        shot = {
            "shot_id": identity("shot"),
            "order": row + 2,
            "start_ms": 0,
            "end_ms": 0,
            "duration_ms": 5000,
            "scene_ref": deepcopy(base["scene_ref"]),
            "characters": [],
            "props": [],
            "action": "",
            "dialogue": [],
            "narration": "",
            "sound": "",
            "shot_size": "",
            "camera_movement": "",
            # Saving needs every shot tied to the script; the new one belongs where it was added.
            "source_refs": deepcopy(base["source_refs"]),
            "video_block_refs": list(base["video_block_refs"]),
        }
        shots.insert(row + 1, shot)
        self.show_notice("")
        self.refresh_table(shot["shot_id"])
        self.changed.emit()

    def split(self) -> None:
        shots = (self.content or {}).get("shots", [])
        row = self.table.currentRow()
        if row < 0:
            return
        first = shots[row]
        if first["duration_ms"] < 2000:
            self.show_notice("这一镜不到 2 秒，拆开后每镜不足 1 秒，不能再拆。")
            return
        second = deepcopy(first)
        second["shot_id"] = identity("shot")
        second["duration_ms"] = first["duration_ms"] // 2 // 100 * 100
        first["duration_ms"] -= second["duration_ms"]
        # Each line is said once: dialogue, narration and sound stay with the first half.
        second.update(dialogue=[], narration="", sound="")
        second.pop("dialogue_text", None)
        shots.insert(row + 1, second)
        self.refresh_table(second["shot_id"])
        self.show_notice(
            f"第 {row + 1}、{row + 2} 镜的动作相同，请分别改写；"
            f"对白、旁白和音效留在第 {row + 1} 镜。"
        )
        self.changed.emit()

    def merge(self) -> None:
        shots = (self.content or {}).get("shots", [])
        row = self.table.currentRow()
        if row < 0 or row + 1 >= len(shots):
            return
        first, second = shots[row : row + 2]
        if first["scene_ref"] != second["scene_ref"]:
            self.show_notice("这两镜在不同场景，不能直接合并；需要时在与 AI 的讨论里说明合并后的场景。")
            return
        # Typed names and lines are not parsed until saving; carry the typed text over whole.
        if "dialogue_text" in first or "dialogue_text" in second:
            boxes = [format_shot_dialogue(s, self.labels(s)) for s in (first, second)]
            first["dialogue_text"] = "\n".join(box for box in boxes if box)
        if "character_text" in first or "character_text" in second:
            names = split_names(self.cast_text(first)) + split_names(self.cast_text(second))
            first["character_text"] = "、".join(dict.fromkeys(names))
        first["duration_ms"] += second["duration_ms"]
        for field in ("action", "narration", "sound"):
            first[field] = join_text(first[field], second[field])
        for field in ("characters", "props", "source_refs"):
            first[field] += [value for value in second[field] if value not in first[field]]
        first["dialogue"] += second["dialogue"]
        labels = {**second.get("character_labels", {}), **first.get("character_labels", {})}
        if labels:
            first["character_labels"] = labels
        first["video_block_refs"] = list(
            dict.fromkeys(first["video_block_refs"] + second["video_block_refs"])
        )
        del shots[row + 1]
        self.refresh_table(first["shot_id"])
        self.show_notice(
            f"第 {row + 1}、{row + 2} 镜已合并为第 {row + 1} 镜，"
            "两镜的动作已接在一起，请改写成适合一镜的动作。"
        )
        self.changed.emit()

    def delete_shot(self) -> None:
        row = self.table.currentRow()
        if self.content and row >= 0:
            if len(self.content["shots"]) == 1:
                self.show_notice("至少保留一个镜头。")
                return
            removed = self.content["shots"].pop(row)
            neighbour = self.content["shots"][min(row, len(self.content["shots"]) - 1)]
            neighbour["video_block_refs"] = list(
                dict.fromkeys(neighbour["video_block_refs"] + removed["video_block_refs"])
            )
            neighbour["source_refs"] += [
                ref for ref in removed["source_refs"] if ref not in neighbour["source_refs"]
            ]
            self.show_notice("")
            self.refresh_table(neighbour["shot_id"])
            self.changed.emit()
