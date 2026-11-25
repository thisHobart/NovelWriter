# -*- coding: utf-8 -*-
"""作品参数的选项表——与界面框架无关的唯一来源。

智能体（`agents/writing/chapter_writing_agent.py`）和 Qt 界面都需要这些选项，
因此统一放在配置层，避免生成层依赖任何 GUI 框架。

只放数据与纯查询函数，不做任何 I/O。
"""
from __future__ import annotations

LENGTH_OPTIONS = ["Short Story", "Novella", "Novel (Standard)"]

# 故事结构不再由作者选择：新作品按篇幅固定结构。已有作品沿用参数文件里保存的结构。
DEFAULT_STRUCTURE = {
    "Short Story": "3-Act Structure",
    "Novella": "3-Act Structure",
    "Novel (Standard)": "6-Act Structure",
}

# 题材、主题、基调都是可改写的下拉框：下面只是预设选项，作者可以直接写成别的。
GENRE_OPTIONS = ["悬疑推理", "恐怖", "科幻", "爱情"]

# 主题、基调按题材给预设；题材不属于任何预设时用空串这一组（通用）。
THEME_OPTIONS = {
    "悬疑推理": ("真相的代价", "正义与程序的冲突", "每个人都有不可告人的秘密", "记忆与谎言", "罪与罚"),
    "恐怖": ("无法摆脱的过去", "恐惧源自人心", "被遗忘的禁忌", "家族的诅咒", "理智的崩塌"),
    "科幻": ("技术进步的代价", "何以为人", "记忆与身份", "个体与集体的抉择", "文明的存续"),
    "爱情": ("成长与和解", "爱与自我实现", "错过与重逢", "治愈与陪伴", "门第与真心"),
    "": ("成长与代价", "选择与后果", "身份认同", "理想与现实"),
}
TONE_OPTIONS = {
    "悬疑推理": ("冷静克制、步步紧逼", "冷峻写实", "阴郁压抑", "紧张悬疑、反转不断"),
    "恐怖": ("阴森压抑", "不安渐强、慢热惊悚", "诡谲克制", "绝望窒息"),
    "科幻": ("冷峻理性", "克制而有张力", "宏大史诗", "荒凉孤寂"),
    "爱情": ("温暖治愈", "轻松甜蜜", "细腻克制", "虐心深情"),
    "": ("克制而有张力", "沉重写实", "轻松幽默", "温暖治愈"),
}

# 作者自己写的题材按评审规则的关键词归到预设题材，例如「都市悬疑」用悬疑推理的主题和基调。
# 键是 core/generation/domain_profiles.py 里关键词表的档案名。
PRESET_GENRE_BY_PROFILE = {
    "legal_suspense": "悬疑推理",
    "detective_mystery": "悬疑推理",
    "horror": "恐怖",
    "scifi": "科幻",
    "romance": "爱情",
}

# 每种故事结构的分段名。故事结构阶段（improve_structure）与场景规划阶段
# （generate_chapter_outline）都按这张表切分幕。除两种默认结构外，其余几种
# 只为读取以前选过它们的作品而保留。
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
    ),
}


def default_structure_for(length: str) -> str:
    return DEFAULT_STRUCTURE.get(length, DEFAULT_STRUCTURE["Novel (Standard)"])


# 势力、人物数量不再由作者填写：按篇幅定（势力数, 人物数）。
COUNTS_BY_LENGTH = {
    "Short Story": (3, 5),
    "Novella": (4, 6),
    "Novel (Standard)": (6, 8),
}


def counts_for(length: str) -> tuple[int, int] | None:
    """篇幅对应的（势力数, 人物数）；不认识的篇幅返回 None，由调用方沿用已有值。"""
    return COUNTS_BY_LENGTH.get(str(length or "").strip())


def sections_for(structure: str) -> tuple[str, ...]:
    return STRUCTURE_SECTIONS_MAP.get(structure, ())


def act_file_stem(structure: str, section: str) -> str:
    """故事结构阶段写某一幕时的文件名（不含扩展名），例如 6-act_structure_first_climax。"""
    part = section.lower().replace(" ", "_").replace(":", "").replace("/", "_")
    return f"{structure.lower().replace(' ', '_')}_{part}"


def act_for_file(stem: str) -> tuple[int, str] | None:
    """文件名认得出是哪一幕时，返回（在结构里的序号, 幕名）。"""
    stem = stem.lower()
    for structure, names in STRUCTURE_SECTIONS_MAP.items():
        for order, name in enumerate(names):
            if act_file_stem(structure, name) == stem:
                return order, name
    return None
