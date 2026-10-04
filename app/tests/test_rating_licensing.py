# -*- coding: utf-8 -*-
"""One chokepoint for Amazon rating licensing, and a net over every surface
that used to print a scraped one.

Amazon's Operating Agreement (updated 14 Apr 2026) allows displaying customer
reviews or star ratings only when they were obtained through the Creators API /
PA API. We hold no such credentials, so every rating on this site comes from a
scrape and none of it may be republished — not on a page, not in a share image,
not in an email, not in generated copy, not in JSON-LD.

This file guards two things:

  1. `amazon.licensed_rating` — the single place that decides, so the rule is
     stated once and there is a single thing to audit.
  2. The reader-facing surfaces that render product data — each one that had a
     leak is named individually and asserted, so a regression in any of them
     fails here rather than in an inbox.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import amazon
import cms
import cms_render
import editorial
import mailer
import market_engine
import seo

#: A product as the scraper returns it: a real star rating we may NOT print.
SCRAPED = {"asin": "B0AAA", "title": "Breville Barista Express",
           "price": 499.95, "stars": 4.6, "reviews": 1234,
           "currency": "USD", "source": "amazon"}

#: The same product as PA-API returns it: licensed for display.
LICENSED = dict(SCRAPED, source="paapi")

#: Claims about a product's reviews that are unsupportable without a licensed
#: number behind them. The old copy reached for these as a fallback; with the
#: number withheld they became bare assertions about someone else's customers.
REVIEW_CLAIMS = ("most-reviewed", "most reviewed", "highly rated", "well reviewed",
                 "best-reviewed", "top-rated", "highest-social-proof")


class TestChokepoint(unittest.TestCase):
    def test_scraped_rating_is_never_licensed(self):
        self.assertEqual((None, None), amazon.licensed_rating(SCRAPED))

    def test_paapi_rating_is_licensed(self):
        self.assertEqual((4.6, 1234), amazon.licensed_rating(LICENSED))

    def test_missing_and_malformed_input_is_safe(self):
        for bad in (None, {}, {"source": "paapi"}, {"stars": "4.5", "source": "x"}):
            self.assertEqual((None, None), amazon.licensed_rating(bad))

    def test_a_similar_source_name_is_not_a_pass(self):
        # "paapi_ish" must not be mistaken for the API path; the check is exact.
        self.assertEqual((None, None),
                         amazon.licensed_rating(dict(SCRAPED, source="paapi-ish")))

    def test_every_surface_shares_this_one_function(self):
        # seo and editorial must delegate rather than keep private copies, or the
        # rule can drift apart between pages.
        self.assertEqual(amazon.licensed_rating(SCRAPED), seo._licensed_rating(SCRAPED))
        self.assertEqual(amazon.licensed_rating(SCRAPED), editorial._licensed(SCRAPED))


class TestSeoSurfaces(unittest.TestCase):
    def test_comparison_cells_are_blank_when_unlicensed(self):
        rows = editorial.comparison_rows([SCRAPED, LICENSED])
        by_name = {r.get("title") or r.get("name"): r for r in rows}
        self.assertNotIn("★", str(editorial.comparison_rows([SCRAPED])))
        self.assertIn("★", str(editorial.comparison_rows([LICENSED])))

    def test_jsonld_omits_aggregate_rating_when_unlicensed(self):
        graph = seo._product_graph([SCRAPED, LICENSED])
        rated = [n for n in graph if "aggregateRating" in n]
        self.assertEqual(1, len(rated))
        self.assertEqual(LICENSED["title"], rated[0]["name"])

    def test_story_slides_carry_no_rating(self):
        slides = seo.story_cards("espresso machine", [SCRAPED],
                                 base_url="https://trypstore.com")
        html = "".join(seo._story_slide_html(s) for s in slides)
        self.assertNotIn("/ 5 from", html)
        self.assertNotIn("★", html)


class TestBuyerCopySurfaces(unittest.TestCase):
    """market_engine generates the emails, captions, DM scripts and promo copy
    that used to carry 'highly rated' / 'most-reviewed' in six places."""

    def _all_copy(self):
        items = [SCRAPED]
        out = [
            market_engine.build_landing_page("espresso machine", items),
            str(market_engine.build_email_sequence("espresso machine", items)),
            str(market_engine.build_social_pack("espresso machine", items)),
            str(market_engine.build_dm_conversation("espresso machine", items)),
            str(market_engine.build_boost_campaigns(
                "espresso machine", items, base_url="https://trypstore.com")),
        ]
        return "\n".join(x for x in out if x)

    def test_no_unlicensed_rating_or_claim_in_generated_copy(self):
        blob = self._all_copy()
        self.assertNotIn("4.6", blob)
        self.assertNotIn("1,234", blob)
        for claim in REVIEW_CLAIMS:
            self.assertNotIn(claim, blob.lower(),
                             "generated copy still claims %r" % claim)

    def test_copy_still_sells_with_an_honest_proof_line(self):
        # Gating must not gut the asset: the fallback is our own ranking work
        # plus the live price, which we are allowed to quote.
        blob = self._all_copy()
        self.assertIn("top-ranked pick", blob)
        self.assertIn("$499.95", blob)

    def test_licensed_rating_does_reach_the_copy(self):
        blob = "\n".join([
            market_engine.build_landing_page("espresso", [LICENSED]),
            str(market_engine.build_social_pack("espresso", [LICENSED])),
        ])
        self.assertIn("4.6", blob)


class TestEmailSurface(unittest.TestCase):
    def test_card_hides_an_unlicensed_rating(self):
        card = mailer.product_card_html(SCRAPED, link_url="https://x.com/a")
        self.assertNotIn("★", card)

    def test_card_shows_a_licensed_rating(self):
        card = mailer.product_card_html(LICENSED, link_url="https://x.com/a")
        self.assertIn("★", card)
        self.assertIn("1,234", card)

    def test_card_cannot_be_forced_by_the_caller(self):
        # No stars= parameter exists any more: a caller must not be able to push
        # an unlicensed number into an email by passing it in.
        with self.assertRaises(TypeError):
            mailer.product_card_html(SCRAPED, link_url="https://x.com/a",
                                     stars=4.6, reviews=1234)


def _cms_conn():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cms.ensure_tables(conn)
    return conn


class TestCmsSurface(unittest.TestCase):
    def test_render_context_never_receives_an_unlicensed_rating(self):
        conn = _cms_conn()
        self.addCleanup(conn.close)
        out = cms.build_page_context(conn, "espresso", {"products": [SCRAPED]})
        self.assertEqual("", out["ctx"].get("stars"))

    def test_render_context_carries_a_licensed_rating(self):
        conn = _cms_conn()
        self.addCleanup(conn.close)
        out = cms.build_page_context(conn, "espresso", {"products": [LICENSED]})
        self.assertEqual("4.6", out["ctx"].get("stars"))

    def test_hero_prints_no_rating_for_a_scraped_pick(self):
        html = cms_render._section_html(
            {"_type": "hero", "headline": "The #1 pick"},
            {"pick": SCRAPED, "items": [SCRAPED]})
        self.assertNotIn("stars-line", html)

    def test_hero_prints_a_licensed_rating(self):
        html = cms_render._section_html(
            {"_type": "hero", "headline": "The #1 pick"},
            {"pick": LICENSED, "items": [LICENSED]})
        self.assertIn("stars-line", html)


if __name__ == "__main__":
    unittest.main()