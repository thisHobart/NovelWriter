from copy import deepcopy
import csv
import json

from openpyxl import load_workbook
import pytest

from core.generation.cancellation import CancelToken, GenerationCancelled
from core.short_video.v2.models import WorkspaceError, ASSET_KINDS, encode
from core.short_video.v2.workflow import WorkflowService
from core.short_video.v2.exporter import ExportService, COLUMNS
from core.short_video.v2.batches import ChunkService
from test_video_v2_workflow import project, sample, generate, prepare_story, refs


def complete(repository):
    prepare_story(repository)
    service = WorkflowService(repository)
    for entity in repository.current("asset_catalog")["content"]["entities"]:
        generate(repository, "asset_spec", entity=entity, kind=ASSET_KINDS[entity["kind"]])
    service.compile_storyboard_prompts(expected_revision=repository.load()["revision"])


def test_formal_export_all_formats_roundtrip_and_immutable_manifest(project, tmp_path):
    complete(project)
    exporter = ExportService(project)
    for suffix in ("xlsx", "csv", "md", "txt", "json"):
        path = tmp_path / f"作品.{suffix}"
        result = exporter.export(path)
        assert path.exists() and result["snapshot"]["mode"] == "selected_content"
        manifest = json.loads((tmp_path / f"作品.{suffix}.manifest.json").read_text(encoding="utf-8"))
        assert all(r["content_hash"] for r in manifest["records"].values())
        with pytest.raises(WorkspaceError, match="存在"):
            exporter.export(path)
    book = load_workbook(tmp_path / "作品.xlsx")
    assert [c.value for c in book["分镜脚本"][1]] == COLUMNS
    with (tmp_path / "作品.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    assert rows[0] == COLUMNS and len(rows) == 3
    assert "铜钥匙" in (tmp_path / "作品.md").read_text(encoding="utf-8")
    assert not (project.root / "media").exists()


def test_formal_missing_views_stale_and_formula_injection(project, tmp_path):
    complete(project)
    board = project.current("storyboard")
    content = deepcopy(board["content"])
    content["shots"][0]["action"] = '=HYPERLINK("https://example.com","test")\n换行，中文'
    project.save("storyboard", content, board["dependency_refs"], parent_id=board["id"], expected_revision=project.load()["revision"])
    with pytest.raises(WorkspaceError, match="旧来源"):
        ExportService(project).snapshot()
    output = tmp_path / "draft.csv"
    ExportService(project).export(output, keys=["storyboard"])
    with output.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    assert rows[1][5].startswith("'=HYPERLINK") and "换行，中文" in rows[1][5]
    record = next(r for r in project.versions() if r["kind"] == "character_spec")
    edited = deepcopy(record["content"])
    edited["views"] = []
    project.save(record["kind"], edited, record["dependency_refs"], parent_id=record["id"], expected_revision=project.load()["revision"])
    with pytest.raises(WorkspaceError, match="视图"):
        ExportService(project).snapshot(keys=[record["key"]])


def test_chunk_cancel_resume_reuses_saved_facts_and_copy(project):
    service = ChunkService(project, chunk_chars=100)
    plan = service.create_plan()
    token = CancelToken()
    requests = []
    def send(prompt):
        requests.append(prompt)
        token.cancel()
        return encode({"facts": [{"text": "井边有铜钥匙", "source_refs": refs(project)}]})
    service.transport = send
    with pytest.raises(GenerationCancelled):
        service.execute(plan["id"], token=token)
    saved = service.load_plan(plan["id"])
    assert saved["chunks"][0]["facts"] is not None and saved["calls_used"] == 1
    token = CancelToken()
    service.transport = lambda prompt: requests.append(prompt) or encode(sample(project, "novel_copy"))
    result = service.execute(plan["id"], token=token)
    assert result["status"] == "succeeded" and len(requests) == 4
    service.execute(plan["id"])
    assert len(requests) == 4 and project.current("novel_copy")["metadata"]["plan_id"] == plan["id"]


def test_chunk_merge_failure_resumes_without_repeating_successful_chunks(project):
    service = ChunkService(project)
    plan = service.create_plan()
    responses = iter([encode({"facts": []}), encode(sample(project, "novel_copy"))] + ["bad"] * 6)
    service.transport = lambda _: next(responses)
    with pytest.raises(WorkspaceError):
        service.execute(plan["id"])
    saved = service.load_plan(plan["id"])
    assert saved["chunks"][0]["output"] is not None and saved["calls_used"] == 8
    requests = []
    service.transport = lambda p: requests.append(p) or encode(sample(project, "novel_copy"))
    service.execute(plan["id"])
    assert len(requests) == 2 and "合并各块" in requests[0]


def test_multiple_chunks_preserve_full_source_and_share_all_facts(project):
    from core.short_video.v2.project_service import ProjectService
    article = "一" * 110 + "\n" + "二" * 110 + "\n" + "三" * 110
    ProjectService(project).import_text(article, expected_revision=project.load()["revision"])
    service = ChunkService(project, chunk_chars=120)
    plan = service.create_plan()
    assert len(plan["chunks"]) == 3 and "".join(c["text"] for c in plan["chunks"]) == article
    requests = []
    def send(prompt):
        requests.append(prompt)
        if prompt.startswith("提取本段"):
            chunk = json.loads(prompt.split("\n", 1)[1])
            return encode({"facts": [{"text": chunk["text"][:1], "source_refs": [
                {"source_id": chunk["source_id"], "start": chunk["start"], "end": chunk["end"]}]}]})
        assert '"shared_facts"' in prompt or '"facts"' in prompt
        return encode(sample(project, "novel_copy"))
    service.transport = send
    service.execute(plan["id"])
    assert len(requests) == 8 and service.load_plan(plan["id"])["calls_used"] == 8


def test_long_article_continues_second_stage_with_explicit_fact_table(project):
    from core.short_video.v2.project_service import ProjectService
    ProjectService(project).import_text("甲" * 12000, expected_revision=project.load()["revision"])
    service = ChunkService(project, chunk_chars=6000)
    plan = service.create_plan()
    def send(prompt):
        if prompt.startswith("提取本段"):
            chunk = json.loads(prompt.split("\n", 1)[1])
            return encode({"facts": [{"text": "本段素材摘要", "source_refs": [{"source_id": chunk["source_id"], "start": chunk["start"], "end": chunk["end"]}]}]})
        return encode(sample(project, "novel_copy"))
    service.transport = send
    service.execute(plan["id"])
    novel = project.current("novel_copy")
    assert "fact_table" in novel["dependency_refs"]
    workflow = WorkflowService(project, transport=lambda _: encode(sample(project, "video_copy")))
    command = workflow.prepare("video_copy")
    assert command["context"]["article"]["representation"] == "explicit_chunk_fact_table"
    assert "未将全文直接放入本次请求" in command["prompt"] and len(command["prompt"]) <= 48000
    workflow.execute(command)
    facts = project.current("fact_table")
    changed = deepcopy(facts["content"])
    changed["facts"][0]["text"] = "重新核对的事实摘要"
    project.save("fact_table", changed, facts["dependency_refs"], parent_id=facts["id"], expected_revision=project.load()["revision"])
    assert not project.is_current(project.current("video_copy")["id"])


def test_batch_recovery_marks_interrupted_without_requests(project):
    from core.short_video.v2.batches import recover_plans
    service = ChunkService(project, transport=lambda _: pytest.fail("recovery must not request"))
    plan = service.create_plan()
    plan["status"] = "running"
    service.save_plan(plan)
    recover_plans(project)
    assert service.load_plan(plan["id"])["status"] == "interrupted"
