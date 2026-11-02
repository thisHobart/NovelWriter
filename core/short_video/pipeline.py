"""Headless short-video workflow with confirmation and bounded whole-story repair."""
from __future__ import annotations

from copy import deepcopy

from agents.writing.short_video_script_agent import ShortVideoScriptAgent
from agents.review.short_video_review_agent import ShortVideoReviewAgent
from core.generation.cancellation import CancelToken, GenerationCancelled
from core.short_video.llm import RequestBudget, ScriptLLM, RecordingError, REQUEST_SECONDS
from core.short_video.models import encode, fingerprint
from core.short_video.repository import ScriptRepository
from core.short_video.validation import script_issues, candidate_rank, passed


class ScriptPipeline:
    def __init__(self, output_dir, model="hosted-llm", *, token=None, report=None, transport=None,
                 budget_factory=RequestBudget):
        self.repo = ScriptRepository(output_dir)
        self.model = model
        self.token = token or CancelToken()
        self.report = report or (lambda *args: None)
        self.transport = transport
        self.budget_factory = budget_factory

    def run(self, action, *, version_id=None, asks=None, expected_source=None):
        if action not in {"outline", "script", "revise"}:
            raise ValueError("未知短视频阶段")
        with self.repo.generation_lock():
            state = deepcopy(self.repo.load())
            if expected_source is not None and expected_source != self.repo.source(state):
                raise ValueError("提交任务后创作依据已变化，请重新确认后再生成")
            if not state["requirements"]:
                raise ValueError("请先保存创作需求")
            if action != "outline" and not self.repo.outline_ready(state):
                raise ValueError("请先确认当前需求对应的大纲")
            source = self.repo.source(state)
            original = None
            if action == "revise":
                original = self.repo.version(version_id)
                if not original or original["source"] != source:
                    raise ValueError("待修订脚本不存在或依据已过期")
                if not asks or not any(str(ask).strip() for ask in asks):
                    raise ValueError("请填写具体修改意见")
            budget = self.budget_factory(max_calls=2 if action == "outline" else 4)
            run_id = self.repo.start_run(action, state, self.model)
            llm = ScriptLLM(self.model, budget, token=self.token, transport=self.transport,
                            record=lambda *args: self.repo.record_call(run_id, *args))
            writer = ShortVideoScriptAgent(self.model, send=llm)
            reviewer = ShortVideoReviewAgent(self.model, send=llm)
            versions = []
            try:
                if action == "outline":
                    self.report("生成完整故事大纲", 0.1)
                    content = self._write(writer, budget, {"action": "outline", "requirements": state["requirements"]}, run_id)
                    self.token.raise_if_cancelled()
                    self.repo.save_outline(content, source=fingerprint(state["requirements"]),
                                           expected=fingerprint(state["outline"]))
                    self.repo.finish_run(run_id, "complete")
                    self.report("大纲已生成，请编辑并确认", 1.0)
                    return {"status": "complete", "run_id": run_id, "calls": budget.calls}

                self.report("生成完整剧情脚本", 0.1)
                task = {"action": action, "requirements": state["requirements"],
                        "outline": state["outline"]["content"]}
                if original:
                    task.update(candidate=original["content"], asks=asks)
                content = self._write(writer, budget, task, run_id)
                version = self.repo.add_version(content, source, run_id=run_id,
                                                issues=script_issues(content, state["requirements"], state["outline"]["content"]))
                versions.append(version)
                self.token.raise_if_cancelled()
                version = self._review(reviewer, state, version)
                versions[-1] = version
                self.token.raise_if_cancelled()
                instructions = [deepcopy(i) for i in version["issues"] if i["target"] == "script"]
                if (version["status"] != "reviewed" and instructions
                        and budget.can_spend(2, reserve_seconds=2 * REQUEST_SECONDS)):
                    self.report("按具体意见完整修订一次", 0.6)
                    task.update(action="revise", candidate=content, asks=instructions)
                    revised = self._write(writer, budget, task, run_id, retry=False)
                    improved = self.repo.add_version(revised, source, run_id=run_id,
                                                     issues=script_issues(revised, state["requirements"], state["outline"]["content"]))
                    versions.append(improved)
                    self.token.raise_if_cancelled()
                    versions[-1] = self._review(reviewer, state, improved)
                    self.token.raise_if_cancelled()
                return self._finish(run_id, versions, budget)
            except GenerationCancelled:
                self._select_best(run_id, versions, budget, "cancelled")
                raise
            except Exception as exc:
                # Every response was already persisted at the SDK boundary. Preserve legal versions too.
                if versions:
                    return self._finish(run_id, versions, budget, error=type(exc).__name__)
                self.repo.finish_run(run_id, "failed", error=type(exc).__name__, calls_used=budget.calls)
                raise RuntimeError("生成未完成，已返回的原始内容保存在成果页的运行记录中") from None

    def _write(self, writer, budget, task, run_id, retry=True):
        task.pop("repair", None)
        attempts = 2 if retry else 1
        last = None
        for _ in range(attempts):
            self.token.raise_if_cancelled()
            try:
                return writer.process_task(task).data["content"]
            except (GenerationCancelled, RecordingError):
                raise
            except Exception as exc:
                last = exc
                if not budget.can_spend():
                    break
                # Format repair has the actual last nonempty candidate, not just an error label.
                calls = next(run for run in self.repo.load()["runs"] if run["id"] == run_id)["calls"]
                raw = next((c["response"] for c in reversed(calls) if c["response"]), "")
                detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                task["repair"] = f"\n上次未通过：{detail}\n完整上次返回：\n{raw}\n请纠正后输出完整 JSON。"
        raise ValueError("没有取得可解析的完整内容") from last

    def _review(self, reviewer, state, version):
        self.report("整篇审阅：连贯性、细节、语言、视频表达与节奏", 0.4)
        self.token.raise_if_cancelled()
        try:
            review = reviewer.process_task({"requirements": state["requirements"],
                        "outline": state["outline"]["content"], "script": version["content"]}).data["review"]
        except (GenerationCancelled, RecordingError):
            raise
        except Exception:
            return version  # Unavailable verdict is explicitly unchecked, never a pass.
        issues = version["issues"] + review["issues"]
        status = "reviewed" if passed(review, issues) else "needs_changes"
        return self.repo.annotate_version(version["id"], review, issues, status)

    def _select_best(self, run_id, versions, budget, status=None):
        best = max(versions, key=candidate_rank) if versions else None
        return self.repo.complete_run(run_id, best["id"] if best else None,
                                      status or (best["status"] if best else "failed"), budget.calls)

    def _finish(self, run_id, versions, budget, error=None):
        best = self._select_best(run_id, versions, budget)
        self.report("脚本候选已保存，可查看评审并人工定稿", 1.0)
        return {"status": best["status"], "run_id": run_id, "version_id": best["id"],
                "calls": budget.calls, "error": error}


class ScriptWork:
    """TaskRunner-compatible callable; snapshot inputs are loaded once inside the locked pipeline."""
    def __init__(self, output_dir, action, model, *, version_id=None, asks=None, token=None):
        self.output_dir, self.action, self.model = output_dir, action, model
        self.version_id, self.asks = version_id, deepcopy(asks)
        self.token = token or CancelToken()
        repository = ScriptRepository(output_dir)
        self.expected_source = repository.source(repository.load())

    def __call__(self, report):
        return ScriptPipeline(self.output_dir, self.model, token=self.token, report=report).run(
            self.action, version_id=self.version_id, asks=self.asks, expected_source=self.expected_source)
