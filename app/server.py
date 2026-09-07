# -*- coding: utf-8 -*-
"""pstore HTTP server: serves the static UI + JSON API on http.server (stdlib).

Endpoints
  GET  /                  -> static/index.html
  GET  /api/markets       -> available marketplaces
  GET  /api/autosuggest?q -> real Amazon keyword ideas
  GET  /api/search?q      -> products for a query (+market, +top, +category)
  GET  /api/mine?seed=    -> niche mining (niches + meta + signals)
GET  /api/niches        -> list saved niches from sqlite
   POST /api/niches        -> save a mined niche shortlist
   GET  /api/settings      -> marketplace + affiliate tag + scraper status
   POST /api/settings      -> set marketplace / affiliate tag / scraper keys
   GET  /api/ai/providers  -> AI provider status (admin)
   POST /api/ai/test       -> one-shot key test (admin)
   POST /api/ai/models     -> list provider models (admin)
   POST /api/ai/config     -> activate a provider runtime (admin)
"""
import datetime
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import amazon
import ai
import cms as cms_mod
import cms_render
import ebook as ebook_mod
import earnings
import indexnow
import mailer
import manual
import market_engine
import niche
import oauth
import paapi
import pricedrop
import seo
import security
import segments
import sem
import social
import publish
import suggest
import webmasters

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
DB = os.environ.get("PSTORE_DB", os.path.join(ROOT, "pstore.db"))
PORT = int(os.environ.get("PORT", "8765"))


def _ensure_db_file():
    """First boot on a persistent disk: if the configured DB file is missing,
    seed it from the in-image copy so the baked niches/settings survive."""
    if os.path.exists(DB):
        return
    seed = os.path.join(ROOT, "pstore.db")
    if os.path.exists(seed) and os.path.abspath(seed) != os.path.abspath(DB):
        try:
            os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
            shutil.copyfile(seed, DB)
        except OSError:
            pass


_ensure_db_file()

# --- admin auth -------------------------------------------------------------
# Everything under /admin + the owner tools/APIs sits behind an admin email +
# password (PSTORE_ADMIN_EMAIL / PSTORE_ADMIN_PASSWORD) with an in-memory
# session cookie. Public pages keep serving without a cookie: /, /n/*, /lp/*,
# about/legal, robots, sitemap, key.
_ADMIN_EMAIL = os.environ.get("PSTORE_ADMIN_EMAIL", "salahuddinhabibisah@gmail.com")
_ADMIN_PW = os.environ.get("PSTORE_ADMIN_PASSWORD", "$_Salahu1991")
_ADMIN_EMAIL_FROM_ENV = bool(os.environ.get("PSTORE_ADMIN_EMAIL"))
_ADMIN_PW_FROM_ENV = bool(os.environ.get("PSTORE_ADMIN_PASSWORD"))
_COOKIE = "pstore_admin"
_OAUTH_COOKIE = "pstore_oauth"
_SESSION_TTL = 12 * 60 * 60  # seconds

# --------------------------------------------------------------- RBAC model
# Every admin tool/API belongs to exactly one *function*. Roles are lists of
# functions (a custom permission matrix); a user's allowed set is the union of
# their roles' functions. The env owner can always do everything and is the
# only one who can manage users/roles.
FUNCTIONS = [
    ("dashboard", "Idea tools"),
    ("email", "Email Studio"),
    ("social", "Social publisher"),
    ("seo", "SEO & consoles"),
    ("content", "Content (CMS/ebooks)"),
    ("marketing", "Marketing & ROI"),
    ("analytics", "Analytics & backup"),
    ("keys", "Keys & API keys"),
]

# Slugs maintained so tests have a stable, documented reference.
FN = {slug: label for slug, label in FUNCTIONS}
ALL_FUNCTIONS = [slug for slug, _label in FUNCTIONS]

# path -> function assignment (prefix; a trailing "/" requires the prefix start).
FUNCTION_PATHS = {
    "dashboard": ("/dashboard", "/index.html", "/tool", "/admin/opportunities",
                  "/admin/priority", "/app.js", "/api/mine", "/api/search",
                  "/api/autosuggest", "/api/niches", "/api/opportunities"),
    "email": ("/admin/emails", "/api/mail", "/api/sequence/", "/api/subscribers"),
    "social": ("/admin/social", "/api/social"),
    "seo": ("/admin/seo", "/admin/seoengines", "/admin/sem", "/seo/snippet/",
            "/api/sem", "/api/seo-audit", "/api/seo/topics", "/api/seoengines",
            "/api/indexnow", "/api/topics/generate"),
    "content": ("/admin/cms", "/admin/ebooks", "/admin/refresh",
                "/api/cms", "/api/suggest", "/api/refresh", "/api/settings",
                "/api/ai/"),
    "marketing": ("/admin/funnel", "/admin/marketing", "/admin/variants",
                  "/admin/segments", "/admin/pricedrop", "/api/funnel",
                  "/api/marketing", "/api/boosts", "/api/variants",
                  "/api/segments", "/api/pricedrop", "/api/tools",
                  "/api/earnings", "/api/subjects"),
    "analytics": ("/admin/analytics", "/admin/backup", "/admin/manual",
                  "/api/analytics"),
    "keys": ("/keys", "/keys/", "/admin/apikeys", "/api/keys"),
}

# Hub/nav chip key -> owning function (for filtering what a user sees).
NAV_FN = {
    "dashboard": "dashboard", "tool": "dashboard", "opportunities": "dashboard",
    "priority": "dashboard", "sem": "seo", "seo": "seo", "seoengines": "seo",
    "cms": "content", "ebooks": "content", "refresh": "content",
    "funnel": "marketing", "marketing": "marketing", "emails": "email",
    "social": "social", "variants": "marketing", "segments": "marketing",
    "pricedrop": "marketing", "keys": "keys", "apikeys": "keys",
    "analytics": "analytics", "backup": "analytics", "manual": "analytics",
}

BUILTIN_ROLES = [
    ("emailer", "Email Studio", ["email"]),
    ("social", "Social publisher", ["social"]),
    ("seo", "SEO & consoles", ["seo"]),
    ("content", "Content builder", ["content"]),
    ("marketing", "Marketing & ROI", ["marketing"]),
    ("analyst", "Analytics", ["analytics"]),
    ("keys", "Keys & API keys", ["keys"]),
    ("operator", "Operator", ["dashboard", "email", "social"]),
    ("full", "Full access", ALL_FUNCTIONS),
]
_SESSIONS = {}  # token -> monotonic expiry

_EBOOKS = {}  # keyword -> build_ebook() dict, LRU-ish (capped below)
_SOCIAL_WEBHOOK = os.environ.get("SOCIAL_WEBHOOK", "")  # optional real-posting hook
_PNG_CACHE = {}  # slug -> raster og card bytes (pure-Python render, capped)
_CRON_SECRET = os.environ.get("EMAIL_CRON_SECRET", "")  # keyed /api/cron/send trigger
_AUTOSEND_HOURS = [int(h) for h in (os.environ.get("AUTOSEND_HOURS") or "").split(",")
                   if h.strip().isdigit()]  # UTC hours the sequence auto-sends (empty=off)
_AUTOSEND_LIMIT = int((os.environ.get("AUTOSEND_LIMIT") or "0") or 0) \
    or mailer.MAX_EMAILS_PER_RUN
_AUTOSEND_LAST_KEY = "autosend.last"  # "YYYY-MM-DD:HH" marker so a slot runs once/day
AUTOSEND_STATE_KEY = "autosend.state"  # json: {status,sent,last_run,last_status,next_run,errors}
SOCIAL_PEAK_SLOTS = (8, 12, 19)  # high-engagement schedule hours (morning/lunch/evening)


def _autosend_cfg():
    """Effective autosend config: settings table overrides env defaults."""
    raw_h = (_get_setting("autosend.hours") or "").strip()
    raw_l = (_get_setting("autosend.limit") or "").strip()
    raw_e = (_get_setting("autosend.enabled") or "").strip()
    hours = [int(h) for h in (raw_h or ",".join(map(str, _AUTOSEND_HOURS))).split(",")
             if h.strip().isdigit() and 0 <= int(h) <= 23]
    limit = _AUTOSEND_LIMIT
    try:
        if raw_l:
            limit = int(raw_l)
    except Exception:
        pass
    enabled = (raw_e != "0") if raw_e or _AUTOSEND_HOURS else bool(hours)
    return {"enabled": bool(enabled and hours), "hours": hours, "limit": max(limit, 0)}

_TOTOP = ('<div class="totop"><a href="#top" aria-label="Back to top">&uarr;</a></div>'
          '<script src="/ui.js" defer></script>')

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

_lock = threading.Lock()

# --- niche data refresh -------------------------------------------------------
# Manual refresh re-mines one/all saved niches so prices, ratings and stock
# stay current. An automatic background loop refreshes "stale" niches on a
# schedule (a niche is stale when it hasn't been refreshed in N minutes) and
# throttles to a few per cycle so it never hammers Amazon.
_REFRESH_STALE_MIN = int(os.environ.get("PSTORE_REFRESH_MIN", "1440"))      # 24h
_REFRESH_INTERVAL_SEC = int(os.environ.get("PSTORE_REFRESH_INTERVAL", "3600"))  # 1h
_REFRESH_MAX_PER_CYCLE = int(os.environ.get("PSTORE_REFRESH_MAX", "3"))
_REFRESHING = set()  # keyword -> in-flight guard so a niche isn't double-mined
_refresh_lock = threading.Lock()

# In-memory render cache for the hot SEO pages (/n/ + /lp/) so crawler storms
# don't re-mine/re-render the same niche repeatedly. Keyed by role+slug+variant
# so A/B variants keep their identity; entries expire to stay fresh.
_render_cache = {}
_render_cache_lock = threading.Lock()
RENDER_CACHE_DEFAULT_TTL = 300.0

# Short-TTL caches for the admin "Grow" payloads, so the page render never
# blocks on per-winner live Amazon autosuggest or a full suggest-engine run.
_ADMIN_DATA_TTL = 15 * 60.0
_ADMIN_SUGG_TTL = 60 * 60.0
_ADMIN_WARMING = set()  # single-flight guard for the background warm thread
_OPP_WARMING_NOTE = ("Opportunities are warming up — this computes live Amazon "
                     "autosuggestions and takes ~30s. Refresh in about a minute.")
_SUGG_WARMING_NOTE = ("Suggestions are warming up — the suggest engine runs "
                      "live searches and takes ~30s. Refresh in about a minute.")


def _bust_admin_data_cache():
    with _render_cache_lock:
        _render_cache.pop("opp:data", None)
        _render_cache.pop("sugg:data", None)


def _warm_startup_admin():
    """Background pre-warm of the admin Grow payloads right after boot so the
    first page visit never blocks on live autosuggest / suggest-engine runs."""
    try:
        Handler.__new__(Handler)._warm_admin_payloads()
    except Exception:
        pass


def _render_cache_get(key):
    with _render_cache_lock:
        hit = _render_cache.get(key)
        if not hit:
            return None
        if hit[0] < time.time():
            _render_cache.pop(key, None)
            return None
        return hit[1]


def _render_cache_put(key, value, ttl):
    with _render_cache_lock:
        _render_cache[key] = (time.time() + ttl, value)
        if len(_render_cache) > 400:  # bounded
            now = time.time()
            for k in [k for k, v in _render_cache.items() if v[0] < now]:
                _render_cache.pop(k, None)
            # still over the cap because everything is young -> evict oldest
            while len(_render_cache) > 300:
                oldest = min(_render_cache.items(), key=lambda kv: kv[1][0])
                _render_cache.pop(oldest[0], None)


def _refresh_stale_candidates(now):
    """Return keywords of saved niches that are stale (never refreshed or past
    their staleness window), oldest-first, capped to a small batch."""
    with _lock:
        conn = _db()
        rows = conn.execute(
            "SELECT keyword, updated_at FROM niches ORDER BY "
            "CASE WHEN updated_at IS NULL THEN 0 ELSE 1 END, updated_at ASC").fetchall()
        conn.close()
    out = []
    for r in rows:
        kw = r["keyword"]
        with _refresh_lock:
            if kw in _REFRESHING:
                continue
        if len(out) >= _REFRESH_MAX_PER_CYCLE:
            break
        updated = r["updated_at"]
        if not updated:
            out.append(kw)
            continue
        try:
            parsed = time.mktime(time.strptime(updated[:19], "%Y-%m-%d %H:%M:%S"))
        except (ValueError, TypeError):
            out.append(kw)
            continue
        if now - parsed >= _REFRESH_STALE_MIN * 60:
            out.append(kw)
    return out


def _refresh_niche(keyword):
    """Re-mine a single saved niche in place. Returns a dict with the updated
    data, or None if the keyword isn't a saved niche. Never raises: on failure
    returns a dict with 'error' set."""
    with _refresh_lock:
        if keyword in _REFRESHING:
            return {"status": "busy", "error": "already refreshing"}
        _REFRESHING.add(keyword)
    try:
        with _lock:
            conn = _db()
            row = conn.execute("SELECT keyword FROM niches WHERE keyword=?",
                               (keyword,)).fetchone()
            conn.close()
        if not row:
            return {"status": "missing", "error": "niche not found"}
        try:
            data = niche.refresh_keyword(keyword)
        except Exception as exc:  # network/provider hiccup — don't crash
            return {"status": "error", "error": str(exc)}
        with _lock:
            conn = _db()
            conn.execute(
                "UPDATE niches SET products=?, score=?, saturation=?, updated_at=datetime('now') "
                "WHERE keyword=?",
                (json.dumps(data.get("products") or []),
                 data.get("score"), data.get("saturation"), keyword))
            conn.commit()
            conn.close()
        return {"status": "ok", "keyword": keyword,
                "products": len(data.get("products") or []),
                "score": data.get("score"), "saturation": data.get("saturation")}
    finally:
        with _refresh_lock:
            _REFRESHING.discard(keyword)


def _auto_refresh_loop():
    """Daemon that periodically refreshes stale niches in small batches. Runs on
    an interval; a zero interval disables automatic refreshing."""
    while True:
        time.sleep(max(_REFRESH_INTERVAL_SEC, 300))
        if _REFRESH_INTERVAL_SEC <= 0:
            continue
        try:
            now = time.time()
            for kw in _refresh_stale_candidates(now):
                _refresh_niche(kw)
        except Exception:
            pass


def _refresh_all_worker(kws):
    """Background worker for a manual 'refresh all' — re-mines every saved
    niche in place so the request returns immediately and the admin page can
    poll status. Never raises."""
    for kw in (kws or []):
        try:
            _refresh_niche(kw)
        except Exception:
            pass


def _publish_key_getter():
    """Wire the persisted social API keys (from the /admin/apikeys settings KV)
    into the native posting gateway. Maps the composer's (platform, field) into
    the settings key that holds that credential:
      * Twitter / X needs four OAuth 1.0a creds stored as `social.key.twitter.<field>`
        (client_id, client_secret, access_token, access_token_secret).
      * Every other platform publishes a single token stored as `social.key.<platform>`,
        so each candidate field name resolves to that one value."""
    def kv(ns, name):
        base = _get_setting("social.key." + ns, "")
        if ns != "twitter":
            return base
        sub = _get_setting("social.key.twitter.%s" % name, "")
        return sub or base
    return kv


def _publish_native(kits):
    """Best-effort native per-platform posting for a batch of kits. Uses the
    keys the operator pasted on /admin/apikeys; platforms without keys report
    'skipped' (so the caller falls back to the webhook). Never raises. Returns
    the list of publish results so callers can count real posts."""
    if not kits:
        return []
    return publish.publish_batch(list(kits), _publish_key_getter())


def _native_posted_count(results):
    return sum(1 for r in results or [] if r and r.get("ok") and r.get("via") == "native")


def _webhook_payload(kit):
    """One Make/Zapier-ready payload per kit. Everything the robot needs:
    copy + tracked link + platform + niche (slug/keyword/board) + both share
    cards (SVG for OG, PNG raster for Pinterest-style posting)."""
    slug = kit.get("slug") or ""
    keyword = (kit.get("keyword") or "").strip() or slug.replace("-", " ") or "niche"
    board = _pinterest_board(keyword)
    image = kit.get("image") or social.og_image_url(seo.BASE_URL, slug)
    image_png = kit.get("image_png") or social.og_image_png_url(seo.BASE_URL, slug)
    return {
        "body": kit.get("body") or "",
        "link": kit.get("link") or "",
        "platform": kit.get("platform") or "",
        "name": kit.get("name") or "",
        "slug": slug,
        "keyword": keyword,
        "board": board,
        "image": image,
        "image_png": image_png,
    }


def _pinterest_board(keyword):
    """Human board name for one post's niche (Make routes pins to a board per
    keyword). Usually the title-cased keyword, capped to a sane size."""
    name = (" ".join(w.capitalize() for w in re.split(r"[^A-Za-z0-9]+",
                                                      keyword.replace("-", " "))
                     if w) if keyword else "Deals") or "Deals"
    return name[:60]


def _webhook_fire(kits):
    """Module-level, background, fire-and-forget SOCIAL_WEBHOOK POST for each
    kit (used by the timer loop; the HTTP handler uses its own instance method
    so the request thread can reuse the same seam). Never raises."""
    webhook = os.environ.get("SOCIAL_WEBHOOK", "") or _SOCIAL_WEBHOOK
    if not webhook or not kits:
        return

    def fire():
        for kit in kits:
            try:
                req = urllib.request.Request(
                    webhook,
                    data=json.dumps(_webhook_payload(kit)).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp.read()
            except Exception:
                continue
    threading.Thread(target=fire, daemon=True).start()


def _flush_due_social(hook=None, now=None):
    """Publish every due scheduled post. Returns (published_now, still_pending).
    ``hook`` is a callable(kits) that fires real posting (webhook); it is invoked
    once with the batch of due posts. Never raises."""
    stamp = now or datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _lock:
            conn = _db()
            rows = conn.execute(
                "SELECT id, slug, keyword, platform, name, body, link FROM social_posts "
                "WHERE status='scheduled' AND scheduled_at IS NOT NULL "
                "AND scheduled_at <= ? ORDER BY scheduled_at", (stamp,)).fetchall()
            due = [dict(r) for r in rows]
            for r in due:
                conn.execute(
                    "UPDATE social_posts SET status='published', published_at=?, "
                    "scheduled_at=NULL WHERE id=?", (stamp, r["id"]))
            conn.commit()
            conn.close()
        # Try native per-platform posting with the operator's pasted keys; only
        # the platforms that had no creds (skipped) fall through to the webhook.
        due_kits = [{"platform": r["platform"], "name": r["name"] or "",
                     "body": r["body"] or "", "link": r["link"] or "",
                     "slug": r["slug"] or "", "keyword": r["keyword"] or ""} for r in due]
        webhook_kits = due_kits
        try:
            if due_kits:
                results = _publish_native(due_kits)
                posted = {str(r.get("slug")) + "|" + str(r.get("platform"))
                          for r in results if r.get("ok") and r.get("via") == "native"}
                if posted:
                    webhook_kits = [k for k in due_kits
                                    if (str(k.get("slug")) + "|" + str(k.get("platform"))) not in posted]
        except Exception:
            webhook_kits = due_kits
        if webhook_kits and hook:
            hook(webhook_kits)
        with _lock:
            conn = _db()
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM social_posts WHERE status='scheduled'"
            ).fetchone()["n"]
            conn.close()
        return len(due), pending
    except Exception:
        return 0, 0


def _get_setting(key, default=""):
    try:
        with _lock:
            conn = _db()
            row = conn.execute("SELECT value FROM settings WHERE key=?",
                               (key,)).fetchone()
            conn.close()
    except Exception:
        return default
    return row["value"] if row else default


def _set_setting(key, value):
    try:
        with _lock:
            conn = _db()
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value or ""))
            conn.commit()
            conn.close()
    except Exception:
        pass


# webmasters keeps connector tokens + synced snapshots in the settings table.
webmasters._STORE_GET = _get_setting
webmasters._STORE_SET = _set_setting


# ------------------------------------------------------------ RBAC data access
def _user_row(email):
    """Full users row for `email` (normalized), or None."""
    with _lock:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE email=?", (email.strip().lower(),)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def _role_functions(slug):
    """Function slugs granted by one role slug (JSON column -> list)."""
    with _lock:
        conn = _db()
        try:
            row = conn.execute("SELECT functions FROM roles WHERE slug=?",
                               (slug,)).fetchone()
            if not row:
                return []
            try:
                return [f for f in json.loads(row["functions"]) if f in FN]
            except ValueError:
                return []
        finally:
            conn.close()


def _user_functions(email=None, uid=None):
    """Union of function slugs across an owner's (None/empty email => all) or
    a user's roles. Never returns functions from a disabled owner email."""
    if email is None and uid is None:
        return ALL_FUNCTIONS
    row = _user_row(email) if email is not None else (
        None if uid is None else _user_row_by_id(uid))
    if not row:
        return []
    try:
        roles = json.loads(row.get("roles") or "[]")
    except ValueError:
        roles = []
    out, seen = [], set()
    for slug in roles:
        for f in _role_functions(slug):
            if f not in seen:
                seen.add(f)
                out.append(f)
    return out


def _user_row_by_id(uid):
    with _lock:
        conn = _db()
        try:
            row = conn.execute("SELECT * FROM users WHERE id=?",
                               (uid,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def _social_flush_loop(interval=60, amplify=True):
    """Daemon: flush due scheduled posts automatically every `interval` seconds
    so owners don't have to click Flush, then re-amplify proven winners.
    Stops when interval <= 0."""
    while True:
        time.sleep(max(interval, 15))
        _flush_due_social(_webhook_fire)
        if amplify:
            try:
                _auto_amplify_winners()
            except Exception:
                pass


def _auto_amplify_winners(now=None):
    """CLOSE THE SOCIAL LOOP: after posts go out and earn clicks, the top
    performers are automatically re-queued to a future peak slot with their
    SAME attribution code (so winners compound, cold ones stay dead).

    This turns "winner/warm/cold" labels into an action. Re-scheduling keeps
    the utm_content stable, so a winner's re-published links keep feeding
    the same click tally.

    Anti-loop guards (all tunable via settings):
      * only published posts count as re-amplifiable candidates,
      * a post must be older than `social.amplify.min_age_hours` (default 24),
      * a post is re-queued at most `social.amplify.max_runs` times (default 2),
      * per sweep we cap at `social.amplify.max_per_sweep` (default 3),
      * a scheduled/queued copy is never double-amped (status must be 'published').

    Feature gate: `social.amplify` unset (or 1/on/true/yes) => on; explicitly
    ​0/off/false => disabled. Returns
    {"requeued": n, "winners": [...], "queue": [...], "on": bool}."""
    now = now or datetime.datetime.utcnow()
    on_flag = _get_setting("social.amplify")
    if on_flag != "" and str(on_flag).strip().lower() not in ("1", "on", "true", "yes"):
        return {"requeued": 0, "winners": [], "queue": [], "on": False}
    on = True
    min_age_h = float(_get_setting("social.amplify.min_age_hours") or 24)
    max_runs = int(_get_setting("social.amplify.max_runs") or 2)
    cap = int(_get_setting("social.amplify.max_per_sweep") or 3)
    window_h = float(_get_setting("social.amplify.window_hours") or 48)
    then = now - datetime.timedelta(hours=min_age_h)
    age_stamp = then.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _lock:
            conn = _db()
            rows = conn.execute(
                "SELECT id, slug, keyword, platform, name, body, link, utm_content, "
                "published_at, COALESCE(amplify_count,0) amp "
                "FROM social_posts "
                "WHERE status='published' AND published_at IS NOT NULL "
                "AND published_at <= ? AND utm_content != ''",
                (age_stamp,)).fetchall()
            stats = {}
            for r in rows:
                stats[r["id"]] = conn.execute(
                    "SELECT COUNT(*) c FROM clicks WHERE lower(slug)=? AND content=?",
                    (r["slug"], r["utm_content"])).fetchone()["c"]
            conn.close()
    except Exception:
        return {"requeued": 0, "winners": [], "queue": [], "on": on}
    best = max(stats.values(), default=0)
    cands = []
    for r in rows:
        clicks = stats.get(r["id"]) or 0
        is_winner = clicks > 0 and (best == 0 or clicks == best) or \
            (best > 0 and clicks >= max(1, int(best * 0.5)))
        if is_winner and (r["amp"] or 0) < max_runs:
            cands.append((clicks, r))
    cands.sort(key=lambda t: (-t[0], t[1]["platform"]))
    queue = cands[:cap]

    def snap_peak(t, delta_hours):
        if t.hour in SOCIAL_PEAK_SLOTS:
            return t
        bl = t
        bd = 25
        for h in SOCIAL_PEAK_SLOTS:
            for day in (0, 1):
                cand = t.replace(hour=h, minute=0, second=0, microsecond=0) \
                    + datetime.timedelta(days=day)
                dist = (cand - t).total_seconds() / 3600.0
                if 0 <= dist <= bd and dist <= float(delta_hours):
                    bd = dist
                    bl = cand
        return bl

    times = []
    for i in range(len(queue)):
        delta = float(window_h) * (i + 1) / float(max(len(queue), 1))
        times.append(snap_peak(now + datetime.timedelta(hours=delta), delta)
                     .strftime("%Y-%m-%d %H:%M:%S"))
    requeued = 0
    winners_out = []
    try:
        with _lock:
            conn = _db()
            for (clicks, r), at in zip(queue, times):
                amp = int(r["amp"] or 0) + 1
                conn.execute(
                    "UPDATE social_posts SET status='scheduled', scheduled_at=?, "
                    "name=?, body=?, link=?, amplify_count=? WHERE id=?",
                    (at, r["name"], r["body"], r["link"], amp, r["id"]))
                requeued += 1
                winners_out.append({"slug": r["slug"], "platform": r["platform"],
                                    "clicks": clicks, "amp": amp, "next": at})
            conn.commit()
            conn.close()
    except Exception:
        pass
    return {"requeued": requeued, "winners": winners_out, "queue": queue, "on": on}


def _db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS niches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        keyword TEXT NOT NULL,
        market TEXT NOT NULL,
        score REAL,
        saturation REAL,
        products TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS subscribers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL,
        source TEXT,
        keyword TEXT,
        first_name TEXT,
        confirmed INTEGER DEFAULT 1,
        unsubscribed INTEGER DEFAULT 0,
        sent_index INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sent_emails (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subscriber_id INTEGER NOT NULL,
        email_index INTEGER NOT NULL,
        subject TEXT,
        sent_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS email_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL,
        subscriber_id INTEGER,
        email_index INTEGER,
        keyword TEXT,
        asin TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_email_events_sub ON email_events"
                 " (type, subscriber_id, email_index)")
    conn.execute("""CREATE TABLE IF NOT EXISTS email_sends (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign TEXT NOT NULL,
        subscriber_id INTEGER NOT NULL,
        keyword TEXT,
        asin TEXT,
        sent_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_email_sends_dedup "
                 "ON email_sends (campaign, subscriber_id, asin)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_email_sends_campaign "
                 "ON email_sends (campaign, subscriber_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        spec TEXT NOT NULL,
        recipients TEXT NOT NULL,
        scheduled_at TEXT,
        status TEXT DEFAULT 'scheduled',
        result TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        sent_at TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox (status, scheduled_at)")
    conn.execute("""CREATE TABLE IF NOT EXISTS mailbox_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id TEXT,
        in_reply_to TEXT,
        mailbox TEXT DEFAULT 'inbox',
        from_addr TEXT,
        from_name TEXT,
        subject TEXT,
        text TEXT,
        html TEXT,
        subscriber_id INTEGER,
        status TEXT DEFAULT 'unread',
        sent_at TEXT DEFAULT (datetime('now')),
        replied_at TEXT,
        UNIQUE(message_id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mailbox_status ON mailbox_messages (status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mailbox_sub ON mailbox_messages (subscriber_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS clicks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT,
        source TEXT,
        ip TEXT,
        referrer TEXT,
        asin TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS social_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT NOT NULL,
        keyword TEXT NOT NULL,
        platform TEXT NOT NULL,
        name TEXT,
        body TEXT,
        link TEXT,
        utm_content TEXT,
        status TEXT DEFAULT 'draft',
        scheduled_at TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        published_at TEXT
    )""")
    try:
        conn.execute("ALTER TABLE social_posts ADD COLUMN scheduled_at TEXT")
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE social_posts ADD COLUMN amplify_count INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE sent_emails ADD COLUMN subject_variant INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass
    conn.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        pass_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'unverified',
        roles TEXT NOT NULL DEFAULT '[]',
        created_at TEXT DEFAULT (datetime('now')),
        last_login_at TEXT
    )""")
    try:
        conn.execute("ALTER TABLE users ADD COLUMN reset_token TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN reset_expires TEXT")
        conn.commit()
    except Exception:
        pass
    conn.execute("""CREATE TABLE IF NOT EXISTS roles (
        slug TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        functions TEXT NOT NULL DEFAULT '[]',
        builtin INTEGER DEFAULT 0
    )""")
    try:
        have = conn.execute("SELECT COUNT(*) c FROM roles").fetchone()["c"]
        if have == 0:
            for slug, label, funcs in BUILTIN_ROLES:
                conn.execute(
                    "INSERT OR IGNORE INTO roles (slug, label, functions, builtin) "
                    "VALUES (?,?,?,1)",
                    (slug, label, json.dumps(list(funcs))))
            conn.commit()
    except Exception:
        pass
    conn.execute("""CREATE TABLE IF NOT EXISTS topics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        parent_slug TEXT NOT NULL,
        term TEXT NOT NULL,
        slug TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(parent_slug, slug)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS boosts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT NOT NULL,
        keyword TEXT NOT NULL,
        name TEXT NOT NULL,
        script TEXT,
        link TEXT,
        utm_content TEXT,
        runs INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active',
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT NOT NULL DEFAULT 'page',
        page TEXT,
        name TEXT NOT NULL,
        keyword TEXT,
        source TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    try:
        conn.execute("ALTER TABLE events ADD COLUMN referrer TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()
    try:
        conn.execute("ALTER TABLE clicks ADD COLUMN content TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()
    try:
        conn.execute("ALTER TABLE clicks ADD COLUMN asin TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()
    try:
        conn.execute("ALTER TABLE niches ADD COLUMN updated_at TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()
    conn.execute("""CREATE TABLE IF NOT EXISTS earnings_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        month TEXT NOT NULL,
        orders INTEGER DEFAULT 0,
        earnings REAL DEFAULT 0,
        note TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS niche_variants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT NOT NULL,
        variant INTEGER DEFAULT 1,
        headline TEXT,
        enabled INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS email_subjects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        keyword TEXT NOT NULL,
        email_index INTEGER NOT NULL,
        variant INTEGER DEFAULT 1,
        subject TEXT,
        enabled INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(keyword, email_index, variant)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS social_captions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT NOT NULL,
        platform TEXT NOT NULL,
        variant INTEGER DEFAULT 1,
        caption TEXT,
        enabled INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(slug, platform, variant)
    )""")
    cms_mod.ensure_tables(conn)
    return conn


def _init():
    amazon.MIN_INTERVAL = 0.5
    amazon.MAX_ATTEMPTS = 3
    with _lock:
        conn = _db()
        conn.close()
    # Restore API keys/settings persisted via the UI (env still wins at
    # read time): PA-API creds and the social webhook survive restarts.
    try:
        paapi.configure(
            _get_setting("paapi.access_key"),
            _get_setting("paapi.secret_key"),
            _get_setting("paapi.partner_tag"),
        )
    except Exception:
        pass
    # A social webhook saved on /admin/apikeys only took effect for the UI
    # until now: the publishing path reads the boot-time value (env first).
    # Rehydrate it here so a UI-saved webhook actually fires after a restart.
    try:
        global _SOCIAL_WEBHOOK
        if not _SOCIAL_WEBHOOK:
            _SOCIAL_WEBHOOK = _get_setting("social.webhook") or ""
    except Exception:
        pass
    # Rehydrate key groups that were persisted via the UI so they survive a
    # restart/redeploy (env vars still take priority at read time). Scraper
    # keys + AI provider keys come back into the in-memory runtime modules.
    try:
        for pid, meta in (amazon._SCRAPER_PROVIDERS or {}).items():
            v = _get_setting("scraper.key." + pid)
            if v:
                amazon.set_scraper_key(pid, v)
    except Exception:
        pass
    try:
        for p in ai.PROVIDERS:
            k = _get_setting("ai.key." + p)
            if k:
                ai.configure_runtime(p, k, _get_setting("ai.model." + p) or "",
                                     _get_setting("ai.base." + p) or "")
    except Exception:
        pass
    # Rehydrate the earnings estimator config persisted via the analytics page
    # so tuned commission/AOV/order-rate survive a restart/redeploy.
    try:
        cp = _get_setting("earnings.commission_pct")
        aov = _get_setting("earnings.avg_order")
        rate = _get_setting("earnings.order_rate")
        if cp or aov or rate:
            earnings.configure(
                commission_pct=float(cp) if cp else None,
                avg_order=float(aov) if aov else None,
                order_rate=float(rate) if rate else None)
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "pstore"
    sys_version = ""

    def log_message(self, *a):
        pass

    def send_header(self, key, value):
        """Defer headers until the status line is written, so callers may set
        cookies before send_response() without corrupting the response."""
        if not getattr(self, "_resp_started", False):
            if not hasattr(self, "_resp_early"):
                self._resp_early = []
            self._resp_early.append((key, value))
            return
        super().send_header(key, value)

    def send_response(self, code, message=None):
        if not getattr(self, "_resp_started", False):
            self._resp_started = True
            super().send_response(code, message)
            for k, v in getattr(self, "_resp_early", []):
                super().send_header(k, v)
            self._resp_early = []
            return
        super().send_response(code, message)

    def _is_secure(self):
        return (self.headers.get("X-Forwarded-Proto") or "http").lower() == "https"

    def end_headers(self):
        """Security headers on every response: clickjacking, MIME sniffing,
        referrer + CSP, permissions and HSTS behind the TLS proxy."""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' https: data:; "
                         "font-src 'self'; connect-src 'self'; object-src 'none'; "
                         "base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
        self.send_header("Permissions-Policy",
                         "geolocation=(), microphone=(), camera=(), payment=()")
        if self._is_secure():
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        super().end_headers()

    def _client_ip(self):
        try:
            return self.client_address[0] or ""
        except Exception:
            return ""

    def _same_origin(self):
        """CSRF defense for state-changing requests: when a browser sends
        Origin, its host must match where the request actually landed."""
        origin = self.headers.get("Origin")
        if not origin:
            return True  # curl / server-to-server
        host = (self.headers.get("Host") or "").lower()

        def norm(h):
            if h.endswith(":443"):
                return h[:-4]
            if h.endswith(":80"):
                return h[:-3]
            return h
        try:
            return norm(urllib.parse.urlsplit(origin).netloc.lower()) == norm(host.split(" ")[0])
        except Exception:
            return False

    def do_HEAD(self):
        """Serve GET-equivalent headers with no body. Many crawlers and sitemap
        checkers probe with HEAD first; an unimplemented method just looks broken."""
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(data)

    def _send_cached(self, body, ctype, max_age=RENDER_CACHE_DEFAULT_TTL, edge=True):
        """Send a public HTML/xml response tagged with a Cache-Control lifetime so
        browsers, crawlers and CDN edges reuse it. The /n/ render cache is keyed by
        content and expires itself, so the stale window is bounded and safe.

        edge=True also emits s-maxage: Cloudflare (already in front of Render)
        then serves the page from the edge, so Google/Bing never wait on the
        free-tier cold start. Pages with A/B variants pass edge=False to keep the
        per-visitor headline intact."""
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        age = max(int(float(max_age)), 0)
        if edge:
            edge_age = max(int(min(float(max_age), 900.0)), 60)
            self.send_header("Cache-Control",
                             "public, max-age=%d, s-maxage=%d, stale-while-revalidate=60"
                             % (age, edge_age))
        else:
            self.send_header("Cache-Control", "public, max-age=%d" % age)
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(data)

    def _settings(self):
        return {
            "market": amazon.MARKET,
            "markets": amazon._MARKETPLACES,
            "affiliate_tag": amazon.AFFILIATE_TAG,
            "scraper": amazon.scraper_status(),
            "paapi": paapi.status(),
            "social": {
                "webhook": bool(_get_setting("social.webhook")),
                "keys": {
                    social._key(platform): bool(_get_setting("social.key." + social._key(platform)))
                    for platform in social.PLATFORMS
                },
            },
            "marketing": market_engine.status_blurb(),
            "demography": self._demo(),
            "mailer": {
                "configured": mailer.configured(),
                "host": mailer.SMTP_HOST or "",
                "max_per_run": mailer.MAX_EMAILS_PER_RUN,
            },
            "ai": {
                "configured": ai.configured(),
                "provider": ai.active_provider(),
                "model": ai.model_for(ai.active_provider() or "openai") if ai.configured() else "",
                "providers": ai.providers(),
                "note": "Ebook/headline copy uses the active AI provider; template fallback otherwise.",
            },
            "publish": {
                "indexnow_key": indexnow.key(),
                "sitemap": "/sitemap.xml",
                "site_url": seo.BASE_URL,
            },
        }

    def _demo(self):
        """Market-demography targeting profile: region + audience interests +
        persona fields. Everything is operator-set (no guessing) and read straight
        from the DB so any tool can use it for audience-aware marketing copy."""
        return {
            "region": _get_setting("demo.region"),
            "interest": _get_setting("demo.interest"),
            "interests_extra": _get_setting("demo.interests_extra"),
            "behavior": _get_setting("demo.behavior"),
            "age": _get_setting("demo.age"),
            "audience": _get_setting("demo.audience"),
            "income": _get_setting("demo.income"),
            "tone": _get_setting("demo.tone"),
        }

    # ------------------------------------------------------------------ AI panel
    def _ai_providers(self):
        return self._send(200, {"providers": ai.providers(),
                                "active": ai.active_provider()})

    def _ai_test(self):
        body = self._body()
        return self._send(200, ai.test(
            body.get("provider"), body.get("api_key") or body.get("key"),
            body.get("model"), body.get("base_url") or body.get("base")))

    def _ai_models(self):
        body = self._body()
        models = ai.list_models(
            body.get("provider"), body.get("api_key") or body.get("key"),
            body.get("base_url") or body.get("base"))
        return self._send(200, {"provider": body.get("provider"), "models": models})

    def _ai_config(self):
        body = self._body()
        out = ai.configure_runtime(
            body.get("provider"), body.get("api_key") or body.get("key"),
            body.get("model"), body.get("base_url") or body.get("base"))
        if out.get("ok"):
            with _lock:
                _EBOOKS.clear()
        return self._send(200, out)

    # ------------------------------------------------------------------ AI field autofill
    _AI_FIELD_BRIEFS = {
        "headline": (
            "Write a single H1 headline for an Amazon-affiliate buying guide page about this niche. "
            "It must front-load the niche, promise a concrete outcome (Suby dream-outcome + PAS), and "
            "be curiosity-driven and click-worthy — not hype. One sentence, under 70 characters.",
            "headline"),
        "subheadline": (
            "Write the subheadline under that headline for the same buying guide. Supportive, specific "
            "and benefit-led: tell the reader exactly what they'll get (saved time, vetted picks, no "
            "46 open tabs). No hype, no clichés. One or two short sentences, under 130 characters.",
            "subheadline"),
        "badge": (
            "Create a short badge / eyebrow label for the top pick on a buying guide page (e.g. 'Top "
            "pick', 'Editor's choice'). 2-4 words, confident, scannable.",
            "subheadline"),
        "proof_label": (
            "Write a short social-proof label shown next to a subscriber count, e.g. 'people already "
            "grabbed this guide'. 4-8 words, encouraging, concrete.",
            "subheadline"),
        "benefits_title": (
            "Write a short section title for the 'What you'll discover inside' benefits list. Honest, "
            "benefit-framed, one line under 50 characters.",
            "headline"),
        "benefit": (
            "Write one benefit line a shopper gets from this buying guide. As a concrete outcome or "
            "pain-removed, not a feature. Under 90 characters. Return only the single line.",
            "subheadline"),
        "cta": (
            "Write a strong, low-friction call-to-action button label for a product/affiliate page, e.g. "
            "'Check price on Amazon →'. Imperative, 3-6 words, action-first.",
            "subheadline"),
        "gate_headline": (
            "Write the headline for a free-PDF email lead magnet (reciprocity). Promise the free, useful "
            "guide for the niche, under 60 characters, benefit-led.",
            "headline"),
        "gate_subheadline": (
            "Write the subheadline for a free PDF email opt-in: what the guide contains, how quick it "
            "takes, and a no-spam reassurance. 1-2 short sentences, under 130 characters.",
            "subheadline"),
        "button_text": (
            "Write a short primary button label for an opt-in form, e.g. 'Send me the guide →'. Imperative, "
            "3-6 words, low friction.",
            "subheadline"),
        "faq_question": (
            "Write a question a real shopper of this niche would type into a FAQ (honest, common). One "
            "short question, under 70 characters.",
            "subheadline"),
        "faq_answer": (
            "Write a short, honest, trust-building FAQ answer about how you pick/rank products. Transparent "
            "(we rank live Amazon data; links are affiliate and price never changes). 2-3 clear sentences.",
            "chapter"),
        "testimonial": (
            "Write one believable first-person customer-style testimonial for a buying-guide page: specific, "
            "shows a before/after or a pain solved. Do NOT fabricate hard numbers, ratings or metric claims. "
            "1-2 sentences.",
            "chapter"),
        "urgency_headline": (
            "Write a short, honest status/scarcity headline (e.g. 'Amazon prices move daily') without fake "
            "countdown fear. Under 55 characters.",
            "headline"),
        "urgency_sub": (
            "Write a short sentence reinforcing that the niche product is competitively priced now but "
            "stock/deals shift — honest, not manipulative. Under 110 characters.",
            "subheadline"),
        "guarantee_headline": (
            "Write a short headline for a trust/guarantee section (e.g. 'Our pick promise'). Under 45 "
            "characters, reassuring.",
            "headline"),
        "guarantee_body": (
            "Write 2-3 honest, reassuring sentences for a trust section: we surface listings real buyers "
            "keep choosing, we're transparent we don't test products ourselves, and Amazon returns protect "
            "the buyer.",
            "chapter"),
        "methodology_title": (
            "Write a short title for a 'How we pick' transparency section (e.g. 'How we pick'). Under 40 "
            "characters.",
            "headline"),
        "methodology_body": (
            "Write 2-3 honest sentences for a methodology section: we pull live Amazon data and score "
            "products on demand, rating, review volume with a pricing nudge; no placement is for sale. "
            "Specific and credible.",
            "chapter"),
        "spotlight_cta": (
            "Write a CTA label for a product spotlight on an affiliate page (imperative, 3-5 words, e.g. "
            "'See it on Amazon →').",
            "subheadline"),
        "status": (
            "Write a short, professional status/note line for this niche context. One concise sentence.",
            "subheadline"),
        "generic": (
            "Write professional, specific, on-brand copy for this field, appropriate for an Amazon-affiliate "
            "buying guide. Plain text, no markdown, no intro lines.",
            "subheadline"),
    }

    # Map every CMS (section_type, field) to the AI brief that fits its job, so
    # each field gets copy written for its exact purpose (headline vs. benefit
    # vs. FAQ answer vs. CTA). Omitted fields are structural/visual and get none.
    _AI_FILL_FIELDS = {
        ("hero", "headline"): "headline",
        ("hero", "subheadline"): "subheadline",
        ("hero", "badge_text"): "badge",
        ("social_proof", "proof_label"): "proof_label",
        ("benefits", "title"): "benefits_title",
        ("product_spotlight", "cta_text"): "spotlight_cta",
        ("email_gate", "headline"): "gate_headline",
        ("email_gate", "subheadline"): "gate_subheadline",
        ("email_gate", "button_text"): "button_text",
        ("email_gate", "privacy_text"): "generic",
        ("urgency", "headline"): "urgency_headline",
        ("urgency", "subheadline"): "urgency_sub",
        ("guarantee", "headline"): "guarantee_headline",
        ("guarantee", "subheadline"): "guarantee_body",
        ("methodology", "title"): "methodology_title",
        ("methodology", "body"): "methodology_body",
        ("cta_band", "headline"): "headline",
        ("cta_band", "subheadline"): "subheadline",
        ("cta_band", "button_text"): "button_text",
        ("testimonials", "title"): "generic",
        ("faq", "title"): "generic",
    }

    def _ai_fill(self):
        body = self._body()
        niche = (body.get("niche") or "").strip()
        field = (body.get("field") or "generic").strip().lower()
        current = (body.get("current") or "").strip()
        hint = (body.get("hint") or "").strip()
        brief, template = self._AI_FIELD_BRIEFS.get(field, self._AI_FIELD_BRIEFS["generic"])
        if not niche:
            return self._send(400, {"error": "missing niche"})
        if not ai.configured():
            return self._send(200, {
                "ok": False, "configured": False,
                "text": "",
                "error": ("No AI provider key is set — add one under /keys (a free "
                          "provider works) to enable one-click AI autofill.")})
        extra = hint
        if current:
            extra = (extra + " | improve on this draft: " + current) if extra else \
                ("improve on this draft: " + current)
        try:
            lines = ai.generate(template, niche, brief + ((" | " + extra) if extra else ""))
        except Exception:
            lines = []
        if not lines:
            return self._send(200, {
                "ok": False, "configured": True, "text": "",
                "error": "The AI provider didn't return usable copy — try again, or check the key under /keys."})
        if field in ("benefit",):
            return self._send(200, {"ok": True, "text": lines[0]})
        return self._send(200, {"ok": True, "text": "\n".join(lines)})

    # ------------------------------------------------------------------ admin auth
    def _cookie_token(self, cookie=_COOKIE):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith(cookie + "="):
                return part[len(cookie) + 1:]
        return None

    def _authed(self):
        tok = self._cookie_token()
        if not tok:
            return False
        with _lock:
            entry = _SESSIONS.get(tok)
            if entry is None:
                return False
            exp, uid = entry
            if exp < time.monotonic():
                _SESSIONS.pop(tok, None)
                return False
            if uid is None:  # the env owner
                return True
        return _user_row_by_id(uid) is not None

    def _session_uid(self):
        """Session's user id, or None (owner) when this session is the owner's."""
        entry = _SESSIONS.get(self._cookie_token())
        return None if not entry else entry[1]

    def _current_user(self):
        """{owner:bool, email, name, roles, status} for the session, or None."""
        uid = self._session_uid()
        if uid is None:
            if not self._authed():
                return None
            return {"owner": True, "email": _ADMIN_EMAIL, "name": "Owner",
                    "roles": ["owner"], "status": "active"}
        row = _user_row_by_id(uid)
        if not row:
            return None
        try:
            roles = json.loads(row.get("roles") or "[]")
        except ValueError:
            roles = []
        return {"owner": False, "email": row["email"], "name": row["name"] or "",
                "roles": roles, "status": row.get("status") or "unverified"}

    def _granted_functions(self):
        """Set of function slugs this session may use; owner gets everything."""
        if not self._authed():
            return set()
        if self._session_uid() is None:
            return set(ALL_FUNCTIONS)
        row = _user_row_by_id(self._session_uid())
        if not row or row.get("status") != "verified":
            return set()
        try:
            roles = json.loads(row.get("roles") or "[]")
        except ValueError:
            roles = []
        out = set()
        for slug in roles:
            for f in _role_functions(slug):
                out.add(f)
        return out

    @staticmethod
    def _function_for_path(path):
        """Function slug covering `path`, or None when no function owns it."""
        for fn, prefixes in FUNCTION_PATHS.items():
            for p in prefixes:
                if path == p or (p.endswith("/") and path.startswith(p)):
                    return fn
        return None

    def _function_denied(self, path):
        """True when this session must not reach `path`. Public paths and the
        owner pass; anything else is allowed only if a granted function owns it
        (unlisted sections like /admin/users stay owner-only)."""
        if path in ("/admin", "/admin/login", "/admin/logout", "/admin/register",
                    "/admin/verify", "/admin/resend", "/admin/pending",
                    "/admin/forgot-password", "/admin/reset-password"):
            return False
        if self._session_uid() is None:  # owner: everything
            return False
        fn = self._function_for_path(path)
        if fn is None:
            return True
        return fn not in self._granted_functions()

    def _function_error(self, path):
        if path.startswith("/api/"):
            return self._send(403, {"error": "forbidden", "auth": True})
        msg = ("This account doesn't have access to that tool (no role with the "
               "<b>%s</b> function assigned). Ask the owner to enable it under "
               '<a href="/admin/users">Users &amp; roles</a>.' % seo._clean(
                   FN.get(self._function_for_path(path), "required")))
        return self._send(403, ("<html><body><main class='wrap'><section class='card'>"
                                "<h1>403 · not allowed</h1><p>%s</p>"
                                '<p><a href="/admin/pending">My access</a> · '
                                '<a href="/admin/logout">Log out</a></p>'
                                "</section></main></body></html>" % msg).encode("utf-8"),
                          "text/html; charset=utf-8")

    def _cron_ok(self):
        secret = (self.headers.get("X-Cron-Secret") or "").strip()
        if not secret:
            parsed = urllib.parse.urlsplit(self.path)
            return bool(_CRON_SECRET) and any(
                hmac.compare_digest(v, _CRON_SECRET)
                for v in urllib.parse.parse_qs(parsed.query).get("cron_secret", []))
        return bool(_CRON_SECRET) and hmac.compare_digest(secret, _CRON_SECRET)

    def _new_session(self, uid=None):
        tok = secrets.token_hex(32)
        with _lock:
            for old, entry in list(_SESSIONS.items()):
                if entry[0] < time.monotonic():
                    _SESSIONS.pop(old, None)
            _SESSIONS[tok] = (time.monotonic() + _SESSION_TTL, uid)
        return tok

    def _drop_session(self, tok):
        if tok:
            with _lock:
                _SESSIONS.pop(tok, None)

    def _set_cookie(self, tok, max_age=_SESSION_TTL, cookie=_COOKIE, path="/", secure=None):
        if secure is None:
            secure = self._is_secure()
        self.send_header("Set-Cookie", "%s=%s; Path=%s; HttpOnly; SameSite=Lax; Max-Age=%d%s"
                         % (cookie, tok, path, max_age, "; Secure" if secure else ""))

    def _prelim_guard(self):
        """Flood/DoS pre-checks shared by every request. Returns the client key
        when the request may proceed, else sends the rejection and returns None."""
        if len(self.path) > security.MAX_URL:
            self._send(414, {"error": "URI too long"})
            return None
        key = security.client_key(self.headers, self._client_ip())
        if not security.HTTP_LIMITER.hit(key):
            self._send(429, {"error": "too many requests"})
            return None
        if self.path.startswith("/api/") and not security.API_LIMITER.hit("api|" + key):
            self._send(429, {"error": "too many requests"})
            return None
        return key

    def _needs_admin(self, path):
        return (path.startswith("/api/") or path.startswith("/keys/") or
                path.startswith("/admin/") or
                path in ("/admin", "/dashboard", "/index.html", "/tool", "/keys"))

    def _redirect_login(self, next_path):
        loc = "/admin/login?next=" + urllib.parse.quote(next_path or "/dashboard", safe="")
        self.send_response(302)
        self.send_header("Location", loc)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return None

    def _redirect_path(self, url):
        url = (url or "/dashboard")
        if not (url.startswith("/") and not url.startswith("//")):
            url = "/dashboard"
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return None

    def _login_page(self, error=None, oauth_error=None):
        err = ('<p class="msg" style="color:#d64545">%s</p>' % seo._clean(error or oauth_error)) if (error or oauth_error) else ""
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        banner = ""
        if q.get("sent"):
            banner = '<p class="msg bake" style="color:#2e7d32">Account requested — check your inbox and click the confirmation link.</p>'
        elif q.get("forgot"):
            banner = '<p class="msg bake" style="color:#2e7d32">If an account exists for that email, a reset link is on its way — check your inbox.</p>'
        elif q.get("reset"):
            banner = '<p class="msg bake" style="color:#2e7d32">Password updated — sign in with your new password.</p>'
        elif q.get("verified"):
            banner = '<p class="msg bake" style="color:#2e7d32">Email verified! Sign in below (the owner still needs to grant your access roles).</p>'
        elif q.get("already"):
            banner = "<p class=\"msg bake\" style=\"color:#d64545\">That link isn't for a pending account — it may already be verified or was deleted.</p>"
        elif q.get("baderr"):
            banner = "<p class=\"msg bake\" style=\"color:#d64545\">That confirmation link is invalid or expired — use “resend” below.</p>"
        route = {"google": "google", "facebook": "fb"}
        prov = oauth.providers_configured()
        if prov:
            oauth_html = ('<div class="oauth">%s</div><p class="divider">— or sign in with email —</p>'
                          % "".join(
                              '<a class="btn oauth-btn" href="/admin/oauth/%s">%s ↗</a>'
                              % (route[p], desc) for p, _name, desc in prov))
        else:
            oauth_html = ""
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Admin login — pstore</title><link rel="stylesheet" href="/style.css">
<style>.login-wrap{{min-height:78vh;display:flex;align-items:center;justify-content:center;padding:24px}}
.login-card{{width:100%;max-width:380px;text-align:center}}
.login-card h1{{font-size:26px;letter-spacing:-.4px}}
.login-card input{{width:100%;padding:13px 16px;border:1px solid var(--border);border-radius:14px;font-size:15px;margin:8px 0 12px;background:#fff}}
.login-card button{{width:100%;margin-top:4px}}
.login-hint{{font-size:12.5px;color:var(--muted);margin-top:14px}}
.login-hint a{{color:var(--accent)}}
.oauth{{display:flex;flex-direction:column;gap:8px;margin:14px 0 4px}}
.oauth-btn{{background:#fff;color:var(--text);border:1px solid var(--border);box-shadow:none}}
.oauth-btn:hover{{transform:translateY(-2px);box-shadow:var(--shadow)}}
.divider{{color:var(--muted);font-size:12px;font-weight:600;letter-spacing:.3px}}
.resend{{margin-top:10px;font-size:12.5px;color:var(--muted)}}
.resend button{{background:none;border:none;color:var(--accent);cursor:pointer;font-size:12.5px;padding:0}}
.bake{{font-size:13px}}</style>
</head><body>
<header><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a></header>
<main class="login-wrap"><div class="login-card">
<section class="card">
<h1>🔐 Admin <span style="color:var(--accent)">login</span></h1>
<p class="tagline" style="margin:0">Owner or team access to the tools, pages and keys.</p>
{banner}
{err}
{oauth_html}
<label style="display:block;text-align:left;font-size:12.5px;color:var(--muted);font-weight:700">Email
<input id="em" type="email" placeholder="you@example.com" autocomplete="username"></label>
<label style="display:block;text-align:left;font-size:12.5px;color:var(--muted);font-weight:700">Password
<input id="pw" type="password" placeholder="password" autocomplete="current-password"></label>
<div style="text-align:right;margin:-2px 0 2px"><a href="/admin/forgot-password" style="font-size:12px;color:var(--accent)">Forgot your password?</a></div>
<button id="go" class="warm">Unlock admin</button>
<p id="msg" class="msg"></p>
<p class="login-hint">Public site: <a href="/">pstore home</a> · no login needed.</p>
<p class="resend">Team member? <a href="/admin/register">Request an account</a> ·
<button id="resend">resend my confirmation link</button></p>
</section></div></main>
<script>
function $(id){{return document.getElementById(id);}}
$("go").onclick = async () => {{
  const next = new URLSearchParams(location.search).get("next") || "/dashboard";
  const r = await fetch("/admin/login", {{method:"POST", headers:{{"Content-Type":"application/json"}},
    body: JSON.stringify({{email: $("em").value.trim(), password: $("pw").value, next: next}})}});
  const d = await r.json().catch(()=>({{ok:false, error:"bad response"}}));
  if (d.ok) location.href = d.next;
  else $("msg").textContent = d.error || "Login failed.";
}};
$("resend").onclick = async () => {{
  const r = await fetch("/admin/resend", {{method:"POST", headers:{{"Content-Type":"application/json"}},
    body: JSON.stringify({{email: $("em").value.trim()}})}});
  const d = await r.json().catch(()=>({{ok:false, error:"bad response"}}));
  $("msg").textContent = d.alert || d.error || "Resent.";
}};
$("pw").addEventListener("keydown", e => {{ if (e.key === "Enter") $("go").onclick(); }});
</script>
</body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _login_post(self):
        key = "login|" + security.client_key(self.headers, self._client_ip())
        if not security.LOGIN_LIMITER.hit(key):
            return self._send(429, {"error": "too many login attempts, try again later"})
        body = self._body()
        email = str(body.get("email") or "").strip().lower()
        pw = str(body.get("password") or "")
        next_path = str(body.get("next") or "/dashboard")
        if not next_path.startswith("/") or next_path.startswith("//"):
            next_path = "/dashboard"
        uid = None
        # owner login (env credentials)
        if (hmac.compare_digest(email.encode("utf-8"), _ADMIN_EMAIL.encode("utf-8"))
                and hmac.compare_digest(pw.encode("utf-8"), _ADMIN_PW.encode("utf-8"))):
            security.LOGIN_LIMITER.clear(key)
            tok = self._new_session()  # uid None = owner
            return self._fail_or_ok(None, tok, next_path)
        # team login (users table)
        row = _user_row(email)
        if not row or not security.verify_password(pw, row.get("pass_hash")):
            return self._send(401, {"ok": False, "error": "Wrong email or password"})
        if row.get("status") == "disabled":
            return self._send(401, {"ok": False,
                                    "error": "This account has been disabled — contact the owner."})
        if row.get("status") != "verified":
            return self._send(401, {"ok": False,
                                    "error": "Your email isn't verified yet — check your inbox for the confirmation link."})
        security.LOGIN_LIMITER.clear(key)
        tok = self._new_session(row["id"])
        if not _user_functions(uid=row["id"]):
            next_path = "/admin/pending"
        with _lock:
            conn = _db()
            try:
                conn.execute("UPDATE users SET last_login_at=datetime('now') WHERE id=?",
                             (row["id"],))
                conn.commit()
            finally:
                conn.close()
        return self._fail_or_ok(None, tok, next_path)

    def _fail_or_ok(self, err, tok, next_path):
        data = json.dumps({"ok": err is None, "error": err, "next": next_path}).encode("utf-8")
        self.send_response(200 if err is None else 401)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if tok is not None:
            self._set_cookie(tok)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
        return None

    def _logout(self):
        self._drop_session(self._cookie_token())
        self.send_response(302)
        self.send_header("Location", "/admin/login")
        self._set_cookie("x", max_age=0)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return None

    # --------------------------------------------------- accounts & verification
    @staticmethod
    def _verify_url(email):
        base = os.environ.get("PSTORE_URL", "").rstrip("/") or ""
        tok = security.make_token("verify:" + email, 72 * 3600)
        return "%s/admin/verify?t=%s&e=%s" % (
            base, urllib.parse.quote(tok, safe=""),
            urllib.parse.quote(email, safe=""))

    def _send_verify_email(self, email, name=""):
        """Email the signed confirmation link; True when accepted by the mailer.
        Branded HTML with a plain-text fallback (professional transactional mail)."""
        confirm = self._verify_url(email)
        body = ("Hi %s,\n\nSomeone (probably you) requested a %s team account with "
                "this email. Confirm it to activate the account:\n\n%s\n\nIf you "
                "didn't sign up, ignore this email — the account stays inactive."
                % (name or "there", mailer.STORE_NAME, confirm))
        html = mailer.verify_email_html(confirm, name or email)
        return mailer.send("Confirm your email — activate your %s account"
                           % mailer.STORE_NAME, body, email, html=html)

    @staticmethod
    def _reset_url(email):
        """Fresh, single-use password-reset token stored on the user row (professional:
        a reset link works once and expires after REDIS_RESET_TTL, so a leaked link can't
        be replayed)."""
        base = os.environ.get("PSTORE_URL", "").rstrip("/") or ""
        tok = secrets.token_urlsafe(32)
        return ("%s/admin/reset-password?t=%s&e=%s"
                % (base, urllib.parse.quote(tok, safe=""),
                   urllib.parse.quote(email.strip().lower(), safe=""))), tok

    def _send_reset_email(self, email, name=""):
        """Email a single-use password-reset link; True when accepted by the mailer."""
        url, token = self._reset_url(email)
        expires = int(time.time()) + 3600
        with _lock:
            conn = _db()
            try:
                conn.execute(
                    "UPDATE users SET reset_token=?, reset_expires=? WHERE email=?",
                    (token, str(expires), email.strip().lower()))
                conn.commit()
            finally:
                conn.close()
        body = ("Hi there,\n\nWe received a request to reset the password for your "
                "%s account. If that was you, open the link below to choose a new "
                "password (valid for 1 hour, single use):\n\n%s\n\nIf you didn't "
                "request a reset, ignore this email — your password stays the same."
                % (mailer.STORE_NAME, url))
        html = mailer.reset_email_html(url, name or email)
        return mailer.send("Reset your %s password" % mailer.STORE_NAME, body, email,
                           html=html)

    def _register_page(self, error=None):
        err = ('<p class="msg" style="color:#d64545">%s</p>' % seo._clean(error)) if error else ""
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Request a team account — pstore</title><link rel="stylesheet" href="/style.css">
<style>.login-wrap{{min-height:78vh;display:flex;align-items:center;justify-content:center;padding:24px}}
.login-card{{width:100%;max-width:380px;text-align:center}}
.login-card h1{{font-size:26px;letter-spacing:-.4px}}
.login-card input{{width:100%;padding:13px 16px;border:1px solid var(--border);border-radius:14px;font-size:15px;margin:8px 0 12px;background:#fff}}
.login-card button{{width:100%;margin-top:4px}}
.login-hint{{font-size:12.5px;color:var(--muted);margin-top:14px}}
.login-hint a{{color:var(--accent)}}</style>
</head><body>
<header><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a></header>
<main class="login-wrap"><div class="login-card">
<section class="card">
<h1>📮 Request a <span style="color:var(--accent)">team account</span></h1>
<p class="tagline" style="margin:0">Sign up with your work email. We'll email you a confirmation link, then the owner grants your access.</p>
{err}
<label style="display:block;text-align:left;font-size:12.5px;color:var(--muted);font-weight:700">Your name
<input id="nm" type="text" placeholder="Jane Doe" autocomplete="name"></label>
<label style="display:block;text-align:left;font-size:12.5px;color:var(--muted);font-weight:700">Work email
<input id="em" type="email" placeholder="you@example.com" autocomplete="username"></label>
<label style="display:block;text-align:left;font-size:12.5px;color:var(--muted);font-weight:700">Password (8+ characters)
<input id="pw" type="password" placeholder="password" autocomplete="new-password"></label>
<label style="display:block;text-align:left;font-size:12.5px;color:var(--muted);font-weight:700">Confirm password
<input id="pw2" type="password" placeholder="repeat password" autocomplete="new-password"></label>
<button id="go" class="warm">Request access</button>
<p id="msg" class="msg"></p>
<p class="login-hint">Already have an account? <a href="/admin/login">Sign in</a>.</p>
</section></div></main>
<script>
function $(id){{return document.getElementById(id);}}
$("go").onclick = async () => {{
  if ($("pw").value !== $("pw2").value) {{ $("msg").textContent = "Passwords don't match."; return; }}
  const r = await fetch("/admin/register", {{method:"POST", headers:{{"Content-Type":"application/json"}},
    body: JSON.stringify({{name: $("nm").value.trim(), email: $("em").value.trim(), password: $("pw").value}})}});
  const d = await r.json().catch(()=>({{ok:false, error:"bad response"}}));
  if (d.ok) location.href = "/admin/login?sent=1";
  else $("msg").textContent = d.error || "Couldn't create the account.";
}};
$("pw2").addEventListener("keydown", e => {{ if (e.key === "Enter") $("go").onclick(); }});
</script>
</body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _register_post(self):
        key = "reg|" + security.client_key(self.headers, self._client_ip())
        if not security.REGISTER_LIMITER.hit(key):
            return self._send(429, {"error": "too many signups from this address, try later"})
        body = self._body()
        name = str(body.get("name") or "").strip()[:120]
        email = str(body.get("email") or "").strip().lower()
        pw = str(body.get("password") or "")
        if not email or "@" not in email or "." not in email.split("@")[-1]:
            return self._send(400, {"ok": False, "error": "Enter a valid email address"})
        if len(pw) < 8:
            return self._send(400, {"ok": False, "error": "Password must be at least 8 characters"})
        if _user_row(email):
            return self._send(409, {"ok": False,
                                    "error": "An account with that email already exists."})
        if (hmac.compare_digest(email.encode("utf-8"), _ADMIN_EMAIL.encode("utf-8"))):
            return self._send(400, {"ok": False, "error": "That email belongs to the owner account."})
        with _lock:
            conn = _db()
            try:
                conn.execute(
                    "INSERT INTO users (email, name, pass_hash, status, roles) "
                    "VALUES (?,?,?,'unverified','[]')",
                    (email, name, security.hash_password(pw)))
                conn.commit()
            finally:
                conn.close()
        sent = self._send_verify_email(email, name)
        security.REGISTER_LIMITER.clear(key)
        return self._send(200, {"ok": True, "mail": sent,
                                "error": None,
                                "alert": ("Check your inbox and confirm your email. "
                                          "Until it's verified, you can't sign in yet.")})

    def _verify_login(self):
        """Activation link handler. On success the account is verified AND the
        visitor is signed straight in, then routed to their dashboard by role
        (dashboard function -> /dashboard, otherwise a welcome onboarding page)."""
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        tok = (q.get("t") or [""])[0]
        want = str((q.get("e") or [""])[0]).strip().lower()
        scope = None
        try:
            scope = security.verify_token(urllib.parse.unquote(tok))
        except Exception:
            scope = None
        ok = False
        uid = None
        if scope and scope.startswith("verify:") and scope.split(":", 1)[1] == want and want:
            with _lock:
                conn = _db()
                try:
                    row = conn.execute("SELECT id, status FROM users WHERE email=?",
                                       (want,)).fetchone()
                    if row and row["status"] == "unverified":
                        conn.execute("UPDATE users SET status='verified' WHERE email=?",
                                     (want,))
                        conn.commit()
                        ok = True
                        uid = row["id"]
                finally:
                    conn.close()
        if tok and not scope:
            loc = "/admin/login?baderr=1"
        elif scope and not ok:
            loc = "/admin/login?already=1"
        elif ok and uid:
            # auto-login: fresh session cookie, then straight to the dashboard
            # the user is allowed (pending/onboarding page until roles are granted).
            sess = self._new_session(uid)
            loc = "/dashboard" if "dashboard" in _user_functions(uid=uid) else "/admin/pending?welcome=1"
            self.send_response(302)
            self._set_cookie(sess)
            self.send_header("Location", loc)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return None
        else:
            loc = "/admin/login?verified=1"
        self.send_response(302)
        self.send_header("Location", loc)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return None

    def _resend_post(self):
        key = "resend|" + security.client_key(self.headers, self._client_ip())
        if not security.RESEND_LIMITER.hit(key):
            return self._send(429, {"error": "too many resend requests, try later"})
        body = self._body()
        email = str(body.get("email") or "").strip().lower()
        if not email:
            return self._send(400, {"ok": False, "error": "Enter your email first"})
        row = _user_row(email)
        if not row:
            return self._send(200, {"ok": True,
                                    "alert": "If that account exists, a confirmation link is on its way."})
        if row.get("status") != "unverified":
            return self._send(400, {"ok": False,
                                    "error": ("This account is already verified" if row["status"] == "verified"
                                              else "This account has been disabled.")})
        sent = self._send_verify_email(email, row.get("name") or email)
        return self._send(200, {"ok": sent, "error": None,
                                "alert": "Confirmation link resent — check your inbox."})

    # ------------------------------------------------------- forgot / reset password

    def _forgot_page(self, error=None):
        err = ('<p class="msg" style="color:#d64545">%s</p>' % seo._clean(error)) if error else ""
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Forgot password — pstore</title><link rel="stylesheet" href="/style.css">
<style>.login-wrap{{min-height:78vh;display:flex;align-items:center;justify-content:center;padding:24px}}
.login-card{{width:100%;max-width:380px;text-align:center}}
.login-card h1{{font-size:24px;letter-spacing:-.4px}}
.login-card input{{width:100%;padding:13px 16px;border:1px solid var(--border);border-radius:14px;font-size:15px;margin:8px 0 12px;background:#fff}}
.login-card button{{width:100%;margin-top:4px}}
.login-hint{{font-size:12.5px;color:var(--muted);margin-top:14px}}
.login-hint a{{color:var(--accent)}}</style>
</head><body>
<header><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a></header>
<main class="login-wrap"><div class="login-card">
<section class="card">
<h1>🔑 Forgot <span style="color:var(--accent)">your password?</span></h1>
<p class="tagline" style="margin:0">Enter the email on your account and we'll send you a secure, single-use reset link.</p>
{err}
<label style="display:block;text-align:left;font-size:12.5px;color:var(--muted);font-weight:700">Account email
<input id="em" type="email" placeholder="you@example.com" autocomplete="username"></label>
<button id="go" class="warm">Email me a reset link</button>
<p id="msg" class="msg"></p>
<p class="login-hint">Remembered it after all? <a href="/admin/login">Back to sign in</a>.</p>
</section></div></main>
<script>
function $(id){{return document.getElementById(id);}}
$("go").onclick = async () => {{
  const r = await fetch("/admin/forgot-password", {{method:"POST", headers:{{"Content-Type":"application/json"}},
    body: JSON.stringify({{email: $("em").value.trim()}})}});
  const d = await r.json().catch(()=>({{ok:false, error:"bad response"}}));
  if (d.ok) location.href = "/admin/login?forgot=1";
  else $("msg").textContent = d.error || "Couldn't send the link.";
}};
$("em").addEventListener("keydown", e => {{ if (e.key === "Enter") $("go").onclick(); }});
</script>
</body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _forgot_post(self):
        """Request a password reset. Always answers with a non-revealing message so
        an attacker can't enumerate which emails have accounts (professional practice)."""
        key = "forgot|" + security.client_key(self.headers, self._client_ip())
        if not security.FORGOT_LIMITER.hit(key):
            return self._send(429, {"ok": False,
                                    "error": "Too many requests — wait a few minutes and try again."})
        body = self._body()
        email = str(body.get("email") or "").strip().lower()
        if not email or "@" not in email or "." not in email.split("@")[-1]:
            return self._send(400, {"ok": False, "error": "Enter a valid email address"})
        row = _user_row(email)
        # Owner account is managed via environment credentials — silently skip.
        if (row and row.get("status") != "disabled"
                and not hmac.compare_digest(email.encode("utf-8"), _ADMIN_EMAIL.encode("utf-8"))):
            self._send_reset_email(email, row.get("name") or "")
        security.FORGOT_LIMITER.clear(key)
        return self._send(200, {"ok": True, "error": None,
                                "alert": "If an account exists for that email, a reset link is on its way."})

    @staticmethod
    def _reset_peek(tok, email):
        """Validate a single-use reset link WITHOUT consuming it (used to render
        the form without burning the token on a plain page view)."""
        if not tok or not email:
            return False
        with _lock:
            conn = _db()
            try:
                row = conn.execute(
                    "SELECT reset_token, reset_expires FROM users WHERE email=?",
                    (email.strip().lower(),)).fetchone()
                if not row or not row["reset_token"]:
                    return False
                try:
                    expired = int(row["reset_expires"] or "0") < time.time()
                except (TypeError, ValueError):
                    expired = True
                return (not expired
                        and hmac.compare_digest(str(row["reset_token"]), tok))
            finally:
                conn.close()

    @staticmethod
    def _reset_claim(tok, email):
        """Validate + consume a single-use reset link. Returns the user id on success
        (token cleared), else None. Expired, used or forged links all fail."""
        if not Handler._reset_peek(tok, email):
            return None
        with _lock:
            conn = _db()
            try:
                row = conn.execute(
                    "SELECT id FROM users WHERE reset_token=? AND email=?",
                    (tok, email.strip().lower())).fetchone()
                if not row:
                    return None
                uid = row["id"]
                conn.execute("UPDATE users SET reset_token=NULL, reset_expires=NULL WHERE id=?",
                             (uid,))
                conn.commit()
                return uid
            finally:
                conn.close()

    def _reset_page(self, error=None):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        tok = (q.get("t") or [""])[0]
        email = str((q.get("e") or [""])[0]).strip().lower()
        if not self._reset_peek(tok, email):
            bad = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Link expired — pstore</title><link rel="stylesheet" href="/style.css">
<style>.login-wrap{{min-height:78vh;display:flex;align-items:center;justify-content:center;padding:24px}}
.login-card{{width:100%;max-width:380px;text-align:center}}</style>
</head><body>
<header><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a></header>
<main class="login-wrap"><div class="login-card"><section class="card">
<h1>🕓 This link is no longer valid</h1>
<p class="tagline">Password reset links expire after 1 hour and can only be used once. If it expired or was already used, just ask for a new one — takes a few seconds.</p>
<p><a class="btn warm" href="/admin/forgot-password">Request a new reset link</a></p>
<p class="login-hint">Or <a href="/admin/login">sign in</a>.</p>
</section></div></main></body></html>"""
            return self._send(200, bad.encode("utf-8"), "text/html; charset=utf-8")
        err = ('<p class="msg" style="color:#d64545">%s</p>' % seo._clean(error)) if error else ""
        tok_json = json.dumps(tok)
        email_json = json.dumps(email)
        reset_js = ("const r = await fetch(\"/admin/reset-password\", "
                    "{method:\"POST\", headers:{\"Content-Type\":\"application/json\"}, "
                    "body: JSON.stringify({t: %s, e: %s, password: $(\"pw\").value})});"
                    % (tok_json, email_json))
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Choose a new password — pstore</title><link rel="stylesheet" href="/style.css">
<style>.login-wrap{{min-height:78vh;display:flex;align-items:center;justify-content:center;padding:24px}}
.login-card{{width:100%;max-width:380px;text-align:center}}
.login-card h1{{font-size:24px;letter-spacing:-.4px}}
.login-card input{{width:100%;padding:13px 16px;border:1px solid var(--border);border-radius:14px;font-size:15px;margin:8px 0 12px;background:#fff}}
.login-card button{{width:100%;margin-top:4px}}
.login-hint{{font-size:12.5px;color:var(--muted);margin-top:14px}}</style>
</head><body>
<header><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a></header>
<main class="login-wrap"><div class="login-card">
<section class="card">
<h1>🛡️ Choose a <span style="color:var(--accent)">new password</span></h1>
<p class="tagline" style="margin:0">For <b>{{email}}</b>. At least 8 characters.</p>
{err}
<label style="display:block;text-align:left;font-size:12.5px;color:var(--muted);font-weight:700">New password
<input id="pw" type="password" placeholder="new password" autocomplete="new-password"></label>
<label style="display:block;text-align:left;font-size:12.5px;color:var(--muted);font-weight:700">Confirm new password
<input id="pw2" type="password" placeholder="repeat new password" autocomplete="new-password"></label>
<button id="go" class="warm">Save new password</button>
<p id="msg" class="msg"></p>
</section></div></main>
<script>
function $(id){{return document.getElementById(id);}}
$("go").onclick = async () => {{
  if ($("pw").value !== $("pw2").value) {{ $("msg").textContent = "Passwords don't match."; return; }}
  {reset_js}
  const d = await r.json().catch(()=>({{ok:false, error:"bad response"}}));
  if (d.ok) location.href = "/admin/login?reset=1";
  else $("msg").textContent = d.error || "Couldn't update the password.";
}};
$("pw2").addEventListener("keydown", e => {{ if (e.key === "Enter") $("go").onclick(); }});
</script>
</body></html>"""
        body = body.replace("{email}", seo._clean(email))
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _reset_post(self):
        key = "reset|" + security.client_key(self.headers, self._client_ip())
        if not security.RESET_LIMITER.hit(key):
            return self._send(429, {"ok": False,
                                    "error": "Too many attempts — wait a few minutes and try again."})
        body = self._body()
        tok = str(body.get("t") or "")
        email = str(body.get("e") or "").strip().lower()
        pw = str(body.get("password") or "")
        if len(pw) < 8:
            return self._send(400, {"ok": False, "error": "Password must be at least 8 characters"})
        uid = self._reset_claim(tok, email)
        if uid is None:
            return self._send(400, {"ok": False,
                                    "error": "This reset link is invalid, expired or already used."})
        with _lock:
            conn = _db()
            try:
                conn.execute("UPDATE users SET pass_hash=? WHERE id=?",
                             (security.hash_password(pw), uid))
                conn.commit()
            finally:
                conn.close()
        return self._send(200, {"ok": True, "error": None,
                                "alert": "Password updated — sign in with your new password."})

    def _pending_page(self):
        user = self._current_user()
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        welcome = '<p class="msg bake" style="color:#2e7d32">Your email is verified — welcome aboard! ' \
                  'The owner needs to grant your access roles before the tools unlock for you.</p>' \
            if q.get("welcome") else ""
        funcs = sorted(self._granted_functions(), key=lambda f: ALL_FUNCTIONS.index(f)
                       if f in ALL_FUNCTIONS else 99)
        if not funcs:
            chips = ('<p class="tagline">No tool access yet. The owner assigns a '
                     'role (e.g. <b>Operator</b> or <b>Full access</b>) under '
                     '<b>Users &amp; roles</b>.</p>')
        else:
            chips = '<div class="page-grid">%s</div>' % "".join(
                ('<span class="btn ghost" style="width:100%%;text-align:left">'
                 '✅ %s</span>' % seo._clean(FN[f])) for f in funcs)
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>My access — pstore</title><link rel="stylesheet" href="/style.css">
</head><body>
<header><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<nav class="mininav"><a href="/admin">All pages</a><a href="/admin/logout">Logout</a></nav></header>
<main class="wrap"><section class="card">
{welcome}
<h1>👋 Welcome, {seo._clean(user.get('name') or user.get('email') or '')}</h1>
<p class="tagline">Email <b>{seo._clean(user['email'])}</b> is verified{((' · ' + ' · '.join(
    '<b>%s</b>' % seo._clean(r) for r in user.get('roles') or [])) if user.get('roles') else '')}.</p>
<h2 style="margin-top:22px">Function access</h2>
{chips}
<p class="login-hint">Missing something? The owner enables functions by assigning you roles at
<a href="/admin/users">Users &amp; roles</a> — reach out to them.</p>
</section></main></body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _admin_nav(self, active=None):
        granted = self._granted_functions()
        is_owner = self._session_uid() is None

        def chip(href, label, key, accent=False):
            cls = ' class="primary"' if accent else (" class=\"%s\"" % key if key == active else "")
            return '<a href="%s"%s>%s</a>' % (href, cls, label)
        def group(title, items):
            parts = []
            for it in items:
                href, label, key = it[0], it[1], it[2]
                accent = it[3] if len(it) > 3 else False
                if not is_owner:
                    if key == "users":
                        continue
                    fn = NAV_FN.get(key)
                    if fn is not None and fn not in granted:
                        continue
                parts.append(chip(href, label, key, accent))
            if not parts:
                return ""
            return '<div class="navgroup titles">%s</div>%s' % (title, "".join(parts))
        groups = [
            ("Find",
             [("/dashboard", "🧭 Dashboard", "dashboard"),
              ("/tool", "🛠 Tools", "tool"),
              ("/admin/opportunities", "📈 Grow", "opportunities"),
              ("/admin/priority", "💰 Prioritize", "priority"),
              ("/admin/sem", "🎯 SEM", "sem")]),
            ("Build",
             [("/admin/seo", "🔍 SEO", "seo"),
              ("/admin/seoengines", "🔎 Engines", "seoengines"),
              ("/admin/cms", "🧩 Lead pages", "cms"),
              ("/admin/ebooks", "📕 Ebooks", "ebooks"),
              ("/admin/refresh", "📡 Refresh", "refresh")]),
            ("Market",
             [("/admin/funnel", "⚙️ Funnel", "funnel", True),
              ("/admin/marketing", "📊 ROI", "marketing"),
              ("/admin/emails", "📨 Email Studio", "emails"),
              ("/admin/social", "📣 Social", "social"),
              ("/admin/variants", "⚗️ A/B", "variants"),
              ("/admin/segments", "🎚 Lead segments", "segments"),
              ("/admin/pricedrop", "🏷 Price drops", "pricedrop"),
              ("/keys", "🔑 Keys", "keys"),
              ("/admin/apikeys", "🔌 API Keys", "apikeys")]),
            ("Analyze",
             [("/admin/analytics", "📈 Analytics", "analytics"),
              ("/admin/backup", "💾 Backup", "backup")]),
            ("Operate",
             [("/admin/manual", "📖 Manual", "manual"),
              ("/admin/users", "👥 Users & roles", "users"),
              ("/admin", "🗺 All pages", "admin", True),
              ("/admin/logout", "⎋ Logout", "logout")]),
        ]
        nav_groups_html = "\n".join(
            group(ti, items) for ti, items in groups)
        return f"""<nav>
{nav_groups_html}
</nav>"""

    def _admin_page(self):
        """Admin hub: a button for every page on the site — admin tools, the
        public site (static + every saved niche + landing page) and the APIs."""
        def btn(href, label, note=None, ghost=False):
            note_html = '<span class="n">%s</span>' % seo._clean(note) if note else ""
            return ('<a href="%s" %s>%s %s</a>'
                    % (seo._clean(href), ('class="btn ghost" style="width:100%"' if ghost else 'class="btn" style="width:100%"'),
                       seo._clean(label), note_html))

        def section(title, items):
            return ('<section class="card"><h2>%s</h2><div class="page-grid">%s</div></section>'
                    % (seo._clean(title), "".join(items)))

        granted = self._granted_functions()
        is_owner = self._session_uid() is None

        def ok(href):
            """Hub visibility: owner sees all admin tooling; team users see the
            tools matching their granted functions plus the public site."""
            if is_owner:
                return True
            fn = self._function_for_path(href)
            if fn is None:
                return (href.startswith("/") and not href.startswith("/admin/")
                        and not href.startswith("/api/") and not href.startswith("/keys"))
            return fn in granted

        def section(title, items):
            rows = []
            for href, label, note in items:
                if ok(href):
                    rows.append(btn(href, label, note))
            if not rows:
                return ""
            return ('<section class="card"><h2>%s</h2><div class="page-grid">%s</div></section>'
                    % (seo._clean(title), "".join(rows)))

        find_section = section("🧭 Find — idea to niche", [
            ("/dashboard", "🧭 Niche finder dashboard", "admin"),
            ("/admin/opportunities", "📈 Grow — opportunities", "long-tail tree"),
            ("/admin/priority", "💰 Prioritize — earnings", "commission + clicks"),
            ("/admin/sem", "🎯 Search-intent (SEM)", "keyword briefs")])

        build_section = section("🛠 Build — content & pages", [
            ("/admin/seo", "🔍 SEO audit", "indexability + schema"),
            ("/admin/seoengines", "🔎 Search-engine consoles", "GSC · Bing · Yandex"),
            ("/admin/cms", "🧩 Lead page CMS", "edit sections &amp; style"),
            ("/admin/ebooks", "📕 AI ebook generator", "PDF lead magnet"),
            ("/admin/refresh", "📡 Data refresh", "manual + auto re-mine")])

        market_section = section("🚀 Market — channels & conversion", [
            ("/admin/funnel", "⚙️ Real sales funnel", "data-backed stages"),
            ("/admin/marketing", "📊 Marketing ROI", "email + social + traffic"),
            ("/admin/emails", "📨 Email Studio", "compose, switches & scheduling"),
            ("/admin/social", "📣 Social publishing", "tracked posts"),
            ("/admin/variants", "⚗️ A/B headline tests", "per-niche split test"),
            ("/admin/segments", "🎚 Lead lifecycle segments", "hot / warm / cold"),
            ("/admin/pricedrop", "🏷 Price-drop deal engine", "scarcity pushes"),
            ("/tool", "🛠 One-click marketing suite", "launch everything"),
            ("/keys", "🔑 Keys &amp; endpoints", "admin"),
            ("/admin/apikeys", "🔌 API keys page", "PA-API + social")])

        analyze_section = section("📈 Analyze — results & growth", [
            ("/admin/analytics", "📊 Click tracking &amp; analytics", "beacons")])

        operate_section = section("🧰 Operate — run the site", [
            ("/admin/manual", "📖 User manual", "visual + PDF guide"),
            ("/admin/users", "👥 Users &amp; roles", "team + permissions"),
            ("/admin/pending", "🔎 My access", "functions &amp; roles"),
            ("/admin/logout", "⎋ Log out", "session")])

        for pid, meta in amazon._SCRAPER_PROVIDERS.items():
            if ok("/keys/" + pid):
                operate_section += '<section class="card"><h2>🔌 %s</h2><div class="page-grid">%s</div></section>' % (
                    seo._clean(meta["name"]) + " key",
                    btn("/keys/" + seo._clean(pid), meta["name"] + " key", pid))
        tools_html = find_section + build_section + market_section + analyze_section + operate_section

        site = [btn("/", "🏠 Home / landing", "public"),
                btn("/sitemap.xml", "🗺 Sitemap", "xml"),
                btn("/robots.txt", "🤖 Robots", "txt")]
        key_path = "/%s.txt" % indexnow.key()
        site.append(btn(key_path, "🔑 IndexNow key file", "txt"))
        for slug in seo.STATIC_PAGES:
            site.append(btn("/" + slug, slug.title() + " page", slug))
        site_html = "".join(site)

        niches = []
        for n in self._all_niches():
            kw = n["keyword"]
            try:
                slug = seo._slugify(kw)
            except Exception:
                slug = "niche"
            niches.append('<div class="pair">%s%s</div>'
                          % (btn("/n/" + slug, kw, "review"),
                             btn("/lp/" + slug, "lp", "landing", ghost=True)))
        niches_html = "".join(niches) if niches else '<p class="hint">No saved niches yet — mine one on the dashboard.</p>'

        api_html = "".join('<a class="pill" href="%s">%s</a>' % (seo._clean(e), seo._clean(e))
                           for e in ("/api/settings", "/api/niches", "/api/tools",
                                     "/api/tools/launch",
                                     "/api/mine", "/api/search", "/api/autosuggest",
                                     "/api/indexnow", "/api/subscribers", "/api/sequence/send",
                                     "/api/mail",
                                     "/api/social", "/api/social/publish",
                                     "/api/sem", "/api/seo-audit")
                           if ok(e))
        api_html = api_html or '<p class="hint">No API endpoints for your functions.</p>'
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Admin — all pages · pstore</title>
<link rel="stylesheet" href="/style.css">
<style>.page-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:10px;margin:6px 0 2px}}
.page-grid .pair{{display:contents}}
.page-grid a{{margin:0}}
.page-grid a.btn.ghost{{background:transparent;color:var(--accent);border:1px solid var(--accent);box-shadow:none}}
.pill{{display:inline-block;font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--accent);
border:1px solid var(--border);border-radius:999px;padding:5px 11px;margin:3px 4px 3px 0;text-decoration:none;background:#fff}}
.pill:hover{{border-color:var(--accent)}}
.api-label{{margin-top:16px}}</style>
</head><body>
<header id="top"><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>All pages, <span>one hub.</span></h1>
<p class="tagline">Every button opens a page of pstore — owner tools first, then the full public site and its APIs.</p></div>
{self._admin_nav('admin')}
</header>
<main>
{tools_html}
<section class="card"><h2>🌐 Public site — global pages</h2><div class="page-grid">{site_html}</div></section>
<section class="card"><h2>🛍 Public site — saved niches</h2>
<p class="hint">One button per saved niche (top = review page, bottom = its landing page). Open in the same tab — use Back to return.</p>
<div class="page-grid">{niches_html}</div></section>
<section class="card"><h2>📡 API endpoints</h2>
<p class="hint">Read-only links; these answer JSON only when this browser holds a valid admin session.</p>
<div class="api-label">{api_html}</div></section>
</main>
<footer><p>Admin hub — never indexed. <a href="/admin/logout">Log out</a> when done.</p></footer>
{_TOTOP}
</body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _oauth(self, provider, callback, q):
        configured = {p for p, _n, _d in oauth.providers_configured()}
        if provider not in configured:
            return self._send(404, {"error": "oauth provider not configured"})
        if not callback:
            # Start: sign a short-lived state token, stash it, bounce to provider.
            state = security.make_token("oauth:" + provider, 600)
            url = oauth.authorize_url(provider, state)
            if not url:
                return self._send(404, {"error": "oauth provider not configured"})
            self.send_response(302)
            self._set_cookie(state, max_age=600, cookie=_OAUTH_COOKIE, path="/admin/oauth")
            self.send_header("Location", url)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return None
        # Callback: verify state, exchange the code, grant only to the admin mail.
        code = (q.get("code") or [""])[0]
        state = (q.get("state") or [""])[0]
        expect = self._cookie_token(_OAUTH_COOKIE)
        scope = security.verify_token(expect) if expect else None
        if not code or not expect or not hmac.compare_digest(expect, state) or scope != "oauth:" + provider:
            self._set_cookie("x", max_age=0, cookie=_OAUTH_COOKIE, path="/admin/oauth")
            return self._login_page(oauth_error="Sign-in link was stale or tampered with — try again.")
        try:
            email, _name = oauth.exchange(provider, code)
        except Exception:
            return self._login_page(oauth_error="OAuth sign-in failed, please try again.")
        if not email or email != _ADMIN_EMAIL.lower():
            return self._login_page(oauth_error="This account is not authorized to administer pstore.")
        tok = self._new_session()
        self.send_response(302)
        self._set_cookie(tok)
        self.send_header("Location", "/dashboard")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return None

    def do_GET(self):
        if self._prelim_guard() is None:
            return
        acquired = security.CONCURRENCY.acquire(timeout=0.05)
        try:
            if not acquired:
                return self._send(503, {"error": "busy, retry shortly"})
            return self._dispatch_get()
        finally:
            if acquired:
                security.CONCURRENCY.release()

    def _dispatch_get(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)
        try:
            # public auth flow — login/logout/register/verify/reset never need a session
            if path == "/admin/login":
                if self._authed():
                    return self._redirect_path(q.get("next", ["/dashboard"])[0])
                return self._login_page()
            if path == "/admin/logout":
                return self._logout()
            if path == "/admin/register":
                return self._register_page()
            if path == "/admin/verify":
                return self._verify_login()
            if path == "/admin/forgot-password":
                return self._forgot_page()
            if path == "/admin/reset-password":
                return self._reset_page()
            if path == "/admin/resend":
                return self._send(405, {"error": "method not allowed"})
            oauth_route = re.match(r"^/admin/oauth/(google|fb)(/callback)?$", path)
            if oauth_route:
                provider = "google" if oauth_route.group(1) == "google" else "facebook"
                return self._oauth(provider, callback=bool(oauth_route.group(2)), q=q)
            eng_oauth = re.match(r"^/admin/oauth/seoengines/cb/(gsc|yandex)$", path)
            if eng_oauth:
                return self._seoengine_oauth_cb(eng_oauth.group(1), q)
            # public opt-out + click beacons never need a session
            if path.startswith("/unsubscribe"):
                return self._unsubscribe(q)
            if path == "/api/track":
                return self._track_click()
            if path == "/api/pageview":
                return self._page_view()
            if path == "/_gated/pdf":
                return self._gated_pdf(q)
            if self._needs_admin(path) and not self._authed():
                if path.startswith("/api/"):
                    return self._send(401, {"error": "unauthorized", "auth": False})
                if path == "/admin/pending":
                    return self._login_page(error="Sign in to see your access.")
                return self._redirect_login(path or "/dashboard")
            if self._needs_admin(path):
                if path == "/admin/pending":
                    return self._pending_page()
                if self._function_denied(path):
                    return self._function_error(path)
            # SEO + marketing routes first (crawlable + short links)
            if path.rstrip("/").lower() == "/robots.txt":
                return self._send_cached(seo.render_robots(), "text/plain; charset=utf-8",
                                         max_age=60, edge=False)
            if path.rstrip("/").lower() == "/sitemap.xml":
                return self._send_cached(self._sitemap(), "application/xml; charset=utf-8",
                                         max_age=3600)
            if path.rstrip("/").lower() == "/sitemap.xsl":
                return self._send_cached(seo.sitemap_xsl(),
                                         "application/xslt+xml; charset=utf-8",
                                         max_age=86400)
            if path == "/blog":
                return self._send_cached(seo.render_blog(self._all_niches()),
                                         "text/html; charset=utf-8", edge=False)
            key_body = indexnow.serve_key(path)
            if key_body is not None:
                return self._send(200, key_body.encode("utf-8"), "text/plain; charset=utf-8")
            gsc_file = seo.serve_verification_file(path)
            if gsc_file is not None:
                return self._send(200, gsc_file.encode("utf-8"),
                                  "text/html; charset=utf-8")
            if path == "/api/indexnow":
                return self._indexnow(q)
            if path == "/":
                return self._send_cached(self._landing(), "text/html; charset=utf-8",
                                         max_age=300, edge=False)
            if path.startswith("/n/"):
                return self._niche_page(path, q)
            if path.startswith("/lp/"):
                return self._landing_page(path)
            post = re.match(r"^/social/([a-z0-9-]+)/([A-Za-z0-9]+)$", path)
            if post:
                return self._social_post_page(post.group(1), post.group(2))
            og = re.match(r"^/og/([a-z0-9-]+)$", path)
            if og:
                return self._og_image(og.group(1))
            ogpng = re.match(r"^/og/([a-z0-9-]+)\.png$", path)
            if ogpng:
                return self._og_image_png(ogpng.group(1))
            go = re.match(r"^/go/([A-Z0-9]{10})$", path)
            if go:
                return self._go(go.group(1))
            eo = re.match(r"^/e/o/([^/]+)$", path)
            if eo:
                return self._email_open(eo.group(1))
            et = re.match(r"^/e/([^/]+)$", path)
            if et:
                return self._email_click(et.group(1))
            if path == "/tool":
                with open(os.path.join(STATIC, "tool.html"), "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            if path == "/admin":
                return self._admin_page()
            if path == "/admin/ebooks/pdf":
                return self._ebook_pdf(q)
            if path == "/admin/ebooks":
                return self._admin_ebooks(q)
            if path == "/admin/emails":
                return self._admin_emails(q)
            if path == "/admin/analytics":
                return self._admin_analytics()
            if path == "/admin/backup":
                return self._admin_backup()
            if path == "/admin/marketing":
                return self._admin_marketing()
            if path == "/admin/funnel":
                return self._admin_funnel()
            if path == "/admin/variants":
                return self._admin_variants(q)
            if path == "/admin/segments":
                return self._admin_segments()
            if path == "/admin/pricedrop":
                return self._admin_pricedrop(q)
            if path == "/api/segments":
                return self._segments_api()
            if path == "/api/pricedrop":
                return self._pricedrop_api()
            if path == "/admin/apikeys":
                return self._admin_apikeys(q)
            if path == "/admin/opportunities":
                return self._admin_opportunities(q)
            if path == "/admin/priority":
                return self._admin_priority(q)
            if path == "/admin/social":
                return self._admin_social(q)
            if path == "/admin/sem":
                return self._admin_sem(q)
            if path == "/admin/seo":
                return self._admin_seo()
            if path == "/admin/seoengines":
                return self._admin_seoengines(q)
            if path == "/admin/manual":
                return self._admin_manual()
            if path == "/admin/manual.pdf":
                return self._admin_manual_pdf()
            if path == "/admin/users":
                return self._admin_users()
            if path == "/api/users":
                return self._users_api()
            if path == "/admin/cms":
                return self._admin_cms(q)
            if path == "/api/cms/pages":
                return self._cms_pages_api()
            cms_page = re.match(r"^/api/cms/pages/([0-9]+)(/sections)?$", path)
            if cms_page:
                return self._cms_page_api(cms_page, q)
            if path == "/admin/refresh":
                return self._admin_refresh(q)
            if path == "/api/sem":
                return self._sem_api(q)
            if path == "/api/seo-audit":
                return self._seo_audit_api()
            if path == "/api/seoengines":
                return self._seoengines_api(q)
            if path == "/api/seo/topics":
                return self._seo_topics_api()
            if path == "/api/marketing":
                return self._marketing_api()
            if path == "/api/funnel":
                return self._funnel_api()
            if path == "/api/suggest":
                return self._suggest_api()
            if path.startswith("/seo/snippet/"):
                return self._seo_snippet(path[len("/seo/snippet/"):])
            if path == "/api/social":
                return self._social_api(q)
            if path == "/keys":
                return self._keys_page()
            key_group = re.match(r"^/keys/([a-z0-9_-]+)/([a-z0-9_.-]+)$", path)
            if key_group:
                return self._keys_managed_page(key_group.group(1), key_group.group(2))
            key_provider = re.match(r"^/keys/([a-z0-9_-]+)$", path)
            if key_provider:
                return self._keys_provider_page(key_provider.group(1))
            if path[1:] in seo.STATIC_PAGES:
                page = getattr(seo, "render_%s" % path[1:])()
                return self._send(200, page, "text/html; charset=utf-8")
            # owner dashboard (static app UI) — kept off "/" so the root stays crawlable
            if path == "/dashboard" or path == "/index.html":
                with open(os.path.join(STATIC, "index.html"), "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            if path == "/app.js":
                with open(os.path.join(STATIC, "app.js"), "rb") as fh:
                    return self._send(200, fh.read(), "application/javascript; charset=utf-8")
            if path == "/style.css":
                with open(os.path.join(STATIC, "style.css"), "rb") as fh:
                    return self._send_cached(fh.read(), "text/css; charset=utf-8", 600)
            if path == "/courier.js":
                with open(os.path.join(STATIC, "courier.js"), "rb") as fh:
                    return self._send_cached(fh.read(),
                                             "application/javascript; charset=utf-8", 600)
            if path == "/table-flow.js":
                with open(os.path.join(STATIC, "table-flow.js"), "rb") as fh:
                    return self._send_cached(fh.read(),
                                             "application/javascript; charset=utf-8", 600)
            if path == "/ui.js":
                with open(os.path.join(STATIC, "ui.js"), "rb") as fh:
                    return self._send_cached(fh.read(),
                                      "application/javascript; charset=utf-8")
            if path == "/ai-fill.js":
                with open(os.path.join(STATIC, "ai-fill.js"), "rb") as fh:
                    return self._send_cached(fh.read(),
                                      "application/javascript; charset=utf-8")
            if path == "/api/settings":
                return self._send(200, self._settings())
            if path == "/api/ai/providers":
                return self._ai_providers()
            if path == "/api/autosuggest":
                qq = (q.get("q") or [""])[0].strip()
                return self._send(200, {"ideas": amazon.autosuggest(qq, limit=10)})
            if path == "/api/search":
                return self._search(q)
            if path == "/api/mine":
                return self._mine(q)
            if path == "/api/niches":
                return self._list_niches()
            if path == "/api/refresh/status":
                return self._refresh_status()
            if path == "/api/subscribers":
                return self._subscribers_json()
            if path == "/api/analytics":
                return self._analytics_api()
            if path == "/api/subjects":
                return self._subjects_api(q)
            if path == "/api/mail":
                return self._studio_api(q)
            if path == "/api/tools":
                return self._tools(q)
            if path == "/api/boosts":
                return self._boosts_api(q)
            if path == "/api/opportunities":
                return self._opportunities(q)
            if path == "/api/earnings/priority":
                return self._earnings_priority(q)
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        if self._prelim_guard() is None:
            return
        if not self._same_origin():
            return self._send(403, {"error": "cross-origin request blocked"})
        try:
            cl = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            cl = 0
        if cl > security.MAX_BODY:
            return self._send(413, {"error": "payload too large"})
        acquired = security.CONCURRENCY.acquire(timeout=0.05)
        try:
            if not acquired:
                return self._send(503, {"error": "busy, retry shortly"})
            return self._dispatch_post()
        finally:
            if acquired:
                security.CONCURRENCY.release()

    def _dispatch_post(self):
        parsed = urllib.parse.urlsplit(self.path)
        try:
            if parsed.path == "/admin/login":
                return self._login_post()
            if parsed.path == "/admin/register":
                return self._register_post()
            if parsed.path == "/admin/resend":
                return self._resend_post()
            if parsed.path == "/admin/forgot-password":
                return self._forgot_post()
            if parsed.path == "/admin/reset-password":
                return self._reset_post()
            if parsed.path == "/subscribe":
                return self._subscribe()
            if parsed.path == "/api/track":
                return self._track_click()
            if parsed.path == "/api/pageview":
                return self._page_view()
            if parsed.path == "/api/cron/send" and self._cron_ok():
                return self._sequence_send()
            if parsed.path == "/api/cron/inbox" and self._cron_ok():
                return self._send(200, _inbox_tick())
            if parsed.path.startswith("/api/") and not self._authed():
                return self._send(401, {"error": "unauthorized", "auth": False})
            if parsed.path.startswith("/api/") and self._function_denied(parsed.path):
                return self._send(403, {"error": "forbidden", "auth": True})
            if parsed.path == "/api/niches":
                return self._save_niche()
            if parsed.path == "/api/refresh":
                return self._refresh_post()
            if parsed.path == "/api/refresh-all":
                return self._refresh_all_post()
            if parsed.path == "/api/settings":
                return self._save_settings()
            if parsed.path == "/api/settings/test":
                return self._settings_test()
            if parsed.path == "/api/indexnow":
                return self._indexnow_post()
            if parsed.path == "/api/seoengines":
                return self._seoengines_post()
            if parsed.path == "/api/users":
                return self._users_api_post()
            if parsed.path == "/api/sequence/send":
                return self._sequence_send()
            if parsed.path == "/api/mail":
                return self._studio_api(
                    {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()})
            if parsed.path == "/api/social/publish":
                return self._social_publish()
            if parsed.path == "/api/social/schedule":
                return self._social_schedule()
            if parsed.path == "/api/social/flush":
                return self._social_flush()
            if parsed.path == "/api/social/amplify":
                return self._social_amplify()
            if parsed.path == "/api/social/topics":
                return self._social_topics()
            if parsed.path == "/api/tools/launch":
                return self._tools_launch()
            if parsed.path == "/api/boosts/run":
                return self._run_boosts()
            if parsed.path == "/api/boosts/social":
                return self._boosts_to_social()
            if parsed.path == "/api/cms/page":
                return self._cms_save_page()
            if parsed.path == "/api/cms/preset":
                return self._cms_apply_preset()
            if parsed.path == "/api/cms/generate":
                return self._cms_generate()
            cms_section = re.match(r"^/api/cms/pages/([0-9]+)/sections/([0-9]+)$", parsed.path)
            if cms_section:
                return self._cms_update_section(cms_section.group(1), cms_section.group(2))
            if parsed.path == "/api/subscribers":
                return self._subscribers_json()
            if parsed.path == "/api/ai/test":
                return self._ai_test()
            if parsed.path == "/api/keys/test":
                return self._keys_test()
            if parsed.path == "/api/keys/save":
                return self._keys_save()
            if parsed.path == "/api/earnings/config":
                return self._earnings_config()
            if parsed.path == "/api/earnings/log":
                return self._earnings_log()
            if parsed.path == "/api/variants/save":
                return self._variants_save()
            if parsed.path == "/api/pricedrop/run":
                return self._pricedrop_run()
            if parsed.path == "/api/pricedrop/send":
                return self._send(200, self._pricedrop_send())
            if parsed.path == "/api/segments/reengage":
                return self._send(200, self._reengage_cold())
            if parsed.path == "/api/subjects/save":
                return self._subjects_save()
            if parsed.path == "/api/captions/save":
                return self._captions_save()
            if parsed.path == "/api/captions/autoclean":
                return self._send(200, self._captions_autoclean())
            if parsed.path == "/api/captions":
                return self._captions_api(q)
            if parsed.path == "/api/subjects":
                return self._subjects_api(q)
            if parsed.path == "/api/variants/autoclean":
                return self._variants_autoclean()
            if parsed.path == "/api/subjects/autoclean":
                return self._send(200, self._subjects_autoclean())
            if parsed.path == "/api/ai/models":
                return self._ai_models()
            if parsed.path == "/api/ai/fill":
                return self._ai_fill()
            if parsed.path == "/api/ai/config":
                return self._ai_config()
            if parsed.path == "/api/opportunities/expand":
                return self._opportunities_expand()
            if parsed.path == "/api/topics/generate":
                return self._topics_generate()
            if parsed.path == "/api/sem/build-topic":
                return self._sem_build_topic_api()
            if parsed.path == "/api/suggest":
                return self._suggest_api()
            if parsed.path == "/api/suggest/build":
                return self._suggest_build_api()
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_PUT(self):
        self._send(405, {"error": "method not allowed"})

    def do_DELETE(self):
        self._send(405, {"error": "method not allowed"})

    def do_PATCH(self):
        self._send(405, {"error": "method not allowed"})

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        if n > security.MAX_BODY:
            raise ValueError("payload too large")
        raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype == "application/x-www-form-urlencoded":
            return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
        if raw:
            try:
                return json.loads(raw)
            except Exception:
                return {}
        return {}

    def _search(self, q):
        query = (q.get("q") or [""])[0].strip()
        market = (q.get("market") or [""])[0].strip()
        category = (q.get("category") or [""])[0].strip()
        top = int((q.get("top") or ["8"])[0])
        prev_market = amazon.MARKET
        try:
            if market:
                amazon.set_market(market)
            items, source = amazon.search(query, top=top, category=category)
        finally:
            if amazon.MARKET != prev_market:
                amazon.set_market(prev_market)
        return self._send(200, {"query": query, "market": amazon.MARKET,
                                "source": source, "items": items, "count": len(items)})

    def _mine(self, q):
        seed = (q.get("seed") or [""])[0].strip()
        market = (q.get("market") or [""])[0].strip()
        top = int((q.get("top") or ["8"])[0])
        prev_market = amazon.MARKET
        try:
            if market:
                amazon.set_market(market)
            niches, meta = niche.mine_niche(seed, top=top)
        finally:
            if amazon.MARKET != prev_market:
                amazon.set_market(prev_market)
        return self._send(200, {"niches": niches, "meta": meta})

    def _save_settings(self):
        body = self._body()
        # Read persisted PA-API values BEFORE taking _lock so we can preserve
        # unsubmitted fields (calling _get_setting inside the lock would deadlock).
        _paapi_prior = {f: _get_setting("paapi.%s" % f) for f in
                        ("access_key", "secret_key", "partner_tag")}
        _scraper_saved = dict(body.get("scraper") or {})
        with _lock:
            if body.get("market"):
                amazon.set_market(body["market"])
            if "affiliate_tag" in body:
                amazon.set_tag(body["affiliate_tag"])
            for pid, key in _scraper_saved.items():
                if pid in amazon._SCRAPER_PROVIDERS and key is not None:
                    amazon.set_scraper_key(pid, key)
            p = body.get("paapi")
            if isinstance(p, dict):
                # Apply only the fields actually submitted; fall back to the
                # persisted value so editing one field never blanks the others.
                paapi.configure(
                    p.get("access_key", _paapi_prior["access_key"]),
                    p.get("secret_key", _paapi_prior["secret_key"]),
                    p.get("partner_tag", _paapi_prior["partner_tag"]),
                )
        # Persist API-key style settings to DB (survive restart). These calls
        # take _lock themselves, so they must stay OUTSIDE the block above.
        for pid, key in _scraper_saved.items():
            if pid in amazon._SCRAPER_PROVIDERS and key is not None:
                _set_setting("scraper.key." + pid, key)
        if isinstance(p, dict):
            # Only persist the PA-API fields the operator actually submitted, so
            # the masked (unchanged) fields are never overwritten with blanks.
            for f in ("access_key", "secret_key", "partner_tag"):
                if f in p:
                    _set_setting("paapi.%s" % f, p.get(f) or "")
        s = body.get("social")
        if isinstance(s, dict):
            if "webhook" in s:
                _set_setting("social.webhook", s.get("webhook") or "")
            for platform in social.PLATFORMS:
                nk = social._key(platform)
                if nk in (s.get("keys") or {}):
                    _set_setting("social.key." + nk,
                                 (s.get("keys") or {}).get(nk) or "")
            # Twitter / X needs four credentials; the apikeys form posts them
            # under keys["twitter.client_id"] etc. Persist each distinctly so the
            # native OAuth 1.0a signing gets the right value per slot.
            tw = s.get("twitter") or {}
            for f in ("client_id", "client_secret", "access_token", "access_token_secret"):
                if f in tw:
                    val = tw[f]
                elif "twitter." + f in tw:
                    val = tw["twitter." + f]
                elif "twitter." + f in (s.get("keys") or {}):
                    val = (s.get("keys") or {})["twitter." + f]
                else:
                    continue
                if val:
                    _set_setting("social.key.twitter.%s" % f, val)
        # Market-demography targeting profile (region / interest / persona).
        demo = body.get("demography")
        if isinstance(demo, dict):
            for f in ("region", "interest", "interests_extra", "behavior",
                      "age", "audience", "income", "tone"):
                if f in demo:
                    _set_setting("demo.%s" % f, str(demo.get(f) or "").strip())
        return self._send(200, self._settings())

    def _settings_test(self):
        """Verifies the saved PA-API credentials against the live Product
        Advertising API by looking up a well-known ASIN. Returns ok even before
        the credentials really work only if we can't reach AWS (offline) — the
        readiness flag comes from paapi.ready() either way."""
        if not paapi.ready():
            return self._send(200, {
                "ok": False, "provider": "paapi",
                "error": "PA-API not configured — save the three PA-API values first.",
            })
        try:
            item = paapi.lookup("B08N5WRWNW")
            if item and item.get("source") == "paapi":
                return self._send(200, {
                    "ok": True, "provider": "paapi",
                    "detail": "AWS accepted keys (lookup of sample ASIN returned: %s)"
                              % (item.get("title") or "ok"),
                })
            return self._send(200, {
                "ok": False, "provider": "paapi",
                "error": "AWS rejected the credentials or returned no item.",
            })
        except Exception as e:
            return self._send(200, {
                "ok": False, "provider": "paapi",
                "error": "test failed: %s" % (str(e)[:200]),
            })

    def _save_niche(self):
        body = self._body()
        products = json.dumps(body.get("products") or [])
        with _lock:
            conn = _db()
            cur = conn.execute(
                "INSERT INTO niches (keyword, market, score, saturation, products) VALUES (?,?,?,?,?)",
                (body.get("keyword"), amazon.MARKET, body.get("score"), body.get("saturation"), products))
            conn.commit()
            nid = cur.lastrowid
            conn.close()
        self._push_indexnow(body.get("keyword"))
        # auto-build long-tail pages under the fresh niche so the plain dashboard
        # "save" immediately has indexable depth (same as the one-click suggest
        # build). Off unless explicitly disabled via setting.
        flag = _get_setting("niches.auto_topics")
        topics_created = []
        if flag == "" or str(flag).strip().lower() in ("1", "on", "true", "yes"):
            try:
                slug = seo._slugify(body.get("keyword"))
                topics_created = self._generate_topics(slug, 6)
            except Exception:
                topics_created = []
        _bust_admin_data_cache()
        return self._send(200, {"id": nid, "topics_created": len(topics_created)})

    def _fire_indexnow(self, paths):
        """Fire-and-forget IndexNow submit for a list of site-relative paths
        (e.g. /n/keto-snacks). Never blocks the caller and never raises."""
        try:
            base = seo.BASE_URL.rstrip("/")
            urls = [base + ("/" + p.lstrip("/")) for p in (paths or [])]
        except Exception:
            return
        if not urls:
            return
        threading.Thread(target=lambda: indexnow.submit_urls(urls), daemon=True).start()

    def _push_indexnow(self, keyword):
        """Fire-and-forget IndexNow submit so a brand-new /n/ page is crawled
        in minutes instead of waiting for a sitemap re-crawl. Never blocks the
        save response and never raises."""
        try:
            slug = seo._slugify(keyword)
        except Exception:
            slug = "niche"
        self._fire_indexnow(["/n/" + slug])

    def _list_niches(self):
        with _lock:
            conn = _db()
            rows = conn.execute("SELECT * FROM niches ORDER BY id DESC LIMIT 50").fetchall()
            conn.close()
        out = []
        for r in rows:
            d = dict(r)
            d["products"] = json.loads(d["products"] or "[]")
            try:
                d["slug"] = seo._slugify(d["keyword"])
            except Exception:
                d["slug"] = ""
            out.append(d)
        return self._send(200, {"niches": out})

    # -------------------------------------------------- Niche data refresh
    def _refresh_status(self):
        with _lock:
            conn = _db()
            total = conn.execute("SELECT COUNT(*) c FROM niches").fetchone()["c"]
            rows = conn.execute("SELECT updated_at FROM niches").fetchall()
            conn.close()
        now = time.time()
        refreshed = stale = 0
        for r in rows:
            u = r["updated_at"]
            if not u:
                stale += 1
                continue
            refreshed += 1
            try:
                parsed = time.mktime(time.strptime(u[:19], "%Y-%m-%d %H:%M:%S"))
            except (ValueError, TypeError):
                stale += 1
                continue
            if now - parsed >= _REFRESH_STALE_MIN * 60:
                stale += 1
        with _refresh_lock:
            inflight = sorted(_REFRESHING)
        return self._send(200, {
            "total": total, "refreshed": refreshed, "stale": stale,
            "inflight": inflight,
            "auto_interval_s": _REFRESH_INTERVAL_SEC,
            "stale_min": _REFRESH_STALE_MIN,
            "max_per_cycle": _REFRESH_MAX_PER_CYCLE})

    def _refresh_post(self):
        body = self._body() or {}
        kw = (body.get("keyword") or "").strip()
        if not kw:
            return self._send(400, {"error": "keyword required"})
        return self._send(200, _refresh_niche(kw))

    def _refresh_all_post(self):
        with _lock:
            conn = _db()
            rows = conn.execute("SELECT keyword FROM niches").fetchall()
            conn.close()
        kws = [r["keyword"] for r in rows]
        threading.Thread(target=_refresh_all_worker, args=(kws,), daemon=True).start()
        return self._send(200, {"status": "started", "queued": len(kws)})

    # ------------------------------------------------------------------ SEO
    def _all_niches(self):
        with _lock:
            conn = _db()
            rows = conn.execute(
                "SELECT keyword, products, created_at FROM niches").fetchall()
            conn.close()
        return [{"keyword": r["keyword"], "products": json.loads(r["products"] or "[]"),
                 "created_at": r["created_at"] or ""}
                for r in rows]

    def _top_clicked_niches(self, limit=10):
        """Aggregate Amazon-click traffic by niche (joining clicks.slug to the
        saved niche keyword). Returns [{slug, keyword, clicks}] most to least."""
        with _lock:
            conn = _db()
            rows = conn.execute(
                "SELECT c.slug, COUNT(*) AS clicks FROM clicks c "
                "WHERE c.slug != '' AND c.asin != '' "
                "GROUP BY c.slug ORDER BY clicks DESC LIMIT ?", (limit,)).fetchall()
            conn.close()
        ranked = []
        for r in rows:
            kw = self._keyword_for_slug(r["slug"])
            if kw:
                ranked.append({"slug": r["slug"], "keyword": kw, "clicks": r["clicks"]})
        return ranked

    def _keyword_for_slug(self, slug):
        from seo import _slugify
        with _lock:
            conn = _db()
            rows = conn.execute("SELECT keyword FROM niches").fetchall()
            conn.close()
        for r in rows:
            if _slugify(r["keyword"]) == slug:
                return r["keyword"]
        for n in seo.STATIC_PAGES:
            if _slugify(n) == slug:
                return n
        return ""

    def _topics_for(self, parent_slug):
        """Long-tail sub-pages for a parent niche, from the topics table."""
        rows = []
        with _lock:
            conn = _db()
            rows = conn.execute(
                "SELECT term, slug, created_at FROM topics "
                "WHERE parent_slug=? ORDER BY id ASC", (parent_slug,)).fetchall()
            conn.close()
        return [{"term": r["term"], "slug": r["slug"],
                 "created_at": r["created_at"] or ""} for r in rows]

    def _generate_topics(self, parent_slug, count=6):
        """Create new long-tail /n/<parent>/<term> pages for a niche from live
        Amazon autosuggest terms not yet built, and ping IndexNow. Returns the
        list of new topics created (dicts)."""
        parent_keyword = ""
        niche = None
        for n in self._all_niches():
            if seo._slugify(n["keyword"]) == parent_slug:
                parent_keyword = n["keyword"]
                niche = n
                break
        if not parent_keyword:
            return []
        existing = {t["slug"] for t in self._topics_for(parent_slug)}
        try:
            ideas = amazon.autosuggest(parent_keyword, limit=12)
        except Exception:
            ideas = []
        created = []
        for idea in ideas:
            if len(created) >= count:
                break
            term = (idea or "").strip().lower()
            term_slug = seo._slugify(term)
            if (not term or len(term) < 4 or term_slug == parent_slug
                    or term_slug in existing):
                continue
            with _lock:
                conn = _db()
                conn.execute(
                    "INSERT OR IGNORE INTO topics (parent_slug, term, slug) "
                    "VALUES (?,?,?)", (parent_slug, term, term_slug))
                conn.commit()
                conn.close()
            existing.add(term_slug)
            created.append({"term": term, "slug": term_slug,
                            "url": "/n/%s/%s" % (parent_slug, term_slug)})
        if created and niche is not None:
            self._fire_indexnow([c["url"] for c in created])
        return created

    def _opportunities_data(self):
        """Revenue-loop payload (winners + note), shared by page + API. Serves
        the render cache; on a miss it kicks off a background warm instead of
        blocking the request on live per-winner Amazon autosuggest."""
        if amazon.CACHE_TTL > 0:
            cached = _render_cache_get("opp:data")
            if cached is not None:
                return cached
            self._kick_admin_warm()
            return {"winners": [], "note": _OPP_WARMING_NOTE}
        return self._compute_opportunities()

    def _compute_opportunities(self):
        """Blocking compute for the opportunities payload (live autosuggest per
        winner). Warms ``opp:data`` when caching is enabled."""
        winners = self._top_clicked_niches(limit=12)
        if not winners:
            payload = {"winners": [], "note": "No click data yet — publish social posts and IndexNow your pages to seed the loop."}
            if amazon.CACHE_TTL > 0:
                _render_cache_put("opp:data", payload, _ADMIN_DATA_TTL)
            return payload
        existing_kw = {n["keyword"].lower() for n in self._all_niches()}
        out = []
        for w in winners:
            try:
                ideas = amazon.autosuggest(w["keyword"], limit=12)
            except Exception:
                ideas = []
            terms = []
            for idea in ideas:
                k = (idea or "").strip().lower()
                if k and len(k) >= 4 and k not in existing_kw:
                    terms.append(k)
            out.append({
                "slug": w["slug"], "keyword": w["keyword"], "clicks": w["clicks"],
                "unbuilt": len(terms), "suggestions": terms[:6],
                "topic_count": len(self._topics_for(w["slug"])),
                "topic_urls": ["/n/%s/%s" % (w["slug"], t["slug"])
                               for t in self._topics_for(w["slug"])[:10]],
            })
        payload = {"winners": out,
                   "note": "Unbuilt terms are real Amazon autosuggestions around a proven winner — expand to auto-create those pages."}
        if amazon.CACHE_TTL > 0:
            _render_cache_put("opp:data", payload, _ADMIN_DATA_TTL)
        return payload

    def _opportunities(self, q):
        """Revenue loop, read side: the niches already pulling real Amazon clicks
        (they're proven), each with how many related, not-yet-built keyword terms
        are waiting to be turned into pages."""
        return self._send(200, self._opportunities_data())

    def _opportunities_expand(self):
        """Auto-create new niche pages for a proven winner's related terms.
        Mines products live, saves each new niche, and tells IndexNow so the
        page is crawled fast. Returns the newly created niches."""
        body = self._body()
        slug = str(body.get("slug") or "").strip().lower()
        count = int(body.get("count") or 3)
        count = max(1, min(count, 6))
        if not slug:
            return self._send(400, {"error": "slug required"})
        kw = self._keyword_for_slug(slug)
        if not kw:
            return self._send(404, {"error": "no saved niche for that slug"})
        existing_kw = {n["keyword"].lower() for n in self._all_niches()}
        try:
            niches, meta = niche.mine_niche(kw, top=6)
        except Exception:
            return self._send(500, {"error": "mining failed"})
        created = []
        for n in niches[1:]:  # skip the seed itself
            k = (n["keyword"] or "").strip().lower()
            if not k or len(k) < 4 or k in existing_kw:
                continue
            if len(created) >= count:
                break
            if not n.get("products"):
                continue
            with _lock:
                conn = _db()
                conn.execute(
                    "INSERT INTO niches (keyword, market, score, saturation, products) "
                    "VALUES (?,?,?,?,?)",
                    (n["keyword"], amazon.MARKET, n.get("score"), n.get("saturation"),
                     json.dumps(n["products"])))
                conn.commit()
                conn.close()
            existing_kw.add(k)
            self._push_indexnow(n["keyword"])
            created.append({"keyword": n["keyword"], "products": len(n["products"]),
                            "slug": seo._slugify(n["keyword"]),
                            "score": n.get("score"), "saturation": n.get("saturation")})
        _bust_admin_data_cache()
        return self._send(200, {"ok": True, "seed": kw, "created": created,
                                "count": len(created)})

    def _topics_generate(self):
        """Generate long-tail topic pages (/n/<parent>/<term>) for a niche from
        live Amazon-autosuggest terms, saved to the topics table + IndexNow."""
        body = self._body()
        slug = str(body.get("slug") or "").strip().lower()
        try:
            count = int(body.get("count") or 6)
        except (TypeError, ValueError):
            count = 6
        count = max(1, min(count, 6))
        if not slug:
            return self._send(400, {"error": "slug required"})
        created = self._generate_topics(slug, count)
        return self._send(200, {"ok": True, "parent": slug, "created": created,
                                "count": len(created),
                                "urls": [c["url"] for c in created]})

    def _sitemap(self):
        # Home/blog are rebuilt/newsy every day; a rolling lastmod signals GSC
        # and Bing that the sitemap changed so the "could not fetch" state from
        # previous failed crawls is re-attempted on the next crawl pass.
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entries = [("/", today), ("/blog", today)]
        for page in seo.STATIC_PAGES:
            entries.append(("/" + page, "2026-08-28"))
        with _lock:
            conn = _db()
            nrows = conn.execute(
                "SELECT keyword, created_at, products FROM niches").fetchall()
            conn.close()
        live = set()
        for r in nrows:
            # Only indexable niches belong in the sitemap — a niche without
            # products renders noindex and must never be listed.
            prods = (r["products"] or "").strip()
            if not prods or prods in ("[]", "{}"):
                continue
            try:
                kw = seo._slugify(r["keyword"])
            except Exception:
                kw = "niche"
            lm = (r["created_at"] or "")[:10] or "2026-08-28"
            entries.append((f"/n/{kw}", lm))
            entries.append((f"/lp/{kw}", lm))
            live.add(kw)
        with _lock:
            conn = _db()
            tro = conn.execute("SELECT parent_slug, slug, created_at FROM topics").fetchall()
            conn.close()
        for t in tro:
            # Long-tail pages resolve products via their parent niche; if the
            # parent is gone (or currently product-less) the page goes noindex,
            # so it has no business in the sitemap.
            if t["parent_slug"] not in live:
                continue
            lm = (t["created_at"] or "")[:10] or "2026-08-28"
            entries.append((f"/n/{t['parent_slug']}/{t['slug']}", lm))
        return seo.render_sitemap(entries)

    def _landing(self):
        return seo.render_landing(self._all_niches())

    def _variant_for(self, slug):
        """Deterministic per-visitor A/B headline: stable by client hash so a
        visitor always sees the same headline variant, but the cohort splits.
        Returns (headline, variant_index) or (None, 0) when no variants set."""
        with _lock:
            conn = _db()
            rows = conn.execute(
                "SELECT variant, headline FROM niche_variants "
                "WHERE lower(slug)=? AND enabled=1 "
                "AND headline IS NOT NULL AND headline != '' "
                "ORDER BY variant ASC", (slug.lower(),)).fetchall()
            conn.close()
        if not rows:
            return None, 0
        idx = int(security.ip_token(self._client_ip()) or "0", 16) % len(rows)
        rows.sort(key=lambda r: r["variant"])
        return rows[idx]["headline"], rows[idx]["variant"]

    def _subject_for(self, kw, idx, sub_id, default_subject):
        """Email subject-line A/B: pick a deterministic subject variant per
        subscriber for (keyword, email_index) so cohorts split but a subscriber
        stays stable across the sequence. Falls back to the default subject.
        Returns {"subject": str, "variant": int}."""
        with _lock:
            conn = _db()
            rows = conn.execute(
                "SELECT variant, subject FROM email_subjects "
                "WHERE lower(keyword)=? AND email_index=? AND enabled=1 "
                "AND subject IS NOT NULL AND subject != '' ORDER BY variant ASC",
                (kw.lower(), idx)).fetchall()
            conn.close()
        if not rows:
            return {"subject": default_subject, "variant": 0}
        idx0 = int(sub_id or 0) % len(rows)
        return {"subject": rows[idx0]["subject"], "variant": rows[idx0]["variant"]}

    def _subjects_for(self, kw):
        """All configured email-subject variants for a niche keyword, for the
        A/B editor."""
        with _lock:
            conn = _db()
            rows = conn.execute(
                "SELECT id, email_index, variant, subject, enabled FROM email_subjects "
                "WHERE lower(keyword)=? ORDER BY email_index, variant", (kw.lower(),)).fetchall()
            conn.close()
        return [dict(r) for r in rows]

    def _subject_stats(self, kw):
        """Per-subject-variant open/click performance for keyword's sequence, so
        the operator sees which email subject wins. Uses sent_emails.subject_variant
        joined to email_events (by subscriber_id + email_index)."""
        with _lock:
            conn = _db()
            rows = conn.execute(
                "SELECT s.email_index, s.subject, s.subject_variant v, "
                "COUNT(DISTINCT s.subscriber_id) sent, "
                "COUNT(DISTINCT e.subscriber_id) opened, "
                "(SELECT COUNT(*) FROM clicks c WHERE c.source='email' AND c.slug=? "
                " AND c.referrer LIKE '%' || s.subscriber_id || '%') clicks "
                "FROM sent_emails s LEFT JOIN email_events e "
                "ON e.subscriber_id=s.subscriber_id AND e.email_index=s.email_index "
                "AND e.type='open' "
                "WHERE s.subject_variant>0 AND s.subject IS NOT NULL "
                "GROUP BY s.email_index, s.subject, s.subject_variant "
                "ORDER BY s.email_index, s.subject_variant", (kw.lower(),)).fetchall()
            conn.close()
        out = []
        for r in rows:
            out.append({"email_index": r["email_index"], "subject": r["subject"],
                        "variant": r["v"], "sent": r["sent"], "opened": r["opened"],
                        "clicks": r["clicks"],
                        "open_rate": round(r["opened"] / max(r["sent"], 1) * 100, 1)})
        return out

    def _subjects_save(self):
        """Save email-subject A/B variants for a keyword. Body: {keyword, items:
        [{email_index, variant, subject, enabled}]}."""
        body = self._body()
        kw = str(body.get("keyword") or "").strip()
        items = body.get("items") or []
        if not kw:
            return self._send(400, {"error": "keyword required"})
        with _lock:
            conn = _db()
            for it in items:
                idx = int(it.get("email_index") or 1)
                variant = int(it.get("variant") or 1)
                subject = str(it.get("subject") or "").strip()
                enabled = 1 if it.get("enabled", True) else 0
                conn.execute(
                    "INSERT INTO email_subjects (keyword, email_index, variant, subject, enabled) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(keyword, email_index, variant) "
                    "DO UPDATE SET subject=excluded.subject, enabled=excluded.enabled",
                    (kw, idx, variant, subject, enabled))
            conn.commit()
            conn.close()
        return self._send(200, {"ok": True, "keyword": kw, "saved": len(items)})

    def _subjects_api(self, q):
        kw = (q.get("keyword") or [""])[0].strip()
        if not kw:
            return self._send(200, {"keyword": "", "subjects": [], "stats": []})
        return self._send(200, {"keyword": kw,
                                "subjects": self._subjects_for(kw),
                                "stats": self._subject_stats(kw)})

    def _subjects_autoclean(self, min_sends=None):
        """Email-subject A/B auto-cleanup: for each keyword + email_index, once a
        position has enough lifetime sends, auto-disable any subject variant whose
        opens are far below the position's opener (the winner is kept, losers stop
        burning deliverability). Mirrors the headline `_ab_autoclean` behaviour."""
        if min_sends is None:
            try:
                min_sends = int(_get_setting("ab.subjects_min_sends") or 40)
            except (TypeError, ValueError):
                min_sends = 40
        changed = []
        with _lock:
            conn = _db()
            kws = conn.execute("SELECT DISTINCT lower(keyword) AS k FROM email_subjects "
                               "WHERE enabled=1").fetchall()
            conn.close()
        for r in kws:
            kw = r["k"]
            stats = self._subject_stats(kw)
            indexes = sorted({s["email_index"] for s in stats})
            for idx in indexes:
                group = [s for s in stats if s["email_index"] == idx]
                if sum(s["sent"] for s in group) < min_sends:
                    continue
                leader = max(group, key=lambda s: s["opened"])
                opened_best = leader["opened"]
                losers = [s["variant"] for s in group
                          if s["variant"] != leader["variant"]
                          and s["opened"] < opened_best * 0.25]
                if not losers:
                    continue
                with _lock:
                    conn = _db()
                    for v in losers:
                        conn.execute(
                            "UPDATE email_subjects SET enabled=0 "
                            "WHERE lower(keyword)=? AND email_index=? AND variant=?",
                            (kw, idx, v))
                    conn.commit()
                    conn.close()
                changed.append({"keyword": kw, "email_index": idx,
                                "disabled": losers, "kept": leader["variant"]})
        return {"ok": True, "changed": changed,
                "note": ("Disabled subject variants opening below 25%% of the "
                         "position's opener once it reached %d lifetime sends."
                         % min_sends)}


    def _captions_api(self, q):
        slug = (q.get("slug") or [""])[0].strip().lower()
        if not slug:
            return self._send(200, {"slug": "", "captions": []})
        return self._send(200, {"slug": slug, "captions": self._captions_for(slug)})

    def _niche_page(self, path, q):
        slug = path[len("/n/"):].rstrip("/") or "niche"
        # Long-tail topic page: /n/<parent>/<term>
        if "/" in slug:
            parent_slug, term_slug = slug.split("/", 1)
            all_niches = self._all_niches()
            niche = None
            for n in all_niches:
                if seo._slugify(n["keyword"]) == parent_slug:
                    niche = n
                    break
            term = ""
            for t in self._topics_for(parent_slug):
                if t["slug"] == term_slug:
                    term = t["term"]
                    break
            if niche is not None and term:
                # content-keyed cache so crawler storms don't re-render
                key = ("/topic/", parent_slug, term_slug, seo._variant_key(niche))
                cached = _render_cache_get(key) if amazon.CACHE_TTL > 0 else None
                if cached is not None:
                    return self._send_cached(cached, "text/html; charset=utf-8")
                res = seo.render_topic(term, niche["keyword"], niche, parent_slug)
                if amazon.CACHE_TTL > 0:
                    _render_cache_put(key, res, RENDER_CACHE_DEFAULT_TTL)
                    return self._send_cached(res, "text/html; charset=utf-8")
                return self._send(200, res, "text/html; charset=utf-8")
            return self._send(404, seo.render_niche(slug, {"products": [], "source": ""}),
                              "text/html; charset=utf-8")
        aware = amazon.CACHE_TTL > 0  # tests set CACHE_TTL=0 -> bypass render cache
        try:
            ttl = float(_get_setting("render_cache_ttl") or RENDER_CACHE_DEFAULT_TTL)
        except (TypeError, ValueError):
            ttl = RENDER_CACHE_DEFAULT_TTL
        cache_key = None
        all_niches = self._all_niches()
        for n in all_niches:
            try:
                if slug == seo._slugify(n["keyword"]):
                    headline, vno = self._variant_for(slug)
                    # A/B headline is chosen per-visitor by IP hash, so edge
                    # caching would pin everyone to one variant. Cache at the CDN
                    # only when no variants are enabled for this page.
                    cacheable = (headline is None and vno == 0)
                    cache_key = ("/n/", slug, amazon.MARKET, vno, headline,
                                 seo._variant_key(n))
                    cached = _render_cache_get(cache_key) if aware else None
                    if cached is not None:
                        return self._send_cached(cached, "text/html; charset=utf-8", ttl,
                                                 edge=cacheable)
                    res = seo.render_niche(n["keyword"], n, saved_niches=all_niches,
                                           ab_headline=headline, ab_variant=vno)
                    if aware:
                        _render_cache_put(cache_key, res, ttl)
                        return self._send_cached(res, "text/html; charset=utf-8", ttl,
                                                 edge=cacheable)
                    return self._send(200, res, "text/html; charset=utf-8")
            except Exception:
                continue
        return self._send(404, seo.render_niche(slug, {"products": [], "source": ""}),
                          "text/html; charset=utf-8")

    def _go(self, asin):
        target, market = market_engine.expand_go(asin)
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return None

    def _email_click(self, token):
        """Click-tracked email outbound: validates the signed token, records a
        click attributed to the email (niche, ASIN, subscriber, email index),
        then 302s to the tagged Amazon link."""
        import urllib.parse as _up
        payload = mailer.decode_track_token(token, scope="e")
        if not payload:
            self.send_response(404)
            self.end_headers()
            return None
        kw, asin, sid, idx = (list(payload) + ["", "", "", ""])[:4]
        try:
            asin = asin.upper()
            if len(asin) == 10:
                self._record_click(slug=kw or "", source="email",
                                   referrer=str(sid or "") + "|" + str(idx or ""),
                                   asin=asin, content="email")
        except Exception:
            pass
        target = amazon.affiliate_url(asin) if asin else ""
        if not target:
            self.send_response(404)
            self.end_headers()
            return None
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return None

    def _email_open(self, token):
        """Open-tracking endpoint (1x1 transparent GIF). Records that this
        subscriber/email was rendered, then returns the pixel."""
        payload = mailer.decode_track_token(token, scope="o")
        _GIF = (b"\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00"
                b"\xff\xff\xff\x00\x00\x00\x00\x00\x00\x21\xf9\x04\x00"
                b"\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00"
                b"\x00\x02\x02\x44\x01\x00\x3b")
        if payload:
            try:
                kw, asin, sid, idx = (list(payload) + ["", "", "", ""])[:4]
                with _lock:
                    conn = _db()
                    conn.execute(
                        "INSERT INTO email_events (type, subscriber_id, email_index, "
                        "keyword, asin) VALUES ('open',?,?,?,?)",
                        (sid, idx, kw, asin or ""))
                    conn.commit()
                    conn.close()
            except Exception:
                pass
        self.send_response(200)
        self.send_header("Content-Type", "image/gif")
        self.send_header("Content-Length", str(len(_GIF)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(_GIF)
        return None

    def _landing_page(self, path):
        slug = path[len("/lp/"):].rstrip("/") or "niche"
        for n in self._all_niches():
            try:
                if slug == seo._slugify(n["keyword"]):
                    # Try CMS render first; fall back to the legacy template.
                    html = self._cms_landing_html(n)
                    if html is None:
                        html = market_engine.build_landing_page(
                            n["keyword"], n["products"], site_url=seo.BASE_URL)
                    return self._send_cached(html.encode("utf-8"), "text/html; charset=utf-8",
                                             max_age=180, edge=True)
            except Exception:
                continue
        return self._send(404, b"<html><body><p>Landing page not found.</p></body></html>",
                          "text/html; charset=utf-8")

    def _cms_landing_html(self, niche):
        """Render a CMS-driven landing page for a niche. Returns None if CMS
        rendering is disabled for this niche (fall back to legacy)."""
        with _lock:
            conn = _db()
            try:
                page = cms_mod.get_or_create_page(conn, niche["keyword"])
                settings = page.get("settings") or {}
                if settings.get("use_cms", True) is False:
                    return None
                sub_count = conn.execute(
                    "SELECT COUNT(*) c FROM subscribers WHERE unsubscribed=0 AND confirmed=1 "
                    "AND lower(keyword)=?", (niche["keyword"].strip().lower(),)
                ).fetchone()["c"]
                ctx = cms_mod.build_page_context(conn, niche["keyword"], {
                    "products": niche.get("products") or [],
                    "subscriber_count": sub_count,
                })
            finally:
                conn.close()
        return cms_render.render_landing_page_page(
            ctx, niche["keyword"], site_url=seo.BASE_URL)

    # ------------------------------------------------------------------ marketing tools
    def _all_urls(self, extra=None):
        """Absolute site URLs that should be indexed (sitemap set + extras)."""
        urls = seo.indexable_urls(self._all_niches())
        for u in (extra or []):
            if str(u).startswith("http"):
                urls.append(str(u))
            else:
                urls.append(seo.BASE_URL.rstrip("/") + str(u))
        return urls

    def _indexnow(self, q):
        return self._send(200, {
            "key": indexnow.key(),
            "key_file": indexnow.key_file_path(),
            "endpoint": indexnow.ENDPOINT,
            "url_count": len(self._all_urls()),
            "status": "ready",
        })

    def _indexnow_post(self):
        body = self._body()
        urls = self._all_urls(body.get("urls") or [])
        ok, message = indexnow.submit_urls(urls)
        return self._send(200, {"ok": ok, "message": message, "submitted": len(urls)})

    # ------------------------------------------------ search-engine consoles
    @staticmethod
    def _engine_from_referrer(referrer):
        """Map a referrer origin to the search engine that sent it."""
        h = (referrer or "").lower()
        if not h:
            return "direct"
        if "google." in h:
            return "google"
        if "bing." in h:
            return "bing"
        if "duckduckgo." in h:
            return "duckduckgo"
        if "yandex." in h:
            return "yandex"
        if "yahoo." in h:
            return "yahoo"
        return "other"

    def _seoengines_traffic(self, days=28):
        """On-site traffic attributed by referrer: pageviews (events) + Amazon
        clicks (clicks) per engine over the window. Self-collected, so it works
        even before console tokens exist."""
        buckets = ("direct", "google", "bing", "yandex", "duckduckgo",
                   "yahoo", "other")
        out = {b: {"views": 0, "clicks": 0} for b in buckets}
        with _lock:
            conn = _db()
            try:
                vw = conn.execute(
                    "SELECT COALESCE(referrer,'') r, COUNT(*) c FROM events "
                    "WHERE name='view' AND created_at >= datetime('now', 'localtime', ?) "
                    "GROUP BY r", ("-%d days" % days,)).fetchall()
                ck = conn.execute(
                    "SELECT COALESCE(referrer,'') r, COUNT(*) c FROM clicks "
                    "WHERE created_at >= datetime('now', 'localtime', ?) "
                    "GROUP BY r", ("-%d days" % days,)).fetchall()
            finally:
                conn.close()
        for r, c in vw:
            e = self._engine_from_referrer(r)
            out[e]["views"] = out.get(e, {}).get("views", 0) + int(c)
        for r, c in ck:
            e = self._engine_from_referrer(r)
            out[e]["clicks"] = out.get(e, {}).get("clicks", 0) + int(c)
        total_v = sum(v["views"] for v in out.values())
        total_c = sum(v["clicks"] for v in out.values())
        return {"days": days, "engines": out, "totals": {"views": total_v,
                                                         "clicks": total_c}}

    def _seoengines_api(self, q):
        engines = webmasters.engines_status()
        days = int((q.get("days") or ["28"])[0])
        traffic = self._seoengines_traffic(days)
        snaps = {}
        for e, _n, *_ in [(x["engine"], x["name"]) for x in engines]:
            s = webmasters.last_sync(e)
            if s:
                snaps[e] = s
        return self._send(200, {"engines": engines, "traffic": traffic,
                                "snapshots": snaps, "host": webmasters.host_of(),
                                "sitemap": webmasters.site_url() + "/sitemap.xml"})

    def _seoengines_post(self):
        body = self._body()
        action = (body.get("action") or "").strip()
        engine = (body.get("engine") or "").strip().lower()
        if action == "sync" and engine in ("gsc", "bing", "yandex"):
            ok, data = webmasters.sync_engine(engine,
                                              int(body.get("days") or 28))
            return self._send(200, {"ok": ok, **data})
        if action == "submit" and engine in ("gsc", "bing"):
            if engine == "gsc":
                ok, msg = webmasters.gsc_submit_sitemap()
            else:
                key = webmasters.BING_API_KEY or \
                    webmasters.store_get("seoeng.bing.apikey", "")
                if not key:
                    return self._send(200, {"ok": False, "error": "bing key not set"})
                s, d = webmasters.bing_submit_sitemap(key, webmasters.site_url())
                ok, msg = s in (200, 201), (str(d)[:150])
            return self._send(200, {"ok": ok, "error" if not ok else "message": msg} if not ok
                              else {"ok": ok, "message": msg})
        if action == "bingkey":
            webmasters.store_set("seoeng.bing.apikey",
                                 (body.get("key") or "").strip())
            return self._send(200, {"ok": True})
        if action == "verify" and engine in ("google", "bing", "yandex"):
            token = (body.get("token") or "").strip()
            setter = {"google": seo.set_google_site_verification,
                      "bing": seo.set_bing_site_verification,
                      "yandex": seo.set_yandex_site_verification}[engine]
            setter(token)
            return self._send(200, {"ok": True, "engine": engine,
                                    "active": bool(token)})
        if action == "connect" and engine in ("gsc", "yandex"):
            state = security.make_token("oauth:seoengines:" + engine, 600)
            url = webmasters.gsc_auth_url(state) if engine == "gsc" \
                else webmasters.yandex_auth_url(state)
            if not url:
                return self._send(200, {"ok": False,
                                        "error": "client id/secret not set (env)"})
            self._set_cookie(state, max_age=600, cookie=_OAUTH_COOKIE, path="/admin")
            return self._send(200, {"ok": True, "url": url})
        return self._send(400, {"error": "unknown action"})

    def _seoengine_oauth_cb(self, engine, q):
        """OAuth consent-callback for a search-engine console (gsc | yandex).
        Seasoned like the login OAuth: signed state cookie must match."""
        code = (q.get("code") or [""])[0]
        state = (q.get("state") or [""])[0]
        expect = self._cookie_token(_OAUTH_COOKIE)
        scope = security.verify_token(expect) if expect else None
        expect_scope = "oauth:seoengines:" + engine
        def go(msg, failed=False):
            if msg:
                return self._redirect("/admin/seoengines?%s=%s" % (
                    "err" if failed else "msg", urllib.parse.quote(msg, safe="")))
            return self._redirect("/admin/seoengines")
        if not code or not expect or not state or scope != expect_scope \
                or not hmac.compare_digest(expect, state):
            self._set_cookie("x", max_age=0, cookie=_OAUTH_COOKIE, path="/admin")
            return go("consent link was stale or tampered with — try again", True)
        try:
            ok, msg = (webmasters.gsc_exchange(code) if engine == "gsc"
                       else webmasters.yandex_exchange(code))
        except Exception as exc:
            ok, msg = False, str(exc)[:200]
        self._set_cookie("x", max_age=0, cookie=_OAUTH_COOKIE, path="/admin")
        if ok:
            return go("connected ✓")
        return go(msg[:200] if msg else "connect failed", True)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return None

    def _admin_seoengines(self, q):
        """Search-engine consoles hub: connect each engine's console, submit the
        sitemap, verify ownership tokens, and read clicks/impressions either from
        each console's API or from our own referrer-attributed on-site data."""
        nav = self._admin_nav('seoengines')
        setup = {
            "gsc": "Create a Google Cloud OAuth2 web client + enable the Search Console API. Set PSTORE_GSC_CLIENT_ID / PSTORE_GSC_CLIENT_SECRET on the host and add redirect URI %s/admin/oauth/seoengines/cb/gsc. Then Connect to approve it."
                   % seo.BASE_URL,
            "bing": "Add this exact site in Bing Webmaster, then paste/保存 your Bing API key (also settable via PSTORE_BING_API_KEY). No OAuth needed.",
            "yandex": "Create a Yandex OAuth app (PSTORE_YANDEX_CLIENT_ID / PSTORE_YANDEX_CLIENT_SECRET) with redirect URI %s/admin/oauth/seoengines/cb/yandex, add this host in Yandex Webmaster, then Connect."
                   % seo.BASE_URL,
        }
        names = {"gsc": "Google Search Console",
                 "bing": "Bing Webmaster",
                 "yandex": "Yandex Webmaster"}
        engines = [("gsc",), ("bing",), ("yandex",)]
        cards = []
        for (eng,) in engines:
            if eng == "bing":
                connect_btn = ('<button class="btn ghost" disabled title="Bing uses a key, '
                               'not OAuth">key-based</button>')
                extra = ("<div class='row' style='flex-wrap:wrap;gap:1px'>"
                         "<input id='vk-bing' placeholder='Bing API key' "
                         "style='width:260px;max-width:100%'>&nbsp;"
                         "<button class='btn' onclick='saveKey(\"bing\")'>Save key</button></div>")
            else:
                connect_btn = ('<button class="warm" onclick="act(\'connect\',\'%s\')">'
                               'Connect console</button>' % eng)
                extra = ""
            cards.append("""
        <div class="card" style="margin:0">
         <h2>%s</h2>
         <div class="row">
          <div class="feature"><h3 id="st-%s">…</h3><p class="hint">state</p></div>
          <div class="feature"><h3 id="tok-%s">…</h3><p class="hint">token</p></div>
          <div class="feature"><h3 id="last-%s">never</h3><p class="hint">last sync</p></div>
         </div>
         <div class="row" style="margin-top:8px;flex-wrap:wrap;gap:8px">
          %s
          <button class="btn" onclick="act('sync','%s')">⟳ Fetch stats</button>
          <button class="btn ghost" onclick="act('submit','%s')">Submit sitemap</button>
         </div>
         <div class="row" style="margin-top:8px;flex-wrap:wrap;gap:8px;align-items:center">
          <input id="vt-%s" placeholder="verification meta token" style="width:250px;max-width:100%%">
          <button class="btn ghost" onclick="verifyTok('%s')">Save verification meta</button>
         </div>
         %s
         <details class="bump" style="margin-top:10px"><summary>Setup guide</summary>
           <p class="hint" style="margin-top:6px">%s</p></details>
         <pre class="preview" id="out-%s" style="display:none"></pre>
        </div>""" % (names[eng], eng, eng, eng, connect_btn, eng, eng, eng,
                      eng, extra, setup[eng], eng))
        engines_rows = "".join(cards)
        page = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Search engines — pstore</title><link rel="stylesheet" href="/style.css">
<meta name="robots" content="noindex,nofollow">
<style>
.stengx{display:grid;grid-template-columns:minmax(0,1fr);gap:18px}
.stengx>.card,.eng-grid,.eng-grid>.card,.stengx .feature{min-width:0}
.eng-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;min-width:0}
pre.preview{white-space:pre-wrap;word-break:break-word;background:#fbf7ef;border:1px solid var(--line,#eee);border-radius:12px;padding:12px;font-size:12.5px;margin-top:8px}
#traf td{vertical-align:middle}
.badg{display:inline-block;padding:2px 10px;border-radius:999px;font-size:11.5px;font-weight:700}
.tblflow{overflow-x:auto}
#traf{min-width:620px}
.stengx .eng-grid h2{font-size:17px}
.stengx .hero h1{font-size:clamp(24px,6vw,40px)}
@media (max-width: 620px){
 .stengx .row{gap:8px}
 .eng-grid{grid-template-columns:1fr}
 main{padding:12px 12px 48px}
 .eng-grid input{min-width:0}
 .stengx .card{padding:16px 14px}
 .stengx .feature{flex:1 1 46%}
}
</style>
</head><body>
<header id="top"><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>Search <span>engines.</span></h1>
<p class="tagline">Verify ownership, submit your sitemap, and watch clicks/impressions per engine — fed by each console's API where a token is connected, and by your own on-site referrer data everywhere else.</p></div>
{nav}
</header>
<main class="stengx">
 <section class="card"><h2>🌍 Consoles</h2>
  <div class="eng-grid" id="engines">{engines_rows}</div>
 </section>
 <section class="card"><h2>📊 Traffic by engine <span class="hint">(last {days} days, referral-attributed)</span></h2>
  <div class="tblflow"><table class="plain" id="traf">
   <thead><tr><th>Engine</th><th class="ct">Pageviews</th><th class="ct">Affiliate clicks</th><th class="ct">CTR→click</th><th>Console rates (last 28d)</th></tr></thead>
   <tbody><tr><td colspan="5" class="hint">Loading…</td></tr></tbody></table></div>
  <p id="traffic-msg" class="msg"></p>
 </section>
 <section class="card"><h2>🩺 On-site health</h2>
  <div class="row"><div class="feature"><h3 id="h-host">—</h3><p class="hint">host</p></div>
   <div class="feature"><h3 id="h-sitemap">—</h3><p class="hint">sitemap</p></div>
   <div class="feature"><h3 id="h-robots">—</h3><p class="hint">robots</p></div></div>
 </section>
</main>
<footer><p>Verification metas (google-site-verification, msvalidate.01, yandex-verification) are emitted on every public page. Console APIs add real impressions/positions; until then the Traffic panel shows referral-attributed visits and clicks collected by your own beacon.</p></footer>
{totop}
<script>
const $=id=>document.getElementById(id);
const ENG={gsc:["Google Search Console","Google","so you need a Google Cloud OAuth client (PSTORE_GSC_CLIENT_ID / PSTORE_GSC_CLIENT_SECRET). Enable the Search Console API, add redirect URI {base}/admin/oauth/seoengines/cb/gsc, then Connect."],
   bing:["Bing Webmaster","Microsoft-Bing Webmaster, key-based","create a Bing Webmaster account, add this exact site, then grab the API key (PSTORE_BING_API_KEY or paste it here). No OAuth — the key is the token."],
   yandex:["Yandex Webmaster","Yandex Webmaster OAuth (PSTORE_YANDEX_CLIENT_ID / _SECRET)","create a Yandex OAuth app, redirect URI {base}/admin/oauth/seoengines/cb/yandex, then Connect."]};
function esc(s){return (s==null?"":String(s)).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function badge(state){return state==="ready"?'<span class="badg" style="background:#e6ffe8;color:#1e8e3e">ready</span>'
  :state==="consent-given"?'<span class="badg" style="background:#ffeedb;color:#a05a00">consent given</span>'
  :state==="needs-consent"?'<span class="badg" style="background:#fff3cd;color:#8a6d1a">needs consent</span>'
  :state==="needs-key"?'<span class="badg" style="background:#fff3cd;color:#8a6d1a">needs key</span>'
  :'<span class="badg" style="background:#ffe6e6;color:#c0392b">'+(state||"?")+'</span>';}
function act(a,e){fetch("/api/seoengines",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:a,engine:e,days:28})}).then(r=>r.json()).then(d=>{
  if(a==="connect"){ if(d.ok&&d.url){window.location=d.url;} else {out(e,"Connect unavailable: "+ (d.error||""));} return;}
  const o=$("out-"+e); if(!o)return;
  o.style.display=o.style.display==="none"?"block":"none";
  if(a==="submit")o.textContent=e==="gsc"?"Simple sitemap PUT → "+ (d.ok?("ok: "+d.message):"err: "+d.error):(d.ok?"submitted ✓":"err: "+d.error);
  else if(a==="sync")o.textContent=formatStats(d);
  load();});}
function saveKey(e){const k=$("vk-"+e).value;fetch("/api/seoengines",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"bingkey",key:k})}).then(()=>load());}
function verifyTok(e){const t=$("vt-"+e).value;fetch("/api/seoengines",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"verify",engine:e,token:t})}).then(()=>{out(e,t?"verification meta saved ✓":"verification meta cleared");load();});}
function formatStats(d){const t=(d.totals||{});const r=(d.rows||[]).slice(0,12);let s=`${esc(d.engine||"")} · ${d.days}d · clicks ${t.clicks??0} · impressions ${t.impressions??0} · position ${t.position??"—"} · ctr ${t.ctr??0}%`;
 if(r.length) s+="\\n\\n" + r.map(x=>`${esc(x.page)}  → ${x.clicks}c / ${x.impressions}i @pos ${x.position}`).join("\\n");
 return s;}
function load(){fetch("/api/seoengines").then(r=>r.json()).then(d=>{
  for(const e of d.engines){$("st-"+e.engine).innerHTML=badge(e.state);$("tok-"+e.engine).textContent=e.state==="ready"?"connected":(e.client?"token pending":"client ids missing");$("last-"+e.engine).textContent=e.last_sync||"never";}
  const tr=d.traffic; const tgs={"google":"Google","bing":"Bing","yandex":"Yandex","duckduckgo":"DuckDuckGo","yahoo":"Yahoo","direct":"Direct","other":"Other"};
  const rows=[];
  for(const k in tr.engines){const v=tr.engines[k];const ctr=v.views?((v.clicks/v.views)*100).toFixed(1):0;
    rows.push(`<tr><td>${tgs[k]}</td><td class="ct">${v.views}</td><td class="ct">${v.clicks}</td><td class="ct">${ctr}%</td><td class="ct hint" style="font-size:12px"></td></tr>`);}
  rows.push(`<tr style="font-weight:700"><td>All</td><td class="ct">${tr.totals.views}</td><td class="ct">${tr.totals.clicks}</td><td class="ct">${tr.totals.views?((tr.totals.clicks/tr.totals.views)*100).toFixed(1):0}%</td><td></td></tr>`);
  $("traf").querySelector("tbody").innerHTML=rows.join("");
  $("h-host").textContent=d.host;$("h-sitemap").textContent=d.sitemap;$("h-robots").textContent="robots.txt ↗";
  msgs();
}).catch(e=>{$("traffic-msg").textContent="Failed to load: "+e;});}
function out(e,t){const o=$("out-"+e);if(o){o.style.display="block";o.textContent=t;}}
function msgs(){const u=new URLSearchParams(location.search);const m=u.get("msg")||u.get("err");if(m){const t=$("traffic-msg");if(t){t.textContent=(u.get("err")?"✗ ":"")+m;setTimeout(()=>{t.textContent="";history.replaceState({},"","/admin/seoengines");},6000);}}}
document.addEventListener("DOMContentLoaded",load);
</script>
</body></html>""".replace("{nav}", nav).replace("{engines_rows}", engines_rows).replace(
            "{totop}", _TOTOP).replace("{days}", "28").replace("{base}", seo.BASE_URL)
        return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

    def _keys_page(self):
        """One admin page with every key/endpoint a tool needs."""
        rows = [
            ("IndexNow key", indexnow.key()),
            ("IndexNow key file", indexnow.key_file_path()),
            ("IndexNow endpoint", indexnow.ENDPOINT),
            ("Submit all URLs (POST)", seo.BASE_URL.rstrip("/") + "/api/indexnow"),
            ("Sitemap", seo.BASE_URL.rstrip("/") + "/sitemap.xml"),
            ("Google verification meta", seo.google_site_verification() or "(none set)"),
            ("Bing verification meta (msvalidate.01)",
             seo.bing_site_verification() or "(none set)"),
            ("Yandex verification meta (yandex-verification)",
             seo.yandex_site_verification() or "(none set)"),
            ("Search consoles hub", seo.BASE_URL.rstrip("/") + "/admin/seoengines"),
            ("Google console OAuth client", (webmasters.GSC_CLIENT_ID or "(PSTORE_GSC_CLIENT_ID unset)")),
            ("Bing Webmaster API key", (webmasters.BING_API_KEY or webmasters.store_get("seoeng.bing.apikey")) or "(PSTORE_BING_API_KEY unset)"),
            ("Yandex console OAuth client", (webmasters.YANDEX_CLIENT_ID or "(PSTORE_YANDEX_CLIENT_ID unset)")),
            ("Affiliate tag", amazon.AFFILIATE_TAG or "(none set)"),
            ("Marketplace", amazon.MARKET),
            ("Scraper providers", "%d / %d keyed" % (
                sum(1 for p in amazon.scraper_status()["providers"].values()
                    if p["has_key"]),
                len(amazon.scraper_status()["providers"]))),
            ("PA-API", "ready" if paapi.ready() else "not configured (optional)"),
        ]
        cards = "".join(
            '<div class="sub"><h3>%s</h3><p class="key" '
            'onclick="navigator.clipboard&&navigator.clipboard.writeText(this.textContent)" '
            'title="Click to copy">%s</p></div>' % (k, v)
            for k, v in rows)
        # Every manage-able key group, each entry linking to its own page + the
        # platform where the real key lives.
        group_html = []
        for g in self._managed_groups():
            entries = "".join(
                '<div class="sub"><h3>%s</h3>'
                '<p class="key"><a href="%s">%s</a> · %s'
                '%s</p></div>'
                % (seo._clean(e["name"]), e["page"], e["page"],
                   seo._clean(e["status"]),
                   (" · <a href='%s' target='_blank' rel='noopener'>⚙ platform ↗</a>" % seo._clean(e["home"]))
                   if e.get("home") else "")
                for e in g["entries"])
            group_html.append(
                '<h2>%s</h2>\n<p class="hint">%s</p>%s' % (g["label"], g["hint"], entries))
        groups = "\n".join(group_html)
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Keys — pstore</title><link rel="stylesheet" href="/style.css">
<meta name="robots" content="noindex,nofollow">
<style>.key{{font-family:ui-monospace,Menlo,monospace;font-size:13px;background:var(--bg);border:1px solid var(--border);
border-radius:10px;padding:10px 12px;margin:0;word-break:break-all;cursor:copy}}
.sub{{padding:8px 0}}.sub h3{{margin:0 0 6px;font-size:13px;color:var(--muted);font-weight:700}}</style>
</head><body>
<header id="top"><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>Keys & <span>endpoints.</span></h1>
<p class="tagline">Every key, URL and endpoint the tools need — click a value to copy it, or open a key page to test & save.</p></div>
{self._admin_nav('keys')}
</header>
<main><section class="card"><h2>🔑 Keys & endpoints</h2>{cards}
<h2>🗝 Manage every key</h2>
<p class="hint">Each key gets its own page — see status, open the platform console directly, test the key and save it here:</p>
{groups}
</section></main>
<footer><p>Keys saved here apply immediately and are kept for the running process; set the matching env var to make them permanent across restarts.</p></footer>
{_TOTOP}
</body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _keys_provider_page(self, pid):
        """Short per-provider page: see key status, open the provider's own
        dashboard to fetch an API key, and save (or clear) it on this instance."""
        meta = amazon._SCRAPER_PROVIDERS.get(pid)
        if not meta:
            return self._send(
                404, b"<html><body><p>Unknown provider.</p></body></html>",
                "text/html; charset=utf-8")
        key = amazon._scraper_key(pid)
        has_env = bool(os.environ.get(meta["env_var"]))
        masked = (("••••" + key[-4:]) if key else "(no key set)")
        status = ("key set" if key else "no key set") + \
                 (" · from environment %s" % meta["env_var"] if has_env else "")
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{meta['name']} key — pstore</title><link rel="stylesheet" href="/style.css">
<meta name="robots" content="noindex,nofollow">
<style>.key{{font-family:ui-monospace,Menlo,monospace;font-size:13px;background:var(--bg);border:1px solid var(--border);
border-radius:10px;padding:10px 12px;margin:0;word-break:break-all}}
.masked{{font-size:18px;font-weight:700;letter-spacing:1px}}</style>
</head><body>
<header><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>{meta['name']} <span>API key.</span></h1>
<p class="tagline">Paste your {meta['name']} API key below, or grab a new one at their dashboard.</p></div>
{self._admin_nav('keys')}
</header>
<main>
<section class="card"><h2>🎯 {meta['name']} ({meta['kind']})</h2>
<div class="sub"><h3>Current status</h3><p class="key masked">{masked}</p><p class="hint">{status}</p></div>
<div class="sub"><h3>Get an API key</h3>
<p class="hint">Create / copy your key at the {meta['name']} dashboard, then paste it here:</p>
<p><a class="btn" href="{meta['key_url']}" target="_blank" rel="noopener">Open {meta['name']} dashboard ↗</a></p>
</div>
<div class="sub"><h3>Test the key</h3>
<p class="hint">Fires a real, free request to {meta['name']} — the provider confirms the key is accepted, with zero search quota used.</p>
<button id="test" class="warm">▶ Test key</button>
<p id="tmsg" class="msg"></p>
</div>
<div class="row">
  <label>API key
    <input id="key" placeholder="paste key…" autocomplete="off" spellcheck="false">
  </label>
  <button id="save" class="warm">Save key</button>
  <button id="clear">Clear key</button>
</div>
<p id="msg" class="msg"></p>
<a href="/keys" class="hint">← all keys</a>
</section></main>
<footer><p>Keys are stored in memory for this instance; set {meta['env_var']} as an environment variable to make them permanent.</p></footer>
<script>
function $(id){{return document.getElementById(id);}}
async function post(path, payload){{
  const r = await fetch(path, {{method:"POST", headers:{{"Content-Type":"application/json"}},
    body: JSON.stringify(payload)}});
  return r;
}}
$("test").onclick = async () => {{
  const t = $("tmsg"), key = $("key").value.trim();
  t.textContent = "Testing…"; t.className = "msg";
  let d, r;
  try {{ r = await post("/api/keys/test", {{pid: "{pid}", key: key}}); d = await r.json(); }}
  catch(e) {{ t.textContent = "✗ Could not reach the server."; return; }}
  if (d.ok) {{ t.textContent = "✓ Valid — the provider accepted this key." +
      (d.latency_ms ? " (" + d.latency_ms + "ms)" : "") + (d.detail ? " · " + d.detail : "");
      t.className = "msg ok"; }}
  else {{ t.textContent = "✗ " + (d.error || "Test failed."); t.className = "msg err"; }}
}};
$("save").onclick = async () => {{
  const v = $("key").value.trim();
  if (await (await post("/api/settings", {{scraper: {{"{pid}": v}}}})).ok) {{ $("msg").textContent = "Saved ✓"; setTimeout(()=>location.reload(), 700); }}
  else $("msg").textContent = "Save failed.";
}};
$("clear").onclick = async () => {{
  if (await (await post("/api/settings", {{scraper: {{"{pid}": ""}}}})).ok) {{ $("msg").textContent = "Cleared."; setTimeout(()=>location.reload(), 700); }}
  else $("msg").textContent = "Clear failed.";
}};
$("key").addEventListener("keydown", e => {{ if (e.key === "Enter") $("save").onclick(); }});
</script>
</body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _keys_test(self):
        body = self._body()
        group = str(body.get("group") or "scraper").strip().lower()
        key_id = str(body.get("keyid") or body.get("pid") or "").strip().lower()
        key = str(body.get("key") or "").strip()
        if group == "ai":
            return self._send(200, ai.test(key_id or "openai", key,
                                           body.get("model"), body.get("base")))
        if group == "oauth":
            # Nothing from a live provider here — report whether the pair is set
            # and valid; the real test is the provider's own dashboard.
            conf = oauth.providers_configured()
            names = {p: n for p, n, _d in conf}
            if key_id == "google":
                ok = bool(oauth.GOOGLE_CLIENT_ID and oauth.GOOGLE_CLIENT_SECRET)
            elif key_id == "facebook":
                ok = bool(oauth.FACEBOOK_APP_ID and oauth.FACEBOOK_APP_SECRET)
            else:
                ok = bool(names.get(key_id))
            return self._send(200, {
                "ok": ok, "provider": key_id,
                "error": None if ok else ("%s credentials not configured" % key_id),
                "detail": "configured via environment" if ok else
                          "set OAUTH_* env vars (clear+restart to apply) or use the platform link above"})
        if group == "site":
            if key_id == "indexnow":
                val = key or indexnow.key()
                ok = bool(val) and len(val) == 32 and all(c in "0123456789abcdef" for c in val)
                return self._send(200, {"ok": ok, "provider": "indexnow",
                                        "error": None if ok else "IndexNow key must be 32 lowercase hex",
                                        "key_file": indexnow.key_file_path() if bool(val) else None})
            if key_id == "gsc":
                val = key or seo.google_site_verification()
                ok = bool(val.strip())
                return self._send(200, {"ok": ok, "provider": "gsc",
                                        "error": None if ok else
                                        "no token set — grab it from Google Search Console's HTML-tag method",
                                        "save": True})
            return self._send(200, {"ok": False, "error": "unknown site token"})
        return self._send(200, amazon.test_scraper_key(key_id, key))

    def _keys_save(self):
        """Persist a managed key for this process (runtime override; set the
        matching env var to survive a restart). Returns the updated status."""
        body = self._body()
        group = str(body.get("group") or "scraper").strip().lower()
        key_id = str(body.get("keyid") or body.get("pid") or "").strip().lower()
        key = str(body.get("key") or "").strip()
        if group == "ai":
            out = ai.configure_runtime(key_id or "openai", key, body.get("model"),
                                       body.get("base"))
            if out.get("ok"):
                # persist so the key survives restart/redeploy (env still wins
                # at read time, but a UI-pasted key must not vanish on deploy)
                p = (key_id or "openai").strip().lower()
                _set_setting("ai.key." + p, key)
                _set_setting("ai.model." + p, str(body.get("model") or "").strip())
                if body.get("base"):
                    _set_setting("ai.base." + p, str(body.get("base")).strip())
            return self._send(200, out)
        if group == "site":
            if key_id == "indexnow":
                indexnow.set_key(key)
                return self._send(200, {"ok": True, "provider": "indexnow",
                                        "live": indexnow.key(),
                                        "note": "applies now; set INDEXNOW_KEY env to persist"})
            if key_id == "gsc":
                seo.set_google_site_verification(key)
                return self._send(200, {"ok": True, "provider": "gsc",
                                        "set": bool(key),
                                        "note": "applies now; set PSTORE_GOOGLE_SITE_VERIFICATION env to persist"})
            return self._send(200, {"ok": False, "error": "unknown site token"})
        if group == "market":
            if key_id == "affiliate":
                amazon.set_tag(key)
                return self._send(200, {"ok": True, "provider": "affiliate",
                                        "tag": amazon.AFFILIATE_TAG,
                                        "note": "applies now; set PSTORE_TAG env to persist"})
            if key_id == "market":
                amazon.set_market(key)
                return self._send(200, {"ok": True, "provider": "market",
                                        "market": amazon.MARKET})
        if group == "scraper":
            if key_id in amazon._SCRAPER_PROVIDERS:
                amazon.set_scraper_key(key_id, key)
                _set_setting("scraper.key." + key_id, key)
                return self._send(200, {"ok": True, "provider": key_id})
        return self._send(200, {"ok": False, "error": "unknown key"})

    # ------------------------------------------------------------------ managed keys hub
    def _managed_groups(self):
        """Metadata for every key group shown on /keys — each entry carries the
        direct link to the platform where the owner manages the real key."""
        scraper_prov = amazon.scraper_status()["providers"]
        ai_prov = ai.providers()
        return [
            {
                "id": "scraper",
                "label": "🛢 Scraper providers",
                "hint": "Data providers used to search & scrape Amazon listings. Each is its own page.",
                "entries": [{
                    "id": pid, "name": pv["name"], "env": amazon._SCRAPER_PROVIDERS[pid]["env_var"],
                    "url": amazon._SCRAPER_PROVIDERS[pid].get("key_url", ""),
                    "home": amazon._SCRAPER_PROVIDERS[pid].get("key_url", ""),
                    "status": "key set" if pv.get("has_key") else "no key set",
                    "page": "/keys/%s" % pid,
                } for pid, pv in scraper_prov.items()],
            },
            {
                "id": "ai",
                "label": "🤖 AI (LLM) providers",
                "hint": "Writes the ebook/headline copy. Paste a key, test it, and save.",
                "entries": [{
                    "id": pid, "name": m["label"], "env": m["key_env"],
                    "url": m["home"], "home": m["home"],
                    "hint": m.get("key_hint", ""),
                    "status": ("active" if ai.active_provider() == pid else "set") if bool(ai.key_for(pid)) else "no key set",
                    "page": "/keys/ai/%s" % pid,
                } for pid, m in ai.PROVIDERS.items()],
            },
            {
                "id": "oauth",
                "label": "🔐 Sign-in (OAuth) keys",
                "hint": "Optional Google / Facebook login for the admin. Configured via environment.",
                "entries": [
                    {"id": "google", "name": "Google", "env": "OAUTH_GOOGLE_CLIENT_ID + OAUTH_GOOGLE_CLIENT_SECRET",
                     "url": "https://console.developers.google.com/apis/credentials",
                     "home": "https://console.developers.google.com/apis/credentials",
                     "status": "set" if oauth.GOOGLE_CLIENT_ID and oauth.GOOGLE_CLIENT_SECRET else "no key set",
                     "page": "/keys/oauth/google"},
                    {"id": "facebook", "name": "Facebook", "env": "OAUTH_FACEBOOK_APP_ID + OAUTH_FACEBOOK_APP_SECRET",
                     "url": "https://developers.facebook.com/apps",
                     "home": "https://developers.facebook.com/apps",
                     "status": "set" if oauth.FACEBOOK_APP_ID and oauth.FACEBOOK_APP_SECRET else "no key set",
                     "page": "/keys/oauth/facebook"},
                ],
            },
            {
                "id": "site",
                "label": "🌐 Site ownership & indexing",
                "hint": "Proves you own the site to search engines. Visible here, saved in memory, env-persisted.",
                "entries": [
                    {"id": "indexnow", "name": "IndexNow key", "env": "INDEXNOW_KEY",
                     "url": "https://www.indexnow.org/", "home": "https://www.indexnow.org/",
                     "status": ("%s" % indexnow.key()[-6:]) if indexnow.key() else "no key set",
                     "page": "/keys/site/indexnow"},
                    {"id": "gsc", "name": "Google Search Console token", "env": "PSTORE_GOOGLE_SITE_VERIFICATION",
                     "url": "https://search.google.com/search-console",
                     "home": "https://search.google.com/search-console",
                     "status": "set" if seo.google_site_verification() else "not set",
                     "page": "/keys/site/gsc"},
                ],
            },
            {
                "id": "market",
                "label": "🛒 Marketplace & affiliates",
                "hint": "Amazon marketplace + your Associates tag on every product link.",
                "entries": [
                    {"id": "affiliate", "name": "Amazon Associates tag", "env": "PSTORE_TAG",
                     "url": "https://affiliate-program.amazon.com/account",
                     "home": "https://affiliate-program.amazon.com/account",
                     "status": amazon.AFFILIATE_TAG or "no tag set",
                     "page": "/keys/market/affiliate"},
                    {"id": "market", "name": "Amazon marketplace", "env": "PSTORE_MARKET",
                     "url": "https://affiliate-program.amazon.com/account",
                     "home": "https://affiliate-program.amazon.com/account",
                     "status": amazon.MARKET or "default",
                     "page": "/keys/market/market"},
                ],
            },
        ]

    def _managed_entry(self, group_id, entry_id):
        for g in self._managed_groups():
            if g["id"] == group_id:
                for e in g["entries"]:
                    if e["id"] == entry_id:
                        return g, e
        return None, None

    def _keys_managed_page(self, group_id, entry_id):
        """One managed-key page (mirrors the scraper provider page): current
        status, a direct link to the platform, and an input to test + save."""
        group, entry = self._managed_entry(group_id.lower(), entry_id.lower())
        if not entry:
            return self._send(404, b"<html><body><p>Unknown key.</p></body></html>",
                              "text/html; charset=utf-8")
        group_label = group["label"]
        name = entry["name"]
        env = entry.get("env", "")
        home = entry.get("home", "")
        key_url = entry.get("url", "")
        masked = ""
        cur = ""
        editable = group_id.lower() in ("ai", "site", "market") or group_id.lower() == "scraper"
        if group_id == "scraper":
            cur = amazon._scraper_key(entry_id)
            masked = (("••••" + cur[-4:]) if cur else "(no key set)")
        elif group_id == "ai":
            cur = ai.key_for(entry_id)
            masked = (("••••" + cur[-4:]) if cur else "(no key set)")
        elif group_id == "site":
            if entry_id == "indexnow":
                cur = indexnow.key()
                masked = cur or "(no key set)"
            else:
                cur = seo.google_site_verification()
                masked = cur or "(no key set)"
        elif group_id == "market":
            if entry_id == "affiliate":
                cur = amazon.AFFILIATE_TAG or "(none set)"
            else:
                cur = amazon.MARKET or "(default)"
            masked = cur
        elif group_id == "oauth":
            masked = ("configured" if entry["status"] == "set" else "(env not set)")
        test_group = "scraper" if group_id == "scraper" else group_id
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{seo._clean(name)} — pstore</title><link rel="stylesheet" href="/style.css">
<meta name="robots" content="noindex,nofollow">
<style>.key{{font-family:ui-monospace,Menlo,monospace;font-size:13px;background:var(--bg);border:1px solid var(--border);
border-radius:10px;padding:10px 12px;margin:0;word-break:break-all}}
.masked{{font-size:16px;font-weight:700;letter-spacing:1px}}</style>
</head><body>
<header><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>{seo._clean(name)} <span>key.</span></h1>
<p class="tagline">Paste or refresh this key, test it, then save it here.</p></div>
{self._admin_nav('keys')}
</header>
<main>
<section class="card"><h2>🎯 {seo._clean(group_label)} — {seo._clean(name)}</h2>
<div class="sub"><h3>Current status</h3><p class="key masked">{seo._clean(masked)}</p>
<p class="hint">{seo._clean(entry['status'])}{(' · env ' + seo._clean(env)) if env else ''}</p></div>
<div class="sub"><h3>Manage your key at the platform</h3>
<p class="hint">Get, reset or revoke the real key at the provider's own console — then paste it back here.</p>
<p><a class="btn" href="{seo._clean(home)}" target="_blank" rel="noopener">Open {seo._clean(name)} console ↗</a></p>
{('' if key_url == home else '<p class="hint">Direct reference: <a href="' + seo._clean(key_url) + '" target="_blank" rel="noopener">' + seo._clean(key_url) + '</a></p>')}
</div>
<div class="sub"><h3>Test the key</h3>
<p class="hint">{'Fires a real, free request to confirm the provider accepts this key' if group_id in ('ai','scraper') else 'Checks the value is present and well-formed.'}</p>
<button id="test" class="warm">▶ Test</button>
<p id="tmsg" class="msg"></p></div>
{('' if group_id == 'oauth' else f'''<div class="row">
  <label>Value <input id="key" placeholder="paste or enter…" autocomplete="off" spellcheck="false"></label>
  <button id="save" class="warm">Save</button>
  <button id="clear">Clear</button></div>''')}
<p id="msg" class="msg"></p>
<a href="/keys" class="hint">← all keys</a>
</section></main>
<footer><p>Saved here applies to this process immediately; set <code>{seo._clean(env)}</code> as an environment variable to make it permanent across restarts.</p></footer>
<script>
function $(id){{return document.getElementById(id);}}
async function post(path, payload){{
  const r = await fetch(path, {{method:"POST", headers:{{"Content-Type":"application/json"}},
    body: JSON.stringify(payload)}});
  return r;
}}
const testBtn = $("test");
if (testBtn) testBtn.onclick = async () => {{
  const t = $("tmsg"), key = $("key") ? $("key").value.trim() : "";
  t.textContent = "Testing…"; t.className = "msg";
  let d, r;
  try {{ r = await post("/api/keys/test", {{group:"{test_group}", keyid:"{entry_id}", key:key}}); d = await r.json(); }}
  catch(e) {{ t.textContent = "✗ Could not reach the server."; return; }}
  if (d.ok) {{ t.textContent = "✓ OK" + (d.latency_ms ? " (" + d.latency_ms + "ms)" : "") + (d.detail ? " · " + d.detail : ""); t.className = "msg ok"; }}
  else {{ t.textContent = "✗ " + (d.error || "Test failed."); t.className = "msg err"; }}
}};
const saveBtn = $("save");
if (saveBtn) saveBtn.onclick = async () => {{
  const v = $("key").value.trim();
  const r = await post("/api/keys/save", {{group:"{group_id}", keyid:"{entry_id}", key:v}});
  const d = await r.json();
  if (d.ok) {{ $("msg").textContent = "Saved ✓ " + (d.note || "applies now."); $("msg").className = "msg ok"; setTimeout(()=>location.reload(), 900); }}
  else {{ $("msg").textContent = "Save failed: " + (d.error || "unknown"); $("msg").className = "msg err"; }}
}};
const clearBtn = $("clear");
if (clearBtn) clearBtn.onclick = async () => {{
  const r = await post("/api/keys/save", {{group:"{group_id}", keyid:"{entry_id}", key:""}});
  const d = await r.json();
  if (d.ok) {{ $("msg").textContent = "Cleared."; setTimeout(()=>location.reload(), 700); }}
  else {{ $("msg").textContent = "Clear failed."; }}
}};
const inp = $("key");
if (inp) inp.addEventListener("keydown", e => {{ if (e.key === "Enter" && saveBtn) saveBtn.onclick(); }});
</script>
</body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _best_for_tools(self, q):
        items = []
        keyword = (q.get("keyword") or [""])[0].strip()
        if q.get("items"):
            try:
                items = json.loads(q["items"][0])
            except Exception:
                items = []
        if not items and keyword:
            for n in self._all_niches():
                if n["keyword"].lower() == keyword.lower():
                    items = n["products"]
                    break
        return items, keyword

    def _workbench_payload(self, keyword, items):
        slug = seo._slugify(keyword) if keyword else ""
        niche_url = seo.BASE_URL.rstrip("/") + "/n/" + slug if slug else None
        landing_url = "/lp/" + slug if slug else None
        subs, clicks, top_product = self._niche_stats(keyword, slug)
        ebook_url = "/admin/ebooks/pdf?keyword=%s" % urllib.parse.quote(keyword) if keyword else None
        boosts = self._boosts_for(keyword) if keyword else []
        return {
            "keyword": keyword,
            "count": len(items or []),
            "affiliate_tag": amazon.AFFILIATE_TAG,
            "text_links": market_engine.build_text_links(items),
            "markdown": market_engine.build_markdown(items, heading=keyword),
            "email": market_engine.build_email_draft(items),
            "post": market_engine.build_post_template(items),
            "top_pick": market_engine.pick_for_buyers(items),
            "funnel": market_engine.build_funnel(keyword, items,
                                                 site_url=seo.BASE_URL,
                                                 affiliate_tag=amazon.AFFILIATE_TAG),
            "boosts": boosts,
            "slug": slug,
            "niche_url": niche_url,
            "landing_url": landing_url,
            "ebook_url": ebook_url,
            "ebook_ready": keyword in _EBOOKS,
            "social_kit": social.post_kits(keyword, items, base_url=seo.BASE_URL),
            "stats": {"subscribers_active": subs["active"],
                      "subscribers_ready": subs["ready"],
                      "clicks": clicks,
                      "top_product": top_product},
            "indexnow": {"key": indexnow.key(), "status": "ready"},
        }

    def _niche_stats(self, keyword, slug):
        """Per-niche feedback loop readings: opt-in subscribers (active / ready for
        the next email) plus click intel gathered by the courier beacon."""
        kw = (keyword or "").strip().lower()
        with _lock:
            conn = _db()
            active = conn.execute(
                "SELECT COUNT(*) c FROM subscribers WHERE unsubscribed=0 AND confirmed=1 "
                "AND lower(keyword)=?", (kw,)).fetchone()["c"]
            ready = conn.execute(
                "SELECT COUNT(*) c FROM subscribers WHERE unsubscribed=0 AND confirmed=1 "
                "AND sent_index < ? AND lower(keyword)=?",
                (mailer.SEQUENCE_LENGTH, kw)).fetchone()["c"]
            clicks = conn.execute(
                "SELECT COUNT(*) c FROM clicks WHERE slug=?", (slug,)).fetchone()["c"]
            top = conn.execute(
                "SELECT asin, COUNT(*) c FROM clicks WHERE slug=? AND asin!='' "
                "GROUP BY asin ORDER BY c DESC LIMIT 1", (slug,)).fetchone()
            conn.close()
        return {"active": active, "ready": ready}, clicks, (dict(top) if top else None)

    def _tools(self, q):
        items, keyword = self._best_for_tools(q)
        return self._send(200, self._workbench_payload(keyword, items))

    def _tools_launch(self):
        """One-click marketing for a saved niche: compile every asset, warm the
        lead-magnet ebook, ping IndexNow for the review + landing URLs, and return
        the full workbench payload plus a launch summary. Never blocks on the
        publish ping and never raises."""
        body = self._body()
        keyword = str(body.get("keyword") or "").strip()
        items = []
        for n in self._all_niches():
            if n["keyword"].lower() == keyword.lower():
                items = n["products"]
                break
        if not keyword or not items:
            return self._send(404, {"error": "no saved niche matches that keyword"})
        cached = self._ebook_for(keyword) is not None
        try:
            slug = seo._slugify(keyword)
        except Exception:
            slug = "niche"
        self._fire_indexnow(["/n/" + slug, "/lp/" + slug])
        payload = self._workbench_payload(keyword, items)
        payload["launched"] = {
            "keyword": keyword, "landing": True,
            "ebook_cached": cached, "indexnow_queued": True,
            "emails_ready": payload["stats"]["subscribers_ready"],
            "clicks": payload["stats"]["clicks"],
        }
        return self._send(200, payload)


# ------------------------------------------------------------------ boosts suite
    def _boost_longtail(self, keyword):
        """Long-tail phrases (SEM autosuggest) to weave into boost copy on a run.
        Gated off in offline tests (CACHE_TTL=0) so runs stay fast and hermit."""
        if amazon.CACHE_TTL <= 0:
            return ()
        try:
            niche = self._saved_niche(keyword)
            if not niche:
                return ()
            info = sem.brief(keyword, niche, seo.BASE_URL,
                             audit_row=seo.audit_niche(niche),
                             indexnow_key=indexnow.key())
            out = [g.get("phrase") for g in (info.get("longtail") or [])]
            out = [p for p in out if p]
            for q in (info.get("paa") or []):
                if len(out) >= 3:
                    break
                if q.get("question"):
                    out.append(q["question"])
            return tuple(out[:3])
        except Exception:
            return ()

    def _boosts_for(self, keyword, keywords=()):
        """Boost campaigns for a niche — live builds merged with run/publish
        history and per-campaign click totals (attribution via utm_content)."""
        niche = self._saved_niche(keyword)
        if not niche:
            return []
        slug = seo._slugify(keyword)
        campaigns = market_engine.build_boost_campaigns(
            keyword, niche["products"] or [], base_url=seo.BASE_URL,
            slug=slug, keywords=keywords)
        with _lock:
            conn = _db()
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM boosts WHERE lower(slug)=? ORDER BY id", (slug,)).fetchall()]
            stat = {}
            for c in campaigns:
                stat[c["code"]] = conn.execute(
                    "SELECT COUNT(*) c FROM clicks WHERE lower(slug)=? AND content=?",
                    (slug, str(c["code"]))).fetchone()["c"]
            conn.close()
        code_from = {r["utm_content"]: r for r in rows if r.get("utm_content")}
        run_map = {r["name"]: r for r in rows}
        seen = set()

        def social_path(code, published_ok=True):
            if not code:
                return None
            if published_ok:
                r = code_from.get(code)
                if not r or r.get("status") != "published":
                    return None
            return "/social/" + slug + "/" + str(code)

        def entry(c, r):
            code = c.get("code") or r.get("utm_content") or ""
            return {"name": c["name"], "id": c.get("id") or r.get("id"),
                    "target": c.get("target", "landing"),
                    "script": c.get("script", ""),
                    "link": c.get("link", ""), "code": code,
                    "qr": c.get("qr", ""), "runs": r.get("runs") or 0,
                    "status": r.get("status") or "ready",
                    "clicks": stat.get(code) or 0,
                    "social": social_path(code),
                    "updated_at": r.get("updated_at")}

        out = []
        for c in campaigns:
            r = run_map.get(c["name"]) or {}
            seen.add(c["name"])
            out.append(entry(c, r))
        for r in rows:
            if r["name"] in seen:
                continue
            c = {"name": r["name"], "link": r["link"], "code": r["utm_content"],
                 "target": "landing", "script": r["script"], "qr": ""}
            out.append(entry(c, r))
        return out

    def _boosts_api(self, q):
        keyword = (q.get("keyword") or [""])[0].strip()
        if not keyword:
            return self._send(200, {"keyword": "", "boosts": []})
        return self._send(200, {"keyword": keyword,
                                 "boosts": self._boosts_for(keyword)})

    def _run_boosts(self):
        """Run boost campaigns for a niche for real: persist each campaign with
        its tracked link, weave in SEM long-tail copy, warm the lead-magnet PDF,
        and ping IndexNow for the landing URL. Returns the live boosts feed."""
        body = self._body()
        keyword = str(body.get("keyword") or "").strip()
        niche = self._saved_niche(keyword)
        if not niche:
            return self._send(404, {"error": "no saved niche matches that keyword"})
        slug = seo._slugify(keyword)
        keywords = self._boost_longtail(keyword)
        campaigns = market_engine.build_boost_campaigns(
            keyword, niche["products"] or [], base_url=seo.BASE_URL,
            slug=slug, keywords=keywords)
        names = body.get("names") or []
        if isinstance(names, str):
            names = [n.strip() for n in names.split(",") if n.strip()]
        pick = campaigns
        if names:
            pick = [c for c in campaigns
                    if c["name"] in names or c.get("id") in names]
            if not pick:
                return self._send(400, {"error": "no campaign matches those names"})
        now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        with _lock:
            conn = _db()
            for c in pick:
                row = conn.execute(
                    "SELECT id FROM boosts WHERE lower(slug)=? AND name=?",
                    (slug, c["name"])).fetchone()
                if row:
                    conn.execute(
                        "UPDATE boosts SET script=?, link=?, utm_content=?, "
                        "runs=runs+1, status='active', updated_at=? WHERE id=?",
                        (c["script"], c["link"], c["code"], now, row["id"]))
                else:
                    conn.execute(
                        "INSERT INTO boosts (slug, keyword, name, script, link, "
                        "utm_content, runs, status, updated_at) VALUES (?,?,?,?,?,?,1,'active',?)",
                        (slug, keyword, c["name"], c["script"], c["link"],
                         c["code"], now))
            conn.commit()
            conn.close()
        try:
            dry = self._ebook_for(keyword) is not None
        except Exception:
            dry = False
        self._fire_indexnow(["/lp/" + slug])
        boosts = self._boosts_for(keyword, keywords=keywords)
        return self._send(200, {
            "ok": True, "keyword": keyword, "ran": len(pick),
            "runs_total": sum(b["runs"] for b in boosts),
            "ebook_cached": dry, "indexnow_queued": True,
            "sem_keywords": list(keywords), "boosts": boosts,
        })

    def _boosts_to_social(self):
        """Push one boost campaign to the social page as a published "Boost"
        post — same UTM link, real webhook fire, click attribution included."""
        body = self._body()
        keyword = str(body.get("keyword") or "").strip()
        name = str(body.get("campaign") or "").strip()
        if not keyword or not name:
            return self._send(400, {"error": "keyword and campaign required"})
        boosts = self._boosts_for(keyword)
        c = next((b for b in boosts if b["name"] == name or b["id"] == name), None)
        if not c:
            return self._send(404, {"error": "no matching boost campaign"})
        slug = seo._slugify(keyword)
        now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        kit = {"platform": "Boost", "name": "Boost · %s" % c["name"],
               "body": c["script"], "link": c["link"],
               "utm_content": c["code"], "keyword": keyword}
        with _lock:
            conn = _db()
            row = conn.execute(
                "SELECT id FROM social_posts WHERE lower(slug)=? AND utm_content=?",
                (slug, c["code"])).fetchone()
            if row:
                conn.execute(
                    "UPDATE social_posts SET status='published', published_at=?, "
                    "name=?, body=?, link=? WHERE id=?",
                    (now, kit["name"], kit["body"], kit["link"], row["id"]))
            else:
                conn.execute(
                    "INSERT INTO social_posts (slug, keyword, platform, name, body, "
                    "link, utm_content, status, published_at) "
                    "VALUES (?,?,?,?,?,?,?, 'published', ?)",
                    (slug, keyword, "Boost", kit["name"], kit["body"],
                     kit["link"], c["code"], now))
            conn.commit()
            conn.close()
        self._publish_kits_best_effort([kit])
        _, stats = self._social_db(keyword, slug, None)
        return self._send(200, {"ok": True, "post": kit,
                                "clicks": c["clicks"], "stats": stats})


# ------------------------------------------------------------------ CMS suite
    def _cms_pages_payload(self):
        """List all CMS-managed pages with their niche keyword, status and stats."""
        with _lock:
            conn = _db()
            try:
                pages = cms_mod.list_pages(conn)
                out = []
                for p in pages:
                    sections = cms_mod.get_sections(conn, p["id"])
                    out.append({
                        "id": p.get("id"),
                        "keyword": p.get("keyword"),
                        "slug": p.get("slug"),
                        "enabled": bool(p.get("enabled", 1)),
                        "section_count": len(sections),
                        "live_url": "/lp/" + p.get("slug", ""),
                        "updated_at": p.get("updated_at"),
                    })
            finally:
                conn.close()
        return {"pages": out}

    def _cms_pages_api(self):
        return self._send(200, self._cms_pages_payload())

    def _cms_page_payload(self, page_id):
        with _lock:
            conn = _db()
            try:
                page = cms_mod._row_to_dict(conn.execute(
                    "SELECT * FROM lead_pages WHERE id=?", (page_id,)).fetchone())
                if not page:
                    return None
                sections = cms_mod.get_sections(conn, page["id"])
                # merge defaults so the editor shows the full value set
                full_sections = []
                for s in sections:
                    st = s.get("section_type")
                    defaults = cms_mod._DEFAULT_SECTIONS.get(st, {})
                    merged = dict(defaults)
                    merged.update(s.get("content") or {})
                    full_sections.append({
                        "id": s.get("id"),
                        "type": st,
                        "label": cms_mod.SECTION_TYPES.get(st, {}).get("label", st),
                        "icon": cms_mod.SECTION_TYPES.get(st, {}).get("icon", "🧩"),
                        "fields": cms_mod.SECTION_TYPES.get(st, {}).get("fields", []),
                        "enabled": bool(s.get("enabled", 1)),
                        "sort_order": s.get("sort_order", 0),
                        "content": merged,
                    })
                page["sections"] = full_sections
                page["section_types"] = list(cms_mod.SECTION_TYPES.keys())
                page["style_defaults"] = cms_mod.DEFAULT_STYLE
                return page
            finally:
                conn.close()

    def _cms_page_api(self, match, q):
        page_id = int(match.group(1))
        page = self._cms_page_payload(page_id)
        if not page:
            return self._send(404, {"error": "page not found"})
        return self._send(200, page)

    def _cms_save_page(self):
        """Save page-level settings (style, settings, section_order)."""
        body = self._body()
        page_id = int(body.get("page_id") or 0)
        with _lock:
            conn = _db()
            try:
                row = conn.execute(
                    "SELECT id FROM lead_pages WHERE id=?", (page_id,)).fetchone()
                if not row:
                    return self._send(404, {"error": "page not found"})
                update = {}
                if "style" in body:
                    update["style"] = body["style"]
                if "settings" in body:
                    update["settings"] = body["settings"]
                if "section_order" in body:
                    update["section_order"] = body["section_order"]
                if "enabled" in body:
                    update["enabled"] = int(bool(body["enabled"]))
                cms_mod.update_page(conn, page_id, update)
            finally:
                conn.close()
        return self._send(200, self._cms_page_payload(page_id))

    def _cms_apply_preset(self):
        """One-click restyle: apply a named style preset (color/mode/shape)."""
        body = self._body()
        page_id = int(body.get("page_id") or 0)
        name = str(body.get("preset") or "").strip()
        if name not in cms_mod.STYLE_PRESETS:
            return self._send(400, {"error": "unknown preset"})
        with _lock:
            conn = _db()
            try:
                row = conn.execute(
                    "SELECT id FROM lead_pages WHERE id=?", (page_id,)).fetchone()
                if not row:
                    return self._send(404, {"error": "page not found"})
                cms_mod.apply_preset(conn, page_id, name)
            finally:
                conn.close()
        return self._send(200, self._cms_page_payload(page_id))

    def _cms_generate(self):
        """One-click copy generation: reseed all section copy from the niche's
        live data (persuasion defaults). Keeps style + settings toggles."""
        body = self._body()
        page_id = int(body.get("page_id") or 0)
        keyword = str(body.get("keyword") or "").strip()
        with _lock:
            conn = _db()
            try:
                row = conn.execute(
                    "SELECT keyword FROM lead_pages WHERE id=?", (page_id,)).fetchone()
                if not row:
                    return self._send(404, {"error": "page not found"})
                keyword = keyword or row["keyword"]
                count = cms_mod.generate_copy(conn, page_id, keyword)
            finally:
                conn.close()
        return self._send(200, {"ok": True, "sections": count,
                                "payload": self._cms_page_payload(page_id)})

    def _cms_update_section(self, page_id, section_id):
        """Update one section's content/enabled/sort_order."""
        page_id = int(page_id)
        section_id = int(section_id)
        body = self._body()
        with _lock:
            conn = _db()
            try:
                row = conn.execute(
                    "SELECT id FROM lead_sections WHERE id=? AND page_id=?",
                    (section_id, page_id)).fetchone()
                if not row:
                    return self._send(404, {"error": "section not found"})
                update = {}
                if "content" in body:
                    update["content"] = body["content"]
                if "enabled" in body:
                    update["enabled"] = int(bool(body["enabled"]))
                if "sort_order" in body:
                    update["sort_order"] = int(body["sort_order"])
                cms_mod.update_section(conn, section_id, update)
            finally:
                conn.close()
        return self._send(200, self._cms_page_payload(page_id))

    def _admin_cms(self, q):
        """Admin content-management editor for lead pages."""
        niches = [n["keyword"] for n in self._all_niches()]
        keyword = (q.get("keyword") or [""])[0].strip() or (niches[0] if niches else "")
        opts = "".join('<option value="%s"%s>%s</option>' % (seo._clean(k),
                        ' selected' if k == keyword else "", seo._clean(k)) for k in niches)
        page_id = None
        page_payload = None
        if keyword:
            with _lock:
                conn = _db()
                try:
                    p = cms_mod.get_or_create_page(conn, keyword)
                    page_id = p["id"]
                finally:
                    conn.close()
            page_payload = self._cms_page_payload(page_id)
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>CMS — pstore</title><link rel="stylesheet" href="/style.css">
<script src="/ai-fill.js" defer></script>
<meta name="robots" content="noindex,nofollow">
<style>
.cms-layout {{ display:grid; grid-template-columns: 1fr 340px; gap: 20px; align-items:start; }}
@media (max-width:860px) {{ .cms-layout {{ grid-template-columns:1fr; }} }}
.field {{ margin-bottom:14px; }}
.field label {{ font-weight:700; margin-bottom:6px; }}
.section-editor {{ border:1px solid var(--border); border-radius:16px; padding:16px;
  margin-bottom:14px; background:#fffdf8; }}
.section-editor h3 {{ margin:0; font-size:15px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
.section-editor textarea {{ min-height:70px; }}
.items-editor textarea {{ min-height:110px; font-family:ui-monospace,Menlo,monospace; font-size:12px; }}
.cols2 {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
.color-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:10px; }}
.color-grid label {{ font-size:11.5px; }}
.outline {{ border:1px solid var(--border); border-radius:12px; padding:12px; margin:10px 0; background:#fff; }}
.preset-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:12px; }}
.preset-btn {{ text-align:left; border:2px solid var(--border); border-radius:16px; padding:12px; cursor:pointer;
  background:#fff; transition:border-color .15s ease, transform .15s ease; }}
.preset-btn:hover {{ transform:translateY(-2px); }}
.preset-btn.active {{ border-color:var(--accent,#ff6b2c); background:#fff6ee; }}
.preset-btn .swatches {{ display:flex; gap:6px; margin-bottom:8px; }}
.preset-btn .swatches i {{ width:22px; height:22px; border-radius:50%; border:1px solid rgba(0,0,0,.12); display:inline-block; }}
.preset-btn b {{ display:block; font-size:13.5px; }}
.preset-btn span {{ font-size:11.5px; color:var(--muted); }}
/* toggle switch */
.sw {{ position:relative; display:inline-block; width:44px; height:24px; vertical-align:middle; flex-shrink:0; }}
.sw input {{ opacity:0; width:0; height:0; }}
.sw .sl {{ position:absolute; inset:0; background:#cbd5e1; border-radius:999px; transition:.2s; cursor:pointer; }}
.sw .sl:before {{ content:""; position:absolute; width:18px; height:18px; left:3px; top:3px; background:#fff;
  border-radius:50%; transition:.2s; box-shadow:0 1px 3px rgba(0,0,0,.25); }}
.sw input:checked + .sl {{ background:#22c55e; }}
.sw input:checked + .sl:before {{ transform:translateX(20px); }}
.toggle-row {{ display:flex; align-items:center; gap:10px; padding:8px 0; }}
.toggle-row .lbl {{ font-weight:700; font-size:14px; }}
.toggle-row .sub {{ color:var(--muted); font-size:12px; }}
.sec-head {{ display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap; }}
.sec-actions {{ display:flex; align-items:center; gap:10px; }}
.copy-expand {{ border-top:1px dashed var(--border); margin-top:12px; padding-top:12px; }}
details.copy-details summary {{ cursor:pointer; color:var(--accent,#ff6b2c); font-weight:700; font-size:13px; }}
.hint-sm {{ font-size:12px; color:var(--muted); }}
.bigbtn {{ width:100%; padding:16px; font-size:17px; font-weight:800; border-radius:14px;
  border:0; cursor:pointer; color:#fff; background:linear-gradient(135deg,#ff6b2c,#ff873c); }}
</style>
</head><body>
<header id="top"><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>Lead page <span>CMS.</span></h1>
<p class="tagline">Edit every element of a niche's landing page — style, sections, CTA, email-gated PDF download, persuasion elements — without touching code.</p></div>
{self._admin_nav('cms')}
</header>
<main>
<section class="card"><h2>🧩 Choose a niche</h2>
<form class="row" method="get" action="/admin/cms">
  <label>Niche <select name="keyword" onchange="this.form.submit()">{opts}</select></label>
</form>
{('<p class="hint">Select a saved niche to start editing its landing page. Each niche gets its own editable page with sensible, persuasion-engineered defaults.</p>' if not keyword else '')}
</section>
{self._cms_admin_editor_html(keyword, page_id, page_payload)}
</main>
<footer><p>CMS edits go live on the page immediately after saving. Uses the “How to Sell Like Crazy” and “Influence” playbook — reciprocity, commitment, social proof, authority, scarcity — all editable per section.</p></footer>
{_TOTOP}
<script>
(function () {{
  "use strict";
  var PAGE_ID = {int(page_id or 0)};
  var KEYWORD = {json.dumps(keyword or "")};
  function $(id) {{ return document.getElementById(id); }}
  function say(m, ok) {{
    var el = $("save-msg");
    if (!el) return;
    el.textContent = m;
    el.style.color = ok ? "#159a4b" : "#d64545";
  }}
  function post(path, body) {{
    return fetch(path, {{
      method: "POST", headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify(body)
    }}).then(function (r) {{ return r.json(); }});
  }}
  function collectStyle() {{
    var st = {{}};
    document.querySelectorAll("[data-style]").forEach(function (i) {{
      st[i.getAttribute("data-style")] = i.value;
    }});
    st.font_family = $("cms-font") ? $("cms-font").value : "";
    st.border_radius = $("cms-radius") ? $("cms-radius").value : "";
    st.cta_gradient = $("cms-cta") ? $("cms-cta").value : "";
    st.layout = $("cms-layout") ? $("cms-layout").value : "centered";
    st.hero_style = $("cms-hero") ? $("cms-hero").value : "gradient";
    st.mode = $("cms-mode") ? $("cms-mode").value : "light";
    var preset = document.querySelector("[data-preset].active");
    if (preset) st.preset = preset.getAttribute("data-preset");
    return st;
  }}
  function collectSettings() {{
    var s = {{ use_cms: $("use-cms") ? $("use-cms").checked : true }};
    var gate = $("cms-gate");
    if (gate) s.pdf_gated = gate.value === "1";
    s.email_gate_enabled = gate ? s.pdf_gated : true;
    s.promo_enabled = $("cms-promo") ? $("cms-promo").checked : false;
    s.promo = {{ text: $("cms-promo-text") ? $("cms-promo-text").value : "",
                code: $("cms-promo-code") ? $("cms-promo-code").value : "" }};
    s.countdown_enabled = $("cms-countdown") ? $("cms-countdown").checked : false;
    s.countdown_minutes = parseInt($("cms-cd-min") ? $("cms-cd-min").value : "30", 10);
    s.countdown_headline = $("cms-cd-head") ? $("cms-cd-head").value : "";
    s.countdown_done = $("cms-cd-done") ? $("cms-cd-done").value : "";
    s.sticky_cta = $("cms-sticky") ? $("cms-sticky").checked : true;
    s.animation = $("cms-anim") ? $("cms-anim").checked : true;
    return s;
  }}
  function sectionContent(sec) {{
    var content = {{}};
    sec.querySelectorAll(".sec-input").forEach(function (txt) {{
      var field = txt.getAttribute("data-field");
      var raw = txt.value;
      if (txt.classList.contains("items-editor")) {{
        try {{ content[field] = JSON.parse(raw); }}
        catch (e) {{ content[field] = raw; }}
      }} else {{ content[field] = raw; }}
    }});
    return content;
  }}
  function saveAll(ev) {{
    ev && ev.preventDefault();
    say("Saving…");
    post("/api/cms/page", {{ page_id: PAGE_ID, style: collectStyle(), settings: collectSettings() }})
      .then(function (d) {{ say("Page settings saved ✓ — live now.", true); }})
      .catch(function () {{ say("Save failed.", false); }});
  }}
  function saveSection(sec, enabled) {{
    var sid = sec.getAttribute("data-sec");
    var toggle = sec.querySelector(".sec-enable");
    var body = {{ section_id: parseInt(sid, 10), content: sectionContent(sec) }};
    body.enabled = enabled !== undefined ? enabled : (toggle ? toggle.checked : true);
    say("Saving section…");
    post("/api/cms/pages/" + PAGE_ID + "/sections/" + sid, body)
      .then(function (d) {{ say("Section saved ✓ — live now.", true); }})
      .catch(function () {{ say("Section save failed.", false); }});
  }}
  function applyPreset(name) {{
    say("Applying " + name + " template…");
    post("/api/cms/preset", {{ page_id: PAGE_ID, preset: name }})
      .then(function (d) {{
        if (d.error) {{ say("Couldn't apply template.", false); return; }}
        location.reload();
      }})
      .catch(function () {{ say("Template apply failed.", false); }});
  }}
  function generatePage() {{
    say("Generating fresh copy from your niche data…");
    post("/api/cms/generate", {{ page_id: PAGE_ID, keyword: KEYWORD }})
      .then(function (d) {{
        if (d.ok) {{ say("Page regenerated ✓ — " + d.sections + " sections written.", true); setTimeout(function () {{ location.reload(); }}, 700); }}
        else {{ say("Generation failed.", false); }}
      }})
      .catch(function () {{ say("Generation failed.", false); }});
  }}
  var saveAllBtn = $("save-all");
  if (saveAllBtn) saveAllBtn.onclick = saveAll;
  var genBtn = $("generate-page");
  if (genBtn) genBtn.onclick = generatePage;
  function syncToggle(boxId, panelId, hiddenId) {{
    var box = $(boxId), panel = $(panelId);
    if (!box) return;
    var sync = function () {{
      if (panel) panel.style.display = box.checked ? "block" : "none";
      if (hiddenId) {{
        var h = $(hiddenId);
        if (h) h.value = box.checked ? "1" : "0";
      }}
    }};
    box.addEventListener("change", sync);
    sync();
  }}
  syncToggle("cms-promo", "promo-fields");
  syncToggle("cms-countdown", "cd-fields");
  syncToggle("cms-gate2", null, "cms-gate");
  /* feature toggles persist the moment they flip, so what you see on the
     live page follows the switch — same instant behavior as section toggles */
  var autoSaveTimer = null;
  function autoSaveSettings() {{
    clearTimeout(autoSaveTimer);
    autoSaveTimer = setTimeout(function () {{
      say("Saving settings…");
      post("/api/cms/page", {{ page_id: PAGE_ID, style: collectStyle(), settings: collectSettings() }})
        .then(function (d) {{ say("Settings saved ✓ — live now.", true); }})
        .catch(function () {{ say("Save failed.", false); }});
    }}, 350);
  }}
  ["cms-promo", "cms-countdown", "cms-sticky", "cms-anim", "cms-gate2", "use-cms"]
    .forEach(function (id) {{
      var box = $(id);
      if (box) box.addEventListener("change", autoSaveSettings);
    }});
  document.querySelectorAll(".preset-btn").forEach(function (btn) {{
    btn.onclick = function () {{ applyPreset(btn.getAttribute("data-preset")); }};
  }});
  document.querySelectorAll(".section-editor").forEach(function (sec) {{
    var b = sec.querySelector(".sec-save");
    if (b) b.onclick = function (ev) {{ ev.preventDefault(); saveSection(sec); }};
    var t = sec.querySelector(".sec-enable");
    if (t) t.onchange = function () {{ saveSection(sec, t.checked); }};
  }});
  document.querySelectorAll(".field textarea, .field input, .field select").forEach(function (el) {{
    el.addEventListener("keydown", function (ev) {{
      if ((ev.ctrlKey || ev.metaKey) && ev.key === "s") {{ ev.preventDefault(); saveAll(); }}
    }});
  }});
}})();
</script>
</body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _cms_admin_editor_html(self, keyword, page_id, page):
        """The actual editor UI for one page. If no page payload, return nothing.

        One-click first: style templates (presets), page toggles (promo,
        countdown, sticky CTA, email gate), per-section show/hide switches, and
        a big "Generate page" button that re-writes copy from niche data.
        Fine-grained copy + color editing collapse under "Edit copy" so the
        screen stays clean.
        """
        if not page:
            return ""
        e = seo._clean
        style = dict(page.get("style") or {})
        if not style.get("preset"):
            style["preset"] = "sunset"
        s = cms_mod.merge_settings(page.get("settings") or {})
        promo = s.get("promo") or {}
        sections = page.get("sections") or []
        live_url = "/lp/" + e(seo._slugify(keyword))
        current_preset = style.get("preset", "sunset")

        # ── one-click style templates ──────────────────────────────────────
        preset_cards = []
        for name, p in cms_mod.STYLE_PRESETS.items():
            sw = "".join('<i style="background:%s"></i>' % e(c) for c in p.get("swatches", []))
            active = " active" if name == current_preset else ""
            preset_cards.append(
                '<button class="preset-btn%s" data-preset="%s">'
                '<span class="swatches">%s</span><b>%s</b><span>%s</span></button>'
                % (active, e(name), sw, e(p.get("label", name)), e(p.get("desc", ""))))
        presets_html = "".join(preset_cards)

        # ── fine style controls ────────────────────────────────────────────
        color_fields = {
            "bg": style.get("bg"), "card_bg": style.get("card_bg"),
            "accent": style.get("accent"), "accent2": style.get("accent2"),
            "text": style.get("text"), "muted": style.get("muted"),
        }
        color_html = "".join(
            '<label>{name}<input type="color" value="{v}" data-style="{k}"></label>'
            .format(name=k.replace("_", " ").title(), k=k, v=e(v or "#ffffff"))
            for k, v in color_fields.items())
        layout_opts = ""
        for lo in ("centered", "wide", "split"):
            sel = ' selected' if (style.get('layout') or 'centered') == lo else ''
            layout_opts += '<option value="%s"%s>%s</option>' % (lo, sel, lo.title())
        hero_labels = {"gradient": "Gradient", "minimal": "Minimal", "bold": "Bold headline"}
        hero_opts = ""
        for ho in ("gradient", "minimal", "bold"):
            sel = ' selected' if (style.get('hero_style') or 'gradient') == ho else ''
            hero_opts += '<option value="%s"%s>%s</option>' % (ho, sel, hero_labels[ho])
        mode_opts = '<option value="light"%s>Light</option><option value="dark"%s>Dark</option>' % (
            ' selected' if (style.get('mode') or 'light') == 'light' else '',
            ' selected' if (style.get('mode') or 'light') == 'dark' else '')

        # ── section editors (toggle + collapsible copy) ────────────────────
        section_editors = []
        for s_idx, sec in enumerate(sections):
            fid = sec["id"]
            stype = sec.get("section_type", "")
            content = sec.get("content") or {}
            body_editor = ""
            for field in sec.get("fields", []):
                val = content.get(field, "")
                ai_key = self._AI_FILL_FIELDS.get((stype, field), "")
                ai_attr = (' data-ai-fill="%s" data-ai-niche="%s"'
                           % (e(ai_key), e(keyword))) if ai_key else ""
                if isinstance(val, (dict, list)):
                    import json as _json
                    val = _json.dumps(val, indent=1)
                    body_editor += ('<div class="field"><label>%s</label>'
                                    '<textarea class="sec-input items-editor" data-field="%s" '
                                    'data-sec="%d"%s>%s</textarea></div>'
                                    % (field.replace("_", " ").title(), field, fid, ai_attr, e(val)))
                else:
                    body_editor += ('<div class="field"><label>%s</label>'
                                    '<textarea class="sec-input" data-field="%s" data-sec="%d"%s>%s</textarea></div>'
                                    % (field.replace("_", " ").title(), field, fid, ai_attr, e(val)))
            enabled = sec.get("enabled", True)
            section_editors.append(f"""
<div class="section-editor" data-sec="{fid}" id="sec-{fid}">
  <div class="sec-head">
    <h3>{sec.get('icon','🧩')} {e(sec.get('label',''))}</h3>
    <div class="sec-actions">
      <span class="hint-sm">Show on page</span>
      <label class="sw"><input type="checkbox" class="sec-enable" data-sec="{fid}" {'checked' if enabled else ''}>
        <span class="sl"></span></label>
      <button class="mini sec-save" data-sec="{fid}">Save copy</button>
    </div>
  </div>
  <details class="copy-details">
    <summary>✏️ Edit copy for this section</summary>
    <div class="outline">{body_editor}</div>
  </details>
</div>""")
        section_html = "".join(section_editors) if section_editors else \
            '<p class="hint">No sections yet — hit “Generate page” to seed defaults.</p>'

        return f"""
<section class="card"><h2>⚡ Build your page</h2>
<div class="row" style="gap:10px">
  <a class="btn" href="{live_url}" target="_blank" rel="noopener">👁 View live page ↗</a>
  <button class="warm" id="save-all">💾 Save all</button>
</div>
<button class="bigbtn" id="generate-page" style="margin-top:12px">⚡ Generate page copy</button>
<p class="hint-sm" style="margin-top:6px">One click re-writes all section copy from your niche's live data (headline, offers, proof, urgency, FAQ, CTA). Your chosen template and toggles stay untouched.</p>
<p id="save-msg" class="msg"></p>
</section>

<section class="card"><h2>🎨 Pick a template</h2>
<p class="hint-sm" style="margin-top:-4px">One click restyles the whole page. Fine-tune colors below after picking.</p>
<div class="preset-grid">{presets_html}</div>
<details class="copy-details" style="margin-top:14px">
  <summary>🎛️ Advanced style (colors / layout)</summary>
  <div class="outline">
    <div class="cols2 color-grid">{color_html}</div>
    <div class="cols2" style="margin-top:12px">
      <div class="field"><label>Mode</label><select id="cms-mode">{mode_opts}</select></div>
      <div class="field"><label>Layout</label><select id="cms-layout">{layout_opts}</select></div>
      <div class="field"><label>Hero style</label><select id="cms-hero">{hero_opts}</select></div>
      <div class="field"><label>Border radius</label><input type="text" id="cms-radius" value="{e(style.get('border_radius',''))}" placeholder="22px"></div>
      <div class="field"><label>Font family</label><input type="text" id="cms-font" value="{e(style.get('font_family',''))}" placeholder="CSS font stack"></div>
      <div class="field"><label>CTA gradient (CSS)</label><input type="text" id="cms-cta" value="{e(style.get('cta_gradient',''))}" placeholder="linear-gradient(...)"></div>
    </div>
  </div>
</details>
</section>

<section class="card"><h2>🚀 Page features</h2>
<div class="toggle-row">
  <label class="sw"><input type="checkbox" id="cms-promo" {'checked' if s.get('promo_enabled') else ''}><span class="sl"></span></label>
  <div><div class="lbl">Promo banner</div><div class="sub">Colored announcement strip at the very top</div></div>
</div>
<div class="outline" id="promo-fields" style="{'display:block' if s.get('promo_enabled') else 'display:none'}">
  <div class="cols2">
    <div class="field"><label>Promo text</label><input type="text" id="cms-promo-text" value="{e(promo.get('text',''))}" placeholder="Free shipping on orders over $25"></div>
    <div class="field"><label>Promo code</label><input type="text" id="cms-promo-code" value="{e(promo.get('code',''))}" placeholder="SAVE10"></div>
  </div>
</div>
<div class="toggle-row">
  <label class="sw"><input type="checkbox" id="cms-countdown" {'checked' if s.get('countdown_enabled') else ''}><span class="sl"></span></label>
  <div><div class="lbl">Countdown timer</div><div class="sub">Live HH:MM:SS boxes under the header — scarcity in one click</div></div>
</div>
<div class="outline" id="cd-fields" style="{'display:block' if s.get('countdown_enabled') else 'display:none'}">
  <div class="cols2">
    <div class="field"><label>Countdown length</label>
      <select id="cms-cd-min">{''.join('<option value="%d"%s>%d minutes</option>' % (m, ' selected' if int(s.get('countdown_minutes') or 30) == m else '', m) for m in (5, 10, 15, 30, 45, 60))}</select></div>
    <div class="field"><label>Headline above timer</label><input type="text" id="cms-cd-head" value="{e(s.get('countdown_headline',''))}" placeholder="⏳ Today's pricing refresh starts soon"></div>
  </div>
  <div class="field"><label>Message when time runs out</label><input type="text" id="cms-cd-done" value="{e(s.get('countdown_done',''))}" placeholder="Pricing just refreshed — see today's best rate below."></div>
</div>
<div class="toggle-row">
  <label class="sw"><input type="checkbox" id="cms-sticky" {'checked' if s.get('sticky_cta', True) else ''}><span class="sl"></span></label>
  <div><div class="lbl">Sticky “see the pick” bar</div><div class="sub">Floating CTA that follows the visitor after the hero</div></div>
</div>
<div class="toggle-row">
  <label class="sw"><input type="checkbox" id="cms-anim" {'checked' if s.get('animation', True) else ''}><span class="sl"></span></label>
  <div><div class="lbl">Scroll animations</div><div class="sub">Gentle reveal-on-scroll</div></div>
</div>
<div class="toggle-row">
  <label class="sw"><input type="checkbox" id="cms-gate2" {'checked' if s.get('pdf_gated', True) else ''}><span class="sl"></span></label>
  <div><div class="lbl">Email-gated PDF lead magnet</div><div class="sub">On = visitors give email to download the guide; Off = direct download</div></div>
  <input type="hidden" id="cms-gate" value="{1 if s.get('pdf_gated', True) else 0}">
</div>
<div class="toggle-row">
  <label class="sw"><input type="checkbox" id="use-cms" {'checked' if s.get('use_cms', True) else ''}><span class="sl"></span></label>
  <div><div class="lbl">Use CMS renderer</div><div class="sub">Turn off to fall back to the legacy template</div></div>
</div>
</section>

<section class="card"><h2>🧱 Page sections</h2>
<p class="hint-sm" style="margin-top:-4px">Flip a switch to show or hide a section (applies instantly). Open “Edit copy” to reword it — complex lists use a compact JSON list.</p>
{section_html}
</section>
"""

    def _saved_niche(self, keyword):
        keyword = (keyword or "").strip().lower()
        for n in self._all_niches():
            if (n["keyword"] or "").strip().lower() == keyword:
                return n
        return None

    def _webhook_url(self):
        """Effective SOCIAL_WEBHOOK — live env first, then whatever was set at
        boot (so a webhook can be flipped on without restarting, and tests can
        inject one at runtime)."""
        return os.environ.get("SOCIAL_WEBHOOK", "") or _SOCIAL_WEBHOOK

    def _social_kits(self, keyword):
        """Six UTM-tracked post kits for the niche's top pick (empty when the
        niche has no products or no saved niche matches). Kit URLs are stable
        per (slug, platform): once a post exists its attribution code is reused,
        so re-publishing flips status instead of forking a new tracked link."""
        n = self._saved_niche(keyword)
        if not n:
            return []
        slug = seo._slugify(n["keyword"])
        kits = social.post_kits(n["keyword"], n["products"] or [],
                                base_url=seo.BASE_URL, slug=slug)
        if not kits:
            return kits
        with _lock:
            conn = _db()
            rows = conn.execute(
                "SELECT platform, utm_content FROM social_posts WHERE lower(slug)=?",
                (slug,)).fetchall()
            conn.close()
        keep = {}
        for r in rows:
            keep.setdefault(r["platform"], r["utm_content"])
        out = []
        for kit in kits:
            code = keep.get(kit["platform"])
            if code:
                kit["utm_content"] = code
                kit["link"] = social.track_link(seo.BASE_URL, slug,
                                                kit["platform"], code)
            variants = self._caption_variants(slug, kit["platform"])
            if len(variants) > 1:
                # Per-variant posts: each enabled caption variant becomes its own
                # published post with a stable variant-suffixed tracked code, so a
                # winner can be auto-kept from real per-variant clicks.
                for v in variants:
                    k2 = dict(kit)
                    vcode = "%s-c%s" % (kit["utm_content"], v["variant"])
                    k2["body"] = v["caption"]
                    k2["utm_content"] = vcode
                    k2["link"] = social.track_link(seo.BASE_URL, slug,
                                                   k2["platform"], vcode)
                    k2["caption_variant"] = v["variant"]
                    out.append(k2)
            elif variants:
                # Single caption variant: keep the one stable post, live body swap.
                kit["body"] = variants[0]["caption"]
                kit["caption_variant"] = variants[0]["variant"]
                out.append(kit)
            else:
                out.append(kit)
        return out

    def _caption_variants(self, slug, platform):
        """Enabled caption A/B variants for a (slug, platform)."""
        with _lock:
            conn = _db()
            rows = conn.execute(
                "SELECT variant, caption FROM social_captions "
                "WHERE lower(slug)=? AND lower(platform)=? AND enabled=1 "
                "AND caption IS NOT NULL AND caption != '' ORDER BY variant ASC",
                (slug.lower(), platform.lower())).fetchall()
            conn.close()
        return [{"variant": r["variant"], "caption": r["caption"]} for r in rows]

    def _caption_for(self, slug, platform, default_body):
        """Social-caption A/B: pick a deterministic alternative body copy per
        (slug, platform) variant by visitor hash, so cohorts split. The active
        variant is woven into the kit (and thus the published post). Falls back
        to the default caption when no variant is set. Returns the caption."""
        with _lock:
            conn = _db()
            rows = conn.execute(
                "SELECT variant, caption FROM social_captions "
                "WHERE lower(slug)=? AND lower(platform)=? AND enabled=1 "
                "AND caption IS NOT NULL AND caption != '' ORDER BY variant ASC",
                (slug.lower(), platform.lower())).fetchall()
            conn.close()
        if not rows:
            return default_body
        idx = int(security.ip_token(self._client_ip()) or "0", 16) % len(rows)
        return rows[idx]["caption"]

    def _captions_for(self, slug):
        with _lock:
            conn = _db()
            rows = conn.execute(
                "SELECT platform, variant, caption, enabled FROM social_captions "
                "WHERE lower(slug)=? ORDER BY platform, variant", (slug.lower(),)).fetchall()
            conn.close()
        return [dict(r) for r in rows]

    def _captions_save(self):
        body = self._body()
        slug = str(body.get("slug") or "").strip().lower()
        items = body.get("variants") or []
        if not slug:
            return self._send(400, {"error": "slug required"})
        with _lock:
            conn = _db()
            for it in items:
                platform = str(it.get("platform") or "").strip()
                variant = int(it.get("variant") or 1)
                caption = str(it.get("caption") or "").strip()
                enabled = 1 if it.get("enabled", True) else 0
                if not platform:
                    continue
                conn.execute(
                    "INSERT INTO social_captions (slug, platform, variant, caption, enabled) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(slug, platform, variant) "
                    "DO UPDATE SET caption=excluded.caption, enabled=excluded.enabled",
                    (slug, platform, variant, caption, enabled))
            conn.commit()
            conn.close()
        return self._send(200, {"ok": True, "slug": slug, "saved": len(items)})

    def _captions_autoclean(self, min_clicks=None):
        """Caption A/B auto-cleanup: for each (slug, platform) published as
        per-variant posts, once a platform has enough lifetime clicks, disable any
        caption variant whose tracked clicks are far below the platform's leader
        (the winner is kept). Mirrors the subject/headline autoclean behaviour."""
        if min_clicks is None:
            try:
                min_clicks = int(_get_setting("ab.captions_min_clicks") or 40)
            except (TypeError, ValueError):
                min_clicks = 40
        changed = []
        with _lock:
            conn = _db()
            slugs = conn.execute(
                "SELECT DISTINCT lower(slug) AS s FROM social_captions WHERE enabled=1").fetchall()
            conn.close()
        for r in slugs:
            slug = r["s"]
            with _lock:
                conn = _db()
                posts = conn.execute(
                    "SELECT platform, utm_content FROM social_posts WHERE lower(slug)=?",
                    (slug,)).fetchall()
                conn.close()
            base = {}
            for p in posts:
                c = p["utm_content"] or ""
                m = re.match(r"^(.*)-c\d+$", c)
                stem = m.group(1) if m else c
                if stem:
                    base.setdefault((p["platform"] or "").lower(), stem)
            with _lock:
                conn = _db()
                variants = conn.execute(
                    "SELECT platform, variant FROM social_captions WHERE lower(slug)=? "
                    "AND enabled=1 ORDER BY platform, variant", (slug,)).fetchall()
                conn.close()
            groups = {}
            for v in variants:
                groups.setdefault((v["platform"] or "").lower(), []).append(v["variant"])
            for platform, vs in groups.items():
                if len(vs) < 2:
                    continue
                bcode = base.get(platform) or ""
                if not bcode:
                    continue
                counts = {}
                for variant in vs:
                    code = "%s-c%s" % (bcode, variant)
                    with _lock:
                        conn = _db()
                        n = conn.execute(
                            "SELECT COUNT(*) c FROM clicks WHERE lower(slug)=? AND content=?",
                            (slug, code)).fetchone()["c"]
                        conn.close()
                    counts[variant] = n
                if sum(counts.values()) < min_clicks:
                    continue
                leader = max(counts, key=counts.get)
                losers = [v for v, n in counts.items()
                          if v != leader and n < counts[leader] * 0.25]
                if not losers:
                    continue
                with _lock:
                    conn = _db()
                    for v in losers:
                        conn.execute(
                            "UPDATE social_captions SET enabled=0 "
                            "WHERE lower(slug)=? AND lower(platform)=? AND variant=?",
                            (slug, platform, v))
                    conn.commit()
                    conn.close()
                changed.append({"slug": slug, "platform": platform,
                                "disabled": losers, "kept": leader,
                                "clicks": counts})
        return {"ok": True, "changed": changed,
                "note": ("Disabled caption variants clicking below 25%% of the "
                         "platform's leader once it reached %d lifetime clicks."
                         % min_clicks)}


    def _social_db(self, keyword, slug, kits):
        """Published posts + per-post click counts (attribution via utm_content)."""
        codes = [k.get("utm_content") for k in kits or []]
        with _lock:
            conn = _db()
            rows = conn.execute(
                "SELECT * FROM social_posts WHERE lower(slug)=? ORDER BY id",
                (slug or "",)).fetchall()
            stats = {}
            for code in codes:
                if not code:
                    continue
                stats[code] = conn.execute(
                    "SELECT COUNT(*) c FROM clicks WHERE lower(slug)=? AND content=?",
                    (slug or "", code)).fetchone()["c"]
            extra = [r["utm_content"] for r in rows
                     if r["utm_content"] and r["utm_content"] not in stats]
            for code in extra:
                stats[code] = conn.execute(
                    "SELECT COUNT(*) c FROM clicks WHERE lower(slug)=? AND content=?",
                    (slug or "", code)).fetchone()["c"]
            conn.close()
        return [dict(r) for r in rows], stats

    def _ranked_posts(self, published, stats):
        """Rank published posts by attributed clicks and tag each as a winner /
        mid / cold performer. Pure function (offline-testable)."""
        best = max(stats.values(), default=0)
        ranked = []
        for p in published:
            clicks = stats.get(p["utm_content"]) or 0
            tag = "cold"
            if clicks > 0 and (best == 0 or clicks == best):
                tag = "winner"
            elif best > 0 and clicks >= max(1, int(best * 0.5)):
                tag = "warm"
            ranked.append((clicks, tag, p))
        ranked.sort(key=lambda row: (-row[0], row[2]["platform"]))
        return ranked

    def _og_image(self, slug):
        """Generated 1200x630 SVG share card — the og:image/twitter:image the
        SEO pages point at. Served publicly + cacheable (static by slug)."""
        for n in self._all_niches():
            try:
                if slug != seo._slugify(n["keyword"]):
                    continue
                items = n["products"] or []
                pick = market_engine.pick_for_buyers(items)
                title = (pick or {}).get("title") or ("Best " + n["keyword"])
                stars = (pick or {}).get("stars")
                reviews = (pick or {}).get("reviews")
                svg = social.og_svg(slug, n["keyword"], title, stars, reviews)
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
                self.send_header("Cache-Control", "public, max-age=3600")
                self.send_header("Content-Length", str(len(svg)))
                self.end_headers()
                self.wfile.write(svg)
                return None
            except Exception:
                continue
        return self._send(404, {"error": "og image not found"})

    def _og_image_png(self, slug):
        """Raster 1200x630 share card at /og/<slug>.png — the same layout as the
        SVG card but a real PNG so Pinterest/Twitter/Facebook render it. Renders
        once per slug (in-memory cache, capped) to hold the pure-Python cost.
        /og/favicon.png renders the small brand icon instead."""
        with _lock:
            hit = _PNG_CACHE.get(slug)
        if hit is None:
            card = None
            if slug == "favicon":
                try:
                    card = social.favicon_png()
                except Exception:
                    card = None
            else:
                for n in self._all_niches():
                    try:
                        if slug != seo._slugify(n["keyword"]):
                            continue
                        items = n["products"] or []
                        pick = market_engine.pick_for_buyers(items)
                        title = (pick or {}).get("title") or ("Best " + n["keyword"])
                        stars = (pick or {}).get("stars")
                        reviews = (pick or {}).get("reviews")
                        card = social.og_png(slug, n["keyword"], title, stars, reviews)
                        break
                    except Exception:
                        continue
            if card is None:
                return self._send(404, {"error": "og image not found"})
            with _lock:
                if len(_PNG_CACHE) >= 48:
                    try:
                        _PNG_CACHE.pop(next(iter(_PNG_CACHE)))
                    except Exception:
                        pass
                _PNG_CACHE[slug] = card
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Length", str(len(card)))
        self.end_headers()
        self.wfile.write(card)
        return None

    def _social_post_page(self, slug, code):
        """Public, standalone page for one published post. Renders the actual
        post copy (platform + body) instead of redirecting to the landing page,
        so the admin's "View live" / "open <a>" links show the real post."""
        with _lock:
            conn = _db()
            try:
                row = conn.execute(
                    "SELECT * FROM social_posts WHERE lower(slug)=? AND utm_content=? "
                    "AND status IN ('published','scheduled') ORDER BY id DESC LIMIT 1",
                    (slug.lower(), code)).fetchone()
            finally:
                conn.close()
        if not row:
            return self._send(404, "<h1>Post not found</h1>", "text/html; charset=utf-8")
        d = dict(row)
        keyword = d.get("keyword") or slug
        title = d.get("name") or ("%s post" % d.get("platform"))
        body = (d.get("body") or "").replace("\n", "<br>")
        link = d.get("link") or "/lp/" + slug
        when = (d.get("published_at") or d.get("created_at") or "").split(" ")[0]
        desc = (("%s · pstore" % keyword)[:160]) if keyword else title
        status = (d.get("status") or "published")
        status_note = ('<p class="hint" style="color:#c0392b">This post is queued but not yet live — '
                       'it publishes on schedule and goes public then.</p>' if status == "scheduled" else "")
        head = seo._head(title, desc, "/social/%s/%s" % (slug, code),
                         "/social/%s/%s" % (slug, code), noindex=True)
        mkup = f"""
<header id="top"><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<nav><a href="/">Home</a><a href="/blog">Blog</a><a href="/n/{seo._clean(slug)}">Full review</a></nav></header>
<main class="wrap" style="max-width:720px;margin:0 auto;padding:24px">
<section class="card">
  <p class="hint">📣 {seo._clean(d.get('platform') or 'Social')} · {seo._clean(when)}</p>
  <h1>{seo._clean(title)}</h1>
  {status_note}
  <div style="font-size:15px;color:var(--text,#333);line-height:1.6;word-break:break-word;overflow-wrap:anywhere">{body}</div>
  <p class="key" style="margin-top:16px"><a href="{seo._clean(link)}" rel="noopener" target="_blank" style="word-break:break-word;overflow-wrap:anywhere;display:inline-block;max-width:100%">{seo._clean(link)}</a></p>
  <p class="hint">Best {seo._clean(keyword)} — researched live from Amazon.</p>
</section>
</main>
"""
        return self._send(200, head + mkup.encode("utf-8") + b"</body>\n</html>\n",
                          "text/html; charset=utf-8")

    def _social_api(self, q):
        keyword = (q.get("keyword") or [""])[0].strip()
        webhook = bool(self._webhook_url())
        if not keyword:
            return self._send(200, {"keyword": "", "kits": [], "published": [],
                                    "stats": {}, "webhook": webhook})
        kits = self._social_kits(keyword)
        slug = seo._slugify(keyword)
        published, stats = self._social_db(keyword, slug, kits)
        return self._send(200, {"keyword": keyword, "kits": kits,
                                "published": published, "stats": stats,
                                "webhook": webhook})

    def _publish_kits_best_effort(self, kits):
        """Publish kits: native per-platform POST with pasted keys first; any
        platform without creds (skipped) falls back to the webhook. Never raises
        and never blocks the response. Returns the native results."""
        if not kits:
            return []
        results = []
        try:
            results = _publish_native(kits)
            posted = {str(r.get("slug")) + "|" + str(r.get("platform"))
                      for r in results if r.get("ok") and r.get("via") == "native"}
            fallback = [k for k in kits
                        if (str(k.get("slug")) + "|" + str(k.get("platform"))) not in posted]
        except Exception:
            fallback = list(kits)
        if fallback:
            self._webhook_publish(fallback)
        return results

    def _webhook_publish(self, kits):
        """Fire-and-forget SOCIAL_WEBHOOK POSTs for each published kit, so
        Zapier/Make/browser tools (or a future native API) can post for real.
        Never raises and never blocks the publish response."""
        webhook = self._webhook_url()
        if not webhook or not kits:
            return

        def fire():
            for kit in kits:
                try:
                    req = urllib.request.Request(
                        webhook,
                        data=json.dumps(_webhook_payload(kit)).encode("utf-8"),
                        headers={"Content-Type": "application/json"}, method="POST")
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        resp.read()
                except Exception:
                    continue
        threading.Thread(target=fire, daemon=True).start()

    def _social_publish(self):
        """Publish one (or all) post kit(s) for a niche: upsert the social_posts
        row to status 'published' and fire the webhook for genuine posting."""
        body = self._body()
        keyword = str(body.get("keyword") or "").strip()
        platform = str(body.get("platform") or "all").strip()
        if not keyword:
            return self._send(400, {"error": "keyword required"})
        kits = self._social_kits(keyword)
        if not kits:
            return self._send(404, {"error": "no saved niche or top pick for that keyword"})
        if platform and platform != "all" and platform not in social.PLATFORMS:
            return self._send(400, {"error": "unknown platform"})
        pick_list = kits if platform == "all" else \
            [k for k in kits if k["platform"] == platform]
        slug = seo._slugify(keyword)
        now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        with _lock:
            conn = _db()
            ids = []
            for kit in pick_list:
                row = conn.execute(
                    "SELECT id FROM social_posts WHERE lower(slug)=? AND utm_content=?",
                    (slug, kit["utm_content"])).fetchone()
                if row:
                    conn.execute(
                        "UPDATE social_posts SET status='published', published_at=?, "
                        "name=?, body=?, link=? WHERE id=?",
                        (now, kit["name"], kit["body"], kit["link"], row["id"]))
                    ids.append(row["id"])
                else:
                    cur = conn.execute(
                        "INSERT INTO social_posts (slug, keyword, platform, name, body, "
                        "link, utm_content, status, published_at) VALUES (?,?,?,?,?,?,?, "
                        "'published', ?)",
                        (slug, keyword, kit["platform"], kit["name"], kit["body"],
                         kit["link"], kit["utm_content"], now))
                    ids.append(cur.lastrowid)
            conn.commit()
            conn.close()
        results = self._publish_kits_best_effort(pick_list)
        _, stats = self._social_db(keyword, slug, kits)
        return self._send(200, {"ok": True, "published": len(ids), "posts": pick_list,
                                "stats": stats, "webhook": bool(self._webhook_url()),
                                "native": _native_posted_count(results),
                                "keyword": keyword})

    def _schedule_times(self, count, hours=24, now=None):
        """Spread `count` posts across the next `hours`, but snap each slot to a
        high-engagement window (peak-slot biasing) so a niche's batch lands when
        readers are actually scrolling. `now` is injectable for deterministic
        tests."""
        slots = SOCIAL_PEAK_SLOTS
        now = now or datetime.datetime.utcnow()
        times = []
        for i in range(count):
            delta = float(hours) * (i + 1) / float(max(count, 1))
            t = now + datetime.timedelta(hours=delta)
            # snap to the nearest peak hour today/tomorrow
            t = self._snap_to_peak(t, slots, delta)
            times.append(t.strftime("%Y-%m-%d %H:%M:%S"))
        return times

    def _snap_to_peak(self, t, slots, delta_hours):
        """Slide a moment to the nearest of the given peak hours (still forward
        within the same spreading window)."""
        best = t
        if t.hour in slots:
            return t
        best_dist = 25
        for h in slots:
            for day in (0, 1):
                cand = t.replace(hour=h, minute=0, second=0, microsecond=0) \
                    + datetime.timedelta(days=day)
                dist = (cand - t).total_seconds() / 3600.0
                if 0 <= dist <= best_dist and dist <= float(delta_hours):
                    best_dist = dist
                    best = cand
        return best

    def _social_schedule(self):
        """Queue a niche's post kits as future, spaced publishes (rate-limit
        friendly). Webhook fires only when each post comes due via flush."""
        body = self._body()
        keyword = str(body.get("keyword") or "").strip()
        platform = str(body.get("platform") or "all").strip()
        hours = int(body.get("hours") or 24)
        if not keyword:
            return self._send(400, {"error": "keyword required"})
        kits = self._social_kits(keyword)
        if not kits:
            return self._send(404, {"error": "no saved niche or top pick for that keyword"})
        if platform and platform != "all" and platform not in social.PLATFORMS:
            return self._send(400, {"error": "unknown platform"})
        pick = kits if platform == "all" else \
            [k for k in kits if k["platform"] == platform]
        if not pick:
            return self._send(400, {"error": "platform produced no kit"})
        slug = seo._slugify(keyword)
        times = self._schedule_times(len(pick), hours=hours)
        with _lock:
            conn = _db()
            ids = []
            for kit, at in zip(pick, times):
                row = conn.execute(
                    "SELECT id FROM social_posts WHERE lower(slug)=? AND utm_content=?",
                    (slug, kit["utm_content"])).fetchone()
                if row:
                    conn.execute(
                        "UPDATE social_posts SET status='scheduled', scheduled_at=?, "
                        "name=?, body=?, link=? WHERE id=?",
                        (at, kit["name"], kit["body"], kit["link"], row["id"]))
                    ids.append(row["id"])
                else:
                    cur = conn.execute(
                        "INSERT INTO social_posts (slug, keyword, platform, name, body, "
                        "link, utm_content, status, scheduled_at) "
                        "VALUES (?,?,?,?,?,?,?,'scheduled',?)",
                        (slug, keyword, kit["platform"], kit["name"], kit["body"],
                         kit["link"], kit["utm_content"], at))
                    ids.append(cur.lastrowid)
            conn.commit()
            conn.close()
        # list scheduled posts so the UI can show the plan
        _, stats = self._social_db(keyword, slug, kits)
        return self._send(200, {"ok": True, "scheduled": len(ids),
                                "platform": platform, "hours": hours,
                                "stats": stats, "keyword": keyword})

    def _social_flush(self):
        """Publish every due scheduled post now: flip status->published, fire the
        webhook (spaced), and return what went out. Safe to call on a timer."""
        published_now, pending = _flush_due_social(self._webhook_publish)
        return self._send(200, {"ok": True, "published_now": published_now,
                                "still_pending": pending})

    def _social_amplify(self):
        """Manual trigger for the auto-amplify loop (POST /api/social/amplify),
        so an owner can re-queue winning posts on demand instead of waiting for
        the daemon. Also toggles the feature via a JSON `enable` flag."""
        body = self._body()
        if "enable" in body:
            val = "1" if body.get("enable") else "0"
            _set_setting("social.amplify", val)
        res = _auto_amplify_winners()
        return self._send(200, dict(res, ok=True))

    def _auto_amplify(self, now=None):
        """Method shim: run the module-level auto-amplify and also expose a
        manual trigger for the admin UI (POST /api/social/amplify)."""
        return _auto_amplify_winners(now=now)

    def _social_topics(self):
        """Recycle long-tail /n/<parent>/<term> pages into tracked post kits +
        scheduled social posts, so every indexed long-tail page also earns social
        traffic. Body: {niche?: keyword (default all), schedule?: bool, hours?: n}."""
        body = self._body()
        only_kw = (str(body.get("niche") or "") or "").strip()
        do_schedule = bool(body.get("schedule"))
        hours = int(body.get("hours") or 24)
        kits_built = 0
        scheduled = 0
        niches_used = []
        for n in self._all_niches():
            kw = n["keyword"]
            if only_kw and kw.strip().lower() != only_kw.strip().lower():
                continue
            products = n["products"] or []
            if not products:
                continue
            parent_slug = seo._slugify(kw)
            topics = self._topics_for(parent_slug)
            if not topics:
                continue
            niches_used.append(kw)
            for topic in topics:
                tterm = topic["term"]
                tkits = social.topic_post_kits(
                    tterm, kw, products, seo.BASE_URL, parent_slug=parent_slug,
                    slug=seo._slugify(topic["slug"]))
                kits_built += len(tkits)
                if do_schedule:
                    scheduled += self._queue_topic_kits(parent_slug, kw, tterm, tkits, hours)
        return self._send(200, {"ok": True,
                                "kits_built": kits_built,
                                "scheduled": scheduled,
                                "niches": niches_used})

    def _queue_topic_kits(self, parent_slug, kw, term, kits, hours):
        """Insert topic kits into social_posts as future scheduled publishes.
        Returns number queued."""
        if not kits:
            return 0
        times = self._schedule_times(len(kits), hours=hours)
        queued = 0
        with _lock:
            conn = _db()
            for kit, at in zip(kits, times):
                code = kit["utm_content"]
                row = conn.execute(
                    "SELECT id FROM social_posts WHERE lower(slug)=? AND utm_content=?",
                    (parent_slug, code)).fetchone()
                if row:
                    conn.execute(
                        "UPDATE social_posts SET status='scheduled', scheduled_at=?, "
                        "name=?, body=?, link=? WHERE id=?",
                        (at, kit["name"], kit["body"], kit["link"], row["id"]))
                else:
                    conn.execute(
                        "INSERT INTO social_posts (slug, keyword, platform, name, body, "
                        "link, utm_content, status, scheduled_at) "
                        "VALUES (?,?,?,?,?,?,?,'scheduled',?)",
                        (parent_slug, ("%s %s" % (kw, term)).strip(),
                         kit["platform"], kit["name"], kit["body"], kit["link"],
                         code, at))
                queued += 1
            conn.commit()
            conn.close()
        return queued

    def _admin_social(self, q):
        niches = [n["keyword"] for n in self._all_niches()]
        keyword = (q.get("keyword") or [""])[0].strip() or (niches[0] if niches else "")
        opts = "".join('<option value="%s"%s>%s</option>' % (seo._clean(k),
                        ' selected' if k == keyword else "", seo._clean(k)) for k in niches)
        if keyword:
            kits = self._social_kits(keyword)
            slug = seo._slugify(keyword)
            published, stats = self._social_db(keyword, slug, kits)
        else:
            kits, published, stats, slug = [], [], {}, ""
        webhook_state = ("<b>Configured</b> — publishing also fires your <code>SOCIAL_WEBHOOK</code>."
                         if self._webhook_url() else
                         "Not set — one click here flips the post to <b>published</b> and shows the "
                         "copy-ready kit to paste anywhere. Set <code>SOCIAL_WEBHOOK</code> to also "
                         "POST <code>{body, link, platform}</code> to Zapier/Make for real posting.")
        kit_cards = []
        for kit in kits:
            hashing = stats.get(kit["utm_content"]) or 0
            badge = ""
            for p in published:
                if p["utm_content"] == kit["utm_content"] and p["status"] == "published":
                    badge = "<span class='badge' style='background:#e6ffe8;color:#1e8e3e'>published %s</span>" % \
                        seo._clean(p.get("published_at") or "")
                    break
                if p["utm_content"] == kit["utm_content"] and p["status"] == "scheduled":
                    badge = "<span class='badge' style='background:#fff6e0;color:#c77d00'>scheduled %s</span>" % \
                        seo._clean(p.get("scheduled_at") or "")
                    break
            kit_cards.append(f"""<div class="sub soc-kit">
<h3>📣 {seo._clean(kit['platform'])} <span class="who">· {seo._clean(kit['name'])}</span> {badge} <span class="who">· {hashing} click(s) on this post</span></h3>
<textarea readonly rows="5">{seo._clean(kit['body'])}</textarea>
<p class="key" title="Tracked link (UTM) — every share uses this exact URL">{seo._clean(kit['link'])}</p>
<div class="row">
<button class="warm soc-pub" data-kw="{seo._clean(keyword)}" data-platform="{seo._clean(kit['platform'])}">Publish now</button>
<button class="soc-sched" data-kw="{seo._clean(keyword)}" data-platform="{seo._clean(kit['platform'])}">Schedule (24h)</button>
<button class="soc-copy">Copy post</button>
<a class="btn" target="_blank" rel="noopener" href="/social/{seo._clean(slug)}/{seo._clean(kit['utm_content'])}">View live ↗</a>
</div>
</div>""")
        kit_html = "".join(kit_cards) if kit_cards else \
            '<p class="hint">Choose a saved niche with products — each kit turns its top pick into a tracked post.</p>'
        pub_rows = "".join(
            "<tr>%s<td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class='ct'>%s</td>"
            "<td class='ct'><a target='_blank' rel='noopener' href='%s'>open ↗</a></td></tr>"
            % ("<td class='ct'>%s</td>" % ("🥇" if tag == "winner" else ("🔥" if tag == "warm" else "")),
               seo._clean(p["platform"]), seo._clean(p["name"]), seo._clean(p["utm_content"]),
               seo._clean(p["published_at"] or p["created_at"]), clicks,
               "/social/" + seo._clean(p["slug"]) + "/" + seo._clean(p["utm_content"]))
            for clicks, tag, p in self._ranked_posts(published, stats)) or \
            "<tr><td colspan='7' class='hint'>Nothing published yet — hit Publish above.</td></tr>"
        perf_note = ""
        ranked = self._ranked_posts(published, stats)
        if ranked:
            n_won = sum(1 for _, t, _ in ranked if t == "winner")
            n_cold = sum(1 for _, t, _ in ranked if t == "cold")
            perf_note = ("<p class='hint' style='margin-top:6px'><b>Post performance</b>: "
                         "%d winner(s) to replicate · %d cold (0-click) post(s) to rewrite or drop. "
                         "Re-publish winners on a fresh code; rewrite cold copy before re-sharing.</p>"
                         % (n_won, n_cold))
        amp_state = _get_setting("social.amplify")
        amp_on = (amp_state == "" or str(amp_state).strip().lower()
                  in ("1", "on", "true", "yes"))
        amp_note = ("<p class='hint' style='margin-top:6px'><b>Auto-amplify %s</b> — after posts flush and "
                    "earn clicks, the top performers are automatically re-queued to a future peak slot "
                    "(same tracked link, capped runs). %s</p>"
                    % ("<span style='color:#1e8e3e'>ON</span>"
                       if amp_on else "<span style='color:#c5221f'>OFF</span>",
                       "Winners compound while cold posts stay dead."))
        amp_btn = ("<button class='warm' id='ampbtn'>⚡ Amplify winners now</button> "
                   "<button class='soc-sched' id='amptog' data-on='%d'>Turn %s</button>"
                   % (1 if amp_on else 0, "OFF" if amp_on else "ON"))
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Social — pstore</title><link rel="stylesheet" href="/style.css">
<meta name="robots" content="noindex,nofollow">
<style>
.soc-kit textarea {{ width:100%; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12.5px; }}
.soc-kit .key {{ margin:8px 0 0; word-break:break-all; }}
</style>
</head><body>
<header id="top"><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>One-click <span>social publishing.</span></h1>
<p class="tagline">Every post is pre-written, UTM-tracked and points back to the niche landing page — publish here, then watch clicks land in Analytics.</p></div>
{self._admin_nav('social')}
</header>
<main>
<section class="card"><h2>📣 Tracked post kits</h2>
<p class="hint" style="margin-top:-4px">Webhook: {webhook_state}</p>
<form class="row" method="get" action="/admin/social">
  <label>Niche <select name="keyword" onchange="this.form.submit()">{opts}</select></label>
</form>
<div class="cols">{kit_html}</div>
<p id="out" class="msg"></p>
</section>
<section class="card"><h2>✅ Published posts &amp; clicks</h2>
<p class="hint">Each code counts clicks on the landing page with that exact UTM tag — so you can see, per post, who clicked through.</p>
<div class="table-wrap"><table class="plain"><thead><tr><th>Perf</th><th>Platform</th><th>Post</th><th>Code</th><th>Published</th><th>Clicks</th><th>Live</th></tr></thead>
<tbody>{pub_rows}</tbody></table></div>
{perf_note}
<button class="warm" id="flush">Flush due scheduled posts</button>
<button class="warm" id="topics">Recycle long-tail topics → posts</button>
<p id="flushout" class="msg"></p></section>
<section class="card" id="amplify"><h2>🔁 Auto-amplify winners</h2>
{amp_note}
<div class="row">{amp_btn}</div>
<p id="ampout" class="msg"></p></section>
</main>
<footer><p>Posts only use real scraped data (title, price, stars, reviews) — no fabricated claims. Landing page carries the courier beacon, so every tap is attributed.</p></footer>
<script src="/table-flow.js" defer></script>
{_TOTOP}
<script>
function $(id){{return document.getElementById(id);}}
async function pub(btn){{
  $("out").textContent = "Publishing…";
  const r = await fetch("/api/social/publish", {{method:"POST", headers:{{"Content-Type":"application/json"}},
    body: JSON.stringify({{keyword: btn.dataset.kw, platform: btn.dataset.platform}})}});
  const d = await r.json().catch(()=>({{ok:false}}));
  $("out").textContent = d && d.ok
    ? "Published " + d.published + " post(s) for “" + d.keyword + "”. See the table below."
    : (d && d.error) || "Publish failed.";
  setTimeout(()=>location.reload(), 900);
}}
async function sched(btn){{
  $("out").textContent = "Scheduling…";
  const r = await fetch("/api/social/schedule", {{method:"POST", headers:{{"Content-Type":"application/json"}},
    body: JSON.stringify({{keyword: btn.dataset.kw, platform: btn.dataset.platform, hours: 24}})}});
  const d = await r.json().catch(()=>({{ok:false}}));
  $("out").textContent = d && d.ok
    ? "Scheduled " + d.scheduled + " post(s) for “" + d.keyword + "” across the next 24h."
    : (d && d.error) || "Schedule failed.";
  setTimeout(()=>location.reload(), 900);
}}
async function flush(){{
  $("flushout").textContent = "Flushing…";
  const r = await fetch("/api/social/flush", {{method:"POST", headers:{{"Content-Type":"application/json"}}}});
  const d = await r.json().catch(()=>({{ok:false}}));
  $("flushout").textContent = d && d.ok
    ? "Published " + d.published_now + " due post(s) now; " + d.still_pending + " still queued."
    : "Flush failed.";
  setTimeout(()=>location.reload(), 900);
}}
async function recycle(){{
  $("flushout").textContent = "Building topic kits…";
  const r = await fetch("/api/social/topics", {{method:"POST", headers:{{"Content-Type":"application/json"}},
    body: JSON.stringify({{schedule: true, hours: 48}})}});
  const d = await r.json().catch(()=>({{ok:false}}));
  $("flushout").textContent = d && d.ok
    ? "Built kits from " + d.kits_built + " long-tail page(s); " + d.scheduled + " scheduled across 48h (" + d.niches.length + " niches)."
    : "Recycle failed.";
  setTimeout(()=>location.reload(), 1200);
}}
async function ampl(){{
  $("ampout").textContent = "Amplifying winners…";
  const r = await fetch("/api/social/amplify", {{method:"POST", headers:{{"Content-Type":"application/json"}}}});
  const d = await r.json().catch(()=>({{ok:false}}));
  if (d && d.ok){{
    $("ampout").textContent = d.requeued > 0
      ? "Re-queued " + d.requeued + " winner(s) to the next peak slots: " + d.winners.map(w=>w.slug+" ("+w.platform+" ×"+w.amp+")").join(", ") + "."
      : "No eligible winners to amplify yet (post needs clicks and to be older than the min age).";
  }} else $("ampout").textContent = "Amplify failed.";
}}
async function amptog(){{
  const r = await fetch("/api/social/amplify", {{method:"POST", headers:{{"Content-Type":"application/json"}},
    body: JSON.stringify({{enable: document.querySelector("#amptog").dataset.on !== "1"}})}});
  const d = await r.json().catch(()=>({{ok:false}}));
  $("ampout").textContent = d && d.ok ? ("Auto-amplify is now " + (d.on ? "ON" : "OFF") + ".") : "Toggle failed.";
  setTimeout(()=>location.reload(), 900);
}}
document.addEventListener("click", (e)=>{{
  const p = e.target.closest(".soc-pub");
  if (p){{ pub(p); return; }}
  const s = e.target.closest(".soc-sched");
  if (s){{ sched(s); return; }}
  if (e.target.closest("#flush")){{ flush(); return; }}
  if (e.target.closest("#topics")){{ recycle(); return; }}
  if (e.target.closest("#ampbtn")){{ ampl(); return; }}
  if (e.target.closest("#amptog")){{ amptog(); return; }}
  const c = e.target.closest(".soc-copy");
  if (c){{
    const box = c.closest(".soc-kit");
    const body = box.querySelector("textarea").value;
    const link = box.querySelector(".key").textContent.trim();
    const text = body + "\\n\\n" + link;
    navigator.clipboard ? navigator.clipboard.writeText(text).then(()=>{{const t=c.textContent;c.textContent="Copied ✓";setTimeout(()=>c.textContent=t,1200);}}) : (document.execCommand("copy"), c.textContent="Copied ✓");
  }}
}});
</script>
</body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")


# ------------------------------------------------------------------ SEM / SEO suite
    def _seo_audit_payload(self):
        """Refresh the audit off the current saved niches (no network)."""
        return seo.audit_sites(self._all_niches())

    def _admin_seo(self):
        """SEO audit hub: indexability of every saved niche + site-level config."""
        audit = self._seo_audit_payload()
        with _lock:
            _conn = _db()
            try:
                active_subs = _conn.execute(
                    "SELECT COUNT(*) c FROM subscribers WHERE confirmed=1 AND unsubscribed=0"
                ).fetchone()["c"]
            finally:
                _conn.close()
        strip = (
            '<div class="feature"><h3>%d</h3><p class="hint">niches saved</p></div>'
            '<div class="feature"><h3>%d</h3><p class="hint">fully indexable</p></div>'
            '<div class="feature"><h3>%d</h3><p class="hint">need work</p></div>'
            '<div class="feature"><h3>%d</h3><p class="hint">active subscribers</p></div>'
            '<div class="feature"><h3>%s</h3><p class="hint">Search Console token</p></div>'
            % (audit["count"], audit["indexable"], audit["needs_work"],
               active_subs,
               "✓ set" if audit["google_verification"] else "—"))
        rows = ""
        for r in audit["niches"]:
            c = r["checks"]
            def mark(ok):
                return ('<span class="badge" style="background:#e6ffe8;color:#1e8e3e">ok</span>'
                        if ok else '<span class="badge" style="background:#ffe6e6;color:#c0392b">fix</span>')
            rows += (
                "<tr class='%s'>"
                "<td class='ct'><a href='%s'>%s</a><br>"
                "<a class='snippet-open' href='#snippet' data-snip='/seo/snippet/%s' "
                "title='Preview the Google snippet'>preview snippet ↗</a></td>"
                "<td>%d</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                % ("top" if r["indexable"] else "",
                   seo._clean(r["url"]), seo._clean(r["keyword"]), seo._clean(r["slug"]),
                   r["products"],
                   mark(c.get("title_ok", False)), mark(c.get("desc_ok", False)),
                   mark(c.get("schema", False)), mark(c.get("og_image", False)),
                   mark(c.get("word_count", False)),
                   ("indexable" if r["indexable"] else "noindex")))
        if not rows:
            rows = "<tr><td colspan='8' class='hint'>No saved niches yet — mine one on the dashboard.</td></tr>"
        gsc_state = ('<span style="color:#1e8e3e">Configured</span> — the site emits your google-site-verification meta.' if audit["google_verification"]
                     else '<span style="color:#c0392b">Not set</span> — prove Search Console ownership to get the site indexed.')
        site_keys = ('<div class="row" style="align-items:stretch;margin-top:10px">'
                     '<div class="feature"><h3>✓</h3><p class="hint">Search Console token<br>'
                     '<a href="/keys/site/gsc">/keys/site/gsc ↗</a></p></div>'
                     '<div class="feature"><h3>✓</h3><p class="hint">IndexNow key<br>'
                     '<a href="/keys/site/indexnow">/keys/site/indexnow ↗</a></p></div>'
                     '<div class="feature"><h3>✓</h3><p class="hint">Sitemap submit<br>'
                     '<a href="/keys">all keys hub ↗</a></p></div>'
                     '<div class="feature"><h3>🔎</h3><p class="hint">Console dashboards<br>'
                     '<a href="/admin/seoengines">live per-engine hub ↗</a></p></div></div>')
        engine_link = lambda name, url: (
            '<tr><td>%s</td><td class="key ct"><a href="%s" target="_blank" rel="noopener">%s ↗</a></td>'
            '<td class="key ct" onclick="navigator.clipboard&&navigator.clipboard.writeText(this.textContent)" '
            'title="click to copy">%s</td></tr>'
            % (name, seo._clean(url), name, seo._clean(audit['sitemap'])))
        engines = ("".join([
            engine_link("Google Search Console",
                        "https://search.google.com/search-console?resource_id=" + urllib.parse.quote(audit['site_url'], safe="")
                        + "&hl=en"),
            engine_link("Bing / IndexNow", "https://www.bing.com/indexnow"),
            engine_link("Yandex Webmaster", "https://webmaster.yandex.com/"),
            engine_link("Yahoo (via Bing)", "https://www.bing.com/webmasters"),
        ]))
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>SEO audit — pstore</title><link rel="stylesheet" href="/style.css">
<meta name="robots" content="noindex,nofollow">
<style>.audit-note{{max-width:720px}}</style>
</head><body>
<header id="top"><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>SEO <span>audit.</span></h1>
<p class="tagline">Every saved niche, checked against the same rules its live page is served with — title/description length, structured data, share image and indexability.</p></div>
{self._admin_nav('seo')}
</header>
<main>
<section class="card"><h2>🔍 Site health</h2>
<div class="row" style="align-items:stretch">{strip}</div>
<p class="hint" style="margin-top:10px">Search Console owner token: {gsc_state}</p>
<p class="hint" style="margin-top:6px">Sitemap <a href="{seo._clean(audit['sitemap'])}">{seo._clean(audit['sitemap'])}</a> · Robots <a href="{seo._clean(audit['robots'])}">{seo._clean(audit['robots'])}</a> · Canonical base <code>{seo._clean(audit['site_url'])}</code></p>
<p class="hint" style="margin-top:4px">Locked to team members granted the <b>SEO &amp; consoles</b> function — the owner hands it out under <a href="/admin/users">Users &amp; roles</a>.</p>
{site_keys}
<div class="sub"><h3>🚀 Push to the engines now</h3>
<p class="hint">Ping IndexNow with every live URL ({audit['count']} niches → {len(self._all_urls())} URLs). Bing, Yandex, Naver & Seznam crawl in minutes. To also reach Google, verify ownership via <a href="/keys/site/gsc">/keys/site/gsc</a> and submit the sitemap in Search Console.</p>
<button id="submitNow" class="warm">▶ Submit all URLS to IndexNow</button>
<p id="inmsg" class="msg"></p>
<div class="table-wrap"><table class="plain"><thead><tr><th>Engine</th><th>Console / submit</th><th>Sitemap to submit (click to copy)</th></tr></thead><tbody>{engines}</tbody></table></div></div>
</section>
<section class="card"><h2>📄 Per-niche checks</h2>
<p class="hint" style="margin-top:-4px">Green = passes the live-page rule. Red = the page is served with that gap today. Titles 30–60 chars, descriptions 70–160.</p>
<div class="table-wrap"><table class="plain"><thead><tr>
<th>Niche</th><th>Products</th><th>Title</th><th>Desc</th><th>Schema</th><th>Share img</th><th>Word count</th><th>Status</th>
</tr></thead><tbody>{rows}</tbody></table></div>
<div id="snippet" class="card" style="display:none;margin-top:12px">
<h3>🔎 Snippet preview</h3>
<p class="hint">What Google is likely to show for this page (title ≤ 60 · description ≤ 160).</p>
<img id="snipimg" alt="SERP snippet preview" style="max-width:100%;border:1px solid var(--border,#ddd);border-radius:8px"></div></section>
</main>
<footer><p>Audit reflects the live pages, not aspirational settings. Set the Search Console and IndexNow keys under <a href="/keys">Keys ↗</a> — pick <code>PSTORE_GOOGLE_SITE_VERIFICATION</code> env or save it there for the running process.</p></footer>
<script>
const $=s=>document.getElementById(s);
if ($("submitNow")) $("submitNow").onclick = async () => {{
  const m = $("inmsg"); m.textContent = "Submitting all URLs…"; m.className = "msg";
  let d, r;
  try {{ r = await fetch("/api/indexnow", {{method:"POST", headers:{{"Content-Type":"application/json"}}, body:"{{}}"}}); d = await r.json(); }}
  catch(e) {{ m.textContent = "✗ Could not reach the server."; return; }}
  m.textContent = (d.ok ? "✓ IndexNow " : "✗ ") + (d.message || "");
  m.className = d.ok ? "msg" : "msg";
}};
document.addEventListener("click", (e) => {{
  const a = e.target.closest(".snippet-open");
  if (!a) return;
  e.preventDefault();
  const box = $("snippet"); box.style.display = "block";
  a.href !== a.dataset.snip ? $("snipimg").src = a.dataset.snip : null;
  box.scrollIntoView({{behavior:"smooth"}});
}});
 </script>
<script src="/table-flow.js" defer></script>
{_TOTOP}
</body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _admin_manual(self):
        """In-app user manual: a visual, fully cross-linked guide to every tool,
        page and feature, with an optional printable PDF download."""
        body = manual.render_admin_manual(self._admin_nav("manual"), _TOTOP)
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _admin_manual_pdf(self):
        """Serve the styled, printable PDF companion to the user manual."""
        data = manual.build_pdf()
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", 'attachment; filename="pstore-user-manual.pdf"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
        return None

    # ------------------------------------------------------- users & roles admin
    def _admin_users(self):
        """Owner-only team manager: create users, assign roles (a user can hold
        several), toggle status, reset passwords, and edit the role matrix."""
        nav = self._admin_nav("users")
        if not self._authed() or self._session_uid() is not None:
            return self._function_error("/admin/users")
        fn_rows = "".join(
            '{"%s":"%s"}' % (slug, seo._clean(label))
            for slug, label in FUNCTIONS)
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Users &amp; roles — pstore</title><link rel="stylesheet" href="/style.css">
<style>
.ur-grid{{display:grid;grid-template-columns:1fr;gap:16px;margin-top:6px}}
.role-check{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:6px;margin:10px 0}}
.role-check label{{font-size:13px;display:flex;gap:7px;align-items:center;background:#fff;border:1px solid var(--border);
border-radius:10px;padding:7px 10px;cursor:pointer}}
.user-row{{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(0,1fr);gap:14px;align-items:start;border-bottom:1px solid var(--border);
padding:14px 0}}
.user-row:last-child{{border-bottom:none}}
.status-pill{{font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;background:#f1f3f6;color:var(--muted)}}
.status-pill.verified{{background:#e6f4ea;color:#1e7e34}}
.status-pill.disabled{{background:#fdecea;color:#c62828}}
.actions{{display:flex;gap:8px;flex-wrap:wrap}}
button.mini{{font-size:12.5px;padding:7px 12px;border-radius:10px;border:1px solid var(--border);background:#fff;cursor:pointer;color:var(--text)}}
button.mini.danger{{color:#c62828}}
input[type=text],input[type=email],input[type=password]{{width:100%;padding:10px 12px;border:1px solid var(--border);border-radius:10px;font-size:14px;margin:4px 0 10px;background:#fff}}
.addform{{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:10px;align-items:end}}
.addform label{{font-size:12px;color:var(--muted);font-weight:700}}
@media(max-width:760px){{.addform{{grid-template-columns:1fr}}}}
.role-title{{display:flex;justify-content:space-between;align-items:center;gap:8px}}
</style></head><body>
<header id="top"><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>Users &amp; <span>roles</span></h1>
<p class="tagline">Self-registered colleagues confirm their email, then you grant tools by ticking roles. A user can hold several roles; each role is a custom matrix of functions.</p></div>
{nav}
</header>
<main>
<section class="card"><h2>➕ Invite a user</h2>
<div class="addform">
<label>Name<input id="n_name" type="text" placeholder="Jane Doe" autocomplete="off"></label>
<label>Email<input id="n_email" type="email" placeholder="jane@example.com" autocomplete="off"></label>
<label>Temp password <input id="n_pw" type="text" placeholder="8+ chars" autocomplete="off"></label>
<button id="n_go" class="btn" style="width:100%">Create user</button>
</div>
<p id="n_msg" class="msg"></p></section>
<section class="card"><h2>👥 Team</h2><div id="users"></div></section>
<section class="card"><h2>🎛 Role matrix <span class="status-pill">functions per role</span></h2>
<div id="roles"></div></section>
</main>
<footer><p>{_TOTOP}</p></footer>
<script>
var FUNCTIONS = {{{fn_rows}}};
function $(id){{return document.getElementById(id);}}
function el(tag, cls, html){{var e=document.createElement(tag); if(cls)e.className=cls; if(html!=null)e.innerHTML=html; return e;}}
function api(method, body, cb){{fetch("/api/users", {{method:method, headers:{{"Content-Type":"application/json"}},
  body: JSON.stringify(body||{{}})}}).then(r=>r.json()).then(d=>{{ if(d.error && !cb) alert(d.error); cb&&cb(d); }});}}
function bake(roleSlugs){{return roleSlugs.filter(s=>s).join(", ") || "— none —";}}
function render(data){{
  var u=$("users"); u.innerHTML="";
  data.users.forEach(usr=>{{
    var div=el("div","user-row");
    var left=el("div","", "<b>"+(usr.name||"—")+"</b><div style='font-size:13px;color:var(--muted)'>"+usr.email+"</div>"+
      "<div style='margin-top:6px'><span class='status-pill "+usr.status+"'>"+usr.status+"</span>"+
      " <span style='font-size:12px;color:var(--muted)'>signups "+usr.created+"</span></div>");
    var roleChips = data.roles.map(r=>{{
      var c=document.createElement("label");
      var cb=document.createElement("input"); cb.type="checkbox"; cb.checked=usr.roles.indexOf(r.slug)>=0;
      cb.onchange=function(){{
        var cur=usr.roles.indexOf(r.slug)>=0 ? usr.roles.filter(s=>s!==r.slug) : usr.roles.concat([r.slug]);
        api("POST", {{action:"set_roles", id:usr.id, roles:cur}}, d=>render(d.data));
      }};
      c.appendChild(cb); c.appendChild(document.createTextNode(r.label)); return c;
    }});
    var mid=el("div","role-check"); roleChips.forEach(c=>mid.appendChild(c));
    var acts=el("div","actions",
      "<button class='mini' data-a='status'>"+(usr.status==="disabled"?"Enable":"Disable")+"</button>"+
      "<button class='mini' data-a='reset'>Reset password</button>"+
      "<button class='mini' data-a='delete'>Delete</button>");
    acts.onclick=function(e){{
      var b=e.target; if(!b.dataset.a)return;
      if(b.dataset.a==="status") api("POST", {{action:"set_status", id:usr.id,
        status: usr.status==="disabled" ? "verified" : "disabled"}}, d=>render(d.data));
      if(b.dataset.a==="reset"){{ var pw=prompt("New password (8+ chars) for "+usr.email); if(pw&&pw.length>=8) api("POST", {{action:"reset_password", id:usr.id, password:pw}}, d=>render(d.data)); }}
      if(b.dataset.a==="delete"){{ if(confirm("Delete "+usr.email+"? Their sessions end immediately.")) api("POST", {{action:"delete_user", id:usr.id}}, d=>render(d.data)); }}
    }};
    div.appendChild(left); div.appendChild(mid);
    var right=el("div","", "");
    right.appendChild(acts); div.appendChild(right);
    u.appendChild(div);
  }});
  if(!data.users.length) u.appendChild(el("p","login-hint","No team members yet — ask them to request access from the login page, or invite one above."));
  var r=$("roles"); r.innerHTML="";
  data.roles.forEach(role=>{{
    var card=el("div","card", undefined); card.style.marginBottom="12px";
    var title=el("div","role-title","<b>"+role.label+"</b>"+
      (role.builtin?"<span class='status-pill'>built-in</span>":"<button class='mini danger' data-del='"+role.slug+"'>Delete role</button>"));
    var checks=el("div","role-check");
    Object.keys(FUNCTIONS).forEach(slug=>{{
      var l=document.createElement("label"); var cb=document.createElement("input");
      cb.type="checkbox"; cb.checked=role.functions.indexOf(slug)>=0;
      cb.onchange=function(){{
        var cur=cb.checked ? role.functions.concat([slug]) : role.functions.filter(s=>s!==slug);
        api("POST", {{action:"save_role", slug:role.slug, label:role.label, functions:cur}}, d=>render(d.data));
      }};
      l.appendChild(cb); l.appendChild(document.createTextNode(FUNCTIONS[slug])); checks.appendChild(l);
    }});
    title.querySelector("[data-del]").onclick=function(){{
      if(confirm("Delete role "+role.label+"? Users lose its functions.")) api("POST", {{action:"delete_role", slug:role.slug}}, d=>render(d.data));
    }};
    card.appendChild(title); card.appendChild(checks); r.appendChild(card);
  }});
  var add=el("div","card", undefined);
  var lab=el("label"); lab.innerHTML="New role name <input id='nr_label' type='text' placeholder='e.g. Writer' autocomplete='off'>";
  var addBtn=el("button","mini","Add role"); addBtn.style.marginTop="8px";
  addBtn.onclick=function(){{
    var label=$("nr_label").value.trim(); if(!label)return;
    var slug=label.toLowerCase().replace(/[^a-z0-9]+/g,"-").replace(/^-+|-+$/g,"")||("role-"+Date.now());
    api("POST", {{action:"save_role", slug:slug, label:label, functions:[]}}, d=>{{
      $("nr_label").value=""; render(d.data);
    }});
  }};
  lab.appendChild(addBtn); add.appendChild(lab); r.appendChild(add);
}}
$("n_go").onclick=function(){{
  var name=$("n_name").value.trim(), email=$("n_email").value.trim(), pw=$("n_pw").value;
  if(email.indexOf("@")<0){{ $("n_msg").textContent="Enter a valid email."; return; }}
  if(pw.length<8){{ $("n_msg").textContent="Password must be 8+ characters."; return; }}
  api("POST", {{action:"create_user", name:name, email:email, password:pw}}, d=>{{
    $("n_msg").textContent=d.error||"Created "+email+" — assign a role to grant access.";
    if(!d.error){{ $("n_name").value=""; $("n_email").value=""; $("n_pw").value=""; render(d.data); }}
  }});
}};
fetch("/api/users").then(r=>r.json()).then(render);
</script>
</body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _users_json(self):
        """{users, roles, functions} — never leaks stored password hashes."""
        with _lock:
            conn = _db()
            try:
                rows = conn.execute(
                    "SELECT id, email, name, status, roles, created_at, last_login_at "
                    "FROM users ORDER BY id").fetchall()
                roles = conn.execute(
                    "SELECT slug, label, functions, builtin FROM roles "
                    "ORDER BY builtin DESC, slug").fetchall()
            finally:
                conn.close()
        users = []
        for r in rows:
            try:
                rl = json.loads(r["roles"] or "[]")
            except ValueError:
                rl = []
            users.append({
                "id": r["id"], "email": r["email"], "name": r["name"],
                "status": r["status"], "roles": [x for x in rl if x],
                "created": (r["created_at"] or "")[:10],
                "last": (r["last_login_at"] or "")[:16] or ""})
        return {"users": users,
                "roles": [{"slug": r["slug"], "label": r["label"],
                           "functions": [f for f in json.loads(r["functions"] or "[]") if f in FN],
                           "builtin": bool(r["builtin"])} for r in roles],
                "functions": [{"slug": s, "label": l} for s, l in FUNCTIONS]}

    def _data_out(self, action, err=None, status=200):
        data = json.dumps({"error": err, "data": self._users_json()}).encode("utf-8")
        return self._send(status, data, "application/json; charset=utf-8")

    def _owners_only(self):
        return bool(self._authed() and self._session_uid() is None)

    def _users_api(self):
        """Owner-only CRUD for users + roles (JSON)."""
        if not self._owners_only():
            return self._send(403, {"error": "owner only"})
        if self.command == "POST":
            return self._users_api_post()
        return self._send(200, json.dumps(self._users_json()).encode("utf-8"),
                          "application/json; charset=utf-8")

    def _users_api_post(self):
        body = self._body()
        action = str(body.get("action") or "")
        err = None
        status = 200
        with _lock:
            conn = _db()
            try:
                if action == "create_user":
                    name = str(body.get("name") or "").strip()[:120]
                    email = str(body.get("email") or "").strip().lower()
                    pw = str(body.get("password") or "")
                    if not email or "@" not in email or "." not in email.split("@")[-1]:
                        err, status = "Enter a valid email address", 400
                    elif len(pw) < 8:
                        err, status = "Password must be at least 8 characters", 400
                    elif conn.execute("SELECT id FROM users WHERE email=?",
                                      (email,)).fetchone():
                        err, status = "That email already exists", 409
                    else:
                        conn.execute(
                            "INSERT INTO users (email, name, pass_hash, status, roles) "
                            "VALUES (?,?,?,'verified','[]')",
                            (email, name, security.hash_password(pw)))
                        conn.commit()
                elif action == "set_roles":
                    try:
                        uid = int(body.get("id") or 0)
                    except (TypeError, ValueError):
                        uid = 0
                    slugs = [str(s) for s in (body.get("roles") or [])]
                    known = {r["slug"] for r in conn.execute(
                        "SELECT slug FROM roles").fetchall()}
                    if not uid or any(s not in known for s in slugs):
                        err, status = "unknown role slug", 400
                    else:
                        conn.execute("UPDATE users SET roles=? WHERE id=?",
                                     (json.dumps(slugs), uid))
                        conn.commit()
                elif action == "set_status":
                    try:
                        uid = int(body.get("id") or 0)
                    except (TypeError, ValueError):
                        uid = 0
                    stval = str(body.get("status") or "")
                    if stval not in ("unverified", "verified", "disabled"):
                        err, status = "bad status", 400
                    else:
                        conn.execute("UPDATE users SET status=? WHERE id=?",
                                     (stval, uid))
                        conn.commit()
                        self._drop_sessions_locked(uid)
                elif action == "reset_password":
                    try:
                        uid = int(body.get("id") or 0)
                    except (TypeError, ValueError):
                        uid = 0
                    pw = str(body.get("password") or "")
                    if len(pw) < 8:
                        err, status = "Password must be at least 8 characters", 400
                    else:
                        conn.execute("UPDATE users SET pass_hash=? WHERE id=?",
                                     (security.hash_password(pw), uid))
                        conn.commit()
                        self._drop_sessions_locked(uid)
                elif action == "delete_user":
                    try:
                        uid = int(body.get("id") or 0)
                    except (TypeError, ValueError):
                        uid = 0
                    conn.execute("DELETE FROM users WHERE id=?", (uid,))
                    conn.commit()
                    self._drop_sessions_locked(uid)
                elif action == "save_role":
                    slug = str(body.get("slug") or "").strip()[:64]
                    label = str(body.get("label") or "").strip()[:80]
                    funcs = [str(f) for f in (body.get("functions") or [])
                             if str(f) in FN]
                    if not slug or not label:
                        err, status = "role slug and label required", 400
                    elif conn.execute("SELECT slug FROM roles WHERE slug=?",
                                      (slug,)).fetchone():
                        conn.execute("UPDATE roles SET label=?, functions=? WHERE slug=?",
                                     (label, json.dumps(funcs), slug))
                        conn.commit()
                    else:
                        conn.execute(
                            "INSERT INTO roles (slug, label, functions, builtin) "
                            "VALUES (?,?,?,0)", (slug, label, json.dumps(funcs)))
                        conn.commit()
                elif action == "delete_role":
                    slug = str(body.get("slug") or "").strip()
                    role = conn.execute("SELECT builtin FROM roles WHERE slug=?",
                                        (slug,)).fetchone()
                    if not role:
                        err, status = "no such role", 404
                    elif role["builtin"]:
                        err, status = "built-in roles can't be deleted", 400
                    else:
                        conn.execute("DELETE FROM roles WHERE slug=?", (slug,))
                        for row in conn.execute("SELECT id, roles FROM users").fetchall():
                            try:
                                rl = [x for x in json.loads(row["roles"] or "[]")
                                      if x != slug]
                            except ValueError:
                                rl = []
                            conn.execute("UPDATE users SET roles=? WHERE id=?",
                                         (json.dumps(rl), row["id"]))
                        conn.commit()
                else:
                    err, status = "unknown action", 400
            finally:
                conn.close()
        return self._data_out(action, err, status)

    def _drop_sessions_locked(self, uid):
        """Drop every session bound to a user; assumes `_lock` is HELD."""
        for tok, (exp, sid) in list(_SESSIONS.items()):
            if sid == uid:
                _SESSIONS.pop(tok, None)

    def _drop_sessions(self, uid):
        with _lock:
            self._drop_sessions_locked(uid)

    def _admin_refresh(self, q):
        """Manual niche data refresh: re-mine one or all saved niches now, and
        show the auto-refresh schedule so prices/ratings/stock stay current."""
        with _lock:
            conn = _db()
            rows = conn.execute(
                "SELECT keyword, products, updated_at FROM niches "
                "ORDER BY CASE WHEN updated_at IS NULL THEN 0 ELSE 1 END, updated_at ASC").fetchall()
            conn.close()
        now = time.time()
        rows_html = ""
        for r in rows:
            kw = r["keyword"]
            prods = json.loads(r["products"] or "[]")
            updated = r["updated_at"]
            if not updated:
                when = '<span class="badge" style="background:#ffe9e9;color:#c0392b">never refreshed</span>'
                stale = True
            else:
                try:
                    parsed = time.mktime(time.strptime(updated[:19], "%Y-%m-%d %H:%M:%S"))
                    age_min = int((now - parsed) // 60)
                except (ValueError, TypeError):
                    age_min = 0
                stale = age_min >= _REFRESH_STALE_MIN
                when = ("%s · <b>%d min ago</b>" % (updated, age_min))
                if stale:
                    when += ' <span class="badge" style="background:#ffe9e9;color:#c0392b">stale</span>'
                else:
                    when += ' <span class="badge" style="background:#e6ffe8;color:#1e8e3e">fresh</span>'
            rows_html += (
                "<tr class='%s'><td class='ct'>%s</td><td>%d</td><td>%s</td>"
                "<td><button class='mini' data-kw=\"%s\">Refresh now</button></td></tr>"
                % ("top" if stale else "", seo._clean(kw), len(prods), when,
                   seo._clean(kw)))
        if not rows_html:
            rows_html = "<tr><td colspan='4' class='hint'>No saved niches yet — mine one on the dashboard.</td></tr>"
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Data refresh — pstore</title><link rel="stylesheet" href="/style.css">
<meta name="robots" content="noindex,nofollow">
</head><body>
<header id="top"><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>Niche data <span>refresh.</span></h1>
<p class="tagline">Re-mine saved niches so prices, ratings and stock reflect current Amazon listings. Manual here, plus an automatic loop runs on a schedule.</p></div>
{self._admin_nav('refresh')}
</header>
<main>
<section class="card"><h2>🔄 Schedule &amp; status</h2>
<div class="row" style="align-items:stretch">
<div class="feature"><h3>{_REFRESH_INTERVAL_SEC}s</h3><p class="hint">auto-loop interval (0 = off)</p></div>
<div class="feature"><h3>{_REFRESH_STALE_MIN}m</h3><p class="hint">staleness window</p></div>
<div class="feature"><h3>{_REFRESH_MAX_PER_CYCLE}</h3><p class="hint">niches per cycle</p></div>
</div>
<p id="status" class="hint" style="margin-top:8px">Fetching status…</p>
</section>
<section class="card"><h2>🛍 Saved niches</h2>
<div class="row" style="margin-bottom:8px">
<button id="refresh-all" class="warm">Refresh all now</button>
</div>
<p id="out" class="msg"></p>
<div class="table-wrap"><table class="plain"><thead><tr>
<th>Niche</th><th>Products</th><th>Last refreshed</th><th></th>
</tr></thead><tbody>{rows_html}</tbody></table></div></section>
</main>
<footer><p>Each refresh re-runs Amazon autosuggest + product search for that niche. Prices move — refreshing keeps the live prices honest on every page.</p></footer>
<script src="/table-flow.js" defer></script>
{_TOTOP}
<script>
function $(id){{return document.getElementById(id);}}
async function fresh() {{
  try {{
    const r = await fetch("/api/refresh/status");
    const d = await r.json();
    $("status").textContent = d.total + " niches · " + d.refreshed + " refreshed · " + d.stale + " stale · in-flight: " + (d.inflight.length ? d.inflight.join(", ") : "none");
  }} catch(e) {{ $("status").textContent = "Status unavailable."; }}
}}
async function run(path, body, out) {{
  out.textContent = "Working…";
  try {{
    const r = await fetch(path, {{method:"POST", headers:{{"Content-Type":"application/json"}}, body: JSON.stringify(body)}});
    const d = await r.json();
    out.textContent = (d.status === "started")
      ? "Queued " + (d.queued||0) + " niche(s) — refreshing in the background. Watch the status line / table update."
      : ((d.status === "ok") ? "Refreshed: " + d.keyword + " (" + d.products + " products) — new score " + d.score + "."
                              : ("Failed: " + (d.error || d.status || "unknown")));
  }} catch(e) {{ out.textContent = "Request failed."; }}
  fresh(); setTimeout(()=>location.reload(), 1200);
}}
$("refresh-all").onclick = () => {{ run("/api/refresh-all", {{}}, $("out")); }};
document.querySelectorAll("button.mini[data-kw]").forEach(b => {{
  b.onclick = () => {{ run("/api/refresh", {{keyword: b.dataset.kw}}, $("out")); }};
}});
fresh();
</script>
</body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _seo_audit_api(self):
        return self._send(200, self._seo_audit_payload())

    def _seo_topics_payload(self):
        """Audit rows for every long-tail topic page (/n/<parent>/<term>)."""
        rows = []
        for n in self._all_niches():
            prods = n["products"] or []
            parent_slug = seo._slugify(n["keyword"])
            for t in self._topics_for(parent_slug):
                rows.append(seo.audit_topic(t["term"], n["keyword"], prods))
        indexable = sum(1 for r in rows if r["indexable"])
        return {"topics": rows, "count": len(rows), "indexable": indexable,
                "needs_work": len(rows) - indexable}

    def _seo_topics_api(self):
        return self._send(200, self._seo_topics_payload())

    def _seo_snippet(self, slug):
        """Google-style SERP snippet preview for a niche (SVG, cacheable)."""
        slug = slug.rstrip("/")
        for n in self._all_niches():
            if seo._slugify(n["keyword"]) == slug:
                svg = seo.render_snippet(n["keyword"], url="/n/" + slug)
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                try:
                    self.wfile.write(svg)
                except Exception:
                    pass
                return None
        return self._send(404, b"<html><body><p>Niche not found.</p></body></html>",
                          "text/html; charset=utf-8")

    def _marketing_payload(self):
        """Unified digital-marketing ROI across email, social and organic
        traffic. Pure aggregation off the same tables the other tools feed:
        clicks (all attribution), email_events (opens/clicks), sent_emails,
        events (pageviews), social_posts. Offline + honest — no fabricated
        metrics."""
        with _lock:
            conn = _db()
            def q1(sql, args=()):
                return conn.execute(sql, args).fetchone()[0] or 0
            confirmed = q1("SELECT COUNT(*) FROM subscribers WHERE confirmed=1 AND unsubscribed=0")
            unsubbed = q1("SELECT COUNT(*) FROM subscribers WHERE unsubscribed=1")
            sent_emails = q1("SELECT COUNT(*) FROM sent_emails")
            opens = q1("SELECT COUNT(*) FROM email_events WHERE type='open'")
            email_clicks = q1("SELECT COUNT(*) FROM clicks WHERE source='email'")
            seq_done = q1("SELECT COUNT(*) FROM subscribers WHERE sent_index>=?",
                          (mailer.SEQUENCE_LENGTH,))
            social_pub = q1("SELECT COUNT(*) FROM social_posts WHERE status='published'")
            social_sched = q1("SELECT COUNT(*) FROM social_posts WHERE status='scheduled'")
            social_clicks = q1("SELECT COUNT(*) FROM clicks WHERE source='social'")
            views = q1("SELECT COUNT(*) FROM events WHERE name='view'")
            promo = q1("SELECT COUNT(*) FROM events WHERE name!='view'")
            niches = q1("SELECT COUNT(*) FROM niches")
            topics = q1("SELECT COUNT(*) FROM topics")
            # top landing pages by clicks (cross-channel attribution)
            top_pages = conn.execute(
                "SELECT slug, COUNT(*) c FROM clicks GROUP BY slug "
                "ORDER BY c DESC LIMIT 5").fetchall()
            # top email open actions
            top_open_niches = conn.execute(
                "SELECT keyword, COUNT(*) c FROM email_events WHERE type='open' "
                "GROUP BY keyword ORDER BY c DESC LIMIT 5").fetchall()
            # winners: published social posts >0 clicks
            soc_rows = conn.execute(
                "SELECT utm_content, platform, name FROM social_posts "
                "WHERE status='published' AND utm_content!=''").fetchall()
            conn.close()
        click_stats = {}
        if soc_rows:
            with _lock:
                c2 = _db()
                for r in soc_rows:
                    click_stats[(r["utm_content"])] = c2.execute(
                        "SELECT COUNT(*) FROM clicks WHERE content=? AND source='social'",
                        (r["utm_content"],)).fetchone()[0] or 0
                c2.close()
        winners = [dict(r, clicks=click_stats.get(r["utm_content"], 0))
                   for r in soc_rows if click_stats.get(r["utm_content"], 0) > 0]
        winners.sort(key=lambda r: -r["clicks"])

        email_open_rate = (opens / sent_emails * 100) if sent_emails else 0.0
        email_ctr = (email_clicks / sent_emails * 100) if sent_emails else 0.0

        # Next-best-action recommendations, tied to real state.
        reco = []
        if not niches:
            reco.append("Add a niche — nothing to market yet.")
        elif (winners):
            reco.append("Amplify your top social winner (<b>%s</b>, %d click(s)) into a fresh post code." %
                        (seo._clean(winners[0]["name"] or (winners[0]["utm_content"] or "")),
                         winners[0]["clicks"]))
        if confirmed and confirmed == unsubbed:
            reco.append("No confirmed subscribers — check the email gate on your landing pages.")
        elif confirmed:
            if sent_emails and opens == 0:
                reco.append("Emails are sending but nothing is opening — tighten subject lines / sender name.")
            elif email_clicks == 0 and sent_emails:
                reco.append("Emails open but get no clicks — your CTA link isn't compelling; try the tracked-link format.")
            if seq_done < confirmed:
                reco.append("Sequence still going — %d of %d confirmed subscribers have finished all %d parts." %
                            (seq_done, confirmed, mailer.SEQUENCE_LENGTH))
        if views and topics:
            reco.append("%d long-tail page(s) live — recycle them into social posts via 'Recycle long-tail topics'." % topics)
        # Audience-angle nudge when a demographic profile is configured.
        demo = self._demo()
        interest = (demo.get("interest") or "").strip().lower()
        if interest:
            reco.append("Audience is set to <b>%s</b> — angle every new email, social kit and /n/ headline at that audience so the copy matches who actually buys." % seo._clean(interest))
        if not reco:
            reco.append("Everything looks set — publish social kits + grow topics and subscribers.")

        return {
            "email": {
                "confirmed": confirmed, "unsubscribed": unsubbed,
                "sent": sent_emails, "opens": opens,
                "open_rate": round(email_open_rate, 1),
                "clicks": email_clicks, "click_rate": round(email_ctr, 1),
                "sequence_done": seq_done, "sequence_length": mailer.SEQUENCE_LENGTH,
            },
            "social": {
                "published": social_pub, "scheduled": social_sched,
                "clicks": social_clicks, "winners": winners,
            },
            "traffic": {
                "views": views, "interactions": promo, "pages": [dict(r) for r in top_pages],
                "top_open_niches": [dict(r) for r in top_open_niches],
            },
            "content": {"niches": niches, "topics": topics},
            "demography": demo,
            "recommendations": reco,
        }

    def _marketing_api(self):
        return self._send(200, self._marketing_payload())

    def _suggest_payload(self):
        """Demography-driven auto niche suggestions. Serves the render cache;
        on a miss it triggers a background warm instead of blocking on the
        suggest engine's live searches."""
        if amazon.CACHE_TTL > 0:
            cached = _render_cache_get("sugg:data")
            if cached is not None:
                return cached
            self._kick_admin_warm()
            return {"suggestions": [], "profile": {}, "note": _SUGG_WARMING_NOTE}
        demo = self._demo() or {}
        return self._compute_suggest(demo)

    def _compute_suggest(self, demo):
        """Blocking suggest-engine run, cached as ``sugg:data`` when enabled."""
        payload = suggest.suggest_niches(demo, top=int(
            _get_setting("suggest.top") or 4))
        if amazon.CACHE_TTL > 0:
            _render_cache_put("sugg:data", payload, _ADMIN_SUGG_TTL)
        return payload

    def _kick_admin_warm(self):
        """Ensure at most one background warm of the admin Grow payloads runs at
        a time. Never blocks; used by the read-through cache misses."""
        if amazon.CACHE_TTL <= 0:
            return
        with _refresh_lock:
            if _ADMIN_WARMING:
                return
            _ADMIN_WARMING.add("grow")
        t = threading.Thread(target=self._warm_admin_payloads,
                             name="warm-admin", daemon=True)
        t.start()

    def _warm_admin_payloads(self):
        try:
            self._compute_opportunities()
            self._compute_suggest(self._demo() or {})
        except Exception:
            pass
        finally:
            with _refresh_lock:
                _ADMIN_WARMING.discard("grow")

    def _suggest_api(self):
        return self._send(200, self._suggest_payload())

    def _suggest_build_api(self):
        """One-click build of a suggested niche: mine the keyword through the
        normal pipeline, save the niche (products/score/saturation + IndexNow)
        AND auto-generate its long-tail topic pages — so the new suggestion
        immediately flows into the whole marketing engine (email/social/SEO)."""
        body = self._body()
        kw = str(body.get("keyword") or "").strip()
        if not kw:
            return self._send(400, {"error": "keyword required"})
        with _lock:
            conn = _db()
            existing = conn.execute(
                "SELECT id FROM niches WHERE lower(keyword)=lower(?)", (kw,)).fetchone()
            conn.close()
        if existing:
            # Already mined before — never insert a duplicate row; hand back the
            # live niche (and its page) instead of re-running the pipeline.
            slug = seo._slugify(kw)
            return self._send(200, {
                "ok": True, "already_existed": True,
                "keyword": kw, "id": existing["id"],
                "slug": slug,
                "topic_pages": 0,
                "urls": ["/n/" + slug],
            })
        try:
            res = suggest.build_route(kw)
        except Exception as e:
            return self._send(200, {"ok": False, "error": "mine failed: %s" % str(e)[:200]})
        n = res["niche"] or {}
        products = json.dumps(n.get("products") or [])
        with _lock:
            conn = _db()
            cur = conn.execute(
                "INSERT INTO niches (keyword, market, score, saturation, products) VALUES (?,?,?,?,?)",
                (n.get("keyword") or kw, amazon.MARKET,
                 n.get("score"), n.get("saturation"), products))
            conn.commit()
            nid = cur.lastrowid
            conn.close()
        slug = seo._slugify(n.get("keyword") or kw)
        self._push_indexnow(n.get("keyword") or kw)
        topics_created = []
        try:
            topics_created = self._generate_topics(slug, 6)
        except Exception:
            topics_created = []
        return self._send(200, {
            "ok": True, "keyword": n.get("keyword") or kw,
            "id": nid, "slug": slug,
            "topic_pages": len(topics_created),
            "urls": ["/n/" + slug] + [c["url"] for c in topics_created],
        })

    def _admin_marketing(self):
        """Unified digital-marketing ROI hub: email + social + organic traffic on
        one page, with next-best-action recommendations."""
        p = self._marketing_payload()
        em, so, tr, co, reco = p["email"], p["social"], p["traffic"], p["content"], p["recommendations"]
        def stat(n, label, extra=""):
            return ('<div class="feature"><h3>%s</h3><p class="hint">%s</p>%s</div>'
                    % (seo._clean(n), label, extra))
        email_row = ("<div class='row' style='align-items:stretch'>" +
                     stat(em["confirmed"], "confirmed") +
                     stat(em["sent"], "emails sent") +
                     stat("%.1f%%" % em["open_rate"], "open") +
                     stat("%.1f%%" % em["click_rate"], "email CTR") +
                     stat(em["clicks"], "email clicks") +
                     stat("%d/%d" % (em["sequence_done"], em["confirmed"]), "finished sequence")
                     + "</div>")
        social_row = ("<div class='row' style='align-items:stretch'>" +
                      stat(so["published"], "published posts") +
                      stat(so["scheduled"], "scheduled") +
                      stat(so["clicks"], "social clicks") +
                      stat(len(so["winners"]), "click winners") +
                      "</div>")
        traffic_row = ("<div class='row' style='align-items:stretch'>" +
                       stat(tr["views"], "page views") +
                       stat(tr["interactions"], "on-page interactions") +
                       stat(co["niches"], "niches") +
                       stat(co["topics"], "long-tail pages") +
                       "</div>")
        winners_html = "".join(
            "<tr><td>%s</td><td>%s</td><td class='ct'>%d</td></tr>"
            % (seo._clean(w["platform"]), seo._clean(w["name"] or ""), w["clicks"])
            for w in so["winners"]) or "<tr><td colspan='3' class='hint'>No click-winning posts yet.</td></tr>"
        top_pages = "".join(
            "<tr><td>%s</td><td class='ct'>%d</td></tr>" % (seo._clean(r["slug"]), r["c"])
            for r in tr["pages"]) or "<tr><td colspan='2' class='hint'>No tracked clicks yet.</td></tr>"
        reco_html = "".join("<li>%s</li>" % r for r in reco)
        demo = p["demography"]
        _d = seo._clean
        demo_fields = [
            ("region", "Region", "target market region, e.g. United States / UK", "text"),
            ("interest", "Primary interest", "audience focus, e.g. fashion, fitness, tech", "text"),
            ("interests_extra", "Other interests", "comma-separated secondary interests", "text"),
            ("behavior", "Behavior", "shopping behavior / use case, e.g. budget-first, quality-first, gift", "text"),
            ("age", "Age bracket", "e.g. 18-34, 35-54", "text"),
            ("audience", "Audience", "who it's for, e.g. style-conscious women", "text"),
            ("income", "Income bracket", "e.g. mid-range / premium", "text"),
            ("tone", "Tone", "copy tone, e.g. upbeat / minimal / trustworthy", "text"),
        ]
        demo_rows = "".join(
            '<label>%s<input type="%s" name="%s" value="%s" placeholder="%s"></label>'
            % (_d(f), t, n, _d((demo.get(n) or "")), _d(ph))
            for n, f, ph, t in demo_fields)
        demo_editor = (
            '<section class="card" id="demo"><h2>🌍 Market demography</h2>'
            '<p class="hint">Who + where you market to. This profile is saved to the DB, drives the <a href="#suggest">Auto niche suggestions</a> below, '
            'and is surfaced in every marketing recommendation so new copy (email, social, SEO) is aimed at the right audience.</p>'
            '<form id="demofrm" onsubmit="demoSave();return false;">'
            '<div class="grid">%s</div>'
            '<button class="warm" type="submit">Save audience target</button>'
            '<span id="demomsg" class="msg"></span></form>'
            '<script>async function demoSave(){const o={};\n'
            '["region","interest","interests_extra","behavior","age","audience","income","tone"].forEach('
            'f=>{const el=document.querySelector(`#demofrm input[name="${f}"]`);o[f]=el.value;});\n'
            'const m=document.querySelector("#demomsg");m.textContent="Saving…";\n'
            'let r,d;try{r=await fetch("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({demography:o})});d=await r.json();'
            '}catch(e){m.textContent="✗ Could not reach the server.";return;}\n'
            'm.textContent=(r.ok?"✓ Audience saved":"✗ "+JSON.stringify(d||{}));\n'
            'if(r.ok)setTimeout(()=>location.reload(),700);}\n'
            '</script></section>'
        ) % demo_rows
        suggest_card = (
            '<section class="card" id="suggest"><h2>🤖 Auto niche suggestions</h2>'
            '<p class="hint">Built from your <b>demography + interest + behavior + audience</b> profile: '
            'real Amazon buyer keywords (autosuggest) ranked by demand, persona fit and competition. '
            'One click builds the /n/ page, saves it and pings IndexNow — the niche then flows into your '
            'email, social and SEO pipelines automatically.</p>'
            '<button id="suggbtn" class="warm" onclick="loadSuggest()">⚡ Suggest niches for this audience</button>'
            '<span id="suggmsg" class="msg"></span>'
            '<div id="sugg"></div>'
            '<script>'
            'async function loadSuggest(){const m=document.getElementById("suggmsg"),box=document.getElementById("sugg");'
            'm.textContent="Thinking about your audience…";m.className="msg";'
            'let r,d;'
            'try{r=await fetch("/api/suggest");d=await r.json();}'
            'catch(e){m.textContent="✗ Could not reach the server.";return;}'
            'if(!r.ok){m.textContent="✗ "+JSON.stringify(d||{});return;}'
            'const s=Array.isArray(d.suggestions)?d.suggestions:[];'
            'if(!s.length){box.innerHTML="<div class=table-wrap><table class=plain><tbody><tr><td class=hint>No suggestions yet — save an interest / behavior in the profile above and try again.</td></tr></tbody></table></div>";m.textContent="";return;}'
            'm.textContent="✓ "+s.length+" suggestion(s) for your audience";'
            'box.innerHTML='
            '"<div class=table-wrap><table class=plain><thead><tr><th>Niche suggestion</th><th>Why</th><th>Demand</th><th>Competition</th><th></th></tr></thead><tbody>"+'
            's.map(c=>"<tr><td><b>"+esc(c.keyword)+"</b><br>"+esc(c.count+ " product(s)")+"</td>"'
            '+ "<td>"+esc(c.reason)+"</td>"'
            '+ "<td class=ct>"+esc(c.demand)+"</td>"'
            '+ "<td class=ct>"+(c.saturation==null?"—":esc(c.saturation))+"</td>"'
            '+ "<td><button class=\"mini warm\" onclick=\"buildNiche(this,\'"'
            '+esc(c.keyword)+'
            '\')\">▶ Build</button></td></tr>").join("")+"</tbody></table></div>";'
            'm.className="msg";}'
            'function esc(x){return String(x).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;"}[c]||c));}'
            'async function buildNiche(btn,kw){btn.disabled=true;let msg=btn.nextElementSibling;'
            'const stMsg=document.createElement("span");stMsg.className="msg";btn.parentNode.appendChild(stMsg);'
            'let r;try{r=await fetch("/api/suggest/build",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({"keyword":kw})});let d=await r.json();'
            'stMsg.textContent=(r.ok?("✓ Built "+esc(d.keyword)+" #"+d.id):"✗ "+JSON.stringify(d||{}));}'
            'catch(e){stMsg.textContent="✗ error";}'
            'btn.disabled=false;}'
            '</script></section>'
        )
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Marketing ROI — pstore</title><link rel="stylesheet" href="/style.css">
<meta name="robots" content="noindex,nofollow"></head><body>
<header id="top"><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>Digital marketing <span>ROI.</span></h1>
<p class="tagline">Email, social and organic traffic consolidated on one page — every number comes from your own attribution (opened emails, tracked clicks, pageviews). No phantom metrics.</p></div>
{self._admin_nav('marketing')}
</header>
<main>
<section class="card"><h2>📧 Email engine</h2>{email_row}
<p class="hint">Opens come from the open-pixel; clicks from tracked /e/ outbound links. Sequence length: {em["sequence_length"]}.</p></section>
<section class="card"><h2>📣 Social publishing</h2>{social_row}
<div class="table-wrap"><table class="plain"><thead><tr><th>Platform</th><th>Winner post</th><th>Clicks</th></tr></thead><tbody>{winners_html}</tbody></table></div></section>
<section class="card"><h2>🌐 Traffic &amp; content</h2>{traffic_row}
<div class="table-wrap"><table class="plain"><thead><tr><th>Most-clicked page</th><th>Clicks</th></tr></thead><tbody>{top_pages}</tbody></table></div></section>
{demo_editor}
{suggest_card}
<section class="card"><h2>💡 Next best action</h2><ul class="reco">{reco_html}</ul>
<p class="hint" style="margin-top:8px">Jump to: <a href="/admin/emails">emails</a> · <a href="/admin/social">social</a> · <a href="/admin/seo">SEO audit</a> · <a href="/admin/analytics">full analytics</a></p></section>
</main>
<footer><p>Attribution truth: a click is an email open (pixel), an email CTA click (/e/), a social tap (UTM), or a pageview. Everything here is computed from those real events.</p></footer>
<script src="/table-flow.js" defer></script>
{_TOTOP}
</body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _funnel_payload(self):
        """A REAL sales funnel, computed from actual attribution, not copy.

        Every stage is a real count from the DB with a genuine conversion % to
        the next stage:
          1. ATTRACT   landing-page views (events -> /lp|/n/)
          2. CAPTURE   confirmed, non-unsubscribed leads (subscribers)
          3. DELIVER   emails sent -> opened (email_events open) -> clicked
                       (clicks source=email, i.e. tracked /e/ CTA taps)
          4. MULTIPLY  total outbound affiliate clicks + social clicks + pages
        Leak = where people drop off, so the operator knows exactly what to fix."""
        with _lock:
            conn = _db()
            def q1(sql, args=()):
                return conn.execute(sql, args).fetchone()[0] or 0
            attract = q1("SELECT COUNT(*) FROM events WHERE name='view' "
                         "AND (slug LIKE 'lp-%' OR slug LIKE 'n-%' OR page LIKE '/lp/%' "
                         "OR page LIKE '/n/%')")
            capture = q1("SELECT COUNT(*) FROM subscribers WHERE confirmed=1 AND unsubscribed=0")
            sent = q1("SELECT COUNT(*) FROM sent_emails")
            opened = q1("SELECT COUNT(*) FROM email_events e JOIN sent_emails s "
                        "ON s.subscriber_id=e.subscriber_id AND s.email_index=e.email_index "
                        "WHERE e.type='open'")
            email_clicks = q1("SELECT COUNT(*) FROM clicks WHERE source='email'")
            page_clicks = q1("SELECT COUNT(*) FROM clicks WHERE source='page'")
            landing_clicks = q1("SELECT COUNT(*) FROM clicks WHERE source='landing-cta'")
            all_clicks = q1("SELECT COUNT(*) FROM clicks")
            social_clicks = q1("SELECT COUNT(*) FROM clicks WHERE source='social'")
            # per-channel click volume -> per-channel estimated earnings
            chan_rows = conn.execute(
                "SELECT COALESCE(NULLIF(source,''), 'page') src, COUNT(*) c FROM clicks "
                "GROUP BY src ORDER BY c DESC").fetchall()
            pages = q1("SELECT COUNT(*) FROM niches") + q1("SELECT COUNT(*) FROM topics")
            reviewed = q1("SELECT COUNT(*) FROM email_events WHERE type='open'")  # engagement proxy
            month_rows = conn.execute(
                "SELECT month, orders, earnings FROM earnings_records "
                "ORDER BY month DESC LIMIT 24").fetchall()
            # per-niche funnel breakdown: views / leads / clicks / est earnings
            niche_rows = conn.execute(
                "SELECT id, keyword, products FROM niches").fetchall()
            by_niche = []
            for nr in niche_rows:
                slug = seo._slugify(nr["keyword"]).lower()
                kw = nr["keyword"]
                nviews = 0
                try:
                    nviews = conn.execute(
                        "SELECT COUNT(*) FROM events WHERE name='view' AND "
                        "(page LIKE ? OR page LIKE ?)", ("%/" + slug + "%", "%/" + slug + "/%")).fetchone()[0] or 0
                except Exception:
                    nviews = 0
                nleads = conn.execute(
                    "SELECT COUNT(*) FROM subscribers WHERE confirmed=1 AND unsubscribed=0 "
                    "AND lower(keyword)=?", (kw.lower(),)).fetchone()[0] or 0
                nclicks = conn.execute(
                    "SELECT COUNT(*) FROM clicks WHERE lower(slug)=?", (slug,)).fetchone()[0] or 0
                nest = earnings.estimate(nclicks, "")
                by_niche.append({
                    "slug": slug, "keyword": kw,
                    "views": nviews, "leads": nleads, "clicks": nclicks,
                    "commission_est": round(nest["commission_est"], 2),
                    "orders_est": nest["orders_est"],
                    "n_products": len(json.loads(nr["products"] or "[]")),
                })
            by_niche.sort(key=lambda x: -x["commission_est"])
            # per-stage drop-off / conversion
            conn.close()
        # distinct subscribers who opened any email (real engaged audience)
        with _lock:
            c = _db()
            opened_subs = c.execute(
                "SELECT COUNT(DISTINCT subscriber_id) FROM email_events WHERE type='open'"
            ).fetchone()[0] or 0
            clicked_subs = c.execute(
                "SELECT COUNT(DISTINCT substr(referrer,1,80)) FROM clicks WHERE source='email'"
            ).fetchone()[0] or 0
            c.close()
        # per-channel estimated earnings (answers "which channel is worth investing in")
        chan_totals = []
        for r in chan_rows:
            est = earnings.estimate(r["c"], "")
            chan_totals.append({"source": r["src"], "clicks": r["c"],
                                "commission_est": round(est["commission_est"], 2),
                                "orders_est": est["orders_est"]})
        chan_totals.sort(key=lambda x: x["commission_est"], reverse=True)
        total_comm_est = round(sum(x["commission_est"] for x in chan_totals), 2)
        total_orders_est = sum(x["orders_est"] for x in chan_totals)
        real_ledger = [{"month": r["month"], "orders": r["orders"],
                        "earnings": r["earnings"]} for r in month_rows]
        real_summary = earnings.monthly_summary(real_ledger)
        # which channel leaks the most at the final click->earn step
        best_channel = chan_totals[0] if chan_totals else None
        stages = [
            {"n": 1, "name": "1 · Attract", "emoji": "🌐",
             "metric": "Landing / topic page views", "value": attract,
             "note": "Eyes on a page — from SEO, social or email."},
            {"n": 2, "name": "2 · Capture", "emoji": "✉️",
             "metric": "Confirmed leads (emails captured)", "value": capture,
             "note": "Visitors who opted in at the email gate."},
            {"n": 3, "name": "3 · Deliver", "emoji": "📨",
             "metric": "Emails opened", "value": opened_subs,
             "note": "Distinct subscribers who opened a sequence email."},
            {"n": 4, "name": "4 · Multiply", "emoji": "💰",
             "metric": "Outbound affiliate clicks", "value": all_clicks,
             "note": "Every tracked tap to Amazon across all channels."},
            {"n": 5, "name": "5 · Earn", "emoji": "🤑",
             "metric": "Estimated commission", "value": total_comm_est,
             "note": "Projected from %s clicks at %.1f%% AOV-$%.0f × %.2f%% order-rate." %
                     (all_clicks, earnings.commission_pct(""), earnings.avg_order(""),
                      earnings.order_rate("") * 100)},
        ]
        # conversion rates between consecutive stages (0 guard)
        def rate(i):
            if i == 0 or not stages[i - 1]["value"]:
                return 0.0
            return round(stages[i]["value"] / max(stages[i - 1]["value"], 1) * 100, 1)
        for i, s in enumerate(stages):
            s["conv"] = rate(i)
        # find the worst leak (biggest drop), to recommend a fix
        drops = [{"from": stages[i - 1], "to": stages[i],
                  "delta": stages[i - 1]["value"] - stages[i]["value"]}
                 for i in range(1, len(stages))]
        worst = max(drops, key=lambda d: d["delta"]) if drops else None
        reco = []
        if not attract:
            reco.append("Build + index pages first — there's no traffic entering the funnel (mine niches, generate long-tail pages, submit IndexNow).")
        elif worst and worst["delta"] > 0:
            reco.append("Biggest leak: %s → %s (lost %d). Fix that stage first." %
                        (worst["from"]["name"], worst["to"]["name"], worst["delta"]))
        if capture and opened_subs == 0:
            reco.append("You have %s leads but ZERO opens — tighten subject lines + sender name." % capture)
        elif opened_subs and email_clicks == 0:
            reco.append("Emails open but nobody clicks the CTA — make the tracked-link call-to-action more compelling.")
        if pages == 0:
            reco.append("No content pages yet — the funnel has no engine. Build niches.")
        if best_channel and best_channel["clicks"] > 0:
            reco.append("Top channel by est. earnings is '%s' (%.2f). Double down there — it's already converting your audience." %
                        (best_channel["source"], best_channel["commission_est"]))
        elif all_clicks == 0:
            reco.append("No clicks recorded yet — push pages to social + email so the funnel earns.")
        if not reco:
            reco.append("Funnel is flowing — grow the top (more pages, more traffic) and keep every stage >0.")
        return {
            "stages": stages,
            "leak": ({"from": worst["from"]["name"], "to": worst["to"]["name"],
                      "lost": worst["delta"]} if worst else None),
            "stats": {
                "emails_sent": sent, "emails_opened": opened, "distinct_openers": opened_subs,
                "email_clicks": email_clicks, "email_convert_to_click": round(
                    email_clicks / max(opened, 1) * 100, 1),
                "social_clicks": social_clicks, "page_clicks": page_clicks,
                "landing_clicks": landing_clicks, "all_clicks": all_clicks,
                "pages": pages,
                "commission_est": total_comm_est, "orders_est": total_orders_est,
                "channels": chan_totals, "best_channel": best_channel["source"] if best_channel else None,
                "real_orders": real_summary["total_orders"],
                "real_earnings": round(real_summary["total_earnings"], 2),
            },
            "by_niche": by_niche,
            "recommendations": reco,
        }

    def _funnel_api(self):
        return self._send(200, self._funnel_payload())

    def _admin_funnel(self):
        """Full-page visual of the REAL sales funnel (data-backed stages + leaks)."""
        p = self._funnel_payload()
        def bar(i, s):
            maxv = max((st["value"] for st in p["stages"]), default=1) or 1
            w = max(2, round(s["value"] / maxv * 100))
            return ('<div class="fstage" id="s%d">'
                    '<div class="flabel"><b>%s %s</b> <span class="fval">%s</span></div>'
                    '<div class="fbar"><span style="width:%s%%"></span></div>'
                    '<div class="fm">%s · <span class="hint">%s</span></div>'
                    '<div class="fconv">→ to next: <b>%s%%</b></div></div>'
                    % (i, s["emoji"], seo._clean(s["name"]), seo._clean(str(s["value"])),
                       w, seo._clean(s["metric"]), seo._clean(s["note"]),
                       s["conv"]))
        stages_html = "".join(bar(i, s) for i, s in enumerate(p["stages"]))
        leak_html = ("<p class='leak'><b>Biggest leak:</b> %s → %s (lost %s).</p>"
                     % (seo._clean(p["leak"]["from"]), seo._clean(p["leak"]["to"]),
                        seo._clean(str(p["leak"]["lost"])))) if p["leak"] else ""
        st = p["stats"]
        stats_html = ("<div class='row' style='align-items:stretch'>"
                      + "".join(
                          '<div class="feature"><h3>%s</h3><p class="hint">%s</p></div>'
                          % (seo._clean(str(v)), seo._clean(l))
                          for v, l in [(st["emails_sent"], "emails sent"),
                                       (st["emails_opened"], "emails opened"),
                                       (st["email_clicks"], "email clicks"),
                                       ("%.1f%%" % st["email_convert_to_click"], "open→click"),
                                       (st["social_clicks"], "social clicks"),
                                       (st["pages"], "content pages")]
                      ) + "</div>")
        reco_html = "".join("<li>%s</li>" % r for r in p["recommendations"])
        chan_rows_html = "".join(
            "<tr><td>%s</td><td class='ct'>%s</td><td class='ct'>%.2f</td></tr>"
            % (seo._clean(x["source"]), x["clicks"], x["commission_est"])
            for x in st["channels"]) or "<tr><td colspan='3' class='hint'>No clicks yet.</td></tr>"
        earn_html = ("<div class='row'><div class='feature'><h3>$%.2f</h3>"
                     "<p class='hint'>est. commission · %.1f orders</p></div>"
                     "<div class='feature'><h3>$%.2f</h3><p class='hint'>real earnings logged</p></div></div>"
                     "<h3>Channel returns (est.)</h3>"
                     "<div class='table-wrap'><table><thead><tr><th>Source</th><th class='ct'>Clicks</th>"
                     "<th class='ct'>Est. commission</th></tr></thead><tbody>%s</tbody></table></div>"
                     % (st["commission_est"], st["orders_est"], st["real_earnings"], chan_rows_html))
        rows_html = ("<h3>Earn — clicks your channels actually bill</h3>"
                     + "<div class='row'>"
                     + "".join('<div class="feature"><h3>%s</h3><p class="hint">%s</p></div>'
                               % (seo._clean(str(v)), seo._clean(l))
                               for v, l in [(st["page_clicks"], "organic page clicks"),
                                            (st["landing_clicks"], "landing CTA clicks"),
                                            (st["commission_est"], "total est. commission $")]
                               ) + "</div>")
        nrows = "".join(
            "<tr><td>%s</td><td class='ct'>%s</td><td class='ct'>%s</td><td class='ct'>%s</td>"
            "<td class='ct'>$%.2f</td><td class='ct'>%s</td></tr>"
            % (seo._clean(x["keyword"]), x["views"], x["leads"], x["clicks"],
               x["commission_est"], x["n_products"])
            for x in p["by_niche"]) or "<tr><td colspan='6' class='hint'>No saved niches yet.</td></tr>"
        niche_html = ("<h3>By niche — who converts &amp; who leaks</h3>"
                      "<div class='table-wrap'><table><thead><tr><th>Niche</th><th class='ct'>Views</th>"
                      "<th class='ct'>Leads</th><th class='ct'>Clicks</th>"
                      "<th class='ct'>Est. commission</th><th class='ct'>Products</th></tr></thead>"
                      "<tbody>%s</tbody></table></div>" % nrows)
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sales funnel — pstore</title><link rel="stylesheet" href="/style.css">
<meta name="robots" content="noindex,nofollow">
<style>.fstage{{margin:0 0 16px}}.flabel{{display:flex;justify-content:space-between;align-items:baseline;font-size:16px}}
.fbar{{height:22px;background:var(--border,#eee);border-radius:999px;overflow:hidden;margin:5px 0}}
.fbar span{{display:block;height:100%;background:linear-gradient(90deg,#5b8cff,#8f6bff);border-radius:999px}}
.fm{{color:#444;font-size:13px;margin:2px 0}}.fconv{{font-size:13px;color:#2e7a5b}}
.leak{{color:#b3261e;font-weight:600;margin:12px 0}}</style>
</head><body>
<header id="top"><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>The real <span>sales funnel.</span></h1>
<p class="tagline">Not a diagram of should-haves — every stage is a live count from your own pageviews, captured leads, opened emails and tracked clicks, with the actual conversion % to the next stage and the biggest leak called out.</p></div>
{self._admin_nav('funnel')}
</header>
<main>
<section class="card"><h2>⚙️ Funnel stages</h2>{stages_html}{leak_html}</section>
<section class="card"><h2>📊 Delivery &amp; multiply detail</h2>{stats_html}</section>
<section class="card"><h2>🤑 Earn from clicks</h2>{earn_html}{rows_html}</section>
<section class="card"><h2>🎯 Per-niche funnel</h2>{niche_html}
<p class="hint">Sorted by est. commission — the niche converting traffic into clicks (and stays) is the one to pour social + email into first; the high-traffic/no-lead ones need a stronger email gate.</p></section>
<section class="card"><h2>💡 What to fix first</h2><ul class="reco">{reco_html}</ul>
<p class="hint" style="margin-top:8px">Act on the leak: <a href="/admin/emails">emails</a> · <a href="/admin/social">social</a> · <a href="/admin/seo">SEO</a> · <a href="/admin/analytics">analytics</a> · <a href="/admin/marketing">ROI dashboard</a></p></section>
</main>
<footer><p>Funnel truth: pages = views of /lp / /n pages; leads = confirmed subscribers; deliver = distinct openers; multiply = all tracked outbound clicks. Never fabricated.</p></footer>
<script src="/table-flow.js" defer></script>
{_TOTOP}
</body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _sem_payload(self, keyword):
        keyword = (keyword or "").strip()
        if not keyword:
            return {"keyword": "", "error": "keyword required", "brief": None}
        niche = self._saved_niche(keyword)
        if not niche:
            return {"keyword": keyword, "error": "no saved niche matches that keyword", "brief": None}
        audit_row = seo.audit_niche(niche)
        entries = [seo._slugify(n["keyword"]) for n in self._all_niches()]
        return sem.brief(keyword, niche, seo.BASE_URL,
                         audit_row=audit_row, indexnow_key=indexnow.key(),
                         sitemap_entries=entries)

    def _sem_api(self, q):
        keyword = (q.get("keyword") or [""])[0].strip()
        return self._send(200, self._sem_payload(keyword))

    def _sem_build_topic_api(self):
        """One-click build of a long-tail topic page under a parent niche
        (POST /api/sem/build-topic). The SEM long-tail 'Build this page' button
        used to call the niche-mining /api/suggest/build, which treated the
        long-tail phrase as a whole new niche — slow, and wrong output. This
        endpoint instead inserts the exact phrase as a /n/<parent>/<term> topic
        page (fast, no mining), fleshes out the sibling topics from autosuggest,
        and pings IndexNow. Returns the topic URL(s)."""
        body = self._body()
        parent = str(body.get("parent") or "").strip()
        term = str(body.get("term") or "").strip()
        if not parent or not term:
            return self._send(400, {"error": "parent and term required",
                                    "ok": False})
        niche = self._saved_niche(parent)
        if not niche:
            return self._send(404, {"error": "no saved niche matches that parent",
                                    "ok": False})
        parent_slug = seo._slugify(niche["keyword"])
        term_slug = seo._slugify(term)
        if not term_slug or term_slug == parent_slug:
            return self._send(400, {"error": "that term can't be built as a topic",
                                    "ok": False})
        url = "/n/%s/%s" % (parent_slug, term_slug)
        with _lock:
            conn = _db()
            conn.execute(
                "INSERT OR IGNORE INTO topics (parent_slug, term, slug) VALUES (?,?,?)",
                (parent_slug, term, term_slug))
            conn.commit()
            conn.close()
        try:
            siblings = self._generate_topics(parent_slug, 5)
        except Exception:
            siblings = []
        self._fire_indexnow([url] + [s["url"] for s in siblings])
        return self._send(200, {"ok": True, "parent": parent_slug,
                                "slug": term_slug, "url": url,
                                "term": term,
                                "topic_pages": len(siblings) + 1,
                                "urls": [url] + [s["url"] for s in siblings]})

    def _admin_apikeys(self, q):
        """One page to paste third-party API keys via the UI (no env/restart
        needed): PA-API (official Amazon product data) and per-platform social
        posting keys + webhook. Values persist to DB and survive restarts."""
        pa = paapi.status()
        s = self._settings()["social"]
        pa_fields = [
            ("access_key", "Client key", "AWS access key",
             _get_setting("paapi.access_key"), "paapi.access_key"),
            ("secret_key", "Secret key", "AWS secret key",
             _get_setting("paapi.secret_key"), "paapi.secret_key"),
            ("partner_tag", "Partner tag", "Associates tag, e.g. pstore-20",
             _get_setting("paapi.partner_tag"), "paapi.partner_tag"),
        ]
        pa_rows = "".join(
            '<label>%s <input type="password" name="%s" value="%s" placeholder="%s" '
            'autocomplete="off" data-masked="1" data-skey="%s"></label>'
            % (lbl, n, self._maskkv(n, skey), ph, skey)
            for n, lbl, ph, v, skey in pa_fields)
        key_rows = "".join(
            '<label>%s <input type="password" name="key_%s" value="%s" '
            'placeholder="API key/token" autocomplete="off" data-masked="1"></label>'
            % (seo._clean(p), seo._clean(social._key(p)), self._maskkv(social._key(p)))
            for p in social.PLATFORMS)
        tw_meta = [
            ("twitter.client_id", "Consumer key (API key)", "Twitter client_id"),
            ("twitter.client_secret", "Consumer secret (API secret)", "client_secret"),
            ("twitter.access_token", "Access token", "access_token"),
            ("twitter.access_token_secret", "Access token secret", "access_token_secret"),
        ]
        tw_rows = "".join(
            '<label>%s <input type="password" name="key_%s" value="%s" '
            'placeholder="%s" autocomplete="off" data-masked="1" data-tw="1"></label>'
            % (lbl, f.split(".")[-1],
               self._maskkv(f.split(".")[-1], "social.key.twitter.%s" % f.split(".")[-1]),
               ph)
            for f, lbl, ph in tw_meta)
        webhook_val = seo._clean(_get_setting("social.webhook"))
        pa_ready = "✅ ready" if pa["ready"] else "⚠️ incomplete — add the three PA-API values"
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>API Keys — pstore</title><link rel="stylesheet" href="/style.css">
<meta name="robots" content="noindex,nofollow">
</head><body>
<header id="top"><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>Paste your <span>API keys</span> once.</h1>
<p class="tagline">No env vars, no restart. Keys are stored on this instance and used on the next request.</p></div>
{self._admin_nav('apikeys')}
</header>
<main>
<section class="card"><h2>🛒 Amazon PA-API (official product data)</h2>
<p class="hint">Status: {pa_ready}. Enables official, richer product lookups instead of the public scraper. Get these at the Amazon Associates <b>Product Advertising API</b> console.</p>
<form class="cols-form" id="fpa" onsubmit="return pa_save();">
  {pa_rows}
  <div class="row"><button class="btn">Save PA-API</button>
  <button type="button" class="btn" onclick="pa_test();">Test PA-API</button>
  <span id="paout" class="msg"></span></div>
</form>
</section>
<section class="card"><h2>📣 Social publishing keys</h2>
<p class="hint">Per-platform {len(social.PLATFORMS)} keys power native posting. Leave blank to skip that platform. Enable native posting by pasting each platform's API key/token here; real posting also fires <code>SOCIAL_WEBHOOK</code> if set.</p>
<form class="cols-form" id="fsoc" onsubmit="return soc_save();">
  <label>Webhook URL <input type="url" name="webhook" value="{webhook_val}" placeholder="https://hook.example/hook (Zapier/Make)"></label>
  {key_rows}
  <h3>Twitter / X (optional, 4 fields)</h3>
  <div class="row">{tw_rows}</div>
  <div class="row"><button class="btn">Save social keys</button><span id="socout" class="msg"></span></div>
</form>
</section>
</main>
<footer><p>Keys are stored locally on this install; the live AWS/social account credentials never leave your instance. API keys are shown masked; only overwrite a field to change it.</p></footer>
<script>
function $(id){{return document.getElementById(id);}}
async function post(url, data){{
  const r = await fetch(url, {{method:"POST", headers:{{"Content-Type":"application/json"}}, body: JSON.stringify(data)}});
  return r.json().catch(()=>({{ok:false}}));
}}
function collect_filled(sel){{
  const d = {{}};
  document.querySelectorAll(sel).forEach(el => {{
    const key = el.name.replace(/^key_/, "");
    if (el.value && el.value.indexOf("•") === -1) d[key] = el.value;
  }});
  return d;
}}
async function pa_save(){{
  $("paout").textContent = "Saving…";
  const pa = collect_filled("#fpa input[data-masked]");
  const d = await post("/api/settings", {{paapi: pa}});
  $("paout").textContent = d && d.ok ? "Saved ✓" : ((d && d.error) || "Save failed");
  return false;
}}
async function pa_test(){{
  $("paout").textContent = "Testing…";
  const d = await post("/api/settings/test", {{paapi: true}});
  $("paout").textContent = d && d.ok ? ("Test ✓ " + (d.detail || "")) : ((d && d.error) || "Test failed");
  return false;
}}
async function soc_save(){{
  $("socout").textContent = "Saving…";
  const keys = collect_filled("#fsoc input[data-masked]:not([data-tw])");
  const twitter = collect_filled("#fsoc input[data-tw]");
  const d = await post("/api/settings", {{social: {{
    webhook: document.querySelector('[name="webhook"]').value,
    keys: keys,
    twitter: twitter
  }}}});
  $("socout").textContent = d && d.ok ? "Saved ✓" : ((d && d.error) || "Save failed");
  return false;
}}
</script>
</body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _maskkv(self, key, skey=None):
        """Short masked placeholder for an already-stored key so existing values
        aren't echoed in plaintext in the form. Empty string (not the literal
        placeholder text) when nothing is stored, so a blank field can never be
        saved back as a real key."""
        v = _get_setting(skey or ("social.key." + key))
        return (v[:4] + "••••") if v else ""

    def _admin_opportunities(self, q):
        """Grow page: proven niches (real clicks) + one-click expansion that
        auto-builds new pages for their related, not-yet-built search terms."""
        note = ""
        winners_html = ""
        try:
            payload = self._opportunities_data()
            note = payload.get("note", "")
            winners = payload.get("winners", [])
        except Exception:
            winners = []
            note = "Opportunities unavailable right now — try again in a moment."
        cards = []
        for w in winners:
            sugg = "".join("<li><code>%s</code></li>" % seo._clean(s)
                           for s in w["suggestions"][:6]) or "<li>none found</li>"
            topics = w.get("topic_urls") or []
            tree = ("<p class='hint'><b>%d long-tail page%s built</b>: %s</p>" %
                    (w.get("topic_count", 0),
                     "" if w.get("topic_count", 0) == 1 else "s",
                     " · ".join("<a href='%s'>%s</a>" % (u, u.rsplit("/", 1)[-1])
                                for u in topics[:6]) or "none yet")) if topics else \
                "<p class='hint'><b>0 long-tail pages built</b> — layer them under this winner.</p>"
            cards.append(
                '<div class="sub"><h3>%s <span class="who">· %d Amazon clicks</span></h3>'
                '<p class="hint">Click through on <b>/%s</b> proves demand. %d related term%s not yet built:</p>'
                '<ul class="pilos">%s</ul>'
                '%s'
                '<button class="warm grow" data-slug="%s">Build %d new page(s)</button>'
                '<button class="warm lt" data-slug="%s">%s</button></div>'
                % (seo._clean(w["keyword"]), w["clicks"], seo._clean(w["slug"]),
                   w["unbuilt"], "" if w["unbuilt"] == 1 else "s", sugg, tree,
                   seo._clean(w["slug"]), min(6, max(w["unbuilt"], 1)),
                   seo._clean(w["slug"]),
                   ("Build long-tail pages" if w.get("topic_count", 0) == 0
                    else "+%d more long-tail page(s)" % min(6, w.get("topic_count", 0)))))
        winners_html = "".join(cards) or \
            '<p class="hint">No proven winners yet. Publish social posts and get pages indexed — clicks here become build targets.</p>'
        try:
            _sg = self._suggest_payload()
            _sugs = _sg.get("suggestions") or []
            _profile = _sg.get("profile") or {}
            _seednote = "profile: %s · %s · %s" % (_profile.get("region") or "any region",
                                                   _profile.get("interest") or "any interest",
                                                   _profile.get("behavior") or "any behavior")
        except Exception:
            _sugs, _seednote = [], ""
        _sug_cards = "".join(
            '<div class="sub"><h3>%s <span class="who">· score %s / sat %s</span></h3>'
            '<p class="hint">%s · %d products</p>'
            '<p><button type="button" class="btn" data-kw="%s">⚡ Build this idea</button> '
            '<span class="pill hint" data-sb="%s">unbuilt</span></p></div>'
            % (seo._clean(s["keyword"]), seo._clean(str(s["score"])),
               seo._clean(str(s["saturation"] or 0)), seo._clean(s.get("reason") or ""),
               s.get("count") or 0, seo._clean(s["keyword"]), seo._clean(s["keyword"]))
            for s in _sugs) or \
            '<p class="hint">Set a demography interest in the Market → ROI dashboard, then come back — or edit the profile below.</p>'
        suggest_html = (
            '<section class="card"><h2>🎯 Suggested by your audience (<a href="/admin/marketing#demo" '
            'style="font-size:12px">edit demography</a>)</h2>'
            '<p class="hint">Fresh niches the engine thinks your audience will click, ranked by demand + persona match. '
            'One click mines it, saves it, and builds 6 long-tail pages. Seeds: %s</p>'
            '<div class="cols">%s</div>'
            '<p id="sout" class="msg"></p></section>'
            % (seo._clean(_seednote), _sug_cards))
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Grow — pstore</title><link rel="stylesheet" href="/style.css">
<meta name="robots" content="noindex,nofollow">
<style>.pilos {{ list-style:none; padding:0; margin:8px 0; }} .pilos li {{ margin:2px 0; }}</style>
</head><body>
<header id="top"><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>Turn clicks into <span>more pages.</span></h1>
<p class="tagline">{note}</p></div>
{self._admin_nav('opportunities')}
</header>
<main>
<section class="card"><h2>📈 Build on your winners</h2>
<p class="hint">Each niche below already pulled real Amazon clicks. "Build" mines its related search terms live, saves new pockets as pages, and pings IndexNow so Google finds them fast. "Build long-tail pages" adds nested <code>/n/&lt;niche&gt;/&lt;term&gt;</code> URLs for even deeper reach.</p>
<div class="cols">{winners_html}</div>
<p id="out" class="msg"></p>
</section>
{suggest_html}
</main>
<footer><p>New pages are created from live Amazon autosuggest terms around a proven niche — demand-driven, not guessed. Long-tail pages layer even more indexable URLs under each winner.</p></footer>
<script>
function $(id){{return document.getElementById(id);}}
async function hit(url, slug, out, btn){{
  const old = btn.textContent; btn.disabled = true;
  $("out").textContent = "Mining and creating pages…";
  const r = await fetch(url, {{method:"POST", headers:{{"Content-Type":"application/json"}},
    body: JSON.stringify({{slug: slug, count: 6}})}});
  const d = await r.json().catch(()=>({{ok:false}}));
  if (d && d.ok) {{
    $("out").textContent = "Created " + d.count + " from “" + (d.seed||d.parent) + "”: " +
      (d.created.map(x=>x.slug||x.term).join(", "));
  }} else {{
    $("out").textContent = (d && (d.error || d.message)) || "Build failed.";
    btn.disabled = false; btn.textContent = old;
  }}
}}
document.addEventListener("click", async (e)=>{{
  const grow = e.target.closest(".grow");
  if (grow) return hit("/api/opportunities/expand", grow.dataset.slug, $("out"), grow);
  const lt = e.target.closest(".lt");
  if (lt) return hit("/api/topics/generate", lt.dataset.slug, $("out"), lt);
  const sb = e.target.closest("[data-kw]");
  if (sb) {{
    sb.disabled = true; const old = sb.textContent; sb.textContent = "Building…";
    $("sout").textContent = "Mining “" + sb.dataset.kw + "” and creating pages…";
    const r = await fetch("/api/suggest/build", {{method:"POST",
      headers:{{"Content-Type":"application/json"}}, body: JSON.stringify({{keyword: sb.dataset.kw}})}});
    const d = await r.json().catch(()=>({{ok:false}}));
    const ph = sb.parentElement.querySelector("[data-sb]");
    if (d && d.ok) {{
      $("sout").textContent = "Built \u201C" + d.keyword + "\u201D /n/" + d.slug + " + " + d.topic_pages + " long-tail pages.";
      ph.textContent = "✓ built"; ph.className = "pill ok"; sb.style.display = "none";
    }} else {{
      $("sout").textContent = (d && (d.error || d.message)) || "Build failed.";
      sb.disabled = false; sb.textContent = old; ph.textContent = "failed"; ph.className = "pill err";
    }}
  }}
}});
</script>
</body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _admin_priority(self, q):
        """Prioritize: the built niches with the highest projected commission go
        first — they get the next social posts, ad boosts and index budget, so
        money flows to the winners instead of being spread thin."""
        d = self._earnings_priority_data()
        rows = ""
        for r in d["ranked"]:
            rows += ('<div class="sub"><div class="rank">%s</div><h3>%s <span class="who">· %s</span></h3>'
                     '<p class="hint">%d Amazon clicks → <b>$%.2f</b> est. commission (%d%% of today\'s projected total)</p></div>'
                     % (d["ranked"].index(r) + 1, seo._clean(r["niche"]), "$%.2f" % r["score"],
                        r["clicks"], r["commission_est"], round(r["share"] * 100)))
        rows = ("<div class='cols'>%s</div>" % rows) if rows else \
            '<p class="hint">No clicks yet. As Amazon clicks accumulate, the niches projected to earn the most rise to the top so you spend effort where money is.</p>'
        real_html = ""
        if d["real"].get("total_orders"):
            real_html = ("<p class='hint'>Real Associates ledger: <b>%d orders · $%.2f earnings</b> across %d month(s)."
                         % (d["real"]["total_orders"], d["real"]["total_earnings"],
                            len(d["real"]["months"])))
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Prioritize — pstore</title><link rel="stylesheet" href="/style.css">
<meta name="robots" content="noindex,nofollow">
<style>.cols .sub{{border-left:4px solid var(--brand, #e8710a);padding:12px 14px;margin:8px 0;}} .rank{{display:inline-block;min-width:1.4em;font-weight:700;color:#e8710a;}}</style>
</head><body>
<header id="top"><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>Spend effort <span>where money is.</span></h1>
<p class="tagline">{d["note"]}</p></div>
{self._admin_nav('priority')}
</header>
<main>
<section class="card"><h2>💰 Niches, ranked by projected commission</h2>
<p class="hint">Formula: Amazon clicks × $avg order × % commission × order rate. Real logged earnings are shown too so you can sanity-check the model.</p>
{rows}
{real_html}
<p id="out" class="msg"></p>
</section>
</main>
<footer><p>Prioritization is earnings-driven: high-commission niches with real traffic outrank quiet ones, guiding your next social post, boost and index ping.</p></footer>
</body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _admin_sem(self, q):
        niches = [n["keyword"] for n in self._all_niches()]
        keyword = (q.get("keyword") or [""])[0].strip() or (niches[0] if niches else "")
        opts = "".join('<option value="%s"%s>%s</option>' % (seo._clean(k),
                        ' selected' if k == keyword else "", seo._clean(k)) for k in niches)
        if not keyword:
            return self._send(200, ("""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>SEM — pstore</title><link rel="stylesheet" href="/style.css">
<meta name="robots" content="noindex,nofollow"></head><body>
<header><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>Search <span>funnel.</span></h1>
<p class="tagline">Turn a review page into a search funnel — long-tail expansion, intent brief, people-also-ask and a performance checklist.</p></div>
{self._admin_nav('sem')}</header><main>
<section class="card"><h2>🎯 Search-engine marketing</h2>
<p class="hint">No saved niches yet — mine one on the dashboard first.</p></section></main></body></html>""").encode("utf-8"), "text/html; charset=utf-8")
        brief = self._sem_payload(keyword)
        info = brief or {}
        if not info:
            return self._send(200, ("<h3>%s</h3><p>%s</p>" % (seo._clean(keyword),
                               seo._clean(brief.get("error") or "no brief"))).encode("utf-8"),
                              "text/html; charset=utf-8")

        lt = info.get("longtail") or []
        intent = info.get("intent") or {}
        paa = info.get("paa") or []
        perf = info.get("performance") or []
        page = info.get("page") or {}
        boosts = self._boosts_for(keyword) if keyword else []
        long_phrases = [g.get("phrase") for g in lt if g.get("phrase")]
        boost_kw = " · ".join(long_phrases[:3]) if long_phrases else "none yet"
        boost_rows = "".join(
            '<div class="sub"><h3>🚀 %s</h3>'
            '<p class="who">%s ✓ · %d run · %d clicks</p>'
            '<p class="key"><a href="%s">%s</a></p></div>'
            % (seo._clean(b["name"]),
               b["status"], b["runs"] or 0, b["clicks"] or 0,
               seo._clean(b["link"] or "#"), seo._clean(b["link"] or "—"))
            for b in boosts) or '<p class="hint">Run boosts on the Workbench — they weave these long-tail terms in.</p>'

        def chip(t, s):
            return '<span class="badge %s">%s</span>' % (s, t)
        sem_parent_slug = seo._slugify(keyword)
        sem_built = {t["slug"] for t in self._topics_for(sem_parent_slug)}
        def _topic_slug(phrase):
            s = seo._slugify(phrase)
            return "-".join([p for p in s.split("/") if p])
        lt_html = "".join(
            '<div class="sub"><h3>%s %s</h3>'
            '<p class="key"><a href="/n/%s/%s">/n/%s/%s</a></p>'
            '<p><button type="button" class="btn" data-build="1" data-parent="%s" data-term="%s">⚡ Build this page</button> '
            '<span class="pill %s" data-state="%s">%s</span></p></div>'
            % (seo._clean(g["phrase"]), chip(g["intent"],
                 "source" if g["intent"] == "target" else "demand"),
               seo._clean(sem_parent_slug), seo._clean(_topic_slug(g["phrase"])),
               seo._clean(sem_parent_slug), seo._clean(_topic_slug(g["phrase"])),
               seo._clean(keyword), seo._clean(g["phrase"]),
               "ok" if _topic_slug(g["phrase"]) in sem_built else "hint",
               seo._clean(_topic_slug(g["phrase"])),
               "✓ built" if _topic_slug(g["phrase"]) in sem_built else "unbuilt")
            for g in lt) or '<p class="hint">No suggestions yet.</p>'
        fixes_html = "".join(
            '<li><b>%s</b> — %s</li>' % (seo._clean(f["label"]), seo._clean(f["detail"]))
            for f in (intent.get("fixes") or []))
        paa_html = "".join(
            ('<details class="faq"><summary>%s</summary>'
             '<p>%s</p></details>' % (seo._clean(qa["question"]),
                                      seo._clean(qa["answer"])))
            for qa in paa) or '<p class="hint">No questions yet.</p>'
        perf_html = "".join(
            '<div class="sub"><h3>✓ %s</h3><p class="who">%s</p></div>'
            % (seo._clean(p["label"]), seo._clean(p["detail"])) for p in perf)
        page_state = ("<b>in sitemap</b>" if page.get("sitemap") else "not in sitemap") + \
            (" · <b>IndexNow key ready</b>" if page.get("indexnow_key") else " · IndexNow key missing")
        sem_build_js = ("(function(){var b=document.querySelectorAll('[data-build]');"
            "Array.prototype.forEach.call(b,function(x){x.addEventListener('click',function(){"
            "var ph=x.parentElement.querySelector('[data-state]');ph.textContent='building…';ph.className='pill hint';"
            "var p=x.getAttribute('data-parent');var t=x.getAttribute('data-term');"
            "fetch('/api/sem/build-topic',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({parent:p,term:t})})"
            ".then(function(r){return r.json()}).then(function(j){"
            "if(j&&j.ok){ph.textContent='✓ built — '+j.url+' + '+j.topic_pages+' topics';ph.className='pill ok';x.style.display='none';"
            "var u=x.parentElement.querySelector('a[href]');if(u){u.href=j.url;}}"
            "else{ph.textContent='build failed'+(j&&j.error?': '+j.error:'');ph.className='pill err';}"
            "}).catch(function(){ph.textContent='build failed';ph.className='pill err';});});});}());")
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>SEM — {seo._clean(keyword)} · pstore</title><link rel="stylesheet" href="/style.css">
<meta name="robots" content="noindex,nofollow">
<style>.key{{font-family:ui-monospace,Menlo,monospace;font-size:12.5px;background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:8px 10px;margin:4px 0 0;word-break:break-all}}</style>
</head><body>
<header id="top"><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>Search <span>funnel.</span></h1>
<p class="tagline">Turn the “best {seo._clean(keyword)}” review page into a search funnel — long-tail expansion, intent brief, people-also-ask and a performance checklist.</p></div>
{self._admin_nav('sem')}
</header>
<main>
<section class="card"><h2>🎯 Choose a niche</h2>
<form class="row" method="get" action="/admin/sem">
  <label>Niche <select name="keyword" onchange="this.form.submit()">{opts}</select></label>
</form></section>
<section class="card"><h2>🔎 Intent brief</h2>
<p class="hint">The one job of this page, stated plainly, with concrete copy targets.</p>
<div class="sub"><h3>Primary question</h3><p class="who">{seo._clean(intent.get('primary_question',''))}</p></div>
<div class="sub"><h3>Search intent</h3><p class="who">{seo._clean(intent.get('search_intent',''))}</p></div>
<div class="sub"><h3>H1 target</h3><p class="key">{seo._clean(intent.get('h1_target',''))}</p></div>
<div class="sub"><h3>Meta target</h3><p class="key">{seo._clean(intent.get('meta_target',''))}</p></div>
<div class="sub"><h3>Quick wins</h3><ul>{fixes_html}</ul></div></section>
<section class="card"><h2>🗂 Long-tail expansion</h2>
<p class="hint">Related searches (from Amazon's autosuggest) the page should ideally cover — view each as its own crawlable URL.</p>
{lt_html}</section>
<section class="card"><h2>❓ People also ask</h2>
<p class="hint">Copy-ready FAQ prompts in SERP phrasing. <code>__blank__</code> answers are yours to fill in with honest copy.</p>
{paa_html}</section>
<section class="card"><h2>⚡ Performance checklist</h2>
{perf_html}</section>
<section class="card"><h2>🌐 Live URL status</h2>
<p class="hint" style="margin-top:-4px">Page status: {page_state}</p>
<div class="sub"><h3>Canonical</h3><p class="key"><a href="{seo._clean(page.get('canonical',''))}">{seo._clean(page.get('canonical',''))}</a></p></div>
<div class="sub"><h3>Share image</h3><p class="key"><a href="{seo._clean(page.get('og_image',''))}">{seo._clean(page.get('og_image',''))}</a></p></div>
<div class="sub"><h3>Landing page</h3><p class="key"><a href="{seo._clean(page.get('landing_url',''))}">{seo._clean(page.get('landing_url',''))}</a></p></div></section>
<section class="card"><h2>🚀 Long-tail boosts</h2>
<p class="hint">These campaigns weave the {seo._clean(boost_kw or 'long-tail')} terms above into social-ready promo copy, each with a stable UTM-tracked link back to the landing page. Run them in the Workbench.</p>
<p class="hint"><b>Fold-in phrases:</b> {seo._clean(boost_kw)}</p>
{boost_rows}
<p class="hint"><a href="/tool?keyword={seo._clean(keyword)}">Open Workbench for “{seo._clean(keyword)}” →</a></p></section>
</main>
<footer><p>The funnel grows the keyword pool around each review page. All suggestions are honest — we never assert traffic or rankings we can't observe.</p></footer>
<script>
{sem_build_js}
</script>
{_TOTOP}
</body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")


# ------------------------------------------------------------------ email suite
    def _record_click(self, slug, source="page", referrer="", asin="", content=""):
        ip = security.ip_token(self._client_ip())
        with _lock:
            conn = _db()
            conn.execute(
                "INSERT INTO clicks (slug, source, ip, referrer, asin, content) VALUES (?,?,?,?,?,?)",
                (slug, source, ip, referrer, asin, content))
            conn.commit()
            conn.close()

    def _subscribe(self):
        key = "sub|" + security.client_key(self.headers, self._client_ip())
        if not security.SUBSCRIBE_LIMITER.hit(key):
            return self._send(429, {"ok": False, "error": "Too many signups from this device — try again later."})
        body = self._body()
        email = str(body.get("email") or "").strip().lower()
        if not _EMAIL_RE.match(email):
            return self._send(200, {"ok": False, "error": "That email doesn't look right."})
        if len(email) > 200:
            return self._send(200, {"ok": False, "error": "Email too long."})
        keyword = str(body.get("keyword") or "").strip()[:120]
        first_name = str(body.get("first_name") or "").strip()[:80]
        source = (str(body.get("source") or "niche").strip()[:40]) or "niche"
        with _lock:
            conn = _db()
            row = conn.execute("SELECT id, unsubscribed FROM subscribers WHERE email=?",
                               (email,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE subscribers SET unsubscribed=0, confirmed=1, source=?, keyword=?, first_name=? "
                    "WHERE id=?", (source, keyword, first_name, row["id"]))
                sid = row["id"]
                msg = "You're subscribed again — the next update will find its way to your inbox."
            else:
                cur = conn.execute(
                    "INSERT INTO subscribers (email, source, keyword, first_name, confirmed) "
                    "VALUES (?,?,?,?,1)", (email, source, keyword, first_name))
                sid = cur.lastrowid
                msg = "Done — you'll only hear from us when these picks change, and you can unsubscribe any time."
            conn.commit()
            conn.close()
        # Signed, short-lived token lets this just-opted-in visitor grab the
        # gated PDF lead magnet immediately (Cialdini's reciprocity in action).
        token = security.make_token("pdf:" + keyword, 10 * 60) if keyword else ""
        return self._send(200, {"ok": True, "id": sid, "message": msg,
                                "download_token": token})

    def _gated_pdf(self, q):
        """Public endpoint that serves the niche's PDF lead magnet:
        - when a page's settings have pdf_gated=false the PDF is served freely
          (reciprocity without an email wall);
        - otherwise a token from /subscribe (HMAC-signed, scoped to the exact
          keyword, short-lived) is required so only just-opted-in visitors can
          grab the lead magnet.
        """
        keyword = (q.get("keyword") or [""])[0].strip()
        if not keyword:
            return self._send(403, {"error": "not authorized"})
        gated = True
        with _lock:
            conn = _db()
            page = cms_mod.get_page(conn, keyword)
            if page and (page.get("settings") or {}).get("pdf_gated") is False:
                gated = False
            conn.close()
        if gated:
            token = (q.get("token") or [""])[0].strip()
            if not token or security.verify_token(token) != "pdf:" + keyword:
                return self._send(403, {"error": "not authorized"})
        book = self._ebook_for(keyword)
        if not book:
            return self._send(404, {"error": "guide not found"})
        data = book.get("pdf") or b""
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % book.get("pdf_name", "guide.pdf"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
        return None

    def _unsubscribe(self, q):
        email = (q.get("e") or [""])[0].strip().lower()
        token = (q.get("t") or [""])[0].strip()
        ok = False
        if email and token and security.verify_token(token) == "unsub:" + email:
            with _lock:
                conn = _db()
                conn.execute("UPDATE subscribers SET unsubscribed=1, confirmed=0 WHERE email=?", (email,))
                conn.commit()
                conn.close()
            ok = True
        title = "You're unsubscribed · pstore" if ok else "Unsubscribe link problem · pstore"
        heading = ("<h1>You're unsubscribed.</h1><p>We've stopped keeping your email for these "
                   "updates. No hard feelings — if you ever want the picks again, just sign up "
                   "again on any niche page.</p>") if ok else \
            ("<h1>That link didn't work.</h1><p>It may have expired, or the address doesn't match "
             "what we have on file. Want out anyway? Email us at %s and we'll fix it by hand.</p>"
             % seo._clean(seo.CONTACT_EMAIL))
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{seo._clean(title)}</title>
<link rel="stylesheet" href="/style.css"></head><body>
<header><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a></header>
<main><section class="card">{heading}
<p class="hint" style="margin-top:14px"><a href="/">← back to pstore</a></p></section></main>
</body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _track_click(self):
        key = "trk|" + security.client_key(self.headers, self._client_ip())
        if not security.TRACK_LIMITER.hit(key):
            return self._send(200, {"ok": True})  # silently drop once throttled
        parsed = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        body = self._body()
        slug = str(body.get("slug") or (q.get("slug") or ["page"])[0]).strip().lower()[:120] or "page"
        source = str(body.get("source") or (q.get("source") or ["page"])[0]).strip()[:40] or "page"
        referrer = str(body.get("referrer") or (q.get("referrer") or [""])[0]).strip()[:250]
        asin = str(body.get("asin") or (q.get("asin") or [""])[0]).strip().upper()[:40]
        content = str(body.get("content") or (q.get("content") or [""])[0]).strip()[:40]
        self._record_click(slug, source, referrer, asin, content)
        return self._send(200, {"ok": True})

    def _page_view(self):
        """Public pageview/event beacon (GET or POST). Records lead-page + public
        site activity (views, promo/countdown/sticky clicks, PDF downloads) —
        rate-limited and IP-anonymized, same privacy posture as /api/track."""
        key = "pv|" + security.client_key(self.headers, self._client_ip())
        if not security.PAGEVIEW_LIMITER.hit(key):
            return self._send(200, {"ok": True})
        parsed = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        body = self._body()
        source = str(body.get("source") or (q.get("source") or [""])[0]).strip()[:40]
        entry = {
            "slug": str(body.get("slug") or (q.get("slug") or ["page"])[0]).strip()[:120] or "page",
            "page": (body.get("page") or (q.get("page") or [""])[0]).strip()[:160] or "/",
            "name": (body.get("name") or (q.get("name") or ["view"])[0]).strip()[:40] or "view",
            "keyword": (body.get("keyword") or (q.get("keyword") or [""])[0]).strip()[:120],
            "source": source or "organic",
            "referrer": str(body.get("referrer") or (q.get("referrer") or [""])[0]).strip()[:200],
        }
        if entry["slug"] in ("page", "") and not entry["keyword"]:
            entry["slug"] = entry["page"].strip("/").split("?")[0][:120] or "page"
        self._record_event(entry)
        return self._send(200, b'{"ok": true}', "application/json")

    def _record_event(self, e):
        e = {k: (v or "").strip()[:k_limit] for k, (v, k_limit) in {
            "slug": (e.get("slug"), 120), "page": (e.get("page"), 160),
            "name": (e.get("name"), 40), "keyword": (e.get("keyword"), 120),
            "source": (e.get("source"), 40), "referrer": (e.get("referrer"), 200)}.items()}
        with _lock:
            conn = _db()
            conn.execute(
                "INSERT INTO events (slug, page, name, keyword, source, referrer) "
                "VALUES (?,?,?,?,?,?)",
                (e["slug"], e["page"], e["name"] or "view",
                 e["keyword"], e["source"], e["referrer"] or ""))
            conn.commit()
            conn.close()

    def _subs_stats(self, conn):
        return {
            "total": conn.execute("SELECT COUNT(*) c FROM subscribers").fetchone()["c"],
            "active": conn.execute(
                "SELECT COUNT(*) c FROM subscribers WHERE confirmed=1 AND unsubscribed=0").fetchone()["c"],
            "unsubscribed": conn.execute(
                "SELECT COUNT(*) c FROM subscribers WHERE unsubscribed=1").fetchone()["c"],
            "emails_sent": conn.execute("SELECT COUNT(*) c FROM sent_emails").fetchone()["c"],
        }

    def _subscribers_json(self):
        with _lock:
            conn = _db()
            stats = self._subs_stats(conn)
            rows = conn.execute("SELECT * FROM subscribers ORDER BY id DESC LIMIT 200").fetchall()
            conn.close()
        return self._send(200, {"stats": stats, "subscribers": [dict(r) for r in rows]})

    # ------------------------------------------------------------------ lead segments
    def _segments_payload(self, keyword=None, limit=500):
        """Aggregate every confirmed/unsubscribed subscriber into lifecycle
        segments. Attribution mirrors the rest of pstore: opens come from the
        email open-pixel (email_events), clicks come from tracked /e/ email
        clicks (clicks.source='email', referrer='<sid>|<idx>').

        Per-subscriber columns:
          opens        - distinct sequence emails opened
          clicks       - distinct outbound email click-throughs (any link)
          clicked_asin - 1 if any of those click-throughs carried a product ASIN
                         (= conversion intent) so the CONVERTED bucket is real
          sent         - how many sequence emails were delivered to this lead"""
        kw = (keyword or "").strip().lower()
        with _lock:
            conn = _db()
            sql = ("SELECT s.id, s.email, s.first_name, s.keyword, s.unsubscribed, "
                   "s.confirmed, "
                   "(SELECT COUNT(*) FROM email_events e WHERE e.subscriber_id=s.id "
                   " AND e.type='open') AS opens, "
                   "(SELECT COUNT(*) FROM clicks c WHERE c.source='email' "
                   " AND c.referrer LIKE s.id || '|%') AS clicks, "
                   "EXISTS(SELECT 1 FROM clicks c WHERE c.source='email' "
                   " AND c.referrer LIKE s.id || '|%' AND c.asin != '') AS clicked_asin, "
                   "(SELECT COUNT(*) FROM sent_emails t WHERE t.subscriber_id=s.id) AS sent "
                   "FROM subscribers s")
            args = ()
            if kw:
                sql += " WHERE lower(s.keyword)=?"
                args = (kw,)
            sql += " ORDER BY s.id DESC LIMIT ?"
            rows = [dict(r) for r in conn.execute(sql, args + (limit,))]
            conn.close()
        return segments.build_report(rows)

    def _subscriber_segment(self, sid):
        """Lifecycle segment for ONE subscriber (mirrors _segments_payload): uses
        the email open-pixel events + tracked email click-throughs to classify
        the lead (hot/warm/cold/converted), so the sequence can act on real
        engagement instead of treating every subscriber the same."""
        with _lock:
            conn = _db()
            row = conn.execute(
                "SELECT s.id, s.email, s.unsubscribed, s.confirmed, "
                "(SELECT COUNT(*) FROM email_events e WHERE e.subscriber_id=s.id"
                " AND e.type='open') AS opens, "
                "(SELECT COUNT(*) FROM clicks c WHERE c.source='email'"
                " AND c.referrer LIKE s.id || '|%') AS clicks, "
                "EXISTS(SELECT 1 FROM clicks c WHERE c.source='email'"
                " AND c.referrer LIKE s.id || '|%' AND c.asin != '') AS clicked_asin "
                "FROM subscribers s WHERE s.id=?", (sid,)).fetchone()
            conn.close()
        if not row:
            return "cold"
        d = dict(row)
        return segments.score_one(d.get("email"), opens=d.get("opens"),
                                  clicks=d.get("clicks"), clicked_asin=d.get("clicked_asin"),
                                  unsubscribed=d.get("unsubscribed"),
                                  confirmed=d.get("confirmed", 1))["segment"]

    def _segments_api(self):
        parsed = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        kw = (q.get("keyword") or [""])[0]
        try:
            limit = int((q.get("limit") or ["500"])[0])
        except (TypeError, ValueError):
            limit = 500
        return self._send(200, self._segments_payload(keyword=kw, limit=limit))

    def _admin_segments(self):
        rep = self._segments_payload()
        counts = rep["counts"]
        stats = rep.get("stats", {})
        def seg_card(name, label, color):
            members = rep["segments"].get(name, [])
            rows = "".join(
                '<tr><td>%s</td><td>%s</td><td class="ct">%d</td><td class="ct">%d</td></tr>'
                % (seo._clean(m.get("email") or ""), seo._clean(m.get("keyword") or ""),
                   m.get("opens") or 0, m.get("clicks") or 0)
                for m in members[:30]) or (
                '<tr><td colspan="4" class="hint">No subscribers here yet.</td></tr>')
            s = stats.get(name, {})
            rates = ('<div class="row"><div class="feature"><h3>%s</h3><p class="hint">open rate</p></div>'
                     '<div class="feature"><h3>%s</h3><p class="hint">click rate (CTR)</p></div>'
                     '<div class="feature"><h3>%s</h3><p class="hint">clicks / lead</p></div></div>'
                     % (str(s.get("open_rate", 0)) + "%",
                        str(s.get("click_rate", 0)) + "%",
                        str(s.get("click_per_lead", 0))))
            return ('<section class="card"><h2 style="color:%s">%s <span class="hint">— %d</span></h2>%s'
                    '<table><thead><tr><th>Email</th><th>Niche</th><th class="ct">Opens</th>'
                    '<th class="ct">Clicks</th></tr></thead><tbody>%s</tbody></table></section>'
                    ) % (color, label, counts.get(name, 0), rates, rows)
        hot = seg_card("hot", "🔥 Hot — opened + clicked", "#b12704")
        warm = seg_card("warm", "🌤 Warm — opened, not clicked", "#e67e22")
        cold = seg_card("cold", "🧊 Cold — no engagement", "#3498db")
        converted = seg_card("converted", "✅ Converted — clicked a product", "#27ae60")
        inactive = seg_card("inactive", "⛔ Inactive — unsubscribed/unconfirmed", "#888")
        rejs = (
            "async function reengage(){const m=document.querySelector('#remsg');const o=document.querySelector('#reout');\n"
            "m.textContent='Sending\u2026';let r,d;try{r=await fetch('/api/segments/reengage',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});d=await r.json();}\n"
            "catch(e){m.textContent='\u2717 Could not reach the server.';return;}\n"
            "m.textContent=(r.ok?'\u2713 ':'\u2717 ')+'sent '+(d.sent||0)+', already sent '+(d.already_sent||0);}")
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lead segments — pstore</title><link rel="stylesheet" href="/style.css">
<meta name="robots" content="noindex,nofollow">
<style>table{{width:100%;border-collapse:collapse;margin-top:8px}}td,th{{text-align:left;padding:6px 8px;
border-bottom:1px solid var(--border);font-size:13px}}.ct{{text-align:right}}
.feature h3{{margin:0;font-size:15px}}</style></head><body>
<header id="top"><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>Lead lifecycle <span>segments.</span></h1>
<p class="tagline">Split subscribers by engagement so the sequence can act per-lead instead of sending everyone the same creep.</p></div>
{self._admin_nav('segments')}</header>
<main>
<section class="card"><h2>📊 Segment mix</h2><div class="row">
<div class="feature"><h3>{counts.get('hot',0)}</h3><p class="hint">hot</p></div>
<div class="feature"><h3>{counts.get('warm',0)}</h3><p class="hint">warm</p></div>
<div class="feature"><h3>{counts.get('cold',0)}</h3><p class="hint">cold</p></div>
<div class="feature"><h3>{counts.get('converted',0)}</h3><p class="hint">converted</p></div>
<div class="feature"><h3>{rep.get('hot_share',0)}%</h3><p class="hint">hot share</p></div>
</div>
<p class="hint">Attribution: opens via email open-pixel; clicks via tracked /e/ outbound links (referrer=&lt;subscriber&gt;|&lt;index&gt;). Next-best email angle per segment is shown under each bucket.</p></section>
{hot}{warm}{cold}{converted}{inactive}
<section class="card"><h2>🚀 Act on segments</h2>
<p class="hint">Push the right next email per group — ownership-button triggered, deduped so nobody gets spammed.</p>
<button class="warm" onclick="reengage()">📨 Re-engage cold leads</button>
<span id="remsg" class="msg"></span><span id="reout" class="msg"></span></section>
<section class="card"><h2>🎯 Recommended next send</h2><ul>
<li><b>Hot:</b> {segments.next_action('hot')['subject_hint']} — {segments.next_action('hot')['sequence_hint']}</li>
<li><b>Warm:</b> {segments.next_action('warm')['subject_hint']} — {segments.next_action('warm')['sequence_hint']}</li>
<li><b>Cold:</b> {segments.next_action('cold')['subject_hint']} — {segments.next_action('cold')['sequence_hint']}</li>
<li><b>Converted:</b> {segments.next_action('converted')['subject_hint']} — {segments.next_action('converted')['sequence_hint']}</li>
</ul></section>
<script>
{rejs}
</script>
</main>
<footer><p>Segments are live-computed from tracked opens and clicks — run the sequence to harvest real engagement data, then re-check this page.</p></footer>
</body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    # ------------------------------------------------------------------ price drops
    def _price_store(self):
        """PriceStore rooted next to the active sqlite DB so tests stay hermit
        and the repo isn't polluted with a stray pricedrops.json."""
        base = DB
        if base.startswith("/tmp"):
            base = os.path.join(os.path.dirname(base),
                                os.path.splitext(os.path.basename(base))[0] + "_pricedrops.json")
        return pricedrop.PriceStore(base)

    def _watched_products(self):
        """Flatten every saved niche's product list into unique ASIN rows with
        title + last-known price (seeds pricedrop baselines)."""
        seen = {}
        for n in self._all_niches():
            for item in (n.get("products") or []):
                asin = str(item.get("asin") or "").strip().upper()
                if not asin:
                    continue
                if asin not in seen:
                    seen[asin] = {"asin": asin, "title": item.get("title"),
                                  "price": item.get("price")}
        return list(seen.values())

    def _pricedrop_api(self):
        store = self._price_store()
        watched = [{"asin": a, "baseline": store.baseline(a)}
                   for a in store.all()]
        return self._send(200, {"watched": watched, "count": len(watched)})

    def _pricedrop_run(self):
        """Re-scrape current prices for every ranked ASIN and report real drops
        against stored baselines. Best-effort and never raises."""
        body = self._body()
        min_pct = body.get("min_pct") or pricedrop.DEFAULT_MIN_DROP_PCT
        try:
            min_pct = float(min_pct)
        except (TypeError, ValueError):
            min_pct = pricedrop.DEFAULT_MIN_DROP_PCT
        store = self._price_store()
        rows = self._watched_products()
        if not rows:
            return self._send(200, {"drops": [], "tracked": 0, "checked": 0,
                                    "error": "no saved niches to watch"})
        fresh = {}
        checked = 0
        for row in rows:
            try:
                items, _src = amazon.search(row["asin"], top=1)
                if items:
                    fresh[row["asin"]] = items[0].get("price")
            except Exception:
                continue
            checked += 1
        result = pricedrop.check(rows, fresh, store=store, min_drop_pct=min_pct)
        result["checked"] = checked
        return self._send(200, result)

    def _admin_pricedrop(self, q):
        js = (
            "async function runCheck(){const m=document.querySelector('#msg');const out=document.querySelector('#out');\n"
            "m.textContent='Scanning prices\u2026';\n"
            "let r,d;try{r=await fetch('/api/pricedrop/run',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});d=await r.json();}\n"
            "catch(e){m.textContent='\u2717 Could not reach the server.';return;}\n"
            "const drops=(d.drops||[]);\n"
            "if(!drops.length){out.innerHTML='<h2>\u2728 Deals right now</h2><p class=\"hint\">No price drops found. Checked '+(d.checked||0)+' products.</p>';}\n"
            "else{out.innerHTML='<h2>\u2728 Deals right now ('+drops.length+')</h2><ul>'+drops.map(x=>"
            "'<li><b>'+x.title+'</b> \u2014 was $'+x.old+', now <b style=\"color:#b12704\">$'+x.new+'</b> (save $'+x.drop+' / '+x.drop_pct+'%)</li>').join('')+'</ul>';}\n"
            "m.textContent='\u2713 Done';}\n"
            "async function sendDrops(){const m=document.querySelector('#msg');\n"
            "m.textContent='Checking + pushing\u2026';let r,d;"
            "try{r=await fetch('/api/pricedrop/send',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});d=await r.json();}"
            "catch(e){m.textContent='\u2717 Could not reach the server.';return;}\n"
            "m.textContent=(r.ok?'\u2713 Emailed '+(d.sent||0)+' hot/converted leads':'')+' (already sent '+(d.already_sent||0)+')';}")
        store = self._price_store()
        allb = store.all()
        rows = "".join(
            '<tr><td>%s</td><td class="ct">%s</td></tr>'
            % (seo._clean(a), pricedrop._fmt(v.get("price")) if v.get("price") is not None else "—")
            for a, v in allb.items()) or (
            '<tr><td colspan="2" class="hint">No prices watched yet — run a check to seed baselines.</td></tr>')
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Price-drop engine — pstore</title><link rel="stylesheet" href="/style.css">
<meta name="robots" content="noindex,nofollow">
<style>table{{width:100%;border-collapse:collapse;margin-top:8px}}td,th{{text-align:left;padding:6px 8px;
border-bottom:1px solid var(--border);font-size:13px}}.ct{{text-align:right}}</style></head><body>
<header id="top"><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>Price-drop <span>deal engine.</span></h1>
<p class="tagline">Watch ranked products, flag real price declines, and push a scarcity 'buy now' email + banner.</p></div>
{self._admin_nav('pricedrop')}</header>
<main>
<section class="card"><h2>🏷 Watched prices</h2>
<p class="hint">Baselines are stored on first sight. A drop of &ge; {pricedrop.DEFAULT_MIN_DROP_PCT}% and &ge; ${pricedrop.DEFAULT_MIN_DROP_ABS} counts as a real deal.</p>
<table><thead><tr><th>ASIN</th><th class="ct">Baseline</th></tr></thead><tbody>{rows}</tbody></table>
<p class="hint" style="margin-top:12px"><b>Run a check</b> to re-scrape current prices and flag who just dropped:</p>
<button class="warm" onclick="runCheck()">🔄 Run price-drop check</button>
<button class="warm" onclick="sendDrops()" style="margin-left:8px">📨 Email hot + converted leads</button>
<span id="msg" class="msg"></span></section>
<section class="card" id="out"><h2>✨ Deals right now</h2><p class="hint">Nothing yet — run a check to see drops.</p></section>
<script>
{js}
</script>
</main>
<footer><p>Deal pushes use scarcity (price + countdown) — pair with the hot segment so you only chase eager buyers.</p></footer>
</body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _sequence_send(self):
        body = self._body()
        dry = bool(body.get("dry_run"))
        niche_kw = str(body.get("keyword") or "").strip().lower()
        try:
            limit = int(body.get("limit") or 0)
        except (TypeError, ValueError):
            limit = 0
        if not mailer.configured():
            return self._send(200, {"ok": False, "error": "SMTP is not configured — set SMTP_HOST/USER/PASSWORD.",
                                    "sent": 0, "errors": 0, "ready": 0, "skipped": 0, "limit": 0})
        with _lock:
            conn = _db()
            if niche_kw:
                subs = conn.execute(
                    "SELECT * FROM subscribers WHERE unsubscribed=0 AND confirmed=1 "
                    "AND sent_index < ? AND lower(keyword)=? ORDER BY id",
                    (mailer.SEQUENCE_LENGTH, niche_kw)).fetchall()
            else:
                subs = conn.execute(
                    "SELECT * FROM subscribers WHERE unsubscribed=0 AND confirmed=1 "
                    "AND sent_index < ? ORDER BY id", (mailer.SEQUENCE_LENGTH,)).fetchall()
            niche_map = {r["keyword"].strip().lower(): r
                         for r in conn.execute("SELECT keyword, products FROM niches")}
            conn.close()
        ready = []
        unready = 0
        ai_copy_cache = {}
        for sub in subs:
            kw = (sub["keyword"] or "").strip()
            item_row = niche_map.get(kw.lower())
            items = json.loads(item_row["products"] or "[]") if item_row else []
            first = sub["first_name"] or ""
            seg = self._subscriber_segment(sub["id"])
            is_converted = seg == "converted"
            idx = (sub["sent_index"] or 0) + 1
            if is_converted:
                # Segment-aware branch: a lead who clicked a product ASIN gets the
                # review + value-ladder upsell follow-up, not the same nurture
                # emails again (Brunson value ladder / Cialdini social proof).
                mail = market_engine.build_converted_followup(kw, items)
            else:
                mail = mailer.next_email(kw, items, idx)
            if not mail:
                unready += 1
                continue
            # AI personalization layer (best-effort, cached per keyword+step so
            # one AI call serves every lead of that niche). Falls back to the
            # deterministic template when no provider is configured (tests).
            ai_key = (kw.lower(), idx, is_converted)
            if ai_key not in ai_copy_cache:
                ai_copy_cache[ai_key] = market_engine._ai_copy(kw, items, "")
            ai_copy = ai_copy_cache.get(ai_key)
            if ai_copy:
                mail["subject"] = ai_copy["subject"]
                mail["body"] = "Hi {{first_name}},\n\n" + ai_copy["body"]
                subv = "ai"
            elif is_converted:
                subv = "review"  # the upsell follow-up carries its own fixed subject
            else:
                subj = self._subject_for(kw, idx, sub["id"], mail["subject"])
                mail["subject"] = subj["subject"]
                subv = subj["variant"]
            pick = market_engine.pick_for_buyers(items)
            asin = (pick or {}).get("asin") or ""
            ready.append((sub["id"], idx, mail, sub["email"], first, kw, asin, subv, is_converted))
        seg_total = sum(1 for r in ready if r[8])
        cap = limit if limit and limit > 0 else mailer.MAX_EMAILS_PER_RUN
        target = ready[:cap] if limit and limit > 0 else ready[:mailer.MAX_EMAILS_PER_RUN]
        eff_limit = min(len(ready), cap)
        if dry:
            return self._send(200, {"ok": True, "dry_run": True, "sent": 0, "errors": 0,
                                    "ready": eff_limit, "skipped": len(ready) - eff_limit,
                                    "keyword": niche_kw or None,
                                    "converted": seg_total,
                                    "limit": cap})
        sent = errors = 0
        for sid, idx, mail, to, to_name, kw, asin, subv, is_converted in target:
            if sent + errors >= mailer.MAX_EMAILS_PER_RUN:
                break
            # tracked affiliate link + open pixel so email actions are attributed
            link_url = mailer.tracked_url(kw, asin, sid, idx) if asin else ""
            pixel_url = mailer.open_pixel_url(kw, asin, sid, idx)
            reply_to = mailer.thread_reply_to(str(sid))
            text = mailer.render_body(mail, to_name=to_name, email=to,
                                      tracked_link=link_url)
            attachments = None
            if idx == 1 and kw:  # hook email carries the lead-magnet PDF
                try:
                    att = self._ebook_attachment(kw)
                    if att:
                        attachments = [att]
                except Exception:
                    attachments = None
            if mailer.send(mail["subject"], text, to, attachments=attachments,
                           pixel_url=pixel_url, reply_to=reply_to):
                sent += 1
                with _lock:
                    conn = _db()
                    new_index = mailer.SEQUENCE_LENGTH if is_converted else idx
                    conn.execute("UPDATE subscribers SET sent_index=? WHERE id=?", (new_index, sid))
                    conn.execute("INSERT INTO sent_emails (subscriber_id, email_index, subject, "
                                 "subject_variant) VALUES (?,?,?,?)",
                                 (sid, idx, mail["subject"], subv))
                    conn.commit()
                    conn.close()
            else:
                errors += 1
            if mailer.EMAIL_SEND_DELAY > 0:
                try:
                    time.sleep(mailer.EMAIL_SEND_DELAY)
                except Exception:
                    pass
        return self._send(200, {"ok": True, "sent": sent, "errors": errors,
                                "ready": eff_limit, "skipped": len(ready) - eff_limit,
                                "keyword": niche_kw or None,
                                "converted": seg_total,
                                "limit": cap})

    # ------------------------------------------------------------- one-off sends
    def _segment_members(self, keyword=None, segments_names=("hot", "converted"),
                         limit=1000):
        """Subscribers in the given lifecycle segments for one niche (or all),
        each with enough data to build a tracked one-off email.
        Returns list of dicts {id, email, keyword, first_name, segment}."""
        rep = self._segments_payload(keyword=keyword, limit=limit)
        out = []
        for name in segments_names:
            for m in rep["segments"].get(name, []):
                out.append({
                    "id": m["id"], "email": m["email"],
                    "keyword": m.get("keyword") or "",
                    "first_name": m.get("first_name") or "",
                    "segment": name,
                })
        return out

    def _log_email_send(self, campaign, sid, kw="", asin=""):
        with _lock:
            conn = _db()
            conn.execute(
                "INSERT OR IGNORE INTO email_sends (campaign, subscriber_id, keyword, asin) "
                "VALUES (?,?,?,?)", (campaign, sid, kw or "", asin or ""))
            conn.commit()
            conn.close()

    def _already_sent(self, campaign, sid, asin=""):
        with _lock:
            conn = _db()
            row = conn.execute(
                "SELECT 1 FROM email_sends WHERE campaign=? AND subscriber_id=? AND asin=?",
                (campaign, sid, asin or "")).fetchone()
            conn.close()
        return row is not None

    def _dispatch_one_off(self, campaign, sub, kw, asin, subject, body_text,
                          attachments=None, pixel_on=True):
        """Send a deduped one-off campaign email to a subscriber through the same
        tracked-link pipeline as the sequence. Returns True when actually sent.
        `campaign` + subscriber + asin form the dedup key (INSERT OR IGNORE)."""
        if self._already_sent(campaign, sub["id"], asin):
            return False
        sid = sub["id"]
        link_url = mailer.tracked_url(kw, asin, sid, 99) if asin else ""
        pixel_url = mailer.open_pixel_url(kw, asin, sid, 99) if pixel_on else ""
        reply_to = mailer.thread_reply_to(str(sid))
        text = mailer.render_body({"body": body_text, "subject": subject},
                                  to_name=sub.get("first_name") or "",
                                  email=sub["email"], tracked_link=link_url)
        ok = mailer.send(subject, text, sub["email"], attachments=attachments,
                         pixel_url=pixel_url, reply_to=reply_to)
        if ok:
            self._log_email_send(campaign, sid, kw, asin)
        return ok

    def _reengage_cold(self, keyword=None, cap=None):
        """Re-engage COLD (confirmed, never opened) subscribers with the plain
        'come back' angle. Deduped to one re-engage send per subscriber ever."""
        cap = cap or 25
        sent = errors = ready = 0
        for sub in self._segment_members(keyword=keyword,
                                         segments_names=("cold",), limit=2000):
            if sent + errors >= cap:
                break
            kw = sub["keyword"]
            item_row = self._niche_items(kw)
            pick = market_engine.pick_for_buyers(item_row) if item_row else None
            asin = (pick or {}).get("asin") or ""
            act = segments.next_action("cold")
            body = ("Hi {{first_name}},\n\n"
                    "Noticed you grabbed the {kw} guide but never opened it — "
                    "maybe it landed in the wrong folder, or life got busy. "
                    "No hard sell here; the two things people actually find useful:\n"
                    "1. The {kw} cheat-sheet (free).\n"
                    "2. One honest 'best pick' link when you're ready.\n\n"
                    "Still here? Just reply and I'll resend the goodies — otherwise,"
                    " no more email from me.\n\n— {{your_name}}").format(kw=kw)
            if self._dispatch_one_off("reengage", sub, kw, asin,
                                      act["subject_hint"], body):
                sent += 1
            else:
                # already sent / skipped -> don't count into the cap budget
                ready += 1
        return {"ok": True, "sent": sent, "errors": errors,
                "already_sent": ready, "keyword": keyword or None}

    def _pricedrop_send(self, keyword=None, min_pct=None, cap=None):
        """Auto-push a 'price dropped' email to HOT + CONVERTED subscribers of any
        niche that just had a real price drop. Deduped per (subscriber, ASIN).

        Returns {ok, drops, candidates, sent, already_sent, keyword}.
        Best-effort and never raises."""
        cap = cap or 50
        body = self._body()
        kw_filter = (keyword or (body or {}).get("keyword") or "").strip().lower()
        try:
            min_pct = float(min_pct if min_pct is not None
                            else (body or {}).get("min_pct") or pricedrop.DEFAULT_MIN_DROP_PCT)
        except (TypeError, ValueError):
            min_pct = pricedrop.DEFAULT_MIN_DROP_PCT

        store = self._price_store()
        rows = self._watched_products()
        if not rows:
            return {"ok": True, "drops": [], "candidates": 0, "sent": 0,
                    "already_sent": 0, "keyword": kw_filter or None}
        fresh = {}
        for row in rows:
            try:
                items, _src = amazon.search(row["asin"], top=1)
                if items:
                    fresh[row["asin"]] = items[0].get("price")
            except Exception:
                continue
        result = pricedrop.check(rows, fresh, store=store, min_drop_pct=min_pct)
        drops = result["drops"]
        if not drops:
            return {"ok": True, "drops": [], "candidates": 0, "sent": 0,
                    "already_sent": 0, "keyword": kw_filter or None}

        # map each dropped ASIN back to its owning niche keyword
        asin_niche = {}
        for n in self._all_niches():
            for item in (n.get("products") or []):
                a = str(item.get("asin") or "").strip().upper()
                if a:
                    asin_niche[a] = n["keyword"]

        sent = candidates = already = errors = 0
        all_members = self._segment_members(keyword=kw_filter or None,
                                            segments_names=("hot", "converted"),
                                            limit=5000)
        for sub in all_members:
            if sent + errors >= cap:
                break
            kw = sub["keyword"]
            niche_drops = [d for d in drops if asin_niche.get(d["asin"], "").strip().lower()
                           == kw.strip().lower()]
            if kw_filter and kw.strip().lower() != kw_filter:
                continue
            if not niche_drops:
                continue
            pick_asin = next((d["asin"] for d in niche_drops), "")
            mail = pricedrop.drop_email(niche_drops, base_url=os.environ.get("PSTORE_URL", ""))
            if not mail["subject"]:
                continue
            candidates += 1
            if self._dispatch_one_off("pricedrop:" + pick_asin if pick_asin else "pricedrop",
                                      sub, kw, pick_asin,
                                      mail["subject"], mail["text"]):
                sent += 1
            else:
                already += 1
        return {"ok": True, "drops": drops, "candidates": candidates,
                "sent": sent, "already_sent": already, "errors": errors,
                "keyword": kw_filter or None}

    def _niche_items(self, keyword):
        """Product list for a saved niche keyword, or []."""
        kw = (keyword or "").strip().lower()
        for n in self._all_niches():
            if n["keyword"].strip().lower() == kw:
                return n.get("products") or []
        return []

    def _ebook_attachment(self, keyword):
        """Return the per-niche lead-magnet PDF as an (filename, bytes) tuple for
        attaching to email #1, or None when the ebook isn't available."""
        try:
            import ebook
            book = ebook.build_ebook(keyword)
            data = book.get("pdf")
            if not data:
                return None
            return (book.get("pdf_name") or "%s-ebook.pdf" % keyword, data)
        except Exception:
            return None

    def _subs_table(self, rows):
        def state(r):
            if r["unsubscribed"]:
                return "<span class='badge'>out</span>"
            if not r["confirmed"]:
                return "<span class='badge'>unconfirmed</span>"
            return "<span class='badge'>active</span>"
        trs = "".join(
            "<tr><td>%s</td><td>%s</td><td>%d / %d</td><td>%s</td><td>%s</td></tr>"
            % (seo._clean(r["email"]), seo._clean(r["keyword"] or "—"),
               r["sent_index"] or 0, mailer.SEQUENCE_LENGTH,
               state(r), seo._clean(r["created_at"] or ""))
            for r in rows)
        return trs

    def _email_sequence_preview(self, keyword):
        for n in self._all_niches():
            if n["keyword"].lower() == keyword.lower():
                items = n["products"]
                break
        else:
            return '<p class="hint">No saved products for that niche yet — mine one on the dashboard first.</p>'
        seq = market_engine.build_email_sequence(keyword, items)
        if not seq:
            return '<p class="hint">No sequence for this niche right now (needs at least one product).</p>'
        cards = "".join(
            '<div class="sub"><h3>%s</h3><p class="key" style="white-space:pre-wrap">%s</p></div>'
            % (seo._clean(m["name"]), seo._clean(mailer.render_body(m, email="you@example.com"))[:1600])
            for m in seq)
        return cards

    # ------------------------------------------------------------- email studio
    def _studio_recipients(self, spec):
        """Resolve a recipients spec into a deduped list of recipient dicts.
        spec: {sub_ids:[int], emails:"a@b, c@d", selector:{niche, segment},
               include_unconfirmed:bool} — sub_ids + selector + typed emails
        are unioned; the same address is never included twice."""
        sub_ids = spec.get("sub_ids") or []
        emails = spec.get("emails") or ""
        selector = spec.get("selector") or {}
        include_unconfirmed = bool(spec.get("include_unconfirmed"))
        out, seen = [], set()

        def _push(email, kind, sid=0, kw="", fn=""):
            e = (email or "").strip().lower()
            if not e or not _EMAIL_RE.match(e) or e in seen:
                return
            seen.add(e)
            out.append({"kind": kind, "id": int(sid or 0), "email": e,
                        "keyword": (kw or "").strip(), "first_name": (fn or "").strip()})

        kwf = (selector.get("niche") or "").strip().lower()
        seg = (selector.get("segment") or "").strip()
        ids = [int(x) for x in sub_ids if str(x or "").lstrip("-").isdigit()]
        if ids:
            with _lock:
                conn = _db()
                ph = ",".join("?" * len(ids))
                sql = ("SELECT id,email,keyword,first_name,confirmed,unsubscribed "
                       "FROM subscribers WHERE id IN (%s)" % ph)
                if not include_unconfirmed:
                    sql += " AND confirmed=1 AND unsubscribed=0"
                for r in conn.execute(sql, ids):
                    _push(r["email"], "sub", r["id"], r["keyword"], r["first_name"])
                conn.close()
        rows = []
        if kwf or seg:
            with _lock:
                conn = _db()
                sql = ("SELECT s.id, s.email, s.keyword, s.first_name, s.confirmed, s.unsubscribed, "
                       "(SELECT COUNT(*) FROM email_events e WHERE e.subscriber_id=s.id AND e.type='open') AS opens, "
                       "(SELECT COUNT(*) FROM clicks c WHERE c.source='email' AND c.referrer LIKE s.id || '|%') AS clicks, "
                       "EXISTS(SELECT 1 FROM clicks c WHERE c.source='email' "
                       " AND c.referrer LIKE s.id || '|%' AND c.asin!='') AS clicked_asin "
                       "FROM subscribers s")
                args = ()
                if kwf:
                    sql += " WHERE lower(s.keyword)=?"
                    args = (kwf,)
                rows = [dict(r) for r in conn.execute(sql, args)]
                conn.close()
            if seg:
                rep = segments.build_report([dict(r) for r in rows])
                members = rep["segments"].get(seg, [])
            else:
                members = rows
            for m in members:
                if (m.get("confirmed") or include_unconfirmed) and not m.get("unsubscribed"):
                    _push(m.get("email"), "sub", m.get("id"), m.get("keyword") or "",
                          m.get("first_name") or "")
        for line in str(emails or "").splitlines():
            for one in re.split(r"[;,\s]+", line):
                if one.strip():
                    _push(one, "custom")
        return out

    def _compose_recipient(self, spec, recip, ai_cache=None):
        """Build the sendable {mail, asin, kw, idx, campaign, attachments,
        converted} for ONE recipient from a compose spec, or None."""
        t = spec.get("type") or "sequence"
        kw = (spec.get("niche") or recip.get("keyword") or "").strip().lower()
        items = []
        if kw:
            items = self._niche_items(kw)
        idx = 99
        if t == "sequence":
            try:
                idx = int(spec.get("step") or 1)
            except (TypeError, ValueError):
                idx = 1
            idx = max(1, min(idx, mailer.SEQUENCE_LENGTH))
            mail = mailer.next_email(kw, items, idx) if (kw and items) else None
        elif t == "converted":
            mail = market_engine.build_converted_followup(kw, items) if (kw and items) else None
        elif t == "reengage":
            act = segments.next_action("cold")
            mail = {"subject": (act.get("subject_hint") or
                                "Still thinking about the %s guide?" % kw),
                    "body": ("Hi {{first_name}},\n\nNoticed you grabbed the %s guide but never "
                             "opened it — maybe it landed in the wrong folder, or life got busy. "
                             "No hard sell here; the two things people actually find useful:\n"
                             "1. The %s cheat-sheet (free).\n2. One honest 'best pick' link when "
                             "you're ready.\n\nStill here? Just reply and I'll resend the goodies "
                             "— otherwise, no more email from me.\n\n— {{your_name}}") % (kw, kw)}
        elif t == "custom":
            subject = (spec.get("subject") or "").strip() or "From pstore"
            body = (spec.get("body") or "").strip()
            if not body:
                return None
            mail = {"subject": subject, "body": body}
        else:
            return None
        if not mail or not (mail.get("body") or "").strip():
            return None
        if t in ("sequence", "converted") and kw and items:
            key = (kw, idx, t == "converted")
            ai_cache = ai_cache if ai_cache is not None else {}
            if key not in ai_cache:
                ai_cache[key] = market_engine._ai_copy(kw, items, "")
            ai = ai_cache.get(key)
            if ai and ai.get("subject") and ai.get("body"):
                mail = {"subject": ai["subject"], "body": "Hi {{first_name}},\n\n" + ai["body"]}
        pick = market_engine.pick_for_buyers(items) if items else None
        asin = (pick or {}).get("asin") or ""
        attachments = None
        if spec.get("options", {}).get("attach_pdf") and kw:
            try:
                att = self._ebook_attachment(kw)
                if att:
                    attachments = [att]
            except Exception:
                attachments = None
        return {"mail": mail, "asin": asin, "kw": kw, "idx": idx,
                "campaign": "studio:%s:%s:%s" % (t, kw or "custom", idx),
                "attachments": attachments, "converted": t == "converted"}

    def _studio_preview(self, spec):
        fake = {"kind": "custom", "id": 0, "email": "you@example.com",
                "keyword": (spec.get("niche") or "").strip(), "first_name": "Jane"}
        comp = self._compose_recipient(spec, fake, {})
        if not comp:
            return {"ok": False,
                    "error": "Nothing to preview — pick a niche with products (sequence/converted) "
                             "or fill the subject + body (custom)."}
        opts = spec.get("options") or {}
        link_url = mailer.tracked_url(comp["kw"], comp["asin"], 0, comp["idx"]) \
            if (opts.get("tracked_links") and comp["asin"]) else ""
        text = mailer.render_body(comp["mail"], to_name="Jane",
                                  email="you@example.com", tracked_link=link_url)
        return {"ok": True, "subject": comp["mail"]["subject"], "body": text,
                "asin": comp["asin"], "idx": comp["idx"]}

    def _dispatch_studio(self, spec, recipients, dry=False):
        """Send a composed campaign to the given recipients (or count it when
        dry). Returns {ok, sent, skipped, errors, recipients, dry_run}."""
        opts = spec.get("options") or {}
        dedup = bool(opts.get("dedup"))
        tracked = bool(opts.get("tracked_links"))
        pixel = bool(opts.get("open_pixel"))
        advance = bool(opts.get("advance"))
        ai_cache = {}
        sent = skipped = errors = 0
        for r in recipients:
            comp = self._compose_recipient(spec, r, ai_cache)
            if not comp:
                skipped += 1
                continue
            cid = int(r.get("id") or 0)
            if dedup and r.get("kind") == "sub" and cid and \
                    self._already_sent(comp["campaign"], cid, comp["asin"]):
                skipped += 1
                continue
            link_url = mailer.tracked_url(comp["kw"], comp["asin"], cid, comp["idx"]) \
                if (tracked and comp["asin"]) else ""
            pixel_url = mailer.open_pixel_url(comp["kw"], comp["asin"], cid, comp["idx"]) \
                if pixel else ""
            reply_to = mailer.thread_reply_to(str(cid)) if cid else ""
            text = mailer.render_body(comp["mail"], to_name=r.get("first_name") or "",
                                      email=r["email"], tracked_link=link_url)
            ok = True
            if not dry:
                ok = mailer.send(comp["mail"]["subject"], text, r["email"],
                                 attachments=comp["attachments"], pixel_url=pixel_url,
                                 reply_to=reply_to)
            if ok:
                sent += 1
                if not dry and dedup and r.get("kind") == "sub" and cid:
                    self._log_email_send(comp["campaign"], cid, comp["kw"], comp["asin"])
                if not dry and advance and r.get("kind") == "sub" and cid:
                    ni = mailer.SEQUENCE_LENGTH if comp["converted"] else comp["idx"]
                    with _lock:
                        conn = _db()
                        conn.execute("UPDATE subscribers SET sent_index=? WHERE id=?", (ni, cid))
                        conn.commit()
                        conn.close()
                if mailer.EMAIL_SEND_DELAY > 0:
                    try:
                        time.sleep(mailer.EMAIL_SEND_DELAY)
                    except Exception:
                        pass
            else:
                errors += 1
        return {"ok": True, "sent": sent, "skipped": skipped, "errors": errors,
                "recipients": len(recipients), "dry_run": bool(dry)}

    def _studio_send(self, body):
        spec = dict(body.get("spec") or {})
        spec["options"] = dict(body.get("options") or {})
        dry = bool(spec["options"].get("dry_run") or body.get("dry_run"))
        schedule_at = (body.get("schedule_at") or "").strip()
        # Only an actual immediate send needs a live SMTP path. Dry runs and
        # future-scheduled campaigns are legitimate without it (the outbox loop
        # reports the send error per row when it finally fires).
        if not mailer.configured() and not dry and not schedule_at:
            return self._send(200, {"ok": False, "error": "SMTP is not configured.",
                                    "sent": 0, "errors": 0, "recipients": 0, "dry_run": dry})
        recipients = self._studio_recipients(body.get("recipients") or {})
        if not recipients:
            return self._send(200, {"ok": False, "error": "No valid recipients selected.",
                                    "sent": 0, "errors": 0, "recipients": 0, "dry_run": dry})
        if schedule_at and not dry:
            normalized = schedule_at.replace("T", " ")
            if len(normalized) == 16:
                normalized += ":00"
            with _lock:
                conn = _db()
                cur = conn.execute(
                    "INSERT INTO outbox (spec, recipients, scheduled_at, status) "
                    "VALUES (?,?,?,?)",
                    (json.dumps(spec), json.dumps(recipients), normalized, "scheduled"))
                conn.commit()
                conn.close()
            return self._send(200, {"ok": True, "scheduled": True,
                                    "outbox_id": cur.lastrowid,
                                    "recipients": len(recipients)})
        res = self._dispatch_studio(spec, recipients, dry=dry)
        return self._send(200, res)

    def _inbox_row(self, mid):
        try:
            mid = int(mid or 0)
        except (TypeError, ValueError):
            return None
        with _lock:
            conn = _db()
            row = conn.execute(
                "SELECT m.*, s.email AS subscriber_email FROM mailbox_messages m "
                "LEFT JOIN subscribers s ON s.id=m.subscriber_id WHERE m.id=?", (mid,)).fetchone()
            conn.close()
        return dict(row) if row else None

    def _inbox_list(self, limit=200):
        with _lock:
            conn = _db()
            rows = [dict(r) for r in conn.execute(
                "SELECT m.*, s.email AS subscriber_email FROM mailbox_messages m "
                "LEFT JOIN subscribers s ON s.id=m.subscriber_id "
                "WHERE m.mailbox='inbox' ORDER BY m.id DESC LIMIT ?", (limit,))]
            unread = conn.execute(
                "SELECT COUNT(*) c FROM mailbox_messages WHERE mailbox='inbox' "
                "AND status='unread'").fetchone()["c"]
            conn.close()
        return {"ok": True, "inbox": rows, "inbox_unread": unread}

    def _inbox_set(self, mid, status):
        try:
            mid = int(mid or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad message id"}
        if status not in ("unread", "read", "archived", "trash"):
            return {"ok": False, "error": "invalid status"}
        with _lock:
            conn = _db()
            conn.execute("UPDATE mailbox_messages SET status=? WHERE id=?", (status, mid))
            conn.commit()
            conn.close()
        return {"ok": True}

    def _inbox_delete(self, mid):
        try:
            mid = int(mid or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad message id"}
        with _lock:
            conn = _db()
            conn.execute("DELETE FROM mailbox_messages WHERE id=?", (mid,))
            conn.commit()
            conn.close()
        return {"ok": True}

    def _inbox_reply(self, mid, body):
        body = (body or "").strip()
        if not body:
            return {"ok": False, "error": "Reply body is empty."}
        msg = self._inbox_row(mid)
        if not msg:
            return {"ok": False, "error": "Message not found."}
        to = (msg.get("from_addr") or "").strip()
        if not to:
            return {"ok": False, "error": "No sender address to reply to."}
        subject = msg.get("subject") or ""
        if subject and not subject.lower().startswith("re:"):
            subject = "Re: " + subject
        reply_to = ""
        if msg.get("subscriber_id"):
            reply_to = mailer.thread_reply_to(str(msg["subscriber_id"]))
        if mailer.send(subject, body, to, reply_to=reply_to,
                       in_reply_to=(msg.get("message_id") or "")):
            with _lock:
                conn = _db()
                conn.execute("UPDATE mailbox_messages SET status='read', "
                             "replied_at=datetime('now') WHERE id=?", (int(mid),))
                conn.commit()
                conn.close()
            sid = int(msg.get("subscriber_id") or 0)
            if sid:
                self._log_email_send("reply:%d" % int(mid), sid, "", "")
            return {"ok": True, "sent": True, "subject": subject or "Re: (no subject)",
                    "to": to}
        return {"ok": False, "error": "SMTP send failed."}

    def _studio_api(self, q):
        if self.command == "GET":
            with _lock:
                conn = _db()
                subs = [dict(r) for r in conn.execute(
                    "SELECT id,email,keyword,first_name,confirmed,unsubscribed,"
                    "sent_index,created_at FROM subscribers ORDER BY id DESC LIMIT 500")]
                outbox = [dict(r) for r in conn.execute(
                    "SELECT * FROM outbox ORDER BY id DESC LIMIT 60")]
                sent_log = [dict(r) for r in conn.execute(
                    "SELECT * FROM sent_emails ORDER BY id DESC LIMIT 60")]
                oneoff_log = [dict(r) for r in conn.execute(
                    "SELECT * FROM email_sends ORDER BY id DESC LIMIT 60")]
                inbox = [dict(r) for r in conn.execute(
                    "SELECT m.*, s.email AS subscriber_email FROM mailbox_messages m "
                    "LEFT JOIN subscribers s ON s.id=m.subscriber_id "
                    "WHERE m.mailbox='inbox' ORDER BY m.id DESC LIMIT 200")]
                inbox_unread = conn.execute(
                    "SELECT COUNT(*) c FROM mailbox_messages WHERE mailbox='inbox' "
                    "AND status='unread'").fetchone()["c"]
                conn.close()
            niches = [n["keyword"] for n in self._all_niches()]
            live_kw = {n["keyword"].strip().lower() for n in self._all_niches()}
            blocked = [dict(r) for r in subs if (r["confirmed"] and not r["unsubscribed"]
                       and (r.get("keyword") or "").strip().lower() not in live_kw)]
            seg_rep = self._segments_payload()
            segmap = {str(m.get("id")): name
                      for name, members in seg_rep["segments"].items()
                      for m in members if m.get("id")}
            state = None
            raw_state = _get_setting(AUTOSEND_STATE_KEY)
            if raw_state:
                try:
                    state = json.loads(raw_state)
                except Exception:
                    state = None
            return self._send(200, {
                "ok": True, "subscribers": subs, "niches": niches,
                "segments": seg_rep["counts"], "segmap": segmap,
                "outbox": outbox,
                "sent_log": sent_log, "oneoff_log": oneoff_log, "blocked": blocked,
                "inbox": inbox, "inbox_unread": inbox_unread,
                "inbox_configured": mailer.inbound_configured(),
                "config": _autosend_cfg(), "autosend_state": state,
                "sequence_length": mailer.SEQUENCE_LENGTH,
                "smtp_configured": mailer.configured(),
                "smtp_host": mailer.SMTP_HOST or ""})
        body = self._body()
        action = (body.get("action") or "send").strip()
        if action == "send":
            return self._studio_send(body)
        if action == "preview":
            return self._send(200, self._studio_preview(body.get("spec") or {}))
        if action == "config":
            hours = body.get("hours")
            enabled = body.get("enabled")
            limit = body.get("limit")
            if hours is not None:
                if isinstance(hours, (list, tuple)):
                    raw = [str(h) for h in hours]
                else:
                    raw = re.split(r"[,\s]+", str(hours))
                cleaned = sorted(set(int(h) for h in raw
                                     if str(h).strip().lstrip("-").isdigit()
                                     and 0 <= int(h) <= 23))
                _set_setting("autosend.hours", ",".join(map(str, cleaned)))
            if enabled is not None:
                _set_setting("autosend.enabled",
                             "1" if str(enabled).lower() in ("1", "true", "yes", "on") else "0")
            if limit is not None:
                try:
                    _set_setting("autosend.limit", str(max(int(limit), 0)))
                except (TypeError, ValueError):
                    pass
            return self._send(200, {"ok": True, "config": _autosend_cfg()})
        if action == "cancel":
            try:
                oid = int(body.get("id") or 0)
            except (TypeError, ValueError):
                oid = 0
            with _lock:
                conn = _db()
                conn.execute("UPDATE outbox SET status='canceled' "
                             "WHERE id=? AND status IN ('scheduled','sending')", (oid,))
                conn.commit()
                conn.close()
            return self._send(200, {"ok": True})
        if action == "inbox_poll":
            res = _inbox_tick()
            if res.get("ok"):
                res.update(self._inbox_list())
            return self._send(200, res)
        if action == "inbox_list":
            return self._send(200, self._inbox_list())
        if action == "inbox_get":
            return self._send(200, {"ok": True,
                                    "message": self._inbox_row(body.get("id") or 0)})
        if action == "inbox_set":
            return self._send(200, self._inbox_set(body.get("id") or 0,
                                                   (body.get("status") or "").strip()))
        if action == "inbox_delete":
            return self._send(200, self._inbox_delete(body.get("id") or 0))
        if action == "inbox_reply":
            return self._send(200, self._inbox_reply(body.get("id") or 0,
                                                     body.get("body") or ""))
        return self._send(400, {"ok": False, "error": "unknown action"})

    def _admin_emails(self, q):
        nav = self._admin_nav('emails')
        page = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Email Studio — pstore</title><link rel="stylesheet" href="/style.css">
<meta name="robots" content="noindex,nofollow">
<style>
.studiox{display:grid;grid-template-columns:1fr;gap:18px}
.pillrow{display:flex;flex-wrap:wrap;gap:8px;margin:4px 0 12px}
.pill{border:1.5px solid var(--line,#eee);background:#fff;border-radius:999px;padding:7px 13px;font-size:13px;cursor:pointer}
.pill b{color:var(--accent,#ff6b2c)}
.pill.on{background:var(--accent,#ff6b2c);border-color:var(--accent,#ff6b2c);color:#fff}
.pill.on b{color:#fff}
.subchip{display:flex;align-items:center;gap:8px;border:1.5px solid var(--line,#eee);border-radius:12px;padding:8px 11px;font-size:13px;background:#fff;margin:4px 0}
.subchip input{width:auto;height:auto}
.subchip .mail{font-weight:600;font-size:13px}
.subchip .kw{color:var(--muted,#888);font-size:11.5px}
.subchip .st{margin-left:auto;font-size:11px;color:var(--muted,#888)}
.chipsrow{max-height:240px;overflow:auto;border:1px solid var(--line,#eee);border-radius:14px;padding:8px 10px}
.switch{position:relative;display:inline-flex;align-items:center;gap:9px;cursor:pointer;font-size:13.5px}
.switch input{position:absolute;opacity:0;width:0;height:0}
.switch .trk{display:inline-block;width:38px;height:21px;background:#ccc;border-radius:999px;transition:.18s;position:relative;flex:none}
.switch .trk:after{content:"";position:absolute;top:2px;left:2px;width:17px;height:17px;background:#fff;border-radius:50%;transition:.18s;box-shadow:0 1px 3px rgba(0,0,0,.25)}
.switch input:checked+.trk{background:var(--accent,#ff6b2c)}
.switch input:checked+.trk:after{left:19px}
.optgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px 18px;margin-top:8px}
.hourgrid{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.hourgap{border:1.5px solid #ddd;border-radius:9px;font-size:11.5px;padding:5px 8px;cursor:pointer;background:#fff;min-width:44px;text-align:center}
.hourgap.on{background:var(--accent2,#7c5cff);border-color:var(--accent2,#7c5cff);color:#fff}
.sec-tag{font-size:11.5px;font-weight:800;letter-spacing:.6px;color:var(--muted,#888);text-transform:uppercase;margin:2px 0 8px}
.compose-box{border:1.5px dashed var(--line,#eee);border-radius:16px;padding:16px;margin-top:12px}
pre.preview{white-space:pre-wrap;word-break:break-word;background:#fbf7ef;border:1px solid var(--line,#eee);border-radius:12px;padding:12px;font-size:13px;margin-top:8px}
.netmsg{font-weight:600}
.tmpls{display:flex;flex-wrap:wrap;gap:8px}
.tmpls label{border:1.5px solid var(--line,#eee);border-radius:12px;padding:8px 12px;font-size:13px;cursor:pointer;display:flex;gap:7px;align-items:center;background:#fff}
.tmpls input{width:auto;height:auto}
.tmpls label.on{border-color:var(--accent,#ff6b2c);background:#fff5ee}
.bigbtn{width:100%;padding:14px;font-size:16px;font-weight:800;border-radius:14px;border:0;background:linear-gradient(135deg,#ff6b2c,#ff873c);color:#fff;cursor:pointer}
.bigbtn:disabled{opacity:.55;cursor:wait}
fieldset{border:1.5px solid var(--line,#eee);border-radius:16px;padding:14px 16px;margin:0}
legend{font-weight:800;font-size:13px;padding:0 8px;color:var(--accent,#ff6b2c)}
.whenrow{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-top:10px}
.bump{background:#fff0e6;border:1.5px solid #ffd2b8;color:#a4421a;border-radius:12px;padding:9px 12px;font-size:12.5px;margin-bottom:12px}
.tabbar{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 2px}
.tab{border:1.5px solid var(--line,#eee);background:#fff;border-radius:999px;padding:8px 16px;font-size:13.5px;font-weight:700;cursor:pointer;color:var(--muted,#888)}
.tab.on{background:var(--accent,#ff6b2c);border-color:var(--accent,#ff6b2c);color:#fff}
.ibadge{display:inline-grid;place-items:center;min-width:19px;height:19px;padding:0 5px;border-radius:999px;background:#fff;color:var(--accent,#ff6b2c);font-size:11.5px;font-weight:800;margin-left:4px;vertical-align:middle}
.tab.on .ibadge{background:#fff;color:var(--accent,#ff6b2c)}
.mailrow{display:flex;align-items:flex-start;gap:10px;border:1.5px solid var(--line,#eee);border-radius:12px;padding:10px 12px;font-size:13px;background:#fff;margin:5px 0;cursor:pointer}
.mailrow.unread{border-color:#ffb98f;background:#fff6ee}
.mailrow .mfrom{font-weight:700;font-size:13px}
.mailrow .msubj{color:var(--text,#2b2233);font-size:13px}
.mailrow .msnip{color:var(--muted,#888);font-size:12px;margin-top:1px}
.mailrow .mact{margin-left:auto;display:flex;gap:6px;flex:none}
.mailrow .mact button{flex:none}
.mailrow .mdate{color:var(--muted,#888);font-size:11.5px;flex:none}
.mailrow .mstatus{flex:none;font-size:11px}
.ibox-head{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
#msg-view pre{white-space:pre-wrap;word-break:break-word;background:#fbf7ef;border:1px solid var(--line,#eee);border-radius:12px;padding:12px;font-size:13px;max-height:320px;overflow:auto}
/* min-width:0 lets flex/grid rows shrink and wrap instead of blowing the
   page out to the sum of their controls' intrinsic widths on narrow screens */
main.studiox,.studiox section,.studiox fieldset,.studiox .row,.studiox .switch{min-width:0}
@media (max-width: 640px){
 main.studiox{padding:12px 12px 44px}
 header{padding:18px 14px 6px}
 .hero{padding:14px 2px 6px}
 .hero h1{font-size:27px}
 .tagline{font-size:14px}
 section.card{padding:14px;border-radius:18px}
 fieldset{padding:11px 12px}
 legend{font-size:12px}
 .subchip{align-items:flex-start;padding:9px 10px}
 .subchip .st{flex:none;margin-left:4px}
 .compose-box{padding:12px}
 pre.preview{font-size:12.5px}
 .bigbtn{padding:12px;font-size:15px}
 .whenrow{gap:10px}
 .switch{font-size:13px}
 input,select,textarea{max-width:100%}
 nav{margin-top:12px}
 nav a{padding:7px 12px;font-size:12.5px}
}
</style>
</head><body>
<header id="top"><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>Email <span>Studio.</span></h1>
<p class="tagline">Compose once — send to one subscriber, a segment, or any address list, now or on a schedule. Every link is signed, tracked and unsubscribable.</p></div>
__NAV__
</header>
<div class="tabbar">
 <button class="tab on" data-tab="send" onclick="tab('send')">✍️ Compose &amp; send</button>
 <button class="tab" data-tab="inbox" onclick="tab('inbox')">📥 Inbox <span class="ibadge" id="ibadge" style="display:none"></span></button>
</div>
<main class="studiox" id="main-send">
 <section class="card"><h2>🕹 Sender &amp; auto-schedule</h2>
  <div class="row">
   <div class="feature"><h3 id="smtp-state">…</h3><p class="hint">SMTP</p></div>
   <div class="feature"><h3 id="as-hrs">—</h3><p class="hint">daily send hours (UTC)</p></div>
   <div class="feature"><h3 id="as-last">never</h3><p class="hint">last auto-send</p></div>
  </div>
  <div class="row">
   <label style="flex-direction:row;align-items:center;gap:8px" class="switch">
     <input type="checkbox" id="as-on"><span class="trk"></span> Auto-send enabled</label>
   <label>Per-run cap
     <input id="as-limit" type="number" min="0" value="50" style="width:90px"></label>
   <button id="as-save" class="warm">Save schedule config</button>
   <span id="as-msg" class="msg"></span>
  </div>
  <p class="sec-tag">Pick the UTC hour chips the 5-step sequence auto-sends on</p>
  <div class="hourgrid" id="hourchips"></div>
 </section>

 <section class="card"><h2>✍️ Compose &amp; send</h2>

  <fieldset><legend>1 · Who gets it</legend>
   <div class="row">
     <label>Niche filter
       <select id="f-niche"><option value="">All niches</option></select></label>
     <label>Segment filter
       <select id="f-seg">
         <option value="">All active</option>
         <option value="hot">🔥 Hot — opened + clicked</option>
         <option value="warm">🌤 Warm — opened</option>
         <option value="cold">🧊 Cold — no engagement</option>
         <option value="converted">✅ Converted — clicked a product</option>
         <option value="inactive">⛔ Inactive</option>
       </select></label>
     <label>Search
       <input id="f-q" type="search" placeholder="filter by email…" style="min-width:200px"></label>
   </div>
   <div class="row">
     <button id="selall" class="btn ghost">☑ Select all shown</button>
     <button id="selnone" class="btn ghost">☐ Clear selection</button>
     <label class="switch" style="margin-left:auto"><input type="checkbox" id="o-unconf"><span class="trk"></span> show unconfirmed</label>
   </div>
   <div id="subs-wrap" class="chipsrow"></div>
   <label style="margin-top:10px;display:block">Or any address(es) — one per line
     <textarea id="f-raw" rows="3" placeholder="anyone@gmail.com&#10;another@proton.me" style="width:100%"></textarea></label>
   <p class="netmsg" id="rcount" style="margin-top:8px">0 recipients selected</p>
  </fieldset>

  <fieldset style="margin-top:14px"><legend>2 · What to send</legend>
   <div class="tmpls" id="tmpls">
     <label class="on"><input type="radio" name="tmpl" value="sequence" checked> Sequence step</label>
     <label><input type="radio" name="tmpl" value="converted"> Converted follow-up</label>
     <label><input type="radio" name="tmpl" value="reengage"> Re-engage cold</label>
     <label><input type="radio" name="tmpl" value="custom"> Custom email</label>
   </div>
   <div class="row" id="seq-opts" style="margin-top:10px">
     <label>Niche
       <select id="c-niche"></select></label>
     <label>Step
       <select id="c-step">
         <option value="1">1 · Hook + lead magnet</option>
         <option value="2">2 · Proof &amp; pain points</option>
         <option value="3">3 · Best picks</option>
         <option value="4">4 · Urgency (price/countdown)</option>
         <option value="5">5 · Follow-up + review ask</option>
       </select></label>
   </div>
   <div id="cust-opts" style="display:none" class="compose-box">
     <label style="display:block">Subject (uses <code>{{first_name}}</code> / <code>{{your_name}}</code>)
       <input id="c-subj" type="text" placeholder="Your subject line…" style="width:100%"></label>
     <label style="display:block;margin-top:8px">Body (plain text, placeholders supported)
       <textarea id="c-body" rows="7" placeholder="Hi {{first_name}},&#10;&#10;…&#10;&#10;— {{your_name}}" style="width:100%"></textarea></label>
   </div>
   <p class="sec-tag" style="margin-top:12px">Live preview</p>
   <pre class="preview" id="prevbox">Pick a template to preview.</pre>
  </fieldset>

  <fieldset style="margin-top:14px"><legend>3 · Delivery options</legend>
   <div class="optgrid">
     <label class="switch"><input type="checkbox" id="o-tl" checked><span class="trk"></span> Tracked affiliate link</label>
     <label class="switch"><input type="checkbox" id="o-px" checked><span class="trk"></span> Open-tracking pixel</label>
     <label class="switch"><input type="checkbox" id="o-pdf"><span class="trk"></span> Attach lead-magnet PDF</label>
     <label class="switch"><input type="checkbox" id="o-dd"><span class="trk"></span> Dedup (never re-send same campaign)</label>
     <label class="switch"><input type="checkbox" id="o-adv" checked><span class="trk"></span> Count toward sequence progress</label>
     <label class="switch"><input type="checkbox" id="o-dry"><span class="trk"></span> Dry run — nothing actually sent</label>
   </div>
  </fieldset>

  <div class="whenrow">
   <label class="switch"><input type="radio" name="when" value="now" checked onchange="onWhen()"><span class="trk"></span> Send now</label>
   <label class="switch"><input type="radio" name="when" value="sched" onchange="onWhen()"><span class="trk"></span> Schedule</label>
   <input type="datetime-local" id="w-dt" style="display:none">
   <span class="hint" id="dt-hint" style="display:none">times are UTC</span>
  </div>
  <div class="row" style="margin-top:12px">
   <button class="bigbtn" id="bigbtn" disabled>Loading…</button>
  </div>
  <p id="outmsg" class="netmsg" style="margin-top:10px"></p>
 </section>

 <section class="card"><h2>🗓 Scheduled sends</h2>
  <div class="table-wrap"><table class="plain">
   <thead><tr><th>#</th><th class="ct">When (UTC)</th><th class="ct">To</th><th>Status</th><th>Result</th><th></th></tr></thead>
   <tbody id="outbox-body"><tr><td colspan="6" class="hint">Loading…</td></tr></tbody></table></div>
 </section>

 <section class="card"><h2>🕘 Recent activity</h2>
  <div id="blocked-wrap"></div>
  <div class="table-wrap"><table class="plain">
   <thead><tr><th>When</th><th>Kind</th><th>Subject / campaign</th><th>To</th></tr></thead>
   <tbody id="log-body"><tr><td colspan="4" class="hint">Loading…</td></tr></tbody></table></div>
 </section>
</main>
<main class="studiox" id="main-inbox" style="display:none">
 <section class="card"><h2>📥 Inbox — replies to your mail</h2>
  <div class="ibox-head">
   <button class="btn warm" onclick="pollInbox()">↻ Check now</button>
   <span class="hint" id="ib-net">…</span>
   <span class="hint" id="ib-count" style="margin-left:auto"></span>
  </div>
  <div id="inbox-list" class="chipsrow"></div>
  <div id="msg-view" class="compose-box" style="display:none">
   <h3 id="mv-subj">—</h3>
   <p class="hint" id="mv-meta"></p>
   <pre id="mv-body"></pre>
   <label style="display:block;margin-top:10px">Reply
     <textarea id="mv-reply" rows="5" placeholder="Write your reply, then send — it lands in the customer's inbox and threads right back here." style="width:100%"></textarea></label>
   <div class="row" style="margin-top:8px">
     <button class="bigbtn" id="mv-send" onclick="sendReply()">↩ Send reply</button>
   </div>
   <p id="mv-msg" class="netmsg" style="margin-top:8px"></p>
  </div>
 </section>
</main>
<footer><p>Times are UTC. Every email carries a signed unsubscribe link and List-Unsubscribe header; tracked links go through <code>/e/</code> and record source=email clicks. Replies sent to your tagged address land on the Inbox tab and map back to the subscriber who wrote.</p></footer>
__TOTOP__
<script>
const $=id=>document.getElementById(id);
let DATA=null, SEL=new Set(), EM="", FILT={niche:"",seg:"",q:""};
let lastSeq=null; // {keyword,step} cache key for preview
function esc(s){return (s==null?"":String(s)).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function tmpl(){const t=document.querySelector('input[name="tmpl"]:checked');return t?t.value:"sequence";}
function opts(){return {tracked_links:$("o-tl").checked,open_pixel:$("o-px").checked,attach_pdf:$("o-pdf").checked,dedup:$("o-dd").checked,advance:$("o-adv").checked,dry_run:$("o-dry").checked};}
function spec(){const t=tmpl();return {type:t,niche:(t==="custom")?"":$("c-niche").value,step:parseInt($("c-step").value||"1",10),subject:$("c-subj").value,body:$("c-body").value};}
function recSpec(){const raw=($("f-raw").value||"").split("\\n").map(s=>s.trim()).filter(Boolean).join("\\n");return {sub_ids:[...SEL].map(Number),emails:raw||"",selector:{},include_unconfirmed:$("o-unconf").checked};}
function visibleSubs(){const q=FILT.q.toLowerCase();return (DATA.subscribers||[]).filter(s=>{if(FILT.niche&&(s.keyword||"").toLowerCase()!==FILT.niche.toLowerCase())return false;if(FILT.seg&&(DATA.segmap||{})[String(s.id)]!==FILT.seg)return false;if(!$("o-unconf").checked&&(!s.confirmed||s.unsubscribed))return false;if(q&&!(s.email||"").toLowerCase().includes(q))return false;return true;});}
function renderSubs(){const list=visibleSubs();$("subs-wrap").innerHTML=list.length?list.map(s=>`<label class="subchip"><input type="checkbox" data-id="${s.id}" ${SEL.has(s.id)?"checked":""} onchange="onChip(${s.id},this.checked)"><span><span class="mail">${esc(s.email)}</span><br><span class="kw">${esc(s.keyword||"—")}</span></span><span class="st">${s.unsubscribed?"unsubscribed":(!s.confirmed?"unconfirmed":"step "+(s.sent_index||0)+"/"+(DATA.sequence_length||5))}</span></label>`).join(""):`<p class="hint">No subscribers match.</p>`;count();}
function onChip(id,on){if(on)SEL.add(id);else SEL.delete(id);count();}
function count(){const n=SEL.size+ (($("f-raw").value||"").split("\\n").map(s=>s.trim()).filter(Boolean).length);$("rcount").textContent=`${n} recipient${n===1?"":"s"} selected (${SEL.size} subscriber${SEL.size===1?"":"s"}${($("f-raw").value||"").trim()?" + typed addresses":""})`;updBtn();}
function updBtn(){const dry=$("o-dry").checked,when=document.querySelector('input[name="when"]:checked').value;const n=SEL.size+$("f-raw").value.trim()?1:0;$("bigbtn").textContent=dry?"👁 Preview sending (nobody is emailed)":(when==="sched"?"📅 Schedule this send":"📨 Send now");$("bigbtn").disabled=false;}
function onWhen(){const sched=document.querySelector('input[name="when"]:checked').value==="sched";$("w-dt").style.display=sched?"":"none";$("dt-hint").style.display=sched?"":"none";updBtn();}
function fillNiches(keep){const sel=DATA.niches||[];const cur=keep||$("c-niche").value;for(const el of [$("f-niche"),$("c-niche")]){el.innerHTML=`<option value="">${el.id==="f-niche"?"All niches":"No niche (custom only)"}</option>`+sel.map(k=>`<option value="${esc(k)}">${esc(k)}</option>`).join("");}if(cur)$("c-niche").value=cur;}
function fillSegs(){if(!DATA)return;const c=DATA.segments||{};const map={"hot":c.hot||0,"warm":c.warm||0,"cold":c.cold||0,"converted":c.converted||0,"inactive":c.inactive||0};const sel=$("f-seg");for(const o of sel.options){if(o.value&&map[o.value]!==undefined)o.textContent=o.value==="inactive"?"⛔ Inactive ("+map[o.value]+")":o.textContent.split("(")[0].trim()+" ("+map[o.value]+")";}}
function renderNow(){const n=DATA.niches||[];$("smtp-state").textContent=DATA.smtp_configured?"✅ "+DATA.smtp_host:"✱ SMTP not configured";const cfg=DATA.config;const hours=cfg.hours||[];$("as-hrs").textContent=hours.length?hours.join(","):"off";$("as-on").checked=cfg.enabled;fillHourChips(hours);const st=DATA.autosend_state;$("as-last").textContent=st&&st.last_run?st.status+" @"+st.last_run+(st.sent?" ("+st.sent+" sent)":""):"never ran";$("as-limit").value=cfg.limit;fillHourChips(hours);}
function fillHourChips(hours){const hset=new Set(hours);$("hourchips").innerHTML=Array.from({length:24},(_,h)=>`<span class="hourgap ${hset.has(h)?"on":""}" data-h="${h}" onclick="togHour(${h})">${String(h).padStart(2,"0")}</span>`).join("");}
function togHour(h){const el=document.querySelector(`[data-h='${h}']`);el.classList.toggle("on");}
function saveCfg(){const hours=[...document.querySelectorAll(".hourgap.on")].map(e=>+e.dataset.h);fetch("/api/mail",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"config",hours,hours,enabled:$("as-on").checked,limit:$("as-limit").value})}).then(r=>r.json()).then(d=>{$("as-msg").textContent="Saved — "+(d.config.hours.length?"sends at "+d.config.hours.join(", ")+" UTC":"autosend off")+". Adjust the sender interval next tick.";setTimeout(()=>$("as-msg").textContent="",4000);});}
async function preview(){const s0=spec();s0.options=opts();const p=await fetch("/api/mail",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"preview",spec:s0})});const d=await p.json();$("prevbox").textContent=d.ok?`Subject: ${esc(d.subject)}\n\n${d.body}\n\n── preview (tracked link ${d.asin?"on":""} · open pixel ${opts().open_pixel?"on":"off"})`:"⚠ "+d.error;}
function onSpec(){preview();}
function onCust(){const c=tmpl()==="custom";$("cust-opts").style.display=c?"":"none";if(c)onSpec();}
function renderOutbox(){const rows=DATA.outbox||[];$("outbox-body").innerHTML=rows.length?rows.map(o=>{let res="";try{const r=JSON.parse(o.result||"null");if(r)res=`${r.recipients??""} → sent ${r.sent??0} / err ${r.errors??0}`;}catch(e){}return `<tr><td>${o.id}</td><td class="ct">${esc(o.scheduled_at||"now")}</td><td class="ct">${(o.recipients?JSON.parse(o.recipients).length:"0")}</td><td><span class="badge">${esc(o.status)}</span></td><td>${esc(res||(o.result||""))}</td><td>${o.status==="scheduled"||o.status==="sending"?`<button class="btn ghost" onclick="cancel(${o.id})">Cancel</button>`:""}</td></tr>`;}).join(""):'<tr><td colspan="6" class="hint">Nothing scheduled.</td></tr>';}
function cancel(id){fetch("/api/mail",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"cancel",id})}).then(r=>r.json()).then(()=>load());}
function renderLog(){const rows=[];for(const s of (DATA.sent_log||[]))rows.push([s.sent_at,"seq "+s.email_index,s.subject,"sub #"+s.subscriber_id]);for(const o of (DATA.oneoff_log||[]))rows.push([o.sent_at,"one-off",o.campaign,o.keyword+" → sub #"+o.subscriber_id]);$("log-body").innerHTML=rows.slice(0,40).map(r=>`<tr><td>${esc(r[0])}</td><td>${r[1]}</td><td>${esc(r[2])}</td><td>${esc(r[3])}</td></tr>`).join("")||'<tr><td colspan="4" class="hint">No sends yet.</td></tr>';}
function renderBlocked(){const b=DATA.blocked||[];if(!b.length){$("blocked-wrap").innerHTML="";return;}$("blocked-wrap").innerHTML=`<div class="bump">⚠ ${b.length} active subscriber(s) never receive sequence mail because their niche isn't saved with products: ${b.map(s=>esc(s.email)+" ("+esc(s.keyword)+")").join(" · ")}. Mine that keyword on the dashboard, or send them a custom email right here.</div>`;}
function renderInbox(){const list=DATA.inbox||[];const unread=DATA.inbox_unread||0;const b=$("ibadge");b.style.display=unread?"":"none";if(unread)b.textContent=unread;if(DATA.inbox_configured){$("ib-net").textContent="IMAP bridge on"+(DATA.smtp_host&&DATA.smtp_configured?" · "+DATA.smtp_host:"");}else{$("ib-net").textContent="No IMAP mailbox configured — replies aren't captured yet (set IMAP_HOST/USER/PASSWORD or forward to /api/cron/inbox).";}$("ib-count").textContent=list.length+" message"+(list.length===1?"":"s")+" · "+unread+" unread";$("inbox-list").innerHTML=list.length?list.map(m=>{const who=(m.from_name&&m.from_name!==m.from_addr)?esc(m.from_name)+" &lt;"+esc(m.from_addr)+"&gt;":esc(m.from_addr);const sub=(m.subscriber_id?(" · linked sub #"+m.subscriber_id+(m.subscriber_email?" — "+esc(m.subscriber_email):"")):"");return `<div class="mailrow ${m.status==="unread"?"unread":""}"><span style="flex:1" onclick="openMsg(${m.id})"><span class="mfrom">${who}</span><br><span class="msubj">${esc(m.subject||"(no subject)")}</span><br><span class="msnip">${esc((m.text||"").replace(/\s+/g," ").slice(0,90))}</span></span><span class="mstatus">${esc(m.status)}${m.replied_at?" · replied":""}</span><span class="mdate">${esc((m.sent_at||"").slice(0,16))}</span><span class="mact"><button class="btn ghost" title="Archive" onclick="event.stopPropagation();msgAction(${m.id},'archived')">🗂</button><button class="btn ghost" title="Delete" onclick="event.stopPropagation();removeMsg(${m.id})">🗑</button></span></div>`;}).join(""):'<p class="hint">No messages yet — reply to one of your sends and it lands here.</p>';}
function openMsg(id){const m=(DATA.inbox||[]).find(x=>x.id===id);if(!m)return;$("mv-subj").textContent=m.subject||"(no subject)";$("mv-meta").textContent="From: "+esc(m.from_addr)+(m.subscriber_email?(" — "+esc(m.subscriber_email)):"")+" · "+esc(m.sent_at||"")+" · linked to sub #"+(m.subscriber_id||"—");$("mv-body").textContent=m.text||"(no plain-text body)";$("msg-view").style.display="";$("msg-view").scrollIntoView({behavior:"smooth",block:"start"});window._curMsg=id;if(m.status!=="read")msgAction(id,"read",true);}
function msgAction(id,status,silent){fetch("/api/mail",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"inbox_set",id,status})}).then(r=>r.json()).then(()=>{if(!silent)load();});}
function removeMsg(id){fetch("/api/mail",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"inbox_delete",id})}).then(r=>r.json()).then(()=>load());}
function pollInbox(){const btn=document.querySelector("#main-inbox .btn.warm");const old=btn.textContent;btn.textContent="Checking…";btn.disabled=true;fetch("/api/mail",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"inbox_poll"})}).then(r=>r.json()).then(d=>{if(d.error){$("ib-net").textContent="⚠ "+d.error;return;}DATA.inbox=d.inbox||DATA.inbox;if(d.inbox_unread!==undefined)DATA.inbox_unread=d.inbox_unread;renderInbox();}).finally(()=>{btn.textContent=old;btn.disabled=false;});}
async function sendReply(){const id=window._curMsg;const body=$("mv-reply").value;if(!body.trim()){alert("Write something first.");return;}const btn=$("mv-send");btn.disabled=true;const r=await fetch("/api/mail",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action:"inbox_reply",id,body})});const d=await r.json();$("mv-msg").textContent=d.error?"✗ "+d.error:(d.sent?"↩ Reply sent to "+d.to+". It'll thread back here.":"");if(d.sent){$("mv-reply").value="";load();}btn.disabled=false;}
function tab(name){$("main-send").style.display=name==="send"?"":"none";$("main-inbox").style.display=name==="inbox"?"":"none";document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("on",t.dataset.tab===name));if(name==="inbox")renderInbox();}
function renderAll(){renderNow();renderSubs();renderOutbox();renderLog();renderBlocked();renderInbox();fillNiches();fillSegs();count();}
async function sendIt(){const pay={action:"send",spec:spec(),options:opts(),recipients:recSpec()};const when=document.querySelector('input[name="when"]:checked').value;let dt="";if(when==="sched"){dt=$("w-dt").value;if(!dt){alert("Pick a schedule time first.");return;}pay.schedule_at=dt;}const btn=$("bigbtn");btn.disabled=true;$("outmsg").textContent="Working…";const r=await fetch("/api/mail",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(pay)});const d=await r.json();if(d.scheduled){$("outmsg").textContent=`📅 Scheduled #${d.outbox_id} — ${d.recipients} recipient(s) at ${dt} UTC.`;}else if(d.error){$("outmsg").textContent="✗ "+d.error;}else if(d.dry_run){$("outmsg").textContent=`👁 Dry run: ${d.sent} ready · ${d.skipped} skipped · ${d.errors} errors · of ${d.recipients} recipient(s).`;}else{$("outmsg").textContent=`📨 Sent ${d.sent} · skipped ${d.skipped} · errors ${d.errors} · of ${d.recipients}.`;}load();setTimeout(()=>{btn.disabled=false;updBtn();},700);}
document.addEventListener("DOMContentLoaded",async()=>{const r=await fetch("/api/mail");DATA=await r.json();renderAll();
$("f-niche").addEventListener("change",e=>{FILT.niche=e.target.value;renderSubs();});
$("f-seg").addEventListener("change",e=>{FILT.seg=e.target.value;renderSubs();});
$("f-q").addEventListener("input",e=>{FILT.q=e.target.value;renderSubs();});
$("f-raw").addEventListener("input",count);
$("o-unconf").addEventListener("change",renderSubs);
$("selall").addEventListener("click",()=>{visibleSubs().forEach(s=>SEL.add(s.id));renderSubs();});
$("selnone").addEventListener("click",()=>{SEL.clear();renderSubs();});
$("c-niche").addEventListener("change",onSpec);
$("c-step").addEventListener("change",onSpec);
$("c-subj").addEventListener("input",onSpec);
$("c-body").addEventListener("input",onSpec);
for(const l of document.querySelectorAll(".tmpls label"))l.addEventListener("click",()=>{document.querySelectorAll(".tmpls label").forEach(x=>x.classList.remove("on"));l.classList.add("on");onCust();});
$("as-save").addEventListener("click",saveCfg);
$("bigbtn").addEventListener("click",sendIt);});
</script>
</body></html>"""
        page = page.replace("__NAV__", nav).replace("__TOTOP__", _TOTOP)
        return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

    def _ebook_for(self, keyword):
        keyword = (keyword or "").strip()
        if not keyword:
            return None
        if keyword in _EBOOKS:
            return _EBOOKS[keyword]
        niche = None
        for n in self._all_niches():
            if n["keyword"].lower() == keyword.lower():
                niche = n
                break
        if not niche or not niche.get("products"):
            return None
        book = ebook_mod.build_ebook(keyword)
        if len(_EBOOKS) >= 20:
            try:
                _EBOOKS.pop(next(iter(_EBOOKS)))
            except StopIteration:
                pass
        _EBOOKS[keyword] = book
        return book

    def _admin_ebooks(self, q):
        niches = [n["keyword"] for n in self._all_niches()]
        keyword = (q.get("keyword") or [""])[0].strip()
        book = None
        if keyword:
            book = self._ebook_for(keyword)
        elif niches:
            keyword = niches[0]
            book = self._ebook_for(keyword)
        opts = "".join('<option value="%s"%s>%s</option>' % (seo._clean(k),
                        ' selected' if k == keyword else "", seo._clean(k)) for k in niches)
        content = ""
        if book:
            content = ('<div class="book-preview"><p class="hint" style="margin-top:-4px">%s</p>'
                       '%s<p style="margin-top:12px"><a class="btn warm" download '
                       'href="/admin/ebooks/pdf?keyword=%s">Download PDF ⬇</a></p></div>'
                       % (seo._clean(book["title"]), book["html_preview"],
                          urllib.parse.quote(keyword)))
        else:
            content = '<p class="hint">Choose a niche and generate its free guide PDF.</p>'
        cached = "".join('<a class="chip" href="/admin/ebooks?keyword=%s">%s</a>'
                         % (urllib.parse.quote(k), seo._clean(k)) for k in _EBOOKS)
        _providers = ai.providers()
        _active = ai.active_provider()
        prov_opts = "".join(
            '<option value="%s"%s>%s%s</option>'
            % (p["name"], ' selected' if p["name"] == _active else "",
               seo._clean(p["label"]), " · in use" if p["active"] else "")
            for p in _providers)
        _models_opt = "".join(
            '<option value="%s">%s</option>' % (seo._clean(m), seo._clean(m))
            for p in _providers for m in p.get("models") or [])
        _models_json = json.dumps({p["name"]: p.get("models") or []
                                   for p in _providers})
        _models_json = (_models_json.replace("&", "&amp;").replace("<", "&lt;")
                        .replace(">", "&gt;"))
        if _active:
            for p in _providers:
                if p["name"] == _active:
                    ai_status_line = ('<span style="color:#1e8e3e"><b>%s</b> (%s)</span> — %s'
                                      % (seo._clean(p["label"]), seo._clean(p["model"]),
                                         "runtime key (resets on redeploy)"
                                         if p["source"] == "runtime" else "server env key"))
                    break
        else:
            ai_status_line = ('<span style="color:#d64545">not configured</span> — templates '
                              + "are being used. Add a key below to enable AI copy.")
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ebooks — pstore</title><link rel="stylesheet" href="/style.css">
<meta name="robots" content="noindex,nofollow">
</head><body>
<header id="top"><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>AI ebook <span>lead magnets.</span></h1>
<p class="tagline">Turn any saved niche into a designed PDF guide in one click — pure stdlib renderer, template copy (AI when a key is set).</p></div>
{self._admin_nav('ebooks')}
</header>
<main>
<section class="card"><h2>📕 Generate a guide</h2>
<form class="row" method="get" action="/admin/ebooks">
  <label>Niche <select name="keyword">{opts}</select></label>
  <button class="warm" type="submit">Generate</button>
</form>
{content}
</section>
<section class="card"><h2>🤖 AI provider</h2>
<p class="hint" style="margin-top:-4px">Free models: OpenCode Zen, Mistral Experiment tier and NVIDIA NIM — plus your OpenAI key. Paste a key, hit <b>Test</b>, then <b>Use</b> to generate with it.</p>
<form class="ai-form" onsubmit="return false">
  <div class="row">
    <label>Provider <select id="ai-provider" name="provider" style="min-width:230px">{prov_opts}</select></label>
    <label>API key <input id="ai-key" name="api_key" type="password" autocomplete="off" placeholder="paste key — never stored on disk"></label>
  </div>
  <div class="row">
    <label>Model <input id="ai-model" name="model" list="ai-models" placeholder="e.g. kimi-k2.5-free"></label>
    <label>Base URL <input id="ai-base" name="base_url" placeholder="optional override"></label>
  </div>
  <datalist id="ai-models">{_models_opt}</datalist>
  <div class="row" style="gap:8px">
    <button class="warm" type="button" id="ai-test">Test key ✓</button>
    <button type="button" id="ai-models-btn">Load models</button>
    <button type="button" id="ai-use">Use this provider</button>
  </div>
  <p class="ai-msg" id="ai-out" style="margin-top:10px">Status: {ai_status_line}</p>
  <p class="hint" style="margin-top:8px">Keys live in this running process only and reset on redeploy. For permanence set the matching env var instead: <code>AI_API_KEY</code> / <code>OPENCODE_API_KEY</code> / <code>MISTRAL_API_KEY</code> / <code>NVIDIA_API_KEY</code>.</p>
</form>
</section>
<section class="card"><h2>🗂 Recently generated</h2>
<div class="chips">{cached if cached else '<span class="hint">Nothing generated yet.</span>'}</div>
<p class="hint" style="margin-top:10px">
AI status: {"<b>configured</b> (%s · %s)" % (seo._clean(_active), seo._clean(ai.model_for(_active))) if _active else "not configured — using the deterministic template copy."}
</p></section>
</main>
<footer><p>PDFs are generated server-side and never contain affiliate links — plain honest guide content that pairs with your review pages.</p></footer>
<script>
(function(){{
  var sel=document.getElementById('ai-provider'), key=document.getElementById('ai-key'),
      model=document.getElementById('ai-model'), base=document.getElementById('ai-base'),
      dl=document.getElementById('ai-models'), out=document.getElementById('ai-out');
  var MODELS = {_models_json};
  function fillModels(prov){{
    dl.innerHTML='';
    var ids = MODELS[prov] || [];
    ids.forEach(function(id){{ var o=document.createElement('option'); o.value=id; dl.appendChild(o); }});
    model.placeholder = ids.length ? 'pick a free model ('+ids.length+' available)'
                                   : 'e.g. kimi-k2.5-free';
  }}
  fillModels(sel.value);
  sel.addEventListener('change', function(){{ fillModels(sel.value); model.value=''; }});
  function say(t, ok){{ out.textContent=t; out.style.color = ok ? '#1e8e3e' : '#d64545'; }}
  function str(v){{ return (v==null ? '' : String(v)).trim(); }}
  function payload(){{
    return {{provider: sel.value, api_key: str(key.value), model: str(model.value), base_url: str(base.value)}};
  }}
  async function post(path, extra){{
    var body = payload(); for (var k in (extra||{{}})) body[k]=extra[k];
    var r = await fetch(path, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(body)}});
    return r.json();
  }}
  document.getElementById('ai-test').onclick = async function(){{
    if(!str(key.value)){{ say('✗ Paste a key first.', false); return; }}
    out.textContent='Testing…'; out.style.color='#666';
    var d = await post('/api/ai/test');
    if(d.ok) say('✓ Valid — '+d.provider+' / '+d.model+' replied "'+d.reply+'" in '+d.latency_ms+'ms', true);
    else say('✗ Failed: '+(d.error||'unknown error'), false);
  }};
  document.getElementById('ai-models-btn').onclick = async function(){{
    if(!str(key.value)){{ say('✗ Paste a key first, then load models.', false); return; }}
    out.textContent='Loading models…'; out.style.color='#666';
    var d = await post('/api/ai/models');
    var fresh = d.models||[];
    var known = MODELS[sel.value] || [];
    var merged = [];
    known.forEach(function(m){{ if(merged.indexOf(m) < 0) merged.push(m); }});
    fresh.forEach(function(m){{ if(merged.indexOf(m) < 0) merged.push(m); }});
    MODELS[sel.value] = merged;
    dl.innerHTML='';
    merged.forEach(function(id){{ var o=document.createElement('option'); o.value=id; dl.appendChild(o); }});
    model.placeholder = 'pick from '+merged.length+' models';
    say(fresh.length ? ('Loaded '+fresh.length+' live models.');
                      : 'No live models returned — showing known free models.', true);
  }};
  document.getElementById('ai-use').onclick = async function(){{
    if(!str(key.value)){{ say('✗ Paste a key first.', false); return; }}
    out.textContent='Saving…'; out.style.color='#666';
    var d = await post('/api/ai/config');
    if(d.ok){{ say('✓ '+d.provider+' is now active ('+d.model+'). Regenerating cache…', true);
              setTimeout(function(){{location.reload();}}, 1000); }}
    else say('✗ '+(d.error||'failed'), false);
  }};
}})();
</script>
{_TOTOP}
</body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _ebook_pdf(self, q):
        keyword = (q.get("keyword") or [""])[0].strip()
        book = self._ebook_for(keyword)
        if not book:
            return self._send(404, {"error": "ebook not found"})
        data = book["pdf"]
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % book["pdf_name"])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
        return None

    def _analytics_api(self):
        """JSON twin of /admin/analytics so a non-owner analyst role gets the
        same numbers programmatically."""
        with _lock:
            conn = _db()
            subs = self._subs_stats(conn)
            total = conn.execute("SELECT COUNT(*) c FROM clicks").fetchone()["c"]
            top_slugs = [dict(r) for r in conn.execute(
                "SELECT slug, COUNT(*) c FROM clicks GROUP BY slug ORDER BY c DESC LIMIT 12").fetchall()]
            top_sources = [dict(r) for r in conn.execute(
                "SELECT source, COUNT(*) c FROM clicks GROUP BY source ORDER BY c DESC LIMIT 8").fetchall()]
            top_products = [dict(r) for r in conn.execute(
                "SELECT asin, slug, COUNT(*) c FROM clicks WHERE asin != '' "
                "GROUP BY asin, slug ORDER BY c DESC LIMIT 10").fetchall()]
            recent = [dict(r) for r in conn.execute(
                "SELECT slug, source, referrer, created_at FROM clicks "
                "ORDER BY id DESC LIMIT 15").fetchall()]
            views = conn.execute(
                "SELECT COUNT(*) c FROM events WHERE name='view'").fetchone()["c"]
            event_breakdown = [dict(r) for r in conn.execute(
                "SELECT name, COUNT(*) c FROM events GROUP BY name ORDER BY c DESC LIMIT 12").fetchall()]
            event_pages = [dict(r) for r in conn.execute(
                "SELECT page, COUNT(*) c FROM events WHERE name='view' "
                "GROUP BY page ORDER BY c DESC LIMIT 10").fetchall()]
            month_rows = conn.execute(
                "SELECT month, orders, earnings FROM earnings_records "
                "ORDER BY month DESC LIMIT 24").fetchall()
            conn.close()
        est = earnings.estimate(total, "")
        return self._send(200, {
            "subs": subs, "totals": {"clicks": total, "niches_clicked": len(top_slugs),
                                     "views": views},
            "top_slugs": top_slugs, "top_sources": top_sources,
            "top_products": top_products, "recent": recent,
            "events": {"breakdown": event_breakdown, "pages": event_pages},
            "earnings": {"estimate": est,
                         "records": [{"month": r["month"], "orders": r["orders"],
                                      "earnings": r["earnings"]} for r in month_rows]},
        })

    def _admin_analytics(self):
        with _lock:
            conn = _db()
            stats = self._subs_stats(conn)
            total = conn.execute("SELECT COUNT(*) c FROM clicks").fetchone()["c"]
            top_slugs = conn.execute(
                "SELECT slug, COUNT(*) c FROM clicks GROUP BY slug ORDER BY c DESC LIMIT 12").fetchall()
            top_sources = conn.execute(
                "SELECT source, COUNT(*) c FROM clicks GROUP BY source ORDER BY c DESC LIMIT 8").fetchall()
            top_products = conn.execute(
                "SELECT asin, slug, COUNT(*) c FROM clicks WHERE asin != '' "
                "GROUP BY asin, slug ORDER BY c DESC LIMIT 10").fetchall()
            recent = conn.execute("SELECT * FROM clicks ORDER BY id DESC LIMIT 15").fetchall()
            views = conn.execute(
                "SELECT COUNT(*) c FROM events WHERE name='view'").fetchone()["c"]
            event_breakdown = conn.execute(
                "SELECT name, COUNT(*) c FROM events GROUP BY name ORDER BY c DESC LIMIT 12").fetchall()
            event_pages = conn.execute(
                "SELECT page, COUNT(*) c FROM events WHERE name='view' "
                "GROUP BY page ORDER BY c DESC LIMIT 10").fetchall()
            month_rows = conn.execute(
                "SELECT month, orders, earnings FROM earnings_records "
                "ORDER BY month DESC LIMIT 24").fetchall()
            conn.close()
        # Earnings estimate from the recorded click volume + config, plus the
        # real orders/earnings logged straight from the Associates dashboard.
        est = earnings.estimate(total, "")
        real = [{"month": r["month"], "orders": r["orders"], "earnings": r["earnings"]}
                for r in month_rows]
        real_summary = earnings.monthly_summary(real)
        earn_note = ("tuned" if (earnings._runtime.get("commission_pct") or
                                 earnings._runtime.get("avg_order") or
                                 earnings._runtime.get("order_rate"))
                     else "defaults (%.1f%% on $%.0f AOV @ %.2f%% order rate) — tune below"
                     % (earnings.commission_pct(""), earnings.avg_order(""),
                        earnings.order_rate("") * 100))
        slug_rows = "".join(
            "<tr><td>%s</td><td class='ct'>%s</td></tr>" % (seo._clean(r["slug"]), r["c"])
            for r in top_slugs) or "<tr><td colspan='2' class='hint'>No clicks yet.</td></tr>"
        src_rows = "".join(
            "<tr><td>%s</td><td>%d</td></tr>" % (seo._clean(r["source"]), r["c"])
            for r in top_sources)
        product_rows = "".join(
            "<tr><td>%s</td><td>%s</td><td class='ct'>%s</td></tr>"
            % (seo._clean(r["asin"]), seo._clean(r["slug"]), r["c"])
            for r in top_products) or "<tr><td colspan='3' class='hint'>No product clicks yet — they appear as soon as a pick gets tapped (data-asin beacon).</td></tr>"
        recent_rows = "".join(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (seo._clean(r["slug"]), seo._clean(r["source"]), seo._clean(r["referrer"] or "—"),
               seo._clean(r["created_at"] or "—"))
            for r in recent) or "<tr><td colspan='4' class='hint'>No clicks yet — they're recorded when visitors tap Amazon links on the pages.</td></tr>"
        ev_rows = "".join(
            "<tr><td>%s</td><td class='ct'>%d</td></tr>"
            % (seo._clean(r["name"]), r["c"]) for r in event_breakdown
        ) or "<tr><td colspan='2' class='hint'>No pageview events yet.</td></tr>"
        ev_page_rows = "".join(
            "<tr><td>%s</td><td class='ct'>%d</td></tr>"
            % (seo._clean(r["page"] or "—"), r["c"]) for r in event_pages
        ) or "<tr><td colspan='2' class='hint'>Views appear as pages load.</td></tr>"
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Analytics — pstore</title><link rel="stylesheet" href="/style.css">
<meta name="robots" content="noindex,nofollow">
</head><body>
<header id="top"><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>Click <span>analytics.</span></h1>
<p class="tagline">Privacy-first: IPs are stored as one-way hashes, never raw. See which niches and sources actually earn clicks.</p></div>
{self._admin_nav('analytics')}
</header>
<main>
<section class="card"><h2>📊 At a glance</h2>
<div class="row" style="align-items:stretch">
  <div class="feature"><h3>{total}</h3><p class="hint">total clicks</p></div>
  <div class="feature"><h3>{len(top_slugs)}</h3><p class="hint">niches clicked</p></div>
  <div class="feature"><h3>{views}</h3><p class="hint">page views (leads + site)</p></div>
  <div class="feature"><h3>{stats['active']}</h3><p class="hint">active subscribers</p></div>
  <div class="feature"><h3>{stats['emails_sent']}</h3><p class="hint">emails sent</p></div>
</div></section>
<section class="card"><h2>💰 Earnings & conversion</h2>
<div class="row" style="align-items:stretch">
  <div class="feature"><h3>${est['commission_est']:.2f}</h3><p class="hint">est. commission this period</p></div>
  <div class="feature"><h3>{est['orders_est']:.0f}</h3><p class="hint">est. orders @ {est['order_rate']*100:.1f}%</p></div>
  <div class="feature"><h3>${real_summary['total_earnings']:.2f}</h3><p class="hint">real earnings (logged)</p></div>
  <div class="feature"><h3>{real_summary['total_orders']}</h3><p class="hint">real orders (logged)</p></div>
</div>
<p class="hint" style="margin-top:8px">Config: {seo._clean(earn_note)}. Commission % is a {earnings.commission_pct(''):,.1f}% of ${earnings.avg_order(''):,.0f} average order at {earnings.order_rate('')*100:,.2f}% order rate — tune the three fields to your real Associates numbers and save.</p>
<div class="row" style="align-items:end;margin-top:6px">
  <label>Commission % <input id="e_pct" type="number" step="0.1" min="0" value="{earnings.commission_pct(''):.1f}"></label>
  <label>Avg order $ <input id="e_aov" type="number" step="1" min="0" value="{earnings.avg_order(''):.0f}"></label>
  <label>Order rate % <input id="e_rate" type="number" step="0.01" min="0" value="{earnings.order_rate('')*100:.2f}"></label>
  <button id="e_save" class="warm">Save config</button>
</div>
<p class="msg" id="e_msg"></p>
<div class="sub"><h3>Log real results from your Associates dashboard</h3>
<div class="row" style="align-items:end">
  <label>Month (YYYY-MM) <input id="r_month" placeholder="2026-09" value="{datetime.date.today().strftime('%Y-%m')}"></label>
  <label>Orders <input id="r_orders" type="number" min="0" placeholder="0"></label>
  <label>Earnings $ <input id="r_earn" type="number" step="0.01" min="0" placeholder="0.00"></label>
  <button id="r_save" class="warm">Log month</button>
</div>
<div class="table-wrap" style="margin-top:8px"><table class="plain"><thead><tr><th>Month</th><th>Orders</th><th>Earnings</th></tr></thead><tbody>
{''.join('<tr><td>%s</td><td class="ct">%d</td><td class="ct">$%.2f</td></tr>' % (seo._clean(m['month']), m['orders'], m['earnings']) for m in real_summary["months"]) or '<tr><td colspan="3" class="hint">Nothing logged yet — add this month’s real orders + earnings from Amazon Associates.</td></tr>'}
</tbody></table></div></div></section>
<section class="card"><h2>👀 Page views by URL</h2>
<div class="table-wrap"><table class="plain"><thead><tr><th>Page</th><th>Views</th></tr></thead><tbody>{ev_page_rows}</tbody></table></div></section>
<section class="card"><h2>⚡ Lead-page interactions</h2>
<p class="hint">Promo taps, countdown timing, sticky-CTAs, gate unlocks and lead-PDF downloads — every element tagged <code>[data-ev]</code> on the landing/build pages.</p>
<div class="table-wrap"><table class="plain"><thead><tr><th>Event</th><th>Count</th></tr></thead><tbody>{ev_rows}</tbody></table></div></section>
<section class="card"><h2>🛒 Top clicked pages</h2>
<div class="table-wrap"><table class="plain"><thead><tr><th>Niche</th><th>Clicks</th></tr></thead><tbody>{slug_rows}</tbody></table></div></section>
<section class="card"><h2>🏆 Most-clicked products</h2>
<p class="hint">Which ASIN earns the taps, per niche — use it to pick which products to push in emails and which pages deserve more variants.</p>
<div class="table-wrap"><table class="plain"><thead><tr><th>ASIN</th><th>Niche</th><th>Clicks</th></tr></thead><tbody>{product_rows}</tbody></table></div></section>
<section class="card"><h2>📥 By source</h2>
<div class="table-wrap"><table class="plain"><thead><tr><th>Source</th><th>Clicks</th></tr></thead><tbody>{src_rows}</tbody></table></div></section>
<section class="card"><h2>🧾 Recent clicks</h2>
<div class="table-wrap"><table class="plain"><thead><tr><th>Niche</th><th>Source</th><th>Referrer</th><th>When</th></tr></thead><tbody>{recent_rows}</tbody></table></div></section>
</main>
<footer><p>Views + interactions are captured privacy-first (IP hashes only) via /api/pageview; clicks via /api/track. Promo/countdown/sticky/gate elements are auto-tagged so you can see exactly which page behavior earns engagement and conversions.</p></footer>
<script src="/table-flow.js" defer></script>
<script>
const $=s=>document.getElementById(s);
async function postj(p, obj){{ const r=await fetch(p,{{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify(obj)}}); return r.json(); }}
if ($("e_save")) $("e_save").onclick = async () => {{
  const m=$("e_msg"); m.textContent="Saving…"; m.className="msg";
  try{{
    const d=await postj("/api/earnings/config",{{commission_pct:parseFloat($("e_pct").value),avg_order:parseFloat($("e_aov").value),order_rate:parseFloat($("e_rate").value)/100}});
    m.textContent=d.ok?"✓ Config saved — estimate updated.":("✗ "+(d.error||"failed")); m.className=d.ok?"msg":"msg";
  }}catch(e){{ m.textContent="✗ Could not reach the server."; }}
}};
if ($("r_save")) $("r_save").onclick = async () => {{
  const m=$("r_msg")||$("e_msg"); m.textContent="Saving…"; m.className="msg";
  try{{
    const d=await postj("/api/earnings/log",{{month:$("r_month").value.trim(),orders:parseInt($("r_orders").value||"0",10),earnings:parseFloat($("r_earn").value||"0")}});
    m.textContent=d.ok?"✓ Logged. Refresh to see the table update.":("✗ "+(d.error||"failed")); m.className=d.ok?"msg":"msg";
  }}catch(e){{ m.textContent="✗ Could not reach the server."; }}
}};
</script>
{_TOTOP}
</body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _admin_backup(self):
        """Admin-only download of the live SQLite DB (portable backup)."""
        try:
            with _lock:
                conn = _db()
                data = "".join(conn.iterdump()).encode("utf-8")
                conn.close()
            ts = time.strftime("%Y%m%d-%H%M%S")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-sqlite3")
            self.send_header("Content-Disposition",
                             'attachment; filename="pstore-%s.sql"' % ts)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def _earnings_config(self):
        """Admin: tune the commission estimator. Persists to the DB `settings`
        table so the tuned numbers survive restarts (unlike a runtime dict)."""
        body = self._body()
        keys = ("commission_pct", "avg_order", "order_rate")
        try:
            vals = {k: float(body.get(k) or 0) for k in keys}
        except (TypeError, ValueError):
            return self._send(200, {"ok": False, "error": "Numbers required."})
        if not (vals["commission_pct"] >= 0 and vals["avg_order"] > 0
                and 0 <= vals["order_rate"] <= 1):
            return self._send(200, {"ok": False, "error": "Out of range."})
        earnings.configure(commission_pct=vals["commission_pct"],
                           avg_order=vals["avg_order"], order_rate=vals["order_rate"])
        _set_setting("earnings.commission_pct", str(vals["commission_pct"]))
        _set_setting("earnings.avg_order", str(vals["avg_order"]))
        _set_setting("earnings.order_rate", str(vals["order_rate"]))
        return self._send(200, {"ok": True})

    def _earnings_log(self):
        """Admin: record a month's real orders + earnings from the dashboard."""
        body = self._body()
        month = str(body.get("month") or "").strip()[:7]
        if not re.match(r"^\d{4}-\d{2}$", month):
            return self._send(200, {"ok": False, "error": "Month must be YYYY-MM."})
        try:
            orders = max(int(body.get("orders") or 0), 0)
            earnings_amt = max(float(body.get("earnings") or 0), 0.0)
        except (TypeError, ValueError):
            return self._send(200, {"ok": False, "error": "Numbers required."})
        with _lock:
            conn = _db()
            cur = conn.execute(
                "SELECT id FROM earnings_records WHERE month=? ORDER BY id DESC LIMIT 1",
                (month,)).fetchone()
            if cur:
                conn.execute(
                    "UPDATE earnings_records SET orders=?, earnings=? WHERE id=?",
                    (orders, earnings_amt, cur["id"]))
            else:
                conn.execute(
                    "INSERT INTO earnings_records (month, orders, earnings) VALUES (?,?,?)",
                    (month, orders, earnings_amt))
            conn.commit()
            conn.close()
        return self._send(200, {"ok": True})

    def _earnings_priority_data(self):
        """Compute the earnings-priority payload once, shared by the API and the
        /admin/priority page so rankings never drift between the two."""
        rows = []
        with _lock:
            conn = _db()
            try:
                rows = conn.execute(
                    "SELECT lower(slug) AS slug, COUNT(*) AS clicks "
                    "FROM clicks GROUP BY lower(slug)").fetchall()
            finally:
                conn.close()
        def cat(r):
            return ""
        payload = earnings.priority_rows([
            {"niche": r["slug"], "clicks": r["clicks"]} for r in rows], cat)
        months = []
        with _lock:
            conn = _db()
            try:
                months = conn.execute(
                    "SELECT month, orders, earnings FROM earnings_records "
                    "ORDER BY month").fetchall()
            finally:
                conn.close()
        real = earnings.monthly_summary([dict(m) for m in months])
        return {
            "ranked": payload["ranked"], "total_est": payload["total_est"],
            "real": real,
            "note": ("Ranked by estimated commission = clicks x $%.0f AOV x %.1f%% x %.1f%% order rate."
                     % (earnings.avg_order(""), earnings.commission_pct(""),
                        earnings.order_rate("") * 100))}

    def _earnings_priority(self, q):
        """Earnings-driven prioritization: rank every built niche by its projected
        commission so the biggest money-levers (niches, social cadence, ad spend)
        are clearly ordered. Uses real click counts x the tuned estimator."""
        return self._send(200, self._earnings_priority_data())

    def _admin_variants(self, q):
        """A/B headline split-tests: per-niche alternative H1s. Variants are
        served deterministically per visitor (stable A/B split) and every click
        on the page is tagged ab-<variant> in analytics so you can pick a
        winner by conversion, not vibes."""
        keyword = (q.get("keyword") or [""])[0].strip()
        niches = self._all_niches()
        row = []
        for n in niches:
            s = seo._slugify(n["keyword"])
            if keyword and s != seo._slugify(keyword):
                continue
            with _lock:
                conn = _db()
                vars_ = conn.execute(
                    "SELECT * FROM niche_variants WHERE lower(slug)=? ORDER BY variant ASC",
                    (s.lower(),)).fetchall()
                click_by = dict(conn.execute(
                    "SELECT content, COUNT(*) c FROM clicks WHERE lower(slug)=? AND content LIKE 'ab-%' "
                    "GROUP BY content", (s.lower(),)).fetchall())
                conn.close()
            vrows = ""
            if not vars_:
                vrows = ('<tr data-slug="%s"><td class="vno">1</td><td><input class="vhead" placeholder="Alternative headline (e.g. %s in %d picks, compared)" value=""></td>'
                         '<td class="ct">—</td><td><label><input type="checkbox" class="ven" checked> on</label></td></tr>'
                         % (seo._clean(s), seo._clean(n["keyword"].title()), len(n.get("products") or [])))
            else:
                for v in vars_:
                    key = "ab-%s" % v["variant"]
                    vrows += ('<tr data-slug="%s"><td class="vno">%s</td><td><input class="vhead" value="%s"></td>'
                              '<td class="ct">%d</td><td><label><input type="checkbox" class="ven" %s> on</label></td></tr>'
                              % (seo._clean(s), v["variant"], seo._clean(v["headline"] or ""),
                                 click_by.get(key, 0), "checked" if v["enabled"] else ""))
            row.append('<section class="card" data-keyword="%s"><h2>%s <span class="hint">(/%s)</span></h2>'
                       '<div class="table-wrap"><table class="plain"><thead><tr><th>Variant</th><th>Headline</th>'
                       '<th>Clicks</th><th>On</th></tr></thead><tbody>%s</tbody></table></div>'
                       '<p class="msg"></p><button class="warm">Save variants</button></section>'
                       % (seo._clean(s), seo._clean(n["keyword"].title()), seo._clean(s), vrows))
        if not row:
            row = ['<p class="hint">No niches yet — mine one on the dashboard first.</p>']
        # ----- email-subject A/B editor (per niche + sequence step) -----
        subjects_row = []
        for n in niches:
            s = seo._slugify(n["keyword"])
            if keyword and s != seo._slugify(keyword):
                continue
            existing = {(r["email_index"], r["variant"]): r for r in self._subjects_for(s)}
            stats = self._subject_stats(s)
            st_by = {("%s|%s" % (r["email_index"], r["variant"])): r for r in stats}
            subs = ""
            for idx in range(1, mailer.SEQUENCE_LENGTH + 1):
                for var in (1, 2):
                    key = (idx, var)
                    rec = existing.get(key)
                    subj = seo._clean(rec["subject"]) if rec else ""
                    enabled = "checked" if (rec is None or rec["enabled"]) else ""
                    peer = st_by.get("%s|%s" % (idx, var))
                    perf = ("· %s sent · %s%% open · %s clicks"
                            % (peer["sent"], peer["open_rate"], peer["clicks"])) if peer else "· no sends yet"
                    subs += ('<tr><td class="ct">%s</td><td class="ct">v%s</td>'
                             '<td><input class="ssub" data-idx="%s" data-var="%s" value="%s"></td>'
                             '<td><label><input type="checkbox" class="sen" data-idx="%s" data-var="%s" %s> on</label></td>'
                             '<td class="hint">%s</td></tr>'
                             % (idx, var, idx, var, subj, idx, var, enabled, seo._clean(perf)))
            subjects_row.append(
                '<section class="card" data-keyword="%s" data-subjects="1"><h2>✉️ %s — email subject A/B %s</h2>'
                '<div class="table-wrap"><table class="plain"><thead><tr><th>Step</th><th>Var</th>'
                '<th>Subject</th><th>On</th><th>Perf</th></tr></thead><tbody>%s</tbody></table></div>'
                '<p class="msg"></p><button class="warm">Save subjects</button></section>'
                % (seo._clean(s), seo._clean(n["keyword"].title()),
                   "<span class='hint'> (/%s)</span>" % seo._clean(s), subs))
        subjects_html = ("<h2 style='margin-top:22px'>✉️ Email subject-line A/B</h2>"
                         "<p class='hint'>Two alternative subjects per sequence step per niche — split ~50/50 per "
                         "subscriber. Open/click perf per subject is shown so you double-down on the opener that "
                         "actually pulls reads, not just sends.</p>" + "".join(subjects_row))
        # ----- social-caption A/B editor (per niche + platform) -----
        captions_row = []
        for n in niches:
            s = seo._slugify(n["keyword"])
            if keyword and s != seo._slugify(keyword):
                continue
            cap_existing = {("%s|%s" % (r["platform"], r["variant"])): r for r in self._captions_for(s)}
            plat_rows = ""
            for platform in social.PLATFORMS:
                for var in (1, 2):
                    rec = cap_existing.get("%s|%s" % (platform, var))
                    cap_txt = seo._clean(rec["caption"]) if rec else ""
                    enabled = "checked" if (rec is None or rec["enabled"]) else ""
                    plat_rows += ('<tr><td class="ct">%s</td><td class="ct">v%s</td>'
                                  '<td><textarea class="scap" data-platform="%s" data-var="%s" rows="2">%s</textarea></td>'
                                  '<td><label><input type="checkbox" class="cen" data-platform="%s" data-var="%s" %s> on</label></td></tr>'
                                  % (seo._clean(platform), var, seo._clean(platform), var, cap_txt,
                                     seo._clean(platform), var, enabled))
            captions_row.append(
                '<section class="card" data-keyword="%s" data-captions="1"><h2>📱 %s — social caption A/B %s</h2>'
                '<p class="hint">Alternative post copy per platform. Visitors are split ~50/50 deterministically; '
                'the active variant is what actually gets published.</p>'
                '<div class="table-wrap"><table class="plain"><thead><tr><th>Platform</th><th>Var</th>'
                '<th>Caption</th><th>On</th></tr></thead><tbody>%s</tbody></table></div>'
                '<p class="msg"></p><button class="warm">Save captions</button></section>'
                % (seo._clean(s), seo._clean(n["keyword"].title()),
                   "<span class='hint'> (/%s)</span>" % seo._clean(s), plat_rows))
        captions_html = ("<h2 style='margin-top:22px'>📱 Social caption A/B</h2>"
                         + "".join(captions_row))
        body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>A/B headline tests — pstore</title><link rel="stylesheet" href="/style.css">
<meta name="robots" content="noindex,nofollow">
</head><body>
<header id="top"><a class="logo" href="/"><span class="mark">P</span><span>pstore</span></a>
<div class="hero"><h1>A/B <span>headline tests.</span></h1>
<p class="tagline">Give each niche alternative H1s. Visitors are split ~50/50 deterministically, and clicks carry an <code>ab-&lt;variant&gt;</code> tag so you pick the winner by conversion.</p></div>
{self._admin_nav('variants')}
</header>
<main>
<section class="card"><h2>⚗️ Automatic cleanup</h2>
<p class="hint">Once a niche accumulates enough real Amazon clicks, any headline variant converting under 25% of that niche's click leader is auto-disabled so it stops wasting impressions. Clicks per variant are shown in each card's table.</p>
<button id="autoclean" class="warm">Run auto-cleanup now</button>
<p id="cleanmsg" class="msg"></p></section>
{'<section class="card"><p class="hint">Filtering by keyword: %s. <a href="/admin/variants">Show all →</a></p></section>' % seo._clean(keyword) if keyword else ''}
{''.join(row)}
{subjects_html}
{captions_html}
</main>
<footer><p>Leave a variant empty to disable it. The control (default) headline is always shown when no variants are set. Winner-takes-CTA after you see enough clicks.</p></footer>
<script>
const $$=s=>Array.prototype.slice.call(document.querySelectorAll(s));
const ac=document.getElementById("autoclean");
if(ac){{
  ac.onclick=async function(){{
    ac.disabled=true; ac.textContent="Running…";
    const m=document.getElementById("cleanmsg"); m.className="msg"; m.textContent="Scanning variants…";
    try{{
      const r=await fetch("/api/variants/autoclean",{{method:"POST",headers:{{"Content-Type":"application/json"}}}});
      const d=await r.json();
      if(!d.ok){{ m.textContent="✗ "+(d.error||"failed"); }}
      else{{
        const n=d.variants.filter(v=>!v.enabled).length;
        m.textContent="✓ Cleanup done. "+n+" variant(s) currently disabled. Reload to see updated click counts.";
      }}
    }}catch(e){{ m.textContent="✗ Could not reach the server."; }}
    ac.disabled=false; ac.textContent="Run auto-cleanup now";
  }};
}}
$$(".card[data-keyword]").forEach(function(card){{
  var btn=card.querySelector("button");
  if(!btn || card.getAttribute("data-subjects")) return;
  btn.onclick=async function(){{
    var rows=card.querySelectorAll("tr[data-slug]");
    var payload={{slug:card.getAttribute("data-keyword"),variants:[]}};
    rows.forEach(function(tr){{
      var v=parseInt(tr.querySelector(".vno").textContent,10)||1;
      var head=tr.querySelector(".vhead").value.trim();
      var on=tr.querySelector(".ven").checked?1:0;
      payload.variants.push({{variant:v,variant_headline:head,enabled:on}});
    }});
    var m=card.querySelector(".msg"); m.className="msg"; m.textContent="Saving…";
    try{{
      var r=await fetch("/api/variants/save",{{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify(payload)}});
      var d=await r.json(); m.textContent=d.ok?"✓ Saved.":("✗ "+(d.error||"failed"));
    }}catch(e){{ m.textContent="✗ Could not reach the server."; }}
  }};
}});
$$(".card[data-subjects]").forEach(function(card){{
  var btn=card.querySelector("button");
  if(!btn) return;
  btn.onclick=async function(){{
    var rows=card.querySelectorAll("tr");
    var items=[];
    rows.forEach(function(tr){{
      var s=tr.querySelector(".ssub");
      if(!s) return;
      var en=tr.querySelector(".sen");
      items.push({{email_index:parseInt(s.getAttribute("data-idx"),10)||1,
                   variant:parseInt(s.getAttribute("data-var"),10)||1,
                   subject:s.value.trim(), enabled:!en || en.checked}});
    }});
    var m=card.querySelector(".msg"); m.className="msg"; m.textContent="Saving…";
    try{{
      var r=await fetch("/api/subjects/save",{{method:"POST",headers:{{"Content-Type":"application/json"}},
        body:JSON.stringify({{keyword:card.getAttribute("data-keyword"),items:items}})}});
      var d=await r.json(); m.textContent=d.ok?"✓ Saved "+d.saved+" subject variant(s).":("✗ "+(d.error||"failed"));
    }}catch(e){{ m.textContent="✗ Could not reach the server."; }}
  }};
}});
$$(".card[data-captions]").forEach(function(card){{
  var btn=card.querySelector("button");
  if(!btn) return;
  btn.onclick=async function(){{
    var variants=[];
    card.querySelectorAll("tr").forEach(function(tr){{
      var t=tr.querySelector(".scap");
      if(!t) return;
      var en=tr.querySelector(".cen");
      variants.push({{platform:t.getAttribute("data-platform"),
                   variant:parseInt(t.getAttribute("data-var"),10)||1,
                   caption:t.value, enabled:!en || en.checked}});
    }});
    var m=card.querySelector(".msg"); m.className="msg"; m.textContent="Saving…";
    try{{
      var r=await fetch("/api/captions/save",{{method:"POST",headers:{{"Content-Type":"application/json"}},
        body:JSON.stringify({{slug:card.getAttribute("data-keyword"),variants:variants}})}});
      var d=await r.json(); m.textContent=d.ok?"✓ Saved "+d.saved+" caption variant(s).":("✗ "+(d.error||"failed"));
    }}catch(e){{ m.textContent="✗ Could not reach the server."; }}
  }};
}});
</script>
<script src="/table-flow.js" defer></script>
{_TOTOP}
</body></html>"""
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _ab_stats(self, slug):
        """Per-variant Amazon-click counts for one niche from the click beacon
        (variants tagged as 'ab-<n>' in UTM content). Returns {variant: clicks}
        limited to variants that actually exist in niche_variants for slug."""
        rows = []
        with _lock:
            conn = _db()
            try:
                rows = conn.execute(
                    "SELECT variant FROM niche_variants WHERE lower(slug)=? "
                    "AND enabled=1 AND headline != '' ORDER BY variant ASC",
                    (slug.lower(),)).fetchall()
            finally:
                conn.close()
        known = [r["variant"] for r in rows]
        if not known:
            return {}
        stats = {}
        with _lock:
            conn = _db()
            try:
                for v in known:
                    n = conn.execute(
                        "SELECT COUNT(*) AS c FROM clicks WHERE lower(slug)=? "
                        "AND content LIKE ?",
                        (slug.lower(), "ab-%s%%" % v)).fetchone()["c"]
                    stats[v] = n
            finally:
                conn.close()
        return stats

    def _ab_autoclean(self, min_clicks=None):
        """A/B auto-cleanup: for each niche, when a headline variant has enough
        combined traffic and converts far below the control, auto-disable it so
        losers stop burning impressions. Returns the actions taken."""
        if min_clicks is None:
            try:
                min_clicks = int(_get_setting("ab.min_clicks") or 40)
            except (TypeError, ValueError):
                min_clicks = 40
        changed = []
        with _lock:
            conn = _db()
            slugs = conn.execute("SELECT DISTINCT lower(slug) AS s FROM niche_variants "
                                 "WHERE enabled=1").fetchall()
            conn.close()
        for r in slugs:
            slug = r["s"]
            stats = self._ab_stats(slug)
            if not stats or sum(stats.values()) < min_clicks:
                continue
            best = max(stats, key=stats.get)
            best_n = stats[best]
            losers = [v for v, n in stats.items() if v != best and n < best_n * 0.25]
            if not losers:
                continue
            with _lock:
                conn = _db()
                for v in losers:
                    conn.execute(
                        "UPDATE niche_variants SET enabled=0 "
                        "WHERE lower(slug)=? AND variant=?", (slug, v))
                conn.commit()
                conn.close()
            changed.append({"slug": slug, "disabled": losers,
                            "clicks": dict(stats), "control": best})
        return {"ok": True, "changed": changed,
                "note": ("Disabled variants converting below 25%% of the leader "
                         "once a niche reached %d lifetime clicks." % min_clicks)}

    def _variants_autoclean(self):
        """Admin: run A/B auto-cleanup on demand and report what got disabled."""
        try:
            self._ab_autoclean()
        except Exception as exc:
            return self._send(200, {"ok": False, "error": str(exc)})
        # freshest stats for the report
        return self._send(200, self._ab_summary())

    def _ab_summary(self):
        """Snapshot of every niche's variant health for the /admin/variants page."""
        rows = []
        with _lock:
            conn = _db()
            try:
                vrows = conn.execute(
                    "SELECT lower(slug) AS slug, variant, headline, enabled "
                    "FROM niche_variants ORDER BY slug, variant").fetchall()
            finally:
                conn.close()
        for r in vrows:
            stats = self._ab_stats(r["slug"])
            rows.append({
                "slug": r["slug"], "variant": r["variant"],
                "headline": r["headline"], "enabled": bool(r["enabled"]),
                "clicks": stats.get(r["variant"], 0),
            })
        return {"ok": True, "variants": rows,
                "note": "Run auto-cleanup to disable any variant under 25% of its niche's click leader (min 40 lifetime clicks)."}

    def _variants_save(self):
        """Admin: upsert a niche's headline variants."""
        body = self._body()
        slug = str(body.get("slug") or "").strip().lower()[:120]
        variants = body.get("variants") or []
        if not slug:
            return self._send(200, {"ok": False, "error": "Missing slug."})
        cleaned = []
        for v in variants:
            if not isinstance(v, dict):
                continue
            try:
                no = int(v.get("variant") or 1)
            except (TypeError, ValueError):
                no = 1
            head = str(v.get("variant_headline") or "").strip()[:160]
            enabled = int(bool(v.get("enabled")))
            cleaned.append((no, head, enabled))
        with _lock:
            conn = _db()
            conn.execute("DELETE FROM niche_variants WHERE lower(slug)=?", (slug,))
            for no, head, enabled in cleaned:
                if head:
                    conn.execute(
                        "INSERT INTO niche_variants (slug, variant, headline, enabled) "
                        "VALUES (?,?,?,?)", (slug, no, head, enabled))
            conn.commit()
            conn.close()
        return self._send(200, {"ok": True, "saved": len([c for c in cleaned if c[1]])})


class _AutosendStub:
    """Duck-typed stand-in for Handler so headless scheduler threads can call
    instance helpers without an HTTP request."""
    _OUTPUT = None  # suppress no-send edge

    def _body(self):
        return {}

    def _send(self, code, payload, _ctype=None):
        return {"status": code, "payload": payload}

    def _subscriber_segment(self, sid):
        return Handler._subscriber_segment(self, sid)

    def _ebook_attachment(self, kw):
        return Handler._ebook_attachment(self, kw)

    def _subject_for(self, kw, idx, sid, default_subject):
        return Handler._subject_for(self, kw, idx, sid, default_subject)

    def _already_sent(self, campaign, sid, asin=""):
        return Handler._already_sent(self, campaign, sid, asin)

    def _log_email_send(self, campaign, sid, kw="", asin=""):
        Handler._log_email_send(self, campaign, sid, kw, asin)

    def _dispatch_studio(self, spec, recipients, dry=False):
        return Handler._dispatch_studio(self, spec, recipients, dry)


def _ingest_inbound(messages, mailbox="inbox"):
    """Store fetched inbound messages into mailbox_messages, deduped by
    Message-ID, and link each to a subscriber: the tagged Reply-To wins (the
    address carries the subscriber id), else a from-address fallback match."""
    import hashlib as _hashlib
    stored = dupes = linked = 0
    cap = min(len(messages or []), 100)
    for m in (messages or [])[:cap]:
        msg_id = (m.get("message_id") or "").strip()
        if not msg_id:
            msg_id = _hashlib.sha1(("%s|%s|%s" % (m.get("from_addr"), m.get("date"), m.get("subject"))
                                    ).encode("utf-8", "replace")).hexdigest()[:24]
        sid = mailer.parse_thread_tag(m.get("to_header") or "")
        if not sid:
            frm = (m.get("from_addr") or "").strip().lower()
            if frm:
                try:
                    with _lock:
                        conn = _db()
                        row = conn.execute("SELECT id FROM subscribers WHERE lower(email)=?",
                                           (frm,)).fetchone()
                        conn.close()
                    sid = row["id"] if row else None
                except Exception:
                    sid = None
        row = None
        with _lock:
            conn = _db()
            cur = conn.execute(
                "INSERT OR IGNORE INTO mailbox_messages "
                "(message_id, in_reply_to, mailbox, from_addr, from_name, subject, text, "
                "html, subscriber_id, status) VALUES (?,?,?,?,?,?,?,?,?,'unread')",
                (msg_id, (m.get("in_reply_to") or "")[:500], mailbox,
                 (m.get("from_addr") or "")[:500], (m.get("from_name") or "")[:500],
                 (m.get("subject") or "")[:500], m.get("text") or "", m.get("html") or "",
                 int(sid or 0)))
            conn.commit()
            conn.close()
        if cur.rowcount and cur.rowcount > 0:
            stored += 1
            if sid:
                linked += 1
        else:
            dupes += 1
    return {"ok": True, "stored": stored, "dupes": dupes, "linked": linked,
            "mailbox": mailbox}


def _inbox_tick():
    """Fetch the latest replies from the IMAP inbox, or no-op when unconfigured.
    Also callable any time via POST /api/cron/inbox (webhook mode). A test hook
    (mailer._imap_fetch) stands in for the network path."""
    if not mailer.inbound_configured() and mailer._imap_fetch is None:
        return {"ok": False, "error": "IMAP inbox not configured "
                "(set IMAP_HOST/USER/PASSWORD or point a forwarder at /api/cron/inbox)",
                "stored": 0, "dupes": 0, "linked": 0}
    try:
        msgs = mailer.fetch_inbound()
    except Exception as exc:  # a flaky network must never kill the loop
        return {"ok": False, "error": str(exc)[:200], "stored": 0, "dupes": 0, "linked": 0}
    return _ingest_inbound(msgs)


def _inbox_loop():
    while True:
        time.sleep(60)
        try:
            _inbox_tick()
        except Exception:
            pass


def _autosend_tick():
    """Run the sequence send now IF (a) enabled and (b) current UTC hour is
    scheduled and (c) this date/hour slot hasn't already run. Persists state
    into settings so the email studio page can display last-run status."""
    import datetime as _dt
    cfg = _autosend_cfg()
    if not cfg["enabled"]:
        _set_setting(AUTOSEND_STATE_KEY, json.dumps({
            "status": "disabled", "hours": cfg["hours"], "limit": cfg["limit"]}))
        return "idle"
    now = _dt.datetime.utcnow()
    hour = now.hour
    if hour not in cfg["hours"]:
        return "idle"
    marker = "%s:%02d" % (now.strftime("%Y-%m-%d"), hour)
    last = _get_setting(_AUTOSEND_LAST_KEY) or ""
    if last == marker:
        return "done"
    stub = _AutosendStub()
    try:
        res = Handler._sequence_send(stub)
        ok = bool(res and isinstance(res, dict)
                  and (res.get("payload") or {}).get("ok"))
        sent = (res or {}).get("payload", {}).get("sent") or 0
        errors = (res or {}).get("payload", {}).get("errors") or 0
    except Exception as exc:  # never let the scheduler die on one bad run
        _set_setting(AUTOSEND_STATE_KEY, json.dumps({
            "status": "error", "error": str(exc)[:200], "hours": cfg["hours"],
            "limit": cfg["limit"]}))
        return "error: %s" % exc
    _set_setting(_AUTOSEND_LAST_KEY, marker)
    _set_setting(AUTOSEND_STATE_KEY, json.dumps({
        "status": "sent" if ok else "fail", "sent": sent, "errors": errors,
        "hours": cfg["hours"], "limit": cfg["limit"], "last_run": marker}))
    return "sent" if ok else "fail"


def _autosend_loop():
    while True:
        try:
            _autosend_tick()
        except Exception:
            pass
        time.sleep(1800)  # check twice hourly so transient misses still catch the slot


def _fire_outbox_rows():
    """Send any outbox rows whose scheduled_at is in the past."""
    import datetime as _dt
    with _lock:
        conn = _db()
        rows = conn.execute(
            "SELECT * FROM outbox WHERE status='scheduled' "
            "AND (scheduled_at IS NULL OR scheduled_at <= datetime('now'))").fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            ph = ",".join("?" * len(ids))
            conn.execute("UPDATE outbox SET status='sending' WHERE id IN (%s)" % ph, ids)
            conn.commit()
        conn.close()
    for row in rows:
        oid = row["id"]
        try:
            spec = json.loads(row["spec"])
            rec = json.loads(row["recipients"])
            stub = _AutosendStub()
            res = Handler._dispatch_studio(stub, spec, rec, dry=False)
        except Exception as exc:
            res = {"ok": False, "sent": 0, "errors": 1,
                   "recipients": len((json.loads(row["recipients"]) if row["recipients"] else [])),
                   "error": str(exc)[:300]}
        with _lock:
            conn = _db()
            conn.execute("UPDATE outbox SET status='done', result=?, sent_at=datetime('now') WHERE id=?",
                         (json.dumps(res), oid))
            conn.commit()
            conn.close()


def _outbox_loop():
    while True:
        time.sleep(45)
        try:
            _fire_outbox_rows()
        except Exception:
            pass


def main():
    _init()
    if not _ADMIN_EMAIL_FROM_ENV or not _ADMIN_PW_FROM_ENV:
        print("WARNING: PSTORE_ADMIN_EMAIL / PSTORE_ADMIN_PASSWORD not both set — "
              "using the default admin credentials. Set them in Render/env before going public.")
    if not oauth.providers_configured():
        print("NOTE: Google/Facebook OAuth login disabled — set OAUTH_GOOGLE_CLIENT_ID/SECRET "
              "or OAUTH_FACEBOOK_APP_ID/APP_SECRET to enable it.")
    if not mailer.configured():
        print("NOTE: SMTP not configured — /subscribe captures and /admin/emails previews work, "
              "but sequence sends are refused. Set SMTP_HOST/USER/PASSWORD to enable sending.")
    if not ai.configured():
        print("NOTE: AI not configured — ebook/headline copy uses deterministic templates. "
              "Set AI_API_KEY, OPENCODE_API_KEY, MISTRAL_API_KEY or NVIDIA_API_KEY (or add a "
              "key in /admin/ebooks) to enable generation.")
    amazon.set_market(os.environ.get("PSTORE_MARKET", amazon.DEFAULT_MARKET))
    amazon.set_tag(os.environ.get("PSTORE_TAG", ""))
    warm = threading.Thread(target=_warm_startup_admin, name="warm-admin-start",
                            daemon=True)
    warm.start()
    print("admin Grow payloads: pre-warming in background (autosuggest + suggest engine)")
    if _REFRESH_INTERVAL_SEC > 0:
        threading.Thread(target=_auto_refresh_loop, daemon=True).start()
        print("niche auto-refresh: every %ds, stale after %dm, %d/cycle"
              % (_REFRESH_INTERVAL_SEC, _REFRESH_STALE_MIN, _REFRESH_MAX_PER_CYCLE))
    threading.Thread(target=_social_flush_loop, daemon=True).start()
    print("social scheduler: auto-flush every 60s (due scheduled posts)")
    threading.Thread(target=_outbox_loop, daemon=True).start()
    print("email outbox: due scheduled studio sends every 45s")
    threading.Thread(target=_inbox_loop, daemon=True).start()
    if mailer.configured() and not mailer.inbound_configured():
        print("NOTE: email inbox not configured — replies go nowhere. Set IMAP_HOST/USER/"
              "PASSWORD (or point a forwarder at POST /api/cron/inbox with EMAIL_CRON_SECRET) "
              "to capture them in the Email Studio. Set PSTORE_REPLY_DOMAIN to tag each "
              "send's Reply-To with its subscriber id.")
    print("email inbox: polls IMAP replies every 60s (POST /api/cron/inbox also triggers)")
    if _AUTOSEND_HOURS or _get_setting("autosend.hours"):
        threading.Thread(target=_autosend_loop, daemon=True).start()
        print("sequence autosend: daily at %s UTC, cap %d/run"
              % (",".join(map(str, _AUTOSEND_HOURS)), _AUTOSEND_LIMIT))
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("pstore running on http://localhost:%d" % PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
