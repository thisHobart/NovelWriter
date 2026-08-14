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
