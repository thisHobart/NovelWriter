# -*- coding: utf-8 -*-
"""产物读取层的用例。不依赖 PySide6，可在没装 Qt 的环境里跑。"""
import json
import os

from ui.services import artifacts


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def test_missing_project_reads_as_empty(tmp_path):
    root = str(tmp_path / "nothing")
    assert artifacts.factions(root) == []
    assert artifacts.characters(root) == []
    assert artifacts.chapters(root) == []
    assert artifacts.lore_text(root) == ""
    assert artifacts.stage_file_counts(root)["lore"] == 0


def test_factions_tolerate_both_shapes(tmp_path):
    root = str(tmp_path)
    _write(os.path.join(root, artifacts.LORE_DIR, "factions.json"),
           json.dumps([{"name": "复核院", "description": "掌握全部神经档案的调阅权。"}],
                      ensure_ascii=False))
    entities = artifacts.factions(root)
    assert len(entities) == 1
    assert entities[0].name == "复核院"
    assert "神经档案" in entities[0].summary

    # 包一层字典也要能读出来
    _write(os.path.join(root, artifacts.LORE_DIR, "factions.json"),
           json.dumps({"factions": [{"faction_name": "环带联邦"}]}, ensure_ascii=False))
    assert artifacts.factions(root)[0].name == "环带联邦"


def test_broken_json_does_not_raise(tmp_path):
    root = str(tmp_path)
    _write(os.path.join(root, artifacts.LORE_DIR, "characters.json"), "{ 这不是 json")
    assert artifacts.characters(root) == []


def test_chapters_sort_numerically_not_lexically(tmp_path):
    root = str(tmp_path)
    for number in (1, 2, 10, 11):
        _write(os.path.join(root, artifacts.CHAPTERS_DIR, f"chapter_{number}.md"),
               f"第 {number} 章正文" * 3)
    numbers = [c.number for c in artifacts.chapters(root)]
    assert numbers == [1, 2, 10, 11]


def test_word_count_counts_cjk_by_character(tmp_path):
    assert artifacts.word_count("你好世界") == 4
    assert artifacts.word_count("hello world") == 2
    assert artifacts.word_count("你好 hello") == 2 + 1


def test_structure_sections_pretty_titles(tmp_path):
    root = str(tmp_path)
    _write(os.path.join(root, artifacts.STRUCTURE_DIR,
                        "6-act_structure_rising_action.md"), "上升动作")
    section = artifacts.structure_sections(root)[0]
    assert section.title == "Rising Action"
    assert section.text == "上升动作"
