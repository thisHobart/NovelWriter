#!/usr/bin/env python3
"""NovelWriter PySide6 application entry point."""
import sys

from core.gui.app import main as qt_main


# Stable indirection retained for callers and tests that replace the GUI
# launcher without importing or starting a real Qt event loop.
run_gui = qt_main


def main() -> int:
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
