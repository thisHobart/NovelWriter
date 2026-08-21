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
    "长短句交替；动作场面以短句为主，一句话只承载一个主要动作或信息。"
    "叙述句以 15-25 字为宜，超过 40 字的长句不得超过三分之一。",
    "不要在“的”之前堆砌超过十二个字的修饰语；"
    "英文放在从句里的信息，中文要拆成独立短句依次说出，不能全压到中心词前面。",
    "人名、地名、机构名一律使用中文，正文中不得出现拉丁字母拼写的名字。",
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


# Thresholds are stated once so the prompt and the checker cannot drift apart.
# Calibrated against the generated chapters the reader rejected, which run
# 26-33% long sentences and 10-12% long attributives.  The limits sit below
# that band so the same prose fails, with headroom for text that merely has a
# few long sentences.
LONG_SENTENCE_CHARS = 40
LONG_SENTENCE_SHARE = 0.20
LONG_ATTRIBUTIVE_CHARS = 12
LONG_ATTRIBUTIVE_SHARE = 0.06

_CLAUSE_SPLIT = re.compile(r"[，。！？；：、\n“”（）()]")
_LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z'’. -]*[A-Za-z]|[A-Za-z]")
# Acronyms a Chinese crime novel genuinely uses unstranslated.
_ALLOWED_LATIN = {"DNA", "RNA", "CT", "MRI", "GPS", "ATM", "USB", "TV", "PH", "AB", "O"}


def _sentences(content: str) -> List[str]:
    return [
        re.sub(r"\s+", "", sentence)
        for sentence in re.split(r"[。！？…]+", content)
        if sentence.strip()
    ]


def _long_attributives(content: str) -> List[str]:
    """Clauses that stack a long modifier in front of 的.

    This is the single clearest marker of translated-sounding Chinese: the
    modifier that English puts in a relative clause after the noun gets piled
    up before it instead, and the reader has to hold it all until the head
    noun finally arrives.
    """
    found = []
    for clause in _CLAUSE_SPLIT.split(content):
        clause = re.sub(r"\s+", "", clause)
        position = clause.find("的")
        if position >= LONG_ATTRIBUTIVE_CHARS:
            found.append(clause)
    return found


def _foreign_names(content: str) -> List[str]:
    runs = {match.group().strip() for match in _LATIN_RUN.finditer(content)}
    return sorted(
        run for run in runs
        if len(run) > 1 and run.upper() not in _ALLOWED_LATIN
    )


def analyze_chinese_prose_style(content: str) -> List[str]:
    """Return lightweight, deterministic style warnings for generated prose."""
    if not content or not content.strip():
        return ["正文为空"]
    warnings = []
    repeated = [f"{term}×{content.count(term)}" for term in STYLE_CLICHES if content.count(term) > 2]
    if repeated:
        warnings.append("高频套语过多：" + "、".join(repeated))

    sentences = _sentences(content)
    long_sentences = [s for s in sentences if len(s) > LONG_SENTENCE_CHARS]
    if sentences and len(long_sentences) / len(sentences) > LONG_SENTENCE_SHARE:
        longest = max(long_sentences, key=len)
        warnings.append(
            f"长句过多：{len(long_sentences)}/{len(sentences)} 句超过 "
            f"{LONG_SENTENCE_CHARS} 字（中文叙述句以 15-25 字为宜）。"
            f"最长一句 {len(longest)} 字：「{longest[:40]}…」"
        )

    clauses = [c for c in (re.sub(r"\s+", "", c) for c in _CLAUSE_SPLIT.split(content)) if c]
    attributives = _long_attributives(content)
    if clauses and len(attributives) / len(clauses) > LONG_ATTRIBUTIVE_SHARE:
        worst = max(attributives, key=lambda c: c.find("的"))
        warnings.append(
            f"定语过长：{len(attributives)} 处小句在“的”之前堆了 "
            f"{LONG_ATTRIBUTIVE_CHARS} 字以上的修饰语，应拆成短句。"
            f"例如「{worst[:45]}…」"
        )

    foreign = _foreign_names(content)
    if foreign:
        warnings.append(
            "正文中出现外文：" + "、".join(foreign[:8])
            + "。人名、地名、机构名必须使用中文"
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


# 硬科幻专名：这些词在一部现实题材小说里几乎不可能是比喻。
# 曾经还收了「全息」「跃迁」，但它们在中文里同样是日常修辞——
# 「全息级高精度显微投影」说的是法医显微镜，「弧光跃迁」说的是人物弧光的转折。
# 这两个词误判了三次，每次都白烧一轮重试，还把文字改得更平，所以移出词表。
NON_SCIFI_MARKERS = (
    "行星",
    "星球",
    "Planet",
    "星际",
    "悬浮车",
    "太空港",
    "曲速",
    "反重力",
    "虫洞",
)

# 规划文档里的批注行（"* **叙事作用**：…"、"- 节奏说明：…"）谈的是写法本身，
# 里面的「弧光」「张力」这类术语是评论词汇，不是故事设定。
_ANNOTATION_LINE = re.compile(
    r"^\s*(?:[-*+]|\d+[.、)])?\s*(?:\*{2}[^*]{1,20}\*{2}|[^：:\n]{1,20})\s*[：:]"
)

# 只跳过纯写作批注。像「场景目标」「出场人物」「环境」这类标签后面跟的是故事内容，
# 真有科幻设定窜进去就该拦下，所以不列入。
_ANNOTATION_KEYS = (
    "作用",
    "衔接",
    "钩子",
    "节奏",
    "张力",
    "弧光",
    "写作",
    "文风",
    "备注",
    "说明",
    "要点",
)


def strip_planning_annotations(markdown_text: str) -> str:
    """Drop the planning commentary so only story content is genre-checked."""
    kept = []
    for line in (markdown_text or "").splitlines():
        match = _ANNOTATION_LINE.match(line)
        if match and any(key in match.group(0) for key in _ANNOTATION_KEYS):
            continue
        kept.append(line)
    return "\n".join(kept)


def find_scene_world_conflicts(
    scene_plan: str,
    lore: str,
    parameters: Dict[str, Any] | None,
) -> List[str]:
    """Find obvious genre additions present in a plan but absent from lore."""
    if is_scifi_story(parameters):
        return []
    haystack = strip_planning_annotations(scene_plan)
    conflicts = []
    for marker in NON_SCIFI_MARKERS:
        if marker in haystack and marker not in lore:
            conflicts.append(marker)
    return conflicts


def style_repair_instruction(warnings: List[str]) -> str:
    """Turn the checker's findings into a rewrite order the model can follow."""
    return (
        "\n\n以上正文未通过中文文风检查，请重写。只修改语言，"
        "不得改变情节、人物、对白内容和信息量：\n- "
        + "\n- ".join(warnings)
        + "\n\n重写要求：把长句拆成短句；"
        "“的”之前的修饰语超过十二个字的，改写成独立的短句依次陈述；"
        "人名、地名、机构名一律用中文。直接输出重写后的正文，不要解释。"
    )


def generate_prose_with_style_retry(
    send, prompt: str, *, retries: int = 2, logger=None, label: str = ""
) -> str:
    """Generate prose and rewrite it while it still reads as translationese.

    Style is a matter of degree, not a corrupt state, so a run that cannot be
    cleaned up returns its best attempt rather than failing the chapter — but
    it returns the *best* one, and says so, instead of silently keeping the
    first draft as the old code did.
    """
    best_text = ""
    best_warnings: List[str] = []
    current = prompt
    for attempt in range(retries + 1):
        text = send(current)
        if not text or not text.strip():
            raise RuntimeError(f"大模型未返回正文{('：' + label) if label else ''}")
        warnings = analyze_chinese_prose_style(text)
        if not warnings:
            return text
        if not best_text or len(warnings) < len(best_warnings):
            best_text, best_warnings = text, warnings
        if logger:
            logger.warning(
                "%s 中文文风检查未通过（第 %s 次）：%s",
                label or "正文",
                attempt + 1,
                "; ".join(warnings),
            )
        if attempt < retries:
            current = prompt + style_repair_instruction(warnings)
    if logger:
        logger.warning(
            "%s 重写 %s 次后仍有文风问题，采用问题最少的一稿：%s",
            label or "正文",
            retries,
            "; ".join(best_warnings),
        )
    return best_text
