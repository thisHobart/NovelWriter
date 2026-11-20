"""生成结果、退出原因与离线原始草稿编辑。"""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPlainTextEdit, QMessageBox, QTabWidget, QTextBrowser

from core.gui.widgets import secondary_button, primary_button
from core.generation.lore_run import load_receipt, save_draft, adopt_draft, source_fingerprint, current_content
from core.generation.lore_repository import digest, encode

STATUS_LABELS = {"complete": "已生成", "needs_changes": "已生成，待修改", "unchecked": "已生成，检查未完成",
                 "draft": "草稿已保留", "running": "生成中，候选已保留", "failed": "未取得内容"}
REASON_LABELS = {"checked": "检查结束", "attempt_limit": "达到自动修复上限", "no_improvement": "重复问题没有改善",
                 "check_unavailable": "检查服务未完成", "budget_exhausted": "旧任务达到当时的限制",
                 "generation_unavailable": "模型服务未完成", "cancelled": "用户停止", "external_change": "外部修改冲突",
                 "save_failed": "保存失败", "manual_adoption": "已手动采用", "manual_draft": "已保存草稿"}
REASON_LABELS["interrupted"] = "上次运行意外中断"
REASON_LABELS["generated"] = "生成结束（标题不作模型检查）"


def extract_body(raw, kind):
    """只提取能可靠解码的文本字段；不把局部对象伪装成有效设定。"""
    if kind not in {"characters", "factions"}:
        from core.generation.design_contract import LORE_CONTRACT_START
        return raw.split(LORE_CONTRACT_START, 1)[0].strip()
    import json
    import re
    from core.localization import zh_field
    lines = []
    for match in re.finditer(r'"(name|description|background|goal|motivation)"\s*:\s*("(?:[^"\\]|\\.)*")', raw):
        try:
            lines.append(zh_field(match[1]) + "：" + json.loads(match[2]))
        except ValueError:
            continue
    return "\n\n".join(lines) or "没有可靠提取的字段正文，请在原始内容中继续编辑。原文仍完整保留。"


class GenerationDraftEditor(QDialog):
    saved = Signal(object)

    def __init__(self, root, identity, parent=None):
        super().__init__(parent)
        self.root, self.identity = root, identity
        self.record = load_receipt(root, identity)
        self.record_fingerprint = digest(encode(self.record))
        self.original = current_content(root, self.record)
        self.fingerprint = source_fingerprint(root)
        self.generation_busy = False
        self.setWindowTitle("编辑生成内容 / 修复结构")
        self.resize(850, 650)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("原始内容始终保留。可保存草稿，或修复为可读取内容后采用为当前设定；此操作不调用模型。"))
        self.field = QPlainTextEdit(self.original)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.field, "原始内容")
        self.extracted = QTextBrowser()
        self.extracted.setPlainText(extract_body(self.original, self.record["kind"]))
        self.field.textChanged.connect(lambda: self.extracted.setPlainText(extract_body(self.field.toPlainText(), self.record["kind"])))
        self.tabs.addTab(self.extracted, "可提取正文（参考）")
        layout.addWidget(self.tabs, 1)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.save_button = secondary_button("保存草稿")
        self.adopt_button = primary_button("采用为当前设定")
        cancel = secondary_button("取消")
        row = QHBoxLayout()
        for button in (self.save_button, self.adopt_button, cancel):
            row.addWidget(button)
        layout.addLayout(row)
        self.save_button.clicked.connect(self.save)
        self.adopt_button.clicked.connect(self.adopt)
        cancel.clicked.connect(self.reject)

    @property
    def dirty(self):
        return self.field.toPlainText() != self.original

    def set_busy(self, busy):
        self.generation_busy = busy
        self.field.setEnabled(not busy)
        self.save_button.setEnabled(not busy)
        self.adopt_button.setEnabled(not busy)

    def save(self):
        return self._save(False)

    def adopt(self):
        return self._save(True)

    def _save(self, adopt):
        if self.generation_busy:
            return False
        try:
            raw = self.field.toPlainText()
            result = (adopt_draft(self.root, self.identity, raw, expected_fingerprint=self.fingerprint,
                                  expected_receipt=self.record_fingerprint) if adopt
                      else save_draft(self.root, self.identity, raw, expected_receipt=self.record_fingerprint))
        except Exception as exc:
            self.message.setText(f"保存失败：{exc}。编辑内容仍保留。")
            return False
        self.original = raw
        self.saved.emit(result)
        self.accept()
        return True

    def resolve_draft(self):
        if not self.dirty:
            return True
        box = QMessageBox(self)
        box.setWindowTitle("生成草稿尚未保存")
        box.setText("是否保存当前草稿？")
        save = box.addButton("保存", QMessageBox.AcceptRole)
        discard = box.addButton("放弃", QMessageBox.DestructiveRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is save:
            return self.save()
        return box.clickedButton() is discard

    def reject(self):
        if self.resolve_draft():
            super().reject()
