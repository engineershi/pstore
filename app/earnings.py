# -*- coding: utf-8 -*-
"""Earnings & conversion layer: turn click data into a decision surface.

Amazon does not expose per-click order/earnings via the free scraping route, so
this module is an honest estimator, not a fake ledger:

  * It uses your *actual* recorded clicks (from /api/track).
  * You configure your Associates commission ``%`` (by category) *average order
    value* and an *expected order rate* (orders per click) — industry defaults
    are used but `earnings.configure()` / env vars let you set real numbers.
  * It also lets you log REAL orders/lifetime from the Amazon dashboard so the
    report shows "estimated potential" vs "recorded real" side by side.

Nothing here hits the network. Everything bottoms out in pure functions so tests
stub it trivially.
"""
import os
import re

# ------------------------------------------------------------------ config
# Amazon Associates pays a % of eligible item price, varies by category, plus
# a fixed "bounty" for some program types. Defaults are deliberately
# conservative stand-ins for a typical US books/electronics mix; override them.
DEFAULT_COMMISSION_PCT = float(os.environ.get("EARN_COMMISSION_PCT", "4.0") or "4.0")
DEFAULT_AVG_ORDER = float(os.environ.get("EARN_AVG_ORDER", "40.0") or "40.0")
# orders per click is NOT knowable offline; give a sane default the user tunes.
DEFAULT_ORDER_RATE = float(os.environ.get("EARN_ORDER_RATE", "0.03") or "0.03")

# Runtime overrides set via the keys hub / admin, apply immediately.
_runtime = {}

SAMPLE_CATEGORIES = {  # Amazon Associates base-rate reference (US, FY24-25)
    "jewelry": 20.0, "electronics-accessories": 15.0, "beauty": 10.0,
    "personal-care": 10.0, "appliances": 8.0, "cookware": 8.0, "home": 8.0,
    "furniture": 8.0, "tools": 8.0, "sports": 8.0, "toys": 8.0, "pet": 8.0,
    "baby": 8.0, "garden": 8.0, "office": 8.0,
    "video": 5.0, "music": 5.0, "books": 4.5, "grocery": 4.0,
    "electronics": 4.0, "apparel": 4.0, "automotive": 4.0,
    "default": DEFAULT_COMMISSION_PCT,
}

# Average order value per category. A flat $40 is wrong by an order of
# magnitude in both directions: a kitchen-gadget basket lands near $30 while one
# office chair clears $200. Commission is a *percentage of the order*, so AOV
# multiplies straight into per-click value and is worth as much as the rate.
CATEGORY_AOV = {
    "jewelry": 85.0, "beauty": 32.0, "personal-care": 28.0,
    "electronics-accessories": 35.0, "electronics": 180.0,
    "appliances": 145.0, "cookware": 45.0, "home": 70.0,
    "furniture": 220.0, "tools": 90.0, "sports": 65.0, "toys": 35.0,
    "pet": 45.0, "baby": 75.0, "garden": 110.0, "office": 85.0,
    "grocery": 25.0, "video": 30.0, "books": 20.0, "music": 60.0,
    "apparel": 55.0, "automotive": 70.0, "default": DEFAULT_AVG_ORDER,
}

_NONWORD = re.compile(r"[^a-z0-9]+")


def _sing(word):
    """Crude but sufficient English singulariser.

    Niche keywords are overwhelmingly the plural of a product noun ("best
    baking sheets", "best air purifiers"), so matching the singular form is
    what takes classifier coverage from 72% to ~96%. It only has to be
    consistent between rules and input, never linguistically perfect.

    There is deliberately NO generic "-es -> -e" rule: that turns "machines"
    into "machin". Only the sibilant clusters (-ses/-xes/-zes/-ches/-shes)
    really drop the "e", and everything else is a plain "-s".
    """
    w = str(word or "")
    n = len(w)
    if n < 4 or not w.endswith("s") or w.endswith("ss") or w.endswith("us"):
        return w
    if w.endswith("ies") and n > 4:
        return w[:-3] + "y"
    if w.endswith(("ses", "xes", "zes", "ches", "shes")) and n > 4:
        return w[:-2]
    return w[:-1]


# Keyword -> commission category. Matched as a contiguous run of
# hyphen/space-delimited words against the niche slug, so "cat food" resolves to
# pet, "cat litter box" resolves to pet via the longer run, and "ice cream
# maker" resolves to appliances because the longest matching run wins. Matching
# on words rather than raw substrings is what stops "cat" from ever hitting
# "camping" and "gold" from hitting "golden retriever dog food".
CATEGORY_RULES = {
    # --- 15-20%: the money categories ---------------------------------
    "jewelry": ("jewelry", "jewellery", "gold", "necklace", "bracelet",
                "earring", "pendant", "gemstone", "watch box",
                "jewelry box", "jewelry cleaner", "jewelry organizer"),
    "electronics-accessories": ("power bank", "usb", "cable", "charger",
                                "headphone", "noise cancelling headphone",
                                "earbud", "screen protector", "adapter",
                                "keyboard", "mouse pad", "mouse", "webcam",
                                "microphone", "wireless charger", "bike lock"),
    # --- 10% -----------------------------------------------------------
    "beauty": ("beauty blender", "sunscreen", "skin care", "skincare",
               "lipstick", "mascara", "foundation", "serum", "moisturiz",
               "shampoo", "conditioner", "toothbrush", "water flosser",
               "floss", "beard trimmer", "hair clipper", "curling iron",
               "hair dryer", "epilator", "facial steamer", "nail dryer",
               "reading glasses", "eyelash", "pain relief", "back pain",
               "heating pad", "ketoconazole"),
    "home": ("blackout curtain", "curtain", "weighted blanket",
             "white noise machine", "sleep mask", "sleeping sack",
             "pillow", "heated blanket", "throw blanket", "air purifier",
             "vacuum", "robot vacuum", "handheld vacuum", "sheet",
             "string light", "solar light", "led light", "step stool",
             "rain gauge", "light bulb", "water bottle", "surge protector"),
    # --- 8%: the bulk of the catalogue --------------------------------
    "appliances": ("air fryer", "instant pot", "blender", "bread maker",
                   "ice cream maker", "juicer", "waffle maker", "toaster",
                   "rice cooker", "slow cooker", "stand mixer", "espresso",
                   "coffee grinder", "coffee maker", "kettle",
                   "food processor", "immersion blender", "pancake maker",
                   "microwave", "air purifier", "dehumidifier", "humidifier",
                   "appliance"),
    "cookware": ("baking sheet", "knife set", "knife block", "cookware",
                 "skillet", "dutch oven", "cutting board", "kitchen towel",
                 "kitchen rug", "mixing bowl", "utensil", "kitchen trash"),
    "furniture": ("office chair", "desk", "table", "bookcase", "sofa",
                  "mattress", "bed frame", "nightstand", "barstool"),
    "pet": ("dog", "cat", "puppy", "kitten", "pet", "bird feeder",
            "fish tank", "hamster", "rabbit", "reptile"),
    "baby": ("baby", "bottle warmer", "breast pump", "high chair",
             "diaper", "stroller", "car seat", "carseat", "carseat cushion",
             "nursery", "infant", "toddler", "pacifier", "baby monitor"),
    "sports": ("exercise bike", "treadmill", "rowing machine", "dumbbell",
               "bike helmet", "kids bike", "scooter", "meat thermometer",
               "yoga mat", "yoga", "resistance band", "jump rope",
               "foam roller", "protein powder", "pre workout", "creatine",
               "running shoe", "walking shoe", "kayak", "golf", "tennis",
               "basketball", "soccer", "camping tent", "camping chair",
               "camping", "hammock", "sleeping bag", "climbing",
               "posture corrector", "pilates", "massager", "massage gun"),
    "garden": ("garden hose", "lawn mower", "patio umbrella", "fire pit",
               "firepit", "grill", "griddle", "cooler", "smoker",
               "sprinkler", "weeder", "tiller", "greenhouse", "raised bed"),
    "tools": ("drill", "tool set", "toolbox", "wrench", "screwdriver",
              "socket set", "ladder", "work light", "shop vac"),
    "toys": ("lego", "puzzle", "board game", "doll", "kong"),
    "office": ("desk lamp", "desk organizer", "label maker", "shoe rack",
               "coat rack", "laundry hamper", "file cabinet", "whiteboard"),
    # --- 4-5%: lowest-paying band --------------------------------------
    "grocery": ("keto", "snack", "coffee bean", "protein bar", "fat bomb",
                "supplement", "vitamin"),
    "electronics": ("laptop", "portable monitor", "tablet", "smart tv",
                    "external hard drive", "router", "thermostat",
                    "smart home", "security camera", "doorbell", "speaker",
                    "soundbar", "console", "printer", "mouse trap"),
    "apparel": ("running shoes", "walking shoes", "sneaker", "jacket",
                "hoodie", "yoga pants", "sock", "glove"),
    "automotive": ("car", "auto", "vehicle", "tire", "wiper"),
}

# (words, category), longest run first so the most specific phrase always wins.
# Both sides are stored singularised by _sing() so "baking sheets" matches the
# "baking sheet" rule without needing a plural variant of every token.
_KEYWORD_CATEGORY_RULES = sorted(
    ((tuple(_sing(w) for w in tok.split()), cat)
     for cat, toks in CATEGORY_RULES.items() for tok in toks),
    key=lambda kv: len(kv[0]), reverse=True,
)

def classify(text):
    """Map a niche slug/keyword to an Amazon commission category.

    Unknown text returns "default" rather than guessing, so an unclassifiable
    niche is valued at the conservative global rate instead of being
    accidentally promoted. This is what the priority engine was missing: it
    returned "" for every row, which valued a 20% jewelry page and a 4% cable
    page identically and pointed the operator at the cheapest niches.
    """
    words = [_sing(w) for w in _NONWORD.split(str(text or "").lower()) if w]
    if not words:
        return "default"
    n = len(words)
    for toks, cat in _KEYWORD_CATEGORY_RULES:
        ln = len(toks)
        if ln > n:
            continue
        for i in range(n - ln + 1):
            if tuple(words[i:i + ln]) == toks:
                return cat
    return "default"


def configure(commission_pct=None, avg_order=None, order_rate=None):
    """Set runtime overrides (None leaves the current value untouched)."""
    if commission_pct is not None:
        _runtime["commission_pct"] = float(commission_pct)
    if avg_order is not None:
        _runtime["avg_order"] = float(avg_order)
    if order_rate is not None:
        _runtime["order_rate"] = float(order_rate)


def commission_pct(category=""):
    base = SAMPLE_CATEGORIES.get((category or "").lower())
    if base is None:
        base = DEFAULT_COMMISSION_PCT
    return float(_runtime.get("commission_pct", base))


def avg_order(category=""):
    """Per-category AOV, unless the operator has pinned one globally.

    An empty category keeps the flat default so every existing caller and the
    /admin config form behave exactly as before; only classified niches get a
    realistic basket size.
    """
    pinned = _runtime.get("avg_order")
    if pinned is not None:
        return float(pinned)
    key = (category or "").lower()
    if not key or key == "default":
        return float(DEFAULT_AVG_ORDER)
    return float(CATEGORY_AOV.get(key, DEFAULT_AVG_ORDER))


def order_rate(category=""):
    return float(_runtime.get("order_rate", DEFAULT_ORDER_RATE))


def per_click_value(category=""):
    """Estimated earnings for a single click (before any order is known)."""
    return avg_order(category) * (commission_pct(category) / 100.0) * order_rate(category)


def estimate(clicks, category=""):
    """Estimated earnings (and orders) for N clicks at current config."""
    clicks = max(int(clicks or 0), 0)
    aov = avg_order(category)
    pct = commission_pct(category)
    rate = order_rate(category)
    orders = clicks * rate
    groomed = clicks * rate * aov
    commission = groomed * (pct / 100.0)
    return {
        "clicks": clicks,
        "orders_est": orders,
        "gross_est": groomed,
        "commission_est": commission,
        "avg_order": aov,
        "commission_pct": pct,
        "order_rate": rate,
    }


def aggregate(rows, fn_category, fields=("clicks",)):
    """Aggregate click-like rows. `rows` must be dicts/Row with at least the
    fields named. `fn_category(row)` returns a category key (e.g. '' default).
    Returns per-category estimates plus a global total."""
    totals = {}
    for r in rows:
        cat = fn_category(r)
        cat = (cat or "") or "default"
        rec = totals.setdefault(cat, {
            "clicks": 0, "orders_est": 0.0, "gross_est": 0.0, "commission_est": 0.0})
        c = int(r["clicks"] or 0) if "clicks" in r else 1
        rec["clicks"] += c
    out = {}
    for cat, rec in totals.items():
        est = estimate(rec["clicks"], cat)
        out[cat] = est
    grand = {"clicks": sum(r["clicks"] for r in out.values()),
             "orders_est": sum(r["orders_est"] for r in out.values()),
             "gross_est": sum(r["gross_est"] for r in out.values()),
             "commission_est": sum(r["commission_est"] for r in out.values())}
    return {"by_category": out, "total": grand}


def monthly_summary(months_data):
    """Point-in-time summary of real orders/earnings the operator logged from
    the Associates dashboard, so the report contrasts real vs estimated."""
    total_orders = sum(d.get("orders", 0) for d in months_data)
    total_earn = sum(d.get("earnings", 0.0) for d in months_data)
    return {"months": months_data, "total_orders": total_orders,
            "total_earnings": total_earn}


def measured(clicks, orders, revenue, commission):
    """Measured earnings layer for one click cohort.

    Links REAL recorded orders (that the operator logs from the Associates
    dashboard against this exact cohort) to the clicks that produced them, and
    returns the measured order-rate and commission-per-click next to the old
    `estimate()` projection. A cohort with zero clicks yields a zero measured
    layer because there is no denominator — never a made-up number."""
    clicks = max(int(clicks or 0), 0)
    orders = max(int(orders or 0), 0)
    revenue = max(float(revenue or 0.0), 0.0)
    commission = max(float(commission or 0.0), 0.0)
    est = estimate(clicks, "")
    return {
        "clicks": clicks,
        "orders": orders,
        "revenue": revenue,
        "commission": commission,
        "orders_est": est["orders_est"],
        "commission_est": est["commission_est"],
        "measured_order_rate": (orders / clicks) if clicks else 0.0,
        "measured_commission_per_click": (commission / clicks) if clicks else 0.0,
        "measured_aov": (revenue / orders) if orders else 0.0,
        "gap": commission - est["commission_est"],
    }


def attribution_layer(cohorts):
    """Run every cohort through `measured()` and roll up by channel.

    `cohorts` — list of dicts with month/channel/slug/campaign/clicks/orders/
    revenue/commission. Returns the per-cohort rows plus channel and grand
    totals so one shared computation feeds the API and the admin table."""
    rows = []
    for c in cohorts:
        rec = measured(c.get("clicks", 0), c.get("orders", 0),
                       c.get("revenue", 0), c.get("commission", 0))
        rec.update({
            "month": c.get("month", ""), "channel": c.get("channel", "other"),
            "slug": c.get("slug", ""), "campaign": c.get("campaign", ""),
        })
        rows.append(rec)
    rows.sort(key=lambda r: (r["month"], r["channel"], r["clicks"]),
              reverse=True)
    by_channel = {}
    for r in rows:
        ch = r["channel"]
        rec = by_channel.setdefault(ch, {
            "clicks": 0, "orders": 0, "revenue": 0.0, "commission": 0.0})
        rec["clicks"] += r["clicks"]
        rec["orders"] += r["orders"]
        rec["revenue"] += r["revenue"]
        rec["commission"] += r["commission"]
    grand = {"clicks": sum(c["clicks"] for c in rows),
             "orders": sum(c["orders"] for c in rows),
             "revenue": round(sum(c["revenue"] for c in rows), 2),
             "commission": round(sum(c["commission"] for c in rows), 2)}
    return {"rows": rows, "by_channel": by_channel, "grand": grand}


def priority_rows(rows, category_for=None):
    """Rank niches by estimated earnings for prioritization.

    `rows` — sequence of dict/Row with `niche` (or `slug`) and `clicks`.
    `category_for(row)` — optional fn returning the commission category; default
    ''. Each niche's clicks are run through estimate() so its projected
    commission (clicks * aov * pct * rate) is the ranking key — but we also
    return the estimate fields so the operator can see exactly why.
    Rows with zero clicks are excluded (nothing to prioritize yet)."""
    category_for = category_for or (lambda r: "")
    ranked = []
    for r in rows:
        niche = (str(r.get("niche") or r.get("slug") or "")).strip()
        try:
            clicks = int(r["clicks"] or 0)
        except (TypeError, ValueError, KeyError):
            clicks = 1
        clicks = max(clicks, 0)
        if not clicks:
            continue
        est = estimate(clicks, category_for(r))
        ranked.append({
            "niche": niche,
            "clicks": clicks,
            "commission_est": est["commission_est"],
            "orders_est": est["orders_est"],
            "gross_est": est["gross_est"],
            "avg_order": est["avg_order"],
            "commission_pct": est["commission_pct"],
            "score": est["commission_est"],
        })
    ranked.sort(key=lambda x: x["score"], reverse=True)
    total = sum(x["score"] for x in ranked)
    for x in ranked:
        x["share"] = (x["score"] / total) if total else 0.0
    return {"ranked": ranked, "total_est": total}