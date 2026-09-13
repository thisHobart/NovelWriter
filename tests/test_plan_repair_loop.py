# -*- coding: utf-8 -*-
"""正文改不动的规划缺陷，自动回规划里改，改完重写整章。

在这之前，写正文时发现「这一句是规划要求的」只做三件事：写进评审警告、从重修清单
里撤掉、如果只剩这几条就直接报错。三件都止于告诉人，回规划那一步得作者自己动手。
实测八轮里这类缺陷出现了三十二次，最高频的是编造精确数字。

每条用例对着一个真会出事的地方，坑写在各自的 docstring 里。
"""
from __future__ import annotations

import pytest

from agents.review.legal_suspense_review_agent import DomainReview, SCORE_DIMENSIONS
from core.generation.chapter_generation_loop import (
    ChapterGenerationLoop,
    QualityGateError,
)

# 规划里那句话被正文逐字照搬，因此评审的引文与规划共用的片段远超 12 字的门槛。
FAULT_LINE = "梁浩当场断定这是顾阳波质谱分析报告里的四号神经阻断剂"
FAULTY_PLAN = f"""### 场景 1：质谱
{FAULT_LINE}。

### 场景 2：质证
律师核对门禁记录，确认签发时间。
"""
FIXED_PLAN = """### 场景 1：质谱
梁浩只描述瞳孔与皮肤的现象，把定性留给送检结果。

### 场景 2：质证
律师核对门禁记录，确认签发时间。
"""


def _scores(value: float) -> dict:
    return {dimension: value for dimension in SCORE_DIMENSIONS}


def _contract(chapter_number: int) -> dict:
    """一份过得了严格档校验的契约，照 tests/test_chapter_continuity_loop.py 的形状。"""
    return {
        "chapter": chapter_number,
        # 严格档要求契约由场景规划阶段生成；缺了这一栏，重跑第一步就停机。
        "origin": "scene_planning",
        "schema_version": 2,
        "chapter_function": "reveal",
        "continuity": {
            "picks_up_from": "上一章结尾梁浩蹲在冷柜前没有起身",
            "time_gap": "紧接上一章结尾",
            "opening_positions": [{"character": "梁浩", "location": "冷库"}],
        },
        "core_question": "阻断剂是谁注射的？",
        "facts_added": [],
        "facts_confirmed": [],
        "facts_contradicted": [],
        "timeline_events": [],
        "character_updates": [],
        "plot_thread_updates": [],
        "scene_boundaries": [],
    }


class PlanFaultReviewer:
    """整章评审每一轮都报同一条硬伤，引文逐字来自场景规划。

    规划改掉那句话之后就通过——这样才验得出「回去改规划」这一步真的发生了，而不是
    重修碰巧蒙对。
    """

    def __init__(self, *, fix_on_revise: bool = True, revised_plan: str = FIXED_PLAN):
        self.fix_on_revise = fix_on_revise
        self.revised_plan = revised_plan
        self.plan_revisions: list[dict] = []
        self.scene_revisions = 0
        self.plan_fixed = False
        self._chapter_rounds = 0

    # --- 契约与规划 ---
    def build_chapter_contract(self, chapter_number, *args, **kwargs):
        return _contract(chapter_number)

    def review_plan(self, *args, **kwargs):
        return DomainReview(stage="plan", passed=True, scores=_scores(3.6))

    def revise_plan(self, scene_plan, review, contract, unmet_asks=None, asks=None):
        self.plan_revisions.append(
            {"plan": scene_plan, "asks": list(asks or []), "review": review}
        )
        if self.fix_on_revise:
            self.plan_fixed = True
        return self.revised_plan

    # --- 场景与整章 ---
    def review_scene(self, *args, **kwargs):
        return DomainReview(stage="scene", passed=True, scores=_scores(3.6))

    def revise_scene(self, scene_content, *args, **kwargs):
        self.scene_revisions += 1
        return scene_content + " 又改了一遍。"

    def review_chapter(self, *args, **kwargs):
        if self.plan_fixed:
            return DomainReview(stage="chapter", passed=True, scores=_scores(3.8))
        self._chapter_rounds += 1
        return DomainReview(
            stage="chapter",
            passed=False,
            # 每轮略有长进，否则「同一批要求原样退回」会让重修在第一轮就收手，
            # 那条规划缺陷永远等不到第二次报出、也就永远不算确认。
            scores=_scores(2.6 + 0.1 * self._chapter_rounds),
            repair_scope="scene_1",
            repair_instructions=["把开场的总结句换成动作"],
            hard_failures=[
                {
                    "code": "UNSUPPORTED_PRECISION",
                    "problem": "肉眼无法完成质谱定性",
                    "quote": FAULT_LINE,
                    "change": "改写成现象描述",
                }
            ],
        )


def _loop(tmp_path, reviewer, **kwargs):
    return ChapterGenerationLoop(
        output_dir=str(tmp_path), model="hosted-llm", reviewer=reviewer, **kwargs
    )


PARAMETERS = {"Genre": "Mystery", "Subgenre": "Legal Thriller"}


def _run(loop, reviewer, plan=FAULTY_PLAN, chapter_number=1):
    saved: list[str] = []
    return loop.run(
        chapter_number=chapter_number,
        plan_content=plan,
        parameters=PARAMETERS,
        lore="海陵市适用统一的刑事诉讼规则。",
        generate_scene=lambda **kw: f"第{kw['scene_number']}场：{FAULT_LINE}。",
        on_plan_revised=saved.append,
    ), saved


def test_a_fault_the_prose_cannot_fix_goes_back_to_the_plan(tmp_path):
    """坑：这条路以前根本不存在，缺陷只会停在待复审等人来改。"""
    reviewer = PlanFaultReviewer()

    result, saved = _run(_loop(tmp_path, reviewer), reviewer)

    assert len(reviewer.plan_revisions) == 1
    assert saved == [FIXED_PLAN.strip()], "改完的规划必须写回去，否则下次还是照旧那一份"
    assert result.plan_content == FIXED_PLAN.strip()
    assert result.chapter_review.passed


def test_only_the_plan_mandated_asks_reach_the_plan_reviser(tmp_path):
    """坑：把整份整章评审发过去，模型会顺手把规划改成另一个样子。

    一份整章评审里绝大多数条目说的是正文，真正要动的只有被引文钉住的那一两句。
    """
    reviewer = PlanFaultReviewer()

    _run(_loop(tmp_path, reviewer), reviewer)

    asks = reviewer.plan_revisions[0]["asks"]
    assert asks, "没有显式给清单的话，revise_plan 会退回用整份评审的 asks"
    assert any(FAULT_LINE in ask for ask in asks)
    assert not any("把开场的总结句换成动作" in ask for ask in asks)


def test_the_contract_is_rebound_to_the_revised_plan(tmp_path):
    """坑：契约是拿 source_hash 认规划的，不重存，重跑第一步就停机。

    严格档下载不到匹配的契约会直接报「请返回场景规划阶段重新生成」，
    于是自动回退比不回退还糟——本来还能交一份待复审的现场。
    """
    reviewer = PlanFaultReviewer()
    loop = _loop(tmp_path, reviewer, require_planning_contract=True)
    # 用同一份参数初始化：空参数会把故事圣经锁在 general 档，那一档的重修次数更少，
    # 规划缺陷来不及被第二次报出，整条路就测不到。
    loop.ledger.initialize(PARAMETERS)
    loop.ledger.save_contract(1, _contract(1), FAULTY_PLAN)

    result, _ = _run(loop, reviewer)

    assert result.chapter_review.passed
    assert loop.ledger.load_contract(1, FIXED_PLAN.strip()) is not None


def test_the_plan_repair_only_gets_one_shot(tmp_path):
    """坑：规划与正文可以无限互相甩锅，一次来回就是重写整章、约二十次调用。"""
    reviewer = PlanFaultReviewer(fix_on_revise=False, revised_plan=FAULTY_PLAN.replace(
        "律师核对门禁记录", "律师核对门禁签到"
    ))
    loop = _loop(tmp_path, reviewer)

    with pytest.raises(QualityGateError):
        _run(loop, reviewer)

    assert len(reviewer.plan_revisions) == 1


def test_a_plan_revision_without_scenes_keeps_the_old_behaviour(tmp_path):
    """改规划要重写整章，赌不起：任何一步不成就退回今天的行为，别赔掉现场。"""
    reviewer = PlanFaultReviewer(fix_on_revise=False, revised_plan="抱歉，我无法完成。")
    loop = _loop(tmp_path, reviewer)

    with pytest.raises(QualityGateError) as caught:
        _run(loop, reviewer)

    assert caught.value.partial_scenes, "现场要一起交出去，作者才能接着改"
    assert len(reviewer.plan_revisions) == 1


def test_an_unchanged_plan_does_not_trigger_a_rewrite(tmp_path):
    """模型原样退回同一份规划时，重写整章只是把钱再花一遍。"""
    reviewer = PlanFaultReviewer(fix_on_revise=False, revised_plan=FAULTY_PLAN)
    loop = _loop(tmp_path, reviewer)

    with pytest.raises(QualityGateError):
        _run(loop, reviewer)

    assert len(reviewer.plan_revisions) == 1


class OrdinaryFailureReviewer(PlanFaultReviewer):
    """硬伤的引文出自正文自己，不是照搬规划——这种不该回规划。"""

    def review_chapter(self, *args, **kwargs):
        self._chapter_rounds += 1
        return DomainReview(
            stage="chapter",
            passed=False,
            scores=_scores(2.6 + 0.1 * self._chapter_rounds),
            repair_scope="scene_1",
            repair_instructions=["把开场的总结句换成动作"],
            hard_failures=[
                {"code": "READER_CONFUSION", "problem": "读者读不明白", "quote": "他愣住了。"}
            ],
        )


def test_a_prose_only_failure_never_touches_the_plan(tmp_path):
    """坑：把普通失败也当成规划问题，每章都会白白重写一遍。"""
    reviewer = OrdinaryFailureReviewer(fix_on_revise=False)
    loop = _loop(tmp_path, reviewer)

    with pytest.raises(QualityGateError):
        _run(loop, reviewer)

    assert reviewer.plan_revisions == []
    assert reviewer.scene_revisions > 0, "普通失败仍旧走定向重修"


def test_turning_the_budget_off_restores_the_previous_behaviour(tmp_path):
    reviewer = PlanFaultReviewer()
    loop = _loop(tmp_path, reviewer, max_plan_repairs=0)

    with pytest.raises(QualityGateError):
        _run(loop, reviewer)

    assert reviewer.plan_revisions == []
