# 项目文件与保存格式

更新日期：2026-11-03。以下路径相对于用户选择的作品输出目录；文件名和结构以当前业务代码为准。

## 小说作品

| 路径 | 用途 |
|---|---|
| `system/parameters.txt` | 作品参数，按键值行保存，由参数页面读写 |
| `story/lore/characters.json`、`factions.json` | 当前人物与势力资料 |
| `story/lore/generated_lore.md`、`lore_contract.json` | 世界观正文及实体契约 |
| `system/lore_edits.json` | 世界设定编辑与恢复元数据 |
| `story/structure/` | 人物／势力故事弧、分段结构与结构契约 |
| `story/planning/chapter_outlines/` | 章节大纲 |
| `story/planning/detailed_scene_plans/` | 逐章场景规划 |
| `story/content/chapters/` | 章节正文 Markdown |

结构和规划文件随所选篇幅与故事结构命名，不依赖旧 Tk 模块或旧随机生成器。读取与展示入口可查 `core/gui/pages/novel/services/artifacts.py`；生成入口见[工作流程](workflow.md)。小说章节的账本、质量状态及恢复记录继续由现有生成模块维护。

## 短视频项目

| 路径 | 用途 |
|---|---|
| `short_video/project_v2.json` | 项目清单、项目名称、当前版本引用与任务状态 |
| `short_video/artifacts/<版本ID>.json` | 不可变文章、选区、配置、稿件与提示词版本 |
| `short_video/conversations/` | 项目内聊天消息、状态与模型信息 |
| `short_video/tasks/` | 多阶段任务的既有进度和恢复信息 |

内部引用与指纹由软件维护，用户在编辑区修改正文。时间轴为毫秒整数；文章坐标使用 Unicode 码点 `[start,end)`。Agent Loop 的检查反馈不写入聊天、报告或调用原文，只在本轮内存中用于修改。

文案、分镜和提示词统一导出为 Markdown（.md）。导出清单记录实际版本和来源，文件不包含真实图片或视频。完整内容结构见[短视频接口设计](short_video/interfaces.md)。

## 模型配置与维护

模型配置存放在应用使用的 `llm_settings.json`，与作品内容分开；参见[模型服务配置](API_CONFIGURATION.md)。请勿将密钥提交到版本库。

保存、来源校验和原子写入由业务代码完成。手动修改内部清单可能破坏引用关系；正文修改使用页面编辑和保存。过时格式不会因打开项目而自动补齐或改写。
