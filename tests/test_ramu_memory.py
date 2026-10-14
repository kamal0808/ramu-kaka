import os
import sys
import tempfile
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ramu_memory as rm  # noqa: E402


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = os.path.join(self._tmp.name, "memory")

    def tearDown(self):
        self._tmp.cleanup()

    def save(self, key, text, **kw):
        return rm.remember([{"key": key, "text": text, **kw}], mem_dir=self.d)

    def test_save_and_recall_relevant(self):
        self.save("gym-trainer", "The trainer is Alex; sessions are on weekday mornings.", type="person")
        self.save("dinner-preference", "Prefers spicy food, no mushrooms, less sugar.", type="preference")
        out = rm.recall("when is my session with the trainer", self.d)
        self.assertIn("Alex", out)
        self.assertNotIn("mushrooms", out)

    def test_same_key_replaces_not_duplicates(self):
        self.save("gym-trainer", "Trainer is Alex.", importance=3)
        res = self.save("gym-trainer", "Trainer is now Jordan.", importance=2)
        self.assertTrue(res[0].startswith("updated"))
        entries = rm.load_all(self.d)
        self.assertEqual(len(entries), 1)
        self.assertIn("Jordan", entries[0]["text"])
        self.assertEqual(entries[0]["importance"], 3)  # never downgraded by a correction

    def test_near_duplicate_under_other_key_is_merged(self):
        self.save("food-prefs", "Prefers spicy food, no mushrooms, less sugar and sweets.", type="preference")
        res = self.save("food-rules", "Prefers spicy food no mushrooms less sugar and sweets.", type="preference")
        self.assertTrue(res[0].startswith("merged"))
        self.assertEqual(len(rm.load_all(self.d)), 1)

    def test_secrets_are_refused(self):
        for text in [
            "the card number is 4111 1111 1111 1111",
            "the wifi password is hunter2hunter2",
            "token 123456789:ABCdefGhIJKlmNoPQRsTUVwxyz0123456789",
            "OTP is 483920",
        ]:
            res = self.save("misc", text)
            self.assertTrue(res[0].startswith("skipped"), text)
        self.assertEqual(rm.load_all(self.d), [])

    def test_bad_input_never_raises(self):
        res = rm.remember([None, "x", {"key": "", "text": "t"}, {"key": "k"}, {"key": "ok", "text": "fine", "importance": "high", "type": "weird"}], mem_dir=self.d)
        self.assertEqual(len(res), 5)
        e = rm.load_all(self.d)
        self.assertEqual(len(e), 1)
        self.assertEqual((e[0]["type"], e[0]["importance"]), ("fact", 3))

    def test_pinned_importance_5_always_recalled(self):
        self.save("hard-rule", "Never message anyone but the user.", importance=5, type="lesson")
        self.save("cafe", "Likes the filter coffee at the corner cafe.", importance=2, type="place")
        out = rm.recall("what should I eat tonight", self.d)
        self.assertIn("Never message anyone", out)
        self.assertNotIn("filter coffee", out)

    def test_budget_is_respected(self):
        for i in range(30):
            self.save(f"trip-{i}", f"Trip number {i} to the mountains with friends and lots of detail here.", type="event")
        out = rm.recall("mountains trip friends", self.d, budget_tokens=100, max_items=30)
        self.assertLessEqual(rm.estimate_tokens(out), 100)
        self.assertGreater(len(out), 0)

    def test_recall_marks_use(self):
        self.save("gym-trainer", "Trainer is Alex.")
        rm.recall("trainer", self.d)
        e = rm.load_all(self.d)[0]
        self.assertEqual(e["uses"], 1)
        self.assertTrue(e["last_used"])

    def test_forget(self):
        self.save("gym-trainer", "Trainer is Alex.")
        self.assertEqual(rm.forget(["gym-trainer", "nope"], self.d), ["forgot: gym-trainer", "not found: nope"])
        self.assertEqual(rm.load_all(self.d), [])

    def test_consolidate_merges_and_prunes_stale(self):
        self.save("a-one", "Prefers evening workouts around seven after work.", type="preference")
        self.save("a-two", "Prefers evening workouts around seven after work hours.", type="preference", importance=4)
        # bypass write-time merge by writing the second file directly
        entries = rm.load_all(self.d)
        if len(entries) == 1:  # write-time merge already fired; add a distinct dup manually
            e = dict(entries[0], key="a-three", slug="a-three")
            rm._write_file(self.d, e)
        old = (rm._now() - timedelta(days=200)).isoformat(timespec="seconds")
        rm._write_file(self.d, {"key": "stale", "slug": "stale", "type": "fact", "importance": 1,
                                "created": old, "updated": old, "last_used": "", "uses": 0,
                                "source": "t", "text": "An unimportant old passing remark about weather."})
        res = rm.consolidate(self.d)
        self.assertGreaterEqual(res["merged"], 1)
        self.assertEqual(res["pruned"], 1)
        keys = {e["slug"] for e in rm.load_all(self.d)}
        self.assertNotIn("stale", keys)

    def test_cap_prunes_lowest_value_never_pinned(self):
        self.save("pinned", "Never do X ever.", importance=5)
        for i in range(6):
            rm.remember([{"key": f"minor-{i}", "text": f"unrelated minor note number {i} alpha{i}", "importance": 1}],
                        mem_dir=self.d, max_entries=4)
        keys = {e["slug"] for e in rm.load_all(self.d)}
        self.assertIn("pinned", keys)
        self.assertLessEqual(len(keys), 4)

    def test_file_is_human_readable_markdown(self):
        self.save("gym-trainer", "Trainer is Alex.", type="person")
        with open(os.path.join(self.d, "gym-trainer.md")) as fh:
            raw = fh.read()
        self.assertTrue(raw.startswith("---\nkey: gym-trainer\ntype: person"))
        self.assertTrue(raw.rstrip().endswith("Trainer is Alex."))

    def test_synonyms_bridge_wording_but_dont_merge_duplicates(self):
        self.save("social-note", "Wants to meet more people, maybe at board game nights.", type="preference", importance=4)
        self.save("gym-trainer", "Trainer is Alex.", type="person")
        self.assertIn("board game", rm.recall("how to find friends", self.d, touch=False))
        self.assertNotIn("board game", rm.recall("what did the trainer say", self.d, touch=False))
        # dedupe stays exact: near-synonym text under another key is NOT merged
        res = self.save("friends-note", "Would like a regular hangout with friends.", type="preference")
        self.assertTrue(res[0].startswith("saved"))

    def test_extract_json_object_tolerates_wrapping(self):
        ex = rm.extract_json_object
        self.assertEqual(ex('{"reply": "hi"}', "reply"), {"reply": "hi"})
        self.assertEqual(ex('Sure!\n```json\n{"reply": "hi", "remember": []}\n```', "reply")["reply"], "hi")
        self.assertEqual(ex('note {"x": 1} then {"reply": "yes"}', "reply"), {"reply": "yes"})
        self.assertIsNone(ex("just plain words", "reply"))
        self.assertIsNone(ex('{"other": 1}', "reply"))
        self.assertIsNone(ex("", "reply"))
        self.assertEqual(ex('{"a": {"reply": "nested"}}'), {"a": {"reply": "nested"}})


if __name__ == "__main__":
    unittest.main()
