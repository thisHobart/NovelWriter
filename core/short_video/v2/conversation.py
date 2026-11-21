"""Project-local discussions with optional read-only content from the page on screen."""

from __future__ import annotations

import logging
from collections.abc import Callable
from copy import deepcopy

from core.common.cancellation import CancelToken, GenerationCancelled

from .contracts import (
    JsonObject,
    ModelRequest,
    ProgressReporter,
    TextTransport,
)
from .conversation_context import (
    ConversationContext,
    build_conversation_context,
    repair_request,
    validate_attachment,
)
from .conversation_store import ConversationStore, select_completed_turns
from .feedback import TaskContext, TaskTracker, failure_feedback
from .llm import ScriptLLM
from .models import (
    WorkspaceError,
    capture_model_snapshot,
    fingerprint,
    identity,
    require,
    text,
    timestamp,
    validate_model_snapshot,
)
from .page_material import validate_attached
from .repository import ProjectRepository
from .storyboard_chat import validate_storyboard_reference
from .storyboard_edits import edit_base, reply_problems, validate_storyboard_edit

logger = logging.getLogger(__name__)

# Stands in for the question while the input box is still empty.
PREVIEW_QUESTION = "（输入框里的消息）"


def current_message(user_text: str, attachment: JsonObject | None) -> JsonObject:
    """The user message a send adds, carrying what it is about when a page is attached: the
    shot for the storyboard, the attachment's label for other pages."""
    message = {
        "id": identity("message"),
        "turn_id": identity("turn"),
        "role": "user",
        "content": user_text,
        "status": "pending",
    }
    if attachment is not None and "reference" in attachment:
        message["storyboard_reference"] = validate_storyboard_reference(attachment["reference"])
    elif attachment is not None and attachment["page"] != "storyboard":
        message["attached"] = validate_attached(attachment)
    return message


class ConversationService:
    def __init__(
        self,
        repository: ProjectRepository,
        model: str = "hosted-llm",
        *,
        transport: TextTransport | None = None,
        max_context_chars: int = 48000,
    ) -> None:
        self.repository, self.model, self.transport = repository, model, transport
        self.max_context_chars = max_context_chars
        self.store = ConversationStore(repository)

    def prepare(
        self,
        conversation_id: str,
        user_text: str,
        *,
        request_id: str | None = None,
        attachment: JsonObject | None = None,
        storyboard_edit: JsonObject | None = None,
    ) -> JsonObject:
        """attachment is what the current page attaches (see validate_attachment).

        storyboard_edit asks for revised shots: {"shots": draft shots being revised,
        "entities": the bound asset catalog}; the attachment must ask for the same shots.
        """
        text(user_text, "消息")
        conversation = self.store.get(conversation_id)
        require(
            not any(m["status"] == "pending" for m in conversation["messages"]),
            "PROJECT_BUSY",
            "会话有未完成消息",
        )
        state = self.repository.load()
        if attachment is not None:
            attachment = deepcopy(validate_attachment(attachment))
        message = current_message(user_text, attachment)
        edit_check = None
        if storyboard_edit is not None:
            shots = deepcopy(storyboard_edit.get("shots"))
            require(
                attachment is not None
                and attachment.get("edit")
                and isinstance(shots, list)
                and [s.get("shot_id") for s in shots] == attachment.get("edit_shot_ids"),
                "INVALID_INPUT",
                "镜头修改请求与附带分镜不一致",
            )
            shot_ids = [s["shot_id"] for s in shots]
            message["storyboard_edit"] = validate_storyboard_edit(
                {"shot_ids": shot_ids, "base": edit_base(shots, shot_ids)}
            )
            # The shots as sent let one format repair check the reply before it is saved; the
            # order maps the reply's shot numbers back to them.
            edit_check = {
                "shots": {s["shot_id"]: s for s in shots},
                "entities": deepcopy(storyboard_edit.get("entities", [])),
                "order": message.get("storyboard_reference", {}).get("shot_ids"),
            }
        history = select_completed_turns(conversation["messages"])
        context = build_conversation_context(
            history + [message],
            message["id"],
            max_context_chars=self.max_context_chars,
            attachment=attachment,
        )
        snapshot = {
            "history_message_ids": [m["id"] for m in context.messages[:-1]],
            "context_hash": fingerprint(context.request),
        }
        for key in ("storyboard_reference", "storyboard_edit", "attached"):
            if key in message:
                snapshot[key] = message[key]
        return {
            **({"edit_check": edit_check} if edit_check else {}),
            "stage": "chat",
            "model": self.model,
            "request_id": request_id or identity("request"),
            "model_snapshot": capture_model_snapshot(self.model),
            "expected_revision": state["revision"],
            "conversation_id": conversation_id,
            "conversation_revision": conversation["revision"],
            "user_text": user_text,
            "context_snapshot": snapshot,
            "messages": context.request,
        }

    def preview(
        self,
        conversation_id: str | None,
        user_text: str,
        *,
        attachment: JsonObject | None = None,
    ) -> ConversationContext:
        """The request sending user_text would make now; nothing is saved or sent.

        A pending reply is left out, as a send would wait for it; an empty input is shown
        as PREVIEW_QUESTION.
        """
        messages = self.store.get(conversation_id)["messages"] if conversation_id else []
        if attachment is not None:
            attachment = validate_attachment(attachment)
        message = current_message(user_text if user_text.strip() else PREVIEW_QUESTION, attachment)
        return build_conversation_context(
            select_completed_turns(messages) + [message],
            message["id"],
            max_context_chars=self.max_context_chars,
            attachment=attachment,
        )

    def execute(
        self,
        command: JsonObject,
        *,
        token: CancelToken | None = None,
        report: ProgressReporter | None = None,
        on_context: Callable[[TaskContext], None] | None = None,
    ) -> JsonObject:
        token = token or CancelToken()
        report = report or (lambda *args: None)
        tracker = TaskTracker("AI 回复", target="当前会话")

        def phase(name: str, *, saved: bool = False) -> None:
            tracker.update_context(phase=name, saved=saved, network_retries=0)
            if on_context:
                on_context(tracker.task_context)

        with self.repository.generation_lock():
            run, created = self.repository.start_run(
                command,
                expected_revision=command["expected_revision"],
                snapshot={
                    "model": command["model"],
                    "context_snapshot": command["context_snapshot"],
                },
            )
            if not created:
                return {
                    "run_id": run["id"],
                    "status": run["status"],
                    "conversation_id": command["conversation_id"],
                }
            user_message = None
            reply_saved = False

            try:
                validate_model_snapshot(command["model_snapshot"])
                phase("保存用户消息")
                user_message = self.store.save_user_message(
                    command["conversation_id"],
                    command["user_text"],
                    turn_id=run["id"],
                    model_snapshot=command["model_snapshot"],
                    expected_revision=command["conversation_revision"],
                    storyboard_reference=command["context_snapshot"].get("storyboard_reference"),
                    storyboard_edit=command["context_snapshot"].get("storyboard_edit"),
                    attached=command["context_snapshot"].get("attached"),
                )

                def record_call(request: ModelRequest, response: str, error: str | None) -> None:
                    self.repository.update_run(
                        run["id"],
                        lambda r, _: r["calls"].append(
                            {
                                "messages": request,
                                "response": response,
                                "error": error,
                                "created_at": timestamp(),
                            }
                        ),
                    )

                send = ScriptLLM(
                    command["model"],
                    token=token,
                    transport=self.transport,
                    record=record_call,
                    before_request=lambda: validate_model_snapshot(command["model_snapshot"]),
                    on_retry=lambda error, attempt, maximum, delay: tracker.report_network_retry(
                        error, attempt, maximum, delay, report, on_context
                    ),
                    trace_context=lambda: {
                        "run_id": run["id"],
                        "conversation_id": command["conversation_id"],
                        "stage": "chat",
                        "storyboard_reference": command["context_snapshot"].get(
                            "storyboard_reference"
                        ),
                        "attached": command["context_snapshot"].get("attached"),
                    },
                )
                token.raise_if_cancelled()
                report("等待 LLM 回复", -1)
                phase("等待回复")
                response = send(command["messages"])
                require(bool(response.strip()), "INVALID_OUTPUT", "模型未返回有效回复")
                check = command.get("edit_check")
                passed, problems = (
                    reply_problems(response, check["shots"], check["entities"], check["order"])
                    if check and not token.cancelled
                    else (0, [])
                )
                if problems:
                    report("修正修改结果", -1)
                    phase("修正修改结果")
                    # One more turn of the same conversation: the reply, then what to fix.
                    repaired = send(
                        [
                            *command["messages"],
                            {"role": "assistant", "content": response},
                            {"role": "user", "content": repair_request(problems)},
                        ]
                    )
                    # The repair is kept only when more shots pass, or as many with fewer problems.
                    fixed, remaining = reply_problems(
                        repaired, check["shots"], check["entities"], check["order"]
                    )
                    if repaired.strip() and (fixed, -len(remaining)) > (passed, -len(problems)):
                        response = repaired
                status = "cancelled" if token.cancelled else "complete"

                phase("保存回复")
                self.store.save_final_reply(
                    command["conversation_id"], user_message["id"], response, status=status
                )
                reply_saved = True
                phase("回复已保存", saved=True)
                self.repository.update_run(
                    run["id"],
                    lambda r, _: r.update(
                        status="cancelled" if token.cancelled else "succeeded", ended_at=timestamp()
                    ),
                )
                token.raise_if_cancelled()
                return {
                    "run_id": run["id"],
                    "status": "succeeded",
                    "conversation_id": command["conversation_id"],
                }
            except Exception as exc:
                status = (
                    "cancelled"
                    if token.cancelled or isinstance(exc, GenerationCancelled)
                    else "failed"
                )
                if user_message is not None and not reply_saved:
                    try:
                        self.store.finish_without_reply(
                            command["conversation_id"], user_message["id"], status
                        )
                    except (OSError, WorkspaceError) as storage_error:
                        exc.add_note(
                            f"会话 {command['conversation_id']} 的失败状态未保存：{type(storage_error).__name__}"
                        )
                        logger.error(
                            "Failed to save conversation failure: conversation_id=%s error_type=%s",
                            command["conversation_id"],
                            type(storage_error).__name__,
                            exc_info=True,
                        )
                self.repository.record_run_failure(
                    run["id"],
                    exc,
                    cancelled=status == "cancelled",
                    feedback=failure_feedback(tracker.task_context, exc),
                )
                raise

    def recover(self) -> None:
        self.repository.recover()
        active = {
            r["id"] for r in self.repository.load()["runs"] if r["status"] in {"queued", "running"}
        }
        self.store.recover(active)


class ChatWork(TaskTracker):
    def __init__(
        self,
        repository: ProjectRepository,
        command: JsonObject,
        *,
        token: CancelToken | None = None,
        transport: TextTransport | None = None,
    ) -> None:
        super().__init__("AI 回复", target="当前会话")
        self.output_dir, self.command = repository.output_dir, deepcopy(command)
        self.token, self.transport = token or CancelToken(), transport

    def __call__(self, report: ProgressReporter) -> JsonObject:
        return ConversationService(
            ProjectRepository(self.output_dir), self.command["model"], transport=self.transport
        ).execute(self.command, token=self.token, report=report, on_context=self.accept_context)
