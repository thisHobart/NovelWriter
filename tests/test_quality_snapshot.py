# -*- coding: utf-8 -*-
"""质量快照：把一次性的临时统计固定成可重跑、可对比的东西。

每一条测试对应一个真实踩过或差点踩中的坑，坑写在各自的 docstring 里。
一律用临时目录：命令行会写快照，指向仓库里的真实项目会改动它们。
"""
from __future__ import annotations

import json

import pytest

from core.evaluation import quality_snapshot as qs
from tools import diagnose_quality


def _component(passed: bool, average: float = 3.5) -> dict:
    return {
        "stage": "chapter",
        "passed": passed,
        "pass_average": 3.2,
        "average_score": average,
        "scores": {"subtext": average},
        "hard_failures": [],
    }


def _merged(components: dict, hard: list | None = None) -> dict:
    """一份带分项的章节合议评审，形状贴着 DomainReview.to_dict()。"""
    return {
        "stage": "chapter",
        "passed": all(child["passed"] for child in components.values()),
        "pass_average": 3.2,
        "average_score": 3.3,
        "hard_failures": hard or [],
        "component_reviews": components,
    }


def _scored(scores: dict, stage: str = "chapter") -> dict:
    """一份合议评审，顶层 scores 用带分项名前缀的形式（reader_blind.subtext）。"""
    components: dict = {}
    for key, value in scores.items():
        name, _, dimension = key.partition(".")
        child = components.setdefault(
            name,
            {
                "stage": name,
                "passed": True,
                "pass_average": 3.2,
                "average_score": value,
                "scores": {},
                "hard_failures": [],
            },
        )
        child["scores"][dimension] = value
        child["passed"] = child["passed"] and value >= 3.0
    return {
        "stage": stage,
        "passed": all(child["passed"] for child in components.values()),
        "pass_average": 3.2,
        "average_score": round(sum(scores.values()) / len(scores), 3),
        "hard_failures": [],
        "scores": dict(scores),
        "component_reviews": components,
    }


def _write_review(root, chapter: int, stage: str, payload, stamp: str = "20260911_120000_000001"):
    directory = root / "quality" / "legal_suspense_reviews" / f"chapter_{chapter}"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stage}_{stamp}.json"
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


# --- 闸门名解析 -------------------------------------------------------------


@pytest.mark.parametrize(
    "stage, gate, retry, marker",
    [
        ("chapter", "chapter", 0, ""),
        ("chapter_retry_1", "chapter", 1, ""),
        ("chapter_retry_2", "chapter", 2, ""),
        ("chapter_waived", "chapter", 0, "waived"),
        ("chapter_rerun", "chapter", 0, "rerun"),
        ("plan", "plan", 0, ""),
        ("plan_retry_1", "plan", 1, ""),
        ("scene_1", "scene_1", 0, ""),
        ("scene_3", "scene_3", 0, ""),
        ("scene_1_retry_2", "scene_1", 2, ""),
        # 复合名：整章重修期间跑的场景评审，属于场景闸门，不属于整章
        ("chapter_retry_1_scene_2", "scene_2", 1, ""),
        ("chapter_retry_2_scene_1", "scene_1", 2, ""),
    ],
)
def test_gate_stage_parsing(stage, gate, retry, marker):
    """坑：按 `_retry_` 切前缀会把 chapter_retry_1_scene_2 算到整章那一栏。"""
    parsed = qs.parse_gate_stage(stage)
    assert parsed is not None
    assert (parsed.gate, parsed.retry, parsed.marker) == (gate, retry, marker)


@pytest.mark.parametrize(
    "stage",
    [
        "acceptance_canon", "acceptance_artifact", "narrative_signals",
        "review_unavailable", "contract_regeneration", "case_bible", "gaps",
    ],
)
def test_free_form_stages_are_not_gates(stage):
    assert qs.parse_gate_stage(stage) is None


# --- 重试与重跑 -------------------------------------------------------------


def test_repeated_bare_stage_is_a_rerun_not_a_retry(tmp_path):
    """坑：整章从头重跑不会在文件名里留 `_retry_`。

    实测 current_work：规划带 retry 的只有 3 章，而同一阶段出现两次的有 12 章。
    只数带 retry 的，会对一个大半章节被重新规划过的项目印出「重试率 5%」。
    """
    _write_review(tmp_path, 1, "plan", _merged({"contract": _component(True)}),
                  "20260911_120000_000001")
    _write_review(tmp_path, 1, "plan", _merged({"contract": _component(True)}),
                  "20260911_130000_000002")

    gates = qs.gate_section(qs.scan_reviews(str(tmp_path)))
    assert gates["plan"]["retried_chapters"] == 0
    assert gates["plan"]["rerun_chapters"] == 1
    assert gates["plan"]["rerun_rate"] == 1.0


def test_retry_and_rerun_are_counted_separately(tmp_path):
    _write_review(tmp_path, 1, "chapter", _merged({"contract": _component(False)}))
    _write_review(tmp_path, 1, "chapter_retry_1", _merged({"contract": _component(True)}),
                  "20260911_121000_000002")
    _write_review(tmp_path, 2, "chapter", _merged({"contract": _component(True)}),
                  "20260911_122000_000003")

    gates = qs.gate_section(qs.scan_reviews(str(tmp_path)))
    assert gates["chapter"]["chapters"] == 2
    assert gates["chapter"]["retried_chapters"] == 1
    assert gates["chapter"]["rerun_chapters"] == 0
    assert gates["chapter"]["max_retry"] == 1


# --- 分项统计 ---------------------------------------------------------------


def test_reviewer_denominators_are_counted_per_reviewer(tmp_path):
    """坑：整章重跑那种评审只带两个分项，共用一个分母会把数字算歪。"""
    _write_review(tmp_path, 1, "chapter", _merged({
        "contract": _component(True),
        "reader_blind": _component(False),
        "plausibility": _component(False),
    }))
    _write_review(tmp_path, 2, "chapter", _merged({
        "contract": _component(True),
        "reader_blind": _component(False),
    }), "20260911_121000_000002")

    reviewers = qs.reviewer_section(qs.scan_reviews(str(tmp_path)))
    assert reviewers["contract"]["reviewed"] == 2
    assert reviewers["reader_blind"]["reviewed"] == 2
    assert reviewers["plausibility"]["reviewed"] == 1
    assert reviewers["reader_blind"]["rate"] == 1.0
    assert reviewers["contract"]["rate"] == 0.0


def test_a_fourth_component_needs_no_code_change(tmp_path):
    """分项不止三个，还有 continuity。不能写死名字。"""
    _write_review(tmp_path, 1, "chapter", _merged({
        "contract": _component(True),
        "continuity": _component(False),
    }))
    reviewers = qs.reviewer_section(qs.scan_reviews(str(tmp_path)))
    assert reviewers["continuity"]["reviewed"] == 1


def test_old_single_reviewer_chapter_reviews_are_not_counted(tmp_path):
    """坑：分项转换器在没有 component_reviews 时会把总评自己退回成一项。

    真正会踩中的不是 acceptance_canon 那类——它们连闸门都不是，早被滤掉了。
    踩中的是**旧格式的整章评审**：stage 就叫 chapter、是正经闸门，但里面没有
    分项。全仓库 400 份 bare chapter 里有 270 份是这种。按 stage 名字判，它们
    会各自变出一个名叫 chapter 的假分项，把未通过率整个推高。
    """
    _write_review(tmp_path, 1, "chapter", _merged({"contract": _component(True)}))
    # 旧格式：有分数、判了通过与否，但没有 component_reviews
    for index in (1, 2):
        _write_review(
            tmp_path, index + 1, "chapter",
            {"stage": "chapter", "passed": False, "pass_average": 3.2,
             "average_score": 2.5, "scores": {"subtext": 2.5}, "hard_failures": []},
            f"20260911_13000{index}_00000{index}",
        )

    corpus = qs.scan_reviews(str(tmp_path))
    # 先确认它们确实被读进来了，否则这条测试会因为文件名没被识别而空跑通过
    assert corpus.file_count == 3
    assert len(corpus.all_reviews()) == 3

    reviewers = qs.reviewer_section(corpus)
    assert set(reviewers) == {"contract"}
    assert "chapter" not in reviewers
    assert len(corpus.merged()) == 1


def test_free_form_reviews_are_filtered_before_components(tmp_path):
    """acceptance_canon 这类连闸门都不是，在更上游就被滤掉。"""
    _write_review(tmp_path, 1, "chapter", _merged({"contract": _component(True)}))
    for index, stage in enumerate(
        ("acceptance_canon", "narrative_signals", "review_unavailable"), start=1
    ):
        _write_review(
            tmp_path, 1, stage,
            {"stage": stage, "passed": True, "issues": []},
            f"20260911_14000{index}_00000{index}",
        )

    corpus = qs.scan_reviews(str(tmp_path))
    assert corpus.file_count == 4
    assert corpus.unreadable == []
    assert len(corpus.all_reviews()) == 1
    assert set(qs.reviewer_section(corpus)) == {"contract"}


# --- 稳健性 -----------------------------------------------------------------


def test_unreadable_review_is_recorded_not_raised(tmp_path):
    _write_review(tmp_path, 1, "chapter", _merged({"contract": _component(True)}))
    _write_review(tmp_path, 1, "chapter", "{", "20260911_130000_000002")

    corpus = qs.scan_reviews(str(tmp_path))
    assert len(corpus.unreadable) == 1
    assert len(corpus.merged()) == 1


def test_a_review_that_is_a_list_is_recorded_not_raised(tmp_path):
    _write_review(tmp_path, 1, "chapter", [1, 2, 3])
    corpus = qs.scan_reviews(str(tmp_path))
    assert corpus.unreadable[0]["error"] == "顶层不是对象"


def test_missing_denominator_never_divides_by_zero(tmp_path):
    """没有章节大纲时不能除零，比率应当是 None 而不是 0。"""
    snapshot = qs.build_snapshot(str(tmp_path))
    assert snapshot["corpus"]["expected_chapters"] is None
    assert snapshot["reviewers"] == {}
    assert qs._rate(0, 0) is None
    assert qs._rate(0, 5) == 0.0


def test_broken_planning_contract_does_not_abort_the_rest(tmp_path):
    """契约读不了时，只标这一区块不可用，评审那块照常出。"""
    _write_review(tmp_path, 1, "chapter", _merged({"contract": _component(True)}))
    contracts = tmp_path / "system" / "story_ledgers" / "chapter_contracts"
    contracts.mkdir(parents=True)
    (contracts / "chapter_1.json").write_text("{", encoding="utf-8")

    snapshot = qs.build_snapshot(str(tmp_path))
    assert snapshot["contract_defects"]["available"] is False
    assert snapshot["contract_defects"]["reason"]
    assert snapshot["reviewers"]["contract"]["reviewed"] == 1


def test_planning_rejection_codes_are_counted(tmp_path):
    retries = tmp_path / "archive" / "planning_retries" / "chapter_4"
    retries.mkdir(parents=True)
    (retries / "attempt_1_defects.json").write_text(
        json.dumps([{"code": "world_conflict", "message": "x"},
                    {"code": "world_conflict", "message": "y"}], ensure_ascii=False),
        encoding="utf-8",
    )
    (retries / "attempt_2_defects.json").write_text(
        json.dumps([{"code": "thread_action_before_open"}], ensure_ascii=False),
        encoding="utf-8",
    )

    section = qs.planning_rejection_section(str(tmp_path))
    assert section["available"] is True
    assert section["attempts"] == 2
    assert section["chapters"] == 1
    assert section["codes"]["world_conflict"] == 2


def test_cost_is_absent_without_logs(tmp_path):
    section = qs.cost_section(str(tmp_path))
    assert section["available"] is False
    assert "opt-in" in section["reason"]


def test_cost_activity_names_are_normalized(tmp_path):
    run = tmp_path / "system" / "quality_runs" / "r1"
    run.mkdir(parents=True)
    rows = [
        {"stage": "chapter_7_write", "prompt_chars": 10, "elapsed_ms": 1, "success": True},
        {"stage": "chapter_9_write", "prompt_chars": 10, "elapsed_ms": 1, "success": True},
        {"stage": "structure_full_attempt_2", "prompt_chars": 5, "elapsed_ms": 1,
         "success": False},
        {"stage": "", "prompt_chars": 1, "elapsed_ms": 1, "success": True},
    ]
    (run / "llm_calls.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\nnot json\n",
        encoding="utf-8",
    )

    section = qs.cost_section(str(tmp_path))
    assert section["by_activity"]["write_review_accept"]["calls"] == 2
    assert "write" not in section["by_activity"]
    assert "structure_full" in section["by_activity"]
    assert section["by_activity"]["未标注"]["calls"] == 1
    assert section["failed_calls"] == 1
    assert section["unparsable_lines"] == 1
    # 后端不返回真实用量，这里绝不能凭空出一个金额
    assert section["amount_usd"] is None


# --- 快照与对比 -------------------------------------------------------------


def _project_with_one_chapter(tmp_path, passed: bool = True):
    _write_review(tmp_path, 1, "chapter", _merged({
        "contract": _component(passed),
        "reader_blind": _component(passed),
    }))
    return tmp_path


def test_snapshot_and_latest_are_written(tmp_path):
    _project_with_one_chapter(tmp_path)
    snapshot = qs.build_snapshot(str(tmp_path))
    path, note = qs.write_snapshot(str(tmp_path), snapshot)

    assert note == ""
    latest = tmp_path / "quality" / "reports" / "quality_snapshot_latest.json"
    assert latest.is_file()
    assert json.loads(latest.read_text(encoding="utf-8")) == snapshot


def test_previous_is_read_before_latest_is_written(tmp_path):
    """坑：先写 latest 再读基线，等于每次拿自己跟自己比，永远显示零变化。"""
    _project_with_one_chapter(tmp_path, passed=True)
    assert diagnose_quality.main(str(tmp_path)) in (0, 1)

    # 第二轮：同一章改判为未通过
    _write_review(tmp_path, 1, "chapter", _merged({
        "contract": _component(False),
        "reader_blind": _component(False),
    }), "20260911_140000_000002")

    previous, reason = qs.read_previous(str(tmp_path))
    assert reason == ""
    current = qs.build_snapshot(str(tmp_path))
    delta = qs.diff_snapshots(previous, current)
    assert delta["reviewers.contract.rate"]["before"] == 0.0
    assert delta["reviewers.contract.rate"]["after"] > 0


def test_first_run_has_no_baseline_and_says_why(tmp_path):
    previous, reason = qs.read_previous(str(tmp_path))
    assert previous is None
    assert "首次运行" in reason


def test_a_baseline_from_another_project_is_refused(tmp_path):
    _project_with_one_chapter(tmp_path)
    snapshot = qs.build_snapshot(str(tmp_path))
    snapshot["project_key"] = "另一个项目"
    reports = tmp_path / "quality" / "reports"
    reports.mkdir(parents=True)
    (reports / "quality_snapshot_latest.json").write_text(
        json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
    )

    previous, reason = qs.read_previous(str(tmp_path))
    assert previous is None
    assert "另一个项目" in reason


def test_an_old_schema_baseline_is_refused(tmp_path):
    reports = tmp_path / "quality" / "reports"
    reports.mkdir(parents=True)
    (reports / "quality_snapshot_latest.json").write_text(
        json.dumps({"schema": "quality_snapshot_v0"}), encoding="utf-8"
    )
    previous, reason = qs.read_previous(str(tmp_path))
    assert previous is None
    assert "旧格式" in reason


def test_diff_polarity(tmp_path):
    before = {"reviewers": {"a": {"rate": 0.8, "reviewed": 10}},
              "hard_failures": {"total": 10},
              "corpus": {"merged_chapter_reviews": 10}}
    after = {"reviewers": {"a": {"rate": 0.6, "reviewed": 10}},
             "hard_failures": {"total": 14},
             "corpus": {"merged_chapter_reviews": 10}}

    delta = qs.diff_snapshots(before, after)
    assert delta["reviewers.a.rate"]["direction"] == "better"
    assert delta["hard_failures.total"]["direction"] == "worse"


def test_diff_marks_incomparable_when_the_sample_moves(tmp_path):
    """分母变了带来的比率变化不是质量信号，只给数字、不下判断。"""
    before = {"reviewers": {"a": {"rate": 0.8, "reviewed": 10}},
              "corpus": {"merged_chapter_reviews": 10}}
    after = {"reviewers": {"a": {"rate": 0.4, "reviewed": 3}},
             "corpus": {"merged_chapter_reviews": 3}}

    delta = qs.diff_snapshots(before, after)
    assert delta["reviewers.a.rate"]["direction"] == "incomparable"


def test_count_metrics_also_respect_the_sample_guard(tmp_path):
    """少跑了几章，硬伤当然变少，那不叫好转。"""
    before = {"hard_failures": {"total": 40}, "corpus": {"merged_chapter_reviews": 30}}
    after = {"hard_failures": {"total": 10}, "corpus": {"merged_chapter_reviews": 8}}

    delta = qs.diff_snapshots(before, after)
    assert delta["hard_failures.total"]["direction"] == "incomparable"


# --- 结论 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "snapshot, code, exit_code",
    [
        ({"corpus": {"merged_chapter_reviews": 0}}, "no_data", 2),
        ({"corpus": {"merged_chapter_reviews": 5},
          "reviewers": {"reader_blind": {"rate": 0.63}}}, "reviewer_bottleneck", 1),
        ({"corpus": {"merged_chapter_reviews": 5},
          "reviewers": {"a": {"rate": 0.1}},
          "gates": {"plan": {"rerun_rate": 0.8, "retry_rate": 0.0}}},
         "rerun_dominant", 1),
        ({"corpus": {"merged_chapter_reviews": 5},
          "reviewers": {"a": {"rate": 0.1}},
          "gates": {"chapter": {"rerun_rate": 0.1, "retry_rate": 0.9}}},
         "gate_retry_dominant", 1),
        ({"corpus": {"merged_chapter_reviews": 5},
          "reviewers": {"a": {"rate": 0.1}},
          "hard_failures": {"top_share": 0.5, "codes": {"X": 5}}},
         "hard_failure_concentrated", 1),
        ({"corpus": {"merged_chapter_reviews": 5},
          "reviewers": {"a": {"rate": 0.1}},
          "planning_rejections": {"available": True, "attempts": 3}},
         "planning_contract_rejections", 1),
        ({"corpus": {"merged_chapter_reviews": 5},
          "reviewers": {"a": {"rate": 0.1}}}, "clean", 0),
    ],
)
def test_verdict_rules(snapshot, code, exit_code):
    verdict = qs.decide_verdict(snapshot)
    assert (verdict["code"], verdict["exit"]) == (code, exit_code)


def test_verdict_carries_identifiers_not_chinese(tmp_path):
    """中文标签不能进快照：改个措辞就会让历史对比对不上。"""
    verdict = qs.decide_verdict({
        "corpus": {"merged_chapter_reviews": 5},
        "reviewers": {"reader_blind": {"rate": 0.63}},
    })
    assert verdict["subject"] == "reader_blind"
    assert verdict["value"] == 0.63


# --- 首稿维度分 -------------------------------------------------------------


def test_a_waived_or_rerun_review_is_not_the_first_draft(tmp_path):
    """坑：waived 与 rerun 也是 gate=="chapter"、retry==0，按那两个条件会被当成首稿。

    waived 是重修之后作者放行的那一稿，rerun 是对同一份旧正文补跑的评审，两者都在
    首稿之后。实测把它们算进来，九章首稿的潜台词均分从 2.000 变成 2.111，
    「零方差」这个结论就没了。
    """
    _write_review(tmp_path, 7, "chapter", _scored({"reader_blind.subtext": 2.0}),
                  "20260911_120000_000001")
    _write_review(tmp_path, 7, "chapter_waived", _scored({"reader_blind.subtext": 4.0}),
                  "20260911_120500_000001")
    _write_review(tmp_path, 7, "chapter_rerun", _scored({"reader_blind.subtext": 4.0}),
                  "20260911_121000_000001")

    section = qs.dimension_section(qs.scan_reviews(str(tmp_path)))

    assert section["first_draft"]["reader_blind"]["subtext"]["mean"] == 2.0
    assert section["first_draft"]["reader_blind"]["subtext"]["chapters"] == 1


def test_a_rerun_chapter_pairs_the_last_round_with_its_own_retry(tmp_path):
    """坑：一章跑了两轮，拿第一轮的首稿减第二轮的重修，两个数不是同一份稿子。

    第 18 章就是这个形状：第一轮首稿 3.0，中途一次契约让位触发整章重生成，
    第二轮首稿 2.0。按时间序切段、只看最后一段，首稿才和重修配得上对。

    里面还藏着第二个坑：目录列出来的顺序按不得。文件名是「阶段_时间戳」，阶段名排
    在时间戳前面，于是 chapter_20260911_171937 会排在 chapter_retry_1_20260911_171500
    之前——照文件名顺序走，第二轮的首稿会跟第一轮的重修配成一对。必须按时间戳排。
    """
    _write_review(tmp_path, 18, "chapter", _scored({"reader_blind.subtext": 3.0}),
                  "20260911_171258_000001")
    _write_review(tmp_path, 18, "chapter_retry_1", _scored({"reader_blind.subtext": 4.0}),
                  "20260911_171500_000001")
    _write_review(tmp_path, 18, "chapter", _scored({"reader_blind.subtext": 2.0}),
                  "20260911_171937_000001")
    _write_review(tmp_path, 18, "chapter_retry_1", _scored({"reader_blind.subtext": 3.0}),
                  "20260911_172005_000001")

    section = qs.dimension_section(qs.scan_reviews(str(tmp_path)))

    assert section["first_draft"]["reader_blind"]["subtext"]["mean"] == 2.0
    moved = section["retry_gain"]["reader_blind"]["subtext"]
    assert (moved["before"], moved["after"], moved["gain"]) == (2.0, 3.0, 1.0)


def test_bare_dimension_names_from_different_reviewers_do_not_merge(tmp_path):
    """坑：裸维度名会撞车。

    契约评审有 continuity，读者盲读有 chapter_continuity；契约评审有 chinese_prose，
    读者盲读有 chinese_readability。合到一起算均分就是两回事相加。
    """
    _write_review(tmp_path, 3, "chapter", _scored({
        "contract.continuity": 4.0,
        "reader_blind.chapter_continuity": 2.0,
    }))

    section = qs.dimension_section(qs.scan_reviews(str(tmp_path)))

    assert section["first_draft"]["contract"]["continuity"]["mean"] == 4.0
    assert section["first_draft"]["reader_blind"]["chapter_continuity"]["mean"] == 2.0


def test_old_reviews_without_top_level_scores_still_get_prefixed_keys(tmp_path):
    """老留档没有顶层 scores，只能按分项现拼，拼出来必须是同一套键。"""
    payload = _scored({"reader_blind.subtext": 2.0})
    payload.pop("scores")
    _write_review(tmp_path, 4, "chapter", payload)

    section = qs.dimension_section(qs.scan_reviews(str(tmp_path)))

    assert section["first_draft"]["reader_blind"]["subtext"]["mean"] == 2.0


def test_dimension_denominators_are_counted_per_dimension(tmp_path):
    """各维度的章数不同：盲读各项 9 章，合理性各项 8 章，接上一章只有 3 章。

    共用一个分母，样本只有 3 份的那一项会被当成有几十份撑着。
    """
    _write_review(tmp_path, 1, "chapter", _scored({
        "reader_blind.subtext": 2.0,
        "reader_blind.chapter_continuity": 3.0,
    }))
    _write_review(tmp_path, 2, "chapter", _scored({"reader_blind.subtext": 2.0}))

    section = qs.dimension_section(qs.scan_reviews(str(tmp_path)))
    first = section["first_draft"]["reader_blind"]

    assert first["subtext"]["chapters"] == 2
    assert first["chapter_continuity"]["chapters"] == 1
    assert qs.DENOMINATORS["dimensions.first_draft.*.*.mean"] == "chapters"


def test_a_project_without_merged_reviews_says_so_instead_of_showing_zero(tmp_path):
    section = qs.dimension_section(qs.scan_reviews(str(tmp_path)))
    assert section["available"] is False
    assert section["reason"]
    assert section["first_draft"] == {}


def test_two_wildcards_expand_because_dimensions_nest_twice():
    """维度要「分项名 + 维度名」两层通配；键里带点的扁平写法会在切路径时碎掉。"""
    snapshot = {"dimensions": {"first_draft": {
        "reader_blind": {"subtext": {"mean": 2.0}},
        "plausibility": {"evidence_handling": {"mean": 2.5}},
    }}}

    paths = qs._expand("dimensions.first_draft.*.*.mean", snapshot)

    assert sorted(paths) == [
        "dimensions.first_draft.plausibility.evidence_handling.mean",
        "dimensions.first_draft.reader_blind.subtext.mean",
    ]


def test_a_score_delta_is_printed_in_points_not_as_a_count():
    note = diagnose_quality._delta_note(
        {"dimensions.first_draft.reader_blind.subtext.mean": {
            "direction": "context", "before": 2.0, "after": 2.5,
            "delta": 0.5, "unit": "score"}},
        "dimensions.first_draft.reader_blind.subtext.mean",
    )

    assert "上次 2.00" in note
    assert "仅供参考" in note
    assert diagnose_quality._scale(
        {"delta": 0.5, "unit": "score"}
    ) == "0.50 分"


def test_dimension_scores_never_take_over_the_verdict_movement():
    """坑：非比率一律按相对变化排大小，2.0 分动 0.5 会永远压过未通过率动 10 点。

    维度指标标成 context_only 就不会进「变化最大的那一项」的筛选，结论行的
    「较上次」不会被它长期占住。
    """
    previous = {
        "reviewers": {"reader_blind": {"rate": 0.64, "reviewed": 10}},
        "dimensions": {"first_draft": {"reader_blind": {
            "subtext": {"mean": 2.0, "chapters": 10}}}},
    }
    current = {
        "reviewers": {"reader_blind": {"rate": 0.54, "reviewed": 10}},
        "dimensions": {"first_draft": {"reader_blind": {
            "subtext": {"mean": 3.0, "chapters": 10}}}},
    }

    delta = qs.diff_snapshots(previous, current)

    path = "dimensions.first_draft.reader_blind.subtext.mean"
    assert delta[path]["unit"] == "score"
    assert delta[path]["direction"] == "context"
    assert qs._largest_movement(delta)["subject"] == "reader_blind"


# --- 排版 -------------------------------------------------------------------


def test_render_returns_lines_and_localizes(capsys):
    snapshot = {
        "project": "x",
        "corpus": {"expected_chapters": 3, "review_files": 9,
                   "merged_chapter_reviews": 3, "unreadable": []},
        "reviewers": {"reader_blind": {"reviewed": 3, "not_passed": 2, "rate": 0.667}},
        "gates": {}, "hard_failures": {},
        "planning_rejections": {"available": False, "reason": "没有存档"},
        "contract_defects": {"available": True, "total": 0, "codes": {}},
        "cost": {"available": False, "reason": "没有日志"},
        "verdict": {"code": "reviewer_bottleneck", "exit": 1,
                    "subject": "reader_blind", "value": 0.667, "detail": "未通过"},
    }
    lines = diagnose_quality.render(snapshot, None, [])

    assert isinstance(lines, list)
    assert all(isinstance(line, str) for line in lines)
    assert any("读者盲读" in line for line in lines)
    assert lines[-1].startswith("结论：")
    assert "reader_blind" not in lines[-1]
    assert capsys.readouterr().out == ""


def test_the_coarse_write_stage_is_never_labelled_as_prose_only():
    """`chapter_N_write` 包住的是整个成章流程，评审占大头。

    实测一轮 61 次调用里评审 34 次，正文 27 次。把这一段标成「写正文」，成本报告
    就会说正文吃掉了九成五的调用，而真相是一半以上花在评审上。
    """
    assert qs.parse_trace_stage("chapter_7_write") == (7, "write_review_accept")
    assert qs.parse_trace_stage("chapter_7_write_retry_2") == (7, "write_review_accept")
    assert "write" not in diagnose_quality.ACTIVITY_LABELS
    assert "写正文并评审验收" == diagnose_quality.ACTIVITY_LABELS["write_review_accept"]


def test_gate_line_carries_a_comparison_for_both_rates():
    """闸门那行有两个比率，对比不能只跟着其中一个走。

    箭头式后缀挂在行尾，读的人没法知道它说的是「重修」还是「重跑」；只印一个，
    另一个的变化就无声消失了。
    """
    snapshot = {
        "project": "x",
        "corpus": {"expected_chapters": 3, "review_files": 9,
                   "merged_chapter_reviews": 3, "unreadable": []},
        "reviewers": {}, "hard_failures": {},
        "gates": {"plan": {"chapters": 10, "retry_rate": 0.1, "rerun_rate": 0.5}},
        "planning_rejections": {"available": False, "reason": "没有存档"},
        "contract_defects": {"available": True, "total": 0, "codes": {}},
        "cost": {"available": False, "reason": "没有日志"},
        "verdict": {"code": "clean", "exit": 0},
    }
    delta = {
        "gates.plan.retry_rate": {"direction": "better", "before": 0.3, "after": 0.1,
                                  "delta": -0.2, "unit": "rate"},
        "gates.plan.rerun_rate": {"direction": "worse", "before": 0.4, "after": 0.5,
                                  "delta": 0.1, "unit": "rate"},
    }
    line = next(
        line for line in diagnose_quality.render(snapshot, delta, []) if "场景规划" in line
    )

    assert line.index("重修") < line.index("好转") < line.index("重跑")
    assert "恶化" in line[line.index("重跑"):]


def test_a_count_that_moves_by_one_is_not_a_hundred_point_swing():
    """靠数值大小猜单位，次数变动 1 会被印成「恶化 100 点」。

    实测硬失败从 61 次变成 62 次，报告与结论句都印了「恶化 100 点」。单位跟着指标
    表走，不由数值大小推。
    """
    note = diagnose_quality._delta_note(
        {"hard_failures.total": {"direction": "worse", "before": 61, "after": 62,
                                 "delta": 1, "unit": "count"}},
        "hard_failures.total",
    )

    assert "100" not in note
    assert "1 次" in note and "上次 61" in note


def test_a_change_too_small_to_show_says_so_instead_of_zero():
    """未通过率动了不到半个点，印成「好转 0 点」是句废话。"""
    note = diagnose_quality._delta_note(
        {"reviewers.contract.rate": {"direction": "better", "before": 0.0769,
                                     "after": 0.0732, "delta": -0.0037,
                                     "unit": "rate"}},
        "reviewers.contract.rate",
    )

    assert "0 点" not in note
    assert "几乎持平" in note


def test_units_come_from_the_metric_table_not_from_magnitude():
    previous = {"reviewers": {"a": {"rate": 0.5, "reviewed": 10}},
                "hard_failures": {"total": 61}}
    current = {"reviewers": {"a": {"rate": 0.4, "reviewed": 10}},
               "hard_failures": {"total": 62}}

    delta = qs.diff_snapshots(previous, current)

    assert delta["reviewers.a.rate"]["unit"] == "rate"
    assert delta["hard_failures.total"]["unit"] == "count"


def test_the_largest_movement_is_not_always_whichever_metric_counts_things():
    """比绝对值等于让次数永远胜出：+1 次的绝对值压过动了 10 个点的比率。"""
    delta = {
        "reviewers.reader_blind.rate": {"direction": "better", "before": 0.64,
                                        "after": 0.54, "delta": -0.10,
                                        "unit": "rate"},
        "hard_failures.total": {"direction": "worse", "before": 61, "after": 62,
                                "delta": 1, "unit": "count"},
    }

    assert qs._largest_movement(delta)["subject"] == "reader_blind"


def test_missing_directory_returns_two(tmp_path, capsys):
    assert diagnose_quality.main(str(tmp_path / "没有这个目录")) == 2
    assert "找不到目录" in capsys.readouterr().out


def test_an_empty_project_is_not_reported_as_clean(tmp_path, capsys):
    """空项目返回 0 会和干净项目分不出来，那正是这个工具要防的事。"""
    assert diagnose_quality.main(str(tmp_path)) == 2
    assert "无法诊断" in capsys.readouterr().out
