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


def test_shot_numbers_wrap_onto_more_rows_instead_of_running_off_the_edge():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from core.gui.pages.short_video.shot_header import ShotHeader

    app = QApplication.instance() or QApplication([])
    header = ShotHeader()
    resized = Mock()
    header.resized.connect(resized)
    chips = header.chips
    chips.set_count(12)
    chips.set_current(11)
    header.resize(1000, 200)
    header.show()
    app.processEvents()
    resized.reset_mock()

    header.resize(600, 200)  # Beside a widened chat.
    app.processEvents()
    for chip in chips.chips:
        assert chip.isVisible() and chip.geometry().right() < chips.width()
    assert chips.chips[11].isChecked() and chips.chips[11].y() > chips.chips[0].y()
    assert chips.rows == 2 and resized.called  # The page re-measures its height.
    assert header.layout().heightForWidth(600) > header.layout().heightForWidth(1000)
    chips.chips[5].click()
    assert chips.chips[5].isChecked()

    chips.set_count(3)
    app.processEvents()
    assert chips.rows == 1 and len(chips.findChildren(type(chips.chips[0]))) >= 3
    header.close()


def picker_texts(page, node_id="137"):
    return [action.text() for action in page.media.picker_menu(node_id).actions() if action.text()]


def test_shot_tab_puts_chips_header_and_shot_assets_in_the_video_area(scenario, prompt_page):
    page, started = prompt_page
    page.resize(1400, 1000)
    page.tabs.setCurrentIndex(3)
    page.show()
    from PySide6.QtWidgets import QApplication

    QApplication.instance().processEvents()
    board = scenario.current("storyboard")["content"]["shots"]
    assert page.body_stack.currentWidget() is page.media
    assert page.selector_bar.isHidden() and page.action_bar.isHidden() and page.status.isHidden()
    chips = page.shot_header.chips
    assert [chip.text() for chip in chips.chips] == ["01", "02", "03"]
    assert chips.chips[0].isChecked()
    assert page.shot_header.title.text() == "镜头 01"
    assert page.shot_header.duration.text() == f"分镜 {board[0]['duration_ms'] / 1000:g} 秒"
    assert page.shot_header.action.toolTip() == " ".join(board[0]["action"].split())
    assert page.media.shot_duration_ms == board[0]["duration_ms"]
    texts = picker_texts(page)
    assert [text.split(" · ")[0] for text in texts if text != "从文件选择…"][:1] == ["场景"]
    assert any(text.startswith("人物 · ") for text in texts)
    assert any(text.startswith("道具 · ") for text in texts)
    # The image prompts are folded under the video prompt, with their own buttons.
    assert page.image_fold.isVisibleTo(page) and not page.image_fold.expanded()
    assert page.image_fold.body.isAncestorOf(page.editor)
    assert page.image_fold.body.isAncestorOf(page.save_button)
    assert page.save_button.isVisibleTo(page.image_fold.body)
    chips.chips[1].click()
    assert page.objects.currentData() == "shot_2" and chips.chips[1].isChecked()
    assert page.shot_header.title.text() == "镜头 02"
    assert any("夏装" in text for text in picker_texts(page))
    page.tabs.setCurrentIndex(0)
    assert page.body_stack.currentWidget() is page.text_body
    assert page.text_scroll.widget().isAncestorOf(page.editor)
    assert page.action_bar.isAncestorOf(page.save_button) and page.selector_bar.isVisibleTo(page)
    assert started == []


def test_folded_image_prompt_edits_stay_marked_and_switching_shots_saves_them(scenario, prompt_page):
    page, _ = prompt_page
    page.tabs.setCurrentIndex(3)
    page.image_fold.head.click()
    assert page.image_fold.expanded()
    page.editor.positive.setPlainText("镜头正文的未保存修改")
    assert page.dirty and "有未保存修改" in page.image_fold.note.text()
    assert page.shot_header.notice.text() == "镜头画面提示词有未保存修改"
    before = snapshot(scenario)
    page.shot_header.chips.chips[2].click()
    # The fixture saves on a switch; the saved bundle holds the edit and shot 3 shows.
    assert snapshot(scenario) != before and not page.dirty
    assert page.objects.currentData() == "shot_3" and page.shot_header.title.text() == "镜头 03"
    saved = scenario.current("prompt_bundle")["content"]["shots"][0]["resolved_prompt"]
    assert saved == "镜头正文的未保存修改"
    assert page.image_fold.note.text() == "正向 · 负面" and page.shot_header.notice.isHidden()
    assert page.image_fold.expanded()


def test_refused_shot_switch_keeps_the_chip_on_the_current_shot(scenario, prompt_page):
    page, _ = prompt_page
    page.tabs.setCurrentIndex(3)
    page.workbench.resolve_page = lambda page: False
    page.editor.positive.setPlainText("未保存")
    page.shot_header.chips.chips[1].click()
    assert page.objects.currentData() == "shot_1" and page.shot_header.chips.chips[0].isChecked()


def test_missing_views_show_as_unavailable_picker_entries(scenario, prompt_page):
    with scenario.transaction() as state:
        state["current"].pop("prop_spec:asset_prop:base")
    page, _ = prompt_page
    page.tabs.setCurrentIndex(3)
    texts = picker_texts(page)
    props = [text for text in texts if text.startswith("道具 · ")]
    assert props and all(text.endswith("（未生成图片）") for text in props)


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
    # The new shot keeps only the scene; its required text is typed in the form.
    editor.size_input.setText("近景")
    for field, value in [("camera_movement", "固定"), ("action", "放下杯子")]:
        editor.detail_fields[field].setPlainText(value)
    content = editor.value()
    added = content["shots"][2]["shot_id"]
    scenario.save("storyboard", content, board["dependency_refs"], expected_revision=scenario.load()["revision"])
    page.refresh()
    assert len(page.shot_header.chips.chips) == 4
    page.shot_header.chips.chips[2].click()
    assert page.objects.currentData() == added
    assert [a["kind"] for a in page.media.shot_assets] == ["scene"]
    board = scenario.current("storyboard")
    content = deepcopy(board["content"])
    content["shots"].reverse()
    scenario.save("storyboard", content, board["dependency_refs"], expected_revision=scenario.load()["revision"])
    page.refresh()
    assert page.objects.currentData() == added
    assert page.shot_header.chips.chips[1].isChecked() and page.shot_header.title.text() == "镜头 02"
    assert [a["kind"] for a in page.media.shot_assets] == ["scene"]
    editor.close()


def test_no_storyboard_hides_the_shot_header_without_writing(project, prompt_project, prompt_page):
    with project.transaction() as state:
        state["current"].pop("storyboard")
    before = snapshot(project)
    page, _ = prompt_page
    page.tabs.setCurrentIndex(3)
    assert page.shot_header.isHidden() and page.objects.count() == 0
    assert page.media.shot_assets == [] and not page.status.isHidden()
    assert page.image_fold.isHidden()
    assert WorkflowService(project).shot_assets("shot_1") == []
    assert snapshot(project) == before
