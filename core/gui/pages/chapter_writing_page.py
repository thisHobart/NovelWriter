# -*- coding: utf-8 -*-
"""阶段 5 · 章节撰写。

正文区衬线体、64px 内边距、1.95 行高（已与用户确认）。生成中时主按钮自身变禁用，
取消交给底部状态栏。「重写本章」按先确认再覆盖处理。
规格见 docs/UI_CONTRACT.md 第 5 节。

未过质量闸门的章节在这里也有位置：列表里单列一档「待复审」，选中后正文区显示
被拦下的那一稿并把问题句标出来，右栏列出按严重程度排好的问题与三个出口。
"""
from __future__ import annotations

from html import escape
import re
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
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
    IssueRow,
    VScrollArea,
    hint_label,
    primary_button,
    secondary_button,
)
from core.gui.widgets.inspector import StepButton

#: 短于这个长度的引用不拿去正文里匹配：几个字的片段会在全章命中一堆无关位置。
MIN_QUOTE_FRAGMENT = 6

#: 正文里标问题句用的底色。Qt 富文本只认 background-color / color 这几样，边框
#: 会被静默丢掉，所以硬伤与建议只能靠底色深浅区分。锚点也只写 name 不写 href：
#: 带 href 的会被当成链接统一加上下划线，两类标记看起来就一样了。
HARD_TINT = "#F6DCD9"
SOFT_TINT = "#FBF1DF"


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
        self.setHtml("".join(self._document(title, text, [])))

    def set_draft(self, title: str, text: str, marks: List[Tuple[str, str, str]]) -> None:
        """渲染未过闸的一稿，被点名的句子标底色。

        marks 是 (锚点名, 原文片段, 严重程度) 三元组。锚点让右栏点一条就能滚到
        正文对应位置——问题清单和正文必须能互相指认，否则清单还是要人自己去找。
        """
        self.setHtml("".join(self._document(title, text, marks)))

    # ------------------------------------------------------------------ 内部
    def _document(self, title: str, text: str,
                  marks: List[Tuple[str, str, str]]) -> List[str]:
        paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
        html = [f"<h2>{escape(title)}</h2>"]
        body_count = 0
        for paragraph in paragraphs:
            if paragraph.startswith("#"):
                continue  # 标题已单独渲染
            html.append(f"<p>{self._mark(paragraph, marks)}</p>")
            body_count += 1
        if not body_count:
            html.append('<p class="pending">（该章尚无正文）</p>')
        return html

    @staticmethod
    def _mark(paragraph: str, marks: List[Tuple[str, str, str]]) -> str:
        """在转义后的段落里给命中的片段套上底色与锚点。

        匹配放在转义之后、用同样转义过的片段来做，避免把 `&amp;` 这类实体从中间
        切开；一段里同一片段只标第一处，够用来定位，也不至于满屏底色。
        """
        rendered = escape(paragraph)
        for anchor, fragment, kind in marks:
            needle = escape(fragment)
            if len(needle) < MIN_QUOTE_FRAGMENT or needle not in rendered:
                continue
            tint = HARD_TINT if kind == "hard" else SOFT_TINT
            replacement = (
                f'<a name="{anchor}"></a>'
                f'<span style="background-color: {tint}; color: {theme.INK_900};">'
                f'{needle}</span>'
            )
            rendered = rendered.replace(needle, replacement, 1)
        return rendered


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
        self._pending: Dict[int, Any] = {}
        #: 列表每一行是什么：("chapter", 章号) / ("review", 章号) / ("todo", 章号)
        self._rows: List[Tuple[str, int]] = []
        self._selected: Tuple[str, int] = ("none", 0)
        self._issue_rows: List[IssueRow] = []
        self._running_chapter: Optional[int] = None
        self._generation_active = False

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

        # 未过闸的稿子必须自报家门，否则和已定稿的正文在同一个位置长得一样。
        self.draft_banner = QLabel("")
        self.draft_banner.setWordWrap(True)
        self.draft_banner.setAttribute(Qt.WA_StyledBackground, True)
        self.draft_banner.setContentsMargins(20, 10, 20, 10)
        self.draft_banner.setStyleSheet(
            f"background: #FBF1DF; color: {theme.WARN};"
            f" font-size: {theme.FS_SM}px;"
            f" border-bottom: 1px solid {theme.LINE};"
        )
        self.draft_banner.hide()
        card.body.addWidget(self.draft_banner)

        self.prose = ProseView()
        card.body.addWidget(self.prose, 1)
        self.set_left(card)

    # ---------------------------------------------------------------- 右侧
    def _build_inspector(self) -> None:
        self.list_meta = QLabel("")
        self.list_meta.setObjectName("Tertiary")
        chapters_card = Card("章节", self.list_meta, padding=(8, 8, 8, 8))
        self.chapter_list = ArtifactList()
        self.chapter_list.setMinimumHeight(120)
        self.chapter_list.currentRowChanged.connect(self._on_row_selected)
        chapters_card.body.addWidget(self.chapter_list)
        self.add_card(chapters_card, stretch=1)

        # 有章节待复审时，问题清单才是这一屏的重点，章节列表让位。
        self.review_card = self._build_review_card()
        self.add_card(self.review_card, stretch=3)
        self.review_card.hide()

        self.quality_card = Card("质量闭环")
        self.quality_summary = QLabel("")
        self.quality_summary.setWordWrap(True)
        self.quality_summary.setObjectName("Secondary")
        self.quality_card.body.addWidget(self.quality_summary)
        self.add_card(self.quality_card)

        # 「撰写下一章」只保留页头主按钮这一个入口；右栏只放页头没有的批量动作，
        # 避免同一动作出现两次（规格见 docs/UI_CONTRACT.md 第 5 节）。
        actions = Card("操作")
        self.step_buttons = {
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

    def _build_review_card(self) -> Card:
        card = Card("复审", padding=(12, 12, 12, 12))

        self.review_verdict = QLabel("")
        self.review_verdict.setWordWrap(True)
        self.review_verdict.setStyleSheet(
            f"font-size: {theme.FS_MD}px; font-weight: 600; color: {theme.DANGER};")
        card.body.addWidget(self.review_verdict)

        self.review_scope = hint_label("")
        card.body.addWidget(self.review_scope)

        holder = QWidget()
        self._issues_layout = QVBoxLayout(holder)
        self._issues_layout.setContentsMargins(0, 0, 0, 0)
        self._issues_layout.setSpacing(6)
        self._issues_layout.addStretch(1)

        self._issues_scroll = VScrollArea()
        self._issues_scroll.setWidget(holder)
        self._issues_scroll.setMinimumHeight(200)
        card.body.addWidget(self._issues_scroll, 1)

        self.revise_button = primary_button("照建议重修")
        self.waive_button = secondary_button("人工放行")
        self.replan_button = secondary_button("回去改规划")
        self.revise_button.clicked.connect(self._revise_pending)
        self.waive_button.clicked.connect(self._waive_pending)
        self.replan_button.clicked.connect(
            lambda: self.navigate_requested.emit("scenes"))
        # 三个出口并排一行：竖排要多吃七十来像素，那点高度留给问题清单更值钱。
        exits = QHBoxLayout()
        exits.setContentsMargins(0, 0, 0, 0)
        exits.setSpacing(6)
        for button in (self.revise_button, self.waive_button, self.replan_button):
            exits.addWidget(button)
        exits.addStretch(1)
        card.body.addLayout(exits)
        return card

    # ---------------------------------------------------------------- 行为
    def _on_step_clicked(self, key: str) -> None:
        if key == "all":
            self._write_all()

    def _write_next(self) -> None:
        self._dispatch("next", "撰写下一章…")

    def _write_all(self) -> None:
        self._dispatch("all", "撰写全部剩余章节…")

    def _rewrite_current(self) -> None:
        kind, number = self._selected
        if kind not in ("chapter", "review"):
            self.status_message.emit("warn", "还没有可重写的章节。")
            return
        if kind == "review":
            question = f"第 {number} 章现有的一稿没过质量闸门，重写会丢掉它。继续吗？"
        else:
            chapter = self._chapter(number)
            words = chapter.words if chapter else 0
            question = f"第 {number} 章已有 {words} 字，重写会覆盖现有正文。继续吗？"
        answer = QMessageBox.question(
            self, "重写本章", question,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self._dispatch("rewrite", f"正在重写第 {number} 章…", chapter_number=number)

    def _analyze(self) -> None:
        """只读操作：统计当前章节产出，不进 busy 态。"""
        if not self._chapters:
            self.status_message.emit("warn", "尚无章节可分析。")
            return
        total = sum(c.words for c in self._chapters)
        expected = artifacts.expected_chapter_count(self.output_dir)
        shortest = min(self._chapters, key=lambda c: c.words)
        message = (
            f"共 {len(self._chapters)} / {expected or '?'} 章，累计 {total:,} 字，"
            f"最短为第 {shortest.number} 章（{shortest.words} 字）。"
        )
        if self._pending:
            waiting = "、".join(f"第 {n} 章" for n in sorted(self._pending))
            message += f" {waiting}待复审。"
        self.status_message.emit("info", message)

    # -------------------------------------------------------------- 复审出口
    def _revise_pending(self) -> None:
        kind, number = self._selected
        record = self._pending.get(number) if kind == "review" else None
        if record is None:
            return
        chosen = [row.issue_id for row in self._issue_rows if row.checked]
        selectable = [row.issue_id for row in self._issue_rows if row.checkbox]
        if selectable and not [i for i in chosen if i in selectable]:
            self.status_message.emit("warn", "至少要留一条建议，重修才有依据。")
            return
        self._dispatch(
            "revise",
            f"正在按建议重修第 {number} 章…",
            chapter_number=number,
            options={"issue_ids": chosen},
        )

    def _waive_pending(self) -> None:
        kind, number = self._selected
        record = self._pending.get(number) if kind == "review" else None
        if record is None:
            return
        reason, confirmed = QInputDialog.getText(
            self, "人工放行",
            f"第 {number} 章将带着 {record.headline} 写进正式稿件。\n"
            "请写下放行理由（会记进账本，事后可查）：",
        )
        if not confirmed:
            return
        self._dispatch(
            "waive",
            f"正在放行第 {number} 章…",
            chapter_number=number,
            options={"reason": reason},
        )

    def _dispatch(self, action: str, busy_text: str,
                  chapter_number: Optional[int] = None,
                  options: Optional[Dict[str, Any]] = None) -> None:
        self._running_chapter = None
        self.run_task(
            action_work(
                "chapters",
                action,
                self.output_dir,
                chapter_number=chapter_number,
                options=options,
            ),
            busy_button=self.primary,
            busy_text=busy_text,
            on_success=lambda _: (self.refresh(), self.artifacts_changed.emit()),
        )

    def handle_generation_progress(self, text: str) -> None:
        """Refresh durable chapter progress at worker-reported safe boundaries."""
        started = re.fullmatch(
            r"正在撰写第\s*(\d+)\s*章（已完成\s*(\d+)/(\d+)）",
            text.strip(),
        )
        completed = re.fullmatch(
            r"第\s*(\d+)\s*章已完成（(\d+)/(\d+)）",
            text.strip(),
        )
        if started:
            self._generation_active = True
            self._running_chapter = int(started.group(1))
        elif completed:
            self._generation_active = True
            self._running_chapter = None
        else:
            return
        # 该方法只由 TaskRunner 的 queued progress 信号在 Qt 主线程调用。
        self.refresh()

    def finish_generation_progress(self) -> None:
        """Clear a running row after success, failure, or cancellation."""
        if not self._generation_active and self._running_chapter is None:
            # 生成失败会新写一份待复审记录，列表要立刻反映出来。
            self.refresh()
            return
        self._generation_active = False
        self._running_chapter = None
        self.refresh()

    # ---------------------------------------------------------------- 选中
    def _chapter(self, number: int) -> Optional[artifacts.Chapter]:
        for chapter in self._chapters:
            if chapter.number == number:
                return chapter
        return None

    def _on_row_selected(self, row: int) -> None:
        if not (0 <= row < len(self._rows)):
            return
        self._selected = self._rows[row]
        kind, number = self._selected
        if kind == "review":
            self._show_pending(number)
        elif kind == "chapter":
            self._show_chapter(number)
        else:
            self._show_empty(number)
        self.rewrite_button.setEnabled(
            kind in ("chapter", "review") and not self._generation_active)

    def _show_chapter(self, number: int) -> None:
        chapter = self._chapter(number)
        if chapter is None:
            return
        self.draft_banner.hide()
        self.review_card.hide()
        self.quality_card.show()
        self.chapter_title.setText(f"第 {chapter.number} 章 · {chapter.title}")
        self.chapter_meta.setText(f"{chapter.words:,} 字    {chapter.updated}")
        self.prose.set_chapter(f"第 {chapter.number} 章", chapter.text)

    def _show_empty(self, number: int) -> None:
        self.draft_banner.hide()
        self.review_card.hide()
        self.quality_card.show()
        self.chapter_title.setText(f"第 {number} 章")
        self.chapter_meta.setText("尚未撰写")
        self.prose.set_chapter(f"第 {number} 章", "")

    def _show_pending(self, number: int) -> None:
        record = self._pending.get(number)
        if record is None:
            return
        self.quality_card.hide()
        self.review_card.show()
        self.chapter_title.setText(f"第 {number} 章 · 待复审")
        self.chapter_meta.setText(record.created_at)
        self.draft_banner.setText(
            (
                f"质量评审没能给出结论，这一稿暂不进正式稿件。{record.message}"
                if record.verdict_unavailable
                else f"这一稿没通过质量闸门，不在正式稿件里。{record.message}"
            )
        )
        self.draft_banner.show()

        marks: List[Tuple[str, str, str]] = []
        for issue in record.issues:
            for line in str(issue.quote or "").split("\n"):
                fragment = line.strip()
                if len(fragment) >= MIN_QUOTE_FRAGMENT:
                    marks.append((issue.id, fragment, issue.kind))
        self.prose.set_draft(f"第 {number} 章（未通过）", record.prose, marks)

        self.review_verdict.setText(record.verdict)
        self.review_scope.setText(
            f"位置：{record.scope}" if record.scope else "评审未指明具体位置。")
        self._fill_issues(record)

        # 评审自己没出结论时，这个按钮的含义变了：不是照清单重修，而是让评审
        # 重判一次这一稿——没有清单可照，但也不必整章重写。
        rerun = record.needs_review_rerun and not record.asks_for()
        self.revise_button.setText("重跑评审" if rerun else "照建议重修")
        self.revise_button.setEnabled(rerun or bool(record.asks_for()))
        self.revise_button.setToolTip(
            "重新评审这一稿：通过就直接收进正式稿件，不通过再按新清单重修。"
            if rerun
            else ("" if record.asks_for() else "这份评审没给出可执行的修改依据。")
        )
        self.waive_button.setEnabled(record.resumable)
        self.waive_button.setToolTip(
            "" if record.resumable
            else "这一稿只写到一半就被拦下，没有完整章节可放行；请重写本章。")

    def _fill_issues(self, record: Any) -> None:
        while self._issues_layout.count() > 1:
            item = self._issues_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._issue_rows = []
        for issue in record.issues:
            row = IssueRow(
                issue.id,
                issue.kind,
                issue.title,
                issue.detail,
                issue.change,
                selectable=issue.selectable,
            )
            row.activated.connect(self.prose.scrollToAnchor)
            self._issues_layout.insertWidget(self._issues_layout.count() - 1, row)
            # 刚插进来的控件要到下一轮事件循环才自己现身，而清单高度是按可见的
            # 行算出来的；不显式 show 一下，这一轮量出来的高度就是零。
            row.show()
            self._issue_rows.append(row)
        self._issues_scroll.sync_content_height()

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
        known_pending = set(self._pending)
        self._pending = artifacts.pending_reviews(self.output_dir)
        # 刚被拦下的那一章直接选中：出问题时第一步就该看见问题，而不是先在列表
        # 里找出哪一章变了颜色。已经知道的待复审章不抢焦点。
        appeared = sorted(set(self._pending) - known_pending)
        if appeared:
            self._selected = ("review", appeared[0])
        expected = artifacts.expected_chapter_count(self.output_dir)
        plans = len(artifacts.scene_plans(self.output_dir))

        rows = []
        self._rows = []
        for chapter in self._chapters:
            rows.append((f"{chapter.number:02d}  {chapter.title[:14]}",
                         f"{chapter.words:,} 字", "done", chapter.path))
            self._rows.append(("chapter", chapter.number))
        written_numbers = {chapter.number for chapter in self._chapters}
        for number in range(1, (expected or 0) + 1):
            if number in written_numbers:
                continue
            if number == self._running_chapter:
                rows.append((f"{number:02d}  正在撰写", "", "running", ""))
                self._rows.append(("running", number))
            elif number in self._pending:
                rows.append((f"{number:02d}  待复审",
                             self._pending[number].headline, "review", ""))
                self._rows.append(("review", number))
            else:
                rows.append((f"{number:02d}  待撰写", "", "todo", ""))
                self._rows.append(("todo", number))
        # 重填期间屏蔽选中信号：清空旧行时 Qt 会把当前行往后挪并逐次上报，那些
        # 行号指的是刚被替换掉的旧列表，落到新的 _rows 上就是另一章，选中会莫名
        # 其妙跳走。选哪一行由 _restore_selection 一处说了算。
        self.chapter_list.blockSignals(True)
        self.chapter_list.set_items(rows)
        self.chapter_list.blockSignals(False)
        self._restore_selection()

        written = len(self._chapters)
        self.list_meta.setText(f"{written} / {expected or '?'}")
        total_words = sum(c.words for c in self._chapters)

        locked = plans == 0
        self.step_buttons["all"].set_state(
            "locked" if locked else "todo",
            "待解锁" if locked else (f"剩 {max(0, (expected or 0) - written)} 章"
                                  if expected else ""))
        self.primary.setEnabled(not locked)
        self.primary.setToolTip("需先完成场景规划。" if locked else "")
        self.rewrite_button.setEnabled(self._selected[0] in ("chapter", "review"))

        if self._generation_active:
            self.primary.setEnabled(False)
            self.rewrite_button.setEnabled(False)
            for button in self.step_buttons.values():
                button.set_state(
                    "running",
                    (f"第 {self._running_chapter} 章"
                     if self._running_chapter is not None else "验收已落盘"),
                )

        self.quality_summary.setText(
            f"已写 {written} 章、累计 {total_words:,} 字。"
            f"场景规划 {plans} 份，评审记录在 quality/reviews 下。"
            if written else
            ("场景规划尚未生成，章节撰写被阻塞。" if locked else "尚未开始撰写。")
        )
        self.header.set_subtitle(self._subtitle(expected, written, total_words, locked))

    def _subtitle(self, expected: int, written: int, total_words: int,
                  locked: bool) -> str:
        if self._pending:
            waiting = "、".join(str(n) for n in sorted(self._pending))
            return f"第 {waiting} 章待复审 · 已完成 {written} 章 · {total_words:,} 字"
        if written:
            return (f"共 {expected or '?'} 章 · 已完成 {written} 章 · "
                    f"{total_words:,} 字")
        return "被阻塞 · 需先完成场景规划" if locked else "最终故事正文"

    def _restore_selection(self) -> None:
        """刷新后尽量停在原来那一行；原来那行没了就优先停在待复审的一章。"""
        if not self._rows:
            self._selected = ("none", 0)
            self.chapter_title.setText("尚无章节")
            self.chapter_meta.setText("")
            self.draft_banner.hide()
            self.review_card.hide()
            self.prose.set_chapter("尚无章节", "")
            return
        target = self._rows.index(self._selected) if self._selected in self._rows else -1
        if target < 0:
            review_rows = [i for i, row in enumerate(self._rows) if row[0] == "review"]
            target = review_rows[0] if review_rows else 0
        self.chapter_list.setCurrentRow(target)
        self._on_row_selected(target)
