# -*- coding: utf-8 -*-
"""阶段 2 · 世界设定。

左侧浏览生成结果，并通过独立编辑窗口修改已有设定。
生成参数与分步按钮收进右栏检查器。
规格见 docs/novel/interaction.md 第 4 节。
"""
from __future__ import annotations

from typing import List, Optional
import json

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
from core.gui.pages.novel.stage_base import StagePage
from core.gui.pages.novel.services import artifacts
from core.gui.pages.novel.services.step_runner import action_work, step_work
from core.gui.widgets import (
    Card,
    Stepper,
    hint_label,
    primary_button,
    secondary_button,
)
from core.gui.pages.novel.inspector import StepButton
from core.generation.lore_repository import (
    LoreRepository, LORE, BODY, CHARACTERS, FACTIONS, CONTRACT, background_name,
)
from core.gui.pages.novel.lore_editor import LoreEditor
from core.lore_localization import display_value, file_label, display_message
from core.generation.lore_run import list_receipts, load_receipt, recover_interrupted, LABELS
from core.gui.pages.novel.lore_results import GenerationDraftEditor, STATUS_LABELS, REASON_LABELS


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


def _clock(timestamp) -> str:
    from datetime import datetime
    try:
        moment = datetime.fromtimestamp(float(timestamp))
    except (TypeError, ValueError, OSError):
        return ""
    return moment.strftime("%H:%M" if moment.date() == datetime.now().date() else "%m-%d %H:%M")


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

        summary = QLabel(display_value(entity.summary) if entity.summary else "（该条目没有描述字段）")
        summary.setWordWrap(True)
        summary.setStyleSheet(
            f"font-size: {theme.FS_SM}px; color: {theme.INK_600}; border: none;")
        layout.addWidget(summary)


class LorePage(StagePage):
    stage_key = "lore"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("世界设定", "世界构建与背景")
        self.active_editor = None
        self.generation_busy = False

        # 「完善主要人物」只在右栏「分步生成」里出现一次；页头留右栏没有的
        # 「推荐作品标题」和跑完整阶段的主按钮。
        self.titles_button = secondary_button("推荐作品标题")
        self.generate_button = primary_button("生成世界设定")
        for button in (self.titles_button, self.generate_button):
            self.header.add_action(button)
        self.titles_button.clicked.connect(
            lambda: self._run_step("titles", self.titles_button))
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
            edit = secondary_button("查看 / 编辑")
            kind = "characters" if entity.kind == "人物" else "factions"
            edit.setEnabled(not self.generation_busy)
            edit.clicked.connect(lambda checked=False, k=kind, i=index: self._edit_entity(k, i))
            card.layout().addWidget(edit)
            if kind == "characters":
                expected = background_name(entity.raw)
                for relative in artifacts.background_files(self.output_dir):
                    if relative.replace("\\", "/").split("/")[-1] == expected:
                        background = secondary_button("编辑人物详细背景")
                        background.setEnabled(not self.generation_busy)
                        background.clicked.connect(lambda checked=False, p=relative: self.edit_file(LORE + p.replace("\\", "/")))
                        card.layout().addWidget(background)
            layout.addWidget(card)
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def _text_tab(self, text: str, relative_path=None) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 18, 20, 18)
        if relative_path and text.strip():
            edit = secondary_button("编辑")
            edit.setEnabled(not self.generation_busy)
            edit.clicked.connect(lambda: self.edit_file(relative_path))
            layout.addWidget(edit)
        layout.addWidget(read_only_text(text, serif=True))
        return page

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
        page = QWidget()
        layout = QVBoxLayout(page)
        try:
            repo = LoreRepository(self.output_dir)
            status = repo.review_status()
            items = status.get("review_items", [])
            pending = status.get("pending", {})
            lines = [display_message(item) for item in items] + [f"{file_label(path)}：设定已修改，待核对" for path in pending]
            view = QTextBrowser()
            view.setPlainText("\n\n".join(lines) or "当前没有待核对事项。")
            layout.addWidget(view)
            if lines:
                acknowledge = secondary_button("已核对当前版本")
                acknowledge.setEnabled(not self.generation_busy)
                acknowledge.clicked.connect(lambda: self._acknowledge(status["version"]))
                layout.addWidget(acknowledge)
        except Exception as exc:
            layout.addWidget(hint_label(f"读取核对状态失败：{exc}"))
        return page

    def _acknowledge(self, version):
        try:
            LoreRepository(self.output_dir).acknowledge(version)
            self.refresh()
            self.artifacts_changed.emit()
        except Exception as exc:
            self.status_message.emit("error", display_message(exc))

    def _generation_results_tab(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        page = QWidget()
        layout = QVBoxLayout(page)
        self.result_buttons = []
        for receipt in list_receipts(self.output_dir):
            if receipt["kind"] not in LABELS:
                continue
            label = LABELS[receipt["kind"]]
            if receipt.get("subject"):
                label += "「" + str(receipt["subject"].get("name", "")) + "」"
            status = ("已撤回" if receipt.get("reverted")
                      else STATUS_LABELS.get(receipt["status"], receipt["status"]))
            if receipt.get("notes") and receipt["status"] == "complete" and not receipt.get("reverted"):
                status += " · 有检查备注"
            card = Card(label + " · " + status)
            detail = QLabel(f"{REASON_LABELS.get(receipt.get('exit_reason'), '候选已保留')} · 实际请求 {receipt.get('calls', 0)}/6 · 修复 {receipt.get('repairs', 0)} 次 · 用时 {receipt.get('elapsed_seconds', 0):.1f} 秒")
            detail.setWordWrap(True)
            card.body.addWidget(detail)
            issues = QLabel("\n".join(display_message(issue) for issue in receipt.get("issues", [])) or "当前没有检查问题。")
            issues.setWordWrap(True)
            card.body.addWidget(issues)
            if receipt.get("notes"):
                notes = QLabel("检查备注（不影响交付）：\n" + "\n".join(receipt["notes"]))
                notes.setWordWrap(True)
                notes.setObjectName("Tertiary")
                card.body.addWidget(notes)
            version = QLabel("此结果已保存为设定；页面中的设定卡片展示当前磁盘版本。" if receipt.get("disposition") == "current"
                             else "此结果仅保留为草稿，尚未替换当前设定。继续下一步将读取当前已保存设定；需要使用此草稿时请先修复结构并采用。")
            version.setWordWrap(True)
            card.body.addWidget(version)
            row = QHBoxLayout()
            edit = secondary_button("手动修改" if receipt.get("disposition") == "current" else "编辑草稿 / 修复结构")
            edit.clicked.connect(lambda checked=False, identity=receipt["id"]: self._edit_generation_result(identity))
            repair = secondary_button("再修复一次")
            repair.clicked.connect(lambda checked=False, identity=receipt["id"]: self._repair_generation_result(identity))
            regenerate = secondary_button("重新生成")
            regenerate.clicked.connect(lambda checked=False, identity=receipt["id"]: self._regenerate_generation_result(identity))
            next_button = secondary_button("继续下一步")
            next_action = {"factions": "characters", "characters": "lore", "world": "enhance", "background": "titles"}.get(receipt["kind"])
            if next_action:
                next_button.clicked.connect(lambda checked=False, key=next_action: self._run_step(key))
            else:
                next_button.setEnabled(False)
            for button in (edit, repair, regenerate, next_button):
                if next_action or button is not next_button:
                    button.setEnabled(not self.generation_busy)
                row.addWidget(button)
                self.result_buttons.append(button)
            card.body.addLayout(row)
            if receipt.get("disposition") == "current":
                raw = secondary_button("编辑原始结构")
                raw.setEnabled(not self.generation_busy)
                raw.clicked.connect(lambda checked=False, identity=receipt["id"]: self._edit_generation_result(identity, raw=True))
                card.body.addWidget(raw)
                self.result_buttons.append(raw)
            layout.addWidget(card)
        layout.addStretch(1)
        scroll.setWidget(page)
        return scroll

    def _edit_generation_result(self, identity, raw=False):
        if self.generation_busy or not self.resolve_draft():
            return
        record = load_receipt(self.output_dir, identity)
        if not raw and record["kind"] != "titles" and record.get("disposition") == "current" and record.get("files"):
            self.edit_file(record["files"][0])
            return
        editor = GenerationDraftEditor(self.output_dir, identity, self)
        self.active_editor = editor
        editor.exec()
        self.active_editor = None
        self.refresh()
        self.artifacts_changed.emit()
        editor.deleteLater()

    def _repair_generation_result(self, identity):
        if self.generation_busy or not self.resolve_draft():
            return
        self.run_task(action_work("lore", "repair", self.output_dir, self.model, options={"receipt_id": identity}),
                      busy_text="正在修复设定一次…", on_success=self._show_generation_result)

    def _regenerate_generation_result(self, identity):
        receipt = load_receipt(self.output_dir, identity)
        if receipt["kind"] != "background":
            return self._run_step({"world": "lore"}.get(receipt["kind"], receipt["kind"]))
        if self.generation_busy or not self.resolve_draft():
            return
        self.run_task(action_work("lore", "enhance", self.output_dir, self.model,
                                  options={"subject_name": receipt["subject"]["name"], "target": receipt.get("target")}),
                      busy_text="正在重新生成该人物背景…", on_success=self._show_generation_result)

    def _show_generation_result(self, _=None):
        self.refresh()
        self.artifacts_changed.emit()
        for index in range(self.tabs.count()):
            if self.tabs.tabText(index) == "生成结果与草稿":
                self.tabs.setCurrentIndex(index)

    # ---------------------------------------------------------------- 右侧
    def _build_inspector(self) -> None:
        params = Card("生成参数")
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(theme.SP[3])
        grid.setVerticalSpacing(theme.SP[3])

        self.faction_stepper = Stepper(6, 1, 10)
        self.character_stepper = Stepper(8, 2, 10)
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
        self.open_diagnostics_button = secondary_button("打开参数检查记录", "folder")
        self.open_diagnostics_button.clicked.connect(lambda: self._open_folder("system/generation_runs"))
        outputs.body.addWidget(self.open_diagnostics_button)
        self.add_card(outputs)

        self.finish_inspector()

    # ---------------------------------------------------------------- 行为
    def _run_step(self, key: str, busy_button=None) -> None:
        if not self.resolve_draft():
            return
        labels = {
            "factions": "正在生成势力…",
            "characters": "正在生成人物…",
            "lore": "正在生成世界观…",
            "enhance": "正在完善主要人物…",
            "titles": "正在推荐标题…",
            "full": "正在生成设定…",
        }
        work = (
            step_work("lore", self.output_dir, self.model, parameters={
                "num_factions": self.faction_stepper.value(),
                "num_characters": self.character_stepper.value(),
            })
            if key == "full"
            else action_work(
                "lore",
                key,
                self.output_dir,
                self.model,
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
            on_success=self._show_generation_result,
        )

    def _open_folder(self, subdirectory=None) -> None:
        import os
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
        backgrounds = artifacts.background_files(self.output_dir)
        total = artifacts.lore_file_count(self.output_dir)

        current = self.tabs.currentIndex()
        self.tabs.clear()
        self.tabs.addTab(self._text_tab(lore_text, BODY), "世界观")
        self.tabs.addTab(self._entity_tab(factions, "尚未生成势力。"),
                         f"势力 {len(factions)}" if factions else "势力")
        self.tabs.addTab(self._entity_tab(characters, "尚未生成人物。"),
                         f"人物 {len(characters)}" if characters else "人物")
        if titles.strip():
            self.tabs.addTab(self._text_tab(titles), "候选标题")
        import os
        if os.path.exists(os.path.join(self.output_dir, CONTRACT)):
            registry = QWidget()
            registry_layout = QVBoxLayout(registry)
            registry_layout.addWidget(hint_label("编辑年代、技术水平、世界规则与规范名称。修改后请核对世界观正文。"))
            edit_registry = secondary_button("编辑名称与规则登记表")
            edit_registry.setEnabled(not self.generation_busy)
            edit_registry.clicked.connect(lambda: self.edit_file(CONTRACT))
            registry_layout.addWidget(edit_registry)
            registry_layout.addStretch()
            self.tabs.addTab(registry, "名称与规则")
        if backgrounds:
            background_page = QWidget()
            background_layout = QVBoxLayout(background_page)
            for relative in backgrounds:
                button = secondary_button(file_label(relative))
                button.setEnabled(not self.generation_busy)
                button.clicked.connect(lambda checked=False, p=relative: self.edit_file(LORE + p.replace("\\", "/")))
                background_layout.addWidget(button)
            background_layout.addStretch()
            self.tabs.addTab(background_page, "人物背景")
        try:
            cast_path = os.path.join(self.output_dir, CHARACTERS)
            if os.path.exists(cast_path):
                with open(cast_path, encoding="utf-8") as handle:
                    cast = json.load(handle)
                if isinstance(cast, dict) and isinstance(cast.get("relationships", []), list):
                    relation_page = QWidget()
                    relation_layout = QVBoxLayout(relation_page)
                    relation_button = secondary_button("编辑人物关系")
                    relation_button.setEnabled(not self.generation_busy)
                    relation_button.clicked.connect(lambda: self.edit_file(CHARACTERS, ("relationships",)))
                    relation_layout.addWidget(relation_button)
                    relation_layout.addStretch()
                    self.tabs.addTab(relation_page, "人物关系")
        except (OSError, ValueError):
            pass
        self.tabs.addTab(self._review_tab(), "待核对")
        if list_receipts(self.output_dir):
            self.tabs.addTab(self._generation_results_tab(), "生成结果与草稿")
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

        seen = set()
        for record in list_receipts(self.output_dir):
            key = {"world": "lore", "background": "enhance"}.get(record["kind"], record["kind"])
            if record.get("reverted"):
                continue
            if key in self.step_buttons and key not in seen:
                seen.add(key)
                if record["status"] != "complete":
                    button = self.step_buttons[key]
                    button.set_state(button.state, STATUS_LABELS.get(record["status"], "需核对"))

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
        self._refresh_change_bar()
