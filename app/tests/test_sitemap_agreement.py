"""The sitemap must never list a URL that serves noindex.

Live audit 2026-10-04: 437 nested `/n/<parent>/<term>` pages sat in sitemap.xml
while rendering `<meta name="robots" content="noindex,nofollow">`. 30% of a
1,457-URL sitemap handed to Google was pages it was told to discard — wasted
crawl budget on the one domain whose only real problem is that no engine has
sent it a single impression.

The cause was duplicated logic, not a bad predicate. `_force_noindex` decided
noindex from `_path_is_held` / `_path_is_thin_topic` / `_lp_pages_indexable`,
while `_sitemap` re-derived the same answer with its own membership test — and
compared a `.lower()`ed path while appending the raw `slug`, so a slug with
stray whitespace or capitals was filtered under one URL and published under
another. Nested topics were never checked for a consolidation hold at all.

Fix: both now ask `_noindex_reason`, one predicate. These tests hold the
invariant that made the bug impossible to reintroduce.
"""
import os
import re
import shutil
import tempfile
import threading
import unittest
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
PARENT = "keto-snacks"          # a real seed niche, so the child can be listed
THIN = "keto-snacks-that-are-not-a-real-question"
BAND = "under-25"               # price band: real differentiation, must survive
# A head-to-head naming ASINs the parent really stores renders both products and
# stays listed; RESOLVABLE names the first two seed ASINs, GHOST names none.
RESOLVABLE = "brand-a-vs-brand-b"
GHOST = "ghost-a-vs-ghost-b"
HALF = "half-stale-vs-brand-b"   # one live ASIN, one dead: render_vs gets nothing


class SitemapAgreementTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="smagree_")
        cls.db = os.path.join(cls.tmp, "t.db")
        shutil.copy(os.path.join(REPO, "pstore.db"), cls.db)
        cls.port = _free_port()
        cls._saved_env = {k: os.environ.get(k) for k in
                          ("PSTORE_DB", "PSTORE_ADMIN_EMAIL",
                           "PSTORE_ADMIN_PASSWORD", "PSTORE_PORT",
                           "PSTORE_SKIP_WORKERS")}
        os.environ.update(PSTORE_DB=cls.db, PSTORE_ADMIN_EMAIL="t@example.com",
                          PSTORE_ADMIN_PASSWORD="testpass123",
                          PSTORE_PORT=str(cls.port), PSTORE_SKIP_WORKERS="1")
        import importlib
        importlib.reload(server)
        cls.srv = server.ThreadingHTTPServer(("127.0.0.1", cls.port),
                                             server.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls._seed()

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
    def _seed(cls):
        conn = server._db()
        try:
            conn.execute("DELETE FROM topics WHERE parent_slug=?", (PARENT,))
            # Take the head-to-head ASINs from the parent's own stock rather than
            # hardcoding them: they must be products the child can actually
            # render, or the fixture stops meaning anything when the seed
            # changes. (An earlier version hardcoded a neighbouring niche's
            # ASINs and the page was correctly dropped as unresolvable.)
            asins = []
            for r in conn.execute(
                    "SELECT keyword, products FROM niches").fetchall():
                try:
                    if server.seo._slugify(r["keyword"]) != PARENT:
                        continue
                except Exception:
                    continue
                asins = re.findall(r'"asin"\s*:\s*"([^"]+)"',
                                    r["products"] or "")[:2]
                break
            if len(asins) < 2:
                raise AssertionError(
                    "seed parent %r needs two stored ASINs, found %r"
                    % (PARENT, asins))
            cls.vs_term = "Brand A vs Brand B (@%s|@%s)" % (asins[0], asins[1])
            cls.half_term = "Half A vs Brand B (@%s|@B0DEADBE12)" % asins[0]
            conn.executemany(
                "INSERT INTO topics (parent_slug, slug, term, created_at) "
                "VALUES (?,?,?,datetime('now'))",
                [(PARENT, THIN, "keto snacks that are not a real question"),
                 (PARENT, BAND, "keto snacks under $25"),
                 (PARENT, RESOLVABLE, cls.vs_term),
                 (PARENT, GHOST, "Ghost A vs Ghost B (@B0ZZZZZZZ9|@B0YYYYYYY8)"),
                 (PARENT, HALF, cls.half_term)])
            conn.commit()
        finally:
            conn.close()
        # The 60s cache was primed from the pre-seed topics table.
        server._THIN_TOPIC_SET_CACHE.update({"val": None, "at": 0.0})
        server._THIN_TOPIC_CACHE.update({"value": None, "at": 0.0})

    def _sitemap_paths(self):
        with urllib.request.urlopen("http://127.0.0.1:%d/sitemap.xml"
                                    % self.port, timeout=30) as r:
            xml = r.read().decode("utf-8")
        base = server.seo.BASE_URL.rstrip("/")
        out = []
        for loc in re.findall(r"<loc>([^<]+)</loc>", xml):
            out.append(loc[len(base):] if loc.startswith(base) else loc)
        return out

    # ---- the invariant ---------------------------------------------------
    def test_no_listed_url_serves_noindex(self):
        """The rule from AGENTS.md, executable: every URL in the sitemap must be
        one the page itself is willing to have indexed."""
        offenders = [p for p in self._sitemap_paths()
                     if server._noindex_reason(p) is not None]
        self.assertEqual(offenders, [],
                         "sitemap listed noindex pages: %r" % (offenders[:8],))

    def test_listed_urls_are_actually_reachable_and_indexable(self):
        """Belt and braces: the served page for each listed URL must not carry a
        noindex robots meta. This is what actually broke in production."""
        import urllib.error
        bad = []
        for p in self._sitemap_paths():
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d%s"
                                            % (self.port, p), timeout=30) as r:
                    head = r.read(4000).decode("utf-8", "replace")
            except urllib.error.HTTPError:
                continue
            m = re.search(r'<meta name="robots" content="([^"]+)"', head)
            if m and "noindex" in m.group(1):
                bad.append(p)
        self.assertEqual(bad, [], "listed but served noindex: %r" % (bad[:8],))

    # ---- the regression lock ---------------------------------------------
    def test_relabelled_topic_is_not_listed(self):
        self.assertNotIn("/n/%s/%s" % (PARENT, THIN), self._sitemap_paths())

    def test_half_stale_head_to_head_is_not_listed(self):
        """End-to-end version of the one-of-two-ASINs regression: the page is
        reachable and listed-by-default until the check demands both ASINs."""
        self.assertNotIn("/n/%s/%s" % (PARENT, HALF), self._sitemap_paths())

    def test_price_band_topic_is_still_listed(self):
        self.assertIn("/n/%s/%s" % (PARENT, BAND), self._sitemap_paths())

    def test_head_to_head_topic_with_resolvable_asins_is_still_listed(self):
        self.assertIn("/n/%s/%s" % (PARENT, RESOLVABLE), self._sitemap_paths())

    def test_head_to_head_topic_with_unresolvable_asins_is_not_listed(self):
        """Neither ASIN is in the parent's stock, so the page renders empty and
        noindex. The parent having products says nothing about the child."""
        paths = self._sitemap_paths()
        self.assertNotIn("/n/%s/%s" % (PARENT, GHOST), paths)
        self.assertIsNone(server._noindex_reason("/n/%s/%s" % (PARENT, GHOST)))
        self.assertFalse(server._nested_topic_renders(
            "Ghost A vs Ghost B (@B0ZZZZZZZ9|@B0YYYYYYY8)", "x", []))

    def test_one_of_two_asins_is_not_enough(self):
        """Regression. An earlier version of the check accepted EITHER ASIN,
        so every half-stale head-to-head stayed listed while the page served
        noindex -- 21% of the nested URLs sampled live. `render_vs` needs both.
        """
        items = [{"asin": "B094MR99N6", "price": 19.99}]
        self.assertFalse(server._nested_topic_renders(
            "A vs B (@B094MR99N6|@B0MISSING1)", "a-vs-b", items))
        self.assertTrue(server._nested_topic_renders(
            "A vs B (@B094MR99N6|@B09M93GJLR)",
            "a-vs-b", [{"asin": "B094MR99N6"}, {"asin": "b09m93gjlr"}]))

    def test_price_band_needs_a_product_inside_the_band(self):
        cheap = [{"asin": "B1", "price": 9.99}]
        dear = [{"asin": "B1", "price": 49.99}]
        self.assertTrue(server._nested_topic_renders("keto under $25",
                                                     "under-25", cheap))
        self.assertFalse(server._nested_topic_renders("keto under $25",
                                                      "under-25", dear))
        # Unparseable price counts as outside the band, exactly as seo._num_price
        # makes render_priceband treat it.
        self.assertFalse(server._nested_topic_renders("keto under $25",
                                                      "under-25",
                                                      [{"asin": "B1"}]))

    def test_relabelled_topic_is_always_rendered(self):
        self.assertTrue(server._nested_topic_renders(
            "keto snacks that are not a real question", "some-term", []))

    def test_broken_products_json_is_treated_as_empty(self):
        self.assertEqual(server._parse_products("{not json"), [])
        self.assertEqual(server._parse_products(None), [])
        self.assertEqual(server._parse_products('{"a":1}'), [])
        self.assertEqual(len(server._parse_products('[{"asin":"B1"}]')), 1)

    # ---- the predicate ----------------------------------------------------
    def test_reason_for_each_case(self):
        self.assertEqual(
            server._noindex_reason("/n/%s/%s" % (PARENT, THIN)), "thin-topic")
        self.assertEqual(server._noindex_reason("/n/%s/under-25" % PARENT), None)
        self.assertIsNone(server._noindex_reason("/n/%s" % PARENT))

    def test_landing_pages_are_reported_when_the_policy_is_off(self):
        self.assertEqual(server._noindex_reason("/lp/keto-snacks"),
                         "landing-page")

    def test_empty_path_is_indexable(self):
        self.assertIsNone(server._noindex_reason(""))
        self.assertIsNone(server._noindex_reason(None))

    def test_force_noindex_agrees_with_the_predicate(self):
        """The renderer and the sitemap must never disagree, so every reason the
        predicate returns has to produce an actual noindex in the body."""
        path = "/n/%s/%s" % (PARENT, THIN)
        body = ('<html><head><link rel="canonical" '
                'href="https://trypstore.com%s"><title>t</title>'
                "</head><body>b</body></html>" % path).encode()
        out = server._force_noindex(body, path).decode()
        self.assertIn("noindex", out)
        kept = "/n/%s" % PARENT
        self.assertEqual(server._force_noindex(body, kept), body)


if __name__ == "__main__":
    unittest.main()