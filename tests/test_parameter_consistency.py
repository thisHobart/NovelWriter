"""参数快照、落盘前检查、有限修复及失败保护的离线回归。"""
from copy import deepcopy
import json
import logging
from pathlib import Path

import pytest

from core.generation.parameters import normalize_parameters, freeze_parameters, requirements_block, creation_parameters
from core.generation.parameter_compliance import ComplianceReview, ComplianceServiceError, UpstreamParameterConflict
from core.generation.stage_context import make_context
from core.generation.stage_pipeline import GenerationHost
from core.generation import lore_pipeline, stage_pipeline
from core.gui import params_store
from core.gui.services import step_runner
from core.generation.lore_repository import LoreRepository, BODY, FACTIONS, CHARACTERS, CONTRACT, encode
from test_lore_cast import _faction, _character, _antagonist
from test_lore_repository import write


def review_reply(prompt, *, fail_key=None, evidence="", uncertain=False):
    source = prompt.split("\n## 参数\n", 1)[1].split("\n## 作品要求", 1)[0]
    params = json.loads(source)
    checks = []
    for key, value in params.items():
        status = "fail" if key == fail_key else "uncertain" if uncertain else "pass"
        checks.append({"parameter_key": key, "status": status, "expected": str(value), "actual": "候选实际设定",
                       "evidence": evidence if key == fail_key else "", "reason": "与要求不符" if key == fail_key else "已逐项检查"})
    return json.dumps({"status": "fail" if fail_key else "uncertain" if uncertain else "pass", "checks": checks}, ensure_ascii=False)


def context(root, parameters=None, factions=1, characters=2):
    params = {"genre": "Sci-Fi", "subgenre": "Space Opera", "theme": "外交与和平", "tone": "克制",
              "protagonist_type": "Diplomat", "conflict_scale": "Galactic", "interstellar_travel": False,
              "multiple_species": False, "space_combat": False, "quality_loop": "off"}
    params.update(parameters or {})
    return make_context(str(root), "test-model", params, num_factions=factions, num_chars=characters)


def pipeline(ctx):
    return lore_pipeline.LorePipeline(GenerationHost(ctx, logging.getLogger("test.parameters")))


def test_overrides_update_both_spellings_preserving_false_zero_and_chinese():
    params = normalize_parameters({"Theme": "旧主题", "Space Combat": True, "Novel Title": "旧书名"},
                                  {"theme": "新主题", "space_combat": False, "title": "新书名", "custom_zero": 0})
    assert params["Theme"] == params["theme"] == "新主题"
    assert params["Novel Title"] == params["novel_title"] == "新书名"
    assert params["space_combat"] is params["Space Combat"] is False
    assert params["custom_zero"] == 0
    assert "title" not in params


@pytest.mark.parametrize("key,value", [("num_factions", 0), ("num_factions", 11), ("num_characters", 1),
                                      ("num_characters", 11), ("num_characters", "2.5")])
def test_invalid_counts_fail_before_model_calls(tmp_path, monkeypatch, key, value):
    monkeypatch.setattr(stage_pipeline, "set_backend", lambda *args: pytest.fail("参数无效时不能进入模型设置"))
    with pytest.raises(ValueError):
        stage_pipeline._build_context("lore", str(tmp_path), "test-model", {key: value}, None)


def test_snapshot_ignores_later_disk_and_source_changes(tmp_path, monkeypatch):
    params_store.save(str(tmp_path), {"genre": "Mystery", "theme": "最初主题"})
    source = {"genre": "Sci-Fi", "theme": "任务主题", "space_combat": False}
    snapshot = freeze_parameters(source)
    source["theme"] = "后来改动"
    params_store.save(str(tmp_path), {"genre": "Fantasy", "theme": "磁盘新值"})
    monkeypatch.setattr(stage_pipeline, "set_backend", lambda *args: None)
    ctx, _ = stage_pipeline._build_context("lore", str(tmp_path), "test-model", snapshot, None)
    assert ctx.parameters["Genre"] == "Sci-Fi" and ctx.parameters["Theme"] == "任务主题"
    assert ctx.parameters["Space Combat"] is False
    trace = json.loads((Path(ctx.extras["run_directory"]) / "parameters.json").read_text(encoding="utf-8"))
    assert trace["creation_parameters"]["theme"] == "任务主题"


def test_complete_workflow_freezes_parameters_across_stages(tmp_path, monkeypatch):
    params_store.save(str(tmp_path), {"genre": "Sci-Fi", "theme": "共同快照", "num_factions": 2, "num_characters": 3})
    seen = []
    def fake_run(step, output_dir, model="", parameters=None, report=None):
        seen.append(deepcopy(parameters))
        params_store.save(str(tmp_path), {"genre": "Mystery", "theme": "运行期间变化"})
        return {"step": step}
    monkeypatch.setattr(step_runner, "run_stage", fake_run)
    step_runner.full_workflow_work(str(tmp_path))(lambda *args: None)
    assert len(seen) == 4
    assert all(p["theme"] == "共同快照" and p["num_characters"] == 3 and p["num_factions"] == 2 for p in seen)


def test_all_core_prompts_include_complete_requirements_and_no_runtime_config(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    ctx.parameters.update(backend="api", model="SECRET_RUNTIME_MODEL", output_directory="SECRET_RUNTIME_PATH")
    prompts = []
    def fake_send(prompt, **kwargs):
        assert kwargs["model"] == "test-model"
        if prompt.startswith("你是作品参数符合性检查员"):
            return review_reply(prompt)
        prompts.append(prompt)
        if "设计 1 个势力" in prompt:
            return json.dumps({"factions": [_faction()]}, ensure_ascii=False)
        if "设计 2 位主要人物" in prompt:
            return json.dumps({"characters": [_character(profession="外交官"), _antagonist()], "relationships": []}, ensure_ascii=False)
        if "完善关键人物" in prompt:
            return "人物在旧城成长，后来学会以谈判化解冲突。"
        if "现在生成世界观设定" in prompt:
            from core.generation.design_contract import LORE_CONTRACT_START, LORE_CONTRACT_END
            contract = {"era": "未来", "technology_level": "先进技术", "world_constraints": ["无太空战斗"],
                        "canonical_characters": [{"id": "C1", "name": "梁浩"}, {"id": "C2", "name": "郑娜敏"}],
                        "canonical_factions": [{"id": "F1", "name": _faction()["name"]}], "canonical_locations": []}
            return "外交官在未来城市以和平谈判处理冲突。\n" + LORE_CONTRACT_START + "\n" + encode(contract) + LORE_CONTRACT_END
        pytest.fail("未预期的生成请求")
    monkeypatch.setattr(lore_pipeline, "send_prompt", fake_send)
    pipe = pipeline(ctx)
    pipe._generate_factions(ctx)
    pipe._generate_characters(ctx)
    pipe._generate_lore(ctx)
    pipe._main_character_enhancement(ctx)
    assert len(prompts) == 5
    for prompt in prompts:
        assert "外交官" in prompt and "银河系规模" in prompt
        assert "太空战斗：不得引入" in prompt and "多种族共存：不得引入" in prompt
        assert "星际旅行：不得引入" in prompt
        assert "SECRET_RUNTIME" not in prompt
        assert "现实体系" not in prompt
    assert "人物在旧城成长" in (tmp_path / "story/lore/background_protagonist_梁浩.md").read_text(encoding="utf-8")
    assert list(Path(ctx.extras["run_directory"]).glob("compliance/*/attempt_1_report.json"))


def test_protagonist_mismatch_repairs_before_publication(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    write(tmp_path, CHARACTERS, "旧人物资料")
    generation, reviews = [], []
    def fake_send(prompt, **kwargs):
        if prompt.startswith("你是作品参数符合性检查员"):
            reviews.append(prompt)
            return review_reply(prompt, fail_key="protagonist_type", evidence="刑警") if len(reviews) == 1 else review_reply(prompt)
        generation.append(prompt)
        assert (tmp_path / CHARACTERS).read_text(encoding="utf-8") == "旧人物资料"
        return json.dumps({"characters": [_character(profession="刑警" if len(generation) == 1 else "外交官"), _antagonist()], "relationships": []}, ensure_ascii=False)
    monkeypatch.setattr(lore_pipeline, "send_prompt", fake_send)
    pipeline(ctx)._generate_characters(ctx)
    assert len(generation) == len(reviews) == 2
    assert "外交官" in generation[1] and "原文「刑警」" in generation[1]
    assert json.loads((tmp_path / CHARACTERS).read_text(encoding="utf-8"))["characters"][0]["profession"] == "外交官"


def test_banned_element_stops_on_repeated_problem_and_delivers_current_output(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    write(tmp_path, FACTIONS, "旧势力资料")
    generated, reviewed = [], []
    def fake_send(prompt, **kwargs):
        if prompt.startswith("你是作品参数符合性检查员"):
            reviewed.append(prompt)
            return review_reply(prompt, fail_key="space_combat", evidence="舰队交战")
        generated.append(prompt)
        return json.dumps({"factions": [_faction(description="组织参与舰队交战")]}, ensure_ascii=False)
    monkeypatch.setattr(lore_pipeline, "send_prompt", fake_send)
    pipeline(ctx)._generate_factions(ctx)
    assert len(generated) == len(reviewed) == 2
    assert "舰队交战" in (tmp_path / FACTIONS).read_text(encoding="utf-8")
    assert ctx.extras["lore_results"][-1]["status"] == "needs_changes"
    assert len(list(Path(ctx.extras["run_directory"]).glob("compliance/*/attempt_*_candidate.txt"))) == 2


@pytest.mark.parametrize("failure", ["network", "malformed", "missing_checks"])
def test_review_service_failure_delivers_candidate_with_incomplete_check(tmp_path, monkeypatch, failure):
    ctx = context(tmp_path)
    write(tmp_path, FACTIONS, "旧势力资料")
    generated = []
    def fake_send(prompt, **kwargs):
        if prompt.startswith("你是作品参数符合性检查员"):
            if failure == "network":
                raise RuntimeError("服务不可用")
            return "不合法返回" if failure == "malformed" else '{"status":"pass","checks":[]}'
        generated.append(prompt)
        return json.dumps({"factions": [_faction()]}, ensure_ascii=False)
    monkeypatch.setattr(lore_pipeline, "send_prompt", fake_send)
    pipeline(ctx)._generate_factions(ctx)
    assert len(generated) == 1
    assert json.loads((tmp_path / FACTIONS).read_text(encoding="utf-8"))[0]["name"] == _faction()["name"]
    assert ctx.extras["lore_results"][-1]["status"] == "unchecked"


def test_fail_without_real_evidence_becomes_manual_review(tmp_path):
    ctx = context(tmp_path, {"theme": "政治博弈"})
    review = ComplianceReview(ctx, "world", lambda prompt: review_reply(prompt, fail_key="theme", evidence="不存在的原文"))
    review("普通城市中的和平谈判。")
    assert ctx.extras["parameter_warnings"]
    report = json.loads(next(review.folder.glob("*_report.json")).read_text(encoding="utf-8"))
    assert report["status"] == "uncertain"


def test_manual_upstream_conflict_warns_and_preserves_manual_source(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    write(tmp_path, CHARACTERS, {"characters": [_character(), _antagonist()], "relationships": []})
    repo = LoreRepository(tmp_path)
    data = json.loads(repo.load_snapshot().files[CHARACTERS])
    data["characters"][0]["background"] = "人工指定的刑警经历"
    repo.save(repo.load_snapshot(), {CHARACTERS: encode(data)})
    before = repo.load_snapshot().files
    requests = []
    def fake_send(prompt, **kwargs):
        requests.append(prompt)
        if prompt.startswith("你是作品参数符合性检查员"):
            return review_reply(prompt, fail_key="protagonist_type", evidence="刑警")
        return "外交官处理未来城市冲突。"
    monkeypatch.setattr(lore_pipeline, "send_prompt", fake_send)
    pipeline(ctx)._generate_lore(ctx)
    assert len(requests) <= 6 and repo.load_snapshot().files[CHARACTERS] == before[CHARACTERS]
    assert any("已有人工设定" in issue for issue in ctx.extras["parameter_warnings"])
    assert (tmp_path / BODY).exists()


def test_full_workflow_stops_on_failed_step(tmp_path, monkeypatch):
    seen = []
    def fake_run(step, *args, **kwargs):
        seen.append(step)
        if step == "structure":
            raise ComplianceServiceError("检查失败")
        return {"step": step}
    monkeypatch.setattr(step_runner, "run_stage", fake_run)
    with pytest.raises(ComplianceServiceError):
        step_runner.full_workflow_work(str(tmp_path), parameters=freeze_parameters({"genre": "Sci-Fi"}))(lambda *args: None)
    assert seen == ["lore", "structure"]


def test_explicit_workflow_overrides_are_frozen_once(tmp_path, monkeypatch):
    params_store.save(str(tmp_path), {"Theme": "旧主题", "Genre": "Mystery", "Num Characters": 1})
    seen = []
    def fake_run(step, root, model="", **kwargs):
        seen.append(deepcopy(kwargs["parameters"]))
        params_store.save(str(tmp_path), {"Theme": "中途修改", "Num Characters": 9})
        return {}
    monkeypatch.setattr(step_runner, "run_stage", fake_run)
    step_runner.full_workflow_work(str(tmp_path), parameters={"theme": "明确新主题", "num_characters": 3})(lambda *args: None)
    assert all(p["theme"] == p["Theme"] == "明确新主题" and p["num_characters"] == 3 for p in seen)


@pytest.mark.parametrize("kind,number", [("factions", 2), ("characters", 3)])
def test_local_exact_quantity_check_delivers_readable_output_for_editing(tmp_path, monkeypatch, kind, number):
    ctx = context(tmp_path, factions=number if kind == "factions" else 1,
                  characters=number if kind == "characters" else 2)
    target = FACTIONS if kind == "factions" else CHARACTERS
    write(tmp_path, target, "旧正式产物")
    calls = []
    def fake_send(prompt, **kwargs):
        assert not prompt.startswith("你是作品参数符合性检查员"), "数量未通过时不能进入模型检查"
        calls.append(prompt)
        return encode({"factions": [_faction()]}) if kind == "factions" else encode({"characters": [_character(), _antagonist()]})
    monkeypatch.setattr(lore_pipeline, "send_prompt", fake_send)
    getattr(pipeline(ctx), "_generate_" + kind)(ctx)
    assert len(calls) == 2 and (tmp_path / target).read_text(encoding="utf-8") != "旧正式产物"
    assert "数量要求" in ctx.extras["lore_results"][-1]["issues"][0]
    assert list(Path(ctx.extras["run_directory"]).glob("compliance/*/*structural_error.txt"))


def test_complete_lore_stage_uses_three_characters_two_factions(tmp_path, monkeypatch):
    from test_lore_name_localization import FACTIONS as faction_data, CHARACTERS as character_data, RELATIONSHIPS
    from core.generation.design_contract import LORE_CONTRACT_START, LORE_CONTRACT_END
    params_store.save(str(tmp_path), {"genre": "Mystery", "theme": "磁盘旧主题"})
    snapshot = freeze_parameters({"genre": "Sci-Fi", "theme": "本次主题", "protagonist_type": "Diplomat",
                                  "space_combat": False, "num_factions": 2, "num_characters": 3})
    generated, checked = [], []
    def fake_send(prompt, **kwargs):
        if prompt.startswith("你是作品参数符合性检查员"):
            checked.append(prompt)
            return review_reply(prompt)
        generated.append(prompt)
        assert "本次主题" in prompt and "磁盘旧主题" not in prompt
        if "设计 2 个势力" in prompt:
            return encode({"factions": faction_data})
        if "设计 3 位主要人物" in prompt:
            return encode({"characters": character_data, "relationships": RELATIONSHIPS})
        if "现在生成世界观设定" in prompt:
            contract = {"era": "未来", "technology_level": "先进技术", "world_constraints": ["没有太空战斗"],
                        "canonical_characters": [{"id": f"C{i}", "name": c["name"]} for i, c in enumerate(character_data)],
                        "canonical_factions": [{"id": f"F{i}", "name": f["name"]} for i, f in enumerate(faction_data)],
                        "canonical_locations": []}
            return "未来城市外交官以谈判处理冲突。\n" + LORE_CONTRACT_START + encode(contract) + LORE_CONTRACT_END
        if "完善关键人物" in prompt:
            return "这位人物在未来城市成长，其经历决定了如今的选择。"
        return "1. 静默之城"
    monkeypatch.setattr(lore_pipeline, "send_prompt", fake_send)
    monkeypatch.setattr(stage_pipeline, "set_backend", lambda *args: None)
    result = stage_pipeline.run_stage("lore", str(tmp_path), "test-model", parameters=snapshot)
    assert len(json.loads((tmp_path / FACTIONS).read_text(encoding="utf-8"))) == 2
    assert len(json.loads((tmp_path / CHARACTERS).read_text(encoding="utf-8"))["characters"]) == 3
    assert len(generated) == 7 and len(checked) == 6
    assert result["parameter_run_directory"] and not result["parameter_warnings"]


def test_background_failure_is_checked_per_person_and_delivers_current_backgrounds(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    write(tmp_path, CHARACTERS, {"characters": [_character(profession="外交官"), _antagonist()]})
    write(tmp_path, BODY, "未来城市中的外交冲突")
    write(tmp_path, "story/lore/background_protagonist_梁浩.md", "原主角背景")
    write(tmp_path, "story/lore/background_antagonist_郑娜敏.md", "原反派背景")
    requests = []
    def fake_send(prompt, **kwargs):
        requests.append(prompt)
        if prompt.startswith("你是作品参数符合性检查员"):
            assert "## 本次人物" in prompt
            return review_reply(prompt, fail_key="space_combat", evidence="舰队交战")
        return "主角指挥舰队交战。"
    monkeypatch.setattr(lore_pipeline, "send_prompt", fake_send)
    pipeline(ctx)._main_character_enhancement(ctx)
    assert len(requests) == 8
    assert (tmp_path / "story/lore/background_protagonist_梁浩.md").read_text(encoding="utf-8") == "主角指挥舰队交战。"
    assert (tmp_path / "story/lore/background_antagonist_郑娜敏.md").read_text(encoding="utf-8") == "主角指挥舰队交战。"
    review = ComplianceReview(ctx, "background", lambda *args: "", subject=_antagonist())
    assert "protagonist_type" not in review.parameters and "space_combat" in review.parameters


def test_parameter_warning_persists_on_unchanged_existing_artifacts(tmp_path, monkeypatch):
    write(tmp_path, BODY, "旧项目世界观")
    monkeypatch.setattr(stage_pipeline, "set_backend", lambda *args: None)
    monkeypatch.setattr(stage_pipeline, "_run_lore", lambda ctx, host:
                        ctx.extras.update(parameter_warnings=["主题需人工核对"]) or {"step": "lore"})
    stage_pipeline.run_stage("lore", str(tmp_path), parameters={"genre": "Sci-Fi"})
    status = LoreRepository(tmp_path).review_status()
    assert status["review_items"] == ["主题需人工核对"] and not status["pending"]
    LoreRepository(tmp_path).acknowledge(status["version"])
    assert not LoreRepository(tmp_path).review_status()["review_items"]


def test_all_model_prompts_are_archived_per_run_and_scope_is_reset(tmp_path, monkeypatch):
    from core.generation import ai_helper
    monkeypatch.setattr(stage_pipeline, "set_backend", lambda *args: None)
    monkeypatch.setattr(ai_helper, "get_backend", lambda: "api")
    monkeypatch.setattr(ai_helper.api_providers, "send_messages", lambda *args: "模拟结果")
    def operation(ctx, host):
        ai_helper.send_prompt("本次实际提示词", model="hosted-llm")
        return {"step": "lore"}
    monkeypatch.setattr(stage_pipeline, "_run_lore", operation)
    result = stage_pipeline.run_stage("lore", str(tmp_path), parameters={"theme": "本次主题"})
    folder = Path(result["parameter_run_directory"]) / "prompts"
    assert (folder / "0001.md").read_text(encoding="utf-8") == "本次实际提示词"
    ai_helper.send_prompt("运行之外的请求", model="hosted-llm")
    assert len(list(folder.glob("*.md"))) == 1


def test_chapter_agent_keeps_task_snapshot_in_context(tmp_path):
    from agents.writing.chapter_writing_agent import ChapterWritingAgent
    agent = ChapterWritingAgent(str(tmp_path), use_new_structure=True)
    agent.parameter_snapshot = freeze_parameters({"theme": "启动时主题", "genre": "Sci-Fi"})
    params_store.save(str(tmp_path), {"theme": "后来保存的新主题", "genre": "Mystery"})
    first = agent._load_writing_context()
    assert first["parameters"]["theme"] == "启动时主题"
    first["parameters"]["theme"] = "局部改动"
    assert agent._load_story_parameters(str(tmp_path / "system/parameters.txt"))["Theme"] == "启动时主题"


@pytest.mark.parametrize("step", ["structure", "scenes", "chapters"])
def test_manual_parameter_conflicts_block_dependent_stages(tmp_path, monkeypatch, step):
    from core.generation import ai_helper
    write(tmp_path, BODY, "舰队交战是人工设定中的既有事件")
    repo = LoreRepository(tmp_path)
    repo.save(repo.load_snapshot(), {BODY: "人工设定中的舰队交战"})
    monkeypatch.setattr(stage_pipeline, "set_backend", lambda *args: None)
    monkeypatch.setattr(ai_helper, "send_prompt", lambda prompt, **kwargs:
                        review_reply(prompt, fail_key="space_combat", evidence="舰队交战"))
    monkeypatch.setattr(stage_pipeline, "_run_" + step, lambda *args: pytest.fail("冲突未解决不能生成下游"))
    with pytest.raises(UpstreamParameterConflict):
        stage_pipeline.run_stage(step, str(tmp_path), parameters={"space_combat": False})


@pytest.mark.parametrize("bad_cast", [{"relationships": "不是数组"}, {"relationships": ["不是对象"]}])
def test_invalid_relationship_fields_are_not_silently_discarded(tmp_path, monkeypatch, bad_cast):
    ctx = context(tmp_path)
    write(tmp_path, CHARACTERS, "旧人物资料")
    monkeypatch.setattr(lore_pipeline, "send_prompt", lambda *args, **kwargs:
                        encode({"characters": [_character(), _antagonist()], **bad_cast}))
    pipeline(ctx)._generate_characters(ctx)
    assert ctx.extras["lore_results"][-1]["status"] == "draft"
    assert (tmp_path / CHARACTERS).read_text(encoding="utf-8") == "旧人物资料"


@pytest.mark.parametrize("function,invalid", [("generate_factions", 0), ("generate_factions", 11),
                                               ("generate_characters", 1), ("generate_characters", 11),
                                               ("generate_cast", 1), ("generate_cast", 11)])
def test_direct_card_generators_reject_invalid_counts_before_model(function, invalid):
    from core.generation import lore_cast
    with pytest.raises(ValueError, match="数量必须"):
        getattr(lore_cast, function)(lambda *args: pytest.fail("无效数量不应调用模型"), {"genre": "Sci-Fi"}, invalid)
