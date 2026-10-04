# -*- coding: utf-8 -*-
"""The internal link graph, which is the only thing a crawler actually follows.

The live site listed 1,321 self-canonical, `index,follow` URLs and had received
zero impressions from Google, Bing, Yandex and DuckDuckGo. Crawl and schema
checks had already come back clean, so the graph itself was the remaining
suspect, and it was broken in a specific way.

`related_niches` sorted its candidates by `sha1(candidate_keyword)` -- a key
built from the candidate alone, with no reference to the page being rendered.
Every page therefore returned the *identical* six niches. Six pages took roughly
1,300 inbound internal links each and the other ~1,300 took none. A sitemap is a
discovery hint, not a link graph, so the result was one very fat hub and a long
tail of orphans: 43% of the inventory reachable at depth 1, and a sampled /n/
page linking to zero other content pages (mean fan-out 0.4).

These tests pin the property that fixes it: siblings are chosen by topical
overlap, and no six pages hold the whole site's links.
"""
import unittest

import editorial


def _kw(*names):
    return [{"keyword": n, "products": []} for n in names]


#: Shaped like the real inventory: a handful of topic families with members.
FAMILIES = (
    _kw("jewelry", "gold jewelry", "jewelry box", "jewelry cleaner",
        "jewelry organizer", "ring holder")
    + _kw("yoga", "yoga mat", "yoga blocks", "yoga pants", "crz yoga",
          "yoga strap")
    + _kw("keto", "keto bread", "keto snacks", "keto coffee")
    + _kw("sleep", "sleep mask", "sleep sack", "mellow sleep pillow",
          "sleeping bag")
    + _kw("dog toys", "dog toys for aggressive chewers", "kong dog toys",
          "dog leash")
    + _kw("kitchen", "kitchen towels", "kitchen rugs", "kitchen trash can",
          "kitchen appliances")
    + _kw("back pain", "back-pain", "back pain relief products",
          "back massager for pain relief deep tissue")
    + _kw("desk-lamp", "desk-lamps")
)


def _related(current, pool=None):
    pool = pool if pool is not None else FAMILIES
    return [n["keyword"] for n in editorial.related_niches(current, pool)]


class TestSiblingsAreTopical(unittest.TestCase):
    def test_jewelry_box_finds_the_jewelry_family(self):
        rel = _related("jewelry box")
        for want in ("gold jewelry", "jewelry", "jewelry cleaner",
                     "jewelry organizer"):
            self.assertIn(want, rel)

    def test_yoga_mat_finds_the_yoga_family(self):
        rel = _related("yoga mat")
        for want in ("yoga", "yoga blocks", "yoga pants"):
            self.assertIn(want, rel)

    def test_no_cross_family_leaks_in(self):
        """A jewelry page must not be sent to a yoga page: the block is the
        topical signal, and a wrong sibling costs more than a missing one."""
        rel = _related("jewelry cleaner")
        self.assertNotIn("yoga mat", rel)
        self.assertNotIn("keto bread", rel)

    def test_most_specific_match_ranks_first(self):
        """"sleep" alone is a weak match; a two-token sibling that contains it
        ("sleep mask") is a stronger one. Which of two equally specific
        siblings wins is deliberately arbitrary -- the tie-break is salted per
        page -- so this pins the ranking, not a particular winner."""
        cur = editorial._rel_tokens("sleep")
        rel = _related("sleep")
        first = rel[0]
        cover = len(cur & editorial._rel_tokens(first)) / float(
            len(editorial._rel_tokens(first)))
        self.assertGreater(cover, 1.0 / 3,
                           "%r is only a bare-word match for 'sleep'" % first)
        self.assertNotIn("sleeping bag", rel[:1])   # 1 of 3 tokens

    def test_plural_and_inflection_do_not_split_a_family(self):
        """`desk-lamp` vs `desk-lamps`, and above all `sleep` vs `sleeping`:
        without inflection handling a sleeping bag shares nothing with the
        sleep family and gets filed by the fallback."""
        rel = _related("sleeping bag")
        for want in ("sleep", "sleep mask", "mellow sleep pillow"):
            self.assertIn(want, rel)

        rel = _related("desk-lamps")
        self.assertIn("desk-lamp", rel)

    def test_punctuation_does_not_split_a_family(self):
        self.assertIn("back-pain", _related("back pain"))
        self.assertIn("back pain", _related("back-pain"))

    def test_self_is_never_returned(self):
        for kw in ("jewelry", "sleep mask", "keto", "dog toys"):
            self.assertNotIn(kw, _related(kw))

    def test_stopwords_cannot_carry_a_match(self):
        """"best" and "for" appear in most of the inventory. A block built on
        them would put every page next to every other page again -- so the
        real sibling has to outrank the stopword-mates, not merely be present
        somewhere in the list."""
        pool = _kw(*["best %s" % w for w in
                     ("yoga mat", "coffee", "running shoes", "phone case",
                      "desk lamp", "water bottle", "backpack", "winter coat",
                      "guitar", "camera", "monitor", "keyboard")])
        rel = _related("best air fryer", pool)
        # Nothing here shares a subject with an air fryer, so the whole block
        # is fallback -- and none of it may be ranked as a topical match.
        self.assertNotIn("best yoga mat", rel[:3])
        ranked_as_match = [kw for kw in rel
                           if editorial._rel_tokens("best air fryer") &
                           editorial._rel_tokens(kw)]
        self.assertEqual([], ranked_as_match)


class TestNoPageHoldsTheWholeSite(unittest.TestCase):
    """The actual regression: a stable, popular subset of pages taking every
    internal link on the site while the rest take none."""

    def test_pages_do_not_all_return_the_same_siblings(self):
        picks = [_related(n["keyword"]) for n in FAMILIES]
        self.assertGreater(len(set(tuple(p) for p in picks)), 1)

    def test_no_set_of_six_dominates_a_large_inventory(self):
        """At the real inventory size the old code returned the same six for
        every page. Any six pages must not be the answer for the whole site."""
        pool = _kw(*["niche %d" % i for i in range(200)])
        picks = [tuple(_related(n["keyword"], pool)) for n in pool[:120]]
        frequency = {}
        for p in picks:
            for kw in p:
                frequency[kw] = frequency.get(kw, 0) + 1
        worst = max(frequency.values())
        # With a per-page salted fallback, concentration is spread; the old
        # code put 100% of picks on six pages.
        self.assertLess(worst / float(len(picks)), 0.60,
                        "links are concentrating again: %r" %
                        sorted(frequency.items(), key=lambda kv: -kv[1])[:8])

    def test_a_keyword_with_no_neighbours_still_gets_a_block(self):
        """The fallback exists so no page renders an empty "Keep exploring"."""
        pool = _kw("aardvark alarm", "zeppelin lamp", "quokka phone case")
        rel = _related("aardvark alarm", pool)
        self.assertEqual(2, len(rel))
        self.assertNotIn("aardvark alarm", rel)

    def test_empty_pool_is_safe(self):
        self.assertEqual([], editorial.related_niches("jewelry", []))
        self.assertEqual([], editorial.related_niches("jewelry", None))


class TestRenderedPageCarriesTheBlock(unittest.TestCase):
    """The unit above is only worth anything if it reaches the HTML."""

    def test_topic_page_links_to_a_real_sibling(self):
        import seo
        nice = {"keyword": "jewelry box", "created_at": "2026-01-02",
                "products": [{"asin": "B00000001", "title": "A box",
                              "price": 24.5, "rating": 4.6, "reviews": 812,
                              "img": "/i/1.png", "brand": "Acme", "url": "u",
                              "aff": "/go/B00000001"}]}
        sib = {"keyword": "jewelry cleaner", "created_at": "2026-01-01",
               "products": [{"asin": "B00000002", "title": "A cleaner",
                             "price": 12.0, "rating": 4.2, "reviews": 90,
                             "img": "/i/2.png", "brand": "Acme", "url": "u",
                             "aff": "/go/B00000002"}]}
        html = seo.render_topic("under 25", "jewelry box under $25", nice,
                                "jewelry-box",
                                saved_niches=[nice, sib]).decode()
        self.assertIn("/n/jewelry-cleaner", html)
        self.assertIn('href="/n/jewelry-box-under-25"', html)


if __name__ == "__main__":
    unittest.main()