"""Tests for running generation work off the Tk main thread."""

import threading
import time

import pytest

tk = pytest.importorskip("tkinter")

from core.generation.cancellation import CancelToken, GenerationCancelled
from core.gui import notifications
from core.gui.task_runner import UiSnapshot, run_in_background, snapshot_ui


@pytest.fixture
def root():
    try:
        window = tk.Tk()
    except tk.TclError:  # pragma: no cover - headless environment
        pytest.skip("no display available for Tk")
    window.withdraw()
    yield window
    try:
        window.destroy()
    except tk.TclError:
        pass


def _pump_until(root, predicate, timeout=5.0):
    """Run the Tk event loop until predicate holds or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_work_runs_off_main_thread_and_reports_on_it(root):
    button = tk.Button(root, text="生成", state="normal")
    seen = {}

    def work():
        seen["worker_thread"] = threading.current_thread()
        return "结果"

    def on_success(value):
        seen["result"] = value
        seen["callback_thread"] = threading.current_thread()

    run_in_background(
        root,
        work,
        on_success=on_success,
        busy_button=button,
        busy_text="正在生成…",
    )

    assert str(button.cget("state")) == "disabled"
    assert button.cget("text") == "正在生成…"

    assert _pump_until(root, lambda: "result" in seen)
    assert seen["result"] == "结果"
    assert seen["worker_thread"] is not threading.main_thread()
    assert seen["callback_thread"] is threading.main_thread()
    # 按钮状态与原文案都必须恢复，否则界面会永久卡在忙碌态。
    assert str(button.cget("state")) == "normal"
    assert button.cget("text") == "生成"


def test_failure_restores_the_ui_and_reports_the_error(root):
    button = tk.Button(root, text="生成", state="normal")
    other = tk.Button(root, text="别的", state="normal")
    errors = []

    def work():
        raise RuntimeError("后端不可用")

    run_in_background(
        root,
        work,
        on_error=errors.append,
        busy_widgets=[other],
        busy_button=button,
        busy_text="正在生成…",
    )
    assert str(other.cget("state")) == "disabled"

    assert _pump_until(root, lambda: errors)
    assert str(errors[0]) == "后端不可用"
    assert str(button.cget("state")) == "normal"
    assert str(other.cget("state")) == "normal"
    assert button.cget("text") == "生成"


def test_cancel_button_is_wired_and_reports_cancellation(root):
    button = tk.Button(root, text="生成", state="normal")
    cancel_button = tk.Button(root, text="停止", state="disabled")
    token = CancelToken()
    started = threading.Event()
    cancelled = []

    def work():
        started.set()
        while not token.cancelled:
            time.sleep(0.01)
        token.raise_if_cancelled()

    run_in_background(
        root,
        work,
        on_cancelled=cancelled.append,
        on_error=lambda exc: pytest.fail(f"cancellation must not be an error: {exc}"),
        busy_button=button,
        cancel_button=cancel_button,
        cancel_token=token,
    )

    assert str(cancel_button.cget("state")) == "normal"
    assert started.wait(timeout=5)
    cancel_button.invoke()

    assert _pump_until(root, lambda: cancelled)
    assert isinstance(cancelled[0], GenerationCancelled)
    assert str(cancel_button.cget("state")) == "disabled"
    assert str(button.cget("state")) == "normal"


def test_on_done_runs_even_when_work_failed(root):
    done = []
    run_in_background(root, lambda: 1 / 0, on_error=lambda exc: None, on_done=lambda: done.append(True))
    assert _pump_until(root, lambda: done)


def test_snapshot_reads_every_ui_value_up_front():
    class FakeParams:
        @staticmethod
        def get_current_parameters():
            return {"genre": "Horror", "story_length": "Novel (Standard)"}

    class FakeApp:
        param_ui = FakeParams()

        @staticmethod
        def get_output_dir():
            return "work"

        @staticmethod
        def get_selected_model():
            return "hosted-llm"

    ui = snapshot_ui(FakeApp(), num_factions=5)

    assert isinstance(ui, UiSnapshot)
    assert ui.output_dir == "work"
    assert ui.model == "hosted-llm"
    assert ui.parameters["genre"] == "Horror"
    assert ui.get("num_factions") == 5
    assert ui.get("missing", "默认") == "默认"


def test_snapshot_survives_a_broken_parameters_tab():
    class BrokenParams:
        @staticmethod
        def get_current_parameters():
            raise RuntimeError("参数页尚未初始化")

    class FakeApp:
        param_ui = BrokenParams()

        @staticmethod
        def get_output_dir():
            return "work"

        @staticmethod
        def get_selected_model():
            return "hosted-llm"

    assert snapshot_ui(FakeApp()).parameters == {}


def test_notifications_from_worker_threads_are_queued_not_shown(root):
    notifications.init_notifications(root)
    shown = []

    class RecordingManager:
        parent = root

        def show_error(self, title, message, duration):
            shown.append((title, message, threading.current_thread()))

    notifications._notification_manager = RecordingManager()
    try:
        worker = threading.Thread(
            target=lambda: notifications.show_error("错误", "来自后台线程")
        )
        worker.start()
        worker.join(timeout=5)

        # 后台线程只入队，不能直接碰 Tk。
        assert shown == []

        notifications.drain_pending()
        assert len(shown) == 1
        assert shown[0][1] == "来自后台线程"
        assert shown[0][2] is threading.main_thread()
    finally:
        notifications._notification_manager = None
