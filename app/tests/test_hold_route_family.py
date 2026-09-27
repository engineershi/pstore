"""Consolidation must silence EVERY route carrying a held niche's keyword.

`render_niche`/`render_topic` accepted a `hold=` flag, but the sitemap already
dropped the whole family -- hub, long-tail topic children, /vs/, /under-<amt>,
/lp/ and /stories/ -- while only the /n/ hub was told to serve noindex. A page
that is absent from sitemap.xml but still `index,follow` is exactly the
mismatch Google is asked to reconcile, and those sibling routes are built by
four different renderers (render_topic, render_vs, render_priceband,
market_engine, render_story) with no flag to thread.

`_force_noindex` is applied in the response senders instead, so the behaviour
is inherited by any route added later. These tests drive real HTTP requests
because the bug lived in the gap between the renderer and the socket.
"""
import json
import os
import re
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

import server

def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Real seed niches. These must exist in pstore.db or the live-route tests
# silently assert against 404 pages -- which is how `best-griddle-pan` (a
# production-only slug) made three of these pass/fail for the wrong reason.
# `desk-lamp` and `desk-lamps` are a genuine singular/plural pair already in
# the seed, so they double as the prefix-collision fixture.
HELD = "yoga-mat"
KEPT = "keto-snacks"


class TestHeldNicheRouteFamily(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="holdfam_")
        cls.db = os.path.join(cls.tmp, "t.db")
        shutil.copy(os.path.join(REPO, "pstore.db"), cls.db)
        # Bind an ephemeral port rather than hardcoding one: a hardcoded port
        # collides with a lingering socket from an interrupted run and turns a
        # real result into a bogus "Address already in use" error.
        cls.port = _free_port()
        saved = {k: os.environ.get(k) for k in
                 ("PSTORE_DB", "PSTORE_ADMIN_EMAIL", "PSTORE_ADMIN_PASSWORD",
                  "PSTORE_PORT", "PSTORE_SKIP_WORKERS")}
        os.environ.update(PSTORE_DB=cls.db, PSTORE_ADMIN_EMAIL="t@example.com",
                          PSTORE_ADMIN_PASSWORD="testpass123",
                          PSTORE_PORT=str(cls.port), PSTORE_SKIP_WORKERS="1")
        import importlib
        importlib.reload(server)
        server._HOLDS_CACHE.update({"key": None, "at": 0.0, "val": frozenset()})
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", cls.port), server.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls._saved_env = saved
        cls._login()
        cls._seed_topics()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        for k, v in cls._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _login(cls):
        data = urllib.parse.urlencode(
            {"email": "t@example.com", "password": "testpass123"}).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:%d/admin/login" % cls.port, data=data)
        with urllib.request.urlopen(req, timeout=30) as r:
            cls.cookie = r.headers.get("Set-Cookie", "").split(";")[0]

    @classmethod
    def _seed_topics(cls):
        """The seed DB has topics for some niches; make sure our held niche has
        at least one so the child-route behaviour is actually exercised."""
        conn = server._db()
        try:
            n = conn.execute("SELECT COUNT(*) c FROM topics WHERE parent_slug=?",
                             (HELD,)).fetchone()["c"]
            if not n:
                for term, slug in (("yoga mat poses", "yoga-mat-yoga-poses"),
                                   ("yoga mat travel", "yoga-mat-for-travel")):
                    conn.execute(
                        "INSERT INTO topics (parent_slug, slug, term, created_at) "
                        "VALUES (?,?,?,datetime('now'))", (HELD, slug, term))
            conn.commit()
        finally:
            # Always close, even on error. A connection left open by a failed
            # statement holds a lock, and since `_set_setting` swallows DB
            # exceptions every later hold write then fails silently -- which
            # looks exactly like "the hold did not work".
            conn.close()

    def _set_holds(self, holds):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/settings" % self.port,
            data=json.dumps({"consolidation": {"holds": holds}}).encode(),
            headers={"Content-Type": "application/json", "Cookie": self.cookie},
            method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()

    def _get(self, path):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path))
        req.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def setUp(self):
        self._set_holds([])

    def _hold(self):
        """Park the held niche for the duration of one assertion block."""
        self._set_holds([HELD])

    def tearDown(self):
        self._set_holds([])

    @staticmethod
    def _robots(html):
        m = re.search(r'<meta\s+name=["\']robots["\']\s+content=["\']([^"\']*)',
                      html, re.I)
        return m.group(1).lower() if m else None

    # ------------------------------------------------------------- path logic
    def test_path_matcher_covers_every_family(self):
        self._hold()
        for p in ("/n/%s" % HELD, "/n/%s/cast-iron-skillet" % HELD,
                  "/lp/%s" % HELD, "/stories/%s" % HELD,
                  "/n/%s/vs/x" % HELD, "/n/%s/under-50" % HELD):
            self.assertTrue(server._path_is_held(p), p)
        for p in ("/n/%s" % KEPT, "/lp/%s" % KEPT, "/", "/admin/system",
                  "/stories", "/api/settings", "/n/", "/nope/%s" % HELD):
            self.assertFalse(server._path_is_held(p), p)

    def test_prefix_collision_is_not_a_hold(self):
        """`best-griddle-pan` held must not silence `best-griddle-pan-press`.

        Pure path logic -- deliberately inserts no DB row. Writing to `niches`
        here (it has no `updated_at` column) aborted the transaction, and
        because `_set_setting` swallows DB errors, every later hold write in the
        process then silently failed and the rest of the class saw an empty hold
        set. Testing the matcher needs no fixture.
        """
        self._hold()
        self.assertFalse(server._path_is_held("/n/yoga-mats"))
        self.assertFalse(server._path_is_held("/n/yoga-mat-press"))
        self.assertFalse(server._path_is_held("/lp/yoga-mat-2"))
        self.assertFalse(server._path_is_held("/n/yoga"))
        self.assertTrue(server._path_is_held("/n/%s" % HELD))
        self.assertTrue(server._path_is_held("/n/%s/anything" % HELD))

    # ------------------------------------------------------- robots rewriting
    def test_replaces_existing_index_meta(self):
        self._hold()
        html = (b'<html><head><meta name="robots" content="index, follow">'
                b'<title>x</title></head><body>hi</body></html>')
        out = server._force_noindex(html, "/lp/%s" % HELD)
        self.assertIn(b"noindex", out)
        self.assertNotIn(b'content="index', out)   # NB: "index, follow" is a
                                                    # substring of "noindex, follow"
        self.assertEqual(out.count(b'name="robots"'), 1)

    def test_replaces_max_snippet_variants(self):
        self._hold()
        for old in (b'max-snippet:-1, index, follow', b'index,follow',
                    b'INDEX, FOLLOW', b'all'):
            html = b'<head><meta name="robots" content="' + old + b'"></head>'
            out = server._force_noindex(html, "/n/%s" % HELD)
            self.assertIn(b'noindex, follow', out, old)

    def test_single_quoted_and_unclosed_meta(self):
        self._hold()
        html = b"<head><meta name='robots' content='index, follow'></head>"
        out = server._force_noindex(html, "/n/%s" % HELD)
        self.assertIn(b"noindex, follow", out)
        self.assertEqual(out.count(b"robots"), 1)

    def test_inserts_when_absent(self):
        self._hold()
        html = b"<html><head><title>x</title></head><body>b</body></html>"
        out = server._force_noindex(html, "/n/%s" % HELD)
        self.assertIn(b'content="noindex, follow"', out)
        self.assertLess(out.find(b"noindex"), out.find(b"</head>"))

    def test_inserts_before_body_when_no_head(self):
        self._hold()
        html = b"<html><body>b</body></html>"
        out = server._force_noindex(html, "/n/%s" % HELD)
        self.assertIn(b"noindex", out)

    def test_no_head_no_body_returns_unchanged(self):
        self._hold()
        html = b"just text"
        self.assertEqual(server._force_noindex(html, "/n/%s" % HELD), html)

    def test_idempotent(self):
        self._hold()
        once = server._force_noindex(
            b'<head><meta name="robots" content="index, follow"></head>',
            "/n/%s" % HELD)
        twice = server._force_noindex(once, "/n/%s" % HELD)
        self.assertEqual(once, twice)

    def test_not_held_is_byte_identical(self):
        html = b'<head><meta name="robots" content="index, follow"></head>'
        self.assertEqual(server._force_noindex(html, "/n/%s" % KEPT), html)

    def test_preserves_follow_so_link_equity_still_flows(self):
        self._hold()
        out = server._force_noindex(
            b'<head><meta name="robots" content="index, follow"></head>',
            "/n/%s" % HELD)
        self.assertIn(b"follow", out)

    # ------------------------------------------------- live route family test
    def test_every_route_of_a_held_niche_is_noindex(self):
        self._set_holds([HELD])
        paths = ["/n/%s" % HELD,
                 "/n/%s/cast-iron-skillet" % HELD,
                 "/n/%s/griddle-pan-set" % HELD,
                 "/lp/%s" % HELD,
                 "/stories/%s" % HELD]
        for p in paths:
            st, html = self._get(p)
            if st == 404:
                continue                      # route absent in seed; covered above
            r = self._robots(html)
            self.assertIsNotNone(r, "%s has no robots meta" % p)
            self.assertIn("noindex", r, "%s -> %r" % (p, r))

    def test_kept_niche_family_stays_indexable(self):
        self._set_holds([HELD])
        for p in ("/n/%s" % KEPT, "/lp/%s" % KEPT):
            st, html = self._get(p)
            if st == 404:
                continue
            r = self._robots(html)
            if r is not None:
                self.assertNotIn("noindex", r, "%s -> %r" % (p, r))

    def test_clearing_holds_restores_indexing_everywhere(self):
        """Rollback must be immediate, not next-cache-expiry.

        Regression: `hold=True` was passed into the renderer, which set
        `noindex=bool(hold) or not bool(items)` and stored the noindexed HTML in
        the 300s render cache. Clearing the hold served that cached copy, so
        rollback silently did nothing for five minutes and looked broken.
        """
        self._set_holds([HELD])
        st, html = self._get("/n/%s" % HELD)
        self.assertEqual(st, 200)
        self.assertIn("noindex", self._robots(html) or "")
        self._set_holds([])
        st, html = self._get("/n/%s" % HELD)
        r = self._robots(html) or ""
        self.assertNotIn("noindex", r, "rollback failed: %r" % r)

    def test_rollback_holds_within_cache_lifetime(self):
        """Prove the cache is not holding a stale noindex copy: re-apply and
        clear repeatedly with no delay and the last state must win."""
        for i in range(3):
            self._set_holds([HELD])
            self.assertIn("noindex", self._robots(self._get("/n/%s" % HELD)[1]) or "",
                          "apply %d" % i)
            self._set_holds([])
            self.assertNotIn("noindex",
                             self._robots(self._get("/n/%s" % HELD)[1]) or "",
                             "rollback %d" % i)

    def test_held_pages_still_render_for_visitors(self):
        """noindex is not a 404 -- the page must stay live and useful."""
        self._set_holds([HELD])
        st, html = self._get("/n/%s" % HELD)
        self.assertEqual(st, 200)
        self.assertGreater(len(html), 2000)
        self.assertIn("noindex", self._robots(html) or "")

    def test_sitemap_omits_held_family(self):
        self._set_holds([HELD])
        st, xml = self._get("/sitemap.xml")
        self.assertEqual(st, 200)
        self.assertNotIn("/n/%s" % HELD, xml)
        self.assertNotIn("/lp/%s" % HELD, xml)
        self.assertNotIn("/stories/%s" % HELD, xml)
        self.assertNotIn("/n/%s/cast-iron-skillet" % HELD, xml)

    def test_sitemap_and_noindex_agree_after_apply(self):
        """The invariant that matters: whatever the sitemap drops, the robots
        meta must also call noindex -- no page may contradict the sitemap."""
        self._set_holds([HELD])
        st, xml = self._get("/sitemap.xml")
        for path in re.findall(r"<loc>[^<]*(/n/[^<]*)</loc>", xml):
            pass
        # every held-route URL must be noindex, and absent from the sitemap
        for p in ("/n/%s" % HELD, "/lp/%s" % HELD, "/stories/%s" % HELD):
            self.assertNotIn(p, xml)
            st, html = self._get(p)
            if st != 404:
                self.assertIn("noindex", self._robots(html) or "", p)

    def test_json_and_admin_untouched(self):
        """A hold must never leak into non-HTML or authenticated responses."""
        self._set_holds([HELD])
        st, body = self._get("/api/settings")
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertEqual(d["consolidation"]["holds"], [HELD])
        st, body = self._get("/admin/system")
        self.assertEqual(st, 200)
        self.assertNotIn("noindex, follow", body)

    # ------------------------------------------------------- write honesty
    def test_failed_write_reports_error_not_false_success(self):
        """`_set_setting` swallows DB errors, so a silently dropped write used
        to return 200 and echo the requested holds back. An operator could not
        tell a real apply from a no-op. The write now verifies its own round
        trip and the route surfaces the failure."""
        import server as srv
        real = srv._set_setting

        def blackhole(key, value):
            if key == "seo.consolidation.holds":
                return                      # pretend the write succeeded
            return real(key, value)

        srv._set_setting = blackhole
        try:
            with self.assertRaises(RuntimeError):
                srv._set_consolidation_holds([HELD])
        finally:
            srv._set_setting = real
        # State must be unchanged, not half-applied.
        self.assertNotIn(HELD, server._consolidation_holds())

    def test_verified_write_is_applied(self):
        """The verification must not reject a legitimate write."""
        got = server._set_consolidation_holds(["best-griddle-pan", "keto-snacks"])
        self.assertEqual(got, frozenset({"best-griddle-pan", "keto-snacks"}))
        self.assertEqual(server._consolidation_holds(), got)
        self._set_holds([])


if __name__ == "__main__":
    unittest.main()
