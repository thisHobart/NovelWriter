"""Independent task admission and frozen prompt targets for the video workspace.

Text, image and video tasks each run on a runner of their own, so one of each can run at once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.gui.task_runner import TaskRunner
from core.gui.widgets.status_bar import TaskText
from core.short_video.v2.image_generation import ImageGenerationWork
from core.short_video.v2.media_service import MediaWork
from core.short_video.v2.models import require

if TYPE_CHECKING:
    from core.short_video.v2.contracts import JsonObject, VideoTask


def status_text(work: VideoTask) -> TaskText:
    context = work.task_context
    return TaskText(context.action, context.target, context.location(), work.token.cancelled)


class TaskChannels:
    def __init__(self) -> None:
        self.runners = {"text": TaskRunner(), "image": TaskRunner(), "video": TaskRunner()}
        self.works: dict[str, VideoTask] = {}
        self.prompt_target: tuple[str | None, str | None] | None = None

    @staticmethod
    def channel(work: VideoTask) -> str:
        if isinstance(work, ImageGenerationWork):
            return "image"
        if isinstance(work, MediaWork):
            return "video"
        return "text"

    def busy(self, channel: str = "text") -> bool:
        return self.runners[channel].busy

    @property
    def any_busy(self) -> bool:
        return any(runner.busy for runner in self.runners.values())

    def prompt_busy(self, target: JsonObject) -> bool:
        return bool(
            self.busy("text")
            and self.prompt_target is not None
            and self.prompt_target == (target.get("storyboard_version_id"), target.get("shot_id"))
        )

    def admit(self, work: VideoTask) -> str:
        channel = self.channel(work)
        require(not self.busy(channel), "PROJECT_BUSY", "该类任务已有任务在运行")
        if channel == "video" and work.command is not None:
            require(
                not self.prompt_busy(work.command.get("target", {})),
                "PROJECT_BUSY",
                "当前镜头的视频提示词正在编写，请完成后再生成视频",
            )
        if channel == "text":
            command = getattr(work, "command", None)
            if command is None and getattr(work, "task_id", None):
                task = work.repository.load()["tasks"][work.task_id]
                command = task.get("command")
                require(command is not None, "STALE_INPUT", "原任务输入已变化，请重新生成")
            self.prompt_target = None
            if command and command.get("stage") == "video_prompt":
                self.prompt_target = (
                    command["context"]["storyboard"]["id"],
                    command["shot_ids"][0],
                )
        self.works[channel] = work
        return channel

    def finish(self, channel: str, work: VideoTask) -> None:
        if self.works.get(channel) is work:
            self.works.pop(channel)
            if channel == "text":
                self.prompt_target = None

    def cancel_all(self) -> None:
        for runner in self.runners.values():
            runner.cancel_current()
