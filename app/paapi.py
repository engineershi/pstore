# -*- coding: utf-8 -*-
"""Minimal stdlib PA-API (Product Advertising API) client.

PA-API is AWS's official Amazon Associates product-data API. It gives richer,
quoted fields than the public scraper (exact price, offer count, stock state)
and is the sanctioned, TOS-clean way to read product data.

To stay stdlib-only and testable we hand-roll AWS Signature V4 over ``urllib``
and route the request through ``amazon._urlopen`` so tests can stub it.

Everything is gated: if no access key / secret / partner tag are configured the
module reads nothing, posts nothing and ``lookup()`` returns ``None`` — callers
silently fall back to the existing keyless scraper. Configure via env
(PAAPI_ACCESS_KEY, PAAPI_SECRET_KEY, PAAPI_PARTNER_TAG, PAAPI_HOST) or at
runtime via :func:`configure`. Env always wins.
"""
import datetime
import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

import amazon

PAAPI_HOST = os.environ.get("PAAPI_HOST", "webservices.amazon.com").rstrip("/")
_ENDPOINT_PATH = "/paapi5/getitems"

# Amazon's published PA-API quota starts at 1 request/second and is shared
# across every resource and every client of the account. Firing batches of 10
# ASINs back to back does not get more data faster: it earns an HTTP 429, and
# because `get_items()` swallows every exception the whole batch comes back
# empty. That looks exactly like "the credentials don't work", so the operator
# would throw away a working key. Enforce the floor ourselves and retry 429.
MIN_INTERVAL = float(os.environ.get("PAAPI_MIN_INTERVAL", "1.0"))
MAX_ATTEMPTS = 4
_throttle_lock = threading.Lock()
_last_call = [0.0]


def _pace():
    """Block until at least MIN_INTERVAL has passed since the previous call."""
    with _throttle_lock:
        wait = _last_call[0] + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()


def _retry_after(exc):
    """Amazon sends the throttle window as Retry-After. Honour it when present."""
    try:
        return max(0.0, float((exc.headers or {}).get("Retry-After") or 0))
    except (TypeError, ValueError):
        return 0.0

_PAAPI_CFG = {"access_key": "", "secret_key": "", "partner_tag": ""}
_cfg_lock = threading.Lock()


def _cfg():
    with _cfg_lock:
        stored = dict(_PAAPI_CFG)
    return {
        "access_key": os.environ.get("PAAPI_ACCESS_KEY") or stored["access_key"],
        "secret_key": os.environ.get("PAAPI_SECRET_KEY") or stored["secret_key"],
        "partner_tag": os.environ.get("PAAPI_PARTNER_TAG") or stored["partner_tag"],
    }


def configure(access_key="", secret_key="", partner_tag=""):
    """Set PA-API credentials at runtime (admin UI seam). Returns the store so
    callers can persist it. Empty strings clear the values."""
    with _cfg_lock:
        _PAAPI_CFG["access_key"] = (access_key or "").strip()
        _PAAPI_CFG["secret_key"] = (secret_key or "").strip()
        _PAAPI_CFG["partner_tag"] = (partner_tag or "").strip()
    return dict(_PAAPI_CFG)


def _configure_clear():
    """Test helper: reset all stored creds (env overrides still apply)."""
    configure("", "", "")


def status():
    """Masked key presence + summarized readiness (env overrides stored)."""
    c = _cfg()
    return {
        "has_access_key": bool(c["access_key"]),
        "has_secret_key": bool(c["secret_key"]),
        "has_partner_tag": bool(c["partner_tag"]),
        "ready": bool(c["access_key"] and c["secret_key"] and c["partner_tag"]),
        "host": PAAPI_HOST,
    }


def ready():
    return status()["ready"]


def _sign(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sigv4(host, access_key, secret_key, body):
    """Return (canonical_uri, signature) for the GetItems POST using SigV4."""
    now = datetime.datetime.utcnow()
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    region = "us-east-1"
    service = "ProductAdvertisingAPI"
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_uri = "/paapi5/getitems"
    canonical_headers = "content-encoding:amz-1.0\nhost:%s\nx-amz-date:%s\n" % (
        host, amz_date)
    signed_headers = "content-encoding;host;x-amz-date"
    canonical_request = "\n".join([
        "POST", canonical_uri, "", canonical_headers, signed_headers,
        payload_hash,
    ])
    scope = "/".join([date_stamp, region, service, "aws4_request"])
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    k_date = _sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    k_signing = _sign(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    return canonical_uri, amz_date, signature, signed_headers


def get_items(asin_list):
    """Call PA-API GetItems for a list of ASINs. Returns the raw JSON dict, or
    ``None`` if the API is not configured or the call fails. Never raises."""
    c = _cfg()
    if not (c["access_key"] and c["secret_key"] and c["partner_tag"]):
        return None
    asins = [str(a).strip().upper() for a in asin_list if a]
    if not asins:
        return None
    body = json.dumps({
        "PartnerType": "Associates",
        "PartnerTag": c["partner_tag"],
        # Honor PSTORE_MARKET the way the rest of the app does. Hardcoding
        # www.amazon.com means a store set up for another TLD signs valid
        # requests against a marketplace its partner tag cannot serve.
        "Marketplace": "www.amazon.%s" % (amazon.MARKET or amazon.DEFAULT_MARKET),
        "ItemIds": asins[:10],
        "Resources": [
            "Images.Primary.Large",
            "ItemInfo.Title",
            # Ratings are requested and parsed for the same licensing reason as
            # the image: a scraped star rating is Program Content we may not
            # republish, so `amazon.licensed_rating()` can only ever display a
            # number that arrived through this API. Without these two resources
            # the ONE licensed rating source in the codebase is never fetched,
            # and every ranking page renders stars as blank.
            "ItemInfo.ByLineInfo",
            "ItemInfo.CustomerReviews",
            "OfferSummary.LowestPrice",
            "Offers.Listings.Price",
            "ParentASIN",
            "ItemInfo.ExternalIds",
        ],
    }).encode("utf-8")
    last_err = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            _pace()
            canonical_uri, amz_date, signature, signed_headers = _sigv4(
                PAAPI_HOST, c["access_key"], c["secret_key"], body)
            url = "https://%s%s" % (PAAPI_HOST, canonical_uri)
            req = urllib.request.Request(
                url, data=body,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Content-Encoding": "amz-1.0",
                    "X-Amz-Date": amz_date,
                    "X-Amz-Target": "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.GetItems",
                    "Authorization": (
                        "AWS4-HMAC-SHA256 Credential=%s/%s/a/us-east-1/"
                        "ProductAdvertisingAPI/aws4_request, "
                        "SignedHeaders=%s, Signature=%s"
                    ) % (c["access_key"], amz_date[:8], signed_headers, signature),
                },
                method="POST")
            raw = amazon._urlopen(req, timeout=10)
            if raw is None:
                return None
            data = raw.read()
            return json.loads(data.decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            # 429 is the quota, 500/503/504 are Amazon-side transients. Both are
            # retryable and neither means the credentials are wrong -- 401/403 do.
            last_err = e
            if e.code not in (429, 500, 503, 504) or attempt >= MAX_ATTEMPTS - 1:
                return None
            wait = _retry_after(e) or min(8.0, 0.5 * (2 ** attempt))
            if attempt or e.code == 429:
                time.sleep(wait)
        except Exception:
            return None
    return None


def normalize_item(item):
    """Turn one raw GetItems entry into the same dict shape the scraper uses,
    so callers can treat either source transparently. Pure — no network."""
    if not item:
        return None
    asin = str(item.get("ASIN") or "").strip().upper()
    if not asin:
        return None
    info = item.get("ItemInfo", {}) or {}
    title = ((info.get("Title", {}) or {}).get("DisplayValue")) or ""
    price_obj = ((item.get("Offers", {}) or {}).get("Listings", []) or [])
    price = ""
    currency = ""
    if price_obj:
        p = price_obj[0].get("Price", {}) or {}
        price = p.get("Amount") or ""
        currency = (p.get("Currency") or "")[:3]
    # "Images.Primary.Large" is requested above but was never read back, so the
    # single TOS-clean product-image source was fetched and thrown away. Money
    # pages rendered zero <img> as a result. Amazon CDN images are the only
    # images an Associate site may hotlink, and only via this resource.
    image = ""
    img_obj = ((item.get("Images", {}) or {}).get("Primary", {}) or {}).get("Large", {}) or {}
    image = (img_obj.get("URL") or "").strip()
    # Ratings. PA-API returns them in two different shapes depending on the
    # marketplace and on whether the item has editorial bylines, so read both
    # and keep the first usable value. Left as None when absent: a missing
    # rating must stay None rather than becoming 0, because "0 stars" would be
    # published to readers as if Amazon had said so.
    stars, reviews = "", ""
    cr = info.get("CustomerReviews", {}) or {}
    raw_stars = cr.get("AverageStarRating")
    if raw_stars is not None:
        # PA-API returns "4.5 out of 5 stars" on some hosts and a bare "4.5" on
        # others; take the leading number either way.
        # Store one decimal so the stored value equals what seo.py displays
        # (`round(float(stars), 1)`); a stored 4.25 rendering as 4.2 would make
        # the JSON-LD ratingValue disagree with the visible stars.
        try:
            stars = "%.1f" % float(str(raw_stars).split()[0])
        except (ValueError, IndexError):
            stars = ""
    raw_count = cr.get("Count")
    if raw_count is not None:
        try:
            reviews = int(raw_count)
        except (ValueError, TypeError):
            reviews = ""
    if not stars or not reviews:
        for c in ((info.get("ByLineInfo", {}) or {}).get("Contributors", []) or []):
            blob = "%s %s" % ((c or {}).get("Role") or "", (c or {}).get("Name") or "")
            m = re.search(r"([\d.]+)\s*out of\s*5\s*stars", blob, re.I)
            if m and not stars:
                try:
                    stars = "%.1f" % float(m.group(1))
                except ValueError:
                    pass
            m = re.search(r"([\d,]+)\s*ratings?", blob, re.I)
            if m and not reviews:
                try:
                    reviews = int(m.group(1).replace(",", ""))
                except ValueError:
                    pass
    return {
        "asin": asin,
        "title": title,
        "price": price,
        "stars": stars or None,
        "reviews": reviews or None,
        "url": amazon.affiliate_url(asin),
        "currency": currency,
        "image": image,
        "source": "paapi",
    }


def items_by_asin(asin_list):
    """Fetch and normalize a list of ASINs in GetItems-sized batches.
    Returns {ASIN: normalized_dict}. Empty when PA-API is unconfigured or every
    call fails. Never raises."""
    asins = [str(a).strip().upper() for a in (asin_list or []) if a]
    out = {}
    for k in range(0, len(asins), 10):
        data = get_items(asins[k:k + 10])
        if not data:
            continue
        for raw in (data.get("ItemsResult", {}) or {}).get("Items", []) or []:
            norm = normalize_item(raw)
            if norm:
                out[norm["asin"]] = norm
    return out


def lookup(asin):
    """Fetch one ASIN and normalize it to the same dict shape the scraper uses
    so callers can use either source transparently. Returns ``None`` on any
    missing/disabled/failed path."""
    data = get_items([asin])
    if not data:
        return None
    item = None
    for it in (data.get("ItemsResult", {}) or {}).get("Items", []) or []:
        if (it or {}).get("ASIN") == asin:
            item = it
            break
    if not item:
        return None
    return normalize_item(item)