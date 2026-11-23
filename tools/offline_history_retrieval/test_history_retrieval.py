"""Use two synthetic turns to check exact message-ID retrieval."""

import unittest

from history_retrieval import Message, find_turn_by_message_id

HISTORY: list[Message] = [
    {"id": "m1", "turn_id": "T1", "role": "user", "content": "输入绑定保存什么？"},
    {
        "id": "m2",
        "turn_id": "T1",
        "role": "assistant",
        "content": "保存工作流、选图对应关系和时长。",
    },
    {"id": "m3", "turn_id": "T2", "role": "user", "content": "默认画幅是多少？"},
    {"id": "m4", "turn_id": "T2", "role": "assistant", "content": "9:16。"},
]


class HistoryRetrievalTest(unittest.TestCase):
    def test_either_message_id_returns_the_original_turn(self) -> None:
        for message_id in ("m1", "m2"):
            with self.subTest(message_id=message_id):
                self.assertEqual(find_turn_by_message_id(HISTORY, message_id), HISTORY[:2])

    def test_unknown_message_id_returns_no_turn(self) -> None:
        self.assertEqual(find_turn_by_message_id(HISTORY, "unknown"), [])
