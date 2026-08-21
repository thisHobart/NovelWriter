"""The cast must be Chinese, and must stay the same Chinese across reruns."""

import json
import re

from core.generation.chinese_names import (
    ChineseNamer,
    has_latin,
    load_namer,
    localize_characters,
    localize_factions,
    save_namer,
)


class _Character:
    """Stands in for the genre generators' character objects (not dicts)."""

    def __init__(self, name, gender, spouse_name=None, siblings=None):
        self.name = name
        self.gender = gender
        self.spouse_name = spouse_name
        self.siblings = siblings or []


def _is_chinese(text):
    return bool(re.fullmatch(r"[一-鿿]+", text))


def test_generated_person_names_are_chinese_and_plausible():
    namer = ChineseNamer("project-a")

    names = [namer.person(source, "Male") for source in ("Gary White", "Eric Snow", "Kevin Knight")]

    assert all(_is_chinese(name) for name in names)
    assert all(2 <= len(name) <= 3 for name in names)
    assert len(set(names)) == 3


def test_the_same_source_always_maps_to_the_same_name():
    namer = ChineseNamer("project-a")

    first = namer.person("Michelle Lee", "Female")

    assert namer.person("Michelle Lee", "Female") == first
    assert ChineseNamer("project-a").person("Michelle Lee", "Female") == first


def test_different_projects_get_different_casts():
    a = ChineseNamer("project-a").person("Michelle Lee", "Female")
    b = ChineseNamer("project-b").person("Michelle Lee", "Female")

    assert a != b


def test_characters_and_their_relatives_are_all_renamed():
    namer = ChineseNamer("project-a")
    people = [
        _Character(
            "Gary White",
            "Male",
            spouse_name="Michelle Lee",
            siblings=[{"name": "Frances Jones", "gender": "Female"}],
        )
    ]

    localize_characters(people, namer)

    assert _is_chinese(people[0].name)
    assert _is_chinese(people[0].spouse_name)
    assert _is_chinese(people[0].siblings[0]["name"])


def test_organizations_are_named_by_what_they_are():
    namer = ChineseNamer("project-a")

    police = namer.organization("Violent Crime Division", "临江")
    lab = namer.organization("State Forensic Laboratory", "临江")
    prosecutors = namer.organization("Metro District Attorney's Office", "临江")

    assert "公安" in police or "刑事" in police or "重案" in police
    assert "鉴定" in lab or "法医" in lab
    assert "检察" in prosecutors


def test_factions_have_both_their_name_and_their_territory_localized():
    namer = ChineseNamer("project-a")
    factions = [{"name": "Silent Crime Organization", "territory": "Mist Bay"}]

    localize_factions(factions, namer, city="临江")

    assert not has_latin(factions[0]["name"])
    assert not has_latin(factions[0]["territory"])


def test_rewrite_replaces_the_longest_name_first():
    namer = ChineseNamer("project-a")
    short = namer.person("Gary", "Male")
    full = namer.person("Gary White", "Male")

    rewritten = namer.rewrite("Gary White 走进来，Gary 没有回头。")

    assert full in rewritten
    assert short in rewritten
    assert "White" not in rewritten


def test_the_mapping_survives_a_separate_run(tmp_path):
    """Characters and factions are separate buttons; either may be rerun alone."""
    first = load_namer(str(tmp_path))
    detective = first.person("Michelle Lee", "Female")
    save_namer(first, str(tmp_path))

    second = load_namer(str(tmp_path))

    assert second.person("Michelle Lee", "Female") == detective
    stored = json.loads((tmp_path / "story" / "lore" / "name_mapping.json").read_text("utf-8"))
    assert stored["names"]["Michelle Lee"] == detective


def test_a_reloaded_namer_does_not_reissue_a_taken_name(tmp_path):
    first = load_namer(str(tmp_path))
    taken = first.person("Michelle Lee", "Female")
    save_namer(first, str(tmp_path))

    second = load_namer(str(tmp_path))
    fresh = [second.person(f"Person {index}", "Female") for index in range(20)]

    assert taken not in fresh


def test_descriptive_lists_are_never_mistaken_for_names():
    """goals/flaws hold English sentences; renaming them guts the character."""
    namer = ChineseNamer("project-a")
    people = [
        {
            "name": "Linda Storm",
            "gender": "Female",
            "goals": ["Protect their family from retaliation"],
            "flaws": ["Overly suspicious of everyone"],
            "background": "Has a background in psychology",
        }
    ]

    localize_characters(people, namer)

    assert people[0]["goals"] == ["Protect their family from retaliation"]
    assert people[0]["flaws"] == ["Overly suspicious of everyone"]
    assert people[0]["background"] == "Has a background in psychology"
    assert _is_chinese(people[0]["name"])


def test_nested_family_trees_are_fully_renamed():
    namer = ChineseNamer("project-a")
    people = [
        {
            "name": "Linda Storm",
            "gender": "Female",
            "family": {
                "parents": [
                    {"name": "Gary Storm", "relation": "Father", "gender": "Male"},
                    {"name": "Kim Storm", "relation": "Mother", "gender": "Female"},
                ],
                "children": [{"name": "Eric Storm", "relation": "child", "gender": "Male"}],
            },
        }
    ]

    localize_characters(people, namer)

    family = people[0]["family"]
    everyone = [people[0]["name"]] + [p["name"] for p in family["parents"] + family["children"]]
    assert all(_is_chinese(name) for name in everyone), everyone


def test_blood_relatives_share_a_surname_and_mothers_keep_their_own():
    namer = ChineseNamer("project-a")
    people = [
        {
            "name": "Linda Storm",
            "gender": "Female",
            "family": {
                "parents": [
                    {"name": "Gary Storm", "relation": "Father", "gender": "Male"},
                    {"name": "Kim Storm", "relation": "Mother", "gender": "Female"},
                ],
                "siblings": [{"name": "Dan Storm", "relation": "sibling", "gender": "Male"}],
            },
        }
    ]

    localize_characters(people, namer)

    family = people[0]["family"]
    surname = people[0]["name"][0]
    father = next(p for p in family["parents"] if p["relation"] == "Father")
    mother = next(p for p in family["parents"] if p["relation"] == "Mother")

    assert father["name"][0] == surname
    assert family["siblings"][0]["name"][0] == surname
    assert mother["name"][0] != surname


def test_a_relative_never_ends_up_with_the_characters_own_name():
    """Some generators hand a character and their father the identical name."""
    namer = ChineseNamer("project-a")
    people = [
        {
            "name": "Jonathan White",
            "gender": "Male",
            "family": {
                "parents": [
                    {"name": "Jonathan White", "relation": "Father", "gender": "Male"}
                ]
            },
        }
    ]

    localize_characters(people, namer)

    assert people[0]["name"] != people[0]["family"]["parents"][0]["name"]
