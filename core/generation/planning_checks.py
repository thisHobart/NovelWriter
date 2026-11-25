# -*- coding: utf-8 -*-
"""规划结果的几项不花调用的判断。

现在有两项。

一、章节大纲和场景规划里点到的人名、地名，名称与规则登记表里有没有。认名字的办法
和写场景材料挑人物时一样（别名算同一个，长名字先认）。

- 人名：从「涉及人物」「出场人物」这两栏里取。认不出登记过的名字、又像个人名（两到四个
  汉字，不带「指挥官」「守卫」这类身份词）的，算登记表里没有。「宏升装卸指挥官」这类身份
  称呼不算。
- 地名：从「具体地点」「环境与具体地点」这两栏里取，只看写成「城市·具体地点」的：「·」前面
  那一段认不出登记过的地点，才算登记表里没有。「临山·废弃矿区实验室」里的具体位置本来就
  不登记。

二、每章场景数在不在建议范围里。长篇的范围照样例作品的基线先定成每章 2–4 场
（``LONG_FORM_SCENE_RANGE``），短篇用「规划场景」提示词里要求的 3–6 场。

两项现在都只计数、记进运行记录，不打回：「生成章节大纲」「规划场景」收下一份大纲或规划
之后把结果写进运行记录（``name_note``、``scene_count_note``），不花调用的规划检查
（``tools/check_planning.py``）用同一套函数计数，两边不会各判各的。要不要改成打回，等作者
看过误报再定（``docs/novel/chapter_planning_goal.md`` 第八节）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Tuple

from core.generation.lore_repository import CONTRACT, lore_files
from core.generation.scene_material import mentioned, name_terms

#: 长篇每章场景数的建议范围。样例作品 30 章是 2–4 场（前 10 章 2 场，后面 3 场，一章 4 场）；
#: 作者确认前只记进运行记录，不打回。
LONG_FORM_SCENE_RANGE: Tuple[int, int] = (2, 4)

PEOPLE_FIELDS = ("涉及人物", "出场人物")
PLACE_FIELDS = ("环境与具体地点", "具体地点")

_FIELD_LINE = re.compile(
    r"^[\s*>\-]*(?:\d+[.、]\s*)?\**\s*(" + "|".join(PEOPLE_FIELDS + PLACE_FIELDS)
    + r")\s*\**\s*[:：]\s*\**\s*(.+)$",
    re.M,
)
_PARENTHESES = re.compile(r"（[^（）]*）|\([^()]*\)|【[^【】]*】|\[[^\[\]]*\]")
_SEPARATORS = re.compile(r"[、，,；;／/]|\s+及\s+")
_CJK_NAME = re.compile(r"^[一-鿿·]{2,4}$")

#: 带这些字样的是身份称呼，不是人名。
ROLE_WORDS = (
    "法官", "检察官", "警察", "警员", "警官", "守卫", "杀手", "司机", "护士", "医生", "保安",
    "记者", "指挥官", "指挥", "队长", "队员", "组长", "头目", "人员", "士兵", "律师", "秘书",
    "清道夫", "特勤", "主任", "局长", "经理", "老板", "代表", "工人", "成员", "嫌疑人", "证人",
    "狱警", "法警", "书记员", "死者", "受害者", "父母", "妻子", "丈夫", "母亲", "父亲", "女儿",
    "儿子", "家属", "众人", "群众", "路人", "围观", "手下", "部下", "打手", "保镖", "专家",
    "技术员", "调查员", "法医", "幕后", "高层", "管理层", "董事", "职员", "员工", "居民", "住户",
    "邻居", "同事", "店主", "老人", "孩子", "少年", "少女", "男子", "女子", "男人", "女人",
    "公诉人", "人质", "平民", "死士", "看守", "囚犯", "犯人", "医护", "警卫", "特警", "探员",
)


@dataclass
class NameFindings:
    """登记表里没有的人名、地名（各自去重，按第一次出现的先后）。"""

    people: List[str] = field(default_factory=list)
    places: List[str] = field(default_factory=list)

    def merge(self, other: "NameFindings") -> "NameFindings":
        for item in other.people:
            if item not in self.people:
                self.people.append(item)
        for item in other.places:
            if item not in self.places:
                self.places.append(item)
        return self


def load_registry(output_dir: str) -> Dict[str, Any]:
    """作品的名称与规则登记表；没有或读不出来时是空字典。"""
    text = dict(lore_files(output_dir)).get(CONTRACT)
    try:
        registry = json.loads(text) if text else {}
    except (TypeError, ValueError):
        return {}
    return registry if isinstance(registry, dict) else {}


def listed_names(text: str) -> Tuple[List[str], List[str]]:
    """大纲或场景规划里列出的人物、地点：按「涉及人物：」「具体地点：」这些栏取。"""
    people: List[str] = []
    places: List[str] = []
    for match in _FIELD_LINE.finditer(str(text or "")):
        label, value = match.group(1), match.group(2)
        target = people if label in PEOPLE_FIELDS else places
        for item in _items(value):
            if item not in target:
                target.append(item)
    return people, places


def unregistered_names(text: str, registry: Dict[str, Any]) -> NameFindings:
    """text 里列出的人名、地名中，登记表里没有的。登记表是空的就不判，返回空结果。"""
    people, places = listed_names(text)
    found = NameFindings()
    person_terms = name_terms((), _entries(registry, "canonical_characters"))
    place_terms = name_terms((), _entries(registry, "canonical_locations"))
    if person_terms:
        for item in people:
            if mentioned(item, person_terms) or not looks_like_a_name(item):
                continue
            if item not in found.people:
                found.people.append(item)
    if place_terms:
        for item in places:
            if "·" not in item or mentioned(item, place_terms):
                continue
            city = item.split("·", 1)[0].strip()
            if city and not mentioned(city, place_terms) and city not in found.places:
                found.places.append(city)
    return found


def name_note(findings: NameFindings) -> str:
    """登记表里没有的人名、地名写成一句（“人名：陆巡；地名：雾港”）；一个都没有时是空文字。"""
    parts = []
    if findings.people:
        parts.append("人名：" + "、".join(findings.people))
    if findings.places:
        parts.append("地名：" + "、".join(findings.places))
    return "；".join(parts)


def scene_count_note(count: int, scene_range: Tuple[int, int]) -> str:
    """场景数不在范围里时写成一句（“共 5 场，不在建议的 2–4 场之内”）；在范围里是空文字。"""
    low, high = scene_range
    if low <= int(count) <= high:
        return ""
    return f"共 {int(count)} 场，不在建议的 {low}–{high} 场之内"


def looks_like_a_name(item: str) -> bool:
    """两到四个汉字、不带身份词的，像个人名。"""
    return bool(_CJK_NAME.match(item)) and not any(word in item for word in ROLE_WORDS)


def _items(value: str) -> Iterable[str]:
    value = _PARENTHESES.sub("", value)
    value = re.split(r"[。！？]", value, maxsplit=1)[0]
    for raw in _SEPARATORS.split(value):
        item = raw.strip(" \t*：:“”\"'.。")
        item = re.sub(r"等(?:人|地)?$", "", item).strip()
        if item:
            yield item


def _entries(registry: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    value = registry.get(key) if isinstance(registry, dict) else None
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict) and str(entry.get("name", "")).strip()]
