# 小说创作：开发文档

日期：2026-11-25。本文用于定位代码和执行验证。
整体结构见[架构设计](architecture.md)，接口见[接口设计](interfaces.md)，界面见[交互设计](interaction.md)。

## 1. 代码入口

| 要改的东西 | 位置 |
|---|---|
| 工作区、步骤栏 | `core/gui/pages/novel/workspace.py`、`step_rail.py` |
| 各阶段页面 | `core/gui/pages/novel/*_page.py`，设定编辑窗口 `lore_editor.py`、`lore_results.py` |
| 参数读写、选项 | `core/gui/pages/novel/params_store.py`、`core/config/story_options.py` |
| 界面读产物 | `core/gui/pages/novel/services/artifacts.py` |
| 阶段入口、阶段状态 | `core/generation/stage_pipeline.py`、`workflow_status.py` |
| 参数规范化与作品要求 | `core/generation/parameters.py`、`parameter_compliance.py` |
| 世界设定 | `core/generation/lore_pipeline.py`、`lore_cast.py`、`lore_repository.py`、`lore_run.py` |
| 故事结构 / 场景规划 | `structure_pipeline.py`、`scene_pipeline.py`、`design_contract.py`、`planning_contract.py`；「场景规划」两个按钮的提示词 `planning_prompt.py`，大纲和规划里认名字 `planning_checks.py` |
| 章节循环 | `agents/writing/chapter_writing_agent.py`、`core/generation/chapter_generation_loop.py`、`chapter_acceptance.py`、`chapter_continuity.py` |
| 评审与题材档案 | `agents/review/domain_review_agent.py`、`core/generation/domain_profiles.py` |
| 账本、叙事图、待复审 | `story_ledger.py`、`narrative_graph.py`、`pending_review.py`、`plan_fault.py` |
| 提示词公用片段 | `prompt_context.py`、`scene_prompt.py`、`scene_material.py`（写一场戏的设定材料）、`prompt_material.py`（材料按块取舍） |
| 模型调用 | `core/generation/ai_helper.py`、`llm_interface/` |

## 2. 运行与测试

```powershell
# 启动
.\venv\Scripts\python.exe main.py

# 只跑小说的测试
.\venv\Scripts\python.exe -m pytest tests/novel -q

# 全量回归（含两个功能的边界检查）
.\venv\Scripts\python.exe -m pytest tests -q --tb=short

# 不花调用的规划检查：只读，不调用模型；加 --chapters 逐章列一行
.\venv\Scripts\python.exe tools\check_planning.py <作品目录> --chapters
```

规划检查用「场景规划」页正式流程同一套函数拼出每一幕、每一章会收到的提示词（不发送），报告每章场景数、场景边界（写第 N 场时应拦和实际拦的后面场次边界）、提示词每块字数和截断、内部字段、「本章必须埋下 / 引入」里其实早已存在的条目、名字为空的条目、材料篇幅放不下去掉了块的提示词、本章大纲点名却没带卡片的人物、登记表里没有的人名和地名，以及跨章检查的结果。想看一份作品的规划现在是什么样，或者改了规划流程想对比前后，就在作品副本上跑它。

测试不调用真实模型。`tests/common/test_feature_boundaries.py` 会拦下小说与短视频之间的互相引用。

## 3. 质量评估

离线固定样例（脚本模型，不花钱）：

```powershell
.\venv\Scripts\python.exe -m core.evaluation.fixed_story_quality --output-dir quality_runs\fixed_mystery_3ch
```

真实模型按成本递增运行，前一步失败就不要继续：

```powershell
.\venv\Scripts\python.exe -m core.evaluation.live_quality_probe --output-dir quality_runs\fixed_mystery_3ch
.\venv\Scripts\python.exe -m core.evaluation.live_chapter_probe --output-dir quality_runs\live_single_chapter
.\venv\Scripts\python.exe -m core.evaluation.live_story_quality --output-dir quality_runs\live_fixed_mystery_3ch
.\venv\Scripts\python.exe -m core.evaluation.completion_audit --real-dir quality_runs\live_fixed_mystery_3ch --offline-dir quality_runs\fixed_mystery_3ch
```

真实探针在端点不可达时不发起调用；三章运行中断后重复同一命令即可从已验收部分继续。正文身体反应套语过多时，可用 `core.evaluation.live_style_polish --output-dir <目录> --chapter N` 只做语言收敛。

真人盲测（主要指标是续读意愿）：

```powershell
.\venv\Scripts\python.exe -m core.evaluation.reader_blind_test create --baseline-dir <旧版章节目录> --candidate-dir <改进版章节目录> --output-dir <盲测目录> --participants 30
.\venv\Scripts\python.exe -m core.evaluation.reader_blind_test summarize --test-dir <盲测目录>
```

`manifest.private.json` 是答案映射，不要发给读者；收齐样本前不要看中途结果。

其他工具：

- `core.evaluation.lore_acceptance_probe --run-live`：用真实窗口和真实模型验收世界设定。
- `tools/run_sandbox_chapters.py`：复制 `current_work` 到副本连写几章，报告每章重修轮数和相邻章套语；不动原项目。
- `tools/diagnose_planning.py`、`tools/diagnose_quality.py`：诊断已有项目的规划和质量记录。

## 4. 约定

- 小说代码不引用短视频代码；两边都要用的放共用层，见[两个功能的边界](../feature_boundaries.md)。
- 界面不直接调模型，生成一律经过 `stage_pipeline` 的两个入口。
- 改动后更新这四份文档里对应的部分；文档只写当前实现，过程和验收写进 [DEVLOG](../../DEVLOG.md)，条目标题标【小说】，内容写简短。
