"""Tests for the bounded design-generation-review chapter loop."""

import json

import pytest

from agents.review.legal_suspense_review_agent import DomainReview, SCORE_DIMENSIONS
from core.generation.chapter_generation_loop import ChapterGenerationLoop, QualityGateError
from core.generation.chapter_acceptance import (
    ChapterAcceptanceError,
    ValidationIssue,
    ValidationReport,
)
from core.generation.cancellation import CancelToken, GenerationCancelled
from core.generation.domain_profiles import HORROR
from core.generation.story_ledger import StoryLedgerManager


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
    acceptance = loop.accept_result(1, result)

    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["accepted_chapters"][0]["chapter"] == 1
    assert ledger["clues"][0]["id"] == "C001"
    assert ledger["revision"] == 1
    assert acceptance.committed_revision == 1
    assert result.base_revision == 0


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


def test_strict_planning_contract_retries_plan_without_rebuilding_contract(tmp_path):
    class FailOncePlanReviewer(PassingReviewer):
        def __init__(self):
            super().__init__()
            self.plan_reviews = 0
            self.revision_count = 0

        def review_plan(self, *args):
            self.plan_reviews += 1
            return failed_review("plan") if self.plan_reviews == 1 else passed_review("plan")

        def revise_plan(self, scene_plan, *args):
            self.revision_count += 1
            return scene_plan.replace("证人交出收据", "证人当面交出收据")

        def build_chapter_contract(self, *args):
            raise AssertionError("严格前置模式不得在写作阶段重建契约")

    reviewer = FailOncePlanReviewer()
    contract = PassingReviewer().build_chapter_contract(1)
    contract.update(
        {
            "origin": "scene_planning",
            "schema_version": 2,
            "facts_added": [],
            "facts_confirmed": [],
            "facts_contradicted": [],
            "timeline_events": [],
            "character_updates": [],
            "plot_thread_updates": [],
        }
    )
    StoryLedgerManager(str(tmp_path)).save_contract(1, contract, PLAN)
    loop = ChapterGenerationLoop(
        str(tmp_path),
        "hosted-llm",
        reviewer=reviewer,
        require_planning_contract=True,
        max_plan_retries=2,
    )

    result = loop.run(
        1,
        PLAN,
        {},
        "世界观",
        lambda **kwargs: f"第{kwargs['scene_number']}场正文。",
    )

    assert reviewer.revision_count == 1
    assert result.plan_revised is True
    assert "当面交出" in result.plan_content
    assert result.contract["origin"] == "scene_planning"


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


def test_final_acceptance_retries_prose_and_reruns_quality_reviews(tmp_path):
    class AcceptanceRepairReviewer(PassingReviewer):
        def __init__(self):
            super().__init__()
            self.acceptance_repairs = 0

        def revise_for_acceptance(self, scenes, scene_plans, report, contract):
            self.acceptance_repairs += 1
            return 1, scenes[0] + " 已补上验收要求的明确表述。"

    class FlakyAcceptance:
        def __init__(self):
            self.calls = 0

        def accept(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ChapterAcceptanceError(
                    ValidationReport(
                        stage="artifact_validation",
                        issues=[
                            ValidationIssue(
                                "missing_prose_statement",
                                "正文缺少契约要求的明确表述",
                                repair_target="prose",
                            )
                        ],
                    )
                )
            return "accepted-after-repair"

    reviewer = AcceptanceRepairReviewer()
    acceptance = FlakyAcceptance()
    loop = ChapterGenerationLoop(
        str(tmp_path),
        "hosted-llm",
        reviewer=reviewer,
        acceptance_service=acceptance,
        max_acceptance_retries=2,
    )
    result = loop.run(
        1,
        PLAN,
        {},
        "世界观",
        lambda **kwargs: f"第{kwargs['scene_number']}场正文。",
    )
    chapter_path = tmp_path / "story" / "content" / "chapters" / "chapter_1.md"
    chapter_path.parent.mkdir(parents=True)
    chapter_path.write_text(result.chapter_content, encoding="utf-8")

    accepted = loop.accept_result(1, result, str(chapter_path))

    assert accepted == "accepted-after-repair"
    assert acceptance.calls == 2
    assert reviewer.acceptance_repairs == 1
    assert "已补上验收要求" in chapter_path.read_text(encoding="utf-8")
    assert result.retry_count == 1


class HorrorReviewer(PassingReviewer):
    """恐怖档案的契约字段与法律悬疑完全不同。"""

    def build_chapter_contract(self, chapter_number, *args):
        return {
            "chapter": chapter_number,
            "core_question": "地下室的声音从哪里来？",
            "reader_knows_after": ["声音只在无人注视时出现"],
            "anomaly_rule_uses": [
                {"id": "A001", "rule": "回声只在无人注视时出现", "cost": "听者失去一段记忆"}
            ],
            "dread_beats": [{"id": "D001", "signal": "墙内传来指甲刮擦声", "explained": False}],
            "personal_cost": "周砚再也无法独处",
            "cost_character": "周砚",
            "irreversible_change": "地下室被封死",
            "scene_boundaries": [],
        }


def test_non_legal_genre_runs_loop_and_commits_its_own_record_streams(tmp_path):
    reviewer = HorrorReviewer()
    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=reviewer,
    )

    result = loop.run(
        chapter_number=1,
        plan_content=PLAN,
        parameters={"Genre": "Horror", "Subgenre": "Cosmic Horror"},
        lore="镇上的老宅有一间封死的地下室。",
        generate_scene=lambda **kwargs: f"第{kwargs['scene_number']}场正文：声音又出现了。",
    )

    assert result.profile_key == "horror"
    assert loop.profile.key == "horror"
    assert loop.max_scene_retries == HORROR.max_scene_retries

    chapter_path = tmp_path / "story" / "content" / "chapters" / "chapter_1.md"
    chapter_path.parent.mkdir(parents=True)
    chapter_path.write_text(result.chapter_content, encoding="utf-8")
    loop.accept_result(1, result)

    ledger = json.loads(
        (tmp_path / "system" / "story_ledgers" / "suspense_ledger.json").read_text(encoding="utf-8")
    )
    # 恐怖档案把 anomaly_rule_uses / dread_beats 映射到两个通用记录流。
    assert [item["id"] for item in ledger["clues"]] == ["A001"]
    assert [item["id"] for item in ledger["evidence"]] == ["D001"]
    assert ledger["personal_costs"]["周砚"] == ["周砚再也无法独处"]
    assert ledger["revision"] == 1


class ChapterRepairReviewer(PassingReviewer):
    def __init__(self):
        super().__init__()
        self.chapter_review_count = 0
        self.revise_tails = []

    def review_chapter(self, *args):
        self.chapter_review_count += 1
        if self.chapter_review_count == 1:
            return failed_review("chapter", scope="scene_1")
        return passed_review("chapter")

    def revise_scene(self, scene_content, review, scene_plan, previous_scene_tail, *args):
        self.revise_tails.append(previous_scene_tail)
        return scene_content + " 已按章节评审修订。"


def test_chapter_level_repair_of_first_scene_keeps_previous_chapter_tail(tmp_path):
    previous_path = tmp_path / "story" / "content" / "chapters" / "chapter_1.md"
    previous_path.parent.mkdir(parents=True)
    previous_path.write_text("第一章结尾：证人把门禁卡交给了林衡。", encoding="utf-8")

    reviewer = ChapterRepairReviewer()
    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=reviewer,
    )

    result = loop.run(
        chapter_number=2,
        plan_content=PLAN,
        parameters={"Genre": "Mystery", "Subgenre": "Legal Thriller"},
        lore="世界观",
        generate_scene=lambda **kwargs: f"第{kwargs['scene_number']}场正文。",
    )

    assert reviewer.chapter_review_count == 2
    assert len(reviewer.revise_tails) == 1
    # 章节级修复第 1 场时必须仍然带着上一章结尾，否则重写的开头会与上一章脱节。
    assert "证人把门禁卡交给了林衡" in reviewer.revise_tails[0]
    assert "已按章节评审修订" in result.scenes[0]


class ExplodingReviewer(PassingReviewer):
    """关闭档不得触发任何领域评审调用。"""

    def build_chapter_contract(self, *args):
        raise AssertionError("quality loop off must not build an LLM contract")

    def deterministic_contract(self, chapter_number, parameters, scene_plan):
        return {"chapter": chapter_number, "core_question": "", "scene_boundaries": []}

    def review_plan(self, *args):
        raise AssertionError("quality loop off must not review the plan")

    def review_scene(self, *args):
        raise AssertionError("quality loop off must not review scenes")

    def review_chapter(self, *args):
        raise AssertionError("quality loop off must not review the chapter")


def test_quality_loop_off_skips_reviews_but_still_commits(tmp_path):
    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=ExplodingReviewer(),
    )

    result = loop.run(
        chapter_number=1,
        plan_content=PLAN,
        parameters={"Genre": "Horror", "Quality Loop": "off"},
        lore="世界观",
        generate_scene=lambda **kwargs: f"第{kwargs['scene_number']}场正文。",
    )

    assert result.chapter_review.passed
    assert result.chapter_review.reviewer_warning
    assert result.retry_count == 0
    assert len(result.scenes) == 2

    chapter_path = tmp_path / "story" / "content" / "chapters" / "chapter_1.md"
    chapter_path.parent.mkdir(parents=True)
    chapter_path.write_text(result.chapter_content, encoding="utf-8")
    acceptance = loop.accept_result(1, result)

    # 关闭档仍然推进账本，章节之间的 revision 链不会断。
    assert acceptance.committed_revision == 1


def test_strict_mode_raises_the_pass_threshold(tmp_path):
    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=PassingReviewer(),
    )

    loop.run(
        chapter_number=1,
        plan_content=PLAN,
        parameters={"Genre": "Horror", "Quality Loop": "strict"},
        lore="世界观",
        generate_scene=lambda **kwargs: "正文。",
    )

    assert loop.profile.pass_average == pytest.approx(HORROR.pass_average + 0.2)
    assert loop.max_scene_retries == 2


def test_failed_gate_reports_partial_scenes_for_archiving(tmp_path):
    class EmptySecondScene:
        def __init__(self):
            self.calls = 0

        def __call__(self, **kwargs):
            self.calls += 1
            return "第一场正文。" if kwargs["scene_number"] == 1 else ""

    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=PassingReviewer(),
    )

    with pytest.raises(QualityGateError) as excinfo:
        loop.run(
            chapter_number=4,
            plan_content=PLAN,
            parameters={"Genre": "Mystery", "Subgenre": "Legal Thriller"},
            lore="世界观",
            generate_scene=EmptySecondScene(),
        )

    assert excinfo.value.chapter_number == 4
    assert excinfo.value.partial_scenes == ["第一场正文。"]


def test_cancellation_stops_at_a_scene_boundary_without_committing(tmp_path):
    token = CancelToken()
    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=PassingReviewer(),
        cancel_token=token,
    )
    generated = []

    def generate_scene(**kwargs):
        generated.append(kwargs["scene_number"])
        token.cancel()  # 第一场写完后请求停止
        return f"第{kwargs['scene_number']}场正文。"

    with pytest.raises(GenerationCancelled):
        loop.run(
            chapter_number=1,
            plan_content=PLAN,
            parameters={"Genre": "Mystery", "Subgenre": "Legal Thriller"},
            lore="世界观",
            generate_scene=generate_scene,
        )

    # 取消发生在场景之间：第二场没有开工，账本也没有提交。
    assert generated == [1]
    ledger = json.loads(
        (tmp_path / "system" / "story_ledgers" / "suspense_ledger.json").read_text(encoding="utf-8")
    )
    assert ledger["accepted_chapters"] == []
    assert ledger["revision"] == 0
    assert not (tmp_path / "story" / "content" / "chapters").exists()


def _write_design_context(tmp_path):
    structure_dir = tmp_path / "story" / "structure"
    structure_dir.mkdir(parents=True, exist_ok=True)
    (structure_dir / "act_1.md").write_text("全书结构草稿。", encoding="utf-8")


def _write_declared_structure_contract(tmp_path):
    _write_design_context(tmp_path)
    contract = {
        "sections": [
            {
                "section": "第1幕",
                "section_index": 1,
                "total_sections": 1,
                "central_question": "谁伪造了法医时间戳？",
                "central_conflict": {
                    "legal_answer": "流浪汉纵火",
                    "truth_answer": "专案组灭口",
                    "moral_question": "结案率与程序正义",
                },
                "truths_introduced": [
                    {"id": "T011", "fact": "门禁时钟被调慢"},
                    {"id": "T012", "fact": "副检验员死于火灾之前"},
                    {"id": "T013", "fact": "原始硬盘被转移"},
                ],
                "chronology_events": [
                    {
                        "id": "TL011",
                        "order": 1,
                        "event": "零点十二分运尸车进入法医中心",
                        "known_initially_by": [],
                    }
                ],
                "threads_opened": [],
                "threads_closed": [],
            }
        ]
    }
    (tmp_path / "story" / "structure" / "structure_contract.json").write_text(
        json.dumps(contract, ensure_ascii=False), encoding="utf-8"
    )


class HollowBibleReviewer(PassingReviewer):
    """结构与大纲太单薄时，提炼出来的底稿就是这种空壳。"""

    def build_case_bible(self, parameters, lore, design_context, baseline):
        return {
            **baseline,
            "central_question": "",
            "central_conflict": {},
            "truth": [],
            "chronology": [],
        }


def test_hollow_case_bible_blocks_before_any_prose_is_written(tmp_path):
    _write_design_context(tmp_path)
    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Mystery", "Subgenre": "Legal Thriller"})

    scenes_written = []

    def generate_scene(**kwargs):
        scenes_written.append(kwargs["scene_number"])
        return "不应该被生成的正文。"

    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=HollowBibleReviewer(),
    )

    with pytest.raises(QualityGateError, match="尚不足以支撑跨章校验"):
        loop.run(
            chapter_number=1,
            plan_content=PLAN,
            parameters={"Genre": "Mystery", "Subgenre": "Legal Thriller"},
            lore="世界观",
            generate_scene=generate_scene,
        )

    # 拦在生成之前，而不是写完再报错。
    assert scenes_written == []
    assert manager.load_case_bible()["status"] == "incomplete"


def test_declared_structure_spine_makes_hollow_extraction_ready_end_to_end(tmp_path):
    _write_declared_structure_contract(tmp_path)
    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Mystery", "Subgenre": "Legal Thriller"})
    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=HollowBibleReviewer(),
    )

    result = loop.run(
        chapter_number=1,
        plan_content=PLAN,
        parameters={"Genre": "Mystery", "Subgenre": "Legal Thriller"},
        lore="世界观",
        generate_scene=lambda **kwargs: f"第 {kwargs['scene_number']} 场正文。",
    )

    saved = manager.load_case_bible()
    assert len(result.scenes) == 2
    assert saved["status"] == "ready"
    assert saved["gaps"] == []
    assert saved["central_question"] == "谁伪造了法医时间戳？"


def test_project_without_design_context_is_not_blocked(tmp_path):
    """还没走到结构阶段的项目不该被这道闸门拦住。"""
    manager = StoryLedgerManager(str(tmp_path))
    manager.initialize({"Genre": "Mystery", "Subgenre": "Legal Thriller"})

    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=PassingReviewer(),
    )

    result = loop.run(
        chapter_number=1,
        plan_content=PLAN,
        parameters={"Genre": "Mystery", "Subgenre": "Legal Thriller"},
        lore="世界观",
        generate_scene=lambda **kwargs: f"第 {kwargs['scene_number']} 场正文。",
    )

    assert len(result.scenes) == 2
