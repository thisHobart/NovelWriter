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
    # 实测模型写出「HS-CONSTRUCTION-2018」「XF-0917」这类案号后被文风检查退回，
    # 各白烧一轮重写。规则本来就要求编号也用中文，只是从没说出口。
    "案号、卷号、文号、账号一律按中文公文写法写，例如「（2018）海刑初 123 号」"
    "「海公刑诉字〔2018〕第 17 号」，不要用 HS-2018、XF-0917 这类拉丁字母编号。"
    "DNA、CT 这类通用缩写可以保留。",
    "“冰冷、冷冽、瞬间、极度、死死、不容置疑、空气中弥漫、如同、仿佛”等套语只可偶尔使用，不得反复出现。",
    "避免译制片腔、企业汇报腔和网络套话，如“归档闭环、降维打击、技术威压、不可逆转的轨迹”。",
    # 九章首稿的「潜台词」维度全是 2.0 分，零方差、从第 17 章到第 25 章没有长进；
    # 重修一轮就到 3.5 以上，每章都白付这一轮，占掉整轮三成的调用。
    # 这两条原先写的是「不要发表连续口号」「不要由叙述者直接解释情绪结论」——规则在，
    # 但是抽象禁令，写的时候没法当场自查。评审给的改法反复指向同一个位置：段末与场末
    # 那句替读者总结的话。把判据改写成位置。
    "对白只说这个人此刻会说的话，符合他的身份和说话习惯，不要用它集中交代背景。",
    "不要替读者说出意义：人物的动机、立场、觉悟以及全篇的主题，只能从他做了什么、"
    "挑哪件事说、避开哪件事里看出来，不得由旁白点破，也不得让人物自己宣讲。"
    "最容易犯的位置是收尾——每一段和每一场的最后一句必须是动作、对白或可观察的细节，"
    "不得是总结句、定性句或格言式台词。",
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


def foreign_words(content: str) -> List[str]:
    """文本里真正算「没翻译」的外文串。

    单个字母不算：中文楼宇门牌本来就写「A 座」「B 栋」「1 号楼 C 单元」，把它判成
    外文会让设定阶段为一个正确的地名反复重试。缩写也不算，DNA、CT 这些在中文刑侦
    小说里本来就不翻译。剩下的成串字母才是遗留的英文名（Michelle Lee、Police
    Department）。
    """
    runs = {match.group().strip() for match in _LATIN_RUN.finditer(content)}
    return sorted(
        run for run in runs
        if len(run) > 1 and run.upper() not in _ALLOWED_LATIN
    )


def _foreign_names(content: str) -> List[str]:
    return foreign_words(content)


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


def first_field(record: Any, *keys: str) -> str:
    """按顺序取第一个有内容的字段；数组取第一项。

    人物卡有两种形状：模型生成的新卡用单数 goal/flaw/strength 与 description/
    background，旧项目的 characters.json 用复数数组与 appearance_summary/
    backstory_summary。四处读取方（章节写作、结构、短篇、世界观）此前各自只认
    一种，另一种就静默变成空。
    """
    if not isinstance(record, dict):
        return ""
    for key in keys:
        value = record.get(key)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        text = str(value or "").strip()
        if text:
            return text
    return ""


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
        # 新卡是单数 goal，旧项目的 factions.json 是复数 goals 的数组。只读复数
        # 会让整本书的势力摘要里「主要目标」一栏对新项目全空——而这份摘要正是
        # 章节写作、结构与短篇三条路径判断势力想要什么的唯一依据。
        goals = _as_list(faction.get("goals") or faction.get("goal"))
        if goals:
            # 分隔符保持原样：既有输出一直是逗号，没有理由在这次改动里换掉。
            details.append(f"主要目标：{', '.join(goals[:3])}")
        # 势力之间的利害关系是新卡才有的，旧项目没有这一栏，读不到就不写。
        conflicts = [
            f"与「{item.get('with')}」：{item.get('over', '')}".rstrip("：")
            for item in faction.get("conflicts", []) or []
            if isinstance(item, dict) and item.get("with")
        ]
        if conflicts:
            details.append(f"冲突：{'；'.join(conflicts[:3])}")
        summaries.append("\n".join(details))
    return "主要势力概览：\n\n" + "\n\n".join(summaries) if summaries else "没有可用的势力信息。"


#: 写进世界观提示词的人物字段。以前这份清单由各题材的 genre handler 各给一份
#: （科幻要 homeworld、悬疑要 agency），而人物卡本身是英文词表随机拼的。现在
#: 卡片是统一 schema，所以清单也只需要一份。复数字段名保留，旧项目的
#: characters.json 里 goals/motivations/flaws/strengths 仍是数组。
CHARACTER_PROMPT_KEYS = (
    "gender", "age", "faction", "profession", "title",
    "goal", "goals", "motivation", "motivations", "flaw", "flaws",
    "strength", "strengths", "arc", "background", "description",
)


def format_faction_section(factions: Any) -> str:
    """世界观提示词里的势力段落。

    以前这一段由 genre handler 的 get_faction_capitals_info 产出，每个题材读各自
    的字段名。势力卡改成统一 schema 之后，那些分支要么读到同样的字段、要么读到
    空——一份格式化就够了。
    """
    if isinstance(factions, dict):
        factions = factions.get("factions", [])
    if not isinstance(factions, list) or not factions:
        return ""

    lines = ["\n## 势力：\n"]
    for faction in factions:
        if not isinstance(faction, dict):
            continue
        name = faction.get("faction_name") or faction.get("name") or "未知势力"
        lines.append(f"- {name}")
        for label, key in (("性质", "nature"), ("类型", "type"), ("活动地点", "territory")):
            value = faction.get(key)
            if isinstance(value, dict):
                value = value.get("name", "")
            if value:
                lines.append(f"  - {label}：{value}")
        description = faction.get("faction_profile") or faction.get("description")
        if description:
            lines.append(f"  - 描述：{description}")
        goals = _as_list(faction.get("goal") or faction.get("goals"))
        if goals:
            lines.append(f"  - 目标：{'；'.join(goals[:3])}")
        for conflict in faction.get("conflicts", []) or []:
            if isinstance(conflict, dict) and conflict.get("with"):
                lines.append(f"  - 与「{conflict['with']}」冲突：{conflict.get('over', '')}")
    return "\n".join(lines) + "\n"


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


def _style_warning_score(warnings: List[str]) -> tuple[int, int]:
    """Rank failed style drafts by category count, then issue occurrences."""
    occurrences = 0
    for warning in warnings:
        if warning.startswith(("长句过多：", "定语过长：")):
            match = re.search(r"：(\d+)", warning)
            occurrences += int(match.group(1)) if match else 1
        elif warning.startswith("高频套语过多："):
            counts = [int(value) for value in re.findall(r"×(\d+)", warning)]
            occurrences += sum(counts) if counts else 1
        elif warning.startswith("正文中出现外文："):
            listing = warning.split("：", 1)[1].split("。", 1)[0]
            occurrences += len([item for item in listing.split("、") if item])
        else:
            occurrences += 1
    return len(warnings), occurrences


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
    best_score: tuple[int, int] | None = None
    current = prompt
    for attempt in range(retries + 1):
        text = send(current)
        if not text or not text.strip():
            raise RuntimeError(f"大模型未返回正文{('：' + label) if label else ''}")
        warnings = analyze_chinese_prose_style(text)
        if not warnings:
            return text
        score = _style_warning_score(warnings)
        if not best_text or best_score is None or score < best_score:
            best_text, best_warnings = text, warnings
            best_score = score
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
