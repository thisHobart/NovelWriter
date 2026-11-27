# 小说创作：接口设计

日期：2026-11-25。本文说明阶段调用、作品参数和作品文件。
整体结构见[架构设计](architecture.md)，界面行为见[交互设计](interaction.md)，代码入口见[开发文档](development.md)。

## 1. 阶段调用

界面的每个生成按钮都落到 `core/generation/stage_pipeline.py` 的两个入口，二者不依赖 Qt，可以无界面调用：

```python
run_stage(step, output_dir, model="", parameters=None, report=None) -> dict
run_stage_action(step, action, output_dir, model="", parameters=None,
                 report=None, chapter_number=None, options=None) -> dict
```

- `parameters` 覆盖磁盘参数中的部分值；传入 `freeze_parameters(...)` 的结果视为完整快照，不再读磁盘。
- 返回值含 `generated_files`、`parameter_run_directory`（本次运行记录目录）、`parameter_warnings`。
- 界面通过 `services/step_runner.py` 的 `step_work`、`action_work`、`full_workflow_work` 把它们包装成后台任务。

| step | action | 界面按钮 |
|---|---|---|
| `lore` | 空 / `full` | 生成世界设定 |
| | `factions` `characters` `lore` | 生成势力 / 生成人物 / 生成世界观（生成过的叫「重新生成…」） |
| | `enhance` | 生成人物背景 / 重新生成人物背景（`options.subject_name` 指定单个人物） |
| | `titles` | 候选标题页的「换一组」 |
| | `repair` | 让 AI 再修一次（`options.receipt_id` 指定哪条生成结果）；`options.continue` 为真时是「再修一次并继续生成」：修好后接着跑那次整段生成剩下的几步 |
| | `continue` | 接着生成后面几项 / 按新势力重写后面几项 / 就用现在的，继续生成（`options.steps` 为要跑的步骤，按整段生成的顺序跑；`options.source` 只用于改动记录里的名称；`options.accept` 是停下的那条生成结果的编号，先把它记为 `accepted` 再跑） |
| `structure` | 空 / `full` | 生成故事结构 |
| | `arcs` `faction_arcs` `locations` `plot` | 生成人物弧光 / 生成势力弧光 / 将地点融入故事弧 / 创建详细情节 |
| `scenes` | 空 / `full` | 规划场景（整阶段） |
| | `outline` `scenes` | 生成章节大纲 / 规划场景 |
| `chapters` | `next` `all` `rewrite` | 撰写下一章 / 撰写全部剩余章节 / 重写本章（`chapter_number`） |

世界设定整段生成（`full`、`continue`、带 `continue` 的 `repair`）按 `stage_pipeline._stops_here` 判断要不要停：一份结果只留了草稿，或势力、人物、世界观三项里的一项以 `attempt_limit` / `no_improvement` 结束、仍是 `needs_changes`。停下时那条生成结果写 `remaining`，返回值里 `stopped` 为真；`full_workflow_work` 遇到 `stopped` 或 `status == "draft"` 就不跑后面阶段。
| | `revise` | 照建议重修（`options.issue_ids` 为勾选条目） |
| | `waive` | 人工放行（`options.reason` 为放行理由） |

阶段状态由 `core/generation/workflow_status.py` 的 `assess_workflow(output_dir)` 给出，每个阶段一个 `StageStatus`：`complete` / `partial` / `blocked` / `empty`，附原因文字和完成数。

## 2. 作品参数

保存在 `system/parameters.txt`，每行 `键: 值`，由 `core/gui/pages/novel/params_store.py` 读写。内部统一用小写下划线键名，`core/generation/parameters.py` 负责规范化和冻结。

| 界面字段 | 键 | 用在哪里 |
|---|---|---|
| 输出目录 | `output_directory` | 作品目录；不发给模型 |
| 作品标题 | `novel_title` | 世界设定、场景规划、正文提示词；合并稿标题 |
| 题材 | `genre` | 下拉预设悬疑推理 / 恐怖 / 科幻 / 爱情，可改写；世界设定、场景规划、正文提示词里的类型；按其中的关键词选题材档案（评审规则） |
| 故事篇幅 | `story_length` | 短篇 / 中篇 / 长篇三档；短篇走单独管线 |
| — | `story_structure` | 界面不显示：新作品按篇幅定（短篇、中篇三幕，长篇六幕），已有作品沿用保存的结构；结构和场景规划按幕切分 |
| 主题 / 基调 | `theme` `tone` | 下拉预设按题材给，可改写；世界设定、场景规划、正文提示词；主题记入账本作为全书核心问题；题材档案匹配的最后一级 |
| — | `num_factions` `num_characters` | 界面不显示：按篇幅定，短篇 3 / 5、中篇 4 / 6、长篇 6 / 8（`story_options.COUNTS_BY_LENGTH`，`normalize_parameters` 里套用）；参数文件里旧的数量原样保留，但不再生效 |
| 质量闭环档位 | `quality_loop` | 在「章节撰写」页选择：`off` / `standard` / `strict`；只影响章节循环，不发给模型 |

题材、主题、基调的预设在 `core/config/story_options.py`（`GENRE_OPTIONS`、`THEME_OPTIONS`、`TONE_OPTIONS`），只决定下拉列表里有什么；发给模型的是框里最后的文字。

`backend`、`model` 也写在同一文件，但不算作品参数，不进作品要求。

发给模型的作品参数只取上表中的作品标题、题材、篇幅、结构、主题、基调和两个数量（`parameters.py` 的 `CREATION_KEYS`）；文件里的其他键原样保留，但不进任何提示词。世界设定提示词里的「作品要求（最高优先级）」逐条列出这些参数；故事结构只用篇幅、结构和标题（融入地点时另带题材）；场景规划和正文带类型、篇幅、结构、主题、基调和标题。

旧版作品文件里的子题材在打开时并入题材（`爱情（当代爱情）`），超出三档的篇幅换成长篇；作者姓名、性别比例和附加设置页签的键在下次保存时丢弃。

## 3. 作品文件

路径相对于作品输出目录。

| 路径 | 内容 |
|---|---|
| `system/parameters.txt` | 作品参数 |
| `story/lore/factions.json`、`characters.json` | 势力、人物卡片 |
| `story/lore/generated_lore.md`、`lore_contract.json` | 世界观正文、名称与规则登记表 |
| `story/lore/background_*.md` | 主要人物详细背景 |
| `story/structure/` | 人物/势力弧光、各幕结构、`structure_contract.json` |
| `story/planning/chapter_outlines/` | 各幕章节大纲 |
| `story/planning/detailed_scene_plans/` | 逐章场景规划（含章节契约） |
| `story/content/chapters/chapter_N.md` | 正式章节正文 |
| `system/story_ledgers/` | 账本：`case_bible.json`、`narrative_graph.json`、`suspense_ledger.json`、逐章契约与事实变化 |
| `quality/pending_review/chapter_N.json` | 待复审记录；文件存在即表示该章待复审，验收后删除 |
| `quality/` 其他目录 | 各轮评审、章节事务、指标 |
| `system/generation_runs/<编号>/` | 每次阶段运行的生效参数、指纹和实际提示词 |
| `system/lore_generation/results/<编号>.json` | 世界设定每次生成的结果：状态、问题、候选、请求数；另有 `notes`（检查备注）、`reverted`（已被撤回）、`file_digests`（保存时各文件指纹，用来认出手动修改）、`remaining`（整段生成停在这里时还剩哪几步）、`accepted`（作者点了「就用现在的，继续生成」，不再算要处理）。旧记录没有这些字段，照常显示 |
| `system/lore_edits.json` | 设定的版本号、待核对项（`review_items`）、后面阶段待核对的文件（`pending`，每条 `{stage, version}`；只因改名还写着旧名或别名才待核对的另带 `names: true`） |
| `system/lore_history/changes.json`、`system/lore_history/<编号>/state.json` | 改动记录：每次改动的名称、时间、前后两份完整设定快照的编号；撤回、恢复按它进行 |
| `system/lore_history/<编号>/transaction.json` | 保存事务的备份，中断后恢复用 |

## 4. 设定编辑

`core/generation/lore_repository.py` 的 `LoreRepository`：

- `load_snapshot()`：返回绑定文件指纹的项目快照。
- `preview(snapshot, edits, mappings)`：返回改名带来的关联更新和影响范围。
- `save(snapshot, edits, mappings)`：校验、备份并提交；文件被外部改过时拒绝覆盖。

`edits` 的键是设定文件的项目相对路径，值是完整文本。登记条目无法唯一确定时抛 `MappingRequired`，由界面让用户选择。手动保存与阶段生成共用 `system/.lore.lock`。

- 改名：`preview` 同步结构化引用，并把世界设定内自由文本（正文、背景、卡片描述、登记表）里的完整旧名换成新名（`rename_plan`、`swap_names`）。旧名只有一个字或是别的名字的一部分时不换，记在 `LorePreview.kept`；换掉的处数在 `LorePreview.replaced`，编辑窗口用 `rename_impact(preview)` 写成说明。后面阶段的文件不改，列进待核对。
- 只改名字的保存：`LorePreview.rename_only` 为真表示把旧名都换成新名以后，作者改的每个文件和改前一样（`_names_only`）。这时 `review_items` 不加「设定已修改，请核对……」那句，保留之前的其他待核对项；后面阶段只按旧名和登记过的别名（`_aliases`：两个字以上、新名里没有的）找，找到的写成「路径：仍含旧名「…」（现名「…」），请核对。」或「路径：仍含别名「…」（现名「…」），请核对。」（`NAME_ITEM`），文件列在 `LorePreview.touched`；`save` 只把这些文件加进 `pending`，之前的 `pending` 照旧。不是只改名字时，后面阶段的文件全部进 `pending`。
- 撤回：`change_log()` 返回 `{"undo": 最近一次改动, "redo": 最近一次撤回}`；`undo(id)`、`redo(id)` 拿同一把锁，写入前先把当前磁盘状态记下来。每次保存、生成（经 `tracked_generation`）、采用草稿和 `acknowledge` 都记一条改动；新改动之后不能再恢复更早撤回的那条。
- 待核对：`review_summary(output_dir)` 返回 `{version, general, groups}`。`groups` 每个阶段一行：`{stage, title, line, files}`，`files` 是 `[(路径, ["旧名「韩雪」", "别名「住建局」"…])]`。一个阶段的文件都只因改名待核对时，`line` 写「故事结构：8 个文件仍写着旧名「韩雪」（现名「韩新雪」）。」，否则写「……个文件是按旧设定写的，其中……」。
- 后面阶段的提示词：`lore_context(output_dir)` 只带还属于现有人物的背景文件（`current_backgrounds`，按 `background_<身份>_<姓名>.md` 认人）；旧版本留下的 `story/lore/name_mapping.json`（`LEGACY_TABLES`）也不带。两类文件都留在磁盘上。每份文件的小标题用 `lore_localization.file_label` 的中文名（「名称与规则登记表」「人物背景 · 主角 · 沈宁」），不用路径。`file_label` 也给后面阶段的文件起中文名（「故事结构 · 开端」「故事结构 · 结构登记表」「章节大纲 · 结局」「第 3 章场景」「地点与故事弧」「第 12 章正文」），认不出的照旧返回路径；`display_message` 把消息里 `story/` 下的路径都换成这些名字。
- 故事结构文件的名字：`story_options.act_file_stem(结构, 幕名)` 是故事结构阶段写每一幕时的文件名，`act_for_file(文件名)` 反查是哪一幕；`localization.structure_file_label(路径)` 给出「故事结构 · 开端」「章节大纲 · 结局」这样的中文名，界面幕卡片和全书设定整理的材料都用它。
- 写章节前整理全书设定的材料：`StoryLedgerManager.load_design_context(max_chars=72000)` 返回 `DesignMaterial`（一段文字），按 故事结构登记表、名称与规则登记表、各幕故事结构、各幕章节大纲、世界观正文、人物资料、势力资料、统一故事弧、人物弧光、势力弧光、人物背景 排列，幕按结构里的先后。超出篇幅时从最后一块起整块去掉（两张登记表总是带），去掉的列在材料末尾，也在 `omitted` 上；保存时记进 `case_bible.json` 的 `design_omitted`。`source_hash` 按原始文件内容算，`case_bible_needs_refresh`、`save_case_bible` 用它判断要不要重新整理；传普通文字时退回按全文算。`includes_lore` 为真时，整理提示词不再另附一份世界设定。
- 写一场戏的设定材料：`scene_material.build_scene_material(作品目录, 场景规划, contract=本章契约, scene_number=第几场, max_chars=30000)` 返回 `SceneMaterial`（一段文字），按 名称与规则登记表、本场人物、相关势力、世界观正文、本场人物的背景 排。本场人物是场景规划里点到名字的（登记表里的别名也算，一个字的名字不认），加上本章契约点名的（`cost_character`、`character_updates`，第一场另加 `continuity.opening_positions`），一个都认不出时带主角；相关势力是本场人物所属的、规划点到名字的、地盘在规划点到的地点的。人物卡的 `arc` 不带。超出篇幅时从最后一块起整块去掉（前两块总是带），`characters`、`factions`、`omitted` 记着带了谁、去掉了什么。章节写作和短篇都把它传给 `build_scene_prompt(material=...)`，这时不再带整份的 `lore`、人物名单和势力概览；作品没有 `story/lore/` 下的设定文件时返回空文字，照旧用那三份。两份材料共用 `prompt_material.fit_blocks` 的取舍规则。
- 「场景规划」页两个按钮的提示词：`planning_prompt.outline_prompt(...)`（「生成章节大纲」每一幕一份）、`scene_plan_prompt(...)`（「规划场景」每章一份）、`short_story_prompt(...)`（短篇整篇一份）只拼文字，返回 `PlanningPrompt`（一段文字），`blocks` 是按先后排的（中文名，原文），原文首尾相接就是整段提示词。其中契约格式说明由 `contract_instructions(作品目录, 章号, 作品参数)` 拼，拆成「契约格式与填写规则」「仍未了结的悬念」「后续章节依赖本章」「叙事图上下文」「叙事图规则」「既有事实、时间事件与已了结线索」「衔接规则」「上一章结束时的状态」几块（`planning_contract.contract_instruction_blocks`）；既有事实、时间事件、已了结的悬念写成「F-001-01｜事实｜取值」「TL-001-01｜时间｜地点｜事件」这样的中文条目，一条不截（`established_records_text`），叙事图写成中文条目、一个节点都没有时整块为空（`graph_context_text`），哪几块算材料见 `MATERIAL_LABELS`。跨章修复的提示词用的是整份契约格式说明。
- 「规划场景」每章的材料：`planning_material(作品目录, 章号, 全书每章大纲, 契约格式说明, lore_content=..., max_chars=PLANNING_BUDGET)` 返回 `PlanningMaterial`（一段文字），用 `prompt_material.fit_blocks` 按 本章大纲、名称与规则登记表、本章人物、相关势力、上一章结束时的状态、仍未了结的悬念和后续章节依赖、既有事实和时间事件、前后相邻两章的大纲、世界观正文 排；超出篇幅时从最后一块起整块去掉（本章大纲、登记表、本章人物、悬念和依赖总是带），`kept`、`omitted`、`characters` 记着带了哪几块、去掉了哪几块、带了谁的卡。本章人物用 `scene_material.pick_characters` 按本章那一段大纲挑；事实和时间事件按编号（后面章节要用到的、上一章记下的）和本章大纲点到的人物、势力、地点挑，没列出的条数写在块末。`scene_plan_prompt` 把材料排在作品参数之后、写法要求之前，契约格式说明里属于材料的几块不在后面重复；「规划场景」把带了、没带的块写进运行记录（「第 N 章规划材料带了：…；篇幅放不下没带：…」）。全书每章那一段大纲由 `book_outlines(作品目录, 结构, 幕列表, 大纲目录)` 读，章号认法和「规划场景」一样，一幕的大纲按章切开用 `chapter_parts`。作品没有设定文件时没有登记表、人物和势力几块，世界观正文用 `lore_content`。
- 「生成章节大纲」每一幕的设定材料：`outline_material(作品目录, 上一幕最后一章, max_chars=OUTLINE_BUDGET)` 返回 `PlanningMaterial`，用 `fit_blocks` 按 名称与规则登记表（总是带）、人物和势力名单（`cast_roster`）、上一幕最后一章的大纲 排，超出篇幅时从最后一块起整块去掉。上一幕最后一章由 `previous_act_ending(作品目录, 结构, 幕列表, 这一幕, 大纲目录)` 取：往前找最近一幕已有的大纲，取最后一章那一段，标题挪到从三级起；第一幕或前面都没有大纲时是空文字。`outline_prompt(..., output_dir=作品目录, previous_ending=...)` 把这几块排在「这一幕的故事结构」之后、「写法要求」之前，返回的 `PlanningPrompt` 带 `kept`、`omitted`；不给 `output_dir` 时和以前一样。「生成章节大纲」把带了、没带的块写进运行记录（「“某一幕”大纲材料带了：…；篇幅放不下没带：…」）。作品没有设定文件时只有上一幕最后一章那一块。这几个函数只读：叙事图用 `NarrativeGraphManager.reader(作品目录)`，不建文件、不迁移旧字段；正式流程在拼之前先建好账本和叙事图文件。世界观一块用 `planning_lore(作品目录)`。`tools/check_planning.py` 用的是同一套函数。
- 规划阶段不花调用的两项判断在 `planning_checks.py`：`unregistered_names(文字, 登记表)` 找大纲或规划里列出、登记表里没有的人名和地名，`name_note(结果)` 写成「人名：…；地名：…」；`scene_count_note(场景数, 范围)` 在场景数不在范围里时写成「共 N 场，不在建议的 2–4 场之内」，长篇范围是 `LONG_FORM_SCENE_RANGE`（2–4），短篇用 `SHORT_STORY_MIN_SCENES`–`SHORT_STORY_MAX_SCENES`。「规划场景」收下一章规划时（`ScenePipeline._generate_valid_scene_response(..., scene_range=...)`，包括跨章修复重写的那一章）、「生成章节大纲」收下一幕大纲时，把这两项写进运行记录（警告级，句末「只记录，不打回」），规划照旧保存。规划检查脚本的「【场景数】」和「【登记表里没有的名字】」用的是同一组函数。
- 世界观冲突：`prompt_context.find_scene_world_conflicts(规划或大纲, 设定, 作品参数)` 只对非科幻作品查，返回写了、设定里却没有的硬科幻专名（`NON_SCIFI_MARKERS`）；写作批注行（标签带「作用」「弧光」「节奏」这类字的）不查。`world_conflict_message(专名, 作品参数, source="世界观")` 把它写成按题材说的一句「写了这部悬疑小说的世界观里没有的设定：行星、星际」，题材名没有中文对照时说「这部小说」。「规划场景」长篇和短篇、「生成章节大纲」（`source="故事结构"`，比的是这一幕的故事结构）打回或报错时在前面加上「场景规划」「大纲」。
- 场景边界：新规划每一场写一条 `{"scene_number": N, "must_do": [...], "must_not_do": [...], "end_state": "这一场停在哪里"}`，「规划场景」的提示词逐项说明；`scene_boundary_defects(契约, 场景数)` 查每一场都有带 `scene_number` 和非空 `end_state` 的一条，不合格的和其他契约问题一起退回重写（问题代码 `scene_boundary_missing`），只查新生成的规划。读边界认得旧作品的写法：`scene_number`、`scene_index`、`scene` 等键，「scene_2: …」「场景 2：…」「第 2 场 …」这样的文字，纯数字；整份列表一条都没写场次时按先后当第 1、2、3 场（`boundary_scenes`），旧契约文件不改。`boundary_scene(一条边界)` 返回这条自己写明的场次，认不出时是 `None`；写第 N 场时列成「禁止触碰」的后面场次边界是 `later_scene_boundaries(契约, N)`，本场及以前的留在 `contract_for_scene(契约, N)` 里。
- 后续章节依赖本章：`downstream_obligations(作品目录, 章号)` 返回 `{"threads_closed_later": {id: {id, thread, closed_at, opened_at}}, "facts_used_later": {id: {id, fact, used_at, introduced_at}}}`，只收本章或更早引入、后面已经规划的章要用到的。`opened_at`、`introduced_at` 是落盘契约里真正埋下、引入它的那一章，问不出来时是 `None`；后面章节引用时没写名字，`thread`、`fact` 取引入那一章写的。「规划场景」的提示词按出处分两组：本章自己的（`opened_at` 等于本章或问不出来）写「本章必须埋下 / 引入」；更早章节的写「第 X 章已埋下，第 Y 章要了结，本章不能了结它，也不能把它写没」，事实写「第 X 章已引入，第 Y 章要引用，本章不能推翻它，也不能把它写没」，原定截止章已到的另说要写一条 extend 记录推迟。谁都没写名字时只写编号，不留空括号。重规划的章把后面的章要了结的悬念先了结掉，`iter_history_defects` 报 `thread_closed_before_payoff`，和其他契约问题一起退回重写。

`core/generation/lore_run.py` 从生成记录推算页面状态：`latest_by_product` 每份产物最新一次，`needs_attention` / `attention_receipts` 还要作者处理的几份，`edited_after_save` 保存后是否手动改过，`stale_lore` 按生成先后找出过期的几项。
