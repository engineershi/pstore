# -*- coding: utf-8 -*-
"""Offline tests for the paid-traffic hooks: session affiliate-tag override
(served through amazon.affiliate_url / market_engine.clean_tag) and the
Handler._apply_paid_session UTM detection that swaps the tag per request."""

import importlib
import os
import unittest

sys_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if sys_path not in os.sys.path:
    os.sys.path.insert(0, sys_path)

import amazon
import market_engine


class TestAffiliateTagOverride(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tags = (amazon.AFFILIATE_TAG, market_engine.clean_tag())
        amazon.AFFILIATE_TAG = "ORG-20"

    @classmethod
    def tearDownClass(cls):
        amazon.AFFILIATE_TAG = cls._tags[0]
        amazon.clear_session_tag()

    def test_default_tag_is_organic(self):
        amazon.clear_session_tag()
        url = amazon.affiliate_url("B000000001")
        self.assertIn("tag=ORG-20", url)
        self.assertNotIn("PAID-20", url)

    def test_session_tag_overrides_rendered_links(self):
        amazon.set_session_tag("PAID-20")
        url = amazon.affiliate_url("B000000001")
        self.assertIn("tag=PAID-20", url)
        self.assertNotIn("ORG-20", url)
        amazon.clear_session_tag()
        url = amazon.affiliate_url("B000000001")
        self.assertIn("tag=ORG-20", url)

    def test_explicit_tag_beats_session(self):
        amazon.set_session_tag("PAID-20")
        url = amazon.affiliate_url("B000000002", tag="LOCAL-20")
        self.assertIn("tag=LOCAL-20", url)
        amazon.clear_session_tag()

    def test_market_engine_clean_tag_honors_session(self):
        amazon.set_session_tag("PAID-20")
        self.assertEqual(market_engine.clean_tag(), "PAID-20")
        amazon.clear_session_tag()
        self.assertEqual(market_engine.clean_tag(), "ORG-20")

    def test_no_tag_no_affiliate_param(self):
        amazon.clear_session_tag()
        saved = amazon.AFFILIATE_TAG
        amazon.AFFILIATE_TAG = ""
        try:
            self.assertNotIn("tag=", amazon.affiliate_url("B000000003"))
        finally:
            amazon.AFFILIATE_TAG = saved


class TestPaidSessionHook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server as srv
        importlib.reload(srv)
        cls.server = srv

    def _handler_with(self, path):
        h = self.server.Handler.__new__(self.server.Handler)
        h.path = path
        return h

    def test_paid_utm_sets_session_tag(self):
        os.environ["PSTORE_PAID_TAG"] = "PAIDTAG-20"
        try:
            h = self._handler_with("/n/keto-snacks?utm_source=paid&utm_campaign=fb1")
            h._apply_paid_session()
            self.assertEqual(amazon._active_tag(), "PAIDTAG-20")
        finally:
            os.environ.pop("PSTORE_PAID_TAG", None)
            amazon.clear_session_tag()

    def test_click_ids_set_session_tag(self):
        os.environ["PSTORE_PAID_TAG"] = "PAIDTAG-20"
        try:
            h = self._handler_with("/n/best-strings?fbclid=abc123")
            h._apply_paid_session()
            self.assertEqual(amazon._active_tag(), "PAIDTAG-20")
        finally:
            os.environ.pop("PSTORE_PAID_TAG", None)
            amazon.clear_session_tag()

    def test_organic_utm_does_not_override(self):
        amazon.AFFILIATE_TAG = "ORG-20"
        os.environ["PSTORE_PAID_TAG"] = "PAIDTAG-20"
        try:
            h = self._handler_with("/n/keto?utm_source=organic")
            h._apply_paid_session()
            self.assertEqual(amazon._active_tag(), "ORG-20")
        finally:
            os.environ.pop("PSTORE_PAID_TAG", None)
            amazon.clear_session_tag()

    def test_no_paid_tag_configured_is_noop(self):
        os.environ.pop("PSTORE_PAID_TAG", None)
        h = self._handler_with("/n/x?utm_source=paid")
        h._apply_paid_session()
        self.assertEqual(amazon._active_tag(), amazon.AFFILIATE_TAG or "")


if __name__ == "__main__":
    unittest.main()