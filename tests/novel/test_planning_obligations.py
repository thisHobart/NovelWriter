# -*- coding: utf-8 -*-
"""重规划中间一章时，「后续章节依赖本章」按出处分开说，名字取引入它那一章写的。"""
import contextlib
import io
import json

from core.generation.planning_contract import (
    collect_history_defects,
    contract_output_instructions,
    downstream_obligations,
    load_planning_contracts,
)
from core.generation.planning_prompt import contract_instructions

FUNCTIONS = ["advance", "reveal", "relationship"]
OBLIGATION_CODES = {
    "thread_opening_dropped",
    "fact_introduction_dropped",
    "thread_closed_before_payoff",
    "deadline_before_payoff",
}


def _contract(chapter, **fields):
    return {
        "chapter": chapter,
        "origin": "scene_planning",
        "schema_version": 2,
        "chapter_function": FUNCTIONS[chapter % 3],
        **fields,
    }


def _book(root, *, first_deadline=14, extended_at=None):
    """第 1 章埋下 PT-001-01、引入 F-001-01；第 12 章自己埋下、引入一条；第 13–15 章用到它们。

    第 13 章引用 F-001-01 时没写名字，第 14 章了结 PT-001-01 时也没写名字。
    """
    folder = root / "system" / "story_ledgers" / "chapter_contracts"
    folder.mkdir(parents=True)
    chapters = {number: _contract(number) for number in range(1, 16)}
    chapters[1].update(
        plot_thread_updates=[{"id": "PT-001-01", "thread": "谁寄的信", "status": "open",
                              "deadline_chapter": first_deadline}],
        facts_added=[{"id": "F-001-01", "fact": "钥匙", "value": "在信封里"}],
    )
    if extended_at:
        chapters[extended_at]["plot_thread_updates"] = [
            {"id": "PT-001-01", "status": "open", "extend": True, "deadline_chapter": 14}
        ]
    chapters[12].update(
        plot_thread_updates=[{"id": "PT-012-01", "thread": "暗号指向哪扇门", "status": "open",
                              "deadline_chapter": 15}],
        facts_added=[{"id": "F-012-01", "fact": "暗号", "value": "三短一长"}],
    )
    chapters[13].update(facts_confirmed=[{"id": "F-001-01", "fact": "", "value": "在信封里"},
                                         {"id": "F-012-01", "fact": "暗号", "value": "三短一长"}])
    chapters[14].update(plot_thread_updates=[{"id": "PT-001-01", "status": "closed"}])
    chapters[15].update(plot_thread_updates=[{"id": "PT-012-01", "thread": "暗号指向哪扇门",
                                              "status": "closed"}])
    for number, contract in chapters.items():
        (folder / f"chapter_{number}.json").write_text(
            json.dumps(contract, ensure_ascii=False), encoding="utf-8"
        )
    return root


def _quiet(call, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return call(*args, **kwargs)


def test_replanning_chapter_12_tells_earlier_threads_apart_from_its_own(tmp_path):
    root = _book(tmp_path)
    obligations = _quiet(downstream_obligations, str(root), 12)
    threads = obligations["threads_closed_later"]
    facts = obligations["facts_used_later"]
    assert threads["PT-001-01"] == {"id": "PT-001-01", "thread": "谁寄的信", "closed_at": 14, "opened_at": 1}
    assert threads["PT-012-01"]["opened_at"] == 12
    # 第 13 章引用时没写名字，取第 1 章引入时写的。
    assert facts["F-001-01"] == {"id": "F-001-01", "fact": "钥匙", "used_at": 13, "introduced_at": 1}
    assert facts["F-012-01"]["introduced_at"] == 12

    text = _quiet(contract_instructions, str(root), 12, {})
    assert "- 悬念 PT-001-01（谁寄的信）：第 1 章已埋下，第 14 章要了结，本章不能了结它，也不能把它写没" in text
    assert "- 事实 F-001-01（钥匙）：第 1 章已引入，第 13 章要引用，本章不能推翻它，也不能把它写没" in text
    assert "- 悬念 PT-012-01（暗号指向哪扇门）：第 15 章要了结它，本章必须埋下，且 deadline_chapter 不得早于第 15 章" in text
    assert "- 事实 F-012-01（暗号）：第 13 章要引用它，本章必须引入" in text
    # 「本章必须埋下 / 引入」只留给本章自己的；全文没有空名字。
    must = [line for line in text.splitlines() if "本章必须埋下" in line or "本章必须引入" in line]
    assert len(must) == 2 and all("012" in line for line in must)
    assert "（）" not in text


def test_an_earlier_thread_due_now_is_told_to_be_extended_not_closed(tmp_path):
    root = _book(tmp_path, first_deadline=12, extended_at=13)
    text = _quiet(contract_instructions, str(root), 12, {})
    assert ("- 悬念 PT-001-01（谁寄的信）：第 1 章已埋下，第 14 章要了结，本章不能了结它，也不能把它写没；"
            "它原定第 12 章了结，本章要写一条 extend 记录，把 deadline_chapter 推迟到第 14 章或更晚") in text


def test_replanning_chapter_12_may_not_close_what_chapter_14_closes(tmp_path):
    root = _book(tmp_path)
    contracts = _quiet(load_planning_contracts, str(root))
    obligations = _quiet(downstream_obligations, str(root), 12)
    own = {
        "plot_thread_updates": [{"id": "PT-012-01", "thread": "暗号指向哪扇门", "status": "open",
                                 "deadline_chapter": 15}],
        "facts_added": [{"id": "F-012-01", "fact": "暗号", "value": "三短一长"}],
    }

    def codes(contract):
        defects = collect_history_defects(contract, contracts, 12, obligations=obligations)
        return {defect.code: str(defect) for defect in defects if defect.code in OBLIGATION_CODES}

    assert codes(_contract(12, **own)) == {}

    closes_early = _contract(12, **own)
    closes_early["plot_thread_updates"] = own["plot_thread_updates"] + [
        {"id": "PT-001-01", "thread": "谁寄的信", "status": "closed"}
    ]
    found = codes(closes_early)
    assert set(found) == {"thread_closed_before_payoff"}
    assert "第 14 章要了结悬念 PT-001-01（谁寄的信），本章不能先了结它" in found["thread_closed_before_payoff"]

    drops_own = _contract(12, plot_thread_updates=[], facts_added=[])
    found = codes(drops_own)
    assert set(found) == {"thread_opening_dropped", "fact_introduction_dropped"}
    assert "F-012-01（暗号）" in found["fact_introduction_dropped"]


def test_a_record_nobody_named_is_written_without_empty_brackets():
    obligations = {
        "threads_closed_later": {"PT-003-01": {"id": "PT-003-01", "thread": "", "closed_at": 9,
                                               "opened_at": 3}},
        "facts_used_later": {"F-005-01": {"id": "F-005-01", "fact": "", "used_at": 9,
                                          "introduced_at": 5}},
    }
    text = contract_output_instructions(6, {"open_plot_threads": []}, obligations=obligations)
    assert "- 悬念 PT-003-01：第 3 章已埋下" in text
    assert "- 事实 F-005-01：第 9 章要引用它，本章必须引入" not in text
    assert "- 事实 F-005-01：第 5 章已引入" in text
    assert "（）" not in text
