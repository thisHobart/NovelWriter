# -*- coding: utf-8 -*-
"""待复审记录：把闸门的结论收成一份「问题在哪」，而不是一句「请人工审核」。"""
from __future__ import annotations

import json

from core.generation import pending_review


def _review() -> dict:
    """一份贴着真实形状的未过闸评审：三个分项，只有读者盲测没过。"""
    reader_blind = {
        "stage": "chapter",
        "passed": False,
        "pass_average": 3.2,
        "average_score": 2.714,
        "scores": {"subtext": 2.0, "narrative_restraint": 2.0, "opening_pull": 4.0},
        "hard_failures": [
            {
                "code": "AI_TEMPLATE_SATURATION",
                "quote": "七年前未竟的残局，都在这一刻压弯了肩膀。",
                "problem": "全知总结压过了实战前的具体行为。",
                "change": "删去该句，改成两人对接线束时的微动作。",
                "review": "reader_blind",
            }
        ],
        "repair_instructions": ["删除分岔路口的叙述者旁白。"],
        "upgrades": [
            {
                "dimension": "subtext",
                "quote": "七年前未竟的残局，都在这一刻压弯了肩膀。",
                "missing": "克制",
                "change": "直接删去该句。",
            }
        ],
    }
    contract = {
        "stage": "chapter",
        "passed": True,
        "pass_average": 3.2,
        "average_score": 3.889,
        # 通过的分项里也会有偏低的维度，但它不是这一章被拦下的理由。
        "scores": {"chinese_prose": 3.0, "continuity": 4.0},
        "hard_failures": [],
    }
    return {
        "stage": "chapter",
        "passed": False,
        "pass_average": 3.2,
        "average_score": 3.333,
        "repair_scope": "scene_3（路口分道扬镳段落）",
        # 总评把分项的改动合并过一遍并给维度加了前缀，分项里还留着同一份。
        "hard_failures": list(reader_blind["hard_failures"]),
        "repair_instructions": list(reader_blind["repair_instructions"]),
        "upgrades": [
            {
                "dimension": "reader_blind.subtext",
                "quote": "七年前未竟的残局，都在这一刻压弯了肩膀。",
                "missing": "克制",
                "change": "直接删去该句。",
            }
        ],
        "component_reviews": {
            "contract": contract,
            "reader_blind": reader_blind,
        },
    }


def _record(**overrides) -> pending_review.PendingReview:
    defaults = dict(
        message="第 20 章在定向修订后仍未通过章节级质量检查，请人工审核",
        review=_review(),
        prose="第一段。\n\n七年前未竟的残局，都在这一刻压弯了肩膀。",
        prose_path="archive/failed_generations/chapter_20.md",
        stage=pending_review.STAGE_CHAPTER,
        snapshot={"scenes": ["一", "二", "三"], "base_revision": 4},
    )
    defaults.update(overrides)
    return pending_review.build(20, **defaults)


def test_record_ranks_hard_failures_above_scores_and_polish():
    record = _record()

    assert [issue.kind for issue in record.issues[:2]] == ["hard", "dimension"]
    assert record.hard_failures[0].title == "AI_TEMPLATE_SATURATION"
    assert record.headline == "1 处硬伤"
    assert record.verdict == "读者盲测 2.71 / 3.20"
    assert record.scope.startswith("scene_3")
    assert record.resumable


def test_only_the_failing_component_contributes_score_rows():
    """通过的分项里偏低的维度不列出来：它不是这一章被拦下的原因。"""
    dimensions = {issue.title for issue in _record().failed_dimensions}

    assert dimensions == {"subtext", "narrative_restraint"}
    assert "chinese_prose" not in dimensions


def test_the_same_change_is_not_listed_twice():
    """同一条改动在总评和分项评审里各存一份，清单里只能出现一次。"""
    record = _record()
    quotes = [issue.quote for issue in record.upgrades if issue.quote]

    assert len(record.hard_failures) == 1
    assert len(quotes) == len(set(quotes))
    assert len(record.upgrades) == 2  # 一条修复指令 + 一条润色建议


def test_asks_carry_only_the_boxes_the_author_left_checked():
    record = _record()
    everything = record.asks_for()
    hard_only = record.asks_for([record.hard_failures[0].id])

    assert len(everything) == 3
    assert hard_only == [ask for ask in everything if ask.startswith("【硬伤")]
    assert record.asks_for([]) == []
    # 分数维度没有可执行文本，勾不动，也不该混进重修提示词。
    assert all(not issue.selectable for issue in record.failed_dimensions)


def test_a_half_written_draft_cannot_be_revised_or_waived():
    record = _record(stage=pending_review.STAGE_SCENE, snapshot={})

    assert not record.resumable


def test_records_round_trip_and_disappear_once_the_chapter_is_accepted(tmp_path):
    record = _record()
    path = pending_review.save(record, str(tmp_path))
    assert json.loads(open(path, encoding="utf-8").read())["chapter_number"] == 20

    loaded = pending_review.load(str(tmp_path), 20)
    assert loaded is not None
    assert loaded.verdict == record.verdict
    assert [i.id for i in loaded.issues] == [i.id for i in record.issues]
    assert loaded.snapshot["scenes"] == ["一", "二", "三"]
    assert list(pending_review.load_all(str(tmp_path))) == [20]

    assert pending_review.clear(str(tmp_path), 20)
    assert pending_review.load(str(tmp_path), 20) is None
    assert pending_review.load_all(str(tmp_path)) == {}


def test_unreadable_records_are_treated_as_absent(tmp_path):
    """界面永远要能画出来：坏掉的记录当没有，不是抛异常。"""
    path = tmp_path / pending_review.PENDING_DIR / "chapter_7.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 半个 JSON", encoding="utf-8")

    assert pending_review.load(str(tmp_path), 7) is None
    assert pending_review.load_all(str(tmp_path)) == {}


def test_a_verdictless_record_says_so_and_offers_a_rerun(tmp_path):
    """评审自己没出结论时，记录里没有问题清单，出口是重跑评审或直接放行。"""
    record = pending_review.build(
        7,
        message="第 7 章的质量评审没能给出结论：现实合理性 质量检查未能返回有效结果",
        review={},
        prose="一场。\n\n---\n\n二场。",
        snapshot={"scenes": ["一场。", "二场。"], "contract": {"chapter": 7}},
        verdict_unavailable=True,
    )

    assert record.headline == "评审未出结论"
    assert record.needs_review_rerun
    assert record.resumable          # 正文完整，放行这条路走得通
    assert record.issues == []
    assert record.verdict == record.message

    pending_review.save(record, str(tmp_path))
    assert pending_review.load(str(tmp_path), 7).needs_review_rerun


def test_a_verdictless_half_written_draft_offers_no_rerun(tmp_path):
    """只写到一半的稿子没有可重跑的对象，重跑评审这条路不成立。"""
    record = pending_review.build(
        7,
        message="第 7 章的质量评审没能给出结论",
        review={},
        prose="只有一场。",
        stage=pending_review.STAGE_SCENE,
        snapshot={},
        verdict_unavailable=True,
    )

    assert not record.needs_review_rerun
    assert not record.resumable
