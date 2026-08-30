import json

from core.evaluation import live_quality_probe


def test_unreachable_live_probe_records_blocker_without_model_call(monkeypatch, tmp_path):
    monkeypatch.setattr(
        live_quality_probe,
        "_reachable",
        lambda host, port: (False, "ConnectionRefusedError: refused"),
    )
    monkeypatch.setattr(
        live_quality_probe,
        "send_prompt",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not call model")),
    )

    code, report = live_quality_probe.run_probe(tmp_path)

    assert code == 2
    assert report["status"] == "blocked"
    assert report["attempted_model_calls"] == 0
    saved = json.loads(
        (tmp_path / "system" / "quality_runs" / live_quality_probe.RUN_ID / "blocker.json").read_text(
            encoding="utf-8"
        )
    )
    assert saved["recovery"]
