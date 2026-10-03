"""Commission-category classification.

This is the fix for the highest-leverage revenue bug on the site: the priority
engine valued every niche with `return ""`, which meant every page was priced
at the 4% default regardless of whether it was a 20% jewelry page or a 4%
grocery page. The engine therefore told the operator to promote the cheapest
niches on the catalogue.

These tests pin the contract, not the exact token list, so the rules table can
be extended without rewriting them.
"""

import unittest

import earnings


class TestSingularisation(unittest.TestCase):
    """Plural keywords are the norm ("best baking sheets"), so rules and input
    are both singularised. These were all real bugs."""

    def test_plain_plural_drops_one_s(self):
        self.assertEqual(earnings._sing("machines"), "machine")
        self.assertEqual(earnings._sing("bottles"), "bottle")
        self.assertEqual(earnings._sing("purifiers"), "purifier")
        self.assertEqual(earnings._sing("headphones"), "headphone")

    def test_sibilant_clusters_drop_the_e(self):
        self.assertEqual(earnings._sing("glasses"), "glass")
        self.assertEqual(earnings._sing("boxes"), "box")
        self.assertEqual(earnings._sing("benches"), "bench")
        self.assertEqual(earnings._sing("dishes"), "dish")

    def test_ies_becomes_y(self):
        self.assertEqual(earnings._sing("babies"), "baby")

    def test_words_that_must_not_be_mangled(self):
        # Short words, -ss, -us and empty input must come back untouched.
        for w in ("gas", "ss", "us", "grass", "s", ""):
            self.assertEqual(earnings._sing(w), w)

    def test_no_generic_es_rule(self):
        """Regression: a blanket '-es -> -e' turned 'machines' into 'machin',
        which silently broke every 3-word rule ending in a plural noun."""
        self.assertEqual(earnings._sing("watches"), "watch")


class TestClassify(unittest.TestCase):
    def test_empty_and_junk_is_default_not_a_guess(self):
        for v in ("", None, "---", "!!!"):
            self.assertEqual(earnings.classify(v), "default")

    def test_high_rate_categories(self):
        self.assertEqual(earnings.classify("gold-jewelry"), "jewelry")
        self.assertEqual(earnings.classify("jewelry-box"), "jewelry")
        self.assertEqual(earnings.classify("best-power-bank-for-iphone"),
                         "electronics-accessories")
        self.assertEqual(earnings.classify("best-noise-cancelling-headphones"),
                         "electronics-accessories")

    def test_real_production_niches(self):
        """Every slug below exists in the live 511-niche catalogue."""
        cases = {
            "best-office-chair": "furniture",
            "best-standing-desk": "furniture",
            "best-mattress-topper": "furniture",
            "best-air-fryer": "appliances",
            "best-espresso-machine": "appliances",
            "best-instant-pot": "appliances",
            "best-griddle": "garden",
            "best-cooler-with-wheels": "garden",
            "best-dog-food": "pet",
            "best-cat-litter-box": "pet",
            "best-baby-monitor": "baby",
            "best-stroller": "baby",
            "best-electric-toothbrush": "beauty",
            "best-curling-iron": "beauty",
            "best-yoga-mat": "sports",
            "best-dumbbells": "sports",
            "best-keto-snacks": "grocery",
            "best-robot-vacuum": "home",
            "best-led-light-bulbs": "home",
        }
        for slug, want in cases.items():
            self.assertEqual(earnings.classify(slug), want, slug)

    def test_plural_matches_singular_rule(self):
        self.assertEqual(earnings.classify("best-baking-sheets"),
                         earnings.classify("baking sheet"))
        self.assertEqual(earnings.classify("best-air-purifiers"), "home")

    def test_cat_does_not_match_camping(self):
        """The whole reason matching is on word runs rather than substrings."""
        self.assertEqual(earnings.classify("best-cat-food"), "pet")
        self.assertEqual(earnings.classify("best-camping-tent"), "sports")

    def test_longest_run_wins_over_shorter(self):
        # 'kitchen' alone is ambiguous; the product noun must decide.
        self.assertEqual(earnings.classify("best-blenders-for-kitchen-2026"),
                         "appliances")
        self.assertEqual(earnings.classify("kitchen-appliances"), "appliances")
        self.assertEqual(earnings.classify("best-desk-lamps-for-home-office"),
                         "office")

    def test_car_seat_is_baby_not_automotive(self):
        self.assertEqual(earnings.classify("best-car-seat-cushion"), "baby")

    def test_every_rule_resolves_to_a_real_category(self):
        for cat, toks in earnings.CATEGORY_RULES.items():
            self.assertIn(cat, earnings.SAMPLE_CATEGORIES, cat)
            self.assertIn(cat, earnings.CATEGORY_AOV, cat)
            for tok in toks:
                self.assertTrue(tok.strip(), cat)


class TestEconomics(unittest.TestCase):
    def tearDown(self):
        earnings.configure(commission_pct=None, avg_order=None, order_rate=None)
        earnings._runtime.clear()

    def test_empty_category_keeps_the_flat_default(self):
        """Every pre-existing caller and the /admin config form must be
        unchanged when no category is supplied."""
        self.assertEqual(earnings.avg_order(""),
                         earnings.DEFAULT_AVG_ORDER)
        self.assertEqual(earnings.commission_pct(""),
                         earnings.DEFAULT_COMMISSION_PCT)

    def test_per_category_aov_is_used(self):
        self.assertNotEqual(earnings.avg_order("furniture"),
                            earnings.avg_order("grocery"))

    def test_operator_override_beats_category_table(self):
        earnings.configure(avg_order=123.0)
        self.assertEqual(earnings.avg_order("furniture"), 123.0)
        self.assertEqual(earnings.avg_order("grocery"), 123.0)

    def test_unknown_category_falls_back_to_defaults(self):
        self.assertEqual(earnings.commission_pct("not-a-category"),
                         earnings.DEFAULT_COMMISSION_PCT)
        self.assertEqual(earnings.avg_order("not-a-category"),
                         earnings.DEFAULT_AVG_ORDER)

    def test_classification_beats_flat_rate_on_identical_traffic(self):
        """The whole point: same clicks, more money, because we stopped pricing
        a 20% jewelry page like a 4% grocery page."""
        flat = earnings.per_click_value("default")
        money = earnings.per_click_value(earnings.classify("gold-jewelry"))
        cheap = earnings.per_click_value(earnings.classify("best-keto-snacks"))
        self.assertGreater(money, flat * 5)
        self.assertGreater(money, cheap * 10)

    def test_estimate_uses_the_category(self):
        got = earnings.estimate(1000, "jewelry")
        self.assertEqual(got["commission_pct"], 20.0)
        self.assertAlmostEqual(got["commission_est"],
                               1000 * earnings.DEFAULT_ORDER_RATE * 85.0 * 0.20)


if __name__ == "__main__":
    unittest.main()