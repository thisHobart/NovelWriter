"""MCP-to-ComfyUI video integration, using real SDK calls and a fake HTTP server."""

import base64
import json
import os
from types import SimpleNamespace

import httpx
import pytest

from core.generation.cancellation import GenerationCancelled
from core.short_video.v2.media_repository import MediaRepository
from core.short_video.v2.media_service import MediaService, MediaWork, validate_nodes
from core.short_video.v2.media_workflows import (
    DEFAULT_VIDEO_WORKFLOW,
    MediaConfig,
    load_video_workflow,
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

    def request(self, request):
        path = request.url.path
        self.calls.append(path)
        if self.failure:
            raise self.failure
        if path == "/system_stats":
            return httpx.Response(200, json={"system": {"comfyui_version": "test"}})
        if path == "/object_info":
            nodes = {}
            for node in load_video_workflow(DEFAULT_VIDEO_WORKFLOW).graph.values():
                nodes[node["class_type"]] = {"input": {"required": {}}}
            return httpx.Response(200, json=nodes)
        if path == "/upload/image":
            self.uploads.append(request.content)
            return httpx.Response(
                200,
                json={"name": "server-renamed.png", "subfolder": "NovelWriter", "type": "input"},
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


def test_api_workflow_uses_only_connected_image_and_preserves_template():
    template = load_video_workflow(DEFAULT_VIDEO_WORKFLOW)
    assert template.image_node == "137"
    assert template.output_node == "92"
    assert not {"139", "147", "149"} & template.graph.keys()
    original = template.graph["137"]["inputs"]["image"]
    bound = template.bind("NovelWriter/input.png", "Use <Picture 1>.", "job")
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


def test_default_workflow_reopens_after_packaged_resource_directory_changes(tmp_path, monkeypatch):
    from core.short_video.v2 import media_workflows

    MediaConfig().save(tmp_path)
    relocated = tmp_path / "next-launch-template.json"
    relocated.write_bytes(DEFAULT_VIDEO_WORKFLOW.read_bytes())
    monkeypatch.setattr(media_workflows, "DEFAULT_VIDEO_WORKFLOW", relocated)
    reopened = MediaConfig.load(tmp_path)
    assert reopened.video_workflow == str(relocated)
    assert load_video_workflow(reopened.video_workflow).image_node == "137"


def test_video_runs_through_mcp_upload_submission_download_and_replay(tmp_path, image):
    server = ComfyServer()
    command = MediaService(tmp_path).prepare_video(image["image_id"], "Use <Picture 1>.")
    transport = httpx.MockTransport(server.request)
    result = MediaWork(str(tmp_path), command, transport=transport)(lambda *_: None)
    assert result["status"] == "succeeded"
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
    command = MediaService(tmp_path).prepare_video(image["image_id"], "Use <Picture 1>.")
    transport = httpx.MockTransport(server.request)
    with pytest.raises(httpx.ReadTimeout, match="response lost"):
        MediaWork(str(tmp_path), command, transport=transport)(lambda *_: None)
    job = MediaRepository(tmp_path).jobs()[0]
    assert job["status"] == "unknown"
    server.drop_response = False
    result = MediaWork(str(tmp_path), job_id=job["job_id"], transport=transport)(lambda *_: None)
    assert result["status"] == "succeeded"
    assert len(server.submissions) == 1


def test_failed_download_only_fetches_existing_video_again(tmp_path, image):
    server = ComfyServer()
    server.fail_download = True
    command = MediaService(tmp_path).prepare_video(image["image_id"], "Use <Picture 1>.")
    transport = httpx.MockTransport(server.request)
    with pytest.raises(httpx.HTTPStatusError):
        MediaWork(str(tmp_path), command, transport=transport)(lambda *_: None)
    job = MediaRepository(tmp_path).jobs()[0]
    assert job["status"] == "collecting"
    server.fail_download = False
    result = MediaWork(str(tmp_path), job_id=job["job_id"], transport=transport)(lambda *_: None)
    assert result["status"] == "succeeded"
    assert len(server.submissions) == 1


def test_rejected_submission_is_failed_instead_of_remote_status_unknown(tmp_path, image):
    server = ComfyServer()

    def reject_workflow(request):
        if request.url.path == "/prompt":
            return httpx.Response(400, json={"error": "invalid workflow"})
        return server.request(request)

    command = MediaService(tmp_path).prepare_video(image["image_id"], "Use <Picture 1>.")
    with pytest.raises(httpx.HTTPStatusError):
        MediaWork(str(tmp_path), command, transport=httpx.MockTransport(reject_workflow))(
            lambda *_: None
        )
    assert MediaRepository(tmp_path).jobs()[0]["status"] == "failed"


def test_reserved_image_workflow_does_not_block_video_preparation(tmp_path, image):
    repository = MediaRepository(tmp_path)
    repository.write(
        "settings.json",
        {
            "base_url": "http://127.0.0.1:8188",
            "video_workflow": str(DEFAULT_VIDEO_WORKFLOW),
            "image_workflow": str(tmp_path / "not-implemented.json"),
        },
    )
    command = MediaService(tmp_path).prepare_video(image["image_id"], "Use <Picture 1>.")
    assert command["image"]["image_id"] == image["image_id"]


def test_local_cancel_never_sends_global_comfy_interrupt(tmp_path, image):
    server = ComfyServer()
    server.running = True
    command = MediaService(tmp_path).prepare_video(image["image_id"], "Use <Picture 1>.")
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
    command = MediaService(tmp_path).prepare_video(image["image_id"], "Use <Picture 1>.")
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
    with pytest.raises(WorkspaceError, match="只连接一张"):
        service.prepare_video(image["image_id"], "Use <Picture 2>.")
    MediaRepository(tmp_path).resolve(image["relative_path"]).write_bytes(PNG + b"changed")
    with pytest.raises(WorkspaceError, match="已被修改"):
        service.prepare_video(image["image_id"], "Use <Picture 1>.")


def test_video_ui_accepts_manual_image_without_storyboard(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication

    from core.gui.pages.short_video.media_preview import MediaPreview

    app = QApplication.instance() or QApplication([])
    submitted = []
    workbench = SimpleNamespace(
        output_dir=str(tmp_path),
        host=SimpleNamespace(runner=SimpleNamespace(busy=False)),
        perform=lambda operation: operation(),
        configure_comfy=lambda: None,
        submit=lambda work, button: submitted.append(work),
    )
    panel = MediaPreview(workbench)
    image = QImage(8, 8, QImage.Format_RGB32)
    image.fill(0xFF0088)
    path = tmp_path / "ui-reference.png"
    assert image.save(str(path))
    panel.import_images([str(path)])
    panel.generate_button.click()
    app.processEvents()
    assert len(submitted) == 1
    assert "<Picture 1>" in submitted[0].command["prompt"]
    panel.prompt.setPlainText("Use <Picture 1>, slow camera push.")
    panel.generate_button.click()
    app.processEvents()
    assert len(submitted) == 2
    assert submitted[1].command["image"]["name"] == "ui-reference.png"
    assert submitted[1].command["prompt"].endswith("slow camera push.")
    panel.close()
