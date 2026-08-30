"""Conservatively reconcile LLM-generated contract records with prior IDs.

The language model only classifies meaning.  This module owns every mutation:
it validates the model's small JSON reply, applies confidence thresholds, keeps
canonical IDs, and records an audit trail.  Creative Markdown is never sent
back for rewriting here.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import os
import re
import tempfile
from typing import Any, Callable, Dict, Iterable, List, Optional

from core.generation.chapter_acceptance import values_conflict
from core.generation.domain_profiles import GENERAL, DomainProfile
from core.generation.planning_contract import load_planning_contracts


PROMPT_VERSION = 1
SAME_THRESHOLD = 0.86
VALID_RELATIONS = {"same", "related", "different", "uncertain"}


@dataclass
class SemanticResolution:
    contract: Dict[str, Any]
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _record_text(record: Dict[str, Any]) -> str:
    keys = (
        "fact", "value", "thread", "event", "time", "location_id",
        "character", "attribute", "reason", "new_value",
        "surface_meaning", "true_meaning", "item", "origin", "type",
    )
    return " ".join(str(record.get(key, "")) for key in keys if record.get(key))


def _candidate_score(record: Dict[str, Any], candidate: Dict[str, Any]) -> int:
    left = set(re.sub(r"\s+", "", _record_text(record)))
    right = set(re.sub(r"\s+", "", _record_text(candidate)))
    return len(left & right)


def _limited_candidates(
    record: Dict[str, Any], candidates: Iterable[Dict[str, Any]], limit: int = 8
) -> List[Dict[str, Any]]:
    unique: Dict[str, Dict[str, Any]] = {}
    for candidate in candidates:
        candidate_id = str(candidate.get("id", "")).strip()
        if candidate_id:
            unique[candidate_id] = candidate
    return sorted(
        unique.values(),
        key=lambda item: _candidate_score(record, item),
        reverse=True,
    )[:limit]


def _parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    if not isinstance(text, str):
        return None
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text, flags=re.I)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _normalise_decision(
    payload: Optional[Dict[str, Any]], candidate_ids: set[str]
) -> Dict[str, Any]:
    if not payload:
        return {
            "relation": "uncertain", "similarity": 0.0,
            "matched_id": None, "reason": "模型没有返回合法 JSON",
        }
    relation = str(payload.get("relation", "uncertain")).strip().lower()
    if relation not in VALID_RELATIONS:
        relation = "uncertain"
    try:
        similarity = max(0.0, min(1.0, float(payload.get("similarity", 0.0))))
    except (TypeError, ValueError):
        similarity = 0.0
        relation = "uncertain"
    matched_id = payload.get("matched_id")
    matched_id = str(matched_id).strip() if matched_id is not None else None
    if matched_id not in candidate_ids:
        matched_id = None
        if relation == "same":
            relation = "uncertain"
    return {
        "relation": relation,
        "similarity": similarity,
        "matched_id": matched_id,
        "reason": str(payload.get("reason", "")).strip()[:500],
    }


def _ask_model(
    record_type: str,
    record: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    model: str,
    send_prompt_fn: Callable[..., str],
) -> Dict[str, Any]:
    candidate_view = [
        {key: value for key, value in item.items() if not str(key).startswith("_")}
        for item in candidates
    ]
    prompt = f"""你只负责判断小说规划记录的语义身份，不创作或改写故事。

记录类型：{record_type}
待判断的新记录：
{json.dumps(record, ensure_ascii=False, indent=2)}

已有候选记录：
{json.dumps(candidate_view, ensure_ascii=False, indent=2)}

判断新记录与哪一条候选记录描述的是同一个需要持续追踪的事实、事件、人物属性或情节线。
- same：措辞可不同，但确实是同一个追踪对象。
- related：有关联或属于同一主题，但仍是两个不同对象。
- different：不是同一个对象。
- uncertain：信息不足，无法可靠判断。

只返回一个 JSON 对象，不要代码围栏或解释：
{{"relation":"same|related|different|uncertain","similarity":0到1之间的小数,"matched_id":"候选ID或null","reason":"一句简短理由"}}
只有 relation 为 same 时 matched_id 才能填写候选 ID；不要因为用词相近就判为 same。"""
    try:
        response = send_prompt_fn(prompt, model=model)
    except Exception as exc:
        return {
            "relation": "uncertain",
            "similarity": 0.0,
            "matched_id": None,
            "reason": f"语义判断调用失败：{exc}",
        }
    payload = _parse_json_object(response)
    if payload is None:
        format_prompt = prompt + (
            "\n\n上一次回复格式无效。不要重新分析，只把结论修复成指定的单个合法 JSON 对象。"
        )
        try:
            payload = _parse_json_object(send_prompt_fn(format_prompt, model=model))
        except Exception:
            payload = None
    return _normalise_decision(payload, {str(item["id"]) for item in candidates})


def _audit(output_dir: str, chapter: int, decisions: List[Dict[str, Any]]) -> None:
    if not decisions:
        return
    directory = os.path.join(
        output_dir, "system", "story_ledgers", "semantic_matches"
    )
    os.makedirs(directory, exist_ok=True)
    payload = {
        "prompt_version": PROMPT_VERSION,
        "chapter": chapter,
        "created_at": datetime.now().isoformat(),
        "decisions": decisions,
    }
    digest = hashlib.sha256(
        json.dumps(payload["decisions"], ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    path = os.path.join(directory, f"chapter_{chapter}_{digest}.json")
    handle, temp_path = tempfile.mkstemp(prefix=".semantic_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def _prior_records(contracts: Iterable[Dict[str, Any]], field_name: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for contract in contracts:
        chapter = int(contract.get("chapter", 0))
        for raw in contract.get(field_name, []):
            item = deepcopy(raw)
            item["_chapter"] = chapter
            records.append(item)
    return records


def _open_threads(contracts: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    states: Dict[str, Dict[str, Any]] = {}
    for contract in contracts:
        chapter = int(contract.get("chapter", 0))
        for raw in contract.get("plot_thread_updates", []):
            item = deepcopy(raw)
            item["_chapter"] = chapter
            states[str(item.get("id", ""))] = item
    return [item for item in states.values() if item.get("status") == "open"]


def _attribute_key(record: Dict[str, Any]) -> str:
    """Compound identity for a character attribute, insensitive to spacing."""
    character = re.sub(r"\s+", "", str(record.get("character", "")))
    attribute = re.sub(r"\s+", "", str(record.get("attribute", "")))
    return f"{character}::{attribute}" if character and attribute else ""


def _same_text(left: Any, right: Any) -> bool:
    return re.sub(r"\s+", "", str(left or "")) == re.sub(r"\s+", "", str(right or ""))


def _new_record_id(prefix: str, chapter_number: int, used_ids: set[str]) -> str:
    sequence = 1
    while True:
        candidate = f"{prefix}-{chapter_number:03d}-{sequence:02d}"
        if candidate not in used_ids:
            return candidate
        sequence += 1


def _id_prefix(record_id: str, fallback: str) -> str:
    match = re.match(r"([A-Za-z]+)", str(record_id or ""))
    return (match.group(1) if match else fallback).upper()


def has_domain_id_collision(
    contract: Dict[str, Any],
    output_dir: str,
    chapter_number: int,
    profile: Optional[DomainProfile] = None,
) -> bool:
    """Whether a tracked domain record reuses an ID from an earlier chapter."""
    profile = profile or GENERAL
    prior = [
        item for item in load_planning_contracts(output_dir)
        if int(item.get("chapter", 0)) < int(chapter_number)
    ]
    for spec in profile.contract_fields:
        if spec.delta_slot not in {"clue_updates", "evidence_updates"} or not spec.is_list:
            continue
        prior_ids = {
            str(item.get("id", ""))
            for item in _prior_records(prior, spec.name)
            if item.get("id")
        }
        if any(
            str(item.get("id", "")) in prior_ids
            for item in contract.get(spec.name, [])
            if isinstance(item, dict) and item.get("id")
        ):
            return True
    return False


def _resolve_domain_fields(
    resolved: Dict[str, Any],
    prior: List[Dict[str, Any]],
    chapter_number: int,
    model: str,
    send_prompt_fn: Callable[..., str],
    profile: DomainProfile,
    decisions: List[Dict[str, Any]],
) -> None:
    """Normalise stable IDs for profile-defined clue/evidence streams."""
    for spec in profile.contract_fields:
        if spec.delta_slot not in {"clue_updates", "evidence_updates"} or not spec.is_list:
            continue
        records = _prior_records(prior, spec.name)
        if not records or not isinstance(resolved.get(spec.name, []), list):
            continue
        records_by_id = {str(item.get("id")): item for item in records if item.get("id")}
        used_ids = set(records_by_id) | {
            str(item.get("id", ""))
            for item in resolved.get(spec.name, [])
            if isinstance(item, dict) and item.get("id")
        }
        identity_fields = spec.identity_fields or spec.immutable_fields[:1]
        fallback_prefix = "C" if spec.delta_slot == "clue_updates" else "E"
        normalised_records: List[Dict[str, Any]] = []

        for record in resolved.get(spec.name, []):
            if not isinstance(record, dict):
                continue
            original = deepcopy(record)
            original_id = str(record.get("id", ""))
            matched = records_by_id.get(original_id)
            decision: Optional[Dict[str, Any]] = None
            identity_changed = bool(
                matched
                and identity_fields
                and any(
                    not _same_text(record.get(name), matched.get(name))
                    for name in identity_fields
                )
            )

            if matched is None or identity_changed:
                candidates = _limited_candidates(record, records)
                if candidates:
                    decision = _ask_model(
                        f"领域记录 {spec.name}", record, candidates, model, send_prompt_fn
                    )
                    if (
                        decision["relation"] == "same"
                        and decision["similarity"] >= SAME_THRESHOLD
                    ):
                        matched = records_by_id.get(str(decision["matched_id"]))
                    elif identity_changed:
                        matched = None

            if matched is not None:
                record["id"] = matched["id"]
                for name in dict.fromkeys((*identity_fields, *spec.immutable_fields)):
                    if matched.get(name) not in (None, ""):
                        record[name] = deepcopy(matched[name])
                decisions.append(
                    {
                        "type": spec.delta_slot,
                        "field": spec.name,
                        "action": "canonicalized",
                        "original": original,
                        "canonical_id": matched["id"],
                        "model_decision": decision,
                    }
                )
            elif original_id in records_by_id:
                prefix = _id_prefix(original_id, fallback_prefix)
                new_id = _new_record_id(prefix, chapter_number, used_ids)
                used_ids.add(new_id)
                record["id"] = new_id
                decisions.append(
                    {
                        "type": spec.delta_slot,
                        "field": spec.name,
                        "action": "colliding_id_reassigned",
                        "original": original,
                        "new_id": new_id,
                        "model_decision": decision,
                    }
                )
            elif decision:
                decisions.append(
                    {
                        "type": spec.delta_slot,
                        "field": spec.name,
                        "action": "kept",
                        "original": original,
                        "model_decision": decision,
                    }
                )
            normalised_records.append(record)
        resolved[spec.name] = normalised_records


def resolve_domain_identities(
    contract: Dict[str, Any],
    output_dir: str,
    chapter_number: int,
    model: str,
    send_prompt_fn: Callable[..., str],
    profile: Optional[DomainProfile] = None,
) -> SemanticResolution:
    """Resolve only domain-field collisions, avoiding unrelated model calls."""
    resolved = deepcopy(contract)
    prior = [
        item for item in load_planning_contracts(output_dir)
        if int(item.get("chapter", 0)) < int(chapter_number)
    ]
    decisions: List[Dict[str, Any]] = []
    _resolve_domain_fields(
        resolved,
        prior,
        chapter_number,
        model,
        send_prompt_fn,
        profile or GENERAL,
        decisions,
    )
    _audit(output_dir, chapter_number, decisions)
    return SemanticResolution(resolved, decisions, [])


def resolve_contract_identities(
    contract: Dict[str, Any],
    output_dir: str,
    chapter_number: int,
    model: str,
    send_prompt_fn: Callable[..., str],
    profile: Optional[DomainProfile] = None,
) -> SemanticResolution:
    """Return a copy whose references use prior canonical IDs when safe."""
    resolved = deepcopy(contract)
    all_contracts = load_planning_contracts(output_dir)
    prior = [
        item for item in all_contracts
        if int(item.get("chapter", 0)) < int(chapter_number)
    ]
    # Replanning a chapter that already has planned successors must not delete a
    # thread they close, however duplicated it looks against earlier chapters.
    protected_thread_ids = {
        str(record.get("id", ""))
        for item in all_contracts
        if int(item.get("chapter", 0)) > int(chapter_number)
        for record in item.get("plot_thread_updates", [])
        if str(record.get("status", "")).lower() == "closed"
        and record.get("opened_in_chapter") is not True
    }
    decisions: List[Dict[str, Any]] = []
    warnings: List[str] = []
    profile = profile or GENERAL

    facts = _prior_records(prior, "facts_added")
    facts_by_id = {str(item.get("id")): item for item in facts}
    used_fact_ids = set(facts_by_id) | {
        str(item.get("id", ""))
        for field_name in ("facts_added", "facts_confirmed", "facts_contradicted")
        for item in resolved.get(field_name, [])
    }
    for field_name in ("facts_added", "facts_confirmed", "facts_contradicted"):
        kept: List[Dict[str, Any]] = []
        for record in resolved.get(field_name, []):
            original = deepcopy(record)
            record_id = str(record.get("id", ""))
            matched = facts_by_id.get(record_id)
            decision: Optional[Dict[str, Any]] = None
            if (
                matched is not None
                and field_name == "facts_added"
                and not _same_text(record.get("fact"), matched.get("fact"))
            ):
                candidates = _limited_candidates(record, facts)
                decision = _ask_model("事实", record, candidates, model, send_prompt_fn)
                if not (
                    decision["relation"] == "same"
                    and decision["similarity"] >= SAME_THRESHOLD
                    and decision["matched_id"] == matched.get("id")
                ):
                    matched = None
            elif matched is None and facts:
                candidates = _limited_candidates(record, facts)
                decision = _ask_model("事实", record, candidates, model, send_prompt_fn)
                if decision["relation"] == "same" and decision["similarity"] >= SAME_THRESHOLD:
                    matched = facts_by_id.get(str(decision["matched_id"]))
            if matched is not None:
                if field_name == "facts_added" and values_conflict(
                    matched.get("value"), record.get("value")
                ):
                    # 模型沿用同一个 id 时两侧 id 相同，只报 id 等于什么都没说；
                    # 真正需要人看到的是两个互相打架的取值。
                    warnings.append(
                        f"事实 {matched.get('id')}（{matched.get('fact')}）取值冲突："
                        f"既有「{matched.get('value')}」，本章声明「{record.get('value')}」，未自动合并"
                    )
                else:
                    record["id"] = matched["id"]
                    if field_name == "facts_added":
                        resolved.setdefault("facts_confirmed", []).append(record)
                        decisions.append({
                            "type": "fact", "action": "added_to_confirmed",
                            "original": original, "canonical_id": matched["id"],
                            "model_decision": decision,
                        })
                        continue
            if matched is None and record_id in facts_by_id:
                new_id = _new_record_id("F", chapter_number, used_fact_ids)
                used_fact_ids.add(new_id)
                record["id"] = new_id
                decisions.append({
                    "type": "fact", "action": "colliding_id_reassigned",
                    "original": original, "new_id": new_id,
                    "model_decision": decision,
                })
            elif decision:
                decisions.append({"type": "fact", "action": "kept", "original": original, "model_decision": decision})
            kept.append(record)
        resolved[field_name] = kept

    all_threads = _prior_records(prior, "plot_thread_updates")
    all_thread_ids = {str(item.get("id", "")) for item in all_threads}
    open_threads = _open_threads(prior)
    threads_by_id = {str(item.get("id")): item for item in open_threads}
    used_thread_ids = all_thread_ids | {
        str(item.get("id", "")) for item in resolved.get("plot_thread_updates", [])
    }
    kept_threads: List[Dict[str, Any]] = []
    for record in resolved.get("plot_thread_updates", []):
        original = deepcopy(record)
        status = str(record.get("status", "")).lower()
        operation = str(record.get("action") or status).lower()
        if operation == "closed":
            operation = "close"
        original_id = str(record.get("id", ""))

        # 延期（extend）是对同一条既有悬念的再次声明，不是新线索：ID 必须原样保留。
        # 到期未了结时，校验器给模型的修复指令正是「写一条
        # {"id":"PT-001-01","status":"open","extend":true,"deadline_chapter":N}」；
        # 那条建议里没有 thread 字段，落到下面的语义比对就会被判成「撞了 ID 的新线索」
        # 并改名，于是原悬念仍然到期未处理——模型照着提示改，却永远修不好。
        if record.get("extend") and original_id in all_thread_ids:
            kept_threads.append(record)
            continue

        matched = threads_by_id.get(original_id)
        decision: Optional[Dict[str, Any]] = None
        if (
            matched is not None
            and operation == "open"
            and not _same_text(record.get("thread"), matched.get("thread"))
        ):
            # An LLM can accidentally reuse an old ID for a different but
            # related thread.  The ID collision itself is not semantic proof.
            candidates = _limited_candidates(record, open_threads)
            decision = _ask_model("情节线索", record, candidates, model, send_prompt_fn)
            if not (
                decision["relation"] == "same"
                and decision["similarity"] >= SAME_THRESHOLD
                and decision["matched_id"] == matched.get("id")
            ):
                matched = None
        elif matched is None and open_threads:
            candidates = _limited_candidates(record, open_threads)
            decision = _ask_model("情节线索", record, candidates, model, send_prompt_fn)
            if decision["relation"] == "same" and decision["similarity"] >= SAME_THRESHOLD:
                matched = threads_by_id.get(str(decision["matched_id"]))
        if matched is not None:
            if operation == "open":
                # Dropping a duplicate is only safe while some chapter still
                # opens this exact ID.  When the model matched it to a
                # *different* thread, removing it orphans the later close.
                if (
                    original_id in protected_thread_ids
                    and str(matched.get("id", "")) != original_id
                ):
                    warnings.append(
                        f"线索 {original_id} 与既有线索 {matched.get('id')} 相似，"
                        "但后续章节要了结它，本章保留其开启记录"
                    )
                    kept_threads.append(record)
                    continue
                decisions.append({
                    "type": "plot_thread", "action": "duplicate_open_removed",
                    "original": original, "canonical_id": matched["id"],
                    "model_decision": decision,
                })
                continue
            record["id"] = matched["id"]
            decisions.append({
                "type": "plot_thread",
                "action": (
                    "close_id_canonicalized"
                    if operation == "close"
                    else "continuation_id_canonicalized"
                ),
                "original": original, "canonical_id": matched["id"],
                "model_decision": decision,
            })
        elif (
            operation == "open"
            and original_id in all_thread_ids
            and original_id in protected_thread_ids
        ):
            # Renaming would leave the later chapter closing an ID nobody opens.
            warnings.append(
                f"线索 {original_id} 的 id 与既有线索重复，但后续章节按此 id 了结，"
                "未自动改名，需要人工确认"
            )
        elif operation == "open" and original_id in all_thread_ids:
            new_id = _new_record_id("PT", chapter_number, used_thread_ids)
            used_thread_ids.add(new_id)
            record["id"] = new_id
            decisions.append({
                "type": "plot_thread", "action": "colliding_id_reassigned",
                "original": original, "new_id": new_id,
                "model_decision": decision,
            })
        elif decision:
            decisions.append({"type": "plot_thread", "action": "kept", "original": original, "model_decision": decision})
        kept_threads.append(record)
    resolved["plot_thread_updates"] = kept_threads

    # Events are immutable planning milestones.  A high-confidence semantic
    # duplicate is omitted; related events remain separate.
    events = _prior_records(prior, "timeline_events")
    events_by_id = {str(item.get("id")): item for item in events}
    used_event_ids = set(events_by_id) | {
        str(item.get("id", "")) for item in resolved.get("timeline_events", [])
    }
    kept_events: List[Dict[str, Any]] = []
    for record in resolved.get("timeline_events", []):
        original = deepcopy(record)
        original_id = str(record.get("id", ""))
        matched = events_by_id.get(original_id)
        decision = None
        if matched is not None and not _same_text(record.get("event"), matched.get("event")):
            candidates = _limited_candidates(record, events)
            decision = _ask_model("时间线事件", record, candidates, model, send_prompt_fn)
            if not (
                decision["relation"] == "same"
                and decision["similarity"] >= SAME_THRESHOLD
                and decision["matched_id"] == matched.get("id")
            ):
                matched = None
        elif matched is None and events:
            candidates = _limited_candidates(record, events)
            decision = _ask_model("时间线事件", record, candidates, model, send_prompt_fn)
            if decision["relation"] == "same" and decision["similarity"] >= SAME_THRESHOLD:
                matched = events_by_id.get(str(decision["matched_id"]))
        if matched is not None:
            decisions.append({"type": "timeline_event", "action": "duplicate_removed", "original": original, "canonical_id": matched["id"], "model_decision": decision})
            continue
        if original_id in events_by_id:
            new_id = _new_record_id("TL", chapter_number, used_event_ids)
            used_event_ids.add(new_id)
            record["id"] = new_id
            decisions.append({"type": "timeline_event", "action": "colliding_id_reassigned", "original": original, "new_id": new_id, "model_decision": decision})
        elif decision:
            decisions.append({"type": "timeline_event", "action": "kept", "original": original, "model_decision": decision})
        kept_events.append(record)
    resolved["timeline_events"] = kept_events

    # 人物属性的身份是「谁的哪项属性」，不是记录编号。同一属性写成「萨姆·金」和
    # 「山姆·金」会变成两条互不相干的记录，从而绕过取值比对——这一流尤其需要语义
    # 归并，而不只是 id 对齐。
    attributes = _prior_records(prior, "character_updates")
    attributes_by_key = {
        _attribute_key(item): item for item in attributes if _attribute_key(item)
    }
    prior_attribute_ids = {str(item.get("id", "")) for item in attributes}
    used_attribute_ids = prior_attribute_ids | {
        str(item.get("id", "")) for item in resolved.get("character_updates", [])
    }
    kept_attributes: List[Dict[str, Any]] = []
    for record in resolved.get("character_updates", []):
        original = deepcopy(record)
        key = _attribute_key(record)
        matched = attributes_by_key.get(key) if key else None
        decision: Optional[Dict[str, Any]] = None
        if matched is None and attributes:
            candidates = _limited_candidates(record, attributes)
            decision = _ask_model("人物属性", record, candidates, model, send_prompt_fn)
            if decision["relation"] == "same" and decision["similarity"] >= SAME_THRESHOLD:
                matched = next(
                    (
                        item
                        for item in attributes
                        if str(item.get("id")) == str(decision["matched_id"])
                    ),
                    None,
                )
        if matched is not None:
            if (matched.get("stable") or record.get("stable")) and values_conflict(
                matched.get("value"), record.get("value")
            ):
                warnings.append(
                    f"人物属性 {matched.get('id')}（{matched.get('character')}·{matched.get('attribute')}）取值冲突："
                    f"既有「{matched.get('value')}」，本章声明「{record.get('value')}」，未自动合并"
                )
            else:
                # 归一到既有写法，让 (character, attribute) 在账本里落到同一条记录上。
                record["id"] = matched["id"]
                record["character"] = matched.get("character", record.get("character"))
                record["attribute"] = matched.get("attribute", record.get("attribute"))
                decisions.append({
                    "type": "character_attribute",
                    "action": "canonicalized",
                    "original": original,
                    "canonical_id": matched["id"],
                    "model_decision": decision,
                })
        elif str(record.get("id", "")) in prior_attribute_ids:
            new_id = _new_record_id("CU", chapter_number, used_attribute_ids)
            used_attribute_ids.add(new_id)
            record["id"] = new_id
            decisions.append({
                "type": "character_attribute",
                "action": "colliding_id_reassigned",
                "original": original,
                "new_id": new_id,
                "model_decision": decision,
            })
        elif decision:
            decisions.append({
                "type": "character_attribute",
                "action": "kept",
                "original": original,
                "model_decision": decision,
            })
        kept_attributes.append(record)
    resolved["character_updates"] = kept_attributes

    _resolve_domain_fields(
        resolved,
        prior,
        chapter_number,
        model,
        send_prompt_fn,
        profile,
        decisions,
    )

    _audit(output_dir, chapter_number, decisions)
    return SemanticResolution(resolved, decisions, warnings)
