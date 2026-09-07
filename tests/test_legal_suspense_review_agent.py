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


def test_a_truncated_response_is_reported_as_truncated_not_as_a_missing_field():
    """回复没写完时不能拿里面的小块顶包。

    残缺对象内部还有一堆能单独解析的小对象，评分表自己就是其中之一。把评分表
    当成整份评审交出去，报出来的错会变成「评审里没有评分表」，与真正的原因差了
    十万八千里，重来一次只会更长、更容易再断一次。
    """
    truncated = (
        '{"scores": {"behavioral_logic": 3, "evidence_handling": 2},\n'
        ' "hard_failures": [{"code": "IMPOSSIBLE_MECHANISM", "quote": "门禁记录'
    )

    with pytest.raises(ValueError, match="没有写完"):
        extract_json_object(truncated)


def test_a_response_cut_off_inside_a_string_is_also_reported_as_truncated():
    with pytest.raises(ValueError, match="没有写完"):
        extract_json_object('{"scores": {"a": 1}, "repair_scope": "scene_1')


def test_a_malformed_but_complete_object_still_yields_to_the_next_one():
    """没写完和写错了要分开：写错的那个跳过去继续找，不能一竿子打死。"""
    assert extract_json_object('{"a": 1,}\n{"scores": {"a": 1}}') == {"scores": {"a": 1}}


BROKEN_KEY_REPLY = """{
  "scores": {"continuity": 3, "fair_play": 3},
  "upgrades": [
    {
      "dimension": "opening_pull",
      "quote": "他把图纸铺开。",
      "missing:开篇缺少可直观的工程标记",
      "missing": "开篇缺少可直观的工程标记",
      "change": "补一句具体工序动作。"
    }
  ]
}"""


def test_a_broken_object_is_not_mined_for_the_fragments_inside_it():
    """键名上的一个引号写错，整块就废了，但里面的小对象还是能单独解析。

    掉进去捡到的往往正是评分表自己，于是报出来的错变成「评审里没有评分表」——
    模型明明写了评分表，照这句话去改根本改不到点子上。
    """
    with pytest.raises(ValueError) as raised:
        extract_json_object(BROKEN_KEY_REPLY)

    message = str(raised.value)
    assert "语法错误" in message
    # 报错要指出坏在哪一处，重试提示词才带得上有用的信息。
    assert "missing:开篇缺少" in message


def test_a_second_object_after_a_broken_one_is_still_reachable():
    """整块跳过去，不是一竿子打死：坏块之后真有另一份回复时照样收得下。"""
    assert extract_json_object(BROKEN_KEY_REPLY + chr(10) + '{"scores": {"a": 1}}') == {
        "scores": {"a": 1}
    }


def test_a_broken_retry_falls_back_to_the_readable_first_reply():
    """第二次连 JSON 都写坏时，用第一次那份读得出来的判定，别让一个标点毁掉整章。

    第一次的问题只是低分维度没给出对应的改法——那是依据不足，补救一轮之后本就
    不再有阻断资格；第二次坏在标点上，手上仅剩的判定就是第一次那份。
    """
    first = json.dumps(
        {"scores": {dimension: 2 for dimension in SCORE_DIMENSIONS}},
        ensure_ascii=False,
    )
    prompts = []

    def replies(prompt, model=None):
        prompts.append(prompt)
        return first if len(prompts) == 1 else BROKEN_KEY_REPLY

    review = LegalSuspenseReviewAgent(
        model="hosted-llm", send_prompt_fn=replies
    ).review_plan("### 场景 1：开始", {}, {}, {})

    assert len(prompts) == 2
    assert review.passed
    assert "ungrounded_low_scores_ignored" in review.reviewer_warning
    # 评的是第一次那份的完整维度，不是坏掉的第二份里那个两项的碎片。
    assert set(review.scores) <= set(SCORE_DIMENSIONS)
    assert len(review.scores) > 2


def test_a_reply_broken_on_punctuation_is_told_so_when_asked_again():
    """坏在标点上时，重试提示词要说清楚这一点，别让模型掉头去改内容。"""
    prompts = []

    def replies(prompt, model=None):
        prompts.append(prompt)
        return BROKEN_KEY_REPLY

    with pytest.raises(DomainReviewError):
        LegalSuspenseReviewAgent(
            model="hosted-llm", send_prompt_fn=replies
        ).review_plan("### 场景 1：开始", {}, {}, {})

    assert len(prompts) == 2
    assert "语法错误" in prompts[1]
    assert "键名两侧的引号" in prompts[1]


def test_a_schema_failure_keeps_both_raw_responses():
    """只记一句错误摘要查不出原因，模型原样回了什么必须留下来。"""
    responses = [
        json.dumps({"scores": [{"dimension": "continuity", "score": 3}]}),
        json.dumps({"scores": [{"dimension": "continuity", "score": 4}]}),
    ]
    sent = iter(responses)
    reviewer = LegalSuspenseReviewAgent(
        model="hosted-llm",
        send_prompt_fn=lambda *args, **kwargs: next(sent),
    )

    with pytest.raises(DomainReviewError) as raised:
        reviewer.review_plan("### 场景 1：开始", {}, {}, {})

    assert raised.value.stage == "plan"
    assert raised.value.responses == responses


def test_a_cut_off_reply_is_asked_again_before_being_treated_as_a_bad_answer():
    """写到一半断掉不是「答得不对」，是这次调用没成，该原样再问一次。

    同一个提示词在同一个端点上，有时回完整的两千多字，有时三百多字就断。把断掉
    的那份送去补救，等于拿评审仅有的一次机会去修一份不存在的错误。
    """
    prompts = []

    def truncated(prompt, model=None):
        prompts.append(prompt)
        return '{"scores": {"continuity": 3'

    reviewer = LegalSuspenseReviewAgent(
        model="hosted-llm", send_prompt_fn=truncated
    )

    with pytest.raises(DomainReviewError):
        reviewer.review_plan("### 场景 1：开始", {}, {}, {})

    tries = reviewer.TRUNCATED_REPLY_RETRIES
    # 原样重问，不是改口去要别的东西。
    assert prompts[:tries] == [prompts[0]] * tries
    # 一直断到重问用尽，才当成答得不对：那一次要让模型写短一点，别写更长。
    assert "没有写完" in prompts[tries]
    assert "宁可少写" in prompts[tries]


def test_a_reply_that_comes_back_whole_on_the_second_ask_is_used():
    """重问一次就拿到完整回复时，评审照常成立，不消耗补救机会。"""
    replies = ['{"scores": {"continuity": 3', json.dumps(
        {"scores": {dimension: 4 for dimension in SCORE_DIMENSIONS}},
        ensure_ascii=False,
    )]
    sent = iter(replies)
    review = LegalSuspenseReviewAgent(
        model="hosted-llm", send_prompt_fn=lambda *a, **k: next(sent)
    ).review_plan("### 场景 1：开始", {}, {}, {})

    assert review.passed


def test_two_verdicts_out_of_three_still_gate_the_chapter():
    """一份评审始终问不出结果时，用在场的两份判定，并把缺的那份写进警告。

    真实情况是模型写到某类内容就被自己的安全策略截断，同一章重问多少次都一样。
    为一个拿不到的第三意见把整章停掉，停的不是有问题的稿子。
    """
    reviewer = LegalSuspenseReviewAgent(model="hosted-llm", send_prompt_fn=lambda *a, **k: "{}")
    scored = {dimension: 4 for dimension in SCORE_DIMENSIONS}
    reviewer.review_chapter = lambda *a, **k: reviewer._normalize_review("chapter", {"scores": scored})
    reviewer.review_reader_blind = lambda *a, **k: reviewer._normalize_review("chapter", {"scores": scored})

    def unavailable(*args, **kwargs):
        raise DomainReviewError("plausibility 质量检查未能返回有效结果", stage="plausibility")

    reviewer.review_plausibility = unavailable

    merged = reviewer.review_chapter_bundle("正文", {}, {}, {})

    assert merged.passed
    assert set(merged.component_reviews) == {"contract", "reader_blind"}
    assert "review_unavailable: plausibility" in merged.reviewer_warning


def test_a_chapter_with_only_one_verdict_still_goes_to_a_human():
    """三份里两份问不出来，就是真的判不了，仍旧交给人工。"""
    reviewer = LegalSuspenseReviewAgent(model="hosted-llm", send_prompt_fn=lambda *a, **k: "{}")
    reviewer.review_chapter = lambda *a, **k: reviewer._normalize_review(
        "chapter", {"scores": {d: 4 for d in SCORE_DIMENSIONS}})

    def unavailable(*args, **kwargs):
        raise DomainReviewError("未能返回有效结果")

    reviewer.review_reader_blind = unavailable
    reviewer.review_plausibility = unavailable

    with pytest.raises(DomainReviewError, match="没能给出结论"):
        reviewer.review_chapter_bundle("正文", {}, {}, {})


def _dialogue_reviewer():
    """记下每一次多轮调用收到的完整消息列表。"""
    from agents.review.domain_review_agent import SceneDialogue

    sent = []

    def conversation(messages):
        sent.append([dict(m) for m in messages])
        return f"第 {len(sent)} 次改完的正文。"

    reviewer = LegalSuspenseReviewAgent(
        model="hosted-llm",
        send_prompt_fn=lambda *a, **k: "{}",
        send_conversation_fn=conversation,
    )
    return reviewer, sent, SceneDialogue()


def _failed(scope="scene_1"):
    review = DomainReview(
        stage="scene_1",
        passed=False,
        scores={dimension: 2.0 for dimension in SCORE_DIMENSIONS},
        repair_scope=scope,
        repair_instructions=["删掉告别口号，只留动作。"],
    )
    return review


def test_the_first_repair_turn_shows_the_draft_as_the_model_own_answer():
    """一次性提问里上一稿是「别人给的一段文字」，模型容易整体重写。

    做成对话，上一稿是它自己的回答，改动更贴着被点名的那几句走。
    """
    reviewer, sent, dialogue = _dialogue_reviewer()

    reviewer.revise_scene(
        "原始正文。", _failed(), "场景规划", "上一场结尾", "下一场规划", {"chapter": 3},
        dialogue=dialogue,
    )

    messages = sent[0]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    # 开场是当初的写作依据，正文作为模型自己的回答。
    assert "章节契约" in messages[0]["content"]
    assert "场景规划" in messages[0]["content"]
    assert messages[1]["content"] == "原始正文。"
    # 最后一轮只发要改什么，不再重念契约与规划。
    assert "删掉告别口号" in messages[2]["content"]
    assert "章节契约" not in messages[2]["content"]


def test_the_second_repair_turn_keeps_what_happened_in_the_first():
    """第二轮改同一场时，模型手上还有它第一轮改了什么、当时被要求了什么。"""
    reviewer, sent, dialogue = _dialogue_reviewer()

    first = reviewer.revise_scene(
        "原始正文。", _failed(), "场景规划", "尾", "下一场", {},
        dialogue=dialogue,
    )
    second_review = _failed()
    second_review.repair_instructions = ["告别口号还在，直接删掉那一句。"]
    reviewer.revise_scene(
        first, second_review, "场景规划", "尾", "下一场", {},
        unmet_asks=["删掉告别口号，只留动作。"],
        dialogue=dialogue,
    )

    messages = sent[1]
    assert [m["role"] for m in messages] == [
        "user", "assistant", "user", "assistant", "user",
    ]
    # 第一轮的要求和它自己改出来的那一稿都还在。
    assert "删掉告别口号" in messages[2]["content"]
    assert messages[3]["content"] == first
    assert "告别口号还在" in messages[4]["content"]


def test_a_draft_changed_elsewhere_restarts_the_conversation():
    """正文被别处改过就对不上了，接着旧对话改会改到一份已经作废的稿子上。"""
    reviewer, sent, dialogue = _dialogue_reviewer()

    reviewer.revise_scene("原始正文。", _failed(), "规划", "尾", "下一场", {},
                          dialogue=dialogue)
    reviewer.revise_scene("别处换过的正文。", _failed(), "规划", "尾", "下一场", {},
                          dialogue=dialogue)

    messages = sent[1]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[1]["content"] == "别处换过的正文。"


def test_without_a_dialogue_the_single_turn_prompt_is_unchanged():
    """没给对话就还是原来那条路：一次性提问，正文写在提示词里。"""
    prompts = []
    reviewer = LegalSuspenseReviewAgent(
        model="hosted-llm",
        send_prompt_fn=lambda prompt, model=None: prompts.append(prompt) or "改完的正文。",
    )

    reviewer.revise_scene("原始正文。", _failed(), "规划", "尾", "下一场", {})

    assert prompts[0].startswith("请对场景正文进行最小范围修订")
    assert "原正文：" in prompts[0]
    assert "原始正文。" in prompts[0]


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
                    "change": "删去这份报告，改用前文已经展示的门禁记录完成推理",
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


def test_review_retries_incomplete_score_schema_then_fails_closed():
    calls = []
    incomplete_scores = {
        dimension: 4 for dimension in SCORE_DIMENSIONS if dimension != "continuity"
    }

    def incomplete_response(prompt, model=None):
        calls.append(prompt)
        return json.dumps({"scores": incomplete_scores}, ensure_ascii=False)

    reviewer = LegalSuspenseReviewAgent(
        model="hosted-llm",
        send_prompt_fn=incomplete_response,
    )

    with pytest.raises(DomainReviewError, match="质量检查未能返回有效结果"):
        reviewer.review_plan("### 场景 1：开始", {}, {}, {})

    assert len(calls) == 2
    assert "continuity" in calls[1]
    assert "schema" in calls[1].lower()


def test_review_retries_ungrounded_low_scores_then_removes_their_blocking_power():
    calls = []

    def ungrounded_response(prompt, model=None):
        calls.append(prompt)
        return json.dumps(
            {"scores": {dimension: 2 for dimension in SCORE_DIMENSIONS}},
            ensure_ascii=False,
        )

    reviewer = LegalSuspenseReviewAgent(
        model="hosted-llm",
        send_prompt_fn=ungrounded_response,
    )

    review = reviewer.review_plan("### 场景 1：开始", {}, {}, {})

    assert len(calls) == 2
    assert "upgrades" in calls[1]
    assert review.passed
    assert "ungrounded_low_scores_ignored" in review.reviewer_warning


def test_main_domain_review_ignores_only_an_ungrounded_hard_failure_after_repair():
    calls = []

    def response(prompt, model=None):
        calls.append(prompt)
        return json.dumps(
            {
                "scores": {dimension: 4 for dimension in SCORE_DIMENSIONS},
                "hard_failures": [
                    {
                        "code": "CONTINUITY_DUPLICATION",
                        "quote": "第一处……第二处",
                        "problem": "声称重复",
                        "change": "删除一处",
                    }
                ],
            },
            ensure_ascii=False,
        )

    review = LegalSuspenseReviewAgent(
        model="hosted-llm", send_prompt_fn=response
    ).review_plan("第一处在这里，第二处在后面。", {}, {}, {})

    assert len(calls) == 2
    assert review.passed
    assert review.hard_failures == []
    assert "ungrounded_hard_failures_ignored: 1" in review.reviewer_warning


def test_blocking_hard_failure_requires_quote_impact_and_minimal_change():
    content = "她把唯一的硬盘砸碎，随后从碎片里恢复了完整录像。"
    reviewer = LegalSuspenseReviewAgent(model="hosted-llm", send_prompt_fn=lambda *a, **k: "{}")
    base = {"scores": {dimension: 4 for dimension in SCORE_DIMENSIONS}}

    for incomplete in (
        {"code": "LEGAL_IMPOSSIBILITY", "quote": "不在正文中的话", "problem": "证物已毁", "change": "改用备份"},
        {"code": "LEGAL_IMPOSSIBILITY", "quote": "唯一的硬盘砸碎", "problem": "", "change": "改用备份"},
        {"code": "LEGAL_IMPOSSIBILITY", "quote": "唯一的硬盘砸碎", "problem": "证物已毁", "change": ""},
    ):
        with pytest.raises(ValueError, match="hard_failures"):
            reviewer._normalize_review(
                "chapter",
                {**base, "hard_failures": [incomplete]},
                content=content,
            )

    valid = reviewer._normalize_review(
        "chapter",
        {
            **base,
            "hard_failures": [
                {
                    "code": "LEGAL_IMPOSSIBILITY",
                    "quote": "唯一的硬盘砸碎",
                    "problem": "唯一原件被毁后，正文仍把恢复结果当作核心证明",
                    "change": "改用销毁前已封存且可校验的独立备份",
                }
            ],
        },
        content=content,
    )
    assert not valid.passed
    assert "独立备份" in valid.asks[0]


def test_review_schema_repair_requires_one_contiguous_verbatim_quote():
    calls = []

    def invalid_review(prompt, model=None):
        calls.append(prompt)
        return json.dumps(
            {
                "scores": {
                    dimension: 4
                    for dimension in LegalSuspenseReviewAgent._BLIND_DIMENSIONS
                },
                "hard_failures": [
                    {
                        "code": "READER_CONFUSION",
                        "quote": "第一处……第二处",
                        "problem": "指代冲突",
                        "change": "统一指代",
                    }
                ],
            },
            ensure_ascii=False,
        )

    reviewer = LegalSuspenseReviewAgent(
        model="hosted-llm",
        send_prompt_fn=invalid_review,
    )

    review = reviewer.review_reader_blind("第一处在这里，第二处在后面。")

    assert len(calls) == 2
    assert "连续、逐字可检索" in calls[1]
    assert "不得用省略号拼接" in calls[1]
    assert review.passed
    assert review.hard_failures == []
    assert "ungrounded_hard_failures_ignored: 1" in review.reviewer_warning


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


def _blind_reply(agent, **overrides):
    """一份合规的盲读回复：每个维度都给分，低分维度配好改法。"""
    scores = {dimension: 4 for dimension in agent._BLIND_DIMENSIONS}
    scores.update(overrides.pop("scores", {}))
    payload = {"scores": scores, "hard_failures": [], "upgrades": [], "evidence": []}
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def test_the_blind_reader_is_asked_whether_the_chapter_picks_up_where_the_last_one_stopped():
    prompts = []
    agent = LegalSuspenseReviewAgent(model="hosted-llm", send_prompt_fn=lambda p, **k: (
        prompts.append(p) or _blind_reply(agent)
    ))

    review = agent.review_reader_blind(
        "顾阳波松开五指，金属提手弹起一道细响。",
        previous_chapter_tail="他把铅盒推到桌子中央，谁也没有伸手去接。",
    )

    prompt = prompts[0]
    assert "这一章的开头是不是接着上一章" in prompt
    assert "他把铅盒推到桌子中央" in prompt
    assert "CONTINUITY_BREAK" in prompt
    assert "REDUNDANT_RESTAGING" in prompt
    assert "chapter_continuity" in review.scores


def test_the_first_chapter_is_not_scored_on_a_hand_off_it_cannot_have():
    agent = LegalSuspenseReviewAgent(
        model="hosted-llm", send_prompt_fn=lambda p, **k: _blind_reply(agent)
    )
    review = agent.review_reader_blind("第一章正文。", previous_chapter_tail="")
    assert "chapter_continuity" not in review.scores


def test_a_broken_hand_off_fails_the_blind_reader():
    quote = "与此同时，另一边的防空洞里"
    prose = f"{quote}，水珠还在往下掉。"
    agent = LegalSuspenseReviewAgent(
        model="hosted-llm",
        send_prompt_fn=lambda p, **k: _blind_reply(
            agent,
            hard_failures=[
                {
                    "code": "CONTINUITY_BREAK",
                    "quote": quote,
                    "problem": "开头另起炉灶，读者认不出它接的是上一章的哪件事",
                    "change": "改成顾阳波松开铅盒之后的下一个动作",
                }
            ],
        ),
    )
    review = agent.review_reader_blind(prose, previous_chapter_tail="他把铅盒推到桌子中央。")
    assert review.passed is False
    assert review.hard_failures[0]["code"] == "CONTINUITY_BREAK"


def test_a_hand_off_complaint_without_a_real_quote_is_not_accepted():
    """所有硬失败都要能拿引文回正文定位，新代码不例外。"""
    agent = LegalSuspenseReviewAgent(
        model="hosted-llm",
        send_prompt_fn=lambda p, **k: _blind_reply(
            agent,
            hard_failures=[
                {
                    "code": "REDUNDANT_RESTAGING",
                    "quote": "这句话正文里根本没有出现过",
                    "problem": "重新铺陈了防空洞",
                    "change": "删掉环境描写",
                }
            ],
        ),
    )
    review = agent.review_reader_blind("顾阳波松开五指。", previous_chapter_tail="他把铅盒推到桌子中央。")
    # 引文查无实据的硬失败被丢弃，评审仍然成立，只是留下警告。
    assert review.hard_failures == []


def test_the_deterministic_continuity_report_joins_the_chapter_bundle():
    reviewer = LegalSuspenseReviewAgent(model="hosted-llm", send_prompt_fn=lambda *a, **k: "{}")
    scored = {dimension: 4 for dimension in SCORE_DIMENSIONS}
    for name in ("review_chapter", "review_reader_blind", "review_plausibility"):
        setattr(
            reviewer,
            name,
            lambda *a, **k: reviewer._normalize_review("chapter", {"scores": scored}),
        )

    merged = reviewer.review_chapter_bundle(
        "坍塌的水泥板压着排水铁管。",
        {},
        {},
        {},
        continuity_report={
            "stage": "continuity",
            "passed": False,
            "hard_failures": [
                {
                    "code": "OPENING_BOILERPLATE_REUSE",
                    "quote": "坍塌的水泥板",
                    "problem": "开头与上一章开头逐字共用了 4 处片段",
                    "change": "改写成承接上一章结尾的动作",
                }
            ],
            "warnings": [],
            "repair_scope": "scene_1",
            "metrics": {},
        },
    )

    assert merged.passed is False
    assert "continuity" in merged.component_reviews
    assert merged.repair_scope == "scene_1"


def test_rewriting_the_first_scene_keeps_the_hand_off_requirements():
    """定向重修不该把刚接上的开头改回重新布景那一版。"""
    prompts = []
    agent = LegalSuspenseReviewAgent(
        model="hosted-llm",
        send_prompt_fn=lambda p, **k: prompts.append(p) or "修订后的正文。",
    )
    agent.revise_scene(
        "原正文。",
        DomainReview(stage="scene_1", passed=False, repair_instructions=["改开头"]),
        "### 场景 1：交接",
        "上一场结尾。",
        "### 场景 2：质证",
        {},
        continuity_rules=["本场开头必须直接承接上一章结尾的这件事：林衡把收据推回桌面。"],
    )
    assert "本章开场的硬性衔接要求" in prompts[0]
    assert "林衡把收据推回桌面" in prompts[0]


def test_a_later_scene_revision_gets_no_hand_off_requirements():
    prompts = []
    agent = LegalSuspenseReviewAgent(
        model="hosted-llm",
        send_prompt_fn=lambda p, **k: prompts.append(p) or "修订后的正文。",
    )
    agent.revise_scene(
        "原正文。",
        DomainReview(stage="scene_2", passed=False, repair_instructions=["改结尾"]),
        "### 场景 2：质证",
        "上一场结尾。",
        "",
        {},
    )
    assert "本章开场的硬性衔接要求" not in prompts[0]


def test_a_revision_is_told_where_to_stop_but_not_what_happens_next():
    """重修同样只给下一场的标题：越界最常发生在定向重修那一步。"""
    next_scene = (
        "### 场景 3：轮机舱截停\n"
        "* **关键行动/事件**：梁浩扑向自毁引信，铝热剂白烟灌满轮机舱。\n"
    )
    prompts = []
    agent = LegalSuspenseReviewAgent(
        model="hosted-llm",
        send_prompt_fn=lambda p, **k: prompts.append(p) or "修订后的正文。",
    )
    agent.revise_scene(
        "原正文。",
        DomainReview(stage="scene_2", passed=False, repair_instructions=["收紧结尾"]),
        "### 场景 2：冷链舱",
        "上一场结尾。",
        next_scene,
        {},
    )

    assert "### 场景 3：轮机舱截停" in prompts[0]
    assert "铝热剂白烟" not in prompts[0]
    assert "自毁引信" not in prompts[0]


def test_a_revision_also_gets_the_later_scenes_as_a_forbidden_list():
    prompts = []
    agent = LegalSuspenseReviewAgent(
        model="hosted-llm",
        send_prompt_fn=lambda p, **k: prompts.append(p) or "修订后的正文。",
    )
    agent.revise_scene(
        "原正文。",
        DomainReview(stage="scene_1", passed=False, repair_instructions=["收紧结尾"]),
        "### 场景 1：蒋静登庭",
        "上一场结尾。",
        "### 场景 2：郑娜敏签发逮捕令",
        {
            "chapter": 3,
            "scene_boundaries": [
                "scene_1: 蒋静登庭指认",
                "scene_2: 郑娜敏签发逮捕令",
                "scene_3: 法官当庭裁决梁浩无罪",
            ],
        },
    )

    assert "以下内容属于本章后面的场次，本场一个字都不许碰" in prompts[0]
    assert "scene_3: 法官当庭裁决梁浩无罪" in prompts[0]
