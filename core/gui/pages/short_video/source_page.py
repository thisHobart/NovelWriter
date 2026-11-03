"""Article input, track selection and source range editing."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from core.short_video.v2.models import (
    TRACKS,
    WorkspaceError,
    default_config,
    require,
)
from core.short_video.v2.project_service import ProjectService
from core.short_video.v2.source import (
    qt_to_codepoint,
    read_source_file,
    source_content,
)

from .ui_helpers import create_action_button

if TYPE_CHECKING:
    from .workbench import Workbench


class SourcePage(QWidget):
    def __init__(self, workbench: Workbench) -> None:
        super().__init__()
        self.workbench, self.dirty, self.revision, self.ranges, self.loaded_source = (
            workbench,
            False,
            0,
            [],
            None,
        )
        self.input_refs = {}
        layout = QVBoxLayout(self)
        heading = QHBoxLayout()
        heading.addWidget(QLabel("原文"), 1)
        self.import_button = create_action_button(
            "导入 TXT / MD", self.import_file, heading, workbench
        )
        layout.addLayout(heading)
        self.article = QPlainTextEdit()
        self.article.setPlaceholderText("粘贴原始文章。文章内容作为改编素材。")
        self.article.setMinimumHeight(230)
        layout.addWidget(self.article, 1)
        self.article.textChanged.connect(self.source_changed)
        self.validation_label = QLabel()
        self.validation_label.setStyleSheet("color:#c74736")
        layout.addWidget(self.validation_label)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("改编范围"))
        self.full_radio, self.selection_radio = QRadioButton("全文"), QRadioButton("所选文字")
        self.full_radio.setChecked(True)
        self.full_radio.clicked.connect(lambda: workbench.perform(self.full_range))
        self.selection_radio.clicked.connect(lambda: workbench.perform(self.add_range))
        bar.addWidget(self.full_radio)
        bar.addWidget(self.selection_radio)
        self.range_label = QLabel()
        bar.addWidget(self.range_label, 1)
        layout.addLayout(bar)
        self.track_boxes = {}
        bar = QHBoxLayout()
        bar.addWidget(QLabel("赛道"))
        for key, label in [("themes", "主题"), ("settings", "设定"), ("backgrounds", "背景")]:
            button = QPushButton(label)
            menu = QMenu(button)
            self.track_boxes[key] = []
            for value in TRACKS[key]:
                check = QCheckBox(value)
                check.toggled.connect(self.mark_dirty)
                check.toggled.connect(
                    lambda _, b=button, k=key, title=label: b.setText(
                        title + f" · {sum(c.isChecked() for c in self.track_boxes[k])}"
                    )
                )
                action = QWidgetAction(menu)
                action.setDefaultWidget(check)
                menu.addAction(action)
                self.track_boxes[key].append(check)
            button.setMenu(menu)
            bar.addWidget(button)
        bar.addStretch()
        layout.addLayout(bar)
        self.more_button = QPushButton("更多创作要求")
        self.more_button.setCheckable(True)
        layout.addWidget(self.more_button)
        self.requirements = QWidget()
        form = QFormLayout(self.requirements)
        self.title = QLineEdit()
        self.title.setPlaceholderText("作品标题")
        self.title.textChanged.connect(self.mark_dirty)
        form.addRow("作品标题", self.title)
        self.fields = {}
        for key, label in [
            ("audience", "目标观众"),
            ("tone", "基调"),
            ("narrative_person", "人称"),
            ("target_words", "目标字数（可留空）"),
            ("target_duration_ms", "目标时长（秒，可留空）"),
            ("required", "必需内容（；分隔）"),
            ("forbidden", "禁止内容（；分隔）"),
        ]:
            if key == "narrative_person":
                edit = QComboBox()
                for title, value in [
                    ("沿用原文", "source"),
                    ("第一人称", "first"),
                    ("第三人称", "third"),
                ]:
                    edit.addItem(title, value)
                edit.currentIndexChanged.connect(self.mark_dirty)
            else:
                edit = QLineEdit()
                edit.textChanged.connect(self.mark_dirty)
            form.addRow(label, edit)
            self.fields[key] = edit
        self.requirements.hide()
        self.more_button.toggled.connect(self.requirements.setVisible)
        layout.addWidget(self.requirements)
        bar = QHBoxLayout()
        self.save_button = create_action_button("保存", self.save, bar, workbench)
        bar.addStretch()
        self.generate_button = create_action_button(
            "生成小说推文 →", self.generate, bar, workbench, primary=True
        )
        layout.addLayout(bar)

    def generate(self) -> None:
        if self.save():
            self.workbench.start_creation("novel_copy", self.generate_button)

    def mark_dirty(self, *_: object) -> None:
        self.dirty = True

    def source_changed(self) -> None:
        self.dirty, self.ranges, self.loaded_source = True, [], None
        self.full_radio.setChecked(True)
        self.validation_label.clear()
        self.range_label.setText(f"共 {len(self.article.toPlainText())} 字")

    def clear_tracks(self) -> None:
        for boxes in self.track_boxes.values():
            for check in boxes:
                check.setChecked(False)

    def add_range(self) -> None:
        cursor = self.article.textCursor()
        value = self.article.toPlainText()
        full_text = not self.ranges or self.ranges == [{"start": 0, "end": len(value)}]
        if not cursor.hasSelection():
            self.full_radio.setChecked(full_text)
            self.selection_radio.setChecked(not full_text)
            raise WorkspaceError("INVALID_INPUT", "请先在原文中选择文字")
        if full_text:
            self.ranges = []
        self.ranges.append(
            {
                "start": qt_to_codepoint(value, cursor.selectionStart()),
                "end": qt_to_codepoint(value, cursor.selectionEnd()),
            }
        )
        from core.short_video.v2.source import validate_ranges

        try:
            self.ranges = validate_ranges(value, self.ranges)
        except WorkspaceError:
            self.ranges.pop()
            raise
        self.selection_radio.setChecked(True)
        self.range_label.setText(
            f"已选 {sum(r['end'] - r['start'] for r in self.ranges)} 字 / 全文 {len(value)} 字"
        )
        self.dirty = True

    def full_range(self) -> None:
        self.ranges, self.dirty = [], True
        self.full_radio.setChecked(True)
        self.range_label.setText(f"共 {len(self.article.toPlainText())} 字")

    def import_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "导入原始文章", "", "文章 (*.txt *.md)")
        if not path:
            return
        try:
            value = read_source_file(path)
        except WorkspaceError as exc:
            if exc.code != "SOURCE_ENCODING_ERROR":
                raise
            encoding, ok = QInputDialog.getItem(
                self,
                "选择文章编码",
                "原文件不是有效 UTF-8，请选择编码",
                ["gb18030", "utf-16"],
                0,
                False,
            )
            if not ok:
                return
            value = read_source_file(path, encoding=encoding)
        self.article.setPlainText(value["normalized_text"])
        self.loaded_source = value

    def save(self) -> bool:
        if not self.article.toPlainText().strip():
            self.validation_label.setText("请先粘贴或导入原文")
            self.article.setFocus()
            return False
        state = self.workbench.repository.load()
        require(
            {key: state["current"].get(key) for key in ("config", "source", "selection")}
            == self.input_refs,
            "REVISION_CONFLICT",
            "文章或创作要求已被其他操作更新；编辑保留，请核对后再保存",
        )
        current = self.workbench.repository.current("config", state=state)
        config = deepcopy(current["content"]) if current else default_config()
        config["title"] = self.title.text()
        config["text_provider"] = self.workbench.model()
        config["tracks"] = {
            k: [c.text() for c in boxes if c.isChecked()] for k, boxes in self.track_boxes.items()
        }
        for key, edit in self.fields.items():
            if key == "narrative_person":
                config[key] = edit.currentData()
                continue
            raw = edit.text().strip()
            if key in {"target_words", "target_duration_ms"}:
                require(
                    not raw or raw.isdigit() and int(raw) > 0,
                    "INVALID_INPUT",
                    "目标字数／时长需为正整数或留空",
                )
                config[key] = (
                    int(raw) * (1000 if key == "target_duration_ms" else 1) if raw else None
                )
            else:
                config[key] = (
                    [s.strip() for s in raw.split("；") if s.strip()]
                    if key in {"required", "forbidden", "permitted_adaptations"}
                    else raw
                )
        content = self.loaded_source or source_content(
            self.article.toPlainText(), title=config["title"]
        )
        ProjectService(self.workbench.repository).save_inputs(
            config, content, self.ranges or None, expected_revision=state["revision"]
        )
        self.dirty = False
        self.workbench.refresh()
        self.workbench.notice("文章和创作要求已保存；依赖旧输入的稿件会提示过期。")
        return True

    def refresh(self) -> None:
        if self.dirty:
            return
        state = self.workbench.repository.load()
        self.revision = state["revision"]
        self.input_refs = {
            key: state["current"].get(key) for key in ("config", "source", "selection")
        }
        current = self.workbench.repository.current("config", state=state)
        config = current["content"] if current else default_config()
        self.title.setText(config["title"])
        for key, edit in self.fields.items():
            value = config.get(key)
            if key == "narrative_person":
                edit.setCurrentIndex(edit.findData(value))
                continue
            if key == "target_duration_ms" and value:
                value //= 1000
            edit.setText(
                "；".join(value)
                if isinstance(value, list)
                else str(value)
                if value is not None
                else ""
            )
        for key, boxes in self.track_boxes.items():
            for check in boxes:
                check.setChecked(check.text() in config["tracks"][key])
        source = self.workbench.repository.current("source", state=state)
        self.article.setPlainText(source["content"]["normalized_text"] if source else "")
        self.loaded_source = deepcopy(source["content"]) if source else None
        selection = self.workbench.repository.current("selection", state=state)
        self.ranges = deepcopy(selection["content"]["ranges"]) if selection else []
        length = len(self.article.toPlainText())
        selected = bool(self.ranges and self.ranges != [{"start": 0, "end": length}])
        self.selection_radio.setChecked(selected)
        self.full_radio.setChecked(not selected)
        self.range_label.setText(
            f"共 {sum(r['end'] - r['start'] for r in self.ranges) if selected else length} 字"
        )
        self.dirty = False
