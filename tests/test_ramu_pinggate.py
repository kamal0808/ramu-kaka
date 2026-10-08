import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ramu_pinggate as pg  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def at(h, m=0, day=22):
    return datetime(2030, 9, day, h, m, tzinfo=IST)


def turn(when, role="ramu_kaka", unsolicited=False, urgency=None, chase=None):
    t = {"ts": when.isoformat(), "role": role, "text": "x"}
    if unsolicited:
        t["unsolicited"] = True
    if urgency:
        t["urgency"] = urgency
    if chase:
        t["chase"] = chase
    return t


class PingGateTests(unittest.TestCase):
    def test_quiet_hours_block_everything_including_briefing(self):
        self.assertEqual(pg.evaluate([], at(3))[0], False)
        self.assertEqual(pg.evaluate([], at(3), kind="briefing")[0], False)
        self.assertEqual(pg.evaluate([], at(7, 59))[0], False)

    def test_open_hours_with_clean_history_allowed(self):
        self.assertEqual(pg.evaluate([], at(10)), (True, ""))

    def test_briefing_ignores_unanswered_gap_and_cap(self):
        turns = [turn(at(9, 50), unsolicited=True) for _ in range(5)]
        self.assertTrue(pg.evaluate(turns, at(10), kind="briefing")[0])

    def test_unanswered_message_blocks_the_next_one(self):
        turns = [turn(at(9), "user"), turn(at(10), unsolicited=True)]
        ok, why = pg.evaluate(turns, at(15))
        self.assertFalse(ok)
        self.assertIn("unanswered", why)

    def test_user_reply_clears_the_block(self):
        turns = [turn(at(10), unsolicited=True), turn(at(11), "user")]
        self.assertTrue(pg.evaluate(turns, at(13))[0])

    def test_no_cap_while_user_keeps_answering(self):
        turns = []
        for h in (9, 10, 11, 12, 13, 14):
            turns += [turn(at(h), unsolicited=True), turn(at(h, 5), "user")]
        self.assertTrue(pg.evaluate(turns, at(14, 6))[0])

    def test_more_urgent_topic_breaks_through(self):
        turns = [turn(at(9), "user"), turn(at(10), unsolicited=True, urgency="chat")]
        self.assertTrue(pg.evaluate(turns, at(10, 30), urgency="urgent")[0])
        self.assertFalse(pg.evaluate(turns, at(10, 30), urgency="chat")[0])

    def test_ordinary_replies_are_not_unsolicited(self):
        turns = [turn(at(10), "user"), turn(at(10, 1))]
        self.assertTrue(pg.evaluate(turns, at(10, 2))[0])

    def test_bad_timestamps_are_ignored(self):
        turns = [{"ts": "garbage", "role": "user", "unsolicited": True}, {"role": "user"}]
        self.assertTrue(pg.evaluate(turns, at(10))[0])

    def test_bad_timestamps_are_ignored_by_chase(self):
        self.assertIsNone(pg.next_chase([{"ts": "garbage", "unsolicited": True}], at(12)))

    def test_quiet_window_that_wraps_midnight(self):
        self.assertTrue(pg.in_quiet_hours(at(23), 22, 7))
        self.assertTrue(pg.in_quiet_hours(at(2), 22, 7))
        self.assertFalse(pg.in_quiet_hours(at(12), 22, 7))



def chase_run(urgency, start, until, step_minutes=1):
    """Simulate the chase loop polling every minute with no reply from the user."""
    turns = [turn(start, "user"), turn(start, unsolicited=True, urgency=urgency)]
    out, now = [], start
    while now <= until:
        c = pg.next_chase(turns, now)
        if c:
            out.append(((now - start).seconds // 60, c["action"], c["step"]))
            turns.append(turn(now, unsolicited=True, urgency=urgency, chase=c["step"]))
        now += timedelta(minutes=step_minutes)
    return out, turns


class ChaseTests(unittest.TestCase):
    def test_chat_nudges_twice_then_lets_go(self):
        out, _ = chase_run("chat", at(12), at(19, 59))
        self.assertEqual(out, [(90, "text", 1), (240, "text", 2)])

    def test_untapped_checkin_is_never_chased_but_still_holds_the_gate(self):
        q = turn(at(13, 39), unsolicited=True)
        q["checkin_q"] = "lunch"
        turns = [turn(at(12), "user"), q]
        self.assertIsNone(pg.next_chase(turns, at(15, 10)))
        self.assertFalse(pg.evaluate(turns, at(15, 10))[0])

    def test_important_escalates_to_a_call(self):
        out, _ = chase_run("important", at(12), at(18))
        self.assertEqual(out, [(30, "text", 1), (75, "text", 2), (150, "text", 3), (180, "call", 4)])

    def test_urgent_escalates_fast(self):
        out, _ = chase_run("urgent", at(12), at(13))
        self.assertEqual(out, [(5, "text", 1), (10, "text", 2), (15, "text", 3), (20, "call", 4)])

    def test_user_reply_stops_the_chase(self):
        _, turns = chase_run("urgent", at(12), at(12, 7))
        turns.append(turn(at(12, 8), "user"))
        self.assertIsNone(pg.next_chase(turns, at(12, 30)))

    def test_old_untagged_messages_chase_as_chat(self):
        turns = [turn(at(12), unsolicited=True)]
        self.assertEqual(pg.next_chase(turns, at(13, 30))["urgency"], "chat")

    def test_no_chase_in_quiet_hours_or_across_the_night(self):
        turns = [turn(at(23, day=21), unsolicited=True, urgency="urgent")]
        self.assertIsNone(pg.next_chase(turns, at(0, 30)))
        self.assertIsNone(pg.next_chase(turns, at(9)))

    def test_stale_chase_is_dropped(self):
        turns = [turn(at(9), unsolicited=True, urgency="chat")]
        self.assertIsNone(pg.next_chase(turns, at(17, 30)))

    def test_overdue_steps_after_restart_are_spaced(self):
        turns = [turn(at(12), unsolicited=True, urgency="urgent"),
                 turn(at(12, 30), unsolicited=True, urgency="urgent", chase=1)]
        self.assertIsNone(pg.next_chase(turns, at(12, 31)))
        self.assertEqual(pg.next_chase(turns, at(12, 35))["step"], 2)

    def test_chase_lines_cover_every_text_step(self):
        for u, sched in pg.CHASE_SCHEDULE.items():
            self.assertEqual(len(pg.CHASE_LINES[u]), sum(1 for s in sched if s != "call"))


if __name__ == "__main__":
    unittest.main()
