"""Tree editor for story fields with a readable Markdown preview."""
from copy import deepcopy
from uuid import uuid4

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem,
                               QTextBrowser, QSplitter, QComboBox, QPlainTextEdit, QTreeWidgetItemIterator)
from core.gui.widgets import secondary_button
from core.short_video.validation import to_markdown


LABELS = {"title": "标题", "characters": "人物", "locations": "地点", "conflict": "核心冲突",
          "beats": "故事节拍", "ending": "结尾", "id": "编号", "name": "名称", "motivation": "动机",
          "appearance": "外观", "action": "可见动作", "duration_seconds": "预计秒数", "scenes": "场次",
          "location_id": "地点", "character_ids": "出场人物", "character_id": "说话人物",
          "dialogue": "对白", "text": "台词", "narration": "旁白", "sound": "声音"}


class StoryEditor(QWidget):
    changed = Signal()

    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind, self._content, self._loading = kind, None, False
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        self.add_button = secondary_button("添加选中列表的条目")
        self.remove_button = secondary_button("删除选中条目")
        toolbar.addWidget(self.add_button)
        toolbar.addWidget(self.remove_button)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        split = QSplitter()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["内容", "编辑值"])
        self.tree.setColumnWidth(0, 150)
        self.tree.setWordWrap(True)
        self.preview = QTextBrowser()
        self.preview.setOpenExternalLinks(False)
        split.addWidget(self.tree)
        split.addWidget(self.preview)
        split.setSizes([420, 500])
        layout.addWidget(split)
        self.text_editor = QPlainTextEdit()
        self.text_editor.setPlaceholderText("选中动作、对白等文字，在这里编辑完整内容。")
        self.text_editor.setMaximumHeight(100)
        layout.addWidget(self.text_editor)
        self._text_path = None
        self.text_editor.textChanged.connect(self._long_edit)
        self.tree.itemSelectionChanged.connect(self._selection)
        self.tree.itemChanged.connect(self._edited)
        self.add_button.clicked.connect(self._add)
        self.remove_button.clicked.connect(self._remove)

    def value(self):
        if self._content is None:
            raise ValueError("尚无可编辑内容，请先生成")
        return deepcopy(self._content)

    def set_content(self, content):
        self._content = deepcopy(content)
        self._loading = True
        self.tree.clear()
        self._text_path = None
        self.text_editor.clear()
        if content:
            for key, value in content.items():
                self._append(self.tree.invisibleRootItem(), key, value, [key])
        self.tree.expandToDepth(1)
        self._loading = False
        self._preview()

    def _get(self, path):
        value = self._content
        for part in path:
            value = value[part]
        return value

    def _set(self, path, value):
        parent = self._get(path[:-1]) if len(path) > 1 else self._content
        parent[path[-1]] = value

    def _append(self, parent, key, value, path):
        if key == "id":
            return  # References are edited by human-readable name; internal ids stay invisible.
        label = LABELS.get(key, f"第 {key + 1} 条" if isinstance(key, int) else str(key))
        item = QTreeWidgetItem(parent, [label, ""])
        item.setData(0, Qt.UserRole, path)
        if isinstance(value, dict):
            for child, inner in value.items():
                self._append(item, child, inner, path + [child])
        elif isinstance(value, list):
            for index, inner in enumerate(value):
                self._append(item, index, inner, path + [index])
        else:
            reference = key in {"location_id", "character_id"} or (len(path) > 1 and path[-2] == "character_ids")
            if reference:
                choices = self._content["locations" if key == "location_id" else "characters"]
                combo = QComboBox()
                for row in choices:
                    combo.addItem(row["name"], row["id"])
                index = combo.findData(value)
                if index < 0:
                    combo.addItem(f"未知：{value}", value)
                    index = combo.count() - 1
                combo.setCurrentIndex(index)
                combo.currentIndexChanged.connect(lambda _, p=path, c=combo: self._reference(p, c.currentData()))
                self.tree.setItemWidget(item, 1, combo)
            else:
                item.setText(1, str(value))
                if key != "id":
                    item.setFlags(item.flags() | Qt.ItemIsEditable)

    def _reference(self, path, value):
        if not self._loading:
            self._set(path, value)
            self._preview()
            self.changed.emit()

    def _selection(self):
        item = self.tree.currentItem()
        self._text_path = None
        if item:
            path = item.data(0, Qt.UserRole)
            value = self._get(path)
            if isinstance(value, str) and path[-1] not in {"id", "location_id", "character_id"} and path[-2:-1] != ["character_ids"]:
                self._text_path = path
        blocked = self.text_editor.blockSignals(True)
        self.text_editor.setPlainText(self._get(self._text_path) if self._text_path else "")
        self.text_editor.setEnabled(self._text_path is not None)
        self.text_editor.blockSignals(blocked)

    def _long_edit(self):
        if self._loading or self._text_path is None:
            return
        value = self.text_editor.toPlainText()
        self._set(self._text_path, value)
        item = self.tree.currentItem()
        if item:
            self._loading = True
            item.setText(1, value)
            self._loading = False
        self._preview()
        self._refresh_reference_labels()
        self.changed.emit()

    def _edited(self, item, column):
        if self._loading or column != 1:
            return
        path = item.data(0, Qt.UserRole)
        old = self._get(path)
        text = item.text(1)
        if isinstance(old, (int, float)):
            try:
                value = float(text)
            except ValueError:
                self._loading = True
                item.setText(1, str(old))
                self._loading = False
                return
        else:
            value = text
        self._set(path, value)
        self._selection()
        self._preview()
        self._refresh_reference_labels()
        self.changed.emit()

    def _refresh_reference_labels(self):
        """Keep visible names current while retaining stable reference ids."""
        iterator = QTreeWidgetItemIterator(self.tree)
        while iterator.value():
            item = iterator.value()
            combo = self.tree.itemWidget(item, 1)
            if isinstance(combo, QComboBox):
                path = item.data(0, Qt.UserRole)
                key = "locations" if path[-1] == "location_id" else "characters"
                names = {row["id"]: row["name"] for row in self._content[key]}
                for index in range(combo.count()):
                    identity = combo.itemData(index)
                    combo.setItemText(index, names.get(identity, f"未知：{identity}"))
            iterator += 1

    def _preview(self):
        if self._content is None:
            self.preview.setPlainText("先生成内容，再在左侧编辑并保存。")
            return
        try:
            self.preview.setMarkdown(to_markdown(self._content, self.kind))
        except (ValueError, KeyError, TypeError):
            self.preview.setPlainText("当前内容尚不完整；请检查人物、地点与场次。")

    def _add(self):
        item = self.tree.currentItem()
        if not item or self._content is None:
            return
        path = item.data(0, Qt.UserRole)
        if not isinstance(self._get(path), list) and path and isinstance(path[-1], int):
            path = path[:-1]
        values = self._get(path)
        if not isinstance(values, list):
            return
        key = path[-1]
        identity = uuid4().hex[:8]
        first_character = self._content["characters"][0]["id"] if self._content["characters"] else ""
        templates = {
            "characters": {"id": identity, "name": "新人物", "motivation": "请填写", "appearance": "请填写"},
            "locations": {"id": identity, "name": "新地点"},
            "beats": {"id": identity, "action": "请填写可见动作", "duration_seconds": 10},
            "scenes": {"id": identity, "location_id": self._content["locations"][0]["id"] if self._content["locations"] else "",
                       "character_ids": [first_character], "action": "请填写可见动作", "dialogue": [],
                       "narration": "", "sound": "", "duration_seconds": 10},
            "dialogue": {"character_id": first_character, "text": "请填写对白"},
            "character_ids": first_character,
        }
        if key in templates:
            values.append(templates[key])
            self.set_content(self._content)
            self.changed.emit()

    def _remove(self):
        item = self.tree.currentItem()
        if item:
            path = item.data(0, Qt.UserRole)
            if isinstance(path[-1], int):
                del self._get(path[:-1])[path[-1]]
                self.set_content(self._content)
                self.changed.emit()
