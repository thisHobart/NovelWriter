# 网文视频推广工作台：开发文档

文档版本：2.4；日期：2026-11-03；状态：按当前源码更新维护说明；本次仅修改文档，历史测试结果见末节。

依据：[架构设计](architecture.md)、[接口设计](interfaces.md)、[交互设计](interaction.md)。本文只保留当前实现、维护约定和验证方法。开发过程和历史验收集中记录在根目录 [DEVLOG.md](../../DEVLOG.md)。

## 当前实现与入口

- `workflow.py` 的 `WorkflowService.execute` 负责准备输入、生成、接收反馈和控制重试。`DEFAULT_MAX_RETRIES=2`；prepare 冻结该值，0 表示首次生成并检查后结束。通过提前停止；持续不通过输出最新候选，保存 review_status=issues 状态而非通过状态，不按问题数选择较早内容。
- `review.py` 的 `check_candidate` 接收阶段要求、必要上下文与当前候选，复用现有规则和检查响应校验，返回 `QualityCheckResult(passed,feedback)`。它不触发生成，不保存报告。重试输入保留原始任务、上下文并加入最新候选与反馈。
- 反馈、问题及检查原文只在本轮内存中使用；生成链路不调用 save_report 或持久化 issues。阶段 calls 仅记调用类型、错误类型和时间，预算计数回调不传原文。初始命令仍保存任务上下文；聊天和分段调用有独立的原文记录，不能套用阶段 calls 的限制。
- `creation.py` 接通目录／分镜与人物、场景、道具提示词；`batches.py` 的最终合稿复用相同 Agent Loop。镜头提示词重新生成仍使用该流程。普通讨论不进入质量检查，不新增图片／视频能力或检查 UI。
- 默认单阶段最多 6 次请求／300 秒，分镜任务 12 次／600 秒，资产任务 6 × 待处理项次；多对象时限不变。格式修复和失败消耗同一总预算。预算不足与接口／检查异常不作为质量反馈；已有候选保持可编辑及明确未完成状态，取消和内部错误沿用既有处理。
- 编辑区未新增控件，只显示简短的未通过／未完成状态，最新候选可直接手改并保存。

合法候选在检查前落盘并添加到运行 output_refs，暂不切换 current。收尾时核对 selection_snapshot 和 selected_before；其他操作修改当前内容时返回 selected=false，仅保留历史候选。服务／预算／可处理的输出异常在已有候选时分别返回 unavailable 或 incomplete；取消和其他异常停止并保留已落盘内容。运行 succeeded 不等于 review_status=passed。

| 场景 | 入口与职责 |
|---|---|
| 原文与创作要求 | SourcePage → ProjectService.save_inputs → Repository |
| 小说推文、视频文案 | Workbench.start_creation → WorkflowService.prepare → WorkspaceWork → execute |
| 分镜 | prepare_storyboard_task → CreationWork：目录循环后执行分镜循环 |
| 资产提示词 | prepare_prompt_task → CreationWork：按实体状态生成，compile_asset 产生视图，compile_storyboard_prompts 产生镜头文字 |
| 当前镜头优化 | PromptPage.regenerate_shot_prompts → shot_prompts 单阶段循环 |
| 超长原文 | BatchWork／ChunkService：逐段事实、逐段稿、最终合稿循环；复用成功分段 |
| 普通聊天 | ChatPanel → ConversationService.prepare → ChatWork → execute；Store 存取、context 纯函数构造 |
| 人工保存和导出 | Repository.save／ExportService，本地校验与编译，不调用模型 |

预算的具体默认值及正常／异常流程统一见[架构设计](architecture.md)，调用签名见[接口设计](interfaces.md)，会话存储和容量细节见[会话管理](conversation_management.md)。结构格式修复与质量修改是两个回路，共用次数和时限；不得通过 SDK 自动重试绕过预算。多阶段任务每项保存完成进度；普通组合任务停止后不会沿用同一任务编号重发，应从页面重新生成未完成项，长文计划则支持显式续跑。

## 当前代码维护：兼容写法与编码风格

1. 使用满足当前需求的简单实现；在界面、领域、网络、存储和状态生命周期边界抽象，不为假设需求建立框架或隐式注册机制。
2. 变量表达具体业务对象；函数使用动词与业务对象；类表达明确职责；文件按业务功能命名。子任务有独立业务含义、需要复用或职责混杂时才拆分，避免机械拆分和多层转发。
3. 函数按必要校验、业务处理、返回结果组织。外部数据在进入业务前验证，内部调用遵循类型契约，不重复防御性兜底。
4. 参数和返回值写明类型；共享复杂结构使用明确契约。阶段不同的 JSON 内容通过 JsonObject 表达，并由对应阶段校验器检查，不能用该类型逃避必要的边界校验。
5. 明确单位与缺失语义：时间轴和时长为毫秒整数，原文坐标为 Unicode 码点，未指定目标使用 None，不用空字典或零值掩盖失败。
6. 在能够恢复或反馈的位置捕获具体异常，保留原因及任务上下文；必要配置首次使用时明确报错。编程错误不包装成网络异常、不自动重试。仅对明确适合修复的模型格式错误使用现有有限预算。
7. 保持入口、实际实现和中间结果可查；记录必要失败上下文，避免重复日志。网络替换通过显式 transport，文件与模型边界可独立验证。

仓库 ruff.toml 只覆盖当前短视频领域、界面和文字评估入口，检查名称／导入及函数类型标注，统一100字符排版。Ruff仅作为开发检查工具，不加入运行依赖；可运行 ruff check 和 ruff format --check。

## 模型配置、对话与编辑

短视频顶部和项目设置均复用第一部分 ModelConfigDialog；两部分共享服务商、地址、密钥与模型，运行中禁用配置切换。普通聊天只提供建议，不恢复作用目标、应用修改、聊天修订或另存候选。稿件与提示词直接编辑保存；历史版本按需恢复。

聊天入口统一使用 ConversationStore 和 build_conversation_context。ConversationService 中重复的文件读写与上下文拼接已删除；界面和评估入口直接访问 service.store。长历史按完整轮次裁剪副本，本轮消息只加入一次。预检失败不落盘，执行时保存用户消息与最终回复，停止和恢复沿用明确终态。详见[会话管理](conversation_management.md)。

## 验证

```powershell
python -m pytest tests -q --tb=short
ruff check core/short_video/v2 core/gui/pages/short_video
ruff format --check core/short_video/v2 core/gui/pages/short_video
```

必要回归覆盖 Agent Loop 精确重试次数、反馈只在内存、异常／取消不触发质量重试、各生成入口、手改内容进入下一阶段、纯聊天不读取文档或保存右侧编辑、并发保存保护、导出及模型配置。使用临时项目与离线替身，不把测试结果当作真实文案质量评估。

定向验证入口：

```powershell
python -m pytest tests/test_video_v2_creation_loop.py tests/test_conversation_management.py tests/test_video_v2_chat.py tests/test_short_video_ui.py -q --tb=short
```

### 历史验证记录

以下为此前实现阶段记录，不是本次 2.4 文档核对重新运行的结果，也不据此推断当前用例总数：

- 此前清理阶段：1175 passed、5 subtests passed（162.07 秒）；同时记录 Ruff、格式、编译和文档链接等检查通过。
- 后续会话清理阶段：1179 passed、5 subtests passed（147.68 秒）；全量后又清理评估参数并增加 CLI 用例，聊天定向回归 8 项通过（1.21 秒）。完整说明和既有证据路径见[会话管理验证记录](conversation_management.md#历史验证记录)。

本次只修改 docs/short_video 内文档，核对源码接口、预算、状态和界面，不重新运行应用或发起模型请求。检查本目录 Markdown 相对文件链接、代码围栏、图表定义及 git diff --check；不产生新的功能测试通过数。历史实现证据仍保留，开发过程以 [DEVLOG.md](../../DEVLOG.md) 为准。
