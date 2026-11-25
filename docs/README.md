# NovelWriter 文档

更新日期：2026-11-25。此索引仅列当前保留的功能说明；开发历史集中在根目录开发日志。

## 使用与配置

- [用户指南](user_guide.md)：启动、模型配置、小说创作与短视频脚本入口。
- [工作流程](workflow.md)：两部分的输入、生成、编辑与保存关系。
- [模型服务配置](API_CONFIGURATION.md)：服务商、接口地址、密钥与模型配置。
- [项目文件](file_formats.md)：当前作品目录与保存格式。
- [两个功能的边界](feature_boundaries.md)：小说与短视频各自的代码位置、共用层、主窗约定、共用界面规则、模型选择和测试目录。

## 小说创作

- [架构设计](novel/architecture.md)：分层、数据流和关键决定。
- [接口设计](novel/interfaces.md)：阶段调用、作品参数、作品文件。
- [交互设计](novel/interaction.md)：各页面的按钮与状态。
- [开发文档](novel/development.md)：代码入口、测试和质量评估命令。

## 短视频脚本

[工作台说明](short_video/README.md) 包含 [架构](short_video/architecture.md)、[接口](short_video/interfaces.md)、[交互](short_video/interaction.md)、[开发文档](short_video/development.md) 和 [编码约束](short_video/coding_prompt.md)。

[会话存储与上下文](short_video/conversation_management.md)说明独立模块边界与接入状态。

当前流程为原文 → 小说推文 → 视频推广文案 → 分镜 → 资产与镜头提示词。Agent Loop 在后台检查和有限修改，左侧对话只提供建议。

## 维护记录

- [开发日志](../DEVLOG.md)：已完成变更与历史验证记录。
- [后续方向](../ROADMAP.md)：待明确需求的候选工作。
