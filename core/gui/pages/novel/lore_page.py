# -*- coding: utf-8 -*-
"""阶段 2 · 世界设定。

左侧只显示有内容的产物页签，并通过独立编辑窗口修改已有设定。
分步按钮收进右栏检查器；势力、人物数量按篇幅由系统定。
规格见 docs/novel/interaction.md 第 4 节。
"""
from __future__ import annotations

import json
import os
import re
from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QScrollArea,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from core.generation.lore_repository import (
    BODY,
    CHARACTERS,
    CONTRACT,
    FACTIONS,
    LORE,
    LoreRepository,
    background_name,
    current_backgrounds,
    review_summary,
)
from core.generation.lore_run import (
    LABELS,
    attention_receipts,
    edited_after_save,
    latest_by_product,
    list_receipts,
    load_receipt,
    needs_attention,
    recover_interrupted,
    stale_lore,
)
from core.gui import theme
from core.gui.pages.novel.inspector import StepButton
from core.gui.pages.novel.lore_editor import ROLE_LABELS, LoreEditor
from core.gui.pages.novel.lore_results import GenerationDraftEditor, result_details, result_status
from core.gui.pages.novel.services import artifacts
from core.gui.pages.novel.services.step_runner import action_work, step_work
from core.gui.pages.novel.stage_base import StagePage
from core.gui.widgets import (
    Card,
    hint_label,
    primary_button,
    secondary_button,
)
from core.lore_localization import FIELD_LABELS, display_message, display_value, file_label


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


def _from_top(view: MarkdownView) -> MarkdownView:
    # 光标留在文末时，第一次显示会滚到末尾；设定页打开时应从第一行读起。
    view.moveCursor(QTextCursor.Start)
    return view


def _clock(timestamp) -> str:
    from datetime import datetime
    try:
        moment = datetime.fromtimestamp(float(timestamp))
    except (TypeError, ValueError, OSError):
        return ""
    return moment.strftime("%H:%M" if moment.date() == datetime.now().date() else "%m-%d %H:%M")


#: 续跑时提示「修好后接着生成：…」用的步骤名；分步按钮也按这几个名字叫
STEP_TITLES = {"factions": "势力", "characters": "人物", "lore": "世界观", "enhance": "人物背景", "titles": "候选标题"}

#: 有详细背景的几种身份；人物身份都不是这几种时，按全部人物写背景
MAIN_ROLES = ("protagonist", "deuteragonist", "antagonist")

#: 每份产物生成 1 次、检查 1 次
CALLS_PER_PRODUCT = 2

_TITLE_LINE = re.compile(r"^\s*(?:\d+\s*[.、)）]|[-*•])\s*(.+?)\s*$")


def parse_titles(text: str) -> List[str]:
    """候选标题文件里一行一个标题：去掉编号、加粗、书名号和破折号后的解释。"""
    titles: List[str] = []
    for line in str(text or "").splitlines():
        match = _TITLE_LINE.match(line)
        if not match:
            continue
        title = re.split(r"——| — | - ", match.group(1))[0].strip().strip("*").strip().strip("《》").strip()
        if title and title not in titles:
            titles.append(title)
    return titles


def _names(items, with_role=False) -> List[str]:
    lines = []
    for item in items:
        if not isinstance(item, dict):
            lines.append(f"- {display_value(item)}")
            continue
        line = f"- {item.get('name', '（未命名）')}"
        aliases = [str(alias) for alias in item.get("aliases") or [] if str(alias).strip()]
        if aliases:
            line += f"（别名：{'、'.join(aliases)}）"
        if with_role and item.get("role"):
            line += f"：{display_value(item['role'])}"
        lines.append(line)
    return lines


def contract_markdown(data) -> str:
    """名称与规则登记表按字段写成可读的几段。"""
    if not isinstance(data, dict):
        return ""
    parts = []
    for key in ("era", "technology_level"):
        if data.get(key):
            parts.append(f"**{FIELD_LABELS[key]}**：{display_value(data[key])}")
    rules = data.get("world_constraints") or []
    if rules:
        parts.append(f"### {FIELD_LABELS['world_constraints']}\n" + "\n".join(f"- {display_value(rule)}" for rule in rules))
    for key in ("canonical_characters", "canonical_factions", "canonical_locations"):
        if data.get(key):
            parts.append(f"### {FIELD_LABELS[key]}\n" + "\n".join(_names(data[key], key == "canonical_characters")))
    shown = {"era", "technology_level", "world_constraints", "canonical_characters", "canonical_factions",
             "canonical_locations", "schema_version", "origin"}
    for key, value in data.items():
        if key not in shown and value not in (None, "", [], {}):
            parts.append(f"**{FIELD_LABELS.get(key, key)}**：{display_value(value)}")
    return "\n\n".join(parts)


def relationships_markdown(relations) -> str:
    lines = []
    for item in relations or []:
        if not isinstance(item, dict):
            continue
        line = f"- **{item.get('character1', '')}** 与 **{item.get('character2', '')}**"
        if item.get("type"):
            line += f"：{display_value(item['type'])}"
        if item.get("description"):
            line += f"。{display_value(item['description'])}"
        lines.append(line)
    return "\n".join(lines)


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
        # 人物卡角标写身份（主角 / 反派…），势力卡写「势力」。
        role = entity.raw.get("role") if entity.kind == "人物" else None
        kind = QLabel(ROLE_LABELS.get(role, entity.kind))
        kind.setStyleSheet(
            f"background: {theme.SURFACE}; border: 1px solid {theme.LINE};"
            f"border-radius: 10px; padding: 2px 8px; font-size: {theme.FS_XS}px;"
            f"color: {theme.INK_600};")
        top.addWidget(name, 1)
        top.addWidget(kind, 0)
        layout.addLayout(top)

        summary = QLabel(display_value(entity.summary) if entity.summary else "（该条目没有描述字段）")
        summary.setWordWrap(True)
        summary.setStyleSheet(
            f"font-size: {theme.FS_SM}px; color: {theme.INK_600}; border: none;")
        layout.addWidget(summary)


class LorePage(StagePage):
    stage_key = "lore"
    #: 作者在「候选标题」里点了「用作作品标题」
    title_chosen = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("世界设定", "世界构建与背景")
        self.active_editor = None
        self.generation_busy = False

        # 页头的蓝色按钮跟着状态走：现在该做的那一件事（见 _refresh_status）。
        # 平时用不上的入口收进「更多」；推荐标题是整段生成的一部分，不单独摆在页头。
        self.more_button = secondary_button("更多")
        self.more_menu = QMenu(self.more_button)
        self.open_folder_action = self.more_menu.addAction("打开设定文件夹")
        self.open_folder_action.triggered.connect(lambda: self._open_folder())
        self.open_records_action = self.more_menu.addAction("打开生成记录")
        self.open_records_action.setToolTip("每次请求的提示词和检查报告原文，排查问题时用")
        self.open_records_action.triggered.connect(lambda: self._open_folder("system/generation_runs"))
        # 有了任何设定以后，整段重来只从这里点，不占页头的蓝色按钮。
        self.more_menu.insertSeparator(self.open_folder_action)
        self.regenerate_action = QAction("重新生成世界设定", self.more_menu)
        self.regenerate_action.triggered.connect(lambda: self._run_step("full", self.generate_button))
        self.more_menu.insertAction(self.more_menu.actions()[0], self.regenerate_action)
        self.more_menu.setToolTipsVisible(True)
        self.more_button.setMenu(self.more_menu)
        # 蓝色按钮旁边的白色按钮：设定已经能用时是「下一步：故事结构」；
        # 整段生成停在有问题的那份时是「就用现在的，继续生成」。没有就不出现。
        self.side_button = secondary_button("下一步：故事结构")
        self.side_button.clicked.connect(self._side_clicked)
        self.generate_button = primary_button("生成世界设定")
        for button in (self.more_button, self.side_button, self.generate_button):
            self.header.add_action(button)
        self.generate_button.clicked.connect(self._primary_clicked)
        self._ready = False
        self._primary = None
        self._side = None

        self._build_left()
        self._build_inspector()
        self.refresh()

    # ---------------------------------------------------------------- 左侧
    def _build_left(self) -> None:
        container = Card()
        container.body.setContentsMargins(0, 0, 0, 0)
        container.body.setSpacing(0)

        # 改动条：最近一次改动和「撤回」，撤回后多一个「恢复」。
        self.change_bar = QWidget()
        bar = QHBoxLayout(self.change_bar)
        bar.setContentsMargins(20, 10, 20, 10)
        bar.setSpacing(10)
        self.change_label = QLabel("")
        self.change_label.setObjectName("Tertiary")
        self.change_label.setWordWrap(True)
        self.undo_button = secondary_button("撤回")
        self.redo_button = secondary_button("恢复")
        self.undo_button.clicked.connect(self._undo)
        self.redo_button.clicked.connect(self._redo)
        bar.addWidget(self.change_label, 1)
        bar.addWidget(self.undo_button)
        bar.addWidget(self.redo_button)
        self.change_bar.hide()
        container.body.addWidget(self.change_bar)

        # 状态说明：一句话说清设定现在怎样；该做的事在页头蓝色按钮上，这里不另放按钮。
        # 还没有任何设定时左侧只有这一块，不摆空页签。
        self.status_strip = QWidget()
        strip = QHBoxLayout(self.status_strip)
        strip.setContentsMargins(20, 12, 20, 12)
        strip.setSpacing(10)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        strip.addWidget(self.status_label, 1)
        strip.setAlignment(Qt.AlignTop)   # 没有页签时说明贴在顶部，不浮在中间
        container.body.addWidget(self.status_strip)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        container.body.addWidget(self.tabs)
        self.set_left(container)

    # ---------------------------------------------------------------- 撤回
    def _refresh_change_bar(self) -> None:
        try:
            log = LoreRepository(self.output_dir).change_log()
        except Exception:
            log = {"undo": None, "redo": None}
        undo, redo = log.get("undo"), log.get("redo")
        parts = []
        if undo:
            parts.append(f"最近一次改动：{undo['label']}（{_clock(undo.get('at'))}）")
        if redo:
            parts.append(f"已撤回：{redo['label']}")
        self.change_label.setText("　·　".join(parts))
        self.undo_button.setVisible(bool(undo))
        self.redo_button.setVisible(bool(redo))
        self.undo_button.setToolTip(f"撤回「{undo['label']}」，回到那之前的设定" if undo else "")
        self.redo_button.setToolTip(f"恢复「{redo['label']}」" if redo else "")
        for button in (self.undo_button, self.redo_button):
            button.setEnabled(not self.generation_busy)
        self.change_bar.setVisible(bool(undo or redo))
        self._change_log = log

    def _undo(self) -> None:
        self._step_history("undo")

    def _redo(self) -> None:
        self._step_history("redo")

    def _step_history(self, direction: str) -> None:
        if self.generation_busy or not self.resolve_draft():
            return
        entry = (getattr(self, "_change_log", None) or {}).get(direction)
        try:
            repo = LoreRepository(self.output_dir)
            done = repo.undo(entry and entry["id"]) if direction == "undo" else repo.redo(entry and entry["id"])
        except Exception as exc:
            self.status_message.emit("error", display_message(exc))
            self.refresh()
            return
        self.refresh()
        self.artifacts_changed.emit()
        self.status_message.emit("done", ("已撤回：" if direction == "undo" else "已恢复：") + done["label"])

    def _entity_tab(self, entities: List[artifacts.Entity], empty_hint: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        if not entities:
            layout.addWidget(hint_label(empty_hint))
        for index, entity in enumerate(entities):
            card = EntityCard(entity)
            edit = secondary_button("编辑")
            kind = "characters" if entity.kind == "人物" else "factions"
            edit.setEnabled(not self.generation_busy)
            edit.clicked.connect(lambda checked=False, k=kind, i=index: self._edit_entity(k, i))
            card.layout().addWidget(edit)
            if kind == "characters":
                expected = background_name(entity.raw)
                for relative in artifacts.background_files(self.output_dir):
                    if relative.replace("\\", "/").split("/")[-1] == expected:
                        background = secondary_button("编辑人物背景")
                        background.setEnabled(not self.generation_busy)
                        background.clicked.connect(lambda checked=False, p=relative: self.edit_file(LORE + p.replace("\\", "/")))
                        card.layout().addWidget(background)
            layout.addWidget(card)
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def _text_tab(self, text: str, relative_path=None, object_path=(), serif=True) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 18, 20, 18)
        if relative_path and text.strip():
            edit = secondary_button("编辑")
            edit.setEnabled(not self.generation_busy)
            edit.clicked.connect(lambda: self.edit_file(relative_path, object_path))
            layout.addWidget(edit, 0, Qt.AlignLeft)
        layout.addWidget(_from_top(read_only_text(text, serif=serif)))
        return page

    def _titles_tab(self, titles_text: str) -> QWidget:
        """候选标题：每个标题旁「用作作品标题」，底部「换一组」。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(8)
        titles = parse_titles(titles_text)
        if not titles:
            layout.addWidget(_from_top(read_only_text(titles_text, serif=True)))
        for title in titles:
            row = QHBoxLayout()
            label = QLabel(title)
            label.setStyleSheet(f"font-family: {theme.SERIF_STACK}; font-size: 15px;")
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            use = secondary_button("用作作品标题")
            use.setEnabled(not self.generation_busy)
            use.clicked.connect(lambda checked=False, t=title: self.title_chosen.emit(t))
            row.addWidget(label, 1)
            row.addWidget(use)
            layout.addLayout(row)
        self.retitle_button = secondary_button("换一组")
        self.retitle_button.setToolTip("按当前世界设定重新推荐一组标题（1 次模型调用）")
        self.retitle_button.setEnabled(not self.generation_busy)
        self.retitle_button.clicked.connect(lambda: self._run_step("titles", self.retitle_button))
        layout.addWidget(self.retitle_button, 0, Qt.AlignLeft)
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def _edit_entity(self, kind, index):
        relative = CHARACTERS if kind == "characters" else FACTIONS
        try:
            snapshot = LoreRepository(self.output_dir).load_snapshot()
            payload = json.loads(snapshot.files[relative])
            path = (index,) if isinstance(payload, list) else ((kind, index) if kind in payload else ())
            self.edit_file(relative, path)
        except Exception as exc:
            self.status_message.emit("error", display_message(exc))

    def edit_file(self, relative, object_path=()):
        if self.generation_busy:
            self.status_message.emit("warn", "生成任务运行中，请等待完成后编辑。")
            return
        if not self.resolve_draft():
            return
        try:
            editor = LoreEditor(self.output_dir, relative, object_path, self)
        except Exception as exc:
            self.status_message.emit("error", display_message(exc))
            return
        self.active_editor = editor
        editor.saved.connect(lambda result: self.status_message.emit("done", "设定已保存；关联内容可在待核对列表查看。"))
        editor.exec()
        self.active_editor = None
        self.refresh()
        self.artifacts_changed.emit()
        editor.deleteLater()

    def resolve_draft(self):
        return self.active_editor is None or self.active_editor.resolve_draft()

    def set_generation_busy(self, busy):
        self.generation_busy = busy
        if self.active_editor:
            self.active_editor.set_busy(busy)
        else:
            self.refresh()

    def _review_tab(self):
        """待核对按阶段汇总成几行，每行可展开看文件、可跳到那个阶段。没有事项返回 None（页签不出现）。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        try:
            summary = review_summary(self.output_dir)
        except Exception as exc:
            layout.addWidget(hint_label(f"读取核对状态失败：{exc}"))
            return page
        if not summary["general"] and not summary["groups"]:
            return None
        for item in summary["general"]:
            label = QLabel(display_message(item))
            label.setWordWrap(True)
            layout.addWidget(label)
        for group in summary["groups"]:
            layout.addWidget(self._review_group(group))
        acknowledge = secondary_button("全部标为已看过")
        acknowledge.setToolTip("清空待核对；之后可以在页面顶部撤回")
        acknowledge.setEnabled(not self.generation_busy)
        acknowledge.clicked.connect(lambda: self._acknowledge(summary["version"]))
        layout.addWidget(acknowledge, 0, Qt.AlignLeft)
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def _review_group(self, group) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        row = QHBoxLayout()
        line = QLabel(group["line"])
        line.setWordWrap(True)
        row.addWidget(line, 1)
        toggle = secondary_button("查看文件")
        toggle.setCheckable(True)
        row.addWidget(toggle)
        if group["stage"] != "lore":
            go = secondary_button("去" + group["title"])
            go.clicked.connect(lambda checked=False, stage=group["stage"]: self.navigate_requested.emit(stage))
            row.addWidget(go)
        layout.addLayout(row)
        files = QLabel("\n".join(file_label(relative) + (" · " + "、".join(names) if names else "")
                                 for relative, names in group["files"]))
        files.setObjectName("Tertiary")
        files.setWordWrap(True)
        files.setTextInteractionFlags(Qt.TextSelectableByMouse)
        files.hide()
        toggle.toggled.connect(files.setVisible)
        layout.addWidget(files)
        return box

    def _acknowledge(self, version):
        try:
            LoreRepository(self.output_dir).acknowledge(version)
            self.refresh()
            self.artifacts_changed.emit()
        except Exception as exc:
            self.status_message.emit("error", display_message(exc))

    @staticmethod
    def _result_title(receipt) -> str:
        label = LABELS.get(receipt["kind"], "设定")
        if receipt.get("subject"):
            label += "「" + str(receipt["subject"].get("name", "")) + "」"
        return label

    def _generation_results_tab(self):
        """每份产物只摆最新一张卡，按钮按状态出现；更早的记录收在卡片底部。"""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        self.result_buttons = []
        receipts = [r for r in list_receipts(self.output_dir) if r["kind"] in LABELS]
        for receipt, earlier in latest_by_product(receipts):
            edited = edited_after_save(self.output_dir, receipt)
            attention = needs_attention(self.output_dir, receipt)
            has_notes = bool(receipt.get("notes")) and not receipt.get("reverted")
            # 卡上只有标题时不留正文边距，已完成的几份排得紧一些。
            padding = (20, 18, 20, 20) if attention or has_notes or earlier else (20, 0, 20, 0)
            card = Card(self._result_title(receipt) + " · " + result_status(receipt, edited), padding=padding)
            card.setToolTip(result_details(receipt))
            if attention:
                problems = "\n".join(display_message(issue) for issue in receipt.get("issues", []))
                if receipt.get("disposition") != "current":
                    # 没采用的那份：标题已说明原因，读取报错原文只放在悬停提示里。
                    kept = QLabel("当前设定没有被替换；返回的原文保留着，可以让 AI 再修一次，或自己改成能读的内容后采用。")
                    kept.setWordWrap(True)
                    card.body.addWidget(kept)
                    if problems:
                        card.setToolTip(result_details(receipt) + "\n" + problems)
                elif problems:
                    issues = QLabel(problems)
                    issues.setWordWrap(True)
                    card.body.addWidget(issues)
                card.body.addLayout(self._result_actions(receipt))
            if has_notes:
                notes = QLabel("检查备注（不影响交付）：\n" + "\n".join(receipt["notes"]))
                notes.setWordWrap(True)
                notes.setObjectName("Tertiary")
                card.body.addWidget(notes)
            if earlier:
                card.body.addWidget(self._earlier_results(earlier))
            layout.addWidget(card)
        layout.addStretch(1)
        scroll.setWidget(page)
        return scroll

    @staticmethod
    def _then_continue(receipt) -> bool:
        """整段生成停在这一份：修好后要接着跑剩下几步。"""
        return bool(receipt.get("remaining")) and not receipt.get("accepted")

    @classmethod
    def _can_use_as_is(cls, receipt) -> bool:
        """停在一份已保存、只是有问题的结果上：可以就用现在的，把剩下几步跑完。"""
        return cls._then_continue(receipt) and receipt.get("disposition") == "current"

    def _use_as_is_tip(self, receipt, calls) -> str:
        steps = receipt["remaining"]
        names = "、".join(STEP_TITLES.get(key, key) for key in steps)
        total = sum(calls.get(key, CALLS_PER_PRODUCT) for key in steps)
        return f"按现在保存的{LABELS.get(receipt['kind'], '设定')}接着生成：{names}（约 {total} 次请求）"

    def _use_as_is(self, receipt, busy_button=None) -> None:
        """「就用现在的，继续生成」：这份不再算要处理，剩下几步按它接着写。"""
        if self.generation_busy or not self.resolve_draft():
            return
        options = {"steps": list(receipt["remaining"]), "accept": receipt["id"]}
        self.run_task(action_work("lore", "continue", self.output_dir, self.model, options=options),
                      busy_button=busy_button, busy_text="正在接着生成…",
                      on_success=self._show_generation_result)

    def _repair_tip(self, receipt, calls) -> str:
        if self._then_continue(receipt):
            steps = receipt["remaining"]
            names = "、".join(STEP_TITLES.get(key, key) for key in steps)
            total = CALLS_PER_PRODUCT + sum(calls.get(key, CALLS_PER_PRODUCT) for key in steps)
            return f"修好后接着生成：{names}（约 {total} 次请求）"
        return f"按检查意见再修一次（约 {CALLS_PER_PRODUCT} 次请求）"

    def _result_actions(self, receipt) -> QHBoxLayout:
        """有问题：「让 AI 再修一次」+「编辑」；读不出来：「让 AI 再修一次」+「编辑原文」。
        整段生成停在这份时是「再修一次并继续生成」。都用白色按钮：蓝色只留给页头那一个。"""
        row = QHBoxLayout()
        identity = receipt["id"]
        then_continue = self._then_continue(receipt)
        repair = secondary_button("再修一次并继续生成" if then_continue else "让 AI 再修一次")
        repair.setToolTip(self._repair_tip(receipt, self._step_calls(artifacts.characters(self.output_dir))))
        repair.clicked.connect(lambda checked=False: self._repair_generation_result(identity, then_continue))
        buttons = [repair]
        if self._can_use_as_is(receipt):
            use = secondary_button("就用现在的，继续生成")
            use.setToolTip(self._use_as_is_tip(receipt, self._step_calls(artifacts.characters(self.output_dir))))
            use.clicked.connect(lambda checked=False: self._use_as_is(receipt))
            buttons.append(use)
        if receipt.get("status") != "failed":
            edit = secondary_button("编辑" if receipt.get("disposition") == "current" else "编辑原文")
            edit.clicked.connect(lambda checked=False: self._edit_generation_result(identity))
            buttons.append(edit)
        for button in buttons:
            button.setEnabled(not self.generation_busy)
            row.addWidget(button)
            self.result_buttons.append(button)
        row.addStretch(1)
        return row

    def _earlier_results(self, earlier) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 4, 0, 0)
        toggle = secondary_button(f"更早的记录（{len(earlier)}）")
        toggle.setCheckable(True)
        lines = QLabel("\n".join(f"{_clock(r.get('created_at'))}　{result_status(r)}" for r in earlier))
        lines.setObjectName("Tertiary")
        lines.hide()
        toggle.toggled.connect(lines.setVisible)
        layout.addWidget(toggle, 0, Qt.AlignLeft)
        layout.addWidget(lines)
        return box

    def _edit_generation_result(self, identity):
        if self.generation_busy or not self.resolve_draft():
            return
        record = load_receipt(self.output_dir, identity)
        if record["kind"] != "titles" and record.get("disposition") == "current" and record.get("files"):
            self.edit_file(record["files"][0])
            return
        editor = GenerationDraftEditor(self.output_dir, identity, self)
        self.active_editor = editor
        editor.exec()
        self.active_editor = None
        self.refresh()
        self.artifacts_changed.emit()
        editor.deleteLater()

    def _repair_generation_result(self, identity, then_continue=False, busy_button=None):
        if self.generation_busy or not self.resolve_draft():
            return
        options = {"receipt_id": identity, **({"continue": True} if then_continue else {})}
        self.run_task(action_work("lore", "repair", self.output_dir, self.model, options=options),
                      busy_button=busy_button,
                      busy_text="正在修复并接着生成…" if then_continue else "正在修复设定一次…",
                      on_success=self._show_generation_result)

    def _show_generation_result(self, _=None):
        self.refresh()
        self.artifacts_changed.emit()
        # 只有需要作者处理时才切到「生成结果」；其余时候留在原来那一页。
        if attention_receipts(self.output_dir):
            self._open_tab("生成结果")

    def _open_tab(self, name) -> None:
        for index in range(self.tabs.count()):
            if self.tabs.tabText(index).split(" ")[0] == name:
                self.tabs.setCurrentIndex(index)

    # ---------------------------------------------------------------- 状态说明
    def _primary_clicked(self) -> None:
        if self._primary is not None:
            self._primary[1]()

    def _side_clicked(self) -> None:
        if self._side is not None:
            self._side[1]()

    def _continue_steps(self, steps, source=None) -> None:
        if not self.resolve_draft():
            return
        options = {"steps": list(steps), **({"source": source} if source else {})}
        self.run_task(action_work("lore", "continue", self.output_dir, self.model, options=options),
                      busy_button=self.generate_button,
                      busy_text=f"正在按新{source}重写…" if source else "正在接着生成…",
                      on_success=self._show_generation_result)

    def _step_calls(self, characters) -> dict:
        """每一步大约几次请求：每份产物生成 1 次、检查 1 次；人物背景按主要人物一人一份。"""
        main = [c for c in characters if c.raw.get("role") in MAIN_ROLES] or characters
        calls = {key: CALLS_PER_PRODUCT for key in ("factions", "characters", "lore")}
        calls["enhance"] = CALLS_PER_PRODUCT * (len(main) or len(MAIN_ROLES))
        calls["titles"] = 1
        return calls

    def _set_status(self, text, primary=None, next_step=False, side=None) -> None:
        """状态说明只写一句话；现在该做的那件事放在页头蓝色按钮上。

        primary / side：(按钮文字, 点下去做什么, 悬停说明)。primary 不给就是「下一步：故事结构」。
        next_step：蓝色按钮是别的事、但设定已经能用时，白色按钮是「下一步：故事结构」。
        """
        self.status_label.setText(text)
        go_on = ("下一步：故事结构", lambda: self.navigate_requested.emit("structure"), "")
        self._primary = primary or go_on
        self._side = side or (go_on if next_step else None)

    def _failed_twice(self, receipt, receipts) -> bool:
        """同一份连续两次修过仍有问题：检查可能判严了。"""
        for latest, earlier in latest_by_product([r for r in receipts if r.get("kind") in LABELS]):
            if latest["id"] == receipt["id"]:
                return bool(earlier) and earlier[0].get("status") == "needs_changes" and not earlier[0].get("reverted")
        return False

    def _refresh_status(self, factions, characters, lore_text, backgrounds, titles, receipts) -> None:
        """没有设定 → 要处理 → 过期 → 还差几项 → 已就绪，取第一种成立的。"""
        self._ready = bool(factions and characters and lore_text.strip())
        calls = self._step_calls(characters)
        if not (factions or characters or lore_text.strip()):
            self._set_status("还没有世界设定。点「生成世界设定」，按作品参数一次写好势力、人物、世界观和人物背景；"
                             "也可以在右侧分步生成。",
                             ("生成世界设定", lambda: self._run_step("full", self.generate_button),
                              f"按作品参数一次写好势力、人物、世界观和人物背景（约 {sum(calls.values())} 次请求）"))
            return
        attention = attention_receipts(self.output_dir, receipts)
        if attention:
            items = "、".join(f"{self._result_title(r)}（{result_status(r)}）" for r in attention)
            text = f"{len(attention)} 份需要处理：{items}。"
            stopped = any(self._then_continue(r) for r in attention)
            side = None
            if len(attention) == 1:
                receipt = attention[0]
                label = "再修一次并继续生成" if stopped else "让 AI 再修一次"
                primary = (label, lambda: self._repair_generation_result(receipt["id"], stopped, self.generate_button),
                           self._repair_tip(receipt, calls))
                if stopped:
                    text = f"整段生成停在{self._result_title(receipt)}：{result_status(receipt).replace('：', '，', 1)}。"
                if self._can_use_as_is(receipt):
                    side = ("就用现在的，继续生成", lambda: self._use_as_is(receipt, self.side_button),
                            self._use_as_is_tip(receipt, calls))
                    if self._failed_twice(receipt, receipts):
                        text += "检查可能判严了，内容可以用就点「就用现在的，继续生成」。"
            else:
                primary = (f"去处理（{len(attention)} 份）", lambda: self._open_tab("生成结果"),
                           "在「生成结果」里逐份处理")
            # 整段生成停在半路时，后面几项可能还没有，不给「下一步」。
            self._set_status(text, primary, next_step=self._ready and not stopped, side=side)
            return
        stale = stale_lore(self.output_dir, receipts)
        if stale:
            steps, source = stale["steps"], stale["source"]
            total = sum(calls.get(key, CALLS_PER_PRODUCT) for key in steps)
            self._set_status(f"{'、'.join(stale['stale'])}是按旧{source}写的。",
                             (f"按新{source}重写后面几项", lambda: self._continue_steps(steps, source),
                              f"约 {total} 次请求，会重写{'、'.join(stale['stale'])}，可撤回。"),
                             next_step=self._ready)
            return
        present = {"factions": bool(factions), "characters": bool(characters), "lore": bool(lore_text.strip()),
                   "enhance": bool(backgrounds) or not any(c.raw.get("role") in MAIN_ROLES for c in characters)}
        order = ["factions", "characters", "lore", "enhance"]
        missing = [key for key in order if not present[key]]
        if missing:
            steps = order[order.index(missing[0]):] + ([] if titles.strip() else ["titles"])
            names = "、".join(STEP_TITLES[key] for key in missing)
            total = sum(calls.get(key, CALLS_PER_PRODUCT) for key in steps)
            self._set_status(f"还差{names}。",
                             ("接着生成后面几项", lambda: self._continue_steps(steps),
                              f"依次生成{'、'.join(STEP_TITLES[key] for key in steps)}（约 {total} 次请求）"))
            return
        notes = sum(len(r.get("notes") or []) for r, _earlier in latest_by_product(receipts)
                    if r.get("status") == "complete" and not r.get("reverted"))
        parts = [f"{len(factions)} 个势力", f"{len(characters)} 个人物", "世界观"]
        if backgrounds:
            parts.append(f"{len(backgrounds)} 份人物背景")
        text = "世界设定已就绪：" + " · ".join(parts) + "。"
        if notes:
            text += f"有 {notes} 条检查备注，在「生成结果」里。"
        self._set_status(text)

    # ---------------------------------------------------------------- 右侧
    def _build_inspector(self) -> None:
        self.steps_meta = QLabel("")
        self.steps_meta.setObjectName("Tertiary")
        steps = Card("分步生成", self.steps_meta)
        # 势力、人物数量按篇幅由系统定，这里只说明，不让作者填。
        self.counts_hint = hint_label("")
        steps.body.addWidget(self.counts_hint)
        self.step_buttons = {
            "factions": StepButton("factions", "生成势力"),
            "characters": StepButton("characters", "生成人物"),
            "lore": StepButton("lore", "生成世界观"),
            "enhance": StepButton("enhance", "生成人物背景"),
        }
        for button in self.step_buttons.values():
            button.clicked.connect(self._run_step)
            steps.body.addWidget(button)
        self.add_card(steps)

        self.finish_inspector()

    # ---------------------------------------------------------------- 行为
    def _run_step(self, key: str, busy_button=None) -> None:
        if not self.resolve_draft():
            return
        labels = {
            "factions": "正在生成势力…",
            "characters": "正在生成人物…",
            "lore": "正在生成世界观…",
            "enhance": "正在生成人物背景…",
            "titles": "正在推荐标题…",
            "full": "正在生成设定…",
        }
        work = (step_work("lore", self.output_dir, self.model) if key == "full"
                else action_work("lore", key, self.output_dir, self.model))
        self.run_task(
            work,
            busy_button=busy_button or self.step_buttons.get(key, self.generate_button),
            busy_text=labels[key],
            on_success=self._show_generation_result,
        )

    def _refresh_step_buttons(self, factions, characters, lore_text, backgrounds) -> None:
        """没生成过的叫「生成…」，生成过的叫「重新生成…」；右侧只写数量。

        悬停说清替换什么、约几次请求、之后哪几项会算作按旧的写的。要处理的事不挂在这里，
        只在状态说明里说。
        """
        has_lore = bool(lore_text.strip())
        whom = "主角、第二主角、反派" if any(c.raw.get("role") in MAIN_ROLES for c in characters) else "每个人物"
        present = {"factions": bool(factions), "characters": bool(characters),
                   "lore": has_lore, "enhance": bool(backgrounds)}
        counts = {"factions": f"{len(factions)} 个", "characters": f"{len(characters)} 个",
                  "lore": "1 篇", "enhance": f"{len(backgrounds)} 份"}
        existing = {"factions": f"现有 {len(factions)} 个势力", "characters": f"现有 {len(characters)} 个人物",
                    "lore": "现有世界观", "enhance": f"现有 {len(backgrounds)} 份人物背景"}
        calls = self._step_calls(characters)
        order = list(self.step_buttons)
        for key in order:
            button, name = self.step_buttons[key], STEP_TITLES[key]
            if key == "enhance" and not characters:
                button.set_label("生成人物背景")
                button.set_state("locked", "需先生成人物")
                button.setToolTip("需先生成人物")
                continue
            done = present[key]
            button.set_label(("重新生成" if done else "生成") + name)
            button.set_state("done" if done else "todo", counts[key] if done else "")
            what = f"给{whom}各写一份详细背景" if key == "enhance" else f"按作品参数生成{name}"
            if not done:
                button.setToolTip(f"{what}（约 {calls[key]} 次请求）")
                continue
            later = [STEP_TITLES[k] for k in order[order.index(key) + 1:] if present[k]]
            tip = (f"{what}，替换" if key == "enhance" else "替换") + f"{existing[key]}（约 {calls[key]} 次请求）。"
            if later:
                tip += f"之后{'、'.join(later)}会标为按旧{name}写的。"
            button.setToolTip(tip + "可撤回。")

    def _counts_text(self) -> str:
        from core.generation.stage_pipeline import load_stage_parameters
        from core.localization import zh_label
        try:
            params = load_stage_parameters(self.output_dir)
        except (OSError, ValueError):
            params = {}
        if not params:
            return "势力、人物数量按作品篇幅决定。"
        length = zh_label(str(params.get("story_length", "")))
        return f"按篇幅（{length}）生成 {params.get('num_factions', 6)} 个势力 · {params.get('num_characters', 8)} 个人物"

    def _open_folder(self, subdirectory=None) -> None:
        import subprocess
        import sys

        path = os.path.join(self.output_dir, subdirectory if isinstance(subdirectory, str) else artifacts.LORE_DIR)
        if not os.path.isdir(path):
            self.status_message.emit("warn", f"目录还不存在：{path}")
            return
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606 - 打开资源管理器
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def _read_json(self, relative):
        try:
            with open(os.path.join(self.output_dir, relative), encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    # ---------------------------------------------------------------- 刷新
    def refresh(self) -> None:
        if self.active_editor is not None:
            return
        if not self.generation_busy:
            try:
                LoreRepository(self.output_dir).recover_pending()
                recover_interrupted(self.output_dir)
            except Exception as exc:
                self.status_message.emit("error", f"无法恢复设定保存状态：{exc}")
                return
        factions = artifacts.factions(self.output_dir)
        characters = artifacts.characters(self.output_dir)
        lore_text = artifacts.lore_text(self.output_dir)
        titles = artifacts.suggested_titles(self.output_dir)
        # 只算还属于现有人物的背景；人物改名或重生成后留下的旧背景文件不列出（仍在磁盘上）。
        backgrounds = current_backgrounds(self.output_dir)
        total = artifacts.lore_file_count(self.output_dir)
        receipts = list_receipts(self.output_dir)

        # 页签按内容出现；刷新后按名字回到原来那一页（数字会变，只比名字）。
        previous = self.tabs.tabText(self.tabs.currentIndex()).split(" ")[0] if self.tabs.count() else ""
        self.tabs.clear()
        if lore_text.strip():
            self.tabs.addTab(self._text_tab(lore_text, BODY), "世界观")
        if factions:
            self.tabs.addTab(self._entity_tab(factions, ""), f"势力 {len(factions)}")
        if characters:
            self.tabs.addTab(self._entity_tab(characters, ""), f"人物 {len(characters)}")
        contract = contract_markdown(self._read_json(CONTRACT))
        if contract:
            self.tabs.addTab(self._text_tab(contract, CONTRACT, serif=False), "名称与规则")
        cast = self._read_json(CHARACTERS)
        relations = relationships_markdown(cast.get("relationships")) if isinstance(cast, dict) else ""
        if relations:
            self.tabs.addTab(self._text_tab(relations, CHARACTERS, ("relationships",), serif=False), "人物关系")
        if titles.strip():
            self.tabs.addTab(self._titles_tab(titles), "候选标题")
        review = self._review_tab()
        if review is not None:
            self.tabs.addTab(review, "待核对")
        if receipts:
            self.tabs.addTab(self._generation_results_tab(), "生成结果")
        for index in range(self.tabs.count()):
            if self.tabs.tabText(index).split(" ")[0] == previous:
                self.tabs.setCurrentIndex(index)
        self.tabs.setVisible(self.tabs.count() > 0)
        self._refresh_status(factions, characters, lore_text, backgrounds, titles, receipts)

        self._refresh_step_buttons(factions, characters, lore_text, backgrounds)
        done = sum(1 for button in self.step_buttons.values() if button.state == "done")
        self.steps_meta.setText(f"{done} / {len(self.step_buttons)} 已完成")
        self.counts_hint.setText(self._counts_text())
        self.header.set_subtitle(
            # 只报页面上看得到的数量；文件数会把不再列出的旧背景也算进去。
            f"{len(factions)} 个势力 · {len(characters)} 个人物"
            if total else "世界构建与背景 · 尚未生成"
        )
        # 运行中不换按钮文字：页头按钮正显示「正在…」，跑完会再刷新一次。
        label, _action, tip = self._primary
        if not self.generation_busy:
            self.generate_button.setText(label)
            self.generate_button.setToolTip(tip)
        self.generate_button.setEnabled(not self.generation_busy)
        self.side_button.setVisible(self._side is not None)
        if self._side is not None and not self.generation_busy:
            self.side_button.setText(self._side[0])
            self.side_button.setToolTip(self._side[2])
        self.side_button.setEnabled(not self.generation_busy)
        has_any = bool(factions or characters or lore_text.strip())
        full = sum(self._step_calls(characters).values())
        self.regenerate_action.setVisible(has_any)
        self.regenerate_action.setToolTip(f"按作品参数重写全部设定（约 {full} 次请求）；之后可以在页面顶部撤回")
        self.regenerate_action.setEnabled(not self.generation_busy)
        self._refresh_change_bar()
