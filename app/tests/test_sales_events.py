# -*- coding: utf-8 -*-
"""Offline tests for the hot-sale finder: the seasonal-events calendar, the
review-momentum (trending/viral) screen, event leverage in drop emails and
social kits, and the /api/pricedrop/events + /admin/pricedrop wiring."""

import datetime
import json
import os
import shutil
import sys
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
import social
import pricedrop
import sales_events
import telegram_admin  # noqa: F401


def _snaps(*pairs):
    return [{"ts": t, "reviews": r} for t, r in pairs]


class TestSalesEvents(unittest.TestCase):

    def test_black_friday_window_active_through_season(self):
        events = sales_events.window_status(datetime.date(2026, 11, 12))
        bf = next(e for e in events if e["id"] == "blackfriday")
        self.assertEqual(bf["status"], "active")
        self.assertIn("LIVE", bf["tagline"])
        self.assertGreater(bf["ends_in"], 0)

    def test_black_friday_incoming_shows_early(self):
        events = sales_events.window_status(datetime.date(2026, 10, 1))
        bf = next(e for e in events if e["id"] == "blackfriday")
        self.assertEqual(bf["status"], "upcoming")
        self.assertGreater(bf["days_until"], 20)

    def test_events_sorted_active_first(self):
        events = sales_events.window_status(datetime.date(2026, 11, 18))
        statuses = [e["status"] for e in events]
        self.assertEqual(statuses[0], "active")
        self.assertIn("upcoming", statuses)

    def test_upcoming_summary_picks_most_urgent(self):
        s = sales_events.upcoming_summary(datetime.date(2026, 11, 18))
        self.assertTrue(s["active"])
        self.assertEqual(s["event"]["id"], "blackfriday")
        self.assertIn("LIVE", s["line"])
        self.assertTrue(any(h.startswith("#BlackFriday") for h in s["hashtags"]))

    def test_custom_event_active_and_incoming(self):
        custom = [{"id": "launch", "name": "My Launch", "emoji": "🚀",
                   "start": "2026-09-10", "end": "2026-09-12",
                   "hashtags": "#MyLaunch"}]
        s = sales_events.upcoming_summary(datetime.date(2026, 9, 11), custom=custom)
        self.assertEqual(s["event"]["id"], "launch")
        self.assertTrue(s["active"])
        s2 = sales_events.upcoming_summary(datetime.date(2026, 9, 1), custom=custom)
        self.assertFalse(s2["active"])
        self.assertEqual(s2["event"]["id"], "launch")
        self.assertEqual(s2["event"]["days_until"], 9)

    def test_year_wrap_custom_event(self):
        custom = [{"name": "New Season", "start": "2026-12-30", "end": "2027-01-02"}]
        s = sales_events.upcoming_summary(datetime.date(2026, 12, 28), custom=custom)
        self.assertEqual(s["event"]["name"], "New Season")
        self.assertEqual(s["event"]["days_until"], 2)
        s2 = sales_events.upcoming_summary(datetime.date(2027, 1, 1), custom=custom)
        self.assertTrue(s2["active"])


class TestTrendEngine(unittest.TestCase):

    def test_flat_history_is_not_trending(self):
        t = pricedrop.compute_trend(_snaps(("2026-05-01", 100), ("2026-05-31", 102)),
                                    now=datetime.date(2026, 6, 1))
        self.assertFalse(t["trending"])
        self.assertFalse(t["viral"])

    def test_sustained_growth_is_trending(self):
        t = pricedrop.compute_trend(
            _snaps(("2026-05-01", 100), ("2026-05-20", 120), ("2026-05-31", 140)),
            now=datetime.date(2026, 6, 1))
        self.assertTrue(t["trending"])
        self.assertFalse(t["viral"])
        self.assertGreaterEqual(t["added"], 30)

    def test_burst_is_viral(self):
        t = pricedrop.compute_trend(_snaps(("2026-05-01", 100), ("2026-05-31", 400)),
                                    now=datetime.date(2026, 6, 1))
        self.assertTrue(t["trending"])
        self.assertTrue(t["viral"])
        self.assertGreaterEqual(t["rate"], 5.0)

    def test_acceleration_spike_is_viral(self):
        t = pricedrop.compute_trend(
            _snaps(("2026-04-15", 50), ("2026-05-01", 55), ("2026-05-15", 60),
                   ("2026-05-28", 120), ("2026-05-31", 135)),
            now=datetime.date(2026, 6, 1))
        self.assertTrue(t["viral"])

    def test_stale_history_ignored(self):
        self.assertIsNone(pricedrop.compute_trend(
            _snaps(("2026-01-01", 100), ("2026-02-01", 102)),
            now=datetime.date(2026, 6, 1)))

    def test_price_store_snapshot_roundtrip(self):
        store = pricedrop.PriceStore("/tmp/pstore_trend_%s.json" % uuid.uuid4().hex[:8])
        store.record("B0ABC", price=19.99, reviews=100, when="2026-05-01")
        store.record("B0ABC", price=19.99, reviews=120, when="2026-05-02")
        store.record("B0ABC", price=14.99, reviews=120, when="2026-05-02")
        snaps = store.snapshots("b0abc")
        self.assertEqual(len(snaps), 2)
        self.assertEqual(snaps[-1]["reviews"], 120)
        self.assertEqual(snaps[-1]["price"], 14.99)
        store.set_baseline("B0ABC", 14.99)  # compat path still works
        self.assertEqual(store.baseline("b0abc"), 14.99)


class TestEventLeverageCopy(unittest.TestCase):

    def test_drop_email_leverages_active_event(self):
        events = sales_events.upcoming_summary(datetime.date(2026, 11, 12))
        mail = pricedrop.drop_email(
            [{"asin": "B0X", "title": "Widget", "old": 30.0, "new": 20.0,
              "drop": 10.0, "drop_pct": 33.3}], events=events)
        self.assertIn("Black Friday", mail["subject"])
        self.assertIn("LIVE", mail["html"])
        self.assertIn("LIVE", mail["text"])

    def test_drop_email_plain_when_no_event(self):
        mail = pricedrop.drop_email(
            [{"asin": "B0X", "title": "Widget", "old": 30.0, "new": 20.0,
              "drop": 10.0, "drop_pct": 33.3}])
        self.assertTrue(mail["subject"].startswith("Price dropped"))

    def test_social_kits_gain_event_hashtag_and_hook(self):
        event = sales_events.upcoming_summary(datetime.date(2026, 11, 18))
        items = [{"asin": "B0X1", "title": "Widget Pro", "price": 22.0,
                  "rating": 4.8, "reviews": 3200}]
        kits = social.post_kits("best widget", items, "https://pstore.test", event=event)
        self.assertEqual(len(kits), len(social.PLATFORMS))
        fb = next(k for k in kits if k["platform"] == "Facebook")
        self.assertIn("#BlackFriday", fb["hashtags"])
        self.assertTrue(fb["body"].startswith("🔥"))
        tw = next(k for k in kits if k["platform"] == "Twitter / X")
        self.assertIn("BlackFriday", tw["hashtags"])
        self.assertLessEqual(len(tw["body"]), 280)
        plain = social.post_kits("best widget", items, "https://pstore.test")
        for k in plain:
            self.assertNotIn("Black Friday", k["hashtags"])

    def test_event_hook_keeps_short_caption_platforms_under_cap(self):
        event = sales_events.upcoming_summary(datetime.date(2026, 11, 18))
        items = [{"asin": "B0X1", "title": "Widget Pro", "price": 22.0,
                  "rating": 4.8, "reviews": 3200}]
        kits = social.post_kits("best widget", items, "https://pstore.test", event=event)
        tt = next(k for k in kits if k["platform"] == "TikTok")
        self.assertLessEqual(len(tt["body"]), 180)
        self.assertIn("BlackFriday", tt["hashtags"])
        th = next(k for k in kits if k["platform"] == "Threads")
        self.assertLessEqual(len(th["body"]), 500)


class TestEventsServer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.db = "/tmp/pstore_test_ev_%s.db" % uuid.uuid4().hex[:8]
        shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "pstore.db"), cls.db)
        os.environ["PSTORE_DB"] = cls.db
        os.environ["PSTORE_ADMIN_EMAIL"] = "owner@test.example"
        os.environ["PSTORE_ADMIN_PASSWORD"] = "test-pass-123"
        os.environ.pop("PSTORE_URL", None)
        import importlib
        importlib.reload(server)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.PORT = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.cookie = cls._login()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    @classmethod
    def _login(cls):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", cls.PORT, timeout=60)
        conn.request("POST", "/admin/login",
                     body=b"email=owner@test.example&password=test-pass-123",
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        resp = conn.getresponse()
        sc = resp.getheader("Set-Cookie")
        resp.read()
        conn.close()
        assert sc and sc.startswith("pstore_admin="), sc
        return sc.split(";")[0]

    @classmethod
    def _raw(cls, path, method="GET", body=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", cls.PORT, timeout=60)
        hdrs = {"Cookie": cls.cookie}
        if body is not None:
            hdrs["Content-Type"] = "application/json"
            body = json.dumps(body).encode()
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        out = (resp.status, resp.read())
        conn.close()
        return out

    def _get(self, path):
        return self._raw(path)

    def _post(self, path, payload):
        return self._raw(path, method="POST", body=payload)

    def test_events_api_get_lists_recurring_and_upcoming(self):
        st, raw = self._get("/api/pricedrop/events")
        self.assertEqual(st, 200)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        self.assertIsInstance(data["custom"], list)
        self.assertGreaterEqual(len(data["events"]), 11)
        self.assertIn("events", data)

    def test_events_api_add_delete_custom(self):
        st, raw = self._post("/api/pricedrop/events", {
            "action": "add", "name": "Test Launch", "emoji": "🚀",
            "start": "2026-12-28", "end": "2027-01-02", "hashtags": "#TestLaunch"})
        self.assertEqual(st, 200)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        st, raw = self._get("/api/pricedrop/events")
        custom = json.loads(raw)["custom"]
        ids = [c["id"] for c in custom]
        self.assertIn("Test Launch".lower().replace(" ", "-"), ids)
        st, raw = self._post("/api/pricedrop/events",
                             {"action": "delete", "id": ids[-1]})
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(raw)["deleted"])

    def test_admin_pricedrop_renders_hot_sale_finder(self):
        st, raw = self._get("/admin/pricedrop")
        html = raw.decode("utf-8", "replace")
        self.assertIn("Hot Sale Finder", html)
        self.assertIn("Add your own event", html)
        self.assertIn("addEvent()", html)
        self.assertIn("Leverage at work", html)


if __name__ == "__main__":
    unittest.main()