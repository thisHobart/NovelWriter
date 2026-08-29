# -*- coding: utf-8 -*-
"""在独立进程里跑一个生成阶段（过渡方案）。

背景：真正的生成逻辑目前长在 core/gui/lore.py、story_structure.py、
scene_plan.py、chapter_writing.py 这四个 **tkinter 类** 里，编排器
（agents/orchestration/story_generation_orchestrator.py）是靠
`app_instance.lore_ui.generate_factions()` 这样"点按钮"来驱动的，
中间还会 `root.update()` 泵 Tk 事件循环。

所以在 Qt 界面里没法直接复用：两个 GUI 框架的事件循环不能塞进同一个线程，
而 Tk 又不允许跨线程调用。折中办法是把整段旧逻辑放进**子进程**执行：
  * 子进程里建一个隐藏（withdraw）的 Tk 根窗口，旧代码原样运行；
  * 进度以 JSON 行写到 stdout，父进程转成状态栏进度；
  * 取消 = 终止子进程，不会污染 Qt 主进程。

等生成逻辑从 tk 类里抽出来（见 docs/UI_MIGRATION.md 第 7 步），
这个模块整体删除，ui/services/step_runner.py 改为直接调用即可。

用法（一般由 step_runner.py 调起，不手工执行）：
    python -m ui.services.legacy_step --step lore --output-dir current_work
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback


def emit(event: str, **payload) -> None:
    """一行一个 JSON 事件，父进程按行解析。"""
    sys.stdout.write(json.dumps({"event": event, **payload}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def run(step: str, output_dir: str, model: str = "") -> int:
    import tkinter as tk

    from core.gui.app import NovelWriterApp

    emit("progress", text=f"正在初始化 {step} 阶段…", fraction=0.02)

    root = tk.Tk()
    root.withdraw()  # 隐藏窗口：只借它的事件循环，不给用户看

    try:
        app = NovelWriterApp(root)

        # 对齐输出目录与模型
        app.param_ui.output_dir_var.set(output_dir)
        app.param_ui.load_parameters()
        if model:
            try:
                app.selected_model_var.set(model)
            except Exception:  # noqa: BLE001 - 模型不在列表里就沿用默认
                pass

        if getattr(app, "agentic_enabled", None) is None:
            emit("error", message="智能编排组件不可用（agents 未就绪）")
            return 2
        app.agentic_enabled.set(True)
        app.init_agentic_orchestrators()

        orchestrator = app.story_orchestrator
        if orchestrator is None:
            emit("error", message="编排器初始化失败")
            return 2

        def on_progress(step_name, status):
            emit("progress",
                 text=f"{step_name}：{getattr(status, 'value', status)}",
                 fraction=-1.0)

        orchestrator.progress_callback = on_progress

        params = app.param_ui.get_current_parameters()
        orchestrator.load_or_create_workflow_state(params)

        emit("progress", text=f"正在执行 {step} 阶段…", fraction=-1.0)
        result = orchestrator.execute_single_step(step, params)

        success = bool(getattr(result, "success", False))
        if success:
            emit("done", step=step,
                 quality=getattr(result, "quality_scores", {}) or {})
            return 0

        emit("error", message=str(getattr(result, "error_message", "") or "阶段执行失败"))
        return 1
    except Exception as exc:  # noqa: BLE001
        emit("error", message=f"{exc}", traceback=traceback.format_exc())
        return 3
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="在子进程里执行一个生成阶段")
    parser.add_argument("--step", required=True,
                        choices=["lore", "structure", "scenes", "chapters"])
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    return run(args.step, args.output_dir, args.model)


if __name__ == "__main__":
    raise SystemExit(main())
