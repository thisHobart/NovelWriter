"""Human edits must survive restarts without corrupting canonical references."""
from copy import deepcopy
import base64
import json
from pathlib import Path

import pytest

from core.generation.lore_repository import (
    LoreRepository, LoreEditError, MappingRequired, tracked_generation, lore_context,
    BODY, CHARACTERS, FACTIONS, CONTRACT, META, LORE, encode,
)


def write(root, rel, value):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encode(value) if not isinstance(value, str) else value, encoding="utf-8")


@pytest.fixture
def project(tmp_path):
    write(tmp_path, BODY, "# 世界\n张明在调查局工作。")
    write(tmp_path, CHARACTERS, {
        "characters": [
            {"name": "张明", "role": "protagonist", "age": 30, "gender": "男", "faction": "调查局",
             "background": "旧背景", "goals": ["破案", "归家"], "extra": {"keep": 7}},
            {"name": "王柳", "role": "antagonist", "faction": "公司",
             "opposes": {"character": "张明", "blocked_goal": "破案"}},
        ],
        "relationships": [{"character1": "张明", "character2": "王柳", "type": "对手", "description": "多年旧怨"}],
        "metadata": {"generation_date": "yesterday", "legacy": True},
    })
    write(tmp_path, FACTIONS, [
        {"name": "调查局", "description": "原描述", "territory": "旧城", "territory_kind": "城区",
         "conflicts": [{"with": "公司", "over": "旧案"}]},
        {"name": "公司", "description": "商业机构", "conflicts": [{"with": "调查局", "over": "旧案"}]},
    ])
    write(tmp_path, CONTRACT, {
        "era": "当代", "technology_level": "现实", "world_constraints": ["无超能力"],
        "canonical_characters": [{"id": "CH1", "name": "张明", "aliases": []}, {"id": "CH2", "name": "王柳", "aliases": []}],
        "canonical_factions": [{"id": "F1", "name": "调查局"}, {"id": "F2", "name": "公司"}],
        "canonical_locations": [{"id": "L1", "name": "旧城"}], "custom": {"keep": "yes"},
    })
    write(tmp_path, LORE + "background_protagonist_张明.md", "张明的详细背景")
    write(tmp_path, "story/structure/act.md", "张明追查旧案")
    write(tmp_path, "story/planning/detailed_scene_plans/scenes_ch1.md", "张明进入调查局")
    write(tmp_path, "story/content/chapters/chapter_1.md", "已有正文")
    return tmp_path


def edit_payload(snapshot, key):
    return json.loads(snapshot.files[key])


def test_rename_character_updates_all_structured_references_and_background(project):
    repo = LoreRepository(project)
    snapshot = repo.load_snapshot()
    cast = edit_payload(snapshot, CHARACTERS)
    cast["characters"][0]["name"] = "张远"
    preview = repo.save(snapshot, {CHARACTERS: encode(cast)})
    after = LoreRepository(project).load_snapshot()
    updated = edit_payload(after, CHARACTERS)
    assert updated["relationships"][0]["character1"] == "张远"
    assert updated["characters"][1]["opposes"]["character"] == "张远"
    assert updated["metadata"] == cast["metadata"]
    assert updated["characters"][0]["extra"] == {"keep": 7}
    assert updated["characters"][0]["goals"] == ["破案", "归家"]
    canonical = edit_payload(after, CONTRACT)["canonical_characters"][0]
    assert canonical == {"id": "CH1", "name": "张远", "aliases": []}
    assert LORE + "background_protagonist_张明.md" not in after.files
    assert after.files[LORE + "background_protagonist_张远.md"] == "张明的详细背景"
    assert after.files[BODY] == snapshot.files[BODY]
    assert any("仍含旧名" in item for item in preview.review_items)
    assert (project / "story/content/chapters/chapter_1.md").read_text(encoding="utf-8") == "已有正文"
    assert len(after.metadata["pending"]) == 3
    assert list((project / "system/lore_history").glob("*/transaction.json"))


def test_faction_rename_synchronizes_members_conflicts_and_registry(project):
    repo = LoreRepository(project)
    snapshot = repo.load_snapshot()
    factions = edit_payload(snapshot, FACTIONS)
    factions[0]["name"] = "调查署"
    repo.save(snapshot, {FACTIONS: encode(factions)})
    after = repo.load_snapshot()
    assert edit_payload(after, CHARACTERS)["characters"][0]["faction"] == "调查署"
    assert edit_payload(after, FACTIONS)[1]["conflicts"][0]["with"] == "调查署"
    assert edit_payload(after, CONTRACT)["canonical_factions"][0]["name"] == "调查署"


def test_registry_rename_updates_cards_and_references(project):
    repo = LoreRepository(project)
    snapshot = repo.load_snapshot()
    registry = edit_payload(snapshot, CONTRACT)
    registry["canonical_characters"][0]["name"] = "张远"
    repo.save(snapshot, {CONTRACT: encode(registry)})
    assert edit_payload(repo.load_snapshot(), CHARACTERS)["characters"][0]["name"] == "张远"


@pytest.mark.parametrize("kind", ["blank", "duplicate", "bad_reference", "wrong_type"])
def test_invalid_edits_keep_files_intact(project, kind):
    repo = LoreRepository(project)
    snapshot = repo.load_snapshot()
    cast = edit_payload(snapshot, CHARACTERS)
    person = cast["characters"][0]
    if kind == "blank":
        person["name"] = ""
    elif kind == "duplicate":
        person["name"] = "王柳"
    elif kind == "bad_reference":
        person["faction"] = "不存在的势力"
    else:
        person["age"] = "thirty"
    with pytest.raises(LoreEditError):
        repo.save(snapshot, {CHARACTERS: encode(cast)})
    assert repo.load_snapshot().files == snapshot.files


def test_body_background_and_rules_persist_without_model(project):
    repo = LoreRepository(project)
    snapshot = repo.load_snapshot()
    contract = edit_payload(snapshot, CONTRACT)
    contract["era"] = "近未来"
    contract["world_constraints"] = ["禁止时间旅行", "有代价的技术"]
    edits = {BODY: "# 新世界\n近未来的城。", CONTRACT: encode(contract),
             LORE + "background_protagonist_张明.md": "全新的背景"}
    repo.save(snapshot, edits)
    after = LoreRepository(project).load_snapshot()
    for rel, text in edits.items():
        assert after.files[rel] == text
    assert "近未来" in lore_context(project)
    assert "全新的背景" in lore_context(project)


def test_noop_does_not_create_revision_or_backup(project):
    repo = LoreRepository(project)
    snapshot = repo.load_snapshot()
    assert not repo.save(snapshot, {BODY: snapshot.files[BODY]}).changed_files
    assert not (project / META).exists()
    assert not (project / "system/lore_history").exists()


def test_external_write_is_detected_and_never_overwritten(project):
    repo = LoreRepository(project)
    snapshot = repo.load_snapshot()
    write(project, BODY, "另一个编辑器的修改")
    with pytest.raises(LoreEditError, match="文件已"):
        repo.save(snapshot, {BODY: "我的草稿"})
    assert (project / BODY).read_text(encoding="utf-8") == "另一个编辑器的修改"


def test_partial_write_failure_rolls_back_every_file(project, monkeypatch):
    repo = LoreRepository(project)
    snapshot = repo.load_snapshot()
    original_atomic = repo._atomic
    failed = False
    def fail_once(path, data):
        nonlocal failed
        if path == repo.path(BODY) and not failed:
            failed = True
            raise OSError("disk full")
        original_atomic(path, data)
    monkeypatch.setattr(repo, "_atomic", fail_once)
    with pytest.raises(OSError, match="disk full"):
        repo.save(snapshot, {BODY: "修改", LORE + "background_protagonist_张明.md": "背景修改"})
    assert repo.load_snapshot().files == snapshot.files
    assert not (project / META).exists()


def test_next_open_recovers_prepared_transaction(project):
    original = (project / BODY).read_bytes()
    write(project, "system/lore_history/interrupted/transaction.json", {
        "status": "prepared", "before": {BODY: base64.b64encode(original).decode()},
    })
    write(project, BODY, "只写了一半")
    LoreRepository(project).load_snapshot()
    assert (project / BODY).read_bytes() == original


def test_background_collision_rejected(project):
    write(project, LORE + "background_protagonist_张远.md", "另一个人的背景")
    repo = LoreRepository(project)
    snapshot = repo.load_snapshot()
    cast = edit_payload(snapshot, CHARACTERS)
    cast["characters"][0]["name"] = "张远"
    with pytest.raises(LoreEditError, match="背景文件已存在"):
        repo.save(snapshot, {CHARACTERS: encode(cast)})
    assert repo.load_snapshot().files == snapshot.files


def test_explicit_registry_mapping_when_legacy_name_has_no_match(project):
    contract = json.loads((project / CONTRACT).read_text(encoding="utf-8"))
    contract["canonical_characters"][0]["name"] = "旧登记名"
    write(project, CONTRACT, contract)
    repo = LoreRepository(project)
    snapshot = repo.load_snapshot()
    cast = edit_payload(snapshot, CHARACTERS)
    cast["characters"][0]["name"] = "张远"
    with pytest.raises(MappingRequired) as exc:
        repo.save(snapshot, {CHARACTERS: encode(cast)})
    assert len(exc.value.candidates) == 2
    repo.save(snapshot, {CHARACTERS: encode(cast)}, {"characters:0": 0})
    assert edit_payload(repo.load_snapshot(), CONTRACT)["canonical_characters"][0]["id"] == "CH1"


def test_legacy_missing_contract_can_edit_and_rename(project):
    (project / CONTRACT).unlink()
    repo = LoreRepository(project)
    snapshot = repo.load_snapshot()
    cast = edit_payload(snapshot, CHARACTERS)
    cast["characters"][0]["name"] = "新名"
    repo.save(snapshot, {CHARACTERS: encode(cast)})
    assert edit_payload(repo.load_snapshot(), CHARACTERS)["relationships"][0]["character1"] == "新名"


def test_regeneration_clears_only_changed_files_and_backups_lore(project):
    repo = LoreRepository(project)
    repo.save(repo.load_snapshot(), {BODY: "人工新设定"})
    @tracked_generation
    def generate(step, output_dir):
        write(Path(output_dir), "story/structure/act.md", "新大纲")
        return {"generated_files": ["story/structure/act.md", "story/content/chapters/chapter_1.md"]}
    generate("structure", str(project))
    pending = repo.review_status()["pending"]
    assert "story/structure/act.md" not in pending
    assert "story/content/chapters/chapter_1.md" in pending
    @tracked_generation
    def regenerate(step, output_dir):
        write(Path(output_dir), BODY, "重新生成")
    regenerate("lore", str(project))
    backups = list((project / "system/lore_history").glob("*/backup.json"))
    assert json.loads(backups[0].read_text(encoding="utf-8"))["files"][BODY] == "人工新设定"
    assert len(repo.review_status()["pending"]) == 3


def test_acknowledge_is_version_bound_and_new_edit_reopens_review(project):
    repo = LoreRepository(project)
    assert not repo.review_status().get("pending")
    repo.save(repo.load_snapshot(), {BODY: "第一次修改"})
    version = repo.review_status()["version"]
    repo.acknowledge(version)
    assert not LoreRepository(project).review_status()["pending"]
    repo.save(repo.load_snapshot(), {BODY: "第二次修改"})
    assert repo.review_status()["pending"]
    with pytest.raises(LoreEditError, match="版本"):
        repo.acknowledge(version)


def test_lore_change_invalidates_fact_baseline_even_if_only_background_changed(project):
    from core.generation.story_ledger import StoryLedgerManager
    manager = StoryLedgerManager(str(project))
    context = manager.load_design_context()
    from core.generation.story_ledger import source_hash
    bible = {"status": "ready", "generated_from_design": True, "source_hash": source_hash(context)}
    repo = LoreRepository(project)
    repo.save(repo.load_snapshot(), {LORE + "background_protagonist_张明.md": "新的背景事实"})
    assert manager.case_bible_needs_refresh(bible, manager.load_design_context())


def test_generation_lock_rejects_cross_thread_save(project):
    from concurrent.futures import ThreadPoolExecutor
    from filelock import Timeout
    repo = LoreRepository(project)
    snapshot = repo.load_snapshot()
    with repo.lock(), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(repo.save, snapshot, {BODY: "不能覆盖"})
        with pytest.raises(Timeout):
            future.result(timeout=5)
    assert repo.load_snapshot().files == snapshot.files


def test_structure_model_receives_saved_rules_cards_and_background(project, monkeypatch):
    import logging
    from core.generation import structure_pipeline
    from core.generation.stage_context import make_context
    from core.generation.stage_pipeline import GenerationHost
    repo = LoreRepository(project)
    snapshot = repo.load_snapshot()
    registry = edit_payload(snapshot, CONTRACT)
    registry["world_constraints"] = ["只有地下通道可以离开旧城"]
    cast = edit_payload(snapshot, CHARACTERS)
    cast["characters"][0]["background"] = "作者指定的侦查经历"
    repo.save(snapshot, {CONTRACT: encode(registry), CHARACTERS: encode(cast),
                         LORE + "background_protagonist_张明.md": "作者指定的详细过去"})
    prompts = []
    def fake_send(prompt, **kwargs):
        prompts.append(prompt)
        return "张明循着旧案改变自己。"
    monkeypatch.setattr(structure_pipeline, "send_prompt", fake_send)
    context = make_context(str(project), "test-model", {"genre": "Mystery"})
    host = GenerationHost(context, logging.getLogger("test.lore"))
    structure_pipeline.StructurePipeline(host)._generate_arcs(context)
    assert prompts
    assert "只有地下通道可以离开旧城" in prompts[0]
    assert "作者指定的侦查经历" in prompts[0]
    assert "作者指定的详细过去" in prompts[0]


def test_short_and_long_form_contexts_read_latest_registry(project):
    import logging
    from core.generation.short_story_pipeline import ShortStoryPipeline
    from core.generation.stage_context import make_context
    from core.generation.stage_pipeline import GenerationHost
    from agents.writing.chapter_writing_agent import ChapterWritingAgent
    repo = LoreRepository(project)
    snapshot = repo.load_snapshot()
    registry = edit_payload(snapshot, CONTRACT)
    registry["technology_level"] = "由作者修改的蒸汽技术"
    repo.save(snapshot, {CONTRACT: encode(registry)})
    context = make_context(str(project), "test-model", {"genre": "Mystery"})
    short = ShortStoryPipeline(GenerationHost(context, logging.getLogger("test.lore")))
    assert "由作者修改的蒸汽技术" in short._load_lore(str(project))
    # The long-form agent's normal context loader uses the same source bundle.
    agent = ChapterWritingAgent.__new__(ChapterWritingAgent)
    from core.config.directory_config import get_directory_manager
    agent.output_dir = str(project)
    agent.logger = logging.getLogger("test.lore")
    agent.dir_manager = get_directory_manager(str(project), use_new_structure=True)
    assert "由作者修改的蒸汽技术" in agent._load_writing_context()["lore"]


def test_unresolved_rename_findings_survive_another_save(project):
    repo = LoreRepository(project)
    snapshot = repo.load_snapshot()
    cast = edit_payload(snapshot, CHARACTERS)
    cast["characters"][0]["name"] = "张远"
    repo.save(snapshot, {CHARACTERS: encode(cast)})
    repo.save(repo.load_snapshot(), {BODY: "张远的新世界"})
    items = repo.review_status()["review_items"]
    assert any("story/structure/act.md" in text and "旧名" in text for text in items)
    assert not any(text.startswith(BODY + "：仍含旧名") for text in items)


def test_contract_location_rename_updates_territory_without_touching_prose(project):
    repo = LoreRepository(project)
    snapshot = repo.load_snapshot()
    registry = edit_payload(snapshot, CONTRACT)
    registry["canonical_locations"][0]["name"] = "新城"
    repo.save(snapshot, {CONTRACT: encode(registry)})
    assert edit_payload(repo.load_snapshot(), FACTIONS)[0]["territory"] == "新城"


def test_failure_after_generation_change_preserves_output_and_marks_review(project):
    repo = LoreRepository(project)
    @tracked_generation
    def generate(step, output_dir):
        write(Path(output_dir), BODY, "已生成的新设定")
        raise RuntimeError("后续步骤失败")
    with pytest.raises(RuntimeError):
        generate("lore", str(project))
    assert repo.load_snapshot().files[BODY] == "已生成的新设定"
    assert len(repo.review_status()["pending"]) == 3


def test_legacy_faction_name_and_shape_are_preserved_and_synchronized(project):
    old = json.loads((project / FACTIONS).read_text(encoding="utf-8"))
    old[0]["faction_name"] = old[0]["name"]
    write(project, FACTIONS, {"factions": old, "metadata": {"old": True}})
    repo = LoreRepository(project)
    snapshot = repo.load_snapshot()
    data = edit_payload(snapshot, FACTIONS)
    data["factions"][0]["name"] = "新调查局"
    repo.save(snapshot, {FACTIONS: encode(data)})
    saved = edit_payload(repo.load_snapshot(), FACTIONS)
    assert saved["factions"][0]["faction_name"] == "新调查局"
    assert saved["metadata"] == {"old": True}


# ---------------------------------------------------------------- 撤回与恢复
def test_old_project_without_change_log_opens_and_has_nothing_to_undo(project):
    repo = LoreRepository(project)
    assert repo.change_log() == {"undo": None, "redo": None}
    with pytest.raises(LoreEditError, match="撤回"):
        repo.undo()


def test_regeneration_can_be_undone_and_redone(project):
    repo = LoreRepository(project)
    repo.save(repo.load_snapshot(), {BODY: "人工改过的世界观"})
    @tracked_generation
    def regenerate(step, output_dir, action=""):
        write(Path(output_dir), BODY, "新生成的世界观")
        write(Path(output_dir), LORE + "background_protagonist_新人.md", "新人背景")
    regenerate("lore", str(project), action="lore")
    assert repo.change_log()["undo"]["label"] == "生成世界观"

    repo.undo()
    files = repo.load_snapshot().files
    assert files[BODY] == "人工改过的世界观"
    assert LORE + "background_protagonist_新人.md" not in files
    assert repo.change_log()["redo"]["label"] == "生成世界观"
    assert repo.change_log()["undo"]["label"] == "编辑世界观"

    repo.redo()
    files = repo.load_snapshot().files
    assert files[BODY] == "新生成的世界观"
    assert files[LORE + "background_protagonist_新人.md"] == "新人背景"
    assert repo.change_log()["redo"] is None


def test_new_change_after_undo_cuts_the_redo_chain_but_keeps_snapshots(project):
    repo = LoreRepository(project)
    repo.save(repo.load_snapshot(), {BODY: "第一版"})
    repo.undo()
    snapshots = len(list((project / "system/lore_history").glob("*/state.json")))
    repo.save(repo.load_snapshot(), {BODY: "另一版"})
    assert repo.change_log()["redo"] is None
    assert len(list((project / "system/lore_history").glob("*/state.json"))) == snapshots + 2


def test_undo_of_rename_restores_cards_registry_and_background(project):
    repo = LoreRepository(project)
    snapshot = repo.load_snapshot()
    cast = edit_payload(snapshot, CHARACTERS)
    cast["characters"][0]["name"] = "张远"
    repo.save(snapshot, {CHARACTERS: encode(cast)})
    assert repo.change_log()["undo"]["label"] == "改名：张明 → 张远"
    repo.undo()
    restored = repo.load_snapshot().files
    assert restored == snapshot.files


def test_acknowledge_can_be_undone(project):
    repo = LoreRepository(project)
    repo.save(repo.load_snapshot(), {BODY: "修改"})
    status = repo.review_status()
    repo.acknowledge(status["version"])
    assert not repo.review_status()["pending"]
    assert repo.change_log()["undo"]["label"] == "全部标为已看过"
    repo.undo()
    after = repo.review_status()
    assert after["pending"].keys() == status["pending"].keys()
    assert after["review_items"] == status["review_items"]
    assert after["version"] > status["version"]


def test_undo_marks_downstream_rewritten_meanwhile_for_review(project):
    repo = LoreRepository(project)
    @tracked_generation
    def regenerate(step, output_dir, action=""):
        write(Path(output_dir), FACTIONS, [{"name": "新势力", "description": "新"}])
    regenerate("lore", str(project), action="factions")
    repo.acknowledge(repo.review_status()["version"])
    repo.undo()  # 撤回「全部标为已看过」
    repo.undo()  # 撤回「生成势力」
    assert edit_payload(repo.load_snapshot(), FACTIONS)[0]["name"] == "调查局"
    assert not repo.review_status().get("pending")

    regenerate("lore", str(project), action="factions")
    write(project, "story/structure/act.md", "按新势力重写的大纲")
    repo.acknowledge(repo.review_status()["version"])
    repo.undo()
    repo.undo()
    status = repo.review_status()
    assert "story/structure/act.md" in status["pending"]
    assert any("撤回" in item for item in status["review_items"])


def test_undo_reverts_generation_receipts(project):
    receipts = project / "system/lore_generation/results"
    @tracked_generation
    def regenerate(step, output_dir, action=""):
        write(Path(output_dir), FACTIONS, [{"name": "新势力", "description": "新"}])
        write(receipts.parent.parent.parent, "system/lore_generation/results/abc123.json",
              {"id": "abc123", "kind": "factions", "status": "complete", "candidate": "", "created_at": 9e12})
        return {"lore_results": [{"id": "abc123"}]}
    regenerate("lore", str(project), action="factions")
    repo = LoreRepository(project)
    repo.undo()
    assert json.loads((receipts / "abc123.json").read_text(encoding="utf-8"))["reverted"] is True
    repo.redo()
    assert json.loads((receipts / "abc123.json").read_text(encoding="utf-8"))["reverted"] is False


def test_undo_is_rejected_while_generation_holds_the_lock(project):
    from concurrent.futures import ThreadPoolExecutor
    from filelock import Timeout
    repo = LoreRepository(project)
    repo.save(repo.load_snapshot(), {BODY: "修改"})
    with repo.lock(), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(repo.undo)
        with pytest.raises(Timeout):
            future.result(timeout=5)
    assert repo.load_snapshot().files[BODY] == "修改"
