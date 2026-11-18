"""Concise task feedback shared by workers and the UI; never replaces exceptions."""

from __future__ import annotations

import errno
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import httpx
from openai import APIConnectionError, APITimeoutError

from .contracts import ProgressReporter
from .llm import RecordingError
from .models import WorkspaceError

if TYPE_CHECKING:
    from .repository import ProjectRepository


ACTION_NAMES = {
    "novel_copy": "小说推文生成",
    "video_copy": "视频文案生成",
    "asset_catalog": "资产目录整理",
    "storyboard": "分镜生成",
    "storyboard_task": "分镜生成",
    "prompt_task": "资产提示词生成",
    "asset_spec": "资产提示词生成",
    "shot_prompts": "镜头提示词优化",
    "video_prompt": "视频提示词生成",
    "chat": "AI 回复",
}


def concise(value: object, limit: int = 180) -> str:
    text = " ".join(str(value).split())
    # Technical exception text can contain credentials or request URLs.
    text = re.sub(r"https?://\S+", "[接口地址]", text)
    text = re.sub(r"(?i)(bearer\s+|sk-)[\w.\-]+", "[已隐藏]", text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def error_reason(error: BaseException) -> str:
    """Map evidence from the original exception, without guessing its root cause."""
    code = getattr(error, "status_code", None)
    if isinstance(error, httpx.HTTPStatusError):
        code = error.response.status_code
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        body = body.get("error", body)
        if isinstance(body, dict) and body.get("code") in ("insufficient_quota", "quota_exceeded"):
            return f"接口配额不足（{code}），请检查服务配额"
    messages = {
        400: "接口拒绝请求（400），请核对请求参数",
        401: "接口认证失败（401），请检查密钥",
        403: "接口拒绝访问（403），请检查使用权限",
        404: "请求地址或资源不存在（404），请核对配置",
        422: "接口参数校验失败（422），请核对请求参数",
        429: "接口限流（429），请稍后重试",
    }
    if code in messages:
        return messages[code]
    if isinstance(code, int) and code >= 500:
        return f"接口服务异常（{code}），请稍后重试"
    if isinstance(error, (APITimeoutError, httpx.TimeoutException, TimeoutError)):
        return "请求超时，未取得本次请求的结果"
    if isinstance(error, (APIConnectionError, httpx.ConnectError, ConnectionError)):
        return "无法连接服务，请检查接口地址和服务连接"
    if isinstance(error, PermissionError):
        return "文件访问被拒绝，请检查权限或文件占用"
    if isinstance(error, FileNotFoundError):
        return "文件或目录不存在，请检查所选路径"
    if isinstance(error, OSError) and error.errno == errno.ENOSPC:
        return "磁盘空间不足，未完成写入"
    if isinstance(error, WorkspaceError):
        return readable_fields(concise(error))
    if isinstance(error, RecordingError):
        return concise(error)
    return f"{type(error).__name__}：{concise(error) or '未提供具体原因'}"


def readable_fields(text: str) -> str:
    for field, label in {
        "visual_prompt_draft": "画面提示词",
        "camera_movement": "镜头运动",
        "shot_size": "景别",
        "duration_ms": "时长",
        "action": "动作",
        "narration": "旁白",
        "scene_ref": "场景引用",
    }.items():
        text = text.replace(field, label)
    return re.sub(r"shots\.(\d+)", lambda m: f"镜头{int(m[1]) + 1}", text)


@dataclass(frozen=True)
class TaskContext:
    action: str
    target: str = ""
    phase: str = "准备"
    completed: int = 0
    total: int = 0
    saved: bool = False
    remote_status: str = ""
    resumable: bool = False
    network_retries: int = 0

    def location(self) -> str:
        return " · ".join(part for part in (self.target, self.phase) if part)

    def retained(self) -> str:
        if self.action == "资产图片生成":
            return f"已保存{self.completed}/{self.total}张图片"
        if self.total:
            suffix = "；本项候选已保存" if self.saved else ""
            return f"本次已完成{self.completed}/{self.total}项{suffix}"
        if self.action == "AI 回复":
            return "本轮回复已保存" if self.saved else "当前会话已有消息保留"
        return "本次候选已保存" if self.saved else "当前已有内容保留"


class TaskTracker:
    """Replace immutable snapshots so GUI callbacks never read a mutating dict."""

    def __init__(self, action: str, *, target: str = "") -> None:
        self.task_context = TaskContext(action, target=target)

    def update_context(self, **changes: Any) -> None:
        self.task_context = replace(self.task_context, **changes)

    def accept_context(self, context: TaskContext) -> None:
        self.update_context(
            target=context.target,
            phase=context.phase,
            saved=context.saved,
            resumable=context.resumable,
            network_retries=context.network_retries,
        )

    def report_network_retry(
        self,
        error: Exception,
        attempt: int,
        maximum: int,
        delay: float,
        report: ProgressReporter,
        on_context: Callable[[TaskContext], None] | None = None,
    ) -> None:
        self.update_context(network_retries=attempt)
        if on_context:
            on_context(self.task_context)
        report(
            f"{self.task_context.location()} · 网络重试{attempt}/{maximum}："
            f"{error_reason(error)}；{delay:.1f}秒后重试",
            -1,
        )


def command_target(command: dict) -> str:
    context = command.get("context", {})
    entity = context.get("entity", {})
    variant = next(
        (v for v in entity.get("variants", []) if v["variant_id"] == context.get("variant_id")), {}
    )
    if entity:
        label = {"character": "人物", "scene": "场景", "prop": "道具"}.get(
            entity.get("kind"), "资产"
        )
        return " · ".join(
            str(v) for v in (f"{label}「{entity.get('name', '')}」", variant.get("label")) if v
        )
    if command.get("shot_ids"):
        board = context.get("storyboard", {}).get("content", {})
        names = [
            f"镜头{s['order']}"
            for s in board.get("shots", [])
            if s["shot_id"] in command["shot_ids"]
        ]
        return "、".join(names) or "所选镜头"
    return ACTION_NAMES.get(command.get("stage"), "").removesuffix("生成").removesuffix("整理")


def failure_feedback(context: TaskContext, error: BaseException) -> dict:
    cause = error_reason(error)
    return {
        "phase": context.phase,
        "target": context.target,
        "cause": cause,
        "summary": f"{context.phase}失败：{cause}"
        + (f"；网络已重试{context.network_retries}次" if context.network_retries else ""),
        **({"network_retries": context.network_retries} if context.network_retries else {}),
        "issue_count": 0,
    }


def review_message(status: str | None, feedback: dict | None) -> str:
    feedback = feedback or {}
    if status in {"unavailable", "incomplete"}:
        return feedback.get("summary") or "此前自动检查未完成，未记录具体原因"
    if status == "issues":
        summary = feedback.get("summary") or "此前检查发现问题，未记录具体问题"
        count = feedback.get("issue_count", 0)
        return summary + (f"（共{count}项问题）" if count > 1 else "")
    if status == "passed":
        return "自动检查通过"
    return "尚未自动检查"


def stale_sources(repository: ProjectRepository, record: dict, state: dict) -> list[str]:
    """Inputs that changed after the record was made, as names people know."""
    changed: list[str] = []
    seen = set()

    def visit(item: dict) -> None:
        for key, ref in item.get("dependency_refs", {}).items():
            if ref in seen:
                continue
            seen.add(ref)
            if state["current"].get(key) != ref:
                label = {
                    "source": "原文",
                    "selection": "原文选区",
                    "config": "创作配置",
                    "novel_copy": "小说推文",
                    "video_copy": "视频文案",
                    "asset_catalog": "资产目录",
                    "storyboard": "分镜",
                }.get(key)
                label = label or ("资产设定" if "_spec:" in key else "来源内容")
                if label not in changed:
                    changed.append(label)
            else:
                visit(repository.record(ref, state=state))

    visit(record)
    return changed


def record_message(repository: ProjectRepository, record: dict, state: dict) -> str:
    run = next((r for r in state["runs"] if r["id"] == record["metadata"].get("run_id")), None)
    parts = ["已保存"]
    if record.get("origin") == "model" and run:
        parts.append(review_message(run.get("review_status"), run.get("feedback")))
    else:
        parts.append("当前内容尚未自动检查")
    changed = stale_sources(repository, record, state)
    if changed:
        parts.append("、".join(changed) + "已更新，当前内容仍使用旧版来源")
    return "；".join(parts)


_SHOT_PATH = re.compile(r"^shots(?:\[(\d+)\]|\.(\d+))")


def record_check(
    repository: ProjectRepository, record: dict, state: dict, *, shot_index: int | None = None
) -> dict:
    """The check result as one short line plus the issues behind it, for one shot when given.

    status is passed / issues / other_shots / incomplete / unchecked; issues are readable
    sentences; stale names inputs that changed since the record was made.
    """
    run = next((r for r in state["runs"] if r["id"] == record["metadata"].get("run_id")), None)
    stale = stale_sources(repository, record, state)
    result = {"status": "unchecked", "summary": "尚未自动检查", "issues": [], "stale": stale}
    if record.get("origin") != "model" or not run:
        return result
    status = run.get("review_status")
    if status == "passed":
        return {**result, "status": "passed", "summary": "检查通过"}
    if status in {"unavailable", "incomplete"}:
        return {**result, "status": "incomplete", "summary": review_message(status, run.get("feedback"))}
    if status != "issues":
        return result
    found = (run.get("review_result") or {}).get("issues") or []
    if not found:
        # Runs saved before issues were stored keep only the summary.
        summary = (run.get("feedback") or {}).get("summary") or "检查发现问题，未记录具体问题"
        return {**result, "status": "issues", "summary": "检查发现问题", "issues": [summary]}

    def shot_of(issue: dict) -> int | None:
        match = _SHOT_PATH.match(issue.get("target_path", ""))
        return int(match[1] or match[2]) if match else None

    mine = [
        issue
        for issue in found
        if shot_index is None or shot_of(issue) in (None, shot_index)
    ]
    sentences = []
    for issue in mine:
        sentence = readable_fields(concise(issue.get("explanation", ""))) or "未说明原因"
        suggestion = readable_fields(concise(issue.get("suggestion", "")))
        sentences.append(sentence + (f"建议：{suggestion}" if suggestion else ""))
    if not mine:
        return {
            **result,
            "status": "other_shots",
            "summary": f"其他镜头有 {len(found)} 项问题",
        }
    return {
        **result,
        "status": "issues",
        "summary": f"检查发现 {len(mine)} 项问题",
        "issues": sentences,
    }


def task_message(
    context: TaskContext,
    *,
    result: dict | None = None,
    error: BaseException | None = None,
    cancelled: bool = False,
) -> tuple[str, str, str]:
    """Return severity, title, detail for the existing single status bar."""
    if cancelled:
        detail = (
            "已停止本地等待，ComfyUI 可能仍在运行；请查询原任务"
            if context.action == "视频生成"
            else context.retained()
        )
        if context.action == "视频生成" and context.remote_status == "cancelled":
            detail = "已停止，尚未提交远程任务"
        return (
            "warning",
            context.action + "已停止",
            detail + ("；可点击继续生成" if context.resumable else ""),
        )
    if error is not None:
        title = context.action + ("中断" if context.saved or context.completed else "失败")
        detail = f"{context.location()}：{error_reason(error)}；{context.retained()}"
        if context.network_retries:
            detail += f"；已自动重试{context.network_retries}次"
        if context.resumable:
            detail += "；可点击继续生成"
        if isinstance(error, RecordingError) or (
            isinstance(error, OSError)
            and not isinstance(error, (TimeoutError, ConnectionError))
            and ("保存" in context.phase or context.phase == "选用结果")
        ):
            subject = context.action.removesuffix("生成").removesuffix("优化")
            return "error", subject + "保存失败", detail
        if context.action == "视频生成":
            if context.remote_status == "collecting":
                return (
                    "warning",
                    "视频保存未完成",
                    detail + "；远程视频已生成，请查询原任务继续获取",
                )
            if context.remote_status in {"unknown", "queued", "running"}:
                return "warning", "视频任务状态待确认", detail + "；请查询原任务，避免重复提交"
        if isinstance(error, WorkspaceError) and error.code == "TARGET_VERSION_CONFLICT":
            detail = f"{context.location()}：{error_reason(error)}；当前内容未被替换"
            return "warning", context.action + "发生内容冲突", detail
        return ("warning" if context.saved or context.completed else "error"), title, detail
    result = result or {}
    if context.action == "资产图片生成":
        return (
            "success" if result.get("status") == "succeeded" else "warning",
            "资产图片已保存" if result.get("status") == "succeeded" else "资产图片部分完成",
            result.get("image_summary", context.retained()),
        )
    if context.action == "视频生成":
        if result.get("status") == "succeeded":
            return "success", "视频已保存", "可在镜头页播放或打开视频位置"
        return "warning", "暂时无法确认远程任务状态", "请查询原任务，避免重复提交"
    if result.get("selected") is False:
        detail = "输入或当前稿已变化；当前内容保留"
        return "warning", context.action + "未替换当前内容", detail
    if result.get("status") != "succeeded":
        return "warning", context.action + "未完成", context.retained()
    if context.action == "AI 回复":
        return "success", "AI 已回复", "回复已保存到当前会话"
    status = result.get("review_status")
    message = review_message(status, result.get("feedback"))
    if result.get("review_summary"):
        message = result["review_summary"]
    subject = context.action.removesuffix("生成").removesuffix("优化")
    return (
        "warning" if status in {"issues", "unavailable", "incomplete"} else "success",
        subject + "已保存",
        message,
    )
