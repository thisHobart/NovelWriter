"""Load the app-owned storyboard skill before freezing a generation request."""

from pathlib import Path


def load_storyboard_skill() -> str:
    return (
        Path(__file__).with_name("skills") / "short-video-storyboard" / "SKILL.md"
    ).read_text(encoding="utf-8")
