"""Types shared at the workspace's UI, model and storage boundaries."""

from collections.abc import Callable
from typing import Any, ParamSpec, Protocol, TypeAlias, TypedDict, TypeVar

from core.generation.cancellation import CancelToken

# Content differs by stage; its concrete fields are checked at the input boundary.
JsonObject: TypeAlias = dict[str, Any]
ProgressReporter: TypeAlias = Callable[[str, float], None]
TextTransport: TypeAlias = Callable[[str], str]
RequestRecorder: TypeAlias = Callable[[str, str, str | None], None]
OperationResult = TypeVar("OperationResult")
OperationParameters = ParamSpec("OperationParameters")

READ_ONLY_CONTENT_FIELDS = {
    "id",
    "asset_id",
    "variant_id",
    "shot_id",
    "view_id",
    "source_id",
    "spec_version_id",
    "storyboard_version_id",
    "order",
    "start_ms",
    "end_ms",
    "content_hash",
    "spec_input_hash",
    "template_ref",
    "prompts_generated",
}


class VideoTask(Protocol):
    token: CancelToken
    output_dir: str

    def __call__(self, report: ProgressReporter) -> JsonObject: ...


class ModelSnapshot(TypedDict):
    provider: str
    model: str


class SourceRange(TypedDict):
    start: int
    end: int


class ArtifactRecord(TypedDict):
    id: str
    kind: str
    key: str
    content: JsonObject
    content_hash: str
    dependency_refs: dict[str, str]
    parent_id: str | None
    origin: str
    created_at: str
    metadata: JsonObject
