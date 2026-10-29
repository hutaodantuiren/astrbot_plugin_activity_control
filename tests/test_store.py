from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_activity_control.core import Mode
from astrbot_plugin_activity_control.store import StateStore


class StoreTests(unittest.TestCase):
    def test_roundtrip_and_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = StateStore(path, default_mode=Mode.DIVE.value)
            self.assertEqual(store.get("1").mode, Mode.DIVE.value)
            store.set_mode("1", Mode.ACTIVE.value)
            store.mark_reply("1", 123.0)
            self.assertTrue(store.save())

            loaded = StateStore(path, default_mode=Mode.SLEEP.value)
            self.assertEqual(loaded.get("1").mode, Mode.ACTIVE.value)
            self.assertEqual(loaded.get("1").last_reply_at, 123.0)
            self.assertEqual(loaded.get("2").mode, Mode.SLEEP.value)

    def test_corrupt_file_is_backed_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            store = StateStore(path)
            self.assertTrue(store.load_errors)
            self.assertEqual(store.get("1").mode, Mode.DIVE.value)
            self.assertTrue(any(p.name.startswith("state.json.corrupt-") for p in Path(tmp).iterdir()))

    def test_prune_old_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = StateStore(path)
            state = store.get("old")
            state.updated_at = 1.0
            state.last_seen_at = 1.0
            state.last_reply_at = 0.0
            store.get("new").updated_at = 9_999_999_999.0
            removed = store.prune(max_groups=20, ttl_days=30)
            self.assertEqual(removed, 1)
            self.assertNotIn("old", store.groups)

    def test_saved_json_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = StateStore(path)
            store.set_mode("100", Mode.SLEEP.value)
            store.save()
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["version"], 1)
            self.assertEqual(data["groups"]["100"]["mode"], "sleep")


if __name__ == "__main__":
    unittest.main()
