# Development Log

## 2026-09-09（续：全书时间线编号）

上一条的结论是「拓扑排序的查环那半边早就在了，缺的是图上一条边都没有」。顺着
它往下找，找到一件**不需要等边、今天就能查、而且每个真实项目都已经在犯**的错：
结构契约里的 `chronology_events.order`。

**这个字段声称是全书真实发生顺序，实际是各部分互不相干的局部计数。** 各部分分别
生成，谁也看不见别人用过什么号，而校验只查单条大于零。实测 current_work 六个部分：

| 部分 | 声明的 order |
|---|---|
| 1 | 1–4 |
| 2 | 6–9 |
| 3 | 31–34 |
| 4 | 13–15 |
| 5 | 13–15 |
| 6 | 1–3 |

第 4、5 部分并列，第 6 部分从头重编、与第 1 部分整段重叠，六个编号重复。仓库里
十一份结构契约无一例外全部撞车。下游拿这个字段排不出任何顺序。

**三处一起改，缺一处都不成立。** 提示词把前面各部分登记过的事件连同已占用的编号
发下去——此前只发未了结的悬念和已确立的真相，时间线一条不发，模型只能从 1 或从
本部分序号重新起编，撞车是必然的；逐段校验在拿到契约的当下就拦，因为那里还救得
回来，单段不合格会走带反馈的重试，报错原文直接当修复指令发回去；合校再兜一次底。

**规则只能定成「编号唯一」，不能定成「顺序递增」。** order 明写着是故事世界的真实
先后、不是读者得知顺序，所以后面的部分完全可以登记更早发生的事，倒叙合法，留空档
也合法。唯一站得住的约束是两条事件不能共用一个编号；真同时发生的两件事仍要各给一
个号，在 event 文字里说明它们同时。

**走错的第一步：差点把已经定稿的项目锁上。** 合校那个函数同时被流程门禁和总览页
的状态判定调用。第一版直接加了硬校验，current_work 的结构那一步立刻变成 blocked，
左侧步骤栏会给它挂一把锁——一本正文已经写完的书，因为一个它自己那次生成根本无从
避免的缺陷被判不合格，而且没法补救，正文一个字也不会因此变好。改成按契约版本分档：
版本升到 3，只有新流程写出来的契约才查这一条，旧档原样放行。实测 current_work 四个
阶段的状态与改前逐字一致。

**走错的第二步在提示词自己身上。** 第一部分没有已占用编号，那一版仍照着「下面这些
编号已经被占用」往下写，后面接一句「本部分是第一批」，再跟一个空数组，前后自相
矛盾。整段按有无前置拆成两个写法之后才通顺。

**实跑验证。** 在一份只有 lore 的副本上跑真实结构阶段：350 秒，零契约重试，三个
部分共 22 条时间线事件，编号 1–22，无一重复，合校通过。第 2 部分从 10 接上、第 3
部分从 17 接上，正是把已占用编号发下去的效果。

**Decisions & notes:** 这一条和前面七次同源，但坏得更深一层——前几次是字段语义只
写在校验代码里、没写进提示词，这次连校验代码里都没有，「order 表示全书真实发生
顺序」只活在 schema 的一句注释里。写进 schema 注释既不等于写进了校验，也不等于模
型收得到判据。真正的教训是：**凡是跨段共享的编号或 id，「谁来分配」和「怎么让下
一段知道已经分配了什么」必须在同一次改动里定下来**，否则每一段都会各编各的。校验
放在能重试的地方而不是最后合校处，是因为前者代价是一次重试，后者代价是整个结构
阶段（实测三幕 350 秒、六部分 465 秒）作废且无补救路径。给已有项目上新校验一律按
契约版本分档，不追溯——校验是为了让还没生成的东西更好，不是为了给已经交付的东西
判分。

测试 729 → 736 项，全部通过。current_work 自 2026-09-06 起未被写入。

## 2026-09-09

把昨天留下的那个需要定方向的问题做完了：叙事图此前从不被播种，于是那 1966 行代码
是一条只会误伤、不会生效的路径。方向选的是「从结构契约确定性地建图」。播种本身
半小时就写完了，**难的是它之后**——严格档校验从来没有真正执行过，每跑一轮就暴露
一层没人走过的路径，前后跑了五轮才收敛。

**播种本身不花调用。** 结构阶段已经声明了 `threads_opened`（id / 悬念 / 最晚第几
部分了结）与 `truths_introduced`（id / 真相 / 最晚第几部分揭晓），直接转成 thread
与 fact 节点即可。真相映射成 fact 而不是 reveal——第一版写成 reveal，校验当场拒绝，
才发现 reveal 节点要求有 clue 或 fact 前置（公平推理：揭晓必须建立在读者见过的证据
上），而结构阶段声明的是故事世界里成立的事实本身，什么时候揭给读者是账本里的运行期
状态。章号来自章节大纲：大纲按部分分文件、章号连续（实测 1–7 / 8–14 / 15–20），
每一部分的最后一章就是该部分悬念的计划了结章。

**五轮实跑的收敛曲线**（每轮都被端点 500 打断过，那是基础设施，不是代码）：

| 轮次 | 走到第几章 | 硬失败 | 重试 |
|---|---|---|---|
| 1 | 2 | — | — |
| 2 | 5 | 1 章 | — |
| 3 | 7 | 3 章 | — |
| 4 | 10 | 0 章 | 9 次 |
| 5 | **20（全部）** | **0 章** | **0 次** |

中间我说过一次「更差」，那是错的：三轮实际是一轮比一轮走得远，走得远才看见更多章
的失败。

**四层根因，全部是同一个毛病的不同深度。**

第一层，「先 open 再 advance」。`touch/advance/complicate/cross` 都要求这条线已经被
前面某一章打开过，只有 open 能起头。播种出来的节点在图上存在，但账本里一章都还没
打开它们，模型看见 id 就直接写 advance。

第二层，schema 示例在教模型编造 id。`"id":"PT-004-01"` 是按章号现编的，
`"via_node_ids":["图节点ID"]` 是占位符——示例演示的正是「编一个不存在的 id」。我上
一条改动在正文里写了「不要自己编造」，却没动示例，两边互相矛盾，模型跟着示例走。
改用图上真实存在的 id 之后又引入一个回归：示例的 node_id 换成了 fact 节点，却仍列着
完整的 transition 枚举，而 `execute` 只能用于 reveal 节点，于是第 4、5、6 章都报
「execute 不能应用到 fact 节点」。按节点类型裁剪枚举才修干净。

第三层是最值得记的一条：**提示词和校验读的是两个不同的数据源。** 第 4、5、6 章最后
一次重试的契约看起来完全正确——`primary_thread=PT001`、`primary_action=open`、有同
id 同 action 的 update、deadline 齐全、引用的节点也都存在——却仍判失败，因为 PT001
在第 4 章就已经打开过了，第 5、6 章又各开一遍。模型为什么反复开同一条线：提示词的
「已打开/未打开」来自 `planning_context.active_threads`，那读的是账本里的
`thread_status`，而账本只在章节**验收**时才写；规划阶段一章都还没验收，于是它对每
一章都说「一条都没打开」。而校验读的是已经累积的规划契约，知道这条线上一章就开过了。
两边各说各话，模型照着提示词做，必然被校验打回。改成都用
`build_existing_planning_index` 的 `open_plot_threads` / `closed_plot_threads`。

第四层，三个「收 id 的字段」从没说过自己收 id。`allowed_reveals` /
`forbidden_reveals` / `intentionally_silent_threads` 在 schema 里只写成空数组、没有
任何说明，模型往里填自然语言描述（「正衡律所管委会选择迎合汇达保理的商业利益而放弃
程序合规防线」），而校验把每一项都当节点 id。同一批还有 `facts_confirmed` 引用了图上
的 fact 节点——图上的 fact 是全书设计层的真相，`facts_confirmed` 要的是前面章节
`facts_added` 建立过的事实，两个不同的登记簿。

**最终结果。** 第五轮 20 章全部规划通过，零重试、零硬失败，2185 秒。六条悬念全部有
完整生命周期：PT004 开于第 1 章结于第 20 章、PT006 开于第 15 章结于第 17 章，
情节线动作分布 open 6 / advance 23 / touch 6 / close 6。叙事图从一条只会误伤的路径
变成了真正在记账的东西。

同日另修两条小的：正文里的编号写法（案号「HS-CONSTRUCTION-2018」被文风检查判为
外文、白烧一轮重写）——保持检查严格，把中文公文写法的例子写进写作要求，并说明
DNA、CT 这类通用缩写可以保留；界面上人物卡片的摘要显示英文枚举 `protagonist`，改成
优先显示外貌与背景，确实只剩角色时译成中文。

**Decisions & notes:** 这一路下来同一个模式出现了七次——**契约的字段语义只存在于校验
代码里，从没写进过给模型的提示词**。前几轮修的是「约束没告诉模型」，第三层那条更深
一步：告诉模型的和用来判他的根本不是同一份数据。往后新增任何契约字段或校验规则，
应当把「这条规则怎么讲给模型」和「用哪份数据讲」当成同一次改动的一部分，否则代价是
每章一轮白烧的调用，最坏是整个阶段作废。一版草稿曾把字段说明写成 schema 里的一个假键
`"__这三栏只收节点 id__"`，那会被模型照抄进契约，已改为放进散文规则。播种失败只记
日志、不阻断场景规划——它是加分项。短篇没有 structure_contract.json，图保持为空，
走已有的空图分支，行为不变。

测试 708 → 729 项，全部通过。

## 2026-09-08（续：从零跑通两条链路）

前面几轮改的都是既有项目上的东西——`current_work` 已经跑到第三十章，很多问题
被它既成的状态掩盖着。这一天从空目录建了两个新项目，一条短篇、一条长篇，四个阶段
一个一个跑过去，看每一步真正输出了什么。九处问题，其中三处会让整个阶段崩溃。

**第一处是我自己上一轮种下的。** 势力卡的地名限 12 个字，而模型写出的
「市公安局旧办公楼地下一层档案室」「棉纺三厂职工宿舍四号筒子楼水房」都是 15 字
——它们恰恰是提示词要的那种具体地点。字数根本分不开地名和描述：15 字的具体地点
和 16 字的机构描述一样长。更糟的是这条约束**只写在报错里、没写进提示词**，模型
从头到尾不知道它存在，只能靠重试撞，两次撞不中整个设定阶段崩溃、零产出。上限放宽
到 25 字（只拦「明显是一整句话」），判据交回给抽象后缀表，限制写进提示词。同一轮
还有一条同源的：`has_latin` 是「出现任何一个拉丁字母就算外文」，把「海陵资金监管
A座十二层」判成了外文——中文楼宇门牌本来就写「A 座」「B 栋」。判据改成与正文文风
检查共用一份，单字母与 DNA、CT 这类缩写不算，成串字母才算。

**空叙事图上，场景规划必然失败。** 契约校验按「有没有引用图节点」分两档：零引用
是合法的，`current_work` 一路三十章都跑在空图上；一旦引用了任何节点，
`primary_thread`、`primary_action`、`via_node_ids`、`plot_thread_updates` 全部
变成必填且必须指向真实存在的节点。而 schema 里仍写着「从 active_threads 选择」
并给了一条 `advance` 的示例——空图时那几个列表全是空的，模型只能自己编一个情节线
id，契约当场升级成「图支撑」，六条规则一起报错。查下来**生产代码里没有任何一处会
往叙事图里加节点**，只有 `tools/run_e2e_10_chapters.py` 会。也就是说这条不是偶发，
是新项目必然踩中；`current_work` 躲过只是因为它的契约恰好从未引用过节点。改法是
图上没有可用节点时，提示词明确要求那几个字段留空。

**结构阶段的全书合校有校验、没有补救。** 各部分是分别生成的，全书校验要求同一个
truth id 在各部分说的是同一件事。但后面几部分只收到「尚未了结的悬念」，没收到
前面已经声明过的真相，于是第二幕用不同措辞重新定义了 T001，等到三幕全部生成完
才在合校时报错——465 秒、八次调用的产出一起作废，且无法修复只能重跑。新增
`truths_after()`，与既有的 `open_threads_after()` 对称，把已声明的真相连 id 带
fact 发给后面各部分。

**短篇的写作回调从 2026-09-06 起就是坏的。** 那天给长篇加章节衔接时，写作回调多了
一个 `continuity_rules` 参数，短篇那条路径漏改，于是短篇跑到最后一个阶段直接抛
TypeError，前三个阶段约 11 分钟的产出全部作废。补上参数并往下传，同时收
`**kwargs`；新增一个不打模型的用例，直接比对章节循环发出的关键字与短篇回调的形参，
两条路径的签名不允许再各走各的。

**短篇的场景数没有上限。** 提示词只说「把故事拆分成一系列清晰、独立的场景」，模型
给了 10 个。而下游把整篇短篇当作**一个章节**送进质量闸门，那套闸门的重修预算
（2 轮）与分数门槛都是在 2–3 场的章节上实测定下来的。10 场进去的结果：三轮章节级
评审分数一路走低（3.524 → 2.429 → 1.905），`NEXT_SCENE_PREMATURE` 与
`PROCEDURAL_IMPOSSIBILITY` 反复出现，30 分钟、28 份评审之后判不通过，整篇进归档
作废。改成要求 3–6 个场景之后重跑：5 个场景，正文 15 分钟，首评 3.524、一轮重修
后 4.0 分通过，评审 16 份。**没有动重修预算和分数门槛**——三轮分数是往下走的，
加预算没有数据支持。

**长篇规划里有一场重试风暴。** 20 章的规划里有 6 章（3、8、11、15、18、20）栽在
同一条规则上：「连着三章不能是同一个 chapter_function」。这是硬规则，而前两章填了
什么从没发给模型，只能靠失败重试去撞，每章白烧一轮规划调用。另有一章把
`primary_action` 才有的 `complicate` 填进了 `chapter_function`——两套枚举只共用
`advance` 一个词。两条都改成在衔接说明里提前讲清楚。

**最后一处不影响流程，但一直在骗人。** 长篇第 1 章的日志里连报三次
`Content lacks proper sentence structure`，查下来 `review_agent` 那套启发式整体
只认英文：句子按 `[.!?]` 切（中文用「。！？」，于是合格的中文稿整篇被算作一句话）；
大写率对中文恒为 0，那 +0.1 中文稿永远拿不到；按空格切词在中文里等于按行切，
`unique_ratio` 恒约 1.0，那 +0.1 反而白送。后两项偏差方向相反，**恰好把分数固定
住**——沙盒报告里连续三章都是「质量：0.77」，本轮是 0.78/0.77/0.78，这个展示给
作者的数字与正文内容无关。改成按语言分流后，正常中文 0.90、高度重复中文 0.50。

**Decisions & notes:** 七处既有缺陷里有五处是同一个模式——约束是确定性的、在提示词
构造时就完全知道，却只通过「校验失败→重问一轮」传达给模型。代价轻则每次一轮白烧
的调用，重则整个阶段作废且无法补救。修法统一为把已知的约束提前写进提示词：已声明
的真相、前两章的功能定位、两套枚举的归属、场景数上限、空图时该留空的字段。这类修改
不花任何额外调用。叙事图那条留了个更大的问题没动：既然生产代码从不往图里加节点，
那整套图契约校验目前是一条只会误伤、不会生效的路径，要么补上建图的一步，要么把它
降级——这需要先定产品方向，没有擅自决定。短篇正文里模型写的案号「HS-CONSTRUCTION
-2018」被文风检查判为外文，这一条没改：正文里的机构与编号本来就该用中文，模型改得
掉。界面上人物卡片的摘要显示的是英文枚举 `protagonist`，旧卡也如此，属既有问题，
本轮未动。

四个阶段的实测耗时（从零建项目）：短篇设定 271 秒、结构 258 秒、场景 79 秒、正文
897 秒；长篇设定 368 秒、结构约 465 秒、场景规划 3227 秒（20 章，最贵的一段）、
写第一章 367 秒。测试 683 → 708 项，全部通过。

## 2026-09-08

这一天要回答的是前一天留下的验证缺口：昨天的整改只量了提示词长度和测试数，
没有在真模型上跑过。今天在 current_work 的副本上连写第 23、24、25 章，并用全新
参数目录生成了一份新 lore。结论分两半——世界观那一条成立，提示词重排那一条
**没有换来可证实的收益**。

**先说没达到预期的那一条。** 昨天把评审提示词按稳定度重排，回放预测可复用开头从
0.1% 涨到 72.4%。实测对上了：第 23 章 73.9%、第 25 章 73.6%（去掉首份 plan 评审
——它跑在规划修订之前，手上的契约本来就不同）；连把 plan 评审算进来也有 44–45%，
因为评分标准与案件底稿现在排在契约前面，契约不同的那一次也能共用开头。但**长度
不等于收益**。三章合计 91 次 → 89 次调用、27.3 → 25.2 分钟，都在单次样本的波动
范围内。中途我一度按跨轮对比（每次评审 17.9s → 10.9s）断言「缓存确实生效了」，
这个结论下早了：改用同一次跑内部的冷热对比，第 24 章像冷启动（首次 38.3s、随后
12–15s），第 23 章却反过来（首次 8.6s 快于后续均值 11.1s），两章互相打架。

于是做了受控实验，把生成长度压到 3 个字以隔离预填充：同一段 23,306 字的提示词
连发三次是 3.84 / 1.98 / 2.26 秒；**改掉开头**（前缀必然失效）是 1.98 秒，和热重复
一样快；每次换一个新开头的对照组是 4.05 / 2.67 秒。改前缀不变慢，说明这个端点上
前缀缓存要么没开、要么收益淹没在噪声里。而且预填充本来就只占真实评审调用的两成
左右——23,306 字只生成 3 个字要 2–4 秒，真实评审要 12–14 秒，其余都花在生成那
约 2,000 字上。**即使缓存完美命中，可省的上限也就是那两成。** 重排没有坏处（前缀
确实变长了，换个支持得更好的端点就能兑现），但今天它没有省下任何可测量的东西，
不该记成收益。

顺带一个没预期到的数字：「打了低分却没给改法」的补调用从每三章 9 次降到 5 次。
可能与重排把「每一个 3 分或更低的维度都必须在 upgrades 里出现一条」这条要求挪到
了正文**之前**有关，但单次样本说明不了因果，只作记录。

**世界观那一条成立。** 用一份全新参数（悬疑·警察程序、主题写的是旧案证据在城市
改造中被逐块抹去）生成 6 个势力、8 位人物：势力的 nature 与名字全部相符，
territory 全是真地点，两个反派的 opposes 都指向名单里真实存在的主角与其真实目标，
没有空字段、没有残留英文，女性占比 4/8 正好命中设定的 50%。落盘前的校验在实跑中
真的拦下过一次——第一稿某位人物的 goal 里残留英文，失败原文发回去，重试即通过。
对照旧数据（反派目标抽到「Help solve the case」、势力名是民营咨询公司而 type 写着
Police Department、「Court and legal systems」被当成地名转成城市），这是根子上的
变化。三章成章验证也全部通过：23/24/25 章全部验收、零人工放行、零待复审、零失败
调用，合议分 3.182 / 3.636 / 3.824。

**中间走错的地方有四处，都是同一类：只在自己写的桩数据上验证过。**

第一处，`Female Percentage: 50` 从 parameters.txt 读回来是字符串，无界面直接调用
时 `"50" / 100.0` 直接抛 TypeError，整个设定阶段崩掉。昨天写的用例传的是 int，正好
绕开——这正是 `test_lore_name_localization` 开头写明的那种「导入和编译都通过、按下
按钮才炸」。

后三处是同一个根因、在真实数据一渲染就全暴露了：新卡用单数 `goal`/`flaw`/
`strength` 与 `description`/`background`，而四个读取方都只认复数数组与
`appearance_summary`/`backstory_summary`。后果按严重程度排：**章节写作的人物名单
除了姓名、角色、性别、年龄之外全是空的，外貌显示「无」**——模型写这本书时看不到
任何一个人物想要什么、怕什么；结构阶段的名单是人物弧光生成的唯一输入，同样读空；
短篇名单和势力摘要的「主要目标」一栏也全空。已合并成一个共用的 `first_field()`，
两种形状都读，并顺带把新卡才有的所属、职业、职务、动机、弧光和「反派挡住谁」也
补进各处名单。

还有两处操作失误：第一次跑漏了 `--approve-contradictions`（脚本里写明「无人值守跑
必需」），第 23 章停在人工裁定闸门，白跑一轮；提交那一版时 `sandbox_verify/` 被收
进了仓库，因为 .gitignore 里只写了 `sandbox_run*/`，已补规则。

**Decisions & notes:** 提示词重排保留但不再当作性能改动记账——它的价值是「前缀
可复用」这个结构性质，在当前端点上兑现不了。要兑现需要的是端点侧开启前缀缓存，
或者换一个按缓存计价的托管端点；今天的数据不支持在此基础上再做优化。势力的
territory 有 4/6 填的是该势力自己的办公楼（「沿港分局刑侦小楼」），校验拦得住
「××系统」这类统称、拦不住「机构把自己门牌当活动地点」，这是校验强度的上限，没有
擅自加规则。人物卡的数量仍由界面步进器传入、不存进 parameters.txt，无界面跑固定
用默认的 6 和 8，这是既有行为。界面上人物卡片的摘要显示的是英文枚举
`protagonist`，旧卡也如此，属既有问题，本轮未动。current_work 全程未被改动，跑前
跑后 1060 个文件的指纹一致（`2ef4b96a…`）。测试 679 → 683 项。

## 2026-09-07（续：全项目审查后的整改）

一次全项目审查之后的集中整改，五件事，按「先让它别坏、再让它别贵、最后让它别错」
的顺序做，每一步都有可复现的度量。

**一个能复现的永久卡死。** 旧项目（`narrative_graph.json` 还是迁移前形状、运行期
字段留在节点上）做章节验收会永远停住：验收先拿账本锁，中途现场新建叙事图管理器，
管理器构造时的旧格式迁移先拿叙事图锁、再回头拿同一把账本锁。`FileLock` 的实例各自
计数，同线程嵌套即自锁；而全项目 12 处 `FileLock` 用的都是默认 `timeout=-1`，所以
表现不是报错，是状态栏什么都不显示、「停止」也按不动——线程阻塞在 `acquire()` 里，
根本走不到取消检查点。新增 `core/generation/locks.py`，`is_singleton=True` 让同线程
嵌套变成计数加一，并给出有限超时；`accept_chapter` 把管理器移到锁外构造，锁顺序统一
为「先叙事图、后账本」。**目前只有测试在调那个入口**（正式流程走的是
`accept_chapter_with_delta`，它一直是对的），所以这是一个潜伏缺陷，不是正在发作的
故障——但两把锁在代码里存在两种相反的获取顺序，以后谁在账本锁里碰一下叙事图就会中招。

**提示词重排，可复用开头 0.1% → 72.4%。** 同一章的 8～11 次评审之间，89% 的内容
（案件底稿、章节契约、故事账本、评分标准）逐字相同，但公共前缀只有 20 个字符——首行
那句「评审 scene_1」把每一次调用都岔开了。块序改为按「多久变一次」排，阶段名移到
「本次评审对象：」。用 `sandbox_run` 的真实轨迹回放测量，三章 30 次评审的可复用开头
从 20 字涨到约 19,000 字（13k token）。**这一条只测了前缀长度，没有测端到端耗时**：
那需要在自建端点上真跑一遍。

**取消的等待从「整章」缩到「一轮合议」。** 评审阶段不设检查点是 `cancellation.py`
写明的有意取舍（半路停下会白扔一整章），这一条没动。但一轮章节级重修要再花约十次
调用，而这一章尚未验收——停在那里丢掉的与「下一场之前」停下同类。检查点加在重修轮
之间，按实测调用时长，最长等待从整章剩余的全部重修轮缩到约一分半。

**删掉从未接到界面上的那一层，约 4,900 行。** 界面的「运行完整流程」走的是
`stage_pipeline` 逐阶段执行，`step_runner.py` 里写明这是刻意选择，所以
`StoryGenerationOrchestrator`（2,256 行）、`MultiAgentOrchestrator`、
`ConsistencyAgent`、`QualityControlAgent`、`BaseTool` 一直没有调用者。随之修掉两处
界面与配置的谎言：「重置工作流状态」清的是一份自界面改版后再没有任何代码写入的记录
（`current_work` 里那份停在 8 月 21 日），按钮移除；`backgrounds_dir` 被声明、被创建，
但背景故事一直平铺写在 lore 目录下，于是每个项目都凭空多出一个空目录。四份 agentic
文档开头加了说明——它们此前把这套已死的层描述成现行架构。

**世界观数据改由模型直接写中文卡片，删掉 12,593 行英文随机词库。** 这是影响成品
质量的一条。人物卡的 `goals`/`flaws`/`profession`/`title` 原本是从各十来条英文短语里
分别随机抽的，与故事前提无关；名字换成中文之后其余字段原样留下。留下的实例：势力名
是「海陵岸线风险咨询」而 `type` 写着 `Police Department`；模板里的 `territories`
写的是「Court and legal systems」这类描述性短语，被当成地名转成了城市「雾平」「榆州」；
反派抽到的目标是「Help solve the case」；两位搭档警察被标成「Victim and perpetrator」。
新增 `core/generation/lore_cast.py`，模型按题材、书名、主题、基调直接写中文卡，落盘前
做五项确定性校验（必填字段、无拉丁字母、机关不能叫公司、地点不能是统称、反派必须写明
挡的是谁的哪个目标），不通过就把失败原文当修复指令发回去重写。

**Decisions & notes:** 评审的结构化输出这一条**按错误的数字立项，查证后放弃**。原判
「11% 的调用在修评审自己的格式」，逐条分开看是 9 次「打了低分却没给改法」加 1 次
schema 不合格加 1 次截断重问——前者是跨字段的语义约束，`response_format` 与
JSON schema 都表达不了，真正能省的只有 1 次。为这个在本仓库另开一条绕过
`llm-backends` 的直连路径不划算（会把当初合并掉的重复又引回来），改记进 ROADMAP：
把分数与改法合并成同一个对象，让「低分而无改法」在结构上写不出来，属于评审契约变更，
需要实跑验证。写正文的提示词也量过，本来就有 4,061 字公共前缀、每章仅 5 次调用，
收益有限，没动。

Python 总行数 69,235 → 52,223；测试 670 → 679 项，全部通过。本轮所有度量都来自
`sandbox_run` 里那次真实的三章运行轨迹或本地回放，没有新跑生成。

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
