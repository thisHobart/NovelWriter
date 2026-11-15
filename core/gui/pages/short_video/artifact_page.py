"""Current copy and storyboard editing workspaces."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from core.short_video.v2.feedback import record_message
from core.short_video.v2.models import require
from core.short_video.v2.prompts import INSTRUCTIONS
from core.short_video.v2.storyboard_text import render_storyboard_text

from .ui_helpers import create_action_button, show_text_dialog
from .v2_editor import CopyEditor, StoryboardEditor

if TYPE_CHECKING:
    from .workbench import Workbench


class ArtifactPage(QWidget):
    def __init__(self, workbench: Workbench, stage: str, title: str) -> None:
        super().__init__()
        self.workbench, self.stage = workbench, stage
        self.dirty, self.record, self.revision = False, None, 0
        layout = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setStyleSheet("font-size:20px; font-weight:600")
        layout.addWidget(heading)
        self.status = QLabel("尚无稿件，从上一阶段继续。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.editor = StoryboardEditor() if stage == "storyboard" else CopyEditor()
        layout.addWidget(self.editor, 1)
        self.editor.changed.connect(self.mark_dirty)
        bar = QHBoxLayout()
        create_action_button("查看原文", self.locate_source, bar, workbench)
        if stage == "storyboard":
            create_action_button("复制分镜文本", self.copy_storyboard_text, bar, workbench)
        self.generate_button = create_action_button("重新生成", self.generate, bar, workbench)
        create_action_button("编辑阶段提示词", self.edit_template, bar, workbench)
        bar.addStretch()
        layout.addLayout(bar)
        bar = QHBoxLayout()
        self.save_button = create_action_button("保存", self.save, bar, workbench)
        bar.addStretch()
        title = {
            "novel_copy": "用这版生成视频文案 →",
            "video_copy": "用这版生成分镜 →",
            "storyboard": "生成资产提示词 →",
        }[stage]
        self.next_button = create_action_button(
            title, self.continue_creation, bar, workbench, primary=True
        )
        layout.addLayout(bar)

    def key(self) -> str:
        return self.stage

    def copy_storyboard_text(self) -> None:
        require(self.record is not None, "INVALID_INPUT", "请先生成分镜")
        catalog = self.workbench.repository.record(self.record["dependency_refs"]["asset_catalog"])
        value = render_storyboard_text(self.editor.value(), catalog["content"])
        QApplication.clipboard().setText(value)
        self.workbench.notice("已复制完整分镜文本，可直接粘贴；未保存的编辑也已包含。")

    def mark_dirty(self) -> None:
        self.dirty = True
        self.status.setText("有未保存修改")

    def refresh(self) -> None:
        if self.dirty:
            return
        state = self.workbench.repository.load()
        self.revision = state["revision"]
        record = self.workbench.repository.current(self.stage, state=state)
        if isinstance(self.editor, StoryboardEditor):
            catalog = (
                self.workbench.repository.record(
                    record["dependency_refs"]["asset_catalog"], state=state
                )
                if record
                else None
            )
            self.editor.catalog = (catalog or {}).get("content", {}).get("entities", [])
            self.editor.names = {e["asset_id"]: e["name"] for e in self.editor.catalog}
        changed = not self.record or not record or self.record["id"] != record["id"]
        self.record = record
        if changed:
            self.editor.set_content(record["content"] if record else None)
        if not record:
            self.status.setText("尚无稿件，从上一阶段继续。")
        else:
            self.status.setText(record_message(self.workbench.repository, record, state))

    def save(self) -> bool:
        require(self.record is not None, "INVALID_INPUT", "请先生成稿件")
        if not self.dirty:
            return True
        state = self.workbench.repository.load()
        require(
            state["current"].get(self.record["key"]) == self.record["id"],
            "TARGET_VERSION_CONFLICT",
            "当前内容已变化，编辑已保留；请先复制未保存文字，再刷新核对当前内容",
        )
        self.workbench.repository.save(
            self.record["kind"],
            self.editor.value(),
            self.record["dependency_refs"],
            expected_revision=state["revision"],
        )
        self.dirty = False
        if self.stage == "storyboard":
            self.workbench.update_shot_prompts()
        self.workbench.refresh()
        return True

    def continue_creation(self) -> None:
        if not self.save():
            return
        stage = {"novel_copy": "video_copy", "video_copy": "storyboard", "storyboard": "prompts"}[
            self.stage
        ]
        self.workbench.start_creation(stage, self.next_button)

    def generate(self) -> None:
        if self.dirty and not self.save():
            return
        self.workbench.start_creation(self.stage, self.generate_button)

    def edit_template(self) -> None:
        current = self.workbench.repository.current(f"prompt_template:{self.stage}")
        value = show_text_dialog(
            self,
            "阶段提示词",
            current["content"]["instruction"] if current else INSTRUCTIONS[self.stage],
        )
        if value:
            self.workbench.repository.save(
                "prompt_template",
                {"stage": self.stage, "instruction": value},
                {},
                key=f"prompt_template:{self.stage}",
                expected_revision=self.workbench.repository.load()["revision"],
            )

    def locate_source(self) -> None:
        from core.short_video.v2.project_service import ProjectService

        context = ProjectService(self.workbench.repository).context(
            selection_id=self.record["dependency_refs"]["selection"] if self.record else None
        )
        show_text_dialog(self, "原文对照", context["text"], readonly=True)
