"""Report what a project's quality gates are actually catching, and what it costs.

    python tools/diagnose_quality.py <项目目录> [用来对比的旧快照.json]

跑完打一份中文报告，同时把快照写进 quality/reports/，并自动跟上一次的快照比，
多印一列变化。给第二个参数就拿它当基线，并且不写盘——对比不该覆盖 latest。

退出码：0 四项指标都在阈值内 · 1 有值得动手的问题 · 2 目录不存在或无从诊断。
注意 2 不只表示路径错：一个没有任何带分项章节评审的项目同样返回 2，
因为「没有数据」和「干净」必须分得开，那正是这个工具要防的事。
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.evaluation.quality_snapshot import (  # noqa: E402
    build_snapshot,
    decide_verdict,
    diff_snapshots,
    read_previous,
    write_snapshot,
)

GATE_LABELS = {
    "plan": "场景规划",
    "scene_1": "第一场景",
    "scene_2": "第二场景",
    "scene_3": "第三场景",
    "scene_4": "第四场景",
    "chapter": "整章评审",
}

REVIEWER_LABELS = {
    "contract": "契约符合",
    "reader_blind": "读者盲读",
    "plausibility": "现实合理性",
    "continuity": "衔接",
}

CODE_LABELS = {
    "CONTINUITY_DUPLICATION": "重复上一章已交代过的",
    "UNSUPPORTED_PRECISION": "编造精确数字充当细节",
    "AI_TEMPLATE_SATURATION": "整段 AI 套话",
    "IMPOSSIBLE_MECHANISM": "现实里做不到的操作",
    "CHARACTER_LOGIC_BREAK": "人物违背自身设定",
    "PROCEDURAL_IMPOSSIBILITY": "司法程序上走不通",
    "TRUTH_CONTRADICTION": "与已确立的真相冲突",
    "NEXT_SCENE_PREMATURE": "提前做了下一场的事",
    "READER_CONFUSION": "读者读不明白",
    "KNOWLEDGE_LEAK": "人物知道了不该知道的",
}

DEFECT_LABELS = {
    "contract_node_not_found": "引用了不存在的叙事图节点",
    "primary_thread_update_required": "主线声明与记账对不上",
    "secondary_thread_update_required": "副线声明与记账对不上",
    "thread_action_before_open": "线还没打开就推进",
    "world_conflict": "写了世界观里没定义的东西",
    "planning_contract_invalid": "契约本身不合法",
    "transition_node_type_invalid": "转移用错了节点类型",
    "blocked_reveal_selected": "选了被封禁的揭示",
    "thread_closed_before_required_reveals": "该揭的还没揭就收线",
}

ACTIVITY_LABELS = {
    # 没有 "write"：它在 parse_trace_stage 里已归一到 write_review_accept。
    "write_review_accept": "写正文并评审验收",
    "scene_planning": "场景规划",
    "continuity_backfill": "契约补写",
    "structure_full": "故事结构",
    "lore_full": "世界设定",
    "未标注": "未标注",
}

DIRECTION_TEXT = {
    "better": "好转",
    "worse": "恶化",
    "same": "持平",
    "new": "新增",
    "gone": "已消失",
    "incomparable": "样本变动过大，不作判断",
    "context": "",
}


VERDICT_TEXT = {
    "no_data": "这个项目还没有带分项的章节评审，无法诊断。",
    "reviewer_bottleneck": "拦下章节的是文学质量，不是硬规则——改写作提示词比加校验有用。",
    "rerun_dominant": "重跑在补规划阶段本该做完的事，先看规划提示词。",
    "gate_retry_dominant": "这道闸门是主要开销，值得先看它的重修要求写清楚了没有。",
    "hard_failure_concentrated": "硬伤高度集中在一个码上，一条提示词规则就能压掉大半。",
    "planning_contract_rejections": "规划契约仍被判不合格：契约字段的语义没讲进提示词。",
    "clean": "四项指标都在阈值内。",
}


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.0%}"


def _pad(text: str, columns: int) -> str:
    """按显示宽度补空格。中文一个字占两列，用字符数对齐会歪。"""
    width = sum(2 if ord(char) > 0x2E80 else 1 for char in text)
    return text + " " * max(columns - width, 0)


SECTION_LABELS = {
    "hard_failures": "硬失败总数",
    "planning_rejections": "规划契约被拒次数",
    "corpus": "章节合议评审数",
    "cost": "调用次数",
}


def _label(name: str) -> str:
    for table in (REVIEWER_LABELS, GATE_LABELS, CODE_LABELS, SECTION_LABELS):
        if name in table:
            return table[name]
    return name


def _verdict_line(verdict: Dict[str, Any]) -> str:
    """结论句在这里拼。快照里只存标识与数字，中文留在排版层。"""
    body = VERDICT_TEXT.get(verdict.get("code", ""), "")
    subject = verdict.get("subject") or ""
    value = verdict.get("value")
    detail = verdict.get("detail") or ""
    if detail and value is not None:
        shown = f"{value:.0%}" if isinstance(value, float) and value <= 1 else f"{value}"
        head = f"{_label(subject)} {detail} {shown}" if subject else f"{shown} {detail}"
        body = f"{head}。{body}"
    movement = verdict.get("movement") or {}
    if movement:
        verb = DIRECTION_TEXT.get(movement.get("direction", ""), "")
        scale = _scale(movement)
        tail = f"{verb} {scale}".strip() if scale else "几乎持平"
        body = f"{body}　较上次：{_label(movement.get('subject', ''))} {tail}"
    return body


def _scale(entry: Dict[str, Any]) -> str:
    """变化幅度按它自己的单位印，不按数值大小猜。

    比率印「点」，次数印「次」。曾经靠「绝对值不超过 1 就当比率」去猜，硬失败从
    61 次变成 62 次就被印成「恶化 100 点」。四舍五入之后归零的，不印数字。
    """
    amount = abs(entry.get("delta") or 0)
    if entry.get("unit") == "rate":
        points = round(amount * 100)
        return f"{points} 点" if points else ""
    count = round(amount)
    return f"{count} 次" if count else ""


def _delta_note(delta: Optional[Dict[str, Any]], path: str) -> str:
    entry = (delta or {}).get(path)
    if not entry:
        return ""
    word = DIRECTION_TEXT.get(entry.get("direction", ""), "")
    if not word:
        return ""
    before = entry.get("before")
    if entry.get("direction") in {"new", "gone"}:
        return f"　← {word}"
    shown = _pct(before) if entry.get("unit") == "rate" else before
    if entry.get("direction") in {"same", "incomparable"}:
        return f"　← 上次 {shown}，{word}"
    scale = _scale(entry)
    if not scale:
        return f"　← 上次 {shown}，几乎持平"
    return f"　← 上次 {shown}，{word} {scale}"


def _delta_brief(delta: Optional[Dict[str, Any]], path: str) -> str:
    """同一行上挂着两个比率时用这个：对比紧跟在它说的那个数后面。

    箭头式后缀只能挂在行尾，读的人没法知道它指的是「重修」还是「重跑」——闸门那
    一行两个数都在动，挂错一个就是反着读。
    """
    note = _delta_note(delta, path)
    if not note:
        return ""
    return "（" + note.replace("　← ", "").replace("，", " ") + "）"


def render(
    snapshot: Dict[str, Any],
    delta: Optional[Dict[str, Any]],
    notes: List[str],
) -> List[str]:
    """把一份快照排成中文报告。只回字符串，不碰磁盘、不打印。"""
    out: List[str] = []
    corpus = snapshot.get("corpus") or {}
    out.append(f"项目：{snapshot.get('project')}")
    out.append(
        f"章节大纲宣告 {corpus.get('expected_chapters') or '未知'} 章，"
        f"留档评审 {corpus.get('review_files', 0)} 份，"
        f"其中带分项的章节合议 {corpus.get('merged_chapter_reviews', 0)} 份"
    )
    for note in notes:
        out.append(f"（{note}）")
    out.append("")

    reviewers = snapshot.get("reviewers") or {}
    if reviewers:
        out.append("各评审看同一批章节，各自的未通过率：")
        ranked = sorted(
            reviewers.items(), key=lambda item: -(item[1].get("rate") or 0)
        )
        for name, entry in ranked:
            label = REVIEWER_LABELS.get(name, name)
            out.append(
                f"  {_pad(label, 12)}{entry.get('not_passed', 0):>4} / "
                f"{entry.get('reviewed', 0):<4} {_pct(entry.get('rate')):>5}"
                f"{_delta_note(delta, f'reviewers.{name}.rate')}"
            )
        out.append("")

    gates = snapshot.get("gates") or {}
    if gates:
        out.append("各闸门重修与整章重跑的比例：")
        out.append("  （重修 = 原地再来一轮；重跑 = 整章流程从头再走一遍）")
        for name in sorted(gates):
            entry = gates[name]
            label = GATE_LABELS.get(name, name)
            out.append(
                f"  {_pad(label, 10)}共 {entry.get('chapters', 0):>3} 章　"
                f"重修 {_pct(entry.get('retry_rate')):>5}"
                f"{_delta_brief(delta, f'gates.{name}.retry_rate')}"
                f"　重跑 {_pct(entry.get('rerun_rate')):>5}"
                f"{_delta_brief(delta, f'gates.{name}.rerun_rate')}"
            )
        out.append("")

    hard = snapshot.get("hard_failures") or {}
    if hard.get("total"):
        out.append(
            f"硬失败共 {hard['total']} 次，涉及 {hard.get('chapters_affected', 0)} 章："
            f"{_delta_note(delta, 'hard_failures.total')}"
        )
        for code, count in list((hard.get("codes") or {}).items())[:6]:
            out.append(f"  {count:>3} 次  {CODE_LABELS.get(code, code)}（{code}）")
        out.append("")

    planning = snapshot.get("planning_rejections") or {}
    if planning.get("available") and planning.get("attempts"):
        out.append(
            f"规划契约被判不合格 {planning['attempts']} 次，"
            f"涉及 {planning.get('chapters', 0)} 章："
        )
        for code, count in list((planning.get("codes") or {}).items())[:6]:
            out.append(f"  {count:>3} 次  {DEFECT_LABELS.get(code, code)}")
        out.append("")
    elif not planning.get("available"):
        out.append(f"规划重试：{planning.get('reason')}")
        out.append("")

    contract = snapshot.get("contract_defects") or {}
    if contract.get("available") and contract.get("total"):
        out.append(f"跨章契约缺陷 {contract['total']} 处：")
        for code, count in list((contract.get("codes") or {}).items())[:6]:
            out.append(f"  {count:>3} 处  {DEFECT_LABELS.get(code, code)}")
        out.append("")
    elif not contract.get("available"):
        out.append(f"跨章契约：{contract.get('reason')}")
        out.append("")

    cost = snapshot.get("cost") or {}
    if cost.get("available"):
        out.append(
            f"调用成本：{cost.get('calls', 0)} 次，"
            f"失败 {cost.get('failed_calls', 0)} 次，"
            f"耗时 {(cost.get('elapsed_ms') or 0) / 60000:.0f} 分"
        )
        out.append(f"  {cost.get('note')}")
        for activity, entry in list((cost.get("by_activity") or {}).items())[:8]:
            out.append(
                f"  {_pad(ACTIVITY_LABELS.get(activity, activity), 26)}"
                f"{entry.get('calls', 0):>4} 次　{_pct(entry.get('share')):>5}"
            )
        out.append("")
    else:
        out.append(f"调用成本：{cost.get('reason')}")
        out.append("")

    unreadable = corpus.get("unreadable") or []
    if unreadable:
        out.append(f"⚠ {len(unreadable)} 份评审无法读取，已跳过：")
        for item in unreadable[:3]:
            out.append(f"  · {item.get('path')}　{item.get('error')}")
        out.append("")

    out.append(f"结论：{_verdict_line(snapshot.get('verdict') or {})}")
    return out


def main(output_dir: str, previous_path: Optional[str] = None) -> int:
    if not os.path.isdir(output_dir):
        print(f"找不到目录：{output_dir}")
        return 2

    # 基线必须在写盘之前读：先写 latest 再读，等于每次拿自己跟自己比。
    notes: List[str] = []
    if previous_path:
        previous, reason = _load_explicit(previous_path)
    else:
        previous, reason = read_previous(output_dir)
    if reason:
        notes.append(reason)

    snapshot = build_snapshot(output_dir)
    delta = diff_snapshots(previous, snapshot) if previous else None
    snapshot["verdict"] = decide_verdict(snapshot, delta)

    if previous_path:
        notes.append("指定了对比基线，本次不写快照")
    else:
        path, write_note = write_snapshot(output_dir, snapshot)
        if write_note:
            notes.append(write_note)
        elif path:
            notes.append(f"已存快照：{os.path.relpath(path, output_dir)}")

    print("\n".join(render(snapshot, delta, notes)))
    return int(snapshot["verdict"].get("exit", 1))


def _load_explicit(path: str):
    import json

    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        return None, f"指定的基线读不了，跳过对比：{exc}"
    if not isinstance(payload, dict):
        return None, "指定的基线格式不对，跳过对比"
    return payload, ""


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        print(__doc__)
        raise SystemExit(2)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    raise SystemExit(main(*sys.argv[1:3]))
