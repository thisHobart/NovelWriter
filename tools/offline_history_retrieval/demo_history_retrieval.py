"""在 PyCharm 中用自然语言试查分镜改稿和应用回执。"""

from argparse import ArgumentParser
import json
from pathlib import Path

from evidence_pack import build_evidence, compact_revision, pack_evidence
from history_retrieval import Message
from run_experiment import CONTEXT_CHARS, TOP_K, grouped_history
from structured_search import rank_structured, record_from_message

DEFAULT_HISTORY = Path(__file__).with_name("history.json")
ROLE_LABELS = {"user": "用户", "assistant": "AI 回复", "program": "程序应用回执"}


def show_messages(messages: list[Message], form: str) -> None:
    for message in messages:
        content = message["content"]
        if form == "compact" and message["role"] == "assistant":
            content = compact_revision(content)
        print(f"{ROLE_LABELS[message['role']]}\n{content}")


def main() -> None:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY, help="指定归一后的单会话历史快照。")
    history_path = parser.parse_args().history
    try:
        history: list[Message] = json.loads(history_path.read_text(encoding="utf-8"))
        if not isinstance(history, list) or any(
            not isinstance(row, dict)
            or any(not isinstance(row.get(key), str) for key in ("id", "turn_id", "role", "content"))
            or row["role"] not in ROLE_LABELS
            or not (row["turn_id"].startswith("T") and row["turn_id"][1:].isascii()
                    and row["turn_id"][1:].isdigit())
            for row in history
        ):
            raise ValueError("请选择轮次已归一为 T1、T2 等编号的实验历史快照。")
    except (OSError, ValueError) as error:
        raise SystemExit(f"读取 {history_path.name} 失败：{error}") from error

    turns = grouped_history(history)
    evidence = {key: build_evidence(messages) for key, messages in turns.items()}
    session_id = str(history_path.resolve())
    records = [record for message in history if (record := record_from_message(message, session_id))]

    print(f"分镜历史检索 · {len(turns)} 轮")
    if history_path == DEFAULT_HISTORY:
        print("试试问『第六镜从助跑到冰面滑行再推门，实际应用的动作顺序是什么？』")
    while True:
        try:
            question = input("\n输入问题（直接回车退出）：").strip()
        except EOFError:
            return
        if not question:
            return

        ranked = rank_structured(question, turns, records, session_id)
        packed = pack_evidence([key for key, _, _ in ranked.rows[:TOP_K]], evidence, CONTEXT_CHARS)
        if not packed:
            print("没有找到可以带入上下文的相关记录。")
            continue

        print(f"找到 {len(packed)} 轮相关记录。")
        for item in packed:
            print()
            show_messages(turns[item.turn_id], item.form)


if __name__ == "__main__":
    main()
