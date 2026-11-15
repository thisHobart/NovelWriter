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
import core.short_video.v2.runtime as runtime_module


ARTICLE = "\ufeff乡村😀老宅\r\n井口响了三声。\r父亲留下铜钥匙。"


def current_records(repository, key=None):
    state = repository.load()
    return [repository.record(ref, state=state) for name, ref in state["current"].items()
            if key is None or name == key]


def rebuild_with_config(repository, config):
    """Explicitly regenerate the fixture pipeline after its source config is replaced."""
    old = current_records(repository)
    saved = repository.save("config", config, {}, expected_revision=repository.load()["revision"])
    replacements = {r["id"]: r["id"] for r in current_records(repository)}
    replacements.update({r["id"]: saved["id"] for r in old if r["key"] == "config"})

    def update(value):
        if isinstance(value, str):
            return replacements.get(value, value)
        if isinstance(value, list):
            return [update(item) for item in value]
        if isinstance(value, dict):
            return {key: update(item) for key, item in value.items()}
        return value

    for record in old:
        if record["key"] in repository.load()["current"]:
            continue
        result = repository.save(record["kind"], update(record["content"]),
                                 update(record["dependency_refs"]), key=record["key"],
                                 expected_revision=repository.load()["revision"])
        replacements[record["id"]] = result["id"]


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


def test_config_replacement_removes_dependent_content(project):
    first = novel(project)
    video = project.save("video_copy", {"blocks": [
        {"id": "b1", "role": "opening", "narration": "井口有声音", "visible_action": "镜头靠近井口", "dialogue": []},
        {"id": "b2", "role": "cliffhanger", "narration": "是谁？", "visible_action": "", "dialogue": []}]},
        {"novel_copy": first["id"]}, expected_revision=project.load()["revision"])
    assert project.is_current(video["id"])
    cfg = project.current("config")["content"]
    cfg["tracks"]["backgrounds"] = ["乡村"]
    ProjectService(project).save_inputs(cfg, project.current("source")["content"], expected_revision=project.load()["revision"])
    assert project.current("novel_copy") is None and project.current("video_copy") is None
    assert first["id"] not in project.load()["records"]
    assert not (project.root / "artifacts" / f"{video['id']}.json").exists()


def test_manual_save_keeps_only_current_content(project):
    first = novel(project)
    changed = deepcopy(first["content"])
    changed["opening"]["text"] = "这口井藏着什么？"
    second = project.save("novel_copy", changed, first["dependency_refs"],
                          expected_revision=project.load()["revision"])
    assert "parent_id" not in second
    assert project.current("novel_copy")["id"] == second["id"]
    assert first["id"] not in project.load()["records"]
    assert not (project.root / "artifacts" / f"{first['id']}.json").exists()


def test_conflicting_save_and_failed_commit_do_not_lose_old_state(project, monkeypatch):
    first = novel(project)
    old_revision = project.load()["revision"]
    novel(project)
    before = project.path.read_bytes()
    with pytest.raises(WorkspaceError) as error:
        project.save("novel_copy", first["content"], first["dependency_refs"], expected_revision=old_revision)
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
    assert len(current_records(project, "novel_copy")) == 1
    monkeypatch.setattr(repository_module, "atomic_write_text", actual_atomic)
    project.recover()
    assert {path.stem for path in (project.root / "artifacts").glob("*.json")} == set(project.load()["records"])


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
    first = next(record for record in current_records(repository) if record["kind"] == kind)
    previous_bundle = repository.current("prompt_bundle")
    content = deepcopy(first["content"])
    content["views"][0]["positive_prompt"] += "，光线柔和"
    latest = repository.save(kind, content, first["dependency_refs"],
                             expected_revision=repository.load()["revision"])
    assert "parent_id" not in latest
    assert len(current_records(repository, first["key"])) == 1
    assert not (repository.root / "artifacts" / f"{first['id']}.json").exists()
    assert not (repository.root / "artifacts" / f"{previous_bundle['id']}.json").exists()
    assert repository.current("prompt_bundle") is None
    rebuilt = WorkflowService(repository).compile_storyboard_prompts(expected_revision=repository.load()["revision"])
    assert latest["id"] in rebuilt["content"]["asset_spec_refs"]
    assert len(current_records(repository, "prompt_bundle")) == 1
    assert len(current_records(repository, "storyboard")) == 1
    result = ExportService(repository).export(repository.root / "exports" / "prompts.md", keys=["prompts"])
    assert "光线柔和" in Path(result["path"]).read_text(encoding="utf-8")
    assert not hasattr(repository, "select")


def test_prompt_agent_retries_keep_only_final_result_and_do_not_replay_old_results(prompt_project, monkeypatch):
    from core.short_video.v2.review import QualityCheckResult
    from core.short_video.v2.workflow import WorkflowService
    import core.short_video.v2.workflow as workflow_module

    repository = prompt_project
    original = repository.current("prop_spec:asset_prop:base")
    snapshot = {"provider": "offline", "model": "offline"}
    monkeypatch.setattr(workflow_module, "capture_model_snapshot", lambda provider: snapshot)
    monkeypatch.setattr(runtime_module, "validate_model_snapshot", lambda value: None)
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
    assert len(current_records(repository, original["key"])) == 1
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
    assert error.value.code == "OUTPUT_REPLACED" and len(calls) == 2
    assert repository.current(original["key"])["id"] == replacement["id"]
    assert all("parent_id" not in record for record in current_records(repository))


def test_format_repairs_do_not_consume_the_quality_repair_rounds(prompt_project, monkeypatch):
    from core.short_video.v2.models import encode
    from core.short_video.v2.workflow import WorkflowService
    import core.short_video.v2.workflow as workflow_module

    monkeypatch.setattr(workflow_module, "capture_model_snapshot", lambda _: {"model": "offline"})
    monkeypatch.setattr(runtime_module, "validate_model_snapshot", lambda _: None)
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
    monkeypatch.setattr(runtime_module, "validate_model_snapshot", lambda _: None)
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
    monkeypatch.setattr(runtime_module, "validate_model_snapshot", lambda _: None)
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
    assert prompt_project.load()["runs"][-1]["status"] == "paused"


def test_creation_task_uses_request_counts_only_for_recording(prompt_project, monkeypatch):
    from unittest.mock import Mock
    from core.short_video.v2 import creation, workflow
    from core.short_video.v2.models import encode

    snapshot = {"provider": "hosted-llm", "model": "offline"}
    for module in (creation, workflow):
        monkeypatch.setattr(module, "capture_model_snapshot", lambda _: snapshot)
        monkeypatch.setattr(runtime_module, "validate_model_snapshot", lambda _: None)
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
        monkeypatch.setattr(runtime_module, "validate_model_snapshot", lambda _: None)
    monkeypatch.setattr(batches, "validate_model_snapshot", lambda _: None)
    content = novel(project)["content"]
    custom_instruction = "按我的简短口语风格整理素材，不编造结果。"
    project.save("prompt_template", {"stage": "novel_copy", "instruction": custom_instruction},
                 {}, key="prompt_template:novel_copy", expected_revision=project.load()["revision"])
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
    assert all(custom_instruction in send.call_args_list[index].args[0] for index in (1, 2))


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


def test_config_changes_remove_old_prompts_instead_of_exporting_them(prompt_project):
    from core.short_video.v2.exporter import ExportService

    repository = prompt_project
    config = repository.current("config")["content"]
    config["tone"] = "克制悬疑"
    ProjectService(repository).save_inputs(config, repository.current("source")["content"],
                                           expected_revision=repository.load()["revision"])
    assert repository.current("storyboard") is None
    assert repository.current("prop_spec:asset_prop:base") is None
    assert repository.current("prompt_bundle") is None
    with pytest.raises(WorkspaceError, match="缺少内容"):
        ExportService(repository).snapshot(keys=["prompts"])


def test_prompt_generation_conflict_keeps_newer_manual_edit(prompt_project, monkeypatch):
    from core.short_video.v2.review import QualityCheckResult
    from core.short_video.v2.workflow import WorkflowService
    import core.short_video.v2.workflow as workflow_module

    repository = prompt_project
    original = repository.current("prop_spec:asset_prop:base")
    monkeypatch.setattr(workflow_module, "capture_model_snapshot", lambda provider: {"provider": "offline", "model": "offline"})
    monkeypatch.setattr(runtime_module, "validate_model_snapshot", lambda value: None)

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
    assert len(current_records(repository, original["key"])) == 1
    assert "保留人工修改" in repository.current(original["key"])["content"]["views"][0]["positive_prompt"]
    assert repository.load()["runs"][-1]["status"] == "invalidated"


def test_storyboard_skill_is_frozen_and_timing_feedback_reaches_retry(prompt_project, monkeypatch):
    from core.short_video.v2.models import encode
    from core.short_video.v2.workflow import WorkflowService
    import core.short_video.v2.workflow as workflow_module

    repository = prompt_project
    config = deepcopy(repository.current("config")["content"])
    config["storyboard_skill_id"] = "short-video-content-storyboard"
    rebuild_with_config(repository, config)
    monkeypatch.setattr(workflow_module, "capture_model_snapshot", lambda _: {"model": "offline"})
    monkeypatch.setattr(runtime_module, "validate_model_snapshot", lambda _: None)
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
    assert command["requirements"]["skill_id"] == "short-video-content-storyboard"
    assert "short-video-content-storyboard" in command["requirements"]["instruction"]
    monkeypatch.setattr(workflow_module, "load_storyboard_skill",
                        lambda *_: pytest.fail("执行与重试不能重新加载已冻结的Skill"))
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
    monkeypatch.setattr(runtime_module, "validate_model_snapshot", lambda _: None)
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


def test_storyboard_skill_requires_an_explicit_choice(prompt_project, monkeypatch):
    from core.short_video.v2.review import build_review_prompt
    from core.short_video.v2.workflow import WorkflowService
    import core.short_video.v2.workflow as workflow_module

    monkeypatch.setattr(workflow_module, "capture_model_snapshot", lambda _: {"model": "offline"})
    monkeypatch.setattr(workflow_module, "load_storyboard_skill",
                        lambda *_: pytest.fail("未选择或其他阶段不能加载Skill"))
    before = prompt_project.path.read_bytes()
    service = WorkflowService(prompt_project)
    command = service.prepare("storyboard")
    assert command["requirements"]["skill_id"] is None
    assert "MiniMax H3" not in command["requirements"]["instruction"]
    assert "5–8秒" not in command["requirements"]["instruction"]
    review = build_review_prompt(ProjectService(prompt_project).context(),
                                prompt_project.current("storyboard"),
                                task={"requirements": command["requirements"]})
    assert "MiniMax H3" not in review and "5–8秒" not in review
    assert prompt_project.path.read_bytes() == before

    config = deepcopy(prompt_project.current("config")["content"])
    config["storyboard_skill_id"] = "short-video-content-storyboard"
    rebuild_with_config(prompt_project, config)
    for stage in ("novel_copy", "video_copy", "asset_catalog"):
        other = service.prepare(stage)
        assert other["requirements"]["skill_id"] is None


def test_legacy_config_defaults_to_no_skill_without_mutation():
    from core.short_video.v2.models import validate_config

    legacy = default_config()
    legacy.pop("storyboard_skill_id")
    before = deepcopy(legacy)
    assert validate_config(legacy)["storyboard_skill_id"] is None
    assert legacy == before
    for invalid in ("unknown-skill", "../storyboard", ["short-video-content-storyboard"]):
        with pytest.raises(WorkspaceError) as error:
            validate_config({**legacy, "storyboard_skill_id": invalid})
        assert error.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("skill_id", [None, "short-video-content-storyboard"])
def test_storyboard_creation_task_uses_only_selected_skill(prompt_project, monkeypatch, skill_id):
    from core.short_video.v2 import creation, workflow
    from core.short_video.v2.models import encode
    from core.short_video.v2.storyboard_skill import load_storyboard_skill

    snapshot = {"provider": "hosted-llm", "model": "offline"}
    for module in (creation, workflow):
        monkeypatch.setattr(module, "capture_model_snapshot", lambda _: snapshot)
        monkeypatch.setattr(runtime_module, "validate_model_snapshot", lambda _: None)
    config = deepcopy(prompt_project.current("config")["content"])
    config["storyboard_skill_id"] = skill_id
    rebuild_with_config(prompt_project, config)
    catalog = prompt_project.current("asset_catalog")["content"]
    board = prompt_project.current("storyboard")["content"]
    skill_text = load_storyboard_skill("short-video-content-storyboard")
    prompts = []
    responses = iter([encode(catalog), '{"issues":[]}', encode(board), '{"issues":[]}'])

    def send(prompt):
        prompts.append(prompt)
        return next(responses)

    command = creation.prepare_storyboard_task(prompt_project, "hosted-llm")
    assert command["first_command"]["requirements"]["skill_id"] is None
    result = creation.CreationWork(prompt_project, command, transport=send)(lambda *_: None)
    assert result["review_status"] == "passed" and len(prompts) == 4
    assert skill_text not in prompts[0]
    for prompt in prompts[2:]:
        # Review embeds the frozen instruction as an encoded JSON string.
        has_skill = skill_text in prompt or json.dumps(skill_text, ensure_ascii=False)[1:-1] in prompt
        assert has_skill == (skill_id is not None)


@pytest.fixture
def source_page(project):
    import os
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from core.gui.pages.short_video.source_page import SourcePage

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    started, notices = [], []
    workbench = SimpleNamespace(repository=project, model=lambda: "hosted-llm",
        perform=lambda operation: operation(), notice=notices.append,
        start_creation=lambda stage, button: started.append(
            (stage, project.current("config")["content"].get("storyboard_skill_id"))))
    page = SourcePage(workbench)
    workbench.refresh = page.refresh
    page.refresh()
    yield page, started, notices, app
    page.close()


def test_source_page_skill_selection_roundtrip_preserves_source_and_legacy_settings(project, source_page):
    from PySide6.QtWidgets import QLineEdit, QPushButton

    page, started, _, app = source_page
    before = project.path.read_bytes()
    assert page.skill_combo.currentData() is None
    assert page.skill_combo.count() == 2
    assert not page.findChildren(QLineEdit)
    assert not any(b.text() == "更多创作要求" for b in page.findChildren(QPushButton))
    page.refresh()
    app.processEvents()
    assert not page.dirty and started == []
    assert project.path.read_bytes() == before

    config = deepcopy(project.current("config")["content"])
    config.update(title="保留项目名称", target_duration_ms=45000, required=["保留铜钥匙"])
    config["tracks"]["backgrounds"] = ["乡村"]
    project.save("config", config, {}, expected_revision=project.load()["revision"])
    page.refresh()
    source_id, selection_id = (project.current(k)["id"] for k in ("source", "selection"))
    page.skill_combo.setCurrentIndex(1)
    assert page.dirty
    page.save_button.click()
    saved = project.current("config")["content"]
    assert saved == {**config, "storyboard_skill_id": "short-video-content-storyboard"}
    assert project.current("source")["id"] == source_id
    assert project.current("selection")["id"] == selection_id
    assert not page.dirty and page.skill_combo.currentIndex() == 1

    page.skill_combo.setCurrentIndex(0)
    page.save_button.click()
    assert project.current("config")["content"]["storyboard_skill_id"] is None
    assert started == []


def test_source_page_saves_explicit_skill_before_starting_adaptation(project, source_page):
    page, started, _, _ = source_page
    page.article.setPlainText("井边的人举起铜钥匙，转身走进老宅。")
    page.skill_combo.setCurrentIndex(1)
    page.generate_button.click()
    assert started == [("novel_copy", "short-video-content-storyboard")]
    assert project.current("source")["content"]["normalized_text"] == page.article.toPlainText()
    assert not page.dirty


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
    assert repository.current("prompt_bundle") is None
    assert old_bundle["id"] not in repository.load()["records"]


def test_closed_event_pipeline_preserves_ending_voice_only_and_export(project, monkeypatch, tmp_path):
    from core.short_video.v2.exporter import ExportService
    from core.short_video.v2.models import encode
    from core.short_video.v2.workflow import WorkflowService
    import core.short_video.v2.workflow as workflow_module

    article = "小林发现文件夹落在门口，把它拿回书桌。母亲在电话里提醒他带钥匙，他拿起钥匙离开。"
    ProjectService(project).import_text(article, expected_revision=project.load()["revision"])
    refs = ProjectService(project).context()["source_refs"]
    monkeypatch.setattr(workflow_module, "capture_model_snapshot", lambda _: {"model": "offline"})
    monkeypatch.setattr(runtime_module, "validate_model_snapshot", lambda _: None)
    copies = {
        "novel_copy": {
            "opening": {"text": "小林发现文件夹落在门口。", "source_refs": refs},
            "paragraphs": [{"id": "p1", "text": "他把文件夹拿回书桌，接到母亲提醒带钥匙的电话。", "source_refs": refs}],
            "cliffhanger": {"text": "他拿起钥匙离开。", "source_refs": refs}, "changes": [],
        },
        "video_copy": {"blocks": [
            {"id": "b1", "role": "opening", "narration": "", "dialogue": [],
             "visible_action": "小林捡起门口文件夹，放回书桌。", "novel_paragraph_refs": ["opening", "p1"], "source_refs": refs},
            {"id": "b2", "role": "body", "narration": "", "visible_action": "小林看向钥匙。",
             "dialogue": [{"speaker_label": "母亲", "text": "出门别忘钥匙。", "voice_over": True}],
             "novel_paragraph_refs": ["p1"], "source_refs": refs},
            {"id": "b3", "role": "cliffhanger", "narration": "", "dialogue": [],
             "visible_action": "小林拿起钥匙离开。", "novel_paragraph_refs": ["cliffhanger"], "source_refs": refs},
        ], "changes": []},
        "asset_catalog": {"entities": [
            {"asset_id": f"draft_{i}", "kind": kind, "name": name, "aliases": [],
             "usage": usage, "variants": [{"variant_id": "base", "label": "当前状态"}],
             "source_refs": refs, "inferred_fields": []}
            for i, (kind, name, usage) in enumerate([
                ("character", "小林", "on_screen"), ("character", "母亲", "voice_only"),
                ("scene", "书桌旁", "on_screen"), ("prop", "文件夹", "on_screen"), ("prop", "钥匙", "on_screen"),
            ])
        ]},
    }
    calls = []

    def send(prompt):
        calls.append(prompt)
        if prompt.startswith("自动审阅"):
            payload = json.loads(prompt[prompt.index("\n{") + 1:])
            assert payload["task"]["requirements"] == command["requirements"]
            return '{"issues":[]}'
        if stage != "storyboard":
            return encode(copies[stage])
        entities = {e["name"]: e for e in project.current("asset_catalog")["content"]["entities"]}

        def ref(name):
            return {"asset_id": entities[name]["asset_id"], "variant_id": "base"}

        return encode({"shots": [
            {"shot_id": f"s{i}", "duration_ms": duration, "shot_size": "中景", "camera_movement": "固定",
             "scene_ref": ref("书桌旁"), "characters": [ref("小林")], "props": [ref(prop)],
             "action": block["visible_action"], "visual_prompt_draft": block["visible_action"],
             "narration": "", "sound": "", "dialogue": [
                 {"character_id": entities[line["speaker_label"]]["asset_id"], "text": line["text"], "voice_over": True}
                 for line in block["dialogue"]], "video_block_refs": [block["id"]], "source_refs": refs}
            for i, (block, duration, prop) in enumerate(zip(copies["video_copy"]["blocks"], [6000, 6500, 3500], ["文件夹", "钥匙", "钥匙"]))
        ]})

    service = WorkflowService(project, transport=send)
    for stage in ("novel_copy", "video_copy", "asset_catalog", "storyboard"):
        command = service.prepare(stage)
        assert command["prompt_ref"] == f"{stage}-default-v3"
        result = service.execute(command)
        assert result["review_status"] == "passed"
    assert len(calls) == 8
    board = project.current("storyboard")["content"]
    mother = next(e for e in project.current("asset_catalog")["content"]["entities"] if e["name"] == "母亲")
    assert all(mother["asset_id"] not in [r["asset_id"] for r in s["characters"]] for s in board["shots"])
    assert board["shots"][1]["dialogue"][0]["voice_over"] is True
    assert board["shots"][-1]["end_ms"] == 16000
    assert project.current("novel_copy")["content"]["cliffhanger"]["text"] == "他拿起钥匙离开。"
    destination = tmp_path / "闭合事件.md"
    ExportService(project).export(destination, keys=["novel_copy", "video_copy", "storyboard"])
    assert "他拿起钥匙离开。" in destination.read_text(encoding="utf-8")


@pytest.mark.parametrize("duration,target,rules", [
    (59999, None, []), (60000, None, ["SHOT-TOTAL-001"]),
    (75000, 45000, ["SHOT-TOTAL-001", "SHOT-TARGET-002"]),
    (45000, 60000, ["SHOT-TARGET-001"]), (45000, 45000, []),
])
def test_total_duration_is_a_review_clue_not_a_save_gate(prompt_project, duration, target, rules):
    from core.short_video.v2.validation import local_issues

    board = prompt_project.current("storyboard")
    content = deepcopy(board["content"])
    content["shots"][0]["duration_ms"] = duration
    issues = local_issues("storyboard", content,
                          {"target_duration_ms": target, "shot_duration_range_ms": [1, 100000]})
    assert [i["rule_id"] for i in issues] == rules
    assert all(i["severity"] == "warning" for i in issues)
    saved = prompt_project.save("storyboard", content, board["dependency_refs"],
                                expected_revision=prompt_project.load()["revision"])
    assert saved["content"]["shots"][-1]["end_ms"] == duration


def test_missing_result_remains_unpassed_after_quality_retries(project, monkeypatch):
    from core.short_video.v2.models import encode
    from core.short_video.v2.workflow import WorkflowService
    import core.short_video.v2.workflow as workflow_module

    monkeypatch.setattr(workflow_module, "capture_model_snapshot", lambda _: {"model": "offline"})
    monkeypatch.setattr(runtime_module, "validate_model_snapshot", lambda _: None)
    content = novel(project)["content"]
    original = project.current("novel_copy")
    prompts = []

    def send(prompt):
        prompts.append(prompt)
        if prompt.startswith("自动审阅"):
            return encode({"issues": [{"rule_id": "TEXT-RESULT-001", "severity": "warning",
                "target_path": "cliffhanger.text", "explanation": "当前素材尚未给出井中声音的结果。",
                "suggestion": "保留原文停点并报告素材缺失，不补造结局。", "quote": None, "source_refs": []}]})
        return encode(content)

    result = WorkflowService(project, transport=send).execute(WorkflowService(project).prepare("novel_copy"))
    assert result["review_status"] == "issues" and result["retries_used"] == 2
    assert len(prompts) == 6
    assert project.current("novel_copy")["content"] == original["content"]
    assert original["id"] not in project.load()["records"]


def test_custom_stage_instruction_is_preserved_and_prepare_is_readonly(project, monkeypatch):
    from core.short_video.v2.workflow import WorkflowService
    import core.short_video.v2.workflow as workflow_module

    monkeypatch.setattr(workflow_module, "capture_model_snapshot", lambda _: {"model": "offline"})
    custom = project.save("prompt_template", {"stage": "novel_copy", "instruction": "保留我的简短口语风格。"},
                          {}, key="prompt_template:novel_copy", expected_revision=project.load()["revision"])
    before = project.path.read_bytes()
    command = WorkflowService(project).prepare("novel_copy")
    assert command["prompt_ref"] == custom["id"]
    assert command["requirements"]["instruction"] == custom["content"]["instruction"]
    assert custom["content"]["instruction"] in command["prompt"]
    assert project.path.read_bytes() == before


def test_storyboard_format_example_does_not_pack_the_whole_catalog(prompt_project):
    from core.short_video.v2.prompts import output_example

    context = {key: prompt_project.current(key) for key in ("asset_catalog", "video_copy")}
    context["article"] = ProjectService(prompt_project).context()
    extra = deepcopy(context["asset_catalog"]["content"]["entities"][0])
    extra.update(asset_id="unused_character", name="未出镜配角")
    context["asset_catalog"]["content"]["entities"].append(extra)
    shot = output_example("storyboard", context)["shots"][0]
    assert shot["characters"] == [] and shot["props"] == []
    assert shot["narration"] == ""
    assert shot["video_block_refs"] == [context["video_copy"]["content"]["blocks"][0]["id"]]


def test_compiled_shot_deduplicates_visible_text_without_changing_refs(prompt_project):
    from core.short_video.v2.prompt_compiler import compile_shots

    board = deepcopy(prompt_project.current("storyboard"))
    board["content"]["shots"][0]["visual_prompt_draft"] = "举起铜钥匙。"
    specs = [r for r in current_records(prompt_project) if r["kind"] in {"character_spec", "scene_spec", "prop_spec"}]
    original = compile_shots(prompt_project.current("storyboard"), specs)
    compiled = compile_shots(board, specs)
    shot = compiled["shots"][0]
    assert shot["resolved_prompt"].count("举起铜钥匙") == 1
    assert "不要求全部细节同时可见" in shot["resolved_prompt"]
    assert shot["asset_refs"] == original["shots"][0]["asset_refs"]
    assert shot["source_refs"] == original["shots"][0]["source_refs"]
    assert compiled["asset_spec_refs"] == original["asset_spec_refs"]


@pytest.mark.parametrize("failure_phase", ["生成", "格式修复", "自动审阅", "质量修订"])
def test_task_feedback_keeps_original_failure_and_actual_phase(prompt_project, monkeypatch, caplog, failure_phase):
    from core.short_video.v2 import workflow
    from core.short_video.v2.feedback import record_message, task_message
    from core.short_video.v2.models import encode
    from core.short_video.v2.review import QualityCheckResult

    monkeypatch.setattr(workflow, "capture_model_snapshot", lambda _: {"model": "offline"})
    monkeypatch.setattr(runtime_module, "validate_model_snapshot", lambda _: None)
    original = prompt_project.current("storyboard")
    error = TimeoutError("original transport failure")
    calls = []

    def send(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            if failure_phase == "生成":
                raise error
            if failure_phase == "格式修复":
                return "bad JSON"
            return encode(original["content"])
        raise error

    def check(command, candidate):
        if failure_phase == "自动审阅":
            raise error
        return QualityCheckResult(False, "镜头1：动作过多，请拆镜", 1)

    command = workflow.WorkflowService(prompt_project).prepare("storyboard")
    # Exercise the same service through the real worker, with a deterministic reviewer.
    monkeypatch.setattr(runtime_module, "check_candidate", lambda command, candidate, *args: check(command, candidate))
    work = workflow.WorkspaceWork(prompt_project, command, transport=send)
    with pytest.raises(TimeoutError) as caught:
        work(lambda *_: None)
    assert caught.value is error
    assert prompt_project.current("storyboard")["id"] == original["id"]
    feedback = prompt_project.load()["runs"][-1]["feedback"]
    assert work.task_context.phase == failure_phase
    assert work.task_context.resumable
    assert "继续生成" in task_message(work.task_context, error=error)[2]
    assert feedback["phase"] == failure_phase
    assert "请求超时" in feedback["cause"]


def test_review_feedback_manual_edit_and_source_replacement_are_distinct(prompt_project, monkeypatch):
    from core.short_video.v2 import workflow
    from core.short_video.v2.feedback import record_message
    from core.short_video.v2.models import encode
    from core.short_video.v2.review import QualityCheckResult

    monkeypatch.setattr(workflow, "capture_model_snapshot", lambda _: {"model": "offline"})
    monkeypatch.setattr(runtime_module, "validate_model_snapshot", lambda _: None)
    content = prompt_project.current("storyboard")["content"]
    service = workflow.WorkflowService(prompt_project, transport=lambda _: encode(content),
                                      quality_check=lambda *_: QualityCheckResult(False, "总时长超过60秒", 2))
    result = service.execute(service.prepare("storyboard"))
    board = prompt_project.current("storyboard")
    assert result["review_status"] == "issues"
    message = record_message(prompt_project, board, prompt_project.load())
    assert "总时长超过60秒" in message and "共2项" in message
    manual = prompt_project.save("storyboard", content, board["dependency_refs"],
                                 expected_revision=prompt_project.load()["revision"])
    message = record_message(prompt_project, manual, prompt_project.load())
    assert "尚未自动检查" in message and "总时长超过60秒" not in message
    config = deepcopy(prompt_project.current("config")["content"])
    config["audience"] = "另一个观众群"
    prompt_project.save("config", config, {}, expected_revision=prompt_project.load()["revision"])
    assert prompt_project.current("storyboard") is None
    assert board["id"] not in prompt_project.load()["records"]


def test_asset_batch_reports_object_partial_progress_and_original_429(prompt_project, monkeypatch):
    import httpx
    from openai import RateLimitError
    from core.short_video.v2 import creation, workflow
    from core.short_video.v2.feedback import task_message
    from core.short_video.v2.models import encode
    from core.short_video.v2.review import QualityCheckResult

    for module in (creation, workflow):
        monkeypatch.setattr(module, "capture_model_snapshot", lambda *_: {"model": "offline"})
        monkeypatch.setattr(runtime_module, "validate_model_snapshot", lambda *_: None)
    originals = {kind: prompt_project.current(f"{kind}_spec:asset_{kind}:base")["content"]
                 for kind in ("character", "scene", "prop")}
    with prompt_project.transaction() as state:
        for key in list(state["current"]):
            if "_spec:" in key:
                state["current"].pop(key)
    monkeypatch.setattr(runtime_module, "check_candidate", lambda *_: QualityCheckResult(True))
    response = httpx.Response(429, request=httpx.Request("POST", "https://example.test"))
    error = RateLimitError("Upstream rate limit exceeded", response=response, body={})

    def send(prompt):
        if "寻井人" in work.task_context.target:
            return encode(originals["character"])
        raise error

    work = creation.CreationWork(prompt_project, creation.prepare_prompt_task(prompt_project, "hosted-llm"),
                                 transport=send)
    with pytest.raises(RateLimitError) as caught:
        work(lambda *_: None)
    assert caught.value is error
    assert work.task_context.completed == 1 and work.task_context.total == 3
    severity, title, detail = task_message(work.task_context, error=error)
    assert severity == "warning" and title == "资产提示词生成中断"
    assert "老宅" in detail and "生成" in detail and "429" in detail and "1/3" in detail
    assert prompt_project.current("character_spec:asset_character:base") is not None


def test_asset_batch_keeps_both_incomplete_review_and_quality_issues(prompt_project, monkeypatch):
    from core.short_video.v2 import creation, workflow
    from core.short_video.v2.feedback import task_message
    from core.short_video.v2.models import encode
    from core.short_video.v2.review import QualityCheckResult

    for module in (creation, workflow):
        monkeypatch.setattr(module, "capture_model_snapshot", lambda *_: {"model": "offline"})
        monkeypatch.setattr(runtime_module, "validate_model_snapshot", lambda *_: None)
    names = {"寻井人": "character", "老宅": "scene", "铜钥匙": "prop"}
    originals = {kind: prompt_project.current(f"{kind}_spec:asset_{kind}:base")["content"]
                 for kind in names.values()}
    with prompt_project.transaction() as state:
        for key in list(state["current"]):
            if "_spec:" in key:
                state["current"].pop(key)

    def check(command, candidate, *args):
        kind = command["context"]["entity"]["kind"]
        if kind == "character":
            raise TimeoutError("review timeout")
        return QualityCheckResult(kind != "scene", "反向视角的门窗布局不一致", 1)

    def send(prompt):
        kind = next(kind for name, kind in names.items() if name in work.task_context.target)
        return encode(originals[kind])

    monkeypatch.setattr(runtime_module, "check_candidate", check)
    work = creation.CreationWork(prompt_project, creation.prepare_prompt_task(prompt_project, "hosted-llm"),
                                 transport=send)
    with pytest.raises(TimeoutError):
        work(lambda *_: None)
    task = next(iter(prompt_project.load()["tasks"].values()))
    assert task["status"] == "failed" and task["resumable"]
    assert work.task_context.completed == 0
    monkeypatch.setattr(runtime_module, "check_candidate", lambda command, candidate, *args:
                        QualityCheckResult(command["context"]["entity"]["kind"] != "scene", "反向视角的门窗布局不一致", 1))
    work = runtime_module.ResumeWork(prompt_project, task["id"], transport=send)
    result = work(lambda *_: None)
    assert result["review_status"] == "issues"
    assert "1项检查发现问题" in result["review_summary"] and "门窗布局不一致" in result["review_summary"]



@pytest.mark.parametrize("key", ["source", "selection", "config", "novel_copy", "video_copy", "asset_catalog", "storyboard", "prompt_template:storyboard"])
def test_creative_content_replacement_deletes_old_file(prompt_project, key):
    repository = prompt_project
    if key.startswith("prompt_template:"):
        repository.save("prompt_template", {"stage": "storyboard", "instruction": "按动作拆镜"}, {},
                        key=key, expected_revision=repository.load()["revision"])
    original = repository.current(key)
    replacement = repository.save(original["kind"], original["content"], original["dependency_refs"],
                                  key=key, expected_revision=repository.load()["revision"])
    state = repository.load()
    assert repository.current(key)["id"] == replacement["id"]
    assert original["id"] not in state["records"]
    assert not (repository.root / "artifacts" / f"{original['id']}.json").exists()
    assert "parent_id" not in replacement
    assert len([m for m in state["records"].values() if m["key"] == key]) == 1


def test_second_article_cannot_reuse_first_article_assets(prompt_project):
    repository = prompt_project
    previous_ids = set(repository.load()["records"])
    ProjectService(repository).import_text("第二份原文：雪山木屋里的新故事。", expected_revision=repository.load()["revision"])
    state = repository.load()
    assert set(state["current"]) == {"config", "source", "selection"}
    assert all(meta["kind"] not in {"character_spec", "scene_spec", "prop_spec", "storyboard", "prompt_bundle"}
               for meta in state["records"].values())
    assert not any((repository.root / "artifacts" / f"{ref}.json").exists()
                   for ref in previous_ids - state["records"].keys())
    assert ProjectService(repository).context()["text"] == "第二份原文：雪山木屋里的新故事。"


def test_recovery_prunes_legacy_history_and_stale_downstream(prompt_project):
    repository = prompt_project
    old_ids = set(repository.load()["records"])
    with repository.generation_lock():
        run, _ = repository.start_run({"stage": "novel_copy", "request_id": "legacy-run", "context": {"article": "old"}},
                                      expected_revision=repository.load()["revision"], snapshot={"input_refs": {"old": "old"}})
        ProjectService(repository).import_text("新原文", expected_revision=repository.load()["revision"])
        latest = novel(repository)
        # Represents a previously interrupted project with old records still on disk.
        assert old_ids <= repository.load()["records"].keys()
        before = repository.path.read_bytes()
        repository.load()
        assert repository.path.read_bytes() == before
    repository.recover()
    state = repository.load()
    assert repository.current("novel_copy")["id"] == latest["id"]
    assert repository.current("storyboard") is None and repository.current("prompt_bundle") is None
    assert len(state["records"]) == len(state["current"]) == 4
    assert state["runs"][-1]["id"] == run["id"] and state["runs"][-1]["status"] == "interrupted"
    assert "command" not in state["runs"][-1] and "input_refs" not in state["runs"][-1]["snapshot"]
    assert all(path.stem in state["records"] for path in (repository.root / "artifacts").glob("*.json"))


def test_cancel_recovery_discards_unselected_candidate(project):
    original = novel(project)
    with project.generation_lock():
        run, _ = project.start_run({"stage": "novel_copy", "request_id": "unfinished"},
                                   expected_revision=project.load()["revision"], snapshot={})
        with project.transaction() as state:
            candidate = project._save(state, "novel_copy", original["content"], original["dependency_refs"], select=False)
        assert candidate["id"] in project.load()["records"]
    project.recover()
    assert project.current("novel_copy")["id"] == original["id"]
    assert candidate["id"] not in project.load()["records"]
    assert not (project.root / "artifacts" / f"{candidate['id']}.json").exists()


@pytest.mark.parametrize("stage", ["novel_copy", "video_copy", "storyboard"])
def test_copy_and_storyboard_ui_has_no_history_actions(prompt_project, stage):
    import os
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication, QPushButton
    from core.gui.pages.short_video.artifact_page import ArtifactPage

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    page = ArtifactPage(SimpleNamespace(repository=prompt_project, perform=lambda callback: callback()), stage, "内容")
    page.refresh()
    titles = [button.text() for button in page.findChildren(QPushButton)]
    assert "更多" not in titles and "历史版本" not in titles
    assert "编辑阶段提示词" in titles and not hasattr(page, "restore_version")
    assert not hasattr(prompt_project, "select") and not hasattr(prompt_project, "versions")
    page.close()
    app.processEvents()


@pytest.mark.parametrize("stage", ["novel_copy", "video_copy", "storyboard"])
def test_generation_conflict_keeps_manual_content_without_history(prompt_project, monkeypatch, stage):
    from core.short_video.v2 import workflow
    from core.short_video.v2.models import encode
    from core.short_video.v2.review import QualityCheckResult

    monkeypatch.setattr(workflow, "capture_model_snapshot", lambda *_: {"model": "offline"})
    monkeypatch.setattr(runtime_module, "validate_model_snapshot", lambda *_: None)
    original = prompt_project.current(stage)
    candidate_ids = []

    def check(command, candidate):
        candidate_ids.append(candidate["id"])
        prompt_project.save(stage, original["content"], original["dependency_refs"],
                            expected_revision=prompt_project.load()["revision"])
        return QualityCheckResult(True)

    service = workflow.WorkflowService(prompt_project, transport=lambda _: encode(original["content"]), quality_check=check)
    with pytest.raises(WorkspaceError) as error:
        service.execute(service.prepare(stage))
    assert error.value.code == "TARGET_VERSION_CONFLICT"
    state = prompt_project.load()
    assert prompt_project.current(stage)["id"] not in {original["id"], *candidate_ids}
    assert len(current_records(prompt_project, stage)) == 1
    assert not any(ref in state["records"] for ref in [original["id"], *candidate_ids])
    assert state["runs"][-1]["status"] == "invalidated" and not state["runs"][-1]["output_refs"]


def test_paused_candidate_is_retained_for_resume(prompt_project, monkeypatch):
    from core.generation.cancellation import GenerationCancelled
    from core.short_video.v2 import workflow
    from core.short_video.v2.feedback import task_message
    from core.short_video.v2.models import encode
    from core.short_video.v2.review import QualityCheckResult

    monkeypatch.setattr(workflow, "capture_model_snapshot", lambda *_: {"model": "offline"})
    monkeypatch.setattr(runtime_module, "validate_model_snapshot", lambda *_: None)
    original = prompt_project.current("storyboard")
    work = workflow.WorkspaceWork(prompt_project, workflow.WorkflowService(prompt_project).prepare("storyboard"),
                                  transport=lambda _: encode(original["content"]))

    def check(command, candidate, *args):
        assert candidate["id"] in prompt_project.load()["records"]
        work.token.cancel()
        return QualityCheckResult(True)

    monkeypatch.setattr(runtime_module, "check_candidate", check)
    with pytest.raises(GenerationCancelled):
        work(lambda *_: None)
    assert prompt_project.current("storyboard")["id"] == original["id"]
    assert len(current_records(prompt_project, "storyboard")) == 1
    assert work.task_context.saved is True
    assert "继续生成" in task_message(work.task_context, cancelled=True)[2]
