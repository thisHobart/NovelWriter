# -*- coding: utf-8 -*-
"""项目文件锁：可重入、有超时、有固定获取顺序。

账本与叙事图各有一把跨进程互斥锁。直接用 `FileLock(path)` 有两个问题，都会让
程序停在一个不报错的地方：

1. **默认永不超时**。`filelock` 的默认 `timeout=-1` 是无限等待，一把没被释放的
   陈旧锁（上次崩溃留下的）会让界面无声地卡死，状态栏什么都不显示，「停止」也
   按不动——线程阻塞在 `acquire()` 里，根本走不到取消检查点。

2. **同一线程重入会自锁**。`FileLock` 的实例各自计数，同一个路径建两个实例、
   在同一线程里嵌套获取，第二次会阻塞在第一次上。实测确认过：章节验收先拿账本
   锁，验收过程中新建叙事图管理器，管理器的旧格式迁移会再拿一次账本锁，于是永久
   卡死。

`project_lock()` 用 `is_singleton=True` 让同一路径共用一个实例（同线程嵌套变成
计数加一），并给出一个有限超时（跨线程真冲突时抛 `LockBusy` 而不是挂住）。

## 锁的获取顺序

需要同时持有两把锁时，**先叙事图、后账本**。理由是结构性写入天然是「改图 → 让
账本跟上」这个方向（见 `narrative_graph.apply_change`），反过来没有真实需求。
反序获取在同一线程里由重入计数兜住，跨线程则会超时报错而不是死等。
"""
from __future__ import annotations

import os
from typing import Optional

from filelock import FileLock, Timeout


#: 锁内做的都是本地文件读写，正常情况远低于一秒。这个值只用来把「陈旧锁」和
#: 「真冲突」区分出来：超过它就不是在等一次正常提交，而是出了别的问题。
DEFAULT_LOCK_TIMEOUT = 120.0

#: 覆盖用的环境变量，给需要长时间批处理的场合留一个出口。
LOCK_TIMEOUT_ENV = "NOVELWRITER_LOCK_TIMEOUT"


class LockBusy(RuntimeError):
    """等锁超时。消息面向使用者，可以直接显示。"""


def lock_timeout() -> float:
    raw = os.environ.get(LOCK_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_LOCK_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_LOCK_TIMEOUT
    return value if value > 0 else DEFAULT_LOCK_TIMEOUT


def project_lock(path: str, timeout: Optional[float] = None) -> FileLock:
    """返回一把可重入、有超时的项目锁。

    同一路径返回同一个实例，所以同一线程嵌套获取是安全的；不同线程仍然互斥。
    """
    return FileLock(
        path,
        timeout=lock_timeout() if timeout is None else timeout,
        is_singleton=True,
    )


def describe_lock_timeout(path: str) -> str:
    return (
        f"等待文件锁超时（{lock_timeout():.0f} 秒）：{path}\n"
        "可能是另一个 NovelWriter 进程正在写同一个项目，"
        "也可能是上次异常退出留下了未释放的锁文件。确认没有其他进程后可以删除该文件重试。"
    )


__all__ = [
    "DEFAULT_LOCK_TIMEOUT",
    "LOCK_TIMEOUT_ENV",
    "LockBusy",
    "Timeout",
    "describe_lock_timeout",
    "lock_timeout",
    "project_lock",
]
