# -*- coding: utf-8 -*-
"""End-to-end traffic doctor.

Answers, from the software itself, the only question that matters when a site is
live but silent: *why is nothing indexed and nothing clicking?* It crawls the
live origin like a search-engine bot, samples real URLs straight out of the
sitemap, checks that each page is actually indexable, verifies the IndexNow key
file the engines fetch before they trust a submission, reports which search
consoles are connected (and states the hard truth that **IndexNow never reaches
Google**), and reads the social delivery state.

Everything bottoms out in the module-level `_fetch` so offline tests inject fake
responses (same convention as amazon._urlopen / webmaster._req). Never raises:
a dead origin becomes a `fail` check, not an exception.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_INFO = "info"

_LOC_RX = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_TITLE_RX = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.S)
_CANON_RX = re.compile(
    r'<link[^>]*\brel=["\']?canonical["\']?[^>]*\bhref=["\']([^"\']+)["\']',
    re.IGNORECASE)
_NOINDEX_RX = re.compile(r'<meta[^>]*name=["\']?robots["\']?[^>]*'
                         r'content=["\']?[^"\'>]*noindex', re.IGNORECASE)
_TG_MARK = b"data-pstore-tg-join"
_FREE_HOSTS = (".onrender.com", ".vercel.app", ".netlify.app", ".herokuapp.com",
               ".glitch.me", ".fly.dev")


def _default_fetch(url, timeout=15):
    """GET a URL as a crawler. Returns (status, headers, body_bytes). A network
    failure returns (None, {}, b"error: ..."), never raises."""
    try:
        req = urllib.request.Request(url, method="GET", headers={
            "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; "
                          "+http://www.google.com/bot.html)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(400000)
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return getattr(resp, "status", 200), headers, data
    except urllib.error.HTTPError as e:
        try:
            body = e.read(40000)
        except Exception:
            body = b""
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body
    except Exception as exc:
        return None, {}, ("error: %s: %s" % (type(exc).__name__, exc))[:200].encode()


def _check(cid, area, title, status, detail, fix=""):
    return {"id": cid, "area": area, "title": title, "status": status,
            "detail": detail, "fix": fix}


def _text(body):
    if isinstance(body, bytes):
        return body.decode("utf-8", "replace")
    return body or ""


def _crawl_checks(base, fetch):
    """robots.txt + sitemap.xml: the two files every crawler reads first.
    Returns (checks, sitemap_urls)."""
    checks = []
    urls = []

    status, _h, body = fetch(base + "/robots.txt")
    txt = _text(body)
    if status != 200:
        checks.append(_check("robots", "crawl", "robots.txt reachable", STATUS_FAIL,
                             "robots.txt returned %s" % (status or "no response"),
                             "Serve a 200 text/plain robots.txt; a broken robots "
                             "file alone can stop Google crawling the whole site."))
    else:
        has_sitemap = "sitemap:" in txt.lower()
        has_admin = "disallow: /admin" in txt.lower()
        if has_sitemap and has_admin:
            checks.append(_check("robots", "crawl", "robots.txt reachable", STATUS_OK,
                                 "200; points at the sitemap and blocks /admin"))
        else:
            missing = []
            if not has_sitemap:
                missing.append("no Sitemap: line")
            if not has_admin:
                missing.append("no Disallow: /admin")
            checks.append(_check("robots", "crawl", "robots.txt reachable", STATUS_WARN,
                                 "200, but " + ", ".join(missing),
                                 "Add a `Sitemap: %s/sitemap.xml` line so crawlers "
                                 "find every URL." % base))

    status, _h, body = fetch(base + "/sitemap.xml")
    txt = _text(body)
    urls = _LOC_RX.findall(txt)
    if status != 200:
        checks.append(_check("sitemap", "crawl", "sitemap.xml valid", STATUS_FAIL,
                             "sitemap.xml returned %s" % (status or "no response"),
                             "Expose a valid XML sitemap with every public URL."))
    elif not urls:
        checks.append(_check("sitemap", "crawl", "sitemap.xml valid", STATUS_FAIL,
                             "200 but no <loc> URLs parsed",
                             "Regenerate the sitemap so it lists your real pages."))
    else:
        checks.append(_check("sitemap", "crawl", "sitemap.xml valid", STATUS_OK,
                             "%d URLs listed" % len(urls)))

    host = urlsplit_host(base)
    if host and host.endswith(_FREE_HOSTS):
        checks.append(_check(
            "domain", "crawl", "Custom domain", STATUS_WARN,
            "Serving on the free host %s" % host,
            "Google treats free hostname subdomains as low-trust; a custom domain "
            "is the single biggest indexing lever at this page count."))
    return checks, urls


def urlsplit_host(base):
    try:
        return urllib.parse.urlsplit(base).hostname or ""
    except Exception:
        return ""


def _page_check(url, fetch):
    status, _h, body = fetch(url)
    path = urllib.parse.urlsplit(url).path or "/"
    if status != 200:
        return _check("page:" + path, "pages", "Indexable page " + path, STATUS_FAIL,
                      "returned %s" % (status or "no response"),
                      "A non-200 sample URL is not indexable. Check the route and "
                      "the sitemap entry.")
    txt = _text(body)
    if _NOINDEX_RX.search(txt):
        return _check("page:" + path, "pages", "Indexable page " + path, STATUS_FAIL,
                      "serves <meta robots noindex>",
                      "Remove noindex from public money pages — the single most "
                      "common reason 'Google indexed 0 pages'.")
    canon = (_CANON_RX.search(txt) or [None, ""])[1]
    title = re.sub(r"\s+", " ", (_TITLE_RX.search(txt) or [None, ""])[1]).strip()
    problems = []
    if not canon:
        problems.append("no canonical")
    elif canon.rstrip("/") != url.rstrip("/"):
        problems.append("canonical → %s" % canon)
    if not title:
        problems.append("no <title>")
    if len(txt) < 1500:
        problems.append("thin body (%d bytes)" % len(txt))
    if problems:
        return _check("page:" + path, "pages", "Indexable page " + path, STATUS_WARN,
                      "; ".join(problems),
                      "Each page needs a unique <title> and a self-referencing "
                      "canonical.")
    return _check("page:" + path, "pages", "Indexable page " + path, STATUS_OK,
                  "200 · self-canonical · %d bytes" % len(txt))


def _engine_checks(engines, indexnow_key, base, fetch):
    checks = []
    engines = engines or []
    by = {str(e.get("engine")): e for e in engines}

    gsc = by.get("gsc") or {}
    if gsc.get("state") == "ready":
        checks.append(_check("gsc", "google", "Google Search Console connected",
                             STATUS_OK,
                             "Token ready — sitemap submit + URL inspection available."))
    else:
        checks.append(_check(
            "gsc", "google", "Google Search Console connected", STATUS_FAIL,
            "state: %s" % (gsc.get("state") or "not configured"),
            "IndexNow does NOT reach Google — only Search Console does. Verify the "
            "property (the google-site-verification meta is already on every page) "
            "and submit the sitemap in Search Console, or set PSTORE_GSC_CLIENT_ID/"
            "PSTORE_GSC_CLIENT_SECRET and Connect on /admin/seoengines."))

    for eng, label in (("bing", "Bing"), ("yandex", "Yandex")):
        e = by.get(eng) or {}
        if e.get("state") == "ready":
            checks.append(_check(eng, "engines", "%s console connected" % label,
                                 STATUS_OK, "submission + stats available"))
        else:
            checks.append(_check(eng, "engines", "%s console connected" % label,
                                 STATUS_WARN,
                                 "state: %s" % (e.get("state") or "not configured"),
                                 "Connect %s so you can submit the sitemap and see "
                                 "impressions directly." % label))

    if not indexnow_key:
        checks.append(_check("indexnow", "engines", "IndexNow key", STATUS_FAIL,
                             "no IndexNow key configured",
                             "Set a 32-hex INDEXNOW_KEY so IndexNow pings fire."))
        return checks
    status, _h, body = fetch("%s/%s.txt" % (base, indexnow_key))
    served = _text(body).strip().lower()
    if status == 200 and served == indexnow_key.lower():
        checks.append(_check(
            "indexnow", "engines", "IndexNow key file served", STATUS_OK,
            "/%s.txt returns the key — pings to Bing/Yandex/DuckDuckGo/Seznam/Naver "
            "are trusted." % indexnow_key))
    else:
        checks.append(_check(
            "indexnow", "engines", "IndexNow key file served", STATUS_FAIL,
            "/%s.txt returned %s (body %r)" % (indexnow_key, status or "no response",
                                               served[:40]),
            "IndexNow engines fetch this key file before trusting a submission. "
            "If it 404s, every ping is silently discarded."))
    return checks


def _social_checks(social):
    checks = []
    social = social or {}
    webhook = bool(social.get("webhook"))
    native = [p for p in (social.get("native") or []) if p]
    if webhook or native:
        detail = []
        if webhook:
            detail.append("SOCIAL_WEBHOOK set")
        if native:
            detail.append("native keys: " + ", ".join(native))
        checks.append(_check("social-channel", "social", "Social delivery channel",
                             STATUS_OK, "; ".join(detail)))
    else:
        checks.append(_check(
            "social-channel", "social", "Social delivery channel", STATUS_FAIL,
            "no webhook and no native platform keys — posts are queued but never sent",
            "Set SOCIAL_WEBHOOK (point an n8n/Make webhook at "
            "/api/social/webhook) or paste platform keys on /admin/apikeys."))
    platforms = social.get("platforms") or {}
    if platforms:
        ready = sorted(p for p, ok in platforms.items() if ok)
        missing = sorted(p for p, ok in platforms.items() if not ok)
        detail = ("ready: " + (", ".join(ready) if ready else "none") +
                  " · missing: " + (", ".join(missing) if missing else "none"))
        status = STATUS_OK if not missing else (STATUS_WARN if ready else STATUS_FAIL)
        fix = ("Paste the missing platform keys on /admin/apikeys (or route them "
               "through SOCIAL_WEBHOOK). Every missing platform is a channel that "
               "never sees your posts." if missing else "")
        checks.append(_check("social-platforms", "social",
                             "Native platform credentials", status, detail, fix))
    stats = social.get("webhook_stats") or {}
    if webhook and int(stats.get("fail") or 0) > 0 and not stats.get("last_ok"):
        checks.append(_check(
            "social-webhook", "social", "Webhook last result", STATUS_FAIL,
            "%d failed, last error: %s" % (int(stats.get("fail") or 0),
                                           stats.get("err") or "unknown"),
            "The webhook URL is dead or rejecting the payload — fix it on "
            "/admin/apikeys."))
    counts = social.get("counts") or {}
    if counts:
        checks.append(_check(
            "social-counts", "social", "Social queue", STATUS_INFO,
            "%d published · %d scheduled · %d draft" % (
                int(counts.get("published") or 0),
                int(counts.get("scheduled") or 0),
                int(counts.get("draft") or 0)),
            "Draft counts climbing with no published rows means nothing is actually "
            "posting."))
    return checks


def run(base, sample=4, fetch=None, engines=None, indexnow_key="", social=None):
    """Run every check and return an ordered, fix-first report.

    Returns {ok, verdict, blockers, warnings, checks, sitemap_size, sampled}.
    `verdict` is 'fail' when any check failed, else 'warn' when any warned,
    else 'ok'. Checks are ordered fail → warn → info → ok so the panel leads
    with what to fix."""
    fetch = fetch or _default_fetch
    base = (base or "").rstrip("/")
    checks, sitemap_urls = _crawl_checks(base, fetch)
    checks.append(_page_check(base + "/", fetch))
    picks = [u for u in sitemap_urls if u.rstrip("/") != base][:max(0, int(sample))]
    for u in picks:
        checks.append(_page_check(u, fetch))
    checks.extend(_engine_checks(engines, indexnow_key, base, fetch))
    checks.extend(_social_checks(social))

    order = {STATUS_FAIL: 0, STATUS_WARN: 1, STATUS_INFO: 2, STATUS_OK: 3}
    checks.sort(key=lambda c: order.get(c["status"], 4))
    fails = [c for c in checks if c["status"] == STATUS_FAIL]
    warns = [c for c in checks if c["status"] == STATUS_WARN]
    verdict = "fail" if fails else ("warn" if warns else "ok")
    return {"ok": True, "verdict": verdict,
            "blockers": len(fails), "warnings": len(warns),
            "sitemap_size": len(sitemap_urls), "sampled": len(picks) + 1,
            "checks": checks}


def to_json(report):
    return json.dumps(report, indent=1)
