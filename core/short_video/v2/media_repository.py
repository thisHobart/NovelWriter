"""Local image imports, immutable video jobs and downloaded output files."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from core.generation.locks import project_lock

from .contracts import JsonObject
from .models import WorkspaceError, encode, require, timestamp
from .storage import atomic_write_text


class MediaRepository:
    def __init__(self, output_dir: str | Path) -> None:
        self.root = Path(output_dir).resolve() / "short_video" / "media"

    def resolve(self, relative_path: str) -> Path:
        path = (self.root / relative_path).resolve()
        require(
            path.is_relative_to(self.root.resolve()), "INVALID_INPUT", "媒体文件路径超出项目目录"
        )
        return path

    def read(self, relative_path: str) -> JsonObject:
        return json.loads(self.resolve(relative_path).read_text(encoding="utf-8"))

    def write(self, relative_path: str, value: JsonObject) -> None:
        atomic_write_text(self.resolve(relative_path), encode(value))

    def write_file(self, relative_path: str, data: bytes) -> Path:
        require(bool(data), "INVALID_OUTPUT", "媒体输出文件为空")
        destination = self.resolve(relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
        return destination

    def import_image(self, path: str | Path, *, source: JsonObject | None = None) -> JsonObject:
        """Manual UI and future image workflows share this completed-file import contract."""
        path = Path(path)
        data = path.read_bytes()
        return self.save_image(data, name=path.name, source=source)

    def save_image(
        self,
        data: bytes,
        *,
        name: str,
        source: JsonObject | None = None,
        image_id: str | None = None,
    ) -> JsonObject:
        suffix = image_suffix(data)
        image_id = image_id or uuid4().hex
        require(
            len(image_id) == 32 and all(c in "0123456789abcdef" for c in image_id),
            "INVALID_INPUT",
            "图片编号无效",
        )
        relative_path = f"images/{image_id}{suffix}"
        self.write_file(relative_path, data)
        record = {
            "image_id": image_id,
            "name": name,
            "relative_path": relative_path,
            "sha256": hashlib.sha256(data).hexdigest(),
            "created_at": timestamp(),
            "source": source if source is not None else {"kind": "manual"},
        }
        self.write(f"images/{image_id}.json", record)
        return record

    def images(self) -> list[JsonObject]:
        records = []
        for path in (self.root / "images").glob("*.json"):
            records.append(json.loads(path.read_text(encoding="utf-8")))
        return sorted(records, key=lambda record: record["created_at"])

    def image(self, image_id: str) -> JsonObject:
        return self.read(f"images/{image_id}.json")

    def jobs(self) -> list[JsonObject]:
        records = []
        for path in (self.root / "jobs").glob("*.json"):
            records.append(json.loads(path.read_text(encoding="utf-8")))
        return sorted(records, key=lambda record: record["created_at"])

    def job(self, job_id: str) -> JsonObject:
        return self.read(f"jobs/{job_id}.json")

    def save_job(self, job: JsonObject) -> None:
        with project_lock(str(self.root / "media.lock")):
            job["updated_at"] = timestamp()
            self.write(f"jobs/{job['job_id']}.json", job)

    def video_output_path(self, job_id: str, index: int, suffix: str) -> str:
        """Name outputs by the frozen generation start, independent of download retries."""
        job = self.job(job_id)
        created = datetime.fromisoformat(job["created_at"]).astimezone(timezone(timedelta(hours=8)))
        number = job["input_snapshot"].get("target", {}).get("shot_number")
        label = f"镜头{number:02d}" if type(number) is int and number > 0 else "视频"
        return f"videos/{created:%Y-%m-%d_%H-%M-%S}_{label}_{job_id[:8]}_{index + 1:02d}{suffix}"


def image_suffix(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    raise WorkspaceError("INVALID_INPUT", "请选择 PNG、JPEG 或 WebP 图片")
