"""Regression tests for agentic chapter generation model and result handling."""

import json
import logging
from types import SimpleNamespace

import pytest

from agents.base.agent import AgentResult
from agents.writing.chapter_writing_agent import (
    ChapterInfo,
    ChapterWritingPlan,
    ChapterWritingAgent,
    SceneReview,
)
from core.generation import ai_helper, helper_fns
from core.generation.story_ledger import StoryLedgerManager
from agents.writing.chapter_writing_agent import get_chapter_progress


def _bare_agent(tmp_path, app=None, model="hosted-llm"):
    agent = object.__new__(ChapterWritingAgent)
    agent.app = app
    agent.model = model
    agent.output_dir = str(tmp_path)
    agent.logger = logging.getLogger("chapter-writing-test")
    agent.require_planning_contract = False
    return agent


@pytest.mark.parametrize("use_app", [True, False])
def test_scene_generation_uses_hosted_model_from_app_or_agent(monkeypatch, tmp_path, use_app):
    class HostedApp:
        @staticmethod
        def get_selected_model():
            return "hosted-llm"

    agent = _bare_agent(tmp_path, app=HostedApp() if use_app else None)
    calls = {}

    def fake_send_prompt(prompt, model=None):
        calls["model"] = model
        return "生成的场景正文"

    monkeypatch.setattr(ai_helper, "send_prompt", fake_send_prompt)
    monkeypatch.setattr(helper_fns, "save_prompt_to_file", lambda *args, **kwargs: None)

    result = agent._generate_scene_prose(
        chapter_num=1,
        scene_num=1,
        scene_plan="### 场景 1：开端",
        context={"parameters": {}},
    )

    assert result == "生成的场景正文"
    assert calls["model"] == "hosted-llm"


def test_scene_generation_raises_instead_of_returning_error_placeholder(monkeypatch, tmp_path):
    agent = _bare_agent(tmp_path)

    def failing_send_prompt(prompt, model=None):
        raise RuntimeError("hosted endpoint unavailable")

    monkeypatch.setattr(ai_helper, "send_prompt", failing_send_prompt)
    monkeypatch.setattr(helper_fns, "save_prompt_to_file", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="hosted endpoint unavailable"):
        agent._generate_scene_prose(1, 1, "### 场景 1：开端", {"parameters": {}})


def test_scene_prompt_uses_selected_genre_and_conflict_priority(monkeypatch, tmp_path):
    agent = _bare_agent(tmp_path)
    calls = {}

    def fake_send_prompt(prompt, model=None):
        calls["prompt"] = prompt
        return "生成的法律悬疑场景"

    monkeypatch.setattr(ai_helper, "send_prompt", fake_send_prompt)
    monkeypatch.setattr(helper_fns, "save_prompt_to_file", lambda *args, **kwargs: None)

    agent._generate_scene_prose(
        1,
        1,
        "### 场景 1：庭审前夜",
        {
            "parameters": {
                "Genre": "Mystery",
                "Subgenre": "Legal Thriller",
                "Story Length": "Novel (Epic)",
                "Story Structure": "Episodic",
            },
            "lore": "新都法院是主要地点。",
        },
    )

    prompt = calls["prompt"]
    assert "悬疑推理（法律惊悚）" in prompt
    assert "请撰写科幻" not in prompt
    assert "作品参数（最高优先级）" in prompt
    assert "事实冲突时依次以作品参数、整体世界观、当前场景规划为准" in prompt
    assert "采用自然、克制的现代中文小说语言" in prompt
    assert "不得把城市改写成星球" in prompt


def test_lore_sanitizer_removes_leading_model_analysis():
    dirty_lore = """**Defining the Genre**
The model is deciding how to frame the story.

**Analyzing the Elements**
The model is still planning.

**一、核心世界观与社会背景**
故事发生在新都，司法系统是冲突中心。
"""

    cleaned = ChapterWritingAgent._sanitize_lore_content(dirty_lore)

    assert cleaned.startswith("**一、核心世界观与社会背景**")
    assert "Defining the Genre" not in cleaned
    assert "Analyzing the Elements" not in cleaned


@pytest.mark.parametrize(
    "faction, expected",
    [
        (
            {
                "name": "新都检察院",
                "description": "负责重大刑事案件公诉。",
                "type": "司法机关",
                "jurisdiction": "新都",
                "goals": ["查明证据链", "赢得审判"],
            },
            ["势力名称：新都检察院", "简介：负责重大刑事案件公诉。", "主要目标：查明证据链, 赢得审判"],
        ),
        (
            {
                "faction_name": "旧版势力",
                "faction_profile": "旧版字段仍应兼容。",
                "primary_traits": ["谨慎"],
            },
            ["势力名称：旧版势力", "简介：旧版字段仍应兼容。", "主要特征：谨慎"],
        ),
    ],
)
def test_faction_summary_supports_current_and_legacy_schema(tmp_path, faction, expected):
    agent = _bare_agent(tmp_path)
    lore_dir = tmp_path / "story" / "lore"
    lore_dir.mkdir(parents=True)
    (lore_dir / "factions.json").write_text(
        json.dumps([faction], ensure_ascii=False),
        encoding="utf-8",
    )

    summary = agent._load_faction_summary()

    for text in expected:
        assert text in summary
    assert "势力名称：无" not in summary


def test_non_scifi_scene_conflict_stops_generation(monkeypatch, tmp_path):
    agent = _bare_agent(tmp_path)
    monkeypatch.setattr(ai_helper, "send_prompt", lambda *args, **kwargs: pytest.fail("LLM should not be called"))

    with pytest.raises(ValueError, match="行星"):
        agent._generate_scene_prose(
            1,
            1,
            "### 场景 1：圣兰卡星\n环境：行星",
            {
                "parameters": {"Genre": "Mystery", "Subgenre": "Legal Thriller"},
                "lore": "圣兰卡是一座沿海城市。",
            },
        )


def test_error_placeholder_is_not_a_completed_chapter(tmp_path):
    output_path = tmp_path / "chapter_1.md"
    output_path.write_text(
        "[[[ERROR GENERATING CHAPTER 1, SCENE 1: missing key]]]",
        encoding="utf-8",
    )
    assert not ChapterWritingAgent._is_valid_generated_output(str(output_path))

    output_path.write_text("这是有效的章节正文。", encoding="utf-8")
    assert ChapterWritingAgent._is_valid_generated_output(str(output_path))


def test_unaccepted_prose_file_is_not_treated_as_a_completed_chapter(tmp_path):
    output_path = tmp_path / "story" / "content" / "chapters" / "chapter_15.md"
    output_path.parent.mkdir(parents=True)
    output_path.write_text("第十五章尚未通过验收的正文。", encoding="utf-8")
    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Mystery"})

    assert not _bare_agent(tmp_path)._is_completed_chapter(str(output_path), 15)

    manager.accept_chapter(
        15,
        {},
        {},
        chapter_delta={"content_hash": "accepted", "contract_hash": "contract"},
        expected_revision=0,
    )
    assert _bare_agent(tmp_path)._is_completed_chapter(str(output_path), 15)

    manager.require_chapter_regeneration(15, "author_accepted_reversal", ["F-014-02"])
    assert not _bare_agent(tmp_path)._is_completed_chapter(str(output_path), 15)


def test_single_chapter_passes_review_arguments_by_name(tmp_path):
    agent = _bare_agent(tmp_path)
    agent.review_agent = object()
    # 所有题材现在都走质量闭环；关闭档位让本用例专注于评审参数传递，不触发大模型。
    agent._load_writing_context = lambda: {"parameters": {"Quality Loop": "off"}}
    agent._generate_scene_prose = lambda *args, **kwargs: "生成的场景正文"

    scene_review = SceneReview(
        scene_number=1,
        chapter_number=7,
        quality_score=0.8,
        word_count=100,
        issues=[],
        strengths=["完整"],
        suggestions=[],
        timestamp="2026-08-14T00:00:00",
        confidence=0.9,
    )
    agent._review_scene = lambda *args, **kwargs: scene_review
    review_call = {}

    def fake_review_chapter(**kwargs):
        review_call.update(kwargs)
        return None

    agent._review_chapter = fake_review_chapter

    scene_path = tmp_path / "plans" / "scene_7.md"
    scene_path.parent.mkdir(parents=True)
    scene_path.write_text("### 场景 1：开端\n规划内容", encoding="utf-8")

    chapter_info = ChapterInfo(
        chapter_number=7,
        section_name="Rising Action",
        scene_plan_file="plans/scene_7.md",
        output_file="chapters/chapter_7.md",
    )
    result = agent._write_single_chapter(chapter_info)

    assert result.success
    assert review_call["chapter_number"] == 7
    assert review_call["section_name"] == "Rising Action"
    assert review_call["scene_reviews"] == [scene_review]
    assert (tmp_path / "chapters" / "chapter_7.md").read_text(encoding="utf-8") == "生成的场景正文"


def test_scene_review_uses_review_agent_public_contract(tmp_path):
    agent = _bare_agent(tmp_path)
    calls = {}

    class ReviewAgent:
        @staticmethod
        def review_step_output(step_name, content, context=None):
            calls.update(step_name=step_name, content=content, context=context)
            return SimpleNamespace(
                quality_score=0.82,
                issues_found=["问题"],
                strengths_found=["优点"],
                improvement_suggestions=["建议"],
                confidence=0.91,
            )

    agent.review_agent = ReviewAgent()
    agent.quality_thresholds = SimpleNamespace(minimum_scene_quality=0.6)
    agent.record_quality_trend = lambda **kwargs: None
    agent.should_retry_based_on_thresholds = lambda **kwargs: False

    review = agent._review_scene("场景正文", 2, 3)

    assert calls == {
        "step_name": "scene",
        "content": "场景正文",
        "context": {"chapter_number": 3, "scene_number": 2},
    }
    assert review.quality_score == 0.82
    assert review.issues == ["问题"]
    assert review.strengths == ["优点"]
    assert review.suggestions == ["建议"]


def test_chapter_review_passes_content_to_content_analyzers(tmp_path):
    agent = _bare_agent(tmp_path)
    agent.quality_thresholds = SimpleNamespace(
        coherence_threshold=0.0,
        pacing_threshold=0.0,
        character_development_threshold=0.0,
        minimum_chapter_quality=0.0,
    )
    agent.record_quality_trend = lambda **kwargs: None
    agent.should_retry_based_on_thresholds = lambda **kwargs: False
    agent._analyze_chapter_coherence = lambda reviews: 0.8
    calls = {}
    agent._analyze_chapter_pacing = lambda content, reviews: calls.setdefault(
        "pacing", (content, reviews)
    ) and 0.8
    agent._analyze_character_development = lambda content: calls.setdefault(
        "character", content
    ) and 0.8
    scene_review = SceneReview(
        scene_number=1,
        chapter_number=4,
        quality_score=0.8,
        word_count=100,
        issues=[],
        strengths=[],
        suggestions=[],
        timestamp="2026-08-30T00:00:00",
        confidence=0.9,
    )

    review = agent._review_chapter("章节正文", [scene_review], 4, "Act II")

    assert review.pacing_score == 0.8
    assert review.character_development_score == 0.8
    assert calls["pacing"] == ("章节正文", [scene_review])
    assert calls["character"] == "章节正文"


def test_automatic_progress_detects_structured_workspace(tmp_path):
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "parameters.txt").write_text(
        "Story Structure: Episodic Structure\nStory Length: Novel (Epic)\n",
        encoding="utf-8",
    )
    outline_dir = tmp_path / "story" / "planning" / "chapter_outlines"
    plan_dir = tmp_path / "story" / "planning" / "detailed_scene_plans"
    chapter_dir = tmp_path / "story" / "content" / "chapters"
    outline_dir.mkdir(parents=True)
    plan_dir.mkdir(parents=True)
    chapter_dir.mkdir(parents=True)
    (outline_dir / "chapter_outlines_episodic_structure_episode_1_introduction.md").write_text(
        "### 第 1 章：开端\n\n### 第 2 章：质证\n",
        encoding="utf-8",
    )
    (plan_dir / "scenes_episodic_structure_episode_1_introduction_ch1.md").write_text(
        "### 场景 1：开端\n规划",
        encoding="utf-8",
    )
    (chapter_dir / "chapter_1.md").write_text("第一章正文", encoding="utf-8")

    progress = get_chapter_progress(str(tmp_path))

    assert progress["total_chapters"] == 2
    assert progress["completed_chapters"] == 1
    assert progress["next_chapter"] == 2
    assert not progress["next_chapter_ready"]
    assert progress["missing_scene_plans"] == [2]


def test_review_actions_bind_the_context_needed_to_regenerate(monkeypatch, tmp_path):
    """人工放行/照建议重修必须把重生成上下文绑给质量闭环。

    验收有可能判定这一章的契约要让位于账本，那时旧正文全部作废、要逐场重写。
    没绑上下文的话，作者点下去要等到最后一步才收到一句「缺少完整重生成上下文」，
    而那时调用已经烧掉了。
    """
    from core.generation import pending_review
    from agents.writing import chapter_writing_agent as agent_module

    bound = {}

    class RecordingLoop:
        def __init__(self, **kwargs):
            pass

        def bind_generation_context(self, parameters, lore, generate_scene,
                                    on_plan_revised=None):
            bound["parameters"] = parameters
            bound["lore"] = lore
            bound["generate_scene"] = generate_scene

        def accept_waived(self, chapter_number, result, reason, chapter_path=None):
            bound["reason"] = reason
            return SimpleNamespace(committed_revision=3, delta_path="")

    monkeypatch.setattr(agent_module, "ChapterGenerationLoop", RecordingLoop)

    agent = _bare_agent(tmp_path)
    agent.cancel_token = None
    agent.dir_manager = None
    monkeypatch.setattr(
        agent_module.ChapterWritingAgent,
        "_load_writing_context",
        lambda self: {"parameters": {"Genre": "Mystery"}, "lore": "世界观正文"},
    )
    monkeypatch.setattr(
        agent_module.ChapterWritingAgent,
        "analyze_chapter_structure",
        lambda self: (
            [ChapterInfo(
                chapter_number=4,
                section_name="Rising Action",
                scene_plan_file="plans/scene_4.md",
                output_file="chapters/chapter_4.md",
            )],
            {},
        ),
    )

    record = pending_review.build(
        4,
        message="第 4 章仍未通过章节级质量检查",
        review={"stage": "chapter", "passed": False, "pass_average": 3.2,
                "average_score": 2.9},
        prose="一场。\n\n---\n\n二场。",
        stage=pending_review.STAGE_CHAPTER,
        snapshot={"scenes": ["一场。", "二场。"], "chapter_review": {
            "stage": "chapter", "passed": False, "pass_average": 3.2}},
    )
    pending_review.save(record, str(tmp_path))

    result = agent.review_pending_chapter(4, "waive", reason="开篇我认了")

    assert result.success, result.messages
    assert bound["parameters"] == {"Genre": "Mystery"}
    assert bound["lore"] == "世界观正文"
    assert callable(bound["generate_scene"])
    assert bound["reason"] == "开篇我认了"
    # 通过之后待复审记录就不成立了。
    assert pending_review.load(str(tmp_path), 4) is None
    assert (tmp_path / "chapters" / "chapter_4.md").is_file()


def test_review_actions_refuse_a_half_written_draft(tmp_path):
    from core.generation import pending_review

    agent = _bare_agent(tmp_path)
    record = pending_review.build(
        4,
        message="第 4 章场景 2 在 2 次修订后仍未通过质量检查",
        review={},
        prose="只有一场。",
        stage=pending_review.STAGE_SCENE,
        snapshot={},
    )
    pending_review.save(record, str(tmp_path))

    result = agent.review_pending_chapter(4, "waive")

    assert not result.success
    assert "请重写本章" in result.messages[0]


def test_a_review_outage_is_recorded_as_pending_not_as_a_crash(monkeypatch, tmp_path):
    """评审没能出结论时，这一章要进待复审，而不是把整遍成果扔掉只留一句报错。"""
    from core.generation import pending_review
    from agents.writing import chapter_writing_agent as agent_module

    agent = _bare_agent(tmp_path)
    agent.review_agent = None
    agent._load_writing_context = lambda: {"parameters": {}}
    agent._generate_scene_prose = lambda *args, **kwargs: "一场正文。"

    def outage(self, **kwargs):
        raise agent_module.QualityGateError(
            "第 3 章的质量评审没能给出结论：plausibility 质量检查未能返回有效结果",
            partial_scenes=["一场正文。", "二场正文。"],
            chapter_number=3,
            snapshot={"scenes": ["一场正文。", "二场正文。"], "contract": {"chapter": 3}},
            verdict_unavailable=True,
        )

    monkeypatch.setattr(agent_module.ChapterGenerationLoop, "run", outage)

    scene_path = tmp_path / "plans" / "scene_3.md"
    scene_path.parent.mkdir(parents=True)
    scene_path.write_text("### 场景 1：开端\n规划一\n", encoding="utf-8")

    result = agent._write_single_chapter(
        ChapterInfo(
            chapter_number=3,
            section_name="Rising Action",
            scene_plan_file="plans/scene_3.md",
            output_file="chapters/chapter_3.md",
        )
    )

    assert not result.success
    record = pending_review.load(str(tmp_path), 3)
    assert record is not None
    assert record.needs_review_rerun
    assert "二场正文。" in record.prose


def test_failed_scene_archives_partial_prose_instead_of_writing_a_chapter(tmp_path):
    """质量闸门失败时，稿件目录必须保持干净，部分正文只进归档。"""
    agent = _bare_agent(tmp_path)
    agent.review_agent = None
    agent._load_writing_context = lambda: {"parameters": {"Quality Loop": "off"}}

    def flaky_scene(chapter_num, scene_num, *args, **kwargs):
        return "第一场正文。" if scene_num == 1 else ""

    agent._generate_scene_prose = flaky_scene

    scene_path = tmp_path / "plans" / "scene_3.md"
    scene_path.parent.mkdir(parents=True)
    scene_path.write_text(
        "### 场景 1：开端\n规划一\n\n### 场景 2：转折\n规划二\n",
        encoding="utf-8",
    )

    chapter_info = ChapterInfo(
        chapter_number=3,
        section_name="Rising Action",
        scene_plan_file="plans/scene_3.md",
        output_file="chapters/chapter_3.md",
    )
    result = agent._write_single_chapter(chapter_info)

    assert not result.success
    assert not (tmp_path / "chapters" / "chapter_3.md").exists()

    archived = result.data["archived_partial_prose"]
    assert archived
    archived_text = helper_fns.open_file(archived)
    assert "第一场正文。" in archived_text
    # 归档路径必须在 archive/ 下，绝不能落进 story/content/。
    assert "failed_generations" in archived.replace("\\", "/")
    assert "story/content" not in archived.replace("\\", "/")
    # 归档内容不得含有历史上的错误占位符。
    assert "[[[ERROR" not in archived_text

    # 失败同时留下待复审记录：界面据它把这一章标出来，而不是让它看起来没写过。
    from core.generation import pending_review

    record = pending_review.load(str(tmp_path), 3)
    assert record is not None
    assert record.prose_path == archived
    assert "第一场正文。" in record.prose
    assert record.message == result.messages[0].split("；")[0]
    # 只写到一半，谈不上放行或定向重修，界面据此把那两个出口关掉。
    assert not record.resumable


def test_batch_cancellation_keeps_finished_chapters_and_stops(tmp_path):
    """取消发生在章节边界：已写完的章节保留，未开始的章节不写。"""
    from core.generation.cancellation import CancelToken

    from agents.writing.chapter_writing_agent import QualityThresholds

    token = CancelToken()
    agent = _bare_agent(tmp_path)
    agent.review_agent = None
    agent.cancel_token = token
    agent.quality_thresholds = QualityThresholds()
    agent._load_writing_context = lambda: {"parameters": {"Quality Loop": "off"}}

    written = []

    def one_chapter_then_cancel(chapter_num, scene_num, *args, **kwargs):
        if chapter_num not in written:
            written.append(chapter_num)
        token.cancel()
        return f"第 {chapter_num} 章正文。"

    agent._generate_scene_prose = one_chapter_then_cancel

    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    chapter_infos = []
    for number in (1, 2, 3):
        (plan_dir / f"scene_{number}.md").write_text("### 场景 1：开端\n规划", encoding="utf-8")
        chapter_infos.append(
            ChapterInfo(
                chapter_number=number,
                section_name="Rising Action",
                scene_plan_file=f"plans/scene_{number}.md",
                output_file=f"chapters/chapter_{number}.md",
            )
        )

    plan = agent.create_writing_plan(chapter_infos, batch_size=3)
    result = agent.write_chapters_batch(chapter_infos, plan)

    assert written == [1]
    assert result.data["chapters_written"] == [1]
    assert result.success is False
    assert "已按请求停止" in result.messages[0]
    assert (tmp_path / "chapters" / "chapter_1.md").exists()
    assert not (tmp_path / "chapters" / "chapter_2.md").exists()
    assert not (tmp_path / "chapters" / "chapter_3.md").exists()
    # 取消不应被记成章节失败。
    assert result.data["errors"] == []


def test_batch_stops_after_first_rejected_chapter(tmp_path):
    agent = _bare_agent(tmp_path)
    agent.review_agent = None
    attempted = []

    def reject_first(chapter_info):
        attempted.append(chapter_info.chapter_number)
        return AgentResult(success=False, data={}, messages=["验收失败"], metrics={})

    agent._write_single_chapter = reject_first
    chapter_infos = [
        ChapterInfo(number, "Rising Action", f"scene_{number}.md", f"chapter_{number}.md")
        for number in (1, 2, 3)
    ]
    plan = ChapterWritingPlan(
        total_chapters=3,
        chapters_to_write=[1, 2, 3],
        chapters_completed=[],
        batch_size=3,
        enable_reviews=False,
    )

    result = agent.write_chapters_batch(chapter_infos, plan)

    assert attempted == [1]
    assert result.data["chapters_written"] == []
    assert result.data["errors"] == ["第 1 章：验收失败"]
    assert result.success is False


def test_batch_reports_started_and_durably_completed_chapters(tmp_path):
    agent = _bare_agent(tmp_path)
    agent.review_agent = None
    agent._write_single_chapter = lambda chapter_info: AgentResult(
        success=True,
        data={},
        messages=[],
        metrics={},
    )
    chapter_infos = [
        ChapterInfo(number, "Rising Action", f"scene_{number}.md", f"chapter_{number}.md")
        for number in (17, 18)
    ]
    plan = ChapterWritingPlan(
        total_chapters=18,
        chapters_to_write=[17, 18],
        chapters_completed=list(range(1, 17)),
        batch_size=2,
        enable_reviews=False,
    )
    progress = []

    result = agent.write_chapters_batch(
        chapter_infos,
        plan,
        progress_callback=lambda *event: progress.append(event),
    )

    assert result.success
    assert progress == [
        ("started", 17, 16, 18),
        ("completed", 17, 17, 18),
        ("started", 18, 17, 18),
        ("completed", 18, 18, 18),
    ]


def test_batch_preserves_conflict_details_for_the_gui_buttons(tmp_path):
    agent = _bare_agent(tmp_path)
    agent.review_agent = None
    conflict_issue = {
        "code": "fact_contradiction",
        "message": "需要作者裁定",
        "severity": "blocking",
        "repair_target": "human_decision",
        "details": {"id": "F-014-02"},
    }

    agent._write_single_chapter = lambda chapter_info: AgentResult(
        success=False,
        data={
            "chapter_number": chapter_info.chapter_number,
            "needs_human_decision": True,
            "adjudication": "awaiting_author",
            "conflict_issues": [conflict_issue],
            "conflicts": [{"id": "F-014-02"}],
        },
        messages=["等待作者裁定"],
        metrics={},
    )
    chapter_infos = [ChapterInfo(15, "Climax", "scene_15.md", "chapter_15.md")]
    plan = ChapterWritingPlan(
        total_chapters=1,
        chapters_to_write=[15],
        chapters_completed=[],
        batch_size=1,
        enable_reviews=False,
    )

    result = agent.write_chapters_batch(chapter_infos, plan)

    assert result.data["chapter_number"] == 15
    assert result.data["needs_human_decision"] is True
    assert result.data["adjudication"] == "awaiting_author"
    assert result.data["conflict_issues"] == [conflict_issue]


def test_batch_partial_write_is_not_reported_as_success(tmp_path):
    agent = _bare_agent(tmp_path)
    agent.review_agent = None

    def write_first_then_reject(chapter_info):
        if chapter_info.chapter_number == 1:
            return AgentResult(success=True, data={}, messages=[], metrics={})
        return AgentResult(success=False, data={}, messages=["验收失败"], metrics={})

    agent._write_single_chapter = write_first_then_reject
    chapter_infos = [
        ChapterInfo(number, "Rising Action", f"scene_{number}.md", f"chapter_{number}.md")
        for number in (1, 2, 3)
    ]
    plan = ChapterWritingPlan(
        total_chapters=3,
        chapters_to_write=[1, 2, 3],
        chapters_completed=[],
        batch_size=3,
        enable_reviews=False,
    )

    result = agent.write_chapters_batch(chapter_infos, plan)

    assert result.success is False
    assert result.data["chapters_written"] == [1]
    assert result.data["errors"] == ["第 2 章：验收失败"]


def test_blocked_batch_reports_which_chapter_stopped_it(tmp_path):
    """失败消息要自带章号和原因。

    原来只报「已写完 1 章，发生 1 个错误」，真正的原因留在 data["errors"] 里，
    界面上看不到，只能去翻日志才知道停在哪一章、为什么停。
    """
    agent = _bare_agent(tmp_path)
    agent.review_agent = None

    def write_first_then_reject(chapter_info):
        if chapter_info.chapter_number == 1:
            return AgentResult(success=True, data={}, messages=[], metrics={})
        return AgentResult(
            success=False,
            data={},
            messages=["第 2 章场景 1 在 2 次修订后仍未通过质量检查"],
            metrics={},
        )

    agent._write_single_chapter = write_first_then_reject
    chapter_infos = [
        ChapterInfo(number, "Rising Action", f"scene_{number}.md", f"chapter_{number}.md")
        for number in (1, 2, 3)
    ]
    plan = ChapterWritingPlan(
        total_chapters=3,
        chapters_to_write=[1, 2, 3],
        chapters_completed=[],
        batch_size=3,
        enable_reviews=False,
    )

    result = agent.write_chapters_batch(chapter_infos, plan)

    assert result.data["blocked_chapter"] == 2
    message = result.messages[0]
    assert "停在第 2 章" in message
    assert "仍未通过质量检查" in message
    assert "从第 2 章继续" in message
