"""Four content workspaces with shared conversation and selected-content export."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.generation.api_providers import PROVIDER_LABELS, configuration_status
from core.short_video.v2.contracts import OperationResult, VideoTask
from core.short_video.v2.conversation import ConversationService
from core.short_video.v2.creation import CreationWork, prepare_prompt_task, prepare_storyboard_task
from core.short_video.v2.exporter import ExportService
from core.short_video.v2.feedback import ACTION_NAMES, error_reason, record_message
from core.short_video.v2.models import WorkspaceError, fingerprint, require
from core.short_video.v2.repository import ProjectRepository
from core.short_video.v2.workflow import WorkflowService, WorkspaceWork

from .artifact_page import ArtifactPage
from .chat_panel import ChatPanel
from .prompt_page import PromptPage
from .source_page import SourcePage
from .ui_helpers import create_action_button

if TYPE_CHECKING:
    from core.gui.main_window import MainWindow

STEPS = [
    ("video_source", "原文"),
    ("video_adaptation", "改写"),
    ("video_storyboard", "分镜"),
    ("video_prompts", "提示词"),
]


class Workbench(QWidget):
    changed = Signal()
    STEPS = STEPS

    def __init__(self, host: MainWindow) -> None:
        super().__init__()
        self.host, self.transport, self.active_work = host, None, None
        self.settings = QSettings("NovelWriter", "VideoWorkbench")
        self.output_dir = str(
            Path(self.settings.value("last_project", "current_video_work")).resolve()
        )
        self.repository = ProjectRepository(self.output_dir)
        self.repository.load()
        ConversationService(self.repository).recover()
        from core.short_video.v2.batches import recover_plans

        recover_plans(self.repository)
        self.pages = {}
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.directory = QLineEdit(self.output_dir, self)
        self.directory.hide()
        self.project_button = QPushButton("项目")
        self.project_button.setMaximumWidth(240)
        menu = QMenu(self.project_button)
        menu.addAction("新建项目", lambda: self.perform(self.new_project))
        menu.addAction("打开项目", lambda: self.perform(self.browse_project))
        menu.addAction("项目设置", lambda: self.perform(self.project_settings))
        self.project_button.setMenu(menu)
        top.addWidget(self.project_button)
        self.navigation = {}
        for key, title in STEPS:
            button = create_action_button(title, lambda k=key: host._navigate(k), top, self)
            button.setCheckable(True)
            self.navigation[key] = button
        top.addStretch()
        self.model_config_button = QPushButton("服务配置")
        service_menu = QMenu(self.model_config_button)
        service_menu.addAction("文字模型配置", self.host._configure_model)
        service_menu.addAction("ComfyUI 工作流", lambda: self.perform(self.configure_comfy))
        self.model_config_button.setMenu(service_menu)
        top.addWidget(self.model_config_button)
        self.chat_toggle = create_action_button("收起对话", self.toggle_chat, top, self)
        self.export_button = create_action_button("导出", self.export, top, self)
        layout.addLayout(top)
        self.splitter = QSplitter(Qt.Horizontal)
        self.chat = ChatPanel(self)
        self.right = QStackedWidget()
        self.splitter.addWidget(self.chat)
        self.splitter.addWidget(self.right)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([320, 850])
        layout.addWidget(self.splitter, 1)
        self.source = SourcePage(self)
        source_scroll = QScrollArea()
        source_scroll.setWidgetResizable(True)
        source_scroll.setWidget(self.source)
        self.add_page("video_source", source_scroll)
        self.adaptation = QTabWidget()
        self.novel = ArtifactPage(self, "novel_copy", "小说推文")
        self.video = ArtifactPage(self, "video_copy", "视频推广文案")
        self.adaptation.addTab(self.novel, "① 小说推文")
        self.adaptation.addTab(self.video, "② 视频推广文案")
        self.add_page("video_adaptation", self.adaptation)
        self.storyboard = ArtifactPage(self, "storyboard", "分镜")
        self.add_page("video_storyboard", self.storyboard)
        self.specs = PromptPage(self)
        self.add_page("video_prompts", self.specs)
        self.splitter.splitterMoved.connect(
            lambda *_: self.settings.setValue(self.setting_key("sizes"), self.splitter.sizes())
        )
        self.restore_layout()
        self.refresh()

    def model(self) -> str:
        return self.host.selected_model()

    def refresh_model_status(self) -> None:
        provider = self.model()
        _, detail = configuration_status(provider)
        self.model_config_button.setText("服务配置 · " + PROVIDER_LABELS[provider])
        self.model_config_button.setToolTip(detail)

    def setting_key(self, field: str) -> str:
        return fingerprint(self.output_dir)[:20] + "/" + field

    def configure_comfy(self) -> None:
        from .comfy_settings import ComfySettingsDialog

        require(not self.host.runner.busy, "PROJECT_BUSY", "请在任务结束后修改工作流配置")
        ComfySettingsDialog(self.output_dir, self).exec()
        self.specs.media.refresh()

    def add_page(self, key: str, widget: QWidget) -> None:
        self.pages[key] = widget
        self.right.addWidget(widget)

    def perform(self, operation: Callable[[], OperationResult]) -> OperationResult | Literal[False]:
        owner = getattr(operation, "__self__", None)
        method = getattr(operation, "__name__", "")
        stage = getattr(owner, "stage", "")
        name = {
            "save": "保存",
            "generate": "生成",
            "import_file": "原文导入",
            "export": "内容导出",
            "browse_project": "项目打开",
            "project_settings": "项目设置",
            "configure_comfy": "工作流配置",
            "send": "AI 回复",
            "query": "视频任务查询",
            "restore_version": "历史版本恢复",
            "edit_template": "阶段提示词保存",
            "new_project": "项目创建",
            "select_workflow": "视频工作流切换",
            "import_image": "参考图片导入",
            "select": "会话切换",
            "create": "会话创建",
        }.get(method, "当前操作")
        if method in {"save", "generate"}:
            subject = {
                "novel_copy": "小说推文",
                "video_copy": "视频文案",
                "storyboard": "分镜",
            }.get(stage)
            subject = subject or ("原文" if owner is self.source else "提示词")
            name = subject + name
        if method == "generate":
            if owner is self.source:
                name = "小说推文生成"
            elif stage:
                name = ACTION_NAMES.get(stage, name)
            elif owner is self.specs:
                name = (
                    "镜头提示词优化"
                    if owner.record and owner.record["kind"] == "prompt_bundle"
                    else "资产提示词生成"
                )
            elif owner is self.specs.media:
                name = "视频生成"
        if method == "continue_creation":
            name = {
                "novel_copy": "视频文案生成",
                "video_copy": "分镜生成",
                "storyboard": "资产提示词生成",
            }.get(stage, "后续内容生成")
        try:
            result = operation()
            if method == "save" and result is True:
                detail = (
                    "当前内容尚未自动检查"
                    if owner is not self.source
                    else "后续任务使用已保存的原文与Skill选择"
                )
                if owner is not self.source and getattr(owner, "record", None):
                    detail = record_message(
                        self.repository, owner.record, self.repository.load()
                    ).removeprefix("已保存；")
                self.host.status.set_done(name.removesuffix("保存") + "已保存", detail)
            return result
        except (WorkspaceError, OSError) as exc:
            self.host.status.set_error(name + "失败", error_reason(exc))
            logging.getLogger(__name__).error("%s失败", name, exc_info=True)
            return False

    def notice(self, text: str) -> None:
        self.host.status.set_done(text, "")

    def submit(self, work: VideoTask, busy_button: QPushButton | None = None) -> None:
        require(not self.host.runner.busy, "PROJECT_BUSY", "已有任务在运行")
        self.active_work = work
        context = work.task_context
        self.host._on_video_task_requested(work, busy_button, context.action + "中…")
        self.chat.refresh()

    def task_controls(self) -> list[QWidget]:
        return [
            self.project_button,
            self.model_config_button,
            self.source.generate_button,
            *[
                button
                for page in self.artifact_pages()
                for button in (
                    [page.generate_button, page.next_button]
                    if isinstance(page, ArtifactPage)
                    else [page.generate_button]
                )
            ],
            self.chat.send_button,
            *self.specs.media.task_controls(),
        ]

    def artifact_pages(self) -> list[ArtifactPage | PromptPage]:
        return [self.novel, self.video, self.storyboard, self.specs]

    def current_artifact_page(self) -> ArtifactPage | PromptPage | None:
        page = self.right.currentWidget()
        return (
            self.adaptation.currentWidget()
            if page is self.adaptation
            else page
            if isinstance(page, (ArtifactPage, PromptPage))
            else None
        )

    def resolve_page(self, page: SourcePage | ArtifactPage | PromptPage) -> bool:
        if not page.dirty:
            return True
        answer = QMessageBox.question(
            self,
            "尚有未保存修改",
            "保存当前修改后继续？",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if answer == QMessageBox.Cancel:
            return False
        if answer == QMessageBox.Save:
            return bool(self.perform(page.save))
        page.dirty = False
        if hasattr(page, "record"):
            page.record = None
        page.refresh()
        return True

    def resolve_edits(self) -> bool:
        return all(self.resolve_page(page) for page in [self.source, *self.artifact_pages()])

    def navigate(self, key: str) -> bool:
        if key not in self.pages or not self.resolve_edits():
            return False
        self.right.setCurrentWidget(self.pages[key])
        for name, button in self.navigation.items():
            button.setChecked(name == key)
            button.setProperty("variant", "primary" if name == key else "ghost")
            button.style().unpolish(button)
            button.style().polish(button)
        self.settings.setValue(self.setting_key("page"), key)
        self.refresh()
        return True

    def refresh(self) -> None:
        self.refresh_model_status()
        self.source.refresh()
        for page in self.artifact_pages():
            page.refresh()
        config = self.repository.current("config")
        self.project_button.setText(
            (config["content"]["title"] if config else "新项目") or "新项目"
        )
        self.chat.refresh()

    def start_creation(
        self, stage: str, button: QPushButton | None = None, *, asset_key: str | None = None
    ) -> None:
        require(not self.host.runner.busy, "PROJECT_BUSY", "已有任务在运行")
        # Save visible edits before freezing any paid request.
        if self.source.dirty and not self.source.save():
            return
        page = self.current_artifact_page()
        if page and page.dirty:
            page.save()

        def prepare(use_stale: bool) -> VideoTask:
            if stage == "storyboard":
                command = prepare_storyboard_task(
                    self.repository, self.model(), use_stale=use_stale
                )
                return CreationWork(self.repository, command, transport=self.transport)
            if stage == "prompts":
                command = prepare_prompt_task(
                    self.repository, self.model(), use_stale=use_stale, asset_key=asset_key
                )
                return CreationWork(self.repository, command, transport=self.transport)
            command = WorkflowService(self.repository, self.model()).prepare(
                stage, use_stale=use_stale
            )
            return WorkspaceWork(self.repository, command, transport=self.transport)

        try:
            work = prepare(stage == "prompts")
        except WorkspaceError as exc:
            if exc.code == "INPUT_TOO_LARGE":
                if (
                    QMessageBox.question(
                        self, "原文超出容量", "可以缩小原文选区，或分段处理全文。现在分段处理？"
                    )
                    == QMessageBox.Yes
                ):
                    self.chunk_plan()
                return
            if exc.code != "STALE_INPUT":
                raise
            if (
                QMessageBox.question(
                    self,
                    "当前稿基于旧内容",
                    "明确沿用这版稿件及其原有来源继续？选择“否”可返回重新生成。",
                )
                != QMessageBox.Yes
            ):
                return
            work = prepare(True)
        target = {
            "novel_copy": "video_adaptation",
            "video_copy": "video_adaptation",
            "storyboard": "video_storyboard",
            "prompts": "video_prompts",
        }[stage]
        if stage in {"novel_copy", "video_copy"}:
            self.adaptation.setCurrentWidget(self.novel if stage == "novel_copy" else self.video)
        self.host._navigate(target)
        self.submit(work, button)

    def update_shot_prompts(self) -> None:
        board = self.repository.current("storyboard")
        if not board:
            return
        try:
            WorkflowService(self.repository).compile_storyboard_prompts(
                expected_revision=self.repository.load()["revision"], use_stale=True
            )
        except WorkspaceError as exc:
            if exc.code not in {"EXPORT_INCOMPLETE", "STALE_INPUT"}:
                raise

    def toggle_chat(self) -> None:
        self.chat.setVisible(self.chat.isHidden())
        self.chat_toggle.setText("展开对话" if self.chat.isHidden() else "收起对话")
        self.settings.setValue(self.setting_key("chat_hidden"), self.chat.isHidden())

    def restore_layout(self) -> None:
        sizes = self.settings.value(self.setting_key("sizes"))
        self.splitter.setSizes([int(v) for v in sizes] if sizes else [320, 850])
        hidden = self.settings.value(self.setting_key("chat_hidden"), False, type=bool)
        self.chat.setVisible(not hidden)
        self.chat_toggle.setText("展开对话" if hidden else "收起对话")

    def export(self) -> None:
        if not self.resolve_edits():
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("导出内容")
        layout = QVBoxLayout(dialog)
        choices = {}
        current = self.current_artifact_page()
        for key, title in [
            ("novel_copy", "小说推文"),
            ("video_copy", "视频推广文案"),
            ("storyboard", "分镜"),
            ("prompts", "提示词文字包"),
        ]:
            check = QCheckBox(title)
            check.setChecked(
                current is not None
                and (current.key() == key or key == "prompts" and current is self.specs)
            )
            choices[key] = check
            layout.addWidget(check)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        keys = [key for key, check in choices.items() if check.isChecked()]
        require(bool(keys), "INVALID_INPUT", "请选择要导出的内容")
        allow_stale, allow_partial = False, False
        service = ExportService(self.repository)
        while True:
            try:
                service.snapshot(keys=keys, allow_stale=allow_stale, allow_partial=allow_partial)
                break
            except WorkspaceError as exc:
                if exc.code == "EXPORT_INCOMPLETE" and not allow_partial:
                    if (
                        QMessageBox.question(self, "缺少部分内容", str(exc) + "\n导出已有内容？")
                        != QMessageBox.Yes
                    ):
                        return
                    allow_partial = True
                elif exc.code == "STALE_INPUT" and not allow_stale:
                    if (
                        QMessageBox.question(self, "基于旧内容", "明确导出所选旧稿及其真实来源？")
                        != QMessageBox.Yes
                    ):
                        return
                    allow_stale = True
                else:
                    raise
        path, _ = QFileDialog.getSaveFileName(
            self, "保存导出", str(Path(self.output_dir) / "视频推广.md"), "Markdown (*.md)"
        )
        if not path:
            return
        overwrite = Path(path).exists() or Path(path + ".manifest.json").exists()
        if (
            overwrite
            and QMessageBox.question(self, "覆盖导出", "文件已存在，是否覆盖？") != QMessageBox.Yes
        ):
            return
        service.export(
            path,
            keys=keys,
            allow_stale=allow_stale,
            allow_partial=allow_partial,
            overwrite=overwrite,
        )
        self.notice("已导出：" + path)

    def project_settings(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("项目设置")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("项目目录：" + self.output_dir))
        layout.addWidget(QLabel("文字模型"))
        model_summary = QLabel()
        model_summary.setWordWrap(True)
        model_summary.setTextFormat(Qt.PlainText)
        layout.addWidget(model_summary)

        def configure_model() -> None:
            self.host._configure_model()
            update_model_summary()

        def update_model_summary() -> None:
            model_summary.setText(
                PROVIDER_LABELS[self.model()] + " · " + self.model_config_button.toolTip()
            )

        update_model_summary()
        create_action_button("模型配置", configure_model, layout, self)
        layout.addWidget(
            QLabel("服务商、接口地址、API 密钥和模型与小说创作共用，保存配置后立即生效。")
        )
        create_action_button(
            "返回小说创作", lambda: (dialog.accept(), self.host.switch_mode("novel")), layout, self
        )
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def new_project(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择新项目目录", self.output_dir)
        if path:
            require(
                not ProjectRepository(path).path.exists(),
                "INVALID_INPUT",
                "该目录已有项目，请使用打开项目",
            )
            self.directory.setText(path)
            self.open_project()

    def browse_project(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "打开项目目录", self.output_dir)
        if path:
            self.directory.setText(path)
            self.open_project()

    def open_project(self) -> None:
        require(not self.host.runner.busy, "PROJECT_BUSY", "运行中无法切换项目")
        if not self.resolve_edits():
            self.directory.setText(self.output_dir)
            return
        candidate = ProjectRepository(self.directory.text())
        try:
            candidate.load()
            ConversationService(candidate).recover()
            from core.short_video.v2.batches import recover_plans

            recover_plans(candidate)
        except (WorkspaceError, OSError):
            self.directory.setText(self.output_dir)
            raise
        self.repository, self.output_dir = candidate, candidate.output_dir
        self.settings.setValue("last_project", self.output_dir)
        self.chat.conversation_id = None
        self.chat.input.clear()
        self.restore_layout()
        self.refresh()
        restored = self.settings.value(self.setting_key("page"), "video_source")
        self.host._navigate(restored if restored in self.pages else "video_source")
        self.host.refresh_all()
        self.notice("已打开项目")

    def chunk_plan(self) -> None:
        from core.short_video.v2.batches import BatchWork, ChunkService

        service = ChunkService(self.repository, self.model())
        plan = service.find_resumable_plan() or service.create_plan()
        if (
            QMessageBox.question(
                self,
                "分段处理全文",
                f"将全文分为 {len(plan['chunks'])} 段，提取事实并改编，最后合并全文。继续？",
            )
            == QMessageBox.Yes
        ):
            self.submit(BatchWork(self.repository, plan, mode="chunks", transport=self.transport))
