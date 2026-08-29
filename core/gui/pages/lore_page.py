# -*- coding: utf-8 -*-
"""阶段 2 · 世界设定。

左侧是产物浏览器而不是又一个表单：这一步用户真正要做的事是读生成结果、
决定要不要重跑。生成参数与分步按钮收进右栏检查器。
规格见 docs/UI_CONTRACT.md 第 6 节。
"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from core.gui import theme
from core.gui.pages.stage_base import StagePage
from core.gui.services import artifacts
from core.gui.services.step_runner import action_work, step_work
from core.gui.widgets import (
    Card,
    Stepper,
    hint_label,
    primary_button,
    secondary_button,
)
from core.gui.widgets.inspector import StepButton


class MarkdownView(QTextBrowser):
    """只读 GitHub Markdown 视图，供设定、结构和场景页面共用。"""

    def __init__(self, text: str = "", serif: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFrameShape(QTextBrowser.NoFrame)
        self.setOpenExternalLinks(False)
        family = theme.SERIF_STACK if serif else theme.SANS_STACK
        size = 15 if serif else theme.FS_MD
        self.content_font_size = size
        self.setStyleSheet(
            f"QTextBrowser {{ background: {theme.CANVAS}; border: none;"
            f" font-family: {family}; font-size: {size}px; color: {theme.INK_900};"
            f" padding: 4px 0; }}"
        )
        self.document().setDefaultStyleSheet(
            f"body, p, li {{ font-family: {family}; font-size: {size}px;"
            f" color: {theme.INK_900}; line-height: 1.65; }}"
            f"h1, h2, h3, h4, h5, h6 {{ font-family: {family};"
            f" color: {theme.INK_900}; margin-top: 14px; margin-bottom: 8px; }}"
            f"blockquote {{ color: {theme.INK_600}; border-left: 3px solid {theme.LINE_STRONG};"
            f" margin-left: 8px; padding-left: 12px; }}"
            f"code, pre {{ background: {theme.SURFACE}; font-family: Consolas, monospace; }}"
        )
        self.set_markdown(text)

    def set_markdown(self, text: str) -> None:
        # QTextBrowser.setMarkdown() 在当前 PySide6 绑定中只有单参数重载；
        # QTextDocument 暴露了 features 参数，可明确启用 GitHub 方言。
        self.document().setMarkdown(
            text or "（暂无内容）",
            QTextDocument.MarkdownDialectGitHub,
        )


def read_only_text(text: str, serif: bool = False) -> MarkdownView:
    return MarkdownView(text, serif)


class EntityCard(QWidget):
    def __init__(self, entity: artifacts.Entity, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            f"background: {theme.CANVAS}; border: 1px solid {theme.LINE};"
            f"border-radius: 5px;"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 13, 15, 13)
        layout.setSpacing(7)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(10)
        name = QLabel(entity.name)
        name.setStyleSheet(
            f"font-family: {theme.SERIF_STACK}; font-size: 14px; font-weight: 600;"
            f"border: none;")
        kind = QLabel(entity.kind)
        kind.setStyleSheet(
            f"background: {theme.SURFACE}; border: 1px solid {theme.LINE};"
            f"border-radius: 10px; padding: 2px 8px; font-size: {theme.FS_XS}px;"
            f"color: {theme.INK_600};")
        top.addWidget(name, 1)
        top.addWidget(kind, 0)
        layout.addLayout(top)

        summary = QLabel(entity.summary or "（该条目没有描述字段）")
        summary.setWordWrap(True)
        summary.setStyleSheet(
            f"font-size: {theme.FS_SM}px; color: {theme.INK_600}; border: none;")
        layout.addWidget(summary)


class LorePage(StagePage):
    stage_key = "lore"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("世界设定", "世界构建与背景")

        self.titles_button = secondary_button("推荐作品标题")
        self.enhance_button = secondary_button("完善主要人物")
        self.generate_button = primary_button("生成世界设定")
        for button in (self.titles_button, self.enhance_button, self.generate_button):
            self.header.add_action(button)
        self.titles_button.clicked.connect(
            lambda: self._run_step("titles", self.titles_button))
        self.enhance_button.clicked.connect(
            lambda: self._run_step("enhance", self.enhance_button))
        self.generate_button.clicked.connect(
            lambda: self._run_step("full", self.generate_button))

        self._build_left()
        self._build_inspector()
        self.refresh()

    # ---------------------------------------------------------------- 左侧
    def _build_left(self) -> None:
        container = Card()
        container.body.setContentsMargins(0, 0, 0, 0)
        container.body.setSpacing(0)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        container.body.addWidget(self.tabs)
        self.set_left(container)

    def _entity_tab(self, entities: List[artifacts.Entity], empty_hint: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        if not entities:
            layout.addWidget(hint_label(empty_hint))
        for entity in entities:
            layout.addWidget(EntityCard(entity))
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def _text_tab(self, text: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.addWidget(read_only_text(text, serif=True))
        return page

    # ---------------------------------------------------------------- 右侧
    def _build_inspector(self) -> None:
        params = Card("生成参数")
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(theme.SP[3])
        grid.setVerticalSpacing(theme.SP[3])

        self.faction_stepper = Stepper(6, 1, 10)
        self.character_stepper = Stepper(8, 1, 10)
        for row, (label, widget) in enumerate([
            ("势力数量", self.faction_stepper),
            ("人物数量", self.character_stepper),
        ]):
            caption = QLabel(label)
            caption.setObjectName("FieldLabel")
            grid.addWidget(caption, row, 0)
            grid.addWidget(widget, row, 1, Qt.AlignRight)
        grid.setColumnStretch(0, 1)
        params.body.addLayout(grid)
        params.body.addWidget(hint_label("题材附加参数随子题材变化，上限 10。"))
        self.add_card(params)

        self.steps_meta = QLabel("")
        self.steps_meta.setObjectName("Tertiary")
        steps = Card("分步生成", self.steps_meta)
        self.step_buttons = {
            "factions": StepButton("factions", "生成势力"),
            "characters": StepButton("characters", "生成人物"),
            "lore": StepButton("lore", "生成世界观"),
            "enhance": StepButton("enhance", "完善主要人物", "可选"),
        }
        for button in self.step_buttons.values():
            button.clicked.connect(self._run_step)
            steps.body.addWidget(button)
        self.add_card(steps)

        self.files_meta = QLabel("")
        self.files_meta.setObjectName("Tertiary")
        outputs = Card("产物", self.files_meta)
        self.open_folder_button = secondary_button("打开设定文件夹", "folder")
        self.open_folder_button.clicked.connect(self._open_folder)
        outputs.body.addWidget(self.open_folder_button)
        self.add_card(outputs)

        self.finish_inspector()

    # ---------------------------------------------------------------- 行为
    def _run_step(self, key: str, busy_button=None) -> None:
        labels = {
            "factions": "正在生成势力…",
            "characters": "正在生成人物…",
            "lore": "正在生成世界观…",
            "enhance": "正在完善主要人物…",
            "titles": "正在推荐标题…",
            "full": "正在生成设定…",
        }
        work = (
            step_work("lore", self.output_dir)
            if key == "full"
            else action_work(
                "lore",
                key,
                self.output_dir,
                parameters={
                    "num_factions": self.faction_stepper.value(),
                    "num_characters": self.character_stepper.value(),
                },
            )
        )
        self.run_task(
            work,
            busy_button=busy_button or self.step_buttons.get(key, self.generate_button),
            busy_text=labels[key],
            on_success=lambda _: (self.refresh(), self.artifacts_changed.emit()),
        )

    def _open_folder(self) -> None:
        import os
        import subprocess
        import sys

        path = os.path.join(self.output_dir, artifacts.LORE_DIR)
        if not os.path.isdir(path):
            self.status_message.emit("warn", f"目录还不存在：{path}")
            return
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606 - 打开资源管理器
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    # ---------------------------------------------------------------- 刷新
    def refresh(self) -> None:
        factions = artifacts.factions(self.output_dir)
        characters = artifacts.characters(self.output_dir)
        lore_text = artifacts.lore_text(self.output_dir)
        titles = artifacts.suggested_titles(self.output_dir)
        backgrounds = artifacts.background_files(self.output_dir)
        total = artifacts.lore_file_count(self.output_dir)

        current = self.tabs.currentIndex()
        self.tabs.clear()
        self.tabs.addTab(self._text_tab(lore_text), "世界观")
        self.tabs.addTab(self._entity_tab(factions, "尚未生成势力。"),
                         f"势力 {len(factions)}" if factions else "势力")
        self.tabs.addTab(self._entity_tab(characters, "尚未生成人物。"),
                         f"人物 {len(characters)}" if characters else "人物")
        if titles.strip():
            self.tabs.addTab(self._text_tab(titles), "候选标题")
        if 0 <= current < self.tabs.count():
            self.tabs.setCurrentIndex(current)

        self.step_buttons["factions"].set_state(
            "done" if factions else "todo", f"{len(factions)} 个" if factions else "")
        self.step_buttons["characters"].set_state(
            "done" if characters else "todo", f"{len(characters)} 个" if characters else "")
        self.step_buttons["lore"].set_state(
            "done" if lore_text.strip() else "todo", "1 篇" if lore_text.strip() else "")
        self.step_buttons["enhance"].set_state(
            "done" if backgrounds else ("todo" if characters else "locked"),
            f"{len(backgrounds)} 份" if backgrounds else
            ("可选" if characters else "需先生成人物"))

        self.enhance_button.setEnabled(bool(characters))
        self.enhance_button.setToolTip("" if characters else "需先生成人物。")
        self.titles_button.setEnabled(bool(lore_text.strip()))
        self.titles_button.setToolTip("" if lore_text.strip() else "需先生成世界观。")

        done = sum(1 for key in ("factions", "characters", "lore")
                   if self.step_buttons[key].state == "done")
        self.steps_meta.setText(f"{done} / 3 已完成")
        self.files_meta.setText(f"{total} 个文件")
        self.header.set_subtitle(
            f"{len(factions)} 个势力 · {len(characters)} 个人物 · {total} 个产物文件"
            if total else "世界构建与背景 · 尚未生成"
        )
        self.generate_button.setText("重新生成世界设定" if total else "生成世界设定")
