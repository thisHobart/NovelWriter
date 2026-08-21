"""Chinese names for stories written in Chinese.

The genre generators assemble names from hardcoded English word lists before
the language model sees anything, so no prompt can make the cast Chinese —
"Michelle Lee" arrives already decided.  This module replaces those names after
generation and before any prose is written.

Names are assigned through a *stable mapping*: the same source name always
becomes the same Chinese name within one project, so characters, factions,
locations and the backstories written from them never drift apart.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

# Deliberately ordinary surnames: a cast of 司徒/慕容/上官 reads as costume drama
# no matter what the story is.
SURNAMES: Sequence[str] = (
    "王 李 张 刘 陈 杨 黄 赵 吴 周 徐 孙 马 朱 胡 郭 何 高 林 罗 郑 梁 谢 宋 唐 许 韩 冯 邓 曹 "
    "彭 曾 肖 田 董 袁 潘 于 蒋 蔡 余 杜 叶 程 苏 魏 吕 丁 任 沈 姚 卢 姜 崔 钟 谭 陆 汪 范 金 "
    "石 廖 贾 夏 韦 傅 方 白 邹 孟 熊 秦 邱 江 尹 薛 闫 段 雷 侯 龙 史 陶 黎 贺 顾 毛 郝 龚 邵"
).split()

# Given names are gendered on *both* characters: pairing a male first
# character with a female second one ("航岚") reads as a typo, not a name.
_GIVEN = {
    "male": (
        "建 国 军 伟 强 磊 勇 峰 涛 明 超 鹏 华 亮 刚 平 辉 杰 浩 宇 "
        "航 楠 泽 川 岩 阳 帆 恒 岸 野"
    ).split(),
    "female": (
        "芳 娜 敏 静 秀 娟 英 丽 燕 霞 玲 兰 凤 洁 梅 琳 云 莲 蓉 婷 "
        "雅 蕾 薇 宁 妍 悦 岚 桐"
    ).split(),
    "": "文 志 安 嘉 逸 铭 睿 清 舟 然 心 之 遥 微 沐 白".split(),
}

_GIVEN_SECOND = {
    "male": "明 华 军 峰 涛 强 杰 宇 航 川 阳 辉 民 生 波 刚 舟 山".split(),
    "female": "芳 丽 娟 静 敏 燕 霞 玲 洁 梅 琳 云 蓉 婷 雅 薇 宁 妍".split(),
    "": "然 宁 舟 川 岚 遥 之 白 野 屿 桐 澈 沐 辰 铭 清".split(),
}

_CITY_FIRST = "临 江 云 海 青 南 沐 永 安 长 平 新 通 广 明 汇 榆 松 雾".split()
_CITY_SECOND = "州 阳 城 港 湾 川 平 安 山 海 江 河 陵 门".split()

_ORG_TEMPLATES: Dict[str, Sequence[str]] = {
    "police": ("{city}市公安局", "{city}市公安局刑事侦查支队", "{city}市局重案队"),
    "federal": ("公安部督办专案组", "省公安厅刑侦总队", "国家监察委驻{city}联合调查组"),
    "prosecution": ("{city}市人民检察院", "{city}市检察院公诉一部"),
    "forensic": ("{city}市公安局物证鉴定中心", "{city}市司法鉴定中心", "省法医学重点实验室"),
    "detective": ("{city}明诚调查事务所", "{city}岸线风险咨询", "{city}恒安商务调查"),
    "criminal": ("潮汐会", "北岸联号", "长顺贸易", "灰线", "十一号仓"),
    "law_firm": ("{city}恒正律师事务所", "{city}天衡律师事务所"),
    "company": ("{city}宏升物流", "{city}深海资产管理", "{city}瑞和实业"),
}

# Order matters: "Violent Crime Division" and "Silent Crime Organization" both
# say "crime", and only the structural noun tells them apart.
_ORG_KEYWORDS = (
    ("prosecution", ("attorney", "prosecut", "solicitor")),
    ("forensic", ("forensic", "crime lab", "laborator", "evidence", "science center", "medical examiner", "morgue")),
    (
        "police",
        ("police", "sheriff", "constab", "division", "precinct", "squad", "homicide",
         "patrol", "task force", "department", "警"),
    ),
    ("federal", ("federal", "bureau", "national", "homeland", "marshal", "customs", "intelligence")),
    ("detective", ("detective", "investigat", "private", "consultant")),
    ("criminal", ("cartel", "syndicate", "gang", "mafia", "crew", "shadow", "organization", "brotherhood")),
    ("law_firm", ("law firm", "legal", "counsel", "attorneys at law", "associates")),
)

_LATIN = re.compile(r"[A-Za-z]")


def has_latin(text: Any) -> bool:
    return bool(_LATIN.search(str(text or "")))


def _seed(project_seed: str, source: str) -> int:
    """A stable pseudo-random index for one source name in one project.

    Hashing rather than ``random`` keeps the mapping reproducible across runs
    and processes, so regenerating one file cannot rename a character that
    other files already refer to.
    """
    digest = hashlib.sha256(f"{project_seed}\x00{source}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _pick(options: Sequence[str], value: int) -> str:
    return options[value % len(options)]


class ChineseNamer:
    """Assigns Chinese names, remembering every decision it makes."""

    def __init__(self, project_seed: str = "novelwriter"):
        self.project_seed = project_seed
        self.mapping: Dict[str, str] = {}
        self._taken: set[str] = set()

    def person(
        self, source: str, gender: Optional[str] = None, surname: str = ""
    ) -> str:
        """A Chinese personal name for ``source``, stable and unique.

        ``surname`` forces the family name, so blood relatives can share one.
        """
        source = str(source or "").strip()
        if source in self.mapping:
            return self.mapping[source]
        key = str(gender or "").strip().lower()
        key = key if key in _GIVEN else ""
        base = _seed(self.project_seed, source)
        for attempt in range(200):
            value = base + attempt * 0x9E3779B1
            surname = surname or _pick(SURNAMES, value)
            given = _pick(_GIVEN[key], value >> 8)
            if (value >> 16) % 3:  # two-character given names are the common case
                second = _pick(_GIVEN_SECOND[key], value >> 24)
                if second != given:
                    given += second
            candidate = f"{surname}{given}"
            if candidate not in self._taken:
                self._taken.add(candidate)
                self.mapping[source] = candidate
                return candidate
        # Every combination collided, which needs a name rather than a crash.
        fallback = f"{_pick(SURNAMES, base)}{len(self._taken) + 1}"
        self._taken.add(fallback)
        self.mapping[source] = fallback
        return fallback

    def city(self, source: str) -> str:
        source = str(source or "").strip()
        if source in self.mapping:
            return self.mapping[source]
        base = _seed(self.project_seed, source)
        for attempt in range(120):
            value = base + attempt * 0x85EBCA6B
            candidate = _pick(_CITY_FIRST, value) + _pick(_CITY_SECOND, value >> 8)
            if candidate not in self._taken:
                self._taken.add(candidate)
                self.mapping[source] = candidate
                return candidate
        fallback = f"{_pick(_CITY_FIRST, base)}城{len(self._taken) + 1}"
        self.mapping[source] = fallback
        return fallback

    def organization(self, source: str, city: str = "") -> str:
        source = str(source or "").strip()
        if source in self.mapping:
            return self.mapping[source]
        lowered = source.lower()
        kind = next(
            (name for name, words in _ORG_KEYWORDS if any(word in lowered for word in words)),
            "company",
        )
        base = _seed(self.project_seed, source)
        templates = _ORG_TEMPLATES[kind]
        for attempt in range(120):
            value = base + attempt * 0xC2B2AE35
            candidate = _pick(templates, value).format(city=city or self.city("默认城市"))
            if candidate not in self._taken:
                self._taken.add(candidate)
                self.mapping[source] = candidate
                return candidate
        fallback = f"{city}第{len(self._taken) + 1}调查组"
        self.mapping[source] = fallback
        return fallback

    def rewrite(self, text: str) -> str:
        """Replace every already-assigned source name inside free text.

        Longest first, so "Gary White" is not half-replaced by "Gary".
        """
        if not text:
            return text
        for source in sorted(self.mapping, key=len, reverse=True):
            if source:
                text = text.replace(source, self.mapping[source])
        return text


# Keys whose value names an organisation or place, not a person.
_ORG_KEYS = frozenset({"agency", "faction", "organization", "employer", "firm"})
_PLACE_KEYS = frozenset({"headquarters", "territory", "capital", "location", "hometown", "birthplace"})
# Direct name fields the generators put on the character itself.
_PERSON_KEYS = frozenset({"name", "spouse_name", "father_name", "mother_name", "partner_name"})


# Keys whose value names an organisation or place, not a person.
_ORG_KEYS = frozenset({"agency", "faction", "organization", "employer", "firm"})
_PLACE_KEYS = frozenset({"headquarters", "territory", "capital", "location", "hometown", "birthplace"})
# Direct name fields the generators put on a person record.
_PERSON_KEYS = frozenset({"name", "spouse_name", "father_name", "mother_name", "partner_name"})


def localize_characters(characters: Iterable[Any], namer: ChineseNamer) -> List[Any]:
    """Rename a cast, however deeply the generator nested its family trees.

    The generators disagree about shape — some keep relatives in
    ``family.parents[]``, others in ``siblings[]`` — so this walks the record
    rather than naming fields.  Relatives matter: a Chinese protagonist whose
    father is still "Gary Storm" is worse than leaving the whole cast English.

    Only values under known name keys are touched.  Free-text lists such as
    ``goals`` and ``flaws`` are full of English sentences that are emphatically
    not names, and rewriting them would gut the character sheet.
    """
    people = list(characters)
    for person in people:
        _localize_person_record(person, namer)
    return people


def _fields(record: Any) -> Optional[Dict[str, Any]]:
    if isinstance(record, dict):
        return record
    if hasattr(record, "__dict__"):
        return record.__dict__
    return None


# Relations that keep their own family name in Chinese usage.
_KEEPS_OWN_SURNAME = ("mother", "母", "wife", "妻", "spouse", "配偶", "partner")


def _localize_person_record(
    record: Any,
    namer: ChineseNamer,
    owner_name: str = "",
    family_surname: str = "",
) -> None:
    items = _fields(record)
    if items is None:
        return
    gender = items.get("gender")
    relation = str(items.get("relation", "")).lower()
    inherits = family_surname and not any(
        word in relation for word in _KEEPS_OWN_SURNAME
    )
    for key, value in list(items.items()):
        if isinstance(value, str) and has_latin(value):
            if key in _ORG_KEYS:
                items[key] = namer.organization(value)
            elif key in _PLACE_KEYS:
                items[key] = namer.city(value)
            elif key in _PERSON_KEYS:
                items[key] = _distinct_person(
                    namer, value, gender, owner_name,
                    surname=family_surname if inherits else "",
                )
            continue
        # Nested people only: a list of strings is descriptive text, not a cast.
        if not isinstance(value, (dict, list)):
            continue
        own_name = str(items.get("name", ""))
        surname = family_surname or (own_name[0] if own_name else "")
        children = value.values() if isinstance(value, dict) else value
        for item in ([value] if isinstance(value, dict) else children):
            if isinstance(item, list):
                for nested in item:
                    if _fields(nested) is not None:
                        _localize_person_record(
                            nested, namer, owner_name or own_name, surname
                        )
            elif _fields(item) is not None:
                _localize_person_record(item, namer, owner_name or own_name, surname)


def _distinct_person(
    namer: ChineseNamer, source: str, gender: Any, owner_name: str, surname: str = ""
) -> str:
    """A relative must not end up sharing the character's own name.

    Some generators hand a character and their father the identical English
    name, and a mapping keyed only by the source string would faithfully
    reproduce that mistake in Chinese.
    """
    assigned = namer.person(source, gender, surname)
    if owner_name and assigned == owner_name:
        assigned = namer.person(f"{source}#{owner_name}", gender, surname)
    return assigned


def localize_factions(factions: Iterable[Any], namer: ChineseNamer, city: str = "") -> List[Any]:
    """Rename organizations and the places they hold."""
    groups = list(factions)
    for faction in groups:
        for field in ("name", "faction_name", "organization"):
            current = _get(faction, field)
            if current and has_latin(current):
                _set(faction, field, namer.organization(current, city))
        for field in ("territory", "headquarters", "capital", "location"):
            current = _get(faction, field)
            if current and has_latin(current):
                _set(faction, field, namer.city(current))
    return groups


def _get(item: Any, field: str) -> Any:
    if isinstance(item, dict):
        return item.get(field)
    return getattr(item, field, None)


def _set(item: Any, field: str, value: Any) -> None:
    if isinstance(item, dict):
        item[field] = value
    else:
        setattr(item, field, value)


NAME_MAP_FILENAME = "name_mapping.json"


def _mapping_path(output_dir: str) -> str:
    return os.path.join(output_dir, "story", "lore", NAME_MAP_FILENAME)


def load_namer(output_dir: str) -> ChineseNamer:
    """The project's namer, with every decision it has already made.

    Characters and factions are generated by separate buttons, and either can
    be rerun on its own; a namer that forgot its earlier choices would rename
    the cast behind the back of files that already reference them.
    """
    namer = ChineseNamer(project_seed=os.path.abspath(output_dir or "."))
    path = _mapping_path(output_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return namer
    if isinstance(stored, dict):
        mapping = stored.get("names", stored)
        if isinstance(mapping, dict):
            namer.mapping = {str(k): str(v) for k, v in mapping.items()}
            namer._taken = set(namer.mapping.values())
    return namer


def save_namer(namer: ChineseNamer, output_dir: str) -> str:
    """Persist the mapping so later stages resolve the same names."""
    path = _mapping_path(output_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"seed": namer.project_seed, "names": namer.mapping}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
    return path
