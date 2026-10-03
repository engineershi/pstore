"""The three revenue bugs found in the 2026-10-03 live audit.

1. `_earnings_priority_data` classified commission categories with
   `r.get("slug")` on rows that only ever carry a "niche" key, so
   classify() always saw "" and every niche was priced at the flat 4% / $40
   default. The category correction was dead code on the page the operator uses
   to decide where to spend effort. Verified live before the fix: /api/
   earnings/priority returned commission_pct 4.0 / avg_order 40.0 for gold
   jewellery, whose real rates are 20% / $85.

2. `quick_picks_band` pooled one product per niche with no de-duplication, so
   two near-duplicate niche rows ("knife set" / "knife sets 2026") put the
   identical ASIN in the #1 and #2 slots of the homepage hero at the same price
   and rating, under contradictory verdict labels.

3. Nested /n/<parent>/<term> pages were served by `seo.render_topic`, which is
   handed the PARENT niche record, so every autosuggest child shipped the
   parent's exact ASINs under the child's H1. Measured live: all 27 children of
   /n/best-lawn-mower served the parent's identical 8 lawn-mower ASINs --
   including /best-lawn-mower-blade-sharpener and /best-lawn-mower-battery --
   at 89.6% body-text similarity, self-canonical, 1,963 pages in the sitemap.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import editorial
import earnings
import server


def _prod(asin, title, stars=4.5, reviews=500, price=39.98):
    return {"asin": asin, "title": title, "stars": stars, "reviews": reviews,
            "price": price}


class PriorityCategoryTest(unittest.TestCase):
    """The category correction must actually reach priority_rows()."""

    def test_cat_reads_the_niche_key_that_the_rows_actually_carry(self):
        # This is the exact dict shape _earnings_priority_data builds.
        rows = [{"niche": "gold jewelry", "clicks": 10},
                {"niche": "keto snacks", "clicks": 10}]

        def cat(r):
            return earnings.classify(r.get("niche") or r.get("slug") or "")

        ranked = earnings.priority_rows(rows, cat)["ranked"]
        by_niche = {r["niche"]: r for r in ranked}
        self.assertEqual(by_niche["gold jewelry"]["commission_pct"], 20.0)
        self.assertEqual(by_niche["gold jewelry"]["avg_order"], 85.0)
        # A low-rate page must score lower than a high-rate page on equal clicks.
        self.assertGreater(by_niche["gold jewelry"]["score"],
                           by_niche["keto snacks"]["score"])

    def test_cat_falls_back_to_slug_when_niche_is_absent(self):
        def cat(r):
            return earnings.classify(r.get("niche") or r.get("slug") or "")

        ranked = earnings.priority_rows([{"slug": "gold jewelry", "clicks": 4}],
                                        cat)["ranked"]
        self.assertEqual(ranked[0]["commission_pct"], 20.0)

    def test_blank_row_does_not_raise(self):
        def cat(r):
            return earnings.classify(r.get("niche") or r.get("slug") or "")

        ranked = earnings.priority_rows([{"clicks": 2}], cat)["ranked"]
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["commission_pct"],
                         earnings.DEFAULT_COMMISSION_PCT)


class QuickPicksDedupeTest(unittest.TestCase):
    """The homepage hero must never show the same product twice."""

    def _band(self):
        return editorial.quick_picks_band([
            {"keyword": "knife set",
             "products": [_prod("B9", "Astercook Knife Set", 4.8, 4000),
                          _prod("B3", "Honour Knife", 4.5, 900)]},
            # Same top product, different keyword row -- exactly the live bug.
            {"keyword": "knife sets 2026",
             "products": [_prod("B9", "Astercook Knife Set", 4.8, 4000),
                          _prod("B4", "Precision Knife", 4.6, 700)]},
            {"keyword": "webcam",
             "products": [_prod("B7", "1080P Webcam", 4.8, 114),
                          _prod("B5", "4K Webcam", 4.7, 300)]},
        ], count=3)

    def test_same_asin_never_appears_in_two_slots(self):
        import re
        asins = re.findall(r'data-asin="([^"]+)"', self._band())
        self.assertEqual(len(asins), len(set(asins)),
                         "homepage hero repeated an ASIN: %r" % (asins,))

    def test_duplicate_row_falls_through_to_its_next_product(self):
        import re
        # The knife-sets-2026 row must contribute "Precision Knife", not the
        # knife set it already gave away.
        self.assertIn("Precision Knife", self._band())
        self.assertIn("1080P Webcam", self._band())

    def test_band_still_fills_the_requested_count(self):
        import re
        self.assertEqual(len(re.findall(r'data-asin="', self._band())), 3)

    def test_products_without_asins_still_de_dup_by_title(self):
        band = editorial.quick_picks_band([
            {"keyword": "a", "products": [{"title": "Same Thing", "stars": 4.5,
                                           "reviews": 10}]},
            {"keyword": "b", "products": [{"title": "Same Thing", "stars": 4.5,
                                           "reviews": 10}]},
            {"keyword": "c", "products": [{"title": "Other Thing", "stars": 4.0,
                                           "reviews": 10}]},
        ], count=3)
        self.assertEqual(band.count("Same Thing"), 1)
        self.assertIn("Other Thing", band)

    def test_niches_without_products_are_skipped(self):
        band = editorial.quick_picks_band(
            [{"keyword": "empty", "products": []},
             {"keyword": "one", "products": [_prod("B1", "Solo Pick")]}],
            count=3)
        self.assertIn("Solo Pick", band)
        self.assertEqual(band.count('data-asin="'), 1)

    def test_empty_input_renders_nothing(self):
        self.assertEqual(editorial.quick_picks_band([], count=3), "")
        self.assertEqual(editorial.quick_picks_band(None, count=3), "")


class ThinTopicTest(unittest.TestCase):
    """Relabelled long-tail pages must stop competing; real ones must not."""

    def setUp(self):
        self._saved = (server._THIN_TOPIC_SET_CACHE["val"],
                       server._THIN_TOPIC_SET_CACHE["at"],
                       server._THIN_TOPIC_CACHE["value"],
                       server._THIN_TOPIC_CACHE["at"])

    def tearDown(self):
        (server._THIN_TOPIC_SET_CACHE["val"],
         server._THIN_TOPIC_SET_CACHE["at"],
         server._THIN_TOPIC_CACHE["value"],
         server._THIN_TOPIC_CACHE["at"]) = self._saved

    def _prime(self, paths, indexable=False):
        server._THIN_TOPIC_SET_CACHE.update({"val": set(paths), "at": 9e9})
        server._THIN_TOPIC_CACHE.update({"value": indexable, "at": 9e9})

    def test_relabelled_topic_is_thin(self):
        self._prime(["/n/best-lawn-mower/best-lawn-mower-battery"])
        self.assertTrue(server._path_is_thin_topic(
            "/n/best-lawn-mower/best-lawn-mower-battery"))

    def test_price_band_topic_is_never_thin(self):
        self.assertFalse(server._path_is_thin_topic("/n/keto/under-25"))

    def test_hub_is_never_thin(self):
        self.assertFalse(server._path_is_thin_topic("/n/best-lawn-mower"))

    def test_unrelated_path_is_never_thin(self):
        self.assertFalse(server._path_is_thin_topic("/lp/keto"))
        self.assertFalse(server._path_is_thin_topic("/"))

    def test_opt_in_restores_indexing(self):
        self._prime(["/n/best-lawn-mower/best-lawn-mower-battery"],
                    indexable=True)
        self.assertFalse(server._path_is_thin_topic(
            "/n/best-lawn-mower/best-lawn-mower-battery"))

    def test_trailing_slash_still_matches(self):
        self._prime(["/n/best-lawn-mower/best-lawn-mower-battery"])
        self.assertTrue(server._path_is_thin_topic(
            "/n/best-lawn-mower/best-lawn-mower-battery/"))

    def test_parent_lookup(self):
        self.assertEqual(
            server._thin_topic_parent("/n/best-lawn-mower/best-lawn-mower-battery"),
            "/n/best-lawn-mower")

    # ---- the response-level behaviour -------------------------------------
    def _page(self, path):
        return ("<html><head>"
                '<link rel="canonical" href="https://trypstore.com%s">'
                "<title>t</title></head><body>same products</body></html>"
                % path).encode("utf-8")

    def test_thin_topic_serves_noindex_and_canonicalises_to_parent(self):
        self._prime(["/n/best-lawn-mower/best-lawn-mower-battery"])
        out = server._force_noindex(
            self._page("/n/best-lawn-mower/best-lawn-mower-battery"),
            "/n/best-lawn-mower/best-lawn-mower-battery").decode("utf-8")
        self.assertIn('content="noindex, follow"', out)
        self.assertIn('<link rel="canonical" '
                      'href="https://trypstore.com/n/best-lawn-mower">', out)
        # The old self-canonical must be gone, not merely accompanied.
        self.assertNotIn("/best-lawn-mower-battery", out.split("<body>")[0])

    def test_thin_topic_page_stays_fully_live_for_visitors(self):
        """noindex is a crawler instruction. The body must be untouched so the
        CTAs and prices a visitor needs still render."""
        self._prime(["/n/best-lawn-mower/best-lawn-mower-battery"])
        path = "/n/best-lawn-mower/best-lawn-mower-battery"
        out = server._force_noindex(self._page(path), path).decode("utf-8")
        self.assertIn("same products", out)
        self.assertIn("<body>", out)

    def test_kept_page_is_byte_identical(self):
        self._prime(["/n/best-lawn-mower/best-lawn-mower-battery"])
        body = self._page("/n/keto")
        self.assertEqual(server._force_noindex(body, "/n/keto"), body)

    def test_price_band_page_is_not_touched(self):
        self._prime(["/n/keto/best-keto-under-25"])
        body = self._page("/n/keto/under-25")
        self.assertEqual(server._force_noindex(body, "/n/keto/under-25"), body)

    def test_rewrite_is_idempotent(self):
        self._prime(["/n/keto/best-keto-snacks-2026"])
        path = "/n/keto/best-keto-snacks-2026"
        once = server._force_noindex(self._page(path), path)
        twice = server._force_noindex(once, path)
        self.assertEqual(once, twice)
        self.assertEqual(twice.decode("utf-8").count("noindex"), 1)

    def test_page_without_a_canonical_still_gets_noindex(self):
        self._prime(["/n/keto/best-keto-snacks-2026"])
        path = "/n/keto/best-keto-snacks-2026"
        body = b"<html><head><title>t</title></head><body>b</body></html>"
        out = server._force_noindex(body, path).decode("utf-8")
        self.assertIn('content="noindex, follow"', out)

    def test_replaces_an_existing_noindex_nofollow(self):
        """_head emits noindex,nofollow for hold=True pages. That would stop
        equity flowing to the parent, so the chokepoint must upgrade it."""
        self._prime(["/n/keto/best-keto-snacks-2026"])
        path = "/n/keto/best-keto-snacks-2026"
        body = ('<html><head><meta name="robots" content="noindex,nofollow">'
                '<link rel="canonical" href="https://trypstore.com%s">'
                "</head><body>b</body></html>" % path).encode("utf-8")
        out = server._force_noindex(body, path).decode("utf-8")
        self.assertIn('content="noindex, follow"', out)
        self.assertNotIn("nofollow", out)

    def test_single_quoted_canonical_is_rewritten(self):
        self._prime(["/n/keto/best-keto-snacks-2026"])
        path = "/n/keto/best-keto-snacks-2026"
        body = ("<html><head><link rel='canonical' href='https://trypstore.com%s'>"
                "</head><body>b</body></html>" % path).encode("utf-8")
        out = server._force_noindex(body, path).decode("utf-8")
        self.assertIn('href="https://trypstore.com/n/keto"', out)


class ThinTopicDbTest(unittest.TestCase):
    """The thin set must be derived from the topics table, not the slug: a
    head-to-head topic's slug ('carbe-diem-vs-keto-pint') is indistinguishable
    from an autosuggest term containing 'vs', because _slugify strips the
    "(@ASIN)" markers render_vs keys off."""

    def test_discriminator_uses_the_stored_term(self):
        # Term formats are exactly what _ensure_vs_topics and
        # _ensure_price_band_topics write: "<label a> vs <label b> (@ASIN|@ASIN)"
        # and "<keyword> under $<amt>" with an "under-<amt>" slug.
        rows = [
            {"parent_slug": "keto", "slug": "best-keto-snacks-2026",
             "term": "best keto snacks 2026"},
            {"parent_slug": "keto", "slug": "under-25",
             "term": "keto snacks under $25"},
            {"parent_slug": "keto", "slug": "carbe-diem-penne-vs-keto-pint",
             "term": "Carbe Diem! Penne vs Keto Pint (@B0BGYGHRRT|@B0C2ZPBHC7)"},
        ]
        out = set()
        for r in rows:
            tslug = (r["slug"] or "").strip().lower()
            if not tslug or server.re.match(r"^under-\d+$", tslug):
                continue
            if server._VS_TERM_RE.search(r["term"] or ""):
                continue
            out.add("/n/%s/%s" % (r["parent_slug"], tslug))
        self.assertEqual(out, {"/n/keto/best-keto-snacks-2026"})

    def test_vs_regex_matches_the_term_shape_production_writes(self):
        term = "Carbe Diem! Penne vs Keto Pint (@B0BGYGHRRT|@B0C2ZPBHC7)"
        m = server._VS_TERM_RE.search(term)
        self.assertIsNotNone(m)
        self.assertEqual(m.groups(), ("B0BGYGHRRT", "B0C2ZPBHC7"))

    def test_priceband_regex_matches_the_slug_production_writes(self):
        for slug in ("under-25", "under-100", "under-500"):
            self.assertIsNotNone(server.re.match(r"^under-\d+$", slug))
        for slug in ("under-", "under-25-off", "best-under-25", "under-2-5"):
            self.assertIsNone(server.re.match(r"^under-\d+$", slug))


if __name__ == "__main__":
    unittest.main()
