"""Optional, bounded-concurrent image batches with durable per-view provenance."""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Callable, Iterator
from contextlib import AsyncExitStack, contextmanager
from copy import deepcopy
from uuid import uuid4

import httpx
from filelock import Timeout
from openai import APIConnectionError, AsyncOpenAI
from PySide6.QtGui import QImage

from core.generation import api_providers
from core.generation.api_settings import get_image_config
from core.generation.cancellation import CancelToken, GenerationCancelled
from core.generation.locks import project_lock

from .contracts import JsonObject, ProgressReporter
from .feedback import TaskTracker, error_reason
from .llm import retry_delay
from .media_repository import MediaRepository
from .models import WorkspaceError, fingerprint, require, timestamp
from .repository import ProjectRepository
from .validation import PLACEHOLDER, SPEC_FIELDS, UNRESOLVED, view_input_hash

logger = logging.getLogger(__name__)
FROZEN_IMAGE_FIELDS = (
    "key",
    "catalog_id",
    "asset_id",
    "variant_id",
    "view_id",
    "spec_id",
    "prompt_hash",
    "design",
    "positive_prompt",
    "negative_prompt",
    "request_prompt",
    "image_id",
    "response_path",
)


def image_item_hash(item: JsonObject) -> str:
    return fingerprint({key: item[key] for key in FROZEN_IMAGE_FIELDS})


def image_batch_hash(batch: JsonObject) -> str:
    return fingerprint(
        {
            "model": batch["model_snapshot"],
            "concurrency": batch["concurrency"],
            "items": [item["input_hash"] for item in batch["items"]],
        }
    )


def image_model_snapshot() -> JsonObject:
    config = get_image_config()
    return {"model": config.model, "endpoint_hash": fingerprint(config.base_url)}


def image_prompt_hash(spec: JsonObject, view: JsonObject) -> str:
    return fingerprint(
        {
            "design": view_input_hash(spec["kind"], spec["content"], view),
            "positive": view.get("positive_prompt", ""),
            "negative": view.get("negative_prompt", ""),
        }
    )


def image_view_key(catalog_id: str, asset_id: str, variant_id: str, view_id: str) -> str:
    return fingerprint([catalog_id, asset_id, variant_id, view_id])


class ImageGenerationStore:
    def __init__(self, output_dir: str) -> None:
        self.media = MediaRepository(output_dir)

    @contextmanager
    def generation_lock(self) -> Iterator[None]:
        self.media.root.mkdir(parents=True, exist_ok=True)
        try:
            with project_lock(str(self.media.root / ".image-generation.lock"), timeout=0):
                yield
        except Timeout as exc:
            raise WorkspaceError("PROJECT_BUSY", "项目已有生图任务，请等待完成或停止") from exc

    def load(self) -> JsonObject:
        try:
            state = self.media.read("image_generation.json")
        except FileNotFoundError:
            return {"version": 1, "batches": {}, "bindings": {}}
        require(
            isinstance(state, dict)
            and state.get("version") == 1
            and isinstance(state.get("batches"), dict)
            and isinstance(state.get("bindings"), dict),
            "INVALID_IMAGE_STATE",
            "生图任务记录格式不正确，原文件未改动",
        )
        return state

    def update(self, change: Callable[[JsonObject], None]) -> JsonObject:
        with project_lock(str(self.media.root / "media.lock")):
            state = self.load()
            change(state)
            self.media.write("image_generation.json", state)
        return state

    def batch(self, batch_id: str) -> JsonObject:
        state = self.load()
        require(batch_id in state["batches"], "MISSING_IMAGE_BATCH", "找不到原生图批次")
        return state["batches"][batch_id]

    def recover(self) -> None:
        def change(state: JsonObject) -> None:
            for batch in state["batches"].values():
                if batch["status"] != "running":
                    continue
                batch["status"] = "interrupted"
                for item in batch["items"]:
                    if item["status"] in {"requesting", "received", "saving"}:
                        item["status"] = (
                            "received"
                            if self.media.resolve(item["response_path"]).exists()
                            else "unknown"
                        )

        # Startup recovery is explicit and never issues a remote request.
        if self.media.resolve("image_generation.json").exists():
            self.update(change)


def image_for_view(
    output_dir: str,
    catalog_id: str,
    spec: JsonObject,
    view: JsonObject,
    *,
    state: JsonObject | None = None,
) -> JsonObject | None:
    store = ImageGenerationStore(output_dir)
    state = store.load() if state is None else state
    value = spec["content"]
    key = image_view_key(catalog_id, value["asset_id"], value["variant_id"], view["view_id"])
    binding = state["bindings"].get(key)
    if not binding:
        return None
    image = store.media.image(binding["image_id"])
    return {**image, "current": binding["prompt_hash"] == image_prompt_hash(spec, view)}


class ImageGenerationService:
    def __init__(self, repository: ProjectRepository) -> None:
        self.repository = repository
        self.store = ImageGenerationStore(repository.output_dir)

    def recover(self) -> None:
        if self.store.media.resolve("image_generation.json").exists():
            try:
                with self.store.generation_lock():
                    self.store.recover()
            except WorkspaceError as exc:
                if exc.code != "PROJECT_BUSY":
                    raise

    def rows(self) -> list[JsonObject]:
        # Prompt commits can remove superseded artifact files. Read the complete
        # provenance projection under the same short project lock as those commits.
        if not self.repository.root.exists():
            return []
        with project_lock(str(self.repository.root / ".v2-project.lock"), timeout=5):
            return self._rows()

    def _rows(self) -> list[JsonObject]:
        from .workflow import WorkflowService

        state = self.repository.load()
        board = self.repository.current("storyboard", state=state)
        if not board:
            return []
        catalog_id = board["dependency_refs"]["asset_catalog"]
        found = {}
        for shot in board["content"]["shots"]:
            for asset in WorkflowService(self.repository).shot_assets(shot["shot_id"], state=state):
                spec = (
                    self.repository.record(asset["spec_id"], state=state)
                    if asset["spec_id"]
                    else None
                )
                actual = {v["view_id"]: v for v in spec["content"]["views"]} if spec else {}
                for view in asset["views"]:
                    key = image_view_key(
                        catalog_id, asset["asset_id"], asset["variant_id"], view["view_id"]
                    )
                    if key in found:
                        found[key]["shots"].append(shot["order"])
                        continue
                    original = actual.get(view["view_id"])
                    reason = ""
                    if not spec or not original or not view["available"]:
                        reason = "提示词尚未生成"
                    elif any(
                        not spec["content"].get(field, "").strip()
                        for field in SPEC_FIELDS[spec["kind"]]
                    ):
                        reason = "资产视觉设定尚未完整"
                    elif any(
                        PLACEHOLDER.search(view[field]) or UNRESOLVED.search(view[field])
                        for field in ("positive_prompt", "negative_prompt")
                    ):
                        reason = "提示词含未填写内容"
                    elif original.get("spec_input_hash") != view_input_hash(
                        spec["kind"], spec["content"], original
                    ):
                        reason = "提示词与视觉设定不一致，请更新提示词"
                    found[key] = {
                        "key": key,
                        "catalog_id": catalog_id,
                        **{
                            k: asset[k]
                            for k in (
                                "kind",
                                "asset_id",
                                "variant_id",
                                "name",
                                "variant_label",
                                "spec_id",
                            )
                        },
                        "view_id": view["view_id"],
                        "view_label": view["label"],
                        "positive_prompt": view["positive_prompt"],
                        "negative_prompt": view["negative_prompt"],
                        "prompt_hash": image_prompt_hash(spec, original) if original else None,
                        "design": {
                            field: spec["content"][field] for field in SPEC_FIELDS[spec["kind"]]
                        }
                        if spec
                        else {},
                        "shots": [shot["order"]],
                        "reason": reason,
                        "image": view.get("image"),
                    }
        return list(found.values())

    def prepare(self, keys: list[str], *, concurrency: int = 2) -> JsonObject:
        require(
            type(concurrency) is int and 1 <= concurrency <= 8, "INVALID_INPUT", "并行数应为1～8"
        )
        require(bool(keys) and len(set(keys)) == len(keys), "INVALID_INPUT", "请勾选需要生成的图片")
        rows = {row["key"]: row for row in self.rows()}
        require(
            all(k in rows and not rows[k]["reason"] for k in keys),
            "STALE_INPUT",
            "所选图片提示词缺失或已变化，请刷新核对",
        )
        model = image_model_snapshot()
        batch_id = uuid4().hex
        items = []
        for key in keys:
            row = deepcopy(rows[key])
            row.pop("image", None)
            image_id = uuid4().hex
            row.update(
                image_id=image_id,
                status="queued",
                attempts=0,
                response_path=f"image_responses/{image_id}.json",
                request_prompt=row["positive_prompt"]
                + (
                    "\n\n排除或避免的元素：\n" + row["negative_prompt"]
                    if row["negative_prompt"].strip()
                    else ""
                ),
            )
            row["input_hash"] = image_item_hash(row)
            items.append(row)
        command = {
            "id": batch_id,
            "model_snapshot": model,
            "concurrency": concurrency,
            "items": items,
            "status": "prepared",
            "created_at": timestamp(),
        }
        command["input_hash"] = image_batch_hash(command)
        return command


class ImageGenerationWork(TaskTracker):
    drain_on_cancel = True

    def __init__(
        self,
        repository: ProjectRepository,
        command: JsonObject | None = None,
        *,
        batch_id: str | None = None,
        keys: list[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__("资产图片生成")
        self.repository, self.command = repository, deepcopy(command)
        self.output_dir = repository.output_dir
        self.store = ImageGenerationStore(self.output_dir)
        self.batch_id = command["id"] if command else batch_id
        require(bool(self.batch_id), "INVALID_INPUT", "缺少生图批次编号")
        self.keys, self.transport = keys, transport
        self.token = CancelToken()

    def item_update(self, image_id: str, **changes: object) -> None:
        def update(state: JsonObject) -> None:
            item = next(
                i for i in state["batches"][self.batch_id]["items"] if i["image_id"] == image_id
            )
            item.update(changes)

        self.store.update(update)

    def notify(self, report: ProgressReporter) -> None:
        items = self.store.batch(self.batch_id)["items"]
        completed = sum(i["status"] == "succeeded" for i in items)
        self.update_context(
            total=len(items),
            completed=completed,
            saved=bool(completed),
            resumable=any(i["status"] not in {"succeeded", "unknown", "stale"} for i in items),
            phase="正在停止，等待已发送请求返回" if self.token.cancelled else "并行生图与保存",
            target=f"已保存 {completed}/{len(items)} 张",
        )
        report(self.task_context.location(), completed / len(items))

    def selected(self, item: JsonObject) -> bool:
        return (
            item["status"] not in {"succeeded", "stale"}
            and (self.keys is None or item["key"] in self.keys)
            and (item["status"] != "unknown" or self.keys is not None and item["key"] in self.keys)
        )

    def __call__(self, report: ProgressReporter) -> JsonObject:
        with self.store.generation_lock():
            if self.command:

                def start(state: JsonObject) -> None:
                    require(
                        self.batch_id not in state["batches"],
                        "REQUEST_KEY_CONFLICT",
                        "批次已存在，请使用继续未完成",
                    )
                    state["batches"][self.batch_id] = deepcopy(self.command)

                self.store.update(start)
            else:
                self.store.recover()
            batch = self.store.batch(self.batch_id)
            require(
                batch.get("input_hash") == image_batch_hash(batch)
                and all(item.get("input_hash") == image_item_hash(item) for item in batch["items"]),
                "INVALID_IMAGE_STATE",
                "生图批次的冻结输入已变化，请重新勾选生成",
            )
            self.update_context(total=len(batch["items"]), resumable=True)
            self.needs_api = any(
                self.selected(i) and not self.store.media.resolve(i["response_path"]).exists()
                for i in batch["items"]
            )
            if self.needs_api:
                require(
                    image_model_snapshot() == batch["model_snapshot"],
                    "IMAGE_CONFIGURATION_CHANGED",
                    "生图模型或接口已变化，请恢复原配置后继续，或勾选后新建生图批次",
                )
            self.store.update(
                lambda state: state["batches"][self.batch_id].update(status="running")
            )
            try:
                asyncio.run(self.execute(batch, report))
            except Exception:
                self.store.update(
                    lambda state: state["batches"][self.batch_id].update(status="interrupted")
                )
                raise
            items = self.store.batch(self.batch_id)["items"]
            counts = {
                name: sum(i["status"] == name for i in items)
                for name in ("succeeded", "failed", "unknown", "paused", "stale")
            }
            status = "succeeded" if counts["succeeded"] == len(items) else "partial"
            self.store.update(lambda state: state["batches"][self.batch_id].update(status=status))
            self.notify(report)
            if self.token.cancelled:
                raise GenerationCancelled()
            summary = f"已保存{counts['succeeded']}张；失败{counts['failed']}项；结果未知{counts['unknown']}项；待继续{counts['paused']}项；来源已变化{counts['stale']}项"
            return {
                "status": status,
                "batch_id": self.batch_id,
                "image_summary": summary,
                "counts": counts,
            }

    async def execute(self, batch: JsonObject, report: ProgressReporter) -> None:
        timeout = httpx.Timeout(600, connect=10)
        self.halt = False
        self.fatal: list[Exception] = []
        semaphore = asyncio.Semaphore(batch["concurrency"])
        async with AsyncExitStack() as stack:
            client = None
            if self.needs_api:
                config = get_image_config()
                client = await stack.enter_async_context(
                    api_providers.AsyncOpenAI(
                        api_key=config.api_key,
                        base_url=config.base_url,
                        max_retries=0,
                        http_client=httpx.AsyncClient(transport=self.transport, timeout=timeout),
                    )
                )
            download = await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=self.transport,
                    timeout=httpx.Timeout(60, connect=10),
                    follow_redirects=True,
                )
            )

            async def run(item: JsonObject) -> None:
                async with semaphore:
                    if not self.selected(item):
                        return
                    if self.token.cancelled or self.halt:
                        self.item_update(item["image_id"], status="paused")
                        return
                    await self.execute_item(item, batch, client, download, report)

            await asyncio.gather(*(run(item) for item in batch["items"]))
        if self.fatal:
            raise self.fatal[0]

    def source_current(self, item: JsonObject) -> bool:
        row = next(
            (r for r in ImageGenerationService(self.repository).rows() if r["key"] == item["key"]),
            None,
        )
        return bool(row and not row["reason"] and row["prompt_hash"] == item["prompt_hash"])

    async def execute_item(
        self,
        item: JsonObject,
        batch: JsonObject,
        client: AsyncOpenAI | None,
        download: httpx.AsyncClient,
        report: ProgressReporter,
    ) -> None:
        image_id = item["image_id"]
        response_path = self.store.media.resolve(item["response_path"])
        has_response = response_path.exists()
        requesting = False
        response_received = has_response
        try:
            if not has_response:
                require(client is not None, "INVALID_INPUT", "该生图步骤缺少接口连接")
                require(
                    self.source_current(item),
                    "STALE_INPUT",
                    "该资产视图提示词已变化，未发送生图请求",
                )
                require(
                    image_model_snapshot() == batch["model_snapshot"],
                    "IMAGE_CONFIGURATION_CHANGED",
                    "生图服务配置已变化，未发送请求",
                )
                for attempt in range(3):
                    self.token.raise_if_cancelled()
                    require(
                        self.source_current(item),
                        "STALE_INPUT",
                        "该资产视图提示词已变化，未发送生图请求",
                    )
                    require(
                        image_model_snapshot() == batch["model_snapshot"],
                        "IMAGE_CONFIGURATION_CHANGED",
                        "生图服务配置已变化，未发送请求",
                    )
                    self.item_update(
                        image_id,
                        status="requesting",
                        attempts=item["attempts"] + attempt + 1,
                        error=None,
                    )
                    self.notify(report)
                    requesting = True
                    try:
                        result = await client.images.generate(
                            model=batch["model_snapshot"]["model"],
                            prompt=item["request_prompt"],
                            n=1,
                        )
                        requesting = False
                        response_received = True
                        break
                    except Exception as exc:
                        safe = safe_image_retry(exc)
                        delay = retry_delay(exc, attempt) if safe and attempt < 2 else None
                        if delay is None:
                            raise
                        requesting = False
                        until = asyncio.get_running_loop().time() + delay
                        self.item_update(image_id, status="retrying", error=error_reason(exc))
                        self.notify(report)
                        while asyncio.get_running_loop().time() < until:
                            self.token.raise_if_cancelled()
                            await asyncio.sleep(
                                min(0.1, max(0, until - asyncio.get_running_loop().time()))
                            )
                require(bool(result.data), "INVALID_IMAGE_RESPONSE", "生图接口未返回图片")
                response = result.data[0].model_dump()
                require(
                    bool(response.get("b64_json") or response.get("url")),
                    "INVALID_IMAGE_RESPONSE",
                    "生图接口未返回Base64图片或下载地址",
                )
                self.store.media.write(item["response_path"], response)
            self.item_update(image_id, status="received", error=None)
            self.notify(report)
            response = self.store.media.read(item["response_path"])
            if response.get("b64_json"):
                data = base64.b64decode(response["b64_json"], validate=True)
            else:
                remote = await download.get(response["url"])
                remote.raise_for_status()
                data = remote.content
            require(
                not QImage.fromData(data).isNull(),
                "INVALID_IMAGE_RESPONSE",
                "接口返回的文件不是可读取图片",
            )
            self.item_update(image_id, status="saving")
            source = {
                "kind": "image_generation",
                "batch_id": self.batch_id,
                "model_snapshot": batch["model_snapshot"],
                **{
                    k: item[k]
                    for k in (
                        "catalog_id",
                        "asset_id",
                        "variant_id",
                        "view_id",
                        "spec_id",
                        "prompt_hash",
                        "design",
                        "positive_prompt",
                        "negative_prompt",
                        "request_prompt",
                    )
                },
            }
            self.store.media.save_image(
                data,
                name=f"{item['name']}·{item['variant_label']}·{item['view_label']}",
                source=source,
                image_id=image_id,
            )
            current = self.source_current(item)

            def finish(state: JsonObject) -> None:
                saved = next(
                    i for i in state["batches"][self.batch_id]["items"] if i["image_id"] == image_id
                )
                saved.update(
                    status="succeeded", current=current, error=None, finished_at=timestamp()
                )
                if current:
                    state["bindings"][item["key"]] = {
                        "image_id": image_id,
                        "prompt_hash": item["prompt_hash"],
                    }

            # Serialize provenance selection with manual project edits.
            with project_lock(str(self.repository.root / ".v2-project.lock"), timeout=5):
                current = self.source_current(item)
                self.store.update(finish)
        except GenerationCancelled:
            self.item_update(image_id, status="paused")
        except Exception as exc:
            logger.error(
                "资产图片 %s·%s·%s 失败",
                item["name"],
                item["variant_label"],
                item["view_label"],
                exc_info=True,
            )
            unknown = (
                requesting
                and isinstance(
                    exc, (APIConnectionError, httpx.TransportError, TimeoutError, ConnectionError)
                )
                and not safe_image_retry(exc)
            ) or (response_received and isinstance(exc, OSError) and not response_path.exists())
            status = (
                "unknown"
                if unknown
                else "stale"
                if isinstance(exc, WorkspaceError) and exc.code == "STALE_INPUT"
                else "failed"
            )
            self.item_update(
                image_id, status=status, error=f"{type(exc).__name__}：{error_reason(exc)}"
            )
            if (
                getattr(exc, "status_code", None) in {401, 403}
                or isinstance(exc, WorkspaceError)
                and exc.code == "IMAGE_CONFIGURATION_CHANGED"
            ):
                self.halt = True
            if isinstance(exc, OSError) and not isinstance(
                exc, (httpx.HTTPError, TimeoutError, ConnectionError)
            ):
                self.halt = True
                self.fatal.append(exc)
        self.notify(report)


def safe_image_retry(error: Exception) -> bool:
    cause = error.__cause__ if isinstance(error, APIConnectionError) else error
    return getattr(error, "status_code", None) == 429 or isinstance(
        cause, (httpx.ConnectError, httpx.ConnectTimeout)
    )
