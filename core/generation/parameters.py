"""规范化作品参数、冻结任务输入，并生成完整的中文创作要求。"""
from __future__ import annotations

from copy import deepcopy
from contextvars import ContextVar
from functools import wraps
import json
import hashlib
from pathlib import Path
from uuid import uuid4

from core.config.story_options import GENDER_BIAS_MAP
from core.localization import zh_label
from core.lore_localization import FIELD_LABELS

ALIASES = {"num_chars": "num_characters", "title": "novel_title"}
RUNTIME_KEYS = {"output_directory", "backend", "model", "quality_loop", "scene_dialogue",
                "temperature", "max_tokens", "api_key", "base_url"}
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
    bias = result.get("gender_generation_bias_string", "Balanced (50F/50M)")
    female, male = GENDER_BIAS_MAP.get(str(bias), (50, 50))
    if "gender_generation_bias_string" in incoming:
        result.update(female_percentage=incoming.get("female_percentage", female),
                      male_percentage=incoming.get("male_percentage", male))
    else:
        result.setdefault("female_percentage", female)
        result.setdefault("male_percentage", male)
    for key in ("female_percentage", "male_percentage"):
        try:
            value = float(result[key])
            if not 0 <= value <= 100:
                raise ValueError()
            result[key] = int(value) if value.is_integer() else value
        except (ValueError, TypeError) as exc:
            raise ValueError("性别比例必须是 0–100 之间的数字") from exc
    # 兼容旧消费者，同时保证两个拼写永远指向同一值。
    for key, value in list(result.items()):
        result[key.replace("_", " ").title()] = deepcopy(value)
    return result


def freeze_parameters(source):
    return ParameterSnapshot(deepcopy(normalize_parameters(source)))


def creation_parameters(source):
    return {key: value for key, value in internal_parameters(source).items()
            if key not in RUNTIME_KEYS and not key.startswith("_") and value is not None and value != ""}


def parameter_label(key):
    known = {"novel_title": "作品标题", "author_name": "作者姓名", "story_length": "故事篇幅",
             "story_structure": "故事结构", "theme": "主题", "tone": "基调", "num_factions": "势力数量",
             "num_characters": "人物数量", "gender_generation_bias_string": "性别比例",
             "female_percentage": "女性比例", "male_percentage": "男性比例"}
    return known.get(key, FIELD_LABELS.get(key, zh_label(key.replace("_", " ").title())))


def requirements_block(parameters):
    lines = ["## 作品要求（最高优先级）", "本次所有生成必须遵守以下要求。未提供的参数不视为禁用；已有资料或格式示例不能覆盖这些要求。"]
    for key, value in creation_parameters(parameters).items():
        if value is None or value == "":
            continue
        label = parameter_label(key)
        if isinstance(value, bool):
            lines.append(f"- {label}：{'必须体现' if value else '不得引入'}。")
        else:
            lines.append(f"- {label}：{zh_label(value)}")
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
