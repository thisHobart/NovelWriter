"""Offline Images API, per-view provenance, bounded concurrency and real crash recovery."""

import asyncio
import base64
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import httpx
import pytest

from core.generation import api_settings
from core.generation.cancellation import GenerationCancelled
from core.short_video.v2 import image_generation as images
from core.short_video.v2.feedback import task_message
from core.short_video.v2.models import WorkspaceError
from core.short_video.v2.workflow import WorkflowService
from test_video_v2_repository import project, prompt_project, prompt_page  # noqa: F401


@pytest.fixture
def configured(prompt_project, monkeypatch, tmp_path):
    monkeypatch.setattr(api_settings, "settings_file", lambda: tmp_path / "image-settings.json")
    api_settings.save_provider("hosted-llm", "https://images.test/v1", "image-test-key", "image-model", purpose="image")
    monkeypatch.setattr(images, "retry_delay", lambda *_: 0)
    return prompt_project


@pytest.fixture
def png():
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QImage
    image = QImage(8, 8, QImage.Format_RGB32)
    image.fill(0xFFAA44)
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(buffer.data())


def reply(png):
    return httpx.Response(200, json={"created": 1, "data": [{"b64_json": base64.b64encode(png).decode()}]})


def prepare(repository, count=1, concurrency=2):
    service = images.ImageGenerationService(repository)
    return service.prepare([r["key"] for r in service.rows()[:count]], concurrency=concurrency)


def test_rows_and_disabled_image_generation_need_no_image_configuration(prompt_project, monkeypatch, tmp_path):
    monkeypatch.setattr(api_settings, "settings_file", lambda: tmp_path / "missing.json")
    original = prompt_project.path.read_bytes()
    service = images.ImageGenerationService(prompt_project)
    rows = service.rows()
    assert len(rows) == 9 and all(not r["reason"] for r in rows)
    assert all(r["image"] is None and r["shots"] == [1] for r in rows)
    with pytest.raises(ValueError, match="生图 API 配置"):
        service.prepare([rows[0]["key"]])
    assert prompt_project.path.read_bytes() == original
    assert not service.store.media.resolve("image_generation.json").exists()


def test_real_sdk_concurrency_prompt_and_current_provenance(configured, png):
    active, peak, requests = 0, 0, []
    command = prepare(configured, 5, concurrency=2)
    before = configured.path.read_bytes()

    async def handler(request):
        nonlocal active, peak
        assert request.url.path == "/v1/images/generations"
        assert request.headers["authorization"] == "Bearer image-test-key"
        body = json.loads(request.content)
        requests.append(body)
        assert set(body) == {"model", "prompt", "n"}
        assert body["model"] == "image-model" and body["n"] == 1
        assert "排除或避免的元素" in body["prompt"]
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return reply(png)

    work = images.ImageGenerationWork(configured, command, transport=httpx.MockTransport(handler))
    result = work(lambda *_: None)
    assert peak == 2 and len(requests) == 5
    assert result["counts"]["succeeded"] == 5
    assert configured.path.read_bytes() == before
    store = work.store.load()
    assert len(store["bindings"]) == 5
    for item in store["batches"][command["id"]]["items"]:
        image = work.store.media.image(item["image_id"])
        assert image["source"]["request_prompt"] == item["request_prompt"]
        assert image["source"]["model_snapshot"] == command["model_snapshot"]
        assert work.store.media.resolve(image["relative_path"]).read_bytes() == png
    assert "image-test-key" not in json.dumps(store)
    assert all(r["image"]["current"] for r in images.ImageGenerationService(configured).rows()[:5])
    view = WorkflowService(configured).shot_assets("shot_1")[0]["views"][0]
    assert view["image"]["image_id"] == command["items"][0]["image_id"]
    assert task_message(work.task_context, result=result)[0] == "success"


def test_url_failure_only_downloads_on_resume_and_preserves_successes(configured, png, monkeypatch):
    calls = []
    command = prepare(configured, 2)

    def handler(request):
        calls.append((request.method, request.url.host))
        if request.method == "POST":
            if sum(m == "POST" for m, _ in calls) == 1:
                return reply(png)
            return httpx.Response(200, json={"data": [{"url": "https://cdn.test/result.png"}]})
        assert "authorization" not in request.headers
        return httpx.Response(503)

    work = images.ImageGenerationWork(configured, command, transport=httpx.MockTransport(handler))
    result = work(lambda *_: None)
    assert result["counts"]["succeeded"] == result["counts"]["failed"] == 1
    first = work.store.batch(command["id"])["items"][0]["image_id"]
    monkeypatch.setattr(images, "get_image_config", Mock(side_effect=AssertionError("cached response requires no generation config")))
    resumed = []
    def download(request):
        resumed.append(request.method)
        return httpx.Response(200, content=png)
    result = images.ImageGenerationWork(configured, batch_id=command["id"], transport=httpx.MockTransport(download))(lambda *_: None)
    assert resumed == ["GET"] and result["status"] == "succeeded"
    assert work.store.batch(command["id"])["items"][0]["image_id"] == first


@pytest.mark.parametrize("status", [401, 403, 500])
def test_permanent_errors_do_not_use_sdk_or_automatic_generation_retries(configured, status):
    handler = Mock(return_value=httpx.Response(status, json={"error": {"message": "original image error"}}))
    command = prepare(configured, 2, concurrency=1)
    result = images.ImageGenerationWork(configured, command, transport=httpx.MockTransport(handler))(lambda *_: None)
    assert handler.call_count == (1 if status in {401, 403} else 2)
    assert result["status"] == "partial"
    assert result["counts"]["failed"] >= 1


@pytest.mark.parametrize("kind", ["connection", "rate_limit"])
def test_safe_network_failures_retry_but_only_inside_image_adapter(configured, png, kind):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            if kind == "connection":
                raise httpx.ConnectError("connect failed", request=request)
            return httpx.Response(429, headers={"retry-after-ms": "0"}, json={"error": {"message": "rate limit"}})
        return reply(png)
    command = prepare(configured)
    work = images.ImageGenerationWork(configured, command, transport=httpx.MockTransport(handler))
    assert work(lambda *_: None)["status"] == "succeeded"
    assert len(calls) == 3 and work.store.batch(command["id"])["items"][0]["attempts"] == 3


def test_read_timeout_unknown_requires_explicit_selection(configured, png):
    calls = []
    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("response lost", request=request)
    command = prepare(configured)
    work = images.ImageGenerationWork(configured, command, transport=httpx.MockTransport(timeout))
    assert work(lambda *_: None)["counts"]["unknown"] == 1
    assert len(calls) == 1
    handler = Mock(return_value=reply(png))
    images.ImageGenerationWork(configured, batch_id=command["id"], transport=httpx.MockTransport(handler))(lambda *_: None)
    handler.assert_not_called()
    result = images.ImageGenerationWork(configured, batch_id=command["id"], keys=[command["items"][0]["key"]],
                                        transport=httpx.MockTransport(handler))(lambda *_: None)
    assert result["status"] == "succeeded" and handler.call_count == 1


def test_stop_drains_sent_requests_and_resume_skips_saved_items(configured, png):
    command = prepare(configured, 5, concurrency=2)
    calls = []
    async def handler(request):
        calls.append(request)
        if len(calls) == 2:
            work.token.cancel()
        await asyncio.sleep(0.02)
        return reply(png)
    work = images.ImageGenerationWork(configured, command, transport=httpx.MockTransport(handler))
    messages = []
    with pytest.raises(GenerationCancelled):
        work(lambda text, _: messages.append(text))
    assert len(calls) == 2
    batch = work.store.batch(command["id"])
    assert [i["status"] for i in batch["items"]].count("succeeded") == 2
    assert [i["status"] for i in batch["items"]].count("paused") == 3
    assert any("正在停止" in m for m in messages)
    handler = Mock(return_value=reply(png))
    result = images.ImageGenerationWork(configured, batch_id=command["id"], transport=httpx.MockTransport(handler))(lambda *_: None)
    assert result["status"] == "succeeded" and handler.call_count == 3


def test_prompt_changes_mark_images_stale_without_invalidating_other_views(configured, png):
    service = images.ImageGenerationService(configured)
    command = prepare(configured, 2)
    images.ImageGenerationWork(configured, command, transport=httpx.MockTransport(lambda _: reply(png)))(lambda *_: None)
    spec = configured.current("scene_spec:asset_scene:base")
    content = deepcopy(spec["content"])
    content["views"][1]["positive_prompt"] += "，这是更新后的反向视图"
    configured.save(spec["kind"], content, spec["dependency_refs"], expected_revision=configured.load()["revision"])
    first, second = service.rows()[:2]
    assert first["image"]["current"] and not second["image"]["current"]
    assert second["image"]["source"]["positive_prompt"] != second["positive_prompt"]
    assert service.store.media.resolve(second["image"]["relative_path"]).exists()


def test_source_changes_during_request_save_file_without_selecting_it(configured, png):
    command = prepare(configured)
    def handler(_):
        spec = configured.current("scene_spec:asset_scene:base")
        content = deepcopy(spec["content"])
        content["views"][0]["positive_prompt"] += "，已修改"
        configured.save(spec["kind"], content, spec["dependency_refs"], expected_revision=configured.load()["revision"])
        return reply(png)
    work = images.ImageGenerationWork(configured, command, transport=httpx.MockTransport(handler))
    work(lambda *_: None)
    item = work.store.batch(command["id"])["items"][0]
    assert item["status"] == "succeeded" and not item["current"]
    assert work.store.media.image(item["image_id"])["source"]["prompt_hash"] == command["items"][0]["prompt_hash"]
    assert not work.store.load()["bindings"]


@pytest.mark.parametrize("failure", ["invalid_png", "invalid_base64", "empty_data", "unsupported_data"])
def test_invalid_response_is_reported_per_item(configured, failure):
    payload = {"data": [{"b64_json": base64.b64encode(b"not an image").decode()}]}
    if failure == "invalid_base64":
        payload = {"data": [{"b64_json": "!!!"}]}
    elif failure == "empty_data":
        payload = {"data": []}
    elif failure == "unsupported_data":
        payload = {"data": [{"other": "unsupported"}]}
    command = prepare(configured)
    work = images.ImageGenerationWork(configured, command, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    assert work(lambda *_: None)["counts"]["failed"] == 1
    assert work.store.batch(command["id"])["items"][0]["error"]
    assert not work.store.load()["bindings"]


def test_changed_configuration_and_source_block_new_requests(configured, png):
    command = prepare(configured)
    api_settings.save_provider("hosted-llm", "https://other.test/v1", "new-key", "other-model", purpose="image")
    handler = Mock(return_value=reply(png))
    with pytest.raises(WorkspaceError, match="生图模型或接口已变化"):
        images.ImageGenerationWork(configured, command, transport=httpx.MockTransport(handler))(lambda *_: None)
    handler.assert_not_called()
    api_settings.save_provider("hosted-llm", "https://images.test/v1", "rotated-key", "image-model", purpose="image")
    assert images.ImageGenerationWork(configured, batch_id=command["id"], transport=httpx.MockTransport(handler))(lambda *_: None)["status"] == "succeeded"
    assert handler.call_count == 1


@pytest.mark.parametrize("boundary", ["requesting", "response_saved", "image_saved", "selected"])
def test_real_crash_recovers_without_regenerating_saved_response(configured, png, boundary):
    script = r'''
import os, sys, base64, httpx
from pathlib import Path
from core.generation import api_settings
from core.short_video.v2 import image_generation as images
from core.short_video.v2.repository import ProjectRepository
api_settings.settings_file = lambda: Path(sys.argv[2])
r = ProjectRepository(sys.argv[1])
original = images.MediaRepository.write
def write(self, path, data):
    original(self, path, data)
    hit = sys.argv[3] == 'response_saved' and path.startswith('image_responses/')
    hit |= sys.argv[3] == 'image_saved' and path.startswith('images/')
    if path == 'image_generation.json':
        batch = list(data['batches'].values())[-1]
        hit |= sys.argv[3] == 'requesting' and batch['items'][0]['status'] == 'requesting'
        hit |= sys.argv[3] == 'selected' and batch['items'][0]['status'] == 'succeeded'
    if hit:
        os._exit(73)
images.MediaRepository.write = write
service = images.ImageGenerationService(r)
command = service.prepare([service.rows()[0]['key']])
handler = lambda request: httpx.Response(200,json={'data':[{'b64_json':sys.argv[4]}]})
images.ImageGenerationWork(r,command,transport=httpx.MockTransport(handler))(lambda *_:None)
'''
    process = subprocess.run([sys.executable, "-c", script, configured.output_dir,
                              str(api_settings.settings_file()), boundary, base64.b64encode(png).decode()],
                             cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=30)
    assert process.returncode == 73, process.stderr.decode(errors="replace")
    service = images.ImageGenerationService(configured)
    before = service.store.load()
    service.recover()
    batch = list(service.store.load()["batches"].values())[-1]
    assert before["batches"][batch["id"]]["status"] == "running"
    assert batch["status"] == "interrupted"
    handler = Mock(side_effect=AssertionError("must not regenerate"))
    result = images.ImageGenerationWork(configured, batch_id=batch["id"], transport=httpx.MockTransport(handler))(lambda *_: None)
    assert result["status"] == ("partial" if boundary == "requesting" else "succeeded")
    assert result["counts"]["unknown"] == (1 if boundary == "requesting" else 0)
    handler.assert_not_called()


def test_batch_table_is_opt_in_and_generated_view_can_be_used_at_selected_node(configured, prompt_page, png, monkeypatch):
    from PySide6.QtCore import Qt
    page, _ = prompt_page
    page.tabs.setCurrentIndex(4)
    panel = page.images_page
    assert panel.table.rowCount() == 9 and panel.checked_keys() == []
    assert not panel.generate_button.isEnabled()
    assert page.selector_bar.isHidden() and page.action_bar.isHidden()
    panel.select_all_button.click()
    assert len(panel.checked_keys()) == 9
    panel.clear_button.click()
    assert panel.checked_keys() == []
    submitted = []
    page.workbench.submit = lambda work, button: submitted.append(work)
    panel.table.item(0, 0).setCheckState(Qt.Checked)
    panel.generate_button.click()
    assert len(submitted) == 1 and submitted[0].command["concurrency"] == 2
    work = submitted[0]
    work.transport = httpx.MockTransport(lambda _: reply(png))
    work(lambda *_: None)
    page.refresh()
    panel.table.setCurrentCell(0, 1)
    assert panel.preview.image["current"]
    page.tabs.setCurrentIndex(3)
    generated = work.command["items"][0]["image_id"]
    views = [view for asset in page.media.shot_assets for view in asset["views"]]
    assert [view["image"]["image_id"] for view in views if view.get("image")] == [generated]
    assert not any(page.media.image_selection().values())
    original_prompt = page.media.prompt.toPlainText()
    use_asset_image(page, "139")
    assert page.media.image_selection()["139"] == generated
    assert page.media.image_selection()["137"] is None and page.media.image_selection()["147"] is None
    assert page.media.image_fields["139"].summary() == "图片 2 · <Picture 1>"
    assert page.media.prompt.toPlainText() == original_prompt


def use_asset_image(page, node_id):
    """Pick the one generated asset view from the 本镜资产 part of the slot's picker."""
    menu = page.media.picker_menu(node_id)
    usable = [
        action
        for action in menu.actions()
        if action.text() and action.isEnabled() and action.text() != "从文件选择…"
    ]
    assert len(usable) == 1
    usable[0].trigger()


def test_response_cache_failure_is_unknown_and_never_automatically_regenerated(configured, png, monkeypatch):
    command = prepare(configured)
    handler = Mock(return_value=reply(png))
    work = images.ImageGenerationWork(configured, command, transport=httpx.MockTransport(handler))
    original = work.store.media.write
    failure = OSError("response cache disk full")
    def write(path, value):
        if path.startswith("image_responses/"):
            raise failure
        original(path, value)
    monkeypatch.setattr(work.store.media, "write", write)
    with pytest.raises(OSError) as caught:
        work(lambda *_: None)
    assert caught.value is failure and handler.call_count == 1
    assert work.store.batch(command["id"])["items"][0]["status"] == "unknown"
    result = images.ImageGenerationWork(configured, batch_id=command["id"], transport=httpx.MockTransport(handler))(lambda *_: None)
    assert result["counts"]["unknown"] == 1 and handler.call_count == 1


def test_image_disk_failure_resumes_from_cached_response(configured, png, monkeypatch):
    command = prepare(configured)
    handler = Mock(return_value=reply(png))
    work = images.ImageGenerationWork(configured, command, transport=httpx.MockTransport(handler))
    failure = OSError("image disk full")
    monkeypatch.setattr(work.store.media, "write_file", Mock(side_effect=failure))
    with pytest.raises(OSError) as caught:
        work(lambda *_: None)
    assert caught.value is failure and handler.call_count == 1
    result = images.ImageGenerationWork(configured, batch_id=command["id"], transport=httpx.MockTransport(handler))(lambda *_: None)
    assert result["status"] == "succeeded" and handler.call_count == 1


def test_source_is_rechecked_before_a_network_retry(configured, png):
    calls = []
    def handler(request):
        calls.append(request)
        spec = configured.current("scene_spec:asset_scene:base")
        content = deepcopy(spec["content"])
        content["views"][0]["positive_prompt"] += "，新要求"
        configured.save(spec["kind"], content, spec["dependency_refs"], expected_revision=configured.load()["revision"])
        return httpx.Response(429, headers={"retry-after-ms": "0"}, json={"error": {"message": "rate limit"}})
    command = prepare(configured)
    work = images.ImageGenerationWork(configured, command, transport=httpx.MockTransport(handler))
    assert work(lambda *_: None)["counts"]["stale"] == 1 and len(calls) == 1


def test_missing_views_are_unselectable_and_switching_workflows_updates_target_nodes(configured, prompt_page, png):
    from PySide6.QtCore import Qt
    command = prepare(configured)
    images.ImageGenerationWork(configured, command, transport=httpx.MockTransport(lambda _: reply(png)))(lambda *_: None)
    page, _ = prompt_page
    page.tabs.setCurrentIndex(3)
    page.media.add_reference_image()
    assert "reference_input_4" in page.media.image_fields
    page.media.workflows.setCurrentIndex(page.media.workflows.findData("minimax-first-last"))
    assert "reference_input_4" not in page.media.image_fields
    assert len(page.media.image_fields) == 5
    first_frame = next(i for i in page.media.template.image_inputs if i.role == "first_frame")
    use_asset_image(page, first_frame.node_id)
    assert page.media.image_selection()[first_frame.node_id] == command["items"][0]["image_id"]
    assert page.media.image_fields[first_frame.node_id].tag_text() == "首帧图片"
    with configured.transaction() as state:
        state["current"].pop("prop_spec:asset_prop:base")
    page.tabs.setCurrentIndex(4)
    panel = page.images_page
    missing = [i for i, r in enumerate(panel.rows) if r["asset_id"] == "asset_prop"]
    assert len(missing) == 3
    assert all(not panel.table.item(i, 0).flags() & Qt.ItemIsUserCheckable for i in missing)
    assert all(panel.rows[i]["display_status"] == "提示词尚未生成" for i in missing)


def test_rows_deduplicate_shared_assets_and_keep_image_state_out_of_text_manifest(configured):
    spec = configured.current("storyboard")
    content = deepcopy(spec["content"])
    content["shots"].append({**deepcopy(content["shots"][0]), "shot_id": "shared_shot"})
    configured.save("storyboard", content, spec["dependency_refs"], expected_revision=configured.load()["revision"])
    original = configured.path.read_bytes()
    service = images.ImageGenerationService(configured)
    rows = service.rows()
    assert len(rows) == 9 and all(r["shots"] == [1, 2] for r in rows)
    service.prepare([rows[0]["key"]])
    assert configured.path.read_bytes() == original


def test_frozen_input_corruption_stops_before_a_paid_request(configured, png):
    command = prepare(configured)
    command["items"][0]["request_prompt"] = "corrupted task input"
    handler = Mock(return_value=reply(png))
    with pytest.raises(WorkspaceError, match="冻结输入已变化"):
        images.ImageGenerationWork(configured, command, transport=httpx.MockTransport(handler))(lambda *_: None)
    handler.assert_not_called()


def test_selecting_a_row_recovers_its_original_download_even_after_another_batch(configured, prompt_page, png):
    service = images.ImageGenerationService(configured)
    rows = service.rows()
    first = service.prepare([rows[0]["key"]])
    def first_handler(request):
        return (httpx.Response(200, json={"data": [{"url": "https://cdn.test/picture.png"}]})
                if request.method == "POST" else httpx.Response(503))
    images.ImageGenerationWork(configured, first, transport=httpx.MockTransport(first_handler))(lambda *_: None)
    second = service.prepare([rows[1]["key"]])
    images.ImageGenerationWork(configured, second, transport=httpx.MockTransport(lambda _: reply(png)))(lambda *_: None)
    page, _ = prompt_page
    page.tabs.setCurrentIndex(4)
    panel = page.images_page
    assert panel.rows[0]["display_status"] == "失败"
    panel.table.setCurrentCell(0, 1)
    assert panel.batch_id == first["id"] and panel.resume_button.isEnabled()
    submitted = []
    page.workbench.submit = lambda work, button: submitted.append(work)
    panel.resume_button.click()
    requests = []
    def download(request):
        requests.append(request.method)
        return httpx.Response(200, content=png)
    submitted[0].transport = httpx.MockTransport(download)
    assert submitted[0](lambda *_: None)["status"] == "succeeded"
    assert requests == ["GET"]
