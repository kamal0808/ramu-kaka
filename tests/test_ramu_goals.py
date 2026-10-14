import os
import sqlite3
import sys
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ramu_goals as rg  # noqa: E402
import ramu_store as rs  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
WED = datetime(2030, 9, 25, 20, 0, tzinfo=IST)  # Wed; week started Mon 23 Sep

# Fictional goals so the tests don't depend on whatever SEED ships with.
GOALS = [
    {"id": "gym", "title": "Gym 3x a week", "metric": "gym_sessions_week", "target": 3,
     "direction": "at_least", "why": "energy"},
    {"id": "habit", "title": "Under 3 coffees a day", "metric": "tracked_habit_per_day_week",
     "target": 3, "direction": "at_most", "why": "health"},
    {"id": "people", "title": "Meet 2 new people a week", "metric": "new_people_week", "target": 2,
     "direction": "at_least", "why": "friends"},
    {"id": "income", "title": "Earn 5,000 a month", "metric": "income_mtd", "target": 5000,
     "direction": "at_least", "why": "savings"},
    {"id": "sleep", "title": "Sleep 8h on average", "metric": "sleep_avg_week", "target": 8,
     "direction": "at_least", "why": "rest"},
]


class GoalTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(rs.SCHEMA)
        with mock.patch.object(rg, "SEED", GOALS):
            self.goals = {g["id"]: g for g in rg.seed(WED)}

    def ctx(self, **kw):
        return {"db": self.db, "people": [], "presence": {}, **kw}

    def test_gym_counts_distinct_days_this_week_only(self):
        self.db.executemany("INSERT INTO training_log (ts, note, is_rest_day) VALUES (?, ?, ?)", [
            ("2030-09-23 11:00", "curls", 0), ("2030-09-23 18:00", "walk", 0),
            ("2030-09-24 10:00", "rest", 1), ("2030-09-21 10:00", "last week", 0)])
        v, _, verdict = rg.evaluate(self.goals["gym"], self.ctx(), WED)
        self.assertEqual(v, 1)
        self.assertEqual(verdict, "behind")  # 1 of 3 with ~40% of week gone -> behind 1.3

    def test_habit_average_over_logged_days_and_at_most(self):
        self.db.executemany("INSERT INTO habits_log (ts, kind, note) VALUES (?, 'tracked', ?)",
                            [("2030-09-24 10:00", "a"), ("2030-09-24 12:00", "b"), ("2030-09-25 09:00", "c")])
        v, _, verdict = rg.evaluate(self.goals["habit"], self.ctx(), WED)
        self.assertEqual((v, verdict), (1.5, "met"))

    def test_no_data_is_said_not_invented(self):
        v, detail, verdict = rg.evaluate(self.goals["sleep"], self.ctx(), WED)
        self.assertEqual((v, verdict), (None, "no_data"))

    def test_income_pace_for_month(self):
        self.db.execute("INSERT INTO wealth_snapshot (ts, total) VALUES ('2030-09-25', 3000)")
        v, _, verdict = rg.evaluate(self.goals["income"], self.ctx(), WED)
        self.assertEqual((v, verdict), (3000, "behind"))

    def test_new_people_ignores_seeded(self):
        people = [{"name": "Sam", "source": "seed", "added": "2030-09-24T00:00"},
                  {"name": "Ben", "added": "2030-09-24T20:00"}]
        v, _, _ = rg.evaluate(self.goals["people"], self.ctx(people=people), WED)
        self.assertEqual(v, 1)

    def test_block_marks_proposed(self):
        b = rg.block(list(self.goals.values()), self.ctx(), WED)
        self.assertIn("PROPOSED", b)
        self.assertIn("Gym 3x a week", b)
        self.assertEqual(rg.block([], self.ctx(), WED), "")

    def test_apply_updates(self):
        goals, changed = rg.apply_updates(list(self.goals.values()), [
            {"id": "gym", "status": "active", "target": 4},
            {"id": "habit", "status": "bogus"},
            {"id": "steps", "title": "Walk", "metric": "not_a_metric", "target": 1},
        ], WED)
        gym = next(g for g in goals if g["id"] == "gym")
        self.assertEqual((gym["status"], gym["target"]), ("active", 4))
        self.assertEqual(next(g for g in goals if g["id"] == "habit")["status"], "proposed")
        self.assertEqual(changed, ["gym", "habit"])
        self.assertFalse(any(g["id"] == "steps" for g in goals))


if __name__ == "__main__":
    unittest.main()
