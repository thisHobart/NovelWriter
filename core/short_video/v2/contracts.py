"""Types shared at the workspace's UI, model and storage boundaries."""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ParamSpec, Protocol, TypeAlias, TypedDict, TypeVar

if TYPE_CHECKING:
    from .feedback import TaskContext

from core.generation.cancellation import CancelToken

# Content differs by stage; its concrete fields are checked at the input boundary.
JsonObject: TypeAlias = dict[str, Any]
ProgressReporter: TypeAlias = Callable[[str, float], None]
TextTransport: TypeAlias = Callable[[str], str]
RequestRecorder: TypeAlias = Callable[[str, str, str | None], None]
OperationResult = TypeVar("OperationResult")
OperationParameters = ParamSpec("OperationParameters")


class VideoTask(Protocol):
    token: CancelToken
    output_dir: str
    task_context: "TaskContext"

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
    origin: str
    created_at: str
    metadata: JsonObject
