"""Workspace provenance and atomicity, using real temporary files."""
from copy import deepcopy
from pathlib import Path
import json
import threading

import pytest

from core.short_video.v2.models import default_config, WorkspaceError
from core.short_video.v2.project_service import ProjectService, invoke
from core.short_video.v2.repository import ProjectRepository
from core.short_video.v2.source import qt_to_codepoint, codepoint_to_qt, source_content
import core.short_video.v2.repository as repository_module


ARTICLE = "\ufeff乡村😀老宅\r\n井口响了三声。\r父亲留下铜钥匙。"


@pytest.fixture
def project(tmp_path):
    repository = ProjectRepository(tmp_path)
    ProjectService(repository).import_text(ARTICLE, expected_revision=0)
    return repository


def novel(repository):
    state = repository.load()
    refs = ProjectService(repository).context()["source_refs"]
    content = {"opening": {"text": "封井里传出了熟悉的声音。", "source_refs": refs},
               "paragraphs": [{"id": "p1", "text": "我回到老宅，在井边找到铜钥匙。", "source_refs": refs}],
               "cliffhanger": {"text": "父亲为什么留下它？", "source_refs": refs}, "changes": []}
    deps = {k: state["current"][k] for k in ("selection", "config")}
    return repository.save("novel_copy", content, deps, expected_revision=state["revision"])


def test_reads_are_lazy(tmp_path):
    repository = ProjectRepository(tmp_path)
    assert repository.load()["schema_version"] == 2
    repository.recover()
    assert not repository.root.exists()


def test_normalization_and_qt_unicode_roundtrip(project):
    source = project.current("source")["content"]["normalized_text"]
    assert source == "乡村😀老宅\n井口响了三声。\n父亲留下铜钥匙。"
    for offset in range(len(source) + 1):
        assert qt_to_codepoint(source, codepoint_to_qt(source, offset)) == offset
    with pytest.raises(WorkspaceError):
        qt_to_codepoint(source, 3)  # middle of the emoji's UTF-16 pair


def test_multiple_ranges_reference_original_coordinates(project):
    service = ProjectService(project)
    source = project.current("source")["content"]
    ranges = [{"start": 0, "end": 2}, {"start": 14, "end": len(source["normalized_text"])}]
    service.save_inputs(project.current("config")["content"], source, ranges,
                        expected_revision=project.load()["revision"])
    context = service.context()
    assert context["segments"][1]["start"] == 14
    assert context["text"] == "乡村\n父亲留下铜钥匙。"
    before = project.path.read_bytes()
    with pytest.raises(WorkspaceError):
        service.save_inputs(project.current("config")["content"], source,
                            [{"start": 0, "end": 5}, {"start": 4, "end": 8}], expected_revision=project.load()["revision"])
    assert project.path.read_bytes() == before


def test_same_inputs_are_noop_and_long_target_allowed(project):
    state = project.load()
    source = project.current("source")["content"]
    config = project.current("config")["content"]
    ProjectService(project).save_inputs(config, source, expected_revision=state["revision"])
    assert project.load() == state
    config["target_duration_ms"] = 600000
    ProjectService(project).save_inputs(config, source, expected_revision=state["revision"])
    assert project.current("config")["content"]["target_duration_ms"] == 600000


def test_encoding_failure_preserves_existing_project(project, tmp_path):
    path = tmp_path / "原文.txt"
    path.write_bytes("乡村故事".encode("gbk"))
    before = project.path.read_bytes()
    with pytest.raises(WorkspaceError, match="解码"):
        ProjectService(project).import_file(path, expected_revision=project.load()["revision"])
    assert project.path.read_bytes() == before
    ProjectService(project).import_file(path, encoding="gbk", expected_revision=project.load()["revision"])
    assert ProjectService(project).context()["text"] == "乡村故事"


def test_source_and_config_changes_stale_descendants_but_keep_versions(project):
    first = novel(project)
    video = project.save("video_copy", {"blocks": [
        {"id": "b1", "role": "opening", "narration": "井口有声音", "visible_action": "镜头靠近井口", "dialogue": []},
        {"id": "b2", "role": "cliffhanger", "narration": "是谁？", "visible_action": "", "dialogue": []}]},
        {"novel_copy": first["id"]}, expected_revision=project.load()["revision"])
    assert project.is_current(video["id"])
    cfg = project.current("config")["content"]
    cfg["tracks"]["backgrounds"] = ["乡村"]
    ProjectService(project).save_inputs(cfg, project.current("source")["content"], expected_revision=project.load()["revision"])
    assert not project.is_current(first["id"]) and not project.is_current(video["id"])
    assert project.record(video["id"])["content"] == video["content"]


def test_manual_revision_preserves_original_version(project):
    first = novel(project)
    changed = deepcopy(first["content"])
    changed["opening"]["text"] = "这口井藏着什么？"
    second = project.save("novel_copy", changed, first["dependency_refs"], parent_id=first["id"],
                          expected_revision=project.load()["revision"])
    assert second["parent_id"] == first["id"]
    assert project.current("novel_copy")["id"] == second["id"]
    assert project.record(first["id"])["content"]["opening"]["text"] != second["content"]["opening"]["text"]


def test_conflicting_save_and_failed_commit_do_not_lose_old_state(project, monkeypatch):
    first = novel(project)
    old_revision = project.load()["revision"]
    novel(project)
    before = project.path.read_bytes()
    with pytest.raises(WorkspaceError) as error:
        project.select(first["id"], expected_revision=old_revision)
    assert error.value.code == "REVISION_CONFLICT" and project.path.read_bytes() == before
    actual_atomic = repository_module.atomic_write_text
    def broken(path, value):
        if path == project.path:
            raise OSError("disk full")
        actual_atomic(path, value)
    monkeypatch.setattr(repository_module, "atomic_write_text", broken)
    with pytest.raises(OSError):
        novel(project)
    assert project.path.read_bytes() == before
    assert len(project.versions("novel_copy")) == 2


def test_quote_cannot_claim_text_outside_selected_article(project):
    source = project.current("source")
    ProjectService(project).save_inputs(project.current("config")["content"], source["content"],
                                        [{"start": 0, "end": 2}], expected_revision=project.load()["revision"])
    first = novel(project)
    invalid = deepcopy(first["content"])
    invalid["opening"]["source_refs"] = [{"source_id": source["id"], "start": 14, "end": 16, "quote": "父亲"}]
    with pytest.raises(WorkspaceError, match="选区"):
        project.save("novel_copy", invalid, first["dependency_refs"], expected_revision=project.load()["revision"])


def test_corrupt_record_and_path_escape_fail_without_rewriting(project):
    first = novel(project)
    before = project.path.read_bytes()
    path = project.root / "artifacts" / f"{first['id']}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["content"]["opening"]["text"] = "external modification"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(WorkspaceError):
        project.load()
    with pytest.raises(WorkspaceError):
        project.record("../../project")
    assert project.path.read_bytes() == before


def test_run_deduplication_and_recovery_honor_live_generation(project):
    command = {"stage": "novel_copy", "request_id": "one"}
    revision = project.load()["revision"]
    with project.generation_lock():
        run, created = project.start_run(command, expected_revision=revision, snapshot={})
        assert created
        before = project.path.read_bytes()
        repeated, created = project.start_run(command, expected_revision=revision, snapshot={})
        assert not created and repeated["id"] == run["id"] and project.path.read_bytes() == before
        with pytest.raises(WorkspaceError) as exc:
            project.start_run({**command, "stage": "video_copy"}, expected_revision=revision, snapshot={})
        assert exc.value.code == "REQUEST_KEY_CONFLICT"
        # FileLock is reentrant in the owning thread; recovery from another thread must not steal it.
        thread = threading.Thread(target=project.recover)
        thread.start()
        thread.join(3)
        assert not thread.is_alive()
        assert project.load()["runs"][-1]["status"] == "running"
    project.recover()
    assert project.load()["runs"][-1]["status"] == "interrupted"


def test_result_boundary_returns_structured_error(project):
    result = invoke(ProjectService(project).import_text, "", expected_revision=project.load()["revision"])
    assert result["error"]["code"] == "INVALID_INPUT" and "data" not in result


@pytest.fixture
def prompt_project(project):
    from core.short_video.v2.models import VIEWS
    from core.short_video.v2.prompt_compiler import finalize_asset_prompts
    from core.short_video.v2.validation import SPEC_FIELDS
    from core.short_video.v2.workflow import WorkflowService

    first = novel(project)
    video = project.save("video_copy", {"blocks": [
        {"id": "b1", "role": "opening", "narration": "井口有声音", "visible_action": "靠近井口", "dialogue": []},
        {"id": "b2", "role": "cliffhanger", "narration": "是谁？", "visible_action": "停下脚步", "dialogue": []},
    ]}, {"novel_copy": first["id"]}, expected_revision=project.load()["revision"])
    entities = [
        {"asset_id": "asset_" + kind, "kind": kind, "name": name,
         "variants": [{"variant_id": "base", "label": "常态"}]}
        for kind, name in [("character", "寻井人"), ("scene", "老宅"), ("prop", "铜钥匙")]
    ]
    catalog = project.save("asset_catalog", {"entities": entities}, {"video_copy": video["id"]},
                           expected_revision=project.load()["revision"])
    dependencies = {"asset_catalog": catalog["id"],
                    **{key: project.current(key)["id"] for key in ("config", "selection")}}
    for entity in entities:
        kind = entity["kind"] + "_spec"
        content = {field: "明确的视觉设定" for field in SPEC_FIELDS[kind]}
        content.update(asset_id=entity["asset_id"], variant_id="base", source_refs=[])
        content["views"] = [
            {"view_id": view_id, "label": label, "camera_view": label,
             "composition": "完整主体", "required": True,
             "positive_prompt": f"{entity['name']}的{label}，保持造型与状态一致",
             "negative_prompt": "避免身份漂移和空间结构矛盾"}
            for view_id, label in VIEWS[entity["kind"]]
        ]
        project.save(kind, finalize_asset_prompts(kind, content), dependencies,
                     expected_revision=project.load()["revision"])
    shot = {"shot_id": "shot_1", "order": 1, "duration_ms": 7000, "shot_size": "中景",
            "camera_movement": "固定镜头", "action": "举起铜钥匙", "visual_prompt_draft": "老宅井边举起铜钥匙",
            "sound": "风声", "narration": "井里是谁", "dialogue": [], "source_refs": [],
            "scene_ref": {"asset_id": "asset_scene", "variant_id": "base"},
            "characters": [{"asset_id": "asset_character", "variant_id": "base"}],
            "props": [{"asset_id": "asset_prop", "variant_id": "base"}], "video_block_refs": ["b1", "b2"]}
    project.save("storyboard", {"shots": [shot]},
                 {"asset_catalog": catalog["id"], "video_copy": video["id"],
                  **{key: project.current(key)["id"] for key in ("config", "selection")}},
                 expected_revision=project.load()["revision"])
    WorkflowService(project).compile_storyboard_prompts(expected_revision=project.load()["revision"])
    return project


@pytest.mark.parametrize("kind", ["character_spec", "scene_spec", "prop_spec"])
def test_prompt_save_replaces_files_and_rebuilds_only_current_bundle(prompt_project, kind):
    from core.short_video.v2.exporter import ExportService
    from core.short_video.v2.workflow import WorkflowService

    repository = prompt_project
    first = next(record for record in repository.versions() if record["kind"] == kind)
    previous_bundle = repository.current("prompt_bundle")
    content = deepcopy(first["content"])
    content["views"][0]["positive_prompt"] += "，光线柔和"
    latest = repository.save(kind, content, first["dependency_refs"], parent_id=first["id"],
                             expected_revision=repository.load()["revision"])
    assert latest["parent_id"] is None
    assert len(repository.versions(first["key"])) == 1
    assert not (repository.root / "artifacts" / f"{first['id']}.json").exists()
    assert not (repository.root / "artifacts" / f"{previous_bundle['id']}.json").exists()
    assert repository.current("prompt_bundle") is None
    rebuilt = WorkflowService(repository).compile_storyboard_prompts(expected_revision=repository.load()["revision"])
    assert latest["id"] in rebuilt["content"]["asset_spec_refs"]
    assert len(repository.versions("prompt_bundle")) == 1
    assert len(repository.versions("storyboard")) == 1
    result = ExportService(repository).export(repository.root / "exports" / "prompts.md", keys=["prompts"])
    assert "光线柔和" in Path(result["path"]).read_text(encoding="utf-8")
    with pytest.raises(WorkspaceError, match="不支持恢复"):
        repository.select(latest["id"], expected_revision=repository.load()["revision"])


def test_prompt_agent_retries_keep_only_final_result_and_do_not_replay_old_results(prompt_project, monkeypatch):
    from core.short_video.v2.models import PROMPT_KINDS
    from core.short_video.v2.review import QualityCheckResult
    from core.short_video.v2.workflow import WorkflowService
    import core.short_video.v2.workflow as workflow_module

    repository = prompt_project
    original = repository.current("prop_spec:asset_prop:base")
    snapshot = {"provider": "offline", "model": "offline"}
    monkeypatch.setattr(workflow_module, "capture_model_snapshot", lambda provider: snapshot)
    monkeypatch.setattr(workflow_module, "validate_model_snapshot", lambda value: None)
    calls, checked = [], []

    def send(prompt):
        calls.append(prompt)
        value = deepcopy(original["content"])
        value["views"][0]["positive_prompt"] += f"，第{len(calls)}次生成"
        return json.dumps(value, ensure_ascii=False)

    def check(command, candidate):
        checked.append(candidate["id"])
        return QualityCheckResult(len(checked) == 2, "请改善主体细节")

    service = WorkflowService(repository, transport=send, quality_check=check)
    command = service.prepare("asset_spec", entity_id="asset_prop", variant_id="base", kind="prop_spec")
    result = service.execute(command)
    current = repository.current(original["key"])
    assert current["id"] == result["version_id"]
    assert "第2次生成" in current["content"]["views"][0]["positive_prompt"]
    assert len(repository.versions(original["key"])) == 1
    assert checked[0] not in repository.load()["records"]
    assert service.execute(command)["version_id"] == result["version_id"]
    assert len(calls) == 2
    run = repository.load()["runs"][-1]
    assert run["output_refs"] == [current["id"]]
    assert "command" not in run and "input_refs" not in run["snapshot"]
    replacement = repository.save(current["kind"], current["content"], current["dependency_refs"],
                                  expected_revision=repository.load()["revision"])
    with pytest.raises(WorkspaceError) as error:
        service.execute(command)
    assert error.value.code == "PROMPT_REPLACED" and len(calls) == 2
    assert repository.current(original["key"])["id"] == replacement["id"]
    assert all(record["parent_id"] is None for record in repository.versions() if record["kind"] in PROMPT_KINDS)


def test_format_repairs_do_not_consume_the_quality_repair_rounds(prompt_project, monkeypatch):
    from core.short_video.v2.models import encode
    from core.short_video.v2.workflow import WorkflowService
    import core.short_video.v2.workflow as workflow_module

    monkeypatch.setattr(workflow_module, "capture_model_snapshot", lambda _: {"model": "offline"})
    monkeypatch.setattr(workflow_module, "validate_model_snapshot", lambda _: None)
    content = prompt_project.current("storyboard")["content"]
    calls = []

    def send(prompt):
        calls.append(prompt)
        position = (len(calls) - 1) % 4
        if position < 2:
            return "invalid JSON"
        if position == 2:
            return encode(content)
        if len(calls) < 12:
            return encode({"issues": [{"rule_id": "SHOT-TIMING-001", "severity": "warning",
                "target_path": "shots.0.duration_ms", "explanation": "动作节奏需要调整",
                "suggestion": "结合动作重新安排节奏", "quote": None, "source_refs": []}]})
        return '{"issues":[]}'

    service = WorkflowService(prompt_project, transport=send)
    result = service.execute(service.prepare("storyboard"))

    assert len(calls) == 12
    assert result["review_status"] == "passed"
    assert result["retries_used"] == 2
    assert "budget" not in prompt_project.load()["runs"][-1]["snapshot"]


def test_invalid_output_still_stops_after_finite_format_repairs(prompt_project, monkeypatch):
    from unittest.mock import Mock
    from core.short_video.v2.workflow import WorkflowService
    import core.short_video.v2.workflow as workflow_module

    monkeypatch.setattr(workflow_module, "capture_model_snapshot", lambda _: {"model": "offline"})
    monkeypatch.setattr(workflow_module, "validate_model_snapshot", lambda _: None)
    original = prompt_project.current("storyboard")["id"]
    send = Mock(return_value="invalid JSON")
    service = WorkflowService(prompt_project, transport=send)

    with pytest.raises(WorkspaceError, match="未取得可用稿件"):
        service.execute(service.prepare("storyboard"))

    assert send.call_count == 3
    assert prompt_project.current("storyboard")["id"] == original
    assert prompt_project.load()["runs"][-1]["status"] == "failed"


def test_cancelled_format_repair_stops_without_replacing_the_current_storyboard(prompt_project, monkeypatch):
    from core.generation.cancellation import CancelToken, GenerationCancelled
    from core.short_video.v2.workflow import WorkflowService
    import core.short_video.v2.workflow as workflow_module

    monkeypatch.setattr(workflow_module, "capture_model_snapshot", lambda _: {"model": "offline"})
    monkeypatch.setattr(workflow_module, "validate_model_snapshot", lambda _: None)
    original = prompt_project.current("storyboard")["id"]
    token = CancelToken()
    calls = []

    def send(prompt):
        calls.append(prompt)
        token.cancel()
        return "invalid JSON"

    service = WorkflowService(prompt_project, transport=send)
    with pytest.raises(GenerationCancelled):
        service.execute(service.prepare("storyboard"), token=token)

    assert len(calls) == 1
    assert prompt_project.current("storyboard")["id"] == original
    assert prompt_project.load()["runs"][-1]["status"] == "cancelled"


def test_creation_task_uses_request_counts_only_for_recording(prompt_project, monkeypatch):
    from unittest.mock import Mock
    from core.short_video.v2 import creation, workflow
    from core.short_video.v2.models import encode

    snapshot = {"provider": "hosted-llm", "model": "offline"}
    for module in (creation, workflow):
        monkeypatch.setattr(module, "capture_model_snapshot", lambda _: snapshot)
        monkeypatch.setattr(module, "validate_model_snapshot", lambda _: None)
    original = prompt_project.current("prop_spec:asset_prop:base")
    command = creation.prepare_prompt_task(prompt_project, "hosted-llm", asset_key=original["key"])
    assert "max_calls" not in command and "seconds" not in command
    send = Mock(side_effect=[encode(original["content"]), '{"issues":[]}'])
    work = creation.CreationWork(prompt_project, command, transport=send)

    result = work(lambda *args: None)

    assert result["status"] == "succeeded" and result["review_status"] == "passed"
    assert result["calls"] == 2
    assert work(lambda *args: None) == result
    assert send.call_count == 2


def test_legacy_exhausted_chunk_plan_can_resume_without_budget_checks(project, monkeypatch):
    from unittest.mock import Mock
    from core.short_video.v2 import batches, workflow
    from core.short_video.v2.models import encode, fingerprint

    snapshot = {"provider": "hosted-llm", "model": "offline"}
    for module in (batches, workflow):
        monkeypatch.setattr(module, "capture_model_snapshot", lambda _: snapshot)
        monkeypatch.setattr(module, "validate_model_snapshot", lambda _: None)
    content = novel(project)["content"]
    send = Mock(side_effect=['{"facts":[]}', encode(content), encode(content), '{"issues":[]}'])
    service = batches.ChunkService(project, transport=send)
    plan = service.create_plan()
    assert "max_calls" not in plan and "seconds" not in plan
    plan.update(max_calls=0, seconds=0, calls_used=100, status="failed")
    frozen = {key: plan[key] for key in (
        "project_id", "mode", "model", "model_snapshot", "input_refs", "max_calls", "seconds", "selected_before",
    )}
    frozen["chunks"] = [
        {key: chunk[key] for key in ("id", "source_id", "start", "end", "text")} for chunk in plan["chunks"]
    ]
    plan["input_hash"] = fingerprint(frozen)
    service.save_plan(plan)

    result = service.execute(plan["id"])

    assert result["status"] == "succeeded"
    assert result["review_status"] == "passed"
    assert service.load_plan(plan["id"])["calls_used"] == 104
    assert service.execute(plan["id"])["version_id"] == result["version_id"]
    assert send.call_count == 4


def test_interrupted_prompt_candidates_are_removed_without_losing_current_content(prompt_project):
    repository = prompt_project
    original = repository.current("prop_spec:asset_prop:base")
    with repository.generation_lock():
        run, _ = repository.start_run({"stage": "asset_spec", "request_id": "interrupted-prompts"},
                                      expected_revision=repository.load()["revision"], snapshot={})
        with repository.transaction() as state:
            candidate = repository._save(state, original["kind"], original["content"],
                                         original["dependency_refs"], select=False)
            state["runs"][-1]["output_refs"] = [candidate["id"]]
        assert candidate["id"] in repository.load()["records"]
    repository.recover()
    assert repository.current(original["key"])["id"] == original["id"]
    assert candidate["id"] not in repository.load()["records"]
    assert not (repository.root / "artifacts" / f"{candidate['id']}.json").exists()
    assert repository.load()["runs"][-1]["status"] == "interrupted"
    before = repository.path.read_bytes()
    repository.recover()
    assert repository.path.read_bytes() == before


def test_failed_prompt_replacement_commit_keeps_original_files(prompt_project, monkeypatch):
    repository = prompt_project
    original = repository.current("prop_spec:asset_prop:base")
    original_manifest = repository.path.read_bytes()
    actual_atomic = repository_module.atomic_write_text

    def reject_manifest(path, value):
        if path == repository.path:
            raise OSError("disk full")
        actual_atomic(path, value)

    monkeypatch.setattr(repository_module, "atomic_write_text", reject_manifest)
    with pytest.raises(OSError):
        repository.save(original["kind"], original["content"], original["dependency_refs"],
                        expected_revision=repository.load()["revision"])
    assert repository.path.read_bytes() == original_manifest
    assert repository.record(original["id"])["content"] == original["content"]


def test_current_prompts_remain_usable_after_config_changes(prompt_project):
    from core.short_video.v2.exporter import ExportService

    repository = prompt_project
    config = repository.current("config")["content"]
    config["tone"] = "克制悬疑"
    ProjectService(repository).save_inputs(config, repository.current("source")["content"],
                                           expected_revision=repository.load()["revision"])
    assert not repository.is_current(repository.current("storyboard")["id"])
    assert repository.is_current(repository.current("prop_spec:asset_prop:base")["id"])
    assert repository.is_current(repository.current("prompt_bundle")["id"])
    assert not ExportService(repository).snapshot(keys=["prompts"])["problems"]


def test_prompt_generation_conflict_keeps_newer_manual_edit(prompt_project, monkeypatch):
    from core.short_video.v2.review import QualityCheckResult
    from core.short_video.v2.workflow import WorkflowService
    import core.short_video.v2.workflow as workflow_module

    repository = prompt_project
    original = repository.current("prop_spec:asset_prop:base")
    monkeypatch.setattr(workflow_module, "capture_model_snapshot", lambda provider: {"provider": "offline", "model": "offline"})
    monkeypatch.setattr(workflow_module, "validate_model_snapshot", lambda value: None)

    def check(command, candidate):
        content = deepcopy(original["content"])
        content["views"][0]["positive_prompt"] += "，保留人工修改"
        repository.save(original["kind"], content, original["dependency_refs"],
                        expected_revision=repository.load()["revision"])
        return QualityCheckResult(True)

    service = WorkflowService(repository, transport=lambda prompt: json.dumps(original["content"]), quality_check=check)
    command = service.prepare("asset_spec", entity_id="asset_prop", variant_id="base", kind="prop_spec")
    with pytest.raises(WorkspaceError) as error:
        service.execute(command)
    assert error.value.code == "TARGET_VERSION_CONFLICT"
    assert len(repository.versions(original["key"])) == 1
    assert "保留人工修改" in repository.current(original["key"])["content"]["views"][0]["positive_prompt"]
    assert repository.load()["runs"][-1]["status"] == "failed"


def test_storyboard_skill_is_frozen_and_timing_feedback_reaches_retry(prompt_project, monkeypatch):
    from core.short_video.v2.models import encode
    from core.short_video.v2.workflow import WorkflowService
    import core.short_video.v2.workflow as workflow_module

    repository = prompt_project
    monkeypatch.setattr(workflow_module, "capture_model_snapshot", lambda _: {"model": "offline"})
    monkeypatch.setattr(workflow_module, "validate_model_snapshot", lambda _: None)
    base = repository.current("storyboard")["content"]["shots"][0]
    first, revised = {"shots": []}, {"shots": []}
    actions = ["靠近井口", "俯看井口", "听见声音后停住", "看向铜钥匙"]
    for index, duration in enumerate([2000, 4500, 7000, 1500]):
        shot = {**deepcopy(base), "shot_id": f"s{index}", "action": actions[index],
                "narration": "井口有声音" if index == 1 else "", "duration_ms": 10000}
        first["shots"].append(shot)
        revised["shots"].append({**deepcopy(shot), "duration_ms": duration})
    calls = []
    feedback = "第一镜只需完成靠近井口，缩短后把旁白安排在俯看井口的镜头。"

    def send(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return encode(first)
        if len(calls) == 2:
            task = json.loads(prompt[prompt.index("\n{") + 1:])
            assert task["task"]["requirements"]["instruction"] == command["requirements"]["instruction"]
            assert any(i["rule_id"] == "SHOT-004" for i in task["code_issues"])
            return encode({"issues": [{"rule_id": "SHOT-TIMING-001", "severity": "warning",
                "target_path": "shots.0.duration_ms", "explanation": "短动作被拉长",
                "suggestion": feedback, "quote": None, "source_refs": []}]})
        if len(calls) == 3:
            assert prompt.startswith(command["prompt"])
            assert feedback in prompt and '"duration_ms":10000' in prompt.replace(" ", "")
            return encode(revised)
        assert len(calls) == 4
        return '{"issues":[]}'

    service = WorkflowService(repository, transport=send)
    command = service.prepare("storyboard")
    assert "short-video-storyboard" in command["requirements"]["instruction"]
    monkeypatch.setattr(workflow_module, "load_storyboard_skill",
                        lambda: pytest.fail("执行与重试不能重新加载已冻结的Skill"))
    result = service.execute(command)
    shots = repository.record(result["version_id"])["content"]["shots"]
    assert result["review_status"] == "passed" and result["retries_used"] == 1
    assert [s["duration_ms"] for s in shots] == [2000, 4500, 7000, 1500]
    assert [(s["start_ms"], s["end_ms"]) for s in shots] == [
        (0, 2000), (2000, 6500), (6500, 13500), (13500, 15000)]
    assert len(calls) == 4


def test_storyboard_equal_short_shots_can_pass_without_forced_retiming(prompt_project, monkeypatch):
    from core.short_video.v2.models import encode
    from core.short_video.v2.workflow import WorkflowService
    import core.short_video.v2.workflow as workflow_module

    repository = prompt_project
    monkeypatch.setattr(workflow_module, "capture_model_snapshot", lambda _: {"model": "offline"})
    monkeypatch.setattr(workflow_module, "validate_model_snapshot", lambda _: None)
    base = repository.current("storyboard")["content"]["shots"][0]
    content = {"shots": [{**deepcopy(base), "shot_id": f"s{i}", "duration_ms": 1500,
                          "narration": "", "dialogue": []} for i in range(4)]}
    responses = iter([encode(content), '{"issues":[]}'])
    service = WorkflowService(repository, transport=lambda _: next(responses))
    result = service.execute(service.prepare("storyboard"))
    assert result["review_status"] == "passed" and result["retries_used"] == 0
    record = repository.current("storyboard")
    assert [s["duration_ms"] for s in record["content"]["shots"]] == [1500] * 4
    # A manual save also accepts short shots without a mandatory exception reason.
    saved = repository.save("storyboard", record["content"], record["dependency_refs"],
                            expected_revision=repository.load()["revision"])
    assert saved["content"]["shots"][-1]["end_ms"] == 6000


def test_storyboard_review_uses_project_duration_range(prompt_project):
    from core.short_video.v2.review import build_review_prompt

    record = prompt_project.current("storyboard")  # 7 seconds, inside the old default range.
    prompt = build_review_prompt(ProjectService(prompt_project).context(), record,
                                task={"context": {"config": {"shot_duration_range_ms": [1000, 4000]}}})
    data = json.loads(prompt[prompt.index("\n{") + 1:])
    assert [i["rule_id"] for i in data["code_issues"]] == ["SHOT-003"]
    assert data["code_issues"][0]["target_path"] == "shots.0.duration_ms"


@pytest.fixture
def prompt_page(prompt_project):
    import os
    from types import SimpleNamespace

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.pages.short_video.prompt_page import PromptPage

    app = QApplication.instance() or QApplication([])
    started = []
    workbench = SimpleNamespace(repository=prompt_project, output_dir=prompt_project.output_dir,
        host=SimpleNamespace(runner=SimpleNamespace(busy=False)), configure_comfy=lambda: None,
        perform=lambda operation: operation(),
        resolve_page=lambda page: page.save() if page.dirty else True, export=lambda: None,
        start_creation=lambda stage, button, **kwargs: started.append((stage, kwargs)))
    page = PromptPage(workbench)
    workbench.refresh = page.refresh
    yield page, started
    page.close()
    page.deleteLater()
    app.processEvents()


def test_missing_shot_bundle_shows_copyable_drafts_and_can_start_generation(prompt_project, prompt_page):
    from PySide6.QtWidgets import QApplication, QPushButton

    repository = prompt_project
    page, started = prompt_page
    content = deepcopy(repository.current("storyboard")["content"])
    content["shots"].append({**deepcopy(content["shots"][0]), "shot_id": "shot_2",
                             "visual_prompt_draft": "井口的铜钥匙特写"})
    repository.save("storyboard", content, repository.current("storyboard")["dependency_refs"],
                    expected_revision=repository.load()["revision"])
    with repository.transaction() as state:
        state["current"].pop("scene_spec:asset_scene:base")
    assert repository.current("prompt_bundle") is None
    before = repository.path.read_bytes()
    page.tabs.setCurrentIndex(3)
    assert page.objects.count() == 2
    assert page.editor.positive.toPlainText() == content["shots"][0]["visual_prompt_draft"]
    assert page.editor.positive.isReadOnly() and page.editor.positive.isEnabled()
    assert not page.save_button.isEnabled() and "缺少 1 项" in page.status.text()
    page.objects.setCurrentIndex(1)
    assert page.editor.positive.toPlainText() == "井口的铜钥匙特写"
    copy = next(button for button in page.findChildren(QPushButton) if button.text() == "复制正向")
    copy.click()
    assert QApplication.clipboard().text() == "井口的铜钥匙特写"
    page.generate_button.click()
    assert started == [("prompts", {"asset_key": None})]
    assert not page.dirty and repository.path.read_bytes() == before


def test_shot_page_switches_from_draft_to_compiled_prompt_after_assets_complete(prompt_project, prompt_page):
    from core.short_video.v2.workflow import WorkflowService

    repository = prompt_project
    page, _ = prompt_page
    with repository.transaction() as state:
        state["current"].pop("prompt_bundle")
    page.tabs.setCurrentIndex(3)
    assert page.editor.positive.isReadOnly() and page.record is None
    bundle = WorkflowService(repository).compile_storyboard_prompts(
        expected_revision=repository.load()["revision"])
    page.refresh()
    assert page.record["id"] == bundle["id"]
    assert page.editor.positive.toPlainText() == bundle["content"]["shots"][0]["resolved_prompt"]
    assert not page.editor.positive.isReadOnly() and page.save_button.isEnabled()
    assert page.generate_button.text() == "重新生成"
    page.editor.positive.setPlainText("完整镜头画面的新编辑")
    assert page.save()
    assert repository.current("prompt_bundle")["content"]["shots"][0]["resolved_prompt"] == "完整镜头画面的新编辑"


def test_shot_page_does_not_show_bundle_from_another_storyboard(prompt_project, prompt_page):
    repository = prompt_project
    page, _ = prompt_page
    old_bundle = repository.current("prompt_bundle")
    board = repository.current("storyboard")
    content = deepcopy(board["content"])
    content["shots"][0]["visual_prompt_draft"] = "当前分镜的井口全景"
    repository.save("storyboard", content, board["dependency_refs"],
                    expected_revision=repository.load()["revision"])
    page.tabs.setCurrentIndex(3)
    assert page.editor.positive.toPlainText() == "当前分镜的井口全景"
    assert page.record is None and page.editor.positive.isReadOnly()
    assert repository.current("prompt_bundle")["id"] == old_bundle["id"]
