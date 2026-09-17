# -*- coding: utf-8 -*-
"""pstore link-authority engine: the "votes of confidence" pillar.

SEO's big gap vs. on-page work is *external* authority — hyperlinks from sites
Google/Bing trust.  This module generates a deterministic, per-niche outreach
pipeline for the five classic money-making link tactics:

  * guestpost   — posts on niche blogs that accept contributions
  * haro        — answering journalist source requests ("be the expert")
  * brokenlink  — fixing dead outbound links on authority resource roundups
  * pr          — news-jacking a seasonal/trending angle (echoes Hot-Sale Finder)
  * resource    — getting listed on niche resource/link pages

Every target is just a search-query *strategy* plus a ready-to-send pitch, so
the operator can run the day's list without any paid tooling.  The module is
stdlib-only and fully deterministic (no network, no shell) — generation is
hermetic in tests.
"""
import re
import urllib.parse
import zlib

# ------------------------------------------------------------------ outreach table
SCHEMA = """CREATE TABLE IF NOT EXISTS backlink_outreach (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL,
    slug TEXT NOT NULL DEFAULT '',
    tactic TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'discovered',
    angle TEXT,
    pitch TEXT,
    source_url TEXT DEFAULT '',
    target_url TEXT DEFAULT '',
    note TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
)"""
SCHEMA_INDEX = ("CREATE INDEX IF NOT EXISTS idx_backlink_kw "
                "ON backlink_outreach (keyword, tactic)")

STATUSES = ["discovered", "researched", "pitched", "published", "live", "dead"]
# "live" is the only one that counts toward authority; everything else is progress.

TACTICS = {
    "guestpost": {
        "label": "Guest post",
        "icon": "✍️",
        "goal": ("Author a post on a niche blog that accepts contributions — "
                 "every post backlinks to your niche page with a real editorial "
                 "vote."),
        "platforms": ("Search for sites with 'write for us' pages; vet by traffic "
                      "and domain authority. Well-known guest-post marketplaces "
                      "list eager, high-DR blogs."),
    },
    "haro": {
        "label": "HARO / expert source",
        "icon": "📰",
        "goal": ("Answer journalist source requests in your niche. One good quote "
                 "= a backlink from a major news site."),
        "platforms": ("Help A Reporter Out (HARO — relaunched 2025), Featured, "
                      "JournoRequests, SourceBottle. Requests land in your inbox "
                      "3x/day."),
    },
    "brokenlink": {
        "label": "Broken-link building",
        "icon": "🔗",
        "goal": ("Find a dead outbound link on an authority resource roundup and "
                 "offer your live niche page as the replacement — a free, "
                 "editor-friendly swap."),
        "platforms": ("Use 'best <niche> resources' roundups; check their links "
                      "with a link checker; email the editor with your "
                      "replacement page."),
    },
    "pr": {
        "label": "News-jack / PR",
        "icon": "📣",
        "goal": ("Ride a seasonal or trending angle (the Hot-Sale Finder engine) "
                 "to a timely quote, stat, or data point journalists quote on "
                 "deadline."),
        "platforms": ("Google News + HARO-type replies. Tie your stat to a live "
                      "event so reporters treat it as a credible sourcing "
                      "option."),
    },
    "resource": {
        "label": "Resource list",
        "icon": "🗂",
        "goal": ("Get your niche page listed on a curated '<niche> resources' page "
                 "— a permanent, high-context backlink that also drives referral "
                 "traffic."),
        "platforms": ("Curated directories, roundups, and 'tools we use' pages in "
                      "the niche; reach out with what to add and why it earns "
                      "the slot."),
    },
}

_PITCH = {
    "guestpost": ("I publish a fact-checked buyer's guide for '{kw}' and would "
                  "love to contribute a guest post — a practical '{kw}' checklist "
                  "your readers could actually use. I keep links genuinely useful, "
                  "no stuffing. Open to your editorial guidelines."),
    "haro": ("I research '{kw}' full-time and have fresh, citable data plus a "
             "short, quotable take on {angle}. Happy to give you an exclusive "
             "stat before it runs anywhere else."),
    "brokenlink": ("While reading your '{kw}' roundup I noticed one of the "
                   "linked resources is now a dead page. I built a live, "
                   "{angle}-focused guide you could point readers to instead — "
                   "it's updated and matches the section perfectly."),
    "pr": ("Hoping to offer a timely data point on {angle} — I track '{kw}' "
           "pricing/trends and can share a fresh stat + a punchy quote if your "
           "story on the trend is still open."),
    "resource": ("Your '{kw}' resources list is the best I've found — one gap: "
                 "no up-to-date '{kw}' guide. Could I suggest my page for that "
                 "slot? It's updated monthly and earns its keep with real picks."),
}

_ANGLES = {
    "guestpost": ["{kw} mistakes to avoid",
                  "the honest {kw} starter checklist",
                  "{kw} buyers' mistakes",
                  "what nobody tells you about {kw}"],
    "haro": ["how {kw} shopping changed this year",
             "the rising cost of {kw}",
             "first-time {kw} mistakes",
             "how to spot a bad {kw} deal"],
    "brokenlink": ["{kw} for real beginners",
                   "what to look for in {kw}",
                   "budget {kw} that actually works",
                   "the {kw} mistakes guide"],
    "pr": ["{kw} prices this holiday season",
           "why {kw} demand spiked this quarter",
           "{kw} myths every shopper still believes",
           "the data behind {kw} trends"],
    "resource": ["a true buyer's guide to {kw}",
                 "research-backed {kw} picks",
                 "the only {kw} guide you need",
                 "honest {kw} recommendations"],
}

_SEARCHES = {
    "guestpost": ['{kw} "write for us"', '{kw} blog "guest post"',
                  "{kw} " '"submit a guest post"', '{kw} "become an author"'],
    "haro": ['{kw} "seeking sources"', "{kw} journalist " '"source request"',
             '{kw} "expert comment" journalist', '{kw} HARO "source" query'],
    "brokenlink": ["best {kw} resources for beginners", "best {kw} tools 2026",
                   "top {kw} blogs and resources"],
    "pr": ["{kw} statistics 2026", "{kw} trend news", "latest {kw} news"],
    "resource": ["{kw} resources page", "{kw} link directory",
                 "{kw} useful tools and websites"],
}

_BASE_LINKS = {
    "guestpost": "systems/tools to find the best sites: Google/LinkedIn social search.",
    "haro": "HARO, Featured, JournoRequests, SourceBottle — reply within the source-request window.",
    "brokenlink": "The 'cash-for-links' method is dead: this is a genuine dead-link fix, offer a live replacement.",
    "pr": "Windows close fast. Prioritize this tactic the day you generate it; 48h turnaround typical.",
    "resource": "Listings last. A good resource page = permanent top-of-list backlink.",
}


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def _kw(keyword, stopwords=("the", "for", "and", "best", "top", "your", "of", "a", "in", "to")):
    k = (keyword or "").strip().lower()
    if k:
        return k
    return "your niche"


def search_urls(query):
    """Safe, real search URLs an operator can click to run a target's query."""
    q = urllib.parse.quote(query)
    return {
        "google": "https://www.google.com/search?q=%s" % q,
        "bing": "https://www.bing.com/search?q=%s" % q,
        "duckduckgo": "https://duckduckgo.com/?q=%s" % q,
    }


def suggest_queries(tactic, keyword, cap=4):
    k = _kw(keyword)
    pats = _SEARCHES.get(tactic, _SEARCHES["resource"])
    out = []
    for pat in pats[:cap]:
        out.append(pat.format(kw=k))
    return out


def build_target(keyword, tactic, dlp_url=""):
    """One deterministic outreach proposal for ``keyword`` + ``tactic``."""
    k = _kw(keyword)
    slug = slugify(keyword)
    angles = _ANGLES.get(tactic, _ANGLES["resource"])
    angle = angles[zlib.crc32(("%s:%s" % (k, tactic)).encode()) % len(angles)].format(kw=k)
    found = suggest_queries(tactic, k)
    src = search_urls(found[0])
    pitch = _PITCH.get(tactic, _PITCH["resource"]).format(kw=k, angle=angle)
    if dlp_url:
        pitch = pitch + (" My page is live at %s." % dlp_url)
    return {
        "keyword": k,
        "slug": slug,
        "tactic": tactic,
        "angle": angle,
        "pitch": pitch,
        "source_url": "",  # editor sets the actual page found
        "target_url": "",  # the page that will carry the backlink
        "searches": [search_urls(q) for q in found],
        "base": _BASE_LINKS.get(tactic, ""),
    }


def generate(conn, keywords, limit=60, dlp_url=""):
    """Insert fresh proposals for ``keywords`` into the outreach table.

    Deterministic and idempotent: an existing (keyword, tactic) row is never
    duplicated. Returns {"added": n, "total": rows_in_table}.
    """
    rows = []
    for kw in keywords:
        for tactic in TACTICS:
            rows.append(build_target(kw, tactic, dlp_url))
    added = 0
    for r in rows:
        if limit and added >= limit:
            break
        got = conn.execute(
            "SELECT id FROM backlink_outreach WHERE keyword=? AND tactic=? AND "
            "status<>'dead'",
            (r["keyword"], r["tactic"])).fetchone()
        if got:
            continue
        conn.execute(
            "INSERT INTO backlink_outreach (keyword, slug, tactic, status, angle, "
            "pitch, source_url, target_url) VALUES (?,?,?,?,?,?,?,?)",
            (r["keyword"], r["slug"], r["tactic"], "discovered", r["angle"],
             r["pitch"], r["source_url"], r["target_url"]))
        added += 1
    conn.commit()
    return {"added": added,
            "total": conn.execute(
                "SELECT COUNT(*) c FROM backlink_outreach").fetchone()["c"]}


def update(conn, row_id, status=None, source_url=None, target_url=None, note=None):
    """Edit one outreach row; returns the updated row or None."""
    if status:
        conn.execute("UPDATE backlink_outreach SET status=?, updated_at="
                     "datetime('now') WHERE id=?", (status, row_id))
    if source_url is not None:
        conn.execute("UPDATE backlink_outreach SET source_url=?, updated_at="
                     "datetime('now') WHERE id=?", (source_url.strip(), row_id))
    if target_url is not None:
        conn.execute("UPDATE backlink_outreach SET target_url=?, updated_at="
                     "datetime('now') WHERE id=?", (target_url.strip(), row_id))
    if note is not None:
        conn.execute("UPDATE backlink_outreach SET note=?, updated_at="
                     "datetime('now') WHERE id=?", (note.strip(), row_id))
    conn.commit()
    row = conn.execute("SELECT * FROM backlink_outreach WHERE id=?",
                       (row_id,)).fetchone()
    return dict(row) if row else None


def delete(conn, row_id):
    cur = conn.execute("DELETE FROM backlink_outreach WHERE id=?", (row_id,))
    conn.commit()
    return cur.rowcount


def list_rows(conn, status=None, tactic=None, keyword=None, limit=300):
    sql = "SELECT * FROM backlink_outreach WHERE 1=1"
    args = []
    if status:
        sql += " AND status=?"
        args.append(status)
    if tactic:
        sql += " AND tactic=?"
        args.append(tactic)
    if keyword:
        sql += " AND keyword LIKE ?"
        args.append("%" + keyword + "%")
    sql += " ORDER BY updated_at DESC, id DESC LIMIT %d" % limit
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def stats(conn):
    """Per-status counts so the admin page can show the funnel at a glance."""
    counts = {s: 0 for s in STATUSES}
    total = 0
    for r in conn.execute(
            "SELECT status, COUNT(*) c FROM backlink_outreach GROUP BY status"):
        counts[r["status"]] = r["c"]
        total += r["c"]
    counts["total"] = total
    counts["authority"] = counts["live"]
    return counts