"""What the page on screen attaches to a chat message, and how the input box offers it.

分镜 attaches the whole storyboard; 原文 the text within its 改编范围; 改写 the draft of the tab
on screen; 提示词 the shown object's settings and prompts, or on 「镜头」 the shot's video setup.
Unsaved edits are read from the page as they are; nothing is saved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.short_video.v2.contracts import JsonObject
from core.short_video.v2.models import STAGE_LABELS, require
from core.short_video.v2.page_material import (
    asset_prompt_label,
    asset_prompt_material,
    copy_material,
    shot_video_label,
    shot_video_material,
    source_label,
    source_material,
)
from core.short_video.v2.storyboard_chat import KIND_LABELS, read_storyboard_material

from .artifact_page import ArtifactPage
from .prompt_page import PromptPage
from .source_page import SourcePage

if TYPE_CHECKING:
    from .workbench import Workbench

STORYBOARD_PARTS = (
    "附带：整份分镜（含未保存修改）、资产目录、讨论镜头用到的资产设定、原文、项目要求"
)
ASSET_TABS = ("character", "scene", "prop")
SHOT_TAB = 3


@dataclass(frozen=True)
class ChatOffer:
    """How the input box offers the page on screen."""

    # Which page; detaching one page leaves the others attached.
    key: str
    # Named on the buttons: 「+ 附带当前分镜」, 「不附带当前分镜，按普通聊天发送」.
    subject: str
    # The chip after 「附带」, such as 「分镜 · 第 3 镜 · 已保存」; empty while there is nothing.
    label: str
    # Why there is nothing to attach yet.
    empty: str
    # What the attachment holds, as the chip's tooltip.
    parts: str


def _state(page: ArtifactPage | SourcePage | PromptPage) -> str:
    return "含未保存修改" if page.dirty else "已保存"


def chat_offer(page: object) -> ChatOffer | None:
    """None when the page, or its tab, attaches nothing (资产图片)."""
    if isinstance(page, ArtifactPage) and page.stage == "storyboard":
        selected = page.editor.selected_shots() if page.record else []
        label = (
            f"分镜 · 第 {page.editor.table.currentRow() + 1} 镜 · {_state(page)}"
            if selected
            else ""
        )
        return ChatOffer(
            "storyboard", "当前分镜", label, "先在分镜表格里选中要讨论的镜头", STORYBOARD_PARTS
        )
    if isinstance(page, ArtifactPage):
        name = STAGE_LABELS[page.stage]
        label = f"{name} · {_state(page)}" if page.editor.form_rows() else ""
        parts = f"附带：当前{name}（含未保存修改）、这一步用到的项目要求"
        return ChatOffer(page.stage, "当前" + name, label, f"先生成{name}", parts)
    if isinstance(page, SourcePage):
        article = page.article.toPlainText()
        label = f"{source_label(article, page.ranges)} · {_state(page)}" if article.strip() else ""
        parts = "附带：改编范围内的原文（含未保存修改）、赛道和创作要求"
        return ChatOffer("source", "当前原文", label, "先粘贴或导入原文", parts)
    if isinstance(page, PromptPage) and page.tabs.currentIndex() == SHOT_TAB:
        shown = page.objects.currentData() is not None
        label = shot_video_label(page.objects.currentIndex() + 1) if shown else ""
        parts = "附带：本镜和前后镜的分镜、视频提示词（输入框里的当前内容）、参考图片和首尾帧、时长和分辨率"
        return ChatOffer("shot_video", "本镜视频提示词", label, "先生成分镜", parts)
    if isinstance(page, PromptPage) and page.tabs.currentIndex() < len(ASSET_TABS):
        kind = ASSET_TABS[page.tabs.currentIndex()]
        word = KIND_LABELS[kind]
        label = ""
        if page.record is not None:
            state = page.variants.currentText()
            label = f"{asset_prompt_label(kind, page.chat_name, state)} · {_state(page)}"
        parts = f"附带：当前{word}的设定和各视图的正向、负面提示词（含未保存修改）"
        return ChatOffer("asset_prompts", f"当前{word}提示词", label, "先生成提示词", parts)
    return None


def chat_attachment(workbench: Workbench, page: object, question: str = "") -> JsonObject | None:
    """What a message about the page carries; None when the page has nothing to attach."""
    offer = chat_offer(page)
    if offer is None or not offer.label:
        return None
    repository = workbench.repository
    if offer.key == "storyboard":
        return read_storyboard_material(
            repository,
            page.editor.draft_snapshot(),
            page.editor.selected_shots()[0],
            page.record,
            unsaved=page.dirty,
            question=question,
        )
    if offer.key in ("source", "novel_copy", "video_copy"):
        config = repository.current("config")
        config = config["content"] if config else None
        if offer.key == "source":
            return source_material(
                page.article.toPlainText(), page.ranges, config, unsaved=page.dirty
            )
        return copy_material(page.stage, page.editor.form_rows(), config, unsaved=page.dirty)
    if offer.key == "asset_prompts":
        # The view on screen as edited; a settings record without views yet is read as saved.
        spec = (
            page.editor.value() if page.record["content"].get("views") else page.record["content"]
        )
        catalog = repository.record(page.record["dependency_refs"]["asset_catalog"])
        return asset_prompt_material(
            spec, catalog["content"]["entities"], page.editor.index, unsaved=page.dirty
        )
    board = repository.current("storyboard")
    require(board is not None, "INVALID_INPUT", "请先生成分镜")
    shots = board["content"]["shots"]
    shot_id = page.objects.currentData()
    index = next((i for i, shot in enumerate(shots) if shot["shot_id"] == shot_id), None)
    require(index is not None, "STALE_INPUT", "这个镜头已不在当前分镜中，请刷新后再试")
    catalog = repository.record(board["dependency_refs"]["asset_catalog"])
    return shot_video_material(
        shots, index, catalog["content"]["entities"], page.media.chat_inputs()
    )
