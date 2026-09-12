# -*- coding: utf-8 -*-
"""从项目已有的产物里算出一份质量快照，不花任何模型调用。

诊断此前只存在于一次性的临时脚本里：翻评审留档、数重试记录、分类调用日志，
算完就没了。数字是某一刻的快照，而页面上写的比例过几周就不再是真的，却不会
有任何东西提示它已经失真——这正是这个项目反复出问题的形态。

这里把那些计算固定下来，并且**带基线对比**：改完写作提示词之后，某一项到底
有没有好转，只有跑两次、拿两份快照比才答得出来。

排版与命令行在 ``tools/diagnose_quality.py``；这个模块只算，不打印。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from glob import glob
from typing import Any, Dict, List, Optional, Tuple

from core.generation.pending_review import components_from_review

SCHEMA = "quality_snapshot_v1"

#: 评审留档的目录名是写死的字面量，不是按题材拼的（见 story_ledger.py 里
#: ``review_dir`` 的赋值）。奇幻项目也写进这个目录，所以这里当常量用；
#: 拼成 ``quality/{题材}_reviews`` 会一份都找不到。
REVIEW_ROOT = ("quality", "legal_suspense_reviews")
REPORT_DIR = ("quality", "reports")
SNAPSHOT_STEM = "quality_snapshot"

#: 文件名形如「阶段_YYYYMMDD_HHMMSS_微秒」。直接用前缀 glob 会让 chapter 把
#: chapter_retry_1 也算进去，那正是「重修跑了几轮」被算错的原因。
REVIEW_STAMP = re.compile(r"^(?P<stage>.+)_\d{8}_\d{6}_\d+$")

#: 闸门名必须整体解析。``chapter_retry_1_scene_2`` 是「整章重修期间的第二场景
#: 评审」，按 ``_retry_`` 切前缀会把它算到整章那一栏去。
GATE_STAGE = re.compile(
    r"^(?P<outer>chapter|plan|scene_\d+)"
    r"(?:_retry_(?P<retry>\d+))?"
    r"(?:_(?P<inner>scene_\d+))?"
    r"(?:_(?P<marker>waived|waived_by_author|rerun))?$"
)

#: 追踪记录里的阶段名把章号和重试轮次插在里面（chapter_24_write），
#: 按原样分组会碎得没法看。
TRACE_CHAPTER = re.compile(r"^chapter_(?P<chapter>\d+)_")
TRACE_SUFFIX = re.compile(r"_(?:retry|attempt)_\d+$")

#: 旧追踪把「写正文并评审验收」整段只记成 write：`run_sandbox_chapters` 用
#: `chapter_N_write` 包住了整个成章流程，评审与重修都算在里面。实测一轮 61 次调用
#: 里评审占 34 次，按字面标成「写正文」会让成本报告谎报正文的开销。归一到两个工具
#: 里那个说全了的名字，新旧存档才算同一件事。
TRACE_ACTIVITY_ALIASES = {"write": "write_review_accept"}

#: 分母变动超过这个幅度时，比率的变化就不再是质量信号。
DENOMINATOR_DRIFT = 0.20


@dataclass(frozen=True)
class GateStage:
    """一份评审留档属于哪个闸门、是第几轮。"""

    gate: str
    retry: int
    marker: str


@dataclass
class ParsedReview:
    chapter: int
    stage: GateStage
    raw_stage: str
    payload: Dict[str, Any]

    @property
    def is_merged(self) -> bool:
        """是不是一份带分项的章节合议评审。

        唯一可靠的判据是 component_reviews 这个键，不是 stage 名字白名单：
        分项转换器在没有这个键时会把总评自己退回成一项，于是
        acceptance_canon 这类自由格式留档会凭空变出一个分数为零、判为未通过
        的假分项，把未通过率整个推高。
        """
        children = self.payload.get("component_reviews")
        return isinstance(children, dict) and bool(children)


@dataclass
class ReviewCorpus:
    chapters: Dict[int, List[ParsedReview]] = field(default_factory=dict)
    unreadable: List[Dict[str, str]] = field(default_factory=list)
    file_count: int = 0

    def all_reviews(self) -> List[ParsedReview]:
        return [item for items in self.chapters.values() for item in items]

    def merged(self) -> List[ParsedReview]:
        return [item for item in self.all_reviews() if item.is_merged]


def _rate(hit: int, total: int) -> Optional[float]:
    """全模块唯一的除法入口。

    分母为零返回 None 而不是 0.0：前者表示「没有分母」，后者表示「确实零失败」。
    混同这两件事，会让一个还没开始写的项目看起来完美。
    """
    if not total:
        return None
    return round(hit / total, 4)


def parse_gate_stage(stage: str) -> Optional[GateStage]:
    """把留档的阶段名解析成闸门。不是闸门评审就返回 None。"""
    match = GATE_STAGE.match(str(stage or "").strip())
    if not match:
        return None
    inner = match.group("inner")
    return GateStage(
        gate=inner or match.group("outer"),
        retry=int(match.group("retry") or 0),
        marker=match.group("marker") or "",
    )


def parse_trace_stage(stage: str) -> Tuple[Optional[int], str]:
    """把追踪里的阶段名拆成（章号，做的是什么事）。"""
    text = str(stage or "").strip()
    if not text:
        return None, "未标注"
    chapter = None
    match = TRACE_CHAPTER.match(text)
    if match:
        chapter = int(match.group("chapter"))
        text = text[match.end():]
    activity = TRACE_SUFFIX.sub("", text) or "未标注"
    return chapter, TRACE_ACTIVITY_ALIASES.get(activity, activity)


def review_root(output_dir: str) -> str:
    return os.path.join(output_dir, *REVIEW_ROOT)


def scan_reviews(output_dir: str) -> ReviewCorpus:
    """走一遍评审留档。读不了的文件记下来、跳过，不抛。"""
    corpus = ReviewCorpus()
    root = review_root(output_dir)
    if not os.path.isdir(root):
        return corpus

    for chapter_dir in sorted(glob(os.path.join(root, "chapter_*"))):
        match = re.search(r"chapter_(\d+)$", os.path.basename(chapter_dir))
        if not match:
            continue
        chapter = int(match.group(1))
        for path in sorted(glob(os.path.join(chapter_dir, "*.json"))):
            corpus.file_count += 1
            stamp = REVIEW_STAMP.match(os.path.splitext(os.path.basename(path))[0])
            if not stamp:
                continue
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, ValueError) as exc:
                corpus.unreadable.append(
                    {"path": os.path.relpath(path, output_dir), "error": str(exc)}
                )
                continue
            if not isinstance(payload, dict):
                corpus.unreadable.append(
                    {"path": os.path.relpath(path, output_dir), "error": "顶层不是对象"}
                )
                continue
            raw_stage = stamp.group("stage")
            gate = parse_gate_stage(raw_stage)
            if gate is None:
                continue
            corpus.chapters.setdefault(chapter, []).append(
                ParsedReview(chapter, gate, raw_stage, payload)
            )
    return corpus


def reviewer_section(corpus: ReviewCorpus) -> Dict[str, Any]:
    """各分项评审各自的未通过率。

    按实际出现的键遍历、每项各存各的分母：分项不止三个（还有 continuity），
    而且整章重跑那种评审只带 contract 与 reader_blind，没有 plausibility。
    共用一个分母会把数字算歪。
    """
    tally: Dict[str, Dict[str, Any]] = {}
    for review in corpus.merged():
        for component in components_from_review(review.payload):
            entry = tally.setdefault(
                component.name,
                {"reviewed": 0, "not_passed": 0, "score_sum": 0.0, "threshold": None},
            )
            entry["reviewed"] += 1
            if not component.passed:
                entry["not_passed"] += 1
            entry["score_sum"] += component.average
            if component.threshold:
                entry["threshold"] = component.threshold

    result: Dict[str, Any] = {}
    for name, entry in tally.items():
        reviewed = entry["reviewed"]
        result[name] = {
            "reviewed": reviewed,
            "not_passed": entry["not_passed"],
            "rate": _rate(entry["not_passed"], reviewed),
            "average": round(entry["score_sum"] / reviewed, 3) if reviewed else None,
            "threshold": entry["threshold"],
        }
    return result


def gate_section(corpus: ReviewCorpus) -> Dict[str, Any]:
    """各闸门的重试率与整章重跑率。

    这两件事必须分开报。闸门原地重试会在文件名里留下 _retry_N；而整章流程从头
    重跑不会——它表现为同一个不带 retry 的阶段名在一章里出现两次以上。实测
    current_work：规划带 retry 的只有 3 章，而重跑过的有 12 章。只报前者，会对
    一个大半章节被重新规划过的项目印出「规划重试率 5%」。
    """
    gates: Dict[str, Dict[str, Any]] = {}
    for chapter, reviews in corpus.chapters.items():
        seen_bare: Dict[str, int] = {}
        retried: Dict[str, int] = {}
        markers: Dict[str, set] = {}
        for review in reviews:
            gate = review.stage.gate
            if review.stage.retry:
                retried[gate] = max(retried.get(gate, 0), review.stage.retry)
            elif not review.stage.marker:
                seen_bare[gate] = seen_bare.get(gate, 0) + 1
            if review.stage.marker:
                markers.setdefault(gate, set()).add(review.stage.marker)

        for gate in set(seen_bare) | set(retried) | set(markers):
            entry = gates.setdefault(
                gate,
                {
                    "chapters": 0,
                    "reviews": 0,
                    "retried_chapters": 0,
                    "rerun_chapters": 0,
                    "max_retry": 0,
                    "marked_chapters": {},
                },
            )
            entry["chapters"] += 1
            if gate in retried:
                entry["retried_chapters"] += 1
                entry["max_retry"] = max(entry["max_retry"], retried[gate])
            if seen_bare.get(gate, 0) > 1:
                entry["rerun_chapters"] += 1
            for marker in markers.get(gate, ()):
                counts = entry["marked_chapters"]
                counts[marker] = counts.get(marker, 0) + 1

    for review in corpus.all_reviews():
        if review.stage.gate in gates:
            gates[review.stage.gate]["reviews"] += 1

    for entry in gates.values():
        chapters = entry["chapters"]
        entry["retry_rate"] = _rate(entry["retried_chapters"], chapters)
        entry["rerun_rate"] = _rate(entry["rerun_chapters"], chapters)
    return gates


def hard_failure_section(corpus: ReviewCorpus) -> Dict[str, Any]:
    """硬失败码的分布。硬失败不看分数，命中即拦。"""
    codes: Dict[str, int] = {}
    chapters: set = set()
    reviews_with_any = 0
    for review in corpus.all_reviews():
        found = False
        for raw in review.payload.get("hard_failures") or []:
            code = ""
            if isinstance(raw, dict):
                code = str(raw.get("code") or "").strip()
            elif isinstance(raw, str):
                code = raw.strip()
            if not code:
                continue
            codes[code] = codes.get(code, 0) + 1
            chapters.add(review.chapter)
            found = True
        if found:
            reviews_with_any += 1
    total = sum(codes.values())
    return {
        "total": total,
        "chapters_affected": len(chapters),
        "reviews_with_any": reviews_with_any,
        "codes": dict(sorted(codes.items(), key=lambda item: -item[1])),
        "top_share": _rate(max(codes.values()), total) if codes else None,
    }


def planning_rejection_section(output_dir: str) -> Dict[str, Any]:
    """规划契约被判不合格的原因，来自重试存档。"""
    root = os.path.join(output_dir, "archive", "planning_retries")
    if not os.path.isdir(root):
        return {
            "available": False,
            "reason": "没有规划重试存档：archive/planning_retries 不存在",
            "attempts": None,
            "chapters": None,
            "codes": {},
            "unreadable": [],
        }

    codes: Dict[str, int] = {}
    chapters: set = set()
    attempts = 0
    unreadable: List[Dict[str, str]] = []
    for path in sorted(glob(os.path.join(root, "*", "attempt_*_defects.json"))):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            unreadable.append(
                {"path": os.path.relpath(path, output_dir), "error": str(exc)}
            )
            continue
        if not isinstance(payload, list):
            unreadable.append(
                {"path": os.path.relpath(path, output_dir), "error": "顶层不是数组"}
            )
            continue
        attempts += 1
        match = re.search(r"chapter_(\d+)", path.replace("\\", "/"))
        if match:
            chapters.add(int(match.group(1)))
        for defect in payload:
            if not isinstance(defect, dict):
                continue
            code = str(defect.get("code") or "").strip()
            if code:
                codes[code] = codes.get(code, 0) + 1

    return {
        "available": True,
        "reason": "",
        "attempts": attempts,
        "chapters": len(chapters),
        "codes": dict(sorted(codes.items(), key=lambda item: -item[1])),
        "unreadable": unreadable,
    }


def cost_section(output_dir: str) -> Dict[str, Any]:
    """调用成本，只在项目里确实有调用日志时才算。

    追踪是 opt-in 的，只有沙箱与端到端工具跑才记录，所以正常用界面生成的项目
    一条都没有。没有就说清楚原因，不去动生成链路。
    """
    logs = sorted(
        glob(os.path.join(output_dir, "system", "quality_runs", "*", "llm_calls.jsonl"))
    )
    if not logs:
        return {
            "available": False,
            "reason": "没有调用日志：system/quality_runs/*/llm_calls.jsonl 不存在。"
                      "追踪是 opt-in 的，只有沙箱与端到端工具跑才记录",
            "calls": None,
            "runs": [],
            "by_activity": {},
        }

    by_activity: Dict[str, Dict[str, Any]] = {}
    calls = failed = unparsable = exact_usage = 0
    elapsed_ms = 0.0
    prompt_tokens = completion_tokens = 0
    for path in logs:
        try:
            handle = open(path, "r", encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    unparsable += 1
                    continue
                if not isinstance(record, dict):
                    unparsable += 1
                    continue
                calls += 1
                if record.get("success") is False:
                    failed += 1
                usage = record.get("usage") or {}
                if isinstance(usage, dict):
                    if usage.get("total_tokens") is not None:
                        exact_usage += 1
                    prompt_tokens += int(usage.get("estimated_prompt_tokens") or 0)
                    completion_tokens += int(
                        usage.get("estimated_completion_tokens") or 0
                    )
                elapsed_ms += float(record.get("elapsed_ms") or 0)
                _, activity = parse_trace_stage(record.get("stage"))
                entry = by_activity.setdefault(
                    activity, {"calls": 0, "prompt_chars": 0, "elapsed_ms": 0.0}
                )
                entry["calls"] += 1
                entry["prompt_chars"] += int(record.get("prompt_chars") or 0)
                entry["elapsed_ms"] += float(record.get("elapsed_ms") or 0)

    for entry in by_activity.values():
        entry["elapsed_ms"] = round(entry["elapsed_ms"], 1)
        entry["share"] = _rate(entry["calls"], calls)

    return {
        "available": True,
        "reason": "",
        # 后端不返回真实用量，这句必须跟着数字一起走，不能在转述时掉队。
        "note": "token 为估算值：后端不暴露真实用量，费用无从计算",
        "runs": [os.path.basename(os.path.dirname(path)) for path in logs],
        "calls": calls,
        "failed_calls": failed,
        "unparsable_lines": unparsable,
        "exact_usage_calls": exact_usage,
        "amount_usd": None,
        "estimated_prompt_tokens": prompt_tokens,
        "estimated_completion_tokens": completion_tokens,
        "elapsed_ms": round(elapsed_ms, 1),
        "by_activity": dict(
            sorted(by_activity.items(), key=lambda item: -item[1]["calls"])
        ),
    }


def contract_defect_section(output_dir: str, expected: int) -> Dict[str, Any]:
    """跨章契约缺陷。契约模块读不了时只标这一区块不可用，其余照常出。"""
    try:
        from core.generation.planning_contract import (
            PlanningContractError,
            collect_contract_defects,
            load_planning_contracts,
        )

        contracts = load_planning_contracts(output_dir)
        defects = collect_contract_defects(contracts, total_chapters=expected or None)
    except Exception as exc:  # noqa: BLE001  少一个区块不该让整份诊断作废
        return {
            "available": False,
            "reason": f"契约文件无法读取：{exc}",
            "total": None,
            "codes": {},
        }

    codes: Dict[str, int] = {}
    for defect in defects:
        code = getattr(defect, "code", "") or "planning_contract_invalid"
        codes[code] = codes.get(code, 0) + 1
    return {
        "available": True,
        "reason": "",
        "contracts": len(contracts),
        "total": len(defects),
        "codes": dict(sorted(codes.items(), key=lambda item: -item[1])),
    }


def _expected_chapters(output_dir: str) -> int:
    try:
        from core.generation.workflow_status import expected_chapter_count

        return int(expected_chapter_count(output_dir) or 0)
    except Exception:  # noqa: BLE001
        return 0


def project_key(output_dir: str) -> str:
    """用来拒绝拿另一个项目的快照当基线。"""
    return os.path.normcase(os.path.realpath(output_dir))


def build_snapshot(output_dir: str) -> Dict[str, Any]:
    corpus = scan_reviews(output_dir)
    expected = _expected_chapters(output_dir)
    merged = corpus.merged()
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "project": output_dir,
        "project_key": project_key(output_dir),
        "corpus": {
            "expected_chapters": expected or None,
            "chapter_dirs": len(corpus.chapters),
            "review_files": corpus.file_count,
            "gate_reviews": len(corpus.all_reviews()),
            "merged_chapter_reviews": len(merged),
            "unreadable": corpus.unreadable,
        },
        "reviewers": reviewer_section(corpus),
        "gates": gate_section(corpus),
        "hard_failures": hard_failure_section(corpus),
        "planning_rejections": planning_rejection_section(output_dir),
        "contract_defects": contract_defect_section(output_dir, expected),
        "cost": cost_section(output_dir),
    }


# --- 结论 -------------------------------------------------------------------

VERDICT_TEXT = {
    "no_data": "这个项目还没有带分项的章节评审，无法诊断。",
    "reviewer_bottleneck": "拦下章节的是文学质量，不是硬规则——改写作提示词比加校验有用。",
    "rerun_dominant": "大半章节整段重跑过：重跑在补规划阶段本该做完的事，先看规划提示词。",
    "gate_retry_dominant": "这道闸门是主要开销，值得先看它的重修要求写清楚了没有。",
    "hard_failure_concentrated": "硬伤高度集中在一个码上，一条提示词规则就能压掉大半。",
    "planning_contract_rejections": "规划契约仍被判不合格：契约字段的语义没讲进提示词。",
    "clean": "四项指标都在阈值内。",
}


def decide_verdict(
    snapshot: Dict[str, Any], delta: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """一句能照着做的结论，按「你会先动手改哪个」排序，第一条命中即止。"""
    corpus = snapshot.get("corpus") or {}
    if not corpus.get("merged_chapter_reviews"):
        return _verdict("no_data", 2, "")

    reviewers = snapshot.get("reviewers") or {}
    worst = _worst(reviewers, "rate")
    if worst and (worst[1] or 0) >= 0.50:
        return _verdict(
            "reviewer_bottleneck", 1, "未通过", delta, worst[0], worst[1]
        )

    gates = snapshot.get("gates") or {}
    worst_rerun = _worst(gates, "rerun_rate")
    if worst_rerun and (worst_rerun[1] or 0) >= 0.50:
        return _verdict(
            "rerun_dominant", 1, "整章重跑", delta, worst_rerun[0], worst_rerun[1]
        )

    worst_retry = _worst(gates, "retry_rate")
    if worst_retry and (worst_retry[1] or 0) >= 0.50:
        return _verdict(
            "gate_retry_dominant", 1, "重修", delta, worst_retry[0], worst_retry[1]
        )

    hard = snapshot.get("hard_failures") or {}
    if (hard.get("top_share") or 0) >= 0.30 and hard.get("codes"):
        top = next(iter(hard["codes"]))
        return _verdict(
            "hard_failure_concentrated", 1, "占硬伤", delta, top, hard["top_share"]
        )

    planning = snapshot.get("planning_rejections") or {}
    if planning.get("available") and (planning.get("attempts") or 0) > 0:
        return _verdict(
            "planning_contract_rejections", 1, "次规划重试留档", delta,
            "", planning["attempts"],
        )

    return _verdict("clean", 0, "", delta)


def _verdict(
    code: str,
    exit_code: int,
    detail: str,
    delta: Optional[Dict[str, Any]] = None,
    subject: str = "",
    value: Optional[float] = None,
) -> Dict[str, Any]:
    """结论里只放机器标识与数字，中文标签留给排版层。

    ``subject`` 是分项名或闸门名这类原始标识，排版时才换成中文——标签一旦进了
    快照 JSON，改个措辞就会让历史对比对不上。
    """
    return {
        "code": code,
        "exit": exit_code,
        "subject": subject,
        "value": value,
        "detail": detail,
        "movement": _largest_movement(delta) if delta else "",
    }


def _worst(section: Dict[str, Any], key: str) -> Optional[Tuple[str, Optional[float]]]:
    candidates = [
        (name, entry.get(key))
        for name, entry in section.items()
        if isinstance(entry, dict) and entry.get(key) is not None
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[1])


# --- 快照落盘与对比 ---------------------------------------------------------

#: 只比这张表上的指标，不遍历整棵树。方向性写在这里而不是排版里，好直接测。
METRICS: Tuple[Tuple[str, str], ...] = (
    ("reviewers.*.rate", "lower_is_better"),
    ("gates.*.retry_rate", "lower_is_better"),
    ("gates.*.rerun_rate", "lower_is_better"),
    ("hard_failures.total", "lower_is_better"),
    ("planning_rejections.attempts", "lower_is_better"),
    ("corpus.merged_chapter_reviews", "context_only"),
    ("cost.calls", "context_only"),
)

#: 哪些指标是比率。排版层不能靠数值大小去猜：一个次数刚好变动 1，
#: 「小于等于 1 就当成比率」这条猜法会把它印成「100 点」。
RATE_METRICS = frozenset(
    {"reviewers.*.rate", "gates.*.retry_rate", "gates.*.rerun_rate"}
)

#: 每个比率对应的分母字段，用来判断变化是不是被分母带出来的。
DENOMINATORS = {
    "reviewers.*.rate": "reviewed",
    "gates.*.retry_rate": "chapters",
    "gates.*.rerun_rate": "chapters",
}


def snapshot_paths(output_dir: str) -> Tuple[str, str]:
    directory = os.path.join(output_dir, *REPORT_DIR)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return (
        os.path.join(directory, f"{SNAPSHOT_STEM}_{stamp}.json"),
        os.path.join(directory, f"{SNAPSHOT_STEM}_latest.json"),
    )


def read_previous(output_dir: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """读上一份快照当基线。永不抛异常，读不成就说清楚为什么。"""
    path = os.path.join(output_dir, *REPORT_DIR, f"{SNAPSHOT_STEM}_latest.json")
    if not os.path.isfile(path):
        return None, "首次运行，没有可对比的基线"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        return None, f"上一份快照读不了，跳过对比：{exc}"
    if not isinstance(payload, dict):
        return None, "上一份快照格式不对，跳过对比"
    if payload.get("schema") != SCHEMA:
        return None, f"上一份快照是旧格式（{payload.get('schema')}），跳过对比"
    if payload.get("project_key") and payload["project_key"] != project_key(output_dir):
        return None, "上一份快照来自另一个项目，跳过对比"
    return payload, ""


def write_snapshot(
    output_dir: str, snapshot: Dict[str, Any]
) -> Tuple[Optional[str], str]:
    """写带时间戳的快照，另存一份 latest。写不进去只报一句，不掩盖诊断结论。"""
    stamped, latest = snapshot_paths(output_dir)
    body = json.dumps(snapshot, ensure_ascii=False, indent=2)
    try:
        os.makedirs(os.path.dirname(stamped), exist_ok=True)
        with open(stamped, "w", encoding="utf-8") as handle:
            handle.write(body)
    except OSError as exc:
        return None, f"快照写入失败：{exc}；本次只打印不落盘"
    try:
        with open(latest, "w", encoding="utf-8") as handle:
            handle.write(body)
    except OSError as exc:
        return stamped, f"latest 副本写入失败：{exc}"
    return stamped, ""


def _dig(snapshot: Dict[str, Any], path: List[str]) -> Any:
    node: Any = snapshot
    for part in path:
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _expand(pattern: str, snapshot: Dict[str, Any]) -> List[str]:
    """把带通配的指标路径展开成具体路径。"""
    parts = pattern.split(".")
    if "*" not in parts:
        return [pattern]
    index = parts.index("*")
    parent = _dig(snapshot, parts[:index])
    if not isinstance(parent, dict):
        return []
    return [
        ".".join(parts[:index] + [name] + parts[index + 1:]) for name in parent
    ]


def diff_snapshots(
    previous: Dict[str, Any], current: Dict[str, Any]
) -> Dict[str, Any]:
    """按指标表比两份快照。分母变动过大的只给数字，不下判断。"""
    result: Dict[str, Any] = {}
    for pattern, polarity in METRICS:
        paths = sorted(set(_expand(pattern, previous)) | set(_expand(pattern, current)))
        for path in paths:
            parts = path.split(".")
            before = _dig(previous, parts)
            after = _dig(current, parts)
            if before is None and after is None:
                continue
            entry: Dict[str, Any] = {
                "before": before,
                "after": after,
                "polarity": polarity,
                "unit": "rate" if pattern in RATE_METRICS else "count",
            }
            if before is None:
                entry["direction"] = "new"
            elif after is None:
                entry["direction"] = "gone"
            else:
                entry["delta"] = round(after - before, 4)
                entry["direction"] = _direction(
                    entry["delta"], polarity,
                    _drifted(pattern, parts, previous, current),
                )
            result[path] = entry
    return result


def _direction(delta: float, polarity: str, drifted: bool) -> str:
    if polarity == "context_only":
        return "context"
    if drifted:
        return "incomparable"
    if abs(delta) < 1e-9:
        return "same"
    if polarity == "lower_is_better":
        return "better" if delta < 0 else "worse"
    return "worse" if delta < 0 else "better"


def _drifted(
    pattern: str,
    parts: List[str],
    previous: Dict[str, Any],
    current: Dict[str, Any],
) -> bool:
    """样本变动超过两成，这一项的变化就不是质量信号。

    比率看自己那一项的分母；计数没有分母，看整个语料的规模——少跑了几章，
    硬伤当然变少，那不叫好转。
    """
    field_name = DENOMINATORS.get(pattern)
    if field_name:
        base = _dig(previous, parts[:-1] + [field_name])
        now = _dig(current, parts[:-1] + [field_name])
    else:
        base = _dig(previous, ["corpus", "merged_chapter_reviews"])
        now = _dig(current, ["corpus", "merged_chapter_reviews"])
    if not base or now is None:
        return False
    return abs(now - base) / base > DENOMINATOR_DRIFT


def _largest_movement(delta: Dict[str, Any]) -> Dict[str, Any]:
    """变化最大的那一项。只回标识与数字，中文措辞留给排版层。"""
    ranked = [
        (path, entry)
        for path, entry in (delta or {}).items()
        if entry.get("direction") in {"better", "worse"} and entry.get("delta")
    ]
    if not ranked:
        return {}
    path, entry = max(ranked, key=lambda item: _movement_magnitude(item[1]))
    parts = path.split(".")
    # reviewers.reader_blind.rate -> reader_blind；hard_failures.total -> hard_failures
    subject = parts[-2] if len(parts) >= 3 else parts[0]
    return {
        "path": path,
        "subject": subject,
        "direction": entry["direction"],
        "delta": entry["delta"],
        "unit": entry.get("unit", "count"),
    }


def _movement_magnitude(entry: Dict[str, Any]) -> float:
    """把比率和次数换算到同一把尺上再排大小。

    直接比绝对值等于让次数永远胜出：硬失败多了 1 次的绝对值，比未通过率动了 4 个
    点大得多，于是「变化最大的那一项」永远指向次数。次数改用相对变化。
    """
    amount = abs(entry.get("delta") or 0)
    if entry.get("unit") == "rate":
        return amount
    before = entry.get("before") or 0
    return amount / max(abs(before), 1)
