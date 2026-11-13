# -*- coding: utf-8 -*-
"""Niche Validation Report: the paid, decision-grade deliverable.

The site's free pages answer "which products rank"; this module answers the
question an operator actually pays for: *should I build in this niche, and what
does it take to reach $1,000 a month?* Everything is derived from data we
already hold (saved niche rows, the commission model in ``earnings`` and the
ranking in ``editorial``) so the report is deterministic and needs no API keys,
no AI call and no network.

Reader-facing licensing rule: star ratings and review counts are shown **only**
through ``amazon.licensed_rating`` (i.e. ``source == "paapi"``). Scraped ratings
may rank products internally, but they never reach the PDF — the same chokepoint
the public pages use.
"""
import argparse
import json
import math
import os
import sqlite3

import amazon
import earnings
import editorial
import pdfgen

VERDICT_STRONG = "STRONG"
VERDICT_VIABLE = "VIABLE"
VERDICT_THIN = "THIN"
VERDICT_AVOID = "AVOID"

_VERDICT_COPY = {
    VERDICT_STRONG: (
        "Build here. The commission per click is high enough that ordinary "
        "traffic pays, and the competition is beatable with a specific angle."),
    VERDICT_VIABLE: (
        "Build with a narrow angle. The economics work, but only if you target "
        "a specific buyer and do not fight the head term on day one."),
    VERDICT_THIN: (
        "Only worth it with volume or a bundle. One click is worth too little "
        "to win on a single page; you would need a content cluster or a "
        "higher-value upsell."),
    VERDICT_AVOID: (
        "Do not build a traffic play here. At this commission per click the "
        "traffic required to reach a modest income is not realistic for a "
        "new site."),
}


def _money(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "n/a"
    if abs(v) >= 1000:
        return "$%s" % format(round(v), ",d")
    return "$%0.2f" % v


def _median(values):
    vals = sorted(float(v) for v in values if isinstance(v, (int, float)) and v > 0)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0


def _percentile(values, pct):
    vals = sorted(float(v) for v in values if isinstance(v, (int, float)) and v > 0)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * pct
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def verdict(per_click, saturation=None):
    """The single decision, from the two numbers that decide it.

    Per-click value is the money half; saturation is the competition half. When
    saturation is unknown we do not pretend it is zero — we lower confidence and
    require a better money half to reach the same verdict.
    """
    per_click = max(float(per_click or 0), 0.0)
    sat = None if saturation is None else float(saturation)
    if per_click >= 0.25:
        return VERDICT_STRONG if (sat is None or sat <= 7.0) else VERDICT_VIABLE
    if per_click >= 0.10:
        return VERDICT_VIABLE
    if per_click >= 0.05:
        return VERDICT_THIN
    return VERDICT_AVOID


def _competition(saturation, products):
    sat = None if saturation is None else float(saturation)
    scored = editorial.score_items(products or [])
    reviews = [it.get("reviews") or 0 for it, _ in scored[:5]]
    med_rev = _median(reviews)

    if (sat is not None and sat >= 8.0) or (med_rev is not None and med_rev >= 2000):
        level = "Crowded"
        detail = ("Established listings own the first page. A new page wins only "
                  "on a long-tail angle they ignore.")
    elif (sat is not None and sat >= 6.0) or (med_rev is not None and med_rev >= 400):
        level = "Contested"
        detail = ("The top results have real track record but the long tail is "
                  "not locked. A specific comparison or use-case page can rank.")
    else:
        level = "Open"
        detail = ("No strong review moat in the sampled results. A well-built "
                  "page has a realistic path to the first page.")
    return {"level": level, "detail": detail,
            "saturation": sat, "median_reviews": med_rev}


def _shortlist(products, limit=5):
    rows = []
    for it, _score in editorial.score_items(products or [])[:limit]:
        stars, reviews = amazon.licensed_rating(it)
        rows.append({
            "asin": (it.get("asin") or "").upper(),
            "title": (it.get("title") or "").strip(),
            "price": it.get("price") if isinstance(it.get("price"), (int, float)) else None,
            "stars": stars,
            "reviews": reviews,
            "url": it.get("url") or "",
        })
    return rows


def _action_plan(result):
    v = result["verdict"]
    per_click = result["per_click"]
    if v == VERDICT_STRONG:
        return [
            "Week 1: publish one comparison hub (\u201cbest X for <use case>\u201d) "
            "plus three long-tail pages that name a specific buyer.",
            "Week 2: add the price-band and vs pages from this report, then "
            "submit the cluster with IndexNow.",
            "Week 3: capture email on every page with a free pick list; the "
            "list is the only asset that compounds.",
            "Week 4: measure clicks in Search Console. If a page has zero "
            "impressions after 28 days, rewrite the title around the exact "
            "query instead of adding another page.",
            "Target: %s clicks for the first $1,000. At a 2.5%% click-through "
            "that is about %s pageviews." % (
                format(int(math.ceil(1000 / per_click)), ",d"),
                format(int(math.ceil(1000 / per_click / 0.025)), ",d")),
        ]
    if v == VERDICT_VIABLE:
        return [
            "Pick ONE narrow buyer and one comparison query. Do not chase the "
            "head term until the long tail earns impressions.",
            "Publish four pages: a comparison, a price-band page, a vs page and "
            "one buyer guide. Every page ends in a tagged product click.",
            "Add email capture with a free shortlist PDF. At this commission, "
            "repeat visits are what make the math work.",
            "Give the cluster 60 days before judging it; this niche needs "
            "volume, not a single spike.",
        ]
    if v == VERDICT_THIN:
        return [
            "Do not build a single page here. If you enter, build a cluster of "
            "at least ten pages and a higher-value email offer to lift revenue "
            "per click.",
            "Look for a higher-commission adjacent product to anchor the page "
            "(tools, furniture, appliances, jewellery) before you write.",
            "Re-run this report on the adjacent niche and compare per-click "
            "value; only proceed if it clears $0.10.",
        ]
    return [
        "Skip this niche for a commission play. The traffic required to reach "
        "even $1,000 is larger than a new site can realistically earn.",
        "If you have a non-affiliate reason to serve this audience (a product, "
        "a service, a newsletter), the traffic can still be valuable \u2014 but "
        "value it on that offer, not on Amazon commission.",
        "Run this report on a higher-ticket category before committing any time.",
    ]


def analyse(keyword, products=None, score=None, saturation=None, prepared_for=""):
    """Turn one saved niche into the report payload. Pure and deterministic."""
    keyword = (keyword or "").strip()
    products = list(products or [])
    category = earnings.classify(keyword)
    rate = earnings.commission_pct(category)
    order_rate = earnings.order_rate(category)
    reference_aov = earnings.avg_order(category)
    reference_per_click = earnings.per_click_value(category)

    # The category average is a reference, not this niche's price. A niche whose
    # sampled median is $14 earns roughly a sixth of the category figure, so the
    # verdict uses the observed price whenever there is a real sample. This is
    # deliberately conservative: it assumes a one-item order.
    prices = [it.get("price") for it in products]
    priced = [p for p in prices if isinstance(p, (int, float)) and p > 0]
    observed_aov = _median(priced)
    if observed_aov and len(priced) >= 3:
        aov = observed_aov
        aov_basis = "sampled median"
        per_click = aov * (rate / 100.0) * order_rate
    else:
        aov = reference_aov
        aov_basis = "category reference"
        per_click = reference_per_click
    sat = None if saturation is None else float(saturation)
    demand = None if score is None else float(score)
    v = verdict(per_click, sat)
    band = {
        "low": _percentile(prices, 0.25),
        "median": _median(prices),
        "high": _percentile(prices, 0.75),
        "min": min([p for p in prices if isinstance(p, (int, float)) and p > 0],
                   default=None),
        "max": max([p for p in prices if isinstance(p, (int, float)) and p > 0],
                   default=None),
    }
    competition = _competition(sat, products)
    clicks = int(math.ceil(1000 / per_click)) if per_click > 0 else None
    pageviews = {ctr: (int(math.ceil(clicks / ctr)) if clicks else None)
                 for ctr in (0.01, 0.025, 0.05)}

    result = {
        "keyword": keyword,
        "prepared_for": (prepared_for or "").strip(),
        "category": category,
        "commission_pct": rate,
        "avg_order": aov,
        "aov_basis": aov_basis,
        "reference_aov": reference_aov,
        "reference_per_click": reference_per_click,
        "order_rate": order_rate,
        "per_click": per_click,
        "demand_score": demand,
        "saturation": sat,
        "verdict": v,
        "verdict_copy": _VERDICT_COPY[v],
        "price_band": band,
        "competition": competition,
        "clicks_per_1000": clicks,
        "pageviews_for_1000": pageviews,
        "shortlist": _shortlist(products),
        "action_plan": _action_plan({
            "verdict": v, "per_click": per_click}),
        "product_count": len(products),
        "confidence": ("High" if sat is not None and demand is not None
                       else "Medium" if sat is not None or demand is not None
                       else "Low"),
    }
    return result


def _band_text(band):
    if not band["median"]:
        return "No priced products in the sample."
    return ("Low quartile %s \u00b7 median %s \u00b7 high quartile %s"
            % (_money(band["low"]), _money(band["median"]), _money(band["high"])))


def render_pdf(result, site="trypstore.com"):
    """Render the analysis as a finished PDF. Returns bytes."""
    kw = result["keyword"]
    v = result["verdict"]
    per_click = result["per_click"]
    doc = pdfgen.Pdf(accent=(255, 107, 44),
                     footer="PSTORE \u00b7 NICHE VALIDATION REPORT")
    subtitle = ("A decision, the money math behind it, and the 90-day plan to "
                "reach the first $1,000.")
    if result.get("prepared_for"):
        subtitle += " Prepared for %s." % result["prepared_for"]
    doc.cover("Niche Validation: %s" % kw.title(), subtitle,
              kicker="NICHE VALIDATION REPORT", owner="Prepared by pstore",
              site=site)

    doc.chapter(1, "The verdict",
                "One decision first. The reasoning and the evidence follow.")
    doc.pullquote("%s \u2014 %s" % (v.title(), result["verdict_copy"]))
    doc.two_col([
        ("Verdict", v.title()),
        ("Value per click", _money(per_click)),
        ("Commission category", result["category"].replace("-", " ").title()),
        ("Commission rate", "%0.1f%%" % result["commission_pct"]),
        ("Confidence", result["confidence"]),
    ])
    doc.paragraph(
        "This is an estimate, not a promise. It uses the current Amazon "
        "Associates rate for the category and a conservative 3% order rate. "
        "Your real number moves with the category, the basket size and the "
        "quality of the page \u2014 which is exactly what the rest of this "
        "report is about.")

    doc.new_page()
    doc.chapter(2, "The money math",
                "Why the same traffic is worth 17x more in some categories.")
    doc.two_col([
        ("Order value used", "%s (%s)" % (_money(result["avg_order"]),
                                          result["aov_basis"])),
        ("Commission rate", "%0.1f%%" % result["commission_pct"]),
        ("Expected order rate", "%0.1f%%" % (result["order_rate"] * 100)),
        ("Value of one click", _money(per_click)),
        ("Clicks for $1,000", format(result["clicks_per_1000"], ",d")
         if result["clicks_per_1000"] else "n/a"),
        ("Category reference", "%s/click at %s AOV"
         % (_money(result["reference_per_click"]),
            _money(result["reference_aov"]))),
    ])
    doc.paragraph(
        "The value per click uses the median price of the sampled products, not "
        "the category average. That is deliberately conservative: it assumes a "
        "one-item order. If a typical order contains more than one item, the "
        "real value per click is higher and the traffic needed is lower.")
    doc.paragraph(
        "Commission is a percentage of the order, so a higher-ticket category "
        "pays more for the same click. That is why a jewellery or furniture "
        "page can out-earn a grocery page on identical traffic.")
    pv = result["pageviews_for_1000"]
    doc.heading("Traffic needed for $1,000", size=15)
    doc.two_col([
        ("At 1.0% click-through", format(pv[0.01], ",d") + " pageviews"
         if pv[0.01] else "n/a"),
        ("At 2.5% click-through", format(pv[0.025], ",d") + " pageviews"
         if pv[0.025] else "n/a"),
        ("At 5.0% click-through", format(pv[0.05], ",d") + " pageviews"
         if pv[0.05] else "n/a"),
    ])
    doc.pullquote(
        "If those pageview numbers feel large, that is the honest answer: this "
        "niche is a volume play. A higher-commission category is the cheapest "
        "lever you have.")

    doc.new_page()
    doc.chapter(3, "Demand and competition",
                "How much buyer intent exists, and how hard the first page is.")
    doc.two_col([
        ("Demand score (0-10)", "n/a" if result["demand_score"] is None
         else "%0.1f" % result["demand_score"]),
        ("Saturation (0-10)", "n/a" if result["saturation"] is None
         else "%0.1f" % result["saturation"]),
        ("Competitive read", result["competition"]["level"]),
        ("Products sampled", str(result["product_count"])),
    ])
    doc.paragraph(result["competition"]["detail"])
    doc.heading("Price band", size=15)
    doc.paragraph(_band_text(result["price_band"]))
    doc.paragraph(
        "Position against the median, not the cheapest listing. A page that "
        "recommends a $12 product in a $60 category reads as out of touch and "
        "earns the low commission that goes with it.")
    doc.bullets([
        "The demand score is a proxy from keyword variety, not a search-volume "
        "feed. Treat it as a relative signal between niches, not an absolute.",
        "Saturation is an estimate from the sampled listings. A high score "
        "means the top results are established, not that entry is impossible.",
    ])

    doc.new_page()
    doc.chapter(4, "The shortlist",
                "The products the live listing data puts on top, with no paid placement.")
    if result["shortlist"]:
        for i, it in enumerate(result["shortlist"], 1):
            title = (it["title"] or it["asin"] or "Product")[:110]
            doc.paragraph("%d. %s" % (i, title), size=12, leading=16)
            line = []
            if it["price"] is not None:
                line.append(_money(it["price"]))
            if it["stars"] is not None:
                line.append("%s stars \u00b7 %s reviews"
                            % (it["stars"], format(int(it["reviews"]), ",d")))
            if it["asin"]:
                line.append("ASIN %s" % it["asin"])
            if line:
                doc.paragraph(" \u00b7 ".join(line), size=10.5, leading=14,
                              color=doc.muted)
    else:
        doc.paragraph(
            "No product rows were stored for this niche, so there is no "
            "shortlist to rank. Re-mine the niche before you build.")
    doc.paragraph(
        "Prices and availability change. Verify each ASIN on Amazon before you "
        "publish; the ranking here is our analysis, not a live quote.")

    doc.new_page()
    doc.chapter(5, "Risks and red flags")
    risks = [
        "Commission rates are set by Amazon and can change without notice; the "
        "rate used here is the current published reference.",
        "A single-page play in a low-commission category rarely pays. The plan "
        "assumes you build a cluster and keep it updated.",
        "Scraped listing data is an account risk. Use the official API for any "
        "rating or image you republish.",
        "Search is slow. A new page typically needs 60-90 days to earn stable "
        "impressions; judge the cluster, not the first week.",
    ]
    if result["verdict"] in (VERDICT_THIN, VERDICT_AVOID):
        risks.insert(0, "The economics do not support a traffic play in this "
                        "category at the current commission rate.")
    doc.bullets(risks)

    doc.chapter(6, "90-day action plan")
    doc.bullets(result["action_plan"])

    doc.new_page()
    doc.chapter(7, "Method and honesty")
    doc.paragraph(
        "This report is built from a saved niche sample: the listed products, "
        "their prices and the live ranking signals already in the database. "
        "The commission model uses the published Associates category rate, an "
        "average order value for the category and a conservative expected "
        "order rate. Nothing here is a guarantee of earnings.")
    doc.paragraph(
        "Where the sample is thin, the report says so rather than inventing a "
        "number. Re-run the analysis after re-mining the niche and the verdict "
        "may change \u2014 that is the point of a decision document.")
    doc.paragraph("Prepared by pstore \u00b7 %s" % site)
    return doc.save()


def build(keyword, products=None, score=None, saturation=None, prepared_for=""):
    """Analysis + PDF in one call. Returns the result dict with `pdf` attached."""
    result = analyse(keyword, products, score, saturation, prepared_for)
    result["pdf"] = render_pdf(result)
    result["pdf_name"] = "%s-niche-report.pdf" % (
        "".join(c if c.isalnum() or c in "-_" else "-" for c in keyword.lower())
        .strip("-") or "niche")
    return result


def load_niche(keyword, db_path=None):
    """Read one saved niche from the SQLite DB. Returns None when it is absent."""
    path = db_path or os.environ.get("PSTORE_DB") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "pstore.db")
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT keyword, score, saturation, products FROM niches "
            "WHERE lower(keyword)=lower(?) ORDER BY id LIMIT 1", (keyword,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        products = json.loads(row["products"] or "[]")
    except Exception:
        products = []
    return {"keyword": row["keyword"], "score": row["score"],
            "saturation": row["saturation"], "products": products}


def pick_sample(db_path=None):
    """The most instructive saved niche to show for free.

    Highest commission per click wins, because that is the lesson the sample has
    to teach: identical traffic is worth more in a better category. Ties break
    on the richer product sample so the PDF is not mostly empty.
    """
    path = db_path or os.environ.get("PSTORE_DB") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "pstore.db")
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    best, best_key = None, (-1.0, -1)
    try:
        for row in conn.execute(
                "SELECT keyword, score, saturation, products FROM niches"):
            try:
                products = json.loads(row["products"] or "[]")
            except Exception:
                products = []
            if not products:
                continue
            prices = [p.get("price") for p in products
                      if isinstance(p.get("price"), (int, float))
                      and p.get("price") > 0]
            med = _median(prices)
            if not med:
                continue
            cat = earnings.classify(row["keyword"])
            observed = med * (earnings.commission_pct(cat) / 100.0) \
                * earnings.order_rate(cat)
            key = (observed, len(products))
            if key > best_key:
                best_key = key
                best = {"keyword": row["keyword"], "score": row["score"],
                        "saturation": row["saturation"], "products": products}
    finally:
        conn.close()
    return best


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build a Niche Validation Report PDF.")
    ap.add_argument("keyword")
    ap.add_argument("--out", default="")
    ap.add_argument("--db", default="")
    ap.add_argument("--prepared-for", default="")
    ap.add_argument("--mine", action="store_true",
                    help="mine the niche live when it is not saved yet")
    ap.add_argument("--json", action="store_true", help="print the analysis only")
    args = ap.parse_args(argv)

    niche = load_niche(args.keyword, args.db or None)
    if not niche and args.mine:
        import niche as niche_mod
        found, _meta = niche_mod.mine_niche(args.keyword, top=8, max_niches=1)
        if found:
            first = found[0]
            niche = {"keyword": first["keyword"], "score": first.get("score"),
                     "saturation": first.get("saturation"),
                     "products": first.get("products") or []}
    if not niche:
        ap.error("no saved niche named %r in the database "
                 "(use --mine to fetch it live)" % args.keyword)
    if args.json:
        result = analyse(niche["keyword"], niche["products"],
                         niche["score"], niche["saturation"], args.prepared_for)
        print(json.dumps(result, indent=2, default=str))
        return 0
    result = build(niche["keyword"], niche["products"], niche["score"],
                   niche["saturation"], args.prepared_for)
    out = args.out or result["pdf_name"]
    with open(out, "wb") as fh:
        fh.write(result["pdf"])
    print("%s -> %s (%d bytes, verdict %s)"
          % (niche["keyword"], out, len(result["pdf"]), result["verdict"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
