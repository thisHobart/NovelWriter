#!/usr/bin/env python3
"""NovelWriter PySide6 application entry point."""
from core.gui.app import main as qt_main


def main() -> int:
    return qt_main()


if __name__ == "__main__":
    raise SystemExit(main())
