# -*- coding: utf-8 -*-
"""把「执行一个生成阶段」包装成 TaskRunner 能跑的 work 函数。

work 在后台线程里起子进程（ui/services/legacy_step.py），逐行读它的 JSON 事件，
通过 report() 把进度送回主线程。取消令牌一旦触发就终止子进程。

页面只需要：
    task = runner.run(step_work("lore", output_dir, model),
                      pass_progress=True, busy_button=btn, busy_text="正在生成…")
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Callable, Dict, Optional

try:
    from core.generation.cancellation import GenerationCancelled
except Exception:  # noqa: BLE001 - pragma: no cover
    class GenerationCancelled(Exception):
        pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STEP_LABELS = {
    "lore": "世界设定",
    "structure": "故事结构",
    "scenes": "场景规划",
    "chapters": "章节撰写",
}


class StepFailed(RuntimeError):
    """子进程报告的业务失败（区别于进程本身崩掉）。"""


def _popen(step: str, output_dir: str, model: str) -> subprocess.Popen:
    command = [
        sys.executable, "-u", "-m", "ui.services.legacy_step",
        "--step", step, "--output-dir", output_dir,
    ]
    if model:
        command += ["--model", model]

    creationflags = 0
    if os.name == "nt":  # Windows 下不弹控制台窗口
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    return subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
    )


def step_work(step: str, output_dir: str, model: str = "") -> Callable[[Callable], Dict[str, Any]]:
    """返回一个 `work(report)`，交给 TaskRunner.run(..., pass_progress=True)。"""

    def work(report: Callable[[str, float], None]) -> Dict[str, Any]:
        label = STEP_LABELS.get(step, step)
        report(f"正在启动{label}…", 0.01)

        process = _popen(step, output_dir, model)
        result: Dict[str, Any] = {"step": step}
        tail: list[str] = []

        try:
            assert process.stdout is not None
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                tail.append(line)
                del tail[:-40]  # 只留最后 40 行用于报错

                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 旧代码的普通日志，忽略

                kind = payload.get("event")
                if kind == "progress":
                    # report() 内部会检查取消令牌并抛 GenerationCancelled
                    report(payload.get("text", label),
                           float(payload.get("fraction", -1.0)))
                elif kind == "done":
                    result.update(payload)
                elif kind == "error":
                    raise StepFailed(payload.get("message") or f"{label}执行失败")
        except GenerationCancelled:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            raise
        finally:
            if process.poll() is None:
                process.terminate()

        code = process.wait()
        if code != 0 and "quality" not in result:
            detail = "\n".join(tail[-6:])
            raise StepFailed(f"{label}执行失败（退出码 {code}）\n{detail}".strip())

        report(f"{label}已完成", 1.0)
        return result

    return work


def python_available() -> bool:
    """子进程用的解释器是否存在（打包后可能没有 sys.executable）。"""
    return bool(sys.executable) and os.path.exists(sys.executable)


ORDERED_STEPS = ["lore", "structure", "scenes", "chapters"]


def full_workflow_work(output_dir: str, model: str = "",
                       steps: Optional[list] = None) -> Callable[[Callable], Dict[str, Any]]:
    """依次跑完四个阶段，进度按阶段等分。

    刻意不调用编排器的 execute_complete_workflow：逐阶段跑可以在任一阶段失败时
    保留前面的产物，也让状态栏的进度落在真实的阶段边界上。
    """
    plan = steps or ORDERED_STEPS

    def work(report: Callable[[str, float], None]) -> Dict[str, Any]:
        results: Dict[str, Any] = {}
        span = 1.0 / len(plan)
        for index, step in enumerate(plan):
            base = index * span
            label = STEP_LABELS.get(step, step)

            def scoped(text: str, fraction: float, _base=base, _label=label) -> None:
                if fraction < 0:
                    report(f"{_label} · {text}", -1.0)
                else:
                    report(f"{_label} · {text}", _base + fraction * span)

            results[step] = step_work(step, output_dir, model)(scoped)
        report("完整流程已完成", 1.0)
        return results

    return work
