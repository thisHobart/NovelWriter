# 网文视频推广工作台：接口设计

文档版本：2.5；日期：2026-11-04；状态：同步分镜文本复制与导出，内部结构契约不变。

关联：[架构设计](architecture.md)、[交互设计](interaction.md)、[开发文档](development.md)。下文保留文字与提示词范围，使用本地 Python 服务，不新增媒体 API。

## 1. 用户动作对应接口

| 动作 | 当前实际入口 | 行为 |
|---|---|---|
| 保存原文与配置 | ProjectService.save_inputs(config, source, ranges=None, *, expected_revision) | 同一次业务变更保存配置、原文与选区 |
| 保存稿件／提示词 | ProjectRepository.save(kind, content, dependencies, *, expected_revision, ...) | 稿件追加版本；提示词替换当前内容并删除旧记录，不启动 AI 检查 |
| 复制分镜文本 | ArtifactPage.copy_storyboard_text → render_storyboard_text(content, catalog) | 使用当前编辑器内容及该稿精确依赖的目录，复制整段文字，不保存或调用模型 |
| 单阶段生成／重新生成 | WorkflowService.prepare(stage, ...) → WorkspaceWork → execute(command, ...) | 冻结输入并执行生成、检查、有限修改和保存 |
| 生成分镜 | prepare_storyboard_task → CreationWork | 先 asset_catalog，再 storyboard，共用任务预算 |
| 生成资产提示词 | prepare_prompt_task → CreationWork | 按待生成实体／状态执行 asset_spec，最后本地编译镜头提示词 |
| 优化当前镜头提示词 | prepare("shot_prompts", mode="revise", target_id=..., asks=..., shot_ids=...) → WorkspaceWork | 保留其他镜头内容，执行单阶段循环 |
| 用当前稿继续 | ArtifactPage.continue_creation → Workbench.start_creation | 先保存可见稿，再准备下阶段；保存失败不请求模型 |
| 查看／停止 | repository.load()["runs"] / host.runner.cancel_current() | 页面读取状态，CancelToken 传播停止请求；保留已落盘内容 |
| 导出 | ExportService.snapshot / export(destination, *, keys, allow_stale=False, allow_partial=False, overwrite=False) | 仅接受 .md，其他扩展名返回 INVALID_INPUT；本地验证并输出所选内容，不以 confirmation 为门槛 |
| 聊天讨论 | ConversationService.prepare / execute | 仅接收用户消息和历史对话，不接收文章／稿件／镜头／视图引用；保留项目隔离和手动重试 |

表中省略号表示省略可选参数，不是待开发 API；实际签名以对应源码为准。原语义占位接口 save_content／generate／continue_from_current／get_run／cancel_run 不作为可调用方法。confirm/rebind、UI 手动检查与 human 记录、目录确认、单独编译确认均不在当前界面路径中。必要校验、关系解析和编译由内部调用完成。

同请求 ID 同内容只执行一次；同键不同内容报冲突。来源 ID、revision、精确版本和请求预算由应用层维护，不要求用户填写。运行中用户另存内容时，生成结果保留为候选，不能覆盖新的人工选择。

## 2. 内部生成与检查契约

`WorkflowService(..., max_retries=2).prepare(...)` 冻结重试上限，`execute` 执行 Agent Loop。首次生成不计入重试；N 次重试对应最多 N+1 次结构合法的候选生成与检查。默认最多 6 次模型请求（3 次生成＋3 次检查）；max_retries=0 时只首次生成和检查。格式修复、失败请求均占共享预算，无额外网络重试。每请求最多 120 秒，普通任务总时限 300 秒；预算／时限耗尽提前停止并保留最新可用稿，明确标记未完成。

外部质量检查函数为 `review.check_candidate(command,candidate,send,repository) -> QualityCheckResult(passed: bool,feedback: str)`。command 提供阶段、原始要求和必要上下文；candidate 为最新合法内容。实现复用既有阶段规则、原文引文校验和 AI 判断，不增加评分维度；检查器只返回结果，不重新生成。`quality_check` 注入点用于控制检查替身，不新增服务层或部署要求。

Agent Loop 对通过结果立即结束；未通过且尚有重试次数时，用原始任务／上下文＋最新候选＋本次反馈重新生成；达到上限正常结束并输出最新候选，不能按问题数量回退。返回及原有运行终态中的 `review_status` 为 passed（通过）、issues（检查仍不通过）、unavailable（检查或调用不可用）、incomplete（预算／时限不足）；`retries_used` 记录本次修改生成次数，不包含首次生成。它们均不是审核流程或问题报告。结果仍可用现有编辑器保存、继续、导出。

分镜任务包含目录与分镜两个 Agent Loop，默认最多 12 次／600 秒；资产任务最多 6 × 待生成项数次，总时限 min(1800, max(300, 300 × 项数)) 秒。长文计划最多 4 × 段数 + 6 次，单次总时限 min(1800, 300 × (2 × 段数 + 1)) 秒；最终合稿使用相同 Agent Loop，显式续跑不重发成功分段。复用已有任务快照和保存机制，不增加独立运行存储。

检查反馈及中间检查问题仅在本轮内存中使用：不调用 save_report，不写 issues 到任务结果，不写入会话历史，也不持久化检查原文或含反馈的修改请求。已有调用记录仅保留类型、错误类型和时间；共享预算计数回调不传递请求／响应原文。独立历史诊断接口不属于本轮自动生成链路。

上述调用记录限制仅针对阶段 Agent Loop。合法候选在每次检查前保存，尚未选为当前稿；最终提交核对输入快照与 selected_before。文案／分镜冲突时返回 selected=false，保留候选；提示词冲突返回 TARGET_VERSION_CONFLICT，清理候选并保留当前编辑。提示词任务完成后仅保留最终当前结果，删除旧提示词、中间候选及运行命令中的正文副本；旧请求结果被替换后重放返回 PROMPT_REPLACED，不重新请求模型。其他阶段的初始运行命令继续保留原始上下文；普通聊天会记录实际 prompt／response，分段提取和分段改编也有各自的调用记录。

模型异常、检查服务异常及无效检查响应不触发质量修改循环。生成前失败沿用现有异常；已有合法候选时沿用保留可用稿与未完成／不可用状态；取消沿用取消异常和恢复机制。内部编程错误原样暴露。普通聊天不使用 Agent Loop；手改只做本地校验。

### 2.1 纯聊天实际契约

- `ConversationService.prepare(conversation_id, user_text, *, request_id=None)`：只读预检；读取会话、选择已完成轮次、加入本轮临时消息，调用上下文纯函数；冻结实际历史 ID、context_hash、项目与会话修订号、模型快照和 prompt，不提前落盘 pending。
- `execute(command, *, token=None, report=None)`：生成锁和 request_id 去重后，保存本轮用户消息，再发送 1 次／120 秒请求；保存最终回复和运行终态。保存失败、配置变化或修订冲突不继续模型请求。
- `ConversationStore` 是唯一会话文件读写入口；仅保存 user／assistant 文本和状态，不保存项目引用、检查反馈或候选。
- `build_conversation_context(...)` 是纯函数，48,000 字符默认上限包含系统规则与序列化文本；按最早完整轮次裁剪副本，保留本轮完整消息。规则和本轮仍超限返回 CONTEXT_TOO_LARGE，不删除磁盘历史。
- 停止期间完整返回的回复以 cancelled 保存；失败、取消、中断及无完整回复的轮次不进入后续模型历史。“重试上次问题”只填回输入框，必须再次点击发送。

全部存储签名、恢复规则及聊天时序见[会话管理](conversation_management.md)。普通聊天使用完整返回的文字，不提供流式 UI；ScriptLLM 将完整 prompt 作为一次 user 消息提交，模型 SDK 不另行读取会话文件。

## 3. 内部关系与版本

保存时自动维护已知姓名、别名与实体关系；无法可靠判断身份时保留草稿并返回具体歧义。取消人工绑定 UI 不等于删除实体引用校验。

旧 confirmed 状态不再控制下阶段入口或导出。新任务引用用户选择的当前可用版本；继续操作不是新的审批记录。上游改变后仍保存旧依赖，明确沿用旧稿不得重写成依赖最新原文。

导出仅校验实际所选内容的结构、来源说明和输出文件。不再区分带审核门槛的“正式”与“草稿”流程；未包含的部分在清单注明。无需确认所有视图或手动编译，确定性 PromptCompiler 在必要时内部执行；若需要额外 LLM 调用，必须属于已启动生成任务。

## 4. 内容模型与 Prompt 的使用边界

以下模型用于保存和自动检查，不据此自动生成一整页表单。对用户显示姓名、正文和提示词；内部引用、来源范围和视图要求由软件维护。原 user Prompt 保持，两次改写仍为不同阶段。

## 5. 项目、来源与选题模型

### 5.1 ProjectConfig

| 字段 | 类型／缺省 | 含义 |
|---|---|---|
| title | 非空 string | 项目名称 |
| tracks | object | themes／settings／backgrounds 三个 string[]，默认空；自定义标签单列 custom_tracks |
| audience / tone | string | 受众、语气，允许空；空表示无额外约束 |
| narrative_person | first / third / source，默认 source | 保持或改为第一／第三人称，不暗改人物身份 |
| target_words | int 或 null | 可选文案字数目标，正数 |
| target_duration_ms | int 或 null | 可选总时长目标，正数；不限制为 30–90 秒 |
| shot_duration_range_ms | 默认 [5000,15000] | 建议区间，不是强制每镜等长 |
| required / forbidden | string[] | 必需、禁止内容，默认空 |
| permitted_adaptations | string[] | 用户明确允许的改编，例如压缩时间或合并次要人物；默认空 |
| text_provider | 配置引用 | 阶段生成和聊天共用现有文字服务；提交时冻结实际选择，不保存明文密钥 |
| prompt_refs / ruleset_ref | 版本引用 | 各阶段模板版本与规则集版本 |

内置赛道词表见架构第 3 节，接口返回固定分组及可用项，不从 UI 文案反向猜分组。

配置、原文、选区及下列业务模型均保存在统一 ArtifactRecord 外壳的 content 中；id、kind、key、dependency_refs、parent_id、content_hash、created_at 属于记录外壳。ConfigVersion／SourceVersion 等是业务名称，不是额外的 Python 数据类或独立文件格式。生成请求的 config 引用不可变配置记录。

### 5.2 SourceVersion、SelectionVersion 与 SourceRef

- SourceVersion：`id, title, origin(paste/file), original_file_name?, raw_sha256, normalized_text, text_sha256, paragraphs[]`。TXT／MD 默认按 UTF-8／UTF-8 BOM 解码，失败返回 SOURCE_ENCODING_ERROR 并允许选择编码重导；MD 先作为文本保留，不执行 HTML。
- `paragraphs[]`：`paragraph_id, start, end`。来源位置基于这一 SourceVersion 的规范化文本：移除 BOM、统一换行为 LF，以 Unicode 码点计数，使用 `[start,end)`；Qt UTF-16 光标位置必须显式转换。
- SelectionVersion：`id, source_id, ranges[{start,end}], selected_text_sha256`，范围有效、非空、排序后不重叠。显示选区拼接文本时保留映射，不能用拼接后的偏移冒充原文偏移。
- SourceRef：`source_id, paragraph_id?, start, end`；可选 `quote` 用于展示，提供时必须与对应切片一致。
- 超过模型有效上下文时，返回 INPUT_TOO_LARGE 和统计信息；允许缩小选区，或用户启用分块计划。分块记录范围、顺序、共用事实表和最终合稿依赖；不得静默截断后声称处理全文。

## 6. 文案模型

### 6.1 NovelCopy

`opening{text,source_refs}, paragraphs[{id,text,source_refs}], cliffhanger{text,source_refs}, changes[{type(delete/compress/rephrase/adapt),reason,source_refs}], unresolved_facts[]`。

完整推文由 opening、paragraphs、cliffhanger 依次拼接，输出结构避免把开头或结尾又放进正文造成重复。开头可以重排原文信息；悬念必须指向已有矛盾，不能伪造未发生的核心事实。

### 6.2 VideoCopy

`blocks[{id,role(opening/body/cliffhanger),narration,dialogue[{speaker_label,text}],visible_action,source_refs,novel_paragraph_refs}], changes[], estimated_speech_ms?, estimation_config_ref?`。

至少有 opening 和 cliffhanger 角色的非空文本块，body 按内容需要。口播、对白和可见动作分别记录；必要旁白允许保留。口播估计依据速度设置，不等于实际成片时长。

## 7. 资产目录、设定与视图提示词

### 7.1 AssetCatalog

`entities[{asset_id,kind(character/scene/prop),name,aliases[],usage(on_screen/voice_only),source_refs[],variants[{variant_id,label}],inferred_fields[]}]`。usage 对人物生效，其他类型固定为 on_screen；voice_only 不自动要求定妆图，出镜用途改变时重新核对视图需求。

模型提出名称与别名，应用分配稳定 ID 并匹配已有实体。仅在身份确实无法确定时提出具体问题。未确定外观列在 inferred_fields，不标成原文事实，用户可以直接改文字。目录解析与校验作为生成分镜的内部步骤，不要求用户确认目录。

### 7.2 AssetSpec

共同字段：`asset_id,variant_id,catalog_version_id,style,positive_prompt,negative_prompt,views[],source_refs,inferred_fields`。保存产生对应 kind 的 ArtifactVersion。

| kind | 结构化字段 | 默认视图计划 |
|---|---|---|
| character_spec | age,gender,appearance,hair,makeup,clothes,accessories,period,state,identity_constraints | head_front、body_front、body_side、body_back |
| scene_spec | drama_type,era,location,time,spatial_structure,height_notes,center,left,right,rear,entrances,landmarks,main_light,fill_light,atmosphere,composition | establishing、reverse，默认均必需 |
| prop_spec | name,description,era,size_shape,material,main_color,secondary_color,components,state | front、three_quarter、top，默认均必需 |

每个 view 具有 `view_id,label,camera_view,composition,required,positive_prompt,negative_prompt`；默认视图 required=true，额外视图可在内容中保留，但当前界面不提供视图增删管理。导出已选资产时要求默认必要视图齐全，缺失视图属于结构错误，不能用 allow_partial 绕过；allow_partial 允许跳过尚未生成的内容项。视图属于拍摄方向，variant 属于时期／造型／状态。

### 7.3 ViewPrompt：每个视图的具体提示词

视图内容至少包含 `view_id,label,camera_view,composition,required,positive_prompt,negative_prompt`；生成后由代码补充 `source_refs,template_ref,content_hash,spec_input_hash`，不重写模型生成的视角及正负提示词。视图嵌入 AssetSpec 的 views，不要求独立 id／spec_version_id；设计补充主要由设定的 inferred_fields 表达，不把概念字段当作所有视图的必需字段。

`spec_input_hash` 在内部关联设定与视图文本。设定改变时标记旧提示词需要更新；可确定的本地编译自动执行，需要模型生成的部分随下一次生成任务处理。用户可明确导出原有文字并保留旧来源说明。内部哈希不能由界面或聊天直接改写，历史记录不原地改变。

人物四视图共享身份和服装设定，只有视角／构图不同；场景角度共享布局；道具角度共享结构与状态。source_refs 表示原文依据，inferred_fields 表达设计补充，不建立逐项审批记录。

模型输出应为具体文字。asset_spec 必须包含当前类型的全部默认视图；view_prompts 必须包含原版本计划的全部视图（包括已有额外视图），且不能更改基础设定。生成专用校验拒绝缺失／重复／新增的视图编号、空字段、`{age}`、`【道具名称】` 等未填变量，沿用现有格式修复及总预算；无法形成合法候选则保留原稿。手动保存与历史读取的契约不收紧，显式 compile_asset 入口保留。导出时已选资产仍须通过本地结构和完整性检查。正负提示词分别复制，不触发任何外部图片／视频请求。

## 8. 分镜契约与时间轴

Storyboard：`video_copy_version_id,catalog_version_id,shots[]`；每镜字段如下。

| 字段 | 类型与规则 | 表格显示 |
|---|---|---|
| shot_id / order | 稳定 ID；order 连续正整数 | 镜号：按当前顺序显示，改顺序不改 ID |
| duration_ms | 正整数 | 时间轴及可选时长列 |
| start_ms / end_ms | 由代码按 order 累计，不让模型随意填写 | 时间轴 `[起点,终点)` |
| shot_size | 远景／全景／中景／近景／特写／自定义 | 景别 |
| camera_movement | string | 镜头运动 |
| scene_ref | asset_id,variant_id；有效场景 | 放入扩展场景列／元数据 |
| characters | 人物引用数组，允许空 | 人物：显示姓名与状态 |
| props | 道具引用数组，允许空 | 放入扩展道具列／元数据 |
| action | 非空 string | 动作 |
| dialogue / narration | 对白数组及 string；允许空 | 对白列以“人物：”“旁白：”区分 |
| sound | string，允许空 | 音效 |
| visual_description | 非空 string | 内部画面描述，随镜头内容保存 |
| visual_prompt_draft | 非空 string | 画面生成提示词草稿 |
| video_block_refs / source_refs | 视频块引用与原文引用 | 元数据，可展开来源 |
| duration_exception_reason | string 或 null | 超出建议区间时可空，不作为继续操作的门槛，作为 warning 处置 |

第一镜 start_ms=0，end_ms=start_ms+duration_ms，下一镜 start_ms=前镜 end_ms。没有重叠、负值和缝隙；镜头拆分、合并、排序后全部重新计算。时间轴由 validate_content／recalculate_timeline 根据时长重算，不信任模型提供的 start_ms／end_ms；当前没有单独的时间轴解析报告。

分镜需覆盖已保存视频文案的有效内容；镜头中无人、静物特写是合法情况。人物对白说话者必须在本镜出场或标记 voice_over；旁白不创建虚构出镜人物。

PromptBundle：`storyboard_version_id,asset_spec_refs[],shots[{shot_id,resolved_prompt,negative_prompt,video_motion_prompt?,asset_refs,source_refs}]`。编译时验证资产设定版本和变体，无法解析的引用返回具体内容问题；已生成文字仍可按所选范围导出并说明缺项。video_motion_prompt 可选，基于该镜动作、时长和运镜生成文字，不调用视频模型。

character_spec／scene_spec／prop_spec 按 asset_id＋variant_id、prompt_bundle 按全体镜头，各只保留当前一份。新记录的 parent_id=null，select 不支持恢复提示词，替换后删除旧记录与文件；资产提示词被替换时，依赖旧记录的镜头提示词一并清除并从当前资产重新编排。is_current 对提示词表示是否为当前保留内容，不再递归比较上游历史；生成中的快照与修订冲突校验仍然有效。


## 9. Prompt 契约：保留用户原意，结构化封装

### 分镜对外交付格式

`storyboard_text.render_storyboard_text(content, catalog) -> str` 是无 I/O 的文本编排函数；输入为结构化分镜 content 和对应资产目录 content。重算连续时间轴，以整数毫秒转为精确秒数，按“视觉风格／场景概述／分镜／镜头要求／音频／限制”输出整段中文，不显示内部编号和来源坐标。

时间段、镜号、景别、运镜、人物与状态、场景、道具、画面、动作及各镜音频均保留。无出镜人物与画外对白分开表达，未指定音频不补写音乐。可选 visual_description 缺失时使用已有动作及 visual_prompt_draft。不会调用模型重写或改变保存数据。

分镜页复制包含未保存编辑；导出仍先处理编辑保存，再建立不可变快照。Markdown 导出复用同一编排函数，按 storyboard 的 asset_catalog 依赖读取精确目录，避免错误使用当前另一版目录。文件导出仅支持 .md，继续生成 .manifest.json 来源清单；项目 schema_version 仍为 2。

模板库以阶段区分角色与 instruction；自定义模板保存为不可变 prompt_template 版本（content: stage,instruction），默认模板引用为阶段 default-v2。结构约束与负面约束由 prompts.py 和 validation.py 统一定义，任务冻结最终完整 Prompt、输入和具体模型名。原始小说置于清晰分隔的数据区。

### 9.1 第一阶段：小说推文

用户给定核心指令：

> 你是一个专业的小说推文作者，请你优化这段小说，使用“爆款开头+结尾留钩”的结构，不利于观众停留的剧情酌情删掉。

注入：选题配置、原文选区、允许改编范围、长度目标。输出 NovelCopy，同时标记删减／压缩／改编与原文位置；保持核心人物身份、因果和事件事实，不能为制造开头捏造原文没有的关键反转。

### 9.2 第二阶段：视频推广文案

用户给定核心指令：

> 你是一个专业的视频推文博主，请你优化这段文案，用作视频推文，“爆款开头+结尾留钩”的结构，不适合做成视频的内容适当进行改编。

注入：已保存小说推文、对应原文依据、视频呈现目标。输出 VideoCopy，压缩拖沓画面，把难以表现的心理或解释转成可拍动作、对白或必要旁白。改编超出 permitted_adaptations 时列入待人工核对，不静默改变核心事实。

### 9.3 第三阶段：分镜

用户给定核心指令：

> 你是一名资深的AI真人短剧分镜师，擅长将故事剧本进行细致拆分并转化为分镜脚本，请基于之前生成的剧本，生成包括“镜号、时间轴、景别、镜头运动、人物、动作、对白、音效、画面生成提示词”的分镜头脚本。

注入：VideoCopy、内部解析的资产目录、时长建议、镜头模板。返回结构化 Storyboard；镜头号和绝对时间轴由代码生成。用户文字中的“警别”统一为“景别”。

### 9.4 人物定妆照

字段：年龄、性别、外貌特征、发型、妆容、服装、配饰；每个时期／状态变体另存。标准提示词模板：

```text
写实真人短剧定妆照，{age}，{gender}，{appearance}，{hair}，{makeup}，
{clothes}，{accessories}，{view_pose}，{composition}，背景为纯白色，
无多余光效，真实皮肤质感，五官自然稳定，服装材质清晰，角色造型统一，
高清细节，适合真人短剧人物资产图。
```

head_front 使用正面头部特写；body_front 正面站立全身；body_side 侧面全身；body_back 背面全身。构图不能同时写头部特写和全身，方向不能相互冲突。

负面提示词默认建议：额外人物、重复肢体、手部畸形、面部变形、服饰漂移、文字水印、复杂背景、过曝、模糊。用户可编辑；它们是生成约束，不是质量保证。原文中的“真是皮肤质感”按“真实皮肤质感”规范化。

### 9.5 场景资产

```text
写实真人短剧场景资产图，{drama_type}，{era}，{location}，{time}，
空间为{spatial_structure}，高度信息为{height_notes}，中央为{center}，
左侧为{left}，右侧为{right}，后方设有{rear}，门窗与出入口为{entrances}，
场景中包括{landmarks}，物品位置清晰，空间结构完整，主要通道无遮挡，
环境光线为{main_light}，辅以{fill_light}，整体呈现{atmosphere}，
空置场景，无角色参与，采用{composition}，以{camera_view}完整展示场景空间，
真实摄影质感，空间层次清楚，高清细节，适合作为后续分镜设计、人物入景和视频创作的场景资产参考图。
```

各角度共享同一布局；反向视角下用空间坐标关系描述陈设，不能机械复制画面左右造成场景镜像。未提供高度可留为“未限定”，不虚构精确尺寸。

### 9.6 道具资产

```text
写实真人短剧类型道具资产图，{era}的{name}，道具整体为{size_shape}，
外观呈现{description}，道具采用{material}制作，整体以{main_color}为主，
辅以{secondary_color}，材质质感清晰可见。结构上包含{components}，
当前状态为{state}，比例自然，层次清楚，细节完整，便于后续识别和使用。
单个道具或单组道具独立展示，采用{camera_view}视角，完整展示整体轮廓、
主要结构和关键细节。纯白色背景或简洁干净背景，光线均匀柔和，无人物，
无手部，无多余场景干扰，真实摄影质感，高清细节，道具资产参考图。
```

负面词按资产类型配置，例如场景排除人物、道具排除手部；不把同一组负面词无差别套到镜头画面。

### 9.7 按文章生成可用提示词

第 9.4～9.6 节为用户原始模板，不是最终交付。当前 asset_spec 阶段由 LLM 一次生成具体人物／场景／道具字段及全部计划视图的正负提示词，保存 source_refs 与 inferred_fields；顶层正负提示词为空，具体文字位于 views。finalize_asset_prompts 仅校验完整性、排序并维护内部指纹，不用模板覆盖文字。view_prompts 入口保留，仅优化 views。compile_asset 只用于显式本地编译；compile_shots 仍根据分镜与已有设定生成镜头提示词，用户优化镜头时通过 shot_prompts 调用模型。禁止遗留未替换变量，禁止把文章没有的信息伪装成原文事实。可选视频运动文字不调用视频模型。

review.check_candidate 对资产使用专门的整组提示词检查要求，检查统一设定、单视图及跨视图一致性，不套用文案的开头留钩等规则。输出仍为既有 issues 格式，target_path 指向具体字段；跨视图问题在 explanation 中说明关联视图，suggestion 给出可执行修改。反馈传入现有循环，修订后重新检查；不新增检查报告、UI 或图片调用。



## 当前实现的格式与错误边界

当前生成入口已实现自动 Agent Loop。数据入口只接受当前完整格式：模型快照为 provider／model 对象，配置包含必需字段；保存和编译路径维护已生成视图的 content_hash／spec_input_hash。旧 confirmed／reports 字段保留不等于支持任意旧来源类型、字符串快照或缺失指纹的格式；读取失败明确反馈并保持原文件不变。

业务、界面、文字服务和持久化的共享类型见 core/short_video/v2/contracts.py。模型网络异常保留原始 cause 并转换为 PROVIDER_UNAVAILABLE；内部字段、类型、逻辑错误原样暴露。运行状态收尾不改变原错误类别，收尾文件失败另记上下文。格式修复只接收明确的输入／输出校验失败。
