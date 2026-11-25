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


def sections_for(structure: str) -> tuple[str, ...]:
    return STRUCTURE_SECTIONS_MAP.get(structure, ())
