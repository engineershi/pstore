"""Regression tests for topical consolidation holds.

Search Console reports the site as fully indexed (URL Inspection verdict PASS,
coverage "Submitted and indexed", robots ALLOWED) while delivering 0
impressions — so the failure is ranking, not indexability. Cause: 50 niche URLs
competing for ~16 intents (92% of pages sit in a duplicate family), including
singular/plural twins and "best griddle" / "best griddle pan" /
"best griddle electric" style modifier variants.

Consolidation parks the out-of-focus pages as noindex while leaving them fully
live for visitors, and concentrates authority on the pages that are kept.
"""
import unittest

import seo
import server


def _niche():
    return {"products": [{"asin": "B01", "title": "Keto Bar", "price": 12.99,
                          "stars": 4.5, "reviews": 3100,
                          "url": "https://www.amazon.com/dp/B01"}]}


class TestHoldRendering(unittest.TestCase):
    def _html(self, res):
        return res.decode("utf-8", "replace") if isinstance(res, bytes) else res

    def test_niche_held_is_noindex_but_still_live(self):
        held = self._html(seo.render_niche("best keto", _niche(), hold=True))
        self.assertIn("noindex", held)
        # the page must still render its products — a held page is parked,
        # not deleted, so existing links and traffic keep working
        self.assertIn("Keto Bar", held)
        self.assertIn("amazon.com", held)

    def test_niche_unheld_is_indexable(self):
        self.assertNotIn("noindex", self._html(seo.render_niche("best keto", _niche())))

    def test_topic_held_is_noindex(self):
        held = self._html(seo.render_topic("keto snacks", "best keto", _niche(),
                                           "best-keto", hold=True))
        self.assertIn("noindex", held)
        live = self._html(seo.render_topic("keto snacks", "best keto", _niche(),
                                           "best-keto"))
        self.assertNotIn("noindex", live)

    def test_default_is_not_held(self):
        """Omitting the flag must preserve today's behaviour."""
        self.assertFalse(self._html(seo.render_niche("best keto", _niche())).count("noindex"))

    def test_held_and_productless_both_noindex(self):
        empty = {"products": []}
        self.assertIn("noindex", self._html(seo.render_niche("x", empty, hold=True)))


class TestHoldStore(unittest.TestCase):
    """The hold set is a single settings row.

    These tests patch the store rather than hitting the DB on purpose: the full
    suite runs real HTTP servers on background threads against one shared
    sqlite file, and `_get_setting`/`_set_setting` deliberately swallow
    exceptions. A transient "database is locked" there makes a real write
    silently vanish, which is safe in production (the read fails *open*, so a
    held page is briefly indexable again) but makes any test that asserts on a
    round-trip flaky. Patching keeps this deterministic.
    """

    def setUp(self):
        self.store = {}
        self._g = server._get_setting
        self._s = server._set_setting

        def fake_get(key, default=""):
            return self.store.get(key, default)

        def fake_set(key, value):
            self.store[key] = value or ""

        server._get_setting = fake_get
        server._set_setting = fake_set
        server._HOLDS_CACHE.update({"key": None, "at": 0.0, "val": frozenset()})

    def tearDown(self):
        server._get_setting = self._g
        server._set_setting = self._s
        server._HOLDS_CACHE.update({"key": None, "at": 0.0, "val": frozenset()})

    def test_empty_by_default(self):
        self.assertEqual(server._consolidation_holds(), frozenset())

    def test_round_trip(self):
        server._set_consolidation_holds(["best-griddle-pan", "best-cooler"])
        self.assertEqual(server._consolidation_holds(),
                         frozenset({"best-griddle-pan", "best-cooler"}))

    def test_strips_whitespace_and_drops_blanks(self):
        got = server._set_consolidation_holds(["  a  ", "", "b", "   ", None])
        self.assertEqual(got, frozenset({"a", "b"}))
        self.assertEqual(server._consolidation_holds(), frozenset({"a", "b"}))

    def test_clear_restores_everything(self):
        server._set_consolidation_holds(["a", "b"])
        server._set_consolidation_holds([])
        self.assertEqual(server._consolidation_holds(), frozenset())

    def test_deduplicates(self):
        got = server._set_consolidation_holds(["a", "a", "A"])
        self.assertEqual(got, frozenset({"a", "A"}))

    def test_malformed_input_is_survivable(self):
        server._set_setting("seo.consolidation.holds", "a,,b,  ,c")
        server._HOLDS_CACHE.update({"key": None, "at": 0.0, "val": frozenset()})
        self.assertEqual(server._consolidation_holds(), frozenset({"a", "b", "c"}))

    def test_write_failure_fails_open(self):
        """A DB error must never mark held pages indexable-by-accident... it
        must fall back to holding nothing, which is the pre-consolidation state
        and therefore the safe one."""
        def boom(key, value):
            raise RuntimeError("database is locked")
        server._set_setting = boom
        self.assertEqual(server._consolidation_holds(), frozenset())

    def test_cache_short_circuits_between_reads(self):
        """Two reads in a row must not hit the DB twice — this runs on every
        /n/ render. A write deliberately invalidates the cache, so the store is
        mutated *after* the first read to prove the second one is served from
        memory."""
        first = server._consolidation_holds()
        self.store["seo.consolidation.holds"] = "a,b,c"
        self.assertEqual(server._consolidation_holds(), first)

    def test_write_invalidates_cache(self):
        server._consolidation_holds()                 # prime the cache
        server._set_consolidation_holds(["fresh"])
        self.assertEqual(server._consolidation_holds(), frozenset({"fresh"}))


if __name__ == "__main__":
    unittest.main()