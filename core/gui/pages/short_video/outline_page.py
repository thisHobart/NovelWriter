from core.gui.widgets import primary_button, secondary_button
from core.gui.pages.short_video.workspace import VideoPage
from core.gui.pages.short_video.editor import StoryEditor
from core.short_video.models import fingerprint


class ShortVideoOutlinePage(VideoPage):
    def __init__(self, workspace):
        super().__init__(workspace, "故事大纲", "先编辑并确认人物、冲突、节拍与结尾，再生成完整脚本")
        self.generate_button = secondary_button("生成大纲")
        self.save_button = secondary_button("保存大纲")
        self.confirm_button = primary_button("确认大纲")
        for button in (self.generate_button, self.save_button, self.confirm_button):
            self.header.add_action(button)
        self.editor = StoryEditor("outline")
        self.layout.addWidget(self.editor)
        self.editor.changed.connect(self.mark_dirty)
        self.generate_button.clicked.connect(lambda: workspace.run(self, "outline", self.generate_button))
        self.save_button.clicked.connect(self.save)
        self.confirm_button.clicked.connect(self.confirm)
        self._loaded_hash = fingerprint(None)
        self._requirements_hash = fingerprint(None)

    def save(self):
        def operation():
            self.workspace.repo.save_outline(self.editor.value(), expected=self._loaded_hash,
                                              source=self._requirements_hash)
            self.dirty = False
            self.workspace.changed.emit()
        return self.perform(operation)

    def confirm(self):
        if self.dirty and not self.save():
            return
        if self.perform(lambda: self.workspace.repo.confirm_outline(expected=self._loaded_hash)):
            self.workspace.changed.emit()
            self.status_message.emit("done", "大纲已确认，可以写作")

    def refresh(self):
        if self.dirty:
            return
        state = self.workspace.repo.load()
        outline = state["outline"]
        self._loaded_hash = fingerprint(outline)
        self._requirements_hash = fingerprint(state["requirements"])
        self.editor.set_content(outline["content"] if outline else None)
        ready = self.workspace.repo.outline_ready(state)
        self.notice.setText("大纲已确认" if ready else "大纲未确认或需求已变化，请检查并确认。")
        self.generate_button.setEnabled(bool(state["requirements"]))
        self.confirm_button.setEnabled(bool(outline))
        self.save_button.setEnabled(bool(outline))
