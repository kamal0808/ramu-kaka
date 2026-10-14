import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ramu_dashboard as rd  # noqa: E402
import ramu_store as rs  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


# Synthetic ClickUp date_closed epoch (ms): 2030-01-01 00:00 UTC.
E = 1893456000000


def at(day, h=12, m=0):
    return datetime(2030, 9, day, h, m, tzinfo=IST)


# The dashboard reads its timezone, tracked habit and income settings from
# profile.json; pin fictional values so the tests don't depend on it.
_patch = mock.patch.multiple(
    rd, TZ=IST, HABIT_KEYWORD="coffee", HABIT_CAP=3, CAP_LIMIT=3,
    CAP_START=date(2030, 9, 25), CAP_END=date(2030, 10, 2),
    HOURLY_PROJECT_ID=1001, HOURLY_RATE=100.0, HOURLY_WITHHOLDING=0.2,
    OTHER_MONTHLY_INCOME=1000, WEALTH_TARGET=5000,
)


def setUpModule():
    _patch.start()


def tearDownModule():
    _patch.stop()


class TestParseTs(unittest.TestCase):
    def test_timed_bracket(self):
        dt, rest = rd.parse_ts("[2030-09-25 10:26] Sleep: bad (tap check-in).")
        self.assertEqual(dt, at(25, 10, 26))
        self.assertEqual(rest, "Sleep: bad (tap check-in).")

    def test_date_only_bracket(self):
        dt, rest = rd.parse_ts("[2030-09-25] Habit plan — long writeup.")
        self.assertEqual(dt, at(25, 0, 0))
        self.assertEqual(rest, "Habit plan — long writeup.")

    def test_no_bracket_is_none(self):
        dt, rest = rd.parse_ts("Sleep log — 1 Sep 2030\nOnset ~23:00")
        self.assertIsNone(dt)


class TestBodyTile(unittest.TestCase):
    def test_streak_breaks_on_explicit_rest_day(self):
        comments = [
            "[2030-09-25 10:27] 20min run, light weights.",
            "[2030-09-24 18:45] Movement: not today (tap check-in).",
            "[2030-09-23 09:00] Morning gym session.",
        ]
        tile = rd.body_tile(comments, at(25, 15, 0))
        self.assertEqual(tile["streak_days"], 1)
        self.assertEqual(tile["status"], "warning")
        self.assertIn("20min run", tile["last"])

    def test_consecutive_days_count(self):
        comments = [
            "[2030-09-25 10:00] Gym session done.",
            "[2030-09-24 09:00] Gym session done.",
            "[2030-09-23 09:00] Gym session done.",
        ]
        tile = rd.body_tile(comments, at(25, 15, 0))
        self.assertEqual(tile["streak_days"], 3)
        self.assertEqual(tile["status"], "good")

    def test_no_data(self):
        tile = rd.body_tile([], at(25))
        self.assertEqual(tile["streak_days"], 0)
        self.assertEqual(tile["status"], "serious")
        self.assertEqual(tile["last"], "No workout logged yet")


class TestNutritionTile(unittest.TestCase):
    def test_counts_only_today(self):
        comments = [
            "[2030-09-25 08:00] Breakfast: oatmeal.",
            "[2030-09-25 13:30] Lunch: proper meal (tap check-in).",
            "[2030-09-24 20:38] Dinner: not yet (tap check-in).",
        ]
        tile = rd.nutrition_tile(comments, at(25, 15, 0))
        self.assertEqual(tile["logged_count"], 2)
        self.assertEqual(tile["status"], "good")

    def test_nothing_today_is_serious(self):
        tile = rd.nutrition_tile(["[2030-09-24 20:38] Dinner: not yet (tap check-in)."], at(25))
        self.assertEqual(tile["logged_count"], 0)
        self.assertEqual(tile["status"], "serious")


class TestSleepTile(unittest.TestCase):
    def test_pairs_sleep_and_wake_across_lines(self):
        comments = [
            "[2030-09-24 00:30] Going to sleep ~00:30, 24 Sep",
            "[2030-09-24 08:30] Woke around 08:30",
            "[2030-09-24 08:31] Sleep: okay (tap check-in).",
        ]
        tile = rd.sleep_tile(comments, at(24, 15, 0))
        self.assertEqual(tile["hours_last_night"], 8.0)
        self.assertEqual(tile["last_quality"], "okay")
        self.assertEqual(tile["status"], "warning")

    def test_no_pair_falls_back_to_quality(self):
        comments = ["[2030-09-25 10:26] Sleep: bad (tap check-in)."]
        tile = rd.sleep_tile(comments, at(25, 15, 0))
        self.assertIsNone(tile["hours_last_night"])
        self.assertEqual(tile["last_quality"], "bad")
        self.assertEqual(tile["status"], "serious")

    def test_negated_wake_mention_not_treated_as_wake_event(self):
        comments = [
            "[2030-09-22 06:00] Going to sleep ~6am, late night.",
            "[2030-09-22 13:00] Up now, no specific wake time or sleep quality given.",
        ]
        tile = rd.sleep_tile(comments, at(22, 15, 0))
        self.assertIsNone(tile["hours_last_night"])

    def test_implausible_gap_not_paired(self):
        comments = [
            "[2030-09-20 23:00] Going to sleep ~11pm",
            "[2030-09-24 11:00] Woke around 11:00",
        ]
        tile = rd.sleep_tile(comments, at(24, 15, 0))
        self.assertIsNone(tile["hours_last_night"])


class TestHabitsTile(unittest.TestCase):
    def test_counts_todays_habit_against_cap(self):
        comments = [
            "[2030-09-25] Habit plan — long writeup mentions coffee several times.",
            "[2030-09-25 12:00] Coffee #1.",
        ]
        tile = rd.habits_tile(comments, at(25, 15, 0))
        self.assertEqual(tile["count_today"], 1)
        self.assertEqual(tile["cap"], 3)
        self.assertEqual(tile["remaining"], 2)
        self.assertEqual(tile["status"], "good")

    def test_over_cap_is_critical(self):
        comments = [f"[2030-09-25 1{i}:00] coffee #{i}" for i in range(4)]
        tile = rd.habits_tile(comments, at(25, 20, 0))
        self.assertEqual(tile["count_today"], 4)
        self.assertEqual(tile["status"], "critical")

    def test_outside_taper_window_no_cap(self):
        tile = rd.habits_tile([], at(15))
        self.assertIsNone(tile["cap"])
        self.assertEqual(tile["status"], "warning")


class TestWealthTile(unittest.TestCase):
    def test_computes_hourly_net_and_total(self):
        entries = [
            {"project_id": 1001, "duration": 3600 * 10},
            {"project_id": 999, "duration": 3600 * 5},  # different project, excluded
            {"project_id": 1001, "duration": -1},  # running entry, excluded
        ]
        tile = rd.wealth_tile(entries, at(25))
        self.assertEqual(tile["hourly_hours_mtd"], 10.0)
        self.assertEqual(tile["hourly_net"], round(10 * 100 * 0.8))
        self.assertEqual(tile["other_income"], 1000)
        self.assertEqual(tile["total"], tile["hourly_net"] + tile["other_income"])

    def test_status_relative_to_target(self):
        tile = rd.wealth_tile([], at(25))
        self.assertIn(tile["status"], ("good", "warning", "serious"))


class TestSocialTile(unittest.TestCase):
    def test_finds_last_and_next(self):
        closed = [{"name": "Dinner with Sam", "date_closed": str(E)}]
        open_tasks = [{"name": "Board game brunch — Sat 5 Oct, 11am"}]
        tile = rd.social_tile(closed, open_tasks, at(25))
        self.assertEqual(tile["last_experience"], "Dinner with Sam")
        self.assertIn("Board game brunch", tile["next_queued"])

    def test_skips_day_template_tasks_for_last_experience(self):
        closed = [
            {"name": "📅 Plan for tomorrow: gym, comedy evening", "date_closed": str(E + 20000)},
            {"name": "🎭 Comedy night with Sam — confirm the time", "date_closed": str(E)},
        ]
        tile = rd.social_tile(closed, [], at(25))
        self.assertIn("Comedy night with Sam", tile["last_experience"])

    def test_compound_word_substring_not_a_false_match(self):
        closed = [{"name": "🎨 Showcase prep notes", "date_closed": str(E + 10000)}]
        tile = rd.social_tile(closed, [], at(25))
        self.assertIsNone(tile["last_experience"])

    def test_no_data_is_honest_not_fabricated(self):
        tile = rd.social_tile([], [], at(25))
        self.assertIsNone(tile["last_experience"])
        self.assertIsNone(tile["next_queued"])


class TestRenderHtml(unittest.TestCase):
    def test_renders_without_error_and_contains_all_tiles(self):
        now = at(25, 15, 47)
        body = rd.body_tile([], now)
        nutrition = rd.nutrition_tile([], now)
        sleep = rd.sleep_tile([], now)
        habits = rd.habits_tile([], now)
        wealth = rd.wealth_tile([], now)
        social = rd.social_tile([], [], now)
        html = rd.render_html(body, nutrition, sleep, habits, wealth, social, "test")
        for label in ("Body", "Nutrition", "Sleep", "Habits", "Wealth", "Social"):
            self.assertIn(f">{label}<", html)
        self.assertNotIn("<svg", html)  # no series passed -> no charts, still valid HTML

    def test_series_param_adds_real_charts(self):
        now = at(25, 15, 47)
        body = rd.body_tile([], now)
        nutrition = rd.nutrition_tile([], now)
        sleep = rd.sleep_tile([], now)
        habits = rd.habits_tile([], now)
        wealth = rd.wealth_tile([], now)
        social = rd.social_tile([], [], now)
        series = {
            "training": [{"date": "24 Sep", "trained": True}, {"date": "25 Sep", "trained": False}],
            "nutrition": [{"date": "24 Sep", "count": 2}, {"date": "25 Sep", "count": 1}],
            "sleep": [{"date": "24 Sep", "hours": 8.0}, {"date": "25 Sep", "hours": None}],
            "habits": [{"date": "24 Sep", "count": 1}, {"date": "25 Sep", "count": 0}],
            "wealth": [{"date": "24 Sep", "net": 500}, {"date": "25 Sep", "net": None}],
        }
        html = rd.render_html(body, nutrition, sleep, habits, wealth, social, "test", series=series)
        self.assertGreaterEqual(html.count("<svg"), 5)


class TestIngest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.conn = rs.connect(os.path.join(self._t.name, "life.db"))

    def tearDown(self):
        self.conn.close()
        self._t.cleanup()

    def test_ingest_training_flags_rest_days(self):
        comments = [
            "[2030-09-25 10:27] 20min run, light weights.",
            "[2030-09-24 18:45] Movement: not today (tap check-in).",
        ]
        rd.ingest_training(self.conn, comments)
        rows = self.conn.execute("SELECT note, is_rest_day FROM training_log ORDER BY ts").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["is_rest_day"], 1)
        self.assertEqual(rows[1]["is_rest_day"], 0)

    def test_ingest_training_is_idempotent_across_repeated_fetches(self):
        comments = ["[2030-09-25 10:27] 20min run, light weights."]
        rd.ingest_training(self.conn, comments)
        rd.ingest_training(self.conn, comments)  # a re-fetch always returns full recent history
        count = self.conn.execute("SELECT COUNT(*) FROM training_log").fetchone()[0]
        self.assertEqual(count, 1)

    def test_ingest_nutrition_infers_meal_slot(self):
        rd.ingest_nutrition(self.conn, ["[2030-09-25 08:00] Breakfast: oatmeal."])
        row = self.conn.execute("SELECT meal_slot FROM nutrition_log").fetchone()
        self.assertEqual(row["meal_slot"], "breakfast")

    def test_ingest_sleep_writes_checkin_and_session(self):
        comments = [
            "[2030-09-24 00:30] Going to sleep ~00:30, 24 Sep",
            "[2030-09-24 08:30] Woke around 08:30",
            "[2030-09-24 08:31] Sleep: okay (tap check-in).",
        ]
        rd.ingest_sleep(self.conn, comments)
        checkins = self.conn.execute("SELECT quality FROM sleep_checkin").fetchall()
        sessions = self.conn.execute("SELECT hours FROM sleep_session").fetchall()
        self.assertEqual([r["quality"] for r in checkins], ["okay"])
        self.assertEqual([r["hours"] for r in sessions], [8.0])

    def test_ingest_sleep_day_bucket_matches_tile_logic(self):
        # A fresh data point logged via habit_log must show up under the
        # correct calendar day, same as the live tile computes it.
        comments = ["[2030-09-25 00:31] Sleep: bad (tap check-in)."]
        rd.ingest_sleep(self.conn, comments)
        series = rs.sleep_hours_series(self.conn, datetime(2030, 9, 25).date(), days=1)
        self.assertEqual(series[0]["hours"], 4.5)
        self.assertTrue(series[0]["estimated"])

    def test_ingest_habits_classifies_tracked_vs_other(self):
        rd.ingest_habits(self.conn, [
            "[2030-09-25 12:00] Coffee #1.",
            "[2030-09-25 16:00] Energy: low (tap check-in).",
        ])
        kinds = {r["kind"] for r in self.conn.execute("SELECT kind FROM habits_log")}
        self.assertEqual(kinds, {"tracked", "other"})

    def test_ingest_wealth_toggl_skips_running_entries_and_converts_to_local(self):
        entries = [
            {"id": 1, "start": "2030-09-25T04:30:00Z", "project_id": 1001, "duration": 3600},
            {"id": 2, "start": "2030-09-25T05:00:00Z", "project_id": 1001, "duration": -1},
        ]
        rd.ingest_wealth_toggl(self.conn, entries)
        rows = self.conn.execute("SELECT ts FROM wealth_toggl_entry").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["ts"].startswith("2030-09-25 10:00"))  # UTC 04:30 -> local 10:00

    def test_ingest_social_only_stores_closed_experiences(self):
        closed = [
            {"name": "Dinner with Sam", "date_closed": str(E)},
            {"name": "🎟️ Buy tickets for the comedy night", "date_closed": str(E + 10000)},
        ]
        rd.ingest_social(self.conn, closed)
        names = [r["name"] for r in self.conn.execute("SELECT name FROM social_log")]
        self.assertEqual(names, ["Dinner with Sam"])


if __name__ == "__main__":
    unittest.main()
