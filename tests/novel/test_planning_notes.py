# -*- coding: utf-8 -*-
"""规划阶段两项不花调用的检查：场景数在不在建议范围里、有没有登记表里没有的名字。
现在只写进运行记录、不打回；正式流程和规划检查脚本用同一套判断。"""
import contextlib
import io
import json
import logging
import os
import sys

import core.generation.scene_pipeline as scene_pipeline
from core.generation.lore_repository import CONTRACT, encode
from core.generation.planning_checks import LONG_FORM_SCENE_RANGE, NameFindings, name_note, scene_count_note
from core.generation.planning_contract import CONTRACT_END, CONTRACT_START
from core.generation.scene_pipeline import ScenePipeline
from core.generation.stage_context import make_context
from core.generation.stage_pipeline import GenerationHost, load_stage_parameters

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
import check_planning  # noqa: E402

PARAMETERS = "Genre: 悬疑\nStory Length: Novel (Standard)\nStory Structure: 6-Act Structure\nTheme: 真相\n"


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _registry(root):
    _write(root, CONTRACT, encode({
        "era": "当代",
        "canonical_characters": [{"id": "CH001", "name": "沈宁", "aliases": ["阿宁"]},
                                 {"id": "CH002", "name": "陈放", "aliases": []}],
        "canonical_locations": [{"id": "LOC001", "name": "白螺里", "aliases": []}],
        "schema_version": 4,
    }))


def _scenes(count, people="沈宁、陈放"):
    return "\n\n".join(
        f"### 场景 {index}：第 {index} 场\n* **环境与具体地点**：白螺里·三号楼。\n* **出场人物**：{people}。\n"
        f"沈宁在楼道里走第 {index} 段路。"
        for index in range(1, count + 1)
    )


def _reply(count, people="沈宁、陈放"):
    contract = {
        "chapter": 1,
        "chapter_function": "advance",
        "core_question": "谁寄的信",
        "scene_boundaries": [
            {"scene_number": index, "must_do": [f"走第 {index} 段路"], "must_not_do": [],
             "end_state": f"沈宁停在第 {index} 层"}
            for index in range(1, count + 1)
        ],
        "facts_added": [], "timeline_events": [], "plot_thread_updates": [],
    }
    return (f"{_scenes(count, people)}\n\n"
            f"{CONTRACT_START}\n{json.dumps(contract, ensure_ascii=False)}\n{CONTRACT_END}")


class _Records(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _logger(name):
    logger = logging.getLogger(name)
    records = _Records()
    logger.addHandler(records)
    logger.setLevel(logging.INFO)
    return logger, records


def test_the_two_notes_say_nothing_when_nothing_is_off():
    assert scene_count_note(2, LONG_FORM_SCENE_RANGE) == "" and scene_count_note(4, LONG_FORM_SCENE_RANGE) == ""
    assert scene_count_note(5, LONG_FORM_SCENE_RANGE) == "共 5 场，不在建议的 2–4 场之内"
    assert scene_count_note(1, (3, 6)) == "共 1 场，不在建议的 3–6 场之内"
    assert name_note(NameFindings()) == ""
    assert name_note(NameFindings(people=["陆巡"], places=["雾港"])) == "人名：陆巡；地名：雾港"


def test_an_accepted_plan_with_five_scenes_and_a_new_name_is_logged_not_sent_back(tmp_path, monkeypatch):
    _registry(tmp_path)
    sent = []
    monkeypatch.setattr(scene_pipeline, "send_prompt",
                        lambda prompt, **_: sent.append(prompt) or _reply(5, "沈宁、陆巡"))
    logger, records = _logger("test_planning_notes.accepted")
    host = GenerationHost(make_context(str(tmp_path), "fake", {}), logger)
    with contextlib.redirect_stdout(io.StringIO()):
        _response, markdown, contract = ScenePipeline(host)._generate_valid_scene_response(
            "请规划第 1 章。", "fake", 1, "当代港城。", {}, output_dir=str(tmp_path),
            scene_range=LONG_FORM_SCENE_RANGE,
        )
    assert len(sent) == 1                       # 没有打回
    assert len(contract["scene_boundaries"]) == 5
    assert "第 1 章场景规划共 5 场，不在建议的 2–4 场之内（只记录，不打回）" in records.lines
    assert "第 1 章场景规划里有登记表没有的名字：人名：陆巡（只记录，不打回）" in records.lines


def test_a_plan_inside_the_range_with_registered_names_logs_nothing(tmp_path, monkeypatch):
    _registry(tmp_path)
    monkeypatch.setattr(scene_pipeline, "send_prompt", lambda prompt, **_: _reply(3))
    logger, records = _logger("test_planning_notes.quiet")
    host = GenerationHost(make_context(str(tmp_path), "fake", {}), logger)
    with contextlib.redirect_stdout(io.StringIO()):
        ScenePipeline(host)._generate_valid_scene_response(
            "请规划第 1 章。", "fake", 1, "当代港城。", {}, output_dir=str(tmp_path),
            scene_range=LONG_FORM_SCENE_RANGE,
        )
    assert not [line for line in records.lines if "只记录，不打回" in line]


def test_the_check_counts_what_the_run_log_would_name(tmp_path):
    _write(tmp_path, "system/parameters.txt", PARAMETERS)
    _registry(tmp_path)
    _write(tmp_path, "story/structure/6-act_structure_beginning.md", "# 开端\n沈宁收到一封信。")
    _write(tmp_path, "story/planning/chapter_outlines/chapter_outlines_6-act_structure_beginning.md",
           "### 第 1 章：信\n* **涉及人物**：沈宁。\n\n### 第 2 章：楼道\n* **涉及人物**：陈放。\n")
    plans = "story/planning/detailed_scene_plans/scenes_6-act_structure_beginning_ch{}.md"
    _write(tmp_path, plans.format(1), _scenes(5, "沈宁、陆巡"))
    _write(tmp_path, plans.format(2), _scenes(3))
    with contextlib.redirect_stdout(io.StringIO()):
        text = check_planning.render(check_planning.collect(str(tmp_path)))
    assert "建议每章 2–4 场；不在范围里的 1 章（第 1 章 5 场）" in text
    assert "场景规划：人名 1 个（陆巡）" in text


def test_an_accepted_outline_with_a_new_name_is_logged(tmp_path, monkeypatch):
    _write(tmp_path, "system/parameters.txt", PARAMETERS + "Quality Loop: off\n")
    _registry(tmp_path)
    _write(tmp_path, "story/structure/6-act_structure_beginning.md", "# 开端\n沈宁收到一封信。")
    sent = []
    monkeypatch.setattr(scene_pipeline, "send_prompt",
                        lambda prompt, **_: sent.append(prompt) or
                        "### 第 1 章：信\n* **涉及人物**：沈宁、陆巡。\n* **具体地点**：白螺里·三号楼。\n")
    for name in ("show_error", "show_warning", "show_info"):
        monkeypatch.setattr(scene_pipeline, name, lambda *a, **k: None, raising=False)
    logger, records = _logger("test_planning_notes.outline")
    host = GenerationHost(make_context(str(tmp_path), "fake", load_stage_parameters(str(tmp_path))), logger)
    with contextlib.redirect_stdout(io.StringIO()):
        ScenePipeline(host)._generate_chapter_outline(host.context)
    assert len(sent) == 1
    assert "“开端”大纲里有登记表没有的名字：人名：陆巡（只记录，不打回）" in records.lines
    outline = tmp_path / "story/planning/chapter_outlines/chapter_outlines_6-act_structure_beginning.md"
    assert "陆巡" in outline.read_text(encoding="utf-8")   # 照旧保存，没有打回
