"""Tests for the shared chapter-number parser used across planning and writing.

章节大纲写明的章号是唯一事实来源。场景规划、章节写作和大纲生成三处必须对同一份
大纲得出同一组章号，否则规划内容、文件名和账本会互相错位。
"""

import pytest

from core.generation.helper_fns import (
    parse_chapter_numbers,
    resolve_section_chapter_numbers,
)


@pytest.mark.parametrize(
    "heading,expected",
    [
        ("### 第 1 章：开端", [1]),
        ("### 第1章 开端", [1]),
        ("## Chapter 3: Fallout", [3]),
        ("## chapter 4", [4]),
        ("**第 5 章 终局**", [5]),
        ("# 第 12 章", [12]),
        # 以下都不是章标题，旧的宽松正则会把它们当成章节，导致整体错位。
        ("### 第 2 幕：对峙", []),
        ("### 第 4 部分", []),
        ("### 第 6 节：附记", []),
        ("### 第 3 卷", []),
        ("正文里提到第 7 章的伏笔时不算标题", []),
    ],
)
def test_only_explicit_chapter_headings_count(heading, expected):
    assert parse_chapter_numbers(heading) == expected


def test_reading_order_is_preserved_and_repeats_collapse():
    outline = (
        "## 第 1 幕：铺垫\n"
        "### 第 1 章：开端\n"
        "正文……\n"
        "### 第 2 章：线索\n"
        "#### 第 2 部分\n"
        "### 第 2 章：线索（续）\n"
        "## Chapter 3: Fallout\n"
    )
    assert parse_chapter_numbers(outline) == [1, 2, 3]


def test_empty_input_is_safe():
    assert parse_chapter_numbers("") == []
    assert parse_chapter_numbers(None) == []


def test_outline_numbers_are_trusted_even_after_a_gap():
    # 前面的部分缺了大纲，本部分仍然是第 5、6 章——不能改写成 3、4。
    numbers, warning = resolve_section_chapter_numbers([5, 6], next_expected=3, claimed={1, 2})
    assert numbers == [5, 6]
    assert warning is None


def test_collision_falls_back_to_sequential_and_reports():
    # 模型在每个部分都从第 1 章重新编号时，必须改号并说明。
    numbers, warning = resolve_section_chapter_numbers([1, 2], next_expected=3, claimed={1, 2})
    assert numbers == [3, 4]
    assert "重复" in warning


def test_missing_numbers_report_instead_of_guessing():
    numbers, warning = resolve_section_chapter_numbers([], next_expected=3, claimed={1, 2})
    assert numbers == []
    assert "没有可识别的章标题" in warning


def test_out_of_order_numbers_fall_back_to_sequential():
    numbers, warning = resolve_section_chapter_numbers([7, 5], next_expected=3, claimed={1, 2})
    assert numbers == [3, 4]
    assert "递增" in warning


def test_first_section_starts_at_one():
    numbers, warning = resolve_section_chapter_numbers([1, 2, 3], next_expected=1, claimed=set())
    assert numbers == [1, 2, 3]
    assert warning is None
