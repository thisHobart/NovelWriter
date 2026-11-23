"""在 PyCharm 中手动查询模拟历史消息。"""

import json
from pathlib import Path

from history_retrieval import Message, find_turn_by_message_id


def show_messages(messages: list[Message]) -> None:
    role_labels = {"user": "用户", "assistant": "AI", "program": "程序（模拟）"}
    for message in messages:
        role = role_labels.get(message["role"], message["role"])
        print(f"[{message['turn_id']} / {message['id']}] {role}：{message['content']}")


def main() -> None:
    history_path = Path(__file__).with_name("history.json")
    try:
        history: list[Message] = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit(f"读取 {history_path.name} 失败：{error}") from error

    cases: dict[int, list[Message]] = {}
    for message in history:
        case = (int(message["turn_id"][1:]) - 1) // 5 + 1
        cases.setdefault(case, []).append(message)

    print("分镜修改案例（输入任一消息 ID 查看完整案例）")
    for case, messages in cases.items():
        first = messages[0]
        print(f"{case:02d} [{first['id']}] {first['content'].splitlines()[0]}")

    while True:
        try:
            message_id = input("\n输入消息 ID（例如 m2，直接回车退出）：").strip()
        except EOFError:
            return
        if not message_id:
            return

        turn = find_turn_by_message_id(history, message_id)
        if turn:
            case = (int(turn[0]["turn_id"][1:]) - 1) // 5 + 1
            print(f"案例 {case} · 命中 {turn[0]['turn_id']} / {message_id}")
            show_messages(cases[case])
        else:
            print(f"未找到消息 ID：{message_id}")


if __name__ == "__main__":
    main()
