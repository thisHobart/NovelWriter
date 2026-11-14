"""Paste and save the project's fixed ComfyUI workflow catalog."""

from __future__ import annotations

import asyncio
from copy import deepcopy

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.gui.task_runner import TaskRunner
from core.short_video.v2.comfy_mcp import ComfyMCPClient
from core.short_video.v2.contracts import JsonObject
from core.short_video.v2.media_repository import MediaRepository
from core.short_video.v2.media_service import validate_nodes
from core.short_video.v2.media_workflows import MediaConfig, parse_video_workflow
from core.short_video.v2.models import encode


class ComfySettingsDialog(QDialog):
    def __init__(self, output_dir: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.output_dir = output_dir
        self.catalog = MediaConfig.load(output_dir)
        self.setWindowTitle("ComfyUI 工作流配置")
        self.resize(760, 600)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.address = QLineEdit(self.catalog.base_url)
        form.addRow("服务地址", self.address)
        bar = QHBoxLayout()
        self.workflows = QComboBox()
        for workflow in self.catalog.workflows:
            self.workflows.addItem(workflow["name"], workflow["workflow_id"])
        self.new_button = QPushButton("新增")
        bar.addWidget(self.workflows, 1)
        bar.addWidget(self.new_button)
        form.addRow("工作流", bar)
        self.name = QLineEdit()
        form.addRow("名称", self.name)
        layout.addLayout(form)
        layout.addWidget(QLabel("API JSON"))
        self.api_json = QPlainTextEdit()
        self.api_json.setPlaceholderText("粘贴固定工作流的 API JSON")
        layout.addWidget(self.api_json, 1)
        self.feedback = QLabel()
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.test_button = buttons.addButton("测试连接", QDialogButtonBox.ActionRole)
        self.save_button = buttons.button(QDialogButtonBox.Save)
        self.save_button.setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        self.test_button.clicked.connect(self.test_connection)
        self.workflows.currentIndexChanged.connect(self.load_selected)
        self.new_button.clicked.connect(self.new_workflow)
        layout.addWidget(buttons)
        self.runner = TaskRunner()
        self.workflows.setCurrentIndex(self.workflows.findData(self.catalog.selected_workflow_id))
        self.load_selected()

    def load_selected(self) -> None:
        workflow_id = self.workflows.currentData()
        if workflow_id is None:
            self.name.clear()
            self.api_json.clear()
        else:
            record = self.catalog.selected(workflow_id)
            self.name.setText(record["name"])
            self.api_json.setPlainText(encode(record["graph"]))
        self.feedback.clear()

    def new_workflow(self) -> None:
        self.workflows.setCurrentIndex(-1)
        self.name.setFocus()

    def controls(self) -> list[QWidget]:
        return [
            self.save_button,
            self.test_button,
            self.workflows,
            self.new_button,
            self.address,
            self.name,
            self.api_json,
        ]

    def save(self) -> None:
        # 读取控件在主线程，JSON 校验与磁盘保存交给任务边界反馈原始异常。
        name, api_json = self.name.text(), self.api_json.toPlainText()
        workflow_id = self.workflows.currentData()
        config = deepcopy(self.catalog)
        config.base_url = self.address.text().strip().rstrip("/")

        def write() -> None:
            config.put_workflow(name, api_json, workflow_id)
            config.save(self.output_dir)

        self.runner.run(
            write,
            disable=self.controls(),
            on_success=lambda _: self.accept(),
            on_error=self.show_error,
        )

    def test_connection(self) -> None:
        name, api_json = self.name.text(), self.api_json.toPlainText()
        workflow_id = self.workflows.currentData()
        config = deepcopy(self.catalog)
        config.base_url = self.address.text().strip().rstrip("/")

        async def check() -> JsonObject:
            config.put_workflow(name, api_json, workflow_id)
            config.validate()
            client = ComfyMCPClient(config.base_url, MediaRepository(self.output_dir))
            async with client.connect():
                result = await client.call("comfy_status")
                template = parse_video_workflow(config.selected()["graph"])
                validate_nodes(template.graph, result["nodes"])
                return {"version": result["system"]["comfyui_version"]}

        self.feedback.setText("正在检查连接、节点和模型…")
        self.runner.run(
            lambda: asyncio.run(check()),
            disable=self.controls(),
            on_success=lambda result: self.feedback.setText(
                f"连接成功：ComfyUI {result['version']}；节点及模型可用。"
            ),
            on_error=self.show_error,
        )

    def show_error(self, exc: Exception) -> None:
        self.feedback.setText(f"{type(exc).__name__}: {exc}")

    def reject(self) -> None:
        if self.runner.busy:
            return
        super().reject()
