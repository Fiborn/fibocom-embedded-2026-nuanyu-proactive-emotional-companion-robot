import os
import sys
import unittest

# The importable package lives in <repo>/app (app/src, app/static, ...).
_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from src.memory.ai_context import build_ai_messages, build_memory_context


class AIMemoryContextTest(unittest.TestCase):
    def test_all_user_memory_types_are_injected(self):
        context = build_memory_context(
            {
                "nickname": "小明",
                "notes": ["记住我对花生过敏"],
                "favorite_goals": ["高等数学"],
                "recent_moods": ["开心", "疲惫"],
                "recent_sessions": [
                    {"goal": "背单词", "duration_text": "25分钟"},
                ],
            },
            {"user_fact:hobby": "喜欢打羽毛球"},
        )

        self.assertIn("小明", context)
        self.assertIn("花生过敏", context)
        self.assertIn("高等数学", context)
        self.assertIn("开心、疲惫", context)
        self.assertIn("背单词（25分钟）", context)
        self.assertIn("喜欢打羽毛球", context)

    def test_persona_cannot_overwrite_memory_context(self):
        messages = build_ai_messages(
            "你的名字叫暖暖。",
            "自然聊天。",
            "传感器在线。",
            "用户明确要求记住的内容：用户喜欢蓝色。",
            [],
            "我喜欢什么颜色？",
        )

        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("你的名字叫暖暖", messages[0]["content"])
        self.assertIn("用户喜欢蓝色", messages[0]["content"])

    def test_recent_history_is_included_without_current_turn_duplicate(self):
        messages = build_ai_messages(
            "system",
            "chat",
            "sensor",
            "memory",
            [
                {"role": "user", "content": "我叫小明"},
                {"role": "assistant", "content": "你好，小明"},
                {"role": "user", "content": "你还记得我吗"},
            ],
            "你还记得我吗",
        )

        self.assertEqual(
            [m["content"] for m in messages],
            [
                "system\nmemory\nchat\nsensor",
                "我叫小明",
                "你好，小明",
                "你还记得我吗",
            ],
        )

    def test_memories_are_isolated_by_caller_input(self):
        alice = build_memory_context({"notes": ["Alice喜欢茶"]}, {})
        bob = build_memory_context({"notes": ["Bob喜欢咖啡"]}, {})

        self.assertNotIn("Bob", alice)
        self.assertNotIn("Alice", bob)


if __name__ == "__main__":
    unittest.main()
