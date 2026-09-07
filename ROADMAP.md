# Roadmap — NovelWriter

_Status: active · updated 2026-09-07_

> Python/PySide6 desktop app that helps authors write multi-genre novels and short stories with LLMs, managing parameters, lore, story structure, scene plans, and chapter prose, layered with an agentic multi-agent orchestration and multi-level review framework.
>
> Collated from docs/refactor_plan.md (the source of the refactor checklists) plus the shipped agents/ package and docs/agentic_*.md for the agentic milestone. Design/spec docs that carry no actionable task lists are otherwise excluded.

## Codebase Refactor (core/ + agents/ structure)
> Completed August 4, 2025 per the "Refactor Completion Status" section of refactor_plan.md. The original document left its checklists unchecked, but its own sign-off confirms these were done (reflected here as complete). Double-check the two "Code Quality" items below.
- [x] Create `core/` directory structure (gui, generation, config, utils) with `__init__.py` files
- [x] Create `agents/` directory structure (base, quality, consistency, orchestration)
- [x] Move GUI components to `core/gui/` (main.py → core/gui/app.py, parameters, lore, story_structure, scene_plan, chapter_writing)
- [x] Move generation logic to `core/generation/` (ai_helper, helper_fns, rag_helper)
- [x] Move configuration to `core/config/` (logger_config, genre_configs)
- [x] Move utilities to `core/utils/` (combine.py)
- [x] Update all import statements across moved modules
- [x] Add new `main.py` entry point that imports `core.gui.app`

## Refactor Verification: Functional
- [x] Application starts successfully from new `main.py`
- [x] All GUI tabs load and function correctly
- [x] Parameter collection works
- [x] Lore generation works
- [x] Story structure generation works
- [x] Scene planning works
- [x] Chapter writing works
- [x] File save/load operations work
- [x] Logging functions correctly

## Refactor Verification: Code Quality & Agent Readiness
- [x] No circular imports
- [x] Clean import statements
- [x] Proper package structure
- [x] All `__init__.py` files in place
- [x] No hardcoded paths that break
- [x] Clear separation between core and future agents
- [x] Easy import path for agents to use core functionality
- [x] Modular structure ready for extension

## Agentic Framework (multi-agent orchestration + review) — 部分撤回
> 这一节记录的是 2026-07 之前的状态。其中的多智能体编排与断点续跑**从未接到界面上**，
> 已于 2026-09-07 删除（约 4,900 行）；实际保留并在跑的是评审与章节写作两支。
> 详见 docs/ 下四份 agentic_* 文档开头的说明。
- [x] Base agent 抽象（BaseAgent / AgentResult / AgentMessage，`agents/base/agent.py`）
- [x] 章节写作智能体（`agents/writing/chapter_writing_agent.py`）
- [x] 多级评审：场景 / 章节 / 四份独立评审合议（`agents/review/domain_review_agent.py`）
- [x] 统一多后端 LLM 接口（`core/generation/llm_interface`，由 llm-backends 支撑）
- [x] 四阶段流程执行（`core/generation/stage_pipeline.py`，逐阶段跑而非总编排）
- [x] 章节质量闸门、定向重修、待复审（`core/generation/chapter_generation_loop.py`）
- [x] 章节账本与叙事图，带修订号与跨进程锁（`story_ledger.py` / `narrative_graph.py`）
- [~] ~~MultiAgentOrchestrator / StoryGenerationOrchestrator~~ — 已删除，见上
- [~] ~~QualityControlAgent / ConsistencyAgent 及其工具~~ — 已删除，从未被调用
- [~] ~~CheckpointStateManager 断点续跑~~ — 已删除；阶段状态一律以磁盘产物为准
- [~] ~~BaseTool / ToolRegistry~~ — 已删除，没有任何智能体使用过

## Planned: Cross-pollination from StoryDaemon
> Learnings and self-contained subsystems to bring over from the sibling StoryDaemon project. Full analysis and rationale in StoryDaemon's docs/CROSS_POLLINATION.md (https://github.com/EdwardAThomson/StoryDaemon/blob/main/docs/CROSS_POLLINATION.md).
- [ ] Tension / arc-pressure control: adopt the target-tension curve, LLM tension scorer, and writer/planner guidance, mapping story position to outline position (NovelWriter has no tension control today)
- [ ] Grounded name generation: mint names in Python and have the LLM select and justify rather than invent (port name_generator plus its data banks)
- [ ] Contradiction detection: similarity pre-filter plus LLM judge plus older-wins canon policy, adapted to the consistency entity model
- [~] ~~Wire up the dormant ChromaDB RAG memory (rag_helper.py)~~ — `rag_helper.py` 已随依赖瘦身一并删除；
      若要重做检索记忆，等同于新起一项，不再挂在这条下面。
- [ ] Enforce QA retry: make the review loop actually regenerate or bounded-rewrite toward target instead of only recommending (the ChapterWritingAgent retry path is currently inert)

## Backlog
- [ ] CI/CD: build scripts, test paths, deployment scripts
      Low priority for a desktop app; revisit only if distribution/packaging calls for it.
- [ ] Extract the duplicated LLM-backend layer into a shared Python package both apps depend on
      Spans NovelWriter, StoryDaemon, and LLM-Remote-Runner; scoped in StoryDaemon's docs/CROSS_POLLINATION.md.
      Pending decisions on home, name, and distribution.
- [ ] 世界观数据仍来自 `Generators/` 的英文随机词库（约 12,600 行，无任何测试）：
      人物卡的 goals/flaws/profession/title 是英文短语随机抽取，与故事前提无关；
      势力卡出现「名字是民营公司、type 写着 Police Department」这类自相矛盾；
      `territories` 里的抽象短语（Court and legal systems）被当成地名转成了中文城市。
      方向已定：改为让模型按题材与故事前提直接生成中文卡片，落盘前做确定性校验。
- [ ] 评审「打了低分却没给改法」需要补一轮调用（实测三章 9 次，占全部调用 10%）。
      这是跨字段的语义约束，JSON schema 表达不了；真正的解法是把分数与改法合并成
      同一个对象，让「低分而无改法」在结构上写不出来。属于评审契约变更，需实跑验证。
- [ ] `core/evaluation/` 七个模块（约 2,800 行）只有命令行入口，界面里进不到。
- [ ] 各项阈值（重修门槛 3.20、规划回声 12 字、开场套语 11 字）都只在法律悬疑
      一个题材上实测过，另外八个题材沿用同一组数字。

## Notes
- refactor_plan.md documents the one-time core/ + agents/ reorganization. The agentic layer it prepared for has since shipped (see the Agentic Framework milestone); its design lives in docs/agentic_implementation.md, agentic_integration_guide.md, and agent_workflow_explanation.md.
