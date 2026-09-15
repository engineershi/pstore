# -*- coding: utf-8 -*-
import json
import unittest

import publish
import social


class FakeResp:
    def __init__(self, code, body=b"{}"):
        self.code = code
        self._body = body

    def status(self):
        return self.code

    def read(self):
        return self._body


class Base(unittest.TestCase):
    def setUp(self):
        self._orig_post = publish._post
        self._orig_get = publish._get
        self._orig_img = publish.og_image
        publish._PINT_BOARD_CACHE.clear()
        publish._PINT_BOARDS_CACHE.clear()
        publish.og_image = lambda url: "https://example.com/og.png"
        self.captured = []
        self.requests = []

    def tearDown(self):
        publish._post = self._orig_post
        publish._get = self._orig_get
        publish.og_image = self._orig_img

    def _ok(self, payload=None, boards=None):
        boards = boards if boards is not None else [{"id": "board-1", "name": "Default"}]
        def fake(url, payload_, headers, timeout=15):
            self.requests.append((url, payload_, headers))
            if "/v5/boards" in url and payload_.get("name"):
                # POST /v5/boards (auto-create) -> fresh board id
                return 201, {"id": "board-new", "name": payload_.get("name")}
            if "/v5/boards" in url:
                return 200, {"items": boards}
            return 201, (payload or json.loads('{"id":"r1"}' if url.endswith("/pins") else '{}'))
        publish._post = fake
        publish._get = lambda url, headers, timeout=15: fake(url, {}, headers, timeout=timeout)

    def _keys(self, mapping=None):
        mapping = mapping or {}
        def kv(ns, name):
            return mapping.get((ns, name), "")
        return kv

    def _kit(self, platform, body="Hello world", keyword=""):
        return {"platform": platform, "slug": "keto", "body": body,
                "link": "https://x/lp/keto?utm_content=ab", "name": "post",
                "keyword": keyword, "hashtags": "#keto"}


class TestNativeScaffold(Base):
    def test_twitter_requires_keys_then_posts(self):
        self._ok()
        res = publish.post_to("Twitter / X", self._kit("Twitter / X"),
                              self._keys({("twitter", "client_id"): "K",
                                          ("twitter", "client_secret"): "S",
                                          ("twitter", "token"): "T",
                                          ("twitter", "token_secret"): "TS"}))
        self.assertTrue(res["ok"])
        self.assertEqual(res["via"], "native")
        url = self.requests[0][0]
        self.assertIn("api.twitter.com/2/tweets", url)
        self.assertIn("OAuth", self.requests[0][2].get("Authorization", ""))

    def test_platform_skipped_without_keys(self):
        res = publish.post_to("Twitter / X", self._kit("Twitter / X"),
                              self._keys())
        self.assertFalse(res["ok"])
        self.assertEqual(res["via"], "skipped")
        self.assertEqual(self.requests, [])  # no network hit

    def test_pinterest_posts_with_token(self):
        self._ok()
        res = publish.post_to("Pinterest", self._kit("Pinterest"),
                              self._keys({("pinterest", "token"): "PIN"}))
        self.assertTrue(res["ok"])
        self.assertEqual(res["via"], "native")
        boards = [(u, p, h) for u, p, h in self.requests if "/v5/boards" in u]
        pins = [(u, p, h) for u, p, h in self.requests if "/v5/pins" in u]
        self.assertEqual(len(boards), 1, "board lookup happens once")
        self.assertEqual(len(pins), 1)
        self.assertIn("api.pinterest.com/v5/pins", pins[0][0])
        self.assertEqual(pins[0][1]["board_id"], "board-1")
        self.assertEqual(pins[0][1]["media_source"], {"source_type": "image_url",
                                                      "url": "https://example.com/og.png"})
        self.assertIn("Bearer PIN", pins[0][2].get("Authorization", ""))

    def test_pinterest_board_name_override_and_cache(self):
        self._ok()
        # Two posts with the same token+board resolve the board once (cache).
        publish.post_to("Pinterest", self._kit("Pinterest"),
                        self._keys({("pinterest", "token"): "PIN",
                                    ("pinterest", "board"): "Keto Zone"}))
        res = publish.post_to("Pinterest", self._kit("Pinterest"),
                              self._keys({("pinterest", "token"): "PIN",
                                          ("pinterest", "board"): "Keto Zone"}))
        self.assertTrue(res["ok"])
        boards = [p for p, _, _ in self.requests if "/v5/boards" in p]
        self.assertEqual(len(boards), 1)

    def test_pinterest_no_board_fails_natively(self):
        self._ok(boards=[])
        res = publish.post_to("Pinterest", self._kit("Pinterest"),
                              self._keys({("pinterest", "token"): "PIN"}))
        self.assertFalse(res["ok"])
        self.assertEqual(res["via"], "native")
        self.assertIn("no board", res["message"].lower())

    def test_pinterest_auto_creates_per_niche_board(self):
        # Zero-follower playbook: a fresh niche pin (keyword present) with no
        # matching board creates its own board on the account, then lands there.
        self._ok(boards=[{"id": "board-1", "name": "Default"}])
        res = publish.post_to("Pinterest", self._kit("Pinterest", body="Keto picks",
                                                     keyword="keto snacks"),
                              self._keys({("pinterest", "token"): "PIN",
                                          ("pinterest", "auto_board"): "1",
                                          ("pinterest", "max_boards"): "15"}))
        self.assertTrue(res["ok"])
        creates = [(u, p, h) for u, p, h in self.requests
                   if "/v5/boards" in u and isinstance(p, dict) and p.get("name")]
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0][1]["name"], "Keto Snacks")
        pins = [p for u, p, _ in self.requests
                if "/v5/pins" in u]
        self.assertEqual(pins[-1]["board_id"], "board-new")

    def test_pinterest_no_auto_create_when_disabled(self):
        self._ok(boards=[{"id": "board-1", "name": "Default"}])
        res = publish.post_to("Pinterest", self._kit("Pinterest", body="Keto picks",
                                                     keyword="keto snacks"),
                              self._keys({("pinterest", "token"): "PIN",
                                          ("pinterest", "auto_board"): "0"}))
        self.assertTrue(res["ok"])
        creates = [(u, p, h) for u, p, h in self.requests
                   if "/v5/boards" in u and isinstance(p, dict) and p.get("name")]
        self.assertEqual(creates, [])
        pins = [p for u, p, _ in self.requests
                if "/v5/pins" in u]
        self.assertEqual(pins[-1]["board_id"], "board-1")

    def test_facebook_posts_with_token(self):
        self._ok()
        res = publish.post_to("Facebook", self._kit("Facebook"),
                              self._keys({("facebook", "token"): "FBTK"}))
        self.assertTrue(res["ok"])
        self.assertEqual(res["via"], "native")
        self.assertIn("graph.facebook.com", self.requests[0][0])

    def test_linkedin_posts_with_token(self):
        self._ok()
        res = publish.post_to("LinkedIn", self._kit("LinkedIn"),
                              self._keys({("linkedin", "token"): "LITK"}))
        self.assertTrue(res["ok"])
        self.assertEqual(res["via"], "native")
        self.assertIn("api.linkedin.com/v2/ugcPosts", self.requests[0][0])

    def test_telegram_skipped_without_token(self):
        res = publish.post_to("Telegram", self._kit("Telegram"), self._keys())
        self.assertFalse(res["ok"])
        self.assertEqual(res["via"], "skipped")
        self.assertEqual(self.requests, [])  # no network hit

    def test_telegram_requires_chat_when_only_token(self):
        res = publish.post_to("Telegram", self._kit("Telegram"),
                              self._keys({("telegram", "token"): "TG:tok"}))
        self.assertFalse(res["ok"])
        self.assertEqual(res["via"], "native")
        self.assertIn("chat", res["message"].lower())
        self.assertEqual(self.requests, [])

    def test_telegram_posts_message_with_token_and_chat(self):
        self._ok()
        res = publish.post_to("Telegram", self._kit("Telegram", "Ranked list here"),
                              self._keys({("telegram", "token"): "TG123:tok",
                                          ("telegram", "chat"): "@pstore"}))
        self.assertTrue(res["ok"])
        self.assertEqual(res["via"], "native")
        url, payload, _ = self.requests[0]
        self.assertIn("api.telegram.org/botTG123:tok/sendMessage", url)
        self.assertEqual(payload["chat_id"], "@pstore")
        self.assertIn("utm_content=ab", payload["text"])

    def test_telegram_token_bar_chat_folding(self):
        self._ok()
        res = publish.post_to("Telegram", self._kit("Telegram"),
                              self._keys({("telegram", "token"): "TG:tok|@chan"}))
        self.assertTrue(res["ok"])
        self.assertEqual(self.requests[0][1]["chat_id"], "@chan")

    def test_telegram_sends_photo_caption_when_image_present(self):
        self._ok()
        kit = self._kit("Telegram", "Hello world")
        kit["image_png"] = "https://x/og.png"
        res = publish.post_to("Telegram", kit,
                              self._keys({("telegram", "token"): "TG:tok",
                                          ("telegram", "chat"): "12345"}))
        self.assertTrue(res["ok"])
        url, payload, _ = self.requests[0]
        self.assertIn("/sendPhoto", url)
        self.assertEqual(payload["photo"], "https://x/og.png")
        self.assertIn("Hello world", payload["caption"])

    def test_unknown_platform_skipped(self):
        res = publish.post_to("Threads", self._kit("Threads"), self._keys())
        self.assertEqual(res["via"], "skipped")
        self.assertFalse(res["ok"])

    def test_publish_batch_never_raises_mixed(self):
        self._ok()
        kits = [self._kit("Twitter / X"), self._kit("Pinterest")]
        out = publish.publish_batch(kits, self._keys({("pinterest", "token"): "P"}))
        # twitter no keys -> skipped, pinterest -> native
        self.assertEqual(len(out), 2)
        by = {r["platform"]: r for r in out}
        self.assertEqual(by["Twitter / X"]["via"], "skipped")
        self.assertEqual(by["Pinterest"]["via"], "native")

    def test_pinterest_payload_is_seo_copy(self):
        self._ok()
        kit = self._kit("Pinterest")
        kit["keyword"] = "keto snacks"
        kit["hashtags"] = social.hashtags("keto snacks")
        res = publish.post_to("Pinterest", kit,
                              self._keys({("pinterest", "token"): "PIN"}))
        self.assertTrue(res["ok"])
        payload = self.requests[-1][1]
        self.assertIn("keto snacks", payload["title"].lower())
        self.assertIn("#", payload["description"])
        self.assertIn("utm_content=ab", payload["description"])
        self.assertLessEqual(len(payload["title"]), 100)
        self.assertLessEqual(len(payload["description"]), 500)

    def test_pin_title_prefers_keyword_when_body_misses_it(self):
        title = publish._pin_title(
            "See our top list of picks for the best gadget by far today",
            "best gadgets")
        self.assertTrue(title.lower().startswith("best gadgets"))
        self.assertLessEqual(len(title), 95)

    def test_pin_copy_strips_emoji(self):
        title = publish._pin_title("Amazing pick \U0001F680", "")
        self.assertEqual(title, "Amazing pick")
        desc = publish._pin_desc("Great deals \U0001F600",
                                 "https://x/lp/a", "#BestPicks", "keto")
        self.assertNotIn("\U0001F600", desc)
        self.assertIn("#BestPicks", desc)
        self.assertLessEqual(len(desc), 500)

    def test_http_error_reports_not_ok_not_raises(self):
        def fake(url, payload_, headers, timeout=15):
            return 429, {"errors": []}
        publish._post = fake
        res = publish.post_to("LinkedIn", self._kit("LinkedIn"),
                              self._keys({("linkedin", "token"): "L"}))
        self.assertFalse(res["ok"])
        self.assertEqual(res["via"], "native")


class TestServerNativeWiring(unittest.TestCase):
    def setUp(self):
        import server as srv
        self.srv = srv
        self._orig_twitter = self.srv._get_setting("social.key.twitter")

    def tearDown(self):
        self.srv._set_setting("social.key.twitter", self._orig_twitter if self._orig_twitter else "")

    def test_publish_native_uses_pasted_keys(self):
        import server
        server._set_setting("social.key.twitter", "CK|CS|AT|ATS")
        hits = []
        def fake(url, payload_, headers, timeout=15):
            hits.append((url, payload_, headers))
            return 201, {"data": {"id": "12345"}}
        publish._post = fake
        kit = {"platform": "Twitter / X", "slug": "keto", "body": "Best keto ranked",
               "link": "https://x/lp/keto?utm_content=ab1", "name": "p"}
        res = server._publish_native([kit])
        self.assertEqual(len(res), 1)
        self.assertTrue(res[0]["ok"])
        self.assertEqual(res[0]["via"], "native")
        self.assertEqual(hits[0][0], "https://api.twitter.com/2/tweets")
        self.assertTrue(publish._post is fake)