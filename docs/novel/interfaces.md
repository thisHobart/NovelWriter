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
| | `titles` | 推荐作品标题 |
| | `repair` | 再修复一次（`options.receipt_id` 指定哪条生成结果） |
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
| 作者姓名 | `author_name` | 只出现在世界设定的作品要求里 |
| 题材 / 子题材 | `genre` `subgenre` | 世界设定、场景规划、正文提示词里的类型；决定题材档案（评审规则）；子题材决定附加设置页签 |
| 故事篇幅 | `story_length` | 短篇走单独管线；决定可选结构 |
| 故事结构 | `story_structure` | 分幕名称，结构和场景规划按幕切分 |
| 主题 / 基调 | `theme` `tone` | 世界设定、场景规划、正文提示词；主题记入账本作为全书核心问题；题材档案匹配的最后一级 |
| 性别比例 | `gender_generation_bias_string` 及两个百分比 | 生成人物时的男女比例 |
| 质量闭环 | `quality_loop` | `off` / `standard` / `strict`，只影响章节循环 |
| 附加设置页签 | 子题材自带的键（如 `happy_ending`、`protagonist_type`） | 只进世界设定的作品要求：勾选为「必须体现」，不勾为「不得引入」 |
| 势力数量 / 人物数量 | `num_factions` `num_characters` | 在「世界设定」页设置，范围 1–10 / 2–10，默认 6 / 8 |

`backend`、`model` 也写在同一文件，但不算作品参数，不进作品要求。

世界设定提示词里的「作品要求（最高优先级）」列出全部作品参数；故事结构只用篇幅、结构和标题（融入地点时另带题材）；场景规划和正文带类型、篇幅、结构、主题、基调和标题。

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
| `system/lore_generation/results/` | 世界设定交付记录：状态、问题、候选、请求数 |
| `system/lore_edits.json`、`system/lore_history/` | 设定手动编辑的版本、待核对项、事务备份 |

## 4. 设定编辑

`core/generation/lore_repository.py` 的 `LoreRepository`：

- `load_snapshot()`：返回绑定文件指纹的项目快照。
- `preview(snapshot, edits, mappings)`：返回改名带来的关联更新和影响范围。
- `save(snapshot, edits, mappings)`：校验、备份并提交；文件被外部改过时拒绝覆盖。

`edits` 的键是设定文件的项目相对路径，值是完整文本。登记条目无法唯一确定时抛 `MappingRequired`，由界面让用户选择。手动保存与阶段生成共用 `system/.lore.lock`。
