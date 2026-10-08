import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ramu_learning as rl  # noqa: E402

RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Mental models</title><link>https://blog.example.com/a</link><description>&lt;p&gt;Think &lt;b&gt;better&lt;/b&gt;&lt;/p&gt;</description></item>
<item><title>No link</title></item></channel></rss>"""
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Agents</title><link href="https://example.org/x"/><summary>tools</summary></entry></feed>"""


class LearningTests(unittest.TestCase):
    def test_rss_and_atom(self):
        r = rl.parse_feed("FS", RSS)
        self.assertEqual([(x["title"], x["url"]) for x in r], [("Mental models", "https://blog.example.com/a")])
        self.assertEqual(r[0]["summary"], "Think better")
        a = rl.parse_feed("SW", ATOM)
        self.assertEqual(a[0]["url"], "https://example.org/x")
        self.assertEqual(rl.parse_feed("x", b"not xml"), [])

    def test_hn(self):
        hn = rl.parse_hn({"hits": [{"title": "T", "url": "https://t", "points": 5},
                                   {"title": "Ask HN", "url": None, "objectID": "42"}, {"title": None}]})
        self.assertEqual([h["url"] for h in hn], ["https://t", "https://news.ycombinator.com/item?id=42"])

    def test_pick_must_be_a_real_candidate(self):
        cands = [{"source": "FS", "title": "x", "url": "https://blog.example.com/a"}]
        good = '{"url": "https://blog.example.com/a", "topic": "inversion", "message": "a\\nb\\nTry: c"}'
        self.assertEqual(rl.parse_pick(good, cands)["topic"], "inversion")
        self.assertIsNone(rl.parse_pick('{"url": "https://made.up", "message": "x"}', cands))
        self.assertIsNone(rl.parse_pick("nope", cands))

    def test_never_repeat_a_link(self):
        cands = [{"url": "a"}, {"url": "b"}, {"url": "a"}]
        self.assertEqual([c["url"] for c in rl.fresh(cands, ["b"])], ["a"])


if __name__ == "__main__":
    unittest.main()
