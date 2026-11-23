"""Run the frozen storyboard retrieval benchmark from this worktree."""

from argparse import ArgumentParser
from collections import Counter
import hashlib
import json
from math import ceil
from pathlib import Path
import platform
from statistics import median
import subprocess
from time import perf_counter_ns

from evidence_pack import build_evidence, compact_revision, message_block, pack_evidence
from experiment_ranker import METHOD, SETTINGS, rank_questions
from history_retrieval import Message, find_turn_by_message_id
from revision_evidence import RevisionEvidence, revision_evidence
from structured_search import METHOD as PURE_METHOD, SETTINGS as PURE_SETTINGS
from structured_search import Record, rank_structured, record_from_message

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


def rank_all(
    queries: list[dict], texts: dict[str, str], revisions: dict[str, RevisionEvidence]
) -> dict[str, tuple[list, float]]:
    groups = {}
    for query in queries:
        groups.setdefault(query["through_turn"], []).append(query)
    result = {}
    for cutoff, group in groups.items():
        visible = {key: value for key, value in texts.items() if turn_number(key) <= cutoff}
        cards = {key: value for key, value in revisions.items() if key in visible}
        start = perf_counter_ns()
        rankings = rank_questions([q["question"] for q in group], visible, cards)
        elapsed_us = (perf_counter_ns() - start) / (1000 * len(group))
        for query, ranked in zip(group, rankings, strict=True):
            assert all(key in visible for key, _, _ in ranked)
            result[query["id"]] = ranked, elapsed_us
    return result


def run_query(
    query: dict,
    ranked: list[tuple[str, float | None, list[str]]],
    elapsed_us: float,
    evidence: dict[str, dict[str, str]],
    message_turns: dict[str, str],
    turn_sources: dict[str, list[str]],
) -> dict:
    top = ranked[:TOP_K]

    selected = pack_evidence([key for key, _, _ in top], evidence, CONTEXT_CHARS)
    packed = [item.turn_id for item in selected]
    used = len("\n\n".join(item.text for item in selected))

    required = query["required_message_ids"]
    # Gold labels are used only for scoring once ranking and packing are complete.
    gold_turns = [message_turns[message_id] for message_id in required]
    assert all(turn_number(key) <= query["through_turn"] for key in gold_turns), query["id"]
    packed_sources = [source for key in packed for source in turn_sources[key]]
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
        "packed_message_ids": packed_sources,
        "covered": all(key in packed_sources for key in required) if required else None,
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



def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def grouped_history(history: list[Message]) -> dict[str, list[Message]]:
    turns = {}
    for message in history:
        turns.setdefault(message["turn_id"], []).append(message)
    return turns


def packed_records(packed: list, turns: dict[str, list[Message]], originals: dict[str, Record]) -> dict[str, Record]:
    result = {}
    for item in packed:
        rendered = []
        for message in turns[item.turn_id]:
            content = message["content"]
            if item.form == "compact" and message["role"] == "assistant":
                content = compact_revision(content)
            rendered.append(message_block(message, content))
            if message["id"] in originals:
                record = record_from_message({**message, "content": content})
                original = originals[message["id"]]
                assert record and (record.state, record.values) == (original.state, original.values), message["id"]
                result[record.message_id] = record
        assert item.text == "\n".join(rendered), item.turn_id
    return result


def evidence_satisfies(query: dict, records: dict[str, Record], labels: dict) -> bool:
    for source in query["required_message_ids"]:
        record = records.get(source)
        if record is None:
            return False
        expected = labels[source]
        slots = query.get("required_slots", {}).get(source, expected["slots"])
        if record.state != expected["state"] or not {tuple(item) for item in slots} <= {
            (value.shot, value.name) for value in record.values
        }:
            return False
    return True


def pure_replay(history: list[Message], queries: list[dict], labels: dict, method: str) -> dict:
    start_load = perf_counter_ns()
    turns = grouped_history(history)
    texts = {key: searchable_text(messages) for key, messages in turns.items()}
    evidence = {key: build_evidence(messages) for key, messages in turns.items()}
    cards = {key: card for key, messages in turns.items() if (card := revision_evidence(messages))}
    records = [record for message in history if (record := record_from_message(message))]
    originals = {record.message_id: record for record in records}
    load_ms = (perf_counter_ns() - start_load) / 1e6
    results = []

    for query in queries:
        start = perf_counter_ns()
        cutoff = query["through_turn"]
        visible = {key: messages for key, messages in turns.items() if turn_number(key) <= cutoff}
        question = query["question"]
        if method == "B0":
            candidate_ids = list(visible)
            ranked = [(turn, None, []) for turn in reversed(visible)]
            reason = "recent"
        elif method == "B2":
            candidate_ids = [key for key in visible if key in cards]
            ranked = rank_questions([question], {key: texts[key] for key in visible},
                                    {key: cards[key] for key in candidate_ids})[0]
            reason = "phrase_match" if ranked else "no_match"
        else:
            ranked, candidate_ids, reason = rank_structured(question, visible, records, query.get("selected_shot"))
        assert set(candidate_ids) <= visible.keys(), query["id"]
        assert all(key in visible for key, _, _ in ranked), query["id"]
        packed = pack_evidence([key for key, _, _ in ranked[:TOP_K]], evidence, CONTEXT_CHARS)
        chars = len("\n\n".join(item.text for item in packed))
        elapsed = (perf_counter_ns() - start) / 1e6

        actual = packed_records(packed, visible, originals)
        expected = query["required_message_ids"]
        assert all(source in originals and originals[source].turn_id in visible for source in expected), query["id"]
        assert not expected or evidence_satisfies(query, originals, labels), query["id"]
        covered = evidence_satisfies(query, actual, labels) if expected else None
        expected_turns = {originals[source].turn_id for source in expected}
        if covered is False and not expected_turns <= set(candidate_ids):
            failure = "candidate"
        elif covered is False and not expected_turns <= {key for key, _, _ in ranked[:TOP_K]}:
            failure = "ranking_or_rejection"
        elif covered is False:
            failure = "packing"
        elif not expected and packed:
            failure = "false_recall"
        else:
            failure = None
        assert chars <= CONTEXT_CHARS
        results.append({
            "id": query["id"],
            "group": query["group"],
            "cutoff": cutoff,
            "expected": expected,
            "candidate_turns": candidate_ids,
            "top": [{"turn": key, "score": score, "matches": matches} for key, score, matches in ranked[:TOP_K]],
            "packed_turns": [item.turn_id for item in packed],
            "packed_message_ids": [m["id"] for item in packed for m in visible[item.turn_id]],
            "evidence": {source: {"state": record.state, "slots": [[v.shot, v.name] for v in record.values]}
                         for source, record in actual.items()},
            "evidence_chars": chars,
            "covered": covered,
            "false_recall": bool(packed) if not expected else None,
            "decision": reason,
            "failure": failure,
            "query_ms": round(elapsed, 5),
        })

    summary = {}
    for name in ("near", "late", "no_evidence"):
        subset = [row for row in results if row["group"] == name]
        if subset:
            summary[name] = {
                "queries": len(subset),
                "covered": sum(row["covered"] is True for row in subset),
                "false_recall": sum(row["false_recall"] is True for row in subset),
            }
    times = sorted(row["query_ms"] for row in results)
    return {"summary": summary, "load_ms": round(load_ms, 5),
            "p50_ms": median(times), "p95_ms": times[ceil(0.95 * len(times)) - 1], "queries": results}


def fixture_history(records: list) -> list[Message]:
    source_rows = {}
    for turn, source, state, shot, field, old, new in records:
        source_rows.setdefault((turn, source, state), []).append((shot, field, old, new))
    history = []
    fence = chr(96) * 3
    for (turn, source, state), rows in source_rows.items():
        if state == "applied":
            tag = "applied_diff"
            role = "program"
            body = [{"镜号": shot, "字段": field, "旧值": old, "新值": new} for shot, field, old, new in rows]
        else:
            tag = "revised_shots"
            role = "assistant"
            body = {"镜头": [{"镜号": shot, field: new} for shot, field, _, new in rows]}
        history.append({"id": source, "turn_id": turn, "role": role,
                        "content": f"{fence}{tag}\n{json.dumps(body, ensure_ascii=False)}\n{fence}"})
    return history


def selected_sources(history: list[Message], question: str, cutoff: int) -> list[str]:
    visible = {key: rows for key, rows in grouped_history(history).items() if turn_number(key) <= cutoff}
    records = [record for message in history if (record := record_from_message(message))]
    ranked, _, _ = rank_structured(question, visible, records)
    packed = pack_evidence([row[0] for row in ranked[:TOP_K]],
                           {key: build_evidence(rows) for key, rows in visible.items()}, CONTEXT_CHARS)
    return [m["id"] for item in packed for m in visible[item.turn_id]]


def reference_checks(datasets: dict, labels: dict, fixture: dict) -> dict:
    verified = {}
    for name in ("real", "simulated"):
        history = datasets[name]
        counts = {}
        for message in history:
            source = message["id"]
            if source not in labels:
                continue
            cutoff = turn_number(message["turn_id"])
            counts[source] = (
                source in selected_sources(history, f"查阅 {source}。", cutoff)
                and not selected_sources(history, f"查阅 {source}。", cutoff - 1)
            )
        unknown = not selected_sources(history, "查阅 p9999。", 999)
        verified[name] = {"known_and_not_visible_before": counts, "unknown_empty": unknown}

    history = fixture_history(fixture["records"])
    alternate = fixture_history(fixture["other_session_records"])
    references = {}
    for key, cutoff, question, expect in fixture["queries"]:
        sources = selected_sources(history, question, cutoff)
        references[key] = {"expected": expect, "actual": sources,
                           "passed": sources == ([expect] if expect else [])}
    references["other_session_positive_control"] = {
        "passed": selected_sources(alternate, "查阅 p199。", 4) == ["p199"],
    }
    verified["receipt_and_scope"] = references
    return verified


def pure_gates(method: dict, checks: dict) -> dict:
    real, paraphrase, sim = (method[key] for key in ("real", "paraphrase", "simulated"))
    r, p, s = (case["summary"] for case in (real, paraphrase, sim))
    return {
        "real_near": r["near"]["covered"] >= 7,
        "real_late": r["late"]["covered"] >= 6,
        "real_paraphrase": p["late"]["covered"] >= 5,
        "real_false_recall": r["no_evidence"]["false_recall"] + p["no_evidence"]["false_recall"] <= 1,
        "simulated": s["near"]["covered"] >= 8 and s["late"]["covered"] >= 4
                     and s["no_evidence"]["false_recall"] <= 1,
        "source_and_cutoff": all(
            len(checks[name]["known_and_not_visible_before"]) == count
            and all(checks[name]["known_and_not_visible_before"].values())
            and checks[name]["unknown_empty"] for name, count in (("real", 8), ("simulated", 10))
        ),
        "receipt_and_scope": all(item["passed"] for item in checks["receipt_and_scope"].values()),
        "evidence_budget": all(row["evidence_chars"] <= CONTEXT_CHARS
                               for dataset in method.values() for row in dataset["queries"]),
        "query_p95": max(real["p95_ms"], paraphrase["p95_ms"], sim["p95_ms"]) <= 50,
        "held_out": (
            method["holdout"]["summary"]["late"]["covered"] * 4 >= 3 * method["holdout"]["summary"]["late"]["queries"]
            and method["holdout"]["summary"]["no_evidence"]["false_recall"] * 4
            <= method["holdout"]["summary"]["no_evidence"]["queries"]
        ) if "holdout" in method else None,
    }


def run_pure(output: Path, holdout: bool) -> None:
    cases_path = HERE / "pure_experiment_cases.json"
    cases_text = cases_path.read_text(encoding="utf-8")
    cases = json.loads(cases_text)
    datasets, queries, hashes = {}, {}, {}
    for name, specification in cases["known_sets"].items():
        history_text = (HERE.parents[1] / specification["history"]).read_text(encoding="utf-8")
        assert digest(history_text) == specification["history_sha256_utf8_lf"], name
        datasets[name] = json.loads(history_text)
        hashes[name] = {"history": digest(history_text), "queries": {}}
        for filename in specification["queries"]:
            text = (HERE / filename).read_text(encoding="utf-8")
            assert digest(text) == specification["queries_sha256_utf8_lf"][filename], filename
            set_name = "paraphrase" if filename == "real_validation_queries.jsonl" else name
            queries[set_name] = [json.loads(line) for line in text.splitlines() if line]
            hashes[name]["queries"][filename] = digest(text)
    if holdout:
        queries["holdout"] = cases["holdout"]
    methods = {}
    labels = cases["required_records"]
    for name in ("B0", "B2", PURE_METHOD):
        methods[name] = {
            group: pure_replay(datasets["simulated" if group == "simulated" else "real"], questions, labels, name)
            for group, questions in queries.items()
        }
    checks = reference_checks(datasets, labels, cases["state_fixture"])
    gates = pure_gates(methods[PURE_METHOD], checks)
    result = {
        "code_commit": git_commit(),
        "python": platform.python_version(),
        "cases_sha256_utf8_lf": digest(cases_text),
        "data": hashes,
        "settings": {
            "top_k": TOP_K, "history_evidence_chars": CONTEXT_CHARS, "candidate": PURE_SETTINGS,
            "scope": "one conversation and visible turns at query cutoff",
            "B0_commit": "c6f7532", "B2_commit": "26d4298",
        },
        "methods": methods,
        "checks": checks,
        "gates": gates,
    }
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name, sets in methods.items():
        print(name, {group: values["summary"] for group, values in sets.items()})
    print("Gates:", gates)


def main() -> None:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, help="Override the built-in history.")
    parser.add_argument("--queries", type=Path, help="Override the frozen query list.")
    parser.add_argument("--output", type=Path, help="Override the result path.")
    parser.add_argument('--pure', action='store_true', help='Run the fixed pure-retrieval comparisons.')
    parser.add_argument('--holdout', action='store_true', help='Include frozen holdout after tuning.')
    options = parser.parse_args()
    if options.holdout and not options.pure:
        parser.error('--holdout requires --pure')
    if options.pure:
        if options.history or options.queries:
            parser.error('--pure uses the frozen inputs in pure_experiment_cases.json')
        run_pure(options.output or HERE / 'pure_experiment_result.json', options.holdout)
        return
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
    turn_sources = {key: [m["id"] for m in group] for key, group in turns.items()}
    revisions = {
        key: card for key, group in turns.items() if (card := revision_evidence(group)) is not None
    }
    rankings = rank_all(queries, texts, revisions)
    rows = [run_query(q, *rankings[q["id"]], evidence, message_turns, turn_sources) for q in queries]
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
