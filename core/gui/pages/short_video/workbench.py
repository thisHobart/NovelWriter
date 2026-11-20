"""Four content workspaces with shared conversation and selected-content export."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from PySide6.QtCore import QSettings, Qt
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

from core.common.api_providers import DEFAULT_API_MODEL, PROVIDER_LABELS, configuration_status
from core.common.locks import project_lock
from core.gui.model_config_dialog import ModelConfigDialog, ServiceProfile
from core.short_video.v2.contracts import OperationResult, VideoTask
from core.short_video.v2.conversation import ConversationService
from core.short_video.v2.creation import CreationWork, prepare_prompt_task, prepare_storyboard_task
from core.short_video.v2.exporter import ExportService
from core.short_video.v2.feedback import (
    ACTION_NAMES,
    error_reason,
    record_message,
    task_message,
)
from core.short_video.v2.models import WorkspaceError, fingerprint, require
from core.short_video.v2.repository import ProjectRepository, shared_project_reads
from core.short_video.v2.service_settings import (
    get_review_config,
    image_form_values,
    review_form_values,
    review_provider,
    save_image,
    save_review,
)
from core.short_video.v2.workflow import WorkflowService, WorkspaceWork

from .artifact_page import ArtifactPage
from .chat_panel import ChatPanel
from .prompt_page import PromptPage
from .source_page import SourcePage
from .task_channels import TaskChannels, status_text
from .ui_helpers import create_action_button

if TYPE_CHECKING:
    from core.gui.feature import FeatureHost

# Default width of the 「与 AI 讨论」 panel; dragging the splitter is remembered per project.
CHAT_WIDTH = 384
STEPS = [
    ("video_source", "原文"),
    ("video_adaptation", "改写"),
    ("video_storyboard", "分镜"),
    ("video_prompts", "提示词"),
]
REVIEW_SERVICE = ServiceProfile(
    "校验模型配置", "独立保存，仅用于检查生成结果", review_form_values, save_review
)
IMAGE_SERVICE = ServiceProfile(
    "生图 API 配置", "填写当前服务的连接信息，保存后立即生效", image_form_values, save_image,
    only_provider=("hosted-llm", "OpenAI 兼容生图接口"), list_models_to_check=True,
)


class Workbench(QWidget):
    """The short video feature; the app window reaches it only through core.gui.feature."""

    side_panel = None

    @classmethod
    def open(cls, host: FeatureHost, **options: str) -> Workbench | None:
        """Open on the last project, or report in the status bar why it cannot be read."""
        try:
            return cls(host, **options)
        except (WorkspaceError, OSError) as exc:
            host.status.set_error("无法打开短视频项目", str(exc))
            return None

    def __init__(self, host: FeatureHost, *, inherit_model: str = DEFAULT_API_MODEL) -> None:
        super().__init__()
        self.host, self.transport = host, None
        self.tasks = TaskChannels()
        self.settings = QSettings("NovelWriter", "VideoWorkbench")
        # The model service used for text, remembered for the short video alone (not per
        # project); before one was ever chosen here, it starts as the novel's.
        self.text_model = self.settings.value("text_model", "")
        if self.text_model not in PROVIDER_LABELS:
            self.text_model = inherit_model if inherit_model in PROVIDER_LABELS else DEFAULT_API_MODEL
            self.settings.setValue("text_model", self.text_model)
        self.output_dir = str(
            Path(self.settings.value("last_project", "current_video_work")).resolve()
        )
        self.repository = ProjectRepository(self.output_dir)
        self.repository.load()
        ConversationService(self.repository).recover()
        from core.short_video.v2.image_generation import ImageGenerationService

        ImageGenerationService(self.repository).recover()
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
            button = create_action_button(title, lambda k=key: self.navigate(k), top, self)
            button.setCheckable(True)
            self.navigation[key] = button
        top.addStretch()
        self.model_config_button = QPushButton("服务配置")
        service_menu = QMenu(self.model_config_button)
        service_menu.addAction("生成模型配置", lambda: self.perform(self.configure_text_model))
        service_menu.addAction("校验模型配置", lambda: self.perform(self.configure_review_model))
        service_menu.addAction("生图 API 配置", lambda: self.perform(self.configure_image_model))
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
        self.splitter.setSizes([CHAT_WIDTH, 850])
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
        # Only the visible tab is kept current, so the other one catches up when shown.
        self.adaptation.currentChanged.connect(self.refresh_adaptation_tab)
        self.add_page("video_adaptation", self.adaptation)
        self.storyboard = ArtifactPage(self, "storyboard", "分镜")
        self.add_page("video_storyboard", self.storyboard)
        self.storyboard.editor.selection_changed.connect(self.chat.refresh_reference)
        self.storyboard.editor.changed.connect(self.chat.refresh_reference)
        self.right.currentChanged.connect(self.chat.refresh_reference)
        self.specs = PromptPage(self)
        self.add_page("video_prompts", self.specs)
        self.splitter.splitterMoved.connect(
            lambda *_: self.settings.setValue(self.setting_key("sizes"), self.splitter.sizes())
        )
        self.restore_layout()
        self.refresh()

    # ---- what the app window asks of a feature (core.gui.feature)
    @property
    def busy(self) -> bool:
        return self.tasks.any_busy

    def enter(self) -> None:
        self.host.status.set_idle(f"输出目录 {self.output_dir}")
        self.navigate(self.last_page())
        self.refresh_all()

    def can_leave(self) -> bool:
        return self.resolve_edits()

    def can_close(self) -> bool:
        return self.resolve_edits()

    def cancel_selected(self, channel: str | None) -> None:
        """Stop the task picked in the status bar; the other channels keep running."""
        if channel in self.tasks.runners:
            self.tasks.runners[channel].cancel_current()
            self.host.status.render_task()

    def cancel_all(self) -> None:
        self.tasks.cancel_all()

    def model_settings_changed(self) -> None:
        self.refresh_model_status()

    def refresh_all(self) -> None:
        """Refresh, telling the status bar when the project cannot be read."""
        try:
            self.refresh()
        except (OSError, WorkspaceError) as exc:
            self.host.status.set_error("无法读取短视频项目", str(exc))

    def last_page(self) -> str:
        page = self.settings.value(self.setting_key("page"), "video_source")
        return page if page in self.pages else "video_source"

    # ---- model services
    def model(self) -> str:
        return self.text_model

    def configure_text_model(self) -> None:
        """Addresses and keys are shared with the novel; which service to use is ours."""
        require(not self.tasks.any_busy, "PROJECT_BUSY", "请在任务结束后修改生成模型配置")
        dialog = ModelConfigDialog(self.text_model, self)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.text_model = dialog.selected_provider()
                self.settings.setValue("text_model", self.text_model)
                self.host.model_settings_saved()
                self.host.status.show_notice(
                    "success", "模型配置已保存", "已生效，将用于下一次生成任务"
                )
        finally:
            dialog.deleteLater()

    def refresh_model_status(self) -> None:
        provider = self.model()
        _, detail = configuration_status(provider)
        self.model_config_button.setText("服务配置 · " + PROVIDER_LABELS[provider])
        try:
            config = get_review_config()
            review_detail = f"校验模型：{PROVIDER_LABELS[config.provider]} · {config.model}"
        except ValueError as exc:
            review_detail = str(exc)
        self.model_config_button.setToolTip(f"生成模型：{detail}\n{review_detail}")

    def configure_review_model(self) -> None:
        require(not self.tasks.any_busy, "PROJECT_BUSY", "请在任务结束后修改校验模型配置")
        dialog = ModelConfigDialog(review_provider(), self, profile=REVIEW_SERVICE)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.refresh_model_status()
                self.notice("校验模型配置已保存")
        finally:
            dialog.deleteLater()

    def configure_image_model(self) -> None:
        require(not self.tasks.any_busy, "PROJECT_BUSY", "请在任务结束后修改生图配置")
        dialog = ModelConfigDialog("hosted-llm", self, profile=IMAGE_SERVICE)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.notice("生图 API 配置已保存")
        finally:
            dialog.deleteLater()

    def setting_key(self, field: str) -> str:
        return fingerprint(self.output_dir)[:20] + "/" + field

    def configure_comfy(self) -> None:
        from .comfy_settings import ComfySettingsDialog

        require(not self.tasks.any_busy, "PROJECT_BUSY", "请在任务结束后修改工作流配置")
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
            "configure_text_model": "生成模型配置",
            "configure_review_model": "校验模型配置",
            "configure_image_model": "生图 API 配置",
            "generate_images": "资产图片生成",
            "generate_prompt": "视频提示词生成",
            "resume_prompt": "视频提示词继续",
            "resume_images": "资产图片继续",
            "send": "AI 回复",
            "query": "视频任务查询",
            "open_location": "视频目录打开",
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
                self.host.status.show_notice(
                    "success", name.removesuffix("保存") + "已保存", detail
                )
            return result
        except (WorkspaceError, OSError) as exc:
            self.host.status.show_notice("error", name + "失败", error_reason(exc))
            logging.getLogger(__name__).error("%s失败", name, exc_info=True)
            return False

    def notice(self, text: str) -> None:
        self.host.status.show_notice("success", text)

    def submit(self, work: VideoTask, busy_button: QPushButton | None = None) -> None:
        """Run a task on its channel; the status bar lists it and shows how it ended."""
        channel = self.tasks.admit(work)
        status = self.host.status

        def ended(**outcome: object) -> None:
            status.finish_task(channel, work, task_message(work.task_context, **outcome))

        def done() -> None:
            self.tasks.finish(channel, work)
            self.refresh_all()

        task = self.tasks.runners[channel].run(
            work, pass_progress=True, cancel_token=work.token,
            on_success=lambda value: ended(result=value),
            on_error=lambda exc: ended(error=exc),
            on_cancelled=lambda: ended(cancelled=True),
            on_done=done,
        )
        if task is None:
            self.tasks.finish(channel, work)
        else:
            status.start_task(channel, work, lambda: status_text(work))
            task.signals.progress.connect(
                lambda _text, fraction: status.update_task(channel, work, fraction),
                Qt.QueuedConnection,
            )
            self.refresh_task_controls()
        self.chat.refresh()

    def refresh_task_controls(self) -> None:
        self.refresh_buttons()
        if self.showing_shot_tab():
            self.specs.media.refresh_result()
            self.specs.refresh_shot_marks()

    def refresh_buttons(self) -> None:
        """Which buttons can be pressed while tasks run; cheap, so every page is updated."""
        self.project_button.setEnabled(not self.tasks.any_busy)
        self.model_config_button.setEnabled(not self.tasks.any_busy)
        for control in [
            self.source.generate_button,
            *[
                button
                for page in self.artifact_pages()
                for button in (
                    [page.generate_button, page.next_button, page.resume_button]
                    if isinstance(page, ArtifactPage)
                    else [page.generate_button, page.resume_button]
                )
            ],
        ]:
            control.setEnabled(not self.tasks.busy("text"))
        self.chat.refresh_send_button()
        self.specs.images_page.update_controls()

    def showing_shot_tab(self) -> bool:
        """The video area is on screen only on 提示词 → 镜头."""
        return self.right.currentWidget() is self.specs and self.specs.on_shot_tab()

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

    def shown_pages(self) -> list[SourcePage | ArtifactPage | PromptPage]:
        """The page on screen; on 改写 only its visible tab."""
        page = self.right.currentWidget()
        if page is self.pages["video_source"]:
            return [self.source]
        if page is self.adaptation:
            return [self.adaptation.currentWidget()]
        return [page] if isinstance(page, (ArtifactPage, PromptPage)) else []

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
        # Only the page being shown; the chat's attached-shot line follows the page change.
        self.refresh_shown()
        return True

    @contextmanager
    def project_reads(self) -> Iterator[None]:
        """Hold the project lock and let the whole refresh share one read of the project."""
        root = self.repository.root
        lock = (
            project_lock(str(root / ".v2-project.lock"), timeout=5)
            if root.exists()
            else nullcontext()
        )
        with lock, shared_project_reads():
            yield

    def refresh(self) -> None:
        """After saves, finished tasks or a project change: the page on screen and the chat.

        Hidden pages are left as they are and refresh when shown.
        """
        with self.project_reads():
            self.refresh_model_status()
            for page in self.shown_pages():
                page.refresh()
            config = self.repository.current("config")
            self.project_button.setText(
                (config["content"]["title"] if config else "新项目") or "新项目"
            )
            self.chat.refresh()
            # Also the video status: a finished video must show even while prompts are unsaved.
            self.refresh_task_controls()

    def refresh_shown(self) -> None:
        with self.project_reads():
            for page in self.shown_pages():
                page.refresh()
            # Pages are never left with unsaved edits, so a shown 镜头 tab has just reloaded
            # its video area (which also re-marks the shot chips).
            self.refresh_buttons()

    def refresh_adaptation_tab(self) -> None:
        if self.right.currentWidget() is self.adaptation:
            self.refresh_shown()

    def resume_generation(self, stage: str, button: QPushButton | None = None) -> None:
        from core.short_video.v2.runtime import ResumeWork, get_recovery

        require(not self.tasks.busy("text"), "PROJECT_BUSY", "已有任务在运行")
        if not self.resolve_edits():
            return
        task = get_recovery(self.repository, stage)
        require(task is not None and task["valid"], "STALE_INPUT", "原任务输入已变化，请重新生成")
        self.submit(ResumeWork(self.repository, task["id"], transport=self.transport), button)

    def start_creation(
        self, stage: str, button: QPushButton | None = None, *, asset_key: str | None = None
    ) -> None:
        require(not self.tasks.busy("text"), "PROJECT_BUSY", "已有任务在运行")
        # Save visible edits before freezing any paid request.
        if self.source.dirty and not self.source.save():
            return
        page = self.current_artifact_page()
        if page and page.dirty:
            page.save()

        def prepare() -> VideoTask:
            if stage == "storyboard":
                command = prepare_storyboard_task(self.repository, self.model())
                return CreationWork(self.repository, command, transport=self.transport)
            if stage == "prompts":
                command = prepare_prompt_task(self.repository, self.model(), asset_key=asset_key)
                return CreationWork(self.repository, command, transport=self.transport)
            command = WorkflowService(self.repository, self.model()).prepare(stage)
            return WorkspaceWork(self.repository, command, transport=self.transport)

        try:
            work = prepare()
        except WorkspaceError as exc:
            if exc.code == "INPUT_TOO_LARGE":
                QMessageBox.warning(self, "超出输入容量", str(exc))
                return
            raise
        target = {
            "novel_copy": "video_adaptation",
            "video_copy": "video_adaptation",
            "storyboard": "video_storyboard",
            "prompts": "video_prompts",
        }[stage]
        if stage in {"novel_copy", "video_copy"}:
            self.adaptation.setCurrentWidget(self.novel if stage == "novel_copy" else self.video)
        self.navigate(target)
        self.submit(work, button)

    def update_shot_prompts(self) -> None:
        board = self.repository.current("storyboard")
        if not board:
            return
        try:
            WorkflowService(self.repository).compile_storyboard_prompts(
                expected_revision=self.repository.load()["revision"]
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
        self.splitter.setSizes([int(v) for v in sizes] if sizes else [CHAT_WIDTH, 850])
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
        allow_partial = False
        service = ExportService(self.repository)
        while True:
            try:
                service.snapshot(keys=keys, allow_partial=allow_partial)
                break
            except WorkspaceError as exc:
                if exc.code == "EXPORT_INCOMPLETE" and not allow_partial:
                    if (
                        QMessageBox.question(self, "缺少部分内容", str(exc) + "\n导出已有内容？")
                        != QMessageBox.Yes
                    ):
                        return
                    allow_partial = True
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
            self.perform(self.configure_text_model)
            update_model_summary()

        def update_model_summary() -> None:
            model_summary.setText(
                PROVIDER_LABELS[self.model()] + " · " + self.model_config_button.toolTip()
            )

        update_model_summary()
        create_action_button("模型配置", configure_model, layout, self)
        layout.addWidget(
            QLabel("各服务商的接口地址、API 密钥和模型与小说创作共用，保存配置后立即生效；"
                   "短视频用哪个服务商单独记住，不影响小说创作。")
        )
        create_action_button(
            "返回小说创作", lambda: (dialog.accept(), self.host.open_feature("novel")), layout, self
        )
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def new_project(self) -> None:
        require(not self.tasks.any_busy, "PROJECT_BUSY", "运行中无法切换项目")
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
        require(not self.tasks.any_busy, "PROJECT_BUSY", "运行中无法切换项目")
        path = QFileDialog.getExistingDirectory(self, "打开项目目录", self.output_dir)
        if path:
            self.directory.setText(path)
            self.open_project()

    def open_project(self) -> None:
        require(not self.tasks.any_busy, "PROJECT_BUSY", "运行中无法切换项目")
        if not self.resolve_edits():
            self.directory.setText(self.output_dir)
            return
        candidate = ProjectRepository(self.directory.text())
        try:
            candidate.load()
            ConversationService(candidate).recover()
            from core.short_video.v2.image_generation import ImageGenerationService

            ImageGenerationService(candidate).recover()
        except (WorkspaceError, OSError):
            self.directory.setText(self.output_dir)
            raise
        self.repository, self.output_dir = candidate, candidate.output_dir
        self.host.status.clear_tasks()
        self.settings.setValue("last_project", self.output_dir)
        self.chat.conversation_id = None
        self.chat.input.clear()
        self.restore_layout()
        self.refresh()
        self.navigate(self.last_page())
        self.refresh_all()
        self.notice("已打开项目")
