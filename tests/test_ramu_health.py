import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ramu_health as rh  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def at(h, m=0, day=30):
    return datetime(2030, 9, day, h, m, tzinfo=IST)


class HealthTests(unittest.TestCase):
    def test_overnight_location_dropout_caught_in_daytime_only(self):
        seen = {"location": at(0, 15).isoformat(), "telegram": at(11, 59).isoformat(),
                "bankmail": at(11).isoformat(), "footprint": at(11).isoformat(), "goals": at(9).isoformat()}
        p = rh.check(seen, at(12), started_at=at(0))
        self.assertEqual(set(p), {"location"})
        self.assertIn("since 00:15", p["location"])
        self.assertEqual(rh.check(seen, at(4), started_at=at(0)).get("location"), None)  # night: not judged

    def test_never_set_up_phone_feed_is_not_broken(self):
        p = rh.check({"telegram": at(11, 59).isoformat()}, at(12), started_at=at(11))
        self.assertNotIn("health", p)
        self.assertNotIn("whatsapp", p)

    def test_loop_never_beating_is_caught_after_grace(self):
        self.assertNotIn("bankmail", rh.check({}, at(12), started_at=at(11)))
        self.assertIn("bankmail", rh.check({}, at(14), started_at=at(11)))

    def test_tell_once_remind_later_and_recovery(self):
        msgs, st = rh.transitions({"location": "down"}, {}, at(12))
        self.assertEqual(msgs, ["down"])
        msgs, st = rh.transitions({"location": "down"}, st, at(13))
        self.assertEqual(msgs, [])
        msgs, st = rh.transitions({"location": "down"}, st, at(23, 59))
        self.assertEqual(msgs, [])
        msgs, st = rh.transitions({"location": "down"}, st, at(12) + timedelta(hours=12))
        self.assertEqual(msgs, [])  # daily, not twice a day
        msgs, st = rh.transitions({"location": "down"}, st, at(12) + timedelta(hours=24))
        self.assertEqual(msgs, ["down"])
        back = (at(12, 30) + timedelta(days=1)).isoformat()
        msgs, st = rh.transitions({}, st, at(13) + timedelta(days=1), last_seen={"location": back})
        self.assertEqual(msgs, ["Location is back. ✓"])
        self.assertEqual(st, {})

    def test_night_silence_is_not_recovery(self):
        # A phone feed stops being judged at night; that alone must not read as
        # "Location is back" while the feed is still dead.
        dead = at(0, 15).isoformat()
        msgs, st = rh.transitions({"location": "down"}, {}, at(22), last_seen={"location": dead})
        msgs, st = rh.transitions({}, st, at(23, 5), last_seen={"location": dead})
        self.assertEqual(msgs, [])
        self.assertIn("location", st)
        msgs, st = rh.transitions({}, st, at(23, 30), last_seen={"location": at(23, 20).isoformat()})
        self.assertEqual(msgs, ["Location is back. ✓"])

    def test_block(self):
        self.assertEqual(rh.block({}), "")
        self.assertIn("location", rh.block({"location": "x"}))


if __name__ == "__main__":
    unittest.main()
