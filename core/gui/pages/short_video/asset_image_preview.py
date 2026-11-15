"""Local generated-image preview shared by asset batches and shot details."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from core.short_video.v2.contracts import JsonObject
from core.short_video.v2.media_repository import MediaRepository


class AssetImagePreview(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.image: JsonObject | None = None
        self.path = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.thumbnail = QLabel("尚无生成图片")
        self.thumbnail.setAlignment(Qt.AlignCenter)
        self.thumbnail.setMinimumHeight(120)
        layout.addWidget(self.thumbnail)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.open_button = QPushButton("查看大图")
        self.open_button.clicked.connect(self.open_image)
        layout.addWidget(self.open_button)
        self.set_image(".", None)

    def set_image(self, output_dir: str, image: JsonObject | None) -> None:
        self.image = image
        self.path = MediaRepository(output_dir).resolve(image["relative_path"]) if image else None
        pixmap = QPixmap(str(self.path)) if self.path else QPixmap()
        self.thumbnail.clear()
        self.open_button.setEnabled(not pixmap.isNull())
        if pixmap.isNull():
            self.thumbnail.setText("图片文件不可用" if image else "尚无生成图片")
        else:
            self.thumbnail.setPixmap(
                pixmap.scaled(300, 180, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        self.status.setText(
            ""
            if not image
            else "对应当前提示词"
            if image.get("current", True)
            else "图片待更新：来源提示词或视觉设定已变化，仍可手动使用旧图。"
        )

    def open_image(self) -> None:
        if not self.path:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(self.image["name"])
        dialog.resize(900, 700)
        layout = QVBoxLayout(dialog)
        scroll = QScrollArea()
        label = QLabel()
        label.setPixmap(QPixmap(str(self.path)))
        scroll.setWidget(label)
        layout.addWidget(scroll)
        dialog.exec()
