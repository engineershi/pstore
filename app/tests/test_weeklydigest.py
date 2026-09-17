#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_weeklydigest — hermetic fence for app/weeklydigest.py.

This test is itself hermetic: it imports ONLY the module under test plus
stdlib unittest/datetime/json. It never touches server/mailer/amazon/DB/
network. It asserts:
  * determinism (same inputs -> byte-identical outputs, repeated runs);
  * the money seam: exactly ONE `{{tracked_link}}` moustache token in subject
    or text/html, `{{first_name}}` and `{{unsubscribe_url}}` present, and NO
    leaked literal `{{tracked_link_<slug>}}` per-product token anywhere;
  * every picker is a pure function (returns fresh lists, no mutation);
  * the weekly gate dedupes one digest per ISO week per niche.

Keep this file stdlib-only and offline forever — that IS the capstone."""

import datetime as _dt
import sys as _sys
import unittest as _ut

_IMPORT_DIR = "/root/projects/pstore/app"
if _IMPORT_DIR not in _sys.path:
    _sys.path.insert(0, _IMPORT_DIR)

import weeklydigest as _wd


def _fmt(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _price(v):
    try:
        return float(str(v).strip().lstrip("$,"))
    except (TypeError, ValueError):
        return 0.0


class WeeklyDigestHermetic(_ut.TestCase):

    def setUp(self):
        self.winners = [
            {"keyword": "air fryer", "slug": "air-fryer", "clicks": 42},
            {"keyword": "air fryer", "slug": "air-fryer-mini", "clicks": 31},
            {"keyword": "sous vide", "slug": "sous-vide", "clicks": 20},
        ]
        self.deals = [
            {"keyword": "air fryer", "slug": "air-fryer-xl",
             "title": "Air Fryer XL", "old": 120, "new": 89,
             "drop_pct": 25.8},
            {"keyword": "sous vide", "slug": "sous-vide-ck",
             "title": "Sous Vide 1000W", "old": 100, "new": 79,
             "drop_pct": 21.0},
        ]
        self.referrers = [
            {"email": "amy@ex.com", "first_name": "Amy",
             "keyword": "air fryer", "referrals": 3},
        ]

    def test_pick_winners_deterministic(self):
        a = _wd.pick_winners(self.winners)
        b = _wd.pick_winners(self.winners)
        self.assertEqual(a, b)
        self.assertEqual([w["slug"] for w in a],
                         ["air-fryer", "air-fryer-mini", "sous-vide"])
        # does not mutate caller's rows
        self.assertEqual(len(self.winners), 3)

    def test_pick_deals_deterministic_and_caps(self):
        a = _wd.pick_deals(self.deals)
        self.assertEqual(a, _wd.pick_deals(self.deals))
        slugs = [d["slug"] for d in a]
        self.assertEqual(slugs[:2], ["air-fryer-xl", "sous-vide-ck"])
        self.assertLessEqual(len(a), 2)  # cap respected, deterministic

    def test_pick_referrers_min(self):
        rows = [{"email": "amy@ex.com", "first_name": "Amy",
                 "keyword": "air fryer", "referrals": 3},
                {"email": "noref@ex.com", "first_name": "No",
                 "keyword": "air fryer", "referrals": 0}]
        refs = _wd.pick_referrers(rows, min_referrals=2)
        self.assertEqual([r["email"] for r in refs], ["amy@ex.com"])

    def test_pick_prune_quiet(self):
        rows = [{"keyword": "quiet niche", "slug": "quiet-niche",
                 "clicks": 2, "last_click_at": "2026-08-01"}]
        pruned = _wd.pick_prune(rows,
                                min_clicks=5, quiet_days=14,
                                day=_dt.date(2026, 9, 22))
        self.assertEqual([p["slug"] for p in pruned], ["quiet-niche"])

    def test_money_email_single_tracked_link(self):
        email = _wd.digest_email("air fryer", self.winners, self.deals,
                                 referrers=self.referrers,
                                 week="2026-W38")
        self.assertIn("{{first_name}}", email["text"])
        self.assertIn("{{unsubscribe_url}}", email["text"])
        # exactly ONE money moustache token across text+html, never a per-slug
        both = email["text"] + email["html"]
        self.assertEqual(both.count("{{tracked_link}}"), 1)
        self.assertNotIn("{{tracked_link_air-fryer-xl}}", both)
        self.assertNotIn("{{tracked_link_air-fryer}}", both)
        self.assertRegex(email["subject"], r"air fryer")

    def test_weekly_gate_dedupe(self):
        gate = _wd.weekly_gate("air fryer", last_week="2026-W37",
                               week="2026-W38")
        self.assertTrue(gate["due"])
        gate2 = _wd.weekly_gate("air fryer", last_week="2026-W38",
                                week="2026-W38")
        self.assertFalse(gate2["due"])

    def test_money_email_ab_overrides(self):
        """MME-10 money-step A/B: a subject + hero CTA override flows into the
        digest without breaking the single-token money seam."""
        email = _wd.digest_email("air fryer", self.winners, self.deals,
                                 referrers=self.referrers, week="2026-W38",
                                 subject="AIR A", hook="grab today's pick")
        self.assertEqual(email["subject"], "AIR A")
        self.assertIn("grab today's pick", email["text"])
        self.assertIn("grab today's pick", email["html"])
        both = email["text"] + email["html"]
        self.assertEqual(both.count("{{tracked_link}}"), 1)
        self.assertNotIn("see it", email["html"])
        # empty overrides keep the defaults
        dflt = _wd.digest_email("air fryer", self.winners, self.deals,
                                referrers=self.referrers, week="2026-W38",
                                subject=" ", hook="  ")
        self.assertRegex(dflt["subject"], r"air fryer")
        self.assertIn("see it", dflt["html"])

    def test_determinism_repeat(self):
        e1 = _wd.digest_email("air fryer", self.winners, self.deals,
                              referrers=self.referrers, week="2026-W38")
        e2 = _wd.digest_email("air fryer", self.winners, self.deals,
                              referrers=self.referrers, week="2026-W38")
        self.assertEqual(e1["subject"], e2["subject"])
        self.assertEqual(e1["text"], e2["text"])
        self.assertEqual(e1["html"], e2["html"])


if __name__ == "__main__":
    _ut.main()
