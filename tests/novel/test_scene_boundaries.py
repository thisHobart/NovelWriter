# -*- coding: utf-8 -*-
"""场景边界：规划时写清每一场停在哪里，写场景时认得旧作品的各种写法。"""
import contextlib
import io
import json
import logging

import core.generation.scene_pipeline as scene_pipeline
from core.generation.planning_contract import (
    CONTRACT_END,
    CONTRACT_START,
    boundary_scenes,
    contract_for_scene,
    contract_output_instructions,
    later_scene_boundaries,
    scene_boundary_defects,
)
from core.generation.scene_pipeline import ScenePipeline
from core.generation.stage_context import make_context
from core.generation.stage_pipeline import GenerationHost


def test_old_boundary_formats_are_all_read_without_touching_the_contract():
    """旧作品里出现过的写法都认得出场次；契约本身不改。"""
    cases = [
        [{"scene_index": 1, "location": "实验室"}, {"scene_index": 2, "location": "大楼"}],
        [{"scene": 1, "start": "潜入", "end": "挂断"}, {"scene": 2, "start": "调频", "end": "锁定"}],
        [{"scene_number": 1, "start_location": "酒馆"}, {"scene_number": "2", "end_location": "情报所"}],
        ["场景 1：毒雾", "场景 2：手术台"],
        ["scene_1: 暗巷", "scene_2: 冷库"],
        ["第1场 实验室", "第 2 场 通道"],
        [1, 2],
        ["汇州·码头", "临山·长廊"],            # 一条都没写场次：按先后当第 1、2 场
    ]
    for boundaries in cases:
        contract = {"chapter": 3, "scene_boundaries": boundaries}
        before = json.dumps(contract, ensure_ascii=False)
        assert boundary_scenes(boundaries) == [1, 2], boundaries
        later = later_scene_boundaries(contract, 1)
        assert len(later) == 1, boundaries
        assert contract_for_scene(contract, 1)["scene_boundaries"] == [boundaries[0]]
        assert later_scene_boundaries(contract, 2) == []
        assert json.dumps(contract, ensure_ascii=False) == before


def test_free_text_mentioning_a_scene_is_not_mistaken_for_a_marker():
    boundaries = ["scene_1: 他想起场景 3 的那扇门", "scene_2: 码头"]
    assert boundary_scenes(boundaries) == [1, 2]
    # 有的写了场次、有的没写时，没写的那条认不出，留在本场的契约里，不当成后面场次。
    assert boundary_scenes([{"scene_number": 2, "end_state": "门开了"}, "不要提前揭示凶手"]) == [2, None]


def test_the_planning_prompt_says_what_each_boundary_field_is_for():
    text = contract_output_instructions(2, {"open_plot_threads": []})
    assert '"scene_boundaries": []' not in text
    assert '"scene_number": 1' in text and '"end_state": "这一场停在哪里"' in text
    for field in ("must_do", "must_not_do", "end_state"):
        assert f"- {field}：" in text
    assert "只写中文" not in text


def test_every_scene_needs_a_boundary_with_an_end_state():
    good = {"scene_boundaries": [
        {"scene_number": 1, "must_do": ["开门"], "must_not_do": [], "end_state": "门开了"},
        {"scene_number": 2, "must_do": [], "must_not_do": ["揭示凶手"], "end_state": "灯灭了"},
    ]}
    assert scene_boundary_defects(good, 2) == []
    old = {"scene_boundaries": ["scene_1: 暗巷", {"scene": 2, "end": "锁定"}]}
    defects = scene_boundary_defects(old, 2)
    assert len(defects) == 1 and "第 1、2 场" in defects[0] and "end_state" in defects[0]
    blank = {"scene_boundaries": [{"scene_number": 1, "end_state": "门开了"},
                                  {"scene_number": 2, "end_state": "  "}]}
    assert "第 2 场" in scene_boundary_defects(blank, 2)[0]


def _reply(boundaries):
    contract = {
        "chapter": 1,
        "chapter_function": "advance",
        "core_question": "谁寄的信",
        "scene_boundaries": boundaries,
        "facts_added": [], "timeline_events": [], "plot_thread_updates": [],
    }
    return (
        "### 场景 1：信\n沈宁在楼梯上看见门口的信。\n\n"
        "### 场景 2：楼梯\n她没有拿信，下楼去找陈放。\n\n"
        f"{CONTRACT_START}\n{json.dumps(contract, ensure_ascii=False)}\n{CONTRACT_END}"
    )


def test_a_new_plan_without_end_states_is_sent_back_until_every_scene_has_one(tmp_path, monkeypatch):
    replies = [
        _reply(["scene_1: 楼梯", "scene_2: 楼下"]),          # 旧写法：没写停在哪里
        _reply([{"scene_number": 1, "must_do": ["看见信"], "must_not_do": ["拿信"],
                 "end_state": "沈宁盯着信没有伸手"},
                {"scene_number": 2, "must_do": ["找陈放"], "must_not_do": [],
                 "end_state": "陈放开了门"}]),
    ]
    sent = []

    def fake_send(prompt, **_kwargs):
        sent.append(prompt)
        return replies[len(sent) - 1]

    monkeypatch.setattr(scene_pipeline, "send_prompt", fake_send)
    host = GenerationHost(make_context(str(tmp_path), "fake", {}), logging.getLogger("test"))
    with contextlib.redirect_stdout(io.StringIO()):
        _response, _markdown, contract = ScenePipeline(host)._generate_valid_scene_response(
            "请规划第 1 章。", "fake", 1, "当代港城。", {}, output_dir=str(tmp_path)
        )
    assert len(sent) == 2
    assert "scene_boundaries 缺少第 1、2 场" in sent[1]
    assert [item["end_state"] for item in contract["scene_boundaries"]] == ["沈宁盯着信没有伸手", "陈放开了门"]
