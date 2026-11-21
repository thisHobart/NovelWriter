# 网文视频推广工作台：接口设计

文档版本：2.24；日期：2026-11-20。本文维护实际调用入口、数据模型与错误契约。

关联：[架构设计](architecture.md)、[交互设计](interaction.md)、[开发文档](development.md)。第 1～9 节描述已有本地 Python 文字服务；第 10 节记录已实现的多工作流视频服务和内置 MCP 工具；第11节记录已实现的可选 Images API 资产生图。

## 1. 用户动作对应接口

| 动作 | 当前实际入口 | 行为 |
|---|---|---|
| 保存原文与配置 | ProjectService.save_inputs(config, source, ranges=None, *, expected_revision) | 同一次业务变更保存配置、原文与选区 |
| 保存稿件／提示词 | ProjectRepository.save(kind, content, dependencies, *, expected_revision, ...) | 均替换当前内容并删除旧记录和失效下游，不启动 AI 检查 |
| 复制分镜文本 | ArtifactPage.copy_storyboard_text → render_storyboard_text(content, catalog) | 使用当前编辑器内容及该稿精确依赖的目录，复制整段文字，不保存或调用模型 |
| 单阶段生成／重新生成 | WorkflowService.prepare(stage, ...) → WorkspaceWork → execute(command, ...) | 冻结输入并执行生成、检查、有限修改和保存 |
| 生成分镜 | prepare_storyboard_task → CreationWork | 先 asset_catalog，再 storyboard，分别执行质量修复循环 |
| 生成资产提示词 | prepare_prompt_task → CreationWork | 按待生成实体／状态执行 asset_spec，最后本地编译镜头提示词 |
| 查看镜头所需参考资产 | WorkflowService.shot_assets(shot_id, *, state=None) | 返回本镜场景、人物、道具及各状态视图的提示词，只读、不调用模型 |
| 优化当前镜头提示词 | prepare("shot_prompts", mode="revise", target_id=..., asks=..., shot_ids=...) → WorkspaceWork | 保留其他镜头内容，执行单阶段循环 |
| AI 编写视频提示词 | WorkflowService.prepare_video_prompt(shot_id, images, *, workflow_id, parameters, current_prompt="") → WorkspaceWork | video_prompt 阶段只更新所选镜头的完整视频正文，沿用独立审阅和检查点恢复 |
| 用当前稿继续 | ArtifactPage.continue_creation → Workbench.start_creation | 先保存可见稿，再准备下阶段；保存失败不请求模型 |
| 继续未完成任务 | get_recovery(repository, stage) → ResumeWork(repository, task_id) | 校验原任务输入与目标，从检查点继续；不新建生成任务 |
| 查看／停止 | repository.load()["runs"] / 所选通道 runner.cancel_current() | 页面读取状态，CancelToken 传播停止请求；保留已落盘内容 |
| 导出 | ExportService.snapshot / export(destination, *, keys, allow_partial=False, overwrite=False) | 仅接受 .md，其他扩展名返回 INVALID_INPUT；本地验证并输出所选内容，不以 confirmation 为门槛 |
| 聊天讨论 | ConversationService.prepare / preview / execute；chat_attachment(workbench, page, question) | 用户消息、有效历史及当前页面的只读附带内容（分镜、原文、改写、人物／场景／道具提示词、镜头视频设置）；回复供手动修改，不自动写回；保留项目隔离和手动重试 |
| 聊天修改多个镜头 | ChatPanel.send_edit(items) → ConversationService.prepare(..., storyboard_edit=...) → ChatWork；ShotEditPanel.apply / undo_apply | 回复中的 affected_shots 清单选镜，revised_shots 逐镜检查后由用户应用到编辑稿（未保存），可撤销 |
| 镜头检查行 | feedback.record_check(repository, record, state, *, shot_index=None) → {status, summary, issues, stale} | status 为 passed／issues／other_shots／incomplete／unchecked；按 target_path 的 shots[i] 筛出本镜问题，issues 是可读的问题加建议；stale 为已更新的来源名称。record_message 保持原有一行文字 |

表中省略号表示省略可选参数，不是待开发 API；实际签名以对应源码为准。原语义占位接口 save_content／generate／continue_from_current／get_run／cancel_run 不作为可调用方法。confirm/rebind、UI 手动检查与 human 记录、目录确认、单独编译确认均不在当前界面路径中。必要校验、关系解析和编译由内部调用完成。

同请求 ID 同内容复用已保存结果；同键不同内容报冲突。请求结果不明时显式继续可能重发，不能承诺远端只执行一次。来源 ID、revision、内容引用和运行记录由应用层维护，不要求用户填写。运行中用户保存人工修改时，相应旧任务失效并清理未提交候选，保留人工当前内容。

## 2. 内部生成与检查契约

`WorkflowService(..., max_retries=1).prepare(...)` 冻结质量修订上限（仅允许0或1），`execute(command, *, token=None, report=None, on_request=None, on_context=None)` 执行 Agent Loop。首次生成不计入修订；默认最多生成两稿、审阅两轮，格式修复请求另计。首次通过立即采用；否则反馈给生成模型修订一次，再交给独立校验模型，之后采用最新合法稿件。每稿生成回复和每轮审阅回复各允许一次格式反馈修复，与质量修改分别计数；没有总请求次数或整轮时限。`ScriptLLM(model, *, token=None, record=None, transport=None, purpose="generation", network_retries=None, before_request=None, on_retry=None)` 默认最多两次网络重试，共最多三次尝试。连接／超时、HTTP 408／409／429／5xx 可重试；认证、权限、参数、格式、代码及存储错误不进行网络重试，格式错误由Agent Loop显式反馈修复。采用约1秒、2秒的指数退避及少量抖动，尊重 Retry-After／retry-after-ms；服务要求等待超过60秒时停止自动重试，转入现有恢复流程。等待使用取消事件，可立即停止；SDK 内部重试保持0，由本层统一重试以避免叠加。

外部质量检查函数为 `review.check_candidate(command,candidate,send,repository,*,repair=None) -> QualityCheckResult(passed: bool, feedback: str, issue_count: int = 0, issues: list = [])`。command提供阶段、原始要求和必要上下文；candidate为最新合法内容。检查器只执行单次审阅并返回结果，不重新生成候选。Agent Loop在审阅JSON／字段／枚举／引用无效时，将error和原始response存入checkpoint.review_repair，清除待解析回复后，用相同校验API反馈修复一次；修复请求由原审阅任务加本轮错误和回复构建，不递归拼接历史请求。修复计数由检查点中的review_repair表示，恢复不重置；合法审阅完成后清除，下一稿独立计数。`quality_check`注入点继续用于检查替身，不新增服务层。

Agent Loop 对通过结果立即结束；未通过且尚有质量重试次数时，用原始任务／上下文＋最新候选＋本次反馈重新生成；达到上限正常结束并输出最新候选。新生成成功结果的 `review_status` 为 passed（通过）或 issues（检查仍有问题）；审阅网络失败进入可恢复失败，不作为成功结果提交。历史 unavailable／incomplete 状态仅兼容读取。`retries_used` 只记录质量修改生成次数，不包含首次生成与格式修复。结果仍可用现有编辑器保存、继续、导出。

分镜任务包含目录与分镜两个 Agent Loop；资产任务逐项运行，不按累计调用次数或耗时停止。新任务不含 max_calls、seconds；calls_used 仅记录调用次数，历史限制字段不参与执行判断。长文分段计划已移除，旧项目 plans/ 目录中的计划文件不再读取或继续。

文字执行交由 runtime 管理，`WorkflowService.execute` 新增内部 `task_id=None` 和 `resume=False`：独立阶段持有生成锁并创建任务，组合阶段传入父任务编号并共享顶层锁。`prepare` 新增可选 `frozen_rules`，分镜组合任务从目录生成前就冻结分镜模板与 Skill 正文；资产任务冻结整批共用的阶段要求。恢复不得重新读取新 Skill 正文替换旧请求。

- `ResumeWork(repository, task_id, *, transport=None)`：符合 VideoTask 契约，继续指定任务；任务选择不按资产名称或最近文件猜测。
- `get_recovery(repository, stage)`：返回同类最近任务的编号、状态、有效性、完成数、对象、阶段、结果不明提示或失效原因；已成功任务不提供恢复入口。
- `ProjectRepository.recover()`：在生成锁内识别当前项目任务的 interrupted 和 unknown、清理失效检查点；不扫描或迁移独立任务文件，不发送模型请求。

统一清单 `tasks[id]` 保存任务命令、输入与目标 expected 指针、阶段规则、已完成输出、调用次数及当前项目命令；`runs[].checkpoint` 保存 `step=request|validate|review|decide|commit`、原阶段命令、最新候选引用、待处理 response、审阅反馈和格式／质量次数；冻结命令和检查点均校验指纹。任务状态为 running、paused、interrupted、failed、succeeded、invalidated，resumable 单独表示是否允许继续。仅 pending 检查点保存必要正文，结束后移除；不恢复历史候选接口。

调用元数据包含调用编号、逻辑 step_id、purpose=generation|review、实际使用的 model_snapshot 和 sending／received／unknown／failed。发送前落盘 sending，完整回复与下一步骤同步落盘；429等明确服务错误记 failed，无完整确认的网络结果记 unknown。每次物理请求前调用 before_request，分别记录调用与次数；网络失败后 on_retry 提供错误、重试序号、上限和等待秒数。网络自动重试与格式／质量修订分开计数；网络重试耗尽后，用户可继续同一步骤，格式／质量次数不重置。重试反馈保留实际阶段、部分完成数量和网络重试次数；耗尽后裸 raise 传播最后的原始异常。写盘失败停止后续调用，并保留原异常及必要 cause。

任务提交同时核对输入和人工目标，原子更新业务当前指针、run 输出与父任务完成记录。原子事务内同步修正自身清除的下游引用；外部变化返回 TARGET_VERSION_CONFLICT，旧结果被替换返回 OUTPUT_REPLACED。组合任务末尾编排使用同一事务接口，并把 prompt_bundle 加入任务输出。

TaskContext 包含 `resumable` 与当前请求的 `network_retries`；其余 `action,target,phase,completed,total,saved,remote_status` 保留，ProgressReporter 与成功／失败／取消信号保持。任务状态与候选可恢复性从持久化检查点得出，不能只依赖 saved 标记。审阅限流／断网先有限网络重试，耗尽后停止在审阅处，不再返回 unavailable 并视作完成；质量问题达到修订上限仍返回 issues。聊天及视频任务的取消与远程查询语义保持原样。


### 2.1 生成与校验模型配置

- `ModelConfigDialog(provider, parent=None, *, purpose="generation")` 复用服务商、接口地址、API 密钥、模型及获取列表／测试连接，`purpose="review"` 使用独立配置。
- `api_settings.save_provider(..., purpose="review")` 写入 `llm_settings.json.review={provider, providers:{服务商:{base_url,api_key,model}}}`；原 `providers` 继续用于生成与聊天，互不覆盖。
- `review_provider()` 读取校验所选服务；`get_review_config(provider=None)` 只读取显式校验配置，不回退到生成或环境变量。缺失时 `prepare` 返回 `REVIEW_CONFIGURATION_MISSING`，不发送生成请求。
- 阶段命令与组合父任务冻结 `review_model_snapshot={provider, model, endpoint_hash}`；密钥不写任务，允许更新密钥。恢复／每次实际请求前校验当前模型及接口指纹；变化返回 `REVIEW_CONFIGURATION_CHANGED`，可恢复原配置继续或重新生成。
- `ScriptLLM(..., purpose="review")` 使用校验配置，处理quality_check及审阅格式反馈修复；初稿、生成格式修复与唯一一次质量修订使用生成配置。调用记录的format_attempt分别表示相应用途的0／1次格式修复。聊天使用生成配置，不进入校验循环。
- `runs[].snapshot` 保存两份模型快照；`runs[].review_result={passed, feedback, issue_count, issues, candidate_id}` 保存最终完整校验结果及具体字段／原因／建议，绑定实际采用稿。UI 继续使用短摘要，不新增报告页面。
- 运行格式为2；旧格式检查点通过现有失效机制提示重新生成，已保存当前稿不变。

### 2.2 聊天与页面附带内容

- `ConversationService.prepare(conversation_id, user_text, *, request_id=None, attachment=None, storyboard_edit=None)`：只读预检；读取会话、选择已完成轮次、加入本轮临时消息和可选的附带资料，调用上下文纯函数；冻结实际历史 ID、context_hash、附带标注（`storyboard_reference`／`storyboard_edit`／`attached`）、项目与会话修订号、模型快照和请求，不提前落盘 pending。`preview(conversation_id, user_text, *, attachment=None)` 返回同样组装的 `ConversationContext`，不保存也不发送；输入框为空时用“（输入框里的消息）”占位。
- `execute(command, *, token=None, report=None, on_context=None)`：生成锁和 request_id 去重后，保存本轮用户消息，再发送 1 次请求；保存最终回复和运行终态。保存失败、配置变化或修订冲突不继续模型请求。控制台跟踪（NOVELWRITER_LLM_TRACE）的请求头带 `storyboard_reference` 和 `attached`。
- 附带资料 `attachment={page, label, material, notes, edit}`：page 为 storyboard／source／novel_copy／video_copy／asset_prompts／shot_video（`page_material.ATTACHING_PAGES`），label 是界面和历史里的标注，material 是给模型读的文字，notes 是省略或缺失的说明；只有分镜的另带 `reference`（选中镜头）与 `edit`／`edit_shot_ids`（按清单修改）。`validate_attachment` 拒绝其他页面带 reference 或 edit。
- `ConversationStore` 是唯一会话文件读写入口；保存 user／assistant 文本和状态。用户消息可带 `storyboard_reference={shot_id, storyboard_id, order, unsaved, shot_ids?}`（shot_ids 为发送时的镜头顺序），修改镜头的请求另带 `storyboard_edit={shot_ids, base}`（base 为各镜请求时的内容指纹，不含序号与时间轴），其他页面带 `attached={page, label}`（label 不超过 80 字，`validate_attached` 检查）；不将完整资料写入消息正文，也不保存检查反馈或候选。
- 修改镜头：`storyboard_edit={shots: 编辑稿中所选镜头, entities: 本版资产目录}`，须与附带资料的 `edit_shot_ids` 一致；命令另带 `edit_check`（含发送时的镜头顺序）供回复检查。`execute` 收到回复后用 `reply_problems` 检查 revised_shots，有问题时作为同一对话的下一轮再请求 1 次（原请求 + 上次回复 + `repair_request` 列出的问题），修正后通过的镜头更多（或一样多而问题更少）才采用，否则保存第一次回复。
- [storyboard_edits](../../core/short_video/v2/storyboard_edits.py)：`reply_blocks`／`display_text` 读取并隐藏回复末尾的 ```affected_shots``` 与 ```revised_shots``` JSON 代码块；`history_text` 在之后的请求里把代码块换成一句说明（如“〔列出了受影响的镜头：第 2–4 镜〕”）。`affected_shots(content, shots, order=None)` 按“镜号”对应编辑稿（order 为发送时的镜头顺序；旧回复按 shot_id），丢弃不存在与重复的镜头，每项 `{shot_id, order, fields, change}`，“建议时长”（秒）或旧格式的 duration_ms 转成 `duration_ms`（取整后至少 100 的毫秒数，其他值忽略）；`revised_shots(content, base, entities, order=None)` 读取镜号、景别、镜头运动、场景、人物、道具、动作、对白、旁白、音效、时长、画面提示词，场景／人物／道具和说话人按“名称（状态）”对应资产目录（名称按 `asset_names`，同名对象写“寻井人·2”，也认别名），对不上时给出原因；旧格式（shot_id、英文字段与目录编号）要求 EDIT_FIELDS 全部字段，`merge_shot` 保留 shot_id、原文与视频块引用，丢弃编辑中的自由文字，画面提示词改变时删除 visual_description，再用 `validation.validate_shot` 与正整数时长检查；`shot_changes` 给出（列名，原来，改成）；`shot_fingerprint` 忽略 order／start_ms／end_ms。`StoryboardEditor.apply_shots(shots)` 按 shot_id 替换并标记底色，同时去掉这些镜头“画面提示词可能需要同步”的标记；`restore(content, marked)` 用于撤销；`set_content` 清除两种标记，并用 `remove_appended_tails` 去掉旧版程序接在画面提示词末尾的文字（有改动时发出 changed，等用户保存）。
- `build_conversation_context(history, current_message_id, *, system_rules=DEFAULT_SYSTEM_RULES, max_context_chars=48000, attachment=None)` 是纯函数，返回 `ConversationContext(request, messages, omitted_message_ids, shortened_reply_ids, omitted_reply_ids)`：system 消息按通用要求、本页要求（`PAGE_RULES[page]`）、输出格式（只有分镜）分节，之后是交替的历史轮次，最后的 user 消息为附带资料 + “【我的问题】” + 问题；历史里的用户消息前加“〔当时附带……〕”。48,000 字符默认上限包含规则、历史和本轮附带资料；超出时按较早回复缩成开头（`reply_opening`）、省略较早回复、最近一轮回复、丢弃最早整轮的顺序裁剪副本，用户消息不删。仍超限返回 CONTEXT_TOO_LARGE，不截断附带资料或问题，不删除磁盘历史。
- 分镜：`StoryboardEditor.draft_snapshot()` 返回界面编辑稿副本，不保存、不调用整份分镜的严格校验。`read_storyboard_material(repository, draft, shot_id, storyboard, *, unsaved=False, question="", edit_items=None)` 只读取本版分镜绑定的资料（不用更新的草稿），再调用纯函数 `storyboard_material(draft, shot_id, storyboard, records, *, unsaved=False, question="", edit_items=None, budget=STORYBOARD_CHARS)`：整份分镜按表单字段写成文字（`shot_text`），附全片合计与时长约束（`TOTAL_LIMIT_MS`=60000、`USUAL_SHOT_MS`=[5000, 8000]、`MODEL_SHOT_MS`=[4000, 15000]，常量在 `validation.py`）、当前选中与消息提到的镜号（`mentioned_shots`）、资产目录（名称、状态、别名、仅出声标记，不含设定）、焦点镜头用到的资产基础设定（`SPEC_CHARS` 8,000 字内）、引用的原文和项目要求（`requirement_rows`）；传入 `edit_items=[{shot_id, fields, change, duration_ms?}]` 时另加“本轮要修改的镜头”。超出 budget（20,000 字）时 `fit_shots` 保持焦点镜头及其前后镜完整，其余按 `LEVEL_NOTES` 从最远处逐级缩短并写入 notes。视频推广文案块不附带：旁白已在镜头中，动作概述跨多个镜头，由分镜和原文覆盖。
- 其他页面（[page_material](../../core/short_video/v2/page_material.py)，页面读出纯值后调用）：`source_material(article, ranges, config, *, unsaved)` 为改编范围内的原文（`PAGE_SOURCE_CHARS` 20,000 字封顶）加全部项目要求；`copy_material(stage, rows, config, *, unsaved)` 的 rows 来自 `CopyEditor.form_rows()`，即（名称, 文字）或（块标题, None），项目要求只取 `request_config(stage, …)` 的字段；`asset_prompt_material(spec, entities, current_view, *, unsaved)` 的 spec 为 `PromptEditor.value()`（含正在显示视图的修改）；`shot_video_material(shots, index, entities, video)` 的 video 来自 `MediaPreview.chat_inputs()`：`{prompt, pictures: [{reference, name}], frames: [{label, name}], settings: [{label, value, role}]}`，提示词里没有对应图片的 <Picture N> 写进 notes。界面入口在 [chat_attachments](../../core/gui/pages/short_video/chat_attachments.py)：`chat_offer(page) -> ChatOffer | None` 只读控件，给出标签、按钮写法和不能附带的原因；`chat_attachment(workbench, page, question="")` 组装上述资料。
- 原文区间限制在绑定选区。镜头的原文坐标由模型写出，常差几个字，`sentence_range(value, start, end, lo, hi)` 将首尾各移到最近的句子边界（句号、问号、叹号、省略号或换行之后，句末右引号／括号归上一句）：向内最多10字，向外最多150字，不越出选区；片段落在一句之内时取整句，150字内无标点时保留原坐标。之后合并重叠或只隔空白的片段，`source_segments` 只含文字，不带原文编号和起止坐标；按完整片段纳入最多6,000字符的原文补充预算。未提供或无法解析的资料显式说明；正在编辑的人物、对白使用原始文本，旧解析值不重复发送。同名角色不猜身份，当前编辑稿优先于历史建议。
- 停止期间完整返回的回复以 cancelled 保存；失败、取消、中断及无完整回复的轮次不进入后续模型历史。“重试上次问题”只填回输入框，必须再次点击发送，并按当前选择重新取得上下文。聊天只调用生成模型，不进入质量检查或自动写回。

消息存取与历史筛选由 [ConversationStore](../../core/short_video/v2/conversation_store.py) 实现，上下文构造见 [conversation_context.py](../../core/short_video/v2/conversation_context.py)。普通聊天使用完整返回的文字，不提供流式 UI；ScriptLLM 把请求的消息列表（system、交替的历史轮次、本轮 user）原样提交，模型 SDK 不另行读取会话文件。

### 2.3 镜头资产只读查询

`WorkflowService.shot_assets(shot_id, *, state=None) -> list[JsonObject]` 按当前分镜的 shot_id 取引用，顺序为场景、人物、道具；同一资产／状态去重，不把仅对白或画外音人物加入出镜资产。资产名称和状态来自分镜绑定的目录。返回项包含 kind、asset_id、variant_id、name、variant_label、spec_id，以及 views（view_id、label、positive_prompt、negative_prompt、available）。

有当前有效 prompt_bundle 时按本镜 asset_refs 中的 spec_version_id 读取准确设定，不回退到其他设定；没有有效编排结果时，读取同资产编号、状态编号及同目录的当前设定。缺失或不对应的设定返回空提示词及预定视图，available=false；无分镜或找不到镜头返回空列表。查询不更新清单、运行、媒体或任何持久化字段。

### 2.4 自动审阅报告的模型输出契约

所有短视频v2自动审阅的模型输出使用公共JSON Schema，`review_schema(kind)`返回独立副本，
`output_contract(kind)`提供同一Schema和带问题的合法示例。普通审阅放入response_contract，
视频审阅放入“输出结构约束”分块；通过提示词提供契约，不新增模型接口参数。

顶层必填issues对象列表，允许空。每项必填rule_id、severity、target_path、explanation、
suggestion；文字必须包含非空白字符，severity仅warning。quote可省略或为文字／null，
source_refs可省略或为引用对象列表；引用必填source_id、start、end。保持附加字段接受方式，
不注入默认值、不转换类型。video_prompt的target_path必须为video_motion_prompt。
仅在结构和业务引用检查通过后，程序添加id／disposition并映射完整镜头集合中的路径。

`parse_json(raw)`解析任意JSON根类型；既有parse_object仍要求对象。审阅输出的根类型错误
归类schema，语法错误归类syntax，原文来源、坐标、选区和逐字引文错误归类reference。
WorkspaceError保留原有错误码；details.category标明类别。schema／reference的errors条目
包含path、rule、message、expected、actual_type、actual_preview；省略数为omitted_count。
schema最多反馈10项、预览最多200字符；syntax沿用行列、偏移及最多80字符的附近片段。

完整诊断由format_error_feedback写入既有review_repair.error字符串，response仍保存原始
回复，不增加检查点字段或重试次数。原始回复只携带一次；每次修复仍携带同一候选、资料
与Schema，要求保留已有判断和建议。结构、引用错误修复后重验，只有合法审阅结果才进入
正文修订或提交。Schema定义错误、磁盘损坏、配置变化与网络异常沿用原异常处理路径。
内部以及人工／代码check_report的存储结构和验证规则不使用该模型输出Schema替换。

## 3. 来源依赖与当前内容

保存时自动维护已知姓名、别名与实体关系；无法可靠判断身份时保留草稿并返回具体歧义。取消人工绑定 UI 不等于删除实体引用校验。

旧 confirmed 状态不再控制下阶段入口或导出。新任务只引用当前有效内容；继续操作不是审批记录。上游被替换后清理旧下游，不保留旧依赖或旧稿沿用入口。

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

配置、原文、选区及下列业务模型均保存在统一 ArtifactRecord 外壳的 content 中；id、kind、key、dependency_refs、content_hash、created_at 属于记录外壳。内容ID只用于绑定与冲突校验，不是历史版本；新记录不含 parent_id，旧记录的多余字段兼容读取但不再用于恢复。生成请求的 config 引用不可变配置记录。

### 5.2 Source、Selection 与 SourceRef

- Source：`id, title, origin(paste/file), original_file_name?, raw_sha256, normalized_text, text_sha256, paragraphs[]`。TXT／MD 默认按 UTF-8／UTF-8 BOM 解码，失败返回 SOURCE_ENCODING_ERROR 并允许选择编码重导；MD 先作为文本保留，不执行 HTML。
- `paragraphs[]`：`paragraph_id, start, end`。来源位置基于这一 Source 的规范化文本：移除 BOM、统一换行为 LF，以 Unicode 码点计数，使用 `[start,end)`；Qt UTF-16 光标位置必须显式转换。
- Selection：`id, source_id, ranges[{start,end}], selected_text_sha256`，范围有效、非空、排序后不重叠。显示选区拼接文本时保留映射，不能用拼接后的偏移冒充原文偏移。
- SourceRef：`source_id, paragraph_id?, start, end`；可选 `quote` 用于展示，提供时必须与对应切片一致。
- 超过模型有效上下文时，返回 INPUT_TOO_LARGE 和统计信息，提示在原文页缩小改编范围；不得静默截断后声称处理全文。长文分段已移除，原文较长时再考虑语义检索等提取方式。

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

资产内容包含 `asset_id,variant_id,views[]`、类型专属设定，以及 style、source_refs、inferred_fields 等描述字段。顶层正负提示词兼容保留，具体提示词位于 views；资产目录绑定在记录外壳 `dependency_refs.asset_catalog`，不要求 content 中另填 catalog_version_id。保存产生对应 kind 的 ArtifactRecord。

| kind | 结构化字段 | 默认视图计划 |
|---|---|---|
| character_spec | age,gender,appearance,hair,makeup,clothes,accessories,period,state,identity_constraints | head_front、body_front、body_side、body_back |
| scene_spec | drama_type,era,location,time,spatial_structure,height_notes,center,left,right,rear,entrances,landmarks,main_light,fill_light,atmosphere,composition | establishing、reverse，默认均必需 |
| prop_spec | name,description,era,size_shape,material,main_color,secondary_color,components,state | front、three_quarter、top，默认均必需 |

每个 view 具有 `view_id,label,camera_view,composition,required,positive_prompt,negative_prompt`；默认视图 required=true，额外视图可在内容中保留，但当前界面不提供视图增删管理。导出已选资产时要求默认必要视图齐全，缺失视图属于结构错误，不能用 allow_partial 绕过；allow_partial 允许跳过尚未生成的内容项。视图属于拍摄方向，variant 属于时期／造型／状态。

### 7.3 ViewPrompt：每个视图的具体提示词

视图内容至少包含 `view_id,label,camera_view,composition,required,positive_prompt,negative_prompt`；生成后由代码补充 `source_refs,template_ref,content_hash,spec_input_hash`，不重写模型生成的视角及正负提示词。视图嵌入 AssetSpec 的 views，不要求独立 id／spec_version_id；设计补充主要由设定的 inferred_fields 表达，不把概念字段当作所有视图的必需字段。

`spec_input_hash` 在内部关联设定与视图文本。设定改变时标记旧提示词需要更新；可确定的本地编译自动执行，需要模型生成的部分随下一次生成任务处理。导出仅接受当前有效来源，已被替换的内容不能恢复或沿用。内部哈希不能由界面或聊天直接改写，保存采用新文件原子替换，提交后删除旧文件。

人物四视图共享身份和服装设定，只有视角／构图不同；场景角度共享布局；道具角度共享结构与状态。source_refs 表示原文依据，inferred_fields 表达设计补充，不建立逐项审批记录。

模型输出应为具体文字。asset_spec 必须包含当前类型的全部默认视图。生成专用校验拒绝缺失／重复／新增的视图编号、空字段、`{age}`、`【道具名称】` 等未填变量，沿用有限格式修复；无法形成合法候选则保留原稿。手动保存与历史读取的契约不收紧。资产设定和全部视图由同一次生成维护，已删除无调用方的单独视图优化及旧模板编译入口。导出时已选资产仍须通过本地结构和完整性检查。正负提示词分别复制，不触发任何外部图片／视频请求。

## 8. 分镜契约与时间轴

Storyboard 内容为 `shots[]`；视频文案和资产目录绑定在记录外壳 `dependency_refs.video_copy`／`asset_catalog`。每镜字段如下。

| 字段 | 类型与规则 | 界面显示 |
|---|---|---|
| shot_id / order | 稳定 ID；order 连续正整数 | 镜号：按当前顺序显示，改顺序不改 ID |
| duration_ms | 正整数 | 时长列（如“8 秒”），短于 4 秒或超过 15 秒标红、不在 5–8 秒内标黄；表单 0.5 秒一档，可设 1–15 秒 |
| start_ms / end_ms | 由代码按 order 累计，不让模型随意填写 | 时长格提示与表单标题里的时间段 `[起点,终点)` |
| shot_size | 远景／全景／中景／近景／特写／主观视角／自定义 | 景别列与表单 |
| camera_movement | string | 表单“镜头运动” |
| scene_ref | asset_id,variant_id；有效场景 | 表单只读显示场景名 |
| characters | 人物引用数组，允许空 | 人物列与表单：显示名字 |
| props | 道具引用数组，允许空 | 表单只读显示道具名 |
| action | 非空 string | 动作列（只显示一行）与表单 |
| dialogue / narration | 对白数组及 string；允许空 | 表单“对白（含旁白）”每行一条，以“人物：”“旁白：”区分 |
| sound | string，允许空 | 表单“音效” |
| visual_description | 可选 string；表单改画面提示词、合并镜头、应用 AI 改过的画面提示词时删除；打开分镜时画面提示词恰好等于它加旧版程序接上的“。景别，镜头运动。动作：……”，就把画面提示词还原为它并删除（未保存修改） | 不显示；复制分镜文本时与动作、画面提示词去重后一起使用 |
| visual_prompt_draft | 非空 string | 表单“画面提示词” |
| video_block_refs / source_refs | 视频块引用与原文引用 | 元数据，可展开来源 |
| duration_exception_reason | string 或 null | 超出建议区间时可空，不作为继续操作的门槛，作为 warning 处置 |

第一镜 start_ms=0，end_ms=start_ms+duration_ms，下一镜 start_ms=前镜 end_ms。没有重叠、负值和缝隙；镜头拆分、合并、排序后全部重新计算。时间轴由 validate_content／recalculate_timeline 根据时长重算，不信任模型提供的 start_ms／end_ms；当前没有单独的时间轴解析报告。

storyboard 的 prepare 根据当前配置 storyboard_skill_id 明确选择性加载[短视频内容分镜 Skill](../../core/short_video/v2/skills/short-video-content-storyboard/SKILL.md)，将ID和完整文本冻结到 command.requirements.skill_id／instruction，检查与重试使用该快照，不重新读取文件或选择 Skill。缺失／null 时不加载，基础提示和审阅也不隐性包含 H3／5–8秒规则。其他阶段 requirements.skill_id=null，即使项目已选 Skill 也不注入其正文；普通聊天亦不使用。

选中该 Skill 后，H3 常规镜头按5–8秒设计，必要短镜可低于5秒，超过8秒优先拆镜，旧区间仅供参考。duration_ms 仍是计划时长，低于工作流范围需后续裁切，当前未自动裁切。结构字段、模型参数和人工检查入口不变。

分镜需覆盖已保存视频文案的有效内容；镜头中无人、静物特写是合法情况。人物对白说话者必须在本镜出场或标记 voice_over；旁白不创建虚构出镜人物。

PromptBundle：`storyboard_version_id,asset_spec_refs[],shots[{shot_id,resolved_prompt,negative_prompt,video_motion_prompt?,video_prompt_binding?,asset_refs,source_refs}]`。编译时验证资产设定版本和变体，无法解析的引用返回具体内容问题；已生成文字仍可按所选范围导出并说明缺项。video_motion_prompt 由视频面板“AI 编写”生成完整视频正文；video_prompt_binding 由代码记录工作流指纹、实际图片与角色／编号／来源、文件哈希及视频时长。文字编排和视频提示词生成都不提交视频模型；提交仍由独立 MediaWork 执行。

PromptPage 的镜头页只将 storyboard_version_id 与当前分镜匹配的 PromptBundle 作为完整可编辑结果。缺少该结果时，在内存中将当前分镜的 visual_prompt_draft 显示为只读、可复制的画面草稿，负面提示词留空；不写入 PromptBundle，不作为完整提示词导出。状态显示缺少的资产设定数，生成按钮复用 start_creation("prompts") 补齐资产并编排；用户查看或切换草稿不触发模型请求和项目写入。

原文、配置、选区、文案、目录、分镜、阶段模板及各项提示词均按内容键只保留当前一份；人物／场景／道具按 asset_id＋variant_id 分别保存。删除 versions／select 接口和 parent_id 参数。替换后递归清理依赖旧记录的下游及文件；is_current 对所有内容核对当前指针与完整来源链。未完成运行可保留一个最新临时候选与必要检查点；停止或可恢复失败后继续使用，成功、失效或被替代后清理。冲突及停止均不覆盖已有当前内容。


## 9. Prompt 契约：保留用户原意，结构化封装

### 分镜对外交付格式

`storyboard_text.render_storyboard_text(content, catalog) -> str` 是无 I/O 的文本编排函数；输入为结构化分镜 content 和对应资产目录 content。重算连续时间轴，以整数毫秒转为精确秒数，按“视觉风格／场景概述／分镜／镜头要求／音频／限制”输出整段中文，不显示内部编号和来源坐标。

时间段、镜号、景别、运镜、人物与状态、场景、道具、画面、动作及各镜音频均保留。无出镜人物与画外对白分开表达，未指定音频不补写音乐。画面一项由可选 visual_description、动作和 visual_prompt_draft 去重后依次组成，缺 visual_description 时只用后两项。不会调用模型重写或改变保存数据。

分镜页复制包含未保存编辑；导出仍先处理编辑保存，再建立不可变快照。Markdown 导出复用同一编排函数，按 storyboard 的 asset_catalog 依赖读取精确目录，避免错误使用当前另一版目录。文件导出仅支持 .md，继续生成 .manifest.json 来源清单；项目 schema_version 仍为 2。

模板库以阶段区分 instruction；自定义模板保存为当前 prompt_template 内容（content: stage,instruction），替换后清理旧记录，并继续优先，默认模板引用为阶段 default-v3，小说推文与视频推广文案改写后为 default-v4；版本号随默认任务文字更新，旧版本运行中的任务恢复时提示重新生成。任务冻结完整Prompt、输入、模型与所选Skill；旧运行快照与稿件不改。Prompt由共享事实／格式规则、阶段任务、专属约束、只读资料、独立演示和结构样例组成。演示与当前素材隔离，不能混入实际人物与事件。

### 9.1 第一阶段：小说推文

默认任务：把原文改写成观众不看原书也能听懂的一条推文。先定一句话能说清的主事件，只留必要人物、动机、行动、因果与已有结果；开头一两句直接交代处境、异常、冲突或关键动作，不用空泛口播套话；正文一段一个推进，背景首次需要时一句带过，称呼统一；口语短句；结尾交代结果或落点，不加评论、说教或预告，不强制悬念、反转或笑点。未指定 target_words 时一般3–6段、约300字以内；人称、语气、必须与禁止内容按配置。

输入为原文选区与配置，输出 NovelCopy。请求和 AI 审阅只带配置中的 audience、tone、narrative_person、target_words、required、forbidden、permitted_adaptations；保存的配置保持完整。允许合理外化，不新增关键事实；changes 使用原枚举说明删改依据。素材无结果时保留实际停点并说明缺失，不能编造收束。

### 9.2 第二阶段：视频推广文案

默认任务：把当前小说推文改成看画面、听声音就能懂的视频文案。一块一个视频节拍（一个地点里的一个主要动作或一次信息变化），一般4–8块，推文各段都有承接或删改说明；可见动作写具体可拍的画面，不写镜头术语；旁白只补画面拍不出的时间、前因、心理、关系和数字，提到的人和物在同块画面中出现；对白只留关键短句。开头块直接进入事件，结尾块落在结果画面，不加预告或引导语。未指定目标时长时旁白和对白合计一般不超过约200字，不继续文学润色。

输入为当前小说推文和原文依据，输出 VideoCopy。配置只带 audience、tone、target_duration_ms、required、forbidden、permitted_adaptations。请求资料先放小说推文正文（opening、paragraphs、cliffhanger、unresolved_facts，不带其 changes 和记录外壳字段），再放原文与配置；AI 审阅看到同样精简的资料。各文字阶段发给模型的原文只含 source_id、ranges 和 text，不再重复 source_refs 与 selection_id，程序侧任务仍保留完整记录。少于一分钟；有效 target_duration_ms 按目标规划，未指定时自然结束，超范围目标如实报告。清晰中文声音可从每秒3–4字估算，另考虑停顿、换人和情绪；顺序累计、并行取覆盖时长，不假称已测配音。

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

video_prompt 使用专业 AI 视频导演、电影摄影师和 MiniMax H3 提示词工程师角色。VIDEO_PROMPT_RULES 统一约定“参考图 → 场景与构图 → 分阶段动作 → 摄影机运动 → 光影与风格 → 声音 → 负面约束”，完整中文正文写入所选镜头的 video_motion_prompt，包含声音和负面约束，一次复制。现有 resolved_prompt／negative_prompt、精确引用和其他镜头保持不变。前置为已保存分镜、当前有效镜头提示词及实际选图；不齐时明确提示，不隐式生成前置内容。snapshot_video_inputs 依据工作流实际接线压缩 Picture 编号，首尾帧保持角色名称，校验文件哈希与参数；只有图片来源匹配本镜资产、状态和目录时附加资产名和视图，不按文件名猜测。

新阶段沿用已有 Agent Loop、独立校验模型、一次质量修订及步骤检查点。每次模型请求与提交前再次核对工作流／图片输入；模型输出不能引用未选的 Picture 编号，也不能修改画面文字或其他镜头。UI 仅在当前镜头、选图、工作流及视频时长匹配时回填保存结果；继续按钮也核对这组输入。生成结果落盘后再审阅，中断审阅可直接继续，不重复请求已保存的视频文字。

video_prompt 只使用当前单镜头协议，无专用协议版本标记或旧协议分支，模型仅返回 {shot_id, video_motion_prompt}，程序合并为当前完整 prompt_bundle。入口和恢复按当前本镜资料及预算字段校验，不支持旧任务迁移或旧请求。生成、审阅和修订使用同一本镜事实资料，补充原文按已有引用读取并受预算约束；审阅不携带完整任务或镜头集合。修订重新组装最新正文和本轮反馈，不拼接旧请求。预算随任务冻结，四类模型请求均在发送前检查字符容量。字段、预算和验收细则见[单镜头上下文](video_prompt_context.md)。

review.check_candidate 分别检查故事因果与已有结果、视频声画容量、目录实际对象、分镜执行与衔接、资产多视角、镜头画面及视频文字。合理外化不当作新增关键事实；素材缺结果不得要求编造结局。资产与镜头画面不套用视频文案格式；video_prompt按上述中文视频正文要求检查，复杂动作的必要拆镜建议进入现有 issues，不夹在可复制正文中，不自行拆镜。输出仍为既有 issues，定位具体字段与对应内容，给出可执行修改，不以爆款感或高级感等偏好重写。格式修复仅处理JSON、字段、枚举和引用，不借机改剧情。反馈进入一次质量修订流程，最终完整校验结果保存在 run 中；不新增独立报告文件或图片调用。



## 现有文字实现的格式与错误边界

文字生成入口使用自动 Agent Loop。数据入口只接受当前完整格式：生成模型快照为 provider／model 对象，校验模型另含 endpoint_hash，配置包含必需字段；保存和编译维护视图的 content_hash／spec_input_hash。保留旧 confirmed／reports 字段不等于支持任意旧来源类型、字符串快照或缺失指纹；读取失败明确反馈并保持原文件不变。

业务、界面、文字服务和持久化的共享类型见 core/short_video/v2/contracts.py。文字模型网络异常保留原始类型与信息，不统一包装成 ValueError 或 PROVIDER_UNAVAILABLE；记录失败使用 RecordingError 并保留存储异常 cause。内部字段、类型、逻辑错误原样暴露。运行收尾不改变原错误类别，格式修复只处理明确的输入／输出校验失败。

## 10. ComfyUI MCP 媒体接口

### 10.1 实际应用入口

| 入口 | 输入与行为 |
|---|---|
| MediaConfig.load / save | 按项目读取／保存 base_url、workflows、selected_workflow_id；每项包含 workflow_id、name、graph。读取旧配置时丢弃未使用的 image_workflow 字段 |
| parse_video_workflow(graph) | 从已保存的 API JSON 适配有效图片、提示词、参数和唯一 SaveVideo |
| MediaRepository.import_image(path, source=None) | 导入 PNG／JPEG／WebP，保存独立文件、哈希、原文件名及来源；供手动导图与后续图片工作流共用 |
| MediaService.prepare_video(images, prompt, workflow_id=None, parameters=None, target=None, request_id=None) | images 为图片位置标识到 image_id 或 None 的映射；所有模板过滤 None，按实际选图裁剪参考输入及首尾引导，至少选一张。parameters 为参数键到类型值；只读校验并冻结实际输入，不提交 GPU 工作 |
| MediaWork(output_dir, command) | 符合 VideoTask 调用约定，通过现有 TaskRunner 执行并报告状态 |
| MediaWork(output_dir, job_id=...) | 查询同一远程任务或重新取回结果，不再次提交 |
| MediaRepository.images / jobs / image / job | 读取本地图片与任务记录；读取不创建媒体目录。images 默认不含已从选图列表移除的图片，include_hidden=True 时全部返回；image 始终可读 |
| MediaRepository.set_image_hidden(image_id, hidden) / hidden_images() | 从视频页选图列表移除或恢复图片，写入 hidden_images.json；图片文件与记录不变，资产视图、视频提示词绑定和已提交任务继续使用 |

无需先生成分镜或完整 PromptBundle。target 可记录 storyboard_version_id 与 shot_id，不作为生成的前置条件；实际提示词来自 UI 中单独可编辑的视频文字。普通聊天、文字编辑保存和文字 Agent Loop 不触发媒体生成。

后续图片工作流取回图片后可调用 import_image(path, source={kind: image_workflow, job_id: ...})，返回与手动导入相同的 image_id 与文件记录。当前资产生图通过独立 Images API 实现，复用媒体存储，见第11节；不通过 ComfyUI 图片工作流提交。

### 10.2 工作流与 MCP 工具

MediaConfig.put_workflow(name, api_json, workflow_id=None) 校验名称、解析 JSON 并验证支持的模板；新增时生成标识，更新时保留标识。save 将整个列表原子写入项目；旧 video_workflow 路径配置在读取时转为 JSON 快照，不覆盖旧任务。

parse_video_workflow(graph) 返回 VideoWorkflow：有效 graph、image_inputs、output_node、prompt_node／prompt_input、parameters、kind 与 generator_node。参考图按 ref_images.ref_image_N 的数字顺序显示，首尾帧可来自 MiniMaxH3ImageToVideo.first_frame／last_frame，也可来自参考图条件后的 MiniMaxH3AddGuide。允许仅首帧、仅尾帧或二者组合；同时存在时按 frame_idx = 0、-1 依次连接，latent 均来自同一参考图生成节点。图片均直接连接独立 LoadImage。支持一个 SaveVideo，不推测未知节点映射。ImageInput.role 分别为 reference、first_frame、last_frame；VideoWorkflow.reference_count 只计算外观参考图片，不把首尾帧计入 Picture N 编号。kind 为 reference、first_last 或 reference_first_last。

VideoWorkflow.prepare_reference_inputs(node_ids) 仅用于 kind=reference，接收界面顺序排列的 1～9 个已选位置标识；复制图、清除原参考图连线，为新增位置创建 LoadImage，再依次连接 ref_image_0～N-1，并裁剪未使用依赖。新增标识不能覆盖其他业务节点。prepare_video 只冻结和上传这些实际图片；空位不保留旧文件名，也不作为空图片提交。UI 卡片底部显示固定位置“图片 N”，选图后角标显示实际代称 <Picture M>；M 按已选图片顺序连续编号，与提交顺序一致；清空中间位置导致已有提示词引用错位时，生成前要求核对修改。附加位置与引用关联仅保留在当前工作流的内存草稿，不写入配置。

WorkflowParameter 包含 node_id、input_name、label、default、minimum、maximum；key 为 node_id.input_name。当前公开时长（PrimitiveFloat 接入帧数换算）、随机种子（RandomNoise）、像素量（ResolutionSelector）、采样步数（PrimitiveInt）及参考图加速开关（PrimitiveBoolean）。像素量下限为当前 ResolutionSelector 允许的 0.1；保留模板的帧率与时长计算关系，不单独暴露 fps。大整数种子使用文本控件读取为 Python int，布尔值不转字符串。

VideoWorkflow.prepare_image_inputs(node_ids) 为本次任务保留实际选中的图片；重排参考图连线，旁路未选帧对应的引导并裁剪无用依赖，独立首尾帧节点移除未选图片参数。MediaService 和 AI 视频提示词输入快照共用这一适配，原配置 JSON 不修改。允许只选首尾帧、只选尾帧、只选参考图及混合组合，全部空白则拒绝。

VideoWorkflow.bind(image_names, prompt, job_id, parameters) 核对裁剪后图片集合、参数键、数值类型及范围，再复制模板并替换值。Picture N 只按实际已选参考图从1连续编号；首尾帧使用角色名称，不占用编号，零参考图时任何 Picture 引用均拒绝。后端仅上传实际已选图片，以返回的 subfolder/name 替换对应 LoadImage.image，不向 ComfyUI 发送桌面绝对路径。

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

MediaCommand 保存 request_id、base_url、workflow_id、workflow_name、images（节点标识到图片记录）、parameters、prompt、target、workflow、workflow_hash 及 output_node。target 可含 storyboard_version_id、shot_id 和界面所示 shot_number（从1开始）。此处均为项目契约中的 JSON 对象；内部字段由 prepare 构造，不由用户填写。

MediaJob 保存 job_id、request_id、command_hash、input_snapshot、status、remote_job_id、created_at／updated_at、outputs；提交前保存 submitted_workflow，失败另记 error.type/message。相同 request_id 与相同命令复用已有任务，不同输入报冲突；主动重新生成使用新请求编号。

ImageRecord 保存 image_id、name、relative_path、sha256、created_at、source。视频输出保存 relative_path、sha256、size、remote_output_ref 及服务提供的 metadata；未返回的尺寸或时长不伪造。

```text
short_video/media/
  settings.json
  images/<image_id>.png|jpg|webp
  images/<image_id>.json
  hidden_images.json        # 选图列表中已移除的 image_id
  jobs/<job_id>.json
  videos/2026-11-16_15-30-45_镜头03_a1b2c3d4_01.mp4|mov|webm|mkv
```

文件先写临时文件再原子改名，数据路径限制在项目媒体目录内；视频只接受支持的容器和对应文件头，不将空文件、网页或 GIF 冒充视频。独立文件不会覆盖其他任务产物。媒体没有单独的 current 指针或 index.json，界面按当前工作流及实际图片选择展示最新任务（普通参考图忽略空位）；文字 schema 仍为 2。

新下载视频按“北京时间的任务开始时间_镜头编号_任务ID前8位_结果序号”命名，时间来自冻结的 created_at，镜头编号来自 target.shot_number；没有镜头编号时使用“视频”。结果序号从01开始，同任务重新查询或下载保持同一文件名。已有成功视频不自动重命名，任务内保存的输出路径继续用于定位文件；ComfyUI远端输出名和远端结果引用保持原样。

### 10.4 状态与恢复

正常状态：prepared → submitting → queued/running → collecting → succeeded。失败为 failed；提交结果或远程状态待核实为 unknown；本地等待中断为 interrupted，未提交即停止为 cancelled。

执行前读取服务、节点和可选模型名称，检查 required／optional 中的列表枚举、COMBO.options 和数值 min／max；配置错误、模型缺失、图片哈希变化不会提交。提交响应丢失不自动重发，查询通过请求标记找到原任务后继续跟踪；找不到保持 unknown。下载失败保持 collecting 和远程任务编号，下次仅查询与取回结果。已成功请求重放直接返回本地记录。

停止不发送全局 interrupt，也不谎称远程已取消。重新打开不自动提交或续跑。HTTP 单次网络超时为连接 5 秒、读写等操作 60 秒；不以此限制 GPU 总执行时间，不新增预算和任务总时限。

### 10.5 导出边界

ExportService 继续只负责 Markdown 与文字来源清单。媒体直接保存到项目，UI 保存当前结果路径并提供始终可用的“打开视频位置”，通过 QDesktopServices 打开项目 short_video/media/videos；没有目录时点击创建，不依赖本地结果或任务状态，失败返回具体原因。不加载视频、不创建播放器或音频输出对象。暂不实现批量视频队列、自动剪辑、媒体打包导出或多图合成。

## 11. 可选资产生图接口

| 入口 | 输入与行为 |
|---|---|
| api_settings.get_image_config / save_provider(..., purpose="image") | 独立 image 配置保存 base_url、api_key、model；不回退到文字／校验配置，模型获取只查询列表，不用聊天探测 |
| ImageGenerationService.rows() | 从当前分镜引用按目录／资产／状态／视图去重，返回所用镜头、提示词、可生成原因与当前关联图片；不生成文字或图片 |
| prepare(keys, concurrency=2) | 只读预检，要求勾选完整有效的视图；并行范围1～8，每视图一张；冻结设计、正负及实际请求提示词、模型／接口指纹和输入哈希 |
| ImageGenerationWork(repository, command=None, *, batch_id=None, keys=None, transport=None) | 新批次或继续指定原批次，遵守 VideoTask 契约，一个 TaskRunner 工作内部受限并发；keys 可显式允许重试未知项 |
| ImageGenerationService.recover() | 持有项目生成锁识别中断；有完整响应继续处理，无响应的已发送请求记 unknown；不发网络请求 |
| MediaRepository.save_image(data, *, name, source=None, image_id=None) | 原子保存 PNG／JPEG／WebP 与来源；固定图片编号使响应处理可重放，import_image 复用同一入口 |
| MediaPreview.use_image(image_id, node_id) | 用户明确放入指定现有节点，保留其他选图、视频提示词及已有 Picture 引用检查 |

请求仅使用 model、prompt、n=1，正向正文后附“排除或避免的元素”及负向文字，不假定有 negative_prompt 参数；尺寸和质量使用服务默认。支持 b64_json 或 URL，完整响应先保存，之后下载／解码并验证可读取。模型列表不支持时允许手动填写，缺少生图配置只阻止生图，不阻止文字任务。

生图清单在媒体目录 image_generation.json，含 batches 和 bindings；原子更新并持有媒体锁，不混入文字 tasks／runs 或 ComfyUI jobs。批次与每项冻结输入均校验哈希，响应保存在 image_responses。每项使用固定 image_id，图片 source 保存目录／资产／状态／视图、原设定编号、设计和实际提示词、指纹、批次及模型／接口指纹，不保存密钥。

WorkflowService.shot_assets 的 views 可返回 image（含 image_id、relative_path、source、current 等媒体字段）。匹配使用目录／资产／状态／视图及独立视图提示词、设计指纹；改其他视图不影响当前图片。旧图不删除，变更后 current=false，允许查看和手动使用；原文重建新目录不会静默复用旧图。

每项状态为 queued、requesting、retrying、received、saving、succeeded、failed、unknown、paused、stale；收到响应后先落盘，保存成功后在项目锁和媒体锁内核对来源再更新绑定。来源在执行中变化仍保存已返回文件，但不选为当前图片。一次失败不丢失其他成功项，返回 counts 与 image_summary，不使用 AI 审阅状态。

仅连接建立失败和429有限退避，SDK重试为0；请求发出后超时／断连不自动重发，unknown 须显式勾选，界面提示可能重复计费。缓存响应的下载／解码恢复不要求当前生图配置，也不重发生成；响应缓存写盘失败记 unknown 并停止后续请求。停止不启动排队项，等待已发请求返回保存；TaskRunner 仅对 drain_on_cancel 工作允许取消后的进度回报。恢复跳过 succeeded，默认跳过 unknown；认证／配置失败停止后续排队请求。

## 12. 短视频 UI 三通道调度

TaskChannels 维护 text／image／video 三个 TaskRunner，都由短视频工作区自己持有，不借用小说的任务通道。
channel(work) 按 ImageGenerationWork／MediaWork 分类，其余为文字任务。
admit 在启动前检查同类互斥；同镜头 video_prompt 任务阻止新视频，查询原任务例外。
prompt_busy 按冻结的分镜版本和 shot_id 匹配，恢复任务也在启动前读取原命令的目标。
finish 按工作对象身份清理，旧回调不影响新任务。any_busy 用于项目、模式、配置和退出检查。

AppStatusBar.start_task／update_task／finish_task 绑定通道和工作对象，选择器按通道保留最近结果。
取消按当前选择路由，不通过最后提交任务推断对象。TaskRunner.busy 在主线程处理完结束前保持为真。
以上均为进程内 UI 状态，不增加检查点或媒体任务字段，不改模型协议与恢复次数。
