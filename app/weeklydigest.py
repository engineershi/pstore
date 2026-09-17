#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""weeklydigest — hermetically-built WEEKLY MONEY DIGEST (stdlib only, pure,
deterministic, no network/DB/server/mailer/pricedrop/amazon imports).

THE MONEY SEAM (read this before touching anything):
  * This module builds ONE weekly money email payload through the app's
    renderer seam: the payload returns {"subject","text","html"} where the
    ONLY money link in ALL THREE parts is a SINGLE `{{tracked_link}}`
    moustache token that the app's mailer renderer fills with the real
    tracked hop (mailer.tracked_url → in-app tracked redirect). It never
    emits a per-product `{{tracked_link_<slug>}}` token, because the app's
    renderer only fills the bare `{{tracked_link}}` and a leaked per-slug
    token would go out as LITERAL text — a dead money link in the capstone
    weekly money email)Skip. This is the hermetic contract that makes the
    capstone money loop real: winners, deals and referral re-enrollment all
    flow through ONE tracked hop that the app counts, so the weekly loop
    can actually amplify winners and prune losers next week.
  * NO .format() and NO %-formatting ANYWHERE on the email body. Percentage
    values use a pure helper `_pct_str` and HTML/CSS with literal `%`/`{}`
    is carried by CONCATENATION with a single moustache token, so a money
    placeholder can never collide with the renderer's token fill. That's the
    hermetic fence: this module is testable offline, byte-deterministic, and
    cannot dead-link the weekly money email by accident.
  * Stdlib import ONLY. No datetime.now(), no wall clock — every function
    takes deterministic `day`/`week` inputs (default fixed epoch 2026-09-22
    Monday) so tests are stable across days and machines.

Caller (server.py) does ownership: reading rows from DB, persisting the
`weeklydigest.last_week` gate key, filling `{{first_name}}`,
`{{unsubscribe_url}}` and the real {{tracked_link}} through mailer.render_
email_html, and sending through the same dispatch seam as the shell."""

import datetime as _dt
import re as _re

DEFAULT_WINNERS_PER_NICHE = 2
DEFAULT_TOTAL_WINNERS = 6
DEFAULT_MIN_CLICKS = 1
DEFAULT_DEALS_PER_NICHE = 2
DEFAULT_TOTAL_DEALS = 6
DEFAULT_MIN_DROP_PCT = 12.0
DEFAULT_REFERRAL_MIN = 1
DEFAULT_PRUNE_MIN_CLICKS = 3
DEFAULT_PRUNE_QUIET_DAYS = 21

EPOCH = _dt.date(2026, 9, 22)      # fixed Monday · hermetic "today"

_SLUG_RE = _re.compile(r"[^a-z0-9]+")
_WS_RE = _re.compile(r"\s+")
_MONEY_RE = _re.compile(r"\$\s*([0-9]+(?:\.[0-9]{1,2})?)")
_TAG_RE = _re.compile(r"<[^>]+>")


def _txt(v):
    return _WS_RE.sub(" ", str(v or "")).strip()


def _txt_lower(v):
    return _txt(v).lower()


def _slug(v):
    s = _txt_lower(v)
    s = s.encode("ascii", "ignore").decode("ascii")
    return _SLUG_RE.sub("-", s).strip("-") or "pick"


def _int(v, d=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return d


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _price(v):
    m = _MONEY_RE.search(str(v or ""))
    return _num(m.group(1)) if m else _num(v)


def _pct_num(v, old, new):
    old, new = _num(old), _num(new)
    if old <= 0:
        return 0.0
    return round((old - new) * 100.0 / old, 1)


def _iso_week(day=None):
    d = day or EPOCH
    if isinstance(d, _dt.datetime):
        d = d.date()
    y, w, _ = d.isocalendar()
    return "%04d-W%02d" % (y, w)


def _date(v, d=None):
    if v is None:
        return d or EPOCH
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    try:
        return _dt.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return d or EPOCH


# ---------------------------------------------------------------------------
# pick_winners — the best-clicked money picks (MME winners digest). Rows:
#   {keyword, slug, clicks}. Deterministic: clicks desc, slug asc per niche,
#   capped per-niche and total.

def pick_winners(rows, per_niche=DEFAULT_WINNERS_PER_NICHE,
                 total=DEFAULT_TOTAL_WINNERS, min_clicks=DEFAULT_MIN_CLICKS):
    rows = list(rows or [])
    ranked = []
    for r in rows:
        kw = _txt_lower(r.get("keyword"))
        if not kw:
            continue
        slug = _slug(r.get("slug") or r.get("keyword"))
        clicks = _int(r.get("clicks"))
        if clicks < min_clicks:
            continue
        ranked.append({"keyword": kw, "slug": slug, "clicks": clicks})
    ranked.sort(key=lambda c: (-c["clicks"], c["slug"]))
    out, per = [], {}
    for c in ranked:
        if len(out) >= total:
            break
        if per.get(c["keyword"], 0) >= per_niche:
            continue
        per[c["keyword"]] = per.get(c["keyword"], 0) + 1
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# pick_deals — real price drops (MME deals digest). Rows:
#   {keyword, slug, title, old, new}. Deterministic: drop_pct desc, slug asc,
#   per-niche cap, total cap. Only real drops >= min_drop_pct.

def pick_deals(rows, per_niche=DEFAULT_DEALS_PER_NICHE,
               total=DEFAULT_TOTAL_DEALS, min_drop_pct=DEFAULT_MIN_DROP_PCT):
    rows = list(rows or [])
    cands = []
    for r in rows:
        kw = _txt_lower(r.get("keyword"))
        if not kw:
            continue
        slug = _slug(r.get("slug") or r.get("keyword"))
        title = _txt(r.get("title") or slug)
        old = _price(r.get("old"))
        new = _price(r.get("new"))
        drop_pct = _pct_num(r.get("drop_pct"), old, new)
        if drop_pct < min_drop_pct:
            continue
        cands.append({"keyword": kw, "slug": slug, "title": title,
                      "old": round(old, 2), "new": round(new, 2),
                      "drop_pct": drop_pct})
    cands.sort(key=lambda c: (-c["drop_pct"], c["slug"]))
    out, per = [], {}
    for c in cands:
        if len(out) >= total:
            break
        if per.get(c["keyword"], 0) >= per_niche:
            continue
        per[c["keyword"]] = per.get(c["keyword"], 0) + 1
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# pick_referrers — referrers to re-pay (referral loop: re-enrollment). Rows:
#   {email, first_name, keyword, referrals}. Deterministic: referrals desc,
#   email asc.

def pick_referrers(rows, min_referrals=DEFAULT_REFERRAL_MIN):
    rows = list(rows or [])
    out = []
    for r in rows:
        email = _txt_lower(r.get("email"))
        if "@" not in email:
            continue
        refs = _int(r.get("referrals"))
        if refs < min_referrals:
            continue
        out.append({"email": email,
                    "first_name": _txt(r.get("first_name")),
                    "keyword": _txt_lower(r.get("keyword")),
                    "referrals": refs})
    out.sort(key=lambda c: (-c["referrals"], c["email"]))
    return out


# ---------------------------------------------------------------------------
# pick_prune — quiet niches to stop spending email budget on (password: the
#   weekly digest must never burn a tracked-link email on a dead niche).
#   Rows: {keyword, slug, clicks, last_click_at}. Deterministic: keyword asc.

def pick_prune(rows, min_clicks=DEFAULT_PRUNE_MIN_CLICKS,
               quiet_days=DEFAULT_PRUNE_QUIET_DAYS, day=None):
    day = _date(day)
    rows = list(rows or [])
    out = []
    for r in rows:
        kw = _txt_lower(r.get("keyword"))
        if not kw:
            continue
        slug = _slug(r.get("slug") or r.get("keyword"))
        clicks = _int(r.get("clicks"))
        last = _date(r.get("last_click_at"))
        quiet = max(0, (day - last).days)
        if clicks < min_clicks and quiet >= quiet_days:
            out.append({"keyword": kw, "slug": slug, "clicks": clicks,
                        "quiet_days": quiet})
    out.sort(key=lambda c: (c["keyword"], c["slug"]))
    return out


# ---------------------------------------------------------------------------
# weekly_gate — one digest per ISO week per niche (string dedupe; caller
#   persists last sent week as the gate key). Deterministic.

def weekly_gate(keyword, last_week="", week=None, day=None):
    week = _txt_lower(week or _iso_week(day))
    kw = _txt_lower(keyword)
    already = _txt_lower(last_week)
    return {"due": already != week, "week": week, "keyword": kw,
            "last_week": already}


# ---------------------------------------------------------------------------
# digest_email — THE weekly money digest. Returns {"subject","text","html"}
# where the ONLY money link in all three is a single `{{tracked_link}}`
# token (filled by the app's renderer). No .format(), no %-interp on the
# body: hero winner hyperlink is the ONE tracked hop; deels + referrer
# re-enroll CTAs are plain lines. Exactly ONE moustache money token.

def digest_email(keyword, winners, deals, referrers=None, week=None,
                 base_url=""):
    kw = _txt_lower(keyword) or (winners[:1] or [{}])[0].get("keyword") \
        or (deals[:1] or [{}])[0].get("keyword") or "this niche"
    week = week or _iso_week()
    kw_t = kw.title()
    winners = list(winners or [])
    deals = list(deals or [])
    referrers = list(referrers or [])

    subject = "Your weekly %s money picks — %s" % (kw, week)

    # ---- plain text
    text = []
    text.append("Your weekly %s money picks — week of %s" % (kw_t, week))
    text.append("")
    text.append("Hi {{first_name}},")
    text.append("")
    hero = (winners[:1] or deals[:1] or [{}])[0]
    if hero.get("slug") or hero.get("title"):
        text.append("1. %s — best-clicked this week; open it in the email:"
                    % (hero.get("title") or kw_t))
    else:
        text.append("Quiet week in %s — a slow one can be a buying signal. "
                    "Reply and I'll dig up fresh picks." % kw_t)
    if deals:
        text.append("")
        for i, d in enumerate(deals, 1):
            text.append("%d. %s — now %s (was %s, -%s%%)"
                        % (i, d["title"], ("$%0.2f" % d["new"]),
                           ("$%0.2f" % d["old"]), d["drop_pct"]))
    if referrers:
        text.append("")
        for i, r in enumerate(referrers, 1):
            text.append("Referral re-enroll #%d — claim your reward" % i)
    text.append("")
    text.append("— your %s deal-hunter, weekly" % kw_t)
    text.append("")
    text.append("Unsubscribe from these weekly money picks anytime: "
                "{{unsubscribe_url}}")
    text = "\n".join(text)

    # ---- tiny HTML (single {{tracked_link}}; inline-styled like the shell)
    hero = (winners[:1] or deals[:1] or [{}])[0]
    if hero.get("slug") or hero.get("title"):
        lis_w = ("<li style='margin:8px 0'><strong>1.</strong> Hero pick — "
                 "<strong>%s</strong> — "
                 "<a style='color:#a453ff' href='{{tracked_link}}'>see it</a></li>"
                 % (hero.get("title") or hero.get("slug")))
    else:
        lis_w = ("<li style='margin:8px 0'>Quiet week in %s — a slow one can be "
                 "a buying signal. Reply and I'll dig up fresh picks.</li>" % kw)
    lis_w += "".join(
        "<li style='margin:8px 0'>Also best-clicked in %s: <strong>%s</strong></li>"
        % (kw, w["slug"]) for w in winners[1:])
    lis_d = "".join(
        "<li style='margin:8px 0'><strong>%d.</strong> %s — now <strong>%s</strong> "
        "(was %s, <span style='color:#e3562a'>-%s%%</span>)</li>"
        % (i, d["title"], ("$%0.2f" % d["new"]), ("$%0.2f" % d["old"]),
           d["drop_pct"]) for i, d in enumerate(deals, 1))
    lis_r = "".join(
        "<li style='margin:8px 0'>Referral re-enroll #%d — claim your "
        "reward</li>" % i for i, _ in enumerate(referrers, 1))
    body = (lis_w + lis_d + lis_r).strip()
    if not body:
        body = ("<li style='margin:8px 0'>Quiet week in %s — reply and I'll "
                "dig up fresh picks for you.</li>" % kw)

    html = (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "</head><body style='margin:0;padding:0;background:#f4f4f7'>"
        "<table role='presentation' width='100%%' cellspacing='0' cellpadding='0' "
        "style='background:#f4f4f7'><tr><td align='center' style='padding:28px 0'>"
        "<table role='presentation' width='560' cellspacing='0' cellpadding='0' "
        "style='max-width:560px;width:100%%;background:#ffffff;border-radius:16px;"
        "overflow:hidden'>"
        "<tr><td style=\"background:linear-gradient(120deg,#ff7a18 0%%,#a453ff 100%%);"
        "padding:26px 34px\">"
        "<div style=\"font-family:'Inter',Helvetica,Arial,sans-serif;font-size:19px;"
        "font-weight:800;color:#ffffff\">Your weekly %s money picks</div>"
        "<div style=\"font-family:'Inter',Helvetica,Arial,sans-serif;font-size:12px;"
        "color:rgba(255,255,255,.85);margin-top:2px\">week of %s · straight talk</div>"
        "</td></tr><tr><td style='padding:30px 34px'>"
        "<div style=\"font-family:'Inter',Helvetica,Arial,sans-serif;font-size:15px;"
        "line-height:1.65;color:#3a3f4b\">"
        "<p style='margin:0 0 8px'>Hi {{first_name}},</p>"
        "<ul style='margin:0;padding-left:20px'>%s</ul>"
        "<p style='margin:18px 0 0;color:#9aa0ad;font-size:13px'>One friendly weekly "
        "email — reply to get last week's list too.</p>"
        "</div></td></tr><tr><td style='background:#fafafc;padding:16px 34px;"
        "border-top:1px solid #ececf1'>"
        "<div style=\"font-family:'Inter',Helvetica,Arial,sans-serif;font-size:12px;"
        "color:#9aa0ad\">%s — <a href='{{unsubscribe_url}}' style='color:#9aa0ad'>"
        "unsubscribe</a></div>"
        "</td></tr></table></td></tr></table></body></html>"
        % (kw_t, week, body, kw)
    )

    return {"subject": subject, "text": text, "html": html,
            "keywords": [kw], "winners": winners, "deals": deals,
            "referrers": referrers}
