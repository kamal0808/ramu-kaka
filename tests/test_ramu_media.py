import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ramu_profile  # noqa: E402
import ramu_media as rm  # noqa: E402


class R:
    def __init__(self, stdout=""):
        self.stdout, self.returncode = stdout, 0


class MediaTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.d = self._t.name

    def tearDown(self):
        self._t.cleanup()

    def fetch_as(self, name, content=b"x"):
        def fetch(file_id):
            p = os.path.join(self.d, name)
            with open(p, "wb") as f:
                f.write(content)
            return p
        return fetch

    def test_plain_text_has_no_media(self):
        self.assertEqual(rm.ingest({"text": "hi"}, None), {"note": "", "paths": []})

    def test_photo_is_passed_to_the_model(self):
        out = rm.ingest({"media_kind": "photo", "media": {"file_id": "f"}}, self.fetch_as("a.jpg"))
        self.assertEqual(len(out["paths"]), 1)
        self.assertEqual(out["note"], "")

    def test_location_and_contact_need_no_download(self):
        loc = rm.ingest({"media_kind": "location", "media": {"latitude": 40.5, "longitude": 10.9}}, None)
        self.assertIn("40.5, 10.9", loc["note"])
        con = rm.ingest({"media_kind": "contact", "media": {"first_name": "Sam", "phone_number": "999"}}, None)
        self.assertIn("Sam 999", con["note"])

    def test_voice_note_is_transcribed_with_absolute_paths(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return R("  hello   there \n") if cmd[0] == rm.WHISPER else R()
        out = rm.ingest({"media_kind": "voice", "media": {"file_id": "f"}}, self.fetch_as("v.oga"), run)
        self.assertEqual(out["note"], f'[voice note, {ramu_profile.NAME} said: "hello there"]')
        self.assertEqual([c[0] for c in calls], [rm.FFMPEG, rm.WHISPER])
        self.assertTrue(all(os.path.isabs(c[0]) for c in calls))

    def test_silent_voice_note_is_reported_honestly(self):
        out = rm.ingest({"media_kind": "voice", "media": {"file_id": "f"}}, self.fetch_as("v.oga"), lambda *a, **k: R(""))
        self.assertIn("nothing intelligible", out["note"])

    def test_video_gets_transcript_and_frames(self):
        def run(cmd, **kw):
            if cmd[0] == rm.FFMPEG and "-vf" in cmd:
                stem = cmd[-1].replace("_%d.jpg", "")
                open(f"{stem}_1.jpg", "wb").close()
            return R("look at this") if cmd[0] == rm.WHISPER else R()
        out = rm.ingest({"media_kind": "video", "media": {"file_id": "f"}}, self.fetch_as("m.mp4"), run)
        self.assertIn("look at this", out["note"])
        self.assertEqual(len(out["paths"]), 1)

    def test_documents_by_type(self):
        pdf = rm.ingest({"media_kind": "document", "media": {"file_id": "f", "file_name": "bill.pdf"}}, self.fetch_as("bill.pdf"))
        self.assertEqual(len(pdf["paths"]), 1)
        txt = rm.ingest({"media_kind": "document", "media": {"file_id": "f", "file_name": "n.txt"}}, self.fetch_as("n.txt", b"steps 9000"))
        self.assertIn("steps 9000", txt["note"])
        other = rm.ingest({"media_kind": "document", "media": {"file_id": "f", "file_name": "z.zip"}}, self.fetch_as("z.zip"))
        self.assertIn("saved at", other["note"])

    def test_failed_download_becomes_an_honest_note_not_a_crash(self):
        def boom(file_id):
            raise RuntimeError("file is over the 20MB bot download limit")
        out = rm.ingest({"media_kind": "video", "media": {"file_id": "f"}}, boom)
        self.assertIn("could not be fetched", out["note"])
        self.assertIn("20MB", out["note"])

    def test_cleanup_removes_only_old_files(self):
        old, new = os.path.join(self.d, "old"), os.path.join(self.d, "new")
        for p in (old, new):
            open(p, "w").close()
        past = time.time() - 5 * 86400
        os.utime(old, (past, past))
        rm.cleanup(self.d, 3)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))


if __name__ == "__main__":
    unittest.main()
