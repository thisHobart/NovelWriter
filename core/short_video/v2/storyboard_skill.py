"""Explicitly selectable storyboard skills; no automatic discovery or routing."""

from pathlib import Path

STORYBOARD_SKILLS = {"short-video-content-storyboard": "短视频内容分镜"}


def load_storyboard_skill(skill_id: str) -> str:
    # An unknown choice fails instead of becoming an arbitrary filesystem path.
    STORYBOARD_SKILLS[skill_id]
    return (Path(__file__).with_name("skills") / skill_id / "SKILL.md").read_text(encoding="utf-8")
