"""Tests for durable legal-suspense story state."""

import json

from core.generation.story_ledger import StoryLedgerManager


def test_contract_hash_and_chapter_acceptance(tmp_path):
    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Mystery", "Subgenre": "Legal Thriller"})

    assert manager.case_bible_path.endswith("case_bible.json")
    assert manager.suspense_ledger_path.endswith("suspense_ledger.json")

    plan = "### 场景 1：证词\n证人交出一张收据。"
    contract = {
        "core_question": "收据为何晚了三十二分钟？",
        "reader_knows_after": ["收据时间与门禁记录冲突"],
        "fair_play_clues": [
            {
                "id": "C001",
                "surface_meaning": "打印延迟",
                "true_meaning": "有人调整过设备时间",
            }
        ],
        "personal_cost": "主角被暂停接触案卷",
        "cost_character": "林衡",
        "irreversible_change": "调查转入非公开状态",
    }
    saved = manager.save_contract(1, contract, plan)

    assert manager.load_contract(1, plan)["source_hash"] == saved["source_hash"]
    assert manager.load_contract(1, plan + "\n改动") is None

    manager.accept_chapter(1, saved, {"average_score": 3.6})
    manager.accept_chapter(1, saved, {"average_score": 3.8})
    ledger = json.loads(
        (tmp_path / "system" / "story_ledgers" / "suspense_ledger.json").read_text(
            encoding="utf-8"
        )
    )

    assert ledger["reader_knowledge"] == ["收据时间与门禁记录冲突"]
    assert [item["id"] for item in ledger["clues"]] == ["C001"]
    assert ledger["personal_costs"]["林衡"] == ["主角被暂停接触案卷"]
    assert len(ledger["accepted_chapters"]) == 1
    assert ledger["accepted_chapters"][0]["score"] == 3.8


def test_design_context_refreshes_generated_case_bible(tmp_path):
    structure_dir = tmp_path / "story" / "structure"
    outline_dir = tmp_path / "story" / "planning" / "chapter_outlines"
    structure_dir.mkdir(parents=True)
    outline_dir.mkdir(parents=True)
    (structure_dir / "act_1.md").write_text("真相：门禁时钟被调慢。", encoding="utf-8")
    (outline_dir / "chapters.md").write_text("第1章埋下三十二分钟线索。", encoding="utf-8")

    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Mystery", "Subgenre": "Legal Thriller"})
    design_context = manager.load_design_context()
    baseline = manager.load_case_bible()

    assert "门禁时钟被调慢" in design_context
    assert "三十二分钟线索" in design_context
    assert manager.case_bible_needs_refresh(baseline, design_context)

    saved = manager.save_case_bible(
        {**baseline, "truth": [{"id": "T001", "fact": "门禁时钟被调慢"}]},
        design_context,
    )
    assert saved["status"] == "ready"
    assert not manager.case_bible_needs_refresh(saved, design_context)

    (structure_dir / "act_1.md").write_text("真相：门禁时钟被调快。", encoding="utf-8")
    assert manager.case_bible_needs_refresh(saved, manager.load_design_context())
