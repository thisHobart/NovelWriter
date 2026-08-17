"""Tests for the bounded design-generation-review chapter loop."""

import json

import pytest

from agents.review.legal_suspense_review_agent import DomainReview, SCORE_DIMENSIONS
from core.generation.chapter_generation_loop import ChapterGenerationLoop, QualityGateError


PLAN = """### 场景 1：收据
证人交出收据。

### 场景 2：质证
律师核对门禁记录。
"""


def passed_review(stage):
    return DomainReview(
        stage=stage,
        passed=True,
        scores={dimension: 3.6 for dimension in SCORE_DIMENSIONS},
    )


def failed_review(stage, scope="scene_1"):
    return DomainReview(
        stage=stage,
        passed=False,
        scores={dimension: 2.0 for dimension in SCORE_DIMENSIONS},
        repair_scope=scope,
        repair_instructions=["删除重复动作，只保留收据时间这一条推进线"],
    )


class PassingReviewer:
    def __init__(self):
        self.scene_inputs = []

    def build_chapter_contract(self, chapter_number, *args):
        return {
            "chapter": chapter_number,
            "core_question": "收据为何晚了三十二分钟？",
            "reader_knows_after": ["收据和门禁时间冲突"],
            "fair_play_clues": [{"id": "C001", "surface_meaning": "延迟"}],
            "personal_cost": "律师失去案卷访问权",
            "cost_character": "林衡",
            "irreversible_change": "调查转入私下",
            "scene_boundaries": [],
        }

    def review_plan(self, *args):
        return passed_review("plan")

    def review_scene(
        self,
        scene_content,
        scene_plan,
        scene_number,
        previous_scene_tail,
        next_scene_plan,
        *args,
    ):
        self.scene_inputs.append((scene_number, previous_scene_tail, next_scene_plan))
        return passed_review(f"scene_{scene_number}")

    def review_chapter(self, *args):
        return passed_review("chapter")

    def revise_plan(self, *args):
        raise AssertionError("passing plan must not be revised")

    def revise_scene(self, *args):
        raise AssertionError("passing scene must not be revised")


def test_loop_passes_continuity_and_commits_only_after_save(tmp_path):
    reviewer = PassingReviewer()
    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=reviewer,
    )
    generation_calls = []

    def generate_scene(**kwargs):
        generation_calls.append(kwargs)
        return f"第{kwargs['scene_number']}场正文：证据状态已经改变。"

    result = loop.run(
        chapter_number=1,
        plan_content=PLAN,
        parameters={"Genre": "Mystery", "Subgenre": "Legal Thriller"},
        lore="圣兰卡市采用统一的刑事诉讼规则。",
        generate_scene=generate_scene,
    )

    assert generation_calls[0]["previous_scene_tail"] == ""
    assert "第1场正文" in generation_calls[1]["previous_scene_tail"]
    assert "场景 2" in generation_calls[0]["next_scene_plan"]
    assert generation_calls[1]["next_scene_plan"] == ""

    ledger_path = tmp_path / "system" / "story_ledgers" / "suspense_ledger.json"
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["accepted_chapters"] == []

    chapter_path = tmp_path / "story" / "content" / "chapters" / "chapter_1.md"
    chapter_path.parent.mkdir(parents=True)
    chapter_path.write_text(result.chapter_content, encoding="utf-8")
    loop.accept_result(1, result)

    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["accepted_chapters"][0]["chapter"] == 1
    assert ledger["clues"][0]["id"] == "C001"


def test_first_scene_receives_previous_chapter_tail(tmp_path):
    previous_path = tmp_path / "story" / "content" / "chapters" / "chapter_1.md"
    previous_path.parent.mkdir(parents=True)
    previous_path.write_text("第一章结尾：证人把门禁卡交给了林衡。", encoding="utf-8")

    reviewer = PassingReviewer()
    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=reviewer,
    )
    calls = []

    loop.run(
        chapter_number=2,
        plan_content=PLAN,
        parameters={},
        lore="世界观",
        generate_scene=lambda **kwargs: calls.append(kwargs) or "续写正文。",
    )

    assert "证人把门禁卡交给了林衡" in calls[0]["previous_scene_tail"]


class AlwaysFailingPlanReviewer(PassingReviewer):
    def __init__(self):
        super().__init__()
        self.revision_count = 0

    def review_plan(self, *args):
        return failed_review("plan")

    def revise_plan(self, scene_plan, *args):
        self.revision_count += 1
        return scene_plan


def test_plan_retry_is_bounded(tmp_path):
    reviewer = AlwaysFailingPlanReviewer()
    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=reviewer,
        max_plan_retries=2,
    )

    with pytest.raises(QualityGateError, match="2 次修订后仍未通过"):
        loop.run(
            chapter_number=1,
            plan_content=PLAN,
            parameters={},
            lore="世界观",
            generate_scene=lambda **kwargs: pytest.fail("failed plans must not generate prose"),
        )

    assert reviewer.revision_count == 2


class OneRetryReviewer(PassingReviewer):
    def __init__(self):
        super().__init__()
        self.first_scene_review = True
        self.revision_count = 0

    def review_scene(self, *args):
        scene_number = args[2]
        if scene_number == 1 and self.first_scene_review:
            self.first_scene_review = False
            return failed_review("scene_1")
        return passed_review(f"scene_{scene_number}")

    def revise_scene(self, scene_content, *args):
        self.revision_count += 1
        return scene_content + " 已删除重复动作。"


def test_scene_failure_triggers_targeted_retry(tmp_path):
    reviewer = OneRetryReviewer()
    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=reviewer,
    )

    result = loop.run(
        chapter_number=1,
        plan_content=PLAN,
        parameters={},
        lore="世界观",
        generate_scene=lambda **kwargs: f"第{kwargs['scene_number']}场正文。",
    )

    assert reviewer.revision_count == 1
    assert result.retry_count == 1
    assert "已删除重复动作" in result.scenes[0]
