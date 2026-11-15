"""Video workflow inputs and the saved output location for the prompt workspace."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
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
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.gui.theme import CONTROL_HEIGHT, SP
from core.short_video.v2.media_repository import MediaRepository
from core.short_video.v2.media_service import MediaService, MediaWork
from core.short_video.v2.media_workflows import (
    MAX_REFERENCE_IMAGES,
    ImageInput,
    MediaConfig,
    parse_video_workflow,
)
from core.short_video.v2.models import require

if TYPE_CHECKING:
    from .workbench import Workbench

DEFAULT_VIDEO_PROMPT = (
    "以 <Picture 1> 为画面参考，保持主体外观、环境与构图一致，"
    "生成自然连贯的动态画面和轻微镜头运动。无文字、无水印。"
)


class MediaPreview(QWidget):
    inputs_changed = Signal()

    def __init__(self, workbench: Workbench) -> None:
        super().__init__()
        self.workbench = workbench
        self.current_job = None
        self.target = {}
        self.video_path = None
        self.project_dir = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SP[2], 0, 0, 0)
        self.settings_area = QScrollArea()
        self.settings_area.setWidgetResizable(True)
        self.settings_area.setFrameShape(QScrollArea.NoFrame)
        outer.addWidget(self.settings_area, 1)
        settings = QWidget()
        layout = QVBoxLayout(settings)
        layout.setContentsMargins(0, 0, SP[2], 0)
        layout.setSpacing(SP[2])
        self.settings_area.setWidget(settings)
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
        self.input_area.setFixedHeight(CONTROL_HEIGHT * 6)
        layout.addWidget(self.input_area)
        self.add_image_button = QPushButton("＋添加参考图片")
        layout.addWidget(self.add_image_button)
        self.image_fields = {}
        self.image_labels = {}
        self.additional_reference_slots = []
        self.prompt_image_bindings = {}
        self.parameter_fields = {}
        self.drafts = {}
        self.loaded_workflow_id = None
        layout.addWidget(QLabel("视频提示词"))
        self.prompt = QPlainTextEdit()
        self.prompt.setMinimumHeight(CONTROL_HEIGHT * 5)
        self.prompt.textChanged.connect(self.capture_prompt_image_bindings)
        layout.addWidget(self.prompt, 3)
        self.parameter_area = QWidget()
        self.parameter_area.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.parameter_form = QFormLayout(self.parameter_area)
        self.parameter_form.setContentsMargins(0, SP[2], 0, SP[2])
        self.parameter_form.setVerticalSpacing(SP[2])
        self.parameter_form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.parameter_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        layout.addWidget(self.parameter_area)
        bar = QHBoxLayout()
        self.generate_button = QPushButton("生成视频")
        self.query_button = QPushButton("查询原任务")
        bar.addWidget(self.generate_button)
        bar.addWidget(self.query_button)
        layout.addLayout(bar)
        self.status = QLabel()
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        bar = QHBoxLayout()
        self.open_button = QPushButton("打开视频位置")
        bar.addWidget(self.open_button)
        bar.addStretch()
        outer.addLayout(bar)
        self.workflows.currentIndexChanged.connect(lambda: workbench.perform(self.select_workflow))
        self.add_image_button.clicked.connect(lambda: workbench.perform(self.add_reference_image))
        self.generate_button.clicked.connect(lambda: workbench.perform(self.generate))
        self.query_button.clicked.connect(lambda: workbench.perform(self.query))
        self.config_button.clicked.connect(lambda: workbench.perform(workbench.configure_comfy))
        self.open_button.clicked.connect(self.open_location)

    def task_controls(self) -> list[QWidget]:
        return [
            self.workflows,
            self.input_area,
            self.add_image_button,
            self.parameter_area,
            self.prompt,
            self.generate_button,
            self.query_button,
            self.config_button,
        ]

    def use_image(self, image_id: str, node_id: str) -> None:
        require(not self.workbench.host.runner.busy, "PROJECT_BUSY", "请在任务结束后选择图片")
        self.refresh()
        require(node_id in self.image_fields, "INVALID_INPUT", "请选择有效的图片节点")
        combo = self.image_fields[node_id][0]
        index = combo.findData(image_id)
        require(index >= 0, "INVALID_INPUT", "图片不存在，请刷新后核对")
        combo.setCurrentIndex(index)

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

    def add_reference_image(self) -> None:
        require(
            len(self.image_fields) < MAX_REFERENCE_IMAGES,
            "INVALID_INPUT",
            "最多添加 9 个参考图片位置，请使用已有空位",
        )
        number = len(self.image_fields) + 1
        node_id = f"reference_input_{number}"
        while node_id in self.template.graph or node_id in self.image_fields:
            number += 1
            node_id = f"reference_input_{number}"
        self.additional_reference_slots.append(node_id)
        self.remember_draft()
        self.load_workflow()
        QTimer.singleShot(0, self, self.scroll_to_last_image)

    def scroll_to_last_image(self) -> None:
        scrollbar = self.input_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def image_selection(self) -> dict[str, str | None]:
        return {node_id: fields[0].currentData() for node_id, fields in self.image_fields.items()}

    def capture_prompt_image_bindings(self) -> None:
        self.prompt_image_bindings = {}
        self.refresh_reference_labels(self.image_selection())

    def refresh_reference_labels(self, selection: dict[str, str | None]) -> None:
        if self.template.kind != "reference":
            references = [
                image for image in self.template.image_inputs if image.role == "reference"
            ]
            for number, image in enumerate(references, 1):
                self.image_labels[image.node_id].setText(f"图片节点 {number}\n<Picture {number}>")
                self.image_labels[image.node_id].setToolTip(
                    f"提示词中使用 <Picture {number}> 引用这张参考图片。"
                )
            return
        selected_nodes = []
        for slot, (node_id, image_id) in enumerate(selection.items(), 1):
            label = self.image_labels[node_id]
            if image_id is None:
                label.setText(f"图片节点 {slot}")
                label.setToolTip("选择图片后显示提示词代称；编号按实际已选图片顺序排列。")
            else:
                selected_nodes.append(node_id)
                reference = f"<Picture {len(selected_nodes)}>"
                label.setText(f"图片节点 {slot}\n{reference}")
                label.setToolTip(f"提示词中使用 {reference} 引用这张图片。")
        if not selected_nodes:
            self.prompt_image_bindings = {}
        for matched in re.findall(r"<Picture\s+(\d+)>", self.prompt.toPlainText(), re.IGNORECASE):
            number = int(matched)
            if number not in self.prompt_image_bindings and 1 <= number <= len(selected_nodes):
                self.prompt_image_bindings[number] = selected_nodes[number - 1]

    def validate_prompt_image_bindings(self) -> None:
        selected_nodes = []
        for node_id, image_id in self.image_selection().items():
            if image_id is not None:
                selected_nodes.append(node_id)
        for number, node_id in self.prompt_image_bindings.items():
            require(
                number <= len(selected_nodes) and selected_nodes[number - 1] == node_id,
                "INVALID_INPUT",
                f"图片编号已变化，请核对并修改提示词中的 <Picture {number}> 后再生成",
            )

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
                "additional_reference_slots": list(self.additional_reference_slots),
                "prompt_image_bindings": dict(self.prompt_image_bindings),
                "image_scroll": self.input_area.verticalScrollBar().value(),
            }

    def refresh(self) -> None:
        if self.project_dir != self.workbench.output_dir:
            self.project_dir = self.workbench.output_dir
            self.loaded_workflow_id = None
            self.drafts.clear()
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
        self.image_labels = {}
        self.additional_reference_slots = []
        image_inputs = list(self.template.image_inputs)
        if self.template.kind == "reference":
            available = MAX_REFERENCE_IMAGES - len(image_inputs)
            for node_id in draft.get("additional_reference_slots", []):
                if len(self.additional_reference_slots) >= available:
                    break
                if node_id not in self.template.graph:
                    self.additional_reference_slots.append(node_id)
            for node_id in self.additional_reference_slots:
                image_inputs.append(ImageInput(node_id, f"图片节点 {len(image_inputs) + 1}"))
        self.add_image_button.setVisible(self.template.kind == "reference")
        container = QWidget()
        form = QFormLayout(container)
        repository = MediaRepository(self.workbench.output_dir)
        images = repository.images()
        for image_input in image_inputs:
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
            label = QLabel(image_input.label)
            form.addRow(label, row)
            self.image_labels[image_input.node_id] = label
            self.image_fields[image_input.node_id] = (combo, choose, preview)
            choose.clicked.connect(
                lambda checked=False, node_id=image_input.node_id: self.workbench.perform(
                    lambda: self.choose_image(node_id)
                )
            )
            combo.currentIndexChanged.connect(lambda: self.workbench.perform(self.refresh_result))
        self.input_area.setWidget(container)
        QTimer.singleShot(
            0,
            self,
            lambda: self.input_area.verticalScrollBar().setValue(draft.get("image_scroll", 0)),
        )
        self.prompt.blockSignals(True)
        self.prompt.setPlainText(
            draft.get(
                "prompt",
                DEFAULT_VIDEO_PROMPT if self.template.kind == "reference" else self.template.prompt,
            )
        )
        self.prompt.blockSignals(False)
        self.prompt_image_bindings = dict(draft.get("prompt_image_bindings", {}))
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
            control.setMinimumHeight(CONTROL_HEIGHT)
            form.addRow(parameter.label, control)
            self.parameter_fields[parameter.key] = control
        self.refresh_result()

    def refresh_result(self) -> None:
        repository = MediaRepository(self.workbench.output_dir)
        selection = self.image_selection()
        self.refresh_reference_labels(selection)
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
        if self.template.kind == "reference":
            selection = {
                node_id: image_id for node_id, image_id in selection.items() if image_id is not None
            }
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
            if job.get("feedback"):
                text += "；" + job["feedback"]["summary"]
            elif job.get("error"):
                from core.short_video.v2.feedback import concise

                text += "；" + concise(f"{job['error']['type']}: {job['error']['message']}")
            if job["status"] == "unknown":
                text += "；请查询原任务，避免重复提交"
            self.status.setText(text)
        else:
            self.status.setText("")
        path = (
            repository.resolve(job["outputs"][0]["relative_path"])
            if job and job["outputs"]
            else None
        )
        self.video_path = path
        busy = self.workbench.host.runner.busy
        self.generate_button.setEnabled(all(selection.values()) and bool(selection) and not busy)
        self.add_image_button.setEnabled(len(self.image_fields) < MAX_REFERENCE_IMAGES and not busy)
        self.query_button.setEnabled(
            bool(job and job["status"] not in {"succeeded", "cancelled", "failed"}) and not busy
        )
        self.open_button.setEnabled(path is not None)
        self.inputs_changed.emit()

    def generate(self) -> None:
        require(not self.workbench.host.runner.busy, "PROJECT_BUSY", "已有任务在运行")
        if self.template.kind == "reference":
            self.validate_prompt_image_bindings()
        prompt = self.prompt.toPlainText().strip()
        command = MediaService(self.workbench.output_dir).prepare_video(
            self.image_selection(),
            prompt,
            workflow_id=self.loaded_workflow_id,
            parameters=self.parameter_values(),
            target=self.target,
        )
        self.workbench.submit(MediaWork(self.workbench.output_dir, command), self.generate_button)

    def query(self) -> None:
        require(self.current_job is not None, "INVALID_INPUT", "尚无视频任务")
        self.workbench.submit(
            MediaWork(self.workbench.output_dir, job_id=self.current_job["job_id"]),
            self.query_button,
        )

    def open_location(self) -> None:
        if self.video_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.video_path.parent)))
