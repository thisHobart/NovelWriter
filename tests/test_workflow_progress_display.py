import json
import os

from core.gui.app import workflow_step_visual
from core.generation.story_ledger import StoryLedgerManager
from core.generation.workflow_status import (
    BLOCKED,
    COMPLETE,
    EMPTY,
    PARTIAL,
    StageStatus,
    assess_workflow,
)


def test_not_started_step_with_existing_files_is_not_shown_as_empty():
    visual = workflow_step_visual("not_started", True)

    assert visual["text"] == "检测到已有文件"
    assert visual["indicator"] == "◌"
    assert visual["can_view"] is True


def test_not_started_step_without_files_stays_not_started():
    visual = workflow_step_visual("not_started", False)

    assert visual["text"] == "未开始"
    assert visual["can_view"] is False


def test_persisted_status_takes_priority_over_file_presence():
    assert workflow_step_visual("in_progress", False)["text"] == "进行中"
    assert workflow_step_visual("completed", True)["text"] == "已完成"
    assert workflow_step_visual("failed", True)["text"] == "失败"


def _write(root, relative, text):
    path = os.path.join(str(root), relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _contract(chapter, threads=()):
    return {
        "chapter": chapter,
        "origin": "scene_planning",
        "schema_version": 2,
        "facts_added": [],
        "facts_confirmed": [],
        "facts_contradicted": [],
        "timeline_events": [],
        "character_updates": [],
        "plot_thread_updates": list(threads),
    }


def _project(tmp_path, *, chapters=4):
    _write(tmp_path, "story/lore/generated_lore.md", "圣兰卡是一座海港城市。")
    _write(tmp_path, "story/lore/lore_contract.json", json.dumps({"entities": []}))
    _write(tmp_path, "story/structure/act_1.md", "第一幕大纲")
    _write(
        tmp_path,
        "story/structure/structure_contract.json",
        json.dumps(
            {
                "sections": [
                    {
                        "section": "第1幕",
                        "section_index": 1,
                        "total_sections": 1,
                        "central_question": "谁伪造了时间戳？",
                        "central_conflict": {
                            "legal_answer": "流浪汉纵火",
                            "truth_answer": "专案组灭口",
                            "moral_question": "结案率与程序正义",
                        },
                        "truths_introduced": [
                            {"id": "T011", "fact": "门禁时钟被调慢", "reveal_at_section": "第1幕"}
                        ],
                        "threads_opened": [],
                        "threads_closed": [],
                    }
                ]
            },
            ensure_ascii=False,
        ),
    )
    _write(
        tmp_path,
        "story/planning/chapter_outlines/chapter_outlines_x_a.md",
        "\n".join(f"### 第 {number} 章：标题" for number in range(1, chapters + 1)),
    )
    return StoryLedgerManager(str(tmp_path))


def test_a_stage_with_files_but_missing_chapters_is_not_reported_as_done(tmp_path):
    manager = _project(tmp_path, chapters=4)
    for chapter in (1, 2):
        manager.save_contract(chapter, _contract(chapter), "### 场景 1：占位")

    scenes = assess_workflow(str(tmp_path))["scenes"]

    assert scenes.state == PARTIAL
    assert (scenes.done, scenes.total) == (2, 4)
    assert "还缺第 3、4 章" in scenes.detail


def test_a_complete_but_inconsistent_plan_is_flagged_for_repair(tmp_path):
    manager = _project(tmp_path, chapters=4)
    for chapter in (1, 2, 3):
        manager.save_contract(chapter, _contract(chapter), "### 场景 1：占位")
    manager.save_contract(
        4,
        _contract(4, [{"id": "PT-002-01", "thread": "被调换的证物袋", "status": "closed"}]),
        "### 场景 1：占位",
    )

    scenes = assess_workflow(str(tmp_path))["scenes"]

    assert scenes.state == BLOCKED
    assert "PT-002-01" in scenes.detail


def test_a_valid_complete_plan_is_reported_as_done(tmp_path):
    manager = _project(tmp_path, chapters=4)
    for chapter in range(1, 5):
        manager.save_contract(chapter, _contract(chapter), "### 场景 1：占位")

    assessment = assess_workflow(str(tmp_path))

    assert assessment["lore"].state == COMPLETE
    assert assessment["structure"].state == COMPLETE
    assert assessment["scenes"].state == COMPLETE
    assert assessment["chapters"].state == EMPTY


def test_chapters_are_counted_against_the_outlined_total(tmp_path):
    _project(tmp_path, chapters=4)
    for chapter in (1, 2):
        _write(tmp_path, f"story/content/chapters/chapter_{chapter}.md", "正文")
    _write(tmp_path, "story/content/chapters/chapter_3.md", "   ")

    chapters = assess_workflow(str(tmp_path))["chapters"]

    assert chapters.state == PARTIAL
    assert (chapters.done, chapters.total) == (2, 4)


def test_lore_without_its_contract_is_not_complete(tmp_path):
    _write(tmp_path, "story/lore/generated_lore.md", "圣兰卡是一座海港城市。")

    assert assess_workflow(str(tmp_path))["lore"].state == PARTIAL


def test_the_artifacts_override_a_stale_completed_flag():
    blocked = StageStatus(BLOCKED, "第 8 章要了结悬念 PT-005-01（被调换的证物袋），但前面没有任何一章埋下它")

    visual = workflow_step_visual("completed", True, blocked)

    assert visual["indicator"] == "!"
    assert visual["text"] == "待修复"
    assert visual["detail"] == blocked.detail


def test_a_running_stage_still_reports_progress_over_the_artifacts():
    visual = workflow_step_visual("in_progress", True, StageStatus(EMPTY, "尚未规划场景"))

    assert visual["text"] == "进行中"


def test_partial_progress_is_shown_as_a_fraction():
    visual = workflow_step_visual("not_started", True, StageStatus(PARTIAL, "还缺第 3、4 章", 2, 4))

    assert visual["indicator"] == "◑"
    assert visual["text"] == "未完成（2/4）"
