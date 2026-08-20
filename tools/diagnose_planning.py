"""Report every cross-chapter defect in a project's scene planning.

Run this before deciding whether to repair a project or re-plan it:

    python tools/diagnose_planning.py <项目目录>

The workflow panel shows the first blocking defect; this shows all of them,
grouped by kind, with the chapters each repair would rewrite.  A handful of
defects is worth repairing in place; a defect in most chapters means the plan
was produced before the contract gates existed and is cheaper to regenerate.
"""

from __future__ import annotations

import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.generation.planning_contract import (  # noqa: E402
    PlanningContractError,
    collect_contract_defects,
    load_planning_contracts,
)
from core.generation.workflow_status import (  # noqa: E402
    assess_workflow,
    expected_chapter_count,
)

KIND_LABELS = {
    "missing_chapter_contract": "缺少章节契约",
    "thread_closed_before_open": "了结了没人埋下的悬念",
    "thread_missing_closure": "悬念到期仍无了结安排",
    "thread_closes_late": "了结晚于自己承诺的截止章",
    "thread_reopened": "同一条悬念被重复埋下",
    "unknown_fact_reference": "引用了尚未引入的事实",
    "fact_definition_conflict": "同一事实出现冲突定义",
    "planning_contract_invalid": "契约本身不合法",
}


def main(output_dir: str) -> int:
    if not os.path.isdir(output_dir):
        print(f"找不到目录：{output_dir}")
        return 2

    expected = expected_chapter_count(output_dir)
    try:
        contracts = load_planning_contracts(output_dir)
    except PlanningContractError as exc:
        print(f"契约文件无法读取，必须先处理：{exc}")
        return 1

    print(f"项目：{output_dir}")
    print(f"章节大纲宣告 {expected or '未知'} 章，磁盘上有 {len(contracts)} 份章节契约\n")

    for name, label in (
        ("lore", "设定生成"), ("structure", "故事结构"),
        ("scenes", "场景规划"), ("chapters", "章节写作"),
    ):
        status = assess_workflow(output_dir).get(name)
        if status:
            print(f"  {label}：{status.state} — {status.detail}")
    print()

    defects = collect_contract_defects(contracts, total_chapters=expected or None)
    if not defects:
        print("跨章校验没有发现问题，可以直接进入写作阶段。")
        return 0

    by_kind: Counter[str] = Counter()
    touched: dict[str, set[int]] = defaultdict(set)
    for defect in defects:
        by_kind[defect.code] += 1
        for chapter in defect.chapters:
            touched[defect.code].add(chapter)

    repair_targets = {defect.chapters[0] for defect in defects if defect.chapters}
    print(f"共 {len(defects)} 处矛盾，涉及 {len(repair_targets)} 个待重写章节：\n")
    for code, count in by_kind.most_common():
        label = KIND_LABELS.get(code, code)
        chapters = "、".join(str(number) for number in sorted(touched[code])[:12])
        print(f"  {count:>3} 处  {label}（{code}）")
        print(f"         涉及章节：{chapters}")

    print("\n前 20 条明细：")
    for defect in defects[:20]:
        print(f"  · [{defect.code}] {defect}")
    if len(defects) > 20:
        print(f"  …… 另有 {len(defects) - 20} 条")

    share = len(repair_targets) / expected if expected else 0
    print()
    if expected and share >= 0.5:
        print(
            f"待重写章节占全书 {share:.0%}。逐章修复不会比重新规划便宜，"
            "建议只保留章节大纲，重新规划场景。"
        )
    else:
        print(
            "待重写章节是少数，建议直接重跑“规划场景”让修复循环就地修，"
            "不要删除已有文件。"
        )
    return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
