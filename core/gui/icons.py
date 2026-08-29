# -*- coding: utf-8 -*-
"""内联 SVG 图标。

约定（docs/UI_CONTRACT.md）：不用 emoji，全部描边式 SVG，16px 网格，
统一 1.4~1.6 描边宽度，颜色由调用方传入 token。
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from core.gui import theme

_TEMPLATES = {
    "check": '<path d="M3.5 8.5L6.5 11.5L12.5 4.5" stroke="{c}" stroke-width="1.6" '
             'stroke-linecap="round" stroke-linejoin="round" fill="none"/>',
    "half": '<circle cx="8" cy="8" r="5.2" stroke="{c}" stroke-width="1.6" fill="none"/>'
            '<path d="M8 2.8A5.2 5.2 0 0 1 8 13.2Z" fill="{c}"/>',
    "empty": '<circle cx="8" cy="8" r="5.2" stroke="{c}" stroke-width="1.4" fill="none" '
             'stroke-dasharray="2.4 2.2"/>',
    "lock": '<rect x="3.5" y="7" width="9" height="6.2" rx="1.4" stroke="{c}" '
            'stroke-width="1.4" fill="none"/>'
            '<path d="M5.8 7V5.4a2.2 2.2 0 0 1 4.4 0V7" stroke="{c}" stroke-width="1.4" '
            'stroke-linecap="round" fill="none"/>',
    "chevron": '<path d="M4 6.5L8 10.5L12 6.5" stroke="{c}" stroke-width="1.5" '
               'stroke-linecap="round" stroke-linejoin="round" fill="none"/>',
    "folder": '<path d="M2 4.5A1.5 1.5 0 0 1 3.5 3h2.4l1.3 1.6h5.3A1.5 1.5 0 0 1 14 6.1v5.4'
              'A1.5 1.5 0 0 1 12.5 13h-9A1.5 1.5 0 0 1 2 11.5v-7Z" stroke="{c}" '
              'stroke-width="1.3" stroke-linejoin="round" fill="none"/>',
    "refresh": '<path d="M13 8a5 5 0 1 1-1.6-3.7" stroke="{c}" stroke-width="1.4" '
               'stroke-linecap="round" fill="none"/>'
               '<path d="M13 2.6V5h-2.4" stroke="{c}" stroke-width="1.4" '
               'stroke-linecap="round" stroke-linejoin="round" fill="none"/>',
    "stop": '<rect x="4" y="4" width="8" height="8" rx="1.2" fill="{c}"/>',
    "doc": '<path d="M4 2.5h5L12 5.5v8h-8v-11Z" stroke="{c}" stroke-width="1.3" '
           'stroke-linejoin="round" fill="none"/>'
           '<path d="M9 2.5V5.5h3" stroke="{c}" stroke-width="1.3" '
           'stroke-linejoin="round" fill="none"/>',
    "spinner": '<circle cx="8" cy="8" r="5.6" stroke="#D6D5D1" stroke-width="1.8" fill="none"/>'
               '<path d="M8 2.4a5.6 5.6 0 0 1 5.6 5.6" stroke="{c}" stroke-width="1.8" '
               'stroke-linecap="round" fill="none"/>',
    "dot": '<circle cx="8" cy="8" r="3" fill="{c}"/>',
}

# 阶段状态 -> 图标名与颜色，供步骤栏与状态栏共用
STATE_ICONS = {
    "complete": ("check", theme.SUCCESS),
    "partial": ("half", theme.WARN),
    "blocked": ("lock", theme.DANGER),
    "running": ("spinner", theme.ACCENT),
    "idle": ("empty", theme.INK_400),
}

_cache: dict[tuple[str, str, int], QPixmap] = {}


def pixmap(name: str, color: str = theme.INK_600, size: int = 16, dpr: float = 2.0) -> QPixmap:
    key = (name, color, size)
    if key in _cache:
        return _cache[key]
    body = _TEMPLATES.get(name, _TEMPLATES["dot"]).format(c=color)
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">{body}</svg>'
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    px = QPixmap(QSize(int(size * dpr), int(size * dpr)))
    px.setDevicePixelRatio(dpr)
    px.fill(Qt.transparent)
    painter = QPainter(px)
    renderer.render(painter)
    painter.end()
    _cache[key] = px
    return px


def icon(name: str, color: str = theme.INK_600, size: int = 16) -> QIcon:
    return QIcon(pixmap(name, color, size))


def state_pixmap(state: str, size: int = 16) -> QPixmap:
    name, color = STATE_ICONS.get(state, STATE_ICONS["idle"])
    return pixmap(name, color, size)
