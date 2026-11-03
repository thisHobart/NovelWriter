"""Current-format boundaries and programming-error visibility, without live requests."""

from copy import deepcopy
from pathlib import Path

import pytest
from test_video_v2_workflow import ARTICLE, generate, prepare_story, sample

from core.generation import api_providers
from core.short_video.v2.batches import ChunkService, create_batch_service
from core.short_video.v2.checks import CheckService
from core.short_video.v2.conversation import ConversationService
from core.short_video.v2.llm import TextProviderError
from core.short_video.v2.models import (
    WorkspaceError,
    encode,
    fingerprint,
    parse_object,
    validate_model_snapshot_format,
)
from core.short_video.v2.project_service import ProjectService
from core.short_video.v2.repository import ProjectRepository
from core.short_video.v2.source import source_content
from core.short_video.v2.storage import atomic_write_text
from core.short_video.v2.workflow import WorkflowService


@pytest.fixture
def article_project(tmp_path: Path) -> ProjectRepository:
    repository = ProjectRepository(tmp_path)
    ProjectService(repository).import_text(ARTICLE, expected_revision=0)
    return repository


@pytest.mark.parametrize(
    "snapshot",
    [None, "hosted-llm", {}, {"provider": "hosted-llm"}, {"provider": "hosted-llm", "model": None}],
)
def test_old_or_missing_model_snapshot_is_rejected(snapshot: object) -> None:
    with pytest.raises(WorkspaceError) as error:
        validate_model_snapshot_format(snapshot)
    assert error.value.code == "INVALID_MODEL_SNAPSHOT"


def test_missing_configuration_fails_before_task_admission(
    article_project: ProjectRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration_error = ValueError("缺少 HOSTED_LLM_MODEL")

    def missing_configuration(provider: str) -> None:
        raise configuration_error

    monkeypatch.setattr(api_providers, "get_config", missing_configuration)
    with pytest.raises(WorkspaceError) as error:
        WorkflowService(article_project).prepare("novel_copy")
    assert error.value.code == "MODEL_CONFIGURATION_MISSING"
    assert error.value.__cause__ is configuration_error
    assert not article_project.load()["runs"]


def test_empty_explicit_config_does_not_fall_back_to_saved_config(
    article_project: ProjectRepository,
) -> None:
    original = article_project.path.read_bytes()
    with pytest.raises(WorkspaceError, match="必需字段"):
        ProjectService(article_project).import_text(
            "不应保存的新文章", config={}, expected_revision=article_project.load()["revision"]
        )
    assert article_project.path.read_bytes() == original


@pytest.mark.parametrize(
    "programming_error",
    [
        KeyError("incorrect_field"),
        TypeError("incorrect_argument"),
        RuntimeError("broken_invariant"),
    ],
)
def test_programming_error_from_transport_is_not_relabelled_or_retried(
    article_project: ProjectRepository, programming_error: Exception
) -> None:
    requests = []

    def broken_transport(prompt: str) -> str:
        requests.append(prompt)
        raise programming_error

    service = WorkflowService(article_project, transport=broken_transport)
    with pytest.raises(type(programming_error)) as error:
        service.execute(service.prepare("novel_copy"))
    assert error.value is programming_error and len(requests) == 1
    assert article_project.load()["runs"][-1]["status"] == "failed"
    assert article_project.current("novel_copy") is None


def test_internal_validation_bug_does_not_trigger_format_repair(
    article_project: ProjectRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = []

    def send(prompt: str) -> str:
        requests.append(prompt)
        return encode(sample(article_project, "novel_copy"))

    service = WorkflowService(article_project, transport=send)

    def broken_validator(*args: object) -> None:
        raise KeyError("internal_validator_field")

    monkeypatch.setattr(service, "_validate_output", broken_validator)
    with pytest.raises(KeyError, match="internal_validator_field"):
        service.execute(service.prepare("novel_copy"))
    assert len(requests) == 1


def test_chat_programming_error_keeps_failed_message_and_original_error(
    article_project: ProjectRepository,
) -> None:
    programming_error = AttributeError("incorrect_client_attribute")

    def broken_transport(prompt: str) -> str:
        raise programming_error

    service = ConversationService(article_project, transport=broken_transport)
    conversation = service.store.create()
    with pytest.raises(AttributeError) as error:
        service.execute(service.prepare(conversation["id"], "核对这段文章"))
    assert error.value is programming_error
    assert service.store.get(conversation["id"])["messages"][-1]["status"] == "failed"


def test_ai_check_programming_error_is_not_a_fake_unavailable_report(
    article_project: ProjectRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    import core.short_video.v2.checks as checks

    draft = generate(article_project, "novel_copy")
    service = CheckService(article_project, transport=lambda _: encode({"issues": []}))

    def broken_validator(*args: object, **kwargs: object) -> None:
        raise TypeError("incorrect_check_argument")

    previous_reports = article_project.latest_reports(draft["id"])
    monkeypatch.setattr(checks, "validate_content", broken_validator)
    with pytest.raises(TypeError, match="incorrect_check_argument"):
        service.execute(service.prepare(draft["id"]))
    assert article_project.latest_reports(draft["id"]) == previous_reports
    assert article_project.load()["runs"][-1]["status"] == "failed"


def test_provider_error_keeps_original_connection_failure(
    article_project: ProjectRepository,
) -> None:
    network_error = ConnectionError("connection interrupted")

    def disconnected_transport(prompt: str) -> str:
        raise network_error

    service = WorkflowService(article_project, transport=disconnected_transport)
    with pytest.raises(TextProviderError) as error:
        service.execute(service.prepare("novel_copy"))
    assert error.value.__cause__ is network_error
    assert len(article_project.load()["runs"][-1]["calls"]) == 1


@pytest.mark.parametrize("raw", ["null", "[]", "{broken", '{"score": NaN}', '{"score": Infinity}'])
def test_malformed_external_json_has_an_explicit_validation_error(raw: str) -> None:
    with pytest.raises(WorkspaceError) as error:
        parse_object(raw)
    assert error.value.code == "INVALID_OUTPUT"


@pytest.mark.parametrize("invalid_field", ["role", "dialogue"])
def test_malformed_model_field_can_be_repaired_without_hiding_internal_errors(
    article_project: ProjectRepository, invalid_field: str
) -> None:
    generate(article_project, "novel_copy")
    invalid = sample(article_project, "video_copy")
    invalid["blocks"][0][invalid_field] = [] if invalid_field == "role" else [1]
    responses = iter([invalid, sample(article_project, "video_copy"), {"issues": []}])
    requests = []

    def send(prompt: str) -> str:
        requests.append(prompt)
        return encode(next(responses))

    service = WorkflowService(article_project, transport=send)
    assert service.execute(service.prepare("video_copy"))["status"] == "succeeded"
    assert len(requests) == 3


def test_missing_command_snapshot_is_an_internal_contract_error(
    article_project: ProjectRepository,
) -> None:
    service = WorkflowService(article_project)
    command = service.prepare("novel_copy")
    del command["model_snapshot"]
    with pytest.raises(KeyError, match="model_snapshot"):
        service.execute(command)
    assert not article_project.load()["runs"]


def test_old_source_origin_is_not_accepted() -> None:
    with pytest.raises(WorkspaceError, match="来源类型"):
        source_content("井边的钥匙", origin="legacy_script")


def test_unknown_batch_mode_does_not_fall_back_to_asset_generation(article_project: ProjectRepository) -> None:
    with pytest.raises(WorkspaceError, match="未知批量计划类型"):
        create_batch_service(article_project, "legacy")
    assert not article_project.load()["runs"]


def test_old_chat_snapshot_is_rejected_without_rewriting_history(
    article_project: ProjectRepository,
) -> None:
    service = ConversationService(article_project, transport=lambda _: "保留钥匙这个道具。")
    conversation = service.store.create()
    service.execute(service.prepare(conversation["id"], "保留哪个道具？"))
    value = service.store.get(conversation["id"])
    value["messages"][0]["model_snapshot"] = "hosted-llm"
    path = service.store._path(conversation["id"])
    atomic_write_text(path, encode(value))
    original = path.read_bytes()
    with pytest.raises(WorkspaceError) as error:
        service.store.get(conversation["id"])
    assert error.value.code == "INVALID_MODEL_SNAPSHOT" and path.read_bytes() == original


def test_plan_without_model_snapshot_is_rejected_without_migration(
    article_project: ProjectRepository,
) -> None:
    service = ChunkService(article_project)
    plan = service.create_plan()
    del plan["model_snapshot"]
    path = service.path(plan["id"])
    atomic_write_text(path, encode(plan))
    original = path.read_bytes()
    with pytest.raises(WorkspaceError, match="必需字段"):
        service.load_plan(plan["id"])
    assert path.read_bytes() == original


def test_stored_view_without_input_hash_is_rejected_without_backfill(
    article_project: ProjectRepository,
) -> None:
    prepare_story(article_project)
    entity = article_project.current("asset_catalog")["content"]["entities"][0]
    draft = generate(article_project, "asset_spec", entity=entity, kind="character_spec")
    compiled = WorkflowService(article_project).compile_asset_prompts(
        draft["id"], expected_revision=article_project.load()["revision"]
    )
    state = article_project.load()
    record = deepcopy(compiled)
    del record["content"]["views"][0]["spec_input_hash"]
    record["content_hash"] = fingerprint(record["content"])
    state["records"][record["id"]]["hash"] = fingerprint(record)
    path = article_project.root / "artifacts" / f"{record['id']}.json"
    atomic_write_text(path, encode(record))
    atomic_write_text(article_project.path, encode(state))
    original = path.read_bytes()
    with pytest.raises(WorkspaceError, match="提示词指纹"):
        article_project.load()
    assert path.read_bytes() == original


@pytest.mark.parametrize("change", ["current_reference", "run_id", "record_index"])
def test_invalid_persisted_fields_are_reported_without_masking_programming_errors(
    article_project: ProjectRepository, change: str
) -> None:
    state = article_project.load()
    if change == "current_reference":
        state["current"]["source"] = []
    elif change == "run_id":
        state["runs"] = [{"id": [], "status": "running"}]
    else:
        state["records"][state["current"]["source"]] = []
    atomic_write_text(article_project.path, encode(state))
    original = article_project.path.read_bytes()
    with pytest.raises(WorkspaceError):
        article_project.load()
    assert article_project.path.read_bytes() == original


def test_failed_status_write_does_not_replace_original_programming_error(
    article_project: ProjectRepository,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = WorkflowService(
        article_project, transport=lambda _: encode(sample(article_project, "novel_copy"))
    )
    original_update = article_project.update_run

    def update_once(run_id: str, operation: object) -> object:
        if article_project.load()["runs"][-1]["calls"]:
            raise PermissionError("run record is locked")
        return original_update(run_id, operation)

    def broken_validator(*args: object) -> None:
        raise KeyError("original_programming_error")

    monkeypatch.setattr(article_project, "update_run", update_once)
    monkeypatch.setattr(service, "_validate_output", broken_validator)
    with pytest.raises(KeyError, match="original_programming_error") as error:
        service.execute(service.prepare("novel_copy"))
    assert any("PermissionError" in note for note in error.value.__notes__)
    assert "Failed to save run failure" in caplog.text
