"""Tests for durable legal-suspense story state."""

import json

from core.generation.domain_profiles import HORROR
import pytest

from core.generation.story_ledger import (
    LEDGER_VERSION,
    MIN_COMPACT_JSON_CHARS,
    StoryLedgerManager,
    build_ledger_prompt_view,
    compact_json,
)


def _complete_bible_spine():
    """A story bible with enough spine to ground cross-chapter checks."""
    return {
        "central_question": "门禁记录为何晚了三十二分钟？",
        "central_conflict": {
            "legal_answer": "流浪汉纵火",
            "truth_answer": "专案组伪造时间戳",
            "moral_question": "程序正义与结案率",
        },
        "truth": [
            {"id": "T001", "fact": "门禁时钟被调慢"},
            {"id": "T002", "fact": "副检验员死于火灾前"},
            {"id": "T003", "fact": "原始硬盘被转移"},
        ],
        "chronology": [{"order": 1, "event": "零点十二分运尸车入场"}],
    }


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
        {**baseline, **_complete_bible_spine()},
        design_context,
    )
    assert saved["status"] == "ready"
    assert saved["gaps"] == []
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


def test_compact_json_never_returns_broken_json():
    data = {
        "facts": [{"id": f"F{i}", "value": "很长的事实描述" * 30} for i in range(30)],
        "plot_threads": [{"id": f"PT{i}", "status": "open"} for i in range(30)],
    }

    text = compact_json(data, 1200)
    parsed = json.loads(text)

    assert isinstance(parsed, dict)
    assert "已截断" not in text
    assert parsed["_context_meta"]["truncated"] is True
    assert "context_overflow" not in parsed["_context_meta"]
    assert parsed["facts"] or parsed["plot_threads"]
    assert len(text) <= 1200


def test_compact_json_counts_omission_metadata_inside_budget():
    data = {
        "facts": [
            {"id": f"F{i}", "value": "需要保留的案件事实" * 35}
            for i in range(32)
        ]
    }

    text = compact_json(data, 8000)
    parsed = json.loads(text)

    assert 0 < len(parsed["facts"]) < len(data["facts"])
    assert parsed["_context_meta"]["truncated"] is True
    assert parsed["_context_meta"]["omitted"]["$.facts"] > 0
    assert "context_overflow" not in parsed["_context_meta"]
    assert len(text) <= 8000


def test_compact_json_preserves_items_for_a_top_level_list():
    data = [
        {"id": index, "value": "完整列表条目" * 20}
        for index in range(200)
    ]

    text = compact_json(data, 2000)
    parsed = json.loads(text)

    assert isinstance(parsed, list)
    assert 0 < len(parsed) < len(data)
    assert len(text) <= 2000


def test_ledger_prompt_view_excludes_audit_history_and_prioritizes_due_threads():
    ledger = {
        "revision": 7,
        "facts": [],
        "plot_threads": [
            {"id": "late", "status": "open", "deadline_chapter": 20},
            {"id": "due", "status": "open", "deadline_chapter": 8},
            {"id": "closed", "status": "closed", "deadline_chapter": 3},
        ],
        "accepted_chapters": [{"chapter": number} for number in range(1, 8)],
        "chapter_commits": [{"chapter": number} for number in range(1, 8)],
        "resolved_conflicts": [{"id": "old"}],
    }

    view = build_ledger_prompt_view(ledger, chapter_number=8)

    assert [item["id"] for item in view["open_plot_threads"]] == ["due", "late"]
    assert "accepted_chapters" not in view
    assert "chapter_commits" not in view
    assert "resolved_conflicts" not in view


def test_compact_json_shrinks_streams_proportionally():
    """体积贪心会先把记录最宽的流整个抽干；按比例收缩必须让各流同步缩水。"""
    data = {
        # facts 记录字段最多、单条最大，正是旧策略下第一个被清空的流。
        "facts": [
            {
                "id": f"F{i:03d}",
                "fact": f"事实{i}",
                "value": f"取值{i}",
                "source": "contract",
                "first_stated_at": "scene_1",
            }
            for i in range(96)
        ],
        "timeline_events": [
            {"id": f"TL{i:03d}", "event": f"事件{i}", "time": "00:12"}
            for i in range(48)
        ],
        "character_updates": [
            {"id": f"CU{i:03d}", "character": "人物", "attribute": f"属性{i}"}
            for i in range(48)
        ],
    }

    parsed = json.loads(compact_json(data, 8000))

    ratios = {
        key: len(parsed[key]) / len(data[key])
        for key in ("facts", "timeline_events", "character_updates")
    }
    # 每个流都必须留下内容，且保留比例彼此接近。
    assert all(value > 0 for value in ratios.values())
    assert max(ratios.values()) - min(ratios.values()) < 0.15
    # facts 条数最多，按比例也应当保留最多。
    assert len(parsed["facts"]) > len(parsed["timeline_events"])


def test_ledger_view_keeps_recently_touched_records_first(tmp_path):
    """久未被任何章节提起的记录先让位；被复述过的旧事实要活下来。"""
    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Mystery", "Subgenre": "Legal Thriller"})

    for chapter in range(1, 6):
        facts = [{"id": f"N{chapter}{i}", "fact": f"噪音{chapter}-{i}"} for i in range(3)]
        if chapter == 1:
            facts.append({"id": "FOUNDATION", "fact": "第一章确立的基础事实"})
        if chapter == 5:
            # 第五章复述基础事实，等同于把它重新标记为在用。
            facts.append({"id": "FOUNDATION", "fact": "第一章确立的基础事实"})
        manager.accept_chapter(
            chapter,
            {"chapter": chapter, "core_question": "q"},
            {"average_score": 4.0, "passed": True},
            chapter_delta={"facts_added": facts},
            expected_revision=chapter - 1,
        )

    view = build_ledger_prompt_view(manager.load_suspense_ledger(), 6)
    order = [record["id"] for record in view["facts"]]

    assert order[0] == "FOUNDATION" or order.index("FOUNDATION") < order.index("N10")
    assert order.index("FOUNDATION") < order.index("N20")


def test_compact_json_rejects_a_budget_below_the_envelope():
    with pytest.raises(ValueError):
        compact_json({"facts": [1, 2, 3]}, MIN_COMPACT_JSON_CHARS - 1)


def test_compact_json_honours_the_smallest_supported_budget():
    text = compact_json(
        {"facts": [{"id": f"F{i}", "value": "x" * 40} for i in range(20)]},
        MIN_COMPACT_JSON_CHARS,
    )
    assert len(text) <= MIN_COMPACT_JSON_CHARS
    assert json.loads(text)["_context_meta"]["truncated"] is True


def test_compact_json_stays_fast_on_a_large_ledger():
    """逐条 pop 后全量重新序列化会退化成 O(n²)：长篇账本曾在这里耗时数秒。"""
    import time

    data = {
        "facts": [
            {
                "id": f"F{i:04d}",
                "fact": f"事实{i}",
                "value": f"取值{i}",
                "source": "contract",
                "first_stated_at": "scene_1",
            }
            for i in range(480)
        ],
        "timeline_events": [
            {"id": f"TL{i:04d}", "event": f"事件{i}", "time": "00:12"}
            for i in range(240)
        ],
    }

    start = time.perf_counter()
    text = compact_json(data, 8000)
    elapsed = time.perf_counter() - start

    assert len(text) <= 8000
    assert json.loads(text)["facts"]
    # 二分收缩约 25 次序列化即可收敛；逐条 pop 需要数百次，耗时高一到两个数量级。
    assert elapsed < 1.0


def test_hollow_case_bible_is_marked_incomplete(tmp_path):
    """能解析成 JSON 不等于说了什么：空壳底稿必须与可用底稿区分开。"""
    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Mystery", "Subgenre": "Legal Thriller"})

    saved = manager.save_case_bible(
        {
            "central_question": "",
            "central_conflict": {},
            "truth": [],
            "chronology": [],
        },
        "一份过于单薄的大纲",
    )

    assert saved["status"] == "incomplete"
    assert any("central_question" in gap for gap in saved["gaps"])
    assert any("truth" in gap for gap in saved["gaps"])
    assert any("chronology" in gap for gap in saved["gaps"])


def test_case_bible_gaps_flags_missing_domain_conflict_fields(tmp_path):
    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Mystery", "Subgenre": "Legal Thriller"})

    spine = _complete_bible_spine()
    spine["central_conflict"] = {"legal_answer": "只填了一个字段"}
    saved = manager.save_case_bible(spine, "大纲文本")

    assert saved["status"] == "incomplete"
    assert any("truth_answer" in gap for gap in saved["gaps"])


def test_llm_failure_still_reports_degraded_rather_than_incomplete(tmp_path):
    """调用失败和内容单薄是两种毛病，不能混为一谈。"""
    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Mystery", "Subgenre": "Legal Thriller"})

    saved = manager.save_case_bible(
        {"case_bible_warning": "大模型超时", "truth": []},
        "大纲文本",
    )

    assert saved["status"] == "degraded"


def _write_structure_contract(tmp_path, sections):
    structure_dir = tmp_path / "story" / "structure"
    structure_dir.mkdir(parents=True, exist_ok=True)
    (structure_dir / "act_1.md").write_text("散文大纲。", encoding="utf-8")
    (structure_dir / "structure_contract.json").write_text(
        json.dumps({"sections": sections}, ensure_ascii=False), encoding="utf-8"
    )


def _declared_sections():
    return [
        {
            "section": "第1幕",
            "section_index": 1,
            "total_sections": 2,
            "central_question": "谁伪造了法医时间戳？",
            "central_conflict": {
                "legal_answer": "流浪汉纵火",
                "truth_answer": "专案组灭口",
                "moral_question": "结案率与程序正义",
            },
            "truths_introduced": [
                {"id": "T011", "fact": "门禁时钟被调慢", "reveal_at_section": "第2幕"},
                {"id": "T012", "fact": "副检验员死于火灾之前", "reveal_at_section": "第2幕"},
            ],
            "chronology_events": [
                {
                    "id": "TL011",
                    "order": 1,
                    "event": "零点十二分运尸车进入法医中心",
                    "known_initially_by": [],
                }
            ],
            "threads_opened": [
                {"id": "PT011", "thread": "时间戳由谁伪造", "must_close_by_section": 2}
            ],
        },
        {
            "section": "第2幕",
            "section_index": 2,
            "total_sections": 2,
            "truths_introduced": [
                {"id": "T021", "fact": "原始硬盘被转移", "reveal_at_section": "第2幕"}
            ],
            "chronology_events": [
                {
                    "id": "TL021",
                    "order": 2,
                    "event": "凌晨一点原始硬盘被转移",
                    "known_initially_by": ["专案组长"],
                }
            ],
            "threads_opened": [],
            "threads_closed": ["PT011"],
        },
    ]


def test_declared_spine_comes_from_the_structure_contract(tmp_path):
    """结构阶段声明过的骨架是作者写下的，不该再靠模型从散文里猜。"""
    _write_structure_contract(tmp_path, _declared_sections())
    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Mystery", "Subgenre": "Legal Thriller"})

    spine = manager.declared_story_spine()

    assert spine["central_question"] == "谁伪造了法医时间戳？"
    assert spine["central_conflict"]["truth_answer"] == "专案组灭口"
    assert [record["id"] for record in spine["truth"]] == ["T011", "T012", "T021"]
    assert [record["id"] for record in spine["chronology"]] == ["TL011", "TL021"]
    assert spine["chronology"][0]["event"] == "零点十二分运尸车进入法医中心"
    assert all(
        record["event"] not in {truth["fact"] for truth in spine["truth"]}
        for record in spine["chronology"]
    )


def test_declared_spine_fills_the_gaps_a_hollow_extraction_leaves(tmp_path):
    _write_structure_contract(tmp_path, _declared_sections())
    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Mystery", "Subgenre": "Legal Thriller"})

    hollow = {"central_question": "", "central_conflict": {}, "truth": [], "chronology": []}
    assert manager.save_case_bible(dict(hollow), "ctx")["status"] == "incomplete"

    merged = manager.merge_declared_story_spine(hollow)
    saved = manager.save_case_bible(merged, manager.load_design_context())

    assert saved["status"] == "ready"
    assert saved["gaps"] == []


def test_editing_only_the_structure_contract_still_refreshes_the_bible(tmp_path):
    """契约不是 .md，如果不并进设计上下文，改了它不会触发重建。"""
    sections = _declared_sections()
    _write_structure_contract(tmp_path, sections)
    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Mystery", "Subgenre": "Legal Thriller"})

    context = manager.load_design_context()
    assert "structure_contract.json" in context

    saved = manager.save_case_bible(
        {**_complete_bible_spine(), **manager.declared_story_spine()}, context
    )
    assert not manager.case_bible_needs_refresh(saved, manager.load_design_context())

    sections[0]["central_question"] = "到底是谁下的灭口令？"
    _write_structure_contract(tmp_path, sections)

    assert manager.case_bible_needs_refresh(saved, manager.load_design_context())


def test_changing_a_non_spine_contract_field_also_refreshes_the_bible(tmp_path):
    sections = _declared_sections()
    _write_structure_contract(tmp_path, sections)
    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Mystery", "Subgenre": "Legal Thriller"})
    context = manager.load_design_context()
    saved = manager.save_case_bible(
        manager.merge_declared_story_spine(_complete_bible_spine()), context
    )

    # threads_opened 不会被摊平进 case_bible，但仍属于完整结构契约；修改它也必须刷新。
    sections[0]["threads_opened"][0]["must_close_by_section"] = 1
    _write_structure_contract(tmp_path, sections)

    assert manager.case_bible_needs_refresh(saved, manager.load_design_context())


def test_declared_spine_merges_lists_without_losing_model_extraction(tmp_path):
    _write_structure_contract(tmp_path, _declared_sections())
    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Mystery", "Subgenre": "Legal Thriller"})
    inferred = {
        "central_question": "模型猜测的问题",
        "central_conflict": {"model_note": "来自章节大纲"},
        "truth": [
            {"id": "T011", "fact": "模型对同一事实的旧表述", "extra": "保留"},
            {"id": "T099", "fact": "章节大纲补充事实"},
        ],
        "chronology": [
            {"order": 3, "event": "第二章大纲补充的真实事件"}
        ],
    }

    merged = manager.merge_declared_story_spine(inferred)

    truths = {item["id"]: item for item in merged["truth"]}
    assert truths["T011"]["fact"] == "门禁时钟被调慢"
    assert truths["T011"]["extra"] == "保留"
    assert truths["T099"]["fact"] == "章节大纲补充事实"
    assert any(item["event"] == "第二章大纲补充的真实事件" for item in merged["chronology"])


def test_project_without_a_structure_contract_still_works(tmp_path):
    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Mystery", "Subgenre": "Legal Thriller"})

    assert manager.declared_story_spine() == {}
    assert manager.load_structure_contract() == {}
