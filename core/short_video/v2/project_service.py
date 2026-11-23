"""Article/configuration use cases; no model request is made by these operations."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

from .contracts import JsonObject, OperationParameters, OperationResult, SourceRange
from .models import WorkspaceError, default_config, fingerprint, require, text
from .repository import ProjectRepository
from .source import (
    read_source_file,
    selection_content,
    source_content,
    source_references,
)


class ProjectService:
    def __init__(self, repository: ProjectRepository) -> None:
        self.repository = repository

    def title(self) -> str:
        state = self.repository.load()
        if title := state.get("project_title"):
            return title
        config = self.repository.current("config", state=state)
        return config["content"]["title"] if config else "新项目"

    def set_title(self, title: str, *, expected_revision: int | None = None) -> str:
        """Save the display title independently of the inputs used by generation."""
        title = text(title, "项目名称").strip()
        with self.repository.transaction(expected_revision) as state:
            state["project_title"] = title
        return title

    def save_inputs(
        self,
        config: JsonObject,
        source: JsonObject,
        ranges: list[SourceRange] | None = None,
        *,
        expected_revision: int,
    ) -> JsonObject:
        """Commit configuration, normalized source and ranges as one business change."""
        with self.repository.transaction(expected_revision) as state:
            config_record = self.repository.current("config", state=state)
            # Validate even when a caller supplies a content-equivalent dictionary.
            from .models import validate_config

            config = validate_config(config)
            if config_record is None or config_record["content_hash"] != fingerprint(config):
                config_record = self.repository._save(state, "config", config, {})
            original = self.repository.current("source", state=state)
            if original is None or original["content_hash"] != fingerprint(source):
                original = self.repository._save(state, "source", source, {}, origin="import")
            if ranges is None:
                ranges = [{"start": 0, "end": len(original["content"]["normalized_text"])}]
            selection = selection_content(original, ranges)
            selected = self.repository.current("selection", state=state)
            if selected is None or selected["content_hash"] != fingerprint(selection):
                selected = self.repository._save(
                    state, "selection", selection, {"source": original["id"]}
                )
        return {
            "config": config_record,
            "source": original,
            "selection": selected,
            "project_revision": self.repository.load()["revision"],
        }

    def import_text(
        self,
        value: Any,
        *,
        config: JsonObject | None = None,
        ranges: list[SourceRange] | None = None,
        expected_revision: int,
        title: str = "原文",
    ) -> JsonObject:
        existing = self.repository.current("config")
        return self.save_inputs(
            config
            if config is not None
            else (existing["content"] if existing else default_config()),
            source_content(value, title=title),
            ranges,
            expected_revision=expected_revision,
        )

    def import_file(
        self,
        filename: str | Path,
        *,
        encoding: str = "utf-8-sig",
        config: JsonObject | None = None,
        ranges: list[SourceRange] | None = None,
        expected_revision: int,
    ) -> JsonObject:
        existing = self.repository.current("config")
        return self.save_inputs(
            config
            if config is not None
            else (existing["content"] if existing else default_config()),
            read_source_file(filename, encoding=encoding),
            ranges,
            expected_revision=expected_revision,
        )

    def context(
        self, *, state: JsonObject | None = None, selection_id: str | None = None
    ) -> JsonObject:
        state = self.repository.load() if state is None else state
        selection = (
            self.repository.record(selection_id, state=state)
            if selection_id
            else self.repository.current("selection", state=state)
        )
        require(
            selection is not None and selection["kind"] == "selection",
            "INVALID_INPUT",
            "请先保存文章与选区",
        )
        source = self.repository.record(selection["content"]["source_id"], state=state)
        refs = source_references(source, selection)
        return {
            "selection_id": selection["id"],
            "source_id": source["id"],
            "ranges": deepcopy(selection["content"]["ranges"]),
            "source_refs": refs,
            # Selected text appears once; requests drop the per-ref quotes and keep coordinates.
            "text": "\n".join(ref["quote"] for ref in refs),
        }


def invoke(
    operation: Callable[OperationParameters, OperationResult],
    *args: OperationParameters.args,
    **kwargs: OperationParameters.kwargs,
) -> JsonObject:
    """Result<T> boundary for GUI/CLI clients; domain methods retain typed exceptions."""
    try:
        return {"data": operation(*args, **kwargs)}
    except WorkspaceError as exc:
        return exc.result()
    except OSError:
        return WorkspaceError(
            "STORAGE_ERROR", "文件无法可靠保存或读取，请检查目录与磁盘状态"
        ).result()
