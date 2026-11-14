"""Manual reference-image input and local video preview for the prompt workspace."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.short_video.v2.media_repository import MediaRepository
from core.short_video.v2.media_service import MediaService, MediaWork
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
        self.images = QComboBox()
        self.import_button = QPushButton("导入图片")
        bar.addWidget(self.images, 1)
        bar.addWidget(self.import_button)
        layout.addLayout(bar)
        self.image_preview = QLabel("导入一张 PNG／JPEG／WebP 参考图片")
        self.image_preview.setAlignment(Qt.AlignCenter)
        self.image_preview.setMinimumHeight(90)
        self.image_preview.setMaximumHeight(120)
        layout.addWidget(self.image_preview)
        layout.addWidget(QLabel("视频提示词（可直接修改，不写回分镜）"))
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("描述动作与运镜，使用 <Picture 1> 引用当前图片。")
        self.prompt.setMaximumHeight(130)
        layout.addWidget(self.prompt, 1)
        bar = QHBoxLayout()
        self.generate_button = QPushButton("生成视频")
        self.query_button = QPushButton("查询原任务")
        self.config_button = QPushButton("工作流配置")
        bar.addWidget(self.generate_button)
        bar.addWidget(self.query_button)
        bar.addWidget(self.config_button)
        layout.addLayout(bar)
        self.status = QLabel("图片生成暂未接入；可直接导入图片测试视频工作流。")
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
        self.images.currentIndexChanged.connect(lambda: workbench.perform(self.refresh_result))
        self.import_button.clicked.connect(lambda: workbench.perform(self.choose_images))
        self.generate_button.clicked.connect(lambda: workbench.perform(self.generate))
        self.query_button.clicked.connect(lambda: workbench.perform(self.query))
        self.config_button.clicked.connect(lambda: workbench.perform(workbench.configure_comfy))
        self.play_button.clicked.connect(self.toggle_playback)
        self.open_button.clicked.connect(self.open_location)

    def task_controls(self) -> list[QWidget]:
        return [
            self.images,
            self.import_button,
            self.generate_button,
            self.query_button,
            self.config_button,
        ]

    def choose_images(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "导入参考图片", "", "图片 (*.png *.jpg *.jpeg *.webp)"
        )
        if paths:
            self.import_images(paths)

    def import_images(self, paths: list[str]) -> None:
        repository = MediaRepository(self.workbench.output_dir)
        last_id = None
        for path in paths:
            require(not QPixmap(path).isNull(), "INVALID_INPUT", f"图片无法解码：{Path(path).name}")
            last_id = repository.import_image(path)["image_id"]
        self.refresh()
        self.images.setCurrentIndex(self.images.findData(last_id))

    def refresh(self) -> None:
        if self.project_dir != self.workbench.output_dir:
            self.project_dir = self.workbench.output_dir
            self.prompt.setPlainText(DEFAULT_VIDEO_PROMPT)
            self.player.stop()
            self.player.setSource(QUrl())
            self.video_path = None
        selected = self.images.currentData()
        self.images.blockSignals(True)
        self.images.clear()
        for image in MediaRepository(self.workbench.output_dir).images():
            self.images.addItem(image["name"], image["image_id"])
        if selected:
            self.images.setCurrentIndex(max(0, self.images.findData(selected)))
        self.images.blockSignals(False)
        self.refresh_result()

    def refresh_result(self) -> None:
        repository = MediaRepository(self.workbench.output_dir)
        image_id = self.images.currentData()
        if image_id:
            image = repository.image(image_id)
            pixmap = QPixmap(str(repository.resolve(image["relative_path"])))
            self.image_preview.setPixmap(
                pixmap.scaled(340, 115, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        else:
            self.image_preview.setText("导入一张 PNG／JPEG／WebP 参考图片")
        self.current_job = None
        for job in reversed(repository.jobs()):
            if job["input_snapshot"]["image"]["image_id"] == image_id:
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
            self.status.setText("每次使用一张参考图片生成一个视频；不需要先生成分镜或资产提示词。")
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
        self.generate_button.setEnabled(bool(image_id) and not busy)
        self.query_button.setEnabled(
            bool(job and job["status"] not in {"succeeded", "cancelled", "failed"}) and not busy
        )
        self.open_button.setEnabled(path is not None)
        self.play_button.setEnabled(path is not None)

    def generate(self) -> None:
        require(not self.workbench.host.runner.busy, "PROJECT_BUSY", "已有任务在运行")
        prompt = self.prompt.toPlainText().strip()
        if not prompt:
            prompt = DEFAULT_VIDEO_PROMPT
            self.prompt.setPlainText(prompt)
        command = MediaService(self.workbench.output_dir).prepare_video(
            self.images.currentData(),
            prompt,
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
