import json

from agents.review.domain_review_agent import DomainReview, DomainReviewAgent
from core.generation.domain_profiles import get_domain_profile


PROFILE = get_domain_profile("legal_suspense")
PROSE = "林衡把数据卡放回证物袋。她没有解释，只在封口处签了名字。"


def _all_fours(dimensions):
    return {
        "scores": {dimension: 4 for dimension in dimensions},
        "hard_failures": [],
        "evidence": [],
        "upgrades": [],
        "repair_scope": "",
        "repair_instructions": [],
        "strengths": [],
    }


def test_reader_blind_review_never_receives_author_intent():
    prompts = []

    def send(prompt, model=None):
        prompts.append(prompt)
        return json.dumps(_all_fours(DomainReviewAgent._BLIND_DIMENSIONS), ensure_ascii=False)

    reviewer = DomainReviewAgent("hosted-llm", profile=PROFILE, send_prompt_fn=send)
    review = reviewer.review_reader_blind(PROSE, "上一章：门外有人敲了三下。")

    assert review.passed
    assert "门外有人敲了三下" in prompts[0]
    assert "你没有章节大纲、章节契约" in prompts[0]
    assert "章节契约：" not in prompts[0]


def test_plausibility_review_sees_declared_rules_but_not_chapter_contract():
    prompts = []

    def send(prompt, model=None):
        prompts.append(prompt)
        return json.dumps(
            _all_fours(DomainReviewAgent._PLAUSIBILITY_DIMENSIONS), ensure_ascii=False
        )

    reviewer = DomainReviewAgent("hosted-llm", profile=PROFILE, send_prompt_fn=send)
    review = reviewer.review_plausibility(
        PROSE, {"domain_rules": {"model": "本世界允许记忆取证"}}
    )

    assert review.passed
    assert "本世界允许记忆取证" in prompts[0]
    assert "符合计划" in prompts[0]
    assert "章节契约：" not in prompts[0]


def test_merged_review_preserves_which_independent_gate_failed():
    passing = DomainReview(
        stage="contract", passed=True, scores={"continuity": 4}, pass_average=3.2
    )
    failing = DomainReview(
        stage="plausibility",
        passed=False,
        scores={"technical_plausibility": 2},
        hard_failures=[
            {
                "code": "UNSUPPORTED_PRECISION",
                "quote": "工业压电喷墨微观点阵血迹",
                "problem": "结论依赖未支持的精确机制",
            }
        ],
        repair_scope="scene_2",
        pass_average=3.2,
        blocking_dimensions=["technical_plausibility"],
    )

    merged = DomainReviewAgent._merge_reviews(
        "chapter", {"contract": passing, "plausibility": failing}
    )

    assert not merged.passed
    assert merged.repair_scope == "scene_2"
    assert merged.hard_failures[0]["review"] == "plausibility"
    assert "plausibility.technical_plausibility" in merged.blocking_dimensions
    assert set(merged.component_reviews) == {"contract", "plausibility"}

