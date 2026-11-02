from PySide6.QtWidgets import QPlainTextEdit, QHBoxLayout
from core.gui.widgets import primary_button, secondary_button
from core.gui.pages.short_video.workspace import VideoPage
from core.gui.pages.short_video.editor import StoryEditor


class ShortVideoScriptPage(VideoPage):
    def __init__(self, workspace):
        super().__init__(workspace, "完整剧情脚本", "整篇生成与有限修订；每次保存人工稿均创建新版本")
        self.generate_button = primary_button("生成完整脚本")
        self.save_button = secondary_button("保存为人工新版本")
        self.finalize_button = secondary_button("人工定稿")
        for button in (self.generate_button, self.save_button, self.finalize_button):
            self.header.add_action(button)
        self.editor = StoryEditor("script")
        self.layout.addWidget(self.editor, 1)
        self.editor.changed.connect(self.mark_dirty)
        self.asks = QPlainTextEdit()
        self.asks.setPlaceholderText("每行填写一条具体修改意见；修订会保留旧稿。")
        self.asks.setMaximumHeight(75)
        self.layout.addWidget(self.asks)
        self.revise_button = secondary_button("按意见再次修订")
        self.layout.addWidget(self.revise_button)
        self.generate_button.clicked.connect(lambda: workspace.run(self, "script", self.generate_button))
        self.save_button.clicked.connect(self.save)
        self.finalize_button.clicked.connect(self.finalize)
        self.revise_button.clicked.connect(self.revise)
        self.version_id, self.source = None, None

    def save(self):
        def operation():
            version = self.workspace.repo.save_manual(self.editor.value(), self.source)
            self.version_id = version["id"]
            self.dirty = False
            self.workspace.changed.emit()
        return self.perform(operation)

    def finalize(self):
        if self.dirty and not self.save():
            return
        if not self.version_id:
            return
        if self.perform(lambda: self.workspace.repo.finalize(self.version_id)):
            self.workspace.changed.emit()
            self.status_message.emit("done", "已人工定稿；模型评审状态保持原记录")

    def revise(self):
        asks = [line.strip() for line in self.asks.toPlainText().splitlines() if line.strip()]
        if not asks:
            self.status_message.emit("warn", "请填写具体修改意见")
            return
        if not self.workspace.resolve_edits():
            return
        self.workspace.run(self, "revise", self.revise_button, version_id=self.version_id, asks=asks)

    def refresh(self):
        if self.dirty:
            return
        repo = self.workspace.repo
        state = repo.load()
        version = repo.version()
        ready = repo.outline_ready(state)
        self.version_id = version["id"] if version else None
        self.source = version["source"] if version else repo.source(state)
        self.editor.set_content(version["content"] if version else None)
        stale = bool(version and version["source"] != repo.source(state))
        labels = {"unchecked": "检查未完成", "needs_changes": "待修改", "reviewed": "已审阅"}
        self.notice.setText(("创作依据已过期，请重新确认大纲并生成。" if stale else
                            labels.get(version["status"], "") + (" · 已人工定稿" if version["finalized"] else " · 候选稿"))
                           if version else "先确认大纲，再生成完整脚本。")
        self.generate_button.setEnabled(ready)
        for button in (self.save_button, self.finalize_button, self.revise_button):
            button.setEnabled(bool(version and ready and not stale))
