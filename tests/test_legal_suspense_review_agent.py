"""Tests for structured legal-suspense review output."""

import json

import pytest

from agents.review.legal_suspense_review_agent import (
    HARD_FAILURE_CODES,
    SCORE_DIMENSIONS,
    DomainReview,
    DomainReviewError,
    LegalSuspenseReviewAgent,
    extract_json_object,
)


def test_extract_json_object_accepts_fence_and_leading_explanation():
    result = extract_json_object('说明如下：\n```json\n{"passed": true, "score": 3.5}\n```')
    assert result == {"passed": True, "score": 3.5}


def test_contract_response_is_normalized():
    response = {
        "core_question": "证词为何改变？",
        "reader_knows_after": "错误类型也应被归一化",
        "scene_boundaries": [{"scene_number": 1}],
    }
    reviewer = LegalSuspenseReviewAgent(
        model="hosted-llm",
        send_prompt_fn=lambda *args, **kwargs: json.dumps(response, ensure_ascii=False),
    )

    contract = reviewer.build_chapter_contract(
        3,
        "### 场景 1：质证\n质证开始。",
        {},
        "世界观",
        {},
        {},
    )

    assert contract["chapter"] == 3
    assert contract["reader_knows_after"] == []
    assert contract["scene_boundaries"] == [{"scene_number": 1}]


def test_review_gate_uses_scores_and_hard_failures():
    # 评分门槛与硬失败代码现在来自领域档案，因此要通过实例访问。
    reviewer = LegalSuspenseReviewAgent(model="hosted-llm", send_prompt_fn=lambda *a, **k: "{}")

    passing = reviewer._normalize_review(
        "chapter",
        {"scores": {dimension: 3.5 for dimension in SCORE_DIMENSIONS}},
    )
    assert passing.passed

    failing = reviewer._normalize_review(
        "chapter",
        {
            "scores": {dimension: 4 for dimension in SCORE_DIMENSIONS},
            "hard_failures": [
                {
                    "code": "UNSEEDED_SOLUTION",
                    "quote": "一份此前没有出现的报告",
                    "problem": "决定性证据没有伏笔",
                },
                {"code": "UNKNOWN_CODE", "quote": "忽略", "problem": "忽略"},
            ],
        },
    )

    assert not failing.passed
    assert failing.hard_failures[0]["code"] in HARD_FAILURE_CODES
    assert len(failing.hard_failures) == 1


def test_review_retries_invalid_json_then_fails_closed():
    calls = []

    def invalid_response(prompt, model=None):
        calls.append(prompt)
        return "这不是 JSON"

    reviewer = LegalSuspenseReviewAgent(
        model="hosted-llm",
        send_prompt_fn=invalid_response,
    )

    with pytest.raises(DomainReviewError, match="质量检查未能返回有效结果"):
        reviewer.review_plan("### 场景 1：开始", {}, {}, {})

    assert len(calls) == 2


# --- 重修方向 ---------------------------------------------------------------
#
# 评审经常一边判 passed=False、一边把 repair_instructions 留空并写
# repair_scope="none"：它认为没有硬伤，只是分数没到门槛。原样把这种评审回抛给
# 模型等于让它「照旧重写一遍」，重修必然原地打转。


def _soft_failure(scores):
    return DomainReview(
        stage="scene_1",
        passed=False,
        scores=scores,
        repair_scope="none",
        repair_instructions=[],
        pass_average=3.2,
    )


def test_revision_focus_names_the_weakest_dimensions_when_no_repairs_are_listed():
    scores = {dimension: 3.5 for dimension in SCORE_DIMENSIONS}
    scores["fair_play"] = 2.5
    scores["continuity"] = 2.5

    focus = LegalSuspenseReviewAgent.revision_focus(_soft_failure(scores))

    assert "fair_play" in focus and "continuity" in focus
    assert "reversal" not in focus
    assert "门槛 3.20 分" in focus


def test_revision_focus_stays_out_of_the_way_when_the_reviewer_gave_instructions():
    review = _soft_failure({dimension: 3.0 for dimension in SCORE_DIMENSIONS})
    review.repair_instructions = ["删掉第二段重复的门禁描写"]

    assert LegalSuspenseReviewAgent.revision_focus(review) == ""


def test_revision_focus_is_silent_without_a_real_threshold():
    """门槛为 0 的评审不是走 _normalize_review 算出来的，分差无意义。"""
    review = _soft_failure({dimension: 3.0 for dimension in SCORE_DIMENSIONS})
    review.pass_average = 0.0

    assert LegalSuspenseReviewAgent.revision_focus(review) == ""


def test_revise_scene_prompt_carries_the_shortfall_direction():
    prompts = []

    def capture(prompt, model=None):
        prompts.append(prompt)
        return "修订后的正文"

    reviewer = LegalSuspenseReviewAgent(model="hosted-llm", send_prompt_fn=capture)
    scores = {dimension: 3.0 for dimension in SCORE_DIMENSIONS}
    scores["chinese_prose"] = 2.5

    reviewer.revise_scene("原正文", _soft_failure(scores), "### 场景 1：收据", "", "", {})

    assert "重修方向" in prompts[0]
    assert "chinese_prose" in prompts[0]


def test_revision_focus_does_not_enumerate_every_dimension_when_scores_are_tied():
    """全维度同分时逐一列出等于没说，改为点明整场平庸。"""
    review = _soft_failure({dimension: 3.0 for dimension in SCORE_DIMENSIONS})

    focus = LegalSuspenseReviewAgent.revision_focus(review)

    assert "所有维度都停在 3.0 分" in focus
    assert "fair_play" not in focus
