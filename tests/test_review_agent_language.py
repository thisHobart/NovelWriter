"""基础质量启发式必须看得懂中文。

这套检查原本只认英文，用在中文正文上全部失灵：句子按 `[.!?]` 切（中文用「。！？」，
于是整篇算一句话，每一章都被报 Content lacks proper sentence structure）；大写率对
中文恒为 0，那 +0.1 中文稿永远拿不到；按空格切词在中文里等于按行切，unique_ratio
恒约 1.0，那 +0.1 反而白送。两项偏差方向相反，把每章分数钉在 0.77 上下、与内容
无关——实测连续三章都是 0.77。
"""
from agents.review.review_agent import (
    ReviewAndRetryAgent,
    _is_cjk,
    _repetition_units,
    _split_sentences,
)


CHINESE = (
    "崔礼把卷宗放回架上。灯管闪了一下。他没有回头，只把移交单翻到最后一页。"
    "签名栏是空的。他记得自己签过。走廊尽头传来推车的声音，越来越近。"
)
ENGLISH = "He put the file back. The lamp flickered once. He did not turn around."


def test_chinese_sentence_punctuation_is_recognised():
    assert len([p for p in _split_sentences(CHINESE) if p.strip()]) >= 5
    assert len([p for p in _split_sentences(ENGLISH) if p.strip()]) >= 3


def test_language_detection():
    assert _is_cjk(CHINESE)
    assert not _is_cjk(ENGLISH)
    assert not _is_cjk("")


def test_repetition_is_measured_per_character_pair_for_chinese():
    """按空格切词在中文里等于按行切，重复度检查完全不起作用。"""
    repeated = "他看着卷宗。" * 40
    units = _repetition_units(repeated)
    assert len(set(units)) / len(units) < 0.3, "高度重复的中文必须被识别出来"

    varied = _repetition_units(CHINESE)
    assert len(set(varied)) / len(varied) > 0.7

    # 英文仍按词切
    assert _repetition_units(ENGLISH)[0] == "he"


def test_well_formed_chinese_is_not_reported_as_missing_sentence_structure():
    agent = ReviewAndRetryAgent()
    issues = agent._identify_issues("scene", CHINESE)
    assert not [item for item in issues if "sentence structure" in item]


def test_chinese_without_any_punctuation_is_still_caught():
    agent = ReviewAndRetryAgent()
    issues = agent._identify_issues("scene", "崔礼把卷宗放回架上灯管闪了一下他没有回头")
    assert [item for item in issues if "sentence structure" in item]


def test_the_score_now_moves_with_the_content():
    """此前中文稿无论写成什么样都是同一个分数。"""
    agent = ReviewAndRetryAgent()
    good = agent._assess_language_quality(CHINESE)
    repetitive = agent._assess_language_quality("他看着卷宗。" * 40)

    assert good > repetitive, (good, repetitive)
    assert good >= 0.8
    assert repetitive <= 0.6
