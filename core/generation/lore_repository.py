"""Local lore editing, recoverable saves and non-blocking review receipts.

No model calls or Qt dependencies. Existing artifact formats remain authoritative.
All participating generation entry points and edits share the same project lock.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass, field
from functools import wraps
import hashlib
import inspect
import json
import os
import re
from pathlib import Path
import tempfile
from uuid import uuid4

from core.generation.locks import project_lock

LORE = "story/lore/"
META = "system/lore_edits.json"
CHARACTERS = LORE + "characters.json"
FACTIONS = LORE + "factions.json"
CONTRACT = LORE + "lore_contract.json"
BODY = LORE + "generated_lore.md"


class LoreEditError(ValueError):
    pass


class MappingRequired(LoreEditError):
    def __init__(self, key, candidates):
        self.key, self.candidates = key, candidates
        super().__init__(f"{key} 的名称登记映射不唯一，请选择对应条目")


def encode(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def records(payload, kind):
    """Return the original list, never a projection that drops unknown fields."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if kind in payload:
            if not isinstance(payload[kind], list):
                raise LoreEditError(f"{kind} 必须是列表")
            return payload[kind]
        if any(k in payload for k in ("name", "faction_name", "character_name", "full_name")):
            return [payload]
    raise LoreEditError(f"无法识别 {kind} 的数据格式")


def name_of(record):
    for key in ("name", "character_name", "full_name", "faction_name", "title"):
        if isinstance(record.get(key), str):
            return record[key].strip()
    return ""


def set_name(record, old, new):
    for key in ("name", "character_name", "full_name", "faction_name"):
        if record.get(key) == old:
            record[key] = new


def background_name(record):
    def safe(value):
        return str(value).lower().replace(" ", "_").replace("/", "_").replace("\\", "_").replace(":", "_")
    return f"background_{safe(record.get('role', ''))}_{safe(name_of(record))}.md"


@dataclass
class LoreSnapshot:
    root: str
    files: dict[str, str]
    fingerprints: dict[str, str]
    metadata: dict


@dataclass
class LorePreview:
    files: dict[str, str]
    changed_files: list[str]
    review_items: list[str] = field(default_factory=list)


class LoreRepository:
    def __init__(self, output_dir):
        self.root = Path(output_dir).resolve()
        self.history = self.root / "system/lore_history"

    def lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "system").mkdir(exist_ok=True)
        return project_lock(str(self.root / "system/.lore.lock"), timeout=0)

    def path(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise LoreEditError("文件路径超出当前项目")
        return path

    def _files(self):
        base = self.root / LORE
        paths = sorted(base.rglob("*")) if base.exists() else []
        return {p.relative_to(self.root).as_posix(): p.read_text(encoding="utf-8")
                for p in paths if p.is_file() and p.suffix in {".md", ".json"}}

    def _metadata(self):
        path = self.path(META)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def _fingerprints(self, files):
        result = {key: hashlib.sha256(self.path(key).read_bytes()).hexdigest() for key in files}
        result[META] = hashlib.sha256(self.path(META).read_bytes()).hexdigest() if self.path(META).exists() else ""
        return result

    @staticmethod
    def _atomic(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".lore-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _restore(self, before):
        for rel, content in before.items():
            path = self.path(rel)
            if content is None:
                path.unlink(missing_ok=True)
            else:
                self._atomic(path, base64.b64decode(content))

    def _recover(self):
        if not self.history.exists():
            return
        for manifest in sorted(self.history.glob("*/transaction.json")):
            state = json.loads(manifest.read_text(encoding="utf-8"))
            if state["status"] == "prepared":
                self._restore(state["before"])
                state["status"] = "rolled_back"
                self._atomic(manifest, encode(state).encode())

    def _transaction(self, changes, reason="manual"):
        folder = self.history / uuid4().hex
        folder.mkdir(parents=True)
        before = {}
        for rel in changes:
            path = self.path(rel)
            before[rel] = base64.b64encode(path.read_bytes()).decode() if path.exists() else None
        state = {"status": "prepared", "reason": reason, "before": before}
        manifest = folder / "transaction.json"
        self._atomic(manifest, encode(state).encode())
        try:
            for rel, text in changes.items():
                if text is None:
                    self.path(rel).unlink(missing_ok=True)
                else:
                    self._atomic(self.path(rel), text.encode("utf-8"))
            state["status"] = "committed"
            self._atomic(manifest, encode(state).encode())
        except BaseException:
            self._restore(before)
            state["status"] = "rolled_back"
            self._atomic(manifest, encode(state).encode())
            raise

    def load_snapshot(self):
        with self.lock():
            self._recover()
            files = self._files()
            return LoreSnapshot(str(self.root), files, self._fingerprints(files), self._metadata())

    def recover_pending(self):
        """Recover before displaying a project, without creating an empty project."""
        if self.history.exists():
            with self.lock():
                self._recover()

    def downstream(self, stamps=True):
        result = {}
        for folder, stage in (("story/structure", "structure"), ("story/planning", "scenes"),
                              ("story/content", "chapters")):
            base = self.root / folder
            for p in sorted(base.rglob("*")) if base.exists() else []:
                if p.is_file() and p.suffix in {".md", ".json"} and p.name != "suggested_titles.md":
                    rel = p.relative_to(self.root).as_posix()
                    result[rel] = {"stage": stage}
                    if stamps:
                        result[rel]["stamp"] = f"{p.stat().st_mtime_ns}:{hashlib.sha256(p.read_bytes()).hexdigest()}"
        return result

    @staticmethod
    def _compatible(old, new, label):
        if type(old) is not type(new):
            raise LoreEditError(f"{label} 的字段类型不能改变")
        if isinstance(old, dict):
            if old.keys() != new.keys():
                raise LoreEditError(f"{label} 的字段不能删除或新增")
            for key in old:
                LoreRepository._compatible(old[key], new[key], f"{label}.{key}")
        elif isinstance(old, list):
            if all(isinstance(v, str) for v in old + new):
                return
            if len(old) != len(new):
                raise LoreEditError(f"{label} 本期不支持新增或删除条目")
            for index, (a, b) in enumerate(zip(old, new)):
                LoreRepository._compatible(a, b, f"{label}[{index}]")
        elif label.rsplit(".", 1)[-1] in {"id", "schema_version", "origin"} and old != new:
            raise LoreEditError(f"{label} 是只读字段")
        elif isinstance(old, str) and old.strip() and not new.strip() and label.rsplit(".", 1)[-1] in {
            "name", "character_name", "faction_name", "full_name", "role", "description", "background",
            "goal", "motivation", "era", "technology_level", "blocked_goal",
        }:
            raise LoreEditError(f"{label} 不能为空")

    def preview(self, snapshot, edits, mappings=None):
        if snapshot.root != str(self.root):
            raise LoreEditError("编辑快照不属于当前项目")
        files = dict(snapshot.files)
        for rel, text in edits.items():
            if rel not in files:
                raise LoreEditError("只能编辑已有设定文件")
            if not isinstance(text, str) or not text.strip():
                raise LoreEditError("设定内容不能为空")
            if rel.endswith(".json"):
                try:
                    self._compatible(json.loads(files[rel]), json.loads(text), rel)
                except json.JSONDecodeError as exc:
                    raise LoreEditError(f"JSON 无法解析：{exc}") from exc
            files[rel] = text
        payloads = {key: json.loads(files[key]) for key in (CHARACTERS, FACTIONS, CONTRACT) if key in files}
        original = {key: json.loads(snapshot.files[key]) for key in payloads}
        groups = {key: records(payloads[key], kind) if key in payloads else []
                  for key, kind in ((CHARACTERS, "characters"), (FACTIONS, "factions"))}
        contract = payloads.get(CONTRACT, {})
        renames = []
        mappings = mappings or {}
        for key, kind, registry in ((CHARACTERS, "characters", "canonical_characters"),
                                    (FACTIONS, "factions", "canonical_factions")):
            old_rows = records(original[key], kind) if key in original else []
            old_registry = original.get(CONTRACT, {}).get(registry, [])
            new_registry = contract.get(registry, [])
            for index, (old, new) in enumerate(zip(old_rows, groups[key])):
                old_name, new_name = name_of(old), name_of(new)
                matches = [i for i, item in enumerate(old_registry)
                           if old_name == item.get("name") or old_name in (item.get("aliases") or [])]
                token = f"{kind}:{index}"
                # Attribute edits do not require mapping; canonical renames do.
                canonical_changed = any(a.get("name") != b.get("name") for a, b in zip(old_registry, new_registry))
                chosen = mappings.get(token)
                if len(matches) == 1:
                    chosen = matches[0]
                elif CONTRACT in payloads and (old_name != new_name or (canonical_changed and matches)):
                    if chosen is None:
                        raise MappingRequired(token, [(i, r.get("name", "")) for i, r in enumerate(old_registry)])
                    if chosen not in range(len(old_registry)):
                        raise LoreEditError("请选择有效的名称登记条目")
                if chosen is not None:
                    previous = old_registry[chosen]["name"]
                    proposed = new_registry[chosen]["name"]
                    if old_name != new_name:
                        if proposed not in {previous, new_name}:
                            raise LoreEditError("人物/势力名称与名称登记表的修改冲突")
                        new_registry[chosen]["name"] = new_name
                    elif previous != proposed:
                        set_name(new, old_name, proposed)
                        new_name = proposed
                elif old_name != new_name and CONTRACT in payloads:
                    raise MappingRequired(token, [(i, r.get("name", "")) for i, r in enumerate(old_registry)])
                if old_name != new_name:
                    set_name(new, old_name, new_name)
                    renames.append((kind, old_name, new_name))
                if key == CHARACTERS and (old_name != new_name or old.get("role") != new.get("role")):
                    old_file, new_file = background_name(old), background_name(new)
                    for prefix in (LORE, LORE + "backgrounds/"):
                        src, dst = prefix + old_file, prefix + new_file
                        if src in files and src != dst:
                            if dst in files or self.path(dst).exists():
                                raise LoreEditError(f"背景文件已存在，不能覆盖：{dst}")
                            self.path(dst)
                            if Path(dst).name != new_file or any(c in new_file for c in '\\<>"|?*'):
                                raise LoreEditError("新名称包含不能用于背景文件名的字符")
                            files[dst] = files.pop(src)
        # Apply maps simultaneously: a rename must never cascade through another rename.
        char_map = {a: b for kind, a, b in renames if kind == "characters"}
        faction_map = {a: b for kind, a, b in renames if kind == "factions"}
        location_map = {}
        for old, new in zip(original.get(CONTRACT, {}).get("canonical_locations", []), contract.get("canonical_locations", [])):
            if old.get("name") != new.get("name"):
                location_map[old["name"]] = new["name"]
                renames.append(("locations", old["name"], new["name"]))
        cast = payloads.get(CHARACTERS, {})
        if isinstance(cast, dict):
            for relation in cast.get("relationships", []):
                for endpoint in ("character1", "character2"):
                    if endpoint in relation:
                        relation[endpoint] = char_map.get(relation[endpoint], relation[endpoint])
        for person in groups[CHARACTERS]:
            if isinstance(person.get("opposes"), dict):
                value = person["opposes"].get("character")
                if value in char_map:
                    person["opposes"]["character"] = char_map[value]
            if "faction" in person:
                person["faction"] = faction_map.get(person["faction"], person["faction"])
        for faction in groups[FACTIONS]:
            for conflict in faction.get("conflicts", []) or []:
                value = conflict.get("with")
                if value in faction_map:
                    conflict["with"] = faction_map[value]
            territory = faction.get("territory")
            if isinstance(territory, str) and territory in location_map:
                faction["territory"] = location_map[territory]
            elif isinstance(territory, dict) and territory.get("name") in location_map:
                territory["name"] = location_map[territory["name"]]
        self._validate(original, payloads, groups)
        for key, value in payloads.items():
            if value != original[key]:
                files[key] = encode(value)
            elif key in edits:
                files[key] = snapshot.files[key]  # Formatting-only changes are not revisions.
        changed = sorted(k for k in files.keys() | snapshot.files.keys() if files.get(k) != snapshot.files.get(k))
        review = []
        if changed:
            review.append("设定已修改，请核对正文、人物背景与名称及规则登记表是否一致。")
        texts = dict(files)
        prior_findings = []
        for item in snapshot.metadata.get("review_items", []):
            found = re.fullmatch(r"(.*?)：仍含旧名「(.*?)」（现名「(.*?)」），请核对。", item)
            if found:
                prior_findings.append((item, *found.groups()))
        if renames or prior_findings:
            texts.update({rel: self.path(rel).read_text(encoding="utf-8") for rel in self.downstream(stamps=False)})
        review.extend(item for item, rel, old, new in prior_findings if old in texts.get(rel, ""))
        for _, old, new in renames:
            for rel, text in texts.items():
                if old in text:
                    review.append(f"{rel}：仍含旧名「{old}」（现名「{new}」），请核对。")
        return LorePreview(files, changed, list(dict.fromkeys(review)))

    @staticmethod
    def _validate(original, payloads, groups):
        for key, rows in groups.items():
            seen = set()
            for row in rows:
                name = name_of(row)
                if not name or name in seen:
                    raise LoreEditError(f"名称不能为空或重复：{name}")
                seen.add(name)
        def defects(data):
            errors = set()
            cast = data.get(CHARACTERS, {})
            characters = records(cast, "characters") if CHARACTERS in data else []
            factions = records(data[FACTIONS], "factions") if FACTIONS in data else []
            cn, fn = {name_of(r) for r in characters}, {name_of(r) for r in factions}
            pairs = set()
            for r in cast.get("relationships", []) if isinstance(cast, dict) else []:
                for endpoint in ("character1", "character2"):
                    if r.get(endpoint) not in cn:
                        errors.add(f"人物关系引用不存在：{r.get(endpoint)}")
                if r.get("character1") == r.get("character2"):
                    errors.add("人物关系的两端不能相同")
                pair = tuple(sorted((str(r.get("character1", "")), str(r.get("character2", "")))))
                if pair in pairs:
                    errors.add(f"人物关系重复：{'、'.join(pair)}")
                pairs.add(pair)
            for r in characters:
                target = (r.get("opposes") or {}).get("character")
                if target and target not in cn:
                    errors.add(f"反派目标引用不存在：{target}")
                if r.get("faction") and r["faction"] not in fn:
                    errors.add(f"所属势力不存在：{r['faction']}")
            for r in factions:
                for c in r.get("conflicts", []) or []:
                    if c.get("with") and c["with"] not in fn:
                        errors.add(f"势力冲突引用不存在：{c['with']}")
            return errors
        errors = defects(payloads) - defects(original)
        if errors:
            raise LoreEditError("\n".join(sorted(errors)))
        if CONTRACT in payloads and payloads[CONTRACT] != original[CONTRACT]:
            from core.generation.design_contract import validate_lore_contract
            validate_lore_contract(payloads[CONTRACT])
            ids = []
            for key in ("canonical_characters", "canonical_factions", "canonical_locations"):
                ids.extend(r["id"] for r in payloads[CONTRACT].get(key, []))
            if len(ids) != len(set(ids)):
                raise LoreEditError("名称登记表 ID 不能重复")

    def _revision(self, metadata, files, changed, review, manual):
        updated = deepcopy(metadata)
        updated["version"] = int(updated.get("version", 0)) + 1
        updated["fingerprint"] = digest(encode(files))
        updated["review_items"] = review
        updated["pending"] = {rel: {"stage": item["stage"], "version": updated["version"]}
                              for rel, item in self.downstream(stamps=False).items()}
        if manual:
            updated["manual_files"] = sorted(set(updated.get("manual_files", [])) | set(changed))
        return updated

    def save(self, snapshot, edits, mappings=None):
        with self.lock():
            self._recover()
            if snapshot.fingerprints != self._fingerprints(self._files()):
                raise LoreEditError("文件已在编辑期间变化，请保留草稿并重新打开后核对，未覆盖磁盘内容。")
            preview = self.preview(snapshot, edits, mappings)
            if not preview.changed_files:
                return preview
            metadata = self._revision(snapshot.metadata, preview.files, preview.changed_files, preview.review_items, True)
            changes = {k: preview.files.get(k) for k in preview.changed_files}
            changes[META] = encode(metadata)
            from core.generation.lore_run import receipt_changes_for_edits
            changes.update(receipt_changes_for_edits(self.root, snapshot.files, preview.files))
            self._transaction(changes)
            return preview

    def review_status(self):
        # Read-only and cheap enough for UI refresh. Recovery happens on edit/generation.
        metadata = self._metadata()
        existing = self.downstream(stamps=False)
        return {**metadata, "pending": {k: v for k, v in metadata.get("pending", {}).items() if k in existing}}

    def acknowledge(self, version, stage=None):
        with self.lock():
            self._recover()
            metadata = self._metadata()
            if metadata.get("version") != version:
                raise LoreEditError("设定版本已变化，请重新核对")
            metadata["pending"] = {k: v for k, v in metadata.get("pending", {}).items() if stage and v["stage"] != stage}
            if stage is None:
                metadata["review_items"] = []
            self._transaction({META: encode(metadata)}, "acknowledge")

    def backup_generation(self, files):
        if not files:
            return
        folder = self.history / uuid4().hex
        folder.mkdir(parents=True)
        self._atomic(folder / "backup.json", encode({"reason": "before_generation", "files": files}).encode())


def lore_context(output_dir):
    """Complete source fingerprint precedes prose, so truncation cannot hide changes."""
    repo = LoreRepository(output_dir)
    files = repo._files()
    if not files:
        return ""
    ordered = [CONTRACT, BODY, CHARACTERS, FACTIONS]
    ordered += sorted(set(files) - set(ordered))
    from core.generation.prompt_context import sanitize_lore_content
    fingerprint = digest(encode(files))
    if BODY in files:
        files[BODY] = sanitize_lore_content(files[BODY])
    return (f"\n## 当前世界设定（人工保存版本优先）\n完整设定指纹：{fingerprint}\n"
            + "名称、年代、技术与规则以名称登记表为准；人物及势力属性以对应卡片为准。自由文本若有旧设定，按当前登记与卡片纠正。\n"
            + "\n".join(f"### {key}\n{files[key]}" for key in ordered if key in files))


def tracked_generation(function):
    """Serialize edits with generation and clear only artifacts actually rewritten."""
    signature = inspect.signature(function)
    @wraps(function)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        repo = LoreRepository(bound.arguments["output_dir"])
        with repo.lock():
            repo._recover()
            before = repo._files()
            downstream = repo.downstream()
            if bound.arguments["step"] == "lore" and bound.arguments.get("action") != "titles":
                repo.backup_generation(before)
            success = False
            result = None
            try:
                result = function(*args, **kwargs)
                success = True
                return result
            finally:
                after = repo._files()
                metadata = repo._metadata()
                previous = deepcopy(metadata)
                if after != before:
                    metadata = repo._revision(metadata, after, [], ["设定已重新生成，请核对相关内容。"], False)
                if success:
                    current = repo.downstream()
                    metadata["pending"] = {
                        rel: value for rel, value in metadata.get("pending", {}).items()
                        if rel in current and current[rel] == downstream.get(rel)
                    }
                    from core.generation.parameter_compliance import collect_parameter_warnings
                    warnings = collect_parameter_warnings(result)
                    if warnings:
                        metadata.setdefault("version", 0)
                        metadata.setdefault("fingerprint", digest(encode(after)))
                        metadata["review_items"] = list(dict.fromkeys(metadata.get("review_items", []) + warnings))
                if metadata != previous and (previous or after != before or metadata.get("review_items")):
                    repo._transaction({META: encode(metadata)}, "generation_receipt")
    return wrapped
