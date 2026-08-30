"""重试预算只有两次，每一次都必须换来新结果。

线上账本里的实测数据说明它没有：17 份未过闸的评审中有 6 份既没有硬失败也没有任何
修复项，其中一份的 evidence 逐条写着「通过」、strengths 列了三条，分数却是全维度
3.0、门槛 3.2。这种评审回抛给模型等于「照旧再写一遍」，两次重试必然空烧四次调用。

这里锁住三件事：评审判不通过就必须说清改哪里；重修提示词把要改的条目摆在最前面而
不是埋在整份评审 JSON 里；同一批要求原样退回来时立刻收手。
"""

import json

import pytest

from agents.review.domain_review_agent import DomainReview, DomainReviewAgent
from core.generation.chapter_generation_loop import ChapterGenerationLoop, waiver_reason
from core.generation.domain_profiles import get_domain_profile


PROFILE = get_domain_profile("legal_suspense")
SCENE = "郑娜敏把收据摊在桌上，指腹压住那行被涂改过的时间。骑楼外的雨没停。"


def reviewer(send=lambda *args, **kwargs: "{}"):
    return DomainReviewAgent(model="hosted-llm", profile=PROFILE, send_prompt_fn=send)


def passing_json():
    return json.dumps(
        {"scores": {dimension: 4 for dimension in PROFILE.score_dimensions}},
        ensure_ascii=False,
    )


def flat_scores(value=3.0):
    return {dimension: value for dimension in PROFILE.score_dimensions}


def test_merged_bundle_preserves_a_component_only_soft_failure():
    contract = DomainReview(
        stage="chapter", passed=True, scores={"focus": 4.0}, pass_average=3.2
    )
    blind = DomainReview(
        stage="reader_blind",
        passed=False,
        scores={"readability": 3.14},
        pass_average=3.2,
    )
    plausibility = DomainReview(
        stage="plausibility", passed=True, scores={"realism": 4.0}, pass_average=3.2
    )

    merged = DomainReviewAgent._merge_reviews(
        "chapter",
        {"contract": contract, "reader_blind": blind, "plausibility": plausibility},
    )

    assert merged.average_score > merged.pass_average
    assert merged.soft_failure is True
    assert merged.shortfall == pytest.approx(0.06)
    assert waiver_reason(merged, "三路章节审阅") is not None


def test_merged_bundle_never_treats_a_component_hard_failure_as_soft():
    failed = DomainReview(
        stage="reader_blind",
        passed=False,
        scores={"readability": 3.14},
        hard_failures=[{"code": "READER_CONFUSION"}],
        pass_average=3.2,
    )
    merged = DomainReviewAgent._merge_reviews(
        "chapter",
        {
            "contract": DomainReview(stage="chapter", passed=True, scores={"focus": 4.0}),
            "reader_blind": failed,
        },
    )

    assert merged.soft_failure is False
    assert waiver_reason(merged, "三路章节审阅") is None


# --- 评审必须交出可执行的东西 ---------------------------------------------


def test_a_scored_upgrade_becomes_an_actionable_repair():
    """全维度 3.0、没有硬失败——原先这就是一份「失败但无从下手」的评审。"""
    scores = flat_scores(4.0)
    scores["fair_play"] = 2.5
    review = reviewer()._normalize_review(
        "scene_1",
        {
            "scores": scores,
            "upgrades": [
                {
                    "dimension": "fair_play",
                    "quote": "那行被涂改过的时间",
                    "missing": "涂改前的原始数字",
                    "change": "让郑娜敏读出被覆盖的那一版时间",
                }
            ],
        },
        content=SCENE,
    )

    assert not review.passed
    assert review.has_actionable_repair
    assert "涂改前的原始数字" in review.asks[0]


def test_low_scores_without_grounded_upgrades_are_invalid_review_schema():
    """完整低分表也不能替代带原文证据的可执行意见。"""
    with pytest.raises(ValueError, match="upgrades 缺少低分维度"):
        reviewer()._normalize_review(
            "scene_1",
            {"scores": flat_scores(2.0), "upgrades": []},
            content=SCENE,
        )


def test_contract_blank_optional_beats_do_not_force_repairs_or_schema_retry():
    calls = []
    scores = flat_scores(4.0)
    scores.update(
        {
            "reversal": 0,
            "moral_gray": 0,
            "attack_defense": 0,
            "personal_cost": 0,
        }
    )

    def send(prompt, model=None):
        calls.append(prompt)
        return json.dumps(
            {
                "scores": scores,
                "hard_failures": [],
                "upgrades": [],
                "repair_instructions": [],
            },
            ensure_ascii=False,
        )

    review = reviewer(send).review_chapter(
        SCENE,
        {
            "chapter": 1,
            "reversal": "",
            "attack_move": "",
            "defense_move": "",
            "personal_cost": "",
        },
        {},
        {},
    )

    assert review.passed
    assert len(calls) == 1
    assert set(review.scores).isdisjoint(
        {"reversal", "moral_gray", "attack_defense", "personal_cost"}
    )
    assert "personal_cost" in review.reviewer_warning


def test_second_schema_attempt_preserves_grounded_hard_failure_and_ignores_only_ungrounded_scores():
    calls = []
    raw = {
        "scores": {dimension: 3 for dimension in DomainReviewAgent._BLIND_DIMENSIONS},
        "hard_failures": [
            {
                "code": "CHARACTER_LOGIC_BREAK",
                "quote": "郑娜敏把收据摊在桌上",
                "problem": "角色主动送出只有凶手知道的答案，推理失去可信度",
                "change": "改为被追问后无意失言",
            }
        ],
        "upgrades": [
            {
                "dimension": "opening_pull",
                "quote": "郑娜敏把收据摊在桌上",
                "missing": "动作缺少阻力",
                "change": "让证人先拒绝交出收据",
            }
        ],
        "repair_scope": "scene_1",
        "repair_instructions": ["把主动泄密改成被动失言"],
    }

    def send(prompt, model=None):
        calls.append(prompt)
        return json.dumps(raw, ensure_ascii=False)

    review = reviewer(send).review_reader_blind(SCENE)

    assert len(calls) == 2
    assert review.passed is False
    assert review.hard_failures[0]["code"] == "CHARACTER_LOGIC_BREAK"
    assert review.scores["opening_pull"] == 3
    assert review.scores["reader_orientation"] == 4
    assert "ungrounded_low_scores_ignored" in review.reviewer_warning


def test_style_polish_prompt_locks_story_state_and_scene_boundaries():
    prompts = []

    def send(prompt, model=None):
        prompts.append(prompt)
        return "修订后第一场。\n\n---\n\n修订后第二场。"

    result = reviewer(send).revise_chapter_style(
        "原第一场。\n\n---\n\n原第二场。",
        {"chapter": 3, "reader_must_not_know_yet": ["秘密"]},
        ["长句过多"],
        ["喉头滚动", "额头虚汗"],
    )

    assert "不得增加、删除或改变事实、时间、地点、人物知识" in prompts[0]
    assert "保留原有两个场景" in prompts[0]
    assert "喉头滚动" in prompts[0]
    assert "---" in result


def test_an_upgrade_without_a_real_quote_is_dropped():
    """定位不到原文的意见没法照着改，留着只会让重修凭空发挥。"""
    with pytest.raises(ValueError, match="upgrades 缺少低分维度"):
        reviewer()._normalize_review(
            "scene_1",
            {
                "scores": flat_scores(),
                "upgrades": [
                    {"dimension": "fair_play", "quote": "根本不在正文里的一句", "change": "改点什么"},
                    {"dimension": "并不存在的维度", "quote": "骑楼外的雨没停", "change": "改点什么"},
                    {"dimension": "reversal", "quote": "骑楼外的雨没停", "change": ""},
                ],
            },
            content=SCENE,
        )


def test_the_review_prompt_defines_what_a_three_means():
    """没有标尺时模型把一切都打成 3 分，而 3.0 正好卡在 3.2 门槛下方。"""
    prompts = []
    reviewer(lambda prompt, model=None: prompts.append(prompt) or passing_json()).review_scene(
        SCENE, "### 场景 1：收据", 1, "", "", {}, {}, {}
    )

    assert "3＝达标但可替换" in prompts[0]
    assert "都必须在 upgrades 里出现一条" in prompts[0]


def test_a_re_review_is_told_what_the_last_round_asked_for():
    """每轮从零重评，等于让模型每次挑一批不同的毛病，重修永远追不上。"""
    prompts = []
    reviewer(lambda prompt, model=None: prompts.append(prompt) or passing_json()).review_scene(
        SCENE,
        "### 场景 1：收据",
        1,
        "",
        "",
        {},
        {},
        {},
        repairs_requested=["补上收据被涂改前的时间"],
    )

    assert "补上收据被涂改前的时间" in prompts[0]
    assert "先逐条判断上面每一项是否已经落实" in prompts[0]


# --- 重修提示词 -------------------------------------------------------------


def test_the_revision_brief_leads_with_the_checklist():
    prompts = []
    review = DomainReview(
        stage="scene_1",
        passed=False,
        scores=flat_scores(),
        repair_instructions=["删掉第二段重复的门禁描写"],
        strengths=["骑楼的湿冷写得准确"],
        pass_average=3.2,
    )

    reviewer(lambda prompt, model=None: prompts.append(prompt) or "改好的正文").revise_scene(
        SCENE, review, "### 场景 1：收据", "", "", {}
    )

    assert "必须修复的问题" in prompts[0]
    assert "1. 删掉第二段重复的门禁描写" in prompts[0]
    # 优点保留下来是为了防止重修把已经写好的地方一起改掉。
    assert "骑楼的湿冷写得准确" in prompts[0]


def test_a_repeated_ask_is_called_out_as_repeated():
    prompts = []
    review = DomainReview(
        stage="scene_1",
        passed=False,
        scores=flat_scores(),
        repair_instructions=["删掉第二段重复的门禁描写", "补上收据的原始时间"],
        pass_average=3.2,
    )

    reviewer(lambda prompt, model=None: prompts.append(prompt) or "改好的正文").revise_scene(
        SCENE,
        review,
        "### 场景 1：收据",
        "",
        "",
        {},
        unmet_asks=["删掉第二段重复的门禁描写"],
    )

    body = prompts[0]
    assert "删掉第二段重复的门禁描写（上一轮已提出，仍未解决）" in body
    assert "补上收据的原始时间（上一轮已提出" not in body
    assert "直接改写被引用的那句原文本身" in body


def test_the_brief_falls_back_to_the_shortfall_when_nothing_was_listed():
    """评审什么都没说时仍要给个着力点，否则重修只能原样再写一遍。"""
    review = DomainReview(
        stage="scene_1", passed=False, scores=flat_scores(), pass_average=3.2
    )

    brief = reviewer().revision_brief(review)

    assert "重修方向" in brief


# --- 无进展检测 -------------------------------------------------------------


def _review_with(instructions):
    return DomainReview(
        stage="scene_1",
        passed=False,
        scores=flat_scores(),
        repair_instructions=list(instructions),
        pass_average=3.2,
    )


@pytest.mark.parametrize(
    "review, requested, stuck",
    [
        (_review_with(["补上收据的原始时间"]), ["补上收据的原始时间"], True),
        (_review_with(["补上收据的原始时间"]), ["删掉重复的门禁描写"], False),
        (_review_with(["补上收据的原始时间"]), [], False),
        (_review_with([]), ["补上收据的原始时间"], True),
    ],
    ids=["same-ask-returned", "new-ask", "first-round", "no-feedback-at-all"],
)
def test_retry_is_stuck_only_when_another_round_cannot_change_anything(
    review, requested, stuck
):
    assert ChapterGenerationLoop._retry_is_stuck(review, requested) is stuck


def test_unmet_asks_are_the_ones_that_survived_their_own_repair():
    previous = ["补上收据的原始时间", "删掉重复的门禁描写"]
    review = _review_with(["补上收据的原始时间", "对白太长"])

    assert review.unmet_asks_from(previous) == ["补上收据的原始时间"]
