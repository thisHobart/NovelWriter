# -*- coding: utf-8 -*-
"""写章节前整理全书设定的材料：按重要性排、整块取舍、指纹只看原始内容。"""
import json
import re

from agents.review.domain_review_agent import DomainReviewAgent
from core.generation.lore_repository import BODY, CHARACTERS, CONTRACT, FACTIONS, encode
from core.generation.story_ledger import StoryLedgerManager


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _work(root):
    _write(root, CONTRACT, encode({"canonical_characters": [{"id": "CH001", "name": "沈宁", "aliases": []}]}))
    _write(root, BODY, "# 世界\n白螺里的住房规则。")
    _write(root, CHARACTERS, encode({"characters": [{"name": "沈宁", "role": "protagonist", "gender": "女"}]}))
    _write(root, FACTIONS, encode([{"name": "住建局", "goal": "按期完成复核"}]))
    _write(root, "story/lore/background_protagonist_沈宁.md", "沈宁在白螺里长大。" * 50)
    # 按英文文件名排是 first_climax、resolution、rising_action；按六幕的先后是情节上升、第一次高潮、结局。
    for name, text in (("first_climax", "第一次高潮的结构"), ("resolution", "结局的结构"),
                       ("rising_action", "情节上升的结构")):
        _write(root, f"story/structure/6-act_structure_{name}.md", text)
        _write(root, f"story/planning/chapter_outlines/chapter_outlines_6-act_structure_{name}.md",
               text.replace("结构", "大纲"))
    for name, text in (("faction_arcs", "势力弧光正文"), ("character_arcs", "人物弧光正文"),
                       ("reconciled_arcs", "统一故事弧正文")):
        _write(root, f"story/structure/{name}.md", text)
    _write(root, "story/structure/structure_contract.json",
           json.dumps({"sections": [{"section": "情节上升", "section_index": 1}]}, ensure_ascii=False))
    manager = StoryLedgerManager(str(root))
    manager.initialize({"Genre": "Mystery"})
    return manager


def _headings(material):
    return re.findall(r"^## (.+)$", material, flags=re.M)


def test_material_follows_importance_and_act_order_not_file_names(tmp_path):
    material = _work(tmp_path).load_design_context()
    assert _headings(material) == [
        "故事结构登记表（必须遵守）", "名称与规则登记表（必须遵守）",
        "故事结构 · 情节上升", "故事结构 · 第一次高潮", "故事结构 · 结局",
        "章节大纲 · 情节上升", "章节大纲 · 第一次高潮", "章节大纲 · 结局",
        "世界观正文", "人物资料", "势力资料", "统一故事弧", "人物弧光", "势力弧光",
        "人物背景 · 主角 · 沈宁",
    ]
    # 人物、势力写成中文条目，不是原始数据。
    assert "名称：沈宁" in material and "角色定位：主角" in material
    assert '"role"' not in material
    assert "\n### 世界\n" in material                    # 正文自己的标题降两级，不和块标题同级
    assert material.includes_lore and material.omitted == ()


def test_over_budget_drops_whole_blocks_from_the_least_important_and_lists_them(tmp_path):
    manager = _work(tmp_path)
    full = manager.load_design_context()
    material = manager.load_design_context(max_chars=len(full) - 100)
    assert len(material) <= len(full) - 100
    assert material.omitted == ("人物背景 · 主角 · 沈宁",)
    assert "沈宁在白螺里长大" not in material
    assert _headings(material)[-1] == "篇幅放不下、这次没带的材料"
    assert material.endswith("- 人物背景 · 主角 · 沈宁（450 字）")

    tight = manager.load_design_context(max_chars=10)
    # 两张登记表篇幅再紧也带；其余整块让出，不在块中间截断。
    assert _headings(tight)[:3] == ["故事结构登记表（必须遵守）", "名称与规则登记表（必须遵守）",
                                    "篇幅放不下、这次没带的材料"]
    assert "情节上升的结构" not in tight


def test_refresh_follows_the_sources_not_the_wording_or_the_budget(tmp_path):
    manager = _work(tmp_path)
    material = manager.load_design_context()
    saved = manager.save_case_bible({"status": "ready"}, manager.load_design_context(max_chars=200))
    assert saved["design_omitted"]                       # 记下了这次没带的几块
    assert manager.load_design_context(max_chars=200).source_hash == material.source_hash
    assert not manager.case_bible_needs_refresh({**saved, "status": "ready"}, material)

    _write(tmp_path, "story/lore/background_protagonist_沈宁.md", "沈宁十岁搬进白螺里。")
    assert manager.case_bible_needs_refresh({**saved, "status": "ready"}, manager.load_design_context())


def test_story_bible_prompt_carries_the_world_setting_once(tmp_path):
    material = _work(tmp_path).load_design_context()
    prompts = []
    reviewer = DomainReviewAgent(model="test", send_prompt_fn=lambda prompt, **_: prompts.append(prompt)
                                 or '{"central_question": "沈宁能不能独自签约"}')
    reviewer.build_case_bible({"genre": "爱情"}, "按字数截断的世界设定全文", material, {})
    assert "按字数截断的世界设定全文" not in prompts[0]
    assert "世界观：" not in prompts[0]
    assert prompts[0].count("白螺里的住房规则") == 1
