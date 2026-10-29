from __future__ import annotations

import unittest

from astrbot_plugin_activity_control.core import (
    ChatMessage,
    Mode,
    build_activity_block,
    decide_action,
    format_transcript,
    match_wake_word,
    mode_label,
    normalize_mode,
    normalize_user_ids,
    normalize_words,
    resolve_owner_ids,
)


class OwnerTests(unittest.TestCase):
    def test_normalize_ids_accepts_list_and_text(self):
        self.assertEqual(
            normalize_user_ids(["123", " 456 ", "123", "qq:789", "@999"]),
            ["123", "456", "789", "999"],
        )
        self.assertEqual(
            normalize_user_ids("111, 222，333;444\n555"),
            ["111", "222", "333", "444", "555"],
        )

    def test_placeholder_is_ignored(self):
        self.assertEqual(normalize_user_ids(["astrbot", "admin", "10001"]), ["10001"])

    def test_sources(self):
        astrbot = ["10001", "10002"]
        plugin = ["10002", "20002"]
        self.assertEqual(resolve_owner_ids(astrbot, plugin, "astrbot"), ["10001", "10002"])
        self.assertEqual(resolve_owner_ids(astrbot, plugin, "plugin"), ["10002", "20002"])
        self.assertEqual(
            resolve_owner_ids(astrbot, plugin, "merge"),
            ["10001", "10002", "20002"],
        )


class ModeTests(unittest.TestCase):
    def test_mode_aliases(self):
        self.assertEqual(normalize_mode("活跃"), Mode.ACTIVE.value)
        self.assertEqual(normalize_mode("sleep"), Mode.SLEEP.value)
        self.assertEqual(normalize_mode("unknown"), Mode.DIVE.value)
        self.assertEqual(mode_label("sleep"), "睡眠")

    def test_decide_active(self):
        self.assertEqual(
            decide_action(
                mode=Mode.ACTIVE.value,
                is_command=False,
                is_owner=False,
                at_bot=False,
            ).action,
            "ignore",
        )
        self.assertEqual(
            decide_action(
                mode=Mode.ACTIVE.value,
                is_command=False,
                is_owner=False,
                at_bot=True,
            ).action,
            "reply",
        )
        self.assertEqual(
            decide_action(
                mode=Mode.ACTIVE.value,
                is_command=False,
                is_owner=False,
                at_bot=False,
                wake_word="爱莉希雅",
            ).trigger,
            "wake:爱莉希雅",
        )

    def test_decide_dive(self):
        self.assertEqual(
            decide_action(
                mode=Mode.DIVE.value,
                is_command=False,
                is_owner=False,
                at_bot=False,
                wake_word="爱莉希雅",
            ).action,
            "ignore",
        )
        self.assertEqual(
            decide_action(
                mode=Mode.DIVE.value,
                is_command=False,
                is_owner=False,
                at_bot=True,
            ).action,
            "reply",
        )
        self.assertEqual(
            decide_action(
                mode=Mode.DIVE.value,
                is_command=True,
                is_owner=False,
                at_bot=False,
            ).action,
            "block_command",
        )

    def test_decide_sleep(self):
        self.assertEqual(
            decide_action(
                mode=Mode.SLEEP.value,
                is_command=False,
                is_owner=False,
                at_bot=True,
            ).action,
            "ignore",
        )
        self.assertEqual(
            decide_action(
                mode=Mode.SLEEP.value,
                is_command=False,
                is_owner=True,
                at_bot=True,
            ).action,
            "reply",
        )
        self.assertEqual(
            decide_action(
                mode=Mode.SLEEP.value,
                is_command=True,
                is_owner=True,
                at_bot=False,
            ).action,
            "allow_command",
        )
        self.assertEqual(
            decide_action(
                mode=Mode.SLEEP.value,
                is_command=True,
                is_owner=False,
                at_bot=False,
            ).action,
            "block_command",
        )

    def test_active_member_command_switch(self):
        self.assertEqual(
            decide_action(
                mode=Mode.ACTIVE.value,
                is_command=True,
                is_owner=False,
                at_bot=False,
                member_commands_in_active=True,
            ).action,
            "allow_command",
        )
        self.assertEqual(
            decide_action(
                mode=Mode.ACTIVE.value,
                is_command=True,
                is_owner=False,
                at_bot=False,
                member_commands_in_active=False,
            ).action,
            "block_command",
        )


class WakeWordTests(unittest.TestCase):
    def test_words_preserve_spaces(self):
        self.assertEqual(normalize_words(["爱莉 希雅", "", "爱莉 希雅"]), ["爱莉 希雅"])

    def test_contains_prefix_exact(self):
        self.assertEqual(match_wake_word("你好，爱莉希雅在吗", ["爱莉希雅"], "contains"), "爱莉希雅")
        self.assertEqual(match_wake_word("爱莉希雅在吗", ["爱莉希雅"], "prefix"), "爱莉希雅")
        self.assertEqual(match_wake_word("爱莉希雅在吗", ["爱莉希雅"], "exact"), "")
        self.assertEqual(match_wake_word("爱莉希雅", ["爱莉希雅"], "exact"), "爱莉希雅")

    def test_casefold(self):
        self.assertEqual(match_wake_word("Hey BOT", ["bot"], "contains"), "bot")


class TranscriptTests(unittest.TestCase):
    def test_format_and_limit(self):
        messages = [
            ChatMessage("1", "10001", "甲", "第一句\n换行", 1),
            ChatMessage("2", "10002", "乙", "第二句", 2),
        ]
        text = format_transcript(messages, limit=1, max_chars=1000)
        self.assertIn("乙(10002", text)
        self.assertNotIn("第一句", text)

    def test_empty(self):
        self.assertEqual(format_transcript([]), "")

    def test_build_block_contains_rules(self):
        block = build_activity_block(
            mode=Mode.DIVE.value,
            trigger="at",
            transcript="[12:00] 甲(1): hi",
        )
        self.assertIn("潜水", block)
        self.assertIn("被 @", block)
        self.assertIn("不要复述这段说明", block)
        self.assertIn("甲(1)", block)


if __name__ == "__main__":
    unittest.main()
