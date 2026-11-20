"""协作式取消。

长时间的生成任务现在跑在后台线程上，界面需要一个能中途叫停的办法。线程无法被
安全地强杀，所以采用协作式取消：生成循环在每个安全点（场景边界、章节边界）检查
令牌，被取消时抛出 `GenerationCancelled`，让调用方按普通失败处理。

安全点的选择原则：只在「已完成的产物不会被破坏」的位置检查，而且不丢掉已经完成的
成果。具体是两处：

* **下一场之前**——本章尚未写完，丢弃的是半成品，不会落盘也不会提交账本；
* **下一章之前**——上一章已经落盘并提交，停在这里不损失任何东西；
* **下一轮章节级重修之前**——这一章没通过闸门、尚未验收，再走一轮要付出约十次
  调用。丢弃的东西与第一条同类：一份本来也不会落盘的稿子。

评审本身不设检查点。所有场景都写完之后，整章的生成成本已经付出，一轮评审应当走
完再判断去留，否则取消等于白白丢掉一整章。代价是按下「停止」到真正停下之间会有
一段等待——按实测的调用时长，最长的一段是一轮章节级合议，约一分半。
"""

from __future__ import annotations

import threading
from typing import Optional


class GenerationCancelled(RuntimeError):
    """Raised at a safe point when the user asked to stop generation."""


class CancelToken:
    """A cancel flag that can be shared between the UI and a worker thread."""

    def __init__(self, event: Optional[threading.Event] = None):
        self._event = event or threading.Event()

    @property
    def event(self) -> threading.Event:
        return self._event

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def reset(self) -> None:
        self._event.clear()

    def raise_if_cancelled(self, message: str = "生成已被用户取消") -> None:
        if self.cancelled:
            raise GenerationCancelled(message)


def raise_if_cancelled(token: Optional["CancelToken"], message: str = "生成已被用户取消") -> None:
    """Convenience for the common `token may be None` case."""
    if token is not None:
        token.raise_if_cancelled(message)
