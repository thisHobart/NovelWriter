"""Shared prompt context and validation helpers for story generation."""

import re
from typing import Any, Dict, Iterable, List

from core.localization import zh_label


PARAMETER_ALIASES = {
    "Genre": ("Genre", "genre"),
    "Subgenre": ("Subgenre", "subgenre"),
    "Story Length": ("Story Length", "story_length"),
    "Story Structure": ("Story Structure", "story_structure"),
    "Theme": ("Theme", "theme"),
    "Tone": ("Tone", "tone"),
    "Novel Title": ("Novel Title", "novel_title"),
}


def normalize_story_parameters(parameters: Dict[str, Any] | None) -> Dict[str, Any]:
    """Normalize GUI-style and parameters.txt-style keys."""
    source = parameters or {}
    normalized = dict(source)
    for canonical, aliases in PARAMETER_ALIASES.items():
        for alias in aliases:
            value = source.get(alias)
            if value not in (None, ""):
                normalized[canonical] = value
                break
    return normalized


def format_genre_label(parameters: Dict[str, Any] | None) -> str:
    params = normalize_story_parameters(parameters)
    genre = params.get("Genre", "General Fiction")
    subgenre = params.get("Subgenre", "")
    label = zh_label(genre)
    if subgenre and subgenre != genre:
        label = f"{label}（{zh_label(subgenre)}）"
    return label


def is_scifi_story(parameters: Dict[str, Any] | None) -> bool:
    genre = str(normalize_story_parameters(parameters).get("Genre", "")).lower()
    return genre in {"sci-fi", "science fiction", "scifi"}


def is_legal_suspense(parameters: Dict[str, Any] | None) -> bool:
    params = normalize_story_parameters(parameters)
    genre = str(params.get("Genre", "")).lower()
    subgenre = str(params.get("Subgenre", "")).lower()
    mystery_genres = {"mystery", "悬疑", "悬疑推理", "推理"}
    return genre in mystery_genres and ("legal" in subgenre or "法律" in subgenre)


def build_story_parameter_lines(parameters: Dict[str, Any] | None) -> List[str]:
    params = normalize_story_parameters(parameters)
    lines = [
        "## 作品参数（最高优先级）：",
        f"- 类型：{format_genre_label(params)}",
    ]
    for key, label in (
        ("Story Length", "篇幅"),
        ("Story Structure", "结构"),
        ("Theme", "主题"),
        ("Tone", "基调"),
    ):
        value = params.get(key)
        if value:
            lines.append(f"- {label}：{zh_label(value)}")
    return lines


def build_location_guidance(parameters: Dict[str, Any] | None) -> List[str]:
    """Return genre-safe location instructions for planning prompts."""
    if is_scifi_story(parameters):
        location_examples = "行星、城市、区域、建筑和房间等具体地点"
        restriction = "不得加入世界观未定义的星球、物种或未来技术。"
    else:
        location_examples = "城市、街区、建筑和房间等具体地点"
        restriction = (
            "本作不是科幻小说：不得把城市改写成星球，不得擅自加入行星、"
            "星际机构、悬浮交通、全息设备、未来武器或其他科幻设定。"
        )
    return [
        f"地点只使用世界观已经定义的{location_examples}。",
        restriction,
        "若章节大纲或场景规划与世界观中的地点、时代或技术冲突，以世界观为准并静默纠正。",
    ]


CHINESE_PROSE_REQUIREMENTS = [
    "采用自然、克制的现代中文小说语言，符合中国读者的阅读习惯。",
    "优先使用准确的名词和动词，避免连续堆叠形容词、副词和比喻。",
    "长短句交替；动作场面以短句为主，一句话只承载一个主要动作或信息。",
    "“冰冷、冷冽、瞬间、极度、死死、不容置疑、空气中弥漫、如同、仿佛”等套语只可偶尔使用，不得反复出现。",
    "避免译制片腔、企业汇报腔和网络套话，如“归档闭环、降维打击、技术威压、不可逆转的轨迹”。",
    "对白应符合人物身份和真实说话习惯，不要用对白集中解释背景或发表连续口号。",
    "通过行动、反应和可观察细节暗示人物目的，不要由叙述者直接解释阴谋或情绪结论。",
    "法律、法医和技术术语必须准确；不确定时使用朴素表达，不得编造术语、精确数值或鉴定结论。",
    "保持司法体系和程序称谓一致，不得混用不同法域的机构、罪名和诉讼程序。",
]

STYLE_CLICHES = (
    "冰冷",
    "冷冽",
    "瞬间",
    "极度",
    "死死",
    "不容置疑",
    "空气中弥漫",
    "如同",
    "仿佛",
)


def analyze_chinese_prose_style(content: str) -> List[str]:
    """Return lightweight, deterministic style warnings for generated prose."""
    if not content or not content.strip():
        return ["正文为空"]
    warnings = []
    repeated = [f"{term}×{content.count(term)}" for term in STYLE_CLICHES if content.count(term) > 2]
    if repeated:
        warnings.append("高频套语过多：" + "、".join(repeated))

    sentences = [
        re.sub(r"\s+", "", sentence)
        for sentence in re.split(r"[。！？]+", content)
        if sentence.strip()
    ]
    long_sentences = [sentence for sentence in sentences if len(sentence) > 60]
    if sentences and len(long_sentences) / len(sentences) > 0.15:
        warnings.append(
            f"长句比例过高：{len(long_sentences)}/{len(sentences)} 句超过60字"
        )
    return warnings


def sanitize_lore_content(content: str) -> str:
    """Remove leading model-analysis sections from generated lore."""
    if not content or not content.strip():
        return "Lore not available."

    lines = content.strip().splitlines()
    meta_heading = re.compile(
        r"^(?:defining|analy[sz]ing|developing|refining|exploring|"
        r"constructing|considering|crafting|mapping|formulating|"
        r"establishing|finali[sz]ing|reviewing|assessing|synthesi[sz]ing)\b",
        re.IGNORECASE,
    )
    headings = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not (
            stripped.startswith("#")
            or (stripped.startswith("**") and stripped.endswith("**"))
        ):
            continue
        heading = stripped.lstrip("#").strip().strip("*_ ")
        headings.append((index, bool(meta_heading.match(heading))))

    if headings and headings[0][1]:
        for index, is_meta in headings[1:]:
            if not is_meta:
                return "\n".join(lines[index:]).strip() or "Lore not available."
    return content.strip()


def _as_list(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)]


def format_faction_summary(factions_data: Any, limit: int = 5) -> str:
    """Format both current and legacy faction JSON schemas."""
    if isinstance(factions_data, dict):
        factions_data = factions_data.get("factions", [factions_data])
    if not isinstance(factions_data, list):
        return "没有可用的势力信息。"

    summaries = []
    for faction in factions_data[:limit]:
        if not isinstance(faction, dict):
            continue
        name = faction.get("faction_name") or faction.get("name") or "无"
        profile = faction.get("faction_profile") or faction.get("description") or "无"
        details = [f"势力名称：{name}", f"简介：{profile}"]
        faction_type = faction.get("type")
        if faction_type:
            details.append(f"类型：{faction_type}")
        jurisdiction = faction.get("jurisdiction") or faction.get("territory")
        if jurisdiction:
            details.append(f"管辖或活动范围：{jurisdiction}")
        traits = _as_list(faction.get("primary_traits") or faction.get("traits"))
        if traits:
            details.append(f"主要特征：{', '.join(traits[:3])}")
        goals = _as_list(faction.get("goals"))
        if goals:
            details.append(f"主要目标：{', '.join(goals[:3])}")
        summaries.append("\n".join(details))
    return "主要势力概览：\n\n" + "\n\n".join(summaries) if summaries else "没有可用的势力信息。"


NON_SCIFI_MARKERS = ("行星", "星球", "Planet", "星际", "悬浮车", "全息", "太空港", "跃迁")


def find_scene_world_conflicts(
    scene_plan: str,
    lore: str,
    parameters: Dict[str, Any] | None,
) -> List[str]:
    """Find obvious genre additions present in a plan but absent from lore."""
    if is_scifi_story(parameters):
        return []
    conflicts = []
    for marker in NON_SCIFI_MARKERS:
        if marker in scene_plan and marker not in lore:
            conflicts.append(marker)
    return conflicts
