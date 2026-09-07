"""评审提示词按稳定度分层：同一章的多次评审必须共用一段很长的开头。

自建端点的前缀缓存只认逐字相同的开头。重排之前，同一章 25～32 次调用之间的
公共前缀是 0 个字符——首行那句「评审 scene_1」就足以把每一次调用岔开，于是
89% 的重复内容每次都要重新算一遍。

这里不打模型，只量提示词本身：公共前缀有多长、稳定块有没有排在变动内容前面、
以及重排之后每一段原有信息是否都还在。
"""
import json
from os.path import commonprefix

from agents.review.domain_review_agent import DomainReviewAgent
from core.generation.domain_profiles import get_domain_profile


PROFILE = get_domain_profile("legal_suspense")

CONTRACT = {
    "chapter": 24,
    "core_question": "梁浩能否在冷链舱找到活体实验受害者？",
    "reader_knows_after": ["HL-0941 集装箱内有幸存者"],
    "scene_boundaries": [
        {"scene_number": 1, "must_do": ["索降登船"], "end_state": "站上甲板"},
        {"scene_number": 2, "must_do": ["打开冷链舱"], "end_state": "看见幸存者"},
    ],
}
CASE_BIBLE = {
    "domain_rules": {"forensic": "假死状态需 NB-4 阻断剂维持"},
    "truths": ["宏升物流经营跨境器官转运"] * 12,
}
LEDGER = {"revision": 7, "accepted_chapters": [{"chapter": n} for n in range(1, 24)]}


def _capture(reviewer_call):
    """跑一次评审，把发出去的提示词原样接住。"""
    prompts = []

    def send(prompt, model=None):
        prompts.append(prompt)
        return json.dumps(
            {
                "scores": {d: 4 for d in PROFILE.score_dimensions},
                "hard_failures": [],
                "evidence": [],
                "upgrades": [],
                "repair_scope": "",
                "repair_instructions": [],
                "strengths": [],
            },
            ensure_ascii=False,
        )

    reviewer = DomainReviewAgent("hosted-llm", profile=PROFILE, send_prompt_fn=send)
    reviewer_call(reviewer)
    return prompts[0]


def _scene_prompt(number):
    return _capture(
        lambda r: r.review_scene(
            scene_content=f"第{number}场正文。梁浩踩上湿滑的甲板。",
            scene_plan=f"### 场景 {number}：登船",
            scene_number=number,
            previous_scene_tail="上一场结尾。",
            next_scene_plan="### 场景 3：冷链舱",
            contract=CONTRACT,
            case_bible=CASE_BIBLE,
            suspense_ledger=LEDGER,
        )
    )


def _chapter_prompt():
    return _capture(
        lambda r: r.review_chapter(
            chapter_content="整章正文。梁浩踩上湿滑的甲板。",
            contract=CONTRACT,
            case_bible=CASE_BIBLE,
            suspense_ledger=LEDGER,
        )
    )


def test_reviews_of_one_chapter_share_a_long_verbatim_prefix():
    prompts = [_scene_prompt(1), _scene_prompt(2), _chapter_prompt()]
    shared = commonprefix(prompts)

    # 稳定块（档案规则、评分标准、案件圣经、章节契约、故事账本）都要落在共用的
    # 那一段里；只有「本次评审对象」往后才允许分岔。
    assert len(shared) > 2000, f"公共前缀只有 {len(shared)} 字"
    assert min(len(shared) / len(p) for p in prompts) > 0.5

    assert "章节契约" in shared
    assert PROFILE.bible_noun in shared
    assert "故事账本" in shared
    assert "评分维度为0到4分" in shared

    # 分岔点就是「本次评审对象」，它之后才是每次不同的内容。
    for prompt in prompts:
        assert prompt[len(shared):].lstrip().startswith("本次评审对象") or (
            "本次评审对象" in prompt[max(0, len(shared) - 40):]
        )


def test_variable_parts_still_reach_the_model():
    """重排不能顺手弄丢任何一段：正文、场景规划、上一场结尾都要还在。"""
    prompt = _scene_prompt(2)

    assert "本次评审对象：scene_2" in prompt
    assert "第2场正文。梁浩踩上湿滑的甲板。" in prompt
    assert "### 场景 2：登船" in prompt
    assert "上一场结尾。" in prompt
    assert "### 场景 3：冷链舱" in prompt
    assert "只输出 JSON" in prompt
    assert "unregistered_narrative_elements" in prompt


def test_repair_round_only_changes_the_tail():
    """带上一轮修复要求的复评，前缀必须和首评完全一致。"""
    first = _chapter_prompt()
    with_repairs = _capture(
        lambda r: r.review_chapter(
            chapter_content="整章正文。梁浩踩上湿滑的甲板。",
            contract=CONTRACT,
            case_bible=CASE_BIBLE,
            suspense_ledger=LEDGER,
            repairs_requested=["把第二场的时间读数改回十四分二十秒"],
        )
    )

    shared = commonprefix([first, with_repairs])
    assert "故事账本" in shared
    assert "把第二场的时间读数改回十四分二十秒" in with_repairs
    assert "把第二场的时间读数改回十四分二十秒" not in shared
