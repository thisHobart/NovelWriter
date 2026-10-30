"""世界设定的请求预算、候选交付与可恢复运行记录。"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
import json
import time

from core.generation.lore_repository import LoreRepository, encode, digest, BODY, CONTRACT, CHARACTERS, FACTIONS

ACTIVE_BUDGET = ContextVar("lore_request_budget", default=None)
IN_REQUEST = ContextVar("lore_inside_request", default=False)
RESULTS = "system/lore_generation/results"
LABELS = {"characters": "人物", "factions": "势力", "world": "世界观", "background": "人物背景", "titles": "推荐标题"}


class BudgetExhausted(RuntimeError):
    pass


@dataclass
class RequestBudget:
    progress: object = None
    max_calls: int = 6
    seconds: float = 300
    clock: object = time.monotonic
    calls: int = 0
    started: float = field(init=False)

    def __post_init__(self):
        self.started = self.clock()

    @property
    def remaining(self):
        return self.seconds - (self.clock() - self.started)

    def check(self):
        if self.progress:
            self.progress()
        if self.calls >= self.max_calls:
            raise BudgetExhausted(f"已达到 {self.max_calls} 次模型请求预算")
        if self.remaining <= 0:
            raise BudgetExhausted("已达到单份产物 5 分钟的总预算")

    @contextmanager
    def activate(self):
        token = ACTIVE_BUDGET.set(self)
        try:
            yield
        finally:
            ACTIVE_BUDGET.reset(token)


def budgeted_call(invoke):
    """SDK 内部重试关闭后，每次底层请求只在此计数一次。"""
    budget = ACTIVE_BUDGET.get()
    if budget is None or IN_REQUEST.get():
        return invoke()
    budget.check()
    budget.calls += 1
    token = IN_REQUEST.set(True)
    try:
        return invoke()
    finally:
        IN_REQUEST.reset(token)


def source_fingerprint(root, changes=None):
    files = LoreRepository(root)._files()
    files.update({path: text for path, text in (changes or {}).items() if path.startswith("story/lore/")})
    return digest(encode(dict(sorted(files.items()))))


def save_receipt(root, receipt):
    path = LoreRepository(root).path(f"{RESULTS}/{receipt['id']}.json")
    LoreRepository._atomic(path, encode(receipt).encode("utf-8"))


def load_receipt(root, identity):
    # 记录 ID 只允许本服务产生的十六进制标识，不能作为任意路径入口。
    if not identity or any(c not in "0123456789abcdef" for c in identity):
        raise ValueError("生成记录编号无效")
    return json.loads(LoreRepository(root).path(f"{RESULTS}/{identity}.json").read_text(encoding="utf-8"))


def list_receipts(root):
    folder = Path(root) / RESULTS
    records = []
    for path in sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(record, dict) and all(k in record for k in ("id", "kind", "status", "candidate")):
                records.append(record)
        except (OSError, ValueError):
            continue
    return records


def recover_interrupted(root):
    """只有取得项目锁后才能判断运行已中断，避免误处理另一进程的任务。"""
    if not (Path(root) / RESULTS).exists():
        return
    repo = LoreRepository(root)
    with repo.lock():
        repo._recover()
        for record in list_receipts(root):
            if record["status"] == "running" or (record["status"] in {"complete", "needs_changes", "unchecked"} and record.get("disposition") == "draft"):
                record.update(status="draft", exit_reason="interrupted", disposition="draft",
                              issues=["上次生成未正常结束；已返回内容保留为草稿，当前有效设定未被草稿替换。"])
                save_receipt(root, record)


def readable_json(raw, key):
    import re
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", raw.strip(), flags=re.I)
    try:
        data = json.loads(cleaned)
    except ValueError:
        data = json.loads(cleaned[cleaned.index("{"):cleaned.rindex("}") + 1])
    items = data if isinstance(data, list) else data.get(key)
    if not isinstance(items, list) or not items or any(not isinstance(r, dict) for r in items):
        raise ValueError("没有可读取的条目列表")
    if any(not isinstance(r.get("name"), str) or not r["name"].strip() for r in items):
        raise ValueError("条目缺少可读取的名称")
    if key == "characters":
        relationships = data.get("relationships", []) if isinstance(data, dict) else []
        if not isinstance(relationships, list) or any(not isinstance(r, dict) for r in relationships):
            raise ValueError("人物关系不是可读取的对象数组")
        return {"characters": items, "relationships": relationships,
                **({k: v for k, v in data.items() if k not in {"characters", "relationships"}} if isinstance(data, dict) else {})}
    return items


def readable_world(raw):
    from core.generation.design_contract import LORE_CONTRACT_START, LORE_CONTRACT_END
    body = raw.split(LORE_CONTRACT_START, 1)[0].strip()
    if not body:
        raise ValueError("没有可读取的世界观正文")
    contract = None
    if LORE_CONTRACT_START in raw and LORE_CONTRACT_END in raw:
        try:
            value = json.loads(raw.split(LORE_CONTRACT_START, 1)[1].split(LORE_CONTRACT_END, 1)[0].strip())
            from core.generation.design_contract import validate_lore_contract
            validate_lore_contract(value)
            contract = value
        except (ValueError, TypeError, KeyError):
            pass
    return body, contract


def target_files(kind, value, target=None):
    if kind == "characters":
        return {CHARACTERS: encode(value)}
    if kind == "factions":
        from core.generation.lore_cast import storage_factions
        return {FACTIONS: encode(storage_factions(value))}
    if kind == "world":
        body, contract = value
        return {BODY: body, **({CONTRACT: encode(contract)} if contract is not None else {})}
    return {target: value}


def adopt_draft(root, identity, raw, *, expected_fingerprint=None, expected_receipt=None):
    """离线编辑草稿并采用；只要求基础数据可读，规则问题继续提示。"""
    repo = LoreRepository(root)
    with repo.lock():
        repo._recover()
        receipt = load_receipt(root, identity)
        if expected_receipt is not None and digest(encode(receipt)) != expected_receipt:
            raise ValueError("生成记录已被其他窗口修改；当前编辑内容保留，请重新打开后核对")
        if source_fingerprint(root) != (expected_fingerprint or receipt["source_fingerprint"]):
            raise ValueError("设定已在草稿编辑期间变化；草稿保留，请重新打开后核对")
        kind = receipt["kind"]
        value = readable_json(raw, kind) if kind in {"characters", "factions"} else readable_world(raw) if kind == "world" else raw.strip()
        if not value:
            raise ValueError("草稿不能为空")
        files = target_files(kind, value, receipt.get("target"))
        if not files or None in files:
            raise ValueError("草稿缺少保存目标")
        before = repo._files()
        updated = dict(before, **files)
        metadata = repo._revision(repo._metadata(), updated, list(files), ["草稿已手动采用，请核对生成问题。"], True)
        from core.generation.lore_repository import META
        receipt.update(candidate=raw, status="needs_changes", exit_reason="manual_adoption", files=list(files),
                       source_fingerprint=source_fingerprint(root, files), disposition="current")
        repo._transaction({**files, META: encode(metadata), f"{RESULTS}/{identity}.json": encode(receipt)}, "adopt_generation_draft")
        return receipt


def save_draft(root, identity, raw, *, expected_receipt=None):
    with LoreRepository(root).lock():
        receipt = load_receipt(root, identity)
        if expected_receipt is not None and digest(encode(receipt)) != expected_receipt:
            raise ValueError("生成记录已被其他窗口修改；当前编辑内容保留，请重新打开后核对")
        receipt.update(candidate=raw, status="draft", disposition="draft", exit_reason="manual_draft")
        save_receipt(root, receipt)
        return receipt


def current_content(root, receipt):
    if receipt.get("disposition") != "current":
        return receipt.get("candidate", "")
    repo = LoreRepository(root)
    files = {p: repo.path(p).read_text(encoding="utf-8") for p in receipt.get("files", []) if repo.path(p).exists()}
    if receipt["kind"] == "world" and BODY in files:
        from core.generation.design_contract import LORE_CONTRACT_START, LORE_CONTRACT_END
        contract = repo.path(CONTRACT)
        return files[BODY] + ("\n" + LORE_CONTRACT_START + "\n" + contract.read_text(encoding="utf-8") + LORE_CONTRACT_END if contract.exists() else "")
    return "\n".join(files.values()) or receipt.get("candidate", "")


def receipt_changes_for_edits(root, before, after):
    """人物改名/换身份时，背景操作入口跟随当前文件；原始候选仍留作诊断。"""
    if before.get(CHARACTERS) == after.get(CHARACTERS):
        return {}
    from core.generation.lore_repository import records, background_name
    try:
        old = records(json.loads(before.get(CHARACTERS, "{}")), "characters")
        new = records(json.loads(after.get(CHARACTERS, "{}")), "characters")
    except (ValueError, TypeError):
        return {}
    moves = {"story/lore/" + background_name(a): ("story/lore/" + background_name(b), a, b)
             for a, b in zip(old, new) if background_name(a) != background_name(b)}
    changes = {}
    for receipt in list_receipts(root):
        target = receipt.get("target")
        if receipt["kind"] != "background" or target not in moves:
            continue
        destination, original, current = moves[target]
        receipt.setdefault("original_subject", receipt.get("subject"))
        receipt.update(subject=current, target=destination,
                       files=[destination if path == target else path for path in receipt.get("files", [])])
        receipt.setdefault("issues", []).append(f"人物身份或名称已修改为「{current.get('name', '')}」，原始候选中的旧称请人工核对。")
        changes[f"{RESULTS}/{receipt['id']}.json"] = encode(receipt)
    return changes
