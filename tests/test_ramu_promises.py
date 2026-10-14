import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ramu_promises as rp  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def at(h, m=0, day=29):
    return datetime(2030, 9, day, h, m, tzinfo=IST)


class PromiseTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "promises.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_evening_promise_is_kept(self):
        raw = [{"at": "2030-09-29T21:00:00+05:30", "say": "It's 9. Time to head out."}]
        rp.add(self.path, rp.normalize(raw, at(14, 45)))
        self.assertEqual(rp.due(rp.load(self.path), at(20, 59)), ([], []))
        send, expire = rp.due(rp.load(self.path), at(21, 0))
        self.assertEqual([p["say"] for p in send], ["It's 9. Time to head out."])
        rp.mark(self.path, {send[0]["id"]}, "sent", at(21, 0))
        self.assertEqual(rp.due(rp.load(self.path), at(21, 5)), ([], []))

    def test_naive_time_is_read_as_local(self):
        recs = rp.normalize([{"at": "2030-09-29T21:00", "say": "x"}], at(14))
        self.assertEqual(recs[0]["at"], "2030-09-29T21:00+05:30")

    def test_junk_past_and_far_future_are_dropped(self):
        raw = [
            {"at": "2030-09-29T10:00+05:30", "say": "already past"},
            {"at": "tonight", "say": "unparseable"},
            {"at": "2030-09-29T22:00+05:30", "say": "  "},
            {"at": "2030-11-29T22:00+05:30", "say": "two months out"},
            "not a dict",
        ]
        self.assertEqual(rp.normalize(raw, at(14)), [])

    def test_same_promise_twice_is_stored_once(self):
        raw = [{"at": "2030-09-29T21:00+05:30", "say": "x"}]
        rp.add(self.path, rp.normalize(raw, at(14)))
        self.assertEqual(rp.add(self.path, rp.normalize(raw, at(15))), [])
        self.assertEqual(len(rp.load(self.path)), 1)

    def test_missed_by_hours_expires_instead_of_firing(self):
        rp.add(self.path, rp.normalize([{"at": "2030-09-29T21:00+05:30", "say": "x"}], at(14)))
        send, expire = rp.due(rp.load(self.path), at(23, 30))
        self.assertEqual(send, [])
        self.assertEqual(len(expire), 1)

    def test_slightly_late_after_restart_still_fires(self):
        rp.add(self.path, rp.normalize([{"at": "2030-09-29T21:00+05:30", "say": "x"}], at(14)))
        send, _ = rp.due(rp.load(self.path), at(21, 40))
        self.assertEqual(len(send), 1)

    def test_urgency_defaults_to_chat(self):
        recs = rp.normalize([{"at": "2030-09-29T21:00+05:30", "say": "x"},
                             {"at": "2030-09-29T22:00+05:30", "say": "y", "urgency": "important"}], at(14))
        self.assertEqual([r["urgency"] for r in recs], ["chat", "important"])

    def test_pending_block_lists_only_pending(self):
        rp.add(self.path, rp.normalize([{"at": "2030-09-29T21:00+05:30", "say": "Head out"},
                                        {"at": "2030-09-29T22:00+05:30", "say": "sleep"}], at(14)))
        first = rp.load(self.path)[0]
        rp.mark(self.path, {first["id"]}, "sent", at(21))
        block = rp.pending_block(rp.load(self.path), at(21))
        self.assertIn("sleep", block)
        self.assertNotIn("Head out", block)
        self.assertEqual(rp.pending_block([], at(21)), "")

    def test_missing_file_is_empty(self):
        self.assertEqual(rp.load(os.path.join(self.dir.name, "nope.json")), [])


class CallPromiseTests(unittest.TestCase):
    def test_call_flag_kept(self):
        recs = rp.normalize([{"at": "2030-09-30T08:00+05:30", "say": "Wake up", "call": True},
                             {"at": "2030-09-30T09:00+05:30", "say": "text only"}], at(2))
        self.assertEqual([r["call"] for r in recs], [True, False])


if __name__ == "__main__":
    unittest.main()
