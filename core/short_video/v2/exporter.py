"""Snapshot-based Markdown deliverables, with no media prerequisites."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from .contracts import JsonObject
from .models import (
    ASSET_KINDS,
    VIEWS,
    encode,
    identity,
    require,
    timestamp,
)
from .repository import ProjectRepository
from .storyboard_text import render_storyboard_text
from .validation import local_issues, validate_content

class ExportService:
    def __init__(self, repository: ProjectRepository) -> None:
        self.repository = repository

    def snapshot(
        self,
        *,
        keys: list[str] | None = None,
        allow_stale: bool = False,
        allow_partial: bool = False,
    ) -> JsonObject:
        state = self.repository.load()
        requested = (
            keys
            if keys is not None
            else [
                key
                for key in state["current"]
                if key in {"novel_copy", "video_copy", "storyboard", "prompt_bundle"}
                or key.split(":")[0] in ASSET_KINDS.values()
            ]
        )
        selected = []
        missing = []
        for key in requested:
            if key == "prompts":
                prompt_keys = [
                    k
                    for k in state["current"]
                    if k == "prompt_bundle" or k.split(":")[0] in ASSET_KINDS.values()
                ]
                selected.extend(prompt_keys)
                if not prompt_keys:
                    missing.append("提示词")
                board = self.repository.current("storyboard", state=state)
                catalog = (
                    self.repository.record(board["dependency_refs"]["asset_catalog"], state=state)
                    if board
                    else self.repository.current("asset_catalog", state=state)
                )
                for entity in (catalog or {}).get("content", {}).get("entities", []):
                    if entity.get("usage") == "voice_only":
                        continue
                    for variant in entity["variants"]:
                        asset_key = f"{ASSET_KINDS[entity['kind']]}:{entity['asset_id']}:{variant['variant_id']}"
                        if asset_key not in state["current"]:
                            missing.append(entity["name"] + "／" + variant["label"] + "提示词")
                if board and "prompt_bundle" not in state["current"]:
                    missing.append("镜头提示词")
            elif key in state["current"]:
                selected.append(key)
            else:
                missing.append(key)
        selected = list(dict.fromkeys(selected))
        require(
            not missing or allow_partial, "EXPORT_INCOMPLETE", "缺少内容：" + "、".join(missing)
        )
        require(bool(selected), "EXPORT_INCOMPLETE", "没有可导出的内容")
        records, provenance = {}, {}

        def include(record_id: str) -> None:
            if record_id in provenance:
                return
            record = self.repository.record(record_id, state=state)
            provenance[record_id] = record
            records.setdefault(record["key"], record)
            for ref in record["dependency_refs"].values():
                include(ref)

        for key in selected:
            record = self.repository.record(state["current"][key], state=state)
            validate_content(
                record["kind"],
                record["content"],
                dependencies=record["dependency_refs"],
                resolve=lambda ref: self.repository.record(ref, state=state),
            )
            if record["kind"] in ASSET_KINDS.values():
                entity_kind = next(k for k, v in ASSET_KINDS.items() if v == record["kind"])
                required_views = {view_id for view_id, _ in VIEWS[entity_kind]}
                require(
                    required_views <= {v["view_id"] for v in record["content"]["views"]},
                    "EXPORT_INVALID",
                    f"{key}：缺少必要视图提示词",
                )
            errors = [
                i
                for i in local_issues(record["kind"], record["content"])
                if i["severity"] == "error"
            ]
            require(
                not errors,
                "EXPORT_INVALID",
                f"{key}：" + "；".join(f"{i['target_path']}：{i['explanation']}" for i in errors),
            )
            records[key] = record
        stale = [
            key
            for key, record in records.items()
            if not self.repository.is_current(record["id"], state=state)
        ]
        require(allow_stale or not stale, "STALE_INPUT", "所选内容基于旧来源：" + "、".join(stale))
        for record in list(records.values()):
            include(record["id"])
        return {
            "schema_version": 2,
            "id": identity("export"),
            "project_id": state["project_id"],
            "revision": state["revision"],
            "mode": "selected_content",
            "created_at": timestamp(),
            "selected_keys": selected,
            "missing": missing,
            "problems": [f"{key} 基于旧来源" for key in stale],
            "records": records,
            "provenance": provenance,
        }

    def export(
        self,
        destination: str | Path,
        *,
        keys: list[str] | None = None,
        allow_stale: bool = False,
        allow_partial: bool = False,
        overwrite: bool = False,
    ) -> JsonObject:
        path = Path(destination).resolve()
        sidecar = path.with_name(path.name + ".manifest.json")
        require(
            path.suffix.lower() == ".md",
            "INVALID_INPUT",
            "仅支持 Markdown（.md）导出",
        )
        require(
            not path.is_relative_to(self.repository.root)
            or path.is_relative_to(self.repository.root / "exports"),
            "INVALID_INPUT",
            "项目内部请导出到 exports 目录，避免覆盖项目资料",
        )
        require(
            overwrite or not path.exists() and not sidecar.exists(),
            "EXPORT_EXISTS",
            "导出文件已存在，请明确选择覆盖或另存",
        )
        snapshot = self.snapshot(keys=keys, allow_stale=allow_stale, allow_partial=allow_partial)
        lines = ["# 视频推广文字成果", ""]
        if snapshot["missing"]:
            lines += ["本次未包含：" + "、".join(snapshot["missing"]), ""]
        for key in snapshot["selected_keys"]:
            record = snapshot["records"][key]
            content = record["content"]
            if key == "novel_copy":
                lines += [
                    "## 小说推文",
                    "",
                    content["opening"]["text"],
                    "",
                    *[p["text"] + "\n" for p in content["paragraphs"]],
                    content["cliffhanger"]["text"],
                    "",
                ]
            elif key == "video_copy":
                lines += ["## 视频推广文案", ""]
                for block in content["blocks"]:
                    lines += [
                        block["narration"],
                        "动作：" + block["visible_action"],
                        *[
                            line["speaker_label"] + "：" + line["text"]
                            for line in block["dialogue"]
                        ],
                        "",
                    ]
            elif key == "storyboard":
                lines += ["## 分镜", ""]
                lines += [
                    render_storyboard_text(
                        content,
                        snapshot["provenance"][record["dependency_refs"]["asset_catalog"]]["content"],
                    ),
                    "",
                ]
            else:
                lines += [
                    "## " + ("镜头提示词" if key == "prompt_bundle" else "资产提示词"),
                    "",
                ]
                for index, item in enumerate(content.get("shots", content.get("views", []))):
                    lines += [
                        "### " + item.get("label", f"镜头 {index + 1}"),
                        "正向：" + item.get("resolved_prompt", item.get("positive_prompt", "")),
                        "",
                        "负面：" + item["negative_prompt"],
                        "",
                    ]
        data = "\n".join(lines).encode("utf-8")
        manifest = {k: v for k, v in snapshot.items() if k not in {"records", "provenance"}}
        manifest["provenance"] = {
            ref: {
                "key": r["key"],
                "content_hash": r["content_hash"],
                "dependency_refs": r["dependency_refs"],
            }
            for ref, r in snapshot["provenance"].items()
        }
        manifest["records"] = {
            k: {
                "id": r["id"],
                "content_hash": r["content_hash"],
                "dependency_refs": r["dependency_refs"],
            }
            for k, r in snapshot["records"].items()
        }
        manifest["file_sha256"] = hashlib.sha256(data).hexdigest()
        manifest["file_name"] = path.name
        path.parent.mkdir(parents=True, exist_ok=True)

        def write_atomic(target: Path, content: bytes) -> None:
            fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".export-", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                if overwrite:
                    os.replace(temporary, target)
                else:
                    # O_EXCL via hard-link also protects a racing exporter from overwriting.
                    os.link(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)

        write_atomic(sidecar, encode(manifest).encode("utf-8"))
        write_atomic(path, data)
        with self.repository.transaction() as state:
            state["exports"].append(
                {
                    "id": snapshot["id"],
                    "snapshot_revision": snapshot["revision"],
                    "mode": snapshot["mode"],
                    "path": str(path),
                    "file_sha256": manifest["file_sha256"],
                    "created_at": timestamp(),
                }
            )
        return {"path": str(path), "manifest_path": str(sidecar), "snapshot": snapshot}
