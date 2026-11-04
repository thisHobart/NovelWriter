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
    from core.short_video.v2.prompt_compiler import compile_asset
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
        project.save(kind, compile_asset(kind, content), dependencies,
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
