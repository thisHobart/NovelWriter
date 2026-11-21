# 短视频工作台：开发文档

文档版本：2.24；日期：2026-11-18。本文用于定位代码、维护实现与执行验证。
整体结构见[架构设计](architecture.md)，字段和接口见[接口设计](interfaces.md)，操作与提示见[交互设计](interaction.md)。历次变更及验收证据见[DEVLOG](../../DEVLOG.md)。

## 1. 运行与代码入口

使用 Python 3.11，依赖安装在项目虚拟环境中。启动桌面应用：

```powershell
.\venv\Scripts\python.exe main.py
```

| 维护内容 | 主要代码入口 |
|---|---|
| 工作区、导航与生成入口 | `core/gui/pages/short_video/workbench.py`；`artifact_page.py`、`prompt_page.py`、`source_page.py` |
| 「镜头」标签：镜头号、镜头卡片与检查行 | `shot_header.py`，接入 `prompt_page.py`；检查结果由 `feedback.record_check` 按镜筛选 |
| 「镜头」标签的视频区与选图菜单 | `media_preview.py`（选图菜单读 `workflow.py` 的 shot_assets）；图片卡片、两栏布局、折叠区、开关在 `video_widgets.py` |
| 「与 AI 讨论」面板 | `chat_panel.py`（标题栏、附带标签、输入框、发送／停止）；`chat_attachments.py`（各页附带什么，标签和按钮的写法）；`request_preview.py`（“查看附带内容”和输入框下方的提示）；`chat_stream.py`（消息流、气泡、Markdown）；`shot_edit_panel.py`（回复下的镜头卡片） |
| 后台线程与 UI 状态 | `core/gui/task_runner.py`；`core/short_video/v2/feedback.py` |
| 短视频三通道调度与状态栏 | `core/gui/pages/short_video/task_channels.py`（三个通道和交给状态栏显示的任务说明）；任务的启动、结束和停止在 `workbench.py`；共用状态栏 `core/gui/widgets/status_bar.py` |
| 准备阶段输入、多阶段编排 | `core/short_video/v2/workflow.py`、`creation.py` |
| Agent Loop、检查点与继续执行 | `core/short_video/v2/runtime.py` |
| 项目与内容存储 | `repository.py`、`storage.py`、`project_service.py`、`models.py`、`contracts.py` |
| 生成／校验配置与模型调用 | 地址、密钥等共用配置在 `core/common/api_settings.py`；校验模型和生图 API 配置在 `service_settings.py`；对话框 `core/gui/model_config_dialog.py`；`llm.py` |
| 提示词、校验与审阅 | `prompts.py`、`validation.py`、`review.py`、`storyboard_skill.py` |
| 可选资产生图与来源 | `image_generation.py`、`media_repository.py`；UI 为 `asset_images_page.py`、`asset_image_preview.py` |
| 会话与上下文 | `conversation.py`、`conversation_store.py`、`conversation_context.py`（规则、历史与容量）；附带资料 `storyboard_chat.py`（分镜）、`page_material.py`（原文、改写、提示词）；回复里的清单与修改结果 `storyboard_edits.py` |
| 编排及导出 | `prompt_compiler.py`、`storyboard_text.py`、`exporter.py` |
| 视频工作流与媒体任务 | `media_workflows.py`、`media_service.py`、`media_repository.py`、`comfy_mcp.py`；UI 为 `media_preview.py`、`comfy_settings.py` |

表中省略目录的领域文件均位于 `core/short_video/v2/`，UI 文件位于 `core/gui/pages/short_video/`。

## 2. 文字生成的维护要点

1. `WorkflowService.prepare` 读取当前有效内容，冻结生成与校验模型、来源、模板、要求和目标内容；分镜组合任务在目录生成前冻结后续分镜规则。
2. `CreationWork` 编排目录→分镜，或逐项资产→镜头本地编排；单阶段任务通过 `WorkspaceWork` 执行。
3. `AgentExecution` 执行 request→validate→review→decide→commit；`validation` 保证结构和引用，`review` 返回质量反馈，二者不自行启动新任务。
4. 质量修订默认最多一次：首次校验失败后由生成模型修订，再用独立校验模型复检，第二次残留质量问题只记录、不触发第三稿。每稿生成回复和每轮审阅回复各允许一次格式反馈修复，分别使用生成API和校验API，与质量修订独立计数。审阅修复的错误与原始回复随检查点保存，中断恢复不重置次数，已保存回复不重复请求。ScriptLLM另负责最多两次网络重试，SDK内部重试保持0，避免嵌套重试；不设置整轮调用预算或总时限。

修改内容规则时，阶段任务与样例在 `prompts.py`，硬性约束在 `validation.py`，AI 审阅标准在 `review.py`；保持三者一致。改动某阶段默认任务文字时，在 `prompts.py` 的 `DEFAULT_PROMPT_VERSIONS` 中提升该阶段版本，新稿据此记录所用提示词版本。自定义阶段模板优先，不以更新内置提示词为由覆盖用户模板。

视频提示词的单镜头上下文、局部模型返回、引用读取及统一预算见[上下文设计与验收](video_prompt_context.md)。`video_context.py`负责资料投影与证据预算；`video_request`重新组装每次请求，审阅不携带完整任务或镜头集合。只保留当前任务数据校验，无专用旧协议版本标记、旧生成回退或独立任务文件迁移；独立的镜头画面提示词由分镜的取景范围、运镜、动作及资产设定编排。

视频面板的 VIDEO_PROMPT_RULES 在 stage_prompt 的 video_prompt 分支统一加入，保留自定义任务目标。WorkflowService.prepare_video_prompt 冻结本镜分镜、实际使用的资产及视频输入；video_prompt.py依据真实工作流接线生成 Picture 顺序并验证图片哈希，WorkflowParameter.role="duration"标识时长。完整 H3 中文正文位于 video_motion_prompt，video_prompt_binding由代码维护，resolved_prompt／negative_prompt保持原状。沿用 AgentExecution、独立审阅和检查点，模型每次请求及提交前核对媒体来源。UI按镜头与工作流缓存临时编辑，只有选图及时间匹配时回填保存结果，保持手动编辑；文字生成和ComfyUI提交各自独立。回归见 tests/short_video/test_video_prompt.py。

分镜 Skill 资源为 [short-video-content-storyboard/SKILL.md](../../core/short_video/v2/skills/short-video-content-storyboard/SKILL.md)。仅显式选择时注入分镜，其他阶段及聊天不注入。当前 Skill 除短片总时长、常规5–8秒镜头、动作复杂度与衔接外，还规定镜头顺序与景别、对话正反打与轴线视线、旁白与画面分工、开头结尾，并附一镜写法正反例；具体规则以文件为准。Skill 正文只保留模型可执行的规则；H3 时长依据见 [MiniMax H3 官方模型卡](https://huggingface.co/MiniMaxAI/MiniMax-H3) 和 [官方镜头与首尾帧提示词指南](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md)，官方输出范围为4–15秒，5–8秒是本项目制作策略。

## 3. 检查点、依赖与提交

- 运行数据统一存入 `project_v2.json` 的 tasks／runs；`TaskContext` 仅用于界面反馈。不要将内存进度当作恢复依据，也不要为新任务重新写 tasks/*.json。
- ScriptLLM 的 before_request 在每次实际发送前校验任务并记录独立调用及实际模型用途／快照，防止重试期间输入或校验配置变更；完整回复先落盘；本地解析、审阅与修订分别推进步骤。每个未完成运行仅保留最新候选及当前待处理回复。
- 最终完整校验结果保存在 `run.review_result`，含完整 issues／反馈及最终 candidate_id；不要只存 UI 截断摘要。校验配置显式独立保存，缺失即预检失败，不默认使用生成模型。
- 当前内容指针、run 结果与父任务完成项在同一事务提交；镜头本地编排也须在事务内校验任务，并将产物加入任务输出。
- 网络错误先有限退避重试；停止或重试耗尽后保留可恢复状态；修订次数跨恢复保持。质量修订耗尽仍提交合法候选并标为 issues，格式修复耗尽则失败。
- 恢复先核对输入与人工目标。外部修改使原任务失效；任务自身更新产生的指针变化须同步到预期引用。清理依赖后再次校验任务，避免旧原文残留检查点。
- 普通读取保持只读；中断识别放在持有生成锁的 recover 中，只处理项目清单中的当前任务。当前任务字段缺失时拒绝，不推测执行位置或迁移独立任务文件。

业务内容保持当前一份。内容 ID、哈希和 revision 用于依赖、完整性与并发控制；不要重新加入历史版本菜单或旧稿沿用路径。

镜头资产查询只从当前分镜与准确设定读取，UI 不从正文猜测资产身份。独立 `ShotAssetsPanel` 展示只读列表和可收起详情，与 PromptEditor 编辑状态分离；空提示词不得用其他资产或状态填补。视图选择仅控制查看，不写视频图片选择、业务内容或任务状态，不触发生成。对应回归见 `tests/short_video/test_shot_assets.py`，覆盖同名／不同状态、去重／画外音、缺失／部分视图、准确来源、复制及未保存编辑隔离、视频选择不变、新增／重排和无分镜。

分镜编辑器在 [v2_editor.py](../../core/gui/pages/short_video/v2_editor.py)：`ShotTable` 只读（镜号、时长、取景范围、人物、动作），只管选择、拖动排序和右键菜单；选中镜头的字段由下方表单编辑，表格与表单放在竖向 QSplitter 里。长文字框 `GrowingText` 按 documentLayout 逐块累加当前宽度下的高度，加上边框与文档边距，限制在最少／最多行之间，内容或宽度变化时重算。表单改动直接写回编辑稿并只重填这一行；改时长时重算时间轴，刷新全部行、摘要和标题。时长常量在 `validation.py`：`TOTAL_LIMIT_MS`（成片上限）、`USUAL_SHOT_MS`（分镜 Skill 的常规单镜）、`MODEL_SHOT_MS`（H3 一次能生成的长度），表格着色、摘要和聊天上下文的 `timing` 共用；配置里的 `shot_duration_range_ms` 仍只给审阅 SHOT-003 用。拆分清空后一镜的对白、旁白和音效；新增镜头沿用场景、原文引用与视频块引用（保存校验要求每镜有视频块引用并覆盖全部视频块），其余留空；`value()` 遇到空的必填文字时选中该镜、给表单控件设 `missing` 属性再报错。回归见 `tests/short_video/test_storyboard_editor.py`：只读表格与表单编辑、打开旧分镜时忽略退役字段、拆分、新增与标红、合并（保留输入的对白和人物、旁白多行往返）、时长提示与合计。

## 4. 会话与媒体边界

会话通过 ConversationStore 读写，默认按48,000字符容量构造请求，仅裁剪本轮使用的完整历史副本。预检失败不写 pending；执行时保存本轮消息和完整回复。重试仅填回输入框，用户再次发送；聊天不写回业务文档、不进入质量修订循环。

各页附带什么由 [chat_attachments](../../core/gui/pages/short_video/chat_attachments.py) 决定：`chat_offer(page)` 只看界面控件、不读仓库，给出标签、按钮写法和不能附带的原因，按键时也会调用；`chat_attachment` 在预览和发送时才读取页面与仓库，组装附带资料。附带资料是 `{page, label, material, notes, edit}`，分镜的另带 `reference` 和 `edit_shot_ids`；`material` 是给 AI 读的文字（“【附带资料】……”开头，按“## ”分节，省略或缺失写进“## 资料说明”），不含内部编号。分镜由 `storyboard_chat` 从 `draft_snapshot()` 投影整份分镜（只复制编辑器内容，未填完整的人物／对白不阻断讨论，不调用 `value()` 或保存），超过 `STORYBOARD_CHARS`（20,000 字）时 `fit_shots` 让当前、提到和要修改的镜头及其前后镜保持完整，其余从最远处按 `LEVEL_NOTES` 逐级缩短；目录、设定和原文按该分镜的依赖读取（不附带视频推广文案块），未生成设定也可讨论。其他页面由 [page_material](../../core/short_video/v2/page_material.py) 用页面读出的纯值生成：原文按改编范围（`PAGE_SOURCE_CHARS` 20,000 字封顶），改写按 `CopyEditor.form_rows()`，人物／场景／道具按 `PromptEditor.value()`（含正在显示视图的修改），镜头按 `MediaPreview.chat_inputs()`。项目要求统一由 `requirement_rows` 写成“受众：……”等行，改写只带 `request_config(stage, …)` 里这一步用到的字段。标签由页面的 `changed` 信号、编辑器信号和 `Workbench.refresh_shown` 末尾更新（页面切换时稿子在 `currentChanged` 之后才载入）；隐藏页面不读取。原文补充按有效坐标截取后，首尾各移到最近的句子边界（向内最多10字、向外最多150字，见`source.sentence_range`），重叠或只隔空白的片段合并，只发送文字、不带原文编号和坐标，完整片段最多6,000字符；缺失资料或无法解析的名字明确标记，不回退到其他版本。用户消息只保存轻量标注：分镜为 `storyboard_reference`（含发送时的镜头顺序 `shot_ids`，用来把回复里的镜号对应回镜头），其他页面为 `attached={page, label}`；之后的请求写成“〔当时附带……〕”，完整资料只组装到本轮请求；历史建议不视为已应用。

请求由 `build_conversation_context` 组装：system 消息按“## 通用要求”“## 本页要求”“## 输出格式”分节（`PAGE_RULES` 每个附带页面一段，只有分镜要求输出格式），之后是交替的历史轮次，最后一条 user 消息是附带资料加“【我的问题】”和问题。历史里的 AI 回复由 `history_text` 把代码块换成一句说明；超出容量时按 `_reductions` 的顺序先把较早回复缩成开头（`reply_opening`，第一段最多160字并注明省略字数），再省略较早回复、最近一轮回复，最后丢弃最早整轮，用户消息不删。`ConversationContext` 记录缩短、省略和丢弃的消息，供输入框下方的提示（`context_hint`，输入停顿400毫秒后计算）和“查看附带内容”使用；`ConversationService.preview` 与发送组装同样的请求，但不保存。

修改镜头走两轮聊天：规则要求建议修改镜头时（只改一镜也是）在回复末尾附 affected_shots 代码块，每镜带建议时长 duration_ms，聊天面板的
[ShotEditPanel](../../core/gui/pages/short_video/shot_edit_panel.py)只读最后一条完整回复，按当前编辑稿列出镜头供勾选，作为卡片挂在消息流中这条回复下方（较早回复的卡片由 earlier_card 收成一行）；
建议时长与编辑稿不同时，卡片把“时长”换成“时长 X → Y 秒”；“修改所选镜头”带 `edit_items`（含 duration_ms）重新组装上下文并追加修改规则，规则要求按给出的时长写 revised_shots，回复的 revised_shots 由
[storyboard_edits](../../core/short_video/v2/storyboard_edits.py)逐镜合并、检查，与正式保存共用`validation.validate_shot`。
回复用“镜号”和“名称（状态）”：镜号按发送时的镜头顺序对应回镜头，名称按 `asset_names`（同名对象写“寻井人·2”，也认别名）对应回资产目录，对不上时给出原因；旧格式（shot_id、英文字段和目录编号）仍能读取。
格式修正只在聊天服务内做一次，作为同一对话的下一轮（上次回复 + `repair_request` 列出的问题），不进入阶段任务的审阅循环。应用只改编辑稿并标记底色，保存仍走原有“保存”；
撤销保存应用前的整份编辑稿，只有编辑稿仍等于应用后的样子时可用。用户消息的 `storyboard_edit.base`
记录请求时各镜的内容指纹，用于提示覆盖之后的编辑。

`tests/short_video/test_storyboard_chat.py`覆盖只读取值、未保存与未完成编辑、状态与同名人物、原文补整句与预算、
前后镜、请求冻结和容量、UI标注与预览、普通聊天、失败重试和停止，验证不改写或失效下游。
`tests/short_video/test_page_attachments.py`覆盖原文、改写、提示词、镜头各页附带的内容与标签，项目要求按步骤取字段、长原文截断、各页规则、消息保存的附带标注和之后的“〔当时附带……〕”，以及真实 Workbench 里标签跟着页面和子标签变化、按页取消附带。
`tests/short_video/test_storyboard_chat_edits.py`覆盖受影响镜头清单（含建议时长与卡片里的“时长 X → Y 秒”）、修改轮与格式修正、应用、覆盖确认和撤销，以及规则要求的时长写法、界面名称和单镜也附清单。

视频任务使用独立媒体快照和远程任务编号。修改模板适配时只改本轮 JSON 副本，保留用户保存的工作流、模型和默认参数。普通 H3 参考图支持最多九个位置；所有模板空位跳过，prepare_image_inputs统一裁剪未选参考输入、首尾帧参数与引导依赖。首尾帧可单独选择；Picture编号只对应实际参考图，首尾帧不占编号。MediaService与AI提示词快照共用裁剪逻辑，恢复按实际已选图片匹配。资产视图另可通过独立 Images API 生图，结果进入同一媒体库；不自动提交视频任务。

media_preview.py 不导入 QtMultimedia／QtMultimediaWidgets，也不创建播放器、视频部件或音频输出，不加载视频源或处理播放状态。结果只保留 video_path，“打开视频”交给系统打开文件，“打开视频位置”打开其父目录；生成、查询和下载保存流程继续复用原接口。整页是 VScrollArea：图片卡片（SlotCard／SlotGrid）每张至少148px宽、缩略图16:9，ColumnGrid 在宽度≥680时分两栏（图片横跨上方，提示词与视频卡片同一行；“用 <Picture N>…”和时长提示会换行，两栏最小宽度约620），视频卡片的 SplitBody 在≥560时让状态区与参数并排；提示词至少200px、参数控件至少32px，尺寸与间距使用主题 token。图片选择仍是 image_selection()／image_fields（节点→SlotCard），提示词编号错位由 binding_shifts() 同时驱动即时提示和生成前校验。相关回归在 test_comfyui_video.py，覆盖卡片代称、添加位置后滚动与恢复、打开文件和目录、状态标签、时长提示、选图菜单、小窗口下控件高度，以及提示词与视频卡片同一行且改参数不换位置。

Qt 布局注意：控件加入已显示的父控件后要等排队的显示事件才可见，隐藏期间不计入 heightForWidth，消息流和视频页因此在加入后立即 show 并 reveal 子控件；两栏或并排改排后，各级布局和 QWidgetItem 的尺寸缓存仍是旧值，MediaPreview.relayout 先逐个 updateGeometry／invalidate 再按宽度测高，否则页面底部会留白。移除控件时先 hide 再 deleteLater，不用 setParent(None)：聊天连续重绘两次时（例如分镜页出现后紧接着刷新），刚加入的消息还有排队的显示事件，脱离父控件后会作为独立窗口闪一下（回归见 test_storyboard_chat_edits.py、test_comfyui_video.py 的 watch_windows）；没有父控件的控件也不能先 setVisible。icons.pixmap 按目标尺寸渲染 SVG，旧写法固定按16px绘制，其他尺寸的图标会被裁切（小说页的图标同样受益）。

刷新规则：切换顶部页面（Workbench.navigate）只刷新显示的那一页（refresh_shown；“改写”只刷新当前子标签，切子标签时补刷新），不刷新聊天；保存、任务结束、打开项目走 Workbench.refresh，刷新当前页、顶部、聊天和按钮状态，隐藏页面留到显示时再刷新。离开有未保存修改的页面前一定会 resolve_edits，所以显示出来的页面总是先刷新再用；新代码不要读取隐藏页面的 record 或编辑器内容，需要最新内容时直接读仓库。提示词页的视频区只在“镜头”标签载入（showing_shot_tab 控制 refresh_task_controls 里的视频状态与镜头标记）。两种刷新都在 Workbench.project_reads 里进行：持有项目锁并开启 repository.shared_project_reads，同一线程内所有 ProjectRepository.load 共用一份解析结果，每次取用前比对文件修改时间和大小；transaction 始终自己读取并在写入前丢弃共用副本，其他线程不受影响。共用期间读到的项目状态不能就地修改，需要改动时先 deepcopy。聊天 render_messages 按会话、消息、镜头草稿和“正在回复…”算指纹，与上次相同就不重建。回归在 test_video_page_refresh.py（真实 Workbench，设置写入临时 ini，不碰注册表）。

本次界面调整：54项关联回归和全量420项回归通过，变更生产文件 Ruff 检查与格式通过。离屏 Qt 检查660×900布局可完整显示设置，660×520时可滚动且参数框不压缩；未调用真实媒体生成服务。

提交响应丢失先查询原任务，下载失败只重新取回文件；不自动重提视频任务，也不对共享 ComfyUI 发送全局中断。文字网络错误在共享调用层有限重试，耗尽后原样传播；媒体仍使用其独立提交与查询策略。任务边界记录堆栈，UI 显示操作、对象、阶段和实际原因。

### 4.1 资产生图批次

ImageGenerationService 从已有结果列视图，ImageGenerationWork 单次 asyncio 运行内用 Semaphore 限并发；不为每张图调用 TaskRunner，不使用文字质量修订循环。图片配置仅启动新请求时检查；响应处理无需调用生图模型。同步文件写入保持短事务，后台线程不访问 QWidget，UI 在活动生图期间只读刷新表格。

媒体生图清单维护逐项状态和当前图片绑定，响应先落盘再解码／下载；固定 image_id 保证进程在响应、图片文件或关联提交处退出后可重放。冻结输入校验与实际请求、来源指纹一致，来源变化时不得把旧任务结果绑定到新提示词。关联提交持有项目锁再持有媒体锁，避免与人工编辑竞态。

连接建立失败和429最多重试两次；未知结果、认证、参数及存储错误不自动重发生图。缓存响应写盘失败须记未知，防止恢复时重复付费；图片文件写盘失败从缓存恢复。每项保存，不因单项失败回滚其他图片。停止仅对生图工作 drain_on_cancel 允许继续报告和保存已发送结果，原文字停止语义不变。

测试用真实 SDK＋模拟 HTTP，核对并发上限、两类响应、未知结果、磁盘错误、跨批次恢复、提示词变更、真实退出边界和明确选图；不调用付费 API。新增资源只有 Python 模块，无需新增模型或工作流打包数据。生图配置和图片生成按钮不是文字任务的前置条件。

## 5. 开发与验证

使用显式 transport 替身和临时项目验证文字流程，避免测试调用付费模型或修改用户数据。

```powershell
# 全量回归
.\venv\Scripts\python.exe -m pytest tests -q --tb=short

# 只跑短视频的测试
.\venv\Scripts\python.exe -m pytest tests/short_video -q

# 文字存储、恢复、会话与反馈
.\venv\Scripts\python.exe -m pytest tests/test_video_v2_repository.py tests/short_video/test_video_runtime.py tests/short_video/test_conversation_management.py tests/short_video/test_video_feedback.py tests/short_video/test_video_network_retry.py tests/short_video/test_video_review_model.py tests/short_video/test_shot_assets.py tests/short_video/test_asset_image_generation.py -q

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

### 5.1 控制台查看模型数据流

在启动程序的同一个 PowerShell 中启用文字请求追踪：

```powershell
$env:NOVELWRITER_LLM_TRACE="1"
$env:PYTHONIOENCODING="utf-8"
.\venv\Scripts\python.exe main.py
```

PyCharm 运行配置也可设置 `NOVELWRITER_LLM_TRACE=1` 后重启程序。默认关闭；
停止程序后移除环境变量或设为 `0`，下一次启动不再输出全文。

追踪直接输出控制台，不通过应用的文件日志处理器，也不新增项目内的诊断文件。
按 `task_id`、`run_id`、`call_id` 对照生成和独立校验 API 的调用：

- `REQUEST`：实际发送的完整 user 消息，以及模型、接口、生成参数、网络尝试和格式修复次数。
- `RESPONSE`：原始回复正文，以及服务端返回的 `finish_reason`、usage、refusal；未返回的统计为 null。
- `PARSED_CANDIDATE`：通过本地结构校验后的候选；视频阶段仅显示本镜结果。
- `GENERATION_PARSE_ERROR`、`REVIEW_PARSE_ERROR`：本地校验原因及是否进入一次反馈修复；修复请求的 REQUEST 包含原始错误和回复。
- `PARSED_REVIEW`：解析后的问题、反馈及审阅结果；`COMMITTED_RESULT` 表示最终保存的版本。
- `API_ERROR` 为调用异常；`REUSED_RESPONSE` 表示恢复时使用已保存回复，没有新 API 调用。

认证头不参与输出；当前调用使用的 API 密钥会被替换为 `[REDACTED]`，接口地址去掉账号、查询参数和片段。
请求与回复内容保留原文和候选，便于逐字核对数据传递。原有运行记录和临时恢复检查点的保存规则保持现状。

HTTP 200 只表示接口成功响应；审阅 JSON、字段和原文引用仍须通过本地校验。
`quote` 如非空，必须逐字等于 `source_refs` 中某个准确原文区间；区间使用 Unicode 码点、左闭右开，
无需在引用对象中重复填写 quote。选区边界及视频阶段本次实际提供的原文范围仍会检查。
评论候选稿原句时将原句放入 explanation，quote=null、source_refs=[]，保留原来的问题和建议。

JSON语法错误会反馈具体行列、字符偏移、解析器原因及最多80字符的错误附近片段，
原始回复仍按一次格式修复流程携带；首字符即无法解析的纯文本不重复附加全文片段。
错误片段放在结构化 details 中，只用于反馈和控制台追踪，普通错误消息仅包含位置及原因。
程序继续按JSON标准严格解析模型返回的对象，格式修复依据实际解析错误，不固定归因为引号问题。

### 5.2 自动审阅契约

`review_contract`使用现有jsonschema依赖，公共Schema描述全部自动审阅的模型输出，
按审阅类型返回独立副本，视频阶段额外限定target_path；Schema自身在构造请求前验证，
校验器按类型缓存。字段定义同时用于模型提示和本地校验，类型错误不会被强制转换，
可选quote／source_refs与额外字段的接受范围保持现状。内部check_report存储规则不变。

普通审阅请求的response_contract包含schema和带问题的合法example；视频审阅使用
单独的“输出结构约束”分块。示例仅说明结构，不作为检查事实；无问题返回issues空列表。
首次与格式修复请求均携带同一契约，完整预算包含Schema、示例、错误反馈及原始回复。

控制台REVIEW_PARSE_ERROR的details.category为syntax、schema或reference。
schema诊断按路径稳定排序，errors最多10项，omitted_count说明省略数；每项包含path、
rule、message、expected、actual_type、actual_preview，实际值预览最多200字符，
missing与null分别标记。reference使用同样的定位信息；syntax沿用解析器位置及80字符片段。
修复反馈通过format_error_feedback序列化到既有error字符串，原始response只在修复块
保存一次，恢复不新增额度；存储、模型配置、Schema自身或网络异常不伪装成输出格式错误。

`test_video_review_contract.py`覆盖各类型契约、派生隔离、诊断限制、来源与选区、
独立SDK路由、结构反馈后的正文修订、重复失败、网络恢复和发送前预算。

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

## 三通道并行维护

短视频通过 TaskChannels 分开调度文字／图片／视频，不使用全局禁用控件列表。
按钮从各通道实时状态和当前镜头输入派生；配置与项目切换仍检查全部通道。
图片改用独立跨进程生图锁，保留项目／媒体短事务锁；来源投影读取须和旧文件清理互斥。
底部回调绑定工作身份，完成事件处理前不能复用该通道。

新增 tests/short_video/test_video_task_channels.py：阻塞任务验证同时运行、独立停止和结束边界；
真实 OpenAI SDK 配合模拟 HTTP 同时执行文字、生图及 ComfyUI，验证冻结输入和运行中恢复。
另覆盖同镜头输入锁、不同镜头并行、查询例外、继续任务目标、项目／配置／退出保护、
未保存稿保留及无视频时打开目录和失败路径。所有测试使用临时项目，不调用真实服务。
