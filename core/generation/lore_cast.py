# -*- coding: utf-8 -*-
"""按题材与故事前提直接生成中文人物卡与势力卡。

## 这个模块替代了什么

在此之前，人物与势力来自 `Generators/` 里按题材写死的英文词表，字段是随机抽取的：
`goals` 从十来条英文短语里抽一条、`profession` 从另一张表里抽一条，彼此之间、以及
与这本书的前提之间都没有关系。名字随后被中文名替换，其余字段原样留在卡上。真实
项目里因此出现过三类硬伤：

* **卡片自相矛盾**：势力名是「海陵岸线风险咨询」（民营风控公司），`type` 却写着
  `Police Department`，`jurisdiction` 写着 `Federal`。模型只能在背景故事里用括号
  硬圆成「（公权力刑侦机构）」。
* **抽象类别被当成地名**：势力模板里的 `territories` 写的是「Court and legal
  systems」「Information gathering networks」这种描述性短语，改名环节把它们当成
  地名，于是全书的地理里多出了叫「雾平」「榆州」的城市。
* **反派与主角同向**：反派抽到的目标是「Help solve the case」——他想帮忙破案。

模型直接用中文写整张卡，上面三类问题里的前两类在结构上就不存在了（名字与类型是
同一次生成的，地点是显式的地点对象）。第三类靠校验拦。

## 为什么还要一层确定性校验

生成式的一步没办法保证不出错，但**落盘前**可以按确定的规则判。这里只判那些
「一眼能看出是错的、且下游要付出昂贵代价」的事，判据全部不花调用：

* 字段不得为空——旧数据里 `description` 一直是空字符串；
* 正文可见字段不得残留拉丁字母；
* 势力的 `nature` 是公权力机关时，名字不得带「有限公司」「事务所」这类商业后缀；
* `territory` 必须是一个地点对象，名字不得是「……系统」「……网络」这种抽象类别；
* 反派必须显式写明他挡的是哪位主角的哪个目标，且该主角必须真的存在。

校验不通过时，失败原文原样发回去当修复指令——与设计契约、章节契约用的是同一套
重试机制（`generate_with_contract_retry`）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Sequence, Tuple

from core.generation.design_contract import (
    DesignContractError,
    generate_with_contract_retry,
)


_LATIN = re.compile(r"[A-Za-z]")


def has_latin(text: Any) -> bool:
    """卡片里还剩英文，就是这一张没按要求写完。

    以前这个判断属于 `chinese_names`——那个模块负责把英文词库生成的名字换成中文。
    卡片改成直接用中文生成之后整套改名机制都没有了（它本身就是「Criminal
    Investigation Unit」被映射成民营咨询公司的成因），只留下这一条检查。
    """
    return bool(_LATIN.search(str(text or "")))


CAST_RETRY_LIMIT = 2

#: 势力的性质。写死成枚举是为了能对名字做确定性检查：一个「公权力机关」不该叫
#: 「××商务调查」。自由书写的类型字段没法这样判。
FACTION_NATURES = ("公权力机关", "商业机构", "民间组织", "犯罪组织", "其他")

#: 带这些后缀的名字属于商业或民间主体，不能同时声称自己是公权力机关。
_COMMERCIAL_SUFFIXES = (
    "有限公司", "股份公司", "集团", "事务所", "咨询", "工作室",
    "商行", "商会", "俱乐部", "基金会",
)

#: 地点名不该是一类事物的统称。旧数据里「法院与司法系统」「情报收集网络」正是
#: 被当成地名转成了城市。
_ABSTRACT_TAILS = ("系统", "网络", "体系", "领域", "范围", "机制", "行业", "圈子")

#: 地名的字数上限。这条曾经是 12 字，实测把整个设定阶段判死过：模型写出的
#: 「市公安局旧办公楼地下一层档案室」「棉纺三厂职工宿舍四号筒子楼水房」都是 15 字，
#: 而它们恰恰是提示词要的那种具体地点。字数根本分不开地名和描述——15 字的具体地点
#: 和 16 字的机构描述一样长。真正管用的判据是上面那张后缀表；这里只拦「明显是一整
#: 句话」的情况，所以放宽到一个中文地名不可能达到的长度。
MAX_PLACE_NAME_CHARS = 25

CHARACTER_ROLES = ("protagonist", "deuteragonist", "antagonist", "supporting")

#: 每张人物卡必须写满的字段。旧词表把 description 一直留空。
_REQUIRED_CHARACTER_FIELDS = (
    "name", "role", "gender", "profession", "title",
    "goal", "motivation", "flaw", "strength", "arc", "background", "description",
)
_REQUIRED_FACTION_FIELDS = ("name", "nature", "type", "description", "goal")

_LATIN_EXEMPT_KEYS = frozenset({"id", "role", "gender"})


class CastError(DesignContractError):
    """人物或势力卡没有通过落盘前的确定性校验。"""


# --------------------------------------------------------------------- 校验

def _defect(message: str) -> str:
    return message


def _walk_strings(value: Any, path: str = "") -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            out.extend(_walk_strings(item, f"{path}.{key}" if path else str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            out.extend(_walk_strings(item, f"{path}[{index}]"))
    elif isinstance(value, str):
        out.append((path, value))
    return out


def _latin_defects(record: Dict[str, Any], label: str) -> List[str]:
    defects = []
    for path, text in _walk_strings(record):
        leaf = path.split(".")[-1].split("[")[0]
        if leaf in _LATIN_EXEMPT_KEYS:
            continue
        if has_latin(text):
            defects.append(
                _defect(f"{label} 的 {path} 里还有英文：{text[:40]!r}；所有正文可见字段必须是中文")
            )
    return defects


def _percentage(value: Any, default: float = 50.0) -> float:
    """把性别比例读成数字。

    `parameters.txt` 是纯文本，`Female Percentage: 50` 读回来是字符串 `"50"`。
    界面那条路径上有人先转过 int，无界面直接调用时没有——于是「按下按钮才炸」。
    这里在校验入口统一收口，读不出数字就退回默认值而不是让整个设定阶段崩掉。
    """
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return number if 0.0 <= number <= 100.0 else default


def _missing_fields(record: Dict[str, Any], required: Sequence[str], label: str) -> List[str]:
    return [
        _defect(f"{label} 的 {field} 是空的，必须写实际内容")
        for field in required
        if not str(record.get(field, "")).strip()
    ]


def validate_factions(factions: Sequence[Dict[str, Any]]) -> List[str]:
    """势力卡的确定性缺陷清单；空列表表示通过。"""
    defects: List[str] = []
    if not factions:
        return ["没有生成任何势力"]

    seen_names = set()
    for index, faction in enumerate(factions, 1):
        label = f"第 {index} 个势力"
        if not isinstance(faction, dict):
            defects.append(_defect(f"{label} 不是一个对象"))
            continue

        name = str(faction.get("name", "")).strip()
        defects.extend(_missing_fields(faction, _REQUIRED_FACTION_FIELDS, label))
        defects.extend(_latin_defects(faction, label))

        if name and name in seen_names:
            defects.append(_defect(f"势力名「{name}」重复了，每个势力必须有唯一的名字"))
        seen_names.add(name)

        nature = str(faction.get("nature", "")).strip()
        if nature and nature not in FACTION_NATURES:
            defects.append(
                _defect(f"{label} 的 nature 是「{nature}」，只能从 {'、'.join(FACTION_NATURES)} 里选一个")
            )
        if nature == "公权力机关":
            hit = next((s for s in _COMMERCIAL_SUFFIXES if s in name), "")
            if hit:
                defects.append(
                    _defect(
                        f"势力「{name}」的 nature 写着公权力机关，名字里却有「{hit}」——"
                        "机关和公司不是一回事，改名字或者改 nature"
                    )
                )

        territory = faction.get("territory")
        if not isinstance(territory, dict):
            defects.append(
                _defect(f"{label} 的 territory 必须是 {{\"name\": 地名, \"kind\": 类别}} 这样的对象")
            )
            continue
        place = str(territory.get("name", "")).strip()
        if not place:
            defects.append(_defect(f"{label} 的 territory.name 是空的"))
        elif any(place.endswith(tail) for tail in _ABSTRACT_TAILS):
            defects.append(
                _defect(
                    f"{label} 的 territory.name 是「{place}」，这是一类事物的统称，不是地名。"
                    "写一个具体地点，比如某个城区、码头、大楼或街道"
                )
            )
        elif len(place) > MAX_PLACE_NAME_CHARS:
            defects.append(
                _defect(
                    f"{label} 的 territory.name 是「{place}」，这是一整句描述，不是地名。"
                    f"写出这个地方叫什么，不超过 {MAX_PLACE_NAME_CHARS} 个字"
                )
            )
    return defects


def storage_factions(factions: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把校验用的结构摊平成下游一直在读的形状。

    `territory` 在生成与校验阶段是 `{name, kind}`——只有拆开才判得了「这是地名
    还是一类事物的统称」。但 `prompt_context` 与 `structure_pipeline` 一直把它
    当字符串读，所以落盘时摊回字符串，类别单独放在 `territory_kind`。
    """
    stored = []
    for faction in factions:
        item = dict(faction)
        territory = item.get("territory")
        if isinstance(territory, dict):
            item["territory"] = str(territory.get("name", "")).strip()
            kind = str(territory.get("kind", "")).strip()
            if kind:
                item["territory_kind"] = kind
        stored.append(item)
    return stored


def validate_relationships(
    relationships: Sequence[Dict[str, Any]],
    characters: Sequence[Dict[str, Any]],
) -> List[str]:
    """人物关系的确定性缺陷清单。

    旧数据里的关系是在人物两两配对上随机贴标签的，于是同一条里 type 写着
    「Suspect and detective」、description 写着「They support each other's goals」，
    两位搭档警察被标成「Victim and perpetrator」。这里判的是能判的那几件：
    两端必须真的存在、不能自己和自己、同一对不能出现两次。
    """
    defects: List[str] = []
    names = {str(c.get("name", "")).strip() for c in characters if isinstance(c, dict)}
    seen = set()
    for index, item in enumerate(relationships, 1):
        label = f"第 {index} 条人物关系"
        if not isinstance(item, dict):
            defects.append(_defect(f"{label} 不是一个对象"))
            continue
        first = str(item.get("character1", "")).strip()
        second = str(item.get("character2", "")).strip()
        for who in (first, second):
            if who not in names:
                defects.append(
                    _defect(f"{label} 里的「{who}」不在人物名单中（{'、'.join(sorted(names))}）")
                )
        if first and first == second:
            defects.append(_defect(f"{label} 的两端是同一个人「{first}」"))
        pair = frozenset({first, second})
        if pair in seen:
            defects.append(_defect(f"「{first}」和「{second}」的关系写了不止一条"))
        seen.add(pair)
        defects.extend(_missing_fields(item, ("type", "description"), label))
        defects.extend(_latin_defects(item, label))
    return defects


def validate_characters(
    characters: Sequence[Dict[str, Any]],
    *,
    female_percentage: int = 50,
    tolerance: float = 0.25,
) -> List[str]:
    """人物卡的确定性缺陷清单；空列表表示通过。"""
    female_percentage = _percentage(female_percentage)
    defects: List[str] = []
    if not characters:
        return ["没有生成任何人物"]

    seen_names = set()
    protagonists: List[Dict[str, Any]] = []
    antagonists: List[Dict[str, Any]] = []

    for index, person in enumerate(characters, 1):
        label = f"第 {index} 位人物"
        if not isinstance(person, dict):
            defects.append(_defect(f"{label} 不是一个对象"))
            continue

        name = str(person.get("name", "")).strip()
        defects.extend(_missing_fields(person, _REQUIRED_CHARACTER_FIELDS, label))
        defects.extend(_latin_defects(person, label))

        if name and name in seen_names:
            defects.append(_defect(f"人物名「{name}」重复了，每个人物必须有唯一的名字"))
        seen_names.add(name)

        role = str(person.get("role", "")).strip()
        if role and role not in CHARACTER_ROLES:
            defects.append(
                _defect(f"{label}（{name}）的 role 是「{role}」，只能从 {'、'.join(CHARACTER_ROLES)} 里选一个")
            )
        if role in ("protagonist", "deuteragonist"):
            protagonists.append(person)
        elif role == "antagonist":
            antagonists.append(person)

        age = person.get("age")
        if not isinstance(age, int) or not (0 < age < 120):
            defects.append(_defect(f"{label}（{name}）的 age 必须是 1 到 119 之间的整数"))

    if not protagonists:
        defects.append("没有任何人物的 role 是 protagonist")
    if not antagonists:
        defects.append("没有任何人物的 role 是 antagonist")

    # 反派必须挡住主角。旧词表里出现过反派的目标是「帮忙破案」——他和主角想要
    # 的是同一件事，整本书就没有对抗可写了。
    protagonist_names = {str(p.get("name", "")).strip() for p in protagonists}
    for person in antagonists:
        name = str(person.get("name", "")).strip()
        opposes = person.get("opposes")
        if not isinstance(opposes, dict):
            defects.append(
                _defect(
                    f"反派「{name}」缺少 opposes：必须写成 "
                    "{\"character\": 主角名, \"blocked_goal\": 他挡住的那个目标}"
                )
            )
            continue
        target = str(opposes.get("character", "")).strip()
        blocked = str(opposes.get("blocked_goal", "")).strip()
        if target not in protagonist_names:
            defects.append(
                _defect(
                    f"反派「{name}」的 opposes.character 是「{target}」，"
                    f"不在主角名单里（{'、'.join(sorted(protagonist_names))}）"
                )
            )
        if not blocked:
            defects.append(_defect(f"反派「{name}」的 opposes.blocked_goal 是空的"))

    genders = [str(p.get("gender", "")).strip() for p in characters if isinstance(p, dict)]
    known = [g for g in genders if g in ("男", "女")]
    if len(known) != len(genders):
        defects.append("每位人物的 gender 必须是「男」或「女」")
    elif known:
        actual = sum(1 for g in known if g == "女") / len(known)
        wanted = female_percentage / 100.0
        if abs(actual - wanted) > tolerance:
            defects.append(
                _defect(
                    f"女性占比 {actual:.0%}，与设定的 {wanted:.0%} 相差过大"
                    f"（允许 {tolerance:.0%} 以内）"
                )
            )
    return defects


# --------------------------------------------------------------------- 生成

def _premise_block(parameters: Dict[str, Any]) -> str:
    def value(*keys: str) -> str:
        for key in keys:
            item = str(parameters.get(key, "") or "").strip()
            if item:
                return item
        return "未指定"

    return "\n".join(
        [
            f"题材：{value('Genre', 'genre')} · {value('Subgenre', 'subgenre')}",
            f"书名：{value('Novel Title', 'title')}",
            f"主题：{value('Theme', 'theme')}",
            f"基调：{value('Tone', 'tone')}",
            f"篇幅：{value('Story Length', 'story_length')}",
        ]
    )


def _parse_json_list(response: str, key: str) -> List[Dict[str, Any]]:
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", response.strip(), flags=re.IGNORECASE)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as error:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise CastError(f"返回的内容不是 JSON：{error}", code="cast_not_json") from error
        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as inner:
            raise CastError(f"返回的内容不是 JSON：{inner}", code="cast_not_json") from inner
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict) and isinstance(data.get(key), list):
        return [item for item in data[key] if isinstance(item, dict)]
    raise CastError(f"JSON 里没有 {key} 数组", code="cast_missing_key")


def _run(send, prompt: str, key: str, validate, on_retry=None):
    def parse(response: str):
        records = _parse_json_list(response, key)
        defects = validate(records)
        if defects:
            raise CastError("；".join(defects[:8]), code="cast_invalid")
        return response, {key: records}

    try:
        _, payload = generate_with_contract_retry(
            send, prompt, parse, retry_limit=CAST_RETRY_LIMIT, on_retry=on_retry
        )
    except CastError:
        raise
    except DesignContractError as error:
        # 重试耗尽时抛的是基类；调用方按「卡片没通过校验」处理，类型该是一致的。
        raise CastError(str(error), code=getattr(error, "code", "cast_invalid")) from error
    return payload[key]


def generate_factions(
    send: Callable[[str], str],
    parameters: Dict[str, Any],
    num_factions: int,
    *,
    on_retry: Callable[[int, Exception], None] | None = None,
) -> List[Dict[str, Any]]:
    """按故事前提写出 `num_factions` 张中文势力卡。"""
    prompt = f"""请为下面这部中文小说设计 {num_factions} 个势力（组织、机构或团体）。

{_premise_block(parameters)}

要求：
- 全部用中文写。名字要像这个题材、这个时代真实存在的机构，不要英文原名，也不要音译。
- nature 只能从这几个里选一个：{'、'.join(FACTION_NATURES)}。
  名字必须和 nature 对得上——公权力机关不能叫「××商务调查」或「××咨询」。
- territory 是这个势力活动的**具体地点**，不是一类事物的统称。
  「市法院大楼」「北岸三号码头」「棉纺三厂四号筒子楼」是地点；
  「法院与司法系统」「情报网络」不是。地名写这个地方叫什么就行，不要写成一句
  描述，不超过 25 个字。
- 势力之间要有明确的利害关系：谁挡了谁的路，写进 conflicts。
- 每个势力都要和上面的主题、基调有关，不要写成通用模板。

只输出 JSON：
{{"factions": [
  {{
    "name": "海陵市公安局刑事侦查支队",
    "nature": "公权力机关",
    "type": "市级刑侦机构",
    "description": "一句话说清它是干什么的、现在处境如何",
    "goal": "它在这本书里想达成什么",
    "resources": ["它手上有什么"],
    "territory": {{"name": "海陵老城区", "kind": "城区"}},
    "conflicts": [{{"with": "另一个势力的名字", "over": "为什么冲突"}}]
  }}
]}}"""
    return _run(send, prompt, "factions", validate_factions, on_retry)


def _character_prompt(
    parameters: Dict[str, Any],
    num_characters: int,
    female_percentage: Any,
    factions: Sequence[Dict[str, Any]],
) -> str:
    female_percentage = _percentage(female_percentage)
    faction_names = [str(f.get("name", "")).strip() for f in factions if isinstance(f, dict)]
    faction_block = (
        "已有的势力（人物的 faction 只能从这里面选，或者留空表示无组织）：\n"
        + "\n".join(f" - {name}" for name in faction_names if name)
        + "\n\n"
        if any(faction_names)
        else ""
    )
    prompt = f"""请为下面这部中文小说设计 {num_characters} 位主要人物。

{_premise_block(parameters)}

{faction_block}要求：
- 全部用中文写，名字是普通的中文姓名。职业、职务要符合这个题材所在的现实体系。
- role 只能是 protagonist / deuteragonist / antagonist / supporting，
  至少要有一位 protagonist 和一位 antagonist。
- gender 只写「男」或「女」；女性大约占 {female_percentage:.0f}%。
- 每个人的 goal、flaw、background 都要来自上面的主题和基调，彼此之间要能产生冲突，
  不要写成可以套在任何一本书上的通用短语。
- **反派必须挡住主角**：antagonist 要写 opposes，说明他挡的是哪位主角的哪个目标。
  反派和主角想要同一件事，这本书就没有对抗可写了。
- description 要写实际内容，不能留空。

只输出 JSON：
{{"characters": [
  {{
    "name": "梁浩",
    "role": "protagonist",
    "gender": "男",
    "age": 44,
    "faction": "海陵市公安局刑事侦查支队",
    "profession": "刑警",
    "title": "重案队副队长",
    "goal": "他这本书里要达成的具体目标",
    "motivation": "他为什么非做不可",
    "flaw": "会让他付出代价的那个缺陷",
    "strength": "他真正擅长的事",
    "arc": "他会从什么变成什么",
    "background": "两三句前史，交代他为什么是现在这样",
    "description": "外形与给人的第一印象"
  }},
  {{
    "name": "郑娜敏",
    "role": "antagonist",
    "gender": "女",
    "age": 35,
    "faction": "海陵恒正律师事务所",
    "profession": "辩护律师",
    "title": "合伙人",
    "goal": "她要达成什么",
    "motivation": "她为什么非做不可",
    "flaw": "缺陷",
    "strength": "长处",
    "arc": "转变",
    "background": "前史",
    "description": "外形",
    "opposes": {{"character": "梁浩", "blocked_goal": "她具体挡住了梁浩的哪个目标"}}
  }}
],
 "relationships": [
  {{"character1": "梁浩", "character2": "郑娜敏",
   "type": "法庭上的对手", "description": "一句话说清他们之间发生过什么"}}
 ]}}

人物关系只写真正影响故事的那几对：两端都必须是上面出现过的人，同一对只写一条，
type 和 description 必须说的是同一件事。"""
    return prompt


def generate_characters(
    send: Callable[[str], str],
    parameters: Dict[str, Any],
    num_characters: int,
    *,
    female_percentage: int = 50,
    factions: Sequence[Dict[str, Any]] = (),
    on_retry: Callable[[int, Exception], None] | None = None,
) -> List[Dict[str, Any]]:
    """只要人物卡这一部分；关系另行校验时用 :func:`generate_cast`。"""

    def validate(records):
        return validate_characters(records, female_percentage=female_percentage)

    prompt = _character_prompt(parameters, num_characters, female_percentage, factions)
    return _run(send, prompt, "characters", validate, on_retry)


def generate_cast(
    send: Callable[[str], str],
    parameters: Dict[str, Any],
    num_characters: int,
    *,
    female_percentage: int = 50,
    factions: Sequence[Dict[str, Any]] = (),
    on_retry: Callable[[int, Exception], None] | None = None,
) -> Dict[str, Any]:
    """一次生成人物与人物关系，返回 characters.json 落盘用的完整内容。"""
    payload: Dict[str, Any] = {}

    def parse(response: str):
        characters = _parse_json_list(response, "characters")
        defects = validate_characters(characters, female_percentage=female_percentage)
        relationships = []
        try:
            relationships = _parse_json_list(response, "relationships")
        except CastError:
            relationships = []
        defects.extend(validate_relationships(relationships, characters))
        if defects:
            raise CastError("；".join(defects[:8]), code="cast_invalid")
        return response, {"characters": characters, "relationships": relationships}

    prompt = _character_prompt(parameters, num_characters, female_percentage, factions)
    try:
        _, payload = generate_with_contract_retry(
            send, prompt, parse, retry_limit=CAST_RETRY_LIMIT, on_retry=on_retry
        )
    except CastError:
        raise
    except DesignContractError as error:
        raise CastError(str(error), code=getattr(error, "code", "cast_invalid")) from error
    return payload


__all__ = [
    "CAST_RETRY_LIMIT",
    "generate_cast",
    "storage_factions",
    "validate_relationships",
    "CHARACTER_ROLES",
    "CastError",
    "FACTION_NATURES",
    "generate_characters",
    "generate_factions",
    "validate_characters",
    "validate_factions",
]
