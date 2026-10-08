import json
import re
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ramu_capabilities as rc  # noqa: E402

FX_SCRIPT = '#!/bin/bash\ncurl -s "https://open.er-api.com/v6/latest/${1:-USD}" | python3 -c "import sys,json;print(json.load(sys.stdin)[\'rates\'][\'INR\'])"\n'


def cap(**kw):
    base = {"slug": "fx-rate", "title": "Look up a live currency rate",
            "when_to_use": "they ask the exchange rate of any currency against euros",
            "steps": "1. Run the script with the currency code.\n2. Report the number.",
            "script": {"name": "run.sh", "content": FX_SCRIPT}}
    base.update(kw)
    return base


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.d = os.path.join(self._t.name, "capabilities")

    def tearDown(self):
        self._t.cleanup()

    def learn(self, **kw):
        return rc.learn([cap(**kw)], self.d)

    # -- saving -------------------------------------------------------------
    def test_learn_writes_playbook_and_executable_script(self):
        res = self.learn()
        self.assertEqual(res, ["learned: fx-rate [read]"])
        base = os.path.join(self.d, "fx-rate")
        self.assertTrue(os.path.exists(os.path.join(base, "CAPABILITY.md")))
        self.assertTrue(os.stat(os.path.join(base, "run.sh")).st_mode & stat.S_IXUSR)
        c = rc.load_all(self.d)[0]
        self.assertEqual((c["revision"], c["status"], c["tier"]), (1, "active", "read"))

    def test_relearn_bumps_revision_keeps_counters_and_keeps_prev_script(self):
        self.learn()
        rc.record_use([{"slug": "fx-rate", "ok": True, "note": "worked"}], self.d)
        self.learn(script={"name": "run.sh", "content": FX_SCRIPT + "# v2\n"})
        c = rc.load_all(self.d)[0]
        self.assertEqual((c["revision"], c["uses"], c["successes"]), (2, 1, 1))
        self.assertTrue(os.path.exists(os.path.join(self.d, "fx-rate", "run.sh.prev")))

    # -- guards -------------------------------------------------------------
    def test_rejects_secrets(self):
        res = self.learn(script={"name": "run.sh", "content": "curl -H 'Authorization: Bearer sk-abcdefghijklmnopqrstuv' x"})
        self.assertTrue(res[0].startswith("skipped (looks like it contains a secret"))
        self.assertEqual(rc.load_all(self.d), [])

    def test_rejects_work_systems(self):
        # Work terms come from the profile's work_scope_block list; use fake ones here.
        block = re.compile(r"(?i)\b(kubectl|jira|acme|examplecorp)\b")
        with mock.patch.object(rc, "_SCOPE_BLOCK", block):
            for blob in ["kubectl get pods", "jira issue list", "check the Acme staging pipeline", "open examplecorp admin"]:
                res = self.learn(steps=blob, script=None)
                self.assertIn("work/company", res[0], blob)
        self.assertEqual(rc.load_all(self.d), [])

    def test_rejects_dangerous_and_bad_script_name(self):
        self.assertIn("dangerous", self.learn(script={"name": "run.sh", "content": "sudo reboot"})[0])
        self.assertIn("dangerous", self.learn(script={"name": "run.sh", "content": "rm -rf ~/x"})[0])
        self.assertIn("script name", self.learn(script={"name": "../evil.sh", "content": "echo hi"})[0])

    def test_rejects_incomplete_and_bad_input_never_raises(self):
        res = rc.learn([None, "x", {}, {"slug": "a", "title": "t", "when_to_use": "w"}, cap(slug="", title="")], self.d)
        self.assertEqual(len(res), 5)
        self.assertTrue(all(r.startswith("skipped") for r in res))

    # -- tiers --------------------------------------------------------------
    def test_tier_classification(self):
        c = rc.classify_tier
        self.assertEqual(c("composio execute GMAIL_FETCH_EMAILS -d '{}'"), "read")
        self.assertEqual(c("composio execute CLICKUP_CREATE_TASK -d '{}'"), "write")
        self.assertEqual(c("composio execute GMAIL_SEND_EMAIL -d '{}'"), "external")
        self.assertEqual(c("composio execute CALLSVC_MAKE_CALL -d '{}'"), "external")
        self.assertEqual(c("composio execute SOMETHING_NEW_ACTION"), "write")  # unknown -> assume it changes something
        self.assertEqual(c('curl -s https://api.example.com/x'), "read")
        self.assertEqual(c("curl -X POST https://api.example.com/x -d '{}'"), "external")
        self.assertEqual(c("requests.post(url, json=x)"), "external")
        self.assertEqual(c("echo hi > out.txt"), "write")
        self.assertEqual(c("./say.sh 'hello'"), "write")
        self.assertEqual(c("cmd 2>&1 | grep x >/dev/null"), "read")
        self.assertEqual(c("def f(x) -> int:\n    return x >= 3"), "read")

    def test_prose_placeholders_are_not_redirects(self):
        steps = "1. Run: composio execute GMAIL_FETCH_EMAILS -d '{\"query\": \"from:<sender>\"}'\n2. Report subject."
        res = rc.learn([cap(slug="gmail-by-sender", steps=steps, script=None)], self.d)
        self.assertIn("[read]", res[0])
        # ...but a real redirect inside a SCRIPT still counts
        res = rc.learn([cap(slug="saves-file", script={"name": "run.sh", "content": "echo hi > out.txt"})], self.d)
        self.assertIn("[write]", res[0])
        # and an external composio call in prose is still caught
        res = rc.learn([cap(slug="mails-someone", steps="1. composio execute GMAIL_SEND_EMAIL -d '{}'", script=None)], self.d)
        self.assertIn("[external]", res[0])

    def test_external_needs_approval_and_declared_tier_only_raises(self):
        res = self.learn(slug="call-someone", title="Phone someone", when_to_use="they want a call placed",
                         script={"name": "run.sh", "content": "composio execute CALLSVC_MAKE_CALL -d '{}'"})
        self.assertIn("[external]", res[0])
        c = rc.load_all(self.d)[0]
        self.assertEqual(c["status"], "needs_approval")
        # model under-declares: scan wins
        self.assertEqual(c["tier"], "external")
        # model over-declares: honoured
        res = self.learn(slug="looks-harmless", declared_tier="write")
        self.assertIn("[write]", res[0])

    # -- recall -------------------------------------------------------------
    def test_recall_relevant_only_with_run_hint(self):
        self.learn()
        rc.learn([cap(slug="find-gym", title="Find gyms near an area", when_to_use="they want a gym or fitness class nearby",
                      script=None, steps="1. WebSearch gyms in the area.")], self.d)
        out = rc.recall("what is the dollar to euros exchange rate", self.d)
        self.assertIn("fx-rate", out)
        self.assertIn("bash capabilities/fx-rate/run.sh", out)
        self.assertNotIn("find-gym", out)
        self.assertEqual(rc.recall("zebra quantum", self.d), "")
        self.assertIn("follow the steps in capabilities/find-gym/CAPABILITY.md", rc.recall("find a gym nearby", self.d))

    def test_recall_flags_approval_and_budget(self):
        rc.learn([cap(slug="call-someone", title="Phone someone", when_to_use="they want a call placed",
                      script={"name": "run.sh", "content": "composio execute CALLSVC_MAKE_CALL -d '{}'"})], self.d)
        self.assertIn("EXPLICIT YES", rc.recall("place a call for me", self.d))
        for i in range(10):
            rc.learn([cap(slug=f"rate-{i}", title=f"Rate lookup number {i}", when_to_use="exchange rate euros lookup", script=None, steps="x")], self.d)
        out = rc.recall("exchange rate in euros", self.d, budget_tokens=120, max_items=10)
        self.assertLessEqual(rc.rm.estimate_tokens(out), 120)

    # -- reliability --------------------------------------------------------
    def test_use_tracking_marks_broken_after_three_failures_and_heals(self):
        self.learn()
        for _ in range(3):
            rc.record_use([{"slug": "fx-rate", "ok": False, "note": "API 403"}], self.d)
        c = rc.load_all(self.d)[0]
        self.assertEqual((c["status"], c["failures"]), ("broken", 3))
        self.assertIn("BROKEN (last note: API 403)", rc.recall("exchange rate euros", self.d))
        rc.record_use([{"slug": "fx-rate", "ok": True}], self.d)
        self.assertEqual(rc.load_all(self.d)[0]["status"], "active")

    def test_relearn_resets_broken(self):
        self.learn()
        for _ in range(3):
            rc.record_use([{"slug": "fx-rate", "ok": False}], self.d)
        self.learn()
        self.assertEqual(rc.load_all(self.d)[0]["status"], "active")

    def test_record_use_unknown_and_retire(self):
        self.assertEqual(rc.record_use([{"slug": "nope", "ok": True}, "junk"], self.d), ["unknown capability: nope"])
        self.learn()
        rc.retire("fx-rate", self.d)
        self.assertEqual(rc.recall("exchange rate euros", self.d), "")

    # -- audit --------------------------------------------------------------
    def test_audit_log_records_events(self):
        self.learn()
        self.learn(script={"name": "run.sh", "content": "sudo x"})
        rc.record_use([{"slug": "fx-rate", "ok": True}], self.d)
        with open(os.path.join(self.d, "LOG.jsonl")) as f:
            events = [json.loads(l)["event"] for l in f]
        self.assertEqual(events, ["learned", "rejected", "used"])


if __name__ == "__main__":
    unittest.main()
