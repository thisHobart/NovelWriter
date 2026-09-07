import json

from agents.review.domain_review_agent import DomainReviewAgent
from core.evaluation import live_chapter_probe
from core.generation.llm_trace import trace_model_call


def _passing(dimensions):
    return json.dumps(
        {
            "scores": {dimension: 4 for dimension in dimensions},
            "hard_failures": [],
            "evidence": [],
            "upgrades": [],
            "repair_scope": "",
            "repair_instructions": [],
            "strengths": ["通过"],
        },
        ensure_ascii=False,
    )


def test_live_chapter_probe_uses_isolated_reviews_and_accepts(monkeypatch, tmp_path):
    monkeypatch.setattr(live_chapter_probe, "_reachable", lambda host, port: (True, ""))

    def fake_send(prompt, model=None):
        def invoke():
            if "中文类型小说盲读审稿人" in prompt:
                return _passing(DomainReviewAgent._BLIND_DIMENSIONS)
            if "现实合理性与专业机制审稿人" in prompt:
                return _passing(DomainReviewAgent._PLAUSIBILITY_DIMENSIONS)
            if "本次评审对象：chapter" in prompt:
                return _passing(live_chapter_probe.get_domain_profile("legal_suspense").score_dimensions)
            return (
                "雨落在扫描室窗外。程砚看着21:17的门禁记录，没有急着下结论。"
                "记录只能说明陈岚的卡被读取，不能说明拿卡的人是谁。"
                "墙角的硬盘已经损坏，他让勘查员原样封存，没有尝试恢复。"
                "门自动合上，新的问题留在证物袋外：谁在雨夜拿过这张卡？"
            )

        return trace_model_call(
            prompt=prompt,
            backend="scripted-test-double",
            model=model or "test",
            invoke=invoke,
        )

    monkeypatch.setattr(live_chapter_probe, "send_prompt", fake_send)

    code, report = live_chapter_probe.run_probe(tmp_path)

    assert code == 0
    assert report["mode"] == "real"  # tool mode; backend trace identifies the test double
    assert report["attempted_model_calls"] == 4
    assert report["acceptance"]["revision"] == 1
    assert all(report["isolation"].values())
    assert report["continue_to_three_chapters"] is True


def test_live_chapter_probe_revises_once_and_keeps_an_improvement(monkeypatch, tmp_path):
    monkeypatch.setattr(live_chapter_probe, "_reachable", lambda host, port: (True, ""))
    chapter_review_count = 0
    original = "程砚看着门禁卡。记录只能证明卡被读取，不能证明是谁拿卡。"
    revised = "程砚否定了直接锁人的结论，排查范围因此扩大。他把损坏硬盘原样封存。"

    def fake_send(prompt, model=None):
        def invoke():
            nonlocal chapter_review_count
            if "中文类型小说盲读审稿人" in prompt:
                return _passing(DomainReviewAgent._BLIND_DIMENSIONS)
            if "现实合理性与专业机制审稿人" in prompt:
                return _passing(DomainReviewAgent._PLAUSIBILITY_DIMENSIONS)
            if "本次评审对象：chapter" in prompt:
                chapter_review_count += 1
                if chapter_review_count == 1:
                    dimensions = live_chapter_probe.get_domain_profile(
                        "legal_suspense"
                    ).score_dimensions
                    scores = {dimension: 4 for dimension in dimensions}
                    low_dimensions = (
                        "focus_depth",
                        "concrete_detail",
                        "chinese_prose",
                    )
                    for dimension in low_dimensions:
                        scores[dimension] = 0
                    return json.dumps(
                        {
                            "scores": scores,
                            "hard_failures": [],
                            "evidence": [],
                            "upgrades": [
                                {
                                    "dimension": dimension,
                                    "quote": "程砚看着门禁卡",
                                    "missing": "排查没有代价",
                                    "change": "写明排查范围扩大",
                                }
                                for dimension in low_dimensions
                            ],
                            "repair_scope": "scene_1",
                            "repair_instructions": ["写明排查范围扩大"],
                            "strengths": [],
                        },
                        ensure_ascii=False,
                    )
                return _passing(
                    live_chapter_probe.get_domain_profile(
                        "legal_suspense"
                    ).score_dimensions
                )
            if "请对场景正文进行最小范围修订" in prompt:
                return revised
            return original

        return trace_model_call(
            prompt=prompt,
            backend="scripted-test-double",
            model=model or "test",
            invoke=invoke,
        )

    monkeypatch.setattr(live_chapter_probe, "send_prompt", fake_send)

    code, report = live_chapter_probe.run_probe(tmp_path)

    assert code == 0
    assert report["attempted_model_calls"] == 8
    assert report["retry_count"] == 1
    assert report["retry_history"][0]["improved"] is True
    assert report["baseline_prose"] == original
    assert report["prose"] == revised
    assert report["acceptance"]["revision"] == 1


def test_print_json_falls_back_to_ascii_on_non_utf_stdout(monkeypatch):
    class Cp1252Sink:
        encoding = "cp1252"

        def __init__(self):
            self.parts = []

        def write(self, value):
            value.encode(self.encoding)
            self.parts.append(value)
            return len(value)

        def flush(self):
            return None

    sink = Cp1252Sink()
    monkeypatch.setattr(live_chapter_probe.sys, "stdout", sink)

    live_chapter_probe._print_json({"message": "中文"})

    assert "\\u4e2d\\u6587" in "".join(sink.parts)
