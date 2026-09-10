# -*- coding: utf-8 -*-
"""写正文之前，缺字段的旧契约要先补上，而不是让写作阶段当场判不合格。

`continuity` 与 `chapter_function` 都是后来才加进契约的。补写这条路早就写好了，
却只有 tools/run_sandbox_chapters.py 会走：从界面写正文的人撞上同一个缺陷，只能
整章重规划，把一份已经过了跨章校验、已经被后面章节依赖的规划整个重掷一次。
"""
import logging

import pytest

from core.generation import stage_pipeline
from core.generation.stage_context import make_context


class _Recorder:
    """记下补写函数收到了哪几章，以及各自返回什么。"""

    def __init__(self, outcomes):
        self.outcomes = dict(outcomes)
        self.seen = []

    def backfill_chapter_continuity(self, output_dir, chapter_number, model, parameters):
        self.seen.append(int(chapter_number))
        outcome = self.outcomes.get(int(chapter_number), False)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def patched(monkeypatch):
    def install(outcomes):
        recorder = _Recorder(outcomes)
        monkeypatch.setattr(
            stage_pipeline, "ScenePipeline", lambda host: recorder
        )
        return recorder

    return install


def _context(tmp_path):
    return make_context(str(tmp_path), model="hosted-llm", parameters={})


def test_every_chapter_about_to_be_written_is_checked(patched, tmp_path):
    recorder = patched({2: True, 4: True})
    repaired = stage_pipeline._backfill_missing_contract_fields(
        _context(tmp_path), [2, 3, 4]
    )

    assert recorder.seen == [2, 3, 4]
    assert repaired == [2, 4]


def test_a_healthy_project_reports_nothing_repaired(patched, tmp_path):
    patched({})
    assert stage_pipeline._backfill_missing_contract_fields(
        _context(tmp_path), [1, 2]
    ) == []


def test_a_failed_backfill_does_not_stop_the_stage(patched, tmp_path, caplog):
    """补不上就让写作阶段照原路报那个契约错误，那里的报错信息更准。"""
    recorder = patched({1: RuntimeError("补不上"), 2: True})
    with caplog.at_level(logging.WARNING, logger="generation.chapters"):
        repaired = stage_pipeline._backfill_missing_contract_fields(
            _context(tmp_path), [1, 2]
        )

    assert recorder.seen == [1, 2]
    assert repaired == [2]
    assert "补不上" in caplog.text


def test_repairs_are_reported_in_the_progress_stream(patched, tmp_path):
    patched({3: True})
    reported = []
    context = make_context(
        str(tmp_path),
        model="hosted-llm",
        parameters={},
        report=lambda text, fraction=-1.0: reported.append(text),
    )

    stage_pipeline._backfill_missing_contract_fields(context, [3])
    assert any("第 3 章契约补齐衔接字段" in text for text in reported)


# --- 正文写完之后，故事讲完了没有 ------------------------------------------


import json

from core.generation.narrative_graph import NarrativeGraphManager
from core.generation.story_ledger import StoryLedgerManager
from core.generation.workflow_status import COMPLETE, PARTIAL, _assess_chapters


SECTIONS = [
    {
        "section_index": 1,
        "total_sections": 1,
        "threads_opened": [
            {"id": "PT001", "thread": "工资专户能否解冻", "must_close_by_section": 1}
        ],
        "truths_introduced": [
            {"id": "T001", "fact": "公章是伪造的", "reveal_at_section": 1, "depends_on": []}
        ],
        "threads_closed": [],
    }
]


def _project_with_two_chapters(root):
    chapters = root / "story" / "content" / "chapters"
    chapters.mkdir(parents=True)
    for number in (1, 2):
        (chapters / f"chapter_{number}.md").write_text("正文。", encoding="utf-8")
    return root


def test_a_book_that_never_closed_what_it_opened_is_not_complete(tmp_path):
    _project_with_two_chapters(tmp_path)
    NarrativeGraphManager(str(tmp_path)).seed_from_structure(SECTIONS, {1: 2})
    StoryLedgerManager(str(tmp_path)).initialize({})

    status = _assess_chapters(str(tmp_path), 2)
    assert status.state == PARTIAL
    assert "PT001" in status.detail
    assert "T001" in status.detail


def test_a_project_without_a_graph_is_judged_exactly_as_before(tmp_path):
    """在此之前生成的项目图是空的，状态必须一个字不变。"""
    _project_with_two_chapters(tmp_path)

    status = _assess_chapters(str(tmp_path), 2)
    assert status.state == COMPLETE
    assert status.detail == "2 章正文已写完"


def test_reading_the_status_never_creates_a_graph_on_disk(tmp_path):
    """状态评估是刷新界面时反复调的只读操作，不该有任何落盘副作用。

    图管理器的构造函数会建目录、写一份空图，还会顺带做一次账本迁移。短篇本来就
    没有图，不能因为看了一眼状态就给它凭空造一份。
    """
    _project_with_two_chapters(tmp_path)
    before = {path for path in tmp_path.rglob("*")}

    _assess_chapters(str(tmp_path), 2)

    assert {path for path in tmp_path.rglob("*")} == before
    assert not (tmp_path / "system").exists()


def test_an_unfinished_book_is_not_audited_for_its_ending(tmp_path):
    """还缺正文的时候报「缺第几章」，不该先扯没兑现的承诺。"""
    _project_with_two_chapters(tmp_path)
    NarrativeGraphManager(str(tmp_path)).seed_from_structure(SECTIONS, {1: 5})
    StoryLedgerManager(str(tmp_path)).initialize({})

    status = _assess_chapters(str(tmp_path), 5)
    assert status.state == PARTIAL
    assert status.detail == "还缺第 3、4、5 章正文"
