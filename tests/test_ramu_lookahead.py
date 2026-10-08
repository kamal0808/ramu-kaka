import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ramu_lookahead as la  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def at(h, m=0):
    return datetime(2030, 10, 2, h, m, tzinfo=IST)


EV = [{"id": "a", "start": "2030-10-04T11:00:00+05:30", "end": "2030-10-04T14:00:00+05:30", "summary": "Brunch"}]


class LookaheadTests(unittest.TestCase):
    def test_fingerprint_changes_when_event_moves(self):
        moved = [dict(EV[0], end="2030-10-04T17:00:00+05:30")]
        self.assertNotEqual(la.fingerprint(EV), la.fingerprint(moved))
        self.assertEqual(la.fingerprint(EV), la.fingerprint(list(EV)))

    def test_morning_and_evening_slots_once_each(self):
        fp = la.fingerprint(EV)
        run, why = la.due(at(10, 5), {}, fp)
        self.assertTrue(run)
        st = la.mark({}, at(10, 5), fp, why)
        self.assertFalse(la.due(at(12), st, fp)[0])
        run, why = la.due(at(18, 2), st, fp)
        self.assertTrue(run)
        st = la.mark(st, at(18, 2), fp, why)
        self.assertFalse(la.due(at(20), st, fp)[0])

    def test_calendar_change_runs_but_not_too_often(self):
        fp = la.fingerprint(EV)
        st = la.mark(la.mark({}, at(10), fp, "2030-10-02T10"), at(10), fp, "")
        st["slots"] = ["2030-10-02T10"]
        new = la.fingerprint([dict(EV[0], summary="Brunch, moved to 12")])
        self.assertFalse(la.due(at(11), st, new)[0])      # within 3h of the last run
        self.assertEqual(la.due(at(13, 30), st, new), (True, "changed"))

    def test_late_start_runs_once_not_twice(self):
        fp = la.fingerprint(EV)
        run, why = la.due(at(19), {}, fp)
        st = la.mark({}, at(19), fp, why)
        self.assertFalse(la.due(at(19, 45), st, fp)[0])

    def test_never_at_night(self):
        self.assertFalse(la.due(at(2), {}, "x")[0])

    def test_brief_carries_both_calendars_and_rules(self):
        b = la.brief(EV, [{"summary": "Standup"}], at(10), "user@example.com", "acct")
        self.assertIn("Brunch", b)
        self.assertIn("READ-ONLY", b)
        self.assertIn("empty string", b)


if __name__ == "__main__":
    unittest.main()
