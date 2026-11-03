"""Behavioral tests for distinct adaptation stages and article-backed prompt generation."""
from copy import deepcopy
import threading

import pytest

from core.generation.cancellation import CancelToken, GenerationCancelled
from core.short_video.v2.models import WorkspaceError, default_config, encode
from core.short_video.v2.repository import ProjectRepository
from core.short_video.v2.project_service import ProjectService
from core.short_video.v2.workflow import WorkflowService
from core.short_video.v2.validation import SPEC_FIELDS


ARTICLE = "我回到乡村老宅，井边压着父亲的铜钥匙。井里响了三声，手机播放父亲的录音：别回答。"


@pytest.fixture
def project(tmp_path):
    repository = ProjectRepository(tmp_path)
    ProjectService(repository).import_text(ARTICLE, expected_revision=0)
    return repository


def refs(repository):
    return ProjectService(repository).context()["source_refs"]


def sample(repository, stage, **params):
    source = refs(repository)
    if stage == "novel_copy":
        return {"opening": {"text": "井里响起三声，我却不敢回答。", "source_refs": source},
                "paragraphs": [{"id": "p1", "text": "我在井边找到父亲的铜钥匙，手机里是他的警告。", "source_refs": source}],
                "cliffhanger": {"text": "他为什么提前留下警告？", "source_refs": source}, "changes": []}
    if stage == "video_copy":
        return {"blocks": [{"id": "b1", "role": "opening", "narration": "井里响了三声。", "dialogue": [], "visible_action": "井盖轻响", "source_refs": source},
                           {"id": "b2", "role": "cliffhanger", "narration": "他为什么让我别回答？", "dialogue": [], "visible_action": "手停在井盖上", "source_refs": source}]}
    if stage == "asset_catalog":
        return {"entities": [{"asset_id": str(i), "name": name, "kind": kind, "aliases": [], "usage": "on_screen",
                              "variants": [{"variant_id": "base", "label": "原文状态"}], "source_refs": source, "inferred_fields": []}
                             for i, (kind, name) in enumerate([("character", "返乡主人公"), ("scene", "老宅井边"), ("prop", "铜钥匙")])]}
    if stage == "storyboard":
        entities = repository.current("asset_catalog")["content"]["entities"]
        entity_refs = {e["kind"]: {"asset_id": e["asset_id"], "variant_id": "base"} for e in entities}
        return {"shots": [{"shot_id": f"s{i+1}", "duration_ms": 10000, "shot_size": "近景", "camera_movement": "缓慢推进",
                           "scene_ref": entity_refs["scene"], "characters": [entity_refs["character"]] if i == 0 else [],
                           "props": [entity_refs["prop"]], "action": "铜钥匙压在井边，手慢慢收回。", "dialogue": [],
                           "narration": "井里为什么传出敲击？", "sound": "井口敲击声", "visual_prompt_draft": "乡村老宅井边的铜钥匙特写",
                           "video_block_refs": [block], "source_refs": source} for i, block in enumerate(["b1", "b2"])]}
    if stage == "asset_spec":
        entity, kind = params["entity"], params["kind"]
        details = {
            "character_spec": ["25岁", "女性", "圆脸，棕色眼睛", "黑色齐肩短发", "自然淡妆", "深蓝棉布外套与长裤", "无配饰", "当代", "返乡日常状态", "同一圆脸与齐肩短发，深蓝外套保持不变"],
            "scene_spec": ["乡村悬疑", "当代", "老宅院落井边", "夜晚", "方形院落，井在中央", "单层瓦房", "石砌井口", "木凳", "竹篱", "瓦房外墙", "院门在南侧", "石井与父亲的铜钥匙", "月光", "手机冷光", "安静悬疑", "完整空间全景"],
            "prop_spec": ["铜钥匙", "井边父亲遗留的旧铜钥匙", "当代", "掌心大小，细长钥匙柄", "黄铜", "暗金色", "褐色锈迹", "圆形钥匙环与两齿钥匙头", "完整有磨损"],
        }
        value = dict(zip(SPEC_FIELDS[kind], details[kind]))
        value.update(asset_id=entity["asset_id"], variant_id="base", style="写实真人短剧", views=[],
                     positive_prompt="", negative_prompt="", source_refs=source, inferred_fields=[])
        return value
    raise AssertionError(stage)


def generate(repository, stage, **params):
    payload = sample(repository, stage, **params)
    service = WorkflowService(repository, transport=lambda prompt: encode({"issues": []}) if prompt.startswith("自动审阅") else encode(payload))
    prepare_args = {k: v for k, v in params.items() if k != "entity"}
    if "entity" in params:
        prepare_args.update(entity_id=params["entity"]["asset_id"], variant_id="base")
    result = service.execute(service.prepare(stage, **prepare_args))
    return repository.record(result["version_id"])


def prepare_story(repository):
    for stage in ("novel_copy", "video_copy", "asset_catalog", "storyboard"):
        record = generate(repository, stage)
    return record


def test_two_stages_have_distinct_inputs_without_confirmation_gate(project):
    service = WorkflowService(project, transport=lambda _: pytest.fail("gate must run before model request"))
    with pytest.raises(WorkspaceError) as exc:
        service.prepare("video_copy")
    assert exc.value.code == "MISSING_UPSTREAM"
    first = generate(project, "novel_copy")
    assert project.load()["confirmations"] == {}
    second_command = service.prepare("video_copy")
    assert second_command["input_refs"]["novel_copy"] == first["id"]
    assert first["content"]["opening"]["text"] in second_command["prompt"]
    assert "专业的视频推文博主" in second_command["prompt"]


def test_whole_article_to_specific_prompts_without_media(project):
    prepare_story(project)
    catalog = project.current("asset_catalog")["content"]["entities"]
    view_counts = {}
    service = WorkflowService(project)
    for entity in catalog:
        kind = {"character": "character_spec", "scene": "scene_spec", "prop": "prop_spec"}[entity["kind"]]
        spec = generate(project, "asset_spec", entity=entity, kind=kind)
        compiled = service.compile_asset_prompts(spec["id"], expected_revision=project.load()["revision"])
        view_counts[entity["kind"]] = len(compiled["content"]["views"])
        assert all(v["positive_prompt"] and v["negative_prompt"] and "{" not in v["positive_prompt"] for v in compiled["content"]["views"])
        if entity["kind"] == "character":
            assert all("深蓝棉布外套" in v["positive_prompt"] for v in compiled["content"]["views"])
    assert view_counts == {"character": 4, "scene": 2, "prop": 3}
    bundle = service.compile_storyboard_prompts(expected_revision=project.load()["revision"])
    assert len(bundle["content"]["shots"]) == 2
    assert "铜钥匙" in bundle["content"]["shots"][0]["resolved_prompt"]
    assert not (project.root / "media").exists()


def test_format_repair_counts_calls_and_network_failure_does_not_retry(project):
    payloads = iter(["{broken", encode(sample(project, "novel_copy")), encode({"issues": []})])
    prompts = []
    def transport(prompt):
        prompts.append(prompt)
        return next(payloads)
    service = WorkflowService(project, transport=transport)
    result = service.execute(service.prepare("novel_copy"))
    assert result["status"] == "succeeded" and len(prompts) == 3 and "上次原始回复" in prompts[1]
    calls = []
    def fail(prompt):
        calls.append(prompt)
        raise ConnectionError("SDK error with secret-token")
    service = WorkflowService(project, transport=fail)
    with pytest.raises(WorkspaceError, match="文字服务"):
        service.execute(service.prepare("novel_copy"))
    assert len(calls) == 1
    run = project.load()["runs"][-1]
    assert run["status"] == "failed" and len(run["calls"]) == 1 and "secret-token" not in encode(run)


def test_cancellation_keeps_returned_candidate_without_selecting(project):
    token = CancelToken()
    def transport(_):
        token.cancel()
        return encode(sample(project, "novel_copy"))
    service = WorkflowService(project, transport=transport)
    with pytest.raises(GenerationCancelled):
        service.execute(service.prepare("novel_copy"), token=token)
    assert project.current("novel_copy") is None
    assert len(project.versions("novel_copy")) == 1 and project.load()["runs"][-1]["status"] == "cancelled"


def test_manual_save_during_request_is_not_overwritten(project):
    original = generate(project, "novel_copy")
    def transport(_):
        content = deepcopy(original["content"])
        content["opening"]["text"] = "人工保留的开头"
        project.save("novel_copy", content, original["dependency_refs"], parent_id=original["id"], expected_revision=project.load()["revision"])
        return encode(sample(project, "novel_copy"))
    service = WorkflowService(project, transport=transport)
    result = service.execute(service.prepare("novel_copy"))
    assert not result["selected"]
    assert project.current("novel_copy")["content"]["opening"]["text"] == "人工保留的开头"


def test_timeline_and_partial_revision_protect_unselected_shots(project):
    board = prepare_story(project)
    changed = deepcopy(board["content"])
    changed["shots"][0]["duration_ms"] = 8000
    changed["shots"][0]["start_ms"] = 9999
    saved = project.save("storyboard", changed, board["dependency_refs"], parent_id=board["id"], expected_revision=project.load()["revision"])
    assert saved["content"]["shots"][0]["start_ms"] == 0
    assert saved["content"]["shots"][1]["start_ms"] == 8000
    changed["shots"][1]["action"] = "偷偷改写未选镜头"
    service = WorkflowService(project, transport=lambda _: encode(changed))
    with pytest.raises(WorkspaceError):
        service.execute(service.prepare("storyboard", mode="revise", target_id=saved["id"], asks=["第一镜更短"], shot_ids=["s1"]))
    assert project.current("storyboard")["id"] == saved["id"]


def test_catalog_reextraction_preserves_entity_ids(project):
    prepare_story(project)
    before = project.current("asset_catalog")["content"]["entities"]
    after = generate(project, "asset_catalog")["content"]["entities"]
    assert [e["asset_id"] for e in before] == [e["asset_id"] for e in after]


def test_context_overflow_does_not_silently_truncate_or_call(project):
    service = WorkflowService(project, max_context_chars=10, transport=lambda _: pytest.fail("must not call"))
    with pytest.raises(WorkspaceError) as exc:
        service.prepare("novel_copy")
    assert exc.value.code == "INPUT_TOO_LARGE" and not project.load()["runs"]


def test_concrete_model_snapshot_conflict_and_completed_dedup(project, monkeypatch):
    from types import SimpleNamespace
    from core.generation import api_providers
    name = ["model-one"]
    monkeypatch.setattr(api_providers, "get_config", lambda _: SimpleNamespace(model=name[0]))
    calls = []
    service = WorkflowService(project, transport=lambda p: calls.append(p) or encode(sample(project, "novel_copy")))
    command = service.prepare("novel_copy")
    assert command["model_snapshot"] == {"provider": "hosted-llm", "model": "model-one"}
    name[0] = "model-two"
    with pytest.raises(WorkspaceError) as exc:
        service.execute(command)
    assert exc.value.code == "MODEL_CONFIGURATION_CHANGED" and not calls and project.load()["runs"][-1]["status"] == "failed"
    command = service.prepare("novel_copy")
    service.execute(command)
    name[0] = "model-three"
    assert service.execute(command)["status"] == "succeeded" and len(calls) == 2
