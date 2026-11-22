"""Run the frozen storyboard retrieval benchmark from this worktree."""

from argparse import ArgumentParser
from collections import Counter
import hashlib
import json
from pathlib import Path
import platform
import subprocess
from time import perf_counter_ns

from evidence_pack import build_evidence, pack_evidence
from experiment_ranker import METHOD, SETTINGS, rank_turns
from history_retrieval import Message, find_turn_by_message_id

HERE = Path(__file__).resolve().parent
TOP_K = 3
CONTEXT_CHARS = 6000


def turn_number(turn_id: str) -> int:
    return int(turn_id[1:])


def searchable_text(messages: list[Message]) -> str:
    paragraphs = []
    for message in messages:
        prose, _, blocks = message["content"].partition("\n\n```")
        paragraphs.append(prose)
        if message["role"] == "program" and blocks.startswith("applied_diff\n"):
            changes = json.loads(blocks.split("\n", 1)[1].split("\n```", 1)[0])
            paragraphs.extend(
                f"第 {row['镜号']} 镜 {row['字段']} {row['新值']}" for row in changes
            )
    return "\n".join(paragraphs)


def run_query(
    query: dict,
    texts: dict[str, str],
    evidence: dict[str, dict[str, str]],
    message_turns: dict[str, str],
) -> dict:
    visible = {key: value for key, value in texts.items() if turn_number(key) <= query["through_turn"]}
    start = perf_counter_ns()
    ranked = rank_turns(query["question"], visible)
    elapsed_us = (perf_counter_ns() - start) / 1000
    top = ranked[:TOP_K]

    selected = pack_evidence([key for key, _, _ in top], evidence, CONTEXT_CHARS)
    packed = [item.turn_id for item in selected]
    used = len("\n\n".join(item.text for item in selected))

    required = query["required_message_ids"]
    # Gold labels are used only for scoring once ranking and packing are complete.
    gold_turns = [message_turns[message_id] for message_id in required]
    assert all(key in visible for key in gold_turns), query["id"]
    ranks = {key: rank for rank, (key, _, _) in enumerate(ranked, 1)}
    return {
        **query,
        "gold_ranks": {m: ranks.get(t) for m, t in zip(required, gold_turns, strict=True)},
        "ranked_count": len(ranked),
        "top": [
            {"turn_id": key, "score": score, "matched_features": features}
            for key, score, features in top
        ],
        "packed_turn_ids": packed,
        "packed_formats": {item.turn_id: item.form for item in selected},
        "evidence_chars": used,
        "covered": all(key in packed for key in gold_turns) if required else None,
        "false_positive": bool(packed) if not required else None,
        "ranking_us": round(elapsed_us, 2),
    }


def verify_references(history: list[Message]) -> dict:
    known = find_turn_by_message_id(history, "m38")
    prefix = [m for m in history if turn_number(m["turn_id"]) <= 48]
    return {
        "known_ref_m38": [m["id"] for m in known],
        "unknown_ref_returns_empty": not find_turn_by_message_id(history, "missing-id"),
        "future_ref_m98_returns_empty_at_t48": not find_turn_by_message_id(prefix, "m98"),
    }


def git_commit() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=HERE, text=True).strip()


def main() -> None:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, help="Override the built-in history.")
    parser.add_argument("--queries", type=Path, help="Override the frozen query list.")
    parser.add_argument("--output", type=Path, help="Override the result path.")
    options = parser.parse_args()
    history_text = (options.history or HERE / "history.json").read_text(encoding="utf-8")
    queries_text = (options.queries or HERE / "queries.jsonl").read_text(encoding="utf-8")
    history = json.loads(history_text)
    queries = [json.loads(line) for line in queries_text.splitlines() if line]
    turns: dict[str, list[Message]] = {}
    for message in history:
        turns.setdefault(message["turn_id"], []).append(message)
    texts = {key: searchable_text(messages) for key, messages in turns.items()}
    evidence = {key: build_evidence(messages) for key, messages in turns.items()}
    message_turns = {m["id"]: m["turn_id"] for m in history}
    rows = [run_query(q, texts, evidence, message_turns) for q in queries]
    summary = {
        group: {
            "queries": sum(row["group"] == group for row in rows),
            "covered": sum(row["group"] == group and row["covered"] is True for row in rows),
            "false_positives": sum(
                row["group"] == group and row["false_positive"] is True for row in rows
            ),
        }
        for group in ("near", "late", "no_evidence")
    }
    result = {
        "method": METHOD,
        "code_commit": git_commit(),
        "python": platform.python_version(),
        "dataset": {
            "turns": len(turns),
            "roles": dict(Counter(m["role"] for m in history)),
            "history_sha256_utf8_lf": hashlib.sha256(history_text.encode("utf-8")).hexdigest(),
            "queries_sha256_utf8_lf": hashlib.sha256(queries_text.encode("utf-8")).hexdigest(),
        },
        "parameters": {
            "top_k": TOP_K,
            "history_evidence_chars": CONTEXT_CHARS,
            "evidence_unit": "complete_turn_or_compact_revision",
            "scope": "one conversation; completed turns at or before each query cutoff",
            "score": SETTINGS,
        },
        "references": verify_references(history) if options.history is None else {},
        "summary": summary,
        "queries": rows,
    }
    default_file = "experiment_holdout_result.json" if options.history else "experiment_result.json"
    output_path = options.output or HERE / default_file
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(METHOD)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"逐题结果已保存：{output_path.name}")


if __name__ == "__main__":
    main()
