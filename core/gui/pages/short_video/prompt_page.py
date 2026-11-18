"""Prompt workspace: positive/negative prompts of assets with object and view selectors, the
「镜头」 tab's video generation, and the asset images tab."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

from PySide6.QtCore import QMargins, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from core.short_video.v2.contracts import JsonObject
from core.short_video.v2.feedback import record_check, record_message
from core.short_video.v2.media_repository import MediaRepository
from core.short_video.v2.models import ASSET_KINDS, require
from core.short_video.v2.workflow import WorkflowService, WorkspaceWork

from .asset_images_page import AssetImagesPage
from .media_preview import RUNNING, MediaPreview
from .shot_header import ShotHeader
from .ui_helpers import create_action_button
from .video_widgets import Fold

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
            edit.setMinimumHeight(140 if edit is self.positive else 64)
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

    def set_content(
        self, content: JsonObject | None, index: int = 0, *, readonly: bool = False
    ) -> None:
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
            edit.setReadOnly(readonly)
            edit.blockSignals(False)


class PromptPage(QWidget):
    def __init__(self, workbench: Workbench) -> None:
        super().__init__()
        self.workbench, self.dirty, self.record = workbench, False, None
        self.displayed_view = None
        self.displayed_selection = (0, None, None)
        layout = QVBoxLayout(self)
        self.tabs = QTabBar()
        for title in ["人物", "场景", "道具", "镜头", "资产图片"]:
            self.tabs.addTab(title)
        layout.addWidget(self.tabs)
        bar = QHBoxLayout()
        self.object_label = QLabel("对象")
        bar.addWidget(self.object_label)
        self.objects, self.variants, self.views = QComboBox(), QComboBox(), QComboBox()
        bar.addWidget(self.objects, 2)
        bar.addWidget(self.variants, 1)
        self.view_label = QLabel("视图")
        bar.addWidget(self.view_label)
        bar.addWidget(self.views, 1)
        self.selector_bar = QWidget()
        self.selector_bar.setLayout(bar)
        layout.addWidget(self.selector_bar)
        self.status = QLabel("从分镜页生成资产提示词。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.editor = PromptEditor()
        self.editor.changed.connect(self.mark_dirty)
        self.editor_margins = self.editor.layout().contentsMargins()
        text_widget = QWidget()
        self.text_area = QVBoxLayout(text_widget)
        self.text_area.setContentsMargins(0, 0, 0, 0)
        self.text_area.addWidget(self.editor, 1)
        self.text_scroll = QScrollArea()
        self.text_scroll.setWidgetResizable(True)
        self.text_scroll.setFrameShape(QScrollArea.NoFrame)
        self.text_scroll.setWidget(text_widget)
        self.body_stack = QStackedWidget()
        self.text_body = QWidget()
        body = QVBoxLayout(self.text_body)
        body.setContentsMargins(0, 0, 0, 0)
        body.addWidget(self.text_scroll, 1)
        self.body_stack.addWidget(self.text_body)
        # 「镜头」: shot chips and the shot's check line on top, the shot's image prompts folded
        # under the video prompt.
        self.media = MediaPreview(workbench)
        self.media.inputs_changed.connect(self.refresh_shot_marks)
        self.shot_header = ShotHeader()
        self.shot_header.chosen.connect(self.choose_shot)
        self.shot_header.resized.connect(self.media.relayout)
        self.media.set_header(self.shot_header)
        self.image_fold = Fold("镜头画面提示词", "正向 · 负面")
        self.image_fold.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.image_fold.toggled.connect(self.fold_toggled)
        self.fold_closed_by_hand = False
        self.fold_bar = QHBoxLayout()
        self.fold_bar.setSpacing(6)
        self.fold_bar.addStretch()
        self.image_fold.body_layout.addLayout(self.fold_bar)
        self.media.add_section(self.image_fold)
        self.body_stack.addWidget(self.media)
        self.images_page = AssetImagesPage(workbench)
        self.body_stack.addWidget(self.images_page)
        layout.addWidget(self.body_stack, 1)
        self.action_layout = QHBoxLayout()
        bar = self.action_layout
        self.save_button = create_action_button("保存", self.save, bar, workbench)
        self.generate_button = create_action_button("重新生成", self.generate, bar, workbench)
        self.resume_stage = "prompt_task"
        self.resume_button = create_action_button(
            "继续生成",
            lambda: workbench.resume_generation(self.resume_stage, self.resume_button),
            bar,
            workbench,
        )
        self.resume_button.hide()
        bar.addStretch()
        create_action_button("导出", workbench.export, bar, workbench, primary=True)
        self.action_bar = QWidget()
        self.action_bar.setLayout(bar)
        layout.addWidget(self.action_bar)
        self.controls_in_fold = False
        self.tabs.currentChanged.connect(lambda: workbench.perform(self.change_object))
        self.objects.currentIndexChanged.connect(lambda: workbench.perform(self.change_object))
        self.variants.currentIndexChanged.connect(lambda: workbench.perform(self.change_object))
        self.views.currentIndexChanged.connect(lambda: workbench.perform(self.change_view))

    def choose_shot(self, index: int) -> None:
        """A shot chip stands for an entry of the hidden shot list; a refused switch snaps back."""
        self.objects.setCurrentIndex(index)
        self.shot_header.chips.set_current(self.objects.currentIndex())

    def fold_toggled(self, expanded: bool) -> None:
        self.fold_closed_by_hand = not expanded
        self.media.relayout()

    def place_prompt_controls(self, shot: bool) -> None:
        """The image prompt editor and its buttons live in the fold on 「镜头」, else in the page."""
        if shot == self.controls_in_fold:
            return
        self.controls_in_fold = shot
        buttons = (self.save_button, self.generate_button, self.resume_button)
        # Flush with the fold's title in the fold; the page's usual inset elsewhere.
        self.editor.layout().setContentsMargins(
            QMargins(0, 0, 0, 0) if shot else self.editor_margins
        )
        if shot:
            self.image_fold.body_layout.insertWidget(0, self.editor)
            for button in reversed(buttons):
                self.fold_bar.insertWidget(1, button)
        else:
            self.text_area.insertWidget(0, self.editor, 1)
            for position, button in enumerate(buttons):
                self.action_layout.insertWidget(position, button)

    def refresh_shot_marks(self) -> None:
        """Chip marks from each shot's latest video job: saved, failed, running or unsure."""
        if not self.workbench.output_dir or not self.shot_header.chips.chips:
            return
        latest = {}
        for job in MediaRepository(self.workbench.output_dir).jobs():
            shot_id = job["input_snapshot"].get("target", {}).get("shot_id")
            if shot_id:
                latest[shot_id] = job["status"]
        marks = []
        for index in range(self.objects.count()):
            status = latest.get(self.objects.itemData(index))
            marks.append(
                "ok"
                if status == "succeeded"
                else "bad"
                if status == "failed"
                else "run"
                if status in RUNNING
                else "warn"
                if status in {"unknown", "interrupted"}
                else ""
            )
        self.shot_header.chips.set_marks(marks)

    def mark_dirty(self) -> None:
        self.dirty = True
        self.status.setText("有未保存修改")
        if self.controls_in_fold:
            self.shot_header.set_notice("镜头画面提示词有未保存修改")
            self.image_fold.note.setText("正向 · 负面 · 有未保存修改")
            self.media.relayout()

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

    def on_shot_tab(self) -> bool:
        return self.tabs.currentIndex() == 3

    def refresh(self, selected_key: str | None = None) -> None:
        if self.dirty:
            return
        image_tab = self.tabs.currentIndex() == 4
        shot_tab = self.tabs.currentIndex() == 3
        self.place_prompt_controls(shot_tab)
        self.selector_bar.setVisible(not image_tab and not shot_tab)
        self.action_bar.setVisible(not image_tab and not shot_tab)
        self.status.setVisible(not image_tab)
        self.body_stack.setCurrentWidget(
            self.images_page if image_tab else self.media if shot_tab else self.text_body
        )
        if image_tab:
            self.images_page.refresh()
            self.displayed_selection = (4, None, None)
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
        board = None
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
            board = self.workbench.repository.current("storyboard", state=state)
            bundle = self.workbench.repository.current("prompt_bundle", state=state)
            self.record = (
                bundle
                if bundle and board and bundle["content"]["storyboard_version_id"] == board["id"]
                else None
            )
            editor_content = self.record["content"] if self.record else None
            if board and not self.record:
                # A failed asset task must not hide the storyboard's usable image drafts.
                editor_content = {
                    "shots": [
                        {
                            "shot_id": shot["shot_id"],
                            "resolved_prompt": shot["visual_prompt_draft"],
                            "negative_prompt": "",
                        }
                        for shot in board["content"]["shots"]
                    ]
                }
            for index, shot in enumerate((editor_content or {}).get("shots", [])):
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
            editor_content = self.record["content"] if self.record else None
        self.variants.setVisible(self.variants.count() > 1)
        self.view_label.setVisible(kind != "shot")
        self.displayed_selection = (
            self.tabs.currentIndex(),
            self.objects.currentData(),
            self.variants.currentData(),
        )
        self.editor.set_content(
            editor_content, self.displayed_view, readonly=kind == "shot" and not self.record
        )
        show_text_editor = kind != "shot" or bool(editor_content)
        self.editor.setVisible(show_text_editor)
        self.objects.setVisible(show_text_editor)
        self.object_label.setVisible(show_text_editor)
        self.save_button.setVisible(show_text_editor)
        self.generate_button.setVisible(show_text_editor)
        self.image_fold.setVisible(bool(editor_content))
        if kind == "shot":
            shot_id = self.objects.currentData()
            self.media.set_target(
                {
                    "storyboard_version_id": board["id"] if board else None,
                    "shot_id": shot_id,
                    "shot_number": self.objects.currentIndex() + 1,
                }
            )
            self.media.set_shot(
                (self.board_shot(board, shot_id) or {}).get("duration_ms"),
                WorkflowService(self.workbench.repository).shot_assets(shot_id, state=state)
                if shot_id
                else [],
            )
            # The video area shows only on 镜头; other tabs leave it until it is shown again.
            self.media.refresh()
        self.save_button.setEnabled(self.record is not None)
        self.generate_button.setText(
            "生成镜头提示词" if kind == "shot" and not self.record else "重新生成"
        )
        if kind == "shot" and board and not self.record:
            required = {
                f"{asset_kind}_spec:{ref['asset_id']}:{ref['variant_id']}"
                for shot in board["content"]["shots"]
                for asset_kind, refs in (
                    ("scene", [shot["scene_ref"]]),
                    ("character", shot["characters"]),
                    ("prop", shot["props"]),
                )
                for ref in refs
            }
            missing = len(required - state["current"].keys())
            detail = f"缺少 {missing} 项资产设定；" if missing else ""
            self.status.setText(
                "当前显示分镜画面草稿，可查看和复制，修改请到分镜页。"
                + detail
                + "点击“生成镜头提示词”补齐资产并编排完整提示词。"
            )
        else:
            self.status.setText(
                record_message(self.workbench.repository, self.record, state)
                if self.record
                else "从分镜页生成资产提示词。"
            )
        plain_status = self.status.text()
        for combo in (self.objects, self.variants, self.views):
            combo.blockSignals(False)

        from core.short_video.v2.runtime import get_recovery, recovery_message

        shot_task = (
            get_recovery(self.workbench.repository, "shot_prompts") if kind == "shot" else None
        )
        self.resume_stage = "shot_prompts" if shot_task and shot_task["valid"] else "prompt_task"
        task = get_recovery(self.workbench.repository, self.resume_stage)
        self.resume_button.setVisible(bool(task and task["valid"] and task["status"] != "running"))
        if task and task["valid"] and task["status"] != "running":
            self.status.setText(recovery_message(task))
        elif task and not task["valid"]:
            self.status.setText("上次任务不能继续：" + task.get("reason", "输入已变化，请重新生成"))
        if kind == "shot":
            self.refresh_shot_header(board, state, plain_status)

    @staticmethod
    def board_shot(board: JsonObject | None, shot_id: str | None) -> JsonObject | None:
        shots = (board or {}).get("content", {}).get("shots", [])
        return next((shot for shot in shots if shot["shot_id"] == shot_id), None)

    def refresh_shot_header(self, board: JsonObject | None, state: JsonObject, plain: str) -> None:
        """Chips, the shot's length, action and check; the page status only when it says more."""
        index = self.objects.currentIndex()
        chips = self.shot_header.chips
        chips.set_count(self.objects.count())
        chips.set_current(index)
        self.shot_header.setVisible(index >= 0)
        self.status.setVisible(index < 0)
        if index >= 0:
            shot = self.board_shot(board, self.objects.currentData()) or {}
            self.shot_header.set_shot(index + 1, shot.get("duration_ms"), shot.get("action", ""))
            self.shot_header.set_check(
                record_check(self.workbench.repository, self.record, state, shot_index=index)
                if self.record
                else None
            )
            text = self.status.text()
            self.shot_header.set_notice("" if text == plain and self.record else text)
        self.refresh_shot_marks()
        self.image_fold.note.setText("正向 · 负面")
        if not self.record and not self.fold_closed_by_hand:
            # Without shot prompts, 「生成镜头提示词」 is the next step and sits in the fold.
            self.image_fold.set_expanded(True)
        self.media.relayout()

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
            expected_revision=state["revision"],
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
        command = service.prepare(
            "shot_prompts",
            mode="revise",
            target_id=self.record["id"],
            asks=asks,
            shot_ids=shot_ids,
        )
        self.workbench.submit(
            WorkspaceWork(self.workbench.repository, command, transport=self.workbench.transport),
            self.generate_button,
        )
