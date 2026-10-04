# -*- coding: utf-8 -*-
import json
import unittest
import urllib.error
import urllib.request

import amazon
import paapi


def _reset_cfg():
    paapi._configure_clear()


class FakeResponse:
    def __init__(self, code=200, body=b"{}", headers=None):
        self.code = code
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body


class FakeHTTPError(urllib.error.HTTPError):
    """urllib raises on a non-2xx, but PA-API also returns structured 429
    bodies with the throttle type inside them."""

    def __init__(self, code, headers=None):
        super().__init__("https://webservices.amazon.com/paapi5/getitems",
                         code, "throttled", {}, None)
        self.headers = headers or {}
        self._payload = json.dumps({
            "__type": "com.amazon.paapi5.v1.ThrottledException"}).encode()

    def read(self):
        return self._payload


class Base(unittest.TestCase):
    def setUp(self):
        _reset_cfg()
        self._orig_urlopen = amazon._urlopen
        self._orig_sleep = paapi.time.sleep
        self._orig_interval = paapi.MIN_INTERVAL
        self.captured = {}
        self.slept = []
        # Throttling is real in production and must not make the suite take
        # minutes: record the sleeps instead of performing them.
        paapi.time.sleep = lambda s: self.slept.append(round(s, 3))
        paapi.MIN_INTERVAL = 0.0
        paapi._last_call[0] = 0.0

    def tearDown(self):
        amazon._urlopen = self._orig_urlopen
        paapi.time.sleep = self._orig_sleep
        paapi.MIN_INTERVAL = self._orig_interval
        paapi._last_call[0] = 0.0
        _reset_cfg()

    def _stub(self, payload):
        def fake(req, timeout=None):
            self.captured["url"] = req.full_url
            self.captured["body"] = req.data
            self.captured["method"] = req.get_method()
            self.captured["headers"] = dict(req.headers)
            self.captured["unredirected"] = dict(req.unredirected_hdrs)
            return FakeResponse(200, json.dumps(payload).encode("utf-8"))
        amazon._urlopen = fake


class TestPaapiGate(Base):
    def test_disabled_without_creds(self):
        self.assertEqual(paapi.get_items(["B0KETO1234"]), None)
        self.assertEqual(paapi.lookup("B0KETO1234"), None)
        self.assertFalse(paapi.ready())

    def test_configure_and_status(self):
        paapi.configure("ACC", "SEC", "tag-20")
        self.assertTrue(paapi.ready())
        st = paapi.status()
        self.assertTrue(st["has_access_key"])
        self.assertNotIn("ACC", json.dumps(st))  # masked / not echoed


class TestRatingsAreFetched(Base):
    """A scraped star rating is Program Content. PA-API is the ONLY licensed
    source, and `normalize_item` hardcoded stars/reviews to None while
    get_items never requested the resources -- so the one legal rating source in
    the codebase was never fetched and every page rendered stars blank."""

    def setUp(self):
        super().setUp()
        paapi.configure("AKIAEXAMPLE", "SECRETSECRET", "tag-20")

    def test_requests_the_rating_resources(self):
        self._stub({"ItemsResult": {"Items": []}})
        paapi.get_items(["B0KETO1234"])
        res = json.loads(self.captured["body"])["Resources"]
        self.assertIn("ItemInfo.CustomerReviews", res)
        self.assertIn("ItemInfo.ByLineInfo", res)

    def test_customer_reviews_shape(self):
        n = paapi.normalize_item({
            "ASIN": "B01", "ItemInfo": {"CustomerReviews": {
                "Count": 812, "AverageStarRating": "4.6 out of 5 stars"}}})
        self.assertEqual(n["stars"], "4.6")
        self.assertEqual(n["reviews"], 812)

    def test_byline_shape_with_thousands_separator(self):
        n = paapi.normalize_item({"ASIN": "B02", "ItemInfo": {"ByLineInfo": {
            "Contributors": [{"Name": "4.4 out of 5 stars", "Role": "star_rating"},
                             {"Name": "1,203 ratings", "Role": "rating"}]}}})
        self.assertEqual(n["stars"], "4.4")
        self.assertEqual(n["reviews"], 1203)

    def test_bare_numeric_rating_is_accepted(self):
        n = paapi.normalize_item({"ASIN": "B03", "ItemInfo": {
            "CustomerReviews": {"Count": 5, "AverageStarRating": "4.25"}}})
        # One decimal, matching how seo.py renders it: round(float(stars), 1)
        # is Python's banker's rounding, so 4.25 -> 4.2, not 4.3. Storing a
        # different value than we display would make the JSON-LD ratingValue
        # disagree with the visible stars.
        self.assertEqual(n["stars"], "4.2")

    def test_absent_rating_stays_none_and_never_becomes_zero(self):
        """A 0 would be published to readers as if Amazon had said so."""
        n = paapi.normalize_item({"ASIN": "B04", "ItemInfo": {}})
        self.assertIsNone(n["stars"])
        self.assertIsNone(n["reviews"])

    def test_count_without_stars_does_not_invent_a_score(self):
        n = paapi.normalize_item({"ASIN": "B05", "ItemInfo": {
            "CustomerReviews": {"Count": 7}}})
        self.assertIsNone(n["stars"])
        self.assertEqual(n["reviews"], 7)

    def test_ratings_now_flow_through_lookup(self):
        self._stub({"ItemsResult": {"Items": [{
            "ASIN": "B0KETO1234",
            "ItemInfo": {"Title": {"DisplayValue": "Keto Bar"},
                         "CustomerReviews": {"Count": 812,
                                             "AverageStarRating": "4.6 out of 5 stars"}},
        }]}})
        it = paapi.lookup("B0KETO1234")
        self.assertEqual((it["stars"], it["reviews"]), ("4.6", 812))
        self.assertEqual(it["source"], "paapi")
        self.assertEqual(amazon.licensed_rating(it), ("4.6", 812))

    def test_unparseable_rating_is_dropped_not_crashed_on(self):
        n = paapi.normalize_item({"ASIN": "B06", "ItemInfo": {
            "CustomerReviews": {"Count": "n/a",
                                "AverageStarRating": "see page"}}})
        self.assertIsNone(n["stars"])
        self.assertIsNone(n["reviews"])

class TestPaapiRequest(Base):
    def setUp(self):
        super().setUp()
        paapi.configure("AKIAEXAMPLE", "SECRETSECRET", "tag-20")

    def test_get_items_sends_signed_post_to_paapi(self):
        self._stub({"ItemsResult": {"Items": []}})
        paapi.get_items(["b0keto1234"])
        h = {k.lower(): v for k, v in self.captured["headers"].items()}
        self.assertEqual(self.captured["method"], "POST")
        self.assertTrue(self.captured["url"].startswith(
            "https://webservices.amazon.com/paapi5/getitems"))
        auth = h.get("authorization", "")
        self.assertIn("AWS4-HMAC-SHA256", auth)
        self.assertIn("ProductAdvertisingAPI", auth)
        self.assertIn("AKIAEXAMPLE", auth)
        body = json.loads(self.captured["body"])
        self.assertEqual(body["PartnerTag"], "tag-20")
        self.assertEqual(body["ItemIds"], ["B0KETO1234"])
        self.assertEqual(h["content-encoding"], "amz-1.0")

    def test_lookup_normalizes_item(self):
        payload = {
            "ItemsResult": {"Items": [{
                "ASIN": "B0KETO1234",
                "ItemInfo": {"Title": {"DisplayValue": "Keto Bar Crunch"}},
                "Offers": {"Listings": [
                    {"Price": {"Amount": "12.99", "Currency": "USD"}}]},
            }]}
        }
        self._stub(payload)
        it = paapi.lookup("B0KETO1234")
        self.assertEqual(it["asin"], "B0KETO1234")
        self.assertEqual(it["title"], "Keto Bar Crunch")
        self.assertEqual(it["price"], "12.99")
        self.assertEqual(it["currency"], "USD")
        self.assertEqual(it["source"], "paapi")
        self.assertTrue(it["url"].startswith("https://"))

    def test_lookup_returns_none_when_asin_not_in_results(self):
        self._stub({"ItemsResult": {"Items": [{"ASIN": "B0OTHER0"}]}})
        self.assertIsNone(paapi.lookup("B0KETO1234"))

    def test_marketplace_follows_the_configured_market(self):
        """A store on another TLD must not sign requests against amazon.com --
        that is a valid-looking request whose partner tag earns nothing."""
        self._stub({"ItemsResult": {"Items": []}})
        self._orig_market = amazon.MARKET
        try:
            amazon.MARKET = "co.uk"
            paapi.get_items(["B0KETO1234"])
        finally:
            amazon.MARKET = self._orig_market
        self.assertEqual(json.loads(self.captured["body"])["Marketplace"],
                         "www.amazon.co.uk")

    def test_throttled_response_is_retried_not_silently_dropped(self):
        """The single most expensive PA-API bug: a 429 hit the bare
        `except Exception` and returned None, which is indistinguishable from
        bad credentials. The caller then concludes the key is dead."""
        calls = []

        def fake(req, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise FakeHTTPError(429, {"Retry-After": "2"})
            return FakeResponse(200, json.dumps({"ItemsResult": {"Items": []}}).encode())
        amazon._urlopen = fake
        self.assertEqual(paapi.get_items(["B0KETO1234"]),
                         {"ItemsResult": {"Items": []}})
        self.assertEqual(len(calls), 2)
        self.assertIn(2.0, self.slept)  # honored Retry-After

    def test_persistent_throttle_gives_up_after_bounded_attempts(self):
        calls = []

        def fake(req, timeout=None):
            calls.append(1)
            raise FakeHTTPError(429, {"Retry-After": "1"})
        amazon._urlopen = fake
        self.assertIsNone(paapi.get_items(["B0KETO1234"]))
        self.assertEqual(len(calls), paapi.MAX_ATTEMPTS)

    def test_auth_failure_is_not_retried(self):
        """401/403 mean the key is wrong. Retrying just burns the quota and
        delays the error the operator needs to see."""
        calls = []

        def fake(req, timeout=None):
            calls.append(1)
            raise FakeHTTPError(403)
        amazon._urlopen = fake
        self.assertIsNone(paapi.get_items(["B0KETO1234"]))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.slept, [])

    def test_calls_are_paced_to_the_published_quota(self):
        self._stub({"ItemsResult": {"Items": []}})
        paapi.MIN_INTERVAL = 0.25
        try:
            paapi._last_call[0] = 0.0
            paapi.get_items(["B0KETO1234"])
            paapi.get_items(["B0KETO1234"])
        finally:
            paapi.MIN_INTERVAL = 0.0
        self.assertTrue(any(s >= 0.2 for s in self.slept),
                        "expected a pacing sleep between calls")


if __name__ == "__main__":
    unittest.main()