# -*- coding: utf-8 -*-
"""Offline tests for the search-engine console integrations:
webmasters.py clients (Google GSC, Bing, Yandex) with a fake transport, the
server /admin/seoengines hub + /api/seoengines CRUD, referrer-attributed
on-site traffic, and the signed OAuth consent callbacks."""

import http.client
import importlib
import json
import os
import shutil
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer

sys_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if sys_path not in os.sys.path:
    os.sys.path.insert(0, sys_path)

import security
import seo
import webmasters

GSC_TOK = {"access_token": "gsc-at", "refresh_token": "gsc-rt",
           "expires_in": 3600, "expires_at": 2 ** 40}
YANDEX_TOK = {"access_token": "yandex-at", "expires_in": 31536000,
              "expires_at": 2 ** 40}


class FakeWm:
    """In-process fake for webmasters._req + its settings store.

    Routes by URL fragment and logs every call so tests can assert on the
    exact transport that a given client produces."""

    def __init__(self):
        self.store = {}
        self.calls = []
        self.wm_get = lambda k, d="": self.store.get(k, d)
        self.wm_set = lambda k, v: self.store.__setitem__(k, v)

    def hook(self):
        self._saved = (webmasters._req, webmasters._STORE_GET,
                       webmasters._STORE_SET)
        webmasters._set_transport(self._req)
        webmasters._STORE_GET = self.wm_get
        webmasters._STORE_SET = self.wm_set

    def unhook(self):
        webmasters._req, webmasters._STORE_GET, webmasters._STORE_SET = \
            self._saved

    def _req(self, method, url, headers=None, body=None, timeout=25):
        self.calls.append((method, url))
        if "oauth2.googleapis.com/token" in url:
            return 200, dict(GSC_TOK)
        if "oauth.yandex.ru/token" in url:
            return 200, dict(YANDEX_TOK)
        if "searchAnalytics/query" in url:
            return 200, {"rows": [
                {"keys": ["/n/keto", "/"], "clicks": 5, "impressions": 100,
                 "ctr": 0.05, "position": 3.5}]}
        if "sitemaps/" in url and method == "PUT":
            return 204, {}
        if "/sitemaps" in url:
            return 200, {"sitemap": [
                {"path": "https://x/sitemap.xml",
                 "contents": [{"submitted": "2020-01-01",
                               "lastDownloaded": "2020-01-02"}],
                 "isPending": False}]}
        if "/webmasters/v3/sites" in url and method == "PUT":
            return 204, {}
        if "/webmasters/v3/sites" in url and "urlInspection" not in url:
            return 200, {"siteEntry": [
                {"siteUrl": "https://pstore-gxbv.onrender.com/",
                 "permissionLevel": "full"},
                {"siteUrl": "sc-domain:example.com"}]}
        if "urlInspection/index" in url:
            return 200, {"inspectionResult": {
                "inspectionResultLink": "x",
                "indexStatusResult": {
                    "coverageState": "Indexed, not submitted",
                    "indexingState": "INDEXING_ALLOWED",
                    "lastCrawlTime": "2026-01-01T00:00:00Z",
                    "verdict": "pass",
                    "robotsTxtState": "ALLOWED"}}}
        if "sitemaps/" in url and method == "PUT":
            return 204, {}
        if "/sitemaps" in url:
            return 200, {"sitemap": [
                {"path": "https://x/sitemap.xml",
                 "contents": [{"submitted": "2020-01-01",
                               "lastDownloaded": "2020-01-02"}],
                 "isPending": False}]}
        if "GetKeywordStats" in url:
            return 200, [{"Query": "keto", "Clicks": 3, "Impressions": 90,
                          "Position": 2.5, "MaxPosition": 1}]
        if "SubmitSitemap" in url:
            return 200, {"d": "ok"}
        if url.endswith("/user/"):
            return 200, {"user_id": "uid-1"}
        if url.endswith("/hosts/") and method == "POST":
            return 200, {"host_name": webmasters.host_of(),
                          "verified": False}
        if "/hosts/" in url and "search-queries" not in url:
            return 200, {"hosts": [{"host_id": "h1",
                                    "host_name": "pstore-gxbv.onrender.com"}]}
        if "/indexing/" in url:
            return 201, {}
        if "search-queries/summary" in url:
            return 200, {"totals": {"clicks": 11, "shows": 400, "position": 4.2}}
        return 404, {}


class TestWebmastersClients(unittest.TestCase):
    """Unit tests for the engine clients with a fake transport."""

    @classmethod
    def setUpClass(cls):
        os.environ["PSTORE_GSC_CLIENT_ID"] = "cid-test"
        os.environ["PSTORE_GSC_CLIENT_SECRET"] = "cs-test"
        os.environ["PSTORE_BING_API_KEY"] = "bing-key-test"
        os.environ["PSTORE_YANDEX_CLIENT_ID"] = "yid-test"
        os.environ["PSTORE_YANDEX_CLIENT_SECRET"] = "ys-test"
        os.environ["PSTORE_URL"] = "https://pstore-gxbv.onrender.com"
        importlib.reload(seo)
        importlib.reload(webmasters)
        cls.wm = FakeWm()
        cls.wm.hook()

    @classmethod
    def tearDownClass(cls):
        cls.wm.unhook()
        for k in ("PSTORE_GSC_CLIENT_ID", "PSTORE_GSC_CLIENT_SECRET",
                  "PSTORE_BING_API_KEY", "PSTORE_YANDEX_CLIENT_ID",
                  "PSTORE_YANDEX_CLIENT_SECRET"):
            os.environ.pop(k, None)

    def test_gsc_auth_url(self):
        url = webmasters.gsc_auth_url("st-tok")
        self.assertIn("accounts.google.com/o/oauth2/v2/auth", url)
        self.assertIn("client_id=cid-test", url)
        self.assertIn("response_type=code", url)
        self.assertIn("scope=", url)
        # write scope: Search Analytics reads + sitemap submit + URL inspection
        self.assertIn("webmasters", url)
        self.assertNotIn("readonly", url)
        self.assertIn("redirect_uri=", url)
        self.assertIn("%2Fadmin%2Foauth%2Fseoengines%2Fcb%2Fgsc", url)
        self.assertIn("state=st-tok", url)
        self.assertNotIn(" ", url)

    def test_gsc_auth_url_needs_client(self):
        saved = (webmasters.GSC_CLIENT_ID, webmasters.GSC_CLIENT_SECRET)
        try:
            webmasters.GSC_CLIENT_ID = ""
            webmasters.GSC_CLIENT_SECRET = ""
            self.assertEqual(webmasters.gsc_auth_url("s"), "")
        finally:
            webmasters.GSC_CLIENT_ID, webmasters.GSC_CLIENT_SECRET = saved

    def test_gsc_exchange_and_refresh(self):
        ok, msg = webmasters.gsc_exchange("code-1")
        self.assertTrue(ok)
        tok = json.loads(self.wm.store["seoeng.gsc.token"])
        self.assertEqual(tok["access_token"], "gsc-at")
        self.assertEqual(tok["refresh_token"], "gsc-rt")
        # not-expired tokens are used as-is
        self.assertEqual(webmasters._gsc_bearer(), "gsc-at")
        # forced refresh still returns a token (fake returns GSC_TOK)
        self.assertEqual(webmasters.gsc_refresh(), "gsc-at")

    def test_gsc_performance(self):
        self.wm.store["seoeng.gsc.token"] = json.dumps(GSC_TOK)
        ok, data = webmasters.gsc_performance(28)
        self.assertTrue(ok)
        self.assertEqual(data["rows"][0]["page"], "/n/keto")
        self.assertEqual(data["rows"][0]["clicks"], 5)
        self.assertEqual(data["totals"]["clicks"], 5)
        self.assertEqual(data["totals"]["impressions"], 100)
        self.assertAlmostEqual(data["totals"]["position"], 3.5)

    def test_gsc_performance_not_connected(self):
        self.wm.store.pop("seoeng.gsc.token", None)
        ok, data = webmasters.gsc_performance(7)
        self.assertFalse(ok)

    def test_gsc_submit_sitemap(self):
        self.wm.store["seoeng.gsc.token"] = json.dumps(GSC_TOK)
        self.wm.calls.clear()
        ok, msg = webmasters.gsc_submit_sitemap()
        self.assertTrue(ok)
        put = [c for c in self.wm.calls if c[0] == "PUT" and "sitemaps/" in c[1]]
        self.assertEqual(len(put), 1)
        # feed path is host-less (just "sitemap.xml"); host only in site id
        self.assertTrue(put[0][1].endswith("/sitemaps/sitemap.xml"))
        self.assertIn("https:%2F%2Fpstore-gxbv.onrender.com%2F", put[0][1])

    def test_gsc_crawl_budget_and_inspect(self):
        # inspector needs a connected bearer token
        self.wm.store["seoeng.gsc.token"] = json.dumps(GSC_TOK)
        self.wm.store.pop("seoeng.gsc.inspect", None)
        self.wm.store.pop("seoeng.gsc.sitemap", None)
        self.wm.calls.clear()
        urls = ["https://pstore-gxbv.onrender.com/n/keto-snacks/low-carb",
                "https://pstore-gxbv.onrender.com/n/keto-snacks/healthy"]
        out = webmasters.gsc_crawl(urls)
        self.assertTrue(out["ok"])
        self.assertEqual(out["inspected"], 2)
        self.assertEqual(out["total"], 2)
        inspect = [c for c in self.wm.calls if "urlInspection/index" in c[1]]
        self.assertEqual(len(inspect), 2)
        # budget consumed by the two inspections
        self.assertEqual(webmasters._inspect_budget(),
                         webmasters.GSC_INSPECT_DAY_LIMIT -
                         webmasters.GSC_INSPECT_DAY_MARGIN - 2)
        # re-run same day: sitemap re-submitted + inspections counted again
        calls_before = len(self.wm.calls)
        webmasters.gsc_crawl(urls)
        self.assertEqual(len(self.wm.calls), calls_before + 3)

    def test_gsc_crawl_disabled(self):
        self.wm.store["seoeng.gsc.token"] = json.dumps(GSC_TOK)
        self.wm.store["seoeng.gsc.enabled"] = "0"
        self.wm.store.pop("seoeng.gsc.inspect", None)
        out = webmasters.gsc_crawl(["https://pstore-gxbv.onrender.com/n/x"])
        self.assertEqual(out["inspected"], 0)
        self.assertTrue(out["submitted"])  # sitemap submit stays on
        self.wm.store["seoeng.gsc.enabled"] = "1"

    def test_gsc_inspect_skips_when_unconnected(self):
        tok = self.wm.store.get("seoeng.gsc.token")
        self.wm.store.pop("seoeng.gsc.token", None)
        self.wm.store.pop("seoeng.gsc.inspect", None)
        try:
            self.assertIsNone(webmasters.inspect_new("https://x/y"))
        finally:
            if tok:
                self.wm.store["seoeng.gsc.token"] = tok

    def test_gsc_sitemap_status(self):
        self.wm.store["seoeng.gsc.token"] = json.dumps(GSC_TOK)
        ok, items = webmasters.gsc_sitemap_status()
        self.assertTrue(ok)
        self.assertEqual(items[0]["last_submitted"], "2020-01-01")
        self.assertFalse(items[0]["is_pending"])

    def test_gsc_user_sites(self):
        self.wm.store["seoeng.gsc.token"] = json.dumps(GSC_TOK)
        self.wm.calls.clear()
        ok, data = webmasters.gsc_user_sites()
        self.assertTrue(ok)
        self.assertTrue(data["registered"])
        self.assertTrue(any("pstore-gxbv.onrender.com" in s for s in data["sites"]))
        self.assertEqual(len([c for c in self.wm.calls if "/webmasters/v3/sites" in c[1]]), 1)

    def test_gsc_user_sites_not_connected(self):
        self.wm.store.pop("seoeng.gsc.token", None)
        ok, data = webmasters.gsc_user_sites()
        self.assertFalse(ok)
        self.assertEqual(data["sites"], [])
        self.assertFalse(data["registered"])

    def test_gsc_403_api_not_enabled_shows_fix_hint(self):
        self.wm.store["seoeng.gsc.token"] = json.dumps(GSC_TOK)
        orig = webmasters._req
        def _forbid(method, url, headers=None, body=None, timeout=25):
            if "/webmasters/v3/sites" in url and method == "GET":
                return 403, {"error": {"code": 403, "status": "PERMISSION_DENIED",
                                       "message": "Search Console API has not been "
                                                  "used in project 123 before or it "
                                                  "is disabled."}}
            return orig(method, url, headers=headers, body=body, timeout=timeout)
        webmasters._set_transport(_forbid)
        try:
            ok, data = webmasters.gsc_user_sites()
            self.assertFalse(ok)
            self.assertIn("fix", data["error"])
            self.assertIn("Search Console API", data["error"])
        finally:
            webmasters._set_transport(orig)

    def test_gsc_hint_quota_and_property(self):
        msg = webmasters._gsc_hint(
            403, {"error": {"status": "RESOURCE_EXHAUSTED",
                            "message": "Quota exceeded for quota metric"}})
        self.assertIn("quota", msg.lower())
        msg = webmasters._gsc_hint(
            403, {"error": {"status": "PERMISSION_DENIED",
                            "message": "The caller does not have permission"}})
        self.assertIn("fix", msg)

    def test_gsc_add_site(self):
        self.wm.store["seoeng.gsc.token"] = json.dumps(GSC_TOK)
        self.wm.calls.clear()
        ok, msg = webmasters.gsc_add_site()
        self.assertTrue(ok)
        self.assertIn("registered", msg)
        puts = [c for c in self.wm.calls
                if c[0] == "PUT" and "/webmasters/v3/sites" in c[1]
                and "/sitemaps" not in c[1]]
        self.assertEqual(len(puts), 1)
        # sitemap also submitted during add_site
        sitemap_puts = [c for c in self.wm.calls
                        if c[0] == "PUT" and "sitemaps/" in c[1]]
        self.assertEqual(len(sitemap_puts), 1)

    def test_gsc_submit_url(self):
        self.wm.store["seoeng.gsc.token"] = json.dumps(GSC_TOK)
        self.wm.store.pop("seoeng.gsc.inspect", None)
        self.wm.calls.clear()
        ok, d = webmasters.gsc_submit_url("/n/keto")
        self.assertTrue(ok)
        self.assertIn("url", d)
        self.assertTrue(d["indexed"])
        self.assertEqual(d["verdict"], "pass")
        self.assertIn("robots_txt_state", d)
        # budget consumed
        self.assertLess(webmasters._inspect_budget(),
                        webmasters.GSC_INSPECT_DAY_LIMIT -
                        webmasters.GSC_INSPECT_DAY_MARGIN)

    def test_gsc_submit_url_budget_spent(self):
        self.wm.store["seoeng.gsc.token"] = json.dumps(GSC_TOK)
        # spend entire budget
        spent = webmasters.GSC_INSPECT_DAY_LIMIT - webmasters.GSC_INSPECT_DAY_MARGIN
        import time as _time
        today = _time.strftime("%Y-%m-%d", _time.gmtime())
        self.wm.store["seoeng.gsc.inspect"] = json.dumps(
            {"date": today, "used": spent})
        ok, d = webmasters.gsc_submit_url("/n/keto")
        self.assertFalse(ok)
        self.assertIn("budget", d["error"])

    def test_yandex_user_hosts(self):
        self.wm.store["seoeng.yandex.token"] = json.dumps(YANDEX_TOK)
        ok, data = webmasters.yandex_user_hosts()
        self.assertTrue(ok)
        self.assertEqual(data["user"], "uid-1")
        self.assertTrue(data["registered"])
        self.assertIn("pstore-gxbv.onrender.com", data["hosts"])

    def test_yandex_add_host(self):
        self.wm.store["seoeng.yandex.token"] = json.dumps(YANDEX_TOK)
        ok, d = webmasters.yandex_add_host()
        self.assertTrue(ok)
        self.assertIn("host", d)
        self.assertFalse(d["verified"])
        self.assertIn("verify", d["message"].lower())

    def test_yandex_submit_url(self):
        self.wm.store["seoeng.yandex.token"] = json.dumps(YANDEX_TOK)
        ok, d = webmasters.yandex_submit_url("/n/keto")
        self.assertTrue(ok)
        self.assertIn("url", d)
        self.assertIn("keto", d["url"])

    def test_yandex_submit_url_no_host(self):
        self.wm.store["seoeng.yandex.token"] = json.dumps(YANDEX_TOK)
        orig = webmasters._req
        def _no_host_req(method, url, headers=None, body=None, timeout=25):
            if url.endswith("/hosts/") and method != "POST":
                return 200, {"hosts": []}
            return orig(method, url, headers=headers, body=body, timeout=timeout)
        webmasters._set_transport(_no_host_req)
        try:
            ok, d = webmasters.yandex_submit_url()
            self.assertFalse(ok)
            self.assertIn("error", d)
        finally:
            webmasters._set_transport(orig)

    def test_bing_stats(self):
        ok, data = webmasters.bing_stats("bing-key-test",
                                         "https://pstore-gxbv.onrender.com", 28)
        self.assertTrue(ok)
        self.assertEqual(data["rows"][0]["page"], "keto")
        self.assertEqual(data["totals"]["clicks"], 3)
        self.assertEqual(data["totals"]["impressions"], 90)

    def test_bing_submit_sitemap(self):
        s, d = webmasters.bing_submit_sitemap(
            "bing-key-test", "https://pstore-gxbv.onrender.com")
        self.assertEqual(s, 200)
        calls = [c for c in self.wm.calls if "SubmitSitemap" in c[1]]
        self.assertEqual(len(calls), 1)

    def test_yandex_auth_url(self):
        url = webmasters.yandex_auth_url("y-st")
        self.assertIn("oauth.yandex.ru/authorize", url)
        self.assertIn("client_id=yid-test", url)
        self.assertIn("webmaster%3Ahost%3Aall", url)
        self.assertIn("cb%2Fyandex", url)

    def test_yandex_exchange(self):
        ok, msg = webmasters.yandex_exchange("y-code")
        self.assertTrue(ok)
        self.assertEqual(json.loads(self.wm.store["seoeng.yandex.token"])[
            "access_token"], "yandex-at")

    def test_yandex_summary(self):
        self.wm.store["seoeng.yandex.token"] = json.dumps(YANDEX_TOK)
        ok, data = webmasters.yandex_summary(28)
        self.assertTrue(ok)
        self.assertEqual(data["totals"]["clicks"], 11)
        self.assertEqual(data["totals"]["impressions"], 400)
        self.assertAlmostEqual(data["totals"]["position"], 4.2)

    def test_sync_engine_persists_snapshot(self):
        self.wm.store["seoeng.gsc.token"] = json.dumps(GSC_TOK)
        ok, data = webmasters.sync_engine("gsc", 28)
        self.assertTrue(ok)
        self.assertIn("totals", data)
        snap = webmasters.last_sync("gsc")
        self.assertTrue(snap)
        self.assertIn("at", snap)
        self.assertIn("totals", snap)
        self.assertEqual(snap["totals"]["clicks"], 5)

    def test_sync_engine_unknown(self):
        ok, data = webmasters.sync_engine("brave")
        self.assertFalse(ok)

    def test_engines_status_states(self):
        self.wm.store["seoeng.gsc.token"] = json.dumps(GSC_TOK)
        self.wm.store["seoeng.yandex.token"] = json.dumps(YANDEX_TOK)
        st = {r["engine"]: r for r in webmasters.engines_status()}
        self.assertEqual(st["gsc"]["state"], "ready")
        self.assertEqual(st["gsc"]["site"], "https://pstore-gxbv.onrender.com/")
        self.assertEqual(st["bing"]["state"], "ready")
        self.assertEqual(st["yandex"]["state"], "ready")

    def test_bing_state_key_fallback(self):
        saved = (webmasters.BING_API_KEY, self.wm.store.get("seoeng.bing.apikey"))
        try:
            webmasters.BING_API_KEY = ""
            self.wm.store["seoeng.bing.apikey"] = ""
            st = {r["engine"]: r for r in webmasters.engines_status()}
            self.assertEqual(st["bing"]["state"], "needs-key")
            self.wm.store["seoeng.bing.apikey"] = "stored-key"
            st = {r["engine"]: r for r in webmasters.engines_status()}
            self.assertEqual(st["bing"]["state"], "ready")
            self.assertTrue(st["bing"]["client"])
        finally:
            webmasters.BING_API_KEY, key = saved
            if key:
                self.wm.store["seoeng.bing.apikey"] = key

    def test_engines_status_no_client(self):
        saved = (webmasters.GSC_CLIENT_ID, webmasters.GSC_CLIENT_SECRET,
                 webmasters.YANDEX_CLIENT_ID, webmasters.YANDEX_CLIENT_SECRET,
                 webmasters.BING_API_KEY)
        try:
            webmasters.GSC_CLIENT_ID = webmasters.GSC_CLIENT_SECRET = ""
            webmasters.YANDEX_CLIENT_ID = webmasters.YANDEX_CLIENT_SECRET = ""
            webmasters.BING_API_KEY = ""
            self.wm.store["seoeng.bing.apikey"] = ""
            self.wm.store.pop("seoeng.gsc.token", None)
            self.wm.store.pop("seoeng.yandex.token", None)
            st = {r["engine"]: r for r in webmasters.engines_status()}
            self.assertEqual(st["gsc"]["state"], "needs-client")
            self.assertEqual(st["bing"]["state"], "needs-key")
            self.assertEqual(st["yandex"]["state"], "needs-client")
        finally:
            (webmasters.GSC_CLIENT_ID, webmasters.GSC_CLIENT_SECRET,
             webmasters.YANDEX_CLIENT_ID, webmasters.YANDEX_CLIENT_SECRET,
             webmasters.BING_API_KEY) = saved


class TestSeoengineServer(unittest.TestCase):
    """End-to-end through the HTTP handler with a fake webmasters transport."""

    IPKEY = "127.0.0.1|127.0.0.1"

    @classmethod
    def setUpClass(cls):
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.db = "/tmp/pstore_test_seoengines_%s.db" % uuid.uuid4().hex[:8]
        shutil.copy(os.path.join(base, "pstore.db"), cls.db)
        os.environ["PSTORE_DB"] = cls.db
        os.environ["PSTORE_ADMIN_EMAIL"] = "owner@test.example"
        os.environ["PSTORE_ADMIN_PASSWORD"] = "test-pass-123"
        os.environ["PSTORE_GOOGLE_SITE_VERIFICATION"] = "gsc-owner-meta"
        os.environ["PSTORE_BING_SITE_VERIFICATION"] = "bing-owner-meta"
        os.environ["PSTORE_YANDEX_VERIFICATION"] = "yandex-owner-meta"
        os.environ["PSTORE_URL"] = "https://pstore-gxbv.onrender.com"
        cls.wm_saved = (webmasters._req, webmasters._STORE_GET,
                        webmasters._STORE_SET)
        cls.wm = FakeWm()
        importlib.reload(webmasters)
        importlib.reload(seo)
        import server
        importlib.reload(server)
        cls.server = server
        webmasters._set_transport(cls.wm._req)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cls.server.Handler)
        cls.PORT = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.cookie = cls._login()

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

    @classmethod
    def tearDownClass(cls):
        security.TRACK_LIMITER.clear("trk|" + cls.IPKEY)
        security.PAGEVIEW_LIMITER.clear("pv|" + cls.IPKEY)
        cls.httpd.shutdown()
        cls.thread.join(timeout=2)
        cls.httpd.server_close()
        webmasters._req, webmasters._STORE_GET, webmasters._STORE_SET = \
            cls.wm_saved
        for k in ("PSTORE_GOOGLE_SITE_VERIFICATION", "PSTORE_BING_SITE_VERIFICATION",
                  "PSTORE_YANDEX_VERIFICATION"):
            os.environ.pop(k, None)
        if os.path.exists(cls.db):
            os.unlink(cls.db)

    def _raw(self, method, path, body=None, cookie=None, ctype=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.PORT, timeout=10)
        h = {}
        if cookie:
            h["Cookie"] = cookie
        if ctype:
            h["Content-Type"] = ctype
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        status, loc = resp.status, resp.getheader("Location")
        conn.close()
        return status, loc, data

    def test_admin_login(self):
        self.assertIn("pstore_admin", self.cookie)

    def test_seoengines_hub_requires_auth(self):
        st, loc, _ = self._raw("GET", "/admin/seoengines")
        self.assertEqual(st, 302)
        self.assertIn("admin/login", loc)

    def test_seoengines_hub_page(self):
        st, _, body = self._raw("GET", "/admin/seoengines", cookie=self.cookie)
        self.assertEqual(st, 200)
        html = body.decode("utf-8", "replace")
        self.assertIn("Consoles", html)
        self.assertIn("Connect console", html)
        self.assertIn("Fetch stats", html)
        self.assertIn("Submit sitemap", html)
        self.assertIn("Setup guide", html)
        self.assertIn("Test connection", html)
        self.assertIn("Add this site", html)
        self.assertIn("Google Search Console", html)
        self.assertIn("Bing Webmaster", html)
        self.assertIn("Yandex Webmaster", html)
        self.assertIn("Traffic by engine", html)
        self.assertIn("/api/seoengines", html)

    def test_seoengines_api_get(self):
        st, _, body = self._raw("GET", "/api/seoengines", cookie=self.cookie)
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertIn("engines", d)
        self.assertIn("traffic", d)
        self.assertIn("host", d)
        self.assertIn("sitemap", d)
        self.assertEqual(len(d["engines"]), 3)
        for e in d["engines"]:
            self.assertIn("state", e)
            self.assertIn("engine", e)
        self.assertIn("totals", d["traffic"])
        self.assertIn("google", d["traffic"]["engines"])

    def test_seoengines_api_requires_auth(self):
        st, _, _ = self._raw("GET", "/api/seoengines")
        self.assertEqual(st, 401)

    def test_bingkey_post_and_state(self):
        st, _, body = self._raw(
            "POST", "/api/seoengines", cookie=self.cookie,
            body=json.dumps({"action": "bingkey", "key": "k-abcd"}),
            ctype="application/json")
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(self.server._get_setting("seoeng.bing.apikey"), "k-abcd")
        st, _, body = self._raw("GET", "/api/seoengines", cookie=self.cookie)
        d = json.loads(body)
        bing = [e for e in d["engines"] if e["engine"] == "bing"][0]
        self.assertEqual(bing["state"], "ready")

    def test_verify_post_sets_runtime_tokens(self):
        st, _, body = self._raw(
            "POST", "/api/seoengines", cookie=self.cookie,
            body=json.dumps({"action": "verify", "engine": "bing",
                             "token": "rt-token-xyz"}),
            ctype="application/json")
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(seo.bing_site_verification(), "rt-token-xyz")
        seo.set_bing_site_verification("")
        st, _, body = self._raw(
            "POST", "/api/seoengines", cookie=self.cookie,
            body=json.dumps({"action": "verify", "engine": "yandex",
                             "token": "yt-token-xyz"}),
            ctype="application/json")
        self.assertEqual(seo.yandex_site_verification(), "yt-token-xyz")
        seo.set_yandex_site_verification("")
        st, _, body = self._raw(
            "POST", "/api/seoengines", cookie=self.cookie,
            body=json.dumps({"action": "verify", "engine": "google",
                             "token": "gt-token-xyz"}),
            ctype="application/json")
        self.assertEqual(seo.google_site_verification(), "gt-token-xyz")
        seo.set_google_site_verification("")
        st, _, body = self._raw(
            "POST", "/api/seoengines", cookie=self.cookie,
            body=json.dumps({"action": "verify", "engine": "pinterest",
                             "token": "pt-token-xyz"}),
            ctype="application/json")
        self.assertEqual(st, 200)
        self.assertEqual(seo.pinterest_site_verification(), "pt-token-xyz")
        seo.set_pinterest_site_verification("")

    def test_sync_gsc_over_http(self):
        self.server._set_setting("seoeng.gsc.token", json.dumps(GSC_TOK))
        st, _, body = self._raw(
            "POST", "/api/seoengines", cookie=self.cookie,
            body=json.dumps({"action": "sync", "engine": "gsc", "days": 28}),
            ctype="application/json")
        d = json.loads(body)
        self.assertEqual(st, 200)
        self.assertTrue(d.get("ok"))
        self.assertIn("totals", d)
        self.assertIn("rows", d)
        self.assertEqual(d["rows"][0]["page"], "/n/keto")
        snap = self.server._get_setting("seoeng.gsc.last")
        self.assertTrue(snap)
        self.assertIn("at", json.loads(snap))

    def test_submit_sitemap_gsc(self):
        self.server._set_setting("seoeng.gsc.token", json.dumps(GSC_TOK))
        st, _, body = self._raw(
            "POST", "/api/seoengines", cookie=self.cookie,
            body=json.dumps({"action": "submit", "engine": "gsc"}),
            ctype="application/json")
        d = json.loads(body)
        self.assertEqual(st, 200)
        self.assertEqual(d.get("ok"), True)
        self.assertEqual(d.get("message"), "submitted")

    def test_gsc_console_actions_over_http(self):
        self.server._set_setting("seoeng.gsc.token", json.dumps(GSC_TOK))
        st, _, body = self._raw(
            "POST", "/api/seoengines", cookie=self.cookie,
            body=json.dumps({"action": "gsctest", "engine": "gsc"}),
            ctype="application/json")
        d = json.loads(body)
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        self.assertTrue(d["registered"])
        self.assertTrue(d["sites"])
        st, _, body = self._raw(
            "POST", "/api/seoengines", cookie=self.cookie,
            body=json.dumps({"action": "gscadd", "engine": "gsc"}),
            ctype="application/json")
        d = json.loads(body)
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        self.assertIn("registered", d["message"])
        st, _, body = self._raw(
            "POST", "/api/seoengines", cookie=self.cookie,
            body=json.dumps({"action": "gscurl", "engine": "gsc",
                             "page": "/n/keto"}),
            ctype="application/json")
        d = json.loads(body)
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        self.assertTrue(d["indexed"])
        self.assertIn("url", d)

    def test_yandex_console_actions_over_http(self):
        self.server._set_setting("seoeng.yandex.token", json.dumps(YANDEX_TOK))
        st, _, body = self._raw(
            "POST", "/api/seoengines", cookie=self.cookie,
            body=json.dumps({"action": "yatest", "engine": "yandex"}),
            ctype="application/json")
        d = json.loads(body)
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        self.assertTrue(d["registered"])
        st, _, body = self._raw(
            "POST", "/api/seoengines", cookie=self.cookie,
            body=json.dumps({"action": "yaadd", "engine": "yandex"}),
            ctype="application/json")
        d = json.loads(body)
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        self.assertIn("host", d)
        st, _, body = self._raw(
            "POST", "/api/seoengines", cookie=self.cookie,
            body=json.dumps({"action": "yaurl", "engine": "yandex",
                             "page": "/n/keto"}),
            ctype="application/json")
        d = json.loads(body)
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        self.assertIn("url", d)

    def test_crawl_post_and_paid_set(self):
        self.server._set_setting("seoeng.gsc.token", json.dumps(GSC_TOK))
        st, _, body = self._raw(
            "POST", "/api/seoengines", cookie=self.cookie,
            body=json.dumps({"action": "crawl",
                             "urls": ["https://pstore-gxbv.onrender.com/n/x"]}),
            ctype="application/json")
        d = json.loads(body)
        self.assertEqual(st, 200)
        self.assertEqual(d["crawl"]["inspected"], 1)
        # paid campaign tag
        st, _, body = self._raw(
            "POST", "/api/seoengines", cookie=self.cookie,
            body=json.dumps({"action": "paidset", "tag": "PAIDTAG-20"}),
            ctype="application/json")
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(self.server._get_setting("paid.tag"), "PAIDTAG-20")
        # gsc crawl enable toggle
        st, _, body = self._raw(
            "POST", "/api/seoengines", cookie=self.cookie,
            body=json.dumps({"action": "gscenable", "enabled": False}),
            ctype="application/json")
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(self.server._get_setting("seoeng.gsc.enabled"), "0")
        self.server._set_setting("paid.tag", "")
        st, _, body = self._raw("GET", "/api/seoengines", cookie=self.cookie)
        self.assertIn("gsc_crawl_enabled", json.loads(body))
        self.assertIn("paid_tag", json.loads(body))
        self.assertIn("gsc_inspect_budget", json.loads(body))

    def test_connect_without_client_gives_url(self):
        # env has a fake client id in this harness, so the hub can build URLs;
        # the callback route is exercised separately for the stale-state path.
        st, _, body = self._raw(
            "POST", "/api/seoengines", cookie=self.cookie,
            body=json.dumps({"action": "connect", "engine": "gsc"}),
            ctype="application/json")
        d = json.loads(body)
        self.assertEqual(st, 200)
        if not webmasters.GSC_CLIENT_ID:
            self.assertFalse(d.get("ok"))
        else:
            self.assertTrue(d.get("ok"))
            self.assertIn("accounts.google.com", d["url"])

    def test_oauth_cb_stale_or_missing_state(self):
        st, loc, body = self._raw(
            "GET",
            "/admin/oauth/seoengines/cb/gsc?code=abc&state=forged-state")
        self.assertEqual(st, 302)
        self.assertIn("admin/seoengines", loc)
        self.assertIn("err=", loc)

    def test_oauth_cb_surfaces_google_error(self):
        st, loc, _ = self._raw(
            "GET",
            "/admin/oauth/seoengines/cb/gsc"
            "?error=invalid_client&state=stale")
        self.assertEqual(st, 302)
        self.assertIn("admin/seoengines?err=", loc)
        self.assertIn("consent%20error", loc)

    def test_pageview_referrer_attribution(self):
        st, _, body = self._raw(
            "POST", "/api/pageview", cookie=None,
            body=json.dumps({"slug": "keto", "page": "/n/keto", "name": "view",
                             "source": "organic",
                             "referrer": "https://www.google.com/search?q=keto"}),
            ctype="application/json")
        self.assertEqual(st, 200)
        st2, _, body2 = self._raw(
            "POST", "/api/pageview", cookie=None,
            body=json.dumps({"slug": "keto", "page": "/n/keto", "name": "view",
                             "source": "organic",
                             "referrer": "https://www.bing.com/search?q=keto"}),
            ctype="application/json")
        self.assertEqual(st2, 200)
        st, _, body = self._raw("GET", "/api/seoengines", cookie=self.cookie)
        tr = json.loads(body)["traffic"]["engines"]
        self.assertGreaterEqual(tr["google"]["views"], 1)
        self.assertGreaterEqual(tr["bing"]["views"], 1)

    def test_engine_from_referrer_map(self):
        m = self.server.Handler._engine_from_referrer
        self.assertEqual(m("https://www.google.com/search?q=x"), "google")
        self.assertEqual(m("https://www.bing.com/search"), "bing")
        self.assertEqual(m("https://yandex.ru/yandsearch"), "yandex")
        self.assertEqual(m("https://duckduckgo.com/?q=x"), "duckduckgo")
        self.assertEqual(m("https://search.yahoo.com/search?p=x"), "yahoo")
        self.assertEqual(m(""), "direct")
        self.assertEqual(m("https://news.ycombinator.com/"), "other")


if __name__ == "__main__":
    unittest.main()