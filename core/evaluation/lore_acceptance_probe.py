"""显式选择的真实模型验收：真实 Qt 窗口、后台任务与生产存储。

不会被 pytest 自动调用。示例（可能产生模型费用）：
python -m core.evaluation.lore_acceptance_probe --run-live --output sandbox_verify_lore/characters
每份产物仍受生产管线的 6 次请求 / 300 秒预算约束。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live", action="store_true", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--action", choices=["characters", "full"], default="characters")
    parser.add_argument("--provider", default="hosted-llm", choices=["hosted-llm", "deepseek", "qwen"])
    args = parser.parse_args()
    root = Path(args.output).resolve()
    # 验收只创建全新项目，不会重置或覆盖已有作品。
    if root.exists():
        parser.error("验收目录已存在，请指定一个全新的目录")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from PySide6.QtCore import Qt
    from core.gui.main_window import MainWindow
    from core.gui.app import _pick_ui_font
    from core.gui import theme
    from core.gui import params_store
    from core.generation.lore_run import list_receipts
    from core.generation.lore_repository import LoreRepository, FACTIONS, encode
    app = QApplication.instance() or QApplication([])
    app.setFont(_pick_ui_font())
    app.setStyleSheet(theme.build_qss())
    params_store.save(str(root), {"novel_title": "静城谈判", "genre": "Sci-Fi", "subgenre": "Space Opera",
                                 "theme": "一个未来封闭城市的外交官以协商化解供水危机，讨论互信与权力的代价。",
                                 "tone": "克制而有张力", "protagonist_type": "Diplomat", "conflict_scale": "Personal",
                                 "space_combat": False, "interstellar_travel": False, "multiple_species": False,
                                 "num_characters": 2, "num_factions": 1, "quality_loop": "off"})
    if args.action == "characters":
        LoreRepository(root)._transaction({FACTIONS: encode([{"name": "静城议事会", "nature": "公权力机关",
            "type": "城市议事机构", "description": "处理静城供水分配和居民争端。", "goal": "维持供水与信任",
            "resources": ["供水记录"], "territory": "静城", "territory_kind": "城市", "conflicts": []}])}, "probe_fixture")
    window = MainWindow()
    window.parameters_page.load(str(root))
    window.parameters_page.output_dir_edit.setText(str(root))
    window.rail.model_combo.setCurrentText(args.provider)
    assert window.parameters_page.save()
    window.refresh_all()
    window._navigate("lore")
    window.show()
    started = time.monotonic()
    last_print = [0.0]
    def progress(text, fraction):
        if time.monotonic() - last_print[0] > 8:
            print(text, flush=True)
            last_print[0] = time.monotonic()
    window.runner.progress.connect(progress)
    button = window.lore_page.generate_button if args.action == "full" else window.lore_page.step_buttons["characters"]
    QTest.mouseClick(button, Qt.LeftButton)
    deadline = 1850 if args.action == "full" else 320
    while window.runner.current is not None:
        app.processEvents()
        QTest.qWait(20)
        if time.monotonic() - started > deadline:
            window.runner.cancel_current()
    app.processEvents()
    records = list_receipts(root)
    summary = {"provider": args.provider, "action": args.action, "elapsed_seconds": round(time.monotonic() - started, 2),
               "ui_status": window.status._state._text.text(), "actual_model_calls": sum(r["calls"] for r in records),
               "artifacts": [{k: r.get(k) for k in ("id", "kind", "subject", "status", "calls", "repairs", "exit_reason", "issues", "files")}
                             for r in records], "human_assessment": "尚需人工查看原文和检查证据；模型自评不能替代验收。"}
    # 新窗口重新读取保存状态，不复用原页中的缓存。
    reopened = MainWindow()
    reopened.parameters_page.load(str(root))
    reopened.parameters_page.output_dir_edit.setText(str(root))
    reopened.refresh_all()
    reopened._navigate("lore")
    summary["restart_records"] = len(list_receipts(reopened.output_dir()))
    summary["restart_tabs"] = [reopened.lore_page.tabs.tabText(i) for i in range(reopened.lore_page.tabs.count())]
    window.grab().save(str(root / "acceptance_ui.png"))
    (root / "acceptance_summary.json").write_text(encode(summary), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    reopened.close()
    window.close()
    return 0 if records and not any(r["status"] in {"failed", "running"} for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
