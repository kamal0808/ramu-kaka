import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ramu_store as rs  # noqa: E402


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.conn = rs.connect(os.path.join(self._t.name, "life.db"))

    def tearDown(self):
        self.conn.close()
        self._t.cleanup()

    def test_schema_creates_a_table_per_hq_key(self):
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        expected = {
            "training_log", "nutrition_log", "sleep_checkin", "sleep_session",
            "habits_log", "activity_log", "appearance_log", "wealth_toggl_entry",
            "wealth_snapshot", "social_log",
        }
        self.assertTrue(expected.issubset(tables))

    def test_insert_training_is_idempotent(self):
        rows = [("2030-09-25 10:00:00", "Gym session done.", False)]
        self.assertEqual(rs.insert_training(self.conn, rows), 1)
        self.assertEqual(rs.insert_training(self.conn, rows), 0)  # re-ingesting the same comment
        count = self.conn.execute("SELECT COUNT(*) FROM training_log").fetchone()[0]
        self.assertEqual(count, 1)

    def test_insert_nutrition_distinct_notes_both_kept(self):
        rs.insert_nutrition(self.conn, [
            ("2030-09-25 08:00:00", "breakfast", "Breakfast: oatmeal."),
            ("2030-09-25 13:30:00", "lunch", "Lunch: proper meal."),
        ])
        count = self.conn.execute("SELECT COUNT(*) FROM nutrition_log").fetchone()[0]
        self.assertEqual(count, 2)

    def test_sleep_hours_series_prefers_real_session_over_estimate(self):
        rs.insert_sleep_sessions(self.conn, [("2030-09-24 00:30:00", "2030-09-24 08:30:00", 8.0)])
        rs.insert_sleep_checkins(self.conn, [("2030-09-24 08:31:00", "okay")])
        series = rs.sleep_hours_series(self.conn, date(2030, 9, 24), days=1)
        self.assertEqual(series[0]["hours"], 8.0)
        self.assertFalse(series[0]["estimated"])

    def test_sleep_hours_series_falls_back_to_quality_estimate(self):
        rs.insert_sleep_checkins(self.conn, [("2030-09-25 10:26:00", "bad")])
        series = rs.sleep_hours_series(self.conn, date(2030, 9, 25), days=1)
        self.assertEqual(series[0]["hours"], 4.5)
        self.assertTrue(series[0]["estimated"])

    def test_sleep_hours_series_gap_when_no_data(self):
        series = rs.sleep_hours_series(self.conn, date(2030, 9, 25), days=1)
        self.assertIsNone(series[0]["hours"])

    def test_day_boundary_not_shifted_by_late_night_local_entry(self):
        # 00:31 local time on the 25th must bucket to the 25th, not the 24th -- this
        # is exactly the case a stored UTC-offset timestamp would get wrong.
        rs.insert_nutrition(self.conn, [("2030-09-25 00:31:00", "dinner", "Dinner eaten.")])
        series = rs.nutrition_daily_counts(self.conn, date(2030, 9, 25), days=1)
        self.assertEqual(series[0]["count"], 1)

    def test_nutrition_daily_counts(self):
        rs.insert_nutrition(self.conn, [
            ("2030-09-25 08:00:00", "breakfast", "Breakfast: oatmeal."),
            ("2030-09-25 13:30:00", "lunch", "Lunch: proper meal."),
        ])
        series = rs.nutrition_daily_counts(self.conn, date(2030, 9, 25), days=1)
        self.assertEqual(series[0]["count"], 2)

    def test_nutrition_daily_counts_real_zero_not_a_gap(self):
        series = rs.nutrition_daily_counts(self.conn, date(2030, 9, 25), days=1)
        self.assertEqual(series[0]["count"], 0)

    def test_wealth_daily_series_filters_project_and_converts_net(self):
        rs.insert_wealth_entries(self.conn, [
            ("e1", "2030-09-25 10:00:00", 1001, 3600 * 5, "client work"),
            ("e2", "2030-09-25 11:00:00", 999, 3600 * 5, "other client, excluded"),
        ])
        series = rs.wealth_daily_series(self.conn, date(2030, 9, 25), 1001, 100, 0.2, days=1)
        self.assertEqual(series[0]["net"], round(5 * 100 * 0.8))

    def test_wealth_daily_series_gap_when_no_entries(self):
        series = rs.wealth_daily_series(self.conn, date(2030, 9, 25), 1001, 100, 0.2, days=1)
        self.assertIsNone(series[0]["net"])

    def test_training_streak_series(self):
        rs.insert_training(self.conn, [("2030-09-25 10:00:00", "Gym.", False)])
        series = rs.training_streak_series(self.conn, date(2030, 9, 25), days=2)
        self.assertTrue(series[1]["trained"])
        self.assertFalse(series[0]["trained"])

    def test_habits_tracked_series(self):
        rs.insert_habits(self.conn, [("2030-09-25 12:00:00", "tracked", "1st coffee")])
        series = rs.habits_tracked_series(self.conn, date(2030, 9, 25), days=1)
        self.assertEqual(series[0]["count"], 1)

    def test_upsert_wealth_snapshot_replaces_same_day(self):
        rs.upsert_wealth_snapshot(self.conn, "2030-09-25", 10.0, 1000, 1000, 2000, 5000)
        rs.upsert_wealth_snapshot(self.conn, "2030-09-25", 12.0, 1200, 1000, 2200, 5000)
        rows = self.conn.execute("SELECT total FROM wealth_snapshot").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["total"], 2200)


if __name__ == "__main__":
    unittest.main()
