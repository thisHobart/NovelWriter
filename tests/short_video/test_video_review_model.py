"""Independent review configuration, one revision, and durable final feedback."""

from copy import deepcopy
import json
import re

import httpx
import pytest
from openai import APIConnectionError, AsyncOpenAI

from core.common import api_providers, api_settings
from core.short_video.v2 import creation, llm, models, runtime, service_settings, workflow
from core.short_video.v2.models import WorkspaceError, encode
from core.short_video.v2.project_service import ProjectService
from core.short_video.v2.repository import ProjectRepository
from test_video_v2_repository import ARTICLE, novel


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(api_settings, "settings_file", lambda: tmp_path / "llm_settings.json")
    api_settings.save_provider("hosted-llm", "https://generate.test/v1", "generation-key", "generator")
    service_settings.save_review("hosted-llm", "https://review.test/v1", "review-key", "reviewer")
    repository = ProjectRepository(tmp_path / "project")
    ProjectService(repository).import_text(ARTICLE, expected_revision=0)
    novel(repository)
    return repository


def issue(explanation="动作节奏需要调整"):
    return {"rule_id": "TEXT-001", "severity": "warning", "target_path": "opening.text",
            "explanation": explanation, "suggestion": "保留核心信息，简化开头", "quote": None, "source_refs": []}


def source_issue(repository):
    source = repository.current("source")
    quote = "井口响了三声。"
    start = source["content"]["normalized_text"].index(quote)
    return {**issue(), "quote": quote, "source_refs": [
        {"source_id": source["id"], "start": start, "end": start + len(quote)}]}


def test_review_binds_quote_to_actual_source_without_duplicate_quote_field(configured):
    from core.short_video.v2.review import parse_review

    problem = source_issue(configured)
    result = parse_review(encode({"issues": [problem]}), configured.current("novel_copy"), configured)
    assert result[0]["quote"] == problem["quote"]
    assert result[0]["source_refs"] == problem["source_refs"]


def test_review_binds_miscounted_and_retyped_quotes_by_searching_the_source(tmp_path):
    """The reviewer only copies the sentence; positions and quote marks are settled by search."""
    from core.short_video.v2.review import parse_review

    repository = ProjectRepository(tmp_path / "quoted")
    ProjectService(repository).import_text(
        "雨夜😀巷口。\r\n站在原地的那道“本体”化作白猫，“少年”把它抱进怀里。\r\n白猫走了。白猫走了。",
        expected_revision=0,
    )
    novel(repository)
    source = repository.current("source")
    text = source["content"]["normalized_text"]
    sentence = "站在原地的那道“本体”化作白猫，“少年”把它抱进怀里。"
    start = text.index(sentence)
    candidate = repository.current("novel_copy")

    def ref(begin, end):
        return {"source_id": source["id"], "start": begin, "end": end}

    # As in a real reply: candidate text in quote, the source sentence retyped with ASCII
    # quotes inside a reference whose coordinates were miscounted.
    retyped = {**issue(), "quote": candidate["content"]["opening"]["text"], "source_refs": [
        {**ref(start + 9, start + 40), "quote": sentence.replace("“", "'").replace("”", "'")}]}
    across_line = {**issue(), "quote": "巷口。站在原地", "source_refs": []}
    repeated = {**issue(), "quote": "白猫走了。", "source_refs": [ref(len(text) - 3, len(text))]}
    absent = {**issue(), "quote": "原文里没有这句", "source_refs": [ref(0, 2)]}
    result = parse_review(
        encode({"issues": [retyped, across_line, repeated, absent]}), candidate, repository)
    line_start, line_end = text.index("巷口。"), text.index("站在原地") + 4
    assert [(r["quote"], r["source_refs"]) for r in result] == [
        (sentence, [ref(start, start + len(sentence))]),
        (text[line_start:line_end], [ref(line_start, line_end)]),
        ("白猫走了。", [ref(text.rindex("白猫走了。"), len(text))]),
        (None, []),
    ]
    assert "\n" in result[1]["quote"]
    assert all(r["explanation"] == issue()["explanation"] for r in result)


def test_review_configuration_is_independent_and_never_falls_back(configured, monkeypatch):
    original = api_providers.get_config("hosted-llm")
    review = service_settings.get_review_config()
    assert (review.base_url, review.model, review.api_key) == ("https://review.test/v1", "reviewer", "review-key")
    service_settings.save_review("qwen", api_settings.DEFAULT_URLS["qwen"], "qwen-review-key", "reviewer-2")
    assert api_providers.get_config("hosted-llm") == original
    assert service_settings.review_provider() == "qwen"
    assert service_settings.get_review_config().model == "reviewer-2"
    assert service_settings.review_form_values("hosted-llm")["api_key"] == "review-key"
    with pytest.raises(ValueError, match="校验模型服务已变化"):
        service_settings.get_review_config("hosted-llm")
    data = json.loads(api_settings.settings_file().read_text(encoding="utf-8"))
    data.pop("review")
    api_settings.settings_file().write_text(encode(data), encoding="utf-8")
    monkeypatch.setenv("HOSTED_LLM_API_KEY", "environment-generation-key")
    assert service_settings.review_form_values("hosted-llm")["api_key"] == ""
    with pytest.raises(ValueError, match="配置校验模型"):
        service_settings.get_review_config()


@pytest.mark.parametrize("first_pass,final_pass", [(True, True), (False, True), (False, False)])
def test_real_sdk_routes_generation_and_review_and_stops_after_one_revision(configured, monkeypatch, first_pass, final_pass):
    repository = configured
    content = repository.current("novel_copy")["content"]
    calls = []
    generated, reviewed = [], []

    def handler(request):
        body = json.loads(request.content)
        calls.append((request.url.host, body["model"], request.headers["authorization"]))
        if body["model"] == "generator":
            candidate = deepcopy(content)
            candidate["opening"]["text"] += f"第{len(generated) + 1}稿。"
            generated.append(candidate)
            if len(generated) == 2:
                assert "保留核心信息，简化开头" in body["messages"][0]["content"]
            result = candidate
        else:
            assert generated[-1]["opening"]["text"] in body["messages"][0]["content"]
            passed = first_pass if not reviewed else final_pass
            result = {"issues": [] if passed else [issue(f"第{len(reviewed) + 1}次检查仍有问题")]}
            reviewed.append(result)
        return httpx.Response(200, json={
            "id": "mock", "object": "chat.completion", "created": 1, "model": body["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": encode(result)}, "finish_reason": "stop"}],
        })

    def client(**kwargs):
        assert kwargs["max_retries"] == 0
        return AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(api_providers, "AsyncOpenAI", client)
    service = workflow.WorkflowService(repository)
    result = service.execute(service.prepare("novel_copy"))
    expected = [("generate.test", "generator", "Bearer generation-key"), ("review.test", "reviewer", "Bearer review-key")]
    assert calls == expected * (1 if first_pass else 2)
    assert result["retries_used"] == (0 if first_pass else 1)
    assert repository.current("novel_copy")["content"] == generated[-1]
    run = repository.load()["runs"][-1]
    assert run["review_result"]["candidate_id"] == result["version_id"]
    assert run["review_result"]["passed"] == (first_pass or final_pass)
    assert [{k: v for k, v in i.items() if k not in {"id", "disposition"}}
            for i in run["review_result"]["issues"]] == reviewed[-1]["issues"]
    assert run["snapshot"]["review_model_snapshot"]["model"] == "reviewer"
    assert [c["purpose"] for c in run["calls"]] == ["generation", "review"] * len(generated)
    assert [c["model_snapshot"]["model"] for c in run["calls"]] == ["generator", "reviewer"] * len(generated)
    assert "generation-key" not in encode(repository.load()) and "review-key" not in encode(repository.load())


def test_missing_review_configuration_stops_before_generation(configured):
    data = json.loads(api_settings.settings_file().read_text(encoding="utf-8"))
    data.pop("review")
    api_settings.settings_file().write_text(encode(data), encoding="utf-8")
    calls = []
    service = workflow.WorkflowService(configured, transport=lambda p: calls.append(p))
    with pytest.raises(WorkspaceError) as caught:
        service.prepare("novel_copy")
    assert caught.value.code == "REVIEW_CONFIGURATION_MISSING"
    assert not calls and not configured.load()["tasks"]


@pytest.mark.parametrize("change", ["model", "base_url", "provider"])
def test_review_configuration_change_blocks_recovery_until_restored(configured, monkeypatch, change):
    content = configured.current("novel_copy")["content"]
    command = workflow.WorkflowService(configured).prepare("novel_copy")
    monkeypatch.setattr(llm, "NETWORK_MAX_RETRIES", 0)
    replies = iter([encode(content), ConnectionError("review connection failed")])

    def send(_):
        value = next(replies)
        if isinstance(value, Exception):
            raise value
        return value

    with pytest.raises(ConnectionError):
        workflow.WorkflowService(configured, transport=send).execute(command)
    assert configured.load()["runs"][-1]["checkpoint"]["step"] == "review"
    values = service_settings.review_form_values("hosted-llm")
    provider = "hosted-llm"
    if change == "provider":
        provider = "qwen"
        values["base_url"] = api_settings.DEFAULT_URLS["qwen"]
    else:
        values[change] = "reviewer-changed" if change == "model" else "https://other.test/v1"
    service_settings.save_review(provider, **values)
    calls = []
    with pytest.raises(WorkspaceError) as caught:
        runtime.ResumeWork(configured, command["request_id"], transport=lambda p: calls.append(p))(lambda *_: None)
    assert caught.value.code == "REVIEW_CONFIGURATION_CHANGED"
    assert calls == []
    service_settings.save_review("hosted-llm", "https://review.test/v1", "new-review-key", "reviewer")
    result = runtime.ResumeWork(configured, command["request_id"], transport=lambda p: calls.append(p) or '{"issues":[]}')(lambda *_: None)
    assert result["review_status"] == "passed"
    assert len(calls) == 1 and calls[0].startswith("自动审阅")


def test_second_review_network_failure_resumes_review_without_third_generation(configured, monkeypatch):
    content = configured.current("novel_copy")["content"]
    command = workflow.WorkflowService(configured).prepare("novel_copy")
    purposes = []
    responses = iter([encode(content), encode({"issues": [issue()]}), encode(content), ConnectionError("second review failed")])
    monkeypatch.setattr(llm, "NETWORK_MAX_RETRIES", 0)

    def request(self, _):
        purposes.append(self.purpose)
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(llm.ScriptLLM, "_request", request)
    with pytest.raises(ConnectionError):
        workflow.WorkflowService(configured).execute(command)
    assert purposes == ["generation", "review", "generation", "review"]
    assert configured.load()["runs"][-1]["checkpoint"]["revision"] == 1
    monkeypatch.setattr(llm.ScriptLLM, "_request", lambda self, _: purposes.append(self.purpose) or encode({"issues": [issue()]}))
    result = runtime.ResumeWork(configured, command["request_id"])(lambda *_: None)
    assert result["review_status"] == "issues" and result["retries_used"] == 1
    assert purposes == ["generation", "review", "generation", "review", "review"]
    assert configured.load()["runs"][-1]["review_result"]["issue_count"] == 1


def test_storyboard_composition_freezes_same_review_model(configured):
    # A catalog+storyboard parent and both children must bind the same reviewer.
    configured.save("video_copy", {"blocks": [
        {"id": "b1", "role": "opening", "narration": "井口有声音", "visible_action": "靠近井口", "dialogue": []},
        {"id": "b2", "role": "cliffhanger", "narration": "是谁？", "visible_action": "", "dialogue": []},
    ]}, {"novel_copy": configured.current("novel_copy")["id"]}, expected_revision=configured.load()["revision"])
    command = creation.prepare_storyboard_task(configured, "hosted-llm")
    assert command["review_model_snapshot"] == command["first_command"]["review_model_snapshot"]
    assert command["review_model_snapshot"] == models.capture_review_model_snapshot()


def test_quality_revision_limit_cannot_exceed_one(configured):
    with pytest.raises(WorkspaceError, match="质量修订次数"):
        workflow.WorkflowService(configured, max_retries=2)


def sdk_replies(monkeypatch, replies):
    """Use the actual SDK routing while keeping every HTTP request in-process."""
    calls = []
    sequence = iter(replies)
    monkeypatch.setattr(llm, "NETWORK_MAX_RETRIES", 0)

    def handler(request):
        body = json.loads(request.content)
        model, value = next(sequence)
        assert body["model"] == model
        provider = "generate" if model == "generator" else "review"
        assert request.url.host == provider + ".test"
        assert request.headers["authorization"] == "Bearer " + (
            "generation-key" if model == "generator" else "review-key")
        calls.append(body["messages"][0]["content"])
        if isinstance(value, Exception):
            raise value
        return httpx.Response(200, json={
            "id": "mock", "object": "chat.completion", "created": 1, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content":
                value if isinstance(value, str) else encode(value)}, "finish_reason": "stop"}],
        })

    def client(**kwargs):
        return AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(api_providers, "AsyncOpenAI", client)
    return calls


def test_sdk_accepts_coordinate_only_source_evidence_without_format_repair(configured, monkeypatch):
    problem = source_issue(configured)
    content = configured.current("novel_copy")["content"]
    calls = sdk_replies(monkeypatch, [("generator", content), ("reviewer", {"issues": [problem]})])
    service = workflow.WorkflowService(configured, max_retries=0)
    result = service.execute(service.prepare("novel_copy"))
    assert len(calls) == 2 and result["review_status"] == "issues"
    assert "原文位置由程序查找" in calls[1] and "Unicode码点" not in calls[1]
    assert configured.load()["runs"][-1]["review_result"]["issues"][0]["quote"] == problem["quote"]


def test_unfound_review_quote_keeps_the_issue_without_format_repair(configured, monkeypatch):
    content = configured.current("novel_copy")["content"]
    problem = {**source_issue(configured), "quote": content["opening"]["text"]}
    calls = sdk_replies(monkeypatch, [("generator", content), ("reviewer", {"issues": [problem]})])
    service = workflow.WorkflowService(configured, max_retries=0)
    result = service.execute(service.prepare("novel_copy"))
    assert len(calls) == 2 and result["review_status"] == "issues"
    stored = configured.load()["runs"][-1]["review_result"]["issues"][0]
    assert (stored["quote"], stored["source_refs"]) == (None, [])
    assert stored["explanation"] == problem["explanation"]


@pytest.mark.parametrize("failure", ["generation", "review"])
def test_json_syntax_feedback_reaches_correct_api_and_retains_content(configured, monkeypatch, failure):
    quoted = 'reference 字段为"首帧图片"'
    content = deepcopy(configured.current("novel_copy")["content"])
    content["opening"]["text"] = quoted
    problem = issue(quoted)
    reviewed = {"issues": [problem]}
    valid = encode(content if failure == "generation" else reviewed)
    invalid = valid.replace(r'\"首帧图片\"', '"首帧图片"')
    assert invalid != valid
    replies = ([("generator", invalid), ("generator", content), ("reviewer", reviewed)]
               if failure == "generation" else
               [("generator", content), ("reviewer", invalid), ("reviewer", reviewed)])
    calls = sdk_replies(monkeypatch, replies)
    service = workflow.WorkflowService(configured, max_retries=0)
    result = service.execute(service.prepare("novel_copy"))
    assert len(calls) == 3 and result["review_status"] == "issues"
    if failure == "review":
        repair = json.loads(calls[2].split("【审阅格式修复】\n", 1)[1])
        assert repair["response"] == invalid
        details = json.loads(repair["error"].split("JSON解析详情：\n", 1)[1])
        assert quoted in details["snippet"]
        assert details["line"] > 0 and details["column"] > 0
    else:
        assert "JSON解析详情" in calls[1] and "snippet" in calls[1]
    assert configured.current("novel_copy")["content"]["opening"]["text"] == quoted
    run = configured.load()["runs"][-1]
    assert run["review_result"]["issues"][0]["explanation"] == quoted
    assert [call["format_attempt"] for call in run["calls"]] == ([0, 1, 0] if failure == "generation" else [0, 0, 1])


@pytest.mark.parametrize("repaired", [True, False])
def test_review_format_failure_traces_exact_requests_replies_and_one_feedback(configured, monkeypatch, capsys, repaired):
    monkeypatch.setenv("NOVELWRITER_LLM_TRACE", "1")
    original = configured.current("novel_copy")
    content = deepcopy(original["content"])
    content["opening"]["text"] += "新增候选内容。"
    invalid = {**issue(content["opening"]["text"]), "severity": "error"}
    first = {"issues": [invalid]}
    second = {"issues": [issue()]} if repaired else first
    calls = sdk_replies(monkeypatch, [("generator", content), ("reviewer", first), ("reviewer", second)])
    service = workflow.WorkflowService(configured, max_retries=0)
    command = service.prepare("novel_copy")
    result = service.execute(command)
    assert result["review_status"] == ("issues" if repaired else "unreviewed")
    assert configured.current("novel_copy")["id"] == result["version_id"] != original["id"]
    output = capsys.readouterr().err
    events = [(name, json.loads(header), body) for name, header, body in re.findall(
        r"\[LLM_TRACE (\w+)\] ([^\n]+)\n(.*?)\n\[END LLM_TRACE \1\]", output, re.S)]
    requests = [(header, body) for name, header, body in events if name == "REQUEST"]
    responses = [(header, body) for name, header, body in events if name == "RESPONSE"]
    assert [body for _, body in requests] == calls
    assert [body for _, body in responses] == [encode(content), encode(first), encode(second)]
    assert [header["purpose"] for header, _ in requests] == ["generation", "review", "review"]
    assert [header["review_format_attempt"] for header, _ in requests] == [0, 0, 1]
    assert [header["model"] for header, _ in requests] == ["generator", "reviewer", "reviewer"]
    assert [header["endpoint"] for header, _ in requests] == ["https://generate.test/v1", "https://review.test/v1", "https://review.test/v1"]
    assert [header["call_id"] for header, _ in requests] == [header["call_id"] for header, _ in responses]
    assert len({header["call_id"] for header, _ in requests}) == 3
    assert all(header["finish_reason"] == "stop" for header, _ in responses)
    assert all(header["task_id"] == command["request_id"] for header, _ in requests)
    assert any(name == "PARSED_CANDIDATE" and json.loads(body) == content for name, _, body in events)
    errors = [(header, json.loads(body)) for name, header, body in events if name == "REVIEW_PARSE_ERROR"]
    assert errors[0][0]["call_id"] == requests[1][0]["call_id"]
    assert errors[0][1]["will_repair"] is True
    assert "issues[0].severity" in errors[0][1]["error"]
    repair = json.loads(calls[2].split("【审阅格式修复】\n", 1)[1])
    assert repair["response"] == encode(first)
    message, detail = repair["error"].split("\n审阅校验详情：\n", 1)
    assert message == errors[0][1]["error"]
    assert json.loads(detail) == errors[0][1]["details"]
    assert errors[0][1]["details"]["category"] == "schema"
    assert content["opening"]["text"] in calls[1] and content["opening"]["text"] in calls[2]
    assert any(name == "COMMITTED_RESULT" and json.loads(body)["version_id"] == result["version_id"] for name, _, body in events)
    if repaired:
        assert any(name == "PARSED_REVIEW" for name, _, _ in events)
    else:
        assert len(errors) == 2 and errors[1][1]["will_repair"] is False
        unavailable = [json.loads(body) for name, _, body in events if name == "REVIEW_UNAVAILABLE"]
        assert len(unavailable) == 1 and "issues[0].severity" in unavailable[0]["unavailable"]
        assert all(name != "PARSED_REVIEW" for name, _, _ in events)
    assert "generation-key" not in output and "review-key" not in output


@pytest.mark.parametrize("invalid_review", ["审阅不是JSON", {"issues": "错误列表类型"},
                                            {"issues": [{**issue(), "severity": "error"}]}])
def test_generation_and_review_format_repairs_use_their_own_apis_once(configured, monkeypatch, invalid_review):
    content = configured.current("novel_copy")["content"]
    calls = sdk_replies(monkeypatch, [("generator", "生成不是JSON"), ("generator", content),
                                     ("reviewer", invalid_review), ("reviewer", {"issues": []})])
    service = workflow.WorkflowService(configured)
    result = service.execute(service.prepare("novel_copy"))
    assert result["review_status"] == "passed" and result["retries_used"] == 0
    assert len(calls) == 4 and "生成不是JSON" in calls[1]
    assert "【审阅格式修复】" in calls[3] and "issues" in calls[3]
    repair = json.loads(calls[3].split("【审阅格式修复】\n", 1)[1])
    assert repair["response"] == (invalid_review if isinstance(invalid_review, str) else encode(invalid_review))
    assert repair["error"]
    run = configured.load()["runs"][-1]
    assert [c["purpose"] for c in run["calls"]] == ["generation", "generation", "review", "review"]
    assert [c["format_attempt"] for c in run["calls"]] == [0, 1, 0, 1]
    assert len({c["step_id"] for c in run["calls"]}) == 4


def test_invalid_generation_format_stops_after_one_feedback_repair_and_keeps_original(configured, monkeypatch):
    original = configured.current("novel_copy")
    replies = [("generator", "坏格式1"), ("generator", "坏格式2")]
    calls = sdk_replies(monkeypatch, replies)
    service = workflow.WorkflowService(configured)
    command = service.prepare("novel_copy")
    with pytest.raises(WorkspaceError, match="未取得可用稿件"):
        service.execute(command)
    assert len(calls) == len(replies)
    assert configured.current("novel_copy")["id"] == original["id"]
    task = configured.load()["tasks"][command["request_id"]]
    assert task["status"] == "failed" and not task["resumable"]
    assert runtime.get_recovery(configured, "novel_copy") is None


def test_review_that_never_answers_in_format_keeps_new_draft_unchecked(configured, monkeypatch):
    from core.short_video.v2.feedback import UNREVIEWED_SUMMARY, record_message

    content = deepcopy(configured.current("novel_copy")["content"])
    content["opening"]["text"] = "新的开头。"
    calls = sdk_replies(monkeypatch, [("generator", content), ("reviewer", "坏格式1"), ("reviewer", "坏格式2")])
    service = workflow.WorkflowService(configured)
    command = service.prepare("novel_copy")
    result = service.execute(command)
    # No quality revision is attempted without a review, even though retries remain.
    assert len(calls) == 3 and result["retries_used"] == 0
    assert result["review_status"] == "unreviewed" and result["selected"] is True
    assert result["feedback"]["summary"] == UNREVIEWED_SUMMARY and result["feedback"]["issue_count"] == 0
    current = configured.current("novel_copy")
    assert current["id"] == result["version_id"] and current["content"]["opening"]["text"] == "新的开头。"
    run = configured.load()["runs"][-1]
    assert "回复不是有效 JSON 对象" in run["review_result"]["unavailable"]
    assert record_message(configured, current, configured.load()) == "已保存；" + UNREVIEWED_SUMMARY
    assert configured.load()["tasks"][command["request_id"]]["status"] == "succeeded"


def test_each_quality_review_has_one_separate_format_repair(configured, monkeypatch):
    content = configured.current("novel_copy")["content"]
    calls = sdk_replies(monkeypatch, [
        ("generator", content), ("reviewer", "首稿审阅坏格式"),
        ("reviewer", {"issues": [issue()]}), ("generator", content),
        ("reviewer", "修订稿审阅坏格式"), ("reviewer", {"issues": []}),
    ])
    service = workflow.WorkflowService(configured)
    result = service.execute(service.prepare("novel_copy"))
    assert len(calls) == 6 and result["retries_used"] == 1 and result["review_status"] == "passed"
    assert "首稿审阅坏格式" in calls[2] and "修订稿审阅坏格式" in calls[5]
    assert "首稿审阅坏格式" not in calls[5]
    assert [c["format_attempt"] for c in configured.load()["runs"][-1]["calls"]] == [0, 0, 1, 0, 0, 1]


@pytest.mark.parametrize("repaired", [True, False])
def test_review_repair_network_failure_resumes_without_resetting_attempt(configured, monkeypatch, repaired):
    content = configured.current("novel_copy")["content"]
    calls = sdk_replies(monkeypatch, [
        ("generator", content), ("reviewer", "审阅坏格式"),
        ("reviewer", httpx.ConnectError("review offline")),
    ])
    service = workflow.WorkflowService(configured)
    command = service.prepare("novel_copy")
    with pytest.raises(APIConnectionError, match="Connection error"):
        service.execute(command)
    checkpoint = configured.load()["runs"][-1]["checkpoint"]
    assert len(calls) == 3 and checkpoint["step"] == "review"
    assert checkpoint["review_repair"]["response"] == "审阅坏格式" and checkpoint["response"] is None
    resumed = sdk_replies(monkeypatch, [("reviewer", {"issues": []} if repaired else "仍然坏格式")])
    work = runtime.ResumeWork(configured, command["request_id"])
    assert work(lambda *_: None)["review_status"] == ("passed" if repaired else "unreviewed")
    assert len(resumed) == 1 and "审阅坏格式" in resumed[0] and "【审阅格式修复】" in resumed[0]


@pytest.mark.parametrize("repaired", [True, False])
def test_saved_repair_response_is_replayed_without_another_api_call(configured, monkeypatch, repaired):
    from core.short_video.v2 import review

    raw = encode({"issues": []}) if repaired else "仍然坏格式"
    original_parser = review.parse_review
    calls = sdk_replies(monkeypatch, [("generator", configured.current("novel_copy")["content"]),
                                     ("reviewer", "第一次坏格式"), ("reviewer", raw)])

    def interrupt(response, *args, **kwargs):
        if response == raw:
            raise OSError("interrupt after repair response saved")
        return original_parser(response, *args, **kwargs)

    monkeypatch.setattr(review, "parse_review", interrupt)
    service = workflow.WorkflowService(configured)
    command = service.prepare("novel_copy")
    with pytest.raises(OSError, match="response saved"):
        service.execute(command)
    checkpoint = configured.load()["runs"][-1]["checkpoint"]
    assert checkpoint["response"] == raw and "review_repair" in checkpoint
    monkeypatch.setattr(review, "parse_review", original_parser)
    work = runtime.ResumeWork(configured, command["request_id"])
    assert work(lambda *_: None)["review_status"] == ("passed" if repaired else "unreviewed")
    assert len(calls) == 3
