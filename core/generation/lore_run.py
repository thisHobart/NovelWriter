"""世界设定的候选交付与可恢复运行记录。"""
from __future__ import annotations

from pathlib import Path
import json

from core.generation.lore_repository import LoreRepository, encode, digest, BODY, CONTRACT, CHARACTERS, FACTIONS
from core.generation.design_contract import DesignContractError

RESULTS = "system/lore_generation/results"
LABELS = {"characters": "人物", "factions": "势力", "world": "世界观", "background": "人物背景", "titles": "候选标题"}


def source_fingerprint(root, changes=None):
    files = LoreRepository(root)._files()
    files.update({path: text for path, text in (changes or {}).items() if path.startswith("story/lore/")})
    return digest(encode(dict(sorted(files.items()))))


def file_digests(files):
    """保存时每个文件的指纹；之后磁盘内容和它不同，就是作者手动改过。"""
    return {path: digest(str(text).replace("\r\n", "\n")) for path, text in files.items()}


def edited_after_save(root, receipt):
    """这份结果保存后作者有没有手动改过（编辑、改名都算）。撤回到保存时的样子就又不算了。

    旧收据没有记指纹，一律当没改过。
    """
    saved = receipt.get("file_digests")
    if receipt.get("disposition") != "current" or not saved:
        return False
    repo = LoreRepository(root)
    for path, value in saved.items():
        try:
            text = repo.path(path).read_text(encoding="utf-8")
        except (OSError, ValueError):
            return True
        if digest(text.replace("\r\n", "\n")) != value:
            return True
    return False


def product_key(receipt):
    """同一份产物的几次生成归到一起：人物背景按人物分，其余按种类。"""
    subject = receipt.get("subject") if isinstance(receipt.get("subject"), dict) else {}
    return receipt.get("kind"), str(subject.get("name", "")) if receipt.get("kind") == "background" else ""


def latest_by_product(receipts):
    """按产物分组，每组最新一次在前：[(最新收据, [更早的收据…]), …]，组按最新时间排。"""
    groups = {}
    for receipt in sorted(receipts, key=lambda r: r.get("created_at", 0), reverse=True):
        groups.setdefault(product_key(receipt), []).append(receipt)
    return [(items[0], items[1:]) for items in groups.values()]


def needs_attention(root, receipt):
    """这份最新结果还要不要作者处理：已完成、已撤回、保存后手动改过的都不用。"""
    if receipt.get("reverted") or receipt.get("status") == "complete":
        return False
    return not edited_after_save(root, receipt)


def attention_receipts(root, receipts=None):
    """每份产物最新那次结果里，还要作者处理的几份（按时间新的在前）。"""
    receipts = list_receipts(root) if receipts is None else receipts
    return [latest for latest, _earlier in latest_by_product([r for r in receipts if r.get("kind") in LABELS])
            if needs_attention(root, latest)]


#: 世界设定内部的生成先后，后面的按前面的写：(收据种类, 名称, 分步按钮)
LORE_ORDER = (("factions", "势力", "factions"), ("characters", "人物", "characters"),
              ("world", "世界观", "lore"), ("background", "人物背景", "enhance"))


def _generated_at(root, receipts):
    """每一项最近一次被采用的生成时间（撤回的不算）。人物背景只看现有主要人物的，取其中最旧的一份。"""
    from core.generation.lore_repository import current_backgrounds
    current = {relative.rsplit("/", 1)[-1] for relative in current_backgrounds(root)}
    times, backgrounds = {}, {}
    for receipt in receipts:
        if receipt.get("reverted") or receipt.get("disposition") != "current":
            continue
        kind, at = receipt.get("kind"), float(receipt.get("created_at") or 0)
        if kind == "background":
            name = str(receipt.get("target") or "").rsplit("/", 1)[-1]
            if name in current:
                backgrounds[name] = max(backgrounds.get(name, 0), at)
        elif kind in {"factions", "characters", "world"}:
            times[kind] = max(times.get(kind, 0), at)
    if backgrounds:
        times["background"] = min(backgrounds.values())
    return times


def stale_lore(root, receipts=None):
    """按生成先后找过期的几项：前面的某项比后面的新，后面的就是按旧内容写的。

    返回 {"source": 更新过的那一项, "stale": [过期的几项名称], "steps": [重写要跑的分步按钮]}；
    没有过期，或没有收据可推算（旧作品），返回 None。手动编辑不算，只看生成先后。
    """
    receipts = list_receipts(root) if receipts is None else receipts
    times = _generated_at(root, receipts)
    source, newest = None, 0.0
    for index, (kind, name, _step) in enumerate(LORE_ORDER):
        if kind not in times:
            continue
        if times[kind] < newest:
            later = LORE_ORDER[index:]
            return {"source": source, "stale": [n for k, n, _s in later if k in times],
                    "steps": [step for _k, _n, step in later]}
        source, newest = name, times[kind]
    return None


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
    except json.JSONDecodeError as exc:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise DesignContractError("返回内容不是可读取的 JSON") from exc
        try:
            data = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as inner:
            raise DesignContractError("返回内容不是可读取的 JSON") from inner
    if not isinstance(data, (list, dict)):
        raise DesignContractError("返回内容必须是对象或数组")
    items = data if isinstance(data, list) else data.get(key)
    if not isinstance(items, list) or not items or any(not isinstance(r, dict) for r in items):
        raise DesignContractError("没有可读取的条目列表")
    if any(not isinstance(r.get("name"), str) or not r["name"].strip() for r in items):
        raise DesignContractError("条目缺少可读取的名称")
    if key == "characters":
        relationships = data.get("relationships", []) if isinstance(data, dict) else []
        if not isinstance(relationships, list) or any(not isinstance(r, dict) for r in relationships):
            raise DesignContractError("人物关系不是可读取的对象数组")
        return {"characters": items, "relationships": relationships,
                **({k: v for k, v in data.items() if k not in {"characters", "relationships"}} if isinstance(data, dict) else {})}
    return items


def readable_world(raw):
    from core.generation.design_contract import LORE_CONTRACT_START, LORE_CONTRACT_END
    body = raw.split(LORE_CONTRACT_START, 1)[0].strip()
    if not body:
        raise DesignContractError("没有可读取的世界观正文")
    contract = None
    if LORE_CONTRACT_START in raw and LORE_CONTRACT_END in raw:
        try:
            value = json.loads(raw.split(LORE_CONTRACT_START, 1)[1].split(LORE_CONTRACT_END, 1)[0].strip())
            from core.generation.design_contract import validate_lore_contract
            validate_lore_contract(value)
            contract = value
        except (json.JSONDecodeError, DesignContractError):
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
        before_state = repo._state()
        updated = dict(before, **files)
        metadata = repo._revision(repo._metadata(), updated, list(files), ["草稿已手动采用，请核对生成问题。"], True)
        from core.generation.lore_repository import META
        receipt.update(candidate=raw, status="needs_changes", exit_reason="manual_adoption", files=list(files),
                       source_fingerprint=source_fingerprint(root, files), disposition="current",
                       file_digests=file_digests(files), remaining=[])
        repo._transaction({**files, META: encode(metadata), f"{RESULTS}/{identity}.json": encode(receipt)}, "adopt_generation_draft")
        repo.record_change("采用草稿：" + LABELS.get(kind, "设定"), before_state, [identity])
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
