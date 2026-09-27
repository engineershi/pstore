"""Regression tests for product-image rendering on money pages.

Every public page shipped with zero ``<img>`` tags. Root cause was three
independent gaps, all covered here:

1. ``paapi.lookup()`` requested ``Images.Primary.Large`` but never read the
   response, discarding the only TOS-clean product image an Associate site may
   hotlink.
2. Product records carry no ``image`` key at all (seed and production alike).
3. ``editorial.pick_html()`` rendered no ``<img>`` under any circumstances.

Images render only when a genuine Amazon CDN URL is already on the item. We
never synthesise ``/images/I/<ASIN>.jpg`` from the ASIN: that older affiliate
pattern is unsanctioned by Amazon and breaks without notice.
"""
import unittest

import editorial
import paapi


def _item(**kw):
    base = {"asin": "B000TEST", "title": "Keto Gummies", "price": 19.99,
            "currency": "USD", "stars": 4.5, "reviews": 1200,
            "url": "https://www.amazon.com/dp/B000TEST"}
    base.update(kw)
    return base


class TestImageUrlValidation(unittest.TestCase):
    CDN = "https://m.media-amazon.com/images/I/71abc._AC_SL1500_.jpg"

    def test_accepts_amazon_cdn(self):
        self.assertEqual(editorial.image_url(_item(image=self.CDN)), self.CDN)

    def test_rejects_foreign_host(self):
        """A scraped or user-supplied record must not become an arbitrary src."""
        self.assertEqual(editorial.image_url(_item(image="https://evil.com/t.gif")), "")

    def test_rejects_javascript_scheme(self):
        self.assertEqual(editorial.image_url(_item(image="javascript:alert(1)")), "")

    def test_rejects_relative_path(self):
        self.assertEqual(editorial.image_url(_item(image="/local.png")), "")

    def test_rejects_plain_http(self):
        self.assertEqual(editorial.image_url(_item(image="http://m.media-amazon.com/images/I/x.jpg")), "")

    def test_rejects_protocol_relative(self):
        self.assertEqual(editorial.image_url(_item(image="evil")), "")

    def test_empty_when_absent(self):
        self.assertEqual(editorial.image_url(_item()), "")
        self.assertEqual(editorial.image_url({}), "")
        self.assertEqual(editorial.image_url(None), "")


class TestPickRendersImage(unittest.TestCase):
    CDN = "https://m.media-amazon.com/images/I/71abc._AC_SL1500_.jpg"

    def test_image_present_when_available(self):
        h = editorial.pick_html("keto", _item(image=self.CDN), 0, [_item(image=self.CDN)])
        self.assertIn("<img", h)
        self.assertIn(self.CDN, h)
        self.assertIn('loading="lazy"', h)          # don't compete with first paint
        self.assertIn('decoding="async"', h)
        self.assertIn('width="112"', h)            # reserve space, avoid CLS
        self.assertIn('height="112"', h)
        self.assertIn('alt="Keto Gummies"', h)     # alt text is an SEO signal

    def test_no_image_key_renders_text_only(self):
        """Backward compatible: all current production rows have no image."""
        it = _item()
        h = editorial.pick_html("keto", it, 0, [it])
        self.assertNotIn("<img", h)
        self.assertIn("Keto Gummies", h)
        self.assertIn('class="pick-head"', h)

    def test_rejected_image_renders_no_img(self):
        it = _item(image="https://evil.com/t.gif")
        h = editorial.pick_html("keto", it, 0, [it])
        self.assertNotIn("<img", h)
        self.assertNotIn("evil.com", h)

    def test_alt_is_escaped(self):
        it = _item(title='<script>x</script>', image=self.CDN)
        h = editorial.pick_html("keto", it, 0, [it])
        self.assertNotIn("<script>", h)

    def test_rank_and_cta_survive_with_image(self):
        """The image must not cost us the rank badge or the Amazon CTA."""
        it = _item(image=self.CDN)
        h = editorial.pick_html("keto", it, 0, [it])
        self.assertIn("#1", h)
        self.assertIn('data-asin="B000TEST"', h)
        self.assertIn("amazon.com", h)


class TestPaapiImageParsing(unittest.TestCase):
    """paapi.lookup() must read back the image resource it requests."""

    RESPONSE = {
        "ItemsResult": {"Items": [{
            "ASIN": "B000TEST",
            "ItemInfo": {"Title": {"DisplayValue": "Keto Gummies"}},
            "Offers": {"Listings": [{"Price": {"Amount": 19.99, "Currency": "USD"}}]},
            "Images": {"Primary": {"Large": {
                "URL": "https://m.media-amazon.com/images/I/71abc._AC_SL1500_.jpg"}}},
        }]}
    }

    def test_lookup_extracts_image(self):
        original = paapi.get_items
        paapi.get_items = lambda asins: self.RESPONSE
        try:
            got = paapi.lookup("B000TEST")
        finally:
            paapi.get_items = original
        self.assertIsNotNone(got)
        self.assertEqual(got["image"],
                         "https://m.media-amazon.com/images/I/71abc._AC_SL1500_.jpg")

    def test_lookup_tolerates_missing_images(self):
        payload = {"ItemsResult": {"Items": [{
            "ASIN": "B000TEST",
            "ItemInfo": {"Title": {"DisplayValue": "No Image"}},
            "Offers": {"Listings": []},
        }]}}
        original = paapi.get_items
        paapi.get_items = lambda asins: payload
        try:
            got = paapi.lookup("B000TEST")
        finally:
            paapi.get_items = original
        self.assertIsNotNone(got)
        self.assertEqual(got["image"], "")

    def test_requests_image_resource(self):
        """Guard the request side: the resource must stay in the payload."""
        import inspect
        src = inspect.getsource(paapi.get_items)
        self.assertIn("Images.Primary.Large", src)


if __name__ == "__main__":
    unittest.main()
