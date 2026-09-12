# -*- coding: utf-8 -*-
"""成章验证跑的汇总表：印出来的分数必须是这一章最后的裁决。

一律用临时目录：真实副本里一章有十几份留档，改动它们会毁掉下一轮的对比基线。
"""
from __future__ import annotations

import json
from pathlib import Path

from tools import run_sandbox_chapters as runner


def _review(directory: Path, stem: str, *, passed: bool, score: float, hard=()) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{stem}.json").write_text(
        json.dumps(
            {
                "passed": passed,
                "average_score": score,
                "hard_failures": [{"code": code} for code in hard],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_an_accepted_chapter_reports_the_review_that_accepted_it(tmp_path):
    """一章重修过之后，汇总表印的是首轮那份未过的评审。

    实测第 23 章：首轮 3.182 分带两条硬失败，重修一轮后 4.0 分通过并验收。汇总表
    却在「验收 是」旁边印 3.182 和那两条硬失败，读起来像是带着硬伤放行的。
    """
    directory = runner.review_dir(tmp_path, 23)
    _review(
        directory,
        "chapter_20260911_215239_092365",
        passed=False,
        score=3.182,
        hard=("READER_CONFUSION", "PROCEDURAL_IMPOSSIBILITY"),
    )
    _review(
        directory, "chapter_retry_1_20260911_215443_249452", passed=True, score=4.0
    )

    review = runner.final_chapter_review(tmp_path, 23)

    assert review["passed"] is True
    assert review["average_score"] == 4.0
    assert review["hard_failures"] == []


def test_scene_reviews_never_count_as_the_chapter_verdict(tmp_path):
    """`chapter_retry_1_scene_2` 也以 chapter 开头，按前缀取会拿到场景那份。"""
    directory = runner.review_dir(tmp_path, 24)
    _review(directory, "chapter_20260911_220000_000001", passed=False, score=3.3)
    _review(
        directory, "chapter_retry_1_20260911_220100_000002", passed=True, score=3.864
    )
    _review(
        directory,
        "chapter_retry_1_scene_3_20260911_220200_000003",
        passed=True,
        score=5.0,
    )

    assert runner.final_chapter_review(tmp_path, 24)["average_score"] == 3.864


def test_a_chapter_without_reviews_gives_an_empty_verdict(tmp_path):
    assert runner.final_chapter_review(tmp_path, 99) == {}
