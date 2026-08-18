"""Tests for keyword-based domain profile dispatch."""

import pytest

from dataclasses import replace as replace_fields

from core.generation.domain_profiles import (
    DOMAIN_PROFILES,
    GENERAL,
    LEGAL_SUSPENSE,
    apply_quality_loop_mode,
    get_domain_profile,
    resolve_domain_profile,
    resolve_quality_loop_mode,
)


def replace_pass_average(profile, value):
    return replace_fields(profile, pass_average=value)


# 改造前 agents/review/legal_suspense_review_agent.py 里的硬编码值。
# 法律悬疑档案必须与之逐项相等，既有项目的评审数据才可比。
PRE_REFACTOR_LEGAL_DIMENSIONS = (
    "focus_depth",
    "concrete_detail",
    "information_gap",
    "fair_play",
    "reversal",
    "legal_realism",
    "moral_gray",
    "attack_defense",
    "personal_cost",
    "continuity",
    "chinese_prose",
)

PRE_REFACTOR_LEGAL_HARD_FAILURES = {
    "TRUTH_CONTRADICTION",
    "UNSEEDED_SOLUTION",
    "LEGAL_IMPOSSIBILITY",
    "CONTINUITY_DUPLICATION",
    "KNOWLEDGE_LEAK",
    "NEXT_SCENE_PREMATURE",
    "TYPE_CONFLICT",
}


def test_legal_profile_matches_pre_refactor_constants():
    assert LEGAL_SUSPENSE.score_dimensions == PRE_REFACTOR_LEGAL_DIMENSIONS
    assert set(LEGAL_SUSPENSE.hard_failure_codes) == PRE_REFACTOR_LEGAL_HARD_FAILURES
    assert LEGAL_SUSPENSE.pass_average == 3.2
    assert LEGAL_SUSPENSE.required_dimensions == ("fair_play", "legal_realism", "continuity")
    assert LEGAL_SUSPENSE.max_plan_retries == 2
    assert LEGAL_SUSPENSE.max_scene_retries == 2


# core/gui/parameters.py 的 populate_subgenres() 全量子题材，逐项断言分派结果。
SUBGENRE_MATRIX = [
    ("Sci-Fi", "Space Opera", "scifi"),
    ("Sci-Fi", "Hard Sci-Fi", "scifi"),
    ("Sci-Fi", "Cyberpunk", "scifi"),
    ("Sci-Fi", "Time Travel", "scifi"),
    ("Sci-Fi", "Post-Apocalyptic", "scifi"),
    ("Sci-Fi", "Biopunk", "scifi"),
    ("Fantasy", "High Fantasy", "fantasy"),
    ("Fantasy", "Dark Fantasy", "fantasy"),
    ("Fantasy", "Urban Fantasy", "fantasy"),
    ("Fantasy", "Sword and Sorcery", "fantasy"),
    ("Fantasy", "Mythic Fantasy", "fantasy"),
    ("Fantasy", "Fairy Tale", "fantasy"),
    ("Horror", "Gothic Horror", "horror"),
    ("Horror", "Psychological Horror", "horror"),
    ("Horror", "Supernatural Horror", "horror"),
    ("Horror", "Body Horror", "horror"),
    ("Horror", "Cosmic Horror", "horror"),
    ("Horror", "Slasher", "horror"),
    ("Mystery", "Cozy Mystery", "detective_mystery"),
    ("Mystery", "Hard-boiled Detective", "detective_mystery"),
    ("Mystery", "Police Procedural", "detective_mystery"),
    ("Mystery", "Amateur Sleuth", "detective_mystery"),
    ("Mystery", "Legal Thriller", "legal_suspense"),
    ("Mystery", "Forensic Mystery", "detective_mystery"),
    ("Romance", "Contemporary Romance", "romance"),
    ("Romance", "Historical Romance", "romance"),
    ("Romance", "Paranormal Romance", "romance"),
    ("Romance", "Romantic Suspense", "romance"),
    ("Romance", "Regency Romance", "romance"),
    ("Romance", "Western Romance", "romance"),
    ("Thriller", "Espionage Thriller", "thriller"),
    ("Thriller", "Psychological Thriller", "thriller"),
    ("Thriller", "Action Thriller", "thriller"),
    ("Thriller", "Techno-Thriller", "thriller"),
    ("Thriller", "Medical Thriller", "thriller"),
    ("Thriller", "Legal Thriller", "legal_suspense"),
    ("Western", "Traditional Western", "western"),
    ("Western", "Weird Western", "western"),
    ("Western", "Space Western", "western"),
    ("Western", "Modern Western", "western"),
    ("Western", "Outlaw Western", "western"),
    ("Western", "Cattle Drive Western", "western"),
    ("Historical Fiction", "Ancient History", "historical"),
    ("Historical Fiction", "Medieval", "historical"),
    ("Historical Fiction", "Renaissance", "historical"),
    ("Historical Fiction", "Colonial America", "historical"),
    ("Historical Fiction", "Civil War Era", "historical"),
    ("Historical Fiction", "World War Era", "historical"),
]


@pytest.mark.parametrize("genre,subgenre,expected", SUBGENRE_MATRIX)
def test_every_gui_subgenre_resolves_to_a_profile(genre, subgenre, expected):
    profile = resolve_domain_profile({"Genre": genre, "Subgenre": subgenre})
    assert profile.key == expected


@pytest.mark.parametrize(
    "genre,expected",
    [
        ("Sci-Fi", "scifi"),
        ("Fantasy", "fantasy"),
        ("Horror", "horror"),
        ("Mystery", "detective_mystery"),
        ("Romance", "romance"),
        ("Thriller", "thriller"),
        ("Western", "western"),
        ("Historical Fiction", "historical"),
    ],
)
def test_genre_alone_resolves_when_subgenre_is_missing(genre, expected):
    assert resolve_domain_profile({"Genre": genre}).key == expected


def test_subgenre_wins_over_genre():
    # "Legal Thriller" 挂在两个不同主类型下，都必须落到法律悬疑。
    assert resolve_domain_profile(
        {"Genre": "Thriller", "Subgenre": "Legal Thriller"}
    ).key == "legal_suspense"
    assert resolve_domain_profile(
        {"Genre": "Mystery", "Subgenre": "Legal Thriller"}
    ).key == "legal_suspense"


def test_chinese_keywords_resolve():
    assert resolve_domain_profile({"Genre": "悬疑推理", "Subgenre": "法律悬疑"}).key == "legal_suspense"
    assert resolve_domain_profile({"Genre": "科幻", "Subgenre": "赛博朋克"}).key == "scifi"
    assert resolve_domain_profile({"Genre": "恐怖"}).key == "horror"


def test_theme_and_tone_are_the_last_keyword_source():
    profile = resolve_domain_profile(
        {"Genre": "", "Subgenre": "", "Theme": "一场关于魔法代价的故事", "Tone": "冷峻"}
    )
    assert profile.key == "fantasy"


def test_explicit_override_beats_keywords():
    profile = resolve_domain_profile(
        {"Genre": "Romance", "Subgenre": "Regency Romance", "Domain Profile": "thriller"}
    )
    assert profile.key == "thriller"


def test_unknown_and_empty_fall_back_to_general():
    assert resolve_domain_profile({}).key == GENERAL.key
    assert resolve_domain_profile(None).key == GENERAL.key
    assert resolve_domain_profile({"Genre": "Cooking Manual"}).key == GENERAL.key
    assert get_domain_profile("no-such-profile").key == GENERAL.key
    # 无效的 override 不应吞掉关键词分派。
    assert resolve_domain_profile(
        {"Genre": "Horror", "Domain Profile": "no-such-profile"}
    ).key == "horror"


@pytest.mark.parametrize("key,profile", sorted(DOMAIN_PROFILES.items()))
def test_every_profile_is_internally_consistent(key, profile):
    assert profile.key == key
    # 评分维度不得重复，且必要维度必须在维度表内，否则门槛永远取不到分数。
    assert len(set(profile.score_dimensions)) == len(profile.score_dimensions)
    assert set(profile.required_dimensions) <= set(profile.score_dimensions)
    assert 0 < profile.pass_average <= 4
    # 通用维度必须在每个档案里都出现，跨题材的连续性和文风才有可比性。
    assert {"focus_depth", "continuity", "chinese_prose"} <= set(profile.score_dimensions)
    # 每个档案的记录流最多各绑一个稳定 id 容器所需的不可变字段集合。
    for slot in ("clue_updates", "evidence_updates"):
        for spec in profile.fields_for_slot(slot):
            assert spec.is_list, f"{spec.name} 汇入 {slot} 时必须是列表字段"
    assert profile.review_focus.keys() >= {"plan", "scene", "chapter"}


# --- 质量闭环档位 ---------------------------------------------------------


@pytest.mark.parametrize(
    "parameters,expected",
    [
        ({}, "standard"),
        (None, "standard"),
        ({"Quality Loop": "off"}, "off"),
        ({"Quality Loop": "关闭"}, "off"),
        ({"Quality Loop": "标准"}, "standard"),
        ({"Quality Loop": "严格"}, "strict"),
        ({"quality_loop": "strict"}, "strict"),
        ({"Quality Loop": "STRICT"}, "strict"),
        ({"Quality Loop": ""}, "standard"),
        ({"Quality Loop": "无此档位"}, "standard"),
    ],
)
def test_quality_loop_mode_accepts_gui_and_file_spellings(parameters, expected):
    assert resolve_quality_loop_mode(parameters) == expected


def test_parameters_file_round_trip(tmp_path):
    # save_parameters() 写出的是 "Quality Loop: strict" 这种行；两条写作路径都按
    # 同样的方式解析参数文件，所以档位必须能原样读回。
    params_file = tmp_path / "parameters.txt"
    params_file.write_text(
        "Genre: Horror\nSubgenre: Cosmic Horror\nQuality Loop: strict\n", encoding="utf-8"
    )
    parsed = {}
    for line in params_file.read_text(encoding="utf-8").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            parsed[key.strip()] = value.strip()

    assert resolve_quality_loop_mode(parsed) == "strict"
    assert resolve_domain_profile(parsed).key == "horror"


def test_strict_mode_tightens_and_off_mode_leaves_thresholds_alone():
    strict = apply_quality_loop_mode(LEGAL_SUSPENSE, "strict")
    assert strict.pass_average == pytest.approx(LEGAL_SUSPENSE.pass_average + 0.2)
    assert strict.max_scene_retries >= 2
    assert strict.key == LEGAL_SUSPENSE.key
    assert strict.score_dimensions == LEGAL_SUSPENSE.score_dimensions

    # 关闭档在循环里直接跳过评审，不应改门槛。
    assert apply_quality_loop_mode(LEGAL_SUSPENSE, "off") is LEGAL_SUSPENSE
    assert apply_quality_loop_mode(LEGAL_SUSPENSE, "standard") is LEGAL_SUSPENSE


def test_strict_mode_never_exceeds_the_score_ceiling():
    ceiling = replace_pass_average(GENERAL, 3.95)
    assert apply_quality_loop_mode(ceiling, "strict").pass_average <= 4.0
