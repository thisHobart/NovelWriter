"""Project-local ComfyUI settings; the image workflow is configuration only."""

from __future__ import annotations

import asyncio

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.gui.task_runner import TaskRunner
from core.short_video.v2.comfy_mcp import ComfyMCPClient
from core.short_video.v2.contracts import JsonObject
from core.short_video.v2.media_repository import MediaRepository
from core.short_video.v2.media_service import validate_nodes
from core.short_video.v2.media_workflows import MediaConfig, load_video_workflow


class ComfySettingsDialog(QDialog):
    def __init__(self, output_dir: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.output_dir = output_dir
        self.setWindowTitle("ComfyUI 工作流配置")
        self.resize(680, 250)
        config = MediaConfig.load(output_dir)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.address = QLineEdit(config.base_url)
        form.addRow("ComfyUI 地址", self.address)
        self.video_workflow = self.workflow_field(form, "视频工作流", config.video_workflow)
        self.image_workflow = self.workflow_field(form, "图片工作流（预留）", config.image_workflow)
        layout.addLayout(form)
        layout.addWidget(
            QLabel("内置 MCP 桥接，无需另起服务。图片工作流本轮只保存配置，不执行生图。")
        )
        self.feedback = QLabel("")
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.test_button = buttons.addButton("测试连接", QDialogButtonBox.ActionRole)
        self.save_button = buttons.button(QDialogButtonBox.Save)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        self.test_button.clicked.connect(self.test_connection)
        layout.addWidget(buttons)
        self.runner = TaskRunner()

    def workflow_field(self, form: QFormLayout, label: str, value: str) -> QLineEdit:
        bar = QHBoxLayout()
        field = QLineEdit(value)
        choose = QPushButton("选择 JSON")
        choose.clicked.connect(lambda: self.choose_workflow(field))
        bar.addWidget(field, 1)
        bar.addWidget(choose)
        form.addRow(label, bar)
        return field

    def choose_workflow(self, field: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 API 格式工作流", "", "API JSON (*.json)")
        if path:
            field.setText(path)

    def config(self) -> MediaConfig:
        return MediaConfig(
            self.address.text().strip().rstrip("/"),
            self.video_workflow.text().strip(),
            self.image_workflow.text().strip(),
        )

    def save(self) -> None:
        # 同步配置操作也走任务边界，错误类型和回溯交给 TaskRunner。
        config = self.config()
        self.runner.run(
            lambda: config.save(self.output_dir),
            disable=[self.save_button, self.test_button],
            on_success=lambda _: self.accept(),
            on_error=self.show_error,
        )

    def test_connection(self) -> None:
        config = self.config()

        async def check() -> JsonObject:
            config.validate()
            client = ComfyMCPClient(config.base_url, MediaRepository(self.output_dir))
            async with client.connect():
                result = await client.call("comfy_status")
                template = load_video_workflow(config.video_workflow)
                validate_nodes(template.graph, result["nodes"])
                return {"version": result["system"]["comfyui_version"]}

        self.feedback.setText("正在检查连接、视频节点和模型，不执行生成…")
        self.runner.run(
            lambda: asyncio.run(check()),
            disable=[self.save_button, self.test_button],
            on_success=lambda result: self.feedback.setText(
                f"连接成功：ComfyUI {result['version']}；视频节点及模型可用。"
            ),
            on_error=self.show_error,
        )

    def show_error(self, exc: Exception) -> None:
        self.feedback.setText(f"{type(exc).__name__}: {exc}")

    def reject(self) -> None:
        if self.runner.busy:
            self.feedback.setText("正在检查或保存配置，请等待完成后关闭。")
            return
        super().reject()
