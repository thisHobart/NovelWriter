# 短视频脚本模式实施计划

## 范围与边界

小说入口、项目格式和生成行为保持兼容。短视频独立保存参数、状态、版本与任务上下文，共享模型配置、后台任务与状态栏。短视频页面按需创建；仅启动小说不创建脚本文件。

## 开发顺序

- [x] 创建文档，固定最小数据模型、状态和样例约定。
- [x] 存储、数据校验、请求预算与离线测试。
- [x] 写作及评审 Agent、无界面生成管线。
- [x] 首页切换、独立导航与任务路由。
- [x] 需求、大纲、脚本编辑、评审、版本恢复和 Markdown 导出。
- [x] 新功能测试与小说回归，更新已知限制。
- [ ] 显式运行真实模型与人工 A/B 质量验收（工具已完成，本轮未调用真实模型）。

## 模块

`core/short_video/`：models、repository、validation、prompts、llm、pipeline。

`agents/writing/short_video_script_agent.py` 和 `agents/review/short_video_review_agent.py`：遵循 BaseAgent/AgentResult，独立生成与评审。

`core/gui/pages/short_video/`：总览、需求、大纲、脚本、成果。MainWindow 仅增加模式路由与任务适配；StepRail 支持重新配置流程项目。

## 状态与验收

需求改变使大纲确认失效；大纲改变使旧脚本过期；用户确认后冻结输入。脚本版本状态为未检查、待修改、已审阅，人工定稿另记；旧版依据变更后仍可查看。生成不自动覆盖人工稿。

每项开发必须有真实行为和对应测试。自动化通过证明功能可用；真实写作质量需要独立样稿比较，不使用脚本假模型的结果声称质量提升。真实模型仅通过显式命令启动。

## 逐项验收记录（2026-10-30）

下表路径相对于项目根目录。测试均替换模型或网络边界；界面测试使用真实 Qt 控件、信号和后台任务。

| 要求 | 实现位置 | 验证证据 | 剩余问题 |
|---|---|---|---|
| 文档、模型与最小完整样例 | 本目录五份文档；`core/short_video/models.py` | 已逐份核对；`test_agents_return_contract_and_full_context` | 无功能缺口 |
| 中文独立剧情、30–90 秒默认 60 秒及全部需求字段 | `models.py`；`requirements_page.py`；`prompts.py` | 需求控件范围、固定样例与 `test_duration_and_markdown_are_honest_estimates` | 时长为估计 |
| 小说默认首页、双向切换、独立导航与会话目录/页面位置 | `core/gui/main_window.py`；两个 `workflow_page.py` | `test_lazy_home_switch_and_novel_navigation_restored`；五页 Qt 截图检查 | 自定义目录在重启后需重新打开 |
| 页面按需创建，启动小说不创建脚本文件 | `MainWindow._build_video_pages`；`ScriptRepository.load` | `test_lazy_home_switch_and_novel_navigation_restored`；`test_reads_are_lazy_and_novel_files_untouched` | 无 |
| 共享模型配置、任务管理、进度；脚本不走小说检查 | `MainWindow._on_video_task_requested`；`llm.py` | `test_real_buttons_generate_confirm_review_and_finalize`；小说 `test_every_generation_entry_uses_live_selection_over_saved_model` | 无 |
| 运行时禁止切换，停止入口保持可用 | `MainWindow.switch_mode`；共享状态栏 | `test_running_blocks_switch_cancel_preserves_returned_script` | 等当前请求返回或超时后停止 |
| 未保存编辑保存/放弃/取消；目录状态刷新 | `workspace.py`；`requirements_page.py` | `test_unsaved_switch_cancel_and_discard`；`test_unsaved_save_navigation_and_ui_markdown_export`；`test_open_recovers_draft_and_interrupted_run_without_requests` | 无 |
| 独立存储、同根目录隔离、旧小说项目兼容 | `repository.py`；小说存储保持原路径 | `test_writing_video_in_legacy_novel_root_preserves_novel_files`；`test_reads_are_lazy_and_novel_files_untouched`；原有 `test_ui_params_store.py`、`test_ui_artifacts.py`、`test_ui_integration.py` | 无需迁移 |
| Agent 遵循 BaseAgent/AgentResult，显式业务输入 | 两个 `short_video_*_agent.py`；`pipeline.py` | `test_agents_return_contract_and_full_context`；`test_proactive_revision_preserves_original_and_passes_full_inputs` | 无 |
| 结构、引用、有限时长校验与完整 Markdown | `validation.py`；`editor.py` | `test_invalid_script_structure_is_rejected`；`test_review_cannot_invent_evidence`；`test_review_accepts_real_multiline_action_evidence`；UI 导出测试 | 语义质量依赖模型和人工判断 |
| 需求保存、大纲编辑确认门槛 | `repository.py`；`outline_page.py`；`pipeline.py` | `test_outline_then_confirm_then_script`；`test_confirmation_cannot_approve_an_unseen_external_edit`；实际按钮流程测试 | 无 |
| 冻结快照、指纹过期、确认新大纲后才能写稿 | `ScriptWork`；`ScriptPipeline.run`；`ScriptRepository.source` | `test_submitted_work_rejects_changed_source_before_any_call`；`test_snapshot_and_changed_requirements_make_new_script_stale`；`test_confirmation_staleness_and_restart`；`test_outline_edit_retains_old_script_but_requires_new_confirmation` | 无 |
| 整篇生成、本地检查、独立评审、最多修订一次 | `pipeline.py`；两个 Agent；`prompts.py` | `test_one_whole_script_repair_with_evidence`；`test_invalid_generation_retry_and_review_stay_within_four_calls` | 真实创作质量未验收 |
| 仅有可执行脚本问题且预算足够才修订；不自动改大纲 | `ScriptPipeline.run` | `test_revision_reserves_time_for_both_requests`；`test_outline_fault_does_not_automatically_change_approved_outline` | 不足 240 秒时保留问题，等待主动修订 |
| 候选排序、修订失败保留、人工稿和定稿保护 | `candidate_rank`；`complete_run`；`save_manual` | `test_worse_repair_cannot_displace_better_candidate`；`test_failed_repair_keeps_better_version`；人工稿/定稿保护及并发保存测试 | 无 |
| 模型评审与人工定稿分离；非法/失败输出不伪装通过 | `pipeline.py`；`repository.py`；脚本/成果页 | `test_unavailable_review_is_unchecked_and_no_secret_persisted`；实际按钮定稿测试；记录保存失败测试 | 人工可明确选择定稿，不能代替创作质量证明 |
| 大纲最多 2 次、脚本最多 4 次；失败计数；禁隐式重试 | `llm.py`；`pipeline.py` | 空/非法输出重试测试；`test_budget_counts_failures_and_never_implicitly_retries`；SDK 参数测试 | 无 |
| 单次 120 秒、任务 300 秒与修订时间预留 | `RequestBudget`；`ScriptLLM` | `test_real_sdk_deadline_and_retry_options`；`test_total_deadline_prevents_review_and_retains_draft`；修订时间预留测试 | 不保证在超时前取得有效回复 |
| 原子保存、冲突保护、安全运行记录 | `repository.py`；`ScriptLLM` | 原子替换失败、事务内来源检查、外部需求冲突、损坏内部数据及非文字 SDK 回复测试 | 持续磁盘故障无法保证最新返回落盘 |
| 取消前后保留、恢复中断任务、不补发请求 | `pipeline.py`；`recover`；项目打开入口 | 请求前、返回后和评审中取消测试；`test_recovery_respects_another_threads_live_generation`；重启界面测试 | 不强杀后台线程 |
| 实际编辑、版本恢复、主动修订、定稿及导出 | `core/gui/pages/short_video/` | 实际按钮、长文本/引用名称/列表编辑、版本选择、主动修订与 UI Markdown 导出测试 | 无 |
| 6 个创意 × 两种时长、显式真实评估、A/B 材料 | `core/evaluation/short_video_probe.py` | `test_quality_materials_prepare_twelve_cases_without_model_calls`；`test_quality_comparison_is_balanced_and_private`；命令见 testing.md | 本次未调用真实模型、未进行人工 A/B 评价 |

## 本轮修复与验证

- 将人工稿追加与选择、模型候选选择与任务结束分别合并为原子事务；增加需求保存的外部修改检查。
- 加载时校验内部数据，损坏项目不会替换当前项目或被恢复操作覆盖；过期产物仍可读取。
- 自动修订传递完整问题证据，并检查次数及 240 秒时间预留。
- 修复人物改名后引用名称未更新、模式切换后目录状态未更新，以及拒绝切换时停止入口被隐藏。
- 短视频专项：67 项通过。全量最终复验结果见 testing.md。
- 已渲染并检查五个当前 Qt 页面，离线示例能定稿和导出；截图与示例位于忽略目录 `temp/short_video_audit/`，不作为真实模型样稿。
- 本次未提交、推送、发布或调用付费模型。真实写作质量仍待单独评估。
