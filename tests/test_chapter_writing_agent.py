"""Regression tests for agentic chapter generation model and result handling."""

import logging

import pytest

from agents.writing.chapter_writing_agent import (
    ChapterInfo,
    ChapterWritingAgent,
    SceneReview,
)
from core.generation import ai_helper, helper_fns


def _bare_agent(tmp_path, app=None, model="hosted-llm"):
    agent = object.__new__(ChapterWritingAgent)
    agent.app = app
    agent.model = model
    agent.output_dir = str(tmp_path)
    agent.logger = logging.getLogger("chapter-writing-test")
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


def test_error_placeholder_is_not_a_completed_chapter(tmp_path):
    output_path = tmp_path / "chapter_1.md"
    output_path.write_text(
        "[[[ERROR GENERATING CHAPTER 1, SCENE 1: missing key]]]",
        encoding="utf-8",
    )
    assert not ChapterWritingAgent._is_valid_generated_output(str(output_path))

    output_path.write_text("这是有效的章节正文。", encoding="utf-8")
    assert ChapterWritingAgent._is_valid_generated_output(str(output_path))


def test_single_chapter_passes_review_arguments_by_name(tmp_path):
    agent = _bare_agent(tmp_path)
    agent.review_agent = object()
    agent._load_writing_context = lambda: {}
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
