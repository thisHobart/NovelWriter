"""章节循环调用写作回调时用的那组关键字，两条路径都必须接得住。

短篇的回调曾经比长篇少一个 `continuity_rules`——那是 2026-09-06 加章节衔接时
长篇改了、短篇漏了。表现是短篇跑到最后一个阶段直接抛 TypeError，前三个阶段
（约 11 分钟、十几次调用）的产出全部作废。这个用例不打模型，只比签名。
"""
import inspect

from core.generation.chapter_generation_loop import ChapterGenerationLoop
from core.generation.short_story_pipeline import ShortStoryPipeline


def _loop_call_keywords() -> set:
    """章节循环实际发给回调的那组关键字，直接从源码里读。"""
    source = inspect.getsource(ChapterGenerationLoop._run)
    start = source.index("prose = generate_scene(")
    end = source.index(")", source.index("continuity_rules", start))
    call = source[start:end]
    return {
        line.strip().split("=")[0].strip()
        for line in call.splitlines()[1:]
        if "=" in line and not line.strip().startswith("#")
    }


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
