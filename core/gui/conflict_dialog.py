"""The Tk presentation of a blocked acceptance.

Everything that decides *what the choices are* lives in
``core.generation.conflict_briefing`` so the headless batch writer can describe
the same clash without importing a GUI toolkit.  This file only draws it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable, Dict, Optional

from core.generation.conflict_briefing import (  # re-exported for callers
    ACCEPT_REVERSAL,
    KEEP_EXISTING,
    ConflictBriefing,
    ConflictChoice,
    apply_decision,
    adjudication_intro,
    build_briefing,
    conflict_record_path,
    describe_briefing,
    needs_author_decision,
)

__all__ = [
    "ACCEPT_REVERSAL",
    "KEEP_EXISTING",
    "ConflictBriefing",
    "ConflictChoice",
    "apply_decision",
    "ask_on_main_thread",
    "build_briefing",
    "conflict_record_path",
    "describe_briefing",
    "needs_author_decision",
    "show_conflict_dialog",
]


def _reveal(path: str) -> None:
    """Open the stored conflict record in whatever the OS uses for JSON."""
    if not path or not os.path.exists(path):
        return
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa: S606 - opening a file the app itself wrote
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def show_conflict_dialog(
    parent: tk.Misc,
    briefing: ConflictBriefing,
    on_decision: Callable[[str], None],
    conflict_path: str = "",
    adjudication: str = "",
) -> Optional[str]:
    """Present the clash and the two rulings; returns the chosen one."""
    window = tk.Toplevel(parent)
    window.title(f"第 {briefing.chapter} 章与已接受设定冲突")
    window.transient(parent)
    window.grab_set()
    chosen: Dict[str, Optional[str]] = {"value": None}

    frame = ttk.Frame(window, padding=12)
    frame.pack(fill="both", expand=True)

    ttk.Label(
        frame,
        text=adjudication_intro(briefing.chapter, adjudication),
        font=("Arial", 10, "bold"),
        wraplength=560,
    ).pack(anchor="w", pady=(0, 8))

    for choice in briefing.choices:
        box = ttk.LabelFrame(
            frame, text=f"{choice.label or choice.record_id}（{choice.record_id}）", padding=8
        )
        box.pack(fill="x", pady=4)
        ttk.Label(box, text=f"账本已接受：{choice.existing}", wraplength=540).pack(anchor="w")
        ttk.Label(box, text=f"本章声明：　{choice.proposed}", wraplength=540).pack(anchor="w")
        if choice.reason:
            ttk.Label(
                box, text=f"本章给出的理由：{choice.reason}", wraplength=540, foreground="#1976D2"
            ).pack(anchor="w", pady=(4, 0))

    for message in briefing.other_messages:
        ttk.Label(frame, text=f"· {message}", wraplength=560, foreground="gray").pack(anchor="w")

    ttk.Label(
        frame,
        text=(
            "采纳本章：账本改用本章的取值，后续章节以新取值为准。\n"
            "维持账本：保留原取值，本章需要重新生成。"
        ),
        wraplength=560,
        foreground="gray",
    ).pack(anchor="w", pady=(8, 4))

    buttons = ttk.Frame(frame)
    buttons.pack(fill="x", pady=(8, 0))

    def choose(value: str) -> None:
        chosen["value"] = value
        window.destroy()
        on_decision(value)

    if needs_author_decision(briefing, adjudication):
        ttk.Button(
            buttons,
            text="采纳本章（记为剧情反转）",
            command=lambda: choose(ACCEPT_REVERSAL),
        ).pack(side="left", padx=4)
        ttk.Button(
            buttons,
            text="维持账本，重写本章",
            command=lambda: choose(KEEP_EXISTING),
        ).pack(side="left", padx=4)
    if conflict_path:
        ttk.Button(
            buttons, text="查看冲突记录", command=lambda: _reveal(conflict_path)
        ).pack(side="left", padx=4)
    close_text = (
        "稍后再说" if needs_author_decision(briefing, adjudication) else "关闭"
    )
    ttk.Button(buttons, text=close_text, command=window.destroy).pack(side="right", padx=4)

    parent.wait_window(window)
    return chosen["value"]


def ask_on_main_thread(
    root: tk.Misc,
    briefing: ConflictBriefing,
    conflict_path: str = "",
    adjudication: str = "",
    timeout: float = 600.0,
) -> Optional[str]:
    """Ask the author from a worker thread without touching Tk from one.

    Generation runs on a background thread, and a modal built there is a crash
    waiting to happen.  The dialog is scheduled onto the event loop and the
    worker simply waits for the answer.
    """
    answered = threading.Event()
    result: Dict[str, Optional[str]] = {"value": None}

    def present() -> None:
        try:
            result["value"] = show_conflict_dialog(
                root, briefing, lambda _value: None, conflict_path, adjudication
            )
        finally:
            answered.set()

    root.after(0, present)
    if not answered.wait(timeout):
        return None
    return result["value"]
