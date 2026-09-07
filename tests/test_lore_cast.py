"""人物卡与势力卡的落盘前校验。

用例直接对着 current_work 里真实出现过的三类硬伤写：势力名与性质自相矛盾、
抽象类别被当成地名、反派和主角想要同一件事。
"""
import json

import pytest

from core.generation.lore_cast import (
    CastError,
    generate_characters,
    generate_factions,
    validate_characters,
    validate_factions,
)


PARAMETERS = {
    "Genre": "Mystery",
    "Subgenre": "Forensic Mystery",
    "Novel Title": "镜中囚徒",
    "Theme": "不可靠叙述者背后的潜意识迷局",
    "Tone": "阴郁诡谲",
    "Story Length": "Novel (Standard)",
}


def _faction(**overrides):
    base = {
        "name": "海陵市公安局刑事侦查支队",
        "nature": "公权力机关",
        "type": "市级刑侦机构",
        "description": "负责海陵全市重大刑事案件的侦查。",
        "goal": "查清宏升物流的跨境转运链条",
        "resources": ["物证鉴定中心"],
        "territory": {"name": "海陵老城区", "kind": "城区"},
        "conflicts": [{"with": "海陵宏升物流", "over": "对港区货运的管辖"}],
    }
    base.update(overrides)
    return base


def _character(**overrides):
    base = {
        "name": "梁浩",
        "role": "protagonist",
        "gender": "男",
        "age": 44,
        "faction": "海陵市公安局刑事侦查支队",
        "profession": "刑警",
        "title": "重案队副队长",
        "goal": "找到能推翻伪造不在场证明的人证",
        "motivation": "他答应过死者的女儿",
        "flaw": "情绪失控时会动手",
        "strength": "能从废弃档案里还原时间线",
        "arc": "从独断专行到肯把证据交给别人核",
        "background": "服过刑，因熟悉黑产被特招进刑侦体系。",
        "description": "双鬓微霜，眼里布满血丝。",
    }
    base.update(overrides)
    return base


def _antagonist(**overrides):
    base = _character(
        name="郑娜敏",
        role="antagonist",
        gender="女",
        age=35,
        profession="辩护律师",
        title="合伙人",
        goal="让宏升的核心账本永远进不了法庭",
        opposes={"character": "梁浩", "blocked_goal": "找到能推翻伪造不在场证明的人证"},
    )
    base.update(overrides)
    return base


# ------------------------------------------------------------------ 势力校验

def test_a_clean_faction_passes():
    assert validate_factions([_faction()]) == []


def test_public_authority_cannot_be_named_like_a_company():
    """真实数据里出现过：名字是「海陵岸线风险咨询」，type 却写着 Police Department。"""
    defects = validate_factions([_faction(name="海陵岸线风险咨询")])
    assert any("公权力机关" in d and "咨询" in d for d in defects), defects


def test_abstract_category_is_not_a_place_name():
    """真实数据里「Court and legal systems」被当成地名转成了城市「雾平」。"""
    defects = validate_factions(
        [_faction(territory={"name": "法院与司法系统", "kind": "区域"})]
    )
    assert any("不是地名" in d for d in defects), defects

    defects = validate_factions(
        [_faction(territory={"name": "情报收集网络", "kind": "区域"})]
    )
    assert any("不是地名" in d for d in defects), defects


def test_territory_must_be_an_object_not_a_bare_string():
    defects = validate_factions([_faction(territory="海陵老城区")])
    assert any("territory 必须是" in d for d in defects), defects


def test_english_leftovers_are_rejected():
    defects = validate_factions([_faction(type="Police Department")])
    assert any("还有英文" in d for d in defects), defects


def test_empty_description_is_rejected():
    defects = validate_factions([_faction(description="")])
    assert any("description 是空的" in d for d in defects), defects


def test_duplicate_faction_names_are_rejected():
    defects = validate_factions([_faction(), _faction()])
    assert any("重复" in d for d in defects), defects


# ------------------------------------------------------------------ 人物校验

def test_a_clean_cast_passes():
    assert validate_characters([_character(), _antagonist()], female_percentage=50) == []


def test_antagonist_must_say_which_goal_it_blocks():
    """真实数据里反派的目标抽到的是「Help solve the case」——他想帮忙破案。"""
    cast = [_character(), _antagonist(opposes=None)]
    defects = validate_characters(cast, female_percentage=50)
    assert any("缺少 opposes" in d for d in defects), defects


def test_antagonist_cannot_oppose_someone_who_is_not_in_the_cast():
    cast = [_character(), _antagonist(opposes={"character": "无名氏", "blocked_goal": "某事"})]
    defects = validate_characters(cast, female_percentage=50)
    assert any("不在主角名单里" in d for d in defects), defects


def test_a_cast_without_an_antagonist_is_rejected():
    defects = validate_characters([_character()], female_percentage=50)
    assert any("antagonist" in d for d in defects), defects


def test_empty_character_description_is_rejected():
    defects = validate_characters([_character(description=""), _antagonist()])
    assert any("description 是空的" in d for d in defects), defects


def test_gender_ratio_must_be_close_to_the_setting():
    cast = [_character(), _antagonist(gender="男")]
    defects = validate_characters(cast, female_percentage=50)
    assert any("女性占比" in d for d in defects), defects

    # 设定成「多为男性」时，同一批人就不该再报了。
    assert not [
        d for d in validate_characters(cast, female_percentage=25) if "女性占比" in d
    ]


def test_age_must_be_a_plausible_integer():
    defects = validate_characters([_character(age="四十四"), _antagonist()])
    assert any("age" in d for d in defects), defects


# ------------------------------------------------------------------ 生成与重试

def test_generation_feeds_the_defect_back_and_succeeds_on_retry():
    """第一稿把抽象类别当地名，失败原文要原样回到下一次提示词里。"""
    prompts = []
    replies = [
        json.dumps({"factions": [_faction(territory={"name": "法院与司法系统", "kind": "区域"})]},
                   ensure_ascii=False),
        json.dumps({"factions": [_faction()]}, ensure_ascii=False),
    ]

    def send(prompt):
        prompts.append(prompt)
        return replies[len(prompts) - 1]

    factions = generate_factions(send, PARAMETERS, 1)

    assert len(factions) == 1
    assert factions[0]["territory"]["name"] == "海陵老城区"
    assert len(prompts) == 2
    assert "不是地名" in prompts[1], "修复指令必须原样带上失败原文"


def test_generation_gives_up_with_a_readable_error():
    bad = json.dumps({"factions": [_faction(name="海陵岸线风险咨询")]}, ensure_ascii=False)
    with pytest.raises(CastError) as excinfo:
        generate_factions(lambda prompt: bad, PARAMETERS, 1)
    assert "咨询" in str(excinfo.value)


def test_character_prompt_carries_the_premise_and_the_faction_list():
    prompts = []

    def send(prompt):
        prompts.append(prompt)
        return json.dumps({"characters": [_character(), _antagonist()]}, ensure_ascii=False)

    generate_characters(
        send, PARAMETERS, 2, female_percentage=50, factions=[_faction()]
    )

    prompt = prompts[0]
    assert "镜中囚徒" in prompt
    assert "不可靠叙述者背后的潜意识迷局" in prompt
    assert "阴郁诡谲" in prompt
    assert "海陵市公安局刑事侦查支队" in prompt
    assert "女性大约占 50%" in prompt


def test_markdown_fenced_json_is_accepted():
    payload = json.dumps({"factions": [_faction()]}, ensure_ascii=False)
    factions = generate_factions(
        lambda prompt: f"```json\n{payload}\n```", PARAMETERS, 1
    )
    assert factions[0]["name"] == "海陵市公安局刑事侦查支队"
