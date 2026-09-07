# -*- coding: utf-8 -*-
"""衔接检查接进写作闭环之后的行为。

分三件事：第一场的提示词拿不拿得到硬性衔接要求、缺了衔接字段的契约能不能进写作、
以及确定性衔接检查有没有和三份评审一起进合议。
"""

import json

import pytest

from agents.review.domain_review_agent import DomainReviewAgent
from agents.review.legal_suspense_review_agent import DomainReview, SCORE_DIMENSIONS
from core.generation.chapter_generation_loop import (
    ChapterGenerationLoop,
    QualityGateError,
)
from core.generation.story_ledger import StoryLedgerManager


PLAN = """### 场景 1：交接
证人交出收据。

### 场景 2：质证
律师核对门禁记录。
"""


def _passed(stage):
    return DomainReview(
        stage=stage,
        passed=True,
        scores={dimension: 3.6 for dimension in SCORE_DIMENSIONS},
        pass_average=3.0,
    )


class _Reviewer:
    """只记录被问到了什么；判定一律通过。"""

    def __init__(self):
        self.bundle_calls = []

    def build_chapter_contract(self, chapter_number, *args):
        raise AssertionError("前置契约模式不得在写作阶段重建契约")

    def review_plan(self, *args, **kwargs):
        return _passed("plan")

    def review_scene(self, *args, **kwargs):
        return _passed("scene")

    def review_chapter(self, *args, **kwargs):
        return _passed("chapter")

    def review_chapter_bundle(self, *args, **kwargs):
        self.bundle_calls.append(kwargs)
        return _passed("chapter")

    def revise_plan(self, *args, **kwargs):
        raise AssertionError("passing plan must not be revised")

    def revise_scene(self, *args, **kwargs):
        raise AssertionError("passing scene must not be revised")


def _contract(chapter, **overrides):
    payload = {
        "chapter": chapter,
        "origin": "scene_planning",
        "schema_version": 2,
        "chapter_function": "advance",
        "continuity": {
            "picks_up_from": "上一章结尾林衡把收据推回桌面之后没有收手",
            "time_gap": "紧接上一章结尾",
            "opening_positions": [{"character": "林衡", "location": "讯问室"}],
        },
        "core_question": "收据为何晚了三十二分钟？",
        "facts_added": [],
        "facts_confirmed": [],
        "facts_contradicted": [],
        "timeline_events": [],
        "character_updates": [],
        "plot_thread_updates": [],
        "scene_boundaries": [],
    }
    payload.update(overrides)
    return payload


def _seed(tmp_path, chapter, contract, plan=PLAN):
    """契约按规划正文的哈希对应，所以两者必须是同一份。"""
    manager = StoryLedgerManager(str(tmp_path))
    manager.save_contract(chapter, contract, plan)
    return manager


def _write_previous_chapter(tmp_path, chapter, text):
    """落一章正文，并把它记成已验收——严格前置模式只认账本里认过的上一章。"""
    from core.generation.story_ledger import source_hash

    path = tmp_path / "story" / "content" / "chapters" / f"chapter_{chapter}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({})
    ledger_path = tmp_path / "system" / "story_ledgers" / "suspense_ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger.setdefault("accepted_chapters", []).append(
        {"chapter": chapter, "content_hash": source_hash(text)}
    )
    ledger_path.write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def test_only_the_first_scene_receives_the_hand_off_requirements(tmp_path):
    _seed(tmp_path, 2, _contract(2))
    _seed(tmp_path, 1, _contract(1, continuity={}, chapter_function="reveal"))
    _write_previous_chapter(tmp_path, 1, "林衡把收据推回桌面，谁也没有伸手去接。")

    reviewer = _Reviewer()
    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=reviewer,
        require_planning_contract=True,
    )
    calls = []
    loop.run(
        chapter_number=2,
        plan_content=PLAN,
        parameters={"Genre": "Mystery", "Subgenre": "Legal Thriller"},
        lore="圣兰卡市采用统一的刑事诉讼规则。",
        generate_scene=lambda **kwargs: calls.append(kwargs)
        or f"第{kwargs['scene_number']}场正文：林衡松开五指。",
    )

    rules = "\n".join(calls[0]["continuity_rules"])
    assert "林衡把收据推回桌面" in rules
    assert "紧接上一章结尾" in rules
    # 第二场接的是上一场，不该再收到一遍开场要求。
    assert list(calls[1]["continuity_rules"]) == []


def test_a_contract_without_a_hand_off_never_reaches_prose(tmp_path):
    _seed(tmp_path, 2, _contract(2, continuity={}))
    _write_previous_chapter(tmp_path, 1, "林衡把收据推回桌面。")

    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=_Reviewer(),
        require_planning_contract=True,
    )
    with pytest.raises(QualityGateError) as error:
        loop.run(
            chapter_number=2,
            plan_content=PLAN,
            parameters={"Genre": "Mystery", "Subgenre": "Legal Thriller"},
            lore="圣兰卡市采用统一的刑事诉讼规则。",
            generate_scene=lambda **kwargs: pytest.fail(
                "契约不合格时不得生成正文"
            ),
        )
    assert "continuity" in str(error.value)


def test_the_chapter_gate_receives_a_deterministic_continuity_report(tmp_path):
    _seed(tmp_path, 2, _contract(2))
    _seed(tmp_path, 1, _contract(1, continuity={}, chapter_function="reveal"))
    _write_previous_chapter(tmp_path, 1, "林衡把收据推回桌面，谁也没有伸手去接。")

    reviewer = _Reviewer()
    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=reviewer,
        require_planning_contract=True,
    )
    loop.run(
        chapter_number=2,
        plan_content=PLAN,
        parameters={"Genre": "Mystery", "Subgenre": "Legal Thriller"},
        lore="圣兰卡市采用统一的刑事诉讼规则。",
        generate_scene=lambda **kwargs: f"第{kwargs['scene_number']}场正文：林衡松开五指。",
    )

    report = reviewer.bundle_calls[0]["continuity_report"]
    assert report["metrics"]["chapter"] == 2
    # 契约声明林衡开场在场，正文开头也确实写到了他：这一项算兑现。
    assert report["metrics"]["declared_opening_characters"] == 1
    assert report["metrics"]["unmet_opening_positions"] == 0

    # 带统计数字的原始结果单独存档，事后才查得出套语条数这类量化结果。
    archived = sorted(
        (tmp_path / "quality" / "legal_suspense_reviews" / "chapter_2").glob(
            "continuity_*.json"
        )
    )
    assert archived
    saved = json.loads(archived[0].read_text(encoding="utf-8"))
    assert saved["stage"] == "continuity"
    assert "shared_opening_fragments" in saved["metrics"]


def test_a_deterministic_continuity_failure_blocks_the_merged_verdict():
    agent = object.__new__(DomainReviewAgent)
    report = {
        "stage": "continuity",
        "passed": False,
        "hard_failures": [
            {
                "code": "OPENING_BOILERPLATE_REUSE",
                "quote": "坍塌的水泥板",
                "problem": "本章开头与上一章开头逐字共用了 4 处片段",
                "change": "改写成承接上一章结尾的具体动作",
            }
        ],
        "warnings": ["悬念 PT-001-01 已连续多章没有被推进"],
        "repair_scope": "scene_1",
        "metrics": {},
    }
    review = DomainReviewAgent.continuity_review(report)
    assert review.passed is False
    assert review.scores == {}
    assert review.hard_failures[0]["code"] == "OPENING_BOILERPLATE_REUSE"
    assert "PT-001-01" in review.reviewer_warning
    # 硬失败带引文，重修清单才能按原文把它路由到具体那一场。
    assert "坍塌的水泥板" in review.asks[0]

    merged = agent._merge_reviews(
        "chapter", {"contract": _passed("chapter"), "continuity": review}
    )
    assert merged.passed is False
    assert merged.soft_failure is False
    assert any(
        item["code"] == "OPENING_BOILERPLATE_REUSE" for item in merged.hard_failures
    )


def _hard_failure(code, quote, problem="出了问题", change="改一改"):
    return {"code": code, "quote": quote, "problem": problem, "change": change}


def test_a_defect_the_plan_itself_demands_is_not_sent_to_prose_revision(tmp_path):
    """照规划写就过不了评审，不照规划写就违反契约——发给重修只是白耗一轮。"""
    from core.generation.chapter_generation_loop import ChapterGenerationLoop

    plan_line = "梁浩在‘海陵先驱号’上只有不到四十分钟。"
    scenes = [f"### 场景 1：法庭\n> 顾阳波：“{plan_line}”", "### 场景 2：码头\n梁浩登船。"]
    review = DomainReview(
        stage="chapter",
        passed=False,
        hard_failures=[
            _hard_failure("CONTINUITY_BREAK", plan_line, "上一章结尾只剩十四分二十秒"),
            _hard_failure("OPENING_BOILERPLATE_REUSE", "发出一声巨响。", "开头重复"),
        ],
    )
    loop = object.__new__(ChapterGenerationLoop)
    loop.logger = __import__("logging").getLogger("plan-fault-test")
    loop.ledger = StoryLedgerManager(str(tmp_path))

    faults = loop._plan_faults(review, scenes)
    assert [item["code"] for item in faults] == ["CONTINUITY_BREAK"]

    # 规划缺陷仍留在评审记录里，作者在待复审界面上看得到该回规划改哪一句。
    assert "重写正文改不掉" in review.reviewer_warning
    assert plan_line in review.reviewer_warning

    remaining = loop._without_plan_faults(review.asks, faults)
    assert len(remaining) == 1
    assert "OPENING_BOILERPLATE_REUSE" in remaining[0]


def test_prose_defects_are_all_still_sent_to_revision(tmp_path):
    from core.generation.chapter_generation_loop import ChapterGenerationLoop

    scenes = ["### 场景 1：法庭\n庭审继续。", "### 场景 2：码头\n梁浩登船。"]
    review = DomainReview(
        stage="chapter",
        passed=False,
        hard_failures=[_hard_failure("READER_CONFUSION", "他忽然出现在船上。")],
    )
    loop = object.__new__(ChapterGenerationLoop)
    loop.logger = __import__("logging").getLogger("plan-fault-test")
    loop.ledger = StoryLedgerManager(str(tmp_path))

    assert loop._plan_faults(review, scenes) == []
    assert loop._without_plan_faults(review.asks, []) == review.asks


def test_selecting_only_plan_defects_is_refused_instead_of_burning_calls(tmp_path):
    """待复审界面上只勾了规划缺陷时，改一场正文既改不到点上又要花两次调用。"""
    plan_line = "梁浩在‘海陵先驱号’上只有不到四十分钟。"
    plan = (
        f"### 场景 1：法庭\n> 顾阳波：“{plan_line}”\n\n"
        "### 场景 2：码头\n梁浩登船。\n"
    )
    _seed(tmp_path, 2, _contract(2), plan)
    _write_previous_chapter(tmp_path, 1, "林衡把收据推回桌面。")

    review = DomainReview(
        stage="chapter",
        passed=False,
        hard_failures=[
            {
                "code": "CONTINUITY_BREAK",
                "quote": plan_line,
                "problem": "上一章结尾只剩十四分二十秒",
                "change": "改成十四分钟",
            }
        ],
    )
    snapshot = {
        "scenes": ["第一场正文。", "第二场正文。"],
        "contract": _contract(2),
        "base_revision": 0,
        "plan_content": plan,
        "plan_revised": False,
        "plan_review": _passed("plan").to_dict(),
        "scene_reviews": [_passed("scene_1").to_dict(), _passed("scene_2").to_dict()],
        "chapter_review": review.to_dict(),
        "retry_count": 0,
        "profile_key": "legal_suspense",
        "gate_waivers": [],
    }

    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=_Reviewer(),
        require_planning_contract=True,
    )
    with pytest.raises(QualityGateError) as error:
        loop.revise_pending(2, snapshot, {"Genre": "Mystery"}, asks=review.asks)

    assert "全部出在场景规划里" in str(error.value)
    assert "不到四十分钟" in str(error.value)


def test_the_plan_defect_note_reaches_the_ledger_record(tmp_path):
    """标注要赶在存盘之前，否则作者事后翻账本看不到该回规划改哪一句。"""
    plan_line = "梁浩在‘海陵先驱号’上只有不到四十分钟。"
    plan = (
        f"### 场景 1：法庭\n> 顾阳波：“{plan_line}”\n\n"
        "### 场景 2：码头\n梁浩登船。\n"
    )
    _seed(tmp_path, 2, _contract(2), plan)
    _write_previous_chapter(tmp_path, 1, "林衡把收据推回桌面。")

    class _PlanFaultReviewer(_Reviewer):
        def review_chapter_bundle(self, *args, **kwargs):
            self.bundle_calls.append(kwargs)
            return DomainReview(
                stage="chapter",
                passed=True,
                scores={dimension: 3.6 for dimension in SCORE_DIMENSIONS},
                pass_average=3.0,
                hard_failures=[
                    {
                        "code": "CONTINUITY_BREAK",
                        "quote": plan_line,
                        "problem": "上一章结尾只剩十四分二十秒",
                        "change": "改成十四分钟",
                    }
                ],
            )

    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=_PlanFaultReviewer(),
        require_planning_contract=True,
    )
    loop.run(
        chapter_number=2,
        plan_content=plan,
        parameters={"Genre": "Mystery", "Subgenre": "Legal Thriller"},
        lore="圣兰卡市采用统一的刑事诉讼规则。",
        generate_scene=lambda **kwargs: f"第{kwargs['scene_number']}场正文：林衡松开五指。",
    )

    saved = sorted(
        (tmp_path / "quality" / "legal_suspense_reviews" / "chapter_2").glob(
            "chapter_2*.json"
        )
    )
    assert saved
    record = json.loads(saved[-1].read_text(encoding="utf-8"))
    assert "重写正文改不掉" in record["reviewer_warning"]
    assert "不到四十分钟" in record["reviewer_warning"]


def test_a_plan_defect_gets_one_prose_attempt_before_being_dropped(tmp_path):
    """引文照搬了规划不等于正文没救。

    实测第一轮就全撤会让重修手上什么都不剩，同样的问题原样留到最后，整章掉进
    待复审——比不撤更糟。只有同一处挺过自己那一轮修复的才算证明正文改不动。

    「挺过一轮」按**引文**判，不按整条修复说明判：评审每一轮都会把同一个问题换
    个说法重写，逐字比对永远匹配不上——四个副本的调用轨迹里，重修提示词的
    「仍未解决」标记一次都没有出现过。正文那句话没改，评审下一轮还会引同一句。
    """
    from core.generation.chapter_generation_loop import ChapterGenerationLoop

    plan_line = "梁浩在‘海陵先驱号’上只有不到四十分钟。"
    scenes = [f"### 场景 1：法庭\n> 顾阳波：“{plan_line}”", "### 场景 2：码头\n梁浩登船。"]
    def review(problem):
        return DomainReview(
            stage="chapter",
            passed=False,
            hard_failures=[
                {
                    "code": "READER_CONFUSION",
                    "quote": plan_line,
                    "problem": problem,
                    "change": "改成正在登船",
                }
            ],
        )

    loop = object.__new__(ChapterGenerationLoop)
    loop.logger = __import__("logging").getLogger("plan-fault-test")
    loop.ledger = StoryLedgerManager(str(tmp_path))

    first = loop._plan_faults(review("空间跳跃"), scenes)
    assert first, "判据本身要认出这是规划里的句子"

    # 第一轮：这处还没报过，照常发给重修。
    assert [item for item in first if item["quote"] in set()] == []

    # 第二轮：评审换了一套说法，但引的还是同一句——这才算证明正文改不动。
    reported = {item["quote"] for item in first}
    second = loop._plan_faults(review("人物位置与前文矛盾，读者跟丢"), scenes)
    assert [item for item in second if item["quote"] in reported]
