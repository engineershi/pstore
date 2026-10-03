"""Tests for the three growth-safety changes added 2026-10-02.

1. PA-API product enrichment — fills the missing product image that made every
   ranking page render zero <img>, no-ops when PA-API is unconfigured, and
   never overwrites a live scraped value with a cached one.
2. /lp/ noindex — landing pages are conversion destinations, not search
   destinations; they stay live but leave the index and the sitemap, and the
   page estimate must agree with the sitemap that is actually emitted.
3. Cannibalization auto-parking — only objectively duplicated pages are held,
   the keeper of each group is never held, shadowed slug rows are reported
   rather than auto-deleted, and applying is reversible and idempotent.
"""
import unittest

import server
import paapi
import earnings
import os


_TMP = []


def setUpModule():
    """Own a private database, seeded from the tracked 43-niche file.

    Necessary because neighbouring modules leave `PSTORE_DB` pointing at files
    they have already deleted: tests/test_pstore.py unlinks its
    `/tmp/pstore_test_sec_*.db` in tearDownClass, and tests/test_hold_route_family.py
    removes its tempdir. Since Python caches module objects, whichever file runs
    next keeps querying that dead path and dies with 'no such table: settings'.
    Creating our own copy is the only order-independent fix, and it also keeps
    the committed seed untouched.

    `server.DB` is rebound rather than the module reloaded: reloading would
    invalidate the `server._cannibalization_report` references already captured
    by the tests below.
    """
    import shutil
    import tempfile
    seed = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "pstore.db")
    tmp = tempfile.mkdtemp(prefix="growth_safety_")
    _TMP.append(tmp)
    db = os.path.join(tmp, "growth.db")
    shutil.copy(seed, db)
    os.environ["PSTORE_DB"] = db
    server.DB = db
    server._db_schema_ready = False
    server._init()
    server._HOLDS_CACHE.update({"key": None, "at": 0.0, "val": frozenset()})
    server._CANNIBAL_CACHE.update({"val": None, "at": 0.0})
    server._LP_INDEX_CACHE.update({"value": None, "at": 0.0})


def tearDownModule():
    import shutil
    server._set_setting("seo.lp_index", "")
    server._set_setting("seo.consolidation.holds", "")
    server._LP_INDEX_CACHE.update({"value": None, "at": 0.0})
    server._HOLDS_CACHE.update({"key": None, "at": 0.0, "val": None})
    server._CANNIBAL_CACHE.update({"val": None, "at": 0.0})
    for tmp in _TMP:
        shutil.rmtree(tmp, ignore_errors=True)
    _TMP[:] = []


class PAAPIEnrichmentTests(unittest.TestCase):
    def _items(self, payload):
        return {"ItemsResult": {"Items": payload}}

    def _stub(self, payload):
        """Install a fake GetItems and mark PA-API as configured."""
        original_cfg, original_get = paapi.ready, paapi.get_items
        paapi.configure("AK", "SK", "PT-20")
        paapi.get_items = lambda asins: self._items(payload)
        self.addCleanup(lambda: (paapi.configure(original_cfg() or "", "",
                                              original_cfg() or ""),
                                 setattr(paapi, "get_items", original_get)))
        self.addCleanup(setattr, paapi, "get_items", original_get)
        # restore empty creds so nothing else in the suite sees them
        self.addCleanup(lambda: paapi.configure("", "", ""))

    def test_noop_when_paapi_unconfigured(self):
        """The scraping fallback must be completely untouched by this change."""
        paapi.configure("", "", "")
        products = [{"asin": "B000000001", "price": "9.99", "title": "X"}]
        self.assertEqual(server._enrich_products(products), products)

    def test_fills_missing_image(self):
        self._stub([{"ASIN": "B000000001",
                    "ItemInfo": {"Title": {"DisplayValue": "T"}},
                    "Offers": {"Listings": [{"Price": {"Amount": 11.5,
                                                       "Currency": "USD"}}]},
                    "Images": {"Primary": {"Large": {
                        "URL": "https://m-images-na.ssl-images-amazon.com"
                               "/images/I/71a._AC_.jpg"}}}}])
        out = server._enrich_products([{"asin": "B000000001"}])
        self.assertTrue(out[0]["image"].endswith("71a._AC_.jpg"))
        self.assertEqual(out[0]["title"], "T")
        self.assertEqual(out[0]["currency"], "USD")

    def test_never_overwrites_existing_values(self):
        """A live scraped price beats a cached PA-API price for freshness."""
        self._stub([{"ASIN": "B000000001",
                    "ItemInfo": {"Title": {"DisplayValue": "PAAPI"}},
                    "Offers": {"Listings": [{"Price": {"Amount": 99.0}}]},
                    "Images": {"Primary": {"Large": {
                        "URL": "https://m-images-na.ssl-images-amazon.com"
                               "/images/I/71b._AC_.jpg"}}}}])
        out = server._enrich_products([{"asin": "B000000001",
                                        "title": "Scraped",
                                        "price": "9.99"}])
        self.assertEqual(out[0]["price"], "9.99")
        self.assertEqual(out[0]["title"], "Scraped")
        self.assertTrue(out[0]["image"])

    def test_complete_product_untouched(self):
        """No network call is warranted when nothing is missing."""
        called = []

        def boom(asins):
            called.append(asins)
            return None

        original = paapi.get_items
        paapi.configure("AK", "SK", "PT-20")
        paapi.get_items = boom
        try:
            out = server._enrich_products([{"asin": "B1", "title": "T",
                                            "price": "1.00", "image": "x"}])
        finally:
            paapi.get_items = original
            paapi.configure("", "", "")
        self.assertEqual(called, [])
        self.assertEqual(out[0]["image"], "x")

    def test_batches_ten_at_a_time(self):
        seen = []
        original = paapi.get_items
        paapi.configure("AK", "SK", "PT-20")

        def rec(asins):
            seen.append(list(asins))
            return None

        paapi.get_items = rec
        try:
            server._enrich_products([{"asin": "B%03d" % i}
                                     for i in range(25)])
        finally:
            paapi.get_items = original
            paapi.configure("", "", "")
        self.assertEqual([len(b) for b in seen], [10, 10, 5])

    def test_never_raises(self):
        def boom(asins):
            raise RuntimeError("network down")

        original = paapi.get_items
        paapi.configure("AK", "SK", "PT-20")
        paapi.get_items = boom
        try:
            out = server._enrich_products([{"asin": "B1"}])
        finally:
            paapi.get_items = original
            paapi.configure("", "", "")
        self.assertEqual(out, [{"asin": "B1"}])

    def test_normalize_item_matches_amazon_image_allowlist(self):
        """The stored URL must actually survive editorial.image_url()."""
        import editorial
        url = ("https://m-images-na.ssl-images-amazon.com/images/I/"
               "71abc._AC_SX679_.jpg")
        norm = paapi.normalize_item(
            {"ASIN": "B1", "Images": {"Primary": {"Large": {"URL": url}}}})
        self.assertEqual(editorial.image_url(norm), url)


class LandingPageNoindexTests(unittest.TestCase):
    def _force(self, value):
        server._set_setting("seo.lp_index", value)
        server._LP_INDEX_CACHE.update({"value": None, "at": 0.0})
        self.addCleanup(lambda: (server._set_setting("seo.lp_index", ""),
                                 server._LP_INDEX_CACHE.update(
                                     {"value": None, "at": 0.0})))

    def test_landing_page_noindex_by_default(self):
        """511 pages at 97% similarity, zero impressions. They must stop
        competing with the /n/ hub they were duplicating."""
        body = b"<html><head></head><body>x</body></html>"
        self.assertIn(b"noindex", server._force_noindex(body, "/lp/anything"))

    def test_page_stays_live(self):
        """noindex must only affect search, not visitors or conversions."""
        server._LP_INDEX_CACHE.update({"value": False, "at": 0.0})
        body = b"<html><head></head><body><a href='#'>buy</a></body></html>"
        out = server._force_noindex(body, "/lp/keto")
        self.assertIn(b"buy", out)

    def test_hub_and_home_stay_indexable(self):
        server._LP_INDEX_CACHE.update({"value": False, "at": 0.0})
        body = b"<html><head></head><body>x</body></html>"
        for path in ("/n/keto", "/", "/blog", "/stories/keto", "/lp2"):
            self.assertNotIn(b"noindex", server._force_noindex(body, path),
                             "%s must stay indexable" % path)

    def test_reversible_via_setting(self):
        """One setting restores the old behaviour with no redeploy."""
        self._force("1")
        body = b"<html><head></head><body>x</body></html>"
        self.assertNotIn(b"noindex", server._force_noindex(body, "/lp/keto"))

    def test_missing_robots_meta_gets_one_inserted(self):
        body = b"<html><head><title>t</title></head><body>x</body></html>"
        out = server._force_noindex(body, "/lp/keto")
        self.assertIn(b'<meta name="robots" content="noindex, follow">', out)

    def test_existing_robots_meta_is_replaced_not_duplicated(self):
        body = (b"<html><head><meta name=robots content=\"index, follow\">"
                b"</head><body>x</body></html>")
        out = server._force_noindex(body, "/lp/keto")
        self.assertEqual(out.count(b'name="robots"'), 1)
        self.assertIn(b"noindex", out)

    def test_idempotent(self):
        body = b"<html><head></head><body>x</body></html>"
        once = server._force_noindex(body, "/lp/keto")
        twice = server._force_noindex(once, "/lp/keto")
        self.assertEqual(once, twice)

    def test_estimate_matches_real_sitemap(self):
        """The ceiling that protects us from re-flooding the index must count
        exactly what the sitemap emits, or the guard is fiction."""
        for value, label in (("", "off"), ("1", "on")):
            self._force(value)
            handler = server.Handler.__new__(server.Handler)
            xml = handler._sitemap()
            self.assertEqual(xml.count(b"<url>"),
                             server._indexable_url_estimate(),
                             "sitemap/estimate disagree with lp_index %s" % label)

    def test_landing_pages_absent_from_sitemap_when_noindexed(self):
        self._force("")
        locs = _locs(server.Handler.__new__(server.Handler)._sitemap())
        self.assertFalse([u for u in locs if "/lp/" in u],
                         "noindexed landing pages must leave the sitemap")
        self.assertTrue([u for u in locs if "/n/" in u],
                        "the hub pages we actually need to rank stay listed")


def _locs(xml):
    import re
    return [m.decode() for m in re.findall(rb"<loc>[^<]*</loc>", xml)
            and [m.split(b"</loc>")[0].split(b"<loc>")[1]
                 for m in re.findall(rb"<loc>[^<]*</loc>", xml)]]


class SocialKitImageTests(unittest.TestCase):
    """Regression: queued social kits were delivered with no image at all.

    `social.post_kits()` populates image/image_png/pin_image, but the delivery
    queue rebuilds kits from the `social_posts` row instead — and that
    reconstruction returned six bare fields. So Instagram posts failed outright
    ("Instagram needs an image URL in the kit."), Telegram degraded to bare text
    with no share card, and Pinterest had to scrape an SVG og:image that it then
    rejected as a pin source.
    """

    class _Row(dict):
        def keys(self):
            return dict.keys(self)

    def _row(self, **extra):
        row = self._Row({"platform": "pinterest", "name": "n", "body": "b",
                         "link": "https://trypstore.com/lp/keto-snacks",
                         "slug": "keto-snacks", "keyword": "keto snacks"})
        row.update(extra)
        return row

    def test_queue_kit_carries_every_image_field(self):
        import publish
        kit = server._social_kits([self._row()])[0]
        for field in ("image", "image_png", "pin_image"):
            self.assertTrue(kit.get(field), "%s missing from queued kit" % field)

    def test_instagram_post_no_longer_fails(self):
        """Instagram is the strictest consumer: empty image == failed post."""
        import publish
        kit = server._social_kits([self._row(platform="instagram")])[0]
        body = publish._body_for("instagram", kit)
        self.assertTrue(body.get("image_png") or body.get("pin_image")
                        or body.get("image"),
                        "Instagram would refuse this kit")

    def test_telegram_gets_a_share_card(self):
        import publish
        kit = server._social_kits([self._row(platform="telegram")])[0]
        body = publish._body_for("telegram", kit)
        self.assertTrue(body.get("image_png") or body.get("image"),
                        "Telegram would post text with no photo")

    def test_pinterest_gets_the_tall_pin_card(self):
        """Pinterest requires a 2:3 raster; the square OG card is not accepted."""
        import publish
        kit = server._social_kits([self._row(platform="pinterest")])[0]
        self.assertIn("-pin.png", kit["pin_image"])

    def test_persisted_drip_variant_wins_over_reconstruction(self):
        """A scheduled repin must keep its own rotated card, not be flattened
        back to the canonical .png."""
        import json
        kit = server._social_kits([self._row(
            images=json.dumps({"pin_image": "/og/keto-snacks-pin.v3.png",
                               "image_png": "/og/keto-snacks.v3.png"}))])[0]
        self.assertEqual(kit["pin_image"], "/og/keto-snacks-pin.v3.png")
        self.assertEqual(kit["image_png"], "/og/keto-snacks.v3.png")

    def test_unparsable_images_column_falls_back_safely(self):
        """Corrupt JSON must degrade to the canonical rasters, not raise."""
        kit = server._social_kits([self._row(images="{not json")])[0]
        self.assertTrue(kit.get("image_png"))

    def test_row_without_slug_still_returns_a_kit(self):
        kit = server._social_kits([self._row(slug="")])[0]
        self.assertEqual(kit["platform"], "pinterest")
        self.assertEqual(kit.get("pin_image", ""), "")


class CannibalizationTests(unittest.TestCase):
    def _with_niches(self, rows, clicks=()):
        """Run against a synthetic niche set by patching the three readers the
        detector uses.

        Deliberately does NOT write to SQLite. `app/pstore.db` is a tracked
        seed database, and the obvious backup-and-restore approach is a trap:
        `_db()` hands out a fresh connection per call, so a TEMP backup table
        created on one connection is invisible to the next and the restore
        silently no-ops — which is how a test run once wiped 43 niches. The
        seed DB has no `clicks` table either, since that arrives via migration
        at runtime. Patching the readers keeps every test hermetic.
        """
        import json
        fixtures = [{"keyword": kw,
                     "products": [{"asin": a} for a in asins],
                     "created_at": "2026-01-01"} for kw, asins in rows]
        click_map = {}
        for slug, n in clicks:
            click_map[slug] = click_map.get(slug, 0) + n

        originals = (server._niches_rows, server._niche_clicks_by_slug)
        server._niches_rows = lambda: [dict(f) for f in fixtures]
        server._niche_clicks_by_slug = lambda: dict(click_map)

        def restore():
            (server._niches_rows, server._niche_clicks_by_slug) = originals
            server._CANNIBAL_CACHE.update({"val": None, "at": 0.0})
        self.addCleanup(restore)

        # Hold/apply writes the real settings table, so snapshot and restore it
        # rather than letting a test leave a parked page behind.
        with server._lock:
            conn = server._db()
            row = conn.execute("SELECT value FROM settings WHERE "
                               "key='seo.consolidation.holds'").fetchone()
            conn.close()
        original_setting = row["value"] if row else None

        def restore_setting():
            with server._lock:
                conn = server._db()
                if original_setting is None:
                    conn.execute("DELETE FROM settings WHERE "
                                 "key='seo.consolidation.holds'")
                else:
                    conn.execute("INSERT INTO settings (key,value) VALUES "
                                 "(?,?) ON CONFLICT(key) DO UPDATE SET "
                                 "value=excluded.value",
                                 ("seo.consolidation.holds", original_setting))
                conn.commit()
                conn.close()
            server._HOLDS_CACHE.update({"key": None, "at": 0.0, "val": None})
        self.addCleanup(restore_setting)

        server._CANNIBAL_CACHE.update({"val": None, "at": 0.0})
        server._HOLDS_CACHE.update({"key": None, "at": 0.0, "val": None})
        self.addCleanup(setattr, server, "_cannibalization_report",
                        server._cannibalization_report)

    def _sitemap_locs(self):
        import re
        handler = server.Handler.__new__(server.Handler)
        xml = handler._sitemap()
        return [m.decode() for m in re.findall(rb"<loc>[^<]*</loc>", xml)
                and [m.split(b"</loc>")[0].split(b"<loc>")[1]
                     for m in re.findall(rb"<loc>[^<]*</loc>", xml)]]

    def test_identical_product_sets_are_detected(self):
        self._with_niches([
            ("best griddle", ["A1", "A2", "A3", "A4"]),
            ("best griddle pan", ["A1", "A2", "A3", "A4"]),
            ("air fryer", ["B1", "B2", "B3", "B4"]),
        ])
        rep = server._cannibalization_report(use_cache=False)
        self.assertEqual(rep["duplicate_pairs"], 1)
        self.assertEqual(rep["holdable"], ["best-griddle-pan"])

    def test_keeper_is_never_held(self):
        self._with_niches([
            ("best griddle", ["A1", "A2", "A3", "A4"]),
            ("best griddle pan", ["A1", "A2", "A3", "A4"]),
        ])
        rep = server._cannibalization_report(use_cache=False)
        for group in rep["groups"]:
            self.assertNotIn(group["keeper"], group["hold"])
            self.assertTrue(group["hold"])

    def test_family_always_keeps_a_representative(self):
        """Three identical pages must not all be parked — the intent needs a
        page left standing to collect the ranking."""
        self._with_niches([
            ("griddle", ["A1", "A2", "A3", "A4"]),
            ("griddle pan", ["A1", "A2", "A3", "A4"]),
            ("griddle 50ft", ["A1", "A2", "A3", "A4"]),
        ])
        rep = server._cannibalization_report(use_cache=False)
        self.assertEqual(len(rep["groups"]), 1)
        group = rep["groups"][0]
        self.assertNotIn(group["keeper"], group["hold"])
        self.assertEqual(len(group["hold"]), 2,
                         "two of three identical pages are parked")
        self.assertEqual(
            len([s for s in ("griddle", "griddle-pan", "griddle-50ft")
                 if s not in rep["holdable"]]), 1,
            "exactly one page per intent must survive")

    def test_pages_with_clicks_are_protected(self):
        self._with_niches([
            ("griddle", ["A1", "A2", "A3", "A4"]),
            ("griddle pan", ["A1", "A2", "A3", "A4"]),
        ], clicks=[("griddle-pan", 25)])
        rep = server._cannibalization_report(use_cache=False)
        self.assertEqual(rep["holdable"], ["griddle"])
        self.assertEqual(rep["groups"][0]["keeper"], "griddle-pan")

    def test_similar_but_distinct_pages_are_left_alone(self):
        """A 50% overlap is not proof of duplication. Parking it would be a
        business decision, not a calculation."""
        self._with_niches([
            ("griddle", ["A1", "A2", "A3", "A4"]),
            ("air fryer", ["A3", "A4", "C1", "C2"]),
        ])
        rep = server._cannibalization_report(use_cache=False)
        self.assertEqual(rep["holdable"], [])

    def test_thin_product_sets_ignored(self):
        """Two shared ASINs is noise; a thin niche cannot prove duplication."""
        self._with_niches([
            ("thing one", ["A1", "A2"]),
            ("thing two", ["A1", "A2"]),
        ])
        rep = server._cannibalization_report(use_cache=False)
        self.assertEqual(rep["holdable"], [])

    def test_slug_collisions_reported_not_auto_held(self):
        """Two keywords can slugify to one URL. Holding the slug would noindex
        the page that actually renders, so this must be reported only."""
        self._with_niches([
            ("back pain", ["A1", "A2", "A3", "A4"]),
            ("back-pain", ["A1", "A2", "A3", "A4"]),
        ])
        rep = server._cannibalization_report(use_cache=False)
        self.assertTrue(rep["collisions"])
        self.assertEqual(rep["collisions"][0]["slug"], "back-pain")
        self.assertTrue(rep["collisions"][0]["same_products"])
        self.assertEqual(rep["holdable"], [])

    def test_dry_run_changes_nothing(self):
        self._with_niches([
            ("best griddle", ["A1", "A2", "A3", "A4"]),
            ("best griddle pan", ["A1", "A2", "A3", "A4"]),
        ])
        res = server._apply_cannibalization_holds(dry_run=True)
        self.assertEqual(res["would_hold"], ["best-griddle-pan"])
        self.assertEqual(server._consolidation_holds(), frozenset())

    def test_apply_then_revert_is_lossless(self):
        self._with_niches([
            ("best griddle", ["A1", "A2", "A3", "A4"]),
            ("best griddle pan", ["A1", "A2", "A3", "A4"]),
        ])
        page = b"<html><head></head><body>x</body></html>"
        self.assertNotIn(b"noindex",
                         server._force_noindex(page, "/n/best-griddle-pan"))

        res = server._apply_cannibalization_holds(dry_run=False)
        self.assertEqual(res["held"], ["best-griddle-pan"])

        # parked on every route that carries the keyword, not just the hub
        for path in ("/n/best-griddle-pan", "/lp/best-griddle-pan",
                     "/stories/best-griddle-pan"):
            self.assertTrue(server._path_is_held(path), path)
            self.assertIn(b"noindex", server._force_noindex(page, path), path)
        self.assertFalse(server._path_is_held("/n/best-griddle"))

        # revert
        server._set_setting("seo.consolidation.holds", "")
        server._HOLDS_CACHE.update({"key": None, "at": 0.0, "val": None})
        self.assertFalse(server._path_is_held("/n/best-griddle-pan"))
        self.assertNotIn(b"noindex",
                         server._force_noindex(page, "/n/best-griddle-pan"))

    def test_apply_is_idempotent(self):
        self._with_niches([
            ("best griddle", ["A1", "A2", "A3", "A4"]),
            ("best griddle pan", ["A1", "A2", "A3", "A4"]),
        ])
        first = server._apply_cannibalization_holds(dry_run=False)
        second = server._apply_cannibalization_holds(dry_run=False)
        self.assertEqual(first["held_count"], 1)
        self.assertEqual(second["held_count"], 0)

    def test_earnings_classifier_available_for_scoring(self):
        """keeper_score must not blow up on an unclassifiable keyword."""
        self.assertIsNotNone(earnings.classify("griddle pan"))
        self.assertIsNotNone(earnings.per_click_value(earnings.classify("")))


if __name__ == "__main__":
    unittest.main()