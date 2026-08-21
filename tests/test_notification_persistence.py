"""An error that vanishes before it is read is worse than no error at all.

Errors ask the reader to do something, so they must wait for the reader.  This
was wrong in two places at once: the manager auto-closed everything, and the
module-level helpers — which is what every caller actually uses — passed an
explicit 5000ms that overrode the manager's own default.
"""

import inspect
import tkinter as tk

import pytest

from core.gui import notifications


@pytest.fixture(scope="module")
def root():
    """One Tk per module: repeatedly creating and destroying roots is flaky."""
    try:
        window = tk.Tk()
    except tk.TclError:  # pragma: no cover - headless CI
        pytest.skip("no display available")
    window.withdraw()
    yield window
    window.destroy()


@pytest.fixture
def manager(root):
    made = notifications.NotificationManager(root)
    yield made
    made.close_all()


LONG_MESSAGE = (
    "第 15 章尚未验收，不能继续生成第 16 章。\n"
    "原因：F-014-01 与账本冲突——账本为「用于转移神经毒剂核心母液的绝密温控舱」，"
    "第 15 章声明「装载活体样本与制剂的特制加固恒温舱」。\n"
    "请重新生成第 15 章：契约冲突会先交给模型判定，确属剧情反转时再由你裁定。"
)


def test_an_error_starts_no_auto_close_timer(manager, monkeypatch):
    started = []
    monkeypatch.setattr(
        notifications.threading,
        "Thread",
        lambda *args, **kwargs: started.append(kwargs) or _NoThread(),
    )
    manager.show_error("写作错误", LONG_MESSAGE)

    assert started == [], "an error must wait for the reader, not a timer"
    assert manager.notifications, "the window should still be open"


def test_a_success_still_dismisses_itself(manager, monkeypatch):
    started = []
    monkeypatch.setattr(
        notifications.threading,
        "Thread",
        lambda *args, **kwargs: started.append(kwargs) or _NoThread(),
    )
    manager.show_success("完成", "已写完第 3 章")

    assert len(started) == 1, "routine confirmations should not pile up on screen"


def test_the_window_grows_to_fit_a_long_message(root, manager):
    short = manager.show_success("完成", "好了", duration=0)
    tall = manager.show_error("写作错误", LONG_MESSAGE)
    root.update_idletasks()

    def height(window):
        return int(window.geometry().split("+")[0].split("x")[1])

    # The old fixed 350x100 box cut long messages off mid-sentence.
    assert height(tall) > height(short)
    assert height(tall) <= notifications.MAX_HEIGHT


def test_stacked_notifications_do_not_overlap(root, manager):
    first = manager.show_error("错误一", LONG_MESSAGE)
    second = manager.show_error("错误二", "短消息")
    root.update_idletasks()

    def top(window):
        return int(window.geometry().split("+")[2])

    def height(window):
        return int(window.geometry().split("+")[0].split("x")[1])

    assert top(second) >= top(first) + height(first)


def test_the_module_level_helpers_do_not_override_the_manager():
    """The regression: callers use these, and they were still passing 5000."""
    for name in ("show_error", "show_warning"):
        default = inspect.signature(getattr(notifications, name)).parameters["duration"].default
        assert default == 0, f"{name} would auto-dismiss"


class _NoThread:
    def start(self):
        pass
