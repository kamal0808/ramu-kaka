import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ramu_footprint as fp  # noqa: E402
import ramu_ingest  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2030, 9, 30, 12, 0, tzinfo=IST)


class FootprintTests(unittest.TestCase):
    MAILS = [{"id": "m1", "from": "RailCo", "subject": "Booking confirmed", "at": None, "text": "PNR 1234"},
             {"id": "m2", "from": "FoodApp", "subject": "Your order", "at": None, "text": "Pizza Place"}]

    def test_compact_skips_seen_and_masks_long_numbers(self):
        msgs = [{"messageId": "a", "sender": "x", "subject": "s", "messageText": "<b>acct 123456789012</b>"},
                {"messageId": "b", "sender": "y", "subject": "t", "messageText": "hi"}]
        out = fp.compact(msgs, {"b"}, ramu_ingest.email_text)
        self.assertEqual([m["id"] for m in out], ["a"])
        self.assertNotIn("123456789012", out[0]["text"])

    def test_parse_keeps_only_known_ids_and_types(self):
        out = ('noise {"signals": [{"id": "m1", "type": "travel", "title": "Train City A to City B", '
               '"when": "2030-10-04T07:00+05:30", "amount": 500, "importance": "high"}, '
               '{"id": "zz", "type": "travel", "title": "made up"}, '
               '{"id": "m2", "type": "pizza", "title": "bad type"}]} tail')
        sigs = fp.parse(out, self.MAILS)
        self.assertEqual([s["id"] for s in sigs], ["m1"])
        self.assertEqual(fp.parse("not json", self.MAILS), [])

    def test_merge_drops_old_and_dedupes(self):
        old = [{"id": "o", "type": "order", "title": "x", "when": "2030-09-01T10:00+05:30", "importance": "normal"}]
        new = [{"id": "n", "type": "travel", "title": "Train", "when": "2030-10-04T07:00+05:30", "importance": "high"}]
        merged = fp.merge(old + new, new, NOW)
        self.assertEqual(sorted(s["id"] for s in merged), ["n"])

    def test_block_shows_upcoming_not_low_or_stale(self):
        store = [{"id": "1", "type": "travel", "title": "Train City A to City B", "when": "2030-10-04T07:00+05:30",
                  "amount": None, "importance": "high"},
                 {"id": "2", "type": "other", "title": "promo", "when": None, "amount": None, "importance": "low"},
                 {"id": "3", "type": "order", "title": "Delivered yesterday", "when": "2030-09-28T10:00+05:30",
                  "amount": None, "importance": "normal"}]
        b = fp.block(store, NOW)
        self.assertIn("Train City A to City B", b)
        self.assertNotIn("promo", b)
        self.assertNotIn("Delivered yesterday", b)
        self.assertEqual(fp.block([], NOW), "")


if __name__ == "__main__":
    unittest.main()
