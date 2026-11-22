"""Modal lore editor. A draft stays attached to its original project snapshot."""
from __future__ import annotations

from copy import deepcopy
import json

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from core.generation.lore_repository import LoreRepository, MappingRequired, encode
from core.lore_localization import field_label, display_value, file_label, describe_value, display_message

ROLE_LABELS = {"protagonist": "主角", "deuteragonist": "第二主角", "antagonist": "反派", "supporting": "配角"}


class ValueForm(QWidget):
    """Recursive typed fields, including legacy arrays; never round-trips through a projection."""
    changed = Signal()

    def __init__(self, value, key="", parent=None):
        super().__init__(parent)
        self.original = deepcopy(value)
        self.key = key
        identity_field = key in {"name", "character_name", "full_name", "faction_name", "id", "character1", "character2", "character", "faction", "with"}
        self.displayed = ("\n".join(display_value(v) for v in value) if isinstance(value, list) and all(isinstance(v, str) for v in value)
                          else str(value) if identity_field else display_value(value))
        self.children_fields = {}
        self.control = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if isinstance(value, dict) or (isinstance(value, list) and not all(isinstance(v, str) for v in value)):
            form = QFormLayout()
            layout.addLayout(form)
            items = value.items() if isinstance(value, dict) else enumerate(value)
            for index, (child_key, child_value) in enumerate(items):
                child = ValueForm(child_value, str(child_key))
                self.children_fields[child_key] = child
                child.changed.connect(self.changed)
                label = field_label(child_key, index)
                if isinstance(child_value, dict):
                    box = QGroupBox(label if not isinstance(child_key, int) else f"第 {child_key + 1} 项 · {child_value.get('name', '')}")
                    QVBoxLayout(box).addWidget(child)
                    form.addRow(box)
                else:
                    form.addRow(label, child)
        elif key in {"id", "schema_version", "origin", "metadata"} or value is None:
            layout.addWidget(QLabel(self.displayed))
        elif isinstance(value, bool):
            self.control = QCheckBox()
            self.control.setChecked(value)
            self.control.toggled.connect(self.changed)
            layout.addWidget(self.control)
        elif key == "role" and isinstance(value, str):
            self.control = QComboBox()
            for role in dict.fromkeys([value, *ROLE_LABELS]):
                self.control.addItem(display_value(role), role)
            self.control.currentIndexChanged.connect(self.changed)
            layout.addWidget(self.control)
        elif key == "gender" and value in {"Male", "Female", "male", "female", "男", "女"}:
            self.control = QComboBox()
            choices = ("Male", "Female") if value in {"Male", "Female"} else (("male", "female") if value in {"male", "female"} else ("男", "女"))
            for choice in choices:
                self.control.addItem(display_value(choice), choice)
            self.control.setCurrentIndex(choices.index(value))
            self.control.currentIndexChanged.connect(self.changed)
            layout.addWidget(self.control)
        elif isinstance(value, list) or (isinstance(value, str) and (len(value) > 80 or key in {
            "description", "background", "backstory_summary", "goal", "motivation", "world_constraints",
        })):
            self.control = QPlainTextEdit(self.displayed)
            if isinstance(value, list):
                self.control.setPlaceholderText("每行填写一项")
            self.control.setMinimumHeight(85)
            self.control.setMaximumHeight(170)
            self.control.textChanged.connect(self.changed)
            layout.addWidget(self.control)
        else:
            self.control = QLineEdit(self.displayed)
            self.control.textChanged.connect(self.changed)
            layout.addWidget(self.control)

    def value(self):
        if self.children_fields:
            if isinstance(self.original, dict):
                return {k: child.value() for k, child in self.children_fields.items()}
            return [child.value() for child in self.children_fields.values()]
        if self.control is None:
            return deepcopy(self.original)
        if isinstance(self.control, QCheckBox):
            return self.control.isChecked()
        if isinstance(self.control, QComboBox):
            return self.control.currentData()
        text = self.control.toPlainText() if isinstance(self.control, QPlainTextEdit) else self.control.text()
        if text == self.displayed:
            return deepcopy(self.original)
        if isinstance(self.original, list):
            lines = [v.strip() for v in text.splitlines() if v.strip()]
            return [self.original[i] if i < len(self.original) and display_value(self.original[i]) == v else v
                    for i, v in enumerate(lines)]
        if isinstance(self.original, (int, float)):
            try:
                return type(self.original)(text)
            except ValueError as exc:
                raise ValueError(f"{field_label(self.key)}需要填写有效的" + ("整数" if isinstance(self.original, int) else "数字")) from exc
        return text


class LoreEditor(QDialog):
    saved = Signal(object)

    def __init__(self, output_dir, relative_path, object_path=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑设定")
        self.resize(880, 720)
        self.setModal(True)
        self.repository = LoreRepository(output_dir)
        self.snapshot = self.repository.load_snapshot()
        self.relative_path = relative_path
        self.object_path = object_path
        self.mappings = {}
        self.generation_busy = False
        self._saved = False
        source = self.snapshot.files[relative_path]
        self.payload = json.loads(source) if relative_path.endswith(".json") else source
        self.original = self.payload
        for key in object_path:
            self.original = self.original[key]
        self.original = deepcopy(self.original)
        outer = QVBoxLayout(self)
        title = file_label(relative_path)
        if isinstance(self.original, dict) and self.original.get("name"):
            title += " · " + str(self.original["name"])
        elif object_path and object_path[0] == "relationships":
            title = "人物关系"
        self.setWindowTitle("编辑" + title)
        outer.addWidget(QLabel(title))
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        if isinstance(self.original, str) and relative_path.endswith(".md"):
            self.field = QPlainTextEdit(self.original)
            self.field.textChanged.connect(self._changed)
            self.tabs.addTab(self.field, "编辑正文")
        else:
            self.field = ValueForm(self.original)
            self.field.changed.connect(self._changed)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(self.field)
            self.tabs.addTab(scroll, "编辑字段")
        self.preview_view = QTextBrowser()
        self.preview_view.setOpenExternalLinks(False)
        self.tabs.addTab(self.preview_view, "预览 / 影响范围")
        self.tabs.currentChanged.connect(self._preview)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        outer.addWidget(self.message)
        actions = QHBoxLayout()
        self.dirty_label = QLabel("未修改")
        actions.addWidget(self.dirty_label, 1)
        self.save_button = QPushButton("保存")
        self.save_button.clicked.connect(self.save)
        actions.addWidget(self.save_button)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        outer.addLayout(actions)

    def value(self):
        return self.field.toPlainText() if isinstance(self.field, QPlainTextEdit) else self.field.value()

    @property
    def dirty(self):
        try:
            return not self._saved and self.value() != self.original
        except ValueError:
            return True

    def _changed(self):
        self.dirty_label.setText("未保存" if self.dirty else "未修改")

    def edits(self):
        value = self.value()
        if self.object_path:
            payload = deepcopy(self.payload)
            target = payload
            for key in self.object_path[:-1]:
                target = target[key]
            target[self.object_path[-1]] = value
        else:
            payload = value
        return {self.relative_path: encode(payload) if self.relative_path.endswith(".json") else payload}

    def _preview(self, index):
        if index != 1:
            return
        try:
            preview = self.repository.preview(self.snapshot, self.edits(), self.mappings)
            value = self.value()
            review = "\n\n".join(display_message(item) for item in preview.review_items) or "当前没有待核对事项。"
            if isinstance(value, str):
                self.preview_view.setMarkdown(value + "\n\n---\n### 待核对\n" + review)
            else:
                self.preview_view.setPlainText(describe_value(value) + "\n\n待核对\n" + review)
        except Exception as exc:
            self.preview_view.setPlainText(display_message(exc))

    def set_busy(self, busy):
        self.generation_busy = busy
        self.save_button.setEnabled(not busy)
        self.field.setEnabled(not busy)

    def save(self):
        if self.generation_busy:
            self.message.setText("生成任务运行中，请等待完成后保存。")
            return False
        while True:
            try:
                result = self.repository.save(self.snapshot, self.edits(), self.mappings)
            except MappingRequired as exc:
                labels = [f"第 {index + 1} 项：{name}" for index, name in exc.candidates]
                if not labels:
                    self.message.setText("名称登记表没有可关联条目，请先修复登记表后再改名。草稿仍保留。")
                    return False
                choice, ok = QInputDialog.getItem(self, "选择名称登记条目", "无法唯一确定对应的名称登记，请选择与当前人物或势力对应的条目。", labels, 0, False)
                if not ok:
                    return False
                self.mappings[exc.key] = exc.candidates[labels.index(choice)][0]
                continue
            except Exception as exc:
                self.mappings.clear()
                self.message.setText(f"保存失败：{display_message(exc)}")
                return False
            self._saved = True
            self.saved.emit(result)
            self.accept()
            return True

    def resolve_draft(self):
        if not self.dirty:
            return True
        box = QMessageBox(self)
        box.setWindowTitle("设定尚未保存")
        box.setText("是否保存当前修改？")
        save = box.addButton("保存", QMessageBox.AcceptRole)
        discard = box.addButton("放弃", QMessageBox.DestructiveRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is save:
            return self.save()
        return box.clickedButton() is discard

    def reject(self):
        if self.resolve_draft():
            super().reject()
