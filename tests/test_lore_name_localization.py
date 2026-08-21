"""The lore stage must hand downstream stages a Chinese cast.

These run the real handlers end to end.  The bug they exist to catch was a
NameError that only surfaced when the button was pressed: importing the module
and compiling it both succeeded, so nothing but execution could find it.
"""

import json

import pytest

from core.gui import lore as lore_module
from core.gui.lore import Lore
from core.generation.chinese_names import has_latin


class _Logger:
    def info(self, *args, **kwargs):
        pass

    debug = warning = info

    def error(self, *args, **kwargs):
        raise AssertionError(f"lore stage logged an error: {args}")


class _App:
    def __init__(self, output_dir):
        self.output_dir = str(output_dir)
        self.logger = _Logger()


class _UI(dict):
    def __init__(self, output_dir, **values):
        super().__init__(values)
        self.output_dir = str(output_dir)
        self.parameters = {
            "genre": "Mystery",
            "subgenre": "Legal Thriller",
            "female_percentage": 50,
            "male_percentage": 50,
        }


@pytest.fixture
def planner(tmp_path, monkeypatch):
    """A Lore panel wired to a temp project, with dialogs turned into failures."""
    from core.config.directory_config import get_directory_manager

    panel = object.__new__(Lore)
    panel.app = _App(tmp_path)
    panel.dir_manager = get_directory_manager(str(tmp_path), use_new_structure=True)

    def fail(title, message):
        raise AssertionError(f"{title}: {message}")

    monkeypatch.setattr(lore_module, "show_error", fail)
    monkeypatch.setattr(lore_module, "show_warning", fail, raising=False)
    return panel


def _names(payload):
    """Every name-ish string in a saved lore file."""
    found = []
    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"name", "faction_name", "territory", "headquarters"} and isinstance(value, str):
                    found.append(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(payload)
    return found


def test_generating_factions_saves_chinese_names(planner, tmp_path):
    planner._generate_factions(_UI(tmp_path, num_factions=2))

    saved = json.loads((tmp_path / "story" / "lore" / "factions.json").read_text("utf-8"))
    names = _names(saved)

    assert names, "no faction names were saved"
    assert not [name for name in names if has_latin(name)], names


def test_generating_characters_saves_chinese_names(planner, tmp_path):
    planner._generate_characters(_UI(tmp_path, num_chars=3))

    saved = json.loads((tmp_path / "story" / "lore" / "characters.json").read_text("utf-8"))
    names = _names(saved)

    assert names, "no character names were saved"
    assert not [name for name in names if has_latin(name)], names


def test_the_two_stages_share_one_name_mapping(planner, tmp_path):
    planner._generate_factions(_UI(tmp_path, num_factions=2))
    planner._generate_characters(_UI(tmp_path, num_chars=3))

    mapping = json.loads(
        (tmp_path / "story" / "lore" / "name_mapping.json").read_text("utf-8")
    )["names"]
    chinese = list(mapping.values())

    # A shared mapping is only useful if it never hands out the same name twice.
    assert len(chinese) == len(set(chinese))
    assert len(mapping) >= 5
