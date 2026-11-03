"""Agent Loop, task budgets and selected-content provenance."""
from copy import deepcopy
import json

import pytest

from core.short_video.v2.creation import CreationWork, prepare_prompt_task, prepare_storyboard_task
from core.short_video.v2.exporter import ExportService
from core.short_video.v2.llm import RequestBudget
from core.short_video.v2.models import ASSET_KINDS, WorkspaceError, encode
from core.short_video.v2.project_service import ProjectService
from core.short_video.v2.workflow import WorkflowService
from core.short_video.v2.review import QualityCheckResult
from core.short_video.v2.conversation import ConversationService
from core.generation.cancellation import CancelToken, GenerationCancelled
from test_video_v2_workflow import project, generate, sample, prepare_story


ISSUE = {"rule_id": "TXT-003", "severity": "warning", "target_path": "opening.text", "explanation": "开头有重复解释", "suggestion": "压缩开头", "quote": None, "source_refs": []}


def sequence_service(repository, responses):
    responses = iter(responses)
    calls = []
    def send(prompt):
        calls.append(prompt)
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, str) else encode(value)
    return WorkflowService(repository, transport=send), calls


def test_generation_review_revision_review_share_four_calls(project):
    original = sample(project, "novel_copy")
    revised = deepcopy(original)
    revised["opening"]["text"] = "铜钥匙入手，井里敲了三声。"
    service, calls = sequence_service(project, [original, {"issues": [ISSUE]}, revised, {"issues": []}])
    result = service.execute(service.prepare("novel_copy"))
    assert len(calls) == 4 and "外部质量检查反馈" in calls[2]
    assert result["review_status"] == "passed"
    assert project.current("novel_copy")["content"] == revised
    assert project.load()["confirmations"] == {}


def test_retry_limit_returns_latest_candidate_even_with_more_feedback(project):
    original = sample(project, "novel_copy")
    revised = deepcopy(original)
    revised["opening"]["text"] = "不够好的另一版"
    extra = {**ISSUE, "explanation": "结尾也有问题", "target_path": "cliffhanger.text"}
    service, calls = sequence_service(project, [original, {"issues": [ISSUE]}, revised, {"issues": [ISSUE, extra]}])
    service.max_retries = 1
    result = service.execute(service.prepare("novel_copy"))
    assert result["review_status"] == "issues" and len(calls) == 4
    assert project.current("novel_copy")["content"] == revised
    assert len(project.versions("novel_copy")) == 2


def test_format_repairs_use_review_budget_and_keep_valid_result(project):
    content = sample(project, "novel_copy")
    service, calls = sequence_service(project, ["broken"] * 5 + [content])
    result = service.execute(service.prepare("novel_copy"))
    assert len(calls) == 6 and result["review_status"] == "incomplete"
    assert project.current("novel_copy")["content"] == content
    assert not project.latest_reports(result["version_id"])


def test_exhausted_deadline_stops_without_quality_retry(project):
    clock = [0]
    budget = RequestBudget(4, seconds=300, clock=lambda: clock[0])
    responses = iter([sample(project, "novel_copy"), {"issues": [ISSUE]}])
    def send(prompt):
        clock[0] += 160
        return encode(next(responses))
    service = WorkflowService(project, transport=send)
    result = service.execute(service.prepare("novel_copy"), budget=budget)
    assert budget.calls == 2 and result["review_status"] == "incomplete"
    assert project.current("novel_copy")


def test_review_failure_keeps_draft_but_programming_error_surfaces(project):
    content = sample(project, "novel_copy")
    service, calls = sequence_service(project, [content, ConnectionError("review-offline")])
    result = service.execute(service.prepare("novel_copy"))
    assert result["review_status"] == "unavailable" and len(calls) == 2
    old = project.current("novel_copy")["id"]
    service, _ = sequence_service(project, [content, TypeError("internal-review-bug")])
    with pytest.raises(TypeError, match="internal-review-bug"):
        service.execute(service.prepare("novel_copy"))
    assert project.current("novel_copy")["id"] == old
    assert project.load()["runs"][-1]["status"] == "failed"


def test_manual_save_during_review_is_not_overwritten(project):
    first = generate(project, "novel_copy")
    calls = []
    def send(prompt):
        calls.append(prompt)
        if prompt.startswith("自动审阅"):
            edited = deepcopy(first["content"])
            edited["opening"]["text"] = "审阅期间的人工稿"
            project.save("novel_copy", edited, first["dependency_refs"], parent_id=first["id"], expected_revision=project.load()["revision"])
            return encode({"issues": []})
        return encode(first["content"])
    service = WorkflowService(project, transport=send)
    result = service.execute(service.prepare("novel_copy"))
    assert not result["selected"]
    assert project.current("novel_copy")["content"]["opening"]["text"] == "审阅期间的人工稿"


def test_explicit_stale_continue_preserves_old_article_and_dependencies(project):
    novel = generate(project, "novel_copy")
    original_article = ProjectService(project).context()["text"]
    ProjectService(project).import_text("全新文章与原文不同。", expected_revision=project.load()["revision"])
    service = WorkflowService(project)
    with pytest.raises(WorkspaceError) as error:
        service.prepare("video_copy")
    assert error.value.code == "STALE_INPUT"
    command = service.prepare("video_copy", use_stale=True)
    assert command["input_refs"]["selection"] == novel["dependency_refs"]["selection"]
    assert command["context"]["article"]["text"] == original_article
    payload = sample(project, "video_copy")
    # The response must cite the frozen old article, not the currently selected source.
    old_refs = command["context"]["article"]["source_refs"]
    for block in payload["blocks"]:
        block["source_refs"] = old_refs
    service.transport = lambda prompt: encode({"issues": []}) if prompt.startswith("自动审阅") else encode(payload)
    result = service.execute(command)
    assert result["selected"]
    assert project.current("video_copy")["dependency_refs"]["selection"] != project.current("selection")["id"]


def task_transport(repository, calls):
    def send(prompt):
        calls.append(prompt)
        if prompt.startswith("自动审阅"):
            return encode({"issues": []})
        for stage in ("asset_catalog", "storyboard", "asset_spec"):
            if "阶段：" + stage in prompt:
                params = {}
                if stage == "asset_spec":
                    context = json.loads(prompt.split("冻结的输入资料：\n", 1)[1].split("\n输出结构样例：", 1)[0])
                    params = {"entity": context["entity"], "kind": ASSET_KINDS[context["entity"]["kind"]]}
                return encode(sample(repository, stage, **params))
        raise AssertionError(prompt)
    return send


def test_storyboard_and_asset_tasks_automate_dependencies_and_record_budget(project):
    generate(project, "novel_copy")
    generate(project, "video_copy")
    calls = []
    work = CreationWork(project, prepare_storyboard_task(project, "hosted-llm"), transport=task_transport(project, calls))
    result = work(lambda *args: None)
    assert result["status"] == "succeeded" and len(calls) == 4
    assert project.current("asset_catalog") and project.current("storyboard")
    command = prepare_prompt_task(project, "hosted-llm")
    assert command["max_calls"] == 18 and command["seconds"] == 900
    work = CreationWork(project, command, transport=task_transport(project, calls))
    result = work(lambda *args: None)
    assert result["calls"] == 6 and project.current("prompt_bundle")
    assert work(lambda *args: None) == result and len(calls) == 10
    assert project.load()["confirmations"] == {}
    stored = json.loads((project.root / "tasks" / (command["request_id"] + ".json")).read_text(encoding="utf-8"))
    assert stored["calls_used"] == 6 and len(stored["outputs"]) == 3


def test_asset_task_hard_budget_preserves_completed_items(project):
    prepare_story(project)
    command = prepare_prompt_task(project, "hosted-llm")
    command["max_calls"] = 2
    calls = []
    result = CreationWork(project, command, transport=task_transport(project, calls))(lambda *args: None)
    assert result["status"] == "partial" and result["review_status"] == "incomplete"
    assert len(calls) == 2 and not project.current("prompt_bundle")
    assert len([key for key in project.load()["current"] if key.startswith("character_spec:")]) == 1


def test_export_validates_only_selected_content_and_preserves_stale_provenance(project, tmp_path):
    novel = generate(project, "novel_copy")
    prepare_story(project)
    ProjectService(project).import_text("最新的另一篇文章。", expected_revision=project.load()["revision"])
    with pytest.raises(WorkspaceError):
        ExportService(project).snapshot(keys=["novel_copy"])
    result = ExportService(project).export(tmp_path / "旧稿.md", keys=["novel_copy"], allow_stale=True)
    manifest = json.loads((tmp_path / "旧稿.md.manifest.json").read_text(encoding="utf-8"))
    selected = project.current("novel_copy")
    assert manifest["records"]["novel_copy"]["dependency_refs"] == selected["dependency_refs"]
    assert "storyboard" not in result["snapshot"]["records"]
    assert project.current("selection")["id"] not in manifest["provenance"]
    assert "version_" not in (tmp_path / "旧稿.md").read_text(encoding="utf-8")


def test_shot_prompt_regeneration_uses_same_review_loop_and_exact_refs(project):
    prepare_story(project)
    command = prepare_prompt_task(project, "hosted-llm")
    CreationWork(project, command, transport=task_transport(project, []))(lambda *args: None)
    bundle = project.current("prompt_bundle")
    revised = deepcopy(bundle["content"])
    revised["shots"][0]["resolved_prompt"] = "铜钥匙在井沿，冷光特写，手缓慢离开。"
    service, calls = sequence_service(project, [revised, {"issues": []}])
    result = service.execute(service.prepare("shot_prompts", mode="revise", target_id=bundle["id"], asks=["第一镜更紧凑"], shot_ids=[revised["shots"][0]["shot_id"]]))
    assert result["review_status"] == "passed" and len(calls) == 2
    current = project.current("prompt_bundle")
    assert current["content"]["shots"][0]["asset_refs"] == bundle["content"]["shots"][0]["asset_refs"]
    assert current["content"]["shots"][1] == bundle["content"]["shots"][1]


def test_partial_prompt_export_names_missing_items_without_review_gate(project, tmp_path):
    prepare_story(project)
    entity = project.current("asset_catalog")["content"]["entities"][0]
    generate(project, "asset_spec", entity=entity, kind="character_spec")
    exporter = ExportService(project)
    with pytest.raises(WorkspaceError) as error:
        exporter.snapshot(keys=["prompts"])
    assert error.value.code == "EXPORT_INCOMPLETE" and "铜钥匙" in str(error.value)
    result = exporter.export(tmp_path / "已有提示词.md", keys=["prompts"], allow_partial=True)
    assert "镜头提示词" in result["snapshot"]["missing"]
    body = (tmp_path / "已有提示词.md").read_text(encoding="utf-8")
    assert "本次未包含" in body and "深蓝棉布外套" in body
    assert project.load()["confirmations"] == {}


@pytest.mark.parametrize("max_retries", [0, 1, 2])
def test_agent_loop_counts_initial_generation_separately_and_returns_latest(project, max_retries):
    candidates = []
    generated_prompts = []
    feedback = "ONLY_IN_MEMORY_检查反馈_压缩开头"
    checks = []
    def generate_candidate(prompt):
        generated_prompts.append(prompt)
        candidate = sample(project, "novel_copy")
        candidate["opening"]["text"] = f"第{len(generated_prompts)}次候选"
        candidates.append(candidate)
        return encode(candidate)
    def check(command, candidate):
        checks.append(candidate["content"])
        assert command["requirements"]["instruction"]
        assert command["context"]["article"]["text"]
        return QualityCheckResult(False, feedback)
    service = WorkflowService(project, max_retries=max_retries, transport=generate_candidate, quality_check=check)
    command = service.prepare("novel_copy")
    result = service.execute(command)
    assert len(candidates) == len(checks) == max_retries + 1
    assert result["retries_used"] == max_retries and result["review_status"] == "issues"
    assert project.current("novel_copy")["content"] == candidates[-1]
    for index, prompt in enumerate(generated_prompts[1:]):
        assert prompt.startswith(command["prompt"])
        assert candidates[index]["opening"]["text"] in prompt and feedback in prompt
    assert service.execute(command) == result  # the existing request deduplication still works
    assert project.load()["reports"] == {} and not list(project.root.rglob("*report*"))
    for path in project.root.rglob("*.json"):
        assert feedback not in path.read_text(encoding="utf-8")


def test_first_check_passes_without_retry_and_preserves_discussion(project):
    conversation = ConversationService(project).store.create()
    before = ConversationService(project).store.get(conversation["id"])
    calls, checks = [], []
    content = sample(project, "novel_copy")
    def send(prompt):
        calls.append(prompt)
        return encode(content)
    def check(command, candidate):
        checks.append(candidate)
        return QualityCheckResult(True)
    service = WorkflowService(project, transport=send, quality_check=check)
    result = service.execute(service.prepare("novel_copy"))
    assert len(calls) == len(checks) == 1 and result["retries_used"] == 0
    assert result["review_status"] == "passed"
    assert project.current("novel_copy")["content"] == content
    assert ConversationService(project).store.get(conversation["id"]) == before


def test_feedback_is_temporary_even_in_actual_checker_request_records(project):
    original = sample(project, "novel_copy")
    revised = deepcopy(original)
    revised["opening"]["text"] = "新的开头"
    problem = {**ISSUE, "suggestion": "TRANSIENT_ONLY_不要持久化此反馈"}
    service, calls = sequence_service(project, [original, {"issues": [problem]}, revised, {"issues": []}])
    conversation = ConversationService(project).store.create()
    notifications = []
    result = service.execute(service.prepare("novel_copy", asks=["原始要求要保留"]), on_request=lambda *args: notifications.append(args))
    assert notifications == [("", "", None)] * 4
    assert result["review_status"] == "passed" and len(calls) == 4
    assert "TRANSIENT_ONLY" in calls[2] and "原始要求要保留" in calls[1]
    assert project.load()["reports"] == {}
    assert ConversationService(project).store.get(conversation["id"])["messages"] == []
    for path in project.root.rglob("*.json"):
        assert "TRANSIENT_ONLY" not in path.read_text(encoding="utf-8")
    assert all(set(call) == {"type", "error", "created_at"} for call in project.load()["runs"][-1]["calls"])


@pytest.mark.parametrize("phase", ["generation", "check", "retry"])
def test_provider_errors_do_not_become_quality_feedback(project, phase):
    content = sample(project, "novel_copy")
    responses = {
        "generation": [ConnectionError("offline")],
        "check": [content, ConnectionError("offline")],
        "retry": [content, {"issues": [ISSUE]}, ConnectionError("offline")],
    }[phase]
    service, calls = sequence_service(project, responses)
    if phase == "generation":
        with pytest.raises(WorkspaceError):
            service.execute(service.prepare("novel_copy"))
    else:
        result = service.execute(service.prepare("novel_copy"))
        assert result["review_status"] == "unavailable"
        assert project.current("novel_copy")["content"] == content
    assert len(calls) == len(responses)


@pytest.mark.parametrize("phase", ["generation", "check"])
def test_cancel_does_not_enter_quality_retry(project, phase):
    token = CancelToken()
    calls = []
    def send(prompt):
        calls.append(prompt)
        if phase == "generation" or prompt.startswith("自动审阅"):
            token.cancel()
        return encode({"issues": [ISSUE]}) if prompt.startswith("自动审阅") else encode(sample(project, "novel_copy"))
    service = WorkflowService(project, transport=send)
    with pytest.raises(GenerationCancelled):
        service.execute(service.prepare("novel_copy"), token=token)
    assert len(calls) == (1 if phase == "generation" else 2)
    assert project.current("novel_copy") is None
    assert project.load()["runs"][-1]["status"] == "cancelled"


@pytest.mark.parametrize("response", ["not json", '{"issues":[{}]}'])
def test_invalid_check_response_is_unavailable_not_a_quality_failure(project, response):
    service, calls = sequence_service(project, [sample(project, "novel_copy"), response])
    result = service.execute(service.prepare("novel_copy"))
    assert len(calls) == 2 and result["review_status"] == "unavailable"
    assert result["retries_used"] == 0


@pytest.mark.parametrize("stage,asset_kind", [
    ("novel_copy", None), ("video_copy", None), ("storyboard", None),
    ("asset_spec", "character"), ("asset_spec", "scene"), ("asset_spec", "prop"),
])
def test_all_business_generation_stages_use_agent_loop(project, stage, asset_kind):
    prepare_story(project)
    entity = next((e for e in project.current("asset_catalog")["content"]["entities"] if e["kind"] == asset_kind), None)
    params = {"entity": entity, "kind": ASSET_KINDS[asset_kind]} if entity else {}
    content = sample(project, stage, **params)
    generated, checked = [], []
    def send(prompt):
        generated.append(prompt)
        return encode(content)
    def check(command, candidate):
        checked.append(deepcopy(candidate["content"]))
        assert command["stage"] == stage
        if entity:
            assert len(candidate["content"]["views"]) == {"character": 4, "scene": 2, "prop": 3}[asset_kind]
        return QualityCheckResult(len(checked) == 2, "使当前任务的表达更具体")
    service = WorkflowService(project, transport=send, quality_check=check)
    command = service.prepare(stage, **({"entity_id": entity["asset_id"], "variant_id": entity["variants"][0]["variant_id"], "kind": params["kind"]} if entity else {}))
    result = service.execute(command)
    assert len(generated) == len(checked) == 2 and result["review_status"] == "passed"
    assert generated[1].startswith(command["prompt"]) and "使当前任务的表达更具体" in generated[1]
    assert project.current(command["key"])["id"] == result["version_id"]
