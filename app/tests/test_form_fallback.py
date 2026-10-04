# -*- coding: utf-8 -*-
"""Public opt-in forms must not render raw JSON in a browser window.

The reporter's symptom: submitting the spare email showed a JSON document in
the page — `{"ok": true, "id": 7, "message": ..., "download_token": ...,
"referral_url": ...}` — including a signed PDF token.

Cause: `/subscribe` and `/price-alert` are plain `<form action="/subscribe"
method="post">` elements whose handler *only* returned JSON. With JS working,
courier.js intercepts the submit and reads that JSON. Without it — blocked
asset, extension, CSP kill, JS error, stale cached HTML — the browser performs
the native POST and displays the response body as a document. The signup still
succeeded; it just looked like a broken website to the one person whose email
address we were trying to capture.

These tests pin the content negotiation on both directions: a browser
navigation gets a real page (no token in the visible body), and `fetch()` keeps
the exact JSON contract courier.js depends on.
"""
import http.client
import json
import os
import re
import shutil
import threading
import unittest
import urllib.parse
import uuid
from http.server import ThreadingHTTPServer

BROWSER_ACCEPT = ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "*/*;q=0.8")


class FormFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = "/tmp/pstore_test_formfb_%s.db" % uuid.uuid4().hex[:8]
        shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "pstore.db"), cls.db)
        os.environ["PSTORE_DB"] = cls.db
        os.environ["PSTORE_ADMIN_EMAIL"] = "owner@test.example"
        os.environ["PSTORE_ADMIN_PASSWORD"] = "test-pass-123"
        import server
        cls.server = server
        cls._saved_schema_ready = getattr(server, "_db_schema_ready", False)
        cls.server.DB = cls.db
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cls.server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    def setUp(self):
        import security
        # Other suites in a full run set PSTORE_DB to their own temp path and
        # reload the server module, so `server.DB` moves out from under a server
        # instance we booted earlier. Handlers read it per call, which pointed
        # this class at another suite's schema and turned every assertion into a
        # 500. Re-pin it before each test, and restore afterwards so we do not
        # push our DB out from under them.
        self.server.DB = self.db
        # app/pstore.db is the tracked 43-niche *seed*, not a full schema: it has
        # no subscribers/consent tables. Those are created by server._db() on
        # first use — but only while _db_schema_ready is False. Another suite
        # that boots the server first flips that flag, so this copied DB would
        # never be seeded and every subscribe died with
        # `sqlite3.OperationalError: no such table: subscribers`.
        self.server._db_schema_ready = False
        # 6 opt-ins per 10 min per device, keyed by client; this class posts ~10
        # times from 127.0.0.1. Clear so each test hits its own branch instead of
        # inheriting the previous test's 429 — and restore on the way out.
        self._saved_hits = dict(security.SUBSCRIBE_LIMITER._hits)
        security.SUBSCRIBE_LIMITER._hits = {}

    def tearDown(self):
        import security
        security.SUBSCRIBE_LIMITER._hits = self._saved_hits

    @classmethod
    def tearDownClass(cls):
        cls.server._db_schema_ready = cls._saved_schema_ready
        cls.httpd.shutdown()
        cls.thread.join(timeout=2)
        cls.httpd.server_close()
        cls.server.DB = os.environ.get("PSTORE_DB", cls.db)
        if os.path.exists(cls.db):
            os.unlink(cls.db)

    def _post(self, path, form, accept=BROWSER_ACCEPT):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        body = urllib.parse.urlencode(form)
        conn.request("POST", path, body=body, headers={
            "Accept": accept,
            "Content-Type": "application/x-www-form-urlencoded",
        })
        r = conn.getresponse()
        status, ctype, data = r.status, r.getheader("Content-Type"), r.read()
        conn.close()
        return status, ctype, data.decode("utf-8", "replace")

    def _email(self, tag):
        return "formfb-%s-%s@example.com" % (tag, uuid.uuid4().hex[:6])

    # ------------------------------------------------------------------ the bug
    def test_browser_submit_returns_a_page_not_json(self):
        status, ctype, body = self._post(
            "/subscribe", {"email": self._email("a"), "keyword": "keto"})
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertFalse(body.lstrip().startswith("{"),
                         "a browser must never be shown the raw JSON response")
        self.assertIn("<h1>", body)

    def test_signed_pdf_token_is_not_rendered_into_the_page(self):
        """The token is a bearer credential for the gated PDF. Showing it in the
        visible HTML of a confirmation page puts it in the page source, the
        browser history and any cache in between — and it is minted for a
        10-minute window, so a leaked one is a free download for whoever has
        it. It belongs in a link href only where the reader asked for the
        guide, and never in visible text."""
        _, _, body = self._post(
            "/subscribe", {"email": self._email("b"), "keyword": "keto",
                           "source": "niche-gate"})
        self.assertNotIn("download_token", body)
        self.assertNotRegex(body, r"pdf:keto:\d")
        # ...but the gate's whole promise is the download, so it must be
        # reachable: the token rides in a link href, not as text.
        links = re.findall(r'href="(/_gated/pdf[^"]+)"', body)
        self.assertTrue(links, "gate submit without JS must still offer the PDF")
        self.assertIn("token=", links[0])

    def test_confirmation_offers_a_way_onward(self):
        _, _, body = self._post(
            "/subscribe", {"email": self._email("c"), "keyword": "keto"})
        self.assertIn('href="/n/keto"', body)

    def test_confirmation_is_noindex_and_never_in_the_sitemap(self):
        """House rule: the sitemap and the robots meta must agree. This is a
        per-visitor confirmation, not a page, so it is noindex and unlisted."""
        _, _, body = self._post(
            "/subscribe", {"email": self._email("d"), "keyword": "keto"})
        self.assertIn('name="robots" content="noindex', body)
        xml = self.server.Handler.__new__(self.server.Handler)._sitemap()
        self.assertNotIn(b"/confirmed", xml)
        self.assertNotIn(b"/subscribed", xml)

    def test_repeat_subscriber_gets_a_page_too(self):
        email = self._email("e")
        self._post("/subscribe", {"email": email, "keyword": "keto"})
        status, ctype, body = self._post(
            "/subscribe", {"email": email, "keyword": "keto"})
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn("subscribed again", body.lower())

    # ------------------------------------------------------------ fetch() side
    def test_fetch_still_gets_the_exact_json_contract(self):
        """courier.js reads ok/message/download_token/referral_url. Content
        negotiation must not quietly break the JS path, or every opt-in on
        every page silently stops unlocking the guide."""
        status, ctype, body = self._post(
            "/subscribe", {"email": self._email("f"), "keyword": "keto",
                           "source": "niche-gate"},
            accept="*/*")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        d = json.loads(body)
        self.assertTrue(d["ok"])
        for field in ("id", "message", "download_token", "referral_url"):
            self.assertIn(field, d)
        self.assertTrue(d["download_token"])

    def test_fetch_error_path_still_returns_json(self):
        status, ctype, body = self._post(
            "/subscribe", {"email": "not-an-email", "keyword": "keto"},
            accept="*/*")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        d = json.loads(body)
        self.assertFalse(d["ok"])
        self.assertIn("error", d)

    # ------------------------------------------------------------- validation
    def test_browser_sees_why_a_bad_email_failed_and_can_retry(self):
        status, ctype, body = self._post(
            "/subscribe", {"email": "nope", "keyword": "keto"})
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertFalse(body.lstrip().startswith("{"))
        self.assertIn("doesn&#x27;t look right", body)
        self.assertIn('action="/subscribe"', body)

    # ------------------------------------------------------------ price alert
    def test_price_alert_browser_submit_returns_a_page(self):
        status, ctype, body = self._post(
            "/price-alert", {"email": self._email("g"), "keyword": "keto",
                             "asin": "B0TESTASIN1"})
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertFalse(body.lstrip().startswith("{"))
        self.assertNotIn("download_token", body)

    def test_price_alert_fetch_still_gets_json(self):
        status, ctype, body = self._post(
            "/price-alert", {"email": self._email("h"), "keyword": "keto",
                             "asin": "B0TESTASIN1"}, accept="*/*")
        self.assertIn("application/json", ctype)
        self.assertTrue(json.loads(body)["ok"])

    # --------------------------------------------------------------- the gate
    def test_expired_gate_link_shows_a_recovery_page(self):
        """Clicking an old shared /_gated/pdf link used to render
        {"error": "not authorized"}. It now offers the opt-in that mints a fresh
        token, which is the only path a reader without the token can take."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        conn.request("GET", "/_gated/pdf?keyword=keto&token=bogus",
                     headers={"Accept": BROWSER_ACCEPT})
        r = conn.getresponse()
        status, ctype, data = r.status, r.getheader("Content-Type"), r.read()
        conn.close()
        body = data.decode("utf-8", "replace")
        self.assertEqual(status, 403)
        self.assertIn("text/html", ctype)
        self.assertFalse(body.lstrip().startswith("{"))
        self.assertIn('action="/subscribe"', body)

    def test_gate_link_fetch_still_gets_json(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        conn.request("GET", "/_gated/pdf?keyword=keto&token=bogus",
                     headers={"Accept": "*/*"})
        r = conn.getresponse()
        status, body = r.status, r.read()
        conn.close()
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"], "not authorized")


class NegotiationUnitTests(unittest.TestCase):
    """The discriminator itself, isolated from HTTP."""

    class _H:
        def __init__(self, accept):
            self.headers = {"Accept": accept}

    def test_browser_navigation_is_html(self):
        from server import _wants_html
        for accept in (BROWSER_ACCEPT, "text/html", "text/html,*/*;q=0.1"):
            self.assertTrue(_wants_html(self._H(accept)), accept)

    def test_fetch_is_not_html(self):
        from server import _wants_html
        # fetch() sends */* by default; XHR often sends the full list, but
        # always with a JSON Accept from our own scripts.
        for accept in ("*/*", "application/json", "application/json, */*", ""):
            self.assertFalse(_wants_html(self._H(accept)), accept)


class NoOrphanCaptureFormTests(unittest.TestCase):
    """A capture form without courier.js is the exact shape of the reported bug.

    /blog and /stories both rendered lead_gate_html() — whose own docstring says
    courier.js operates it — and neither emitted <script src="/courier.js">. The
    submit listener therefore never attached, the browser fell back to a native
    form POST, and the reporter saw the raw /subscribe JSON (including a signed
    download token) rendered as a document.

    The shared _footer() loads ui.js only, so every renderer must load
    courier.js itself. That is easy to forget and impossible to notice by eye, so
    assert it per page type.
    """

    def _renderers(self):
        import seo
        products = [{"asin": "B0TEST0001", "title": "Test item",
                     "price": "9.99", "currency": "USD"}]
        pool = [{"keyword": "keto", "products": products}]
        return {
            "niche": seo.render_niche("keto", pool[0], saved_niches=pool),
            "topic": seo.render_topic("keto snacks", "keto", pool[0], "keto",
                                      saved_niches=pool),
            "blog": seo.render_blog(pool),
            "stories_gallery": seo.render_stories_gallery(pool),
            "story": seo.render_story(pool[0]),
            "landing": seo.render_landing(pool),
            "niche_index": seo.render_niche_index(pool),
            "priceband": seo.render_priceband("50", "keto", "keto", products),
            "vs": seo.render_vs("keto", "keto snacks", "B0TEST0001",
                                "B0TEST0002", "keto", "keto", products),
        }

    def test_no_page_emits_a_capture_form_without_courier_js(self):
        offenders = []
        for name, html in self._renderers().items():
            body = html.decode("utf-8", "replace") if isinstance(html, bytes) else html
            has_form = ('class="courier' in body) or ('class="wm-form' in body)
            if has_form and 'src="/courier.js"' not in body:
                offenders.append(name)
        self.assertEqual(offenders, [],
                         "these pages render a capture form with no courier.js, "
                         "so submitting falls through to a raw JSON document: %s"
                         % ", ".join(offenders))

    def test_lead_gate_requires_the_script_argument_to_be_explicit(self):
        """Guards the call-site ergonomics: the parameter has no default that
        would silently render a form nobody operates."""
        import inspect
        import seo
        params = inspect.signature(seo.lead_gate_html).parameters
        self.assertIn("courier_script_tag", params)
        self.assertIs(params["courier_script_tag"].default, "")


if __name__ == "__main__":
    unittest.main()