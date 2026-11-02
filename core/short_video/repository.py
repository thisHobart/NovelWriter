"""Atomic, versioned project storage. Constructors and reads create no files."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from filelock import Timeout
from core.generation.locks import project_lock
from core.short_video.models import ScriptRequirements, encode, fingerprint
from core.short_video.validation import validate_story, local_issues, to_markdown


def now():
    return datetime.now(timezone.utc).isoformat()


class ScriptRepository:
    def __init__(self, output_dir):
        self.root = Path(output_dir).resolve() / "short_video"
        self.path = self.root / "project.json"

    def load(self):
        if not self.path.exists():
            return {"schema_version": 1, "requirements": None, "outline": None,
                    "versions": [], "runs": [], "selected_version": None}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("不支持的短视频项目格式")
        if (not {"requirements", "outline", "versions", "runs", "selected_version"} <= value.keys()
                or not isinstance(value["versions"], list) or not isinstance(value["runs"], list)):
            raise ValueError("短视频项目数据不完整，请从备份恢复；当前文件未被修改")
        try:
            self._validate_state(value)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError("短视频项目内容损坏或不完整，请从备份恢复；当前文件未被修改") from exc
        return value

    @staticmethod
    def _validate_state(state):
        """Validate persisted structures before a UI can consume them.

        Old artifacts may exceed newly edited requirements, so validate their
        structure against the supported limits rather than today's project limits.
        """
        limits = {"character_limit": 8, "location_limit": 6}

        def text(value):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("缺少文字字段")

        def outline(value):
            if value is not None:
                validate_story(value["content"], limits, "outline")
                text(value["requirements_hash"])
                text(value["created_at"])
                if type(value["confirmed"]) is not bool:
                    raise ValueError("大纲确认状态无效")

        def issues(rows):
            if not isinstance(rows, list):
                raise ValueError("问题列表无效")
            for row in rows:
                for key in ("code", "suggestion"):
                    text(row[key])
                if (not isinstance(row["quote"], str) or row["severity"] not in {"hard", "soft"}
                        or row["target"] not in {"script", "outline"}):
                    raise ValueError("问题条目无效")

        if state["requirements"] is not None:
            ScriptRequirements.from_dict(state["requirements"])
            if not ScriptRequirements.__dataclass_fields__.keys() <= state["requirements"].keys():
                raise ValueError("需求字段不完整")
        outline(state["outline"])
        identities = set()
        for version in state["versions"]:
            text(version["id"])
            if version["id"] in identities:
                raise ValueError("版本编号重复")
            identities.add(version["id"])
            validate_story(version["content"], limits, "script")
            text(version["source"])
            text(version["created_at"])
            if (version["origin"] not in {"manual", "model"}
                    or version["status"] not in {"unchecked", "needs_changes", "reviewed"}
                    or type(version["finalized"]) is not bool):
                raise ValueError("版本状态无效")
            issues(version["issues"])
            if version["review"] is not None:
                from core.short_video.validation import validate_review
                validate_review(version["review"], version["content"])
            elif version["status"] == "reviewed":
                raise ValueError("已审阅版本缺少评审")
        if state["selected_version"] is not None and state["selected_version"] not in identities:
            raise ValueError("当前版本不存在")
        run_ids = set()
        for run in state["runs"]:
            text(run["id"])
            if run["id"] in run_ids:
                raise ValueError("运行编号重复")
            run_ids.add(run["id"])
            if (run["action"] not in {"outline", "script", "revise"}
                    or run["status"] not in {"running", "interrupted", "complete", "failed", "cancelled",
                                             "unchecked", "needs_changes", "reviewed"}):
                raise ValueError("运行状态无效")
            ScriptRequirements.from_dict(run["snapshot"]["requirements"])
            outline(run["snapshot"]["outline"])
            if not isinstance(run["calls"], list):
                raise ValueError("请求列表无效")
            for call in run["calls"]:
                if not isinstance(call["prompt"], str) or not isinstance(call["response"], str):
                    raise ValueError("请求记录无效")

    @staticmethod
    def atomic(path: Path, text: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                             dir=path.parent, prefix=".script-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    def update(self, operation):
        self.root.mkdir(parents=True, exist_ok=True)
        with project_lock(str(self.root / ".project.lock"), timeout=5):
            state = self.load()
            result = operation(state)
            self.atomic(self.path, encode(state))
            return deepcopy(result)

    @contextmanager
    def generation_lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with project_lock(str(self.root / ".generation.lock"), timeout=0):
            yield

    def recover(self):
        if not self.path.exists():
            return
        try:
            with self.generation_lock():
                if any(run["status"] == "running" for run in self.load()["runs"]):
                    def repair(state):
                        for run in state["runs"]:
                            if run["status"] == "running":
                                run.update(status="interrupted", finished_at=now())
                    self.update(repair)
        except Timeout:
            pass  # A different process is still generating; do not claim interruption.

    def save_requirements(self, value, *, expected=None):
        clean = ScriptRequirements.from_dict(value).to_dict()
        def save(state):
            if expected is not None and fingerprint(state["requirements"]) != expected:
                raise ValueError("需求已被其他窗口修改，请重新载入后再保存；当前编辑仍保留")
            state["requirements"] = clean
            return clean
        return self.update(save)

    def save_outline(self, value, *, source=None, expected=None):
        def save(state):
            requirements = state["requirements"]
            if requirements is None:
                raise ValueError("请先保存创作需求")
            if source and fingerprint(requirements) != source:
                raise ValueError("需求已改变，大纲候选已保留在运行记录，请重新生成")
            if expected is not None and fingerprint(state["outline"]) != expected:
                raise ValueError("大纲已被其他窗口修改，请重新载入")
            validate_story(value, requirements, "outline")
            current = state["outline"]
            if current and current["content"] == value and current["requirements_hash"] == fingerprint(requirements):
                return current
            state["outline"] = {"content": deepcopy(value), "requirements_hash": fingerprint(requirements),
                                "confirmed": False, "created_at": now()}
            return state["outline"]
        return self.update(save)

    @staticmethod
    def source(state):
        return fingerprint({"requirements": state["requirements"], "outline": state["outline"]})

    @staticmethod
    def outline_ready(state):
        outline = state["outline"]
        return bool(outline and outline["confirmed"] and state["requirements"]
                    and outline["requirements_hash"] == fingerprint(state["requirements"]))

    def confirm_outline(self, *, expected=None):
        def confirm(state):
            outline = state["outline"]
            if expected is not None and fingerprint(outline) != expected:
                raise ValueError("大纲已被其他窗口修改，请重新载入并检查后再确认")
            if not outline or outline["requirements_hash"] != fingerprint(state["requirements"]):
                raise ValueError("大纲不存在或需求已变化，请重新生成或保存大纲")
            validate_story(outline["content"], state["requirements"], "outline")
            if local_issues(outline["content"], state["requirements"]):
                raise ValueError("大纲预计时长偏离目标，请先调整")
            outline["confirmed"] = True
            return outline
        return self.update(confirm)

    def start_run(self, action, snapshot, model):
        run = {"id": uuid4().hex, "action": action, "status": "running", "model": model,
               "started_at": now(), "snapshot": {"requirements": deepcopy(snapshot["requirements"]),
                                                   "outline": deepcopy(snapshot["outline"])},
               "calls": [], "selected_version": None}
        self.update(lambda state: state["runs"].append(run))
        return run["id"]

    def record_call(self, run_id, prompt, response, error=None):
        def save(state):
            run = next(run for run in state["runs"] if run["id"] == run_id)
            run["calls"].append({"prompt": prompt, "response": response, "error": error, "at": now()})
        self.update(save)

    def finish_run(self, run_id, status, **extra):
        def finish(state):
            run = next(run for run in state["runs"] if run["id"] == run_id)
            run.update(status=status, finished_at=now(), **extra)
        self.update(finish)

    @staticmethod
    def _new_version(content, source, *, run_id=None, review=None, issues=None,
                     status="unchecked", origin="model"):
        return {"id": uuid4().hex, "content": deepcopy(content), "source": source,
                   "run_id": run_id, "review": review, "issues": issues or [], "status": status,
                   "origin": origin, "created_at": now(), "finalized": False}

    def add_version(self, content, source, **options):
        version = self._new_version(content, source, **options)
        self.update(lambda state: state["versions"].append(version))
        return version

    def annotate_version(self, version_id, review, issues, status):
        def change(state):
            version = next(v for v in state["versions"] if v["id"] == version_id)
            version.update(review=review, issues=issues, status=status)
            return version
        return self.update(change)

    def select_version(self, version_id):
        def select(state):
            if not any(v["id"] == version_id for v in state["versions"]):
                raise ValueError("脚本版本不存在")
            state["selected_version"] = version_id
        self.update(select)

    def version(self, version_id=None):
        state = self.load()
        identity = version_id or state["selected_version"]
        return next((v for v in state["versions"] if v["id"] == identity), None)

    def save_manual(self, content, source):
        def save(state):
            if not self.outline_ready(state) or source != self.source(state):
                raise ValueError("创作依据已变化，当前编辑保留，请确认新大纲后再保存")
            validate_story(content, state["requirements"], "script")
            version = self._new_version(content, source, origin="manual",
                                        issues=local_issues(content, state["requirements"]))
            state["versions"].append(version)
            state["selected_version"] = version["id"]
            return version
        return self.update(save)

    def complete_run(self, run_id, best_id, status, calls):
        """Choose the candidate and finish under one lock, preserving author selection."""
        def finish(state):
            best = next((v for v in state["versions"] if v["id"] == best_id), None)
            selected = next((v for v in state["versions"] if v["id"] == state["selected_version"]), None)
            if best and (not selected or (selected["origin"] != "manual" and not selected["finalized"])):
                state["selected_version"] = best_id
            run = next(r for r in state["runs"] if r["id"] == run_id)
            run.update(status=status, finished_at=now(), selected_version=best_id, calls_used=calls)
            return best
        return self.update(finish)

    def finalize(self, version_id):
        def finish(state):
            version = next(v for v in state["versions"] if v["id"] == version_id)
            if not self.outline_ready(state) or version["source"] != self.source(state):
                raise ValueError("脚本依据已过期，不能定稿")
            validate_story(version["content"], state["requirements"], "script")
            if local_issues(version["content"], state["requirements"]):
                raise ValueError("预计时长偏离目标，请先调整")
            version["finalized"] = True
            version["finalized_at"] = now()
            state["selected_version"] = version_id
        self.update(finish)

    def export(self, version_id, path):
        version = self.version(version_id)
        if version is None:
            raise ValueError("请选择脚本版本")
        target = Path(path).resolve()
        if target == self.root or self.root in target.parents:
            raise ValueError("请导出到 short_video 数据目录之外，避免覆盖项目文件")
        state = self.load()
        label = "已定稿" if version["finalized"] else "候选稿"
        if version["source"] != self.source(state):
            label += " · 创作依据已过期"
        self.atomic(target, f"> {label}；模型评审状态：{version['status']}\n\n" + to_markdown(version["content"]))
        return str(target)
