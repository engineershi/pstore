# -*- coding: utf-8 -*-
"""Offline integration tests for the weekly money digest wired into the
server: admin page, status API, and the run/dispatch + gate pipeline."""

import json
import os
import shutil
import sys
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mailer
import amazon
import security
import server


def _no_network(req, timeout=None):
    raise OSError("offline test stub")


class TestWeeklyDigestServer(unittest.TestCase):

    IPKEY = "127.0.0.1|127.0.0.1"

    @classmethod
    def setUpClass(cls):
        cls.db = "/tmp/pstore_test_wd_%s.db" % uuid.uuid4().hex[:8]
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
        cls._saved_send = mailer._send
        mailer._send = lambda *a, **k: True
        cls._saved_smtp = (mailer.SMTP_HOST, mailer.SMTP_USER, mailer.SMTP_PASSWORD)
        mailer.SMTP_HOST = "smtp.test.local"
        mailer.SMTP_USER = "x@x"
        mailer.SMTP_PASSWORD = "pw"
        cls.cookie = cls._login()

    @classmethod
    def tearDownClass(cls):
        mailer._send = cls._saved_send
        mailer.SMTP_HOST, mailer.SMTP_USER, mailer.SMTP_PASSWORD = cls._saved_smtp
        security.SUBSCRIBE_LIMITER.clear("sub|" + cls.IPKEY)
        security.TRACK_LIMITER.clear("trk|" + cls.IPKEY)
        security.PAGEVIEW_LIMITER.clear("pv|" + cls.IPKEY)
        cls.httpd.shutdown()
        cls.thread.join(timeout=2)
        cls.httpd.server_close()
        store_path = server._pricedrop_store().path
        for p in (cls.db, store_path):
            if p and os.path.exists(p):
                os.unlink(p)

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
    def _raw(cls, path, method="GET", body=None, cookie=None, headers=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", cls.PORT, timeout=60)
        hdrs = dict(headers or {})
        if cookie:
            hdrs["Cookie"] = cookie
        if body is not None:
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        out = (resp.status, resp.getheader("Content-Type"), resp.read())
        conn.close()
        return out

    def setUp(self):
        security.SUBSCRIBE_LIMITER.clear("sub|" + self.IPKEY)
        security.TRACK_LIMITER.clear("trk|" + self.IPKEY)
        security.API_LIMITER.clear("api|" + self.IPKEY)
        amazon._scraper_search = lambda *a, **k: ([], "")
        amazon._urlopen = _no_network
        with server._lock:
            conn = server._db()
            conn.execute("DELETE FROM subscribers")
            conn.execute("DELETE FROM sent_emails")
            conn.execute("DELETE FROM email_events")
            conn.execute("DELETE FROM clicks")
            conn.execute("DELETE FROM niches")
            conn.execute("DELETE FROM pricewatch")
            conn.execute("DELETE FROM email_sends")
            conn.commit()
            conn.close()
        server._set_setting(server._WEEKLYDIGEST_STATE_KEY, "")
        server._set_setting(server._WEEKLYDIGEST_GATE_KEY, "")
        server._set_setting(server._WEEKLYDIGEST_PRUNED_KEY, "")

    def tearDown(self):
        amazon._urlopen = _no_network

    def _seed(self, n_subs=1):
        """One saved niche with a product, n active CONVERTED subscribers (open +
        clicked a tracked ASIN), a winner click + a real price drop + one
        referrer, so every digest picker hits."""
        with server._lock:
            conn = server._db()
            for i in range(n_subs):
                sid = conn.execute(
                    "INSERT INTO subscribers (email, keyword, confirmed, unsubscribed, "
                    "sent_index, referrals) VALUES (?,?,1,0,0,?)",
                    ("hot%d@x" % i, "keto snacks", 3 if i == 0 else 0)).lastrowid
                conn.execute(
                    "INSERT INTO email_events (type, subscriber_id, email_index, keyword, asin) "
                    "VALUES ('open',?,1,?,?)", (sid, "keto snacks", ""))
                conn.execute(
                    "INSERT INTO clicks (slug, source, referrer, asin) "
                    "VALUES ('keto-snacks','email',?,?)",
                    ("%s|1" % sid, "B012345678"))
            conn.execute(
                "INSERT INTO niches (keyword, market, products, created_at) "
                "VALUES (?,?,?,datetime('now'))",
                ("keto snacks", "com", json.dumps([
                    {"asin": "B012345678", "title": "Keto Gummies",
                     "price": 19.99}])))
            conn.commit()
            conn.close()
        # pricedrop store: baseline $20, latest snapshot $9.99 -> real drop
        store = server._pricedrop_store()
        store.set_baseline("B012345678", 20.00)
        store.record("B012345678", price=9.99, reviews=50,
                     when="2026-09-10")

    def test_admin_weeklydigest_page(self):
        self._seed()
        st, ct, page = self._raw("/admin/weeklydigest", cookie=self.cookie)
        self.assertEqual(st, 200)
        self.assertIn(b"Weekly money digest", page)
        self.assertIn(b"Winners", page)
        self.assertIn(b"Deals", page)
        self.assertIn(b"Referrers", page)
        self.assertIn(b"Quiet niches", page)

    def test_weeklydigest_api_data(self):
        self._seed()
        st, ct, body = self._raw("/api/weeklydigest", cookie=self.cookie)
        self.assertEqual(st, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["week"], server.weeklydigest._iso_week())
        self.assertIn("config", data)
        self.assertIn("gate", data)
        self.assertTrue(data["winners"], data)
        self.assertTrue(data["deals"], data)
        self.assertTrue(data["referrers"], data)

    def test_weeklydigest_run_sends_and_gates(self):
        """POST /api/weeklydigest/run sends each due niche's digest (tracked
        link filled via mailer seam), dedupes, and the weekly gate blocks a
        second run for the same ISO week."""
        self._seed(n_subs=2)
        captured = []
        saved_send = mailer.send
        mailer.send = lambda subject, body, to, attachments=None, pixel_url="", \
            html="", reply_to="", in_reply_to="": (
            captured.append({"to": to, "subject": subject, "body": body,
                             "html": html}) or True)
        try:
            st, ct, body = self._raw("/api/weeklydigest/run", method="POST",
                                     body=b'{"force":true}', cookie=self.cookie)
            self.assertEqual(st, 200)
            data = json.loads(body)
            self.assertTrue(data["ok"], data)
            self.assertEqual(data["sent"], 2, data)
            self.assertEqual(data["already_sent"], 0, data)
            self.assertEqual(len(data["winners"]), 1, data)
            self.assertEqual(len(data["deals"]), 1, data)
            # every digest carries ONE tracked hop, filled from the mailer seam
            self.assertEqual(len(captured), 2)
            htmls = " ".join(c["html"] for c in captured)
            self.assertIn("/e/", htmls)
            self.assertNotIn("{{tracked_link}}", htmls)
            # gate persisted for the niche
            st, ct, body = self._raw("/api/weeklydigest", cookie=self.cookie)
            gate = json.loads(body)["gate"]
            self.assertTrue(gate.get("keto snacks"), gate)
        finally:
            mailer.send = saved_send

    def test_weeklydigest_gate_blocks_same_week(self):
        self._seed(n_subs=1)
        captured = []
        saved_send = mailer.send
        mailer.send = lambda subject, body, to, attachments=None, pixel_url="", \
            html="", reply_to="", in_reply_to="": (
            captured.append({"to": to}) or True)
        try:
            st, _, _ = self._raw("/api/weeklydigest/run", method="POST",
                                 body=b'{"force":true}', cookie=self.cookie)
            self.assertEqual(len(captured), 1)
            # second run same week WITHOUT force: gate blocks it
            st, _, body = self._raw("/api/weeklydigest/run", method="POST",
                                    body=b'{}', cookie=self.cookie)
            self.assertEqual(st, 200)
            data = json.loads(body)
            self.assertEqual(data["sent"], 0, data)
            self.assertEqual(len(captured), 1)  # no duplicate sends
        finally:
            mailer.send = saved_send

    def test_weeklydigest_tick_disabled(self):
        cfg = server._weeklydigest_cfg()
        self._seed(n_subs=1)
        server._set_setting("weeklydigest.enabled", "0")
        try:
            res = server._weeklydigest_tick()
            self.assertEqual(res, "idle")
            state = json.loads(server._get_setting(server._WEEKLYDIGEST_STATE_KEY) or "{}")
            self.assertEqual(state.get("status"), "disabled")
        finally:
            server._set_setting("weeklydigest.enabled", cfg.get("enabled") and "1" or "")

    def test_weeklydigest_never_emits_per_slug_tokens(self):
        """The seam contract: an admin dry-run report must never leak a per-slug
        {{tracked_link_<slug>}} into the payload that would go out literally."""
        self._seed(n_subs=1)
        st, _, body = self._raw("/api/weeklydigest", cookie=self.cookie)
        self.assertEqual(st, 200)
        data = json.loads(body)
        for w in data["winners"] + data["deals"]:
            self.assertNotIn("{{tracked_link_",
                             json.dumps(w).replace("{{tracked_link_", "{{X__"))

    def _seed_quiet(self):
        """A dead niche: 2 OLD clicks (still a 'winner' at min_clicks=1), last
        click 30 days ago → prune-quiet (clicks < 3, quiet ≥ 21 days)."""
        with server._lock:
            conn = server._db()
            conn.execute(
                "INSERT INTO niches (keyword, market, products, created_at) "
                "VALUES ('quiet niche','com',?,datetime('now','-40 days'))",
                (json.dumps([{"asin": "B000000009", "title": "Dead Product",
                              "price": 9.99}]),))
            sid = conn.execute(
                "INSERT INTO subscribers (email, keyword, confirmed, unsubscribed, "
                "sent_index, referrals) VALUES ('quiet@x',?,1,0,0,0)",
                ("quiet niche",)).lastrowid
            conn.execute(
                "INSERT INTO email_events (type, subscriber_id, email_index, keyword, asin) "
                "VALUES ('open',?,1,?,?)", (sid, "quiet niche", ""))
            for _ in range(2):
                conn.execute(
                    "INSERT INTO clicks (slug, source, referrer, asin, created_at) "
                    "VALUES ('quiet-niche','email',?,?,datetime('now','-30 days'))",
                    ("%s|1" % sid, "B000000009"))
            conn.commit()
            conn.close()

    def test_weeklydigest_autoprune_pauses_quiet_niche(self):
        """A niche that is quiet (few old clicks) must NOT be emailed — it is
        recorded in the pruned map and dropped from the due set."""
        self._seed_quiet()
        captured = []
        saved_send = mailer.send
        mailer.send = lambda *a, **k: (captured.append(a) or True)
        try:
            st, _, body = self._raw("/api/weeklydigest/run", method="POST",
                                    body=b'{"force":true}', cookie=self.cookie)
            self.assertEqual(st, 200)
            data = json.loads(body)
            self.assertTrue(data["ok"], data)
            # winner-worthy clicks exist, but quiet-zone auto-pause wins
            self.assertEqual(data["sent"], 0, data)
            self.assertEqual(len(captured), 0)
            self.assertIn("quiet niche", [p["keyword"] for p in data["prune"]], data)
            self.assertNotIn("quiet niche", data["niches"], data)
            pruned = server._wd_pruned_state()
            self.assertIn("quiet niche", pruned, pruned)
        finally:
            mailer.send = saved_send

    def test_weeklydigest_unprune_resumes_niche(self):
        """Paused niches reappear after /api/weeklydigest/unprune: removed from
        the pruned map and their digest gate cleared (so next cycle re-emails)."""
        self._seed_quiet()
        server._wd_pruned_save({"quiet niche": "2026-09-01",
                                "keto snacks": "2026-09-01"})
        server._wd_gate_save("quiet niche", "2026-W37")
        st, _, body = self._raw("/api/weeklydigest/unprune", method="POST",
                                body=b'{"keyword":"quiet niche"}',
                                cookie=self.cookie)
        self.assertEqual(st, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"], data)
        self.assertNotIn("quiet niche", server._wd_pruned_state())
        self.assertNotIn("quiet niche", server._wd_gate_state())
        # untouched niches stay paused
        self.assertIn("keto snacks", server._wd_pruned_state())


if __name__ == "__main__":
    unittest.main()