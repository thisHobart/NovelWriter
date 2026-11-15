# 网文视频推广工作台：接口设计

文档版本：2.12；日期：2026-11-15；状态：文字提示词 default-v3；记录字段不迁移，多工作流视频接口保持不变。

关联：[架构设计](architecture.md)、[交互设计](interaction.md)、[开发文档](development.md)。第 1～9 节描述已有本地 Python 文字服务；第 10 节记录已实现的多工作流视频服务和内置 MCP 工具；资产生图仍未实现。

## 1. 用户动作对应接口

| 动作 | 当前实际入口 | 行为 |
|---|---|---|
| 保存原文与配置 | ProjectService.save_inputs(config, source, ranges=None, *, expected_revision) | 同一次业务变更保存配置、原文与选区 |
| 保存稿件／提示词 | ProjectRepository.save(kind, content, dependencies, *, expected_revision, ...) | 稿件追加版本；提示词替换当前内容并删除旧记录，不启动 AI 检查 |
| 复制分镜文本 | ArtifactPage.copy_storyboard_text → render_storyboard_text(content, catalog) | 使用当前编辑器内容及该稿精确依赖的目录，复制整段文字，不保存或调用模型 |
| 单阶段生成／重新生成 | WorkflowService.prepare(stage, ...) → WorkspaceWork → execute(command, ...) | 冻结输入并执行生成、检查、有限修改和保存 |
| 生成分镜 | prepare_storyboard_task → CreationWork | 先 asset_catalog，再 storyboard，分别执行质量修复循环 |
| 生成资产提示词 | prepare_prompt_task → CreationWork | 按待生成实体／状态执行 asset_spec，最后本地编译镜头提示词 |
| 优化当前镜头提示词 | prepare("shot_prompts", mode="revise", target_id=..., asks=..., shot_ids=...) → WorkspaceWork | 保留其他镜头内容，执行单阶段循环 |
| 用当前稿继续 | ArtifactPage.continue_creation → Workbench.start_creation | 先保存可见稿，再准备下阶段；保存失败不请求模型 |
| 查看／停止 | repository.load()["runs"] / host.runner.cancel_current() | 页面读取状态，CancelToken 传播停止请求；保留已落盘内容 |
| 导出 | ExportService.snapshot / export(destination, *, keys, allow_stale=False, allow_partial=False, overwrite=False) | 仅接受 .md，其他扩展名返回 INVALID_INPUT；本地验证并输出所选内容，不以 confirmation 为门槛 |
| 聊天讨论 | ConversationService.prepare / execute | 仅接收用户消息和历史对话，不接收文章／稿件／镜头／视图引用；保留项目隔离和手动重试 |

表中省略号表示省略可选参数，不是待开发 API；实际签名以对应源码为准。原语义占位接口 save_content／generate／continue_from_current／get_run／cancel_run 不作为可调用方法。confirm/rebind、UI 手动检查与 human 记录、目录确认、单独编译确认均不在当前界面路径中。必要校验、关系解析和编译由内部调用完成。

同请求 ID 同内容只执行一次；同键不同内容报冲突。来源 ID、revision、精确版本和运行记录由应用层维护，不要求用户填写。运行中用户另存内容时，生成结果保留为候选，不能覆盖新的人工选择。

## 2. 内部生成与检查契约

`WorkflowService(..., max_retries=2).prepare(...)` 冻结质量重试上限，`execute(command, *, token=None, report=None, on_request=None)` 执行 Agent Loop。首次生成不计入重试；N 次重试对应最多 N+1 次结构合法的候选生成与检查。格式错误每次候选生成最多修复两次，与质量修改分别计数；没有总请求次数或整轮时限。`ScriptLLM(model, *, token=None, record=None, transport=None)` 不接收预算，不设整次墙钟期限，网络超时沿用 SDK 默认值，自动重试关闭。

外部质量检查函数为 `review.check_candidate(command,candidate,send,repository) -> QualityCheckResult(passed: bool,feedback: str)`。command 提供阶段、原始要求和必要上下文；candidate 为最新合法内容。实现复用既有阶段规则、原文引文校验和 AI 判断，不增加评分维度；检查器只返回结果，不重新生成。`quality_check` 注入点用于控制检查替身，不新增服务层或部署要求。

Agent Loop 对通过结果立即结束；未通过且尚有质量重试次数时，用原始任务／上下文＋最新候选＋本次反馈重新生成；达到上限正常结束并输出最新候选。`review_status` 为 passed（通过）、issues（检查仍不通过）、unavailable（检查或调用不可用）；历史 incomplete 状态继续可读。`retries_used` 只记录质量修改生成次数，不包含首次生成与格式修复。结果仍可用现有编辑器保存、继续、导出。

分镜任务包含目录与分镜两个 Agent Loop；资产任务逐项运行；长文计划逐段提取事实、改编并最终合稿，不按累计调用次数或耗时停止。新任务和分段计划不含 max_calls、seconds；calls_used 仅记录调用次数。旧分段计划保留原冻结哈希核验，历史限制字段不参与执行判断。长文续跑不重发成功分段，不增加独立运行存储。

检查反馈及中间检查问题仅在本轮内存中使用：不保存检查报告，不写 issues 到任务结果，不写入会话历史，也不持久化检查原文或含反馈的修改请求。已有调用记录仅保留类型、错误类型和时间；调用计数回调不传递请求／响应原文。已删除无调用方的独立检查服务与人工确认、报告写入接口。

上述调用记录限制仅针对阶段 Agent Loop。合法候选在每次检查前保存，尚未选为当前稿；最终提交核对输入快照与 selected_before。文案／分镜冲突时返回 selected=false，保留候选；提示词冲突返回 TARGET_VERSION_CONFLICT，清理候选并保留当前编辑。提示词任务完成后仅保留最终当前结果，删除旧提示词、中间候选及运行命令中的正文副本；旧请求结果被替换后重放返回 PROMPT_REPLACED，不重新请求模型。其他阶段的初始运行命令继续保留原始上下文；普通聊天会记录实际 prompt／response，分段提取和分段改编也有各自的调用记录。

模型异常、检查服务异常及无效检查响应不触发质量修改循环。生成前失败沿用现有异常；已有合法候选时沿用保留可用稿与未完成／不可用状态；取消沿用取消异常和恢复机制。内部编程错误原样暴露。普通聊天不使用 Agent Loop；手改只做本地校验。

### 2.1 纯聊天实际契约

- `ConversationService.prepare(conversation_id, user_text, *, request_id=None)`：只读预检；读取会话、选择已完成轮次、加入本轮临时消息，调用上下文纯函数；冻结实际历史 ID、context_hash、项目与会话修订号、模型快照和 prompt，不提前落盘 pending。
- `execute(command, *, token=None, report=None)`：生成锁和 request_id 去重后，保存本轮用户消息，再发送 1 次请求；保存最终回复和运行终态。保存失败、配置变化或修订冲突不继续模型请求。
- `ConversationStore` 是唯一会话文件读写入口；仅保存 user／assistant 文本和状态，不保存项目引用、检查反馈或候选。
- `build_conversation_context(...)` 是纯函数，48,000 字符默认上限包含系统规则与序列化文本；按最早完整轮次裁剪副本，保留本轮完整消息。规则和本轮仍超限返回 CONTEXT_TOO_LARGE，不删除磁盘历史。
- 停止期间完整返回的回复以 cancelled 保存；失败、取消、中断及无完整回复的轮次不进入后续模型历史。“重试上次问题”只填回输入框，必须再次点击发送。

全部存储签名、恢复规则及聊天时序见[会话管理](conversation_management.md)。普通聊天使用完整返回的文字，不提供流式 UI；ScriptLLM 将完整 prompt 作为一次 user 消息提交，模型 SDK 不另行读取会话文件。

## 3. 内部关系与版本

保存时自动维护已知姓名、别名与实体关系；无法可靠判断身份时保留草稿并返回具体歧义。取消人工绑定 UI 不等于删除实体引用校验。

旧 confirmed 状态不再控制下阶段入口或导出。新任务引用用户选择的当前可用版本；继续操作不是新的审批记录。上游改变后仍保存旧依赖，明确沿用旧稿不得重写成依赖最新原文。

导出仅校验实际所选内容的结构、来源说明和输出文件。不再区分带审核门槛的“正式”与“草稿”流程；未包含的部分在清单注明。无需确认所有视图或手动编译，确定性 PromptCompiler 在必要时内部执行；若需要额外 LLM 调用，必须属于已启动生成任务。

## 4. 内容模型与 Prompt 的使用边界

以下模型用于保存和自动检查，不据此自动生成一整页表单。对用户显示姓名、正文和提示词；引用、来源范围和视图要求由软件维护。两次改写分别整理事件、转换视听表达，默认提示词升级，自定义模板不覆盖。

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
| shot_duration_range_ms | 默认 [5000,15000] | 参考区间；按动作与声音决定时长，允许合理短镜头和长镜头，不强制等长 |
| required / forbidden | string[] | 必需、禁止内容，默认空 |
| permitted_adaptations | string[] | 用户明确允许的改编，例如压缩时间或合并次要人物；默认空 |
| text_provider | 配置引用 | 阶段生成和聊天共用现有文字服务；提交时冻结实际选择，不保存明文密钥 |
| prompt_refs / ruleset_ref | 版本引用 | 各阶段模板版本与规则集版本 |
| storyboard_skill_id | string 或 null，默认 null | 手动选用的分镜 Skill；当前仅 short-video-content-storyboard。旧配置缺失同 null，不自动选择；其他值在准备请求前拒绝 |

原文页仅编辑文章、选区和 storyboard_skill_id；上述赛道及其他创作设定字段保留原值／默认值，不再展示整页表单。兼容旧配置读取时不写文件，用户保存后才把选择作为配置版本持久化。

内置赛道词表作为旧配置数据保留，原文页不再展示或编辑，不从 UI 文案反向猜分组。

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

完整推文由 opening、paragraphs、cliffhanger 依次拼接，避免重复。cliffhanger 是保留的兼容字段名，表示结尾段，可以写原文已有结果、收束或实际停点，不要求悬念。允许重排、压缩、忠实口语化和合理外化，不能新增关键事实或结局；无结果时可在 unresolved_facts 说明缺失。

### 6.2 VideoCopy

`blocks[{id,role(opening/body/cliffhanger),narration,dialogue[{speaker_label,text}],visible_action,source_refs,novel_paragraph_refs}], changes[], estimated_speech_ms?, estimation_config_ref?`。

至少有 opening 与 cliffhanger（结尾）角色的非空块，body 按内容需要；校验不要求结尾具备悬念。可见动作、对白、旁白分别记录，画面能传达的信息不重复旁白。按少于60秒估算容量，顺序与并行声画分别计时；文本估算不等于实测成片时长。

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

模型输出应为具体文字。asset_spec 必须包含当前类型的全部默认视图。生成专用校验拒绝缺失／重复／新增的视图编号、空字段、`{age}`、`【道具名称】` 等未填变量，沿用有限格式修复；无法形成合法候选则保留原稿。手动保存与历史读取的契约不收紧。资产设定和全部视图由同一次生成维护，已删除无调用方的单独视图优化及旧模板编译入口。导出时已选资产仍须通过本地结构和完整性检查。正负提示词分别复制，不触发任何外部图片／视频请求。

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

storyboard 的 prepare 根据当前配置 storyboard_skill_id 明确选择性加载[短视频内容分镜 Skill](../../core/short_video/v2/skills/short-video-content-storyboard/SKILL.md)，将ID和完整文本冻结到 command.requirements.skill_id／instruction，检查与重试使用该快照，不重新读取文件或选择 Skill。缺失／null 时不加载，基础提示和审阅也不隐性包含 H3／5–8秒规则。其他阶段 requirements.skill_id=null，即使项目已选 Skill 也不注入其正文；普通聊天亦不使用。

选中该 Skill 后，H3 常规镜头按5–8秒设计，必要短镜可低于5秒，超过8秒优先拆镜，旧区间仅供参考。duration_ms 仍是计划时长，低于工作流范围需后续裁切，当前未自动裁切。结构字段、模型参数和人工检查入口不变。

分镜需覆盖已保存视频文案的有效内容；镜头中无人、静物特写是合法情况。人物对白说话者必须在本镜出场或标记 voice_over；旁白不创建虚构出镜人物。

PromptBundle：`storyboard_version_id,asset_spec_refs[],shots[{shot_id,resolved_prompt,negative_prompt,video_motion_prompt?,asset_refs,source_refs}]`。编译时验证资产设定版本和变体，无法解析的引用返回具体内容问题；已生成文字仍可按所选范围导出并说明缺项。video_motion_prompt 是可选的已有镜头运动文字，保留读取和编排，不提供独立生成入口。现有文字编译本身不调用视频模型；新增媒体任务单独读取这些文字。

PromptPage 的镜头页只将 storyboard_version_id 与当前分镜匹配的 PromptBundle 作为完整可编辑结果。缺少该结果时，在内存中将当前分镜的 visual_prompt_draft 显示为只读、可复制的画面草稿，负面提示词留空；不写入 PromptBundle，不作为完整提示词导出。状态显示缺少的资产设定数，生成按钮复用 start_creation("prompts") 补齐资产并编排；用户查看或切换草稿不触发模型请求和项目写入。

character_spec／scene_spec／prop_spec 按 asset_id＋variant_id、prompt_bundle 按全体镜头，各只保留当前一份。新记录的 parent_id=null，select 不支持恢复提示词，替换后删除旧记录与文件；资产提示词被替换时，依赖旧记录的镜头提示词一并清除并从当前资产重新编排。is_current 对提示词表示是否为当前保留内容，不再递归比较上游历史；生成中的快照与修订冲突校验仍然有效。


## 9. Prompt 契约：保留用户原意，结构化封装

### 分镜对外交付格式

`storyboard_text.render_storyboard_text(content, catalog) -> str` 是无 I/O 的文本编排函数；输入为结构化分镜 content 和对应资产目录 content。重算连续时间轴，以整数毫秒转为精确秒数，按“视觉风格／场景概述／分镜／镜头要求／音频／限制”输出整段中文，不显示内部编号和来源坐标。

时间段、镜号、景别、运镜、人物与状态、场景、道具、画面、动作及各镜音频均保留。无出镜人物与画外对白分开表达，未指定音频不补写音乐。可选 visual_description 缺失时使用已有动作及 visual_prompt_draft。不会调用模型重写或改变保存数据。

分镜页复制包含未保存编辑；导出仍先处理编辑保存，再建立不可变快照。Markdown 导出复用同一编排函数，按 storyboard 的 asset_catalog 依赖读取精确目录，避免错误使用当前另一版目录。文件导出仅支持 .md，继续生成 .manifest.json 来源清单；项目 schema_version 仍为 2。

模板库以阶段区分 instruction；自定义模板保存为不可变 prompt_template 版本（content: stage,instruction）并继续优先，默认模板引用升级为阶段 default-v3。任务冻结完整Prompt、输入、模型与所选Skill；旧运行快照与稿件不改。Prompt由共享事实／格式规则、阶段任务、专属约束、只读资料、独立演示和结构样例组成。演示与当前素材隔离，不能混入实际人物与事件。

### 9.1 第一阶段：小说推文

默认任务：从原文组织一个适合独立短视频的完整事件，保留必要人物、动机、行动、因果与已有结果，压缩背景、重复说明和无关支线；开头直接进入事件，结尾交代结果或落点，不强制悬念、反转或笑点。

输入为原文选区与配置，输出 NovelCopy。允许合理外化，不新增关键事实；changes 使用原枚举说明删改依据。素材无结果时保留实际停点并说明缺失，不能编造收束。

### 9.2 第二阶段：视频推广文案

默认任务：将当前故事文案转为可观看、可听懂的内容，分别安排可见动作、必要对白和旁白；不重复解释已展示的画面，不继续文学润色。保留理解事件所需的因果和已有结果。

输入为当前小说推文和原文依据，输出 VideoCopy。少于一分钟；有效 target_duration_ms 按目标规划，未指定时自然结束，超范围目标如实报告。清晰中文声音可从每秒3–4字估算，另考虑停顿、换人和情绪；顺序累计、并行取覆盖时长，不假称已测配音。

### 9.3 第三阶段：分镜

默认任务：按视觉与声音节拍拆镜，写清主体、起始状态、主要动作、结束状态、景别、运镜与声音，保持空间及动作连续，不机械按段落切镜或均分时长。输入为 VideoCopy、实际资产目录及用户明确选择的可选 Skill，返回 Storyboard，绝对时间由代码累加。

目录只提取当前视频实际出镜、发声或使用的对象与必要状态，原文只用于核对。分镜结构样例不再把全目录人物、道具和全部视频块放进第一镜；实际输出仍须真实覆盖全部视频块。代码给出 SHOT-TOTAL-001（合计达到或超过60秒）、SHOT-TARGET-001（目标超范围）、SHOT-TARGET-002（有效目标不闭合）提醒，手动保存不新增时长门槛。

### 9.4 人物定妆照

字段：年龄、性别、外貌特征、发型、妆容、服装、配饰；每个时期／状态变体另存。标准提示词模板：

以下资产模板仅说明描述组成；实际统一设定及全部视图由模型生成，代码不以模板覆盖正文。只生成当前视频需要的状态，不为未使用时期扩展资产。

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

第 9.4～9.6 节为用户原始模板，不是最终交付。当前 asset_spec 阶段由 LLM 一次生成具体人物／场景／道具字段及全部计划视图的正负提示词，保存 source_refs 与 inferred_fields；顶层正负提示词为空，具体文字位于 views。finalize_asset_prompts 仅校验完整性、排序并维护内部指纹，不用模板覆盖文字。compile_shots 根据分镜与已有设定生成镜头提示词，用户优化镜头时通过 shot_prompts 调用模型。禁止遗留未替换变量，禁止把文章没有的信息伪装成原文事实。旧模板编译、单独视图优化和视频运动文字生成分支已删除。

review.check_candidate 分别检查故事因果与已有结果、视频声画容量、目录实际对象、分镜执行与衔接、资产多视角及镜头画面文字。合理外化不当作新增关键事实；素材缺结果不得要求编造结局。资产不套用文案风格要求，镜头提示词不套用 H3 专用格式。输出仍为既有 issues，定位具体字段与对应内容，给出可执行修改，不以爆款感或高级感等偏好重写。格式修复仅处理JSON、字段、枚举和引用，不借机改剧情。反馈进入现有有限循环，不新增检查报告、UI 或图片调用。



## 现有文字实现的格式与错误边界

文字生成入口使用自动 Agent Loop。数据入口只接受当前完整格式：模型快照为 provider／model 对象，配置包含必需字段；保存和编译维护视图的 content_hash／spec_input_hash。保留旧 confirmed／reports 字段不等于支持任意旧来源类型、字符串快照或缺失指纹；读取失败明确反馈并保持原文件不变。

业务、界面、文字服务和持久化的共享类型见 core/short_video/v2/contracts.py。文字模型网络异常保留原始 cause 并转换为 PROVIDER_UNAVAILABLE；内部字段、类型、逻辑错误原样暴露。运行收尾不改变原错误类别，格式修复只处理明确的输入／输出校验失败。

## 10. ComfyUI MCP 媒体接口

### 10.1 实际应用入口

| 入口 | 输入与行为 |
|---|---|
| MediaConfig.load / save | 按项目读取／保存 base_url、workflows、selected_workflow_id；每项包含 workflow_id、name、graph。读取旧配置时丢弃未使用的 image_workflow 字段 |
| parse_video_workflow(graph) | 从已保存的 API JSON 适配有效图片、提示词、参数和唯一 SaveVideo |
| MediaRepository.import_image(path, source=None) | 导入 PNG／JPEG／WebP，保存独立文件、哈希、原文件名及来源；供手动导图与后续图片工作流共用 |
| MediaService.prepare_video(images, prompt, workflow_id=None, parameters=None, target=None, request_id=None) | images 为 LoadImage 节点标识到 image_id 的映射，parameters 为参数键到类型值；只读校验并冻结输入与可选目标镜头；不提交 GPU 工作 |
| MediaWork(output_dir, command) | 符合 VideoTask 调用约定，通过现有 TaskRunner 执行并报告状态 |
| MediaWork(output_dir, job_id=...) | 查询同一远程任务或重新取回结果，不再次提交 |
| MediaRepository.images / jobs / image / job | 读取本地图片与任务记录；读取不创建媒体目录 |

无需先生成分镜或完整 PromptBundle。target 可记录 storyboard_version_id 与 shot_id，不作为生成的前置条件；实际提示词来自 UI 中单独可编辑的视频文字。普通聊天、文字编辑保存和文字 Agent Loop 不触发媒体生成。

后续图片工作流取回图片后可调用 import_image(path, source={kind: image_workflow, job_id: ...})，返回与手动导入相同的 image_id 与文件记录。本轮没有图片生成、图片工作流提交或空实现的生成方法。

### 10.2 工作流与 MCP 工具

MediaConfig.put_workflow(name, api_json, workflow_id=None) 校验名称、解析 JSON 并验证支持的模板；新增时生成标识，更新时保留标识。save 将整个列表原子写入项目；旧 video_workflow 路径配置在读取时转为 JSON 快照，不覆盖旧任务。

parse_video_workflow(graph) 返回 VideoWorkflow：有效 graph、image_inputs、output_node、prompt_node／prompt_input、parameters 与 kind。参考图按 ref_images.ref_image_N 的数字顺序显示，首尾帧可来自 MiniMaxH3ImageToVideo.first_frame／last_frame，也可来自参考图条件后串联的两个 MiniMaxH3AddGuide。后一结构必须按 frame_idx = 0、-1 依次连接，latent 均来自同一参考图生成节点；新增对应图片输入。图片均直接连接独立 LoadImage。支持一个 SaveVideo，不推测未知节点映射。ImageInput.role 分别为 reference、first_frame、last_frame；VideoWorkflow.reference_count 只计算外观参考图片，不把首尾帧计入 Picture N 编号。kind 为 reference、first_last 或 reference_first_last。

WorkflowParameter 包含 node_id、input_name、label、default、minimum、maximum；key 为 node_id.input_name。当前公开时长（PrimitiveFloat 接入帧数换算）、随机种子（RandomNoise）、像素量（ResolutionSelector）、采样步数（PrimitiveInt）及参考图加速开关（PrimitiveBoolean）。像素量下限为当前 ResolutionSelector 允许的 0.1；保留模板的帧率与时长计算关系，不单独暴露 fps。大整数种子使用文本控件读取为 Python int，布尔值不转字符串。

VideoWorkflow.bind(image_names, prompt, job_id, parameters) 核对完整图片集合、参数键、数值类型及范围，再复制模板并替换值。参考图提示词中 Picture N 必须在 reference_count 范围内，组合模板的五个输入中仅前三个对应 Picture 1～3。后端逐张上传，以返回的 subfolder/name 替换对应 LoadImage.image，不向 ComfyUI 发送桌面绝对路径；原配置 JSON 不修改。

内置 MCPServer 使用官方 Python SDK 2.x，与 Client 在进程内连接；不占端口，不启动子进程。服务端工具由本项目实现，名称并非 ComfyUI 或 MCP 的内置标准：

| 工具 | 对应能力 |
|---|---|
| comfy_status | 读取 system_stats 与 object_info，检查服务器、节点及模型选项，不运行生成 |
| comfy_upload_image(image_id) | 从项目读取并核对图片哈希，上传到 ComfyUI input/NovelWriter，返回实际图片引用 |
| comfy_submit_workflow(workflow, request_id) | POST /prompt 一次，extra_data 记录 novelwriter_request_id 以便丢失响应后对账 |
| comfy_get_job(remote_job_id) | 查询指定历史任务或队列，返回运行／排队／未知状态及已完成结果 |
| comfy_find_request(request_id) | 在队列及历史中寻找请求标记，不创建任务；找不到不视为可重新提交 |
| comfy_download_video(job_id, output, index) | GET /view 取回指定视频，检查文件头并原子保存，返回文件和哈希 |

工具调用检查 is_error 与 structured_content。内置桥接在协议边界保留原始异常对象，客户端在正常关闭 SDK 连接后传播原异常，避免内部 TypeError 等被隐藏为普通工具失败或 ExceptionGroup。后台任务边界统一记录完整回溯，UI 展示类型和消息，不自动重试。

### 10.3 实际数据与文件

MediaCommand 保存 request_id、base_url、workflow_id、workflow_name、images（节点标识到图片记录）、parameters、prompt、target、workflow、workflow_hash 及 output_node。此处均为项目契约中的 JSON 对象；内部字段由 prepare 构造，不由用户填写。

MediaJob 保存 job_id、request_id、command_hash、input_snapshot、status、remote_job_id、created_at／updated_at、outputs；提交前保存 submitted_workflow，失败另记 error.type/message。相同 request_id 与相同命令复用已有任务，不同输入报冲突；主动重新生成使用新请求编号。

ImageRecord 保存 image_id、name、relative_path、sha256、created_at、source。视频输出保存 relative_path、sha256、size、remote_output_ref 及服务提供的 metadata；未返回的尺寸或时长不伪造。

```text
short_video/media/
  settings.json
  images/<image_id>.png|jpg|webp
  images/<image_id>.json
  jobs/<job_id>.json
  videos/<job_id>_<index>.mp4|mov|webm|mkv
```

文件先写临时文件再原子改名，数据路径限制在项目媒体目录内；视频只接受支持的容器和对应文件头，不将空文件、网页或 GIF 冒充视频。独立文件不会覆盖其他任务产物。媒体没有单独的 current 指针或 index.json，界面按当前工作流及全部图片输入展示最新任务；文字 schema 仍为 2。

### 10.4 状态与恢复

正常状态：prepared → submitting → queued/running → collecting → succeeded。失败为 failed；提交结果或远程状态待核实为 unknown；本地等待中断为 interrupted，未提交即停止为 cancelled。

执行前读取服务、节点和可选模型名称，检查 required／optional 中的列表枚举、COMBO.options 和数值 min／max；配置错误、模型缺失、图片哈希变化不会提交。提交响应丢失不自动重发，查询通过请求标记找到原任务后继续跟踪；找不到保持 unknown。下载失败保持 collecting 和远程任务编号，下次仅查询与取回结果。已成功请求重放直接返回本地记录。

停止不发送全局 interrupt，也不谎称远程已取消。重新打开不自动提交或续跑。HTTP 单次网络超时为连接 5 秒、读写等操作 60 秒；不以此限制 GPU 总执行时间，不新增预算和任务总时限。

### 10.5 导出边界

ExportService 继续只负责 Markdown 与文字来源清单。媒体直接保存到项目，UI 提供播放及打开视频位置。暂不实现批量视频队列、自动剪辑、媒体打包导出或多图合成。
