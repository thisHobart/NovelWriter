# -*- coding: utf-8 -*-
"""与界面无关的作品参数选项表。

这些常量目前在 core/gui/parameters.py（tkinter 版）里也有一份。那个模块在
导入时会拉起 tkinter 与 ai_helper，界面层不应为了取几个列表而付这个代价，
因此这里优先从旧模块导入、失败时回落到本地副本。

TODO：tkinter 版退场后，把这份表迁到 core/config/ 下作为唯一来源。
"""
from __future__ import annotations

LENGTH_OPTIONS = ["Short Story", "Novella", "Novel (Standard)", "Novel (Epic)"]

STRUCTURE_MAP = {
    "Short Story": ["3-Act Structure", "Fichtean Curve", "Freytag's Pyramid"],
    "Novella": ["3-Act Structure", "Seven-Point Structure", "Hero's Journey (Simplified)"],
    "Novel (Standard)": ["3-Act Structure", "6-Act Structure", "Save the Cat!", "Hero's Journey"],
    "Novel (Epic)": ["6-Act Structure", "Hero's Journey", "Save the Cat!", "Episodic Structure"],
}

DEFAULT_STRUCTURE = {
    "Short Story": "3-Act Structure",
    "Novella": "3-Act Structure",
    "Novel (Standard)": "6-Act Structure",
    "Novel (Epic)": "6-Act Structure",
}

GENDER_BIAS_MAP = {
    "Balanced (50F/50M)": (50, 50),
    "Slightly Female (60F/40M)": (60, 40),
    "Mostly Female (75F/25M)": (75, 25),
    "Primarily Female (90F/10M)": (90, 10),
    "Slightly Male (40F/60M)": (40, 60),
    "Mostly Male (25F/75M)": (25, 75),
    "Primarily Male (10F/90M)": (10, 90),
    "Exclusively Female (100F/0M)": (100, 0),
    "Exclusively Male (0F/100M)": (0, 100),
}

# populate_subgenres() 里那串 if/elif 的数据化版本
SUBGENRES = {
    "Sci-Fi": ("Space Opera", "Hard Sci-Fi", "Cyberpunk", "Time Travel",
               "Post-Apocalyptic", "Biopunk"),
    "Fantasy": ("High Fantasy", "Dark Fantasy", "Urban Fantasy",
                "Sword and Sorcery", "Mythic Fantasy", "Fairy Tale"),
    "Horror": ("Gothic Horror", "Psychological Horror", "Supernatural Horror",
               "Body Horror", "Cosmic Horror", "Slasher"),
    "Mystery": ("Cozy Mystery", "Hard-boiled Detective", "Police Procedural",
                "Amateur Sleuth", "Legal Thriller", "Forensic Mystery"),
    "Romance": ("Contemporary Romance", "Historical Romance", "Paranormal Romance",
                "Romantic Suspense", "Regency Romance", "Western Romance"),
    "Thriller": ("Espionage Thriller", "Psychological Thriller", "Action Thriller",
                 "Techno-Thriller", "Medical Thriller", "Legal Thriller"),
    "Western": ("Traditional Western", "Weird Western", "Space Western",
                "Modern Western", "Outlaw Western", "Cattle Drive Western"),
    "Historical Fiction": ("Ancient History", "Medieval", "Renaissance",
                           "Colonial America", "Civil War Era", "World War Era"),
}

DEFAULT_GENRES = tuple(SUBGENRES.keys())

# --- 尽量以旧模块为准 -------------------------------------------------------
try:  # pragma: no cover - 取决于运行环境是否装了 llm-backends
    from core.gui.parameters import (  # type: ignore
        DEFAULT_STRUCTURE as _DEFAULT_STRUCTURE,
        GENDER_BIAS_MAP as _GENDER_BIAS_MAP,
        LENGTH_OPTIONS as _LENGTH_OPTIONS,
        STRUCTURE_MAP as _STRUCTURE_MAP,
    )
    LENGTH_OPTIONS = list(_LENGTH_OPTIONS)
    STRUCTURE_MAP = dict(_STRUCTURE_MAP)
    DEFAULT_STRUCTURE = dict(_DEFAULT_STRUCTURE)
    GENDER_BIAS_MAP = dict(_GENDER_BIAS_MAP)
except Exception:  # noqa: BLE001 - 缺依赖时用本地副本，界面仍可运行
    pass


def supported_genres() -> tuple[str, ...]:
    try:
        from Generators.GenreHandlers import get_supported_genres  # type: ignore

        genres = tuple(get_supported_genres())
        if genres:
            return genres
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_GENRES


def subgenres_for(genre: str) -> tuple[str, ...]:
    return SUBGENRES.get(genre, ())


def structures_for(length: str) -> list[str]:
    return list(STRUCTURE_MAP.get(length, []))


def default_structure_for(length: str) -> str:
    options = structures_for(length)
    fallback = options[0] if options else ""
    return DEFAULT_STRUCTURE.get(length, fallback)
