# -*- coding: utf-8 -*-
"""分辨「正文写坏了」和「规划本来就这么要求」的回归测试。

判据的门槛按实测定：真出在规划里的三条命中 13、19、20 字，正文自己的问题全在
7 字以下。这里用同样量级的例子把两边都钉住。
"""

from core.generation.plan_fault import (
    PLAN_ECHO_CHARS,
    describe_plan_faults,
    plan_echo,
    plan_mandated_failures,
)


PLAN_ONE = """### 场景 1：法庭对峙
* **关键对白**：
  > 顾阳波：“我们撕开了临山的遮羞布，但海上的闸门还没关上。梁浩在‘海陵先驱号’上只有不到四十分钟。”
"""

PLAN_TWO = """### 场景 2：冷链舱
* **关键行动**：梁浩剪断铅封，露出纵向排列的低温休眠舱。
"""


def _failure(code, quote, problem="出了问题", change="改一改"):
    return {"code": code, "quote": quote, "problem": problem, "change": change}


def test_a_quote_lifted_straight_from_the_plan_is_a_plan_defect():
    """评审引的这句话规划里原样写着，那就不是正文的错。"""
    quote = "梁浩在‘海陵先驱号’上只有不到四十分钟。"
    echo = plan_echo(quote, PLAN_ONE)
    assert echo
    assert len(echo) >= PLAN_ECHO_CHARS


def test_prose_the_model_wrote_itself_is_not_a_plan_defect():
    assert plan_echo("防空洞拱顶不断滴落浑浊的地下水。", PLAN_ONE) == ""


def test_a_short_coincidental_overlap_stays_below_the_threshold():
    """七八个字的偶合不算——正文自己的问题实测都落在这个量级。"""
    assert plan_echo("梁浩剪断了绳索。", PLAN_TWO) == ""


def test_the_offending_scene_is_named_so_the_author_can_go_fix_it():
    failures = plan_mandated_failures(
        [
            _failure("CONTINUITY_BREAK", "梁浩在‘海陵先驱号’上只有不到四十分钟。"),
            _failure("OPENING_BOILERPLATE_REUSE", "发出一声巨响。"),
        ],
        [PLAN_ONE, PLAN_TWO],
    )
    assert [item["code"] for item in failures] == ["CONTINUITY_BREAK"]
    assert failures[0]["plan_scene"] == 1
    assert "不到四十分钟" in failures[0]["plan_echo"]


def test_a_failure_without_a_quote_is_never_blamed_on_the_plan():
    assert plan_mandated_failures([_failure("READER_CONFUSION", "")], [PLAN_ONE]) == []


def test_the_author_is_told_which_plan_line_to_change():
    failures = plan_mandated_failures(
        [_failure("CONTINUITY_BREAK", "梁浩在‘海陵先驱号’上只有不到四十分钟。", "时序矛盾")],
        [PLAN_ONE],
    )
    text = describe_plan_faults(failures)
    assert "重写正文改不掉" in text
    assert "CONTINUITY_BREAK" in text
    assert "规划第 1 场" in text
    assert "不到四十分钟" in text


def test_nothing_to_report_when_every_defect_is_the_prose_own():
    assert describe_plan_faults([]) == ""
