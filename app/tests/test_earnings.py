# -*- coding: utf-8 -*-
"""Offline tests for the earnings/conversion estimator + analytics routes."""
import json
import os
import sys
import unittest
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import earnings


class TestEarningsEstimator(unittest.TestCase):
    def tearDown(self):
        earnings._runtime.clear()

    def test_default_estimate(self):
        est = earnings.estimate(100)
        self.assertEqual(est["clicks"], 100)
        self.assertAlmostEqual(est["orders_est"], 3.0)
        self.assertAlmostEqual(est["commission_est"], 4.8)

    def test_zero_clicks(self):
        est = earnings.estimate(0)
        self.assertEqual(est["clicks"], 0)
        self.assertAlmostEqual(est["commission_est"], 0.0)

    def test_configure_overrides(self):
        earnings.configure(commission_pct=8.0, avg_order=60.0, order_rate=0.05)
        est = earnings.estimate(100)
        self.assertAlmostEqual(est["commission_est"], 24.0)
        self.assertAlmostEqual(est["orders_est"], 5.0)

    def test_negative_clicks_floored(self):
        self.assertEqual(earnings.estimate(-5)["clicks"], 0)

    def test_category_rate(self):
        self.assertEqual(earnings.commission_pct("beauty"), 10.0)
        self.assertAlmostEqual(earnings.commission_pct("unknown"), earnings.DEFAULT_COMMISSION_PCT)

    def test_aggregate_total(self):
        rows = [{"clicks": 10}, {"clicks": 5, "category": "books"}]
        agg = earnings.aggregate(rows, lambda r: r.get("category", ""))
        self.assertEqual(agg["total"]["clicks"], 15)
        self.assertEqual(set(agg["by_category"].keys()), {"default", "books"})

    def test_monthly_summary(self):
        s = earnings.monthly_summary([{"orders": 3, "earnings": 9.0}])
        self.assertEqual(s["total_orders"], 3)
        self.assertAlmostEqual(s["total_earnings"], 9.0)


class TestMeasuredLayer(unittest.TestCase):
    def tearDown(self):
        earnings._runtime.clear()

    def test_measured_links_orders_to_clicks(self):
        m = earnings.measured(200, 9, 1200.0, 72.0)
        self.assertEqual(m["clicks"], 200)
        self.assertEqual(m["orders"], 9)
        self.assertAlmostEqual(m["measured_order_rate"], 0.045)
        self.assertAlmostEqual(m["measured_commission_per_click"], 0.36)
        self.assertAlmostEqual(m["measured_aov"], 1200.0 / 9)
        # estimate for the same click volume (default 3% @ 4% of $40)
        self.assertAlmostEqual(m["orders_est"], 6.0)
        self.assertAlmostEqual(m["commission_est"], 9.6)
        self.assertAlmostEqual(m["gap"], 72.0 - 9.6)

    def test_zero_clicks_never_fabricates_a_rate(self):
        m = earnings.measured(0, 4, 512.5, 30.75)
        self.assertEqual(m["clicks"], 0)
        self.assertEqual(m["measured_order_rate"], 0.0)
        self.assertEqual(m["measured_commission_per_click"], 0.0)

    def test_missing_fields_floored(self):
        m = earnings.measured(None, None, None, None)
        self.assertEqual(m["clicks"], 0)
        self.assertEqual(m["orders"], 0)
        self.assertEqual(m["commission"], 0.0)

    def test_attribution_layer_rolls_up(self):
        cohorts = [
            {"month": "2026-09", "channel": "email", "slug": "keto-snacks",
             "campaign": "s1", "clicks": 100, "orders": 4,
             "revenue": 500.0, "commission": 30.0},
            {"month": "2026-09", "channel": "email", "slug": "",
             "campaign": "s2", "clicks": 40, "orders": 0,
             "revenue": 0.0, "commission": 0.0},
            {"month": "2026-09", "channel": "social", "slug": "keto-snacks",
             "campaign": "", "clicks": 0, "orders": 2,
             "revenue": 150.0, "commission": 9.0},
        ]
        layer = earnings.attribution_layer(cohorts)
        self.assertEqual(len(layer["rows"]), 3)
        self.assertEqual(layer["grand"], {"clicks": 140, "orders": 6,
                                          "revenue": 650.0, "commission": 39.0})
        by = layer["by_channel"]
        self.assertEqual(by["email"]["orders"], 4)
        self.assertEqual(by["social"]["clicks"], 0)
        self.assertEqual(by["social"]["orders"], 2)

    def test_attribution_layer_sorts_newest_first(self):
        layer = earnings.attribution_layer([
            {"month": "2026-08", "channel": "email", "slug": "", "campaign": "",
             "clicks": 5, "orders": 0, "revenue": 0, "commission": 0},
            {"month": "2026-09", "channel": "email", "slug": "", "campaign": "",
             "clicks": 9, "orders": 0, "revenue": 0, "commission": 0},
        ])
        self.assertEqual([r["month"] for r in layer["rows"]],
                         ["2026-09", "2026-08"])


if __name__ == "__main__":
    unittest.main()