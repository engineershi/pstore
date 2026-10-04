# -*- coding: utf-8 -*-
"""pstore editorial layer: the human voice and trust machinery that turns a bare
product grid into an answer-first, E-E-A-T-friendly review page.

Everything here is generated from data we actually hold (live listings: price,
rating, review volume, availability) and stays deliberately honest: we never
claim physical testing, never invent product features, and never let a
commission decide placement. Pros/cons are statements about the real data, not
marketing flourish.
"""
import datetime
import hashlib
import html
import math
import os
import re

import amazon

BASE_URL = os.environ.get("PSTORE_URL", "https://trypstore.com").rstrip("/")


def _clean(s):
    return html.escape(str(s or ""), quote=True)


_BEST_LEAD = re.compile(r"^best\b[\s:,-]*", re.I)


def _best_prefixed(keyword):
    """Return `keyword` carrying exactly one leading "Best".

    Stored keywords are nearly all phrased "best <thing>", so a naive
    `"Best %s" % keyword` shipped indexable copy reading
    "Best best camping tent 2 person: ranked picks". The original casing of the
    keyword is preserved so the result reads naturally in a sentence.
    """
    kw = (keyword or "").strip()
    if not kw:
        return kw
    return kw if _BEST_LEAD.match(kw) else "Best %s" % kw


def _title_kw(keyword):
    """Title Case a stored keyword for use in an H1 or headline.

    Mined keywords are stored exactly as Amazon's autosuggest returns them:
    lower case ("best lawn mower battery"). Emitting that verbatim produced H1s
    reading "best lawn mower battery" on every long-tail page — Google rewrites
    them, and the page loses the headline it paid for. Sentence-case every word
    EXCEPT small words that conventionally stay lower inside a title, and never
    touch an acronym the operator typed deliberately (all-caps tokens of 2+).

    Returns the input unchanged when there is nothing to capitalise.
    """
    kw = (keyword or "").strip()
    if not kw:
        return kw
    small = {"a", "an", "and", "as", "at", "but", "by", "for", "from", "in",
             "into", "nor", "of", "on", "onto", "or", "over", "per", "so",
             "the", "to", "up", "via", "vs", "with", "yet"}
    out = []
    words = kw.split()
    last = len(words) - 1
    for i, word in enumerate(words):
        bare = word.strip(".,:;!?'\"()[]")
        if len(bare) > 1 and bare.isupper():
            out.append(word)            # deliberate acronym: DSLR, HDMI, USB-C
            continue
        low = bare.lower()
        if i > 0 and i < last and low in small:
            out.append(low)              # "Best Lawn Mower for Small Yards"
        else:
            out.append(low[:1].upper() + low[1:] if low else word)
    return " ".join(out)


def _bare_kw(keyword):
    """Strip a leading "best" so a template that already says "the best ..."
    doesn't produce "the best best ...".

    Use this — not `_best_prefixed` — in copy like "See the best %s, ranked",
    where the template supplies the word itself.
    """
    kw = (keyword or "").strip()
    return _BEST_LEAD.sub("", kw, count=1).strip() if kw else kw


def _h(keyword, salt=""):
    return int(hashlib.sha1(("%s:%s" % (keyword, salt)).encode("utf-8")).hexdigest(), 16)


def _slug(text):
    s = (text or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "niche"


def _price(item):
    p = item.get("price")
    if not isinstance(p, (int, float)):
        return None
    return "%s%0.2f" % (amazon.currency_symbol(item.get("currency")), p)


def _median(values):
    vals = sorted(v for v in values if isinstance(v, (int, float)) and v > 0)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0


def _as_float(v):
    try:
        fv = float(v)
        return fv if fv == fv else 0.0  # NaN guard
    except (TypeError, ValueError):
        return 0.0


def _review_hum(reviews):
    if isinstance(reviews, (int, float)) and reviews >= 1000:
        return "%sk" % round(reviews / 1000.0)
    return "%s" % (int(reviews) if isinstance(reviews, (int, float)) else reviews)


def _licensed(it):
    """The Amazon review stars/count of `it`, or (None, None) when we may not
    show them. Thin alias over the single chokepoint in `amazon` — see
    `amazon.licensed_rating` for why a scraped rating is never published."""
    return amazon.licensed_rating(it)


def _stars_cell(it):
    """Comparison-table rating cell. Blank when unlicensed rather than an
    em dash, so the column does not imply a rating exists."""
    stars, _ = _licensed(it)
    return ("★ %s" % stars) if isinstance(stars, (int, float)) else ""


def _reviews_cell(it):
    _, reviews = _licensed(it)
    if isinstance(reviews, (int, float)) and reviews >= 0:
        return _review_hum(reviews)
    return ""


_ASINISH = re.compile(r"^B[0-9A-Z]{9}$")


_IMG_HOSTS = (
    "m.media-amazon.com", "images-na.ssl-images-amazon.com",
    "images-eu.ssl-images-amazon.com", "images-fe.ssl-images-amazon.com",
    "images.amazon.com", "m.media-amazon.co.uk", "m.media-amazon.de",
)
_IMG_SAFE = re.compile(r"^https://[a-z0-9.\-]*amazon\.[a-z.]{2,6}/images/I/[A-Za-z0-9_+.\-]+$")


def image_url(item):
    """Amazon CDN product image for an item, or "" when none is available.

    Amazon's conditions only permit hotlinking images obtained through the
    Product Advertising API, so this never synthesises a CDN URL from the ASIN
    the way older affiliate themes did \u2014 that pattern is unsanctioned and
    breaks without notice. The URL must already be stored on the item (populated
    by ``paapi.lookup()`` from ``Images.Primary.Large``).

    The allow-list is deliberate: an image field arriving from a scraped or
    user-supplied record must not become an arbitrary ``src`` (tracking pixel,
    hotlink to a third party, or a javascript: URL on a lax parser)."""
    raw = str((item or {}).get("image") or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    elif raw.startswith("/"):
        return ""                      # never resolve a relative path off-site
    if not raw.lower().startswith("https://"):
        return ""
    if _IMG_SAFE.match(raw):
        return raw
    # Fall back to a host allow-list for the alternate Amazon image CDNs.
    m = re.match(r"^https://([^/]+)(/.*)$", raw)
    if m and m.group(1).lower() in _IMG_HOSTS:
        return raw
    return ""


def product_img(item, keyword=None, cls="shot", sizes="(max-width: 640px) 96px, 112px"):
    """<img> for a pick, or "" when the product has no usable image.

    Emits explicit width/height so the row reserves its space before the CDN
    responds (avoids CLS on the money section) and ``loading="lazy"`` so the
    36 images on a long page do not compete with the first paint."""
    src = image_url(item)
    if not src:
        return ""
    alt = _display(item, keyword)
    return ('<img class="%s" src="%s" alt="%s" width="112" height="112" '
            'loading="lazy" decoding="async" sizes="%s" referrerpolicy="no-referrer">'
            % (_clean(cls), _clean(src), _clean(alt), _clean(sizes)))


def _display(item, keyword=None):
    """Human-facing name for a pick. Never leaks a bare ASIN as a title —
    listings without a real title get a clean, keyword-rich label instead."""
    it = item or {}
    title = str(it.get("title") or "").strip()
    asin = str(it.get("asin") or "").strip()
    if not title or (asin and title.lower() == asin.lower()) or _ASINISH.match(title.upper()):
        if keyword and str(keyword).strip():
            return "Top %s option" % str(keyword).strip()
        return "This pick"
    return title


def _aff(item):
    """Affiliate-tagged URL for an item (see amazon.tagged_url). Falls back to
    the stored URL when it isn't a recognizable Amazon product link."""
    it = item or {}
    url = it.get("url") or ""
    asin = it.get("asin") or ""
    if not url and asin:
        return amazon.affiliate_url(asin)
    return amazon.tagged_url(url) if url else ""


def score_items(items):
    """Rank scoring: rating weight, log-scaled review volume, price closeness
    to the list median. Higher is better."""
    med = _median([it.get("price") for it in items])
    scored = []
    for it in items:
        s = 0.0
        if isinstance(it.get("stars"), (int, float)):
            s += it["stars"] * 2.0
        rev = it.get("reviews") or 0
        if isinstance(rev, (int, float)) and rev > 0:
            s += min(math.log10(rev) / 2.0 * 5.0, 5.0)
        p = it.get("price")
        if isinstance(p, (int, float)) and med:
            s += max(min((med - p) / med, 1.0), -1.0)
        p0 = _as_float(p)
        scored.append((s, it.get("reviews") or 0, -p0, it))
    scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    return [(t[3], t[0]) for t in scored]


def best_pick(items):
    scored = score_items(items or [])
    return scored[0][0] if scored else None


_INTROS = (
    "Shopping for {kw}? You're far from alone — it's one of the most-browsed "
    "{kw} searches on Amazon. We pulled the current live listings, weighed "
    "ratings and review volume against price, and trimmed the field to the "
    "picks most shoppers actually settle on.",
    "Choosing a {kw} shouldn't mean an evening of open tabs. We dug through "
    "the live Amazon results for {kw}, ranked them by rating and review "
    "volume, and kept only the products we'd feel comfortable pointing you to.",
    "There's no shortage of {kw} listings on Amazon — the problem is that most "
    "of them sink in the noise. We did the sifting: these are the {kw} "
    "products that keep coming out on top across price, rating and how many "
    "buyers have already weighed in.",
    "If you've typed \"{kw}\" into an Amazon search box, you've seen how "
    "cluttered the results are. This page is the short version: the strongest "
    "live listings for {kw}, ranked with real signals like star rating, review "
    "count and price — not a paid placement.",
    "Picking the right {kw} comes down to a few honest signals: how well it's "
    "rated, how many people have actually bought and reviewed it, and whether "
    "the price makes sense for what you get. Here's what the live Amazon data "
    "says for {kw}.",
)


def intro(keyword, items):
    tpl = _INTROS[_h(keyword, "intro") % len(_INTROS)]
    return tpl.format(kw=keyword)


def rank_badge(item, idx, items):
    badge, reason = "Best overall", "leads this list on rating, reviews and price together"
    scores = score_items(items)
    top = scores[0][0] if scores else None
    if idx == 1:
        if top and item.get("price") and isinstance(top.get("price"), (int, float)) \
                and item["price"] < top["price"]:
            badge, reason = "Great value", "prices below the top pick while still earning a strong score"
        elif top and item.get("reviews") and top.get("reviews") \
                and item["reviews"] > top["reviews"]:
            badge, reason = "Readers' favorite", "more total reviews than the top pick"
        else:
            badge, reason = "Solid runner-up", "a close second across rating, reviews and price"
    elif idx > 1:
        badge, reason = "Worth a look", "made the shortlist on the same live signals"
    return badge, reason


def quick_take(item, items):
    stars, rev = _licensed(item)
    p = item.get("price")
    pieces = []
    if isinstance(p, (int, float)):
        pieces.append("It sits at %s on this page." % _price(item))
    if isinstance(stars, (int, float)) and isinstance(rev, (int, float)) and rev > 0:
        pieces.append("It averages %s★ across %s Amazon ratings." % (stars, _review_hum(rev)))
    elif isinstance(stars, (int, float)):
        pieces.append("It averages %s★ on Amazon's current data." % stars)
    if not pieces:
        return "One of the more-browsed listings in this niche right now."
    return " ".join(pieces)


def pros_cons(item, items):
    stars, rev = _licensed(item)
    p = item.get("price")
    prices = [it.get("price") for it in items if isinstance(it.get("price"), (int, float))]
    low = min(prices) if prices else None
    med = _median(prices)
    pros, cons = [], []

    if isinstance(rev, (int, float)) and rev >= 500:
        pros.append("Well-reviewed: %s shoppers have already rated it, a strong demand signal." % _review_hum(rev))
    if isinstance(stars, (int, float)) and stars >= 4.2:
        pros.append("Solid average rating of %s★ — consistently higher than the typical listing." % stars)
    if isinstance(p, (int, float)) and isinstance(med, (int, float)) and p < med:
        pros.append("Priced below the mid-point of this list (%s)." % _price(item))
    if isinstance(p, (int, float)) and low is not None and p == low:
        pros.append("The lowest price in this shortlist — good if budget is the deciding factor.")

    if prices and isinstance(p, (int, float)) and p == max(prices):
        cons.append("At the top of the price range here (%s) — fine if the specs justify it, but it's not the budget pick." % _price(item))
    elif isinstance(p, (int, float)) and isinstance(med, (int, float)) and p > med:
        cons.append("Slightly above the list median in price (%s)." % _price(item))
    if isinstance(rev, (int, float)) and rev < 100:
        cons.append("A smaller review base right now, so the verdict is less battle-tested.")
    if isinstance(stars, (int, float)) and stars < 4.0:
        cons.append("Average rating below 4★ — worth skimming the negative reviews before ordering.")

    if not pros:
        pros.append("Made the shortlist on current demand and listing strength for this niche.")
    if not cons:
        cons.append("Price and availability change often on Amazon — confirm today's details before you order.")
    return pros, cons


def comparison_rows(items, start=1, top_asin=None, keyword=None):
    """Rows for the scannable comparison table."""
    rows = []
    scored = score_items(items)
    for idx, (it, _s) in enumerate(scored):
        asin = it.get("asin") or ""
        rows.append({
            "rank": "#%d" % (start + idx),
            "title": _display(it, keyword),
            "asin": asin,
            "price": _price(it) or "—",
            "stars": _stars_cell(it),
            "reviews": _reviews_cell(it),
            "url": _aff(it) or "",
            "top": bool(top_asin) and asin == top_asin,
            "badge": "Top pick" if asin == top_asin else ("Runner-up" if idx == 1 else "Picked"),
        })
    return rows


_FAQS = (
    ("What's the best {kw} right now?",
     "Based on current Amazon data, our top pick is {pick} at {price}. It "
     "leads the list because rating, review volume and price all point the "
     "same way. If your budget is tighter, the runner-up is worth a look too."),
    ("Are the prices on this page accurate?",
     "Prices are pulled live from Amazon but change constantly, so treat them "
     "as indicative. Always confirm the current price and availability on the "
     "Amazon listing before you order."),
    ("Do these links cost me anything?",
     "No. If you buy after clicking one, the price you pay is exactly the same. "
     "As an Amazon Associate we may earn a small commission on qualifying "
     "purchases — it's how the site stays free, and it never decides what's "
     "listed (see our Disclosure)."),
    ("How often is this {kw} list refreshed?",
     "The underlying product data is re-pulled from Amazon on an ongoing "
     "basis, and the ranking is recalculated from the same live signals each "
     "time — so the page reflects current listings rather than a frozen "
     "snapshot."),
)


def faq(keyword, best):
    out = []
    price = _price(best) or "a live Amazon price"
    pick = _display(best, keyword)
    # The FAQ templates already say "the best {kw}", so a keyword that itself
    # starts with "best" produced "What's the best best keto bars right now?".
    kw = _bare_kw(keyword)
    for q, a in _FAQS:
        out.append((q.format(kw=kw), a.format(kw=kw, pick=pick, price=price)))
    return out


def related_niches(current, niches):
    """Up to 6 sibling niche pages, self-excluded, deterministically chosen."""
    pool = [n for n in (niches or [])
            if isinstance(n, dict) and n.get("keyword") and n["keyword"] != current]
    if not pool:
        return []
    return sorted(pool, key=lambda n: _h(n["keyword"], "related"))[:6]


def reading_minutes(keyword, items, best):
    words = len(intro(keyword, items).split()) + 90
    for it in (items or [])[:10]:
        p, c = pros_cons(it, items)
        words += len((" ".join(p) + " " + " ".join(c)).split())
    words += 140
    return max(2, round(words / 190 + 0.4))


def byline_html(keyword, items, best):
    today = datetime.date.today().strftime("%b %d, %Y")
    mins = reading_minutes(keyword, items, best)
    return ('<p class="byline">By the <a href="/about">pstore</a> '
            'editorial team · Updated %s · %d min read</p>' % (today, mins))


def breadcrumbs_html(keyword):
    return ('<nav class="crumbs"><a href="/">Home</a>'
            '<span class="sep">›</span><span>%s</span></nav>' % _clean(keyword))


def methodology_html():
    return ('<div class="trust"><h3>How we pick</h3>'
            '<p>Every list starts with live Amazon data: we pull current '
            'listings for the niche, then score each product on demand, '
            'average rating and review volume, with a nudge for sane pricing. '
            'No placement is for sale, no vendor can buy a slot, and anything '
            'that doesn\'t clear the bar is left off. That\'s the whole '
            'method — see our <a href="/about">About</a> page for more.</p></div>')


def trust_block_html():
    return ('<div class="trust"><h3>Why trust pstore</h3>'
            '<p>We\'re an independent picks site — not an Amazon seller and '
            'not paid to place products. Rankings come from the same data you '
            'can see on this page: rating, review volume and live price. Some '
            'links are Amazon Associates affiliate links (as an Amazon '
            'Associate we earn from qualifying purchases), which may earn us '
            'a commission if you buy — the price you pay never changes. '
            'Questions or corrections? We answer everything at '
            '<a href="/contact">Contact.</a></p></div>')


def urgency_html():
    """Conversion nudge under the verdict: prices are live and change often,
    so the decision window is real."""
    return ('<p class="hint urgency" data-ev="urgency"><b>Prices pulled live from '
            'Amazon.</b> They change often and deals sell out — check the price '
            'on Amazon before you order.</p>')


def sticky_cta_html(keyword, best):
    """Sticky bottom CTA that appears once the visitor scrolls past the picks:
    hands them straight to the #1 pick's Amazon page. Returns '' when there's
    no best pick to send to."""
    url = _aff(best)
    if not url:
        return ""
    title = best.get("title") or (keyword + " top pick")
    asin = best.get("asin") or ""
    price = _price(best)
    lbl = "See it on Amazon"
    if price:
        lbl += " · %s" % price
    return ('<div class="sticky-cta" data-niche="%s" data-source="niche">'
            '<p class="sticky-line">Our #1 pick for <b>%s</b></p>'
            '<div class="sticky-actions">'
            '<a class="cta ghost" href="#courier" data-ev="sticky-guide">Free guide ↓</a>'
            '<a class="cta warm" href="%s" data-beacon="sticky" data-ev="sticky" data-asin="%s">%s →</a>'
            '</div></div>' % (
                _clean(_slug(keyword)), _clean(title)[:44],
                url, _clean(asin), _clean(lbl)))


# Inline style for the niche-page sticky bar. Kept as a plain (non-f-string)
# constant so its CSS braces never collide with an f-string body.
STICKY_CSS = """<style>
.sticky-cta{position:fixed;left:50%;transform:translateX(-50%);bottom:calc(14px + env(safe-area-inset-bottom));z-index:40;display:none;align-items:center;gap:12px;padding:8px 8px 8px 16px;border-radius:18px;background:rgba(17,19,24,.92);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border:1px solid rgba(255,255,255,.10);box-shadow:0 12px 32px rgba(0,0,0,.38);width:max-content;max-width:calc(100% - 24px);}
body.show-sticky .sticky-cta{display:flex;}
.sticky-cta .sticky-line{margin:0;color:#dfe3ea;font-size:13px;line-height:1.25;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:250px;}
.sticky-cta .sticky-line b{color:#fff;}
.sticky-cta .sticky-actions{display:flex;gap:8px;align-items:center;flex:0 0 auto;}
.sticky-cta .cta{margin:0;text-decoration:none;font-weight:700;border-radius:12px;padding:10px 15px;font-size:13.5px;line-height:1;transition:transform .15s ease, box-shadow .15s ease, filter .15s ease;}
.sticky-cta .cta.ghost{background:rgba(255,255,255,.08);color:#cfd6e0;border:1px solid rgba(255,255,255,.12);}
.sticky-cta .cta.ghost:hover{background:rgba(255,255,255,.15);color:#fff;}
.sticky-cta .cta.warm{background:linear-gradient(135deg,#f6a24d,#f2c94c);color:#231a06;box-shadow:0 6px 16px rgba(232,148,10,.35);}
.sticky-cta .cta.warm:hover{transform:translateY(-1px);box-shadow:0 8px 20px rgba(232,148,10,.45);filter:brightness(1.03);}
.sticky-cta .cta:active{transform:translateY(0);}
@media (max-width:560px){
  .sticky-cta{width:auto;bottom:calc(10px + env(safe-area-inset-bottom));padding:7px 7px 7px 12px;border-radius:15px;gap:10px;}
  .sticky-cta .sticky-line{max-width:120px;font-size:12px;}
  .sticky-cta .cta{padding:10px 13px;font-size:13px;}
}
@media (max-width:380px){
  .sticky-cta .sticky-line{display:none;}
  .sticky-cta .cta.ghost{display:none;}
  .sticky-cta{justify-content:center;}
}
.urgency{font-size:14px;}
</style>"""


def pick_html(keyword, item, idx, items):
    badge, why = rank_badge(item, idx, items)
    price = _price(item)
    p, c = pros_cons(item, items)
    pros = "".join("<li>%s</li>" % _clean(x) for x in p)
    cons = "".join("<li>%s</li>" % _clean(x) for x in c)
    stars, reviews = "", ""
    _st, _rv = _licensed(item)
    if _st is not None:
        stars = '<span class="stars">★ %s</span>' % _st
    if isinstance(_rv, (int, float)) and _rv >= 0:
        reviews = '<span class="meta-strong">%s ratings</span>' % _review_hum(_rv)
    cta = ""
    if _aff(item):
        label = "Check price on Amazon" + (" — %s" % price if price else "")
        cta = '<a class="btn" href="%s" data-asin="%s" target="_blank" rel="nofollow sponsored noopener">%s</a>' \
              % (_clean(_aff(item)), _clean(item.get("asin") or ""), label)
    watch = ""
    if _aff(item) and item.get("price") is not None:
        watch = ('<button type="button" class="watch-price" data-watch-asin="%s" '
                 'data-watch-keyword="%s" style="margin-top:10px;display:inline-block;'
                 'border:1px solid #d0d4dc;background:#f7f8fa;color:#23262e;padding:9px 14px;'
                 'border-radius:8px;font-weight:600;cursor:pointer;font-size:12.5px">'
                 '\U0001f514 Track price — email me when it drops</button>'
                 % (_clean(item.get("asin") or ""), _clean(keyword)))
    shot = product_img(item, keyword)
    if shot:
        head_html = ('<div class="pick-head"><img class="shot" src="%s" alt="%s" '
                     'width="112" height="112" loading="lazy" decoding="async" '
                     'referrerpolicy="no-referrer">'
                     '<div class="pick-titles"><span class="rank">#%d</span>'
                     '<h3>%s</h3><span class="badge">%s</span></div></div>'
                     % (_clean(image_url(item)), _clean(_display(item, keyword)),
                        idx + 1, _clean(_display(item, keyword)), badge))
        return ('<div class="pick%s">'
                '%s'
                '<p class="quick">%s</p>'
                '<p class="why">%s</p>'
                '<div class="pilo"><div><h4>Good to know</h4><ul class="pros">%s</ul></div>'
                '<div><h4>Watch out</h4><ul class="cons">%s</ul></div></div>'
                '<p class="starsline">%s %s</p>%s%s</div>'
                % (" top" if idx == 0 else "", head_html,
                   _clean(quick_take(item, items)), _clean(why), pros, cons,
                   stars, reviews, cta, watch))
    return ('<div class="pick%s">'
            '<div class="pick-head"><span class="rank">#%d</span>'
            '<h3>%s</h3><span class="badge">%s</span></div>'
            '<p class="quick">%s</p>'
            '<p class="why">%s</p>'
            '<div class="pilo"><div><h4>Good to know</h4><ul class="pros">%s</ul></div>'
            '<div><h4>Watch out</h4><ul class="cons">%s</ul></div></div>'
            '<p class="starsline">%s %s</p>%s%s</div>'
            % (" top" if idx == 0 else "", idx + 1, _clean(_display(item, keyword)), badge,
               _clean(quick_take(item, items)), _clean(why), pros, cons,
               stars, reviews, cta, watch))


def upsell_block(items, keyword=""):
    """Cross-sell strip: the pick + the two runner-ups as compact 'also
    consider' cards, so a visitor weighing options above the fold has every
    buying path in easy reach — raises the chance of a same-session order and
    total order value."""
    scored = score_items(items or [])
    def _link(it):
        return _aff(it)
    scored = [it for it, _s in scored if it.get("asin") and _link(it)]
    if not scored:
        return ""
    pick = scored[0]
    alt = scored[1:3]
    def card(it, tag):
        price = _price(it)
        label = "Check price on Amazon" + (" — %s" % price if price else "")
        return ('<div class="pick subpick"><span class="badge">%s</span>'
                '<strong>%s</strong>'
                '<a class="btn" href="%s" data-asin="%s" target="_blank" '
                'rel="nofollow sponsored noopener">%s</a></div>'
                % (_clean(tag), _clean(_display(it, keyword)), _clean(_link(it)),
                   _clean(it.get("asin") or ""), label))
    pieces = [card(pick, "Top pick")]
    pieces += [card(it, "Also consider") for it in alt]
    heading = ("Complete the %s shortlist — the pick and the two names that ran it close."
               % _clean(keyword)) if keyword else \
              "The pick and the two that ran it close."
    return ('<div class="card" data-role="upsell"><h2>🛒 And also worth a look</h2>'
            '<p class="hint">%s</p><div class="picks ups">%s</div></div>'
            % (heading, "".join(pieces)))


def comparison_html(items, keyword=None):
    rows = comparison_rows(items, top_asin=(best_pick(items) or {}).get("asin"),
                           keyword=keyword)
    body = "".join(
        "<tr%s><td>%s</td><td class='ct'><a href='%s' data-asin='%s'>%s</a></td><td>%s</td>"
        "<td>%s</td><td>%s</td><td>%s</td></tr>" % (
            " class='top'" if r["top"] else "", r["rank"],
            _clean(r["url"]), _clean(r["asin"]), _clean(r["title"]),
            r["price"], r["stars"], r["reviews"], r["badge"])
        for r in rows)
    return ('<div class="table-wrap"><table class="cgrid"><caption>Compare the '
            'shortlist at a glance</caption><thead><tr><th>Rank</th>'
            '<th>Product</th><th>Price</th><th>Rating</th><th>Reviews</th>'
            '<th>Verdict</th></tr></thead><tbody>%s</tbody></table></div>' % body)


def faq_html(keyword, best):
    qas = faq(keyword, best)
    body = "".join(
        '<details class="faq"><summary>%s</summary><p>%s</p></details>'
        % (_clean(q), _clean(a)) for q, a in qas)
    return "<h2>Questions shoppers ask about %s</h2>%s" % (_clean(keyword), body)


def faq_jsonld(keyword, best):
    qas = faq(keyword, best)
    return {"@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": q,
         "acceptedAnswer": {"@type": "Answer", "text": a}}
        for q, a in qas]}


def breadcrumb_jsonld(keyword, parent=None):
    """BreadcrumbList for a page's position in the site. A topic page passes
    its parent niche as `parent`, yielding Home > Parent > Term so Google's
    breadcrumb rich result matches the visible breadcrumb trail."""
    steps = [{"@type": "ListItem", "position": 1, "name": "Home",
              "item": BASE_URL + "/"}]
    parent_picks = (parent or "").strip()
    if parent_picks:
        steps.append({"@type": "ListItem", "position": 2,
                      "name": "%s picks" % parent_picks,
                      "item": BASE_URL + "/n/" + _slug(parent_picks)})
    steps.append({"@type": "ListItem", "position": len(steps) + 1,
                  "name": "%s picks" % keyword})
    return {"@type": "BreadcrumbList", "itemListElement": steps}


def item_list_jsonld(items, keyword):
    """Ranked-item rich snippet: signals a curated list of top products."""
    ranked = [it for it, _s in score_items(items)]
    elements = []
    for pos, it in enumerate(ranked[:10], start=1):
        if not (it or {}).get("title"):
            continue
        url = it.get("url") or ""
        elements.append({
            "@type": "ListItem", "position": pos,
            "name": it["title"],
            "item": (url if url else ("/n/%s#%s" % (_slug(keyword), (it.get("asin") or pos)))),
        })
    if not elements:
        return None
    return {"@type": "ItemList",
            "name": "%s ranked" % _best_prefixed(keyword),
            "itemListElement": elements}


def related_html(keyword, niches):
    rel = related_niches(keyword, niches)
    if not rel:
        return ""
    links = "".join(
        '<a class="chip" href="%s/n/%s">%s</a>'
        % (BASE_URL.rstrip("/"), _slug(kw), _clean(kw))
        for kw in [n["keyword"] for n in rel])
    return ("<h2>Keep exploring</h2><p>Browse more niches we've hand-picked:</p>"
            "<div class=\"chips\">%s</div>" % links)


def featured_pick(saved_niches):
    """Best single product across all niches, for the home-page hero."""
    best = None
    for n in (saved_niches or []):
        prods = n.get("products") or []
        top = best_pick(prods)
        if not top:
            continue
        score = score_items(prods)[0][1]
        if best is None or score > best[1]:
            best = (top, score, n.get("keyword") or n.get("name") or "")
    return best


def featured_html(saved_niches):
    f = featured_pick(saved_niches)
    if not f:
        return ""
    top, _score, kw = f
    url = _aff(top) or ""
    price = _price(top)
    _st, _rv = _licensed(top)
    stars = ('<span class="stars">★ %s</span>' % _st) if isinstance(_st, (int, float)) else ""
    price_txt = price and (" · " + price) or ""
    cta = ""
    if url:
        label = "Check price on Amazon" + (price and (" — " + price) or "")
        cta = '<a class="btn" href="%s" target="_blank" rel="nofollow sponsored noopener">%s</a>' \
              % (_clean(url), label)
    return ('<section class="card"><h2>Today&#39;s standout pick</h2>'
            '<div class="pick top">'
            '<div class="pick-head"><span class="rank">Best overall</span>'
            '<h3>%s</h3></div>'
            '<p class="quick">%s</p>'
            '<p class="starsline">%s%s · %s</p>'
            '<p class="why">Full ranking: <a href="%s">our %s picks</a>.</p>%s</div></section>'
            % (_clean(_display(top, kw)), _clean(quick_take(top, [top])),
               stars, price_txt, kw,
               "/n/" + _slug(kw), _clean(kw), cta))


def quick_picks_band(saved_niches, count=3):
    """A 'Quick Verdict' band for the home hero: the best-scoring product from
    the best-scoring niches, each with its ranked badge, live price/rating, one
    line of reasoning and a primary CTA. Answer-first — a shopper gets a
    decision-ready pick above the fold without scrolling into a long review.

    Products are de-duplicated by ASIN across the whole band. Near-duplicate
    niche rows are the norm on this site (variant keywords like "knife set" and
    "knife sets 2026" both exist as their own row), and two of them resolving
    to the same top product put the identical item in the #1 and #2 slots at the
    same price with the same rating and near-identical reasoning — under two
    contradictory verdict labels, on the highest-traffic page on the site. Skip
    the repeat and keep walking down the pool so the band always shows `count`
    genuinely different products."""
    pools = []
    seen_asins = set()
    for n in (saved_niches or []):
        prods = n.get("products") or []
        scored = score_items(prods)
        if not scored:
            continue
        kw = n.get("keyword") or n.get("name") or ""
        for item, item_score in scored:
            asin = (item.get("asin") or "").strip().upper()
            # No ASIN on the row: fall through to a title key rather than
            # dropping the niche, but never let two unidentifiable products
            # share a slot either.
            key = asin or ("~name:" + (
                item.get("title") or item.get("name") or "").strip().lower())
            if key in seen_asins:
                continue
            seen_asins.add(key)
            pools.append((item, item_score, kw))
            break
    pools.sort(key=lambda t: t[1], reverse=True)
    if not pools:
        return ""
    cards = []
    for idx, (top, _score, kw) in enumerate(pools[:count]):
        badge, why = rank_badge(top, idx + 1, [top])
        price = _price(top)
        _st, _rv = _licensed(top)
        stars = ('<span class="stars">★ %s</span>' % _st) \
            if isinstance(_st, (int, float)) else ""
        reviews = ('<span class="r-count">%s ratings</span>' % _review_hum(_rv)) \
            if isinstance(_rv, (int, float)) and _rv >= 0 else ""
        cta = ""
        if _aff(top):
            cta = '<a class="btn" href="%s" data-asin="%s" target="_blank" rel="nofollow sponsored noopener">Check price%s</a>' \
                  % (_clean(_aff(top)), _clean(top.get("asin") or ""),
                     ((" — " + price) if price else ""))
        cards.append(
            '<div class="qpick%s">'
            '<div class="pick-head"><span class="rank">%s</span>'
            '<h3>%s</h3><span class="badge">%s</span></div>'
            '<p class="who">Best %s pick</p>'
            '<p class="quick">%s</p>'
            '<p class="why">%s</p>'
            '<p class="starsline">%s %s</p>%s</div>'
            % (" top" if idx == 0 else "", ("#%d" % (idx + 1)), _clean(_display(top, kw)),
               _clean(badge), _clean(kw), _clean(quick_take(top, [top])), _clean(why),
               stars, reviews, cta))
    return '<section class="card qband"><h2>Quick verdict — today’s top picks</h2>' \
           '<p class="hint">Ranked from live Amazon data you can verify before you click.</p>' \
           '<div class="qgrid">%s</div></section>' % "".join(cards)


def home_trust_strip():
    """A transparent, honesty-first strip. Where most review sites hide their
    sourcing, we state it plainly — this is the differentiation we lean on."""
    return ('<section class="card trust-strip">'
            '<div class="trow">'
            '<div class="tcell"><b>Live prices</b><span>Prices pulled from current Amazon listings — not cached guesses.</span></div>'
            '<div class="tcell"><b>Independently ranked</b><span>Ranked by star rating, review volume and price — no paid placement.</span></div>'
            '<div class="tcell"><b>Verdict first</b><span>See the pick and the price before the long read. Justified below.</span></div>'
            '<div class="tcell"><b>Disclosure-first</b><span>Affiliate links, plainly labelled. Buying never changes the price to you.</span></div>'
            '</div></section>')


def niche_grid(saved_niches, limit=36, anchor=""):
    """Explore-niches grid: each tile links to its ranked page and shows how
    many live picks it holds, so the homepage doubles as a topic map."""
    if not saved_niches:
        return ""
    tiles = []
    for n in (saved_niches or [])[:limit]:
        kw = n.get("keyword") or n.get("name") or ""
        count = len(n.get("products") or [])
        slug = _slug(kw)
        tiles.append('<a class="ntile" href="/n/%s"><b>%s</b>'
                     '<span>%d ranked picks · live prices</span></a>'
                     % (_clean(slug), _clean(kw.title()), count))
    sid = ' id="%s"' % _clean(anchor) if anchor else ""
    return '<section class="card"%s><h2>Explore the niches</h2>' \
           '<p class="hint">Every page below is fully crawlable and carries live, affiliate-tagged links.</p>' \
           '<div class="ngrid">%s</div></section>' % (sid, "".join(tiles))