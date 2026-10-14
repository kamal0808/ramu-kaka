import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ramu_checkins as ci  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def at(h, m=0, day=22):
    return datetime(2030, 9, day, h, m, tzinfo=IST)


class CheckinTests(unittest.TestCase):
    def test_nothing_due_before_ten(self):
        self.assertEqual(ci.due(at(9, 59), set()), [])

    def test_sleep_first_then_meals_as_the_day_goes(self):
        self.assertEqual(ci.due(at(10), set()), ["sleep"])
        self.assertEqual(ci.due(at(14), set()), ["sleep", "lunch"])
        self.assertEqual(ci.due(at(21), set()), ["sleep", "lunch", "energy", "move", "dinner"])

    def test_answered_items_drop_out(self):
        self.assertEqual(ci.due(at(14), {"sleep"}), ["lunch"])

    def test_answered_today_only_counts_today(self):
        turns = [
            {"ts": at(11, day=21).isoformat(), "role": "user", "checkin": "sleep"},
            {"ts": at(11).isoformat(), "role": "user", "checkin": "lunch"},
        ]
        self.assertEqual(ci.answered_today(turns, at(15)), {"lunch"})

    def test_build_and_parse_round_trip(self):
        text, rows = ci.build("sleep")
        self.assertEqual(text, "How did you sleep?")
        data = [b[1] for row in rows for b in row]
        for d in data:
            self.assertIsNotNone(ci.parse(d))
        self.assertLessEqual(max(len(d.encode()) for d in data), 64)

    def test_parse_rejects_junk(self):
        for bad in ("hello", "ci:", "ci:sleep", "ci:nope:good", "ci:sleep:banana", "ci:sleep:good:x", None, 5):
            self.assertIsNone(ci.parse(bad))

    def test_describe(self):
        line, note = ci.describe("sleep", "bad")
        self.assertEqual(line, "Sleep: bad")
        self.assertIn("bad", note)
        line, note = ci.describe("lunch", "skip")
        self.assertEqual(line, "Lunch: skipped")
        self.assertIsNone(note)

    def test_recent_questions_window(self):
        turns = [
            {"ts": at(12, 0).isoformat(), "role": "ramu_kaka", "checkin_q": "sleep"},
            {"ts": at(14, 50).isoformat(), "role": "ramu_kaka", "checkin_q": "lunch"},
            {"ts": at(14, 55).isoformat(), "role": "ramu_kaka", "text": "not a checkin"},
        ]
        self.assertEqual(ci.recent_questions(turns, at(15)), 1)

    def k(self, h, m, text, day=22):
        return {"ts": at(h, m, day).isoformat(), "role": "user", "text": text}

    def test_gym_sets_in_free_text_answer_move(self):
        turns = [self.k(11, 30, "Went for a run, then some yoga")]
        self.assertIn("move", ci.answered_today(turns, at(18)))
        self.assertNotIn("move", ci.due(at(18), ci.answered_today(turns, at(18))))

    def test_gym_plan_is_not_an_answer(self):
        turns = [self.k(10, 0, "Will go to gym later today")]
        self.assertNotIn("move", ci.said_today(turns, at(18)))

    def test_rest_day_answers_move(self):
        self.assertIn("move", ci.said_today([self.k(11, 0, "Taking a rest day")], at(18)))

    def test_meals_split_by_time_of_day(self):
        self.assertEqual(ci.said_today([self.k(13, 0, "had rice and soup")], at(21)), {"lunch"})
        self.assertEqual(ci.said_today([self.k(21, 0, "Had pizza")], at(21, 5)), {"dinner"})

    def test_meetings_are_not_lunch(self):
        self.assertEqual(ci.said_today([self.k(13, 0, "Busy with meetings all afternoon")], at(14)), set())

    def test_sleep_in_words(self):
        self.assertIn("sleep", ci.said_today([self.k(10, 0, "I slept well")], at(11)))
        self.assertIn("sleep", ci.said_today([self.k(10, 30, "Short night, woke up at 7")], at(11)))

    def test_reply_to_question_counts_even_without_keywords(self):
        t = self.k(19, 0, '[replying to: "Move your body today?"] Yes, earlier')
        self.assertIn("move", ci.said_today([t], at(20)))

    def test_yesterdays_words_dont_count(self):
        self.assertEqual(ci.said_today([self.k(11, 0, "Did some squats", day=21)], at(18)), set())

    def test_every_checkin_maps_to_an_hq_key(self):
        allowed = {"training", "nutrition", "habits", "activity", "appearance", "sleep"}
        for k, c in ci.CHECKINS.items():
            self.assertIn(c["hq"], allowed, k)

    def test_no_lunch_question_right_after_food_reply(self):
        turns = [{"role": "ramu_kaka", "ts": at(14, 5).isoformat(),
                  "text": "A snack now, a proper meal when you're hungry"}]
        self.assertIn("lunch", ci.answered_today(turns, at(14, 9)))

    def test_day_plan_mentioning_dinner_does_not_count(self):
        turns = [{"role": "ramu_kaka", "unsolicited": True, "ts": at(14, 2).isoformat(),
                  "text": "Rest of the day: some work, then dinner at 8"}]
        self.assertNotIn("lunch", ci.answered_today(turns, at(14, 9)))

    def test_in_conversation(self):
        turns = [{"role": "user", "ts": at(10, 3).isoformat(), "text": "Can we move that to later?"},
                 {"role": "ramu_kaka", "ts": at(10, 3).isoformat(), "text": "Fair"}]
        self.assertTrue(ci.in_conversation(turns, at(10, 9)))
        self.assertFalse(ci.in_conversation(turns, at(10, 40)))
        nudge = [{"role": "ramu_kaka", "unsolicited": True, "ts": at(10, 0).isoformat(), "text": "There?"}]
        self.assertFalse(ci.in_conversation(nudge, at(10, 5)))

    def test_meal_order_answers_lunch(self):
        self.assertIn("lunch", ci.answered_today([], at(14), [at(13, 10)]))
        self.assertNotIn("lunch", ci.answered_today([], at(14), [at(20, 30, day=21)]))


if __name__ == "__main__":
    unittest.main()
