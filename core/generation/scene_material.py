"""写一场戏时给模型的设定材料：由代码按本场挑，整块取舍。

以前每一场都带整份世界设定（登记表、世界观正文、全部人物卡、全部势力卡、所有人物背景），
后面又把全部人物、全部势力各列一遍。现在只带这一场用得上的，按重要性排：

1. 名称与规则登记表（必须遵守）：年代、技术水平、世界规则，和全书登记过的人物、势力、
   地点名字。本场没出场的人物只在这里留一行名字和身份。
2. 本场人物（必须遵守）：场景规划里点到名字的人物，加上本章契约点名的（承担代价的人物、
   人物变化、第一场时本章开场在场的人物）。一个都认不出时带主角。
3. 相关势力（必须遵守）：本场人物所属的、场景规划点到名字的、地盘就在规划点到的地点的。
4. 世界观正文（必须遵守）。
5. 本场人物的详细背景（参考）。

超出篇幅时从最后一块起整块去掉（前两块总是带），去掉的列在材料末尾。

有意不带的：

- 当前幕的故事结构和人物卡里的成长方向（arc）：写的都是后面才发生的事。写场景的提示词
  一直在防模型提前写掉后面的事（下一场只给标题，后面场次列成禁区），把这些递过去等于先
  把后面的情节交出去。
- 本章没碰的悬念：本章契约已经写明这一章推进哪几条，再列别的只会招模型去动不该动的线。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from core.generation.chapter_continuity import CONTINUITY_KEY
from core.generation.lore_repository import (
    BODY,
    CHARACTERS,
    CONTRACT,
    FACTIONS,
    LORE,
    LoreEditError,
    background_name,
    lore_files,
    name_of,
    records,
)
from core.generation.planning_contract import contract_for_scene
from core.generation.prompt_context import sanitize_lore_content
from core.generation.prompt_material import MaterialBlock, fit_blocks
from core.lore_localization import describe_value, display_value, file_label

#: 写一场戏的设定材料总字数上限。
SCENE_BUDGET = 30000

#: 人物卡里不带进写场景材料的字段：成长方向写的是后面几幕的事。
FUTURE_FIELDS = ("arc",)

_NAME_KEYS = ("name", "character_name", "full_name", "faction_name", "title")
_REGISTRY_LISTS = (
    ("canonical_characters", "人物"),
    ("canonical_factions", "势力"),
    ("canonical_locations", "地点"),
)
_REGISTRY_SKIPPED = {"schema_version", "origin"}


class SceneMaterial(str):
    """写一场戏的设定材料（就是一段文字），另带三项说明。

    characters、factions：这次带了卡片的人物和势力；omitted：篇幅放不下、这次没带的几块。
    """

    characters: Tuple[str, ...] = ()
    factions: Tuple[str, ...] = ()
    omitted: Tuple[str, ...] = ()


def build_scene_material(
    output_dir: str,
    scene_plan: str,
    *,
    contract: Optional[Dict[str, Any]] = None,
    scene_number: Optional[int] = None,
    max_chars: int = SCENE_BUDGET,
) -> SceneMaterial:
    """按本场挑出的设定材料。作品没有 story/lore/ 下的设定文件时返回空文字。"""
    files = dict(lore_files(output_dir))
    if not files:
        return SceneMaterial("")
    registry = _json(files.get(CONTRACT))
    registry = registry if isinstance(registry, dict) else {}
    cast = _cards(files.get(CHARACTERS), "characters")
    faction_cards = _cards(files.get(FACTIONS), "factions")

    chosen, fallback = pick_characters(scene_plan, cast, registry, contract, scene_number)
    factions = pick_factions(scene_plan, chosen, faction_cards, registry)

    blocks: List[MaterialBlock] = []

    def add(key, label, text, authority, required=False, demote=True):
        if str(text or "").strip():
            blocks.append(MaterialBlock(key, label, str(text).strip(), required=required,
                                        authority=authority, demote=demote))

    if CONTRACT in files:
        add(CONTRACT, file_label(CONTRACT),
            registry_text(registry) if registry else files[CONTRACT], "必须遵守", required=True)
    if chosen:
        note = "场景规划里没有认出登记过的人物，先带主角的资料。\n\n" if fallback else ""
        add("characters", "本场人物", note + "\n\n".join(card_text(card) for card in chosen),
            "必须遵守", required=True, demote=False)
    if factions:
        add("factions", "相关势力", "\n\n".join(card_text(card) for card in factions),
            "必须遵守", demote=False)
    if BODY in files:
        add(BODY, file_label(BODY), sanitize_lore_content(files[BODY]), "必须遵守")
    for card in chosen:
        name = background_name(card)
        for key in (LORE + name, LORE + "backgrounds/" + name):
            if key in files:
                add(key, file_label(key), files[key], "参考")
                break

    text, _kept, omitted = fit_blocks(blocks, max_chars)
    material = SceneMaterial(text)
    material.characters = tuple(name_of(card) for card in chosen)
    material.factions = tuple(name_of(card) for card in factions)
    material.omitted = tuple(block.label for block in omitted)
    return material


def pick_characters(
    scene_plan: str,
    cast: Sequence[Dict[str, Any]],
    registry: Dict[str, Any],
    contract: Optional[Dict[str, Any]] = None,
    scene_number: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], bool]:
    """本场要带卡片的人物（按人物资料里的先后），以及是不是一个都没认出、退回了主角。"""
    terms = name_terms(cast, _entries(registry, "canonical_characters"))
    named = set(mentioned(scene_plan, terms)) | set(mentioned(_contract_names(contract, scene_number), terms))
    chosen = [card for card in cast if name_of(card) in named]
    if chosen or not cast:
        return chosen, False
    leads = [card for card in cast if str(card.get("role", "")).strip().lower() == "protagonist"]
    return leads or list(cast[:1]), True


def pick_factions(
    scene_plan: str,
    chosen: Sequence[Dict[str, Any]],
    faction_cards: Sequence[Dict[str, Any]],
    registry: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """本场人物所属的、场景规划点到名字的、地盘就在规划点到的地点的势力（按势力资料里的先后）。"""
    faction_terms = name_terms(faction_cards, _entries(registry, "canonical_factions"))
    place_terms = name_terms((), _entries(registry, "canonical_locations"))
    affiliations = "\n".join(_flat(card.get("faction")) for card in chosen)
    named = set(mentioned(scene_plan, faction_terms)) | set(mentioned(affiliations, faction_terms))
    places = set(mentioned(scene_plan, place_terms))
    picked = []
    for card in faction_cards:
        territory = _flat(card.get("territory")) + "\n" + _flat(card.get("territories"))
        if name_of(card) in named or places & set(mentioned(territory, place_terms)):
            picked.append(card)
    return picked


def name_terms(
    cards: Iterable[Dict[str, Any]], entries: Iterable[Dict[str, Any]]
) -> Dict[str, str]:
    """名字和别名 → 规范名。登记表里的别名算同一个；卡片自己带的别名也算。

    给了卡片就只认卡片上的人物或势力；没给（地点没有卡片）就认登记表里的全部条目。
    """
    cards = list(cards)
    terms: Dict[str, str] = {}
    aliases: Dict[str, List[str]] = {}
    for entry in entries:
        name = str(entry.get("name", "")).strip()
        aliases.setdefault(name, []).extend(_strings(entry.get("aliases")))
    names = [name_of(card) for card in cards] if cards else list(aliases)
    by_name = {name_of(card): card for card in cards}
    for name in names:
        for term in (name, *aliases.get(name, []), *_strings((by_name.get(name) or {}).get("aliases"))):
            term = term.strip()
            if term:
                terms.setdefault(term, name)
    return terms


def mentioned(text: Any, terms: Dict[str, str]) -> List[str]:
    """text 里点到的名字（别名算同一个），按第一次出现的先后。

    长名字先认，认过的字不再算给短名字：「白螺里七号楼」不会再算一次「白螺里」。
    一个字的名字和别名不认，太容易撞上别的词。
    """
    text = str(text or "")
    taken = bytearray(len(text))
    first: Dict[str, int] = {}
    for term in sorted(terms, key=len, reverse=True):
        if len(term) < 2:
            continue
        start = text.find(term)
        while start != -1:
            end = start + len(term)
            if not any(taken[start:end]):
                taken[start:end] = b"\x01" * len(term)
                name = terms[term]
                first[name] = min(first.get(name, start), start)
            start = text.find(term, start + 1)
    return sorted(first, key=first.get)


def registry_text(registry: Dict[str, Any]) -> str:
    """名称与规则登记表写成中文条目；编号不写，模型写正文用不上。"""
    lines: List[str] = []
    for key, label in (("era", "年代"), ("technology_level", "技术水平")):
        value = registry.get(key)
        if value not in (None, "", [], {}):
            lines.append(f"{label}：{_scalar(value)}")
    rules = registry.get("world_constraints")
    if rules:
        lines.append("世界规则：")
        lines.extend(f"- {_scalar(rule)}" for rule in (rules if isinstance(rules, list) else [rules]))
    for key, label in _REGISTRY_LISTS:
        entries = _entries(registry, key)
        if not entries:
            continue
        lines.append(f"{label}：")
        for entry in entries:
            alias = _strings(entry.get("aliases"))
            line = str(entry["name"]).strip() + (f"（又称{'、'.join(alias)}）" if alias else "")
            role = entry.get("role")
            if role:
                line += f"：{display_value(role)}"
            lines.append(f"- {line}")
    known = {"era", "technology_level", "world_constraints", *(key for key, _ in _REGISTRY_LISTS)}
    rest = {key: value for key, value in registry.items() if key not in known | _REGISTRY_SKIPPED}
    if rest:
        lines.append(describe_value(rest))
    return "\n".join(lines)


def card_text(card: Dict[str, Any]) -> str:
    """一张人物或势力卡：名字做小标题，其余写成中文条目；成长方向不带。"""
    name = name_of(card)
    name_key = next((key for key in _NAME_KEYS if isinstance(card.get(key), str)), None)
    rest = {key: value for key, value in card.items() if key != name_key and key not in FUTURE_FIELDS}
    return f"### {name}\n{describe_value(rest)}" if rest else f"### {name}"


def _contract_names(contract: Optional[Dict[str, Any]], scene_number: Optional[int]) -> str:
    """契约里点名的人物：承担代价的人物、到本场为止的人物变化，第一场另加本章开场在场的人物。"""
    if not isinstance(contract, dict):
        return ""
    scoped = contract_for_scene(contract, scene_number) or {}
    names: List[Any] = [scoped.get("cost_character")]
    names += [item.get("character") for item in scoped.get("character_updates") or [] if isinstance(item, dict)]
    if not scene_number or int(scene_number) <= 1:
        continuity = scoped.get(CONTINUITY_KEY)
        if isinstance(continuity, dict):
            names += [item.get("character") for item in continuity.get("opening_positions") or []
                      if isinstance(item, dict)]
    return "\n".join(str(name) for name in names if name)


def _json(text: Optional[str]) -> Any:
    try:
        return json.loads(text) if text else None
    except (TypeError, ValueError):
        return None


def _cards(text: Optional[str], kind: str) -> List[Dict[str, Any]]:
    payload = _json(text)
    if payload is None:
        return []
    try:
        items = records(payload, kind)
    except LoreEditError:
        return []
    return [item for item in items if isinstance(item, dict) and name_of(item)]


def _entries(registry: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    value = registry.get(key) if isinstance(registry, dict) else None
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict) and str(entry.get("name", "")).strip()]


def _strings(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if isinstance(item, str) and item.strip()]
    return []


def _flat(value: Any) -> str:
    """地盘、所属这类字段可能是文字、列表或字典，摊平成一段文字拿来认名字。"""
    if isinstance(value, dict):
        return "\n".join(_flat(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return "\n".join(_flat(item) for item in value)
    return str(value or "")


def _scalar(value: Any) -> str:
    return display_value(value) if not isinstance(value, (dict, list)) else describe_value(value)
