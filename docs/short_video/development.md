# 短视频工作台：开发文档

文档版本：2.18；日期：2026-11-15。本文用于定位代码、维护实现与执行验证。
整体结构见[架构设计](architecture.md)，字段和接口见[接口设计](interfaces.md)，操作与提示见[交互设计](interaction.md)。历次变更及验收证据见[DEVLOG](../../DEVLOG.md)。

## 1. 运行与代码入口

使用 Python 3.11，依赖安装在项目虚拟环境中。启动桌面应用：

```powershell
.\venv\Scripts\python.exe main.py
```

| 维护内容 | 主要代码入口 |
|---|---|
| 工作区、导航与生成入口 | `core/gui/pages/short_video/workbench.py`；`artifact_page.py`、`prompt_page.py`、`source_page.py` |
| 后台线程与 UI 状态 | `core/gui/task_runner.py`；`core/short_video/v2/feedback.py` |
| 准备阶段输入、多阶段编排 | `core/short_video/v2/workflow.py`、`creation.py` |
| Agent Loop、检查点与继续执行 | `core/short_video/v2/runtime.py` |
| 项目与内容存储 | `repository.py`、`storage.py`、`project_service.py`、`models.py`、`contracts.py` |
| 生成／校验配置与模型调用 | `core/generation/api_settings.py`、`core/gui/model_config_dialog.py`；`llm.py` |
| 提示词、校验与审阅 | `prompts.py`、`validation.py`、`review.py`、`storyboard_skill.py` |
| 会话与上下文 | `conversation.py`、`conversation_store.py`、`conversation_context.py` |
| 长文分块、编排及导出 | `batches.py`、`prompt_compiler.py`、`storyboard_text.py`、`exporter.py` |
| 视频工作流与媒体任务 | `media_workflows.py`、`media_service.py`、`media_repository.py`、`comfy_mcp.py`；UI 为 `media_preview.py`、`comfy_settings.py` |

表中省略目录的领域文件均位于 `core/short_video/v2/`，UI 文件位于 `core/gui/pages/short_video/`。

## 2. 文字生成的维护要点

1. `WorkflowService.prepare` 读取当前有效内容，冻结生成与校验模型、来源、模板、要求和目标内容；分镜组合任务在目录生成前冻结后续分镜规则。
2. `CreationWork` 编排目录→分镜，或逐项资产→镜头本地编排；单阶段任务通过 `WorkspaceWork` 执行。
3. `AgentExecution` 执行 request→validate→review→decide→commit；`validation` 保证结构和引用，`review` 返回质量反馈，二者不自行启动新任务。
4. 质量修订默认最多一次：首次校验失败后由生成模型修订，再用独立校验模型复检，第二次残留质量问题只记录、不触发第三稿。每轮候选的格式修复最多两次，独立计数；分块请求允许一次格式修复。ScriptLLM 另负责最多两次网络重试，SDK 内部重试保持0，避免嵌套重试；不设置整轮调用预算或总时限。

修改内容规则时，阶段任务与样例在 `prompts.py`，硬性约束在 `validation.py`，AI 审阅标准在 `review.py`；保持三者一致。自定义阶段模板优先，不以更新内置提示词为由覆盖用户模板。

分镜 Skill 资源为 [short-video-content-storyboard/SKILL.md](../../core/short_video/v2/skills/short-video-content-storyboard/SKILL.md)。仅显式选择时注入分镜，其他阶段及聊天不注入。当前 Skill 以短片总时长、常规5–8秒镜头、动作复杂度与衔接为主要约束；具体规则以文件为准。

## 3. 检查点、依赖与提交

- 运行数据统一存入 `project_v2.json` 的 tasks／runs；`TaskContext` 仅用于界面反馈。不要将内存进度当作恢复依据，也不要为新任务重新写 tasks/*.json。
- ScriptLLM 的 before_request 在每次实际发送前校验任务并记录独立调用及实际模型用途／快照，防止重试期间输入或校验配置变更；完整回复先落盘；本地解析、审阅与修订分别推进步骤。每个未完成运行仅保留最新候选及当前待处理回复。
- 最终完整校验结果保存在 `run.review_result`，含完整 issues／反馈及最终 candidate_id；不要只存 UI 截断摘要。校验配置显式独立保存，缺失即预检失败，不默认使用生成模型。
- 当前内容指针、run 结果与父任务完成项在同一事务提交；镜头本地编排也须在事务内校验任务，并将产物加入任务输出。
- 网络错误先有限退避重试；停止或重试耗尽后保留可恢复状态；修订次数跨恢复保持。质量修订耗尽仍提交合法候选并标为 issues，格式修复耗尽则失败。
- 恢复先核对输入与人工目标。外部修改使原任务失效；任务自身更新产生的指针变化须同步到预期引用。清理依赖后再次校验任务，避免旧原文残留检查点。
- 普通读取保持只读；迁移和中断识别放在持有生成锁的 recover 中。旧任务没有步骤数据时说明无法继续，不推测执行位置。
- 长文通过 `merge_task_id` 关联合稿任务，继续时跳过成功分块并恢复合稿；已耗尽修复次数的合稿不能靠继续重置上限。

业务内容保持当前一份。内容 ID、哈希和 revision 用于依赖、完整性与并发控制；不要重新加入历史版本菜单或旧稿沿用路径。

## 4. 会话与媒体边界

会话通过 ConversationStore 读写，默认按48,000字符容量构造请求，仅裁剪本轮使用的完整历史副本。预检失败不写 pending；执行时保存本轮消息和完整回复。重试仅填回输入框，用户再次发送；聊天不写回业务文档、不进入质量修订循环。

视频任务使用独立媒体快照和远程任务编号。修改模板适配时只改本轮 JSON 副本，保留用户保存的工作流、模型和默认参数。普通 H3 参考图支持最多九个位置，空位跳过，图片编号与 Picture 引用需一致；首尾帧模板按实际输入要求校验。当前只接入图片导入，不提前实现生图。

提交响应丢失先查询原任务，下载失败只重新取回文件；不自动重提视频任务，也不对共享 ComfyUI 发送全局中断。文字网络错误在共享调用层有限重试，耗尽后原样传播；媒体仍使用其独立提交与查询策略。任务边界记录堆栈，UI 显示操作、对象、阶段和实际原因。

## 5. 开发与验证

使用显式 transport 替身和临时项目验证文字流程，避免测试调用付费模型或修改用户数据。

```powershell
# 全量回归
.\venv\Scripts\python.exe -m pytest tests -q --tb=short

# 文字存储、恢复、会话与反馈
.\venv\Scripts\python.exe -m pytest tests/test_video_v2_repository.py tests/test_video_runtime.py tests/test_conversation_management.py tests/test_video_feedback.py tests/test_video_network_retry.py tests/test_video_review_model.py -q

# 按仓库 Ruff 配置检查生产代码
.\venv\Scripts\python.exe -m ruff check core/short_video/v2 core/gui/pages/short_video
.\venv\Scripts\python.exe -m ruff format --check core/short_video/v2 core/gui/pages/short_video
```

核心回归应覆盖：

- 不同生成／校验接口、密钥及模型的真实 SDK 路由；首检通过、修订后通过和修订后仍有问题；完整问题持久化、配置变更阻断恢复。
- 回复、候选、审阅和提交边界退出后的恢复；已完成资产及已保存回复不重复调用。
- 暂时网络故障后成功、重试耗尽、Retry-After、等待中停止及不嵌套 SDK 重试；认证／权限／存储错误不自动重试。
- 限流、结果不明、磁盘失败、损坏检查点、并发恢复和修复次数保持。
- 原文、Skill、模板、上游及人工目标变化后的任务失效和下游清理。
- UI 继续入口与真实错误提示；分镜时间轴、资产引用、编辑与 Markdown 导出。
- 媒体输入和工作流隔离、远程任务对账、下载恢复；真实联调使用隔离项目并单独记录参数与产物。

各次回归与校验模型接入的验收结果见 DEVLOG。离线测试不代表成片质量。

## 6. 打包与变更约定

Windows 单文件构建入口：

```powershell
.\venv\Scripts\python.exe -m PyInstaller NovelWriter.spec
```

`NovelWriter.spec` 包含分镜 Skill、两份 H3 工作流 JSON 和 MCP 依赖。新增运行资源时同步检查打包清单；源码运行成功不能代替安装包资源验证。

维护时保持以下约定：

- UI、任务编排、领域规则、模型适配与存储分别承担职责；不为未确定需求添加交互入口。
- 时间使用整数毫秒，原文偏移使用 Unicode 码点；缺失目标使用 None。
- 函数边界写明类型，阶段 JSON 在对应校验器检查；异常只在有恢复或反馈职责的边界捕获。
- 提交前运行与改动相关的验证，并更新对应文档；历史过程和验收写入 DEVLOG，四份维护文档只描述当前实现。
