"""Regression tests for LLM-generated Markdown scene headings."""

import unittest

from core.generation.helper_fns import parse_scene_sections


class SceneParsingTests(unittest.TestCase):
    def test_accepts_generated_heading_variants(self):
        headings = [
            "### Scene 1: Arrival",
            "## 场景 1：抵达",
            "#### 场景一：血腥与谎言的现场",
            "### **场景二：暗巷中的越界**",
            "**场景三：碎裂的平衡**",
        ]

        for heading in headings:
            with self.subTest(heading=heading):
                scenes = parse_scene_sections(f"规划说明\n\n{heading}\n场景正文")
                self.assertEqual(scenes, [f"{heading}\n场景正文"])

    def test_splits_mixed_headings_and_omits_preamble(self):
        content = """章节级说明

#### 场景一：开始
第一场内容

### Scene 2 - Conflict
Second scene content

**场景三：结局**
第三场内容
"""

        scenes = parse_scene_sections(content)

        self.assertEqual(len(scenes), 3)
        self.assertTrue(scenes[0].startswith("#### 场景一：开始"))
        self.assertTrue(scenes[1].startswith("### Scene 2 - Conflict"))
        self.assertTrue(scenes[2].startswith("**场景三：结局**"))
        self.assertNotIn("章节级说明", scenes[0])

    def test_does_not_treat_chapter_heading_as_scene(self):
        self.assertEqual(parse_scene_sections("### 第 1 章：开端\n只有章节说明"), [])

    def test_handles_empty_content(self):
        self.assertEqual(parse_scene_sections(""), [])
        self.assertEqual(parse_scene_sections("   \n"), [])


if __name__ == "__main__":
    unittest.main()


def test_short_story_planning_bounds_the_scene_count():
    """短篇整篇是作为一个章节送进质量闸门的，场景数必须有上限。

    提示词以前只说「拆分成一系列场景」，实测模型给了 10 个。而那套闸门的重修预算
    （2 轮）与分数门槛都是在 2-3 场的章节上实测定下来的：10 场进去，三轮评审分数
    一路走低（2.52 → 2.43 → 1.91），30 分钟后判不通过、整篇作废。
    """
    import inspect

    from core.generation.scene_pipeline import (
        SHORT_STORY_MAX_SCENES,
        SHORT_STORY_MIN_SCENES,
        ScenePipeline,
    )

    assert 1 < SHORT_STORY_MIN_SCENES <= SHORT_STORY_MAX_SCENES <= 8

    source = inspect.getsource(ScenePipeline._plan_short_story_scenes)
    assert "SHORT_STORY_MAX_SCENES" in source, "上限必须真的写进提示词，不能只是个常量"
    assert "一系列清晰、独立的场景。" not in source, "旧的无上限说法应当已被替换"
