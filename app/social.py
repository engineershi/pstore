# -*- coding: utf-8 -*-
"""pstore social: platform-ready, trackable post kits for one-click publishing.

Every kit turns the niche's top pick into short-form copy that sends readers to
the niche LANDING page with UTM parameters (utm_source=<platform>,
utm_campaign=<slug>, utm_content=<post-code>). The landing page carries the
courier beacon, so every visit and every Amazon click is attributed to the exact
post — no cloaked shortlinks, and traffic + conversions are counted per platform
and per post in /admin/social and /admin/analytics.

Publishing is kept honest and pluggable:
  * default     — /admin/social generates drafts; one click "publishes" them
                  (status flip) and returns copy-ready posts to paste anywhere.
  * webhook     — set SOCIAL_WEBHOOK and a POST of {"body","link","platform"}
                  fires for each published post, so Zapier/Make/browser tools
                  (or a future native API) can post it for real.
"""
import math
import re
import secrets
import urllib.parse

import market_engine

PLATFORMS = ["Twitter / X", "Facebook", "LinkedIn", "Instagram", "Pinterest", "Threads"]

# 1-marketing-safe short codes (no 0/1/o/l/i) for per-post attribution.
_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"


def short_code():
    n = int.from_bytes(secrets.token_bytes(3), "big")
    out = ""
    while n:
        n, r = divmod(n, len(_ALPHABET))
        out = _ALPHABET[r] + out
    return out or "a"


def _key(name):
    return {"Twitter / X": "twitter", "Threads": "threads"}.get(name) or \
        (name or "social").lower().replace(" ", "-")


def track_link(base_url, slug, platform, content):
    """Public, tracked link back to the niche landing page (UTM-tagged)."""
    base = (base_url or "").rstrip("/")
    q = urllib.parse.urlencode({
        "utm_source": _key(platform), "utm_medium": "social",
        "utm_campaign": slug, "utm_content": content})
    return "%s/lp/%s?%s" % (base, slug, q)


def og_image_url(base_url, slug):
    """Absolute URL to the auto-generated 1200x630 share card for a niche
    (og:image / Pinterest / Instagram-adjacent). Lives at /og/<slug>."""
    base = (base_url or "").rstrip("/")
    return "%s/og/%s" % (base, slug)


def og_image_png_url(base_url, slug):
    """Absolute URL to the raster 1200x630 share card (Pinterest/Twitter/FB
    friendly). Lives at /og/<slug>.png — the SVG card plus a real PNG."""
    base = (base_url or "").rstrip("/")
    return "%s/og/%s.png" % (base, slug)


def _clip(s, n=80):
    s = str(s or "")
    return s[:n - 1].rstrip() + "…" if len(s) > n else s


def _price(it, s=None):
    try:
        return market_engine._price(it) or s
    except Exception:
        return s


def _proof(it):
    stars = it.get("stars")
    reviews = it.get("reviews")
    if stars:
        rv = f" · {reviews:,} reviews" if isinstance(reviews, (int, float)) else ""
        return f"{stars:.1f}★{rv}"
    if isinstance(reviews, (int, float)):
        return f"{reviews:,} reviews"
    return "top-rated"


def _hashwords(keyword):
    words = re.findall(r"[a-z0-9]+", (keyword or "").lower())
    return "".join(w.capitalize() for w in words) or "AmazonPicks"


def hashtags(keyword):
    tag = _hashwords(keyword)
    return " ".join(["#" + tag, "#BestPicks", "#AmazonFinds", "#BuyersGuide",
                     "#Ranked", "#TopPicks", "#HonestReviews"])


# ------------------------------------------------------------------ composers

def _twitter(keyword, title, proof, price, link, slug):
    head = f"🏆 The best {keyword} is now ranked."
    body = (f"{head} {_clip(title, 60)} comes in #1 on our list — {proof}."
            f"\n\nFull ranked list + live prices: {link}")
    return {"platform": "Twitter / X", "name": "X post (top pick)",
            "body": body[:275], "link": link,
            "hashtags": hashtags(keyword)}


def _facebook(keyword, title, proof, price, link, slug):
    body = (f"Tired of guessing which {keyword} to buy? We ranked them from "
            f"live Amazon data (rating + review volume + price).\n\n"
            f"🥇 Pick: {_clip(title, 90)} — {proof}"
            f"{' · ' + price if price else ''}\n\n"
            f"See the full ranked list and why it won: {link}")
    return {"platform": "Facebook", "name": "Facebook post (hook + list)",
            "body": body, "link": link, "hashtags": hashtags(keyword)}


def _linkedin(keyword, title, proof, price, link, slug):
    body = (f"We just published our review of the best {keyword}, ranked by "
            f"real buyer signals — star rating, review volume and price — not "
            f"opinions.\n\n"
            f"Top pick: {_clip(title, 90)}\n{proof}"
            f"{' · ' + price if price else ''}\n\n"
            f"The ranked guide (updated from live listings): {link}")
    return {"platform": "LinkedIn", "name": "LinkedIn post (professional)",
            "body": body, "link": link, "hashtags": hashtags(keyword)}


def _instagram(keyword, title, proof, price, link, slug):
    body = (f"Which {keyword} actually earn their hype? 📊\n\n"
            f"We scored live Amazon listings on rating, review volume and "
            f"price. One clear winner:\n\n"
            f"🏆 {_clip(title, 70)} — {proof}"
            f"{' (' + price + ')' if price else ''}\n\n"
            f"Full ranked list in our bio link ⬅️ {link}")
    return {"platform": "Instagram", "name": "Instagram caption (visual)",
            "body": body, "link": link, "hashtags": hashtags(keyword)}


def _pinterest(keyword, title, proof, price, link, slug):
    body = (f"Best {keyword} ranked — see which one buyers keep choosing, why, "
            f"and what it costs. Pin for your next {keyword} decision.\n\n"
            f"Top pick: {_clip(title, 90)} ({proof}). Prices are live on "
            f"Amazon: {link}")
    return {"platform": "Pinterest", "name": "Pinterest pin (keyword-rich)",
            "body": body, "link": link, "hashtags": hashtags(keyword)}


def _threads(keyword, title, proof, price, link, slug):
    body = (f"Unpopular opinion: most “best {keyword}” lists are guessing.\n\n"
            f"We ranked them from live Amazon data. Top pick: {_clip(title, 55)} "
            f"— {proof}. Full list: {link}")
    return {"platform": "Threads", "name": "Threads post (hot take)",
            "body": body[:495], "link": link, "hashtags": hashtags(keyword)}


_COMPOSERS = {
    "Twitter / X": _twitter, "Facebook": _facebook, "LinkedIn": _linkedin,
    "Instagram": _instagram, "Pinterest": _pinterest, "Threads": _threads,
}


def post_kits(keyword, items, base_url, slug=None):
    """Return one ready-to-post kit (dict) per platform, UTM-tagged. Empty when
    the niche has no top pick."""
    pick = market_engine.pick_for_buyers(items)
    if not (pick or {}).get("asin"):
        return []
    slug = slug or (re.sub(r"[^a-z0-9]+", "-", (keyword or "").lower()).strip("-") or "niche")
    title = pick.get("title") or ""
    proof = _proof(pick)
    price = _price(pick)
    kits = []
    for platform in PLATFORMS:
        content = short_code()
        link = track_link(base_url, slug, platform, content)
        kit = _COMPOSERS[platform](keyword, title, proof, price, link, slug)
        kit["slug"] = slug
        kit["keyword"] = keyword
        kit["utm_content"] = content
        kit["proof"] = proof
        kit["target"] = "landing"
        kit["image"] = og_image_url(base_url, slug)
        kit["image_png"] = og_image_png_url(base_url, slug)
        kits.append(kit)
    return kits


def topic_post_kits(term, parent_keyword, items, base_url, parent_slug=None, slug=None):
    """Long-tail topic recycling: same one pick, but the copy names the specific
    long-tail angle (/n/<parent>/<term>) so every indexed topic page earns social
    traffic too. Uses its own tracked link with content tagged for the topic."""
    pick = market_engine.pick_for_buyers(items)
    if not (pick or {}).get("asin"):
        return []
    parent_slug = parent_slug or (re.sub(r"[^a-z0-9]+", "-", (parent_keyword or "").lower())
                                  .strip("-") or "niche")
    term_slug = slug or (re.sub(r"[^a-z0-9]+", "-", (term or "").lower())
                         .strip("-") or parent_slug)
    title = pick.get("title") or ""
    proof = _proof(pick)
    price = _price(pick)
    label = _hashtag(term) or term or term_slug
    kits = []
    for platform in PLATFORMS:
        content = short_code()
        # point to the long-tail page with the topic in the UTM campaign
        q = urllib.parse.urlencode({
            "utm_source": _key(platform), "utm_medium": "social",
            "utm_campaign": term_slug + "-t-" + _hashtag(term) if term else term_slug,
            "utm_content": content})
        link = "%s/n/%s/%s?%s" % ((base_url or "").rstrip("/"), parent_slug, term_slug, q) \
            if term else track_link(base_url, parent_slug, platform, content)
        kit = _COMPOSERS[platform]("%s %s" % (parent_keyword, term), title, proof,
                                   price, link, term_slug)
        kit["slug"] = term_slug
        kit["keyword"] = parent_keyword
        kit["utm_content"] = content
        kit["proof"] = proof
        kit["target"] = "topic"
        kit["term"] = term
        kit["image"] = og_image_url(base_url, parent_slug)
        kit["image_png"] = og_image_png_url(base_url, parent_slug)
        kits.append(kit)
    return kits


def _hashtag(s):
    words = re.findall(r"[a-z0-9]+", (s or "").lower())
    return (words[0] if words else "") or (words or [""])[0]


# ------------------------------------------------------------------ og preview
def og_svg(slug, keyword, title, stars, reviews):
    """Small share-preview card (SVG, stdlib-only) used as og:image/twitter:image."""
    kw = html_esc(keyword) or "Niche pick"
    t = html_esc(title or "Best picks, ranked fresh")
    pr = ("%.1f" % float(stars)) if stars else "Top rated"
    if stars and isinstance(reviews, (int, float)):
        pr += " · %d reviews" % int(reviews)
    tr = t if len(t) <= 34 else t[:33] + "…"
    kwl = kw if len(kw) <= 26 else kw[:25] + "…"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">
 <defs>
  <linearGradient id="bg" x1="0" y1="0" x2="0.45" y2="1">
   <stop offset="0" stop-color="#0D1A33"/><stop offset="1" stop-color="#24395E"/>
  </linearGradient>
  <radialGradient id="w" cx="0.18" cy="0.22" r="0.95">
   <stop offset="0" stop-color="#FFB060" stop-opacity="0.32"/>
   <stop offset="1" stop-color="#FFB060" stop-opacity="0"/>
  </radialGradient>
  <radialGradient id="t" cx="0.84" cy="0.88" r="0.95">
   <stop offset="0" stop-color="#56CCFF" stop-opacity="0.25"/>
   <stop offset="1" stop-color="#56CCFF" stop-opacity="0"/>
  </radialGradient>
 </defs>
 <rect width="1200" height="630" fill="url(#bg)"/>
 <rect width="1200" height="630" fill="url(#w)"/>
 <rect width="1200" height="630" fill="url(#t)"/>
 <circle cx="86" cy="76" r="6" fill="#FFBA6A"/>
 <text x="116" y="84" font-family="Helvetica,Arial,sans-serif" font-size="32" font-weight="700" fill="rgba(203,221,252,0.95)" letter-spacing="2">PSTORE</text>
 <text x="96" y="156" font-family="Helvetica,Arial,sans-serif" font-size="44" font-weight="800" fill="#FFBA6A" letter-spacing="1">{kwl}</text>
 <text x="96" y="280" font-family="Helvetica,Arial,sans-serif" font-size="72" font-weight="800" fill="#F7FAFF">{tr}</text>
 <g fill="#FFBA6A"><text x="96" y="420" font-size="64">★★★</text></g>
 <text x="330" y="416" font-family="Helvetica,Arial,sans-serif" font-size="40" font-weight="700" fill="#DAE5FA">{html_esc(pr)}</text>
 <line x1="100" y1="470" x2="1100" y2="470" stroke="rgba(255,255,255,.18)" stroke-width="6" stroke-dasharray="2 22" stroke-linecap="round"/>
 <rect x="96" y="498" width="610" height="80" rx="26" fill="#FFBB74"/>
 <rect x="104" y="506" width="594" height="64" rx="22" fill="#FFBA6A"/>
 <text x="148" y="556" font-family="Helvetica,Arial,sans-serif" font-size="44" font-weight="900" fill="#182540">FULL LIST + PRICES</text>
 <path d="M640 538 h34 m-14 -13 l14 13 l-14 13" stroke="#182540" stroke-width="7" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
 <circle cx="822" cy="544" r="4" fill="#50D99F"/>
 <text x="846" y="556" font-family="Helvetica,Arial,sans-serif" font-size="28" font-weight="600" fill="rgba(174,194,228,0.95)" letter-spacing="1">RANKED FRESH</text>
</svg>""".encode("utf-8")


def html_esc(s):
    import html
    return html.escape(str(s or ""), quote=True)


# ----------------------------------------------------------------------------
# Raster share card (real PNG, stdlib-only). Pinterest/Twitter/FB reject the
# SVG card above, so /og/<slug>.png re-draws the same layout as a genuine
# 1200x630 PNG using a tiny built-in 5x7 font + hand-rolled encoder. No PIL,
# no fonts, no third-party deps — a few hundred lines of pure zlib/struct.
# ----------------------------------------------------------------------------

_CANVAS = (1200, 630)


def _png_encode(width, height, rgb_rows):
    """Encode truecolor 8-bit RGB rows into PNG bytes (stdlib zlib/struct)."""
    import struct, zlib
    sig = b"\x89PNG\r\n\x1a\n"
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    scanlines = b"".join(b"\x00" + bytes(rgb) for rgb in rgb_rows)
    return (sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(scanlines, 2))
            + chunk(b"IEND", b""))


# 5x7 pixel font (7 rows of 5 columns, "#" = on). Lowercase maps to uppercase,
# non-ASCII falls back to a safe substitute or space.
_FONT = {
    "A": ("#####", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "B": ("####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."),
    "C": (".####", "#...#", "#....", "#....", "#....", "#...#", ".####"),
    "D": ("####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."),
    "E": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "F": ("#####", "#....", "#....", "####.", "#....", "#....", "#...."),
    "G": (".####", "#...#", "#....", "#.###", "#...#", "#...#", ".####"),
    "H": ("#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "I": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "#####"),
    "J": ("..###", "...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."),
    "K": ("#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"),
    "L": ("#....", "#....", "#....", "#....", "#....", "#....", "#####"),
    "M": ("#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"),
    "N": ("#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#", "#...#"),
    "O": (".####", "#...#", "#...#", "#...#", "#...#", "#...#", ".####"),
    "P": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
    "Q": (".####", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"),
    "R": ("####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"),
    "S": (".####", "#....", "#....", ".####", "....#", "....#", ".####"),
    "T": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "U": ("#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".####"),
    "V": ("#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."),
    "W": ("#...#", "#...#", "#...#", "#.#.#", "#.#.#", "##.##", "#...#"),
    "X": ("#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"),
    "Y": ("#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."),
    "Z": ("#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"),
    "0": (".####", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".####"),
    "1": ("..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "2": (".####", "#...#", "....#", "..##.", ".#...", "#....", "#####"),
    "3": (".####", "#...#", "....#", "..##.", "....#", "#...#", ".####"),
    "4": ("...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."),
    "5": ("#####", "#....", "####.", "....#", "....#", "#...#", ".####"),
    "6": (".####", "#....", "#....", "####.", "#...#", "#...#", ".####"),
    "7": ("#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."),
    "8": (".####", "#...#", "#...#", ".####", "#...#", "#...#", ".####"),
    "9": (".####", "#...#", "#...#", ".####", "....#", "....#", ".####"),
    " ": (".....", ".....", ".....", ".....", ".....", ".....", "....."),
    ".": (".....", ".....", ".....", ".....", ".....", ".....", "..#.."),
    "-": (".....", ".....", ".....", "#####", ".....", ".....", "....."),
    "_": (".....", ".....", ".....", ".....", ".....", ".....", "#####"),
    "#": ("#.#..", "#.#..", "#####", "#.#..", "#####", "#.#..", "#.#.."),
    "!": ("..#..", "..#..", "..#..", "..#..", "..#..", ".....", "..#.."),
    "?": (".###.", "#...#", "....#", "..##.", "..#..", ".....", "..#.."),
    "&": (".##..", "#..#.", "#.#..", ".##..", "#.#.#", "#..#.", ".##.#"),
    "%": ("##..#", "##.#.", "...#.", "..#..", ".#...", ".#.##", "#..##"),
    "/": ("....#", "....#", "...#.", "..#..", ".#...", "#....", "#...."),
    ":": (".....", "..#..", ".....", "..#..", ".....", "..#..", "....."),
    "'": ("..#..", "..#..", "..#..", ".....", ".....", ".....", "....."),
    "(": ("...#.", "..#..", ".#...", "#....", "#....", ".#...", "..#.."),
    ")": (".#...", "..#..", "...#.", "....#", "....#", "...#.", "..#.."),
    "+": (".....", "..#..", "..#..", "#####", "..#..", "..#..", "....."),
    ",": (".....", ".....", ".....", ".....", "..#..", "..#..", ".#..."),
    ">": ("#....", ".#...", "..#..", "...#.", "..#..", ".#...", "#...."),
    "<": ("....#", "...#.", "..#..", ".#...", "..#..", "...#.", "....#"),
    "=": (".....", ".....", "#####", ".....", "#####", ".....", "....."),
    "*": (".....", "#.#.#", ".#.#.", "#####", ".#.#.", "#.#.#", "....."),
    '"': (".#.#.", ".#.#.", ".#.#.", ".....", ".....", ".....", "....."),
}

_SUBST = {"\u00b7": ".", "\u2026": "...", "\u2192": ">", "\u2605": "*",
          "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2014": "-",
          "\u2013": "-", "\u00a9": "C", "\u00ae": "R", "\u20ac": "E",
          "\ufffc": " "}  # non-breaking / object replacements stripped below


def _raster_text(s, x, y, scale, gap=0):
    """Pixel positions (set of (x, y)) for the uppercase text `s` drawn at
    origin (x, y) with the built-in 5x7 font scaled `scale`x. Non-ASCII maps
    through _SUBST, then onto the font (uppercase fallback), else a space.
    `gap` adds extra pixels of letter-spacing for a calmer, premium look."""

    def _rows(ch):
        if ch in _SUBST:
            ch = _SUBST[ch]
        if ch in _FONT:
            return _FONT[ch]
        u = ch.upper()
        if u in _FONT:
            return _FONT[u]
        if ch == "\n":
            return None
        return _FONT[" "]

    advance = 6 * scale + 1 + gap
    pts = set()
    xx, yy = x, y
    for raw in s:
        rows = _rows(raw)
        if rows is None:
            yy += 8 * scale
            xx = x
            continue
        for r_i, row in enumerate(rows):
            for c_i, cell in enumerate(row):
                if cell != "#":
                    continue
                for dx in range(scale):
                    for dy in range(scale):
                        pts.add((xx + c_i * scale + dx, yy + r_i * scale + dy))
        xx += advance
    return pts


# ----------------------------------------------------------------------------
# Card design system — calm, smooth, trustworthy: a deep navy canvas with two
# soft light glows (amber + teal), gold accents and a single clear gold call
# to action. Every niche card shares one pre-rendered background canvas, so
# warm-up of hundreds of cards is a memcpy + label pass, not a full re-draw.
# ----------------------------------------------------------------------------

_GRAD_CARD = ((13, 26, 48), (36, 58, 102))  # calm navy, top -> bottom
_GOLD = (255, 186, 106)
_WHITE = (247, 250, 255)
_GOLD_SOFT = (255, 168, 74)
_INK = (24, 37, 64)                          # navy text on the gold button
_CREAM = (203, 221, 252)
_MUTED = (174, 194, 228)
_BODY = (218, 229, 250)
_LINE = (90, 116, 162)
_GREEN = (80, 217, 159)


def _blend(img, W, H, x, y, rgb, a=1.0):
    i = (y * W + x) * 3
    img[i] = int(img[i] + (rgb[0] - img[i]) * a)
    img[i + 1] = int(img[i + 1] + (rgb[1] - img[i + 1]) * a)
    img[i + 2] = int(img[i + 2] + (rgb[2] - img[i + 2]) * a)


def _round_run(x0, y0, x1, y1, r, y):
    """Inclusive (xa, xb) x-span of the rounded rect at row y."""
    cy = y0 + r
    if y > y1 - r:
        cy = y1 - r
    dy = y - cy
    d = r * r - dy * dy
    dx = int(math.sqrt(d)) if d > 0 else 0
    xa = max(x0, x0 + r - dx)
    xb = min(x1, x1 - r + dx)
    return (xa, xb)


def _fill_round_rect(img, W, H, x0, y0, x1, y1, r, rgb, a=1.0):
    """Rounded rect painted as horizontal runs (fast bytearray slices)."""
    if a >= 0.999:
        for y in range(y0, y1 + 1):
            xa, xb = _round_run(x0, y0, x1, y1, r, y)
            if xa > xb:
                continue
            o = y * W * 3 + xa * 3
            img[o:o + (xb - xa + 1) * 3] = bytes(rgb) * (xb - xa + 1)
        return
    for y in range(y0, y1 + 1):
        xa, xb = _round_run(x0, y0, x1, y1, r, y)
        o = y * W * 3 + xa * 3
        for _ in range(xa, xb + 1):
            img[o] = int(img[o] + (rgb[0] - img[o]) * a)
            img[o + 1] = int(img[o + 1] + (rgb[1] - img[o + 1]) * a)
            img[o + 2] = int(img[o + 2] + (rgb[2] - img[o + 2]) * a)
            o += 3


def _ring_round_rect(img, W, H, x0, y0, x1, y1, r, rgb, a=1.0, w=2):
    ir = max(0, r - w)
    for y in range(y0, y1 + 1):
        oxa, oxb = _round_run(x0, y0, x1, y1, r, y)
        ixa, ixb = _round_run(x0 + w, y0 + w, x1 - w, y1 - w, ir, y)
        for x in range(oxa, ixa):
            o = y * W * 3 + x * 3
            img[o] = int(img[o] + (rgb[0] - img[o]) * a)
            img[o + 1] = int(img[o + 1] + (rgb[1] - img[o + 1]) * a)
            img[o + 2] = int(img[o + 2] + (rgb[2] - img[o + 2]) * a)
        for x in range(ixb + 1, oxb + 1):
            o = y * W * 3 + x * 3
            img[o] = int(img[o] + (rgb[0] - img[o]) * a)
            img[o + 1] = int(img[o + 1] + (rgb[1] - img[o + 1]) * a)
            img[o + 2] = int(img[o + 2] + (rgb[2] - img[o + 2]) * a)


def _glow(img, W, H, cx, cy, R, rgb, amp, step=2):
    """Soft radial light. Painted every `step`-th pixel (fine for a blur)."""
    xmin, xmax = max(0, cx - R), min(W - 1, cx + R)
    ymin, ymax = max(0, cy - R), min(H - 1, cy + R)
    R2 = R * R
    for py in range(ymin, ymax + 1, step):
        dy2 = (py - cy) ** 2
        for px in range(xmin, xmax + 1, step):
            d2 = (px - cx) ** 2 + dy2
            if d2 >= R2:
                continue
            t = 1.0 - (d2 ** 0.5) / R
            _blend(img, W, H, px, py, rgb, amp * t * t)


def _dot(img, W, H, cx, cy, rad, rgb, a=1.0):
    r2 = rad * rad
    for dy in range(-rad, rad + 1):
        for dx in range(-rad, rad + 1):
            if dx * dx + dy * dy <= r2:
                x, y = cx + dx, cy + dy
                if 0 <= x < W and 0 <= y < H:
                    _blend(img, W, H, x, y, rgb, a)


def _stroke(img, W, H, x0, y0, x1, y1, rgb, width, a=1.0):
    steps = max(abs(x1 - x0), abs(y1 - y0), 1)
    for i in range(steps + 1):
        px = round(x0 + (x1 - x0) * i / steps)
        py = round(y0 + (y1 - y0) * i / steps)
        for dy in range(-width, width + 1):
            for dx in range(-width, width + 1):
                x, y = px + dx, py + dy
                if 0 <= x < W and 0 <= y < H:
                    _blend(img, W, H, x, y, rgb, a)


def _star_pts(cx, cy, R):
    pts = []
    for i in range(10):
        ang = -90.0 + i * 36.0
        rad = R if i % 2 == 0 else R * 0.45
        pts.append((cx + rad * math.cos(math.radians(ang)),
                    cy + rad * math.sin(math.radians(ang))))
    return pts


def _in_poly(px, py, poly):
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > py) != (yj > py) and \
           px < (xj - xi) * (py - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _star(img, W, H, cx, cy, R, rgb, a=1.0):
    poly = _star_pts(cx, cy, R)
    x0, y0 = int(cx - R), int(cy - R)
    x1, y1 = int(cx + R) + 1, int(cy + R) + 1
    for py in range(y0, y1):
        for px in range(x0, x1):
            if _in_poly(px + 0.5, py + 0.5, poly):
                _blend(img, W, H, px, py, rgb, a)


def _vignette(img, W, H, strength=0.13):
    w2, h2 = W / 2.0, H / 2.0
    maxd = (w2 * w2 + h2 * h2) ** 0.5
    cut = 0.55
    for py in range(H):
        for px in range(W):
            d = (((px - w2) ** 2 + (py - h2) ** 2) ** 0.5) / maxd
            if d <= cut:
                continue
            s = 1.0 - strength * min(1.0, (d - cut) / (1.0 - cut))
            i = (py * W + px) * 3
            img[i] = int(img[i] * s)
            img[i + 1] = int(img[i + 1] * s)
            img[i + 2] = int(img[i + 2] * s)


_BG_CANVAS = None  # shared, immutable background (same for every card)


def _base_canvas():
    """The calm shared backdrop: navy gradient + two soft glows + vignette,
    plus the static brand chrome (pill, dotted divider, gold CTA button with
    label + arrow, reassurance tag) that is identical on every card. Rendered
    once and reused by every niche card (one bytearray copy per card), so the
    whole pinned fleet looks pixel-identical and warms up fast."""
    global _BG_CANVAS
    if _BG_CANVAS is None:
        W, H = _CANVAS
        t0, t1 = _GRAD_CARD
        img = bytearray(W * H * 3)
        for y in range(H):
            t = y / (H - 1)
            img[y * W * 3:(y + 1) * W * 3] = bytes((
                int(t0[0] + (t1[0] - t0[0]) * t),
                int(t0[1] + (t1[1] - t0[1]) * t),
                int(t0[2] + (t1[2] - t0[2]) * t))) * W
        _glow(img, W, H, 250, 150, 520, (255, 176, 96), 0.15)
        _glow(img, W, H, 1010, 540, 470, (86, 204, 255), 0.12)
        _vignette(img, W, H)

        def _stamp(pts, rgb, a=1.0):
            for (px, py) in pts:
                if 0 <= px < W and 0 <= py < H:
                    _blend(img, W, H, px, py, rgb, a)

        # brand row — quiet, top-left
        _dot(img, W, H, 86, 76, 6, _GOLD)
        _stamp(_raster_text("PSTORE", 116, 58, 3, gap=2), _CREAM)

        # fresh pill — busiest signal sits small, top-right
        pill = "UPDATED DAILY"
        pw = _text_width(pill, 3, gap=1) + 40
        px0, py0, px1, py1 = 1104 - pw, 56, 1104, 96
        _fill_round_rect(img, W, H, px0, py0, px1, py1, 20, (255, 255, 255), 0.06)
        _ring_round_rect(img, W, H, px0, py0, px1, py1, 20, (255, 255, 255), 0.20, 2)
        _dot(img, W, H, px0 + 18, 76, 4, _GREEN)
        _stamp(_raster_text(pill, px0 + 36, 64, 3, gap=1), _MUTED)

        # calm dotted divider
        for x in range(100, 1106, 26):
            _dot(img, W, H, x, 472, 3, (255, 255, 255), 0.14)

        # single gold call to action — painted once, identical everywhere
        bx0, by0, bx1, by1 = 96, 496, 706, 576
        _fill_round_rect(img, W, H, bx0 + 3, by0 + 5, bx1 + 3, by1 + 5, 26, (8, 15, 30), 0.35)
        _fill_round_rect(img, W, H, bx0, by0, bx1, by1, 26, _GOLD_SOFT, 1.0)
        _fill_round_rect(img, W, H, bx0 + 8, by0 + 8, bx1 - 8, by1 - 8, 22, _GOLD, 1.0)
        _stamp(_raster_text("FULL LIST + PRICES", 148, 532, 4, gap=1), _INK)
        _stroke(img, W, H, 640, 536, 674, 536, _INK, 6)
        _stroke(img, W, H, 660, 522, 674, 536, _INK, 6)
        _stroke(img, W, H, 660, 550, 674, 536, _INK, 6)

        # soft reassurance, bottom-right
        _dot(img, W, H, 822, 542, 4, _GREEN)
        _stamp(_raster_text("RANKED FRESH", 846, 528, 3, gap=1), _MUTED)

        # two faint plus marks frame the composition without noise
        for (px, py) in ((66, 258), (1134, 316)):
            _stamp(_raster_text("+", px, py, 2, gap=0), (255, 255, 255), 0.16)

        _BG_CANVAS = bytes(img)
    return _BG_CANVAS


def _text_width(s, scale, gap=0):
    return len(s) * (6 * scale + 1 + gap)


def _wrap(s, limit):
    lines = []
    for w in str(s or "").split(" "):
        if not lines:
            lines.append(w)
        elif len(lines[-1]) + 1 + len(w) <= limit:
            lines[-1] += " " + w
        else:
            lines.append(w)
    return (lines[:2] or [""]) if lines else [""]


def og_png(slug, keyword, title, stars, reviews):
    """Raster 1200x630 share card — the PNG that Pinterest and the OG crawlers
    see. Calm navy chrome from the shared canvas plus per-niche content: gold
    keyword label, one headline, and gold-star trust proof. Pure stdlib."""
    W, H = _CANVAS
    img = bytearray(_base_canvas())

    def _stamp(pts, rgb, a=1.0):
        for (px, py) in pts:
            if 0 <= px < W and 0 <= py < H:
                _blend(img, W, H, px, py, rgb, a)

    # keyword — gold label, the niche the card is about
    kw = (keyword or slug or "niche").replace("-", " ").upper()
    kw_t = kw if len(kw) <= 30 else kw[:29] + "..."
    _stamp(_raster_text(kw_t, 96, 122, 4, gap=2), _GOLD)

    # headline — cool white, calm line spacing, at most two soft-wrapped lines
    lines = _wrap(title or "Best picks, ranked", 32)
    for i, ln in enumerate(lines[:2]):
        ln = ln.upper()
        if len(ln) > 32:
            ln = ln[:31] + "..."
        _stamp(_raster_text(ln, 96, 208 + i * 50, 5, gap=1), _WHITE)

    # trust row — gold stars + proof number (or a check for top-rated)
    y_star = 392
    if stars:
        try:
            _s = float(stars)
        except (TypeError, ValueError):
            _s = 0.0
        for i in range(3):
            _star(img, W, H, 108 + i * 52, y_star, 17, _GOLD)
        pr = "%.1f" % _s
        if isinstance(reviews, (int, float)) and reviews:
            pr += " · %d REVIEWS" % int(reviews)
        _stamp(_raster_text(pr.upper(), 300, 372, 4, gap=1), _BODY)
    else:
        _stroke(img, W, H, 102, 384, 118, 400, _GOLD, 7)
        _stroke(img, W, H, 118, 400, 148, 368, _GOLD, 7)
        _stamp(_raster_text("TOP RATED PICKS", 176, 372, 4, gap=1), _BODY)

    rows = (bytes(img[i:i + W * 3]) for i in range(0, len(img), W * 3))
    return _png_encode(W, H, rows)


def favicon_png(size=64):
    """Small brand icon (default 64x64) for /og/favicon.png — calm navy
    background with a gold "P": the same story as the share cards but tiny.
    Pure stdlib; cached by the HTTP layer."""
    W = H = size
    t0, t1 = _GRAD_CARD
    img = bytearray(W * H * 3)
    for y in range(H):
        t = y / max(H - 1, 1)
        r = int(t0[0] + (t1[0] - t0[0]) * t)
        g = int(t0[1] + (t1[1] - t0[1]) * t)
        b = int(t0[2] + (t1[2] - t0[2]) * t)
        img[y * W * 3:(y + 1) * W * 3] = bytes((r, g, b)) * W
    scale = max(2, size // 16)  # 5x7 font at this scale -> centered letter
    pts = _raster_text("P", (W - 5 * scale) // 2, (H - 7 * scale) // 2, scale)
    for (px, py) in pts:
        if 0 <= px < W and 0 <= py < H:
            i = (py * W + px) * 3
            img[i] = _GOLD[0]; img[i + 1] = _GOLD[1]; img[i + 2] = _GOLD[2]
    rows = (bytes(img[i:i + W * 3]) for i in range(0, len(img), W * 3))
    return _png_encode(W, H, rows)