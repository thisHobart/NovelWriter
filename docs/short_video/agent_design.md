# Agent 与质量循环

## 数据

需求：title、idea、audience、genre、tone、duration_seconds、ending、character_limit、location_limit、required、forbidden。

大纲：title、characters（id/name/motivation/appearance）、locations（id/name）、conflict、beats（id/action/duration_seconds）、ending。

脚本：title、characters、locations、scenes（id/location_id/character_ids/action/dialogue、narration、sound、duration_seconds）；对白为 character_id/text 数组。

评审：五个维度 continuity/details/language/visual/rhythm，各为 1–5 分；issues 为 code/severity/quote/suggestion/target 数组，severity 为 hard/soft，target 为 script/outline。

模型输出必须为 JSON；界面以中文字段树编辑，人物和地点通过名称选择，长动作和对白可在文本框编辑，旁边实时显示 Markdown 预览。内部编号不作为用户操作内容。评审引文必须能在完整脚本中找到。目标时长与场次时长是估计，偏差超过 max(3 秒, 目标的 15%) 时作为本地问题反馈。

## Loop

```text
需求 → 生成大纲 → 人工编辑并确认
                   ↓
       完整脚本生成 → 本地校验 → 整篇评审
                              ↓ 有可执行意见
                    完整修订一次 → 复评
                              ↓
                    已审阅候选 / 待修改
                              ↓
                         人工编辑与定稿
```

大纲最多 2 次底层请求；脚本任务最多 4 次（生成、评审、修订、复评）。所有空回复、网络和格式重试计数；SDK 重试关闭。每次请求最多 120 秒、整个任务最多 300 秒。自动修订前必须剩余至少两次请求，并为修订和复评各预留 120 秒（共 240 秒）；意见不能仅针对大纲。不满足次数或时间条件时保留候选和问题，供用户主动修订。大纲问题提示用户修改确认，不自动重写确认内容。

保留每份非空原始响应、合法候选、评审、任务状态和来源指纹。排序依次为已通过、硬问题少、软评分高。自动修订携带完整原稿、需求、大纲，以及包含原文证据、类型和建议的完整问题条目。修订失败不丢前稿，取消先保存响应再停止。用户主动修订是独立任务，不改变原版本。

人工版本追加与当前版本选择在同一事务中保存；生成任务的候选选择与运行结束记录也在同一事务中提交，并保护当时已选中的人工稿或定稿。需求和大纲编辑采用来源指纹检查，外部窗口已修改时拒绝覆盖并保留当前编辑。加载时校验项目内部数据，损坏文件不自动修复或覆盖；旧产物依据过期后仍可读取。

## 语义检查

- 连贯性：动机、因果、人物知识、时间地点、道具状态。
- 细节：关键变化有具体、可见动作支撑。
- 语言：可朗读、有角色差异，少解释与口号。
- 视频表达：内心外化、角色外观和动作连续。
- 节奏：开场建立观看理由，中间推进，结尾回应冲突。

模型结论与人工定稿分离。评审无效或不可用时保留候选，状态为未检查，不能伪装为通过。
