# 网文视频推广工作台：开发文档

文档版本：2.5；日期：2026-11-04；状态：同步分镜整段文本复制与导出，保留资产多视角提示词实现说明。

依据：[架构设计](architecture.md)、[接口设计](interfaces.md)、[交互设计](interaction.md)。本文只保留当前实现、维护约定和验证方法。开发过程和历史验收集中记录在根目录 [DEVLOG.md](../../DEVLOG.md)。

## 提示词只保留当前内容（2026-11-04）

人物／场景／道具按对象与状态、镜头提示词按整组镜头，各保留当前一份。保存与成功生成替换旧内容，删除提示词历史入口、恢复及旧来源确认。Repository 在事务提交后清理旧文件及中间候选，资产内容被替换后重新编排镜头提示词；运行中的候选临时保留，失败或冲突不覆盖当前编辑。恢复步骤清理已有历史，普通读取仍只读。

新增 8 项回归覆盖替换、重试、重放、恢复、磁盘失败、配置变化及并发编辑，实际界面验证保存和入口移除；当前全量 **145 passed（10.59 秒）**。未调用真实模型。现有项目清理 9 份历史记录，24 份当前提示词正文保持不变。

## 分镜文本交付（2026-11-04）

- 新增 storyboard_text.py，将镜头编排为“视觉风格、场景概述、分镜、镜头要求、音频、限制”六部分正文；使用当前时长累计时间，保留人物与状态、道具、画面、动作、对白、画外音及旁白。
- ArtifactPage 增加“复制分镜文本”，使用编辑器当前有效内容及该稿精确依赖的目录；包含未保存修改，不写文件、不调用模型。
- 文件导出仅保留 Markdown，分镜使用段落结构，来源清单照常写入；分镜文本可通过复制按钮直接粘贴使用。
- 不新增存储字段或 AI 摘要调用，不把参考示例中的重叠时间段或动作片设定套到其他故事。

分镜文本功能的离线集成验证覆盖毫秒转秒、连续时间轴、源内容不变、旁白／画外对白、空镜头／无指定音频、真实 Qt 按钮与剪贴板、未保存编辑复制不落盘、旧稿使用精确历史目录。文件导出随后收敛为仅 Markdown，已删除其他格式实现和表格依赖。使用临时项目，无真实模型调用；现有全量测试 **137 passed（7.83 秒）**。当前环境未安装 Ruff，本次未执行该项检查。

## 当前实现与入口

- `workflow.py` 的 `WorkflowService.execute` 负责准备输入、生成、接收反馈和控制重试。`DEFAULT_MAX_RETRIES=2`；prepare 冻结该值，0 表示首次生成并检查后结束。通过提前停止；持续不通过输出最新候选，保存 review_status=issues 状态而非通过状态，不按问题数选择较早内容。
- `review.py` 的 `check_candidate` 接收阶段要求、必要上下文与当前候选，复用现有规则和检查响应校验，返回 `QualityCheckResult(passed,feedback)`。它不触发生成，不保存报告。重试输入保留原始任务、上下文并加入最新候选与反馈。
- `prompts.py` 的 asset_spec 一次要求模型生成基础设定和完整 views；人物、场景、道具有各自的视角与可见性要求。`validation.validate_generated_asset_prompts` 校验新模型输出的具体字段及计划视图；`prompt_compiler.finalize_asset_prompts` 仅规范顺序和内部指纹，保留模型文字。view_prompts 继续只修改 views，显式 compile_asset 本地入口保留；手动保存和历史读取沿用原契约。
- 资产审阅对照基础设定检查整组视图，分类型检查人物朝向、场景布局和道具可见表面，反馈包含具体路径、矛盾及建议。只检查文字，不声称验证了图片；反馈修订继续使用原有预算与重试流程。
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
| 资产提示词 | prepare_prompt_task → CreationWork：按实体状态由模型生成设定和全部视图，finalize_asset_prompts 保留文字并绑定指纹，compile_storyboard_prompts 产生镜头文字 |
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

短视频顶部和项目设置均复用第一部分 ModelConfigDialog；两部分共享服务商、地址、密钥与模型，运行中禁用配置切换。普通聊天只提供建议，不恢复作用目标、应用修改、聊天修订或另存候选。稿件与提示词直接编辑保存；仅文案与分镜支持历史恢复。人物／场景／道具及镜头提示词按对象与状态保留当前一份，替换时清理旧记录和中间候选，取消提示词旧来源提示及确认。打开项目的恢复步骤清理已有非当前提示词，普通读取保持只读。

聊天入口统一使用 ConversationStore 和 build_conversation_context。ConversationService 中重复的文件读写与上下文拼接已删除；界面和评估入口直接访问 service.store。长历史按完整轮次裁剪副本，本轮消息只加入一次。预检失败不落盘，执行时保存用户消息与最终回复，停止和恢复沿用明确终态。详见[会话管理](conversation_management.md)。

## 验证

```powershell
python -m pytest tests -q --tb=short
ruff check core/short_video/v2 core/gui/pages/short_video
ruff format --check core/short_video/v2 core/gui/pages/short_video
```

自动化测试已按精简要求缩减，短视频部分仅保留项目仓储、版本／来源绑定、会话存取及上下文构造的核心回归。本轮资产提示词、接口错误、模型配置及模型发现的专项测试已删除。历史完整验收不代表当前仍有相同覆盖面；使用临时项目及离线替身的测试结果也不代表真实文案质量。

定向验证入口：

```powershell
python -m pytest tests/test_video_v2_repository.py tests/test_conversation_management.py -q --tb=short
```

### 历史验证记录

以下为此前实现阶段记录，不是本次 2.4 文档核对重新运行的结果，也不据此推断当前用例总数：

- 此前清理阶段：1175 passed、5 subtests passed（162.07 秒）；同时记录 Ruff、格式、编译和文档链接等检查通过。
- 后续会话清理阶段：1179 passed、5 subtests passed（147.68 秒）；全量后又清理评估参数并增加 CLI 用例，聊天定向回归 8 项通过（1.21 秒）。完整说明和既有证据路径见[会话管理验证记录](conversation_management.md#历史验证记录)。

此前 2.4 文档核对仅修改 docs/short_video 内文档，检查接口、预算、状态和界面、Markdown 相对文件链接、代码围栏、图表定义及 git diff --check，未产生新的功能测试通过数。本次资产提示词改动的验证单独记录于 [DEVLOG.md](../../DEVLOG.md)，不覆盖历史实现证据。
