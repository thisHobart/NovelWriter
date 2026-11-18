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
    listed = picker_images(panel, "147")
    assert scene not in listed and sheep in listed
    repository = MediaRepository(tmp_path)
    assert repository.resolve(repository.image(scene)["relative_path"]).exists()
    panel.refresh()
    assert panel.image_selection()["137"] is None and scene not in picker_images(panel, "137")
    panel.use_image(scene, "147")
    assert panel.image_selection()["147"] == scene
    assert scene not in repository.hidden_images()
    panel.close()


def picker_images(panel, node_id):
    """Names of the project images the slot's picker lists, as image ids."""
    from PySide6.QtWidgets import QWidgetAction

    from core.gui.pages.short_video.media_preview import PickRow

    names = {
        image["name"]: image["image_id"]
        for image in MediaRepository(panel.workbench.output_dir).images(include_hidden=True)
    }
    return [
        names[action.defaultWidget().pick.text()]
        for action in panel.picker_menu(node_id).actions()
        if isinstance(action, QWidgetAction) and isinstance(action.defaultWidget(), PickRow)
    ]
