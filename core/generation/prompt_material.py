"""给模型的材料怎么拼：按重要性排好的几块，超出篇幅时从最不重要的一块起整块去掉。

写章节前整理全书设定、写场景时的设定材料都走这里，规则只有一套：

- 每块一个中文小标题，后面括号里写怎么用（「必须遵守」「参考」，可以不写）。
- 超出篇幅时从最后一块起整块去掉，`required` 的块篇幅再紧也带；不在一块中间按字数截。
- 去掉了哪几块、各多少字，列在材料末尾。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Sequence, Tuple

OMITTED_HEADING = "篇幅放不下、这次没带的材料"


@dataclass
class MaterialBlock:
    """材料里的一块。

    text 是给模型看的写法，source 是原始内容（算指纹用，不进材料）。demote 为真时，
    块里原有的标题降两级，免得和块标题（二级）混在一起；代码自己拼的块直接用三级标题，
    不再降。
    """

    key: str
    label: str
    text: str
    source: str = ""
    required: bool = False
    authority: str = ""
    lore: bool = False
    demote: bool = True


def demote_headings(text: str) -> str:
    return re.sub(r"^(#{1,4})(?=\s)", r"##\1", text, flags=re.M)


def render_blocks(kept: Sequence[MaterialBlock], omitted: Sequence[MaterialBlock] = ()) -> str:
    parts = [
        f"## {block.label}{f'（{block.authority}）' if block.authority else ''}\n"
        + (demote_headings(block.text) if block.demote else block.text)
        for block in kept
    ]
    if omitted:
        parts.append(f"## {OMITTED_HEADING}\n" + "\n".join(
            f"- {block.label}（{len(block.text)} 字）" for block in omitted))
    return "\n\n".join(parts)


def fit_blocks(
    blocks: Sequence[MaterialBlock], max_chars: int
) -> Tuple[str, List[MaterialBlock], List[MaterialBlock]]:
    """拼成一段材料，返回（材料，带上的块，去掉的块）。"""
    kept, omitted = list(blocks), []
    droppable = [block for block in reversed(blocks) if not block.required]
    text = render_blocks(kept, omitted)
    while len(text) > max_chars and droppable:
        block = droppable.pop(0)
        kept = [item for item in kept if item is not block]
        omitted.insert(0, block)
        text = render_blocks(kept, omitted)
    return text, kept, omitted
