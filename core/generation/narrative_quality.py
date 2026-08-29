"""StoryScope-inspired diagnostics for Chinese narrative prose.

These signals describe a manuscript; they do not decide whether it is good.
Their purpose is to expose recurring AI-fiction tendencies to a blind reader
reviewer without leaking the chapter plan or telling the reviewer what the
chapter was *supposed* to achieve.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from statistics import mean
from typing import Any, Dict, Iterable, List, Sequence, Tuple


_THEME_PATTERNS = (
    r"这(?:才)?意味着",
    r"真正(?:重要|可怕|困难|危险|残酷|的问题)",
    r"(?:他|她|他们|她们)?终于明白",
    r"(?:他|她|他们|她们)?意识到",
    r"归根结底",
    r"说到底",
    r"这(?:就)?是.{0,8}(?:代价|道理|真相|意义)",
)
_CAUSAL_PATTERNS = (r"因此", r"所以", r"于是", r"因而", r"这导致", r"换句话说")
_EMBODIED_PATTERNS = (
    r"喉咙.{0,4}(?:发紧|发干|一紧|滚动)",
    r"胸口.{0,5}(?:发紧|一沉|堵|疼|窒)",
    r"心(?:脏|跳).{0,5}(?:加快|狂跳|一沉|漏了一拍)",
    r"冷汗",
    r"指节.{0,4}(?:发白|泛白)",
    r"手心.{0,4}(?:出汗|发凉|湿)",
    r"呼吸.{0,4}(?:一滞|急促|停住)",
)
_DIRECT_EMOTION_PATTERNS = (
    r"(?:感到|觉得|充满|陷入)(?:了)?(?:恐惧|害怕|愤怒|悲伤|绝望|焦虑|紧张|震惊)",
    r"(?:他|她)(?:很|十分|非常)?(?:害怕|愤怒|悲伤|绝望|焦虑|紧张|震惊)",
)
_BEHAVIORAL_EMOTION_PATTERNS = (
    r"移开(?:了)?目光",
    r"没有回答",
    r"沉默(?:了)?",
    r"停顿(?:了)?",
    r"攥(?:紧|住)",
    r"松开(?:了)?",
    r"后退(?:了)?半步",
    r"把.{0,8}(?:推开|扣下|藏起|放回)",
)
_TEMPORAL_PATTERNS = (
    r"(?:多年|数年|几个月|三天|两天|一天)前",
    r"那一年",
    r"小时候",
    r"回忆",
    r"后来(?:才)?(?:知道|明白|发现)",
    r"与此同时",
    r"同一时刻",
    r"时间回到",
)
_AMBIGUITY_PATTERNS = (
    r"未必",
    r"也许",
    r"不得不",
    r"两难",
    r"没有(?:正确|更好)的选择",
    r"既.{0,12}又",
    r"一方面.{0,20}另一方面",
    r"即使.{0,16}也",
)
_SUBPLOT_PATTERNS = (
    r"与此同时",
    r"另一边",
    r"同一时刻",
    r"电话那头",
    r"另一条(?:线索|消息|关系)",
    r"与此无关",
)
_RECONTEXTUALIZATION_PATTERNS = (
    r"原来",
    r"并非",
    r"不是.{0,12}而是",
    r"此前.{0,12}(?:以为|认定|判断)",
    r"重新(?:理解|看待|解释)",
    r"这让.{0,12}(?:变了|不同|倒过来)",
)


def _matches(text: str, patterns: Sequence[str], limit: int = 8) -> Tuple[int, List[str]]:
    found: List[Tuple[int, str]] = []
    total = 0
    for pattern in patterns:
        matches = list(re.finditer(pattern, text))
        total += len(matches)
        found.extend((match.start(), match.group(0)) for match in matches)
    found.sort(key=lambda item: item[0])
    return total, [value for _, value in found[:limit]]


def _density(count: int, char_count: int) -> float:
    return round(count * 10000 / max(char_count, 1), 2)


@dataclass(frozen=True)
class NarrativeSignal:
    count: int
    per_10k_chars: float
    examples: Tuple[str, ...] = ()


@dataclass
class NarrativeQualityReport:
    char_count: int
    dialogue_ratio: float
    thematic_explicitness: NarrativeSignal
    causal_explanation: NarrativeSignal
    embodied_emotion: NarrativeSignal
    direct_emotion_labels: NarrativeSignal
    behavioral_emotion: NarrativeSignal
    temporal_complexity: NarrativeSignal
    moral_ambiguity: NarrativeSignal
    subplot_independence: NarrativeSignal
    recontextualization: NarrativeSignal
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def prompt_block(self) -> str:
        data = self.to_dict()
        lines = [
            "以下是正文统计信号，只用于提醒你复核，不是评分答案，也不能单独构成失败：",
            f"- 字符数：{self.char_count}；引号对白占比：{self.dialogue_ratio:.1%}",
        ]
        for key in (
            "thematic_explicitness",
            "causal_explanation",
            "embodied_emotion",
            "direct_emotion_labels",
            "behavioral_emotion",
            "temporal_complexity",
            "moral_ambiguity",
            "subplot_independence",
            "recontextualization",
        ):
            signal = data[key]
            lines.append(
                f"- {key}: {signal['count']} 次（每万字 {signal['per_10k_chars']}）"
            )
        if self.warnings:
            lines.append("- 需人工复核：" + "；".join(self.warnings))
        return "\n".join(lines)


def analyze_narrative_quality(text: str) -> NarrativeQualityReport:
    """Return transparent, deterministic narrative-shape indicators."""

    content = text or ""
    char_count = len(re.sub(r"\s+", "", content))

    def signal(patterns: Sequence[str]) -> NarrativeSignal:
        count, examples = _matches(content, patterns)
        return NarrativeSignal(count, _density(count, char_count), tuple(examples))

    quoted = sum(len(match.group(1)) for match in re.finditer(r"[“\"]([^”\"]+)[”\"]", content))
    report = NarrativeQualityReport(
        char_count=char_count,
        dialogue_ratio=round(quoted / max(char_count, 1), 4),
        thematic_explicitness=signal(_THEME_PATTERNS),
        causal_explanation=signal(_CAUSAL_PATTERNS),
        embodied_emotion=signal(_EMBODIED_PATTERNS),
        direct_emotion_labels=signal(_DIRECT_EMOTION_PATTERNS),
        behavioral_emotion=signal(_BEHAVIORAL_EMOTION_PATTERNS),
        temporal_complexity=signal(_TEMPORAL_PATTERNS),
        moral_ambiguity=signal(_AMBIGUITY_PATTERNS),
        subplot_independence=signal(_SUBPLOT_PATTERNS),
        recontextualization=signal(_RECONTEXTUALIZATION_PATTERNS),
    )
    if report.thematic_explicitness.per_10k_chars >= 8:
        report.warnings.append("主题解释较密，检查是否替读者总结了本可由行动或意象承担的含义")
    if report.causal_explanation.per_10k_chars >= 14:
        report.warnings.append("因果连接词较密，检查情节是否被解释得过于整齐")
    if report.embodied_emotion.per_10k_chars >= 10:
        report.warnings.append("身体化情绪套语较密，检查是否出现可替换的生理反应")
    if char_count >= 2500 and report.moral_ambiguity.count == 0:
        report.warnings.append("本章没有检测到价值冲突信号；这不是错误，但需确认人物选择是否过于单向")
    if char_count >= 3500 and report.recontextualization.count == 0:
        report.warnings.append("本章没有检测到重新解释旧信息的信号")
    return report


def summarize_narrative_reports(texts: Iterable[str]) -> Dict[str, Any]:
    """Aggregate chapter reports so repeated corpus-level habits become visible."""

    reports = [analyze_narrative_quality(text) for text in texts]
    if not reports:
        return {"chapter_count": 0, "averages": {}, "warning_chapters": 0}
    keys = (
        "thematic_explicitness",
        "causal_explanation",
        "embodied_emotion",
        "direct_emotion_labels",
        "behavioral_emotion",
        "temporal_complexity",
        "moral_ambiguity",
        "subplot_independence",
        "recontextualization",
    )
    return {
        "chapter_count": len(reports),
        "averages": {
            key: round(mean(getattr(report, key).per_10k_chars for report in reports), 2)
            for key in keys
        },
        "average_dialogue_ratio": round(mean(report.dialogue_ratio for report in reports), 4),
        "warning_chapters": sum(bool(report.warnings) for report in reports),
        "chapters": [report.to_dict() for report in reports],
    }
