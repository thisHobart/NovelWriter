"""Manual reference-image input and local video preview for the prompt workspace."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.short_video.v2.media_repository import MediaRepository
from core.short_video.v2.media_service import MediaService, MediaWork
from core.short_video.v2.media_workflows import MediaConfig, parse_video_workflow
from core.short_video.v2.models import require

if TYPE_CHECKING:
    from .workbench import Workbench

DEFAULT_VIDEO_PROMPT = (
    "以 <Picture 1> 为画面参考，保持主体外观、环境与构图一致，"
    "生成自然连贯的动态画面和轻微镜头运动。无文字、无水印。"
)


class MediaPreview(QWidget):
    def __init__(self, workbench: Workbench) -> None:
        super().__init__()
        self.workbench = workbench
        self.current_job = None
        self.target = {}
        self.video_path = None
        self.project_dir = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 0, 0, 0)
        layout.addWidget(QLabel("ComfyUI 图生视频"))
        bar = QHBoxLayout()
        self.workflows = QComboBox()
        self.config_button = QPushButton("配置")
        bar.addWidget(QLabel("工作流"))
        bar.addWidget(self.workflows, 1)
        bar.addWidget(self.config_button)
        layout.addLayout(bar)
        self.input_area = QScrollArea()
        self.input_area.setWidgetResizable(True)
        self.input_area.setMaximumHeight(240)
        layout.addWidget(self.input_area)
        self.image_fields = {}
        self.parameter_fields = {}
        self.drafts = {}
        self.loaded_workflow_id = None
        layout.addWidget(QLabel("视频提示词"))
        self.prompt = QPlainTextEdit()
        self.prompt.setMaximumHeight(130)
        layout.addWidget(self.prompt, 1)
        self.parameter_area = QWidget()
        self.parameter_form = QFormLayout(self.parameter_area)
        layout.addWidget(self.parameter_area)
        bar = QHBoxLayout()
        self.generate_button = QPushButton("生成视频")
        self.query_button = QPushButton("查询原任务")
        bar.addWidget(self.generate_button)
        bar.addWidget(self.query_button)
        layout.addLayout(bar)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.video = QVideoWidget()
        self.video.setMinimumHeight(140)
        layout.addWidget(self.video, 2)
        self.audio = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.player.errorOccurred.connect(self.playback_error)
        bar = QHBoxLayout()
        self.play_button = QPushButton("播放／暂停")
        self.open_button = QPushButton("打开视频位置")
        bar.addWidget(self.play_button)
        bar.addWidget(self.open_button)
        bar.addStretch()
        layout.addLayout(bar)
        self.workflows.currentIndexChanged.connect(lambda: workbench.perform(self.select_workflow))
        self.generate_button.clicked.connect(lambda: workbench.perform(self.generate))
        self.query_button.clicked.connect(lambda: workbench.perform(self.query))
        self.config_button.clicked.connect(lambda: workbench.perform(workbench.configure_comfy))
        self.play_button.clicked.connect(self.toggle_playback)
        self.open_button.clicked.connect(self.open_location)

    def task_controls(self) -> list[QWidget]:
        return [
            self.workflows,
            self.input_area,
            self.parameter_area,
            self.prompt,
            self.generate_button,
            self.query_button,
            self.config_button,
        ]

    def choose_image(self, node_id: str) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "", "图片 (*.png *.jpg *.jpeg *.webp)"
        )
        if path:
            self.import_image(path, node_id)

    def import_image(self, path: str, node_id: str) -> None:
        if self.project_dir != self.workbench.output_dir:
            self.refresh()
        repository = MediaRepository(self.workbench.output_dir)
        require(not QPixmap(path).isNull(), "INVALID_INPUT", f"图片无法解码：{Path(path).name}")
        image_id = repository.import_image(path)["image_id"]
        self.remember_draft()
        self.drafts[self.loaded_workflow_id]["images"][node_id] = image_id
        self.load_workflow()

    def image_selection(self) -> dict[str, str]:
        return {node_id: fields[0].currentData() for node_id, fields in self.image_fields.items()}

    def parameter_values(self, *, raw: bool = False) -> dict:
        values = {}
        for key, control in self.parameter_fields.items():
            if isinstance(control, QCheckBox):
                values[key] = control.isChecked()
            elif isinstance(control, QLineEdit):
                values[key] = control.text() if raw else int(control.text())
            else:
                values[key] = control.value()
        return values

    def remember_draft(self) -> None:
        if self.loaded_workflow_id is not None:
            self.drafts[self.loaded_workflow_id] = {
                "images": self.image_selection(),
                "prompt": self.prompt.toPlainText(),
                "parameters": self.parameter_values(raw=True),
            }

    def refresh(self) -> None:
        if self.project_dir != self.workbench.output_dir:
            self.project_dir = self.workbench.output_dir
            self.loaded_workflow_id = None
            self.drafts.clear()
            self.player.stop()
            self.player.setSource(QUrl())
            self.video_path = None
        self.remember_draft()
        self.catalog = MediaConfig.load(self.workbench.output_dir)
        self.workflows.blockSignals(True)
        self.workflows.clear()
        for workflow in self.catalog.workflows:
            self.workflows.addItem(workflow["name"], workflow["workflow_id"])
        self.workflows.setCurrentIndex(self.workflows.findData(self.catalog.selected_workflow_id))
        self.workflows.blockSignals(False)
        self.load_workflow()

    def select_workflow(self) -> None:
        self.remember_draft()
        self.catalog.selected_workflow_id = self.workflows.currentData()
        self.catalog.save(self.workbench.output_dir)
        self.load_workflow()

    def load_workflow(self) -> None:
        selected = self.catalog.selected(self.workflows.currentData())
        self.template = parse_video_workflow(selected["graph"])
        self.loaded_workflow_id = selected["workflow_id"]
        draft = self.drafts.get(self.loaded_workflow_id, {})
        self.image_fields = {}
        container = QWidget()
        form = QFormLayout(container)
        repository = MediaRepository(self.workbench.output_dir)
        images = repository.images()
        for image_input in self.template.image_inputs:
            row = QWidget()
            bar = QHBoxLayout(row)
            combo = QComboBox()
            combo.addItem("请选择图片", None)
            for image in images:
                combo.addItem(image["name"], image["image_id"])
            selected_id = draft.get("images", {}).get(image_input.node_id)
            combo.setCurrentIndex(max(0, combo.findData(selected_id)))
            choose = QPushButton("选图")
            preview = QLabel()
            preview.setFixedSize(110, 64)
            bar.addWidget(combo, 1)
            bar.addWidget(choose)
            bar.addWidget(preview)
            form.addRow(image_input.label, row)
            self.image_fields[image_input.node_id] = (combo, choose, preview)
            choose.clicked.connect(
                lambda checked=False, node_id=image_input.node_id: self.workbench.perform(
                    lambda: self.choose_image(node_id)
                )
            )
            combo.currentIndexChanged.connect(lambda: self.workbench.perform(self.refresh_result))
        self.input_area.setWidget(container)
        self.prompt.setPlainText(
            draft.get(
                "prompt",
                DEFAULT_VIDEO_PROMPT if self.template.kind == "reference" else self.template.prompt,
            )
        )
        while self.parameter_form.rowCount():
            self.parameter_form.removeRow(0)
        form = self.parameter_form
        self.parameter_fields = {}
        for parameter in self.template.parameters:
            value = draft.get("parameters", {}).get(parameter.key, parameter.default)
            if type(parameter.default) is bool:
                control = QCheckBox()
                control.setChecked(value)
            elif parameter.input_name == "noise_seed":
                control = QLineEdit(str(value))
            elif type(parameter.default) is int:
                control = QSpinBox()
                control.setRange(int(parameter.minimum), int(parameter.maximum))
                control.setValue(value)
            else:
                control = QDoubleSpinBox()
                control.setRange(parameter.minimum, parameter.maximum)
                control.setValue(value)
            form.addRow(parameter.label, control)
            self.parameter_fields[parameter.key] = control
        self.refresh_result()

    def refresh_result(self) -> None:
        repository = MediaRepository(self.workbench.output_dir)
        selection = self.image_selection()
        for node_id, (combo, choose, preview) in self.image_fields.items():
            image_id = selection[node_id]
            if image_id:
                image = repository.image(image_id)
                pixmap = QPixmap(str(repository.resolve(image["relative_path"])))
                preview.setPixmap(
                    pixmap.scaled(110, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
            else:
                preview.clear()
        self.current_job = None
        for job in reversed(repository.jobs()):
            snapshot = job["input_snapshot"]
            if "images" in snapshot:
                job_images = {
                    node_id: image["image_id"] for node_id, image in snapshot["images"].items()
                }
                workflow_id = snapshot["workflow_id"]
            else:
                job_images = {snapshot["image_node"]: snapshot["image"]["image_id"]}
                workflow_id = "minimax-reference"
            if workflow_id == self.loaded_workflow_id and job_images == selection:
                self.current_job = job
                break
        states = {
            "prepared": "准备中",
            "submitting": "提交中",
            "queued": "正在排队",
            "running": "正在生成",
            "collecting": "正在保存结果",
            "succeeded": "视频已保存",
            "failed": "生成失败",
            "unknown": "远程状态待确认",
            "interrupted": "已停止本地等待，远程可能仍在运行",
            "cancelled": "已取消",
        }
        job = self.current_job
        if job:
            text = states[job["status"]]
            if job["outputs"]:
                duration = job["outputs"][0].get("metadata", {}).get("duration")
                if isinstance(duration, (int, float)):
                    text += f"；实际时长 {duration:.2f} 秒"
            if job.get("error"):
                text += f"；{job['error']['type']}: {job['error']['message']}"
            self.status.setText(text)
        else:
            self.status.setText("")
        path = (
            repository.resolve(job["outputs"][0]["relative_path"])
            if job and job["outputs"]
            else None
        )
        if path != self.video_path:
            self.player.stop()
            self.player.setSource(QUrl.fromLocalFile(str(path)) if path else QUrl())
            self.video_path = path
        busy = self.workbench.host.runner.busy
        self.generate_button.setEnabled(all(selection.values()) and bool(selection) and not busy)
        self.query_button.setEnabled(
            bool(job and job["status"] not in {"succeeded", "cancelled", "failed"}) and not busy
        )
        self.open_button.setEnabled(path is not None)
        self.play_button.setEnabled(path is not None)

    def generate(self) -> None:
        require(not self.workbench.host.runner.busy, "PROJECT_BUSY", "已有任务在运行")
        prompt = self.prompt.toPlainText().strip()
        command = MediaService(self.workbench.output_dir).prepare_video(
            self.image_selection(),
            prompt,
            workflow_id=self.loaded_workflow_id,
            parameters=self.parameter_values(),
            target=self.target,
        )
        self.player.stop()
        self.workbench.submit(MediaWork(self.workbench.output_dir, command), self.generate_button)

    def query(self) -> None:
        require(self.current_job is not None, "INVALID_INPUT", "尚无视频任务")
        self.workbench.submit(
            MediaWork(self.workbench.output_dir, job_id=self.current_job["job_id"]),
            self.query_button,
        )

    def toggle_playback(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def open_location(self) -> None:
        if self.video_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.video_path.parent)))

    def playback_error(self, error: QMediaPlayer.Error, message: str) -> None:
        if error != QMediaPlayer.NoError:
            self.status.setText(f"视频播放失败：{message}；可打开视频位置使用外部播放器。")
