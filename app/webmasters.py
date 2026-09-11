# -*- coding: utf-8 -*-
"""Search-engine webmaster integrations: Google Search Console, Bing Webmaster,
Yandex Webmaster. Stdlib-only. Covers verification-agnostic plumbing that the
/admin/seoengines hub and the per-engine dashboards call:

  Google  - OAuth2 (offline refresh token) -> Search Analytics + sitemap status
  Bing    - API key -> keyword/page stats + sitemap submit
  Yandex  - OAuth2 -> host search-queries summary

Transport is `_req`, module-level so tests replace it with a fake (same
convention as amazon._urlopen). Tokens/persisted keys are stored through the
module-level STORE_GET / STORE_SET hooks, wired by server.py to its settings
table, so nothing ever touches disk here.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import seo

# ------------------------------------------------------------------ hooks
_STORE_GET = None   # callable(key, default="") -> str
_STORE_SET = None   # callable(key, value) -> None  ("" clears)
_req = None         # set to the real _http_req below; tests override


def _set_transport(fn):
    global _req
    _req = fn


def _default_req(method, url, headers=None, body=None, timeout=25):
    """Return (status_or_None, parsed_json_or_str). Never raises."""
    req = urllib.request.Request(
        url,
        data=None if body is None else
        (body if isinstance(body, (bytes, str)) else json.dumps(body).encode()),
        method=method,
        headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode("utf-8", "replace") or "null")
            except (ValueError, TypeError):
                return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = (e.read() or b"").decode("utf-8", "replace")
        try:
            return e.code, (json.loads(raw) if raw.strip() else {})
        except ValueError:
            return e.code, raw
    except OSError as exc:
        return None, str(exc)[:300]


_req = _default_req


def _hdr(**kw):
    h = {"Content-Type": "application/json; charset=utf-8"}
    h.update(kw)
    return h


# ------------------------------------------------------------------ store
def store_get(key, default=""):
    return _STORE_GET(key, default) if _STORE_GET else default


def store_set(key, value):
    if _STORE_SET:
        _STORE_SET(key, value)


def _json_get(key, default=None):
    raw = store_get(key, "")
    try:
        return json.loads(raw) if raw else default
    except ValueError:
        return default


def _json_set(key, value):
    store_set(key, json.dumps(value))


# ------------------------------------------------------------------ config
GSC_CLIENT_ID = os.environ.get("PSTORE_GSC_CLIENT_ID", "")
GSC_CLIENT_SECRET = os.environ.get("PSTORE_GSC_CLIENT_SECRET", "")
BING_API_KEY = os.environ.get("PSTORE_BING_API_KEY", "")
YANDEX_CLIENT_ID = os.environ.get("PSTORE_YANDEX_CLIENT_ID", "")
YANDEX_CLIENT_SECRET = os.environ.get("PSTORE_YANDEX_CLIENT_SECRET", "")

GSC_TOKEN_URL = "https://oauth2.googleapis.com/token"
GSC_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GSC_API = "https://searchconsole.googleapis.com"
GSC_SCOPE = "https://www.googleapis.com/auth/webmasters"
GSC_INSPECT_DAY_LIMIT = 200  # Google's URL Inspection API quota
GSC_INSPECT_DAY_MARGIN = 10  # stay under the hard cap
BING_API = "https://ssl.bing.com/webmaster/api.svc/json"
YANDEX_OAUTH = "https://oauth.yandex.ru"
YANDEX_API = "https://api.webmaster.yandex.net/v3"

TOKEN_SETTINGS = {"gsc": "seoeng.gsc.token", "bing": "seoeng.bing.token",
                  "yandex": "seoeng.yandex.token"}
SITE_SETTINGS = {"gsc": "seoeng.gsc.site", "bing": "seoeng.bing.site",
                 "yandex": "seoeng.yandex.site"}


def host_of():
    """The bare hostname the property is registered under (no scheme)."""
    h = urllib.parse.urlsplit(seo.BASE_URL).hostname or "pstore-gxbv.onrender.com"
    return h.rstrip(".").lower()


def site_url():
    return seo.BASE_URL.rstrip("/")


def gsc_site_id(host=None):
    """GSC resource id for the property. We use the URL-prefix form
    (https://host/) which can be verified on a Render *.onrender.com
    subdomain; a domain property (sc-domain:host) needs DNS control we
    do not have there."""
    return "https://%s/" % (host or host_of())


def engines_status():
    """Per-engine readiness + last-sync info. No network calls."""
    rows = []
    gtok = _json_get(TOKEN_SETTINGS["gsc"])
    rows.append({
        "engine": "gsc", "name": "Google Search Console",
        "client": bool(GSC_CLIENT_ID and GSC_CLIENT_SECRET),
        "token": bool(gtok and gtok.get("refresh_token")),
        "site": gsc_site_id(),
        "state": ("ready" if (GSC_CLIENT_ID and GSC_CLIENT_SECRET and gtok
                              and gtok.get("refresh_token")) else
                  ("consent-given" if gtok else
                   ("needs-client" if not (GSC_CLIENT_ID and GSC_CLIENT_SECRET)
                    else "needs-consent"))),
    })
    bkey = BING_API_KEY or store_get("seoeng.bing.apikey", "")
    rows.append({
        "engine": "bing", "name": "Bing Webmaster",
        "client": bool(bkey),
        "token": False, "site": site_url(),
        "state": "ready" if bkey else "needs-key",
    })
    ytok = _json_get(TOKEN_SETTINGS["yandex"])
    rows.append({
        "engine": "yandex", "name": "Yandex Webmaster",
        "client": bool(YANDEX_CLIENT_ID and YANDEX_CLIENT_SECRET),
        "token": bool(ytok and ytok.get("access_token")),
        "site": host_of(),
        "state": ("ready" if (YANDEX_CLIENT_ID and YANDEX_CLIENT_SECRET
                              and ytok and ytok.get("access_token")) else
                  ("needs-client" if not (YANDEX_CLIENT_ID and YANDEX_CLIENT_SECRET)
                   else "needs-consent")),
    })
    for r in rows:
        last = _json_get("seoeng.%s.last" % r["engine"])
        r["last_sync"] = (last or {}).get("at") if last else None
    return rows


# ------------------------------------------------------------------ Google
def gsc_auth_url(state):
    if not (GSC_CLIENT_ID and GSC_CLIENT_SECRET):
        return ""
    redir = seo.BASE_URL.rstrip("/") + "/admin/oauth/seoengines/cb/gsc"
    return ("%s?client_id=%s&redirect_uri=%s&response_type=code&scope=%s"
            "&access_type=offline&prompt=consent&state=%s"
            % (GSC_AUTH_URL, urllib.parse.quote(GSC_CLIENT_ID, safe=""),
               urllib.parse.quote(redir, safe=""),
               urllib.parse.quote(GSC_SCOPE, safe=""),
               urllib.parse.quote(state, safe="")))


def gsc_exchange(code):
    """Exchange the consent code for tokens and persist them."""
    redir = seo.BASE_URL.rstrip("/") + "/admin/oauth/seoengines/cb/gsc"
    status, data = _req("POST", GSC_TOKEN_URL, _hdr(),
                        {"code": code, "client_id": GSC_CLIENT_ID,
                         "client_secret": GSC_CLIENT_SECRET,
                         "redirect_uri": redir,
                         "grant_type": "authorization_code"})
    if status != 200 or not isinstance(data, dict) or not data.get("refresh_token"):
        return False, (str(data)[:200] if data else "exchange failed")
    data["expires_at"] = int(time.time()) + int(data.get("expires_in", 3600))
    _json_set(TOKEN_SETTINGS["gsc"], data)
    return True, "connected"


def gsc_refresh():
    tok = _json_get(TOKEN_SETTINGS["gsc"])
    if not tok or not tok.get("refresh_token"):
        return None
    status, data = _req("POST", GSC_TOKEN_URL, _hdr(),
                        {"grant_type": "refresh_token",
                         "refresh_token": tok.get("refresh_token"),
                         "client_id": GSC_CLIENT_ID,
                         "client_secret": GSC_CLIENT_SECRET})
    if status != 200 or not isinstance(data, dict) or not data.get("access_token"):
        return None
    data["refresh_token"] = tok.get("refresh_token")
    data["expires_at"] = int(time.time()) + int(data.get("expires_in", 3600))
    _json_set(TOKEN_SETTINGS["gsc"], data)
    return data.get("access_token")


def _gsc_bearer():
    tok = _json_get(TOKEN_SETTINGS["gsc"])
    if not tok:
        return None
    if int(tok.get("expires_at", 0)) < int(time.time()) + 90:
        return gsc_refresh()
    return tok.get("access_token")


def _gsc(path, method="GET", body=None):
    bearer = _gsc_bearer()
    if not bearer:
        return None, {"error": "google not connected"}
    url = GSC_API + path.replace(" ", "%20")
    return _req(method, url, _hdr(Authorization="Bearer " + bearer),
                body=body)


def gsc_performance(days=28, site=None):
    """Search Analytics for the property. Returns (ok, {rows, totals})."""
    site = site or gsc_site_id()
    now = time.time()
    end = time.strftime("%Y-%m-%d", time.gmtime(now))
    start = time.strftime("%Y-%m-%d", time.gmtime(now - days * 86400))
    status, data = _gsc(
        "/webmasters/v3/sites/%s/searchAnalytics/query" % urllib.parse.quote(site, safe=":"),
        "POST",
        {"startDate": start, "endDate": end, "dimensions": ["page"],
         "rowLimit": 25, "type": "web"})
    if status != 200 or not isinstance(data, dict):
        return False, {"error": str(data)[:200], "rows": []}
    rows = [{
        "page": (r.get("keys") or ["/"])[0],
        "clicks": int(r.get("clicks") or 0),
        "impressions": int(r.get("impressions") or 0),
        "ctr": round((r.get("ctr") or 0) * 100, 1),
        "position": round((r.get("position") or 0), 1),
    } for r in data.get("rows") or []]
    totals = {
        "clicks": sum(r["clicks"] for r in rows),
        "impressions": sum(r["impressions"] for r in rows),
        "ctr": (round(sum(r["clicks"] for r in rows) / max(
            sum(r["impressions"] for r in rows), 1) * 100, 1)),
        "position": (round(sum(r["position"] for r in rows) / max(len(rows), 1), 1)),
        "days": days,
    }
    return True, {"rows": rows, "totals": totals}


def gsc_submit_sitemap(site=None, sitemap=None):
    site = site or gsc_site_id()
    sitemap = (sitemap or site_url() + "/sitemap.xml").strip()
    feed = urllib.parse.urlsplit(sitemap).path.lstrip("/") or "sitemap.xml"
    status, data = _gsc(
        "/webmasters/v3/sites/%s/sitemaps/%s"
        % (urllib.parse.quote(site, safe=":"), urllib.parse.quote(feed, safe="")),
        "PUT", body={})
    if status in (200, 204):
        return True, "submitted"
    return False, ("already-submitted" if status == 400 else str(data)[:200])


def gsc_sitemap_status(site=None):
    site = site or gsc_site_id()
    status, data = _gsc(
        "/webmasters/v3/sites/%s/sitemaps" % urllib.parse.quote(site, safe=":"))
    if status != 200 or not isinstance(data, dict):
        return (False, str(data)[:200]) if status != 200 else (True, [])
    out = []
    for s in (data.get("sitemap") or []):
        out.append({"path": s.get("path", ""), "last_submitted":
                    s.get("contents", [{}])[0].get("submitted", "")
                    if s.get("contents") else "",
                    "last_downloaded":
                    s.get("contents", [{}])[0].get("lastDownloaded", "")
                    if s.get("contents") else "",
                    "is_pending": s.get("isPending", False)})
    return True, out


def url_inspect(url, site=None):
    """Request Google re-crawl one URL via the URL Inspection API (the closest
    thing to an instant index push). Needs the write scope, so the consent
    flow must have granted webmasters (see GSC_SCOPE)."""
    site = site or gsc_site_id()
    body = {"inspectionUrl": (url or "").strip(), "siteUrl": site}
    status, data = _gsc("/webmasters/v3/urlInspection/index/inspect",
                        "POST", body)
    return status, data


_GSC_DAY_KEY = "seoeng.gsc.inspect"


def _inspect_budget(days=1):
    """URLs still available today under the day quota. Persisted via the
    settings store so every process/slot shares the same daily budget."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    raw = store_get(_GSC_DAY_KEY, "")
    try:
        used = json.loads(raw) or {}
    except Exception:
        used = {}
    if used.get("date") != today:
        used = {"date": today, "used": 0}
        store_set(_GSC_DAY_KEY, json.dumps(used))
        return GSC_INSPECT_DAY_LIMIT - GSC_INSPECT_DAY_MARGIN
    return max(0, GSC_INSPECT_DAY_LIMIT - GSC_INSPECT_DAY_MARGIN - int(used.get("used", 0)))


def _consume_inspect(n):
    today = time.strftime("%Y-%m-%d", time.gmtime())
    raw = store_get(_GSC_DAY_KEY, "")
    try:
        used = json.loads(raw) or {}
    except Exception:
        used = {}
    if used.get("date") != today:
        used = {"date": today, "used": 0}
    used["used"] = int(used.get("used", 0)) + max(0, int(n))
    store_set(_GSC_DAY_KEY, json.dumps(used))


def gsc_submit_sitemap_daily(site=None):
    """Submit /sitemap.xml to Google at most once per day (GSC is slow to
    register changes anyway; IndexNow + URL inspection cover new URLs)."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if store_get("seoeng.gsc.sitemap", "") == today:
        return False
    if not _gsc_bearer():
        return False
    ok, _ = gsc_submit_sitemap(site=site)
    if ok:
        store_set("seoeng.gsc.sitemap", today)
    return ok


def inspect_new(url, site=None):
    """Budget-aware single-URL inspect: a no-op (returns None) when Google is
    unconnected, GSC crawls are disabled, or the daily quota is spent. Returns
    True only when the inspection request was actually issued."""
    if store_get("seoeng.gsc.enabled", "1") != "1":
        return None
    if _inspect_budget() <= 0:
        return None
    try:
        if not _gsc_bearer():
            return None
        _gsc("/webmasters/v3/urlInspection/index/inspect", "POST",
             {"inspectionUrl": (url or "").strip(),
              "siteUrl": site or gsc_site_id()})
        _consume_inspect(1)
    except Exception:
        return None
    return True


def gsc_crawl(urls, site=None, cap=None):
    """Best-effort Google re-crawl for a batch of URLs: submits the sitemap
    once, then requests inspection for each new URL, capped by the daily quota.
    Returns {ok, submitted, inspected, skipped, total}. Never raises."""
    urls = [u for u in (urls or []) if isinstance(u, str) and u.startswith("http")]
    if not urls:
        return {"ok": False, "submitted": False, "inspected": 0,
                "skipped": len(urls or []), "total": 0}
    out = {"ok": False, "submitted": False, "inspected": 0, "skipped": 0,
           "total": len(urls)}
    submitted_ok, _ = gsc_submit_sitemap(site=site)
    out["submitted"] = submitted_ok
    for u in urls:
        if cap is not None and out["inspected"] >= cap:
            break
        if inspect_new(u, site=site):
            out["inspected"] += 1
    out["skipped"] = max(0, len(urls) - out["inspected"])
    out["ok"] = out["inspected"] > 0 or out["submitted"]
    return out
def bing_add_site(key, url):
    return _req("POST", BING_API + "/AddSite", _hdr(WebmasterAPI=key),
                {"siteUrl": url})


def bing_submit_sitemap(key, url, sitemap=None):
    sitemap = sitemap or url.rstrip("/") + "/sitemap.xml"
    return _req("POST", BING_API + "/SubmitSitemap", _hdr(WebmasterAPI=key),
                {"siteUrl": url, "sitemapUrl": sitemap})


def bing_stats(key, url, days=28):
    """Bing keyword/page stats. GET artifact varies by endpoint; we ask
    GetKeywordStats and parse the top-query aggregate for the site."""
    q = urllib.parse.urlencode({"siteUrl": url, "country": "US"})
    status, data = _req("GET", BING_API + "/GetKeywordStats?%s" % q,
                        _hdr(WebmasterAPI=key))
    if status != 200 or not isinstance(data, list):
        return False, {"error": str(data)[:200], "rows": []}
    rows = [{"page": (r.get("Query") or "?"), "clicks": int(r.get("Clicks") or 0),
             "impressions": int(r.get("Impressions") or 0),
             "position": round(float(r.get("Position") or 0), 1),
             "max_position": round(float(r.get("MaxPosition") or 0), 1)}
            for r in data]
    totals = {"clicks": sum(r["clicks"] for r in rows),
              "impressions": sum(r["impressions"] for r in rows),
              "ctr": round(sum(r["clicks"] for r in rows) / max(
                  sum(r["impressions"] for r in rows), 1) * 100, 1),
              "position": round(sum(r["position"] for r in rows) / max(len(rows), 1), 1),
              "days": days}
    return True, {"rows": rows, "totals": totals}


def _strip_site_url(v):
    """Normalize a site string for comparison: drop scheme + trailing slash,
    lower-case. Bing returns site URLs with or without the scheme."""
    s = str(v or "").strip().rstrip("/")
    low = s.lower()
    for pre in ("https://", "http://"):
        if low.startswith(pre):
            s = s[len(pre):]
            break
    return s.lower()


def bing_user_sites(key):
    """Sites registered to a Bing Webmaster API key. Also doubles as the
    cheapest key-liveliness check (GetUserSites). Returns
    (ok, {"sites": [...], "registered": bool})."""
    if not key:
        return False, {"error": "bing API key not set", "sites": [], "registered": False}
    status, data = _req("GET", BING_API + "/GetUserSites", _hdr(WebmasterAPI=key))
    if status != 200:
        return False, {"error": str(data)[:200], "sites": [], "registered": False}
    if isinstance(data, dict):  # JSON-fragment wrapper
        inner = data.get("d")
        data = inner if isinstance(inner, list) else []
    if not isinstance(data, list):
        return False, {"error": "unexpected GetUserSites response", "sites": [],
                       "registered": False}
    sites = {_strip_site_url(v) for v in data if isinstance(v, str)}
    return True, {"sites": sorted(sites),
                  "registered": _strip_site_url(site_url()) in sites}


def bing_submit_url(key, url, page=None):
    """Push a single URL to Bing for immediate crawling (SubmitUrl). `url` is
    the site root; `page` is a path (None submits the root). Returns
    (ok, {"submitted": [...], "error": ...})."""
    target = url.rstrip("/") + (("/" + page.lstrip("/")) if page else "")
    status, data = _req("POST", BING_API + "/SubmitUrl", _hdr(WebmasterAPI=key),
                        {"siteUrl": url, "url": target})
    if status in (200, 201):
        return True, {"submitted": [target]}
    return False, {"error": str(data)[:200], "submitted": []}


# ------------------------------------------------------------------ Yandex
def yandex_auth_url(state):
    if not (YANDEX_CLIENT_ID and YANDEX_CLIENT_SECRET):
        return ""
    redir = seo.BASE_URL.rstrip("/") + "/admin/oauth/seoengines/cb/yandex"
    return ("%s/authorize?response_type=code&client_id=%s&redirect_uri=%s"
            "&scope=%s&state=%s"
            % (YANDEX_OAUTH, urllib.parse.quote(YANDEX_CLIENT_ID, safe=""),
               urllib.parse.quote(redir, safe=""),
               urllib.parse.quote("webmaster:host:all", safe=""),
               urllib.parse.quote(state, safe="")))


def yandex_exchange(code):
    redir = seo.BASE_URL.rstrip("/") + "/admin/oauth/seoengines/cb/yandex"
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": code,
        "client_id": YANDEX_CLIENT_ID, "client_secret": YANDEX_CLIENT_SECRET,
        "redirect_uri": redir}).encode()
    status, data = _req("POST", YANDEX_OAUTH + "/token",
                        {"Content-Type": "application/x-www-form-urlencoded",
                         "Content-Length": str(len(body))}, body)
    if status != 200 or not isinstance(data, dict) or not data.get("access_token"):
        return False, str(data)[:200]
    data["expires_at"] = int(time.time()) + int(data.get("expires_in", 86400 * 365))
    _json_set(TOKEN_SETTINGS["yandex"], data)
    return True, "connected"


def yandex_bearer():
    tok = _json_get(TOKEN_SETTINGS["yandex"])
    if not tok or not tok.get("access_token"):
        return None
    return tok.get("access_token")


def _yandex(path):
    tok = yandex_bearer()
    if not tok:
        return None, {"error": "yandex not connected"}
    return _req("GET", YANDEX_API + path, {"Authorization": "OAuth " + tok})


def yandex_user_id():
    status, data = _yandex("/user/")
    if status != 200 or not isinstance(data, dict) or not data.get("user_id"):
        return None, str(data)[:200]
    return data["user_id"], None


def yandex_hosts(uid):
    status, data = _yandex("/user/%s/hosts/" % uid)
    if status != 200 or not isinstance(data, dict):
        return [], str(data)[:200]
    return (data.get("hosts") or []), None


def yandex_pick_host():
    uid, err = yandex_user_id()
    if err:
        return None, None, err
    hosts, err = yandex_hosts(uid)
    if err:
        return None, None, err
    host = host_of()
    for h in hosts:
        if (h.get("host_name") or "").lower().strip("/") == host:
            return uid, h.get("host_id"), None
    return uid, None, "host not in your Yandex account yet"


def yandex_summary(days=28):
    uid, hid, err = yandex_pick_host()
    if err:
        return False, {"error": err, "rows": []}
    status, data = _yandex("/user/%s/hosts/%s/search-queries/summary/"
                           % (uid, hid))
    if status != 200 or not isinstance(data, dict):
        return False, {"error": str(data)[:200], "rows": []}
    t = data.get("totals") or {}
    clicks = int(t.get("clicks") or 0)
    shows = int(t.get("shows") or 0)
    pos = float(t.get("position") or 0)
    totals = {"clicks": clicks, "impressions": shows,
              "ctr": round(clicks / max(shows, 1) * 100, 1),
              "position": round(pos, 1), "days": days}
    return True, {"rows": [], "totals": totals}


# ------------------------------------------------------------------ sync / persist
def sync_engine(engine, days=28):
    """Refresh one engine's rates and persist a webtraffic snapshot so the hub
    renders from local data even if the console API is down later. Returns
    (ok, summary_dict)."""
    payload = None
    if engine == "gsc":
        ok, data = gsc_performance(days)
        payload = data
    elif engine == "bing":
        key = BING_API_KEY or store_get("seoeng.bing.apikey", "")
        if not key:
            return False, {"error": "bing API key not set"}
        ok, data = bing_stats(key, site_url(), days)
        payload = data
    elif engine == "yandex":
        ok, data = yandex_summary(days)
        payload = data
    else:
        return False, {"error": "unknown engine"}
    if ok and isinstance(payload, dict):
        _json_set("seoeng.%s.last" % engine,
                  {"at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
                   "totals": payload.get("totals") or {},
                   "rows": payload.get("rows") or []})
    return ok, {"engine": engine, "days": days, **payload}


def last_sync(engine):
    return _json_get("seoeng.%s.last" % engine)