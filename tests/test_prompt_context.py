"""Regression tests for shared story prompt constraints."""

import inspect

from core.generation import prompt_context

from core.generation.prompt_context import (
    CHINESE_PROSE_REQUIREMENTS,
    analyze_chinese_prose_style,
    generate_prose_with_style_retry,
    build_location_guidance,
    find_scene_world_conflicts,
    format_faction_summary,
    format_genre_label,
    is_legal_suspense,
)
from core.generation.scene_pipeline import ScenePipeline
from core.generation.short_story_pipeline import ShortStoryPipeline


MYSTERY_PARAMS = {"Genre": "Mystery", "Subgenre": "Legal Thriller"}


def test_mystery_identity_and_location_rules_are_not_scifi():
    assert format_genre_label(MYSTERY_PARAMS) == "悬疑推理（法律惊悚）"
    guidance = "\n".join(build_location_guidance(MYSTERY_PARAMS))
    assert "不得把城市改写成星球" in guidance
    assert "城市、街区、建筑和房间" in guidance


def test_legal_suspense_detection_supports_saved_and_localized_values():
    assert is_legal_suspense(MYSTERY_PARAMS)
    assert is_legal_suspense({"Genre": "悬疑推理", "Subgenre": "法律惊悚"})
    assert not is_legal_suspense({"Genre": "Mystery", "Subgenre": "Cozy Mystery"})


def test_scifi_location_rules_still_allow_planets():
    guidance = "\n".join(build_location_guidance({"Genre": "Sci-Fi"}))
    assert "行星、城市" in guidance
    assert "不得把城市改写成星球" not in guidance


def test_scene_conflicts_only_flag_additions_absent_from_lore():
    conflicts = find_scene_world_conflicts(
        "地点：圣兰卡星；交通：悬浮车",
        "圣兰卡是一座沿海城市。",
        MYSTERY_PARAMS,
    )
    assert conflicts == ["悬浮车"] or conflicts == ["星球", "悬浮车"]
    assert "行星" not in conflicts


def test_current_faction_schema_formats_without_empty_names():
    summary = format_faction_summary([
        {
            "name": "市检察院",
            "description": "负责重大案件公诉。",
            "goals": ["维护证据完整性"],
        }
    ])
    assert "势力名称：市检察院" in summary
    assert "简介：负责重大案件公诉。" in summary
    assert "势力名称：无" not in summary


def test_generation_sources_have_no_scifi_prompt_literals():
    source = inspect.getsource(ScenePipeline) + inspect.getsource(ShortStoryPipeline)
    assert "请为科幻小说" not in source
    assert "请撰写科幻" not in source
    assert "地点（包括行星）" not in source
    assert "环境（行星" not in source


def test_chinese_prose_rules_cover_style_and_technical_accuracy():
    rules = "\n".join(CHINESE_PROSE_REQUIREMENTS)
    assert "自然、克制的现代中文" in rules
    assert "避免连续堆叠形容词" in rules
    assert "法律、法医和技术术语必须准确" in rules


def test_the_subtext_rule_gives_a_position_to_check_not_just_a_prohibition():
    """九章首稿的潜台词维度全是 2.0 分，而规则一直都在，只是没法当场自查。

    「不要发表连续口号」写的时候对不上任何一句具体的话；「每一段和每一场的最后一句
    必须是动作、对白或可观察的细节」指到了位置，写完扫一眼就能核。评审给的改法反复
    指向的也正是段末与场末那句替读者总结的话。
    """
    rules = "\n".join(CHINESE_PROSE_REQUIREMENTS)
    assert "最后一句" in rules
    assert "总结句" in rules
    assert "不得由旁白点破" in rules


def test_style_analyzer_flags_repetition_and_long_sentences():
    prose = "冰冷。冰冷。冰冷。" + ("这是一个塞入了过多动作和解释、没有及时停顿的句子" * 5) + "。"
    warnings = analyze_chinese_prose_style(prose)
    assert any("冰冷×3" in warning for warning in warnings)
    assert any("长句过多" in warning for warning in warnings)


def test_long_attributives_are_flagged_as_translationese():
    """The marker the reader called 定语偏多: everything piled before 的."""
    prose = "。".join(
        [
            "分明是使用高渗透性含氯特种去污溶剂定向清除物理接触面的典型特征",
            "视网膜上烙印着不锈钢台面上被化学试剂大面积毁损的父亲遗体",
            "那是胸腹部遭受强烈挤压引起的急性机械性窒息过程",
            "他点头",
            "她坐下",
        ]
    ) + "。"

    warnings = analyze_chinese_prose_style(prose)

    assert any("定语过长" in warning for warning in warnings)


def test_short_natural_chinese_passes_every_check():
    prose = (
        "雨停了。"
        "她把伞收起来，靠在门边。"
        "楼道里没有灯，只有电表箱的红点一闪一闪。"
        "他先开了口：“东西还在吗？”"
        "她没有答话，把手伸进口袋。"
    )

    assert analyze_chinese_prose_style(prose) == []


def test_latin_names_are_reported_but_common_acronyms_are_not():
    prose = "Michelle Lee 把 DNA 比对结果交给了 Eric Snow。她没有说话。"

    warnings = analyze_chinese_prose_style(prose)
    foreign = [w for w in warnings if "出现外文" in w]

    assert foreign, warnings
    assert "Michelle Lee" in foreign[0]
    assert "DNA" not in foreign[0]


def test_style_retry_feeds_the_findings_back_and_stops_when_clean():
    clean = "雨停了。她把伞收起来。他先开了口。"
    dirty = "Michelle Lee 站在那里。" + "他用那种被反复擦拭过许多次的金属托盘端来证物。" * 6
    seen = []

    def send(prompt):
        seen.append(prompt)
        return dirty if len(seen) == 1 else clean

    result = generate_prose_with_style_retry(send, "写一个场景")

    assert result == clean
    assert len(seen) == 2
    assert "以上正文未通过中文文风检查" in seen[1]
    assert "Michelle Lee" in seen[1]


def test_style_retry_keeps_the_best_draft_rather_than_failing():
    """Style is a degree, not a corrupt state: never lose a chapter over it."""
    worse = "Michelle Lee 和 Eric Snow 冰冷。冰冷。冰冷。" + "他用那种被反复擦拭过很多次的金属托盘端来证物。" * 6
    better = "Michelle Lee 说话了。"
    replies = [worse, better, worse]

    result = generate_prose_with_style_retry(lambda prompt: replies.pop(0), "写一个场景")

    assert result == better


def test_style_retry_compares_occurrence_counts_within_the_same_category(monkeypatch):
    drafts = ["十五处版本", "十三处版本", "十一处版本"]
    counts = {"十五处版本": 15, "十三处版本": 13, "十一处版本": 11}

    monkeypatch.setattr(
        prompt_context,
        "analyze_chinese_prose_style",
        lambda text: [
            f"定语过长：{counts[text]} 处小句在‘的’之前堆了 12 字以上的修饰语"
        ],
    )

    result = generate_prose_with_style_retry(
        lambda prompt: drafts.pop(0), "写一个场景", retries=2
    )

    assert result == "十一处版本"


# --- 世界观冲突检查 -----------------------------------------------------------


def test_metaphor_prone_words_no_longer_trigger_a_conflict():
    """「全息级显微投影」「弧光跃迁」在中文里是修辞，不是科幻设定。

    这两个词曾在一部法律悬疑里误判三次，每次白烧一轮重试，还把文字改得更平。
    """
    params = {"Genre": "Mystery", "Subgenre": "Legal Thriller"}
    lore = "世界观：滨海城市圣兰卡。"

    assert find_scene_world_conflicts("他调出全息级高精度显微投影。", lore, params) == []
    assert find_scene_world_conflicts("人物弧光完成了一次跃迁。", lore, params) == []


def test_hard_science_fiction_nouns_are_still_caught():
    params = {"Genre": "Mystery", "Subgenre": "Legal Thriller"}
    lore = "世界观：滨海城市圣兰卡。"

    assert find_scene_world_conflicts("梁浩乘悬浮车前往星际法庭。", lore, params) == [
        "星际",
        "悬浮车",
    ]
    assert find_scene_world_conflicts("顾阳波在行星轨道上取证。", lore, params) == ["行星"]


def test_craft_annotations_are_skipped_but_content_lines_are_not():
    params = {"Genre": "Mystery", "Subgenre": "Legal Thriller"}
    lore = "世界观：滨海城市圣兰卡。"

    # 纯写作批注谈的是写法，里面的术语不是设定。
    assert find_scene_world_conflicts("* **叙事作用**：制造一次张力跃迁", lore, params) == []
    # 但内容型标签后面跟的是故事，真窜进科幻设定就要拦下。
    assert find_scene_world_conflicts(
        "* **场景目标**：证人乘悬浮车抵达", lore, params
    ) == ["悬浮车"]


def test_a_scifi_story_is_never_flagged():
    assert find_scene_world_conflicts(
        "舰队跃迁至行星轨道。", "", {"Genre": "Sci-Fi", "Subgenre": "Space Opera"}
    ) == []


def test_markers_present_in_the_lore_are_allowed():
    params = {"Genre": "Mystery", "Subgenre": "Legal Thriller"}
    assert find_scene_world_conflicts(
        "案发地点在行星站。", "世界观提到行星站是一座旧工业区的绰号。", params
    ) == []


def test_faction_summary_reads_both_the_new_singular_goal_and_the_legacy_array():
    """新卡是单数 goal，旧项目的 factions.json 是复数 goals 的数组。

    只读复数会让「主要目标」一栏对新项目全空，而这份摘要是章节写作、结构与
    短篇三条路径判断势力想要什么的唯一依据。
    """
    new_card = format_faction_summary([
        {
            "name": "海陵市公安局沿港分局刑侦大队",
            "description": "辖区大半已划入拆迁范围。",
            "nature": "公权力机关",
            "type": "基层刑侦机构",
            "territory": "沿港分局刑侦小楼",
            "goal": "在回填工程覆盖抛尸现场前固定关键物证",
            "conflicts": [{"with": "临港新城城市更新建设指挥部", "over": "是否叫停工期"}],
        }
    ])
    assert "主要目标：在回填工程覆盖抛尸现场前固定关键物证" in new_card
    assert "冲突：与「临港新城城市更新建设指挥部」：是否叫停工期" in new_card

    legacy_card = format_faction_summary([
        {"name": "旧势力", "description": "旧格式", "goals": ["守住码头", "洗白账目"]}
    ])
    assert "主要目标：守住码头, 洗白账目" in legacy_card
    assert "冲突：" not in legacy_card
