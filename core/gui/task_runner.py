"""把耗时生成任务挪出 Tk 主线程。

问题：章节生成的按钮回调此前直接同步调用大模型。写一整章要经过 N 个场景 ×
最多 3 轮评审重试，「撰写全部章节」还要乘以章数——主线程被占住几十分钟，窗口
彻底失去响应，Windows 还会弹「程序无响应」。

约定：`work` 在后台线程执行，**不得触碰任何 Tk 部件**；`on_success` /
`on_error` / `on_done` 在主线程执行，可以自由操作界面。按钮的禁用与恢复由本
模块统一接管，调用方不要自己 config。
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from core.generation.cancellation import CancelToken, GenerationCancelled


DEFAULT_POLL_MS = 120


@dataclass
class BackgroundTask:
    """Handle for work started by `run_in_background`."""

    thread: threading.Thread
    cancel_token: CancelToken
    done: threading.Event = field(default_factory=threading.Event)

    @property
    def running(self) -> bool:
        return not self.done.is_set()

    def cancel(self) -> None:
        self.cancel_token.cancel()


def run_in_background(
    root: tk.Misc,
    work: Callable[[], Any],
    *,
    on_success: Optional[Callable[[Any], None]] = None,
    on_error: Optional[Callable[[BaseException], None]] = None,
    on_cancelled: Optional[Callable[[GenerationCancelled], None]] = None,
    on_done: Optional[Callable[[], None]] = None,
    busy_widgets: Iterable[tk.Widget] = (),
    busy_button: Optional[tk.Widget] = None,
    busy_text: Optional[str] = None,
    cancel_button: Optional[tk.Widget] = None,
    cancel_token: Optional[CancelToken] = None,
    logger: Optional[logging.Logger] = None,
    poll_ms: int = DEFAULT_POLL_MS,
) -> BackgroundTask:
    """Run `work` on a daemon thread and report back on the Tk main thread.

    `busy_widgets` are disabled for the duration and restored afterwards, even
    if `work` raised. `busy_button` additionally shows `busy_text` while busy.
    `cancel_button` is enabled only while the task runs and is wired to the
    cancel token.
    """
    log = logger or logging.getLogger("task-runner")
    token = cancel_token or CancelToken()
    token.reset()

    widgets = [widget for widget in busy_widgets if widget is not None]
    if busy_button is not None and busy_button not in widgets:
        widgets.append(busy_button)

    previous_states = []
    for widget in widgets:
        try:
            previous_states.append((widget, str(widget.cget("state"))))
            widget.config(state="disabled")
        except tk.TclError:
            pass

    previous_text = None
    if busy_button is not None and busy_text:
        try:
            previous_text = busy_button.cget("text")
            busy_button.config(text=busy_text)
        except tk.TclError:
            previous_text = None

    if cancel_button is not None:
        try:
            cancel_button.config(state="normal", command=token.cancel)
        except tk.TclError:
            pass

    results: "queue.Queue[tuple]" = queue.Queue(maxsize=1)
    task = BackgroundTask(thread=None, cancel_token=token)  # type: ignore[arg-type]

    def runner():
        try:
            results.put(("ok", work()))
        except GenerationCancelled as cancelled:
            results.put(("cancelled", cancelled))
        except BaseException as exc:  # noqa: BLE001  reported to the UI below
            results.put(("error", exc))

    def restore():
        for widget, state in previous_states:
            try:
                widget.config(state=state)
            except tk.TclError:
                pass
        if busy_button is not None and previous_text is not None:
            try:
                busy_button.config(text=previous_text)
            except tk.TclError:
                pass
        if cancel_button is not None:
            try:
                cancel_button.config(state="disabled")
            except tk.TclError:
                pass

    def poll():
        try:
            kind, payload = results.get_nowait()
        except queue.Empty:
            try:
                root.after(poll_ms, poll)
            except tk.TclError:
                pass  # window closed while work was running
            return

        task.done.set()
        try:
            restore()
            if kind == "ok":
                if on_success:
                    on_success(payload)
            elif kind == "cancelled":
                log.info("Background task cancelled by user: %s", payload)
                if on_cancelled:
                    on_cancelled(payload)
                elif on_error:
                    on_error(payload)
            else:
                log.error("Background task failed: %s", payload, exc_info=payload)
                if on_error:
                    on_error(payload)
        finally:
            if on_done:
                try:
                    on_done()
                except Exception:  # noqa: BLE001  never leave the UI wedged
                    log.exception("Background task on_done callback failed")

    thread = threading.Thread(target=runner, daemon=True)
    task.thread = thread
    thread.start()
    root.after(poll_ms, poll)
    return task


@dataclass
class UiSnapshot:
    """主线程读到的界面取值，供后台线程使用。

    Tk 变量只能在主线程读取，所以每个后台任务开始前先在主线程把需要的值一次读齐，
    工作函数只从这个快照取值。`extras` 放各标签页自己的输入（数量、章节编号等）。
    """

    output_dir: str
    model: str
    parameters: dict = field(default_factory=dict)
    extras: dict = field(default_factory=dict)

    def get(self, name: str, default: Any = None) -> Any:
        return self.extras.get(name, default)


def snapshot_ui(app, **extras) -> UiSnapshot:
    """Read every UI-derived value a worker will need. Main thread only."""
    parameters = {}
    if app is not None and hasattr(app, "param_ui"):
        try:
            parameters = app.param_ui.get_current_parameters()
        except Exception:  # noqa: BLE001  参数页尚未就绪时按空参数处理
            parameters = {}
    return UiSnapshot(
        output_dir=app.get_output_dir() if app is not None else "current_work",
        model=app.get_selected_model() if app is not None else "",
        parameters=parameters,
        extras=extras,
    )
