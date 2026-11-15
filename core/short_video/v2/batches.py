"""Explicit durable batch plans. Successful items are never requested again on resume."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from core.generation.cancellation import CancelToken, GenerationCancelled

from .contracts import JsonObject, OperationResult, ProgressReporter, TextTransport
from .llm import ScriptLLM
from .models import (
    WorkspaceError,
    capture_model_snapshot,
    encode,
    fingerprint,
    identity,
    parse_object,
    reference_text,
    reject_json_constant,
    require,
    timestamp,
    validate_model_snapshot,
    validate_model_snapshot_format,
)
from .project_service import ProjectService
from .prompts import INSTRUCTIONS, stage_prompt
from .repository import ProjectRepository
from .source import validate_source_ref
from .storage import atomic_write_text
from .validation import rows, validate_content
from .workflow import WorkflowService


class BatchPlanService:
    mode = "base"

    def __init__(
        self,
        repository: ProjectRepository,
        model: str = "hosted-llm",
        *,
        transport: TextTransport | None = None,
    ) -> None:
        self.repository, self.model, self.transport = repository, model, transport

    def path(self, plan_id: str) -> Path:
        from .models import ID_PATTERN

        require(
            isinstance(plan_id, str) and ID_PATTERN.fullmatch(plan_id),
            "INVALID_INPUT",
            "计划编号无效",
        )
        return self.repository.root / "plans" / f"{plan_id}.json"

    def load_plan(self, plan_id: str) -> JsonObject:
        try:
            value = json.loads(
                self.path(plan_id).read_text(encoding="utf-8"), parse_constant=reject_json_constant
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise WorkspaceError("INVALID_PLAN", "批量计划损坏或丢失") from exc
        require(
            isinstance(value, dict)
            and {
                "id",
                "project_id",
                "mode",
                "model",
                "model_snapshot",
                "input_refs",
                "input_hash",
                "selected_before",
                "calls_used",
                "status",
            }
            <= value.keys(),
            "INVALID_PLAN",
            "计划缺少当前必需字段",
        )
        require(
            value["id"] == plan_id and value["project_id"] == self.repository.load()["project_id"],
            "INVALID_PLAN",
            "计划不属于当前项目",
        )
        require(
            value["mode"] == "chunks",
            "INVALID_PLAN",
            "计划类型无效",
        )
        require(
            isinstance(value["input_refs"], dict) and isinstance(value["model"], str),
            "INVALID_PLAN",
            "计划输入绑定或模型无效",
        )
        require(
            type(value["calls_used"]) is int and value["calls_used"] >= 0,
            "INVALID_PLAN",
            "计划调用记录无效",
        )
        if value["mode"] == "chunks":
            require(
                isinstance(value.get("chunks"), list)
                and all(
                    isinstance(chunk, dict)
                    and {"id", "source_id", "start", "end", "text", "facts", "output"}
                    <= chunk.keys()
                    for chunk in value["chunks"]
                ),
                "INVALID_PLAN",
                "分块计划缺少当前字段",
            )
        validate_model_snapshot_format(value["model_snapshot"])
        require(
            value["model"] == value["model_snapshot"]["provider"],
            "INVALID_PLAN",
            "计划模型与快照不一致",
        )
        require(
            value.get("input_hash") == self.hash_plan_inputs(value),
            "INVALID_PLAN",
            "批量计划的冻结输入已变化",
        )
        for chunk in value.get("chunks", []):
            for field in ("facts", "output"):
                if chunk.get(field) is not None:
                    require(
                        chunk.get(field + "_hash") == fingerprint(chunk[field]),
                        "INVALID_PLAN",
                        "已完成分块的内容校验失败",
                    )
        return value

    @staticmethod
    def hash_plan_inputs(plan: JsonObject) -> str:
        data = {
            key: plan[key]
            for key in ("project_id", "mode", "model", "model_snapshot", "input_refs")
        }
        data.update(
            selected_before=plan["selected_before"],
            chunks=[
                {key: chunk[key] for key in ("id", "source_id", "start", "end", "text")}
                for chunk in plan["chunks"]
            ],
        )
        # 旧计划的冻结哈希包含这两个字段；仅兼容核验，不用于限制执行。
        if "max_calls" in plan and "seconds" in plan:
            data.update(max_calls=plan["max_calls"], seconds=plan["seconds"])
        return fingerprint(data)

    def save_plan(self, plan: JsonObject) -> None:
        plan["updated_at"] = timestamp()
        atomic_write_text(self.path(plan["id"]), encode(plan))

    def find_resumable_plan(self) -> JsonObject | None:
        folder = self.repository.root / "plans"
        plans = (
            [self.load_plan(p.stem) for p in folder.glob("plan_*.json")] if folder.exists() else []
        )
        plans = [
            p
            for p in plans
            if p["mode"] == self.mode and p["status"] != "succeeded" and self.inputs_are_current(p)
        ]
        return max(plans, key=lambda p: p["updated_at"]) if plans else None

    def inputs_are_current(self, plan: JsonObject) -> bool:
        state = self.repository.load()
        return all(
            state["current"].get(k) == v and self.repository.is_current(v, state=state)
            for k, v in plan["input_refs"].items()
        )


class ChunkService(BatchPlanService):
    mode = "chunks"

    def __init__(
        self,
        repository: ProjectRepository,
        model: str = "hosted-llm",
        *,
        transport: TextTransport | None = None,
        chunk_chars: int = 8000,
    ) -> None:
        super().__init__(repository, model, transport=transport)
        self.chunk_chars = chunk_chars

    def create_plan(self) -> JsonObject:
        state = self.repository.load()
        article = ProjectService(self.repository).context(state=state)
        config = self.repository.current("config", state=state)
        require(self.chunk_chars >= 100, "INVALID_INPUT", "分块容量过小")
        chunks = []
        for segment in article["segments"]:
            start = segment["start"]
            while start < segment["end"]:
                end = min(start + self.chunk_chars, segment["end"])
                original = self.repository.record(article["source_id"], state=state)["content"][
                    "normalized_text"
                ]
                if end < segment["end"]:
                    boundary = original.rfind("\n", start + self.chunk_chars // 2, end)
                    if boundary > start:
                        end = boundary + 1
                chunks.append(
                    {
                        "id": identity("chunk"),
                        "source_id": article["source_id"],
                        "start": start,
                        "end": end,
                        "text": original[start:end],
                        "facts": None,
                        "output": None,
                    }
                )
                start = end
        plan = {
            "id": identity("plan"),
            "mode": self.mode,
            "project_id": state["project_id"],
            "status": "planned",
            "model": self.model,
            "model_snapshot": capture_model_snapshot(self.model),
            "input_refs": {"selection": article["selection_id"], "config": config["id"]},
            "chunks": chunks,
            "calls_used": 0,
            "attempts": [],
            "output_ref": None,
            "selected_before": state["current"].get("novel_copy"),
            "created_at": timestamp(),
        }
        plan["input_hash"] = self.hash_plan_inputs(plan)
        self.save_plan(plan)
        return plan

    def execute(
        self,
        plan_id: str,
        *,
        token: CancelToken | None = None,
        report: ProgressReporter | None = None,
    ) -> JsonObject:
        token, report = token or CancelToken(), report or (lambda *args: None)
        with self.repository.generation_lock():
            plan = self.load_plan(plan_id)
            require(
                self.inputs_are_current(plan),
                "STALE_INPUT",
                "分块计划来源已过期，请基于当前文章新建计划",
            )
            if plan["status"] == "succeeded":
                return {"status": "succeeded", "version_id": plan["output_ref"]}
            plan["status"] = "running"
            self.save_plan(plan)
            try:
                config = self.repository.record(plan["input_refs"]["config"])["content"]
                custom = self.repository.current("prompt_template:novel_copy")
                instruction = (
                    custom["content"]["instruction"] if custom else INSTRUCTIONS["novel_copy"]
                )
                for index, chunk in enumerate(plan["chunks"]):
                    token.raise_if_cancelled()
                    if chunk["facts"] is not None:
                        continue
                    report(
                        f"提取第 {index + 1}/{len(plan['chunks'])} 块事实",
                        index / (len(plan["chunks"]) * 2 + 1),
                    )
                    prompt = (
                        '提取本段明确存在的事实，不补写。只输出JSON：{"facts":[{"text":"事实",'
                        '"source_refs":[{"source_id":"来源编号","start":0,"end":1}]}]}。偏移是原文码点坐标。\n'
                        + encode(chunk)
                    )

                    def validate_facts(content: JsonObject) -> list[JsonObject]:
                        facts = rows(content.get("facts"), "事实表", key=None, empty=True)
                        for fact in facts:
                            require(
                                isinstance(fact.get("text"), str)
                                and fact["text"].strip()
                                and bool(fact.get("source_refs")),
                                "INVALID_OUTPUT",
                                "事实缺少文字或依据",
                            )
                            for ref in fact["source_refs"]:
                                validate_source_ref(ref, self.repository.record(chunk["source_id"]))
                                require(
                                    ref["source_id"] == chunk["source_id"]
                                    and chunk["start"] <= ref["start"] < ref["end"] <= chunk["end"],
                                    "INVALID_OUTPUT",
                                    "事实引文越过分块",
                                )
                        return facts

                    chunk["facts"] = self.request_chunk_content(
                        plan, prompt, validate_facts, token, "facts:" + chunk["id"]
                    )
                    chunk["facts_hash"] = fingerprint(chunk["facts"])
                    self.save_plan(plan)
                    token.raise_if_cancelled()
                shared_facts = [f for chunk in plan["chunks"] for f in chunk["facts"]]
                require(
                    len(encode(reference_text(shared_facts))) <= 20000,
                    "INPUT_TOO_LARGE",
                    "共享事实表超限，请缩小选区或人工整理后新建计划",
                )
                if shared_facts and not plan.get("fact_table_ref"):
                    saved_facts = self.repository.save(
                        "fact_table",
                        {"facts": shared_facts, "plan_id": plan["id"]},
                        plan["input_refs"],
                        expected_revision=self.repository.load()["revision"],
                        origin="model",
                    )
                    plan["fact_table_ref"] = saved_facts["id"]
                    self.save_plan(plan)

                def validate_copy(content: JsonObject) -> JsonObject:
                    return validate_content(
                        "novel_copy",
                        content,
                        dependencies=plan["input_refs"],
                        resolve=self.repository.record,
                    )

                for index, chunk in enumerate(plan["chunks"]):
                    token.raise_if_cancelled()
                    if chunk["output"] is not None:
                        continue
                    article = {
                        "source_id": chunk["source_id"],
                        "selection_id": plan["input_refs"]["selection"],
                        "text": chunk["text"],
                        "segments": [{k: chunk[k] for k in ("source_id", "start", "end", "text")}],
                        "source_refs": [
                            {
                                "source_id": chunk["source_id"],
                                "start": chunk["start"],
                                "end": chunk["end"],
                            }
                        ],
                    }
                    context = {"article": article, "config": config, "shared_facts": shared_facts}
                    prompt = stage_prompt(
                        "novel_copy",
                        context,
                        instruction=instruction
                        + "本次仅整理当前块，结合共享事实保持身份与因果。保留本段来源和实际停点，后续块才有的结果留到合稿处理，不为每块补造结局。",
                    )
                    report(
                        f"改编第 {index + 1}/{len(plan['chunks'])} 块",
                        (len(plan["chunks"]) + index) / (len(plan["chunks"]) * 2 + 1),
                    )
                    chunk["output"] = self.request_chunk_content(
                        plan, prompt, validate_copy, token, "copy:" + chunk["id"]
                    )
                    chunk["output_hash"] = fingerprint(chunk["output"])
                    self.save_plan(plan)
                    token.raise_if_cancelled()
                service = WorkflowService(self.repository, plan["model"], transport=self.transport)
                command = service.prepare("novel_copy", fact_table_id=plan.get("fact_table_ref"))
                command["prompt"] += (
                    "\n从各块中组织本条完整事件，合并重复开头、背景和结尾，保留核心因果、已有结果与原始码点来源；没有结果时如实说明素材不足，不把分块停点拼成连载预告：\n"
                    + encode(
                        reference_text({"copies": [chunk["output"] for chunk in plan["chunks"]]})
                    )
                )
                require(
                    len(command["prompt"]) <= 48000,
                    "INPUT_TOO_LARGE",
                    "合稿上下文超限；分段结果保留，请调整改编范围",
                )
                command["plan_id"] = plan["id"]
                command["selected_before"] = plan["selected_before"]

                def record_merge(sent: str, response: str, error: str | None) -> None:
                    plan["calls_used"] += 1
                    self.save_plan(plan)

                result = service.execute(
                    command, token=token, report=report, on_request=record_merge
                )
                plan.update(output_ref=result["version_id"], status="succeeded")
                self.save_plan(plan)
                return result
            except Exception as exc:
                plan["status"] = (
                    "cancelled"
                    if token.cancelled or isinstance(exc, GenerationCancelled)
                    else "failed"
                )
                self.save_plan(plan)
                raise

    def request_chunk_content(
        self,
        plan: JsonObject,
        prompt: str,
        validate: Callable[[JsonObject], OperationResult],
        token: CancelToken,
        item_id: str,
    ) -> OperationResult:
        validate_model_snapshot(plan["model_snapshot"])
        require(
            len(prompt) <= 48000, "INPUT_TOO_LARGE", "分块任务上下文超限；已完成块保留，请调整计划"
        )
        attempt = {"id": identity("attempt"), "item_id": item_id, "calls": [], "status": "running"}
        plan["attempts"].append(attempt)
        self.save_plan(plan)

        def record_call(sent: str, response: str, error: str | None) -> None:
            plan["calls_used"] += 1
            attempt["calls"].append({"prompt": sent, "response": response, "error": error})
            self.save_plan(plan)

        send = ScriptLLM(plan["model"], token=token, transport=self.transport, record=record_call)
        current = prompt
        try:
            for attempt_index in range(2):
                token.raise_if_cancelled()
                raw = send(current)
                try:
                    result = validate(parse_object(raw))
                    attempt["status"] = "succeeded"
                    self.save_plan(plan)
                    return result
                except WorkspaceError as exc:
                    if exc.code not in {"INVALID_INPUT", "INVALID_OUTPUT"}:
                        raise
                    token.raise_if_cancelled()
                    if attempt_index == 1:
                        raise WorkspaceError(
                            "INVALID_OUTPUT", "分块输出未通过校验；回复已保存"
                        ) from exc
                    current = (
                        prompt
                        + "\n仅修复格式和引文，保留已有叙事，不新增事实、补结局或借格式修复删改剧情："
                        + str(exc)
                        + "\n"
                        + encode({"previous_response": raw})
                    )
                    require(
                        len(current) <= 48000, "INPUT_TOO_LARGE", "格式修复超限；原始回复已保存"
                    )
        except Exception:
            attempt["status"] = "cancelled" if token.cancelled else "failed"
            self.save_plan(plan)
            raise


def create_batch_service(
    repository: ProjectRepository, mode: str, *, transport: TextTransport | None = None
) -> ChunkService:
    require(mode == "chunks", "INVALID_PLAN", f"未知批量计划类型：{mode}")
    return ChunkService(repository, transport=transport)


class BatchWork:
    def __init__(
        self,
        repository: ProjectRepository,
        plan: JsonObject,
        *,
        mode: str,
        transport: TextTransport | None = None,
    ) -> None:
        self.output_dir, self.plan_id, self.mode, self.transport = (
            repository.output_dir,
            plan["id"],
            mode,
            transport,
        )
        self.token = CancelToken()

    def __call__(self, report: ProgressReporter) -> JsonObject:
        service = create_batch_service(
            ProjectRepository(self.output_dir), self.mode, transport=self.transport
        )
        return service.execute(self.plan_id, token=self.token, report=report)


def recover_plans(repository: ProjectRepository) -> None:
    """Never mark a live process interrupted; recovery never submits new requests."""
    folder = repository.root / "plans"
    if not folder.exists():
        return
    try:
        with repository.generation_lock():
            for path in folder.glob("plan_*.json"):
                try:
                    value = json.loads(
                        path.read_text(encoding="utf-8"), parse_constant=reject_json_constant
                    )
                except (OSError, UnicodeError, ValueError) as exc:
                    raise WorkspaceError("INVALID_PLAN", "计划文件损坏，原文件未修改") from exc
                require(
                    isinstance(value, dict)
                    and value.get("id") == path.stem
                    and value.get("project_id") == repository.load()["project_id"],
                    "INVALID_PLAN",
                    "计划归属或编号损坏",
                )
                if value.get("status") == "running":
                    value.update(status="interrupted", updated_at=timestamp())
                    atomic_write_text(path, encode(value))
    except WorkspaceError as exc:
        if exc.code != "PROJECT_BUSY":
            raise
