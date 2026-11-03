from copy import deepcopy

import pytest

from core.generation.cancellation import CancelToken, GenerationCancelled
from core.short_video.v2.conversation import ConversationService
from core.short_video.v2.models import WorkspaceError, identity
from test_video_v2_workflow import project, generate


def test_history_context_and_reopen_without_business_changes(project):
    prompts = []
    service = ConversationService(project, transport=lambda p: prompts.append(p) or "井边的钥匙来自父亲。")
    conversation = service.store.create()
    before = deepcopy(project.load()["current"])
    command = service.prepare(conversation["id"], "钥匙在哪里？")
    service.execute(command)
    service.execute(command)  # durable idempotency, no second request
    service.execute(service.prepare(conversation["id"], "谁留下的？"))
    assert len(prompts) == 2 and "钥匙在哪里" in prompts[1] and "钥匙在哪里" in prompts[0]
    assert project.load()["current"] == before
    reopened = ConversationService(project).store.get(conversation["id"])
    assert len(reopened["messages"]) == 4
    assert all(m["status"] == "complete" for m in reopened["messages"])


def test_editing_request_remains_a_discussion_without_creating_content_versions(project):
    record = generate(project, "novel_copy")
    before = deepcopy(project.load()["records"])
    service = ConversationService(project, transport=lambda _: "建议压缩开头。\n改写示例：井底响了三声。")
    conversation = service.store.create()
    service.execute(service.prepare(conversation["id"], "修改开头"))
    assert project.load()["records"] == before
    assert project.current("novel_copy")["id"] == record["id"]
    reopened = ConversationService(project).store.get(conversation["id"])
    assert reopened["messages"][-1]["content"] == "建议压缩开头。\n改写示例：井底响了三声。"


def test_cancel_and_failure_preserve_message_and_never_retry(project):
    token = CancelToken()
    def send(_):
        token.cancel()
        return "已返回的建议"
    service = ConversationService(project, transport=send)
    conversation = service.store.create()
    with pytest.raises(GenerationCancelled):
        service.execute(service.prepare(conversation["id"], "问题"), token=token)
    assert service.store.get(conversation["id"])["messages"][-1]["content"] == "已返回的建议"
    assert service.store.get(conversation["id"])["messages"][-1]["status"] == "cancelled"
    calls = []
    def fail(prompt):
        calls.append(prompt)
        raise ConnectionError("secret")
    service.transport = fail
    with pytest.raises(WorkspaceError):
        service.execute(service.prepare(conversation["id"], "再问"))
    assert len(calls) == 1 and service.store.get(conversation["id"])["messages"][-1]["status"] == "failed"


def test_chat_overflow_and_project_ownership(project, tmp_path):
    service = ConversationService(project, max_context_chars=10)
    conversation = service.store.create()
    with pytest.raises(WorkspaceError) as exc:
        service.prepare(conversation["id"], "超长问题")
    assert exc.value.code == "CONTEXT_TOO_LARGE" and not project.load()["runs"]
    service.max_context_chars = 48000
    value = service.store.get(conversation["id"])
    value["project_id"] = "other"
    from core.short_video.v2.storage import atomic_write_text
    from core.short_video.v2.models import encode
    atomic_write_text(service.store._path(conversation["id"]), encode(value))
    with pytest.raises(WorkspaceError):
        service.store.get(conversation["id"])


def test_chat_never_reads_project_documents_or_old_reference_snapshots(project, monkeypatch):
    service = ConversationService(project, transport=lambda _: "请提供需要讨论的文字。")
    conversation = service.store.create()
    service.execute(service.prepare(conversation["id"], "你好"))
    saved = service.store.get(conversation["id"])
    saved["messages"][0]["context_snapshot"] = {"artifact_refs": ["deleted_document"]}
    from core.short_video.v2.storage import atomic_write_text
    from core.short_video.v2.models import encode
    atomic_write_text(service.store._path(conversation["id"]), encode(saved))

    def forbidden(*args, **kwargs):
        pytest.fail("Chat must not read project documents")

    from core.short_video.v2.project_service import ProjectService
    monkeypatch.setattr(ProjectService, "context", forbidden)
    command = service.prepare(conversation["id"], "继续")
    import json
    payload = json.loads(command["prompt"].split("\n", 1)[1])
    assert set(payload) == {"conversation"}
    assert payload["conversation"][0]["content"] == "你好"
    assert "deleted_document" not in command["prompt"]
    assert set(command["context_snapshot"]) == {"history_message_ids", "context_hash"}
    service.execute(command)


def test_real_chat_entry_trims_complete_turns_and_saves_current_once(project):
    import json
    from core.short_video.v2.conversation_context import build_conversation_context

    requests = []
    service = ConversationService(project, transport=lambda prompt: requests.append(prompt) or "最终回复")
    conversation = service.store.create()
    for question, answer in [("较早的问题" * 1000, "较早的回复" * 1000), ("最近的问题", "最近的回复")]:
        user = service.store.save_user_message(
            conversation["id"], question, turn_id=identity("run"),
            model_snapshot={"provider": "hosted-llm", "model": "workspace-test-model"},
        )
        service.store.save_final_reply(conversation["id"], user["id"], answer)
    before = service.store.get(conversation["id"])["messages"]
    current = {"id": identity("message"), "turn_id": identity("turn"), "role": "user", "content": "本轮问题", "status": "pending"}
    service.max_context_chars = build_conversation_context(before[-2:] + [current], current["id"]).character_count
    command = service.prepare(conversation["id"], "本轮问题")
    assert service.store.get(conversation["id"])["messages"] == before
    assert command["context_snapshot"]["history_message_ids"] == [message["id"] for message in before[-2:]]
    service.execute(command)
    service.execute(command)
    assert len(requests) == 1
    payload = json.loads(requests[0].split("\n", 1)[1])
    assert [message["content"] for message in payload["conversation"]] == ["最近的问题", "最近的回复", "本轮问题"]
    saved = service.store.get(conversation["id"])["messages"]
    assert saved[:4] == before
    assert [message["content"] for message in saved[-2:]] == ["本轮问题", "最终回复"]
    assert all("context_snapshot" not in message for message in saved)


def test_stale_chat_command_does_not_overwrite_another_pending_user(project):
    service = ConversationService(project, transport=lambda _: pytest.fail("must fail before model request"))
    conversation = service.store.create()
    command = service.prepare(conversation["id"], "旧问题")
    other_user = service.store.save_user_message(
        conversation["id"], "另一个窗口的问题", turn_id=identity("run"),
        model_snapshot={"provider": "hosted-llm", "model": "workspace-test-model"},
    )
    with pytest.raises(WorkspaceError) as error:
        service.execute(command)
    assert error.value.code == "REVISION_CONFLICT"
    assert service.store.get(conversation["id"])["messages"] == [other_user]


def test_chat_probe_uses_current_store_and_message_contract(tmp_path, monkeypatch, capsys):
    import json
    import sys
    from core.evaluation import video_v2_probe

    requests = []

    def create_chat_service(repository, model):
        return ConversationService(repository, model, transport=lambda prompt: requests.append(prompt) or "使用短句推动故事。")

    monkeypatch.setattr(video_v2_probe, "ConversationService", create_chat_service)
    monkeypatch.setattr(sys, "argv", ["video_v2_probe", "--root", str(tmp_path), "--case", "rural", "--stage", "chat"])
    video_v2_probe.main()
    saved = json.loads(capsys.readouterr().out)
    assert [message["role"] for message in saved["messages"]] == ["user", "assistant"]
    assert saved["messages"][-1]["content"] == "使用短句推动故事。"
    assert all("context_snapshot" not in message for message in saved["messages"])
    assert len(requests) == 1
    payload = json.loads(requests[0].split("\n", 1)[1])
    assert set(payload) == {"conversation"}
    assert "井沿却放着一把干净的铜钥匙" not in requests[0]
