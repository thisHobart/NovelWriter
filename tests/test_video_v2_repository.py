"""Workspace provenance and atomicity, using real temporary files."""
from copy import deepcopy
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
