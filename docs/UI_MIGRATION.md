# PySide6 界面迁移说明

界面迁移已经完成。当前项目只有一套 PySide6 UI，行为规格见
`docs/UI_CONTRACT.md`。

## 当前结构

```text
core/gui/
  app.py                    应用入口
  main_window.py            主窗口、路由和任务状态分发
  theme.py / icons.py       主题与图标
  task_runner.py            Qt 后台任务执行器
  params_store.py           参数文件读写
  pages/                    工作流总览及五个阶段页面
  widgets/                  通用界面组件
  services/
    artifacts.py            只读产物访问
    step_runner.py          UI 与无界面生成管线之间的边界

core/generation/
  stage_context.py          阶段参数、模型、输出目录和进度回调
  stage_pipeline.py         四个阶段的统一无界面入口
  lore_pipeline.py          世界设定业务逻辑
  structure_pipeline.py     故事结构业务逻辑
  scene_pipeline.py         章节大纲与场景规划业务逻辑
  short_story_pipeline.py   短篇正文业务逻辑
```

## 运行方式

```bash
python main.py
```

启动入口和生成层都不会导入 Tk。阶段生成直接在 `QThreadPool` 工作线程中调用
`core.generation.stage_pipeline.run_stage()`；进度通过普通回调送回 Qt 主线程。

## 分层约束

- `widgets/` 只处理通用控件和主题。
- `pages/` 只负责展示、收集输入和发送任务请求。
- `core/gui/services/step_runner.py` 是 UI 调用生成层的唯一入口。
- `core/generation/` 不得导入 `core.gui`、PySide6 或其他 GUI 框架。
- 参数选项唯一来源是 `core/config/story_options.py`。
- 工作流状态以磁盘产物及契约校验结果为准。

## 验证

- `tests/test_ui_smoke.py`：窗口和页面构造。
- `tests/test_ui_integration.py`：控件、信号、路由、产物刷新、完成与取消状态。
- `tests/test_headless_stage_pipeline.py`：确认阶段生成入口不加载 Tk，并验证 UI 到
  无界面管线的调用链。
