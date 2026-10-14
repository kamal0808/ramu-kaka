import os
import sys
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ramu_radar as rr  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2030, 10, 2, 17, 0, tzinfo=IST)

# Fictional people so the tests don't depend on whatever SEED ships with.
PEOPLE = [
    {"name": "Nina", "aliases": ["nina's", "ninas"], "circle": "friend", "place": "Nina's"},
    {"name": "Leo", "aliases": [], "circle": "friend", "place": "Leo place"},
    {"name": "Leonard", "aliases": [], "circle": "friend"},
    {"name": "Dev", "aliases": [], "circle": "friend"},
    {"name": "Sam", "aliases": [], "circle": "friend"},
    {"name": "Lena", "aliases": [], "circle": "friend"},
    {"name": "Maya", "aliases": [], "circle": "friend"},
    {"name": "Tom Hale", "aliases": ["tom"], "circle": "network"},
    {"name": "Omar", "aliases": [], "circle": "network"},
]


def seed(now):
    with mock.patch.object(rr, "SEED", PEOPLE):
        return rr.seed(now)


def ts(day, h=12):
    return datetime(2030, 9, day, h, 0, tzinfo=IST).isoformat()


class RadarTests(unittest.TestCase):
    def setUp(self):
        self.people = seed(datetime(2030, 8, 1, tzinfo=IST))

    def by(self, people, name):
        return next(p for p in people if p["name"] == name)

    def test_user_mentions_count_ramus_dont(self):
        turns = [{"role": "user", "ts": ts(29), "text": "Going to Nina's today"},
                 {"role": "ramu_kaka", "ts": ts(30), "text": "How's Leonard?"}]
        p = rr.learn(self.people, turns=turns)
        self.assertEqual(self.by(p, "Nina")["last_contact"], ts(29))
        self.assertIsNone(self.by(p, "Leonard").get("last_contact"))

    def test_word_boundaries(self):
        p = rr.learn(self.people, turns=[{"role": "user", "ts": ts(29), "text": "samantha store"}])
        self.assertIsNone(self.by(p, "Sam").get("last_contact"))

    def test_place_visit_counts(self):
        ev = [{"kind": "place", "event": "enter", "place": "Nina's", "ts": ts(30)}]
        p = rr.learn(self.people, events=ev)
        self.assertEqual(self.by(p, "Nina")["last_how"], "at Nina's")

    def test_overdue_worst_first_and_limited(self):
        p = rr.learn(self.people, turns=[{"role": "user", "ts": ts(29), "text": "nina leo leonard dev"}])
        rows = rr.overdue(p, NOW)
        self.assertLessEqual(len(rows), 3)
        names = [r[0]["name"] for r in rows]
        self.assertNotIn("Nina", names)
        self.assertEqual(rows[0][1] / rows[0][2], max(r[1] / r[2] for r in rows))

    def test_paused_people_skipped(self):
        for x in self.people:
            x["paused"] = True
        self.assertEqual(rr.overdue(self.people, NOW), [])
        self.assertIsNone(rr.radar_note([]))


class RadarWhatsAppTests(unittest.TestCase):
    def test_one_to_one_whatsapp_is_contact_group_isnt(self):
        people = seed(datetime(2030, 8, 1, tzinfo=IST))
        ev = [{"kind": "whatsapp", "from": "Leonard Bro", "group": None, "ts": ts(30)},
              {"kind": "whatsapp", "from": "Dev", "group": "Hiking Crew", "ts": ts(30)}]
        p = rr.learn(people, events=ev)
        self.assertEqual(next(x for x in p if x["name"] == "Leonard")["last_how"], "WhatsApp")
        self.assertIsNone(next(x for x in p if x["name"] == "Dev").get("last_contact"))


if __name__ == "__main__":
    unittest.main()
