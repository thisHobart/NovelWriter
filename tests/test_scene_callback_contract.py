"""章节循环调用写作回调时用的那组关键字，两条路径都必须接得住。

短篇的回调曾经比长篇少一个 `continuity_rules`——那是 2026-09-06 加章节衔接时
长篇改了、短篇漏了。表现是短篇跑到最后一个阶段直接抛 TypeError，前三个阶段
（约 11 分钟、十几次调用）的产出全部作废。这个用例不打模型，只比签名。
"""
import inspect
import re

from agents.writing.chapter_writing_agent import ChapterWritingAgent
from core.generation.chapter_generation_loop import ChapterGenerationLoop
from core.generation.short_story_pipeline import ShortStoryPipeline

KEYWORD = re.compile(r"^\s*([A-Za-z_]\w*)\s*=(?!=)")
FROM_KWARGS = re.compile(r"""kwargs(?:\[|\.get\()["'](\w+)["']""")


def _call_source(source: str, opening: str) -> str:
    """从 opening 开始按括号配对截出这一次调用的源码。

    原先用 `source.index(")", ...)` 找收尾，实际停在
    `continuity_rules=continuity_rules if index == 1 else ()` 里那个 `()` 上，
    调用的后半截整个被切掉。现在恰好因为 continuity_rules 是最后一个关键字才侥幸
    全看得见——任何新关键字写在它后面，这个守卫都会静默跳过它，然后照样通过。
    """
    start = source.index(opening)
    depth = 0
    for offset in range(start + len(opening) - 1, len(source)):
        if source[offset] == "(":
            depth += 1
        elif source[offset] == ")":
            depth -= 1
            if depth == 0:
                return source[start:offset]
    raise AssertionError(f"找不到 {opening} 的收尾括号")


def _loop_call_keywords() -> set:
    """章节循环实际发给回调的那组关键字，直接从源码里读。"""
    call = _call_source(
        inspect.getsource(ChapterGenerationLoop._run), "prose = generate_scene("
    )
    return {
        match.group(1)
        for line in call.splitlines()[1:]
        for match in [KEYWORD.match(line)]
        if match
    }


def _closure_forwarded_keywords(method) -> set:
    """写作回调闭包从 kwargs 里取出来、真的往下传的那组关键字。

    两个闭包都是 `def generate_scene(**kwargs)` 再逐个 kwargs 转写。漏一个不会抛
    TypeError，只是静默丢掉——比短篇那次的 TypeError 更难发现。
    """
    source = inspect.getsource(method)
    start = source.index("def generate_scene(**kwargs):")
    body = _call_source(source[start:], "self._generate_scene_prose(")
    return set(FROM_KWARGS.findall(body))


def _short_story_callback_parameters() -> tuple[set, bool]:
    """短篇那个内嵌回调的形参集合，以及它收不收 **kwargs。"""
    source = inspect.getsource(ShortStoryPipeline._write_short_story_prose)
    start = source.index("def generate_scene_prose(")
    end = source.index("):", start)
    names = set()
    accepts_extra = False
    for raw in source[start:end].splitlines()[1:]:
        item = raw.strip().rstrip(",")
        if not item:
            continue
        if item.startswith("**"):
            accepts_extra = True
            continue
        names.add(item.split("=")[0].strip())
    return names, accepts_extra


def test_short_story_callback_accepts_every_keyword_the_loop_sends():
    sent = _loop_call_keywords()
    accepted, accepts_extra = _short_story_callback_parameters()

    assert "continuity_rules" in sent, "循环不再发这个参数的话，这个用例该更新"
    missing = sent - accepted
    assert not missing or accepts_extra, (
        f"短篇回调接不住循环发出的参数：{sorted(missing)}"
    )


def test_short_story_callback_tolerates_future_keywords():
    """收 **kwargs，下次再加参数时短篇不会又被漏掉。"""
    _, accepts_extra = _short_story_callback_parameters()
    assert accepts_extra


def test_short_story_callback_forwards_continuity_rules_to_the_prompt():
    source = inspect.getsource(ShortStoryPipeline._write_short_story_prose)
    assert "continuity_rules=continuity_rules" in source, (
        "接住了却不往下传，等于衔接要求悄悄丢掉"
    )


def test_the_guard_reads_the_whole_call_not_just_up_to_the_first_paren():
    """坑：`continuity_rules=... if index == 1 else ()` 里那个 `()` 会提前收尾。

    截断之后，写在它后面的关键字一个都看不见，而这个用例照样全绿。
    """
    call = _call_source(
        inspect.getsource(ChapterGenerationLoop._run), "prose = generate_scene("
    )
    assert call.rstrip().endswith("(),")
    assert "continuity_rules" in _loop_call_keywords()


def test_both_chapter_callbacks_forward_every_keyword_the_loop_sends():
    """这两个闭包都收 **kwargs，接不住不报错，只是把参数悄悄扔了。

    契约重生成那条路走的是 review_pending_chapter 绑进去的那一个，它真的跑过：
    current_work/quality/contract_regenerations/ 下有第 18 到 22 章五个目录。
    """
    sent = _loop_call_keywords()
    for method in (
        ChapterWritingAgent.review_pending_chapter,
        ChapterWritingAgent._write_single_chapter,
    ):
        missing = sent - _closure_forwarded_keywords(method)
        assert not missing, f"{method.__name__} 接住了却不往下传：{sorted(missing)}"
