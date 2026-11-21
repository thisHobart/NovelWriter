"""LLM video prompts use exact shot assets, image slots and durable text checkpoints."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_video_v2_repository import project, prompt_page, prompt_project  # noqa: F401

from core.short_video.v2 import models, runtime, workflow
from core.short_video.v2.media_repository import MediaRepository
from core.short_video.v2.media_workflows import MediaConfig, parse_video_workflow
from core.short_video.v2.models import WorkspaceError, encode
from core.short_video.v2.video_prompt import snapshot_video_inputs
from core.short_video.v2.workflow import WorkflowService

BODY = (
    "<Picture 1>：用作老宅布局与光线的参考。\n\n"
    "视频提示词（7秒）\n"
    "7秒，中景，写实质感，固定镜头。\n"
    "声音：风声，旁白“井里是谁”。\n"
    "限制：避免身份漂移、画面变形、文字和水印。\n\n"
    "0–3秒｜握紧铜钥匙\n寻井人站在井边，将铜钥匙握紧。\n"
    "3–7秒｜举起铜钥匙\n寻井人举起铜钥匙，手停在胸前。"
)


@pytest.fixture
def ready(prompt_project, monkeypatch):
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QImage

    monkeypatch.setattr(
        workflow, "capture_model_snapshot", lambda _: {"provider": "offline", "model": "offline"}
    )
    monkeypatch.setattr(runtime, "validate_model_snapshot", lambda _: None)
    monkeypatch.setattr(models, "validate_review_model_snapshot", lambda _: None)
    def png(color):
        image = QImage(8, 8, QImage.Format_RGB32)
        image.fill(color)
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        assert image.save(buffer, "PNG")
        return bytes(buffer.data())

    media = MediaRepository(prompt_project.output_dir)
    spec = prompt_project.current("scene_spec:asset_scene:base")
    first = media.save_image(
        png(0xFFAA44),
        name="scene",
        source={
            "kind": "image_generation",
            "catalog_id": prompt_project.current("asset_catalog")["id"],
            "asset_id": "asset_scene",
            "variant_id": "base",
            "view_id": "reverse",
            "spec_id": spec["id"],
        },
    )
    second = media.save_image(png(0x3344FF), name="unlabeled")
    return prompt_project, first, second


def prepare(ready, **kwargs):
    repository, image, _ = ready
    return WorkflowService(repository).prepare_video_prompt(
        "shot_1",
        {"137": image["image_id"], "139": None, "147": None},
        workflow_id="minimax-reference",
        parameters={"132.value": 7.0},
        **kwargs,
    )


def response(repository, body=BODY):
    return {"shot_id": "shot_1", "video_motion_prompt": body}


def request_text(request):
    return request if isinstance(request, str) else request[0]["content"][0]["text"]


def test_prepare_freezes_only_current_shot_assets_and_actual_picture_order(ready):
    repository, first, second = ready
    before = repository.path.read_bytes()
    command = WorkflowService(repository).prepare_video_prompt(
        "shot_1",
        {"137": first["image_id"], "139": None, "reference_input_4": second["image_id"]},
        workflow_id="minimax-reference",
        parameters={"132.value": 6.5},
        current_prompt="仅作措辞参考",
    )
    assert repository.path.read_bytes() == before
    assert command["stage"] == "video_prompt" and command["kind"] == "prompt_bundle"
    context = command["context"]
    assert [i["reference"] for i in context["video_inputs"]["images"]] == [
        "<Picture 1>",
        "<Picture 2>",
    ]
    assert context["video_inputs"]["duration_seconds"] == 6.5
    assert [i["image_name"] for i in context["video_inputs"]["images"]] == ["scene", "unlabeled"]
    assert all("asset_description" not in i for i in context["video_inputs"]["images"])
    assert command["context"]["video_input_binding"]["images"][1].keys() == {
        "node_id", "image_id", "sha256", "role", "reference", "asset_source"
    }
    assert context["storyboard"]["content"]["shots"][0]["action"] == "举起铜钥匙"
    assert all("views" not in spec["content"] for spec in context["asset_specs"])
    assert context["current_video_prompt"] == "仅作措辞参考"
    assert "MiniMax H3 提示词工程师" in command["prompt"]


def test_request_tells_the_model_what_every_picture_shows(ready):
    """Every picture, added by hand or generated for an asset view, is sent by its name only."""
    from core.short_video.v2.video_context import model_context

    repository, first, _ = ready
    media = MediaRepository(repository.output_dir)
    noodles = media.save_image(media.resolve(first["relative_path"]).read_bytes(),
                               name="蓝白瓷碗香辣牛肉面.png")
    command = WorkflowService(repository).prepare_video_prompt(
        "shot_1",
        {"137": noodles["image_id"], "139": first["image_id"]},
        workflow_id="minimax-reference",
        parameters={"132.value": 6.5},
    )
    pictures = model_context(command["context"])["video_inputs"]
    assert pictures == {
        "kind": "reference",
        "duration_seconds": 6.5,
        "images": [
            {"reference": "<Picture 1>", "role": "reference", "image_name": "蓝白瓷碗香辣牛肉面"},
            {"reference": "<Picture 2>", "role": "reference", "image_name": first["name"]},
        ],
    }
    assert "蓝白瓷碗香辣牛肉面" in command["prompt"]
    # The body format: one usage line per picture, then the timed sections.
    assert "<Picture 1>：" in command["prompt"] and "首帧图片：" in command["prompt"]
    assert "〔N〕秒" in command["prompt"] and "〔起〕–〔止〕秒｜" in command["prompt"]
    assert noodles["sha256"] not in command["prompt"]


def test_first_last_frame_roles_never_consume_picture_numbers(ready):
    repository, image, _ = ready
    template = parse_video_workflow(
        MediaConfig.load(repository.output_dir).selected("minimax-first-last")["graph"]
    )
    inputs = snapshot_video_inputs(
        repository.output_dir,
        {i.node_id: image["image_id"] for i in template.image_inputs},
        "minimax-first-last",
        {},
    )
    refs = [i for i in inputs["images"] if i["role"] == "reference"]
    frames = [i for i in inputs["images"] if i["role"] != "reference"]
    assert [i["reference"] for i in refs] == ["<Picture 1>", "<Picture 2>", "<Picture 3>"]
    assert [i["reference"] for i in frames] == ["首帧图片", "尾帧图片"]


@pytest.mark.parametrize("nodes", [("147", "148"), ("147",), ("148",)])
def test_video_prompt_uses_only_selected_frame_roles_and_can_resume_binding(ready, nodes):
    from core.short_video.v2.video_prompt import validate_video_inputs

    repository, image, _ = ready
    command = WorkflowService(repository).prepare_video_prompt(
        "shot_1", {key: image["image_id"] if key in nodes else None
                   for key in ("137", "139", "152", "147", "148")},
        workflow_id="minimax-first-last", parameters={})
    binding = command["context"]["video_input_binding"]
    assert {i["node_id"] for i in binding["images"]} == set(nodes)
    assert all(i["role"] != "reference" and "Picture" not in i["reference"]
               for i in binding["images"])
    validate_video_inputs(repository.output_dir, binding)
    with pytest.raises(WorkspaceError, match="未选择的参考图片"):
        WorkflowService(repository)._validate_output(command, response(repository))
    uses = "\n".join(
        f"{i['reference']}：约束{'起始' if i['role'] == 'first_frame' else '结束'}画面。"
        for i in binding["images"]
    )
    body = uses + "\n" + BODY.split("\n", 1)[1]
    send = Mock(side_effect=[encode(response(repository, body)), '{"issues":[]}'])
    result = WorkflowService(repository, transport=send).execute(command)
    assert result["review_status"] == "passed"
    assert repository.current("prompt_bundle")["content"]["shots"][0]["video_prompt_binding"] == binding


def test_generation_review_revision_preserves_shot_text_and_saves_binding(ready):
    repository, _, _ = ready
    command = prepare(ready)
    original = deepcopy(repository.current("prompt_bundle")["content"])
    calls = []

    def send(request):
        prompt = request_text(request)
        calls.append(prompt)
        if prompt.startswith("自动审阅"):
            assert "video_motion_prompt" in prompt and "video_inputs" in prompt
            return encode(
                {
                    "issues": [
                        {
                            "rule_id": "VIDEO-CAPACITY",
                            "severity": "warning",
                            "target_path": "video_motion_prompt",
                            "explanation": "动作需要精简",
                            "suggestion": "仅保留举起钥匙这一主要动作",
                            "quote": None,
                            "source_refs": [],
                        }
                    ]
                }
            )
        return encode(response(repository, BODY + ("保持动作简洁。" if len(calls) > 1 else "")))

    result = WorkflowService(repository, transport=send).execute(command)
    assert result["status"] == "succeeded" and result["review_status"] == "issues"
    assert len(calls) == 4 and "仅保留举起钥匙" in calls[2]
    saved = repository.current("prompt_bundle")["content"]["shots"][0]
    assert saved["resolved_prompt"] == original["shots"][0]["resolved_prompt"]
    assert saved["negative_prompt"] == original["shots"][0]["negative_prompt"]
    assert saved["video_motion_prompt"].endswith("保持动作简洁。")
    assert saved["video_prompt_binding"] == command["context"]["video_input_binding"]
    assert repository.current("storyboard")["content"]["shots"][0]["action"] == "举起铜钥匙"
    assert MediaRepository(repository.output_dir).jobs() == []


def test_video_generation_repair_and_revision_send_selected_image_bytes_only_to_generator(
    ready, monkeypatch, capsys
):
    import base64
    import json

    import httpx
    from openai import AsyncOpenAI

    from core.common import api_providers
    from core.short_video.v2 import llm_trace, service_settings

    repository, first, second = ready
    command = WorkflowService(repository).prepare_video_prompt(
        "shot_1",
        {"137": first["image_id"], "139": None, "147": second["image_id"], "148": first["image_id"]},
        workflow_id="minimax-first-last",
        parameters={},
    )
    generator = api_providers.APIConfig("offline", "https://generate.test/v1", "vision", "vision-key")
    reviewer = api_providers.APIConfig("hosted-llm", "https://review.test/v1", "text", "text-key")
    monkeypatch.setattr(api_providers, "get_config", lambda _: generator)
    monkeypatch.setattr(service_settings, "get_review_config", lambda _: reviewer)
    monkeypatch.setenv("NOVELWRITER_LLM_TRACE", "1")
    monkeypatch.setattr(llm_trace, "_secrets", set())
    warning = {
        "rule_id": "VIDEO-CAPACITY", "severity": "warning", "target_path": "video_motion_prompt",
        "explanation": "动作需要精简", "suggestion": "仅保留主要动作", "quote": None, "source_refs": [],
    }
    outputs = iter([
        "不是 JSON",
        encode(response(repository)),
        encode({"issues": [warning]}),
        encode(response(repository)),
        '{"issues":[]}',
    ])
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append((request.url.host, payload["messages"][0]["content"]))
        assert "data:image/" not in repository.path.read_text(encoding="utf-8")
        return httpx.Response(200, json={
            "id": "reply", "object": "chat.completion", "created": 1, "model": payload["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": next(outputs)},
                         "finish_reason": "stop"}],
        })

    monkeypatch.setattr(api_providers, "AsyncOpenAI", lambda **kwargs: AsyncOpenAI(
        **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ))
    result = WorkflowService(repository).execute(command)
    assert result["review_status"] == "passed"
    assert [host for host, _ in calls] == [
        "generate.test", "generate.test", "review.test", "generate.test", "review.test",
    ]
    parts = calls[0][1]
    assert "具体描述中间可见的位置、姿态或道具变化" in parts[0]["text"]
    media = MediaRepository(repository.output_dir)
    labels = ["<Picture 1>", "首帧图片", "尾帧图片"]
    assert len(parts) == 1 + 2 * len(labels)
    for label, image, caption, attachment in zip(labels, (first, second, first), parts[1::2], parts[2::2]):
        assert caption["type"] == "text" and label in caption["text"]
        assert attachment["type"] == "image_url"
        mime, data = attachment["image_url"]["url"].split(",", 1)
        assert mime == "data:image/png;base64"
        assert base64.b64decode(data) == media.resolve(image["relative_path"]).read_bytes()
    assert calls[1][1][1:] == calls[3][1][1:] == parts[1:]
    assert all(isinstance(calls[i][1], str) for i in (2, 4))
    trace = capsys.readouterr().err
    assert "data:image/" not in trace and "[IMAGE DATA REDACTED]" in trace


def test_model_rejecting_video_prompt_images_does_not_fall_back_or_replace_draft(ready):
    import httpx
    from openai import BadRequestError

    repository, _, _ = ready
    command = prepare(ready)
    before = repository.current("prompt_bundle")["id"]
    http_response = httpx.Response(400, request=httpx.Request("POST", "https://example.test/v1"))
    send = Mock(side_effect=BadRequestError("unsupported image_url", response=http_response, body=None))
    with pytest.raises(WorkspaceError, match="请确认接口和模型支持多图输入"):
        WorkflowService(repository, transport=send).execute(command)
    send.assert_called_once()
    assert repository.current("prompt_bundle")["id"] == before


def test_oversized_video_prompt_images_stop_before_model_request(ready, monkeypatch):
    from core.short_video.v2 import video_prompt

    repository, _, _ = ready
    command = prepare(ready)
    monkeypatch.setattr(video_prompt, "MAX_PROMPT_IMAGE_BYTES", 1)
    send = Mock()
    with pytest.raises(WorkspaceError, match="选图总大小超过"):
        WorkflowService(repository, transport=send).execute(command)
    send.assert_not_called()


@pytest.mark.parametrize("change", ["picture", "shot_text", "other_shot"])
def test_output_cannot_invent_picture_or_modify_unrelated_content(ready, change):
    repository, _, _ = ready
    if change == "other_shot":
        board = repository.current("storyboard")
        content = deepcopy(board["content"])
        content["shots"].append({**deepcopy(content["shots"][0]), "shot_id": "shot_2"})
        repository.save(
            "storyboard",
            content,
            board["dependency_refs"],
            expected_revision=repository.load()["revision"],
        )
        WorkflowService(repository).compile_storyboard_prompts(
            expected_revision=repository.load()["revision"]
        )
    command = prepare(ready)
    candidate = response(repository)
    if change == "picture":
        candidate["video_motion_prompt"] += "以 <Picture 2> 为参考。"
    elif change == "shot_text":
        candidate["resolved_prompt"] = "篡改画面文字"
    else:
        candidate["shot_id"] = "shot_2"
    with pytest.raises(WorkspaceError):
        WorkflowService(repository)._validate_output(command, candidate)


def test_changed_image_or_workflow_stops_before_any_model_request(ready):
    repository, image, _ = ready
    command = prepare(ready)
    media = MediaRepository(repository.output_dir)
    media.resolve(image["relative_path"]).write_bytes(b"changed")
    send = Mock(side_effect=AssertionError("stale inputs must not call models"))
    with pytest.raises(WorkspaceError, match="图片文件已变化"):
        WorkflowService(repository, transport=send).execute(command)
    send.assert_not_called()


def test_interrupted_review_resumes_without_regenerating_video_text(ready):
    repository, _, _ = ready
    command = prepare(ready)
    generated = Mock(return_value=encode(response(repository)))
    service = WorkflowService(
        repository,
        transport=generated,
        quality_check=Mock(side_effect=ConnectionError("review interrupted")),
    )
    with pytest.raises(ConnectionError):
        service.execute(command)
    assert generated.call_count == 1
    task = runtime.get_recovery(repository, "video_prompt")
    assert task["valid"] and task["candidate_saved"]
    review = Mock(return_value='{"issues":[]}')
    runtime.ResumeWork(repository, task["id"], transport=review)(lambda *_: None)
    assert review.call_count == 1 and review.call_args.args[0].startswith("自动审阅")
    assert repository.current("prompt_bundle")["content"]["shots"][0]["video_motion_prompt"] == BODY


def test_ui_fills_saved_video_prompt_without_changing_images_or_shot_editor(ready, prompt_page):
    repository, first, _ = ready
    page, _ = prompt_page
    page.tabs.setCurrentIndex(3)
    media = page.media
    media.use_image(first["image_id"], "137")
    media.parameter_fields["132.value"].setValue(7.0)
    page.workbench.resolve_edits = lambda: page.save() if page.dirty else True
    page.workbench.model = lambda: "hosted-llm"
    page.workbench.transport = lambda request: (
        '{"issues":[]}' if request_text(request).startswith("自动审阅") else encode(response(repository))
    )
    submitted = []
    page.workbench.submit = lambda work, button: submitted.append(work)
    original = page.editor.positive.toPlainText()
    selected = media.image_selection()
    assert media.generate_prompt_button.isEnabled()
    media.generate_prompt_button.click()
    assert len(submitted) == 1 and isinstance(submitted[0], workflow.WorkspaceWork)
    submitted[0](lambda *_: None)
    page.refresh()
    assert media.prompt.toPlainText() == BODY
    assert media.image_selection() == selected and page.editor.positive.toPlainText() == original
    media.prompt.setPlainText(BODY + "人工微调。")
    page.refresh()
    assert media.prompt.toPlainText().endswith("人工微调。")
    media.parameter_fields["132.value"].setValue(8.0)
    assert "时长已变化" in media.prompt_status.text()
    assert media.prompt.toPlainText().endswith("人工微调。")


def test_switching_shots_isolates_video_prompt_and_selected_images(ready, prompt_page):
    repository, first, _ = ready
    board = repository.current("storyboard")
    content = deepcopy(board["content"])
    content["shots"].append({**deepcopy(content["shots"][0]), "shot_id": "shot_2"})
    repository.save(
        "storyboard",
        content,
        board["dependency_refs"],
        expected_revision=repository.load()["revision"],
    )
    WorkflowService(repository).compile_storyboard_prompts(
        expected_revision=repository.load()["revision"]
    )
    page, _ = prompt_page
    page.tabs.setCurrentIndex(3)
    page.media.use_image(first["image_id"], "137")
    page.media.prompt.setPlainText("镜头一的手动视频提示词")
    page.objects.setCurrentIndex(1)
    assert page.media.target["shot_id"] == "shot_2"
    assert page.media.target["shot_number"] == 2
    assert not any(page.media.image_selection().values())
    assert page.media.prompt.toPlainText() != "镜头一的手动视频提示词"
    page.media.prompt.setPlainText("镜头二的手动视频提示词")
    page.objects.setCurrentIndex(0)
    assert page.media.target["shot_number"] == 1
    assert page.media.prompt.toPlainText() == "镜头一的手动视频提示词"
    assert page.media.image_selection()["137"] == first["image_id"]


def test_saving_the_storyboard_keeps_the_video_area(ready, prompt_page):
    repository, first, _ = ready
    page, _ = prompt_page
    page.tabs.setCurrentIndex(3)
    media = page.media
    media.use_image(first["image_id"], "137")
    media.parameter_fields["132.value"].setValue(7.0)
    media.prompt.setPlainText("<Picture 1> 手动写的视频提示词")
    old_version = media.target["storyboard_version_id"]
    board = repository.current("storyboard")
    content = deepcopy(board["content"])
    content["shots"][0]["action"] = "分镜里改过的动作"
    repository.save("storyboard", content, board["dependency_refs"],
                    expected_revision=repository.load()["revision"])
    page.refresh()
    assert media.target["storyboard_version_id"] != old_version
    assert page.shot_header.action.full == "分镜里改过的动作"
    assert media.prompt.toPlainText() == "<Picture 1> 手动写的视频提示词"
    assert media.image_selection()["137"] == first["image_id"]
    assert media.parameter_values()["132.value"] == 7.0


def test_video_area_comes_back_after_reopening(ready, prompt_page):
    from core.gui.pages.short_video.prompt_page import PromptPage
    from core.short_video.v2.media_workflows import shot_drafts_path

    repository, first, second = ready
    page, _ = prompt_page
    page.tabs.setCurrentIndex(3)
    media = page.media
    media.use_image(first["image_id"], "137")
    media.use_image(second["image_id"], "139")
    media.parameter_fields["132.value"].setValue(7.0)
    media.prompt.setPlainText("<Picture 1> 隔天接着写的视频提示词")
    # Typing is written a moment later; closing the window writes it at once.
    assert media.save_timer.isActive()
    media.remember_draft()
    assert shot_drafts_path(repository.output_dir).is_file()

    reopened = PromptPage(page.workbench)
    try:
        reopened.tabs.setCurrentIndex(3)
        again = reopened.media
        assert again.prompt.toPlainText() == "<Picture 1> 隔天接着写的视频提示词"
        assert again.image_selection()["137"] == first["image_id"]
        assert again.parameter_values()["132.value"] == 7.0
        # <Picture 1> still points at the first picture; once the second picture moves up into
        # its place, the shift is caught instead of failing.
        assert again.prompt_image_bindings == {1: "137"}
        assert again.binding_shifts() == []
        again.image_fields["137"].set_image(None)
        assert again.binding_shifts() == [1]
        assert not again.prompt_shift.isHidden()
    finally:
        reopened.close()
        reopened.deleteLater()


def test_saved_picture_numbers_come_back_as_numbers(tmp_path):
    from core.short_video.v2.media_workflows import (
        load_shot_drafts,
        save_shot_drafts,
        shot_drafts_path,
    )

    save_shot_drafts(tmp_path, {"s2": {"first_last": {"prompt_image_bindings": {1: "137"}}}})
    assert '"1": "137"' in shot_drafts_path(tmp_path).read_text(encoding="utf-8")
    assert load_shot_drafts(tmp_path)["s2"]["first_last"]["prompt_image_bindings"] == {1: "137"}
    # A hand-edited file keeps only usable numbers.
    shot_drafts_path(tmp_path).write_text(
        encode(
            {
                "s2": {
                    "first_last": {
                        "prompt_image_bindings": {"2": "139", "0": "1", "x": "2", "3": 4}
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert load_shot_drafts(tmp_path)["s2"]["first_last"]["prompt_image_bindings"] == {2: "139"}


def test_unreadable_saved_video_area_starts_empty(ready, prompt_page):
    from core.short_video.v2.media_workflows import load_shot_drafts, shot_drafts_path

    repository, _, _ = ready
    path = shot_drafts_path(repository.output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    assert load_shot_drafts(repository.output_dir) == {}
    page, _ = prompt_page
    page.tabs.setCurrentIndex(3)
    assert page.media.prompt.toPlainText()


def add_shots(repository, count, action="相邻镜头动作"):
    board = repository.current("storyboard")
    content = deepcopy(board["content"])
    for index in range(len(content["shots"]), count):
        content["shots"].append({**deepcopy(content["shots"][0]),
                                 "shot_id": f"shot_{index + 1}", "action": action})
    repository.save("storyboard", content, board["dependency_refs"],
                    expected_revision=repository.load()["revision"])
    WorkflowService(repository).compile_storyboard_prompts(
        expected_revision=repository.load()["revision"]
    )


def test_all_request_sizes_ignore_unrelated_shot_collection_and_merge_only_target(ready):
    from core.short_video.v2.prompts import video_request
    from core.short_video.v2.review import video_review_prompt
    from core.short_video.v2.video_context import model_context

    repository, _, _ = ready
    add_shots(repository, 2)
    before = prepare(ready, current_prompt="旧措辞唯一标记")

    def requests(command):
        target = repository.record(command["candidate_id"])
        return [command["prompt"], video_review_prompt(command, target),
                video_request(command["context"], draft="最新正文唯一标记", feedback="修改声音")]

    small = requests(before)
    add_shots(repository, 35, action="无关远处镜头标记" + "无关动作" * 600)
    command = prepare(ready, current_prompt="旧措辞唯一标记")
    large = requests(command)
    assert [len(p) for p in small] == [len(p) for p in large]
    assert all("无关远处镜头标记" not in p for p in large)
    assert all("asset_spec_refs" not in p and "video_input_binding" not in p for p in large)
    assert large[0].count("旧措辞唯一标记") == 1
    assert "旧措辞唯一标记" not in large[1] + large[2]
    assert large[2].count("最新正文唯一标记") == 1
    original = deepcopy(repository.current("prompt_bundle")["content"])
    sent = []

    def send(request):
        prompt = request_text(request)
        sent.append(prompt)
        return '{"issues":[]}' if prompt.startswith("自动审阅") else encode(response(repository))

    result = WorkflowService(repository, transport=send).execute(command)
    saved = repository.current("prompt_bundle")["content"]
    assert result["review_status"] == "passed" and len(sent) == 2
    assert saved["shots"][1:] == original["shots"][1:]
    assert saved["shots"][0]["video_motion_prompt"] == BODY
    frozen = encode(model_context(command["context"]))
    assert frozen in sent[0] and frozen in sent[1]
    call = repository.load()["runs"][-1]["calls"][0]
    assert call["input_chars"] == len(sent[0]) and "本镜上下文" in call["context_sizes"]


def test_video_context_keeps_neighbour_shots_but_reads_no_original_text():
    from core.short_video.v2.video_context import prepare_evidence

    source = "开头😀本镜原文。中间空白。第二个片段。结尾不读取。"
    shot = {"shot_id": "selected", "source_refs": [{"source_id": "s", "start": 2, "end": 8}]}
    board = {"content": {"shots": [{"shot_id": "prev", "action": "前镜"}, shot,
                                    {"shot_id": "next", "action": "后镜"},
                                    {"shot_id": "far", "action": "不读取"}]}}
    context = {"article": {"source_id": "s", "selection_id": "sel", "text": source,
                           "ranges": [{"start": 0, "end": len(source)}]},
               "storyboard": {"content": {"shots": [shot]}}}
    prepare_evidence(context, board, 48000)
    article = context["article"]
    assert "text" not in article and "segments" not in article
    assert [s["shot_id"] for s in article["adjacent_shots"]] == ["prev", "next"]
    assert "本镜原文" not in encode(context)
    assert not context["supplement_manifest"]["omitted"]


def test_neighbour_shots_over_budget_are_omitted_whole():
    from core.short_video.v2.video_context import prepare_evidence

    shot = {"shot_id": "one", "source_refs": []}
    board = {"content": {"shots": [{"shot_id": "prev", "action": "前镜动作" * 50}, shot,
                                    {"shot_id": "next", "action": "后镜动作" * 50}]}}
    context = {"article": {"source_id": "s", "selection_id": "sel", "ranges": [], "text": ""},
               "storyboard": {"content": {"shots": [shot]}}}
    prepare_evidence(context, board, 400)
    assert context["article"]["adjacent_shots"] == []
    assert context["article"]["omitted_items"] == 2
    assert context["supplement_manifest"]["omitted"] == [{"shot_id": "prev"}, {"shot_id": "next"}]


def test_revision_replaces_old_draft_and_preserves_custom_goal(ready):
    repository, _, _ = ready
    repository.save("prompt_template", {"stage": "video_prompt", "instruction": "专属目标标记"},
                    {}, key="prompt_template:video_prompt",
                    expected_revision=repository.load()["revision"])
    command = prepare(ready, current_prompt="人工措辞唯一标记")
    calls = []
    reviews = 0

    def send(request):
        nonlocal reviews
        prompt = request_text(request)
        calls.append(prompt)
        if prompt.startswith("自动审阅"):
            reviews += 1
            return encode({"issues": [] if reviews > 1 else [{
                "rule_id": "SOUND", "severity": "warning", "target_path": "video_motion_prompt",
                "explanation": "风声不明确", "suggestion": "明确风声", "quote": None,
                "source_refs": []}]})
        return encode(response(repository, BODY + ("首稿唯一标记" if len(calls) == 1 else "修订完成")))

    result = WorkflowService(repository, transport=send).execute(command)
    assert result["review_status"] == "passed" and len(calls) == 4
    assert "人工措辞唯一标记" not in "".join(calls[1:])
    assert calls[2].count("首稿唯一标记") == 1
    assert "参考图片在开头集中说明" in calls[2] and "实际图片附在文字之后" in calls[2]
    assert all("专属目标标记" in p for p in calls)
    run = repository.load()["runs"][-1]
    assert len(run["calls"]) == 4 and result["retries_used"] == 1


def test_format_repair_rebuilds_local_request_without_recursive_history(ready):
    repository, _, _ = ready
    command = prepare(ready)
    calls = []

    def send(request):
        prompt = request_text(request)
        calls.append(prompt)
        if prompt.startswith("自动审阅"):
            return '{"issues":[]}'
        if len(calls) == 1:
            return "非JSON回复" + str(len(calls))
        return encode(response(repository))

    result = WorkflowService(repository, transport=send).execute(command)
    assert result["status"] == "succeeded" and len(calls) == 3
    assert calls[1].count("非JSON回复1") == 1
    assert "非JSON回复1" not in calls[2]
    assert all(p.count("【本镜上下文】") == 1 for p in calls)


def test_writing_and_checking_define_picture_uses_upfront_and_keep_action_sections_clear(ready):
    from core.short_video.v2.review import video_review_prompt

    repository, _, _ = ready
    command = prepare(ready)
    writing = command["prompt"]
    assert writing.index("<Picture 1>：") < writing.index("声音：〔") < writing.index("〔起〕–〔止〕秒｜")
    assert "后续分镜不重复引用编号" in writing
    assert "具体描述中间可见的位置、姿态或道具变化" in writing
    checking = video_review_prompt(command, repository.record(command["candidate_id"]))
    assert "每张选中的图片是否在开头有一行明确用途" in checking
    assert "后文是否重复图片引用" in checking


@pytest.mark.parametrize("phase", ["审阅", "修订", "格式修复", "审阅格式修复"])
def test_each_request_checks_frozen_budget_and_preserves_original_on_overflow(ready, phase, caplog):
    import logging
    from core.short_video.v2.review import video_review_prompt
    caplog.set_level(logging.ERROR)
    repository, _, _ = ready
    command = prepare(ready)
    # Permit the ordinary review including its schema, then overflow the selected phase.
    review_prompt = video_review_prompt(command, repository.record(command["candidate_id"]))
    command["max_context_chars"] = max(len(command["prompt"]), len(review_prompt)) + 1000
    original = repository.current("prompt_bundle")["id"]
    calls = []

    def send(request):
        prompt = request_text(request)
        calls.append(prompt)
        if phase == "格式修复":
            return "无效回复" * 10000
        if prompt.startswith("自动审阅"):
            if phase == "审阅格式修复":
                return "不是JSON" * 10000
            return encode({"issues": [{
                "rule_id": "LONG", "severity": "warning", "target_path": "video_motion_prompt",
                "explanation": "反馈" * 10000, "suggestion": "精简", "quote": None,
                "source_refs": []}]})
        return encode(response(repository, BODY + ("正文" * 10000 if phase == "审阅" else "")))

    with pytest.raises(WorkspaceError, match=phase + "上下文超限"):
        WorkflowService(repository, transport=send).execute(command)
    assert len(calls) == (2 if phase in {"修订", "审阅格式修复"} else 1)
    assert repository.current("prompt_bundle")["id"] == original
    assert repository.load()["runs"][-1]["checkpoint"]["step"] in {"request", "review"}


def test_initial_generation_budget_explains_blocks_without_any_request(ready):
    repository, image, _ = ready
    service = WorkflowService(repository, max_context_chars=100)
    with pytest.raises(WorkspaceError, match="生成上下文超限.*字符"):
        service.prepare_video_prompt("shot_1", {"137": image["image_id"]},
                                     workflow_id="minimax-reference", parameters={})
    assert repository.load()["runs"] == []


def test_review_rejects_other_field_and_maps_current_field_to_full_index(ready):
    from core.short_video.v2.review import parse_review

    repository, _, _ = ready
    add_shots(repository, 3)
    target = repository.current("prompt_bundle")
    problem = {"rule_id": "VOICE", "severity": "warning", "target_path": "video_motion_prompt",
               "explanation": "声音不一致", "suggestion": "修正声音", "quote": None,
               "source_refs": []}
    problems = parse_review(encode({"issues": [problem]}), target, repository, shot_id="shot_3")
    assert problems[0]["target_path"] == "shots[2].video_motion_prompt"
    problem["target_path"] = "resolved_prompt"
    with pytest.raises(WorkspaceError, match="只能定位"):
        parse_review(encode({"issues": [problem]}), target, repository, shot_id="shot_3")


@pytest.mark.parametrize("field", ["max_context_chars", "article", "selected_shot_ids"])
def test_incomplete_video_command_is_rejected_before_any_write_or_request(ready, field):
    repository, _, _ = ready
    command = prepare(ready)
    if field == "max_context_chars":
        command.pop(field)
    else:
        command["context"].pop(field)
    before = repository.path.read_bytes()
    send = Mock(side_effect=AssertionError("incomplete requests must not be sent"))
    with pytest.raises(WorkspaceError, match="缺少本镜资料或输入预算"):
        WorkflowService(repository, transport=send).execute(command)
    send.assert_not_called()
    assert repository.path.read_bytes() == before


def test_resume_keeps_frozen_input_budget_instead_of_new_service_default(ready):
    repository, _, _ = ready
    command = prepare(ready)
    # This draft fits the explicitly larger task budget but exceeds the 48k default.
    command["max_context_chars"] = 90000
    body = BODY + "完整长正文" * 10000
    generated = Mock(return_value=encode(response(repository, body)))
    service = WorkflowService(repository, transport=generated,
                              quality_check=Mock(side_effect=ConnectionError("review interrupted")))
    with pytest.raises(ConnectionError):
        service.execute(command)
    reviewed = Mock(return_value='{"issues":[]}')
    result = runtime.ResumeWork(repository, command["request_id"], transport=reviewed)(lambda *_: None)
    assert result["review_status"] == "passed" and generated.call_count == reviewed.call_count == 1
    assert len(reviewed.call_args.args[0]) > 48000
    assert repository.current("prompt_bundle")["content"]["shots"][0]["video_motion_prompt"] == body


def test_generation_for_nonfirst_shot_only_changes_its_body_and_binding(ready):
    repository, image, _ = ready
    add_shots(repository, 3)
    original = deepcopy(repository.current("prompt_bundle")["content"])
    command = WorkflowService(repository).prepare_video_prompt(
        "shot_3", {"137": image["image_id"]}, workflow_id="minimax-reference", parameters={}
    )
    send = Mock(side_effect=[encode({"shot_id": "shot_3", "video_motion_prompt": BODY}),
                             '{"issues":[]}'])
    WorkflowService(repository, transport=send).execute(command)
    saved = repository.current("prompt_bundle")["content"]
    assert saved["shots"][:2] == original["shots"][:2]
    assert saved["shots"][2]["video_motion_prompt"] == BODY
    assert saved["shots"][2]["video_prompt_binding"] == command["context"]["video_input_binding"]
    allowed = {"video_motion_prompt", "video_prompt_binding"}
    assert {k: v for k, v in saved["shots"][2].items() if k not in allowed} == {
        k: v for k, v in original["shots"][2].items() if k not in allowed}


def test_changed_storyboard_invalidates_old_context_and_prevents_any_request(ready):
    repository, _, _ = ready
    command = prepare(ready)
    add_shots(repository, 2)
    send = Mock(side_effect=AssertionError("changed inputs must not call models"))
    with pytest.raises(WorkspaceError):
        WorkflowService(repository, transport=send).execute(command)
    send.assert_not_called()


def test_incomplete_interrupted_video_task_is_rejected_without_model_request(ready):
    from core.short_video.v2.models import fingerprint

    repository, _, _ = ready
    command = prepare(ready)
    original = repository.current("prompt_bundle")["id"]
    service = WorkflowService(repository, transport=Mock(return_value=encode(response(repository))),
                              quality_check=Mock(side_effect=ConnectionError("interrupted")))
    with pytest.raises(ConnectionError):
        service.execute(command)
    # A consistent fingerprint cannot substitute for required task data.
    with repository.transaction() as state:
        task = state["tasks"][command["request_id"]]
        task["command"]["context"].pop("supplement_manifest")
        task["command_hash"] = fingerprint(task["command"])
        run = state["runs"][-1]
        run["checkpoint"]["command"] = deepcopy(task["command"])
        run["checkpoint_hash"] = fingerprint(run["checkpoint"])
    recovery = runtime.get_recovery(repository, "video_prompt")
    assert recovery and not recovery["valid"] and "任务输入不完整" in recovery["reason"]
    before = repository.path.read_bytes()
    reviewed = Mock(side_effect=AssertionError("incomplete tasks must not resume"))
    with pytest.raises(WorkspaceError, match="任务输入不完整"):
        runtime.ResumeWork(repository, command["request_id"], transport=reviewed)(lambda *_: None)
    reviewed.assert_not_called()
    assert repository.path.read_bytes() == before
    assert repository.current("prompt_bundle")["id"] == original


def test_video_stage_has_only_local_output_and_never_copies_full_candidate(ready):
    from core.short_video.v2.prompts import output_example, stage_prompt

    repository, _, _ = ready
    command = prepare(ready)
    candidate = deepcopy(repository.current("prompt_bundle"))
    candidate["content"]["unused_metadata"] = "不应发送的完整集合数据"
    prompt = stage_prompt("video_prompt", command["context"], candidate=candidate)
    assert "完整当前稿" not in prompt and "不应发送的完整集合数据" not in prompt
    assert set(output_example("video_prompt", command["context"])) == {
        "shot_id", "video_motion_prompt"}
    with pytest.raises(WorkspaceError, match="不返回完整镜头集合"):
        WorkflowService(repository)._validate_output(command, candidate["content"])


def test_video_request_and_review_use_no_original_text(ready):
    from core.short_video.v2.review import check_candidate
    from core.short_video.v2.video_context import model_context

    repository, _, _ = ready
    command = prepare(ready)
    source = repository.current("source")
    sent = model_context(command["context"])
    assert "segments" not in command["context"]["article"]
    assert set(sent) == {"shot", "assets", "video_inputs", "adjacent_shots", "requirements"}
    assert "source_refs" not in sent["shot"]
    quote = source["content"]["normalized_text"][:4]
    issue = {"rule_id": "FACT", "severity": "warning", "target_path": "video_motion_prompt",
             "explanation": "声称看过原文", "suggestion": "修正", "quote": quote,
             "source_refs": [{"source_id": source["id"], "start": 0, "end": 4, "quote": quote}]}
    with pytest.raises(WorkspaceError, match="未提供的原文") as caught:
        check_candidate(command, repository.current("prompt_bundle"),
                        Mock(return_value=encode({"issues": [issue]})), repository)
    assert caught.value.details["category"] == "reference"
    assert caught.value.details["errors"][0]["path"] == "issues[0].source_refs[0]"


def test_video_review_contract_is_metered_and_schema_failure_precedes_internal_mapping(ready, monkeypatch):
    from core.short_video.v2 import review, video_context
    from core.short_video.v2.review_contract import output_contract

    repository, _, _ = ready
    command = prepare(ready)
    candidate = repository.record(command["candidate_id"])
    prompt = review.video_review_prompt(command, candidate)
    assert video_context.request_sizes(prompt)["输出结构约束"] == len(encode(output_contract("video_prompt")))
    command["max_context_chars"] = len(prompt) - 1
    with pytest.raises(WorkspaceError, match="审阅上下文超限"):
        review.check_candidate(command, candidate, Mock(side_effect=AssertionError("over budget")), repository)
    monkeypatch.setattr(review, "identity", lambda _: pytest.fail("invalid report received internal id"))
    with pytest.raises(WorkspaceError) as caught:
        review.parse_review(encode({"issues": [{"suggestion": None}]}), candidate, repository, shot_id="shot_1")
    assert caught.value.details["category"] == "schema"
