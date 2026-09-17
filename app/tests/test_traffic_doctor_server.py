# -*- coding: utf-8 -*-
"""Offline server tests for the traffic doctor wiring and the Telegram join
button gate: the /api/seoengines 'doctor' action returns a fix-first report
(network is stubbed via doctor._default_fetch), the Engines hub renders the
doctor section, and the public Telegram button is confined to non-commercial
pages so it can never leak a money-page buyer off-site."""

import http.client
import json
import os
import shutil
import sys
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import doctor
import indexnow
import seo
import server


class TestTrafficDoctorServer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.db = "/tmp/pstore_test_doctor_%s.db" % uuid.uuid4().hex[:8]
        shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "pstore.db"), cls.db)
        os.environ["PSTORE_DB"] = cls.db
        os.environ["PSTORE_ADMIN_EMAIL"] = "owner@test.example"
        os.environ["PSTORE_ADMIN_PASSWORD"] = "test-pass-123"
        os.environ.pop("PSTORE_URL", None)
        cls._saved_fetch = doctor._default_fetch
        doctor._default_fetch = cls._fake_fetch
        import importlib
        importlib.reload(server)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.PORT = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.cookie = cls._login()

    @classmethod
    def tearDownClass(cls):
        doctor._default_fetch = cls._saved_fetch
        cls.httpd.shutdown()
        cls.thread.join(timeout=2)
        cls.httpd.server_close()
        if os.path.exists(cls.db):
            os.unlink(cls.db)

    @classmethod
    def _fake_fetch(cls, url, timeout=15):
        """Path-keyed fake origin: robots, sitemap, the IndexNow key file and
        every page are healthy — so the endpoint's own logic is what we test."""
        if url.endswith("/robots.txt"):
            return (200, {}, b"User-agent: *\nDisallow: /admin\nSitemap: x\n")
        if url.endswith("/sitemap.xml"):
            return (200, {}, (
                '<urlset><loc>%s/a</loc><loc>%s/b</loc></urlset>'
                % (seo.BASE_URL, seo.BASE_URL)).encode())
        if url.endswith(".txt"):
            return (200, {}, indexnow.key().encode())
        body = ('<!DOCTYPE html><html><head><title>T</title>'
                '<link rel="canonical" href="%s">'
                '<meta name="description" content="d"></head>'
                '<body>%s</body></html>' % (url, "x" * 2000)).encode()
        return (200, {"content-type": "text/html"}, body)

    @classmethod
    def _login(cls):
        conn = http.client.HTTPConnection("127.0.0.1", cls.PORT, timeout=5)
        conn.request("POST", "/admin/login",
                     body=b"email=owner@test.example&password=test-pass-123",
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        resp = conn.getresponse()
        cookie = resp.getheader("Set-Cookie")
        conn.close()
        return cookie.split(";")[0]

    def _raw(self, method, path, body=None, cookie=None, ctype=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.PORT, timeout=60)
        h = {}
        if cookie:
            h["Cookie"] = cookie
        if ctype:
            h["Content-Type"] = ctype
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        status = resp.status
        conn.close()
        return status, data

    # --- Telegram gate ------------------------------------------------------
    def test_telegram_button_allowed_only_non_commercial(self):
        allowed = ["/about", "/contact", "/privacy", "/terms",
                   "/disclosure", "/stories", "/blog", "/blog/some-post"]
        blocked = ["/", "/n/keto-snacks", "/lp/beauty", "/stories/keto-bread",
                   "/vs/a-b", "/admin/seoengines", "/api/social", "/go/B0123"]
        for p in allowed:
            self.assertTrue(server._telegram_button_allowed(p), p)
        for p in blocked:
            self.assertFalse(server._telegram_button_allowed(p), p)

    def test_inject_telegram_button_never_on_money_page(self):
        server._set_setting("telegram.on_page", "1")
        server._set_setting("telegram.botname", "pstorebot")
        try:
            html = b"<!DOCTYPE html><html><body>hi</body></html>"
            money = server._inject_telegram_button(html, "/n/keto-snacks")
            self.assertNotIn(server._TG_JOIN_MARK, money)
            home = server._inject_telegram_button(html, "/about")
            self.assertIn(server._TG_JOIN_MARK, home)
        finally:
            server._set_setting("telegram.on_page", "0")

    # --- Doctor endpoint ----------------------------------------------------
    def test_doctor_action_returns_fix_first_report(self):
        st, body = self._raw("POST", "/api/seoengines",
                             body=json.dumps({"action": "doctor", "sample": 1}).encode(),
                             cookie=self.cookie, ctype="application/json")
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertTrue(d["ok"], d)
        self.assertIn(d["verdict"], ("ok", "warn", "fail"))
        self.assertIn("checks", d)
        ids = {c["id"] for c in d["checks"]}
        for cid in ("robots", "sitemap", "indexnow", "gsc", "social-channel"):
            self.assertIn(cid, ids, d)
        # Google is never connected in this env, so its blocker and the
        # IndexNow-only truth must surface.
        gsc = [c for c in d["checks"] if c["id"] == "gsc"][0]
        self.assertEqual(gsc["status"], "fail")
        self.assertIn("IndexNow does NOT reach Google", gsc["fix"])
        # fix-first ordering: failures before passes.
        self.assertEqual(d["checks"][0]["status"], "fail")

    def test_doctor_requires_auth(self):
        st, _body = self._raw("POST", "/api/seoengines",
                              body=b'{"action":"doctor"}', ctype="application/json")
        self.assertIn(st, (401, 403))

    def test_engines_page_renders_doctor_section(self):
        st, body = self._raw("GET", "/admin/seoengines", cookie=self.cookie)
        self.assertEqual(st, 200)
        self.assertIn(b"End-to-end doctor", body)
        self.assertIn(b"runDoctor", body)


if __name__ == "__main__":
    unittest.main()
