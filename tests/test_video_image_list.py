"""Reference-image lists can drop entries without deleting files other features still use."""

import os
from types import SimpleNamespace

import pytest

from core.short_video.v2.media_repository import MediaRepository
from core.short_video.v2.models import WorkspaceError


def png(tmp_path, name, color):
    from PySide6.QtGui import QImage

    image = QImage(8, 8, QImage.Format_RGB32)
    image.fill(color)
    path = tmp_path / name
    assert image.save(str(path))
    return path


@pytest.fixture
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_hidden_images_leave_the_list_but_keep_record_and_file(tmp_path, app):
    repository = MediaRepository(tmp_path)
    first = repository.import_image(png(tmp_path, "a.png", 0xFF0000))
    second = repository.import_image(png(tmp_path, "b.png", 0x00FF00))
    repository.set_image_hidden(first["image_id"], True)
    assert [r["image_id"] for r in repository.images()] == [second["image_id"]]
    assert len(repository.images(include_hidden=True)) == 2
    assert repository.image(first["image_id"]) == first
    assert repository.resolve(first["relative_path"]).exists()
    repository.set_image_hidden(first["image_id"], False)
    assert len(repository.images()) == 2
    with pytest.raises((WorkspaceError, OSError)):
        repository.set_image_hidden("0" * 32, True)


def popup_combo(app):
    from core.gui.pages.short_video.removable_combo import RemovableComboBox

    combo = RemovableComboBox()
    combo.addItem("请选择图片", None)
    for name in ("场景1.png", "三视角.png", "小羊.png"):
        combo.addItem(name, name)
    combo.setCurrentIndex(2)
    combo.resize(300, 30)
    combo.show()
    combo.showPopup()
    app.processEvents()
    return combo


def click(app, combo, row, *, on_remove):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from core.gui.pages.short_video.removable_combo import remove_rect

    view = combo.view()
    rect = view.visualRect(combo.model().index(row, 0))
    point = remove_rect(rect).center() if on_remove else rect.adjusted(0, 0, -60, 0).center()
    QTest.mouseClick(view.viewport(), Qt.LeftButton, Qt.NoModifier, point)
    app.processEvents()


def test_clicking_the_cross_requests_removal_without_selecting_or_closing(app):
    combo = popup_combo(app)
    requested = []
    combo.remove_requested.connect(requested.append)
    click(app, combo, 1, on_remove=True)
    assert requested == ["场景1.png"]
    assert combo.currentIndex() == 2 and combo.view().isVisible()
    # The placeholder row has no cross; clicking where it would be requests nothing.
    click(app, combo, 0, on_remove=True)
    assert requested == ["场景1.png"]
    click(app, combo, 0, on_remove=False)
    assert combo.currentIndex() == 0 and not combo.view().isVisible()
    combo.close()


def test_owner_removal_keeps_the_popup_open_with_remaining_rows(app):
    combo = popup_combo(app)
    combo.remove_requested.connect(lambda data: combo.removeItem(combo.findData(data)))
    click(app, combo, 3, on_remove=True)
    assert [combo.itemText(i) for i in range(combo.count())] == ["请选择图片", "场景1.png", "三视角.png"]
    assert combo.view().isVisible() and combo.currentData() == "三视角.png"
    click(app, combo, 1, on_remove=False)
    assert combo.currentData() == "场景1.png" and not combo.view().isVisible()
    combo.close()


def test_video_page_removes_an_image_from_every_slot_and_asset_use_restores_it(tmp_path, app):
    from core.gui.pages.short_video.media_preview import MediaPreview
    from core.gui.pages.short_video.task_channels import TaskChannels

    workbench = SimpleNamespace(
        output_dir=str(tmp_path),
        host=SimpleNamespace(runner=SimpleNamespace(busy=False)),
        perform=lambda operation: operation(),
        configure_comfy=lambda: None,
        submit=lambda work, button: None,
    )
    workbench.tasks = TaskChannels(workbench.host.runner)
    panel = MediaPreview(workbench)
    panel.import_image(str(png(tmp_path, "scene.png", 0xFF0000)), "137")
    panel.import_image(str(png(tmp_path, "sheep.png", 0x00FF00)), "139")
    scene, sheep = panel.image_selection()["137"], panel.image_selection()["139"]
    panel.remove_image(scene)
    assert panel.image_selection() == {"137": None, "139": sheep, "147": None}
    for combo, _, _ in panel.image_fields.values():
        assert combo.findData(scene) < 0 and combo.findData(sheep) > 0
    repository = MediaRepository(tmp_path)
    assert repository.resolve(repository.image(scene)["relative_path"]).exists()
    panel.refresh()
    assert all(combo.findData(scene) < 0 for combo, _, _ in panel.image_fields.values())
    panel.use_image(scene, "147")
    assert panel.image_selection()["147"] == scene
    assert scene not in repository.hidden_images()
    panel.close()
