"""Recovery at durable boundaries, including real process exit and paid-call counts."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from core.common.cancellation import CancelToken, GenerationCancelled
from core.short_video.v2 import runtime, workflow, creation
from core.short_video.v2.models import WorkspaceError, encode
from core.short_video.v2.repository import ProjectRepository
from core.short_video.v2.review import QualityCheckResult
from test_video_v2_repository import project, prompt_project, novel  # noqa: F401


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from core.short_video.v2 import llm
    monkeypatch.setattr(llm, "NETWORK_MAX_RETRIES", 0)
    snapshot = {"provider": "hosted-llm", "model": "offline"}
    monkeypatch.setattr(workflow, "capture_model_snapshot", lambda _: snapshot)
    monkeypatch.setattr(creation, "capture_model_snapshot", lambda _: snapshot)
    monkeypatch.setattr(runtime, "validate_model_snapshot", lambda _: None)


def test_pause_after_response_resumes_without_generation(project):
    content = novel(project)["content"]
    command = workflow.WorkflowService(project).prepare("novel_copy")
    original = project.current("novel_copy")["id"]
    token = CancelToken()

    def send(_):
        token.cancel()
        return encode(content)

    with pytest.raises(GenerationCancelled):
        workflow.WorkflowService(project, transport=send).execute(command, token=token)
    run = project.load()["runs"][-1]
    assert run["checkpoint"]["step"] == "validate"
    assert run["checkpoint"]["response"] == encode(content)
    assert project.current("novel_copy")["id"] == original
    project.recover()
    calls = []
    result = runtime.ResumeWork(project, command["request_id"], transport=lambda p: calls.append(p) or '{"issues":[]}')(lambda *_: None)
    assert len(calls) == 1 and "自动审阅" in calls[0]
    assert result["status"] == "succeeded"
    assert "checkpoint" not in project.load()["runs"][-1]
    assert len(project.load()["records"]) == len(project.load()["current"])


def test_review_timeout_preserves_candidate_and_continues_only_review(prompt_project):
    repository = prompt_project
    original = repository.current("storyboard")
    command = workflow.WorkflowService(repository).prepare("storyboard")
    calls = []

    def send(p):
        calls.append(p)
        if len(calls) == 2:
            raise TimeoutError("review timed out")
        return encode(original["content"])

    with pytest.raises(TimeoutError):
        workflow.WorkspaceWork(repository, command, transport=send)(lambda *_: None)
    run = repository.load()["runs"][-1]
    candidate = run["checkpoint"]["candidate"]
    repository.recover()
    assert repository.record(candidate)
    assert repository.current("storyboard")["id"] == original["id"]
    recovery = runtime.get_recovery(repository, "storyboard")
    assert recovery["unknown"] and "重复计费" in runtime.recovery_message(recovery)
    calls.clear()
    result = runtime.ResumeWork(repository, command["request_id"], transport=lambda p: calls.append(p) or '{"issues":[]}')(lambda *_: None)
    assert result["version_id"] == candidate
    assert len(calls) == 1 and "自动审阅" in calls[0]


@pytest.mark.parametrize("change", ["config", "source", "template", "target"])
def test_changed_inputs_invalidate_resume_and_clear_temporary_candidate(project, change):
    original = novel(project)
    command = workflow.WorkflowService(project).prepare("novel_copy")
    service = workflow.WorkflowService(project, transport=lambda _: encode(original["content"]),
                                       quality_check=lambda *_: (_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(TimeoutError):
        service.execute(command)
    candidate = project.load()["runs"][-1]["checkpoint"]["candidate"]
    if change == "template":
        project.save("prompt_template", {"stage": "novel_copy", "instruction": "新要求"}, {},
                     key="prompt_template:novel_copy", expected_revision=project.load()["revision"])
    else:
        record = project.current({"target": "novel_copy"}.get(change, change))
        project.save(record["kind"], record["content"], record["dependency_refs"],
                     expected_revision=project.load()["revision"])
    task = project.load()["tasks"][command["request_id"]]
    assert task["status"] == "invalidated"
    assert candidate not in project.load()["records"]
    with pytest.raises(WorkspaceError):
        runtime.ResumeWork(project, task["id"], transport=lambda _: pytest.fail("must not send"))(lambda *_: None)


def test_new_generation_supersedes_paused_checkpoint(project):
    original = novel(project)
    service = workflow.WorkflowService(project, transport=lambda _: encode(original["content"]),
                                       quality_check=lambda *_: (_ for _ in ()).throw(TimeoutError()))
    first = service.prepare("novel_copy")
    with pytest.raises(TimeoutError):
        service.execute(first)
    candidate = project.load()["runs"][-1]["checkpoint"]["candidate"]
    second = service.prepare("novel_copy")
    service.quality_check = lambda *_: QualityCheckResult(True)
    service.execute(second)
    assert project.load()["tasks"][first["request_id"]]["status"] == "invalidated"
    assert candidate not in project.load()["records"]


def test_batch_resumes_at_second_asset_review_and_compiles_once(prompt_project):
    repository = prompt_project
    content = {k: repository.current(f"{k}_spec:asset_{k}:base")["content"] for k in ("character", "scene", "prop")}
    with repository.transaction() as state:
        for key in list(state["current"]):
            if "_spec:" in key:
                state["current"].pop(key)
    command = creation.prepare_prompt_task(repository, "hosted-llm")
    responses = iter([encode(content["character"]), '{"issues":[]}', encode(content["scene"])])
    calls = []

    def send(p):
        calls.append(p)
        if len(calls) == 4:
            raise TimeoutError()
        return next(responses)

    with pytest.raises(TimeoutError):
        creation.CreationWork(repository, command, transport=send)(lambda *_: None)
    task = repository.load()["tasks"][command["request_id"]]
    assert task["completed"] == 1
    first = repository.current("character_spec:asset_character:base")["id"]
    # Parent completion and selected business output are committed together.
    assert task["items"]["0"]["result"]["version_id"] == first
    assert "command" not in task["items"]["0"]
    repository.recover()
    responses = iter(['{"issues":[]}', encode(content["prop"]), '{"issues":[]}'])
    calls.clear()
    observed = []
    def resume_send(prompt):
        observed.append(work.task_context.completed)
        calls.append(prompt)
        return next(responses)
    work = runtime.ResumeWork(repository, task["id"], transport=resume_send)
    result = work(lambda *_: None)
    assert observed == [1, 2, 2]
    assert len(calls) == 3 and "自动审阅" in calls[0]
    assert repository.current("character_spec:asset_character:base")["id"] == first
    task = repository.load()["tasks"][task["id"]]
    assert task["status"] == "succeeded" and task["completed"] == 3
    assert repository.current("prompt_bundle")["id"] in task["outputs"]
    assert "command" not in task and "items" not in task
    assert result["review_status"] == "passed"


@pytest.mark.parametrize("boundary,remaining", [("sending", 2), ("validate", 1), ("review", 1), ("review_response", 0), ("commit", 0), ("selected", 0)])
def test_real_process_exit_recovers_from_last_durable_boundary(project, boundary, remaining):
    novel(project)
    script = r'''
import os, sys, json
from core.short_video.v2 import workflow, runtime, repository, models
r = repository.ProjectRepository(sys.argv[1])
workflow.capture_model_snapshot = lambda _: {"provider":"hosted-llm", "model":"offline"}
runtime.validate_model_snapshot = lambda _: None
models.capture_review_model_snapshot = lambda: {"provider":"hosted-llm", "model":"review-offline", "endpoint_hash":"offline"}
original = r.current("novel_copy")["content"]
write = repository.atomic_write_text
def exiting(path, value):
    write(path, value)
    if path != r.path:
        return
    state = json.loads(value)
    if not state["runs"]:
        return
    run = state["runs"][-1]
    cp = run.get("checkpoint", {})
    boundary = sys.argv[2]
    hit = (boundary == cp.get("step"))
    hit |= boundary == "sending" and bool(run["calls"]) and run["calls"][-1].get("state") == "sending"
    hit |= boundary == "review_response" and cp.get("step") == "review" and cp.get("response") is not None
    hit |= boundary == "selected" and run["status"] == "succeeded"
    if hit:
        os._exit(73)
repository.atomic_write_text = exiting
service = workflow.WorkflowService(r, transport=lambda p: '{"issues":[]}' if "自动审阅" in p else json.dumps(original, ensure_ascii=False))
service.execute(service.prepare("novel_copy"))
'''
    result = subprocess.run([sys.executable, "-c", script, project.output_dir, boundary],
                            cwd=Path(__file__).resolve().parents[2], capture_output=True, timeout=30)
    assert result.returncode == 73, result.stderr.decode(errors="replace")
    repository = ProjectRepository(project.output_dir)
    repository.recover()
    task = next(iter(repository.load()["tasks"].values()))
    assert task["status"] == "interrupted"
    calls = []
    original = repository.current("novel_copy")["content"]
    runtime.ResumeWork(repository, task["id"], transport=lambda p: calls.append(p) or ('{"issues":[]}' if "自动审阅" in p else encode(original)))(lambda *_: None)
    assert len(calls) == remaining
    assert sum("自动审阅" in p for p in calls) == min(remaining, 1)
    assert repository.load()["tasks"][task["id"]]["status"] == "succeeded"


def test_recovery_does_not_reset_format_or_quality_attempts(project):
    original = novel(project)
    command = workflow.WorkflowService(project).prepare("novel_copy")
    sends = iter(["bad JSON", encode(original["content"])])
    token = CancelToken()

    def check(*_):
        token.cancel()
        return QualityCheckResult(False, "需修订", 1)

    with pytest.raises(GenerationCancelled):
        workflow.WorkflowService(project, transport=lambda _: next(sends), quality_check=check).execute(command, token=token)
    cp = project.load()["runs"][-1]["checkpoint"]
    assert cp["format_attempt"] == 1 and cp["revision"] == 0 and cp["step"] == "decide"
    responses = iter([encode(original["content"]), '{"issues":[]}' ])
    result = runtime.ResumeWork(project, command["request_id"], transport=lambda _: next(responses))(lambda *_: None)
    assert result["retries_used"] == 1


def test_duplicate_request_cannot_change_inputs(project):
    content = novel(project)["content"]
    service = workflow.WorkflowService(project, transport=lambda _: encode(content), quality_check=lambda *_: QualityCheckResult(True))
    command = service.prepare("novel_copy")
    service.execute(command)
    changed = deepcopy(command)
    changed["prompt"] += "不同任务"
    with pytest.raises(WorkspaceError, match="任务编号"):
        service.execute(changed)


def test_recovery_reads_only_manifest_tasks_and_keeps_current_content(project):
    before = dict(project.load()["current"])
    folder = project.root / "tasks"
    folder.mkdir()
    path = folder / "standalone.json"
    path.write_text("not a task document", encoding="utf-8")
    project.recover()
    assert path.read_text(encoding="utf-8") == "not a task document"
    assert project.load()["current"] == before
    assert project.load()["tasks"] == {}
    assert runtime.get_recovery(project, "prompt_task") is None


@pytest.mark.parametrize("field", ["tasks", "runtime_schema_version"])
def test_repository_requires_current_task_storage_fields_without_rewriting(project, field):
    state = project.load()
    state.pop(field)
    project.path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    before = project.path.read_bytes()
    with pytest.raises(WorkspaceError, match="任务|运行任务格式"):
        project.load()
    assert project.path.read_bytes() == before


def test_unknown_runtime_version_is_invalidated(project):
    original = novel(project)
    token = CancelToken()
    command = workflow.WorkflowService(project).prepare("novel_copy")
    with pytest.raises(GenerationCancelled):
        workflow.WorkflowService(project, transport=lambda _: (token.cancel() or encode(original["content"]))).execute(command, token=token)
    with project.transaction() as state:
        state["tasks"][command["request_id"]]["runtime_version"] = 999
    assert project.load()["tasks"][command["request_id"]]["status"] == "invalidated"


def test_concurrent_resume_and_recover_cannot_take_over_a_live_worker(project):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    original = novel(project)
    token = CancelToken()
    command = workflow.WorkflowService(project).prepare("novel_copy")
    with pytest.raises(GenerationCancelled):
        workflow.WorkflowService(project, transport=lambda _: token.cancel() or encode(original["content"])).execute(command, token=token)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def send(prompt):
        calls.append(prompt)
        entered.set()
        assert release.wait(10)
        return '{"issues":[]}'

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runtime.ResumeWork(project, command["request_id"], transport=send), lambda *_: None)
        try:
            assert entered.wait(10)
            with pytest.raises(WorkspaceError) as caught:
                runtime.ResumeWork(project, command["request_id"], transport=lambda _: pytest.fail("duplicate request"))(lambda *_: None)
            assert caught.value.code == "PROJECT_BUSY"
            project.recover()
            assert project.load()["tasks"][command["request_id"]]["status"] == "running"
        finally:
            release.set()
        assert future.result(timeout=10)["status"] == "succeeded"
    assert len(calls) == 1


def test_response_write_failure_stops_requests_and_preserves_original_error(project, monkeypatch):
    import core.short_video.v2.repository as storage

    original = novel(project)
    command = workflow.WorkflowService(project).prepare("novel_copy")
    write = storage.atomic_write_text
    disk_full = OSError("disk full")
    calls = []

    def fail_on_response(path, value):
        if path == project.path:
            state = json.loads(value)
            if any(r.get("checkpoint", {}).get("response") is not None for r in state["runs"]):
                raise disk_full
        write(path, value)

    monkeypatch.setattr(storage, "atomic_write_text", fail_on_response)
    with pytest.raises(runtime.RecordingError) as caught:
        workflow.WorkflowService(project, transport=lambda p: calls.append(p) or encode(original["content"])).execute(command)
    assert caught.value.__cause__ is disk_full
    assert len(calls) == 1
    assert project.current("novel_copy")["id"] == original["id"]
    assert runtime.get_recovery(project, "novel_copy")["unknown"]


def test_corrupt_checkpoint_does_not_send_or_modify_project(project):
    original = novel(project)
    command = workflow.WorkflowService(project).prepare("novel_copy")
    token = CancelToken()
    with pytest.raises(GenerationCancelled):
        workflow.WorkflowService(project, transport=lambda _: token.cancel() or encode(original["content"])).execute(command, token=token)
    state = project.load()
    state["runs"][-1]["checkpoint"]["step"] = "not-a-step"
    project.path.write_text(encode(state), encoding="utf-8")
    before = project.path.read_bytes()
    with pytest.raises(WorkspaceError, match="检查点损坏"):
        project.recover()
    assert project.path.read_bytes() == before


def test_composed_storyboard_freezes_skill_before_catalog_generation(prompt_project, monkeypatch):
    from test_video_v2_repository import rebuild_with_config

    cfg = prompt_project.current("config")["content"]
    cfg["storyboard_skill_id"] = "short-video-content-storyboard"
    rebuild_with_config(prompt_project, cfg)
    catalog = prompt_project.current("asset_catalog")["content"]
    board = prompt_project.current("storyboard")["content"]
    monkeypatch.setattr(workflow, "load_storyboard_skill", lambda _: "冻结的旧规则")
    command = creation.prepare_storyboard_task(prompt_project, "hosted-llm")
    monkeypatch.setattr(workflow, "load_storyboard_skill", lambda _: pytest.fail("must use frozen skill"))
    responses = iter([encode(catalog), '{"issues":[]}', encode(board), '{"issues":[]}'])
    prompts = []
    creation.CreationWork(prompt_project, command, transport=lambda p: prompts.append(p) or next(responses))(lambda *_: None)
    assert "冻结的旧规则" in prompts[2]


@pytest.mark.parametrize("stage", ["novel_copy", "video_copy", "storyboard"])
def test_continue_button_appears_only_for_valid_pending_task(prompt_project, stage):
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from core.gui.pages.short_video.artifact_page import ArtifactPage

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    page = ArtifactPage(SimpleNamespace(repository=prompt_project, perform=lambda cb: cb()), stage, "内容")
    page.refresh()
    assert page.resume_button.isHidden()
    token = CancelToken()
    original = prompt_project.current(stage)
    command = workflow.WorkflowService(prompt_project).prepare(stage)
    with pytest.raises(GenerationCancelled):
        workflow.WorkflowService(prompt_project, transport=lambda _: token.cancel() or encode(original["content"])).execute(command, token=token)
    page.refresh()
    assert not page.resume_button.isHidden()
    assert "未完成任务" in page.status.text()
    assert page.editor.value() == original["content"]
    prompt_project.save(original["kind"], original["content"], original["dependency_refs"],
                        expected_revision=prompt_project.load()["revision"])
    page.refresh()
    assert page.resume_button.isHidden()
    assert "上次任务不能继续" in page.status.text()
    page.close()
    app.processEvents()


def test_fatal_invalid_output_has_no_resume_or_saved_candidate_claim(project):
    original = novel(project)
    command = workflow.WorkflowService(project).prepare("novel_copy")
    work = workflow.WorkspaceWork(project, command, transport=lambda _: "invalid JSON")
    with pytest.raises(WorkspaceError):
        work(lambda *_: None)
    assert not work.task_context.saved and not work.task_context.resumable
    assert runtime.get_recovery(project, "novel_copy") is None
    assert project.current("novel_copy")["id"] == original["id"]


def test_template_changed_between_prepare_and_admission_is_rejected(project):
    command = workflow.WorkflowService(project).prepare("novel_copy")
    project.save("prompt_template", {"stage": "novel_copy", "instruction": "刚修改的新要求"}, {},
                 key="prompt_template:novel_copy", expected_revision=project.load()["revision"])
    with pytest.raises(WorkspaceError, match="任务输入已变化"):
        workflow.WorkflowService(project, transport=lambda _: pytest.fail("outdated prompt sent")).execute(command)
    assert not project.load()["tasks"]


def test_compile_cannot_overwrite_prompt_bundle_edited_during_task(prompt_project):
    command = creation.prepare_prompt_task(prompt_project, "hosted-llm")
    assert not command["items"]
    original = prompt_project.current("prompt_bundle")

    def report(*_):
        prompt_project.save("prompt_bundle", original["content"], original["dependency_refs"],
                            expected_revision=prompt_project.load()["revision"])

    with pytest.raises(WorkspaceError, match="输入已变化"):
        creation.CreationWork(prompt_project, command, transport=lambda _: pytest.fail("local-only task"))(report)
    assert prompt_project.current("prompt_bundle")["id"] != original["id"]
    assert prompt_project.load()["tasks"][command["request_id"]]["status"] == "invalidated"
