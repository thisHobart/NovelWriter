from PySide6.QtWidgets import QComboBox, QTextBrowser, QHBoxLayout, QFileDialog, QLabel
from core.gui.widgets import secondary_button
from core.gui.pages.short_video.workspace import VideoPage
from core.short_video.models import encode, DIMENSION_LABELS
from core.short_video.validation import to_markdown


class ShortVideoResultsPage(VideoPage):
    def __init__(self, workspace):
        super().__init__(workspace, "成果与评审", "版本、原始返回与运行记录均保留；模型评审与人工定稿独立")
        row = QHBoxLayout()
        self.versions = QComboBox()
        self.select_button = secondary_button("选为当前编辑版本")
        self.export_button = secondary_button("导出 Markdown")
        row.addWidget(self.versions, 1)
        row.addWidget(self.select_button)
        row.addWidget(self.export_button)
        self.layout.addLayout(row)
        self.review = QTextBrowser()
        self.layout.addWidget(self.review, 1)
        self.layout.addWidget(QLabel("运行记录（含未解析候选；不会自动补发请求）"))
        self.runs = QComboBox()
        self.layout.addWidget(self.runs)
        self.raw = QTextBrowser()
        self.layout.addWidget(self.raw, 1)
        self.versions.currentIndexChanged.connect(self.show_version)
        self.runs.currentIndexChanged.connect(self.show_run)
        self.select_button.clicked.connect(self.select)
        self.export_button.clicked.connect(self.export)

    def refresh(self):
        state = self.workspace.repo.load()
        previous = self.versions.currentData() or state["selected_version"]
        self.versions.blockSignals(True)
        self.versions.clear()
        for index, version in enumerate(state["versions"], 1):
            label = f"版本 {index} · {'人工' if version['origin'] == 'manual' else '模型'} · "
            label += {"unchecked": "检查未完成", "needs_changes": "待修改", "reviewed": "已审阅"}[version["status"]]
            if version["finalized"]:
                label += " · 已定稿"
            if version["source"] != self.workspace.repo.source(state):
                label += " · 已过期"
            self.versions.addItem(label, version["id"])
        index = self.versions.findData(previous)
        self.versions.setCurrentIndex(index if index >= 0 else self.versions.count() - 1)
        self.versions.blockSignals(False)
        self.runs.blockSignals(True)
        self.runs.clear()
        for run in reversed(state["runs"]):
            self.runs.addItem(f"{run['action']} · {run['status']} · {len(run['calls'])} 次请求", run["id"])
        self.runs.blockSignals(False)
        self.show_version()
        self.show_run()

    def show_version(self, *args):
        version = self.workspace.repo.version(self.versions.currentData()) if self.versions.currentData() else None
        self.export_button.setEnabled(bool(version))
        self.select_button.setEnabled(bool(version))
        if not version:
            self.review.setPlainText("暂无脚本版本。生成的非空原始返回可在下方运行记录查看。")
            return
        lines = [to_markdown(version["content"]), "\n## 评审"]
        if version["review"]:
            lines.extend(f"- {DIMENSION_LABELS[key]}：{score}/5" for key, score in version["review"]["scores"].items())
        else:
            lines.append("检查未完成；没有可用的模型结论。")
        for issue in version["issues"]:
            lines.append(f"\n- {'硬性问题' if issue['severity'] == 'hard' else '建议'}：{issue['suggestion']}\n  原文：{issue['quote']}")
        self.review.setMarkdown("\n".join(lines))

    def show_run(self, *args):
        identity = self.runs.currentData()
        run = next((r for r in self.workspace.repo.load()["runs"] if r["id"] == identity), None)
        self.raw.setPlainText(encode(run) if run else "暂无运行记录")

    def select(self):
        identity = self.versions.currentData()
        if identity and self.workspace.resolve_edits() and self.perform(lambda: self.workspace.repo.select_version(identity)):
            self.workspace.changed.emit()
            self.navigate_requested.emit("video_script")

    def export(self):
        identity = self.versions.currentData()
        if not identity:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出短视频脚本", "短视频脚本.md", "Markdown (*.md)")
        if path and self.perform(lambda: self.workspace.repo.export(identity, path)):
            self.status_message.emit("done", f"已导出：{path}")
