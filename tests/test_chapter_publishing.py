import json

import pytest

from core.generation.chapter_generation_loop import ChapterGenerationLoop, QualityGateError
from core.generation.helper_fns import publish_chapter_with_acceptance
from core.generation.story_ledger import source_hash


def test_rejected_chapter_is_quarantined_and_previous_file_is_restored(tmp_path):
    final_path = tmp_path / "story" / "content" / "chapters" / "chapter_2.md"
    final_path.parent.mkdir(parents=True)
    final_path.write_text("旧的已验收正文", encoding="utf-8")

    def reject(_path):
        raise RuntimeError("验收失败")

    with pytest.raises(RuntimeError, match="验收失败"):
        publish_chapter_with_acceptance(
            str(tmp_path), 2, str(final_path), "新的冲突正文", reject
        )

    assert final_path.read_text(encoding="utf-8") == "旧的已验收正文"
    rejected = list(
        (tmp_path / "quality" / "chapter_transactions" / "chapter_2").glob(
            "*/rejected.md"
        )
    )
    assert len(rejected) == 1
    assert rejected[0].read_text(encoding="utf-8") == "新的冲突正文"


def test_previous_chapter_tail_requires_matching_acceptance_hash(tmp_path):
    previous = tmp_path / "story" / "content" / "chapters" / "chapter_1.md"
    previous.parent.mkdir(parents=True)
    previous.write_text("第一章已验收结尾", encoding="utf-8")

    loop = ChapterGenerationLoop(
        str(tmp_path),
        "hosted-llm",
        require_planning_contract=True,
    )
    loop.ledger.initialize({})
    ledger_path = tmp_path / "system" / "story_ledgers" / "suspense_ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["accepted_chapters"] = [
        {"chapter": 1, "content_hash": source_hash("第一章已验收结尾")}
    ]
    ledger_path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")

    assert "已验收结尾" in loop._load_previous_chapter_tail(2)
    previous.write_text("磁盘上被替换的未验收正文", encoding="utf-8")

    with pytest.raises(QualityGateError, match="与验收账本不一致"):
        loop._load_previous_chapter_tail(2)
