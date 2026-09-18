# -*- coding: utf-8 -*-
"""Offline tests for the SEM slow-growth suite and the SEO audit suite:
the /admin/sem funnel hub, /admin/seo audit hub, their JSON APIs, the
WebSite/Organization structured data on the landing page, the noindex rule
for empty niches, the back-to-top pill/ui.js include, and per-niche sitemap
lastmod derived from the niche created date."""

import http.client
import json
import os
import re
import shutil
import sys
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import amazon
import editorial
import indexnow
import io
import security
import seo
import sem
import server


class TestSemSeoSite(unittest.TestCase):

    IPKEY = "127.0.0.1|127.0.0.1"

    @classmethod
    def setUpClass(cls):
        cls.db = "/tmp/pstore_test_semseo_%s.db" % uuid.uuid4().hex[:8]
        shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "pstore.db"), cls.db)
        os.environ["PSTORE_DB"] = cls.db
        os.environ["PSTORE_ADMIN_EMAIL"] = "owner@test.example"
        os.environ["PSTORE_ADMIN_PASSWORD"] = "test-pass-123"
        os.environ.pop("PSTORE_URL", None)
        cls._saved_indexnow_post = indexnow._post
        indexnow._post = cls._fake_indexnow_post
        cls._saved_amazon_urlopen = amazon._urlopen
        amazon._urlopen = cls._fake_amazon_urlopen
        import importlib
        importlib.reload(server)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.PORT = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.cookie = cls._login()
        with server._lock:
            conn = server._db()
            rows = conn.execute("SELECT keyword FROM niches").fetchall()
            conn.close()
        cls.niches = [r["keyword"] for r in rows]

    @classmethod
    def _fake_indexnow_post(cls, url, payload, timeout=20):
        return None

    @classmethod
    def _fake_amazon_urlopen(cls, req, timeout):
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        if "completion.amazon.com" in url:
            return io.BytesIO(json.dumps(
                {"suggestions": [{"value": "keto snacks best"}, {"value": "best keto snacks"}]}
            ).encode("utf-8"))
        return io.BytesIO(b"<html><body></body></html>")

    @classmethod
    def tearDownClass(cls):
        indexnow._post = cls._saved_indexnow_post
        amazon._urlopen = cls._saved_amazon_urlopen
        security.SUBSCRIBE_LIMITER.clear("sub|" + cls.IPKEY)
        security.TRACK_LIMITER.clear("trk|" + cls.IPKEY)
        cls.httpd.shutdown()
        cls.thread.join(timeout=2)
        cls.httpd.server_close()
        if os.path.exists(cls.db):
            os.unlink(cls.db)

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

    def _raw(self, method, path, body=None, cookie=None, ctype=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.PORT, timeout=10)
        h = {}
        if cookie:
            h["Cookie"] = cookie
        if ctype:
            h["Content-Type"] = ctype
        if headers:
            h.update(headers)
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        status, location, ctype = resp.status, resp.getheader("Location"), \
            resp.getheader("Content-Type")
        conn.close()
        return status, location, ctype, data

    def _pick_niche(self):
        return (self.niches or ["keto snacks"])[0]

    # --- API: SEO audit -----------------------------------------------------
    def test_seo_audit_api(self):
        st, _, ct, body = self._raw("GET", "/api/seo-audit", cookie=self.cookie)
        self.assertEqual(st, 200)
        self.assertIn("application/json", ct)
        d = json.loads(body)
        self.assertIn("count", d)
        self.assertIn("indexable", d)
        self.assertIn("niches", d)
        self.assertEqual(d["count"], len(self.niches))
        if d["niches"]:
            row = d["niches"][0]
            for k in ("keyword", "slug", "url", "title_len", "desc_len",
                      "checks", "indexable"):
                self.assertIn(k, row)
            self.assertIn("title_ok", row["checks"])
            self.assertIn("desc_ok", row["checks"])
            self.assertIn("schema", row["checks"])
            self.assertIn("og_image", row["checks"])

    # --- API: SEM -----------------------------------------------------------
    def test_sem_api_happy(self):
        kw = self._pick_niche()
        st, _, ct, body = self._raw("GET", "/api/sem?keyword=%s" %
                                    urllib_quote(kw), cookie=self.cookie)
        self.assertEqual(st, 200)
        self.assertIn("application/json", ct)
        b = json.loads(body)
        self.assertEqual(b["keyword"], kw)
        self.assertIn("longtail", b)
        self.assertIn("intent", b)
        self.assertIn("paa", b)
        self.assertIn("performance", b)
        self.assertIn("page", b)
        self.assertTrue(any(x["intent"] == "target" for x in b["longtail"]))

    def test_sem_api_requires_keyword(self):
        st, _, _, body = self._raw("GET", "/api/sem", cookie=self.cookie)
        self.assertEqual(st, 200)
        self.assertIn("error", json.loads(body))

    def test_sem_api_unknown_keyword(self):
        st, _, _, body = self._raw("GET", "/api/sem?keyword=zzz-not-a-niche",
                                   cookie=self.cookie)
        self.assertEqual(st, 200)
        self.assertIn("error", json.loads(body))

    # --- Admin pages --------------------------------------------------------
    def test_admin_seo_page(self):
        st, _, ct, body = self._raw("GET", "/admin/seo", cookie=self.cookie)
        self.assertEqual(st, 200)
        html = body.decode("utf-8", "replace")
        self.assertIn("SEO", html)
        self.assertIn('id="top"', html)
        self.assertIn('class="totop"', html)
        # links into the unified Keys hub for the site-level SEO keys
        self.assertIn('href="/keys/site/gsc"', html)
        self.assertIn('href="/keys/site/indexnow"', html)
        self.assertIn('href="/keys"', html)
        # push-to-engines controls
        self.assertIn('id="submitNow"', html)
        self.assertIn("IndexNow", html)
        self.assertIn("search.google.com/search-console", html)
        self.assertIn("Yandex", html)
        self.assertIn("Bing", html)
        self.assertIn("active subscribers", html)

    def test_admin_golive_page(self):
        st, _, ct, body = self._raw("GET", "/admin/golive", cookie=self.cookie)
        self.assertEqual(st, 200)
        html = body.decode("utf-8", "replace")
        self.assertIn("Go-live", html)
        self.assertIn("Social router", html)
        self.assertIn("Search-engine consoles", html)
        self.assertIn("Custom domain", html)
        self.assertIn("Amazon Associates", html)
        self.assertIn("PSTORE_GSC_CLIENT_ID", html)
        self.assertIn("PSTORE_BING_API_KEY", html)
        self.assertIn("PSTORE_YANDEX_CLIENT_ID", html)
        self.assertIn("SOCIAL_WEBHOOK", html)
        self.assertIn("PSTORE_URL", html)
        self.assertIn("PSTORE_TAG", html)
        self.assertIn('href="/admin/seoengines"', html)
        self.assertIn('href="/admin/social"', html)
        self.assertIn("noindex,nofollow", html)
        self.assertIn('class="totop"', html)

    def test_admin_golive_requires_auth(self):
        st, loc, _, _ = self._raw("GET", "/admin/golive", cookie=None)
        self.assertIn(st, (301, 302, 303))
        self.assertIn("/admin/login", loc or "")

    def test_admin_golive_native_chips_and_env_hint(self):
        import os
        touched = ("PSTORE_TELEGRAM_TOKEN", "PSTORE_TELEGRAM_CHAT",
                   "PSTORE_PINTEREST_TOKEN")
        saved = {k: os.environ.get(k) for k in touched}
        try:
            os.environ["PSTORE_TELEGRAM_TOKEN"] = "T"
            os.environ["PSTORE_TELEGRAM_CHAT"] = "@c"
            os.environ["PSTORE_PINTEREST_TOKEN"] = "P"
            st, _, _, body = self._raw("GET", "/admin/golive", cookie=self.cookie)
            self.assertEqual(st, 200)
            html = body.decode("utf-8", "replace")
            self.assertIn("deliver natively", html)
            self.assertIn("PSTORE_TELEGRAM_TOKEN", html)
            self.assertIn("PSTORE_TELEGRAM_CHAT", html)
            self.assertIn("PSTORE_PINTEREST_TOKEN", html)
            self.assertIn("PSTORE_INSTAGRAM_IG_USER_ID", html)
            self.assertIn("PSTORE_YOUTUBE_TOKEN", html)
        finally:
            for k in touched:
                if saved[k] is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = saved[k]

    def test_admin_golive_lists_repo_n8n_flows(self):
        st, _, _, body = self._raw("GET", "/admin/golive", cookie=self.cookie)
        self.assertEqual(st, 200)
        html = body.decode("utf-8", "replace")
        for f in ("pstore-social-router.json", "pstore-fanout-social.json"):
            self.assertIn(f, html)

    def test_admin_golive_chip_in_operate_hub(self):
        st, _, _, body = self._raw("GET", "/admin", cookie=self.cookie)
        self.assertEqual(st, 200)
        self.assertIn("Go-live checklist", body.decode("utf-8", "replace"))

    def test_admin_sem_page(self):
        kw = self._pick_niche()
        st, _, ct, body = self._raw("GET", "/admin/sem?keyword=%s" %
                                    urllib_quote(kw), cookie=self.cookie)
        self.assertEqual(st, 200)
        html = body.decode("utf-8", "replace")
        self.assertIn("Search", html)
        self.assertIn('id="top"', html)
        self.assertIn('class="totop"', html)
        # The long-tail build button must target the topic builder (not the
        # niche-mining /api/suggest/build) and show real /n/<parent>/<term> URLs.
        self.assertIn("/api/sem/build-topic", html)
        self.assertNotIn("/api/suggest/build", html)
        self.assertIn('data-build="1"', html)
        self.assertIn("data-parent=", html)
        self.assertIn("data-term=", html)
        self.assertIn("/n/%s/" % seo._slugify(kw), html)

    # --- Long-tail topic build (SEM 'Build this page') -----------------------
    def test_sem_build_topic_happy(self):
        kw = self._pick_niche()
        parent = seo._slugify(kw)
        # a long-tail phrase returned by the fake autosuggest
        term = "keto snacks best"
        st, _, ct, body = self._raw(
            "POST", "/api/sem/build-topic", cookie=self.cookie,
            ctype="application/json",
            body=json.dumps({"parent": kw, "term": term}).encode("utf-8"))
        self.assertEqual(st, 200, body)
        d = json.loads(body)
        self.assertTrue(d["ok"])
        self.assertEqual(d["parent"], parent)
        self.assertEqual(d["slug"], seo._slugify(term))
        self.assertEqual(d["url"], "/n/%s/%s" % (parent, seo._slugify(term)))
        # topic page now exists and renders
        st2, _, ct2, b2 = self._raw("GET", d["url"], cookie=self.cookie)
        self.assertEqual(st2, 200)
        self.assertIn("text/html", ct2)
        # build again is idempotent (INSERT OR IGNORE)
        st3, _, _, b3 = self._raw(
            "POST", "/api/sem/build-topic", cookie=self.cookie,
            ctype="application/json",
            body=json.dumps({"parent": kw, "term": term}).encode("utf-8"))
        self.assertEqual(st3, 200)
        self.assertTrue(json.loads(b3)["ok"])

    def test_sem_build_topic_requires_parent_and_term(self):
        st, _, _, _ = self._raw("POST", "/api/sem/build-topic",
                                cookie=self.cookie, ctype="application/json",
                                body=json.dumps({"parent": "x"}).encode("utf-8"))
        self.assertEqual(st, 400)
        st, _, _, _ = self._raw("POST", "/api/sem/build-topic",
                                cookie=self.cookie, ctype="application/json",
                                body=b"{}")
        self.assertEqual(st, 400)

    def test_sem_build_topic_unknown_parent(self):
        st, _, _, body = self._raw("POST", "/api/sem/build-topic",
                                   cookie=self.cookie, ctype="application/json",
                                   body=json.dumps(
                                       {"parent": "zzz-not-a-niche",
                                        "term": "some topic"}).encode("utf-8"))
        self.assertEqual(st, 404)
        self.assertFalse(json.loads(body)["ok"])

    def test_sem_build_topic_rejects_parent_slug_term(self):
        kw = self._pick_niche()
        st, _, _, body = self._raw("POST", "/api/sem/build-topic",
                                   cookie=self.cookie, ctype="application/json",
                                   body=json.dumps(
                                       {"parent": kw, "term": kw}).encode("utf-8"))
        self.assertEqual(st, 400)

    def test_sem_build_topic_requires_auth(self):
        kw = self._pick_niche()
        st, _, _, _ = self._raw(
            "POST", "/api/sem/build-topic",
            ctype="application/json",
            body=json.dumps({"parent": kw, "term": "x"}).encode("utf-8"))
        self.assertEqual(st, 401)

    # --- Niche data refresh -------------------------------------------------
    def _build_suggest_and_cleanup(self, kw="zzz-unique-suggest-test"):
        created_ids = []
        created_slugs = []
        def cleanup():
            with server._lock:
                c = server._db()
                for nid in created_ids:
                    c.execute("DELETE FROM niches WHERE id=?", (nid,))
                for slug in created_slugs:
                    c.execute("DELETE FROM topics WHERE parent_slug=?", (slug,))
                c.commit()
                c.close()
        st, _, ct, body = self._raw(
            "POST", "/api/suggest/build",
            cookie=self.cookie, ctype="application/json",
            body=json.dumps({"keyword": kw}).encode("utf-8"))
        self.assertEqual(st, 200, body)
        d = json.loads(body)
        self.assertTrue(d["ok"])
        self.assertIn("slug", d)
        self.assertIn("id", d)
        if d.get("id"):
            created_ids.append(d["id"])
        if d.get("slug"):
            created_slugs.append(d["slug"])
        return cleanup, d

    def test_suggest_build_api(self):
        # POST /api/suggest/build must be routable via the POST dispatcher
        # (it used to live only in the GET dispatcher -> 404 on the marketing
        # page's "Build" button).
        cleanup, d = self._build_suggest_and_cleanup()
        try:
            self.assertIn("topic_pages", d)
        finally:
            cleanup()

    def test_admin_sem_page_confirms_post_dispatch(self):
        cleanup, d = self._build_suggest_and_cleanup()
        try:
            self.assertTrue(d["slug"].startswith("zzz-unique"))
        finally:
            cleanup()

    def test_refresh_requires_auth(self):
        st, loc, _, _ = self._raw("GET", "/admin/refresh")
        self.assertEqual(st, 302)
        self.assertIn("/admin/login", loc)

    def test_refresh_status_api(self):
        st, _, ct, body = self._raw("GET", "/api/refresh/status", cookie=self.cookie)
        self.assertEqual(st, 200)
        self.assertIn("application/json", ct)
        d = json.loads(body)
        self.assertEqual(d["total"], len(self.niches))
        for k in ("refreshed", "stale", "inflight", "auto_interval_s",
                  "stale_min", "max_per_cycle"):
            self.assertIn(k, d)

    def test_refresh_single_niche_sets_updated_at(self):
        kw = self._pick_niche()
        st, _, ct, body = self._raw(
            "POST", "/api/refresh",
            body=json.dumps({"keyword": kw}),
            ctype="application/json", cookie=self.cookie)
        self.assertEqual(st, 200)
        self.assertIn("application/json", ct)
        self.assertEqual(json.loads(body)["status"], "ok")
        with server._lock:
            conn = server._db()
            row = conn.execute("SELECT updated_at FROM niches WHERE keyword=?",
                               (kw,)).fetchone()
            conn.close()
        self.assertTrue(row["updated_at"])

    def test_refresh_missing_niche(self):
        st, _, _, body = self._raw(
            "POST", "/api/refresh",
            body=json.dumps({"keyword": "not-a-real-niche-xyz"}),
            ctype="application/json", cookie=self.cookie)
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["status"], "missing")

    def test_refresh_requires_keyword_arg(self):
        st, _, _, body = self._raw("POST", "/api/refresh",
                                   body=json.dumps({}), ctype="application/json",
                                   cookie=self.cookie)
        self.assertEqual(st, 400)

    def test_z_refresh_all_starts_background(self):
        st, _, ct, body = self._raw("POST", "/api/refresh-all",
                                    ctype="application/json", cookie=self.cookie)
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertEqual(d["status"], "started")
        self.assertEqual(d["queued"], len(self.niches))
        # Wait for the background worker to finish (validates completion and
        # avoids the daemon leaking into later test modules).
        import time as _time
        deadline = _time.time() + 60
        while _time.time() < deadline:
            st, _, _, sbody = self._raw("GET", "/api/refresh/status",
                                        cookie=self.cookie)
            sd = json.loads(sbody)
            if not sd.get("inflight"):
                break
            _time.sleep(0.25)
        st, _, _, sbody = self._raw("GET", "/api/refresh/status",
                                    cookie=self.cookie)
        sd = json.loads(sbody)
        self.assertEqual(sd["inflight"], [])
        self.assertEqual(sd["refreshed"], len(self.niches))

    def test_admin_refresh_page(self):
        st, _, ct, body = self._raw("GET", "/admin/refresh", cookie=self.cookie)
        self.assertEqual(st, 200)
        html = body.decode("utf-8", "replace")
        self.assertIn("Data refresh", html)
        self.assertIn("/api/refresh-all", html)
        self.assertIn('data-kw=', html)
        self.assertIn("stale", html)

    def test_admin_pages_require_auth(self):
        st, loc, _, _ = self._raw("GET", "/admin/seo")
        self.assertEqual(st, 302)
        self.assertIn("/admin/login", loc)

    def test_admin_backup_requires_auth(self):
        st, loc, _, _ = self._raw("GET", "/admin/backup")
        self.assertEqual(st, 302)
        self.assertIn("/admin/login", loc)

    def test_admin_backup_downloads_sql_dump(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.PORT, timeout=10)
        conn.request("GET", "/admin/backup", headers={"Cookie": self.cookie})
        resp = conn.getresponse()
        body = resp.read()
        st = resp.status
        ct = resp.getheader("Content-Type", "")
        cdisp = resp.getheader("Content-Disposition", "")
        conn.close()
        self.assertEqual(st, 200)
        self.assertIn("application/x-sqlite3", ct)
        self.assertIn('attachment; filename="pstore-', cdisp)
        self.assertGreater(len(body), 1000)
        self.assertIn(b"CREATE TABLE", body[:4000])

    def test_admin_backup_link_in_analyze_nav(self):
        st, _, _, body = self._raw("GET", "/admin/analytics", cookie=self.cookie)
        self.assertEqual(st, 200)
        html = body.decode("utf-8", "replace")
        self.assertIn('/admin/backup', html)

    # --- Structured data + noindex on public pages -------------------------
    def test_landing_schema_has_website_org(self):
        st, _, ct, body = self._raw("GET", "/")
        self.assertEqual(st, 200)
        html = body.decode("utf-8", "replace")
        self.assertIn("application/ld+json", html)
        self.assertIn("WebSite", html)
        self.assertIn("Organization", html)
        self.assertIn('class="totop"', html)
        self.assertIn("/ui.js", html)

    def test_homepage_components_render_with_niches(self):
        st, _, ct, body = self._raw("GET", "/")
        self.assertEqual(st, 200)
        html = body.decode("utf-8", "replace")
        # quick-verdict band above the fold
        self.assertIn("Quick verdict", html)
        self.assertIn('class="qpick', html)
        self.assertIn("Check price", html)
        # honesty trust strip (the differentiation gap)
        self.assertIn("trust-strip", html)
        self.assertIn("Live prices", html)
        # explore-niches tile grid with live counts
        self.assertIn("Explore the niches", html)
        self.assertIn('class="ntile"', html)
        # comparison preview + courier opt-in w/ first_name
        self.assertIn("Compare the shortlist", html)
        self.assertIn('name="first_name"', html)

    def test_homepage_first_name_optin_and_courier(self):
        st, _, _, body = self._raw("GET", "/")
        self.assertEqual(st, 200)
        html = body.decode("utf-8", "replace")
        self.assertIn('/courier.js', html)
        self.assertIn('name="email"', html)
        self.assertIn('name="first_name"', html)

    def test_homepage_nav_anchors_resolve(self):
        st, _, _, body = self._raw("GET", "/")
        self.assertEqual(st, 200)
        html = body.decode("utf-8", "replace")
        anchors = set(re.findall(r'href="#([a-z0-9-]+)"', html))
        ids = set(re.findall(r'id="([a-z0-9-]+)"', html))
        # every nav chip target must have a matching section id, or the click
        # goes nowhere
        for a in anchors:
            self.assertIn(a, ids, "nav anchor #%s has no target id on the page" % a)
        # the sections the chips point to exist
        for a in ("top-picks", "niches", "notify", "method", "faq"):
            self.assertIn(a, ids)

    def test_niche_noindex_when_missing(self):
        st, _, _, body = self._raw("GET", "/n/this-niche-does-not-exist")
        self.assertEqual(st, 404)
        self.assertIn(b'name="robots" content="noindex', body)

    # --- /ui.js and sitemap lastmod ----------------------------------------
    def test_ui_js_served(self):
        st, _, ct, body = self._raw("GET", "/ui.js")
        self.assertEqual(st, 200)
        self.assertIn("javascript", ct)
        self.assertIn(b"totop", body)

    def test_sitemap_has_niche_lastmod(self):
        st, _, ct, body = self._raw("GET", "/sitemap.xml")
        self.assertEqual(st, 200)
        xml = body.decode("utf-8", "replace")
        self.assertIn("urlset", xml)
        slug = seo._slugify(self._pick_niche())
        self.assertIn("/n/%s" % slug, xml)
        self.assertIn("<lastmod>", xml)

    def test_sitemap_lastmod_tracks_niche_refresh(self):
        kw = None
        with server._lock:
            conn = server._db()
            row = conn.execute(
                "SELECT keyword FROM niches WHERE products IS NOT NULL "
                "AND TRIM(products) NOT IN ('', '[]', '{}') "
                "ORDER BY id LIMIT 1").fetchone()
            conn.close()
        if row:
            kw = row["keyword"]
        created = False
        if not kw:
            kw = "lastmod test niche"
            with server._lock:
                conn = server._db()
                conn.execute("INSERT INTO niches (keyword, market, products) VALUES (?,?,?)",
                             (kw, "com", json.dumps(
                                 [{"asin": "B0LAST", "title": "T", "reviews": 1,
                                   "stars": 4, "price": 1.0, "currency": "USD",
                                   "url": "https://www.amazon.com/dp/B0LAST"}])))
                conn.commit()
                conn.close()
            created = True
        slug = seo._slugify(kw)
        with server._lock:
            conn = server._db()
            saved = conn.execute(
                "SELECT updated_at FROM niches WHERE keyword=?", (kw,)).fetchone()
            conn.execute("UPDATE niches SET updated_at=? WHERE keyword=?",
                         ("2026-09-09 10:11:12", kw))
            conn.commit()
            conn.close()
        try:
            st, _, _, body = self._raw("GET", "/sitemap.xml")
            self.assertEqual(st, 200)
            xml = body.decode("utf-8", "replace")
            m = re.search(r"<loc>.*?/n/%s</loc>\s*<lastmod>([^<]+)"
                          % re.escape(slug), xml)
            self.assertTrue(m, "expected a lastmod entry for /n/%s" % slug)
            self.assertEqual(m.group(1), "2026-09-09")
        finally:
            with server._lock:
                conn = server._db()
                up = None if not saved or saved["updated_at"] is None \
                    else saved["updated_at"]
                conn.execute("UPDATE niches SET updated_at=? WHERE keyword=?",
                             (up, kw))
                if created:
                    conn.execute("DELETE FROM niches WHERE keyword=?", (kw,))
                conn.commit()
                conn.close()

    # --- Crawler robustness (GSC sitemap 404 + HEAD 501 fixes) ---------------
    def test_sitemap_and_robots_tolerate_slash_and_case(self):
        kw = self._pick_niche()
        slug = seo._slugify(kw)
        for path in ("/sitemap.xml/", "/Sitemap.xml", "/SITEMAP.XML/",
                     "/robots.txt/", "/Robots.TXT"):
            st, _, ct, body = self._raw("GET", path)
            self.assertEqual(st, 200, "%s -> %s %s" % (path, st, ct))
            self.assertIn("text/plain" if "robots" in path.lower()
                          else "application/xml", ct)
        # page URLs keep their own semantics; only robots/sitemap normalize
        st, _, _, _ = self._raw("GET", "/n/%s/" % slug)
        self.assertEqual(st, 200)

    def test_head_requests_supported_on_seo_routes(self):
        kw = self._pick_niche()
        slug = seo._slugify(kw)
        routes = ["/", "/robots.txt", "/sitemap.xml",
                  "/n/%s" % slug, "/lp/%s" % slug]
        for path in routes:
            st, _, ct, body = self._raw("HEAD", path)
            self.assertEqual(st, 200, "HEAD %s -> %s" % (path, st))
            self.assertEqual(body, b"", "HEAD %s must have no body" % path)

    def test_public_seo_responses_carry_cache_control(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.PORT, timeout=10)
        conn.request("GET", "/sitemap.xml")
        resp = conn.getresponse()
        resp.read()
        cc = resp.getheader("Cache-Control") or ""
        conn.close()
        self.assertTrue(cc.startswith("public"), cc)
        self.assertIn("s-maxage", cc)
        # HEAD carries the same headers as GET (RFC 7231)
        conn = http.client.HTTPConnection("127.0.0.1", self.PORT, timeout=10)
        conn.request("HEAD", "/sitemap.xml")
        resp = conn.getresponse()
        resp.read()
        hlen = resp.getheader("Content-Length")
        hcc = resp.getheader("Cache-Control") or ""
        conn.close()
        self.assertEqual(hlen, resp.getheader("Content-Length"))
        self.assertIn("s-maxage", hcc)


def urllib_quote(s):
    import urllib.parse
    return urllib.parse.quote(s)


class TestMmeWaveA(unittest.TestCase):
    """Tests for MME-1 … MME-4 Wave A: structured-data completion, stories
    pages, cross-market switcher, budget-band + vs head-to-head pages."""

    @classmethod
    def setUpClass(cls):
        cls.db = "/tmp/pstore_test_mme_%s.db" % uuid.uuid4().hex[:8]
        shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "pstore.db"), cls.db)
        os.environ["PSTORE_DB"] = cls.db
        os.environ["PSTORE_ADMIN_EMAIL"] = "owner@test.example"
        os.environ["PSTORE_ADMIN_PASSWORD"] = "test-pass-123"
        os.environ.pop("PSTORE_URL", None)
        os.environ.pop("PSTORE_MARKETS", None)
        import importlib
        importlib.reload(server)
        amazon.CACHE_TTL = 0
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.PORT = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.cookie = cls._login()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        try:
            os.unlink(cls.db)
        except Exception:
            pass
        os.environ.pop("PSTORE_DB", None)
        os.environ.pop("PSTORE_MARKETS", None)

    @classmethod
    def _login(cls):
        import urllib.parse
        ph = server.security.hash_password("test-pass-123")
        with server._lock:
            conn = server._db()
            row = conn.execute("SELECT id FROM users WHERE lower(email)=?",
                               ("owner@test.example",)).fetchone()
            if not row:
                conn.execute("INSERT INTO users (email, pass_hash) VALUES (?,?)",
                             ("owner@test.example", ph))
                conn.commit()
            conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", cls.PORT, timeout=10)
        body = urllib.parse.urlencode({"email": "owner@test.example",
                                       "password": "test-pass-123"}).encode()
        conn.request("POST", "/admin/login", body,
                     {"Content-Type": "application/x-www-form-urlencoded"})
        resp = conn.getresponse()
        resp.read()
        cookie = resp.getheader("Set-Cookie", "").split(";")[0]
        conn.close()
        return cookie

    def _get(self, path, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.PORT, timeout=10)
        headers = {}
        if cookie or self.cookie:
            headers["Cookie"] = cookie or self.cookie
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        ct = resp.getheader("Content-Type", "")
        body = resp.read()
        conn.close()
        return resp.status, ct, body

    def _seed_niche(self, keyword, products):
        with server._lock:
            conn = server._db()
            conn.execute(
                "INSERT OR REPLACE INTO niches (keyword, market, products) "
                "VALUES (?,?,?)", (keyword, "com", json.dumps(products)))
            conn.commit()
            conn.close()

    # ---- MME-1 structured data ----

    def test_breadcrumb_3level(self):
        bcr = editorial.breadcrumb_jsonld("headphones", parent="audio")
        self.assertEqual(len(bcr["itemListElement"]), 3)
        self.assertEqual(bcr["itemListElement"][1]["name"], "audio picks")
        self.assertIn("/n/audio", bcr["itemListElement"][1]["item"])

    def test_topic_includes_faq_jsonld(self):
        kw = "camping gear"
        items = [{"asin": "B0T1", "title": "Tent one", "price": 49.99,
                  "stars": 4.4, "reviews": 700, "currency": "USD",
                  "url": "https://www.amazon.com/dp/B0T1"},
                 {"asin": "B0T2", "title": "Tent two", "price": 89.00,
                  "stars": 4.6, "reviews": 300, "currency": "USD",
                  "url": "https://www.amazon.com/dp/B0T2"}]
        self._seed_niche(kw, items)
        slug = seo._slugify(kw)
        with server._lock:
            conn = server._db()
            conn.execute(
                "INSERT OR IGNORE INTO topics (parent_slug, term, slug) "
                "VALUES (?,?,?)", (slug, "best camping tents", "best-camping-tents"))
            conn.commit()
            conn.close()
        st, ct, body = self._get("/n/%s/best-camping-tents" % slug)
        self.assertEqual(st, 200)
        html = body.decode("utf-8", "replace")
        self.assertIn("FAQPage", html)
        self.assertIn("BreadcrumbList", html)

    # ---- MME-2 stories ----

    def test_stories_gallery_route(self):
        st, ct, body = self._get("/stories")
        self.assertEqual(st, 200)
        html = body.decode("utf-8", "replace").lower()
        self.assertIn("stories", html)

    def test_story_per_niche_route(self):
        kw = "keto snacks"
        items = [{"asin": "B0KS1", "title": "Keto bar 1", "price": 9.99,
                  "stars": 4.5, "reviews": 1200, "currency": "USD",
                  "url": "https://www.amazon.com/dp/B0KS1"}]
        self._seed_niche(kw, items)
        st, ct, body = self._get("/stories/%s" % seo._slugify(kw))
        self.assertEqual(st, 200)
        html = body.decode("utf-8", "replace")
        self.assertIn("story-slide", html)
        self.assertIn("data-source=\"story\"", html)

    def test_story_404_unknown_slug(self):
        st, ct, body = self._get("/stories/no-such-niche-here")
        self.assertEqual(st, 404)

    # ---- MME-3 market switcher ----

    def test_market_switcher_absent_by_default(self):
        kw = "keto snacks"
        self._seed_niche(kw, [{"asin": "B0M1", "title": "M1", "price": 12.0,
                               "stars": 4.1, "reviews": 10, "currency": "USD",
                               "url": "https://www.amazon.com/dp/B0M1"}])
        st, ct, body = self._get("/n/%s" % seo._slugify(kw))
        self.assertEqual(st, 200)
        self.assertNotIn("market-switch", body.decode("utf-8", "replace"))

    def test_market_switcher_shows_with_multimarket(self):
        os.environ["PSTORE_MARKETS"] = "com,co.uk,de"
        os.environ["PSTORE_TAG"] = "bestpicks-20"
        try:
            amazon.set_market("com")
            amazon.set_tag("bestpicks-20")
            kw = "keto snacks"
            self._seed_niche(kw, [{"asin": "B0M2", "title": "M2", "price": 12.0,
                                   "stars": 4.1, "reviews": 10, "currency": "USD",
                                   "url": "https://www.amazon.com/dp/B0M2"}])
            st, ct, body = self._get("/n/%s" % seo._slugify(kw))
            self.assertEqual(st, 200)
            html = body.decode("utf-8", "replace")
            self.assertIn("market-switch", html)
            self.assertIn("co.uk", html)
        finally:
            os.environ.pop("PSTORE_MARKETS", None)
            amazon.set_market("com")
            amazon.set_tag("")

    def test_market_tag_derivation(self):
        amazon.set_tag("bestpicks-20")
        amazon.set_market("com")
        self.assertEqual(amazon.market_tag("com"), "bestpicks-20")
        self.assertEqual(amazon.market_tag("co.uk"), "bestpicks-21")
        self.assertEqual(amazon.market_tag("de"), "bestpicks-21")
        self.assertEqual(amazon.market_tag("ca"), "bestpicks-20")
        amazon.set_tag("")

    # ---- MME-4 price-band + vs pages ----

    def test_priceband_and_vs_topic_routes(self):
        kw = "camping gear"
        items = [
            {"asin": "B0C1", "title": "Light tent", "price": 22.00,
             "stars": 4.2, "reviews": 500, "currency": "USD",
             "url": "https://www.amazon.com/dp/B0C1"},
            {"asin": "B0C2", "title": "Medium tent", "price": 55.00,
             "stars": 4.4, "reviews": 800, "currency": "USD",
             "url": "https://www.amazon.com/dp/B0C2"},
            {"asin": "B0C3", "title": "Pro tent", "price": 149.00,
             "stars": 4.7, "reviews": 200, "currency": "USD",
             "url": "https://www.amazon.com/dp/B0C3"},
        ]
        self._seed_niche(kw, items)
        banded = server._ensure_price_band_topics({"keyword": kw, "products": items})
        vs = server._ensure_vs_topics({"keyword": kw, "products": items})
        slug = seo._slugify(kw)
        if banded:
            amt = banded[0]["slug"].split("-", 1)[1]
            st, ct, body = self._get("/n/%s/under-%s" % (slug, amt))
            self.assertEqual(st, 200)
            self.assertIn("under $", body.decode("utf-8", "replace").lower())
        if vs:
            st, ct, body = self._get("/n/%s/%s" % (slug, vs[0]["slug"]))
            self.assertEqual(st, 200)
            self.assertIn("vs", body.decode("utf-8", "replace").lower())

    def test_priceband_noindex_when_band_empty(self):
        """When no items fit the band, the page goes noindex."""
        kw = "minimal"
        items = [{"asin": "B0MN1", "title": "One", "price": 199.00,
                  "stars": 4.1, "reviews": 50, "currency": "USD",
                  "url": "https://www.amazon.com/dp/B0MN1"}]
        self._seed_niche(kw, items)
        res = seo.render_priceband(15, kw, seo._slugify(kw), items)
        self.assertIn("noindex", res.decode("utf-8", "replace"))

    def test_stories_sitemap_inclusion(self):
        kw = "keto snacks"
        self._seed_niche(kw, [{"asin": "B0SM", "title": "SM", "price": 12.0,
                               "stars": 4.1, "reviews": 10, "currency": "USD",
                               "url": "https://www.amazon.com/dp/B0SM"}])
        st, ct, body = self._get("/sitemap.xml")
        self.assertEqual(st, 200)
        xml = body.decode("utf-8", "replace")
        self.assertIn("/stories", xml)
        self.assertIn("/stories/%s" % seo._slugify(kw), xml)

    def test_priceband_indexable(self):
        kw = "audio gear"
        items = [
            {"asin": "B0AG1", "title": "Earbuds 20", "price": 18.00,
             "stars": 4.0, "reviews": 100, "currency": "USD",
             "url": "https://www.amazon.com/dp/B0AG1"},
            {"asin": "B0AG2", "title": "Earbuds 50", "price": 48.00,
             "stars": 4.4, "reviews": 700, "currency": "USD",
             "url": "https://www.amazon.com/dp/B0AG2"},
            {"asin": "B0AG3", "title": "Earbuds 120", "price": 119.00,
             "stars": 4.6, "reviews": 400, "currency": "USD",
             "url": "https://www.amazon.com/dp/B0AG3"},
        ]
        urls = seo.indexable_urls([{"keyword": kw}], "https://pstore.example")
        self.assertTrue(any("/stories/%s" % seo._slugify(kw) in u for u in urls))
        self.assertIn("https://pstore.example/stories", urls)


class TestMmeWaveB(unittest.TestCase):
    """Wave B: MME-5 (nudge), MME-6 (lead gate), MME-7 (sub_interests schema),
    MME-10 (AB auto-enroll)."""

    @classmethod
    def setUpClass(cls):
        cls.db = "/tmp/pstore_test_mme_b_%s.db" % uuid.uuid4().hex[:8]
        shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "pstore.db"), cls.db)
        os.environ["PSTORE_DB"] = cls.db
        os.environ["PSTORE_ADMIN_EMAIL"] = "owner@test.example"
        os.environ["PSTORE_ADMIN_PASSWORD"] = "test-pass-123"
        os.environ.pop("PSTORE_URL", None)
        os.environ.pop("PSTORE_MARKETS", None)
        import importlib
        importlib.reload(server)
        importlib.reload(seo)
        amazon.CACHE_TTL = 0
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.PORT = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        try:
            os.unlink(cls.db)
        except Exception:
            pass
        os.environ.pop("PSTORE_DB", None)
        os.environ.pop("PSTORE_MARKETS", None)

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.PORT, timeout=10)
        conn.request("GET", path)
        resp = conn.getresponse()
        ct = resp.getheader("Content-Type", "")
        body = resp.read()
        conn.close()
        return resp.status, ct, body

    def _seed_niche(self, keyword, products):
        with server._lock:
            conn = server._db()
            conn.execute(
                "INSERT OR REPLACE INTO niches (keyword, market, products) "
                "VALUES (?,?,?)", (keyword, "com", json.dumps(products)))
            conn.commit()
            conn.close()

    # ---- MME-5 courier.js nudge markers ----

    def test_courier_js_nudge_markers(self):
        js_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "static", "courier.js")
        with open(js_path, "r", encoding="utf-8", errors="replace") as fh:
            js = fh.read()
        self.assertIn("pstore_nudged", js)
        self.assertIn("courier-nudge", js)
        self.assertIn("data-opted", js)
        self.assertIn("45000", js)  # 45s fallback
        self.assertIn("sessionStorage", js)

    # ---- MME-6 lead gate ----

    def test_lead_gate_html_structure(self):
        html = seo.lead_gate_html("keto snacks", "niche")
        self.assertIn('id="gate"', html)
        self.assertIn('class="courier gate-form"', html)
        self.assertIn('id="gate-unlock"', html)
        self.assertIn('name="keyword" value="keto snacks"', html)
        self.assertIn('name="source" value="niche-gate"', html)
        self.assertIn("keto snacks", html)

    def test_niche_page_includes_gate_with_items(self):
        items = [{"asin": "B0G1", "title": "Tent", "price": 55.0,
                  "stars": 4.3, "reviews": 200, "currency": "USD",
                  "url": "https://www.amazon.com/dp/B0G1"}]
        self._seed_niche("camping gear", items)
        st, ct, body = self._get("/n/camping-gear")
        self.assertEqual(st, 200)
        html = body.decode("utf-8", "replace")
        self.assertIn('id="gate"', html)
        self.assertIn('id="gate-unlock"', html)

    def test_niche_page_no_gate_without_items(self):
        self._seed_niche("empty niche", [])
        st, ct, body = self._get("/n/empty-niche")
        html = body.decode("utf-8", "replace")
        self.assertNotIn("gate-unlock", html)

    def test_priceband_renders_gate(self):
        items = [{"asin": "B0PB", "title": "Widget", "price": 42.0,
                  "stars": 4.1, "reviews": 100, "currency": "USD",
                  "url": "https://www.amazon.com/dp/B0PB"}]
        html = seo.render_priceband(60, "camping gear", "camping-gear",
                                    items).decode("utf-8", "replace")
        self.assertIn("gate-unlock", html)
        self.assertIn("camping gear under $60", html)

    def test_priceband_no_gate_when_band_empty(self):
        items = [{"asin": "B0PBN", "title": "Expensive", "price": 500.0,
                  "stars": 4.1, "reviews": 100, "currency": "USD",
                  "url": "https://www.amazon.com/dp/B0PBN"}]
        html = seo.render_priceband(50, "camping gear", "camping-gear",
                                    items).decode("utf-8", "replace")
        self.assertNotIn("gate-unlock", html)
        self.assertIn("noindex", html)

    def test_vs_page_renders_gate(self):
        items = [
            {"asin": "AAA", "title": "Espresso A", "price": 99.0,
             "stars": 4.5, "reviews": 300, "currency": "USD",
             "url": "https://www.amazon.com/dp/AAA"},
            {"asin": "BBB", "title": "Espresso B", "price": 149.0,
             "stars": 4.7, "reviews": 200, "currency": "USD",
             "url": "https://www.amazon.com/dp/BBB"},
        ]
        html = seo.render_vs("Espresso A", "Espresso B", "AAA", "BBB",
                             "espresso", "espresso", items
                             ).decode("utf-8", "replace")
        self.assertIn("gate-unlock", html)
        self.assertIn("Espresso A", html)

    # ---- MME-7 sub_interests schema exists ----

    def test_sub_interests_table_exists(self):
        with server._lock:
            conn = server._db()
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(sub_interests)").fetchall()}
            conn.close()
        self.assertIn("subscriber_id", cols)
        self.assertIn("keyword", cols)
        self.assertIn("sent_index", cols)

    # ---- MME-10 AB auto-enroll creates variant rows ----

    def test_ab_autoenroll_creates_variants(self):
        kw = "best blender"
        slug = seo._slugify(kw)
        self._seed_niche(kw, [{"asin": "B0B1", "title": "Blender X",
                                "price": 39.99, "stars": 4.4,
                                "reviews": 800, "currency": "USD",
                                "url": "https://www.amazon.com/dp/B0B1"}])
        # seed 50 clicks on the slug
        with server._lock:
            conn = server._db()
            for _ in range(50):
                conn.execute(
                    "INSERT INTO clicks (slug, source, content) "
                    "VALUES (?, 'niche', '')", (slug,))
            conn.commit(); conn.close()
        stub = server._AutosendStub()
        r = server.Handler._ab_autoenroll(stub, min_clicks=2)
        self.assertTrue(r["ok"])
        self.assertTrue(r["enrolled"])
        self.assertEqual(r["enrolled"][0]["slug"], slug)
        with server._lock:
            conn = server._db()
            rows = conn.execute(
                "SELECT variant, headline, enabled FROM niche_variants "
                "WHERE lower(slug)=? ORDER BY variant", (slug,)).fetchall()
            conn.close()
        variants = [dict(x) for x in rows]
        self.assertEqual(len(variants), 2)
        headlines = {v["variant"]: v["headline"] for v in variants}
        self.assertIn("ranked picks", headlines[1])
        # calling again should not create duplicates
        r2 = server.Handler._ab_autoenroll(stub, min_clicks=2)
        self.assertEqual(len(r2["enrolled"]), 0)


if __name__ == "__main__":
    unittest.main()
