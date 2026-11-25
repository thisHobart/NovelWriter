import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.short_video.v2.media_repository import MediaRepository
from core.short_video.v2.media_workflows import (
    WORKFLOW_DIRECTORY,
    MediaConfig,
    load_shot_drafts,
    parse_video_workflow,
    read_api_workflow,
)


def row_labels(form) -> list[str]:
    """A form's row labels as read, without the color dot."""
    from PySide6.QtWidgets import QFormLayout

    return [
        re.sub(r"<[^>]+>|●|&nbsp;", "", form.itemAt(row, QFormLayout.LabelRole).widget().text()).strip()
        for row in range(form.rowCount())
    ]


def test_dual_scene_panel_reads_like_the_other_workflows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication, QFormLayout

    from core.gui.pages.short_video import media_preview
    from core.gui.pages.short_video.task_channels import TaskChannels
    from core.gui.pages.short_video.video_widgets import SLOT_MIN_WIDTH

    app = QApplication.instance() or QApplication([])
    graph = read_api_workflow(WORKFLOW_DIRECTORY / "scail2_dual_scene.json")
    catalog = MediaConfig()
    workflow_id = catalog.put_workflow("双人动作迁移＋自定义场景", json.dumps(graph))
    catalog.save(tmp_path)
    template = parse_video_workflow(graph)
    keys = {parameter.role: parameter.key for parameter in template.parameters}
    submitted = []
    workbench = SimpleNamespace(
        output_dir=str(tmp_path),
        perform=lambda action: action(),
        configure_comfy=lambda: None,
        submit=lambda work, _: submitted.append(work.command),
        tasks=TaskChannels(),
    )
    panel = media_preview.MediaPreview(workbench)
    panel.set_target({"shot_id": "shot"})
    panel.refresh()
    panel.resize(1000, 750)
    panel.show()
    app.processEvents()
    marks = media_preview.DUAL_MARKS
    try:
        assert [slot.caption for slot in panel.role_grid.visible_cells()] == ["角色 A", "角色 B"]
        assert [slot.caption for slot in panel.scene_grid.visible_cells()] == ["当前场景"]
        assert all(slot.thumb.contain for slot in panel.image_fields.values())
        # As small as the reference cards of the other workflows, not stretched across the page.
        assert all(slot.width() < 2 * SLOT_MIN_WIDTH for slot in panel.image_fields.values())
        assert [slot.thumb.mark for slot in panel.role_slots] == [marks["role_a"], marks["role_b"]]
        # Person descriptions and 画幅 are rows of the video card, like 「原视频人物描述」 for one person.
        assert row_labels(panel.parameter_form) == [
            "工作流", "画幅", "原片人物 A 描述", "原片人物 B 描述", "保留原声",
        ]
        description_labels = [
            panel.parameter_form.itemAt(row, QFormLayout.LabelRole).widget().text() for row in (2, 3)
        ]
        assert marks["role_a"] in description_labels[0] and marks["role_b"] in description_labels[1]
        assert row_labels(panel.advanced_form) == ["输出长边（像素）", "采样步数", "随机种子"]
        assert panel.advanced.title.text() == "高级参数"
        for role in ("scene_transition", "action_end", "hold_tail", "tracking_cuts", "clip_ranges"):
            assert keys[role] not in panel.parameter_fields
        # The prompt sits beside the video card without opening anything.
        assert not panel.advanced.expanded()
        assert panel.prompt.isVisible()
        assert panel.generate_prompt_button.isHidden()
        assert panel.prompt_status.text() == "请选择原视频、角色 A、角色 B、场景参考图"
        assert not panel.swap_roles_button.isEnabled()
        assert not panel.tracking_preview_button.isEnabled()
        assert panel.open_tracking_button.isVisible() and not panel.open_tracking_button.isEnabled()
        assert panel.tracking_status.text().startswith("选好原视频后")

        source = tmp_path / "source.mp4"
        source.write_bytes(b"\x00\x00\x00\x18ftypisom" + bytes(64))
        monkeypatch.setattr(media_preview.QFileDialog, "getOpenFileName", lambda *args: (str(source), ""))
        panel.choose_driving_video()
        assert panel.tracking_preview_button.isEnabled()
        assert panel.tracking_status.text().startswith("提取动作预览后")
        assert not panel.generate_button.isEnabled()
        panel.generate_tracking_preview()
        preview = submitted.pop()
        assert preview["tracking_preview"] is True
        assert preview["images"] == {}
        repository = MediaRepository(tmp_path)
        path = repository.resolve("videos/tracking.mp4")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source.read_bytes())
        repository.save_job({
            "job_id": "preview", "created_at": "2026-11-24T11:00:00+08:00", "status": "succeeded",
            "input_snapshot": preview, "outputs": [{"relative_path": "videos/tracking.mp4"}],
        })
        panel.refresh_result()
        assert panel.video_path is None
        assert panel.open_tracking_button.isEnabled()
        assert panel.tracking_preview_button.text() == "重新提取动作预览"
        assert panel.tracking_status.text().startswith("动作预览已提取")
        opened = []
        monkeypatch.setattr(media_preview.QDesktopServices, "openUrl", lambda url: opened.append(url) or True)
        panel.open_tracking_preview()
        assert Path(opened[0].toLocalFile()) == path
        panel.parameter_fields["443.text"].setText("person in a black shirt")
        panel.refresh_tracking_preview()
        assert not panel.open_tracking_button.isEnabled()
        assert panel.tracking_status.text().startswith("动作预览是按之前的设置提取的，已过期")
        assert panel.tracking_preview_button.text() == "重新提取动作预览"
        image = QImage(16, 12, QImage.Format_RGB32)
        for index, key in enumerate(panel.image_fields):
            assert not panel.generate_button.isEnabled()
            image.fill(0xFF0000 + index * 50)
            path = tmp_path / f"chosen-{index}.png"
            assert image.save(str(path))
            panel.import_image(str(path), key)
        assert panel.generate_button.isEnabled()
        assert panel.slots_note.text() == "已选 3 / 3"
        assert panel.prompt_status.text().startswith("蓝色 → chosen-0，红色 → chosen-1")
        assert [p["reference"] for p in panel.chat_inputs()["pictures"]] == [
            "角色 A", "角色 B", "当前场景",
        ]

        # 「对调」 trades the two pictures; the descriptions, and so the colors, stay put.
        before = panel.image_selection()
        first, second = (slot.node_id for slot in panel.role_slots)
        panel.swap_roles_button.click()
        after = panel.image_selection()
        assert (after[first], after[second]) == (before[second], before[first])
        assert panel.parameter_fields["443.text"].text() == "person in a black shirt"
        assert panel.prompt_status.text().startswith("蓝色 → chosen-1，红色 → chosen-0")
        panel.swap_roles_button.click()
        assert panel.image_selection() == before

        description_b = panel.parameter_fields["source_b_text.text"]
        description_b.setText(" ")
        assert not panel.generate_button.isEnabled()
        assert panel.prompt_status.text() == "请填写原片人物 B 描述"
        description_b.setText("person in a white outfit")
        assert panel.generate_button.isEnabled()

        panel.prompt.clear()
        panel.generate()
        images = {key: value["image_id"] for key, value in submitted[0]["images"].items()}
        assert images == panel.image_selection()
        assert submitted[0]["driving_video"]["video_id"] == panel.driving_video["video_id"]
        assert submitted[0]["parameters"][keys["single_shot"]] is True
        assert submitted[0]["prompt"] == media_preview.DUAL_SHOT_PROMPT
        panel.remember_draft()
        draft = load_shot_drafts(tmp_path)["shot"][workflow_id]
        assert draft["images"] == images
        assert draft["parameters"][keys["single_shot"]] is True
        panel.scene_grid.visible_cells()[0].set_image(None)
        assert not panel.generate_button.isEnabled()
        assert panel.prompt_status.text() == "请选择场景参考图"
    finally:
        panel.close()
        app.processEvents()
