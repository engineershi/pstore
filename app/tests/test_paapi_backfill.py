# -*- coding: utf-8 -*-
"""PA-API image backfill: the step that makes the credentials do anything.

`_enrich_products()` only runs on the save/refresh paths, so niches saved before
PA-API was configured keep their image-less scraped rows forever. `seo.py` gates
product images on `source == "paapi"`, so those pages render zero <img> and
merchant-listing rich results never fire. These tests pin that the backfill
fills them in, adds only (never overwrites), and refuses to run unconfigured.
"""
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
import uuid

import amazon
import paapi
import server


def _prod(asin, **over):
    p = {"asin": asin, "title": "", "price": "", "image": "", "source": "scraper"}
    p.update(over)
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="paapi_backfill_")
        self.db = os.path.join(self.tmp, "p.db")
        shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "pstore.db"), self.db)
        self._orig_db = server.DB
        server.DB = self.db
        self._orig_urlopen = amazon._urlopen
        self._orig_interval = paapi.MIN_INTERVAL
        paapi.MIN_INTERVAL = 0.0
        paapi._last_call[0] = 0.0
        paapi._configure_clear()
        self.calls = []

    def tearDown(self):
        server.DB = self._orig_db
        amazon._urlopen = self._orig_urlopen
        paapi.MIN_INTERVAL = self._orig_interval
        paapi._configure_clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, rows):
        """Replace the niches table with an explicit, small inventory."""
        conn = sqlite3.connect(self.db)
        # The tracked seed DB predates the updated_at migration that runs at
        # server boot, so apply it here rather than depending on import order.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(niches)")]
        if "updated_at" not in cols:
            conn.execute("ALTER TABLE niches ADD COLUMN updated_at TEXT")
        conn.execute("DELETE FROM niches")
        for kw, products in rows:
            conn.execute(
                "INSERT INTO niches (keyword, market, score, saturation, products) "
                "VALUES (?,?,?,?,?)",
                (kw, "com", 50, 20, json.dumps(products)))
        conn.commit()
        conn.close()

    def _products(self, kw):
        conn = sqlite3.connect(self.db)
        row = conn.execute("SELECT products FROM niches WHERE keyword=?",
                           (kw,)).fetchone()
        conn.close()
        return json.loads(row[0])

    def _stub_paapi(self, items):
        """Return a PA-API payload for the ASINs requested."""
        def fake(req, timeout=None):
            body = json.loads(req.data)
            self.calls.append(body["ItemIds"])
            matched = [i for i in items if i["ASIN"] in body["ItemIds"]]
            class R:
                code = 200
                headers = {}
                def read(_s):
                    return json.dumps(
                        {"ItemsResult": {"Items": matched}}).encode()
            return R()
        amazon._urlopen = fake

    def _ready(self):
        paapi.configure("AKIAEXAMPLE", "SECRET", "tag-20")


class TestTheGapItCloses(Base):
    def test_refuses_to_run_without_credentials(self):
        """The dangerous failure is a backfill that looks like it worked."""
        self._seed([("widget", [_prod("B0AAA0000A")])])
        rep = server._paapi_image_backfill()
        self.assertFalse(rep["ok"])
        self.assertIn("not configured", rep["skipped"])
        self.assertEqual(rep["updated"], 0)
        self.assertEqual(self._products("widget")[0]["image"], "")

    def test_fills_a_missing_image_on_an_already_saved_niche(self):
        """This is the whole point: the row already existed, so no save/refresh
        path would ever have enriched it."""
        self._seed([("widget", [_prod("B0AAA0000A", title="Widget")])])
        self._ready()
        self._stub_paapi([{
            "ASIN": "B0AAA0000A",
            "ItemInfo": {"Title": {"DisplayValue": "Widget"}},
            "Images": {"Primary": {"Large": {"URL": "https://img/w.jpg"}}},
            "Offers": {"Listings": [{"Price": {"Amount": "19.99",
                                                "Currency": "USD"}}]},
        }])
        rep = server._paapi_image_backfill()
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["updated"], 1)
        self.assertGreaterEqual(rep["images"], 1)
        got = self._products("widget")[0]
        self.assertEqual(got["image"], "https://img/w.jpg")
        self.assertEqual(got["source"], "paapi")

    def test_never_overwrites_a_live_scraped_price(self):
        """_enrich_products() only ADDs. A fresh scraped price beats a cached
        PA-API one, and a backfill that clobbered it would be a regression."""
        self._seed([("widget", [_prod("B0AAA0000A", price="9.99",
                                      title="Live", image="https://live/i.jpg")])])
        self._ready()
        self._stub_paapi([{
            "ASIN": "B0AAA0000A",
            "ItemInfo": {"Title": {"DisplayValue": "Stale PA-API title"}},
            "Offers": {"Listings": [{"Price": {"Amount": "29.99",
                                                "Currency": "USD"}}]},
        }])
        server._paapi_image_backfill()
        got = self._products("widget")[0]
        self.assertEqual(got["price"], "9.99")
        self.assertEqual(got["title"], "Live")
        self.assertEqual(got["image"], "https://live/i.jpg")

    def test_does_not_rewrite_a_row_that_gained_nothing(self):
        self._seed([("widget", [_prod("B0AAA0000A", title="T", price="1.00",
                                      image="https://i/x.jpg")])])
        self._ready()
        self._stub_paapi([])  # PA-API knows nothing about this ASIN
        rep = server._paapi_image_backfill()
        self.assertEqual(rep["updated"], 0)
        self.assertEqual(rep["fields"], 0)

    def test_walks_every_niche_not_just_one_page(self):
        """The bug this guards: paginating with LIMIT/OFFSET while rewriting the
        rows you just read skips records, so the operator runs it twice and some
        niches still have no image."""
        rows = [("niche-%03d" % i, [_prod("B0AAA%03dA" % i)])
                for i in range(7)]
        self._seed(rows)
        self._ready()
        self._stub_paapi([{
            "ASIN": "B0AAA%03dA" % i,
            "Images": {"Primary": {"Large": {"URL": "https://img/%d.jpg" % i}}},
        } for i in range(7)])
        rep = server._paapi_image_backfill(limit=2)
        self.assertEqual(rep["scanned"], 7)
        self.assertEqual(rep["updated"], 7)
        for i in range(7):
            self.assertEqual(
                self._products("niche-%03d" % i)[0]["image"],
                "https://img/%d.jpg" % i,
                "niche-%03d was skipped by pagination" % i)

    def test_survives_a_corrupt_products_column(self):
        """One bad row must not abort the sweep -- the operator gets one shot."""
        self._seed([("good", [_prod("B0AAA0000A")])])
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO niches (keyword, market, score, saturation, "
                     "products) VALUES (?,?,?,?,?)",
                     ("broken", "com", 1, 1, "{not json"))
        conn.commit()
        conn.close()
        self._ready()
        self._stub_paapi([{
            "ASIN": "B0AAA0000A",
            "Images": {"Primary": {"Large": {"URL": "https://img/g.jpg"}}},
        }])
        rep = server._paapi_image_backfill()
        self.assertTrue(rep["ok"])
        self.assertEqual(self._products("good")[0]["image"], "https://img/g.jpg")

    def test_no_api_key_is_ever_written_to_the_db(self):
        """The secret lives in settings, not in a product row."""
        self._seed([("widget", [_prod("B0AAA0000A")])])
        self._ready()
        self._stub_paapi([{
            "ASIN": "B0AAA0000A",
            "Images": {"Primary": {"Large": {"URL": "https://img/w.jpg"}}},
        }])
        server._paapi_image_backfill()
        blob = json.dumps(self._products("widget"))
        self.assertNotIn("SECRET", blob)


class TestPromotionIsLicenceSafe(Base):
    """`source == "paapi"` is a legal gate, not a cosmetic flag. Getting it
    wrong republishes scraped star ratings -- Program Content Amazon does not
    license -- on every ranking page."""

    def _one(self, seeded, paapi_item):
        self._seed([("widget", [seeded])])
        self._ready()
        self._stub_paapi([paapi_item])
        server._paapi_image_backfill()
        return self._products("widget")[0]

    def test_image_alone_unlocks_the_image(self):
        got = self._one(
            _prod("B0AAA0000A", title="Scraped title", price="9.99"),
            {"ASIN": "B0AAA0000A",
             "Images": {"Primary": {"Large": {"URL": "https://img/a.jpg"}}}})
        self.assertEqual(got["source"], "paapi")
        self.assertEqual(got["image"], "https://img/a.jpg")

    def test_scraped_ratings_are_not_unlocked_by_a_paapi_image(self):
        """The exact trap: PA-API supplied a legitimate image, but the row also
        carries a scraped star rating. amazon.rating_pair() would now publish it.
        The image costs us a thumbnail; the rating costs us the account."""
        got = self._one(
            _prod("B0AAA0000A", title="T", price="1.00", stars=4.8, reviews=1200),
            {"ASIN": "B0AAA0000A",
             "Images": {"Primary": {"Large": {"URL": "https://img/a.jpg"}}}})
        self.assertNotEqual(got["source"], "paapi")
        self.assertEqual(got["stars"], 4.8)  # still stored internally...
        self.assertEqual(amazon.licensed_rating(got), (None, None))  # ...never shown

    def test_paapi_supplied_ratings_are_published(self):
        got = self._one(
            _prod("B0AAA0000A", title="T", price="1.00"),
            {"ASIN": "B0AAA0000A",
             "Images": {"Primary": {"Large": {"URL": "https://img/a.jpg"}}},
             "ItemInfo": {"Title": {"DisplayValue": "T"},
                          "ByLineInfo": {"Contributors": [{"Name": "4.6 out of 5 stars", "Role": "star_rating"}]},
                          "CustomerReviews": {"Count": 812, "AverageStarRating": "4.6"}}})
        self.assertEqual(got["source"], "paapi")
        self.assertEqual(amazon.licensed_rating(got), ("4.6", 812))

    def test_price_only_enrichment_does_not_relabel(self):
        """A cached PA-API price is not reader-facing licensed content, so it
        must not flip the flag that unlocks images and ratings."""
        got = self._one(
            _prod("B0AAA0000A", title="T"),
            {"ASIN": "B0AAA0000A",
             "Offers": {"Listings": [{"Price": {"Amount": "5.00",
                                                 "Currency": "USD"}}]}})
        self.assertEqual(got["price"], "5.00")
        self.assertNotEqual(got["source"], "paapi")

class TestRouteAndButton(Base):
    def test_route_is_refused_unconfigured_rather_than_silently_ok(self):
        h = server.Handler.__new__(server.Handler)
        h.command = "POST"
        sent = {}
        h._body = lambda: {}
        h._send = lambda code, payload: sent.update(code=code, payload=payload)
        h._paapi_backfill_post()
        self.assertEqual(sent["code"], 200)
        self.assertFalse(sent["payload"]["ok"])
        self.assertIn("not configured", sent["payload"]["error"])

    def test_route_runs_the_backfill_when_configured(self):
        self._seed([("widget", [_prod("B0AAA0000A")])])
        self._ready()
        self._stub_paapi([{
            "ASIN": "B0AAA0000A",
            "Images": {"Primary": {"Large": {"URL": "https://img/w.jpg"}}},
        }])
        h = server.Handler.__new__(server.Handler)
        h.command = "POST"
        sent = {}
        h._body = lambda: {"limit": 5}
        h._send = lambda code, payload: sent.update(code=code, payload=payload)
        h._paapi_backfill_post()
        self.assertTrue(sent["payload"]["ok"])
        self.assertIn("Backfilled", sent["payload"]["message"])

    def test_admin_page_offers_the_button(self):
        html = server.Handler.__new__(server.Handler)._keys_page_html() \
            if hasattr(server.Handler, "_keys_page_html") else ""
        if not html:  # the page is assembled inline; assert on the source
            with open(server.__file__) as fh:
                html = fh.read()
        self.assertIn("pa_backfill", html)
        self.assertIn("/api/paapi/backfill", html)

    def test_endpoint_is_owned_by_the_content_role(self):
        """An unowned API path would 403 for every real user."""
        prefixes = server.FUNCTION_PATHS["content"]
        self.assertTrue(any("/api/paapi/backfill".startswith(p) for p in prefixes))


if __name__ == "__main__":
    unittest.main()