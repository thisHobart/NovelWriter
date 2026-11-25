# -*- coding: utf-8 -*-
"""界面参数读写必须保持既有工作区的磁盘格式。

这组用例不依赖 PySide6，可在没装 Qt 的环境里跑。
"""
import os

from core.gui.pages.novel import params_store, story_options


def _sample(output_dir):
    return {
        "output_directory": output_dir,
        "genre": "近未来科幻",
        "story_length": "Novel (Standard)",
        "story_structure": "Hero's Journey",
        "novel_title": "静默星环",
        "theme": "记忆的代价",
        "tone": "冷峻、克制",
        "quality_loop": "standard",
        "backend": "api",
        "model": "claude-sonnet-4-6",
        "num_factions": 6,
        "scene_dialogue": "off",
    }


def test_round_trip_preserves_core_and_extra_keys(tmp_path):
    output_dir = str(tmp_path)
    path = params_store.save(output_dir, _sample(output_dir))

    assert path == os.path.join(output_dir, "system", "parameters.txt")
    loaded = params_store.load(output_dir)
    assert loaded["Novel Title"] == "静默星环"
    assert loaded["Genre"] == "近未来科幻"
    assert loaded["Story Structure"] == "Hero's Journey"
    assert loaded["Num Factions"] == "6"
    assert loaded["Scene Dialogue"] == "off"


def test_core_keys_keep_legacy_order(tmp_path):
    output_dir = str(tmp_path)
    params_store.save(output_dir, _sample(output_dir))
    with open(params_store.parameters_path(output_dir), encoding="utf-8") as handle:
        keys = [line.split(":", 1)[0] for line in handle if ":" in line]
    core = [k for k in keys if k in params_store.CORE_ORDER]
    assert core == [k for k in params_store.CORE_ORDER if k in core]


def test_windows_path_value_survives_colon_split(tmp_path):
    output_dir = str(tmp_path)
    params = _sample(output_dir)
    params["output_directory"] = r"D:\work\novel"
    params_store.save(output_dir, params)
    # 保存目录仍是 tmp_path，但写入的值必须完整保留盘符里的冒号
    loaded = params_store.load(output_dir)
    assert loaded["Output Directory"] == r"D:\work\novel"


def test_missing_file_returns_empty(tmp_path):
    assert params_store.load(str(tmp_path / "nope")) == {}


def test_length_decides_structure():
    assert story_options.LENGTH_OPTIONS == ["Short Story", "Novella", "Novel (Standard)"]
    assert story_options.default_structure_for("Short Story") == "3-Act Structure"
    assert story_options.default_structure_for("Novella") == "3-Act Structure"
    assert story_options.default_structure_for("Novel (Standard)") == "6-Act Structure"


def test_zero_false_and_aliases_are_saved_once(tmp_path):
    params_store.save(str(tmp_path), {"Theme": "历史值", "theme": "中文新值", "custom_flag": False, "custom_zero": 0})
    loaded = params_store.load(str(tmp_path))
    assert loaded["Theme"] == "中文新值" and loaded["Custom Flag"] == "False" and loaded["Custom Zero"] == "0"
    assert (tmp_path / "system/parameters.txt").read_text(encoding="utf-8").count("Theme:") == 1


def test_replace_failure_preserves_saved_file(tmp_path, monkeypatch):
    import pytest
    path = params_store.save(str(tmp_path), {"theme": "有效旧参数"})
    monkeypatch.setattr(params_store.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("替换失败")))
    with pytest.raises(OSError):
        params_store.save(str(tmp_path), {"theme": "不能部分覆盖"})
    assert params_store.load(str(tmp_path))["Theme"] == "有效旧参数"
    assert not list((tmp_path / "system").glob(".parameters_*.tmp"))
