# -*- coding: utf-8 -*-
"""默认 GUI 使用的作品参数选项查询。

数据的唯一来源是 :mod:`core.config.story_options`；此模块保留界面所需的短导入路径。
"""
from __future__ import annotations

from core.config.story_options import LENGTH_OPTIONS, default_structure_for

__all__ = ["LENGTH_OPTIONS", "default_structure_for"]
