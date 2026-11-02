from PySide6.QtWidgets import QLabel
from core.gui.widgets import primary_button, secondary_button
from core.gui.pages.short_video.workspace import VideoPage


class ShortVideoWorkflowPage(VideoPage):
    def __init__(self, workspace):
        super().__init__(workspace, "短视频脚本", "中文独立剧情 · 30–90 秒 · 面向 AI 视频制作")
        self.back_button = secondary_button("返回小说创作")
        self.header.add_action(self.back_button)
        self.back_button.clicked.connect(lambda: workspace.window.switch_mode("novel"))
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.layout.addWidget(self.summary)
        for key, label in (("requirements", "1. 填写创作需求"), ("outline", "2. 生成、编辑并确认大纲"),
                           ("script", "3. 完整脚本与修订"), ("results", "4. 查看版本、评审与导出")):
            button = primary_button(label)
            button.clicked.connect(lambda _, k=key: self.navigate_requested.emit("video_" + k))
            self.layout.addWidget(button)
        self.layout.addWidget(QLabel("先确认故事方向，再完整写稿。预计时长仅供参考；模型审阅与人工定稿分别记录。"))
        self.layout.addStretch()

    def refresh(self):
        state = self.workspace.repo.load()
        ready = self.workspace.repo.outline_ready(state)
        self.summary.setText(f"项目：{self.workspace.output_dir}\n需求：{'已保存' if state['requirements'] else '未填写'}\n"
                             f"大纲：{'已确认' if ready else '待生成或确认'}\n脚本：{len(state['versions'])} 个版本")
