# -*- coding: utf-8 -*-
"""阶段 5 · 章节撰写。

正文区衬线体、64px 内边距、1.95 行高（已与用户确认）。生成中时主按钮自身变禁用，
取消交给底部状态栏。「重写本章」按先确认再覆盖处理。
规格见 docs/UI_CONTRACT.md 第 5 节。
"""
from __future__ import annotations

from html import escape
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.gui import icons, theme
from core.gui.pages.stage_base import StagePage
from core.gui.services import artifacts
from core.gui.services.step_runner import action_work
from core.gui.widgets import (
    ArtifactList,
    Card,
    hint_label,
    primary_button,
    secondary_button,
)
from core.gui.widgets.inspector import StepButton


class ProseView(QTextEdit):
    """只读正文区：衬线体、宽内边距、首行缩进由段落格式给。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFrameShape(QTextEdit.NoFrame)
        self.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.setViewportMargins(64, 28, 64, 28)
        self.document().setDefaultStyleSheet(
            f"p {{ font-family: {theme.SERIF_STACK}; font-size: 16px;"
            f" line-height: 195%; text-indent: 2em; margin: 0 0 14px 0;"
            f" color: {theme.INK_900}; }}"
            f"p.pending {{ color: {theme.INK_600}; }}"
            f"h2 {{ font-family: {theme.SERIF_STACK}; font-size: 22px;"
            f" font-weight: 600; margin: 0 0 16px 0; }}"
        )
        self.setStyleSheet(
            f"QTextEdit {{ background: {theme.CANVAS}; border: none; }}")

    def set_chapter(self, title: str, text: str) -> None:
        paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
        html = [f"<h2>{escape(title)}</h2>"]
        body_count = 0
        for paragraph in paragraphs:
            if paragraph.startswith("#"):
                continue  # 标题已单独渲染
            html.append(f"<p>{escape(paragraph)}</p>")
            body_count += 1
        if not body_count:
            html.append('<p class="pending">（该章尚无正文）</p>')
        self.setHtml("".join(html))


class ChapterWritingPage(StagePage):
    stage_key = "chapters"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("章节撰写", "最终故事正文")

        self.analyze_button = secondary_button("分析章节")
        self.rewrite_button = secondary_button("重写本章")
        self.primary = primary_button("撰写下一章")
        for button in (self.analyze_button, self.rewrite_button, self.primary):
            self.header.add_action(button)

        self.primary.clicked.connect(self._write_next)
        self.rewrite_button.clicked.connect(self._rewrite_current)
        self.analyze_button.clicked.connect(self._analyze)

        self._chapters: List[artifacts.Chapter] = []
        self._current = 0

        self._build_left()
        self._build_inspector()
        self.refresh()

    # ---------------------------------------------------------------- 左侧
    def _build_left(self) -> None:
        card = Card()
        card.body.setContentsMargins(0, 0, 0, 0)
        card.body.setSpacing(0)

        head = QWidget()
        head.setObjectName("CardHeader")
        head.setAttribute(Qt.WA_StyledBackground, True)
        head_layout = QHBoxLayout(head)
        head_layout.setContentsMargins(20, 12, 20, 12)
        head_layout.setSpacing(10)

        self.chapter_title = QLabel("尚未选择章节")
        self.chapter_title.setObjectName("CardTitle")
        self.chapter_badge = QLabel("")
        self.chapter_badge.hide()
        self.chapter_meta = QLabel("")
        self.chapter_meta.setObjectName("Tertiary")

        head_layout.addWidget(self.chapter_title)
        head_layout.addWidget(self.chapter_badge)
        head_layout.addStretch(1)
        head_layout.addWidget(self.chapter_meta)
        card.body.addWidget(head)

        self.prose = ProseView()
        card.body.addWidget(self.prose, 1)
        self.set_left(card)

    # ---------------------------------------------------------------- 右侧
    def _build_inspector(self) -> None:
        self.list_meta = QLabel("")
        self.list_meta.setObjectName("Tertiary")
        chapters_card = Card("章节", self.list_meta, padding=(8, 8, 8, 8))
        self.chapter_list = ArtifactList()
        self.chapter_list.currentRowChanged.connect(self._on_chapter_selected)
        chapters_card.body.addWidget(self.chapter_list)
        self.add_card(chapters_card, stretch=1)

        quality = Card("质量闭环")
        self.quality_summary = QLabel("")
        self.quality_summary.setWordWrap(True)
        self.quality_summary.setObjectName("Secondary")
        quality.body.addWidget(self.quality_summary)
        self.add_card(quality)

        actions = Card("操作")
        self.step_buttons = {
            "next": StepButton("next", "撰写下一章"),
            "all": StepButton("all", "撰写全部剩余章节"),
        }
        for button in self.step_buttons.values():
            button.clicked.connect(self._on_step_clicked)
            actions.body.addWidget(button)
        self.open_folder_button = secondary_button("打开章节文件夹", "folder")
        self.open_folder_button.clicked.connect(self._open_folder)
        actions.body.addWidget(self.open_folder_button)
        self.add_card(actions)

        self.finish_inspector(stretch_last=True)

    # ---------------------------------------------------------------- 行为
    def _on_step_clicked(self, key: str) -> None:
        self._write_next() if key == "next" else self._write_all()

    def _write_next(self) -> None:
        self._dispatch("next", "撰写下一章…")

    def _write_all(self) -> None:
        self._dispatch("all", "撰写全部剩余章节…")

    def _rewrite_current(self) -> None:
        if not self._chapters:
            self.status_message.emit("warn", "还没有可重写的章节。")
            return
        chapter = self._chapters[self._current]
        answer = QMessageBox.question(
            self, "重写本章",
            f"第 {chapter.number} 章已有 {chapter.words} 字，重写会覆盖现有正文。继续吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self._dispatch(
            "rewrite",
            f"正在重写第 {chapter.number} 章…",
            chapter_number=chapter.number,
        )

    def _analyze(self) -> None:
        """只读操作：统计当前章节产出，不进 busy 态。"""
        if not self._chapters:
            self.status_message.emit("warn", "尚无章节可分析。")
            return
        total = sum(c.words for c in self._chapters)
        expected = artifacts.expected_chapter_count(self.output_dir)
        shortest = min(self._chapters, key=lambda c: c.words)
        self.status_message.emit(
            "info",
            f"共 {len(self._chapters)} / {expected or '?'} 章，累计 {total:,} 字，"
            f"最短为第 {shortest.number} 章（{shortest.words} 字）。",
        )

    def _dispatch(self, action: str, busy_text: str,
                  chapter_number: Optional[int] = None) -> None:
        self.run_task(
            action_work(
                "chapters",
                action,
                self.output_dir,
                chapter_number=chapter_number,
            ),
            busy_button=self.primary,
            busy_text=busy_text,
            on_success=lambda _: (self.refresh(), self.artifacts_changed.emit()),
        )

    def _on_chapter_selected(self, row: int) -> None:
        if not (0 <= row < len(self._chapters)):
            return
        self._current = row
        chapter = self._chapters[row]
        self.chapter_title.setText(f"第 {chapter.number} 章 · {chapter.title}")
        self.chapter_meta.setText(f"{chapter.words:,} 字    {chapter.updated}")
        self.prose.set_chapter(f"第 {chapter.number} 章", chapter.text)

    def _open_folder(self) -> None:
        import os
        import subprocess
        import sys

        path = os.path.join(self.output_dir, artifacts.CHAPTERS_DIR)
        if not os.path.isdir(path):
            self.status_message.emit("warn", f"目录还不存在：{path}")
            return
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    # ---------------------------------------------------------------- 刷新
    def refresh(self) -> None:
        self._chapters = artifacts.chapters(self.output_dir)
        expected = artifacts.expected_chapter_count(self.output_dir)
        plans = len(artifacts.scene_plans(self.output_dir))

        rows = []
        for chapter in self._chapters:
            rows.append((f"{chapter.number:02d}  {chapter.title[:14]}",
                         f"{chapter.words:,} 字", "done", chapter.path))
        for number in range(len(self._chapters) + 1, (expected or 0) + 1):
            rows.append((f"{number:02d}  待撰写", "", "todo", ""))
        self.chapter_list.set_items(rows)

        if self._chapters:
            row = min(self._current, len(self._chapters) - 1)
            self.chapter_list.setCurrentRow(row)
            self._on_chapter_selected(row)
        else:
            self.chapter_title.setText("尚无章节")
            self.chapter_meta.setText("")
            self.prose.set_chapter("尚无章节", "")

        written = len(self._chapters)
        self.list_meta.setText(f"{written} / {expected or '?'}")
        total_words = sum(c.words for c in self._chapters)

        locked = plans == 0
        self.step_buttons["next"].set_state(
            "locked" if locked else "todo", "待解锁" if locked else "")
        self.step_buttons["all"].set_state(
            "locked" if locked else "todo",
            "待解锁" if locked else (f"剩 {max(0, (expected or 0) - written)} 章"
                                  if expected else ""))
        self.primary.setEnabled(not locked)
        self.primary.setToolTip("需先完成场景规划。" if locked else "")
        self.rewrite_button.setEnabled(bool(self._chapters))

        self.quality_summary.setText(
            f"已写 {written} 章、累计 {total_words:,} 字。"
            f"场景规划 {plans} 份，评审记录在 quality/reviews 下。"
            if written else
            ("场景规划尚未生成，章节撰写被阻塞。" if locked else "尚未开始撰写。")
        )
        self.header.set_subtitle(
            f"共 {expected or '?'} 章 · 已完成 {written} 章 · {total_words:,} 字"
            if written else ("被阻塞 · 需先完成场景规划" if locked else "最终故事正文"))
