"""Create and summarize a paired, reader-blind prose comparison.

Each participant sees both baseline and candidate excerpts, but the A/B order is
balanced independently for every item.  The private manifest is the only place
where source identities are stored.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, stdev
from typing import Any, Dict, Iterable, List, Sequence, Tuple


RATING_METRICS = (
    "continue_score",
    "plausibility_score",
    "character_score",
    "chinese_comfort_score",
    "ai_likeness_score",
)
RESPONSE_COLUMNS = (
    "participant_id",
    "item_id",
    "preferred_variant",
    *(f"{metric}_{variant}" for metric in RATING_METRICS for variant in ("a", "b")),
    "notes",
)


@dataclass(frozen=True)
class ExcerptPair:
    item_id: str
    baseline_path: Path
    candidate_path: Path
    baseline_text: str
    candidate_text: str


def _natural_key(path: Path) -> Tuple[Any, ...]:
    return tuple(int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name))


def _chapter_files(directory: Path) -> Dict[str, Path]:
    files = [path for path in directory.glob("*.md") if path.is_file()]
    return {path.name: path for path in sorted(files, key=_natural_key)}


def load_excerpt_pairs(
    baseline_dir: str | Path,
    candidate_dir: str | Path,
    *,
    excerpt_chars: int = 3000,
) -> List[ExcerptPair]:
    baseline = _chapter_files(Path(baseline_dir))
    candidate = _chapter_files(Path(candidate_dir))
    shared = [name for name in baseline if name in candidate]
    if not shared:
        raise ValueError("基线目录与改进目录没有同名 Markdown 章节")
    pairs: List[ExcerptPair] = []
    for index, name in enumerate(shared, 1):
        baseline_text = baseline[name].read_text(encoding="utf-8").strip()[:excerpt_chars]
        candidate_text = candidate[name].read_text(encoding="utf-8").strip()[:excerpt_chars]
        if not baseline_text or not candidate_text:
            raise ValueError(f"盲测材料不能为空：{name}")
        pairs.append(
            ExcerptPair(
                item_id=f"item_{index:03d}",
                baseline_path=baseline[name],
                candidate_path=candidate[name],
                baseline_text=baseline_text,
                candidate_text=candidate_text,
            )
        )
    return pairs


def _balanced_candidate_positions(
    participant_ids: Sequence[str], rng: random.Random
) -> Dict[str, str]:
    shuffled = list(participant_ids)
    rng.shuffle(shuffled)
    split = len(shuffled) // 2
    positions = {
        participant_id: ("a" if index < split else "b")
        for index, participant_id in enumerate(shuffled)
    }
    if len(shuffled) % 2:
        positions[shuffled[-1]] = rng.choice(("a", "b"))
    return positions


def create_blind_test(
    baseline_dir: str | Path,
    candidate_dir: str | Path,
    output_dir: str | Path,
    *,
    participants: int = 30,
    excerpt_chars: int = 3000,
    seed: int = 20260829,
) -> Dict[str, Any]:
    """Write participant packets, a private manifest, and a response template."""

    if participants < 2:
        raise ValueError("盲测至少需要 2 名读者")
    pairs = load_excerpt_pairs(
        baseline_dir, candidate_dir, excerpt_chars=max(200, int(excerpt_chars))
    )
    destination = Path(output_dir)
    packets_dir = destination / "packets"
    packets_dir.mkdir(parents=True, exist_ok=True)
    participant_ids = [f"reader_{index:03d}" for index in range(1, participants + 1)]
    rng = random.Random(seed)
    assignments: Dict[str, Dict[str, str]] = {reader: {} for reader in participant_ids}

    for pair in pairs:
        positions = _balanced_candidate_positions(participant_ids, rng)
        for reader, position in positions.items():
            assignments[reader][pair.item_id] = position

    pair_index = {pair.item_id: pair for pair in pairs}
    for reader in participant_ids:
        sections = [
            f"# 中文小说片段盲测（{reader}）",
            "",
            "请不要猜测文本来源。按第一阅读感受评分，所有 1–5 分中 5 表示最强；",
            "AI 感一项的 5 表示最像机器生成。每个条目读完后，把评分填入 responses.csv。",
        ]
        for item_id in sorted(pair_index):
            pair = pair_index[item_id]
            candidate_position = assignments[reader][item_id]
            version_a = pair.candidate_text if candidate_position == "a" else pair.baseline_text
            version_b = pair.baseline_text if candidate_position == "a" else pair.candidate_text
            sections.extend(
                [
                    "",
                    f"## {item_id}",
                    "",
                    "### 版本 A",
                    "",
                    version_a,
                    "",
                    "### 版本 B",
                    "",
                    version_b,
                ]
            )
        (packets_dir / f"{reader}.md").write_text("\n".join(sections), encoding="utf-8")

    manifest = {
        "schema_version": 1,
        "design": "paired_reader_blind_balanced_ab",
        "hypothesis": "改进版提高续读意愿，同时不降低可信度、人物真实感和中文舒适度，并降低 AI 感。",
        "primary_metric": "continue_score",
        "guardrail_metrics": [
            "plausibility_score",
            "character_score",
            "chinese_comfort_score",
        ],
        "diagnostic_metric": "ai_likeness_score (lower is better)",
        "target_readers": participants,
        "seed": seed,
        "items": {
            pair.item_id: {
                "baseline_path": str(pair.baseline_path.resolve()),
                "candidate_path": str(pair.candidate_path.resolve()),
            }
            for pair in pairs
        },
        "assignments": assignments,
    }
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "manifest.private.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (destination / "responses.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESPONSE_COLUMNS)
        writer.writeheader()
        for reader in participant_ids:
            for pair in pairs:
                writer.writerow({"participant_id": reader, "item_id": pair.item_id})
    (destination / "README.md").write_text(
        "# 盲测说明\n\n"
        "1. 每位读者只领取 `packets` 中与自己编号一致的文件。\n"
        "2. 在 `responses.csv` 填写 A/B 偏好及两版各项 1–5 分。\n"
        "3. 不要向读者提供 `manifest.private.json`。\n"
        "4. 至少收齐预注册的 30 名读者后再汇总，避免看到中途结果就停止。\n"
        "5. 汇总命令：`python -m core.evaluation.reader_blind_test summarize --test-dir <目录>`。\n",
        encoding="utf-8",
    )
    return manifest


def _score(row: Dict[str, str], metric: str, variant: str) -> float | None:
    raw = str(row.get(f"{metric}_{variant}", "")).strip()
    if not raw:
        return None
    value = float(raw)
    if value < 1 or value > 5:
        raise ValueError(f"{metric}_{variant} 必须在 1–5 之间")
    return value


def _exact_binomial_two_sided(successes: int, trials: int) -> float | None:
    if trials <= 0:
        return None
    observed = math.comb(trials, successes) / (2**trials)
    probability = sum(
        math.comb(trials, value) / (2**trials)
        for value in range(trials + 1)
        if math.comb(trials, value) / (2**trials) <= observed + 1e-15
    )
    return min(1.0, probability)


def _paired_summary(differences: List[float]) -> Dict[str, Any]:
    if not differences:
        return {"n": 0, "mean_difference": None, "ci95": None}
    effect = mean(differences)
    if len(differences) < 2:
        interval = None
    else:
        margin = 1.96 * stdev(differences) / math.sqrt(len(differences))
        interval = [round(effect - margin, 3), round(effect + margin, 3)]
    return {
        "n": len(differences),
        "mean_difference": round(effect, 3),
        "ci95": interval,
    }


def summarize_blind_test(test_dir: str | Path) -> Dict[str, Any]:
    """Unblind completed rows and calculate paired effects and preference wins."""

    directory = Path(test_dir)
    manifest = json.loads((directory / "manifest.private.json").read_text(encoding="utf-8"))
    with (directory / "responses.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assignments = manifest["assignments"]
    differences = {metric: [] for metric in RATING_METRICS}
    candidate_wins = baseline_wins = ties = 0
    completed_participants = set()
    completed_rows = 0

    for row in rows:
        reader = str(row.get("participant_id", "")).strip()
        item_id = str(row.get("item_id", "")).strip()
        if reader not in assignments or item_id not in assignments[reader]:
            continue
        candidate_position = assignments[reader][item_id]
        baseline_position = "b" if candidate_position == "a" else "a"
        row_has_scores = False
        for metric in RATING_METRICS:
            candidate = _score(row, metric, candidate_position)
            baseline = _score(row, metric, baseline_position)
            if candidate is None or baseline is None:
                continue
            row_has_scores = True
            # Lower AI-likeness is an improvement; every other metric is higher-is-better.
            difference = baseline - candidate if metric == "ai_likeness_score" else candidate - baseline
            differences[metric].append(difference)
        preferred = str(row.get("preferred_variant", "")).strip().lower()
        if preferred in {"a", "b", "tie"}:
            row_has_scores = True
            if preferred == "tie":
                ties += 1
            elif preferred == candidate_position:
                candidate_wins += 1
            else:
                baseline_wins += 1
        if row_has_scores:
            completed_rows += 1
            completed_participants.add(reader)

    decisive = candidate_wins + baseline_wins
    target = int(manifest.get("target_readers", 30))
    report = {
        "design": manifest.get("design"),
        "target_readers": target,
        "completed_readers": len(completed_participants),
        "completed_rows": completed_rows,
        "preference": {
            "candidate_wins": candidate_wins,
            "baseline_wins": baseline_wins,
            "ties": ties,
            "candidate_win_rate_excluding_ties": (
                round(candidate_wins / decisive, 4) if decisive else None
            ),
            "exact_binomial_p_value": _exact_binomial_two_sided(candidate_wins, decisive),
        },
        "paired_effects": {metric: _paired_summary(values) for metric, values in differences.items()},
        "warnings": [],
    }
    if len(completed_participants) < target:
        report["warnings"].append(
            f"只收齐 {len(completed_participants)}/{target} 名读者；未达到预注册样本，不作上线结论。"
        )
    if decisive < 20:
        report["warnings"].append("有效偏好票少于 20，偏好胜率只作方向性参考。")
    (directory / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="中文小说片段配对盲测")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="生成盲测材料")
    create.add_argument("--baseline-dir", required=True)
    create.add_argument("--candidate-dir", required=True)
    create.add_argument("--output-dir", required=True)
    create.add_argument("--participants", type=int, default=30)
    create.add_argument("--excerpt-chars", type=int, default=3000)
    create.add_argument("--seed", type=int, default=20260829)
    summarize = subparsers.add_parser("summarize", help="汇总已填写的 responses.csv")
    summarize.add_argument("--test-dir", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "create":
        result = create_blind_test(
            args.baseline_dir,
            args.candidate_dir,
            args.output_dir,
            participants=args.participants,
            excerpt_chars=args.excerpt_chars,
            seed=args.seed,
        )
        print(json.dumps({"items": len(result["items"]), "readers": result["target_readers"]}, ensure_ascii=False))
    else:
        print(json.dumps(summarize_blind_test(args.test_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
