# 测试与质量评估

## 离线验证

使用项目 venv 的 Python 运行：

```powershell
.\venv\Scripts\python.exe -m pytest tests/test_short_video_pipeline.py tests/test_short_video_repository.py tests/test_short_video_agents.py tests/test_short_video_ui.py -q
# 包含原有小说功能的完整回归。
.\venv\Scripts\python.exe -m pytest tests -q
# 新模块语法及差异检查。
.\venv\Scripts\python.exe -m compileall -q core/short_video core/gui/pages/short_video agents/writing/short_video_script_agent.py agents/review/short_video_review_agent.py core/evaluation/short_video_probe.py
git diff --check
```

覆盖：正常流程、确认门槛、一次修订、候选排序、失败与取消保留、参数快照、过期、重启、原子保存失败、目录隔离、请求次数/总时长/修订时间预留、空回复、超时、非法结构、评审异常、人工稿及定稿保护、模式切换和实际 Qt 后台任务。补充覆盖锁内来源检查、并发人工稿选择、需求冲突、内部数据损坏、活动生成锁保护、真实控件文字及列表编辑、人物改名引用更新和界面 Markdown 导出。

模型测试使用离线边界或可控 HTTP transport，不调用付费接口。小说回归使用现有 tests。仅运行小说入口不得创建短视频项目。

## 12 份样稿

固定六个创意：对白驱动（退还错拿的包裹、父女争论旧录音）；动作驱动（雨夜修灯、最后一班公交）；信息揭示（被擦掉的留言、两份不同日期的照片）。各生成 30 秒和 90 秒，共 12 份。

显式真实评估工具保存同一需求下的直接生成稿、新流程稿、原始请求响应、模型信息和调用次数。不自动认定哪稿更好；提供匿名 A/B 和私有映射，人工评价连贯性、细节、对白、节奏与可表现性。

```powershell
# 只创建 12 份固定需求，拒绝覆盖现有目录，不调用模型。
.\venv\Scripts\python.exe -m core.evaluation.short_video_probe prepare --output quality_runs/short_video_v1
# 明确发起真实模型请求。
.\venv\Scripts\python.exe -m core.evaluation.short_video_probe outline --output quality_runs/short_video_v1 --run-live
# 在应用中逐一打开 case_XX_30s / case_XX_90s 项目，检查、编辑并确认大纲。
.\venv\Scripts\python.exe -m core.evaluation.short_video_probe compare --output quality_runs/short_video_v1 --run-live
```

比较工具要求全部大纲已人工确认；未确认时在调用前停止。生成匿名稿与 ratings.csv，映射和调用记录标为 private；不要把私有文件发给评审者。比较目录已存在时拒绝再次调用，失败后保留已有材料。

本次已实际执行 prepare，在忽略目录 `quality_runs/short_video_v1/` 创建 12 个独立项目，核实每类 4 份、每种时长 6 份、编号唯一，全部没有大纲、脚本或模型运行记录。后续可直接从 outline 命令继续；不要再次执行 prepare 覆盖该目录。这些是评估需求，尚未生成真实模型样稿。

## 限制

时长是估计。单条剧情没有跨集账本；不生成镜头级分镜、图片或视频。模型审阅不能保证剧情质量。真实模型样稿未执行时，必须明确写明「未进行真实质量验收」。停止在当前请求返回或超时后生效，不强杀后台线程。

自动修订采用保守时间预留：剩余时间至少 240 秒，且剩余次数至少两次。首稿与首轮审阅累计超过约 60 秒时，本任务会保留候选及问题而不再自动修订；用户可明确发起新的修订任务。自定义项目目录和页面位置只在当前会话内保留；重启后手动打开原根目录即可恢复已有产物。

磁盘写入失败时原子替换保护旧项目，并停止新增模型请求；如果存储设备持续不可写，不能保证最新响应成功落盘。运行记录只保存安全的异常类型，不保存 SDK 错误正文或模型密钥。

## 本轮结果（2026-10-30）

最终代码验证结果：

| 检查 | 结果 |
|---|---|
| 短视频四个测试文件 | 67 passed，9.01 秒 |
| 完整 tests 回归（功能验收版本，需求预填调整前） | 1083 passed，5 subtests passed，115.38 秒 |
| 新模块 compileall | 通过 |
| git diff --check | 通过；Git 仅提示现有换行格式转换，无差异错误 |
| Qt 五页渲染与人工检查 | 总览、需求、大纲、脚本、成果均可显示；最终状态栏指向短视频目录 |
| 12 个评估需求准备与检查 | 通过，0 次模型请求 |

界面截图及离线格式示例保存在 `temp/short_video_audit/`（忽略目录），示例已验证人工定稿与导出。界面后台任务测试经过实际 SDK 调用边界，返回由离线客户端提供；超时测试使用可控 HTTP transport。未进行真实质量验收，没有以模型自评分宣称创作质量提升。本次未 commit、push 或发布。

### 新项目需求预填调整（2026-10-30）

需求页新增完整可编辑的「雨夜借伞」初始示例，已有项目仍使用已保存需求。调整后短视频四文件专项 67 项通过（8.32 秒）。另用临时目录中的实际 Qt 页面验证：不编辑即可通过需求校验并保存；显示示例时不自动写文件或请求模型；保存自定义内容后切换模式不会被示例覆盖。本次未重复完整 tests 回归，未调用真实模型。
