"""Review wire schemas, structured feedback and provenance failures."""

from copy import deepcopy
import json

from jsonschema import Draft202012Validator, SchemaError
import pytest

from core.short_video.v2 import models, review, review_contract, workflow
from core.short_video.v2.models import WorkspaceError, encode, format_error_feedback
from test_video_review_model import configured, issue, sdk_replies, source_issue  # noqa: F401

KINDS = [
    "novel_copy",
    "video_copy",
    "asset_catalog",
    "storyboard",
    "character_spec",
    "scene_spec",
    "prop_spec",
    "prompt_bundle",
    "video_prompt",
]


@pytest.mark.parametrize("kind", KINDS)
def test_each_stage_advertises_and_validates_its_own_contract(kind):
    contract = review_contract.output_contract(kind)
    Draft202012Validator.check_schema(contract["schema"])
    Draft202012Validator(contract["schema"]).validate(contract["example"])
    assert review_contract.parse_report(encode(contract["example"]), kind) == contract["example"]
    assert review_contract.parse_report('{"issues":[]}', kind) == {"issues": []}


def test_stage_override_and_public_schema_mutation_do_not_leak():
    video = review_contract.review_schema("video_prompt")
    normal = review_contract.review_schema("novel_copy")
    assert video["$defs"]["issue"]["properties"]["target_path"]["const"] == "video_motion_prompt"
    assert "const" not in normal["$defs"]["issue"]["properties"]["target_path"]
    video["$defs"]["issue"]["properties"]["explanation"]["type"] = "integer"
    assert (
        review_contract.output_contract("video_prompt")["schema"]["$defs"]["issue"]["properties"][
            "explanation"
        ]["type"]
        == "string"
    )


@pytest.mark.parametrize("invalid", [[], None, 7, "report"])
def test_valid_json_with_wrong_root_is_a_schema_error(invalid):
    assert models.parse_json(encode(invalid)) == invalid
    with pytest.raises(WorkspaceError) as caught:
        review_contract.parse_report(encode(invalid), "novel_copy")
    details = caught.value.details
    assert details["category"] == "schema"
    assert details["errors"][0]["path"] == "$"
    assert details["errors"][0]["expected"] == "object"
    with pytest.raises(WorkspaceError, match="需要 JSON 对象"):
        models.parse_object(encode(invalid))


@pytest.mark.parametrize(
    "raw", ['{"issues":[}', '{"issues":NaN}', '{"issues":Infinity}', "not JSON"]
)
def test_syntax_feedback_keeps_parser_reason_without_guessing_cause(raw):
    with pytest.raises(WorkspaceError) as caught:
        review_contract.parse_report(raw, "novel_copy")
    error = caught.value
    assert error.details["category"] == "syntax" and error.details["json_error"]
    assert "hint" not in error.details
    assert "JSON解析详情" in format_error_feedback(error)
    if isinstance(error.__cause__, json.JSONDecodeError):
        assert error.details["position"] == error.__cause__.pos
        assert error.details["line"] == error.__cause__.lineno


@pytest.mark.parametrize(
    "field,value,rule",
    [
        ("suggestion", None, "type"),
        ("explanation", [], "type"),
        ("rule_id", " \t\n\u3000", "pattern"),
        ("severity", "error", "enum"),
        ("quote", {}, "type"),
        ("source_refs", None, "type"),
    ],
)
def test_schema_error_contains_exact_path_expected_and_actual(field, value, rule):
    problem = {**issue(), field: value}
    with pytest.raises(WorkspaceError) as caught:
        review_contract.parse_report(encode({"issues": [problem]}), "novel_copy")
    details = caught.value.details
    assert details["category"] == "schema"
    error = details["errors"][0]
    assert error["path"] == f"issues[0].{field}" and error["rule"] == rule
    assert error["actual_preview"] == json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    assert error["expected"]


def test_missing_fields_are_distinct_from_null_and_not_reported_repeatedly():
    with pytest.raises(WorkspaceError) as missing:
        review_contract.parse_report('{"issues":[{}]}', "novel_copy")
    errors = missing.value.details["errors"]
    assert len(errors) == 5 and len({e["path"] for e in errors}) == 5
    assert all(e["actual_type"] == "missing" and e["actual_preview"] == "<missing>" for e in errors)
    with pytest.raises(WorkspaceError) as null:
        review_contract.parse_report(
            encode({"issues": [{**issue(), "suggestion": None}]}), "novel_copy"
        )
    assert null.value.details["errors"][0]["actual_type"] == "null"


def test_optional_fields_extensions_and_values_are_not_normalized():
    problem = {
        k: v for k, v in issue(" 原有空格保留 ").items() if k not in {"quote", "source_refs"}
    }
    problem["extension"] = {"number_as_text": "42"}
    value = {"issues": [problem], "extension": True}
    assert review_contract.parse_report(encode(value), "novel_copy") == value


@pytest.mark.parametrize("field,value", [("source_id", 5), ("start", True), ("end", None)])
def test_reference_structure_is_checked_before_provenance(field, value):
    problem = {**issue(), "source_refs": [{"source_id": "s", "start": 0, "end": 1, field: value}]}
    with pytest.raises(WorkspaceError) as caught:
        review_contract.parse_report(encode({"issues": [problem]}), "novel_copy")
    assert caught.value.details["category"] == "schema"
    assert caught.value.details["errors"][0]["path"] == f"issues[0].source_refs[0].{field}"


def test_diagnostics_are_sorted_bounded_and_do_not_repeat_entire_values():
    problems = [{"severity": "X" * 10000} for _ in range(5)]
    errors = []
    for values in (problems, [dict(reversed(list(p.items()))) for p in problems]):
        with pytest.raises(WorkspaceError) as caught:
            review_contract.parse_report(encode({"issues": values}), "novel_copy")
        details = caught.value.details
        assert len(details["errors"]) == 10 and details["omitted_count"] == 15
        assert all(len(error["actual_preview"]) <= 200 for error in details["errors"])
        assert "X" * 201 not in encode(details)
        errors.append(details)
    assert errors[0] == errors[1]


@pytest.mark.parametrize(
    "invalid,path",
    [
        ({}, "issues"),
        ({"issues": {}}, "issues"),
        ({"issues": [None]}, "issues[0]"),
    ],
)
def test_container_and_required_errors_locate_the_bad_field(invalid, path):
    with pytest.raises(WorkspaceError) as caught:
        review_contract.parse_report(encode(invalid), "novel_copy")
    assert caught.value.details["errors"][0]["path"] == path


@pytest.mark.parametrize(
    "variant,outcome",
    [
        ("unknown_source", "bound"),
        ("range", "bound"),
        ("inline_quote", "bound"),
        ("quote", "dropped"),
        ("selection", "dropped"),
        ("coordinates_only", "coordinates"),
        ("coordinates_only_out_of_range", "dropped"),
    ],
)
def test_bad_citations_are_rebound_or_dropped_but_never_fail_the_review(
    configured, variant, outcome
):
    problem = source_issue(configured)
    quote, refs = problem["quote"], deepcopy(problem["source_refs"])
    ref = problem["source_refs"][0]
    if variant == "unknown_source":
        ref["source_id"] = "unknown_source"
    elif variant == "range":
        ref["end"] = 10000
    elif variant == "inline_quote":
        ref["quote"] = "不是原文"
    elif variant == "quote":
        problem["quote"] = "不是原文"
    elif variant == "coordinates_only":
        problem["quote"] = None
    elif variant == "coordinates_only_out_of_range":
        problem["quote"] = None
        ref["end"] = 10000
    else:
        # Restrict the ancestor selection while keeping the source coordinates valid.
        original_record = configured.record

        def selected_record(record_id, **kwargs):
            record = deepcopy(original_record(record_id, **kwargs))
            if record["kind"] == "selection":
                record["content"]["ranges"] = [{"start": 0, "end": 1}]
            return record

        configured.record = selected_record
    [result] = review.parse_review(
        encode({"issues": [problem]}), configured.current("novel_copy"), configured
    )
    assert result["explanation"] == problem["explanation"]
    assert (result["quote"], result["source_refs"]) == {
        "bound": (quote, refs),
        "coordinates": (None, refs),
        "dropped": (None, []),
    }[outcome]


def test_extension_references_outside_the_shown_text_are_dropped(configured):
    problem = source_issue(configured)
    valid = deepcopy(problem["source_refs"][0])
    problem["extension"] = {"nested": [{"source_refs": [
        {**valid, "paragraph_id": "p1"}, {**valid, "source_id": "unknown"}, {**valid, "quote": "不是原文"}]}]}
    problem["evidence"] = {"source_refs": "不是列表"}
    [result] = review.parse_review(
        encode({"issues": [problem]}), configured.current("novel_copy"), configured
    )
    assert result["extension"]["nested"][0]["source_refs"] == [valid]
    assert result["evidence"]["source_refs"] == []


def test_corrupt_expected_source_is_not_a_model_format_error(configured, monkeypatch):
    problem = source_issue(configured)
    source_id = problem["source_refs"][0]["source_id"]
    original = configured.record
    failure = WorkspaceError("INVALID_PROJECT", "原文文件损坏")

    def read(record_id, **kwargs):
        if record_id == source_id:
            raise failure
        return original(record_id, **kwargs)

    monkeypatch.setattr(configured, "record", read)
    with pytest.raises(WorkspaceError) as caught:
        review.parse_review(
            encode({"issues": [problem]}), configured.current("novel_copy"), configured
        )
    assert caught.value is failure and failure.details == {}


@pytest.mark.parametrize(
    "invalid,path",
    [
        ([], "$"),
        ({"issues": [{**issue(), "suggestion": None}]}, "issues[0].suggestion"),
        ({"issues": [{**issue(), "severity": "error"}]}, "issues[0].severity"),
    ],
)
def test_sdk_feedback_carries_schema_diagnostics_and_keeps_judgment(
    configured, monkeypatch, invalid, path
):
    content = configured.current("novel_copy")["content"]
    calls = sdk_replies(
        monkeypatch,
        [("generator", content), ("reviewer", invalid), ("reviewer", {"issues": [issue()]})],
    )
    service = workflow.WorkflowService(configured, max_retries=0)
    result = service.execute(service.prepare("novel_copy"))
    assert result["review_status"] == "issues" and result["retries_used"] == 0
    assert len(calls) == 3
    data = json.loads(calls[1][calls[1].index("\n{") + 1 :])
    assert data["response_contract"] == review_contract.output_contract("novel_copy")
    assert calls[2].count('"$schema"') == 1
    repair = json.loads(calls[2].split("【审阅格式修复】\n", 1)[1])
    assert repair["response"] == encode(invalid)
    details = json.loads(repair["error"].split("审阅校验详情：\n", 1)[1])
    assert details["category"] == "schema" and details["errors"][0]["path"] == path
    assert (
        configured.load()["runs"][-1]["review_result"]["issues"][0]["explanation"]
        == issue()["explanation"]
    )


def test_our_schema_failure_stops_before_review_api_request(configured, monkeypatch):
    service = workflow.WorkflowService(configured)
    command = service.prepare("novel_copy")

    def broken(_):
        raise SchemaError("invalid application schema")

    monkeypatch.setattr(review, "output_contract", broken)
    with pytest.raises(SchemaError):
        review.check_candidate(
            command,
            configured.current("novel_copy"),
            lambda _: pytest.fail("invalid schema was sent"),
            configured,
        )


def test_schema_repair_then_quality_revision_are_separate_budgets(configured, monkeypatch):
    content = configured.current("novel_copy")["content"]
    calls = sdk_replies(
        monkeypatch,
        [
            ("generator", content),
            ("reviewer", {"issues": [{**issue(), "suggestion": None}]}),
            ("reviewer", {"issues": [issue()]}),
            ("generator", content),
            ("reviewer", {"issues": []}),
        ],
    )
    service = workflow.WorkflowService(configured)
    result = service.execute(service.prepare("novel_copy"))
    assert result["review_status"] == "passed" and result["retries_used"] == 1
    assert issue()["suggestion"] in calls[3]
    assert [call["format_attempt"] for call in configured.load()["runs"][-1]["calls"]] == [
        0,
        0,
        1,
        0,
        0,
    ]


@pytest.mark.parametrize("repaired", [True, False])
def test_schema_feedback_survives_network_resume_without_new_allowance(
    configured, monkeypatch, repaired
):
    import httpx
    from openai import APIConnectionError
    from core.short_video.v2.runtime import ResumeWork

    invalid = {"issues": [{**issue(), "suggestion": None}]}
    calls = sdk_replies(
        monkeypatch,
        [
            ("generator", configured.current("novel_copy")["content"]),
            ("reviewer", invalid),
            ("reviewer", httpx.ConnectError("offline")),
        ],
    )
    service = workflow.WorkflowService(configured)
    command = service.prepare("novel_copy")
    with pytest.raises(APIConnectionError):
        service.execute(command)
    checkpoint = configured.load()["runs"][-1]["checkpoint"]
    assert set(checkpoint["review_repair"]) == {"error", "response"}
    assert '"category": "schema"' in checkpoint["review_repair"]["error"]
    resumed = sdk_replies(monkeypatch, [("reviewer", {"issues": []} if repaired else invalid)])
    work = ResumeWork(configured, command["request_id"])
    assert work(lambda *_: None)["review_status"] == ("passed" if repaired else "unreviewed")
    assert len(calls) == 3 and len(resumed) == 1
    repair = json.loads(resumed[0].split("【审阅格式修复】\n", 1)[1])
    assert repair["error"] == checkpoint["review_repair"]["error"]
    assert repair["response"] == encode(invalid)
    assert "issues[0].suggestion" in resumed[0]


@pytest.mark.parametrize("invalid", [[], {"issues": [{**issue(), "suggestion": None}]}])
def test_repeated_schema_failure_keeps_new_draft_unchecked_after_only_one_repair(
    configured, monkeypatch, invalid
):
    original = configured.current("novel_copy")
    calls = sdk_replies(
        monkeypatch,
        [("generator", original["content"]), ("reviewer", invalid), ("reviewer", invalid)],
    )
    service = workflow.WorkflowService(configured)
    result = service.execute(service.prepare("novel_copy"))
    assert len(calls) == 3 and result["review_status"] == "unreviewed"
    assert configured.current("novel_copy")["id"] == result["version_id"]
    assert "结构错误" in configured.load()["runs"][-1]["review_result"]["unavailable"]


def test_unknown_source_is_rebound_without_review_repair(configured, monkeypatch):
    valid = source_issue(configured)
    invalid = deepcopy(valid)
    invalid["source_refs"][0]["source_id"] = "not_a_project_source"
    calls = sdk_replies(
        monkeypatch,
        [
            ("generator", configured.current("novel_copy")["content"]),
            ("reviewer", {"issues": [invalid]}),
        ],
    )
    service = workflow.WorkflowService(configured, max_retries=0)
    assert service.execute(service.prepare("novel_copy"))["review_status"] == "issues"
    assert len(calls) == 2 and "【审阅格式修复】" not in calls[1]
    stored = configured.load()["runs"][-1]["review_result"]["issues"][0]
    assert (stored["quote"], stored["source_refs"]) == (valid["quote"], valid["source_refs"])


def test_non_video_review_budget_includes_contract_before_sending(configured):
    service = workflow.WorkflowService(configured)
    command = service.prepare("novel_copy")
    candidate = deepcopy(configured.current("novel_copy"))
    candidate["content"]["opening"]["text"] = "正文" * 24000
    with pytest.raises(WorkspaceError, match="上下文超限"):
        review.check_candidate(
            command, candidate, lambda _: pytest.fail("over-budget review sent"), configured
        )
