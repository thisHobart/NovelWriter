# -*- coding: utf-8 -*-
"""在 current_work 的副本上连写几章，量化衔接与套语，不碰已定稿的项目。

改衔接只能靠真实成章验证：评审提示词能单独试跑，但「第 23 章的开头到底接不接得上
第 22 章」必须真的写一遍才知道。这个脚本把整件事做成可重复的一条命令：

    python tools/run_sandbox_chapters.py --reset --chapters 23-25
    python tools/run_sandbox_chapters.py --reset --chapters 23-25 --scene-dialogue off
    python tools/run_sandbox_chapters.py --report-only --sandbox sandbox_run

它做四件事：把 current_work 复制到副本；给还没有衔接字段的旧契约补上它（只问那三
个字段，不重写已经通过验收的场景规划）；逐章写作并验收；最后把每一章是否一次过、
重修几轮、有没有放行或待复审、以及相邻章开头的共用套语条数写成一份报告。

原始回复照旧存档：调用轨迹在 <副本>/system/quality_runs/<run_id>/llm_calls.jsonl，
评审记录在 <副本>/quality/legal_suspense_reviews/。跑坏了先看存档。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents.writing.chapter_writing_agent import ChapterWritingAgent  # noqa: E402
from core.generation import pending_review  # noqa: E402
from core.generation.ai_helper import set_backend  # noqa: E402
from core.generation.chapter_continuity import (  # noqa: E402
    CONTINUITY_KEY,
    hand_off_anchor_finding,
    opening_boilerplate_finding,
    protected_terms,
)
from core.generation.llm_trace import trace_session, trace_stage  # noqa: E402
from core.generation.planning_contract import load_planning_contracts  # noqa: E402
from core.generation.scene_pipeline import ScenePipeline  # noqa: E402
from core.generation.stage_context import make_context  # noqa: E402
from core.generation.stage_pipeline import GenerationHost, load_stage_parameters  # noqa: E402
from core.generation.story_ledger import StoryLedgerManager  # noqa: E402


MODEL = "hosted-llm"
BACKEND = "api"


def parse_chapters(text: str) -> List[int]:
    if "-" in text:
        start, _, end = text.partition("-")
        return list(range(int(start), int(end) + 1))
    return [int(part) for part in text.split(",") if part.strip()]


def reset_sandbox(source: Path, sandbox: Path) -> None:
    if sandbox.exists():
        shutil.rmtree(sandbox)
    # 归档目录只是历史存档，复制它会让每次重置多花几分钟却毫无用处。
    shutil.copytree(
        source, sandbox, ignore=shutil.ignore_patterns("archive", "__pycache__")
    )
    logging.info("Copied %s to %s", source, sandbox)


def set_parameter(sandbox: Path, key: str, value: str) -> None:
    path = sandbox / "system" / "parameters.txt"
    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith(f"{key}:")
    ]
    lines.append(f"{key}: {value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def chapter_path(sandbox: Path, chapter: int) -> Path:
    return sandbox / "story" / "content" / "chapters" / f"chapter_{chapter}.md"


def accepted_chapters(sandbox: Path) -> List[int]:
    ledger = StoryLedgerManager(str(sandbox)).load_suspense_ledger()
    return sorted(
        int(item["chapter"])
        for item in ledger.get("accepted_chapters", [])
        if isinstance(item, dict) and item.get("chapter")
    )


def boilerplate_between(sandbox: Path, chapter: int) -> Dict[str, Any]:
    """本章开头与上一章开头之间的共用套语。"""
    current, previous = chapter_path(sandbox, chapter), chapter_path(sandbox, chapter - 1)
    if not current.is_file() or not previous.is_file():
        return {
            "count": None,
            "fragments": [],
            "shared_chars": None,
            "hand_off_anchors": None,
        }
    try:
        contracts = load_planning_contracts(str(sandbox))
    except Exception:  # noqa: BLE001  统计不该因为一份坏契约就整个断掉
        contracts = []
    protected = protected_terms(
        StoryLedgerManager(str(sandbox)).load_suspense_ledger(), contracts
    )
    prose = current.read_text(encoding="utf-8")
    previous_prose = previous.read_text(encoding="utf-8")
    finding = opening_boilerplate_finding(prose, previous_prose, protected=protected)
    # 两个方向要一起看：和上一章开头重合是重新布景，和上一章结尾重合才是承接。
    anchor = hand_off_anchor_finding(prose, previous_prose, protected=protected)
    return {
        "count": finding["count"],
        "shared_chars": finding["shared_chars"],
        "longest_fragment_chars": finding["longest_fragment_chars"],
        "fragments": finding["fragments"],
        "exceeded": finding["exceeded"],
        "hand_off_anchors": anchor["count"],
        "hand_off_anchor_list": anchor["anchors"][:8],
    }


def hand_off_of(sandbox: Path, chapter: int) -> Dict[str, Any]:
    manager = StoryLedgerManager(str(sandbox))
    matches = sorted((sandbox / "story" / "planning").rglob(f"*_ch{chapter}.md"))
    markdown = matches[0].read_text(encoding="utf-8") if matches else ""
    contract = manager.load_contract(chapter, markdown) or {}
    return {
        "chapter_function": contract.get("chapter_function"),
        CONTINUITY_KEY: contract.get(CONTINUITY_KEY, {}),
    }


REVIEW_STAMP = re.compile(r"^(?P<stage>.+)_\d{8}_\d{6}_\d+$")


def review_dir(sandbox: Path, chapter: int) -> Path:
    return sandbox / "quality" / "legal_suspense_reviews" / f"chapter_{chapter}"


def review_stages(sandbox: Path, chapter: int) -> List[str]:
    """这一章留下的每一份评审记录属于哪个阶段，按写入顺序。

    文件名是「阶段_时间戳」，直接用前缀 glob 会让 chapter 把 chapter_retry_1
    也算进去——那正是「章节级重修跑了几轮」这个数字被算错的原因。
    """
    directory = review_dir(sandbox, chapter)
    if not directory.is_dir():
        return []
    stages = []
    for path in sorted(directory.glob("*.json")):
        match = REVIEW_STAMP.match(path.stem)
        if match:
            stages.append(match.group("stage"))
    return stages


def latest_review(sandbox: Path, chapter: int, stage: str) -> Dict[str, Any]:
    directory = review_dir(sandbox, chapter)
    if not directory.is_dir():
        return {}
    candidates = sorted(
        path
        for path in directory.glob(f"{stage}_*.json")
        if (match := REVIEW_STAMP.match(path.stem)) and match.group("stage") == stage
    )
    if not candidates:
        return {}
    try:
        return json.loads(candidates[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_chapter(sandbox: Path, chapter: int) -> Dict[str, Any]:
    agent = ChapterWritingAgent(
        str(sandbox), app_instance=None, use_new_structure=True, model=MODEL
    )
    infos, _ = agent.analyze_chapter_structure()
    info = next((item for item in infos if item.chapter_number == chapter), None)
    if info is None:
        raise RuntimeError(f"第 {chapter} 章不在已解析的结构里")
    with trace_stage(f"chapter_{chapter}_write"):
        result = agent._write_single_chapter(info)
    return {
        "success": bool(result.success),
        "messages": list(result.messages or []),
        "data": result.data,
    }


def approve_declared_contradictions(sandbox: Path, chapter: int, note: str) -> List[str]:
    """替作者裁定本章声明的事实反转，让无人值守的成章验证能跑完。

    推翻已经写进账本的设定只有人能拍板，闸门为此专门停下来问——这在产品里是对的。
    副本里的验证跑没有人坐在界面前，所以这里把裁定显式记下来：批准了哪几条、理由
    是什么，都写进账本和报告，而不是把闸门放宽。
    """
    manager = StoryLedgerManager(str(sandbox))
    contract = manager.load_contract(chapter) or {}
    approved = []
    for record in contract.get("facts_contradicted", []) or []:
        record_id = str((record or {}).get("id", "")).strip()
        if not record_id:
            continue
        manager.approve_contradiction(chapter, record_id, note)
        approved.append(record_id)
    return approved


def run(
    sandbox: Path,
    chapters: List[int],
    run_id: str,
    *,
    approve_contradictions: bool = False,
) -> Dict[str, Any]:
    parameters = load_stage_parameters(str(sandbox))
    host = GenerationHost(
        make_context(str(sandbox), MODEL, parameters),
        logging.getLogger("sandbox.scene_planning"),
    )
    pipeline = ScenePipeline(host)

    report: Dict[str, Any] = {
        "run_id": run_id,
        "sandbox": str(sandbox),
        "scene_dialogue": parameters.get("Scene Dialogue", "on"),
        "started_at": datetime.now().isoformat(),
        "chapters": {},
    }
    for chapter in chapters:
        entry: Dict[str, Any] = {"chapter": chapter}
        calls_before = _call_count()
        try:
            with trace_stage(f"chapter_{chapter}_continuity_backfill"):
                entry["hand_off_backfilled"] = pipeline.backfill_chapter_continuity(
                    str(sandbox), chapter, MODEL, parameters
                )
            entry["backfill_calls"] = _call_count() - calls_before
            if approve_contradictions:
                entry["approved_contradictions"] = approve_declared_contradictions(
                    sandbox,
                    chapter,
                    f"成章验证跑（{run_id}）预先裁定：本章契约声明的反转按“采纳本章”处理",
                )
            written = write_chapter(sandbox, chapter)
            entry.update(written)
        except Exception as exc:  # noqa: BLE001  一章写崩了也要把这一章的现场留下
            entry["success"] = False
            entry["error"] = f"{type(exc).__name__}: {exc}"
            logging.exception("Chapter %s failed", chapter)

        entry["calls"] = _call_count() - calls_before
        entry["accepted"] = chapter in accepted_chapters(sandbox)
        entry["pending_review"] = pending_review.load(str(sandbox), chapter) is not None
        entry["hand_off"] = hand_off_of(sandbox, chapter)
        entry["opening_reuse"] = boilerplate_between(sandbox, chapter)
        chapter_review = latest_review(sandbox, chapter, "chapter")
        entry["chapter_review"] = {
            "passed": chapter_review.get("passed"),
            "waived": chapter_review.get("waived"),
            "average_score": chapter_review.get("average_score"),
            "hard_failures": [
                item.get("code") for item in chapter_review.get("hard_failures", [])
            ],
        }
        entry["continuity_metrics"] = latest_review(sandbox, chapter, "continuity").get(
            "metrics", {}
        )
        stages = review_stages(sandbox, chapter)
        entry["chapter_retries"] = sum(
            1 for stage in stages if re.fullmatch(r"chapter_retry_\d+", stage)
        )
        entry["plan_retries"] = sum(
            1 for stage in stages if re.fullmatch(r"plan_retry_\d+", stage)
        )
        entry["scene_retries"] = sum(
            1 for stage in stages if re.fullmatch(r"scene_\d+_retry_\d+", stage)
        )
        entry["waived_reviews"] = sorted(
            stage for stage in stages if "waived" in stage
        )
        report["chapters"][str(chapter)] = entry
        _save(sandbox, run_id, report)
        if not entry.get("success"):
            logging.error("Stopping: chapter %s did not finish", chapter)
            break
    report["finished_at"] = datetime.now().isoformat()
    _save(sandbox, run_id, report)
    return report


_TRACE: Dict[str, Any] = {"recorder": None}


def _call_count() -> int:
    recorder = _TRACE.get("recorder")
    return len(recorder.calls) if recorder else 0


def _save(sandbox: Path, run_id: str, report: Dict[str, Any]) -> Path:
    directory = sandbox / "system" / "quality_runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "sandbox_report.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def measure_openings(project: Path, chapters: List[int]) -> str:
    """量一段已有章节的开头套语现状，作为改动前后的对照基线。

    不调用模型，也不改任何东西；`current_work` 上直接跑就是「现状」那一列数字。
    """
    lines = [f"项目：{project}", "相邻两章开头共用的套语片段（长度≥5 汉字，已剔除人名地名）", ""]
    total_count = 0
    total_chars = 0
    total_anchors = 0
    pairs = 0
    for chapter in chapters:
        finding = boilerplate_between(project, chapter)
        if finding["count"] is None:
            lines.append(f"{chapter - 1}~{chapter}：缺章节文件，跳过")
            continue
        pairs += 1
        total_count += finding["count"]
        total_chars += finding["shared_chars"]
        total_anchors += finding["hand_off_anchors"]
        lines.append(
            "{a}~{b}：套语 {count} 条 / {chars} 字 / 最长 {longest} 字{gate}"
            "；承接锚点 {anchors} 处  {frags}".format(
                a=chapter - 1,
                b=chapter,
                count=finding["count"],
                chars=finding["shared_chars"],
                longest=finding["longest_fragment_chars"],
                gate="【超标】" if finding["exceeded"] else "",
                anchors=finding.get("hand_off_anchors"),
                frags="、".join(f"「{item}」" for item in finding["fragments"]),
            )
        )
    if pairs:
        lines.append("")
        lines.append(
            f"合计 {pairs} 对：套语 {total_count} 条 / {total_chars} 字"
            f"（平均每对 {total_count / pairs:.1f} 条）；"
            f"承接锚点 {total_anchors} 处（平均每对 {total_anchors / pairs:.1f} 处）"
        )
    return "\n".join(lines)


def compare_runs(specs: List[str]) -> str:
    """把几轮成章验证摆在一起比。

    改一处提示词到底有没有用，只有把改前改后的同样三章放在同一张表里才看得出来：
    每一轮的报告都留在它自己的副本里，这里只负责读出来对齐。参数写成
    `副本目录:run_id`，例如 `sandbox_run:round2_dialogue_on`。
    """
    lines = ["轮次 | 章 | 验收 | 章节重修 | 放行 | 待复审 | 调用 | 开头套语 | 承接锚点"]
    for spec in specs:
        sandbox_name, _, run_id = spec.partition(":")
        sandbox = Path(sandbox_name).resolve()
        path = sandbox / "system" / "quality_runs" / run_id / "sandbox_report.json"
        if not path.is_file():
            lines.append(f"{run_id} | 找不到报告：{path}")
            continue
        report = json.loads(path.read_text(encoding="utf-8"))
        total = 0
        for key in sorted(report["chapters"], key=int):
            entry = report["chapters"][key]
            total += entry.get("calls", 0)
            # 套语与锚点一律现算：早先几轮跑的时候统计口径还没定下来。
            reuse = boilerplate_between(sandbox, int(key))
            lines.append(
                "{run} | {chapter} | {accepted} | {retries} 轮 | {waived} | {pending} | "
                "{calls} | {count} 条 / {chars} 字 | {anchors} 处".format(
                    run=run_id,
                    chapter=key,
                    accepted="通过" if entry.get("accepted") else "未过",
                    retries=entry.get("chapter_retries", 0),
                    waived="有" if entry.get("waived_reviews") else "无",
                    pending="有" if entry.get("pending_review") else "无",
                    calls=entry.get("calls", 0),
                    count=reuse.get("count"),
                    chars=reuse.get("shared_chars"),
                    anchors=reuse.get("hand_off_anchors"),
                )
            )
        lines.append(f"{run_id} 合计调用：{total}")
        lines.append("")
    return "\n".join(lines)


def summarize(report: Dict[str, Any]) -> str:
    lines = [
        f"run_id: {report['run_id']}   Scene Dialogue: {report['scene_dialogue']}",
        "章 | 验收 | 一次过 | 章节重修 | 放行 | 待复审 | 调用 | 开头套语 | 承接锚点",
    ]
    for key in sorted(report["chapters"], key=int):
        entry = report["chapters"][key]
        reuse = entry.get("opening_reuse", {})
        lines.append(
            "{chapter:>2} | {accepted:^4} | {clean:^6} | {retries:^8} | {waived:^4} | "
            "{pending:^6} | {calls:>4} | {count} 条 / {chars} 字 | {anchors} 处 {frags}".format(
                chapter=key,
                accepted="是" if entry.get("accepted") else "否",
                clean="是" if entry.get("chapter_retries") == 0 else "否",
                retries=entry.get("chapter_retries", 0),
                waived="有" if entry.get("waived_reviews") else "无",
                pending="有" if entry.get("pending_review") else "无",
                calls=entry.get("calls", 0),
                count=reuse.get("count"),
                chars=reuse.get("shared_chars"),
                anchors=reuse.get("hand_off_anchors"),
                frags="、".join(reuse.get("fragments", [])[:5]),
            )
        )
        hand_off = entry.get("hand_off", {}).get(CONTINUITY_KEY, {})
        lines.append(
            f"   chapter_function={entry.get('hand_off', {}).get('chapter_function')}"
            f"  承接={hand_off.get('picks_up_from', '')!r}"
            f"  时间差={hand_off.get('time_gap', '')!r}"
        )
        if entry.get("error"):
            lines.append(f"   错误：{entry['error']}")
        if entry.get("messages"):
            lines.append("   " + "；".join(entry["messages"][:3]))
    return "\n".join(lines)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="current_work")
    parser.add_argument("--sandbox", default="sandbox_run")
    parser.add_argument("--chapters", default="23-25")
    parser.add_argument("--reset", action="store_true", help="重新从 source 复制副本")
    parser.add_argument("--scene-dialogue", choices=("on", "off"), default=None)
    parser.add_argument("--report-only", action="store_true", help="只重算报告，不调用模型")
    parser.add_argument(
        "--compare",
        nargs="+",
        metavar="副本目录:run_id",
        help="把几轮跑的结果摆在一起比（不调用模型），例如 sandbox_run:round2_dialogue_on",
    )
    parser.add_argument(
        "--measure-openings",
        metavar="PROJECT",
        help="只量这个项目里相邻章开头的套语现状（不调用模型，不改任何东西）",
    )
    parser.add_argument(
        "--approve-contradictions",
        action="store_true",
        help="预先按“采纳本章”裁定契约声明的事实反转（无人值守跑必需；会记进账本）",
    )
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    sandbox = Path(args.sandbox).resolve()
    chapters = parse_chapters(args.chapters)
    if args.compare:
        print(compare_runs(args.compare))
        return 0
    if args.measure_openings:
        print(measure_openings(Path(args.measure_openings).resolve(), chapters))
        return 0
    run_id = args.run_id or "sandbox_{}_{}".format(
        args.chapters.replace(",", "_"), args.scene_dialogue or "default"
    )

    if args.reset:
        reset_sandbox(Path(args.source).resolve(), sandbox)
    if not sandbox.is_dir():
        parser.error(f"副本不存在：{sandbox}（第一次跑请加 --reset）")
    if args.scene_dialogue:
        set_parameter(sandbox, "Scene Dialogue", args.scene_dialogue)

    if args.report_only:
        path = sandbox / "system" / "quality_runs" / run_id / "sandbox_report.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        print(summarize(report))
        return 0

    set_backend(BACKEND, MODEL)
    with trace_session(sandbox, run_id=run_id, mode="sandbox") as recorder:
        _TRACE["recorder"] = recorder
        report = run(
            sandbox,
            chapters,
            run_id,
            approve_contradictions=args.approve_contradictions,
        )
    print(summarize(report))
    print(f"\n报告：{_save(sandbox, run_id, report)}")
    return 0 if all(
        entry.get("accepted") for entry in report["chapters"].values()
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
