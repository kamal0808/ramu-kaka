import base64
import os
import sys
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ramu_ingest as ri  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def at(h, m=0, day=29):
    return datetime(2030, 9, day, h, m, tzinfo=IST)


class AuthTests(unittest.TestCase):
    def test_bearer_and_basic(self):
        self.assertTrue(ri.authorized({"Authorization": "Bearer s3cret"}, "s3cret"))
        basic = "Basic " + base64.b64encode(b"user:s3cret").decode()
        self.assertTrue(ri.authorized({"Authorization": basic}, "s3cret"))

    def test_rejects_wrong_missing_and_empty_token(self):
        self.assertFalse(ri.authorized({"Authorization": "Bearer nope"}, "s3cret"))
        self.assertFalse(ri.authorized({}, "s3cret"))
        self.assertFalse(ri.authorized({"Authorization": "Bearer "}, ""))
        self.assertFalse(ri.authorized({"Authorization": "Basic !!!"}, "s3cret"))


class ParseTests(unittest.TestCase):
    def test_owntracks_transition_and_location(self):
        tst = int(at(14, 2).timestamp())
        ev = ri.parse_owntracks({"_type": "transition", "event": "enter", "desc": "Sam's", "tst": tst}, at(14, 3))
        self.assertEqual(ev[0]["kind"], "place")
        self.assertEqual(ev[0]["place"], "Sam's")
        loc = ri.parse_owntracks({"_type": "location", "lat": 40.5, "lon": 10.9, "tst": tst}, at(14, 3))
        self.assertEqual(loc[0]["kind"], "location")
        self.assertEqual(ri.parse_owntracks({"_type": "waypoints"}, at(14)), [])

    def test_otp_sms_is_never_stored(self):
        body = "Your OTP for txn of Rs 2,000 is 482913. Do not share."
        self.assertEqual(ri.parse_generic({"kind": "sms", "from": "BANKSMS", "body": body}, at(14)), [])

    def test_bank_debit_is_summarised_and_masked(self):
        body = "Rs.1,000.00 debited from a/c XX12345678 via UPI to SHOP on 29-09"
        ev = ri.parse_generic({"kind": "sms", "from": "BANKSMS", "body": body}, at(14))[0]
        self.assertEqual((ev["direction"], ev["amount"]), ("debit", 1000.0))
        self.assertNotIn("12345678", ev["text"])

    def test_chatter_sms_dropped(self):
        self.assertEqual(ri.parse_generic({"kind": "sms", "from": "Mom", "body": "call me"}, at(14)), [])

    def test_unknown_kind_dropped(self):
        self.assertEqual(ri.parse_generic({"kind": "rm -rf"}, at(14)), [])


class BankAmountTests(unittest.TestCase):
    def test_available_balance_is_not_the_charge(self):
        # A small USD charge must not be read as the (much larger) available balance.
        s = ri.sms_summary("EXAMPLE BANK", "USD 5.00 has been spent on your EXAMPLE BANK Credit Card ending "
                           "with 1234 at SHOP on 02-10-2030. Avl Bal INR 50,000.00")
        self.assertEqual((s["amount"], s["currency"]), (5.0, "USD"))

    def test_inr_charge_before_balance(self):
        s = ri.sms_summary("EXAMPLE BANK", "INR 400.00 has been spent on your Credit Card at SHOP. Avl Bal INR 50,000.00")
        self.assertEqual((s["amount"], s["currency"]), (400.0, "INR"))

    def test_balance_first_then_charge(self):
        s = ri.sms_summary("X", "Available balance: INR 5,000. Rs 200 debited")
        self.assertEqual(s["amount"], 200.0)


class PresenceTests(unittest.TestCase):
    def test_stale_gps_place_is_marked_unknown(self):
        now = at(10, day=2).replace(month=10)
        p = {"place": "Sam's", "place_since": (now - timedelta(hours=58)).isoformat(),
             "last_location": {"ts": (now - timedelta(hours=58)).isoformat()}}
        self.assertIn("UNKNOWN", ri.presence_block(p, now))

    def test_place_user_said_wins_over_dead_gps(self):
        now = at(10, day=2).replace(month=10)
        p = {"place": "Home", "place_since": now.isoformat(), "place_said": True,
             "last_location": {"ts": (now - timedelta(hours=58)).isoformat()}}
        out = ri.presence_block(p, now)
        self.assertIn("said", out)
        self.assertIn("at 'Home'", out)
        self.assertNotIn("UNKNOWN", out)

    def test_arrive_and_leave_friend(self):
        p, note = ri.apply({}, {"kind": "place", "event": "enter", "place": "Sam's", "ts": at(14).isoformat()}, at(14))
        self.assertIn("arrived at 'Sam's'", note)
        p, note = ri.apply(p, {"kind": "place", "event": "enter", "place": "Sam's", "ts": at(15).isoformat()}, at(15))
        self.assertIsNone(note)  # repeat enter is not news
        p, note = ri.apply(p, {"kind": "place", "event": "leave", "place": "Sam's", "ts": at(21).isoformat()}, at(21))
        self.assertIn("left 'Sam's'", note)
        self.assertIsNone(p["place"])

    def test_wake_after_long_dark_once_a_day(self):
        p = {"last_screen_off": at(3, 0).isoformat()}
        p, note = ri.apply(p, {"kind": "unlock", "ts": at(11, 40).isoformat()}, at(11, 40))
        self.assertIn("likely awake", note)
        p, _ = ri.apply(p, {"kind": "screen_off", "ts": at(12).isoformat()}, at(12))
        p, note = ri.apply(p, {"kind": "unlock", "ts": at(15, 30).isoformat()}, at(15, 30))
        self.assertIsNone(note)

    def test_late_night_unlock_is_not_waking(self):
        p = {"last_screen_off": at(20, 0, day=28).isoformat()}
        _, note = ri.apply(p, {"kind": "unlock", "ts": at(1, 0).isoformat()}, at(1, 0))
        self.assertIsNone(note)

    def test_car_connect_is_driving(self):
        p, note = ri.apply({}, {"kind": "car", "device": "CAR", "state": "connected", "ts": at(20).isoformat()}, at(20))
        self.assertTrue(p["driving"])
        self.assertIn("driving", note)

    def test_presence_block(self):
        self.assertEqual(ri.presence_block({}, at(14)), "")
        p, _ = ri.apply({}, {"kind": "place", "event": "enter", "place": "Home", "ts": at(13).isoformat()}, at(13))
        self.assertIn("at 'Home' since 60m ago", ri.presence_block(p, at(14)))


FRIEND = {"Sam's": {"lat": 40.5146, "lon": 10.9310, "r": 150}}


class DeviceFeedTests(unittest.TestCase):
    def loc(self, lat, lon, h, m=0, spd=0, acc=10):
        return {"kind": "location", "ts": at(h, m).isoformat(), "lat": lat, "lon": lon, "acc": acc, "spd": spd}

    def test_server_side_geofence_enter_and_leave(self):
        p, note = ri.apply({}, self.loc(40.5147, 10.9311, 14), at(14), FRIEND)
        self.assertIn("arrived at 'Sam's'", note)
        p, note = ri.apply(p, self.loc(40.5160, 10.9311, 15), at(15), FRIEND)  # ~155 m: jitter, still there
        self.assertIsNone(note)
        p, note = ri.apply(p, self.loc(40.5300, 10.9311, 21), at(21), FRIEND)
        self.assertIn("left 'Sam's'", note)

    def test_bad_fix_is_ignored_for_places(self):
        p, note = ri.apply({}, self.loc(40.5147, 10.9311, 14, acc=500), at(14), FRIEND)
        self.assertIsNone(note)
        self.assertIsNone(p.get("place"))

    def test_driving_from_speed_once(self):
        p, note = ri.apply({}, self.loc(40.6, 10.8, 20, spd=15), at(20), {})
        self.assertIn("driving speed", note)
        p, note = ri.apply(p, self.loc(40.61, 10.8, 20, 5, spd=16), at(20, 5), {})
        self.assertIsNone(note)

    def test_learn_place_from_last_fix(self):
        p, _ = ri.apply({}, self.loc(40.5, 10.9, 14), at(14), {})
        places, ok = ri.learn_place({}, "Gym", p)
        self.assertTrue(ok)
        self.assertEqual(ri.nearest_place(places, 40.5001, 10.9001)[0], "Gym")
        self.assertFalse(ri.learn_place({}, "Gym", {})[1])

    def test_sms_forwarder_default_payload(self):
        ev = ri.parse_sms_forwarder({"from": "AX-BANKSMS", "text": "INR 500 debited via UPI to SHOP"}, at(14))
        self.assertEqual((ev[0]["direction"], ev[0]["amount"]), ("debit", 500.0))

    def test_health_connect_webhook_payload(self):
        payload = {
            "timestamp": "2030-09-29T06:30:00Z", "app_version": "1.2.3",
            "steps": [{"count": 3200, "start_time": "2030-09-28T18:30:00Z", "end_time": "2030-09-29T06:30:00Z"}],
            "sleep_sessions": [{"session_end_time": "2030-09-29T05:40:00Z", "duration_seconds": 25200, "stages": []}],
            "exercise": [{"start_time": "2030-09-29T04:50:00Z", "duration_seconds": 2400}],
        }
        ev = ri.parse_health(payload, at(12))[0]
        self.assertEqual(ev["steps"], 3200)
        self.assertEqual(ev["sleep_hours"], 7.0)
        self.assertEqual(ev["workout_minutes"], 40)
        p, note = ri.apply({}, ev, at(12))
        self.assertIn("woke at 11:10 after 7.0h sleep", note)
        p, note = ri.apply(p, ev, at(12, 20))  # same session again: not news
        self.assertIsNone(note)

    def test_stale_presence_is_flagged(self):
        p, _ = ri.apply({}, {"kind": "place", "event": "enter", "place": "Home", "ts": at(8).isoformat()}, at(8))
        self.assertIn("may be stale", ri.presence_block(p, at(14)))


class PlaceFromTextTests(unittest.TestCase):
    def test_real_phrasings(self):
        self.assertEqual(ri.place_from_text("i'm still at sam's"), "Sam's")
        self.assertEqual(ri.place_from_text("reached gym"), "Gym")
        self.assertEqual(ri.place_from_text("back home"), "Home")
        self.assertEqual(ri.place_from_text("Reached. Heading in."), None)

    def test_not_places(self):
        for t in ("i am going out", "im tired", "I'm up early today", "i'm fine", ""):
            self.assertIsNone(ri.place_from_text(t), t)

    def test_fresh_fix_only(self):
        p = {"last_location": {"ts": at(14).isoformat(), "lat": 40.5, "lon": 10.9, "acc": 30}}
        self.assertIsNotNone(ri.fresh_fix(p, at(14, 10)))
        self.assertIsNone(ri.fresh_fix(p, at(14, 30)))
        p["last_location"]["acc"] = 400
        self.assertIsNone(ri.fresh_fix(p, at(14, 5)))


class BankEmailTests(unittest.TestCase):
    YES = ("<html><head><style>*{ font-family: Arial; } table td { border: 0; }</style></head><body>"
           "<p>Dear Customer, INR 500.00 has been spent on your EXAMPLE BANK Credit Card "
           "ending with 1234 at SHOP on 29-09-2030 at 02:00:00 pm. Avl Bal INR 50,000.00.</p></body></html>")

    def test_bank_alert_email(self):
        ev = ri.parse_bank_email("Example Bank Alerts <alerts@bank.example.com>", "EXAMPLE BANK - Transaction Alert",
                                 self.YES, "2030-09-29T08:30:00Z", at(15))[0]
        self.assertEqual((ev["direction"], ev["amount"], ev["via"]), ("debit", 500.0, "email"))
        self.assertEqual(ev["ts"], "2030-09-29T14:00:00+05:30")

    def test_style_blocks_gone(self):
        self.assertNotIn("font-family", ri.email_text(self.YES))

    def test_otp_mail_ignored(self):
        self.assertEqual(ri.parse_bank_email("x", "OTP", "Your OTP for INR 500 txn is 123456", None, at(15)), [])


class WhatsAppNotificationTests(unittest.TestCase):
    def test_one_to_one(self):
        ev = ri.parse_notification({"packageName": "com.whatsapp", "title": "Alex", "text": "tomorrow?"}, at(14))[0]
        self.assertEqual((ev["kind"], ev["from"], ev["group"]), ("whatsapp", "Alex", None))

    def test_group_title_and_text_forms(self):
        a = ri.parse_notification({"packageName": "com.whatsapp", "title": "Hiking Crew: Dev", "text": "pics?"}, at(14))[0]
        self.assertEqual((a["group"], a["from"]), ("Hiking Crew", "Dev"))
        b = ri.parse_notification({"packageName": "com.whatsapp", "title": "Hiking Crew", "text": "Sam: done"}, at(14))[0]
        self.assertEqual((b["group"], b["from"], b["preview"]), ("Hiking Crew", "Sam", "done"))

    def test_other_apps_and_noise_dropped(self):
        self.assertEqual(ri.parse_notification({"packageName": "com.google.android.apps.messaging",
                                                "title": "BANKSMS", "text": "OTP 123456"}, at(14)), [])
        for t in ("WhatsApp", "5 new messages", "Missed voice call", "Checking for new messages"):
            self.assertEqual(ri.parse_notification({"packageName": "com.whatsapp", "title": t, "text": "x"}, at(14)), [], t)


class MacActivityTests(unittest.TestCase):
    def mac(self, h, m=0, idle=10, app="Code", host="mac-a", day=29):
        now = at(h, m, day)
        return ri.parse_generic({"kind": "mac", "host": host, "idle_s": idle, "app": app, "interval_s": 300}, now)[0], now

    def run_day(self, p, start_h, n, app="Code", day=29):
        for i in range(n):
            ev, now = self.mac(start_h + (i * 5) // 60, (i * 5) % 60, app=app, day=day)
            p, note = ri.apply(p, ev, now)
        return p, note

    def test_active_minutes_and_idle_ignored(self):
        p, _ = self.run_day({}, 10, 12)
        self.assertEqual(p["mac_day"]["active_min"], 60)
        ev, now = self.mac(12, idle=900)
        p, _ = ri.apply(p, ev, now)
        ev, now = self.mac(12, 5, app="loginwindow", idle=0)
        p, _ = ri.apply(p, ev, now)
        self.assertEqual(p["mac_day"]["active_min"], 60)

    def test_two_macs_same_window_not_double_counted(self):
        ev1, now = self.mac(10, host="mac-a")
        ev2, _ = self.mac(10, host="mac-b")
        p, _ = ri.apply({}, ev1, now)
        p, _ = ri.apply(p, ev2, now + timedelta(seconds=30))
        self.assertEqual(p["mac_day"]["active_min"], 5)

    def test_late_night_counts_toward_yesterday_and_notes_once(self):
        p, _ = self.run_day({}, 18, 6, day=29)
        ev, now = self.mac(1, 40, day=30)
        p, note = ri.apply(p, ev, now)
        self.assertIsNone(note)  # recorded, never pinged in quiet hours
        self.assertEqual(p["mac_day"]["day"], "2030-09-29")
        self.assertEqual(p["mac_day"]["late_until"][11:16], "01:40")
        self.assertIn("worked till 01:40", ri.presence_block(p, at(1, 50, day=30)))

    def test_ten_hour_mark(self):
        p, _ = self.run_day({}, 9, 96)   # 8h
        self.assertIn("480", p["mac_day"]["noted"])
        p, note = self.run_day(p, 17, 24)  # +2h = 10h
        self.assertIn("600", p["mac_day"]["noted"])

    def test_presence_shows_mac_hours(self):
        p, _ = self.run_day({}, 10, 12)
        self.assertIn("1.0h at the Mac today (Code 1.0h)", ri.presence_block(p, at(11, 10)))


class CallRecordTests(unittest.TestCase):
    def test_ramu_call_record_becomes_a_note(self):
        rec = {"client": "ramu-kaka", "duration_sec": 42, "summary": "Leaving Sam's now.",
               "transcript": "Ramu Kaka: It's 9...\nUser: yes, leaving now"}
        ev = ri.parse_call_record(rec, at(21, 2))[0]
        _, note = ri.apply({}, ev, at(21, 2))
        self.assertIn("Leaving Sam's", note)
        self.assertIn("42s", note)

    def test_other_clients_and_empty_calls_ignored(self):
        self.assertEqual(ri.parse_call_record({"client": "other-app", "summary": "x"}, at(21)), [])
        self.assertEqual(ri.parse_call_record({"client": "ramu-kaka"}, at(21)), [])

class OrderTests(unittest.TestCase):
    NOW = datetime(2030, 10, 2, 18, 0, tzinfo=IST)

    def test_merchant_and_real_time_from_late_alert(self):
        with mock.patch.object(ri.ramu_profile, "TZ", IST):
            s = ri.sms_summary("BANK", "INR 400.00 has been spent on your EXAMPLE BANK Credit Card ending with 1234 at "
                           "INSTACART on 02-10-2030 at 11:30:00 am. Avl Bal INR 50,000.00")
        self.assertEqual(s["merchant"], "INSTACART")
        self.assertEqual(s["txn_at"], "2030-10-02T11:30:00+05:30")

    def test_orders_dedupe_mail_and_charge(self):
        sig = [{"type": "food", "title": "Deliveroo pasta delivered", "when": "2030-10-02T13:10:00+05:30"}]
        sms = [{"kind": "sms", "direction": "debit", "amount": 300, "merchant": "DELIVEROO",
                "txn_at": "2030-10-02T12:40:00+05:30", "ts": "2030-10-02T12:41:00+05:30", "text": ""},
               {"kind": "sms", "direction": "debit", "amount": 400, "merchant": "INSTACART",
                "txn_at": "2030-10-02T11:30:00+05:30", "ts": "2030-10-02T17:20:00+05:30", "text": ""},
               {"kind": "sms", "direction": "debit", "amount": 1500, "merchant": "HARDWARE STORE", "ts": "2030-10-02T14:00:00+05:30", "text": ""}]
        out = ri.orders_today(sig, sms, self.NOW)
        self.assertEqual([o["kind"] for o in out], ["grocery", "meal"])


if __name__ == "__main__":
    unittest.main()
