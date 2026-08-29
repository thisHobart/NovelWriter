# -*- coding: utf-8 -*-
"""新界面的参数读写必须与 tkinter 版共用同一份磁盘格式。

这组用例不依赖 PySide6，可在没装 Qt 的环境里跑。
"""
import os

from ui import params_store, story_options


def _sample(output_dir):
    return {
        "output_directory": output_dir,
        "genre": "Sci-Fi",
        "subgenre": "Space Opera",
        "story_length": "Novel (Epic)",
        "story_structure": "Hero's Journey",
        "novel_title": "静默星环",
        "author_name": "kl",
        "theme": "记忆的代价",
        "tone": "冷峻、克制",
        "quality_loop": "standard",
        "backend": "api",
        "model": "claude-sonnet-4-6",
        "gender_generation_bias_string": "Balanced (50F/50M)",
        "protagonist_type": "Explorer",
        "hard_science": True,
    }


def test_round_trip_preserves_core_and_dynamic(tmp_path):
    output_dir = str(tmp_path)
    path = params_store.save(output_dir, _sample(output_dir))

    assert path == os.path.join(output_dir, "system", "parameters.txt")
    loaded = params_store.load(output_dir)
    assert loaded["Novel Title"] == "静默星环"
    assert loaded["Story Structure"] == "Hero's Journey"
    assert loaded["Protagonist Type"] == "Explorer"
    assert loaded["Hard Science"] == "True"


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


def test_structure_options_follow_length():
    assert "Hero's Journey" in story_options.structures_for("Novel (Epic)")
    assert story_options.default_structure_for("Short Story") == "3-Act Structure"
    assert story_options.subgenres_for("Sci-Fi")[0] == "Space Opera"
    assert story_options.subgenres_for("不存在的题材") == ()
