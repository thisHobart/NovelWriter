# -*- coding: utf-8 -*-
"""作品参数的选项表——与界面框架无关的唯一来源。

智能体（`agents/writing/chapter_writing_agent.py`）和 Qt 界面都需要这些选项，
因此统一放在配置层，避免生成层依赖任何 GUI 框架。

只放数据与纯查询函数，不做任何 I/O。
"""
from __future__ import annotations

from dataclasses import dataclass

LENGTH_OPTIONS = ["Short Story", "Novella", "Novel (Standard)", "Short Video Script"]


@dataclass(frozen=True)
class LengthProfile:
    """一档篇幅的具体要求。以前模型只收到「篇幅：长篇小说」几个字，写几章、每场多长全靠它猜：
    样例长篇已写的 22 章从 1744 字到 4925 字，差了近三倍。

    total        全篇大约多少字，写给模型和作者看
    structure    新作品用的故事结构（已有作品沿用参数文件里保存的结构）
    chapters     全篇几章；1 表示整篇一章写完，走短篇那条流程
    scenes       每章几场（上下限）
    scene_chars  每场正文大约多少字（上下限）
    counts       写几个势力、几个人物
    traits       这一档篇幅的写法特点
    """

    total: str
    structure: str
    chapters: int
    scenes: tuple[int, int]
    scene_chars: tuple[int, int]
    counts: tuple[int, int]
    traits: str

    @property
    def single_chapter(self) -> bool:
        return self.chapters == 1

    def chapters_per_act(self, structure: str) -> int | None:
        """作品用的就是这一档的结构、章数又能按幕平分时，每一幕写几章；否则 None。"""
        sections = sections_for(structure)
        if self.single_chapter or structure != self.structure or not sections or self.chapters % len(sections):
            return None
        return self.chapters // len(sections)

    def summary(self, structure: str | None = None) -> str:
        """一句话说清这一档多长，例如「全书约 10 万字，6 幕、约 30 章，每章 2–4 场，每场约 1000–1500 字」。

        给了 structure 而它不是这一档的结构时（以前选过别的结构的作品），不提幕数和章数，
        免得和作品自己的结构对不上。
        """
        scenes = f"{self.scenes[0]}–{self.scenes[1]} 场"
        chars = f"每场约 {self.scene_chars[0]}–{self.scene_chars[1]} 字"
        if self.single_chapter:
            return f"全篇{self.total}，一章写完，分 {scenes}，{chars}"
        if structure and structure != self.structure:
            return f"全书{self.total}，每章 {scenes}，{chars}"
        acts = len(sections_for(self.structure))
        return f"全书{self.total}，{acts} 幕、约 {self.chapters} 章，每章 {scenes}，{chars}"


# 每档篇幅的数字。长篇照样例作品实际写出来的量定：30 章，每章 2–3 场，每章约 3000 字。
# 短篇整篇作为一个章节送进质量闸门，场数上限见 planning_prompt 里的说明。
LENGTH_PROFILES: dict[str, LengthProfile] = {
    "Short Story": LengthProfile(
        total="约 1 万字",
        structure="3-Act Structure",
        chapters=1,
        scenes=(3, 6),
        scene_chars=(1500, 2500),
        counts=(3, 5),
        traits="一个核心事件，一条主线，不开支线；时间跨度短，结尾落在一次揭示或转折上。",
    ),
    "Novella": LengthProfile(
        total="约 4 万字",
        structure="3-Act Structure",
        chapters=12,
        scenes=(2, 4),
        scene_chars=(1000, 1500),
        counts=(4, 6),
        traits="一条主线加一条支线；主角的弧光完整，中段有一次大转折。",
    ),
    "Novel (Standard)": LengthProfile(
        total="约 10 万字",
        structure="6-Act Structure",
        chapters=30,
        scenes=(2, 4),
        scene_chars=(1000, 1500),
        counts=(6, 8),
        traits="主线加两三条支线交替推进；两次高潮各有收获和代价；每幕结尾留下推动下一幕的问题。",
    ),
    "Short Video Script": LengthProfile(
        total="约 2000 字以内",
        structure="3-Act Structure",
        chapters=1,
        scenes=(2, 3),
        scene_chars=(500, 800),
        counts=(2, 3),
        traits="一个核心事件，两三个人物，地点集中在一两处；用看得见的动作和对白推进，少写心理和背景；"
               "第一段就进入冲突，结尾落在一个看得见的结果或反转上，方便改编成一分钟以内的短视频。",
    ),
}


def length_profile(length: str | None) -> LengthProfile | None:
    """篇幅对应的档案；不认识的篇幅（以前的取值）返回 None，调用方照旧只写篇幅名。"""
    return LENGTH_PROFILES.get(str(length or "").strip())


def is_single_chapter(length: str | None) -> bool:
    """整篇一章写完、走短篇那条流程的篇幅（短篇小说、短视频脚本）。"""
    profile = length_profile(length)
    return bool(profile and profile.single_chapter)


# 故事结构不再由作者选择：新作品按篇幅固定结构。已有作品沿用参数文件里保存的结构。
DEFAULT_STRUCTURE = {length: profile.structure for length, profile in LENGTH_PROFILES.items()}

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
COUNTS_BY_LENGTH = {length: profile.counts for length, profile in LENGTH_PROFILES.items()}


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
