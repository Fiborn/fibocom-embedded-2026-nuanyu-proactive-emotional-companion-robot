#!/usr/bin/env python3
"""Local reminder-intent gate used to avoid an unnecessary LLM request."""
import re

_REMINDER_WORDS = ("提醒我", "叫我", "别忘", "记得提醒", "闹钟", "日程")
_TIME_WORD_RE = re.compile(
    r"(?:\d{1,2}\s*[:：点时]\s*\d{0,2}|"
    r"\d+\s*(?:秒|分钟|小时|天)后|"
    r"今天|今晚|明天|后天|早上|上午|中午|下午|晚上)"
)


def looks_like_reminder(text):
    text = str(text or "")
    if any(word in text for word in _REMINDER_WORDS):
        return True
    action_words = ("开会", "上课", "吃药", "起床", "出门", "提交", "交作业")
    return bool(_TIME_WORD_RE.search(text) and any(word in text for word in action_words))
