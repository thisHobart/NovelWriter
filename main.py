#!/usr/bin/env python3
"""
NovelWriter: An AI-assisted novel writing tool
Entry point for the application.

    python main.py          旧版 tkinter 界面（默认，功能完整）
    python main.py --qt     新版 PySide6 界面（迁移中，见 docs/UI_MIGRATION.md）

两套界面读写同一份工程目录，可随时来回切换。
"""

import sys


def run_qt() -> int:
    try:
        from ui.app import main as qt_main
    except ImportError as exc:  # PySide6 未安装
        print(f"无法启动 PySide6 界面：{exc}", file=sys.stderr)
        print("请先安装依赖：pip install PySide6", file=sys.stderr)
        return 1
    return qt_main()


def run_tk() -> int:
    import tkinter as tk

    from core.gui.app import NovelWriterApp

    root = tk.Tk()
    root.geometry("1200x900")  # Width x Height in pixels
    NovelWriterApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    if "--qt" in sys.argv:
        sys.argv.remove("--qt")
        raise SystemExit(run_qt())
    raise SystemExit(run_tk())
