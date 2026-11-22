# -*- coding: utf-8 -*-
"""从磁盘读取各阶段产物。

门禁与展示都以磁盘为准（与 core/generation/workflow_status.py 同一套判定思路）：
持久化的运行记录只能说明编排器做过什么，不能说明用户后来手改了什么。

所有函数都是纯读取，失败时返回空结果而不是抛异常——界面永远要能画出来。
"""
from __future__ import annotations

import json
import os

from core.localization import zh_label
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

LORE_DIR = os.path.join("story", "lore")
STRUCTURE_DIR = os.path.join("story", "structure")
PLANNING_DIR = os.path.join("story", "planning")
OUTLINES_DIR = os.path.join(PLANNING_DIR, "chapter_outlines")
SCENE_PLANS_DIR = os.path.join(PLANNING_DIR, "detailed_scene_plans")
CHAPTERS_DIR = os.path.join("story", "content", "chapters")


# ============================================================ 通用
def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def _read_json(path: str) -> Any:
    text = _read_text(path)
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _listdir(path: str, suffix: str = "") -> List[str]:
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return []
    return [n for n in names if not suffix or n.endswith(suffix)]


def _mtime_label(path: str) -> str:
    try:
        import time

        return time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
    except OSError:
        return ""


def count_files(output_dir: str, relative_dir: str, suffix: str = "") -> int:
    return len(_listdir(os.path.join(output_dir, relative_dir), suffix))


def word_count(text: str) -> int:
    """中英混排的近似字数：中日韩字符按字算，其余按词算。"""
    cjk = len(re.findall(r"[一-鿿぀-ヿ]", text))
    latin = len(re.findall(r"[A-Za-z0-9]+", text))
    return cjk + latin


# ============================================================ 阶段 2 · 设定
@dataclass
class Entity:
    name: str
    kind: str
    summary: str
    raw: Dict[str, Any] = field(default_factory=dict)


#: 人物卡里 role 的取值。摘要退到这一栏时要译成中文再显示。
CHARACTER_ROLE_LABELS = frozenset(
    {"protagonist", "deuteragonist", "antagonist", "supporting"}
)


def _entity_summary(item: Dict[str, Any], keys: List[str]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    # 没有约定字段时，挑第一个像描述的长字符串
    for value in item.values():
        if isinstance(value, str) and len(value.strip()) > 20:
            return value.strip()
    return ""


def _entities_from(payload: Any, kind: str, name_keys: List[str],
                   summary_keys: List[str]) -> List[Entity]:
    items: List[Dict[str, Any]] = []
    if isinstance(payload, list):
        items = [i for i in payload if isinstance(i, dict)]
    elif isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list):
                items = [i for i in value if isinstance(i, dict)]
                break
        else:
            items = [payload]

    entities = []
    for item in items:
        name = ""
        for key in name_keys:
            if isinstance(item.get(key), str) and item[key].strip():
                name = item[key].strip()
                break
        if not name:
            name = "（未命名）"
        entities.append(Entity(name, kind, _entity_summary(item, summary_keys), item))
    return entities


def factions(output_dir: str) -> List[Entity]:
    payload = _read_json(os.path.join(output_dir, LORE_DIR, "factions.json"))
    return _entities_from(payload, "势力", ["name", "faction_name", "title"],
                          ["description", "summary", "ideology", "goals"])


def characters(output_dir: str) -> List[Entity]:
    """人物卡片。

    摘要以前把 role 排在最前面，而它是英文枚举，于是界面上每个人的说明都是
    「protagonist」。改成优先显示外貌与背景这类真正能认人的字段；确实只剩 role
    时译成中文再显示。
    """
    payload = _read_json(os.path.join(output_dir, LORE_DIR, "characters.json"))
    entities = _entities_from(
        payload, "人物", ["name", "character_name", "full_name"],
        ["description", "appearance_summary", "background", "backstory_summary",
         "summary", "role"],
    )
    return [
        Entity(item.name, item.kind, zh_label(item.summary), item.raw)
        if item.summary in CHARACTER_ROLE_LABELS else item
        for item in entities
    ]


def lore_text(output_dir: str) -> str:
    return _read_text(os.path.join(output_dir, LORE_DIR, "generated_lore.md"))


def background_files(output_dir: str) -> List[str]:
    base = os.path.join(output_dir, LORE_DIR)
    files = [n for n in _listdir(base, ".md") if n.startswith("background_")]
    files += [os.path.join("backgrounds", n)
              for n in _listdir(os.path.join(base, "backgrounds"), ".md")]
    return files


def suggested_titles(output_dir: str) -> str:
    return _read_text(os.path.join(output_dir, PLANNING_DIR, "suggested_titles.md"))


def lore_file_count(output_dir: str) -> int:
    base = os.path.join(output_dir, LORE_DIR)
    count = len(_listdir(base, ".json")) + len(_listdir(base, ".md"))
    return count + len(_listdir(os.path.join(base, "backgrounds"), ".md"))


# ============================================================ 阶段 3 · 结构
@dataclass
class Section:
    key: str          # 文件名去掉扩展名
    title: str        # 人读的小节名
    path: str
    text: str
    updated: str


def _pretty_section(filename: str) -> str:
    stem = os.path.splitext(filename)[0]
    stem = re.sub(r"^\d+[-_]?act[-_]structure[-_]?", "", stem, flags=re.I)
    return stem.replace("_", " ").replace("-", " ").strip().title() or filename


def structure_sections(output_dir: str) -> List[Section]:
    base = os.path.join(output_dir, STRUCTURE_DIR)
    sections = []
    for name in _listdir(base, ".md"):
        path = os.path.join(base, name)
        sections.append(Section(os.path.splitext(name)[0], _pretty_section(name),
                                path, _read_text(path), _mtime_label(path)))
    return sections


def character_arcs(output_dir: str) -> Optional[str]:
    for name in ("character_arcs.md", "character_arcs.json"):
        path = os.path.join(output_dir, STRUCTURE_DIR, name)
        if os.path.exists(path):
            return _read_text(path)
    return None


def faction_arcs(output_dir: str) -> Optional[str]:
    for name in ("faction_arcs.md", "faction_arcs.json"):
        path = os.path.join(output_dir, STRUCTURE_DIR, name)
        if os.path.exists(path):
            return _read_text(path)
    return None


def reconciled_locations(output_dir: str) -> Optional[str]:
    path = os.path.join(output_dir, PLANNING_DIR, "reconciled_locations_arcs.md")
    return _read_text(path) if os.path.exists(path) else None


# ============================================================ 阶段 4 · 场景
def chapter_outlines(output_dir: str) -> List[Section]:
    base = os.path.join(output_dir, OUTLINES_DIR)
    result = []
    for name in _listdir(base, ".md"):
        path = os.path.join(base, name)
        result.append(Section(os.path.splitext(name)[0], _pretty_section(name),
                              path, _read_text(path), _mtime_label(path)))
    return result


def _chapter_number(name: str) -> int:
    """Extract an explicit chapter marker instead of the first filename number.

    Real scene-plan names contain the story structure before the chapter suffix,
    for example ``scenes_6-act_structure_beginning_ch1.md``.  Treating the first
    number as the chapter made every file in a 6-act project appear as Chapter 6.
    """
    stem = os.path.splitext(os.path.basename(name))[0]
    for pattern in (
        r"(?:^|[_\-\s])(?:chapter|ch)[_\-\s]*(\d+)(?=$|[_\-\s])",
        r"第[_\-\s]*(\d+)[_\-\s]*章",
    ):
        match = re.search(pattern, stem, flags=re.I)
        if match:
            return int(match.group(1))
    return 0


def _chapter_sort_key(name: str) -> tuple[bool, int, str]:
    number = _chapter_number(name)
    return number == 0, number, name.lower()


def scene_plans(output_dir: str) -> List[Section]:
    base = os.path.join(output_dir, SCENE_PLANS_DIR)
    names = [n for n in _listdir(base) if n.endswith((".md", ".json"))]
    names.sort(key=_chapter_sort_key)
    result = []
    for name in names:
        path = os.path.join(base, name)
        number = _chapter_number(name)
        title = f"第 {number} 章场景" if number else _pretty_section(name)
        result.append(Section(os.path.splitext(name)[0], title, path,
                              _read_text(path), _mtime_label(path)))
    return result


# ============================================================ 阶段 5 · 章节
@dataclass
class Chapter:
    number: int
    path: str
    title: str
    text: str
    words: int
    updated: str

    @property
    def exists(self) -> bool:
        return bool(self.text.strip())


def _chapter_title(number: int, text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:40]
    return f"第 {number} 章"


def chapters(output_dir: str) -> List[Chapter]:
    base = os.path.join(output_dir, CHAPTERS_DIR)
    names = [n for n in _listdir(base, ".md")]
    names.sort(key=_chapter_sort_key)
    result = []
    for name in names:
        path = os.path.join(base, name)
        text = _read_text(path)
        number = _chapter_number(name)
        result.append(Chapter(number, path, _chapter_title(number, text), text,
                              word_count(text), _mtime_label(path)))
    return result


def pending_reviews(output_dir: str) -> Dict[int, Any]:
    """未通过质量闸门、等着作者裁决的章节，按章号索引。

    记录存在本身就等于「这一章待复审」：章节被接受时生成侧会删掉它。
    """
    from core.generation.pending_review import load_all

    return load_all(output_dir)


def expected_chapter_count(output_dir: str) -> int:
    """优先用 workflow_status 的判定，无计划时退回场景规划数量。"""
    from core.generation.workflow_status import expected_chapter_count as expected

    value = expected(output_dir)
    if value:
        return int(value)
    return len(scene_plans(output_dir))


# ============================================================ 汇总
def stage_file_counts(output_dir: str) -> Dict[str, int]:
    return {
        "lore": lore_file_count(output_dir),
        "structure": len(_listdir(os.path.join(output_dir, STRUCTURE_DIR), ".md")),
        "scenes": len(scene_plans(output_dir)) + len(chapter_outlines(output_dir)),
        "chapters": len(chapters(output_dir)),
    }
