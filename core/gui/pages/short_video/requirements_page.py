from PySide6.QtWidgets import QFormLayout, QLineEdit, QPlainTextEdit, QSpinBox, QHBoxLayout, QFileDialog, QWidget
from core.gui.widgets import primary_button, secondary_button
from core.gui.widgets.controls import VScrollArea
from core.gui.pages.short_video.workspace import VideoPage
from core.short_video.models import example_requirements, fingerprint


class ShortVideoRequirementsPage(VideoPage):
    def __init__(self, workspace):
        super().__init__(workspace, "创作需求", "保存需求后生成大纲；修改需求会使原大纲确认失效")
        self.directory = QLineEdit(workspace.output_dir)
        self.open_button = secondary_button("打开项目目录")
        self.browse_button = secondary_button("浏览")
        row = QHBoxLayout()
        row.addWidget(self.directory)
        row.addWidget(self.open_button)
        row.addWidget(self.browse_button)
        self.layout.addLayout(row)
        self.open_button.clicked.connect(self._open)
        self.browse_button.clicked.connect(self._browse)
        self.fields = {}
        form = QFormLayout()
        defaults = example_requirements().to_dict()
        labels = {"title": "标题", "idea": "创意", "audience": "目标观众", "genre": "题材",
                  "tone": "基调", "duration_seconds": "目标秒数", "ending": "结尾偏好",
                  "character_limit": "最多人物", "location_limit": "最多地点",
                  "required": "必须出现", "forbidden": "禁止出现"}
        for key, default in defaults.items():
            if isinstance(default, int):
                widget = QSpinBox()
                widget.setMinimumHeight(30)
                widget.setRange(*( (30, 90) if key == "duration_seconds" else (1, 8 if key == "character_limit" else 6)))
                widget.setValue(default)
                widget.valueChanged.connect(self.mark_dirty)
            elif key in {"idea", "required", "forbidden"}:
                widget = QPlainTextEdit(default)
                widget.setMaximumHeight(80)
                widget.textChanged.connect(self.mark_dirty)
            else:
                widget = QLineEdit(default)
                widget.textChanged.connect(self.mark_dirty)
            self.fields[key] = widget
            form.addRow(labels[key], widget)
        form_widget = QWidget()
        form_widget.setLayout(form)
        scroll = VScrollArea()
        scroll.setWidget(form_widget)
        self.layout.addWidget(scroll)
        self.save_button = primary_button("保存创作需求")
        self.header.add_action(self.save_button)
        self.save_button.clicked.connect(self.save)
        self.directory.editingFinished.connect(self._open)
        self._loaded_hash = fingerprint(None)

    def values(self):
        return {key: (widget.value() if isinstance(widget, QSpinBox) else
                      widget.toPlainText() if isinstance(widget, QPlainTextEdit) else widget.text())
                for key, widget in self.fields.items()}

    def save(self):
        def operation():
            self.workspace.repo.save_requirements(self.values(), expected=self._loaded_hash)
            self.dirty = False
            self.workspace.changed.emit()
            self.status_message.emit("done", "需求已保存，请生成并确认大纲")
        return self.perform(operation)

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "选择短视频项目根目录", self.workspace.output_dir)
        if path:
            self.directory.setText(path)
            self._open()

    def _open(self):
        target = self.directory.text().strip()
        if target == self.workspace.output_dir:
            return
        if self.workspace.window.runner.busy or not self.workspace.resolve_edits():
            self.directory.setText(self.workspace.output_dir)
            return
        if not target:
            self.directory.setText(self.workspace.output_dir)
            return
        from core.short_video.repository import ScriptRepository
        def operation():
            repository = ScriptRepository(target)
            repository.load()  # Validate before switching away from a good project.
            repository.recover()
            self.workspace.output_dir = target
            self.status_message.emit("info", f"输出目录 {target}")
            self.workspace.changed.emit()
        if not self.perform(operation):
            self.directory.setText(self.workspace.output_dir)

    def refresh(self):
        if self.dirty:
            return
        saved = self.workspace.repo.load()["requirements"]
        self._loaded_hash = fingerprint(saved)
        values = saved if saved is not None else example_requirements().to_dict()
        self.notice.setText("已载入保存的创作需求。" if saved is not None else
                            "已填入 60 秒验证示例，可直接保存需求后生成大纲；所有内容均可修改。")
        self.directory.setText(self.workspace.output_dir)
        for key, widget in self.fields.items():
            blocked = widget.blockSignals(True)
            if isinstance(widget, QSpinBox):
                widget.setValue(values[key])
            elif isinstance(widget, QPlainTextEdit):
                widget.setPlainText(values[key])
            else:
                widget.setText(values[key])
            widget.blockSignals(blocked)
