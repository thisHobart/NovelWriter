from core.generation.narrative_quality import (
    analyze_narrative_quality,
    summarize_narrative_reports,
)


def test_storyscope_signals_are_transparent_counts_not_a_pass_fail_score():
    text = (
        "他终于明白，这才意味着真正的代价。于是他喉咙发紧，冷汗顺着后背往下流。"
        "她没有回答，只把钥匙推开。原来门后的人并非凶手。"
    )

    report = analyze_narrative_quality(text)

    assert report.thematic_explicitness.count >= 2
    assert report.embodied_emotion.count >= 2
    assert report.behavioral_emotion.count >= 1
    assert report.recontextualization.count >= 2
    assert "不是评分答案" in report.prompt_block()


def test_corpus_summary_exposes_repeated_shape_across_chapters():
    summary = summarize_narrative_reports(
        ["他终于明白。这意味着代价。", "与此同时，另一边的人重新解释了证词。"]
    )

    assert summary["chapter_count"] == 2
    assert "thematic_explicitness" in summary["averages"]
    assert len(summary["chapters"]) == 2


def test_variant_embodied_emotion_templates_are_counted():
    prose = (
        "方屿的喉头动了一下，呼吸微微发紧，额头冒出虚汗。"
        "他的嘴唇抽动，手指颤抖，指尖又痉挛了一下。"
    )

    report = analyze_narrative_quality(prose)

    assert report.embodied_emotion.count >= 5
    assert any("身体化情绪套语" in warning for warning in report.warnings)
