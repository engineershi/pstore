# -*- coding: utf-8 -*-
"""Owned-revenue engine: sell our own product instead of relying on Amazon.

Everything the site earns today arrives as an Amazon commission, and Amazon
gates its own product API behind ten qualifying sales in thirty days — so the
commission line cannot be grown without first growing the thing that gates it.
This module is the unblocked alternative: an operator-run checkout that grants a
paid entitlement and delivers the product, with no marketplace in the loop.

Design constraints that shaped it:

* **No Stripe secret key on this server.** Checkout happens on a Stripe-hosted
  Payment Link, and the only Stripe-issued credential we store is the webhook
  signing secret, which cannot move money. A leaked secret key means fraud; a
  leaked signing secret means at worst a forged entitlement for a product we
  already sell.
* **Signature before parse.** The raw request body is HMAC-verified against the
  signing secret with a timestamp tolerance before it is deserialised, so a
  replayed or forged payload never reaches the grant path.
* **Idempotent.** Stripe retries webhooks. ``paid_events`` stores the event id,
  and a repeat delivery is acknowledged without re-granting or re-sending.
* **Delivery is best-effort and re-runnable.** The buyer always gets a signed,
  expiring download link in the email, so a failed PDF build costs a redelivery
  rather than a refund.

Offer copy, price, checkout link and delivery target live in ``settings`` so the
operator can change the product without a deploy. Reads are lazy and never raise:
with nothing configured every function reports "not configured" and no route
offers a dead checkout.
"""
import hashlib
import hmac
import json
import time

import security
import server

#: Stripe signs `t=<unix>,v1=<hex>` and the signed payload is `"{t}.{raw}"`.
SIG_TOLERANCE_SEC = 300

#: Re-download links expire; long enough to survive a weekend, short enough that a
#: leaked inbox does not hand over the product permanently.
DOWNLOAD_TTL_SEC = 7 * 24 * 3600


def _db():
    import sqlite3
    return sqlite3.connect(server.DB, timeout=30)


def _setting(key, default=""):
    try:
        return server._get_setting(key, default)
    except Exception:
        return default


def init_db(conn=None):
    """Create the paid-revenue tables. Safe to call on every boot."""
    own = conn is None
    if own:
        conn = _db()
    cur = conn.cursor()
    cur.execute("""
CREATE TABLE IF NOT EXISTS paid_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ref TEXT UNIQUE,
  email TEXT NOT NULL,
  product TEXT NOT NULL,
  amount_cents INTEGER DEFAULT 0,
  currency TEXT DEFAULT 'usd',
  status TEXT DEFAULT 'paid',
  created_at REAL
)""")
    cur.execute("""
CREATE TABLE IF NOT EXISTS paid_entitlements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL,
  product TEXT NOT NULL,
  order_ref TEXT,
  granted_at REAL,
  downloads INTEGER DEFAULT 0,
  last_download_at REAL,
  UNIQUE(email, product)
)""")
    cur.execute("""
CREATE TABLE IF NOT EXISTS paid_events (
  event_id TEXT PRIMARY KEY,
  type TEXT,
  email TEXT,
  product TEXT,
  received_at REAL
)""")
    if own:
        conn.commit()
        conn.close()


# ----------------------------------------------------------------- offer config
def offer():
    """The single paid offer, assembled from settings with safe defaults."""
    return {
        "name": _setting("paid_offer_name", "") or "Niche Playbook",
        "price_label": _setting("paid_offer_price", "") or "$39",
        "blurb": _setting("paid_offer_blurb", "") or (
            "The full ranked niche brief as a downloadable PDF: the picks, the "
            "reasoning behind each one, and the price band that fits."),
        "niche": _setting("paid_offer_niche", "") or "",
        "checkout_url": _setting("paid_checkout_url", "").strip(),
        "deliver_pdf": _setting("paid_offer_deliver_pdf", "1") not in ("0", "", "no"),
    }


def checkout_url():
    return offer()["checkout_url"]


def configured():
    """True when a buyer can reach a checkout page. Says nothing about delivery.

    Use `sellable` to decide whether the offer may be advertised; this only
    answers the narrower "is there a link to send someone to".
    """
    return bool(checkout_url())


def sellable():
    """True when a buyer who pays can actually receive the product.

    Both halves are required, and the webhook half is not optional politeness.
    The signature check is the *only* authentication on `POST /webhook/stripe`,
    and `handle_event` is reached from nowhere else, so a checkout link without
    a signing secret is not a half-configured offer — it is a broken one:

      * `configured()` used to be the whole gate, and /pro 404s until it passes,
        so pasting a Payment Link was enough to put a live "Get the Niche
        Playbook" button on 1,300+ pages;
      * every subsequent `checkout.session.completed` then hits the route's
        `if not payments.webhook_secret(): return 503` and Stripe retries into
        the same wall;
      * no entitlement is ever granted, no PDF is ever built, no email goes out,
        and the `paid_events` ledger stays empty, so the retry cannot even be
        recognised as a duplicate when the secret is finally added;
      * meanwhile the buyer has been charged and is writing to the support
        address on the delivery email that never arrived.

    Better to 404 the page and keep it out of the footer and the sitemap than to
    sell something this deployment cannot deliver.
    """
    return bool(checkout_url()) and bool(webhook_secret())


def webhook_secret():
    """Webhook signing secret. Env wins so a deploy can override a stored value."""
    import os
    return (os.environ.get("STRIPE_WEBHOOK_SECRET")
            or _setting("stripe_webhook_secret", "")).strip()


def status():
    """Admin-facing readiness, with no secret material in the return value."""
    off = offer()
    return {
        "configured": bool(off["checkout_url"]),
        "has_webhook_secret": bool(webhook_secret()),
        "sellable": bool(off["checkout_url"]) and bool(webhook_secret()),
        "product": off["name"],
        "price_label": off["price_label"],
        "niche": off["niche"],
        "deliver_pdf": off["deliver_pdf"],
        "orders": order_count(),
        "entitlements": entitlement_count(),
    }


# ------------------------------------------------------------------ verification
def verify_signature(raw, header, secret=None, tolerance=SIG_TOLERANCE_SEC):
    """True when Stripe's `Stripe-Signature` header vouches for `raw`.

    Compares in constant time and honours the tolerance so a captured webhook
    cannot be replayed indefinitely.
    """
    secret = secret if secret is not None else webhook_secret()
    if not secret or not raw or not header:
        return False
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    if isinstance(secret, str):
        secret = secret.encode("utf-8", "replace")
    stamps, sigs = [], []
    for part in str(header).split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if key == "t":
            stamps.append(value)
        elif key == "v1":
            sigs.append(value)
    if not stamps or not sigs:
        return False
    try:
        ts = int(stamps[0])
    except (TypeError, ValueError):
        return False
    if abs(int(time.time()) - ts) > tolerance:
        return False
    expected = hmac.new(secret, ("%d." % ts).encode("ascii") + raw,
                         hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)


def sign_for_test(raw, secret, timestamp=None):
    """Produce a valid signature header. Used by the offline test suite and by
    a local dry-run; never by the request path."""
    ts = int(timestamp if timestamp is not None else time.time())
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    mac = hmac.new(secret.encode("utf-8"),
                   ("%d." % ts).encode("ascii") + raw,
                   hashlib.sha256).hexdigest()
    return "t=%d,v1=%s" % (ts, mac)


# ---------------------------------------------------------------------- granting
def _email_of(event):
    """Pull the buyer email out of a Checkout Session, preferring the field
    Stripe guarantees over a nested customer record."""
    data = event.get("data") or {}
    obj = data.get("object") or {}
    email = ((obj.get("customer_details") or {}).get("email")
             or obj.get("customer_email") or "")
    return str(email or "").strip().lower()


def _product_of(event, fallback):
    data = event.get("data") or {}
    obj = data.get("object") or {}
    meta = obj.get("metadata") or {}
    return str(meta.get("product") or fallback or "").strip()


def entitled(email, product=""):
    """True when this email already owns the product. Empty product means the
    default offer, so callers never have to know its slug."""
    email = (email or "").strip().lower()
    if not email:
        return False
    conn = _db()
    try:
        if product:
            row = conn.execute(
                "SELECT id FROM paid_entitlements WHERE email=? AND product=?",
                (email, product)).fetchone()
            return bool(row)
        row = conn.execute(
            "SELECT id FROM paid_entitlements WHERE email=?", (email,)).fetchone()
        return bool(row)
    except Exception:
        return False
    finally:
        conn.close()


def grant(email, product="", ref="", amount_cents=0, currency="usd"):
    """Record the order and entitlement. Idempotent per (email, product) so a
    retried webhook upgrades nothing and re-sends nothing."""
    email = (email or "").strip().lower()
    if not email:
        return False
    product = (product or "").strip() or "playbook"
    conn = _db()
    try:
        if ref:
            conn.execute(
                "INSERT OR IGNORE INTO paid_orders"
                " (ref,email,product,amount_cents,currency,status,created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (ref, email, product, int(amount_cents or 0),
                 currency or "usd", "paid", time.time()))
        conn.execute(
            "INSERT OR IGNORE INTO paid_entitlements"
            " (email,product,order_ref,granted_at) VALUES (?,?,?,?)",
            (email, product, ref or None, time.time()))
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()


def note_download(email):
    conn = _db()
    try:
        conn.execute(
            "UPDATE paid_entitlements SET downloads=downloads+1, last_download_at=?"
            " WHERE email=?", (time.time(), (email or "").strip().lower()))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def order_count():
    conn = _db()
    try:
        return int(conn.execute("SELECT COUNT(*) c FROM paid_orders").fetchone()[0])
    except Exception:
        return 0
    finally:
        conn.close()


def entitlement_count():
    conn = _db()
    try:
        return int(conn.execute(
            "SELECT COUNT(*) c FROM paid_entitlements").fetchone()[0])
    except Exception:
        return 0
    finally:
        conn.close()


def recent_orders(limit=25):
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT ref,email,product,amount_cents,currency,created_at"
            " FROM paid_orders ORDER BY id DESC LIMIT ?",
            (int(limit),)).fetchall()
    except Exception:
        return []
    finally:
        conn.close()
    return [dict(zip(("ref", "email", "product", "amount_cents", "currency",
                      "created_at"), r)) for r in rows]


# ---------------------------------------------------------------------- delivery
def download_token(email, product=""):
    """Mint a signed, expiring re-download token for an entitled buyer."""
    email = (email or "").strip().lower()
    if not email:
        return ""
    scope = "dl:%s:%s" % (email, product or "playbook")
    return security.make_token(scope, DOWNLOAD_TTL_SEC)


def verify_download_token(token):
    """Return the email a download token belongs to, or '' when it is invalid,
    expired, or minted for a buyer who no longer holds the entitlement."""
    scope = security.verify_token(token or "")
    if not scope or not scope.startswith("dl:"):
        return ""
    parts = scope.split(":")
    if len(parts) < 3:
        return ""
    email, product = parts[1], ":".join(parts[2:])
    if not entitled(email, product):
        return ""
    return email


def build_pdf(niche=""):
    """Render the delivered PDF. Returns (filename, bytes) or None."""
    off = offer()
    keyword = (niche or off.get("niche") or "").strip()
    if not keyword:
        return None
    try:
        import ebook
        book = ebook.build_ebook(keyword)
    except Exception:
        return None
    data = book.get("pdf")
    if not data:
        return None
    return (book.get("pdf_name") or "%s-playbook.pdf" % keyword, data)


def deliver(email, token=""):
    """Email the product: the PDF as an attachment when we can build one, plus a
    signed download link either way. Returns True when the mail was handed to
    the mailer."""
    email = (email or "").strip().lower()
    if not email:
        return False
    off = offer()
    if not token:
        token = download_token(email)
    site = _site_base()
    link = "%s/pro/download?token=%s" % (site, token) if token else ""
    attachments = []
    if off.get("deliver_pdf"):
        built = build_pdf()
        if built:
            name, data = built
            attachments.append({"filename": name, "data": data,
                                "content_type": "application/pdf"})
    if link:
        delivery = ('<p><a class="btn" href="%s">Download your PDF</a></p>'
                    '<p class="hint">The button works from any device and the '
                    'link stays good for 7 days. Reply to this email if it ever '
                    'expires and we will re-send it.</p>' % link)
    else:
        delivery = ("<p>Your download link is being prepared — reply to this "
                    "email if it has not arrived in ten minutes.</p>")
    html = ("<p>Thanks — your %s is ready.</p><p>%s</p>%s"
            "<p>If anything looks wrong, reply to this email and a human will "
            "fix it.</p>" % (seo_name(off), off["blurb"], delivery))
    text = ("Thanks — your %s is ready.\n\n%s\n\n%s\n"
            % (seo_name(off), off["blurb"], link or
               "Reply to this email and we will re-send your download link."))
    try:
        import mailer
        return bool(mailer.send("Your %s" % seo_name(off), text, email,
                                attachments=attachments or None, html=html))
    except Exception:
        return False


def seo_name(off):
    """Product name as it should appear in a subject line and an email body."""
    try:
        import seo
        return seo._clean(off.get("name") or "your order")
    except Exception:
        return off.get("name") or "your order"


def _site_base():
    import os
    base = (os.environ.get("PSTORE_URL")
            or _setting("site_url", "") or "").strip()
    return base.rstrip("/")


# ----------------------------------------------------------------------- webhook
def _already_seen(event_id):
    if not event_id:
        return False
    conn = _db()
    try:
        row = conn.execute("SELECT event_id FROM paid_events WHERE event_id=?",
                           (event_id,)).fetchone()
        return bool(row)
    except Exception:
        return False
    finally:
        conn.close()


def _remember(event_id, etype, email, product):
    conn = _db()
    try:
        conn.execute("INSERT OR IGNORE INTO paid_events"
                     " (event_id,type,email,product,received_at) VALUES (?,?,?,?,?)",
                     (event_id or "", etype or "", email or "", product or "",
                      time.time()))
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()


def handle_event(event):
    """Apply one verified Stripe event. Returns a small result dict so the route
    can log something honest without leaking buyer data."""
    event = event or {}
    etype = str(event.get("type") or "")
    event_id = str(event.get("id") or "")
    result = {"type": etype, "granted": False, "duplicate": False,
              "delivered": False, "email": ""}
    if etype not in ("checkout.session.completed", "charge.succeeded"):
        _remember(event_id, etype, "", "")
        result["ignored"] = True
        return result
    email = _email_of(event)
    result["email"] = email
    if not email:
        _remember(event_id, etype, "", "")
        result["error"] = "no buyer email on the event"
        return result
    obj = (event.get("data") or {}).get("object") or {}
    product = _product_of(event, "playbook")
    amount = obj.get("amount_total")
    currency = obj.get("currency") or "usd"
    ref = str(obj.get("id") or event_id or "")
    if _already_seen(event_id):
        result["duplicate"] = True
        return result
    _remember(event_id, etype, email, product)
    result["granted"] = grant(email, product, ref,
                              int(amount or 0), str(currency))
    if result["granted"]:
        result["delivered"] = deliver(email)
    return result