import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ramu_blockers as rb  # noqa: E402
import ramu_profile  # noqa: E402


def blocker(**kw):
    base = {"goal": "place a test call to the user", "toolkit": "callsvc",
            "word_id": "callsvc_example-one", "resume": "List numbers, describe the call, wait for their yes."}
    base.update(kw)
    return base


class BlockerTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.p = os.path.join(self._t.name, "blockers.json")

    def tearDown(self):
        self._t.cleanup()

    def test_add_and_list(self):
        res = rb.add([blocker()], self.p)
        self.assertTrue(res[0].startswith("waiting on callsvc"))
        (b,) = rb.open_blockers(self.p)
        self.assertEqual((b["toolkit"], b["status"]), ("callsvc", "open"))

    def test_dedupes_same_goal_and_refreshes_resume(self):
        rb.add([blocker()], self.p)
        res = rb.add([blocker(resume="new resume text")], self.p)
        self.assertEqual(res, ["refreshed: callsvc"])
        (b,) = rb.open_blockers(self.p)
        self.assertEqual(b["resume"], "new resume text")

    def test_rejects_bad_input_and_secrets(self):
        res = rb.add([None, "x", {}, blocker(goal=""), blocker(resume="use key sk-abcdefghijklmnopqrstuv")], self.p)
        self.assertEqual(len(res), 5)
        self.assertTrue(all(r.startswith("skipped") for r in res))
        self.assertEqual(rb.open_blockers(self.p), [])

    def test_resolves_only_when_specific_connection_active(self):
        rb.add([blocker()], self.p)
        pending = lambda tk: [{"status": "INITIATED", "word_id": "callsvc_example-one"}]
        self.assertEqual(rb.check(self.p, lister=pending), [])
        other = lambda tk: [{"status": "ACTIVE", "word_id": "callsvc_someone-else"}]
        self.assertEqual(rb.check(self.p, lister=other), [])  # a different account doesn't count
        live = lambda tk: [{"status": "ACTIVE", "word_id": "callsvc_example-one"}]
        (done,) = rb.check(self.p, lister=live)
        self.assertEqual(done["toolkit"], "callsvc")
        self.assertEqual(rb.open_blockers(self.p), [])

    def test_without_word_id_any_active_connection_resolves(self):
        rb.add([blocker(word_id="")], self.p)
        self.assertEqual(len(rb.check(self.p, lister=lambda tk: [{"status": "ACTIVE", "word_id": "x"}])), 1)

    def test_failed_check_never_resolves_or_loses_the_blocker(self):
        rb.add([blocker()], self.p)
        self.assertEqual(rb.check(self.p, lister=lambda tk: None), [])
        self.assertEqual(len(rb.open_blockers(self.p)), 1)

    def test_resolved_only_once(self):
        rb.add([blocker()], self.p)
        live = lambda tk: [{"status": "ACTIVE", "word_id": "callsvc_example-one"}]
        self.assertEqual(len(rb.check(self.p, lister=live)), 1)
        self.assertEqual(rb.check(self.p, lister=live), [])

    def test_stale_blockers_expire_silently(self):
        rb.add([blocker()], self.p)
        items = json.load(open(self.p))
        items[0]["created"] = (rb._now() - timedelta(days=9)).isoformat(timespec="seconds")
        json.dump(items, open(self.p, "w"))
        called = []
        self.assertEqual(rb.check(self.p, expire_days=5, lister=lambda tk: called.append(tk) or []), [])
        self.assertEqual(called, [])  # expired before spending a composio call
        self.assertEqual(json.load(open(self.p))[0]["status"], "expired")

    def test_resume_message_is_marked_automatic_and_keeps_the_yes_rule(self):
        rb.add([blocker()], self.p)
        msg = rb.resume_message(rb.open_blockers(self.p)[0])
        self.assertTrue(msg.startswith(rb.AUTO_PREFIX))
        self.assertIn(f"Not from {ramu_profile.NAME}", msg)
        self.assertIn("place a test call to the user", msg)
        self.assertIn("still need", msg)
        self.assertIn(f"{ramu_profile.NAME}'s yes", msg)

    def test_missing_or_corrupt_file_is_empty(self):
        self.assertEqual(rb.open_blockers(self.p), [])
        open(self.p, "w").write("{not json")
        self.assertEqual(rb.open_blockers(self.p), [])
        self.assertEqual(rb.check(self.p, lister=lambda tk: []), [])


if __name__ == "__main__":
    unittest.main()
