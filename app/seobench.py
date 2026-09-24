# -*- coding: utf-8 -*-
"""seobench: head-to-head SEO engine benchmark against a live competitor.

Compares the SAME page types on two sites (guide / landing / story pages of a
matched keyword) and scores both across the signals Search Console raises:
crawl & indexability, structured-data richness, lab performance, content
depth, affiliate hygiene, and SERP-feature eligibility. Output is a scored
matrix with a prioritized gap list of concrete fixes.

Targeted at the programmatic-affiliate category (ProductFind, best-X engines)
— NOT manual-tested editorial (Wirecutter/Forbes), which competes on brand +
backlinks, not on engine signals.

Stdlib only; every network call bottoms out in module-level `_urlopen` so
tests can inject fake responses (mirrors amazon._urlopen).

Engine use:
    python3 seobench.py https://trypstore.com/n/keto https://productfind.com/best-keto
    python3 seobench.py --json ...
"""
import json
import re
import time
import urllib.parse
import urllib.request

NET_TIMEOUT = 25
_MAX = 8  # pages crawled per site, max
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/125.0 Safari/537.36")

# Rich-result eligibility rules (Google's own requirements for the types we use).
REVIEW_SNIPPET_PRICE = ("Product needs a single valid Price (offers.price > 0 with "
                        "a priceCurrency) for Google review-snippet eligibility.")
REVIEW_SNIPPET_RATING = ("AggregateRating needs ratingValue on a 1-5 scale AND a "
                         "reviewCount of real reviews — aggregated/'best-of' lists "
                         "widely share the same source. Use product-level counts.")
FAQ_REQUIRED = ("FAQPage only eligible when the visible page actually shows the "
                "question+answer text.")
ITEMLIST_REQUIRED = ("ItemList must reference a Product with a real price, else "
                     "Google treats it as a plain link list (no rich result).")

DIMENSIONS = [
    "crawl", "schema", "content", "performance", "affiliate", "features",
]
DIM_LABEL = {
    "crawl": "Indexability",
    "schema": "Structured data",
    "content": "Content depth",
    "performance": "Lab performance",
    "affiliate": "Affiliate hygiene",
    "features": "SERP features",
}


def _urlopen(req, timeout):
    return urllib.request.urlopen(req, timeout=timeout)


def _fetch(url, timeout=NET_TIMEOUT):
    """GET a URL, returning (status, headers, body)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA, "Accept": "text/html,*/*",
        "Accept-Encoding": "gzip",
    })
    t0 = time.monotonic()
    try:
        resp = _urlopen(req, timeout)
        raw = resp.read()
        headers = {k.lower(): v for k, v in resp.headers.items()}
        status = resp.status
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), b""
    except Exception:
        return 0, {}, b""
    elapsed = round((time.monotonic() - t0) * 1000)
    if headers.get("content-encoding") == "gzip":
        import gzip  # stdlib
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    import io
    _noop = io.BytesIO  # keep import surface obvious; noop
    return status, headers, raw


def _text_of(html):
    """Strip tags/scripts/styles -> visible words."""
    s = re.sub(r"<(script|style|svg)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&[a-z]+;", " ", s)
    return re.sub(r"\s+", " ", s)


def extract_jsonld(html):
    """Return decoded application/ld+json blocks (list of dicts)."""
    out = []
    for m in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                        html, flags=re.S | re.I):
        try:
            d = json.loads(m.strip())
        except Exception:
            continue
        if isinstance(d, list):
            out.extend(d)
        else:
            out.append(d)
    return out


def _graph_nodes(blocks):
    nodes = []
    for d in blocks:
        if isinstance(d, dict):
            g = d.get("@graph")
            if isinstance(g, list):
                nodes.extend(g)
            else:
                nodes.append(d)
    return nodes


def _count_type(nodes, t):
    c = 0
    for n in nodes:
        ty = n.get("@type") or ""
        types = ty if isinstance(ty, list) else [ty]
        if t in types:
            c += 1
        # nested under ItemList.itemListElement[].item
        if n.get("@type") == "ItemList":
            for entry in n.get("itemListElement") or []:
                it = (entry or {}).get("item") or entry
                if isinstance(it, dict) and (it.get("@type") == t):
                    c += 1
    return c


def _product_quality(blocks):
    """Return dict describing how well Product schema is populated."""
    nodes = _graph_nodes(blocks)
    products = [n for n in nodes if _has_type(n, "Product")
                or _has_type(n, "ListItem")]
    # flatten ItemList inner products
    flat = []
    for n in nodes:
        if _has_type(n, "ItemList"):
            for e in n.get("itemListElement") or []:
                it = (entry_item(e))
                if it:
                    flat.append(it)
        if _has_type(n, "Product"):
            flat.append(n)
    priced = 0
    rated = 0
    for p in flat:
        offers = p.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price = offers.get("price") if isinstance(offers, dict) else None
        if price not in (None, "") and str(price).replace(".", "").isdigit() and float(price) > 0:
            priced += 1
        ar = p.get("aggregateRating") or {}
        if (ar.get("ratingValue") and (ar.get("reviewCount") or ar.get("ratingCount"))):
            rated += 1
    return {"products": len(flat), "priced": priced, "rated": rated}


def _has_type(node, t):
    ty = node.get("@type") or ""
    return t == ty or (isinstance(ty, list) and t in ty)


def entry_item(entry):
    it = (entry or {}).get("item")
    return it if isinstance(it, dict) else None


def _head_meta(html, name):
    m = re.search(r'<meta[^>]+name=["\']%s["\'][^>]*content=["\'](.*?)["\']' % name,
                  html, flags=re.I | re.S)
    if not m:
        m = re.search(r'<meta[^>]+content=["\'](.*?)["\'][^>]*name=["\']%s["\']' % name,
                      html, flags=re.I | re.S)
    return (m.group(1)[:220].replace("&#039;", "'") if m else "")


def _canonical(html):
    m = re.search(r'<link[^>]*rel=["\']canonical["\'][^>]*href=["\'](.*?)["\']',
                  html, flags=re.I)
    if not m:
        m = re.search(r"<link[^>]*rel=['\"]canonical['\"][^>]*href=['\"](.*?)['\"]",
                      html, flags=re.I)
    return (m.group(1) if m else "")


def analyze_page(keyword, url, page_kind, expected):
    """Fetch one page and distill the signals the engine comparison cares about."""
    status, headers, body = _fetch(url)
    html = body.decode("utf-8", "replace")
    base = {"url": url, "status": status, "kind": page_kind}

    noindex = bool(re.search(r'<meta[^>]+name=["\']robots["\'][^>]+content=["\'][^"]*noindex',
                             html, flags=re.I))
    canonical = _canonical(html)
    title = (_head_meta(html, "title") or
             (re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.S | re.I)
              and re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.S | re.I).group(1).strip())
             or "")
    desc = _head_meta(html, "description")
    h1 = len(re.findall(r"<h1[ >]", html, flags=re.I))
    h2 = len(re.findall(r"<h2[ >]", html, flags=re.I))
    words = len(_text_of(html).split())
    faq_present = bool(re.search(r'class="faq|id="faq|mainEntity[^}]*acceptedAnswer',
                                 html, flags=re.I))
    links = re.findall(r'<a [^>]*href=["\']([^"\']+)["\']', html, flags=re.I)
    internal = sum(1 for l in links if l.startswith("/") or "trypstore.com" in l
                   or "productfind.com" in l)
    amazon = [l for l in links if "amazon" in l]
    tagged = sum(1 for l in amazon if "tag=" in l)
    imgs = re.findall(r"<img[^>]*>", html, flags=re.I)
    imgs_alt = sum(1 for i in imgs if re.search(r'alt=["\'][^"\']+', i, flags=re.I))

    blocks = extract_jsonld(html)
    nodes = _graph_nodes(blocks)
    types = {}
    for n in nodes:
        ty = n.get("@type") or ""
        for t in (ty if isinstance(ty, list) else [ty]):
            types[t] = types.get(t, 0) + 1
    pq = _product_quality(blocks)

    schema_ok = True
    schema_notes = []
    if pq["products"] and not pq["priced"]:
        schema_ok = False
        schema_notes.append(REVIEW_SNIPPET_PRICE)
    if pq["products"] and not pq["rated"]:
        schema_notes.append(REVIEW_SNIPPET_RATING)
    if (types.get("FAQPage") or "FAQPage" in types) and not faq_present:
        schema_notes.append(FAQ_REQUIRED)

    base.update({
        "noindex": bool(noindex),
        "canonical": canonical,
        "title_len": len(title), "desc_len": len(desc),
        "h1": h1, "h2": h2, "words": words,
        "internal_links": internal,
        "amazon_links": len(amazon), "amazon_tagged": tagged,
        "images": len(imgs), "images_alt": imgs_alt,
        "jsonld_blocks": len(blocks),
        "ld_types": types,
        "product_quality": pq,
        "faq_present": faq_present,
        "schema_ok": schema_ok,
        "schema_notes": schema_notes,
        "html_bytes": len(body),
        "gzip": headers.get("content-encoding") == "gzip",
        "cache": headers.get("cache-control") or "",
        "ttfb": 0,  # set below for our side; competitor TTFB not comparable
    })
    return base


def score_page(p, site):
    """0-10 per page for one signal family; returns (score, notes bool)."""
    s = 0.0
    if p.get("noindex"):
        return 0.0
    if site == "ours":
        s += 3.0  # +ttfb below
        if p.get("status") == 200:
            s += 1.0
        if p.get("ttfb", 0) and p["ttfb"] < 800:
            s += 3.0
    return s


def score_site(pages, site):
    """Aggregate a scored matrix for one site across 6 dimensions (0-100)."""
    def avg(fn):
        vals = [fn(p) for p in pages if p.get("status") == 200]
        return round(sum(vals) / len(vals)) if vals else 0

    crawl = avg(lambda p: 100 * (1 if not p["noindex"] else 0) * (
        1 if p.get("status") == 200 else 0) * (0.5 if not p["canonical"] else 1))
    crawl = avg(lambda p: 100 * (0.0 if p["noindex"] else (
        1.0 if p["status"] == 200 and p["canonical"] else 0.5)))

    def schema_score(p):
        if not p["jsonld_blocks"]:
            return 0
        s = 40.0  # has some LD+JSON
        if p["schema_ok"]:
            s += 20
        pq = p["product_quality"]
        if pq["priced"]:
            s += 20
        if pq["rated"]:
            s += 10
        s += 10 * min(1.0, p.get("faq_present", 0))
        return min(100, s)
    schema = avg(schema_score)

    def content_score(p):
        s = 0.0
        s += 25 * (1 if 30 <= p["title_len"] <= 60 else 0)
        s += 15 * (1 if 70 <= p["desc_len"] <= 160 else 0)
        s += 10 * (1 if p["h1"] == 1 else 0)
        s += 10 * (1 if p["h2"] >= 3 else 0)
        s += 20 * (1 if p["words"] >= 600 else 0 if p["words"] >= 300 else 0.4 if p["words"] >= 150 else 0.1)
        s += 10 * (1 if p["faq_present"] else 0)
        s += 10 * (1 if p["images"] >= 3 else 0)
        return min(100, s)
    content = avg(content_score)

    def perf_score(p):
        s = 0.0
        if p["html_bytes"] and p["html_bytes"] < 150000:
            s += 40
        elif p["html_bytes"] and p["html_bytes"] < 300000:
            s += 25
        if p["gzip"]:
            s += 15
        if p["cache"]:
            s += 15
        if p["html_bytes"] and p["html_bytes"] < 6000:
            s += 10
        return min(100, s)
    performance = avg(perf_score)

    def affiliate_score(p):
        s = 0.0
        # A "best-of" affiliate engine monetizes: outbound tagged links to the
        # retail site on every pick, and human-readable image alt text.
        if p["amazon_links"]:
            s += 40.0
            s += 40.0 * (p["amazon_tagged"] / p["amazon_links"]
                         if p["amazon_links"] else 0.0)
        if p["images"]:
            s += 20.0 * (p["images_alt"] / p["images"]
                         if p["images"] else 0.0)
        return min(100, s)
    affiliate = avg(affiliate_score)

    def features_score(p):
        s = 0.0
        t = p["ld_types"]
        if "Product" in t:
            s += 30
        if "AggregateRating" in t:
            s += 15
        if "Offer" in t:
            s += 10
        if "FAQPage" in t:
            s += 15
        if "BreadcrumbList" in t:
            s += 10
        if "Organization" in t:
            s += 10
        if "WebSite" in t:
            s += 10
        return min(100, s)
    features = avg(features_score)

    return {
        "crawl": crawl, "schema": schema, "content": content,
        "performance": performance, "affiliate": affiliate, "features": features,
    }


def compare(ours_pages, theirs_pages, ours_label="trypstore", theirs_label="competitor"):
    """Full head-to-head: per-dimension win + overall + prioritized gap list."""
    ours = score_site(ours_pages, "ours")
    theirs = score_site(theirs_pages, "theirs")
    matrix = {"our_name": ours_label, "their_name": theirs_label, "dimensions": {}}
    for d in DIMENSIONS:
        matrix["dimensions"][d] = {
            "label": DIM_LABEL[d],
            "us": ours[d], "them": theirs[d],
            "winner": ours_label if ours[d] >= theirs[d] else theirs_label,
        }
    ours_avg = round(sum(ours[d] for d in DIMENSIONS) / len(DIMENSIONS))
    theirs_avg = round(sum(theirs[d] for d in DIMENSIONS) / len(DIMENSIONS))
    matrix["overall"] = {
        "us": ours_avg, "them": theirs_avg,
        "winner": ours_label if ours_avg >= theirs_avg else theirs_label,
    }
    matrix["gaps"] = _gap_list(ours_pages, theirs_pages, matrix)
    return matrix


def _gap_list(ours_pages, theirs_pages, matrix):
    """Concrete prioritized fixes where the competitor out-scores us."""
    gaps = []
    our_has_website = any("WebSite" in (p.get("ld_types") or {}) for p in ours_pages)
    their_has_website = any("WebSite" in (p.get("ld_types") or {}) for p in theirs_pages)
    if their_has_website and not our_has_website:
        gaps.append("Competitor ships WebSite + a Sitelinks SearchBox (SearchAction) "
                    "schema on every page. Add the WebSite node to the page graph so "
                    "Google can show the searchbox in results.")
    if matrix["dimensions"]["schema"]["them"] > matrix["dimensions"]["schema"]["us"]:
        gaps.append("Competitor emits richer Product/Offer/rating markup per pick — "
                    "they mark price + reviews on every item. Match by keeping "
                    "price/rating populated on every ranked product (re-mine), not "
                    "just the top pick.")
    our_imgs = sum(p["images"] for p in ours_pages)
    their_imgs = sum(p["images"] for p in theirs_pages)
    if their_imgs > our_imgs:
        gaps.append("Competitor renders the product photo on every pick (they also "
                    "alt them). Add a real product <img> (data image, not the og "
                    "placeholder) to each ranked card to earn image + rich results.")
    if matrix["dimensions"]["content"]["them"] > matrix["dimensions"]["content"]["us"]:
        gaps.append("Competitor deepens each page (H2 sections, FAQ block, longer "
                    "copy). Add a real FAQ section + 3+ H2 decision sections to "
                    "every /n/ guide so word count clears 600.")
    if matrix["dimensions"]["features"]["them"] > matrix["dimensions"]["features"]["us"]:
        gaps.append("Competitor earns extra rich results (WebSite+SearchAction, "
                    "Organization+Breadcrumb consistently). Ensure every page ships "
                    "Organization + WebSite + BreadcrumbList.")
    return gaps or ["No competitor gap in the measured engine signals."]


def render_matrix(m):
    lines = []
    lines.append("\nSEO ENGINE BENCHMARK — %s vs %s" % (m["our_name"], m["their_name"]))
    lines.append("%-24s %8s %8s %8s" % ("Dimension", m["our_name"], m["their_name"], "leader"))
    dims = m["dimensions"]
    for d in DIMENSIONS:
        r = dims[d]
        lines.append("%-24s %8d %8d   %s" % (r["label"], r["us"], r["them"], r["winner"]))
    o = m["overall"]
    lines.append("%-24s %8d %8d   %s" % ("OVERALL", o["us"], o["them"], o["winner"]))
    lines.append("\nPrioritized gaps:")
    for g in m["gaps"]:
        lines.append("  - " + g)
    return "\n".join(lines)


def matrix_to_csv(m):
    out = ["dimension,%s,%s" % (m["our_name"], m["their_name"])]
    for d in DIMENSIONS:
        r = m["dimensions"][d]
        out.append("%s,%d,%d" % (d, r["us"], r["them"]))
    o = m["overall"]
    out.append("overall,%d,%d" % (o["us"], o["them"]))
    return "\n".join(out)


def fetch_pages(pairs, tag):
    """Fetch paired url lists; pairs = [(kind, our_url, their_url)]."""
    ours, theirs = [], []
    for kind, a, b in pairs[: _MAX]:
        if a:
            ours.append(analyze_page(kind, a, kind, tag))
        if b:
            theirs.append(analyze_page(kind, b, kind, tag))
    return ours, theirs


def main(argv):
    """CLI: python3 seobench.py [--json] <our-guide> <their-guide> [<our-lp> <their-lp> ...]"""
    if "--json" in argv:
        fmt = "json"
        argv = [a for a in argv if a != "--json"]
    else:
        fmt = "text"
    if len(argv) < 2:
        print(__doc__)
        return 2
    pairs = []
    urls = argv[: _MAX * 2]
    kinds = ["guide", "story", "landing", "guide", "story", "landing",
             "guide", "story"]
    for i in range(0, len(urls), 2):
        a = urls[i]
        b = urls[i + 1] if i + 1 < len(urls) else None
        pairs.append((kinds[(i // 2) % len(kinds)], a, b))
    ours, theirs = fetch_pages(pairs, "guard")
    m = compare(ours, theirs)
    if fmt == "json":
        print(json.dumps(m, indent=2))
    else:
        print(render_matrix(m))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))