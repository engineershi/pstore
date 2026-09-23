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
import urllib.parse
import unittest
import urllib.parse
import uuid
from http.server import ThreadingHTTPServer

sys_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if sys_path not in os.sys.path:
    os.sys.path.insert(0, sys_path)

import security
import seo
import webmasters
import indexnow

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
        self.bing_page_stats = {"d": [
            {"__type": "PageStats:#Microsoft.Bing.Webmaster.Api",
             "AvgClickPosition": 0, "AvgImpressionPosition": 2.5,
             "Clicks": 1, "Impressions": 30,
             "Date": "\\/Date(1399100400000)\\/",
             "Query": "http://trypstore.com/n/keto"},
            {"__type": "PageStats:#Microsoft.Bing.Webmaster.Api",
             "AvgClickPosition": 1, "AvgImpressionPosition": 4.0,
             "Clicks": 2, "Impressions": 60,
             "Date": "\\/Date(1401519600000)\\/",
             "Query": "http://trypstore.com/n/keto"},
            {"__type": "PageStats:#Microsoft.Bing.Webmaster.Api",
             "AvgClickPosition": 0, "AvgImpressionPosition": 9.0,
             "Clicks": 0, "Impressions": 10,
             "Date": "\\/Date(1401519600000)\\/",
             "Query": "http://trypstore.com/n/low-carb"}]}

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
        self.calls.append((method, url, body))
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
                {"siteUrl": "https://trypstore.com/",
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
        if "GetPageStats" in url:
            return 200, self.bing_page_stats
        if "SubmitFeed" in url:
            return 200, {}
        if "GetUserSites" in url:
            return 200, {"d": [{
                "__type": "Site:#Microsoft.Bing.Webmaster.Api",
                "AuthenticationCode": "ED01349E7C980956FE9C55F554DA7600",
                "DnsVerificationCode": "abc.trypstore.com",
                "IsVerified": False,
                "Url": "https://trypstore.com/"}]}
        if "AddSite" in url and "?apikey=" in url:
            return 200, {"d": None}
        if url.endswith("/user/"):
            return 200, {"user_id": "uid-1"}
        if "user-added-sitemaps" in url:
            return 201, {"sitemap_id": "sm-1"}
        if url.endswith("/hosts/") and method == "POST":
            return 201, {"host_id": "http:%s:80" % webmasters.host_of(),
                          "unicode_host_url": webmasters.site_url() + "/",
                          "verified": False}
        if "/hosts/" in url and "search-queries" not in url:
            return 200, {"hosts": [{"host_id": "h1",
                                    "ascii_host_url": "https://trypstore.com/",
                                    "unicode_host_url": "https://trypstore.com/",
                                    "verified": True}]}
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
        os.environ["PSTORE_URL"] = "https://trypstore.com"
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
        self.assertIn("https:%2F%2Ftrypstore.com%2F", put[0][1])

    def test_gsc_crawl_budget_and_inspect(self):
        # inspector needs a connected bearer token
        self.wm.store["seoeng.gsc.token"] = json.dumps(GSC_TOK)
        self.wm.store.pop("seoeng.gsc.inspect", None)
        self.wm.store.pop("seoeng.gsc.sitemap", None)
        self.wm.calls.clear()
        urls = ["https://trypstore.com/n/keto-snacks/low-carb",
                "https://trypstore.com/n/keto-snacks/healthy"]
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
        out = webmasters.gsc_crawl(["https://trypstore.com/n/x"])
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
        self.assertTrue(any("trypstore.com" in s for s in data["sites"]))
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
        self.assertIn("https://trypstore.com/", data["hosts"])

    def test_yandex_add_host(self):
        self.wm.store["seoeng.yandex.token"] = json.dumps(YANDEX_TOK)
        ok, d = webmasters.yandex_add_host()
        self.assertTrue(ok)
        self.assertIn("host", d)
        self.assertFalse(d["verified"])
        self.assertIn("verify", d["message"].lower())

    def test_yandex_add_host_already_registered(self):
        self.wm.store["seoeng.yandex.token"] = json.dumps(YANDEX_TOK)
        orig = webmasters._req
        def _already(method, url, headers=None, body=None, timeout=25):
            if url.endswith("/hosts/") and method == "POST":
                return 409, {"error_code": "HOST_ALREADY_ADDED",
                             "host_id": "http:trypstore.com:80",
                             "verified": False,
                             "error_message": "already there"}
            return orig(method, url, headers=headers, body=body, timeout=timeout)
        webmasters._set_transport(_already)
        try:
            ok, d = webmasters.yandex_add_host()
            self.assertTrue(ok)
            self.assertTrue(d["registered"])
            self.assertIn("already", d["message"].lower())
        finally:
            webmasters._set_transport(orig)

    def test_yandex_submit_url(self):
        self.wm.store["seoeng.yandex.token"] = json.dumps(YANDEX_TOK)
        ok, d = webmasters.yandex_submit_url("/n/keto")
        self.assertTrue(ok)
        self.assertIn("url", d)
        self.assertIn("keto", d["url"])
        call_url = self.wm.calls[-1][1]
        self.assertIn("/indexing/", call_url)
        target = webmasters.site_url() + "/n/keto"
        self.assertIn(urllib.parse.quote(target, safe=""), call_url)
        self.assertNotIn("://", call_url.split("/indexing/")[1])

    def test_yandex_submit_sitemap(self):
        self.wm.store["seoeng.yandex.token"] = json.dumps(YANDEX_TOK)
        ok, msg = webmasters.yandex_submit_sitemap()
        self.assertTrue(ok)
        self.assertIn("sitemap", msg)
        call = self.wm.calls[-1]
        self.assertIn("user-added-sitemaps", call[1])
        self.assertEqual(call[0], "POST")
        self.assertTrue((call[2] or {}).get("url", "").endswith("/sitemap.xml"))

    def test_yandex_submit_sitemap_no_host(self):
        self.wm.store["seoeng.yandex.token"] = json.dumps(YANDEX_TOK)
        orig = webmasters._req
        def _no_host_req(method, url, headers=None, body=None, timeout=25):
            if url.endswith("/hosts/") and method != "POST":
                return 200, {"hosts": []}
            return orig(method, url, headers=headers, body=body, timeout=timeout)
        webmasters._set_transport(_no_host_req)
        try:
            ok, msg = webmasters.yandex_submit_sitemap()
            self.assertFalse(ok)
            self.assertIn("host", str(msg).lower())
        finally:
            webmasters._set_transport(orig)

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
                                         "https://trypstore.com", 28)
        self.assertTrue(ok)
        self.assertEqual([r["page"] for r in data["rows"]],
                         ["http://trypstore.com/n/keto",
                          "http://trypstore.com/n/low-carb"])
        self.assertEqual(data["rows"][0]["clicks"], 3)
        self.assertEqual(data["rows"][0]["impressions"], 90)
        self.assertEqual(data["totals"]["clicks"], 3)
        self.assertEqual(data["totals"]["impressions"], 100)
        self.assertEqual(data["totals"]["ctr"], 3.0)
        self.assertIn("GetPageStats", self.wm.calls[-1][1])
        self.assertNotIn("GetKeywordStats", self.wm.calls[-1][1])

    def test_bing_stats_empty_envelope_is_ok(self):
        saved = self.wm.bing_page_stats
        try:
            self.wm.bing_page_stats = {"d": []}
            ok, data = webmasters.bing_stats(
                "bing-key-test", "https://trypstore.com", 28)
            self.assertTrue(ok)
            self.assertEqual(data["rows"], [])
            self.assertEqual(data["totals"]["clicks"], 0)
            self.assertEqual(data["totals"]["impressions"], 0)
        finally:
            self.wm.bing_page_stats = saved

    def test_bing_stats_bare_array_still_parses(self):
        saved = self.wm.bing_page_stats
        try:
            self.wm.bing_page_stats = [
                {"Clicks": 2, "Impressions": 8, "AvgImpressionPosition": 1.5,
                 "Query": "http://trypstore.com/n/waist"}]
            ok, data = webmasters.bing_stats(
                "bing-key-test", "https://trypstore.com", 28)
            self.assertTrue(ok)
            self.assertEqual(data["rows"][0]["page"],
                             "http://trypstore.com/n/waist")
            self.assertEqual(data["totals"]["impressions"], 8)
        finally:
            self.wm.bing_page_stats = saved

    def test_bing_submit_sitemap(self):
        s, d = webmasters.bing_submit_sitemap(
            "bing-key-test", "https://trypstore.com")
        self.assertEqual(s, 200)
        calls = [c for c in self.wm.calls if "SubmitFeed" in c[1]]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "POST")
        body = calls[0][2] or {}
        self.assertEqual(body.get("siteUrl"), "https://trypstore.com")
        self.assertTrue(body.get("feedUrl", "").endswith("/sitemap.xml"))
        self.assertNotIn("SubmitSitemap", calls[0][1])

    def test_bing_user_sites_parses_site_dicts(self):
        ok, data = webmasters.bing_user_sites("bing-key-test")
        self.assertTrue(ok)
        self.assertEqual(data["sites"], ["trypstore.com"])
        self.assertTrue(data["registered"])
        self.assertFalse(data["verified"])

    def test_yandex_auth_url(self):
        url = webmasters.yandex_auth_url("y-st")
        self.assertIn("oauth.yandex.ru/authorize", url)
        self.assertIn("client_id=yid-test", url)
        self.assertIn("webmaster%3Ahostinfo%20webmaster%3Averify", url)
        self.assertIn("redirect_uri=https%3A%2F%2Foauth.yandex.ru"
                      "%2Fverification_code", url)
        self.assertNotIn("cb%2Fyandex", url)

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

    def test_sync_engine_bing_persists_snapshot_even_when_empty(self):
        saved = self.wm.bing_page_stats
        try:
            self.wm.bing_page_stats = {"d": []}
            ok, data = webmasters.sync_engine("bing", 28)
        finally:
            self.wm.bing_page_stats = saved
        self.assertTrue(ok)
        snap = webmasters.last_sync("bing")
        self.assertTrue(snap)
        self.assertIn("at", snap)
        self.assertEqual(snap["totals"]["clicks"], 0)
        self.assertEqual(snap["totals"]["impressions"], 0)

    def test_engines_status_states(self):
        self.wm.store["seoeng.gsc.token"] = json.dumps(GSC_TOK)
        self.wm.store["seoeng.yandex.token"] = json.dumps(YANDEX_TOK)
        st = {r["engine"]: r for r in webmasters.engines_status()}
        self.assertEqual(st["gsc"]["state"], "ready")
        self.assertEqual(st["gsc"]["site"], "https://trypstore.com/")
        self.assertEqual(st["bing"]["state"], "ready")
        self.assertEqual(st["yandex"]["state"], "ready")

    def test_passive_engines_duckduckgo_yahoo(self):
        st = {r["engine"]: r for r in webmasters.engines_status()}
        for e in ("duckduckgo", "yahoo"):
            self.assertTrue(st[e]["passive"])
            self.assertIn("via", st[e])
            self.assertFalse(st[e]["token"])
            # IndexNow key is always set -> coverage is live.
            self.assertEqual(st[e]["state"], "ready")
        saved = indexnow._RUNTIME_KEY
        try:
            indexnow.set_key("")
            st = {r["engine"]: r for r in webmasters.engines_status()}
            self.assertEqual(st["duckduckgo"]["state"], "needs-key")
            self.assertEqual(st["yahoo"]["state"], "needs-key")
        finally:
            indexnow.set_key(saved if saved is not None else "")

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

    def test_yandex_runtime_creds_from_store(self):
        """Pasted client id/secret survive redeploys: with env cleared, the
        runtime override read from the settings store still marks yandex
        configured and builds an authorize URL."""
        id_saved = (webmasters.YANDEX_CLIENT_ID,
                    webmasters._YANDEX_CLIENT_ID_RUNTIME,
                    self.wm.store.get("seoeng.yandex.clientid"))
        sec_saved = (webmasters.YANDEX_CLIENT_SECRET,
                     webmasters._YANDEX_CLIENT_SECRET_RUNTIME,
                     self.wm.store.get("seoeng.yandex.clientsecret"))
        try:
            webmasters.YANDEX_CLIENT_ID = ""
            webmasters.YANDEX_CLIENT_SECRET = ""
            webmasters.set_yandex_client_creds("rt-id", "rt-secret")
            self.assertEqual(webmasters._yandex_client_id(), "rt-id")
            self.assertEqual(webmasters._yandex_client_secret(), "rt-secret")
            self.assertEqual(self.wm.store["seoeng.yandex.clientid"], "rt-id")
            self.assertEqual(self.wm.store["seoeng.yandex.clientsecret"],
                             "rt-secret")
            st = {r["engine"]: r for r in webmasters.engines_status()}
            self.assertTrue(st["yandex"]["client"])
            url = webmasters.yandex_auth_url("y-st")
            self.assertIn("client_id=rt-id", url)
            self.assertIn("oauth.yandex.ru%2Fverification_code", url)
        finally:
            webmasters.YANDEX_CLIENT_ID, webmasters._YANDEX_CLIENT_ID_RUNTIME, \
                store_id = id_saved
            webmasters.YANDEX_CLIENT_SECRET, \
                webmasters._YANDEX_CLIENT_SECRET_RUNTIME, store_sec = sec_saved
            self.wm.store["seoeng.yandex.clientid"] = store_id
            self.wm.store["seoeng.yandex.clientsecret"] = store_sec

    def test_yandex_exchange_uses_locked_redirect(self):
        ok, msg = webmasters.yandex_exchange("y-code")
        self.assertTrue(ok)
        call = self.wm.calls[-1]
        self.assertIn("/token", call[1])
        self.assertEqual(call[0], "POST")
        body = urllib.parse.parse_qs(call[2].decode())
        self.assertEqual(body["redirect_uri"][0],
                         "https://oauth.yandex.ru/verification_code")


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
        os.environ["PSTORE_URL"] = "https://trypstore.com"
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
        self.assertIn("DuckDuckGo", html)
        self.assertIn("Yahoo (via Bing)", html)
        self.assertIn('id="st-duckduckgo"', html)
        self.assertIn('id="st-yahoo"', html)
        self.assertIn("Traffic by engine", html)
        self.assertIn("/api/seoengines", html)
        # Regression: the inline action-JS must carry \n escapes as backslash-n,
        # never a real newline (a single \n in the python source becomes an actual
        # newline in the served HTML, which kills the whole <script> block).
        self.assertIn("\\n\\nsites on this key:\\n", html)
        self.assertNotIn("\n\nsites on this key:", html)

    def test_rss_feed_served_with_enclosures(self):
        st, _, body = self._raw("GET", "/rss.xml", cookie=self.cookie)
        self.assertEqual(st, 200)
        xml = body.decode("utf-8", "replace")
        self.assertIn("<rss version=\"2.0\"", xml)
        import re
        self.assertNotEqual(re.findall(r"<item>", xml), [])
        self.assertIn("<enclosure url=", xml)
        self.assertIn(".png", xml)
        # niche items carry a stable UTM so pin clicks are attributed to the
        # affiliate funnel (and the guid stays stable for n8n dedup)
        self.assertIn("utm_source=pinterest&amp;utm_medium=rss&amp;utm_campaign=", xml)
        self.assertIn("/n/", xml)

    def test_admin_rss_page(self):
        st, _, body = self._raw("GET", "/admin/rss", cookie=self.cookie)
        self.assertEqual(st, 200)
        page = body.decode("utf-8", "replace")
        self.assertIn("/rss.xml", page)
        self.assertIn("utm_source=pinterest", page)
        self.assertIn("Pinterest", page)
        self.assertIn("n8n", page)

    def test_admin_rss_page_requires_auth(self):
        # admin pages redirect guests to /admin/login (302), unlike APIs (401)
        st, loc, _ = self._raw("GET", "/admin/rss")
        self.assertEqual(st, 302)
        self.assertIn("/admin/login", loc)

    def test_analytics_page_shows_rss_card(self):
        st, _, body = self._raw("GET", "/admin/analytics", cookie=self.cookie)
        self.assertEqual(st, 200)
        page = body.decode("utf-8", "replace")
        self.assertIn("RSS &amp; Pinterest pins", page)
        self.assertIn("/admin/rss", page)

    def test_pin_health_api(self):
        # per-niche Pinterest attribution report backing the n8n health check
        st, _, body = self._raw("GET", "/api/pin-health", cookie=self.cookie)
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertTrue(d["ok"])
        for key in ("pinned_hits", "traffic_no_pin", "published_silent"):
            self.assertIn(key, d)
            self.assertIsInstance(d[key], list)
        self.assertIn("counts", d)
        self.assertIn("published_pinnable", d["counts"])
        self.assertGreaterEqual(d["counts"]["published_pinnable"], 1)
        for row in d["pinned_hits"]:
            self.assertIn("slug", row)
            self.assertGreaterEqual(row["pin_clicks"], 1)
            self.assertGreaterEqual(row["total_clicks"], row["pin_clicks"])

    def test_pin_health_api_days_param(self):
        st, _, body = self._raw("GET", "/api/pin-health?days=0", cookie=self.cookie)
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertEqual(d["days"], 0)

    def test_pin_health_api_requires_auth(self):
        st, _, body = self._raw("GET", "/api/pin-health")
        self.assertEqual(st, 401)
        self.assertIn(b"unauthorized", body)

    def test_seoengines_api_get(self):
        st, _, body = self._raw("GET", "/api/seoengines", cookie=self.cookie)
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertIn("engines", d)
        self.assertIn("traffic", d)
        self.assertIn("host", d)
        self.assertIn("sitemap", d)
        self.assertEqual(len(d["engines"]), 5)
        for e in d["engines"]:
            self.assertIn("state", e)
            self.assertIn("engine", e)
        by_engine = {e["engine"]: e for e in d["engines"]}
        self.assertIn("duckduckgo", by_engine)
        self.assertIn("yahoo", by_engine)
        self.assertTrue(by_engine["duckduckgo"]["passive"])
        self.assertTrue(by_engine["yahoo"]["passive"])
        self.assertIn("totals", d["traffic"])
        self.assertIn("google", d["traffic"]["engines"])

    def test_seoengines_sync_passive_engine(self):
        st, _, body = self._raw(
            "POST", "/api/seoengines", cookie=self.cookie,
            body=json.dumps({"action": "sync", "engine": "duckduckgo",
                             "days": 28}),
            ctype="application/json")
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertTrue(d["ok"])
        self.assertEqual(d["engine"], "DuckDuckGo")
        self.assertIn("totals", d)
        self.assertIn("clicks", d["totals"])
        self.assertIn("impressions", d["totals"])

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

    def test_submit_sitemap_yandex(self):
        self.server._set_setting("seoeng.yandex.token", json.dumps(YANDEX_TOK))
        st, _, body = self._raw(
            "POST", "/api/seoengines", cookie=self.cookie,
            body=json.dumps({"action": "submit", "engine": "yandex"}),
            ctype="application/json")
        d = json.loads(body)
        self.assertEqual(st, 200)
        self.assertEqual(d.get("ok"), True)
        self.assertIn("sitemap", d.get("message", ""))

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

    def test_yandex_pasted_creds_and_code_over_http(self):
        """The verification_code flow: client id/secret saved from the UI
        (persists to store) and the one-time code Yandex showed exchanged for
        a token through the API."""
        st, _, body = self._raw(
            "POST", "/api/seoengines", cookie=self.cookie,
            body=json.dumps({"action": "yacreds", "engine": "yandex",
                             "client_id": "rt-id",
                             "client_secret": "rt-secret"}),
            ctype="application/json")
        d = json.loads(body)
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        st, _, body = self._raw(
            "POST", "/api/seoengines", cookie=self.cookie,
            body=json.dumps({"action": "yacode", "engine": "yandex",
                             "code": "one-time-code"}),
            ctype="application/json")
        d = json.loads(body)
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        tok = json.loads(self.server._get_setting("seoeng.yandex.token"))
        self.assertEqual(tok["access_token"], "yandex-at")
        # the pasted client id/secret survive redeploys via the store
        self.assertEqual(webmasters.store_get("seoeng.yandex.clientid"),
                         "rt-id")
        self.assertEqual(webmasters.store_get("seoeng.yandex.clientsecret"),
                         "rt-secret")

    def test_crawl_post_and_paid_set(self):
        self.server._set_setting("seoeng.gsc.token", json.dumps(GSC_TOK))
        st, _, body = self._raw(
            "POST", "/api/seoengines", cookie=self.cookie,
            body=json.dumps({"action": "crawl",
                             "urls": ["https://trypstore.com/n/x"]}),
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