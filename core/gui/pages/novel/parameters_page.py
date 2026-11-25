# -*- coding: utf-8 -*-
"""阶段 1 · 作品参数。

行为约定见 docs/novel/interaction.md 第 3 节：
  * 只保留会写进后续提示词的几项：输出目录、作品标题、题材、篇幅、主题、基调；
  * 题材由作者直接填写；故事结构不再选择，按篇幅固定，已有作品沿用保存的结构；
  * 校验失败逐字段标红 + 状态栏汇总，不弹窗；
  * 任意字段变更进入「已修改未保存」态；已有下游产物时追加一致性警告。

质量闭环档位与势力、人物数量分别在「章节撰写」「世界设定」页设置，与这里共用保存状态。
磁盘格式由 core/gui/pages/novel/params_store.py 统一维护。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from core.gui import theme

from core.gui.pages.novel import params_store, story_options
from core.gui.widgets import (
    Card,
    LabeledRow,
    PageHeader,
    hint_label,
    primary_button,
    secondary_button,
)
from core.localization import zh_label
from core.generation.domain_profiles import DEFAULT_QUALITY_LOOP_MODE, resolve_quality_loop_mode
from core.generation.parameters import RUNTIME_KEYS, internal_parameters

logger = logging.getLogger("core.gui.parameters")

GENRE_PLACEHOLDER = "例如：都市悬疑、职场甜宠、古风权谋"
GENRE_HINT = ("评审规则按题材里的关键词选择：法律、悬疑 / 推理、惊悚、恐怖、科幻、奇幻、"
              "爱情 / 言情、历史、西部；都不含时用通用规则。")

#: 参数文件里没有界面控件、但仍要原样保留的键（例如手写的 Scene Dialogue、Domain Profile）。
#: 以前页面写过的作者姓名、性别比例、子题材和附加设置不在此列，保存时即从文件中去掉。
PRESERVED_KEYS = (RUNTIME_KEYS - {"output_directory", "backend", "model", "quality_loop"}) | {"domain_profile"}


class ParametersPage(QWidget):
    """作品参数编辑页。对外只暴露 dirty / 参数字典 / 保存与加载。"""

    dirty_changed = Signal(bool)
    output_dir_changed = Signal(str)
    saved = Signal(str)               # 写入路径
    work_title_changed = Signal(str)
    operation_failed = Signal(str, str)  # 标题、原始错误信息
    model_loaded = Signal(str)

    def __init__(self, get_backend: Callable[[], str] = lambda: "",
                 get_model: Callable[[], str] = lambda: "",
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._get_backend = get_backend
        self._get_model = get_model
        self._loading = True          # 载入期间不触发 dirty
        self._dirty = False
        self._saved_source = {}
        self._structure = story_options.default_structure_for(story_options.LENGTH_OPTIONS[0])
        self._quality_mode = DEFAULT_QUALITY_LOOP_MODE
        self.generation_counts = {"num_factions": 6, "num_characters": 8}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.load_button = secondary_button("加载参数")
        self.save_button = primary_button("保存参数")
        self.header = PageHeader(
            "作品参数", "题材决定后续所有阶段的提示词与评审规则",
            [self.load_button, self.save_button],
        )
        layout.addWidget(self.header)
        layout.addWidget(self._build_body(), 1)

        self.load_button.clicked.connect(self.load)
        self.save_button.clicked.connect(self.save)

        self._wire_dirty()
        self.load()

    # ================================================== 构建
    def _build_body(self) -> QWidget:
        body = QWidget()
        outer = QHBoxLayout(body)
        outer.setContentsMargins(24, 22, 24, 22)
        outer.setSpacing(18)
        outer.addWidget(self._build_core_card(), 0)
        outer.addStretch(1)
        return body

    def _build_core_card(self) -> QWidget:
        card = Card("核心信息")
        card.setFixedWidth(640)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        rows = LabeledRow(grid)

        dir_row = QWidget()
        dir_layout = QHBoxLayout(dir_row)
        dir_layout.setContentsMargins(0, 0, 0, 0)
        dir_layout.setSpacing(7)
        self.output_dir_edit = QLineEdit()
        self.browse_button = secondary_button("浏览…", "folder")
        self.browse_button.clicked.connect(self._browse)
        dir_layout.addWidget(self.output_dir_edit, 1)
        dir_layout.addWidget(self.browse_button)
        rows.add("输出目录", dir_row)

        self.title_edit = QLineEdit()
        rows.add("作品标题", self.title_edit)

        self.genre_edit = QLineEdit()
        self.genre_edit.setPlaceholderText(GENRE_PLACEHOLDER)
        rows.add("题材", self.genre_edit)
        rows.add_full(hint_label(GENRE_HINT))

        self.length_combo = QComboBox()
        self.length_combo.setCursor(Qt.PointingHandCursor)
        self.length_combo.addItems([zh_label(x) for x in story_options.LENGTH_OPTIONS])
        rows.add("故事篇幅", self.length_combo)

        self.theme_edit = QLineEdit()
        rows.add("主题", self.theme_edit)
        self.tone_edit = QLineEdit()
        rows.add("基调", self.tone_edit)

        card.body.addLayout(grid)
        card.body.addStretch(1)

        self.length_combo.currentIndexChanged.connect(self._on_length_changed)
        self.output_dir_edit.editingFinished.connect(
            lambda: self.output_dir_changed.emit(self.output_dir_edit.text().strip())
        )
        self.title_edit.textChanged.connect(
            lambda text: self.work_title_changed.emit(text.strip())
        )
        return card

    # ================================================== 事件
    def _wire_dirty(self) -> None:
        for edit in (self.output_dir_edit, self.title_edit, self.genre_edit,
                     self.theme_edit, self.tone_edit):
            edit.textChanged.connect(self._mark_dirty)
        self.length_combo.currentIndexChanged.connect(self._mark_dirty)

    def _mark_dirty(self, *_: Any) -> None:
        if self._loading or self._dirty:
            return
        self._dirty = True
        self.dirty_changed.emit(True)

    @property
    def dirty(self) -> bool:
        return self._dirty

    def _browse(self) -> None:
        start = self.output_dir_edit.text().strip() or os.getcwd()
        chosen = QFileDialog.getExistingDirectory(self, "选择输出目录", start)
        if chosen:  # 取消不改变输入框
            self.output_dir_edit.setText(chosen)
            self.output_dir_changed.emit(chosen)

    def _on_length_changed(self, *_: Any) -> None:
        if not self._loading:
            self._structure = story_options.default_structure_for(self._internal_length())

    # ================================================== 取值
    def _internal_length(self) -> str:
        display = self.length_combo.currentText()
        for option in story_options.LENGTH_OPTIONS:
            if zh_label(option) == display:
                return option
        return display

    def quality_mode(self) -> str:
        return self._quality_mode

    def set_quality_mode(self, mode: str) -> None:
        """「章节撰写」页改档位时调用；与其他参数一起保存。"""
        if mode != self._quality_mode:
            self._quality_mode = mode
            self._mark_dirty()

    # ================================================== 对外
    def output_dir(self) -> str:
        return self.output_dir_edit.text().strip() or "current_work"

    def parameters(self) -> Dict[str, Any]:
        """当前界面上的完整参数字典（内部键名）。"""
        params: Dict[str, Any] = {
            key: value for key, value in self._saved_source.items() if key in PRESERVED_KEYS
        }
        params.update({
            "output_directory": self.output_dir(),
            "genre": self.genre_edit.text().strip(),
            "story_length": self._internal_length(),
            "story_structure": self._structure,
            "novel_title": self.title_edit.text().strip(),
            "theme": self.theme_edit.text().strip(),
            "tone": self.tone_edit.text().strip(),
            "quality_loop": self._quality_mode,
            "backend": self._get_backend(),
            "model": self._get_model(),
        })
        params.update(self.generation_counts)
        return params

    def set_generation_counts(self, factions, characters):
        counts = {"num_factions": factions, "num_characters": characters}
        if counts != self.generation_counts:
            self.generation_counts = counts
            self._mark_dirty()

    def validate(self) -> Dict[str, str]:
        """返回 {字段名: 错误文案}；空字典表示通过。逐字段标红由 save() 施加。"""
        errors: Dict[str, str] = {}
        if not self.output_dir_edit.text().strip():
            errors["output_directory"] = "输出目录不能为空"
        if not self.title_edit.text().strip():
            errors["novel_title"] = "作品标题不能为空"
        if not self.genre_edit.text().strip():
            errors["genre"] = "题材不能为空"
        try:
            from core.generation.parameters import normalize_parameters
            normalize_parameters(self.parameters())
        except ValueError as exc:
            errors["generation_counts"] = str(exc)
        return errors

    def save(self) -> bool:
        errors = self.validate()
        self._apply_field_errors(errors)
        if errors:
            self.saved.emit("")
            return False
        params = self.parameters()
        try:
            path = params_store.save(params["output_directory"], params)
        except Exception as exc:  # noqa: BLE001
            logger.error("保存参数失败：%s", exc, exc_info=True)
            self.operation_failed.emit("保存失败", str(exc) or type(exc).__name__)
            return False
        self._dirty = False
        self._saved_source = params
        self.dirty_changed.emit(False)
        self.saved.emit(path)
        return True

    def load(self, output_dir: Optional[str] = None) -> None:
        self._loading = True
        target = output_dir or (self.output_dir_edit.text().strip() or "current_work")
        try:
            loaded = params_store.load(target)
        except Exception as exc:  # noqa: BLE001
            logger.error("读取参数失败：%s", exc, exc_info=True)
            self._loading = False
            self.operation_failed.emit("读取失败", str(exc) or type(exc).__name__)
            return

        source = internal_parameters(loaded)
        self._saved_source = source
        self.output_dir_edit.setText(str(source.get("output_directory", target)))
        self.generation_counts = {"num_factions": source.get("num_factions", 6),
                                  "num_characters": source.get("num_characters", 8)}
        for key, value in self.generation_counts.items():
            try:
                if str(int(value)) == str(value).strip():
                    self.generation_counts[key] = int(value)
            except (ValueError, TypeError):
                pass  # 保留无效原值，生成前明确校验，不静默修正。

        # 以前的参数文件分题材、子题材两项，且存英文标识；这里合成一句中文显示，保存后即为新格式。
        genre = str(source.get("genre", "")).strip()
        subgenre = str(source.get("subgenre", "")).strip()
        converted = bool(subgenre)
        if genre:
            genre = zh_label(genre)
            if subgenre and zh_label(subgenre) != genre:
                genre = f"{genre}（{zh_label(subgenre)}）"
        self.genre_edit.setText(genre)

        length = str(source.get("story_length", "")).strip()
        if length and length not in story_options.LENGTH_OPTIONS:
            length, converted = "Novel (Standard)", True   # 以前的「长篇（史诗）」
        length = length or story_options.LENGTH_OPTIONS[0]
        index = story_options.LENGTH_OPTIONS.index(length)
        self.length_combo.setCurrentIndex(index)
        self._structure = (str(source.get("story_structure", "")).strip()
                           or story_options.default_structure_for(length))

        self.title_edit.setText(str(source.get("novel_title", "")))
        self.theme_edit.setText(str(source.get("theme", "")))
        self.tone_edit.setText(str(source.get("tone", "")))
        self._quality_mode = resolve_quality_loop_mode(source)

        self._apply_field_errors({})
        self._loading = False
        self._dirty = False
        self.dirty_changed.emit(False)
        if converted:
            self._mark_dirty()
        self.loaded_model = str(source.get("model", ""))
        self.model_loaded.emit(self.loaded_model)
        self.work_title_changed.emit(self.title_edit.text().strip())

    # ================================================== 内部
    def _apply_field_errors(self, errors: Dict[str, str]) -> None:
        mapping = {
            "output_directory": self.output_dir_edit,
            "novel_title": self.title_edit,
            "genre": self.genre_edit,
        }
        for key, widget in mapping.items():
            if key in errors:
                # 连同盒模型一起重声明，避免只写 border 时控件高度/圆角跳变
                widget.setStyleSheet(
                    f"border: 1px solid {theme.DANGER};"
                    f"border-radius: {theme.RADIUS}px;"
                    f"height: {theme.CONTROL_HEIGHT}px;"
                    f"padding: 0 10px;"
                    f"background: {theme.CANVAS};"
                )
                widget.setToolTip(errors[key])
            else:
                widget.setStyleSheet("")
                widget.setToolTip("")
