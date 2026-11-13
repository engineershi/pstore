# -*- coding: utf-8 -*-
"""Offline tests for the paid Niche Validation Report deliverable."""
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import earnings
import report
import seo
import server


def _niche(keyword, prices=(10.0, 20.0, 30.0), source=None, reviews=500):
    products = []
    for i, price in enumerate(prices):
        item = {"asin": "B%09d" % i, "title": "Product %d" % i,
                "price": price, "stars": 4.5, "reviews": reviews,
                "url": "https://www.amazon.com/dp/B%09d" % i}
        if source:
            item["source"] = source
        products.append(item)
    return {"keyword": keyword, "products": products, "score": 8.0,
            "saturation": 5.0}


class TestVerdict(unittest.TestCase):
    def test_high_value_low_saturation_is_strong(self):
        self.assertEqual(report.verdict(0.51, 6.6), report.VERDICT_STRONG)

    def test_high_value_crowded_is_viable_not_strong(self):
        self.assertEqual(report.verdict(0.35, 9.0), report.VERDICT_VIABLE)

    def test_mid_value_is_viable(self):
        self.assertEqual(report.verdict(0.12, 5.0), report.VERDICT_VIABLE)

    def test_low_value_is_avoid(self):
        self.assertEqual(report.verdict(0.03, 5.0), report.VERDICT_AVOID)

    def test_unknown_saturation_keeps_money_verdict(self):
        self.assertEqual(report.verdict(0.51, None), report.VERDICT_STRONG)
        self.assertEqual(report.verdict(0.03, None), report.VERDICT_AVOID)

    def test_negative_per_click_is_avoid(self):
        self.assertEqual(report.verdict(-1, 1.0), report.VERDICT_AVOID)


class TestAnalyse(unittest.TestCase):
    def test_money_math_uses_observed_price(self):
        data = _niche("gold jewelry")  # prices 10/20/30 -> median 20
        out = report.analyse(data["keyword"], data["products"],
                             data["score"], data["saturation"])
        self.assertEqual(out["category"], "jewelry")
        self.assertAlmostEqual(out["commission_pct"], 20.0)
        self.assertAlmostEqual(out["avg_order"], 20.0)
        self.assertEqual(out["aov_basis"], "sampled median")
        self.assertAlmostEqual(out["per_click"], 20.0 * 0.20 * 0.03)
        self.assertAlmostEqual(out["reference_per_click"],
                              earnings.per_click_value("jewelry"))
        self.assertEqual(out["verdict"], report.VERDICT_VIABLE)

    def test_cheap_products_do_not_inherit_category_aov(self):
        out = report.analyse("gold jewelry", [{"price": p} for p in (8, 9, 10)])
        self.assertLess(out["per_click"], out["reference_per_click"])
        self.assertNotEqual(out["verdict"], report.VERDICT_STRONG)

    def test_thin_sample_falls_back_to_category(self):
        out = report.analyse("gold jewelry", [{"price": 10}])
        self.assertEqual(out["aov_basis"], "category reference")
        self.assertAlmostEqual(out["per_click"],
                              earnings.per_click_value("jewelry"))

    def test_low_commission_is_avoid(self):
        data = _niche("keto")
        out = report.analyse(data["keyword"], data["products"])
        self.assertEqual(out["category"], "grocery")
        self.assertEqual(out["verdict"], report.VERDICT_AVOID)

    def test_empty_products_do_not_crash(self):
        out = report.analyse("something unknown", [])
        self.assertEqual(out["verdict"], report.VERDICT_AVOID)
        self.assertIsNone(out["price_band"]["median"])
        self.assertEqual(out["shortlist"], [])
        self.assertEqual(out["confidence"], "Low")

    def test_price_band_quartiles(self):
        out = report.analyse("gold jewelry", [{"price": p} for p in (10, 20, 30, 40)])
        self.assertAlmostEqual(out["price_band"]["median"], 25.0)
        self.assertAlmostEqual(out["price_band"]["low"], 17.5)
        self.assertAlmostEqual(out["price_band"]["high"], 32.5)

    def test_scraped_ratings_are_not_exposed(self):
        out = report.analyse("gold jewelry", _niche("gold jewelry")["products"])
        self.assertTrue(out["shortlist"])
        for item in out["shortlist"]:
            self.assertIsNone(item["stars"])
            self.assertIsNone(item["reviews"])

    def test_paapi_ratings_are_allowed(self):
        data = _niche("gold jewelry", source="paapi")
        out = report.analyse(data["keyword"], data["products"])
        self.assertEqual(out["shortlist"][0]["stars"], 4.5)
        self.assertEqual(out["shortlist"][0]["reviews"], 500)

    def test_shortlist_is_capped(self):
        data = _niche("gold jewelry", prices=range(1, 20))
        out = report.analyse(data["keyword"], data["products"])
        self.assertEqual(len(out["shortlist"]), 5)


class TestRender(unittest.TestCase):
    def test_pdf_is_bytes_and_titled(self):
        data = _niche("gold jewelry")
        out = report.build(data["keyword"], data["products"],
                           data["score"], data["saturation"])
        self.assertTrue(out["pdf"].startswith(b"%PDF"))
        self.assertIn(b"NICHE VALIDATION", out["pdf"])
        self.assertTrue(out["pdf_name"].endswith("-niche-report.pdf"))

    def test_pdf_never_prints_unlicensed_stars(self):
        data = _niche("gold jewelry")
        out = report.build(data["keyword"], data["products"])
        self.assertNotIn(b" stars ", out["pdf"])


class TestPickSample(unittest.TestCase):
    def test_prefers_highest_per_click(self):
        path = os.path.join(tempfile.mkdtemp(), "p.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE niches (id INTEGER PRIMARY KEY, keyword TEXT,"
                     " score REAL, saturation REAL, products TEXT)")
        rows = [
            ("keto", 8.0, 7.8, json.dumps([{"asin": "B1", "price": 10}])),
            ("gold jewelry", 8.8, 6.6, json.dumps([{"asin": "B2", "price": 50}])),
            ("empty", 9.0, 1.0, "[]"),
        ]
        conn.executemany("INSERT INTO niches (keyword, score, saturation, products)"
                         " VALUES (?,?,?,?)", rows)
        conn.commit()
        conn.close()
        got = report.pick_sample(path)
        self.assertEqual(got["keyword"], "gold jewelry")

    def test_no_products_returns_none(self):
        path = os.path.join(tempfile.mkdtemp(), "p.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE niches (id INTEGER PRIMARY KEY, keyword TEXT,"
                     " score REAL, saturation REAL, products TEXT)")
        conn.execute("INSERT INTO niches (keyword, products) VALUES ('x','[]')")
        conn.commit()
        conn.close()
        self.assertIsNone(report.pick_sample(path))


class TestLoadNiche(unittest.TestCase):
    def test_loads_saved_row(self):
        path = os.path.join(tempfile.mkdtemp(), "p.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE niches (id INTEGER PRIMARY KEY, keyword TEXT,"
                     " score REAL, saturation REAL, products TEXT)")
        conn.execute("INSERT INTO niches (keyword, score, saturation, products)"
                     " VALUES (?,?,?,?)",
                     ("gold jewelry", 8.8, 6.6, json.dumps([{"asin": "B1"}])))
        conn.commit()
        conn.close()
        got = report.load_niche("gold jewelry", path)
        self.assertEqual(got["keyword"], "gold jewelry")
        self.assertEqual(got["products"], [{"asin": "B1"}])
        self.assertIsNone(report.load_niche("absent", path))


class TestReportOfferSurface(unittest.TestCase):
    """The report is only a business if buyers can reach it: it has to be in
    the sitemap, linked from every footer, and sell without a payment rail."""

    def test_report_is_indexable_and_linked(self):
        urls = seo.indexable_urls([_niche("gold jewelry")])
        self.assertIn(seo.BASE_URL.rstrip("/") + "/report", urls)
        self.assertIn('href="/report"', seo._footer().decode("utf-8"))

    def test_page_sells_with_email_order_when_checkout_is_unset(self):
        class H:
            def __init__(self):
                self.sent = []

            def _send(self, code, body, ctype=None):
                self.sent.append((code, body, ctype))
                return None

        h = H()
        with mock.patch.object(server, "_get_setting",
                               lambda k, d="": {"report_price": "$49",
                                                "report_checkout_url": ""}.get(k, d)):
            server.Handler._report_page_html(h)
        code, body, ctype = h.sent[-1]
        html = body.decode("utf-8")
        self.assertEqual(200, code)
        self.assertIn("text/html", ctype)
        self.assertIn("Niche Validation Report", html)
        self.assertIn("/report/sample.pdf", html)
        self.assertIn("$49", html)
        self.assertIn("mailto:", html)
        self.assertIn("Email to order", html)

    def test_configured_checkout_becomes_the_cta(self):
        class H:
            def __init__(self):
                self.sent = []

            def _send(self, code, body, ctype=None):
                self.sent.append((code, body, ctype))
                return None

        h = H()
        with mock.patch.object(server, "_get_setting",
                               lambda k, d="": {"report_price": "$79",
                                                "report_checkout_url":
                                                "https://pay.example/report"}.get(k, d)):
            server.Handler._report_page_html(h)
        html = h.sent[-1][1].decode("utf-8")
        self.assertIn('href="https://pay.example/report"', html)
        self.assertIn("Order my report", html)

    def test_sample_pdf_route_serves_a_pdf(self):
        class H:
            def __init__(self):
                self.code = None
                self.headers = []
                self.wfile = io.BytesIO()
                self._head_only = False

            def send_response(self, code):
                self.code = code

            def send_header(self, key, value):
                self.headers.append((key, value))

            def end_headers(self):
                pass

            def _send(self, code, body, ctype=None):
                self.code = code

        sample = {"keyword": "gold jewelry", "products": [], "score": 8.0,
                  "saturation": 5.0}
        built = {"pdf": b"%PDF-1.4 test", "pdf_name": "gold-jewelry.pdf"}
        h = H()
        with mock.patch.object(server.report_mod, "pick_sample",
                               return_value=sample), \
                mock.patch.object(server.report_mod, "build", return_value=built):
            server.Handler._report_sample_pdf(h)
        self.assertEqual(200, h.code)
        self.assertEqual(b"%PDF-1.4 test", h.wfile.getvalue())
        self.assertIn(("Content-Type", "application/pdf"), h.headers)


if __name__ == "__main__":
    unittest.main()
