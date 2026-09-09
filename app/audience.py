# -*- coding: utf-8 -*-
"""Audience-lite: on-site visitor geography + the operator's edited target
persona, folded into machine-readable answers. Deterministic, stdlib-only,
offline. The beacon records the visitor's country (CF-IPCountry, which Render's
Cloudflare edge already provides) on every pageview and Amazon click; the
target persona is edited on /admin/marketing#demo. `regions()` reads the
beacon; `profile()` returns the edited persona plus live regions; `for_niche()`
adds a deterministic search-intent label for one keyword so SEM/SEO pages can
answer "who is searching for this niche, from where"."""

import os
import sqlite3

DB_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pstore.db")

COUNTRY_NAMES = {
    "US": "United States", "GB": "United Kingdom", "CA": "Canada",
    "AU": "Australia", "DE": "Germany", "FR": "France", "IT": "Italy",
    "ES": "Spain", "NL": "Netherlands", "IN": "India", "PL": "Poland",
    "SE": "Sweden", "BR": "Brazil", "MX": "Mexico", "JP": "Japan",
}


def _db():
    conn = sqlite3.connect(os.environ.get("PSTORE_DB", DB_DEFAULT))
    conn.row_factory = sqlite3.Row
    return conn


def _setting(key):
    try:
        conn = _db()
        try:
            row = conn.execute("SELECT value FROM settings WHERE key=?",
                               (key,)).fetchone()
            return row["value"] if row else ""
        finally:
            conn.close()
    except Exception:
        return ""


def _country_name(code):
    code = (code or "").upper().strip()
    return COUNTRY_NAMES.get(code, code) if code else "Other"


def regions(slug=None, days=28):
    """On-site geography from the beacon over the window: per-country
    pageviews + Amazon clicks. Optional `slug` narrows to one niche, so SEM/SEO
    pages can answer "who is actually on this page, from where" honestly.
    Returns [{code, name, views, clicks}] sorted by strongest signal first."""
    when = "created_at >= datetime('now', 'localtime', ?)"
    base_params = ["-%d days" % days]
    if slug:
        slug = (slug or "").lower()
        view_where = when + " AND slug=?"
        click_where = when + " AND slug=?"
        params = tuple(base_params + [slug])
    else:
        view_where = when
        click_where = when
        params = tuple(base_params)
    agg = {}
    try:
        conn = _db()
        try:
            vw = conn.execute(
                "SELECT COALESCE(country,'') country, COUNT(*) n FROM events "
                "WHERE name='view' AND %s GROUP BY country" % view_where,
                params).fetchall()
            ck = conn.execute(
                "SELECT COALESCE(country,'') country, COUNT(*) n FROM clicks "
                "WHERE %s GROUP BY country" % click_where,
                params).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    for r in vw:
        c = (r["country"] or "").upper().strip()
        row = agg.setdefault(c, {"views": 0, "clicks": 0})
        row["views"] += r["n"]
    for r in ck:
        c = (r["country"] or "").upper().strip()
        row = agg.setdefault(c, {"views": 0, "clicks": 0})
        row["clicks"] += r["n"]
    ranked = sorted(agg.items(),
                    key=lambda kv: (-(kv[1]["views"] + kv[1]["clicks"]), kv[0]))
    return [{"code": c or "OW", "name": _country_name(c),
             "views": v["views"], "clicks": v["clicks"]}
            for c, v in ranked]


def profile(days=28):
    """The operator's edited target persona (from /admin/marketing#demo) plus
    the live beacon regions, as one dict for machine + UI use."""
    return {
        "region": _setting("demo.region"),
        "interest": _setting("demo.interest"),
        "behavior": _setting("demo.behavior"),
        "age": _setting("demo.age"),
        "income": _setting("demo.income"),
        "audience": _setting("demo.audience"),
        "tone": _setting("demo.tone"),
        "regions": regions(days=days),
    }


def intent_summary(keyword):
    """Deterministic search-intent label from the keyword shape — no network,
    no model. Feeds the "who is searching" block without over-promising."""
    k = (keyword or "").lower()
    if any(w in k for w in (" vs ", "versus", "compare", "alternative",
                            "alternatives", "replace")):
        return "comparison shopper"
    if any(w in k for w in ("best", "top", "rated", "recommended",
                            "favorite", "popular")):
        return "best-pick researcher"
    if any(w in k for w in ("under", "cheap", "budget", "affordable",
                            "discount", "economical")):
        return "budget-first buyer"
    if any(w in k for w in ("buy", "shopping", "shop", "on sale", "clearance")):
        return "ready to buy"
    return "in research"


def for_niche(keyword, slug=None, days=28):
    """Combine the persona, the live beacon geography for this niche (or the
    whole site) and the keyword's search intent into the SEM/SEO "who is
    searching" payload."""
    p = profile(days=days)
    p["keyword"] = keyword
    p["slug"] = slug or ""
    p["intent"] = intent_summary(keyword)
    p["niche_regions"] = regions(slug=slug, days=days)
    p["regions"] = p["niche_regions"] or p["regions"]
    return p