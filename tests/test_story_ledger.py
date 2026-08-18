"""Tests for durable legal-suspense story state."""

import json

from core.generation.domain_profiles import HORROR
from core.generation.story_ledger import LEDGER_VERSION, StoryLedgerManager


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
    assert ledger["revision"] == 1
    assert len(ledger["chapter_commits"]) == 1


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


def test_initialize_records_domain_profile_and_rules(tmp_path):
    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Horror", "Subgenre": "Cosmic Horror"})

    case_bible = manager.load_case_bible()
    assert case_bible["domain_profile"] == "horror"
    assert case_bible["version"] == LEDGER_VERSION
    assert "legal_system" not in case_bible
    assert case_bible["domain_rules"]["model"] == HORROR.rules_model
    assert case_bible["domain_rules"]["baseline_rules"] == list(HORROR.baseline_rules)
    assert manager.locked_profile().key == "horror"


def test_locked_profile_survives_a_parameter_change(tmp_path):
    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Mystery", "Subgenre": "Legal Thriller"})

    # 换了参数再 initialize 不得改写已锁定的档案：评审维度一旦更换，
    # 已接受章节的评分就不再可比。
    manager.initialize({"Genre": "Romance", "Subgenre": "Regency Romance"})

    assert manager.locked_profile().key == "legal_suspense"
    assert manager.load_case_bible()["domain_profile"] == "legal_suspense"


def test_pre_v3_case_bible_migrates_legal_system_to_domain_rules(tmp_path):
    ledger_dir = tmp_path / "system" / "story_ledgers"
    ledger_dir.mkdir(parents=True)
    legacy_rules = {"model": "虚构法域", "baseline_rules": ["决定性物证必须记录来源"]}
    (ledger_dir / "case_bible.json").write_text(
        json.dumps(
            {
                "version": 2,
                "status": "ready",
                "truth": [{"id": "T001", "fact": "门禁时钟被调慢"}],
                "legal_system": legacy_rules,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Mystery", "Subgenre": "Legal Thriller"})

    case_bible = manager.load_case_bible()
    assert case_bible["version"] == LEDGER_VERSION
    assert "legal_system" not in case_bible
    assert case_bible["domain_rules"] == legacy_rules
    # 迁移前只有法律悬疑会走闭环，因此带 legal_system 的旧底稿归入法律悬疑。
    assert case_bible["domain_profile"] == "legal_suspense"
    # 迁移不得丢失既有真相。
    assert case_bible["truth"] == [{"id": "T001", "fact": "门禁时钟被调慢"}]
