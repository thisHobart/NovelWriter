import json
import re

from agents.review.domain_review_agent import DomainReviewAgent
from core.evaluation import live_story_quality
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


def test_real_three_chapter_runner_accepts_and_exports_ab(monkeypatch, tmp_path):
    monkeypatch.setattr(live_story_quality, "_reachable", lambda host, port: (True, ""))

    def fake_send(prompt, model=None):
        def invoke():
            if "中文类型小说盲读审稿人" in prompt:
                return _passing(DomainReviewAgent._BLIND_DIMENSIONS)
            if "现实合理性与专业机制审稿人" in prompt:
                return _passing(DomainReviewAgent._PLAUSIBILITY_DIMENSIONS)
            if "本次评审对象：chapter" in prompt:
                return _passing(
                    live_story_quality.get_domain_profile(
                        "legal_suspense"
                    ).score_dimensions
                )
            match = re.search(r"第\s*(\d+)\s*章，场景\s*(\d+)", prompt)
            assert match, prompt[:120]
            chapter, scene = map(int, match.groups())
            return (
                f"第{chapter}章第{scene}场。程砚按程序封存证物，只按记录能够证明的边界判断。"
                f"他核对时间与人物已知信息，没有提前泄露后续结论。"
            )

        return trace_model_call(
            prompt=prompt,
            backend="scripted-test-double",
            model=model or "test",
            invoke=invoke,
        )

    monkeypatch.setattr(live_story_quality, "send_prompt", fake_send)

    code, report = live_story_quality.run_live_story(tmp_path)

    assert code == 0
    assert report["mode"] == "real"
    assert report["attempted_model_calls"] == 15
    assert report["accepted_chapters"] == [1, 2, 3]
    assert report["ledger_revision"] == 3
    assert report["unresolved_conflicts"] == []
    assert all(report["isolation"].values())
    assert report["ab_blind_test"]["packets"] == 30
    assert report["ab_blind_test"]["response_rows"] == 90


def test_real_three_chapter_runner_stops_after_first_unimproved_failure(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(live_story_quality, "_reachable", lambda host, port: (True, ""))
    prose = "程砚越过证据边界，直接认定门禁卡证明了持卡人身份。"

    def fake_send(prompt, model=None):
        def invoke():
            if "中文类型小说盲读审稿人" in prompt:
                return _passing(DomainReviewAgent._BLIND_DIMENSIONS)
            if "现实合理性与专业机制审稿人" in prompt:
                return _passing(DomainReviewAgent._PLAUSIBILITY_DIMENSIONS)
            if "本次评审对象：chapter" in prompt:
                dimensions = live_story_quality.get_domain_profile(
                    "legal_suspense"
                ).score_dimensions
                return json.dumps(
                    {
                        "scores": {dimension: 4 for dimension in dimensions},
                        "hard_failures": [
                            {
                                "code": "TRUTH_CONTRADICTION",
                                "quote": "直接认定门禁卡证明了持卡人身份",
                                "problem": "把卡片事件误作身份结论，核心推理失真",
                                "change": "改为门禁记录只能证明卡片被读取",
                            }
                        ],
                        "evidence": [],
                        "upgrades": [],
                        "repair_scope": "scene_1",
                        "repair_instructions": ["校准门禁记录的证明边界"],
                        "strengths": [],
                    },
                    ensure_ascii=False,
                )
            return prose

        return trace_model_call(
            prompt=prompt,
            backend="scripted-test-double",
            model=model or "test",
            invoke=invoke,
        )

    monkeypatch.setattr(live_story_quality, "send_prompt", fake_send)

    code, report = live_story_quality.run_live_story(tmp_path)

    assert code == 1
    assert report["attempted_model_calls"] == 9
    assert report["accepted_chapters"] == []
    assert report["chapters"]["1"]["retry"]["improved"] is False
    assert "2" not in report["chapters"]
    assert report["ab_blind_test"] is None


def test_real_three_chapter_runner_resumes_cached_scenes_after_budget_stop(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(live_story_quality, "_reachable", lambda host, port: (True, ""))
    monkeypatch.setattr(live_story_quality, "MAX_MODEL_CALLS", 12)
    generation_count = 0

    def fake_send(prompt, model=None):
        def invoke():
            nonlocal generation_count
            if "中文类型小说盲读审稿人" in prompt:
                return _passing(DomainReviewAgent._BLIND_DIMENSIONS)
            if "现实合理性与专业机制审稿人" in prompt:
                return _passing(DomainReviewAgent._PLAUSIBILITY_DIMENSIONS)
            if "本次评审对象：chapter" in prompt:
                return _passing(
                    live_story_quality.get_domain_profile(
                        "legal_suspense"
                    ).score_dimensions
                )
            match = re.search(r"第\s*(\d+)\s*章，场景\s*(\d+)", prompt)
            assert match
            generation_count += 1
            chapter, scene = map(int, match.groups())
            return f"第{chapter}章第{scene}场真实正文。程砚只按证据边界判断。"

        return trace_model_call(
            prompt=prompt,
            backend="scripted-test-double",
            model=model or "test",
            invoke=invoke,
        )

    monkeypatch.setattr(live_story_quality, "send_prompt", fake_send)

    first_code, first = live_story_quality.run_live_story(tmp_path)
    assert first_code == 1
    assert first["accepted_chapters"] == [1, 2]
    assert first["attempted_model_calls"] == 12
    assert generation_count == 6

    second_code, second = live_story_quality.run_live_story(tmp_path)

    assert second_code == 0
    assert second["accepted_chapters"] == [1, 2, 3]
    assert second["attempted_model_calls"] == 15
    assert second["resumed_from_previous_failure"] is True
    assert second["chapters"]["3"]["scene_sources"] == [
        "cached_live_trace",
        "cached_live_trace",
    ]
    assert generation_count == 6
