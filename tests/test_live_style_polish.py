from core.evaluation.live_style_polish import (
    _review_from_dict,
    apply_grounded_quote_change,
    deterministic_polish_issues,
)


def test_style_polish_invariants_reject_lost_time_character_and_scene():
    before = "程砚在20:54核对门禁。\n\n---\n\n方屿查看审计副本和封存硬盘。"
    after = "程砚核对门禁，方屿查看审计副本。"

    issues = deterministic_polish_issues(
        before, after, expected_characters=["程砚", "方屿"]
    )

    assert "场景分隔线数量发生变化" in issues
    assert any("20:54" in issue for issue in issues)
    assert any("封存" in issue for issue in issues)


def test_style_polish_invariants_allow_language_only_rephrasing():
    before = "程砚在20:54核对门禁。\n\n---\n\n方屿查看审计副本和封存硬盘。"
    after = "20:54，程砚核对门禁。\n\n---\n\n方屿查看封存的硬盘与审计副本。"

    assert deterministic_polish_issues(
        before, after, expected_characters=["程砚", "方屿"]
    ) == []


def test_review_dict_rehydrates_actionable_hard_failure():
    review = _review_from_dict(
        {
            "stage": "reader_blind",
            "passed": False,
            "scores": {"character_credibility": 2},
            "hard_failures": [
                {
                    "code": "CHARACTER_LOGIC_BREAK",
                    "quote": "去防磁柜取原始封存箱",
                    "problem": "封存箱此前已经被带走",
                    "change": "改为去做补充证言记录",
                }
            ],
            "pass_average": 3.2,
        }
    )

    assert review.hard_failures[0]["code"] == "CHARACTER_LOGIC_BREAK"
    assert "去做补充证言记录" in review.asks[0]


def test_grounded_quote_change_only_replaces_the_cited_sentence():
    quote = (
        "程砚转头看向苏澄："
        "“带上你的工作日志，我们去防磁柜取原始封存箱。”"
    )
    content = "前文不动。" + quote + "后文不动。"
    changed = apply_grounded_quote_change(
        content,
        {
            "quote": quote,
            "change": "将句末改为：“带上你的工作日志，去楼下做补充证言记录。”",
        },
    )

    assert changed == (
        "前文不动。程砚转头看向苏澄："
        "“带上你的工作日志，去楼下做补充证言记录。”后文不动。"
    )
    assert apply_grounded_quote_change(
        content, {"quote": "不存在的引文", "change": "改为：“新句。”"}
    ) == ""
