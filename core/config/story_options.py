# -*- coding: utf-8 -*-
"""作品参数的选项表——与界面框架无关的唯一来源。

智能体（`agents/writing/chapter_writing_agent.py`、编排器）和 Qt 界面都需要这些
选项，因此统一放在配置层，避免生成层依赖任何 GUI 框架。

只放数据与纯查询函数，不做任何 I/O。
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

# 每种故事结构的分段名。故事结构阶段（improve_structure）与场景规划阶段
# （generate_chapter_outline）都按这张表切分幕。
STRUCTURE_SECTIONS_MAP = {
    "3-Act Structure": ("Act 1: Setup", "Act 2: Confrontation", "Act 3: Resolution"),
    "6-Act Structure": ("Beginning", "Rising Action", "First Climax", "Solution Finding", "Second Climax", "Resolution"),
    "Fichtean Curve": ("Inciting Incident", "Rising Action", "Climax", "Falling Action", "Denouement"),
    "Seven-Point Structure": ("Hook", "Plot Point 1", "Pinch Point 1", "Midpoint", "Pinch Point 2", "Plot Point 2", "Resolution"),
    "Hero's Journey": (
        "The Ordinary World", "The Call to Adventure", "Refusal of the Call", "Meeting the Mentor",
        "Crossing the Threshold", "Tests, Allies, and Enemies", "Approach to the Inmost Cave",
        "The Ordeal", "Reward (Seizing the Sword)", "The Road Back", "The Resurrection", "Return with the Elixir"
    ),
    "Hero's Journey (Simplified)": ("Departure", "Initiation", "Return"),
    "Save the Cat!": (
        "Opening Image", "Theme Stated", "Set-up", "Catalyst", "Debate", "Break into Two", "B Story",
        "Fun and Games", "Midpoint", "Bad Guys Close In", "All Is Lost", "Dark Night of the Soul",
        "Break into Three", "Finale", "Final Image"
    ),
    "Episodic Structure": (
        "Episode 1: Introduction", "Episode 2: Rising Action", "Episode 3: Midpoint/Turning Point",
        "Episode 4: Climax Actions", "Episode 5: Resolution/Lead to Next"
    ),  # 5 集为例；num_episodes 改成用户可填时这里要跟着动态化
}

# (女性 %, 男性 %)，顺序与界面下拉里的 F/M 一致。
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


def supported_genres() -> tuple[str, ...]:
    """支持的题材列表。

    这份表曾经以 Generators 里注册的处理器为准、本地表只作兜底。那批处理器随
    英文随机词库一起删除之后，本地表就是唯一来源——两边的内容与顺序当时是一致的。
    """
    return DEFAULT_GENRES


def subgenres_for(genre: str) -> tuple[str, ...]:
    return SUBGENRES.get(genre, ())


def structures_for(length: str) -> list[str]:
    return list(STRUCTURE_MAP.get(length, []))


def default_structure_for(length: str) -> str:
    options = structures_for(length)
    fallback = options[0] if options else ""
    return DEFAULT_STRUCTURE.get(length, fallback)


def sections_for(structure: str) -> tuple[str, ...]:
    return STRUCTURE_SECTIONS_MAP.get(structure, ())
