"""Tests for the bounded design-generation-review chapter loop."""

import json

import pytest

from agents.review.legal_suspense_review_agent import (
    DomainReview,
    DomainReviewError,
    SCORE_DIMENSIONS,
)
from core.generation.chapter_generation_loop import (
    ChapterGenerationLoop,
    QualityGateError,
    restore_result,
)
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

    def review_plan(self, *args, **kwargs):
        return passed_review("plan")

    def review_scene(
        self,
        scene_content,
        scene_plan,
        scene_number,
        previous_scene_tail,
        next_scene_plan,
        *args,
        **kwargs,
    ):
        self.scene_inputs.append((scene_number, previous_scene_tail, next_scene_plan))
        return passed_review(f"scene_{scene_number}")

    def review_chapter(self, *args, **kwargs):
        return passed_review("chapter")

    def revise_plan(self, *args, **kwargs):
        raise AssertionError("passing plan must not be revised")

    def revise_scene(self, *args, **kwargs):
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

    def review_plan(self, *args, **kwargs):
        return failed_review("plan")

    def revise_plan(self, scene_plan, *args, **kwargs):
        self.revision_count += 1
        return scene_plan


def test_plan_retry_stops_once_the_same_repair_survives_itself(tmp_path):
    """同一条修复意见原样退回来，就没有必要再要求第三遍。

    重试预算是拿来换新结果的。评审把上一轮提过的同一条要求原封不动再提一次，说明
    那条要求已经被照做过而问题依旧；用同样的说法再问一次模型，得到的还是同一稿。
    """
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

    assert reviewer.revision_count == 1


def test_plan_retry_spends_the_full_budget_while_feedback_keeps_changing(tmp_path):
    """反过来，评审每轮指出不同问题时，预算要照常花完。

    「无进展」判的是重复，不是失败本身；换了新问题就是新的一次机会，不能因为上
    一轮没过就提前收手。
    """

    class ShiftingPlanReviewer(PassingReviewer):
        def __init__(self):
            super().__init__()
            self.revision_count = 0
            self.round = 0

        def review_plan(self, *args, **kwargs):
            self.round += 1
            review = failed_review("plan")
            review.repair_instructions = [f"第 {self.round} 轮才发现的问题"]
            return review

        def revise_plan(self, scene_plan, *args, **kwargs):
            self.revision_count += 1
            return scene_plan

    reviewer = ShiftingPlanReviewer()
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

        def review_plan(self, *args, **kwargs):
            self.plan_reviews += 1
            return failed_review("plan") if self.plan_reviews == 1 else passed_review("plan")

        def revise_plan(self, scene_plan, *args, **kwargs):
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
            "chapter_function": "advance",
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

    def review_scene(self, *args, **kwargs):
        scene_number = args[2]
        if scene_number == 1 and self.first_scene_review:
            self.first_scene_review = False
            return failed_review("scene_1")
        return passed_review(f"scene_{scene_number}")

    def revise_scene(self, scene_content, *args, **kwargs):
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


def test_a_regressed_revision_is_not_used_as_the_next_starting_point(tmp_path):
    """重修不保证变好，接着变差的那一稿改，退步会一路累积。

    原先只在收尾时挑最好的一稿交出去，中间每一轮却始终接着最新的一稿往下改：第一轮
    改差了，第二轮就是在这份更差的稿子上改，等于把上一轮的退步当成新起点。
    """

    class RegressingReviewer(PassingReviewer):
        def __init__(self):
            super().__init__()
            self.round = 0
            self.revised_from = []

        def review_scene(self, *args, **kwargs):
            scene_number = args[2]
            if scene_number != 1:
                return passed_review(f"scene_{scene_number}")
            self.round += 1
            review = failed_review("scene_1")
            # 第一稿 2.0，重修后掉到 1.0：这一轮把稿子改差了。
            score = {1: 2.0, 2: 1.0}.get(self.round, 1.0)
            review.scores = {dimension: score for dimension in SCORE_DIMENSIONS}
            review.repair_instructions = [f"第 {self.round} 轮的意见"]
            return review

        def revise_scene(self, scene_content, *args, **kwargs):
            self.revised_from.append(scene_content)
            return scene_content + f" 改动{len(self.revised_from)}。"

    reviewer = RegressingReviewer()
    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=reviewer,
        max_scene_retries=2,
    )

    with pytest.raises(QualityGateError):
        loop.run(
            chapter_number=1,
            plan_content=PLAN,
            parameters={},
            lore="世界观",
            generate_scene=lambda **kwargs: f"第{kwargs['scene_number']}场正文。",
        )

    # 第二轮应当退回第一稿重来，而不是接着那份被改差的 1.0 稿。
    assert reviewer.revised_from == ["第1场正文。", "第1场正文。"]


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

    def review_chapter(self, *args, **kwargs):
        self.chapter_review_count += 1
        if self.chapter_review_count == 1:
            return failed_review("chapter", scope="scene_1")
        return passed_review("chapter")

    def revise_scene(self, scene_content, review, scene_plan, previous_scene_tail, *args, **kwargs):
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

    def review_plan(self, *args, **kwargs):
        raise AssertionError("quality loop off must not review the plan")

    def review_scene(self, *args, **kwargs):
        raise AssertionError("quality loop off must not review scenes")

    def review_chapter(self, *args, **kwargs):
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


def test_case_bible_reveal_deadlines_are_added_to_contract_information_boundaries():
    contract = {
        "chapter": 1,
        "reader_must_not_know_yet": [],
        "withheld_truth_ids": [],
    }
    case_bible = {
        "truth": [
            {
                "id": "T003",
                "fact": "门禁内侧离开按钮不生成持卡人记录",
                "must_not_reveal_before": "chapter_2",
            },
            {
                "id": "T004",
                "fact": "第一章允许公开的门禁卡号",
                "must_not_reveal_before": "chapter_1",
            },
        ]
    }

    enriched = ChapterGenerationLoop._apply_case_bible_knowledge_boundaries(
        contract, case_bible, chapter_number=1
    )

    assert enriched["withheld_truth_ids"] == ["T003"]
    assert enriched["reader_must_not_know_yet"] == [
        "门禁内侧离开按钮不生成持卡人记录"
    ]
    assert contract["withheld_truth_ids"] == []  # caller-owned legacy contract is not mutated


def test_generation_receives_case_bible_reveal_boundaries_from_legacy_contract(tmp_path):
    manager = StoryLedgerManager(str(tmp_path))
    parameters = {"Quality Loop": "off"}
    manager.initialize(parameters)
    manager.save_case_bible(
        {
            "truth": [
                {
                    "id": "T003",
                    "fact": "门禁内侧离开按钮不生成持卡人记录",
                    "must_not_reveal_before": "第2章",
                }
            ]
        },
        "",
    )
    # Simulate an existing project written before withheld_truth_ids was added.
    manager.save_contract(
        1,
        {"chapter": 1, "reader_must_not_know_yet": []},
        PLAN,
    )
    generation_calls = []

    ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=PassingReviewer(),
    ).run(
        chapter_number=1,
        plan_content=PLAN,
        parameters=parameters,
        lore="世界观",
        generate_scene=lambda **kwargs: generation_calls.append(kwargs) or "正文。",
    )

    assert generation_calls
    assert generation_calls[0]["contract"]["withheld_truth_ids"] == ["T003"]
    assert generation_calls[0]["contract"]["reader_must_not_know_yet"] == [
        "门禁内侧离开按钮不生成持卡人记录"
    ]


# --- 差分放行 ---------------------------------------------------------------
#
# 闸门原本对两类失败一视同仁：有硬伤的稿子和只差零点几分的稿子都会在重试耗尽后
# 中断整轮写作。真实运行里出现过评审自己在 evidence 里逐条写「通过」、
# repair_scope 填 "none"、repair_instructions 留空，却因为平均分 3.0 差
# 严格档门槛 3.2 而把 30 章的写作整个卡停。


def soft_failed_review(stage, average=3.0, pass_average=3.2):
    """只差平均分的评审：无硬失败、无必要维度不及格、无可执行修复项。"""
    return DomainReview(
        stage=stage,
        passed=False,
        scores={dimension: average for dimension in SCORE_DIMENSIONS},
        repair_scope="none",
        repair_instructions=[],
        pass_average=pass_average,
    )


def hard_failed_review(stage, pass_average=3.2):
    return DomainReview(
        stage=stage,
        passed=False,
        scores={dimension: 3.0 for dimension in SCORE_DIMENSIONS},
        hard_failures=[{"code": "CONTINUITY_DUPLICATION", "quote": "证人交出收据"}],
        pass_average=pass_average,
    )


class SoftFailingSceneReviewer(PassingReviewer):
    """场景永远差 0.2 分，且从不给出修复项。"""

    review_factory = staticmethod(soft_failed_review)

    def __init__(self):
        super().__init__()
        self.revisions = 0

    def review_scene(self, scene_content, scene_plan, scene_number, *args, **kwargs):
        return self.review_factory(f"scene_{scene_number}")

    def revise_scene(self, scene_content, *args, **kwargs):
        self.revisions += 1
        return scene_content


def _run_soft_failing_loop(tmp_path, reviewer, max_scene_retries=2):
    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=reviewer,
        max_scene_retries=max_scene_retries,
    )
    return loop.run(
        chapter_number=1,
        plan_content=PLAN,
        parameters={},
        lore="世界观",
        generate_scene=lambda **kwargs: f"第{kwargs['scene_number']}场正文：证据状态已经改变。",
    )


def test_scene_short_of_threshold_is_waived_instead_of_stopping_the_run(tmp_path):
    reviewer = SoftFailingSceneReviewer()

    result = _run_soft_failing_loop(tmp_path, reviewer)

    assert [review.waived for review in result.scene_reviews] == [True, True]
    assert all(review.passed for review in result.scene_reviews)
    assert len(result.gate_waivers) == 2
    assert "差 0.20 分" in result.gate_waivers[0]
    # 原始的 passed=False 判定和放行记录都要留档，便于事后复盘。
    review_dir = tmp_path / "quality" / "legal_suspense_reviews" / "chapter_1"
    assert list(review_dir.glob("scene_1_20*.json"))
    assert list(review_dir.glob("scene_1_waived*.json"))


def test_scene_with_a_hard_failure_still_stops_the_run(tmp_path):
    class HardFailingSceneReviewer(SoftFailingSceneReviewer):
        review_factory = staticmethod(hard_failed_review)

    with pytest.raises(QualityGateError, match="仍未通过质量检查"):
        _run_soft_failing_loop(tmp_path, HardFailingSceneReviewer())


def test_scene_far_below_threshold_is_not_waived(tmp_path):
    class WeakSceneReviewer(SoftFailingSceneReviewer):
        review_factory = staticmethod(
            lambda stage: soft_failed_review(stage, average=2.0)
        )

    with pytest.raises(QualityGateError, match="仍未通过质量检查"):
        _run_soft_failing_loop(tmp_path, WeakSceneReviewer())


def test_scene_stops_retrying_once_feedback_is_empty_and_scores_stall(tmp_path):
    reviewer = SoftFailingSceneReviewer()

    _run_soft_failing_loop(tmp_path, reviewer, max_scene_retries=3)

    # 每个场景只该重修一次：第一次重修后分数没涨、评审又给不出依据，
    # 继续重试只是把同一次调用重复三遍。
    assert reviewer.revisions == 2


def test_waived_chapter_can_still_be_accepted(tmp_path):
    class SoftFailingChapterReviewer(PassingReviewer):
        def review_chapter(self, *args, **kwargs):
            return soft_failed_review("chapter")

        def revise_scene(self, scene_content, *args, **kwargs):
            return scene_content

    result = _run_soft_failing_loop(tmp_path, SoftFailingChapterReviewer())

    assert result.chapter_review.waived is True
    chapter_path = tmp_path / "story" / "content" / "chapters" / "chapter_1.md"
    chapter_path.parent.mkdir(parents=True)
    chapter_path.write_text(result.chapter_content, encoding="utf-8")

    acceptance = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=PassingReviewer(),
    ).accept_result(1, result)
    assert acceptance.committed_revision == 1


# ---------------------------------------------------------------- 待复审出口
class FailingChapterReviewer(PassingReviewer):
    """场景都过，整章不过：闸门在章节级抬手，现场是完整的。"""

    def __init__(self):
        super().__init__()
        self.requested_per_round = []
        self.revise_asks = []

    def review_chapter(self, *args, **kwargs):
        self.requested_per_round.append(list(kwargs.get("repairs_requested") or []))
        review = failed_review("chapter", scope="scene_2")
        review.hard_failures = [
            {
                "code": "AI_TEMPLATE_SATURATION",
                "quote": "证人交出收据。",
                "problem": "全知总结压过了动作。",
                "change": "改写成手上的动作。",
            }
        ]
        return review

    def revise_scene(self, scene_content, *args, **kwargs):
        self.revise_asks.append(kwargs.get("asks"))
        return scene_content + " 已按意见改过。"


def _run_until_blocked(tmp_path, reviewer=None, retries=1):
    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=reviewer or FailingChapterReviewer(),
        max_scene_retries=retries,
    )
    with pytest.raises(QualityGateError) as blocked:
        loop.run(
            chapter_number=1,
            plan_content=PLAN,
            parameters={},
            lore="世界观",
            generate_scene=lambda **kwargs: f"第{kwargs['scene_number']}场正文。",
        )
    return loop, blocked.value


def test_a_blocked_chapter_hands_back_the_verdict_and_the_scene(tmp_path):
    """报错只带一句话时，作者只能回头翻几十份评审记录，所以现场必须一起交出。"""
    _, blocked = _run_until_blocked(tmp_path)

    assert blocked.review["passed"] is False
    assert blocked.review["hard_failures"][0]["code"] == "AI_TEMPLATE_SATURATION"
    assert blocked.review["repair_scope"] == "scene_2"
    # 快照要够重新落地这一章：正文、契约、账本基线、场景规划、三份评审。
    assert len(blocked.snapshot["scenes"]) == 2
    assert blocked.snapshot["contract"]["chapter"] == 1
    assert blocked.snapshot["plan_content"] == PLAN
    assert blocked.snapshot["chapter_review"]["passed"] is False


def test_a_half_written_chapter_reports_the_scene_that_stopped_it(tmp_path):
    """场景级抬手时没有完整章节，只交评审，不交可放行的现场。"""

    class FailingSceneReviewer(PassingReviewer):
        def review_scene(self, *args, **kwargs):
            return failed_review("scene_1")

        def revise_scene(self, scene_content, *args, **kwargs):
            return scene_content

    _, blocked = _run_until_blocked(tmp_path, FailingSceneReviewer())

    assert blocked.review["passed"] is False
    assert blocked.snapshot == {}


def test_revising_a_pending_chapter_carries_only_the_asks_it_was_given(tmp_path):
    """作者划掉的建议不该再进提示词，否则勾选框只是个装饰。"""
    loop, blocked = _run_until_blocked(tmp_path)
    reviewer = loop.reviewer
    reviewer.requested_per_round.clear()
    reviewer.revise_asks.clear()

    with pytest.raises(QualityGateError):
        loop.revise_pending(
            1, blocked.snapshot, {}, ["只改这一条：把收据的时间点写清楚"]
        )

    # 复评知道要核对哪几条，改写也必须拿到同一份——否则模型照样去动别的地方。
    assert reviewer.requested_per_round[0] == ["只改这一条：把收据的时间点写清楚"]
    assert reviewer.revise_asks[0] == ["只改这一条：把收据的时间点写清楚"]
    # 第二轮起仍以新评审自己提出的改动为准。
    if len(reviewer.revise_asks) > 1:
        assert reviewer.revise_asks[1] != ["只改这一条：把收据的时间点写清楚"]


def _publish(tmp_path, loop, result, waive_reason):
    """走生成侧那条落盘路：先写文件再验收，失败时正文不会留在稿件目录里。"""
    from core.generation.helper_fns import publish_chapter_with_acceptance

    path = tmp_path / "story" / "content" / "chapters" / "chapter_1.md"
    return publish_chapter_with_acceptance(
        str(tmp_path),
        1,
        str(path),
        result.chapter_content,
        lambda saved: (
            loop.accept_result(1, result, chapter_path=saved)
            if waive_reason is None
            else loop.accept_waived(1, result, waive_reason, chapter_path=saved)
        ),
    )


def test_revising_a_pending_chapter_publishes_once_it_passes(tmp_path):
    """重修通过后走的是同一条验收路，不是绕过闸门直接落盘。"""
    loop, blocked = _run_until_blocked(tmp_path)

    class NowPassing(PassingReviewer):
        def revise_scene(self, scene_content, *args, **kwargs):
            return scene_content + " 已改。"

    loop.reviewer = NowPassing()
    result = loop.revise_pending(1, blocked.snapshot, {}, ["把收据时间写清楚"])

    assert result.chapter_review.passed
    assert "已改。" in result.chapter_content
    accepted = _publish(tmp_path, loop, result, waive_reason=None)
    assert accepted.committed_revision >= 1


def test_waiving_records_the_authors_reason_and_still_runs_acceptance(tmp_path):
    """人工放行改的是评分判定，验收照跑，理由留在评审记录里。"""
    loop, blocked = _run_until_blocked(tmp_path)
    result = restore_result(blocked.snapshot)
    assert not result.chapter_review.passed

    accepted = _publish(tmp_path, loop, result, waive_reason="开篇节奏我认了，先往下写")

    assert accepted.committed_revision >= 1
    assert result.chapter_review.passed
    assert result.chapter_review.waived
    assert "开篇节奏我认了" in result.chapter_review.reviewer_warning
    saved = list(
        (tmp_path / "quality" / "legal_suspense_reviews" / "chapter_1").glob(
            "chapter_waived_by_author_*.json"
        )
    )
    assert saved, "放行必须留痕，事后查得出这一章是被谁放过去的"
    assert "作者人工放行" in json.loads(saved[0].read_text(encoding="utf-8"))[
        "reviewer_warning"
    ]


def test_regenerating_after_a_contract_change_needs_a_bound_context(tmp_path):
    """契约让位于账本时旧正文整体作废，必须逐场重写。

    直接调内部方法，是因为要走到这一步得先让验收判出「保留账本、改契约」，
    在测试里搭那套前置比这条规则本身还长。规则本身很简单：没绑上下文就抛错，
    绑了就能重生成——复审路径当初正是漏了绑，作者点下去要等到最后一步才失败。
    """
    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path), model="hosted-llm", reviewer=PassingReviewer()
    )
    result = restore_result(
        {"scenes": ["旧的一场。"], "plan_content": PLAN, "contract": {}}
    )
    target = str(tmp_path / "story" / "content" / "chapters" / "chapter_1.md")

    with pytest.raises(QualityGateError, match="缺少完整重生成上下文"):
        loop._regenerate_after_contract_change(1, result, target)  # noqa: SLF001

    loop.bind_generation_context(
        {}, "世界观", lambda **kwargs: f"第{kwargs['scene_number']}场重生成正文。"
    )
    assert loop._regenerate_after_contract_change(1, result, target)  # noqa: SLF001
    assert "重生成正文" in result.chapter_content
    assert "旧的一场。" not in result.chapter_content


# ------------------------------------------------------- 按场分组的章节级重修
SCENE_ONE = "顾阳波把图纸铺开。防空洞顶部的混凝土严重剥落。"
SCENE_TWO = "梁浩清点了装备。"
SCENE_THREE = "七年前未竟的残局，都在这一刻压弯了肩膀。"


def _upgrade(dimension, quote, change="改写这一句。"):
    return {
        "dimension": dimension,
        "quote": quote,
        "missing": "具体动作",
        "change": change,
    }


def test_asks_are_routed_to_the_scene_their_quote_lives_in():
    scenes = [SCENE_ONE, SCENE_TWO, SCENE_THREE]
    asks = [
        "【subtext】原文「七年前未竟的残局，都在这一刻压弯了肩膀。」缺少克制；改为：删去。",
        "【opening_pull】原文「防空洞顶部的混凝土严重剥落。」缺少动势；改为：改成开工动作。",
        "【pacing】原文「梁浩清点了装备。」缺少节奏；改为：补一句对话。",
        "整体再压一压抒情密度。",
    ]

    grouped = ChapterGenerationLoop._asks_by_scene(asks, scenes, fallback=3)

    assert list(grouped) == [1, 2, 3]
    assert grouped[1] == [asks[1]]
    assert grouped[2] == [asks[2]]
    # 找不到出处的条目归给 repair_scope 指的那一场。
    assert grouped[3] == [asks[0], asks[3]]


def test_an_ask_with_no_usable_quote_falls_back_to_the_named_scene():
    scenes = [SCENE_ONE, SCENE_TWO]

    grouped = ChapterGenerationLoop._asks_by_scene(
        ["原文「太短」缺少什么；改为：随便", "没有任何引用的一条"], scenes, fallback=2
    )

    assert grouped == {2: ["原文「太短」缺少什么；改为：随便", "没有任何引用的一条"]}


class ScatteredIssuesReviewer(PassingReviewer):
    """章节级不过，开出的条目散落在第一场和第三场，而位置只写了第三场。"""

    def __init__(self):
        super().__init__()
        self.revisions = []

    def review_chapter(self, *args, **kwargs):
        review = failed_review("chapter", scope="scene_3")
        review.repair_instructions = []
        review.upgrades = [
            _upgrade("subtext", SCENE_THREE),
            _upgrade("opening_pull", SCENE_ONE),
        ]
        return review

    def revise_scene(self, scene_content, *args, **kwargs):
        self.revisions.append((scene_content, list(kwargs.get("asks") or [])))
        return scene_content + " 已改。"


def _repair(tmp_path, reviewer, scenes_text, retries=1):
    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=reviewer,
        max_scene_retries=retries,
    )
    plans = [f"### 场景 {i}：计划" for i in range(1, len(scenes_text) + 1)]
    return loop._repair_chapter(  # noqa: SLF001 - 直接测这一轮的分派行为
        7,
        plans,
        list(scenes_text),
        [passed_review(f"scene_{i}") for i in range(1, len(scenes_text) + 1)],
        reviewer.review_chapter(),
        {},
        {},
        {},
        "上一章结尾。",
    )


def test_one_round_repairs_every_scene_the_review_points_at(tmp_path):
    """位置只写了第三场，但条目落在第一场和第三场，两场都得改到。

    只改 repair_scope 那一场的话，另一场的条目就是发给模型却没有对应正文的要求，
    而提示词里写着「逐条对应，不要遗漏」。
    """
    reviewer = ScatteredIssuesReviewer()

    revised, _, _, _ = _repair(
        tmp_path, reviewer, [SCENE_ONE, SCENE_TWO, SCENE_THREE]
    )

    touched = [content for content, _ in reviewer.revisions]
    assert SCENE_ONE in touched
    assert SCENE_THREE in touched
    # 没有条目指向第二场，就不该动它。
    assert SCENE_TWO not in touched
    assert revised[1] == SCENE_TWO

    # 每一场只收到属于自己的那几条。
    by_scene = {content: asks for content, asks in reviewer.revisions}
    assert all("防空洞顶部" in ask for ask in by_scene[SCENE_ONE])
    assert all("七年前未竟" in ask for ask in by_scene[SCENE_THREE])


def test_scenes_are_repaired_in_order_so_the_next_one_sees_the_new_text(tmp_path):
    """升序逐场：后一场要接前一场改完之后的结尾，倒着改会接到旧文。"""

    class TailWatchingReviewer(ScatteredIssuesReviewer):
        def __init__(self):
            super().__init__()
            self.tails = {}

        def revise_scene(self, scene_content, review, scene_plan,
                         previous_scene_tail, *args, **kwargs):
            self.tails[scene_content] = previous_scene_tail
            return super().revise_scene(
                scene_content, review, scene_plan, previous_scene_tail,
                *args, **kwargs
            )

    reviewer = TailWatchingReviewer()
    _repair(tmp_path, reviewer, [SCENE_ONE, SCENE_TWO, SCENE_THREE])

    assert reviewer.tails[SCENE_ONE] == "上一章结尾。"
    # 第三场看到的是第二场的原文（第二场没被改），而不是任何旧快照。
    assert reviewer.tails[SCENE_THREE].endswith(SCENE_TWO)


def test_a_review_with_nothing_actionable_still_repairs_the_named_scene(tmp_path):
    """评审开不出条目时保留老行为：改位置指的那一场，依据退回大方向。"""

    class VagueReviewer(PassingReviewer):
        def __init__(self):
            super().__init__()
            self.revisions = []

        def review_chapter(self, *args, **kwargs):
            review = failed_review("chapter", scope="scene_2")
            review.repair_instructions = []
            return review

        def revise_scene(self, scene_content, *args, **kwargs):
            self.revisions.append((scene_content, kwargs.get("asks")))
            return scene_content + " 已改。"

    reviewer = VagueReviewer()
    _repair(tmp_path, reviewer, [SCENE_ONE, SCENE_TWO])

    assert [content for content, _ in reviewer.revisions] == [SCENE_TWO]
    assert reviewer.revisions[0][1] == []


# ------------------------------------------------- 评审自己没能出结论
TRUNCATED = '{"scores": {"behavioral_logic": 3}, "hard_failures": [{"code": "X"'


def _outage(stage="chapter"):
    return DomainReviewError(
        f"{stage} 质量检查未能返回有效结果：评审 JSON schema 校验失败："
        "大模型的回复没有写完：JSON 对象缺少结尾",
        stage=stage,
        responses=[TRUNCATED, TRUNCATED],
    )


class ChapterReviewOutage(PassingReviewer):
    """场景都过，整章评审两次都没能给出可用结论。"""

    def review_chapter(self, *args, **kwargs):
        raise _outage("chapter")


def _reviews_named(tmp_path, prefix):
    directory = tmp_path / "quality" / "legal_suspense_reviews" / "chapter_1"
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.name.startswith(prefix))


def test_a_review_outage_keeps_the_chapter_instead_of_throwing_it_away(tmp_path):
    """判不合格与判不出来后果一样：这一章都进不了正式稿。

    既然如此，正文就该和判不合格时一样留住。此前评审失灵会一路抛到最外层，那一
    遍写出来的东西一个字都留不下，整章的调用连同已经跑完的另外两份评审全部白烧。
    """
    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path), model="hosted-llm", reviewer=ChapterReviewOutage()
    )

    with pytest.raises(QualityGateError) as blocked:
        loop.run(
            chapter_number=1,
            plan_content=PLAN,
            parameters={},
            lore="世界观",
            generate_scene=lambda **kwargs: f"第{kwargs['scene_number']}场正文。",
        )

    error = blocked.value
    assert error.verdict_unavailable
    assert "没能给出结论" in str(error)
    # 正文和现场都还在：作者能重跑评审，也能直接放行。
    assert len(error.partial_scenes) == 2
    assert len(error.snapshot["scenes"]) == 2
    assert error.snapshot["contract"]["chapter"] == 1
    # 没有判定就没有问题清单，别伪装成一份评审结论。
    assert error.review == {}


def test_a_review_outage_saves_what_the_model_actually_returned(tmp_path):
    """只记一句错误摘要查不出原因：漏字段、写错形状、根本没写完，处置完全不同。"""
    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path), model="hosted-llm", reviewer=ChapterReviewOutage()
    )

    with pytest.raises(QualityGateError):
        loop.run(
            chapter_number=1,
            plan_content=PLAN,
            parameters={},
            lore="世界观",
            generate_scene=lambda **kwargs: f"第{kwargs['scene_number']}场正文。",
        )

    saved = _reviews_named(tmp_path, "review_unavailable")
    assert len(saved) == 1
    payload = json.loads(saved[0].read_text(encoding="utf-8"))
    assert payload["stage"] == "chapter"
    assert payload["responses"] == [TRUNCATED, TRUNCATED]


def test_a_scene_review_outage_keeps_the_half_written_draft(tmp_path):
    """整章还没写完时交出的只有半稿，不构成可放行的现场。"""

    class SceneReviewOutage(PassingReviewer):
        def review_scene(self, *args, **kwargs):
            raise _outage("scene_1")

    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path), model="hosted-llm", reviewer=SceneReviewOutage()
    )

    with pytest.raises(QualityGateError) as blocked:
        loop.run(
            chapter_number=1,
            plan_content=PLAN,
            parameters={},
            lore="世界观",
            generate_scene=lambda **kwargs: f"第{kwargs['scene_number']}场正文。",
        )

    assert blocked.value.verdict_unavailable
    assert blocked.value.snapshot == {}
    assert _reviews_named(tmp_path, "review_unavailable")


def test_rerunning_the_review_on_a_verdictless_draft_can_publish_it_as_is(tmp_path):
    """评审没出结论的那一稿，重跑一次评审通过就直接收下，不必改一个字。"""
    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path), model="hosted-llm", reviewer=ChapterReviewOutage()
    )
    with pytest.raises(QualityGateError) as blocked:
        loop.run(
            chapter_number=1,
            plan_content=PLAN,
            parameters={},
            lore="世界观",
            generate_scene=lambda **kwargs: f"第{kwargs['scene_number']}场正文。",
        )

    loop.reviewer = PassingReviewer()  # 这一次评审正常回话
    result = loop.revise_pending(1, blocked.value.snapshot, {}, [])

    assert result.chapter_review.passed
    # 一个字都没改：重跑的是判定，不是正文。
    assert result.scenes == blocked.value.snapshot["scenes"]
    assert _reviews_named(tmp_path, "chapter_rerun")


def test_rerunning_the_review_falls_through_to_repair_when_it_fails(tmp_path):
    """重跑之后判不合格，就照新判定接着走定向重修那条路。"""
    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=ChapterReviewOutage(),
        max_scene_retries=1,
    )
    with pytest.raises(QualityGateError) as blocked:
        loop.run(
            chapter_number=1,
            plan_content=PLAN,
            parameters={},
            lore="世界观",
            generate_scene=lambda **kwargs: f"第{kwargs['scene_number']}场正文。",
        )

    reviewer = FailingChapterReviewer()
    loop.reviewer = reviewer
    with pytest.raises(QualityGateError) as again:
        loop.revise_pending(1, blocked.value.snapshot, {}, [])

    assert reviewer.revise_asks  # 重修真的跑了
    assert not again.value.verdict_unavailable
    assert again.value.review["hard_failures"][0]["code"] == "AI_TEMPLATE_SATURATION"


def test_waiving_a_verdictless_draft_records_that_nobody_judged_it(tmp_path):
    """放行没有结论的一稿，等于作者替评审签字，这件事必须留在账本上。"""
    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path), model="hosted-llm", reviewer=ChapterReviewOutage()
    )
    with pytest.raises(QualityGateError) as blocked:
        loop.run(
            chapter_number=1,
            plan_content=PLAN,
            parameters={},
            lore="世界观",
            generate_scene=lambda **kwargs: f"第{kwargs['scene_number']}场正文。",
        )

    result = restore_result(blocked.value.snapshot)
    assert result.chapter_review is None

    accepted = _publish(tmp_path, loop, result, waive_reason="这一稿我自己看过了")

    assert accepted.committed_revision >= 1
    assert result.chapter_review.waived
    assert "质量评审未能给出结论" in result.chapter_review.reviewer_warning
    assert "这一稿我自己看过了" in result.chapter_review.reviewer_warning


# ---------------------------------------------- 单场景多轮重修
class DialogueWatchingReviewer(PassingReviewer):
    """记下每次重修拿到的是哪一段对话。"""

    def __init__(self, fail_rounds=1):
        super().__init__()
        self.fail_rounds = fail_rounds
        self.seen = []
        self.reviews = 0

    def review_scene(self, *args, **kwargs):
        self.reviews += 1
        if self.reviews <= self.fail_rounds:
            review = failed_review("scene_1")
            # 每轮换一条：原样退回同一条会触发「无进展」提前收手。
            review.repair_instructions = [f"第 {self.reviews} 轮发现的问题"]
            return review
        return passed_review("scene_1")

    def revise_scene(self, scene_content, *args, **kwargs):
        dialogue = kwargs.get("dialogue")
        self.seen.append(dialogue)
        if dialogue is not None:
            # 真实实现会往对话里追加两轮，这里照做，好让下一轮看得出是同一段。
            if not dialogue.seeded_for(scene_content):
                dialogue.start("开场依据", scene_content)
            dialogue.messages.append({"role": "user", "content": "改这里"})
            dialogue.messages.append({"role": "assistant", "content": scene_content + " 已改。"})
        return scene_content + " 已改。"


def _run_loop(tmp_path, reviewer, parameters=None, retries=2):
    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=reviewer,
        max_scene_retries=retries,
    )
    return loop.run(
        chapter_number=1,
        plan_content=PLAN,
        parameters=parameters if parameters is not None else {},
        lore="世界观",
        generate_scene=lambda **kwargs: f"第{kwargs['scene_number']}场正文。",
    )


def test_repairing_one_scene_twice_uses_one_conversation(tmp_path):
    """同一场的两轮重修共用一段对话：第二轮时模型手上有它第一轮改了什么。"""
    reviewer = DialogueWatchingReviewer(fail_rounds=2)

    _run_loop(tmp_path, reviewer)

    first, second = reviewer.seen[0], reviewer.seen[1]
    assert first is not None
    assert second is first
    # 第一轮两条（开场依据 + 初稿），加上每轮追加的两条。
    assert len(second.messages) == 6


def test_each_scene_gets_its_own_conversation(tmp_path):
    """一场的对话不能带到下一场：契约相同，正文和问题都不同。"""
    reviewer = DialogueWatchingReviewer(fail_rounds=1)

    _run_loop(tmp_path, reviewer)

    assert len({id(dialogue) for dialogue in reviewer.seen}) == len(reviewer.seen)


def test_scene_dialogue_can_be_switched_off(tmp_path):
    """关掉之后退回一次性提问那条路，不给对话。"""
    reviewer = DialogueWatchingReviewer(fail_rounds=1)

    _run_loop(tmp_path, reviewer, parameters={"Scene Dialogue": "off"})

    assert reviewer.seen and all(dialogue is None for dialogue in reviewer.seen)


def test_a_waiver_names_which_review_was_short_not_the_merged_average():
    """章节级是四份合议，差额只来自其中一份。

    报合议后的平均分会写出「平均 3.59 分、门槛 3.20 分，仍差 0.08 分」这种自相
    矛盾的记录——账本里留下的放行理由必须能查出是哪一份短了。
    """
    from core.generation.chapter_generation_loop import waiver_reason

    merged = DomainReview(
        stage="chapter",
        passed=False,
        scores={"contract.a": 4.0, "reader_blind.b": 3.125},
        pass_average=3.2,
        component_reviews={
            "contract": {"passed": True, "average_score": 4.0, "pass_average": 3.2},
            "reader_blind": {
                "passed": False,
                "average_score": 3.125,
                "pass_average": 3.2,
            },
        },
    )
    reason = waiver_reason(merged, "第 24 章章节级检查")

    assert reason is not None
    assert "reader_blind" in reason
    assert "3.12 分" in reason and "门槛 3.20 分" in reason
    # 合议后的平均分（3.56）不该出现，它比门槛还高，写进去只会让人看不懂。
    assert "平均 3.56" not in reason


def test_a_single_review_waiver_still_reports_its_own_numbers():
    from core.generation.chapter_generation_loop import waiver_reason

    single = DomainReview(
        stage="chapter",
        passed=False,
        scores={dimension: 3.1 for dimension in SCORE_DIMENSIONS},
        pass_average=3.2,
    )
    reason = waiver_reason(single, "第 3 章章节级检查")
    assert reason is not None
    assert "平均 3.10 分，门槛 3.20 分" in reason


def test_cancellation_stops_before_another_chapter_repair_round(tmp_path):
    """按下停止之后，不该再启动一轮章节级重修。

    评审本身没有检查点（半路停下会白扔一整章），但一轮重修要再花约十次调用，
    而这一章尚未验收——停在这里丢掉的东西和「下一场之前」停下是同一类。
    """

    class FailingChapterReviewer(PassingReviewer):
        def __init__(self, token):
            super().__init__()
            self.token = token
            self.chapter_reviews = 0

        def review_chapter(self, *args, **kwargs):
            self.chapter_reviews += 1
            # 第一轮合议出结果的同时，作者按下了停止。
            self.token.cancel()
            return failed_review("chapter")

        def revise_scene(self, *args, **kwargs):
            raise AssertionError("取消之后不该再重修任何一场")

    token = CancelToken()
    reviewer = FailingChapterReviewer(token)
    loop = ChapterGenerationLoop(
        output_dir=str(tmp_path),
        model="hosted-llm",
        reviewer=reviewer,
        cancel_token=token,
    )

    with pytest.raises(GenerationCancelled):
        loop.run(
            chapter_number=1,
            plan_content=PLAN,
            parameters={"Genre": "Mystery", "Subgenre": "Legal Thriller"},
            lore="世界观",
            generate_scene=lambda **kwargs: f"第{kwargs['scene_number']}场正文。",
        )

    # 那一轮评审自己走完了，重修一轮都没有开始，账本也没有提交。
    assert reviewer.chapter_reviews == 1
    ledger = json.loads(
        (tmp_path / "system" / "story_ledgers" / "suspense_ledger.json").read_text(
            encoding="utf-8"
        )
    )
    assert ledger["accepted_chapters"] == []
