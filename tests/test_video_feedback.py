"""Task feedback is specific, truthful, and fits the existing Qt surfaces."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx
import pytest

from core.short_video.v2.feedback import TaskContext, error_reason, review_message, task_message
from core.short_video.v2.models import WorkspaceError

if TYPE_CHECKING:
    from PySide6.QtWidgets import QApplication


@pytest.mark.parametrize(
    "code, phrase",
    [
        (401, "认证失败"),
        (403, "拒绝访问"),
        (404, "不存在"),
        (429, "限流"),
        (503, "服务异常"),
    ],
)
def test_http_failure_summary_preserves_evidence(code: int, phrase: str) -> None:
    response = httpx.Response(code, request=httpx.Request("POST", "https://example.test"))
    error = httpx.HTTPStatusError("raw upstream text", request=response.request, response=response)
    assert phrase in error_reason(error) and str(code) in error_reason(error)
    assert str(error) == "raw upstream text"


def test_connection_timeout_unknown_and_legacy_are_not_conflated() -> None:
    assert "请求超时" in error_reason(httpx.ReadTimeout("read failed"))
    assert "无法连接" in error_reason(httpx.ConnectError("connection refused"))
    assert error_reason(RuntimeError("specific failure")) == "RuntimeError：specific failure"
    unusual = RuntimeError("provider returned an unexpected error shape")
    unusual.body = {"code": ["provider-specific"]}
    assert "unexpected error shape" in error_reason(unusual)
    assert "未记录具体原因" in review_message("unavailable", None)
    assert "未记录具体问题" in review_message("issues", None)
    assert "网络" not in review_message("unavailable", None)
    context = TaskContext("分镜生成", phase="选用结果", saved=True)
    severity, title, detail = task_message(context, error=PermissionError("write denied"))
    assert severity == "error" and title == "分镜保存失败"
    assert "文件访问被拒绝" in detail


def test_conflict_and_media_unknown_never_suggest_recreating_remote_tasks() -> None:
    context = TaskContext("资产提示词生成", saved=True, phase="选用结果")
    error = WorkspaceError("TARGET_VERSION_CONFLICT", "提示词在生成期间已被修改")
    severity, title, detail = task_message(context, error=error)
    assert severity == "warning" and "版本冲突" in title
    assert "当前内容未被替换" in detail and "历史" not in detail
    context = TaskContext("视频生成", phase="提交远程任务", remote_status="unknown")
    severity, title, detail = task_message(context, error=TimeoutError())
    assert severity == "warning" and "待确认" in title
    assert "查询原任务" in detail and "避免重复提交" in detail
    context = TaskContext("视频生成", phase="保存视频", remote_status="collecting")
    _, title, detail = task_message(context, error=TimeoutError())
    assert title == "视频保存未完成" and "已生成" in detail
    context = TaskContext("视频生成", remote_status="cancelled")
    assert "尚未提交" in task_message(context, cancelled=True)[2]
    assert task_message(TaskContext("AI 回复"), result={"status": "succeeded"})[1] == "AI 已回复"


@pytest.fixture
def app() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_window_uses_worker_context_and_refresh_keeps_final_notice(
    app: QApplication, tmp_path: Path
) -> None:
    from core.gui.main_window import MainWindow
    from core.gui.widgets.status_bar import AppStatusBar

    status = AppStatusBar()
    callbacks = {}
    runner = SimpleNamespace(busy=False, run=lambda *args, **kwargs: callbacks.update(kwargs))
    refreshes = []
    window = SimpleNamespace(
        _mode="video",
        runner=runner,
        status=status,
        refresh_all=lambda: refreshes.append(True),
        rail=SimpleNamespace(backend_combo=None, model_combo=None, config_button=None),
        video_workspace=SimpleNamespace(task_controls=lambda: []),
    )
    work = SimpleNamespace(
        output_dir=str(tmp_path),
        token=object(),
        task_context=TaskContext(
            "资产提示词生成", target="场景「木屋」", phase="生成", completed=2, total=5
        ),
    )
    MainWindow._on_video_task_requested(window, work, None, "正在生成…")
    callbacks["on_error"](TimeoutError("raw error"))
    callbacks["on_done"]()
    assert status._state._text.text() == "资产提示词生成中断"
    assert "木屋" in status._context.text() and "请求超时" in status._context.text()
    assert "2/5" in status._context.text() and "短视频任务" not in status._state._text.text()
    assert len(refreshes) == 2
    status.close()


def test_status_bar_long_text_does_not_expand_window_or_leave_stale_right_label(
    app: QApplication,
) -> None:
    from core.gui.widgets.status_bar import AppStatusBar

    status = AppStatusBar()
    status.resize(800, 44)
    status.show()
    status.set_idle(right="上次保存 12:00")
    value = "自动审阅请求超时，候选已保存。" * 200
    status.set_error("资产提示词生成失败", value)
    app.processEvents()
    assert status.width() == 800
    assert status._context.text() == value and status._context.minimumWidth() == 0
    assert status._context.width() < 800 and not status._right.text()
    assert status._stop.isHidden()
    status.close()


def test_local_save_notice_names_operation_and_keeps_validation_location(app: QApplication) -> None:
    from core.gui.pages.short_video.workbench import Workbench
    from core.gui.widgets.status_bar import AppStatusBar

    class Editor:
        stage = "storyboard"
        invalid = True

        def save(self) -> bool:
            if self.invalid:
                raise WorkspaceError("INVALID_OUTPUT", "镜头1的景别不能为空")
            return True

    status = AppStatusBar()
    workbench = SimpleNamespace(host=SimpleNamespace(status=status), source=object())
    editor = Editor()
    assert Workbench.perform(workbench, editor.save) is False
    assert status._state._text.text() == "分镜保存失败"
    assert status._context.text() == "镜头1的景别不能为空"
    editor.invalid = False
    assert Workbench.perform(workbench, editor.save) is True
    assert status._state._text.text() == "分镜已保存"
    assert "尚未自动检查" in status._context.text()
    status.close()


def test_preflight_failure_names_the_requested_next_stage(app: QApplication) -> None:
    from core.gui.pages.short_video.workbench import Workbench
    from core.gui.widgets.status_bar import AppStatusBar

    class Source:
        def generate(self) -> None:
            raise WorkspaceError("INPUT_TOO_LARGE", "原文超出本次输入容量")

    class Storyboard:
        stage = "storyboard"

        def continue_creation(self) -> None:
            raise WorkspaceError("MISSING_UPSTREAM", "请先生成分镜")

    status = AppStatusBar()
    source = Source()
    workbench = SimpleNamespace(host=SimpleNamespace(status=status), source=source)
    Workbench.perform(workbench, source.generate)
    assert status._state._text.text() == "小说推文生成失败"
    Workbench.perform(workbench, Storyboard().continue_creation)
    assert status._state._text.text() == "资产提示词生成失败"
    assert status._context.text() == "请先生成分镜"
    status.close()
