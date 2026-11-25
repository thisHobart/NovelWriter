# 当前工作流程

更新日期：2026-11-03。界面入口为 `core/gui/app.py` 和 `core/gui/main_window.py`。主窗只放置和切换两个功能，两个功能各自运行后台任务、刷新编辑区，边界见[两个功能的边界](feature_boundaries.md)。

## 小说创作

作品参数 → 世界设定 → 故事结构 → 场景规划 → 章节撰写。详见[小说架构设计](novel/architecture.md)。

## 短视频脚本

原文与赛道 → 小说推文 → 视频推广文案 → 分镜 → 人物／场景／道具及镜头提示词 → 导出。

- 界面位于 `core/gui/pages/short_video/`，业务位于 `core/short_video/v2/`。
- `WorkflowService.execute` 实现 Agent Loop；外部质量检查复用既有规则，只返回是否通过及本轮临时反馈，不自行生成。
- 默认首次生成后最多重试 2 次，每次保留原始要求、必要上下文、最新候选和反馈。达到上限输出最新候选，不回退、不标记成通过。
- 下一步先保存可见修改，再冻结实际来源；来源改变不伪造旧稿依赖。分镜和多对象提示词由现有任务顺序协调，分别记录进度。
- 左侧讨论不进入质量检查，也不直接修改稿件。保存与导出采用当前内容和实际来源。

两部分共用[模型配置](API_CONFIGURATION.md)与任务调度。详见[短视频架构](short_video/architecture.md)、[接口](short_video/interfaces.md)与[项目文件](file_formats.md)。
