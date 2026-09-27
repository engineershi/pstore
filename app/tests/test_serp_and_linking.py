"""Regression tests for SERP titles and landing-page internal linking.

Two defects, both confirmed on production before these tests existed:

1. Every page shipped the identical title pattern "<kw> - ranked picks", so
   nothing differentiated the result and none of it matched a real query. The
   new builder leads with the head term, states a truthful pick count, and adds
   a freshness year. It must never exceed the 60-char SERP budget (including
   the " | pstore" suffix) and must never cut mid-word.
2. ``/lp/<slug>`` had a single onward link, making every landing page a crawl
   dead end. ``_related_guides_html`` now hands readers into the ranking pages.
"""
import datetime
import unittest

import cms_render
import seo


def _items(n):
    return [{"asin": "B%02d" % i, "title": "P%d" % i, "price": 9.99,
             "stars": 4.2, "reviews": 400} for i in range(n)]


class TestGuideTitle(unittest.TestCase):
    def test_stays_within_60_including_suffix(self):
        for kw in ("best keto", "best hammock", "keto snacks", "rocking chair",
                   "best string lights for classroom",
                   "back massager for pain relief deep tissue",
                   "a" * 90, "best best hammock"):
            for n in (0, 1, 2, 6, 30):
                t = seo._guide_title(kw, _items(n))
                self.assertLessEqual(len("%s | pstore" % t), 60, (kw, n))
                self.assertTrue(t.strip(), kw)

    def test_no_best_best(self):
        self.assertNotIn("best best", seo._guide_title("best best hammock",
                                                        _items(4)).lower())

    def test_leads_with_head_term(self):
        t = seo._guide_title("best keto", _items(6))
        self.assertTrue(t.lower().startswith("best keto"), t)

    def test_count_is_truthful(self):
        """The number shown must be the number of products actually rendered."""
        for n in (2, 3, 7, 12):
            t = seo._guide_title("best keto", _items(n))
            self.assertIn(str(n), t, (n, t))

    def test_no_invented_count_when_empty(self):
        t = seo._guide_title("best keto", [])
        self.assertNotIn("0 Top", t)
        self.assertNotIn("Top Picks (0)", t)

    def test_year_present_when_products(self):
        y = str(datetime.datetime.now().year)
        self.assertIn(y, seo._guide_title("best keto", _items(6)))

    def test_never_cuts_mid_word(self):
        kw = "back massager for pain relief deep tissue professional"
        t = seo._guide_title(kw, _items(4))
        body = t.split(":")[0]
        for word in body.replace("Best", "").split():
            self.assertIn(word.lower(), kw.lower(), (word, t))

    def test_distinguishes_pages(self):
        """Two niches must not produce the same title."""
        a = seo._guide_title("best keto", _items(6))
        b = seo._guide_title("best hammock", _items(6))
        self.assertNotEqual(a, b)

    def test_works_without_items(self):
        self.assertTrue(seo._guide_title("best keto"))
        self.assertTrue(seo._guide_title("best keto", None))


class TestLandingRelatedGuides(unittest.TestCase):
    CTX = {"slug": "keto", "sections": [], "style": {}, "settings": {},
           "pick": {"title": "Keto Bar",
                    "url": "https://www.amazon.com/dp/B1?tag=pstore2006-20"}}

    def test_renders_sibling_links(self):
        ctx = dict(self.CTX, related=[{"slug": "keto-snacks", "keyword": "keto snacks"},
                                      {"slug": "keto-gummies", "keyword": "keto gummies"}])
        h = cms_render.render_landing_page_page(ctx, "keto",
                                                site_url="https://trypstore.com")
        self.assertIn("Related buying guides", h)
        self.assertIn('href="/n/keto-snacks"', h)
        self.assertIn('href="/n/keto-gummies"', h)

    def test_lp_is_no_longer_a_dead_end(self):
        ctx = dict(self.CTX, related=[{"slug": "keto-snacks", "keyword": "keto snacks"}])
        h = cms_render.render_landing_page_page(ctx, "keto",
                                                site_url="https://trypstore.com")
        self.assertGreaterEqual(h.count('href="/n/'), 2)

    def test_suppressed_when_no_siblings(self):
        h = cms_render.render_landing_page_page(dict(self.CTX, related=[]), "keto",
                                                site_url="https://trypstore.com")
        self.assertNotIn("Related buying guides", h)

    def test_escapes_keyword(self):
        ctx = dict(self.CTX, related=[{"slug": "x", "keyword": '<script>a</script>'}])
        h = cms_render.render_landing_page_page(ctx, "keto")
        self.assertNotIn("<script>a</script>", h)

    def test_caps_at_eight(self):
        rel = [{"slug": "s%d" % i, "keyword": "kw%d" % i} for i in range(20)]
        h = cms_render._related_guides_html({"related": rel})
        self.assertEqual(h.count('class="chip"'), 8)

    def test_ignores_malformed_entries(self):
        h = cms_render._related_guides_html({"related": [{}, None, {"slug": ""}, 5]})
        self.assertEqual(h, "")


if __name__ == "__main__":
    unittest.main()
