# -*- coding: utf-8 -*-
"""pstore SEO layer: fully server-rendered, crawlable pages + search-engine
packaging (meta, OpenGraph, Twitter cards, JSON-LD, canonical, sitemap,
robots). Every niche/search page is plain static HTML so Google can index it
without JavaScript.

Injected affiliate links carry the ?tag= built by amazon.affiliate_url, so each
indexed page is a live direct-buyer link surface.
"""
import html
import json
import os
import re
import hashlib
import amazon
from datetime import datetime

import editorial

SITE_NAME = "pstore"
SITE_DESC = "Hand-picked Amazon product picks by niche."
BASE_URL = os.environ.get("PSTORE_URL", "https://trypstore.com").rstrip("/")

# Blog cards per page — keeps /blog light (the old single page ballooned to
# ~445KB / 485 <h2> once the niche list grew).
BLOG_PAGE_SIZE = 24

# Optional Google Search Console ownership token — emits <meta name="google-site-verification">.
GOOGLE_SITE_VERIFICATION = os.environ.get("PSTORE_GOOGLE_SITE_VERIFICATION", "")
BING_SITE_VERIFICATION = os.environ.get("PSTORE_BING_SITE_VERIFICATION", "")
YANDEX_SITE_VERIFICATION = os.environ.get("PSTORE_YANDEX_VERIFICATION", "")
PINTEREST_SITE_VERIFICATION = os.environ.get("PSTORE_PINTEREST_VERIFICATION", "")
# Runtime override set via the /keys hub (never written to disk) so a saved
# token takes effect immediately without a restart.
_GOOGLE_SITE_VERIFICATION_RUNTIME = None
_BING_SITE_VERIFICATION_RUNTIME = None
_YANDEX_SITE_VERIFICATION_RUNTIME = None
_PINTEREST_SITE_VERIFICATION_RUNTIME = None


_META_RE = re.compile(r"content\s*=\s*[\"']?([^\"' >]+)[\"']?", re.I)


def _gsc_token(value):
    """Normalize whatever the operator pasted into the bare token. Handles all
    the ways Google hands out the value:
      * the raw token (googlexxxx…)
      * the HTML file NAME (googlexxxx….html)
      * the HTML file BODY (google-site-verification: googlexxxx…)
      * a content="…" snippet
      * the entire <meta name=…> tag they'd place in <head>"""
    val = (value or "").strip()
    if not val:
        return ""
    if val.startswith("<"):
        m = _META_RE.search(val)
        val = m.group(1).strip() if m else ""
    elif val.lower().startswith("p:domain_verify:"):
        val = val[len("p:domain_verify:"):].strip().strip("\"' ")
    elif val.lower().startswith("google-site-verification:"):
        val = val.split(":", 1)[1].strip()
    elif val.lower().startswith("content="):
        val = val[len("content="):].strip()
        if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
            val = val[1:-1]
    if val.lower().endswith(".html"):
        val = val[:-5]
    return val.strip()


def set_google_site_verification(token):
    """Set (or clear with "") the Google Search Console token for this process.
    Accepts the bare token or the whole meta tag / content snippet."""
    global _GOOGLE_SITE_VERIFICATION_RUNTIME
    _GOOGLE_SITE_VERIFICATION_RUNTIME = _gsc_token(token)


def set_bing_site_verification(token):
    """Set (or clear with "") the Bing msvalidate.01 token for this process."""
    global _BING_SITE_VERIFICATION_RUNTIME
    _BING_SITE_VERIFICATION_RUNTIME = _gsc_token(token)


def set_yandex_site_verification(token):
    """Set (or clear with "") the Yandex yandex-verification token."""
    global _YANDEX_SITE_VERIFICATION_RUNTIME
    _YANDEX_SITE_VERIFICATION_RUNTIME = _gsc_token(token)


def set_pinterest_site_verification(token):
    """Set (or clear with "") the Pinterest p:domain_verify token. Accepts the
    bare hex token or the whole meta tag / content snippet."""
    global _PINTEREST_SITE_VERIFICATION_RUNTIME
    _PINTEREST_SITE_VERIFICATION_RUNTIME = _gsc_token(token)


def google_site_verification():
    """Effective token: runtime override first, else env (module-load) value."""
    if _GOOGLE_SITE_VERIFICATION_RUNTIME is not None:
        return _GOOGLE_SITE_VERIFICATION_RUNTIME
    return _gsc_token(GOOGLE_SITE_VERIFICATION)


def bing_site_verification():
    if _BING_SITE_VERIFICATION_RUNTIME is not None:
        return _BING_SITE_VERIFICATION_RUNTIME
    return _gsc_token(BING_SITE_VERIFICATION)


def yandex_site_verification():
    if _YANDEX_SITE_VERIFICATION_RUNTIME is not None:
        return _YANDEX_SITE_VERIFICATION_RUNTIME
    return _gsc_token(YANDEX_SITE_VERIFICATION)


def pinterest_site_verification():
    if _PINTEREST_SITE_VERIFICATION_RUNTIME is not None:
        return _PINTEREST_SITE_VERIFICATION_RUNTIME
    return _gsc_token(PINTEREST_SITE_VERIFICATION)


def verification_metas():
    """All ownership meta tags (Google + Bing + Yandex + Pinterest) as one
    string, "" when none configured. Emitted in <head> on every public page."""
    out = []
    g = google_site_verification()
    if g:
        out.append('<meta name="google-site-verification" content="%s">\n' % _clean(g))
    b = bing_site_verification()
    if b:
        out.append('<meta name="msvalidate.01" content="%s">\n' % _clean(b))
    y = yandex_site_verification()
    if y:
        out.append('<meta name="yandex-verification" content="%s">\n' % _clean(y))
    pi = pinterest_site_verification()
    if pi:
        out.append('<meta name="p:domain_verify" content="%s">\n' % _clean(pi))
    return "".join(out)


def serve_verification_file(path):
    """Google's HTML-file method asks for `/<token>.html`. Current GSC checks
    the served document for the matching <meta> tag (a plain
    `google-site-verification:` text line alone is now rejected as wrong
    content), so serve a full HTML document mirroring Google's download:
    meta tag in <head> plus the legacy plain-text line in <body>."""
    t = google_site_verification()
    if not t:
        return None
    base = (path or "").strip("/")
    if base and base.lower().endswith(".html") and base[:-5] == t:
        return ("<!DOCTYPE html>\n"
                "<html xmlns=\"http://www.w3.org/1999/xhtml\">\n"
                "<head>\n<meta name=\"google-site-verification\" "
                "content=\"%s\" />\n</head>\n<body>\n"
                "google-site-verification: %s\n</body>\n</html>\n") % (t, t)
    return None
# Organization identity shown in JSON-LD (schema.org Organization / Person).
ORG_NAME = SITE_NAME
ORG_URL = BASE_URL
CONTACT_URL = BASE_URL + "/contact"
AUTHOR_NAME = "pstore Editorial Team"


def _org_jsonld():
    """Small Organization node referenced by other graphs (WebSite/Article)."""
    return {
        "@type": "Organization", "name": SITE_NAME,
        "url": ORG_URL, "logo": ORG_URL + "/og/home",
        "contactPoint": {"@type": "ContactPoint", "url": CONTACT_URL},
    }


def _website_jsonld():
    return {
        "@context": "https://schema.org", "@type": "WebSite",
        "name": SITE_NAME, "url": ORG_URL,
    }


def _clean(s):
    return html.escape(str(s or ""), quote=True)


def _slugify(text):
    s = (text or "").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "niche"


def _product_graph(items, page_url=None, slug=None):
    """Product nodes (+Offer, +AggregateRating) for niche/topic/landing pages.
    Only fully-populated products are marked up: Google rejects an Offer without
    a price and flags nodes missing priceCurrency, availability, aggregateRating
    or review — so rows missing the price or the rating data are skipped rather
    than serialized half-dressed. Each node carries a stable @id so the graph's
    ItemList can reference it."""
    graph = []
    for it in (items or [])[:10]:
        if not it.get("title"):
            continue
        price = it.get("price")
        have_price = price not in (None, "") and not (
            isinstance(price, (int, float)) and float(price) <= 0)
        stars = it.get("stars")
        reviews = it.get("reviews")
        if not have_price or not stars or not reviews:
            continue
        node = {"@type": "Product", "name": it.get("title")}
        if it.get("image"):
            node["image"] = it["image"]
        elif slug:
            node["image"] = "%s/og/%s.png" % (BASE_URL, slug)
        if it.get("title"):
            node["description"] = it["title"]
        if it.get("asin"):
            node["sku"] = it["asin"]
            node["mpn"] = it["asin"]
            if page_url:
                node["@id"] = page_url.rstrip("/") + "#product-" + it["asin"]
        node["offers"] = {"@type": "Offer",
                          "price": price if isinstance(price, (int, float))
                          else str(price),
                          "priceCurrency": it.get("currency") or "USD",
                          "availability": "https://schema.org/InStock"}
        if it.get("url"):
            node["offers"]["url"] = it["url"]
        node["aggregateRating"] = {
            "@type": "AggregateRating",
            "ratingValue": round(float(stars), 1),
            "reviewCount": int(reviews),
            "bestRating": 5,
            "worstRating": 1,
        }
        graph.append(node)
    return graph


def landing_product_jsonld(pick, page_url, image_url=""):
    """Single-Product graph for the /lp/<slug> sales page (the top pick, so the
    page stays "one-product focused" per Google merchant guidelines). Nulls and
    partial ratings are omitted rather than serialized. Also carries the site's
    trailing BreadcrumbList in the same graph."""
    if not pick or not pick.get("title"):
        return None
    node = {
        "@type": "Product",
        "name": pick.get("title"),
        "description": pick.get("title"),
        "image": pick.get("image") or image_url or "",
    }
    if pick.get("asin"):
        node["sku"] = pick["asin"]
        if page_url:
            node["@id"] = page_url.rstrip("/") + "#product-" + pick["asin"]
    price = pick.get("price")
    have_price = price not in (None, "") and not (
        isinstance(price, (int, float)) and float(price) <= 0)
    offers = {"@type": "Offer"}
    if pick.get("url"):
        offers["url"] = pick["url"]
    if have_price:
        offers["price"] = price if isinstance(price, (int, float)) \
            else str(price)
        offers["priceCurrency"] = pick.get("currency") or "USD"
        offers["availability"] = "https://schema.org/InStock"
        node["offers"] = offers
    stars, reviews = pick.get("stars"), pick.get("reviews")
    if stars and reviews:
        node["aggregateRating"] = {
            "@type": "AggregateRating",
            "ratingValue": round(float(stars), 1),
            "reviewCount": int(reviews),
            "bestRating": 5,
            "worstRating": 1,
        }
    graph = [node]
    if page_url:
        kw = (pick.get("keyword") or "").strip()
        bcrumb = editorial.breadcrumb_jsonld(kw or pick.get("slug") or "pick")
        bcrumb["itemListElement"].append({
            "@type": "ListItem",
            "position": len(bcrumb["itemListElement"]) + 1,
            "name": pick.get("title", "")[:120],
            "item": page_url})
        graph.append(bcrumb)
    return {"@context": "https://schema.org", "@graph": graph}


def _head(title, desc, canonical, path, jsonld=None, og_image=None, noindex=False, extra=""):
    """Build the shared <head>. `extra` injects raw <link>/<meta> tags (used by
    the paginated blog for rel prev/next) right after the canonical tag."""
    img_html = ""
    if og_image:
        abs_img = og_image if str(og_image).startswith("http") else BASE_URL + og_image
        img_html = (f'<meta property="og:image" content="{_clean(abs_img)}">\n'
                    f'<meta property="og:image:width" content="1200">\n'
                    f'<meta property="og:image:height" content="630">\n'
                    f'<meta property="og:image:alt" content="{_clean(title)}">\n'
                    f'<meta name="twitter:image" content="{_clean(abs_img)}">\n')
    gsc = verification_metas()
    rob = ('<meta name="robots" content="noindex,nofollow">\n' if noindex else "")
    head = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_clean(title)} | {SITE_NAME}</title>
<meta name="description" content="{_clean(desc)}">
<link rel="icon" type="image/png" href="{_clean(BASE_URL)}/og/favicon.png">
{rob}<link rel="canonical" href="{_clean(BASE_URL + path)}">
{extra}
<meta property="og:locale" content="en_US">
<meta property="og:type" content="website">
<meta property="og:title" content="{_clean(title)}">
<meta property="og:description" content="{_clean(desc)}">
<meta property="og:url" content="{_clean(BASE_URL + path)}">
<meta property="og:site_name" content="{SITE_NAME}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{_clean(title)}">
<meta name="twitter:description" content="{_clean(desc)}">
{img_html}{gsc}<link rel="stylesheet" href="/style.css">
""".encode("utf-8")
    if jsonld:
        payload = json.dumps(jsonld).replace("</", "<\\/")
        head += ("<script type=\"application/ld+json\">%s</script>\n" % payload).encode("utf-8")
    head += b"</head>\n<body class=\"pub\">\n"
    return head


def _masthead(links, cta=("/#top-picks", "Today's top picks")):
    """Premium sticky masthead for every public page. `links` is a list of
    (label, href, active) triples rendered as the nav row; `cta` is the
    (href, label) pair for the highlight action. Keeps the marketplace
    switcher when more than one market is enabled."""
    items = "".join(
        ('<a href="%s"%s>%s</a>' % (_clean(href), " class=\"on\"" if active else "",
                                    _clean(label)))
        for label, href, active in links)
    cta_html = ""
    if cta and cta[0] and cta[1]:
        cta_html = '<a class="btn mast-cta" href="%s">%s</a>' \
            % (_clean(cta[0]), _clean(cta[1]))
    return ("<header id=\"top\" class=\"mast\"><div class=\"mast-inner\">"
            "<a class=\"logo\" href=\"/\" aria-label=\"%s home\">"
            "<span class=\"mark\">P</span><span class=\"word\">%s</span></a>"
            "<nav class=\"mast-nav\">%s</nav>%s%s</div></header>"
            % (_clean(SITE_NAME), _clean(SITE_NAME), items, cta_html,
               market_switcher_html()))


def _footer():
    return f"""<footer>
  <div class="foot-wrap">
    <div class="foot-grid">
      <div class="foot-brand">
        <a class="logo" href="/" aria-label="{_clean(SITE_NAME)} home">
          <span class="mark">P</span><span class="word">{_clean(SITE_NAME)}</span></a>
        <p>{_clean(SITE_DESC)}</p>
        <p>Prices are indicative — always confirm the live price on Amazon before ordering.</p>
      </div>
      <div class="fcol">
        <h4>Browse</h4>
        <div class="foot-links">
          <a href="/#top-picks">Today's top picks</a>
          <a href="/#niches">All niches</a>
          <a href="/blog">Blog</a>
          <a href="/stories">Stories</a>
        </div>
      </div>
      <div class="fcol">
        <h4>Company</h4>
        <div class="foot-links">
          <a href="/about">About</a>
          <a href="/contact">Contact</a>
          <a href="/#method">How we pick</a>
          <a href="/#notify">Stay updated</a>
        </div>
      </div>
      <div class="fcol">
        <h4>Legal</h4>
        <div class="foot-links">
          <a href="/disclosure">Disclosure</a>
          <a href="/privacy">Privacy</a>
          <a href="/terms">Terms</a>
          <a href="/sitemap.xml">Sitemap</a>
        </div>
      </div>
    </div>
    <div class="foot-legal">
      <span>© {datetime.now().year} {_clean(SITE_NAME)} — comparison picks from live Amazon data.</span>
      <span>As an Amazon Associate we earn from qualifying purchases.</span>
    </div>
  </div>
</footer>
<div class="totop" aria-hidden="false">
  <a href="#top" aria-label="Back to top">&uarr;</a>
</div>
<script src="/ui.js" defer></script>
</body>
</html>
""".encode("utf-8")


# ------------------------------------------------------------------ info pages
STATIC_PAGES = ["about", "contact", "privacy", "terms", "disclosure"]
CONTACT_EMAIL = os.environ.get("PSTORE_CONTACT", "hello@trypstore.com")


def _page_header():
    return _masthead([("Home", "/", True), ("Blog", "/blog", False),
                      ("Stories", "/stories", False)],
                     cta=("/#top-picks", "Today's top picks"))


def render_page(slug, title, desc, content_html):
    """Generic crawlable info page (abouts/legal). Returns full HTML bytes."""
    canonical = "/" + slug
    jsonld = {"@context": "https://schema.org", "@type": "WebPage",
              "name": SITE_NAME, "description": desc, "url": BASE_URL + canonical}
    head = _head(title, desc, canonical, canonical, jsonld=jsonld)
    crumbs = ('<nav class="crumbs"><a href="/">Home</a>'
              '<span class="sep">›</span><span>%s</span></nav>' % _clean(title))
    body = ("%s\n<main class=\"page\">%s<article class=\"card\"><h1>%s</h1>"
            "%s</article></main>\n"
            % (_page_header(), crumbs, _clean(title), content_html)).encode("utf-8")
    return head + body + _footer()


def render_404():
    """Friendly, noindex 404 page for unknown page URLs — never a JSON 404 in a
    browser. No canonical/og:url so crawlers treat it as a soft-404, not a page."""
    title = "Page not found"
    desc = "The page you were looking for doesn't exist on %s anymore — or never did." % SITE_NAME
    head = _head(title, desc, "/404", "/", noindex=True)
    body = ("%s\n<main class=\"page\"><article class=\"card\">"
            "<p class=\"eyebrow\">404</p>"
            "<h1>%s</h1>"
            "<p class=\"lede\">The address you opened isn't a page on %s.</p>"
            "<div class=\"hero-ctas\">"
            "<a class=\"btn\" href=\"/\">Back to the homepage</a>"
            "<a class=\"btn ghost\" href=\"/blog\">Browse the blog</a>"
            "</div></article></main>\n"
            % (_page_header(), _clean(title), _clean(SITE_NAME))).encode("utf-8")
    return head + body + _footer()


def render_about():
    desc = "What pstore is — independent, niche-by-niche Amazon product picks and how we make them."
    content = f"""
<p>pstore is an independent product-discovery site. We research Amazon's own catalog by niche,
then rank products by demand, rating and saturation so shoppers can compare the strongest picks in
one place instead of dredging through pages of results.</p>
<h2>How picks are made</h2>
<ul>
  <li><b>Mine the niche.</b> Each topic starts from a broad seed and is expanded using Amazon's
  autosuggest index to surface the search terms real shoppers use.</li>
  <li><b>Rank by real signals.</b> Products are scored on demand and saturation from live listings —
  price, rating and review volume — never on who pays us.</li>
  <li><b>Curate honestly.</b> If a product doesn't clear the bar, it's not listed. We correct or drop
  picks when data changes.</li>
</ul>
<h2>Editorial independence</h2>
<p>Vendors can't buy a placement here. Some links are affiliate links (see our
<a href="/disclosure">Disclosure</a>), which means we may earn a small commission if you buy after
clicking them — at no extra cost to you. Affiliate relationships never affect which products are chosen
or their ranking.</p>
<p>Questions or corrections? <a href="/contact">Contact us</a>.</p>
"""
    return render_page("about", "About pstore", desc, content)


def render_contact():
    desc = "How to reach pstore — corrections, questions and feedback."
    content = f"""
<p>We read and answer every message. Whether it's a price correction, a niche you'd love to see,
or a general question, drop us a line:</p>
<p><a class="btn" href="mailto:{CONTACT_EMAIL}">✉️ {CONTACT_EMAIL}</a></p>
<h2>What to include</h2>
<ul>
  <li>Which page or product you're writing about (a link helps).</li>
  <li>What you noticed — price, availability, rating, or a missing pick.</li>
  <li>Your name and a way to reply, if you'd like one.</li>
</ul>
<p class="hint">We reply within 1–2 business days. Please don't send unsolicited marketing or
partnership pitches — see <a href="/about">About</a> for how we decide what's listed.</p>
"""
    return render_page("contact", "Contact pstore", desc, content)


def render_privacy():
    desc = "What data pstore collects and how it's used."
    content = f"""
<h2>What we collect</h2>
<ul>
  <li><b>Server logs.</b> Standard web logs (IP, user agent, page requested) used for uptime,
  abuse prevention and analytics. Logs aren't sold or shared.</li>
  <li><b>Opt-in emails.</b> If you join our niche-update list, we keep the email address and the
  niche you signed up for, so we can send the updates you asked for. You can unsubscribe from any
  email we send, or hit the unsubscribe link on any email, and we stop immediately.</li>
  <li><b>Click analytics.</b> When you click an affiliate link on our pages, we record a hashed
  fingerprint of your visit (never a raw address) to see which picks get clicked. We do not build
  cross-site profiles.</li>
  <li><b>Affiliate cookies.</b> When you click an affiliate link to Amazon, Amazon may set a short
  cookie that lets them credit the referral. We don't see or store your Amazon account data.</li>
  <li><b>Search-engine tokens.</b> We submit our pages to search engines (e.g. via IndexNow/Bing).
  No personal data is involved.</li>
</ul>
<h2>What we don't</h2>
<p>We have no accounts and no payment processing. We don't sell or rent email addresses, we don't
build profiles, track you across other sites, or use advertising trackers beyond the affiliate
relationship above.</p>
<h2>Emails</h2>
<p>Email addresses are only used to send the niche updates you opted into. Every email includes a
working unsubscribe link, and unsubscribing removes you from future sends.</p>
<h2>Third parties</h2>
<p>Product data and prices come from Amazon; a QR widget may load images from a third-party API.
Those services have their own privacy terms. Outbound links leave this site.</p>
<h2>Contact</h2>
<p>Privacy questions: <a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a></p>
"""
    return render_page("privacy", "Privacy Policy", desc, content)


def render_terms():
    desc = "Terms of service for using pstore."
    content = f"""
<h2>Use of this site</h2>
<p>Content is provided for personal, non-commercial browsing. You may share individual pages and
links, but not scrape, mirror or republish our picks at scale.</p>
<h2>Third-party products and links</h2>
<p>Product listings, prices, availability and offers originate from Amazon, where purchases happen.
We're an independent site and not an Amazon seller. Prices shown are indicative and change frequently —
always confirm price and availability on Amazon before buying.</p>
<h2>Affiliate relationship</h2>
<p>Some outbound links are affiliate links. As an Amazon Associate we earn from qualifying purchases,
at no extra cost to you. See our <a href="/disclosure">Disclosure</a>.</p>
<h2>No warranty / liability</h2>
<p>Picks are informational opinions, not professional advice. We work to keep data accurate but can't
warrant that every price, rating or description is current. To the fullest extent permitted by law,
pstore isn't liable for decisions made based on this content.</p>
<h2>Changes</h2>
<p>These terms may be updated; the dated version on this page governs. Continued use means you accept
any updates.</p>
<h2>Contact</h2>
<p><a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a></p>
"""
    return render_page("terms", "Terms of Service", desc, content)


def render_disclosure():
    desc = "pstore affiliate disclosure — how recommendations are funded."
    content = f"""
<p>pstore is reader-supported. Some of the links on this site are affiliate links — most notably
through the <b>Amazon Associates</b> program — and we may earn a small commission, at no extra cost to
you, when you click through and complete a qualifying purchase.</p>
<h2>Being upfront</h2>
<ul>
  <li>A commission never changes the price you pay.</li>
  <li>A commission never determines whether (or where) a product appears on this site.</li>
  <li>Picks are chosen for quality, demand and signal strength — not for affiliate payout.</li>
</ul>
<p>"As an Amazon Associate I earn from qualifying purchases."</p>
<p>This disclosure is required by the U.S. Federal Trade Commission and by the Amazon Associates
operating agreement, and we're glad to honor both. If you ever see a placement that feels like it
contradicts this, <a href="/contact">tell us</a>.</p>
"""
    return render_page("disclosure", "Affiliate Disclosure", desc, content)


def optin_html(keyword, source="niche", anchor=""):
    """Email-capture widget: freq 0->1 niche updates. Hooks rendered by courier.js."""
    kw = _clean(keyword or "picks")
    label = ("Updates when these %s picks change" % keyword) if keyword else "The pstore picks note"
    sub = ("One honest email when this page's picks move — price drops, sold-out swaps, "
           "new top-rated options. Unsubscribe any time.")
    fid = ' id="%s"' % _clean(anchor) if anchor else ""
    return f"""<form class="courier card"{fid} action="/subscribe" method="post">
  <h3>{label}</h3>
  <p class="hint">{sub}</p>
  <div class="courier-row">
    <input type="text" name="first_name" placeholder="First name (optional)" autocomplete="given-name" maxlength="80">
    <input type="email" name="email" placeholder="you@example.com" required autocomplete="email">
    <input type="hidden" name="keyword" value="{kw}">
    <button type="submit" class="warm">Notify me</button>
  </div>
  <p class="courier-msg"></p>
  <noscript><p class="hint">Email us a note at {CONTACT_EMAIL} to be notified when this page updates.</p></noscript>
</form>"""


_LEAD_GATE_CSS = """
.gate{box-sizing:border-box;max-width:600px;margin:26px auto 0;padding:30px 28px;border:1px solid var(--line,#e6e9ee);border-radius:20px;background:linear-gradient(180deg,#ffffff,#f7fafb);text-align:center;box-shadow:0 20px 48px rgba(18,22,33,.08)}
.gate .gift{font-size:34px;line-height:1;margin-bottom:10px}
.gate h2{margin:0 0 6px;font-size:23px;color:var(--ink,#10131b);letter-spacing:-.015em}
.gate .muted{color:var(--muted,#66707f);font-size:14.5px;margin:4px auto 12px;line-height:1.6;max-width:440px}
.gate form{display:flex;flex-wrap:wrap;gap:10px;max-width:460px;margin:14px auto 0}
.gate form input{flex:1 1 190px;min-width:0;padding:14px 16px;border:1.5px solid var(--border,#dfe4ea);border-radius:12px;font-size:15px;font-family:inherit;background:#fff}
.gate form input:focus{outline:none;border-color:var(--accent,#0f8b9d);box-shadow:0 0 0 4px rgba(15,139,157,.14)}
.gate form button{flex:1 1 150px;padding:14px 16px;border:0;border-radius:999px;font-size:15px;font-weight:800;color:#1c1305;background:linear-gradient(135deg,var(--grad-warm-from,#f59e0b),var(--grad-warm-to,#f7c048));cursor:pointer;box-shadow:0 8px 20px rgba(232,148,10,.3)}
.gate form button:hover{filter:brightness(1.05);transform:translateY(-1px)}
.gate .courier-msg{color:#159a4b;font-size:13px;min-height:18px;margin:10px 0 0}
.gate .cta{display:inline-block;margin-top:14px;padding:14px 26px;border-radius:999px;background:linear-gradient(135deg,var(--grad-from,#0f8b9d),var(--grad-to,#14afb6));color:#fff;font-weight:800;text-decoration:none;font-size:15px;box-shadow:0 8px 20px rgba(15,139,157,.3)}
.gate .hint{font-size:12px;color:var(--muted,#8a93a2);margin-top:12px}
"""


def lead_gate_html(keyword, source="niche"):
    """Email-gated free-guide PDF block for SEO pages (MME-6): the visitor enters
    their email to unlock the niche's generated PDF guide. Markup mirrors the
    CMS landing gate so courier.js' existing gate-form handler unlocks #gate-unlock
    with the /subscribe download token. Ships its own CSS (SEO pages don't load
    the CMS gate styles)."""
    kw = _clean(keyword or "picks")
    return f"""<style>{_LEAD_GATE_CSS}</style>
<div class="gate" id="gate" data-source="{_clean(source)}">
  <div class="gift">🎁</div>
  <h2>Free guide: best {kw} to buy</h2>
  <p class="muted">One compact PDF of the ranked picks — the score, the price, the
  quick take on each. We'll also ping you if any ranked pick's price drops.</p>
  <form class="courier gate-form" action="/subscribe" method="post">
    <input type="text" name="first_name" placeholder="First name" autocomplete="given-name">
    <input type="email" name="email" placeholder="you@example.com" required autocomplete="email">
    <input type="hidden" name="keyword" value="{kw}">
    <input type="hidden" name="source" value="{_clean(source)}-gate">
    <button type="submit">Send me the free guide →</button>
  </form>
  <p class="courier-msg gate-msg"></p>
  <a class="cta" id="gate-unlock" href="#" rel="noopener" style="display:none">⬇ Your guide is ready</a>
  <p class="hint">No spam. Unsubscribe any time. A price-drop alert only fires when a ranked pick's price changes.</p>
</div>
<script>var _lgcc=document.getElementById('gate');if(_lgcc){{var i=_lgcc.querySelector('input[name=email]');if(i)setTimeout(function(){{if(!document.body.getAttribute('data-opted'))i.focus();}},900);}}</script>
"""


def courier_script():
    return '<script src="/courier.js" defer></script>'.encode("utf-8")


_MARKET_LABELS = {"com": "US", "co.uk": "UK", "de": "DE", "ca": "CA",
                  "co.jp": "JP", "com.au": "AU", "in": "IN"}


def market_switcher_html():
    """Small client-side marketplace switcher shown only when more than one
    market is enabled (PSTORE_MARKETS). Every button bounces the page's
    Amazon links to that market's host + derived affiliate tag; courier.js
    applies the choice and remembers it per visitor. Empty string otherwise."""
    blob = amazon.markets_blob()
    if len(blob) < 2:
        return ""
    opts = "".join(
        '<button type="button" data-mkt="%s"%s>%s</button>'
        % (k, " class=\"on\"" if k == amazon.MARKET else "",
           _MARKET_LABELS.get(k, k))
        for k in ("com", "co.uk", "de", "ca", "co.jp", "com.au", "in")
        if k in blob)
    return ('<span class="market-switch" data-markets="%s" data-active="%s">'
            '<b>Shop:</b>%s</span>'
            % (_clean(json.dumps(blob)), _clean(amazon.MARKET), opts))


def render_landing(saved_niches):
    """Storefront-style home: value prop, how-we-pick, niche index, FAQ."""
    jsonld = {
        "@context": "https://schema.org", "@graph": [
            {"@type": "WebSite", "name": SITE_NAME, "url": BASE_URL},
            {"@type": "Organization", "name": SITE_NAME, "url": BASE_URL},
        ],
    }
    home_title = ("Best Amazon Picks by Niche — live prices, real ratings")
    home_desc = ("Ranked, data-backed best-Amazon-pick guides by niche — live price, "
                 "rating and review signals decide the ranking. Honest picks, no filler.")
    head = _head(home_title, home_desc, "/", "/", jsonld=jsonld,
                 og_image=BASE_URL + "/og/home.png")
    top_pick_niches = saved_niches or []
    # comparison preview of the single most-picked niche (scannable, table-flow pill)
    comp_kw = ""
    comp_preview = ""
    for n in (top_pick_niches or []):
        if n.get("products"):
            comp_kw = n["keyword"]
            comp_preview = editorial.comparison_html(n["products"], n["keyword"])
            break
    niche_count = len(top_pick_niches)
    pick_count = sum(len((n or {}).get("products") or []) for n in top_pick_niches)
    masthead = _masthead([("Home", "/", True), ("Niches", "/#niches", False),
                          ("Blog", "/blog", False), ("Stories", "/stories", False)],
                         cta=("/#top-picks", "Today's top picks"))
    jumps = ('<nav class="sec-jumps" aria-label="On this page">'
             '<a class="chip" href="#top-picks">Top picks today</a>'
             '<a class="chip" href="#niches">All niches</a>'
             '<a class="chip" href="#method">How we pick</a>'
             '<a class="chip" href="#faq">Quick questions</a></nav>')
    body = f"""
{masthead}
<main data-niche="home" data-source="home" data-keyword="best amazon niche picks" data-tag="{_clean(amazon.AFFILIATE_TAG)}">
<section class="hero-home">
  <p class="eyebrow">Ranked from live Amazon data</p>
  <h1>Find the best <em>Amazon picks</em>, by niche — before you scroll once.</h1>
  <p class="hero-sub">Every niche page ranks the strongest products on Amazon right now — live price,
  rating and review signals decide the verdict, and the proof is below the fold. No endless
  listicles, no guesswork: pick, price, reason.</p>
  <div class="hero-ctas">
    <a class="btn lg" href="#top-picks">See today's top picks</a>
    <a class="btn ghost lg" href="#notify">Get the free picks guide</a>
  </div>
  <div class="hero-stats">
    <div><b>{niche_count}</b><span>niches ranked</span></div>
    <div><b>{pick_count}</b><span>live product picks</span></div>
    <div><b>Live</b><span>prices pulled from Amazon</span></div>
    <div><b>Honest</b><span>no paid placement, ever</span></div>
  </div>
</section>
{jumps}
<section id="top-picks">
{editorial.quick_picks_band(saved_niches)}
{comp_preview and ("<section class='card'><h2>Compare the shortlist — {0}</h2><p class='hint'>Scannable table of the live picks for {1}. Swipe or scroll sideways if it overflows.</p>{2}</section>".format(_clean(comp_kw.title()), _clean(comp_kw), comp_preview)) or ""}
</section>
{editorial.home_trust_strip()}
{optin_html(comp_kw, "home", anchor="notify")}

{editorial.niche_grid(saved_niches, anchor="niches")}

<section class="card" id="method"><h2>How we pick</h2>
<div class="features">
  <div class="feature"><h3>Niche mining</h3>
  <p>We expand each topic through Amazon's own autosuggest index to find the terms real shoppers use.</p></div>
  <div class="feature"><h3>Real signals</h3>
  <p>Products are ranked on demand and saturation from live listings — price, rating and review volume.</p></div>
  <div class="feature"><h3>Shop on Amazon</h3>
  <p>Every pick links straight to the product on Amazon. Purchases may earn us a commission at no cost to you.</p></div>
</div>
<div class="trust">
  <h3>The one thing most review sites skip</h3>
  <p>Most “best X to buy” pages make you wade through 2,000 words before showing a price, and never tell you
  when their data went stale. We do the opposite: the pick, the rank and the live price come first, and every
  page says it reflects <b>current Amazon listings</b> — prices move, so we re-pull rather than guess.</p>
</div></section>

<section class="card" id="faq"><h2>Quick questions</h2>
<div class="sub">
<h3>Do your links cost me anything?</h3>
<p>No. If you buy after clicking a link, the price is the same — we may earn a small commission
(see <a href="/disclosure">Disclosure</a>).</p></div>
<div class="sub">
<h3>Are prices accurate?</h3>
<p>Prices are pulled live from Amazon and are indicative. Always confirm the price on Amazon before
ordering — deals change constantly.</p></div>
<div class="sub">
<h3>Can you pick a niche for me?</h3>
<p>We publish niches continuously. Want one you don't see?
<a href="/contact">Contact us</a> — we read everything.</p></div>
</section>
<script src="/courier.js" defer></script>
</main>
""".encode("utf-8")
    return head + body + _footer()


def _variant_key(n):
    """Stable short content key for a saved niche that changes when its
    products/source refresh, so the /n/ render cache invalidates on update."""
    items = n.get("products") or []
    stub = "%s|%s|%s|%s|%s" % (
        n.get("keyword", ""), n.get("source", ""), n.get("updated", ""),
        len(items),
        (items[0].get("asin") if items else "") if items else "")
    return hashlib.md5(stub.encode("utf-8")).hexdigest()[:8]


def render_niche(keyword, niche, saved_niches=None, ab_headline=None, ab_variant=0,
                 style_pack=None):
    """Crawlable niche page in the answer-first review layout: breadcrumbs,
    byline, human intro, ranked picks with honest pros/cons, comparison table,
    methodology + trust, FAQ, related niches."""
    items = niche.get("products") or []
    canonical = "/n/" + _slugify(keyword)
    title = "Best %s to Buy — Ranked Picks From Live Amazon Data" % keyword
    desc = (f"See the best {keyword}, ranked. We score live Amazon listings on "
            f"rating, review volume and price, then show you which to buy and "
            f"why — with honest pros and cons for each.")
    best = editorial.best_pick(items)
    ranked = "".join(
        editorial.pick_html(keyword, it, idx, items)
        for idx, it in enumerate(score_order(items)))
    graph = _product_graph(items, BASE_URL + canonical, _slugify(keyword))
    il = editorial.item_list_jsonld(items, keyword)
    if il:
        graph.append(il)
    if best:
        graph.append(editorial.faq_jsonld(keyword, best))
        graph.append(editorial.breadcrumb_jsonld(keyword))
    graph.append(_org_jsonld())
    jsonld = {"@context": "https://schema.org", "@graph": graph}
    og = BASE_URL + "/og/" + _slugify(keyword) + ".png"
    head = _head(title, desc, canonical, canonical, jsonld=jsonld, og_image=og,
                 noindex=not bool(items))
    headline = ab_headline or ("Best %s: ranked picks" % keyword)
    ab_attr = (' data-variant="%s"' % ab_variant) if ab_variant else ""
    banner_slot = (style_pack or {}).get("banner") or ""
    style_slot = (style_pack or {}).get("css") or ""
    masthead = _masthead([("Home", "/", False), ("Blog", "/blog", False),
                          ("Stories", "/stories", False),
                          ("One-pager", "/lp/" + _slugify(keyword), False),
                          ("Disclosure", "/disclosure", False)],
                         cta=("#courier", "Get the free guide"))
    body = f"""
{masthead}
{banner_slot}{style_slot}
<main data-niche="{_clean(_slugify(keyword))}" data-source="niche" data-keyword="{_clean(keyword)}"{ab_attr} data-tag="{_clean(amazon.AFFILIATE_TAG)}">
<div class="card">
  {editorial.breadcrumbs_html(keyword)}
  <h1>{_clean(headline)}</h1>
  {editorial.byline_html(keyword, items, best) if best else ""}
  <p class="lede">{_clean(editorial.intro(keyword, items))}</p>
  {editorial.trust_block_html()}
  {editorial.urgency_html()}
  {editorial.faq_html(keyword, best) if best else ""}
  <h2>The ranked list</h2>
  {ranked}
  {editorial.upsell_block(items, keyword)}
  {lead_gate_html(keyword, "niche") if items else ""}
  {editorial.comparison_html(items, keyword) if items else ""}
  {editorial.methodology_html()}
  {editorial.related_html(keyword, saved_niches) if saved_niches else ""}
</div>
{editorial.sticky_cta_html(keyword, best)}
{editorial.STICKY_CSS}
<script>
(function(){{
var s=document.querySelector(".sticky-cta");var t=null;var gate=document.querySelector('[data-role="upsell"]');
function show(){{document.body.classList.add("show-sticky");}}
function on(){{
if(!s)return;
var n=window.scrollY||document.documentElement.scrollTop;
var m=gate?gate.getBoundingClientRect().top:2000;
if(n>420||m<innerHeight){{show();}}
else{{document.body.classList.remove("show-sticky");}}
}}
window.addEventListener("scroll",function(){{clearTimeout(t);t=setTimeout(on,120);}},{{passive:true}});
window.addEventListener("load",on);on();
/* exit-intent: cursor leaving the top of the viewport nudges the CTA even pre-scroll */
document.documentElement.addEventListener("mouseout",function(e){{
if(s&&!e.relatedTarget&&(e.clientY||0)<=0&&!document.body.classList.contains("show-sticky")){{
show();
if(!document.body.getAttribute("data-opted")){{
var o=document.querySelector("form.courier");
if(o){{o.scrollIntoView({{behavior:"smooth",block:"center"}});
var f=o.querySelector("[name=email]");if(f)setTimeout(function(){{f.focus();}},500);}}
}}
}}
}});
}})();
</script>
{optin_html(keyword, "niche", anchor="courier")}
<script src="/courier.js" defer></script>
<script src="/table-flow.js" defer></script>
</main>
""".encode("utf-8")
    return head + body + _footer()


def score_order(items):
    return [it for it, _s in editorial.score_items(items)]


def render_topic(term, parent_keyword, niche, parent_slug, style_pack=None):
    """Long-tail page (/n/<parent>/<term>): reframes the parent niche's ranked
    picks around a related autosuggest term so Google sees a distinct intent.
    Shares the niche's product data (still relevant), URL-canonical for the term,
    and links back to the parent hub. Each generated term is a unique indexable
    URL — the core of the aggressive long-tail play."""
    items = niche.get("products") or []
    term_slug = _slugify(term)
    canonical = "/n/%s/%s" % (_slugify(parent_keyword), term_slug)
    title = "Best %s — Ranked From Live Amazon Data" % (term or parent_keyword)
    desc = (f"Looking for the best {term}? The same live Amazon scoring — rating, "
            f"review volume, price — that ranks the parent {parent_keyword} guide "
            f"now points you straight at the top products for this {term} list.")
    graph = []
    il = editorial.item_list_jsonld(items, term or parent_keyword)
    if il:
        graph.append(il)
    graph.extend(_product_graph(items, BASE_URL + canonical, term_slug))
    best = editorial.best_pick(items)
    if best:
        graph.append(editorial.faq_jsonld(term or parent_keyword, best))
        graph.append(editorial.breadcrumb_jsonld(term or parent_keyword,
                                                 parent_keyword))
    graph.append(_org_jsonld())
    jsonld = {"@context": "https://schema.org", "@graph": graph}
    og = BASE_URL + "/og/" + (term_slug or _slugify(parent_keyword)) + ".png"
    head = _head(title, desc, canonical, canonical, jsonld=jsonld, og_image=og,
                 noindex=not bool(items))
    ranked = "".join(editorial.pick_html(term or parent_keyword, it, idx, items)
                     for idx, it in enumerate(score_order(items)))
    hub = "/n/%s" % _slugify(parent_keyword)
    banner_slot = (style_pack or {}).get("banner") or ""
    style_slot = (style_pack or {}).get("css") or ""
    masthead = _masthead([("Home", "/", False), ("Blog", "/blog", False),
                          (_clean(parent_keyword.title()), hub, False),
                          ("Disclosure", "/disclosure", False)],
                         cta=("#courier", "Get the free guide"))
    body = f"""
{masthead}
{banner_slot}{style_slot}
<main data-niche="{_clean(term_slug)}" data-source="topic" data-keyword="{_clean(term or parent_keyword)}" data-tag="{_clean(amazon.AFFILIATE_TAG)}">
<div class="card">
  {editorial.breadcrumbs_html(term or parent_keyword)}
  <h1>Best {_clean(term or parent_keyword)}</h1>
  <p class="lede">You searched for the best {_clean(term or parent_keyword)}. Here are the same products our
  {_clean(parent_keyword)} guide ranks — scored live on rating, review volume and price.</p>
  {editorial.trust_block_html()}
  <h2>Top {_clean(term or parent_keyword)} picks</h2>
  {ranked}
  {editorial.upsell_block(items, term or parent_keyword)}
  {lead_gate_html(term or parent_keyword, "topic") if items else ""}
  {editorial.comparison_html(items, term or parent_keyword) if items else ""}
  {editorial.methodology_html()}
  <p class="hint">This is a focused sub-topic of our <a href="{_clean(hub)}">full {_clean(parent_keyword)} guide</a>.</p>
</div>
{optin_html(term or parent_keyword, "niche", anchor="courier")}
<script src="/courier.js" defer></script>
<script src="/table-flow.js" defer></script>
</main>
""".encode("utf-8")
    return head + body + _footer()


def _num_price(item):
    """Numeric price of an item, or None when it isn't a positive number."""
    p = (item or {}).get("price")
    if isinstance(p, (int, float)):
        return p if p > 0 else None
    try:
        f = float(p)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def render_priceband(amount, parent_keyword, parent_slug, items,
                     currency=None):
    """/n/<parent>/under-<amount> — the parent niche's ranked set reframed on a
    hard budget: only items priced at or below $<amount>. Shoppers searching
    "<kw> under $X" get a tight, indexable shortlist instead of the full guide.
    Shares the niche's live data; noindex when nothing fits the band."""
    amount = int(amount)
    band = [it for it in (items or [])
            if (_num_price(it) is not None and _num_price(it) <= amount)]
    term_label = "%s under $%s" % (parent_keyword, amount)
    canonical = "/n/%s/under-%d" % (parent_slug, amount)
    title = "Best %s under $%s — ranked from live Amazon data" \
        % (parent_keyword, "{:,}".format(amount))
    desc = (f"Looking for the best {parent_keyword} under ${amount:,}? "
            f"Same live scoring as the full guide — rating, review volume, "
            f"price — now filtered to what actually fits your budget.")
    graph = []
    il = editorial.item_list_jsonld(band, term_label)
    if il:
        graph.append(il)
    graph.extend(_product_graph(band, BASE_URL + canonical, "under-%d" % amount))
    best = editorial.best_pick(band)
    if best:
        graph.append(editorial.faq_jsonld(term_label, best))
        graph.append(editorial.breadcrumb_jsonld(term_label, parent_keyword))
    graph.append(_org_jsonld())
    jsonld = {"@context": "https://schema.org", "@graph": graph}
    og = BASE_URL + "/og/" + _slugify(parent_keyword) + ".png"
    head = _head(title, desc, canonical, canonical, jsonld=jsonld, og_image=og,
                 noindex=not bool(band))
    ranked = "".join(editorial.pick_html(term_label, it, idx, band)
                     for idx, it in enumerate(score_order(band)))
    hub = "/n/%s" % (parent_slug or _slugify(parent_keyword))
    masthead = _masthead([("Home", "/", False), ("Blog", "/blog", False),
                          (_clean(parent_keyword.title()), hub, False),
                          ("Disclosure", "/disclosure", False)],
                         cta=("#courier", "Get the free guide"))
    body = f"""
{masthead}
<main data-niche="{_clean(_slugify(parent_keyword))}" data-source="topic" data-keyword="{_clean(term_label)}" data-tag="{_clean(amazon.AFFILIATE_TAG)}">
<div class="card">
  {editorial.breadcrumbs_html(term_label)}
  <h1>Best {_clean(parent_keyword)} under ${amount:,}</h1>
  <p class="lede">Your budget said “${amount:,}” — so this list skips everything pricier and keeps
  the {_clean(parent_keyword)} picks that score highest within it. Live prices: a pick that climbs past the
  cap gets replaced the moment the data refreshes.</p>
  {editorial.trust_block_html()}
  <h2>Top {_clean(parent_keyword)} picks under ${amount:,}</h2>
  {ranked}
  {editorial.upsell_block(band, term_label)}
  {lead_gate_html(term_label, "priceband") if band else ""}
  {editorial.comparison_html(band, parent_keyword) if band else ""}
  {editorial.methodology_html()}
  <p class="hint">This is a budget slice of our <a href="{_clean(hub)}">full {_clean(parent_keyword)} guide</a>.</p>
</div>
{optin_html(term_label, "topic", anchor="courier")}
<script src="/courier.js" defer></script>
<script src="/table-flow.js" defer></script>
</main>
""".encode("utf-8")
    return head + body + _footer()


def render_vs(title_a, title_b, a_asin, b_asin, parent_keyword, parent_slug,
              items, currency=None):
    """/n/<parent>/<a>-vs-<b> — a head-to-head between the two ranked picks.
    Both candidates are compared outright (what to choose, and when not to),
    so "<a> or <b>" searches land on a page that actually answers the split.
    Resolves the two products from the current niche data by ASIN."""
    by_asin = {}
    for it in (items or []):
        a = (it.get("asin") or "").strip().upper()
        if a:
            by_asin[a] = it
    cand = [by_asin[a_asin], by_asin[b_asin]] \
        if a_asin in by_asin and b_asin in by_asin else []
    term_label = "%s vs %s" % (title_a or a_asin, title_b or b_asin)
    canonical = "/n/%s/%s-vs-%s" % (parent_slug, _slugify(title_a or a_asin),
                                    _slugify(title_b or b_asin))
    title = "%s vs %s — which %s wins" % (title_a or a_asin,
                                          title_b or b_asin, parent_keyword)
    desc = (f"{title_a} or {title_b} for {parent_keyword}? Head-to-head verdict "
            f"from live Amazon price, rating and review data.")
    graph = []
    il = editorial.item_list_jsonld(cand, term_label)
    if il:
        graph.append(il)
    graph.extend(_product_graph(cand, BASE_URL + canonical, "vs"))
    best = editorial.best_pick(cand)
    if best:
        graph.append(editorial.faq_jsonld(term_label, best))
        graph.append(editorial.breadcrumb_jsonld(term_label, parent_keyword))
    graph.append(_org_jsonld())
    jsonld = {"@context": "https://schema.org", "@graph": graph}
    og = BASE_URL + "/og/" + _slugify(parent_keyword) + ".png"
    head = _head(title, desc, canonical, canonical, jsonld=jsonld, og_image=og,
                 noindex=not bool(cand))
    ranked = "".join(editorial.pick_html(term_label, it, idx, cand)
                     for idx, it in enumerate(score_order(cand)))
    hub = "/n/%s" % (parent_slug or _slugify(parent_keyword))
    masthead = _masthead([("Home", "/", False), ("Blog", "/blog", False),
                          (_clean(parent_keyword.title()), hub, False),
                          ("Disclosure", "/disclosure", False)],
                         cta=("#courier", "Get the free guide"))
    body = f"""
{masthead}
<main data-niche="{_clean(_slugify(parent_keyword))}" data-source="topic" data-keyword="{_clean(term_label)}" data-tag="{_clean(amazon.AFFILIATE_TAG)}">
<div class="card">
  {editorial.breadcrumbs_html(term_label)}
  <h1>{_clean(title_a or a_asin)} vs {_clean(title_b or b_asin)}</h1>
  <p class="lede">Choosing between two good {_clean(parent_keyword)} picks? Here's the head-to-head
  from our live data — the winner, and exactly who the runner-up is still the right answer for.</p>
  {editorial.trust_block_html()}
  <h2>The verdict</h2>
  {ranked}
  {lead_gate_html(term_label, "vs") if cand else ""}
  {editorial.comparison_html(cand, parent_keyword) if cand else ""}
  {editorial.methodology_html()}
  <p class="hint">Part of our <a href="{_clean(hub)}">full {_clean(parent_keyword)} guide</a>.</p>
</div>
{optin_html(term_label, "topic", anchor="courier")}
<script src="/courier.js" defer></script>
<script src="/table-flow.js" defer></script>
</main>
""".encode("utf-8")
    return head + body + _footer()


def indexable_urls(saved_niches, base_url=None, saved_topics=None):
    """Absolute URLs that belong in the sitemap + IndexNow submissions.

    Mirrors render_sitemap() but returns ready-to-submit absolute URLs.
    saved_topics: iterable of (parent_slug, slug) for built long-tail pages.
    """
    base = (base_url or BASE_URL).rstrip("/")
    urls = [base + "/", base + "/blog", base + "/stories"]
    for page in STATIC_PAGES:
        urls.append(base + "/" + page)
    seen = set()
    for n in (saved_niches or []):
        main = base + "/n/" + _slugify(n["keyword"])
        if main in seen:
            continue  # "back pain" vs "back-pain" slugify identically
        seen.add(main)
        urls.append(main)
        urls.append(base + "/lp/" + _slugify(n["keyword"]))
        urls.append(base + "/stories/" + _slugify(n["keyword"]))
    for p_slug, t_slug in (saved_topics or []):
        urls.append("%s/n/%s/%s" % (base, p_slug, t_slug))
    return urls


def render_blog(saved_niches, page=1, per_page=BLOG_PAGE_SIZE):
    """Public /blog landing: index of editorial articles, one per saved niche.
    Each card links to the full ranked notebook (/n/<slug>) and is SEO-shaped
    (title/desc/canonical + indexable). Cards are paginated (BLOG_PAGE_SIZE per
    page) so the page stays light even with hundreds of niches: page 1 holds the
    newest cards and deep pages are noindex,follow with prev/next navigation.
    The JSON-LD blogPost array appears only on page 1, capped to the newest
    handful, while the sitemap still carries the full listing."""
    niches = [n for n in (saved_niches or []) if n.get("products")]
    niches.sort(key=lambda n: (n.get("created_at") or ""), reverse=True)
    total_pages = max(1, -(-len(niches) // per_page))
    page = max(1, int(page or 1))
    page = min(page, total_pages)
    page_niches = niches[(page - 1) * per_page:page * per_page]
    articles = []
    if page == 1:
        for n in niches[:12]:
            slug = _slugify(n["keyword"])
            best = editorial.best_pick(n["products"])
            title = "The best %s: a ranked, data-backed pick" % n["keyword"]
            synopsis = (best or {}).get("title") or n["keyword"]
            articles.append({
                "@type": "BlogPosting",
                "headline": title,
                "description": synopsis[:160],
                "image": BASE_URL + "/og/" + slug + ".png",
                "url": BASE_URL + "/n/" + slug,
                "datePublished": (n.get("created_at") or "")[:10],
                "dateModified": (n.get("created_at") or "")[:10],
                "author": {"@type": "Organization", "name": SITE_NAME},
                "publisher": {"@type": "Organization", "name": SITE_NAME},
            })
    jsonld = None
    if articles:
        jsonld = {"@context": "https://schema.org", "@type": "Blog",
                  "name": SITE_NAME, "url": BASE_URL + "/blog",
                  "blogPost": articles}
    page_url = "/blog" if page <= 1 else "/blog?p=%d" % page
    rel_prev_next = ""
    if total_pages > 1 and page > 1:
        rel_prev_next += ('<link rel="prev" href="%s/blog?p=%d">'
                          % (BASE_URL, page - 1))
    if total_pages > 1 and page < total_pages:
        rel_prev_next += ('<link rel="next" href="%s/blog?p=%d">'
                          % (BASE_URL, page + 1))
    head = _head("The blog", "Ranked buying guides, data methodology and honest picks, niche by niche.",
                 page_url, page_url, noindex=page > 1 or len(niches) == 0,
                 jsonld=jsonld, og_image=BASE_URL + "/og/blog.png",
                 extra=rel_prev_next)
    cards = ""
    for n in page_niches:
        slug = _slugify(n["keyword"])
        best = editorial.best_pick(n["products"])
        title = "The best %s: a ranked, data-backed pick" % n["keyword"]
        synopsis = (best or {}).get("title") or n["keyword"]
        cards += f"""
<article class="card">
  <img class="card-img" src="{_clean(BASE_URL)}/og/{_clean(slug)}.png" alt="{_clean(n['keyword'])} picks" loading="lazy">
  <div class="blog-body">
    <a class="blog-title" href="/n/{_clean(slug)}"><h2>{_clean(title)}</h2></a>
    <p class="hint">Top pick · {_clean(synopsis[:90])}{"…" if len(synopsis) > 90 else ""}</p>
    <p class="muted">{editorial.reading_minutes(n["keyword"], n["products"], best)} read · {len(n["products"] or 0)} products ranked from live Amazon data</p>
  </div>
</article>"""
    if not cards:
        cards = '<section class="card"><h2>Fresh guides on the way</h2><p class="hint">We\'re ranking new niches now. Check back soon or <a href="/">browse the picks</a>.</p></section>'
    nav = ""
    if total_pages > 1:
        prev_link = ('<a class="blog-prev" href="/blog?p=%d" rel="prev">&larr; Newer</a>'
                     % (page - 1)) if page > 1 else ""
        next_link = ('<a class="blog-next" href="/blog?p=%d" rel="next">Older &rarr;</a>'
                     % (page + 1)) if page < total_pages else ""
        nav = ('<nav class="blog-pager" style="display:flex;gap:16px;margin:24px 0">'
               "%s<span class=\"pager-page\" style=\"flex:1;text-align:center\">Page %d of %d</span>%s</nav>"
               % (prev_link, page, total_pages, next_link))
    masthead = _masthead([("Home", "/", False), ("Niches", "/#niches", False),
                          ("Blog", "/blog", True), ("Stories", "/stories", False)],
                         cta=("/#top-picks", "Today's top picks"))
    body = f"""{masthead}
<main data-niche="blog" data-source="blog" data-tag="{_clean(amazon.AFFILIATE_TAG)}">
<section class="hero-home">
  <p class="eyebrow">The blog</p>
  <h1>Every guide is a <em>data-backed ranking</em>.</h1>
  <p class="hero-sub">Live price, rating and review signals decide the pick for each niche — honest
  methodology, verified before you click, and no filler anywhere.</p>
</section>
{cards}
{nav}</main>
""".encode("utf-8")
    return head + body + _footer()


def story_cards(keyword, niche, base_url=None):
    """Slides for one niche's story reel: a cover card + one card per ranked
    product (image, live price, honest take, tagged link). Shared by the
    /stories/<slug> page and the /stories gallery so both stay consistent."""
    items = ((niche.get("products") or []) if isinstance(niche, dict)
             else list(niche or []))
    items = score_order(items) if items else []
    base = (base_url or BASE_URL).rstrip("/")
    slides = [{
        "kind": "cover",
        "keyword": keyword,
        "count": len(items),
        "title": "Best %s" % keyword,
        "sub": "Ranked from live Amazon price, rating + review data.",
        "img": base + "/og/" + _slugify(keyword) + ".png",
    }]
    for i, item in enumerate(items, 1):
        price = item.get("price")
        have_price = price not in (None, "") and not (
            isinstance(price, (int, float)) and float(price) <= 0)
        slides.append({
            "kind": "product",
            "position": i,
            "title": editorial._display(item, keyword),
            "stars": item.get("stars"),
            "reviews": item.get("reviews"),
            "price": price if have_price else None,
            "url": item.get("url") or amazon.affiliate_url(item.get("asin") or "") or "",
            "img": item.get("image") or (base + "/og/" + _slugify(keyword) + ".png"),
        })
    return slides


def _story_slide_html(slide):
    if slide.get("kind") == "cover":
        kw = (slide.get("keyword") or "").strip() or "picks"
        count = int(slide.get("count") or 0)
        if count:
            sub = ("We did the homework: %d picks ranked from live Amazon price, "
                   "rating and review signals — no guesswork, no paid placements, "
                   "just the ones we would buy." % count)
        else:
            sub = ("Ranked from live Amazon price, rating and review signals — "
                   "no guesswork, no paid placements, just the ones we would buy.")
        return ('<section class="story-slide story-cover">'
                '<div class="story-inner">'
                '<p class="story-kicker">The %s story</p>'
                '<h1>Stop scrolling — the <em>%s</em> shortlist you can actually trust.</h1>'
                '<p class="story-sub">%s</p>'
                '<p class="story-trust"><span>Live price data</span>'
                '<span>Independent ranking</span><span>Verified buy links</span></p>'
                '</div></section>'
                % (_clean(kw.upper()), _clean(kw), _clean(sub)))
    price = ("%s" % slide["price"]) if slide.get("price") is not None else "check price"
    rating = ""
    if slide.get("stars") and slide.get("reviews"):
        rating = ('<p class="story-rating">%s / 5 from %s reviews</p>'
                  % (round(float(slide["stars"]), 1),
                     format(int(slide["reviews"]), ",")))
    img = (slide.get("img") or "").strip()
    img_html = ('<img src="%s" alt="%s" loading="lazy">' % (_clean(img),
                _clean(slide.get("title", "")))) if img else ""
    url = slide.get("url") or ""
    cta = ('<a class="story-cta" href="%s" rel="nofollow sponsored noopener">'
           'See it on Amazon \u2192</a>' % _clean(url)) if url else ""
    return ('<section class="story-slide">'
            '<div class="story-img">%s</div>'
            '<div class="story-inner"><p class="story-rank">Slot %s</p>'
            '<h2>%s</h2>%s<p class="story-price">%s</p>%s</div></section>'
            % (img_html, slide.get("position", ""),
               _clean(slide.get("title", "")), rating,
               _clean(price), cta))


def story_listitem(pos, slide):
    """One ItemList entry. A product only gets @type Product when it can be
    marked up completely (price + rating + review count — same rule as the
    guide pages); otherwise it stays a plain, valid ListItem."""
    item = {}
    price = slide.get("price")
    have_price = price not in (None, "") and not (
        isinstance(price, (int, float)) and float(price) <= 0)
    stars, reviews = slide.get("stars"), slide.get("reviews")
    title = slide.get("title") or ""
    if have_price and stars and reviews:
        item = {"@type": "Product", "name": title}
        img = slide.get("img") or ""
        if img:
            item["image"] = img
        item["offers"] = {
            "@type": "Offer",
            "price": price if isinstance(price, (int, float)) else str(price),
            "priceCurrency": "USD",
            "availability": "https://schema.org/InStock",
        }
        if slide.get("url"):
            item["offers"]["url"] = slide["url"]
        item["aggregateRating"] = {
            "@type": "AggregateRating",
            "ratingValue": round(float(stars), 1),
            "reviewCount": int(reviews),
            "bestRating": 5,
            "worstRating": 1,
        }
    entry = {"@type": "ListItem", "position": pos}
    if item:
        entry["item"] = item
    else:
        entry["name"] = title
    return entry


def _ldjson_product_issues(node):
    """Google's Product/Offer field rules, split into errors and warnings.
    Errors = the exact diagnostics Search Console raised (an Offer serialized
    without price, priceCurrency or availability, or a malformed rating) —
    these make the item ineligible. Warnings = Google's enhancement notes (a
    product with zero offers, or no aggregateRating because the source data
    has none): valid, but no Product rich snippet is earned. Returns
    (errors, warnings)."""
    errors, warnings = [], []
    name = node.get("name")
    if not name or not str(name).strip():
        errors.append("name missing")
    offers = node.get("offers")
    if isinstance(offers, dict):
        if offers.get("price") in (None, ""):
            errors.append("offers.price missing")
        if not offers.get("priceCurrency"):
            errors.append("offers.priceCurrency missing")
        if not offers.get("availability"):
            errors.append("offers.availability missing")
    elif not offers:
        warnings.append("no offers emitted (unpriced product — reviews-only snippet)")
    else:
        errors.append("offers empty")
    rating = node.get("aggregateRating")
    if not isinstance(rating, dict) or not rating.get("ratingValue"):
        if "aggregateRating" in node:
            errors.append("aggregateRating malformed")
        else:
            warnings.append("no aggregateRating (unrated product — no review snippet)")
    return errors, warnings


def _ldjson_node_issues(node):
    """Validate any JSON-LD node the site emits against Google's minimal
    required fields for its @type, split into errors (invalid / not eligible)
    and warnings (enhancement notes). Product nodes use the Offer/rating rules
    above; the supporting types the site ships (ItemList, FAQPage,
    BreadcrumbList, WebSite, Organization) get their own required-field checks
    so a regression in ANY structured data shows up on the dashboard before
    Search Console reports it."""
    t = (node or {}).get("@type")
    if t == "Product":
        return _ldjson_product_issues(node)
    errors, warnings = [], []
    if t in ("ItemList", "BreadcrumbList"):
        items = node.get("itemListElement")
        if not isinstance(items, list) or not items:
            errors.append("%s.itemListElement missing or empty" % t)
        else:
            for it in items:
                if not isinstance(it, dict):
                    continue
                nm = str(it.get("name") or "").strip()
                if not nm and isinstance(it.get("item"), dict):
                    nm = str(it["item"].get("name") or "").strip()
                if not nm:
                    errors.append("%s item without name" % t)
    elif t == "FAQPage":
        main = node.get("mainEntity")
        if not isinstance(main, list) or not main:
            errors.append("FAQPage.mainEntity missing or empty")
        else:
            for q in main:
                if not str(q.get("name") or "").strip():
                    errors.append("FAQPage question without name")
    elif t in ("Organization", "WebSite"):
        if not str(node.get("name") or "").strip():
            errors.append("%s.name missing" % t)
        if not node.get("url"):
            errors.append("%s.url missing" % t)
    return errors, warnings


def audit_jsonld(niche):
    """Validate the structured data the site actually emits for a niche —
    guide page graph, landing node and story reel — against Google's required
    fields for every schema type the site ships, with no network. Product
    nodes are checked against the Offer/price/rating rules Search Console
    raised; ItemList / FAQPage / BreadcrumbList / Organization / WebSite get
    their own required-field checks. Errors = what the console reports;
    warnings = Google's enhancement notes. Coverage counters tell the operator
    why products go unmarked (titleless / unpriced / unrated) so they can fix
    the data, not just the markup."""
    kw = (niche or {}).get("keyword") or ""
    prods = (niche or {}).get("products") or []
    slug = _slugify(kw)
    pages = []
    errors, warnings = [], []
    invalid = 0
    covered = eligible = skipped_title = skipped_price = skipped_rating = 0

    def check_page(kind, url, nodes):
        nonlocal invalid
        nodes = list(nodes)
        for node in (n for n in nodes if isinstance(n, dict)):
            if node.get("@type") == "ItemList":
                # Products inside an ItemList live at
                # itemListElement[].item — count and validate them too or a
                # story reel full of marked products reports 0 nodes.
                for entry in node.get("itemListElement") or []:
                    item = (entry or {}).get("item")
                    if isinstance(item, dict):
                        nodes.append(item)
        prod_nodes = [n for n in nodes
                      if isinstance(n, dict) and n.get("@type") == "Product"]
        types = {}
        for node in (n for n in nodes if isinstance(n, dict)):
            t = node.get("@type")
            types[t] = types.get(t, 0) + 1
            node_errors, node_warnings = _ldjson_node_issues(node)
            prefix = "%s %s (%s)" % (kind, url, t)
            for e in node_errors:
                invalid += 1
                errors.append("%s — %s" % (prefix, e))
            for w in node_warnings:
                warnings.append("%s — %s" % (prefix, w))
        pages.append({"kind": kind, "url": url,
                      "nodes": len(prod_nodes), "types": types})

    # Guide page: the full graph exactly as render_niche assembles it —
    # ranked Products, an ItemList of ranked picks, FAQ, breadcrumb, org.
    graph = _product_graph(prods)
    il = editorial.item_list_jsonld(prods, kw)
    if il:
        graph.append(il)
    best = editorial.best_pick(prods) if prods else None
    if best:
        graph.append(editorial.faq_jsonld(kw, best))
        graph.append(editorial.breadcrumb_jsonld(kw))
    graph.append(_org_jsonld())
    check_page("guide", "/n/" + slug, graph)
    covered = len([n for n in graph if isinstance(n, dict)
                   and n.get("@type") == "Product"])
    for it in (prods or [])[:10]:
        if not it.get("title"):
            skipped_title += 1
            continue
        price = it.get("price")
        have_price = price not in (None, "") and not (
            isinstance(price, (int, float)) and float(price) <= 0)
        if not have_price:
            skipped_price += 1
        elif not it.get("stars") or not it.get("reviews"):
            skipped_rating += 1
        else:
            eligible += 1

    # Landing sales page (top pick) — same rule, price-gated offers.
    if best:
        landing = landing_product_jsonld(
            best, "%s/lp/%s" % (BASE_URL, slug))
        if isinstance(landing, dict):
            check_page("landing", "/lp/" + slug, landing.get("@graph") or [])

    # Story reel — the ItemList of story_listitem entries (incomplete picks
    # stay plain ListItems), tracked by its breadcrumb + org nodes.
    slides = story_cards(kw, niche)
    story_entries = [story_listitem(p, s)
                     for p, s in enumerate(slides, 1)
                     if s.get("kind") == "product"]
    check_page("story", "/stories/" + slug,
               [{"@type": "ItemList",
                 "name": "Best %s — story" % kw,
                 "itemListElement": story_entries},
                editorial.breadcrumb_jsonld(kw), _org_jsonld()])

    return {
        "ok": invalid == 0,
        "pages": pages,
        "errors": errors,
        "warnings": warnings,
        "node_count": sum(p["nodes"] for p in pages),
        "invalid": invalid,
        "covered": covered,
        "eligible": eligible,
        "skipped_title": skipped_title,
        "skipped_price": skipped_price,
        "skipped_rating": skipped_rating,
    }


def render_story(niche, keyword=None):
    """Full-reel /stories/<slug> page: a vertical, swipeable (scroll-snap)
    story of the niche — the same data as the ranked page, in a format built
    for thumb-first readers on social and links shared from the /stories reel.
    Indexable and canonical to itself."""
    keyword = (keyword or (niche or {}).get("keyword") or "picks").strip()
    slug = _slugify(keyword)
    canonical = "/stories/" + slug
    slides = story_cards(keyword, niche)
    slides_html = "".join(_story_slide_html(s) for s in slides)
    desc = "Swipe the %s story: ranked picks from live Amazon price, rating and review data." % keyword

    jsonld = {"@context": "https://schema.org", "@graph": [
        {"@type": "ItemList", "name": "Best %s — story" % keyword,
         "itemListElement": [
             story_listitem(p, s)
             for p, s in enumerate(slides, 1) if s.get("kind") == "product"]},
        editorial.breadcrumb_jsonld(keyword),
        _org_jsonld(),
    ]}
    head = _head("Best %s — the story" % keyword, desc, canonical, canonical,
                 jsonld=jsonld,
                 og_image=BASE_URL + "/og/" + slug + ".png")
    body = f"""
{_masthead([("All stories", "/stories", False), ("Full guide", "/n/" + _clean(slug), False)],
           cta=("/stories", "All stories"))}
<main data-niche="{_clean(slug)}" data-source="story" data-tag="{_clean(amazon.AFFILIATE_TAG)}">
<div class="story-reel">{slides_html}
{optin_html(keyword, "story", anchor="courier")}</div>
</main>
<style>.story-reel{{height:86vh;overflow-y:auto;scroll-snap-type:y proximity;border-radius:20px;margin:6px 0;scrollbar-width:thin}}
.story-slide{{min-height:86vh;display:flex;flex-direction:column;justify-content:flex-start;
scroll-snap-align:start;background:#fff;border:1px solid var(--line);border-radius:20px;margin-bottom:16px;
overflow:hidden;box-sizing:border-box;position:relative;
box-shadow:0 10px 34px rgba(16,19,27,.08)}}
.story-slide .story-img{{position:relative;flex:1 1 auto;min-height:0;display:flex;
align-items:center;justify-content:center;background:linear-gradient(180deg,#eef6f8,#ffffff);
border-bottom:1px solid var(--line);box-sizing:border-box}}
.story-slide .story-img img{{width:100%;height:100%;object-fit:contain;padding:26px;box-sizing:border-box;display:block}}
.story-slide .story-inner{{position:relative;z-index:auto;padding:20px 26px 26px}}
.story-cover{{justify-content:center;background-image:linear-gradient(-20deg,#0d1117 0%,#12242e 55%,#0f8b9d 100%)!important}}
.story-cover .story-inner{{color:#fff;text-align:center;max-width:640px;margin:0 auto;padding:44px 26px}}
.story-kicker{{letter-spacing:.3em;font-size:11px;font-weight:800;opacity:.85;text-transform:uppercase}}
.story-cover h1{{font-size:clamp(28px,5vw,42px);line-height:1.14;margin:12px 0 10px;letter-spacing:-.02em}}
.story-cover h1 em{{font-family:Georgia,serif;font-style:italic;font-weight:500;color:#8fe0ea}}
.story-sub{{opacity:.92;max-width:520px;margin:0 auto;font-size:15px;line-height:1.6}}
.story-trust{{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin:18px 0 0}}
.story-trust span{{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
color:#dceff4;border:1px solid rgba(255,255,255,.25);background:rgba(255,255,255,.07);
padding:6px 12px;border-radius:999px}}
.story-rank{{font-weight:800;color:var(--accent-deep,#0a7da0);letter-spacing:.12em;font-size:12px}}
.story-inner h2{{font-size:22px;line-height:1.3;margin:6px 0}}
.story-rating{{color:var(--muted);font-size:14px;margin:0 0 8px}}
.story-price{{font-size:28px;font-weight:900;letter-spacing:-.02em;color:var(--ink);margin:10px 0 14px}}
.story-cta{{display:inline-block;background:linear-gradient(135deg,var(--grad-warm-from),var(--grad-warm-to));
color:#1c1305;font-weight:800;text-decoration:none;padding:12px 24px;border-radius:999px;
box-shadow:0 8px 20px rgba(232,148,10,.35)}}
.story-slide form.courier{{position:relative;z-index:2;border-radius:18px}}
@media (max-width:560px){{.story-slide{{min-height:74vh}}.story-slide .story-img img{{padding:18px}}
.story-slide .story-inner{{padding:16px 18px 20px}}.story-cover .story-inner{{padding:30px 18px}}}}</style>
<script src="/courier.js" defer></script>
<script src="/ui.js" defer></script>
""".encode("utf-8")
    return head + body + _footer()


def render_stories_gallery(saved_niches):
    """/stories listing — one reel per saved niche with a real product set."""
    niches = [n for n in (saved_niches or []) if n.get("products")]
    desc = "Swipeable stories of every ranked niche — the best picks, by price, rating and review data."
    jsonld = {"@context": "https://schema.org", "@type": "CollectionPage",
              "name": "pstore stories", "description": desc,
              "hasPart": [
                  {"@type": "WebPage",
                   "url": BASE_URL + "/stories/" + _slugify(n["keyword"]),
                   "name": "Best %s — the story" % n["keyword"]}
                  for n in niches]}
    head = _head("Stories", desc, "/stories", "/stories", jsonld=jsonld,
                 noindex=len(niches) == 0, og_image=BASE_URL + "/og/home.png")
    cards = ""
    for n in niches:
        slug = _slugify(n["keyword"])
        cnt = len(n.get("products") or [])
        cards += (f'<a class="card story-card" href="/stories/{_clean(slug)}">'
                  f'<div class="blog-body"><h2>Best {_clean(n["keyword"])} <span class="hint">· {cnt} picks</span></h2>'
                  f'</div>'
                  f'<img src="{_clean(BASE_URL)}/og/{_clean(slug)}.png" alt="{_clean(n["keyword"])}" loading="lazy">'
                  f'</a>')
    if not cards:
        cards = ('<section class="card"><h2>Fresh stories on the way</h2>'
                 '<p class="hint">We are ranking new niches now — the reel fills up as guides ship. '
                 '<a href="/">Browse the picks</a>.</p></section>')
    masthead = _masthead([("Home", "/", False), ("Niches", "/#niches", False),
                          ("Blog", "/blog", False), ("Stories", "/stories", True)],
                         cta=("/#top-picks", "Today's top picks"))
    body = f"""{masthead}
<main data-niche="stories" data-source="stories" data-tag="{_clean(amazon.AFFILIATE_TAG)}">
<section class="hero-home"><p class="eyebrow">The stories reel</p>
<h1>Every ranked guide, <em>made swipeable</em>.</h1>
<p class="hero-sub">Verdict up top, live price per pick, honest takes. Fast to share, easy to read.</p></section>
<div class="features" style="align-items:stretch">{cards}</div>
</main>
""".encode("utf-8")
    return head + body + _footer()


def render_sitemap(entries):
    """entries: list of (url_path, lastmod). Returns sitemap.xml bytes."""
    urls = "".join(
        f"<url><loc>{BASE_URL}{_clean(p)}</loc><lastmod>{lm}</lastmod></url>\n"
        for p, lm in entries)
    body = '<?xml version="1.0" encoding="UTF-8"?>\n' + SITEMAP_XSL_PI + '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + urls + "</urlset>\n"
    return body.encode("utf-8")


def _rfc2822(sqlite_dt):
    """'YYYY-MM-DD HH:MM:SS' (UTC) -> RFC 2822 pubDate. Falls back to epoch."""
    try:
        dt = datetime.strptime((sqlite_dt or "")[:19], "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
    except Exception:
        return "Thu, 01 Jan 1970 00:00:00 +0000"


def _clean_xml(text):
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


RSS_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<rss version="2.0" '
    'xmlns:atom="http://www.w3.org/2005/Atom" '
    'xmlns:media="http://search.yahoo.com/mrss/">\n'
    "<channel>\n"
    f"<title>{_clean_xml('pstore — fresh buying-guide picks')}</title>\n"
    f"<link>{BASE_URL}</link>\n"
    f"<description>{_clean_xml('Curated affiliate niche picks and buying guides from ' + BASE_URL)}</description>\n"
    f'<atom:link href="{BASE_URL}/rss.xml" rel="self" type="application/rss+xml"/>\n'
    "<language>en-us</language>\n"
    "<ttl>60</ttl>\n"
)


def render_rss(items):
    """items: list of dicts {title, link, description, image, pubdate}.
    RSS 2.0 with a raster (PNG) enclosure per item so feed readers and
    Pinterest's native RSS auto-publisher find an image on every post."""
    body = RSS_HEAD
    for it in items:
        g = _clean_xml(it["link"])
        t = _clean_xml(it.get("title") or it["link"])
        d = _clean_xml(it.get("description") or "")
        img = _clean_xml(it.get("image") or "")
        p = _rfc2822(it.get("pubdate") or "")
        body += "<item>\n"
        body += f"<title>{t}</title>\n"
        body += f"<link>{g}</link>\n"
        body += f'<guid isPermaLink="true">{g}</guid>\n'
        body += f"<pubDate>{p}</pubDate>\n"
        body += f"<description>{d}</description>\n"
        if img:
            body += f'<enclosure url="{img}" type="image/png"/>\n'
            body += f'<media:thumbnail url="{img}"/>\n'
        body += "</item>\n"
    body += "</channel>\n</rss>\n"
    return body.encode("utf-8")


SITEMAP_XSL_PI = '<?xml-stylesheet type="text/xsl" href="/sitemap.xsl"?>\n'


def sitemap_xsl():
    """Brower-only stylesheet so a raw sitemap view wraps and scrolls on small
    screens. Crawlers ignore the XSL processing instruction entirely."""
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<xsl:stylesheet version=\"1.0\" xmlns:xsl=\"http://www.w3.org/1999/XSL/Transform\">\n"
            "<xsl:output method=\"html\" encoding=\"UTF-8\" indent=\"yes\"/>\n"
            "<xsl:template match=\"/\">\n"
            "<html><head><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/><style>\n"
            "body{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;background:#fafafa;margin:0;padding:14px}\n"
            ".sitemap-wrap{overflow-x:auto;max-width:100%}\n"
            "table{border-collapse:collapse;width:100%}\n"
            "td,th{padding:6px 8px;text-align:left;vertical-align:top;word-break:break-all;border-bottom:1px solid #e3e3e3}\n"
            "th{background:#eee}\n"
            "a{color:#1a0dab;text-decoration:none}\n"
            "p.hint{color:#888}\n"
            "</style></head><body>\n"
            "<div class=\"sitemap-wrap\">\n"
            "<p class=\"hint\">Sitemap · <xsl:value-of select=\"count(//*[local-name()='url'])\"/> URLs</p>\n"
            "<table><tr><th>URL</th><th>Lastmod</th></tr>\n"
            "<xsl:for-each select=\"//*[local-name()='url']\">\n"
            "<tr><td><a href=\"{*[local-name()='loc']}\"><xsl:value-of select=\"*[local-name()='loc']\"/></a></td>"
            "<td><xsl:value-of select=\"*[local-name()='lastmod']\"/></td></tr>\n"
            "</xsl:for-each>\n"
            "</table></div></body></html>\n"
            "</xsl:template>\n"
            "</xsl:stylesheet>\n").encode("utf-8")


def render_robots():
    return (f"User-agent: *\nAllow: /\n"
            f"Disallow: /admin\nDisallow: /tool\nDisallow: /keys\n"
            f"Disallow: /dashboard\nDisallow: /api/\nDisallow: /e/\n"
            f"Disallow: /e/o\nDisallow: /social/\nDisallow: /_gated/\n"
            f"Allow: /og/*.png\nDisallow: /og/\n"
            f"Sitemap: {BASE_URL}/sitemap.xml\n").encode("utf-8")


# ------------------------------------------------------------------ audit
# Structural, self-serve SEO audit used by /admin/seo and /api/seo-audit.
# Everything is computed from the actual page a niche would render, so the
# checkboxes reflect the live site rather than aspirational settings.

def _meta_lengths(items):
    """Title + description character counts for a niche page (rule of thumb:
    titles 30–60, descriptions 70–160)."""
    keyword = (items or {}).get("keyword") or ""
    title = "Best %s to Buy — Ranked Picks From Live Amazon Data" % keyword
    desc = (f"See the best {keyword}, ranked. We score live Amazon listings on "
            f"rating, review volume and price, then show you which to buy and "
            f"why — with honest pros and cons for each.")
    return len(title), len(desc)


def _words(items):
    """Rough prose word count for the niche page body (audit only)."""
    prods = (items or {}).get("products") or []
    kw = (items or {}).get("keyword") or ""
    if not prods:
        return 0
    n = len(editorial.intro(kw, prods).split()) + 120
    best = editorial.best_pick(prods)
    n += editorial.reading_minutes(kw, prods, best) * 190
    return n


def _slugify_safe(text):
    try:
        return _slugify(text)
    except Exception:
        return "niche"


def audit_niche(niche):
    """One row of the SEO audit for a saved niche (no network)."""
    kw = niche.get("keyword") or ""
    slug = _slugify_safe(kw)
    prods = niche.get("products") or []
    best = editorial.best_pick(prods) if prods else None
    tl, dl = _meta_lengths(niche)
    wc = _words(niche)
    ld = audit_jsonld(niche)
    checks = {
        "has_products": bool(prods),
        "title_ok": 30 <= tl <= 60,
        "desc_ok": 70 <= dl <= 160,
        "og_image": bool(best),
        "schema": ld["ok"],
        "word_count": wc >= 300,
    }
    return {
        "keyword": kw, "slug": slug,
        "url": "/n/" + slug,
        "products": len(prods),
        "top_asin": (best or {}).get("asin") or "",
        "title_len": tl, "desc_len": dl,
        "words": wc,
        "checks": checks,
        "ldjson": ld,
        "indexable": bool(prods) and checks["title_ok"] and checks["desc_ok"],
    }


def audit_sites(niches):
    """Global audit summary for the /admin/seo header strip + config status."""
    rows = [audit_niche(n) for n in (niches or [])]
    passable = sum(1 for r in rows if r["indexable"])
    ld_nodes = sum((r.get("ldjson") or {}).get("node_count", 0) for r in rows)
    ld_invalid = sum((r.get("ldjson") or {}).get("invalid", 0) for r in rows)
    ld_skipped = sum(
        (r.get("ldjson") or {}).get("skipped_title", 0) +
        (r.get("ldjson") or {}).get("skipped_price", 0) +
        (r.get("ldjson") or {}).get("skipped_rating", 0) for r in rows)
    return {
        "niches": rows,
        "count": len(rows),
        "indexable": passable,
        "needs_work": len(rows) - passable,
        "site_url": BASE_URL,
        "google_verification": bool(google_site_verification()),
        "pinterest_verification": bool(pinterest_site_verification()),
        "sitemap": "/sitemap.xml",
        "robots": "/robots.txt",
        "org": {"name": ORG_NAME, "url": ORG_URL},
        "ldjson_nodes": ld_nodes,
        "ldjson_invalid": ld_invalid,
        "ldjson_skipped": ld_skipped,
    }


def render_snippet(keyword, desc="", url=""):
    """A Google-style SERP snippet preview for one page — the operator's own
    'what will searchers see' check before publishing. Returns an SVG card
    (stdlib-only, cacheable) so it renders in any admin flow without JS."""
    kw = _clean(keyword)
    title = "Best %s to Buy — Ranked Picks From Live Amazon Data" % kw
    if len(title) > 60:
        title = title[:57] + "…"
    url = url or ("%s/n/%s" % (BASE_URL, _slugify_safe(kw)))
    desc = _clean(desc or ("See the best %s, ranked. We score live Amazon listings on "
                           "rating, review volume and price, then show you which to buy and "
                           "why — with honest pros and cons for each." % kw))
    if len(desc) > 160:
        desc = desc[:157].rstrip() + "…"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="640" height="150" viewBox="0 0 640 150">
<rect width="640" height="150" fill="#ffffff" rx="10"/>
<text x="18" y="30" font-family="Arial,Helvetica,sans-serif" font-size="13" fill="#1a0dab">{_clean(url)}</text>
<text x="18" y="58" font-family="Arial,Helvetica,sans-serif" font-weight="700" font-size="20" fill="#1a0dab">{_clean(title)}</text>
<text x="18" y="86" font-family="Arial,Helvetica,sans-serif" font-size="14" fill="#4d5156">{_clean(desc[:110])}</text>
<text x="18" y="108" font-family="Arial,Helvetica,sans-serif" font-size="14" fill="#4d5156">{_clean(desc[110:])}</text>
<text x="18" y="136" font-family="Arial,Helvetica,sans-serif" font-size="12" fill="#70757a">sample · keyword “{_clean(kw)}”</text>
</svg>""".encode("utf-8")


def audit_topic(term, parent_keyword, prods):
    """SEO audit row for a long-tail topic page (/n/<parent>/<term>). Returns
    None-safe check dict mirroring audit_niche() for the topic angle."""
    kw = ("%s %s" % (parent_keyword or "", term or "")).strip()
    slug = _slugify_safe("%s %s" % (parent_keyword or "", term or ""))
    title = "Best %s — ranked picks from live Amazon data" % (kw or "picks")
    desc = ("See the best %s, ranked. Live data, honest pros and cons." % kw) if kw else ""
    tl, dl = len(title), len(desc)
    return {
        "keyword": kw, "slug": slug,
        "url": "/n/%s/%s" % (_slugify_safe(parent_keyword), _slugify_safe(term)),
        "products": len(prods or []),
        "title_len": tl, "desc_len": dl,
        "words": 0,
        "checks": {
            "has_products": bool(prods),
            "title_ok": 30 <= tl <= 60,
            "desc_ok": 20 <= dl <= 160,
            "schema": bool(prods),
            "indexable": bool(prods) and 30 <= tl <= 60,
        },
        "indexable": bool(prods) and 30 <= tl <= 60,
    }
