# -*- coding: utf-8 -*-
"""pstore price-drop / deal engine: watch ranked ASINs and flag real price
declines so the site can auto-email "price dropped" and / or surface a deal
banner. This is the urgency + deadcat / Godfather layer (Cialdini scarcity,
Sell Like Crazy 'buy now' triggers, Brunson's urgency hooks).

Design
------
* Pure: fetched prices come from an injectable `fetch_prices(asins) -> {asin: price}`
  hook so tests can feed plain dicts without any network.
* `check()` is the offline-testable core: given current baseline product rows and
  a set of fresh prices, return which ASINs dropped by >= min_drop_pct (and how
  much), so callers can queue an email + raise a banner.
* A tiny disk-backed baseline lets us remember the price each ASIN was first seen
  at, so a drop is measured against *our* baseline, not whatever scrape randomness
  returned today.
"""

import json
import os
import re
import threading

DEFAULT_MIN_DROP_PCT = 5.0   # flag drops of 5%+ (browse-box noise filtered out)
DEFAULT_MIN_DROP_ABS = 2.0   # ...and at least $2 when a real price exists
_DEFAULT_PATH_CANDIDATES = (
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "pricedrops.json"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "pricedrops.json"),
)

_lock = threading.Lock()


# ------------------------------------------------------------------ price parsing
def parse_price(raw):
    """Best-effort parse of a price string -> float or None. Accepts "$12.99",
    "€9,99", "12.99", "12", "from $10". Returns None on nonsense/empty."""
    if raw is None:
        return None
    text = str(raw).strip().replace("\xa0", " ")
    m = re.search(r"([\d][\d,. ]*)$", text)
    if not m:
        return None
    num = m.group(1).replace(" ", "")
    last_dot = num.rfind(".")
    last_comma = num.rfind(",")
    if last_dot != -1 and last_comma != -1:
        # both separators: the *later* one is the decimal (1.234,56 / 1,234.56)
        dec_is_comma = last_comma > last_dot
    elif last_comma != -1:
        # only a comma — treat as decimal if there are 1-2 digits after it,
        # else as a thousands separator (e.g. "1,234")
        after = num[last_comma + 1:]
        dec_is_comma = bool(after) and len(after) <= 2
    else:
        dec_is_comma = False
    if dec_is_comma:
        # comma is the decimal separator -> drop thousands dots, then swap
        num = num.replace(".", "").replace(",", ".")
    else:
        # dot is the decimal separator -> drop thousands commas
        num = num.replace(",", "")
    try:
        value = float(num)
    except ValueError:
        return None
    return round(value, 2) if value > 0 else None


# ------------------------------------------------------------------ config / store
def _default_path():
    for p in _DEFAULT_PATH_CANDIDATES:
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "a").close()
            return p
        except OSError:
            continue
    return _DEFAULT_PATH_CANDIDATES[-1]


class PriceStore:
    """Persists the baseline price per ASIN to a JSON file with a lock so
    concurrent server threads don't stomp each other's writes."""

    def __init__(self, path=None):
        self.path = path or _default_path()
        self._data = {}
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                self._data = raw
        except Exception:
            self._data = {}

    def save(self):
        tmp = self.path + ".tmp"
        self._lock_path()
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh)
            os.replace(tmp, self.path)
        finally:
            self._unlock_path()

    def _lock_path(self):
        # best-effort; file locking is optional in this stdlib-only sandbox
        pass

    def _unlock_path(self):
        pass

    def baseline(self, asin):
        return self._data.get(asin.upper(), {}).get("price")

    def set_baseline(self, asin, price):
        key = asin.upper()
        cur = self._data.setdefault(key, {})
        cur["price"] = price
        self._data[key] = cur
        self.save()

    def all(self):
        return dict(self._data)


# ------------------------------------------------------------------ check (pure)
def compute_drop(old_price, new_price, min_drop_pct=DEFAULT_MIN_DROP_PCT,
                 min_drop_abs=DEFAULT_MIN_DROP_ABS):
    """Given a baseline old price and a fresh new price, return the drop:
    dict {asin-less} -> {old, new, drop, drop_pct, dropped: bool}.
    dropped is True only when the new price is meaningfully below old."""
    if old_price is None or new_price is None:
        return {"old": old_price, "new": new_price, "drop": 0.0,
                "drop_pct": 0.0, "dropped": False}
    drop = round(old_price - new_price, 2)
    pct = round(drop / old_price * 100, 1) if old_price else 0.0
    dropped = drop > 0 and pct >= min_drop_pct and drop >= min_drop_abs
    # a second threshold: any genuine increase is never a "drop"
    return {"old": old_price, "new": new_price, "drop": drop,
            "drop_pct": pct, "dropped": dropped}


def check(rows, fresh_prices, store=None, min_drop_pct=DEFAULT_MIN_DROP_PCT,
          min_drop_abs=DEFAULT_MIN_DROP_ABS):
    """Compare baseline prices for each product row against fresh prices.

    rows: list of dicts each with at least {"asin": "...", "title": "..."} and an
          optional "price" that seeds a brand-new baseline when none exists yet.
    fresh_prices: dict {asin: float} of today's scraped prices.
    store: optional PriceStore used to persist/read baselines. When given, rows
           without an entry get their current price recorded (first-seen baseline).
    Returns {"drops": [ {asin, title, old, new, drop, drop_pct} , ...] (only real
    drops), "tracked": int, "checked": int}.
    """
    drops = []
    tracked = 0
    checked = 0
    for row in rows:
        asin = str(row.get("asin") or "").strip().upper()
        if not asin:
            continue
        checked += 1
        baseline = None
        if store is not None:
            baseline = store.baseline(asin)
        if baseline is None:
            baseline = parse_price(row.get("price"))
            if baseline is not None and store is not None:
                store.set_baseline(asin, baseline)
        tracked += 1
        if baseline is None:
            continue
        if asin not in fresh_prices:
            continue
        fresh = parse_price(fresh_prices[asin])
        if fresh is None:
            continue
        d = compute_drop(baseline, fresh, min_drop_pct, min_drop_abs)
        if d["dropped"]:
            drops.append({
                "asin": asin,
                "title": row.get("title"),
                "old": d["old"],
                "new": d["new"],
                "drop": d["drop"],
                "drop_pct": d["drop_pct"],
            })
    return {"drops": drops, "tracked": tracked, "checked": checked}


# ------------------------------------------------------------------ email copy
def drop_email(drops, base_url="", pick_links=None, email=""):
    """Build a 'price dropped' email body + subject from a check() result.
    `pick_links` maps an ASIN to an affiliate/check-price URL — each drop gets
    its own "Check price on Amazon" CTA (callers collapse them to a tracked
    link at send time). Returns dict {subject, html, text, drops}."""
    if not drops:
        return {"subject": "", "html": "", "text": "", "drops": []}
    subject = "Price dropped on your picks \u2697\ufe0f"
    rows = []
    text_rows = []
    for d in drops:
        link = ((pick_links or {}).get(d.get("asin")) or "") or ""
        title = _h(d.get("title") or d.get("asin"))
        save = _fmt(d.get("drop"))
        pct = d.get("drop_pct") or 0
        if link:
            cta = ('<div style="margin:12px 0 0"><a href="%s" rel="nofollow sponsored noopener" '
                   'style="display:inline-block;background:#f0a41a;background-image:linear-gradient(180deg,#ffd75e,#f0a41a);'
                   'color:#111;text-decoration:none;font-weight:800;font-size:13px;'
                   'padding:9px 22px;border-radius:999px;border:1px solid #e6a700">See it on Amazon \u2192</a></div>'
                   % link)
        else:
            cta = ""
        rows.append(
            '<div style="margin:0 0 14px;background:#ffffff;border:1px solid #ececf1;'
            'border-radius:14px;padding:16px 18px">'
            '<div style="font-size:15px;line-height:1.45;font-weight:700;color:#191b26">%s</div>'
            '<div style="margin:10px 0 0;font-size:14px;color:#5c6b7a">Was '
            '<s style="color:#9aa0ad">%s</s> \u2192 now '
            '<span style="color:#b12704;font-weight:800;font-size:20px">%s</span>'
            ' <span style="display:inline-block;background:#eef7ee;color:#16802a;font-weight:700;'
            'font-size:12px;border-radius:999px;padding:2px 9px;margin-left:6px">save %s / %.1f%% off</span></div>'
            '%s</div>'
            % (title, _fmt(d.get("old")), _fmt(d.get("new")), save, pct, cta))
        text_rows.append(
            "- %s: was %s, now %s (save %s / %.1f%% off)%s"
            % (d.get("title") or d.get("asin"), _fmt(d.get("old")),
               _fmt(d.get("new")), _fmt(d.get("drop")), pct,
               (" \u2014 check price: %s" % link) if link else ""))
    try:
        import mailer
        intro = ('<p style="margin:0 0 4px;font-size:15px;line-height:1.65;color:#3a3f4b">'
                 'Good news \u2014 a product you&rsquo;re watching is on sale:</p>')
        html = mailer._brand_shell("#deal", "A price just dropped",
                                   intro + "".join(rows),
                                   mailer._footer_html(email))
    except Exception:
        html = ("<div style=\"font-family:Helvetica,Arial,sans-serif\"><h2>\u2697\ufe0f "
                "A price just dropped</h2><ul style=\"list-style:none;padding:0\">%s</ul></div>"
                % "".join(rows))
    text = "A price just dropped!\n\n" + "\n".join(text_rows)
    return {"subject": subject, "html": html, "text": text, "drops": drops}


def _fmt(value):
    if value is None:
        return "?"
    return "$%.2f" % value


def _h(s):
    s = str(s or "")
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))
