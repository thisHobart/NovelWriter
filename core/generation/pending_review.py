# -*- coding: utf-8 -*-
"""质量闸门拦下一章之后，留给作者的那份「问题在哪」。

闸门本来就把该说的都算出来了：哪一句触了硬伤、哪个维度差几分、要改成什么、
位置在第几场。但这些结论此前只随重试记录散落在 quality/legal_suspense_reviews/
下（一章能堆几十个 JSON），报错本身只带走一句「仍未通过质量检查，请人工审核」。
于是作者得先找到这一稿的正文，再按时间戳翻出最后一份评审，才能开始判断。

这里把闸门抬手那一刻的结论收成一份记录：一章一个文件，正文、问题清单、以及
足够「照建议重修」和「人工放行」用的现场快照都在里面。章节被接受后即删除，
所以文件存在本身就等于「这一章待复审」。

纯 JSON，不依赖任何评审类型或 Qt——界面与生成两边都要读它。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

PENDING_DIR = os.path.join("quality", "pending_review")

#: 一稿只写到一半就被拦下（场景级闸门）。没有完整章节，谈不上放行与定向重修。
STAGE_SCENE = "scene"
#: 整章写完了，卡在章节级检查。快照齐全，可以重修，也可以人工放行。
STAGE_CHAPTER = "chapter"

HARD = "hard"
DIMENSION = "dimension"
UPGRADE = "upgrade"

#: 清单排序：硬伤在前，未达标维度居中，可选润色垫底。
_KIND_ORDER = {HARD: 0, DIMENSION: 1, UPGRADE: 2}


@dataclass
class Issue:
    """清单里的一条。`quote` 是正文原句，界面据它在正文里定位。"""

    id: str
    kind: str
    title: str
    quote: str = ""
    detail: str = ""
    change: str = ""
    dimension: str = ""
    score: Optional[float] = None
    threshold: Optional[float] = None
    #: 对应到重修提示词里的那一条；维度行没有可执行文本，留空。
    ask: str = ""

    @property
    def selectable(self) -> bool:
        return bool(self.ask)


@dataclass
class Component:
    """章节总评由几个彼此独立的评审合成，这是其中一个的成绩。"""

    name: str
    label: str
    average: float
    threshold: float
    passed: bool


@dataclass
class PendingReview:
    chapter_number: int
    created_at: str = ""
    stage: str = STAGE_CHAPTER
    message: str = ""
    prose: str = ""
    prose_path: str = ""
    scope: str = ""
    components: List[Component] = field(default_factory=list)
    issues: List[Issue] = field(default_factory=list)
    snapshot: Dict[str, Any] = field(default_factory=dict)
    #: 评审自己没能给出结论（回复没写完、格式不对），不是判了不合格。这一稿没有
    #: 问题清单可循，出口是重跑评审或直接放行。
    verdict_unavailable: bool = False

    @property
    def needs_review_rerun(self) -> bool:
        """这一章卡住是因为没人判过，重跑一次评审就可能直接放行。"""
        return self.verdict_unavailable and self.resumable

    @property
    def resumable(self) -> bool:
        """有完整现场才谈得上定向重修与人工放行。"""
        return self.stage == STAGE_CHAPTER and bool(self.snapshot.get("scenes"))

    @property
    def hard_failures(self) -> List[Issue]:
        return [issue for issue in self.issues if issue.kind == HARD]

    @property
    def failed_dimensions(self) -> List[Issue]:
        return [issue for issue in self.issues if issue.kind == DIMENSION]

    @property
    def upgrades(self) -> List[Issue]:
        return [issue for issue in self.issues if issue.kind == UPGRADE]

    @property
    def headline(self) -> str:
        """章节列表右侧那一格：一眼看出卡在什么上。"""
        if self.verdict_unavailable and not self.issues:
            return "评审未出结论"
        hard = len(self.hard_failures)
        if hard:
            return f"{hard} 处硬伤"
        dimensions = len(self.failed_dimensions)
        if dimensions:
            return f"{dimensions} 项未达标"
        return "待复审"

    @property
    def verdict(self) -> str:
        """卡在哪个评审、差多少分。没有分数时退回错误原文。"""
        failed = [c for c in self.components if not c.passed]
        if not failed:
            return self.message
        worst = min(failed, key=lambda c: c.average - c.threshold)
        return f"{worst.label} {worst.average:.2f} / {worst.threshold:.2f}"

    def asks_for(self, issue_ids: Optional[List[str]] = None) -> List[str]:
        """选中的那几条摊平成重修提示词用的清单，顺序仍按严重程度。"""
        chosen = set(issue_ids) if issue_ids is not None else None
        return [
            issue.ask
            for issue in self.issues
            if issue.ask and (chosen is None or issue.id in chosen)
        ]


# ============================================================ 读写
def _path(output_dir: str, chapter_number: int) -> str:
    return os.path.join(output_dir, PENDING_DIR, f"chapter_{chapter_number}.json")


def save(record: PendingReview, output_dir: str) -> str:
    path = _path(output_dir, record.chapter_number)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = asdict(record)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)
    return path


def load(output_dir: str, chapter_number: int) -> Optional[PendingReview]:
    """读一章的待复审记录。读不出来就当没有——界面永远要能画出来。"""
    try:
        with open(_path(output_dir, chapter_number), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return _from_payload(payload, chapter_number)


def load_all(output_dir: str) -> Dict[int, PendingReview]:
    base = os.path.join(output_dir, PENDING_DIR)
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return {}
    records: Dict[int, PendingReview] = {}
    for name in names:
        match = re.fullmatch(r"chapter_(\d+)\.json", name)
        if not match:
            continue
        record = load(output_dir, int(match.group(1)))
        if record is not None:
            records[record.chapter_number] = record
    return records


def clear(output_dir: str, chapter_number: int) -> bool:
    """章节被接受后调用。已经没有记录时静默返回。"""
    try:
        os.remove(_path(output_dir, chapter_number))
        return True
    except OSError:
        return False


def _from_payload(payload: Dict[str, Any], chapter_number: int) -> PendingReview:
    issues = [
        Issue(**{k: v for k, v in raw.items() if k in Issue.__dataclass_fields__})
        for raw in payload.get("issues", [])
        if isinstance(raw, dict)
    ]
    components = [
        Component(**{k: v for k, v in raw.items() if k in Component.__dataclass_fields__})
        for raw in payload.get("components", [])
        if isinstance(raw, dict)
    ]
    return PendingReview(
        chapter_number=int(payload.get("chapter_number") or chapter_number),
        created_at=str(payload.get("created_at", "")),
        stage=str(payload.get("stage") or STAGE_CHAPTER),
        message=str(payload.get("message", "")),
        prose=str(payload.get("prose", "")),
        prose_path=str(payload.get("prose_path", "")),
        scope=str(payload.get("scope", "")),
        components=components,
        issues=issues,
        snapshot=payload.get("snapshot") or {},
        verdict_unavailable=bool(payload.get("verdict_unavailable")),
    )


# ============================================================ 从评审结论构造
_COMPONENT_LABELS = {
    "contract": "契约",
    "reader_blind": "读者盲测",
    "plausibility": "现实合理性",
    "chapter": "章节总评",
    "scene": "场景评审",
}


def _component_label(name: str) -> str:
    return _COMPONENT_LABELS.get(name, name)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _float_or_none(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def components_from_review(review: Dict[str, Any]) -> List[Component]:
    """拆出各分项评审的成绩；没有分项时退回总评自己一行。"""
    return [
        Component(
            name=name,
            label=_component_label(name),
            average=float(_float_or_none(child.get("average_score")) or 0.0),
            threshold=float(_float_or_none(child.get("pass_average")) or 0.0),
            passed=bool(child.get("passed")),
        )
        for name, child in _components(review)
    ]


def _components(review: Dict[str, Any]) -> List[tuple]:
    """(名字, 评审内容) 列表。没有分项评审时，总评自己算一项。"""
    children = review.get("component_reviews")
    if isinstance(children, dict) and children:
        return [
            (str(name), child)
            for name, child in children.items()
            if isinstance(child, dict)
        ]
    return [(str(review.get("stage") or "chapter"), review)]


def _list_source(review: Dict[str, Any], key: str) -> List[Any]:
    """取某一类改动条目。

    总评把三个分项评审的改动合并过一遍并给维度名加了前缀，分项里还留着同一份
    原文。两处都读就会把每条列两遍，所以总评有内容时以总评为准，只有在总评这
    一项为空（例如单场景评审）时才回到分项去取。
    """
    merged = review.get(key)
    if isinstance(merged, list) and merged:
        return merged
    items: List[Any] = []
    for _, child in _components(review):
        if child is review:
            continue
        value = child.get(key)
        if isinstance(value, list):
            items.extend(value)
    return items


def _hard_issues(review: Dict[str, Any], seen: set) -> List[Issue]:
    issues = []
    for raw in _list_source(review, "hard_failures"):
        if not isinstance(raw, dict):
            continue
        code = _clean(raw.get("code")) or "硬伤"
        quote = _clean(raw.get("quote"))
        problem = _clean(raw.get("problem"))
        change = _clean(raw.get("change"))
        key = (HARD, code, quote[:60])
        if key in seen:
            continue
        seen.add(key)
        ask = f"【硬伤·{code}】{problem}"
        if quote:
            ask += f"（原文：「{quote}」）"
        if change:
            ask += f"；最小改法：{change}"
        issues.append(
            Issue(
                id=f"hard-{len(seen)}",
                kind=HARD,
                title=code,
                quote=quote,
                detail=problem,
                change=change,
                dimension=_clean(raw.get("review")),
                ask=ask,
            )
        )
    return issues


def _dimension_issues(review: Dict[str, Any], seen: set) -> List[Issue]:
    """挡下这一章的那些维度。

    只看没通过的分项：闸门判的是分项平均分，通过的分项里某个维度偏低不是拦下
    的理由，列出来只会让作者去改一处本来就放行的地方。分数本身不可执行，所以
    这些条目不带勾选框，也不进重修提示词，只回答「差在哪、差多少」。
    """
    issues = []
    for name, child in _components(review):
        if child is not review and child.get("passed"):
            continue
        threshold = _float_or_none(child.get("pass_average")) or 0.0
        scores = child.get("scores")
        if not threshold or not isinstance(scores, dict):
            continue
        label = _component_label(name)
        for dimension, value in scores.items():
            score = _float_or_none(value)
            if score is None or score >= threshold:
                continue
            key = (DIMENSION, str(dimension))
            if key in seen:
                continue
            seen.add(key)
            issues.append(
                Issue(
                    id=f"dim-{len(seen)}",
                    kind=DIMENSION,
                    title=str(dimension),
                    detail=f"{label}：{score:.1f} 分，门槛 {threshold:.1f} 分",
                    dimension=str(dimension),
                    score=score,
                    threshold=threshold,
                )
            )
    issues.sort(key=lambda issue: issue.score if issue.score is not None else 0.0)
    return issues


def _instruction_issues(review: Dict[str, Any], seen: set) -> List[Issue]:
    """评审直接写下的修复指令：没有引用也没有维度，单独成条。"""
    issues = []
    for raw in _list_source(review, "repair_instructions"):
        text = _clean(raw)
        if not text:
            continue
        key = (UPGRADE, text[:80])
        if key in seen:
            continue
        seen.add(key)
        issues.append(
            Issue(
                id=f"fix-{len(seen)}",
                kind=UPGRADE,
                title="修复指令",
                detail=text,
                ask=text,
            )
        )
    return issues


def _upgrade_issues(review: Dict[str, Any], seen: set) -> List[Issue]:
    issues = []
    for raw in _list_source(review, "upgrades"):
        if not isinstance(raw, dict):
            continue
        quote = _clean(raw.get("quote"))
        missing = _clean(raw.get("missing"))
        change = _clean(raw.get("change"))
        dimension = _clean(raw.get("dimension"))
        key = (UPGRADE, dimension.split(".")[-1], quote[:60])
        if key in seen:
            continue
        seen.add(key)
        issues.append(
            Issue(
                id=f"up-{len(seen)}",
                kind=UPGRADE,
                title=dimension or "润色建议",
                quote=quote,
                detail=missing,
                change=change,
                dimension=dimension,
                ask=f"【{dimension}】原文「{quote}」缺少{missing}；改为：{change}",
            )
        )
    return issues


def issues_from_review(review: Dict[str, Any]) -> List[Issue]:
    """把一份未过闸的评审摊平成一份按严重程度排好的清单。

    同一处问题会同时出现在总评和分项评审里，靠 `seen` 去重；分数维度排在硬伤
    之后，是因为它说明「差多少」，而硬伤说明「哪一句不能留」。
    """
    seen: set = set()
    issues = _hard_issues(review, seen)
    issues += _dimension_issues(review, seen)
    issues += _instruction_issues(review, seen)
    issues += _upgrade_issues(review, seen)
    issues.sort(key=lambda issue: _KIND_ORDER.get(issue.kind, 9))
    return issues


def build(
    chapter_number: int,
    *,
    message: str,
    review: Optional[Dict[str, Any]],
    prose: str,
    prose_path: str = "",
    stage: str = STAGE_CHAPTER,
    snapshot: Optional[Dict[str, Any]] = None,
    verdict_unavailable: bool = False,
) -> PendingReview:
    """把闸门抬手那一刻手上有的东西收成一份记录。"""
    review = review or {}
    return PendingReview(
        chapter_number=int(chapter_number),
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        stage=stage,
        message=message,
        prose=prose,
        prose_path=prose_path,
        scope=_clean(review.get("repair_scope")),
        components=components_from_review(review) if review else [],
        issues=issues_from_review(review) if review else [],
        snapshot=snapshot or {},
        verdict_unavailable=bool(verdict_unavailable),
    )
