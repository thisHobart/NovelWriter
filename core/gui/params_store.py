# -*- coding: utf-8 -*-
"""参数文件读写（不依赖任何 GUI 框架）。

使用既有工作区磁盘格式：`<output_dir>/system/parameters.txt`，每行
`Display Key: value`。
"""
from __future__ import annotations

import os
import tempfile
from typing import Any, Dict, Tuple

# 稳定顺序即文件里的行序
CORE_ORDER = [
    "Output Directory", "Genre", "Subgenre", "Story Length", "Story Structure",
    "Novel Title", "Author Name", "Theme", "Tone", "Gender Generation Bias String",
    "Quality Loop", "Backend", "Model",
]

CORE_KEYS = {key.lower().replace(" ", "_") for key in CORE_ORDER}


def parameters_path(output_dir: str) -> str:
    return os.path.join(output_dir, "system", "parameters.txt")


def legacy_path(output_dir: str) -> str:
    return os.path.join(output_dir, "parameters.txt")


def resolve_existing(output_dir: str) -> Tuple[str, bool]:
    """返回 (要读的路径, 是否存在)。优先新版结构化路径，回落到旧的扁平路径。"""
    structured = parameters_path(output_dir)
    if os.path.exists(structured):
        return structured, True
    flat = legacy_path(output_dir)
    if os.path.exists(flat):
        return flat, True
    return structured, False


def load(output_dir: str) -> Dict[str, str]:
    """读出 `Display Key -> value` 原样字典；文件不存在时返回空字典。"""
    path, exists = resolve_existing(output_dir)
    if not exists:
        return {}
    loaded: Dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            if ":" in line:
                key, value = line.split(":", 1)
                loaded[key.strip()] = value.strip()
    return loaded


def save(output_dir: str, params: Dict[str, Any]) -> str:
    """按稳定行序写出，返回写入路径。

    params 用内部 snake_case 键（与 get_current_parameters 一致）；
    未在 CORE_ORDER 中的键作为动态参数追加在后面。
    """
    from core.generation.parameters import internal_parameters
    params = internal_parameters(params)
    lines = []
    for display in CORE_ORDER:
        key = display.lower().replace(" ", "_")
        value = params.get(key, "")
        if value is not None and value != "":
            lines.append(f"{display}: {value}")

    for key, value in params.items():
        if key in CORE_KEYS:
            continue
        if value is not None and value != "":
            lines.append(f"{key.replace('_', ' ').title()}: {value}")

    system_dir = os.path.join(output_dir, "system")
    os.makedirs(system_dir, exist_ok=True)
    path = parameters_path(output_dir)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=system_dir,
                                         prefix=".parameters_", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            handle.write("\n".join(lines))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.remove(temporary)
    return path
