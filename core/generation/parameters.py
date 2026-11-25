"""规范化作品参数、冻结任务输入，并生成完整的中文创作要求。"""
from __future__ import annotations

from copy import deepcopy
from contextvars import ContextVar
from functools import wraps
import json
import hashlib
from pathlib import Path
from uuid import uuid4

from core.localization import zh_label
from core.lore_localization import FIELD_LABELS

ALIASES = {"num_chars": "num_characters", "title": "novel_title"}
RUNTIME_KEYS = {"output_directory", "backend", "model", "quality_loop", "scene_dialogue",
                "temperature", "max_tokens", "api_key", "base_url"}
#: 作品参数：写进「作品要求」、参与参数检查和快照指纹的只有这些。参数文件里的其他键
#: （例如以前的作者姓名、性别比例、子题材附加设置）不再发给模型。
CREATION_KEYS = ("novel_title", "genre", "story_length", "story_structure", "theme", "tone",
                 "num_factions", "num_characters")
_ACTIVE_RUN = ContextVar("novelwriter_parameter_run", default=None)


class ParameterSnapshot(dict):
    """标记完整快照：后台不能再用磁盘参数补充或覆盖它。"""


def internal_parameters(source):
    result = {}
    # 内部键优先于同一次输入中的历史显示键。
    items = sorted((source or {}).items(), key=lambda pair: pair[0] == pair[0].lower().replace(" ", "_"))
    for key, value in items:
        key = ALIASES.get(key.lower().replace(" ", "_"), key.lower().replace(" ", "_"))
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            value = value.lower() == "true"
        result[key] = deepcopy(value)
    return result


def normalize_parameters(source, overrides=None):
    result = internal_parameters(source)
    incoming = internal_parameters(overrides)
    result.update(incoming)
    from core.config.story_options import counts_for
    counts = counts_for(result.get("story_length"))
    if counts:
        # 数量按篇幅由系统定；参数文件里旧的数量照常保留在文件中，但不再生效。
        result["num_factions"], result["num_characters"] = counts
    for key, default, lower, upper in (("num_factions", 6, 1, 10), ("num_characters", 8, 2, 10)):
        value = result.get(key, default)
        try:
            if isinstance(value, bool) or str(value).strip() != str(int(value)):
                raise ValueError()
            result[key] = int(value)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError(f"{'势力' if key == 'num_factions' else '人物'}数量必须是整数") from exc
        if not lower <= result[key] <= upper:
            raise ValueError(f"{'势力' if key == 'num_factions' else '人物'}数量必须在 {lower}–{upper} 之间")
    # 兼容旧消费者，同时保证两个拼写永远指向同一值。
    for key, value in list(result.items()):
        result[key.replace("_", " ").title()] = deepcopy(value)
    return result


def freeze_parameters(source):
    return ParameterSnapshot(deepcopy(normalize_parameters(source)))


def creation_parameters(source):
    params = internal_parameters(source)
    return {key: params[key] for key in CREATION_KEYS
            if params.get(key) is not None and params.get(key) != ""}


def parameter_label(key):
    known = {"novel_title": "作品标题", "genre": "题材", "story_length": "故事篇幅",
             "story_structure": "故事结构", "theme": "主题", "tone": "基调", "num_factions": "势力数量",
             "num_characters": "人物数量"}
    return known.get(key, FIELD_LABELS.get(key, zh_label(key.replace("_", " ").title())))


def requirements_block(parameters):
    lines = ["## 作品要求（最高优先级）", "本次所有生成必须遵守以下要求。未提供的参数不视为禁用；已有资料或格式示例不能覆盖这些要求。"]
    for key, value in creation_parameters(parameters).items():
        if value is None or value == "":
            continue
        lines.append(f"- {parameter_label(key)}：{zh_label(value)}")
    return "\n".join(lines)


def record_run(output_dir, stage, parameters):
    folder = Path(output_dir) / "system/generation_runs" / uuid4().hex
    folder.mkdir(parents=True)
    creative = creation_parameters(parameters)
    fingerprint = hashlib.sha256(json.dumps(creative, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    (folder / "parameters.json").write_text(json.dumps({
        "stage": stage, "creation_parameters": creative, "snapshot_sha256": fingerprint,
        "model": parameters.get("model"), "backend": parameters.get("backend"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    active = _ACTIVE_RUN.get()
    if active is not None:
        active.update(folder=folder, sequence=0)
    return str(folder)


def audit_parameters(function):
    """每次阶段调用独立记录，退出后恢复调用者的上下文。"""
    @wraps(function)
    def wrapped(*args, **kwargs):
        token = _ACTIVE_RUN.set({})
        try:
            return function(*args, **kwargs)
        finally:
            _ACTIVE_RUN.reset(token)
    return wrapped


def record_effective_prompt(prompt, backend, model):
    active = _ACTIVE_RUN.get()
    if not active or "folder" not in active:
        return
    active["sequence"] += 1
    folder = active["folder"] / "prompts"
    folder.mkdir(exist_ok=True)
    name = f"{active['sequence']:04d}"
    (folder / (name + ".md")).write_text(prompt, encoding="utf-8")
    (folder / (name + ".json")).write_text(
        json.dumps({"backend": backend, "model": model}, ensure_ascii=False, indent=2), encoding="utf-8")
