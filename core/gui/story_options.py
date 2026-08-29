# -*- coding: utf-8 -*-
"""默认 GUI 使用的作品参数选项查询。

数据的唯一来源是 :mod:`core.config.story_options`；此模块保留界面所需的短导入路径。
"""
from __future__ import annotations

from core.config.story_options import (
    DEFAULT_STRUCTURE,
    GENDER_BIAS_MAP,
    LENGTH_OPTIONS,
    STRUCTURE_MAP,
    SUBGENRES,
)
from Generators.GenreHandlers import get_supported_genres


def supported_genres() -> tuple[str, ...]:
    genres = tuple(get_supported_genres())
    if not genres:
        raise RuntimeError("题材处理器没有返回任何支持的题材")
    return genres


def subgenres_for(genre: str) -> tuple[str, ...]:
    return SUBGENRES.get(genre, ())


def structures_for(length: str) -> list[str]:
    return list(STRUCTURE_MAP.get(length, []))


def default_structure_for(length: str) -> str:
    options = structures_for(length)
    fallback = options[0] if options else ""
    return DEFAULT_STRUCTURE.get(length, fallback)
