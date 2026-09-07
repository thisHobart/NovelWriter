"""项目锁：可重入、有超时、旧格式项目不会卡死在验收上。"""
import json
import threading

import pytest
from filelock import Timeout

from core.generation.locks import DEFAULT_LOCK_TIMEOUT, lock_timeout, project_lock
from core.generation.story_ledger import StoryLedgerManager


def test_same_thread_nesting_does_not_deadlock(tmp_path):
    """同一线程嵌套拿同一把锁必须直接通过，不能互相等。"""
    path = str(tmp_path / ".demo.lock")
    with project_lock(path, timeout=3):
        with project_lock(path, timeout=3):
            pass  # 走到这里就说明没自锁


def test_other_thread_is_still_excluded(tmp_path):
    """可重入不能变成不互斥：另一个线程仍然要被挡在外面。"""
    path = str(tmp_path / ".demo.lock")
    outcome = []

    def contender():
        try:
            with project_lock(path, timeout=1):
                outcome.append("acquired")
        except Timeout:
            outcome.append("timeout")

    with project_lock(path, timeout=1):
        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(10)

    assert outcome == ["timeout"]


def test_timeout_is_finite_by_default():
    """默认必须是有限等待——无限等待会让界面停在一个不报错的地方。"""
    assert lock_timeout() == DEFAULT_LOCK_TIMEOUT
    assert 0 < DEFAULT_LOCK_TIMEOUT < float("inf")


def _write_pre_migration_graph(root):
    """写一份迁移前形状的叙事图：运行期字段还留在节点上。"""
    ledger_dir = root / "system" / "story_ledgers"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    (ledger_dir / "narrative_graph.json").write_text(
        json.dumps(
            {
                "version": 1,
                "revision": 1,
                "nodes": [{"id": "reveal_1", "type": "reveal", "status": "hidden"}],
                "edges": [],
                "updated_at": "",
            }
        ),
        encoding="utf-8",
    )


def test_accepting_a_chapter_on_a_pre_migration_project_finishes(tmp_path):
    """旧项目的验收曾经永久卡死：拿着账本锁去建叙事图管理器，管理器的迁移又回头
    拿同一把账本锁。这里在子线程里跑，超时未返回就判失败。"""
    _write_pre_migration_graph(tmp_path)
    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"genre": "Mystery"})

    outcome = []

    def accept():
        try:
            manager.accept_chapter(
                1,
                {"chapter": 1},
                {"average_score": 4.0},
                chapter_delta={"chapter": 1, "narrative_transitions": []},
            )
            outcome.append("returned")
        except Exception as error:  # noqa: BLE001  失败原因要能看见
            outcome.append(f"{type(error).__name__}: {error}")

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    thread.join(30)

    assert outcome == ["returned"], outcome or "验收在 30 秒内没有返回（死锁）"
