"""The application entry point starts the PySide6 GUI."""
from __future__ import annotations

import main


def test_default_starts_qt(monkeypatch):
    calls = []
    monkeypatch.setattr(main.sys, "argv", ["main.py"])
    monkeypatch.setattr(main, "run_gui", lambda: calls.append("qt") or 0)

    assert main.main() == 0
    assert calls == ["qt"]
