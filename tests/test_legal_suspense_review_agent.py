"""Tests for structured legal-suspense review output."""

import json

import pytest

from agents.review.legal_suspense_review_agent import (
    HARD_FAILURE_CODES,
    SCORE_DIMENSIONS,
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
    passing = LegalSuspenseReviewAgent._normalize_review(
        "chapter",
        {"scores": {dimension: 3.5 for dimension in SCORE_DIMENSIONS}},
    )
    assert passing.passed

    failing = LegalSuspenseReviewAgent._normalize_review(
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
