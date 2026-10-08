import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ramu_friction as rf  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2030, 9, 30, 12, 0, tzinfo=IST)


class FrictionTests(unittest.TestCase):
    def test_typical_complaints(self):
        self.assertIn("re-asked", rf.detect("I already told you I finished that. Why ask again?"))
        self.assertIn("forgot context", rf.detect("Don't you remember what we talked about?"))
        self.assertIn("spam", rf.detect("Please stop messaging me so much"))
        self.assertIn("not proactive", rf.detect("That reply was of no use"))
        self.assertIn("not proactive", rf.detect("You're not doing anything for me"))
        self.assertIn("didn't understand", rf.detect("That's not what I asked."))

    def test_ordinary_messages_are_not_frictions(self):
        for t in ("Ok great", "I'm in the gym", "Show me the list", "yes this is better", "Leaving for the market", ""):
            self.assertEqual(rf.detect(t), [], t)

    def test_reply_prefix_ignored_and_long_pastes_skipped(self):
        self.assertEqual(rf.detect('[replying to: "you forgot"] ok'), [])
        self.assertEqual(rf.detect("useless " + "x" * 700), [])

    def test_record_dedupes_and_keeps_prior_reply(self):
        fr, f = rf.record([], "no use", ["not proactive"], "2030-09-30T10:00+05:30", "Gym at 4.")
        self.assertEqual((f["id"], f["after"], f["status"]), ("f001", "Gym at 4.", "open"))
        fr2, f2 = rf.record(fr, "no use", ["not proactive"], "2030-09-30T10:00+05:30", "Gym at 4.")
        self.assertIsNone(f2)
        self.assertEqual(len(fr2), 1)

    def test_backfill_uses_the_message_before(self):
        turns = [{"role": "ramu_kaka", "text": "Move your body today?", "ts": "a"},
                 {"role": "user", "text": "i already told u", "ts": "2030-09-25T18:00+05:30"}]
        fr = rf.backfill(turns)
        self.assertEqual(fr[0]["after"], "Move your body today?")

    def test_block_repeats_and_weekly(self):
        fr = []
        for i in range(3):
            fr, _ = rf.record(fr, f"told u {i}", ["re-asked"], f"2030-09-2{7 + i % 3}T10:00+05:30", "")
        b = rf.block(fr, NOW)
        self.assertIn("recurring: re-asked x3", b)
        fr[0]["status"] = "fixed"
        self.assertIn("fixed 1, open 2", rf.weekly(fr, NOW))
        self.assertEqual(rf.block([], NOW), "")

    def test_sounds_low(self):
        for t in ["Rough one", "feeling low today", "I'm exhausted", "bad day",
                  '[voice note: "Honestly I\'m not good today."]']:
            self.assertTrue(rf.sounds_low(t), t)
        for t in ["Had pasta for lunch", "Seriously?", "the roads are rough", "rough patch on the road?"]:
            self.assertFalse(rf.sounds_low(t), t)


if __name__ == "__main__":
    unittest.main()
