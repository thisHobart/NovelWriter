# Development Log

## 2026-09-07

跟着昨天的章节衔接往下走，这一天处理的是**章内场次之间**的越界：写第 N 场的模型
把第 N+1 场的戏提前写了，代价是多跑一轮章节级重修（约十次调用）。起点是一个合理
的质疑——「不要提前完成下一场的任务」是一句提示词，这种核心流程该由代码控制。查
下来发现两道防线都不是代码：提示词里的那句话，和一个由模型判定的硬失败代码；而且
提示词在说「别写下一场」的同时，把下一场规划的前 5000 字（含环境、出场人物、编号
的关键事件表）一并发了过去。

分工厘清成三段：**代码决定发什么给模型**、**评审判写出来的算不算数**、**代码决定
判完之后怎么处置**。生成式的一步没办法禁止模型写出某段文字，能管的只有前后两头。
中间那段试过改成确定性统计，用真实数据证明做不到：把当初被判越界的那一稿从调用
轨迹里取回来，算了三种判据（与下一场规划逐字共用的片段、下一场专属的 2–3 字词、
再加一层「全书前面章节从未出现过」的过滤），没有一种能把越界那一稿和改好那一稿
分开，逐字片段那一条还反过来在干净的场次上误报。原因是越界的稿子不抄规划的字：
规划写「当庭敲响法槌」，正文写「法官在审判台上重重敲下法槌」，一个字都不重合。
跨章套语能用统计判，是因为那里的缺陷本身就是逐字重复；场次越界的缺陷是「同一件事
被提前写了」，措辞可以完全不同。

发什么给模型这一头改了两次，**中间走错一步**。先把下一场规划削成只给标题行——越界
没有消失，只是从第 24 章挪到了第 23 章，因为材料还有第二份：章节契约里的场次边界
那一栏一句话就把后面几场写明白了，而那一份的抬头写着「必须兑现」。于是把后面场次
的边界也从契约副本里删掉，**这一步是错的**：第 23 章一章内越界 2 次，三轮定向重修
分数从 3.14 一路掉到 1.96，最后进了待复审。那几行同时还在当栅栏用，模型靠它们才
知道自己这一场到哪儿为止，删掉之后不是变谨慎，是失去参照，一路写到底。最终版是
信息一个字不减、只换名义：后面场次从「必须兑现」的清单移到「一个字都不许碰」的
清单，第 23 章随即回到验收。

第二件事是让规划层面的缺陷不再由重修硬扛。有些硬伤重写多少遍正文都不会好，因为
根子在规划里——规划写着「梁浩在'海陵先驱号'上只有不到四十分钟」，而上一章结尾的
倒计时是十四分二十秒；另一章的规划写死了一句台词，现实合理性每轮都报精度无据。
判据是确定性的且不花调用：每条硬伤都必须附正文逐字引文，把引文拿去和场景规划比，
共用片段达到 12 个汉字就算「正文只是照规划写的」。门槛按实测定（真出在规划里的
三条命中 13、19、20 字，正文自己的问题全在 7 字以下），在两轮的全部评审记录上跑
过，只命中四条真缺陷，零误报。

**这里也走错了一步，而且连错两次。** 第一版一检测到就把这些条目全撤出重修清单，
结果重修手上什么都不剩，同样的问题原样留到最后，第 23 章从验收退成待复审——比不撤
更糟。「引文照搬了规划」不等于「正文没救」：规划那句话往某个方向推，正文往往仍有
回旋余地（「在船上」改成「正在登船」就能解掉空间矛盾）。改成「挺过一轮再撤」之后
差点又跑掉一轮验证：那一版挂在「同一条修复要求原样退回来」上，而四个副本的调用
轨迹里这个标记出现 0 次——评审每轮都把同一个问题换个说法重写，整条文本逐字比对
永远匹配不上。最终按**引文**判：正文那句话没改，评审下一轮还会引同一句。第六轮
的数据回放确认这条会正确触发——同一句引文被引了七次，三轮重修一次都没解掉。

改完之后的第七轮是七轮里最好的一次：第 23、24、25 章全部验收，各只用一轮章节级
重修，零放行、零待复审、78 次调用——「对话开着」的配置里第一次做到 1/1/1。而且
它把第六轮的误判钉死了：那条在四轮里反复出现的规划台词，这一轮**一次重修就解掉**
（3.41 → 3.96，零硬伤）。它此前解不掉不是因为正文改不动，是每轮重修被别的条目挤
占——判它「没救」判早了。新规则在第七轮只产出信息、没有产生任何拦截，这正是它该
有的保守程度。

**Decisions & notes:** 场次越界最终停在「每三章约 1 次」，五轮实测（整段发 / 只给
标题 / 删掉边界 / 禁令写法，各跑同样三章）没有一版低于这个数，而每次都被闸门抓住
并修好，没有一次漏进正式稿件。判定这一段留给评审不是妥协，是有数据的取舍。差分
放行的记录顺手修了一处看不懂的地方：章节级是四份评审合议，差额只来自其中一份，
原先报的却是合议后的平均分，写出「平均 3.59 分、门槛 3.20 分，仍差 0.08 分」这种
自相矛盾的记录；现在点名是哪一份短了。测试从 639 增至 666。

## 2026-09-06

章节之间接不上是这一天要解决的问题：每一章开头都重新锚定时间、重新描写地点、重新
介绍在场人物，读起来像每章重新开书——第 18、19、20 章连着三次把同一个防空洞从头
铺了一遍。根子在规划，章节契约里没有任何一个字段负责回答「这一章从上一章的什么状态
接过来」，三十份契约的相关字段全是空的，`chapter_function` 二十三份没填、七份写着
`advance`。新增 `core/generation/chapter_continuity.py` 把这件事分成三层：契约层要求
第二章起必填 `continuity`（承接的动作或悬念、与上一章结尾的时间差、开场时人物各在
哪），空着就按契约不合格走已有的规划重试；写作层把这三项作为第一场的硬要求发下去，
并附上最近三章已经建立过的地点与已经露过面的人物，明确禁止重新铺陈；验收层新增一份
不花调用的确定性检查，与契约、读者盲测、现实合理性三份评审一起合议。读者盲测同时
增加 `chapter_continuity` 维度和 `CONTINUITY_BREAK` / `REDUNDANT_RESTAGING` 两个硬
失败代码，沿用「必须附正文逐字可检索的引文」这条既有约束。旧项目的契约不重新规划，
改走定点补写：只问缺的那几个字段，一次调用，场景规划的 Markdown 一个字不动。

在 `current_work` 的副本上连写第 23、24、25 章验证（`tools/run_sandbox_chapters.py`，
定稿正文与账本一个字没动）：三章全部通过闸门并验收，无人工放行、无待复审，章节级
重修分别是 1、2、1 轮，共 91 次调用。相邻两章开头的共用套语片段从第 18–21 章的
11 条 / 74 字（每对 3.7 条）降到 2 条 / 11 字（每对 0.7 条），三对里没有一对超过门槛；
三章开头与上一章结尾的字面回指分别是 7、1、5 处，`chapter_function` 分别是 reveal、
advance、aftermath。测试从 576 项增加到 639 项，全部通过。

同一批三章又跑了一遍 `Scene Dialogue: off` 作对照：同样三章全部验收、零放行、零待
复审，章节级重修 1/1/1 轮、73 次调用，套语 1 条、承接锚点 18 处。对上开着的那一遍
（1/2/1 轮、91 次调用、套语 2 条、锚点 13 处），这一次没有量到多轮对话提高命中率，
多花的 25% 调用倒是量到了。三章一遍的样本定不了结论，但目前没有支持「默认开着」的
数据。

**Decisions & notes:** 时间读数的跨章检查只有「同一个日标签下时间倒流」判为阻断——
日标签是模型自由书写的（「案发第三日」「决战清晨」「决战当夜」），跨标签本来就排不出
先后，把排不出来的也判成错误只会让作者去伺候一个猜的规则；标签回退与漂移只作提示。
悬念长期沉默同样只作提示并写进下一章的规划提示词：一章本来就不可能碰到每一条线。
`OPENING_BOILERPLATE_REUSE` 的门槛按已验收章节实测定（相邻章最长共用片段 11 个字、
每对 1–8 条），并额外加了「单条达到 20 字」这一条——整段照抄只会留下一条很长的片段，
只看条数反而漏掉最露骨的那种。已验收章节的场景规划不再重新生成：正文已经落盘，
重规划只会让两者对不上。第 24 章多跑一轮章节级重修的原因是重修后的第二场报了
`NEXT_SCENE_PREMATURE`（写进了第三场的地盘），这是重修越界、不是衔接问题，留作下一
轮的首要改进项。

## 2026-07-28

Follow-up cleanup pass on the `llm-backends` migration, catching two spots the July 15 adoption had left inconsistent with the new package-backed registry and moving the pin up to v0.2.0. The most consequential fix was in `chapter_writing_agent.py`: its headless fallback (used whenever there's no GUI app instance to ask for the selected model) still hardcoded the literal `"gpt-4"`, which is no longer a valid registry key and so raised `ValueError` on any run without the app. That now falls back to `ai_helper.DEFAULT_API_MODEL`, so the value tracks the registry instead of drifting from it. Second, `load_parameters` in `core/gui/parameters.py` was silently dropping any model spelling not already in the available-models list, which meant older `parameters.txt` files carrying pre-migration names (e.g. `claude-4-5-sonnet`) lost their model on load; it now routes unknown spellings through `llm_backends.resolve_model()` first and only warns-and-ignores if that also fails. The requirements pin moved to `@v0.2.0`.

**Decisions & notes:** The v0.2.0 bump is behavior-neutral here: the package's 0.2.0 change (no default system prompt) is a no-op for NovelWriter because the `ai_helper` facade already passes `role_description` explicitly on every call. The 16-test suite from the migration still passes.

## 2026-07-15

The day's arc was the shared `llm-backends` migration, bookended by a hotfix and its retirement. It opened with a security-flavored interim fix (step 0 of the plan tracked in StoryDaemon's `docs/LLM_BACKENDS_INVENTORY.md`): because `ai_helper.py` loads `.env` into `os.environ`, the codex/claude/gemini CLI backends were inheriting provider API keys, and agent CLIs treat an environment key as outranking their configured subscription login, so CLI generations were being silently billed to metered keys (codex was also bypassing its sandbox). A new `_env.subprocess_env_without()` strips the relevant provider keys from a per-call copy of the environment, so the failure mode flips from silent spend to a visible auth error. Later the same day the real migration landed (step 4): NovelWriter adopted `llm-backends` v0.1.1, collapsing its two disagreeing internal model registries into package-backed shims, with `llm_interface.py` kept as a thin wrapper for the four NovelWriter-specific needs and `ai_helper.py` as a facade over the package's registry/dispatch. This retired the morning's `_env.py` hotfix (the package now strips keys by default), moved CLI backends onto the package's neutral-cwd isolation and read-only codex sandbox, and shipped the first real test suite for this layer (16 tests, plus un-ignoring `tests/` in `.gitignore`, a legacy rule that would have silently discarded the new suite). A small unrelated commit added an optimized WebP thumbnail alongside the existing PNG for use as a web card image.

**Decisions & notes:** The model list was intentionally pruned as part of step 4: `gpt-4o`, `o3`, `o4-mini`, `gpt-5-2025-08-07`, and `gemini-2.5-pro-exp-03-25` now raise `ValueError` with the supported list, and the default API model moved to `gpt-5.5` (was `gpt-4o`). `claude-4-5-sonnet` resolves via the package alias table, but `claude-4-5-opus` was deliberately NOT aliased to `opus-4-8` because that would be a silent model upgrade; it errors loudly instead. Saved `parameters.txt` files referencing retired models fall back to the default via the pre-existing guard. A live smoke test via OpenRouter (deepseek-chat) confirmed the path end to end.

## 2026-07-14

A project-housekeeping day focused on making NovelWriter's status and direction legible from the repo itself. The morning brought GitHub Sponsors support: a `.github/FUNDING.yml` config plus a sponsor badge in the README. The bigger work was creating `ROADMAP.md`, which collates the completed core/ + agents/ refactor (signed off August 2025 in `docs/refactor_plan.md`) and the since-shipped agentic framework milestone (base agent/tool abstractions, specialized agents, orchestration, checkpointing, multi-level review, and the multi-backend LLM interface) into one checklist-style document. A follow-up pass then added a "Cross-pollination from StoryDaemon" section, planning to port tension/arc-pressure control, grounded name generation, contradiction detection, wiring up the dormant ChromaDB RAG memory, and making the QA review loop actually enforce retries, with the full rationale living in StoryDaemon's `docs/CROSS_POLLINATION.md`.

**Decisions & notes:** The roadmap deliberately marks the refactor checklists complete based on the plan's own sign-off even though the original document left them unchecked. New backlog items acknowledge known debt: no test suite for core/ and agents/, dead code to remove (IntegratedStoryOrchestrator, AdaptivePlanningAgent, the mismatched top-level `generate_story()`), and a possible shared LLM-backend package spanning NovelWriter, StoryDaemon, and LLM-Remote-Runner (home/name/distribution still undecided).

## 2026-06-20

Reconciled the README against the current codebase after a refactor had moved several modules around. The documentation still pointed at old locations, so paths were corrected to reflect the new layout — `combine.py` now lives at `core/utils/combine.py` (previously described as being in the root directory) and `ai_helper.py` at `core/generation/ai_helper.py`. The configured-models list was also brought up to date: Gemini 3 Pro and Claude 4.5 Opus were added, and o3 / o4-mini were removed from the Deprecated list since they are in fact still active per `ai_helper.py`.

**Decisions & notes:** This was a full-audit pass of the README rather than a one-off fix, aimed at keeping the docs trustworthy as the code is refactored.
