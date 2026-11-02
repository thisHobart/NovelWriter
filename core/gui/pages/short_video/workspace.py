"""Session controller and shared page behavior for the isolated script workspace."""
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox, QWidget, QVBoxLayout, QLabel

from core.gui.pages.stage_base import StagePage
from core.short_video.repository import ScriptRepository
from core.short_video.pipeline import ScriptWork


class ScriptWorkspace(QObject):
    changed = Signal()

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.output_dir = "current_video_work"
        self.pages = {}

    @property
    def repo(self):
        return ScriptRepository(self.output_dir)

    def resolve_edits(self):
        for page in self.pages.values():
            if not getattr(page, "dirty", False):
                continue
            answer = QMessageBox.question(self.window, "短视频内容尚未保存", "是否保存当前编辑内容？",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save)
            if answer == QMessageBox.Cancel:
                return False
            if answer == QMessageBox.Save:
                if not page.save():
                    return False
            else:
                page.dirty = False
                if not page.perform(page.refresh):
                    page.dirty = True
                    return False
        return True

    def refresh(self):
        for page in self.pages.values():
            page.refresh()

    def run(self, page, action, button, **kwargs):
        if self.window.runner.busy or not self.resolve_edits():
            return
        def submit():
            work = ScriptWork(self.output_dir, action, self.window.selected_model(), **kwargs)
            page.run_task(work, busy_button=button, busy_text="正在生成短视频脚本…",
                          on_success=lambda _: self.changed.emit())
        page.perform(submit)


class VideoPage(StagePage):
    def __init__(self, workspace, title, subtitle):
        super().__init__(title, subtitle)
        self.workspace = workspace
        self.dirty = False
        self.body = QWidget()
        self.layout = QVBoxLayout(self.body)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.layout.addWidget(self.notice)
        self.set_left(self.body)

    def mark_dirty(self, *args):
        self.dirty = True

    def perform(self, operation):
        try:
            operation()
        except (OSError, ValueError, KeyError, StopIteration) as exc:
            self.status_message.emit("error", str(exc))
            return False
        return True

    def refresh(self):
        pass
