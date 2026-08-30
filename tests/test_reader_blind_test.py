import csv
import json

from core.evaluation import reader_blind_test
from core.evaluation.reader_blind_test import create_blind_test, summarize_blind_test


def _write_chapters(directory, prefix):
    directory.mkdir()
    (directory / "chapter_1.md").write_text(f"{prefix}第一章正文。" * 40, encoding="utf-8")
    (directory / "chapter_2.md").write_text(f"{prefix}第二章正文。" * 40, encoding="utf-8")


def test_blind_test_balances_ab_positions_and_hides_sources(tmp_path):
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    output = tmp_path / "test"
    _write_chapters(baseline, "旧版")
    _write_chapters(candidate, "新版")

    manifest = create_blind_test(
        baseline, candidate, output, participants=4, excerpt_chars=200, seed=7
    )

    for item_id in manifest["items"]:
        positions = [mapping[item_id] for mapping in manifest["assignments"].values()]
        assert positions.count("a") == positions.count("b") == 2
    packet = (output / "packets" / "reader_001.md").read_text(encoding="utf-8")
    assert "chapter_1.md" not in packet
    assert "baseline" not in packet
    assert (output / "manifest.private.json").exists()
    with (output / "responses.csv").open(encoding="utf-8-sig") as handle:
        assert len(list(csv.DictReader(handle))) == 8


def test_blind_test_summary_unmasks_candidate_and_uses_paired_differences(tmp_path):
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    output = tmp_path / "test"
    _write_chapters(baseline, "旧版")
    _write_chapters(candidate, "新版")
    create_blind_test(baseline, candidate, output, participants=4, seed=11)
    manifest = json.loads((output / "manifest.private.json").read_text(encoding="utf-8"))

    response_path = output / "responses.csv"
    with response_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
        fieldnames = list(rows[0])
    for row in rows:
        position = manifest["assignments"][row["participant_id"]][row["item_id"]]
        other = "b" if position == "a" else "a"
        row["preferred_variant"] = position
        for metric in (
            "continue_score",
            "plausibility_score",
            "character_score",
            "chinese_comfort_score",
        ):
            row[f"{metric}_{position}"] = "5"
            row[f"{metric}_{other}"] = "3"
        row[f"ai_likeness_score_{position}"] = "2"
        row[f"ai_likeness_score_{other}"] = "4"
    with response_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    summary = summarize_blind_test(output)

    assert summary["completed_readers"] == 4
    assert summary["preference"]["candidate_wins"] == 8
    assert summary["paired_effects"]["continue_score"]["mean_difference"] == 2
    assert summary["paired_effects"]["ai_likeness_score"]["mean_difference"] == 2
    assert summary["warnings"]


def test_cli_json_prints_on_non_utf_windows_stdout(monkeypatch):
    class Cp1252Sink:
        encoding = "cp1252"

        def __init__(self):
            self.parts = []

        def write(self, value):
            value.encode(self.encoding)
            self.parts.append(value)
            return len(value)

        def flush(self):
            return None

    sink = Cp1252Sink()
    monkeypatch.setattr(reader_blind_test.sys, "stdout", sink)

    reader_blind_test._print_json({"warning": "尚未收齐读者"})

    assert "\\u5c1a\\u672a" in "".join(sink.parts)
