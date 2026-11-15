"""Read-only reference-asset list and view prompts for the selected storyboard shot."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.short_video.v2.contracts import JsonObject

from .asset_image_preview import AssetImagePreview

KIND_LABELS = {"scene": "场景", "character": "人物", "prop": "道具"}


class ShotAssetsPanel(QWidget):
    image_requested = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.assets: list[JsonObject] = []
        self.asset: JsonObject | None = None
        self.output_dir = "."
        self.can_use_images = lambda: True
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("当前镜头所需参考资产"))
        hint = QLabel("视图供按镜头角度选择，无需为每个镜头准备全部视图。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["所需参考资产", "可选视图"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setMinimumHeight(90)
        self.table.setMaximumHeight(190)
        self.table.cellClicked.connect(self.show_asset)
        layout.addWidget(self.table)
        self.details = QWidget()
        detail_layout = QVBoxLayout(self.details)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.title = QLabel()
        self.title.setWordWrap(True)
        bar.addWidget(self.title, 1)
        self.collapse_button = QPushButton("收起详情")
        self.collapse_button.clicked.connect(self.collapse)
        bar.addWidget(self.collapse_button)
        detail_layout.addLayout(bar)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("视图"))
        self.views = QComboBox()
        self.views.currentIndexChanged.connect(self.show_view)
        bar.addWidget(self.views, 1)
        detail_layout.addLayout(bar)
        self.status = QLabel()
        detail_layout.addWidget(self.status)
        self.preview = AssetImagePreview()
        detail_layout.addWidget(self.preview)
        image_bar = QHBoxLayout()
        image_bar.addWidget(QLabel("使用到图片节点"))
        self.image_nodes = QComboBox()
        self.image_nodes.currentIndexChanged.connect(self.update_image_button)
        image_bar.addWidget(self.image_nodes, 1)
        self.use_image_button = QPushButton("使用图片")
        self.use_image_button.clicked.connect(self.use_image)
        image_bar.addWidget(self.use_image_button)
        detail_layout.addLayout(image_bar)
        self.positive = QPlainTextEdit()
        self.negative = QPlainTextEdit()
        self.copy_positive = QPushButton("复制资产正向")
        self.copy_negative = QPushButton("复制资产负面")
        for label, edit, button in (
            ("资产正向提示词", self.positive, self.copy_positive),
            ("资产负面提示词", self.negative, self.copy_negative),
        ):
            edit.setReadOnly(True)
            edit.setMinimumHeight(60)
            edit.setMaximumHeight(120 if edit is self.positive else 80)
            detail_layout.addWidget(QLabel(label))
            detail_layout.addWidget(edit)
            button.setMaximumWidth(120)
            button.clicked.connect(
                lambda checked=False, field=edit: QApplication.clipboard().setText(
                    field.toPlainText()
                )
            )
            detail_layout.addWidget(button)
        layout.addWidget(self.details)
        self.details.hide()
        self.hide()

    def set_image_nodes(self, output_dir: str, nodes: list[tuple[str, str]]) -> None:
        self.output_dir = output_dir
        selected = self.image_nodes.currentData()
        self.image_nodes.clear()
        self.image_nodes.addItem("请选择图片节点", None)
        for node_id, label in nodes:
            self.image_nodes.addItem(label, node_id)
        self.image_nodes.setCurrentIndex(max(0, self.image_nodes.findData(selected)))
        self.update_image_button()

    def update_image_button(self) -> None:
        self.use_image_button.setEnabled(
            bool(
                self.preview.image
                and self.image_nodes.currentData()
                and self.preview.open_button.isEnabled()
                and self.can_use_images()
            )
        )

    def use_image(self) -> None:
        if self.preview.image and self.image_nodes.currentData():
            self.image_requested.emit(
                self.preview.image["image_id"], self.image_nodes.currentData()
            )

    def set_assets(self, assets: list[JsonObject]) -> None:
        self.assets = assets
        self.collapse()
        self.table.clearSelection()
        self.table.setRowCount(len(assets))
        for row, asset in enumerate(assets):
            title = f"{KIND_LABELS[asset['kind']]}：{asset['name']} · {asset['variant_label']}"
            labels = "、".join(view["label"] for view in asset["views"])
            if not any(view["available"] for view in asset["views"]):
                labels += "（提示词尚未生成）"
            for column, value in enumerate((title, labels)):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, column, item)
        self.table.resizeRowsToContents()
        height = self.table.horizontalHeader().height() + 2 * self.table.frameWidth()
        height += sum(self.table.rowHeight(row) for row in range(len(assets)))
        self.table.setFixedHeight(min(190, max(90, height)))
        self.setVisible(bool(assets))

    def collapse(self) -> None:
        self.details.hide()
        self.asset = None
        self.views.clear()
        self.title.clear()
        self.status.clear()
        self.positive.clear()
        self.negative.clear()
        self.preview.set_image(self.output_dir, None)
        self.update_image_button()

    def show_asset(self, row: int, column: int = 0) -> None:
        self.asset = self.assets[row]
        self.title.setText(
            f"{KIND_LABELS[self.asset['kind']]}：{self.asset['name']} · {self.asset['variant_label']}"
        )
        self.views.blockSignals(True)
        self.views.clear()
        for view in self.asset["views"]:
            label = view["label"] + ("" if view["available"] else "（未生成）")
            self.views.addItem(label, view["view_id"])
        first = next((i for i, view in enumerate(self.asset["views"]) if view["available"]), 0)
        self.views.setCurrentIndex(first)
        self.views.blockSignals(False)
        self.show_view()
        self.details.show()

    def show_view(self) -> None:
        index = self.views.currentIndex()
        view = self.asset["views"][index] if self.asset and index >= 0 else {}
        self.preview.set_image(self.output_dir, view.get("image"))
        self.update_image_button()
        self.status.setText("" if view.get("available") else "提示词尚未生成")
        for edit, button, field in (
            (self.positive, self.copy_positive, "positive_prompt"),
            (self.negative, self.copy_negative, "negative_prompt"),
        ):
            value = view.get(field, "")
            edit.setPlainText(value)
            button.setEnabled(bool(value.strip()))
