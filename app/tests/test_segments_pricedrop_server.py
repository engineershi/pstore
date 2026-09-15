# -*- coding: utf-8 -*-
"""Offline integration tests for the lead-segment + price-drop engines wired
into the server (admin pages + JSON APIs)."""

import json
import os
import shutil
import sys
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import indexnow
import mailer
import amazon
import security
import server
import market_engine


def _no_network(req, timeout=None):
    """Plays amazon._urlopen; raises OSError for every route (no network)."""
    raise OSError("offline test stub")


class TestSegmentsAndPricedropServer(unittest.TestCase):

    IPKEY = "127.0.0.1|127.0.0.1"

    @classmethod
    def setUpClass(cls):
        cls.db = "/tmp/pstore_test_seg_%s.db" % uuid.uuid4().hex[:8]
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
        cls._saved_indexnow_post = indexnow._post
        mailer._send = lambda *a, **k: True
        indexnow._post = lambda *a, **k: None
        cls.cookie = cls._login()

    @classmethod
    def tearDownClass(cls):
        mailer._send = cls._saved_send
        indexnow._post = cls._saved_indexnow_post
        security.SUBSCRIBE_LIMITER.clear("sub|" + cls.IPKEY)
        security.TRACK_LIMITER.clear("trk|" + cls.IPKEY)
        security.PAGEVIEW_LIMITER.clear("pv|" + cls.IPKEY)
        cls.httpd.shutdown()
        cls.thread.join(timeout=2)
        cls.httpd.server_close()
        if os.path.exists(cls.db):
            os.unlink(cls.db)

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
        # offline: never hit the network from /api/pricedrop/run
        amazon._scraper_search = lambda *a, **k: ([], "")
        amazon._urlopen = _no_network
        # offline: even if a prior module left a runtime AI key behind, the
        # sequence's AI-copy rewrite must not make a real network call (ai._urlopen
        # raising OSError makes ai.generate return [] -> deterministic fallback)
        import ai as _ai
        self._saved_ai_urlopen = _ai._urlopen
        _ai._urlopen = _no_network
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

    def tearDown(self):
        import ai as _ai
        _ai._urlopen = self._saved_ai_urlopen
        amazon._urlopen = _no_network

    def _seed(self):
        """Create 5 subscribers with distinct engagement, plus a saved niche."""
        with server._lock:
            conn = server._db()
            subs = [
                ("hot@x", "keto snacks", 1, 1, 1),
                ("click2@x", "keto snacks", 1, 1, 1),
                ("warm@x", "keto snacks", 1, 1, 0),
                ("cold@x", "keto snacks", 1, 0, 0),
                ("gone@x", "keto snacks", 1, 1, 1),
            ]
            insertion = []
            for email, kw, _u, _c, _ in subs:
                c = conn.execute(
                    "INSERT INTO subscribers (email, keyword, confirmed, unsubscribed, sent_index) "
                    "VALUES (?,?,1,0,0)", (email, kw)).lastrowid
                insertion.append((email, c))
            # opens: hot, click2, warm (cold never opens, gone unsubscribed)
            for email, sid in insertion:
                if email == "gone@x":
                    conn.execute("UPDATE subscribers SET unsubscribed=1 WHERE id=?",
                                 (sid,))
                if email in ("hot@x", "click2@x", "warm@x"):
                    conn.execute(
                        "INSERT INTO email_events (type, subscriber_id, email_index, keyword, asin) "
                        "VALUES ('open',?,1,?,?)", (sid, "keto snacks", ""))
            # email clicks: hot + click2 (clicked), hot on a product ASIN
            for email, sid in insertion:
                if email == "hot@x":
                    conn.execute(
                        "INSERT INTO clicks (slug, source, referrer, asin) "
                        "VALUES ('keto snacks','email',?,?)",
                        ("%s|1" % sid, "B012345678"))
                elif email == "click2@x":
                    conn.execute(
                        "INSERT INTO clicks (slug, source, referrer, asin) "
                        "VALUES ('keto snacks','email',?,?)",
                        ("%s|1" % sid, ""))
            # a saved niche with one product so pricedrop has something to watch
            conn.execute(
                "INSERT INTO niches (keyword, market, products, created_at) "
                "VALUES (?,?,?,datetime('now'))",
                ("keto snacks", "com", json.dumps([
                    {"asin": "B012345678", "title": "Keto Gummies",
                     "price": 19.99}])))
            conn.commit()
            conn.close()
        return f"https://{self.IPKEY}"

    def test_segments_api_counts(self):
        self._seed()
        st, ct, body = self._raw("/api/segments", cookie=self.cookie)
        self.assertEqual(st, 200)
        data = json.loads(body)
        # hot@x opened + clicked a product ASIN -> CONVERTED (real bucket, was dead)
        self.assertEqual(data["counts"]["converted"], 1)
        # click2@x opened + clicked (no ASIN) -> HOT
        self.assertEqual(data["counts"]["hot"], 1)
        self.assertEqual(data["counts"]["warm"], 1)
        self.assertEqual(data["counts"]["cold"], 1)
        self.assertEqual(data["counts"]["inactive"], 1)
        self.assertEqual(data["total"], 5)
        # per-segment engagement stats surfaced for the ROI lens
        self.assertIn("stats", data)
        self.assertIn("open_rate", data["stats"]["converted"])

    def test_admin_segments_page(self):
        self._seed()
        st, ct, body = self._raw("/admin/segments", cookie=self.cookie)
        self.assertEqual(st, 200)
        self.assertIn(b"Lead lifecycle", body)
        self.assertIn(b"hot@x", body)

    def test_admin_segments_surfaces_referrals(self):
        """The operator sees the referral loop on /admin/segments: a summary
        (referred leads, credits, active referrers), the top referrers, and each
        referred lead resolved back to the subscriber who shared the link."""
        self._seed()
        with server._lock:
            conn = server._db()
            conn.execute("UPDATE subscribers SET ref_token='rrffffff00000001' "
                         "WHERE email='hot@x'")
            conn.execute("UPDATE subscribers SET referrals=1 WHERE email='hot@x'")
            conn.execute("UPDATE subscribers SET referred_by='rrffffff00000001' "
                         "WHERE email='warm@x'")
            conn.commit()
            conn.close()
        st, ct, body = self._raw("/api/segments", cookie=self.cookie)
        self.assertEqual(st, 200)
        data = json.loads(body)
        self.assertEqual(data["referral"]["referred_total"], 1)
        self.assertEqual(data["referral"]["credits"], 1)
        self.assertEqual(data["referral"]["referrers"], 1)
        self.assertEqual(data["referral"]["top"][0]["email"], "hot@x")
        warm = [m for m in data["segments"]["warm"] if m["email"] == "warm@x"]
        self.assertEqual(warm[0]["referrer_email"], "hot@x")
        self.assertEqual(warm[0]["referred_by"], "rrffffff00000001")
        st, ct, page = self._raw("/admin/segments", cookie=self.cookie)
        self.assertEqual(st, 200)
        self.assertIn(b"Referral channel", page)
        self.assertIn(b"Referred leads", page)
        self.assertIn(b"Latest referred leads", page)

    def test_pricedrop_api_watched(self):
        self._seed()
        st, ct, body = self._raw("/api/pricedrop", cookie=self.cookie)
        self.assertEqual(st, 200)
        data = json.loads(body)
        self.assertIn("watched", data)
        self.assertIn("count", data)

    def test_pricedrop_admin_page(self):
        self._seed()
        st, ct, body = self._raw("/admin/pricedrop", cookie=self.cookie)
        self.assertEqual(st, 200)
        self.assertIn(b"Price-drop", body)

    def test_pricedrop_run_background_poll(self):
        """Offline: /api/pricedrop/run must answer instantly (background
        worker) and the GET /api/pricedrop poll must reflect completion —
        no long-blocking HTTP request that drops the browser fetch."""
        import time
        self._seed()
        st, ct, body = self._raw("/api/pricedrop/run", method="POST",
                                 body=b"{}", cookie=self.cookie)
        self.assertEqual(st, 200)
        data = json.loads(body)
        self.assertTrue(data.get("started"))
        self.assertEqual(data.get("total"), 1)
        # the run answers before the scrape: no drops key on the kickoff reply
        self.assertNotIn("drops", data)
        final = None
        for _ in range(50):
            st, ct, body = self._raw("/api/pricedrop", cookie=self.cookie)
            s = json.loads(body)
            if not (s.get("state") or {}).get("running"):
                final = s
                break
            time.sleep(0.05)
        self.assertIsNotNone(final, "background worker never finished")
        self.assertIn("drops", final)
        self.assertEqual(final["state"]["status"], "done")
        self.assertGreaterEqual(final["state"]["checked"], 1)

    def test_pricedrop_second_run_short_circuits_while_running(self):
        """A price-drop check already in progress must short-circuit new run
        requests (started=False) instead of spinning a second worker on top."""
        self._seed()
        entered = threading.Event()
        hold = threading.Event()
        saved_worker = server.Handler._pricedrop_worker

        def blocking_worker(self, rows, min_pct):
            entered.set()
            hold.wait(10)

        server.Handler._pricedrop_worker = blocking_worker
        try:
            st, ct, body = self._raw("/api/pricedrop/run", method="POST",
                                     body=b"{}", cookie=self.cookie)
            self.assertEqual(st, 200)
            self.assertTrue(json.loads(body).get("started"))
            self.assertTrue(entered.wait(5), "worker never started")
            st2, _, body2 = self._raw("/api/pricedrop/run", method="POST",
                                      body=b"{}", cookie=self.cookie)
            d2 = json.loads(body2)
            self.assertEqual(st2, 200)
            self.assertFalse(d2.get("started"))
            self.assertTrue(d2.get("running"))
        finally:
            hold.set()
            server.Handler._pricedrop_worker = saved_worker
            import datetime as _dtt
            server._set_setting(server._PRICEDROP_STATE_KEY, json.dumps(
                {"running": False, "status": "done", "checked": 0, "total": 0,
                 "drops": [],
                 "last_run": _dtt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                 "error": ""}))
        # the worker is released: a later poll sees the run complete
        for _ in range(50):
            st, ct, body = self._raw("/api/pricedrop", cookie=self.cookie)
            s = json.loads(body)
            if not s["state"]["running"]:
                break
            import time
            time.sleep(0.05)

    def test_reengage_cold_sends_only_cold(self):
        """Re-engagement must target only COLD leads and be deduped."""
        self._seed()
        saved = (mailer.SMTP_HOST, mailer.SMTP_USER, mailer.SMTP_PASSWORD)
        mailer.SMTP_HOST = "smtp.test.local"
        mailer.SMTP_USER = "x@x"
        mailer.SMTP_PASSWORD = "pw"
        try:
            captured = []
            mailer._send = lambda subject, body, to, attachments=None, pixel_url=None, **k: (
                captured.append({"to": to}) or True)
            st, ct, body = self._raw("/api/segments/reengage", method="POST",
                                     body=b"{}", cookie=self.cookie)
            self.assertEqual(st, 200)
            data = json.loads(body)
            self.assertEqual(data["sent"], 1)      # only cold@x
            self.assertEqual(data["already_sent"], 0)
            self.assertEqual([c["to"] for c in captured], ["cold@x"])
            # second run: deduped, nothing new sent
            mailer._send = lambda *a, **k: (captured.append({"to": "x"}) or True)
            st2, _, body2 = self._raw("/api/segments/reengage", method="POST",
                                      body=b"{}", cookie=self.cookie)
            d2 = json.loads(body2)
            self.assertEqual(d2["sent"], 0)
            self.assertEqual(d2["already_sent"], 1)
        finally:
            mailer.SMTP_HOST, mailer.SMTP_USER, mailer.SMTP_PASSWORD = saved

    def test_pricedrop_send_offline_no_raises(self):
        """Auto price-drop push must answer 200 and not raise with no network."""
        self._seed()
        st, ct, body = self._raw("/api/pricedrop/send", method="POST",
                                 body=b"{}", cookie=self.cookie)
        self.assertEqual(st, 200)
        data = json.loads(body)
        self.assertEqual(data["ok"], True)
        self.assertIn("drops", data)

    def test_price_alert_captures_watcher_and_referral(self):
        """'Track this price' card converts a visitor into a subscriber AND a
        pricewatch row; the first capture credits the referrer once."""
        self._seed()
        st, ct, body = self._raw("/price-alert", method="POST",
                                 body=json.dumps({"email": "watchy@x.com",
                                                  "asin": "B012345678",
                                                  "keyword": "keto snacks",
                                                  "ref": "abc123"}))
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertTrue(d["ok"], d)
        self.assertIn("/lp/keto-snacks?ref=", d.get("referral_url") or "")
        self.assertTrue(d.get("download_token"))
        with server._lock:
            conn = server._db()
            sub = conn.execute("SELECT id, ref_token, referred_by FROM subscribers "
                               "WHERE email='watchy@x.com'").fetchone()
            watch = conn.execute("SELECT COUNT(*) AS c FROM pricewatch "
                                 "WHERE email='watchy@x.com' AND asin='B012345678'").fetchone()
            conn.close()
        self.assertIsNotNone(sub)
        self.assertEqual(sub["referred_by"], "abc123")
        self.assertTrue(sub["ref_token"])
        self.assertEqual(watch["c"], 1)
        # re-watching the same product never duplicates the watch row
        st2, _, body2 = self._raw("/price-alert", method="POST",
                                  body=json.dumps({"email": "watchy@x.com",
                                                   "asin": "B012345678",
                                                   "keyword": "keto snacks"}))
        d2 = json.loads(body2)
        self.assertTrue(d2["ok"])
        with server._lock:
            conn = server._db()
            c = conn.execute("SELECT COUNT(*) AS c FROM pricewatch "
                             "WHERE email='watchy@x.com'").fetchone()
            referred = conn.execute("SELECT referred_by FROM subscribers "
                                    "WHERE email='watchy@x.com'").fetchone()
            conn.close()
        self.assertEqual(c["c"], 1)
        self.assertEqual(referred["referred_by"], "abc123")

    def test_pricedrop_send_fans_out_to_price_watchers(self):
        """A price-watcher (hot segment member or not) gets their own drop alert
        with a tracked check-price CTA, deduped to one per (watcher, ASIN)."""
        self._seed()
        with server._lock:
            conn = server._db()
            conn.execute(
                "INSERT INTO subscribers (email, keyword, confirmed, unsubscribed, sent_index) "
                "VALUES (?,?,1,0,0)", ("watchy@x.com", "keto snacks"))
            conn.execute("INSERT INTO pricewatch (email, asin, keyword) "
                         "VALUES ('watchy@x.com','B012345678','keto snacks')")
            conn.execute("DELETE FROM email_sends")
            conn.commit()
            conn.close()
        server.Handler._price_store(None).set_baseline("B012345678", 19.99)
        saved_search = amazon.search
        saved_send = mailer._send
        saved_smtp = (mailer.SMTP_HOST, mailer.SMTP_USER, mailer.SMTP_PASSWORD)
        mailer.SMTP_HOST = "smtp.test.local"
        mailer.SMTP_USER = "x@x"
        mailer.SMTP_PASSWORD = "pw"
        captured = []
        amazon.search = lambda asin, top=1: (
            [{"asin": asin, "title": "Keto Gummies", "price": 9.99}], "stub")
        mailer._send = lambda subject, body, to, attachments=None, pixel_url=None, **k: (
            captured.append({"to": to, "subject": subject, "body": body}) or True)
        try:
            st, ct, body = self._raw("/api/pricedrop/send", method="POST",
                                     body=b"{}", cookie=self.cookie)
            self.assertEqual(st, 200)
            data = json.loads(body)
            self.assertTrue(data["ok"], data)
            self.assertGreaterEqual(len(data["drops"]), 1)
            self.assertEqual(data["watcher_emails"], 1)
            hit = [c for c in captured if c["to"] == "watchy@x.com"]
            self.assertEqual(len(hit), 1)
            self.assertIn("drop", hit[0]["subject"].lower())
            self.assertIn("check price", hit[0]["body"].lower())
        finally:
            amazon.search = saved_search
            mailer._send = saved_send
            mailer.SMTP_HOST, mailer.SMTP_USER, mailer.SMTP_PASSWORD = saved_smtp
            with server._lock:
                conn = server._db()
                conn.execute("DELETE FROM email_sends")
                conn.close()

    def test_pint_blitz_publishes_newest_niche_pinterest_kit(self):
        """The one-click Pinterest blitz builds + publishes a Pinterest kit for
        the newest saved niche with products and flips its social_post row to
        published."""
        self._seed()
        st, ct, body = self._raw("/api/social/pint", method="POST",
                                 body=b'{"limit": 5}', cookie=self.cookie)
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertTrue(d["ok"], d)
        self.assertTrue(d["published"] >= 1, d)
        self.assertIn("keto snacks", d["niches"])
        with server._lock:
            conn = server._db()
            row = conn.execute("SELECT platform, status FROM social_posts "
                               "WHERE lower(slug)='keto-snacks' AND platform='Pinterest'").fetchone()
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "published")

    def test_sequence_send_branches_converted_to_upsell(self):
        """Segment-aware sequence: a lead who clicked a product ASIN (hot@x in
        _seed -> CONVERTED) gets the review + value-ladder upsell follow-up, not
        another nurture email, and is marked fully nurtured (sent_index reaches
        SEQUENCE_LENGTH). Nothing is sent to the unsubscribed lead."""
        self._seed()
        saved = (mailer.SMTP_HOST, mailer.SMTP_USER, mailer.SMTP_PASSWORD)
        mailer.SMTP_HOST = "smtp.test.local"
        mailer.SMTP_USER = "x@x"
        mailer.SMTP_PASSWORD = "pw"
        captured = []
        mailer._send = lambda subject, body, to, attachments=None, pixel_url=None, **k: (
            captured.append({"to": to, "subject": subject}) or True)
        try:
            st, ct, body = self._raw("/api/sequence/send", method="POST",
                                     body=b"{}", cookie=self.cookie)
            self.assertEqual(st, 200)
            data = json.loads(body)
            self.assertTrue(data["ok"], data)
            # 4 eligible (converted+hot+warm+cold); gone@x is unsubscribed/inactive
            self.assertEqual(data["sent"], 4)
            self.assertEqual(data["converted"], 1)
            by_to = {c["to"]: c["subject"] for c in captured}
            self.assertIn("hot@x", by_to)
            self.assertIn("ladder", by_to["hot@x"].lower())
            # the converted lead is fully nurtured, not advanced by one
            with server._lock:
                conn = server._db()
                hot = conn.execute(
                    "SELECT sent_index FROM subscribers WHERE email='hot@x'").fetchone()
                cold = conn.execute(
                    "SELECT sent_index FROM subscribers WHERE email='cold@x'").fetchone()
                gone = conn.execute(
                    "SELECT sent_index FROM subscribers WHERE email='gone@x'").fetchone()
                conn.close()
            self.assertEqual(hot["sent_index"], mailer.SEQUENCE_LENGTH)
            self.assertEqual(cold["sent_index"], 1)
            self.assertEqual(gone["sent_index"], 0)
        finally:
            mailer.SMTP_HOST, mailer.SMTP_USER, mailer.SMTP_PASSWORD = saved
        # and the converted follow-up copy exists as a deterministic template
        items = json.loads(self._seed_get_products())
        mail = market_engine.build_converted_followup("keto snacks", items)
        self.assertIsNotNone(mail)
        self.assertIn("ladder", mail["subject"].lower())

    def test_subjects_autoclean_disables_low_opener(self):
        """Email-subject A/B auto-cleanup keeps the high-opening variant and
        disables the one opening below 25% of the winner, once a position has
        enough lifetime sends."""
        server._set_setting("ab.subjects_min_sends", "5")
        try:
            with server._lock:
                conn = server._db()
                conn.execute(
                    "INSERT INTO email_subjects (keyword, email_index, variant, subject, enabled) "
                    "VALUES ('keto snacks',1,1,'Variant One',1),('keto snacks',1,2,'Variant Two',1)")
                for i in range(1, 31):
                    sid = conn.execute(
                        "INSERT INTO subscribers (email, keyword, confirmed, unsubscribed, sent_index) "
                        "VALUES (?,?,1,0,0)", ("v1_%s@x" % i, "keto snacks")).lastrowid
                    conn.execute(
                        "INSERT INTO sent_emails (subscriber_id, email_index, subject, subject_variant) "
                        "VALUES (?,1,'Variant One',1)", (sid,))
                    conn.execute(
                        "INSERT INTO email_events (type, subscriber_id, email_index, keyword, asin) "
                        "VALUES ('open',?,1,?,'')", (sid, "keto snacks"))
                for i in range(1, 31):
                    sid = conn.execute(
                        "INSERT INTO subscribers (email, keyword, confirmed, unsubscribed, sent_index) "
                        "VALUES (?,?,1,0,0)", ("v2_%s@x" % i, "keto snacks")).lastrowid
                    conn.execute(
                        "INSERT INTO sent_emails (subscriber_id, email_index, subject, subject_variant) "
                        "VALUES (?,1,'Variant Two',2)", (sid,))
                    if i <= 2:  # only 2 of 30 open variant Two
                        conn.execute(
                            "INSERT INTO email_events (type, subscriber_id, email_index, keyword, asin) "
                            "VALUES ('open',?,1,?,'')", (sid, "keto snacks"))
                conn.commit()
                conn.close()
            st, ct, body = self._raw("/api/subjects/autoclean", method="POST",
                                     body=b"{}", cookie=self.cookie)
            self.assertEqual(st, 200)
            data = json.loads(body)
            self.assertTrue(data["ok"])
            self.assertEqual(len(data["changed"]), 1)
            self.assertEqual(data["changed"][0]["disabled"], [2])
            self.assertEqual(data["changed"][0]["kept"], 1)
            with server._lock:
                conn = server._db()
                en1 = conn.execute("SELECT enabled FROM email_subjects "
                                   "WHERE keyword='keto snacks' AND variant=1").fetchone()
                en2 = conn.execute("SELECT enabled FROM email_subjects "
                                   "WHERE keyword='keto snacks' AND variant=2").fetchone()
                conn.close()
            self.assertEqual(en1["enabled"], 1)
            self.assertEqual(en2["enabled"], 0)
        finally:
            server._set_setting("ab.subjects_min_sends", "")

    def _seed_get_products(self):
        with server._lock:
            conn = server._db()
            r = conn.execute("SELECT products FROM niches WHERE keyword=?",
                             ("keto snacks",)).fetchone()
            conn.close()
        return r["products"] if r else "[]"

    # ------------------------------------------------------------ system console
    def test_system_api_requires_login(self):
        st, ct, body = self._raw("/api/system")
        self.assertEqual(st, 401)

    def test_system_api_payload_shape(self):
        self._seed()
        st, ct, body = self._raw("/api/system", cookie=self.cookie)
        self.assertEqual(st, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        names = {h["name"] for h in data["completed"]}
        self.assertTrue({"content", "social", "outbox", "inbox", "autosend",
                         "refresh", "http", "pricedrop"} <= names)
        self.assertTrue(data["schedule"])
        self.assertIn("outbox_scheduled", data["queues"])
        self.assertIn("social_published_today", data["queues"])
        self.assertIn("smtp", data["config"])
        self.assertIn("db", data)
        self.assertIn("threads", data)
        self.assertIsInstance(data["issues"], list)
        for t in data["threads"]:
            self.assertIn("alive", t)

    def test_system_heartbeat_flags_error_and_stale(self):
        import time as _t
        saved = dict(server._HEARTBEATS)
        try:
            server._HEARTBEATS["outbox"] = {
                "last": _t.time() - 10000, "last_ok": _t.time() - 10000,
                "last_err": 0, "err": ""}
            server._HEARTBEATS["autosend"] = {
                "last": _t.time() - 5, "last_ok": 0, "last_err": _t.time() - 5,
                "err": "smtp 530 auth failed"}
            st, ct, body = self._raw("/api/system", cookie=self.cookie)
            self.assertEqual(st, 200)
            data = json.loads(body)
            by_name = {h["name"]: h for h in data["completed"]}
            self.assertEqual(by_name["outbox"]["status"], "stale")
            self.assertEqual(by_name["autosend"]["status"], "error")
            issue_blob = " ".join(data["issues"]).lower()
            self.assertIn("smtp 530", issue_blob)
            self.assertIn("not reported", issue_blob)
        finally:
            server._HEARTBEATS.clear()
            server._HEARTBEATS.update(saved)

    def test_admin_system_page(self):
        self._seed()
        st, ct, body = self._raw("/admin/system", cookie=self.cookie)
        self.assertEqual(st, 200)
        self.assertIn(b"System console", body)
        self.assertIn(b"Automation health", body)
        self.assertIn(b"Scheduled automation", body)
        self.assertIn(b"Issues to look at", body)
        self.assertIn(b"Queues", body)
        self.assertIn(b"Background threads", body)
        self.assertIn(b"setInterval(tick, 4000)", body)
        # long-page chrome: collapsible section FAB + back-to-top pill
        self.assertIn(b'class="secfab"', body)
        self.assertIn(b'section-nav.js', body)
        self.assertIn(b'id="sec-health"', body)
        self.assertIn(b'href="#sec-pinterest"', body)
        self.assertIn(b'class="totop"', body)

    def test_section_nav_js_served(self):
        st, ct, body = self._raw("/section-nav.js")
        self.assertEqual(st, 200)
        self.assertIn("application/javascript", ct)
        self.assertIn(b"secfab", body)

    def test_system_console_engine_state_matches_hub(self):
        """Console engine flags derive from webmasters.engines_status(), so a Bing
        key saved under seoeng.bing.apikey reads 'connected' here too (regression:
        the console used to read only the bing.api_key setting and always said
        'not connected' even when Bing was registered and syncing)."""
        self._seed()
        saved = server._get_setting("seoeng.bing.apikey")
        try:
            server._set_setting("seoeng.bing.apikey", "k-console-test")
            st, ct, body = self._raw("/api/system", cookie=self.cookie)
            self.assertEqual(st, 200)
            d = json.loads(body)["discovery"]
            self.assertTrue(d["engines"]["bing"])
            states = {e["engine"]: e for e in d["engine_states"]}
            self.assertEqual(states["bing"]["state"], "ready")
            self.assertTrue(states["bing"]["connected"])
            for eng in ("duckduckgo", "yahoo"):
                self.assertIn(eng, states)
            st, ct, page = self._raw("/admin/system", cookie=self.cookie)
            html = page.decode("utf-8", "replace")
            self.assertIn("Bing Webmaster", html)
        finally:
            server._set_setting("seoeng.bing.apikey", saved or "")

    def test_system_api_monitoring_blocks(self):
        """The console payload exposes the full monitoring surface: API health,
        indexing/search-engine discovery, the end-user funnel and the live
        RSS/sitemap/robots status (all derived offline from the DB)."""
        self._seed()
        st, ct, body = self._raw("/api/system", cookie=self.cookie)
        self.assertEqual(st, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        # API health
        self.assertIn("api", data)
        self.assertIsInstance(data["api"]["routes"], list)
        self.assertGreaterEqual(data["api"]["total_hits"], 0)
        self.assertIsInstance(data["api"]["errors"], list)
        # discovery / indexing
        d = data["discovery"]
        self.assertGreater(d["sitemap_entries"], 0)
        self.assertGreater(d["indexable_niches"], 0)
        self.assertGreaterEqual(d["noindex_niches"], 0)
        self.assertGreaterEqual(d["topics_live"], 0)
        self.assertIn("indexnow_key", d)
        self.assertIn("indexnow_last", d)
        self.assertIsInstance(d["engines"], dict)
        for eng in ("gsc", "bing", "yandex"):
            self.assertIn(eng, d["engines"])
        self.assertIsInstance(d["engine_states"], list)
        for eng in ("gsc", "bing", "yandex", "duckduckgo", "yahoo"):
            self.assertIn(eng, {e["engine"] for e in d["engine_states"]})
        self.assertIsInstance(d["engine_traffic"], list)
        # funnel
        f = data["funnel"]
        for key in ("subs_today", "views_today", "opens_today",
                    "email_clicks_today", "referrers", "referred_total",
                    "pricewatch_drops"):
            self.assertIn(key, f)
        self.assertIsInstance(f["published_per_platform"], list)
        self.assertIsInstance(f["clicks_source_today"], list)
        self.assertIsInstance(f["clicks_source_7d"], list)
        self.assertIn("earnings_est", f)
        # live surface
        lv = data["live"]
        self.assertIn("items", lv["feed"])
        self.assertIn("images", lv["feed"])
        self.assertIn("newest", lv["feed"])
        self.assertIn("feed_url", lv)
        self.assertIn("/rss.xml", lv["feed_url"])
        self.assertIn("surface", lv)
        for key in ("rss", "sitemap", "robots"):
            self.assertIn(key, lv["surface"])
        m = data["queues"]
        self.assertGreaterEqual(m["clicks_today"], 2)
        # Pinterest quick-traffic zone
        pz = data.get("pinterest")
        self.assertIsInstance(pz, dict)
        for key in ("connected", "boards", "auto_board", "pins_today",
                    "pins_total", "clicks_today", "clicks_7d", "clicks_total",
                    "top_niches", "drip_on", "drip_daily", "drip_last"):
            self.assertIn(key, pz)
        self.assertIsInstance(pz["top_niches"], list)
        # API-health verdict drives the console strip
        for key in ("health", "health_note", "last_pin"):
            self.assertIn(key, pz)
        self.assertIn(pz["health"], ("ok", "warn", "err", "off"))
        self.assertIsInstance(pz["health_note"], str)

    def test_system_api_route_telemetry_tallies_responses(self):
        """Per-route tallies count every dispatched _send response, so API health
        shows both the hot paths (200s) and the gated ones (401s)."""
        before = dict(server._API_STATS)
        try:
            self._raw("/api/system", cookie=self.cookie)
            self._raw("/api/system")  # no cookie -> 401
            self._raw("/robots.txt")
            hits = server._API_STATS.get("/api/system", {}).get("hits", 0)
            self.assertGreaterEqual(hits, 2)
            self.assertGreaterEqual(server._API_STATS.get("/robots.txt", {}).get("hits", 0), 1)
            st, ct, body = self._raw("/api/system", cookie=self.cookie)
            data = json.loads(body)
            routes = {r["path"]: r for r in data["api"]["routes"]}
            self.assertIn("/api/system", routes)
            self.assertGreaterEqual(routes["/api/system"]["hits"], 2)
            self.assertGreaterEqual(routes["/api/system"]["4xx"], 1)
        finally:
            server._API_STATS.clear()
            server._API_STATS.update(before)

    def test_admin_system_page_monitor_sections(self):
        self._seed()
        st, ct, body = self._raw("/admin/system", cookie=self.cookie)
        self.assertEqual(st, 200)
        html = body.decode("utf-8", "replace")
        for section in ("API health", "Indexing &amp; search engines",
                        "End-user funnel", "Live surface status", "/rss.xml",
                        "sitemap entries", "URLs submitted via IndexNow",
                        "Published today by platform"):
            self.assertIn(section, html)

    def test_system_payload_wave_b_stats_present(self):
        """Wave B surface: sub_interests + AB matchups in queues, lead-gate
        subscribes + nudge impressions in the funnel — all derived offline."""
        self._seed()
        with server._lock:
            conn = server._db()
            conn.execute(
                "INSERT INTO sub_interests (subscriber_id, keyword, sent_index) "
                "VALUES (?,?,0)", (1, "green tea"))
            conn.execute(
                "INSERT INTO niche_variants (slug, variant, headline, enabled) "
                "VALUES ('keto snacks','ab-1','Keto picks 2026',1)")
            conn.execute(
                "INSERT INTO niche_variants (slug, variant, headline, enabled) "
                "VALUES ('keto snacks','ab-2','The keto pantry list',1)")
            conn.execute(
                "INSERT INTO subscribers (email, keyword, source, confirmed, unsubscribed, sent_index) "
                "VALUES ('gate@x','keto snacks','niche-gate',1,0,0)")
            conn.commit()
            conn.close()
        st, ct, body = self._raw("/api/system", cookie=self.cookie)
        self.assertEqual(st, 200)
        data = json.loads(body)
        q = data["queues"]
        self.assertIn("sub_interests", q)
        self.assertGreaterEqual(q["sub_interests"], 1)
        self.assertIn("ab_matchups", q)
        self.assertGreaterEqual(q["ab_matchups"], 1)
        f = data["funnel"]
        self.assertIn("gate_subs_today", f)
        self.assertIn("gate_subs_7d", f)
        self.assertIn("gate_rate", f)
        self.assertIn("nudge_today", f)
        self.assertGreaterEqual(f["gate_subs_today"], 1)

    def test_system_api_route_telemetry_records_last_and_latency(self):
        """Every API route now carries its live state: last-seen time + last
        latency, so the console can show data-fetch + response health."""
        before = dict(server._API_STATS)
        try:
            self._raw("/api/system", cookie=self.cookie)
            st, ct, body = self._raw("/api/system", cookie=self.cookie)
            data = json.loads(body)
            routes = {r["path"]: r for r in data["api"]["routes"]}
            row = routes["/api/system"]
            self.assertGreaterEqual(row["hits"], 2)
            self.assertTrue(row.get("last"))
            self.assertIn("UTC", row["last"])
            self.assertIsNotNone(row.get("last_ms"))
            self.assertGreaterEqual(data["api"]["live_count"], 1)
            self.assertIn("live_count", data["api"])
        finally:
            server._API_STATS.clear()
            server._API_STATS.update(before)

    def test_system_config_exposes_wave_b_toogles(self):
        """The config panel now reports the price-drop auto mode, the A/B
        auto-enroll flag and the social webhook health snapshot."""
        saved_auto = server._get_setting("pricedrop.auto", "1")
        saved_interval = server._get_setting("pricedrop.auto_interval", "6")
        try:
            server._set_setting("pricedrop.auto", "0")
            server._set_setting("pricedrop.auto_interval", "12")
            st, ct, body = self._raw("/api/system", cookie=self.cookie)
            self.assertEqual(st, 200)
            cfg = json.loads(body)["config"]
            self.assertFalse(cfg["price_drop_auto"])
            self.assertEqual(cfg["price_drop_interval_hours"], 12)
            self.assertTrue(cfg["ab_autoenroll"])
            self.assertIn("webhook_health", cfg)
            for k in ("last", "last_ok", "last_fail", "ok", "fail", "err"):
                self.assertIn(k, cfg["webhook_health"])
        finally:
            server._set_setting("pricedrop.auto", saved_auto)
            server._set_setting("pricedrop.auto_interval", saved_interval)

    def test_pricedrop_config_api_saves_auto_mode(self):
        """POST /api/pricedrop/config flips the watcher between manual-only and
        automatic (interval persisted), and the admin page exposes the toggle."""
        saved_auto = server._get_setting("pricedrop.auto", "1")
        saved_interval = server._get_setting("pricedrop.auto_interval", "6")
        try:
            st, ct, body = self._raw(
                "/api/pricedrop/config", "POST",
                body=b'{"auto":"0","interval_hours":12}', cookie=self.cookie)
            self.assertEqual(st, 200)
            d = json.loads(body)
            self.assertTrue(d["ok"])
            self.assertFalse(d["auto"])
            self.assertEqual(d["interval_hours"], 12)
            self.assertEqual(server._get_setting("pricedrop.auto"), "0")
            st, ct, body = self._raw("/api/pricedrop/state", cookie=self.cookie)
            self.assertEqual(st, 200)
            sd = json.loads(body)
            self.assertFalse(sd["auto"])
            self.assertEqual(sd["interval_hours"], 12)
            self.assertIn("state", sd)
            st, ct, body = self._raw("/admin/pricedrop", cookie=self.cookie)
            self.assertEqual(st, 200)
            html = body.decode("utf-8", "replace")
            self.assertIn('id="auto-on"', html)
            self.assertIn('id="auto-hrs"', html)
            self.assertIn("saveCfg", html)
            self.assertIn("Watcher mode", html)
        finally:
            server._set_setting("pricedrop.auto", saved_auto)
            server._set_setting("pricedrop.auto_interval", saved_interval)

    def test_system_wh_broken_webhook_surfaces_issue(self):
        """A configured-but-dead webhook (e.g. an expired free trial) must stop
        reporting clean: the console shows the last POST error and an issue."""
        saved_hook = server._SOCIAL_WEBHOOK
        saved_setting = server._get_setting("social.webhook", "")
        saved_native = dict(server._API_STATS)
        with server._WEBHOOK_LOCK:
            saved_stats = dict(server._WEBHOOK_STATS)
        try:
            server._set_setting("social.webhook", "https://expired.example/hook")
            server._SOCIAL_WEBHOOK = ""
            with server._WEBHOOK_LOCK:
                server._WEBHOOK_STATS.clear()
                server._WEBHOOK_STATS.update({
                    "last": 123.0, "last_ok": 0.0, "last_fail": 123.0,
                    "ok": 0, "fail": 2, "err": "HTTP Error 410: Gone"})
            st, ct, body = self._raw("/api/system", cookie=self.cookie)
            self.assertEqual(st, 200)
            data = json.loads(body)
            cfg = data["config"]
            self.assertTrue(cfg["webhook"])
            self.assertEqual(cfg["webhook_health"]["fail"], 2)
            blob = " ".join(data["issues"]).lower()
            self.assertIn("webhook", blob)
            self.assertIn("not responding", blob)
            self.assertIn("expired", blob)
        finally:
            server._set_setting("social.webhook", saved_setting)
            server._SOCIAL_WEBHOOK = saved_hook
            with server._WEBHOOK_LOCK:
                server._WEBHOOK_STATS.clear()
                server._WEBHOOK_STATS.update(saved_stats)
            server._API_STATS.clear()
            server._API_STATS.update(dict(saved_native))

    def test_courier_js_reports_nudge_impressions(self):
        """courier.js must beacon a 'nudge' event the moment the MME-5
        exit/scroll nudge is shown, so the console funnel can count them."""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "static", "courier.js")
        with open(path, "r", encoding="utf-8") as fh:
            js = fh.read()
        self.assertIn('beacon("nudge")', js)
        self.assertIn("showNudge", js)
        self.assertIn('setItem("pstore_nudged", "1")', js)


if __name__ == "__main__":
    unittest.main()
