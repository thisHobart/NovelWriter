# -*- coding: utf-8 -*-
"""阶段 1 · 作品参数。

行为约定见 docs/UI_CONTRACT.md 第 4 节：
  * 左列固定为核心信息，右列随子题材重建，不再上下堆叠；
  * 质量闭环三档在下拉里各带一行说明，长提示不再常驻占位；
  * 校验失败逐字段标红 + 状态栏汇总，不弹窗；
  * 任意字段变更进入「已修改未保存」态；已有下游产物时追加一致性警告。

磁盘格式由 core/gui/params_store.py 统一维护。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.gui import params_store, story_options, theme
from core.gui.widgets import (
    Card,
    LabeledRow,
    PageHeader,
    hint_label,
    primary_button,
    secondary_button,
)
from core.localization import GENDER_BIAS_ZH_LABELS, internal_label, zh_label
from core.generation.domain_profiles import (
    DEFAULT_QUALITY_LOOP_MODE,
    QUALITY_LOOP_FROM_LABEL,
    QUALITY_LOOP_LABELS,
    QUALITY_LOOP_MODES,
)

logger = logging.getLogger("core.gui.parameters")

QUALITY_LOOP_HINTS = {
    "off": "只生成正文，不做领域评审。调用量约为标准档的四成。",
    "standard": "按题材做规划 / 场景 / 整章评审。",
    "strict": "提高门槛并拉满重试次数，耗时最长。",
}


def _combo(items: list[str] = None) -> QComboBox:
    combo = QComboBox()
    combo.setCursor(Qt.PointingHandCursor)
    if items:
        combo.addItems(items)
    return combo


class ParametersPage(QWidget):
    """作品参数编辑页。对外只暴露 dirty / 参数字典 / 保存与加载。"""

    dirty_changed = Signal(bool)
    output_dir_changed = Signal(str)
    saved = Signal(str)               # 写入路径
    work_title_changed = Signal(str)
    operation_failed = Signal(str, str)  # 标题、原始错误信息

    CORE_DISPLAY_KEYS = {
        "Output Directory", "Genre", "Subgenre", "Story Length", "Story Structure",
        "Novel Title", "Author Name", "Theme", "Tone",
        "Gender Generation Bias String", "Quality Loop", "Backend", "Model",
    }

    def __init__(self, get_backend: Callable[[], str] = lambda: "",
                 get_model: Callable[[], str] = lambda: "",
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._get_backend = get_backend
        self._get_model = get_model
        self._dynamic_widgets: Dict[str, QWidget] = {}
        self._loading = True          # 载入期间不触发 dirty
        self._dirty = False

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
        outer.addWidget(self._build_right_column(), 1)
        return body

    def _build_core_card(self) -> QWidget:
        card = Card("核心信息")
        card.setFixedWidth(520)

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
        self.author_edit = QLineEdit()
        rows.add("作者姓名", self.author_edit)

        self.length_combo = _combo([zh_label(x) for x in story_options.LENGTH_OPTIONS])
        rows.add("故事篇幅", self.length_combo)

        self.structure_combo = _combo()
        rows.add("故事结构", self.structure_combo)

        self.theme_edit = QLineEdit()
        rows.add("主题", self.theme_edit)
        self.tone_edit = QLineEdit()
        rows.add("基调", self.tone_edit)

        self.gender_combo = _combo([
            GENDER_BIAS_ZH_LABELS.get(option, option)
            for option in story_options.GENDER_BIAS_MAP
        ])
        rows.add("性别比例", self.gender_combo)

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

    def _build_right_column(self) -> QWidget:
        column = QWidget()
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        # --- 题材 ---
        genre_card = Card("题材")
        genre_grid = QGridLayout()
        genre_grid.setContentsMargins(0, 0, 0, 0)
        genre_grid.setHorizontalSpacing(theme.SP[4])
        genre_grid.setVerticalSpacing(theme.SP[2])

        genre_label = QLabel("题材")
        genre_label.setObjectName("FieldLabel")
        sub_label = QLabel("子题材")
        sub_label.setObjectName("FieldLabel")
        self.genre_combo = _combo([zh_label(g) for g in story_options.supported_genres()])
        self.subgenre_combo = _combo()
        genre_grid.addWidget(genre_label, 0, 0)
        genre_grid.addWidget(sub_label, 0, 1)
        genre_grid.addWidget(self.genre_combo, 1, 0)
        genre_grid.addWidget(self.subgenre_combo, 1, 1)
        genre_grid.setColumnStretch(0, 1)
        genre_grid.setColumnStretch(1, 1)
        genre_card.body.addLayout(genre_grid)
        genre_card.body.addWidget(
            hint_label("切换子题材会重建下方的设定项，未保存的填写将被清空。")
        )
        layout.addWidget(genre_card)

        # --- 质量闭环 ---
        quality_right = QLabel("影响生成耗时与调用量")
        quality_right.setObjectName("Tertiary")
        quality_card = Card("质量闭环", quality_right)
        quality_grid = QGridLayout()
        quality_grid.setContentsMargins(0, 0, 0, 0)
        quality_rows = LabeledRow(quality_grid)
        self.quality_combo = _combo([
            QUALITY_LOOP_LABELS[mode] for mode in QUALITY_LOOP_MODES
        ])
        quality_rows.add("档位", self.quality_combo)
        self.quality_hint = hint_label("")
        quality_rows.add_full(self.quality_hint)
        quality_card.body.addLayout(quality_grid)
        layout.addWidget(quality_card)

        # --- 动态页签 ---
        self.dynamic_card = Card()
        self.dynamic_tabs = QTabWidget()
        self.dynamic_tabs.setDocumentMode(True)
        self.dynamic_card.body.setContentsMargins(0, 0, 0, 0)
        self.dynamic_card.body.addWidget(self.dynamic_tabs)
        layout.addWidget(self.dynamic_card, 1)

        self.genre_combo.currentIndexChanged.connect(self._on_genre_changed)
        self.subgenre_combo.currentIndexChanged.connect(self._on_subgenre_changed)
        self.quality_combo.currentIndexChanged.connect(self._on_quality_changed)
        return column

    # ================================================== 事件
    def _wire_dirty(self) -> None:
        for edit in (self.output_dir_edit, self.title_edit, self.author_edit,
                     self.theme_edit, self.tone_edit):
            edit.textChanged.connect(self._mark_dirty)
        for combo in (self.length_combo, self.structure_combo, self.genre_combo,
                      self.subgenre_combo, self.gender_combo, self.quality_combo):
            combo.currentIndexChanged.connect(self._mark_dirty)

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
        length = self._internal_length()
        options = story_options.structures_for(length)
        current = self._internal_structure()
        self.structure_combo.blockSignals(True)
        self.structure_combo.clear()
        self.structure_combo.addItems([zh_label(o) for o in options])
        self.structure_combo.setEnabled(bool(options))
        target = current if current in options else story_options.default_structure_for(length)
        if target in options:
            self.structure_combo.setCurrentIndex(options.index(target))
        self.structure_combo.blockSignals(False)

    def _on_genre_changed(self, *_: Any) -> None:
        self._populate_subgenres()
        self._rebuild_dynamic_tabs()

    def _on_subgenre_changed(self, *_: Any) -> None:
        self._rebuild_dynamic_tabs()

    def _on_quality_changed(self, *_: Any) -> None:
        self.quality_hint.setText(QUALITY_LOOP_HINTS.get(self._quality_mode(), ""))

    # ================================================== 下拉内容
    def _populate_subgenres(self) -> None:
        genre = self._internal_genre()
        options = story_options.subgenres_for(genre)
        current = self._internal_subgenre()
        self.subgenre_combo.blockSignals(True)
        self.subgenre_combo.clear()
        self.subgenre_combo.addItems([zh_label(o) for o in options])
        if options:
            index = options.index(current) if current in options else 0
            self.subgenre_combo.setCurrentIndex(index)
        self.subgenre_combo.blockSignals(False)

    def _rebuild_dynamic_tabs(self) -> None:
        """按 genre/subgenre 重建右下页签（对应旧版 update_dynamic_tabs）。"""
        self.dynamic_tabs.clear()
        self._dynamic_widgets.clear()

        genre, subgenre = self._internal_genre(), self._internal_subgenre()
        config = {}
        if genre and subgenre:
            from core.config.genre_configs import get_genre_config

            config = get_genre_config(genre, subgenre) or {}

        if not config:
            empty = QWidget()
            empty_layout = QVBoxLayout(empty)
            empty_layout.setContentsMargins(20, 20, 20, 20)
            empty_layout.addWidget(hint_label("该子题材没有专用设置。"))
            empty_layout.addStretch(1)
            self.dynamic_tabs.addTab(empty, "设置")
            return

        if "implied_settings" in config:
            self.dynamic_tabs.addTab(
                self._dynamic_tab(config["implied_settings"]), "世界设定")
        if "protagonist_types" in config:
            self.dynamic_tabs.addTab(
                self._dynamic_tab({"Protagonist Type": config["protagonist_types"]}), "角色")
        if "conflict_scales" in config:
            self.dynamic_tabs.addTab(
                self._dynamic_tab({"Conflict Scale": config["conflict_scales"]}), "冲突")

    def _dynamic_tab(self, settings: Dict[str, Any]) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(20, 18, 20, 18)
        page_layout.setSpacing(theme.SP[3])

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        rows = LabeledRow(grid)

        for name, value in settings.items():
            key = name.lower().replace(" ", "_")
            widget = self._dynamic_widget(value)
            self._dynamic_widgets[key] = widget
            rows.add(zh_label(name.replace("_", " ").title()), widget)

        page_layout.addLayout(grid)
        page_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def _dynamic_widget(self, value: Any) -> QWidget:
        if isinstance(value, bool):
            widget = QCheckBox()
            widget.setChecked(value)
            widget.stateChanged.connect(self._mark_dirty)
            return widget
        if isinstance(value, (list, tuple)):
            widget = _combo([str(v) for v in value])
            widget.currentIndexChanged.connect(self._mark_dirty)
            return widget
        widget = QLineEdit(str(value))
        widget.textChanged.connect(self._mark_dirty)
        return widget

    # ================================================== 取值（中文显示 -> 内部标识）
    @staticmethod
    def _internal(display: str, options) -> str:
        for option in options:
            if zh_label(option) == display:
                return option
        return display

    def _internal_genre(self) -> str:
        return self._internal(self.genre_combo.currentText(), story_options.supported_genres())

    def _internal_subgenre(self) -> str:
        return self._internal(self.subgenre_combo.currentText(),
                              story_options.subgenres_for(self._internal_genre()))

    def _internal_length(self) -> str:
        return self._internal(self.length_combo.currentText(), story_options.LENGTH_OPTIONS)

    def _internal_structure(self) -> str:
        return self._internal(self.structure_combo.currentText(),
                              story_options.structures_for(self._internal_length()))

    def _internal_gender(self) -> str:
        return internal_label(self.gender_combo.currentText(), GENDER_BIAS_ZH_LABELS) \
            if GENDER_BIAS_ZH_LABELS else self.gender_combo.currentText()

    def _quality_mode(self) -> str:
        label = self.quality_combo.currentText()
        return QUALITY_LOOP_FROM_LABEL.get(label, DEFAULT_QUALITY_LOOP_MODE)

    # ================================================== 对外
    def output_dir(self) -> str:
        return self.output_dir_edit.text().strip() or "current_work"

    def parameters(self) -> Dict[str, Any]:
        """与旧版 get_current_parameters 返回同构的字典。"""
        gender_string = self._internal_gender()
        female_p, male_p = story_options.GENDER_BIAS_MAP.get(gender_string, (50, 50))

        params: Dict[str, Any] = {
            "output_directory": self.output_dir(),
            "genre": self._internal_genre(),
            "subgenre": self._internal_subgenre(),
            "story_length": self._internal_length(),
            "story_structure": self._internal_structure(),
            "novel_title": self.title_edit.text().strip(),
            "author_name": self.author_edit.text().strip(),
            "theme": self.theme_edit.text().strip(),
            "tone": self.tone_edit.text().strip(),
            "quality_loop": self._quality_mode(),
            "backend": self._get_backend(),
            "model": self._get_model(),
            "gender_generation_bias_string": gender_string,
            "female_percentage": female_p,
            "male_percentage": male_p,
        }
        for key, widget in self._dynamic_widgets.items():
            if isinstance(widget, QCheckBox):
                params[key] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                params[key] = widget.currentText()
            elif isinstance(widget, QLineEdit):
                params[key] = widget.text().strip()
        return params

    def validate(self) -> Dict[str, str]:
        """返回 {字段名: 错误文案}；空字典表示通过。逐字段标红由 save() 施加。"""
        errors: Dict[str, str] = {}
        if not self.output_dir_edit.text().strip():
            errors["output_directory"] = "输出目录不能为空"
        if not self.title_edit.text().strip():
            errors["novel_title"] = "作品标题不能为空"
        if not self._internal_structure():
            errors["story_structure"] = "请先选择故事篇幅与结构"
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
        self.dirty_changed.emit(False)
        self.saved.emit(path)
        return True

    def load(self, output_dir: Optional[str] = None) -> None:
        self._loading = True
        target = output_dir or (self.output_dir_edit.text().strip() or "current_work")
        loaded = {}
        try:
            loaded = params_store.load(target)
        except Exception as exc:  # noqa: BLE001
            logger.error("读取参数失败：%s", exc, exc_info=True)
            self._loading = False
            self.operation_failed.emit("读取失败", str(exc) or type(exc).__name__)
            return

        self.output_dir_edit.setText(loaded.get("Output Directory", target))

        genre = loaded.get("Genre", "Sci-Fi")
        self._set_combo(self.genre_combo, zh_label(genre))
        self._populate_subgenres()
        subgenre = loaded.get("Subgenre", "")
        if subgenre:
            self._set_combo(self.subgenre_combo, zh_label(subgenre))

        length = loaded.get("Story Length", story_options.LENGTH_OPTIONS[0])
        self._set_combo(self.length_combo, zh_label(length))
        self._on_length_changed()
        structure = loaded.get("Story Structure", "")
        if structure:
            self._set_combo(self.structure_combo, zh_label(structure))

        self.title_edit.setText(loaded.get("Novel Title", ""))
        self.author_edit.setText(loaded.get("Author Name", ""))
        self.theme_edit.setText(loaded.get("Theme", ""))
        self.tone_edit.setText(loaded.get("Tone", ""))

        mode = str(loaded.get("Quality Loop", DEFAULT_QUALITY_LOOP_MODE)).strip()
        mode = QUALITY_LOOP_FROM_LABEL.get(mode, mode)
        if mode not in QUALITY_LOOP_MODES:
            mode = DEFAULT_QUALITY_LOOP_MODE
        self._set_combo(self.quality_combo, QUALITY_LOOP_LABELS[mode])
        self._on_quality_changed()

        bias = loaded.get("Gender Generation Bias String",
                          next(iter(story_options.GENDER_BIAS_MAP)))
        self._set_combo(self.gender_combo, GENDER_BIAS_ZH_LABELS.get(bias, bias))

        self._rebuild_dynamic_tabs()
        for display, value in loaded.items():
            if display in self.CORE_DISPLAY_KEYS:
                continue
            widget = self._dynamic_widgets.get(display.lower().replace(" ", "_"))
            if widget is None:
                continue
            if isinstance(widget, QCheckBox):
                widget.setChecked(str(value).lower() == "true")
            elif isinstance(widget, QComboBox):
                self._set_combo(widget, str(value))
            elif isinstance(widget, QLineEdit):
                widget.setText(str(value))

        self._apply_field_errors({})
        self._loading = False
        self._dirty = False
        self.dirty_changed.emit(False)
        self.work_title_changed.emit(self.title_edit.text().strip())

    # ================================================== 内部
    @staticmethod
    def _set_combo(combo: QComboBox, text: str) -> None:
        index = combo.findText(text)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _apply_field_errors(self, errors: Dict[str, str]) -> None:
        mapping = {
            "output_directory": self.output_dir_edit,
            "novel_title": self.title_edit,
            "story_structure": self.structure_combo,
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
