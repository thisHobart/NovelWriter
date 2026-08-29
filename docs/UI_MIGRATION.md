# PySide6 迁移方案

行为规格在 `docs/UI_CONTRACT.md`，本文件只讲**代码结构与推进顺序**。

## 1. 为什么是 PySide6

`core/gui/` 8100 行纯 tkinter，全项目没有一处 `ttk.Style`。观感瓶颈在长文本区：
customtkinter 包不住 `tk.Text`，而设定生成、故事结构、章节撰写的主体都是大块文本。
PySide6 用 QSS 能完全掌控 `QTextEdit` 与 `QTableView`，代价是视图层重写。LGPL，商用无碍。

## 2. 目录结构

```
ui/
  theme.py            设计 token + 全局 QSS（唯一样式来源）
  icons.py            内联 SVG 图标（无 emoji）
  task_runner.py      QThreadPool + 信号，替代 core/gui/task_runner.py
  params_store.py     参数文件读写，与旧版共用磁盘格式
  story_options.py    篇幅 / 结构 / 子题材选项表（不依赖 tkinter）
  services/
    artifacts.py      从磁盘读各阶段产物（纯读取，永不抛异常）
    step_runner.py    把「跑一个阶段」包装成 TaskRunner 的 work
    legacy_step.py    子进程里驱动旧 tk 逻辑（过渡件，见下）
  widgets/            卡片、页头、步骤栏、状态栏、检查器块、表单行、步进器
  pages/
    stage_base.py     四个阶段页的共同骨架
    workflow_page.py  工作流总览（落地页）
    parameters_page.py / lore_page.py / structure_page.py /
    scene_plan_page.py / chapter_writing_page.py
  main_window.py      装配 + 路由 + 任务分发 + 状态分发
  app.py              入口
```

## 2.1 生成逻辑目前怎么调用（过渡方案）

真正的生成逻辑长在 `core/gui/lore.py`、`story_structure.py`、`scene_plan.py`、
`chapter_writing.py` 这四个 **tkinter 类**里；编排器
（`agents/orchestration/story_generation_orchestrator.py`）是靠
`app_instance.lore_ui.generate_factions()` 这样"点按钮"驱动的，中间还会
`root.update()` 泵 Tk 事件循环。

两个 GUI 框架的事件循环塞不进同一个线程，Tk 又不允许跨线程调用，所以新界面
把整段旧逻辑放进**子进程**执行（`ui/services/legacy_step.py`）：

* 子进程建一个 withdraw 的 Tk 根窗口，旧代码原样跑；
* 进度以 JSON 行写到 stdout，父进程转成状态栏进度；
* 取消 = 终止子进程，不污染 Qt 主进程。

代价是：分步按钮（生成势力 / 生成人物 / 生成世界观）目前只能整段驱动一个阶段，
没法真正单独跑。把生成逻辑从 tk 类里抽出来之后（第 6 步），
`step_runner.py` 改成直接调用，`legacy_step.py` 整个删掉。

分层铁律：

* `widgets/` 不认识业务，只认识 token；
* `pages/` 不写样式字面值，只用 `theme` 与 `widgets`；
* 业务逻辑一律留在 `core/` 与 `agents/`，视图层只调用与展示；
* 需要从 `core/gui/*` 取的常量，先抽成不依赖 tkinter 的模块（`ui/story_options.py`
  是范例：优先 import 旧模块，失败回落本地副本）。

## 3. 与旧界面并存

```
python main.py          旧版 tkinter（默认，功能完整）
python main.py --qt     新版 PySide6（迁移中）
```

两套界面读写同一份工程目录（`<output_dir>/system/parameters.txt` 格式完全一致），
可以随时切回旧版跑未迁移的阶段。`tests/test_ui_params_store.py` 锁住这个兼容性，
不装 Qt 也能跑。

## 4. 推进顺序

| 步骤 | 内容 | 状态 |
|---|---|---|
| 1 | 主题层 + 主窗壳 + 任务层 + 阶段 1 参数 | ✅ 已完成 |
| 2 | 阶段 2 世界设定：产物浏览器 + 分步按钮 | ✅ 已完成（分步仍整段驱动） |
| 3 | 阶段 3 故事结构：幕卡片 + 门禁面板 | ✅ 已完成 |
| 4 | 阶段 4 场景规划：阻塞空态 + 逐章浏览 | ✅ 已完成 |
| 5 | 阶段 5 章节撰写：正文区 + 生成中态 + 状态栏取消 | ✅ 已完成 |
| 6 | 工作流总览页：阶段卡片、断点、从断点继续 | ✅ 已完成 |
| 7 | **抽离生成逻辑**：把 tk 类里的生成函数搬到 `core/generation/`，删掉 `legacy_step.py`，分步按钮真正单独可跑 | ⬜ 待办 |
| 8 | 退场：删除 `core/gui/`，`--qt` 变默认，选项表迁到 `core/config/` | ⬜ 待办 |

每步的完成标准统一是三条：该阶段的所有按钮都走 `ui/task_runner.py`；
UI 线程不出现任何阻塞调用；旧版仍能读该阶段产生的产物。

## 4.1 第 7 步怎么做（下一步的具体路线）

对每个阶段，按同一套动作拆：

1. 在 `core/generation/` 下新建 `<stage>_pipeline.py`，把 tk 类里的
   `_generate_xxx` 方法原样搬过去，签名改成
   `def generate_xxx(params: dict, output_dir: str, model: str, report=None) -> dict`；
2. 原 tk 方法改成薄壳，只负责取参数、调新函数、更新 tk 控件；
3. `ui/services/step_runner.py` 增加一个直调分支，优先走新函数，
   取不到就回落 `legacy_step` 子进程；
4. 该阶段全部迁完后，删掉它在 `legacy_step.py` 里的分支。

这样每一步都可回退，旧界面始终可用。

## 5. 已知取舍

* `ui/story_options.py` 与 `core/gui/parameters.py` 暂时各有一份选项表。
  第 7 步统一，在那之前以旧模块为准（能 import 就覆盖本地副本）。
* 状态栏是单任务通道：同一时刻只允许一个前台任务，第二个 `run()` 被拒绝并回调
  `on_busy`。批量生成（撰写全部章节）算一个任务，内部再报子进度。
* 取消不再由调用方传 `cancel_button`，统一由状态栏接管。迁移某个阶段时，
  旧代码里传 `cancel_button` 的调用点要一并删掉。
* 分步按钮（势力 / 人物 / 世界观、人物弧光 / 势力弧光 / 地点融合）当前点任何一个
  都是整段跑该阶段——旧逻辑锁在 tk 类里，拆不开。按钮上的完成标记是**按磁盘产物**
  判定的，所以显示是准的，只是不能单独重跑。第 7 步解决。
* 「忽略门禁，强制规划」会产生未标记通过的场景产物，下一道门禁需要能识别这种
  半合法状态。目前的处理是：不改阶段状态，仍按 PARTIAL 计。
