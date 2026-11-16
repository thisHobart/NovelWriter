"""Opt-in per-view image selection, batch progress and generated-image inspection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.short_video.v2.image_generation import ImageGenerationService, ImageGenerationWork
from core.short_video.v2.models import require

from .asset_image_preview import AssetImagePreview

if TYPE_CHECKING:
    from .workbench import Workbench

STATUS_NAMES = {
    "queued": "排队中",
    "requesting": "正在生成",
    "retrying": "等待网络重试",
    "received": "待下载／解码",
    "saving": "正在保存",
    "succeeded": "已生成",
    "failed": "失败",
    "unknown": "结果未知，重试可能重复计费",
    "paused": "待继续",
    "stale": "来源已变化，未生成",
}


class AssetImagesPage(QWidget):
    def __init__(self, workbench: Workbench) -> None:
        super().__init__()
        self.workbench = workbench
        self.rows = []
        self.batch_id = None
        self.can_resume = False
        self.project_dir = None
        self.last_statuses = {}
        self.batches = {}
        self.latest_batch_id = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        hint = QLabel(
            "按资产视图勾选后生成；不勾选、不点击生成就不会调用生图 API。每个视图生成一张，多镜头共享同一资产图。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        bar = QHBoxLayout()
        self.select_all_button = QPushButton("全选可生成项")
        self.clear_button = QPushButton("清空选择")
        self.select_all_button.clicked.connect(lambda: self.check_all(True))
        self.clear_button.clicked.connect(lambda: self.check_all(False))
        bar.addWidget(self.select_all_button)
        bar.addWidget(self.clear_button)
        bar.addStretch()
        bar.addWidget(QLabel("并行数"))
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 8)
        self.concurrency.setValue(2)
        bar.addWidget(self.concurrency)
        self.generate_button = QPushButton("生成所选图片")
        self.generate_button.clicked.connect(lambda: workbench.perform(self.generate_images))
        bar.addWidget(self.generate_button)
        self.resume_button = QPushButton("继续未完成")
        self.resume_button.clicked.connect(lambda: workbench.perform(self.resume_images))
        bar.addWidget(self.resume_button)
        layout.addLayout(bar)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        split = QSplitter(Qt.Horizontal)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["勾选", "资产及状态", "视图", "使用镜头", "图片状态"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.currentCellChanged.connect(lambda row, *_: self.show_row(row))
        self.table.itemChanged.connect(self.update_controls)
        split.addWidget(self.table)
        details = QWidget()
        detail_layout = QVBoxLayout(details)
        self.preview = AssetImagePreview()
        detail_layout.addWidget(self.preview)
        self.detail_status = QLabel()
        self.detail_status.setWordWrap(True)
        detail_layout.addWidget(self.detail_status)
        self.prompt = QPlainTextEdit()
        self.prompt.setReadOnly(True)
        self.prompt.setPlaceholderText("选择表格行查看对应提示词和图片")
        detail_layout.addWidget(self.prompt, 1)
        split.addWidget(details)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 1)
        layout.addWidget(split, 1)
        self.poll = QTimer(self)
        self.poll.setInterval(500)
        self.poll.timeout.connect(self.poll_progress)
        self.poll.start()

    def poll_progress(self) -> None:
        if self.isVisible() and self.workbench.tasks.busy("image"):
            self.workbench.perform(self.refresh)

    def checked_keys(self) -> list[str]:
        return [
            row["key"]
            for index, row in enumerate(self.rows)
            if self.table.item(index, 0).checkState() == Qt.Checked
        ]

    def check_all(self, checked: bool) -> None:
        for index, row in enumerate(self.rows):
            if not row["reason"]:
                self.table.item(index, 0).setCheckState(Qt.Checked if checked else Qt.Unchecked)

    def update_controls(self) -> None:
        busy = self.workbench.tasks.busy("image")
        self.generate_button.setEnabled(not busy and bool(self.checked_keys()))
        self.select_all_button.setEnabled(not busy and bool(self.rows))
        self.clear_button.setEnabled(not busy and bool(self.rows))
        self.concurrency.setEnabled(not busy)
        self.resume_button.setEnabled(not busy and self.can_resume)

    def refresh(self) -> None:
        service = ImageGenerationService(self.workbench.repository)
        project_changed = self.project_dir != self.workbench.output_dir
        checked = set(self.checked_keys()) if not project_changed else set()
        current = self.table.currentRow()
        selected = self.rows[current]["key"] if current >= 0 and not project_changed else None
        if project_changed:
            self.project_dir = self.workbench.output_dir
            self.last_statuses.clear()
        self.rows = service.rows()
        batches = list(service.store.load()["batches"].values())
        self.batches = {b["id"]: b for b in batches}
        batch = batches[-1] if batches else None
        self.latest_batch_id = batch["id"] if batch else None
        self.batch_id = batch["id"] if batch else None
        self.can_resume = bool(
            batch
            and any(i["status"] not in {"succeeded", "unknown", "stale"} for i in batch["items"])
        )
        self.resume_button.setVisible(self.can_resume)
        items = {i["key"]: (b["id"], i) for b in batches for i in b["items"]}
        busy = self.workbench.tasks.busy("image")
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.rows))
        for index, row in enumerate(self.rows):
            batch_id, item = items.get(row["key"], (None, None))
            row["batch_id"] = batch_id
            if (
                item
                and item["status"] == "unknown"
                and self.last_statuses.get(row["key"]) != "unknown"
            ):
                checked.discard(row["key"])
            if item:
                self.last_statuses[row["key"]] = item["status"]
            image = row["image"]
            status = row["reason"] or (
                "已生成" if image and image["current"] else "图片待更新" if image else "未生成"
            )
            error = ""
            if item:
                status = STATUS_NAMES.get(item["status"], item["status"])
                if item["prompt_hash"] != row["prompt_hash"]:
                    status = "来源已变化，图片待更新"
                error = item.get("error") or ""
                if (
                    item["status"] == "failed"
                    and service.store.media.resolve(item["response_path"]).exists()
                ):
                    error += "\n完整响应已保存：点击继续未完成仅处理下载或解码，不重新请求生图。"
                if item["status"] == "succeeded" and not item.get("current", True):
                    status = "已保存，来源已变化"
                    row["image"] = {**service.store.media.image(item["image_id"]), "current": False}
            status = row["reason"] or status
            row["display_status"], row["error"] = status, error
            checkbox = QTableWidgetItem()
            checkbox.setFlags(
                Qt.ItemIsEnabled
                | (Qt.ItemIsUserCheckable if not row["reason"] and not busy else Qt.NoItemFlags)
            )
            checkbox.setCheckState(
                Qt.Checked if row["key"] in checked and not row["reason"] else Qt.Unchecked
            )
            self.table.setItem(index, 0, checkbox)
            for column, text in enumerate(
                (
                    f"{row['name']} · {row['variant_label']}",
                    row["view_label"],
                    "、".join(f"{s:02}" for s in row["shots"]),
                    status,
                ),
                1,
            ):
                cell = QTableWidgetItem(text)
                cell.setToolTip(error or text)
                self.table.setItem(index, column, cell)
        self.table.blockSignals(False)
        row = next((i for i, r in enumerate(self.rows) if r["key"] == selected), -1)
        if row >= 0:
            self.table.setCurrentCell(row, 1)
        else:
            self.table.setCurrentCell(-1, -1)
        self.show_row(row)
        if not self.rows:
            board = self.workbench.repository.current("storyboard")
            self.status.setText("请先生成分镜。" if not board else "当前镜头尚无可生成的资产视图。")
        elif batch:
            count = sum(i["status"] == "succeeded" for i in batch["items"])
            self.status.setText(
                f"当前生图批次：已保存 {count}/{len(batch['items'])} 张。结果未知项需重新勾选；重试可能重复计费。"
            )
        else:
            self.status.setText(
                "提示词就绪后，勾选所需视图并点击生成；也可以继续使用现有文字功能。"
            )
        self.select_batch(self.batch_id)
        active = self.workbench.tasks.works.get("image")
        if busy and isinstance(active, ImageGenerationWork) and active.token.cancelled:
            self.status.setText(
                "正在停止，等待已发送请求返回；已返回的图片会保存，不再启动排队项。"
            )
        self.update_controls()

    def select_batch(self, batch_id: str | None) -> None:
        batch = self.batches.get(batch_id)
        self.batch_id = batch_id
        self.can_resume = bool(
            batch
            and any(i["status"] not in {"succeeded", "unknown", "stale"} for i in batch["items"])
        )
        self.resume_button.setVisible(self.can_resume)
        if batch and self.rows:
            count = sum(i["status"] == "succeeded" for i in batch["items"])
            self.status.setText(
                f"当前资产的生图批次：已保存 {count}/{len(batch['items'])} 张。结果未知项需重新勾选，重试可能重复计费。"
            )
        self.update_controls()

    def show_row(self, index: int) -> None:
        row = self.rows[index] if 0 <= index < len(self.rows) else None
        self.select_batch(
            row.get("batch_id") or self.latest_batch_id if row else self.latest_batch_id
        )
        self.preview.set_image(self.workbench.output_dir, row["image"] if row else None)
        self.prompt.setPlainText(
            row["positive_prompt"] + "\n\n排除或避免的元素：\n" + row["negative_prompt"]
            if row
            else ""
        )
        self.detail_status.setText(
            "\n".join(filter(None, [row["display_status"], row["error"]])) if row else ""
        )

    def generate_images(self) -> None:
        require(not self.workbench.tasks.busy("image"), "PROJECT_BUSY", "已有生图任务在运行")
        command = ImageGenerationService(self.workbench.repository).prepare(
            self.checked_keys(), concurrency=self.concurrency.value()
        )
        self.workbench.submit(
            ImageGenerationWork(self.workbench.repository, command), self.generate_button
        )

    def resume_images(self) -> None:
        require(
            not self.workbench.tasks.busy("image") and bool(self.batch_id),
            "PROJECT_BUSY",
            "请在当前任务结束后继续",
        )
        self.workbench.submit(
            ImageGenerationWork(self.workbench.repository, batch_id=self.batch_id),
            self.resume_button,
        )
