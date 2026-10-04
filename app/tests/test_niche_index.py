# -*- coding: utf-8 -*-
"""/niches -- the index that turns the sitemap hint into a link graph.

The homepage "Explore the niches" grid is capped at 36 tiles and there was no
index of the rest, so on the live site 748 of 1,321 listed URLs were reachable
only from sitemap.xml. Measured before this page existed: 43% of the inventory
at one click from `/`, `/blog` or `/stories`.

The index is deliberately one logical thing split across `?p=` URLs: page 1 is
in the sitemap and the deep pages are not, all of them are indexable and
self-canonical, and they carry rel=prev/next plus a visible pager. So the tests
here cover both halves -- that it lists the whole inventory, and that it stays
one canonical index rather than N competing listings.
"""
import os
import re
import tempfile
import unittest

import server
import seo


def _niche(i, kw=None):
    return {"keyword": kw or ("widget %03d" % i), "created_at": "2026-01-01",
            "products": [{"asin": "B%09d" % i, "title": "Widget %d" % i,
                          "price": 10.0, "rating": 4.0, "reviews": 10,
                          "img": "/i/%d.png" % i, "brand": "Acme", "url": "u",
                          "aff": "/go/B%09d" % i}]}


def _html(res):
    return res.decode("utf-8") if isinstance(res, bytes) else res


class TestIndexListsTheWholeInventory(unittest.TestCase):
    def setUp(self):
        self.pool = [_niche(i) for i in range(130)]

    def test_every_niche_is_reachable_across_the_pages(self):
        """The whole point. One niche that no page links is one page a crawler
        only ever learns about from the sitemap."""
        seen = set()
        for pg in range(1, 6):
            html = _html(seo.render_niche_index(self.pool, page=pg))
            seen.update(re.findall(r'class="ntile" href="/n/([^"]+)"', html))
        self.assertEqual(130, len(seen))

    def test_a_single_page_when_the_inventory_fits(self):
        html = _html(seo.render_niche_index(self.pool[:10], page=1))
        self.assertEqual(10, html.count('class="ntile"'))
        self.assertNotIn('rel="next"', html)
        self.assertNotIn('rel="prev"', html)

    def test_niches_without_products_are_not_listed(self):
        """An empty page is a dead end, and the sitemap already excludes them.
        Two surfaces disagreeing is the contradiction AGENTS.md warns about."""
        pool = [_niche(1), {"keyword": "empty one", "created_at": "2026-01-01",
                            "products": []}]
        html = _html(seo.render_niche_index(pool))
        self.assertIn("/n/widget-001", html)
        self.assertNotIn("/n/empty-one", html)

    def test_slug_collisions_are_listed_once(self):
        """"back pain" and "back-pain" are one URL. Listing both would ship two
        tiles pointing at the same page."""
        pool = [_niche(1, "back pain"), _niche(2, "back-pain"), _niche(3, "yoga")]
        html = _html(seo.render_niche_index(pool))
        self.assertEqual(2, html.count('class="ntile"'))

    def test_ordering_is_stable_across_calls(self):
        """A niche that moves pages every time one is added invalidates the
        crawler's model of how deep the index goes."""
        a = _html(seo.render_niche_index(self.pool, page=2))
        b = _html(seo.render_niche_index(self.pool, page=2))
        self.assertEqual(a, b)
        first = re.findall(r'class="ntile" href="/n/([^"]+)"', a)
        self.assertEqual(sorted(first), first)


class TestItStaysOneCanonicalIndex(unittest.TestCase):
    def setUp(self):
        self.pool = [_niche(i) for i in range(130)]

    def test_page_one_is_self_canonical(self):
        html = _html(seo.render_niche_index(self.pool, page=1))
        self.assertIn('<link rel="canonical" href="https://trypstore.com/niches">',
                      html)

    def test_deep_pages_are_self_canonical_with_rel_prev_next(self):
        html = _html(seo.render_niche_index(self.pool, page=2))
        self.assertIn('rel="canonical" href="https://trypstore.com/niches?p=2"',
                      html)
        self.assertIn('<link rel="prev" href="https://trypstore.com/niches">', html)
        self.assertIn('<link rel="next" href="https://trypstore.com/niches?p=3">',
                      html)

    def test_indexnow_source_agrees_with_the_sitemap(self):
        """seo.indexable_urls() is what scripts/ping_indexnow.py submits. If it
        forgets /niches, the sitemap advertises the hub but no crawler is ever
        told it changed -- which is exactly how 2,469 URLs reached zero views."""
        urls = seo.indexable_urls([_niche(1)], base_url="https://trypstore.com")
        self.assertIn("https://trypstore.com/niches", urls)
        self.assertNotIn("https://trypstore.com/niches?p=2", urls)

    def test_deep_pages_are_indexable(self):
        """/blog puts its deep pages behind noindex,follow. This index is the
        opposite case -- the deep pages are the only route to most of the
        inventory -- so they have to stay indexable."""
        for pg in (1, 2, 3):
            html = _html(seo.render_niche_index(self.pool, page=pg))
            self.assertNotIn('name="robots" content="noindex', html)

    def test_out_of_range_page_clamps_to_the_last(self):
        html = _html(seo.render_niche_index(self.pool, page=99))
        self.assertIn("Page 3 of 3", html)
        self.assertNotIn('rel="next"', html)

    def test_junk_page_is_page_one(self):
        for bad in (0, -5, None):
            html = _html(seo.render_niche_index(self.pool, page=bad))
            self.assertIn('href="https://trypstore.com/niches">', html)

    def test_sitemap_lists_page_one_and_not_the_deep_pages(self):
        """Listing all three would ask a crawler to treat one index as three
        destinations."""
        fd, db = tempfile.mkstemp()
        os.close(fd)
        prior_db, prior_flag = server.DB, server._db_schema_ready
        server.DB = db
        server._db_schema_ready = False
        try:
            server._db().close()
            xml = _html(server.Handler.__new__(server.Handler)._sitemap())
        finally:
            server.DB = prior_db
            server._db_schema_ready = prior_flag
            os.unlink(db)
        self.assertIn("<loc>https://trypstore.com/niches</loc>", xml)
        self.assertNotIn("/niches?p=", xml)


class TestSeoShape(unittest.TestCase):
    def setUp(self):
        self.pool = [_niche(i) for i in range(130)]

    def test_page_one_carries_a_collection_page_graph(self):
        html = _html(seo.render_niche_index(self.pool, page=1))
        ld = re.search(r'<script type="application/ld\+json">(.*?)</script>',
                       html, re.S).group(1)
        self.assertIn('"@type": "CollectionPage"', ld)
        self.assertIn('"@type": "ItemList"', ld)
        self.assertIn('"@type": "BreadcrumbList"', ld)
        # The first tile and the first ItemList entry must be the same URL, or
        # the markup advertises a destination the page does not link to.
        first_link = re.search(r'class="ntile" href="([^"]+)"', html).group(1)
        self.assertIn('"url": "https://trypstore.com%s"' % first_link, ld)

    def test_title_and_description_are_present_and_specific(self):
        html = _html(seo.render_niche_index(self.pool, page=1))
        title = re.search(r"<title>(.*?)</title>", html).group(1)
        self.assertIn("130", title)
        self.assertEqual(1, title.count("pstore"), "site name is appended by _head")
        desc = re.search(r'<meta name="description" content="([^"]+)"', html).group(1)
        self.assertIn("130", desc)
        self.assertLess(len(desc), 300)

    def test_h1_is_h1(self):
        html = _html(seo.render_niche_index(self.pool, page=1))
        self.assertEqual(1, len(re.findall(r"<h1>", html)))

    def test_page_is_linked_from_the_footer_and_masthead(self):
        """The old footer link promised "All niches" and pointed at a
        homepage anchor showing 36 of them."""
        footer = _html(seo._footer())
        self.assertIn('<a href="/niches">All niches</a>', footer)
        self.assertNotIn('/#niches">All niches', footer)


class TestRouteServesIt(unittest.TestCase):
    def _serve(self, path, niches):
        h = server.Handler.__new__(server.Handler)
        h.path = path
        h.command = "GET"
        h.headers = {"Host": "trypstore.com"}
        h.client_address = ("127.0.0.1", 1234)
        sent = {}
        h._send_cached = lambda body, ctype, **kw: sent.update(
            body=body if isinstance(body, str) else body.decode(), **kw)
        h._send = lambda *a, **k: sent.update(body="")
        h._all_niches = lambda: niches
        h._authed = lambda: False
        h._prelim_guard = lambda: "k"
        h.do_GET()
        return sent.get("body", "")

    def test_get_niches_renders(self):
        html = self._serve("/niches", [_niche(1), _niche(2)])
        self.assertIn("/n/widget-001", html)
        self.assertIn("/n/widget-002", html)

    def test_deep_page_is_served_from_the_query_string(self):
        html = self._serve("/niches?p=2", [_niche(i) for i in range(130)])
        # Ordering is alphabetical by slug, so page 2 is widget-060..119.
        self.assertIn("/n/widget-060", html)
        self.assertNotIn("/n/widget-000", html)
        self.assertIn('rel="canonical" href="https://trypstore.com/niches?p=2"', html)

    def test_junk_page_param_still_serves_page_one(self):
        html = self._serve("/niches?p=notanumber", [_niche(1), _niche(2)])
        self.assertIn("/n/widget-001", html)


if __name__ == "__main__":
    unittest.main()