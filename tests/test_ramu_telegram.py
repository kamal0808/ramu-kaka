import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ramu_telegram as tg  # noqa: E402

ME = 123456


def msg(uid, **kw):
    m = {"chat": {"id": ME}, "from": {"id": ME}, "date": 1}
    m.update(kw)
    return {"update_id": uid, "message": m}


class FakeHttp:
    def __init__(self, responses):
        self.calls, self.responses = [], list(responses)

    def __call__(self, url, payload, timeout):
        self.calls.append((url, payload, timeout))
        return self.responses.pop(0)


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self._orig = tg._http_post
        self._t = tempfile.TemporaryDirectory()
        self.off = os.path.join(self._t.name, ".last_update")

    def tearDown(self):
        tg._http_post = self._orig
        self._t.cleanup()

    def test_text_message(self):
        self.assertEqual(tg.normalize_update(msg(1, text=" hi ")),
                         {"update_id": 1, "date": 1, "text": "hi", "type": "message"})

    def test_callback_tap_and_empty_tap(self):
        cq = {"update_id": 2, "callback_query": {"id": "c", "data": "ci:sleep:bad", "from": {"id": ME}, "message": {"date": 5}}}
        self.assertEqual(tg.normalize_update(cq)["text"], "ci:sleep:bad")
        self.assertEqual(tg.normalize_update(cq)["type"], "callback_query")
        cq["callback_query"]["data"] = ""
        self.assertEqual(tg.normalize_update(cq)["type"], "unhandled")

    def test_photo_with_and_without_caption(self):
        u = msg(3, photo=[{"file_id": "small"}, {"file_id": "big"}])
        n = tg.normalize_update(u)
        self.assertEqual((n["text"], n["media_kind"], n["media"]["file_id"]), ("[photo, no caption]", "photo", "big"))
        u = msg(4, photo=[{"file_id": "x"}], caption="look")
        self.assertEqual(tg.normalize_update(u)["text"], "look")

    def test_document_placeholder_forward_and_reply(self):
        u = msg(5, document={"file_id": "d", "file_name": "ticket.pdf"}, forward_origin={"type": "user"},
                reply_to_message={"text": "earlier"})
        n = tg.normalize_update(u)
        self.assertEqual(n["text"], "[document: ticket.pdf]")
        self.assertTrue(n["forwarded"])
        self.assertEqual(n["reply_to_text"], "earlier")

    def test_unknown_shapes_are_never_dropped(self):
        self.assertEqual(tg.normalize_update({"update_id": 6, "poll": {}})["type"], "unhandled")
        self.assertEqual(tg.normalize_update(msg(7, dice={"value": 3}))["type"], "unhandled")

    def test_poll_drops_strangers_but_advances_offset(self):
        stranger = {"update_id": 11, "message": {"from": {"id": 999}, "chat": {"id": 999}, "text": "hack"}}
        tg._http_post = FakeHttp([{"ok": True, "result": [stranger, msg(12, text="real")]}])
        messages, dropped = tg.poll("T", self.off, 0, allowed_chat_id=ME)
        self.assertEqual([m["text"] for m in messages], ["real"])
        self.assertEqual(dropped, 1)
        self.assertEqual(tg.read_offset(self.off), 12)

    def test_poll_sends_offset_plus_one_and_long_poll(self):
        tg.write_offset(self.off, 40)
        fake = FakeHttp([{"ok": True, "result": []}])
        tg._http_post = fake
        tg.poll("T", self.off, 20)
        _, payload, timeout = fake.calls[0]
        self.assertEqual((payload["offset"], payload["timeout"]), (41, 20))
        self.assertGreater(timeout, 20)

    def test_poll_acks_callbacks_after_advancing_offset(self):
        cq = {"update_id": 20, "callback_query": {"id": "cb1", "data": "ok", "from": {"id": ME}, "message": {}}}
        fake = FakeHttp([{"ok": True, "result": [cq]}, {"ok": True, "result": True}])
        tg._http_post = fake
        tg.poll("T", self.off, 0, allowed_chat_id=ME)
        self.assertIn("answerCallbackQuery", fake.calls[1][0])
        self.assertEqual(tg.read_offset(self.off), 20)

    def test_api_error_is_raised_without_the_token(self):
        tg._http_post = FakeHttp([{"ok": False, "description": "Conflict: terminated by other getUpdates"}])
        with self.assertRaises(tg.TelegramError) as ctx:
            tg.call("SECRET123", "getUpdates", {})
        self.assertNotIn("SECRET123", str(ctx.exception))

    def test_send_message_buttons_silent_and_chunking(self):
        fake = FakeHttp([{"ok": True, "result": {}}] * 2)
        tg._http_post = fake
        self.assertTrue(tg.send_message("T", ME, "a" * 4500, buttons=[[["Yes", "y"], ["No", "n"]]], silent=True))
        first, last = fake.calls[0][1], fake.calls[1][1]
        self.assertNotIn("reply_markup", first)
        self.assertEqual(last["reply_markup"]["inline_keyboard"][0][0], {"text": "Yes", "callback_data": "y"})
        self.assertTrue(first["disable_notification"])
        self.assertFalse(tg.send_message("T", ME, ""))

    def test_tls_context_verifies_certificates(self):
        import ssl
        ctx = tg._ssl_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_load_token_from_env_file(self):
        d = self._t.name
        with open(os.path.join(d, ".env"), "w") as f:
            f.write("FOO=1\nexport TELEGRAM_BOT_TOKEN='abc:def'\n")
        old = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        try:
            self.assertEqual(tg.load_token(d), "abc:def")
            self.assertEqual(tg.load_token(os.path.join(d, "nope")), "")
        finally:
            if old is not None:
                os.environ["TELEGRAM_BOT_TOKEN"] = old


if __name__ == "__main__":
    unittest.main()
