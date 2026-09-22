# -*- coding: utf-8 -*-
"""Offline tests for the pstore social suite: UTM-tracked post kits, the
/og/<slug> share-card SVG, one-click publishing (+ optional webhook), the
/admin/social page and per-post click attribution via /api/track."""

import json
import datetime
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import indexnow
import mailer
import market_engine
import publish
import security
import server
import seo
import social
import webmasters


class _WebhookHandler(BaseHTTPRequestHandler):
    received = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        self.__class__.received.append(json.loads(raw))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


class TestSocialSuite(unittest.TestCase):
    """Boots a real HTTP server against a copied DB so kit generation, publish
    and attribution run against the same handlers pstore ships."""

    IPKEY = "127.0.0.1|127.0.0.1"

    @classmethod
    def setUpClass(cls):
        cls.db = "/tmp/pstore_test_social_%s.db" % uuid.uuid4().hex[:8]
        shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "pstore.db"), cls.db)
        import sqlite3 as _sqlite
        _conn = _sqlite.connect(cls.db)
        has_settings = _conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='settings'"
        ).fetchone()[0]
        if has_settings:
            _conn.execute("DELETE FROM settings WHERE key LIKE 'social.key%'")
            _conn.commit()
        _conn.close()
        cls._env_backup = {k: os.environ.get(k) for k in (
            "PSTORE_DB", "PSTORE_ADMIN_EMAIL", "PSTORE_ADMIN_PASSWORD",
            "PSTORE_URL", "SOCIAL_WEBHOOK")}
        os.environ["PSTORE_DB"] = cls.db
        os.environ["PSTORE_ADMIN_EMAIL"] = "owner@test.example"
        os.environ["PSTORE_ADMIN_PASSWORD"] = "test-pass-123"
        os.environ.pop("PSTORE_URL", None)
        os.environ.pop("SOCIAL_WEBHOOK", None)
        import importlib
        importlib.reload(server)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.PORT = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls._saved_indexnow_post = indexnow._post
        indexnow._post = cls._fake_indexnow_post
        cls.cookie = cls._login()

    @classmethod
    def _fake_indexnow_post(cls, url, payload, timeout=20):
        return None

    @classmethod
    def tearDownClass(cls):
        indexnow._post = cls._saved_indexnow_post
        security.SUBSCRIBE_LIMITER.clear("sub|" + cls.IPKEY)
        security.TRACK_LIMITER.clear("trk|" + cls.IPKEY)
        cls.httpd.shutdown()
        cls.thread.join(timeout=2)
        cls.httpd.server_close()
        if os.path.exists(cls.db):
            os.unlink(cls.db)
        for k, v in cls._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import importlib
        importlib.reload(server)

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
        security.SUBSCRIBE_LIMITER.clear("sub|" + self.IPKEY)
        security.TRACK_LIMITER.clear("trk|" + self.IPKEY)
        self._saved_warm = server._warm_og_png
        server._warm_og_png = self._noop_warm  # no background ~4s PNG renders in tests
        with server._lock:
            conn = server._db()
            conn.execute("DELETE FROM clicks")
            conn.execute("DELETE FROM social_posts")
            conn.commit()
            conn.close()

    def tearDown(self):
        server._warm_og_png = self._saved_warm

    @staticmethod
    def _noop_warm(*a, **k):
        """Background og-PNG warming is stubbed in tests: each render is ~4s of
        pure-Python CPU, enough to starve the single sandbox core and make
        unrelated HTTP tests time out."""
        return None

    def _api(self, q="keto snacks"):
        st, _, ctype, data = self._raw("/api/social?keyword=" + urllib.parse.quote(q),
                                       cookie=self.cookie)
        self.assertEqual(st, 200)
        return json.loads(data)

    def test_social_api_requires_admin(self):
        st, _, _, data = self._raw("/api/social?keyword=keto+snacks")
        self.assertEqual(st, 401)
        self.assertIn(b"unauthorized", data)

    def test_admin_social_page_requires_login(self):
        st, location, _, _ = self._raw("/admin/social")
        self.assertEqual(st, 302)
        self.assertTrue(location.startswith("/admin/login"))

    def test_admin_social_page_renders_kits(self):
        st, _, _, data = self._raw("/admin/social", cookie=self.cookie)
        self.assertEqual(st, 200)
        html = data.decode("utf-8", "replace")
        for platform in social.PLATFORMS:
            self.assertIn(platform, html)
        self.assertIn("Publish", html)
        self.assertIn("/api/social/publish", html)
        self.assertIn("View live", html)
        self.assertIn("/lp/keto?utm_source=", html)

    def test_published_post_carries_live_link(self):
        st, _, _, data = self._raw(
            "/api/social/publish", "POST",
            body=json.dumps({"keyword": "keto snacks", "platform": "Twitter / X"}),
            cookie=self.cookie)
        self.assertEqual(st, 200)
        kit = json.loads(data)["posts"][0]
        st, _, _, page = self._raw("/admin/social?keyword=keto+snacks", cookie=self.cookie)
        html = page.decode("utf-8", "replace")
        self.assertIn("open ↗", html)
        self.assertIn(seo._clean(kit["link"]), html)
        st, _, _, api = self._raw(
            "/api/social?keyword=" + urllib.parse.quote("keto snacks"), cookie=self.cookie)
        res = json.loads(api)
        pub = next(p for p in res["published"] if p["utm_content"] == kit["utm_content"])
        self.assertIn("utm_source=", pub["link"])
        self.assertEqual(pub["link"], kit["link"])

    def test_kits_are_utm_tracked_to_landing(self):
        d = self._api()
        self.assertEqual(len(d["kits"]), len(social.PLATFORMS))
        for kit in d["kits"]:
            self.assertIn("utm_source=", kit["link"])
            self.assertIn("utm_campaign=keto-snacks", kit["link"])
            self.assertIn("/lp/keto-snacks?", kit["link"])
            self.assertTrue(kit["utm_content"])
            self.assertEqual(kit["target"], "landing")

    def test_kits_include_telegram_win_loop_channel(self):
        """The composer emits a Telegram kit (utm_source=telegram) so the content
        engine + winner-amplify loop can post natively to a free channel."""
        d = self._api()
        tg = [k for k in d["kits"] if k["platform"] == "Telegram"]
        self.assertEqual(len(tg), 1)
        self.assertEqual(social._key("Telegram"), "telegram")
        self.assertIn("utm_source=telegram", tg[0]["link"])

    def test_kits_compose_tiktok_and_youtube(self):
        """"The video composers ship TikTok and YouTube kits (short-form hook
        caption + a real video title and description) both UTM-tracked to the
        landing page, and they fall through to webhook posting (no native key)."""
        d = self._api()
        tt = [k for k in d["kits"] if k["platform"] == "TikTok"]
        self.assertEqual(len(tt), 1)
        self.assertIn("utm_source=tiktok", tt[0]["link"])
        self.assertIn("utm_campaign=keto-snacks", tt[0]["link"])
        self.assertLessEqual(len(tt[0]["body"]), 180)
        self.assertTrue(tt[0]["hashtags"])
        yt = [k for k in d["kits"] if k["platform"] == "YouTube"]
        self.assertEqual(len(yt), 1)
        self.assertIn("utm_source=youtube", yt[0]["link"])
        self.assertIn("utm_campaign=keto-snacks", yt[0]["link"])
        self.assertLessEqual(len(yt[0]["title"]), 100)
        self.assertIn("https://", yt[0]["body"])
        # no native backend → post_to reports "skipped" (webhook applies)
        kv = lambda ns, name="": ""
        res = publish.post_to("TikTok", yt[0], kv)
        self.assertEqual(res["via"], "skipped")
        res = publish.post_to("YouTube", yt[0], kv)
        self.assertEqual(res["via"], "skipped")

    def test_publish_single_platform(self):
        d = self._api()
        core = social._ALPHABET.lower()

        def code_of(kit):
            return kit["link"].split("utm_content=")[-1]
        for kit in d["kits"]:
            self.assertTrue(all(ch in core for ch in code_of(kit)))
        st, _, ctype, data = self._raw(
            "/api/social/publish", "POST",
            body=json.dumps({"keyword": "keto snacks",
                             "platform": "Twitter / X"}),
            cookie=self.cookie)
        self.assertEqual(st, 200)
        res = json.loads(data)
        self.assertTrue(res["ok"])
        self.assertEqual(res["published"], 1)
        kit = res["posts"][0]
        self.assertTrue(kit["platform"], "Twitter / X")
        with server._lock:
            conn = server._db()
            row = conn.execute(
                "SELECT status FROM social_posts WHERE utm_content=?",
                (kit["utm_content"],)).fetchone()
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "published")

    def test_publish_all_platforms_idempotent(self):
        st, _, _, data = self._raw(
            "/api/social/publish", "POST",
            body=json.dumps({"keyword": "keto snacks", "platform": "all"}),
            cookie=self.cookie)
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(data)["published"], len(social.PLATFORMS))
        st, _, _, data2 = self._raw(
            "/api/social/publish", "POST",
            body=json.dumps({"keyword": "keto snacks", "platform": "all"}),
            cookie=self.cookie)
        self.assertEqual(st, 200)
        with server._lock:
            conn = server._db()
            n = conn.execute("SELECT COUNT(*) c FROM social_posts").fetchone()["c"]
            conn.close()
        self.assertEqual(n, len(social.PLATFORMS))

    def test_publish_all_niches_covers_every_saved_niche(self):
        st, _, _, data = self._raw(
            "/api/social/publish-all", "POST", cookie=self.cookie, timeout=180)
        self.assertEqual(st, 200)
        res = json.loads(data)
        self.assertTrue(res["ok"])
        self.assertGreaterEqual(res["niches"], 1)
        self.assertGreaterEqual(res["published"], len(social.PLATFORMS))
        with server._lock:
            conn = server._db()
            n = conn.execute("SELECT COUNT(*) c FROM social_posts").fetchone()["c"]
            conn.close()
        self.assertGreaterEqual(n, len(social.PLATFORMS))

    def test_publish_unknown_platform_rejected(self):
        st, _, _, data = self._raw(
            "/api/social/publish", "POST",
            body=json.dumps({"keyword": "keto snacks", "platform": "Blogger"}),
            cookie=self.cookie)
        self.assertEqual(st, 400)
        self.assertIn(b"unknown platform", data)

    def test_webhook_fires_per_published_post(self):
        saved = os.environ.get("SOCIAL_WEBHOOK")
        handler = _WebhookHandler
        handler.received = []
        stub = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = stub.server_address[1]
        t = threading.Thread(target=stub.serve_forever, daemon=True)
        t.start()
        try:
            os.environ["SOCIAL_WEBHOOK"] = "http://127.0.0.1:%d/hook" % port
            st, _, _, data = self._raw(
                "/api/social/publish", "POST",
                body=json.dumps({"keyword": "keto snacks", "platform": "all"}),
                cookie=self.cookie)
            self.assertEqual(st, 200)
            self.assertTrue(json.loads(data)["ok"])
            deadline = 0
            while len(handler.received) < len(social.PLATFORMS) and deadline < 50:
                import time as _t
                _t.sleep(0.05)
                deadline += 1
            self.assertEqual(len(handler.received), len(social.PLATFORMS))
            first = handler.received[0]
            for key in ("body", "link", "platform", "slug", "keyword", "board",
                        "name", "image", "image_png", "pin_image"):
                self.assertIn(key, first)
            self.assertEqual(first["keyword"], "keto snacks")
            self.assertEqual(first["slug"], "keto-snacks")
            self.assertEqual(first["board"], "Keto Snacks")
            self.assertTrue(first["image"].endswith("/og/keto-snacks"))
            self.assertTrue(first["image_png"].endswith("/og/keto-snacks.png"))
            self.assertTrue(first["pin_image"].endswith("/og/keto-snacks-pin.png"))
        finally:
            if saved is None:
                os.environ.pop("SOCIAL_WEBHOOK", None)
            else:
                os.environ["SOCIAL_WEBHOOK"] = saved
            stub.shutdown()
            t.join(timeout=2)
            stub.server_close()

    def test_schedule_single_platform_spaced(self):
        st, _, ctype, data = self._raw(
            "/api/social/schedule", "POST",
            body=json.dumps({"keyword": "keto snacks", "platform": "all", "hours": 24}),
            cookie=self.cookie)
        self.assertEqual(st, 200)
        res = json.loads(data)
        self.assertTrue(res["ok"])
        self.assertEqual(res["scheduled"], len(social.PLATFORMS))
        self.assertEqual(res["platform"], "all")
        with server._lock:
            conn = server._db()
            rows = conn.execute(
                "SELECT platform, status, scheduled_at FROM social_posts "
                "WHERE slug=? AND status='scheduled' ORDER BY scheduled_at",
                (seo._slugify("keto snacks"),)).fetchall()
            conn.close()
        self.assertEqual(len(rows), len(social.PLATFORMS))
        ats = [r["scheduled_at"] for r in rows]
        self.assertEqual(sorted(ats), ats)          # spaced, not all at once
        self.assertEqual(set(r["status"] for r in rows), {"scheduled"})
        self.assertNotIn("", ats)

    def test_schedule_requires_auth(self):
        st, _, _, _ = self._raw(
            "/api/social/schedule", "POST",
            body=json.dumps({"keyword": "keto snacks", "platform": "all"}))
        self.assertEqual(st, 401)

    def test_flush_publishes_only_due_scheduled_posts(self):
        # schedule kit 1 of 'all' to a past time so only it is due
        st, _, _, data = self._raw(
            "/api/social/schedule", "POST",
            body=json.dumps({"keyword": "keto snacks", "platform": "all", "hours": 24}),
            cookie=self.cookie)
        self.assertEqual(st, 200)
        with server._lock:
            conn = server._db()
            kinfo = conn.execute(
                "SELECT id FROM social_posts WHERE status='scheduled' "
                "ORDER BY scheduled_at LIMIT 1").fetchone()
            k = conn.execute(
                "SELECT MIN(scheduled_at) AS least FROM social_posts "
                "WHERE status='scheduled'").fetchone()["least"]
            conn.execute("UPDATE social_posts SET scheduled_at=? WHERE id=?",
                         ("2000-01-01 00:00:00", kinfo["id"]))
            conn.commit()
            conn.close()
        st, _, _, data = self._raw(
            "/api/social/flush", "POST", body=b"{}",
            headers={"Content-Type": "application/json"}, cookie=self.cookie)
        self.assertEqual(st, 200)
        res = json.loads(data)
        self.assertEqual(res["published_now"], 1)
        with server._lock:
            conn = server._db()
            due = conn.execute(
                "SELECT status FROM social_posts WHERE id=?",
                (kinfo["id"],)).fetchone()["status"]
            still = conn.execute(
                "SELECT COUNT(*) AS n FROM social_posts WHERE status='scheduled'"
            ).fetchone()["n"]
            conn.close()
        self.assertEqual(due, "published")
        self.assertEqual(res["still_pending"], still)   # the rest remain queued

    def test_blitz_endpoint_publishes_all_queued(self):
        st, _, _, data = self._raw(
            "/api/social/schedule", "POST",
            body=json.dumps({"keyword": "keto snacks", "platform": "all", "hours": 24}),
            cookie=self.cookie)
        self.assertEqual(st, 200)
        with server._lock:
            conn = server._db()
            total = conn.execute(
                "SELECT COUNT(*) n FROM social_posts WHERE status='scheduled'"
            ).fetchone()["n"]
            conn.close()
        st, _, _, data = self._raw(
            "/api/social/blitz", "POST", body=b"{}",
            headers={"Content-Type": "application/json"}, cookie=self.cookie)
        self.assertEqual(st, 200)
        res = json.loads(data)
        self.assertTrue(res["ok"])
        self.assertEqual(res["published_now"], total)
        self.assertEqual(res["still_pending"], 0)

    def test_blitz_requires_auth(self):
        st, _, _, _ = self._raw("/api/social/blitz", "POST",
                                body=b"{}", headers={"Content-Type": "application/json"})
        self.assertEqual(st, 401)

    def test_auto_flush_module_function_due_only(self):
        # queue via the API, then force one post past-due and call the
        # handler-free module function (the timer path) with a captured hook.
        st, _, _, data = self._raw(
            "/api/social/schedule", "POST",
            body=json.dumps({"keyword": "keto snacks", "platform": "all", "hours": 24}),
            cookie=self.cookie)
        self.assertEqual(st, 200)
        with server._lock:
            conn = server._db()
            kinfo = conn.execute(
                "SELECT id FROM social_posts WHERE status='scheduled' "
                "ORDER BY scheduled_at LIMIT 1").fetchone()
            conn.execute("UPDATE social_posts SET scheduled_at='2000-01-01 00:00:00' "
                         "WHERE id=?", (kinfo["id"],))
            conn.commit()
            conn.close()
        fired = []
        n, pending = server._flush_due_social(lambda kits: fired.extend(kits))
        self.assertEqual(n, 1)
        self.assertEqual(len(fired), 1)
        self.assertTrue(fired[0]["platform"])
        self.assertTrue(fired[0]["body"])
        with server._lock:
            conn = server._db()
            st2 = conn.execute("SELECT status FROM social_posts WHERE id=?",
                               (kinfo["id"],)).fetchone()["status"]
            conn.close()
        self.assertEqual(st2, "published")

    def test_blitz_publishes_every_scheduled_post_even_not_due(self):
        # schedule two niches; push one of them far into the future so only a
        # due-flush would skip it — the blitz must publish everything anyway.
        st, _, _, data = self._raw(
            "/api/social/schedule", "POST",
            body=json.dumps({"keyword": "keto snacks", "platform": "all", "hours": 24}),
            cookie=self.cookie)
        self.assertEqual(st, 200)
        with server._lock:
            conn = server._db()
            conn.execute(
                "UPDATE social_posts SET scheduled_at='2099-12-31 00:00:00' "
                "WHERE status='scheduled'")
            total = conn.execute(
                "SELECT COUNT(*) n FROM social_posts WHERE status='scheduled'"
            ).fetchone()["n"]
            conn.commit()
            conn.close()
        self.assertGreater(total, 1)
        fired = []
        n, pending = server._flush_all_social(lambda kits: fired.extend(kits))
        self.assertEqual(n, total)
        self.assertEqual(len(fired), total)
        self.assertEqual(pending, 0)
        with server._lock:
            conn = server._db()
            left = conn.execute(
                "SELECT COUNT(*) n FROM social_posts WHERE status='scheduled'"
            ).fetchone()["n"]
            published = conn.execute(
                "SELECT COUNT(*) n FROM social_posts WHERE status='published'"
            ).fetchone()["n"]
            conn.close()
        self.assertEqual(left, 0)
        self.assertEqual(published, total)

    def test_og_image_served_for_saved_niche(self):
        st, _, ctype, data = self._raw("/og/keto-snacks")
        self.assertEqual(st, 200)
        self.assertTrue(ctype.startswith("image/svg+xml"))
        self.assertTrue(data.lstrip().startswith(b"<svg"))

    def test_og_png_served_as_raster_card(self):
        st, _, ctype, data = self._raw("/og/keto-snacks.png")
        self.assertEqual(st, 200)
        self.assertTrue(ctype.startswith("image/png"))
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(data), 1000)
        # !read−only IHDR: 1200x630 truecolor
        import struct as _st
        w, h = _st.unpack(">II", data[16:24])
        self.assertEqual((w, h), (1200, 630))
        self.assertEqual(data[25], 2)  # color type RGB

    def test_og_png_fallback_for_unknown_slug(self):
        # Every og:image a page declares must resolve, so unknown slugs render
        # a generic brand card instead of a dangling 404 share image.
        st, _, ctype, data = self._raw("/og/not-a-real-niche.png")
        self.assertEqual(st, 200)
        self.assertTrue(ctype.startswith("image/png"))
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(data), 1000)
        import struct as _st
        w, h = _st.unpack(">II", data[16:24])
        self.assertEqual((w, h), (1200, 630))

    def test_og_svg_fallback_for_unknown_slug(self):
        st, _, ctype, data = self._raw("/og/not-a-real-niche")
        self.assertEqual(st, 200)
        self.assertTrue(ctype.startswith("image/svg+xml"))
        self.assertIn(b"Best picks, ranked fresh", data)

    def test_og_png_prewarm_caches_to_disk(self):
        saved = server.OG_CACHE_DIR
        tmp = tempfile.mkdtemp(prefix="ogcache_")
        try:
            server.OG_CACHE_DIR = tmp
            server._prewarm_og_pngs()
            path = os.path.join(tmp, "keto-snacks.png")
            self.assertTrue(os.path.exists(path), "prewarm must persist the real card")
            with open(path, "rb") as f:
                disk = f.read()
            self.assertTrue(disk.startswith(b"\x89PNG\r\n\x1a\n"))
            st, _, ctype, data = self._raw("/og/keto-snacks.png")
            self.assertEqual(st, 200)
            self.assertEqual(data, disk, "endpoint serves the exact prewarmed bytes")
        finally:
            server.OG_CACHE_DIR = saved
            shutil.rmtree(tmp, ignore_errors=True)

    def test_og_png_served_from_disk_after_restart(self):
        # Simulate a fresh process (empty in-memory cache): the persistent
        # disk cache must answer instantly with identical bytes.
        saved = server.OG_CACHE_DIR
        saved_cache = dict(server._PNG_CACHE)
        tmp = tempfile.mkdtemp(prefix="ogcache_")
        try:
            server.OG_CACHE_DIR = tmp
            server._cache_og_png_card("keto-snacks")
            server._PNG_CACHE.clear()
            with open(os.path.join(tmp, "keto-snacks.png"), "rb") as f:
                disk = f.read()
            st, _, ctype, data = self._raw("/og/keto-snacks.png")
            self.assertEqual(st, 200)
            self.assertTrue(ctype.startswith("image/png"))
            self.assertEqual(data, disk)
        finally:
            server.OG_CACHE_DIR = saved
            server._PNG_CACHE.clear()
            server._PNG_CACHE.update(saved_cache)
            shutil.rmtree(tmp, ignore_errors=True)

    def test_og_favicon_png_served(self):
        st, _, ctype, data = self._raw("/og/favicon.png")
        self.assertEqual(st, 200)
        self.assertTrue(ctype.startswith("image/png"))
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        import struct as _st
        w, h = _st.unpack(">II", data[16:24])
        self.assertEqual((w, h), (64, 64))

    def test_pages_reference_favicon_and_locale(self):
        st, _, _, data = self._raw("/blog")
        self.assertEqual(st, 200)
        self.assertIn(b"/og/favicon.png", data)
        self.assertIn(b'property="og:locale" content="en_US"', data)

    def test_pinterest_board_name(self):
        self.assertEqual(server._pinterest_board("keto snacks"), "Keto Snacks")
        self.assertEqual(server._pinterest_board(""), "Deals")
        self.assertEqual(server._pinterest_board("skin care"),
                         "Skin Care")
        self.assertLessEqual(len(server._pinterest_board("x" * 120)), 60)

    def test_niche_page_points_og_at_generated_card(self):
        st, _, _, data = self._raw("/n/keto-snacks")
        self.assertEqual(st, 200)
        self.assertIn(b'property="og:image"', data)
        self.assertIn(b"/og/keto-snacks", data)

    def test_landing_page_carries_beacon_and_og(self):
        st, _, _, data = self._raw("/lp/keto-snacks")
        self.assertEqual(st, 200)
        html = data.decode("utf-8", "replace")
        self.assertIn('data-niche="keto-snacks"', html)
        self.assertIn('data-source="landing"', html)
        self.assertIn("/courier.js", html)
        self.assertIn("data-asin=", html)
        self.assertIn('property="og:image"', html)

    def test_track_stores_utm_content_for_attribution(self):
        with server._lock:
            conn = server._db()
            conn.execute("DELETE FROM clicks")
            conn.commit()
            conn.close()
        st, _, _, _ = self._raw(
            "/api/track", "POST",
            body=json.dumps({"slug": "keto-snacks", "source": "twitter",
                             "content": "abc123", "asin": "B0KETO1234"}))
        self.assertEqual(st, 200)
        with server._lock:
            conn = server._db()
            row = conn.execute(
                "SELECT source, content FROM clicks ORDER BY id DESC LIMIT 1").fetchone()
            conn.close()
        self.assertEqual(row["source"], "twitter")
        self.assertEqual(row["content"], "abc123")

    def test_track_utm_get_fallback(self):
        st, _, _, _ = self._raw(
            "/api/track?slug=keto-snacks&source=facebook&content=x1y2&asin=B0KETO1234")
        self.assertEqual(st, 200)
        with server._lock:
            conn = server._db()
            row = conn.execute(
                "SELECT source, content FROM clicks ORDER BY id DESC LIMIT 1").fetchone()
            conn.close()
        self.assertEqual(row["content"], "x1y2")

    def test_published_post_shows_click_count(self):
        self._raw("/api/social/publish", "POST",
                  body=json.dumps({"keyword": "keto snacks", "platform": "Twitter / X"}),
                  cookie=self.cookie)
        d = self._api()
        code = d["kits"][0]["utm_content"]
        self._raw("/api/track", "POST",
                  body=json.dumps({"slug": "keto-snacks", "source": "twitter",
                                   "content": code}))
        self._raw("/api/track", "POST",
                  body=json.dumps({"slug": "keto-snacks", "source": "twitter",
                                   "content": code}))
        d2 = self._api()
        self.assertEqual(d2["stats"].get(code), 2)
        for p in d2["published"]:
            self.assertEqual(p["status"], "published")

    def test_workbench_includes_tracked_kits(self):
        st, _, _, data = self._raw(
            "/api/tools?keyword=" + urllib.parse.quote("keto snacks"), cookie=self.cookie)
        self.assertEqual(st, 200)
        payload = json.loads(data)
        self.assertEqual(len(payload["social_kit"]), len(social.PLATFORMS))
        for kit in payload["social_kit"]:
            self.assertIn("utm_source=", kit["link"])

    def test_social_generation_needs_top_pick(self):
        kits = social.post_kits("empty niche", [], base_url="http://x.example")
        self.assertEqual(kits, [])

    def test_socialwebhook_not_configured_is_honest(self):
        os.environ.pop("SOCIAL_WEBHOOK", None)
        st, _, _, data = self._raw(
            "/api/social/publish", "POST",
            body=json.dumps({"keyword": "keto snacks", "platform": "Facebook"}),
            cookie=self.cookie)
        res = json.loads(data)
        self.assertEqual(st, 200)
        self.assertFalse(res.get("webhook"))

    def test_webhook_endpoint_is_public_and_records_skipped_draft(self):
        """POST /api/social/webhook needs no login (it is the zero-cost router
        scheduled publishing + external tools fire) and, with no platform creds,
        honestly records the kit as a draft row instead of dropping it."""
        st, _, _, data = self._raw(
            "/api/social/webhook", "POST",
            body=json.dumps({"platform": "Pinterest", "slug": "keto-snacks",
                             "name": "Keto Snacks", "body": "Great keto picks",
                             "link": "https://example.com/lp/keto-snacks"}))
        self.assertEqual(st, 200)
        res = json.loads(data)
        self.assertEqual(res["platform"], "Pinterest")
        self.assertFalse(res["ok"])
        self.assertEqual(res["via"], "skipped")
        with server._lock:
            conn = server._db()
            rows = conn.execute(
                "SELECT status FROM social_posts WHERE slug='keto-snacks' AND "
                "platform='Pinterest' AND utm_content='webhook:Pinterest'"
            ).fetchall()
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "draft")

    def test_webhook_endpoint_requires_platform_and_slug(self):
        st, _, _, data = self._raw(
            "/api/social/webhook", "POST",
            body=json.dumps({"platform": "Pinterest"}))
        self.assertEqual(st, 400)
        st2, _, _, _ = self._raw("/api/social/webhook", "POST", body=b"not-json")
        self.assertEqual(st2, 400)

    def test_webhook_endpoint_posts_natively_with_creds(self):
        """With Pinterest keys pasted on /admin/apikeys the webhook posts the
        kit through the native gateway and flips the recorded row to published."""
        saved = publish._post
        server._set_setting("social.key.pinterest.token", "tok-abc")
        try:
            calls = []

            def fake_post(url, payload, headers, timeout=15):
                calls.append(url)
                return 200, {"id": "555666"}

            publish._post = fake_post
            st, _, _, data = self._raw(
                "/api/social/webhook", "POST",
                body=json.dumps({"platform": "Pinterest", "slug": "keto-snacks",
                                 "board_id": "1212", "name": "Keto",
                                 "body": "Keto picks",
                                 "link": "https://example.com/x",
                                 "image": "https://example.com/i.png"}))
            self.assertEqual(st, 200)
            res = json.loads(data)
            self.assertTrue(res["ok"])
            self.assertEqual(res["via"], "native")
            self.assertEqual(len(calls), 1)
            with server._lock:
                conn = server._db()
                row = conn.execute(
                    "SELECT status FROM social_posts WHERE slug='keto-snacks' AND "
                    "platform='Pinterest'").fetchone()
                conn.close()
            self.assertEqual(row["status"], "published")
        finally:
            publish._post = saved
            server._set_setting("social.key.pinterest.token", "")

    def test_webhook_endpoint_retries_once_on_transient_failure(self):
        saved = publish._post
        server._set_setting("social.key.pinterest.token", "tok-abc")
        try:
            calls = []

            def flaky_post(url, payload, headers, timeout=15):
                calls.append(url)
                if len(calls) == 1:
                    return 0, {}
                return 200, {"id": "777888"}

            publish._post = flaky_post
            st, _, _, data = self._raw(
                "/api/social/webhook", "POST",
                body=json.dumps({"platform": "Pinterest", "slug": "keto-snacks",
                                 "board_id": "9", "name": "Keto",
                                 "body": "Keto picks",
                                 "link": "https://example.com/x",
                                 "image": "https://example.com/i.png"}))
            self.assertEqual(st, 200)
            res = json.loads(data)
            self.assertTrue(res["ok"])
            self.assertEqual(len(calls), 2)
        finally:
            publish._post = saved
            server._set_setting("social.key.pinterest.token", "")

    def test_ui_saved_webhook_activates_after_restart(self):
        """A webhook saved on /admin/apikeys must actually fire after a
        restart — the boot restore rehydrates _SOCIAL_WEBHOOK from the DB
        (env still wins). Regression: UI-saved webhooks only showed in admin
        and never reached the publishing path."""
        saved_env = os.environ.get("SOCIAL_WEBHOOK")
        saved_mod = server._SOCIAL_WEBHOOK
        try:
            os.environ.pop("SOCIAL_WEBHOOK", None)
            server._set_setting("social.webhook", "https://hook.example/restart")
            # simulate cold boot: module value empty, restore re-runs
            server._SOCIAL_WEBHOOK = ""
            server._init()
            self.assertEqual(server._SOCIAL_WEBHOOK, "https://hook.example/restart")
            st, _, _, data = self._raw(
                "/api/social/publish", "POST",
                body=json.dumps({"keyword": "keto snacks", "platform": "Facebook"}),
                cookie=self.cookie)
            res = json.loads(data)
            self.assertEqual(st, 200)
            self.assertTrue(res.get("webhook"))
        finally:
            server._set_setting("social.webhook", "")
            server._SOCIAL_WEBHOOK = saved_mod
            if saved_env is None:
                os.environ.pop("SOCIAL_WEBHOOK", None)
            else:
                os.environ["SOCIAL_WEBHOOK"] = saved_env

    def test_auto_amplify_requeues_winner_to_scheduled(self):
        # seed a published post with clicks, old enough to be re-amplified
        import datetime as _dt
        with server._lock:
            c = server._db()
            c.execute(
                "INSERT INTO social_posts (slug, keyword, platform, name, body, link, "
                "utm_content, status, published_at) VALUES ('keto-snacks','keto snacks',"
                "'Twitter','win','b','http://x/lp/keto','code-amp','published','2020-01-01 08:00:00')")
            pid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            c.commit()
            c.close()
        with server._lock:
            c = server._db()
            c.execute("INSERT INTO clicks (slug, source, ip, referrer, asin, content) "
                      "VALUES ('keto-snacks','social','t','','','code-amp')")
            c.commit()
            c.close()
        now = _dt.datetime(2020, 1, 3, 12, 0, 0)  # 2 days after publish
        res = server._auto_amplify_winners(now=now)
        self.assertTrue(res["on"])
        self.assertEqual(res["requeued"], 1)
        self.assertEqual(res["winners"][0]["slug"], "keto-snacks")
        self.assertEqual(res["winners"][0]["amp"], 1)
        with server._lock:
            c = server._db()
            row = c.execute("SELECT id,status,scheduled_at,amplify_count FROM social_posts "
                            "WHERE id=?", (pid,)).fetchone()
            c.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "scheduled")
        self.assertIsNotNone(row["scheduled_at"])
        self.assertEqual(row["amplify_count"], 1)

    def test_auto_amplify_caps_runs(self):
        # a post already amplified to max_runs is not requeued again
        import datetime as _dt
        server._set_setting("social.amplify.max_runs", "1")
        server._set_setting("social.amplify.min_age_hours", "24")
        try:
            with server._lock:
                c = server._db()
                c.execute(
                    "INSERT INTO social_posts (slug, keyword, platform, name, body, link, "
                    "utm_content, status, published_at, amplify_count) VALUES "
                    "('keto-snacks','keto snacks','Twitter','win','b','http://x/lp/keto',"
                    "'code-cap','published','2020-01-01 08:00:00',1)")
                pid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
                c.commit()
                c.close()
            with server._lock:
                c = server._db()
                c.execute("INSERT INTO clicks (slug, source, ip, referrer, asin, content) "
                          "VALUES ('keto-snacks','social','t','','','code-cap')")
                c.commit()
                c.close()
            now = _dt.datetime(2020, 1, 3, 12, 0, 0)
            res = server._auto_amplify_winners(now=now)
            self.assertEqual(res["requeued"], 0)
            with server._lock:
                c = server._db()
                row = c.execute("SELECT status,amplify_count FROM social_posts WHERE id=?",
                                (pid,)).fetchone()
                c.close()
            self.assertEqual(row["status"], "published")
            self.assertEqual(row["amplify_count"], 1)
        finally:
            server._set_setting("social.amplify.max_runs", "2")
            server._set_setting("social.amplify.min_age_hours", "24")

    def _drip_token(self, token):
        """Paste/clear the Pinterest token for the drip (returns prev for restore)."""
        prev = server._get_setting("social.key.pinterest.token")
        server._set_setting("social.key.pinterest.token", token)
        return prev

    def _drip_settings(self):
        """Capture drip settings (token, daily, min_gap, marker) for restore."""
        return {k: server._get_setting(k) for k in (
            "social.key.pinterest.token", "social.drip.daily",
            "social.drip.min_gap_days", "social.drip.last", "social.drip")}

    def _restore_drip_settings(self, saved):
        for k, v in saved.items():
            server._set_setting(k, v)

    def test_drip_schedules_fresh_pins_for_unpinned_niches(self):
        import datetime as _dt
        saved = self._drip_settings()
        now = _dt.datetime(2026, 9, 13, 10, 0, 0)
        try:
            server._set_setting("social.key.pinterest.token", "pin-token")
            server._set_setting("social.drip.daily", "50")
            server._set_setting("social.drip.last", "2000-01-01")
            res = server._pin_drip(now=now)
            self.assertTrue(res["on"])
            self.assertFalse(res.get("need_token"))
            self.assertGreaterEqual(res["scheduled"], 1)
            self.assertEqual(server._get_setting("social.drip.last"),
                             now.strftime("%Y-%m-%d"))
            with server._lock:
                c = server._db()
                rows = c.execute(
                    "SELECT slug, keyword, status, scheduled_at FROM social_posts "
                    "WHERE platform='Pinterest' AND status='scheduled'").fetchall()
                c.close()
            pins = {r["slug"]: r for r in rows}
            self.assertGreaterEqual(len(pins), 1)
            for slug, r in pins.items():
                self.assertEqual(r["status"], "scheduled")
                hr = int(str(r["scheduled_at"]).split(" ")[1].split(":")[0])
                self.assertIn(hr, server.SOCIAL_PEAK_SLOTS)
        finally:
            self._restore_drip_settings(saved)

    def test_drip_respects_min_gap_days(self):
        import datetime as _dt
        saved = self._drip_settings()
        now = _dt.datetime(2026, 9, 13, 10, 0, 0)
        yesterday = "2026-09-12 08:00:00"
        try:
            server._set_setting("social.key.pinterest.token", "pin-token")
            server._set_setting("social.drip.daily", "50")
            server._set_setting("social.drip.min_gap_days", "30")
            server._set_setting("social.drip.last", "2000-01-01")
            # every saved niche already has a recent Pinterest pin -> all gated
            with server._lock:
                c = server._db()
                kws = [r["keyword"] for r in
                       c.execute("SELECT keyword FROM niches ORDER BY id").fetchall()]
                for i, kw in enumerate(kws):
                    c.execute(
                        "INSERT INTO social_posts (slug, keyword, platform, name, body, "
                        "link, utm_content, status, published_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (server.seo._slugify(kw), kw, "Pinterest", "pin", "b",
                         "http://x/lp/%s" % server.seo._slugify(kw),
                         "drip-gap-%d" % i, "published", yesterday))
                c.commit()
                c.close()
            res = server._pin_drip(now=now)
            self.assertTrue(res["on"])
            self.assertEqual(res["scheduled"], 0)
            self.assertNotIn("need_token", res)
        finally:
            self._restore_drip_settings(saved)

    def test_drip_runs_once_per_day(self):
        import datetime as _dt
        saved = self._drip_settings()
        now = _dt.datetime(2026, 9, 13, 10, 0, 0)
        try:
            server._set_setting("social.key.pinterest.token", "pin-token")
            server._set_setting("social.drip.daily", "5")
            server._set_setting("social.drip.last", "2000-01-01")
            first = server._pin_drip(now=now)
            self.assertGreaterEqual(first["scheduled"], 1)
            second = server._pin_drip(now=now)
            self.assertEqual(second["scheduled"], 0)
            self.assertEqual(second.get("already"), now.strftime("%Y-%m-%d"))
        finally:
            self._restore_drip_settings(saved)

    def test_drip_requires_pinterest_token(self):
        import datetime as _dt
        saved = self._drip_settings()
        now = _dt.datetime(2026, 9, 13, 10, 0, 0)
        try:
            server._set_setting("social.key.pinterest.token", "")
            server._set_setting("social.drip.last", "2000-01-01")
            res = server._pin_drip(now=now)
            self.assertTrue(res["on"])
            self.assertTrue(res.get("need_token"))
            self.assertEqual(res["scheduled"], 0)
        finally:
            self._restore_drip_settings(saved)

    def test_drip_rotates_caption_variant_and_repin_image(self):
        import datetime as _dt
        saved = self._drip_settings()
        now = _dt.datetime(2026, 9, 13, 10, 0, 0)
        try:
            server._set_setting("social.key.pinterest.token", "pin-token")
            server._set_setting("social.drip.daily", "50")
            server._set_setting("social.drip.min_gap_days", "1")
            server._set_setting("social.drip.last", "2000-01-01")
            self._raw("/api/captions/save", "POST",
                      body=json.dumps({"slug": "keto-snacks", "variants": [
                          {"platform": "Pinterest", "variant": 1,
                           "caption": "Caption One", "enabled": True},
                          {"platform": "Pinterest", "variant": 2,
                           "caption": "Caption Two", "enabled": True},
                      ]}), cookie=self.cookie)
            # one prior pin means the next caption is the 2nd variant
            with server._lock:
                c = server._db()
                c.execute(
                    "INSERT INTO social_posts (slug, keyword, platform, name, body, link, "
                    "utm_content, status, published_at) VALUES ('keto-snacks','keto snacks',"
                    "'Pinterest','pin','b','http://x/lp/keto','seed-c1','published',"
                    "'2026-09-10 08:00:00')")
                c.commit()
                c.close()
            res = server._pin_drip(now=now)
            self.assertGreaterEqual(res["scheduled"], 1)
            with server._lock:
                c = server._db()
                row = c.execute(
                    "SELECT body, link, utm_content, status FROM social_posts "
                    "WHERE platform='Pinterest' AND slug='keto-snacks' "
                    "AND status='scheduled' ORDER BY id DESC LIMIT 1").fetchone()
                c.close()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "scheduled")
            self.assertEqual(row["body"], "Caption Two")
            self.assertTrue(row["utm_content"].endswith("-c2"))
            self.assertIn("utm_content=%s" % row["utm_content"], row["link"])
        finally:
            self._restore_drip_settings(saved)
            with server._lock:
                c = server._db()
                c.execute("DELETE FROM social_captions WHERE slug='keto-snacks' "
                          "AND lower(platform)='pinterest'")
                c.commit()
                c.close()

    def test_og_variant_png_renders_distinct_look(self):
        st, _, ctype, base = self._raw("/og/keto-snacks.png")
        self.assertEqual(st, 200)
        self.assertEqual("image/png", (ctype or "").split(";")[0])
        st2, _, ctype2, var = self._raw("/og/keto-snacks.png.v1")
        self.assertEqual(st2, 200)
        self.assertEqual("image/png", (ctype2 or "").split(";")[0])
        self.assertNotEqual(base, var)
        self.assertGreater(len(var), 0)

    def _png_dims(self, data):
        import struct
        return struct.unpack(">II", data[16:24])

    def test_pint_image_url_base_and_variants(self):
        self.assertEqual(social.pint_image_png_url("https://x", "keto"),
                         "https://x/og/keto-pin.png")
        self.assertEqual(social.pint_image_png_url("https://x", "keto", 0),
                         "https://x/og/keto-pin.png")
        self.assertEqual(social.pint_image_png_url("https://x", "keto", 3),
                         "https://x/og/keto-pin.png.v3")
        kits = social.post_kits("keto snacks",
                            [{"asin": "B0KETO1", "title": "Keto Chips",
                              "reviews": 10, "stars": 4.6, "price": 8.99,
                              "currency": "USD"}],
                            "https://x")
        p = next(k for k in kits if (k.get("platform") or "").lower() == "pinterest")
        self.assertTrue(p["pin_image"].startswith(
            "https://x/og/keto-snacks-pin.png"))
        self.assertTrue(p["pin_image"].endswith("-pin.png"))

    def test_og_pint_portrait_png_is_2x3_and_variant_differs(self):
        st, _, ctype, base = self._raw("/og/keto-snacks-pin.png")
        self.assertEqual(st, 200)
        self.assertEqual("image/png", (ctype or "").split(";")[0])
        self.assertEqual(self._png_dims(base), (1000, 1500))
        st2, _, ctype2, var = self._raw("/og/keto-snacks-pin.png.v2")
        self.assertEqual(st2, 200)
        self.assertEqual("image/png", (ctype2 or "").split(";")[0])
        self.assertEqual(self._png_dims(var), (1000, 1500))
        self.assertNotEqual(base, var)

    def test_drip_endpoint_requires_admin_and_runs(self):
        saved = self._drip_settings()
        try:
            st, _, _, _ = self._raw("/api/social/drip", "POST", body=b"{}")
            self.assertEqual(st, 401)
            server._set_setting("social.key.pinterest.token", "pin-token")
            server._set_setting("social.drip.last", "2000-01-01")
            st2, _, _, data = self._raw("/api/social/drip", "POST",
                                        body=b"{}", cookie=self.cookie)
            self.assertEqual(st2, 200)
            res = json.loads(data)
            self.assertTrue(res.get("ok"))
            self.assertIn("scheduled", res)
        finally:
            self._restore_drip_settings(saved)

    def test_drip_board_payload_and_backlog_math(self):
        saved = self._drip_settings()
        try:
            server._set_setting("social.drip.last", "2000-01-01")
            b = server._drip_board()
            self.assertTrue(b["ok"])
            for key in ("on", "token", "daily", "min_gap_days", "last_run",
                        "already_today", "pinnable", "pinned_niches", "backlog",
                        "backfill_days", "horizon"):
                self.assertIn(key, b)
            self.assertEqual(b["daily"], 6)
            self.assertEqual(b["backlog"],
                             max(b["pinnable"] - b["pinned_niches"], 0))
            self.assertFalse(b["already_today"])
            self.assertIsInstance(b["horizon"], list)
        finally:
            self._restore_drip_settings(saved)

    def test_drip_board_gate_and_token_flags(self):
        saved = self._drip_settings()
        try:
            server._set_setting("social.drip", "0")
            server._set_setting("social.key.pinterest.token", "")
            b = server._drip_board()
            self.assertFalse(b["on"])
            self.assertFalse(b["token"])
            server._set_setting("social.drip", "1")
            server._set_setting("social.key.pinterest.token", "pin-token")
            b = server._drip_board()
            self.assertTrue(b["on"])
            self.assertTrue(b["token"])
        finally:
            self._restore_drip_settings(saved)

    def test_spawn_first_pin_gating(self):
        saved = self._drip_settings()
        try:
            server._set_setting("social.drip", "0")
            ok, reason = server._spawn_first_pin("off niche", [{"asin": "B1"}])
            self.assertEqual((ok, reason), (False, "drip_off"))
            server._set_setting("social.drip", "1")
            server._set_setting("social.key.pinterest.token", "")
            ok, reason = server._spawn_first_pin("no token", [{"asin": "B2"}])
            self.assertEqual((ok, reason), (False, "no_token"))
            server._set_setting("social.key.pinterest.token", "pin-token")
            ok, reason = server._spawn_first_pin("", [{"asin": "B3"}])
            self.assertEqual((ok, reason), (False, "not_pinnable"))
        finally:
            self._restore_drip_settings(saved)

    def test_spawn_first_pin_schedules_once_then_skips(self):
        saved = self._drip_settings()
        kw = "fresh drip niche test"
        try:
            server._set_setting("social.key.pinterest.token", "pin-token")
            server._set_setting("social.drip", "1")
            with server._lock:
                c = server._db()
                c.execute("DELETE FROM social_posts WHERE keyword=?", (kw,))
                c.commit()
                c.close()
            ok, reason = server._spawn_first_pin(
                kw, [{"asin": "B9", "title": "T", "price": 9.99,
                      "reviews": 1, "stars": 4.5}])
            self.assertEqual((ok, reason), (True, "scheduled"))
            ok2, reason2 = server._spawn_first_pin(kw, [{"asin": "B9"}])
            self.assertEqual((ok2, reason2), (False, "already_pinned"))
        finally:
            self._restore_drip_settings(saved)

    def test_drip_api_get_board_and_pace_save(self):
        saved = self._drip_settings()
        try:
            st, _, _, _ = self._raw("/api/social/drip")
            self.assertEqual(st, 401)
            server._set_setting("social.key.pinterest.token", "pin-token")
            st, _, _, data = self._raw("/api/social/drip", cookie=self.cookie)
            self.assertEqual(st, 200)
            b = json.loads(data)
            self.assertTrue(b["ok"])
            self.assertIn("backlog", b)
            st, _, _, data = self._raw("/api/social/drip", "POST",
                                       body=json.dumps({"daily": 3}),
                                       cookie=self.cookie)
            self.assertEqual(st, 200)
            self.assertEqual(json.loads(data)["daily"], 3)
            self.assertEqual(server._get_setting("social.drip.daily"), "3")
        finally:
            self._restore_drip_settings(saved)

    def test_drip_api_run_sweep_respects_today_marker(self):
        saved = self._drip_settings()
        try:
            server._set_setting("social.key.pinterest.token", "pin-token")
            server._set_setting("social.drip.last",
                                datetime.datetime.utcnow().strftime("%Y-%m-%d"))
            st, _, _, data = self._raw("/api/social/drip", "POST",
                                       body=json.dumps({"run": True}),
                                       cookie=self.cookie)
            self.assertEqual(st, 200)
            res = json.loads(data)
            self.assertTrue(res["already"])
            self.assertEqual(res["scheduled"], 0)
        finally:
            self._restore_drip_settings(saved)

    def test_save_niche_hook_spawns_first_pin(self):
        saved = self._drip_settings()
        saved_topics = server._get_setting("niches.auto_topics")
        saved_inspect = webmasters.inspect_new
        saved_gsc = webmasters.gsc_submit_sitemap_daily
        kw = "hook test %s" % uuid.uuid4().hex[:6]
        try:
            webmasters.inspect_new = lambda *a, **k: None
            webmasters.gsc_submit_sitemap_daily = lambda *a, **k: None
            server._set_setting("niches.auto_topics", "0")
            server._set_setting("social.drip", "1")
            server._set_setting("social.key.pinterest.token", "")
            self._raw("/api/niches", "POST", cookie=self.cookie,
                      body=json.dumps({"keyword": kw, "score": 5, "saturation": 1,
                                       "products": [{"asin": "B9"}]}))
            server._set_setting("social.key.pinterest.token", "pin-token")
            kw2 = "hook test b %s" % uuid.uuid4().hex[:6]
            self._raw("/api/niches", "POST", cookie=self.cookie,
                      body=json.dumps({"keyword": kw2, "score": 5, "saturation": 1,
                                       "products": [
                                           {"asin": "B9", "title": "T",
                                            "price": 9.99, "reviews": 1,
                                            "stars": 4.5}]}))
            with server._lock:
                c = server._db()
                row = c.execute(
                    "SELECT slug, keyword, status, scheduled_at FROM social_posts "
                    "WHERE lower(platform)='pinterest' AND keyword=?",
                    (kw2,)).fetchone()
                c.close()
            self.assertIsNotNone(row)
            self.assertEqual(row["slug"], seo._slugify(kw2))
            self.assertEqual(row["status"], "scheduled")
            self.assertIsNotNone(row["scheduled_at"])
        finally:
            self._restore_drip_settings(saved)
            server._set_setting("niches.auto_topics", saved_topics)
            webmasters.inspect_new = saved_inspect
            webmasters.gsc_submit_sitemap_daily = saved_gsc

    def _seed_caption_variants(self, platform="Twitter / X"):
        """Insert two enabled caption variants for a platform on keto-snacks."""
        self._raw("/api/captions/save", "POST",
                  body=json.dumps({"slug": "keto-snacks", "variants": [
                      {"platform": platform, "variant": 1, "caption": "Caption One",
                       "enabled": True},
                      {"platform": platform, "variant": 2, "caption": "Caption Two",
                       "enabled": True},
                  ]}), cookie=self.cookie)

    def test_caption_variants_publish_as_separate_posts(self):
        """Multiple enabled caption variants for a platform become one published
        post each, with distinct stable variant-suffixed tracked codes."""
        self._seed_caption_variants()
        st, _, _, data = self._raw(
            "/api/social/publish", "POST",
            body=json.dumps({"keyword": "keto snacks", "platform": "Twitter / X"}),
            cookie=self.cookie)
        self.assertEqual(st, 200)
        res = json.loads(data)
        tw = [k for k in res["posts"] if k["platform"] == "Twitter / X"]
        self.assertEqual(len(tw), 2)
        codes = [k["utm_content"] for k in tw]
        self.assertEqual(len(set(codes)), 2)
        for k in tw:
            self.assertTrue(k["utm_content"].endswith("-c1")
                            or k["utm_content"].endswith("-c2"))
            self.assertIn("caption_variant", k)
        self.assertEqual({k["body"] for k in tw}, {"Caption One", "Caption Two"})

    def test_captions_autoclean_disables_low_clicker(self):
        """Caption A/B autoclean keeps the high-clicking variant and disables the
        one clicking below 25% of the leader once a platform has enough clicks."""
        server._set_setting("ab.captions_min_clicks", "5")
        try:
            self._seed_caption_variants()
            st, _, _, data = self._raw(
                "/api/social/publish", "POST",
                body=json.dumps({"keyword": "keto snacks", "platform": "Twitter / X"}),
                cookie=self.cookie)
            self.assertEqual(st, 200)
            res = json.loads(data)
            by_body = {k["body"]: k["utm_content"]
                       for k in res["posts"] if k["platform"] == "Twitter / X"}
            self.assertIn("Caption One", by_body)
            self.assertIn("Caption Two", by_body)
            for _ in range(30):
                self._raw("/api/track", "POST",
                          body=json.dumps({"slug": "keto-snacks", "source": "twitter",
                                           "content": by_body["Caption One"]}))
            for _ in range(2):
                self._raw("/api/track", "POST",
                          body=json.dumps({"slug": "keto-snacks", "source": "twitter",
                                           "content": by_body["Caption Two"]}))
            st2, _, _, body2 = self._raw("/api/captions/autoclean", "POST",
                                         body=b"{}", cookie=self.cookie)
            self.assertEqual(st2, 200)
            d = json.loads(body2)
            self.assertTrue(d["ok"])
            found = [c for c in d["changed"]
                     if c["slug"] == "keto-snacks"
                     and c["platform"] == "twitter / x"]
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["disabled"], [2])
            self.assertEqual(found[0]["kept"], 1)
            with server._lock:
                conn = server._db()
                en2 = conn.execute(
                    "SELECT enabled FROM social_captions WHERE slug='keto-snacks' "
                    "AND platform='Twitter / X' AND variant=2").fetchone()
                en1 = conn.execute(
                    "SELECT enabled FROM social_captions WHERE slug='keto-snacks' "
                    "AND platform='Twitter / X' AND variant=1").fetchone()
                conn.close()
            self.assertEqual(en1["enabled"], 1)
            self.assertEqual(en2["enabled"], 0)
        finally:
            server._set_setting("ab.captions_min_clicks", "")

    # ------------------------------------------- batch 2: attribution truth

    def test_click_channel_maps_every_platform_key(self):
        u = server._click_channel
        for s in ("twitter", "facebook", "linkedin", "instagram",
                  "pinterest", "threads", "pin", "x"):
            self.assertEqual(u(s), "social", s)
        self.assertEqual(u("email"), "email")
        for s in ("page", "niche", "landing", "organic", "coupon", ""):
            self.assertEqual(u(s), "organic", s)

    def test_track_records_channel_and_rendered_tag(self):
        with server._lock:
            conn = server._db()
            conn.execute("DELETE FROM clicks")
            conn.commit()
            conn.close()
        st, _, _, _ = self._raw(
            "/api/track", "POST",
            body=json.dumps({"slug": "keto-snacks", "source": "instagram",
                             "content": "zzz99", "asin": "B0KETO1234",
                             "tag": "peterm-20"}))
        self.assertEqual(st, 200)
        with server._lock:
            conn = server._db()
            row = conn.execute(
                "SELECT source, channel, tag FROM clicks ORDER BY id DESC LIMIT 1").fetchone()
            conn.close()
        self.assertEqual(row["source"], "instagram")
        self.assertEqual(row["channel"], "social")
        self.assertEqual(row["tag"], "peterm-20")

    def test_organic_site_click_gets_organic_channel(self):
        st, _, _, _ = self._raw(
            "/api/track", "POST",
            body=json.dumps({"slug": "keto-snacks", "source": "page"}))
        self.assertEqual(st, 200)
        with server._lock:
            conn = server._db()
            row = conn.execute(
                "SELECT channel FROM clicks ORDER BY id DESC LIMIT 1").fetchone()
            conn.close()
        self.assertEqual(row["channel"], "organic")

    def test_social_funnel_counts_platform_sources(self):
        """Historic dead query: WHERE source='social' matched nothing because
        the beacon writes the UTM platform key. Channel fixed it: platform-key
        clicks must now count toward the marketing payload's social.clicks."""
        with server._lock:
            conn = server._db()
            conn.execute("DELETE FROM clicks")
            conn.commit()
            conn.close()
        self._raw("/api/track", "POST",
                  body=json.dumps({"slug": "keto-snacks", "source": "twitter"}))
        self._raw("/api/track", "POST",
                  body=json.dumps({"slug": "keto-snacks", "source": "pinterest"}))
        self._raw("/api/track", "POST",
                  body=json.dumps({"slug": "keto-snacks", "source": "page"}))
        st, _, _, data = self._raw("/api/marketing", cookie=self.cookie)
        self.assertEqual(st, 200)
        p = json.loads(data)
        self.assertEqual(p["social"]["clicks"], 2)

    def test_winners_score_real_channel_clicks_and_leads(self):
        with server._lock:
            conn = server._db()
            conn.execute("DELETE FROM clicks")
            conn.execute("DELETE FROM subscribers WHERE utm_content='win-abc'")
            conn.commit()
            conn.execute(
                "INSERT INTO social_posts (slug, keyword, platform, name, link, "
                "utm_content, status, published_at) "
                "VALUES ('keto-snacks','keto snacks','Pinterest','Ketofied',"
                "'','win-abc','published', datetime('now'))")
            conn.execute(
                "INSERT INTO subscribers (email, utm_content, confirmed, unsubscribed) "
                "VALUES ('lead@example.com','win-abc',1,0)")
            conn.commit()
            conn.close()
        st, _, _, _ = self._raw(
            "/api/track", "POST",
            body=json.dumps({"slug": "keto-snacks", "source": "pinterest",
                             "content": "win-abc"}))
        self.assertEqual(st, 200)
        st, _, _, data = self._raw("/api/marketing", cookie=self.cookie)
        self.assertEqual(st, 200)
        winners = json.loads(data)["social"]["winners"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["utm_content"], "win-abc")
        self.assertEqual(winners[0]["clicks"], 1)
        self.assertEqual(winners[0]["leads"], 1)

    def test_subscribe_captures_utm_source_and_content(self):
        email = "utm-lead@example.com"
        with server._lock:
            conn = server._db()
            conn.execute("DELETE FROM subscribers WHERE email=?", (email,))
            conn.commit()
            conn.close()
        st, _, _, data = self._raw(
            "/subscribe", "POST",
            body=json.dumps({"email": email, "keyword": "keto snacks",
                             "source": "niche",
                             "utm_source": "twitter", "utm_content": "win-abc"}))
        self.assertEqual(st, 200)
        d = json.loads(data)
        self.assertTrue(d["ok"], d)
        with server._lock:
            conn = server._db()
            row = conn.execute(
                "SELECT source, utm_source, utm_content FROM subscribers WHERE email=?",
                (email,)).fetchone()
            conn.close()
        self.assertEqual(row["source"], "niche")  # page-type preserved
        self.assertEqual(row["utm_source"], "twitter")
        self.assertEqual(row["utm_content"], "win-abc")

    # ------------------------------------------------------- delivery hardening
    def _insert_scheduled_post(self, slug="keto-snacks", platform="Pinterest",
                               stamp=None):
        with server._lock:
            conn = server._db()
            cur = conn.execute(
                "INSERT INTO social_posts (slug, keyword, platform, name, body, link, "
                "utm_content, status, scheduled_at) VALUES (?,?,?,?,?,?,?, 'scheduled', ?)",
                (slug, "keto snacks", platform, "Ketofied", "Body of %s" % slug,
                 "https://example.com/lp/%s?utm_content=hd%s" % (slug, slug[:4]),
                 "hd-" + slug[:8], stamp or "2000-01-01 00:00:00"))
            pid = cur.lastrowid
            conn.commit()
            conn.close()
        return pid

    def test_flush_claims_then_delivers_then_publishes(self):
        """The pipeline must claim (scheduling->publishing) atomically BEFORE any
        delivery and only flip to published AFTER delivery dispatches, so a
        scheduled post is never 'published' without being sent."""
        pid = self._insert_scheduled_post()
        with server._lock:
            conn = server._db()
            row = conn.execute("SELECT * FROM social_posts WHERE id=?", (pid,)).fetchone()
            conn.close()
        claimed = [dict(row)]
        with server._lock:
            conn = server._db()
            server._claim_due_social(conn, claimed, "2000-01-01 00:00:01")
            conn.commit()
            conn.close()
        with server._lock:
            conn = server._db()
            s1 = conn.execute("SELECT status, deliver_attempts, scheduled_at "
                              "FROM social_posts WHERE id=?", (pid,)).fetchone()
            conn.close()
        self.assertEqual(s1["status"], "publishing")
        self.assertEqual(s1["deliver_attempts"], 1)
        self.assertIsNone(s1["scheduled_at"])
        fired = []
        server._deliver_claimed_social(claimed, lambda kits: fired.extend(kits))
        self.assertEqual([k["slug"] for k in fired], ["keto-snacks"])
        with server._lock:
            conn = server._db()
            d1 = conn.execute("SELECT delivered_at, status FROM social_posts WHERE id=?",
                              (pid,)).fetchone()
            conn.close()
        self.assertEqual(d1["status"], "publishing")
        self.assertTrue(d1["delivered_at"])
        server._settle_claimed_social(claimed, "2000-01-01 00:00:02")
        with server._lock:
            conn = server._db()
            s2 = conn.execute("SELECT status, published_at FROM social_posts WHERE id=?",
                              (pid,)).fetchone()
            conn.close()
        self.assertEqual(s2["status"], "published")
        self.assertEqual(s2["published_at"], "2000-01-01 00:00:02")

    def test_recover_stuck_social_settles_stale_but_skips_fresh(self):
        """Rows stranded in 'publishing' by a crash (old claim, attempts below
        the cap) are re-claimed and delivered; recent claims are left alone."""
        stale = self._insert_scheduled_post(slug="keto-snacks")
        fresh = self._insert_scheduled_post(slug="weight-loss")
        with server._lock:
            conn = server._db()
            conn.execute("UPDATE social_posts SET status='publishing', claimed_at=?, "
                         "deliver_attempts=1 WHERE id=?", ("2000-01-01 00:00:00", stale))
            conn.execute("UPDATE social_posts SET status='publishing', claimed_at=?, "
                         "deliver_attempts=1 WHERE id=?",
                         ("2099-01-01 00:00:00", fresh))
            conn.commit()
            conn.close()
        captured = []
        saved_hook = server._webhook_fire
        server._webhook_fire = lambda kits: captured.extend(kits)
        try:
            n = server._recover_stuck_social()
        finally:
            server._webhook_fire = saved_hook
        self.assertEqual(n, 1)
        with server._lock:
            conn = server._db()
            s1 = conn.execute("SELECT status, delivered_at, deliver_attempts "
                              "FROM social_posts WHERE id=?", (stale,)).fetchone()
            s2 = conn.execute("SELECT status FROM social_posts WHERE id=?",
                              (fresh,)).fetchone()
            conn.close()
        self.assertEqual(len(captured), 1)
        self.assertEqual(s1["status"], "published")
        self.assertTrue(s1["delivered_at"])
        self.assertEqual(s1["deliver_attempts"], 2)
        self.assertEqual(s2["status"], "publishing")  # still owned by its live flush

    def test_recover_stuck_social_exhausts_attempts_without_redelivery(self):
        """A post past the redelivery cap settles as published instead of being
        re-sent every restart — bound the duplicate, never loop forever."""
        pid = self._insert_scheduled_post(slug="keto-snacks")
        with server._lock:
            conn = server._db()
            conn.execute(
                "UPDATE social_posts SET status='publishing', claimed_at=?, "
                "deliver_attempts=9 WHERE id=?",
                ("2000-01-01 00:00:00", pid))
            conn.commit()
            conn.close()
        captured = []
        saved_hook = server._webhook_fire
        server._webhook_fire = lambda kits: captured.extend(kits)
        try:
            n = server._recover_stuck_social()
        finally:
            server._webhook_fire = saved_hook
        self.assertEqual(n, 1)
        self.assertEqual(captured, [])
        with server._lock:
            conn = server._db()
            s = conn.execute("SELECT status, delivered_at, deliver_attempts "
                             "FROM social_posts WHERE id=?", (pid,)).fetchone()
            conn.close()
        self.assertEqual(s["status"], "published")
        self.assertEqual(s["delivered_at"], "")
        self.assertEqual(s["deliver_attempts"], 9)

    def test_recover_stuck_social_defers_to_live_flush(self):
        """Recovery never touches rows a live flush currently owns."""
        pid = self._insert_scheduled_post(slug="keto-snacks")
        with server._lock:
            conn = server._db()
            conn.execute("UPDATE social_posts SET status='publishing', claimed_at=?, "
                         "deliver_attempts=1 WHERE id=?", ("2000-01-01 00:00:00", pid))
            conn.commit()
            conn.close()
        server._SOCIAL_FLUSH_ACTIVE[0] += 1
        try:
            n = server._recover_stuck_social()
        finally:
            server._SOCIAL_FLUSH_ACTIVE[0] -= 1
        self.assertEqual(n, 0)
        with server._lock:
            conn = server._db()
            s = conn.execute("SELECT status FROM social_posts WHERE id=?",
                             (pid,)).fetchone()
            conn.close()
        self.assertEqual(s["status"], "publishing")

    def test_recover_outbox_stuck_requeues_or_discards(self):
        """Outbox rows orphaned in 'sending' by a crash go back to 'scheduled'
        until the send-attempt cap, then settle as 'done' (stalled) instead of
        looping forever."""
        import datetime as _dt
        with server._lock:
            conn = server._db()
            conn.execute("DELETE FROM outbox")
            cur = conn.execute(
                "INSERT INTO outbox (spec, recipients, status, claimed_at, attempts) "
                "VALUES ('{\"subject\":\"recover me\"}', '[\"a@x\"]', 'sending', "
                "? , 2)", ("2000-01-01 00:00:00",))
            requeue_id = cur.lastrowid
            cur2 = conn.execute(
                "INSERT INTO outbox (spec, recipients, status, claimed_at, attempts) "
                "VALUES ('{\"subject\":\"hopeless\"}', '[\"b@x\"]', 'sending', "
                "? , 5)", ("2000-01-01 00:00:00",))
            dead_id = cur2.lastrowid
            conn.commit()
            conn.close()
        n = server._recover_outbox_stuck()
        self.assertEqual(n, 2)
        with server._lock:
            conn = server._db()
            a = conn.execute("SELECT status FROM outbox WHERE id=?", (requeue_id,)).fetchone()
            b = conn.execute("SELECT status, result FROM outbox WHERE id=?",
                             (dead_id,)).fetchone()
            conn.close()
        self.assertEqual(a["status"], "scheduled")
        self.assertEqual(b["status"], "done")
        self.assertIn("stalled", b["result"])

    # ------------------------------------------------------------- referrals
    def _subscribe_via(self, email, body_extra=None):
        body = {"email": email, "keyword": "keto snacks", "source": "niche"}
        body.update(body_extra or {})
        st, _, _, data = self._raw("/subscribe", "POST", body=json.dumps(body))
        self.assertEqual(st, 200)
        return json.loads(data)

    def test_referral_link_credits_referrer_and_rewards_once(self):
        """A fresh lead that lands on the shared ?ref= link credits the referrer
        and fires the (deduped) reward note — the loop the old courier silently
        dropped on /subscribe."""
        with server._lock:
            conn = server._db()
            conn.execute("DELETE FROM subscribers WHERE email IN "
                         "('ref@x.test','friend1@x.test','friend2@x.test','refb@x.test','selfy@x.test')")
            conn.commit()
            conn.close()
        saved_cfg = mailer.configured
        mailer.configured = lambda: True
        sent = []
        saved_send = mailer.send
        mailer.send = (lambda subject, text, to, *a, **k:
                       (sent.append((subject, to)) or True))
        try:
            ref = self._subscribe_via("ref@x.test")
            self.assertTrue(ref["ok"])
            with server._lock:
                conn = server._db()
                token = conn.execute("SELECT ref_token FROM subscribers WHERE email='ref@x.test'"
                                     ).fetchone()["ref_token"]
                conn.close()
            self.assertTrue(token)
            friend = self._subscribe_via("friend1@x.test", {"ref": token})
            self.assertTrue(friend["ok"])
            self.assertNotEqual(friend.get("referral_url", ""), "")
            with server._lock:
                conn = server._db()
                fb = conn.execute("SELECT referred_by FROM subscribers WHERE email='friend1@x.test'"
                                  ).fetchone()
                cred = conn.execute("SELECT referrals FROM subscribers WHERE email='ref@x.test'"
                                    ).fetchone()
                conn.close()
            self.assertEqual(fb["referred_by"], token)
            self.assertEqual(cred["referrals"], 1)
            for _ in range(40):  # the reward fires on a background thread
                if any("reward" in s for s, _ in sent):
                    break
                import time
                time.sleep(0.05)
            rewards = [to for s, to in sent if "reward" in s]
            self.assertEqual(rewards, ["ref@x.test"])
            # second friend: credit again, but the reward stays deduped (one ever)
            friend2 = self._subscribe_via("friend2@x.test", {"ref": token})
            self.assertTrue(friend2["ok"])
            with server._lock:
                conn = server._db()
                cred2 = conn.execute("SELECT referrals FROM subscribers WHERE email='ref@x.test'"
                                     ).fetchone()
                conn.close()
            self.assertEqual(cred2["referrals"], 2)
            self.assertEqual(len(rewards), 1)
        finally:
            mailer.configured = saved_cfg
            mailer.send = saved_send

    def test_referral_never_credits_self_or_override_first(self):
        """Share links don't credit yourself, and an already-attributed lead
        can't be re-credited by a different referrer (first attribution wins)."""
        with server._lock:
            conn = server._db()
            conn.execute("DELETE FROM subscribers WHERE email IN "
                         "('selfy@x.test','taken@x.test','refb@x.test')")
            conn.commit()
            conn.close()
        self._subscribe_via("selfy@x.test")
        self._subscribe_via("refb@x.test")
        with server._lock:
            conn = server._db()
            self_tok = conn.execute("SELECT ref_token, id FROM subscribers WHERE email='selfy@x.test'"
                                    ).fetchone()
            refb_tok = conn.execute("SELECT ref_token FROM subscribers WHERE email='refb@x.test'"
                                    ).fetchone()["ref_token"]
            conn.close()
        # credible referrer signs up 'taken@x.test'
        self._subscribe_via("taken@x.test", {"ref": refb_tok})
        with server._lock:
            conn = server._db()
            take = conn.execute("SELECT referred_by FROM subscribers WHERE email='taken@x.test'"
                                ).fetchone()
            t = conn.execute("SELECT referrals FROM subscribers WHERE email='refb@x.test'"
                             ).fetchone()
            conn.close()
        self.assertEqual(take["referred_by"], refb_tok)
        self.assertEqual(t["referrals"], 1)
        # 'taken@x.test' re-subscribes with a NEW referrer's token: first wins, no change
        self._subscribe_via("taken@x.test", {"ref": self_tok["ref_token"]})
        with server._lock:
            conn = server._db()
            take2 = conn.execute("SELECT referred_by FROM subscribers WHERE email='taken@x.test'"
                                 ).fetchone()
            t2 = conn.execute("SELECT referrals FROM subscribers WHERE email='refb@x.test'"
                              ).fetchone()
            self_cred = conn.execute("SELECT referrals FROM subscribers WHERE id=?",
                                     (self_tok["id"],)).fetchone()
            conn.close()
        self.assertEqual(take2["referred_by"], refb_tok)  # still the original
        self.assertEqual(t2["referrals"], 1)              # no double credit
        self.assertEqual(self_cred["referrals"], 0)       # own token proves nothing


if __name__ == "__main__":
    unittest.main()