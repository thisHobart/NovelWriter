# -*- coding: utf-8 -*-
"""默认 GUI 使用的作品参数选项查询。

数据的唯一来源是 :mod:`core.config.story_options`；此模块保留界面所需的短导入路径，
并按题材挑出主题、基调的预设选项。
"""
from __future__ import annotations

from core.config.story_options import (
    GENRE_OPTIONS,
    LENGTH_OPTIONS,
    PRESET_GENRE_BY_PROFILE,
    THEME_OPTIONS,
    TONE_OPTIONS,
    default_structure_for,
)
from core.generation.domain_profiles import match_profile_key

__all__ = [
    "GENRE_OPTIONS", "LENGTH_OPTIONS", "default_structure_for", "is_preset_theme", "is_preset_tone",
    "preset_genre_for", "theme_options_for", "tone_options_for",
]

_ALL_THEMES = {option for options in THEME_OPTIONS.values() for option in options}
_ALL_TONES = {option for options in TONE_OPTIONS.values() for option in options}


def preset_genre_for(genre: str) -> str:
    """题材归到哪个预设题材：本身就是预设的直接用，否则按评审规则的关键词归类；都不是返回空串。"""
    text = str(genre or "").strip()
    if text in GENRE_OPTIONS:
        return text
    return PRESET_GENRE_BY_PROFILE.get(match_profile_key(text) or "", "")


def theme_options_for(genre: str) -> list[str]:
    return list(THEME_OPTIONS[preset_genre_for(genre)])


def tone_options_for(genre: str) -> list[str]:
    return list(TONE_OPTIONS[preset_genre_for(genre)])


def is_preset_theme(text: str) -> bool:
    return str(text or "").strip() in _ALL_THEMES


def is_preset_tone(text: str) -> bool:
    return str(text or "").strip() in _ALL_TONES
