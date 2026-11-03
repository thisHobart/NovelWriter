"""Plain positive/negative prompt editing with object and view selectors."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from core.short_video.v2.contracts import JsonObject
from core.short_video.v2.models import ASSET_KINDS, WorkspaceError, require
from core.short_video.v2.workflow import WorkflowService, WorkspaceWork

from .ui_helpers import create_action_button

if TYPE_CHECKING:
    from .workbench import Workbench


class PromptEditor(QWidget):
    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.content, self.index, self.bundle = None, 0, False
        layout = QVBoxLayout(self)
        self.positive = QPlainTextEdit()
        self.negative = QPlainTextEdit()
        for label, edit in [("正向提示词", self.positive), ("负面提示词", self.negative)]:
            layout.addWidget(QLabel(label))
            layout.addWidget(edit, 2 if edit is self.positive else 1)
            copy = QPushButton("复制正向" if edit is self.positive else "复制负面")
            copy.clicked.connect(
                lambda _, e=edit: QApplication.clipboard().setText(e.toPlainText())
            )
            layout.addWidget(copy)
            copy.setMaximumWidth(120)
            edit.textChanged.connect(self.changed)

    def value(self) -> JsonObject:
        require(self.content is not None, "INVALID_INPUT", "请先生成提示词")
        value = deepcopy(self.content)
        items = value["shots" if self.bundle else "views"]
        item = items[self.index]
        item["resolved_prompt" if self.bundle else "positive_prompt"] = self.positive.toPlainText()
        item["negative_prompt"] = self.negative.toPlainText()
        return value

    def set_content(self, content: JsonObject | None, index: int = 0) -> None:
        self.content, self.index = deepcopy(content), index
        self.bundle = bool(content and "shots" in content)
        items = (content or {}).get("shots" if self.bundle else "views", [])
        for edit, field in [
            (self.positive, "resolved_prompt" if self.bundle else "positive_prompt"),
            (self.negative, "negative_prompt"),
        ]:
            edit.blockSignals(True)
            edit.setPlainText(items[index][field] if items else "")
            edit.setEnabled(bool(items))
            edit.blockSignals(False)


class PromptPage(QWidget):
    def __init__(self, workbench: Workbench) -> None:
        super().__init__()
        self.workbench, self.dirty, self.record = workbench, False, None
        self.displayed_key, self.displayed_view = None, None
        self.displayed_selection = (0, None, None)
        layout = QVBoxLayout(self)
        self.tabs = QTabBar()
        for title in ["人物", "场景", "道具", "镜头"]:
            self.tabs.addTab(title)
        layout.addWidget(self.tabs)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("对象"))
        self.objects, self.variants, self.views = QComboBox(), QComboBox(), QComboBox()
        bar.addWidget(self.objects, 2)
        bar.addWidget(self.variants, 1)
        bar.addWidget(QLabel("视图"))
        bar.addWidget(self.views, 1)
        layout.addLayout(bar)
        self.status = QLabel("从分镜页生成资产提示词。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.editor = PromptEditor()
        self.editor.changed.connect(self.mark_dirty)
        layout.addWidget(self.editor, 1)
        bar = QHBoxLayout()
        self.save_button = create_action_button("保存", self.save, bar, workbench)
        self.generate_button = create_action_button("重新生成", self.generate, bar, workbench)
        more = QPushButton("更多")
        menu = QMenu(more)
        menu.addAction("历史版本", lambda: workbench.perform(self.restore_version))
        more.setMenu(menu)
        bar.addWidget(more)
        bar.addStretch()
        create_action_button("导出", workbench.export, bar, workbench, primary=True)
        layout.addLayout(bar)
        self.tabs.currentChanged.connect(lambda: workbench.perform(self.change_object))
        self.objects.currentIndexChanged.connect(lambda: workbench.perform(self.change_object))
        self.variants.currentIndexChanged.connect(lambda: workbench.perform(self.change_object))
        self.views.currentIndexChanged.connect(lambda: workbench.perform(self.change_view))

    def mark_dirty(self) -> None:
        self.dirty = True
        self.status.setText("有未保存修改")

    def key(self) -> str:
        return self.variants.currentData() or "prompt_bundle"

    def change_object(self) -> None:
        if not self.workbench.resolve_page(self):
            for widget in (self.tabs, self.objects, self.variants):
                widget.blockSignals(True)
            tab, entity, key = self.displayed_selection
            self.tabs.setCurrentIndex(tab)
            self.objects.setCurrentIndex(self.objects.findData(entity))
            self.variants.setCurrentIndex(self.variants.findData(key))
            for widget in (self.tabs, self.objects, self.variants):
                widget.blockSignals(False)
            return
        self.refresh()

    def change_view(self) -> None:
        index = self.views.currentIndex()
        if self.dirty and not self.workbench.resolve_page(self):
            self.views.blockSignals(True)
            self.views.setCurrentIndex(self.displayed_view)
            self.views.blockSignals(False)
            return
        self.displayed_view = max(0, index)
        self.views.blockSignals(True)
        self.views.setCurrentIndex(self.displayed_view)
        self.views.blockSignals(False)
        self.editor.set_content(
            self.record["content"] if self.record else None, self.displayed_view
        )

    def refresh(self, selected_key: str | None = None) -> None:
        if self.dirty:
            return
        state = self.workbench.repository.load()
        entity_id = self.objects.currentData()
        selected_key = selected_key or self.key()
        for combo in (self.objects, self.variants, self.views):
            combo.blockSignals(True)
        self.objects.clear()
        self.variants.clear()
        self.views.clear()
        kind = ["character", "scene", "prop", "shot"][self.tabs.currentIndex()]
        records = [
            self.workbench.repository.record(ref, state=state)
            for key, ref in state["current"].items()
            if kind != "shot" and key.startswith(ASSET_KINDS[kind] + ":")
        ]
        entities = {}
        for record in records:
            catalog = self.workbench.repository.record(
                record["dependency_refs"]["asset_catalog"], state=state
            )
            for entity in catalog["content"]["entities"]:
                entities[entity["asset_id"]] = entity
        if kind == "shot":
            bundle = self.workbench.repository.current("prompt_bundle", state=state)
            self.record = bundle
            for index, shot in enumerate((bundle or {}).get("content", {}).get("shots", [])):
                self.objects.addItem(f"镜头 {index + 1:02}", shot["shot_id"])
            self.objects.setCurrentIndex(max(0, self.objects.findData(entity_id)))
            self.displayed_view = max(0, self.objects.currentIndex())
            self.views.addItem("画面", self.displayed_view)
            self.views.hide()
        else:
            seen = set()
            for record in records:
                asset_id = record["content"]["asset_id"]
                if asset_id not in seen:
                    self.objects.addItem(entities[asset_id]["name"], asset_id)
                    seen.add(asset_id)
            self.objects.setCurrentIndex(max(0, self.objects.findData(entity_id)))
            for record in records:
                if record["content"]["asset_id"] == self.objects.currentData():
                    variant = next(
                        v
                        for v in entities[record["content"]["asset_id"]]["variants"]
                        if v["variant_id"] == record["content"]["variant_id"]
                    )
                    self.variants.addItem(variant["label"], record["key"])
            self.variants.setCurrentIndex(max(0, self.variants.findData(selected_key)))
            self.record = (
                self.workbench.repository.current(self.key(), state=state)
                if self.variants.count()
                else None
            )
            for index, view in enumerate((self.record or {}).get("content", {}).get("views", [])):
                self.views.addItem(view["label"], index)
            self.views.setCurrentIndex(
                min(self.displayed_view or 0, max(0, self.views.count() - 1))
            )
            self.displayed_view = max(0, self.views.currentIndex())
            self.views.show()
        self.variants.setVisible(self.variants.count() > 1)
        self.displayed_key = self.record["key"] if self.record else None
        self.displayed_selection = (
            self.tabs.currentIndex(),
            self.objects.currentData(),
            self.variants.currentData(),
        )
        self.editor.set_content(
            self.record["content"] if self.record else None, self.displayed_view
        )
        self.status.setText(
            "已保存"
            if self.record and self.workbench.repository.is_current(self.record["id"], state=state)
            else "基于旧内容，可查看和明确导出。"
            if self.record
            else "从分镜页生成资产提示词。"
        )
        for combo in (self.objects, self.variants, self.views):
            combo.blockSignals(False)

    def save(self) -> bool:
        require(self.record is not None, "INVALID_INPUT", "请先生成提示词")
        if not self.dirty:
            return True
        state = self.workbench.repository.load()
        require(
            state["current"].get(self.record["key"]) == self.record["id"],
            "TARGET_VERSION_CONFLICT",
            "当前提示词已被其他操作修改，编辑保留",
        )
        self.workbench.repository.save(
            self.record["kind"],
            self.editor.value(),
            self.record["dependency_refs"],
            parent_id=self.record["id"],
            expected_revision=state["revision"],
            allow_stale=True,
        )
        self.dirty = False
        if self.record["kind"] != "prompt_bundle":
            self.workbench.update_shot_prompts()
        self.workbench.refresh()
        return True

    def generate(self) -> None:
        if self.record and self.record["kind"] == "prompt_bundle":
            self.regenerate_shot_prompts()
            return
        if self.dirty:
            self.save()
        self.workbench.start_creation(
            "prompts",
            self.generate_button,
            asset_key=self.record["key"]
            if self.record and self.record["kind"] != "prompt_bundle"
            else None,
        )

    def regenerate_shot_prompts(self) -> None:
        self.save()
        require(self.record is not None, "INVALID_INPUT", "请先生成提示词")
        asks = ["根据当前分镜和设定优化所选镜头的画面提示词"]
        shot_ids = [self.record["content"]["shots"][self.editor.index]["shot_id"]]
        service = WorkflowService(self.workbench.repository, self.workbench.model())
        try:
            command = service.prepare(
                "shot_prompts",
                mode="revise",
                target_id=self.record["id"],
                asks=asks,
                shot_ids=shot_ids,
            )
        except WorkspaceError as exc:
            if exc.code != "STALE_INPUT":
                raise
            if (
                QMessageBox.question(self, "基于旧内容", "沿用当前提示词原有来源进行修订？")
                != QMessageBox.Yes
            ):
                return
            command = service.prepare(
                "shot_prompts",
                mode="revise",
                target_id=self.record["id"],
                asks=asks,
                use_stale=True,
                shot_ids=shot_ids,
            )
        self.workbench.submit(
            WorkspaceWork(self.workbench.repository, command, transport=self.workbench.transport),
            self.generate_button,
        )

    def restore_version(self) -> None:
        if not self.workbench.resolve_page(self):
            return
        require(self.record is not None, "INVALID_INPUT", "尚无提示词")
        versions = self.workbench.repository.versions(self.record["key"])
        labels = [
            f"第 {i + 1} 版 · {record['created_at'][:19]}" for i, record in enumerate(versions)
        ]
        label, ok = QInputDialog.getItem(
            self, "历史版本", "选择要恢复的提示词", labels, len(labels) - 1, False
        )
        if ok:
            self.workbench.repository.select(
                versions[labels.index(label)]["id"],
                expected_revision=self.workbench.repository.load()["revision"],
            )
            self.workbench.refresh()

    def saved_record(self) -> JsonObject:
        require(self.record is not None and not self.dirty, "EDIT_PENDING", "请先保存当前提示词")
        return self.record
