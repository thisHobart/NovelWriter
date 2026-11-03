# NovelWriter 文档

更新日期：2026-11-03。此索引仅列当前保留的功能说明；开发历史集中在根目录开发日志。

## 使用与配置

- [用户指南](user_guide.md)：启动、模型配置、小说创作与短视频脚本入口。
- [工作流程](workflow.md)：两部分的输入、生成、编辑与保存关系。
- [模型服务配置](API_CONFIGURATION.md)：服务商、接口地址、密钥与模型配置。
- [项目文件](file_formats.md)：当前作品目录与保存格式。

## 小说创作

- [界面交互契约](UI_CONTRACT.md)：小说阶段页面、状态与操作约定。
- [世界设定编辑](lore_editing.md)：人工编辑、保存与恢复。
- [生成参数一致性](parameter_consistency.md)：输入快照、参数冲突和后续生成。
- [世界设定循环与验收](lore_loop_acceptance.md)：现有世界设定生成规则与验证方法。

## 短视频脚本

[工作台说明](short_video/README.md) 包含 [架构](short_video/architecture.md)、[接口](short_video/interfaces.md)、[交互](short_video/interaction.md)、[开发文档](short_video/development.md) 和 [编码约束](short_video/coding_prompt.md)。

[会话存储与上下文](short_video/conversation_management.md)说明独立模块边界与接入状态。

当前流程为原文 → 小说推文 → 视频推广文案 → 分镜 → 资产与镜头提示词。Agent Loop 在后台检查和有限修改，左侧对话只提供建议。

## 维护记录

- [开发日志](../DEVLOG.md)：已完成变更与历史验证记录。
- [后续方向](../ROADMAP.md)：待明确需求的候选工作。
