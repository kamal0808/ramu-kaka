import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ramu_profile  # noqa: E402
import ramu_tasks as rt  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2030, 9, 22, 12, 0, tzinfo=IST)


class TaskTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.p = os.path.join(self._t.name, "tasks.json")

    def tearDown(self):
        self._t.cleanup()

    def mk(self, title="find events", brief="find events this weekend in town"):
        return rt.add(self.p, rt.new_task(title, brief, NOW.isoformat()))

    def test_add_get_update_persist(self):
        t = self.mk()
        self.assertEqual(rt.get(self.p, t["id"])["status"], "queued")
        rt.update(self.p, t["id"], status="running")
        self.assertEqual(rt.get(self.p, t["id"])["status"], "running")
        self.assertIsNone(rt.update(self.p, "nope", status="x"))

    def test_registry_keeps_only_recent(self):
        for i in range(rt.MAX_KEEP + 5):
            self.mk(title=f"t{i}")
        self.assertEqual(len(rt.load(self.p)), rt.MAX_KEEP)

    def test_missing_or_corrupt_registry_is_empty(self):
        self.assertEqual(rt.load(self.p), [])
        with open(self.p, "w") as f:
            f.write("{not json")
        self.assertEqual(rt.load(self.p), [])

    def test_parse_spawn_cleans_and_caps(self):
        raw = [{"title": "a", "brief": "do a"}, {"brief": "no title"}, {"title": "empty", "brief": " "},
               "junk", {"title": "c", "brief": "do c"}, {"title": "d", "brief": "do d"}]
        out = rt.parse_spawn(raw)
        self.assertEqual(len(out), 3 - 1)  # cap of 3 items looked at, one is empty, one is junk
        self.assertEqual(out[0], ("a", "do a"))
        self.assertEqual(rt.parse_spawn(None), [])
        self.assertEqual(rt.parse_spawn("x"), [])

    def test_worker_prompt_carries_contract_and_brief(self):
        t = rt.new_task("Events", "find events", NOW.isoformat())
        p = rt.worker_prompt(t)
        self.assertIn("APPROVAL RULE", p)
        self.assertIn("find events", p)
        self.assertIn("needs_approval", p)
        self.assertIn(f"Do NOT message {ramu_profile.NAME} yourself", p)
        self.assertIn("Sunday 22 September", rt.worker_prompt(t, "Now: Sunday 22 September 2030, 10:00."))

    def test_parse_result_plain_wrapped_and_garbage(self):
        good = '{"status": "completed", "summary": "Booked.", "approval": null}'
        self.assertEqual(rt.parse_result(good)["status"], "completed")
        self.assertEqual(rt.parse_result("Here you go:\n```json\n" + good + "\n```")["summary"], "Booked.")
        junk = rt.parse_result("I looked around and found three things.")
        self.assertEqual(junk["status"], "needs_review")
        self.assertIn("three things", junk["summary"])
        self.assertEqual(rt.parse_result('{"status": "weird"}')["status"], "needs_review")

    def test_awareness_shows_running_and_recent_finished(self):
        run = rt.new_task("Research cafes", "b", (NOW - timedelta(seconds=90)).isoformat())
        run["status"] = "running"
        done = rt.new_task("Book slot", "b", NOW.isoformat())
        done.update(status="completed", summary="Booked for 5pm.")
        block = rt.awareness_block([run, done], NOW)
        self.assertIn("Research cafes (started 90s ago)", block)
        self.assertIn("COMPLETED Book slot: Booked for 5pm.", block)
        self.assertEqual(rt.awareness_block([], NOW), "")

    def test_approval_buttons_round_trip(self):
        rows = rt.approval_buttons("ab12cd34")
        data = [b[1] for b in rows[0]]
        self.assertEqual(rt.parse_approval(data[0]), ("ab12cd34", True))
        self.assertEqual(rt.parse_approval(data[1]), ("ab12cd34", False))
        for bad in ("ap:x", "ap::y", "ap:x:maybe", "ci:sleep:bad", None):
            self.assertIsNone(rt.parse_approval(bad))
        self.assertLessEqual(max(len(d.encode()) for d in data), 64)

    def test_resume_prompts(self):
        self.assertIn("approved", rt.resume_prompt(True, "email Sam"))
        self.assertIn("declined", rt.resume_prompt(False, "email Sam"))

    def test_correction_reaches_running_task(self):
        # A correction sent mid-task must reach the running worker.
        t = self.mk("Corner Cafe", "find Corner Cafe for the weekend trip")
        rt.update(self.p, t["id"], status="running")
        self.assertIsNotNone(rt.add_note(self.p, t["id"], "it's in Old Town", NOW.isoformat()))
        self.assertEqual(rt.take_notes(self.p, t["id"]), ["it's in Old Town"])
        self.assertEqual(rt.take_notes(self.p, t["id"]), [])
        self.assertIn("Old Town", rt.note_prompt(["it's in Old Town"]))

    def test_note_on_finished_task_is_refused(self):
        t = self.mk()
        rt.update(self.p, t["id"], status="completed")
        self.assertIsNone(rt.add_note(self.p, t["id"], "x", NOW.isoformat()))

    def test_parse_notes_and_ids_visible(self):
        self.assertEqual(rt.parse_notes([{"id": "ab", "note": "Old Town"}, {"id": "", "note": "x"}, "junk"]),
                         [("ab", "Old Town")])
        t = self.mk()
        self.assertIn(f"id={t['id']}", rt.awareness_block(rt.load(self.p), NOW))


if __name__ == "__main__":
    unittest.main()
