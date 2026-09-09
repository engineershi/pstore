# -*- coding: utf-8 -*-
"""Offline tests for the Daily Content Engine + audience-lite: the unattended
builder/kit-queuer (settings caps, idempotent reruns), the /api/content
endpoint, country capture on the beacons (CF-IPCountry via Cloudflare), and
the "Who is searching" block inside the SEM payload."""

import json
import os
import shutil
import sys
import threading
import unittest
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import amazon
import audience
import indexnow
import security
import server
import seo


class TestContentAndAudience(unittest.TestCase):
    """Boots a real HTTP server against a copied DB so the engine, the beacon
    geo capture and the SEM who-block run against the shipped handlers."""

    IPKEY = "127.0.0.1|127.0.0.1"
    FAKE_TERMS = ["keto snacks under 100 calories", "keto snacks for diabetics",
                  "keto snack ideas"]

    @classmethod
    def setUpClass(cls):
        cls.db = "/tmp/pstore_content_%s.db" % uuid.uuid4().hex[:8]
        shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "pstore.db"), cls.db)
        os.environ["PSTORE_DB"] = cls.db
        os.environ["PSTORE_ADMIN_EMAIL"] = "owner@test.example"
        os.environ["PSTORE_ADMIN_PASSWORD"] = "test-pass-123"
        os.environ.pop("PSTORE_URL", None)
        cls._saved_urlopen = amazon._urlopen
        amazon.CACHE_TTL = 0
        amazon.MIN_INTERVAL = 0
        amazon._urlopen = cls._fake_urlopen
        cls._saved_autosuggest = amazon.autosuggest
        amazon.autosuggest = cls._fake_autosuggest
        cls._saved_indexnow = indexnow._post
        indexnow._post = cls._fake_indexnow_post
        import importlib
        importlib.reload(server)
        amazon.CACHE_TTL = 0
        amazon.MIN_INTERVAL = 0
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.PORT = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.cookie = cls._login()

    @staticmethod
    def _fake_autosuggest(query, limit=8):
        return list(TestContentAndAudience.FAKE_TERMS)

    @staticmethod
    def _fake_urlopen(url, timeout=8, *a, **k):
        raise OSError("stubbed offline — no network in tests")

    @staticmethod
    def _fake_indexnow_post(url, payload, timeout=20):
        return None

    @classmethod
    def tearDownClass(cls):
        amazon._urlopen = cls._saved_urlopen
        amazon.autosuggest = cls._saved_autosuggest
        indexnow._post = cls._saved_indexnow
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
        conn = http.client.HTTPConnection("127.0.0.1", cls.PORT, timeout=5)
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
    def _raw(cls, path, method="GET", body=None, cookie=None, headers=None,
             timeout=8):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", cls.PORT, timeout=timeout)
        hdrs = dict(headers or {})
        if cookie:
            hdrs["Cookie"] = cookie
        if body is not None:
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        out = (resp.status, resp.getheader("Location"), resp.getheader("Content-Type"),
               resp.read())
        conn.close()
        return out

    def setUp(self):
        security.TRACK_LIMITER.clear("trk|" + self.IPKEY)
        security.PAGEVIEW_LIMITER.clear("pv|" + self.IPKEY)
        with server._lock:
            conn = server._db()
            conn.execute("DELETE FROM clicks")
            conn.execute("DELETE FROM social_posts")
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM topics")
            conn.execute("DELETE FROM settings WHERE key LIKE 'content.%'")
            conn.commit()
            conn.close()

    # ------------------------------------------------------------- engine

    def test_engine_builds_pages_and_queues_kits_then_stops(self):
        server._set_setting("content.only", "keto snacks")
        try:
            summary = server._content_run(limit=6)
            self.assertTrue(summary["on"])
            self.assertTrue(summary["pages_built"] > 0)
            self.assertEqual(summary["kits_queued"], 5)  # kit cap
            with server._lock:
                conn = server._db()
                n = conn.execute(
                    "SELECT COUNT(*) c FROM social_posts WHERE status='scheduled'").fetchone()["c"]
                scheduled = conn.execute(
                    "SELECT COUNT(*) c FROM social_posts WHERE status='scheduled' "
                    "AND scheduled_at IS NOT NULL AND scheduled_at != ''").fetchone()["c"]
                conn.close()
            self.assertEqual(n, 5)
            self.assertEqual(scheduled, 5)
            again = server._content_run(limit=6)
            self.assertEqual(again["pages_built"], 0)  # no new topics left
            self.assertEqual(again["kits_queued"], 0)  # dedupe on scheduled kits
        finally:
            server._set_setting("content.only", "")

    def test_engine_respects_disabled(self):
        server._set_setting("content.enabled", "0")
        try:
            summary = server._content_run(limit=6)
            self.assertFalse(summary["on"])
            self.assertEqual(summary["pages_built"], 0)
            self.assertEqual(summary["kits_queued"], 0)
        finally:
            server._set_setting("content.enabled", "")

    def test_engine_respects_only_filter(self):
        server._set_setting("content.only", "nonexistent-niche-xyz")
        try:
            summary = server._content_run(limit=6)
            self.assertEqual(summary["niches"], 0)
            self.assertEqual(summary["pages_built"], 0)
        finally:
            server._set_setting("content.only", "")

    def test_content_api_requires_admin(self):
        st, _, _, data = self._raw("/api/content", method="POST",
                                   body=b'{"action":"status"}')
        self.assertEqual(st, 401)
        self.assertIn(b"unauthorized", data)

    def test_content_api_save_and_status(self):
        body = json.dumps({"action": "save", "pages_day": "3", "kits_day": "2",
                           "enabled": "1"}).encode("utf-8")
        st, _, _, data = self._raw("/api/content", method="POST", body=body,
                                   cookie=self.cookie)
        self.assertEqual(st, 200)
        cfg = json.loads(data)
        self.assertEqual(cfg["pages_day"], 3)
        self.assertEqual(cfg["kits_day"], 2)
        self.assertTrue(cfg["enabled"])
        st, _, _, data = self._raw("/api/content", method="POST",
                                   body=b'{"action":"run"}', cookie=self.cookie)
        self.assertEqual(st, 200)
        run = json.loads(data)
        self.assertTrue(run["on"])
        st, _, _, data = self._raw("/api/content", method="POST",
                                   body=b'{"action":"status"}', cookie=self.cookie)
        self.assertEqual(st, 200)
        status = json.loads(data)
        self.assertEqual(status["config"]["pages_day"], 3)
        self.assertIn("last_run", status)
        self.assertIn("candidates", status)

    # ------------------------------------------------------------- beacon geo

    def test_beacon_captures_country_and_sem_who_block(self):
        headers = {"CF-IPCountry": "DE"}
        body = json.dumps({"slug": "keto-snacks", "name": "view",
                           "source": "organic"}).encode("utf-8")
        st, _, _, _ = self._raw("/api/pageview", method="POST", body=body,
                                headers=headers)
        self.assertEqual(st, 200)
        st, _, _, _ = self._raw("/api/track", method="POST",
                                body=json.dumps({"slug": "keto-snacks",
                                                 "asin": "B00TEST",
                                                 "source": "page"}).encode("utf-8"),
                                headers=headers)
        self.assertEqual(st, 200)
        regions = audience.regions(slug="keto-snacks")
        self.assertTrue(regions)
        self.assertEqual(regions[0]["code"], "DE")
        self.assertEqual(regions[0]["views"], 1)
        self.assertEqual(regions[0]["clicks"], 1)
        st, _, _, data = self._raw("/api/sem?keyword=" + urllib.parse.quote("keto snacks"),
                                   cookie=self.cookie)
        self.assertEqual(st, 200)
        who = json.loads(data).get("who") or {}
        self.assertEqual(who.get("keyword"), "keto snacks")
        self.assertEqual(who.get("intent"), "in research")
        nr = who.get("niche_regions") or []
        self.assertTrue(nr)
        self.assertEqual(nr[0]["code"], "DE")

    def test_audience_intent_labels(self):
        self.assertEqual(audience.intent_summary("best keto snacks"), "best-pick researcher")
        self.assertEqual(audience.intent_summary("keto snacks vs pencils"), "comparison shopper")
        self.assertEqual(audience.intent_summary("cheap keto snacks under 5 dollars"), "budget-first buyer")
        self.assertEqual(audience.intent_summary("buy keto snacks"), "ready to buy")
        self.assertEqual(audience.intent_summary("keto snacks"), "in research")

    def test_audience_profile_reads_demography_settings(self):
        server._set_setting("demo.region", "United States")
        server._set_setting("demo.interest", "fitness")
        server._set_setting("demo.behavior", "gift")
        try:
            p = audience.profile()
            self.assertEqual(p["region"], "United States")
            self.assertEqual(p["interest"], "fitness")
            self.assertEqual(p["behavior"], "gift")
        finally:
            server._set_setting("demo.region", "")
            server._set_setting("demo.interest", "")
            server._set_setting("demo.behavior", "")

    def test_grow_page_shows_engine_and_audience_cards(self):
        st, _, ctype, data = self._raw("/admin/opportunities", cookie=self.cookie)
        self.assertEqual(st, 200)
        html = data.decode("utf-8", "replace")
        self.assertIn("Daily content engine", html)
        self.assertIn("data-content=", html)
        self.assertIn("Audience signal", html)
        self.assertIn("Persona", html)

    def test_sem_page_shows_who_block(self):
        st, _, _, data = self._raw("/admin/sem?keyword=" + urllib.parse.quote("keto snacks"),
                                   cookie=self.cookie)
        self.assertEqual(st, 200)
        html = data.decode("utf-8", "replace")
        self.assertIn("Who is searching", html)
        self.assertIn("Target persona", html)


if __name__ == "__main__":
    unittest.main()