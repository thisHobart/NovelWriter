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
| | `factions` `characters` `lore` | 生成势力 / 生成人物 / 生成世界观 |
| | `enhance` | 完善主要人物（`options.subject_name` 指定单个人物） |
| | `titles` | 候选标题页的「换一组」 |
| | `repair` | 让 AI 再修一次（`options.receipt_id` 指定哪条生成结果）；`options.continue` 为真时是「再修一次并继续生成」：修好后接着跑那次整段生成剩下的几步 |
| | `continue` | 接着生成后面几项 / 按新势力重写后面几项（`options.steps` 为要跑的步骤，按整段生成的顺序跑；`options.source` 只用于改动记录里的名称） |
| `structure` | 空 / `full` | 生成故事结构 |
| | `arcs` `faction_arcs` `locations` `plot` | 生成人物弧光 / 生成势力弧光 / 将地点融入故事弧 / 创建详细情节 |
| `scenes` | 空 / `full` | 规划场景（整阶段） |
| | `outline` `scenes` | 生成章节大纲 / 规划场景 |
| `chapters` | `next` `all` `rewrite` | 撰写下一章 / 撰写全部剩余章节 / 重写本章（`chapter_number`） |
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
| `system/lore_generation/results/<编号>.json` | 世界设定每次生成的结果：状态、问题、候选、请求数；另有 `notes`（检查备注）、`reverted`（已被撤回）、`file_digests`（保存时各文件指纹，用来认出手动修改）、`remaining`（整段生成停在这里时还剩哪几步）。旧记录没有这些字段，照常显示 |
| `system/lore_edits.json` | 设定的版本号、待核对项、后面阶段待核对的文件 |
| `system/lore_history/changes.json`、`system/lore_history/<编号>/state.json` | 改动记录：每次改动的名称、时间、前后两份完整设定快照的编号；撤回、恢复按它进行 |
| `system/lore_history/<编号>/transaction.json` | 保存事务的备份，中断后恢复用 |

## 4. 设定编辑

`core/generation/lore_repository.py` 的 `LoreRepository`：

- `load_snapshot()`：返回绑定文件指纹的项目快照。
- `preview(snapshot, edits, mappings)`：返回改名带来的关联更新和影响范围。
- `save(snapshot, edits, mappings)`：校验、备份并提交；文件被外部改过时拒绝覆盖。

`edits` 的键是设定文件的项目相对路径，值是完整文本。登记条目无法唯一确定时抛 `MappingRequired`，由界面让用户选择。手动保存与阶段生成共用 `system/.lore.lock`。

- 改名：`preview` 同步结构化引用，并把世界设定内自由文本（正文、背景、卡片描述、登记表）里的完整旧名换成新名（`rename_plan`、`swap_names`）。旧名只有一个字或是别的名字的一部分时不换，记在 `LorePreview.kept`；换掉的处数在 `LorePreview.replaced`，编辑窗口用 `rename_impact(preview)` 写成说明。后面阶段的文件不改，列进待核对。
- 撤回：`change_log()` 返回 `{"undo": 最近一次改动, "redo": 最近一次撤回}`；`undo(id)`、`redo(id)` 拿同一把锁，写入前先把当前磁盘状态记下来。每次保存、生成（经 `tracked_generation`）、采用草稿和 `acknowledge` 都记一条改动；新改动之后不能再恢复更早撤回的那条。
- 待核对：`review_summary(output_dir)` 把待核对项按阶段汇总成几行，每行带文件列表。
- 后面阶段的提示词：`lore_context(output_dir)` 只带还属于现有人物的背景文件（`current_backgrounds`，按 `background_<身份>_<姓名>.md` 认人）。

`core/generation/lore_run.py` 从生成记录推算页面状态：`latest_by_product` 每份产物最新一次，`needs_attention` / `attention_receipts` 还要作者处理的几份，`edited_after_save` 保存后是否手动改过，`stale_lore` 按生成先后找出过期的几项。
