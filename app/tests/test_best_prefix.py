"""Regression tests for the doubled "Best best ..." heading bug.

Every stored niche keyword is phrased "best <thing>", and the H1 templates did
`"Best %s" % keyword` unconditionally. That shipped indexable headings reading:

    <h1>Best best camping tent 2 person: ranked picks</h1>

`_guide_title` already guarded this with a `has_best` check; the two H1
templates did not. Fixed by routing both through `seo._best_prefixed`.
"""
import re
import unittest

import seo

ITEMS = [{"asin": "B0%d" % i, "title": "Item %d" % i, "price": 9.99,
          "stars": 4.5, "reviews": 1200, "url": "https://www.amazon.com/dp/B0%d" % i}
         for i in range(4)]


def _text(html):
    return html.decode("utf-8", "replace") if isinstance(html, bytes) else html


def _h1(html):
    m = re.findall(r"<h1[^>]*>(.*?)</h1>", _text(html), re.S)
    return re.sub(r"<[^>]+>", "", m[0]).strip() if m else ""


class TestBestPrefixed(unittest.TestCase):
    def test_leaves_existing_best_alone(self):
        for kw in ("best camping tent", "Best Griddle Pan", "BEST keto",
                   "best-of-the-best cooler"):
            self.assertEqual(seo._best_prefixed(kw), kw)

    def test_adds_best_when_absent(self):
        self.assertEqual(seo._best_prefixed("camping tent"), "Best camping tent")
        self.assertEqual(seo._best_prefixed("keto snacks"), "Best keto snacks")

    def test_empty_and_none_are_safe(self):
        self.assertEqual(seo._best_prefixed(""), "")
        self.assertEqual(seo._best_prefixed(None), "")
        self.assertEqual(seo._best_prefixed("   "), "")

    def test_only_whole_word_best_counts(self):
        # "bestow"/"bestie" are not the word "best"
        self.assertEqual(seo._best_prefixed("bestowable baskets"),
                         "Best bestowable baskets")

    def test_bare_best_keyword_not_doubled(self):
        self.assertEqual(seo._best_prefixed("best"), "best")


class TestClipWords(unittest.TestCase):
    def test_passthrough_when_short(self):
        self.assertEqual(seo._clip_words("abc", 110), "abc")

    def test_never_exceeds_limit(self):
        self.assertLessEqual(len(seo._clip_words("word " * 100, 110)), 110)

    def test_cuts_on_word_boundary(self):
        src = "alpha bravo charlie delta echo foxtrot"
        out = seo._clip_words(src, 20)
        # A valid cut is one of the whole-word prefixes, never a fragment.
        self.assertIn(out, {"alpha", "alpha bravo", "alpha bravo charlie"})
        self.assertTrue(src.startswith(out))

    def test_cut_never_leaves_a_hanging_word(self):
        src = "alpha bravo charlie delta echo foxtrot"
        for limit in range(1, len(src) + 2):
            out = seo._clip_words(src, limit)
            self.assertTrue(src.startswith(out), "not a prefix at %d" % limit)
            self.assertNotIn("  ", out)
            if out and out != src:
                self.assertFalse(out.endswith(" "), "trailing space at %d" % limit)

    def test_zero_and_negative_limits(self):
        self.assertEqual(seo._clip_words("abc", 0), "abc")
        self.assertEqual(seo._clip_words("abc", -5), "abc")


class TestNicheH1(unittest.TestCase):
    def test_keyword_starting_with_best_is_not_doubled(self):
        html = seo.render_niche("best camping tent 2 person", {"products": ITEMS})
        self.assertNotIn("Best best", _h1(html))
        # The H1 is title-cased (editorial._title_kw): mined keywords arrive
        # lower-case from autosuggest and a lower-case H1 gets rewritten by
        # Google, so compare case-insensitively -- the intent of this test is
        # the single leading "Best", not the casing.
        self.assertIn("best camping tent 2 person", _h1(html).lower())

    def test_h1_is_title_cased_not_raw_slug_case(self):
        """A lower-case H1 reads as a slug, not a headline, and Google
        rewrites it. Guard the casing explicitly so it cannot regress."""
        h1 = _h1(seo.render_niche("best lawn mower battery",
                                  {"products": ITEMS}))
        self.assertEqual(h1, "Best Lawn Mower Battery: Ranked Picks")

    def test_plain_keyword_still_gets_best(self):
        self.assertTrue(_h1(seo.render_niche("camping tent",
                                             {"products": ITEMS})).lower()
                        .startswith("best camping tent"))

    def test_case_insensitive_guard(self):
        self.assertNotIn("Best Best", _h1(seo.render_niche("Best Griddle Pan",
                                                           {"products": ITEMS})))

    def test_headline_stays_bounded(self):
        kw = "best " + "very long descriptor " * 12
        h1 = _h1(seo.render_niche(kw, {"products": ITEMS}))
        self.assertLessEqual(len(h1), 110)
        self.assertNotIn("Best best", h1)

    def test_headline_survives_ab_headline(self):
        """An A/B override must win over the generated headline."""
        kw = "best camping tent 2 person"
        html = seo.render_niche(kw, {"products": ITEMS},
                                ab_headline="Camping Tent Showdown")
        self.assertIn("Camping Tent Showdown", _h1(html))


class TestTopicH1(unittest.TestCase):
    def test_term_starting_with_best_is_not_doubled(self):
        html = seo.render_topic("best keto bars", "keto snacks",
                                {"products": ITEMS}, "keto-snacks")
        self.assertNotIn("Best best", _h1(html))

    def test_plain_term_still_gets_best(self):
        html = seo.render_topic("keto bars", "keto snacks",
                                {"products": ITEMS}, "keto-snacks")
        self.assertNotIn("Best best", _h1(html))
        self.assertIn("keto bars", _h1(html).lower())

    def test_topic_h1_is_title_cased(self):
        self.assertEqual(
            _h1(seo.render_topic("best keto snack bars", "keto snacks",
                                 {"products": ITEMS}, "keto-snacks")),
            "Best Keto Snack Bars")

    def test_no_doubled_best_anywhere_in_topic_page(self):
        """Full-page sweep: the bug reached the H1, the lede, the <h2>, the
        lead-gate heading, the ItemList JSON-LD name, the FAQ question and the
        meta description."""
        for term, parent in (("best keto bars", "keto snacks"),
                             ("keto bars", "keto snacks"),
                             ("best-of-the-best cooler", "coolers")):
            html = _text(seo.render_topic(term, parent, {"products": ITEMS},
                                          "keto-snacks"))
            self.assertNotIn("best best", html.lower(),
                             "doubled Best in topic page for %r" % term)

    def test_no_doubled_best_anywhere_in_niche_page(self):
        for kw in ("best camping tent", "camping tent", "best keto snacks"):
            html = _text(seo.render_niche(kw, {"products": ITEMS}))
            self.assertNotIn("best best", html.lower(),
                             "doubled Best in niche page for %r" % kw)

    def test_long_term_headline_is_bounded(self):
        html = seo.render_topic("best " + "long tail phrase " * 12,
                                "keto snacks", {"products": ITEMS}, "keto-snacks")
        self.assertLessEqual(len(_h1(html)), 110)


class TestGuideTitleUnaffected(unittest.TestCase):
    def test_guide_title_still_leads_with_best_once(self):
        t = seo._guide_title("best camping tent 2 person", ITEMS)
        self.assertNotIn("Best best", t)
        self.assertTrue(t.startswith("Best Camping Tent"))
        self.assertLessEqual(len(t), 60)

    def test_guide_title_respects_60_budget(self):
        long_kw = "best " + "x" * 120
        self.assertLessEqual(len(seo._guide_title(long_kw, ITEMS)), 60)


if __name__ == "__main__":
    unittest.main()
