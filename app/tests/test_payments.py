# -*- coding: utf-8 -*-
"""Offline tests for the owned-revenue engine: Stripe signature verification,
idempotent entitlement granting, and the gated delivery of a bought PDF.

No network. The webhook path is exercised through the real handler with a fake
socket, and delivery is captured by stubbing ``mailer.send_email``.
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

import payments
import seo
import server

SECRET = "whsec_test_secret_value"


class TestSignature(unittest.TestCase):
    def setUp(self):
        self.raw = b'{"id":"evt_1","type":"checkout.session.completed"}'

    def test_valid_signature_accepted(self):
        header = payments.sign_for_test(self.raw, SECRET)
        self.assertTrue(payments.verify_signature(self.raw, header, SECRET))

    def test_wrong_secret_rejected(self):
        header = payments.sign_for_test(self.raw, "whsec_other")
        self.assertFalse(payments.verify_signature(self.raw, header, SECRET))

    def test_tampered_body_rejected(self):
        header = payments.sign_for_test(self.raw, SECRET)
        tampered = self.raw.replace(b"evt_1", b"evt_2")
        self.assertFalse(payments.verify_signature(tampered, header, SECRET))

    def test_empty_inputs_rejected(self):
        self.assertFalse(payments.verify_signature(b"", "", SECRET))
        self.assertFalse(payments.verify_signature(self.raw, "", SECRET))
        self.assertFalse(payments.verify_signature(self.raw, "t=1,v1=x", ""))

    def test_stale_timestamp_rejected(self):
        old = int(time.time()) - (payments.SIG_TOLERANCE_SEC + 60)
        header = payments.sign_for_test(self.raw, SECRET, timestamp=old)
        self.assertFalse(payments.verify_signature(self.raw, header, SECRET))

    def test_fresh_timestamp_accepted(self):
        now = int(time.time())
        header = payments.sign_for_test(self.raw, SECRET, timestamp=now)
        self.assertTrue(payments.verify_signature(self.raw, header, SECRET))

    def test_malformed_header_rejected(self):
        self.assertFalse(payments.verify_signature(self.raw, "garbage", SECRET))


class _FakeHandler(server.Handler):
    """Minimal request double: headers + a readable body, plus the real
    `_stripe_webhook` so the route's own logic is what gets tested."""

    def __init__(self, path, body, headers=None):
        self.path = path
        self.headers = dict(headers or {})
        self.headers.setdefault("Content-Length", str(len(body)))
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self._head_only = False
        self._req_start = time.time()
        self.status = 0
        self.sent = None

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        self.status = code
        self.sent = body
        return None

    def send_response(self, code):
        self.status = code

    def send_header(self, *a):
        pass

    def end_headers(self):
        pass

    def _client_ip(self):
        return "127.0.0.1"

    def log_message(self, *a):
        pass


class TestWebhookRoute(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp()
        os.close(fd)
        self._prior_db = server.DB
        server.DB = self.db
        server._db_schema_ready = False
        payments.init_db(server._db())
        self._sent = []
        self._mailer = patch.object(
            server.mailer, "send",
            side_effect=lambda *a, **k: (self._sent.append((a, k)) or True))
        self._mailer.start()
        self.addCleanup(self._mailer.stop)

    def tearDown(self):
        server.DB = self._prior_db
        server._db_schema_ready = False
        try:
            os.unlink(self.db)
        except OSError:
            pass

    def _event(self, email="buyer@example.com", event_id="evt_1",
               amount=3900, currency="usd"):
        return {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_test_123",
                "amount_total": amount,
                "currency": currency,
                "customer_details": {"email": email},
                "metadata": {"product": "playbook"},
            }},
        }

    def _post(self, event, sign_with=SECRET, env_secret=SECRET, tamper=False,
              path="/webhook/stripe"):
        raw = json.dumps(event).encode("utf-8")
        header = payments.sign_for_test(raw, sign_with)
        if tamper:
            raw = raw.replace(b"buyer@example.com", b"attacker@example.com")
        h = _FakeHandler(path, raw, {"Stripe-Signature": header,
                                     "Content-Type": "application/json"})
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": env_secret}):
            server.Handler._stripe_webhook(h)
        return h

    def test_valid_payment_grants_entitlement_and_emails(self):
        h = self._post(self._event())
        self.assertEqual(200, h.status)
        self.assertTrue(h.sent.get("granted"))
        self.assertTrue(payments.entitled("buyer@example.com"))
        self.assertEqual(1, len(self._sent))

    def test_unsigned_payload_refused(self):
        h = self._post(self._event(), env_secret="whsec_a_different_secret")
        self.assertEqual(400, h.status)
        self.assertFalse(payments.entitled("buyer@example.com"))

    def test_tampered_payload_refused(self):
        h = self._post(self._event(), tamper=True)
        self.assertEqual(400, h.status)
        self.assertFalse(payments.entitled("attacker@example.com"))

    def test_replay_does_not_double_grant(self):
        self._post(self._event())
        h = self._post(self._event())
        self.assertEqual(200, h.status)
        self.assertTrue(h.sent.get("duplicate"))
        self.assertEqual(1, len(self._sent))

    def test_missing_secret_refuses_everything(self):
        h = self._post(self._event(), env_secret="")
        self.assertEqual(503, h.status)
        self.assertFalse(payments.entitled("buyer@example.com"))

    def test_event_without_email_grants_nothing(self):
        ev = self._event(email="")
        h = self._post(ev)
        self.assertEqual(200, h.status)
        self.assertFalse(h.sent.get("granted"))
        self.assertEqual(0, len(self._sent))

    def test_unrelated_event_ignored(self):
        ev = {"id": "evt_9", "type": "invoice.created",
              "data": {"object": {"customer_email": "buyer@example.com"}}}
        h = self._post(ev)
        self.assertEqual(200, h.status)
        self.assertFalse(h.sent.get("granted"))
        self.assertFalse(payments.entitled("buyer@example.com"))

    def test_no_event_without_order_row(self):
        self._post(self._event(amount=4900, currency="usd"))
        orders = payments.recent_orders()
        self.assertEqual(1, len(orders))
        self.assertEqual(4900, orders[0]["amount_cents"])
        self.assertEqual("usd", orders[0]["currency"])
        self.assertEqual("buyer@example.com", orders[0]["email"])


class TestEntitlementAndTokens(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp()
        os.close(fd)
        self._prior_db = server.DB
        server.DB = self.db
        server._db_schema_ready = False
        payments.init_db(server._db())

    def tearDown(self):
        server.DB = self._prior_db
        server._db_schema_ready = False
        try:
            os.unlink(self.db)
        except OSError:
            pass

    def test_entitled_requires_grant(self):
        self.assertFalse(payments.entitled("nobody@example.com"))
        payments.grant("nobody@example.com", "playbook", "cs_1", 3900, "usd")
        self.assertTrue(payments.entitled("nobody@example.com"))
        self.assertTrue(payments.entitled("nobody@example.com", "playbook"))

    def test_grant_is_idempotent(self):
        self.assertTrue(payments.grant("a@example.com", "playbook", "cs_1"))
        self.assertTrue(payments.grant("a@example.com", "playbook", "cs_1"))
        self.assertEqual(1, payments.entitlement_count())
        self.assertEqual(1, payments.order_count())

    def test_empty_email_never_granted(self):
        self.assertFalse(payments.grant("", "playbook", "cs_1"))
        self.assertFalse(payments.entitled(""))

    def test_download_token_roundtrip(self):
        payments.grant("b@example.com", "playbook", "cs_2")
        token = payments.download_token("b@example.com")
        self.assertTrue(token)
        self.assertEqual("b@example.com",
                         payments.verify_download_token(token))

    def test_download_token_rejected_for_non_buyer(self):
        token = payments.download_token("stranger@example.com")
        self.assertEqual("", payments.verify_download_token(token))

    def test_download_token_rejected_when_garbage(self):
        self.assertEqual("", payments.verify_download_token("not-a-token"))
        self.assertEqual("", payments.verify_download_token(""))

    def test_download_token_expires(self):
        payments.grant("c@example.com", "playbook", "cs_3")
        scope = "dl:c@example.com:playbook"
        with patch.object(payments.security, "make_token",
                          return_value=scope), \
             patch.object(payments.security, "verify_token", return_value=scope):
            token = payments.download_token("c@example.com")
            self.assertEqual("c@example.com",
                             payments.verify_download_token(token))
        expired = "dl:c@example.com:playbook:1:deadbeef"
        with patch.object(payments.security, "verify_token",
                          return_value=None):
            self.assertEqual("", payments.verify_download_token(expired))

    def test_note_download_increments(self):
        payments.grant("d@example.com", "playbook", "cs_4")
        payments.note_download("d@example.com")
        payments.note_download("d@example.com")
        conn = payments._db()
        try:
            row = conn.execute("SELECT downloads FROM paid_entitlements"
                               " WHERE email=?", ("d@example.com",)).fetchone()
        finally:
            conn.close()
        self.assertEqual(2, row[0])


class TestOfferConfig(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp()
        os.close(fd)
        self._prior_db = server.DB
        server.DB = self.db
        server._db_schema_ready = False
        server._db().close()

    def tearDown(self):
        server.DB = self._prior_db
        server._db_schema_ready = False
        try:
            os.unlink(self.db)
        except OSError:
            pass

    def test_unconfigured_offer_is_not_advertised(self):
        self.assertFalse(payments.configured())
        self.assertEqual("", payments.checkout_url())

    def test_checkout_link_flips_configured(self):
        server._set_setting("paid_checkout_url", "https://buy.stripe.com/abc")
        self.assertTrue(payments.configured())
        self.assertEqual("https://buy.stripe.com/abc", payments.checkout_url())

    def test_env_webhook_secret_wins(self):
        server._set_setting("stripe_webhook_secret", "whsec_stored")
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": "whsec_env"}):
            self.assertEqual("whsec_env", payments.webhook_secret())
        os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
        self.assertEqual("whsec_stored", payments.webhook_secret())

    def test_status_leaks_no_secret(self):
        server._set_setting("stripe_webhook_secret", "whsec_supersecretvalue")
        st = payments.status()
        self.assertTrue(st["has_webhook_secret"])
        self.assertNotIn("supersecret", json.dumps(st))

    def test_build_pdf_returns_none_without_niche(self):
        self.assertIsNone(payments.build_pdf())


def _text(val):
    """Renderers return bytes; the assertions here are about the markup."""
    return val.decode("utf-8") if isinstance(val, bytes) else val


class TestOfferIsReachable(unittest.TestCase):
    """The offer page used to exist, convert, and be linked from nowhere — a
    checkout nobody can reach earns exactly nothing. These lock the two paths
    that make it findable: the footer link and the sitemap entry.

    Both are gated on `sellable()` — checkout link AND webhook signing secret.
    A link on its own is not a reachable offer, it is a reachable way to charge
    someone for something this deployment cannot deliver."""

    LINK = "https://buy.stripe.com/abc"
    SECRET = "whsec_test"

    def setUp(self):
        fd, self.db = tempfile.mkstemp()
        os.close(fd)
        self._prior_db = server.DB
        self._prior_flag = seo.PAID_OFFER_LIVE
        server.DB = self.db
        server._db_schema_ready = False
        server._db().close()
        os.environ.pop("STRIPE_WEBHOOK_SECRET", None)

    def tearDown(self):
        server.DB = self._prior_db
        server._db_schema_ready = False
        seo.PAID_OFFER_LIVE = self._prior_flag
        os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
        try:
            os.unlink(self.db)
        except OSError:
            pass

    def _go_live(self):
        server._set_setting("paid_checkout_url", self.LINK)
        server._set_setting("stripe_webhook_secret", self.SECRET)
        server._sync_paid_flag()

    def test_footer_hides_pro_until_a_checkout_link_exists(self):
        server._sync_paid_flag()
        self.assertFalse(seo.PAID_OFFER_LIVE)
        self.assertNotIn('href="/pro"', _text(seo._footer()))

    def test_footer_links_pro_once_sellable(self):
        self._go_live()
        self.assertTrue(seo.PAID_OFFER_LIVE)
        self.assertIn('href="/pro"', _text(seo._footer()))

    def test_sitemap_lists_pro_only_while_it_exists(self):
        # /pro 404s until sellable, and a sitemap entry that 404s is the exact
        # robots/sitemap contradiction the sitemap exists to prevent.
        def sitemap():
            return _text(server.Handler.__new__(server.Handler)._sitemap())
        server._sync_paid_flag()
        self.assertNotIn("/pro", sitemap())
        self._go_live()
        self.assertRegex(sitemap(), r"<loc>[^<]*/pro</loc>")


class TestOfferIsNotSellableUntilItCanDeliver(unittest.TestCase):
    """Regression: the offer used to go live on the checkout link alone.

    The webhook signature is the only authentication on POST /webhook/stripe
    and `handle_event` is reached from nowhere else, so with no signing secret
    stored every `checkout.session.completed` hits the route's 503 and no
    entitlement is ever granted. A live buy button in that state charges people
    for a product this server cannot send, so the link must not be published."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp()
        os.close(fd)
        self._prior_db = server.DB
        self._prior_flag = seo.PAID_OFFER_LIVE
        server.DB = self.db
        server._db_schema_ready = False
        server._db().close()
        os.environ.pop("STRIPE_WEBHOOK_SECRET", None)

    def tearDown(self):
        server.DB = self._prior_db
        server._db_schema_ready = False
        seo.PAID_OFFER_LIVE = self._prior_flag
        os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
        try:
            os.unlink(self.db)
        except OSError:
            pass

    def _sitemap(self):
        return _text(server.Handler.__new__(server.Handler)._sitemap())

    def test_link_without_secret_is_configured_but_not_sellable(self):
        server._set_setting("paid_checkout_url", "https://buy.stripe.com/abc")
        self.assertTrue(payments.configured())
        self.assertFalse(payments.sellable())
        self.assertFalse(payments.status()["sellable"])

    def test_secret_without_link_is_not_sellable(self):
        server._set_setting("stripe_webhook_secret", "whsec_test")
        self.assertFalse(payments.sellable())

    def test_undeliverable_offer_is_not_linked_from_the_footer(self):
        server._set_setting("paid_checkout_url", "https://buy.stripe.com/abc")
        server._sync_paid_flag()
        self.assertFalse(seo.PAID_OFFER_LIVE)
        self.assertNotIn('href="/pro"', _text(seo._footer()))

    def test_undeliverable_offer_is_not_in_the_sitemap(self):
        server._set_setting("paid_checkout_url", "https://buy.stripe.com/abc")
        server._sync_paid_flag()
        self.assertNotIn("/pro", self._sitemap())

    def test_the_page_itself_404s(self):
        """The link is the easy half to add and the easy half to forget the
        partner of, so the page itself has to refuse too — not just the
        footer and the sitemap."""
        server._set_setting("paid_checkout_url", "https://buy.stripe.com/abc")
        sent = {}
        h = server.Handler.__new__(server.Handler)
        h._send = lambda code, body, ctype: sent.update(
            code=code, body=body if isinstance(body, str) else body.decode())
        h._pro_offer_html()
        self.assertEqual(404, sent["code"])
        self.assertIn("Page not found", sent["body"])

    def test_env_secret_alone_makes_it_sellable(self):
        """A deploy that carries the signing secret in the environment rather
        than the DB must not be told its offer is broken."""
        server._set_setting("paid_checkout_url", "https://buy.stripe.com/abc")
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": "whsec_env"}):
            self.assertTrue(payments.sellable())
            server._sync_paid_flag()
            self.assertTrue(seo.PAID_OFFER_LIVE)
            self.assertRegex(self._sitemap(), r"<loc>[^<]*/pro</loc>")


if __name__ == "__main__":
    unittest.main()