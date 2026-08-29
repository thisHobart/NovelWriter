# -*- coding: utf-8 -*-
"""设计 token 与全局样式表。

唯一的样式来源：任何颜色、字号、间距都从这里取，不在控件里写字面值。
token 表与 docs/UI_CONTRACT.md 第 0 节一一对应。
"""
from __future__ import annotations

# --- 颜色 -----------------------------------------------------------------
INK_900 = "#1C1B19"
INK_600 = "#6E6C67"
INK_400 = "#9A9792"
LINE = "#E4E4E1"
LINE_STRONG = "#D6D5D1"
RAIL = "#F4F3F0"
CANVAS = "#FFFFFF"
SURFACE = "#FAFAF8"
ACCENT = "#2C4A7C"
ACCENT_HOVER = "#22406F"
ACCENT_TINT = "#EDF1F8"
ACCENT_DISABLED = "#B9C3D6"
SUCCESS = "#2F7D4F"
WARN = "#B4761D"
DANGER = "#B3392F"
DANGER_HOVER = "#9A3129"

# --- 尺寸 -----------------------------------------------------------------
RAIL_WIDTH = 248
HEADER_HEIGHT = 64
STATUSBAR_HEIGHT = 44
INSPECTOR_WIDTH = 340
CONTROL_HEIGHT = 32
RADIUS = 4
RADIUS_CARD = 6
MIN_WINDOW = (1200, 760)

# 间距只用 4 的倍数
SP = {n: n * 4 for n in range(1, 13)}

# --- 字体 -----------------------------------------------------------------
# 项目固定使用经过 ClearType 小字号优化的微软雅黑 UI。阅读区同样使用该字体，
# 避免第三方字体在 125% 等分数缩放下出现灰边或发生字形混用。
SANS_STACK = '"Microsoft YaHei UI"'
SERIF_STACK = '"Microsoft YaHei UI"'

FS_XS = 11
FS_SM = 12
FS_MD = 13
FS_LG = 16
FS_XL = 19

# --- 语义映射（workflow_status 三态 + 运行中 / 未开始）--------------------
STATE_COLORS = {
    "complete": SUCCESS,
    "partial": WARN,
    "blocked": DANGER,
    "running": ACCENT,
    "idle": INK_400,
}

STATE_LABELS = {
    "complete": "已完成",
    "partial": "部分完成",
    "blocked": "被阻塞",
    "running": "进行中",
    "idle": "未开始",
}


def build_qss() -> str:
    """全局样式表。控件通过 objectName / 动态属性选择样式变体。"""
    return f"""
* {{
    font-family: {SANS_STACK};
    font-size: {FS_MD}px;
    color: {INK_900};
}}

QWidget#Root {{ background: {SURFACE}; }}
QWidget#Rail {{ background: {RAIL}; border-right: 1px solid {LINE}; }}
QWidget#Header {{ background: {CANVAS}; border-bottom: 1px solid {LINE}; }}
QWidget#StatusBar {{ background: {SURFACE}; border-top: 1px solid {LINE}; }}
QWidget#Card, QFrame#Card {{
    background: {CANVAS};
    border: 1px solid {LINE};
    border-radius: {RADIUS_CARD}px;
}}
QWidget#CardHeader {{ border-bottom: 1px solid {LINE}; }}
QFrame#HLine {{ background: {LINE}; border: none; max-height: 1px; min-height: 1px; }}
QFrame#VLine {{ background: {LINE}; border: none; max-width: 1px; min-width: 1px; }}

QLabel#AppName {{ font-family: {SERIF_STACK}; font-size: 17px; font-weight: 600; }}
QLabel#PageTitle {{ font-family: {SERIF_STACK}; font-size: {FS_XL}px; font-weight: 600; }}
QLabel#CardTitle {{ font-size: {FS_MD}px; font-weight: 600; }}
QLabel#Secondary {{ font-size: {FS_SM}px; color: {INK_600}; }}
QLabel#Tertiary {{ font-size: {FS_XS}px; color: {INK_400}; }}
QLabel#SectionLabel {{ font-size: {FS_XS}px; color: {INK_400}; letter-spacing: 1px; }}
QLabel#FieldLabel {{ font-size: {FS_SM}px; color: {INK_600}; }}
QLabel#Hint {{ font-size: {FS_XS}px; color: {INK_400}; }}

/* --- 按钮 -------------------------------------------------------------- */
QPushButton {{
    height: {CONTROL_HEIGHT}px;
    padding: 0 14px;
    border: 1px solid {LINE_STRONG};
    border-radius: {RADIUS}px;
    background: {CANVAS};
    font-size: {FS_MD}px;
}}
QPushButton:hover {{ background: {SURFACE}; }}
QPushButton:pressed {{ background: {RAIL}; }}
QPushButton:disabled {{ color: {INK_400}; border-color: {LINE}; background: {CANVAS}; }}

QPushButton[variant="primary"] {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
    color: {CANVAS};
    font-weight: 500;
    padding: 0 15px;
}}
QPushButton[variant="primary"]:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton[variant="primary"]:disabled {{
    background: {ACCENT_DISABLED};
    border-color: {ACCENT_DISABLED};
    color: {CANVAS};
}}

QPushButton[variant="danger"] {{
    background: {DANGER};
    border: 1px solid {DANGER};
    color: {CANVAS};
}}
QPushButton[variant="danger"]:hover {{ background: {DANGER_HOVER}; border-color: {DANGER_HOVER}; }}

QPushButton[variant="ghost"] {{ border-color: transparent; background: transparent; }}
QPushButton[variant="ghost"]:hover {{ background: {SURFACE}; border-color: {LINE}; }}

/* --- 输入 -------------------------------------------------------------- */
QLineEdit {{
    height: {CONTROL_HEIGHT}px;
    padding: 0 10px;
    border: 1px solid {LINE};
    border-radius: {RADIUS}px;
    background: {CANVAS};
    selection-background-color: {ACCENT_TINT};
    selection-color: {INK_900};
}}
QLineEdit:hover {{ border-color: {LINE_STRONG}; }}
QLineEdit:focus {{ border: 1px solid {ACCENT}; }}
QLineEdit:disabled {{ background: {SURFACE}; color: {INK_400}; }}

QComboBox {{
    height: {CONTROL_HEIGHT}px;
    padding: 0 10px;
    border: 1px solid {LINE};
    border-radius: {RADIUS}px;
    background: {CANVAS};
}}
QComboBox:hover {{ border-color: {LINE_STRONG}; }}
QComboBox:focus, QComboBox:on {{ border: 1px solid {ACCENT}; }}
QComboBox:disabled {{ background: {SURFACE}; color: {INK_400}; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox QAbstractItemView {{
    border: 1px solid {ACCENT};
    background: {CANVAS};
    outline: none;
    padding: 4px 0;
    selection-background-color: {ACCENT_TINT};
    selection-color: {INK_900};
}}
QComboBox QAbstractItemView::item {{ min-height: 30px; padding: 0 11px; }}

QCheckBox {{ font-size: {FS_MD}px; spacing: 8px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {LINE_STRONG};
    border-radius: 3px;
    background: {CANVAS};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

/* --- 页签 -------------------------------------------------------------- */
QTabWidget::pane {{ border: none; background: transparent; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{
    height: 34px;
    padding: 0 14px;
    margin: 0;
    background: transparent;
    color: {INK_600};
    border: none;
    border-bottom: 2px solid transparent;
    font-size: {FS_SM}px;
}}
QTabBar::tab:selected {{ color: {INK_900}; font-weight: 500; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:hover:!selected {{ color: {INK_900}; }}

/* --- 进度条 ------------------------------------------------------------ */
QProgressBar {{
    height: 5px;
    border: none;
    border-radius: 3px;
    background: {LINE};
    text-align: center;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}

/* --- 滚动条 ------------------------------------------------------------ */
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ width: 10px; background: transparent; margin: 0; }}
QScrollBar::handle:vertical {{
    background: {LINE_STRONG}; border-radius: 5px; min-height: 40px;
}}
QScrollBar::handle:vertical:hover {{ background: {INK_400}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QToolTip {{
    background: {INK_900};
    color: {CANVAS};
    border: none;
    padding: 5px 8px;
    font-size: {FS_XS}px;
}}
"""
