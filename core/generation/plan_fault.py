# -*- coding: utf-8 -*-
"""分辨一处硬伤是正文写坏了，还是场景规划本来就这么要求的。

定向重修只能改正文。可有些硬伤的根子在规划里——第 23 章的场景规划写着
「梁浩在'海陵先驱号'上只有不到四十分钟」，而上一章结尾的倒计时是十四分二十秒，
于是读者盲测每一轮都报时序矛盾；第 24 章的规划写死了一句台词「是顾阳波质谱分析
报告里的…」，现实合理性每一轮都报精度无据。这两条重修多少遍都不会好：照规划写
就过不了评审，不照规划写就违反契约，模型被夹在中间，白白耗掉一轮章节级重修。

判据是确定性的：评审的每条硬伤都必须附一段正文里逐字可检索的引文（本项目一贯的
约束）。把那段引文拿去和场景规划比，共用片段够长，就说明正文只是把规划上的话照
搬了下来——错不在正文。

门槛按实测定：真出在规划里的三条命中 13、19、20 字，正文自己的问题全在 7 字以下。
取 12 字，两边各留五个字的余量。宁可漏判——漏判只是退回今天的行为（照旧当成正文
问题去重修），误判却会把真正该改正文的地方放过去。

抓不到语义层面的规划缺陷：规划要求「以公诉代理人身份向法警下达羁押指令」，正文
换一套说法写出来，字面对不上。那种仍旧留给作者。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence

from core.generation.chapter_continuity import collapse, shared_fragments


#: 引文与场景规划共用多长的片段，才算「正文只是照规划写的」。
PLAN_ECHO_CHARS = 12


def plan_echo(quote: str, scene_plan: str, protected: Sequence[str] = ()) -> str:
    """引文与场景规划共用的最长片段；短于门槛时返回空串。"""
    flat_quote, flat_plan = collapse(quote), collapse(scene_plan)
    if not flat_quote or not flat_plan:
        return ""
    fragments = shared_fragments(
        flat_plan, flat_quote, min_len=PLAN_ECHO_CHARS, protected=protected
    )
    fragments.sort(key=len, reverse=True)
    return fragments[0] if fragments else ""


def plan_mandated_failures(
    hard_failures: Iterable[Dict[str, Any]],
    scene_plans: Sequence[str],
    *,
    protected: Sequence[str] = (),
) -> List[Dict[str, Any]]:
    """这批硬伤里，哪几条是场景规划自己要求的。

    每条结果带上 `plan_echo`（规划里对应的那段话）和 `plan_scene`（第几场），
    作者据此能直接翻到要改的地方，不必自己在规划和正文之间对字。
    """
    found: List[Dict[str, Any]] = []
    for failure in hard_failures:
        if not isinstance(failure, dict):
            continue
        quote = str(failure.get("quote", ""))
        for index, plan in enumerate(scene_plans, start=1):
            echo = plan_echo(quote, plan, protected)
            if echo:
                found.append({**failure, "plan_echo": echo, "plan_scene": index})
                break
    return found


def plan_fault_asks(failures: Sequence[Dict[str, Any]]) -> List[str]:
    """每处规划缺陷一条修改要求，可以直接发给规划修订。

    单列出来是因为它有两个去处：写进评审警告给作者看，以及作为 `revise_plan` 的
    修改清单。两处必须是同一句话——作者在界面上看到的那一条，正是自动修订拿去改
    的那一条，对不上就没法核对到底改了什么。
    """
    return [
        "【{code}】{problem} → 规划第 {scene} 场里的这句：「{echo}」".format(
            code=item.get("code", ""),
            problem=str(item.get("problem", ""))[:120],
            scene=item.get("plan_scene", "?"),
            echo=item.get("plan_echo", ""),
        )
        for item in failures
    ]


def describe_plan_faults(failures: Sequence[Dict[str, Any]]) -> str:
    """写给作者的一句话：哪几条得回规划里改，对应规划的哪一句。"""
    if not failures:
        return ""
    lines = [
        "以下问题出在场景规划本身，重写正文改不掉——照规划写就过不了评审，"
        "不照规划写就违反契约："
    ]
    lines.extend(f"- {ask}" for ask in plan_fault_asks(failures))
    return "\n".join(lines)
