"""MCP-to-ComfyUI video integration, using real SDK calls and a fake HTTP server."""

import base64
import json
import os
from types import SimpleNamespace

import httpx
import pytest

from core.common.cancellation import GenerationCancelled
from core.short_video.v2.media_repository import MediaRepository
from core.short_video.v2.media_service import MediaService, MediaWork, validate_nodes
from core.short_video.v2.media_workflows import (
    SHOT_ASPECT_RATIO,
    SHOT_MEGAPIXELS,
    MediaConfig,
    default_workflows,
    parse_video_workflow,
    save_shot_drafts,
)
from core.short_video.v2.models import WorkspaceError

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6lL8AAAAASUVORK5CYII="
)
VIDEO = b"\x00\x00\x00\x18ftypisom" + bytes(64)


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "reference.png"
    path.write_bytes(PNG)
    return MediaRepository(tmp_path).import_image(path)


class ComfyServer:
    def __init__(self):
        self.submissions = []
        self.uploads = []
        self.drop_response = False
        self.fail_download = False
        self.running = False
        self.failure = None
        self.calls = []
        self.extra_nodes = {}

    def request(self, request):
        path = request.url.path
        self.calls.append(path)
        if self.failure:
            raise self.failure
        if path == "/system_stats":
            return httpx.Response(200, json={"system": {"comfyui_version": "test"}})
        if path == "/object_info":
            nodes = {}
            for record in MediaConfig().workflows:
                for node in parse_video_workflow(record["graph"]).graph.values():
                    nodes[node["class_type"]] = {"input": {"required": {}}}
            nodes["LoadImage"] = {
                "input": {"required": {"image": [["unrelated-server-file.png"], {}]}}
            }
            nodes.update(self.extra_nodes)
            return httpx.Response(200, json=nodes)
        if path == "/upload/image":
            self.uploads.append(request.content)
            return httpx.Response(
                200,
                json={
                    "name": f"server-renamed{len(self.uploads) if len(self.uploads) > 1 else ''}.png",
                    "subfolder": "NovelWriter",
                    "type": "input",
                },
            )
        if path == "/prompt":
            self.submissions.append(json.loads(request.content))
            if self.drop_response:
                raise httpx.ReadTimeout("response lost after acceptance", request=request)
            return httpx.Response(200, json={"prompt_id": "remote-job"})
        if path == "/queue":
            prompt = self.submissions[0]
            return httpx.Response(
                200,
                json={
                    "queue_running": [
                        [0, "remote-job", prompt["prompt"], prompt["extra_data"], []]
                    ],
                    "queue_pending": [],
                },
            )
        if path.startswith("/history"):
            if self.running or path == "/history":
                return httpx.Response(200, json={})
            return httpx.Response(
                200,
                json={
                    "remote-job": {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {
                            "92": {
                                "images": [
                                    {
                                        "filename": "result.mp4",
                                        "subfolder": "video",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                    }
                },
            )
        if path == "/view":
            if self.fail_download:
                return httpx.Response(503, text="temporarily unavailable")
            return httpx.Response(200, content=VIDEO)
        raise AssertionError(f"Unexpected request {request.method} {path}")


def test_builtin_reference_connects_three_images_and_ignores_orphan_nodes():
    graph = MediaConfig().selected()["graph"]
    graph["orphan-image"] = {"class_type": "LoadImage", "inputs": {"image": "unused.png"}}
    template = parse_video_workflow(graph)
    assert [image.node_id for image in template.image_inputs] == ["137", "139", "147"]
    assert template.reference_count == 3
    assert template.output_node == "92"
    assert not {"orphan-image", "149"} & template.graph.keys()
    original = template.graph["137"]["inputs"]["image"]
    bound = template.bind(
        {
            "137": "NovelWriter/input.png",
            "139": "NovelWriter/second.png",
            "147": "NovelWriter/third.png",
        },
        "Use <Picture 1>.",
        "job",
        {},
    )
    assert bound["139"]["inputs"]["image"] == "NovelWriter/second.png"
    assert bound["147"]["inputs"]["image"] == "NovelWriter/third.png"
    assert bound["137"]["inputs"]["image"] == "NovelWriter/input.png"
    assert bound["138"]["inputs"]["value"] == "Use <Picture 1>."
    assert template.graph["137"]["inputs"]["image"] == original


def test_generated_image_provider_can_reuse_import_contract(tmp_path):
    path = tmp_path / "generated.png"
    path.write_bytes(PNG)
    repository = MediaRepository(tmp_path)
    record = repository.import_image(
        path, source={"kind": "image_workflow", "job_id": "future-job"}
    )
    assert record["source"]["job_id"] == "future-job"
    assert repository.resolve(record["relative_path"]).read_bytes() == PNG
    assert not (tmp_path / "short_video/project_v2.json").exists()


def test_saved_catalog_keeps_json_when_original_resource_moves(tmp_path, monkeypatch):
    from core.short_video.v2 import media_workflows

    MediaConfig().save(tmp_path)
    monkeypatch.setattr(media_workflows, "WORKFLOW_DIRECTORY", tmp_path / "missing")
    reopened = MediaConfig.load(tmp_path)
    assert parse_video_workflow(reopened.selected()["graph"]).image_inputs[0].node_id == "137"


def test_video_runs_through_mcp_upload_submission_download_and_replay(tmp_path, image, monkeypatch):
    from core.short_video.v2 import media_service

    monkeypatch.setattr(media_service, "timestamp", lambda: "2026-11-16T01:02:03+00:00")
    server = ComfyServer()
    command = MediaService(tmp_path).prepare_video(
        {node_id: image["image_id"] for node_id in ["137", "139", "147"]}, "Use <Picture 1>.",
        target={"shot_id": "shot_3", "shot_number": 3},
    )
    transport = httpx.MockTransport(server.request)
    result = MediaWork(str(tmp_path), command, transport=transport)(lambda *_: None)
    assert result["status"] == "succeeded"
    assert result["outputs"][0]["relative_path"] == (
        f"videos/2026-11-16_09-02-03_镜头03_{result['job_id'][:8]}_01.mp4"
    )
    graph = server.submissions[0]["prompt"]
    assert graph["137"]["inputs"]["image"] == "NovelWriter/server-renamed.png"
    assert PNG in server.uploads[0]
    assert (
        MediaRepository(tmp_path).resolve(result["outputs"][0]["relative_path"]).read_bytes()
        == VIDEO
    )
    again = MediaWork(str(tmp_path), command, transport=transport)(lambda *_: None)
    assert again["job_id"] == result["job_id"]
    assert len(server.submissions) == 1


def test_lost_submission_response_queries_original_job_without_resubmission(tmp_path, image):
    server = ComfyServer()
    server.drop_response = True
    command = MediaService(tmp_path).prepare_video(
        {node_id: image["image_id"] for node_id in ["137", "139", "147"]}, "Use <Picture 1>."
    )
    transport = httpx.MockTransport(server.request)
    with pytest.raises(httpx.ReadTimeout, match="response lost"):
        MediaWork(str(tmp_path), command, transport=transport)(lambda *_: None)
    job = MediaRepository(tmp_path).jobs()[0]
    assert job["status"] == "unknown"
    server.drop_response = False
    result = MediaWork(str(tmp_path), job_id=job["job_id"], transport=transport)(lambda *_: None)
    assert result["status"] == "succeeded"
    assert len(server.submissions) == 1


def test_failed_download_only_fetches_existing_video_again(tmp_path, image, monkeypatch):
    from core.short_video.v2 import media_service

    monkeypatch.setattr(media_service, "timestamp", lambda: "2026-11-16T01:02:03+00:00")
    server = ComfyServer()
    server.fail_download = True
    command = MediaService(tmp_path).prepare_video(
        {node_id: image["image_id"] for node_id in ["137", "139", "147"]}, "Use <Picture 1>."
    )
    transport = httpx.MockTransport(server.request)
    with pytest.raises(httpx.HTTPStatusError):
        MediaWork(str(tmp_path), command, transport=transport)(lambda *_: None)
    job = MediaRepository(tmp_path).jobs()[0]
    assert job["status"] == "collecting"
    monkeypatch.setattr(media_service, "timestamp", lambda: "2026-11-17T04:05:06+00:00")
    server.fail_download = False
    result = MediaWork(str(tmp_path), job_id=job["job_id"], transport=transport)(lambda *_: None)
    assert result["status"] == "succeeded"
    assert len(server.submissions) == 1
    assert result["outputs"][0]["relative_path"] == (
        f"videos/2026-11-16_09-02-03_视频_{job['job_id'][:8]}_01.mp4"
    )


def test_videos_generated_in_same_second_have_distinct_names(tmp_path, image, monkeypatch):
    from core.short_video.v2 import media_service

    monkeypatch.setattr(media_service, "timestamp", lambda: "2026-11-16T23:59:59+00:00")
    server = ComfyServer()
    transport = httpx.MockTransport(server.request)
    paths = []
    for _ in range(2):
        command = MediaService(tmp_path).prepare_video(
            {node_id: image["image_id"] for node_id in ["137", "139", "147"]},
            "Use <Picture 1>.", target={"shot_number": 1},
        )
        result = MediaWork(str(tmp_path), command, transport=transport)(lambda *_: None)
        relative = result["outputs"][0]["relative_path"]
        assert relative.startswith("videos/2026-11-17_07-59-59_镜头01_")
        paths.append(relative)
    assert paths[0] != paths[1]
    assert all(MediaRepository(tmp_path).resolve(path).read_bytes() == VIDEO for path in paths)


def test_rejected_submission_is_failed_instead_of_remote_status_unknown(tmp_path, image):
    server = ComfyServer()

    def reject_workflow(request):
        if request.url.path == "/prompt":
            return httpx.Response(400, json={"error": "invalid workflow"})
        return server.request(request)

    command = MediaService(tmp_path).prepare_video(
        {node_id: image["image_id"] for node_id in ["137", "139", "147"]}, "Use <Picture 1>."
    )
    with pytest.raises(httpx.HTTPStatusError):
        MediaWork(str(tmp_path), command, transport=httpx.MockTransport(reject_workflow))(
            lambda *_: None
        )
    assert MediaRepository(tmp_path).jobs()[0]["status"] == "failed"


def test_old_config_discards_unused_image_setting_and_preserves_video(tmp_path, image):
    previous = tmp_path / "previous-api.json"
    previous.write_text(json.dumps(MediaConfig().selected()["graph"]), encoding="utf-8")
    repository = MediaRepository(tmp_path)
    repository.write(
        "settings.json",
        {
            "base_url": "http://127.0.0.1:8188",
            "video_workflow": str(previous),
            "image_workflow": str(tmp_path / "not-implemented.json"),
        },
    )
    command = MediaService(tmp_path).prepare_video(
        {node_id: image["image_id"] for node_id in ["137", "139", "147"]}, "Use <Picture 1>."
    )
    assert command["images"]["137"]["image_id"] == image["image_id"]
    MediaConfig.load(tmp_path).save(tmp_path)
    assert "image_workflow" not in repository.read("settings.json")


def test_local_cancel_never_sends_global_comfy_interrupt(tmp_path, image):
    server = ComfyServer()
    server.running = True
    command = MediaService(tmp_path).prepare_video(
        {node_id: image["image_id"] for node_id in ["137", "139", "147"]}, "Use <Picture 1>."
    )
    work = MediaWork(str(tmp_path), command, transport=httpx.MockTransport(server.request))

    def cancel_when_running(message, fraction):
        if "正在生成视频" in message:
            work.token.cancel()

    with pytest.raises(GenerationCancelled):
        work(cancel_when_running)
    job = MediaRepository(tmp_path).jobs()[0]
    assert job["status"] == "interrupted"
    assert job["remote_job_id"] == "remote-job"
    assert "/interrupt" not in server.calls


def test_mcp_preserves_programming_error_without_retry(tmp_path, image):
    server = ComfyServer()
    server.failure = TypeError("internal transport defect")
    command = MediaService(tmp_path).prepare_video(
        {node_id: image["image_id"] for node_id in ["137", "139", "147"]}, "Use <Picture 1>."
    )
    with pytest.raises(TypeError, match="internal transport defect") as caught:
        MediaWork(str(tmp_path), command, transport=httpx.MockTransport(server.request))(
            lambda *_: None
        )
    assert caught.value is server.failure
    assert server.calls == ["/system_stats"]
    assert MediaRepository(tmp_path).jobs()[0]["status"] == "failed"


def test_missing_model_is_reported_before_gpu_submission():
    with pytest.raises(WorkspaceError, match="不可用"):
        validate_nodes(
            {"1": {"class_type": "UNETLoader", "inputs": {"unet_name": "missing"}}},
            {"UNETLoader": {"input": {"required": {"unet_name": [["available"], {}]}}}},
        )


def test_changed_image_or_invalid_reference_is_rejected(tmp_path, image):
    service = MediaService(tmp_path)
    with pytest.raises(WorkspaceError, match="引用超出范围"):
        service.prepare_video(
            {node_id: image["image_id"] for node_id in ["137", "139", "147"]}, "Use <Picture 4>."
        )
    MediaRepository(tmp_path).resolve(image["relative_path"]).write_bytes(PNG + b"changed")
    with pytest.raises(WorkspaceError, match="已被修改"):
        service.prepare_video(
            {node_id: image["image_id"] for node_id in ["137", "139", "147"]}, "Use <Picture 1>."
        )


def test_video_ui_accepts_manual_image_without_storyboard(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication

    from core.gui.pages.short_video.media_preview import MediaPreview

    app = QApplication.instance() or QApplication([])
    submitted = []
    workbench = SimpleNamespace(
        output_dir=str(tmp_path),
        perform=lambda operation: operation(),
        configure_comfy=lambda: None,
        submit=lambda work, button: submitted.append(work),
    )
    from core.gui.pages.short_video.task_channels import TaskChannels
    workbench.tasks = TaskChannels()
    panel = MediaPreview(workbench)
    image = QImage(8, 8, QImage.Format_RGB32)
    image.fill(0xFF0088)
    path = tmp_path / "ui-reference.png"
    assert image.save(str(path))
    for node_id in ["137", "139", "147"]:
        panel.import_image(str(path), node_id)
    panel.generate_button.click()
    app.processEvents()
    assert len(submitted) == 1
    assert "<Picture 1>" in submitted[0].command["prompt"]
    panel.prompt.setPlainText("Use <Picture 1>, slow camera push.")
    panel.generate_button.click()
    app.processEvents()
    assert len(submitted) == 2
    assert submitted[1].command["images"]["137"]["name"] == "ui-reference.png"
    assert submitted[1].command["prompt"].endswith("slow camera push.")
    panel.close()


def reference_graph(count):
    graph = MediaConfig().selected()["graph"]
    inputs = graph["136"]["inputs"]
    for name in list(inputs):
        if name.startswith("ref_images.ref_image_"):
            del inputs[name]
    for index in range(count):
        node_id = f"image-{index}"
        graph[node_id] = {"class_type": "LoadImage", "inputs": {"image": "original.png"}}
        graph["136"]["inputs"][f"ref_images.ref_image_{index}"] = [node_id, 0]
    return graph


def test_catalog_add_replace_and_reopen_preserves_workflow_identity(tmp_path):
    config = MediaConfig()
    workflow_id = config.put_workflow("三图参考", json.dumps(reference_graph(3)))
    config.save(tmp_path)
    loaded = MediaConfig.load(tmp_path)
    assert loaded.selected()["workflow_id"] == workflow_id
    assert len(parse_video_workflow(loaded.selected()["graph"]).image_inputs) == 3
    loaded.put_workflow("四图参考", json.dumps(reference_graph(4)), workflow_id)
    loaded.save(tmp_path)
    updated = MediaConfig.load(tmp_path)
    assert len(updated.workflows) == 3
    assert len(parse_video_workflow(updated.selected()["graph"]).image_inputs) == 4
    assert updated.selected()["workflow_id"] == workflow_id
    assert (
        len(parse_video_workflow(updated.selected("minimax-first-last")["graph"]).image_inputs) == 5
    )


@pytest.mark.parametrize("workflow_id,count", [("four-images", 4), ("minimax-first-last", 5)])
def test_all_images_parameters_and_selected_graph_reach_comfy(tmp_path, workflow_id, count):
    config = MediaConfig()
    if workflow_id == "four-images":
        workflow_id = config.put_workflow("四图参考", json.dumps(reference_graph(count)))
    config.selected_workflow_id = workflow_id
    config.save(tmp_path)
    template = parse_video_workflow(config.selected()["graph"])
    repository = MediaRepository(tmp_path)
    images = {}
    for index, image_input in enumerate(template.image_inputs):
        path = tmp_path / f"input-{index}.png"
        path.write_bytes(PNG + str(index).encode())
        images[image_input.node_id] = repository.import_image(path)["image_id"]
    parameters = {}
    for parameter in template.parameters:
        if parameter.label == "时长（秒）":
            parameters[parameter.key] = 5.5
        elif parameter.input_name == "aspect_ratio":
            parameters[parameter.key] = "9:16 (Portrait Widescreen)"
        elif parameter.input_name == "noise_seed":
            parameters[parameter.key] = 12345678901234567890
        elif type(parameter.default) is bool:
            parameters[parameter.key] = True
    command = MediaService(tmp_path).prepare_video(
        images, "保持连贯，缓慢推进。", parameters=parameters
    )
    server = ComfyServer()
    result = MediaWork(str(tmp_path), command, transport=httpx.MockTransport(server.request))(
        lambda *_: None
    )
    assert result["status"] == "succeeded"
    assert len(server.uploads) == count
    graph = server.submissions[0]["prompt"]
    assert graph["115"]["inputs"]["aspect_ratio"] == "9:16 (Portrait Widescreen)"
    for index, image_input in enumerate(template.image_inputs, 1):
        suffix = index if index > 1 else ""
        assert (
            graph[image_input.node_id]["inputs"]["image"]
            == f"NovelWriter/server-renamed{suffix}.png"
        )
    for parameter in template.parameters:
        assert graph[parameter.node_id]["inputs"][parameter.input_name] == parameters.get(
            parameter.key, parameter.default
        )
    assert result["input_snapshot"]["workflow_id"] == workflow_id
    assert result["input_snapshot"]["images"].keys() == images.keys()
    assert config.selected()["graph"] == MediaConfig.load(tmp_path).selected()["graph"]
    if template.kind == "reference_first_last":
        assert graph["149"]["inputs"]["image"] == ["147", 0]
        assert graph["150"]["inputs"]["image"] == ["148", 0]
        assert graph["149"]["inputs"]["frame_idx"] == 0
        assert graph["150"]["inputs"]["frame_idx"] == -1
        assert graph["126"]["inputs"]["conditioning"] == ["150", 0]
        assert template.reference_count == 3


def test_aspect_ratio_is_checked_locally_and_against_comfy_options_before_upload(tmp_path, image):
    service = MediaService(tmp_path)
    images = {"137": image["image_id"]}
    with pytest.raises(WorkspaceError, match="画幅不在可选范围内"):
        service.prepare_video(images, "保持动作连贯。", parameters={"115.aspect_ratio": "9:16"})

    command = service.prepare_video(
        images, "保持动作连贯。", parameters={"115.aspect_ratio": "9:16 (Portrait Widescreen)"}
    )
    server = ComfyServer()
    server.extra_nodes["ResolutionSelector"] = {
        "input": {"required": {"aspect_ratio": [["21:9 (Ultrawide)"], {}]}}
    }
    work = MediaWork(str(tmp_path), command, transport=httpx.MockTransport(server.request))
    with pytest.raises(WorkspaceError, match="aspect_ratio 不可用"):
        work(lambda *_: None)
    assert server.uploads == [] and server.submissions == []


def test_missing_image_wrong_parameter_or_unknown_template_is_explicit(tmp_path, image):
    config = MediaConfig()
    config.selected_workflow_id = "minimax-first-last"
    config.save(tmp_path)
    service = MediaService(tmp_path)
    with pytest.raises(WorkspaceError, match="至少选择一张"):
        service.prepare_video(
            {entry.node_id: None for entry in parse_video_workflow(config.selected()["graph"]).image_inputs},
            "camera movement",
        )
    with pytest.raises(WorkspaceError, match="类型或范围"):
        service.prepare_video(
            {
                entry.node_id: image["image_id"]
                for entry in parse_video_workflow(config.selected()["graph"]).image_inputs
            },
            "camera movement",
            parameters={"132.value": "five"},
        )
    graph = reference_graph(1)
    graph["136"]["class_type"] = "UnknownVideoGenerator"
    with pytest.raises(WorkspaceError, match="已适配"):
        config.put_workflow("未知", json.dumps(graph))
    with pytest.raises(json.JSONDecodeError):
        config.put_workflow("错误", "{broken")


def test_ui_switches_roles_and_json_update_changes_input_count(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.pages.short_video.media_preview import MediaPreview
    from core.gui.pages.short_video.comfy_settings import ComfySettingsDialog

    app = QApplication.instance() or QApplication([])
    config = MediaConfig()
    custom_id = config.put_workflow("三图参考", json.dumps(reference_graph(3)))
    config.save(tmp_path)
    submitted = []
    workbench = SimpleNamespace(
        output_dir=str(tmp_path),
        perform=lambda operation: operation(),
        configure_comfy=lambda: None,
        submit=lambda work, button: submitted.append(work),
    )
    from core.gui.pages.short_video.task_channels import TaskChannels
    workbench.tasks = TaskChannels()
    panel = MediaPreview(workbench)
    panel.refresh()
    assert len(panel.image_fields) == 3
    config.put_workflow("四图参考", json.dumps(reference_graph(4)), custom_id)
    config.save(tmp_path)
    panel.refresh()
    assert len(panel.image_fields) == 4
    panel.workflows.setCurrentIndex(panel.workflows.findData("minimax-first-last"))
    assert [entry.label for entry in panel.template.image_inputs] == [
        "参考图片 1",
        "参考图片 2",
        "参考图片 3",
        "首帧图片",
        "尾帧图片",
    ]
    assert [slot.summary() for slot in panel.image_fields.values()] == [
        "图片 1 · 未选", "图片 2 · 未选", "图片 3 · 未选",
        "首帧图片 · 未选", "尾帧图片 · 未选",
    ]
    assert panel.frames_head.isVisibleTo(panel) and not panel.add_image_button.isVisibleTo(panel)
    from PySide6.QtGui import QImage

    path = tmp_path / "ui-image.png"
    ui_image = QImage(8, 8, QImage.Format_RGB32)
    ui_image.fill(0xFF0088)
    assert ui_image.save(str(path))
    panel.import_image(str(path), "137")
    assert panel.generate_button.isEnabled()
    for entry in panel.template.image_inputs[1:]:
        panel.import_image(str(path), entry.node_id)
    aspect = panel.parameter_fields["115.aspect_ratio"]
    assert aspect.currentText() == "9:16 竖屏"
    aspect.setCurrentIndex(aspect.findData("16:9 (Widescreen)"))
    panel.refresh()
    assert panel.parameter_fields["115.aspect_ratio"].currentText() == "16:9 横屏"
    panel.parameter_fields["132.value"].setValue(5.5)
    panel.parameter_fields["129.noise_seed"].setText("18446744073709551615")
    panel.prompt.setPlainText("首尾衔接")
    panel.generate_button.click()
    app.processEvents()
    assert len(submitted) == 1
    command = submitted[0].command
    assert command["workflow_id"] == "minimax-first-last"
    assert command["parameters"]["115.aspect_ratio"] == "16:9 (Widescreen)"
    assert command["parameters"]["132.value"] == 5.5
    assert command["parameters"]["129.noise_seed"] == 2**64 - 1
    assert len(command["images"]) == 5
    assert command["images"]["147"]["image_id"] != command["images"]["148"]["image_id"]
    dialog = ComfySettingsDialog(str(tmp_path))
    dialog.new_button.click()
    dialog.name.setText("粘贴工作流")
    dialog.api_json.setPlainText(json.dumps(reference_graph(4)))
    dialog.save_button.click()
    from PySide6.QtCore import QElapsedTimer

    timer = QElapsedTimer()
    timer.start()
    while dialog.runner.busy and timer.elapsed() < 5000:
        app.processEvents()
    app.processEvents()
    assert MediaConfig.load(tmp_path).selected()["name"] == "粘贴工作流"
    assert len(MediaConfig.load(tmp_path).workflows) == 4
    panel.refresh()
    assert len(panel.image_fields) == 4
    panel.close()
    dialog.close()


def test_video_task_boundary_refreshes_controls_and_keeps_workflow_jobs_separate(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QElapsedTimer
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication
    from core.gui.task_runner import TaskRunner
    from core.gui.pages.short_video.media_preview import MediaPreview

    app = QApplication.instance() or QApplication([])
    runner = TaskRunner()
    failures = []
    workbench = SimpleNamespace(
        output_dir=str(tmp_path),
        perform=lambda operation: operation(),
        configure_comfy=lambda: None,
    )
    from core.gui.pages.short_video.task_channels import TaskChannels
    workbench.tasks = TaskChannels()
    panel = MediaPreview(workbench)
    panel.refresh()
    path = tmp_path / "input.png"
    image = QImage(8, 8, QImage.Format_RGB32)
    image.fill(0x001188)
    assert image.save(str(path))
    for node_id in ["137", "139", "147"]:
        panel.import_image(str(path), node_id)
    selection = panel.image_selection()
    workbench.tasks.runners["video"] = runner
    server = ComfyServer()

    def submit(work, button):
        work.transport = httpx.MockTransport(server.request)
        runner.run(
            work,
            pass_progress=True,
            on_success=lambda _: panel.refresh(),
            on_error=failures.append,
            on_done=panel.refresh_result,
        )
        panel.refresh_result()

    workbench.submit = submit
    panel.generate_button.click()
    assert panel.add_image_button.isEnabled()
    assert not panel.generate_button.isEnabled()
    timer = QElapsedTimer()
    timer.start()
    while runner.busy and timer.elapsed() < 5000:
        app.processEvents()
    app.processEvents()
    assert not runner.busy
    assert failures == []
    assert panel.generate_button.isEnabled()
    assert panel.add_image_button.isEnabled()
    assert panel.current_job["status"] == "succeeded"
    config = MediaConfig.load(tmp_path)
    other_id = config.put_workflow("相同模板另一个工作流", json.dumps(config.selected()["graph"]))
    config.save(tmp_path)
    panel.refresh()
    for node_id, image_id in selection.items():
        panel.image_fields[node_id].set_image(image_id)
    assert panel.loaded_workflow_id == other_id
    assert panel.current_job is None
    panel.close()


def test_reference_and_guides_have_separate_roles_and_picture_numbers(tmp_path, image):
    config = MediaConfig()
    config.selected_workflow_id = "minimax-first-last"
    config.save(tmp_path)
    template = parse_video_workflow(config.selected()["graph"])
    assert [(entry.node_id, entry.role) for entry in template.image_inputs] == [
        ("137", "reference"),
        ("139", "reference"),
        ("152", "reference"),
        ("147", "first_frame"),
        ("148", "last_frame"),
    ]
    images = {entry.node_id: image["image_id"] for entry in template.image_inputs}
    service = MediaService(tmp_path)
    assert (
        service.prepare_video(images, "使用 <Picture 3> 作为外观参考")["workflow_id"]
        == "minimax-first-last"
    )
    with pytest.raises(WorkspaceError, match="3 张参考图片"):
        service.prepare_video(images, "Use <Picture 4>")
    graph = config.selected()["graph"]
    graph["150"]["inputs"]["frame_idx"] = 3
    with pytest.raises(WorkspaceError, match="frame_idx"):
        parse_video_workflow(graph)


def test_independent_first_last_node_remains_supported():
    graph = MediaConfig().selected("minimax-first-last")["graph"]
    inputs = graph["136"]["inputs"]
    graph["136"]["class_type"] = "MiniMaxH3ImageToVideo"
    graph["136"]["inputs"] = {
        key: inputs[key] for key in ["clip", "vae", "prompt", "width", "height", "length"]
    }
    graph["136"]["inputs"].update(first_frame=["137", 0], last_frame=["139", 0])
    graph["126"]["inputs"]["conditioning"] = ["136", 0]
    template = parse_video_workflow(graph)
    assert template.kind == "first_last"
    assert template.reference_count == 0
    assert [entry.label for entry in template.image_inputs] == ["首帧图片", "尾帧图片"]


@pytest.mark.parametrize("selected", [
    ("147", "148"), ("148",), ("147",), ("139",), ("139", "148"),
    ("139", "147"), ("139", "147", "148"), ("137", "139", "152", "148"),
    ("137", "152", "147"), ("137", "139", "152", "147", "148"),
])
def test_combined_workflow_prunes_unselected_images_and_rewires_guides(tmp_path, image, selected):
    from core.short_video.v2.video_prompt import snapshot_video_inputs, validate_video_inputs

    config = MediaConfig()
    config.selected_workflow_id = "minimax-first-last"
    config.save(tmp_path)
    before = (tmp_path / "short_video/media/settings.json").read_bytes()
    template = parse_video_workflow(config.selected()["graph"])
    selection = {entry.node_id: image["image_id"] if entry.node_id in selected else None
                 for entry in template.image_inputs}
    command = MediaService(tmp_path).prepare_video(selection, "保持动作与画面连续。")
    graph = command["workflow"]
    assert {key for key, node in graph.items() if node["class_type"] == "LoadImage"} == set(selected)
    assert set(command["images"]) == set(selected)
    reference_ids = [key for key in ("137", "139", "152") if key in selected]
    reference_inputs = {key: value for key, value in graph["136"]["inputs"].items()
                        if key.startswith("ref_images.ref_image_")}
    assert reference_inputs == {f"ref_images.ref_image_{i}": [key, 0]
                                for i, key in enumerate(reference_ids)}
    previous = "136"
    for node_id, image_id, frame in (("149", "147", 0), ("150", "148", -1)):
        if image_id in selected:
            assert graph[node_id]["inputs"]["positive"] == [previous, 0]
            assert graph[node_id]["inputs"]["latent"] == ["136", 1]
            assert graph[node_id]["inputs"]["frame_idx"] == frame
            previous = node_id
        else:
            assert node_id not in graph
    assert graph["126"]["inputs"]["conditioning"] == [previous, 0]
    binding = snapshot_video_inputs(str(tmp_path), selection, config.selected_workflow_id, {})
    assert [i["reference"] for i in binding["images"] if i["role"] == "reference"] == [
        f"<Picture {i + 1}>" for i in range(len(reference_ids))]
    validate_video_inputs(str(tmp_path), binding)
    server = ComfyServer()
    result = MediaWork(str(tmp_path), command, transport=httpx.MockTransport(server.request))(
        lambda *_: None)
    assert result["status"] == "succeeded"
    assert len(server.uploads) == len(selected) and len(server.submissions) == 1
    assert (tmp_path / "short_video/media/settings.json").read_bytes() == before
    # With zero references, Picture numbers must not refer to a first/last frame.
    if not reference_ids:
        with pytest.raises(WorkspaceError, match="0 张参考图片"):
            MediaService(tmp_path).prepare_video(selection, "Use <Picture 1>.")


@pytest.mark.parametrize("selected", [("137",), ("139",), ("137", "139")])
def test_independent_frame_node_omits_unselected_optional_inputs(tmp_path, image, selected):
    config = MediaConfig()
    graph = config.selected("minimax-first-last")["graph"]
    inputs = graph["136"]["inputs"]
    graph["136"]["class_type"] = "MiniMaxH3ImageToVideo"
    graph["136"]["inputs"] = {key: inputs[key] for key in (
        "clip", "vae", "prompt", "width", "height", "length")}
    graph["136"]["inputs"].update(first_frame=["137", 0], last_frame=["139", 0])
    graph["126"]["inputs"]["conditioning"] = ["136", 0]
    config.put_workflow("纯首尾帧", json.dumps(graph))
    config.save(tmp_path)
    command = MediaService(tmp_path).prepare_video(
        {key: image["image_id"] if key in selected else None for key in ("137", "139")},
        "按所选首尾帧保持连续。")
    prepared = command["workflow"]
    for key, node_id in (("first_frame", "137"), ("last_frame", "139")):
        assert (key in prepared["136"]["inputs"]) == (node_id in selected)
        assert (node_id in prepared) == (node_id in selected)
    server = ComfyServer()
    server.extra_nodes["MiniMaxH3ImageToVideo"] = {"input": {"required": {}}}
    result = MediaWork(str(tmp_path), command, transport=httpx.MockTransport(server.request))(
        lambda *_: None)
    assert result["status"] == "succeeded" and len(server.uploads) == len(selected)


def test_combined_selection_rejects_unknown_nodes_before_upload(tmp_path, image):
    with pytest.raises(WorkspaceError, match="不存在的图片输入"):
        MediaService(tmp_path).prepare_video({"149": image["image_id"]}, "视频正文",
                                             workflow_id="minimax-first-last")
    assert MediaRepository(tmp_path).jobs() == []


def test_optional_combo_is_checked_before_remote_submission():
    with pytest.raises(WorkspaceError, match="chroma_subsampling 不可用"):
        validate_nodes(
            {"130": {"class_type": "CreateVideo", "inputs": {"chroma_subsampling": "none"}}},
            {
                "CreateVideo": {
                    "input": {
                        "optional": {
                            "chroma_subsampling": [
                                "COMBO",
                                {"options": ["auto", "444", "422", "420"]},
                            ]
                        }
                    }
                }
            },
        )


def test_server_numeric_range_is_checked_before_upload():
    with pytest.raises(WorkspaceError, match="megapixels 超出允许范围"):
        validate_nodes(
            {"115": {"class_type": "ResolutionSelector", "inputs": {"megapixels": 0.03}}},
            {
                "ResolutionSelector": {
                    "input": {"required": {"megapixels": ["FLOAT", {"min": 0.1, "max": 16}]}}
                }
            },
        )


def test_dynamic_reference_inputs_skip_blanks_and_upload_only_selected_images(tmp_path, image):
    config = MediaConfig()
    config.save(tmp_path)
    settings_before = (tmp_path / "short_video/media/settings.json").read_bytes()
    selection = {
        "137": image["image_id"],
        "139": None,
        "147": None,
        "reference_input_4": image["image_id"],
        "reference_input_5": None,
    }
    command = MediaService(tmp_path).prepare_video(selection, "Use <Picture 1> and <Picture 2>.")
    assert list(command["images"]) == ["137", "reference_input_4"]
    assert not {"139", "147", "reference_input_5"} & command["workflow"].keys()
    inputs = command["workflow"]["136"]["inputs"]
    assert inputs["ref_images.ref_image_0"] == ["137", 0]
    assert inputs["ref_images.ref_image_1"] == ["reference_input_4", 0]
    assert "ref_images.ref_image_2" not in inputs
    server = ComfyServer()
    job = MediaWork(str(tmp_path), command, transport=httpx.MockTransport(server.request))(
        lambda *_: None
    )
    assert job["status"] == "succeeded"
    assert len(server.uploads) == 2
    graph = server.submissions[0]["prompt"]
    assert graph["reference_input_4"]["class_type"] == "LoadImage"
    assert graph["reference_input_4"]["inputs"]["image"] == "NovelWriter/server-renamed2.png"
    assert graph["137"]["inputs"]["image"] == "NovelWriter/server-renamed.png"
    assert "139" not in graph and "147" not in graph
    assert (tmp_path / "short_video/media/settings.json").read_bytes() == settings_before


def test_dynamic_reference_limits_and_picture_numbers_fail_before_upload(tmp_path, image):
    service = MediaService(tmp_path)
    with pytest.raises(WorkspaceError, match="至少选择一张"):
        service.prepare_video({"137": None, "139": None, "147": None}, "camera movement")
    selected = {}
    for number in range(9):
        selected[f"reference_input_{number + 1}"] = image["image_id"]
    command = service.prepare_video(selected, "Use <Picture 9>.")
    assert len(command["images"]) == 9
    selected["reference_input_10"] = image["image_id"]
    with pytest.raises(WorkspaceError, match="最多使用 9 张"):
        service.prepare_video(selected, "camera movement")
    with pytest.raises(WorkspaceError, match="编号与其他节点冲突"):
        service.prepare_video({"127": image["image_id"]}, "camera movement")
    with pytest.raises(WorkspaceError, match="引用超出范围"):
        service.prepare_video(
            {"137": None, "139": image["image_id"], "147": None}, "Use <Picture 2>."
        )
    assert MediaRepository(tmp_path).jobs() == []


def reference_panel(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from core.gui.pages.short_video.media_preview import MediaPreview

    app = QApplication.instance() or QApplication([])
    submitted = []
    workbench = SimpleNamespace(
        output_dir=str(tmp_path),
        perform=lambda operation: operation(),
        configure_comfy=lambda: None,
        submit=lambda work, button: submitted.append(work),
    )
    from core.gui.pages.short_video.task_channels import TaskChannels
    workbench.tasks = TaskChannels()
    panel = MediaPreview(workbench)
    panel.refresh()
    return app, panel, submitted


def save_ui_image(path):
    from PySide6.QtGui import QImage

    image = QImage(8, 8, QImage.Format_RGB32)
    image.fill(0xFF8800)
    assert image.save(str(path))


def test_ui_add_reference_slot_and_skip_blank_slots_through_task_recovery(tmp_path):
    app, panel, submitted = reference_panel(tmp_path)
    panel.resize(600, 300)
    panel.show()
    app.processEvents()
    path = tmp_path / "manual.png"
    save_ui_image(path)
    assert [slot.summary() for slot in panel.image_fields.values()] == [
        "图片 1 · 未选", "图片 2 · 未选", "图片 3 · 未选",
    ]
    assert panel.add_image_button.text() == "＋ 添加参考图片\n还能加 6 张"
    assert not panel.generate_button.isEnabled()
    panel.prompt.setPlainText("Use <Picture 1> and <Picture 2>.")
    panel.parameter_fields["129.noise_seed"].setText("12345")
    panel.add_image_button.click()
    app.processEvents()
    assert len(panel.image_fields) == 4
    added_id = panel.additional_reference_slots[0]
    assert added_id == "reference_input_4"
    added = panel.image_fields[added_id]
    assert added.summary() == "图片 4 · 未选"
    assert panel.add_image_button.text() == "＋ 添加参考图片\n还能加 5 张"
    # The page scrolls to the new position, and keeps its place when the inputs reload.
    scroll_position = panel.settings_area.verticalScrollBar().value()
    assert scroll_position > 0
    viewport = panel.settings_area.viewport()
    assert viewport.rect().contains(added.mapTo(viewport, added.rect().topLeft()))
    assert viewport.rect().contains(added.mapTo(viewport, added.rect().bottomRight()))
    panel.import_image(str(path), "137")
    app.processEvents()
    scrollbar = panel.settings_area.verticalScrollBar()
    assert scrollbar.value() == min(scroll_position, scrollbar.maximum())
    assert panel.generate_button.isEnabled()
    panel.import_image(str(path), added_id)
    assert panel.image_fields[added_id].summary() == "图片 4 · <Picture 2>"
    assert panel.image_fields["139"].summary() == "图片 2 · 未选"
    assert panel.slots_note.text() == "已选 2 / 4 · 编号按已选顺序"
    assert panel.prompt.toPlainText() == "Use <Picture 1> and <Picture 2>."
    assert panel.parameter_fields["129.noise_seed"].text() == "12345"
    panel.generate_button.click()
    assert len(submitted) == 1
    work = submitted[0]
    assert list(work.command["images"]) == ["137", added_id]
    server = ComfyServer()
    server.running = True
    work.transport = httpx.MockTransport(server.request)

    def stop_local_wait(message, progress):
        if "正在生成视频" in message:
            work.token.cancel()

    with pytest.raises(GenerationCancelled):
        work(stop_local_wait)
    panel.refresh()
    assert len(panel.image_fields) == 4
    assert panel.current_job["status"] == "interrupted"
    assert panel.query_button.isEnabled()
    original_id = panel.current_job["job_id"]
    panel.query()
    assert submitted[-1].job_id == original_id
    assert len(server.submissions) == 1
    assert parse_video_workflow(MediaConfig.load(tmp_path).selected()["graph"]).reference_count == 3
    assert "reference_input_4" not in MediaConfig.load(tmp_path).selected()["graph"]
    panel.close()


def test_ui_reference_number_changes_require_prompt_review_and_allow_undo(tmp_path):
    app, panel, submitted = reference_panel(tmp_path)
    path = tmp_path / "manual.png"
    save_ui_image(path)
    for node_id in ["137", "139", "147"]:
        panel.import_image(str(path), node_id)
    original_image = panel.image_fields["139"].image_id
    panel.prompt.setPlainText("Use <Picture 2>.")
    assert panel.prompt_shift.isHidden()
    panel.image_fields["139"].clear_button.click()
    assert panel.image_fields["147"].summary() == "图片 3 · <Picture 2>"
    assert not panel.prompt_shift.isHidden()
    panel.refresh()
    with pytest.raises(WorkspaceError, match="图片编号已变化"):
        panel.generate()
    assert submitted == []
    panel.image_fields["139"].set_image(original_image)
    assert panel.prompt_shift.isHidden()
    panel.generate()
    assert len(submitted) == 1
    panel.image_fields["139"].set_image(None)
    with pytest.raises(WorkspaceError, match="图片编号已变化"):
        panel.generate()
    panel.prompt.setPlainText("Use <Picture 2>, now the second visible reference.")
    panel.generate()
    assert list(submitted[-1].command["images"]) == ["137", "147"]
    assert panel.prompt.toPlainText().startswith("Use <Picture 2>")
    panel.close()
    app.processEvents()


def test_ui_tail_only_can_submit_and_find_job_after_interruption(tmp_path):
    app, panel, submitted = reference_panel(tmp_path)
    panel.workflows.setCurrentIndex(panel.workflows.findData("minimax-first-last"))
    path = tmp_path / "tail.png"
    save_ui_image(path)
    panel.import_image(str(path), "148")
    panel.prompt.setPlainText("以尾帧为结束画面，保持动作和场景连续。")
    assert panel.generate_button.isEnabled()
    assert panel.image_fields["148"].summary() == "尾帧图片"
    assert panel.image_fields["148"].tag_text() == "尾帧图片"
    assert all("Picture" not in slot.summary() for slot in panel.image_fields.values())
    panel.generate()
    work = submitted[-1]
    assert set(work.command["images"]) == {"148"}
    server = ComfyServer()
    server.running = True
    work.transport = httpx.MockTransport(server.request)

    def stop(message, progress):
        if "正在生成视频" in message:
            work.token.cancel()

    with pytest.raises(GenerationCancelled):
        work(stop)
    panel.refresh()
    assert panel.current_job["status"] == "interrupted" and panel.query_button.isEnabled()
    job_id = panel.current_job["job_id"]
    panel.query()
    query = submitted[-1]
    assert query.job_id == job_id
    server.running = False
    query.transport = httpx.MockTransport(server.request)
    result = query(lambda *_: None)
    assert result["status"] == "succeeded" and len(server.submissions) == 1
    panel.refresh()
    assert panel.video_path.is_file()
    panel.close()
    app.processEvents()


def test_ui_combined_reference_numbering_excludes_frames_and_detects_shifts(tmp_path):
    app, panel, submitted = reference_panel(tmp_path)
    panel.workflows.setCurrentIndex(panel.workflows.findData("minimax-first-last"))
    path = tmp_path / "reference.png"
    save_ui_image(path)
    for node_id in ("139", "152", "148"):
        panel.import_image(str(path), node_id)
    assert panel.image_fields["139"].summary().endswith("<Picture 1>")
    assert panel.image_fields["152"].summary().endswith("<Picture 2>")
    assert panel.image_fields["148"].summary() == "尾帧图片"
    panel.prompt.setPlainText("Use <Picture 2> as a reference, end at the last frame.")
    panel.image_fields["139"].set_image(None)
    assert panel.image_fields["152"].summary().endswith("<Picture 1>")
    with pytest.raises(WorkspaceError, match="图片编号已变化"):
        panel.generate()
    panel.prompt.setPlainText("Use <Picture 1> as a reference, end at the last frame.")
    panel.generate()
    assert set(submitted[-1].command["images"]) == {"152", "148"}
    panel.close()
    app.processEvents()


def test_ui_reference_slot_limit_workflow_isolation_and_project_reset(tmp_path):
    app, panel, submitted = reference_panel(tmp_path)
    for _ in range(6):
        panel.add_image_button.click()
        app.processEvents()
    assert len(panel.image_fields) == 9
    assert not panel.add_image_button.isEnabled()
    with pytest.raises(WorkspaceError, match="最多添加 9 个"):
        panel.add_reference_image()
    panel.workflows.setCurrentIndex(panel.workflows.findData("minimax-first-last"))
    assert panel.add_image_button.isHidden()
    assert len(panel.image_fields) == 5
    assert not panel.generate_button.isEnabled()
    panel.workflows.setCurrentIndex(panel.workflows.findData("minimax-reference"))
    assert len(panel.image_fields) == 9
    panel.workbench.output_dir = str(tmp_path / "other-project")
    panel.refresh()
    assert len(panel.image_fields) == 3
    assert panel.add_image_button.isEnabled()
    assert panel.additional_reference_slots == []
    panel.close()


def test_template_refresh_does_not_duplicate_a_previously_added_reference(tmp_path):
    app, panel, submitted = reference_panel(tmp_path)
    panel.add_image_button.click()
    app.processEvents()
    added_id = panel.additional_reference_slots[0]
    config = MediaConfig.load(tmp_path)
    graph = (
        parse_video_workflow(config.selected()["graph"])
        .prepare_reference_inputs(["137", "139", "147", added_id])
        .graph
    )
    config.put_workflow("updated reference", json.dumps(graph), config.selected_workflow_id)
    config.save(tmp_path)
    panel.refresh()
    assert len(panel.image_fields) == 4
    assert panel.additional_reference_slots == []
    assert len(set(panel.image_fields)) == 4
    panel.close()


def test_video_settings_stay_readable_and_open_saved_output_without_playback(tmp_path, monkeypatch):
    from core.gui import theme
    from core.gui.pages.short_video import media_preview

    app, panel, submitted = reference_panel(tmp_path)
    previous_style = app.styleSheet()
    app.setStyleSheet(theme.build_qss())
    panel.resize(660, 800)
    panel.show()
    app.processEvents()
    assert panel.prompt.height() >= theme.CONTROL_HEIGHT * 5
    for slot in panel.image_fields.values():
        assert slot.width() >= 148 and slot.height() >= 110
    panel.advanced.head.click()
    app.processEvents()
    for control in panel.parameter_fields.values():
        assert control.isVisibleTo(panel) and control.height() >= theme.CONTROL_HEIGHT
    for child in panel.findChildren(media_preview.QWidget):
        assert child.metaObject().className() != "QVideoWidget"
    assert panel.open_button.isEnabled()

    path = tmp_path / "reference.png"
    save_ui_image(path)
    panel.import_image(str(path), "137")
    panel.parameter_fields["132.value"].setValue(3.5)
    panel.generate_button.click()
    work = submitted[0]
    assert work.command["parameters"]["132.value"] == 3.5
    server = ComfyServer()
    work.transport = httpx.MockTransport(server.request)
    work(lambda *_: None)
    panel.refresh()
    app.processEvents()
    assert panel.open_button.isEnabled()
    assert panel.video_path.is_file()
    opened = []
    monkeypatch.setattr(media_preview.QDesktopServices, "openUrl", lambda url: (opened.append(url), True)[1])
    panel.open_button.click()
    assert len(opened) == 1
    assert opened[0].toLocalFile() == str(panel.video_path.parent).replace("\\", "/")

    panel.resize(660, 520)
    app.processEvents()
    assert panel.settings_area.verticalScrollBar().maximum() > 0
    for control in panel.parameter_fields.values():
        assert control.height() >= theme.CONTROL_HEIGHT
    panel.workflows.setCurrentIndex(panel.workflows.findData("minimax-first-last"))
    assert panel.open_button.isEnabled()
    assert panel.video_path is None
    panel.close()
    app.setStyleSheet(previous_style)


def test_saved_video_card_opens_the_file_and_states_follow_the_job(tmp_path, monkeypatch):
    from core.gui.pages.short_video import media_preview

    app, panel, submitted = reference_panel(tmp_path)
    assert panel.pill.text() == "未生成" and panel.query_button.isHidden()
    assert not panel.open_video_button.isEnabled()
    assert panel.status.text() == "请至少选择一张参考图片"
    path = tmp_path / "reference.png"
    save_ui_image(path)
    panel.import_image(str(path), "137")
    assert panel.status.text() == "还没有生成视频"
    panel.generate_button.click()
    work = submitted[0]
    server = ComfyServer()
    server.running = True
    work.transport = httpx.MockTransport(server.request)

    def stop(message, progress):
        if "正在生成视频" in message:
            work.token.cancel()

    with pytest.raises(GenerationCancelled):
        work(stop)
    panel.refresh()
    assert panel.pill.text() == "待确认" and panel.pill.property("tone") == "warn"
    assert panel.query_button.isVisibleTo(panel) and "查询原任务" in panel.preview_box.text()
    panel.query()
    query = submitted[-1]
    server.running = False
    query.transport = httpx.MockTransport(server.request)
    query(lambda *_: None)
    panel.refresh()
    assert panel.pill.text() == "已保存" and panel.pill.property("tone") == "ok"
    assert panel.query_button.isHidden() and panel.status.text().startswith("视频已保存")
    opened = []
    monkeypatch.setattr(
        media_preview.QDesktopServices, "openUrl", lambda url: (opened.append(url), True)[1]
    )
    panel.open_video_button.click()
    assert opened[-1].toLocalFile() == str(panel.video_path).replace("\\", "/")
    panel.image_fields["137"].clear_button.click()
    assert panel.pill.text() == "未生成" and not panel.open_video_button.isEnabled()
    panel.close()
    app.processEvents()


def test_shots_start_vertical_at_one_megapixel_and_keep_what_was_set(tmp_path):
    """A project saved earlier still holds 21:9 and 0.4 in its workflows; its shots start at
    9:16 and 1.0 anyway, and a shot whose values were set keeps them."""
    for workflow in default_workflows():
        resolution = workflow["graph"]["115"]["inputs"]
        assert resolution["aspect_ratio"] == SHOT_ASPECT_RATIO == "9:16 (Portrait Widescreen)"
        assert resolution["megapixels"] == SHOT_MEGAPIXELS == 1.0
    config = MediaConfig()
    for workflow in config.workflows:
        workflow["graph"]["115"]["inputs"].update(aspect_ratio="21:9 (Ultrawide)", megapixels=0.4)
    config.save(tmp_path)
    for workflow in MediaConfig.load(tmp_path).workflows:
        template = parse_video_workflow(workflow["graph"])
        names = {image.node_id: "picture.png" for image in template.image_inputs}
        graph = template.bind(names, "保持连贯。", "job", {})
        assert graph["115"]["inputs"]["aspect_ratio"] == SHOT_ASPECT_RATIO
        assert graph["115"]["inputs"]["megapixels"] == SHOT_MEGAPIXELS
    save_shot_drafts(
        tmp_path,
        {
            "s2": {
                "minimax-reference": {
                    "parameters": {"115.aspect_ratio": "16:9 (Widescreen)", "115.megapixels": 0.4}
                }
            }
        },
    )
    app, panel, _ = reference_panel(tmp_path)
    panel.set_target({"shot_id": "s1"})
    panel.refresh()
    assert panel.parameter_fields["115.aspect_ratio"].currentText() == "9:16 竖屏"
    assert panel.parameter_fields["115.megapixels"].value() == 1.0
    panel.set_target({"shot_id": "s2"})
    panel.refresh()
    assert panel.parameter_fields["115.aspect_ratio"].currentText() == "16:9 横屏"
    assert panel.parameter_fields["115.megapixels"].value() == 0.4
    panel.close()


def test_duration_hint_offers_the_storyboard_length(tmp_path):
    app, panel, _ = reference_panel(tmp_path)
    duration = panel.duration_control()
    assert duration is not None and panel.duration_hint.isHidden()
    seconds = int(duration.value()) + 2
    panel.set_shot(seconds * 1000, [])
    assert f"分镜 {seconds} 秒" in panel.duration_hint.text()
    assert f"改成 {seconds}" in panel.duration_hint.text()
    panel.duration_hint.linkActivated.emit("match")
    assert duration.value() == seconds and "与分镜一致" in panel.duration_hint.text()
    key = next(p.key for p in panel.template.parameters if p.role == "duration")
    assert panel.parameter_values()[key] == seconds
    panel.close()


def test_prompt_and_video_share_a_row_however_the_parameters_change(tmp_path):
    """Beside a widened chat the page is about 790 wide: the video card stays level with the
    prompt, and changing a parameter (the longer duration hint among them) never moves it."""
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QCheckBox

    from core.gui import theme
    from core.gui.pages.short_video.video_widgets import TWO_COLUMNS_AT

    app, panel, _ = reference_panel(tmp_path)
    previous_style = app.styleSheet()
    app.setStyleSheet(theme.build_qss())
    panel.resize(790, 800)
    panel.show()
    app.processEvents()
    duration = panel.duration_control()

    def card_beside_prompt():
        app.processEvents()
        page = panel.settings_area.widget()
        prompt = panel.prompt_section.geometry().translated(
            panel.prompt_section.parentWidget().mapTo(page, QPoint())
        )
        card = panel.video_card.geometry().translated(
            panel.video_card.parentWidget().mapTo(page, QPoint())
        )
        return (
            panel.columns.wide
            and card.left() > prompt.right()
            and abs(card.top() - prompt.top()) < theme.SP[4]
        )

    assert card_beside_prompt()
    panel.set_shot(int(duration.value() + 7.25) * 1000, [])  # 「分镜 N 秒  改成 N」
    assert card_beside_prompt()
    panel.duration_hint.linkActivated.emit("match")  # 「与分镜一致」
    assert card_beside_prompt()
    for control in panel.parameter_fields.values():
        if isinstance(control, QCheckBox):
            control.setChecked(not control.isChecked())
    panel.advanced.head.click()
    assert card_beside_prompt()
    # Two columns always fit from where they start, whichever hint is showing.
    panel.set_shot(int(duration.value() + 7.25) * 1000, [])
    panel.relayout()
    assert panel.columns.minimumSizeHint().width() <= TWO_COLUMNS_AT

    panel.resize(600, 800)  # Too narrow for two: the card follows the prompt.
    app.processEvents()
    assert not panel.columns.wide
    assert panel.video_card.y() > panel.prompt_section.y() + panel.prompt_section.height()
    panel.close()
    app.setStyleSheet(previous_style)


def test_image_picker_lists_shot_assets_project_images_and_a_file(tmp_path):
    from PySide6.QtWidgets import QWidgetAction

    from core.gui.pages.short_video.media_preview import PickRow
    from core.short_video.v2.media_repository import MediaRepository

    app, panel, _ = reference_panel(tmp_path)
    for name in ("snow.png", "cabin.png"):
        save_ui_image(tmp_path / name)
    panel.import_image(str(tmp_path / "snow.png"), "137")
    panel.import_image(str(tmp_path / "cabin.png"), "139")
    images = {image["name"]: image for image in MediaRepository(str(tmp_path)).images()}
    panel.set_shot(None, [
        {"kind": "scene", "name": "雪地", "views": [
            {"label": "全景", "image": images["snow.png"]},
            {"label": "近景", "image": None},
        ]},
    ])
    menu = panel.picker_menu("147")
    texts = [action.text() for action in menu.actions() if action.text()]
    assert texts == ["场景 · 雪地 · 全景", "场景 · 雪地 · 近景（未生成图片）", "从文件选择…"]
    assert not menu.actions()[2].isEnabled()
    headers = [
        action.defaultWidget().text()
        for action in menu.actions()
        if isinstance(action, QWidgetAction) and not isinstance(action.defaultWidget(), PickRow)
    ]
    assert headers == ["本镜资产", "项目图片"]
    rows = {
        action.defaultWidget().pick.text(): action.defaultWidget()
        for action in menu.actions()
        if isinstance(action, QWidgetAction) and isinstance(action.defaultWidget(), PickRow)
    }
    assert set(rows) == {"snow.png", "cabin.png"}
    menu.actions()[1].trigger()
    assert panel.image_fields["147"].image_id == images["snow.png"]["image_id"]
    rows["cabin.png"].pick.click()
    assert panel.image_fields["147"].image_id == images["cabin.png"]["image_id"]
    menu = panel.picker_menu("137")
    row = next(
        action.defaultWidget()
        for action in menu.actions()
        if isinstance(action, QWidgetAction)
        and isinstance(action.defaultWidget(), PickRow)
        and action.defaultWidget().pick.text() == "cabin.png"
    )
    row.remove.click()
    # Removed from lists: every position that showed it is empty, the file stays.
    assert panel.image_fields["139"].image_id is None and panel.image_fields["147"].image_id is None
    assert images["cabin.png"]["image_id"] in MediaRepository(str(tmp_path)).hidden_images()
    assert "cabin.png" not in {image["name"] for image in MediaRepository(str(tmp_path)).images()}
    panel.close()
    app.processEvents()


def watch_windows(app):
    """Record widgets that open as a window of their own (a stray one flashes on screen)."""
    from PySide6.QtCore import QEvent, QObject
    from PySide6.QtWidgets import QWidget

    class Watch(QObject):
        def __init__(self):
            super().__init__()
            self.opened = []

        def eventFilter(self, obj, event):  # noqa: N802 - Qt 命名
            if event.type() == QEvent.Show and isinstance(obj, QWidget) and obj.isWindow():
                self.opened.append(type(obj).__name__)
            return False

    watch = Watch()
    app.installEventFilter(watch)
    return watch


def test_building_and_switching_the_video_area_opens_no_stray_window(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    watch = watch_windows(app)
    try:
        _, panel, _ = reference_panel(tmp_path)
        panel.workflows.setCurrentIndex(panel.workflows.findData("minimax-first-last"))
        panel.workflows.setCurrentIndex(panel.workflows.findData("minimax-reference"))
        panel.add_image_button.click()
        app.processEvents()
        assert watch.opened == []
        assert panel.add_image_button.parentWidget() is panel.slot_grid
    finally:
        app.removeEventFilter(watch)
    panel.close()
