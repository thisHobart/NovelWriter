"""Exact shot/asset/view lookup and read-only browsing in the prompt workspace."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from core.short_video.v2 import llm
from core.short_video.v2.models import ASSET_KINDS, VIEWS
from core.short_video.v2.prompt_compiler import finalize_asset_prompts
from core.short_video.v2.workflow import WorkflowService
from test_video_v2_repository import project, prompt_page, prompt_project  # noqa: F401


def snapshot(repository):
    return {str(p.relative_to(repository.root)): p.read_bytes()
            for p in repository.root.rglob("*") if p.is_file()}


@pytest.fixture
def scenario(prompt_project):
    repository = prompt_project
    board = repository.current("storyboard")
    catalog = repository.current("asset_catalog")
    specs = [repository.current(f"{kind}:asset_{entity_kind}:base")
             for entity_kind, kind in ASSET_KINDS.items()]
    content = deepcopy(catalog["content"])
    character = next(e for e in content["entities"] if e["kind"] == "character")
    character["variants"].append({"variant_id": "summer", "label": "夏装"})
    twin = {**deepcopy(character), "asset_id": "asset_twin"}
    voice = {**deepcopy(character), "asset_id": "asset_voice", "name": "画外音", "usage": "voice_only"}
    unused = {"asset_id": "asset_unused", "name": "未使用的盒子", "kind": "prop",
              "variants": [{"variant_id": "base", "label": "常态"}]}
    content["entities"].extend([twin, voice, unused])
    saved_catalog = repository.save("asset_catalog", content, catalog["dependency_refs"],
                                    expected_revision=repository.load()["revision"])
    for spec in specs:
        entries = [deepcopy(spec["content"])]
        if spec["kind"] == "character_spec":
            entries.extend([
                {**deepcopy(spec["content"]), "variant_id": "summer"},
                {**deepcopy(spec["content"]), "asset_id": "asset_twin"},
            ])
        for entry in entries:
            for view in entry["views"]:
                view["positive_prompt"] = f"{entry['asset_id']} / {entry['variant_id']} / {view['view_id']}"
                view["negative_prompt"] = f"禁止改变 {entry['asset_id']} 的 {view['view_id']}"
            deps = {**spec["dependency_refs"], "asset_catalog": saved_catalog["id"]}
            repository.save(spec["kind"], finalize_asset_prompts(spec["kind"], entry), deps,
                            expected_revision=repository.load()["revision"])
    shots = [deepcopy(board["content"]["shots"][0]) for _ in range(3)]
    shots[0]["characters"] *= 2
    shots[0]["props"] *= 2
    shots[0]["dialogue"] = [{"character_id": "asset_voice", "text": "门外有人。", "voice_over": True}]
    shots[1].update(shot_id="shot_2", characters=[{"asset_id": "asset_character", "variant_id": "summer"}], props=[])
    shots[2].update(shot_id="shot_3", characters=[{"asset_id": "asset_twin", "variant_id": "base"}], props=[])
    repository.save("storyboard", {"shots": shots},
                    {**board["dependency_refs"], "asset_catalog": saved_catalog["id"]},
                    expected_revision=repository.load()["revision"])
    WorkflowService(repository).compile_storyboard_prompts(expected_revision=repository.load()["revision"])
    return repository


def test_exact_assets_order_state_views_and_read_only_lookup(scenario, monkeypatch):
    sent = Mock(side_effect=AssertionError("browsing must not call a model"))
    monkeypatch.setattr(llm.ScriptLLM, "_request", sent)
    before = snapshot(scenario)
    service = WorkflowService(scenario)
    assets = service.shot_assets("shot_1")
    assert [(a["kind"], a["asset_id"], a["variant_id"]) for a in assets] == [
        ("scene", "asset_scene", "base"), ("character", "asset_character", "base"),
        ("prop", "asset_prop", "base"),
    ]
    assert service.shot_assets("missing_shot") == []
    compiled = scenario.current("prompt_bundle")["content"]["shots"][0]
    for asset in assets:
        ref = next(r for r in compiled["asset_refs"] if r["asset_id"] == asset["asset_id"])
        assert asset["spec_id"] == ref["spec_version_id"]
        assert [v["view_id"] for v in asset["views"]] == [v[0] for v in VIEWS[asset["kind"]]]
        for view in asset["views"]:
            assert view["available"]
            assert view["positive_prompt"] == f"{asset['asset_id']} / {asset['variant_id']} / {view['view_id']}"
    summer = service.shot_assets("shot_2")[1]
    twin = service.shot_assets("shot_3")[1]
    assert summer["name"] == twin["name"] == assets[1]["name"]
    assert summer["variant_label"] == "夏装"
    assert "asset_character / summer" in summer["views"][0]["positive_prompt"]
    assert "asset_twin / base" in twin["views"][0]["positive_prompt"]
    assert snapshot(scenario) == before
    sent.assert_not_called()


def test_missing_spec_does_not_use_another_state_and_retains_planned_views(scenario):
    with scenario.transaction() as state:
        state["current"].pop("character_spec:asset_character:summer")
    assert scenario.current("prompt_bundle") is None
    before = snapshot(scenario)
    service = WorkflowService(scenario)
    scene, summer = service.shot_assets("shot_2")
    assert scene["spec_id"] == scenario.current("scene_spec:asset_scene:base")["id"]
    assert summer["spec_id"] is None
    assert len(summer["views"]) == 4
    assert all(not v["available"] and v["positive_prompt"] == "" for v in summer["views"])
    assert all(v["available"] for v in service.shot_assets("shot_1")[1]["views"])
    assert snapshot(scenario) == before


def test_compiled_lookup_does_not_fall_back_when_exact_reference_is_wrong(scenario, monkeypatch):
    original = scenario.current

    def current(key, **kwargs):
        record = original(key, **kwargs)
        if key == "prompt_bundle":
            record = deepcopy(record)
            for ref in record["content"]["shots"][0]["asset_refs"]:
                if ref["asset_id"] == "asset_character":
                    ref["spec_version_id"] = original("character_spec:asset_twin:base")["id"]
        return record

    monkeypatch.setattr(scenario, "current", current)
    character = WorkflowService(scenario).shot_assets("shot_1")[1]
    assert character["spec_id"] is None
    assert all(not v["available"] for v in character["views"])


def test_fallback_rejects_spec_from_another_catalog(scenario, monkeypatch):
    with scenario.transaction() as state:
        state["current"].pop("prompt_bundle")
    original = scenario.record
    target = scenario.current("scene_spec:asset_scene:base")["id"]

    def record(record_id, **kwargs):
        value = original(record_id, **kwargs)
        if record_id == target:
            value = deepcopy(value)
            value["dependency_refs"]["asset_catalog"] = "another_catalog"
        return value

    monkeypatch.setattr(scenario, "record", record)
    scene = WorkflowService(scenario).shot_assets("shot_1")[0]
    assert scene["spec_id"] is None
    assert not any(v["available"] for v in scene["views"])


def test_inline_view_copy_preserves_unsaved_shot_and_video_selection(scenario, prompt_page, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from test_comfyui_video import save_ui_image

    page, started = prompt_page
    page.resize(1400, 1000)
    page.tabs.setCurrentIndex(3)
    page.show()
    app = QApplication.instance()
    app.processEvents()
    panel = page.shot_assets
    assert panel.table.rowCount() == 3 and panel.details.isHidden()
    path = tmp_path / "manual.png"
    save_ui_image(path)
    page.media.import_image(str(path), "137")
    page.media.prompt.setPlainText("Use <Picture 1>.")
    media_before = (page.media.image_selection(), page.media.prompt.toPlainText(),
                    dict(page.media.prompt_image_bindings), page.media.image_labels["137"].text())
    page.editor.positive.setPlainText("镜头正文的未保存修改")
    before = snapshot(scenario)
    sent = Mock(side_effect=AssertionError("browsing must not call a model"))
    monkeypatch.setattr(llm.ScriptLLM, "_request", sent)
    rect = panel.table.visualItemRect(panel.table.item(1, 0))
    QTest.mouseClick(panel.table.viewport(), Qt.LeftButton, pos=rect.center())
    assert not panel.details.isHidden()
    assert panel.positive.isReadOnly() and panel.negative.isReadOnly()
    panel.views.setCurrentIndex(panel.views.findData("body_side"))
    panel.copy_positive.click()
    assert app.clipboard().text() == "asset_character / base / body_side"
    panel.copy_negative.click()
    assert app.clipboard().text() == "禁止改变 asset_character 的 body_side"
    assert page.dirty and page.editor.positive.toPlainText() == "镜头正文的未保存修改"
    assert media_before == (page.media.image_selection(), page.media.prompt.toPlainText(),
                            page.media.prompt_image_bindings, page.media.image_labels["137"].text())
    assert snapshot(scenario) == before and started == []
    sent.assert_not_called()
    panel.collapse_button.click()
    assert panel.details.isHidden()


def test_switching_shots_clears_details_and_hides_assets_on_other_tabs(scenario, prompt_page):
    page, _ = prompt_page
    page.tabs.setCurrentIndex(3)
    panel = page.shot_assets
    panel.show_asset(1)
    page.objects.setCurrentIndex(page.objects.findData("shot_2"))
    assert panel.details.isHidden() and panel.positive.toPlainText() == ""
    assert panel.table.rowCount() == 2
    panel.show_asset(1)
    assert "夏装" in panel.title.text()
    assert "asset_character / summer" in panel.positive.toPlainText()
    page.objects.setCurrentIndex(page.objects.findData("shot_3"))
    assert panel.details.isHidden()
    panel.show_asset(1)
    assert "asset_twin / base" in panel.positive.toPlainText()
    page.tabs.setCurrentIndex(0)
    assert panel.isHidden()


def test_missing_first_view_opens_first_available_and_missing_asset_is_explicit(scenario, prompt_page):
    record = scenario.current("character_spec:asset_character:base")
    content = deepcopy(record["content"])
    content["views"][0]["positive_prompt"] = ""
    content["views"][0]["negative_prompt"] = ""
    scenario.save(record["kind"], content, record["dependency_refs"], expected_revision=scenario.load()["revision"])
    with scenario.transaction() as state:
        state["current"].pop("prop_spec:asset_prop:base")
    page, _ = prompt_page
    page.tabs.setCurrentIndex(3)
    panel = page.shot_assets
    panel.show_asset(1)
    assert panel.views.currentData() == "body_front"
    panel.views.setCurrentIndex(panel.views.findData("head_front"))
    assert panel.status.text() == "提示词尚未生成"
    assert not panel.copy_positive.isEnabled() and not panel.copy_negative.isEnabled()
    panel.show_asset(2)
    assert panel.status.text() == "提示词尚未生成" and panel.views.count() == 3
    assert "提示词尚未生成" in panel.table.item(2, 1).text()


def test_manual_added_and_reordered_shots_refresh_by_id(scenario, prompt_page):
    from core.gui.pages.short_video.v2_editor import StoryboardEditor
    page, _ = prompt_page
    page.tabs.setCurrentIndex(3)
    board = scenario.current("storyboard")
    catalog = scenario.current("asset_catalog")["content"]["entities"]
    editor = StoryboardEditor()
    editor.catalog = catalog
    editor.names = {e["asset_id"]: e["name"] for e in catalog}
    editor.set_content(board["content"])
    editor.table.selectRow(1)
    editor.add_shot()
    content = editor.value()
    added = content["shots"][2]["shot_id"]
    scenario.save("storyboard", content, board["dependency_refs"], expected_revision=scenario.load()["revision"])
    page.refresh()
    page.objects.setCurrentIndex(page.objects.findData(added))
    assert page.shot_assets.table.rowCount() == 2
    assert "夏装" in page.shot_assets.table.item(1, 0).text()
    page.shot_assets.show_asset(1)
    board = scenario.current("storyboard")
    content = deepcopy(board["content"])
    content["shots"].reverse()
    scenario.save("storyboard", content, board["dependency_refs"], expected_revision=scenario.load()["revision"])
    page.refresh()
    assert page.objects.currentData() == added
    assert page.shot_assets.details.isHidden()
    assert "夏装" in page.shot_assets.table.item(1, 0).text()
    editor.close()


def test_no_storyboard_hides_assets_without_writing(project, prompt_project, prompt_page):
    with project.transaction() as state:
        state["current"].pop("storyboard")
    before = snapshot(project)
    page, _ = prompt_page
    page.tabs.setCurrentIndex(3)
    assert page.shot_assets.isHidden() and page.objects.count() == 0
    assert WorkflowService(project).shot_assets("shot_1") == []
    assert snapshot(project) == before
